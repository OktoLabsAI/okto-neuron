"""GraphStore contract shared by every backend (M1: Ladybug and the in-memory double)."""

from __future__ import annotations

import pytest

from okto_neuron.core.schema import Edge, Node


def test_node_roundtrip_and_pinned_created_at(graph_store):
    node = Node(id="g1", type="Concept", title="alpha", content="first", tags=["t"])
    graph_store.add_node(node)
    stored = graph_store.get_node("g1")
    assert stored is not None and stored.title == "alpha"
    graph_store.add_node(Node(id="g1", type="Concept", title="alpha two", content="second"))
    again = graph_store.get_node("g1")
    assert again is not None and again.title == "alpha two"
    assert again.created_at == stored.created_at


def test_get_nodes_keeps_input_order_and_skips_missing(graph_store):
    for i in (3, 1, 2):
        graph_store.add_node(Node(id=f"n{i}", type="Claim", title=f"claim {i}"))
    got = graph_store.get_nodes(["n2", "missing", "n3", "n1", "n2"])
    assert [n.id for n in got] == ["n2", "n3", "n1"]
    assert graph_store.get_nodes([]) == []


def test_list_nodes_ascending_id(graph_store):
    for i in ("b", "c", "a"):
        graph_store.add_node(Node(id=f"id-{i}", type="Concept", title=i))
    assert [n.id for n in graph_store.list_nodes()] == ["id-a", "id-b", "id-c"]
    assert [n.id for n in graph_store.list_nodes(type="Claim")] == []


def test_add_edge_requires_endpoints_and_stable_identity(graph_store):
    graph_store.add_node(Node(id="s", type="Concept", title="s"))
    graph_store.add_node(Node(id="d", type="Concept", title="d"))
    with pytest.raises(ValueError):
        graph_store.add_edge(Edge(id="e1", type="mentions", src="s", dst="ghost"))
    graph_store.add_edge(Edge(id="e1", type="mentions", src="s", dst="d"))
    with pytest.raises(ValueError):
        graph_store.add_edge(Edge(id="e1", type="references", src="s", dst="d"))
    assert [e.id for e in graph_store.list_edges(src="s")] == ["e1"]


def test_rejects_off_schema_type(graph_store):
    with pytest.raises(ValueError):
        graph_store.add_node(Node(id="x", type="Widget", title="no"))
