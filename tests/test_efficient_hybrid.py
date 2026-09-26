"""Efficient-hybrid answer path (task-12 fix A, owner reframe) — model-free units.

CI-safe: no LLM, no embedder, no network. Covers the three invariants the
owner's gate depends on:

  1. **Query-term block selection** — under a tight excerpt budget, a block
     covering the query's discriminative (IDF) terms is selected ahead of the
     anchor-order flood (the diagnosis's pit-104 failure: 14 blocks fetched,
     none with the gold bytes). Composes with T2c dedup/round-robin: zero-
     coverage snippets keep the diversified order (stable sort).
  2. **Budget enforcement** — the assembled excerpt never exceeds the token
     knob, for any budget.
  3. **No-unbounded-read invariant** — ``_effective_source_budget`` always
     resolves to an int (policy > config > 6000 code default); the subgraph
     trace always records a bounded budget; abstention escalation is at most
     2x the base budget (``_ASK_ESCALATION_BUDGET_FACTOR``).
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_neuron import Vault
from okto_neuron.companion import (
    _ASK_ESCALATION_BUDGET_FACTOR,
    _ASK_SOURCE_BLOCK_BUDGET_DEFAULT,
    _ASK_SOURCE_BLOCK_POLICY_DEFAULT,
    AskRetrievalPolicy,
    Companion,
    _effective_source_budget,
    _estimate_context_tokens,
    _hit_context_snippet,
    _query_term_ranked_snippets,
    _source_context_for_hits,
)
from okto_neuron.llm import LLMProviderError, Message, StubLLM
from okto_neuron.models import Node as PublicNode, Provenance, QueryHit
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection

_HASH = "sha256:" + "a" * 64


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def _hit(node_id: str, path: str, start: int, end: int, score: float = 0.5) -> QueryHit:
    return QueryHit(
        node=PublicNode(id=node_id, type="Claim", name=node_id),
        score=score,
        provenance=Provenance(
            path=path,
            byte_start=start,
            byte_end=end,
            content_hash=_HASH,
            extraction_activity_id="act",
            agent_id="agent",
            document_id="doc",
            block_id=f"blk-{node_id}",
        ),
    )


# ── 1. query-term-aware block selection ───────────────────────────────────────
def _flooded_corpus(tmp_path: Path) -> list[QueryHit]:
    """Three files: two flood files full of lexical echo ('cloudbox uses a
    setting' style filler) and one gold file whose LAST block carries the
    discriminative terms. Anchor order puts the gold block dead last."""
    flood_a = tmp_path / "flood_a.md"
    flood_b = tmp_path / "flood_b.md"
    gold = tmp_path / "gold.md"
    filler = "cloudbox has a setting for the web interface theme and locale. "
    flood_a.write_text(filler * 4, encoding="utf-8")
    flood_b.write_text(filler * 4, encoding="utf-8")
    gold_text = "cloudbox stores its metadata in postgresql, the primary database. "
    gold.write_text(filler + gold_text, encoding="utf-8")
    fl = len(filler.encode())
    gl = len(gold_text.encode())
    return [
        _hit("a1", str(flood_a), 0, fl, 0.9),
        _hit("a2", str(flood_a), fl, 2 * fl, 0.8),
        _hit("b1", str(flood_b), 0, fl, 0.7),
        _hit("b2", str(flood_b), fl, 2 * fl, 0.6),
        # the gold block: lowest score, last in anchor order
        _hit("gold", str(gold), fl, fl + gl, 0.1),
    ]


def test_query_term_coverage_beats_anchor_only_ordering(tmp_path: Path) -> None:
    hits = _flooded_corpus(tmp_path)
    # Budget fits ~1.5 snippets: anchor-only order spends it on flood blocks.
    query_terms = {"cloudbox", "database", "postgresql"}
    anchor_only = _source_context_for_hits(hits, max_token_budget=30, diversify=True)
    assert "postgresql" not in anchor_only  # flood consumed the budget
    term_aware = _source_context_for_hits(
        hits, max_token_budget=30, diversify=True, query_terms=query_terms
    )
    # IDF ranks the gold block first: 'postgresql'/'database' are rare in the
    # pool, 'cloudbox' is common (discounted) — the gold bytes land in budget.
    assert "postgresql" in term_aware
    # splitlines()[0] is now the first block's EXCERPT marker; its text follows.
    assert term_aware.splitlines()[1].find("postgresql") >= 0


def test_zero_coverage_preserves_diversified_order(tmp_path: Path) -> None:
    hits = _flooded_corpus(tmp_path)
    # Terms that match nothing → all-zero scores → stable sort keeps the
    # diversified (dedup + round-robin) order byte-identical.
    no_match = _source_context_for_hits(hits, diversify=True, query_terms={"zzz", "qqq"})
    plain = _source_context_for_hits(hits, diversify=True)
    assert no_match == plain


def test_query_terms_none_is_byte_identical_regression_pin(tmp_path: Path) -> None:
    # The block-dump path never passes query_terms — None and the legacy call
    # shape must produce byte-identical output (block-arm regression pin).
    hits = _flooded_corpus(tmp_path)
    legacy = _source_context_for_hits(hits, max_token_budget=500)
    explicit_none = _source_context_for_hits(hits, max_token_budget=500, query_terms=None)
    empty_set = _source_context_for_hits(hits, max_token_budget=500, query_terms=set())
    assert legacy == explicit_none == empty_set


def test_ranking_is_deterministic(tmp_path: Path) -> None:
    hits = _flooded_corpus(tmp_path)
    terms = {"cloudbox", "postgresql"}
    first = _source_context_for_hits(hits, diversify=True, query_terms=terms)
    second = _source_context_for_hits(hits, diversify=True, query_terms=terms)
    assert first == second


def test_snippet_ranker_idf_discounts_common_topic_term() -> None:
    # Every snippet shares the topic term; only one carries the rare answer
    # term — it must rank first even though it enters last.
    snippets = [
        "cloudbox theme setting one",
        "cloudbox locale setting two",
        "cloudbox uses postgresql",
    ]
    ranked = _query_term_ranked_snippets(snippets, {"cloudbox", "postgresql"})
    assert ranked[0] == "cloudbox uses postgresql"
    # ties (the two zero-'postgresql' snippets) keep incoming order
    assert ranked[1:] == ["cloudbox theme setting one", "cloudbox locale setting two"]


# ── 2. budget enforcement ─────────────────────────────────────────────────────
@pytest.mark.parametrize("budget", [10, 30, 50, 100, 300, 1000])
def test_budget_never_exceeded(tmp_path: Path, budget: int) -> None:
    hits = _flooded_corpus(tmp_path)
    for terms in (None, {"cloudbox", "postgresql", "database"}):
        context = _source_context_for_hits(
            hits, max_token_budget=budget, diversify=True, query_terms=terms
        )
        assert _estimate_context_tokens(context + "\n") <= budget


# ── 3. no-unbounded-read invariant ────────────────────────────────────────────
def _cfg_with_budget(step_budget: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        llm=SimpleNamespace(ask=SimpleNamespace(source_block_budget_tokens=step_budget))
    )


def test_effective_source_budget_default_is_bounded_int() -> None:
    budget = _effective_source_budget(_cfg_with_budget(None), None)
    assert isinstance(budget, int)
    assert budget == _ASK_SOURCE_BLOCK_BUDGET_DEFAULT == 6000
    # the module default itself must be an int — None (unbounded) is banned
    assert isinstance(_ASK_SOURCE_BLOCK_BUDGET_DEFAULT, int)


def test_effective_source_budget_config_and_policy_resolution() -> None:
    assert _effective_source_budget(_cfg_with_budget(4000), None) == 4000
    policy = AskRetrievalPolicy(source_block_budget_tokens=2500)
    assert _effective_source_budget(_cfg_with_budget(4000), policy) == 2500


def test_escalation_factor_bounds_the_retry() -> None:
    assert _ASK_ESCALATION_BUDGET_FACTOR == 2
    assert _ASK_SOURCE_BLOCK_POLICY_DEFAULT == "blend"


def test_config_knob_accepted_by_step_llm() -> None:
    from okto_neuron.config._vault import StepLLM

    assert StepLLM().source_block_budget_tokens is None
    assert StepLLM(source_block_budget_tokens=6000).source_block_budget_tokens == 6000
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        StepLLM(source_block_budget_tokens=0)


# ── flow-level: seeded vault, queued fake provider (no real LLM) ──────────────
class _QueuedProvider:
    model = "fake"
    api_base = "http://127.0.0.1:0/v1"

    def __init__(self, answers: list[str], *, raise_at: set[int] | None = None) -> None:
        self._answers = list(answers)
        self._raise_at = raise_at or set()
        self.calls = 0
        self.user_prompts: list[str] = []

    def complete(self, messages: Sequence[Message], **_: object) -> str:
        self.calls += 1
        self.user_prompts.append(messages[-1].content if messages else "")
        if self.calls in self._raise_at:
            raise LLMProviderError("simulated provider down")
        if not self._answers:
            return ""
        return self._answers.pop(0)


def _seeded_vault(tmp_path: Path) -> Vault:
    vault = Vault.init(tmp_path / "v")
    note = Path(vault.path) / "note.md"
    note.write_text(
        "# Alpha\n\nAlice founded [[Acme]] in 2019. The budget was 5 million. #funding\n",
        encoding="utf-8",
    )
    Companion(vault, provider=StubLLM()).remember(note)
    return vault


def _enable_subgraph(vault: Vault) -> None:
    (Path(vault.path) / "okto-neuron.yaml").write_text(
        "marginalia_yaml_version: 1\nllm:\n  ask:\n    enable_subgraph: true\n",
        encoding="utf-8",
    )


def test_blend_is_default_and_trace_records_budget_path_tokens(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    _enable_subgraph(vault)
    try:
        provider = _QueuedProvider(["Alice founded Acme in 2019."])
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.hits
        assert provider.calls == 1
        trace = answer.retrieval
        # the always-blend leg: path + effective bounded budget + token estimate
        assert trace["path"] == "subgraph_blend"
        assert trace["source_block_policy"] == "blend"
        assert trace["source_block_budget_tokens"] == 6000
        assert trace["escalated"] is False
        assert trace["source_blocks_used"] is True
        assert isinstance(trace["context_tokens_estimate"], int)
        assert trace["context_tokens_estimate"] > 0
        # the single completion saw BOTH the render and the source excerpt
        assert "=== SUBGRAPH ===" in provider.user_prompts[0]
        assert "=== SOURCE BLOCKS ===" in provider.user_prompts[0]
    finally:
        vault.close()


def test_config_budget_knob_flows_into_blend(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    (Path(vault.path) / "okto-neuron.yaml").write_text(
        "marginalia_yaml_version: 1\n"
        "llm:\n  ask:\n    enable_subgraph: true\n"
        "    source_block_budget_tokens: 1234\n",
        encoding="utf-8",
    )
    try:
        provider = _QueuedProvider(["Alice founded Acme in 2019."])
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.retrieval["source_block_budget_tokens"] == 1234
    finally:
        vault.close()


def test_legacy_on_coverage_miss_is_now_bounded(tmp_path: Path) -> None:
    # The legacy opt-in keeps its two-pass shape but its Tier-2 read is bounded
    # at the escalation budget — the unbounded (None) read is gone from every
    # subgraph branch.
    vault = _seeded_vault(tmp_path)
    _enable_subgraph(vault)
    try:
        provider = _QueuedProvider(["That is not in the notes.", "The budget was 5 million."])
        answer = Companion(vault, provider=provider).ask(
            "what was the budget?",
            k=5,
            retrieval_policy=AskRetrievalPolicy(source_block_policy="on_coverage_miss"),
        )
        assert answer.hits
        assert provider.calls == 2
        trace = answer.retrieval
        assert trace["path"] == "subgraph_then_sources"
        assert trace["source_block_budget_tokens"] == 6000 * _ASK_ESCALATION_BUDGET_FACTOR
        assert trace["source_block_budget_tokens"] is not None
    finally:
        vault.close()


def test_provider_error_on_blend_is_not_escalated(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    _enable_subgraph(vault)
    try:
        provider = _QueuedProvider(["unused"], raise_at={1})
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert provider.calls == 1  # provider down ≠ abstention — no retry
        assert answer.text == ""
        assert answer.retrieval["path"] == "subgraph_provider_error"
    finally:
        vault.close()


# ── Fix 4 (issue #4 — honest ask trace): rotted anchor ≠ no anchor ───────────


def test_hit_context_snippet_no_fallback_when_anchor_rots(tmp_path: Path) -> None:
    """``_hit_context_snippet`` distinguishes 'anchor present but the read
    failed' from 'no anchor at all'. A rotted anchor (source file moved/
    deleted after ingest) must NOT fall back to the node's name — that
    silent substitution is exactly what let ``source_blocks_used`` claim
    block grounding that never happened."""
    doc = tmp_path / "doc.md"
    doc.write_text("real paragraph text", encoding="utf-8")
    hit = _hit("n1", str(doc), 0, len(b"real paragraph text"))
    hit = hit.model_copy(
        update={"node": hit.node.model_copy(update={"name": "Node Title Should Not Leak"})}
    )

    # While the file exists, the anchor reads real source bytes.
    snippet, from_source = _hit_context_snippet(hit)
    assert from_source is True
    assert snippet == "real paragraph text"

    doc.unlink()  # rot the anchor
    snippet, from_source = _hit_context_snippet(hit)
    assert from_source is False
    assert snippet == ""
    assert "Node Title Should Not Leak" not in snippet

    # A hit with NO anchor at all (empty provenance path — how an anchor-less
    # hit is represented; ``Provenance.path`` is required, non-optional)
    # keeps the legacy name-fallback, unaffected by this fix: a nameless hit
    # never claimed block grounding either way.
    no_anchor_hit = QueryHit(
        node=PublicNode(id="n2", type="Claim", name="Nameless Node"),
        score=0.5,
        provenance=Provenance(
            path="",
            byte_start=0,
            byte_end=0,
            content_hash=_HASH,
            extraction_activity_id="act",
            agent_id="agent",
            document_id="doc",
            block_id="blk-n2",
        ),
    )
    snippet, from_source = _hit_context_snippet(no_anchor_hit)
    assert from_source is False
    assert snippet == "Nameless Node"


def test_ask_source_blocks_used_is_false_when_source_file_deleted(tmp_path: Path) -> None:
    """End-to-end: ingest a note, delete its source file, ask. Every hit's
    byte-anchor now rots (the block-dump default path — no subgraph). The
    trace must NOT claim ``source_blocks_used=True`` by silently falling
    back to node names for the completion context — that silent claim is
    the ask-side half of the has_heading-only bug (issue #4)."""
    vault = _seeded_vault(tmp_path)
    try:
        (Path(vault.path) / "note.md").unlink()

        provider = _QueuedProvider(["stub answer"])
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)

        assert answer.hits  # the graph itself is unaffected — only the byte read fails
        trace = answer.retrieval
        assert trace["source_blocks_used"] is False
        assert trace["path"] == "block_without_sources"
    finally:
        vault.close()


def test_ask_source_blocks_used_is_false_when_budget_too_tiny_for_any_snippet(
    tmp_path: Path,
) -> None:
    """Defect D: ``_source_context_for_hits`` used to count REAL source reads
    over ALL snippet pairs BEFORE the budget trim — a budget too tiny for even
    one line trims the assembled context down to "" while the pre-trim count
    stayed > 0, so callers reported ``source_blocks_used=True`` for a context
    that was never actually sent to the LLM. The source file is present here
    (unlike the deleted-file case above) — the trim, not a rotted anchor, is
    what must zero out the count."""
    vault = _seeded_vault(tmp_path)
    try:
        provider = _QueuedProvider(["stub answer"])
        answer = Companion(vault, provider=provider).ask(
            "who founded acme?",
            k=5,
            retrieval_policy=AskRetrievalPolicy(source_block_budget_tokens=1),
        )

        assert answer.hits  # real hits with real, present source bytes
        trace = answer.retrieval
        assert trace["path"] == "block_without_sources"
        assert trace["source_blocks_used"] is False
    finally:
        vault.close()
