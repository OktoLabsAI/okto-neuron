"""Model-free unit tests for the ADR 0011 subgraph-first ask path.

Exercises ``build_ego_graph`` + ``render_ego_subgraph`` against ``InMemoryStore``
— no rebuild, no LLM, no embedder. This is the decisive check for the
load-bearing edge-direction semantics, the ``O_id`` XOR ``O_literal`` routing,
the dedup, the degree cap, and the quality/infra gates the plan calls out.
"""

from __future__ import annotations

from okto_neuron._internal.infra import INFRA_FACET
from okto_neuron.core.schema import Edge, Node
from okto_neuron.store.memory import InMemoryStore
from okto_neuron.subgraph import (
    _BRIDGE_CLAIM_SCAN_CAP_FACTOR,
    _BRIDGE_CLAIM_SCAN_CAP_FLOOR,
    build_ego_graph,
    render_ego_subgraph,
)


def _entity(node_id: str, title: str, type: str = "Agent") -> Node:
    return Node(id=node_id, type=type, title=title, content=title)


def _claim(
    node_id: str,
    *,
    s_id: str,
    p: str,
    o_id: str | None = None,
    o_literal: str | None = None,
    confidence: float = 0.9,
    block_id: str = "b" * 64,
) -> Node:
    facets: dict = {
        "S_id": s_id,
        "P": p,
        "confidence": confidence,
        "block_id": block_id,
    }
    if o_id is not None:
        facets["O_id"] = o_id
    if o_literal is not None:
        facets["O_literal"] = o_literal
    return Node(id=node_id, type="Claim", title=f"{s_id} {p}", content=f"{s_id} {p}", facets=facets)


def _topology_store() -> tuple[InMemoryStore, str]:
    """alice -[founded]-> acme, wired exactly as the ingest path mints it:
    Claim -[rdf:subject]-> alice ; Claim -[rdf:object]-> acme."""
    store = InMemoryStore()
    alice = _entity("a" * 64, "Alice")
    acme = _entity("c" * 64, "Acme")
    claim = _claim("d" * 64, s_id=alice.id, p="founded", o_id=acme.id)
    for n in (alice, acme, claim):
        store.add_node(n)
    store.add_edge(Edge(id="e1", type="rdf:subject", src=claim.id, dst=alice.id))
    store.add_edge(Edge(id="e2", type="rdf:object", src=claim.id, dst=acme.id))
    return store, alice.id


def test_entity_seed_reaches_related_entity_via_topology_claim() -> None:
    store, alice_id = _topology_store()
    ego = build_ego_graph([(alice_id, 1.0)], store, degree_cap=8, neighbour_budget_tokens=2000)
    # Both endpoints present; the related entity was reached through the Claim.
    assert alice_id in ego.nodes
    assert ego.nodes[alice_id].is_seed is True
    acme_id = "c" * 64
    assert acme_id in ego.nodes
    assert ego.nodes[acme_id].is_seed is False
    # One RELATIONSHIPS row, no propositional claims.
    assert len(ego.relationships) == 1
    rel = ego.relationships[0]
    assert rel.subject_id == alice_id
    assert rel.object_id == acme_id
    assert rel.predicate == "founded"
    assert ego.claims == []


def test_render_emits_relationship_row_no_body_leak() -> None:
    store, alice_id = _topology_store()
    ego = build_ego_graph([(alice_id, 1.0)], store, degree_cap=8, neighbour_budget_tokens=2000)
    out = render_ego_subgraph(ego, max_token_budget=100_000)
    assert "=== NODES ===" in out
    assert "=== RELATIONSHIPS ===" in out
    assert "-[founded]->" in out
    assert "Alice" in out and "Acme" in out
    # No raw block-body payload — the row is a compact verbalized triple.
    assert "\n{block" not in out
    assert len(out) < 600


