"""ADR 0040 hard invariant — the store write boundary refuses off-schema types.

The closed set was previously honoured only by convention on the write side and
re-checked on the read side (``server/http.py`` validates ``?type=``), so a buggy
writer could persist a node outside the schema and only be caught at query time.
Both store implementations now fail closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_neuron.core.schema import Node
from okto_neuron.store.closed_set import (
    CLOSED_NODE_TYPES,
    INTERNAL_NODE_TYPES,
    WRITABLE_NODE_TYPES,
    require_writable_node_type,
)
from okto_neuron.store.ladybug import LadybugStore, VaultConnection
from okto_neuron.store.memory import InMemoryStore


@pytest.fixture(autouse=True)
def close_vault_connections():
    yield
    VaultConnection.close_all()


@pytest.fixture
def ladybug_store(tmp_path: Path) -> LadybugStore:
    handle = LadybugStore(tmp_path / "vault")
    yield handle
    handle.close()


def test_closed_set_is_the_locked_eleven_plus_internal_bookkeeping() -> None:
    assert CLOSED_NODE_TYPES == {
        "Agent",
        "Activity",
        "InformationObject",
        "Concept",
        "Place",
        "Document",
        "Identifier",
        "Annotation",
        "Claim",
        "Block",
        "Finding",
    }
    assert INTERNAL_NODE_TYPES == {"SchemaMetadata"}
    assert WRITABLE_NODE_TYPES == CLOSED_NODE_TYPES | INTERNAL_NODE_TYPES


@pytest.mark.parametrize(
    "bad_type", ["Note", "Question", "Person", "Organization", "", "concept", "Asset"]
)
def test_require_writable_node_type_refuses_off_schema(bad_type: str) -> None:
    with pytest.raises(ValueError, match="outside the closed schema"):
        require_writable_node_type(bad_type)


@pytest.mark.parametrize("good_type", sorted(WRITABLE_NODE_TYPES))
def test_require_writable_node_type_admits_every_writable_type(good_type: str) -> None:
    require_writable_node_type(good_type)


def test_memory_store_refuses_off_schema_node() -> None:
    store = InMemoryStore()
    with pytest.raises(ValueError, match="outside the closed schema"):
        store.add_node(Node(id="x", type="Note", title="off-schema"))
    assert store.get_node("x") is None


def test_ladybug_store_refuses_off_schema_node(ladybug_store: LadybugStore) -> None:
    with pytest.raises(ValueError, match="outside the closed schema"):
        ladybug_store.add_node(Node(id="x", type="Note", title="off-schema"))
    assert ladybug_store.get_node("x") is None


def test_ladybug_store_checks_openness_before_the_schema_guard(tmp_path: Path) -> None:
    """Ordering matters: a closed store reports *closed*, not a schema violation,
    so a shutdown race is never misdiagnosed as corruption."""
    store = LadybugStore(tmp_path / "vault")
    store.close()
    with pytest.raises(RuntimeError, match="LadybugStore is closed"):
        store.add_node(Node(id="x", type="Note", title="off-schema"))


def test_internal_schema_metadata_row_stays_writable() -> None:
    """``store/schema.py`` mints ``SchemaMetadata`` with raw Cypher, so it is a
    real row ``list_nodes`` yields — the rebuild/reembed copy paths hand it back
    to ``add_node`` and must not be refused."""
    store = InMemoryStore()
    node = Node(id="__meta__", type="SchemaMetadata", title="internal")
    store.add_node(node)
    assert store.get_node("__meta__") is node


def test_every_closed_type_round_trips_through_both_stores(
    ladybug_store: LadybugStore,
) -> None:
    memory = InMemoryStore()
    for node_type in sorted(WRITABLE_NODE_TYPES):
        node = Node(id=f"n-{node_type}", type=node_type, title=node_type)
        memory.add_node(node)
        ladybug_store.add_node(node)
    for node_type in sorted(WRITABLE_NODE_TYPES):
        assert memory.get_node(f"n-{node_type}") is not None
        assert ladybug_store.get_node(f"n-{node_type}") is not None
