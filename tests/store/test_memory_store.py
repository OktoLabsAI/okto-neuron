"""Regression tests for InMemoryStore parity with LadybugStore invariants.

Covers deep-review findings 3.14 (edge-identity immutability and created_at
preservation), 3.26 (deterministic list ordering), and 3.27 (embedding
truthiness vs. IS NOT NULL semantics).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from okto_neuron.core.schema import Edge, Node
from okto_neuron.store.index import InMemoryIndexStore, reindex_all
from okto_neuron.store.memory import InMemoryStore


def _node(node_id: str, **overrides: object) -> Node:
    fields: dict[str, object] = {
        "id": node_id,
        "type": "Document",
        "title": f"title-{node_id}",
    }
    fields.update(overrides)
    return Node(**fields)


def test_add_node_preserves_created_at_on_reingest() -> None:
    """3.14: re-adding a node id must not regenerate created_at (mirrors
    LadybugStore.add_node, which always keeps the existing created_at)."""
    store = InMemoryStore()
    original_created_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
    store.add_node(_node("n1", created_at=original_created_at))

    later = original_created_at + timedelta(days=5)
    store.add_node(_node("n1", title="updated title", created_at=later))

    stored = store.get_node("n1")
    assert stored is not None
    assert stored.created_at == original_created_at
    assert stored.title == "updated title"


def test_add_edge_rejects_id_reuse_across_different_identity() -> None:
    """3.14: LadybugStore.add_edge raises ValueError when an existing edge id
    is reused for a different (type, src, dst). InMemoryStore silently
    overwrote instead; port the guard."""
    store = InMemoryStore()
    store.add_node(_node("a"))
    store.add_node(_node("b"))
    store.add_node(_node("c"))

    store.add_edge(Edge(id="e1", type="references", src="a", dst="b"))

    with pytest.raises(ValueError, match="edge identity collision"):
        store.add_edge(Edge(id="e1", type="references", src="a", dst="c"))

    # The original edge must be untouched.
    (kept,) = list(store.list_edges(src="a", dst="b"))
    assert kept.id == "e1"
    assert list(store.list_edges(src="a", dst="c")) == []


def test_add_edge_allows_same_identity_payload_update() -> None:
    """Re-adding an edge id with the same (type, src, dst) but a changed
    payload (e.g. weight) is a legitimate update, not a collision."""
    store = InMemoryStore()
    store.add_node(_node("a"))
    store.add_node(_node("b"))

    store.add_edge(Edge(id="e1", type="references", src="a", dst="b", weight=1.0))
    store.add_edge(Edge(id="e1", type="references", src="a", dst="b", weight=0.5))

    (kept,) = list(store.list_edges(src="a"))
    assert kept.weight == 0.5


def test_list_nodes_and_list_edges_are_sorted_by_id() -> None:
    """3.26: LadybugStore returns nodes/edges sorted by id; InMemoryStore
    previously used dict insertion order, which is fragile against tie-break
    flips wherever a caller (or a test) inserts in a different order than the
    deterministic id order expects."""
    store = InMemoryStore()
    # Insert deliberately out of lexicographic id order.
    for node_id in ("charlie", "alpha", "bravo"):
        store.add_node(_node(node_id))

    assert [n.id for n in store.list_nodes()] == ["alpha", "bravo", "charlie"]

    store.add_edge(Edge(id="z-edge", type="references", src="charlie", dst="alpha"))
    store.add_edge(Edge(id="a-edge", type="references", src="charlie", dst="alpha"))
    store.add_edge(Edge(id="m-edge", type="references", src="charlie", dst="alpha"))

    assert [e.id for e in store.list_edges()] == ["a-edge", "m-edge", "z-edge"]


def test_nodes_with_embeddings_includes_empty_list_embedding() -> None:
    """3.27: LadybugStore's Cypher filter is `n.embedding IS NOT NULL`, which
    includes an empty-list embedding. InMemoryStore's `if n.embedding:` truthy
    check silently excluded it — a real, if currently unreachable, protocol
    divergence."""
    store = InMemoryStore()
    store.add_node(_node("has-empty-embedding", embedding=[]))
    store.add_node(_node("has-none-embedding", embedding=None))
    store.add_node(_node("has-real-embedding", embedding=[0.1, 0.2]))

    index = InMemoryIndexStore()
    reindex_all(store, index)
    result_ids = {node_id for node_id, _ in index.scan_vectors()}
    assert result_ids == {"has-empty-embedding", "has-real-embedding"}
