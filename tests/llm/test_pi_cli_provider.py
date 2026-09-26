"""Tests for the Pi CLI provider (pi_cli) — model-free, mocked subprocess."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from okto_neuron.config._vault import ResolvedLLM, _check_provider
from okto_neuron.llm import (
    LLMProvider,
    LLMProviderError,
    Message,
    get_provider,
    last_call_stats,
    set_call_step,
)
from okto_neuron.llm import _pi_cli as mod
from okto_neuron.llm._pi_cli import PiCliProvider


def _resolved(**overrides) -> ResolvedLLM:
    base = dict(
        provider="pi_cli",
        api_base="http://127.0.0.1:8123/v1",  # required field; ignored by the provider
        model="anthropic/claude-haiku-4-5",
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


def _agent_end_envelope(
    *,
    text: str = "hello",
    stop_reason: str = "end_turn",
    usage: dict | None = None,
    include_thinking: bool = False,
) -> str:
    """Build NDJSON with an agent_end event."""
    content_items: list[dict] = []
    if include_thinking:
        content_items.append({"type": "thinking", "text": "let me think..."})
    content_items.append({"type": "text", "text": text})

    usage_dict = usage or {
        "input": 100,
        "output": 50,
        "cacheRead": 10,
        "cacheWrite": 0,
        "reasoning": 0,
        "totalTokens": 150,
        "cost": 0.001,
    }

    msg = {
        "role": "assistant",
        "content": content_items,
        "usage": usage_dict,
        "stopReason": stop_reason,
    }
    events = [
        {"type": "init", "version": 1},
        {"type": "agent_end", "messages": [msg]},
    ]
    return "\n".join(json.dumps(e) for e in events)


class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


@pytest.fixture
def patched_run(monkeypatch):
    """Patch shutil.which + subprocess.Popen; record the call.

    The provider now shells out via Popen + communicate() (Fix 2 in
    _cli_provider.complete(): reap the whole process group on timeout), so the
    fake process object mimics that surface — constructor records the argv
    (mirroring the old ``subprocess.run`` call-recording), and ``communicate``
    returns/raises what the test asked for and records the piped stdin.
    """
    calls: list[dict] = []
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/local/bin/pi")

    def install(response):
        def fake_popen(cmd, **kwargs):
            entry = {"cmd": cmd, **kwargs}
            # complete()'s finally unlinks the system-prompt tempfile before
            # this function returns to the test, so snapshot its content now
            # (the fake process object never actually reads it).
            if "--system-prompt" in cmd:
                path = cmd[cmd.index("--system-prompt") + 1]
                entry["system_prompt_file_content"] = Path(path).read_text(encoding="utf-8")
                entry["system_prompt_file_path"] = path
            calls.append(entry)

            class _FakeProc:
                pid = 999_999_999  # never a real pid: killpg is guarded, wait() is a no-op
                returncode = 0

                def communicate(self, input=None, timeout=None):
                    entry["input"] = input
                    entry["timeout"] = timeout
                    if isinstance(response, Exception):
                        raise response
                    self.returncode = response.returncode
                    return response.stdout, response.stderr

                def wait(self):
                    return None

            return _FakeProc()

        monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
        return calls

    return install


MESSAGES = [
    Message(role="system", content="You are terse."),
    Message(role="user", content="Say OK."),
]


# ── get_provider routing ───────────────────────────────────────────────


def test_get_provider_routes_pi_cli() -> None:
    provider = get_provider(_resolved())
    assert isinstance(provider, PiCliProvider)
    assert isinstance(provider, LLMProvider)
    assert provider.model == "anthropic/claude-haiku-4-5"


# ── NDJSON parsing — success case ─────────────────────────────────────


def test_basic_completion(patched_run) -> None:
    patched_run(_FakeCompleted(_agent_end_envelope(text="OK")))
    provider = PiCliProvider(_resolved(model="anthropic/claude-haiku-4-5"))
    out = provider.complete(MESSAGES)
    assert out == "OK"


@pytest.mark.parametrize(
    ("request_timeout_s", "expected"),
    [(None, None), (42.0, 42.0)],
)
def test_named_provider_connection_owns_timeout_policy(
    patched_run, monkeypatch, request_timeout_s, expected
) -> None:
    monkeypatch.setenv("OKTO_NEURON_PI_CLI_TIMEOUT", "999")
    calls = patched_run(_FakeCompleted(_agent_end_envelope(text="OK")))
    PiCliProvider(_resolved(provider_ref="managed", request_timeout_s=request_timeout_s)).complete(
        MESSAGES
    )
    assert calls[0]["timeout"] == expected


def test_thinking_items_skipped(patched_run) -> None:
    patched_run(_FakeCompleted(_agent_end_envelope(text="answer", include_thinking=True)))
    out = PiCliProvider(_resolved()).complete(MESSAGES)
    assert out == "answer"


def test_multiple_text_items_concatenated(patched_run) -> None:
    """When the last message has multiple text content items, they are concatenated."""
    content = [
        {"type": "text", "text": "Hello "},
        {"type": "text", "text": "World"},
    ]
    msg = {
        "role": "assistant",
        "content": content,
        "usage": {"input": 10, "output": 5, "cacheRead": 0, "totalTokens": 15, "cost": 0.0},
        "stopReason": "end_turn",
    }
    ndjson = "\n".join(
        [
            json.dumps({"type": "init"}),
            json.dumps({"type": "agent_end", "messages": [msg]}),
        ]
    )
    patched_run(_FakeCompleted(ndjson))
    out = PiCliProvider(_resolved()).complete(MESSAGES)
    assert out == "Hello World"


# ── NDJSON parsing — error cases ──────────────────────────────────────


def test_stop_reason_error_raises(patched_run) -> None:
    patched_run(_FakeCompleted(_agent_end_envelope(text="rate limit", stop_reason="error")))
    with pytest.raises(LLMProviderError, match="reported error") as raised:
        PiCliProvider(_resolved()).complete(MESSAGES)
    assert raised.value.category == "rate_limited"
    assert raised.value.retryable is True


def test_no_agent_end_found_raises(patched_run) -> None:
    ndjson = "\n".join(
        [
            json.dumps({"type": "init"}),
            json.dumps({"type": "message", "content": "partial"}),
        ]
    )
    patched_run(_FakeCompleted(ndjson))
    with pytest.raises(LLMProviderError, match="no agent_end event") as raised:
        PiCliProvider(_resolved()).complete(MESSAGES)
    assert raised.value.category == "malformed_output"


def test_non_json_lines_skipped(patched_run) -> None:
    """Non-JSON lines in the stream are silently skipped."""
    ndjson = "\n".join(
        [
            "some garbage line",
            json.dumps({"type": "init"}),
            "--- progress ---",
            json.dumps(
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "ok"}],
                            "usage": {
                                "input": 1,
                                "output": 1,
                                "cacheRead": 0,
                                "totalTokens": 2,
                                "cost": 0,
                            },
                            "stopReason": "end_turn",
                        }
                    ],
                }
            ),
        ]
    )
    patched_run(_FakeCompleted(ndjson))
    out = PiCliProvider(_resolved()).complete(MESSAGES)
    assert out == "ok"


# ── argv shape and stdin piping ───────────────────────────────────────


def test_argv_shape_and_stdin(patched_run) -> None:
    calls = patched_run(_FakeCompleted(_agent_end_envelope(text="OK")))
    provider = PiCliProvider(_resolved(model="anthropic/claude-sonnet-4-5"))
    out = provider.complete(MESSAGES)
    assert out == "OK"
    call = calls[0]
    cmd = call["cmd"]
    assert "-p" in cmd
    assert cmd[cmd.index("--mode") + 1] == "json"
    assert "--no-session" in cmd
    assert "--no-tools" in cmd
    assert "--no-context-files" in cmd
    assert "--no-extensions" in cmd
    assert "--no-skills" in cmd
    assert "--no-prompt-templates" in cmd
    assert cmd[cmd.index("--model") + 1] == "anthropic/claude-sonnet-4-5"
    # 3.20: the system prompt travels via a private tempfile path, not as
    # literal text in argv (visible via ps -ef). pi's own arg resolver treats
    # an existing-file --system-prompt value as a path to read, not the
    # literal prompt (see module docstring).
    assert call["system_prompt_file_content"] == "You are terse."
    assert call["input"] == "Say OK."
    # _cleanup() (called from the finally in complete()) removes the tempfile.
    assert not Path(call["system_prompt_file_path"]).exists()


def test_model_passed_verbatim_no_splitting(patched_run) -> None:
    """Model string with provider prefix is passed to --model unmodified."""
    calls = patched_run(_FakeCompleted(_agent_end_envelope(text="OK")))
    PiCliProvider(_resolved(model="google/gemini-2.5-flash:high")).complete(MESSAGES)
    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--model") + 1] == "google/gemini-2.5-flash:high"


def test_multiple_system_messages_joined(patched_run) -> None:
    calls = patched_run(_FakeCompleted(_agent_end_envelope()))
    PiCliProvider(_resolved()).complete(
        [
            Message(role="system", content="A"),
            Message(role="system", content="B"),
            Message(role="user", content="u1"),
            Message(role="assistant", content="a1"),
            Message(role="user", content="u2"),
        ]
    )
    assert calls[0]["system_prompt_file_content"] == "A\n\nB"
    assert calls[0]["input"] == "u1\n\nAssistant: a1\n\nu2"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file-mode bits")
def test_system_prompt_tempfile_is_owner_read_write_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3.20: the system-prompt tempfile must not be group/world readable —
    it can carry vault-specific instructions, not just a constant."""
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/local/bin/pi")
    perms: list[int] = []
    response = _FakeCompleted(_agent_end_envelope(text="OK"))

    def fake_popen(cmd, **kwargs):
        prompt_path = cmd[cmd.index("--system-prompt") + 1]
        perms.append(stat.S_IMODE(os.stat(prompt_path).st_mode))

        class _FakeProc:
            pid = 999_999_999
            returncode = 0

            def communicate(self, input=None, timeout=None):
                self.returncode = response.returncode
                return response.stdout, response.stderr

            def wait(self):
                return None

        return _FakeProc()

    monkeypatch.setattr(mod.subprocess, "Popen", fake_popen)
    PiCliProvider(_resolved()).complete(MESSAGES)
    assert perms == [0o600]


