"""Cluster adjudication — the decision gate (REUSES the conservative judge).

ADR 0008. Candidate clusters come from ``candidates.py`` (recall, permissive).
This module decides — and it does so with the EXISTING ``LLMMergeJudge`` /
``_VERDICT_SYSTEM`` / ``parse_verdict`` machinery, which already hard-codes the
precision exemplars ("NX vs NX Lab distinct", "two people sharing a first name
distinct") and defaults DISTINCT on an unparseable reply. No second judge, no
second magic number.

Default path: PAIRWISE — reduce a cluster to its canonical member and judge each
other member against it. Optional ``--cluster-judge`` compare/select asks the
judge to pick the same-entity members in one prompt (SOTA precision edge), with a
DISTINCT-on-unparseable parse and a fall-back to pairwise. Pairwise is always the
safe default.

Corroboration is a GRADED, SUPPORTING signal — NOT a hard precondition. Broad
discovery matches such as handle decomposition may reach the judge, but they do
not become identity-grade auto-merge evidence without exact-token or relational
support.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

from okto_neuron.consolidate import NodeCandidate
from okto_neuron.core.schema import Node
from okto_neuron.llm import ResponseFormat
from okto_neuron.reconcile.candidates import (
    CandidateCluster,
    _is_distinctive,
    _neg_id,
    email_handle_tokens,
    jaro_winkler,
)
from okto_neuron.resolve import (
    _VERDICT_SYSTEM,
    MERGE_CONFIDENCE,
    MergeJudge,
    MergeVerdict,
    _store_relationships,
)
from okto_neuron.semantic_surface import discovery_surface_key, exact_surface_key
from okto_neuron.store.protocol import GraphStore

# ── tunables ──────────────────────────────────────────────────────────────────
JW_STRONG = 0.90
REL_JACCARD = 0.25
"""Neighbour-set Jaccard floor for the 'relational' corroboration signal."""

RECONCILE_AUTO_CONFIDENCE = 0.9
"""High-confidence auto-merge floor (> ``MERGE_CONFIDENCE`` 0.8 — deliberately
conservative; false merges are ~irreversible so precision dominates recall)."""

_VETO_PREDICATES = frozenset({"distinguished_from", "different_from", "not_same_as"})
"""Edge predicates that assert two entities are NOT the same — the corroboration
NEGATIVE tier (ADR 0010 Axis 3). A distinguishing edge between the canonical and a
variant in EITHER direction removes that variant from ``corroborated_ids`` even if
the judge said 'same'. This is the new power: corroboration can SUBTRACT a wrongly-
merged member, not only narrow a merge.

