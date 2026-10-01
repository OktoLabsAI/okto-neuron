"""Auto-rebuild on open: a vault opened through the store opener carries a consistent index."""

from __future__ import annotations

import json
from pathlib import Path

from okto_neuron.core.schema import Node
from okto_neuron.store.index import compute_graph_generation
from okto_neuron.store.index.indexed import IndexedStore
from okto_neuron.store.ladybug import LadybugStore
from okto_neuron.store.vault import _open_vault


def _index_dir(vault: Path) -> Path:
    return vault / ".marginalia" / "index"


def test_open_builds_index_and_reopen_is_consistent(tmp_path: Path):
    vault = tmp_path / "vault"
    store = _open_vault(vault)
    assert isinstance(store, IndexedStore)
    store.add_node(Node(id="a1", type="Concept", title="graph store", content="ladybug"))
    store.checkpoint()
    assert (_index_dir(vault) / "meta.json").exists()
    assert store.index.search_text("graph", k=5)[0][0] == "a1"
    assert store.index.generation() == compute_graph_generation(store.graph)
    store.close()

    reopened = _open_vault(vault)
    assert reopened is not store
    assert reopened.index.search_text("graph", k=5)[0][0] == "a1"
    reopened.close()


def test_stale_index_is_rebuilt_on_open(tmp_path: Path):
    vault = tmp_path / "vault"
    store = _open_vault(vault)
    store.add_node(Node(id="a1", type="Concept", title="graph store"))
    store.checkpoint()
    store.close()

    # Bypass the index: write straight to the graph, as kg rebuild, reembed and heal do.
    raw = LadybugStore(vault)
    raw.add_node(Node(id="a2", type="Concept", title="vector index"))
    raw.checkpoint()
    raw.close()
    stale = json.loads((_index_dir(vault) / "meta.json").read_text())["graph_generation"]

    reopened = _open_vault(vault)
    assert reopened.index.generation() != stale
    assert reopened.index.generation() == compute_graph_generation(reopened.graph)
    assert [i for i, _ in reopened.index.search_text("vector", k=5)] == ["a2"]
    reopened.close()


def _spy_reindex(monkeypatch) -> list[int]:
    from okto_neuron.store import vault as vault_module

    calls: list[int] = []
    real = vault_module.reindex_all

    def spy(store, index):
        calls.append(1)
        return real(store, index)

    monkeypatch.setattr(vault_module, "reindex_all", spy)
    return calls


def test_open_digests_the_graph_once(tmp_path: Path, monkeypatch):
    from okto_neuron.store.index import corpus

    vault = tmp_path / "vault"
    store = _open_vault(vault)
    store.add_node(Node(id="a1", type="Concept", title="graph store", embedding=[0.1] * 384))
    store.add_node(Node(id="a2", type="Concept", title="vector index"))
    store.close()

    digested: list[str] = []
    real = corpus.node_digest

    def spy(node):
        digested.append(node.id)
        return real(node)

    monkeypatch.setattr(corpus, "node_digest", spy)
    reopened = _open_vault(vault)
    node_count = len(list(reopened.graph.list_nodes()))
    reopened.close()
    # One pass over the graph; the index answers from its stored stamp.
    assert len(digested) == node_count


def test_stored_stamp_is_trusted_and_recompute_is_the_self_check(tmp_path: Path, monkeypatch):
    vault = tmp_path / "vault"
    store = _open_vault(vault)
    store.add_node(Node(id="a1", type="Concept", title="graph store"))
    store.close()

    corpus_path = _index_dir(vault) / "corpus.jsonl"
    corpus_path.write_text(corpus_path.read_text().replace("graph store", "tampered"))

    rebuilds = _spy_reindex(monkeypatch)
    reopened = _open_vault(vault)
    assert rebuilds == []
    assert reopened.index.recompute_generation() != reopened.index.generation()
    assert reopened.index.generation() == compute_graph_generation(reopened.graph)
    reopened.close()


def test_mismatching_stored_stamp_rebuilds_once(tmp_path: Path, monkeypatch):
    vault = tmp_path / "vault"
    store = _open_vault(vault)
    store.add_node(Node(id="a1", type="Concept", title="graph store"))
    store.close()

    meta_path = _index_dir(vault) / "meta.json"
    meta = json.loads(meta_path.read_text())
    meta["graph_generation"] = "0" * 64
    meta_path.write_text(json.dumps(meta))

    rebuilds = _spy_reindex(monkeypatch)
    _open_vault(vault).close()
    _open_vault(vault).close()
    assert rebuilds == [1]