def test_cwd_is_tempdir(patched_run) -> None:
    calls = patched_run(_FakeCompleted(_agent_end_envelope()))
    PiCliProvider(_resolved()).complete(MESSAGES)
    assert calls[0]["cwd"] == mod.tempfile.gettempdir()


# ── response_format (prompt-embedded schema) ──────────────────────────


def test_response_format_embeds_schema_in_prompt(patched_run) -> None:
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    calls = patched_run(_FakeCompleted(_agent_end_envelope(text='{"x": 1}')))
    out = PiCliProvider(_resolved()).complete(
        MESSAGES,
        response_format={"type": "json_schema", "json_schema": {"name": "n", "schema": schema}},
    )
    # The schema instructions should be appended to the stdin text
    input_text = calls[0]["input"]
    assert "Respond with ONLY valid JSON" in input_text
    assert '"type": "object"' in input_text
    assert '"properties"' in input_text
    assert '"x"' in input_text
    # complete() now returns validated/normalized JSON, not raw fenced text
    assert json.loads(out) == {"x": 1}


# ── nonzero exit handling ─────────────────────────────────────────────


def test_nonzero_exit_raises_with_stderr(patched_run) -> None:
    calls = patched_run(_FakeCompleted("", returncode=1, stderr='Error: Unknown provider "bogus".'))
    with pytest.raises(LLMProviderError, match="Unknown provider") as raised:
        PiCliProvider(_resolved(model="bogus/model")).complete(MESSAGES)
    assert raised.value.category == "invalid_request"
    # _cleanup() runs from complete()'s finally on the error path too.
    cmd = calls[0]["cmd"]
    prompt_path = cmd[cmd.index("--system-prompt") + 1]
    assert not Path(prompt_path).exists()


