"""`okto-neuron serve` always writes its log file, wherever stdout and stderr go.

A launcher that sends the foreground command's stdout/stderr to /dev/null used to leave no log at all
(a field report could not see why a vault failed to open). These tests start a real server process
under a scratch HOME on free ports.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _env(home: Path) -> dict[str, str]:
    return {
        "HOME": str(home),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": "C",
        "PYTHONPATH": os.pathsep.join(p for p in sys.path if p),
    }


def _wait_health(port: int, timeout: float = 90.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except OSError:
            time.sleep(0.5)
    return False


def _wait_health_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def _messages(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("{"):
            try:
                out.append(str(json.loads(line).get("msg")))
            except ValueError:
                pass
    return out


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:  # pragma: no cover - diagnostic path only
            proc.kill()
            proc.wait()


@pytest.mark.skipif(os.name != "posix", reason="posix process semantics")
def test_foreground_serve_with_output_discarded_still_writes_the_default_log(tmp_path: Path) -> None:
    rest, mcp = _free_port(), _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "okto_neuron.cli", "serve", "--no-open", "--port", str(rest), "--mcp-port", str(mcp)],
        env=_env(tmp_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert _wait_health(rest), "the scratch server did not become healthy"
        log = tmp_path / ".okto-neuron" / "logs" / "okto-neuron-serve.log"
        assert log.is_file(), "the default log file must exist even with stdout/stderr on /dev/null"
        assert "okto-neuron server starting" in _messages(log)
    finally:
        _stop(proc)


@pytest.mark.skipif(os.name != "posix", reason="posix process semantics")
def test_explicit_log_file_is_respected_and_the_default_is_not_created(tmp_path: Path) -> None:
    rest, mcp = _free_port(), _free_port()
    chosen = tmp_path / "elsewhere" / "serve.log"
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "okto_neuron.cli", "serve", "--no-open",
            "--port", str(rest), "--mcp-port", str(mcp), "--log-file", str(chosen),
        ],
        env=_env(tmp_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert _wait_health(rest)
        assert "okto-neuron server starting" in _messages(chosen)
        assert not (tmp_path / ".okto-neuron" / "logs" / "okto-neuron-serve.log").exists()
    finally:
        _stop(proc)


@pytest.mark.skipif(os.name != "posix", reason="posix process semantics")
def test_daemon_mode_writes_one_copy_of_every_record_to_the_default_log(tmp_path: Path) -> None:
    rest, mcp = _free_port(), _free_port()
    env = _env(tmp_path)
    started = subprocess.run(
        [sys.executable, "-m", "okto_neuron.cli", "serve", "--daemon", "--no-open", "--port", str(rest), "--mcp-port", str(mcp)],
        env=env, capture_output=True, text=True, timeout=180, check=False,
    )
    assert started.returncode == 0, started.stderr[-400:]
    try:
        assert _wait_health(rest)
        log = tmp_path / ".okto-neuron" / "logs" / "okto-neuron-serve.log"
        starts = [m for m in _messages(log) if m == "okto-neuron server starting"]
        assert len(starts) == 1, "daemon mode must not write each record twice"
    finally:
        stop = subprocess.run(
            [sys.executable, "-m", "okto_neuron.cli", "stop", "--timeout", "60"],
            env=env, capture_output=True, text=True, timeout=180, check=False,
        )
        # the daemon must really be gone (a leaked server would hold ports for the whole run)
        assert stop.returncode == 0, stop.stderr[-300:]
        assert not _wait_health_up(rest)
