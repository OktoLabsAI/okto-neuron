"""Tests for SIGTERM-clean graceful shutdown (card 9242f29d).

Covers BR br_430ba18c: On SIGTERM/SIGINT the server stops accepting new
requests, drains in-flight requests up to 30s, closes the Ladybug vault
exactly once, removes the PID file, and exits 0.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Unit tests: GracefulShutdown orchestrator
# ---------------------------------------------------------------------------


def test_graceful_shutdown_initial_state() -> None:
    from okto_neuron.server.lifecycle import GracefulShutdown

    gs = GracefulShutdown()
    assert not gs.shutdown_requested
    assert gs.in_flight == 0


def test_graceful_shutdown_tracks_in_flight() -> None:
    from okto_neuron.server.lifecycle import GracefulShutdown

    gs = GracefulShutdown()
    with gs.track_request():
        assert gs.in_flight == 1
        with gs.track_request():
            assert gs.in_flight == 2
        assert gs.in_flight == 1
    assert gs.in_flight == 0


def test_graceful_shutdown_request_is_idempotent() -> None:
    from okto_neuron.server.lifecycle import GracefulShutdown

    gs = GracefulShutdown()
    gs.request_shutdown()
    gs.request_shutdown()  # second call must not raise / re-trigger
    assert gs.shutdown_requested
    # Event set exactly once but stays set.
    assert gs.shutdown_event.is_set()


def test_graceful_shutdown_first_deadline_is_never_extended() -> None:
    from okto_neuron.server.lifecycle import GracefulShutdown

    gs = GracefulShutdown()
    gs.request_shutdown(timeout=0.5)
    first = gs.deadline
    gs.request_shutdown(timeout=30.0)

    assert first is not None
    assert gs.deadline == first
    assert 0.0 < gs.remaining() <= 0.5


def test_graceful_shutdown_drain_waits_until_zero() -> None:
    from okto_neuron.server.lifecycle import GracefulShutdown

    gs = GracefulShutdown()
    cm = gs.track_request()
    cm.__enter__()
    finished = threading.Event()

    def releaser() -> None:
        time.sleep(0.1)
        cm.__exit__(None, None, None)
        finished.set()

    threading.Thread(target=releaser, daemon=True).start()
    drained = gs.wait_for_drain(timeout=2.0)
    assert drained is True
    assert finished.is_set()
    assert gs.in_flight == 0


def test_graceful_shutdown_drain_times_out() -> None:
    from okto_neuron.server.lifecycle import GracefulShutdown

    gs = GracefulShutdown()
    cm = gs.track_request()
    cm.__enter__()
    try:
        t0 = time.monotonic()
        drained = gs.wait_for_drain(timeout=0.2)
        elapsed = time.monotonic() - t0
        assert drained is False
        assert 0.15 <= elapsed < 1.5
        assert gs.in_flight == 1
    finally:
        cm.__exit__(None, None, None)


def test_graceful_shutdown_closes_vault_exactly_once() -> None:
    from okto_neuron.server.lifecycle import GracefulShutdown

    closes: list[int] = []

    class FakeVault:
        def close(self) -> None:
            closes.append(1)

    gs = GracefulShutdown()
    gs.shutdown(vault=FakeVault(), drain_timeout=0.1)
    gs.shutdown(vault=FakeVault(), drain_timeout=0.1)  # second call no-op
    assert closes == [1]


def test_graceful_shutdown_drains_then_closes() -> None:
    from okto_neuron.server.lifecycle import GracefulShutdown

    order: list[str] = []

    class FakeVault:
        def close(self) -> None:
            order.append("close")

    gs = GracefulShutdown()
    cm = gs.track_request()
    cm.__enter__()

    def releaser() -> None:
        time.sleep(0.05)
        order.append("drained")
        cm.__exit__(None, None, None)

    threading.Thread(target=releaser, daemon=True).start()
    gs.shutdown(vault=FakeVault(), drain_timeout=2.0)
    assert order == ["drained", "close"]


def test_graceful_shutdown_close_runs_even_on_drain_timeout() -> None:
    """If the drain window expires, we still close the vault and unblock."""
    from okto_neuron.server.lifecycle import GracefulShutdown

    closes: list[int] = []

    class FakeVault:
        def close(self) -> None:
            closes.append(1)

    gs = GracefulShutdown()
    cm = gs.track_request()
    cm.__enter__()
    try:
        result = gs.shutdown(vault=FakeVault(), drain_timeout=0.1)
        assert closes == [1]
        assert result["drained"] is False
        assert result["closed"] is True
    finally:
        cm.__exit__(None, None, None)


def test_install_shutdown_handlers_sets_flag_on_sigterm() -> None:
    from okto_neuron.server.lifecycle import (
        GracefulShutdown,
        install_shutdown_handlers,
    )

    gs = GracefulShutdown()
    restore = install_shutdown_handlers(gs)
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        # Signals on the main thread are delivered synchronously after
        # the C-level handler runs; give the interpreter a tick.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not gs.shutdown_requested:
            time.sleep(0.01)
        assert gs.shutdown_requested
    finally:
        restore()


def test_runtime_first_signal_drains_and_preserves_ingest_queue(monkeypatch) -> None:
    """A daemon stop pauses durable work; it must not behave like the UI Cancel button."""
    from okto_neuron.server import runtime
    from okto_neuron.server.lifecycle import GracefulShutdown

    class FakeState:
        def __init__(self) -> None:
            self.draining = False
            self.shutting_down = False
            self.ingest_cancel_requested = False
            self.ingest_queue = [object(), object()]
            self.mark_shutting_down_calls = 0

        def mark_shutting_down(self) -> None:
            self.mark_shutting_down_calls += 1
            self.shutting_down = True
            self.draining = True

    class FakeServer:
        should_exit = False
        force_exit = False

    state = FakeState()
    original_queue = list(state.ingest_queue)
    global_kills: list[bool] = []
    http_cancels: list[bool] = []
    monkeypatch.setattr(
        runtime,
        "kill_active_cli_processes",
        lambda: global_kills.append(state.draining) or 1,
    )
    monkeypatch.setattr(
        runtime,
        "cancel_active_litellm_calls",
        lambda: http_cancels.append(state.draining) or 1,
    )

    orchestrator = GracefulShutdown()
    rest_server = FakeServer()
    mcp_server = FakeServer()
    handler = runtime._ShutdownSignalHandler(
        state,  # type: ignore[arg-type]
        orchestrator,
        rest_server,  # type: ignore[arg-type]
        mcp_server,  # type: ignore[arg-type]
    )

    handler()

    assert handler.triggered is True
    assert state.draining is True
    assert state.shutting_down is True
    assert state.mark_shutting_down_calls == 1
    assert orchestrator.shutdown_requested is True
    assert orchestrator.deadline is not None
    assert rest_server.should_exit is True
    assert mcp_server.should_exit is True
    assert rest_server.force_exit is False
    assert mcp_server.force_exit is False
    assert global_kills == [True]
    assert http_cancels == [True]
    # SIGTERM pauses/resumes durable ingest. Only the explicit Stop Bulk Ingest
    # action may set this flag or turn queued items into terminal cancellations.
    assert state.ingest_cancel_requested is False
    assert state.ingest_queue == original_queue


def test_runtime_repeat_signal_force_stops_all_cli_processes(monkeypatch) -> None:
    """A second Ctrl-C retries process cleanup and forces both transports to exit."""
    from okto_neuron.server import runtime
    from okto_neuron.server.lifecycle import GracefulShutdown

    class FakeState:
        draining = False

        def __init__(self) -> None:
            self.shutting_down = False
            self.mark_shutting_down_calls = 0

        def mark_shutting_down(self) -> None:
            self.mark_shutting_down_calls += 1
            self.shutting_down = True
            self.draining = True

    class FakeServer:
        should_exit = False
        force_exit = False

    global_kills: list[bool] = []
    http_cancels: list[bool] = []
    monkeypatch.setattr(
        runtime,
        "kill_active_cli_processes",
        lambda: global_kills.append(True) or 2,
    )
    monkeypatch.setattr(
        runtime,
        "cancel_active_litellm_calls",
        lambda: http_cancels.append(True) or 1,
    )

    state = FakeState()
    orchestrator = GracefulShutdown()
    rest_server = FakeServer()
    mcp_server = FakeServer()
    forced_exit_codes: list[int] = []
    handler = runtime._ShutdownSignalHandler(
        state,  # type: ignore[arg-type]
        orchestrator,
        rest_server,  # type: ignore[arg-type]
        mcp_server,  # type: ignore[arg-type]
        force_process_exit=forced_exit_codes.append,
    )

    handler()
    assert global_kills == [True]
    assert http_cancels == [True]

    handler(signal.SIGINT)

    assert global_kills == [True, True]
    assert http_cancels == [True, True]
    assert state.mark_shutting_down_calls == 1
    assert rest_server.should_exit is True
    assert mcp_server.should_exit is True
    assert rest_server.force_exit is True
    assert mcp_server.force_exit is True
    assert orchestrator.force_requested is True
    assert forced_exit_codes == [128 + signal.SIGINT]


def test_shutdown_state_cannot_be_cleared_by_maintenance_cleanup() -> None:
    from okto_neuron.server.state import ServerState

    state = ServerState(vault=None, vault_path=None)
    state.mark_draining()
    state.mark_shutting_down()

    # Rebuild/reembed/heal finally blocks use this legacy assignment. It must
    # clear only maintenance, never the process-lifecycle shutdown gate.
    state.draining = False

    assert state.shutting_down is True
    assert state.draining is True


@pytest.mark.asyncio
async def test_runtime_tracks_complete_asgi_request_lifetime() -> None:
    from okto_neuron.server import runtime
    from okto_neuron.server.lifecycle import GracefulShutdown

    entered = asyncio.Event()
    release = asyncio.Event()

    async def app(scope, receive, send):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()

    orchestrator = GracefulShutdown()
    tracked = runtime._TrackedASGIApp(app, orchestrator)
    task = asyncio.create_task(tracked({"type": "http"}, None, None))
    await entered.wait()
    assert orchestrator.in_flight == 1

    release.set()
    await task
    assert orchestrator.in_flight == 0


@pytest.mark.asyncio
async def test_mcp_lifecycle_filter_keeps_warnings_and_non_lifecycle_info() -> None:
    import logging

    from okto_neuron.server import runtime

    task = asyncio.current_task()
    assert task is not None
    old_name = task.get_name()
    task.set_name("okto-neuron-mcp")
    try:
        lifecycle = logging.LogRecord(
            "uvicorn.error", logging.INFO, __file__, 1, "Shutting down", (), None
        )
        warning = logging.LogRecord(
            "uvicorn.error", logging.WARNING, __file__, 1, "MCP failed", (), None
        )
        useful_info = logging.LogRecord(
            "uvicorn.error", logging.INFO, __file__, 1, "MCP ready", (), None
        )
        log_filter = runtime._McpLifecycleNoiseFilter()

        assert log_filter.filter(lifecycle) is False
        assert log_filter.filter(warning) is True
        assert log_filter.filter(useful_info) is True
    finally:
        task.set_name(old_name)


@pytest.mark.asyncio
async def test_runtime_one_deadline_bounds_owned_worker_wait(monkeypatch) -> None:
    from okto_neuron.server import runtime
    from okto_neuron.server.lifecycle import GracefulShutdown

    monkeypatch.setattr(runtime, "SHUTDOWN_DRAIN_TIMEOUT", 0.05)

    class FakeServer:
        should_exit = False
        force_exit = False

    class FakeState:
        def __init__(self, worker: asyncio.Task) -> None:
            self.scheduler_task = None
            self.folder_watch_task = None
            self.ingest_worker_task = worker
            self.curation_worker_task = None
            self.writer_lock = asyncio.Lock()
            self.shutting_down = False
            self.close_calls = 0

        def mark_shutting_down(self) -> None:
            self.shutting_down = True

        def close(self) -> None:
            self.close_calls += 1

    never = asyncio.Event()
    worker = asyncio.create_task(never.wait())
    transports = (
        asyncio.create_task(asyncio.sleep(0)),
        asyncio.create_task(asyncio.sleep(0)),
    )
    state = FakeState(worker)
    forced: list[int] = []
    started = time.monotonic()
    try:
        await runtime._graceful_shutdown(
            state=state,  # type: ignore[arg-type]
            orchestrator=GracefulShutdown(),
            rest_server=FakeServer(),  # type: ignore[arg-type]
            mcp_server=FakeServer(),  # type: ignore[arg-type]
            transport_tasks=transports,
            force_process_exit=forced.append,
        )
    finally:
        worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker

    # The drain budget (0.05 s) expired on the stuck worker, which was cancelled;
    # the reserved close budget then let the store close run. No hard exit.
    assert time.monotonic() - started < 2.0
    assert state.shutting_down is True
    assert state.close_calls == 1
    assert forced == []


@pytest.mark.asyncio
async def test_runtime_skips_close_under_an_in_flight_grafx_call(monkeypatch, caplog) -> None:
    """A grafx call still running at the hard deadline is never closed under."""
    import logging

    from okto_neuron.server import lifecycle, runtime
    from okto_neuron.server.lifecycle import GracefulShutdown

    monkeypatch.setattr(runtime, "SHUTDOWN_DRAIN_TIMEOUT", 0.05)
    monkeypatch.setattr(lifecycle, "_MIN_CLOSE_BUDGET_SECONDS", 0.2)

    class FakeServer:
        should_exit = False
        force_exit = False

    class FakePool:
        def calls_in_flight(self) -> dict[str, int]:
            return {"scratch": 2}

    class FakeState:
        scheduler_task = None
        folder_watch_task = None
        ingest_worker_task = None
        curation_worker_task = None
        writer_lock = asyncio.Lock()
        shutting_down = False
        vault_pool = FakePool()
        close_calls = 0

        def mark_shutting_down(self) -> None:
            self.shutting_down = True

        def close(self) -> None:
            self.close_calls += 1

    state = FakeState()
    forced: list[int] = []
    transports = (
        asyncio.create_task(asyncio.sleep(0)),
        asyncio.create_task(asyncio.sleep(0)),
    )
    with caplog.at_level(logging.INFO):
        with pytest.raises(runtime._ShutdownDeadlineExpired):
            await runtime._graceful_shutdown(
                state=state,  # type: ignore[arg-type]
                orchestrator=GracefulShutdown(),
                rest_server=FakeServer(),  # type: ignore[arg-type]
                mcp_server=FakeServer(),  # type: ignore[arg-type]
                transport_tasks=transports,
                force_process_exit=forced.append,
            )

    assert state.close_calls == 0
    assert forced == [1]
    assert (
        "store close skipped: 2 grafx calls in flight, relying on WAL recovery" in caplog.text
    )
    assert "outcome=close_skipped" in caplog.text


@pytest.mark.asyncio
async def test_runtime_waits_for_http_maintenance_before_vault_close() -> None:
    from okto_neuron.server import runtime
    from okto_neuron.server.lifecycle import GracefulShutdown

    class FakeServer:
        should_exit = False
        force_exit = False

    finished = asyncio.Event()
    close_saw_finished: list[bool] = []

    async def maintenance() -> None:
        await asyncio.sleep(0)
        finished.set()

    worker = asyncio.create_task(maintenance())

    class FakeState:
        scheduler_task = None
        folder_watch_task = None
        ingest_worker_task = None
        curation_worker_task = None
        writer_lock = asyncio.Lock()
        shutting_down = False
        maintenance_tasks = {worker}

        def mark_shutting_down(self) -> None:
            self.shutting_down = True

        def close(self) -> None:
            close_saw_finished.append(finished.is_set())

    transports = (
        asyncio.create_task(asyncio.sleep(0)),
        asyncio.create_task(asyncio.sleep(0)),
    )
    await runtime._graceful_shutdown(
        state=FakeState(),  # type: ignore[arg-type]
        orchestrator=GracefulShutdown(),
        rest_server=FakeServer(),  # type: ignore[arg-type]
        mcp_server=FakeServer(),  # type: ignore[arg-type]
        transport_tasks=transports,
    )

    assert close_saw_finished == [True]


# ---------------------------------------------------------------------------
# Integration test: real `okto-neuron serve` over subprocess
# ---------------------------------------------------------------------------


def _build_vault(tmp_path: Path) -> Path:
    """Create the minimal directory layout `okto-neuron serve` requires."""
    from okto_neuron import Vault

    vault = tmp_path / "vault"
    handle = Vault.init(vault, embedding_provider="stub")
    handle.close()
    return vault


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only signal semantics")
def test_serve_sigterm_clean_shutdown(tmp_path: Path) -> None:
    """Real `okto-neuron serve` must respond to SIGTERM with exit 0 and no PID file."""
    from okto_neuron import Vault
    from okto_neuron.server.lifecycle import pid_file_path, read_pid

    vault = _build_vault(tmp_path)
    # Plant a queued sidecar under the HOME that the subprocess environment
    # would otherwise inherit. If daemon discovery escapes its isolated HOME,
    # the worker rewrites this unknown job to ``error`` and the byte assertion
    # below catches the safety breach.
    inherited_home = tmp_path / "inherited-home"
    unrelated = inherited_home / ".marginalia" / "vaults" / "unrelated"
    Vault.init(unrelated, embedding_provider="stub").close()
    unrelated_queue = unrelated / ".marginalia" / "curation-jobs.json"
    unrelated_queue.parent.mkdir(parents=True, exist_ok=True)
    unrelated_queue.write_text(
        '{"version":1,"jobs":[{"id":"sentinel","kind":"never-registered","status":"queued"}]}',
        encoding="utf-8",
    )
    unrelated_before = unrelated_queue.read_bytes()
    # Pick unused high ports; bind to localhost.
    rest_port = 18000 + (os.getpid() % 1000)
    mcp_port = rest_port + 1
    # `python -m marginalia` is not supported (no package __main__); invoke
    # the click app via a small bootstrap so we don't depend on $PATH.
    bootstrap = textwrap.dedent(
        f"""
        import sys
        from okto_neuron.cli import app
        sys.argv = [
            "marginalia",
            "serve",
            "--vault", {str(vault)!r},
            "--host", "127.0.0.1",
            "--port", "{rest_port}",
            "--mcp-port", "{mcp_port}",
            "--no-open",
        ]
        app()
        """
    )
    env = os.environ.copy()
    env["HOME"] = str(inherited_home)
    # The application daemon discovers every registered vault at startup.
    # Subprocess tests must never inherit the developer's real vault registry.
    test_home = tmp_path / "home"
    test_home.mkdir()
    env["HOME"] = str(test_home)
    runtime_root = test_home / ".okto-neuron" / "runtime"
    env.pop("OKTO_NEURON_CONFIG", None)
    env.pop("OKTO_NEURON_VAULT", None)
    proc = subprocess.Popen(
        [sys.executable, "-c", bootstrap],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # Wait for PID file to appear (signals server is up).
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and read_pid(runtime_root) is None:
            time.sleep(0.05)
            if proc.poll() is not None:
                out, err = proc.communicate(timeout=1.0)
                pytest.fail(
                    f"serve exited early rc={proc.returncode}\n"
                    f"stdout={out.decode(errors='replace')}\n"
                    f"stderr={err.decode(errors='replace')}"
                )
        recorded = read_pid(runtime_root)
        assert recorded == proc.pid, f"pid mismatch: pidfile={recorded} proc={proc.pid}"

        # PID creation precedes app construction and signal-handler install.
        # Wait for the REST listener instead of sleeping a guessed duration;
        # cold imports can take longer under a busy full-suite run.
        ready = False
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", rest_port), timeout=0.2):
                    ready = True
                    break
            except OSError:
                time.sleep(0.05)
        assert ready, "REST listener never became ready"
        token_path = test_home / ".okto-neuron" / f"daemon-{rest_port}.token"
        token = token_path.read_text(encoding="utf-8").strip()
        assert token

        # Send SIGTERM and expect clean exit within 30s.
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        try:
            rc = proc.wait(timeout=30.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            pytest.fail("server did not exit within 30s of SIGTERM")
        elapsed = time.monotonic() - t0
        out, err = proc.communicate(timeout=2.0)
        if rc != 0:
            pytest.fail(
                f"non-zero exit rc={rc}\n"
                f"stdout={out.decode(errors='replace')[-3000:]}\n"
                f"stderr={err.decode(errors='replace')[-3000:]}"
            )
        assert elapsed < 30.0
        logs = (out + err).decode(errors="replace")
        assert token not in logs
        assert logs.count("Started server process") == 1
        assert logs.count("Shutting down") == 1
        assert logs.count("Finished server process") == 1
        assert unrelated_queue.read_bytes() == unrelated_before
        # PID file must be gone.
        assert not pid_file_path(runtime_root).exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2.0)
