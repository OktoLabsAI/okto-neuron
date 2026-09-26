from __future__ import annotations

from okto_neuron.core.schema import Edge, Node
from okto_neuron.predicates import (
    PredicateAliasIndex,
    PredicateAliasRecord,
    collect_predicate_vocabulary,
    generate_predicate_candidates,
    shared_argument_evidence,
)
from okto_neuron.store.memory import InMemoryStore


class _ClusterEmbedder:
    def embed(self, text: str) -> list[float]:  # noqa: ARG002
        return [1.0, 0.0]


def _store() -> InMemoryStore:
    store = InMemoryStore()
    for node_id in ("alice", "bob", "carol", "block"):
        store.add_node(Node(id=node_id, type="Agent", title=node_id))
    return store


def _edge(store: InMemoryStore, predicate: str, src: str, dst: str) -> None:
    store.add_edge(Edge(type=predicate, src=src, dst=dst))


def _negative_record(subject: str, obj: str) -> PredicateAliasRecord:
    return PredicateAliasRecord(
        id=f"{subject}-{obj}",
        subject_predicate=subject,
        mapping="distinct",
        object_predicate=obj,
        confidence=0.95,
        justification="negative",
        evidence={},
        judge_model="fake",
        votes={},
        status="rejected",
        created_at="2026-06-12T00:00:00+00:00",
    )


def test_vocabulary_counts_edges_and_claim_facets() -> None:
    store = _store()
    _edge(store, "mentions", "alice", "bob")
    store.add_node(
        Node(
            id="claim-1",
            type="Claim",
            title="Alice founded Acme",
            facets={"S_id": "alice", "P": "founded", "O_id": "bob"},
        )
    )

    vocabulary = collect_predicate_vocabulary(store)
    assert vocabulary["mentions"] == 1
    assert vocabulary["founded"] == 1


def test_candidate_generation_honors_caps_min_support_and_negative_cache(tmp_path) -> None:
    store = _store()
    _edge(store, "alpha", "alice", "bob")
    _edge(store, "alpha", "alice", "carol")
    _edge(store, "alpha_near", "alice", "bob")
    _edge(store, "alpha_near", "bob", "carol")
    _edge(store, "alpha_alias", "alice", "bob")
    _edge(store, "beta", "bob", "alice")

    index = PredicateAliasIndex(tmp_path)
    index.upsert(_negative_record("alpha", "alpha_alias"))

    candidates = generate_predicate_candidates(
        store,
        _ClusterEmbedder(),
        alias_index=index,
        min_support=2,
        max_pairs=30,
    )
    pairs = {candidate.pair for candidate in candidates}
    assert ("alpha", "alpha_alias") not in pairs
    assert ("alpha_alias", "beta") not in pairs
    assert ("alpha", "alpha_near") in pairs

    capped = generate_predicate_candidates(
        store,
        _ClusterEmbedder(),
        alias_index=index,
        min_support=2,
        max_pairs=1,
    )
    assert len(capped) == 1


def test_shared_argument_evidence_counts_same_and_swapped_order() -> None:
    store = _store()
    _edge(store, "shared_by", "alice", "bob")
    _edge(store, "shared_with", "alice", "bob")
    _edge(store, "shared_with", "bob", "alice")

    evidence = shared_argument_evidence(store, "shared_by", "shared_with")

    assert evidence.same_order == 1
    assert evidence.swapped_order == 1
    assert {pair.subject for pair in evidence.pairs} == {"alice"}


def test_candidate_includes_claim_samples_and_argument_signatures() -> None:
    store = _store()
    store.add_node(Node(id="doc-block", type="Block", title="b", content="Source excerpt"))
    store.add_node(
        Node(
            id="claim-1",
            type="Claim",
            title="Alice wrote to Bob",
            facets={"S_id": "alice", "P": "wrote_to", "O_id": "bob", "block_id": "doc-block"},
        )
    )
    store.add_node(
        Node(
            id="claim-2",
            type="Claim",
            title="Alice sent to Bob",
            facets={"S_id": "alice", "P": "sent_to", "O_id": "bob", "block_id": "doc-block"},
        )
    )

    candidates = generate_predicate_candidates(
        store,
        _ClusterEmbedder(),
        min_support=1,
        max_pairs=1,
    )

    candidate = candidates[0]
    assert candidate.samples_a or candidate.samples_b
    assert "Source excerpt" in (candidate.samples_a + candidate.samples_b)[0].source_excerpt
    assert candidate.signatures_a or candidate.signatures_b
