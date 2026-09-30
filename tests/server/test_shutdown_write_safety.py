"""Write-path data safety of the clean shutdown (#22).

Both scenarios run a REAL ``serve`` subprocess (scratch HOME, free ports, small
scratch grafx vault, stub embeddings: no LLM) and write through the daemon's
normal no-LLM write route, ``POST /add`` (a document plus its block nodes and
edges, each committed as its own grafx write transaction).

* ``test_write_parked_before_commit_is_absent_and_committed_writes_survive``:
  K writes commit through ``/add``; then one more ``/add`` is parked INSIDE the
  counted write path, after its statement executed and before ``commit``. The
  stop hits the hard deadline with 1 grafx call in flight, so the store close
  is skipped and the process exits 1. A fresh process reopens the vault: the
  parked write is absent, all K documents are present, the integrity audit is
  not failed.
* ``test_stop_under_a_real_write_stream_loses_no_acked_write``: a client streams
  ``/add`` writes (acks fsynced to a file) while 4 clients poll; the real CLI
  ``stop --timeout 60`` exits 0, and the reopened vault holds every acked
  document (plus at most one committed-but-unacked one).

The only test seam is in the serve wrapper below: for the parking scenario it
replaces ``GrafxStore._execute_write`` with a copy of the production body
(same gate, same begin/execute/commit) that blocks between execute and commit
when a flag file exists.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import signal
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

from okto_neuron.server.lifecycle import (  # noqa: E402
    SHUTDOWN_OUTCOME_CAPABILITY,
    PidFile,
    consume_stop_outcome,
    outcome_file_path,
    pid_record_capabilities,
    read_pid,
    write_close_skipped_outcome,
    write_shutdown_outcome,
)
from okto_neuron.store.grafx import GrafxStore  # noqa: E402
from okto_neuron.store.integrity import AuditStatus, audit_graph  # noqa: E402
from okto_neuron.vault import Vault  # noqa: E402

K_COMMITTED = 6
# Measured against this daemon: each /add of a one-heading note writes a Document,
# a Block and a Claim node; the vault also holds one shared Agent and one Activity.
NODES_PER_ADD = 3
SHARED_NODES = 2
CLOSE_BUDGET_S = 2.0
DRAIN_S = 1.0

# sha256 of GrafxStore._execute_write's source with whitespace normalised. The
# serve wrapper below carries a copy of that body; if production changes, the
# copy must change with it.
_EXECUTE_WRITE_SHA256 = "cc0ebd889f4eaf1ef1c5ed73c2e2b1fcec52d83d860101a7c06ec0136249b68b"


def test_parked_execute_write_copy_tracks_production() -> None:
    source = " ".join(inspect.getsource(GrafxStore._execute_write).split())
    digest = hashlib.sha256(source.encode()).hexdigest()
    assert digest == _EXECUTE_WRITE_SHA256, "production _execute_write changed; update the parked copy"

_SERVE = textwrap.dedent(
    """
    import sys, threading
    from pathlib import Path

    park_flag, parked_marker, close_budget, argv = (
        Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3]), sys.argv[4:]
    )
    from okto_neuron.server import lifecycle
    from okto_neuron.store.grafx import GrafxStore

    lifecycle._MIN_CLOSE_BUDGET_SECONDS = close_budget
    import os
    if os.environ.get("OKTO_TEST_LEGACY_DAEMON"):
        # Behave like a 0.3.1 daemon: no advertised capability, no outcome file.
        lifecycle.SHUTDOWN_OUTCOME_CAPABILITY = "x-legacy"
        lifecycle.write_shutdown_outcome = lambda *a, **k: None
    never = threading.Event()

    def _execute_write(self, statement, params):
        with self._gate.call():
            txn = self._db.begin("write")
            try:
                txn.execute(statement, params)
                if park_flag.exists():
                    park_flag.unlink()
                    parked_marker.write_text("parked")
                    never.wait()  # statement executed, commit never reached
                txn.commit()
            except Exception:
                if txn.active:
                    txn.rollback()
                raise

    GrafxStore._execute_write = _execute_write
    from okto_neuron.cli import app
    sys.argv = ["okto-neuron", *argv]
    app()
    """
)


def _cli(argv: list[str]) -> str:
    return textwrap.dedent(
        f"""
        import sys
        from okto_neuron.cli import app
        sys.argv = {["okto-neuron", *argv]!r}
        app()
        """
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _post_add(base: str, name: str, timeout: float = 30.0) -> str:
    body = json.dumps({"path": f"{name}.md", "content": f"# {name}\n\nbody of {name}\n"}).encode()
    request = urllib.request.Request(
        f"{base}/add", data=body, headers={"content-type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    assert payload["status"] == "ok", payload
    return payload["document_id"]


class _Daemon:
    def __init__(self, tmp_path: Path, *, legacy: bool = False) -> None:
        self.tmp = tmp_path
        self.vault = tmp_path / "scratch-vault"
        Vault.init(self.vault, backend="grafx", embedding_provider="stub").close()
        home = tmp_path / "home"
        home.mkdir()
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("OKTO_NEURON_", "MARGINALIA_"))
        }
        self.env["HOME"] = str(home)
        if legacy:
            self.env["OKTO_TEST_LEGACY_DAEMON"] = "1"
        self.runtime_root = home / ".okto-neuron" / "runtime"
        self.rest_port, self.mcp_port = _free_port(), _free_port()
        self.base = f"http://127.0.0.1:{self.rest_port}"
        self.log_file = tmp_path / "serve.log"
        self.park_flag = tmp_path / "park.flag"
        self.parked_marker = tmp_path / "parked.marker"
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(
            [
                sys.executable, "-c", _SERVE,
                str(self.park_flag), str(self.parked_marker), str(CLOSE_BUDGET_S),
                "serve", "--vault", str(self.vault),
                "--port", str(self.rest_port), "--mcp-port", str(self.mcp_port),
                "--no-open", "--log-file", str(self.log_file),
            ],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            assert self.proc.poll() is None, self.proc.stdout.read().decode(errors="replace")
            try:
                with urllib.request.urlopen(f"{self.base}/health", timeout=2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("daemon never became healthy")
        assert read_pid(self.runtime_root) == self.proc.pid

    def stop_cli(self, timeout: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", _cli(["stop", "--timeout", str(timeout)])],
            env=self.env, capture_output=True, text=True, timeout=180,
        )

    def log(self) -> str:
        return self.log_file.read_text(encoding="utf-8", errors="replace")

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=5)


def _reopen_state(vault: Path) -> tuple[set[str], int, int]:
    """Reopen the vault: (document ids, node count, edge count)."""
    store = GrafxStore(vault)
    try:
        assert store.health().healthy
        nodes = list(store.list_nodes())
        assert audit_graph(store).status is not AuditStatus.FAILED
        return {n.id for n in nodes if n.type == "Document"}, len(nodes), len(list(store.list_edges()))
    finally:
        store.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal semantics")
def test_write_parked_before_commit_is_absent_and_committed_writes_survive(tmp_path: Path) -> None:
    daemon = _Daemon(tmp_path)
    daemon.start()
    parked_result: dict[str, object] = {}
    try:
        committed = [_post_add(daemon.base, f"committed-{i}") for i in range(K_COMMITTED)]
        assert len(set(committed)) == K_COMMITTED
        edges_before = _live_edges(daemon)
        nodes_total_before = K_COMMITTED * NODES_PER_ADD + SHARED_NODES

        daemon.park_flag.write_text("1")

        def parked_write() -> None:
            try:
                parked_result["id"] = _post_add(daemon.base, "parked-uncommitted", timeout=120.0)
            except Exception as exc:  # noqa: BLE001 - connection dies with the daemon
                parked_result["error"] = repr(exc)

        writer = threading.Thread(target=parked_write, daemon=True)
        writer.start()
        deadline = time.monotonic() + 60.0
        while not daemon.parked_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert daemon.parked_marker.exists(), "write never reached the park point"

        stop = daemon.stop_cli(DRAIN_S)
        code = daemon.proc.wait(timeout=90)
        log = daemon.log()
        print("\n".join(l for l in log.splitlines() if "shutdown." in l or "store close" in l))
        print("stop cli:", stop.returncode, (stop.stdout + stop.stderr).strip())
        print("daemon exit code:", code)
    finally:
        daemon.kill()

    assert code == 1, log
    assert stop.returncode == 3, stop.stdout + stop.stderr
    assert (
        "stopped, but the store close was skipped (1 grafx calls in flight); "
        "the next start recovers from the WAL"
    ) in stop.stdout + stop.stderr
    assert not outcome_file_path(daemon.runtime_root).exists(), "stop must consume the outcome file"
    assert "store close skipped: 1 grafx calls in flight, relying on WAL recovery" in log
    assert "store_closed=false outcome=close_skipped" in log
    assert "id" not in parked_result, "the parked write must never be acked"

    docs, nodes, edges = _reopen_state(daemon.vault)
    print(f"reopened: documents={len(docs)} nodes={nodes} edges={edges} (before park: nodes={nodes_total_before} edges={edges_before})")
    assert set(committed) <= docs, f"committed writes lost: {set(committed) - docs}"
    assert docs == set(committed), f"unexpected documents: {docs - set(committed)}"
    assert edges == edges_before, "an uncommitted write left edges behind"
    assert nodes == nodes_total_before, "an uncommitted write left nodes behind"


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal semantics")
def test_daemon_killed_mid_stop_reports_no_outcome_exit_3(tmp_path: Path) -> None:
    daemon = _Daemon(tmp_path)
    daemon.start()
    try:
        _post_add(daemon.base, "before-kill")
        daemon.park_flag.write_text("1")
        threading.Thread(target=_try_post, args=(daemon, "parked"), daemon=True).start()
        deadline = time.monotonic() + 60.0
        while not daemon.parked_marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert daemon.parked_marker.exists()
        stop = subprocess.Popen(
            [sys.executable, "-c", _cli(["stop", "--timeout", "30"])],
            env=daemon.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        deadline = time.monotonic() + 30.0
        while "shutdown." not in daemon.log() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert "shutdown." in daemon.log(), "daemon never started its shutdown"
        daemon.proc.kill()  # after the stop request, before any outcome is written
        output, _ = stop.communicate(timeout=120)
    finally:
        daemon.kill()
    assert stop.returncode == 3, output
    assert (
        "stopped, but the daemon left no shutdown outcome (crash or forced exit); "
        "the next start recovers from the WAL"
    ) in output
    assert not outcome_file_path(daemon.runtime_root).exists()


def _try_post(daemon: _Daemon, name: str) -> None:
    try:
        _post_add(daemon.base, name, timeout=120.0)
    except Exception:  # noqa: BLE001 - the daemon is killed under it
        pass


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal semantics")
def test_legacy_daemon_without_the_capability_keeps_exit_0(tmp_path: Path) -> None:
    daemon = _Daemon(tmp_path, legacy=True)
    daemon.start()
    try:
        _post_add(daemon.base, "legacy-doc")
        assert SHUTDOWN_OUTCOME_CAPABILITY not in pid_record_capabilities(daemon.runtime_root)
        stop = daemon.stop_cli(30)
        code = daemon.proc.wait(timeout=60)
    finally:
        daemon.kill()
    assert stop.returncode == 0, stop.stdout + stop.stderr
    assert code == 0
    assert "stopped okto-neuron server" in stop.stdout + stop.stderr
    assert not outcome_file_path(daemon.runtime_root).exists()


def test_stop_outcome_file_handling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from okto_neuron.server import lifecycle

    path = outcome_file_path(tmp_path)
    assert consume_stop_outcome(tmp_path, 42) is None  # missing

    monkeypatch.setattr(lifecycle, "_OUTCOME_ROOT", tmp_path)
    monkeypatch.setattr(os, "getpid", lambda: 42)
    write_close_skipped_outcome({"vault-a": 2, "vault-b": 0})
    monkeypatch.undo()
    assert path.exists()
    payload = consume_stop_outcome(tmp_path, 42)
    assert payload is not None and payload["calls_in_flight"] == 2
    assert payload["vaults"] == ["vault-a"]
    assert not path.exists(), "consumed file must be removed"

    path.write_text(json.dumps({"outcome": "close_skipped", "pid": 7, "calls_in_flight": 1}))
    assert consume_stop_outcome(tmp_path, 42) is None  # stale: another pid
    assert not path.exists(), "a stale file is removed too"

    path.write_text("{not json")
    assert consume_stop_outcome(tmp_path, 42) is None  # corrupt
    assert not path.exists()

    path.write_text(json.dumps({"outcome": "closed", "pid": 7, "calls_in_flight": 0}))
    assert consume_stop_outcome(tmp_path, 42) is None  # a stale `closed` from another pid
    assert not path.exists()

    path.write_text(json.dumps({"outcome": "weird", "pid": 42, "calls_in_flight": 0}))
    assert consume_stop_outcome(tmp_path, 42) is None  # unknown outcome
    assert not path.exists()

    monkeypatch.setattr(lifecycle, "_OUTCOME_ROOT", tmp_path)
    monkeypatch.setattr(os, "getpid", lambda: 42)
    write_shutdown_outcome("closed")
    monkeypatch.undo()
    closed = consume_stop_outcome(tmp_path, 42)
    assert closed is not None and closed["outcome"] == "closed"


def test_pid_record_advertises_the_outcome_capability(tmp_path: Path) -> None:
    assert pid_record_capabilities(tmp_path) == frozenset()  # no record at all
    with PidFile(tmp_path):
        assert SHUTDOWN_OUTCOME_CAPABILITY in pid_record_capabilities(tmp_path)


def _live_edges(daemon: _Daemon) -> int:
    """Edge count through the daemon's own API (no second writer handle)."""
    # The counts come from the maintained projection: force a rebuild, then wait until it is
    # current (stale and rebuilding both false).
    rebuild = urllib.request.Request(
        f"{daemon.base}/api/v1/upkeep/rebuild-stats", data=b"{}",
        headers={"content-type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(rebuild, timeout=10) as response:
        assert response.status == 202
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with urllib.request.urlopen(f"{daemon.base}/api/v1/graph/stats", timeout=10) as response:
            stats = json.loads(response.read())
            settled = response.status == 200 and not stats.get("stale") and not stats.get("rebuilding")
        if settled:
            assert isinstance(stats["total_edges"], int), stats
            return stats["total_edges"]
        time.sleep(0.1)
    pytest.fail("graph stats never settled")


@pytest.mark.skipif(os.name != "posix", reason="POSIX signal semantics")
def test_stop_under_a_real_write_stream_loses_no_acked_write(tmp_path: Path) -> None:
    daemon = _Daemon(tmp_path)
    daemon.start()
    ack_file = tmp_path / "acks.log"
    stop_clients = threading.Event()
    served: list[int] = []
    attempted: list[str] = []
    try:
        baseline = [_post_add(daemon.base, f"baseline-{i}") for i in range(5)]

        def writer() -> None:
            i = 0
            with ack_file.open("a", encoding="utf-8") as acks:
                while not stop_clients.is_set():
                    name = f"stream-{i}"
                    attempted.append(name)
                    i += 1
                    try:
                        doc_id = _post_add(daemon.base, name, timeout=30.0)
                    except Exception:  # noqa: BLE001 - draining/refused/closed: stop writing
                        if daemon.proc.poll() is not None or stop_clients.is_set():
                            return
                        time.sleep(0.05)
                        continue
                    acks.write(doc_id + "\n")
                    acks.flush()
                    os.fsync(acks.fileno())

        def poll(path: str) -> None:
            while not stop_clients.is_set():
                try:
                    with urllib.request.urlopen(f"{daemon.base}{path}", timeout=5) as response:
                        response.read()
                        served.append(response.status)
                except OSError:
                    time.sleep(0.02)

        threads = [threading.Thread(target=writer, daemon=True)] + [
            threading.Thread(target=poll, args=(p,), daemon=True)
            for p in ("/api/v1/graph/stats", "/api/v1/graph/stats", "/api/v1/review-queue", "/api/v1/review-queue")
        ]
        for t in threads:
            t.start()
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            acked_now = len(ack_file.read_text().split()) if ack_file.exists() else 0
            if acked_now >= 15 and len(served) >= 10:
                break
            time.sleep(0.1)
        assert acked_now >= 15 and len(served) >= 10, (acked_now, len(served))

        stop = daemon.stop_cli(60)
        stop_clients.set()
        code = daemon.proc.wait(timeout=60)
        stop_output = stop.stdout + stop.stderr
        log = daemon.log()
        summary = [l for l in log.splitlines() if "shutdown.summary" in l]
        print("shutdown.summary:", summary)
        print("stop cli:", stop.returncode, stop_output.strip())
    finally:
        stop_clients.set()
        daemon.kill()

    assert stop.returncode == 0, stop_output
    assert code == 0, log
    assert not outcome_file_path(daemon.runtime_root).exists(), "stop must consume the closed outcome"
    assert "stopped okto-neuron server" in stop_output
    assert "PID identity mismatch" not in stop_output + log
    assert "repeat shutdown signal" not in log
    assert "store_closed=true outcome=closed" in log
    assert "store close skipped" not in log

    acked = set(ack_file.read_text().split())
    docs, nodes, edges = _reopen_state(daemon.vault)
    expected = set(baseline) | acked
    missing = expected - docs
    extra = docs - expected
    print(f"acked={len(acked)} baseline={len(baseline)} reopened_documents={len(docs)} nodes={nodes} edges={edges} extra_unacked={len(extra)}")
    assert not missing, f"acked writes missing after reopen: {missing}"
    assert len(extra) <= 1, f"more than one unacked-but-committed write: {extra}"
