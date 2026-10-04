"""``PredicateStats``: the maintained projection equals the old full-scan answers."""

from __future__ import annotations

import json

import pytest

from okto_neuron.core.schema import Edge, Node
from okto_neuron.predicates import (
    PredicateStats,
    build_predicate_stats,
    collect_predicate_vocabulary,
    generate_predicate_candidates,
    shared_argument_evidence,
)
from okto_neuron.store.memory import InMemoryStore


class _Embedder:
    def embed(self, text: str) -> list[float]:  # noqa: ARG002
        return [1.0, 0.0]


def _fixture_store(size: int) -> InMemoryStore:
    store = InMemoryStore()
    ids = [f"n{i:03d}" for i in range(size)]
    for i, node_id in enumerate(ids):
        store.add_node(Node(id=node_id, type="Agent" if i % 2 else "Concept", title=node_id))
    for i in range(size):
        a, b = ids[i], ids[(i + 1) % size]
        store.add_edge(Edge(id=f"e-a{i}", type="alpha", src=a, dst=b))
        if i % 2 == 0:
            store.add_edge(Edge(id=f"e-n{i}", type="alpha_near", src=a, dst=b))
        if i % 3 == 0:
            store.add_edge(Edge(id=f"e-s{i}", type="alpha_near", src=b, dst=a))
        store.add_node(
            Node(
                id=f"claim{i}",
                type="Claim",
                title=f"{a} rel {b}",
                facets={"S_id": a, "O_id": b, "P": "alpha_alias" if i % 2 else "alpha",
                        "source_excerpt": f"excerpt {i}"},
            )
        )
    return store


@pytest.mark.parametrize("size", [0, 1, 6, 40])
def test_stats_reproduce_the_full_scan_answers(size: int) -> None:
    store = _fixture_store(size)
    stats = build_predicate_stats(store)
    assert stats.vocabulary == collect_predicate_vocabulary(store)
    for a, b in (("alpha", "alpha_near"), ("alpha", "alpha_alias"), ("alpha_near", "alpha_alias")):
        assert shared_argument_evidence(None, a, b, stats=stats) == shared_argument_evidence(
            store, a, b
        )
    scanned = generate_predicate_candidates(store, _Embedder(), min_support=1, max_pairs=30)
    projected = generate_predicate_candidates(
        None, _Embedder(), stats=stats, min_support=1, max_pairs=30
    )
    assert projected == scanned
    if size >= 6:
        assert scanned, "fixture must produce candidates or the equality proves nothing"


def test_payload_round_trips_through_json() -> None:
    stats = build_predicate_stats(_fixture_store(12))
    restored = PredicateStats.from_payload(json.loads(json.dumps(stats.to_payload())))
    assert restored == stats
    assert generate_predicate_candidates(
        None, _Embedder(), stats=restored, min_support=1
    ) == generate_predicate_candidates(None, _Embedder(), stats=stats, min_support=1)


@pytest.mark.parametrize(
    "payload",
    [{}, {"vocabulary": {"a": "x"}, "argument_pairs": {}, "signatures": {}, "samples": {}},
     {"vocabulary": {}, "argument_pairs": {"p": [[1]]}, "signatures": {}, "samples": {}}],
)
def test_malformed_payload_is_rejected(payload: dict) -> None:
    with pytest.raises((KeyError, ValueError, TypeError)):
        PredicateStats.from_payload(payload)


def test_a_scan_reads_no_vectors() -> None:
    store = _fixture_store(6)
    seen: list[bool] = []
    original_list, original_get = store.list_nodes, store.get_nodes
    store.list_nodes = lambda type=None, include_embedding=False: (  # type: ignore[method-assign]
        seen.append(include_embedding) or original_list(type, include_embedding=include_embedding)
    )
    store.get_nodes = lambda ids, include_embedding=False: (  # type: ignore[method-assign]
        seen.append(include_embedding) or original_get(ids, include_embedding=include_embedding)
    )
    build_predicate_stats(store)
    assert seen and not any(seen)