def test_literal_claim_routes_to_claims_section_not_relationships() -> None:
    store = InMemoryStore()
    alice = _entity("a" * 64, "Alice")
    claim = _claim("d" * 64, s_id=alice.id, p="has_role", o_literal="founder")
    store.add_node(alice)
    store.add_node(claim)
    store.add_edge(Edge(id="e1", type="rdf:subject", src=claim.id, dst=alice.id))
    ego = build_ego_graph([(alice.id, 1.0)], store, degree_cap=8, neighbour_budget_tokens=2000)
    # O_literal → CLAIMS row, not a RELATIONSHIPS row (the load-bearing XOR).
    assert ego.relationships == []
    assert len(ego.claims) == 1
    assert ego.claims[0].literal == "founder"
    out = render_ego_subgraph(ego, max_token_budget=100_000)
    assert "=== CLAIMS (verbalized) ===" in out
    assert "Alice has role founder" in out


def test_object_seed_surfaces_relationship() -> None:
    """Seeding on the OBJECT of a relationship must still surface the subject and
    the row — "who founded Acme?" seeds on Acme, the answer Alice is the subject."""
    store, _alice_id = _topology_store()
    alice_id = "a" * 64
    acme_id = "c" * 64
    ego = build_ego_graph([(acme_id, 1.0)], store, degree_cap=8, neighbour_budget_tokens=2000)
    assert acme_id in ego.nodes and ego.nodes[acme_id].is_seed is True
    assert alice_id in ego.nodes and ego.nodes[alice_id].is_seed is False
    assert len(ego.relationships) == 1
    rel = ego.relationships[0]
    assert rel.subject_id == alice_id and rel.object_id == acme_id
    out = render_ego_subgraph(ego, max_token_budget=100_000)
    assert "-[founded]->" in out
    assert "Alice" in out


def test_low_budget_truncates_rows_seeds_first() -> None:
    """The render budget (the swept neighbour_budget_tokens knob) actually trims
    rel/claim rows; NODES (the seed index) is never trimmed."""
    store = InMemoryStore()
    alice = _entity("a" * 64, "Alice")
    store.add_node(alice)
    for i in range(15):
        obj = _entity(f"{i:064d}", f"Obj{i}")
        store.add_node(obj)
        claim = _claim(f"{i + 100:064d}", s_id=alice.id, p="knows", o_id=obj.id, confidence=0.9)
        store.add_node(claim)
        store.add_edge(Edge(id=f"s{i}", type="rdf:subject", src=claim.id, dst=alice.id))
        store.add_edge(Edge(id=f"o{i}", type="rdf:object", src=claim.id, dst=obj.id))
    ego = build_ego_graph([(alice.id, 1.0)], store, degree_cap=99, neighbour_budget_tokens=99999)
    full = render_ego_subgraph(ego, max_token_budget=100_000)
    tight = render_ego_subgraph(ego, max_token_budget=60)
    assert full.count("-[knows]->") > tight.count("-[knows]->")
    # NODES header survives even a tiny budget (seeds-first).
    assert tight.startswith("=== NODES ===")


def test_render_caps_nodes_relationships_and_claims() -> None:
    store = InMemoryStore()
    alice = _entity("a" * 64, "Alice")
    store.add_node(alice)
    for i in range(5):
        obj = _entity(f"{i:064d}", f"Obj{i}")
        lit = _claim(f"{i + 200:064d}", s_id=alice.id, p="has_fact", o_literal=f"fact {i}")
        rel = _claim(f"{i + 300:064d}", s_id=alice.id, p="knows", o_id=obj.id)
        store.add_node(obj)
        store.add_node(lit)
        store.add_node(rel)
        store.add_edge(Edge(id=f"ls{i}", type="rdf:subject", src=lit.id, dst=alice.id))
        store.add_edge(Edge(id=f"rs{i}", type="rdf:subject", src=rel.id, dst=alice.id))
        store.add_edge(Edge(id=f"ro{i}", type="rdf:object", src=rel.id, dst=obj.id))

    ego = build_ego_graph([(alice.id, 1.0)], store, degree_cap=20, neighbour_budget_tokens=99999)
    out = render_ego_subgraph(
        ego,
        max_token_budget=100_000,
        max_nodes=2,
        max_relationships=1,
        max_claims=2,
    )
    node_lines = [line for line in out.splitlines() if line.startswith("N") and " [" in line]
    assert len(node_lines) <= 2
    assert out.count("-[knows]->") <= 1
    assert out.count("has fact") <= 2


