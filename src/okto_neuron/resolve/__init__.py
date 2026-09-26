"""Resolve — the talk-back the graph gives a candidate.

Phase D of ``docs/autonomous-architecture-plan.md``. Given a
:class:`~okto_neuron.consolidate.NodeCandidate` and a
:class:`~okto_neuron.store.protocol.GraphStore`, the resolver assembles the
correlations the existing graph "talks back" with — what the candidate is
*similar* to and what it *contradicts* — plus a suggested confidence Phase E's
gate can act on.

The service is read-only: it inspects the store, it never mutates it.

Public surface::

    resolve(candidate, store, *, embedder=None, ...) -> ResolveOutcome
    ResolveOutcome(correlations, confidence)
    find_similar / find_contradictions  (the two probes)

Conventions this layer relies on (process objects, not graph primitives):

- A ``Claim`` candidate carries its assertion in ``facets`` under
  ``subject`` / ``predicate`` / ``object``. A contradiction is an existing
  ``Claim`` node with the same subject+predicate and a differing object.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import Callable, Protocol

from okto_neuron._internal.infra import is_infra
from okto_neuron.companion import Correlation
from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.consolidate._dedup import EdgeEndpointGuard
from okto_neuron.core.schema import Node
from okto_neuron.embed import EmbeddingProvider, get_provider
from okto_neuron.ingest import EXTRACTION_ACTIVITY_ID, SYSTEM_AGENT_ID
from okto_neuron.llm import ResponseFormat
from okto_neuron.semantic_surface import (
    discovery_surface_key,
    exact_surface_key,
    merge_surface_records,
)
from okto_neuron.store.protocol import GraphStore

_LOG = logging.getLogger("okto_neuron.resolve")

_TITLE_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Infrastructure nodes seeded by every ingest (the deterministic-extractor
# provenance Agent + Activity). They are metadata, not entities, and must be
# excluded from entity similarity — otherwise extracted Agents/Activities match
# the "system"/LLM provenance nodes on embedding noise and get gated to review.
# Kept for back-compat; the live exclusion now uses is_infra (covers LLM infra
# nodes too, via their durable facets["infra"] marker).
_INFRA_NODE_IDS = frozenset({SYSTEM_AGENT_ID, EXTRACTION_ACTIVITY_ID})

# ── tunables ──────────────────────────────────────────────────────────────────
SIMILAR_THRESHOLD = 0.82
"""Minimum cosine similarity for a node to count as a ``similar`` correlation.

