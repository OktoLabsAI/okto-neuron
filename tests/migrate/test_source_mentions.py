"""ADR 0006 — node-derived ``schema:mentions`` (Document → entity).

Model-free regression guard (InMemoryStore, no LLM, no embedder). Asserts the
defect that left 301/302 orphans is closed at the helper level, independent of
any call site:

  * every primitive-entity node whose ``source_path`` resolves to an existing
    Document gains degree >= 1 via a ``schema:mentions`` edge from THAT Document
    (D1, D5 — real Document resolution, catches vault-relative path drift);
  * the type allowlist holds — Document / Block / Claim / unknown support nodes
    are never given a self-mention or any mention edge (R2);
  * a ``source_path`` that resolves to NO Document mints nothing (D7);
  * a second call mints zero — idempotent on ``(src, schema:mentions, dst)`` (F6);
  * the ``node_ids`` restriction touches only the named nodes (the live-commit
    contract used by ``remember()`` in D2).
"""

from __future__ import annotations

from pathlib import Path

from okto_neuron.core.schema import Node
from okto_neuron.ingest.markdown import sha256_hex
from okto_neuron.migrate import ensure_source_mentions
from okto_neuron.store import InMemoryStore


def _doc_id(abs_path: str) -> str:
    # Matches ingest.markdown: sha256("document", str(p.resolve())).
    return sha256_hex("document", abs_path)


def _node(node_id: str, type_: str, **facets: object) -> Node:
    return Node(id=node_id, type=type_, title=node_id, facets=dict(facets))


def _mentions_targets(store: InMemoryStore, doc_id: str) -> set[str]:
    return {e.dst for e in store.list_edges(src=doc_id) if e.type == "schema:mentions"}


def _degree(store: InMemoryStore, node_id: str) -> int:
    return sum(1 for e in store.list_edges() if e.src == node_id or e.dst == node_id)


def _seed(tmp_path: Path) -> tuple[InMemoryStore, str, list[str]]:
    """A committed graph with a Document + several primitive entities carrying
    that Document's exact absolute source_path, and ZERO connecting edges/Claims.
    Returns (store, document_id, entity_ids)."""
    store = InMemoryStore()

    # D5: the entity facet, the Document id, and the resolver MUST agree on the
    # absolute resolved path — build the Document id from that same string.
    src_path = str((tmp_path / "notes.md").resolve())
    doc_id = _doc_id(src_path)
    store.add_node(_node(doc_id, "Document", source_path=src_path))

    entity_ids: list[str] = []
    for i, type_ in enumerate(["Agent", "Activity", "Concept", "InformationObject", "Place"]):
        nid = f"entity:{type_}:{i}"
        store.add_node(_node(nid, type_, source_path=src_path))
        entity_ids.append(nid)

    # No edges/claims at all — every entity is degree 0 before the heal.
    assert all(_degree(store, nid) == 0 for nid in entity_ids)
    return store, doc_id, entity_ids


def test_every_sourced_entity_gets_a_mention_edge(tmp_path: Path) -> None:
    store, doc_id, entity_ids = _seed(tmp_path)

    minted = ensure_source_mentions(store)

    assert minted == len(entity_ids)
    targets = _mentions_targets(store, doc_id)
    for nid in entity_ids:
        assert nid in targets, f"{nid} got no schema:mentions edge"
        assert _degree(store, nid) >= 1


def test_idempotent_second_call_mints_zero(tmp_path: Path) -> None:
    store, _doc_id, entity_ids = _seed(tmp_path)

    first = ensure_source_mentions(store)
    second = ensure_source_mentions(store)

    assert first == len(entity_ids)
    assert second == 0


def test_support_types_and_unresolvable_paths_left_untouched(tmp_path: Path) -> None:
    store = InMemoryStore()
    src_path = str((tmp_path / "notes.md").resolve())
    doc_id = _doc_id(src_path)
    store.add_node(_node(doc_id, "Document", source_path=src_path))

    # R2: support-type nodes carrying the SAME source_path must never be minted a
    # mention (no Document→Document, Document→Block, Document→Claim).
    store.add_node(_node("block:1", "Block", source_path=src_path))
    store.add_node(_node("claim:1", "Claim", source_path=src_path))

    # D7: a primitive entity whose source_path resolves to NO Document is left
    # alone (nothing to link to).
    store.add_node(
        _node(
            "orphan:concept",
            "Concept",
            source_path=str((tmp_path / "missing.md").resolve()),
        )
    )

    minted = ensure_source_mentions(store)

    assert minted == 0
    assert _mentions_targets(store, doc_id) == set()
    assert _degree(store, "block:1") == 0
    assert _degree(store, "claim:1") == 0
    assert _degree(store, "orphan:concept") == 0


def test_node_ids_restriction_only_touches_named_nodes(tmp_path: Path) -> None:
    """The live-commit contract (D2): ensure_source_mentions(store, committed)
    mints only for the named ids, not the whole store."""
    store, doc_id, entity_ids = _seed(tmp_path)
    committed = {entity_ids[0]}

    minted = ensure_source_mentions(store, committed)

    assert minted == 1
    targets = _mentions_targets(store, doc_id)
    assert targets == committed
    # The unnamed entities stay orphaned until their own commit/heal.
    for nid in entity_ids[1:]:
        assert _degree(store, nid) == 0