def test_relationship_filter_and_min_confidence_gate_claims() -> None:
    store = InMemoryStore()
    alice = _entity("a" * 64, "Alice")
    acme = _entity("c" * 64, "Acme")
    beta = _entity("b" * 64, "Beta")
    keep = _claim("d" * 64, s_id=alice.id, p="founded", o_id=acme.id, confidence=0.9)
    wrong_type = _claim("e" * 64, s_id=alice.id, p="visited", o_id=beta.id, confidence=0.9)
    low_conf = _claim("f" * 64, s_id=alice.id, p="founded", o_id=beta.id, confidence=0.2)
    for n in (alice, acme, beta, keep, wrong_type, low_conf):
        store.add_node(n)
    for claim in (keep, wrong_type, low_conf):
        store.add_edge(Edge(id=f"s-{claim.id}", type="rdf:subject", src=claim.id, dst=alice.id))
        store.add_edge(
            Edge(id=f"o-{claim.id}", type="rdf:object", src=claim.id, dst=claim.facets["O_id"])
        )

    ego = build_ego_graph(
        [(alice.id, 1.0)],
        store,
        degree_cap=10,
        neighbour_budget_tokens=99999,
        min_claim_confidence=0.5,
        relationship_types=("founded",),
    )
    assert len(ego.relationships) == 1
    assert ego.relationships[0].claim_id == keep.id


def test_claim_seed_resolves_its_own_endpoints() -> None:
    store, _alice_id = _topology_store()
    claim_id = "d" * 64
    ego = build_ego_graph([(claim_id, 1.0)], store, degree_cap=8, neighbour_budget_tokens=2000)
    assert len(ego.relationships) == 1
    assert ego.relationships[0].claim_id == claim_id


def test_schema_mentions_does_not_bloom_sibling_entities() -> None:
    """A hub Document mentioning 50 entities must NOT pull every sibling in when
    seeding on one of them — schema:mentions is walked entity→Document only."""
    store = InMemoryStore()
    doc = _entity("f" * 64, "HubDoc", type="Document")
    store.add_node(doc)
    seed_entity = None
    for i in range(50):
        ent = _entity(f"{i:064d}", f"Entity{i}")
        store.add_node(ent)
        store.add_edge(Edge(id=f"m{i}", type="schema:mentions", src=doc.id, dst=ent.id))
        if i == 0:
            seed_entity = ent
    assert seed_entity is not None
    ego = build_ego_graph(
        [(seed_entity.id, 1.0)], store, degree_cap=8, neighbour_budget_tokens=2000
    )
    # Only the seed itself; no sibling entities, no relationships (mentions are
    # not topology claims and the Document's other mentions are never walked).
    assert seed_entity.id in ego.nodes
    assert ego.relationships == []
    # The 49 siblings must not have entered the ego graph.
    assert len(ego.nodes) == 1


def test_degree_cap_limits_neighbours_per_seed() -> None:
    store = InMemoryStore()
    alice = _entity("a" * 64, "Alice")
    store.add_node(alice)
    for i in range(20):
        obj = _entity(f"{i:064d}", f"Obj{i}")
        store.add_node(obj)
        claim = _claim(
            f"{i + 100:064d}", s_id=alice.id, p="knows", o_id=obj.id, confidence=0.5 + i * 0.01
        )
        store.add_node(claim)
        store.add_edge(Edge(id=f"s{i}", type="rdf:subject", src=claim.id, dst=alice.id))
        store.add_edge(Edge(id=f"o{i}", type="rdf:object", src=claim.id, dst=obj.id))
    ego = build_ego_graph([(alice.id, 1.0)], store, degree_cap=5, neighbour_budget_tokens=99999)
    # At most degree_cap bridging claims survive collection.
    assert len(ego.relationships) <= 5