Calibrated for bge-small (the default embedder), whose baseline cosine between
unrelated short texts is high (~0.4–0.65) — at 0.5 pure embedding noise read as
"similar" and gated genuinely-novel entities to review. Real near-duplicates
score ~0.85+, so 0.82 admits true duplicates while rejecting noise. The earlier
0.5 made commit-vs-queue effectively random around the noise floor."""

# Confidence shaping: a novel candidate (no overlap) sits at the base; each
# relation moves it. Contradiction is the strongest downward pull.
_BASE_CONFIDENCE = 0.7
_NOVEL_BONUS = 0.2
_CONTRADICTION_PENALTY = 0.5
_AMBIGUITY_PENALTY = 0.1


# ── value object ────────────────────────────────────────────────────────────--
@dataclass(frozen=True)
class ResolveOutcome:
    """What the graph said back about a candidate, plus a gate-ready score."""

    correlations: tuple[Correlation, ...] = ()
    confidence: float = _BASE_CONFIDENCE

    @property
    def contradicted(self) -> bool:
        return any(c.kind == "contradicts" for c in self.correlations)


# ── helpers ───────────────────────────────────────────────────────────────────
def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _cosine(a: tuple[float, ...] | list[float], b: tuple[float, ...] | list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na**0.5) * (nb**0.5))


def _candidate_vector(candidate: NodeCandidate, embedder: EmbeddingProvider) -> tuple[float, ...]:
    if candidate.embedding is not None:
        return tuple(candidate.embedding)
    return tuple(embedder.embed(f"{candidate.title}\n{candidate.content}".strip()))


def _node_vector(node: Node, embedder: EmbeddingProvider) -> tuple[float, ...]:
    if node.embedding is not None:
        return tuple(node.embedding)
    return tuple(embedder.embed(f"{node.title}\n{node.content}".strip()))


def _claim_spo(facets: dict) -> tuple[str, str, str] | None:
    subject = facets.get("subject") or facets.get("S")
    predicate = facets.get("predicate") or facets.get("P")
    obj = facets.get("object") or facets.get("O")
    if subject is None or predicate is None or obj is None:
        return None
    return (
        exact_surface_key(subject),
        exact_surface_key(predicate),
        exact_surface_key(obj),
    )


# ── the two probes ──────────────────────────────────────────────────────────
def find_similar(
    candidate: NodeCandidate,
    store: GraphStore,
    *,
    embedder: EmbeddingProvider | None = None,
    threshold: float = SIMILAR_THRESHOLD,
    k: int = 5,
) -> tuple[Correlation, ...]:
    """Top existing nodes of the SAME type whose embedding is close to the
    candidate's.

    Scoped to the candidate's own type on purpose: an extracted entity (e.g. an
    ``Agent``) is textually similar to the raw ``Block``/``Document`` it was
    derived from, but that is provenance, not ambiguity. Comparing only same-type
    entity nodes means a genuinely new entity reads as novel (and auto-commits),
    while a re-mention of an existing entity reads as a duplicate to reconcile.
    """
    embedder = embedder or get_provider()
    cand_vec = _candidate_vector(candidate, embedder)
    scored: list[tuple[float, Node]] = []
    for node in store.list_nodes(type=candidate.type):
        if node.id == candidate.candidate_id or is_infra(node):
            continue
        score = _cosine(cand_vec, _node_vector(node, embedder))
        if score >= threshold:
            scored.append((score, node))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return tuple(
        Correlation(
            kind="similar",
            target_id=node.id,
            score=_clamp(score),
            summary=f"similar to {node.type} {node.title!r}",
        )
        for score, node in scored[:k]
    )


def find_contradictions(
    candidate: NodeCandidate,
    store: GraphStore,
) -> tuple[Correlation, ...]:
    """Existing Claims that assert the same subject+predicate with a differing
    object. Only meaningful for ``Claim`` candidates carrying S-P-O facets."""
    if candidate.type != "Claim":
        return ()
    cand_spo = _claim_spo(candidate.facets)
    if cand_spo is None:
        return ()
    subject, predicate, obj = cand_spo
    out: list[Correlation] = []
    for node in store.list_nodes(type="Claim"):
        existing = _claim_spo(node.facets)
        if existing is None:
            continue
        e_subject, e_predicate, e_obj = existing
        if e_subject == subject and e_predicate == predicate and e_obj != obj:
            out.append(
                Correlation(
                    kind="contradicts",
                    target_id=node.id,
                    score=1.0,
                    summary=(f"asserts {subject!r} {predicate!r} {obj!r}; existing says {e_obj!r}"),
                )
            )
    return tuple(out)


# ── assembly ──────────────────────────────────────────────────────────────────
def resolve(
    candidate: NodeCandidate,
    store: GraphStore,
    *,
    embedder: EmbeddingProvider | None = None,
    similar_threshold: float = SIMILAR_THRESHOLD,
) -> ResolveOutcome:
    """Assemble the full talk-back and derive a gate-ready confidence.

    Read-only: probes the store, mutates nothing. Confidence is a single
    monotonic formula — a novel candidate (nothing related) scores highest, a
    contradicted one lowest, an ambiguous one (similar but not identical) sits
    in between.
    """
    embedder = embedder or get_provider()
    similar = find_similar(candidate, store, embedder=embedder, threshold=similar_threshold)
    contradictions = find_contradictions(candidate, store)
    correlations = (*similar, *contradictions)

    confidence = _BASE_CONFIDENCE
    if contradictions:
        confidence -= _CONTRADICTION_PENALTY
    elif similar:
        confidence -= _AMBIGUITY_PENALTY
    else:
        confidence += _NOVEL_BONUS
    return ResolveOutcome(correlations=correlations, confidence=_clamp(confidence))


# ── Tier 0: deterministic reconcile against the committed store ────────────────
def _normalized_title(title: str) -> str:
    """Shared conservative exact key used by Tier-0 identity decisions."""
    return exact_surface_key(title)


_ALIAS_SUFFIX_TOKENS = frozenset(
    {
        "books",
        "co",
        "company",
        "corp",
        "corporation",
        "inc",
        "incorporated",
        "limited",
        "llc",
        "ltd",
        "plc",
        "press",
        "publisher",
        "publishers",
        "publishing",
    }
)


def _alias_base(title: str) -> tuple[tuple[str, ...], bool]:
    # Alias recall is discovery-only; a match still requires the merge judge.
    tokens = tuple(_TITLE_TOKEN_RE.findall(discovery_surface_key(title)))
    end = len(tokens)
    while end > 1 and tokens[end - 1] in _ALIAS_SUFFIX_TOKENS:
        end -= 1
    return tokens[:end], end < len(tokens)


def _structural_alias_score(left: str, right: str) -> float | None:
    """Score (or reject) a short-name/full-name or lexical-variant pair for the
    merge judge, by reusing the SAME tested string lanes
    ``reconcile.candidates.generate_candidate_clusters`` already uses to recall
    already-committed look-alikes: a strong Jaro-Winkler score (length-gated so
    a coincidental short-token near-spelling doesn't weld), a shared surname, or
    an ordered/unordered token-subset containment ("Karine" ⊂ "Karine
    Buchner", "Casey Lee" ⊂ "Dr Casey Lee"). ``None`` means no lane fired; a
    float means one did. None of these decide a merge — each only proposes a
    pair for the judge, exactly as it does in ``reconcile``.

    The RETURNED SCORE mirrors ``reconcile.candidates``' own pair-score
    convention (see ``IDENTITY_EDGE_SCORE``'s docstring): the two DETERMINISTIC
    token-containment lanes (shared surname, ordered/unordered token-subset)
    score ``IDENTITY_EDGE_SCORE`` (1.0) since token identity is stronger
    evidence than any edit-distance estimate; the Jaro-Winkler fuzzy lane scores
    at its own value, which is always < 1.0 here (an exact match already
    returned earlier, in ``_is_lexical_alias_pair``) and therefore never
    outranks an identity-grade match. Callers that only need the yes/no
    ``_is_structural_alias_pair`` treat "scored" and "None" as True/False.

    Deliberately NOT reinventing a length cap on the long side (an earlier
    version of this function capped at 4 tokens): a cap is a blocking-time
    veto on precision, and blocking must never veto a candidate pair — see
    ``_is_lexical_alias_pair``'s docstring. "NX" / "NX Lab" and a bare word
    against an unrelated longer phrase are both admitted by this shape; the
    judge is conservative and is what rules them DISTINCT.

    Imports ``reconcile.candidates`` locally, not at module level: the
    ``okto_neuron.reconcile`` package's ``__init__`` imports ``reconcile.apply``,
    which imports ``MergeJudge`` from THIS module, so a top-level import here
    would complete a circular import while this module is still initializing.
    """
    from okto_neuron.reconcile.candidates import (
        IDENTITY_EDGE_SCORE,
        JW_FUZZY_MIN_LEN,
        JW_STRONG,
        _surname,
        jaro_winkler,
        token_subset,
        token_subset_unordered,
    )

    # Deterministic lanes are checked FIRST and win the score whenever they
    # fire, even for a pair that would also clear the fuzzy Jaro-Winkler floor
    # below (e.g. "Waverly"/"Waverly Chen" scores 0.92 on Jaro-Winkler alone,
    # comfortably above JW_STRONG) — this is what makes a true short-name/
    # full-name pair rank ahead of a flood of merely-similar spellings once a
    # caller caps how many matches it will judge (see LEXICAL_ALIAS_CAP).
    left_surname = _surname(left)
    if left_surname and left_surname == _surname(right):
        return IDENTITY_EDGE_SCORE
    if token_subset(left, right) or token_subset_unordered(left, right):
        return IDENTITY_EDGE_SCORE
    left_key = discovery_surface_key(left)
    right_key = discovery_surface_key(right)
    jw = jaro_winkler(left, right)
    shorter_len = min(len(left_key), len(right_key))
    if jw >= JW_STRONG and (left_key == right_key or shorter_len >= JW_FUZZY_MIN_LEN):
        return jw
    return None


def _is_structural_alias_pair(left: str, right: str) -> bool:
    """Bool view of ``_structural_alias_score`` — see its docstring for the
    lanes and the scoring convention. Kept as a separate name because most
    callers only need the yes/no recall decision, not the ranking score."""
    return _structural_alias_score(left, right) is not None


def _lexical_alias_score(left: str, right: str) -> float | None:
    """Score (or reject) a pair for the LLM merge judge — DISJUNCTIVE
    (multi-pass) blocking, per standard entity-resolution practice (Christen
    2012 ch.4; Papadakis et al. 2020). ``None`` means no lane fired (blocking
    rejects the pair); any float means at least one lane admitted it, and the
    value is a RANKING signal for a downstream cap, never a merge decision.

    Blocking's job is RECALL: propose every pair that COULD be the same entity.
    Precision is the judge's job alone, downstream. A pair is admitted here if
    ANY lane fires (legal/publisher suffix stripping, or the structural lanes
    in ``_structural_alias_score``); blocking must never veto a pair that one
    lane would have proposed just because another lane also looked at it and
    found nothing. This function itself decides NOTHING about identity — every
    pair it admits still goes to the conservative judge, which is the only
    thing allowed to say "same". The legal/publisher suffix lane is
    DETERMINISTIC token containment, exactly like the structural token-subset
    lanes, so it scores ``IDENTITY_EDGE_SCORE`` (1.0) too — see
    ``_structural_alias_score`` for why that outranks the fuzzy lane.

    Why this exists at all: a SINGLE embedding-cosine key is degenerate for
    short names. In a real vault's candidate ledger, Agent "Saulo" and Agent
    "Saulo Lima" were NEVER compared — "Saulo" was only ever compared to the
    unrelated "Raphael", at cosine 1.0, because short person names embed
    near-identically regardless of who they name. The same split happened for
    three other people. One blocking key covering one notion of similarity
    (embedding distance) cannot recall every kind of true duplicate; lexical
    containment and surname/edit-distance lanes catch the shapes embeddings
    miss, exactly as ``reconcile.candidates`` already does for retroactive
    reconciliation.

    RECALL stays uncapped here on purpose (ADR 0042): this function still
    admits every pair a lane proposes, no matter how many. Bounding the
    resulting JUDGE-CALL volume is the caller's job (see
    ``LEXICAL_ALIAS_CAP``), not this function's — a scoring predicate must
    never itself become a precision veto.
    """

    # Same local-import rationale as ``_structural_alias_score`` (circular
    # import while ``okto_neuron.reconcile`` is still initializing).
    from okto_neuron.reconcile.candidates import IDENTITY_EDGE_SCORE

    left_norm = _normalized_title(left)
    right_norm = _normalized_title(right)
    if not left_norm or not right_norm or left_norm == right_norm:
        return None
    left_base, left_stripped = _alias_base(left)
    right_base, right_stripped = _alias_base(right)
    if left_base and left_base == right_base and (left_stripped or right_stripped):
        return IDENTITY_EDGE_SCORE
    return _structural_alias_score(left, right)


def _is_lexical_alias_pair(left: str, right: str) -> bool:
    """Bool view of ``_lexical_alias_score`` — see its docstring for the lanes,
    the recall contract, and why scoring and capping are kept separate from
    this yes/no predicate."""
    return _lexical_alias_score(left, right) is not None


@dataclass(frozen=True)
class StoreReconciliation:
    """Result of reconciling a batch of candidates against the committed store.

    ``survivors`` are the candidates that are genuinely new (no exact match);
    ``edges`` are the batch's edges with refs remapped from any merged-away
    candidate to the existing store node it merged into; ``merged_into`` maps a
    dropped ``candidate_id`` to the existing store node id it resolved to."""

    survivors: list[NodeCandidate]
    edges: list[EdgeCandidate]
    merged_into: dict[str, str]


def _remap_edges(edges: list[EdgeCandidate], merged_into: dict[str, str]) -> list[EdgeCandidate]:
    """Remap edge endpoints from merged-away candidate ids onto their surviving
    store ids; drop self-loops created by the remap and de-dup identical edges."""

    def remap(ref: str) -> str:
        return merged_into.get(ref, ref)

    remapped: list[EdgeCandidate] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for edge in edges:
        new_src = remap(edge.src_ref)
        new_dst = remap(edge.dst_ref)
        if new_src == new_dst:
            continue
        if new_src == edge.src_ref and new_dst == edge.dst_ref:
            new_edge = edge
        else:
            new_edge = edge.model_copy(update={"src_ref": new_src, "dst_ref": new_dst})
        key = (new_edge.type, new_src, new_dst, new_edge.block_id)
        if key in seen:
            continue
        seen.add(key)
        remapped.append(new_edge)
    return remapped


def reconcile_against_store(
    nodes: list[NodeCandidate],
    edges: list[EdgeCandidate],
    store: GraphStore,
    *,
    merge_blocked: Callable[[str, str], bool] | None = None,
) -> StoreReconciliation:
    """Collapse candidates that EXACTLY match an existing committed node into that
    node, cross-file. This is Tier 0 of entity resolution: no embedding, no
    threshold, no LLM — pure ``(type, normalized-title)`` equality.

    Root cause this fixes: ``resolve`` runs pre-commit and ``collapse_duplicates``
    only dedupes within one ``remember`` call, so the SAME entity mentioned in
    file A and file B commits twice (e.g. one repeated person became 9 nodes). Here,
    a candidate whose ``(type, normalized-title)`` already exists in the store is
    dropped and its edges are remapped onto the existing node id — so the second
    mention's relationship still lands (and still mints its byte-anchored Claim
    against the survivor), but no duplicate entity node is created.

    Exact-string equality is high-precision and deliberately conservative: it
    will NOT merge "NX" into "NX Lab" (different titles → genuinely-distinct
    company vs team stay separate). The first-name→full-name and near-variant
    cases are left to the embedding/LLM-judge tiers. Infra nodes (the extractor
    provenance Agent/Activity) are never match targets.

    Edge-endpoint guard: two batch candidates that are the subject and object of
    one staged edge are distinct by construction, so they must never BOTH collapse
    onto the same existing store node — that would fuse the relationship's two
    ends into a self-loop and silently drop it, even though neither candidate
    directly duplicates the other. Uses the shared ``EdgeEndpointGuard``, keyed
    here by the existing store node id each candidate is trying to merge into,
    via ``blocks_external`` rather than plain ``blocks``: the store id itself is
    never a blocking closure member (a candidate's OWN direct edge to the exact
    node it exact-matches is expected and harmless — the merge remap turns it
    into a self-loop, dropped as always), but a batch SIBLING that already
    merged into that same store id, and is edge-connected to this candidate, IS
    the veto signal — see ``EdgeEndpointGuard.blocks_external``'s docstring.
    """
    # Index existing store nodes by (type, normalized-title), once per type in the
    # batch. First occurrence wins as the canonical survivor id (stable order).
    existing_by_key: dict[tuple[str, str], str] = {}
    for ntype in {c.type for c in nodes}:
        for node in store.list_nodes(type=ntype):
            if is_infra(node):
                continue
            norm = _normalized_title(node.title)
            if norm:
                existing_by_key.setdefault((ntype, norm), node.id)

    guard = EdgeEndpointGuard(edges)
    survivors: list[NodeCandidate] = []
    merged_into: dict[str, str] = {}
    for cand in nodes:
        norm = _normalized_title(cand.title)
        existing_id = existing_by_key.get((cand.type, norm)) if norm else None
        if (
            existing_id is not None
            and existing_id != cand.candidate_id
            and not guard.blocks_external(cand.candidate_id, existing_id)
            and not (merge_blocked is not None and merge_blocked(cand.candidate_id, existing_id))
        ):
            merged_into[cand.candidate_id] = existing_id
            guard.fold(cand.candidate_id, existing_id)
        else:
            survivors.append(cand)

    if not merged_into:
        return StoreReconciliation(survivors, list(edges), {})

    return StoreReconciliation(survivors, _remap_edges(edges, merged_into), merged_into)


# ── Tier 1 + Tier 2: embedding-recall band + LLM same-vs-distinct judge ────────
JUDGE_BAND_THRESHOLD = SIMILAR_THRESHOLD
"""Cosine floor for a candidate to enter the merge-judge band. Reuses the same
0.82 'similar' threshold resolve uses, so anything the resolver would flag as
ambiguous is exactly what the judge adjudicates — no second magic number."""

MERGE_CONFIDENCE = 0.8
"""Minimum self-reported judge confidence to ACT on a 'same' verdict. Below it a
'same' answer is treated as unsure and kept distinct (the candidate still flows
to the normal resolve/gate, which parks an ambiguous node for review). 'Distinct
when unsure' is load-bearing: a false merge collapses two real entities."""

JUDGE_K = 3
"""How many top band-neighbours to offer the judge per candidate; it merges into
the FIRST adjudged 'same'. Bounds LLM calls — the judge only fires when a
same-type neighbour clears the band at all, which is rare for novel content."""

LEXICAL_ALIAS_CAP = 5
"""Maximum lexical-alias matches offered to the judge per candidate in
``judge_against_store``, ranked strongest-first — mirrors
``reconcile.candidates.RECONCILE_CLASS_CAP`` and its ``_split_oversize``
"pack strongest edges first" idea, applied here to a single candidate's match
list instead of a cluster.

Root cause this bounds: ADR 0042 widened ``_is_lexical_alias_pair`` from a
narrow legal/publisher-suffix rule into a disjunctive set of recall lanes
(correctly — blocking must optimize recall, not precision). But the lane feeds
an UNCAPPED ``store.list_nodes()`` scan: every additional same-type node the
wider rule matches costs one more LLM judge call, so the call count grows with
store size, not candidate count. Measured on a live ingest: judge calls for the
SAME first 3 documents went from 25 (before the widening) to 118 (after), a
4.7x increase, one document sitting in ``committing`` for 17+ minutes.

The fix is a budget, not a veto: recall stays uncapped in
``_lexical_alias_score`` (every pair a lane proposes is still admitted and
scored — see its docstring), and only the ALREADY-RECALLED matches are ranked
and truncated here, downstream of recall. Each match is scored by
``_lexical_alias_score`` (``IDENTITY_EDGE_SCORE`` for a deterministic
containment/surname/suffix match, the raw Jaro-Winkler value for the fuzzy
lane — see that function and ``_structural_alias_score`` for why identity-grade
matches always outrank fuzzy ones), sorted descending with a deterministic
tie-break (node id, ascending) so ingest stays reproducible, and only the top
``LEXICAL_ALIAS_CAP`` are kept — so a strong short-name/full-name pair is never
crowded out by a flood of weaker matches sharing one common token.

Set to 5, matching order-of-magnitude with ``RECONCILE_CLASS_CAP`` (8): combined
with the embedding lane's own ``JUDGE_K`` (3), a candidate's total judge-call
budget in ``judge_against_store`` is ``JUDGE_K + LEXICAL_ALIAS_CAP`` = 8 per
candidate, independent of store size — the same budget-per-unit the retroactive
reconcile path already settled on for a cluster."""


@dataclass(frozen=True)
class MergeVerdict:
    """The judge's call on whether a candidate and an existing node are the same
    real-world entity. 'same' is acted on only when 'confidence' clears
    MERGE_CONFIDENCE."""

    same: bool
    confidence: float = 0.0
    reason: str = ""
    # ADR 0039 D5: each transient provider failure the judge call retried
    # (``okto_neuron.llm.complete_with_retry``), for the per-pair ledger row.
    provider_retries: tuple[dict[str, object], ...] = ()


class MergeJudge(Protocol):
    """Adjudicates whether a candidate duplicates an existing store node.

    ``candidate_context``/``existing_context`` carry optional per-entity identity
    signal (currently each entity's relationships) the judge may weigh as
    SUPPORTING evidence. Both default to ``""``; with the defaults a judge must
    behave exactly as it did before context existed."""

    def judge(
        self,
        candidate: NodeCandidate,
        existing: Node,
        *,
        candidate_context: str = "",
        existing_context: str = "",
    ) -> MergeVerdict: ...


_VERDICT_SYSTEM = (
    "You decide whether two knowledge-graph entities refer to the SAME "
    "real-world thing or are DISTINCT.\n"
    'Be conservative: when in any doubt, answer "same": false. Entities that '
    "merely share a topic, a type, or a word in their name are DISTINCT. Only "
    'answer "same": true when they are unmistakably the same specific entity.\n'
    'SAME examples: an initials expansion ("CM Rivera" / "Casey Morgan '
    'Rivera"); "NYC" / "New York City"; a bare single-token name against a '
    'name that STARTS WITH that exact token ("Aurelin" / "Aurelin Voss") — '
    "this is SAME unless a competing full name with a DIFFERENT surname is "
    'present (see the "Other names in scope" line below); with no such '
    'competitor listed, treat "no conflicting surname" as a CHECKED, '
    "SATISFIED condition, not an assumption.\n"
    'DISTINCT examples: a company and a team inside it ("NX" / "NX Lab"); a '
    'team or org-unit versus a person bearing a related name ("NX Lab" team / '
    '"Morgan Lee" person); a person and their employer; a general concept and '
    "a specific instance; two DIFFERENT FULL names that merely share a "
    'leading token ("Aurelin Voss" / "Aurelin Kade") stay DISTINCT — unlike '
    "the bare-token case above, both sides here already carry their own "
    "surname, so there is nothing left to check.\n"
    'When an "Other names in scope" line is given for a pair: it lists every '
    "OTHER entity sharing the leading token, so check each listed name's own "
    "surname against the fuller name in the pair YOU are judging — a listed "
    "name with a DIFFERENT surname is a genuine competitor and makes the "
    "bare-token pairing ambiguous -> DISTINCT; a listed name that is itself a "
    "spelling variant of the SAME fuller name (not a different person) is not "
    "a competitor and does not by itself make the pairing ambiguous. A line "
    "stating none were found is direct, checked confirmation that the "
    "bare-token pairing has no conflicting surname, supporting SAME per the "
    "rule above.\n"
    "Reply with ONLY a JSON object and nothing else: "
    '{"same": <true|false>, "confidence": <0..1>, "reason": "<short>"}.'
)

_VERDICT_RE = re.compile(r"\{[^{}]*?\"same\"[^{}]*?\}", re.DOTALL | re.IGNORECASE)

MERGE_VERDICT_RESPONSE_FORMAT: ResponseFormat = {
    "type": "json_schema",
    "json_schema": {
        "name": "marginalia_merge_verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "same": {"type": "boolean"},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "reason": {"type": "string"},
            },
            "required": ["same", "confidence", "reason"],
            "additionalProperties": False,
        },
    },
}


