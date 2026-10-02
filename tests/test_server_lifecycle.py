"""Tests for okto_neuron.server.lifecycle."""

from __future__ import annotations

import io
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from okto_neuron.server.lifecycle import (
    JsonLogFormatter,
    LifecycleError,
    PidFile,
    StaleLockError,
    configure_logging,
    daemonize,
    pid_file_path,
    read_pid,
    send_stop,
    stop_server,
    stream_is_file,
)
from okto_neuron.server import lifecycle as lifecycle_module


def test_pid_file_path_layout(tmp_path: Path) -> None:
    assert pid_file_path(tmp_path) == tmp_path / ".marginalia" / "server.pid"


def test_pidfile_writes_and_removes(tmp_path: Path) -> None:
    pf = PidFile(tmp_path)
    with pf:
        pid = read_pid(tmp_path)
        assert pid == os.getpid()
        assert pid_file_path(tmp_path).exists()
        payload = json.loads(pid_file_path(tmp_path).read_text(encoding="utf-8"))
        assert payload["version"] == lifecycle_module.PID_RECORD_VERSION
        assert payload["pid"] == os.getpid()
        assert payload["start_token"]
        assert payload["owner_id"]
    assert not pid_file_path(tmp_path).exists()


def test_pidfile_write_does_not_require_fchmod(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(lifecycle_module.os, "fchmod", raising=False)

    with PidFile(tmp_path):
        assert read_pid(tmp_path) == os.getpid()


def test_windows_lock_byte_does_not_block_pid_record_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_calls: list[tuple[int, int]] = []

    class FakeMsvcrt:
        LK_NBLCK = 1
        LK_UNLCK = 2

        @staticmethod
        def locking(fd: int, mode: int, _length: int) -> None:
            lock_calls.append((mode, os.lseek(fd, 0, os.SEEK_CUR)))

    monkeypatch.setitem(sys.modules, "msvcrt", FakeMsvcrt)
    monkeypatch.setattr(lifecycle_module.os, "name", "nt")
    path = tmp_path / "server.pid"
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        assert lifecycle_module._try_lock_pid_fd(fd) is True
        lifecycle_module._write_pid_fd(
            fd,
            lifecycle_module._PidRecord(
                pid=4242,
                start_token="windows:birth",
                owner_id="a" * 32,
            ),
        )
        reader = os.open(path, os.O_RDONLY)
        try:
            payload = json.loads(lifecycle_module._read_pid_fd(reader))
        finally:
            os.close(reader)
        assert payload["pid"] == 4242
        lifecycle_module._unlock_pid_fd(fd)
    finally:
        os.close(fd)

    assert lock_calls == [
        (FakeMsvcrt.LK_NBLCK, lifecycle_module._PID_FILE_LIMIT),
        (FakeMsvcrt.LK_UNLCK, lifecycle_module._PID_FILE_LIMIT),
    ]


def test_pidfile_idempotent_refuses_live(tmp_path: Path) -> None:
    # Simulate a live owner: write our PID then try to acquire again.
    first = PidFile(tmp_path)
    first.acquire()
    try:
        with pytest.raises(StaleLockError):
            PidFile(tmp_path, pid=os.getpid() + 1).acquire()
    finally:
        first.release()


def test_pidfile_reclaims_stale(tmp_path: Path) -> None:
    target = pid_file_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # A PID that cannot be alive on any sane system.
    target.write_text("99999999\n", encoding="utf-8")
    with PidFile(tmp_path) as pf:
        assert read_pid(tmp_path) == pf.pid
    assert not target.exists()


def test_pidfile_reclaims_unlocked_corrupt_record(tmp_path: Path) -> None:
    target = pid_file_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("truncated-json{", encoding="utf-8")

    with PidFile(tmp_path) as owner:
        assert read_pid(tmp_path) == owner.pid
        assert json.loads(target.read_text(encoding="utf-8"))["owner_id"]


def test_pidfile_reclaims_versioned_record_for_reused_live_pid(tmp_path: Path) -> None:
    target = pid_file_path(tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        lifecycle_module._PidRecord(
            pid=os.getpid(),
            start_token="old-process-instance",
            owner_id="stale-owner",
        ).to_json(),
        encoding="utf-8",
    )

    with PidFile(tmp_path) as owner:
        assert read_pid(tmp_path) == owner.pid
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["owner_id"] != "stale-owner"


_CONTENDED_LOCK_SCRIPT = """
import sys, time
from pathlib import Path
from okto_neuron.server.lifecycle import PidFile, StaleLockError

root = Path(sys.argv[1])
gate = Path(sys.argv[2])
winner = Path(sys.argv[3])
deadline = time.monotonic() + 5.0
while not gate.exists() and time.monotonic() < deadline:
    time.sleep(0.005)
try:
    with PidFile(root):
        with winner.open("a", encoding="utf-8") as fh:
            fh.write(str(__import__("os").getpid()) + "\\n")
        time.sleep(0.4)
except StaleLockError:
    raise SystemExit(17)
"""


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-lock contention")
def test_pidfile_atomic_acquisition_allows_exactly_one_process(tmp_path: Path) -> None:
    gate = tmp_path / "gate"
    winner = tmp_path / "winner"
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _CONTENDED_LOCK_SCRIPT,
                str(tmp_path),
                str(gate),
                str(winner),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    gate.write_text("go", encoding="utf-8")
    results = []
    for proc in processes:
        stdout, stderr = proc.communicate(timeout=10.0)
        results.append((proc.returncode, stdout, stderr))

    assert sorted(code for code, _, _ in results) == [0, 17], results
    assert len(winner.read_text(encoding="utf-8").splitlines()) == 1


def test_process_alive_on_windows_uses_windows_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[int] = []

    def fake_windows_process_alive(pid: int) -> bool:
        seen.append(pid)
        return pid == 123

    monkeypatch.setattr(lifecycle_module.os, "name", "nt")
    monkeypatch.setattr(
        lifecycle_module,
        "_windows_process_alive",
        fake_windows_process_alive,
    )

    assert lifecycle_module._process_alive(123) is True
    assert lifecycle_module._process_alive(456) is False
    assert seen == [123, 456]


def test_posix_start_token_forces_stable_c_locale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append({"args": args, **kwargs})
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="Sat Jul 11 08:49:06 2026 /usr/bin/python3\n",
            stderr="",
        )

    monkeypatch.setattr(lifecycle_module.subprocess, "run", fake_run)

    token = lifecycle_module._posix_process_start_token(4242)

    assert token == "posix:Sat Jul 11 08:49:06 2026 /usr/bin/python3"
    environment = calls[0]["env"]
    assert isinstance(environment, dict)
    assert environment["LC_ALL"] == "C"
    assert environment["LANG"] == "C"


def test_read_pid_missing_returns_none(tmp_path: Path) -> None:
    assert read_pid(tmp_path) is None


def test_read_pid_corrupt_returns_none(tmp_path: Path) -> None:
    p = pid_file_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("not-a-pid\n", encoding="utf-8")
    assert read_pid(tmp_path) is None


def test_json_log_envelope_fields(tmp_path: Path) -> None:
    buf = io.StringIO()
    logger = configure_logging(tmp_path, stream=buf, logger_name="marginalia.test_envelope")
    logger.info(
        "hello",
        extra={"component": "server", "event": "test.event", "request_id": "r1"},
    )
    line = buf.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    # Canonical key order.
    assert list(payload.keys())[:7] == [
        "ts",
        "level",
        "component",
        "vault",
        "request_id",
        "event",
        "msg",
    ]
    assert payload["level"] == "info"
    assert payload["component"] == "server"
    assert payload["vault"] == str(tmp_path)
    assert payload["request_id"] == "r1"
    assert payload["event"] == "test.event"
    assert payload["msg"] == "hello"


