"""Ego-graph construction + typed rendering for the subgraph-first ask path.

ADR 0011 Tier 1: instead of dumping ~12 KB raw byte-slices of the top-k hits
(~20K tokens, the block-dump path), answer from a *relevance-capped ego-graph* —
the seed nodes, the bridging Claims that mention them, and their 1-hop graph
neighbours via the ``rdf:subject`` / ``rdf:object`` / ``schema:mentions`` bridge
edges — rendered as a compact typed ``=== NODES / RELATIONSHIPS / CLAIMS ===``
block (~hundreds of tokens).

Two halves, kept apart on purpose:

- ``build_ego_graph`` reads the store (id-based, raw ``node.facets``), applies the
  MANDATORY degree cap + relevance rank during collection (hub-explosion blocker),
  carries over the ``is_infra`` / ``node_quality_weight`` quality gates, and dedups
  on the ``(S_id, P, O_id|O_literal)`` triple.
- ``render_ego_subgraph`` is PURE — no store, no config, no LLM. It only turns an
  ``EgoGraph`` into text, honouring the ``O_id`` XOR ``O_literal`` branch
  (relationship row vs verbalized-claim row) and a seeds-first token budget.

Edge-direction semantics (load-bearing, from ``ingest/__init__.py:131-133``):
  ``Claim    -[rdf:subject]->  subjectEntity``
  ``Claim    -[rdf:object]->   objectEntity``
  ``Document -[schema:mentions]-> entity``
So an *entity* seed is the ``dst`` of its bridging Claims (``list_edges(dst=seed)``
filtered to ``rdf:subject``/``rdf:object``); the bridging Claim is ``edge.src`` and
is fetched separately to read ``confidence`` + ``S_id``/``O_id``. A *Claim* seed
reaches its endpoints via ``list_edges(src=claim)``. ``schema:mentions`` only
attaches the source Document to an entity — we never enumerate a Document's *other*
mentions (that is the 100-mention hub bloom the degree cap guards against).
"""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from okto_neuron._internal.infra import is_infra, is_low_salience, is_superseded
from okto_neuron.extract import _is_metadata_predicate
from okto_neuron.query import node_quality_weight

if TYPE_CHECKING:
    from okto_neuron.core.schema import Node
    from okto_neuron.store.protocol import GraphStore

__all__ = [
    "NodeRecord",
    "RelRecord",
    "ClaimRecord",
    "EgoGraph",
    "build_ego_graph",
    "render_ego_subgraph",
]

# Deviation from plan §3.1: ``schema:mentions`` is intentionally NOT traversed.
# The plan lists it in the bridge set, but the only ``schema:mentions`` walk worth
# making (entity → its source Document) dead-ends — Document nodes are dropped from
# the answerable pool in Phase 1b, and walking a Document's *other* mentions is the
# 100-mention hub bloom §7 explicitly forbids. So entity expansion uses only the
# ``rdf:subject`` / ``rdf:object`` topology bridges. Kept as a comment, not a dead
# constant, so the omission is explicit rather than implied.
# Fallback confidence when a Claim's facet is missing (mirrors
# companion._CLAIM_BASELINE_CONFIDENCE; duplicated here to keep this module
# free of a companion import cycle).
_CLAIM_BASELINE_CONFIDENCE = 0.7

# _ranked_bridge_claims' hub-explosion backstop (finding 3.18): the
# GraphStore.list_edges protocol has no server-side ``limit``, so the edge scan
# feeding candidate collection is bounded client-side to a constant multiple of
# ``degree_cap`` instead of the seed's true (unbounded) degree. This caps the
# per-candidate get_node/gate/score work — the dominant per-query cost for a
# hub entity — at O(degree_cap) rather than O(true degree). The factor is large
# enough that ordinary (non-hub) seeds are never truncated, so ranking among
# candidates that fit within the scan cap is unaffected.
_BRIDGE_CLAIM_SCAN_CAP_FACTOR = 8
_BRIDGE_CLAIM_SCAN_CAP_FLOOR = 64


# ── records ───────────────────────────────────────────────────────────────────
@dataclass
class NodeRecord:
    """One node in the ego-graph (seed or 1-hop neighbour)."""

    id: str
    type: str
    name: str
    is_seed: bool
    score: float = 0.0
    block_id: str = ""
    span: str = ""