def parse_verdict(text: str) -> MergeVerdict:
    """Parse the judge's reply into a MergeVerdict. Tolerant of think blocks and
    prose around the JSON; on any failure returns a DISTINCT verdict — the safe
    default never merges on an unparseable answer. When several objects are
    present (a model that restates), the LAST one wins."""
    import json

    for blob in reversed(_VERDICT_RE.findall(text or "")):
        try:
            data = json.loads(blob)
        except (ValueError, TypeError):
            continue
        if "same" not in data:
            continue
        try:
            conf = float(data.get("confidence", 0.0))
        except (ValueError, TypeError):
            conf = 0.0
        return MergeVerdict(
            same=bool(data["same"]),
            confidence=_clamp(conf),
            reason=str(data.get("reason", ""))[:200],
        )
    return MergeVerdict(same=False, confidence=0.0, reason="unparseable")


class LLMMergeJudge:
    """A MergeJudge backed by an LLM provider. Conservative by construction: the
    prompt biases 'distinct when unsure' and any provider failure yields a
    DISTINCT verdict, so the judge can only ever reduce duplication, never
    fabricate a merge."""

    # max_tokens is explicit (not the provider's opaque 1024 default): a reasoning
    # model emits a <think> block before the verdict JSON, so too small a cap
    # truncates it mid-thought and the judge silently degrades to DISTINCT. 2000
    # gives headroom for a short verdict while staying far cheaper than extraction.
    def __init__(
        self,
        provider,
        *,
        temperature: float = 0.0,
        max_tokens: int = 2000,
        top_p: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        presence_penalty: float | None = None,
        enable_thinking: bool | None = None,
        system_prompt: str | None = None,
    ) -> None:
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._top_p = top_p
        self._top_k = top_k
        self._min_p = min_p
        self._presence_penalty = presence_penalty
        self._enable_thinking = enable_thinking
        self._system_prompt = system_prompt

    def judge(
        self,
        candidate: NodeCandidate,
        existing: Node,
        *,
        candidate_context: str = "",
        existing_context: str = "",
    ) -> MergeVerdict:
        from okto_neuron.llm import LLMProviderError, Message, complete_with_retry

        user = _build_judge_prompt(
            candidate.type,
            candidate.title,
            candidate.content,
            existing.type,
            existing.title,
            existing.content,
            candidate_context,
            existing_context,
        )
        retries: list[dict[str, object]] = []
        try:
            reply = complete_with_retry(
                self._provider,
                [Message("system", self._system_prompt or _VERDICT_SYSTEM), Message("user", user)],
                step="judge",
                retries=retries,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                top_p=self._top_p,
                top_k=self._top_k,
                min_p=self._min_p,
                presence_penalty=self._presence_penalty,
                enable_thinking=self._enable_thinking,
                response_format=MERGE_VERDICT_RESPONSE_FORMAT,
            )
        except LLMProviderError:
            return MergeVerdict(
                same=False,
                confidence=0.0,
                reason="llm-unavailable",
                provider_retries=tuple(retries),
            )
        verdict = parse_verdict(reply)
        return replace(verdict, provider_retries=tuple(retries)) if retries else verdict


