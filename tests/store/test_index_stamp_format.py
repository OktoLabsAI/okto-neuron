"""The v2 index stamp: raw float64 embedding bytes, a ``v2:`` prefix, one rebuild to upgrade."""

from __future__ import annotations

import hashlib
import json
import logging
import struct
import sys
from pathlib import Path

import pytest

from okto_neuron.core.schema import Node
from okto_neuron.store import vault as vault_module
from okto_neuron.store.index.corpus import STAMP_PREFIX, graph_generation, node_digest
from okto_neuron.store.vault import _open_vault

_NODE = Node(id="a1", type="Concept", title="t", content="c", tags=["x"], embedding=[1.0, 2.5])


def test_node_digest_byte_layout_is_pinned():
    head = b'["a1","Concept","t","c",["x"],null]'
    # 1.0 and 2.5 as little-endian float64, behind the 0x01 presence marker
    vector = bytes.fromhex("01" "000000000000f03f" "0000000000000440")
    assert hashlib.sha256(head + vector).hexdigest() == node_digest(_NODE)
    assert node_digest(_NODE) == "071ced6e55315b1f4af6829252235b9f4a63dbd4e2bdab3806f1e3d29b0cf9e6"


def test_missing_embedding_has_its_own_marker():
    bare = _NODE.model_copy(update={"embedding": None})
    head = b'["a1","Concept","t","c",["x"],null]'
    assert node_digest(bare) == hashlib.sha256(head + b"\x00").hexdigest()
    assert node_digest(bare) != node_digest(_NODE)


def test_generation_carries_the_version_prefix():
    assert graph_generation([_NODE]).startswith(STAMP_PREFIX)


def test_digest_is_identical_with_and_without_numpy(monkeypatch):
    pytest.importorskip("numpy")
    vector = [0.1, -3.25, 1e-300, 7.0, float(2**60)]
    node = _NODE.model_copy(update={"embedding": vector})
    with_numpy = node_digest(node)
    monkeypatch.setitem(sys.modules, "numpy", None)  # `import numpy` now raises ImportError
    assert node_digest(node) == with_numpy
    assert struct.pack("<%dd" % len(vector), *vector)  # the fallback path really is struct


def _spy_reindex(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = vault_module.reindex_all

    def spy(store, index):
        calls.append(1)
        return real(store, index)

    monkeypatch.setattr(vault_module, "reindex_all", spy)
    return calls


def test_old_format_stamp_rebuilds_once_then_never(tmp_path: Path, monkeypatch, caplog):
    vault = tmp_path / "vault"
    store = _open_vault(vault)
    store.add_node(Node(id="a1", type="Concept", title="graph store"))
    store.add_node(Node(id="a2", type="Concept", title="vector index"))
    store.close()

    meta_path = vault / ".marginalia" / "index" / "meta.json"
    meta = json.loads(meta_path.read_text())
    assert meta["graph_generation"].startswith(STAMP_PREFIX)
    meta["graph_generation"] = meta["graph_generation"][len(STAMP_PREFIX):]  # pre-v2 shape
    meta_path.write_text(json.dumps(meta))

    rebuilds = _spy_reindex(monkeypatch)
    with caplog.at_level(logging.INFO, logger="okto_neuron.store.vault"):
        reopened = _open_vault(vault)
        node_count = len(list(reopened.graph.list_nodes()))
        reopened.close()
    assert rebuilds == [1]
    upgrades = [r.getMessage() for r in caplog.records if "index stamp format upgraded" in r.getMessage()]
    assert upgrades == [f"index stamp format upgraded, rebuilding {node_count} docs"]
    assert json.loads(meta_path.read_text())["graph_generation"].startswith(STAMP_PREFIX)

    caplog.clear()
    with caplog.at_level(logging.INFO, logger="okto_neuron.store.vault"):
        _open_vault(vault).close()
    assert rebuilds == [1]
    assert not [r for r in caplog.records if "index stamp format upgraded" in r.getMessage()]


def _index(tmp_path: Path):
    from okto_neuron.store.index import DefaultIndexStore

    return DefaultIndexStore(tmp_path / "vault")


def test_directory_is_fsynced_after_each_replace(tmp_path: Path, monkeypatch):
    from okto_neuron.store.index import corpus

    index = _index(tmp_path)
    index.upsert(Node(id="a1", type="Concept", title="first"))
    events: list[str] = []
    real_replace, real_open = corpus.os.replace, corpus.os.open
    dir_fds: set[int] = set()

    def replace(src, dst):
        events.append(f"replace:{Path(dst).name}")
        real_replace(src, dst)

    def open_(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if Path(path) == index.index_dir:
            dir_fds.add(fd)
        return fd

    real_fsync, real_close = corpus.os.fsync, corpus.os.close

    def fsync(fd):
        if fd in dir_fds:
            events.append("fsync:dir")
        real_fsync(fd)

    def close(fd):
        dir_fds.discard(fd)  # descriptor numbers get reused by the next temp file
        real_close(fd)

    monkeypatch.setattr(corpus.os, "replace", replace)
    monkeypatch.setattr(corpus.os, "open", open_)
    monkeypatch.setattr(corpus.os, "fsync", fsync)
    monkeypatch.setattr(corpus.os, "close", close)
    index.checkpoint()
    assert events == ["replace:corpus.jsonl", "fsync:dir", "replace:meta.json", "fsync:dir"]


def test_unsupported_directory_fsync_is_swallowed(tmp_path: Path, monkeypatch):
    from okto_neuron.store.index import corpus

    def refuse(path, flags, *args, **kwargs):
        raise PermissionError("cannot open a directory here")

    index = _index(tmp_path)
    index.upsert(Node(id="a1", type="Concept", title="first"))
    monkeypatch.setattr(corpus.os, "open", refuse)
    index.checkpoint()
    assert (index.index_dir / "meta.json").exists()


def test_stale_tmp_is_removed_on_open_and_never_read(tmp_path: Path):
    index = _index(tmp_path)
    index.upsert(Node(id="a1", type="Concept", title="first"))
    index.checkpoint()
    stamp = index.generation()
    corpus_tmp = index.index_dir / "corpus.jsonl.tmp"
    meta_tmp = index.index_dir / "meta.json.tmp"
    unrelated = index.index_dir / "notes.tmp"
    corpus_tmp.write_text('{"id":"ghost","type":"Concept"}\n')
    meta_tmp.write_text('{"graph_generation": "bogus"}')
    unrelated.write_text("keep")

    reopened = _index(tmp_path)
    assert not corpus_tmp.exists() and not meta_tmp.exists()
    assert unrelated.exists()
    assert reopened.generation() == stamp
    assert [i for i, _ in reopened.search_text("first", k=5)] == ["a1"]


def test_stale_tmp_is_removed_on_save(tmp_path: Path):
    index = _index(tmp_path)
    index.index_dir.mkdir(parents=True)
    (index.index_dir / "corpus.jsonl.tmp").write_text("partial")
    index.upsert(Node(id="a1", type="Concept", title="first"))
    index.checkpoint()
    assert sorted(p.name for p in index.index_dir.iterdir()) == ["corpus.jsonl", "meta.json"]
