"""Tests for Tier 0 cross-file dedup: reconcile_against_store.

Deterministic exact-(type, normalized-title) reconcile of new candidates against
the committed store. No embedding, no threshold, no LLM. This is the fix for the
self-duplication failure mode (one node per file for the same entity, e.g.
"Casey" → 9 nodes), and it must NOT merge genuinely-distinct entities that
happen to share a prefix (NX vs NX Lab).
"""

from __future__ import annotations

from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.core.schema import Node
from okto_neuron.resolve import reconcile_against_store
from okto_neuron.store.memory import InMemoryStore


def _store(*nodes: Node) -> InMemoryStore:
    store = InMemoryStore()
    for node in nodes:
        store.add_node(node)
    return store


def _node(node_id: str, *, type: str = "Agent", title: str = "", **facets) -> Node:
    return Node(id=node_id, type=type, title=title, content="", facets=facets or {})


def _cand(title: str, *, type: str = "Agent", content: str = "") -> NodeCandidate:
    return NodeCandidate(type=type, title=title, content=content)


class TestExactMatchMerge:
    def test_candidate_merges_into_existing_node(self) -> None:
        store = _store(_node("n-casey", title="Casey"))
        cand = _cand("Casey", content="said hi in a later meeting")  # different content → new id
        recon = reconcile_against_store([cand], [], store)
        assert recon.survivors == []
        assert recon.merged_into == {cand.candidate_id: "n-casey"}

    def test_negative_cache_blocks_exact_store_merge(self) -> None:
        store = _store(_node("n-casey", title="Casey"))
        cand = _cand("Casey", content="a different Casey")

        recon = reconcile_against_store(
            [cand],
            [],
            store,
            merge_blocked=lambda left, right: (left, right) == (cand.candidate_id, "n-casey"),
        )

        assert recon.survivors == [cand]
        assert recon.merged_into == {}

    def test_normalization_casefold_and_whitespace(self) -> None:
        store = _store(_node("n-nx", title="NX"))
        cand = _cand("  nx  ")
        recon = reconcile_against_store([cand], [], store)
        assert recon.merged_into == {cand.candidate_id: "n-nx"}

    def test_unicode_canonical_equivalence_merges_at_exact_tier(self) -> None:
        store = _store(_node("n-greek", type="Concept", title="\u0390"))
        cand = _cand("\u03aa\u0301", type="Concept")

        recon = reconcile_against_store([cand], [], store)

        assert recon.merged_into == {cand.candidate_id: "n-greek"}

    def test_discovery_separator_and_camelcase_variants_do_not_merge_at_exact_tier(
        self,
    ) -> None:
        store = _store(_node("n-graph", type="Concept", title="Graph Store"))
        underscore = _cand("Graph_Store", type="Concept")
        camel = _cand("GraphStore", type="Concept")

        recon = reconcile_against_store([underscore, camel], [], store)

        assert recon.merged_into == {}
        assert recon.survivors == [underscore, camel]

    def test_distinct_titles_not_merged(self) -> None:
        # The company-vs-team nuance: exact-string match must keep these apart.
        store = _store(_node("n-nx", title="NX"))
        cand = _cand("NX Lab")
        recon = reconcile_against_store([cand], [], store)
        assert recon.merged_into == {}
        assert [c.title for c in recon.survivors] == ["NX Lab"]

    def test_different_type_same_title_not_merged(self) -> None:
        store = _store(_node("n-sow-agent", type="Agent", title="SOW"))
        cand = _cand("SOW", type="InformationObject")
        recon = reconcile_against_store([cand], [], store)
        assert recon.merged_into == {}
        assert [c.title for c in recon.survivors] == ["SOW"]

    def test_novel_candidate_survives(self) -> None:
        store = _store(_node("n-nx", title="NX"))
        cand = _cand("ExampleCo")
        recon = reconcile_against_store([cand], [], store)
        assert [c.title for c in recon.survivors] == ["ExampleCo"]
        assert recon.merged_into == {}


