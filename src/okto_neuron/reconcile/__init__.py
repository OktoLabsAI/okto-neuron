"""Retroactive entity reconciliation (v0.0.5, ADR 0008).

Consolidates already-committed look-alike entities AFTER they have landed in the
graph — the retroactive sibling of ``resolve``'s pre-commit dedup. Two apply
paths:

- **Option A (default, off-graph)**: ``apply_reconciliation`` writes equivalence
  classes to an off-graph :class:`AuthorityIndex` (``skos:exactMatch``); the read
  chokepoint (``query.search_claims`` via ``vault.query``) folds variants onto the
  canonical at query time. Writes NOTHING to the graph (the ADR-0007 safety
  property), reversible by dropping the index entry.
- **Heal (ADR 0009 P3)**: ``heal_via_copy`` materializes the confirmed equivalences
  into the graph via a deterministic no-LLM graph→fresh-graph copy + atomic swap —
  never an in-place edge mint (ADR-0007-safe), and never re-running the LLM.

This package CONSUMES ``resolve`` primitives (``LLMMergeJudge``,
``_store_relationships``, …); it does not reinvent them.
"""

from __future__ import annotations

from okto_neuron.reconcile.apply import (
    ApplyReport,
    apply_reconciliation,
    build_authority_record,
    is_high_confidence,
)
from okto_neuron.reconcile.authority import (
    AuthorityIndex,
    AuthorityRecord,
)
from okto_neuron.reconcile.candidates import (
    JW_STRONG,
    RECONCILE_CLASS_CAP,
    RECONCILE_RECALL_FLOOR,
    CandidateCluster,
    email_handle_tokens,
    generate_candidate_clusters,
    jaro_winkler,
)
from okto_neuron.reconcile.decisions import (
    AmbiguousReview,
    DistinctDecision,
    IdentityDecisionIndex,
    IdentityDecisionStoreError,
    TypeCorrection,
)
from okto_neuron.reconcile.heal import (
    heal_via_copy,
)
from okto_neuron.reconcile.propose import (
    RECONCILE_AUTO_CONFIDENCE,
    REL_JACCARD,
    ClusterVerdict,
    adjudicate_cluster,
    select_canonical,
)
from okto_neuron.reconcile.queue import QueuedCluster, ReconcileQueue

__all__ = [
    "generate_candidate_clusters",
    "CandidateCluster",
    "jaro_winkler",
    "email_handle_tokens",
    "AuthorityIndex",
    "AuthorityRecord",
    "IdentityDecisionIndex",
    "IdentityDecisionStoreError",
    "TypeCorrection",
    "DistinctDecision",
    "AmbiguousReview",
    "ReconcileQueue",
    "QueuedCluster",
    "adjudicate_cluster",
    "ClusterVerdict",
    "select_canonical",
    "apply_reconciliation",
    "ApplyReport",
    "is_high_confidence",
    "build_authority_record",
    "heal_via_copy",
    "RECONCILE_RECALL_FLOOR",
    "RECONCILE_AUTO_CONFIDENCE",
    "RECONCILE_CLASS_CAP",
    "JW_STRONG",
    "REL_JACCARD",
]
