"""Unit tests for the unified fused retriever (`okto_neuron.query.search_claims`).

Fully isolated from Track A: nodes + embeddings are hand-seeded directly into an
InMemoryStore. No real minted Claims, no network — StubEmbedder (dim 384) only.

StubEmbedder is hash-seeded (not semantic), so we seed a node's embedding equal
to the query embedding when we need a deterministic cosine=1.0 vector hit.
"""

from __future__ import annotations

from okto_neuron.core.schema import Edge, Node
from okto_neuron.embed import StubEmbedder
from okto_neuron.query import search_claims
from okto_neuron.store.memory import InMemoryStore


def test_rrf_returns_mixed_types() -> None:
    embedder = StubEmbedder()
    store = InMemoryStore()
    query = "alpha beta gamma"
    qvec = embedder.embed(query)

    # Concept is a strong VECTOR hit (embedding == query vector).
    store.add_node(
        Node(
            id="concept-1",
            type="Concept",
            title="alpha beta",
            content="alpha beta gamma",
            embedding=qvec,
        )
    )
    # Claim is a strong LEXICAL hit (token overlap), distinct embedding.
    store.add_node(
        Node(
            id="claim-1",
            type="Claim",
            title="alpha beta gamma",
            content="alpha beta gamma delta",
            embedding=embedder.embed("unrelated vector"),
        )
    )
    store.add_node(
        Node(
            id="agent-1",
            type="Agent",
            title="zeta",
            content="nothing relevant",
            embedding=embedder.embed("zeta"),
        )
    )

    hits = search_claims(query, k=5, store=store, embedder=embedder)

    ids = {node.id for node, _ in hits}
    types = {node.type for node, _ in hits}
    assert {"concept-1", "claim-1"} <= ids
    assert len(types) >= 2  # mixed types surfaced, not Claim-only


def test_non_claim_can_outrank_claim() -> None:
    embedder = StubEmbedder()
    store = InMemoryStore()
    query = "quantum entanglement theory"
    qvec = embedder.embed(query)

    # Concept wins BOTH legs: cosine=1.0 (embedding == query) and full lexical overlap.
    store.add_node(
        Node(
            id="concept-top",
            type="Concept",
            title="quantum entanglement",
            content="quantum entanglement theory",
            embedding=qvec,
        )
    )
    # Claim is weak on both legs.
    store.add_node(
        Node(
            id="claim-weak",
            type="Claim",
            title="quantum",
            content="quantum only",
            embedding=embedder.embed("noise"),
        )
    )

    hits = search_claims(query, k=5, store=store, embedder=embedder)

    assert hits[0][0].id == "concept-top"
    assert hits[0][0].type == "Concept"  # no Claims-first short-circuit


def test_semantic_only_zero_overlap_recall_floor() -> None:
    embedder = StubEmbedder()
    store = InMemoryStore()
    query = "photosynthesis chlorophyll sunlight"
    qvec = embedder.embed(query)

    # Title/content share NO tokens with the query — reachable ONLY via cosine.
    store.add_node(
        Node(
            id="semantic-only",
            type="InformationObject",
            title="xxx yyy",
            content="zzz www",
            embedding=qvec,
        )
    )
    store.add_node(
        Node(
            id="lexical-noise",
            type="Concept",
            title="qqq",
            content="rrr",
            embedding=embedder.embed("noise"),
        )
    )

    hits = search_claims(query, k=5, store=store, embedder=embedder)

    ids = {node.id for node, _ in hits}
    assert "semantic-only" in ids  # full-scan cosine recall floor, zero token overlap