def test_degree_cap_bounds_collection_work_not_just_output() -> None:
    """3.18 regression: collection work (get_node + gate + score per candidate)
    must be bounded by degree_cap, not by the seed's true (unbounded) degree.
    Pre-fix, ``_ranked_bridge_claims`` called ``get_node`` once per bridging
    Claim before truncating to ``degree_cap`` at the very end, so a hub seed
    with hundreds of bridging Claims paid for hundreds of get_node calls
    regardless of how small degree_cap was."""
    store = InMemoryStore()
    alice = _entity("a" * 64, "Alice")
    store.add_node(alice)
    n_claims = 500
    for i in range(n_claims):
        obj = _entity(f"{i:064d}", f"Obj{i}")
        store.add_node(obj)
        claim = _claim(f"{i + 100000:064d}", s_id=alice.id, p="knows", o_id=obj.id)
        store.add_node(claim)
        store.add_edge(Edge(id=f"s{i}", type="rdf:subject", src=claim.id, dst=alice.id))
        store.add_edge(Edge(id=f"o{i}", type="rdf:object", src=claim.id, dst=obj.id))

    get_node_calls = 0
    real_get_node = store.get_node

    def _counting_get_node(node_id: str):
        nonlocal get_node_calls
        get_node_calls += 1
        return real_get_node(node_id)

    store.get_node = _counting_get_node  # type: ignore[method-assign]

    degree_cap = 5
    ego = build_ego_graph(
        [(alice.id, 1.0)], store, degree_cap=degree_cap, neighbour_budget_tokens=99999
    )

    assert len(ego.relationships) <= degree_cap
    # Collection work must scale with the scan cap (a bounded constant factor
    # of degree_cap), never with the seed's true degree (500 candidates here).
    # Pre-fix this was >= n_claims (one get_node per bridging Claim, unbounded);
    # post-fix it is bounded by a small multiple of the scan cap regardless of
    # how large the seed's true degree grows.
    scan_cap = max(degree_cap * _BRIDGE_CLAIM_SCAN_CAP_FACTOR, _BRIDGE_CLAIM_SCAN_CAP_FLOOR)
    assert get_node_calls < n_claims
    assert get_node_calls <= scan_cap * 2


def test_dedup_on_triple() -> None:
    """Two Claims asserting the identical (S, P, O) triple collapse to one row."""
    store, alice_id = _topology_store()
    acme_id = "c" * 64
    dup = _claim("e" * 64, s_id=alice_id, p="founded", o_id=acme_id)
    store.add_node(dup)
    store.add_edge(Edge(id="e3", type="rdf:subject", src=dup.id, dst=alice_id))
    store.add_edge(Edge(id="e4", type="rdf:object", src=dup.id, dst=acme_id))
    ego = build_ego_graph([(alice_id, 1.0)], store, degree_cap=8, neighbour_budget_tokens=2000)
    assert len(ego.relationships) == 1


def test_infra_object_drops_the_whole_relationship() -> None:
    store = InMemoryStore()
    alice = _entity("a" * 64, "Alice")
    infra = Node(
        id="c" * 64, type="Agent", title="provenance-agent", content="x", facets=dict(INFRA_FACET)
    )
    claim = _claim("d" * 64, s_id=alice.id, p="founded", o_id=infra.id)
    for n in (alice, infra, claim):
        store.add_node(n)
    store.add_edge(Edge(id="e1", type="rdf:subject", src=claim.id, dst=alice.id))
    store.add_edge(Edge(id="e2", type="rdf:object", src=claim.id, dst=infra.id))
    ego = build_ego_graph([(alice.id, 1.0)], store, degree_cap=8, neighbour_budget_tokens=2000)
    # Object is infra → the whole relationship is dropped, infra never rendered.
    assert ego.relationships == []
    assert infra.id not in ego.nodes


def test_metadata_predicate_dropped() -> None:
    store = InMemoryStore()
    alice = _entity("a" * 64, "Alice")
    claim = _claim("d" * 64, s_id=alice.id, p="built_date", o_literal="2026-05-05")
    store.add_node(alice)
    store.add_node(claim)
    store.add_edge(Edge(id="e1", type="rdf:subject", src=claim.id, dst=alice.id))
    ego = build_ego_graph([(alice.id, 1.0)], store, degree_cap=8, neighbour_budget_tokens=2000)
    # ``built_date`` is a metadata predicate → silently dropped from the render.
    assert ego.claims == []
    assert ego.relationships == []


def test_query_embedding_accepted_as_kwarg() -> None:
    """GN-4: build_ego_graph accepts query_embedding=None without error."""
    store, alice_id = _topology_store()
    ego = build_ego_graph(
        [(alice_id, 1.0)],
        store,
        degree_cap=8,
        neighbour_budget_tokens=2000,
        query_embedding=None,
    )
    assert alice_id in ego.nodes
    assert len(ego.relationships) == 1


