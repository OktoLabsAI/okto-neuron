"""Within-batch (candidate-vs-candidate) entity resolution — Option B.

``judge_within_batch`` closes the twin-entity gap: two look-alikes extracted in
one ``remember()`` batch (e.g. an ``alice`` node from frontmatter and an
``agent:alice`` node from prose) are both novel to the committed store, so the
store-facing judge never compares them to each other. This pass does — gated by
the embedding band + same-type, with the edge-connected guard that keeps a
relationship's subject and object from ever fusing.
"""

from __future__ import annotations

import logging

import pytest

from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.core.schema import Node
from okto_neuron.resolve import COMPETING_NAME_CAP, MergeVerdict, judge_within_batch
from okto_neuron.semantic_surface import build_surface_record


class _StubEmbedder:
    """Only a fallback — every candidate below carries an explicit embedding."""

    dim = 3

    def embed(self, text: str) -> list[float]:  # noqa: ARG002 - never called (embeddings preset)
        return [0.0, 0.0, 0.0]


class _StubJudge:
    """Keyed on (candidate.title, existing.title); default DISTINCT. Records the
    relationship context each side was handed so the assembler can be asserted."""

    def __init__(self, verdicts: dict) -> None:
        self._verdicts = verdicts
        self.calls: list = []
        self.contexts: dict[tuple[str, str], tuple[str, str]] = {}

    def judge(
        self,
        candidate: NodeCandidate,
        existing: Node,
        *,
        candidate_context: str = "",
        existing_context: str = "",
    ) -> MergeVerdict:
        self.calls.append((candidate.title, existing.title))
        self.contexts[(candidate.title, existing.title)] = (
            candidate_context,
            existing_context,
        )
        return self._verdicts.get((candidate.title, existing.title), MergeVerdict(same=False))


def _cand(type_: str, title: str, emb: list[float], content: str = "") -> NodeCandidate:
    return NodeCandidate(type=type_, title=title, content=content, embedding=tuple(emb))