# ── usage stats extraction ────────────────────────────────────────────


def test_usage_stats_extracted(patched_run) -> None:
    usage = {
        "input": 200,
        "output": 80,
        "cacheRead": 30,
        "cacheWrite": 5,
        "totalTokens": 280,
        "cost": 0.005,
        "reasoning": 0,
    }
    patched_run(_FakeCompleted(_agent_end_envelope(text="data", usage=usage)))
    PiCliProvider(_resolved()).complete(MESSAGES)
    stats = last_call_stats()
    assert stats is not None
    assert stats["prompt_tokens"] == 200
    assert stats["completion_tokens"] == 80
    assert stats["cached_tokens"] == 30


def test_usage_stats_partial_keys(patched_run) -> None:
    """Only int keys are included; missing keys are omitted."""
    usage = {"input": 50, "output": 20}  # no cacheRead
    patched_run(_FakeCompleted(_agent_end_envelope(text="ok", usage=usage)))
    PiCliProvider(_resolved()).complete(MESSAGES)
    stats = last_call_stats()
    assert stats is not None
    assert stats["prompt_tokens"] == 50
    assert stats["completion_tokens"] == 20
    assert "cached_tokens" not in stats


def test_usage_log_line_carries_current_call_step(
    patched_run, caplog: pytest.LogCaptureFixture
) -> None:
    """Fix 1 — pi_cli's usage log line must say WHICH pipeline step made the
    call: default step=- when unset, the set label once a caller
    (Companion._get_provider's _StepLabelledProvider wrapper) sets it."""
    caplog.set_level("INFO", logger="okto_neuron.llm")
    patched_run(_FakeCompleted(_agent_end_envelope(text="ok")))

    caplog.clear()
    PiCliProvider(_resolved()).complete(MESSAGES)
    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith("pi_cli usage model=") and m.endswith("step=-") for m in messages)

    caplog.clear()
    set_call_step("curator")
    try:
        PiCliProvider(_resolved()).complete(MESSAGES)
    finally:
        set_call_step(None)
    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith("pi_cli usage model=") and m.endswith("step=curator") for m in messages)