_REL_CAP = 5
"""Cap on relationship lines per entity offered to the judge. A handful of edges
is enough identity signal; more would bloat the prompt and dilute the salient
ones (the band already restricts the judge to genuine look-alikes)."""

_RELATIONSHIPS_LABEL = "Relationships:"
_COMPETING_NAMES_LABEL = "Other names in scope"

_RELATIONSHIPS_NOTE = (
    "The Relationships lines are SUPPORTING evidence about each entity's role and "
    "connections; they do not by themselves decide identity."
)

_COMPETING_NAMES_NOTE = (
    'An "Other names in scope" line is a CHECKED FACT, not supporting evidence: '
    "it tells you directly whether a competing full name sharing the leading "
    "token already exists — see the system instructions for how to use it."
)

_DISTINGUISHING_PREDICATES = frozenset(
    {"distinguished_from", "alias_of", "different_from", "not_same_as"}
)
"""Predicates that argue two entities are DISTINCT. These are pulled to the front
before the relationship cap so a precision-critical edge (the one signal that
keeps a look-alike apart) is never the line dropped on a high-degree entity —
such edges typically arrive as INCOMING edges and would otherwise sort last."""


def _build_judge_prompt(
    a_type: str,
    a_title: str,
    a_content: str,
    b_type: str,
    b_title: str,
    b_content: str,
    a_context: str = "",
    b_context: str = "",
) -> str:
    """Render the same/distinct user prompt. With both contexts empty this is
    byte-for-byte the original name+description prompt — so the default judge
    behaviour is unchanged and any verdict shift is attributable to real context.
    A non-empty context block is injected, indented, beneath its entity — it may
    carry a Relationships block, an "Other names in scope" block (see
    ``_competing_names_context``), or both, concatenated by the caller. Each
    kind gets its OWN explanatory note, and only when that kind is actually
    present — a zero-relationship, shared-leading-token pair must not be told
    to weigh "Relationships lines" that aren't in its prompt."""

    def block(label: str, type_: str, title: str, content: str, context: str) -> str:
        s = (
            f"Entity {label} (type {type_}):\n"
            f"  name: {title}\n"
            f"  description: {content[:500].strip()}\n"
        )
        if context:
            s += f"  {context}\n"
        return s

    user = block("A", a_type, a_title, a_content, a_context)
    user += "\n"
    user += block("B", b_type, b_title, b_content, b_context)
    combined_context = a_context + b_context
    notes = []
    if _RELATIONSHIPS_LABEL in combined_context:
        notes.append(_RELATIONSHIPS_NOTE)
    if _COMPETING_NAMES_LABEL in combined_context:
        notes.append(_COMPETING_NAMES_NOTE)
    if notes:
        user += "\n" + " ".join(notes) + "\n"
    user += "\nAre A and B the same real-world entity?"
    return user


