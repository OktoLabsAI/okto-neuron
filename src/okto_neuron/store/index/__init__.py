"""IndexStore package: the text/vector index port and its default engine.

Public surface: ``IndexStore`` (protocol), ``IndexStats``, ``InMemoryIndexStore``,
``DefaultIndexStore``, ``default_index_store(vault_path)``, ``reindex_all(store,
index)``, and ``compute_graph_generation(store)``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from okto_neuron.store.index.corpus import graph_generation
from okto_neuron.store.index.default import DefaultIndexStore
from okto_neuron.store.index.memory import InMemoryIndexStore
from okto_neuron.store.index.protocol import IndexStats, IndexStore

if TYPE_CHECKING:
    from okto_neuron.store.protocol import GraphStore

__all__ = [
    "IndexStore",
    "IndexStats",
    "InMemoryIndexStore",
    "DefaultIndexStore",
    "default_index_store",
    "reindex_all",
    "compute_graph_generation",
]


def default_index_store(vault_path: Path | str) -> DefaultIndexStore:
    """Construct the default (BM25 + vector-scan) index for a vault."""
    return DefaultIndexStore(vault_path)


def reindex_all(store: "GraphStore", index: IndexStore) -> None:
    """Rebuild the index from scratch off the graph's current nodes."""
    index.clear()
    for node in store.list_nodes(include_embedding=True):
        index.upsert(node)
    index.checkpoint()


def compute_graph_generation(store: "GraphStore") -> str:
    """The generation stamp a consistent index for this store must equal."""
    return graph_generation(store.list_nodes(include_embedding=True))
