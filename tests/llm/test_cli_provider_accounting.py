"""CLI pseudo-providers: canonical usage line, finish reasons, sampler accounting.

Three behaviours that used to be silent are asserted here:

1. Every CLI provider emits the SAME ``litellm usage ...`` line the benchmark
   harness parses. The regex is replicated verbatim from
   ``benchmarks/locomo/usage.py`` (which must not be imported from ``tests/``,
   and must not be edited) so a drift on either side fails this test rather
   than silently reporting a run at zero tokens.
2. A finish reason is surfaced where the CLI actually reports one, and is
   ABSENT — not fabricated as ``"stop"`` — where it does not.
3. A sampling parameter the CLI cannot carry is reported as omitted through the
   same observer seam ``LiteLLMProvider`` uses, instead of being dropped in
   silence.
"""

from __future__ import annotations

import json
import re

import pytest

from okto_neuron.config._vault import ResolvedLLM
from okto_neuron.llm import Message, _set_request_observer, get_provider, last_call_stats

# Replicated from benchmarks/locomo/usage.py:28-32 — byte for byte. The whole
# point of the canonical line is that THIS pattern matches it, so copying the
# pattern is the assertion, not a convenience.
_HARNESS_USAGE_RE = re.compile(
    r"litellm usage model=(?P<model>\S+) prompt_tokens=(?P<prompt_tokens>\S+) "
    r"completion_tokens=(?P<completion_tokens>\S+) cached_tokens=(?P<cached_tokens>\S+)"
    r"(?: step=(?P<step>\S+))?"
)

MESSAGES = [
    Message(role="system", content="You are terse."),
    Message(role="user", content="Say OK."),
]


def _resolved(provider: str, model: str = "m-1", **overrides) -> ResolvedLLM:
    base = dict(
        provider=provider,
        api_base="http://127.0.0.1:8123/v1",  # required field; CLI providers ignore it
        model=model,
        api_key_env=None,
        max_tokens=1024,
        temperature=0.7,
        top_p=0.95,
        top_k=20,
        min_p=0.0,
        presence_penalty=0.0,
        enable_thinking=False,
    )
    base.update(overrides)
    return ResolvedLLM(**base)


def _install_fake_cli(monkeypatch, mod, stdout: str, *, output_content: str | None = None):
    """Patch one CLI provider module's ``shutil.which`` and ``subprocess.Popen``."""

    calls: list[dict] = []
    monkeypatch.setattr(mod.shutil, "which", lambda name: f"/usr/local/bin/{name}")

    def fake_popen(cmd, **kwargs):
        entry = {"cmd": cmd, **kwargs}
        calls.append(entry)
        if output_content is not None and "-o" in cmd:
            from pathlib import Path

            Path(cmd[cmd.index("-o") + 1]).write_text(output_content)

        class _FakeProc:
            pid = 999_999_999
            returncode = 0

            def communicate(self, input=None, timeout=None):
                entry["input"] = input
                return stdout, ""

            def wait(self):
                return None

        return _FakeProc()

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    return calls


def _codex_stdout(usage: dict | None = None) -> str:
    events = [
        {"type": "thread.started", "thread_id": "t"},
        {"type": "item.completed", "item": {"id": "i", "type": "agent_message", "text": "OK"}},
        {
            "type": "turn.completed",
            "usage": usage
            if usage is not None
            else {
                "input_tokens": 100,
                "cached_input_tokens": 10,
                "output_tokens": 50,
                "reasoning_output_tokens": 5,
            },
        },
    ]
    return "\n".join(json.dumps(e) for e in events)


def _claude_stdout(**result_overrides) -> str:
    result = {
        "type": "result",
        "is_error": False,
        "result": "OK",
        "stop_reason": "end_turn",
        "terminal_reason": "completed",
        "total_cost_usd": 0.0024,
        "usage": {
            "input_tokens": 220,
            "output_tokens": 80,
            "cache_read_input_tokens": 30,
        },
    }
    result.update(result_overrides)
    return json.dumps([result])


def _pi_stdout(**msg_overrides) -> str:
    message = {
        "stopReason": "stop",
        "content": [{"type": "text", "text": "OK"}],
        "usage": {"input": 11, "output": 3, "cacheRead": 0},
    }
    message.update(msg_overrides)
    return json.dumps({"type": "agent_end", "messages": [message]})


