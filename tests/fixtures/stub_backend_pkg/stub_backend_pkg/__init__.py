"""StubGraphStore — an installable, out-of-tree GraphStore fixture.

Exists to prove ``okto_neuron.store.registry.resolve_graph_backend`` can
resolve a graph backend it has never imported: this package ships its own
``pyproject.toml`` (see the sibling file) and registers itself under the
``marginalia.graph_backends`` entry-point group as ``stub`` (M3 spec §2.11).
It is pulled into the dev environment only via the root project's
``[tool.uv.sources]`` local-path entry — nothing in ``src/okto_neuron`` ever
imports this module directly.

Implements ``GraphStore`` (``okto_neuron.store.protocol``) over a plain dict,
deliberately independent of ``okto_neuron.store.memory.InMemoryStore`` so a
change to that in-tree reference implementation can never silently make this
fixture pass by accident. Node/edge invariants (closed schema, immutable
identity) are re-derived from ``okto_neuron.store.closed_set``, the single
source of truth for those rules, not copied.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from okto_neuron.core.schema import Edge, Node
from okto_neuron.store.closed_set import require_same_edge_identity, require_writable_node_type
from okto_neuron.store.protocol import BackendHealth

__all__ = ["StubGraphStore"]

_STATE_RELATIVE_PATH = Path(".marginalia") / "stub_backend_store.json"


@dataclass(frozen=True)
class _RecoveryStatus:
    """Local stand-in for the ``store.protocol.RecoveryStatus`` shape proposed
    in M3 spec §2.4, which has not landed on ``GraphStore`` yet. Same field
    names, so swapping this for the real dataclass once it exists is a
    no-op for any caller that only reads ``.recovered``/``.mode``/``.detail``.
    """

    recovered: bool
    mode: Optional[str] = None
    detail: Optional[str] = None


class StubGraphStore:
    """A third-party ``GraphStore`` implementation, for the registry contract suite.

    Constructed two ways: bare ``StubGraphStore()`` (the registry contract
    suite's own use — pure in-memory, no vault, no persistence, unchanged
    from the original fixture) and ``StubGraphStore.from_vault(vault_path,
    config)`` (``store/vault.py``'s ``_construct_backend`` dispatch hook,
    M3 spec §2.6/§2.7 — used whenever a real vault is pinned to
    ``backend: stub``). Only the latter path persists: a vault opened via
    ``kg init``/``okto-neuron init --backend stub`` and reopened later (a
    server restart, a fresh CLI invocation) must see the SAME graph it wrote
    before, which a purely in-process dict cannot do across process
    boundaries. Persistence is one flat JSON document under
    ``<vault>/.marginalia/stub_backend_store.json``, written on
    ``checkpoint()``/``close()`` — mirrors the "one file, whole graph"
    shape ``LadybugStore`` and ``InMemoryStore``'s own snapshot helpers use,
    scaled down to this fixture's needs.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, Edge] = {}
        self._state_path: Optional[Path] = None
        self._closed = False

    @classmethod
    def from_vault(cls, vault_path: Path, config: object | None) -> "StubGraphStore":
        """Construct against a real vault directory, loading any prior state.

        ``config`` (the vault's parsed ``storage`` block) is accepted per
        ``_construct_backend``'s documented ``from_vault(vault_path,
        config)`` hook shape but unused — this stub backend has no
        backend-specific config fields to read.
        """
        store = cls()
        store._state_path = Path(vault_path) / _STATE_RELATIVE_PATH
        store._load()
        return store

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.exists():
            return
        payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        for raw in payload.get("nodes", []):
            node = Node.model_validate(raw)
            self._nodes[node.id] = node
        for raw in payload.get("edges", []):
            edge = Edge.model_validate(raw)
            self._edges[edge.id] = edge

    def _dump(self) -> None:
        if self._state_path is None:
            return
        payload = {
            "nodes": [node.model_dump(mode="json") for node in self._nodes.values()],
            "edges": [edge.model_dump(mode="json") for edge in self._edges.values()],
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_path.with_name(self._state_path.name + ".tmp")
        tmp_path.write_text(json.dumps(payload), encoding="utf-8")
        tmp_path.replace(self._state_path)

    def add_node(self, node: Node, clear_embedding: bool = False) -> None:
        require_writable_node_type(node.type)
        existing = self._nodes.get(node.id)
        if existing is not None:
            # created_at is immutable once a node id exists, matching the
            # invariant both in-tree stores (Ladybug, InMemoryStore) enforce.
            update: dict[str, object] = {"created_at": existing.created_at}
            if node.embedding is None and not clear_embedding:
                update["embedding"] = existing.embedding
            node = node.model_copy(update=update)
        self._nodes[node.id] = node

    def add_edge(self, edge: Edge) -> None:
        if edge.src not in self._nodes or edge.dst not in self._nodes:
            raise ValueError(f"edge endpoints missing: {edge.src} -> {edge.dst}")
        existing = self._edges.get(edge.id)
        if existing is not None:
            # Edge identity (type, src, dst) is immutable once an edge id
            # exists; reusing an id for a different identity is a caller bug.
            require_same_edge_identity(existing, edge)
        self._edges[edge.id] = edge

    def get_node(self, node_id: str, include_embedding: bool = True) -> Optional[Node]:
        return self._nodes.get(node_id)

    def get_nodes(self, node_ids: Iterable[str], include_embedding: bool = False) -> list[Node]:
        result: list[Node] = []
        for node_id in dict.fromkeys(node_ids):
            node = self._nodes.get(node_id)
            if node is not None:
                result.append(node)
        return result

    def list_nodes(
        self, type: Optional[str] = None, include_embedding: bool = False
    ) -> Iterable[Node]:
        for n in sorted(self._nodes.values(), key=lambda n: n.id):
            if type is None or n.type == type:
                yield n

    def list_edges(
        self,
        src: Optional[str] = None,
        dst: Optional[str] = None,
        type: Optional[str] = None,
    ) -> Iterable[Edge]:
        for e in sorted(self._edges.values(), key=lambda e: e.id):
            if src is not None and e.src != src:
                continue
            if dst is not None and e.dst != dst:
                continue
            if type is not None and e.type != type:
                continue
            yield e

    def checkpoint(self) -> None:
        """Flush to ``<vault>/.marginalia/stub_backend_store.json`` when vault-backed.

        A no-op for a bare in-process ``StubGraphStore()`` (``_state_path`` is
        ``None``), matching the original fixture's contract; every write is
        already durably in-process for that construction path.
        """
        self._dump()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._dump()

    @property
    def is_closed(self) -> bool:
        return self._closed

    def generation(self) -> str:
        """A stable content-derived stamp (see ``GraphStore.generation``).

        Like ``InMemoryStore``, this backend has no on-disk file and no
        bootstrap-assigned identity UUID to reflect, so this derives a
        deterministic stamp from current content using the same digest
        ``store.index.compute_graph_generation`` uses to detect a stale
        index. Falls back to a constant if that helper can't be imported
        (kept optional so this module never grows a hard dependency on the
        index package).
        """
        try:
            from okto_neuron.store.index import compute_graph_generation
        except ImportError:
            return "stub"
        return compute_graph_generation(self)

    def health(self) -> BackendHealth:
        """Always healthy: an in-process dict has no separate on-disk state to corrupt."""
        return BackendHealth(healthy=True, detail="stub backend has no backing file")

    def recovery_status(self) -> _RecoveryStatus:
        """Always unrecovered: nothing here was ever corrupted or repaired."""
        return _RecoveryStatus(recovered=False)

    def detect_drift(self, expected_generation: Optional[str]) -> None:
        """Always ``None``: an in-process dict cannot drift from itself."""
        return None