def _format_relationships(typed_lines: list[tuple[str, str]], cap: int = _REL_CAP) -> str:
    """A 'Relationships:' block for the judge prompt, or '' when there are none.
    Continuation lines are indented to sit under the label.

    Distinguishing predicates (``alias_of`` / ``distinguished_from`` / …) are
    pulled to the front before the cap via a *stable* sort, so the one edge that
    keeps a look-alike apart is never the line dropped on a high-degree entity.
    Such edges typically arrive as incoming edges and would otherwise sort last.
    Order within each group is preserved."""
    if not typed_lines:
        return ""
    ordered = sorted(
        typed_lines,
        key=lambda tl: 0 if tl[0] in _DISTINGUISHING_PREDICATES else 1,
    )
    body = "\n    ".join(tl[1] for tl in ordered[:cap])
    return f"{_RELATIONSHIPS_LABEL}\n    {body}"


def _candidate_relationships(
    cand_id: str,
    title: str,
    edges: list[EdgeCandidate],
    id_to_title: dict[str, str],
    cap: int = _REL_CAP,
) -> str:
    """Relationships of a batch candidate, drawn from the batch's edge candidates.
    Two-way by design: an edge can corroborate sameness (a shared neighbour) OR
    reveal distinctness (e.g. ``bob-engineer distinguished_from agent:alice``), so
    it is a safe signal in both directions. Refs resolve to sibling candidate
    titles; a propositional literal renders as its value. Source path is
    deliberately omitted — it is constant within a ``remember()`` batch and so
    carries no discriminating signal, only merge pressure."""
    lines: list[tuple[str, str]] = []
    for e in edges:
        if e.src_ref == cand_id:
            if e.dst_ref:
                target = id_to_title.get(e.dst_ref, e.dst_ref)
            elif e.dst_literal is not None:
                target = str(e.dst_literal)
            else:
                target = e.dst_ref
            lines.append((e.type, f"{title} --{e.type}--> {target}"))
        elif e.dst_ref and e.dst_ref == cand_id:
            source = id_to_title.get(e.src_ref, e.src_ref)
            lines.append((e.type, f"{source} --{e.type}--> {title}"))
    return _format_relationships(lines, cap)


