"""IndexStore contract: both engines reproduce the pinned expected.json exactly.

expected.json was generated ONCE from the pre-extraction LadybugStore scorer
(fixtures/regenerate_expected.py); these tests pin the scoring formula for
every backend and engine that follows (plan decision D-05).
"""

from __future__ import annotations

from okto_neuron.query import _cosine, node_quality_weight
from okto_neuron.store.index import compute_graph_generation
from okto_neuron.store.schema import SCHEMA_METADATA_NODE_ID

TOL = 1e-9


def _vector_leg(index, store, query_embedding, node_type):
    pairs = list(index.scan_vectors(type=node_type))
    nodes = store.get_nodes([node_id for node_id, _ in pairs], include_embedding=True)
    scored = []
    for node in nodes:
        score = _cosine(query_embedding, node.embedding) * node_quality_weight(node)
        if score > 0.0:
            scored.append((node.id, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored


def _embedding_for_case(expected, case_index):
    filters = len(expected["cases"]) // len(expected["query_embeddings"])
    return expected["query_embeddings"][case_index // filters]


def test_search_text_matches_pinned_expectations(populated, populated_index, expected):
    for case in expected["cases"]:
        actual = populated_index.search_text(case["query"], k=expected["k"], type=case["type"])
        want = [(i, s) for i, s in case["lexical_top20"]]
        assert [i for i, _ in actual] == [i for i, _ in want], (case["query"], case["type"])
        for (_, a), (_, e) in zip(actual, want):
            assert abs(a - e) < TOL, (case["query"], case["type"])


def test_vector_leg_matches_pinned_expectations(populated, populated_index, expected):
    for case_index, case in enumerate(expected["cases"]):
        query_embedding = _embedding_for_case(expected, case_index)
        actual = _vector_leg(populated_index, populated, query_embedding, case["type"])
        assert len(actual) == case["vector_total"], (case["query"], case["type"])
        want = [(i, s) for i, s in case["vector_top20"]]
        head = actual[: len(want)]
        assert [i for i, _ in head] == [i for i, _ in want], (case["query"], case["type"])
        for (_, a), (_, e) in zip(head, want):
            assert abs(a - e) < TOL, (case["query"], case["type"])


def test_scan_vectors_is_untruncated_and_ordered(populated, populated_index, corpus_nodes):
    ids = [i for i, _ in populated_index.scan_vectors()]
    embedded = sorted(n.id for n in corpus_nodes if n.embedding is not None)
    assert ids == embedded
    assert SCHEMA_METADATA_NODE_ID not in ids
    assert populated_index.stats().embedded_count == len(embedded)


def test_upsert_delete_and_generation(populated, populated_index, corpus_nodes):
    assert populated_index.generation() == compute_graph_generation(populated)
    victim = corpus_nodes[0]
    populated_index.delete(victim.id)
    assert populated_index.generation() != compute_graph_generation(populated)
    assert victim.id not in {i for i, _ in populated_index.scan_vectors()}
    populated_index.upsert(populated.get_node(victim.id))
    assert populated_index.generation() == compute_graph_generation(populated)


def test_type_filter_uses_global_statistics(populated_index, expected):
    case_all = expected["cases"][0]
    case_claim = expected["cases"][1]
    assert case_all["type"] is None and case_claim["type"] == "Claim"
    scores_all = dict(populated_index.search_text(case_all["query"], k=60))
    for node_id, score in populated_index.search_text(case_claim["query"], k=60, type="Claim"):
        assert abs(scores_all[node_id] - score) < TOL


def test_checkpoint_writes_records_before_the_stamp(tmp_path, monkeypatch):
    import json

    import pytest

    from okto_neuron.core.schema import Node
    from okto_neuron.store.index import DefaultIndexStore
    from okto_neuron.store.index import corpus

    index = DefaultIndexStore(tmp_path / "vault")
    index.upsert(Node(id="a1", type="Concept", title="first"))
    index.checkpoint()
    old_stamp = index.generation()

    replaced: list[str] = []
    real_replace = corpus.os.replace

    def replace(src, dst):
        replaced.append(dst.name)
        if dst.name == "meta.json":
            raise OSError("crash before the stamp lands")
        real_replace(src, dst)

    monkeypatch.setattr(corpus.os, "replace", replace)
    index.upsert(Node(id="a2", type="Concept", title="second"))
    with pytest.raises(OSError):
        index.checkpoint()
    assert replaced == ["corpus.jsonl", "meta.json"]

    # The records landed, the stamp still names the previous corpus: a reopen sees
    # a stamp that cannot claim records which are not on disk.
    meta = json.loads((index.index_dir / "meta.json").read_text())
    assert meta["graph_generation"] == old_stamp
    reopened = DefaultIndexStore(tmp_path / "vault")
    assert reopened.generation() == old_stamp
    assert reopened.recompute_generation() != old_stamp