def test_query_embedding_relevance_degree_cap() -> None:
    """GN-5: degree_cap keeps highest-cosine Claims, not lowest-id ones."""
    store = InMemoryStore()
    alice = _entity("a" * 64, "Alice")
    store.add_node(alice)
    # 10 Claims with equal confidence; assign embeddings so only the LAST 2 are
    # cosine-close to the query vector [1.0, 0.0].
    query_emb = [1.0, 0.0]
    for i in range(10):
        obj = _entity(f"{i:064d}", f"Obj{i}")
        store.add_node(obj)
        # Claims 8 and 9 get a high-cosine embedding; 0-7 get orthogonal [0,1].
        emb = [1.0, 0.0] if i >= 8 else [0.0, 1.0]
        claim = Node(
            id=f"{i + 100:064d}",
            type="Claim",
            title=f"alice knows obj{i}",
            content=f"alice knows obj{i}",
            facets={
                "S_id": alice.id,
                "P": "knows",
                "O_id": obj.id,
                "confidence": 0.8,
                "block_id": "b" * 64,
            },
            embedding=emb,
        )
        store.add_node(claim)
        store.add_edge(Edge(id=f"s{i}", type="rdf:subject", src=claim.id, dst=alice.id))
        store.add_edge(Edge(id=f"o{i}", type="rdf:object", src=claim.id, dst=obj.id))

    ego = build_ego_graph(
        [(alice.id, 1.0)],
        store,
        degree_cap=2,
        neighbour_budget_tokens=99999,
        query_embedding=query_emb,
    )
    # Only 2 Claims survive; they should be the ones with high cosine (Obj8/Obj9).
    assert len(ego.relationships) == 2
    kept_objects = {r.object_id for r in ego.relationships}
    assert f"{'8':>064}" in kept_objects or f"{'9':>064}" in kept_objects


def test_claim_seed_registers_subject_entity() -> None:
    """GN-6: a Claim seed must add its subject entity to ego.nodes."""
    store, _alice_id = _topology_store()
    alice_id = "a" * 64
    claim_id = "d" * 64
    ego = build_ego_graph([(claim_id, 1.0)], store, degree_cap=8, neighbour_budget_tokens=2000)
    # GN-6: subject entity (Alice) must now be in ego.nodes
    assert alice_id in ego.nodes, "GN-6: subject entity of Claim seed must be in ego.nodes"
    assert len(ego.relationships) == 1


def test_claim_seed_sibling_entity_traversal_supersedes_block_colocation() -> None:
    """Hop-1 sibling-entity traversal: a Claim seed promotes its SUBJECT entity to a
    full entity expansion, so ALL siblings asserted about that entity are pulled in —
    including cross-block ones. This supersedes GN-7's same-block-only boundary: the
    ``has_location`` sibling on a DIFFERENT block now appears because it shares the
    subject entity, whereas the old block-colocation pass would have excluded it."""
    BLOCK = "b" * 64
    store = InMemoryStore()
    alice = _entity("a" * 64, "Alice")
    store.add_node(alice)

    # Seed Claim: alice has_role founder
    seed_claim = Node(
        id="d" * 64,
        type="Claim",
        title="alice has_role founder",
        content="alice has_role founder",
        facets={
            "S_id": alice.id,
            "P": "has_role",
            "O_literal": "founder",
            "confidence": 0.9,
            "block_id": BLOCK,
        },
    )
    # Sibling Claim at same block: alice has_skill engineering
    sibling = Node(
        id="e" * 64,
        type="Claim",
        title="alice has_skill engineering",
        content="alice has_skill engineering",
        facets={
            "S_id": alice.id,
            "P": "has_skill",
            "O_literal": "engineering",
            "confidence": 0.9,
            "block_id": BLOCK,
        },
    )
    # Unrelated Claim at different block
    other_block = Node(
        id="f" * 64,
        type="Claim",
        title="alice has_location Berlin",
        content="alice has_location Berlin",
        facets={
            "S_id": alice.id,
            "P": "has_location",
            "O_literal": "Berlin",
            "confidence": 0.9,
            "block_id": "c" * 64,
        },
    )
    for n in (alice, seed_claim, sibling, other_block):
        store.add_node(n)
    for claim in (seed_claim, sibling, other_block):
        store.add_edge(Edge(id=f"s-{claim.id}", type="rdf:subject", src=claim.id, dst=alice.id))

    ego = build_ego_graph(
        [(seed_claim.id, 1.0)], store, degree_cap=8, neighbour_budget_tokens=99999
    )
    # All three siblings assert about the same subject entity (alice), so all three
    # appear once the subject entity is expanded — the block boundary no longer gates.
    claim_predicates = {c.predicate for c in ego.claims}
    assert "has_role" in claim_predicates, "seed claim itself must appear"
    assert "has_skill" in claim_predicates, "same-subject sibling must appear"
    assert "has_location" in claim_predicates, (
        "sibling-entity traversal: a different-block sibling on the same subject "
        "entity now appears (supersedes GN-7's same-block-only boundary)"
    )