def _store_relationships(node: Node, store: GraphStore, cap: int = _REL_CAP) -> str:
    """Relationships of a committed store node, read from its edges (both
    directions). Neighbour ids resolve to titles; an unresolved id falls back to
    itself. Read-only."""

    def title_of(node_id: str) -> str:
        neighbour = store.get_node(node_id)
        return (getattr(neighbour, "title", None) or node_id) if neighbour else node_id

    lines: list[tuple[str, str]] = []
    for e in store.list_edges(src=node.id):
        lines.append((e.type, f"{node.title} --{e.type}--> {title_of(e.dst)}"))
    for e in store.list_edges(dst=node.id):
        lines.append((e.type, f"{title_of(e.src)} --{e.type}--> {node.title}"))
    return _format_relationships(lines, cap)


COMPETING_NAME_CAP = 3
"""Cap on competing-name examples offered to the judge per judged pair. The
judge only needs to know THAT a rival full name exists (or that none does) —
see ``_VERDICT_SYSTEM``'s "no conflicting surname" rule; past a couple of
examples, more names add prompt tokens without changing the answer, the same
"budget, not a veto" reasoning as ``LEXICAL_ALIAS_CAP``."""


def _tokens(title: str) -> list[str]:
    """Normalized tokens of ``title``, via the same tokenizer ``_alias_base``
    uses for discovery-key comparisons."""
    return _TITLE_TOKEN_RE.findall(discovery_surface_key(title))


def _shared_leading_token(a_title: str, b_title: str) -> str | None:
    """The leading token ``a_title`` and ``b_title`` both open on, or ``None``
    when they don't share one. The bare-name/fuller-name ambiguity the
    competing-names context resolves (see ``_VERDICT_SYSTEM``) only arises when
    the two titles being judged open on the same token, so nothing is gathered
    otherwise — this is what keeps an unrelated pair's prompt unchanged."""
    a_tokens = _tokens(a_title)
    b_tokens = _tokens(b_title)
    if not a_tokens or not b_tokens or a_tokens[0] != b_tokens[0]:
        return None
    return a_tokens[0]


def _format_competing_names(
    shared_token: str, names: list[str], cap: int = COMPETING_NAME_CAP
) -> str:
    """Render the judge-facing 'Other names in scope' line for a shared leading
    token. Always explicit once a shared token is found: an empty ``names``
    list still renders a "none found" statement rather than nothing, so the
    absence of a competing name reads as CHECKED, not merely unasked — this is
    what lets ``_VERDICT_SYSTEM`` treat "no conflicting surname" as a satisfied
    condition rather than an assumption. Deduplicated and sorted before the cap
    so which names are shown (and dropped) is deterministic."""
    if not names:
        return f'{_COMPETING_NAMES_LABEL}: none found for "{shared_token}".'
    shown = sorted(dict.fromkeys(names))[:cap]
    return f'{_COMPETING_NAMES_LABEL}: for "{shared_token}" — ' + "; ".join(shown) + "."


def _competing_names_context(a_title: str, b_title: str, pool_titles: list[str]) -> str:
    """Names of OTHER same-type entities already in scope (batch survivors in
    ``judge_within_batch``, committed store nodes in ``judge_against_store`` —
    the caller passes the pool) that open on the SAME leading token as both
    ``a_title`` and ``b_title`` and carry a second token of their own (i.e. are
    themselves a fuller name, not another bare mention) — the checkable fact
    behind ``_VERDICT_SYSTEM``'s "no conflicting surname" condition. ``''``
    (nothing rendered) when the pair doesn't share a leading token at all,
    since that is the only shape this ambiguity takes; matches how
    ``_format_relationships`` renders nothing for a childless entity."""
    token = _shared_leading_token(a_title, b_title)
    if token is None:
        return ""
    matches = []
    for t in pool_titles:
        t_tokens = _tokens(t)
        if len(t_tokens) > 1 and t_tokens[0] == token:
            matches.append(t)
    return _format_competing_names(token, matches)


def _join_context_blocks(*blocks: str) -> str:
    """Join an entity's non-empty context blocks (Relationships / Other names
    in scope) into the single ``context`` string ``_build_judge_prompt``
    accepts. Joined with two-space indentation so a second block lines up
    under the entity the same way ``block()`` aligns the first."""
    return "\n  ".join(b for b in blocks if b)


