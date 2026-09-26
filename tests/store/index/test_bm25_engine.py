"""Hand-computed BM25 checks for the extracted engine (M1 T1).

The expected numbers are computed inside the test with plain math from the
Okapi BM25 definition, independently of okto_neuron.store.index.bm25, so a
drift in the engine cannot silently re-derive its own expectation.
"""

from __future__ import annotations

import math

from okto_neuron.store.index.bm25 import _BM25_B, _BM25_K1, BM25Scorer, _tokenize
from okto_neuron.store.index.corpus import IndexRecord


def _rec(id: str, type: str, title: str, content: str = "", tags=()) -> IndexRecord:
    return IndexRecord(id=id, type=type, title=title, content=content, tags=list(tags))


def _bm25(tf: int, df: int, n: int, dl: int, avgdl: float) -> float:
    idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
    length_norm = _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / avgdl)
    return idf * (tf * (_BM25_K1 + 1)) / (tf + length_norm)


def test_constants_match_ladybug_values():
    assert _BM25_K1 == 1.5
    assert _BM25_B == 0.75


def test_tokenizer_lowercases_and_strips_punctuation():
    assert _tokenize("Hello, World! (graph)") == ["hello", "world", "graph"]
    assert _tokenize("") == []
    assert _tokenize("...") == []


def test_three_document_scores_match_hand_computation():
    # a: graph store ladybug graph   (dl 4, tf graph 2)
    # b: vector index cosine scan    (dl 4, tf index 1)
    # c: graph index graph graph     (dl 4, tf graph 3, tf index 1)
    docs = [
        _rec("a", "Concept", "graph store", "ladybug graph"),
        _rec("b", "Concept", "vector index", "cosine scan"),
        _rec("c", "Claim", "graph", "index graph graph"),
    ]
    scorer = BM25Scorer(docs)
    n = 3
    avgdl = 4.0
    assert scorer.doc_count == n
    assert scorer.avgdl == avgdl
    expected_a = _bm25(2, 2, n, 4, avgdl)
    expected_b = _bm25(1, 2, n, 4, avgdl)
    expected_c = _bm25(3, 2, n, 4, avgdl) + _bm25(1, 2, n, 4, avgdl)
    result = dict(scorer.search("graph index", k=10))
    assert set(result) == {"a", "b", "c"}
    assert abs(result["a"] - expected_a) < 1e-12
    assert abs(result["b"] - expected_b) < 1e-12
    assert abs(result["c"] - expected_c) < 1e-12
    ranked = [doc_id for doc_id, _ in scorer.search("graph index", k=10)]
    assert ranked == ["c", "a", "b"]


def test_zero_token_documents_are_skipped_from_statistics():
    docs = [
        _rec("a", "Concept", "graph"),
        _rec("b", "Concept", "", ""),
        _rec("c", "Concept", "...", ""),
    ]
    scorer = BM25Scorer(docs)
    assert scorer.doc_count == 1
    assert scorer.avgdl == 1.0


def test_type_filter_applies_after_global_statistics():
    docs = [
        _rec("a", "Concept", "graph"),
        _rec("b", "Claim", "graph"),
        _rec("c", "Claim", "other"),
    ]
    scorer = BM25Scorer(docs)
    unfiltered = dict(scorer.search("graph", k=10))
    filtered = dict(scorer.search("graph", k=10, type="Claim"))
    assert set(filtered) == {"b"}
    # Same score as the unfiltered run: n=3 and df=2 come from the whole corpus.
    assert filtered["b"] == unfiltered["b"]
    assert abs(filtered["b"] - _bm25(1, 2, 3, 1, 1.0)) < 1e-12


def test_k_truncation_and_stable_tie_order():
    docs = [_rec(f"id{i:02d}", "Concept", "graph") for i in range(5)]
    scorer = BM25Scorer(docs)
    ranked = [doc_id for doc_id, _ in scorer.search("graph", k=3)]
    # Equal scores keep corpus order (ascending id) under the stable sort.
    assert ranked == ["id00", "id01", "id02"]
    assert scorer.search("graph", k=0) == []
    assert scorer.search("", k=3) == []
    assert scorer.search("nomatch", k=3) == []