def test_empty_seeds_render_clean() -> None:
    store = InMemoryStore()
    ego = build_ego_graph([], store, degree_cap=8, neighbour_budget_tokens=2000)
    assert ego.nodes == {}
    out = render_ego_subgraph(ego, max_token_budget=100_000)
    assert out.strip() == "=== NODES ==="


# ── answer-aware ego-graph assembly (subgraph assembly-gap fix) ───────────────
def _lit_claim(
    node_id: str, *, s_id: str, p: str, literal: str, content: str, confidence: float
) -> Node:
    """A propositional Claim whose ``content`` carries the extracted text (so the
    IDF answer-coverage signal — which reads title+content — can see the
    discriminative object term). Wired with only the ``rdf:subject`` bridge, as the
    ingest path mints a literal Claim."""
    return Node(
        id=node_id,
        type="Claim",
        title=f"{s_id} {p}",
        content=content,
        facets={
            "S_id": s_id,
            "P": p,
            "O_literal": literal,
            "confidence": confidence,
            "block_id": "z" * 64,
        },
    )


def _answer_aware_store() -> tuple[InMemoryStore, str]:
    """Frigate seed with three literal bridging Claims. The discriminative one
    ("redis") has the LOWEST confidence; two generic siblings have higher
    confidence. Pre-patch, a small degree_cap keeps the high-confidence generics
    and drops the answering Claim — this is the RANKING failure the fix targets."""
    store = InMemoryStore()
    frigate = _entity("f" * 64, "Frigate")
    store.add_node(frigate)
    claims = [
        _lit_claim(
            "a" * 64,
            s_id=frigate.id,
            p="uses",
            literal="redis",
            content="frigate uses redis for caching",
            confidence=0.70,
        ),
        _lit_claim(
            "b" * 64,
            s_id=frigate.id,
            p="listens_on",
            literal="port 5000",
            content="frigate listens on port 5000",
            confidence=0.95,
        ),
        _lit_claim(
            "c" * 64,
            s_id=frigate.id,
            p="has_status",
            literal="active",
            content="frigate has status active",
            confidence=0.95,
        ),
    ]
    for i, c in enumerate(claims):
        store.add_node(c)
        store.add_edge(Edge(id=f"rs{i}", type="rdf:subject", src=c.id, dst=frigate.id))
    return store, frigate.id


def test_answer_aware_cut_keeps_discriminative_claim() -> None:
    """WITH answer_terms, IDF answer-coverage is the PRIMARY ranking key, so the
    single kept Claim under a tight degree_cap is the discriminative "redis" one —
    NOT the higher-confidence generic sibling the pre-patch (-confidence, id) cut
    would keep."""
    store, frigate_id = _answer_aware_store()
    ego = build_ego_graph(
        [(frigate_id, 1.0)],
        store,
        degree_cap=1,
        neighbour_budget_tokens=2000,
        answer_terms=frozenset({"redis"}),
    )
    assert len(ego.claims) == 1
    assert ego.claims[0].literal == "redis"
    out = render_ego_subgraph(ego, max_token_budget=100_000)
    assert "redis" in out