def _run(monkeypatch, driver, stdout, *, output_content=None, **resolved_kwargs):
    from okto_neuron.llm import _claude_cli, _codex_cli, _pi_cli

    mod = {"codex_cli": _codex_cli, "claude_cli": _claude_cli, "pi_cli": _pi_cli}[driver]
    calls = _install_fake_cli(monkeypatch, mod, stdout, output_content=output_content)
    provider = get_provider(_resolved(driver, **resolved_kwargs))
    text = provider.complete(MESSAGES)
    return provider, text, calls


# ── 1. the canonical usage line ────────────────────────────────────────


@pytest.mark.parametrize(
    "driver, stdout, output_content, expected",
    [
        ("codex_cli", _codex_stdout(), "OK", ("100", "50", "10")),
        ("claude_cli", _claude_stdout(), None, ("220", "80", "30")),
        ("pi_cli", _pi_stdout(), None, ("11", "3", "0")),
    ],
)
def test_canonical_usage_line_matches_harness_regex(
    monkeypatch, caplog, driver, stdout, output_content, expected
) -> None:
    caplog.set_level("INFO", logger="okto_neuron.llm")
    _run(monkeypatch, driver, stdout, output_content=output_content)

    matches = [
        m
        for record in caplog.records
        if (m := _HARNESS_USAGE_RE.search(record.getMessage())) is not None
    ]
    assert len(matches) == 1, "expected exactly one canonical usage line per call"
    match = matches[0]
    prompt, completion, cached = expected
    assert match.group("prompt_tokens") == prompt
    assert match.group("completion_tokens") == completion
    assert match.group("cached_tokens") == cached
    # The model label carries the driver, so a ledger can tell a CLI row from a
    # hosted one — and so it does NOT claim litellm made the call.
    assert match.group("model") == f"{driver}/m-1"


def test_canonical_line_reports_none_for_a_field_the_cli_omitted(monkeypatch, caplog) -> None:
    """A missing token count is reported as ``None``, which the harness parses.

    ``benchmarks/locomo/usage.py`` turns a non-integer field into ``None``
    rather than dropping the row, so an incomplete usage block must still
    produce a matching line.
    """

    caplog.set_level("INFO", logger="okto_neuron.llm")
    _run(
        monkeypatch,
        "codex_cli",
        _codex_stdout({"input_tokens": 7, "output_tokens": 2}),
        output_content="OK",
    )
    line = next(
        m
        for record in caplog.records
        if (m := _HARNESS_USAGE_RE.search(record.getMessage())) is not None
    )
    assert line.group("prompt_tokens") == "7"
    assert line.group("cached_tokens") == "None"


# ── 2. finish reasons: real where real, absent where unknown ───────────


def test_claude_cli_maps_anthropic_stop_reason(monkeypatch) -> None:
    _run(monkeypatch, "claude_cli", _claude_stdout())
    stats = last_call_stats() or {}
    assert stats["native_finish_reason"] == "end_turn"
    assert stats["finish_reason"] == "stop"
    assert stats["cli_terminal_reason"] == "completed"


def test_claude_cli_reports_truncation_instead_of_a_clean_stop(monkeypatch) -> None:
    """``max_tokens`` must surface as ``length``.

    ``extract/__init__.py`` retries on ``finish_reason == "length"``; before
    this, a truncated claude_cli extraction was indistinguishable from a
    complete one and silently kept its half-written JSON.
    """

    _run(monkeypatch, "claude_cli", _claude_stdout(stop_reason="max_tokens"))
    stats = last_call_stats() or {}
    assert stats["finish_reason"] == "length"
    assert stats["native_finish_reason"] == "max_tokens"


def test_claude_cli_never_invents_a_finish_reason(monkeypatch) -> None:
    _run(monkeypatch, "claude_cli", _claude_stdout(stop_reason="some_future_reason"))
    stats = last_call_stats() or {}
    assert "finish_reason" not in stats
    assert stats["native_finish_reason"] == "some_future_reason"
    assert stats["finish_reason_unmapped"] is True


def test_codex_cli_reports_the_turn_event_and_no_finish_reason(monkeypatch) -> None:
    """codex reports no stop reason at all, so none is fabricated.

    Verified live against codex-cli 0.144.6: the terminal event is exactly
    ``{"type": "turn.completed", "usage": {...}}``. ``turn.completed`` is the
    agent loop ending, not the model's last message, so it travels as the
    native reason only.
    """

    _run(monkeypatch, "codex_cli", _codex_stdout(), output_content="OK")
    stats = last_call_stats() or {}
    assert stats["native_finish_reason"] == "turn.completed"
    assert "finish_reason" not in stats
    assert stats["reasoning_tokens"] == 5


