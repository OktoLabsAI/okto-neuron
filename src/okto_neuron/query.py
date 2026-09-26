"""Unified retrieval over the knowledge graph.

``search_claims`` blends full-scan cosine (vector), BM25 (lexical), and exact
multiword-title legs via reciprocal-rank fusion, expands the top seeds one hop
through the graph, then applies a final content gate: infrastructure/provenance
plumbing nodes are never returned (``_internal.infra.is_infra``), and raw
``Block`` substrate is dropped from the untyped recall/ask path. Returns RAW
``(store-Node, score)`` pairs; the vault maps these to ``QueryHit`` with
byte-range provenance.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from typing import TYPE_CHECKING, MutableMapping

from okto_neuron._internal.infra import is_infra, is_superseded
from okto_neuron.core.schema import Node
from okto_neuron.extract import _is_low_value_title, _is_metadata_predicate
from okto_neuron.models import ContextSpan

if TYPE_CHECKING:
    from okto_neuron.embed import EmbeddingProvider
    from okto_neuron.store.index.protocol import IndexStore
    from okto_neuron.store.protocol import GraphStore

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

# Reciprocal-rank-fusion constant (standard TREC value). Larger -> flatter
# rank weighting; 60 keeps top ranks dominant without starving the tail.
_RRF_K = 60
# Number of top fused seeds expanded through the graph, and the rank discount
# applied to a 1-hop neighbour's borrowed RRF contribution.
_EXPANSION_SEEDS = 10
_HOP_DISCOUNT = 0.5

# ── Fix B (task-12): seed-quality diversification for the ask/explore seed cut.
# The answer-first partition below (see ``search_claims``) floods the top-20 seed
# slots with lexically-echoing scalar-literal Claims (use_case/setting_effect/
# has_config family) on interrogative infra questions, locking out topic entities
# and the answering relationship Claim (measured on a synthetic service/database
# example at fused rank 41 while 20/20 seeds were Claims). ``_diversify_seeds`` is a pure,
# deterministic, zero-LLM re-cut applied ONLY when ``seed_diversity=True`` —
# default recall/query output stays byte-identical (deterministic-floor CI gate).
# Quota defaults are per the task-12 plan; overridable via ``llm.ask`` step
# config / AskRetrievalPolicy, and swept before declaring the mechanism wrong.
_SEED_SUBJECT_CAP = 3  # max Claims per subject entity within the selected seeds
_SEED_ENTITY_MIN = 6  # soft floor: non-Claim (entity) seeds
_SEED_REL_MIN = 4  # soft floor: relationship Claims (O_id truthy)
_SEED_SCALAR_MAX = 10  # hard-ish cap: scalar-literal Claims (backfill may exceed)

# Question-aware answer-Claim retrieval (interrogative queries only). The bare
# entity a question is *about* covers only the topic term(s); the relationship
# Claim that ANSWERS the question covers the discriminative (rare) term too. We
# rank answer Claims by IDF-weighted query-term coverage and fuse that as a THIRD
# RRF list alongside vector + lexical — staying in the rank domain (no additive
# boost, no magic constant). IDF down-weights the common topic term so the answer
# Claim outranks the topic entity on merit. Grounding: Robertson/Zaragoza/Taylor
# BM25F (CIKM 2004); Cormack/Clarke/Buettcher RRF (SIGIR 2009); Yu et al. KBQA
# answer-triple ranking (ACL 2017); plain coverage = Losee coordination-level
# matching (JASIS 1987), the degenerate no-IDF form this replaces.
_WH = {"who", "what", "when", "where", "which", "whom", "whose", "why", "how"}
_STOP = {
    "is",
    "are",
    "was",
    "were",
    "the",
    "a",
    "an",
    "of",
    "on",
    "in",
    "to",
    "for",
    "and",
    "or",
    "with",
    "by",
    "at",
    "be",
    "do",
    "does",
    "did",
    "this",
    "that",
    "it",
    "its",
    "i",
    "me",
    "my",
}
_STRUCTURAL_CLAIM_PREDICATES = {"has_heading", "has_tag", "links_to"}


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.lower())


def _question_terms(query: str) -> set[str] | None:
    """Return the query's content terms iff it reads as a question, else None.

    Interrogative gate: the query ends with ``?`` OR its first token is a
    wh-word. Non-interrogative queries return None so the answer list is empty
    and the third RRF leg is a no-op (keeps the deterministic eval set
    byte-for-byte unchanged)."""
    q = query.strip().lower()
    toks = re.findall(r"[a-z0-9]+", q)
    if not toks:
        return None
    if not (q.endswith("?") or toks[0] in _WH):
        return None
    terms = {t for t in toks if len(t) >= 2 and t not in _WH and t not in _STOP}
    return terms or None


def _answer_ranked(query: str, candidates: list[Node]) -> list[Node]:
    """Interrogative-gated answer list: Claims ranked by IDF-weighted query-term
    coverage (a BM25F-style coverage signal computed over the Claim candidate
    pool). Empty for non-interrogative queries → the RRF fold is a no-op and the
    golden ``entity@k`` set is unchanged.

    IDF is computed LOCALLY over the Claim pool (not pulled from the store: there
    is no public IDF accessor, LadybugStore keeps it private, and InMemoryStore
    has none — so a local computation is store-agnostic and identical under both).
    The formula mirrors LadybugStore's lexical-leg IDF so behaviour is consistent.
    """
    qterms = _question_terms(query)
    if not qterms:
        return []
    claims = [n for n in candidates if n.type == "Claim" and not _is_structural_claim(n)]
    if not claims:
        return []
    toks = {n.id: set(re.findall(r"[a-z0-9]+", f"{n.title} {n.content}".lower())) for n in claims}
    n_claims = len(claims)
    df = {t: sum(1 for n in claims if t in toks[n.id]) for t in qterms}
    idf = {t: math.log(1 + (n_claims - df[t] + 0.5) / (df[t] + 0.5)) for t in qterms}
    scored: list[tuple[float, Node]] = []
    for node in claims:
        score = sum(idf[t] for t in qterms if t in toks[node.id])
        if score > 0.0:
            scored.append((score, node))
    # Deterministic order: IDF-coverage desc, then node id asc for ties.
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [node for _, node in scored]


def _is_structural_claim(node: Node) -> bool:
    pred = re.sub(
        r"[^a-z0-9]+",
        "_",
        str(node.facets.get("P") or "").strip().casefold(),
    ).strip("_")
    if pred in _STRUCTURAL_CLAIM_PREDICATES or _is_metadata_predicate(pred):
        return True
    title = str(node.title or "").strip().casefold()
    return title.startswith(("heading:", "tag:", "wikilink:"))


def _title_terms(node: Node) -> tuple[list[str], set[str]]:
    title_tokens = _tokens(str(node.title or ""))
    title_terms = {token for token in title_tokens if token not in _STOP}
    return title_tokens, title_terms


def _title_ranked(query: str, candidates: list[Node]) -> list[Node]:
    """Rank named-title term coverage without promoting one-word generic nodes.

    This is a title-specific RRF leg, not an additive score boost. A node qualifies
    only when its non-stopword title terms are all present in the query and the
    title has at least two such terms. That lifts stable names like "Alistair
    Cockburn User Story Format" or "Definition of Done" while leaving generic
    one-token taxonomy nodes such as "Story" to the normal vector/lexical legs.
    """
    query_tokens = _tokens(query)
    query_terms = {token for token in query_tokens if token not in _STOP}
    if not query_terms:
        return []

    scored: list[tuple[float, Node]] = []
    for node in candidates:
        title_tokens, unique_terms = _title_terms(node)
        if len(unique_terms) < 2 or not unique_terms <= query_terms:
            continue
        score = (len(unique_terms) + min(len(title_tokens), 12) / 100.0) * node_quality_weight(node)
        scored.append((score, node))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [node for _, node in scored]


def _exact_title_ranked(query: str, candidates: list[Node]) -> list[Node]:
    """Rank exact multiword title phrases as a separate RRF signal."""
    query_tokens = _tokens(query)
    query_terms = {token for token in query_tokens if token not in _STOP}
    query_phrase = " ".join(query_tokens)
    if not query_terms:
        return []

    scored: list[tuple[float, Node]] = []
    for node in candidates:
        title_tokens, unique_terms = _title_terms(node)
        if len(unique_terms) < 2:
            continue
        if unique_terms <= query_terms and " ".join(title_tokens) in query_phrase:
            score = (len(unique_terms) + min(len(title_tokens), 12) / 100.0) * node_quality_weight(
                node
            )
            scored.append((score, node))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [node for _, node in scored]


# ── quality weighting (Layer 2: demote already-committed metadata/low-value noise)
# A multiplier in (0,1] applied to a node's per-channel score so metadata/tag
# Claims and version/number "entities" rank BELOW real content WITHOUT being
# removed (a penalized-but-relevant node still surfaces, just lower). Pattern-
# based only: never penalizes a merely-short legit title (NX, AI, initials).
#
# The primary signal for a metadata Claim is its predicate facet ``P`` (companion
# sets P=predicate and dumps it into the Claim facets) — NOT the title, because a
# real minted tag Claim is titled "<Subject> tag <object>" with the predicate in
# the MIDDLE, so a title regex would miss the actual committed noise. The title
# regex is a secondary catch for the literal "tag …"/"version …" shape.
_META_TITLE_RE = re.compile(r"^(?:tags?|label|version)\b", re.IGNORECASE)
_METADATA_WEIGHT = 0.15
_LOW_VALUE_WEIGHT = 0.3


def node_quality_weight(node: Node) -> float:
    """Score multiplier in (0,1] demoting metadata/low-value nodes; 1.0 normally."""
    pred = str(node.facets.get("P") or "").strip().lower()
    if pred and _is_metadata_predicate(pred):
        return _METADATA_WEIGHT
    title = str(node.title or "")
    if _META_TITLE_RE.match(title.strip()):
        return _METADATA_WEIGHT
    if _is_low_value_title(title):
        return _LOW_VALUE_WEIGHT
    return 1.0


def _cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    mag_a = math.sqrt(sum(a[i] * a[i] for i in range(n)))
    mag_b = math.sqrt(sum(b[i] * b[i] for i in range(n)))
    if not mag_a or not mag_b:
        return 0.0
    return dot / (mag_a * mag_b)


def _vector_seeds(
    query_embedding: list[float],
    index: "IndexStore",
    store: "GraphStore",
    type: str | None = None,
) -> list[tuple[Node, float]]:
    """Full cosine scan over every node carrying an embedding (recall floor).

    There is no ANN index, so this is a deliberate full scan: any node with a
    non-trivial cosine to the query is reachable even with zero token overlap.
    The recall floor is the untruncated ``IndexStore.scan_vectors``.
    """
    pairs = list(index.scan_vectors(type=type))
    nodes = store.get_nodes([node_id for node_id, _ in pairs])
    scored: list[tuple[Node, float]] = []
    for node in nodes:
        score = _cosine(query_embedding, node.embedding) * node_quality_weight(node)
        if score > 0.0:
            scored.append((node, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def _rrf(ranked_lists: list[list[Node]]) -> dict[str, float]:
    """Reciprocal-rank fusion of several ranked node lists (by node id)."""
    fused: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, node in enumerate(ranked):
            fused[node.id] = fused.get(node.id, 0.0) + 1.0 / (_RRF_K + rank + 1)
    return fused


def _degree(store: "GraphStore", node_id: str, cache: dict[str, int]) -> int:
    degree = cache.get(node_id)
    if degree is None:
        degree = sum(1 for _ in store.list_edges(src=node_id)) + sum(
            1 for _ in store.list_edges(dst=node_id)
        )
        cache[node_id] = max(1, degree)
    return cache[node_id]


def _normalized_fact_object(value: object) -> str:
    """Conservative object normalization for fact dedup: lowercase alnum-token
    join. Empty string means "not normalizable" → never merged."""
    return " ".join(_tokens(str(value or "")))


def _seed_class(node: Node) -> str:
    """Seed class for quota accounting: ``entity`` (any non-Claim node), ``rel``
    (relationship Claim — object is a graph node, ``O_id`` truthy), or ``scalar``
    (literal-object Claim, the flooding class)."""
    if node.type != "Claim":
        return "entity"
    if str(node.facets.get("O_id") or "").strip():
        return "rel"
    return "scalar"


def _diversify_seeds(
    results: list[tuple[Node, float]],
    limit: int,
    *,
    subject_cap: int = _SEED_SUBJECT_CAP,
    entity_min: int = _SEED_ENTITY_MIN,
    rel_min: int = _SEED_REL_MIN,
    scalar_max: int = _SEED_SCALAR_MAX,
) -> list[tuple[Node, float]]:
    """Pure, deterministic seed-cut diversification (Fix B, task-12).

    Operates on the FULL fused results list (already sorted: answer-partition
    first, then fused score) and selects ``limit`` seeds:

    1. **Fact dedup** — Claims sharing ``(S_id, predicate, normalized object)``
       collapse to the best-ranked representative, so a Claim re-minted verbatim
       (same subject, same predicate, same object) costs ONE slot, while two
       distinct facts about the same subject+object under different predicates
       (e.g. ``status="active"`` vs. ``priority="active"``) are never merged.
       Conservative: no ``S_id`` or no normalizable object → never merged.
    2. **Soft class floors scanning the full list** — up to ``entity_min``
       non-Claim nodes and ``rel_min`` relationship Claims are admitted in fused
       order even from deep ranks (this is what rescues the rank-41 answering
       REL claim). Floors are soft: underpopulated classes hand their slots back
       to fused-order fill.
    3. **Caps** — at most ``scalar_max`` scalar-literal Claims and at most
       ``subject_cap`` Claims per subject entity; overflow returns ONLY via the
       final backfill (when nothing admissible remains, fused order fills the
       remaining slots so output length == ``min(limit, len(deduped))``).

    Output preserves the input (fused) ordering among selected seeds; scores are
    untouched. Zero LLM, zero store reads — classification is facet reads on
    nodes already in memory.
    """
    if limit <= 0 or not results:
        return []

    # Component 1 — fact dedup (runs first, over the full list).
    deduped: list[tuple[Node, float]] = []
    seen_facts: set[tuple[str, str, str]] = set()
    for node, score in results:
        if node.type == "Claim":
            s_id = str(node.facets.get("S_id") or "").strip()
            pred = str(node.facets.get("P") or "").strip().casefold()
            obj = node.facets.get("O_id") or node.facets.get("O_literal")
            norm = _normalized_fact_object(obj)
            if s_id and norm:
                key = (s_id, pred, norm)
                if key in seen_facts:
                    continue
                seen_facts.add(key)
        deduped.append((node, score))

    if len(deduped) <= limit:
        return deduped

    selected: set[int] = set()
    selected_ids: set[str] = set()
    subject_counts: dict[str, int] = {}
    class_counts = {"entity": 0, "rel": 0, "scalar": 0}

    def _subject(node: Node) -> str:
        if node.type != "Claim":
            return ""
        return str(node.facets.get("S_id") or "").strip()

    def _admissible(node: Node) -> bool:
        cls = _seed_class(node)
        if cls == "scalar" and class_counts["scalar"] >= scalar_max:
            return False
        subj = _subject(node)
        if subj and subject_counts.get(subj, 0) >= subject_cap:
            return False
        return True

    def _take(index: int, node: Node) -> None:
        selected.add(index)
        selected_ids.add(node.id)
        class_counts[_seed_class(node)] += 1
        subj = _subject(node)
        if subj:
            subject_counts[subj] = subject_counts.get(subj, 0) + 1

    # Component 3a — soft floors, filled in fused order from the FULL list.
    for i, (node, _) in enumerate(deduped):
        if len(selected) >= limit or class_counts["entity"] >= entity_min:
            break
        if _seed_class(node) == "entity":
            _take(i, node)
    for i, (node, _) in enumerate(deduped):
        if len(selected) >= limit or class_counts["rel"] >= rel_min:
            break
        if node.id in selected_ids:
            continue
        if _seed_class(node) == "rel" and _admissible(node):
            _take(i, node)

    # Fused-order fill under the caps (components 2 + 3b).
    for i, (node, _) in enumerate(deduped):
        if len(selected) >= limit:
            break
        if node.id in selected_ids:
            continue
        if _admissible(node):
            _take(i, node)

    # Soft backfill: caps never leave slots empty — fused order fills the rest.
    for i, (node, _) in enumerate(deduped):
        if len(selected) >= limit:
            break
        if node.id in selected_ids:
            continue
        _take(i, node)

    return [deduped[i] for i in sorted(selected)]


def search_claims(
    query: str,
    *,
    k: int,
    store: "GraphStore",
    embedder: "EmbeddingProvider",
    index: "IndexStore | None" = None,
    type: str | None = None,
    equivalence: dict[str, str] | None = None,
    predicate_aliases: dict[str, str] | None = None,
    seed_diversity: bool = False,
    seed_subject_cap: int | None = None,
    seed_entity_min: int | None = None,
    seed_rel_min: int | None = None,
    seed_scalar_max: int | None = None,
    metrics: MutableMapping[str, object] | None = None,
) -> list[tuple[Node, float]]:
    """Unified fused retriever over content nodes (or one explicit ``type``).

    Blends a full-scan cosine (vector) leg, a BM25 (lexical) leg, and an exact
    multiword-title leg via reciprocal-rank fusion, then expands the top seeds
    one hop through the graph with a hop-discounted contribution. Returns RAW
    ``(store-Node, score)`` pairs sorted highest-first, ``len <= max(k, 20)``.
    The vault maps these to
    ``QueryHit`` with byte-range provenance; this layer never touches provenance.

    Infrastructure nodes are always removed. An untyped query also removes raw
    ``Block`` substrate; callers can request ``type="Block"`` explicitly.

    ``type`` scopes BOTH candidate legs to that node type so a type-filtered query
    still recovers the full top-``max(k,20)`` of that type — never a thin residue
    left over after a global cut.

    ``equivalence`` (``member_id -> canonical_id``, default ``None``) is the
    Option-A query-time consolidation fold (ADR 0008): a pure, read-only
    post-results transform that collapses each equivalence class to ONE
    representative. ``None`` (the default) leaves output byte-for-byte unchanged;
    this layer never loads the map — the vault passes it in, keeping ``query``
    filesystem-free. ``predicate_aliases`` is the equivalent read-time Claim
    predicate fold for ADR 0017 exact-match aliases.

    ``seed_diversity`` (default ``False``) applies :func:`_diversify_seeds` at
    the final cut (Fix B, task-12): fact dedup + subject cap + predicate-class
    quotas scanning the full fused list, so scalar-literal Claims cannot flood
    all seed slots and topic entities / answering REL claims from deep ranks
    enter the seed set. ``False`` keeps the output byte-identical to the shipped
    behaviour (the deterministic-floor regression pin). The ``seed_*`` quota
    kwargs override the module constants; ``None`` → default.

    ``index`` is the vault IndexStore; ``None`` builds an ephemeral in-memory
    index from ``store`` (library and test convenience, one full pass per call).
    """
    if index is None:
        from okto_neuron.store.index import InMemoryIndexStore, reindex_all

        index = InMemoryIndexStore()
        reindex_all(store, index)

    limit = max(k, 20)

    embedding_started = time.perf_counter()
    query_embedding = embedder.embed(query)
    embedding_ms = (time.perf_counter() - embedding_started) * 1000
    if metrics is not None:
        provider_name = str(getattr(embedder, "provider_name", None) or embedder.__class__.__name__)
        model = str(
            getattr(embedder, "model_name", None)
            or getattr(embedder, "model", None)
            or getattr(embedder, "_model_name", None)
            or embedder.__class__.__name__
        )
        execution = str(getattr(embedder, "execution_location", None) or "unknown")
        metrics.update(
            {
                "query_embedding_calls": 1,
                "query_embedding_latency_ms": round(embedding_ms, 3),
                "query_embedding_provider": provider_name,
                "query_embedding_model": model,
                "query_embedding_execution": execution,
            }
        )
    retrieval_started = time.perf_counter()
    vector_ranked = [node for node, _ in _vector_seeds(query_embedding, index, store, type)]
    lexical_ranked = store.get_nodes([node_id for node_id, _ in index.search_text(query, k=limit, type=type)])
    title_candidates = list(store.list_nodes(type=type))
    exact_title_ranked = _exact_title_ranked(query, title_candidates)
    title_ranked = _title_ranked(query, title_candidates)

    # De-dup the candidate pool by id (first-seen order) — the answer list scores
    # over Claims already recalled by the title/vector/lexical legs, so it never
    # introduces unrelated Claims outside the fused candidate surface.
    pool: list[Node] = []
    pool_ids: set[str] = set()
    for node in (*exact_title_ranked, *title_ranked, *vector_ranked, *lexical_ranked):
        if node.id not in pool_ids:
            pool_ids.add(node.id)
            pool.append(node)

    # Third RRF leg: for interrogative queries, an IDF-weighted answer-Claim list
    # (empty otherwise → no-op). An answer Claim then earns a THIRD reciprocal-rank
    # contribution on top of its vector+lexical ranks and accumulates more fused
    # mass than entity-title nodes that appear in only two lists — lifting it to
    # top-1 in the rank domain, bounded and scale-free (no ceiling, no ε).
    answer_ranked = _answer_ranked(query, pool)

    fused = _rrf(
        [
            exact_title_ranked,
            title_ranked,
            vector_ranked,
            lexical_ranked,
            answer_ranked,
        ]
    )

    # Keep a handle on every node we have seen so expansion can resolve ids.
    seen: dict[str, Node] = {n.id: n for n in pool}

    # Graph expansion: borrow a hop-discounted, target-degree-normalized slice of
    # each seed's fused score for its 1-hop neighbours (matched entity ->
    # connected Claim/edge -> node). Degree normalization keeps generic hubs from
    # out-accumulating exact named matches merely because many claims point at
    # them.
    top_seeds = sorted(fused.items(), key=lambda item: item[1], reverse=True)
    degree_cache: dict[str, int] = {}
    for seed_id, seed_score in top_seeds[:_EXPANSION_SEEDS]:
        neighbour_ids: set[str] = set()
        for edge in store.list_edges(src=seed_id):
            neighbour_ids.add(edge.dst)
        for edge in store.list_edges(dst=seed_id):
            neighbour_ids.add(edge.src)
        for neighbour_id in neighbour_ids:
            if neighbour_id == seed_id:
                continue
            node = seen.get(neighbour_id)
            if node is None:
                node = store.get_node(neighbour_id)
                if node is None:
                    continue
                seen[neighbour_id] = node
            borrowed = seed_score * _HOP_DISCOUNT / _degree(store, neighbour_id, degree_cache)
            fused[neighbour_id] = fused.get(neighbour_id, 0.0) + borrowed

    # Final gate: infra/provenance plumbing is NEVER content (for any type), and
    # raw Block substrate is dropped from the untyped recall/ask path (Blocks are
    # the anchored source-text a Claim is derived from, reachable via the Claim's
    # block_id — surfacing them is surfacing the raw chunk instead of the asserted
    # knowledge). An explicit type="Block" browser query still works.
    # NB: ADR 0011 1b (dropping 0-byte Document nodes too) was reverted — the eval
    # showed it did not move accuracy and it changed the DEFAULT recall output (a
    # "which doc mentions X" answer could resolve to a Document title). Anchorless
    # Documents are instead filtered inside the subgraph builder's quality gates,
    # keeping this shared search_claims path byte-identical for the default.
    # NB: low-salience anchors (e.g. deterministic ``has_heading`` Claims) are NOT
    # hard-dropped here. Lexical/vector recall must surface any node that genuinely
    # matches the query — a document's primary heading is often its subject, so
    # its ``has_heading`` Claim is the byte-anchored provenance target for a topical
    # query (FR3 golden qa2/qa3). Genuine structural noise ("Review Notes", section
    # labels) already scores far lower than content-bearing matches, so plain
    # scoring separates it without a filter. Structural anchors are still curated
    # OUT of the graph-native ask path — ``subgraph.py`` keeps its own
    # ``is_low_salience`` gate — and ``node_quality_weight`` still soft-demotes
    # metadata predicates here.
    results = [
        (seen[node_id], score * node_quality_weight(seen[node_id]))
        for node_id, score in fused.items()
        if node_id in seen
        and (type is None or seen[node_id].type == type)
        and not is_infra(seen[node_id])
        and not is_superseded(seen[node_id])
        and not (type is None and seen[node_id].type == "Block")
    ]
    results.sort(key=lambda item: item[1], reverse=True)

    if equivalence:
        results = _fold_equivalence(results, equivalence)
    if predicate_aliases:
        results = _fold_predicate_aliases(results, predicate_aliases)

    final_answer_ranked = _answer_ranked(query, [node for node, _ in results])
    if final_answer_ranked:
        answer_order = {node.id: rank for rank, node in enumerate(final_answer_ranked)}
        results.sort(
            key=lambda item: (
                0 if item[0].id in answer_order else 1,
                answer_order.get(item[0].id, len(answer_order)),
                -item[1],
                item[0].id,
            )
        )

    # Fix B (task-12): the answer partition above stays untouched (it keeps its
    # shipped behaviour on default recall); the diversified cut subsumes its
    # flooding damage on the ask/explore seed path only.
    #
    # Owner decision 2026-07-28 (ADR 0040 addendum): this partition was removed in
    # the ADR 0039/0040 landing commit and reinstated here after a measured A/B. Removing it costs the
    # deterministic floor 1 hit and 0.1742 MRR@10 on the frozen ``synthetic-ci``
    # vault. Do not remove it again without a measured seed-path test first.
    if seed_diversity:
        final_results = _diversify_seeds(
            results,
            limit,
            subject_cap=seed_subject_cap if seed_subject_cap is not None else _SEED_SUBJECT_CAP,
            entity_min=seed_entity_min if seed_entity_min is not None else _SEED_ENTITY_MIN,
            rel_min=seed_rel_min if seed_rel_min is not None else _SEED_REL_MIN,
            scalar_max=seed_scalar_max if seed_scalar_max is not None else _SEED_SCALAR_MAX,
        )
    else:
        final_results = results[:limit]
    if metrics is not None:
        metrics["deterministic_retrieval_latency_ms"] = round(
            (time.perf_counter() - retrieval_started) * 1000,
            3,
        )
    return final_results


def _fold_predicate_aliases(
    results: list[tuple[Node, float]], predicate_aliases: dict[str, str]
) -> list[tuple[Node, float]]:
    """Return Claim nodes with facet ``P`` rewritten through ``predicate_aliases``.

    Pure transform over result nodes; stored graph rows are not mutated.
    """
    folded: list[tuple[Node, float]] = []
    for node, score in results:
        if node.type != "Claim":
            folded.append((node, score))
            continue
        facets = dict(node.facets or {})
        predicate = str(facets.get("P") or "")
        canonical = predicate_aliases.get(predicate)
        if canonical is None or canonical == predicate:
            folded.append((node, score))
            continue
        folded.append((node.model_copy(update={"facets": {**facets, "P": canonical}}), score))
    return folded


def _fold_equivalence(
    results: list[tuple[Node, float]], equivalence: dict[str, str]
) -> list[tuple[Node, float]]:
    """Collapse each equivalence class to ONE representative (Option A, ADR 0008).

    Pure transform: group result tuples by ``equivalence.get(node.id, node.id)``;
    per class keep the canonical node if present in the results, else the
    highest-scoring member; carry score = max member score; preserve overall
    desc-by-score order. Read-only — touches no store."""
    by_class: dict[str, list[tuple[Node, float]]] = {}
    for node, score in results:
        canonical = equivalence.get(node.id, node.id)
        by_class.setdefault(canonical, []).append((node, score))

    folded: list[tuple[Node, float]] = []
    for canonical, members in by_class.items():
        max_score = max(score for _, score in members)
        rep = next((n for n, _ in members if n.id == canonical), None)
        if rep is None:
            rep = max(members, key=lambda item: item[1])[0]
        folded.append((rep, max_score))
    folded.sort(key=lambda item: item[1], reverse=True)
    return folded


def _sha256_uri(value: object) -> str:
    raw = str(value or "")
    digest = raw.removeprefix("sha256:")
    if _SHA256_HEX.fullmatch(digest):
        return f"sha256:{digest}"
    return f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def build_block_index(store: "GraphStore") -> dict[str, list[Node]]:
    """Map ``source_path`` -> its Blocks sorted by ``block_index``.

    Built once per query so neighbor expansion over k hits costs a single Block
    scan, not one per hit (the ADR flags a real path-indexed accessor as later
    work; this keeps Phase 2 additive without a store-protocol change)."""
    index: dict[str, list[Node]] = {}
    for node in store.list_nodes(type="Block"):
        path = str(node.facets.get("source_path") or "")
        if path:
            index.setdefault(path, []).append(node)
    for blocks in index.values():
        blocks.sort(key=lambda n: int(n.facets.get("block_index") or 0))
    return index


def expand_block_context(
    store: "GraphStore",
    block_id: str,
    *,
    k_neighbors: int,
    index: dict[str, list[Node]] | None = None,
) -> list[ContextSpan]:
    """Same-document neighbors of ``block_id``, ``k_neighbors`` on each side.

    Returns the adjacent Blocks (by ``block_index`` on the SAME ``source_path``),
    excluding the seed block itself, in document order. Returns ``()`` cleanly
    when ``k_neighbors <= 0``, the block id is empty/unknown, or the block has no
    anchored path — entity hits with no single source block simply get no
    context. Neighbors share the seed's exact path string, so this introduces no
    new path into the read/disclosure trust surface."""
    if k_neighbors <= 0 or not block_id:
        return []
    block = store.get_node(block_id)
    if block is None:
        return []
    path = str(block.facets.get("source_path") or "")
    if not path:
        return []
    blocks = (index or build_block_index(store)).get(path, [])
    pos = next((i for i, n in enumerate(blocks) if n.id == block_id), None)
    if pos is None:
        return []
    lo = max(0, pos - k_neighbors)
    hi = min(len(blocks), pos + k_neighbors + 1)
    spans: list[ContextSpan] = []
    for n in blocks[lo:pos] + blocks[pos + 1 : hi]:
        f = n.facets
        spans.append(
            ContextSpan(
                path=path,
                byte_start=int(f.get("byte_start") or 0),
                byte_end=int(f.get("byte_end") or 0),
                content_hash=_sha256_uri(f.get("content_hash") or f.get("sha256") or n.id),
                block_id=str(n.id),
                block_index=int(f.get("block_index") or 0),
            )
        )
    spans.sort(key=lambda s: s.block_index)
    return spans
