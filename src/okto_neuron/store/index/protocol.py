"""IndexStore protocol — the text/vector index port, separate from GraphStore.

See the internal ADR 0041 plan section 3.3 and the M1 spec (part 1
section 2). The index no longer holds ``Node`` objects; both the lexical and
vector legs return node ids, and callers resolve them through
``GraphStore.get_nodes`` in one batch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Optional, Protocol, runtime_checkable

from okto_neuron.core.schema import Node


@dataclass(frozen=True)
class IndexStats:
    doc_count: int
    avgdl: float
    embedded_count: int
    graph_generation: str
    engine: str
    format_version: int


@runtime_checkable
class IndexStore(Protocol):
    def upsert(self, node: Node) -> None:
        """Insert or replace the indexed record for node.id.

        Derived from node.title, node.content, node.tags, node.type,
        node.facets.get('P') and node.embedding. Idempotent.
        """
        ...

    def delete(self, node_id: str) -> None:
        """Remove a record; no-op if absent."""
        ...

    def search_text(
        self, query: str, k: int = 10, type: Optional[str] = None
    ) -> list[tuple[str, float]]:
        """Okapi BM25 over the whole corpus.

        Returns at most k (node_id, score) pairs, score > 0, sorted by score
        descending with stable id-order tie-break; type filters AFTER scoring
        statistics are computed over the whole corpus.
        """
        ...

    def scan_vectors(self, type: Optional[str] = None) -> Iterable[tuple[str, list[float]]]:
        """Every record with a non-None embedding, in ascending node_id order.

        Optionally restricted to one type. Untruncated by contract.
        """
        ...

    def search_vector(
        self, embedding: list[float], k: int, type: Optional[str] = None
    ) -> list[tuple[str, float]] | None:
        """Optional top-k ANN. Returns None when the engine has no ANN.

        Not wired into search_claims in M1 (D-11).
        """
        ...

    def invalidate_by_facet(self, predicate: Callable[[dict], bool]) -> int:
        """Optional; unused in M1."""
        ...

    def stats(self) -> IndexStats:
        """doc_count, avgdl, embedded_count, graph_generation, engine, format_version."""
        ...

    def generation(self) -> str:
        """The graph_generation stamp this index was built from or last updated at."""
        ...

    def clear(self) -> None:
        """Drop every record. Used by reindex_all before a full rebuild."""
        ...

    def checkpoint(self) -> None:
        """Flush the corpus store to disk. No-op if already durable per write."""
        ...

    def close(self) -> None: ...
