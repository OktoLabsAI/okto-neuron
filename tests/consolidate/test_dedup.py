"""Tests for intra-batch candidate dedup (collapse_duplicates)."""

from __future__ import annotations

from okto_neuron.companion import _collapse_exact_edge_candidates
from okto_neuron.consolidate import EdgeCandidate, NodeCandidate, collapse_duplicates
from okto_neuron.semantic_surface import build_surface_record


class _StubEmbedder:
    """Deterministic toy embedder: identical text -> identical vector."""

    dim = 27

    def embed(self, text: str) -> list[float]:
        # A crude but stable bag-of-chars vector over a fixed alphabet.
        alphabet = "abcdefghijklmnopqrstuvwxyz "
        t = text.casefold()
        return [float(t.count(ch)) for ch in alphabet]


def test_same_entity_twice_collapses_to_one_node() -> None:
    nodes = [
        NodeCandidate(type="InformationObject", title="Marginalia", content="a knowledge graph"),
        NodeCandidate(type="InformationObject", title="marginalia ", content="local-first KG"),
    ]
    deduped, _ = collapse_duplicates(nodes, [], embedder=_StubEmbedder())
    assert len(deduped) == 1
    assert deduped[0].title == "Marginalia"  # survivor = first occurrence


def test_explicit_merge_block_keeps_exact_batch_pair_distinct() -> None:
    first = NodeCandidate(type="Concept", title="Mercury", content="planet")
    second = NodeCandidate(type="Concept", title="Mercury", content="element")

    deduped, _ = collapse_duplicates(
        [first, second],
        [],
        merge_blocked=lambda left, right: (
            {left, right} == {first.candidate_id, second.candidate_id}
        ),
    )

    assert deduped == [first, second]


def test_exact_surface_collapses_unicode_equivalent_titles_without_rehashing() -> None:
    first = NodeCandidate(type="Concept", title="\u0390", content="first")
    equivalent = NodeCandidate(type="Concept", title="\u03aa\u0301", content="second")
    original_ids = (first.candidate_id, equivalent.candidate_id)

    deduped, _ = collapse_duplicates([first, equivalent], [])

    assert len(deduped) == 1
    assert deduped[0].candidate_id == original_ids[0]
    assert first.candidate_id == original_ids[0]
    assert equivalent.candidate_id == original_ids[1]
    assert original_ids[0] != original_ids[1]


def test_discovery_only_separator_and_camelcase_matches_do_not_auto_collapse() -> None:
    nodes = [
        NodeCandidate(type="Concept", title="Graph_Store"),
        NodeCandidate(type="Concept", title="Graph Store"),
        NodeCandidate(type="Concept", title="GraphStore"),
    ]

    deduped, _ = collapse_duplicates(nodes, [])

    assert deduped == nodes


def test_exact_collapse_preserves_dropped_surface_aliases_and_flags() -> None:
    survivor = NodeCandidate(
        type="Concept",
        title="Café",
        content="first",
        surface=build_surface_record("Café", "Café"),
    )
    dropped = NodeCandidate(
        type="Concept",
        title="Cafe\u0301",
        content="second",
        surface=build_surface_record("  Cafe\u0301  ", "Cafe\u0301"),
    )

    deduped, _ = collapse_duplicates([survivor, dropped], [])

    assert len(deduped) == 1
    surface = deduped[0].surface
    assert surface is not None
    assert surface.source_surface == "Café"
    assert surface.canonical_title == "Café"
    assert "Cafe\u0301" in surface.aliases
    assert "non_nfc" in surface.normalization_flags
    assert "whitespace_normalized" in surface.normalization_flags


def test_source_grounding_selects_better_duplicate_survivor() -> None:
    weak = NodeCandidate(
        type="Agent",
        title="Frodo",
        content="A character whose journey to Mordor was written by Tolkien in 1944.",
    )
    grounded = NodeCandidate(
        type="Agent",
        title="Frodo",
        content="A hobbit who inherited Bag End and the Ring from Bilbo.",
    )
    deduped, _ = collapse_duplicates(
        [weak, grounded],
        [],
        embedder=_StubEmbedder(),
        source_text_by_id={
            weak.candidate_id: "This note discusses Tolkien's 1944 writing chronology.",
            grounded.candidate_id: "Frodo inherited Bag End and the Ring from Bilbo.",
        },
    )

    assert len(deduped) == 1
    assert deduped[0].candidate_id == grounded.candidate_id


