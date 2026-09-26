"""ADR 0010 P4 (M4) — the optional cluster-judge path must forward the judge's
configured ``enable_thinking`` flag to the provider call (no LLM).

Background: ``_try_cluster_judge`` previously passed only ``temperature`` /
``max_tokens`` to ``provider.complete``, so the cluster path ran at provider-default
thinking. On the configured backend that burns the whole token budget on reasoning
and returns EMPTY content (``finish_reason: length``, 0 content tokens) —
re-triggering the non-committal failure the ADR 0008 fix already corrected for the
pairwise path. This test pins that the flag now reaches the provider.

No real LLM: a stub provider captures the kwargs ``_try_cluster_judge`` passes and
returns a valid (empty-survivor) cluster verdict so the path stays clean. The same
prompt seam also pins bounded candidate descriptions.
"""

from __future__ import annotations

from typing import Any

from okto_neuron.core.schema import Node
from okto_neuron.reconcile.propose import _build_cluster_prompt, _try_cluster_judge
from okto_neuron.resolve import MergeVerdict, NodeCandidate


class _CaptureProvider:
    """Records the kwargs of the single ``complete`` call. Accepts arbitrary
    kwargs (the production call passes temperature/max_tokens/enable_thinking) and
    returns a valid empty-survivor cluster verdict."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    def complete(self, _messages, **kwargs: Any) -> str:
        self.kwargs = kwargs
        return '{"same_indices": [], "confidence": 0.0, "reason": "stub"}'


class _StubJudge:
    """Exposes the LLM-judge seam ``_try_cluster_judge`` reads: a non-None
    ``_provider`` plus the ``_temperature`` / ``_max_tokens`` / ``_enable_thinking``
    getattrs that mirror ``LLMMergeJudge``'s constructor fields."""

    def __init__(self, provider: _CaptureProvider, *, enable_thinking: bool) -> None:
        self._provider = provider
        self._temperature = 0.2
        self._max_tokens = 2000
        self._enable_thinking = enable_thinking
        self._system_prompt = None

    def judge(
        self,
        candidate: NodeCandidate,
        existing: Node,
        *,
        candidate_context: str = "",
        existing_context: str = "",
    ) -> MergeVerdict:
        # ``_try_cluster_judge`` uses only the ``_provider`` seam, never this
        # pairwise method; present solely to satisfy the ``MergeJudge`` protocol.
        return MergeVerdict(same=False, confidence=0.0, reason="unused")


def _node(node_id: str, title: str, content: str = "") -> Node:
    return Node(id=node_id, type="Agent", title=title, content=content)


def test_cluster_judge_forwards_enable_thinking_false() -> None:
    provider = _CaptureProvider()
    judge = _StubJudge(provider, enable_thinking=False)

    result = _try_cluster_judge(_node("c0", "Luke Gray"), [_node("c1", "Luke Gray Lab")], judge)

    # Path ran (valid parse → empty survivor list, not a None fallback).
    assert result == ([], "stub")
    # M4: the configured thinking flag reached the provider call.
    assert provider.kwargs.get("enable_thinking") is False
    response_format = provider.kwargs["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["name"] == "marginalia_cluster_verdict"
    # The pre-existing forwards still hold (regression guard).
    assert provider.kwargs.get("temperature") == 0.2
    assert provider.kwargs.get("max_tokens") == 2000


def test_cluster_judge_forwards_enable_thinking_true() -> None:
    """A judge configured with thinking ON forwards True (not coerced/dropped)."""
    provider = _CaptureProvider()
    judge = _StubJudge(provider, enable_thinking=True)

    _try_cluster_judge(_node("c0", "A"), [_node("c1", "B")], judge)

    assert provider.kwargs.get("enable_thinking") is True


def test_cluster_prompt_includes_bounded_candidate_descriptions() -> None:
    long_description = "x" * 400 + "NOT_IN_PROMPT"

    prompt = _build_cluster_prompt(
        _node("c0", "Canonical"),
        [
            _node("c1", "Described candidate", "The candidate works on the Atlas program."),
            _node("c2", "Long candidate", long_description),
        ],
    )

    assert "  0: Described candidate" in prompt
    assert "    description: The candidate works on the Atlas program." in prompt
    assert "  1: Long candidate" in prompt
    assert f"    description: {'x' * 400}" in prompt
    assert "NOT_IN_PROMPT" not in prompt