def judge_against_store(
    nodes: list[NodeCandidate],
    edges: list[EdgeCandidate],
    store: GraphStore,
    *,
    judge: MergeJudge,
    embedder: EmbeddingProvider | None = None,
    band_threshold: float = JUDGE_BAND_THRESHOLD,
    merge_confidence: float = MERGE_CONFIDENCE,
    k: int = JUDGE_K,
    on_pair: Callable[[str, str, "MergeVerdict"], None] | None = None,
    merge_blocked: Callable[[str, str], bool] | None = None,
) -> StoreReconciliation:
    """Tier 1 + Tier 2 entity resolution: recall the embedding band, then let an
    LLM judge decide same-vs-distinct.

    Runs AFTER Tier 0 (reconcile_against_store) on its survivors, so exact
    (type, normalized-title) duplicates are already gone and every neighbour
    surfaced here is a NON-exact look-alike. For each candidate, Tier 1 recalls
    the top same-type store nodes whose cosine clears band_threshold
    (find_similar, capped at k), plus lexical aliases (legal/publisher suffix,
    surname, token-subset, spelling-variant lanes — see
    ``_lexical_alias_score``) that embeddings can miss; that lexical lane
    RECALLS every match against the whole store but only offers the top
    ``LEXICAL_ALIAS_CAP`` (ranked strongest-first, deterministic tie-break) to
    the judge, so judge-call volume stays bounded by ``k + LEXICAL_ALIAS_CAP``
    per candidate instead of growing with store size — see that constant's
    docstring for the regression this closes. Tier 2 asks the judge whether
    each offered target is truly the same entity and merges into the FIRST
    adjudged 'same' that also clears merge_confidence. Everything else is left
    untouched — a genuinely-distinct look-alike (NX vs NX Lab) flows on to the
    normal resolve/gate, which parks it for review rather than fabricating a
    merge.

    Merges remap edge endpoints onto the surviving store id (so a re-mention's
    relationship still lands and still mints its byte-anchored Claim), exactly as
    Tier 0 does. Read-only with respect to the store; mutates nothing.

    Each judged pair is also handed a competing-names context (see
    ``_competing_names_context``): the OTHER same-type store node titles that
    open on the same leading token as the pair, capped at
    ``COMPETING_NAME_CAP``. This turns the judge prompt's "no conflicting
    surname" condition (``_VERDICT_SYSTEM``) into a checked fact instead of a
    guess — the judge otherwise sees only the two titles being compared and has
    no way to know whether a third, competing full name exists elsewhere in the
    store. Gathered from the same ``same_type_nodes`` scan the lexical lane
    already does, so this adds no extra store reads and no extra judge calls.

    Edge-endpoint guard: same ``EdgeEndpointGuard`` as Tier 0
    (``reconcile_against_store``) and the within-batch judge, built fresh here
    from ``edges`` as received and used via ``blocks_external`` (the store id a
    candidate merges into is never itself a blocking closure member — a
    candidate's own direct edge to that exact node is expected and harmless, see
    ``blocks_external``'s docstring). Within THIS call, that closes the
    transitive case: if candidate X merges into store node E first, a
    later-processed, edge-connected candidate Y is vetoed from also merging into
    E. It does NOT reach across the call boundary from a PRIOR
    ``reconcile_against_store`` pass — Tier 0 folds its own survivors into a
    store id using its own local guard, and that closure is not visible here.
    So a candidate whose Tier-0-remapped edge now points at a store id another
    Tier-0 survivor already merged into can still be re-merged into it at Tier
    1/2, reproducing the same self-loop-and-dropped-relationship failure one
    tier later. Closing that fully needs the two tiers' guards threaded
    together at the call site (``companion.remember``), which is outside this
    module's within-batch/within-store-pass boundary.

    ``on_pair`` is an OPTIONAL callback invoked as ``on_pair(candidate_id,
    target_id, verdict)`` (the judge's ``MergeVerdict``) after every judged pair — observability
    only (e.g. a live per-pair ledger row), never behaviour: a raising
    ``on_pair`` is logged (warning) and can never abort the pass. Default ``None``
    is a no-op, identical to the pre-existing behaviour."""
    embedder = embedder or get_provider()
    guard = EdgeEndpointGuard(edges)
    id_to_title = {c.candidate_id: c.title for c in nodes}
    survivors: list[NodeCandidate] = []
    merged_into: dict[str, str] = {}
    for cand in nodes:
        cand_ctx = _candidate_relationships(cand.candidate_id, cand.title, edges, id_to_title)
        target_id: str | None = None
        targets: list[Node] = []
        seen_target_ids: set[str] = set()
        for corr in find_similar(cand, store, embedder=embedder, threshold=band_threshold, k=k):
            existing = store.get_node(corr.target_id)
            if existing is None:
                continue
            targets.append(existing)
            seen_target_ids.add(existing.id)
        # Same-type store nodes, fetched once and reused below both for the
        # lexical-alias lane and for the competing-names context — avoids a
        # second store.list_nodes(type=...) scan per candidate.
        same_type_nodes = [
            n for n in store.list_nodes(type=cand.type) if n.id != cand.candidate_id and not is_infra(n)
        ]
        # Lexical lane: recall stays uncapped (every same-type store node the
        # widened ADR 0042 rule matches is scored), but the JUDGE CALLS it can
        # trigger are bounded — score, rank strongest-first with a deterministic
        # tie-break, then keep only the top LEXICAL_ALIAS_CAP. See that
        # constant's docstring for the measured 25->118 regression this closes.
        lexical_scored: list[tuple[float, str, Node]] = []
        for existing in same_type_nodes:
            if existing.id in seen_target_ids:
                continue
            score = _lexical_alias_score(cand.title, existing.title)
            if score is None:
                continue
            lexical_scored.append((score, existing.id, existing))
        lexical_scored.sort(key=lambda item: (-item[0], item[1]))
        for _score, existing_id, existing in lexical_scored[:LEXICAL_ALIAS_CAP]:
            targets.append(existing)
            seen_target_ids.add(existing_id)
        for existing in targets:
            if guard.blocks_external(cand.candidate_id, existing.id):
                continue
            if merge_blocked is not None and merge_blocked(cand.candidate_id, existing.id):
                continue
            # "Other same-type entities already in scope" for the competing-names
            # context (Part B): every same-type store node except the one being
            # judged — see ``_competing_names_context`` for the leading-token
            # gate and cap.
            competing_ctx = _competing_names_context(
                cand.title,
                existing.title,
                [n.title for n in same_type_nodes if n.id != existing.id],
            )
            verdict = judge.judge(
                cand,
                existing,
                candidate_context=_join_context_blocks(cand_ctx, competing_ctx),
                existing_context=_join_context_blocks(
                    _store_relationships(existing, store), competing_ctx
                ),
            )
            if on_pair is not None:
                try:
                    on_pair(cand.candidate_id, existing.id, verdict)
                except Exception:
                    # Never aborts the pass, but never silent either: a
                    # callback that no longer matches this signature would
                    # otherwise drop every per-pair ledger row unnoticed.
                    _LOG.warning("dedup on_pair callback failed", exc_info=True)
            if verdict.same and verdict.confidence >= merge_confidence:
                target_id = existing.id
                break
        if target_id is not None and target_id != cand.candidate_id:
            merged_into[cand.candidate_id] = target_id
            guard.fold(cand.candidate_id, target_id)
        else:
            survivors.append(cand)

    if not merged_into:
        return StoreReconciliation(survivors, list(edges), {})
    return StoreReconciliation(survivors, _remap_edges(edges, merged_into), merged_into)


