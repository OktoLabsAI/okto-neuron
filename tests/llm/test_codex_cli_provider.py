"""Tests for the Codex CLI provider (codex_cli) — model-free, mocked subprocess."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from okto_neuron.config._vault import ResolvedLLM, _check_provider
from okto_neuron.llm import (
    LLMProvider,
    LLMProviderError,
    Message,
    _set_call_cancel_predicate,
    get_provider,
    last_call_stats,
    set_call_step,
)
from okto_neuron.llm import _codex_cli as mod
from okto_neuron.llm._cli_provider import cancel_requested_cli_processes
from okto_neuron.llm._codex_cli import CodexCliProvider


def _resolved(**overrides) -> ResolvedLLM:
    base = dict(
        provider="codex_cli",
        api_base="http://127.0.0.1:8123/v1",  # required field; ignored by the provider
        model="gpt-5.5",
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


def _stdout_jsonl(*, usage: dict | None = None, extra_events: list[dict] | None = None) -> str:
    """Build the JSONL event stream ``codex exec --json`` emits on stdout."""
    usage = (
        usage
        if usage is not None
        else {
            "input_tokens": 100,
            "cached_input_tokens": 10,
            "output_tokens": 50,
            "reasoning_output_tokens": 5,
        }
    )
    events: list[dict] = [{"type": "thread.started", "thread_id": "abc"}]
    events += extra_events or []
    events.append({"type": "turn.completed", "usage": usage})
    return "\n".join(json.dumps(e) for e in events)


class _FakeCompleted:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


@pytest.fixture
def patched_run(monkeypatch):
    """Patch shutil.which + subprocess.Popen; record the call and simulate the
    ``-o`` output file the real ``codex`` binary writes on success.

    The provider now shells out via Popen + communicate() (Fix 2: reap the
    whole process group on timeout, see _cli_provider.complete()), so the
    fake process object mimics that surface: constructor records the argv
    (mirroring the old ``subprocess.run`` call-recording), and ``communicate``
    returns/raises what the test asked for and records the piped stdin.
    """
    calls: list[dict] = []
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/local/bin/codex")

    def install(response, *, output_content: str | None = None):
        def fake_popen(cmd, **kwargs):
            entry = {"cmd": cmd, **kwargs}
            calls.append(entry)
            if output_content is not None and "-o" in cmd:
                out_path = cmd[cmd.index("-o") + 1]
                Path(out_path).write_text(output_content)

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


def test_get_provider_routes_codex_cli() -> None:
    provider = get_provider(_resolved())
    assert isinstance(provider, CodexCliProvider)
    assert isinstance(provider, LLMProvider)
    assert provider.model == "gpt-5.5"


# ── JSONL parsing — success case ────────────────────────────────────────


def test_basic_completion(patched_run) -> None:
    patched_run(_FakeCompleted(_stdout_jsonl()), output_content="OK")
    out = CodexCliProvider(_resolved()).complete(MESSAGES)
    assert out == "OK"


@pytest.mark.parametrize(
    ("request_timeout_s", "expected"),
    [(None, None), (42.0, 42.0)],
)
def test_named_provider_connection_owns_timeout_policy(
    patched_run, monkeypatch, request_timeout_s, expected
) -> None:
    monkeypatch.setenv("OKTO_NEURON_CODEX_CLI_TIMEOUT", "999")
    calls = patched_run(_FakeCompleted(_stdout_jsonl()), output_content="OK")
    CodexCliProvider(
        _resolved(provider_ref="managed", request_timeout_s=request_timeout_s)
    ).complete(MESSAGES)
    assert calls[0]["timeout"] == expected


def test_non_fatal_error_item_skipped(patched_run) -> None:
    """A non-fatal item.completed error (e.g. plugin-hook parse warning,
    observed live) must not fail the call."""
    extra = [
        {
            "type": "item.completed",
            "item": {
                "id": "item_0",
                "type": "error",
                "message": "plugin hook parse warning",
            },
        }
    ]
    patched_run(_FakeCompleted(_stdout_jsonl(extra_events=extra)), output_content="fine")
    out = CodexCliProvider(_resolved()).complete(MESSAGES)
    assert out == "fine"


def test_turn_failed_raises(patched_run) -> None:
    events = [
        {"type": "thread.started"},
        {"type": "turn.failed", "error": {"message": "service unavailable"}},
    ]
    stdout = "\n".join(json.dumps(e) for e in events)
    patched_run(_FakeCompleted(stdout), output_content="")
    with pytest.raises(LLMProviderError, match="turn.failed") as raised:
        CodexCliProvider(_resolved()).complete(MESSAGES)
    assert raised.value.category == "unavailable"
    assert raised.value.retryable is True


# ── argv shape and stdin piping ─────────────────────────────────────────


def test_argv_shape_and_stdin(patched_run) -> None:
    calls = patched_run(_FakeCompleted(_stdout_jsonl()), output_content="OK")
    CodexCliProvider(_resolved(model="gpt-5.5")).complete(MESSAGES)
    cmd = calls[0]["cmd"]
    assert cmd[1] == "exec"
    assert cmd[cmd.index("-s") + 1] == "read-only"
    assert "--skip-git-repo-check" in cmd
    assert cmd[cmd.index("-c") + 1] == "approval_policy=never"
    assert "--ephemeral" in cmd
    assert "--json" in cmd
    assert cmd[cmd.index("-m") + 1] == "gpt-5.5"
    assert cmd[-1] == "-"
    # codex has no --system-prompt flag; it's folded into stdin instead
    assert "--system-prompt" not in cmd
    assert calls[0]["input"] == "You are terse.\n\nSay OK."


# ── observer sidecar gating (opt-in via CODEX_OBSERVER_DIR) ─────────────


def test_observer_sidecar_disabled_by_default(monkeypatch, patched_run) -> None:
    """No CODEX_OBSERVER_DIR set → nothing is written, ever (it used to default
    to /tmp/codex-observer and dump the full system_prompt/user_text — i.e. vault
    content — to a world-readable tmp dir on EVERY call, including unit tests)."""
    monkeypatch.delenv("CODEX_OBSERVER_DIR", raising=False)
    captured: dict = {}
    orig_parse = CodexCliProvider._parse_output

    def spy_parse(self, stdout):
        captured["sidecar"] = getattr(self._tmp, "observer_sidecar", None)
        return orig_parse(self, stdout)

    monkeypatch.setattr(CodexCliProvider, "_parse_output", spy_parse)
    patched_run(_FakeCompleted(_stdout_jsonl()), output_content="OK")
    out = CodexCliProvider(_resolved()).complete(MESSAGES)
    assert out == "OK"
    assert captured["sidecar"] is None


def test_observer_sidecar_written_and_cleaned_up_when_enabled(
    monkeypatch, patched_run, tmp_path
) -> None:
    """CODEX_OBSERVER_DIR opts in: the sidecar exists mid-call with the full
    system_prompt/user_text, and is unlinked by _cleanup() after."""
    observer_dir = tmp_path / "observer"
    monkeypatch.setenv("CODEX_OBSERVER_DIR", str(observer_dir))
    captured: dict = {}
    orig_parse = CodexCliProvider._parse_output

    def spy_parse(self, stdout):
        sidecar = getattr(self._tmp, "observer_sidecar", None)
        captured["sidecar_path"] = sidecar
        captured["existed_mid_call"] = sidecar is not None and sidecar.exists()
        if sidecar is not None:
            captured["payload"] = json.loads(sidecar.read_text())
        return orig_parse(self, stdout)

    monkeypatch.setattr(CodexCliProvider, "_parse_output", spy_parse)
    patched_run(_FakeCompleted(_stdout_jsonl()), output_content="OK")
    out = CodexCliProvider(_resolved()).complete(MESSAGES)
    assert out == "OK"
    assert captured["existed_mid_call"] is True
    assert captured["payload"]["system_prompt"] == "You are terse."
    assert captured["payload"]["user_text"] == "Say OK."
    # _cleanup() unlinks it after every attempt (success, failure, or reask).
    assert not captured["sidecar_path"].exists()


def test_no_system_prompt_no_fold(patched_run) -> None:
    calls = patched_run(_FakeCompleted(_stdout_jsonl()), output_content="OK")
    CodexCliProvider(_resolved()).complete([Message(role="user", content="hi")])
    assert calls[0]["input"] == "hi"


def test_cwd_is_tempdir(patched_run) -> None:
    calls = patched_run(_FakeCompleted(_stdout_jsonl()), output_content="OK")
    CodexCliProvider(_resolved()).complete(MESSAGES)
    assert calls[0]["cwd"] == mod.tempfile.gettempdir()


# ── schema handling (native strategy — trust as-is, no repair) ─────────


def test_schema_writes_output_schema_file_and_trusts_output(patched_run) -> None:
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    calls = patched_run(_FakeCompleted(_stdout_jsonl()), output_content='{"x": 1}')
    out = CodexCliProvider(_resolved()).complete(
        MESSAGES,
        response_format={"type": "json_schema", "json_schema": {"name": "n", "schema": schema}},
    )
    cmd = calls[0]["cmd"]
    assert "--output-schema" in cmd
    schema_path = cmd[cmd.index("--output-schema") + 1]
    # native strategy trusts the CLI's own enforcement — returned unchanged,
    # no fence-stripping/validation applied
    assert out == '{"x": 1}'
    # schema tempfile is cleaned up after the call
    assert not Path(schema_path).exists()


def test_no_schema_no_output_schema_flag(patched_run) -> None:
    calls = patched_run(_FakeCompleted(_stdout_jsonl()), output_content="OK")
    CodexCliProvider(_resolved()).complete(MESSAGES)
    assert "--output-schema" not in calls[0]["cmd"]


def test_output_tempfile_cleaned_up(patched_run) -> None:
    calls = patched_run(_FakeCompleted(_stdout_jsonl()), output_content="OK")
    CodexCliProvider(_resolved()).complete(MESSAGES)
    out_path = calls[0]["cmd"][calls[0]["cmd"].index("-o") + 1]
    assert not Path(out_path).exists()


def test_build_command_failure_between_mkstemps_does_not_leak_output_file(
    monkeypatch,
) -> None:
    """Defect H: ``_build_command`` used to run OUTSIDE the per-attempt try
    whose ``finally`` calls ``_cleanup()``. If it raises partway through —
    e.g. the ``-o`` output tempfile is created but the ``--output-schema``
    tempfile's ``mkstemp`` then fails (ENOSPC) — the output tempfile leaked
    forever. ``_build_command`` (and the observer-sidecar hook right after
    it) now run INSIDE the try, so ``_cleanup()`` still fires on this
    exit path too."""
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/local/bin/codex")
    real_mkstemp = mod.tempfile.mkstemp
    created: list[str] = []
    call_count = {"n": 0}

    def flaky_mkstemp(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            fd, path = real_mkstemp(*args, **kwargs)
            created.append(path)
            return fd, path
        raise OSError("ENOSPC: no space left on device")

    monkeypatch.setattr(mod.tempfile, "mkstemp", flaky_mkstemp)

    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    provider = CodexCliProvider(_resolved())
    with pytest.raises(OSError):
        provider.complete(
            MESSAGES,
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "n", "schema": schema},
            },
        )

    assert created, "output tempfile was never created"
    assert not Path(created[0]).exists(), (
        "output tempfile leaked after _build_command raised between mkstemps"
    )


def test_tempfiles_cleaned_up_on_nonzero_exit(patched_run) -> None:
    calls = patched_run(_FakeCompleted("", returncode=1, stderr="boom"))
    with pytest.raises(LLMProviderError):
        CodexCliProvider(_resolved()).complete(MESSAGES)
    out_path = calls[0]["cmd"][calls[0]["cmd"].index("-o") + 1]
    assert not Path(out_path).exists()


# ── usage stats extraction ──────────────────────────────────────────────


def test_usage_stats_extracted(patched_run) -> None:
    usage = {
        "input_tokens": 200,
        "cached_input_tokens": 30,
        "output_tokens": 80,
        "reasoning_output_tokens": 12,
    }
    patched_run(_FakeCompleted(_stdout_jsonl(usage=usage)), output_content="data")
    CodexCliProvider(_resolved()).complete(MESSAGES)
    stats = last_call_stats()
    assert stats is not None
    assert stats["prompt_tokens"] == 200
    assert stats["completion_tokens"] == 80
    assert stats["cached_tokens"] == 30


def test_call_log_lines_carry_current_call_step(
    patched_run, caplog: pytest.LogCaptureFixture
) -> None:
    """Fix 1 — the live 7-hour ingest's 'codex call model=... duration=...'
    lines never said WHICH pipeline step made the call. Both codex log lines
    (the shared _cli_provider.py '<binary> call ...' line and codex_cli.py's
    own 'codex_cli usage ...' line) must carry step=<label> once a caller
    (Companion._get_provider's _StepLabelledProvider wrapper) sets it, and
    default to step=- when unset."""
    caplog.set_level("INFO", logger="okto_neuron.llm")
    patched_run(_FakeCompleted(_stdout_jsonl()), output_content="data")

    caplog.clear()
    CodexCliProvider(_resolved()).complete(MESSAGES)
    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith("codex call model=") and m.endswith("step=-") for m in messages)
    assert any(m.startswith("codex_cli usage model=") and m.endswith("step=-") for m in messages)

    caplog.clear()
    set_call_step("relation_curator")
    try:
        CodexCliProvider(_resolved()).complete(MESSAGES)
    finally:
        set_call_step(None)
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        m.startswith("codex call model=") and m.endswith("step=relation_curator") for m in messages
    )
    assert any(
        m.startswith("codex_cli usage model=") and m.endswith("step=relation_curator")
        for m in messages
    )


# ── error / empty-output handling ───────────────────────────────────────


def test_empty_output_file_raises(patched_run) -> None:
    patched_run(_FakeCompleted(_stdout_jsonl()), output_content="")
    with pytest.raises(LLMProviderError, match="empty output"):
        CodexCliProvider(_resolved()).complete(MESSAGES)


def test_nonzero_exit_raises_with_stderr(patched_run) -> None:
    patched_run(_FakeCompleted("", returncode=1, stderr="Error: bad model"))
    with pytest.raises(LLMProviderError, match="bad model") as raised:
        CodexCliProvider(_resolved(model="bogus/model")).complete(MESSAGES)
    assert raised.value.category == "invalid_request"


# ── timeout handling ─────────────────────────────────────────────────────


def test_timeout_raises(patched_run) -> None:
    patched_run(subprocess.TimeoutExpired(cmd="codex", timeout=300))
    with pytest.raises(LLMProviderError, match="timed out") as raised:
        CodexCliProvider(_resolved()).complete(MESSAGES)
    assert raised.value.category == "timeout"
    assert raised.value.retryable is True


def test_real_subprocess_timeout_reaps_process_group(monkeypatch, tmp_path) -> None:
    """Live incident: a plain ``subprocess.run(timeout=)`` kill only reaches the
    direct child (the CLI's node wrapper); the grandchild arm64 binary reparents
    to launchd and leaks (8 orphans observed, PPID=1, up to 5.2h old, ~110MB RSS
    each). Drive a REAL subprocess whose child forks a grandchild that outlives
    the timeout, and assert the whole group is gone afterwards — not just that
    an error was raised.
    """
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/bin/sh")
    marker = tmp_path / "grandchild.pid"
    script = tmp_path / "spawn_grandchild.sh"
    # The grandchild (`sleep`) is backgrounded in the SAME session as this
    # script — exactly the shape start_new_session=True must reap via killpg.
    script.write_text(f"sleep 30 &\necho $! > {marker}\nwait\n")

    def fake_build_command(self, binary, model, system_prompt, schema):
        return ["/bin/sh", str(script)]

    monkeypatch.setattr(CodexCliProvider, "_build_command", fake_build_command)
    monkeypatch.setattr(CodexCliProvider, "_stdin_payload", lambda self, sp, ut: "")

    provider = CodexCliProvider(_resolved())
    provider._timeout = 0.5

    with pytest.raises(LLMProviderError, match="timed out"):
        provider.complete(MESSAGES)

    # Give the OS a moment to actually tear the group down.
    deadline = time.time() + 5
    grandchild_pid: int | None = None
    while time.time() < deadline:
        if marker.exists() and marker.read_text().strip():
            grandchild_pid = int(marker.read_text().strip())
            break
        time.sleep(0.05)
    assert grandchild_pid is not None, "script never recorded the grandchild pid"

    alive = True
    for _ in range(50):
        try:
            os.kill(grandchild_pid, 0)
        except (ProcessLookupError, PermissionError):
            alive = False
            break
        time.sleep(0.1)
    assert not alive, (
        f"grandchild pid {grandchild_pid} was still alive after timeout — "
        "process-group reap failed, orphan leaked"
    )


def test_real_scoped_cancellation_reaps_process_group(monkeypatch, tmp_path) -> None:
    """Stop a tagged in-flight call and reap its whole subprocess tree.

    The provider runs on a worker thread, matching ``asyncio.to_thread`` in the
    ingest queue. Its thread-local predicate is copied into the active-process
    registry; the event-loop thread then requests scoped cancellation and must
    unblock the provider without waiting for its normal timeout.
    """
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/bin/sh")
    child_marker = tmp_path / "child.pid"
    grandchild_marker = tmp_path / "grandchild.pid"
    script = tmp_path / "spawn_grandchild.sh"
    script.write_text(
        f"echo $$ > {child_marker}\nsleep 30 &\necho $! > {grandchild_marker}\nwait\n"
    )

    def fake_build_command(self, binary, model, system_prompt, schema):
        return ["/bin/sh", str(script)]

    monkeypatch.setattr(CodexCliProvider, "_build_command", fake_build_command)
    monkeypatch.setattr(CodexCliProvider, "_stdin_payload", lambda self, sp, ut: "")

    cancel_requested = threading.Event()
    finished = threading.Event()
    errors: list[BaseException] = []

    def run_provider() -> None:
        previous = _set_call_cancel_predicate(cancel_requested.is_set)
        try:
            CodexCliProvider(_resolved()).complete(MESSAGES)
        except BaseException as exc:  # the cancellation path must unwind the owner thread
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
        assert child_marker.exists(), "script never recorded the child pid"
        assert grandchild_marker.exists(), "script never recorded the grandchild pid"

        child_pid = int(child_marker.read_text().strip())
        grandchild_pid = int(grandchild_marker.read_text().strip())
        cancel_requested.set()
        assert cancel_requested_cli_processes() == 1

        worker.join(timeout=5)
        assert not worker.is_alive(), "provider thread stayed blocked after scoped cancellation"
        assert errors, "cancelled provider call unexpectedly returned successfully"

        for pid, label in ((child_pid, "child"), (grandchild_pid, "grandchild")):
            alive = True
            for _ in range(50):
                try:
                    os.kill(pid, 0)
                except (ProcessLookupError, PermissionError):
                    alive = False
                    break
                time.sleep(0.1)
            assert not alive, f"{label} pid {pid} survived scoped process-group cancellation"
    finally:
        cancel_requested.set()
        cancel_requested_cli_processes()
        worker.join(timeout=5)


def test_binary_missing_raises(monkeypatch) -> None:
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    with pytest.raises(LLMProviderError, match="not found on PATH"):
        CodexCliProvider(_resolved()).complete(MESSAGES)


def test_timeout_env_override(monkeypatch) -> None:
    monkeypatch.setenv("OKTO_NEURON_CODEX_CLI_TIMEOUT", "999")
    assert CodexCliProvider(_resolved())._timeout == 999.0


# ── config provider and aliases ───────────────────────────────────────────


def test_config_provider_and_aliases() -> None:
    assert _check_provider("codex_cli") == "codex_cli"
    assert _check_provider("codex") == "codex_cli"
    with pytest.raises(ValueError):
        _check_provider("codex-desktop")