def test_graph_expansion_surfaces_connected_claim() -> None:
    embedder = StubEmbedder()
    store = InMemoryStore()
    query = "Jordan Lee partner"
    qvec = embedder.embed(query)

    # Matched entity (lexical + vector hit).
    store.add_node(
        Node(
            id="agent-jl",
            type="Agent",
            title="Jordan Lee",
            content="Jordan Lee partner",
            embedding=qvec,
        )
    )
    # A Claim with NO overlap to the query — only reachable via the edge.
    store.add_node(
        Node(
            id="claim-linked",
            type="Claim",
            title="xyzzy",
            content="completely unrelated text",
            embedding=embedder.embed("noise"),
        )
    )
    store.add_edge(Edge(id="e1", type="project_partner", src="agent-jl", dst="claim-linked"))

    hits = search_claims(query, k=10, store=store, embedder=embedder)

    ids = {node.id for node, _ in hits}
    assert "agent-jl" in ids
    assert "claim-linked" in ids  # surfaced by 1-hop graph expansion


def test_type_filter_does_not_under_return() -> None:
    # Concepts lexically dominate; a global top-20 cut would starve Claims. A
    # type="Claim" query must still recover the full top-k OF THAT TYPE.
    embedder = StubEmbedder()
    store = InMemoryStore()
    for i in range(30):
        store.add_node(
            Node(
                id=f"concept-{i}",
                type="Concept",
                title="alpha beta",
                content="alpha beta gamma",
                embedding=embedder.embed(f"c{i}"),
            )
        )
    for i in range(8):
        store.add_node(
            Node(
                id=f"claim-{i}",
                type="Claim",
                title="alpha",
                content="alpha beta",
                embedding=embedder.embed(f"cl{i}"),
            )
        )

    hits = search_claims("alpha beta", k=5, store=store, embedder=embedder, type="Claim")

    assert hits, "type-scoped query returned nothing"
    assert all(node.type == "Claim" for node, _ in hits)
    # All 8 Claims recovered despite 30 lexically-dominant Concepts — a global
    # top-20 cut then type-filter would have starved them to a thin residue.
    assert len({node.id for node, _ in hits}) == 8


def test_interrogative_query_surfaces_answer_claim_top1() -> None:
    # The bare entity the question is ABOUT ('Okto Neuron') is a strong vector+
    # lexical hit; the answer Claim covers the whole question intent (partner +
    # Okto Neuron). For an interrogative query the coverage-proportional rerank
    # must lift the full-coverage answer Claim to #1.
    embedder = StubEmbedder()
    store = InMemoryStore()
    query = "who is the partner on Okto Neuron?"
    qvec = embedder.embed(query)

    store.add_node(
        Node(
            id="io-marg",
            type="InformationObject",
            title="Okto Neuron",
            content="Okto Neuron",
            embedding=qvec,
        )
    )
    store.add_node(
        Node(
            id="agent-jl",
            type="Agent",
            title="Jordan Lee Carter",
            content="Jordan Lee Carter",
            embedding=qvec,
        )
    )
    store.add_node(
        Node(
            id="answer-claim",
            type="Claim",
            title="Jordan Lee Carter project partner on Okto Neuron",
            content="Jordan Lee Carter is the project partner on Okto Neuron",
            embedding=embedder.embed("noise vector"),
        )
    )

    hits = search_claims(query, k=10, store=store, embedder=embedder)

    assert hits[0][0].id == "answer-claim"
    assert hits[0][0].type == "Claim"


