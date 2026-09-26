"""ADR 0005 -- Claim->entity bridge edges (live, per-commit deterministic mint).

Model-free: builds graphs directly on :class:`InMemoryStore` (no LLM, no
embedder). Covers the live deterministic mint helper
(``ingest._add_claim_provenance_edges``):

  * an entity-object Claim mints BOTH rdf:subject and rdf:object;
  * a literal-object Claim mints rdf:subject ONLY (O_literal preserved);
  * minting twice yields one edge per (src, type, dst) -- idempotent;
  * a dangling subject is skipped, never crashed.

The bulk in-place ``backfill_bridge_edges`` migration was REMOVED -- it corrupted
edge adjacency on populated graphs. The corruption-free heal is ``kg rebuild``
(fresh-graph re-extraction); ``ensure_source_mentions`` (the live per-commit
mention mint) is covered in ``test_source_mentions.py``.
"""

from __future__ import annotations

from okto_neuron.core.schema import Node
from okto_neuron.ingest import _add_claim_provenance_edges
from okto_neuron.store import InMemoryStore


def _node(node_id: str, type_: str, **facets: object) -> Node:
    return Node(id=node_id, type=type_, title=node_id, facets=dict(facets))


def _edge_keys(store: InMemoryStore, src: str) -> dict[str, str]:
    return {e.type: e.dst for e in store.list_edges(src=src)}


# -- live deterministic mint helper (ingest._add_claim_provenance_edges) -------


def _seed_block_and_prov(store: InMemoryStore) -> str:
    """Stage a Block + the deterministic Activity/Agent the helper edges to."""
    from okto_neuron.ingest import EXTRACTION_ACTIVITY_ID, SYSTEM_AGENT_ID

    store.add_node(_node("block:1", "Block"))
    store.add_node(_node(EXTRACTION_ACTIVITY_ID, "Activity"))
    store.add_node(_node(SYSTEM_AGENT_ID, "Agent"))
    return "block:1"


def test_entity_object_claim_mints_subject_and_object() -> None:
    store = InMemoryStore()
    block_id = _seed_block_and_prov(store)
    store.add_node(_node("subj", "Concept"))
    store.add_node(_node("obj", "Concept"))
    store.add_node(_node("claim:1", "Claim"))

    _add_claim_provenance_edges(store, "claim:1", block_id, subject_id="subj", object_id="obj")

    by_type = _edge_keys(store, "claim:1")
    assert by_type["rdf:subject"] == "subj"
    assert by_type["rdf:object"] == "obj"
    # PROV-O triple still present (F3 unchanged).
    assert by_type["prov:wasDerivedFrom"] == block_id


def test_literal_object_claim_mints_subject_only() -> None:
    store = InMemoryStore()
    block_id = _seed_block_and_prov(store)
    store.add_node(_node("subj", "Concept"))
    store.add_node(_node("claim:lit", "Claim"))

    # Literal-object claim: object_id is None (O_literal stays an attribute).
    _add_claim_provenance_edges(store, "claim:lit", block_id, subject_id="subj", object_id=None)

    by_type = _edge_keys(store, "claim:lit")
    assert by_type["rdf:subject"] == "subj"
    assert "rdf:object" not in by_type


def test_live_mint_is_idempotent() -> None:
    store = InMemoryStore()
    block_id = _seed_block_and_prov(store)
    store.add_node(_node("subj", "Concept"))
    store.add_node(_node("obj", "Concept"))
    store.add_node(_node("claim:1", "Claim"))

    for _ in range(2):
        _add_claim_provenance_edges(store, "claim:1", block_id, subject_id="subj", object_id="obj")

    edges = list(store.list_edges(src="claim:1"))
    keyed = {(e.src, e.type, e.dst) for e in edges}
    # One edge per (src, type, dst): 3 PROV + rdf:subject + rdf:object.
    assert len(edges) == len(keyed) == 5


def test_live_mint_skips_dangling_subject() -> None:
    store = InMemoryStore()
    block_id = _seed_block_and_prov(store)
    store.add_node(_node("claim:1", "Claim"))

    # subject_id points at a node that was never created -- must skip, not crash.
    _add_claim_provenance_edges(store, "claim:1", block_id, subject_id="ghost", object_id=None)

    by_type = _edge_keys(store, "claim:1")
    assert "rdf:subject" not in by_type
    assert by_type["prov:wasDerivedFrom"] == block_id