def test_json_formatter_handles_missing_optional() -> None:
    formatter = JsonLogFormatter(vault=None)
    record = logging.LogRecord(
        name="marginalia",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="x",
        args=(),
        exc_info=None,
    )
    payload = json.loads(formatter.format(record))
    assert payload["vault"] is None
    assert payload["event"] is None
    assert payload["request_id"] is None
    assert payload["level"] == "warning"


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    buf = io.StringIO()
    logger = configure_logging(tmp_path, stream=buf, logger_name="marginalia.test_idempotent")
    configure_logging(tmp_path, stream=buf, logger_name="marginalia.test_idempotent")
    logger.info("once", extra={"component": "c", "event": "e"})
    assert len(buf.getvalue().strip().splitlines()) == 1


def test_configure_logging_log_file_rotating(tmp_path: Path) -> None:
    import logging.handlers

    log_path = tmp_path / "logs" / "serve.log"
    logger = configure_logging(tmp_path, log_file=log_path, logger_name="marginalia.test_logfile")
    handlers = [h for h in logger.handlers if getattr(h, "_okto_neuron_json", False)]
    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.handlers.RotatingFileHandler)
    logger.info("to file", extra={"component": "c", "event": "e"})
    for h in handlers:
        h.flush()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["msg"] == "to file"
    # Parent dir was created on demand.
    assert log_path.parent.is_dir()


def test_configure_logging_log_file_devnull_no_rotation(tmp_path: Path) -> None:
    import logging as _logging
    import logging.handlers

    logger = configure_logging(
        tmp_path, log_file=Path("/dev/null"), logger_name="marginalia.test_devnull"
    )
    handlers = [h for h in logger.handlers if getattr(h, "_okto_neuron_json", False)]
    assert len(handlers) == 1
    assert isinstance(handlers[0], _logging.FileHandler)
    assert not isinstance(handlers[0], logging.handlers.RotatingFileHandler)
    logger.info("discarded", extra={"component": "c", "event": "e"})


def test_default_daemon_log_path_under_home() -> None:
    from okto_neuron.server.lifecycle import default_daemon_log_path

    p = default_daemon_log_path()
    assert p == Path.home() / ".okto-neuron" / "logs" / "okto-neuron-serve.log"


def test_application_daemon_lock_is_never_vault_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron import cli as cli_module

    monkeypatch.setattr(cli_module, "default_app_home", lambda: tmp_path / "home")

    assert cli_module._server_lock_root() == tmp_path / "home" / "runtime"
    assert (
        cli_module._server_lock_root(
            explicit_vault=True,
            vault_path=tmp_path / "vault",
        )
        == tmp_path / "home" / "runtime"
    )


def test_windows_daemon_child_args_remove_daemon_flag() -> None:
    args = lifecycle_module._daemon_child_args(
        ["--debug", "serve", "--daemon", "--vault", "mynotes"]
    )

    assert args == [
        sys.executable,
        "-m",
        "okto_neuron.cli",
        "--debug",
        "serve",
        "--vault",
        "mynotes",
        "--no-open",
    ]


def test_daemonize_on_windows_delegates_to_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Path | None]] = []

    def fake_spawn(*, stdout: Path | None = None, stderr: Path | None = None) -> int:
        calls.append({"stdout": stdout, "stderr": stderr})
        return 4242

    monkeypatch.setattr(lifecycle_module.os, "name", "nt")
    monkeypatch.setattr(lifecycle_module, "_spawn_windows_daemon", fake_spawn)

    assert daemonize(stdout=Path("serve.log"), stderr=Path("serve.err")) == 4242
    assert calls == [{"stdout": Path("serve.log"), "stderr": Path("serve.err")}]


def test_spawn_windows_daemon_spawns_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakePopen:
        pid = 4343

        def __init__(self, args: list[str], **kwargs: object) -> None:
            calls.append({"args": args, **kwargs})

    monkeypatch.setattr(
        lifecycle_module.sys,
        "argv",
        ["marginalia", "serve", "--daemon", "--vault", "mynotes"],
    )
    monkeypatch.setattr(lifecycle_module.subprocess, "Popen", FakePopen)

    child_pid = lifecycle_module._spawn_windows_daemon(
        stdout=tmp_path / "serve.log",
        stderr=tmp_path / "serve.log",
    )

    assert child_pid == 4343
    assert len(calls) == 1
    assert calls[0]["args"] == [
        sys.executable,
        "-m",
        "okto_neuron.cli",
        "serve",
        "--vault",
        "mynotes",
        "--no-open",
    ]
    assert calls[0]["stderr"] is lifecycle_module.subprocess.STDOUT
    assert calls[0]["close_fds"] is True


def test_send_stop_missing_pidfile(tmp_path: Path) -> None:
    with pytest.raises(LifecycleError):
        send_stop(tmp_path)


def test_send_stop_stale_pidfile_cleans_up(tmp_path: Path) -> None:
    p = pid_file_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("99999999\n", encoding="utf-8")
    with pytest.raises(LifecycleError):
        send_stop(tmp_path)
    assert not p.exists()


def test_send_stop_never_signals_unlocked_reused_pid(tmp_path: Path) -> None:
    p = pid_file_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        lifecycle_module._PidRecord(
            pid=os.getpid(),
            start_token="previous-process-birth",
            owner_id="previous-owner",
        ).to_json(),
        encoding="utf-8",
    )

    with pytest.raises(LifecycleError, match="stale PID file"):
        send_stop(tmp_path)
    assert not p.exists()
    assert not lifecycle_module.signal_file_path(tmp_path).exists()


def test_send_stop_targets_owner_watcher_not_numeric_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivered: list[int] = []
    monkeypatch.setattr(
        lifecycle_module.signal,
        "raise_signal",
        lambda sig: delivered.append(sig),
    )

    def forbidden_kill(pid: int, sig: int) -> None:  # pragma: no cover - must not run
        raise AssertionError(f"external numeric PID signal: pid={pid} sig={sig}")

    monkeypatch.setattr(lifecycle_module.os, "kill", forbidden_kill)
    with PidFile(tmp_path):
        assert send_stop(tmp_path) == os.getpid()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not delivered:
            time.sleep(0.01)

    assert delivered == [signal.SIGTERM]


def test_send_stop_expected_pid_mismatch_never_reaches_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivered: list[int] = []
    monkeypatch.setattr(
        lifecycle_module.signal,
        "raise_signal",
        lambda sig: delivered.append(sig),
    )
    with PidFile(tmp_path):
        with pytest.raises(LifecycleError, match="owner changed"):
            send_stop(tmp_path, expected_pid=os.getpid() + 1)
        time.sleep(0.1)

    assert delivered == []


def test_send_stop_refuses_live_legacy_pid_without_marginalia_proof(tmp_path: Path) -> None:
    p = pid_file_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{os.getpid()}\n", encoding="utf-8")

    with pytest.raises(LifecycleError, match="cannot be proven"):
        send_stop(tmp_path)
    assert p.exists()
    assert not lifecycle_module.signal_file_path(tmp_path).exists()


@pytest.mark.parametrize(
    ("command", "expected_port"),
    [
        ([sys.executable, "-m", "okto_neuron.cli", "serve"], 7777),
        (
            [sys.executable, "/venv/bin/marginalia", "serve", "--port", "7791"],
            7791,
        ),
        ([r"C:\\Tools\\marginalia.exe", "serve", "--port=7792"], 7792),
        ([sys.executable, "/venv/bin/marginalia", "status"], None),
        ([sys.executable, "/venv/bin/not-marginalia", "serve"], None),
    ],
)
def test_legacy_serve_port_recognizes_supported_entrypoints(
    command: list[str], expected_port: int | None
) -> None:
    assert lifecycle_module._legacy_serve_port(command) == expected_port


def test_send_stop_migrates_proven_legacy_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = pid_file_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("4242\n", encoding="utf-8")
    sent: list[tuple[int, int]] = []

    monkeypatch.setattr(lifecycle_module, "_process_alive", lambda pid: pid == 4242)
    monkeypatch.setattr(lifecycle_module, "_process_start_token", lambda pid: "birth-1")
    monkeypatch.setattr(
        lifecycle_module,
        "_process_command",
        lambda pid: [
            sys.executable,
            "-m",
            "okto_neuron.cli",
            "serve",
            "--vault",
            str(tmp_path),
        ],
    )

    def fake_http(port: int, path: str):  # type: ignore[no-untyped-def]
        if path == "/health":
            return 200, {"pid": 4242, "vault_path": str(tmp_path)}
        return 200, {"marginalia_version": "0.0.39"}

    monkeypatch.setattr(lifecycle_module, "_legacy_http_json", fake_http)
    monkeypatch.setattr(
        lifecycle_module.os,
        "kill",
        lambda pid, sig: sent.append((pid, sig)),
    )

    assert send_stop(tmp_path) == 4242
    assert sent == [(4242, signal.SIGTERM)]


