"""Tests for the Claude Code CLI provider (claude_cli) — model-free, mocked subprocess."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from okto_neuron.config._vault import ResolvedLLM, _check_provider
from okto_neuron.llm import (
    LLMCallCancelled,
    LLMProvider,
    LLMProviderError,
    Message,
    _set_call_cancel_predicate,
    get_provider,
    last_call_stats,
    set_call_step,
)
from okto_neuron.llm import _claude_cli as mod
from okto_neuron.llm._cli_provider import cancel_requested_cli_processes
from okto_neuron.llm._claude_cli import ClaudeCliProvider


def _resolved(**overrides) -> ResolvedLLM:
    base = dict(
        provider="claude_cli",
        api_base="http://127.0.0.1:8123/v1",  # required field; ignored by the provider
        model="haiku",
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


def _result_envelope(
    *,
    result: str = "hello",
    structured: object = None,
    is_error: bool = False,
    usage: dict | None = None,
) -> str:
    return json.dumps(
        [
            {"type": "system", "subtype": "init"},
            {
                "type": "result",
                "is_error": is_error,
                "result": result,
                "structured_output": structured,
                "total_cost_usd": 0.0024,
                "usage": usage
                or {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 10,
                    "cache_creation_input_tokens": 5,
                },
            },
        ]
    )


class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


@pytest.fixture
def patched_run(monkeypatch):
    """Patch shutil.which + subprocess.Popen; record the call."""
    calls: list[dict] = []
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/local/bin/claude")

    def install(response):
        def fake_popen(cmd, **kwargs):
            entry = {"cmd": cmd, **kwargs}
            # complete()'s finally unlinks the system-prompt tempfile before
            # this function returns to the test, so snapshot its content now
            # (the fake process object never actually reads it).
            if "--system-prompt-file" in cmd:
                path = cmd[cmd.index("--system-prompt-file") + 1]
                entry["system_prompt_file_content"] = Path(path).read_text(encoding="utf-8")
                entry["system_prompt_file_path"] = path
            calls.append(entry)

            class _FakeProc:
                pid = 999_999_999
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


def test_get_provider_routes_claude_cli() -> None:
    provider = get_provider(_resolved())
    assert isinstance(provider, ClaudeCliProvider)
    assert isinstance(provider, LLMProvider)
    assert provider.model == "haiku"


def test_argv_shape_and_stdin(patched_run) -> None:
    calls = patched_run(_FakeCompleted(_result_envelope(result="OK")))
    provider = ClaudeCliProvider(_resolved(model="claude-fable-5"))
    out = provider.complete(MESSAGES)
    assert out == "OK"
    call = calls[0]
    cmd = call["cmd"]
    assert "--bare" in cmd
    assert "-p" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--no-session-persistence" in cmd
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-fable-5"
    # 3.20: the system prompt travels via a private tempfile + --system-prompt-file,
    # never as a bare --system-prompt <text> argv element (visible via ps -ef).
    assert "--system-prompt" not in cmd
    assert call["system_prompt_file_content"] == "You are terse."
    assert call["input"] == "Say OK."
    assert call["timeout"] == 300.0
    if os.name != "nt":
        assert call["start_new_session"] is True
    assert "--json-schema" not in cmd
    # _cleanup() (called from the finally in complete()) removes the tempfile.
    assert not Path(call["system_prompt_file_path"]).exists()


@pytest.mark.parametrize(
    ("request_timeout_s", "expected"),
    [(None, None), (42.0, 42.0)],
)
def test_named_provider_connection_owns_timeout_policy(
    patched_run, monkeypatch, request_timeout_s, expected
) -> None:
    monkeypatch.setenv("OKTO_NEURON_CLAUDE_CLI_TIMEOUT", "999")
    calls = patched_run(_FakeCompleted(_result_envelope(result="OK")))
    ClaudeCliProvider(
        _resolved(provider_ref="managed", request_timeout_s=request_timeout_s)
    ).complete(MESSAGES)
    assert calls[0]["timeout"] == expected


def test_multiple_system_messages_joined(patched_run) -> None:
    calls = patched_run(_FakeCompleted(_result_envelope()))
    ClaudeCliProvider(_resolved()).complete(
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
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/local/bin/claude")
    perms: list[int] = []
    response = _FakeCompleted(_result_envelope(result="OK"))

    def fake_popen(cmd, **kwargs):
        prompt_path = cmd[cmd.index("--system-prompt-file") + 1]
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
    ClaudeCliProvider(_resolved()).complete(MESSAGES)
    assert perms == [0o600]


def test_response_format_passes_json_schema_and_returns_structured(patched_run) -> None:
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    calls = patched_run(_FakeCompleted(_result_envelope(structured={"x": 1})))
    out = ClaudeCliProvider(_resolved()).complete(
        MESSAGES,
        response_format={"type": "json_schema", "json_schema": {"name": "n", "schema": schema}},
    )
    cmd = calls[0]["cmd"]
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == schema
    assert json.loads(out) == {"x": 1}


def test_plain_result_strips_think(patched_run) -> None:
    patched_run(_FakeCompleted(_result_envelope(result="<think>hmm</think>answer")))
    assert ClaudeCliProvider(_resolved()).complete(MESSAGES) == "answer"


def test_nonzero_exit_raises_with_stderr(patched_run) -> None:
    calls = patched_run(_FakeCompleted("", returncode=2, stderr="unauthorized"))
    with pytest.raises(LLMProviderError, match="unauthorized") as raised:
        ClaudeCliProvider(_resolved()).complete(MESSAGES)
    assert raised.value.category == "authentication"
    assert raised.value.retryable is False
    # _cleanup() runs from complete()'s finally on the error path too.
    cmd = calls[0]["cmd"]
    prompt_path = cmd[cmd.index("--system-prompt-file") + 1]
    assert not Path(prompt_path).exists()


def test_is_error_raises(patched_run) -> None:
    patched_run(_FakeCompleted(_result_envelope(result="bad", is_error=True)))
    with pytest.raises(LLMProviderError, match="is_error"):
        ClaudeCliProvider(_resolved()).complete(MESSAGES)


def test_unparseable_stdout_raises(patched_run) -> None:
    patched_run(_FakeCompleted("not json", stderr="parse diagnostic"))
    with pytest.raises(LLMProviderError, match="unparseable.*parse diagnostic") as raised:
        ClaudeCliProvider(_resolved()).complete(MESSAGES)
    assert raised.value.category == "malformed_output"


def test_missing_result_element_raises(patched_run) -> None:
    patched_run(_FakeCompleted(json.dumps([{"type": "system"}])))
    with pytest.raises(LLMProviderError, match="no result element"):
        ClaudeCliProvider(_resolved()).complete(MESSAGES)


def test_timeout_raises(patched_run) -> None:
    patched_run(subprocess.TimeoutExpired(cmd="claude", timeout=300))
    with pytest.raises(LLMProviderError, match="timed out") as raised:
        ClaudeCliProvider(_resolved()).complete(MESSAGES)
    assert raised.value.category == "timeout"
    assert raised.value.retryable is True


def test_usage_stats_and_step_log_are_preserved(
    patched_run, caplog: pytest.LogCaptureFixture
) -> None:
    usage = {
        "input_tokens": 220,
        "output_tokens": 80,
        "cache_read_input_tokens": 30,
        "cache_creation_input_tokens": 12,
    }
    patched_run(_FakeCompleted(_result_envelope(result="done", usage=usage)))
    caplog.set_level("INFO", logger="okto_neuron.llm")
    set_call_step("relation_curator")
    try:
        assert ClaudeCliProvider(_resolved()).complete(MESSAGES) == "done"
    finally:
        set_call_step(None)

    assert last_call_stats() == {
        "prompt_tokens": 220,
        "completion_tokens": 80,
        "cached_tokens": 30,
        # The claude CLI uniquely reports real spend; it rides the same stats
        # channel as the token counts so cost can be aggregated per step.
        "total_cost_usd": 0.0024,
    }
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        message.startswith("claude_cli call model=")
        and "cost_usd=0.0024" in message
        and message.endswith("step=relation_curator")
        for message in messages
    )
    assert any(
        message.startswith("claude call model=") and message.endswith("step=relation_curator")
        for message in messages
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group regression")
def test_real_scoped_cancellation_reaps_claude_process_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Stop Bulk Ingest cancels Claude's wrapper and its CLI grandchild."""
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/bin/sh")
    child_marker = tmp_path / "child.pid"
    grandchild_marker = tmp_path / "grandchild.pid"
    script = tmp_path / "spawn_claude_tree.sh"
    script.write_text(
        f"echo $$ > {child_marker}\nsleep 30 &\necho $! > {grandchild_marker}\nwait\n",
        encoding="utf-8",
    )

    def fake_build_command(self, binary, model, system_prompt, schema):
        return ["/bin/sh", str(script)]

    monkeypatch.setattr(ClaudeCliProvider, "_build_command", fake_build_command)
    monkeypatch.setattr(ClaudeCliProvider, "_stdin_payload", lambda self, sp, ut: "")

    cancel_requested = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def run_provider() -> None:
        previous = _set_call_cancel_predicate(cancel_requested.is_set)
        try:
            ClaudeCliProvider(_resolved()).complete(MESSAGES)
        except BaseException as exc:
            errors.append(exc)
        finally:
            _set_call_cancel_predicate(previous)
            finished.set()

    worker = threading.Thread(target=run_provider, daemon=True)
    worker.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline:
            if (
                child_marker.exists()
                and child_marker.read_text().strip()
                and grandchild_marker.exists()
                and grandchild_marker.read_text().strip()
            ):
                break
            if finished.is_set():
                break
            time.sleep(0.05)

        assert child_marker.exists(), "Claude wrapper never recorded its pid"
        assert grandchild_marker.exists(), "Claude grandchild never recorded its pid"
        child_pid = int(child_marker.read_text().strip())
        grandchild_pid = int(grandchild_marker.read_text().strip())

        cancel_requested.set()
        assert cancel_requested_cli_processes() == 1
        worker.join(timeout=5)
        assert not worker.is_alive(), "Claude provider stayed blocked after cancellation"
        assert len(errors) == 1
        assert isinstance(errors[0], LLMCallCancelled)

        for pid, label in ((child_pid, "wrapper"), (grandchild_pid, "grandchild")):
            alive = True
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, PermissionError):
                    alive = False
                    break
                time.sleep(0.1)
            assert not alive, f"Claude {label} pid {pid} survived scoped cancellation"
    finally:
        cancel_requested.set()
        cancel_requested_cli_processes()
        worker.join(timeout=5)