@dataclass
class RelRecord:
    """A topology assertion: ``subject -[predicate]-> object`` (``O_id`` claim)."""

    claim_id: str
    subject_id: str
    predicate: str
    object_id: str
    confidence: float
    block_id: str = ""
    # IDF-weighted query-term coverage of this Claim (0.0 for non-interrogative
    # queries). Carried from build time so the render sort keeps the discriminative
    # answering row instead of dropping it by confidence — see _answer_coverage_scores.
    answer_score: float = 0.0


@dataclass
class ClaimRecord:
    """A propositional assertion: ``subject predicate <literal>`` (``O_literal``)."""

    claim_id: str
    subject_id: str
    subject_name: str
    predicate: str
    literal: str
    confidence: float
    block_id: str = ""
    answer_score: float = 0.0


@dataclass
class EgoGraph:
    """The relevance-capped ego-graph assembled around the query seeds."""

    nodes: dict[str, NodeRecord] = field(default_factory=dict)
    relationships: list[RelRecord] = field(default_factory=list)
    claims: list[ClaimRecord] = field(default_factory=list)


# ── helpers ───────────────────────────────────────────────────────────────────
def _claim_confidence(node: "Node") -> float:
    raw = node.facets.get("confidence")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _CLAIM_BASELINE_CONFIDENCE


def _span_repr(node: "Node") -> str:
    """A short ``span:`` anchor for Tier-2 fetch, or ``""``. Reads the optional
    ``source_span`` facet (vault-relative path + byte range)."""
    span = node.facets.get("source_span")
    if isinstance(span, dict):
        start = span.get("byte_start")
        end = span.get("byte_end")
        if start is not None and end is not None:
            return f"{start}-{end}"
    return ""


def _node_record(node: "Node", *, is_seed: bool, score: float) -> NodeRecord:
    return NodeRecord(
        id=node.id,
        type=str(node.type or ""),
        name=str(node.title or node.id),
        is_seed=is_seed,
        score=score,
        block_id=str(node.facets.get("block_id") or ""),
        span=_span_repr(node),
    )


def _passes_gates(node: "Node") -> bool:
    """A node may enter the render iff it is neither infra plumbing nor demoted to
    zero-ish quality. Mirrors the recall final gate (``query.py``)."""
    if is_infra(node) or is_low_salience(node) or is_superseded(node):
        return False
    return node_quality_weight(node) > 0.0


def _verbalize_predicate(predicate: str) -> str:
    """``participated_in`` → ``participated in``. Pure string transform."""
    return predicate.replace("_", " ").lower().strip()


def _cosine_sim(a: list[float] | None, b: list[float] | None) -> float:
    """Cosine similarity between two embedding vectors, returns 0.0 on missing/dim."""
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    mag_a = math.sqrt(sum(a[i] * a[i] for i in range(n)))
    mag_b = math.sqrt(sum(b[i] * b[i] for i in range(n)))
    if not mag_a or not mag_b:
        return 0.0
    return dot / (mag_a * mag_b)


def _answer_coverage_scores(
    claims: list["Node"],
    answer_terms: frozenset[str] | None,
) -> dict[str, float]:
    """IDF-weighted query-term coverage per Claim, computed LOCALLY over ``claims``.

    Ports the answer-Claim ranking of ``query._answer_ranked`` into ego-graph
    assembly. The bare topic entity a question is *about* covers only the common
    term(s); the Claim that ANSWERS the question uniquely covers the discriminative
    (rare) term too. IDF over the local bridging-Claim pool down-weights the common
    topic term and up-weights the rare answer term, so making this the PRIMARY sort
    key (ahead of ``-confidence``) keeps the discriminative Claim through the
    ``degree_cap`` cut instead of the highest-confidence generic sibling.

    Local (per-seed) IDF is store-agnostic and identical under both stores, mirroring
    ``_answer_ranked``. Returns an empty dict when ``answer_terms`` is falsy (a
    non-interrogative query) → every coverage lookup is ``0.0`` and both the cut and
    the render order stay byte-identical to pre-patch.
    """
    if not answer_terms or not claims:
        return {}
    toks = {n.id: set(re.findall(r"[a-z0-9]+", f"{n.title} {n.content}".lower())) for n in claims}
    n_claims = len(claims)
    df = {t: sum(1 for n in claims if t in toks[n.id]) for t in answer_terms}
    idf = {t: math.log(1 + (n_claims - df[t] + 0.5) / (df[t] + 0.5)) for t in answer_terms}
    return {n.id: sum(idf[t] for t in answer_terms if t in toks[n.id]) for n in claims}


