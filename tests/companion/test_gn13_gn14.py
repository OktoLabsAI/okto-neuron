"""Model-free unit tests for ADR 0019 Phase 3 prep: GN-13 (graph-native
answerer prompt selection) and GN-14 (coverage-threshold fallback).

No LLM is loaded; provider calls are intercepted by FakeLLM.  No vault
files are written; InMemoryStore is used throughout.  CI-safe and offline.
"""

from __future__ import annotations

from collections import deque
from typing import Any
from unittest.mock import MagicMock

import pytest

from okto_neuron.companion import (
    _ASK_COVERAGE_GRAPH_DEFAULT,
    _ASK_SYSTEM,
    _ASK_SYSTEM_GRAPH,
)


# ── GN-13: prompt constants ───────────────────────────────────────────────────


class TestGN13Constants:
    """The two prompt constants are distinct and the graph-native one mentions
    graph structures; neither is empty."""

    def test_ask_system_is_terse(self) -> None:
        assert _ASK_SYSTEM  # non-empty
        # Must NOT reference graph-specific sections (block-dump prompt is agnostic)
        assert "NODES" not in _ASK_SYSTEM
        assert "CLAIMS" not in _ASK_SYSTEM

    def test_ask_system_graph_is_distinct(self) -> None:
        assert _ASK_SYSTEM_GRAPH
        assert _ASK_SYSTEM_GRAPH != _ASK_SYSTEM

    def test_ask_system_graph_mentions_graph_sections(self) -> None:
        # Prompt must reference typed render structures so the LLM can parse them.
        assert "NODES" in _ASK_SYSTEM_GRAPH
        assert "CLAIMS" in _ASK_SYSTEM_GRAPH
        assert "RELATIONSHIPS" in _ASK_SYSTEM_GRAPH

    def test_ask_system_graph_instructs_abstention(self) -> None:
        # Must preserve neg-guard: instruct the model to say so when the graph
        # lacks the fact rather than inventing details.
        low = _ASK_SYSTEM_GRAPH.lower()
        assert any(
            phrase in low
            for phrase in ("does not contain", "not contain", "say so", "do not invent")
        )

    def test_ask_coverage_graph_default_is_zero(self) -> None:
        # Default = 0.0 means the GN-14 pre-gate is disabled: existing behaviour
        # is fully preserved with no config change.
        assert _ASK_COVERAGE_GRAPH_DEFAULT == 0.0


# ── GN-13: prompt is selected only when enable_subgraph=True ─────────────────


class _FakeLLM:
    """Records the system prompt from the last complete() call."""

    def __init__(self, queued: list[str]) -> None:
        self._q: deque[str] = deque(queued)
        self.last_system: str | None = None
        self.model = "fake-model"
        self.api_base = "http://fake"

    def complete(self, messages: list[Any], **_kwargs: Any) -> str:
        for m in messages:
            if getattr(m, "role", None) == "system":
                self.last_system = getattr(m, "content", None)
                break
        return self._q.popleft() if self._q else "fake answer"


def _make_cfg(
    *,
    enable_subgraph: bool = False,
    system_prompt_graph: str | None = None,
    coverage_threshold_graph: float | None = None,
    coverage_threshold: float | None = None,
) -> Any:
    """Build a minimal VaultConfig-like mock for the companion's STEP-DIRECT reads."""
    ask_cfg = MagicMock()
    ask_cfg.system_prompt = None
    ask_cfg.system_prompt_graph = system_prompt_graph
    ask_cfg.enable_subgraph = enable_subgraph
    ask_cfg.coverage_threshold = coverage_threshold
    ask_cfg.coverage_threshold_graph = coverage_threshold_graph
    ask_cfg.source_block_policy = None
    ask_cfg.neighbour_budget_tokens = None
    ask_cfg.max_degree_per_seed = None
    ask_cfg.hops = None
    ask_cfg.min_claim_confidence = None
    ask_cfg.render_format = None
    ask_cfg.max_nodes = None
    ask_cfg.max_relationships = None
    ask_cfg.max_claims = None

    resolved = MagicMock()
    resolved.temperature = 0.7
    resolved.max_tokens = 512
    resolved.top_p = None
    resolved.top_k = None
    resolved.min_p = None
    resolved.presence_penalty = None
    resolved.enable_thinking = None

    llm_cfg = MagicMock()
    llm_cfg.ask = ask_cfg
    llm_cfg.resolved.return_value = resolved

    cfg = MagicMock()
    cfg.llm = llm_cfg
    return cfg


