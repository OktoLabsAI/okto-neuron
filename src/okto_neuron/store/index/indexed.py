"""IndexedStore: a GraphStore wrapper that keeps an IndexStore in sync (M1, D-20).

Every node write goes to the graph first and then to the index, so the index
is a rebuildable cache that never lags the durable copy inside one process.
Edges never touch the index. Any attribute the wrapper does not define is
delegated to the wrapped store, so backend-specific helpers stay reachable
through Vault.store during the M1 extraction.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from okto_neuron.core.schema import Edge, Node
from okto_neuron.store.index.protocol import IndexStore
from okto_neuron.store.protocol import BackendHealth, DriftReport, GraphStore, RecoveryStatus


class IndexedStore:
    def __init__(self, store: GraphStore, index: IndexStore) -> None:
        self._store = store
        self._index = index
        # Optional capability protocols (e.g. integrity.EdgeAdjacencyReader) are
        # matched by @runtime_checkable isinstance() checks, which use
        # inspect.getattr_static and therefore never reach __getattr__ below
        # (deliberately, so isinstance() cannot trigger side effects). Mirror
        # any such capability the wrapped store actually has as a real
        # instance attribute so those isinstance() checks keep working through
        # the wrapper; skip it entirely when the wrapped store lacks it, so a
        # non-adjacency-capable backend still reports "capability absent".
        list_edge_adjacency = getattr(store, "list_edge_adjacency", None)
        if list_edge_adjacency is not None:
            self.list_edge_adjacency = list_edge_adjacency

    @property
    def graph(self) -> GraphStore:
        """The raw graph store (for reindex, heal and tests)."""
        return self._store

    @property
    def index(self) -> IndexStore:
        return self._index

    def add_node(self, node: Node, clear_embedding: bool = False) -> None:
        if clear_embedding:
            self._store.add_node(node, clear_embedding=True)
            # The index keeps a record's vector when the node has none, so an
            # explicit clear drops the record first.
            self._index.delete(node.id)
        else:
            self._store.add_node(node)
        self._index.upsert(node)

    def add_edge(self, edge: Edge) -> None:
        self._store.add_edge(edge)

    def get_node(self, node_id: str, include_embedding: bool = True) -> Optional[Node]:
        return self._store.get_node(node_id, include_embedding=include_embedding)

    def get_nodes(self, node_ids: Iterable[str], include_embedding: bool = False) -> list[Node]:
        return self._store.get_nodes(node_ids, include_embedding=include_embedding)

    def list_nodes(
        self, type: Optional[str] = None, include_embedding: bool = False
    ) -> Iterable[Node]:
        return self._store.list_nodes(type=type, include_embedding=include_embedding)

    def list_edges(
        self,
        src: Optional[str] = None,
        dst: Optional[str] = None,
        type: Optional[str] = None,
    ) -> Iterable[Edge]:
        return self._store.list_edges(src=src, dst=dst, type=type)

    def checkpoint(self) -> None:
        self._store.checkpoint()
        self._index.checkpoint()

    def close(self) -> None:
        try:
            self._index.checkpoint()
            self._index.close()
        finally:
            self._store.close()

    @property
    def is_closed(self) -> bool:
        return self._store.is_closed

    def generation(self) -> str:
        return self._store.generation()

    def health(self) -> BackendHealth:
        return self._store.health()

    def recovery_status(self) -> RecoveryStatus:
        return self._store.recovery_status()

    def detect_drift(self, expected_generation: str | None) -> DriftReport | None:
        return self._store.detect_drift(expected_generation)

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes the wrapper does not define itself.
        return getattr(self._store, name)
