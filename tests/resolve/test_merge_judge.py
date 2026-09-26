"""Tier 1 + Tier 2 entity resolution — embedding recall band + LLM merge judge."""

from __future__ import annotations

import logging

import pytest

from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.core.schema import Edge, Node
from okto_neuron.llm import LLMProviderError
from okto_neuron.resolve import (
    COMPETING_NAME_CAP,
    LEXICAL_ALIAS_CAP,
    _is_lexical_alias_pair,
    _VERDICT_SYSTEM,
    LLMMergeJudge,
    MergeVerdict,
    judge_against_store,
    parse_verdict,
)
from okto_neuron.store.memory import InMemoryStore


class _StubEmbedder:
    dim = 3

    def embed(self, text: str) -> list[float]:  # noqa: ARG002 - embeddings preset
        return [0.0, 0.0, 0.0]


class _StubJudge:
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


class _ReplyProvider:
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls = 0
        self.last_user = ""
        self.last_kwargs: dict[str, object] = {}

    def complete(
        self, messages, *, temperature: float = 0.0, max_tokens: int = 1024, **kwargs
    ) -> str:
        self.calls += 1
        self.last_kwargs = kwargs
        self.last_user = next((m.content for m in messages if m.role == "user"), "")
        return self._reply


class _DownProvider:
    def complete(
        self, messages, *, temperature: float = 0.0, max_tokens: int = 1024, **kwargs
    ) -> str:
        raise LLMProviderError("provider down")


def _cand(type_: str, title: str, emb: list[float], content: str = "") -> NodeCandidate:
    return NodeCandidate(type=type_, title=title, content=content, embedding=tuple(emb))


def _store_node(type_: str, title: str, emb: list[float], content: str = "") -> Node:
    base = NodeCandidate(type=type_, title=title, content=content, embedding=tuple(emb)).to_node()
    return base.model_copy(update={"id": f"existing-{title}"})


def _seed(store: InMemoryStore, *nodes: Node) -> None:
    for node in nodes:
        store.add_node(node)


class TestParseVerdict:
    def test_parses_same_true(self) -> None:
        v = parse_verdict('{"same": true, "confidence": 0.9, "reason": "alias"}')
        assert v.same is True
        assert v.confidence == pytest.approx(0.9)
        assert v.reason == "alias"

    def test_parses_with_think_and_prose(self) -> None:
        v = parse_verdict('<think>maybe</think> Verdict: {"same": false, "confidence": 0.7}')
        assert v.same is False
        assert v.confidence == pytest.approx(0.7)

    def test_unparseable_defaults_distinct(self) -> None:
        v = parse_verdict("no json at all here")
        assert v.same is False
        assert v.reason == "unparseable"

    def test_last_object_wins(self) -> None:
        v = parse_verdict(
            '{"same": true, "confidence": 0.9} then {"same": false, "confidence": 0.8}'
        )
        assert v.same is False

    def test_confidence_clamped(self) -> None:
        assert parse_verdict('{"same": true, "confidence": 1.5}').confidence == pytest.approx(1.0)
        assert parse_verdict('{"same": true, "confidence": -3}').confidence == pytest.approx(0.0)


