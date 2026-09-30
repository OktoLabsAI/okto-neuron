"""``okto-neuron stop --timeout 60`` against a real, busy daemon (#22).

A real ``serve`` subprocess runs over a scratch grafx vault while concurrent
clients poll the REST API; the real CLI ``stop`` must report success with no
"PID identity mismatch", the daemon must not see a repeat signal, every
shutdown phase must be in the serve log, and the vault must reopen with the
same counts.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import threading
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("okto_grafx")

from okto_neuron.core.schema import Node  # noqa: E402
from okto_neuron.server.lifecycle import read_pid  # noqa: E402
from okto_neuron.store.grafx import GrafxStore  # noqa: E402
from okto_neuron.vault import Vault  # noqa: E402

NODE_COUNT = 300
PHASES = (
    "signal_quiesce",
    "transports_and_workers",
    "request_drain",
    "late_workers",
    "writer_locks",
    "wait_store_idle",
    "grafx_quiesce",
    "vault_close",
    "store_close",
    "writer_lease_release",
    "store_close_total",
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _cli(argv: list[str]) -> str:
    return textwrap.dedent(
        f"""
        import sys
        from okto_neuron.cli import app
        sys.argv = {["okto-neuron", *argv]!r}
        app()
        """
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal semantics")
def test_stop_under_load_is_clean_and_the_vault_reopens_intact(tmp_path: Path) -> None:
    vault = tmp_path / "scratch-vault"
    Vault.init(vault, backend="grafx", embedding_provider="stub").close()
    store = GrafxStore(vault)
    try:
        for i in range(NODE_COUNT):
            store.add_node(Node(id=f"n{i}", type="Concept", title=f"node {i}", content="x"))
    finally:
        store.close()

    home = tmp_path / "home"
    home.mkdir()
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("OKTO_NEURON_", "MARGINALIA_"))
    }
    env["HOME"] = str(home)
    runtime_root = home / ".okto-neuron" / "runtime"
    rest_port, mcp_port = _free_port(), _free_port()
    serve_log = tmp_path / "serve.log"
    serve = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _cli(
                [
                    "serve",
                    "--vault", str(vault),
                    "--port", str(rest_port),
                    "--mcp-port", str(mcp_port),
                    "--no-open",
                    "--log-file", str(serve_log),
                ]
            ),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    stop_clients = threading.Event()
    served: list[int] = []
    try:
        deadline = time.monotonic() + 60.0
        base = f"http://127.0.0.1:{rest_port}"
        while time.monotonic() < deadline:
            assert serve.poll() is None, serve.stdout.read().decode(errors="replace")
            try:
                with urllib.request.urlopen(f"{base}/health", timeout=2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("daemon never became healthy")
        assert read_pid(runtime_root) == serve.pid

        def poll(path: str) -> None:
            while not stop_clients.is_set():
                try:
                    with urllib.request.urlopen(f"{base}{path}", timeout=5) as response:
                        response.read()
                        served.append(response.status)
                except OSError:
                    time.sleep(0.02)

        clients = [
            threading.Thread(target=poll, args=(path,), daemon=True)
            for path in (
                "/api/v1/graph/stats",
                "/api/v1/graph/stats",
                "/api/v1/review-queue",
                "/api/v1/review-queue",
            )
        ]
        for client in clients:
            client.start()
        while len(served) < 10 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert len(served) >= 10, "background clients never got a response"

        stop = subprocess.run(
            [sys.executable, "-c", _cli(["stop", "--timeout", "60"])],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        stop_clients.set()
        assert serve.wait(timeout=30) == 0
        stop_output = stop.stdout + stop.stderr
        log = serve_log.read_text(encoding="utf-8", errors="replace")
        print("\n".join(line for line in log.splitlines() if "shutdown." in line))

        assert stop.returncode == 0, stop_output
        assert "stopped okto-neuron server" in stop_output
        assert "PID identity mismatch" not in stop_output + log
        assert "repeat shutdown signal" not in log
        for phase in PHASES:
            assert f"shutdown.phase name={phase} " in log, phase
        assert "store_closed=true outcome=closed" in log
        assert "shutdown.drain_expired" not in log
        assert "store close skipped" not in log
    finally:
        stop_clients.set()
        if serve.poll() is None:
            serve.kill()
            serve.wait(timeout=5)

    reopened = GrafxStore(vault)
    try:
        assert reopened.health().healthy
        assert len([n for n in reopened.list_nodes() if n.type == "Concept"]) == NODE_COUNT
    finally:
        reopened.close()
