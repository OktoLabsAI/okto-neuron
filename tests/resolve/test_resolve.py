"""Phase D — resolve talk-back, on InMemoryStore + stub embedder."""

from __future__ import annotations


from okto_neuron.consolidate import NodeCandidate
from okto_neuron.core.schema import Node
from okto_neuron.embed import get_provider
from okto_neuron.resolve import (
    ResolveOutcome,
    find_contradictions,
    find_similar,
    resolve,
)
from okto_neuron.store.memory import InMemoryStore

EMB = get_provider("stub")


def _embed(text: str) -> list[float]:
    return EMB.embed(text)


def _node(node_id: str, type_: str, title: str, content: str = "", **kw) -> Node:
    facets = kw.pop("facets", {})
    embed_text = kw.pop("embed_text", f"{title}\n{content}".strip())
    return Node(
        id=node_id,
        type=type_,
        title=title,
        content=content,
        facets=facets,
        embedding=_embed(embed_text),
    )


# ── find_similar ──────────────────────────────────────────────────────────────
def test_find_similar_match():
    store = InMemoryStore()
    store.add_node(_node("n1", "Concept", "Knowledge graphs", "graphs of facts"))
    cand = NodeCandidate(
        type="Concept",
        title="Knowledge graphs",
        content="graphs of facts",
        embedding=tuple(_embed("Knowledge graphs\ngraphs of facts")),
    )
    cors = find_similar(cand, store, embedder=EMB)
    assert len(cors) == 1
    assert cors[0].kind == "similar"
    assert cors[0].target_id == "n1"
    assert cors[0].score >= 0.99  # identical text under stub embedder


def test_find_similar_no_match():
    store = InMemoryStore()
    store.add_node(_node("n1", "Concept", "Cooking pasta", "boil water then add pasta"))
    cand = NodeCandidate(
        type="Concept",
        title="Quantum chromodynamics",
        content="the strong nuclear force",
    )
    cors = find_similar(cand, store, embedder=EMB)
    assert cors == ()


def test_find_similar_ignores_other_type_nodes_including_blocks():
    """Regression: an extracted entity is textually identical to the Block it was
    derived from, but a Block is provenance, not an ambiguous duplicate. Only
    same-type entity nodes count — otherwise every candidate reads as ambiguous
    and never auto-commits."""
    store = InMemoryStore()
    # the raw paragraph Block the entity came from — same text, different type
    store.add_node(_node("blk", "Block", "Evan Gaur is the CDTO at ExampleCorp"))
    store.add_node(_node("doc", "Document", "Evan Gaur is the CDTO at ExampleCorp"))
    cand = NodeCandidate(type="Agent", title="Evan Gaur", content="CDTO at ExampleCorp")
    cors = find_similar(cand, store, embedder=EMB)
    # no same-type Agent exists → genuinely novel, no false ambiguity
    assert cors == ()


def test_find_similar_excludes_infra_system_agent():
    """Regression: every ingest seeds a provenance Agent titled 'system'. An
    extracted Agent must NOT match it on embedding noise — that was gating real
    entities (e.g. 'Evan Gaur') to review at the old 0.5 threshold."""
    from okto_neuron.ingest import SYSTEM_AGENT_ID

    store = InMemoryStore()
    store.add_node(
        _node(
            SYSTEM_AGENT_ID, "Agent", "system", "Marginalia deterministic extraction system agent"
        )
    )
    cand = NodeCandidate(type="Agent", title="Evan Gaur", content="CDTO at ExampleCorp")
    cors = find_similar(cand, store, embedder=EMB)
    assert cors == ()  # infra node excluded → genuinely novel


def test_find_similar_computes_embedding_for_nodes_without_one():
    store = InMemoryStore()
    # node stored without an embedding; resolver must compute one
    store.add_node(Node(id="n1", type="Concept", title="Alpha", content="beta gamma"))
    cand = NodeCandidate(type="Concept", title="Alpha", content="beta gamma")
    cors = find_similar(cand, store, embedder=EMB)
    assert len(cors) == 1
    assert cors[0].target_id == "n1"