def test_send_stop_legacy_migration_requires_exact_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    p = pid_file_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("4242\n", encoding="utf-8")
    sent: list[tuple[int, int]] = []

    monkeypatch.setattr(lifecycle_module, "_process_alive", lambda pid: True)
    monkeypatch.setattr(lifecycle_module, "_process_start_token", lambda pid: "birth-1")
    monkeypatch.setattr(
        lifecycle_module,
        "_process_command",
        lambda pid: [sys.executable, "-m", "okto_neuron.cli", "serve"],
    )
    monkeypatch.setattr(
        lifecycle_module,
        "_legacy_http_json",
        lambda port, path: (
            (200, {"pid": 4242, "vault_path": str(tmp_path / "other")})
            if path == "/health"
            else (200, {"marginalia_version": "0.0.39"})
        ),
    )
    monkeypatch.setattr(
        lifecycle_module.os,
        "kill",
        lambda pid, sig: sent.append((pid, sig)),
    )

    with pytest.raises(LifecycleError, match="different vault"):
        send_stop(tmp_path)
    assert sent == []


def test_stop_server_never_escalates_on_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One request, then only polling: no second request is sent within --timeout (#22)."""
    target = lifecycle_module._LegacyTarget(pid=4242, start_token="birth-1")
    requested: list[tuple[int, bool]] = []
    started = time.monotonic()

    def fake_request(vault, *, sig, expected=None, force=False, drain_timeout=None):  # type: ignore[no-untyped-def]
        requested.append((sig, force))
        return target

    monkeypatch.setattr(lifecycle_module, "_request_stop_target", fake_request)
    monkeypatch.setattr(lifecycle_module, "_STOP_EXIT_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(lifecycle_module, "_MIN_CLOSE_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(
        lifecycle_module,
        "_legacy_target_still_same",
        lambda vault, current: time.monotonic() - started < 0.4,
    )

    assert stop_server(tmp_path, timeout=2.0, poll_interval=0.01) == 4242
    assert requested == [(signal.SIGTERM, False)]


def test_stop_server_times_out_without_escalating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = lifecycle_module._PidRecord(
        pid=99999, start_token="posix:fixture", owner_id="owner-fixture"
    )
    requested: list[tuple[int, bool, float | None]] = []

    def fake_request(vault, *, sig, expected=None, force=False, drain_timeout=None):  # type: ignore[no-untyped-def]
        requested.append((sig, force, drain_timeout))
        return target

    monkeypatch.setattr(lifecycle_module, "_request_stop_target", fake_request)
    monkeypatch.setattr(lifecycle_module, "_STOP_EXIT_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(lifecycle_module, "_MIN_CLOSE_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(
        lifecycle_module, "_active_pid_record", lambda vault, **kw: target
    )

    with pytest.raises(LifecycleError, match="did not exit"):
        stop_server(tmp_path, timeout=0.3, poll_interval=0.01)
    # The daemon was asked once, carrying --timeout as its drain budget.
    assert requested == [(signal.SIGTERM, False, 0.3)]


def test_stop_server_force_sends_the_force_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = lifecycle_module._PidRecord(
        pid=99999, start_token="posix:fixture", owner_id="owner-fixture"
    )
    requested: list[tuple[int, bool]] = []

    def fake_request(vault, *, sig, expected=None, force=False, drain_timeout=None):  # type: ignore[no-untyped-def]
        requested.append((sig, force))
        return target

    def gone(vault, **kw):  # type: ignore[no-untyped-def]
        raise lifecycle_module._OwnerGone("exited")

    monkeypatch.setattr(lifecycle_module, "_request_stop_target", fake_request)
    monkeypatch.setattr(lifecycle_module, "_active_pid_record", gone)

    assert stop_server(tmp_path, timeout=1.0, poll_interval=0.01, force=True) == 99999
    assert requested == [(signal.SIGTERM, True)]


def test_stop_server_force_kills_a_legacy_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = lifecycle_module._LegacyTarget(pid=4242, start_token="birth-1")
    sent: list[int] = []
    monkeypatch.setattr(lifecycle_module, "_active_pid_record", lambda *a, **k: (_ for _ in ()).throw(
        lifecycle_module._LegacyOwner(4242)))
    monkeypatch.setattr(lifecycle_module, "_validate_legacy_target", lambda vault, pid: target)
    monkeypatch.setattr(
        lifecycle_module, "_signal_legacy_target", lambda vault, tgt, sig: sent.append(sig)
    )
    monkeypatch.setattr(lifecycle_module, "_legacy_target_still_same", lambda vault, cur: False)

    assert stop_server(tmp_path, timeout=1.0, poll_interval=0.01, force=True) == 4242
    assert sent == [signal.SIGKILL]


def test_send_stop_refuses_locked_identity_mismatch(tmp_path: Path) -> None:
    owner = PidFile(tmp_path)
    owner.acquire()
    try:
        payload = json.loads(pid_file_path(tmp_path).read_text(encoding="utf-8"))
        payload["start_token"] = "different-process-birth"
        pid_file_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")

        with pytest.raises(LifecycleError, match="PID identity mismatch"):
            send_stop(tmp_path)
        assert not lifecycle_module.signal_file_path(tmp_path).exists()
    finally:
        owner.release()


def test_send_stop_retries_temporarily_unavailable_process_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lifecycle_module,
        "_process_start_token",
        lambda _pid: "recorded-process-birth",
    )
    owner = PidFile(tmp_path, pid=4242)
    owner.acquire()
    try:
        payload = json.loads(pid_file_path(tmp_path).read_text(encoding="utf-8"))
        reads: list[str | None] = [None, payload["start_token"]]
        monkeypatch.setattr(
            lifecycle_module,
            "_process_start_token",
            lambda _pid: reads.pop(0),
        )
        monkeypatch.setattr(lifecycle_module, "_PROCESS_START_TOKEN_RETRY_SECONDS", 0)

        assert send_stop(tmp_path) == 4242
        assert reads == []
        assert lifecycle_module.signal_file_path(tmp_path).exists()
    finally:
        owner.release()


def test_send_stop_refuses_persistently_unavailable_process_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lifecycle_module,
        "_process_start_token",
        lambda _pid: "recorded-process-birth",
    )
    owner = PidFile(tmp_path, pid=4242)
    owner.acquire()
    try:
        reads: list[int] = []
        monkeypatch.setattr(
            lifecycle_module,
            "_process_start_token",
            lambda pid: reads.append(pid),
        )
        monkeypatch.setattr(lifecycle_module, "_PROCESS_START_TOKEN_RETRY_SECONDS", 0)

        with pytest.raises(LifecycleError, match="PID identity mismatch"):
            send_stop(tmp_path)

        assert reads == [4242] * lifecycle_module._PROCESS_START_TOKEN_ATTEMPTS
        assert not lifecycle_module.signal_file_path(tmp_path).exists()
    finally:
        owner.release()


def test_send_stop_never_retries_conflicting_process_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        lifecycle_module,
        "_process_start_token",
        lambda _pid: "recorded-process-birth",
    )
    owner = PidFile(tmp_path, pid=4242)
    owner.acquire()
    try:
        payload = json.loads(pid_file_path(tmp_path).read_text(encoding="utf-8"))
        reads = iter(["different-process-birth", payload["start_token"]])
        monkeypatch.setattr(
            lifecycle_module,
            "_process_start_token",
            lambda _pid: next(reads),
        )

        with pytest.raises(LifecycleError, match="PID identity mismatch"):
            send_stop(tmp_path)

        assert next(reads) == payload["start_token"]
        assert not lifecycle_module.signal_file_path(tmp_path).exists()
    finally:
        owner.release()


