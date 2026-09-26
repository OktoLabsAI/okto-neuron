"""Okapi BM25 text scoring over IndexRecord corpora.

The constants, tokenizer and search-text builder below are copied verbatim
from ``okto_neuron.store.ladybug`` (lines 31-33 and 589-598 at the time of the
M1 split) so the new engine reproduces the original scores bit-for-bit,
including float evaluation order. ``_node_search_text`` is duck-typed: it
reads ``.title``, ``.content`` and ``.tags`` off whatever is passed in, so it
works unchanged for both ``Node`` and ``IndexRecord``.
"""

from __future__ import annotations

import math
import string
from collections import Counter
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:
    from okto_neuron.store.index.corpus import IndexRecord

_PUNCTUATION = string.punctuation
_BM25_K1 = 1.5
_BM25_B = 0.75


def _node_search_text(node) -> str:
    return f"{node.title} {node.content} {' '.join(node.tags)}"


def _tokenize(text: str) -> list[str]:
    return [
        token
        for raw_token in (text or "").split()
        if (token := raw_token.lower().strip(_PUNCTUATION))
    ]


class BM25Scorer:
    """Corpus statistics and Okapi BM25 scoring over ``IndexRecord`` rows.

    Mirrors ``LadybugStore._ensure_bm25_stats`` / ``search_text`` (ladybug.py
    at the time of the M1 split) exactly: statistics are computed once over
    the whole corpus at construction time, documents with zero tokens are
    skipped, and search applies a type filter after those global statistics
    were already fixed.
    """

    def __init__(self, records: Iterable["IndexRecord"]) -> None:
        docs: list[tuple["IndexRecord", Counter[str], int]] = []
        df: Counter[str] = Counter()
        total_length = 0
        for record in records:
            term_counts = Counter(_tokenize(_node_search_text(record)))
            doc_length = sum(term_counts.values())
            if doc_length == 0:
                continue
            docs.append((record, term_counts, doc_length))
            df.update(term_counts.keys())
            total_length += doc_length

        self._docs = docs
        self._df = df
        self._avgdl = total_length / len(docs) if docs else 0.0

    @property
    def doc_count(self) -> int:
        return len(self._docs)

    @property
    def avgdl(self) -> float:
        return self._avgdl

    def search(
        self, query: str, k: int = 10, type: Optional[str] = None
    ) -> list[tuple[str, float]]:
        q_tokens = set(_tokenize(query))
        if not q_tokens or k <= 0:
            return []

        if not self._docs or self._avgdl <= 0:
            return []

        doc_count = len(self._docs)
        scored: list[tuple["IndexRecord", float]] = []
        for record, term_counts, doc_length in self._docs:
            if type is not None and record.type != type:
                continue
            score = 0.0
            length_norm = _BM25_K1 * (1 - _BM25_B + _BM25_B * (doc_length / self._avgdl))
            for term in q_tokens:
                term_frequency = term_counts.get(term, 0)
                if term_frequency == 0:
                    continue
                doc_frequency = self._df[term]
                idf = math.log(1 + (doc_count - doc_frequency + 0.5) / (doc_frequency + 0.5))
                score += idf * (term_frequency * (_BM25_K1 + 1)) / (term_frequency + length_norm)
            if score:
                scored.append((record, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return [(record.id, score) for record, score in scored[:k]]