def test_source_grounding_keeps_non_duplicate_order_stable() -> None:
    weak = NodeCandidate(
        type="Agent",
        title="Frodo",
        content="A character whose journey to Mordor was written by Tolkien in 1944.",
    )
    middle = NodeCandidate(
        type="InformationObject",
        title="The Fellowship of the Ring",
        content="The first volume of The Lord of the Rings.",
    )
    grounded = NodeCandidate(
        type="Agent",
        title="Frodo",
        content="A hobbit who inherited Bag End and the Ring from Bilbo.",
    )
    deduped, _ = collapse_duplicates(
        [weak, middle, grounded],
        [],
        embedder=_StubEmbedder(),
        source_text_by_id={
            weak.candidate_id: "This note discusses Tolkien's 1944 writing chronology.",
            middle.candidate_id: "The Fellowship of the Ring was published in 1954.",
            grounded.candidate_id: "Frodo inherited Bag End and the Ring from Bilbo.",
        },
    )

    assert [candidate.candidate_id for candidate in deduped] == [
        grounded.candidate_id,
        middle.candidate_id,
    ]


def test_source_grounding_does_not_reorder_edge_connected_same_title_entities() -> None:
    subject = NodeCandidate(
        type="Concept",
        title="Ring",
        content="A subject endpoint with weaker local text.",
    )
    target = NodeCandidate(
        type="Concept",
        title="Ring",
        content="A target endpoint with stronger local text about the One Ring.",
    )
    edge = EdgeCandidate(
        type="contrasts_with",
        src_ref=subject.candidate_id,
        dst_ref=target.candidate_id,
    )
    deduped, remapped = collapse_duplicates(
        [subject, target],
        [edge],
        embedder=_StubEmbedder(),
        source_text_by_id={
            subject.candidate_id: "Weak local source.",
            target.candidate_id: "The One Ring is a central artifact.",
        },
    )

    assert [candidate.candidate_id for candidate in deduped] == [
        subject.candidate_id,
        target.candidate_id,
    ]
    assert remapped == [edge]


def test_distinct_entities_stay_separate() -> None:
    nodes = [
        NodeCandidate(type="Agent", title="Jordan", content="a person"),
        NodeCandidate(type="Agent", title="Jordan Lee Carter", content="the project partner"),
    ]
    deduped, _ = collapse_duplicates(nodes, [], embedder=_StubEmbedder())
    assert len(deduped) == 2


def test_different_types_never_merge() -> None:
    nodes = [
        NodeCandidate(type="Agent", title="Marginalia", content="x"),
        NodeCandidate(type="Concept", title="Marginalia", content="x"),
    ]
    deduped, _ = collapse_duplicates(nodes, [], embedder=_StubEmbedder())
    assert len(deduped) == 2


def test_edges_remapped_to_survivor_no_dangling() -> None:
    dup_a = NodeCandidate(type="InformationObject", title="Marginalia", content="first")
    dup_b = NodeCandidate(type="InformationObject", title="Marginalia", content="second")
    partner = NodeCandidate(type="Agent", title="Jordan Lee Carter", content="partner")
    # edge references the DROPPED duplicate's id.
    edge = EdgeCandidate(
        type="hasPartner", src_ref=dup_b.candidate_id, dst_ref=partner.candidate_id
    )
    deduped, remapped = collapse_duplicates(
        [dup_a, dup_b, partner], [edge], embedder=_StubEmbedder()
    )
    survivor_ids = {c.candidate_id for c in deduped}
    assert dup_b.candidate_id not in survivor_ids  # dropped
    assert len(remapped) == 1
    assert remapped[0].src_ref == dup_a.candidate_id  # remapped to survivor
    # no dangling: both endpoints exist among survivors
    assert remapped[0].src_ref in survivor_ids
    assert remapped[0].dst_ref in survivor_ids


