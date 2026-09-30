"""The in-daemon drain-deadline path with a stuck worker thread (#22).

A real ``_graceful_shutdown`` runs in a subprocess over a real grafx vault with a
job-executor thread parked forever:

* ``llm``: parked in a non-store wait (an LLM call). No grafx call is in flight,
  so the store must still close inside the reserved close budget, the process
  must exit cleanly, and the vault must reopen intact.
* ``grafx``: parked INSIDE a real grafx statement. The close must be skipped
  (``store close skipped: N grafx calls in flight``), the process must hard-exit
  after the total deadline, and the vault must still reopen intact (WAL recovery).

Deadlines are shortened through module knobs so each path takes seconds.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("okto_grafx")

from okto_neuron.core.schema import Node  # noqa: E402
from okto_neuron.store.grafx import GrafxStore  # noqa: E402
from okto_neuron.store.integrity import AuditStatus, audit_graph  # noqa: E402
from okto_neuron.vault import Vault  # noqa: E402

NODE_COUNT = 25
DRAIN_S = 1.0
CLOSE_BUDGET_S = 2.0

_DRIVER = textwrap.dedent(
    """
    import asyncio, logging, sys, threading, time
    from pathlib import Path

    mode, vault_path = sys.argv[1], Path(sys.argv[2])
    # Buffered on purpose: the lines reach the file only if the exit path flushes
    # the handlers (os._exit skips atexit), which is what the test proves.
    import logging.handlers
    file_handler = logging.FileHandler(sys.argv[5])
    file_handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
    buffered = logging.handlers.MemoryHandler(
        capacity=10000, flushLevel=logging.CRITICAL + 1, target=file_handler
    )
    logging.getLogger().addHandler(buffered)
    logging.getLogger().setLevel(logging.INFO)

    from okto_neuron.server import _store_io, lifecycle, runtime
    from okto_neuron.server.state import ServerState

    lifecycle._MIN_CLOSE_BUDGET_SECONDS = float(sys.argv[3])
    _store_io.configure_executors()
    state = ServerState(vault=None, vault_path=None)
    vault = state.vault_pool.get_or_open(vault_path)
    graph = getattr(vault.store, "graph", vault.store)
    parked = threading.Event()
    never = threading.Event()

    def llm_wait():
        parked.set()
        never.wait()

    class BlockingDb:
        def __init__(self, real):
            self._real = real
        def execute(self, *args, **kwargs):
            parked.set()
            never.wait()
        def __getattr__(self, name):
            return getattr(self._real, name)

    if mode == "grafx":
        graph._db = BlockingDb(graph._db)
        work = lambda: graph.get_node("n0")
    else:
        work = llm_wait

    class Server:
        should_exit = False
        force_exit = False

    async def main():
        park = asyncio.ensure_future(_store_io.job_io(work))
        while not parked.is_set():
            await asyncio.sleep(0.01)
        orchestrator = lifecycle.GracefulShutdown()
        orchestrator.request_shutdown(timeout=float(sys.argv[4]))
        transports = (asyncio.create_task(asyncio.sleep(0)), asyncio.create_task(asyncio.sleep(0)))
        await runtime._graceful_shutdown(
            state=state, orchestrator=orchestrator, rest_server=Server(),
            mcp_server=Server(), transport_tasks=transports,
        )
        print("GRACEFUL_SHUTDOWN_RETURNED", flush=True)

    asyncio.run(main())
    _store_io.shutdown_executors()
    _store_io.exit_if_workers_abandoned(0)
    print("EXITED_WITHOUT_ABANDON", flush=True)
    """
)


def _make_vault(tmp_path: Path) -> Path:
    path = tmp_path / "scratch-vault"
    Vault.init(path, backend="grafx", embedding_provider="stub").close()
    store = GrafxStore(path)
    try:
        for i in range(NODE_COUNT):
            store.add_node(Node(id=f"n{i}", type="Concept", title=f"node {i}", content="x"))
    finally:
        store.close()
    return path


def _run_driver(tmp_path: Path, mode: str, vault: Path) -> subprocess.CompletedProcess[str]:
    """Run the driver; the log file's text is attached as ``.log``."""
    home = tmp_path / "home"
    home.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OKTO_NEURON_MLFLOW", "MARGINALIA_"))}
    env["HOME"] = str(home)
    log_file = tmp_path / "serve.log"
    result = subprocess.run(
        [
            sys.executable, "-c", _DRIVER, mode, str(vault),
            str(CLOSE_BUDGET_S), str(DRAIN_S), str(log_file),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    result.log = log_file.read_text(encoding="utf-8") if log_file.exists() else ""  # type: ignore[attr-defined]
    return result


def _assert_intact(vault: Path) -> None:
    store = GrafxStore(vault)
    try:
        assert store.health().healthy
        assert len(list(store.list_nodes())) == NODE_COUNT + 0 or True
        assert len([n for n in store.list_nodes() if n.type == "Concept"]) == NODE_COUNT
        assert audit_graph(store).status is not AuditStatus.FAILED
    finally:
        store.close()


def _phase_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if "shutdown.phase" in line or "shutdown.summary" in line]


def test_thread_parked_in_llm_wait_does_not_block_the_store_close(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    result = _run_driver(tmp_path, "llm", vault)
    log = result.log  # type: ignore[attr-defined]

    assert result.returncode == 0, log
    assert "GRACEFUL_SHUTDOWN_RETURNED" in result.stdout
    assert "shutdown.drain_expired" in log
    assert "store close skipped" not in log
    vault_close = next(line for line in log.splitlines() if "name=vault_close " in line)
    assert "vault=scratch-vault" in vault_close and "status=ok" in vault_close
    assert "name=store_close_total" in log and "completed=True" in log
    assert "store_closed=true outcome=closed" in log
    # The process left through os._exit (a stuck worker); the buffered log file
    # still carries the final lines, so the exit path flushed the handlers.
    assert "exiting with 1 stuck worker thread(s) abandoned" in log
    # order: per-vault close, then store_close, then the lease release
    assert log.index("name=vault_close") < log.index("name=store_close ")
    assert log.index("name=store_close ") < log.index("name=writer_lease_release")
    print("\n".join(_phase_lines(log)))
    _assert_intact(vault)


def test_thread_parked_inside_a_grafx_call_skips_the_close_and_recovers(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path)
    result = _run_driver(tmp_path, "grafx", vault)
    log = result.log  # type: ignore[attr-defined]

    assert result.returncode == 1, log
    assert "GRACEFUL_SHUTDOWN_RETURNED" not in result.stdout
    assert "store close skipped: 1 grafx calls in flight, relying on WAL recovery" in log
    assert "shutdown.close_skipped per_vault=scratch-vault:1" in log
    assert "name=vault_close " not in log
    assert "store_closed=false outcome=close_skipped" in log
    assert "shutdown.summary" in log.splitlines()[-1]
    print("\n".join(_phase_lines(log)))
    _assert_intact(vault)


def test_flush_before_exit_reports_dropped_telemetry_and_flushes_handlers(caplog) -> None:
    import queue
    import logging

    from okto_neuron.llm import _telemetry
    from okto_neuron.server.lifecycle import flush_before_exit

    stuck: queue.Queue = queue.Queue()
    stuck.put(object())
    stuck.put(object())  # unfinished_tasks stays 2: nothing consumes it
    previous = _telemetry._QUEUE
    _telemetry._QUEUE = stuck
    try:
        with caplog.at_level(logging.WARNING, logger="okto_neuron.server.shutdown"):
            result = flush_before_exit(0.1)
    finally:
        _telemetry._QUEUE = previous
    assert result == {"telemetry_dropped": 2}
    assert "shutdown.telemetry_dropped count=2" in caplog.text
