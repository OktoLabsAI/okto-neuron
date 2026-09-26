"""Shared generation-scoped read mixin for server-backed graph stores (D-51).

M5 spec §2, implementing plan lines 285/314. A server backend that isolates a
build (rebuild/reembed) from the live graph via an internal `_generation`
property tag -- rather than a genuinely separate database/file the way
Ladybug/Grafx stage -- needs every read path to filter on that tag
unconditionally, so there is structurally no way for a subclass read method
to forget the filter. This module implements the three read methods
(`get_node`, `list_nodes`, `list_edges`) exactly once here; a concrete
backend (`store/neo4j.py`'s `Neo4jStore`, and a future Neptune backend)
subclasses this mixin and supplies only the driver/session/Cypher-dialect
layer: a `_run_read(cypher, params) -> list[dict]` primitive, a
`_node_from_row`/`_edge_from_row` pair, and the `_generation_tag`/
`_metadata_node_id` attributes this mixin reads.

Not used by Grafx (D-50): Grafx isolates a build via a genuinely separate
directory swapped in atomically (`store/staging.py`'s `GrafxStaging`), so it
has no `_generation` property concept at all -- see `grafx.py`'s own module
docstring for that divergence.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Optional, Protocol

from okto_neuron.core.schema import Edge, Node
from okto_neuron.store import schema


class _ReadPrimitive(Protocol):
    """Structural shape a concrete backend must supply for this mixin to work."""

    _generation_tag: str
    _metadata_node_id: str
    vault_id: str

    def _run_read(self, cypher: str, params: Mapping[str, object]) -> list[Mapping[str, object]]: ...
    def _node_from_row(self, row: Mapping[str, object]) -> Node: ...
    def _edge_from_row(self, row: Mapping[str, object]) -> Edge: ...


class GenerationScopedBackendMixin:
    """Implements `get_node`/`list_nodes`/`list_edges` filtered on
    `vault_id` AND `_generation`.

    Every Cypher statement here appends `AND n.vault_id = $vault_id AND
    n._generation = $generation` (or the `e.`-prefixed equivalents for
    edges) to whatever `WHERE` clause it would otherwise build. Neo4j
    Community Edition has exactly one shared database (`store/neo4j.py`'s
    own module docstring), so `vault_id` is the ONLY thing that keeps two
    vaults pinned to the same instance from reading each other's rows —
    `_generation` alone is not vault-scoped (every vault's rebuild/heal/
    reembed writes the SAME literal tag, e.g. `"rebuild"`), and node ids are
    content-addressed, so two vaults sharing even one identical note would
    otherwise silently merge in every read here. Filtering on both means a
    caller can never read across a vault OR a build's isolated generation
    boundary by construction -- not by convention each subclass has to
    remember.
    """

    #: The metadata singleton's own node id is excluded from `list_nodes`
    #: exactly like `GrafxStore.list_nodes` excludes `schema.SCHEMA_METADATA_NODE_ID`.
    _metadata_node_id: str = schema.SCHEMA_METADATA_NODE_ID

    def get_node(self: _ReadPrimitive, node_id: str) -> Optional[Node]:
        rows = self._run_read(
            "MATCH (n:Node {id: $id, vault_id: $vault_id, _generation: $generation}) RETURN n",
            {"id": node_id, "vault_id": self.vault_id, "generation": self._generation_tag},
        )
        if not rows:
            return None
        return self._node_from_row(rows[0])

    def get_nodes(self: _ReadPrimitive, node_ids: Iterable[str]) -> list[Node]:
        ids = list(dict.fromkeys(node_ids))
        if not ids:
            return []
        rows = self._run_read(
            "MATCH (n:Node) WHERE n.id IN $ids AND n.vault_id = $vault_id "
            "AND n._generation = $generation RETURN n",
            {"ids": ids, "vault_id": self.vault_id, "generation": self._generation_tag},
        )
        by_id = {str(row["n"]["id"]): self._node_from_row(row) for row in rows}  # type: ignore[index]
        return [by_id[i] for i in ids if i in by_id]

    def list_nodes(self: _ReadPrimitive, type: Optional[str] = None) -> Iterable[Node]:
        params: dict[str, object] = {
            "vault_id": self.vault_id,
            "generation": self._generation_tag,
            "metadata_id": self._metadata_node_id,
        }
        clauses = [
            "n.vault_id = $vault_id",
            "n._generation = $generation",
            "n.id <> $metadata_id",
        ]
        if type is not None:
            clauses.append("n.type = $type")
            params["type"] = type
        where = " AND ".join(clauses)
        rows = self._run_read(f"MATCH (n:Node) WHERE {where} RETURN n ORDER BY n.id", params)
        return [self._node_from_row(row) for row in rows]

    def list_edges(
        self: _ReadPrimitive,
        src: Optional[str] = None,
        dst: Optional[str] = None,
        type: Optional[str] = None,
    ) -> Iterable[Edge]:
        params: dict[str, object] = {"vault_id": self.vault_id, "generation": self._generation_tag}
        clauses = ["e.vault_id = $vault_id", "e._generation = $generation"]
        if src is not None:
            clauses.append("e.src = $src")
            params["src"] = src
        if dst is not None:
            clauses.append("e.dst = $dst")
            params["dst"] = dst
        if type is not None:
            clauses.append("e.type = $type")
            params["type"] = type
        where = " AND ".join(clauses)
        rows = self._run_read(
            f"MATCH (:Node)-[e:EDGE]->(:Node) WHERE {where} RETURN e ORDER BY e.id", params
        )
        return [self._edge_from_row(row) for row in rows]


__all__ = ["GenerationScopedBackendMixin"]
