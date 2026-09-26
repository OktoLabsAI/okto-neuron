"""ReconcileQueue: confirm writes off-graph + dequeues; NO graph write possible."""

from __future__ import annotations

from okto_neuron.reconcile.authority import AuthorityIndex
from okto_neuron.reconcile.candidates import CandidateCluster
from okto_neuron.reconcile.propose import ClusterVerdict
from okto_neuron.reconcile.queue import ReconcileQueue


def _cluster() -> CandidateCluster:
    return CandidateCluster(
        cluster_id="cq1",
        type="Agent",
        member_ids=("n-canon", "n-var"),
        lane_evidence={"lexical": ["a <-> b"]},
    )


def _verdict() -> ClusterVerdict:
    return ClusterVerdict(
        cluster_id="cq1",
        same=True,
        confidence=0.85,  # below auto floor → would be queued
        canonical_id="n-canon",
        member_ids=("n-canon", "n-var"),
        corroboration="lexical",
        reason="probable",
    )


def test_enqueue_persists_and_reloads(tmp_path):
    auth = AuthorityIndex(tmp_path / "authority")
    q = ReconcileQueue(tmp_path / "reconcile", auth)
    q.enqueue(_cluster(), _verdict(), correlations={"titles": {"n-canon": "Alex", "n-var": "alex"}})
    assert len(q) == 1
    # reload from disk
    q2 = ReconcileQueue(tmp_path / "reconcile", AuthorityIndex(tmp_path / "authority"))
    assert len(q2) == 1
    assert q2.get("cq1") is not None


def test_confirm_writes_authority_offgraph_and_dequeues(tmp_path):
    auth = AuthorityIndex(tmp_path / "authority")
    q = ReconcileQueue(tmp_path / "reconcile", auth)
    q.enqueue(_cluster(), _verdict(), correlations={"titles": {"n-canon": "Alex", "n-var": "alex"}})
    rec = q.confirm("cq1")
    assert rec.canonical_id == "n-canon"
    assert rec.canonical_name == "Alex"
    assert "n-var" in rec.member_ids
    assert rec.exact_match_pairs == (("n-canon", "n-var"),)
    # dequeued
    assert len(q) == 0
    # authority got the off-graph record
    assert len(auth.records()) == 1
    assert auth.equivalence_map()["n-var"] == "n-canon"


def test_reject_dequeues_without_authority_write(tmp_path):
    auth = AuthorityIndex(tmp_path / "authority")
    q = ReconcileQueue(tmp_path / "reconcile", auth)
    q.enqueue(_cluster(), _verdict())
    q.reject("cq1")
    assert len(q) == 0
    assert auth.records() == []


def test_queue_has_no_store_reference():
    # Structural proof: a graph write is impossible by construction — the queue
    # dataclass has no `store` field at all.
    fields = {f for f in ReconcileQueue.__dataclass_fields__}
    assert "store" not in fields
    assert "authority" in fields
