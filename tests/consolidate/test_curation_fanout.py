"""ADR 0015 D1 — bounded-concurrency curation fan-out."""

from __future__ import annotations

import threading
import time
from typing import Sequence

from okto_neuron.companion import _fan_out_verdicts, _iter_fan_out_verdicts
from okto_neuron.consolidate._candidates import NodeCandidate
from okto_neuron.curator import CuratorVerdict, LLMCandidateCurator
from okto_neuron.llm import Message
from okto_neuron.resolve import ResolveOutcome
from okto_neuron.store.memory import InMemoryStore


def _verdict(reason: str) -> CuratorVerdict:
    return CuratorVerdict(action="commit", confidence=0.9, reason=reason)


def test_fan_out_preserves_input_order() -> None:
    def make_call(index: int, sleep_s: float):
        def call() -> CuratorVerdict:
            time.sleep(sleep_s)
            return _verdict(f"v{index}")

        return call

    # First item is the slowest; order must still match input order.
    calls = [make_call(0, 0.2), make_call(1, 0.05), make_call(2, 0.0), make_call(3, 0.01)]
    verdicts = _fan_out_verdicts(calls, max_concurrent=4, timeout_s=10.0)
    assert [v.reason for v in verdicts] == ["v0", "v1", "v2", "v3"]


def test_timeout_degrades_to_abstain() -> None:
    def slow() -> CuratorVerdict:
        time.sleep(1.0)
        return _verdict("too-late")

    def fast() -> CuratorVerdict:
        return _verdict("ok")

    verdicts = _fan_out_verdicts([slow, fast], max_concurrent=2, timeout_s=0.1)
    assert verdicts[0].action == "abstain"
    assert verdicts[0].reason == "curation-timeout"
    assert verdicts[1].reason == "ok"


def test_exception_degrades_to_abstain() -> None:
    def boom() -> CuratorVerdict:
        raise RuntimeError("provider exploded")

    def ok() -> CuratorVerdict:
        return _verdict("ok")

    verdicts = _fan_out_verdicts([boom, ok], max_concurrent=2, timeout_s=5.0)
    assert verdicts[0].action == "abstain"
    assert verdicts[0].reason.startswith("curation-error")
    assert "provider exploded" in verdicts[0].reason
    assert verdicts[1].reason == "ok"


def test_llm_unavailable_verdict_is_not_retried_by_the_fan_out() -> None:
    """The curator call applies the ADR 0039 D5 retry itself
    (``complete_with_retry``), so an ``llm-unavailable`` verdict has ALREADY
    had its retry. A second, blind retry here doubled the attempts and also
    retried non-retryable errors (authentication, invalid request)."""
    attempts = {"n": 0}

    def always_down() -> CuratorVerdict:
        attempts["n"] += 1
        return CuratorVerdict(action="abstain", confidence=0.0, reason="llm-unavailable")

    for max_concurrent in (1, 4):
        attempts["n"] = 0
        verdicts = _fan_out_verdicts([always_down], max_concurrent=max_concurrent, timeout_s=5.0)
        assert attempts["n"] == 1
        assert verdicts[0].reason == "llm-unavailable"


def test_sequential_path_runs_in_order_without_threads() -> None:
    main_thread = threading.current_thread()
    seen: list[int] = []

    def make_call(index: int):
        def call() -> CuratorVerdict:
            assert threading.current_thread() is main_thread
            seen.append(index)
            return _verdict(f"v{index}")

        return call

    verdicts = _fan_out_verdicts([make_call(i) for i in range(3)], max_concurrent=1, timeout_s=5.0)
    assert seen == [0, 1, 2]
    assert [v.reason for v in verdicts] == ["v0", "v1", "v2"]


# ── ADR 0015 D5a — streamed ordered-prefix drain ─────────────────────────────


def test_streamed_drain_yields_prefix_before_slowest_completes() -> None:
    """Verdict 0 is consumable (ledger-writable) while a later call still runs."""
    release = threading.Event()

    def fast() -> CuratorVerdict:
        return _verdict("v0")

    def slow() -> CuratorVerdict:
        assert release.wait(5.0)
        return _verdict("v1")

    gen = _iter_fan_out_verdicts([fast, slow], max_concurrent=2, timeout_s=10.0)
    first = next(gen)
    # The slow call is still blocked on the event — yet item 0 already arrived.
    assert first.reason == "v0"
    assert not release.is_set()
    release.set()
    assert next(gen).reason == "v1"
    assert list(gen) == []


def test_streamed_drain_keeps_input_order_with_staggered_completions() -> None:
    release = threading.Event()

    def make_call(index: int):
        def call() -> CuratorVerdict:
            if index == 3:
                assert release.wait(5.0)
            else:
                time.sleep(0.01 * (3 - index))  # staggered, out-of-order finish
            return _verdict(f"v{index}")

        return call

    gen = _iter_fan_out_verdicts([make_call(i) for i in range(4)], max_concurrent=4, timeout_s=10.0)
    consumed = [next(gen).reason for _ in range(3)]
    # Items 0..2 were consumed in input order before the slowest item finished.
    assert consumed == ["v0", "v1", "v2"]
    assert not release.is_set()
    release.set()
    assert next(gen).reason == "v3"


def test_sequential_streaming_interleaves_call_and_consume() -> None:
    """max_concurrent=1 degenerates to call → write → call → write (pre-D1)."""
    events: list[str] = []

    def make_call(index: int):
        def call() -> CuratorVerdict:
            events.append(f"call{index}")
            return _verdict(f"v{index}")

        return call

    for verdict in _iter_fan_out_verdicts(
        [make_call(i) for i in range(3)], max_concurrent=1, timeout_s=5.0
    ):
        events.append(f"write:{verdict.reason}")
    assert events == [
        "call0",
        "write:v0",
        "call1",
        "write:v1",
        "call2",
        "write:v2",
    ]


class _FakeProvider:
    """Returns a fixed curator verdict; mirrors test_ledger_telemetry pattern."""

    model = "fake"

    def __init__(self, reply: str = '{"action": "commit", "confidence": 0.9, "reason": "ok"}'):
        self._reply = reply
        self.prompts: list[str] = []

    def complete(self, messages: Sequence[Message], **kwargs: object) -> str:
        self.prompts.append(messages[-1].content)
        return self._reply


def test_prebuilt_prompt_yields_equivalent_verdict() -> None:
    provider = _FakeProvider()
    curator = LLMCandidateCurator(provider)
    store = InMemoryStore()
    candidate = NodeCandidate(type="Agent", title="Alex", content="a participant")
    outcome = ResolveOutcome(correlations=(), confidence=0.5)

    prompt = curator.build_prompt(candidate, outcome, store=store, edges=[])
    with_prebuilt = curator.curate(candidate, outcome, store=store, edges=[], user_prompt=prompt)
    without = curator.curate(candidate, outcome, store=store, edges=[])

    # The LLM saw the identical user prompt both times.
    assert provider.prompts[0] == provider.prompts[1] == prompt
    # Verdicts match on every field except wall-clock duration.
    assert (with_prebuilt.action, with_prebuilt.confidence, with_prebuilt.reason) == (
        without.action,
        without.confidence,
        without.reason,
    )
    assert with_prebuilt.canonical_predicate == without.canonical_predicate
    assert with_prebuilt.usage == without.usage