class TestJudgeWithinBatch:
    def test_merges_twin_into_first_occurrence(self) -> None:
        # alice (frontmatter, first) and agent:alice (prose, second) — same entity,
        # aligned embeddings, no edge between them. Second merges into first.
        alice = _cand("Agent", "alice", [1.0, 0.0, 0.0])
        agent_alice = _cand("Agent", "agent:alice", [1.0, 0.0, 0.0])
        edge = EdgeCandidate(type="authored", src_ref=agent_alice.candidate_id, dst_ref="doc-1")
        judge = _StubJudge({("agent:alice", "alice"): MergeVerdict(same=True, confidence=0.95)})

        out = judge_within_batch(
            [alice, agent_alice], [edge], judge=judge, embedder=_StubEmbedder()
        )

        assert out.survivors == [alice]
        assert out.merged_into == {agent_alice.candidate_id: alice.candidate_id}
        # the prose mention's edge still lands, remapped onto the survivor
        assert len(out.edges) == 1
        assert out.edges[0].src_ref == alice.candidate_id
        assert out.edges[0].dst_ref == "doc-1"

    def test_negative_cache_skips_judge_and_batch_merge(self) -> None:
        first = _cand("Agent", "Casey", [1.0, 0.0, 0.0])
        second = _cand("Agent", "Casey Buck", [1.0, 0.0, 0.0])
        judge = _StubJudge({("Casey Buck", "Casey"): MergeVerdict(same=True, confidence=0.99)})

        out = judge_within_batch(
            [first, second],
            [],
            judge=judge,
            embedder=_StubEmbedder(),
            merge_blocked=lambda left, right: (
                {left, right} == {first.candidate_id, second.candidate_id}
            ),
        )

        assert out.survivors == [first, second]
        assert out.merged_into == {}
        assert judge.calls == []

    def test_merge_retains_variant_surface_evidence(self) -> None:
        survivor = NodeCandidate(
            type="Agent",
            title="alice",
            embedding=(1.0, 0.0, 0.0),
            surface=build_surface_record(" alice ", "alice"),
        )
        variant = NodeCandidate(
            type="Agent",
            title="agent:alice",
            embedding=(1.0, 0.0, 0.0),
            surface=build_surface_record("agent:alice", "agent:alice"),
        )
        judge = _StubJudge({("agent:alice", "alice"): MergeVerdict(same=True, confidence=0.95)})

        out = judge_within_batch([survivor, variant], [], judge=judge, embedder=_StubEmbedder())

        surface = out.survivors[0].surface
        assert surface is not None
        assert surface.source_surface == " alice "
        assert surface.canonical_title == "alice"
        assert "agent:alice" in surface.aliases
        assert "whitespace_normalized" in surface.normalization_flags

    def test_distinct_lookalike_kept(self) -> None:
        nx = _cand("Agent", "NX", [0.0, 1.0, 0.0])
        nx_lab = _cand("Agent", "NX Lab", [0.0, 1.0, 0.0])
        judge = _StubJudge({("NX Lab", "NX"): MergeVerdict(same=False, confidence=0.95)})

        out = judge_within_batch([nx, nx_lab], [], judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [nx, nx_lab]
        assert out.merged_into == {}
        assert judge.calls == [("NX Lab", "NX")]  # was asked, said distinct

    def test_low_confidence_kept_distinct(self) -> None:
        a = _cand("Agent", "alice", [1.0, 0.0, 0.0])
        b = _cand("Agent", "agent:alice", [1.0, 0.0, 0.0])
        judge = _StubJudge({("agent:alice", "alice"): MergeVerdict(same=True, confidence=0.5)})

        out = judge_within_batch([a, b], [], judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [a, b]
        assert out.merged_into == {}

    def test_edge_connected_pair_never_merged(self) -> None:
        # subject and object of one relationship: distinct by construction. Even
        # with aligned embeddings and a judge that would say "same", the guard
        # must veto — the judge is never even asked.
        boss = _cand("Agent", "boss", [1.0, 0.0, 0.0])
        report = _cand("Agent", "report", [1.0, 0.0, 0.0])
        edge = EdgeCandidate(type="manages", src_ref=boss.candidate_id, dst_ref=report.candidate_id)
        judge = _StubJudge({("report", "boss"): MergeVerdict(same=True, confidence=0.99)})

        out = judge_within_batch([boss, report], [edge], judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [boss, report]
        assert out.merged_into == {}
        assert judge.calls == []  # guard fired before any LLM call
        assert out.edges == [edge]

    def test_below_band_skips_judge(self) -> None:
        a = _cand("Agent", "alice", [1.0, 0.0, 0.0])
        z = _cand("Agent", "zeta", [0.0, 0.0, 1.0])  # orthogonal → cosine 0 < 0.82
        judge = _StubJudge({})

        out = judge_within_batch([a, z], [], judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [a, z]
        assert judge.calls == []

    def test_legal_suffix_alias_reaches_judge_below_band(self) -> None:
        publisher = _cand("Agent", "Ballantine Books", [1.0, 0.0, 0.0])
        alias = _cand("Agent", "Ballantine", [0.0, 0.0, 1.0])
        judge = _StubJudge(
            {("Ballantine", "Ballantine Books"): MergeVerdict(same=True, confidence=0.9)}
        )

        out = judge_within_batch([publisher, alias], [], judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [publisher]
        assert out.merged_into == {alias.candidate_id: publisher.candidate_id}
        assert judge.calls == [("Ballantine", "Ballantine Books")]

    def test_non_suffix_token_subset_reaches_judge_below_band(self) -> None:
        """Blocking must never veto a pair (Christen 2012 ch.4; Papadakis et
        al. 2020) — recall is blocking's job, precision is the judge's. The OLD
        invariant here dropped a non-suffix token-subset pair below the
        embedding band BEFORE the judge ever saw it, on the assumption that low
        cosine meant "not worth asking". That assumption is wrong for short
        names: a real vault's candidate ledger shows Agent "Saulo" was only
        ever compared to the unrelated "Raphael" (cosine 1.0), while the true
        duplicate "Saulo"/"Saulo Lima" was never compared at all, because a
        single embedding key is degenerate for short person/company names. The
        fix is to ADMIT "Apple"/"Apple Records" to the judge and let its own
        conservative verdict (DISTINCT here) supply the precision this test
        used to fake at the blocking layer."""
        company = _cand("Agent", "Apple", [1.0, 0.0, 0.0])
        label = _cand("Agent", "Apple Records", [0.0, 0.0, 1.0])
        judge = _StubJudge({("Apple Records", "Apple"): MergeVerdict(same=False, confidence=0.9)})

        out = judge_within_batch([company, label], [], judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [company, label]
        assert out.merged_into == {}
        assert judge.calls != []

    def test_transitive_collapse_into_single_survivor(self) -> None:
        # A~B and C~A: all three are one entity. B and C both merge into A. The
        # key assertion is C is judged against A (the survivor), never B — proving
        # merged-away candidates drop out of the comparison set.
        a = _cand("Agent", "alex", [1.0, 0.0, 0.0])
        b = _cand("Agent", "user:alex", [1.0, 0.0, 0.0])
        c = _cand("Agent", "Marcus", [1.0, 0.0, 0.0])
        judge = _StubJudge(
            {
                ("user:alex", "alex"): MergeVerdict(same=True, confidence=0.9),
                ("Marcus", "alex"): MergeVerdict(same=True, confidence=0.9),
            }
        )

        out = judge_within_batch([a, b, c], [], judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [a]
        assert out.merged_into == {
            b.candidate_id: a.candidate_id,
            c.candidate_id: a.candidate_id,
        }
        # C compared against A, not B (B already merged away)
        assert ("Marcus", "alex") in judge.calls
        assert ("Marcus", "user:alex") not in judge.calls

    def test_transitive_edge_endpoint_guard_blocks_third_candidate(self) -> None:
        # Regression for deep-review 3.17: a, b, c are all same-type/aligned
        # look-alikes; b and c are the two ends of a real edge (distinct by
        # construction), while a is an unrelated common candidate the judge
        # independently calls "same" as BOTH b and c. The OLD veto only checked
        # the incoming candidate against the survivor slot's OWN id, so: b
        # merges into a (b is not connected to a) -- fine -- then c ALSO merges
        # into a (c is not connected to a EITHER, only to b) -- silently fusing
        # b and c into one node and dropping their relationship. The fix tracks
        # the full closure folded into each survivor: once b folds into a, a's
        # closure is {a, b}, and c is vetoed (and never even reaches the judge)
        # because c IS connected to b.
        a = _cand("Agent", "alex", [1.0, 0.0, 0.0])
        b = _cand("Agent", "user:alex", [1.0, 0.0, 0.0])
        c = _cand("Agent", "Marcus", [1.0, 0.0, 0.0])
        edge = EdgeCandidate(type="reports_to", src_ref=b.candidate_id, dst_ref=c.candidate_id)
        judge = _StubJudge(
            {
                ("user:alex", "alex"): MergeVerdict(same=True, confidence=0.9),
                ("Marcus", "alex"): MergeVerdict(same=True, confidence=0.9),
            }
        )

        out = judge_within_batch([a, b, c], [edge], judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [a, c]  # b merges into a; c is vetoed, stays distinct
        assert out.merged_into == {b.candidate_id: a.candidate_id}
        assert ("Marcus", "alex") not in judge.calls  # guard fired before the judge
        assert len(out.edges) == 1
        assert out.edges[0].src_ref == a.candidate_id  # b's ref remapped to survivor a
        assert out.edges[0].dst_ref == c.candidate_id  # c never merged -> no self-loop

    def test_different_type_not_compared(self) -> None:
        agent = _cand("Agent", "Ladybug", [1.0, 0.0, 0.0])
        concept = _cand("Concept", "Ladybug", [1.0, 0.0, 0.0])
        judge = _StubJudge({("Ladybug", "Ladybug"): MergeVerdict(same=True, confidence=0.99)})

        out = judge_within_batch([agent, concept], [], judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [agent, concept]
        assert judge.calls == []

    def test_passthrough_when_nothing_merges(self) -> None:
        a = _cand("Agent", "alice", [1.0, 0.0, 0.0])
        edge = EdgeCandidate(type="authored", src_ref=a.candidate_id, dst_ref="doc-1")
        judge = _StubJudge({})

        out = judge_within_batch([a], [edge], judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [a]
        assert out.merged_into == {}
        assert out.edges == [edge]
        assert judge.calls == []

    def test_assembles_two_way_relationship_context(self) -> None:
        # The corroborating + distinguishing signals the richer-context judge runs
        # on: both twins SIGNED the same Q1 SOW (sameness), and an incoming
        # `distinguished_from` edge (distinctness). The assembler must hand the
        # judge both — candidate side = the later twin's edges, existing side =
        # the survivor's — resolving sibling refs to titles and a literal to its
        # value. Source path is never assembled (it is constant within a batch).
        alice = _cand("Agent", "alice", [1.0, 0.0, 0.0])
        agent_alice = _cand("Agent", "agent:alice", [1.0, 0.0, 0.0])
        sow = _cand("InformationObject", "Q1 SOW", [0.0, 1.0, 0.0])
        bob = _cand("Agent", "bob-engineer", [0.0, 0.0, 1.0])
        edges = [
            EdgeCandidate(type="signed", src_ref=alice.candidate_id, dst_ref=sow.candidate_id),
            EdgeCandidate(
                type="signed", src_ref=agent_alice.candidate_id, dst_ref=sow.candidate_id
            ),
            # propositional literal (dst_ref empty, dst_literal set) renders as value
            EdgeCandidate(
                type="noted_standard", src_ref=agent_alice.candidate_id, dst_literal="CURIE"
            ),
            # incoming edge onto agent:alice — captured in the reverse direction
            EdgeCandidate(
                type="distinguished_from",
                src_ref=bob.candidate_id,
                dst_ref=agent_alice.candidate_id,
            ),
        ]
        judge = _StubJudge({("agent:alice", "alice"): MergeVerdict(same=True, confidence=0.95)})

        out = judge_within_batch(
            [alice, agent_alice, sow, bob], edges, judge=judge, embedder=_StubEmbedder()
        )

        cand_ctx, exist_ctx = judge.contexts[("agent:alice", "alice")]
        # candidate side = agent:alice's edges (ref→title, literal→value, incoming)
        assert cand_ctx.startswith("Relationships:")
        assert "agent:alice --signed--> Q1 SOW" in cand_ctx
        assert "agent:alice --noted_standard--> CURIE" in cand_ctx
        assert "bob-engineer --distinguished_from--> agent:alice" in cand_ctx
        # existing side = alice's edges
        assert "alice --signed--> Q1 SOW" in exist_ctx
        # the shared SOW corroborates → merge lands
        assert out.merged_into == {agent_alice.candidate_id: alice.candidate_id}

    def test_distinguishing_edge_survives_the_cap(self) -> None:
        # The precision guard: a high-degree entity has more than _REL_CAP (5)
        # relationships, and its one distinguishing edge (`distinguished_from`)
        # arrives LAST (incoming edges are appended after outgoing). In edge
        # order it would be dropped by the cap — taking with it the exact signal
        # that keeps a look-alike apart. The assembler must pull it to the front.
        alice = _cand("Agent", "alice", [1.0, 0.0, 0.0])
        agent_alice = _cand("Agent", "agent:alice", [1.0, 0.0, 0.0])
        bob = _cand("Agent", "bob-engineer", [0.0, 0.0, 1.0])
        edges = [
            # six benign outgoing literals on agent:alice — alone they fill the cap
            EdgeCandidate(type="touched", src_ref=agent_alice.candidate_id, dst_literal=f"doc-{i}")
            for i in range(6)
        ]
        # …and the distinguishing edge, appended last (incoming → sorts last)
        edges.append(
            EdgeCandidate(
                type="distinguished_from",
                src_ref=bob.candidate_id,
                dst_ref=agent_alice.candidate_id,
            )
        )
        judge = _StubJudge({("agent:alice", "alice"): MergeVerdict(same=False, confidence=0.95)})

        judge_within_batch([alice, agent_alice, bob], edges, judge=judge, embedder=_StubEmbedder())

        cand_ctx, _exist = judge.contexts[("agent:alice", "alice")]
        # the distinguishing edge was NOT dropped by the cap…
        assert "bob-engineer --distinguished_from--> agent:alice" in cand_ctx
        # …and the cap still held: 5 relationship lines, one benign edge dropped
        assert len([ln for ln in cand_ctx.splitlines() if "-->" in ln]) == 5

    def test_no_edges_yields_empty_context(self) -> None:
        # No relationships → empty context strings → judge sees today's bare prompt.
        a = _cand("Agent", "alice", [1.0, 0.0, 0.0])
        b = _cand("Agent", "agent:alice", [1.0, 0.0, 0.0])
        judge = _StubJudge({("agent:alice", "alice"): MergeVerdict(same=True, confidence=0.95)})

        judge_within_batch([a, b], [], judge=judge, embedder=_StubEmbedder())

        assert judge.contexts[("agent:alice", "alice")] == ("", "")

    def test_on_pair_fires_once_per_judged_pair_with_correct_args(self) -> None:
        """Fix 2 — the dedup ledger going silent for 30+ minutes while LLM
        calls were still firing. on_pair is the observability seam: it must
        fire exactly once per judged pair, with (candidate_id, target_id,
        verdict)."""
        alice = _cand("Agent", "alice", [1.0, 0.0, 0.0])
        agent_alice = _cand("Agent", "agent:alice", [1.0, 0.0, 0.0])
        judge = _StubJudge({("agent:alice", "alice"): MergeVerdict(same=True, confidence=0.95)})

        calls: list[tuple[str, str, MergeVerdict]] = []
        out = judge_within_batch(
            [alice, agent_alice],
            [],
            judge=judge,
            embedder=_StubEmbedder(),
            on_pair=lambda *args: calls.append(args),
        )

        assert calls == [
            (
                agent_alice.candidate_id,
                alice.candidate_id,
                MergeVerdict(same=True, confidence=0.95),
            )
        ]
        assert out.merged_into == {agent_alice.candidate_id: alice.candidate_id}

    def test_on_pair_raising_does_not_abort_the_pass(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A raising on_pair is observability-only — it must never abort the
        dedup pass or change its outcome — but it is logged, so a callback
        whose signature drifted cannot silently drop every per-pair row."""
        alice = _cand("Agent", "alice", [1.0, 0.0, 0.0])
        agent_alice = _cand("Agent", "agent:alice", [1.0, 0.0, 0.0])
        judge = _StubJudge({("agent:alice", "alice"): MergeVerdict(same=True, confidence=0.95)})

        def _boom(*_args: object) -> None:
            raise RuntimeError("boom")

        with caplog.at_level(logging.WARNING, logger="okto_neuron.resolve"):
            out = judge_within_batch(
                [alice, agent_alice], [], judge=judge, embedder=_StubEmbedder(), on_pair=_boom
            )

        assert out.merged_into == {agent_alice.candidate_id: alice.candidate_id}
        assert any(
            r.getMessage() == "dedup on_pair callback failed" and r.exc_info
            for r in caplog.records
        )

    def test_on_pair_default_none_unchanged(self) -> None:
        """Omitting on_pair (the default) is byte-for-byte identical to the
        pre-existing behaviour."""
        nx = _cand("Agent", "NX", [0.0, 1.0, 0.0])
        nx_lab = _cand("Agent", "NX Lab", [0.0, 1.0, 0.0])
        judge = _StubJudge({("NX Lab", "NX"): MergeVerdict(same=False, confidence=0.95)})

        out = judge_within_batch([nx, nx_lab], [], judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [nx, nx_lab]
        assert out.merged_into == {}


class TestCompetingNamesContext:
    """Part B, within-batch twin of ``judge_against_store``'s competing-names
    context (see tests/resolve/test_merge_judge.py::TestCompetingNamesContext):
    the judge is handed the OTHER kept batch survivors' titles sharing the
    judged pair's leading token, so "no conflicting surname" (see
    ``_VERDICT_SYSTEM``) is a checked fact instead of a guess."""

    def test_lists_competing_full_name_when_present(self) -> None:
        voss = _cand("Agent", "Aurelin Voss", [1.0, 0.0, 0.0])
        kade = _cand("Agent", "Aurelin Kade", [0.0, 1.0, 0.0])
        aurelin = _cand("Agent", "Aurelin", [1.0, 0.0, 0.0])
        judge = _StubJudge({("Aurelin", "Aurelin Voss"): MergeVerdict(same=True, confidence=0.95)})

        out = judge_within_batch([voss, kade, aurelin], [], judge=judge, embedder=_StubEmbedder())

        cand_ctx, exist_ctx = judge.contexts[("Aurelin", "Aurelin Voss")]
        assert "Other names in scope" in cand_ctx
        assert "Aurelin Kade" in cand_ctx
        assert "Other names in scope" in exist_ctx
        assert "Aurelin Kade" in exist_ctx
        assert out.merged_into == {aurelin.candidate_id: voss.candidate_id}

    def test_states_explicitly_when_no_competing_name(self) -> None:
        # An empty/absent block must not be silently ambiguous: absence of a
        # competitor is rendered as an explicit "none found", not nothing.
        voss = _cand("Agent", "Aurelin Voss", [1.0, 0.0, 0.0])
        aurelin = _cand("Agent", "Aurelin", [1.0, 0.0, 0.0])
        judge = _StubJudge({("Aurelin", "Aurelin Voss"): MergeVerdict(same=True, confidence=0.95)})

        judge_within_batch([voss, aurelin], [], judge=judge, embedder=_StubEmbedder())

        cand_ctx, exist_ctx = judge.contexts[("Aurelin", "Aurelin Voss")]
        assert 'Other names in scope: none found for "aurelin".' in cand_ctx
        assert 'Other names in scope: none found for "aurelin".' in exist_ctx

    def test_no_shared_leading_token_yields_no_competing_block(self) -> None:
        # "alice" / "agent:alice" don't share a leading token ("alice" vs
        # "agent") — confirms the block is skipped entirely, not rendered
        # empty, when the ambiguity this context supports doesn't apply.
        a = _cand("Agent", "alice", [1.0, 0.0, 0.0])
        b = _cand("Agent", "agent:alice", [1.0, 0.0, 0.0])
        judge = _StubJudge({("agent:alice", "alice"): MergeVerdict(same=True, confidence=0.95)})

        judge_within_batch([a, b], [], judge=judge, embedder=_StubEmbedder())

        cand_ctx, exist_ctx = judge.contexts[("agent:alice", "alice")]
        assert cand_ctx == ""
        assert exist_ctx == ""

    def test_caps_competing_names_at_the_configured_limit(self) -> None:
        # COMPETING_NAME_CAP + 1 other "Aurelin *" survivors plus the judged
        # target "Aurelin Voss": the competing-names POOL is every kept
        # survivor (uncapped), only the RENDERED list is capped.
        voss = _cand("Agent", "Aurelin Voss", [1.0, 0.0, 0.0])
        others = [
            _cand("Agent", f"Aurelin N{i:02d}", [0.0, 1.0, 0.0]) for i in range(COMPETING_NAME_CAP + 1)
        ]
        aurelin = _cand("Agent", "Aurelin", [1.0, 0.0, 0.0])
        judge = _StubJudge({("Aurelin", "Aurelin Voss"): MergeVerdict(same=True, confidence=0.95)})

        judge_within_batch([voss, *others, aurelin], [], judge=judge, embedder=_StubEmbedder())

        cand_ctx, _exist_ctx = judge.contexts[("Aurelin", "Aurelin Voss")]
        kept = [c.title for c in others[:COMPETING_NAME_CAP]]
        dropped = [c.title for c in others[COMPETING_NAME_CAP:]]
        for title in kept:
            assert title in cand_ctx
        for title in dropped:
            assert title not in cand_ctx