def test_without_answer_terms_cut_is_unchanged() -> None:
    """WITHOUT answer_terms (default / non-interrogative), the cut is byte-identical
    to pre-patch: the highest-confidence generic sibling is kept and the "redis"
    Claim is dropped. Passing answer_terms=None must equal omitting it (no-op)."""
    store, frigate_id = _answer_aware_store()
    default_ego = build_ego_graph(
        [(frigate_id, 1.0)],
        store,
        degree_cap=1,
        neighbour_budget_tokens=2000,
    )
    none_ego = build_ego_graph(
        [(frigate_id, 1.0)],
        store,
        degree_cap=1,
        neighbour_budget_tokens=2000,
        answer_terms=None,
    )
    assert len(default_ego.claims) == 1
    # Pre-patch behaviour: a high-confidence generic sibling wins, not "redis".
    assert default_ego.claims[0].literal != "redis"
    # None is a strict no-op vs. the default.
    assert [c.literal for c in none_ego.claims] == [c.literal for c in default_ego.claims]
    assert none_ego.claims[0].answer_score == 0.0


def test_render_truncation_keeps_discriminative_row() -> None:
    """The build-time win must survive render truncation. All three Claims clear a
    wide degree_cap, but a tight render budget fits only one CLAIMS row. WITH
    answer_terms the answer_score leads the render sort, so the "redis" row is the
    one kept — even though its confidence is the lowest."""
    store, frigate_id = _answer_aware_store()
    ego = build_ego_graph(
        [(frigate_id, 1.0)],
        store,
        degree_cap=8,
        neighbour_budget_tokens=2000,
        answer_terms=frozenset({"redis"}),
    )
    assert len(ego.claims) == 3
    # The "redis" row leads the render sort (answer_score first) despite its lowest
    # confidence, so it is emitted before the higher-confidence generic rows.
    full = render_ego_subgraph(ego, max_token_budget=100_000)
    assert full.index("redis") < full.index("port 5000")
    # Under a budget that drops rows, the answering "redis" row is the survivor.
    tight = render_ego_subgraph(ego, max_token_budget=24)
    assert tight.count("\n- ") < 3
    assert "redis" in tight


# ── multi-hop ego-graph reach (sibling-entity traversal + budget bound) ────────
def _cross_entity_store() -> tuple[InMemoryStore, str]:
    """``Aye -[linked_to]-> Bee`` (topology), and ``Bee`` carries a literal answer
    Claim. Seeding on the ENTITY ``Aye``, the Bee-claim is a genuine 2-hop reach:
    hop-1 registers Bee as a neighbour (via the topology row) but does NOT expand it;
    only the hop-2 frontier walk pulls Bee's own claims in. Aye is an entity seed, so
    the hop-1 sibling-entity traversal (Claim-seed-only) does not fire here."""
    store = InMemoryStore()
    aye = _entity("a" * 64, "Aye")
    bee = _entity("b" * 64, "Bee")
    bridge = _claim("d" * 64, s_id=aye.id, p="linked_to", o_id=bee.id)
    answer = _claim("e" * 64, s_id=bee.id, p="has_secret", o_literal="XYZZY")
    for n in (aye, bee, bridge, answer):
        store.add_node(n)
    store.add_edge(Edge(id="s1", type="rdf:subject", src=bridge.id, dst=aye.id))
    store.add_edge(Edge(id="o1", type="rdf:object", src=bridge.id, dst=bee.id))
    store.add_edge(Edge(id="s2", type="rdf:subject", src=answer.id, dst=bee.id))
    return store, aye.id


def test_two_hop_reaches_sibling_entity_claim_that_one_hop_misses() -> None:
    """The core reach guarantee: a claim living on a SIBLING entity one hop past the
    seed enters the pool at hops=2 and is absent at hops=1."""
    store, aye_id = _cross_entity_store()
    ego1 = build_ego_graph(
        [(aye_id, 1.0)], store, degree_cap=8, neighbour_budget_tokens=2000, hops=1
    )
    ego2 = build_ego_graph(
        [(aye_id, 1.0)], store, degree_cap=8, neighbour_budget_tokens=2000, hops=2
    )
    literals1 = {c.literal for c in ego1.claims}
    literals2 = {c.literal for c in ego2.claims}
    # hop-1: Bee is a registered neighbour but its own claim is unreachable.
    assert "XYZZY" not in literals1
    assert any(r.predicate == "linked_to" for r in ego1.relationships)
    # hop-2: the sibling-entity claim is now reached via the frontier walk.
    assert "XYZZY" in literals2
    assert any(r.predicate == "linked_to" for r in ego2.relationships)