# ── timeout handling ──────────────────────────────────────────────────


def test_timeout_raises(patched_run) -> None:
    patched_run(subprocess.TimeoutExpired(cmd="pi", timeout=300))
    with pytest.raises(LLMProviderError, match="timed out") as raised:
        PiCliProvider(_resolved()).complete(MESSAGES)
    assert raised.value.category == "timeout"
    assert raised.value.retryable is True


def test_binary_missing_raises(monkeypatch) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    with pytest.raises(LLMProviderError, match="not found on PATH"):
        PiCliProvider(_resolved()).complete(MESSAGES)


def test_timeout_env_override(monkeypatch) -> None:
    monkeypatch.setenv("OKTO_NEURON_PI_CLI_TIMEOUT", "999")
    assert PiCliProvider(_resolved())._timeout == 999.0


def test_scoped_call_timeout_narrows_process_timeout(patched_run) -> None:
    """3.6: CliShellProvider.complete() must honor an outer scoped task
    deadline for every CLI-shell backend, not just LiteLLM's."""
    from okto_neuron.llm import _scoped_call_timeout

    calls = patched_run(_FakeCompleted(_agent_end_envelope(text="OK")))
    provider = PiCliProvider(_resolved())  # self._timeout == 300.0 (default)
    with _scoped_call_timeout(5.0):
        provider.complete(MESSAGES)
    assert 0.0 < calls[-1]["timeout"] <= 5.0


# ── config provider and aliases ───────────────────────────────────────


def test_config_provider_and_aliases() -> None:
    assert _check_provider("pi_cli") == "pi_cli"
    assert _check_provider("pi") == "pi_cli"
    with pytest.raises(ValueError):
        _check_provider("pi-desktop")
