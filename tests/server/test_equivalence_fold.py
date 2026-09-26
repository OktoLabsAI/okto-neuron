"""Read-time equivalence fold for Browse/Graph (ADR 0009 P2) — pure-transform
unit tests. No store, no vault, no LLM: the fold helpers operate on already-built
node/edge rows + a ``member_id -> canonical_id`` map.
"""

from __future__ import annotations

from types import SimpleNamespace

from okto_neuron.server import _curation


def _node(nid: str):
    return SimpleNamespace(id=nid)


def test_fold_node_list_none_is_noop() -> None:
    nodes = [_node("a"), _node("b")]
    kept, counts = _curation.fold_node_list(nodes, None)
    assert kept == nodes
    assert counts == {}


def test_fold_node_list_collapses_variants_with_badge() -> None:
    # b and c fold onto canonical a.
    nodes = [_node("a"), _node("b"), _node("c"), _node("d")]
    eq = {"a": "a", "b": "a", "c": "a", "d": "d"}
    kept, counts = _curation.fold_node_list(nodes, eq)
    kept_ids = [str(n.id) for n in kept]
    assert kept_ids == ["a", "d"]
    assert counts == {"a": 2}


def test_fold_node_list_keeps_variant_when_canonical_absent() -> None:
    # Canonical 'a' is NOT in this page → keep variant 'b' as a stand-in (so a
    # paginated slice never silently loses a node).
    nodes = [_node("b"), _node("d")]
    eq = {"a": "a", "b": "a", "d": "d"}
    kept, counts = _curation.fold_node_list(nodes, eq)
    assert [str(n.id) for n in kept] == ["b", "d"]
    assert counts == {}


def test_fold_graph_none_is_noop() -> None:
    nodes = [{"id": "a"}, {"id": "b"}]
    edges = [{"src": "a", "dst": "b", "type": "rel"}]
    fn, fe = _curation.fold_graph(nodes, edges, None)
    assert fn == nodes
    assert fe == edges


def test_fold_graph_remaps_edges_and_drops_self_loops() -> None:
    # b folds onto a. An edge b->c must become a->c; an edge a->b becomes a self
    # loop a->a and must be dropped.
    nodes = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    edges = [
        {"src": "b", "dst": "c", "type": "rel"},  # -> a->c
        {"src": "a", "dst": "b", "type": "rel"},  # -> a->a (self-loop, dropped)
        {"src": "a", "dst": "c", "type": "rel"},  # stays a->c (dup of the first)
    ]
    eq = {"a": "a", "b": "a", "c": "c"}
    fn, fe = _curation.fold_graph(nodes, edges, eq)
    kept_ids = sorted(str(n["id"]) for n in fn)
    assert kept_ids == ["a", "c"]  # variant b collapsed away
    # Only one a->c edge survives (remapped + deduped), self-loop dropped.
    assert fe == [{"src": "a", "dst": "c", "type": "rel"}]


def test_fold_graph_drops_edges_to_absent_endpoints() -> None:
    # Edge points at 'b' which folds onto 'a' — but 'a' is present, so it remaps.
    # Edge points at 'z' which is mapped to canonical 'z' but z is not a node →
    # it survives only if both endpoints survive.
    nodes = [{"id": "a"}, {"id": "b"}]
    edges = [{"src": "a", "dst": "b", "type": "rel"}]
    eq = {"a": "a", "b": "a"}
    fn, fe = _curation.fold_graph(nodes, edges, eq)
    assert [str(n["id"]) for n in fn] == ["a"]
    # a->b remaps to a->a (self loop) → dropped; no edges survive.
    assert fe == []


def test_fold_graph_keeps_variant_edges_when_canonical_absent() -> None:
    # api_graph caps nodes by degree BEFORE the fold, so a variant V can be in the
    # view while its canonical C was capped out. V must represent ITSELF in-view —
    # else its real edge V->B is dropped (the inverse of the dangling-edge bug).
    nodes = [{"id": "V"}, {"id": "B"}]
    edges = [{"src": "V", "dst": "B", "type": "rel"}]
    eq = {"V": "C", "C": "C", "B": "B"}
    fn, fe = _curation.fold_graph(nodes, edges, eq)
    assert {str(n["id"]) for n in fn} == {"V", "B"}
    assert fe == [{"src": "V", "dst": "B", "type": "rel"}]


def test_canonical_for() -> None:
    eq = {"a": "a", "b": "a"}
    assert _curation.canonical_for("b", eq) == "a"  # b is a folded variant
    assert _curation.canonical_for("a", eq) is None  # a is its own canonical
    assert _curation.canonical_for("x", eq) is None  # unmapped
    assert _curation.canonical_for("b", None) is None  # no fold active
