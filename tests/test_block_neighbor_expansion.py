"""Phase 2 — query-time block-neighbor expansion (D5).

Storage is overlap-free; context is recovered at read time by expanding a
retrieved block to its same-document neighbours (same ``source_path``, adjacent
``block_index``). Pure in-memory; no network, no LLM.
"""

from __future__ import annotations

from okto_neuron.core.schema import Node
from okto_neuron.query import build_block_index, expand_block_context
from okto_neuron.store.memory import InMemoryStore

_HASH = "sha256:" + "a" * 64


def _block(
    idx: int, path: str = "/vault/doc.md", *, start: int | None = None, end: int | None = None
) -> Node:
    s = idx * 100 if start is None else start
    e = s + 50 if end is None else end
    return Node(
        id=f"blk-{path}-{idx}",
        type="Block",
        title=f"block {idx}",
        content=f"content of block {idx}",
        facets={
            "source_path": path,
            "block_index": idx,
            "byte_start": s,
            "byte_end": e,
            "content_hash": _HASH,
        },
    )


def _seed(*blocks: Node) -> InMemoryStore:
    store = InMemoryStore()
    for b in blocks:
        store.add_node(b)
    return store


def test_middle_block_expands_both_sides() -> None:
    store = _seed(_block(0), _block(1), _block(2), _block(3), _block(4))
    spans = expand_block_context(store, "blk-/vault/doc.md-2", k_neighbors=1)
    assert [s.block_index for s in spans] == [1, 3]
    assert all(s.path == "/vault/doc.md" for s in spans)
    # the seed block itself is never included
    assert all(s.block_id != "blk-/vault/doc.md-2" for s in spans)


def test_expansion_is_ordered_by_block_index() -> None:
    store = _seed(_block(0), _block(1), _block(2), _block(3), _block(4))
    spans = expand_block_context(store, "blk-/vault/doc.md-2", k_neighbors=2)
    assert [s.block_index for s in spans] == [0, 1, 3, 4]


def test_edge_block_expands_one_side_only() -> None:
    store = _seed(_block(0), _block(1), _block(2))
    head = expand_block_context(store, "blk-/vault/doc.md-0", k_neighbors=1)
    tail = expand_block_context(store, "blk-/vault/doc.md-2", k_neighbors=1)
    assert [s.block_index for s in head] == [1]
    assert [s.block_index for s in tail] == [1]


def test_k_zero_returns_empty() -> None:
    store = _seed(_block(0), _block(1), _block(2))
    assert expand_block_context(store, "blk-/vault/doc.md-1", k_neighbors=0) == []


def test_unknown_or_empty_block_id_is_safe() -> None:
    store = _seed(_block(0), _block(1))
    assert expand_block_context(store, "", k_neighbors=2) == []
    assert expand_block_context(store, "does-not-exist", k_neighbors=2) == []


def test_block_with_no_source_path_yields_no_context() -> None:
    orphan = Node(id="orphan", type="Block", title="x", content="x", facets={})
    store = _seed(orphan, _block(0))
    assert expand_block_context(store, "orphan", k_neighbors=2) == []


def test_cross_document_isolation() -> None:
    # Two docs interleaved in the store; expansion must never cross files.
    store = _seed(
        _block(0, "/vault/a.md"),
        _block(1, "/vault/a.md"),
        _block(0, "/vault/b.md"),
        _block(1, "/vault/b.md"),
    )
    spans = expand_block_context(store, "blk-/vault/a.md-0", k_neighbors=5)
    assert {s.path for s in spans} == {"/vault/a.md"}
    assert [s.block_index for s in spans] == [1]


def test_shared_index_reused_matches_unindexed() -> None:
    store = _seed(_block(0), _block(1), _block(2), _block(3))
    idx = build_block_index(store)
    with_index = expand_block_context(store, "blk-/vault/doc.md-1", k_neighbors=1, index=idx)
    without = expand_block_context(store, "blk-/vault/doc.md-1", k_neighbors=1)
    assert [s.block_id for s in with_index] == [s.block_id for s in without]


def test_context_span_carries_byte_coordinates() -> None:
    store = _seed(
        _block(0, start=0, end=10),
        _block(1, start=10, end=25),
        _block(2, start=25, end=40),
    )
    spans = expand_block_context(store, "blk-/vault/doc.md-1", k_neighbors=1)
    by_idx = {s.block_index: s for s in spans}
    assert (by_idx[0].byte_start, by_idx[0].byte_end) == (0, 10)
    assert (by_idx[2].byte_start, by_idx[2].byte_end) == (25, 40)
    assert by_idx[0].content_hash == _HASH


# ── read-time grounding (ask) ───────────────────────────────────────────────


def test_hit_text_assembles_primary_plus_neighbors_in_document_order(tmp_path) -> None:
    from okto_neuron.companion import _hit_text
    from okto_neuron.models import ContextSpan, Node, Provenance, QueryHit

    doc = tmp_path / "doc.md"
    doc.write_text("FIRST. SECOND. THIRD.", encoding="utf-8")
    p = str(doc)
    prov = Provenance(
        path=p,
        byte_start=7,
        byte_end=14,
        content_hash=_HASH,
        extraction_activity_id="a",
        agent_id="g",
        document_id="d",
        block_id="b1",
    )
    # neighbors supplied out of order — _hit_text must re-order by byte position
    ctx = (
        ContextSpan(
            path=p, byte_start=14, byte_end=21, content_hash=_HASH, block_id="b2", block_index=2
        ),
        ContextSpan(
            path=p, byte_start=0, byte_end=6, content_hash=_HASH, block_id="b0", block_index=0
        ),
    )
    hit = QueryHit(
        node=Node(id="b1", type="Block", name="SECOND"),
        score=0.9,
        provenance=prov,
        context_spans=ctx,
    )
    assert _hit_text(hit) == "FIRST.\nSECOND.\nTHIRD."


def test_hit_text_without_context_is_primary_only(tmp_path) -> None:
    from okto_neuron.companion import _hit_text
    from okto_neuron.models import Node, Provenance, QueryHit

    doc = tmp_path / "doc.md"
    doc.write_text("FIRST. SECOND. THIRD.", encoding="utf-8")
    prov = Provenance(
        path=str(doc),
        byte_start=7,
        byte_end=14,
        content_hash=_HASH,
        extraction_activity_id="a",
        agent_id="g",
        document_id="d",
        block_id="b1",
    )
    hit = QueryHit(node=Node(id="b1", type="Block", name="SECOND"), score=0.9, provenance=prov)
    assert _hit_text(hit) == "SECOND."


# ── neighbor-width resolution (env -> config -> off) ────────────────────────


def test_query_neighbors_env_overrides_and_clamps(tmp_path, monkeypatch) -> None:
    from okto_neuron.vault import Vault

    vault = Vault(tmp_path, InMemoryStore())
    monkeypatch.setenv("OKTO_NEURON_QUERY_NEIGHBORS", "3")
    assert vault._query_neighbors() == 3
    monkeypatch.setenv("OKTO_NEURON_QUERY_NEIGHBORS", "-5")
    assert vault._query_neighbors() == 0
    monkeypatch.setenv("OKTO_NEURON_QUERY_NEIGHBORS", "garbage")
    assert vault._query_neighbors() == 0


def test_query_neighbors_defaults_off(tmp_path, monkeypatch) -> None:
    from okto_neuron.vault import Vault

    monkeypatch.delenv("OKTO_NEURON_QUERY_NEIGHBORS", raising=False)
    vault = Vault(tmp_path, InMemoryStore())
    assert vault._query_neighbors() == 0
