"""Empty-result retry: a clean ``stop`` with 0 candidates on non-empty input
gets ONE retry at temperature=0.0 before being accepted as empty (see
``LLMExtractor._extract_baseline`` in ``okto_neuron.extract``)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from okto_neuron.extract import LLMExtractor
from okto_neuron.llm import Message, _set_last_call_stats


@pytest.fixture(autouse=True)
def _reset_call_stats():
    """``_set_last_call_stats`` writes thread-local state that outlives a
    single test; reset it so a ``finish_reason`` stub set here never leaks
    into an unrelated test run later in the same process."""
    yield
    _set_last_call_stats(None)


_GOOD = (
    '{"nodes":['
    '{"type":"Agent","title":"Evan","content":"CDTO at ExampleCorp"},'
    '{"type":"Concept","title":"ROI","content":"return on investment"}'
    '],"edges":[{"type":"discusses","src":"Evan","dst":"ROI"}]}'
)
_EMPTY = '{"nodes":[],"edges":[]}'


class _SequencedLLM:
    """Fake provider returning one reply per call, in order. Records
    temperatures and call count so tests can assert retry behavior."""

    model = "fake"

    def __init__(
        self, replies: Sequence[str], finish_reasons: Sequence[str | None] | None = None
    ) -> None:
        self._replies = list(replies)
        self._finish_reasons = list(finish_reasons) if finish_reasons else ["stop"] * len(replies)
        self.calls: list[dict[str, object]] = []

    def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        **kwargs,
    ) -> str:
        idx = len(self.calls)
        self.calls.append({"temperature": temperature, "messages": messages, **kwargs})
        _set_last_call_stats({"finish_reason": self._finish_reasons[idx]})
        return self._replies[idx]

    @property
    def call_count(self) -> int:
        return len(self.calls)


def test_empty_first_call_retries_and_uses_populated_second() -> None:
    provider = _SequencedLLM([_EMPTY, _GOOD])
    extractor = LLMExtractor(provider, temperature=0.6)

    result = extractor.extract("Evan discussed ROI.")

    assert provider.call_count == 2
    assert provider.calls[0]["temperature"] == 0.6
    assert provider.calls[1]["temperature"] == 0.0
    assert {n.title for n in result.node_candidates} == {"Evan", "ROI"}
    assert result.empty_after_retry is False


def test_non_empty_first_call_does_not_retry() -> None:
    provider = _SequencedLLM([_GOOD])
    extractor = LLMExtractor(provider, temperature=0.6)

    result = extractor.extract("Evan discussed ROI.")

    assert provider.call_count == 1
    assert {n.title for n in result.node_candidates} == {"Evan", "ROI"}
    assert result.empty_after_retry is False


def test_empty_both_times_accepted_as_empty_and_flagged() -> None:
    provider = _SequencedLLM([_EMPTY, _EMPTY])
    extractor = LLMExtractor(provider, temperature=0.6)

    result = extractor.extract("Evan discussed ROI.")

    assert provider.call_count == 2
    assert result.node_candidates == []
    assert result.edge_candidates == []
    assert result.empty_after_retry is True


def test_empty_input_text_never_calls_provider() -> None:
    provider = _SequencedLLM([_GOOD])
    extractor = LLMExtractor(provider, temperature=0.6)

    result = extractor.extract("   \n  ")

    assert provider.call_count == 0
    assert result.node_candidates == []
    assert result.empty_after_retry is False


def test_auto_mode_also_gets_empty_retry() -> None:
    """The default effective mode ("auto") must inherit the retry via
    ``_extract_baseline``, which it calls first before any truncation
    escalation decision."""
    provider = _SequencedLLM([_EMPTY, _GOOD])
    extractor = LLMExtractor(provider, temperature=0.6, mode="auto")

    result = extractor.extract("Evan discussed ROI.")

    assert provider.call_count == 2
    assert {n.title for n in result.node_candidates} == {"Evan", "ROI"}


def test_retry_that_truncates_is_not_accepted_as_clean_empty() -> None:
    """If the retry itself hits the output cap, the ``truncated`` flag must
    still surface (so "auto" mode's existing escalation path can catch it) —
    the empty-retry logic must not mask truncation as ``empty_after_retry``."""
    provider = _SequencedLLM([_EMPTY, ""], finish_reasons=["stop", "length"])
    extractor = LLMExtractor(provider, temperature=0.6)

    result = extractor.extract("Evan discussed ROI.")

    assert provider.call_count == 2
    assert result.truncated is True
    assert result.empty_after_retry is False