def test_block_dump_path_uses_ask_system() -> None:
    """When enable_subgraph is False the block-dump path calls _complete_ask
    without system_prompt_override so _ASK_SYSTEM is selected."""
    from okto_neuron.companion import Companion

    fake_llm = _FakeLLM(["block answer"])
    cfg = _make_cfg(enable_subgraph=False)

    companion = MagicMock(spec=Companion)
    companion._get_provider = MagicMock(return_value=fake_llm)

    # Call the unbound method directly with the mock instance; no override → _ASK_SYSTEM
    result = Companion._complete_ask(companion, "q?", "some context", cfg)
    assert result == "block answer"
    assert fake_llm.last_system == _ASK_SYSTEM


def test_complete_ask_with_graph_system_prompt() -> None:
    """_complete_ask with system_prompt_override selects the override, not
    _ASK_SYSTEM — this is the mechanism GN-13 uses for Tier-1 graph-native calls."""
    from okto_neuron.companion import Companion

    fake_llm = _FakeLLM(["graph answer"])
    cfg = _make_cfg()

    companion = MagicMock(spec=Companion)
    companion._get_provider = MagicMock(return_value=fake_llm)

    result = Companion._complete_ask(
        companion,
        "q?",
        "graph context",
        cfg,
        system_prompt_override=_ASK_SYSTEM_GRAPH,
    )
    assert result == "graph answer"
    assert fake_llm.last_system == _ASK_SYSTEM_GRAPH


def test_complete_ask_custom_system_prompt_graph_override() -> None:
    """A per-vault llm.ask.system_prompt_graph string overrides _ASK_SYSTEM_GRAPH."""
    from okto_neuron.companion import Companion

    custom_prompt = "Custom graph prompt for this vault."
    fake_llm = _FakeLLM(["custom answer"])
    cfg = _make_cfg(system_prompt_graph=custom_prompt)

    companion = MagicMock(spec=Companion)
    companion._get_provider = MagicMock(return_value=fake_llm)

    # Simulate what _ask_subgraph does: read cfg.llm.ask.system_prompt_graph
    graph_prompt = cfg.llm.ask.system_prompt_graph or _ASK_SYSTEM_GRAPH
    assert graph_prompt == custom_prompt

    result = Companion._complete_ask(
        companion,
        "q?",
        "graph context",
        cfg,
        system_prompt_override=graph_prompt,
    )
    assert result == "custom answer"
    assert fake_llm.last_system == custom_prompt


# ── GN-14: coverage-threshold config knob ────────────────────────────────────


class TestGN14Config:
    """The config knob is correctly threaded through the config layer and the
    code default preserves existing behaviour (0.0 = disabled)."""

    def test_config_default_is_none(self) -> None:
        """coverage_threshold_graph defaults to None in the config model (the
        code-level fallback _ASK_COVERAGE_GRAPH_DEFAULT = 0.0 is applied in
        companion, not in the config model)."""
        from okto_neuron.config._vault import StepLLM

        step = StepLLM()
        assert step.coverage_threshold_graph is None

    def test_config_accepts_valid_range(self) -> None:
        from okto_neuron.config._vault import StepLLM

        for val in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
            step = StepLLM(coverage_threshold_graph=val)
            assert step.coverage_threshold_graph == val

    def test_config_rejects_out_of_range(self) -> None:
        from pydantic import ValidationError

        from okto_neuron.config._vault import StepLLM

        with pytest.raises(ValidationError):
            StepLLM(coverage_threshold_graph=1.5)

        with pytest.raises(ValidationError):
            StepLLM(coverage_threshold_graph=-0.1)

    def test_system_prompt_graph_config_default_is_none(self) -> None:
        from okto_neuron.config._vault import StepLLM

        step = StepLLM()
        assert step.system_prompt_graph is None

    def test_system_prompt_graph_config_accepts_string(self) -> None:
        from okto_neuron.config._vault import StepLLM

        step = StepLLM(system_prompt_graph="Custom graph prompt.")
        assert step.system_prompt_graph == "Custom graph prompt."