DELIBERATELY NOT ``resolve._DISTINGUISHING_PREDICATES``: that constant feeds the
judge prompt's relationships block (``_format_relationships``) and includes
``alias_of`` — which denotes SAMENESS, not distinctness (``bob alias_of robert`` =
the two ARE the same; see ``tests/resolve/test_merge_judge.py``). Using it for the
veto would remove a variant that an edge asserts IS the canonical. The veto needs a
clean distinctness-only set, so it is defined locally here (touching the resolve
constant would also be a judge-prompt change = ADR 0010 P4, out of P3 scope)."""


@dataclass(frozen=True)
class ClusterVerdict:
    cluster_id: str
    same: bool
    confidence: float
    canonical_id: str
    member_ids: tuple[str, ...]
    corroboration: str  # "relational" | "lexical" | "both" | "none"
    reason: str = ""
    corroborated_ids: tuple[str, ...] = ()
    """Canonical + the survivors that pass corroboration PER-VARIANT (relational
    Jaccard ≥ ``REL_JACCARD`` OR strong lexical match). The auto-merge path mints
    its AuthorityRecord over THIS subset only; survivors absent here are queued.
    So one corroborated survivor cannot drag an uncorroborated co-member into an
    irreversible-feeling merge (it is queued for human review instead)."""


# ── canonical selection ─────────────────────────────────────────────────────────
def _degree(store: GraphStore, node_id: str) -> int:
    return sum(1 for _ in store.list_edges(src=node_id)) + sum(
        1 for _ in store.list_edges(dst=node_id)
    )


def select_canonical(store: GraphStore, members: list[Node]) -> Node:
    """Highest node degree wins (the richest existing entity); ties → longest
    title, then lexicographically smallest id. Keeps thin handles as variants of
    the rich record (Casey Lee / casey.lee)."""

    def key(node: Node) -> tuple[int, int, str]:
        return (_degree(store, node.id), len(node.title or ""), _neg_id(node.id))

    return max(members, key=key)


# ── corroboration (graded, supporting — never a hard gate) ──────────────────────
def _neighbours(store: GraphStore, node_id: str) -> set[str]:
    out: set[str] = set()
    for e in store.list_edges(src=node_id):
        out.add(e.dst)
    for e in store.list_edges(dst=node_id):
        out.add(e.src)
    out.discard(node_id)
    return out


def _relational_jaccard(store: GraphStore, a_id: str, b_id: str) -> float:
    na, nb = _neighbours(store, a_id), _neighbours(store, b_id)
    if not na and not nb:
        return 0.0
    inter = len(na & nb)
    union = len(na | nb)
    return inter / union if union else 0.0


def _lexical_match(a: Node, b: Node) -> bool:
    if jaro_winkler(a.title, b.title) >= JW_STRONG:
        return True
    a_tok = email_handle_tokens(a.title)
    b_tok = email_handle_tokens(b.title)
    a_title = discovery_surface_key(a.title)
    b_title = discovery_surface_key(b.title)
    if a_tok and a_tok == b_title:
        return True
    if b_tok and b_tok == a_title:
        return True
    if a_tok and b_tok and a_tok == b_tok:
        return True
    return False


def _identity_tokens(title: str) -> frozenset[str]:
    """Whitespace-delimited exact-key tokens for auto-merge corroboration.

    Discovery punctuation/separator folding must not manufacture identity-grade
    evidence. Dotted and underscored handles therefore remain one exact token and
    can be recalled lexically, but cannot auto-merge without relational evidence.
    """

    return frozenset(token for token in exact_surface_key(title).split(" ") if token)


def _shared_distinctive_token(a: Node, b: Node) -> bool:
    """True iff the two titles share at least one DISTINCTIVE token (≥4 chars, not a
    generic org/role word) on the exact-key whitespace tokenization. Used to deny
    ``fuzzy`` a shared exact token (identity-grade, not fuzzy)."""
    return any(
        _is_distinctive(tok) for tok in (_identity_tokens(a.title) & _identity_tokens(b.title))
    )


def _has_identity(a: Node, b: Node) -> bool:
    """Identity-grade evidence from exact-key token containment only."""

    left = _identity_tokens(a.title)
    right = _identity_tokens(b.title)
    if not left or not right or left == right:
        return False
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    return shorter < longer and any(_is_distinctive(token) for token in shorter)


def _has_negative_edge(store: GraphStore, canonical: Node, variant: Node) -> bool:
    """A distinguishing edge (``_VETO_PREDICATES``) between canonical and variant in
    EITHER direction. Node-anchored reads only (``list_edges(src=)`` from each end —
    src-anchored from both nodes covers both directions, never a full-scan
    ``list_edges()`` which mis-projects src/dst, ADR 0010 invariant 4)."""
    for e in store.list_edges(src=canonical.id):
        if e.dst == variant.id and e.type in _VETO_PREDICATES:
            return True
    for e in store.list_edges(src=variant.id):
        if e.dst == canonical.id and e.type in _VETO_PREDICATES:
            return True
    return False


def _variant_evidence(store: GraphStore, canonical: Node, variant: Node) -> set[str]:
    """Per-variant GRADED corroboration (ADR 0010 Axis 3) — ONE unified signal
    replacing the boolean ``_variant_corroborated``. Returns any of:

    - ``identity`` — exact-key token containment. SUFFICIENT for auto-merge.
    - ``relational`` — neighbour-set Jaccard ≥ ``REL_JACCARD`` (node-anchored).
      SUFFICIENT for auto-merge.
    - ``fuzzy`` — ``jaro_winkler ≥ JW_STRONG`` with NO shared distinctive token.
      SUPPORTING ONLY — never sufficient alone (invariant 6).
    - ``negative`` — a distinguishing edge between the two. The variant is REMOVED
      from ``corroborated_ids`` regardless of the judge verdict (the new power:
      corroboration can SUBTRACT a wrongly-merged member).

    ``corroborated_ids`` keeps a variant iff its evidence intersects
    ``{identity, relational}`` AND does NOT contain ``negative``."""
    ev: set[str] = set()
    if _has_negative_edge(store, canonical, variant):
        ev.add("negative")
    if _has_identity(canonical, variant):
        ev.add("identity")
    if _relational_jaccard(store, canonical.id, variant.id) >= REL_JACCARD:
        ev.add("relational")
    if jaro_winkler(canonical.title, variant.title) >= JW_STRONG and not _shared_distinctive_token(
        canonical, variant
    ):
        ev.add("fuzzy")
    return ev


def _variant_corroborated(store: GraphStore, canonical: Node, variant: Node) -> bool:
    """Per-variant corroboration verdict, derived from the graded ``_variant_evidence``:
    a variant is corroborated iff it carries identity-grade or relational evidence AND
    is NOT vetoed by a distinguishing edge. The auto-merge path mints its
    AuthorityRecord over the corroborated subset only, so an uncorroborated co-member
    is queued (never folded) and a vetoed member is subtracted."""
    ev = _variant_evidence(store, canonical, variant)
    if "negative" in ev:
        return False
    return bool(ev & {"identity", "relational"})


def _corroboration(store: GraphStore, canonical: Node, members: list[Node]) -> str:
    relational = any(
        _relational_jaccard(store, canonical.id, m.id) >= REL_JACCARD
        for m in members
        if m.id != canonical.id
    )
    lexical = any(_lexical_match(canonical, m) for m in members if m.id != canonical.id)
    if relational and lexical:
        return "both"
    if relational:
        return "relational"
    if lexical:
        return "lexical"
    return "none"


# ── cluster compare/select prompt (optional optimization) ───────────────────────
def _build_cluster_prompt(canonical: Node, others: list[Node]) -> str:
    """Compare/select prompt: which of the listed members are the SAME entity as
    the canonical? Embeds the same conservative distinct-exemplars as
    ``_VERDICT_SYSTEM`` (carried via the system message) and asks for a JSON list
    of indices + a confidence."""
    lines = [
        f"Canonical entity (type {canonical.type}):",
        f"  name: {canonical.title}",
        f"  description: {(canonical.content or '')[:400].strip()}",
        "",
        "Candidate members (index: name and description):",
    ]
    for idx, node in enumerate(others):
        lines.extend(
            [
                f"  {idx}: {node.title}",
                f"    description: {(node.content or '')[:400].strip()}",
            ]
        )
    lines.append("")
    lines.append(
        "Reply with ONLY a JSON object and nothing else: "
        '{"same_indices": [<indices that are the SAME entity as the canonical>], '
        '"confidence": <0..1>, "reason": "<short>"}.'
    )
    return "\n".join(lines)


_CLUSTER_RE = re.compile(r"\{[^{}]*?\"same_indices\"[^{}]*?\}", re.DOTALL | re.IGNORECASE)

CLUSTER_VERDICT_RESPONSE_FORMAT: ResponseFormat = {
    "type": "json_schema",
    "json_schema": {
        "name": "marginalia_cluster_verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "same_indices": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0},
                },
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "reason": {"type": "string"},
            },
            "required": ["same_indices", "confidence", "reason"],
            "additionalProperties": False,
        },
    },
}


def parse_cluster_verdict(text: str, n_others: int) -> tuple[set[int], float, str] | None:
    """Parse the compare/select reply → (same-index set, confidence, reason).
    Returns ``None`` on any failure so the caller falls back to pairwise
    (DISTINCT-on-unparseable semantics, mirroring ``parse_verdict``)."""
    for blob in reversed(_CLUSTER_RE.findall(text or "")):
        try:
            data = json.loads(blob)
        except (ValueError, TypeError):
            continue
        if "same_indices" not in data:
            continue
        raw = data.get("same_indices") or []
        try:
            idxs = {int(i) for i in raw if 0 <= int(i) < n_others}
        except (ValueError, TypeError):
            continue
        try:
            conf = float(data.get("confidence", 0.0))
        except (ValueError, TypeError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        return idxs, conf, str(data.get("reason", ""))[:200]
    return None


# ── adjudication ────────────────────────────────────────────────────────────────
def _node_to_candidate(node: Node) -> NodeCandidate:
    """Wrap a committed store Node as a NodeCandidate so the existing judge
    signature (candidate vs existing Node) is reused verbatim."""
    return NodeCandidate(
        type=node.type,
        title=node.title,
        content=node.content,
        facets=dict(node.facets),
        embedding=tuple(node.embedding) if node.embedding else None,
    )


def adjudicate_cluster(
    cluster: CandidateCluster,
    store: GraphStore,
    *,
    judge: MergeJudge,
    embedder=None,
    use_cluster_judge: bool = False,
    merge_confidence: float = MERGE_CONFIDENCE,
    merge_blocked: Callable[[str, str], bool] | None = None,
) -> ClusterVerdict:
    """Decide a cluster. Reduce to canonical, judge the rest against it; survivors
    are the members the judge calls 'same' at ≥ ``merge_confidence``. Cluster is
    'same' iff ≥1 survives; confidence = min surviving member confidence."""
    # One batched read (was one get_node per member); member order, duplicate
    # ids and missing-id skips are preserved.
    fetched = {n.id: n for n in store.get_nodes(cluster.member_ids, include_embedding=True)}
    members = [fetched[mid] for mid in cluster.member_ids if mid in fetched]
    if len(members) < 2:
        return ClusterVerdict(
            cluster.cluster_id, False, 0.0, "", cluster.member_ids, "none", "thin"
        )
    canonical = select_canonical(store, members)
    others = [n for n in members if n.id != canonical.id]
    if merge_blocked is not None:
        others = [n for n in others if not merge_blocked(canonical.id, n.id)]

    surviving: list[tuple[Node, float]] = []
    reason = ""

    cluster_judged = False
    if use_cluster_judge:
        cluster_result = _try_cluster_judge(canonical, others, judge)
        if cluster_result is not None:
            # A VALID parse (even "none match" → empty survivors) is authoritative;
            # only a None (unparseable / no provider) falls back to pairwise. This
            # keeps a valid distinct verdict from being overridden by pairwise.
            surviving, reason = cluster_result
            cluster_judged = True

    if not cluster_judged:
        surviving, reason = _pairwise(canonical, others, store, judge, merge_confidence)

    # A cluster can contain two variants that are each plausible aliases of the
    # canonical while an explicit negative-cache record says those variants are
    # distinct from one another. The negative decision wins over transitive
    # closure: retain a stable maximal subset that contains no blocked pair.
    if merge_blocked is not None and surviving:
        consistent: list[tuple[Node, float]] = []
        for node, confidence in surviving:
            if any(merge_blocked(node.id, kept.id) for kept, _ in consistent):
                continue
            consistent.append((node, confidence))
        surviving = consistent

    same = bool(surviving)
    confidence = min((c for _, c in surviving), default=0.0)
    survivor_ids = tuple(sorted([canonical.id, *[n.id for n, _ in surviving]]))
    survivor_nodes = [n for n, _ in surviving]
    corroboration = _corroboration(store, canonical, survivor_nodes) if same else "none"
    # Per-variant GRADED corroboration (ADR 0010 Axis 3) → the subset the auto-merge
    # path may mint over. Evaluated PER survivor (not cluster-level any()): a survivor
    # is kept iff its `_variant_evidence` intersects {identity, relational} and is NOT
    # vetoed by a distinguishing edge. So an uncorroborated co-member is never dragged
    # into auto-merge by a corroborated sibling (it queues), and a member the judge
    # wrongly called 'same' is SUBTRACTED when a distinguishing edge vetoes it.
    corroborated_ids = (
        tuple(
            sorted(
                [canonical.id]
                + [n.id for n in survivor_nodes if _variant_corroborated(store, canonical, n)]
            )
        )
        if same
        else ()
    )
    return ClusterVerdict(
        cluster_id=cluster.cluster_id,
        same=same,
        confidence=confidence,
        canonical_id=canonical.id if same else "",
        member_ids=survivor_ids if same else cluster.member_ids,
        corroboration=corroboration,
        reason=reason,
        corroborated_ids=corroborated_ids,
    )


def _pairwise(
    canonical: Node,
    others: list[Node],
    store: GraphStore,
    judge: MergeJudge,
    merge_confidence: float,
) -> tuple[list[tuple[Node, float]], str]:
    """Judge each ``other`` against the canonical.

    M1 instrumentation (ADR 0010, Axis 2): the judge ``reason`` is captured for
    EVERY member judged — merge AND distinct — and stitched into the returned
    summary string. The prior code only recorded reasons on the surviving merge
    branch, so every distinct-verdict reason was discarded — which is exactly
    what made the "26/28 empty reason" propose-run measurement an artifact rather
    than a real disengagement signal. This is observability only: the survivor
    set, merge confidence, and downstream cluster/corroboration behavior are
    unchanged. (A richer per-member trace seam is deferred to the M2 abstain
    policy, ADR 0010 P6 — not built here.)
    """
    existing_ctx = _store_relationships(canonical, store)
    surviving: list[tuple[Node, float]] = []
    reasons: list[str] = []
    for node in others:
        verdict: MergeVerdict = judge.judge(
            _node_to_candidate(node),
            canonical,
            candidate_context=_store_relationships(node, store),
            existing_context=existing_ctx,
        )
        merged = verdict.same and verdict.confidence >= merge_confidence
        if merged:
            surviving.append((node, verdict.confidence))
        # Capture the reason for EVERY member judged (merge AND distinct), so
        # judge engagement on name-only clusters is no longer discarded.
        if verdict.reason:
            reasons.append(verdict.reason)
    return surviving, "; ".join(reasons[:3])


def _try_cluster_judge(
    canonical: Node,
    others: list[Node],
    judge: MergeJudge,
) -> tuple[list[tuple[Node, float]], str] | None:
    """Run the compare/select prompt via the judge's provider. Returns ``None`` on
    any failure so the caller falls back to pairwise. Only available on judges
    exposing the LLM ``_provider`` seam (the real ``LLMMergeJudge``)."""
    provider = getattr(judge, "_provider", None)
    if provider is None:
        return None
    from okto_neuron.llm import LLMProviderError, Message

    user = _build_cluster_prompt(canonical, others)
    system = getattr(judge, "_system_prompt", None) or _VERDICT_SYSTEM
    try:
        reply = provider.complete(
            [Message("system", system), Message("user", user)],
            temperature=getattr(judge, "_temperature", 0.0),
            max_tokens=getattr(judge, "_max_tokens", 2000),
            # M4 (ADR 0010 Axis 2): forward the judge's configured thinking flag.
            # Without it this path ran at provider-default thinking, which burns the
            # whole token budget on reasoning and returns EMPTY content
            # (finish_reason: length) — re-triggering the non-committal failure the
            # ADR 0008 fix already corrected for the pairwise path. Mirror the
            # _temperature/_max_tokens getattrs; None matches LLMMergeJudge's default.
            enable_thinking=getattr(judge, "_enable_thinking", None),
            response_format=CLUSTER_VERDICT_RESPONSE_FORMAT,
        )
    except LLMProviderError:
        # Deliberately a bare ``complete`` (no ADR 0039 D5 retry): a failure
        # here does not degrade the decision, it falls back to the pairwise
        # judge, whose ``LLMMergeJudge.judge`` calls already retry. Retrying
        # here too would stack attempts, like a batched curation call would.
        return None
    parsed = parse_cluster_verdict(reply, len(others))
    if parsed is None:
        return None
    idxs, conf, reason = parsed
    surviving = [(others[i], conf) for i in sorted(idxs)]
    return surviving, reason


__all__ = [
    "ClusterVerdict",
    "adjudicate_cluster",
    "select_canonical",
    "RECONCILE_AUTO_CONFIDENCE",
    "REL_JACCARD",
    "JW_STRONG",
    "_variant_evidence",
]
