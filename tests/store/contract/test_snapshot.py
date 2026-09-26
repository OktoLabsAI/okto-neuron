"""snapshot.py contract: dump / verify / load round trip, shared by every backend.

Mirrors the M2a spec (the internal ADR 0041 plan, M2a task) section 5's
first test block: round trip, tamper, skip_embeddings, idempotent load, and the
edges-before-nodes ValueError. Runs against both graph_store parametrizations
(ladybug, memory) via the shared contract conftest fixtures.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from okto_neuron import __version__ as OKTO_NEURON_VERSION
from okto_neuron.core.schema import Edge, Node
from okto_neuron.store.index import InMemoryIndexStore, reindex_all
from okto_neuron.store.ladybug import LadybugStore
from okto_neuron.store.memory import InMemoryStore
from okto_neuron.store.schema import CURRENT_SCHEMA_VERSION
from okto_neuron.store.snapshot import dump, load, verify

_EMBEDDING_META = {"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5", "dimension": 384}


def _dump_kwargs(store: object) -> dict:
    return dict(
        vault_id="origin-vault",
        origin_backend="ladybug" if isinstance(store, LadybugStore) else "memory",
        origin_identity=(None, None),
        embedding=dict(_EMBEDDING_META),
        packs=[],
        sources_dir=None,
        marginalia_version=OKTO_NEURON_VERSION,
        schema_version=CURRENT_SCHEMA_VERSION,
        embedding_dim=384,
    )


def _assert_nodes_equal(a: Node, b: Node) -> None:
    assert a.id == b.id
    assert a.type == b.type
    assert a.title == b.title
    assert a.content == b.content
    assert a.tags == b.tags
    assert a.facets == b.facets
    assert a.provenance == b.provenance
    assert a.created_at == b.created_at
    assert a.embedding == b.embedding


def _assert_edges_equal(a: Edge, b: Edge) -> None:
    assert a.id == b.id
    assert a.type == b.type
    assert a.src == b.src
    assert a.dst == b.dst
    assert a.weight == b.weight
    assert a.provenance == b.provenance


@pytest.fixture
def target_store(graph_store, tmp_path_factory):
    """A fresh, empty store of the same kind as `graph_store` -- the load target.

    conftest's `graph_store` fixture builds each kind inline (no reusable
    factory is exposed), so this mirrors that same construction, keyed off the
    already-built instance's type rather than re-declaring the ["ladybug",
    "memory"] param list (which would cross-product needlessly).
    """
    if isinstance(graph_store, LadybugStore):
        store = LadybugStore(tmp_path_factory.mktemp("target") / "vault")
    else:
        store = InMemoryStore()
    yield store
    store.close()


def test_round_trip_preserves_nodes_and_edges(populated, target_store, corpus_rows, tmp_path):
    store = populated
    node_ids = sorted(row["id"] for row in corpus_rows)
    edges = [
        Edge(id="edge-0001", type="mentions", src=node_ids[0], dst=node_ids[1]),
        Edge(id="edge-0002", type="relates_to", src=node_ids[2], dst=node_ids[3], weight=0.5),
        Edge(id="edge-0003", type="mentions", src=node_ids[4], dst=node_ids[0]),
    ]
    for edge in edges:
        store.add_edge(edge)
    store.checkpoint()

    expected_node_count = len(corpus_rows)
    expected_embedded_count = sum(1 for row in corpus_rows if row.get("embedding") is not None)

    dest = tmp_path / "snapshot"
    manifest = dump(store, dest, **_dump_kwargs(store))
    assert manifest.node_count == expected_node_count
    assert manifest.edge_count == len(edges)
    assert manifest.embedded_count == expected_embedded_count

    report = verify(dest)
    assert report.ok, report.problems

    load_report = load(target_store, dest)
    assert load_report.nodes_written == expected_node_count
    assert load_report.edges_written == len(edges)
    assert load_report.embeddings_applied == expected_embedded_count
    assert load_report.skipped_embeddings is False

    origin_nodes = {node.id: node for node in store.list_nodes()}
    loaded_nodes = {node.id: node for node in target_store.list_nodes()}
    assert set(origin_nodes) == set(loaded_nodes)
    for node_id, origin_node in origin_nodes.items():
        _assert_nodes_equal(origin_node, loaded_nodes[node_id])

    origin_edges = {edge.id: edge for edge in store.list_edges()}
    loaded_edges = {edge.id: edge for edge in target_store.list_edges()}
    assert set(origin_edges) == set(loaded_edges)
    for edge_id, origin_edge in origin_edges.items():
        _assert_edges_equal(origin_edge, loaded_edges[edge_id])


def test_tamper_flips_verify_and_load(populated, target_store, tmp_path):
    store = populated
    dest = tmp_path / "snapshot"
    dump(store, dest, **_dump_kwargs(store))

    nodes_path = dest / "nodes.jsonl"
    tampered = bytearray(nodes_path.read_bytes())
    assert tampered, "nodes.jsonl must not be empty for the tamper test to mean anything"
    tampered[0] ^= 0xFF
    nodes_path.write_bytes(bytes(tampered))

    report = verify(dest)
    assert not report.ok
    assert report.problems

    with pytest.raises(ValueError):
        load(target_store, dest)


def test_skip_embeddings_loads_without_vectors(populated, target_store, tmp_path):
    store = populated
    dest = tmp_path / "snapshot"
    dump(store, dest, **_dump_kwargs(store))

    load_report = load(target_store, dest, skip_embeddings=True)
    assert load_report.skipped_embeddings is True
    assert load_report.embeddings_applied == 0

    loaded_nodes = list(target_store.list_nodes())
    assert loaded_nodes, "expected the corpus nodes to be loaded"
    assert all(node.embedding is None for node in loaded_nodes)

    index = InMemoryIndexStore()
    reindex_all(target_store, index)
    assert index.stats().embedded_count == 0


def test_load_twice_is_idempotent(populated, target_store, corpus_rows, tmp_path):
    store = populated
    node_ids = sorted(row["id"] for row in corpus_rows)
    edge = Edge(id="edge-idem-0001", type="mentions", src=node_ids[0], dst=node_ids[1])
    store.add_edge(edge)
    store.checkpoint()

    dest = tmp_path / "snapshot"
    dump(store, dest, **_dump_kwargs(store))

    load(target_store, dest)
    first_nodes = {node.id: node for node in target_store.list_nodes()}
    first_edges = {edge.id: edge for edge in target_store.list_edges()}

    load(target_store, dest)
    second_nodes = {node.id: node for node in target_store.list_nodes()}
    second_edges = {edge.id: edge for edge in target_store.list_edges()}

    assert set(first_nodes) == set(second_nodes)
    for node_id in first_nodes:
        _assert_nodes_equal(first_nodes[node_id], second_nodes[node_id])
        assert first_nodes[node_id].created_at == second_nodes[node_id].created_at

    assert set(first_edges) == set(second_edges)
    for edge_id in first_edges:
        _assert_edges_equal(first_edges[edge_id], second_edges[edge_id])


def test_load_raises_on_dangling_edge_and_leaves_partial_graph(populated, target_store, tmp_path):
    """A well-formed (checksum-valid) snapshot can still carry a dangling edge.

    `verify()` only recomputes checksums/counts (spec section 3); it never
    resolves an edge's src/dst against nodes.jsonl. Only `load()` does, via
    the store's own `add_edge` invariant. A real `dump()` can never emit this
    on its own -- both backends refuse `add_edge` for missing endpoints at
    write time -- so this hand-edits an otherwise-valid dump to drop one
    node's row while keeping the checksums/manifest internally consistent,
    to exercise that defensive path.
    """
    store = populated
    ids = sorted(node.id for node in store.list_nodes() if node.embedding is None)
    missing_id = ids[0]
    survivors = [node.id for node in store.list_nodes() if node.id != missing_id]
    good_src, good_dst = survivors[0], survivors[1]

    # Ascending ids so edges.jsonl (spec: "ascending id") keeps the good edge
    # before the dangling one -- load() must apply it and stop at the next row.
    good_edge = Edge(id="aaa-good-edge", type="mentions", src=good_src, dst=good_dst)
    dangling_edge = Edge(id="bbb-bad-edge", type="mentions", src=good_src, dst=missing_id)
    store.add_edge(good_edge)
    store.add_edge(dangling_edge)
    store.checkpoint()

    dest = tmp_path / "snapshot"
    dump(store, dest, **_dump_kwargs(store))

    nodes_path = dest / "nodes.jsonl"
    lines = nodes_path.read_text(encoding="utf-8").splitlines()
    kept_lines = [line for line in lines if json.loads(line)["id"] != missing_id]
    assert len(kept_lines) == len(lines) - 1
    new_nodes_bytes = ("\n".join(kept_lines) + "\n").encode("utf-8")
    nodes_path.write_bytes(new_nodes_bytes)

    edges_bytes = (dest / "edges.jsonl").read_bytes()
    embeddings_bytes = (dest / "embeddings.jsonl").read_bytes()

    checksums_path = dest / "CHECKSUMS.sha256"
    new_checksum_lines = []
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        _, rel_path = line.split(None, 1)
        if rel_path == "nodes.jsonl":
            new_checksum_lines.append(f"{hashlib.sha256(new_nodes_bytes).hexdigest()}  nodes.jsonl")
        else:
            new_checksum_lines.append(line)
    checksums_path.write_text("\n".join(new_checksum_lines) + "\n", encoding="utf-8")

    manifest_path = dest / "manifest.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_data["node_count"] = len(kept_lines)
    digest = hashlib.sha256()
    digest.update(new_nodes_bytes)
    digest.update(edges_bytes)
    digest.update(embeddings_bytes)
    manifest_data["content_sha256"] = digest.hexdigest()
    manifest_path.write_text(json.dumps(manifest_data, sort_keys=True), encoding="utf-8")

    report = verify(dest)
    assert report.ok, report.problems  # the hand-edit above must stay self-consistent

    with pytest.raises(ValueError):
        load(target_store, dest)

    partial_ids = {node.id for node in target_store.list_nodes()}
    assert missing_id not in partial_ids
    assert partial_ids == set(node["id"] for node in (json.loads(line) for line in kept_lines))

    partial_edges = list(target_store.list_edges())
    assert [edge.id for edge in partial_edges] == [good_edge.id]