# ── find_contradictions ─────────────────────────────────────────────────────--
def _claim_node(node_id: str, s: str, p: str, o: str) -> Node:
    return _node(
        node_id,
        "Claim",
        f"{s} {p} {o}",
        facets={"subject": s, "predicate": p, "object": o},
    )


def test_find_contradiction_detected():
    store = InMemoryStore()
    store.add_node(_claim_node("c1", "Alex", "lives_in", "Lisbon"))
    cand = NodeCandidate(
        type="Claim",
        title="Alex lives_in Porto",
        facets={"subject": "Alex", "predicate": "lives_in", "object": "Porto"},
    )
    cors = find_contradictions(cand, store)
    assert len(cors) == 1
    assert cors[0].kind == "contradicts"
    assert cors[0].target_id == "c1"


def test_no_contradiction_when_object_matches():
    store = InMemoryStore()
    store.add_node(_claim_node("c1", "Alex", "lives_in", "Lisbon"))
    cand = NodeCandidate(
        type="Claim",
        title="Alex lives_in Lisbon",
        facets={"subject": "Alex", "predicate": "lives_in", "object": "Lisbon"},
    )
    assert find_contradictions(cand, store) == ()


def test_no_contradiction_for_exact_surface_equivalence():
    """NFC, case, and collapsed whitespace are exact-equivalence rules for all
    three claim fields; they must not manufacture a contradiction."""
    store = InMemoryStore()
    store.add_node(_claim_node("c1", "Cafe\u0301", "lives_in", "LISBON"))
    cand = NodeCandidate(
        type="Claim",
        title="Café lives_in Lisbon",
        facets={"subject": "  Café  ", "predicate": "LIVES_IN", "object": "Lisbon"},
    )
    assert find_contradictions(cand, store) == ()


def test_no_contradiction_for_non_claim():
    store = InMemoryStore()
    store.add_node(_claim_node("c1", "Alex", "lives_in", "Lisbon"))
    cand = NodeCandidate(type="Concept", title="anything")
    assert find_contradictions(cand, store) == ()


# ── resolve + confidence ordering ─────────────────────────────────────────────
def test_resolve_novel_is_high_confidence():
    store = InMemoryStore()
    store.add_node(_node("n1", "Concept", "Cooking", "boil water"))
    cand = NodeCandidate(type="Concept", title="Astrophysics", content="stars")
    out = resolve(cand, store, embedder=EMB)
    assert isinstance(out, ResolveOutcome)
    assert out.correlations == ()
    assert out.confidence > 0.8


def test_resolve_confidence_ordering():
    store = InMemoryStore()
    store.add_node(_node("dup", "Concept", "Knowledge graphs", "graphs of facts"))
    store.add_node(_claim_node("c1", "Alex", "lives_in", "Lisbon"))

    novel = resolve(
        NodeCandidate(type="Concept", title="Astrophysics", content="stars"),
        store,
        embedder=EMB,
    )
    ambiguous = resolve(
        NodeCandidate(
            type="Concept",
            title="Knowledge graphs",
            content="graphs of facts",
            embedding=tuple(_embed("Knowledge graphs\ngraphs of facts")),
        ),
        store,
        embedder=EMB,
    )
    contradicted = resolve(
        NodeCandidate(
            type="Claim",
            title="Alex lives_in Porto",
            facets={"subject": "Alex", "predicate": "lives_in", "object": "Porto"},
        ),
        store,
        embedder=EMB,
    )

    assert contradicted.contradicted is True
    assert contradicted.confidence < ambiguous.confidence < novel.confidence


def test_resolve_assembles_all_kinds():
    store = InMemoryStore()
    store.add_node(_node("dup", "Concept", "Knowledge graphs", "graphs of facts"))
    cand = NodeCandidate(
        type="Concept",
        title="Knowledge graphs",
        content="graphs of facts",
        embedding=tuple(_embed("Knowledge graphs\ngraphs of facts")),
    )
    out = resolve(cand, store, embedder=EMB)
    kinds = {c.kind for c in out.correlations}
    assert "similar" in kinds
