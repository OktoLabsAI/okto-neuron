"""Layer 2 ranking validation with the REAL fastembed embedder (no LLM, no mocks).

Seeds an InMemoryStore with real BAAI/bge-small-en-v1.5 embeddings for a rich
content Claim, two noise nodes (a metadata "tag" Claim and a bare "v1" Concept),
and a legit short entity "QZX". Asserts the quality weight demotes the noise
below the rich Claim and leaves the legit short entity un-penalized (weight 1.0).

Captures BEFORE (all weights forced to 1.0) and AFTER ordered scores so the test
output is the load-bearing evidence for the fix.
"""

from __future__ import annotations

import pytest

import okto_neuron.query as query
from okto_neuron.core.schema import Node
from okto_neuron.embed import get_provider
from okto_neuron.query import node_quality_weight, search_claims
from okto_neuron.store.memory import InMemoryStore


@pytest.fixture(scope="module")
def embedder():
    emb = get_provider("fastembed")
    if type(emb).__name__ != "_FastEmbedProvider":
        pytest.skip("fastembed unavailable; real-embedder test requires it")
    return emb


def _build_store(embedder) -> InMemoryStore:
    store = InMemoryStore()
    # Rich, real content Claim — the assertion that actually answers the query.
    rich = "Derek hands_over_to Avery Stone"
    store.add_node(
        Node(
            id="claim-rich",
            type="Claim",
            title=rich,
            content=rich,
            facets={"P": "hands_over_to"},
            embedding=list(embedder.embed(rich)),
        )
    )
    # Noise Claim minted from a metadata predicate — predicate carried in facet P.
    tag_text = "Derek tag 9"
    store.add_node(
        Node(
            id="claim-tag",
            type="Claim",
            title=tag_text,
            content=tag_text,
            facets={"P": "tag"},
            embedding=list(embedder.embed(tag_text)),
        )
    )
    # Bare placeholder Concept — a version token, no entity value.
    store.add_node(
        Node(
            id="concept-v1",
            type="Concept",
            title="v1",
            content="v1",
            embedding=list(embedder.embed("v1")),
        )
    )
    # Legit short entity — must NOT be penalized.
    store.add_node(
        Node(
            id="concept-qzx",
            type="Concept",
            title="QZX",
            content="QZX leadership team",
            embedding=list(embedder.embed("QZX leadership team")),
        )
    )
    return store


def _ranked(embedder, store) -> list[tuple[str, float]]:
    hits = search_claims("leadership handover", k=10, store=store, embedder=embedder)
    return [(n.id, round(s, 6)) for n, s in hits]


def test_quality_weight_demotes_noise(embedder, monkeypatch) -> None:
    store = _build_store(embedder)

    # BEFORE: force every weight to 1.0 to capture the un-weighted baseline order.
    monkeypatch.setattr(query, "node_quality_weight", lambda _node: 1.0)
    before = _ranked(embedder, store)
    monkeypatch.undo()

    # AFTER: real weights applied.
    after = _ranked(embedder, store)

    print("\nBEFORE (all weights 1.0):")
    for nid, score in before:
        print(f"  {nid:14s} {score}")
    print("AFTER (quality-weighted):")
    for nid, score in after:
        print(f"  {nid:14s} {score}")

    rank = {nid: i for i, (nid, _) in enumerate(after)}
    # Rich content Claim outranks both noise nodes.
    assert rank["claim-rich"] < rank["claim-tag"]
    assert rank["claim-rich"] < rank["concept-v1"]

    # Legit short entity carries no penalty.
    qzx = store.get_node("concept-qzx")
    assert qzx is not None
    assert node_quality_weight(qzx) == 1.0
    # Noise nodes are penalized (weight < 1.0) but still > 0 (not removed).
    tag = store.get_node("claim-tag")
    assert tag is not None
    assert 0.0 < node_quality_weight(tag) < 1.0
    source_claim = Node(
        id="claim-source",
        type="Claim",
        title="Competency Pillars source Interview extraction",
        content="Competency Pillars source Interview extraction",
        facets={"P": "source"},
    )
    assert 0.0 < node_quality_weight(source_claim) < 1.0
    version_claim = Node(
        id="claim-version",
        type="Claim",
        title="Competency Pillars is_version 1.0",
        content="Competency Pillars is_version 1.0",
        facets={"P": "is_version"},
    )
    assert 0.0 < node_quality_weight(version_claim) < 1.0
    v1 = store.get_node("concept-v1")
    assert v1 is not None
    assert 0.0 < node_quality_weight(v1) < 1.0
