"""THE headline safety test: Option A apply writes ZERO graph nodes/edges.

ADR-0007 proves a bulk add_node/add_edge into a populated Ladybug graph corrupts
edge src/dst + adjacency. Authority is a Node subclass, so an Authority add_node
IS that corruption. This test wraps the actual store instance and asserts BOTH
counters stay 0 across the WHOLE apply_reconciliation run (adjudication +
authority.upsert + queue.enqueue)."""

from __future__ import annotations

from okto_neuron.core.schema import Node
from okto_neuron.reconcile.apply import apply_reconciliation
from okto_neuron.reconcile.authority import AuthorityIndex
from okto_neuron.reconcile.queue import ReconcileQueue
from okto_neuron.resolve import MergeVerdict
from okto_neuron.store.memory import InMemoryStore


class SpyStore(InMemoryStore):
    """InMemoryStore that counts EVERY add_node / add_edge call."""

    def __init__(self):
        super().__init__()
        self.add_node_calls = 0
        self.add_edge_calls = 0

    def add_node(self, node):
        self.add_node_calls += 1
        super().add_node(node)

    def add_edge(self, edge):
        self.add_edge_calls += 1
        super().add_edge(edge)


class StubJudge:
    def __init__(self, same, confidence):
        self._v = MergeVerdict(same=same, confidence=confidence)

    def judge(self, candidate, existing, *, candidate_context="", existing_context=""):
        return self._v


def _seed(store):
    # A high-confidence mergeable pair (auto-merge bucket) + a low-confidence pair
    # (queue bucket) + a distinct pair (skip bucket) — exercises all three splits.
    store.add_node(Node(id="ed-canon", type="Agent", title="Taylor Nguyen", content=""))
    store.add_node(Node(id="ed-var", type="Agent", title="Taylor", content=""))


def test_apply_writes_zero_graph_nodes_and_edges(tmp_path):
    store = SpyStore()
    _seed(store)
    # baseline writes from seeding (we count only DURING apply)
    store.add_node_calls = 0
    store.add_edge_calls = 0

    auth = AuthorityIndex(tmp_path / "authority")
    queue = ReconcileQueue(tmp_path / "reconcile", auth)
    judge = StubJudge(same=True, confidence=0.95)

    report = apply_reconciliation(store, embedder=None, judge=judge, authority=auth, queue=queue)

    # THE assertions: nothing was written to the graph.
    assert store.add_node_calls == 0, "Option A must NEVER add a graph node"
    assert store.add_edge_calls == 0, "Option A must NEVER add a graph edge"

    # And the off-graph side-data WAS written (the auto-merge landed).
    assert report.auto_merged, "expected a high-confidence auto-merge"
    assert len(auth.records()) == 1
    assert auth.equivalence_map().get("ed-var") == "ed-canon"


def test_apply_low_confidence_queues_still_zero_write(tmp_path):
    store = SpyStore()
    _seed(store)
    store.add_node_calls = 0
    store.add_edge_calls = 0

    auth = AuthorityIndex(tmp_path / "authority")
    queue = ReconcileQueue(tmp_path / "reconcile", auth)
    judge = StubJudge(same=True, confidence=0.82)  # below auto floor → queue

    report = apply_reconciliation(store, embedder=None, judge=judge, authority=auth, queue=queue)

    assert store.add_node_calls == 0
    assert store.add_edge_calls == 0
    assert report.queued
    assert len(queue) == 1
    assert auth.records() == []  # nothing auto-merged


def test_apply_distinct_skips_zero_write(tmp_path):
    store = SpyStore()
    store.add_node(Node(id="a", type="Agent", title="Aler Dalvic", content=""))
    store.add_node(Node(id="b", type="Agent", title="Bob Smith", content=""))
    store.add_node_calls = 0
    store.add_edge_calls = 0

    auth = AuthorityIndex(tmp_path / "authority")
    queue = ReconcileQueue(tmp_path / "reconcile", auth)
    judge = StubJudge(same=False, confidence=0.0)

    apply_reconciliation(store, embedder=None, judge=judge, authority=auth, queue=queue)
    assert store.add_node_calls == 0
    assert store.add_edge_calls == 0
    assert auth.records() == []
    assert len(queue) == 0