def _ranked_bridge_claims(
    store: "GraphStore",
    seed_id: str,
    *,
    degree_cap: int,
    min_claim_confidence: float = 0.0,
    relationship_types: frozenset[str] | None = None,
    query_embedding: list[float] | None = None,
    answer_terms: frozenset[str] | None = None,
) -> list[tuple["Node", float]]:
    """The bridging Claims for an entity seed, capped + relevance-ranked DURING
    collection (the hub-explosion backstop). An entity seed is the ``dst`` of its
    bridges, so the Claim is ``edge.src``; we fetch each Claim once, rank, and
    truncate to ``degree_cap``. Returns ``(claim, answer_score)`` pairs so the
    coverage score rides through to the render-side truncation.

    Collection work is itself bounded by ``degree_cap`` (not just the returned
    output): the edge scan feeding ``claim_ids`` stops after ``scan_cap``
    (``_BRIDGE_CLAIM_SCAN_CAP_FACTOR * degree_cap``, floored at
    ``_BRIDGE_CLAIM_SCAN_CAP_FLOOR``) candidates instead of exhausting the
    seed's true degree, so a hub entity with thousands of bridging Claims pays for
    ``get_node``/gate/score work proportional to ``degree_cap``, not to true
    degree (previously every bridging Claim was fetched, gated, and scored
    before the final ``degree_cap`` slice). The over-scan factor keeps normal
    (non-hub) seeds byte-identical to pre-patch ranking — only seeds whose true
    degree exceeds the scan cap see a truncated candidate pool.

    Answer-aware cut (this patch): the PRIMARY sort key is IDF-weighted query-term
    coverage (``_answer_coverage_scores``), so the Claim that covers the
    discriminative answer term survives the ``degree_cap`` cut rather than being
    out-ranked and dropped by a higher-confidence generic sibling. GN-5's
    ``query_embedding`` cosine and confidence remain as secondary tiebreaks. When
    ``answer_terms`` is None (non-interrogative query) every coverage score is 0.0 →
    the key reduces to the prior ``(-confidence, -cosine, id)`` behaviour exactly."""
    scan_cap = max(degree_cap * _BRIDGE_CLAIM_SCAN_CAP_FACTOR, _BRIDGE_CLAIM_SCAN_CAP_FLOOR)
    claim_ids: set[str] = set()
    for edge in itertools.islice(store.list_edges(dst=seed_id), scan_cap):
        if edge.type in ("rdf:subject", "rdf:object"):
            claim_ids.add(edge.src)
    claims: list["Node"] = []
    for cid in claim_ids:
        if cid == seed_id:
            continue
        node = store.get_node(cid)
        if node is None or node.type != "Claim":
            continue
        if not _passes_gates(node):
            continue
        predicate = str(node.facets.get("P") or "")
        if not _predicate_allowed(predicate, relationship_types):
            continue
        if _claim_confidence(node) < min_claim_confidence:
            continue
        claims.append(node)
    coverage = _answer_coverage_scores(claims, answer_terms)
    # Answer-aware cap: coverage FIRST (keeps the discriminative Claim through the
    # degree_cap cut), then GN-5 confidence + query-cosine as deterministic tiebreaks.
    if query_embedding:
        claims.sort(
            key=lambda n: (
                -coverage.get(n.id, 0.0),
                -_claim_confidence(n),
                -_cosine_sim(query_embedding, list(n.embedding) if n.embedding else None),
                n.id,
            )
        )
    else:
        # Backward-compat: no query embedding → id asc tiebreak (original behavior).
        claims.sort(key=lambda n: (-coverage.get(n.id, 0.0), -_claim_confidence(n), n.id))
    return [(n, coverage.get(n.id, 0.0)) for n in claims[: max(0, degree_cap)]]


def _resolve_object_node(store: "GraphStore", claim: "Node") -> "Node | None":
    """The object *entity* of a topology Claim (``O_id``), or ``None``. Read via the
    Claim's ``rdf:object`` bridge (``Claim -[rdf:object]-> objectEntity``), falling
    back to the ``O_id`` facet so a missing edge still resolves."""
    object_id = ""
    for edge in store.list_edges(src=claim.id):
        if edge.type == "rdf:object":
            object_id = edge.dst
            break
    if not object_id:
        object_id = str(claim.facets.get("O_id") or "")
    if not object_id:
        return None
    return store.get_node(object_id)


