"""ADR 0015 D2 — deterministic, no-LLM pre-filter for low-value candidates.

Pure helpers consumed by ``Companion.remember()``. Every rule here is
deterministic and conservative: demotion always means *queue* (recoverable via
the existing review surface), never silent discard. The companion records a
ledger ``comparison`` with ``method: "prefilter"`` for every candidate a rule
touches, so each decision is auditable.

Rules (config block ``consolidation.prefilter`` in ``config/_vault.py``):

1. Edge-predicate demotion list — conversation-mechanics predicates
   (``discusses``/``mentions``/…) are queued without a relation-curator call.
2. Low-signal node demotion — single-mention, single-trivial-sentence node
   candidates are queued without a node-curator call.
3. Near-dup literal collapse — same-block Claim candidates whose normalized
   literals are near-identical keep one representative; the rest supersede.
4. Established-entity fast path — a Tier-0 ``exact_store`` re-mention that
   brings no novel content forgoes the curator verdict (foregone conclusion).
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Iterable, Mapping

from okto_neuron.consolidate._candidates import EdgeCandidate, NodeCandidate
from okto_neuron.core.schema import Node
from okto_neuron.semantic_surface import discovery_surface_key

__all__ = [
    "collapse_near_dup_literals",
    "count_title_mentions",
    "demoted_predicate_reason",
    "is_established_re_mention",
    "is_near_dup",
    "is_trivial_node",
    "normalize_for_near_dup",
]

_NEAR_DUP_STRIP_RE = re.compile(r"[^0-9a-z]+")
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+")


def normalize_for_near_dup(value: object) -> str:
    """Casefold + collapse whitespace/punctuation into a comparable key.

    ``"Casey  Lee!"`` and ``"casey lee"`` normalize identically;
    digits and letters are the only signal kept.
    """

    return _NEAR_DUP_STRIP_RE.sub(" ", str(value or "").casefold()).strip()


def is_near_dup(left: object, right: object) -> bool:
    """True when two values are near-identical after normalization.

    Empty-vs-empty counts as a dup (no content on either side)."""

    return normalize_for_near_dup(left) == normalize_for_near_dup(right)


# ── Rule 1: edge-predicate demotion ────────────────────────────────────────────


def demoted_predicate_reason(
    raw_predicate: str,
    demote_predicates: Iterable[str],
    *,
    canonical_predicate: str = "",
) -> str | None:
    """Return a human-readable demotion reason when the predicate is listed.

    Checks the casefolded RAW predicate and, when provided, its canonical
    ``normalize_predicate`` form (the caller computes it — this module stays
    free of curator imports). Returns ``None`` when the predicate is fine.
    """

    demoted = {str(p).strip().casefold() for p in demote_predicates if str(p).strip()}
    if not demoted:
        return None
    raw = str(raw_predicate or "").strip().casefold()
    canonical = str(canonical_predicate or "").strip().casefold()
    if raw in demoted:
        return (
            f"prefilter: predicate {raw!r} is on the demote_predicates list "
            "(conversation mechanics, not knowledge); queued without a relation-curator call"
        )
    if canonical and canonical in demoted:
        return (
            f"prefilter: predicate {raw_predicate!r} canonicalizes to demoted "
            f"predicate {canonical!r}; queued without a relation-curator call"
        )
    return None


# ── Rule 2: low-signal node demotion ───────────────────────────────────────────


def count_title_mentions(candidates: Iterable[NodeCandidate]) -> Mapping[str, int]:
    """Count normalized-title occurrences across the remember() batch.

    Counts over the RAW per-block candidate sets (pre-collapse), so an entity
    extracted from three blocks counts as three mentions even though dedup
    later folds it to one candidate.
    """

    return Counter(key for key in (discovery_surface_key(cand.title) for cand in candidates) if key)


def _is_single_sentence(content: str) -> bool:
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(content) if s.strip()]
    return len(sentences) <= 1


def is_trivial_node(
    candidate: NodeCandidate,
    mention_count: int,
    *,
    min_mentions: int,
    max_trivial_node_chars: int,
) -> bool:
    """Rule 2 predicate: under-mentioned AND single trivial sentence.

    Content-agnostic by design (ADR 0015 D2 open question 2): keys off
    measurable recurrence within the document, never a content-type label.
    With ``min_mentions=1`` (default) the rule is off — the caller gates on
    that before computing counts.
    """

    if min_mentions < 2 or mention_count >= min_mentions:
        return False
    content = (candidate.content or "").strip()
    return len(content) <= max_trivial_node_chars and _is_single_sentence(content)


# ── Rule 3: near-dup literal collapse ──────────────────────────────────────────


def collapse_near_dup_literals(
    edges: list[EdgeCandidate],
) -> tuple[list[EdgeCandidate], list[tuple[EdgeCandidate, EdgeCandidate]]]:
    """Collapse same-block Claim candidates with near-identical literals.

    Among ``dst_literal`` candidates sharing ``(block_id, src_ref, predicate)``,
    the first normalized literal wins; later near-dups are superseded. Returns
    ``(kept, [(superseded, survivor), ...])`` in original order. Topology edges
    and block-less claims pass through untouched (the key requires a block).
    """

    kept: list[EdgeCandidate] = []
    superseded: list[tuple[EdgeCandidate, EdgeCandidate]] = []
    first_by_key: dict[tuple[str, str, str, str], EdgeCandidate] = {}
    for edge in edges:
        if edge.dst_literal is None or not edge.block_id:
            kept.append(edge)
            continue
        key = (
            edge.block_id,
            edge.src_ref,
            str(edge.type or "").casefold(),
            normalize_for_near_dup(edge.dst_literal),
        )
        survivor = first_by_key.get(key)
        if survivor is None:
            first_by_key[key] = edge
            kept.append(edge)
        else:
            superseded.append((edge, survivor))
    return kept, superseded


# ── Rule 4: established-entity fast path ───────────────────────────────────────


def is_established_re_mention(candidate: NodeCandidate, node: Node) -> bool:
    """True when the candidate brings no novel content beyond a re-mention.

    The node curator is a hallucination firewall, not an existence check: when
    a Tier-0 ``exact_store`` match hit a live store node and the candidate's
    content sentence is a near-dup of content already on that node (its
    ``content`` or any string facet value), the curator verdict is a foregone
    conclusion. Empty candidate content is trivially non-novel.

    ADR 0015 D2 rule 4 open point: ideally "established" would mean the store
    node was itself curator-COMMITTED, not merely present. The store does not
    record curator provenance on nodes today (deriving it would mean a ledger
    scan per candidate), so this implements the documented fallback —
    "exists in store + no novel content".
    """

    content = normalize_for_near_dup(candidate.content)
    if not content:
        return True
    if content in (
        normalize_for_near_dup(node.content),
        normalize_for_near_dup(node.title),
    ):
        return True
    return any(
        content == normalize_for_near_dup(value)
        for value in (node.facets or {}).values()
        if isinstance(value, str)
    )