def test_expected_owner_release_during_identity_read_is_owner_gone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = PidFile(tmp_path)
    owner.acquire()
    try:
        payload = json.loads(pid_file_path(tmp_path).read_text(encoding="utf-8"))

        def release_during_identity_read(_pid: int) -> None:
            owner.release()
            return None

        monkeypatch.setattr(
            lifecycle_module,
            "_process_start_token_with_retry",
            release_during_identity_read,
        )

        with pytest.raises(lifecycle_module._OwnerGone, match="identity validation"):
            lifecycle_module._active_pid_record(
                tmp_path,
                expected_owner_id=payload["owner_id"],
                expected_start_token=payload["start_token"],
            )

        assert not pid_file_path(tmp_path).exists()
    finally:
        owner.release()


def test_expected_locked_owner_with_unavailable_identity_remains_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = PidFile(tmp_path)
    owner.acquire()
    try:
        payload = json.loads(pid_file_path(tmp_path).read_text(encoding="utf-8"))
        monkeypatch.setattr(
            lifecycle_module,
            "_process_start_token_with_retry",
            lambda _pid: None,
        )

        with pytest.raises(LifecycleError, match="PID identity mismatch"):
            lifecycle_module._active_pid_record(
                tmp_path,
                expected_owner_id=payload["owner_id"],
                expected_start_token=payload["start_token"],
            )

        assert pid_file_path(tmp_path).exists()
    finally:
        owner.release()


def test_stop_server_tolerates_transient_identity_check_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A momentary inability to re-verify the signalled owner's identity mid-poll
    must not abort ``stop_server`` outright.

    ``_active_pid_record`` fails closed with ``_IdentityUnverifiable`` (a
    ``LifecycleError`` subclass) when a single process-birth read is
    transiently unavailable while the owner still holds the lock (see
    ``test_expected_locked_owner_with_unavailable_identity_remains_fail_closed``
    for why that fail-closed behaviour is correct there). Under a loaded full
    test-suite run this transient state can legitimately occur once during
    ``stop_server``'s poll loop even though the signalled owner is still alive
    and about to exit; only proof the owner is actually gone
    (``_OwnerGone``/``_OwnerChanged``) should end the wait early, and only the
    deadline should raise. Regression for the flaky
    ``test_stop_server_escalates_with_repeat_signal`` failure ("PID identity
    mismatch ... refusing to signal an unrelated or unverifiable process")
    observed under a loaded full-suite run.
    """
    target = lifecycle_module._PidRecord(
        pid=99999, start_token="posix:fixture", owner_id="owner-fixture"
    )

    monkeypatch.setattr(
        lifecycle_module,
        "_request_stop_target",
        lambda vault, *, sig, expected=None, force=False, drain_timeout=None: target,
    )

    calls = {"n": 0}

    def flaky_active_pid_record(vault, *, expected_owner_id=None, expected_start_token=None):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 1:
            raise lifecycle_module._IdentityUnverifiable(
                f"PID identity mismatch for {target.pid}; refusing to signal an "
                "unrelated or unverifiable process"
            )
        raise lifecycle_module._OwnerGone("owner exited")

    monkeypatch.setattr(lifecycle_module, "_active_pid_record", flaky_active_pid_record)

    assert stop_server(tmp_path, timeout=2.0, poll_interval=0.01) == target.pid
    assert calls["n"] >= 2


def test_stop_server_keeps_polling_through_an_identity_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PID identity mismatch`` while the owner still holds the lock is not an error.

    #22: a stop that had in fact succeeded printed the mismatch and exited 1.
    The birth token reading differently mid-teardown must keep the poll going
    until the owner is gone.
    """
    target = lifecycle_module._PidRecord(
        pid=99999, start_token="posix:fixture", owner_id="owner-fixture"
    )
    monkeypatch.setattr(
        lifecycle_module,
        "_request_stop_target",
        lambda vault, *, sig, expected=None, force=False, drain_timeout=None: target,
    )
    calls = {"n": 0}

    def mismatch_then_gone(vault, *, expected_owner_id=None, expected_start_token=None):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] < 4:
            raise lifecycle_module._IdentityMismatch(
                f"PID identity mismatch for {target.pid}; refusing to signal an "
                "unrelated or unverifiable process"
            )
        raise lifecycle_module._OwnerGone("owner exited")

    monkeypatch.setattr(lifecycle_module, "_active_pid_record", mismatch_then_gone)

    assert stop_server(tmp_path, timeout=2.0, poll_interval=0.01) == target.pid
    assert calls["n"] == 4


def test_stop_server_still_raises_on_a_corrupt_pid_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = lifecycle_module._PidRecord(
        pid=99999, start_token="posix:fixture", owner_id="owner-fixture"
    )
    monkeypatch.setattr(
        lifecycle_module,
        "_request_stop_target",
        lambda vault, *, sig, expected=None, force=False, drain_timeout=None: target,
    )

    def corrupt(vault, *, expected_owner_id=None, expected_start_token=None):  # type: ignore[no-untyped-def]
        raise LifecycleError("daemon lock is held, but its identity record is missing or corrupt")

    monkeypatch.setattr(lifecycle_module, "_active_pid_record", corrupt)

    with pytest.raises(LifecycleError, match="missing or corrupt"):
        stop_server(tmp_path, timeout=2.0, poll_interval=0.01)


def test_send_stop_migrates_localized_versioned_birth_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner = PidFile(tmp_path)
    owner.acquire()
    try:
        path = pid_file_path(tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["start_token"] = "posix:sáb 11 jul 08:49:06 2026 /usr/bin/python3"
        path.write_text(json.dumps(payload), encoding="utf-8")

        monkeypatch.setattr(
            lifecycle_module,
            "_process_start_token",
            lambda pid: "posix:Sat Jul 11 08:49:06 2026 /usr/bin/python3",
        )
        monkeypatch.setattr(
            lifecycle_module,
            "_process_command",
            lambda pid: [sys.executable, "-m", "okto_neuron.cli", "serve"],
        )

        assert send_stop(tmp_path) == os.getpid()
        request = json.loads(
            lifecycle_module.signal_file_path(tmp_path).read_text(encoding="utf-8")
        )
        assert request["owner_id"] == payload["owner_id"]
        assert request["start_token"].startswith("posix:Sat Jul 11")
    finally:
        owner.release()


_CHILD_SCRIPT = """
import os, signal, sys, time
from pathlib import Path
from okto_neuron.server.lifecycle import PidFile

vault = Path(sys.argv[1])
ready = Path(sys.argv[2])
flag = {"v": False}
def handle(signum, frame):
    flag["v"] = True
signal.signal(signal.SIGTERM, handle)
with PidFile(vault):
    ready.write_text("ready")
    deadline = time.monotonic() + 30.0
    while not flag["v"] and time.monotonic() < deadline:
        time.sleep(0.05)
sys.exit(0)
"""


_CHILD_REQUIRES_REPEAT_SCRIPT = """
import signal, sys, time
from pathlib import Path
from okto_neuron.server.lifecycle import PidFile

vault = Path(sys.argv[1])
ready = Path(sys.argv[2])
signals = {"count": 0}
def handle(signum, frame):
    signals["count"] += 1
    ready.with_suffix(".count").write_text(str(signals["count"]))
    if signals["count"] >= 2:
        raise SystemExit(0)
signal.signal(signal.SIGTERM, handle)
with PidFile(vault):
    ready.write_text("ready")
    while True:
        time.sleep(0.05)
"""


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only signal semantics")
def test_stop_server_signals_and_waits(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    script_path = tmp_path / "child.py"
    script_path.write_text(_CHILD_SCRIPT, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script_path), str(tmp_path), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.05)
        assert ready.exists(), "child did not signal readiness"
        recorded = read_pid(tmp_path)
        assert recorded == proc.pid

        signalled = stop_server(tmp_path, timeout=10.0)
        assert signalled == proc.pid
        try:
            rc = proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
            raise
        assert rc == 0
        assert not pid_file_path(tmp_path).exists()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only signal semantics")
def test_stop_server_repeat_requests_are_not_delivered_but_force_is(tmp_path: Path) -> None:
    """The watcher delivers the first request and forced ones, never a repeat (#22)."""
    import threading

    ready = tmp_path / "ready"
    count_file = ready.with_suffix(".count")
    script_path = tmp_path / "child-repeat.py"
    script_path.write_text(_CHILD_REQUIRES_REPEAT_SCRIPT, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, str(script_path), str(tmp_path), str(ready)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.05)
        assert ready.exists(), "child did not signal readiness"

        graceful: dict[str, object] = {}

        def run_graceful() -> None:
            try:
                graceful["pid"] = stop_server(tmp_path, timeout=30.0, poll_interval=0.02)
            except BaseException as exc:  # noqa: BLE001
                graceful["error"] = exc

        waiter = threading.Thread(target=run_graceful)
        waiter.start()
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline and not count_file.exists():
            time.sleep(0.05)
        assert count_file.read_text() == "1"

        # Another graceful request (as a second `stop` would send) is not a
        # second signal: the child still sees exactly one.
        send_stop(tmp_path)
        time.sleep(0.5)
        assert count_file.read_text() == "1"
        assert proc.poll() is None

        # --force is delivered, and is the second signal the child needs.
        assert stop_server(tmp_path, timeout=5.0, poll_interval=0.02, force=True) == proc.pid
        assert proc.wait(timeout=5.0) == 0
        waiter.join(timeout=5.0)
        assert graceful.get("pid") == proc.pid
        assert not pid_file_path(tmp_path).exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)


