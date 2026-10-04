"""In-memory store — for tests and quick experimentation.

Real persistence (ladybug) lands in v0.1.
"""

from __future__ import annotations

from typing import Iterable, Optional

from okto_neuron.core.schema import Edge, Node
from okto_neuron.store.closed_set import require_same_edge_identity, require_writable_node_type
from okto_neuron.store.protocol import BackendHealth, DriftReport, RecoveryStatus


class InMemoryStore:
    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: dict[str, Edge] = {}
        self._closed = False

    def add_node(self, node: Node, clear_embedding: bool = False) -> None:
        require_writable_node_type(node.type)
        existing = self._nodes.get(node.id)
        if existing is not None:
            # Mirror LadybugStore: created_at is immutable once a node id
            # exists, regardless of whether the rest of the payload changed.
            update: dict[str, object] = {"created_at": existing.created_at}
            if node.embedding is None and not clear_embedding:
                update["embedding"] = existing.embedding  # upsert keeps the stored vector
            node = node.model_copy(update=update)
        self._nodes[node.id] = node

    def add_edge(self, edge: Edge) -> None:
        if edge.src not in self._nodes or edge.dst not in self._nodes:
            raise ValueError(f"edge endpoints missing: {edge.src} -> {edge.dst}")
        existing = self._edges.get(edge.id)
        if existing is not None:
            # Mirror LadybugStore: edge identity (type, src, dst) is immutable
            # once an edge id exists; reusing an id for a different identity
            # is a caller bug that must fail loudly, not silently overwrite.
            require_same_edge_identity(existing, edge)
        self._edges[edge.id] = edge

    @staticmethod
    def _project(node: Node, include_embedding: bool) -> Node:
        if include_embedding or node.embedding is None:
            return node
        return node.model_copy(update={"embedding": None})

    def get_node(self, node_id: str, include_embedding: bool = True) -> Optional[Node]:
        node = self._nodes.get(node_id)
        return None if node is None else self._project(node, include_embedding)

    def get_nodes(self, node_ids: Iterable[str], include_embedding: bool = False) -> list[Node]:
        result: list[Node] = []
        for node_id in dict.fromkeys(node_ids):
            node = self._nodes.get(node_id)
            if node is not None:
                result.append(self._project(node, include_embedding))
        return result

    def list_nodes(
        self, type: Optional[str] = None, include_embedding: bool = False
    ) -> Iterable[Node]:
        for n in sorted(self._nodes.values(), key=lambda n: n.id):
            if type is None or n.type == type:
                yield self._project(n, include_embedding)

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
        """No-op: every write is already durably in-process here."""

    def close(self) -> None:
        self._closed = True

    @property
    def embedding_dim(self) -> int | None:
        """Always ``None`` (see GraphStore.embedding_dim): an in-process dict
        stores whatever vector width it is given — there is no fixed stored
        identity to compare a configured dimension against, so the guard skips.
        """
        return None

    @property
    def is_closed(self) -> bool:
        return self._closed

    def generation(self) -> str:
        """A stable content-derived stamp (see GraphStore.generation).

        InMemoryStore has no on-disk file and no bootstrap-assigned identity
        UUID to reflect (unlike LadybugStore's ``_graph_handle.graph_generation``),
        so this derives a deterministic stamp from current content instead —
        the same digest ``store.index.compute_graph_generation`` already uses
        to detect a stale index. Falls back to a constant if that helper can't
        be imported (kept optional so this module never grows a hard
        dependency on the index package).
        """
        try:
            from okto_neuron.store.index import compute_graph_generation
        except ImportError:
            return "memory"
        return compute_graph_generation(self)

    def health(self) -> BackendHealth:
        """Always healthy: an in-process dict has no separate on-disk state to corrupt."""
        return BackendHealth(healthy=True, detail="in-memory store has no backing file")

    def recovery_status(self) -> RecoveryStatus:
        """Always unrecovered (see GraphStore.recovery_status).

        An in-process dict has no on-disk state to have recovered from.
        """
        return RecoveryStatus(recovered=False)

    def detect_drift(self, expected_generation: str | None) -> DriftReport | None:
        """Always None (see GraphStore.detect_drift).

        No on-disk file exists for another process to have replaced.
        """
        return None
