"""Intra-batch candidate dedup — collapse same-entity duplicates BEFORE staging.

Root cause this fixes: ``companion.remember`` resolves each candidate against
the *committed* store before any commit, so two candidates for the SAME entity
extracted within ONE ``remember`` both read as novel, both clear the gate, and
both commit as separate nodes. ``candidate_id`` is a content hash of
``(type, title, content)`` — two mentions with the same title but different
surrounding content get different ids, so the per-block ``node_by_id`` dedup
upstream does not catch them either.

We collapse here, before the session opens, and remap edge references from the
dropped duplicate to the survivor in the same pass, so no edge dangles.

Merge signal: SAME ``type`` AND normalized-title equality — high-precision and
exact-after-normalization. We deliberately do NOT use an embedding-cosine leg:
distinct entities extracted from one block (a relationship's subject and object)
routinely share the SAME content AND near-identical titles (a shared block key),
so any cosine threshold loose enough to catch trivial re-mention variants also
fuses genuinely-distinct co-block entities — collapsing the relationship into a
self-loop and dropping its Claim. Near-but-not-equal titles (0.82–0.92 cosine)
still flow through resolve and the human review queue rather than being silently
fused here.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping

from okto_neuron.consolidate._candidates import EdgeCandidate, NodeCandidate
from okto_neuron.embed import EmbeddingProvider
from okto_neuron.semantic_surface import (
    SurfaceRecord,
    exact_surface_key,
    merge_surface_records,
)


class EdgeEndpointGuard:
    """Vetoes a merge that would fuse the two endpoints of a staged edge into one
    survivor — checked against the FULL closure of ids already folded into that
    survivor, not just the survivor's own id.

    Shared by every merge/dedup pass that runs over one ``remember()`` batch's
    edges (``collapse_duplicates``, ``judge_within_batch``,
    ``reconcile_against_store``): a relationship's subject and object are
    distinct by construction, so they must never end up as the same node — not
    even transitively, when one of them has already been folded into a survivor
    that used to be a *different* id.

    Without closure tracking, a chain of merges can smuggle an edge-connected
    pair together one hop at a time: A (unconnected) merges into survivor S,
    then B — edge-connected to A, but never checked against A once A stopped
    being its own survivor slot — merges into S too, silently turning the A-B
    edge into a self-loop that gets dropped. Tracking the closure closes that
    gap: once A folds into S, S's closure is ``{S, A}``, and B is vetoed
    because B is connected to A, a member of that closure.

    ``fold``/``blocks`` work over plain string ids so the same guard serves
    batch-candidate ids (``collapse_duplicates``, ``judge_within_batch``) and a
    mix of batch-candidate ids and already-committed store node ids
    (``reconcile_against_store``) without caring which is which.
    """

    __slots__ = ("_connected", "_closure")

    def __init__(self, edges: Iterable[EdgeCandidate]) -> None:
        # Edge refs are pre-remap batch-candidate ids (or, for a ref into the
        # store, an id never minted by this batch — harmless, it just never
        # matches anything). dst_ref is "" for a propositional (dst_literal)
        # edge; frozenset(src, "") is inert since no real id is ever "".
        self._connected: frozenset[frozenset[str]] = frozenset(
            frozenset((e.src_ref, e.dst_ref)) for e in edges if e.src_ref != e.dst_ref
        )
        self._closure: dict[str, set[str]] = {}

    @property
    def connected_pairs(self) -> frozenset[frozenset[str]]:
        """The raw edge-endpoint pairs this guard was built from."""
        return self._connected

    def _closure_of(self, node_id: str) -> set[str]:
        return self._closure.get(node_id, {node_id})

    def blocks(self, candidate_id: str, survivor_id: str) -> bool:
        """True if merging ``candidate_id`` into ``survivor_id`` would fuse two
        ids that are the two endpoints of one staged edge, checked against the
        full closure already folded into ``survivor_id`` (and, symmetrically,
        into ``candidate_id`` — a candidate can itself carry a closure when a
        prior merge attempt built one before being vetoed elsewhere).

        Use this when BOTH ids are fresh batch candidates (``collapse_duplicates``,
        ``judge_within_batch``): every id in play is, by this batch's own
        construction, a genuinely distinct newly-extracted entity, so even a
        DIRECT edge between ``candidate_id`` and ``survivor_id`` (no prior fold
        needed) must veto the merge outright."""
        if not self._connected:
            return False
        cand_closure = self._closure_of(candidate_id)
        surv_closure = self._closure_of(survivor_id)
        for left in cand_closure:
            for right in surv_closure:
                if left != right and frozenset((left, right)) in self._connected:
                    return True
        return False

    def blocks_external(self, candidate_id: str, external_id: str) -> bool:
        """Like :meth:`blocks`, but for merging a batch candidate into an id
        that is NOT itself a batch candidate — a committed store node
        (``reconcile_against_store``, ``judge_against_store``).

        A candidate's own DIRECT edge to that exact store id is expected and
        harmless: it becomes a self-loop on the merge remap and is dropped
        there, same as it always was (see e.g. an ``alias_of`` edge from a new
        mention straight to the existing node it turns out to BE — the edge
        was asserting the very identity the merge confirms). So ``external_id``
        is never itself treated as a blocking closure member here.

        What DOES veto: a batch SIBLING that has ALREADY folded into this same
        ``external_id`` (via a prior :meth:`fold` call) and is edge-connected to
        ``candidate_id`` — that is finding 3.3's actual shape: two distinct,
        edge-connected candidates both trying to collapse onto one store node.
        """
        if not self._connected:
            return False
        folded_siblings = self._closure_of(external_id) - {external_id}
        if not folded_siblings:
            return False
        cand_closure = self._closure_of(candidate_id)
        for left in cand_closure:
            for right in folded_siblings:
                if left != right and frozenset((left, right)) in self._connected:
                    return True
        return False

    def fold(self, dropped_id: str, survivor_id: str) -> None:
        """Record that ``dropped_id`` has merged into ``survivor_id``:
        ``survivor_id``'s closure now also owns everything already folded into
        ``dropped_id``, so a later candidate connected to ``dropped_id`` (or to
        anything already folded into it) is still vetoed from merging into
        ``survivor_id``."""
        merged = self._closure_of(survivor_id) | self._closure_of(dropped_id)
        for member in merged:
            self._closure[member] = merged


_WORD = re.compile(r"[\w'-]+", re.UNICODE)
_STOPWORDS = frozenset(
    {
        "about",
        "also",
        "from",
        "into",
        "that",
        "their",
        "there",
        "this",
        "with",
        "where",
        "which",
        "whose",
    }
)


def _normalized_title(title: str) -> str:
    """Shared conservative exact key for deterministic same-type collapse."""
    return exact_surface_key(title)


def collapse_duplicates(
    nodes: list[NodeCandidate],
    edges: list[EdgeCandidate],
    *,
    embedder: EmbeddingProvider | None = None,
    merge_cosine: float | None = None,
    source_text_by_id: Mapping[str, str] | None = None,
    merge_blocked: Callable[[str, str], bool] | None = None,
) -> tuple[list[NodeCandidate], list[EdgeCandidate]]:
    """Collapse intra-batch duplicate node candidates and remap edge refs to the
    survivor.

    Two candidates merge iff SAME ``type`` AND normalized-title-equal. Survivor =
    the strongest source-grounded mention when source text is available, otherwise
    first occurrence (stable). The survivor keeps its own id, title, and content
    (so its ``candidate_id`` is unchanged and edge refs stay valid); ``tags`` are
    unioned and ``facets`` shallow-merged (survivor wins on key conflict). Edge
    ``src_ref``/``dst_ref`` are rewritten through the ``dropped -> survivor`` map;
    self-loops created by the remap are dropped and identical edges de-duped.

    ``embedder`` / ``merge_cosine`` are accepted for call-site/signature
    stability but unused — see the module docstring on why the cosine leg was
    removed.
    """
    _ = (embedder, merge_cosine)  # intentionally unused; see module docstring
    if not nodes:
        return list(nodes), list(edges)

    # Endpoints of a staged edge are distinct entities BY CONSTRUCTION (a
    # relationship's subject ≠ its object). Never merge such a pair, even if they
    # normalize to the same title — that would self-loop the edge and drop its
    # Claim. This is a structural guard independent of the merge signal, checked
    # against the full closure already folded into each survivor (not just the
    # survivor's own id) via the shared EdgeEndpointGuard — see its docstring.
    guard = EdgeEndpointGuard(edges)
    connected = guard.connected_pairs
    indexed_nodes = list(enumerate(nodes))
    if source_text_by_id:
        key_by_id: dict[str, tuple[str, str]] = {}
        group_first_index: dict[tuple[str, str], int] = {}
        group_counts: dict[tuple[str, str], int] = {}
        for index, candidate in indexed_nodes:
            key = _candidate_key(candidate)
            if key is None:
                continue
            key_by_id[candidate.candidate_id] = key
            group_first_index.setdefault(key, index)
            group_counts[key] = group_counts.get(key, 0) + 1

        edge_connected_keys: set[tuple[str, str]] = set()
        for pair in connected:
            left, right = tuple(pair)
            left_key = key_by_id.get(left)
            if left_key is not None and left_key == key_by_id.get(right):
                edge_connected_keys.add(left_key)

        def sort_key(item: tuple[int, NodeCandidate]) -> tuple[int, int, int]:
            original_index, candidate = item
            key = _candidate_key(candidate)
            if key is None:
                return (original_index, 0, original_index)
            score = 0
            if group_counts.get(key, 0) > 1 and key not in edge_connected_keys:
                score = -_source_grounding_score(candidate, source_text_by_id)
            return (group_first_index.get(key, original_index), score, original_index)

        indexed_nodes.sort(key=sort_key)

    # survivor index keyed by (type, normalized-title); empty title never merges.
    by_key: dict[tuple[str, str], int] = {}
    survivors: list[NodeCandidate] = []
    dropped_to_survivor: dict[str, str] = {}

    for _, cand in indexed_nodes:
        key = _candidate_key(cand)
        match_idx = by_key.get(key) if key is not None else None

        if match_idx is not None and (
            guard.blocks(cand.candidate_id, survivors[match_idx].candidate_id)
            or (
                merge_blocked is not None
                and merge_blocked(
                    cand.candidate_id,
                    survivors[match_idx].candidate_id,
                )
            )
        ):
            match_idx = None  # structural/explicit veto: keep distinct

        if match_idx is None:
            if key is not None and key not in by_key:
                by_key[key] = len(survivors)
            survivors.append(cand)
            continue

        # Merge into survivor: union tags, shallow-merge facets (survivor wins).
        surv = survivors[match_idx]
        if cand.candidate_id != surv.candidate_id:
            dropped_to_survivor[cand.candidate_id] = surv.candidate_id
            guard.fold(cand.candidate_id, surv.candidate_id)
        merged_tags = tuple(dict.fromkeys((*surv.tags, *cand.tags)))
        merged_facets = {**cand.facets, **surv.facets}
        survivors[match_idx] = surv.model_copy(
            update={
                "tags": merged_tags,
                "facets": merged_facets,
                "surface": _merge_surface_evidence(surv, cand),
            }
        )

    if not dropped_to_survivor:
        return survivors, list(edges)

    # Remap edges; drop self-loops and de-dup identical edges.
    def remap(ref: str) -> str:
        return dropped_to_survivor.get(ref, ref)

    remapped: list[EdgeCandidate] = []
    seen: set[tuple[str, str, str, str | None]] = set()
    for edge in edges:
        new_src = remap(edge.src_ref)
        new_dst = remap(edge.dst_ref)
        if new_src == new_dst:
            continue  # self-loop created by collapse
        if new_src == edge.src_ref and new_dst == edge.dst_ref:
            new_edge = edge
        else:
            new_edge = edge.model_copy(update={"src_ref": new_src, "dst_ref": new_dst})
        key2 = (new_edge.type, new_src, new_dst, new_edge.block_id)
        if key2 in seen:
            continue
        seen.add(key2)
        remapped.append(new_edge)

    return survivors, remapped


def _merge_surface_evidence(
    survivor: NodeCandidate,
    dropped: NodeCandidate,
) -> SurfaceRecord | None:
    """Retain lossless variant evidence while preserving survivor identity."""
    return merge_surface_records(
        survivor.surface,
        dropped.surface,
        canonical_title=survivor.title,
    )


def _source_grounding_score(
    candidate: NodeCandidate,
    source_text_by_id: Mapping[str, str],
) -> int:
    """Cheap deterministic score used only to pick exact-duplicate survivors."""
    source = _normalized_title(source_text_by_id.get(candidate.candidate_id, ""))
    if not source:
        return 0
    score = 0
    title = _normalized_title(candidate.title)
    if title and title in source:
        score += 50
    source_tokens = set(_significant_tokens(source))
    score += 5 * len(_significant_tokens(candidate.title) & source_tokens)
    score += len(_significant_tokens(candidate.content) & source_tokens)
    return score


def _candidate_key(candidate: NodeCandidate) -> tuple[str, str] | None:
    norm = _normalized_title(candidate.title)
    return (candidate.type, norm) if norm else None


def _significant_tokens(text: str) -> set[str]:
    return {
        token
        for token in (_normalized_title(match.group(0)) for match in _WORD.finditer(text))
        if len(token) >= 4 and token not in _STOPWORDS
    }


__all__ = ["collapse_duplicates", "EdgeEndpointGuard"]
