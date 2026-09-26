"""Option A act path — DEFAULT, strictly OFF-GRAPH.

ADR 0008. ``apply_reconciliation`` runs the full reconcile pipeline:
``generate_candidate_clusters`` → ``adjudicate_cluster`` per cluster → split into
AUTO-MERGE / QUEUE / SKIP.

CRITICAL INVARIANT (the zero-write contract, the one property ADR-0007 forces):
this function and every path it touches write ONLY to
``.marginalia/authority/index.json`` and ``.marginalia/reconcile/queue.json``.
They MUST NOT call ``store.add_node`` or ``store.add_edge``. The unit test spies
BOTH counters and asserts ``== 0``. Authority is a Node subclass, so a bulk
``add_node`` into a populated graph would itself be the ADR-0007 corruption.

Auto-merge is high-confidence ONLY: verdict 'same' AND confidence ≥
``RECONCILE_AUTO_CONFIDENCE`` (0.9) AND positive corroboration (graded relational
overlap OR strong lexical+judge agreement). Reversible by construction:
``authority.remove(cluster_id)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from okto_neuron.reconcile.authority import AuthorityIndex, AuthorityRecord
from okto_neuron.reconcile.candidates import (
    CandidateCluster,
    cluster_id_for,
    generate_candidate_clusters,
)
from okto_neuron.reconcile.propose import (
    RECONCILE_AUTO_CONFIDENCE,
    ClusterVerdict,
    adjudicate_cluster,
)
from okto_neuron.reconcile.queue import ReconcileQueue
from okto_neuron.resolve import MergeJudge
from okto_neuron.store.protocol import GraphStore


@dataclass(frozen=True)
class ApplyReport:
    auto_merged: list[str] = field(default_factory=list)
    queued: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    verdicts: list[ClusterVerdict] = field(default_factory=list)


def _titles_for(store: GraphStore, member_ids) -> dict[str, str]:
    out: dict[str, str] = {}
    for mid in member_ids:
        node = store.get_node(mid)
        if node is not None:
            out[mid] = node.title
    return out


def build_authority_record(
    store: GraphStore,
    verdict: ClusterVerdict,
    cluster: CandidateCluster,
    *,
    judge_model: str = "",
    member_ids: tuple[str, ...] | None = None,
) -> AuthorityRecord:
    """Mint the off-graph equivalence record. When ``member_ids`` is given it scopes
    the record to that SUBSET (the per-variant corroborated survivors) so an
    uncorroborated co-member is never folded — both ``member_ids`` and the
    ``exact_match_pairs`` are derived from the subset, since the query-time fold
    (``AuthorityIndex.equivalence_map``) keys on ``member_ids``, not the pairs."""
    members = tuple(member_ids) if member_ids is not None else tuple(verdict.member_ids)
    titles = _titles_for(store, members)
    canonical_id = verdict.canonical_id
    variant_ids = [m for m in members if m != canonical_id]
    # A subset gets its own content-derived cluster_id so the auto-merged and queued
    # halves of one judged cluster never collide on the same key.
    cid = (
        verdict.cluster_id
        if member_ids is None or set(members) == set(verdict.member_ids)
        else cluster_id_for(members)
    )
    return AuthorityRecord(
        cluster_id=cid,
        canonical_id=canonical_id,
        canonical_name=titles.get(canonical_id, canonical_id),
        member_ids=members,
        variants=tuple(titles.get(m, m) for m in variant_ids),
        exact_match_pairs=tuple((canonical_id, m) for m in variant_ids),
        verdict="same",
        confidence=verdict.confidence,
        provenance={
            "agent": "marginalia-reconcile",
            "activity": "entity-reconciliation",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "lanes": sorted(cluster.lane_evidence.keys()),
            "judge_model": judge_model,
            "corroboration": verdict.corroboration,
        },
    )


def _enqueue_subset(
    store: GraphStore,
    cluster: CandidateCluster,
    verdict: ClusterVerdict,
    queue: ReconcileQueue,
    queue_ids: tuple[str, ...],
) -> str:
    """Queue the uncorroborated survivors (canonical + leftover) as their OWN
    sub-cluster with a content-derived id, so the auto-merged and queued halves of
    one judged cluster never collide on the same queue key. Returns the sub-id."""
    sub_cid = cluster_id_for(queue_ids)
    sub_cluster = CandidateCluster(
        cluster_id=sub_cid,
        type=cluster.type,
        member_ids=queue_ids,
        lane_evidence=dict(cluster.lane_evidence),
    )
    sub_verdict = ClusterVerdict(
        cluster_id=sub_cid,
        same=verdict.same,
        confidence=verdict.confidence,
        canonical_id=verdict.canonical_id,
        member_ids=queue_ids,
        corroboration="none",  # queued precisely because per-variant corroboration failed
        reason=verdict.reason,
        corroborated_ids=(),
    )
    titles = _titles_for(store, queue_ids)
    queue.enqueue(sub_cluster, sub_verdict, correlations={"titles": titles})
    return sub_cid


def is_high_confidence(verdict: ClusterVerdict) -> bool:
    """The auto-merge gate: same ∧ conf ≥ 0.9 ∧ positive corroboration.

    'Positive corroboration' is now evaluated PER-VARIANT: the gate is open iff at
    least one survivor is individually corroborated (``corroborated_ids`` holds the
    canonical + ≥1 corroborated variant). Cluster-level ``corroboration`` is kept
    for display, but the auto-merge decision no longer rides on a cluster-wide
    ``any()`` that could carry an uncorroborated co-member along."""
    return (
        verdict.same
        and verdict.confidence >= RECONCILE_AUTO_CONFIDENCE
        and len(verdict.corroborated_ids) >= 2
    )


def apply_reconciliation(
    store: GraphStore,
    *,
    embedder=None,
    judge: MergeJudge,
    authority: AuthorityIndex,
    queue: ReconcileQueue,
    type: str | None = None,
    use_cluster_judge: bool = False,
    judge_model: str = "",
    merge_blocked: Callable[[str, str], bool] | None = None,
) -> ApplyReport:
    """Reconcile the live graph OFF-GRAPH. NEVER calls ``store.add_node`` /
    ``store.add_edge`` — writes only the two JSON side-files."""
    clusters = generate_candidate_clusters(store, embedder=embedder, type=type)
    auto_merged: list[str] = []
    queued: list[str] = []
    skipped: list[str] = []
    verdicts: list[ClusterVerdict] = []

    for cluster in clusters:
        verdict = adjudicate_cluster(
            cluster,
            store,
            judge=judge,
            embedder=embedder,
            use_cluster_judge=use_cluster_judge,
            merge_blocked=merge_blocked,
        )
        verdicts.append(verdict)
        if not verdict.same:
            skipped.append(cluster.cluster_id)
            continue
        if is_high_confidence(verdict):
            # PARTITION: auto-merge ONLY the per-variant-corroborated subset; any
            # surviving co-member the judge called 'same' but that lacks its own
            # corroboration is queued for review rather than dragged into the merge.
            corroborated = set(verdict.corroborated_ids)
            rec = build_authority_record(
                store,
                verdict,
                cluster,
                judge_model=judge_model,
                member_ids=verdict.corroborated_ids,
            )
            authority.upsert(rec)  # OFF-GRAPH write
            auto_merged.append(rec.cluster_id)

            leftover = [m for m in verdict.member_ids if m not in corroborated]
            if leftover:
                queue_ids = tuple(sorted({verdict.canonical_id, *leftover}))
                queued.append(_enqueue_subset(store, cluster, verdict, queue, queue_ids))
        else:
            titles = _titles_for(store, verdict.member_ids)
            queue.enqueue(cluster, verdict, correlations={"titles": titles})  # OFF-GRAPH write
            queued.append(cluster.cluster_id)

    return ApplyReport(
        auto_merged=auto_merged,
        queued=queued,
        skipped=skipped,
        verdicts=verdicts,
    )


__all__ = ["ApplyReport", "apply_reconciliation", "is_high_confidence", "build_authority_record"]