def test_transitive_edge_endpoint_guard_blocks_third_candidate() -> None:
    # Regression for deep-review 3.8: s, a, b all share a normalized title; a and
    # b are the two ends of a real edge (distinct by construction). The OLD veto
    # only checked a candidate against the survivor slot's OWN id, so a merged
    # into s unchecked (a is not connected to s), then b ALSO merged into s
    # unchecked (b is not connected to s EITHER -- only to a) -- silently fusing
    # the edge's two endpoints into one node and dropping the relationship as a
    # self-loop. The fix tracks the full closure folded into each survivor: once
    # a folds into s, s's closure is {s, a}, and b is vetoed because b IS
    # connected to a. b must stay a distinct survivor, and the a->b edge (a
    # remapped to s) must survive intact rather than self-looping and vanishing.
    s = NodeCandidate(type="Concept", title="Loop", content="survivor")
    a = NodeCandidate(type="Concept", title="Loop", content="first")
    b = NodeCandidate(type="Concept", title="Loop", content="second")
    edge = EdgeCandidate(type="relatedTo", src_ref=a.candidate_id, dst_ref=b.candidate_id)
    deduped, remapped = collapse_duplicates([s, a, b], [edge], embedder=_StubEmbedder())
    survivor_ids = {c.candidate_id for c in deduped}
    assert len(deduped) == 2  # a folds into s; b is vetoed and stays distinct
    assert s.candidate_id in survivor_ids
    assert b.candidate_id in survivor_ids
    assert a.candidate_id not in survivor_ids  # a merged into s
    assert len(remapped) == 1
    assert remapped[0].src_ref == s.candidate_id  # a's id remapped to survivor s
    assert remapped[0].dst_ref == b.candidate_id  # b never merged -> not a self-loop


def test_tags_unioned_facets_merged_survivor_wins() -> None:
    a = NodeCandidate(
        type="Concept", title="X", content="a", tags=("t1",), facets={"k": "survivor"}
    )
    b = NodeCandidate(
        type="Concept", title="X", content="b", tags=("t2",), facets={"k": "drop", "extra": 1}
    )
    deduped, _ = collapse_duplicates([a, b], [], embedder=_StubEmbedder())
    assert len(deduped) == 1
    surv = deduped[0]
    assert set(surv.tags) == {"t1", "t2"}
    assert surv.facets["k"] == "survivor"  # survivor wins on conflict
    assert surv.facets["extra"] == 1


def test_edge_connected_endpoints_never_merge() -> None:
    """Even with the SAME normalized title + type, two candidates joined by a
    staged edge are distinct by construction and must not collapse (which would
    self-loop the edge and drop its Claim)."""
    subj = NodeCandidate(type="Concept", title="Topic", content="block text")
    obj = NodeCandidate(type="Concept", title="Topic", content="block text")
    # different content keeps candidate_ids distinct despite identical titles
    obj2 = obj.model_copy(update={"content": "block text 2"})
    edge = EdgeCandidate(type="relates_to", src_ref=subj.candidate_id, dst_ref=obj2.candidate_id)
    deduped, remapped = collapse_duplicates([subj, obj2], [edge], embedder=_StubEmbedder())
    assert len(deduped) == 2  # not merged
    assert len(remapped) == 1
    assert remapped[0].src_ref != remapped[0].dst_ref  # no self-loop


def test_no_duplicates_is_passthrough() -> None:
    nodes = [
        NodeCandidate(type="Agent", title="A", content="x"),
        NodeCandidate(type="Agent", title="B", content="y"),
    ]
    edges = [
        EdgeCandidate(type="knows", src_ref=nodes[0].candidate_id, dst_ref=nodes[1].candidate_id)
    ]
    deduped, remapped = collapse_duplicates(nodes, edges, embedder=_StubEmbedder())
    assert len(deduped) == 2
    assert len(remapped) == 1


def test_exact_edge_batch_collapse_keeps_first_and_reports_folded_anchor() -> None:
    first = EdgeCandidate(
        type="related-to",
        src_ref="subject",
        dst_ref="object",
        block_id="block-1",
        content_hash="a" * 64,
    )
    duplicate = EdgeCandidate(
        type="related_to",
        src_ref="subject",
        dst_ref="object",
        block_id="block-2",
        content_hash="b" * 64,
    )

    survivors, folded = _collapse_exact_edge_candidates([first, duplicate])

    assert survivors == [first]
    assert folded == [(duplicate, first)]