def test_interrogative_answer_partition_outranks_higher_fused_score() -> None:
    """Pin that the final ``_answer_ranked`` partition IS applied.

    Owner decision 2026-07-28 (ADR 0040 addendum): the partition was removed in
    the ADR 0039/0040 landing commit and reinstated after a measured A/B (the removal cost the
    deterministic floor 1 hit and 0.1742 MRR@10). The discriminating property is
    that a Claim covering the query's content terms is partitioned AHEAD of a
    Claim with a strictly higher fused score but zero term coverage. A silent
    removal makes the ordering fall back to fused-score order and fails here.
    """
    embedder = StubEmbedder()
    store = InMemoryStore()
    query = "which database does cloudbox use?"
    qvec = embedder.embed(query)

    # Zero coverage of the content terms {database, cloudbox, use} — it only
    # overlaps the wh-word and the stopword — but wins the vector leg outright
    # (cosine 1.0), so fused order alone would interleave it among the siblings.
    store.add_node(
        Node(
            id="vector-top",
            type="Claim",
            title="which does Redwood ledger snapshot",
            content="which does Redwood ledger snapshot rotated overnight",
            embedding=qvec,
        )
    )
    store.add_node(
        Node(
            id="term-coverage",
            type="Claim",
            title="CloudBox database use PostgreSQL",
            content="CloudBox database use PostgreSQL as its engine",
            embedding=embedder.embed("unrelated vector"),
        )
    )
    for index in range(8):
        store.add_node(
            Node(
                id=f"sib-{index}",
                type="Claim",
                title=f"CloudBox database use detail {index}",
                content=f"CloudBox database use detail {index}",
                embedding=embedder.embed(f"noise {index}"),
            )
        )

    hits = search_claims(query, k=10, store=store, embedder=embedder)
    ranked = [node.id for node, _score in hits]
    scores = {node.id: score for node, score in hits}

    # 1. Bucket property: every term-covering Claim is partitioned ahead of the
    #    zero-coverage Claim, even though its fused score does not put it last.
    assert ranked[-1] == "vector-top"
    assert scores["vector-top"] > min(scores.values())

    # 2. Within the covering bucket the partition's own order wins over the
    #    fused score: sib-1 carries a strictly LOWER fused score than sib-3 yet
    #    ranks above it (equal IDF coverage, tie-broken by node id ascending).
    assert scores["sib-1"] < scores["sib-3"]
    assert ranked.index("sib-1") < ranked.index("sib-3")

    # 3. Corollary: the public order is therefore NOT fused-score-monotonic.
    #    Removing the partition restores a strictly descending order and fails
    #    every assertion above.
    ordered_scores = [score for _node, score in hits]
    assert ordered_scores != sorted(ordered_scores, reverse=True)


def test_interrogative_rerank_ignores_structural_heading_claims() -> None:
    embedder = StubEmbedder()
    store = InMemoryStore()
    query = "who is the partner on Okto Neuron?"
    qvec = embedder.embed(query)

    store.add_node(
        Node(
            id="heading-claim",
            type="Claim",
            title="heading: Okto Neuron project note",
            content="Okto Neuron project note has heading Okto Neuron project note",
            facets={"P": "has_heading"},
            embedding=qvec,
        )
    )
    store.add_node(
        Node(
            id="answer-claim",
            type="Claim",
            title="Jordan Lee Carter project_partner_on Okto Neuron",
            content="Jordan Lee Carter is the project partner on Okto Neuron",
            embedding=embedder.embed("noise vector"),
        )
    )

    hits = search_claims(query, k=10, store=store, embedder=embedder)

    assert hits[0][0].id == "answer-claim"


def test_answer_list_is_idf_weighted_not_plain_coverage() -> None:
    # The decisive upgrade over plain coverage (#9 → #11): for the question
    # "who is the partner on Okto Neuron?" the content terms are {partner,
    # marginalia}. A Claim covering ONLY the rare/discriminative term ("partner")
    # must outrank a Claim covering ONLY the common topic term ("marginalia").
    # Plain coordination-level coverage would TIE them (both 1/2); IDF weighting
    # breaks the tie toward the answer-bearing term.
    embedder = StubEmbedder()
    store = InMemoryStore()
    query = "who is the partner on Okto Neuron?"

    store.add_node(
        Node(
            id="claim-partner",
            type="Claim",
            title="partner duties",
            content="the partner duties",
            embedding=embedder.embed("n0"),
        )
    )
    store.add_node(
        Node(
            id="claim-marg-only",
            type="Claim",
            title="Okto Neuron notes",
            content="Okto Neuron notes",
            embedding=embedder.embed("n1"),
        )
    )
    # Filler Claims that ALSO mention 'marginalia' → inflate its doc-frequency so
    # IDF('marginalia') << IDF('partner').
    for i in range(6):
        store.add_node(
            Node(
                id=f"filler-{i}",
                type="Claim",
                title="Okto Neuron",
                content=f"Okto Neuron filler {i}",
                embedding=embedder.embed(f"f{i}"),
            )
        )

    hits = search_claims(query, k=20, store=store, embedder=embedder)
    order = [node.id for node, _ in hits]

    assert "claim-partner" in order and "claim-marg-only" in order
    assert order.index("claim-partner") < order.index("claim-marg-only")