def _wide_two_hop_store(n_a: int = 30, n_b: int = 30) -> tuple[InMemoryStore, str]:
    """Seed entity ``Aye`` with many direct literal claims AND a bridge to ``Bee``;
    ``Bee`` carries even more literal claims plus one discriminative answer claim
    (``answerword``). The hop-2 pool is strictly larger than the hop-1 pool, so the
    render budget cap — not reach — must be what bounds the output."""
    store = InMemoryStore()
    aye = _entity("a" * 64, "Aye")
    bee = _entity("b" * 64, "Bee")
    store.add_node(aye)
    store.add_node(bee)
    # High confidence so the bridge survives Aye's hop-1 degree_cap cut (otherwise
    # the reach to Bee is severed before the frontier walk can find it).
    bridge = _claim("d" * 64, s_id=aye.id, p="linked_to", o_id=bee.id, confidence=0.99)
    store.add_node(bridge)
    store.add_edge(Edge(id="bs", type="rdf:subject", src=bridge.id, dst=aye.id))
    store.add_edge(Edge(id="bo", type="rdf:object", src=bridge.id, dst=bee.id))
    for i in range(n_a):
        c = _lit_claim(
            f"{i + 1000:064d}",
            s_id=aye.id,
            p=f"afact{i}",
            literal=f"aval{i}",
            content=f"aye detail number {i}",
            confidence=0.90,
        )
        store.add_node(c)
        store.add_edge(Edge(id=f"as{i}", type="rdf:subject", src=c.id, dst=aye.id))
    for i in range(n_b):
        c = _lit_claim(
            f"{i + 2000:064d}",
            s_id=bee.id,
            p=f"bfact{i}",
            literal=f"bval{i}",
            content=f"bee detail number {i}",
            confidence=0.90,
        )
        store.add_node(c)
        store.add_edge(Edge(id=f"bs{i}", type="rdf:subject", src=c.id, dst=bee.id))
    ans = _lit_claim(
        "e" * 64,
        s_id=bee.id,
        p="records",
        literal="answerword",
        content="bee records answerword",
        confidence=0.90,
    )
    store.add_node(ans)
    store.add_edge(Edge(id="ansS", type="rdf:subject", src=ans.id, dst=bee.id))
    return store, aye.id


def test_two_hop_render_stays_within_budget_despite_larger_pool() -> None:
    """The bound half: at hops=2 the candidate pool grows (Bee's claims enter), but
    the rendered context stays pinned to the token budget — extra reach changes WHICH
    rows compete, not how many are emitted. The discriminative ``answerword`` row (only
    reachable at hops=2) survives truncation via the answer-aware ranking."""
    from okto_neuron.subgraph import _est_tokens

    store, aye_id = _wide_two_hop_store()
    terms = frozenset({"answerword"})
    budget = 120
    factor = 1.117
    ego1 = build_ego_graph(
        [(aye_id, 1.0)],
        store,
        degree_cap=20,
        neighbour_budget_tokens=budget,
        hops=1,
        answer_terms=terms,
    )
    ego2 = build_ego_graph(
        [(aye_id, 1.0)],
        store,
        degree_cap=20,
        neighbour_budget_tokens=budget,
        hops=2,
        answer_terms=terms,
    )
    # The hop-2 pool is strictly larger — reach genuinely widened the candidate set.
    assert len(ego2.claims) > len(ego1.claims)

    r1 = render_ego_subgraph(ego1, max_token_budget=budget, token_factor=factor)
    r2 = render_ego_subgraph(ego2, max_token_budget=budget, token_factor=factor)
    # Reach: the sibling-entity answer is present only in the 2-hop render, and it
    # survived truncation (answer-aware ranking floats it to the front).
    assert "answerword" not in r1
    assert "answerword" in r2
    # Bound: far fewer rows are emitted than the pool holds — the budget cap fired.
    assert r2.count("\n- ") < len(ego2.claims)
    # The 2-hop render is capped by the budget (+ slack for the never-trimmed NODES
    # index and the section headers, which sit outside the row budget).
    slack = 60
    assert _est_tokens(r2, factor) <= budget + slack
    # And the larger pool did NOT grow the render past the 1-hop render — both
    # saturate the same budget, so reach cannot expand the emitted context.
    assert _est_tokens(r2, factor) <= _est_tokens(r1, factor) + slack