def test_cli_stop_without_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from okto_neuron import cli as cli_module
    from okto_neuron.cli import app

    monkeypatch.setattr(cli_module, "default_app_home", lambda: tmp_path / "home")

    runner = CliRunner()
    result = runner.invoke(app, ["stop", "--vault", str(tmp_path)])
    assert result.exit_code != 0
    combined = result.output or ""
    assert "no okto neuron server" in combined.lower() or "stale" in combined.lower()


def test_cli_stop_without_vault_discovers_the_only_vault_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron import Vault
    from okto_neuron import cli as cli_module

    vault_path = tmp_path / "vault"
    vault = Vault.init(vault_path, embedding_provider="stub")
    vault.close()
    monkeypatch.setattr(cli_module, "default_app_home", lambda: tmp_path / "home")
    monkeypatch.setattr(cli_module, "resolve_vault_reference", lambda _value: vault_path)
    monkeypatch.setattr(cli_module, "list_vaults", lambda **_kwargs: [])

    with PidFile(vault_path):
        assert cli_module._discover_stop_root() == vault_path.resolve(strict=False)


def test_cli_stop_discovery_ignores_stale_legacy_pid_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron import cli as cli_module

    runtime_root = tmp_path / "home" / "runtime"
    stale_vault = tmp_path / "stale-vault"
    stale_pid = pid_file_path(stale_vault)
    stale_pid.parent.mkdir(parents=True)
    stale_pid.write_text("99999999\n", encoding="utf-8")

    monkeypatch.setattr(cli_module, "default_app_home", lambda: tmp_path / "home")
    monkeypatch.setattr(cli_module, "resolve_vault_reference", lambda _value: stale_vault)
    monkeypatch.setattr(cli_module, "is_vault", lambda path: path == stale_vault)
    monkeypatch.setattr(cli_module, "list_vaults", lambda **_kwargs: [])

    with PidFile(runtime_root):
        assert cli_module._discover_stop_root() == runtime_root.resolve(strict=False)
    assert not stale_pid.exists()