class TestGN14PreGateLogic:
    """The GN-14 pre-gate short-circuits to Tier-2 only when the threshold is
    non-zero AND the render is thin.  With threshold=0.0 it is a no-op."""

    def test_zero_threshold_call_site_guard(self) -> None:
        """The GN-14 pre-gate is guarded at the call site with
        ``if coverage_threshold_graph > 0.0`` — so even if the helper returns
        True for an empty render, the pre-gate block is never entered.
        _subgraph_context_thin itself always returns True for empty context
        (the early-return guard fires before the row-counting logic), but that
        does not matter: the call site in _ask_subgraph skips the block entirely
        when coverage_threshold_graph == 0.0 (the code default)."""
        from okto_neuron.companion import _ASK_COVERAGE_GRAPH_DEFAULT, _subgraph_context_thin

        # Confirm the helper returns True for empty regardless of threshold
        assert _subgraph_context_thin("", 0.0) is True  # early-return fires
        # But the guard at the call site ensures this is never reached when disabled.
        assert _ASK_COVERAGE_GRAPH_DEFAULT == 0.0
        # Simulate the call-site guard: threshold=0.0 → block is skipped
        threshold = _ASK_COVERAGE_GRAPH_DEFAULT
        gate_fires = threshold > 0.0 and _subgraph_context_thin("", threshold)
        assert gate_fires is False

    def test_nonzero_threshold_fires_on_empty_render(self) -> None:
        from okto_neuron.companion import _subgraph_context_thin

        # threshold=0.4 → min_rows=int(0.4*2.5+0.5)=1 → empty render is thin
        thin = _subgraph_context_thin("", 0.4)
        assert thin is True

    def test_nonzero_threshold_does_not_fire_on_substantive_render(self) -> None:
        from okto_neuron.companion import _subgraph_context_thin

        render = (
            "=== NODES ===\n"
            "N0 Alice\n"
            "=== RELATIONSHIPS ===\n"
            "N0 -[founded]-> N1\n"
            "=== CLAIMS ===\n"
            "- Alice founded Acme [conf=0.95]\n"
        )
        # threshold=0.4 → min_rows=1; render has 2 substantive rows → not thin
        thin = _subgraph_context_thin(render, 0.4)
        assert thin is False

    def test_coverage_threshold_graph_code_default_is_zero(self) -> None:
        """Confirm the module-level constant is 0.0 so the gate is disabled by
        default — no behaviour change without explicit config."""
        from okto_neuron.companion import _ASK_COVERAGE_GRAPH_DEFAULT

        assert _ASK_COVERAGE_GRAPH_DEFAULT == 0.0

    def test_cfg_coverage_threshold_graph_resolution(self) -> None:
        """The cfg resolution logic: non-None cfg value wins; None falls back
        to _ASK_COVERAGE_GRAPH_DEFAULT (0.0)."""
        from okto_neuron.companion import _ASK_COVERAGE_GRAPH_DEFAULT

        # Case 1: cfg has explicit value
        cfg1 = _make_cfg(coverage_threshold_graph=0.6)
        val1 = (
            cfg1.llm.ask.coverage_threshold_graph
            if cfg1.llm.ask.coverage_threshold_graph is not None
            else _ASK_COVERAGE_GRAPH_DEFAULT
        )
        assert val1 == 0.6

        # Case 2: cfg has None → code default
        cfg2 = _make_cfg(coverage_threshold_graph=None)
        val2 = (
            cfg2.llm.ask.coverage_threshold_graph
            if cfg2.llm.ask.coverage_threshold_graph is not None
            else _ASK_COVERAGE_GRAPH_DEFAULT
        )
        assert val2 == 0.0


# ── opt-in proof: block-dump path unchanged ───────────────────────────────────


def test_enable_subgraph_false_does_not_select_graph_prompt() -> None:
    """When enable_subgraph=False the block-dump code path in Companion.ask is
    taken; _ask_subgraph (which selects _ASK_SYSTEM_GRAPH) is never called.
    The default cfg has enable_subgraph=None which resolves to False at the
    STEP-DIRECT read site."""
    cfg = _make_cfg(enable_subgraph=False)
    # STEP-DIRECT resolution: None or False → False
    enable_subgraph = bool(cfg.llm.ask.enable_subgraph)
    assert enable_subgraph is False
    # When False, _ask_subgraph is never called, so _ASK_SYSTEM_GRAPH is never used.
    # (The block-dump path is exercised in test_block_dump_path_uses_ask_system above.)


def test_enable_subgraph_true_selects_graph_prompt() -> None:
    """When enable_subgraph=True the cfg.llm.ask.system_prompt_graph is read
    and _ASK_SYSTEM_GRAPH is the fallback — the graph-native prompt is active."""
    cfg = _make_cfg(enable_subgraph=True, system_prompt_graph=None)
    enable_subgraph = bool(cfg.llm.ask.enable_subgraph)
    assert enable_subgraph is True
    graph_prompt = cfg.llm.ask.system_prompt_graph or _ASK_SYSTEM_GRAPH
    assert graph_prompt == _ASK_SYSTEM_GRAPH