def judge_within_batch(
    nodes: list[NodeCandidate],
    edges: list[EdgeCandidate],
    *,
    judge: MergeJudge,
    embedder: EmbeddingProvider | None = None,
    band_threshold: float = JUDGE_BAND_THRESHOLD,
    merge_confidence: float = MERGE_CONFIDENCE,
    k: int = JUDGE_K,
    on_pair: Callable[[str, str, "MergeVerdict"], None] | None = None,
    merge_blocked: Callable[[str, str], bool] | None = None,
) -> StoreReconciliation:
    """Within-batch (candidate-vs-candidate) entity resolution — the sibling of
    ``judge_against_store`` that closes the twin-entity gap.

    ``judge_against_store`` only compares each candidate to the COMMITTED store,
    so two look-alikes born in the SAME ``remember()`` batch — e.g. an ``alice``
    node from a file's frontmatter block and an ``agent:alice`` node from its
    prose block — are both novel to the store, are never adjudicated against each
    other, and both commit side by side. This pass runs AFTER
    ``collapse_duplicates`` (exact normalized-title) and BEFORE
    ``reconcile_against_store``, comparing surviving candidates to their batch
    siblings.

    A later candidate is merged into an earlier one (first occurrence survives)
    only when ALL of: same type, candidate-embedding cosine clears
    ``band_threshold`` OR the titles are an obvious legal/publisher suffix alias,
    the pair are NOT (transitively) the two endpoints of one edge, and the judge
    returns ``same`` with confidence >= ``merge_confidence``. The edge-connected
    guard mirrors ``collapse_duplicates``: a relationship's subject and object are
    distinct by construction, so they must never fuse — not even transitively,
    via a shared ``EdgeEndpointGuard`` that tracks the full closure of ids already
    folded into each surviving slot (not just that slot's own id), so a candidate
    connected to an EARLIER-merged-away sibling is still vetoed from joining that
    sibling's survivor. 'Distinct when unsure' is preserved — a non-merge leaves
    both candidates to flow on to the store tiers and the gate.

    Transitivity falls out naturally: candidates are only ever compared to *kept
    survivors*, so A~B~C collapses into the single survivor A (B merged into A is
    no longer a comparison target; C is judged against A). Edges are remapped onto
    the surviving candidate id via ``_remap_edges`` (a re-mention's relationship
    still lands and still mints its byte-anchored Claim).

    Read-only with respect to the store — the store is never consulted here.

    Each judged pair is also handed a competing-names context (see
    ``_competing_names_context``): the OTHER kept survivors' titles (of the
    candidate's type) that open on the same leading token as the pair, capped
    at ``COMPETING_NAME_CAP`` — the within-batch twin of the same context
    ``judge_against_store`` gathers from the committed store. No extra judge
    calls; only the prompt content changes.

    ``on_pair`` is an OPTIONAL callback invoked as ``on_pair(candidate_id,
    target_id, verdict)`` (the judge's ``MergeVerdict``) after every judged pair — observability
    only, never behaviour: a raising ``on_pair`` is logged (warning) and can never
    abort the pass. Default ``None`` is a no-op, identical to the
    pre-existing behaviour."""
    embedder = embedder or get_provider()

    # Two candidates that are the endpoints of one edge are a relationship's
    # subject and object — distinct by construction. Never merge such a pair,
    # checked against the full closure already folded into each survivor (mirrors
    # collapse_duplicates' guard). Edge refs are candidate ids pre-remap.
    guard = EdgeEndpointGuard(edges)
    id_to_title = {c.candidate_id: c.title for c in nodes}

    survivors: list[NodeCandidate] = []
    survivor_vecs: list[tuple[float, ...]] = []
    merged_into: dict[str, str] = {}
    for cand in nodes:
        cand_vec = _candidate_vector(cand, embedder)
        cand_ctx = _candidate_relationships(cand.candidate_id, cand.title, edges, id_to_title)
        # Score against every kept survivor of the same type that clears the band
        # or lexical-alias recall, and is not this candidate's edge-partner; judge
        # the top-k by cosine, with lexical aliases kept in the candidate set even
        # when embeddings miss them.
        scored: list[tuple[float, NodeCandidate]] = []
        for surv, surv_vec in zip(survivors, survivor_vecs):
            if surv.type != cand.type:
                continue
            if guard.blocks(cand.candidate_id, surv.candidate_id):
                continue
            if merge_blocked is not None and merge_blocked(cand.candidate_id, surv.candidate_id):
                continue
            score = _cosine(cand_vec, surv_vec)
            is_alias = _is_lexical_alias_pair(cand.title, surv.title)
            if score >= band_threshold or is_alias:
                scored.append((max(score, 1.0 if is_alias else score), surv))
        scored.sort(key=lambda pair: pair[0], reverse=True)

        # "Other same-type entities already in scope" for the competing-names
        # context (Part B): every kept survivor of this candidate's type — see
        # ``_competing_names_context`` for the leading-token gate and cap.
        same_type_survivors = [s for s in survivors if s.type == cand.type]

        target_id: str | None = None
        target_index: int | None = None
        for surv in [pair[1] for pair in scored[:k]]:
            competing_ctx = _competing_names_context(
                cand.title,
                surv.title,
                [s.title for s in same_type_survivors if s.candidate_id != surv.candidate_id],
            )
            verdict = judge.judge(
                cand,
                surv.to_node(),
                candidate_context=_join_context_blocks(cand_ctx, competing_ctx),
                existing_context=_join_context_blocks(
                    _candidate_relationships(surv.candidate_id, surv.title, edges, id_to_title),
                    competing_ctx,
                ),
            )
            if on_pair is not None:
                try:
                    on_pair(cand.candidate_id, surv.candidate_id, verdict)
                except Exception:
                    # Never aborts the pass, but never silent either: a
                    # callback that no longer matches this signature would
                    # otherwise drop every per-pair ledger row unnoticed.
                    _LOG.warning("dedup on_pair callback failed", exc_info=True)
            if verdict.same and verdict.confidence >= merge_confidence:
                target_id = surv.candidate_id
                target_index = survivors.index(surv)
                break

        if target_id is not None and target_id != cand.candidate_id:
            merged_into[cand.candidate_id] = target_id
            guard.fold(cand.candidate_id, target_id)
            if target_index is not None:
                survivor = survivors[target_index]
                survivors[target_index] = survivor.model_copy(
                    update={
                        "surface": merge_surface_records(
                            survivor.surface,
                            cand.surface,
                            canonical_title=survivor.title,
                        )
                    }
                )
        else:
            survivors.append(cand)
            survivor_vecs.append(cand_vec)

    if not merged_into:
        return StoreReconciliation(survivors, list(edges), {})
    return StoreReconciliation(survivors, _remap_edges(edges, merged_into), merged_into)


__all__ = [
    "ResolveOutcome",
    "resolve",
    "find_similar",
    "find_contradictions",
    "reconcile_against_store",
    "StoreReconciliation",
    "judge_against_store",
    "judge_within_batch",
    "MergeVerdict",
    "MergeJudge",
    "LLMMergeJudge",
    "_VERDICT_SYSTEM",
    "parse_verdict",
    "SIMILAR_THRESHOLD",
    "JUDGE_BAND_THRESHOLD",
    "MERGE_CONFIDENCE",
    "LEXICAL_ALIAS_CAP",
    "COMPETING_NAME_CAP",
]
