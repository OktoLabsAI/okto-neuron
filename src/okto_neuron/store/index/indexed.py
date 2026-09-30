"""IndexedStore: a GraphStore wrapper that keeps an IndexStore in sync (M1, D-20).

Every node write goes to the graph first and then to the index, so the index
is a rebuildable cache that never lags the durable copy inside one process.
Edges never touch the index. Any attribute the wrapper does not define is
delegated to the wrapped store, so backend-specific helpers stay reachable
through Vault.store during the M1 extraction.
"""

from __future__ import annotations

import threading
import uuid
from typing import Any, Callable, Iterable, Optional

from okto_neuron.core.schema import Edge, Node
from okto_neuron.store.index.protocol import IndexStore
from okto_neuron.store.protocol import BackendHealth, DriftReport, GraphStore, RecoveryStatus


#: Backend-specific bulk/destructive writers reachable through ``__getattr__`` (not part of
#: ``GraphStore``). Each one is wrapped so it bumps ``write_seq`` like ``add_node`` does.
_BACKEND_MUTATORS = frozenset({"add_nodes", "add_edges", "wipe"})


class IndexedStore:
    def __init__(self, store: GraphStore, index: IndexStore) -> None:
        self._store = store
        self._index = index
        # Change detection for derived projections (see server/_projection.py): a uuid
        # minted per store object plus a counter bumped once per completed mutation. A
        # projection built at (generation, token, seq) is current exactly while all three
        # still match. The counter moves AFTER the write (in a finally, so a write that
        # raised midway also counts): a reader that saw seq N before scanning can never
        # have missed a write that finished before its scan started.
        self._instance_token = uuid.uuid4().hex
        self._write_seq = 0
        self._seq_lock = threading.Lock()
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
    def instance_token(self) -> str:
        """Identity of this store object (a fresh one after every reopen or swap)."""
        return self._instance_token

    @property
    def write_seq(self) -> int:
        """Number of mutations completed through this facade since it was created."""
        return self._write_seq

    def _bump_write_seq(self) -> None:
        with self._seq_lock:
            self._write_seq += 1

    @property
    def graph(self) -> GraphStore:
        """The raw graph store (for reindex, heal and tests)."""
        return self._store

    @property
    def index(self) -> IndexStore:
        return self._index

    def add_node(self, node: Node, clear_embedding: bool = False) -> None:
        try:
            if clear_embedding:
                self._store.add_node(node, clear_embedding=True)
                # The index keeps a record's vector when the node has none, so an
                # explicit clear drops the record first.
                self._index.delete(node.id)
            else:
                self._store.add_node(node)
            self._index.upsert(node)
        finally:
            self._bump_write_seq()

    def add_edge(self, edge: Edge) -> None:
        try:
            self._store.add_edge(edge)
        finally:
            self._bump_write_seq()

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
        attr = getattr(self._store, name)
        if name in _BACKEND_MUTATORS and callable(attr):
            return self._counted(attr)
        return attr

    def _counted(self, method: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return method(*args, **kwargs)
            finally:
                self._bump_write_seq()

        return call
