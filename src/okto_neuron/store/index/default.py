"""Default IndexStore engine: BM25 + full vector scan, backed by JSONL on disk.

Named "ladybug_bm25+vector_scan" per plan 3.8. Needs no configuration and no
external process; the corpus lives at ``<vault>/.marginalia/index/``.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable, Optional

from okto_neuron.core.schema import Node
from okto_neuron.store.index.bm25 import BM25Scorer
from okto_neuron.store.index.corpus import (
    DEFAULT_ENGINE,
    FORMAT_VERSION,
    IndexRecord,
    JsonlCorpusStore,
    graph_generation,
    node_to_record,
)
from okto_neuron.store.index.protocol import IndexStats
from okto_neuron.store.index.vector_scan import scan_vectors

_INDEX_SUBDIR = Path(".marginalia") / "index"


class DefaultIndexStore:
    def __init__(self, vault_path: Path | str) -> None:
        self.vault_path = Path(vault_path)
        self.index_dir = self.vault_path / _INDEX_SUBDIR
        self._corpus_store = JsonlCorpusStore(self.index_dir)
        self._records: dict[str, IndexRecord] = {}
        self._scorer: Optional[BM25Scorer] = None
        if self._corpus_store.exists():
            self._records, _meta = self._corpus_store.load()
        self._dirty = not self._corpus_store.exists()

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
        self._dirty = True

    def delete(self, node_id: str) -> None:
        self._records.pop(node_id, None)
        self._invalidate()
        self._dirty = True

    def clear(self) -> None:
        self._records.clear()
        self._invalidate()
        self._dirty = True

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
        """Unused in M1; see InMemoryIndexStore.invalidate_by_facet."""
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
        """Rewrite corpus.jsonl and meta.json in full."""
        if not self._dirty:
            return
        stamp = self.generation()
        self._corpus_store.save(self._records, graph_generation_stamp=stamp)
        self._dirty = False

    def close(self) -> None:
        pass
