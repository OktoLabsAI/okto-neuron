"""Index record shape, generation stamp, and the JSONL corpus persistence.

Per the M1 spec (part 1 section 3, corrected by part 2 section 7): the corpus
is a flat JSONL file of one record per node plus a meta.json sidecar. The
generation stamp is a digest over every field that can move a search score
(title, content, tags, type, facets.P and embedding), deliberately excluding
created_at, which is pinned at first write and never changes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from okto_neuron.core.schema import Node
from okto_neuron.store.index.bm25 import BM25Scorer

FORMAT_VERSION = 1
DEFAULT_ENGINE = "ladybug_bm25+vector_scan"

CORPUS_FILENAME = "corpus.jsonl"
META_FILENAME = "meta.json"


@dataclass
class IndexRecord:
    id: str
    type: str
    title: str = ""
    content: str = ""
    tags: list[str] = field(default_factory=list)
    facet_p: Optional[str] = None
    embedding: Optional[list[float]] = None


def node_to_record(node: Node) -> IndexRecord:
    """Project a graph Node onto the fields the index needs to persist."""
    return IndexRecord(
        id=node.id,
        type=node.type,
        title=node.title,
        content=node.content,
        tags=list(node.tags),
        facet_p=(node.facets or {}).get("P"),
        embedding=node.embedding,
    )


def _facet_p(obj: Any) -> Any:
    """Read the P facet off either a Node (facets dict) or an IndexRecord."""
    if hasattr(obj, "facet_p"):
        return obj.facet_p
    return (obj.facets or {}).get("P")


def node_digest(node: Any) -> str:
    """Digest of every field that can move a search score, for one record.

    Works on both ``Node`` and ``IndexRecord`` objects: it reads ``facet_p``
    directly when present, and falls back to ``facets.get("P")`` otherwise.
    ``created_at`` is excluded on purpose (pinned at first write, never
    changes).
    """
    payload = json.dumps(
        [
            node.id,
            node.type,
            node.title,
            node.content,
            list(node.tags),
            _facet_p(node),
            node.embedding,
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def graph_generation(nodes_in_ascending_id_order: Iterable[Any]) -> str:
    """Digest over an ascending-id sequence of nodes (or index records)."""
    h = hashlib.sha256()
    for node in nodes_in_ascending_id_order:
        h.update(node_digest(node).encode("ascii"))
    return h.hexdigest()


def _record_to_row(record: IndexRecord) -> dict[str, Any]:
    row = asdict(record)
    return row


def _row_to_record(row: dict[str, Any]) -> IndexRecord:
    return IndexRecord(
        id=row["id"],
        type=row["type"],
        title=row.get("title", ""),
        content=row.get("content", ""),
        tags=list(row.get("tags") or []),
        facet_p=row.get("facet_p"),
        embedding=row.get("embedding"),
    )


class JsonlCorpusStore:
    """Persists an IndexRecord corpus as corpus.jsonl + meta.json.

    Records are rewritten in full on every save (no tombstones, no sqlite —
    M1 keeps this simple, per plan 3.3).
    """

    def __init__(self, index_dir: Path | str) -> None:
        self.index_dir = Path(index_dir)

    @property
    def corpus_path(self) -> Path:
        return self.index_dir / CORPUS_FILENAME

    @property
    def meta_path(self) -> Path:
        return self.index_dir / META_FILENAME

    def exists(self) -> bool:
        return self.corpus_path.exists() and self.meta_path.exists()

    def load(self) -> tuple[dict[str, IndexRecord], dict[str, Any]]:
        """Read the corpus and metadata off disk. Missing files load empty."""
        records: dict[str, IndexRecord] = {}
        if self.corpus_path.exists():
            with self.corpus_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    row = json.loads(line)
                    record = _row_to_record(row)
                    records[record.id] = record

        meta: dict[str, Any] = {}
        if self.meta_path.exists():
            meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
        return records, meta

    def save(
        self,
        records: dict[str, IndexRecord],
        *,
        graph_generation_stamp: str,
        engine: str = DEFAULT_ENGINE,
        format_version: int = FORMAT_VERSION,
    ) -> dict[str, Any]:
        """Rewrite corpus.jsonl and meta.json in full and return the new meta."""
        self.index_dir.mkdir(parents=True, exist_ok=True)
        ordered = sorted(records.values(), key=lambda record: record.id)

        with self.corpus_path.open("w", encoding="utf-8") as handle:
            for record in ordered:
                handle.write(json.dumps(_record_to_row(record), separators=(",", ":"), ensure_ascii=False))
                handle.write("\n")

        scorer = BM25Scorer(ordered)
        embedded_count = sum(1 for record in ordered if record.embedding is not None)
        meta = {
            "format_version": format_version,
            "engine": engine,
            "graph_generation": graph_generation_stamp,
            "doc_count": scorer.doc_count,
            "avgdl": scorer.avgdl,
            "embedded_count": embedded_count,
            "built_at": datetime.now(timezone.utc).isoformat(),
        }
        self.meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return meta
