"""``IndexedStore.write_seq`` moves on EVERY mutating store method.

Derived projections (``server/_projection.py``) are current exactly while
``(generation, instance_token, write_seq)`` is unchanged, so a mutator that forgets to bump the
counter silently freezes them. The enumeration below reads the ``GraphStore`` protocol itself
and demands that every public member is classified as read or mutating: a NEW protocol method
fails here until someone decides which it is.
"""

from __future__ import annotations

import inspect

import pytest

from okto_neuron.core.schema import Edge, Node
from okto_neuron.store.index import InMemoryIndexStore
from okto_neuron.store.index.indexed import _BACKEND_MUTATORS, IndexedStore
from okto_neuron.store.memory import InMemoryStore
from okto_neuron.store.protocol import GraphStore

# Protocol members that never change stored data. close/checkpoint move bytes, not content.
_READ_ONLY = frozenset(
    {
        "get_node", "get_nodes", "list_nodes", "list_edges", "generation", "health",
        "recovery_status", "detect_drift", "is_closed", "checkpoint", "close",
    }
)
_MUTATING = frozenset({"add_node", "add_edge"})


def _protocol_members() -> set[str]:
    return {
        name
        for name, value in vars(GraphStore).items()
        if not name.startswith("_") and (inspect.isfunction(value) or isinstance(value, property))
    }


def test_every_protocol_member_is_classified() -> None:
    members = _protocol_members()
    unclassified = members - _READ_ONLY - _MUTATING
    assert not unclassified, (
        f"new GraphStore members {sorted(unclassified)}: add them to _READ_ONLY or _MUTATING "
        "and make IndexedStore bump write_seq if they mutate"
    )
    assert _MUTATING <= members and _READ_ONLY <= members, "stale entry in the allow-lists"


def _store() -> IndexedStore:
    s = IndexedStore(InMemoryStore(), InMemoryIndexStore())
    s.add_node(Node(id="a", type="Concept", title="a"))
    s.add_node(Node(id="b", type="Concept", title="b"))
    return s


def test_each_mutating_method_bumps_write_seq() -> None:
    s = _store()
    calls = {
        "add_node": lambda: s.add_node(Node(id="c", type="Concept", title="c")),
        "add_edge": lambda: s.add_edge(Edge(id="e1", type="mentions", src="a", dst="b")),
    }
    assert set(calls) == _MUTATING
    for name, call in calls.items():
        before = s.write_seq
        call()
        assert s.write_seq == before + 1, f"{name} did not bump write_seq exactly once"


def test_read_methods_do_not_bump_write_seq() -> None:
    s = _store()
    before = s.write_seq
    s.get_node("a")
    s.get_nodes(["a", "b"])
    list(s.list_nodes())
    list(s.list_edges())
    s.generation()
    s.health()
    s.recovery_status()
    s.detect_drift(None)
    _ = s.is_closed
    s.checkpoint()
    assert s.write_seq == before


def test_a_failed_write_still_bumps() -> None:
    s = _store()
    before = s.write_seq
    with pytest.raises(ValueError):
        s.add_edge(Edge(id="bad", type="mentions", src="a", dst="missing"))
    assert s.write_seq == before + 1  # it may have applied partially: readers must re-scan


def test_backend_bulk_mutators_reached_through_getattr_bump_too() -> None:
    class _Inner(InMemoryStore):
        def add_nodes(self, nodes):
            for node in nodes:
                self.add_node(node)

        def add_edges(self, edges):
            for edge in edges:
                self.add_edge(edge)

        def wipe(self) -> None:
            self._nodes.clear()
            self._edges.clear()

    s = IndexedStore(_Inner(), InMemoryIndexStore())
    calls = {
        "add_nodes": lambda: s.add_nodes([Node(id="x", type="Concept", title="x")]),
        "add_edges": lambda: s.add_edges([]),
        "wipe": lambda: s.wipe(),
    }
    assert set(calls) == _BACKEND_MUTATORS
    for name, call in calls.items():
        before = s.write_seq
        call()
        assert s.write_seq == before + 1, name


def test_instance_token_is_unique_per_store_object() -> None:
    a, b = _store(), _store()
    assert a.instance_token != b.instance_token
    assert a.instance_token == a.instance_token
