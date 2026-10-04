"""ADR 0006 source-mention minting (live, per-commit).

This module holds ``ensure_source_mentions``: the single node-derived
``schema:mentions`` (Document → entity) mint, called per-commit from
``companion.remember`` against the just-committed node set. It mints
``Document --schema:mentions--> entity`` for every primitive-entity-typed node
whose ``source_path`` facet resolves to an existing Document node.

WHY THERE IS NO BULK IN-PLACE BACKFILL HERE: the former ``backfill_bridge_edges``
migration (and a whole-store ``ensure_source_mentions(store)`` heal) bypassed the
verified-generation boundary. Structural heals instead use ``kg rebuild`` so the
complete topology is derived and audited in a fresh graph before atomic swap.
``ensure_source_mentions`` survives only for the per-commit live path.

Idempotency: every edge id is ``sha256("edge", src, type, dst)`` — the same scheme
every mint site uses — so a re-commit dedups on ``edge.id``. Endpoints are
existence-checked before minting; a dangling id is skipped, never crashed
(``add_edge`` raises on a missing endpoint).
"""

from __future__ import annotations

from typing import Iterable

from okto_neuron.core.schema import Edge, Provenance
from okto_neuron.ingest.markdown import sha256_hex

# F7 canonical vocabulary.
_SCHEMA_MENTIONS = "schema:mentions"

# ADR 0006 R2 — schema:mentions is minted ONLY for the 5 locked primitive entity
# types. NEVER Document/Block/Claim/other support types (a Document mentioning
# itself or a Block adds no information and would re-orphan the support layer).
# Hardcoded here to keep the migration self-contained and decoupled from the
# conditionally-imported ``primitives`` package.
_PRIMITIVE_ENTITY_TYPES: frozenset[str] = frozenset(
    {"Agent", "Activity", "Concept", "InformationObject", "Place"}
)

# ADR 0006 — node-derived schema:mentions provenance. Edge ids are
# provenance-independent (``sha256("edge", src, type, dst)``), so idempotency
# holds regardless of the provenance carried.
_MENTIONS_PROV = Provenance(source="migration", rule_id="adr0006-source-mentions")


def _document_id_for_path(abs_path: str) -> str | None:
    """Derive the deterministic Document id for an absolute source path, matching
    ``ingest.markdown`` (``sha256("document", str(p.resolve()))``)."""
    if not abs_path:
        return None
    return sha256_hex("document", abs_path)


def ensure_source_mentions(store: object, node_ids: Iterable[str] | None = None) -> int:
    """ADR 0006 D1 — node-derived ``schema:mentions`` (Document → entity).

    For each **primitive-entity-typed** node carrying a ``source_path`` facet that
    resolves to an existing Document node, mint ``Document --schema:mentions-->
    entity``. The ONLY supported caller is the per-commit live mint in
    ``companion.remember()`` (D2), passing the small set of node ids just committed.

    - ``node_ids``: restrict to these node ids (the set just committed, in the live
      path). ``None`` would scan the whole store; the whole-store heal that used
      this branch was removed in favour of verified fresh-generation rebuild. The
      ``None`` branch survives only for a freshly-built store.
    - Type allowlist (R2): only the 5 locked primitives — never Document / Block /
      Claim / other support types.
    - Idempotent on ``(src, type, dst)`` via the shared edge-id scheme
      (``sha256("edge", src, type, dst)``); an existing edge is skipped.
    - D5/R1: the mint works only because the entity ``source_path`` facet, the
      Document id, and the resolver all agree on the **absolute resolved** path.
      A vault-relative ``source_path`` would resolve to no Document and mint
      nothing — silently. (Verified absolute at the live commit site:
      ``companion._BlockAnchor.source_path = str(Path(source)...resolve())``.)

    Returns the number of mention edges minted.
    """
    existing_edge_ids = {str(e.id) for e in store.list_edges()}

    if node_ids is None:
        nodes: Iterable[object] = store.list_nodes()
    else:
        # One batched read (was one get_node per id); input order and duplicate
        # ids are kept, missing ids are skipped, exactly as before.
        wanted = list(node_ids)
        by_id = {node.id: node for node in store.get_nodes(wanted)}
        nodes = [by_id[nid] for nid in wanted if nid in by_id]

    candidates: list[tuple[object, str]] = []
    for node in nodes:
        if getattr(node, "type", None) not in _PRIMITIVE_ENTITY_TYPES:
            continue
        facets = getattr(node, "facets", {}) or {}
        source_path = str(facets.get("source_path") or "")
        document_id = _document_id_for_path(source_path)
        if not document_id:
            continue
        if str(node.id) == document_id:
            continue  # a node mentioning itself adds no information
        candidates.append((node, document_id))
    # Minting adds edges only, never nodes, so Document existence can be read once
    # for the whole set up front.
    existing_documents = {
        node.id for node in store.get_nodes([doc_id for _node, doc_id in candidates])
    }

    minted = 0
    for node, document_id in candidates:
        entity_id = str(node.id)
        if document_id not in existing_documents:
            continue  # D7: a source_path that resolves to no Document is left alone
        edge_id = sha256_hex("edge", document_id, _SCHEMA_MENTIONS, entity_id)
        if edge_id in existing_edge_ids:
            continue  # idempotent — already linked
        store.add_edge(
            Edge(
                id=edge_id,
                type=_SCHEMA_MENTIONS,
                src=document_id,
                dst=entity_id,
                provenance=_MENTIONS_PROV,
            )
        )
        existing_edge_ids.add(edge_id)
        minted += 1

    flush = getattr(store, "flush", None)
    if callable(flush):
        flush()

    return minted


__all__ = ["ensure_source_mentions"]
