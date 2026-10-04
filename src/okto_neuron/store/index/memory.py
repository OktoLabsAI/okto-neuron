"""In-memory IndexStore — the "memory" engine parameter of the contract suite.

Runs the exact same BM25 and vector-scan code path as the default engine over
records held only in a dict, with no disk persistence. Used by tests that
need an index without a vault on disk.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Iterable, Optional

from okto_neuron.core.schema import Node
from okto_neuron.store.index.bm25 import BM25Scorer
from okto_neuron.store.index.corpus import (
    DEFAULT_ENGINE,
    FORMAT_VERSION,
    IndexRecord,
    graph_generation,
    node_to_record,
)
from okto_neuron.store.index.protocol import IndexStats
from okto_neuron.store.index.vector_scan import scan_vectors


class InMemoryIndexStore:
    def __init__(self) -> None:
        self._records: dict[str, IndexRecord] = {}
        self._scorer: Optional[BM25Scorer] = None

    def _invalidate(self) -> None:
        self._scorer = None

    def _ensure_scorer(self) -> BM25Scorer:
        if self._scorer is None:
            ordered = sorted(self._records.values(), key=lambda record: record.id)
            self._scorer = BM25Scorer(ordered)
        return self._scorer

    def upsert(self, node: Node) -> None:
        record = node_to_record(node)
        previous = self._records.get(node.id)
        if record.embedding is None and previous is not None and previous.embedding is not None:
            # Same contract as GraphStore.add_node: a node without a vector keeps the
            # vector already indexed for that id (explicit clears delete first).
            record = replace(record, embedding=previous.embedding)
        self._records[node.id] = record
        self._invalidate()

    def delete(self, node_id: str) -> None:
        self._records.pop(node_id, None)
        self._invalidate()

    def clear(self) -> None:
        self._records.clear()
        self._invalidate()

    def search_text(
        self, query: str, k: int = 10, type: Optional[str] = None
    ) -> list[tuple[str, float]]:
        return self._ensure_scorer().search(query, k=k, type=type)

    def scan_vectors(self, type: Optional[str] = None) -> Iterable[tuple[str, list[float]]]:
        return scan_vectors(self._records.values(), type=type)

    def search_vector(
        self, embedding: list[float], k: int, type: Optional[str] = None
    ) -> list[tuple[str, float]] | None:
        return None

    def invalidate_by_facet(self, predicate: Callable[[dict], bool]) -> int:
        """Unused in M1; there is no separate cache beyond the BM25 stats,
        which already recompute lazily on the next search. Returns the count
        of records the predicate matches, for parity with the protocol shape.
        """
        return sum(1 for record in self._records.values() if predicate({"P": record.facet_p}))

    def generation(self) -> str:
        ordered = sorted(self._records.values(), key=lambda record: record.id)
        return graph_generation(ordered)

    def stats(self) -> IndexStats:
        scorer = self._ensure_scorer()
        embedded_count = sum(1 for record in self._records.values() if record.embedding is not None)
        return IndexStats(
            doc_count=scorer.doc_count,
            avgdl=scorer.avgdl,
            embedded_count=embedded_count,
            graph_generation=self.generation(),
            engine=DEFAULT_ENGINE,
            format_version=FORMAT_VERSION,
        )

    def checkpoint(self) -> None:
        """No-op: every write is already durably in-process here."""

    def close(self) -> None:
        pass