@pytest.mark.parametrize(
    "stop_reason, expected",
    [("stop", "stop"), ("length", "length"), ("toolUse", "tool_calls")],
)
def test_pi_cli_maps_its_stop_reasons(monkeypatch, stop_reason, expected) -> None:
    _run(monkeypatch, "pi_cli", _pi_stdout(stopReason=stop_reason))
    stats = last_call_stats() or {}
    assert stats["finish_reason"] == expected
    assert stats["native_finish_reason"] == stop_reason


def test_pi_cli_does_not_map_an_aborted_turn_to_stop(monkeypatch) -> None:
    _run(monkeypatch, "pi_cli", _pi_stdout(stopReason="aborted"))
    stats = last_call_stats() or {}
    assert "finish_reason" not in stats
    assert stats["native_finish_reason"] == "aborted"


# ── 3. sampler accounting ──────────────────────────────────────────────


@pytest.fixture
def observed() -> list[dict]:
    """Capture what the provider reports through the shared request observer."""

    seen: list[dict] = []
    previous = _set_request_observer(seen.append)
    try:
        yield seen
    finally:
        _set_request_observer(previous)


@pytest.mark.parametrize(
    "driver, stdout, output_content",
    [
        ("codex_cli", _codex_stdout(), "OK"),
        ("claude_cli", _claude_stdout(), None),
    ],
)
def test_unsupported_samplers_are_reported_not_swallowed(
    monkeypatch, observed, driver, stdout, output_content
) -> None:
    seen = observed
    from okto_neuron.llm import _claude_cli, _codex_cli

    mod = {"codex_cli": _codex_cli, "claude_cli": _claude_cli}[driver]
    _install_fake_cli(monkeypatch, mod, stdout, output_content=output_content)
    provider = get_provider(_resolved(driver))
    provider.complete(MESSAGES, temperature=0.0, top_p=0.5, max_tokens=64)

    assert len(seen) == 1, "one request event per call, same invariant as LiteLLMProvider"
    omitted = seen[0]["omitted"]
    assert set(omitted) == {"temperature", "top_p", "max_tokens"}
    assert all(reason for reason in omitted.values()), "every omission states a reason"
    # A value that was never configured is not reported either way.
    assert "presence_penalty" not in omitted
    # ``model`` is a litellm CONTROL param, scrubbed from the observer payload
    # by ``_OBSERVER_EXCLUDED_REQUEST_KEYS`` for every provider alike — so the
    # driver label is asserted on the canonical usage line instead, above.
    assert seen[0]["sampling_payload_applied"] is False


def test_pi_cli_sends_thinking_off_when_it_can(monkeypatch, observed) -> None:
    """``pi --thinking off`` is the one sampler flag any of the CLIs exposes."""

    seen = observed
    from okto_neuron.llm import _pi_cli

    calls = _install_fake_cli(monkeypatch, _pi_cli, _pi_stdout())
    provider = get_provider(_resolved("pi_cli"))
    provider.complete(MESSAGES, enable_thinking=False)

    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--thinking") + 1] == "off"
    assert "enable_thinking" not in seen[0]["omitted"]


def test_pi_cli_does_not_override_a_model_pinned_thinking_level(monkeypatch, observed) -> None:
    """A ``provider/id:level`` model spec is more specific than the boolean."""

    seen = observed
    from okto_neuron.llm import _pi_cli

    calls = _install_fake_cli(monkeypatch, _pi_cli, _pi_stdout())
    provider = get_provider(_resolved("pi_cli", model="anthropic/sonnet:high"))
    provider.complete(MESSAGES, enable_thinking=False)

    assert "--thinking" not in calls[0]["cmd"]
    assert "enable_thinking" in seen[0]["omitted"]


def test_enable_thinking_true_is_reported_rather_than_guessed(monkeypatch, observed) -> None:
    seen = observed
    from okto_neuron.llm import _pi_cli

    calls = _install_fake_cli(monkeypatch, _pi_cli, _pi_stdout())
    provider = get_provider(_resolved("pi_cli"))
    provider.complete(MESSAGES, enable_thinking=True)

    assert "--thinking" not in calls[0]["cmd"]
    assert "names no pi thinking level" in seen[0]["omitted"]["enable_thinking"]