def test_cli_stop_discovery_finds_a_live_pre_rename_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 0.2.0 daemon holds ~/.marginalia/runtime; bare `stop` must find it."""
    from okto_neuron import cli as cli_module

    legacy_root = tmp_path / "legacy-home" / "runtime"
    monkeypatch.setattr(cli_module, "default_app_home", lambda: tmp_path / "home")
    monkeypatch.setattr(cli_module, "legacy_runtime_root", lambda: legacy_root)
    monkeypatch.setattr(cli_module, "resolve_vault_reference", lambda _value: tmp_path / "none")
    monkeypatch.setattr(cli_module, "is_vault", lambda _path: False)
    monkeypatch.setattr(cli_module, "list_vaults", lambda **_kwargs: [])

    with PidFile(legacy_root):
        assert cli_module._discover_stop_root() == legacy_root.resolve(strict=False)


def test_cli_serve_refuses_while_a_pre_rename_daemon_holds_the_legacy_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from okto_neuron import cli as cli_module
    from okto_neuron.cli import app

    legacy_root = tmp_path / "legacy-home" / "runtime"
    new_root = tmp_path / "home"
    monkeypatch.setattr(cli_module, "default_app_home", lambda: new_root)
    monkeypatch.setattr(cli_module, "legacy_runtime_root", lambda: legacy_root)

    with PidFile(legacy_root) as legacy:
        result = CliRunner().invoke(app, ["serve", "--port", "0", "--mcp-port", "0"])
    assert result.exit_code == 1, result.output
    assert f"pid={legacy.pid}" in result.output
    assert "okto-neuron stop" in result.output
    assert not pid_file_path(new_root / "runtime").exists()


def test_cli_daemon_prints_open_command_without_secret_and_exact_stop_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from okto_neuron import Vault
    from okto_neuron import cli as cli_module
    from okto_neuron.cli import app

    vault_path = tmp_path / "vault with spaces"
    vault = Vault.init(vault_path, embedding_provider="stub")
    vault.close()
    ready_calls: list[tuple[str, float, int | None, Path | None]] = []
    browser_calls: list[str] = []

    monkeypatch.setattr(lifecycle_module, "daemonize", lambda **kwargs: 5151)
    monkeypatch.setattr(cli_module, "default_app_home", lambda: tmp_path / "home")

    def fake_ready(
        endpoint: str,
        *,
        timeout: float,
        expected_pid: int | None = None,
        vault_path: Path | None = None,
    ) -> bool:
        ready_calls.append((endpoint, timeout, expected_pid, vault_path))
        return True

    monkeypatch.setattr(cli_module, "_wait_for_server_health", fake_ready)
    monkeypatch.setattr(
        cli_module,
        "_open_ui_in_browser",
        lambda endpoint: browser_calls.append(endpoint) or True,
    )

    result = CliRunner().invoke(
        app,
        [
            "serve",
            "--daemon",
            "--vault",
            str(vault_path),
            "--port",
            "7791",
        ],
    )

    assert result.exit_code == 0, result.output
    assert ready_calls == [("http://127.0.0.1:7791", 60.0, 5151, vault_path)]
    assert "Okto Neuron is running in the background (pid=5151)" in result.output
    assert "token%2B%2F%3D" not in result.output
    assert "?token=" not in result.output
    assert "Stop: okto-neuron stop" in result.output
    assert "UI: http://127.0.0.1:7791/" in result.output
    assert browser_calls == ["http://127.0.0.1:7791"]
    assert "okto-neuron status --endpoint http://127.0.0.1:7791" in result.output
    assert "status --endpoint http://127.0.0.1:7791 --vault" not in result.output


def test_windows_daemon_readiness_uses_lock_owner_not_venv_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron import cli as cli_module

    calls: list[tuple[int | None, float]] = []

    def fake_ready(
        _endpoint: str,
        *,
        timeout: float,
        expected_pid: int | None = None,
        vault_path: Path | None = None,
    ) -> bool:
        del vault_path
        calls.append((expected_pid, timeout))
        return True

    monkeypatch.setattr(cli_module.os, "name", "nt")
    monkeypatch.setattr(cli_module, "_wait_for_server_health", fake_ready)
    monkeypatch.setattr(lifecycle_module, "active_server_pid", lambda _root: 6161)

    assert (
        cli_module._wait_for_daemon_owner(
            "http://127.0.0.1:7777",
            tmp_path,
            5151,
            timeout=15.0,
            vault_path=None,
        )
        == 6161
    )
    assert calls == [(None, 15.0), (6161, 2.0)]


def test_daemon_readiness_failure_never_stops_a_discovered_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from okto_neuron import cli as cli_module
    from okto_neuron.cli import app

    class PreflightPidFile:
        def __init__(self, _root: Path, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> "PreflightPidFile":
            return self

        def release(self) -> None:
            pass

        def __exit__(self, *_args: object) -> None:
            return None

    stop_calls: list[int | None] = []

    monkeypatch.setattr(lifecycle_module, "PidFile", PreflightPidFile)
    monkeypatch.setattr(lifecycle_module, "daemonize", lambda **_kwargs: 5151)
    monkeypatch.setattr(lifecycle_module, "active_server_pid", lambda _root: 6161)
    monkeypatch.setattr(
        lifecycle_module,
        "send_stop",
        lambda _root, **kwargs: stop_calls.append(kwargs.get("expected_pid")) or 5151,
    )
    monkeypatch.setattr(cli_module, "default_app_home", lambda: tmp_path / "home")
    monkeypatch.setattr(
        lifecycle_module,
        "default_daemon_log_path",
        lambda: tmp_path / "serve.log",
    )
    monkeypatch.setattr(cli_module, "_wait_for_daemon_owner", lambda *_args, **_kwargs: None)
    # active_server_pid is faked for every root, so keep the pre-rename daemon
    # guard (which also asks it) out of this readiness scenario.
    monkeypatch.setattr(cli_module, "_refuse_if_legacy_daemon_running", lambda: None)

    result = CliRunner().invoke(app, ["serve", "--daemon", "--no-open"])

    assert result.exit_code != 0
    assert stop_calls == [5151]
    assert "daemon spawned from pid 5151 did not become ready" in result.output


@pytest.mark.parametrize("mode", ["daemon", "foreground"])
def test_cli_existing_server_opens_and_reports_plain_ui_url(
    mode: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from okto_neuron import cli as cli_module
    from okto_neuron.cli import app

    class RejectingPidFile:
        def __init__(self, root: Path, **_kwargs: object) -> None:
            self.path = pid_file_path(root)

        def __enter__(self) -> None:
            raise StaleLockError(5151, self.path)

        def __exit__(self, *_args: object) -> None:
            return None

    browser_calls: list[str] = []
    readiness_calls: list[tuple[str, float]] = []

    def fake_ready(endpoint: str, *, timeout: float, **_kwargs: object) -> bool:
        readiness_calls.append((endpoint, timeout))
        return True

    monkeypatch.setattr(lifecycle_module, "PidFile", RejectingPidFile)
    monkeypatch.setattr(cli_module, "default_app_home", lambda: tmp_path / "home")
    monkeypatch.setattr(cli_module, "_wait_for_server_health", fake_ready)
    monkeypatch.setattr(
        cli_module,
        "_open_ui_in_browser",
        lambda endpoint: browser_calls.append(endpoint) or True,
    )

    args = ["serve", "--port", "7792"]
    if mode == "daemon":
        args.append("--daemon")
    result = CliRunner().invoke(app, args)

    assert result.exit_code == 1, result.output
    assert readiness_calls == [("http://127.0.0.1:7792", 2.0)]
    assert browser_calls == ["http://127.0.0.1:7792"]
    assert "UI: http://127.0.0.1:7792/" in result.output
    assert "okto-neuron ui --endpoint" not in result.output


def test_cli_foreground_serve_schedules_plain_browser_url_after_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from okto_neuron import Vault
    from okto_neuron import cli as cli_module
    from okto_neuron.cli import app
    from okto_neuron.server import runtime as runtime_module

    vault_path = tmp_path / "vault"
    vault = Vault.init(vault_path, embedding_provider="stub")
    vault.close()
    launches: list[tuple[str, int, Path | None]] = []

    monkeypatch.setattr(runtime_module, "run", lambda **_kwargs: None)
    monkeypatch.setattr(cli_module, "default_app_home", lambda: tmp_path / "home")
    monkeypatch.setattr(
        cli_module,
        "_start_ui_browser_thread",
        lambda endpoint, *, expected_pid, vault_path: launches.append(
            (endpoint, expected_pid, vault_path)
        ),
    )

    result = CliRunner().invoke(
        app,
        ["serve", "--vault", str(vault_path), "--port", "7791"],
    )

    assert result.exit_code == 0, result.output
    assert launches == [("http://127.0.0.1:7791", os.getpid(), vault_path)]


def test_cli_foreground_browser_waits_for_full_startup_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron import cli as cli_module

    waits: list[tuple[str, float, int, Path | None]] = []
    browser_calls: list[str] = []

    def fake_ready(
        endpoint: str,
        *,
        timeout: float,
        expected_pid: int,
        vault_path: Path | None,
    ) -> bool:
        waits.append((endpoint, timeout, expected_pid, vault_path))
        return True

    monkeypatch.setattr(cli_module, "_wait_for_server_health", fake_ready)
    monkeypatch.setattr(
        cli_module,
        "_open_ui_in_browser",
        lambda endpoint: browser_calls.append(endpoint) or True,
    )

    cli_module._open_ui_when_ready(
        "http://127.0.0.1:7791",
        expected_pid=5151,
        vault_path=tmp_path,
    )

    assert waits == [("http://127.0.0.1:7791", 60.0, 5151, tmp_path)]
    assert browser_calls == ["http://127.0.0.1:7791"]


def test_serve_without_explicit_vault_starts_application_without_default_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from okto_neuron import cli as cli_module
    from okto_neuron.cli import app
    from okto_neuron.server import runtime as runtime_module

    starts: list[Path | None] = []
    monkeypatch.setattr(cli_module, "default_app_home", lambda: tmp_path / "home")
    monkeypatch.setattr(
        cli_module,
        "resolve_vault_reference",
        lambda _path: pytest.fail("implicit default vault must not be resolved"),
    )
    monkeypatch.setattr(
        runtime_module,
        "run",
        lambda **kwargs: starts.append(kwargs["vault_path"]),
    )
    monkeypatch.setattr(
        cli_module,
        "_start_ui_browser_thread",
        lambda *_args, **_kwargs: pytest.fail("--no-open must not launch a browser"),
    )

    result = CliRunner().invoke(app, ["serve", "--no-open", "--port", "7792"])

    assert result.exit_code == 0, result.output
    assert starts == [None]


def test_cli_ui_opens_plain_loopback_url(monkeypatch: pytest.MonkeyPatch) -> None:
    import webbrowser

    from click.testing import CliRunner

    from okto_neuron import cli as cli_module
    from okto_neuron.cli import app

    opened: list[str] = []
    monkeypatch.setattr(cli_module, "_wait_for_server_health", lambda *args, **kwargs: True)
    monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

    result = CliRunner().invoke(app, ["ui"])

    assert result.exit_code == 0, result.output
    assert "Opened Okto Neuron at http://127.0.0.1:7777/" in result.output
    assert opened == ["http://127.0.0.1:7777/"]
    assert "token=" not in opened[0]


def test_cli_ui_prints_plain_url_when_browser_launch_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import webbrowser

    from click.testing import CliRunner

    from okto_neuron import cli as cli_module
    from okto_neuron.cli import app

    monkeypatch.setattr(cli_module, "_wait_for_server_health", lambda *args, **kwargs: True)
    monkeypatch.setattr(webbrowser, "open", lambda _url: False)

    result = CliRunner().invoke(app, ["ui"])

    assert result.exit_code == 0, result.output
    assert "Browser did not open automatically; open http://127.0.0.1:7777/" in result.output
    assert "token=" not in result.output


def test_cli_ui_no_open_skips_browser_and_prints_plain_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from okto_neuron import cli as cli_module
    from okto_neuron.cli import app

    monkeypatch.setattr(cli_module, "_wait_for_server_health", lambda *args, **kwargs: True)
    result = CliRunner().invoke(app, ["ui", "--no-open"])

    assert result.exit_code == 0, result.output
    assert "Okto Neuron UI is ready at http://127.0.0.1:7777/" in result.output
    assert "?token=" not in result.output


def test_cli_version_flag_and_command() -> None:
    from click.testing import CliRunner

    from okto_neuron import __version__
    from okto_neuron.cli import app

    runner = CliRunner()
    flag = runner.invoke(app, ["--version"])
    command = runner.invoke(app, ["version"])

    assert flag.exit_code == 0, flag.output
    assert command.exit_code == 0, command.output
    assert __version__ in flag.output
    assert command.output.splitlines() == [f"okto-neuron {__version__}", "Okto Neuron by Okto Labs"]


def test_cli_models_help_and_size_exit_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from okto_neuron.cli import app
    from okto_neuron.cli import models as models_module

    monkeypatch.setattr(models_module, "_dir_size", lambda _path: 0)
    runner = CliRunner()
    help_result = runner.invoke(app, ["models", "--help"])
    size_result = runner.invoke(app, ["models", "size"])

    assert help_result.exit_code == 0, help_result.output
    assert "Manage local model artifacts and caches" in help_result.output
    assert size_result.exit_code == 0, size_result.output
    assert size_result.output.startswith("okto-neuron models size\n")
    assert "Traceback" not in help_result.output + size_result.output


@pytest.mark.parametrize("args", [["add", "--help"], ["query", "--help"], ["rebuild", "--help"]])
def test_kg_compat_cli_flattens_legacy_and_graph_commands(args: list[str]) -> None:
    from click.testing import CliRunner

    from okto_neuron.cli import kg_cli

    result = CliRunner().invoke(kg_cli, args)
    assert result.exit_code == 0, result.output
    assert "Usage: kg-cli kg" not in result.output


def test_cli_status_reports_daemon_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from okto_neuron import cli as cli_module
    from okto_neuron.cli import app

    def fake_request(
        endpoint: str,
        method: str,
        path: str,
        payload: object = None,
        **kwargs: object,
    ) -> dict[str, object]:
        assert endpoint == "http://127.0.0.1:7777"
        assert method == "GET"
        if path == "/version":
            assert kwargs.get("vault") is None
            return {"marginalia_version": "9.8.7", "api_version": "v1"}
        assert path == "/api/v1/status"
        assert kwargs.get("vault") == Path("/tmp/example-vault")
        return {
            "status": "degraded",
            "pid": 4321,
            "vault_path": "/tmp/example-vault",
            "backend": "grafx",
            "active_vault": True,
            "uptime_s": 12.5,
            "queue_error_count": 1,
            "ingest": {
                "total": 3,
                "queued": 1,
                "processing": 1,
                "done": 1,
                "error": 0,
                "cancelled": 0,
                "active": True,
                "cancel_requested": True,
            },
            "degraded_reasons": ["queue_errors: 1 ingest item failed"],
        }

    monkeypatch.setattr(cli_module, "_client_request", fake_request)
    result = CliRunner().invoke(app, ["status", "--vault", "/tmp/example-vault"])

    assert result.exit_code == 0, result.output
    assert "Okto Neuron 9.8.7 — degraded" in result.output
    assert "PID:      4321" in result.output
    assert "Scope:    vault" in result.output
    assert "Backend:  grafx" in result.output
    assert "Ingest:   stopping (1/3 terminal, 1 processing, 1 queued)" in result.output
    assert "queue_errors: 1 ingest item failed" in result.output
    assert "?token=" not in result.output
    assert "UI:       http://127.0.0.1:7777/" in result.output
    assert "okto-neuron ui --endpoint" not in result.output


def test_cli_status_reports_application_scope_without_singular_active_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from okto_neuron import cli as cli_module
    from okto_neuron.cli import app

    def fake_request(
        _endpoint: str,
        _method: str,
        path: str,
        _payload: object = None,
        **_kwargs: object,
    ) -> dict[str, object]:
        if path == "/version":
            return {"marginalia_version": "9.8.7", "api_version": "v1"}
        return {
            "status": "ok",
            "scope": "application",
            "pid": 4321,
            "vault_path": None,
            "active_vault": False,
            "vault_count": 2,
            "vaults": [{"path": "/tmp/a"}, {"path": "/tmp/b"}],
            "ingest": {"total": 0},
            "degraded_reasons": [],
        }

    monkeypatch.setattr(cli_module, "_client_request", fake_request)
    runner = CliRunner()

    result = runner.invoke(app, ["status"])
    help_result = runner.invoke(app, ["status", "--help"])

    assert result.exit_code == 0, result.output
    assert "Scope:    application" in result.output
    assert "Vaults:   2" in result.output
    assert "  Vault:    " not in result.output
    assert "active vault" not in result.output.lower()
    assert help_result.exit_code == 0, help_result.output
    assert "active vault" not in help_result.output.lower()


def test_cli_ui_reports_missing_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from okto_neuron import cli as cli_module
    from okto_neuron.cli import app

    monkeypatch.setattr(cli_module, "_wait_for_server_health", lambda *args, **kwargs: False)

    result = CliRunner().invoke(app, ["ui", "--no-open"])

    assert result.exit_code != 0
    assert "no okto-neuron server reachable" in result.output


def test_wait_for_server_health_accepts_minimal_public_liveness_without_expected_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from okto_neuron import cli as cli_module

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> dict[str, object]:
            return {"status": "ok"}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)

    assert cli_module._wait_for_server_health(
        "http://127.0.0.1:7777",
        timeout=0.1,
    )


def test_wait_for_server_health_rejects_a_healthy_different_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from okto_neuron import cli as cli_module

    requests: list[tuple[str, dict[str, str] | None]] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(
            self,
            url: str,
            headers: dict[str, str] | None = None,
        ) -> FakeResponse:
            requests.append((url, headers))
            if url.endswith("/health"):
                return FakeResponse({"status": "ok"})
            return FakeResponse({"status": "ok", "pid": 5150})

    monkeypatch.setattr(httpx, "Client", FakeClient)
    assert not cli_module._wait_for_server_health(
        "http://127.0.0.1:7777",
        timeout=0.06,
        expected_pid=5151,
        vault_path=tmp_path,
    )
    assert any(url.endswith("/api/v1/status") for url, _headers in requests)
    assert all(headers is None for url, headers in requests if url.endswith("/api/v1/status"))


def test_wait_for_server_health_accepts_expected_daemon_without_browser_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    from okto_neuron import cli as cli_module

    class FakeResponse:
        status_code = 200

        def __init__(self, payload: dict[str, object]) -> None:
            self._payload = payload

        def json(self) -> dict[str, object]:
            return self._payload

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(
            self,
            url: str,
            headers: dict[str, str] | None = None,
        ) -> FakeResponse:
            if url.endswith("/health"):
                return FakeResponse({"status": "ok"})
            assert headers is None
            return FakeResponse({"status": "degraded", "pid": 5151})

    monkeypatch.setattr(httpx, "Client", FakeClient)
    assert cli_module._wait_for_server_health(
        "http://127.0.0.1:7777",
        timeout=0.1,
        expected_pid=5151,
        vault_path=tmp_path,
    )


# ---------------------------------------------------------------------------
# Windows file-sharing semantics (0.3.1 stale-PID fix)
#
# CRT ``os.open`` on Windows opens files without FILE_SHARE_DELETE, so deleting
# a file while any handle to it is open fails with a sharing violation
# (PermissionError, WinError 32). Before 0.3.1, ``PidFile.release`` unlinked
# ``server.pid`` while still holding it open and swallowed that error, so every
# clean stop on Windows left a stale PID record behind. The fixture below gives
# POSIX the Windows rule for the lifecycle module: ``Path.unlink`` refuses a file
# that any PID-record handle still has open, and the module takes its Windows
# (close, then conditionally delete) path. Locking stays the native ``flock``,
# which has the same exclusive, per-handle semantics as ``msvcrt.locking``.
# ---------------------------------------------------------------------------


@pytest.fixture
def windows_sharing(monkeypatch: pytest.MonkeyPatch) -> list[logging.LogRecord]:
    tracked: set[int] = set()
    real_open_pid_fd = lifecycle_module._open_pid_fd
    real_unlink = Path.unlink

    def tracking_open_pid_fd(path: Path, *, create: bool) -> int:
        fd = real_open_pid_fd(path, create=create)
        tracked.add(fd)
        return fd

    def sharing_unlink(self: Path, missing_ok: bool = False) -> None:
        try:
            target = os.stat(self)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        for fd in list(tracked):
            try:
                opened = os.fstat(fd)
            except OSError:
                tracked.discard(fd)
                continue
            if (opened.st_dev, opened.st_ino) == (target.st_dev, target.st_ino):
                raise PermissionError(
                    13,
                    "The process cannot access the file because it is being used "
                    "by another process",
                    str(self),
                )
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(lifecycle_module, "_windows_file_sharing", lambda: True)
    monkeypatch.setattr(lifecycle_module, "_open_pid_fd", tracking_open_pid_fd)
    monkeypatch.setattr(Path, "unlink", sharing_unlink)
    monkeypatch.setattr(lifecycle_module, "_REMOVE_RETRY_SECONDS", 0.0)

    records: list[logging.LogRecord] = []

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = ListHandler(level=logging.DEBUG)
    lifecycle_module._LOG.addHandler(handler)
    previous_level = lifecycle_module._LOG.level
    lifecycle_module._LOG.setLevel(logging.DEBUG)
    yield records
    lifecycle_module._LOG.removeHandler(handler)
    lifecycle_module._LOG.setLevel(previous_level)


def _write_dead_owner_record(root: Path) -> Path:
    target = pid_file_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        lifecycle_module._PidRecord(
            pid=99999999,
            start_token="posix:dead",
            owner_id="d" * 32,
        ).to_json(),
        encoding="utf-8",
    )
    return target


def test_windows_clean_release_removes_pid_record(
    tmp_path: Path, windows_sharing: list[logging.LogRecord]
) -> None:
    with PidFile(tmp_path) as owner:
        assert read_pid(tmp_path) == owner.pid
    assert not pid_file_path(tmp_path).exists()
    assert not [r for r in windows_sharing if r.levelno >= logging.WARNING]


def test_windows_release_retries_while_a_reader_holds_the_record(
    tmp_path: Path,
    windows_sharing: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``okto-neuron stop`` polls the record while the daemon shuts down; a poll
    # that has the file open at the instant of the delete must not leave it.
    owner = PidFile(tmp_path)
    owner.acquire()
    reader = lifecycle_module._open_pid_fd(pid_file_path(tmp_path), create=False)
    real_sleep = time.sleep
    closed: list[bool] = []

    def close_reader_on_first_retry(seconds: float) -> None:
        if not closed:
            os.close(reader)
            closed.append(True)
        real_sleep(seconds)

    monkeypatch.setattr(lifecycle_module.time, "sleep", close_reader_on_first_retry)
    owner.release()
    assert closed == [True]
    assert not pid_file_path(tmp_path).exists()


def test_windows_stop_cleans_stale_record_of_dead_owner(
    tmp_path: Path, windows_sharing: list[logging.LogRecord]
) -> None:
    target = _write_dead_owner_record(tmp_path)
    with pytest.raises(LifecycleError, match="stale PID file"):
        send_stop(tmp_path)
    assert not target.exists()


def test_windows_start_reclaims_stale_record_and_stop_leaves_none(
    tmp_path: Path, windows_sharing: list[logging.LogRecord]
) -> None:
    target = _write_dead_owner_record(tmp_path)
    with PidFile(tmp_path) as owner:
        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["pid"] == owner.pid
        assert payload["owner_id"] != "d" * 32
    assert not target.exists()
    reclaimed = [
        r for r in windows_sharing if getattr(r, "event", None) == "lifecycle.stale_pid_reclaimed"
    ]
    assert len(reclaimed) == 1
    assert "99999999" in reclaimed[0].getMessage()


def test_windows_conditional_delete_never_removes_a_new_owner_record(
    tmp_path: Path, windows_sharing: list[logging.LogRecord]
) -> None:
    old = PidFile(tmp_path)
    old.acquire()
    old_raw = pid_file_path(tmp_path).read_text(encoding="utf-8")
    old.release()
    assert not pid_file_path(tmp_path).exists()

    with PidFile(tmp_path) as newcomer:
        # A late cleanup for the previous owner (the daemon or a stop poller)
        # must leave the live newcomer's record in place.
        assert lifecycle_module._remove_unlocked_pid_record(pid_file_path(tmp_path), old_raw) is False
        assert read_pid(tmp_path) == newcomer.pid
    assert not pid_file_path(tmp_path).exists()


def test_pid_record_removal_failure_is_logged_not_swallowed(
    tmp_path: Path,
    windows_sharing: list[logging.LogRecord],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def always_shared(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(13, "sharing violation", str(self))

    owner = PidFile(tmp_path)
    owner.acquire()
    monkeypatch.setattr(Path, "unlink", always_shared)
    owner.release()
    failures = [
        r for r in windows_sharing if getattr(r, "event", None) == "lifecycle.remove_failed"
    ]
    assert failures
    assert any("PID record" in r.getMessage() for r in failures)
    assert all(r.levelno == logging.WARNING for r in failures)


def test_configure_logging_tees_to_the_file_and_the_console(tmp_path: Path) -> None:
    buf = io.StringIO()
    log_path = tmp_path / "logs" / "serve.log"
    logger = configure_logging(
        tmp_path, log_file=log_path, also_stream=buf, logger_name="okto_neuron.test_tee"
    )
    handlers = [h for h in logger.handlers if getattr(h, "_okto_neuron_json", False)]
    assert len(handlers) == 2
    logger.info("both", extra={"component": "c", "event": "e"})
    for h in handlers:
        h.flush()
    assert json.loads(buf.getvalue().strip())["msg"] == "both"
    assert json.loads(log_path.read_text(encoding="utf-8").strip())["msg"] == "both"


def test_unwritable_log_file_warns_once_and_falls_back_to_the_console(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")  # a file where the log directory should be
    buf = io.StringIO()
    logger = configure_logging(
        tmp_path,
        log_file=blocker / "serve.log",
        also_stream=buf,
        logger_name="okto_neuron.test_unwritable",
    )
    logger.info("still logging", extra={"component": "c", "event": "e"})
    handlers = [h for h in logger.handlers if getattr(h, "_okto_neuron_json", False)]
    assert len(handlers) == 1
    for h in handlers:
        h.flush()
    assert json.loads(buf.getvalue().strip())["msg"] == "still logging"
    err = capsys.readouterr().err
    assert err.count("cannot write the log file") == 1


def test_stream_is_file_matches_only_the_same_file(tmp_path: Path) -> None:
    target = tmp_path / "a.log"
    other = tmp_path / "b.log"
    target.write_text("", encoding="utf-8")
    other.write_text("", encoding="utf-8")
    with open(target, "ab") as handle:
        assert stream_is_file(handle, target)
        assert not stream_is_file(handle, other)
        assert not stream_is_file(handle, tmp_path / "missing.log")
    assert not stream_is_file(io.StringIO(), target)  # no file descriptor



def test_default_log_rotates_at_the_bound_and_stdout_follows_the_new_file(tmp_path: Path) -> None:
    """The daemon child's stdout/stderr are the log file; rotation keeps its name and re-points them."""
    import textwrap

    log = tmp_path / "logs" / "okto-neuron-serve.log"
    log.parent.mkdir()
    script = textwrap.dedent(
        """
        import os, sys
        from pathlib import Path
        from okto_neuron.server.lifecycle import configure_logging
        log = Path(sys.argv[1])
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        os.dup2(fd, 1); os.dup2(fd, 2); os.close(fd)
        logger = configure_logging(
            None, log_file=log, rotate_max_bytes=600, rotate_backups=2, follow_std_fds=True
        )
        for i in range(12):
            logger.warning("record-%02d %s", i, "x" * 80)
        os.write(2, b"raw-stderr-after-rotation\\n")
        """
    )
    subprocess.run([sys.executable, "-c", script, str(log)], check=True, cwd=tmp_path)
    assert log.is_file()
    backups = sorted(p.name for p in log.parent.iterdir() if p.name != log.name)
    assert backups == ["okto-neuron-serve.log.1", "okto-neuron-serve.log.2"]  # standard names, capped
    assert "raw-stderr-after-rotation" in log.read_text()  # fd 2 followed the rotation
    assert log.stat().st_size <= 600 + 200
    assert "record-11" in log.read_text()