class TestJudgeAgainstStore:
    def test_merges_true_duplicate(self) -> None:
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "Casey Buck", [1.0, 0.0, 0.0]))
        casey = _cand("Agent", "Casey", [1.0, 0.0, 0.0])
        edge = EdgeCandidate(type="works_on", src_ref=casey.candidate_id, dst_ref="other")
        judge = _StubJudge({("Casey", "Casey Buck"): MergeVerdict(same=True, confidence=0.9)})

        out = judge_against_store([casey], [edge], store, judge=judge, embedder=_StubEmbedder())

        assert out.survivors == []
        assert out.merged_into == {casey.candidate_id: "existing-Casey Buck"}
        assert len(out.edges) == 1
        assert out.edges[0].src_ref == "existing-Casey Buck"
        assert out.edges[0].dst_ref == "other"

    def test_negative_cache_skips_judge_and_store_merge(self) -> None:
        store = InMemoryStore()
        existing = _store_node("Agent", "Casey Buck", [1.0, 0.0, 0.0])
        _seed(store, existing)
        casey = _cand("Agent", "Casey", [1.0, 0.0, 0.0])
        judge = _StubJudge({("Casey", "Casey Buck"): MergeVerdict(same=True, confidence=0.99)})

        out = judge_against_store(
            [casey],
            [],
            store,
            judge=judge,
            embedder=_StubEmbedder(),
            merge_blocked=lambda left, right: (left, right) == (casey.candidate_id, existing.id),
        )

        assert out.survivors == [casey]
        assert out.merged_into == {}
        assert judge.calls == []

    def test_keeps_distinct_lookalike(self) -> None:
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "NX Lab", [0.0, 1.0, 0.0]))
        nx = _cand("Agent", "NX", [0.0, 1.0, 0.0])
        judge = _StubJudge({("NX", "NX Lab"): MergeVerdict(same=False, confidence=0.95)})

        out = judge_against_store([nx], [], store, judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [nx]
        assert out.merged_into == {}
        assert judge.calls == [("NX", "NX Lab")]

    def test_low_confidence_kept_distinct(self) -> None:
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "Casey Buck", [1.0, 0.0, 0.0]))
        casey = _cand("Agent", "Casey", [1.0, 0.0, 0.0])
        judge = _StubJudge({("Casey", "Casey Buck"): MergeVerdict(same=True, confidence=0.5)})

        out = judge_against_store([casey], [], store, judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [casey]
        assert out.merged_into == {}

    def test_confidence_boundary_merges(self) -> None:
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "Casey Buck", [1.0, 0.0, 0.0]))
        casey = _cand("Agent", "Casey", [1.0, 0.0, 0.0])
        judge = _StubJudge({("Casey", "Casey Buck"): MergeVerdict(same=True, confidence=0.8)})

        out = judge_against_store([casey], [], store, judge=judge, embedder=_StubEmbedder())

        assert out.merged_into == {casey.candidate_id: "existing-Casey Buck"}

    def test_on_pair_fires_once_per_judged_pair_with_correct_args(self) -> None:
        """Fix 2 — the dedup ledger going silent for 30+ minutes while LLM
        calls were still firing. on_pair is the observability seam: it must
        fire exactly once per judged pair, with (candidate_id, target_id,
        verdict)."""
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "Casey Buck", [1.0, 0.0, 0.0]))
        casey = _cand("Agent", "Casey", [1.0, 0.0, 0.0])
        judge = _StubJudge({("Casey", "Casey Buck"): MergeVerdict(same=True, confidence=0.9)})

        calls: list[tuple[str, str, MergeVerdict]] = []
        out = judge_against_store(
            [casey],
            [],
            store,
            judge=judge,
            embedder=_StubEmbedder(),
            on_pair=lambda *args: calls.append(args),
        )

        assert calls == [
            (casey.candidate_id, "existing-Casey Buck", MergeVerdict(same=True, confidence=0.9))
        ]
        assert out.merged_into == {casey.candidate_id: "existing-Casey Buck"}

    def test_on_pair_raising_does_not_abort_the_pass(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A raising on_pair is observability-only — it must never abort the
        dedup pass or change its outcome — but it is logged, so a callback
        whose signature drifted cannot silently drop every per-pair row."""
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "Casey Buck", [1.0, 0.0, 0.0]))
        casey = _cand("Agent", "Casey", [1.0, 0.0, 0.0])
        judge = _StubJudge({("Casey", "Casey Buck"): MergeVerdict(same=True, confidence=0.9)})

        def _boom(*_args: object) -> None:
            raise RuntimeError("boom")

        with caplog.at_level(logging.WARNING, logger="okto_neuron.resolve"):
            out = judge_against_store(
                [casey], [], store, judge=judge, embedder=_StubEmbedder(), on_pair=_boom
            )

        assert out.merged_into == {casey.candidate_id: "existing-Casey Buck"}
        assert any(
            r.getMessage() == "dedup on_pair callback failed" and r.exc_info
            for r in caplog.records
        )

    def test_on_pair_default_none_unchanged(self) -> None:
        """Omitting on_pair (the default) is byte-for-byte identical to the
        pre-existing behaviour."""
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "NX Lab", [0.0, 1.0, 0.0]))
        nx = _cand("Agent", "NX", [0.0, 1.0, 0.0])
        judge = _StubJudge({("NX", "NX Lab"): MergeVerdict(same=False, confidence=0.95)})

        out = judge_against_store([nx], [], store, judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [nx]
        assert out.merged_into == {}

    def test_no_band_neighbor_skips_judge(self) -> None:
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "Casey Buck", [1.0, 0.0, 0.0]))
        zeta = _cand("Agent", "Zeta", [0.0, 0.0, 1.0])
        judge = _StubJudge({})

        out = judge_against_store([zeta], [], store, judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [zeta]
        assert judge.calls == []

    def test_legal_suffix_alias_reaches_judge_below_band(self) -> None:
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "Houghton Mifflin Company", [1.0, 0.0, 0.0]))
        alias = _cand("Agent", "Houghton Mifflin", [0.0, 0.0, 1.0])
        judge = _StubJudge(
            {
                ("Houghton Mifflin", "Houghton Mifflin Company"): MergeVerdict(
                    same=True, confidence=0.9
                )
            }
        )

        out = judge_against_store([alias], [], store, judge=judge, embedder=_StubEmbedder())

        assert out.survivors == []
        assert out.merged_into == {alias.candidate_id: "existing-Houghton Mifflin Company"}
        assert judge.calls == [("Houghton Mifflin", "Houghton Mifflin Company")]

    def test_non_suffix_token_subset_reaches_judge_below_band(self) -> None:
        """Blocking must never veto a pair (Christen 2012 ch.4; Papadakis et
        al. 2020) — recall is blocking's job, precision is the judge's. The OLD
        invariant here was that a non-suffix token-subset pair below the
        embedding band was silently dropped BEFORE the judge ever saw it. That
        was wrong: cosine carries no reliable signal for short names at all — a
        real vault's candidate ledger shows Agent "Saulo" was only ever compared
        to the unrelated "Raphael" (cosine 1.0), while the true duplicate
        "Saulo"/"Saulo Lima" was never compared, because a single embedding key
        is degenerate for short person/company names. The fix is to ADMIT
        "Apple"/"Apple Records" to the judge and let the judge's own
        conservative verdict (DISTINCT for a company vs. a label it doesn't
        obviously subsume) supply the precision this test used to fake at the
        blocking layer."""
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "Apple Records", [1.0, 0.0, 0.0]))
        company = _cand("Agent", "Apple", [0.0, 0.0, 1.0])
        judge = _StubJudge({("Apple", "Apple Records"): MergeVerdict(same=False, confidence=0.9)})

        out = judge_against_store([company], [], store, judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [company]
        assert out.merged_into == {}
        assert judge.calls != []

    def test_self_loop_dropped_on_merge(self) -> None:
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "Casey Buck", [1.0, 0.0, 0.0]))
        casey = _cand("Agent", "Casey", [1.0, 0.0, 0.0])
        edge = EdgeCandidate(
            type="alias_of", src_ref=casey.candidate_id, dst_ref="existing-Casey Buck"
        )
        judge = _StubJudge({("Casey", "Casey Buck"): MergeVerdict(same=True, confidence=0.9)})

        out = judge_against_store([casey], [edge], store, judge=judge, embedder=_StubEmbedder())

        assert out.edges == []

    def test_transitive_edge_endpoint_guard_blocks_second_sibling(self) -> None:
        # Regression for deep-review 3.3, reproduced one tier later: two batch
        # candidates (casey_a, casey_b) both band-match the SAME existing store
        # node and are the two ends of a real edge (distinct by construction).
        # An always-"same" judge would merge BOTH into the store node absent a
        # guard, fusing the edge into a self-loop and dropping it -- destroying
        # one of the two distinct entities exactly like Tier 0's un-guarded
        # reconcile_against_store used to. The fix: once casey_a folds into the
        # store node, casey_b is vetoed because it is edge-connected to casey_a,
        # a member of the store node's now-folded closure (blocks_external).
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "Casey Buck", [1.0, 0.0, 0.0]))
        casey_a = _cand("Agent", "Casey", [1.0, 0.0, 0.0], content="mention A")
        casey_b = _cand("Agent", "Casey", [1.0, 0.0, 0.0], content="mention B")
        edge = EdgeCandidate(
            type="reports_to", src_ref=casey_a.candidate_id, dst_ref=casey_b.candidate_id
        )
        judge = _StubJudge({("Casey", "Casey Buck"): MergeVerdict(same=True, confidence=0.9)})

        out = judge_against_store(
            [casey_a, casey_b], [edge], store, judge=judge, embedder=_StubEmbedder()
        )

        assert out.merged_into == {casey_a.candidate_id: "existing-Casey Buck"}
        assert [c.candidate_id for c in out.survivors] == [casey_b.candidate_id]
        assert len(out.edges) == 1
        assert out.edges[0].src_ref == "existing-Casey Buck"  # casey_a remapped
        assert out.edges[0].dst_ref == casey_b.candidate_id  # casey_b never merged

    def test_direct_edge_to_own_merge_target_still_merges(self) -> None:
        # Companion to the transitive-guard test above: blocks_external must NOT
        # veto a candidate's own DIRECT edge to the exact store node it is being
        # judged against (see test_self_loop_dropped_on_merge) -- only a batch
        # SIBLING that already folded into that store node blocks. Guards
        # against over-broadening the fix to treat the store id itself as a
        # blocking closure member.
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "Casey Buck", [1.0, 0.0, 0.0]))
        casey = _cand("Agent", "Casey", [1.0, 0.0, 0.0])
        edge = EdgeCandidate(
            type="alias_of", src_ref=casey.candidate_id, dst_ref="existing-Casey Buck"
        )
        judge = _StubJudge({("Casey", "Casey Buck"): MergeVerdict(same=True, confidence=0.9)})

        out = judge_against_store([casey], [edge], store, judge=judge, embedder=_StubEmbedder())

        assert out.merged_into == {casey.candidate_id: "existing-Casey Buck"}
        assert out.edges == []  # self-loop from the remap, dropped as before

    def test_different_type_not_judged(self) -> None:
        store = InMemoryStore()
        _seed(store, _store_node("Concept", "Casey Buck", [1.0, 0.0, 0.0]))
        casey = _cand("Agent", "Casey", [1.0, 0.0, 0.0])
        judge = _StubJudge({("Casey", "Casey Buck"): MergeVerdict(same=True, confidence=0.99)})

        out = judge_against_store([casey], [], store, judge=judge, embedder=_StubEmbedder())

        assert out.survivors == [casey]
        assert judge.calls == []

    def test_assembles_relationship_context_both_sides(self) -> None:
        # candidate relationships come from the batch edge candidates; the existing
        # store node's come from its committed edges (neighbour ids → titles).
        store = InMemoryStore()
        _seed(
            store,
            _store_node("Agent", "Casey Buck", [1.0, 0.0, 0.0]),
            _store_node("Place", "ACME HQ", [0.0, 1.0, 0.0]),
        )
        store.add_edge(Edge(type="works_at", src="existing-Casey Buck", dst="existing-ACME HQ"))
        casey = _cand("Agent", "Casey", [1.0, 0.0, 0.0])
        # propositional literal on the candidate side renders as its value
        edge = EdgeCandidate(type="works_at", src_ref=casey.candidate_id, dst_literal="ACME")
        judge = _StubJudge({("Casey", "Casey Buck"): MergeVerdict(same=True, confidence=0.9)})

        judge_against_store([casey], [edge], store, judge=judge, embedder=_StubEmbedder())

        cand_ctx, exist_ctx = judge.contexts[("Casey", "Casey Buck")]
        assert "Casey --works_at--> ACME" in cand_ctx
        assert "Casey Buck --works_at--> ACME HQ" in exist_ctx


class TestLLMMergeJudge:
    def test_parses_provider_reply_and_merges(self) -> None:
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "Casey Buck", [1.0, 0.0, 0.0]))
        casey = _cand("Agent", "Casey", [1.0, 0.0, 0.0])
        provider = _ReplyProvider('{"same": true, "confidence": 0.95, "reason": "alias"}')

        out = judge_against_store(
            [casey], [], store, judge=LLMMergeJudge(provider), embedder=_StubEmbedder()
        )

        assert out.merged_into == {casey.candidate_id: "existing-Casey Buck"}
        assert provider.calls == 1

    def test_requests_structured_output(self) -> None:
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "Casey Buck", [1.0, 0.0, 0.0]))
        casey = _cand("Agent", "Casey", [1.0, 0.0, 0.0])
        provider = _ReplyProvider('{"same": true, "confidence": 0.95, "reason": "alias"}')

        judge_against_store(
            [casey], [], store, judge=LLMMergeJudge(provider), embedder=_StubEmbedder()
        )

        response_format = provider.last_kwargs["response_format"]
        assert response_format["type"] == "json_schema"
        schema = response_format["json_schema"]
        assert schema["name"] == "marginalia_merge_verdict"
        assert schema["schema"]["required"] == ["same", "confidence", "reason"]

    def test_distinct_reply_keeps_separate(self) -> None:
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "NX Lab", [0.0, 1.0, 0.0]))
        nx = _cand("Agent", "NX", [0.0, 1.0, 0.0])
        provider = _ReplyProvider('{"same": false, "confidence": 0.9}')

        out = judge_against_store(
            [nx], [], store, judge=LLMMergeJudge(provider), embedder=_StubEmbedder()
        )

        assert out.survivors == [nx]
        assert out.merged_into == {}

    def test_llm_unavailable_keeps_distinct(self) -> None:
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "Casey Buck", [1.0, 0.0, 0.0]))
        casey = _cand("Agent", "Casey", [1.0, 0.0, 0.0])

        out = judge_against_store(
            [casey], [], store, judge=LLMMergeJudge(_DownProvider()), embedder=_StubEmbedder()
        )

        assert out.survivors == [casey]
        assert out.merged_into == {}

    def test_relationship_context_reaches_the_prompt(self) -> None:
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "Casey Buck", [1.0, 0.0, 0.0]))
        casey = _cand("Agent", "Casey", [1.0, 0.0, 0.0])
        edge = EdgeCandidate(type="signed", src_ref=casey.candidate_id, dst_literal="SOW 2026 Q1")
        provider = _ReplyProvider('{"same": true, "confidence": 0.95}')

        judge_against_store(
            [casey], [edge], store, judge=LLMMergeJudge(provider), embedder=_StubEmbedder()
        )

        assert "Relationships:" in provider.last_user
        assert "Casey --signed--> SOW 2026 Q1" in provider.last_user
        assert "SUPPORTING evidence" in provider.last_user

    def test_no_relationships_leaves_prompt_bare(self) -> None:
        # No batch edges, no store edges, and titles that don't share a leading
        # token (so the competing-names context — see TestCompetingNamesContext
        # below — doesn't fire either) → empty context → today's bare prompt (no
        # Relationships block, no supporting-evidence note).
        store = InMemoryStore()
        _seed(store, _store_node("Agent", "Skyler Vance", [1.0, 0.0, 0.0]))
        dakota = _cand("Agent", "Dakota", [1.0, 0.0, 0.0])
        provider = _ReplyProvider('{"same": false, "confidence": 0.9}')

        judge_against_store(
            [dakota], [], store, judge=LLMMergeJudge(provider), embedder=_StubEmbedder()
        )

        assert "Relationships:" not in provider.last_user
        assert "SUPPORTING evidence" not in provider.last_user
        assert "Other names in scope" not in provider.last_user


class TestLexicalAliasRecall:
    """Which pairs are ALLOWED to reach the merge judge. Recall only — every
    pair admitted here is still ruled on by the judge, which already carries the
    right verdict for each shape."""

    def test_short_name_reaches_the_judge(self) -> None:
        """A bare given name vs the same person's fuller name. _VERDICT_SYSTEM
        already instructs SAME for this shape ("a distinctive single-token name
        that is a token-subset of a fuller name"), but nothing admitted the pair,
        so the judge was never asked and a real vault ended up holding Agent
        "Karine" and Agent "Karine Buchner" side by side."""
        for short, full in (
            ("Karine", "Karine Buchner"),
            ("Silmara", "Silmara Rodrigues"),
            ("Saulo", "Saulo Lima"),
            # Brazilian full name: given + middle + two surnames.
            ("Renato", "Renato André Alves Ferreira"),
        ):
            assert _is_lexical_alias_pair(short, full), (short, full)
            assert _is_lexical_alias_pair(full, short), (full, short)

    def test_legal_suffix_alias_still_recalled(self) -> None:
        assert _is_lexical_alias_pair("Ballantine", "Ballantine Books")

    def test_unrelated_names_are_not_admitted(self) -> None:
        for a, b in (
            ("Karine Buchner", "Silmara Rodrigues"),
            ("ECD", "ECF"),
            ("Contabilizei", "Husky"),
            ("Karine", "Karine"),
            ("Camila", "Karyse"),
            ("Saulo", "Raphael"),
        ):
            assert not _is_lexical_alias_pair(a, b), (a, b)

    def test_a_longer_phrase_is_admitted_not_vetoed(self) -> None:
        """Formerly ``test_a_longer_phrase_is_not_a_name_prefix``: the old
        implementation capped the long side at 4 tokens so a bare word was
        never treated as a prefix of an arbitrarily long phrase. That cap was a
        blocking-time PRECISION veto, which contradicts the disjunctive-
        blocking contract this module now follows (Christen 2012 ch.4;
        Papadakis et al. 2020: blocking optimizes recall, classification
        optimizes precision, and blocking must never veto a pair a lane would
        otherwise propose). "Guias" is a genuine token-subset prefix of "Guias
        DARF DAMSP TFE extras" by the same shared-first-token shape as
        "Karine"/"Karine Buchner" above; there is no principled place to draw a
        token-count line at the blocking layer, so the pair is admitted and the
        conservative judge is trusted to rule it DISTINCT, the same way it
        already does for "NX"/"NX Lab"."""
        assert _is_lexical_alias_pair("Guias", "Guias DARF DAMSP TFE extras")

    def test_jw_lane_recalls_a_spelling_variant(self) -> None:
        """A strong Jaro-Winkler score, length-gated so short coincidental
        near-spellings don't qualify, recalls a single-token spelling variant
        that neither the suffix, surname, nor subset lanes would ever catch
        (neither title contains the other as a token subset)."""
        assert _is_lexical_alias_pair("Waverly", "Waverley")

    def test_surname_lane_recalls_a_shared_surname(self) -> None:
        """Two full names sharing an exact surname, with no token-subset
        relationship between them (different first tokens, neither set
        contains the other) and a Jaro-Winkler score below the fuzzy floor —
        only the shared-surname lane admits this pair. Recall only: the judge
        still rules on whether they are the same person."""
        assert _is_lexical_alias_pair("Alex Waverly", "Jordan Waverly")

    def test_identifier_variants_still_reach_the_judge(self) -> None:
        """The pairs the surname-lane precision fix must NOT cost. An identifier
        or dated title whose short form is a token-subset of its long form rides
        ``token_subset``/``token_subset_unordered`` on its shared WORD tokens
        (``task``, ``darf``), never on the trailing number, so tightening
        ``_surname`` to reject an all-digit trailing token takes nothing away
        here — the first two pairs are real fragmentation observed on a live
        graph (``TASK-01`` stored as a Concept beside the full Activity title;
        three DARF nodes across two types). The last two guard the lanes the
        same fix runs closest to: an ALPHABETIC trailing token is still a
        surname (``lee``), and a bare-token containment still fires. The
        short-given-name pairs are pinned by ``test_short_name_reaches_the_judge``
        above."""
        for short, full in (
            ("TASK-01", "TASK-01: Add is_recurrent Property to Expense Model"),
            ("darf-unificado", "DARF Unificado jul/2026"),
            ("Casey Lee", "Dr Casey Lee"),
            ("NX", "NX Lab"),
        ):
            assert _is_lexical_alias_pair(short, full), (short, full)
            assert _is_lexical_alias_pair(full, short), (full, short)

    def test_shared_trailing_number_is_not_a_shared_surname(self) -> None:
        """REGRESSION: ``_surname`` returned the last token of a title, so
        ``TASK-01``, ``Sprint 01`` and ``CNAE 6201-5/01`` "shared the surname"
        ``01`` and every 2026-dated title shared ``2026`` — and the surname lane
        scores ``IDENTITY_EDGE_SCORE`` here, so those coincidences arrived tied
        at 1.0 with genuine matches. On a live 386-node graph that gave
        ``DARF Unificado jul/2026`` 15 candidates at 1.0, nearly all numeric
        collisions; ``LEXICAL_ALIAS_CAP`` then kept 5 of them by node-id
        tie-break and the true pair never reached the judge.

        This is a precision fix to a blocking SIGNAL, not a blocking-time veto
        (ADR 0042): no other lane proposes these pairs — Jaro-Winkler scores
        0.39-0.59, far below the fuzzy floor, and neither title's token set
        contains the other's."""
        for a, b in (
            ("TASK-01", "Sprint 01"),
            ("TASK-01", "MAHF_2026_01"),
            ("TASK-01", "CNAE 6201-5/01"),
            ("DARF Unificado jul/2026", "ECD 2026"),
        ):
            assert not _is_lexical_alias_pair(a, b), (a, b)
            assert not _is_lexical_alias_pair(b, a), (b, a)

    def test_unordered_subset_lane_recalls_reordered_containment(self) -> None:
        """Order-insensitive token-subset containment (ADR 0010 Axis 1):
        ``{nivod} ⊂ {ari, nivod}`` shares the distinctive token ``nivod`` but
        the first tokens differ, so the ORDERED subset lane misses it while
        the unordered one recovers it. Mirrors
        ``reconcile.candidates.test_token_subset_unordered_pairs_nivod_ari_nivod``."""
        assert _is_lexical_alias_pair("Nivod", "Ari Nivod")
        assert _is_lexical_alias_pair("Ari Nivod", "Nivod")


def _node_with_id(type_: str, title: str, node_id: str, emb: list[float]) -> Node:
    """Like ``_store_node``, but the id is set independently of the title so a
    test can control store iteration order (``InMemoryStore.list_nodes``
    yields nodes sorted by id) separately from lexical-match strength."""
    base = NodeCandidate(type=type_, title=title, content="", embedding=tuple(emb)).to_node()
    return base.model_copy(update={"id": node_id})


class TestLexicalAliasCapBudget:
    """The regression this task fixes: ADR 0042 widened
    ``_is_lexical_alias_pair`` into a disjunctive recall rule, and that lane fed
    an UNCAPPED ``store.list_nodes()`` scan in ``judge_against_store`` — every
    additional same-type node the wider rule matched cost one more LLM judge
    call, so judge volume grew with STORE SIZE, not candidate count (measured:
    25 -> 118 judge calls for the same 3 documents, a 4.7x increase, across one
    ingest cycle). ``LEXICAL_ALIAS_CAP`` bounds it; these tests prove the cap
    (a) actually bounds judge calls and (b) ranks by score rather than
    truncating arbitrarily, so a genuine strong match always still reaches the
    judge."""

    def test_lexical_lane_is_bounded_not_once_per_store_node(self) -> None:
        store = InMemoryStore()
        # 50 same-type nodes that ALL lexically match the candidate via the
        # shared-first-token subset lane ("karine" ⊂ "karine N") — an unbounded
        # lexical lane would judge all 50; the cap must judge far fewer.
        for i in range(50):
            _seed(store, _store_node("Agent", f"Karine {i}", [0.0, 0.0, 0.0]))
        karine = _cand("Agent", "Karine", [0.0, 0.0, 0.0])
        judge = _StubJudge({})  # never "same" — forces the full capped set to be judged

        out = judge_against_store(
            [karine], [], store, judge=judge, embedder=_StubEmbedder()
        )

        assert len(judge.calls) == LEXICAL_ALIAS_CAP
        assert len(judge.calls) < 50
        assert out.survivors == [karine]
        assert out.merged_into == {}

    def test_strongest_lexical_match_survives_the_cap(self) -> None:
        """A genuine short-name/full-name pair must still reach the judge even
        when 50 weaker lexical matches exist — the cap RANKS strongest-first
        (mirroring ``reconcile.candidates``' ``_split_oversize``), it does not
        arbitrarily truncate. The weak matches are seeded with ids that sort
        BEFORE the strong match's id, so a naive "first N in store-iteration-
        order" cap (no ranking) would have dropped the strong pair; only a
        score-ranked cap keeps it."""
        store = InMemoryStore()
        # 50 weaker matches: single-token spelling variants of "Waverly",
        # admitted only by the fuzzy Jaro-Winkler lane (score < IDENTITY_EDGE_SCORE).
        for i in range(50):
            _seed(
                store,
                _node_with_id("Agent", f"Waverly{i}", f"0-weak-{i:02d}", [0.0, 0.0, 0.0]),
            )
        # One genuine short-name/full-name pair: identity-grade (token-subset),
        # scores IDENTITY_EDGE_SCORE — strictly higher than any fuzzy match above.
        _seed(store, _node_with_id("Agent", "Waverly Chen", "z-strong", [0.0, 0.0, 0.0]))
        waverly = _cand("Agent", "Waverly", [0.0, 0.0, 0.0])
        judge = _StubJudge(
            {("Waverly", "Waverly Chen"): MergeVerdict(same=True, confidence=0.9)}
        )

        out = judge_against_store(
            [waverly], [], store, judge=judge, embedder=_StubEmbedder()
        )

        assert ("Waverly", "Waverly Chen") in judge.calls
        assert out.merged_into == {waverly.candidate_id: "z-strong"}


class TestCompetingNamesContext:
    """Part B: the judge is handed the OTHER same-type store node titles that
    share the pair's leading token, so "no conflicting surname" (see
    ``_VERDICT_SYSTEM``) becomes a checked fact instead of a guess. See
    ``_competing_names_context`` for the gathering/rendering logic this
    exercises."""

    def test_lists_competing_full_name_when_present(self) -> None:
        store = InMemoryStore()
        _seed(
            store,
            _node_with_id("Agent", "Aurelin Voss", "id-aurelin-voss", [0.0, 0.0, 0.0]),
            _node_with_id("Agent", "Aurelin Kade", "id-aurelin-kade", [0.0, 0.0, 0.0]),
        )
        aurelin = _cand("Agent", "Aurelin", [0.0, 0.0, 0.0])
        judge = _StubJudge({("Aurelin", "Aurelin Voss"): MergeVerdict(same=True, confidence=0.9)})

        out = judge_against_store([aurelin], [], store, judge=judge, embedder=_StubEmbedder())

        cand_ctx, exist_ctx = judge.contexts[("Aurelin", "Aurelin Voss")]
        assert "Other names in scope" in cand_ctx
        assert "Aurelin Kade" in cand_ctx
        assert "Other names in scope" in exist_ctx
        assert "Aurelin Kade" in exist_ctx
        assert out.merged_into == {aurelin.candidate_id: "id-aurelin-voss"}

    def test_reaches_the_rendered_prompt_aligned_and_alone(self) -> None:
        # Beyond _StubJudge's recorded context strings: check the FULLY
        # ASSEMBLED prompt an LLM actually sees. The competing-names line must
        # sit indented under its entity (not escape to column 0 when it's the
        # only block), and — with zero relationship edges here — only the
        # competing-names note may fire, never the Relationships note for a
        # block that isn't in the prompt.
        store = InMemoryStore()
        _seed(store, _node_with_id("Agent", "Aurelin Voss", "id-aurelin-voss", [0.0, 0.0, 0.0]))
        aurelin = _cand("Agent", "Aurelin", [0.0, 0.0, 0.0])
        provider = _ReplyProvider('{"same": true, "confidence": 0.95}')

        judge_against_store(
            [aurelin], [], store, judge=LLMMergeJudge(provider), embedder=_StubEmbedder()
        )

        assert '  Other names in scope: none found for "aurelin".' in provider.last_user
        assert "Relationships:" not in provider.last_user
        assert "SUPPORTING evidence" not in provider.last_user
        assert "CHECKED FACT" in provider.last_user

    def test_states_explicitly_when_no_competing_name(self) -> None:
        # An empty/absent block must not be silently ambiguous: absence of a
        # competitor is rendered as an explicit "none found", not nothing.
        store = InMemoryStore()
        _seed(store, _node_with_id("Agent", "Aurelin Voss", "id-aurelin-voss", [0.0, 0.0, 0.0]))
        aurelin = _cand("Agent", "Aurelin", [0.0, 0.0, 0.0])
        judge = _StubJudge({("Aurelin", "Aurelin Voss"): MergeVerdict(same=True, confidence=0.9)})

        judge_against_store([aurelin], [], store, judge=judge, embedder=_StubEmbedder())

        cand_ctx, exist_ctx = judge.contexts[("Aurelin", "Aurelin Voss")]
        assert 'Other names in scope: none found for "aurelin".' in cand_ctx
        assert 'Other names in scope: none found for "aurelin".' in exist_ctx

    def test_no_shared_leading_token_yields_no_competing_block(self) -> None:
        # "NX" / "NX Lab" share a leading token ("nx"); pick a pair that
        # genuinely doesn't, to confirm the block is skipped entirely (not
        # just rendered empty) — recalled via identical embeddings, well
        # above the default band threshold.
        store = InMemoryStore()
        _seed(store, _node_with_id("Place", "Skyler Vance", "id-skyler", [1.0, 0.0, 0.0]))
        dakota = _cand("Place", "Dakota", [1.0, 0.0, 0.0])
        judge = _StubJudge({("Dakota", "Skyler Vance"): MergeVerdict(same=True, confidence=0.9)})

        out = judge_against_store([dakota], [], store, judge=judge, embedder=_StubEmbedder())

        cand_ctx, exist_ctx = judge.contexts[("Dakota", "Skyler Vance")]
        assert "Other names in scope" not in cand_ctx
        assert "Other names in scope" not in exist_ctx
        assert cand_ctx == ""
        assert exist_ctx == ""
        assert out.merged_into == {dakota.candidate_id: "id-skyler"}

    def test_caps_competing_names_and_adds_no_extra_judge_calls(self) -> None:
        # cap+2 same-leading-token store nodes: the judged target plus
        # cap+1 competitors. LEXICAL_ALIAS_CAP (5) admits all of them as
        # judge targets (cap+2 == 5 for the current COMPETING_NAME_CAP == 3),
        # so the judge-call COUNT is driven purely by lexical recall, never by
        # how many competing names are gathered for any one pair's context.
        store = InMemoryStore()
        _seed(store, _node_with_id("Agent", "Aurelin Voss", "id-voss", [0.0, 0.0, 0.0]))
        competitor_titles = [f"Aurelin N{i:02d}" for i in range(COMPETING_NAME_CAP + 1)]
        for i, title in enumerate(competitor_titles):
            _seed(store, _node_with_id("Agent", title, f"id-comp-{i}", [0.0, 0.0, 0.0]))
        aurelin = _cand("Agent", "Aurelin", [0.0, 0.0, 0.0])
        judge = _StubJudge({("Aurelin", "Aurelin Voss"): MergeVerdict(same=True, confidence=0.9)})

        judge_against_store([aurelin], [], store, judge=judge, embedder=_StubEmbedder())

        cand_ctx, _exist_ctx = judge.contexts[("Aurelin", "Aurelin Voss")]
        kept = competitor_titles[:COMPETING_NAME_CAP]
        dropped = competitor_titles[COMPETING_NAME_CAP:]
        for title in kept:
            assert title in cand_ctx
        for title in dropped:
            assert title not in cand_ctx
        # No new LLM calls: exactly one judge call per lexically-recalled
        # store node (target + competitors), same as pre-Part-B behaviour.
        assert len(judge.calls) == COMPETING_NAME_CAP + 2


class TestVerdictSystemWording:
    """Prompt-content assertions only: these prove the Part-A instruction TEXT
    is present in the judge's system prompt. They do NOT prove a model obeys
    it — that can only be verified against a live judge (the ``acceptance_judge``
    marker), which is out of scope for this unit suite."""

    def test_states_the_mechanical_bare_token_rule(self) -> None:
        assert "STARTS WITH that exact token" in _VERDICT_SYSTEM
        assert '"Aurelin" / "Aurelin Voss"' in _VERDICT_SYSTEM
        assert "competing full name with a DIFFERENT surname" in _VERDICT_SYSTEM

    def test_restates_the_distinct_bullet_as_two_full_names(self) -> None:
        # The old ambiguous bullet ("two different people who share a first
        # name", lacking a "full names" qualifier) is gone; the new bullet
        # names two FULL names explicitly, so a model can no longer read it as
        # also covering the bare-token case above.
        assert "two different people who share a first name" not in _VERDICT_SYSTEM
        assert "two DIFFERENT FULL names" in _VERDICT_SYSTEM
        assert '"Aurelin Voss" / "Aurelin Kade"' in _VERDICT_SYSTEM

    def test_keeps_the_conservative_default(self) -> None:
        # Part A must not weaken "when in any doubt, answer false" — the fix is
        # to stop the bare-token case being doubtful, not to loosen the default.
        assert 'when in any doubt, answer "same": false' in _VERDICT_SYSTEM

    def test_explains_the_competing_names_context_block(self) -> None:
        assert "Other names in scope" in _VERDICT_SYSTEM
        assert "ambiguous -> DISTINCT" in _VERDICT_SYSTEM
        assert "none were found" in _VERDICT_SYSTEM
