"""Query-time equivalence fold (Option A reads). None == byte-identical."""

from __future__ import annotations

from okto_neuron.core.schema import Node
from okto_neuron.query import _fold_equivalence, search_claims
from okto_neuron.store.memory import InMemoryStore


class StubEmbedder:
    """Deterministic 2-D embeddings keyed by node id for cosine recall."""

    _VECS = {
        "canon": [1.0, 0.0],
        "var": [0.98, 0.20],
        "other": [0.0, 1.0],
        "__query__": [1.0, 0.0],
    }

    def embed(self, text):
        return self._VECS.get("__query__", [1.0, 0.0])


def _node(node_id, title, emb):
    return Node(id=node_id, type="Agent", title=title, content=title, embedding=emb)


def test_fold_collapses_variant_to_canonical_with_max_score():
    results = [
        (_node("canon", "Alex Rivera", [1, 0]), 0.6),
        (_node("var", "alex", [1, 0]), 0.9),  # higher score, but a variant
        (_node("other", "Bob", [0, 1]), 0.5),
    ]
    folded = _fold_equivalence(results, {"var": "canon", "canon": "canon"})
    ids = [n.id for n, _ in folded]
    # canon represents the class (canonical present), carrying the class max score
    assert "var" not in ids
    assert "canon" in ids
    assert "other" in ids
    canon_score = next(s for n, s in folded if n.id == "canon")
    assert canon_score == 0.9  # max member score
    # desc sorted
    scores = [s for _, s in folded]
    assert scores == sorted(scores, reverse=True)


def test_fold_picks_highest_member_when_canonical_absent():
    results = [
        (_node("var", "alex", [1, 0]), 0.7),
    ]
    # canonical "canon" not present in results → keep the highest-scoring member
    folded = _fold_equivalence(results, {"var": "canon"})
    assert [n.id for n, _ in folded] == ["var"]
    assert folded[0][1] == 0.7


def test_search_claims_none_equivalence_is_unchanged():
    store = InMemoryStore()
    store.add_node(_node("canon", "Alex Rivera", [1.0, 0.0]))
    store.add_node(_node("var", "alex", [0.98, 0.20]))
    store.add_node(_node("other", "Bob", [0.0, 1.0]))
    emb = StubEmbedder()

    base = search_claims("Alex", k=10, store=store, embedder=emb, type="Agent")
    same = search_claims("Alex", k=10, store=store, embedder=emb, type="Agent", equivalence=None)
    assert [(n.id, s) for n, s in base] == [(n.id, s) for n, s in same]


def test_search_claims_with_equivalence_collapses_cluster():
    store = InMemoryStore()
    store.add_node(_node("canon", "Alex Rivera", [1.0, 0.0]))
    store.add_node(_node("var", "alex", [0.98, 0.20]))
    store.add_node(_node("other", "Bob", [0.0, 1.0]))
    emb = StubEmbedder()

    eq = {"var": "canon", "canon": "canon"}
    folded = search_claims("Alex", k=10, store=store, embedder=emb, type="Agent", equivalence=eq)
    ids = [n.id for n, _ in folded]
    assert "var" not in ids  # collapsed onto canonical
    assert "canon" in ids


def test_search_claims_predicate_aliases_surface_canonical_claim_predicate():
    store = InMemoryStore()
    store.add_node(
        Node(
            id="claim:1",
            type="Claim",
            title="Alice created Acme",
            content="Alice created Acme",
            embedding=[1.0, 0.0],
            facets={"S_id": "alice", "P": "created", "O_id": "acme"},
        )
    )
    emb = StubEmbedder()

    hits = search_claims(
        "Alice Acme",
        k=5,
        store=store,
        embedder=emb,
        type="Claim",
        predicate_aliases={"created": "founded"},
    )

    assert hits[0][0].facets["P"] == "founded"
    assert store.get_node("claim:1").facets["P"] == "created"
