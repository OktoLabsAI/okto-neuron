"""Per-vault writer lease (#21), end to end across real processes.

A real ``okto-neuron serve`` runs in a scratch HOME on free high ports against a
small scratch vault; every CLI command below is a real ``python -m okto_neuron.cli``
child. Never touches the default ports or any real vault directory.

Refused writers must exit 5, name the daemon's pid on stderr, and leave the
vault's graph, review queue and ledger bytes untouched. A reader runs while the
daemon holds the lease. After a SIGKILL of the daemon a writer takes the lease at
once (the kernel drops a flock with its holder) and a fresh daemon starts clean.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest

from okto_neuron.store.writer_lease import LEASE_FILENAME
from okto_neuron.vault import Vault

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX flock semantics")

_STRIP_ENV = (
    "OKTO_NEURON_MLFLOW_TRACKING_URI",
    "OKTO_NEURON_MLFLOW_EXPERIMENT",
    "MARGINALIA_MLFLOW_TRACKING_URI",
    "MARGINALIA_GLM_API_KEY",
    "MARGINALIA_TOKEN",
    "OKTO_NEURON_CONFIG",
    "OKTO_NEURON_VAULT",
    "OKTO_NEURON_LAPTOP_GATE",
)
_FORBIDDEN_PORTS = {7777, 7787, 7788, 8201, 8211, 8212, 7791, 8231}
_STARTUP_TIMEOUT_S = 40.0


def _free_port() -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in _FORBIDDEN_PORTS and port > 10000:
            return port


@dataclass
class Daemon:
    proc: subprocess.Popen
    vault: Path
    home: Path
    env: dict[str, str]
    rest_port: int
    mcp_port: int

    def lease_record(self) -> dict | None:
        try:
            raw = (self.vault / LEASE_FILENAME).read_text(encoding="utf-8").strip()
            return json.loads(raw.splitlines()[0]) if raw else None
        except (OSError, ValueError):
            return None


def _scratch_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}
    env["HOME"] = str(home)
    return env


def _start_daemon(vault: Path, home: Path) -> Daemon:
    env = _scratch_env(home)
    rest_port, mcp_port = _free_port(), _free_port()
    assert rest_port != mcp_port
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "okto_neuron.cli",
            "serve",
            "--vault",
            str(vault),
            "--host",
            "127.0.0.1",
            "--port",
            str(rest_port),
            "--mcp-port",
            str(mcp_port),
            "--no-open",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    daemon = Daemon(proc, vault, home, env, rest_port, mcp_port)
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            out, err = proc.communicate(timeout=2)
            pytest.fail(
                f"serve exited early rc={proc.returncode}\n"
                f"stdout={out.decode(errors='replace')[-2000:]}\n"
                f"stderr={err.decode(errors='replace')[-2000:]}"
            )
        record = daemon.lease_record()
        if record and record.get("pid") == proc.pid and record.get("role") == "daemon":
            try:
                with socket.create_connection(("127.0.0.1", rest_port), timeout=0.2):
                    return daemon
            except OSError:
                pass
        time.sleep(0.1)
    proc.kill()
    pytest.fail("daemon never took the vault's writer lease and opened its REST port")


def _stop_daemon(daemon: Daemon) -> None:
    if daemon.proc.poll() is None:
        daemon.proc.send_signal(signal.SIGTERM)
        try:
            daemon.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            daemon.proc.kill()
            daemon.proc.wait(timeout=10)
    for stream in (daemon.proc.stdout, daemon.proc.stderr):
        if stream:
            stream.close()


def _cli(daemon: Daemon, *args: str, timeout: float = 90.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "okto_neuron.cli", *args],
        env=daemon.env,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=daemon.home,
    )


def _guarded_argv(vault: Path, scratch: Path) -> dict[str, list[str]]:
    v = str(vault)
    return {
        "watch": ["watch", v, "--once"],
        "pilot": ["pilot", v, "--report-dir", str(scratch / "pilot")],
        "init --wipe": ["init", v, "--wipe"],
        "kg init": ["kg", "init", v],
        "kg rebuild": ["kg", "rebuild", v],
        "kg reembed": ["kg", "reembed", v],
        "kg reindex": ["kg", "reindex", v],
        "reconcile propose": ["kg", "reconcile", "propose", v],
        "reconcile apply": ["kg", "reconcile", "apply", v],
        "review confirm": ["kg", "reconcile", "review", "confirm", "cid", v],
        "review reject": ["kg", "reconcile", "review", "reject", "cid", v],
        "reconcile heal": ["kg", "reconcile", "heal", v],
        "snapshot dump": ["kg", "snapshot", "dump", v, str(scratch / "snap")],
        "onboard": ["onboard", "--vault", v, "--backend", "ladybug", "--disable-llm",
                    "--non-interactive"],
    }


def _protected_hashes(vault: Path) -> dict[str, str]:
    """Bytes the refused commands must not touch: the graph, the review queue, the ledger."""
    out: dict[str, str] = {}
    for path in sorted(vault.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(vault).as_posix()
        if rel == LEASE_FILENAME or rel.endswith(".lock"):
            continue
        if rel.startswith("graph") or "review_queue" in rel or "ledger" in rel:
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


@pytest.fixture
def scratch(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    vault = tmp_path / "vault"
    Vault.init(vault, embedding_provider="stub").close()
    marginalia = vault / ".marginalia"
    marginalia.mkdir(exist_ok=True)
    (marginalia / "review_queue.json").write_text('{"items": []}\n', encoding="utf-8")
    (marginalia / "candidate-ledger.jsonl").write_text('{"run_id": "seed"}\n', encoding="utf-8")
    return tmp_path, vault, home


def test_refused_commands_against_a_real_daemon(scratch) -> None:
    tmp_path, vault, home = scratch
    daemon = _start_daemon(vault, home)
    try:
        before = _protected_hashes(vault)
        assert any(k.startswith("graph") for k in before), before
        cases = _guarded_argv(vault, tmp_path)
        with ThreadPoolExecutor(max_workers=7) as pool:
            futures = {name: pool.submit(_cli, daemon, *argv) for name, argv in cases.items()}
            results = {name: fut.result() for name, fut in futures.items()}

        for name, result in results.items():
            assert result.returncode == 5, (name, result.stdout, result.stderr)
            assert f"pid {daemon.proc.pid}" in result.stderr, (name, result.stderr)
            assert "okto-neuron stop" in result.stderr or "/api/v1/" in result.stderr, name
        assert _protected_hashes(vault) == before
        assert not (tmp_path / "snap").exists()
        assert not (tmp_path / "pilot").exists()
        assert daemon.proc.poll() is None, "a refused command must not disturb the daemon"
        record = daemon.lease_record()
        assert record is not None and record["pid"] == daemon.proc.pid
    finally:
        _stop_daemon(daemon)


def test_reader_succeeds_while_the_daemon_holds_the_lease(scratch) -> None:
    _tmp, vault, home = scratch
    daemon = _start_daemon(vault, home)
    try:
        before = _protected_hashes(vault)
        listed = _cli(daemon, "kg", "reconcile", "review", "list", str(vault), "--json")
        assert listed.returncode == 0, (listed.stdout, listed.stderr)
        assert json.loads(listed.stdout) == []
        assert _protected_hashes(vault) == before
        assert daemon.proc.poll() is None
        with socket.create_connection(("127.0.0.1", daemon.rest_port), timeout=2):
            pass
    finally:
        _stop_daemon(daemon)


def test_sigkill_frees_the_lease_at_once_and_the_daemon_restarts_cleanly(scratch) -> None:
    _tmp, vault, home = scratch
    daemon = _start_daemon(vault, home)
    refused = _cli(daemon, "kg", "reindex", str(vault), "--force")
    assert refused.returncode == 5, (refused.stdout, refused.stderr)

    daemon.proc.kill()
    daemon.proc.wait(timeout=10)
    for stream in (daemon.proc.stdout, daemon.proc.stderr):
        if stream:
            stream.close()

    started = time.monotonic()
    result = _cli(daemon, "kg", "reindex", str(vault), "--force")
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert time.monotonic() - started < 60

    restarted = _start_daemon(vault, home)
    try:
        record = restarted.lease_record()
        assert record is not None and record["pid"] == restarted.proc.pid
        again = _cli(restarted, "kg", "reindex", str(vault), "--force")
        assert again.returncode == 5
        assert f"pid {restarted.proc.pid}" in again.stderr
    finally:
        _stop_daemon(restarted)
    assert restarted.proc.returncode == 0