def test_non_interrogative_query_does_not_rerank() -> None:
    # Same answer Claim, but a NON-interrogative query (no '?', first token not a
    # wh-word) must NOT trigger the rerank — the strong entity hit stays #1.
    embedder = StubEmbedder()
    store = InMemoryStore()
    query = "Okto Neuron"
    qvec = embedder.embed(query)

    store.add_node(
        Node(
            id="io-marg",
            type="InformationObject",
            title="Okto Neuron",
            content="Okto Neuron overview",
            embedding=qvec,
        )
    )
    store.add_node(
        Node(
            id="answer-claim",
            type="Claim",
            title="Jordan Lee Carter project partner on Okto Neuron",
            content="Jordan Lee Carter is the project partner on Okto Neuron",
            embedding=embedder.embed("noise vector"),
        )
    )

    hits = search_claims(query, k=10, store=store, embedder=embedder)

    # rerank is gated off → the weak full-coverage Claim is NOT promoted to #1.
    assert hits[0][0].id == "io-marg"


def test_exact_multiword_title_outranks_generic_single_token_title() -> None:
    embedder = StubEmbedder()
    store = InMemoryStore()
    query = "Alistair Cockburn User Story Format"
    qvec = embedder.embed(query)

    # Generic taxonomy term is a perfect vector hit and still lexically matches
    # "Story"; before the title leg it could beat the exact named method node.
    store.add_node(Node(id="story", type="Concept", title="Story", content="Story", embedding=qvec))
    store.add_node(
        Node(
            id="cockburn-format",
            type="Concept",
            title="Alistair Cockburn User Story Format",
            content="",
        )
    )

    hits = search_claims(query, k=5, store=store, embedder=embedder)
    rank = {node.id: index for index, (node, _) in enumerate(hits)}

    assert rank["cockburn-format"] < rank["story"]


def test_exact_title_survives_graph_expansion_from_generic_hub() -> None:
    embedder = StubEmbedder()
    store = InMemoryStore()
    query = "Alistair Cockburn User Story Format"
    qvec = embedder.embed(query)

    store.add_node(Node(id="story", type="Concept", title="Story", content="Story", embedding=qvec))
    store.add_node(
        Node(
            id="cockburn-format",
            type="Concept",
            title="Alistair Cockburn User Story Format",
            content="",
        )
    )
    for i in range(6):
        claim_id = f"claim-{i}"
        store.add_node(
            Node(
                id=claim_id,
                type="Claim",
                title=f"Alistair Cockburn User Story Format claim {i}",
                content="Alistair Cockburn User Story Format",
                embedding=qvec,
            )
        )
        store.add_edge(Edge(id=f"edge-{i}", type="mentions", src=claim_id, dst="story"))

    hits = search_claims(query, k=10, store=store, embedder=embedder)
    rank = {node.id: index for index, (node, _) in enumerate(hits)}

    assert rank["cockburn-format"] < rank["story"]


def test_returns_at_most_max_k_20() -> None:
    embedder = StubEmbedder()
    store = InMemoryStore()
    for i in range(40):
        store.add_node(
            Node(
                id=f"n-{i}",
                type="Concept",
                title="alpha",
                content="alpha beta",
                embedding=embedder.embed(f"n-{i}"),
            )
        )
    hits = search_claims("alpha beta", k=5, store=store, embedder=embedder)
    assert len(hits) <= 20