class TestInfraExcluded:
    def test_infra_node_is_not_a_match_target(self) -> None:
        # An infra node sharing a title must never absorb a real extracted entity.
        store = _store(_node("infra-1", title="NX", infra=True))
        cand = _cand("NX")
        recon = reconcile_against_store([cand], [], store)
        assert recon.merged_into == {}
        assert [c.title for c in recon.survivors] == ["NX"]


class TestEdgeRemap:
    def test_edge_endpoint_remapped_to_existing_node(self) -> None:
        store = _store(_node("n-nx", title="NX"))
        nx = _cand("NX", content="re-mentioned")  # will merge into n-nx
        sam = _cand("Sam")  # novel survivor
        edge = EdgeCandidate(type="works_for", src_ref=sam.candidate_id, dst_ref=nx.candidate_id)
        recon = reconcile_against_store([nx, sam], [edge], store)
        assert [c.title for c in recon.survivors] == ["Sam"]
        assert len(recon.edges) == 1
        assert recon.edges[0].src_ref == sam.candidate_id
        assert recon.edges[0].dst_ref == "n-nx"  # remapped onto the existing node

    def test_edge_connected_pair_does_not_both_collapse_onto_same_node(self) -> None:
        # Regression for deep-review 3.3: a and b are the two ends of a real
        # edge (distinct by construction) and both exact-title-match the SAME
        # pre-existing store node. reconcile_against_store previously had NO
        # edge-endpoint guard at all, so both merged into "n-nx" -- destroying
        # one of the two distinct entities and silently dropping their
        # relationship as a self-loop (recon.edges == []). The fix vetoes the
        # second merge via the shared EdgeEndpointGuard: a merges into n-nx
        # first, then b is blocked because b is edge-connected to a (now part
        # of n-nx's closure). b survives as a distinct candidate and the
        # relationship lands intact, remapped onto the surviving store node.
        store = _store(_node("n-nx", title="NX"))
        a = _cand("NX", content="mention A")
        b = _cand("NX", content="mention B")
        edge = EdgeCandidate(type="rel", src_ref=a.candidate_id, dst_ref=b.candidate_id)
        recon = reconcile_against_store([a, b], [edge], store)
        assert recon.merged_into == {a.candidate_id: "n-nx"}  # only a merges
        assert [c.candidate_id for c in recon.survivors] == [b.candidate_id]
        assert len(recon.edges) == 1
        assert recon.edges[0].src_ref == "n-nx"  # a's ref remapped to the store node
        assert recon.edges[0].dst_ref == b.candidate_id  # b never merged -> no self-loop

    def test_direct_edge_to_own_merge_target_still_merges(self) -> None:
        # Companion to the transitive-guard test above: the guard must NOT veto
        # a candidate's own DIRECT edge to the exact store node it exact-title-
        # matches -- that is expected and harmless (the remap turns it into a
        # self-loop, dropped as always). Only a batch SIBLING that already
        # folded into that store node should block (blocks_external, not the
        # self-inclusive blocks() collapse_duplicates/judge_within_batch use).
        store = _store(_node("n-nx", title="NX"))
        nx = _cand("NX", content="self-referential mention")
        edge = EdgeCandidate(type="alias_of", src_ref=nx.candidate_id, dst_ref="n-nx")
        recon = reconcile_against_store([nx], [edge], store)
        assert recon.merged_into == {nx.candidate_id: "n-nx"}
        assert recon.edges == []  # self-loop from the remap, dropped as before

    def test_no_merge_leaves_edges_untouched(self) -> None:
        store = _store(_node("n-nx", title="NX"))
        x = _cand("ExampleCo")
        y = _cand("Sam")
        edge = EdgeCandidate(type="rel", src_ref=y.candidate_id, dst_ref=x.candidate_id)
        recon = reconcile_against_store([x, y], [edge], store)
        assert recon.edges == [edge]
        assert recon.merged_into == {}