def test_binary_missing_raises(monkeypatch) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    with pytest.raises(LLMProviderError, match="not found on PATH"):
        ClaudeCliProvider(_resolved()).complete(MESSAGES)


def test_timeout_env_override(monkeypatch) -> None:
    monkeypatch.setenv("OKTO_NEURON_CLAUDE_CLI_TIMEOUT", "42")
    assert ClaudeCliProvider(_resolved())._timeout == 42.0


def test_scoped_call_timeout_narrows_process_timeout(patched_run) -> None:
    """3.6: CliShellProvider.complete() must honor an outer scoped task
    deadline (ADR 0015 curation_call_timeout_s / _scoped_call_timeout) the
    same way _run_litellm_completion does — min(self._timeout, remaining).
    Before the fix, CliShellProvider.complete() only ever passed
    timeout=self._timeout to proc.communicate(), ignoring the scope entirely.
    """
    from okto_neuron.llm import _scoped_call_timeout

    calls = patched_run(_FakeCompleted(_result_envelope(result="OK")))
    provider = ClaudeCliProvider(_resolved())  # self._timeout == 300.0 (default)
    with _scoped_call_timeout(5.0):
        provider.complete(MESSAGES)
    assert 0.0 < calls[-1]["timeout"] <= 5.0


def test_scoped_call_timeout_does_not_widen_provider_timeout(patched_run) -> None:
    """The narrower of the two deadlines always wins, in either direction."""
    from okto_neuron.llm import _scoped_call_timeout

    calls = patched_run(_FakeCompleted(_result_envelope(result="OK")))
    provider = ClaudeCliProvider(_resolved(request_timeout_s=3.0))
    with _scoped_call_timeout(999.0):
        provider.complete(MESSAGES)
    assert calls[-1]["timeout"] == 3.0


def test_scoped_call_timeout_expired_raises_without_spawning_process(
    patched_run,
) -> None:
    """A deadline that has already elapsed must fail closed before shelling
    out at all, matching _run_litellm_completion's pre-flight check."""
    from okto_neuron.llm import _scoped_call_timeout

    calls = patched_run(_FakeCompleted(_result_envelope(result="OK")))
    provider = ClaudeCliProvider(_resolved())
    with _scoped_call_timeout(0.0):
        with pytest.raises(LLMProviderError, match="deadline expired") as raised:
            provider.complete(MESSAGES)
    assert raised.value.category == "timeout"
    assert calls == []


def test_config_provider_and_aliases() -> None:
    assert _check_provider("claude_cli") == "claude_cli"
    assert _check_provider("claude-code") == "claude_cli"
    assert _check_provider("claude_code") == "claude_cli"
    with pytest.raises(ValueError):
        _check_provider("claude-desktop")
