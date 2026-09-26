from __future__ import annotations

import gc
from pathlib import Path

import psutil
import pytest

from okto_neuron.core.schema import Edge, Node, Provenance
from okto_neuron.store.index import InMemoryIndexStore, reindex_all
from okto_neuron.store.ladybug import LadybugStore, VaultConnection


@pytest.fixture(autouse=True)
def close_vault_connections() -> None:
    yield
    VaultConnection.close_all()


@pytest.fixture
def store(tmp_path: Path) -> LadybugStore:
    handle = LadybugStore(tmp_path / "vault")
    yield handle
    handle.close()


def test_ladybug_store_round_trips_nodes_and_edges(store: LadybugStore) -> None:
    source = Node(
        id="node-source",
        type="Document",
        title="Source note",
        content="Okto Neuron stores local graphs.",
        tags=["local", "graph"],
        facets={"kind": "source"},
        provenance=Provenance(source="user", rule_id="test"),
    )
    target = Node(id="node-target", type="Document", title="Target note")
    edge = Edge(
        id="edge-source-target",
        type="references",
        src=source.id,
        dst=target.id,
        weight=0.75,
        provenance=Provenance(source="rule", rule_id="edge-test"),
    )

    store.add_node(source)
    store.add_node(target)
    store.add_edge(edge)

    assert store.get_node(source.id).model_dump(mode="json") == source.model_dump(mode="json")
    assert store.get_node(target.id).model_dump(mode="json") == target.model_dump(mode="json")
    assert [node.id for node in store.list_nodes(type="Document")] == [source.id, target.id]
    assert [stored.model_dump(mode="json") for stored in store.list_edges(src=source.id)] == [
        edge.model_dump(mode="json")
    ]
    assert [stored.id for stored in store.list_edges(dst=target.id, type="references")] == [edge.id]


def test_ladybug_store_search_text_bm25_ranks_exact_match_higher(
    store: LadybugStore,
) -> None:
    store.add_node(Node(id="exact", type="Document", title="Exact", content="alpha beta gamma"))
    store.add_node(Node(id="partial", type="Document", title="Partial", content="alpha beta"))

    index = InMemoryIndexStore()
    reindex_all(store, index)
    hits = index.search_text("alpha beta gamma", k=5)

    assert [node_id for node_id, _ in hits[:2]] == ["exact", "partial"]
    assert hits[0][1] > hits[1][1]


def test_ladybug_store_search_text_bm25_obeys_type_filter(store: LadybugStore) -> None:
    store.add_node(Node(id="note", type="Document", title="Note", content="shared needle"))
    store.add_node(Node(id="task", type="Activity", title="Task", content="shared needle"))

    index = InMemoryIndexStore()
    reindex_all(store, index)
    hits = index.search_text("needle", type="Activity")

    assert [node_id for node_id, _ in hits] == ["task"]


def test_ladybug_store_search_text_bm25_updates_stats_after_add(
    store: LadybugStore,
) -> None:
    store.add_node(Node(id="first", type="Document", title="First", content="alpha"))
    index = InMemoryIndexStore()
    reindex_all(store, index)
    first_score = index.search_text("alpha")[0][1]

    store.add_node(Node(id="second", type="Document", title="Second", content="alpha"))
    reindex_all(store, index)
    hits = index.search_text("alpha", k=5)

    assert {node_id for node_id, _ in hits} == {"first", "second"}
    assert next(score for node_id, score in hits if node_id == "first") < first_score


def test_ladybug_store_close_releases_file_descriptors(tmp_path: Path) -> None:
    process = psutil.Process()
    before = process.num_fds()
    store = LadybugStore(tmp_path / "vault")

    for index in range(20):
        store.add_node(Node(id=f"node-{index}", type="Concept", title=f"Node {index}"))

    store.close()
    gc.collect()

    assert process.num_fds() == before


def test_ladybug_store_100_node_smoke(store: LadybugStore) -> None:
    for index in range(100):
        store.add_node(
            Node(
                id=f"node-{index:03d}",
                type="Concept",
                title=f"Node {index}",
                content="bulk smoke test",
            )
        )

    nodes = list(store.list_nodes(type="Concept"))

    assert len(nodes) == 100
    assert nodes[0].id == "node-000"
    assert nodes[-1].id == "node-099"