def build_ego_graph(
    seed_ids: list[tuple[str, float]],
    store: "GraphStore",
    *,
    degree_cap: int,
    neighbour_budget_tokens: int,
    hops: int = 1,
    min_claim_confidence: float = 0.0,
    relationship_types: tuple[str, ...] | None = None,
    query_embedding: list[float] | None = None,
    answer_terms: frozenset[str] | None = None,
) -> EgoGraph:
    """Assemble the relevance-capped ego-graph around ``seed_ids``.

    ``seed_ids`` are ``(node_id, score)`` pairs straight from ``search_claims``
    (NOT ``QueryHit``), so every node — seeds included — is re-fetched via
    ``store.get_node`` to read raw ``.facets`` (``S_id``/``P``/``O_id``/
    ``O_literal``/``confidence``/``block_id``/``source_span``).

    For each seed we collect its 1-hop graph neighbours through the bridge edges,
    applying the MANDATORY degree cap + confidence rank DURING collection so a hub
    Document with 100+ ``schema:mentions`` can never bloom the context. A topology
    Claim (``O_id``) only survives if BOTH endpoints clear the quality gates — a
    relationship row that can't render its object is dropped whole. Every kept
    triple is deduped on ``(S_id, P, O_id|O_literal)``.

    ``hops`` is the caller-supplied ego-graph depth (the ``llm.ask.hops`` knob,
    defaulted to 2 at the companion layer). Depth-1 is the seed neighbourhood;
    depths 2..N walk the frontier of entity nodes surfaced by the previous level so
    a cross-entity chain the seed never touches directly is pulled in — bounded per
    level (§3) so reach can never bloom the render. Independently of ``hops``, a
    Claim seed also gets a hop-1 *sibling-entity traversal*: its subject entity is
    promoted to a full entity expansion so every cross-block sibling asserted about
    that entity enters the pool, not just the seed's block-colocated ones.
    """
    ego = EgoGraph()
    seen_triples: set[tuple[str, str, str]] = set()
    allowed_relationships = _normalize_relationship_types(relationship_types)

    # 1) Seeds first (NODES is the index; never trimmed). Fetch raw facets.
    seeds: list[tuple["Node", float]] = []
    for node_id, score in seed_ids:
        node = store.get_node(node_id)
        if node is None or not _passes_gates(node):
            continue
        # A Claim seed is a routing anchor, not a NODES row: search_claims is a
        # unified retriever so a large fraction of top-k seeds are Claims, and an
        # untrimmed ``N{i} [Claim] <claim text>`` row would bloat the (never-trimmed)
        # NODES index. _bridge_claim_seed reads seed.facets directly, so routing is
        # unaffected. Entity/Document seeds remain the visible index.
        if node.type != "Claim" and node.id not in ego.nodes:
            ego.nodes[node.id] = _node_record(node, is_seed=True, score=float(score))
        seeds.append((node, float(score)))

    # 2) Hop 1 — bridge each seed through its edges, capped + ranked per seed.
    seed_id_set = {seed.id for seed, _ in seeds}
    expanded: set[str] = set(seed_id_set)  # entity ids already bridged
    claim_seed_subjects: list[str] = []
    for seed, _score in seeds:
        if seed.type == "Claim":
            _bridge_claim_seed(
                store,
                seed,
                ego,
                seen_triples,
                min_claim_confidence=min_claim_confidence,
                relationship_types=allowed_relationships,
                query_embedding=query_embedding,
                answer_terms=answer_terms,
            )
            subject_id = str(seed.facets.get("S_id") or "")
            if subject_id:
                claim_seed_subjects.append(subject_id)
        else:
            _bridge_entity_seed(
                store,
                seed,
                ego,
                seen_triples,
                degree_cap=degree_cap,
                min_claim_confidence=min_claim_confidence,
                relationship_types=allowed_relationships,
                query_embedding=query_embedding,
                answer_terms=answer_terms,
            )

    # 2b) Hop-1 SIBLING-ENTITY traversal. A Claim seed's ``_bridge_claim_seed`` only
    # reaches siblings co-located in the seed's own Block (GN-7). Promote its SUBJECT
    # entity to a first-class entity expansion so cross-block siblings that share the
    # subject entity — the actual "sibling" shape — enter the pool at hop 1, without
    # waiting for the multi-hop loop (so the reach is deterministic at hops=1 too).
    # Bounded by the same per-seed ``degree_cap`` as any entity expansion, and marked
    # ``expanded`` so the multi-hop loop below never re-walks it.
    for subject_id in claim_seed_subjects:
        if subject_id in expanded:
            continue
        expanded.add(subject_id)
        subject_node = store.get_node(subject_id)
        if subject_node is None or subject_node.type == "Claim" or not _passes_gates(subject_node):
            continue
        _bridge_entity_seed(
            store,
            subject_node,
            ego,
            seen_triples,
            degree_cap=degree_cap,
            min_claim_confidence=min_claim_confidence,
            relationship_types=allowed_relationships,
            query_embedding=query_embedding,
            answer_terms=answer_terms,
        )

    # 3) Hops 2..N — parametrized multi-hop. Each level bridges the entity nodes
    # surfaced by the previous level (the new frontier), so a chain the seed never
    # reaches directly (multihop/aggregation) is pulled in. Hub explosion is bounded
    # per level: a per-node cap, a cap on how many frontier nodes expand (degree_cap),
    # and render-side budget truncation. ``hops`` is the caller-supplied depth
    # (llm.ask.hops knob). The per-level cap floors at ``max(degree_cap // 2, 4)``
    # rather than tightening as ``degree_cap // level`` — depth-2 hub expansion is
    # exactly where a genuine cross-entity chain lands, so starving it defeats the
    # reason to walk past hop-1. The render budget (not this cap) is the hard bound on
    # output size, so a slightly wider per-level pool cannot grow the rendered context.
    for level in range(2, max(1, hops) + 1):
        level_cap = max(degree_cap // 2, 4)
        frontier = [nid for nid in list(ego.nodes) if nid not in expanded][: max(1, degree_cap)]
        if not frontier:
            break
        for nid in frontier:
            expanded.add(nid)
            node = store.get_node(nid)
            if node is None or node.type == "Claim" or not _passes_gates(node):
                continue
            _bridge_entity_seed(
                store,
                node,
                ego,
                seen_triples,
                degree_cap=level_cap,
                min_claim_confidence=min_claim_confidence,
                relationship_types=allowed_relationships,
                query_embedding=query_embedding,
                answer_terms=answer_terms,
            )

    # ``neighbour_budget_tokens`` is the assembly budget; the renderer owns the
    # actual char→token truncation (seeds-first). It is carried here so a future
    # collection-time budget split across seeds can use it without a signature
    # change — today truncation is render-side only.
    _ = neighbour_budget_tokens  # render-side budget, applied in the renderer.
    return ego


def _record_topology_claim(
    claim: "Node",
    subject_node: "Node",
    object_node: "Node",
    ego: EgoGraph,
    seen_triples: set[tuple[str, str, str]],
    *,
    relationship_types: frozenset[str] | None,
    answer_score: float = 0.0,
) -> None:
    """Add a ``RELATIONSHIPS`` row + BOTH endpoint nodes, deduped on the triple.

    BOTH endpoints must be registered in ``ego.nodes`` or the renderer drops the
    row (it resolves ``N{src} -> N{dst}`` against the node index). When the seed is
    the *object* of a relationship, the subject is a neighbour that would otherwise
    never be registered — so "who founded Acme?" (seed=Acme) must still surface
    Alice. The ``if id not in ego.nodes`` guard preserves the seed flag for an
    endpoint that is itself a seed."""
    predicate = str(claim.facets.get("P") or "")
    if (
        not predicate
        or _is_metadata_predicate(predicate)
        or not _predicate_allowed(predicate, relationship_types)
    ):
        return
    triple = (subject_node.id, predicate, object_node.id)
    if triple in seen_triples:
        return
    seen_triples.add(triple)
    if subject_node.id not in ego.nodes:
        ego.nodes[subject_node.id] = _node_record(subject_node, is_seed=False, score=0.0)
    if object_node.id not in ego.nodes:
        ego.nodes[object_node.id] = _node_record(object_node, is_seed=False, score=0.0)
    ego.relationships.append(
        RelRecord(
            claim_id=claim.id,
            subject_id=subject_node.id,
            predicate=predicate,
            object_id=object_node.id,
            confidence=_claim_confidence(claim),
            block_id=str(claim.facets.get("block_id") or ""),
            answer_score=answer_score,
        )
    )


def _record_propositional_claim(
    claim: "Node",
    subject_id: str,
    subject_name: str,
    literal: str,
    ego: EgoGraph,
    seen_triples: set[tuple[str, str, str]],
    *,
    relationship_types: frozenset[str] | None,
    answer_score: float = 0.0,
) -> None:
    """Add a verbalized ``CLAIMS`` row, deduped on the triple."""
    predicate = str(claim.facets.get("P") or "")
    if (
        not predicate
        or _is_metadata_predicate(predicate)
        or not _predicate_allowed(predicate, relationship_types)
    ):
        return
    triple = (subject_id, predicate, literal)
    if triple in seen_triples:
        return
    seen_triples.add(triple)
    ego.claims.append(
        ClaimRecord(
            claim_id=claim.id,
            subject_id=subject_id,
            subject_name=subject_name,
            predicate=predicate,
            literal=literal,
            confidence=_claim_confidence(claim),
            block_id=str(claim.facets.get("block_id") or ""),
            answer_score=answer_score,
        )
    )


def _route_claim(
    store: "GraphStore",
    claim: "Node",
    subject_id: str,
    subject_name: str,
    ego: EgoGraph,
    seen_triples: set[tuple[str, str, str]],
    *,
    min_claim_confidence: float,
    relationship_types: frozenset[str] | None,
    answer_score: float = 0.0,
) -> None:
    """Route a Claim to RELATIONSHIPS (``O_id``) or CLAIMS (``O_literal``) — the
    load-bearing XOR. Missing the branch silences half the knowledge."""
    if _claim_confidence(claim) < min_claim_confidence:
        return
    o_literal = claim.facets.get("O_literal")
    o_id = claim.facets.get("O_id")
    if o_id:
        # Both endpoints must clear the quality/infra gate AND be registered, or
        # the renderer silently drops the row. Resolve the subject symmetrically to
        # the object (the seed may sit on either leg — "who founded Acme?" seeds on
        # the object Acme, yet the answer Alice is the subject neighbour).
        subject_node = store.get_node(subject_id)
        if subject_node is None or not _passes_gates(subject_node):
            return  # drop the whole relationship if the subject can't render
        object_node = _resolve_object_node(store, claim)
        if object_node is None or not _passes_gates(object_node):
            return  # drop the whole relationship if the object can't render
        _record_topology_claim(
            claim,
            subject_node,
            object_node,
            ego,
            seen_triples,
            relationship_types=relationship_types,
            answer_score=answer_score,
        )
    elif o_literal is not None:
        _record_propositional_claim(
            claim,
            subject_id,
            subject_name,
            str(o_literal),
            ego,
            seen_triples,
            relationship_types=relationship_types,
            answer_score=answer_score,
        )


def _bridge_entity_seed(
    store: "GraphStore",
    seed: "Node",
    ego: EgoGraph,
    seen_triples: set[tuple[str, str, str]],
    *,
    degree_cap: int,
    min_claim_confidence: float,
    relationship_types: frozenset[str] | None,
    query_embedding: list[float] | None = None,
    answer_terms: frozenset[str] | None = None,
) -> None:
    """Expand an entity seed: its bridging Claims (capped+ranked), routed by XOR.

    The seed is the subject when the bridge is ``rdf:subject`` and the object when
    it is ``rdf:object``; we use the Claim's own ``S_id`` facet as the subject so
    the rendered row is always ``subject -> object`` regardless of which leg the
    seed sits on."""
    for claim, answer_score in _ranked_bridge_claims(
        store,
        seed.id,
        degree_cap=degree_cap,
        min_claim_confidence=min_claim_confidence,
        relationship_types=relationship_types,
        query_embedding=query_embedding,
        answer_terms=answer_terms,
    ):
        subject_id = str(claim.facets.get("S_id") or seed.id)
        subject_name = _subject_name(store, subject_id, ego)
        _route_claim(
            store,
            claim,
            subject_id,
            subject_name,
            ego,
            seen_triples,
            min_claim_confidence=min_claim_confidence,
            relationship_types=relationship_types,
            answer_score=answer_score,
        )


_BLOCK_COLOCATION_CAP = 3  # GN-7: max sibling Claims added per block_id (secondary pass)


def _bridge_claim_seed(
    store: "GraphStore",
    seed: "Node",
    ego: EgoGraph,
    seen_triples: set[tuple[str, str, str]],
    *,
    min_claim_confidence: float,
    relationship_types: frozenset[str] | None,
    query_embedding: list[float] | None = None,
    answer_terms: frozenset[str] | None = None,
) -> None:
    """Expand a Claim seed directly to its subject/object via its own facets.

    GN-6: register the subject entity as a visible NODES row so the answerer can
    pivot from it and sibling Claims on that entity become reachable.

    GN-7: pull in sibling Claims sharing the seed's ``block_id`` (block-colocation
    expansion) — facts co-extracted from the same Block are topically adjacent by
    construction.  Bounded by ``_BLOCK_COLOCATION_CAP`` to prevent O(n) runaway.

    Answer-aware cut: the seed + its siblings are scored by IDF-weighted query-term
    coverage (over the same pool) so the ``_BLOCK_COLOCATION_CAP`` cut keeps the
    discriminative sibling, and the coverage score rides onto the rendered rows."""
    subject_id = str(seed.facets.get("S_id") or "")
    if not subject_id:
        return

    # GN-6: register the subject entity so its siblings are walkable (was skipped
    # by the ``node.type != "Claim"`` guard in the seed loop).
    if subject_id not in ego.nodes:
        subject_node = store.get_node(subject_id)
        if subject_node is not None and _passes_gates(subject_node):
            ego.nodes[subject_id] = _node_record(subject_node, is_seed=False, score=0.0)

    subject_name = _subject_name(store, subject_id, ego)

    # GN-7: collect sibling Claims sharing the seed's Block BEFORE routing, so the
    # coverage IDF is computed over the full {seed} ∪ siblings pool (consistent with
    # the entity-seed path). We iterate candidates from the subject's bridge edges.
    block_id = str(seed.facets.get("block_id") or "")
    siblings: list["Node"] = []
    if block_id:
        sibling_ids: set[str] = set()
        for edge in store.list_edges(dst=subject_id):
            if edge.type in ("rdf:subject", "rdf:object") and edge.src != seed.id:
                sibling_ids.add(edge.src)
        for sid in sibling_ids:
            node = store.get_node(sid)
            if node is None or node.type != "Claim":
                continue
            if not _passes_gates(node):
                continue
            if str(node.facets.get("block_id") or "") != block_id:
                continue
            if _claim_confidence(node) < min_claim_confidence:
                continue
            siblings.append(node)

    coverage = _answer_coverage_scores([seed, *siblings], answer_terms)

    _route_claim(
        store,
        seed,
        subject_id,
        subject_name,
        ego,
        seen_triples,
        min_claim_confidence=min_claim_confidence,
        relationship_types=relationship_types,
        answer_score=coverage.get(seed.id, 0.0),
    )

    if not siblings:
        return
    # Answer-aware cap: coverage FIRST, then GN-7 confidence + query-cosine tiebreaks.
    if query_embedding:
        siblings.sort(
            key=lambda n: (
                -coverage.get(n.id, 0.0),
                -_claim_confidence(n),
                -_cosine_sim(query_embedding, list(n.embedding) if n.embedding else None),
                n.id,
            )
        )
    else:
        siblings.sort(key=lambda n: (-coverage.get(n.id, 0.0), -_claim_confidence(n), n.id))
    for sibling in siblings[:_BLOCK_COLOCATION_CAP]:
        sib_subject_id = str(sibling.facets.get("S_id") or subject_id)
        sib_subject_name = _subject_name(store, sib_subject_id, ego)
        _route_claim(
            store,
            sibling,
            sib_subject_id,
            sib_subject_name,
            ego,
            seen_triples,
            min_claim_confidence=min_claim_confidence,
            relationship_types=relationship_types,
            answer_score=coverage.get(sibling.id, 0.0),
        )


def _subject_name(store: "GraphStore", subject_id: str, ego: EgoGraph) -> str:
    """Best-effort display name for a subject id (already-seen, then store)."""
    rec = ego.nodes.get(subject_id)
    if rec is not None:
        return rec.name
    node = store.get_node(subject_id)
    if node is not None:
        return str(node.title or node.id)
    return subject_id


# ── renderer (pure) ─────────────────────────────────────────────────────────--
def _est_tokens(text: str, token_factor: float) -> int:
    """char/4 × calibration factor — the reference-host token estimate."""
    return int((len(text) / 4) * token_factor)


def render_ego_subgraph(
    ego: EgoGraph,
    *,
    max_token_budget: int,
    token_factor: float = 1.117,
    max_nodes: int | None = None,
    max_relationships: int | None = None,
    max_claims: int | None = None,
) -> str:
    """Render an ``EgoGraph`` to the typed ``=== NODES / RELATIONSHIPS / CLAIMS ===``
    block. PURE: no store, no config, no LLM.

    NODES (seeds + neighbours, the index) is emitted first and never trimmed. Over
    budget, the lowest-confidence neighbour rows (RELATIONSHIPS then CLAIMS) are
    dropped first. Anchor ids (``claim:``/``block:``/``span:``) ride every row for
    Tier-2 fetch without re-parsing.
    """
    short = _short_id
    lines: list[str] = ["=== NODES ==="]
    node_items = list(ego.nodes.items())
    if max_nodes is not None:
        node_items = node_items[:max_nodes]
    index = {nid: i for i, (nid, _rec) in enumerate(node_items)}
    for nid, rec in node_items:
        kind = "seed" if rec.is_seed else "neigh"
        anchor = []
        if rec.block_id:
            anchor.append(f"block:{short(rec.block_id)}")
        if rec.span:
            anchor.append(f"span:{rec.span}")
        suffix = f"  ({kind}{' ' + ' '.join(anchor) if anchor else ''})"
        lines.append(f"N{index[nid]} [{rec.type}] {rec.name}{suffix}")

    # Relationship + claim rows, ordered so the over-budget tail truncation drops
    # the least-relevant rows first. Answer-coverage leads (a discriminative row kept
    # through the degree_cap cut must survive render truncation too), then confidence,
    # then id. answer_score is 0.0 for non-interrogative queries → the key reduces to
    # the prior ``(-confidence, id)`` order, byte-identical to pre-patch.
    rels = sorted(ego.relationships, key=lambda r: (-r.answer_score, -r.confidence, r.claim_id))
    claims = sorted(ego.claims, key=lambda c: (-c.answer_score, -c.confidence, c.claim_id))

    rel_lines: list[str] = []
    rel_count = 0
    for rel in rels:
        si = index.get(rel.subject_id)
        oi = index.get(rel.object_id)
        if si is None or oi is None:
            continue
        if max_relationships is not None and rel_count >= max_relationships:
            break
        pred = _verbalize_predicate(rel.predicate)
        anchor = f"claim:{short(rel.claim_id)} conf={rel.confidence:.2f}"
        if rel.block_id:
            anchor += f"  block:{short(rel.block_id)}"
        rel_lines.append(f"N{si} -[{pred}]-> N{oi}  ({anchor})")
        rel_count += 1

    claim_lines: list[str] = []
    claim_count = 0
    for c in claims:
        if max_claims is not None and claim_count >= max_claims:
            break
        pred = _verbalize_predicate(c.predicate)
        claim_lines.append(f"- {c.subject_name} {pred} {c.literal}. [{short(c.claim_id)}]")
        claim_count += 1

    # Seeds-first budget: NODES is fixed; spend the remainder on rel rows then
    # claim rows (both already ordered high→low confidence).
    nodes_block = "\n".join(lines)
    budget_left = max_token_budget - _est_tokens(nodes_block, token_factor)
    kept_rels: list[str] = []
    kept_claims: list[str] = []
    for line in rel_lines:
        cost = _est_tokens(line + "\n", token_factor)
        if budget_left - cost < 0:
            break
        budget_left -= cost
        kept_rels.append(line)
    for line in claim_lines:
        cost = _est_tokens(line + "\n", token_factor)
        if budget_left - cost < 0:
            break
        budget_left -= cost
        kept_claims.append(line)

    out = [nodes_block]
    if kept_rels:
        out.append("\n=== RELATIONSHIPS ===")
        out.extend(kept_rels)
    if kept_claims:
        out.append("\n=== CLAIMS (verbalized) ===")
        out.extend(kept_claims)
    return "\n".join(out)


def _short_id(value: str) -> str:
    """First 8 hex chars of an id for compact anchors (full id still in citations)."""
    return value[:8] if value else value


def _normalize_relationship_types(types: tuple[str, ...] | None) -> frozenset[str] | None:
    if not types:
        return None
    normalized = {item.strip().lower().replace(" ", "_") for item in types if item and item.strip()}
    return frozenset(normalized) if normalized else None


def _predicate_allowed(predicate: str, allowed: frozenset[str] | None) -> bool:
    if allowed is None:
        return True
    raw = predicate.strip().lower()
    verbalized = _verbalize_predicate(predicate).replace(" ", "_")
    return raw in allowed or verbalized in allowed
