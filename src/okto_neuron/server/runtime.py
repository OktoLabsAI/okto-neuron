"""Dual-port runtime for ``okto-neuron serve``.

Hosts the REST surface (default :7777) and the FastMCP streamable-http
surface (default :8201) in the SAME Python process. ``ServerState`` owns one
handle pool, while each resolved path owns an immutable ``VaultRuntime`` with
its own queues and writer lock. SIGTERM flips draining mode, then both Uvicorn
servers and every per-vault worker drain before pooled handles close.

There is no auto-spawn: ``serve`` is the only entry point that opens
the vault, and the CLI client speaks pure HTTP/JSON over loopback.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import functools
import hashlib
import logging
import os
import re
import signal
import threading
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Optional

import uvicorn
from pydantic import ValidationError

from okto_neuron._compat import CLI_NAME, app_home, legacy_app_home
from okto_neuron import __version__ as OKTO_NEURON_VERSION
from okto_neuron.errors import EmbeddingDimMismatch, OptionalDependencyError
from okto_neuron.llm._cli_provider import kill_active_cli_processes
from okto_neuron.llm._litellm_process import cancel_active_litellm_calls
from okto_neuron.server import _ingest_queue as iq
from okto_neuron.server import _gc_tuning
from okto_neuron.server import _integrity as graph_integrity
from okto_neuron.server._preload import preload_server_modules
from okto_neuron.server._prewarm import start_ledger_prewarm
from okto_neuron.server._store_io import (
    DEFAULT_JOB_WORKERS,
    DEFAULT_STORE_WORKERS,
    acquire_off_loop,
    cancel_queued_work,
    configure_executors,
    job_io,
    shutdown_executors,
    store_io,
    wait_executors_idle_async,
)
from okto_neuron.server._open_failure import client_open_failure
from okto_neuron.server._vault_pool import (
    VaultLease,
    VaultPoolError,
    acquire_daemon_writer_lease,
)
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.lifecycle import (
    GracefulShutdown,
    close_budget,
    last_stop_request,
    set_shutdown_hard_deadline,
    shutdown_phase,
)
from okto_neuron.server.state import (
    ServerState,
    VaultRuntime,
    init_state,
    reset_state_for_tests,
)
from okto_neuron.vault import Vault

# Hard cap on the drain window after SIGTERM (business rule br_430ba18c).
SHUTDOWN_DRAIN_TIMEOUT = 30.0

# Task 4: cap auto-restarts of the global folder-watch task so a persistently
# crashing loop surfaces as degraded on /health instead of hot-looping ensure_future.
MAX_FOLDER_WATCH_RESTARTS = 5

_LOG = logging.getLogger("okto_neuron.server.runtime")

DEFAULT_REST_PORT = 7777
DEFAULT_MCP_PORT = 8201
DEFAULT_HOST = "127.0.0.1"
_LOOPBACK_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _configure_projection() -> None:
    """``[server] projection_min_interval_s`` / ``projection_max_age_s`` (defaults 5 s / 600 s);
    a broken app config falls back to the defaults, like the executor sizes."""
    from okto_neuron.config import OktoNeuronConfig
    from okto_neuron.server import _projection

    try:
        server = OktoNeuronConfig.load().server
        _projection.configure(
            min_interval_s=float(server.projection_min_interval_s),
            max_age_s=float(server.projection_max_age_s),
        )
    except Exception as exc:  # noqa: BLE001 - startup must not die on this knob
        _LOG.warning("could not read [server] projection settings (%s); using defaults", exc)


def _configured_executor_workers() -> tuple[int, int]:
    """``[server] store_workers`` / ``job_workers`` from ``okto-neuron.toml``
    (defaults 4 and 2).

    A broken or unreadable app config never stops ``serve``; it falls back to the
    defaults and says so, because the same file is re-read (and reported) by the
    commands that actually depend on it.
    """
    from okto_neuron.config import OktoNeuronConfig

    try:
        server = OktoNeuronConfig.load().server
        return int(server.store_workers), int(server.job_workers)
    except Exception as exc:  # noqa: BLE001 - startup must not die on this knob
        _LOG.warning(
            "could not read [server] store_workers/job_workers (%s); using %d/%d",
            exc,
            DEFAULT_STORE_WORKERS,
            DEFAULT_JOB_WORKERS,
        )
        return DEFAULT_STORE_WORKERS, DEFAULT_JOB_WORKERS


class _ShutdownSignalHandler:
    """First signal starts one bounded drain; a repeat (or ``stop --force``) exits immediately."""

    def __init__(
        self,
        state: ServerState,
        orchestrator: GracefulShutdown,
        rest_server: uvicorn.Server,
        mcp_server: uvicorn.Server,
        wakeup: asyncio.Event | None = None,
        force_process_exit: Callable[[int], None] | None = None,
    ) -> None:
        self._state = state
        self._orchestrator = orchestrator
        self._rest_server = rest_server
        self._mcp_server = mcp_server
        self._wakeup = wakeup
        self._force_process_exit = force_process_exit
        self.triggered = False

    def __call__(self, signum: int | None = None) -> None:
        request = last_stop_request()
        if self.triggered or (request is not None and request.force):
            self._orchestrator.request_force_shutdown()
            cancelled = kill_active_cli_processes() + cancel_active_litellm_calls()
            self._rest_server.force_exit = True
            self._mcp_server.force_exit = True
            if self._wakeup is not None:
                self._wakeup.set()
            _LOG.warning(
                "repeat shutdown signal; forcing process exit after stopping "
                "%d active model call(s)",
                cancelled,
            )
            if self._force_process_exit is not None:
                exit_signal = signum if signum is not None else signal.SIGTERM
                self._force_process_exit(128 + int(exit_signal))
            return

        self.triggered = True
        drain_timeout = (
            request.drain_timeout
            if request is not None and request.drain_timeout is not None
            else SHUTDOWN_DRAIN_TIMEOUT
        )
        self._orchestrator.request_shutdown(timeout=drain_timeout)
        _LOG.info(
            "shutdown signal received; entering drain (drain_timeout=%.1fs, close_budget=%.1fs)",
            drain_timeout,
            close_budget(drain_timeout),
        )
        self._state.mark_shutting_down()
        cancelled = kill_active_cli_processes() + cancel_active_litellm_calls()
        if cancelled:
            _LOG.info("stopped %d active model call(s)", cancelled)
        self._rest_server.should_exit = True
        self._mcp_server.should_exit = True
        if self._wakeup is not None:
            self._wakeup.set()


class _TrackedASGIApp:
    """Count complete HTTP/WebSocket request lifetimes across both transports."""

    def __init__(self, app: Any, orchestrator: GracefulShutdown) -> None:
        self._app = app
        self._orchestrator = orchestrator

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        with self._orchestrator.track_request():
            await self._app(scope, receive, send)


class _McpLifecycleNoiseFilter(logging.Filter):
    """Hide only duplicate MCP Uvicorn lifecycle INFO lines."""

    _PREFIXES = (
        "Started server process",
        "Waiting for application startup",
        "Application startup complete",
        "Shutting down",
        "Waiting for application shutdown",
        "Application shutdown complete",
        "Finished server process",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            return True
        try:
            task = asyncio.current_task()
        except RuntimeError:
            return True
        if task is None or task.get_name() != "okto-neuron-mcp":
            return True
        return not record.getMessage().startswith(self._PREFIXES)


class _ShutdownDeadlineExpired(RuntimeError):
    """Test-visible fallback when an injected hard-exit callback returns."""


def _hard_exit(exit_code: int) -> None:
    """Exit without waiting for Python's executor shutdown after a hard deadline.

    ``os._exit`` skips ``atexit``, so flush telemetry and the log handlers first
    (the final ``shutdown.summary`` line must reach the serve log).
    """
    from okto_neuron.server.lifecycle import flush_before_exit

    flush_before_exit(_HARD_EXIT_FLUSH_SECONDS)
    os._exit(exit_code)


_HARD_EXIT_FLUSH_SECONDS = 2.0


async def _wait_for_tasks(
    tasks: set[asyncio.Task], orchestrator: GracefulShutdown
) -> set[asyncio.Task]:
    pending = {task for task in tasks if not task.done()}
    if not pending:
        return set()
    remaining = orchestrator.remaining(default_timeout=SHUTDOWN_DRAIN_TIMEOUT)
    if remaining <= 0:
        return pending
    _, pending = await asyncio.wait(pending, timeout=remaining)
    return set(pending)


async def _wait_for_request_drain(orchestrator: GracefulShutdown) -> bool:
    """Wait cooperatively so the event loop can finish tracked request bodies."""
    while orchestrator.in_flight:
        remaining = orchestrator.remaining(default_timeout=SHUTDOWN_DRAIN_TIMEOUT)
        if remaining <= 0:
            return False
        await asyncio.sleep(min(0.05, remaining))
    return True


def _runtime_tasks(state: ServerState) -> set[asyncio.Task]:
    """Snapshot all per-vault tasks, with a legacy-state compatibility fallback."""
    snapshot = getattr(state, "runtime_tasks", None)
    if callable(snapshot):
        return set(snapshot())

    tasks: set[asyncio.Task] = set()
    for name in ("ingest_worker_task", "curation_worker_task"):
        task = getattr(state, name, None)
        if task is not None:
            tasks.add(task)
    tasks.update(getattr(state, "maintenance_tasks", set()))
    return tasks


def _projection_tasks() -> set[asyncio.Task]:
    from okto_neuron.server._projection import projection_tasks

    return set(projection_tasks())


def _runtime_writer_locks(state: ServerState) -> tuple[asyncio.Lock, ...]:
    """Return every distinct writer lock that must be clear before pool close."""
    locks = [state.writer_lock]
    runtimes = getattr(state, "runtimes", None)
    if callable(runtimes):
        locks.extend(runtime.writer_lock for runtime in runtimes())
    unique: list[asyncio.Lock] = []
    seen: set[int] = set()
    for lock in locks:
        if id(lock) not in seen:
            seen.add(id(lock))
            unique.append(lock)
    return tuple(unique)


def _grafx_calls_in_flight(state: ServerState) -> dict[str, int]:
    """Grafx calls executing per vault right now (empty for other backends)."""
    pool = getattr(state, "vault_pool", None)
    counter = getattr(pool, "calls_in_flight", None)
    return dict(counter()) if callable(counter) else {}


def _flush_sidecars(state: Any) -> int:
    """Final write of every coalesced sidecar (ingest queue, curation jobs); returns how many wrote."""
    from okto_neuron.server import _jobs

    written = 0
    runtimes = getattr(state, "runtimes", None)
    try:
        targets = [*(runtimes() if callable(runtimes) else ()), state]
    except Exception:  # noqa: BLE001 - shutdown must not fail on a discovery error
        targets = [state]
    for target in targets:
        if getattr(target, "vault_path", None) is None:
            continue
        for flush in (iq.shutdown_flush, _jobs.shutdown_flush):
            try:
                written += int(bool(flush(target)))
            except Exception:  # noqa: BLE001 - a failed sidecar write must not block shutdown
                _LOG.warning("final sidecar flush failed", exc_info=True)
    return written


async def _graceful_shutdown(
    *,
    state: ServerState,
    orchestrator: GracefulShutdown,
    rest_server: uvicorn.Server,
    mcp_server: uvicorn.Server,
    transport_tasks: tuple[asyncio.Task, asyncio.Task],
    force_process_exit: Callable[[int], None] = _hard_exit,
) -> None:
    """Stop every owned activity and close the stores within two budgets.

    The drain budget (``stop --timeout``, default 30 s) covers transports,
    workers and in-flight requests. When it runs out the work is cancelled, NOT
    abandoned with the stores open: a further close budget
    (``max(5 s, 25 %)``, see :func:`close_budget`) is reserved so the stores
    still close. Only a grafx call that is still executing at the hard deadline
    (drain + close budget) prevents the close: the process then exits without
    closing and relies on WAL recovery; closing under a running statement is
    never attempted.
    """
    started = time.monotonic()
    orchestrator.request_shutdown(timeout=SHUTDOWN_DRAIN_TIMEOUT)
    drain_timeout = orchestrator.drain_timeout
    if drain_timeout is None:
        drain_timeout = SHUTDOWN_DRAIN_TIMEOUT
    deadline = orchestrator.deadline
    drain_deadline = deadline if deadline is not None else started + drain_timeout
    budget = close_budget(drain_timeout)
    hard_deadline = drain_deadline + budget
    set_shutdown_hard_deadline(hard_deadline)
    summary: dict[str, Any] = {"drain_expired": False, "store_closed": False, "outcome": "error"}

    def drain_left() -> float:
        return max(0.0, drain_deadline - time.monotonic())

    owned_tasks: set[asyncio.Task] = set(transport_tasks)

    def abandon_drain(reason: str) -> None:
        """Drain budget spent: cancel what is left and go on to the store close."""
        first = not summary["drain_expired"]
        summary["drain_expired"] = True
        orchestrator.request_force_shutdown()
        rest_server.force_exit = True
        mcp_server.force_exit = True
        cancelled_tasks = 0
        for task in owned_tasks:
            if not task.done():
                task.cancel()
                cancelled_tasks += 1
        stopped_calls = kill_active_cli_processes() + cancel_active_litellm_calls()
        cancelled_queued = cancel_queued_work()
        if first:
            _LOG.warning(
                "shutdown.drain_expired reason=%s cancelled_tasks=%d cancelled_queued_calls=%d "
                "stopped_model_calls=%d; continuing to the store close (%.1fs left)",
                reason,
                cancelled_tasks,
                cancelled_queued,
                stopped_calls,
                max(0.0, hard_deadline - time.monotonic()),
            )

    summary_logged = False

    def log_summary() -> None:
        nonlocal summary_logged
        if summary_logged:
            return
        summary_logged = True
        _LOG.info(
            "shutdown.summary total_ms=%d drain_timeout_s=%.1f close_budget_s=%.1f "
            "drain_expired=%s store_closed=%s outcome=%s",
            int((time.monotonic() - started) * 1000),
            drain_timeout,
            budget,
            str(summary["drain_expired"]).lower(),
            str(summary["store_closed"]).lower(),
            summary["outcome"],
        )

    acquired: list[asyncio.Lock] = []
    try:
        with shutdown_phase("signal_quiesce"):
            state.mark_shutting_down()
            rest_server.should_exit = True
            mcp_server.should_exit = True
            scheduler_task = state.scheduler_task
            folder_watch_task = state.folder_watch_task
            for task in (scheduler_task, folder_watch_task):
                if task is not None and not task.done():
                    task.cancel()
            owned_tasks |= _runtime_tasks(state) | _projection_tasks()
            for task in (scheduler_task, folder_watch_task):
                if task is not None:
                    owned_tasks.add(task)

        with shutdown_phase("transports_and_workers") as detail:
            pending = await _wait_for_tasks(owned_tasks, orchestrator)
            detail["pending"] = len(pending)
            if pending:
                abandon_drain("transports or background workers")
                await asyncio.wait(
                    pending, timeout=min(1.0, max(0.0, hard_deadline - time.monotonic()))
                )

        with shutdown_phase("request_drain") as detail:
            drained = await _wait_for_request_drain(orchestrator)
            detail["in_flight"] = orchestrator.in_flight
            if not drained:
                abandon_drain("in-flight requests")

        # A request already in flight when SIGTERM arrived may have submitted a
        # worker or maintenance task after the first snapshot. Request drain closes
        # that race; resnapshot ALL runtime-owned tasks before any handle can close.
        with shutdown_phase("late_workers") as detail:
            late_runtime_tasks = _runtime_tasks(state) - owned_tasks
            detail["late"] = len(late_runtime_tasks)
            if late_runtime_tasks:
                owned_tasks.update(late_runtime_tasks)
                pending = await _wait_for_tasks(late_runtime_tasks, orchestrator)
                if pending:
                    abandon_drain("late per-vault workers")
                    await asyncio.wait(
                        pending, timeout=min(1.0, max(0.0, hard_deadline - time.monotonic()))
                    )

        with shutdown_phase("writer_locks") as detail:
            skipped = 0
            for writer_lock in _runtime_writer_locks(state):
                try:
                    await asyncio.wait_for(writer_lock.acquire(), timeout=drain_left())
                except asyncio.TimeoutError:
                    # A stuck holder. The close does not depend on this lock: it
                    # is gated on the in-flight grafx count below.
                    skipped += 1
                    abandon_drain("writer locks")
                else:
                    acquired.append(writer_lock)
            detail["held"] = len(acquired)
            detail["skipped"] = skipped

        # A pool call whose awaiting task was cancelled keeps running on its
        # worker; give it the rest of the drain budget before handles close.
        with shutdown_phase("wait_store_idle") as detail:
            idle = await wait_executors_idle_async(drain_left())
            detail["idle"] = idle
            if not idle:
                # Busy workers are in an LLM/network wait or a grafx call; the
                # grafx count below tells which. Nothing queued may start now.
                abandon_drain("busy executors")

        # Coalesced sidecar writes (#37): stop the background flushers and write once
        # more what is still pending, so a clean stop loses no progress event.
        with shutdown_phase("flush_sidecars") as detail:
            detail["flushed"] = _flush_sidecars(state)

        # Close from a dedicated thread: the store executor may be wedged, and
        # the close must not queue behind a stuck worker.
        loop = asyncio.get_running_loop()
        finished = asyncio.Event()
        outcome: dict[str, Any] = {}

        def close_stores() -> None:
            try:
                state.close()
                outcome["ok"] = True
            except BaseException as exc:  # noqa: BLE001 - reported below
                outcome["error"] = exc
            finally:
                loop.call_soon_threadsafe(finished.set)

        with shutdown_phase("grafx_quiesce") as detail:
            while True:
                inflight = _grafx_calls_in_flight(state)
                if not any(inflight.values()) or time.monotonic() >= hard_deadline:
                    break
                await asyncio.sleep(0.02)
            detail["grafx_calls_in_flight"] = sum(inflight.values())

        blocked = sum(inflight.values())
        if blocked:
            summary["outcome"] = "close_skipped"
            _LOG.error(
                "store close skipped: %d grafx calls in flight, relying on WAL recovery", blocked
            )
            _LOG.error(
                "shutdown.close_skipped per_vault=%s",
                ",".join(f"{name}:{count}" for name, count in sorted(inflight.items()) if count),
            )
            raise_exit = True
        else:
            raise_exit = False
            closer = threading.Thread(
                target=close_stores, name="okto-neuron-shutdown-close", daemon=True
            )
            with shutdown_phase("store_close_total") as detail:
                closer.start()
                try:
                    await asyncio.wait_for(
                        finished.wait(), timeout=max(0.001, hard_deadline - time.monotonic())
                    )
                except asyncio.TimeoutError:
                    detail["completed"] = False
                else:
                    detail["completed"] = "error" not in outcome
            if not finished.is_set():
                # The close thread is still running: a grafx call started after
                # the quiesce check (close then waits for it) or the native close
                # itself is stuck. Exit; the next open recovers from the WAL.
                inflight = _grafx_calls_in_flight(state)
                blocked = sum(inflight.values())
                summary["outcome"] = "close_timeout"
                if blocked:
                    _LOG.error(
                        "store close skipped: %d grafx calls in flight, relying on WAL recovery",
                        blocked,
                    )
                else:
                    _LOG.error(
                        "store close did not finish before the hard deadline, "
                        "relying on WAL recovery"
                    )
                raise_exit = True
            elif "error" in outcome:
                summary["outcome"] = "close_error"
                raise outcome["error"]
            else:
                summary["store_closed"] = True
                summary["outcome"] = "closed"
                from okto_neuron.server.lifecycle import write_shutdown_outcome

                write_shutdown_outcome("closed")
        if raise_exit:
            orchestrator.request_force_shutdown()
            rest_server.force_exit = True
            mcp_server.force_exit = True
            if blocked:
                from okto_neuron.server.lifecycle import write_close_skipped_outcome

                write_close_skipped_outcome(inflight)
            log_summary()
            force_process_exit(1)
            raise _ShutdownDeadlineExpired(str(summary["outcome"]))
    finally:
        for writer_lock in reversed(acquired):
            writer_lock.release()
        log_summary()


def auth_token_path(vault_path: Path | None, rest_port: int) -> Path:
    """Return the application-scoped MCP credential path for ``rest_port``.

    ``vault_path`` remains in the signature for source compatibility with callers
    from pre-ADR-0034 releases, but selecting a vault must never rotate or relocate
    the daemon credential.
    """
    return app_home() / f"daemon-{rest_port}.token"


def _legacy_auth_token_path(rest_port: int) -> Path:
    """Where 0.2.x kept the daemon credential (``~/.marginalia``, D5)."""
    return legacy_app_home() / f"daemon-{rest_port}.token"


def _write_auth_token(token: str, vault_path: Path | None, rest_port: int) -> None:
    target = auth_token_path(vault_path, rest_port)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Create/truncate 0600 so only the owner (same-user clients) can read it.
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, token.encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(target, 0o600)
    except OSError as exc:  # pragma: no cover - defensive
        _LOG.warning("could not write capability token to %s: %s", target, exc)


def _read_auth_token(vault_path: Path | None, rest_port: int) -> str | None:
    """Read the application token, migrating a valid legacy vault token once."""

    def read_token(target: Path) -> str | None:
        try:
            token = target.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if token and "\n" not in token and len(token) >= 16:
            return token
        return None

    target = auth_token_path(vault_path, rest_port)
    token = read_token(target)
    if token is not None:
        return token

    # 0.2.x kept the credential in ~/.marginalia. Adopt it so MCP clients that
    # were registered with it keep authenticating after the upgrade.
    token = read_token(_legacy_auth_token_path(rest_port))
    if token is not None:
        _write_auth_token(token, vault_path, rest_port)
        return token

    # Pre-ADR-0034 releases stored the credential under the selected vault.
    # Adopt it into the application-scoped path so an upgrade does not break
    # existing MCP clients, then stop consulting vault state on later boots.
    if vault_path is None:
        return None
    token = read_token(Path(vault_path) / ".marginalia" / "daemon.token")
    if token is None:
        return None
    _write_auth_token(token, vault_path, rest_port)
    return token


def _start_supervised_folder_watch(state: ServerState) -> "asyncio.Task":
    """Spawn the global folder-watch loop with a crash-restart supervisor attached.

    Returns the live task and records it on ``state.folder_watch_task``. The
    supervisor (``_supervise_folder_watch``) is re-attached to every restarted task
    so a task that crashes again is caught too (bounded by the restart cap)."""
    from okto_neuron.server import _folder_watch

    task = asyncio.ensure_future(_folder_watch.run_folder_watch(state))
    task.add_done_callback(lambda t: _supervise_folder_watch(state, t))
    state.folder_watch_task = task
    return task


def _supervise_folder_watch(state: ServerState, task: "asyncio.Task") -> None:
    """done-callback for the global folder-watch task (Task 4).

    Restart policy, most-specific first:

    * ``state.draining`` — orderly shutdown cancel; do nothing (process is exiting).
    * ``task.cancelled()`` — a deliberate EXTERNAL cancel while not draining. Leave
      the task dead so ``/health`` surfaces ``folder_watch_running: false`` (degraded).
      A cancel is not a crash — do not resurrect.
    * clean return (``exception() is None``) — the loop is infinite, so a clean exit
      is unexpected; leave it stopped and visible rather than spin.
    * an exception escaped the loop's per-tick guard — a genuine CRASH. Restart
      (bounded by ``MAX_FOLDER_WATCH_RESTARTS``) so auto-ingest resumes, logging at
      ERROR each time; once the cap is hit, leave it dead → ``/health`` degraded.

    ``task.exception()`` is read ONLY after the ``cancelled()`` guard — it RAISES
    ``CancelledError`` on a cancelled task rather than returning it.
    """
    if state.draining:
        return
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        _LOG.error(
            "folder-watch task exited cleanly but unexpectedly; leaving stopped "
            "(/health will report degraded)"
        )
        return
    if state.folder_watch_restart_count >= MAX_FOLDER_WATCH_RESTARTS:
        _LOG.error(
            "folder-watch task crashed again (%r) but restart cap %d reached; "
            "leaving stopped — /health will report degraded",
            exc,
            MAX_FOLDER_WATCH_RESTARTS,
        )
        return
    state.folder_watch_restart_count += 1
    _LOG.error(
        "folder-watch task crashed (%r); restarting (restart #%d)",
        exc,
        state.folder_watch_restart_count,
        extra={"event": "folder_watch_restart"},
    )
    _start_supervised_folder_watch(state)


def _resume_durable_runtime_work(state: ServerState) -> None:
    """Discover every registered vault and restart its durable queued work.

    Review actions queued while a vault was busy restart their applier too.
    Runtime creation reads only queue/job sidecars. Graph handles stay closed
    until a worker takes its scoped lease, so startup scales independently of
    ``VaultPool.max_open``.
    """
    from okto_neuron.server import _ingest_queue, _jobs, _review_actions
    from okto_neuron.server.http import _companion

    for runtime in state.runtimes(discover=True):
        if _review_actions.has_queued(runtime):
            _review_actions.ensure_applier(runtime)
        if any(item.status == "queued" for item in runtime.ingest_queue):
            _ingest_queue.ensure_worker(runtime, _companion)
        if any(job.status == "queued" for job in runtime.curation_jobs):
            _jobs.ensure_worker(runtime)


def _vault_open_warning(vault_path: Path, exc: EmbeddingDimMismatch) -> dict[str, object]:
    resolved = Path(vault_path).expanduser().resolve(strict=False)
    return {
        "code": "embedding_dim_mismatch",
        "path": str(resolved),
        "detail": exc.user_message(),
        "remedy": (
            "Rebuild vectors at the configured embedding width through the running "
            "daemon (POST /api/v1/vaults/reembed with {\"vault\": \"<vault name>\"}, which "
            "works while the vault cannot be opened, or the vault manager's Re-embed "
            "button); `okto-neuron kg reembed` is refused while the daemon holds the "
            "vault. Or switch to another vault."
        ),
    }


def _open_startup_vault(
    vault_path: Path | None,
) -> tuple[Vault | None, Path | None, dict[str, object] | None]:
    if vault_path is None:
        return None, None, None
    resolved = Path(vault_path).expanduser().resolve(strict=False)
    from okto_neuron.consolidate.review_queue import layout_refusal, log_layout_refusal_once

    # BEFORE the writer lease and the graph open (#14): a v1 vault is refused
    # untouched; the daemon starts without it, status shows the same refusal.
    refusal = layout_refusal(resolved)
    if refusal is not None:
        log_layout_refusal_once(resolved, refusal)
        return None, None, dict(refusal)
    try:
        acquire_daemon_writer_lease(resolved)
    except VaultPoolError as exc:
        _LOG.warning("startup fallback vault is busy; starting the application without it: %s", exc)
        return (
            None,
            None,
            {
                "code": exc.code,
                "path": str(resolved),
                "detail": str(exc),
                "remedy": "Stop the process holding the vault, then select it again.",
            },
        )
    try:
        return Vault.open(resolved), resolved, None
    except EmbeddingDimMismatch as exc:
        warning = _vault_open_warning(resolved, exc)
        _LOG.warning(
            "startup fallback vault could not be opened; starting the application without it: %s",
            warning["detail"],
        )
        return None, None, warning


def _warm_llm_provider_dependencies(state: ServerState) -> None:
    """Import every discovered vault's LLM provider dependencies at daemon boot.

    ``LiteLLMProvider`` imports ``litellm`` lazily per call. A long-running
    daemon sharing its ``.venv`` with other ``uv run`` invocations can have
    non-default groups (litellm) pruned out from under it by an exact re-sync,
    after which EVERY ask/ingest 500s with ``ModuleNotFoundError`` (observed
    2026-07-07: a full 142-question A/B produced 284/284 empty answers this
    way). Importing at boot pins the modules in ``sys.modules`` for the process
    lifetime, making the running daemon immune to later venv mutation.

    Config discovery and loading are graph-free, so an application daemon that
    starts without a compatibility vault still warms the providers its vaults
    will use later. Providers are deduplicated across vaults. One unreadable
    vault config or missing dependency is logged and skipped rather than
    preventing the remaining providers from warming: recall/health/browse
    legitimately serve without an LLM, and the per-request typed error path
    still applies.
    """
    from okto_neuron.config import VaultConfig
    from okto_neuron.config._vault import _STEP_NAMES
    from okto_neuron.llm import LLMProviderError, warm_provider_dependencies
    from okto_neuron.vault_registry import list_vaults

    paths: set[Path] = set()
    if state.vault_path is not None:
        paths.add(state.vault_path.resolve(strict=False))
    try:
        paths.update(entry.path.resolve(strict=False) for entry in list_vaults())
    except Exception:  # noqa: BLE001 — warmup must never take the daemon down
        _LOG.debug("LLM dependency warmup could not discover registered vaults", exc_info=True)

    providers: set[str] = set()
    for path in sorted(paths):
        try:
            cfg = VaultConfig.load(path)
            if cfg.llm.enabled:
                providers.update(cfg.llm.resolved(step).provider for step in _STEP_NAMES)
        except Exception:  # noqa: BLE001 — one bad vault must not block the others
            _LOG.debug(
                "LLM dependency warmup skipped unreadable vault config: %s",
                path,
                exc_info=True,
            )

    for provider in sorted(providers):
        try:
            warm_provider_dependencies(provider)
        except LLMProviderError as exc:
            _LOG.error(
                "LLM provider '%s' is configured but its dependencies are missing — "
                "every ask/ingest will fail until this is fixed: %s",
                provider,
                exc,
            )
        else:
            _LOG.info("LLM provider dependencies warmed at boot: %s", provider)


class VaultResolutionError(Exception):
    """A per-connection vault selector could not be resolved (ADR 0014).

    ``code`` is a stable machine tag; the message is agent-readable (it is
    surfaced verbatim to MCP callers). Codes: ``no_vault``,
    ``vault_selector_required``, ``forbidden_path_selector``, ``unknown_vault``,
    ``ambiguous_vault``, ``bad_vault_name``, ``vault_unavailable``.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _safe_selector_label(selector: str | None) -> str:
    """Render a selector for a CLIENT-facing message without leaking a path.

    A NAME is the caller's own input and is safe to echo. A path-shaped selector
    is not: resolving it expands symlinks, so echoing a RESOLVED path can reveal
    more of the on-disk layout than the caller supplied. The absolute-path branch
    no longer echoes its resolved target (it names no path at all), but this
    label is applied to every selector shape reaching a client message, so the
    path-shaped case still collapses to a neutral phrase.
    """
    if not selector:
        return "the selected vault"
    candidate = selector.strip()
    if (
        not candidate
        or candidate.startswith("~")
        or "/" in candidate
        or "\\" in candidate
        or Path(candidate).is_absolute()
    ):
        return "the selected vault"
    return f"vault {candidate!r}"


def _vault_unavailable(
    exc: OSError,
    *,
    selector: str | None,
    stage: str,
) -> VaultResolutionError:
    """Envelope a filesystem failure as a typed, path-free client error.

    Covers ``OSError`` directly, and the ELOOP ``RuntimeError`` that
    ``pathlib.Path.resolve()`` substitutes for it: the caller at the guard seam
    unwraps that RuntimeError's ``__context__`` OSError and routes it here, so
    there is exactly one sanitiser. The operator log keeps full detail.

    WHY: ``OSError`` carries the offending absolute path in ``str(exc)``,
    ``.strerror`` context and ``.filename``. Reproduced live: a charset-valid
    300-character vault name reached ``os.stat`` and the MCP caller received
    ``[Errno 63] File name too long: '/Users/<user>/.marginalia/vaults/…'`` — the
    home directory and the internal vault layout, on a surface (``ask``/
    ``explore``) with no loopback gate. The length cap closes that one trigger;
    this envelope closes the CLASS. The operator still gets everything, in the log.

    ``vault_unavailable`` is a NEW code rather than a reuse: ``unknown_vault``
    tells the agent to run ``init_vault``, which is false remediation for EACCES
    or EIO on a vault that exists.
    """
    _LOG.exception(
        "vault resolution failed during %s for selector %r: %s (filename=%r)",
        stage,
        selector,
        exc,
        getattr(exc, "filename", None),
    )
    return VaultResolutionError(
        "vault_unavailable",
        f"{_safe_selector_label(selector)} could not be accessed "
        "(filesystem error); see the server log for details",
    )


def _pool_error(exc: VaultPoolError, *, selector: str | None) -> VaultResolutionError:
    """Re-shape a pool error for a CLIENT without its embedded absolute path.

    Pool messages are built as ``"...: {key}"`` where ``key`` is the resolved
    vault PATH (see ``_vault_pool.py``), and this boundary used to pass
    ``str(exc)`` through verbatim. The stable machine ``code`` is what callers
    branch on, so it is preserved; only the human text is sanitised, and the full
    original is logged for the operator.
    """
    _LOG.warning("vault pool error %s for selector %r: %s", exc.code, selector, exc, exc_info=True)
    # A width mismatch is a diagnosable, path-free fact with a working remedy: say so
    # instead of "see the server log" (a field report).
    mismatch = client_open_failure(exc)
    if mismatch is not None:
        return VaultResolutionError(mismatch[0], mismatch[1])
    return VaultResolutionError(
        exc.code,
        # No ``exc.code`` in the text: the MCP layer already renders it as
        # ``f"{exc.code}: {exc}"``.
        f"{_safe_selector_label(selector)} is not available; see the server log for details",
    )


def _resolve_vault_path_selector(
    selector: str | None,
    *,
    is_loopback: bool,
    state: ServerState,
) -> Path:
    """Validate a selector and return its immutable resolved vault path.

    Guard wrapper: any ``OSError`` escaping the resolution body becomes a typed,
    path-free ``vault_unavailable`` (see :func:`_vault_unavailable`), as does the
    one non-``OSError`` shape a filesystem failure can take here — see the ELOOP
    note below.
    """
    try:
        return _resolve_vault_path_selector_unguarded(
            selector, is_loopback=is_loopback, state=state
        )
    except VaultResolutionError:
        raise
    except OSError as exc:
        raise _vault_unavailable(exc, selector=selector, stage="resolution") from exc
    except RuntimeError as exc:
        # ``pathlib.Path.resolve()`` deliberately converts an ELOOP ``OSError``
        # into ``RuntimeError("Symlink loop from %r" % e.filename)`` (CPython
        # pathlib ``check_eloop``) — carrying the offending absolute path in its
        # text. ``vault_registry.resolve_vault_reference`` resolves every name,
        # so a symlink loop under a vault NAME used to escape this seam raw.
        #
        # The catch is constrained by SHAPE, not by type alone: ``check_eloop``
        # raises from inside ``except OSError``, so the ELOOP ``OSError`` is
        # always the RuntimeError's immediate ``__context__``. An unrelated
        # programming-error RuntimeError has no ``__context__``, or one that is
        # not an ELOOP ``OSError``, and is re-raised untouched below.
        cause = exc.__context__
        if not (isinstance(cause, OSError) and cause.errno == errno.ELOOP):
            raise
        raise _vault_unavailable(cause, selector=selector, stage="resolution") from exc


def _raise_if_inaccessible(target: Path) -> None:
    """Re-raise the real ``OSError`` behind a failed ``is_vault`` check.

    ``is_vault`` answers through ``Path.is_dir()``, which swallows ELOOP (and
    ENOENT/ENOTDIR/EBADF) into ``False``. On Python 3.12 a symlink loop never
    gets that far, because ``Path.resolve()`` raises first. On Python 3.13
    ``resolve(strict=False)`` no longer raises on a loop, so the loop reached
    ``is_vault``, came back ``False`` and was reported as ``unknown_vault``:
    wrong remediation (``init_vault``) and nothing in the operator log. A
    missing path (ENOENT/ENOTDIR) is a genuine unknown vault and returns; any
    other ``OSError`` (ELOOP, EACCES, EIO...) propagates to the guard seam,
    which envelopes it as ``vault_unavailable``. Same outcome on 3.12 and 3.13.
    """
    try:
        target.stat()
    except (FileNotFoundError, NotADirectoryError):
        return


def _resolve_vault_path_selector_unguarded(
    selector: str | None,
    *,
    is_loopback: bool,
    state: ServerState,
) -> Path:
    """Validate a selector and return its immutable resolved vault path."""
    from okto_neuron.vault_registry import (
        AmbiguousVaultNameError,
        is_vault,
        list_vaults,
        resolve_vault_reference,
        vault_path_for_name,
    )

    if not selector:
        if state.vault_path is not None:
            return Path(state.vault_path).resolve(strict=False)
        entries = list_vaults()
        try:
            preferred = resolve_vault_reference(None).resolve(strict=False)
        except AmbiguousVaultNameError as exc:
            raise VaultResolutionError("ambiguous_vault", str(exc)) from exc
        except (OSError, ValueError):
            preferred = None
        if preferred is not None and any(entry.path == preferred for entry in entries):
            return preferred
        if len(entries) == 1:
            return entries[0].path
        if not entries:
            raise VaultResolutionError(
                "no_vault",
                "no registered vault; create one in the application or with init_vault",
            )
        raise VaultResolutionError(
            "vault_selector_required",
            "multiple vaults are registered and no default is configured; pass "
            "?vault=<name> or configure a default vault",
        )

    candidate = Path(selector).expanduser()
    if candidate.is_absolute():
        if not is_loopback:
            raise VaultResolutionError(
                "forbidden_path_selector",
                "absolute-path vault selectors are restricted to loopback callers; "
                "use a registered vault name over a non-loopback connection",
            )
        target = candidate.resolve(strict=False)
        if not is_vault(target):
            _raise_if_inaccessible(target)
            # No path in the text. ``target`` is RESOLVED, so echoing it expands
            # symlinks and discloses a location the caller never supplied; and
            # ``candidate`` is ``expanduser()``-ed, so echoing that leaks HOME
            # for a ``~``-prefixed selector. The caller supplied the path, so it
            # already knows which one it asked about.
            raise VaultResolutionError(
                "unknown_vault",
                "no vault at the requested path; create one first (init_vault) or check the path",
            )
        return target

    try:
        # Validate the name, then let the registry owning boundary search every
        # configured root and raise its typed ambiguity error when necessary.
        vault_path_for_name(selector)
        target = resolve_vault_reference(selector)
    except AmbiguousVaultNameError as exc:
        raise VaultResolutionError("ambiguous_vault", str(exc)) from exc
    except ValueError as exc:
        raise VaultResolutionError("bad_vault_name", str(exc)) from exc
    if not is_vault(target):
        _raise_if_inaccessible(target)
        raise VaultResolutionError(
            "unknown_vault",
            f"no vault named {selector!r}; create it first with init_vault",
        )
    return target


def resolve_vault_selector(
    selector: str | None,
    *,
    is_loopback: bool,
    state: ServerState,
) -> Vault:
    """Compatibility resolver returning an unscoped live handle (ADR 0014).

    THIS IS THE MULTI-TENANT AUTH SEAM. Today resolution is purely
    name/path → vault; a future cloud deployment maps token → tenant → allowed
    vaults at exactly this point.

    Selector forms:

    * ``None`` / ``""`` → the explicit compatibility fallback, configured
      default, or sole registered vault, in that order. With multiple vaults and
      no default, raises ``vault_selector_required`` instead of guessing.
    * an absolute path → loopback-only (``forbidden_path_selector`` otherwise,
      mirroring the loopback-only posture of sensitive routes); must already be
      a vault (``unknown_vault``, with an ``init_vault`` hint).
    * anything else → a registered vault NAME, resolved via the registry
      (``bad_vault_name`` on an invalid name; ``unknown_vault`` if not yet a
      vault).

    New application/MCP code uses :func:`resolve_runtime_selector` and takes a
    scoped lease. This function remains for source compatibility and therefore
    pins compatibility handles because its caller lifetime is unknowable.
    """
    target = _resolve_vault_path_selector(selector, is_loopback=is_loopback, state=state)

    try:
        return state.vault_pool.get_or_open(target)
    except VaultPoolError as exc:
        raise _pool_error(exc, selector=selector) from exc
    except OSError as exc:
        raise _vault_unavailable(exc, selector=selector, stage="open") from exc


def _validate_name_only_override(override: str) -> str:
    """Validate a per-CALL vault override: registry NAMES ONLY, never a path.

    WHY THIS EXISTS: ``_resolve_vault_path_selector`` accepts an absolute path
    selector whenever ``is_loopback`` is true, and ``_mcp_request_selector``
    reports ``is_loopback=True`` whenever there is no HTTP context at all. A
    per-call ``vault=`` argument is agent-supplied, not a hand-edited client
    config, so allowing a path here would hand any agent an unfenced read of
    arbitrary on-disk vaults through that default-open loopback gate. Path-shaped
    values are therefore rejected outright; a caller who genuinely needs a path
    selector uses ``?vault=<path>`` on the connection, which goes through the real
    loopback check.
    """
    candidate = override.strip()
    if (
        candidate.startswith("~")
        or Path(candidate).is_absolute()
        or "/" in candidate
        or "\\" in candidate
        or _DRIVE_LETTER_RE.match(candidate) is not None
    ):
        raise VaultResolutionError(
            "forbidden_path_selector",
            "the per-call 'vault' argument accepts registered vault NAMES only "
            "(see list_vaults); pass a path with ?vault=<path> on the connection "
            "instead",
        )
    return candidate


def _canonical_vault_name(name: str) -> str:
    """Return the registry's own spelling of ``name``, or ``name`` unchanged.

    Matching is case-insensitive on the STRING, never on the resolved path: on a
    case-insensitive filesystem ``root/"BETA"`` is a real directory whose
    ``Path`` differs from the registered ``root/"beta"``, so path equality would
    silently miss and hand back the caller's casing.
    """
    from okto_neuron.vault_registry import list_vaults

    folded = name.casefold()
    try:
        entries = list_vaults()
    except OSError:  # registry unreadable — the echo is cosmetic, never fatal
        return name
    for entry in entries:
        if entry.name.casefold() == folded:
            return entry.name
    return name


def resolve_runtime_selector(
    selector: str | None,
    *,
    is_loopback: bool,
    state: ServerState,
    vault: str | None = None,
    override_ignored: list[str] | None = None,
) -> VaultRuntime:
    """Resolve a selector to its one immutable application runtime.

    ``vault`` is the OPTIONAL per-call override supplied as a tool argument. The
    connection's ``?vault=`` selector WINS over it: ``?vault=`` is a deliberate
    hand edit of the client config and must never be silently overridden by an
    agent-chosen argument. The override only applies when the connection supplied
    no selector, and is substituted into the selector position so that every
    downstream rule (configured default, sole vault, typed errors) is unchanged.

    It is nevertheless VALIDATED on every call — shape and resolvability — so a
    pinned connection cannot swallow a path-shaped or unknown name silently.

    ``override_ignored`` is an optional out-param (a caller-supplied list, the
    ``source_reads`` idiom): the ignored-but-valid override name is appended to
    it so the tool layer can echo ``vault_override_ignored``. Keeping it out of
    the return type leaves every existing caller's signature untouched.
    """
    if vault:
        # ALWAYS validate a supplied per-call ``vault``, even when the connection
        # selector wins and the value will be discarded. Validating only on the
        # path where it took effect meant a pinned connection silently swallowed
        # ``vault="../../../etc"`` and ``vault="nao-existe-xyz"`` alike, returning
        # a normal answer — a caller with a traversal attempt or a plain bug got
        # no signal at all. Reject path shapes BEFORE substitution: after it, the
        # resolver would accept an absolute path under the default-open loopback
        # flag.
        validated = _validate_name_only_override(vault)
        if not validated:
            # Whitespace-only override: ``_validate_name_only_override`` strips it
            # to "". Treat it as NO override (the pre-existing behaviour for
            # ``vault=""``) rather than as a name to resolve — resolving "" would
            # re-enter the default-vault branch and could raise
            # ``vault_selector_required`` on a call that was fine before.
            pass
        elif not selector:
            selector = validated
        else:
            # The connection selector still ROUTES the call — validation must
            # never change which vault serves it. We only prove the ignored name
            # would have resolved (raising ``unknown_vault`` when it would not),
            # and deliberately do NOT call ``state.runtime_for`` on it: that would
            # open/adopt a pool entry for a vault that is not serving this call.
            _resolve_vault_path_selector(validated, is_loopback=is_loopback, state=state)
            if override_ignored is not None:
                # Validate FIRST, echo second: an invalid name must error, never
                # be reported as a benign "ignored".
                # Echo the CANONICAL registered name, not the raw argument. The
                # echo used to be the raw string with only whitespace stripped,
                # so it normalised inconsistently: "  beta  " came back as "beta"
                # while "BETA" (which resolves on a case-insensitive filesystem)
                # came back as "BETA". One rule now: whatever the registry calls
                # the vault. The raw value is not worth echoing for audit — it is
                # the caller's own argument, and an invalid one already errors.
                override_ignored.append(_canonical_vault_name(validated))
    target = _resolve_vault_path_selector(selector, is_loopback=is_loopback, state=state)
    try:
        return state.runtime_for(target)
    except VaultPoolError as exc:
        raise _pool_error(exc, selector=selector) from exc
    except OSError as exc:
        raise _vault_unavailable(exc, selector=selector, stage="open") from exc


def _mcp_request_selector() -> tuple[str | None, bool]:
    """Read the current FastMCP request selector and loopback status."""
    selector: str | None = None
    is_loopback = True
    try:
        from fastmcp.server.dependencies import get_http_request  # type: ignore

        request = get_http_request()
    except RuntimeError:  # no HTTP context (in-memory client/tests) — the only
        # exception ``get_http_request`` raises for that case; anything else is
        # a real bug and must not be swallowed as "treat as local".
        request = None
    if request is not None:
        from okto_neuron.server.http import request_is_loopback

        selector = request.query_params.get("vault")
        is_loopback = request_is_loopback(request)
    return selector, is_loopback


def resolve_mcp_vault(state: ServerState) -> Vault:
    """Compatibility raw-handle resolver for an in-flight MCP call.

    Actual tool implementations use :func:`resolve_mcp_runtime` so their handle
    lifetime is fenced and counted. This name remains import-compatible.
    """
    selector, is_loopback = _mcp_request_selector()
    return resolve_vault_selector(selector, is_loopback=is_loopback, state=state)


def resolve_mcp_runtime(
    state: ServerState,
    vault: str | None = None,
    override_ignored: list[str] | None = None,
) -> VaultRuntime:
    """Resolve the immutable runtime for the in-flight MCP tool call.

    ``vault`` is the per-call tool-argument override (registry names only); the
    connection's ``?vault=`` selector takes precedence over it.
    """
    selector, is_loopback = _mcp_request_selector()
    return resolve_runtime_selector(
        selector,
        is_loopback=is_loopback,
        state=state,
        vault=vault,
        override_ignored=override_ignored,
    )


def _mcp_kg_add_allowed() -> bool:
    """Return whether an MCP sensitive write is allowed for the current request.

    The internal name predates retirement of ``kg_add``; current callers gate
    ``remember`` and ``init_vault``.
    """
    try:
        from fastmcp.server.dependencies import get_http_request  # type: ignore

        request = get_http_request()
    except RuntimeError:  # no HTTP context (in-memory client/tests) — the only
        # exception ``get_http_request`` raises for that case; anything else is
        # a real bug and must not fail OPEN on a sensitive-write gate.
        return True
    if request is None:
        return True

    from okto_neuron.server.http import request_is_loopback

    return request_is_loopback(request)


_DRIVE_LETTER_RE = re.compile(r"^[A-Za-z]:")
_BARE_SUFFIX_RE = re.compile(r"^\.[A-Za-z0-9]{1,5}$")


_MCP_HEARTBEAT_INTERVAL_S = 15.0
_MCP_TOKEN_PROGRESS_MIN_INTERVAL_S = 0.25


@contextlib.asynccontextmanager
async def _mcp_heartbeat(label: str = "ask"):
    """Send an MCP progress notification every interval while the body runs.

    Started with the tool call and cancelled in ``finally`` (no notification
    after completion). It never raises into the tool: no MCP context yields a
    no-op, and ``report_progress`` itself no-ops without a ``progressToken``.
    Progress is elapsed seconds; ``total`` is None (duration is unknown).
    """
    try:
        from fastmcp.server.dependencies import get_context  # type: ignore

        ctx = get_context()
    except (RuntimeError, ImportError):
        ctx = None
    if ctx is None:
        yield
        return
    started = time.monotonic()

    async def _beat() -> None:
        while True:
            await asyncio.sleep(_MCP_HEARTBEAT_INTERVAL_S)
            elapsed = time.monotonic() - started
            try:
                await ctx.report_progress(elapsed, None, f"{label} in progress ({elapsed:.0f}s)")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - telemetry must never break the call
                _LOG.debug("mcp heartbeat notification failed", exc_info=True)

    task = asyncio.create_task(_beat())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def _mcp_ask_progress_bridge():
    """Thread→loop bridge for ask's on_stage/on_token callbacks.

    ``companion.ask`` runs under ``job_io`` on a worker thread; the MCP
    ``Context.report_progress`` coroutine must run on the event loop. The loop
    and context are captured HERE (on the loop) because ``get_context()``
    reads context-local state a worker thread does not have. Returns
    ``(on_stage, on_token)``; both are None outside an MCP request (direct
    in-process callers), and a delivery failure is swallowed at debug — the
    answer is the product, the notifications are telemetry.

    ``on_token`` NEVER forwards text: it counts tokens and reports
    ``"synthesizing: N tokens"`` throttled to >=250 ms so a fast stream cannot
    flood the notification channel.
    """
    try:
        from fastmcp.server.dependencies import get_context  # type: ignore

        ctx = get_context()
    except (RuntimeError, ImportError):
        ctx = None
    if ctx is None:
        return None, None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None, None

    import threading

    tokens = {"count": 0}
    last_sent = {"at": 0.0}
    lock = threading.Lock()

    async def _send(progress: float | None, message: str) -> None:
        try:
            await ctx.report_progress(progress, None, message)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _LOG.debug("mcp ask progress notification failed", exc_info=True)

    def _report(message: str, progress: float | None) -> None:
        """Hand one notification to the loop from the worker thread."""
        try:
            asyncio.run_coroutine_threadsafe(_send(progress, message), loop)
        except RuntimeError:
            _LOG.debug("mcp ask progress loop is closed", exc_info=True)

    def on_stage(stage: str) -> None:
        # A numeric progress is required for an MCP progress notification to
        # reach clients: stages report the ordinal position (retrieving=0,
        # synthesising=0.5, done=1) so the message carries the NAME and the
        # number carries a monotonic sense of where the call is.
        _report(stage, {"retrieving": 0.0, "synthesizing": 0.5, "done": 1.0}.get(stage, 0.0))

    def on_token(_delta: str) -> None:
        with lock:
            tokens["count"] += 1
            now = time.monotonic()
            if now - last_sent["at"] < _MCP_TOKEN_PROGRESS_MIN_INTERVAL_S:
                return
            last_sent["at"] = now
            count = tokens["count"]
        _report(f"synthesizing: {count} tokens", float(count))

    return on_stage, on_token


def _raw_text_preview(source: str, limit: int = 80) -> str:
    """One-line, ``limit``-char preview of a RAW-TEXT remember source.

    Empty for path-shaped sources (the queue already shows their filename);
    for raw text, the first line flattened to a single line and truncated on
    a character boundary. Pure string work — no filesystem access."""
    if "\n" not in source:
        try:
            exists = Path(source).expanduser().exists()
        except (OSError, ValueError, RuntimeError):
            exists = False
        has_space = " " in source
        suffix = Path(source).suffix
        is_path_shaped = (
            exists
            or "/" in source
            or "\\" in source
            or _DRIVE_LETTER_RE.match(source) is not None
            or (source.startswith("~") and not has_space)
            or (not has_space and "/" not in source and _BARE_SUFFIX_RE.match(suffix) is not None)
        )
        if is_path_shaped:
            return ""
    first_line = source.lstrip().splitlines()[0] if source.strip() else ""
    first_line = " ".join(first_line.split())
    if len(first_line) <= limit:
        return first_line
    return first_line[: limit - 1].rstrip() + "\u2026"


def _materialize_raw_text_source(vault: Vault, source: str) -> str:
    """Detect a raw-text MCP ``remember`` source and durably materialize it.

    A multi-line ``source`` (contains ``\\n``) is ALWAYS raw text — no
    filesystem probe is attempted, so a multi-line note that happens to start
    with ``~`` can never trip the ``Path.expanduser()`` ``RuntimeError`` below.

    A single-line ``source`` is classified as a PATH (returned unchanged —
    it fails as a path error downstream if it doesn't resolve, rather than
    being silently ingested as a "document" containing the literal path
    string) when any of these fire:

    - it names an existing file OR directory (``Path.exists()``);
    - it contains ``/`` or ``\\\\``;
    - it matches a Windows drive-letter prefix (``^[A-Za-z]:``);
    - it starts with ``~`` AND has no spaces (a bare ``~user`` home-dir
      reference — ``~/...`` and ``~user/...`` are already caught by the
      slash check above; a tilde-led SENTENCE like "~5 minutes to set up"
      has spaces and is deliberately excluded, matching the no-spaces carve
      -out below);
    - it has no slash, no spaces, and a short (1-5 char) alphanumeric suffix
      (e.g. a bare ``note.pdf``) — a spaced phrase like "buy milk, no sugar"
      is NOT suffix-shaped this way and stays raw text.

    Anything else (including any single-line SENTENCE, even one starting
    with ``~``) is raw text. The ``Path.exists()`` probe is guarded against
    ``OSError``/``ValueError``/``RuntimeError`` — ``Path(source).expanduser()``
    raises ``RuntimeError`` for some ``~<not-a-username>`` prefixes, and that
    must not crash the ``remember`` call.

    Detected raw text is written to ``<vault>/.marginalia/sources/<safe
    name>`` — the same durable-copy convention REST ``/add`` and
    ``/api/v1/ingest`` use (a Block's provenance anchors to this path, so the
    bytes must persist on disk) — and that file's path is returned for
    ingestion. Uses the POOLED ``vault``'s own path, not ``state.vault_path``,
    so an explicitly selected runtime still lands in
    the right place. A filename collision with DIFFERENT prior content (the
    8-hex ``safe_source_filename`` hash is short enough to collide) is
    re-minted under the full source hash rather than overwritten, so an
    earlier source's byte-anchored provenance is never corrupted; identical
    content re-paste stays idempotent and reuses the same file.
    """
    if "\n" not in source:
        has_space = " " in source
        try:
            exists = Path(source).expanduser().exists()
        except (OSError, ValueError, RuntimeError):
            exists = False
        suffix = Path(source).suffix
        is_path_shaped = (
            exists
            or "/" in source
            or "\\" in source
            or _DRIVE_LETTER_RE.match(source) is not None
            or (source.startswith("~") and not has_space)
            or (not has_space and "/" not in source and _BARE_SUFFIX_RE.match(suffix) is not None)
        )
        if is_path_shaped:
            return source

    sources_dir = Path(vault.path) / ".marginalia" / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    filename = iq.safe_source_filename("", source)
    target = sources_dir / filename
    if target.exists():
        try:
            existing_content: str | None = target.read_text(encoding="utf-8")
        except OSError:
            existing_content = None
        if existing_content != source:
            stem = Path(filename).stem
            ext = Path(filename).suffix or ".md"
            full_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
            target = sources_dir / f"{stem}-{full_hash}{ext}"
    target.write_text(source, encoding="utf-8")
    return str(target)


def _build_mcp_app(state: ServerState):
    """Build a FastMCP ASGI app bound to the application runtime pool."""
    return _build_mcp_server(state).http_app()


def _vault_llm_model_hint(vault_path: Path) -> str | None:
    """Agent-facing warning for a freshly created vault that cannot ingest.

    A vault created with the application defaults inherits provider + api_base
    from ``LLMDefaults`` but an EMPTY model (discovery-first). Creation genuinely
    succeeds, so this is a hint on the result rather than an exception — but
    without it ``init_vault`` reports a clean creation for a vault whose first
    ``remember`` is refused. Mirrors the pre-flight guard in
    ``Companion.remember``. Returns ``None`` when the vault has a usable model or
    the config cannot be read (never let a hint break creation)."""
    try:
        from okto_neuron.config import LLMDefaults, VaultConfig

        cfg = VaultConfig.load(vault_path)
        resolved = cfg.llm.resolved("extraction")
        defaults = LLMDefaults()
        if (
            resolved.provider == defaults.provider
            and resolved.api_base == defaults.api_base
            and not str(resolved.model or "").strip()
        ):
            return (
                "no LLM model configured: this vault resolved to the built-in "
                "defaults with an empty model, so remember() will refuse. Set "
                "llm.defaults.model in the vault config (okto-neuron.yaml) to a model the endpoint serves."
            )
    except Exception:  # pragma: no cover - a hint must never fail creation
        return None
    return None


def _build_mcp_server(state: ServerState):
    """Build FastMCP tools over client-scoped, leased vault runtimes.

    Split out from :func:`_build_mcp_app` so the registered tool surface can be
    exercised in-process (FastMCP in-memory client) without binding a port.
    """
    try:
        from fastmcp import FastMCP  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise OptionalDependencyError(
            "FastMCP is not installed; install okto-neuron[serve] to run `okto-neuron serve`",
            cause=exc,
        ) from exc

    mcp = FastMCP(CLI_NAME, version=OKTO_NEURON_VERSION)

    # ── agent memory surface ─────────────────────────────────────────────────
    # A deliberately small graph-native surface: ASK a grounded question over the
    # subgraph, EXPLORE the graph by drilling into a topic/node, REMEMBER a new
    # source, and INIT_VAULT to create one. Each connection selects its own vault
    # via ``?vault=`` — resolved per call to an immutable runtime. Each operation
    # holds a scoped handle lease, and writes share that runtime's writer lock
    # with REST/background work for the same vault (ADR 0034).
    from okto_neuron.companion import AskRetrievalPolicy, SourceOutsideVaultError, ask_status
    from okto_neuron.companion import _vault_relative as _companion_vault_relative
    from okto_neuron.errors import IngestError
    from okto_neuron.server.http import (
        MAX_QUERY_K,
        companion_for,
        log_remember_failure,
        validity_subset,
    )
    from okto_neuron.vault_registry import list_vaults as _list_registry_vaults

    def _serving_vault_name(runtime: VaultRuntime) -> str | None:
        """The registry NAME of the vault serving this call — never a path.

        NON-DISCLOSURE (see the shouty note in ``list_vaults``): only the name is
        returned. The basename fallback is reachable only for a vault the caller
        themselves selected by path over a loopback ``?vault=<path>`` connection,
        so it discloses nothing the caller did not already supply."""
        target = Path(runtime.vault_path).resolve(strict=False)
        for entry in _list_registry_vaults():
            if entry.path.resolve(strict=False) == target:
                return entry.name
        return target.name or None

    def _resolve(
        vault_override: str | None = None,
        override_ignored: list[str] | None = None,
    ) -> VaultRuntime:
        try:
            return resolve_mcp_runtime(state, vault_override, override_ignored)
        except VaultResolutionError as exc:
            # Surface code-prefixed, agent-readable (matches the old _require_vault
            # RuntimeError style so existing MCP clients keep parsing the message).
            raise RuntimeError(f"{exc.code}: {exc}") from exc

    def _release_pair(pair: tuple[VaultRuntime, VaultLease[Vault]]) -> None:
        pair[1].release()

    async def _run_leased(
        io: Callable[..., Awaitable[Any]],
        lease: VaultLease[Vault],
        fn: Callable[..., Any],
        /,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run ``fn`` on ``io`` holding an already-taken ``lease``.

        The lease is released when the dispatched call settles, not when this
        await is cancelled: a cancelled caller must not hand the handle back to
        eviction while its worker is still reading it, and a call cancelled
        while still queued (it never runs) must not leak the lease.
        """
        task = asyncio.ensure_future(io(fn, *args, **kwargs))

        def _settled(done: asyncio.Future[Any]) -> None:
            lease.release()
            if not done.cancelled():
                done.exception()  # consume it: nobody awaits a cancelled caller's task

        task.add_done_callback(_settled)
        return await asyncio.shield(task)

    def _lease(
        vault_override: str | None = None,
        override_ignored: list[str] | None = None,
    ) -> tuple[VaultRuntime, VaultLease[Vault]]:
        runtime = _resolve(vault_override, override_ignored)
        try:
            return runtime, runtime.lease_vault()
        except VaultPoolError as exc:
            if exc.code == "vault_fenced":
                # A pool-level fence (e.g. reembed) is routine maintenance, not an
                # error condition — surface the same friendly "maintenance: ..."
                # wording ``remember``/``init_vault`` already use for it, instead
                # of the raw internal "vault_fenced: ..." pool message.
                if runtime.shutting_down:
                    raise RuntimeError("shutting_down: server is shutting down") from exc
                raise RuntimeError(
                    "maintenance: vault maintenance is in progress; try again shortly"
                ) from exc
            # Same leak class as the resolution seam, different seam: pool messages
            # embed the resolved absolute vault path (see ``_vault_pool.py``), so the
            # raw ``str(exc)`` must not reach an MCP client. ``_pool_error`` is the one
            # sanitiser — reused here so the two seams cannot drift — and it keeps the
            # stable machine ``code`` while logging the full original for the operator.
            sanitised = _pool_error(exc, selector=vault_override)
            raise RuntimeError(f"{sanitised.code}: {sanitised}") from exc

    @mcp.tool()
    async def ask(
        question: str,
        k: int = 20,
        hops: int = 1,
        vault: str | None = None,
        enable_subgraph: bool | None = None,
        source_block_policy: Literal["never", "on_coverage_miss", "always", "blend"] | None = None,
        seed_k: int | None = None,
        max_degree_per_seed: int | None = None,
        neighbour_budget_tokens: int | None = None,
        source_block_budget_tokens: int | None = None,
        coverage_threshold: float | None = None,
        min_claim_confidence: float | None = None,
        max_nodes: int | None = None,
        max_relationships: int | None = None,
        max_claims: int | None = None,
        relationship_types: list[str] | None = None,
        include_sources: bool = False,
    ) -> dict[str, object]:
        """Answer a question grounded in the knowledge graph, with citations.

        ``ask`` is one-shot — it synthesises a single answer with no chance to
        re-query — so it seeds *wide*: ``k`` (notes retrieved) defaults to 20,
        capped at ``MAX_QUERY_K``. Raise it for broad or multi-part questions;
        lower it to cut cost on narrow lookups. (If you can issue follow-up
        queries yourself, prefer ``explore``, which seeds tighter and returns
        structured graph to walk.) ``hops`` widens the graph neighbourhood
        around each seed (1 = direct neighbours; raise when an answer needs
        more connected context) — but it only takes effect when subgraph
        retrieval is active for this vault (``llm.ask.enable_subgraph``,
        default off; the default block-dump retrieval mode ignores ``hops``
        entirely). Returns ``{status, text, citations, subgraph_evidence_ids,
        retrieval}``; ``status`` is ``"ok"`` only for a clean answer and
        ``"degraded"`` for anything else (see ``synthesis_status`` below); the ``retrieval`` block echoes the effective ``seed_k``
        and ``mode``, plus ``enable_subgraph`` (whether this call actually
        used subgraph retrieval) and ``hops`` (the value applied, or ``null``
        when ``hops`` had no effect), so you can see what your knobs did. It also
        carries ``vault`` — the NAME of the vault that actually served the call —
        and, when your ``vault`` argument lost to the connection's ``?vault=``
        selector, ``vault_override_ignored`` with the CANONICAL registered name
        of the vault that was discarded (not your literal argument: casing and
        surrounding whitespace are normalised to the registry's spelling).
        ``citations`` is always just the retrieval seeds. When subgraph
        retrieval is active, ``text`` is grounded in a wider 1-hop+ ego-graph
        and may cite ``claim:<id>``/node ids from that wider graph that never
        appear in ``citations`` — ``subgraph_evidence_ids`` carries the full
        set of ids rendered into that ego-graph context so those anchors
        resolve. It is empty outside subgraph mode, where ``citations``
        already is the complete grounding set.

        ALWAYS check ``retrieval["synthesis_status"]``: an empty ``text`` with
        ``synthesis_status == "provider_error"`` (summary in
        ``retrieval["provider_error"]``) means the answering MODEL was
        unreachable, NOT that the graph lacks the answer — retry or fix the LLM
        config rather than concluding the vault is empty. ``"no_llm"`` means no
        usable LLM is configured for the vault, so no model was called
        (``retrieval["no_llm_reason"]`` says what to set); the citations are
        still the retrieval hits. ``"empty"`` means the
        model replied with nothing; ``"ok"`` means synthesis succeeded.
        ``"truncated"`` means the provider hit the token budget (finish_reason
        ``length``) so ``text`` is cut off mid-answer, and ``"abnormal_stop"``
        means it stopped for some other non-``stop`` reason — in both cases
        ``retrieval["finish_reason"]`` (plus ``retrieval["native_finish_reason"]``
        when the provider's raw value differs) says exactly which.

        ``vault`` optionally names the registered vault to read (a NAME from
        ``list_vaults`` — never a path); omit it to use this connection's vault.
        It is ALWAYS validated: a path-shaped or unknown name fails the call even
        when this connection's ``?vault=`` selector takes precedence over it.
        Before choosing, check the project directory for a ``.okto-neuron-vault``
        file, or a pre-0.3.0 ``.marginalia-vault`` (``{"vault": "<name>"}``), and pass the name it pins.

        Retrieval-policy knobs (parity with the web UI's query controls). EVERY
        one defaults to ``None`` = inherit the vault/config default for this
        call; a ``None`` never overwrites a configured value. ``k`` and
        ``seed_k`` are both capped at ``MAX_QUERY_K`` = 100.

        - ``enable_subgraph``: use graph (ego-graph) retrieval instead of the
          default source-block dump. Default ``None`` (inherit; normally OFF).
        - ``source_block_policy``: when to splice raw source blocks into context
          — ``never`` / ``on_coverage_miss`` / ``always`` / ``blend``.
        - ``seed_k``: how many retrieval seeds to fetch (overrides ``k``).
        - ``hops``: ego-graph radius, clamped to 1..5; takes effect ONLY when
          subgraph retrieval is active (block-dump mode ignores it entirely).
        - ``max_degree_per_seed``: max neighbours expanded per seed node.
        - ``neighbour_budget_tokens``: token budget for the neighbour context.
        - ``source_block_budget_tokens``: token budget for spliced source blocks.
        - ``coverage_threshold``: 0.0-1.0 graph-coverage bar below which source
          blocks are spliced in.
        - ``min_claim_confidence``: 0.0-1.0 floor on claim confidence.
        - ``max_nodes`` / ``max_relationships`` / ``max_claims``: hard caps on
          what the assembled subgraph renders.
        - ``relationship_types``: restrict edges to these predicate names.
        - ``include_sources``: when true, add a ``sources`` list with per-hit
          provenance (vault-RELATIVE path, ``block_id``, byte span,
          ``content_hash``, plus ``superseded``/``valid_until`` when stale).
          Default false, so the existing payload is unchanged.

        An out-of-range knob (e.g. ``coverage_threshold=1.5``) fails the call
        with a readable ``invalid retrieval policy: ...`` error.
        """
        if state.shutting_down:
            raise RuntimeError("shutting_down: server is shutting down")
        # P2: heartbeat + stage/token progress. Captured HERE, on the event
        # loop, before the job-executor thread runs the answer (the bridge's
        # own docstring explains why). No MCP context => both None => the
        # companion call is exactly today's.
        on_stage, on_token = _mcp_ask_progress_bridge()
        # Resolving reads the registry and leasing may open the vault: take the
        # lease off-loop first, so a call waiting on another vault's open never
        # occupies a job worker. The answer itself is seconds to minutes: job executor.
        ignored: list[str] = []
        runtime, lease = await acquire_off_loop(_lease, vault, ignored, release=_release_pair)
        async with _mcp_heartbeat("ask"):
            return await _run_leased(
                job_io,
                lease,
                functools.partial(
                    _ask_impl,
                    runtime=runtime,
                    lease=lease,
                    ignored=ignored,
                    question=question,
                    k=k,
                    hops=hops,
                    enable_subgraph=enable_subgraph,
                    source_block_policy=source_block_policy,
                    seed_k=seed_k,
                    max_degree_per_seed=max_degree_per_seed,
                    neighbour_budget_tokens=neighbour_budget_tokens,
                    source_block_budget_tokens=source_block_budget_tokens,
                    coverage_threshold=coverage_threshold,
                    min_claim_confidence=min_claim_confidence,
                    max_nodes=max_nodes,
                    max_relationships=max_relationships,
                    max_claims=max_claims,
                    relationship_types=relationship_types,
                    include_sources=include_sources,
                    on_stage=on_stage,
                    on_token=on_token,
                ),
            )

    def _ask_impl(
        runtime: VaultRuntime,
        lease: VaultLease[Vault],
        ignored: list[str],
        question: str,
        k: int = 20,
        hops: int = 1,
        enable_subgraph: bool | None = None,
        source_block_policy: Literal["never", "on_coverage_miss", "always", "blend"] | None = None,
        seed_k: int | None = None,
        max_degree_per_seed: int | None = None,
        neighbour_budget_tokens: int | None = None,
        source_block_budget_tokens: int | None = None,
        coverage_threshold: float | None = None,
        min_claim_confidence: float | None = None,
        max_nodes: int | None = None,
        max_relationships: int | None = None,
        max_claims: int | None = None,
        relationship_types: list[str] | None = None,
        include_sources: bool = False,
        on_stage=None,
        on_token=None,
    ) -> dict[str, object]:
        if state.shutting_down:
            raise RuntimeError("shutting_down: server is shutting down")
        with lease as selected_vault:
            # Default to the vault's configured retrieval mode (block) — the grounded
            # eval showed block-dump answers (0.792) beat the subgraph path (0.6) while
            # the graph stays extraction-thin; forcing subgraph here served the worse
            # path. ``hops`` still applies once subgraph is enabled in vault config.
            effective_hops = max(1, min(int(hops), 5))
            # Only the knobs the caller actually set are handed to the policy, so
            # an unset param stays None-means-inherit and can never clobber a
            # vault default with a hardcoded value. ``enable_subgraph`` included:
            # omitted => the vault config decides, exactly as before.
            overrides: dict[str, object] = {
                "enable_subgraph": enable_subgraph,
                "source_block_policy": source_block_policy,
                "seed_k": min(int(seed_k), MAX_QUERY_K) if seed_k is not None else None,
                "max_degree_per_seed": max_degree_per_seed,
                "neighbour_budget_tokens": neighbour_budget_tokens,
                "source_block_budget_tokens": source_block_budget_tokens,
                "coverage_threshold": coverage_threshold,
                "min_claim_confidence": min_claim_confidence,
                "max_nodes": max_nodes,
                "max_relationships": max_relationships,
                "max_claims": max_claims,
                "relationship_types": tuple(relationship_types)
                if relationship_types is not None
                else None,
            }
            overrides = {key: value for key, value in overrides.items() if value is not None}
            try:
                policy = AskRetrievalPolicy(hops=effective_hops, **overrides)  # type: ignore[arg-type]
            except ValidationError as exc:
                first = exc.errors()[0] if exc.errors() else {}
                loc = ".".join(str(part) for part in first.get("loc", ())) or "retrieval policy"
                raise RuntimeError(
                    f"invalid retrieval policy: {loc}: {first.get('msg', 'invalid value')}"
                ) from exc
            # Only pass the hooks when they exist so injected test companions
            # (and any older Companion) keep their exact signatures honored.
            ask_kwargs: dict[str, object] = {"retrieval_policy": policy}
            if on_stage is not None:
                ask_kwargs["on_stage"] = on_stage
            if on_token is not None:
                ask_kwargs["on_token"] = on_token
            answer = companion_for(selected_vault).ask(
                question, k=min(int(k), MAX_QUERY_K), **ask_kwargs
            )
            sources: list[dict[str, object]] = []
            if include_sources:
                # NON-DISCLOSURE: QueryHit.provenance.path is ABSOLUTE, and ask has
                # no loopback gate (unlike remember/init_vault), so it is reachable
                # remotely under --allow-remote. Emit ONLY the vault-relative form;
                # when relativization fails the path key is omitted entirely.
                # NEVER add the absolute path (or the vault root) back here.
                vault_root = selected_vault.path
                for hit in getattr(answer, "hits", ()):
                    prov = hit.provenance
                    entry: dict[str, object] = {
                        "id": str(hit.node.id),
                        "block_id": prov.block_id,
                        "byte_start": prov.byte_start,
                        "byte_end": prov.byte_end,
                        "content_hash": prov.content_hash,
                    }
                    relative = _companion_vault_relative(prov.path, vault_root)
                    if relative:
                        entry["path"] = relative
                    # ADR 0024 validity subset, shared with the REST serializer.
                    entry.update(validity_subset(hit.node))
                    sources.append(entry)
        retrieval = dict(answer.retrieval)
        enable_subgraph_effective = retrieval.get("mode") == "subgraph"
        retrieval["enable_subgraph"] = enable_subgraph_effective
        retrieval.setdefault("hops", effective_hops if enable_subgraph_effective else None)
        # Which vault actually answered. Injected HERE, in the MCP layer, so the
        # companion's trace (and its REST consumers / regression pins) is untouched.
        retrieval["vault"] = _serving_vault_name(runtime)
        if ignored:
            # The connection's ?vault= won; say so instead of letting the caller
            # believe their argument routed the call.
            retrieval["vault_override_ignored"] = ignored[0]
        return {
            # "ok" only for a clean answer; "degraded" otherwise, with the
            # reason in retrieval.synthesis_status (same rule as REST /ask).
            "status": ask_status(retrieval),
            "text": answer.text,
            "citations": list(answer.citations),
            # 3.19: the full ego-graph grounding pool for subgraph-mode
            # answers (empty outside subgraph mode, where citations already
            # is the complete grounding set) — see Answer.subgraph_evidence_ids.
            "subgraph_evidence_ids": list(answer.subgraph_evidence_ids),
            "retrieval": retrieval,
            **({"sources": sources} if include_sources else {}),
        }

    @mcp.tool()
    async def explore(
        topic: str = "",
        node_id: str | None = None,
        hops: int = 1,
        k: int = 12,
        vault: str | None = None,
        relationship_types: list[str] | None = None,
        min_claim_confidence: float | None = None,
        max_degree_per_seed: int | None = None,
    ) -> dict[str, object]:
        """Drill into the graph around a topic, then walk outward by node id.

        ``explore`` is the agentic retrieval path — you get back structured graph,
        not prose, and you re-query to widen. So it seeds *tight*: ``k`` (seed
        notes) defaults to 12, capped at ``MAX_QUERY_K``, keeping each pass cheap
        and high-precision; you widen by calling ``explore`` again on a returned
        node's ``id`` rather than by inflating ``k``. (For a single one-shot
        answer with no follow-up, use
        ``ask``, which seeds wider.) Give a ``topic`` (free text) to seed by
        semantic search, OR a ``node_id`` (from a prior ``explore``/``ask`` result)
        to expand directly from that node. ``hops`` widens the neighbourhood per
        call. Returns the structured ego-graph — ``nodes`` (each with an ``id``),
        ``relationships``, and ``claims`` — NOT prose. A ``retrieval`` block
        reports how the call actually retrieved: ``mode`` (``topic`` or
        ``node``), the effective ``seed_k`` (``null`` in ``node`` mode, where
        ``k`` is unused), ``hops``, ``max_degree_per_seed``,
        ``min_claim_confidence``, ``relationship_types`` (``[]`` = unrestricted)
        and ``vault`` (the NAME of the vault that served the call), plus
        ``vault_override_ignored`` when your ``vault`` argument lost to the
        connection's ``?vault=`` selector — the CANONICAL registered name of the
        discarded vault, not your literal argument. There is no
        ``synthesis_status``:
        ``explore`` makes no LLM call.

        ``relationship_types`` restricts edges to those predicate names;
        ``min_claim_confidence`` (0.0-1.0) floors claim confidence;
        ``max_degree_per_seed`` caps neighbours expanded per seed. All three
        default to ``None`` = inherit this vault's ``llm.ask`` config.
        Each returned relationship/claim carries the source ``block_id``.

        ``vault`` optionally names the registered vault to read (a NAME from
        ``list_vaults`` — never a path); omit it to use this connection's vault.
        It is ALWAYS validated: a path-shaped or unknown name fails the call even
        when this connection's ``?vault=`` selector takes precedence over it.
        Before choosing, check the project directory for a ``.okto-neuron-vault``
        file, or a pre-0.3.0 ``.marginalia-vault`` (``{"vault": "<name>"}``), and pass the name it pins.
        """
        if state.shutting_down:
            raise RuntimeError("shutting_down: server is shutting down")
        # Pure graph read (no LLM): the whole call is one store op (issue #13),
        # after a lease taken off-loop so waiting on an open never holds a worker.
        ignored: list[str] = []
        runtime, lease = await acquire_off_loop(_lease, vault, ignored, release=_release_pair)
        return await _run_leased(
            store_io,
            lease,
            _explore_impl,
            runtime,
            lease,
            ignored,
            topic,
            node_id,
            hops,
            k,
            relationship_types,
            min_claim_confidence,
            max_degree_per_seed,
        )

    def _explore_impl(
        runtime: VaultRuntime,
        lease: VaultLease[Vault],
        ignored: list[str],
        topic: str,
        node_id: str | None,
        hops: int,
        k: int,
        relationship_types: list[str] | None,
        min_claim_confidence: float | None,
        max_degree_per_seed: int | None,
    ) -> dict[str, object]:
        if state.shutting_down:
            raise RuntimeError("shutting_down: server is shutting down")
        with lease as selected_vault:
            result = companion_for(selected_vault).explore(
                topic,
                node_id=node_id,
                hops=hops,
                k=min(int(k), MAX_QUERY_K),
                relationship_types=tuple(relationship_types)
                if relationship_types is not None
                else None,
                min_claim_confidence=min_claim_confidence,
                max_degree_per_seed=max_degree_per_seed,
            )
        retrieval = dict(result.get("retrieval") or {})
        retrieval["vault"] = _serving_vault_name(runtime)
        if ignored:
            retrieval["vault_override_ignored"] = ignored[0]
        result["retrieval"] = retrieval
        return result

    @mcp.tool()
    async def remember(
        source: str,
        sensitivity: Literal["local_only", "default"] = "default",
        vault: str | None = None,
    ) -> dict[str, object]:
        """Validate, materialize, and ENQUEUE a source for async ingest.

        ASYNC (P1): this returns immediately — ``{"job_id", "status":
        "queued", "vault" (name, never a path), "source_name", "poll":
        "ingest_status"}`` — after validation and raw-text materialization;
        the vault's ingest-queue worker performs the remember under the
        writer lock. Poll ``ingest_status(job_id)`` for stage/blocks progress
        and the terminal committed/queued counts.

        WRITE op — loopback-only. ``source`` is EITHER a file path OR raw
        text. Raw text is detected conservatively: multi-line strings are
        always raw text; a single-line string counts as raw text only when it
        is NOT path-shaped (path-shaped = an existing file/dir, contains a
        slash or backslash, a Windows drive prefix, a space-free ``~``
        prefix, or a space-free name with a short file suffix like
        ``note.pdf``). A path-shaped source is read IN PLACE and never copied:
        it must be an existing file inside the vault root or a configured
        folder-watch root (``folder_watch.roots`` in the vault config); a
        missing path fails loudly as a path error, and a file outside those
        roots is refused with ``forbidden: ...`` naming the allowed roots and
        how to proceed (copy it under one, add its folder to
        ``folder_watch.roots``, or pass its text as raw text). Detected raw
        text is the only form that is copied: it is materialized to a durable
        ``.marginalia/sources/`` file (same convention as REST /add and
        /api/v1/ingest) and THAT copy is enqueued, so the vault keeps its own
        text. ``sensitivity`` must be exactly ``local_only`` or ``default``
        (enforced by the tool schema); it travels with the queue item and the
        worker honors it: ``local_only`` keeps the source off any remote LLM
        path — the ingest FAILS unless the vault's LLM is local, meaning its
        api_base has no host (stub), or its host is ``localhost``/loopback, a
        private RFC1918 address (10/8, 172.16/12, 192.168/16), an IPv6
        unique-local (fc00::/7) or link-local address. Hostnames are NOT
        local (a DNS name can point anywhere).

        ``vault`` optionally names the registered vault to write to (a NAME
        from ``list_vaults`` — never a path); omit it to use this connection's
        vault. Before choosing, check the project directory for a
        ``.okto-neuron-vault`` file, or a pre-0.3.0 ``.marginalia-vault``
        (``{"vault": "<name>"}``), and pass the name it pins.
        """
        # WRITE op — loopback-only, even under --allow-remote (writes never
        # widen). Matches REST /remember and the init_vault gate below.
        if not _mcp_kg_add_allowed():
            raise RuntimeError(
                "forbidden: remember is restricted to loopback callers; "
                "tunnel (e.g. SSH) to write remotely"
            )
        sens = sensitivity
        # Resolution happens here, AFTER the loopback write gate above, so an
        # unknown vault name fails loudly before anything is written. Resolving
        # reads the registry and leasing may open the vault: off-loop.
        runtime, lease = await acquire_off_loop(_lease, vault, release=_release_pair)
        if runtime.draining:
            lease.release()
            if runtime.shutting_down:
                raise RuntimeError("shutting_down: server is shutting down")
            raise RuntimeError("maintenance: vault maintenance is in progress; writes are paused")
        from okto_neuron.server.http import _companion as _queue_companion

        try:
            with lease as selected_vault:
                # Fail fast on the integrity fence; the worker re-checks per
                # item under the writer lock, but a fenced vault should refuse
                # the enqueue, not accept work it will reject.
                await store_io(
                    graph_integrity.require_write_allowed, runtime, selected_vault
                )
                ingest_source = await store_io(
                    _materialize_raw_text_source, selected_vault, source
                )
                # Fail fast on an out-of-tree PATH source exactly the way the
                # inline flow did (field report: the refusal must name the
                # roots and the remedies AT CALL TIME) — the worker would
                # otherwise only fail the job after the caller has moved on.
                # Raw-text sources are already under .marginalia/sources and
                # pass trivially; missing paths pass through (the ingest
                # reader fails closed on them, as before).
                from okto_neuron.companion import (
                    _source_is_ingestable_path,
                    _source_outside_roots_message,
                )
                from okto_neuron.config import VaultConfig

                # The config read is YAML I/O: off-loop (event-loop guard).
                watch_roots = list(
                    await store_io(
                        lambda: VaultConfig.load(
                            Path(selected_vault.path)
                        ).folder_watch.roots
                    )
                )
                if not _source_is_ingestable_path(
                    ingest_source, selected_vault.path, watch_roots
                ):
                    raise SourceOutsideVaultError(
                        _source_outside_roots_message(
                            ingest_source, selected_vault.path, watch_roots
                        )
                    )
                # A PATH-shaped source that does not exist fails loudly as a
                # path error at call time (the docstring's contract) instead
                # of enqueueing a job that only fails in the worker.
                if ingest_source == source and not Path(str(ingest_source)).expanduser().is_file():
                    raise RuntimeError(
                        f"bad_path: source does not exist: {ingest_source}"
                    )
                item = iq.enqueue_materialized(
                    runtime,
                    ingest_source,
                    sensitivity=sens,
                    preview=_raw_text_preview(source),
                )
                iq.ensure_worker(runtime, _queue_companion)
                await store_io(iq.persist, runtime)
        except graph_integrity.IntegrityFenceError as exc:
            raise RuntimeError(str(exc)) from exc
        except SourceOutsideVaultError as exc:
            # Normalized the same way REST's /remember does (403 forbidden)
            # instead of leaking the raw exception type.
            log_remember_failure(exc, source, _LOG)
            raise RuntimeError(f"forbidden: {exc}") from exc
        except IngestError as exc:
            # Materialization-time ingest errors keep the client wording the
            # inline path always used.
            log_remember_failure(exc, source, _LOG)
            raise RuntimeError(
                f"ladybug_write_failed: ladybug write failed: {exc}"
            ) from exc
        vault_name = await store_io(_serving_vault_name, runtime)
        return {
            "job_id": item.id,
            "status": "queued",
            "vault": vault_name,
            "source_name": item.name,
            "poll": "ingest_status",
        }

    @mcp.tool()
    async def ingest_status(
        job_id: str, vault: str | None = None
    ) -> dict[str, object]:
        """Poll an async ``remember`` job (the ``job_id`` it returned).

        ``status`` is ``queued | processing | done | error | cancelled``;
        ``ok`` is the caller-friendly verdict: ``true`` ONLY for a finished
        job whose ingest actually yielded — a provider error with zero yield
        or a failed/integrity-failed outcome quality is ``status=error`` and
        ``ok=false`` with the reason in ``error``/``provider_error``.
        ``stage``/``blocks_done``/``blocks_total`` carry the within-file
        progress (stage: queued | parsing | extracting | embedding | dedup |
        committing | done | error), ``committed``/``queued`` the terminal
        curation counts, ``document_id`` the ingested document once known.
        ``vault`` is the serving vault's NAME (never a path). A ``job_id``
        that never existed (or belongs to another vault) fails loudly as
        ``not_found``. A ``local_only`` sensitivity job can only succeed on a
        vault whose LLM is local (loopback, RFC1918, IPv6 ULA fc00::/7, or
        link-local api_base host — see ``remember``).
        """
        runtime, lease = await acquire_off_loop(_lease, vault, release=_release_pair)
        with lease:
            detail = await iq.item_detail_async(runtime, job_id)
            # The registry scan in _serving_vault_name reads YAML: off-loop
            # (the event-loop guard pins this tool to never block the loop).
            vault_name = await store_io(_serving_vault_name, runtime)
        if detail is None:
            raise RuntimeError(
                f"not_found: no ingest job {job_id!r} on this vault; "
                "job ids come from remember's queued result"
            )
        queued_item = detail["item"]
        outcome = queued_item.get("outcome") if isinstance(queued_item.get("outcome"), dict) else {}
        quality = str(outcome.get("quality") or "").strip()
        zero_yield = bool(
            queued_item.get("provider_error")
            and int(queued_item.get("committed") or 0) == 0
            and int(queued_item.get("queued") or 0) == 0
            and int(queued_item.get("claims") or 0) == 0
        )
        # Mirrors the drain worker's own terminal rule: done AND actually
        # yielded. Zero-yield/failed-quality items are status=error already;
        # the explicit recomputation keeps legacy sidecar items honest too.
        ok = queued_item.get("status") == "done" and not zero_yield and quality not in {
            "failed",
            "integrity_failed",
        }
        return {
            "job_id": queued_item.get("id"),
            "status": queued_item.get("status"),
            "ok": ok,
            "stage": queued_item.get("stage"),
            "blocks_done": int(queued_item.get("blocks_done") or 0),
            "blocks_total": int(queued_item.get("blocks_total") or 0),
            "committed": int(queued_item.get("committed") or 0),
            "queued": int(queued_item.get("queued") or 0),
            "document_id": (str(queued_item.get("document_id") or "") or None),
            "llm_disabled": bool(queued_item.get("llm_disabled") or False),
            "error": queued_item.get("error"),
            "provider_error": queued_item.get("provider_error"),
            "vault": vault_name,
            "source_name": queued_item.get("name"),
        }


    @mcp.tool()
    async def list_vaults() -> dict[str, object]:
        """List the vault NAMES this server can reach, so you can pick one.

        Use this to discover what exists, then pass ``vault=<name>`` to
        ``ask``/``explore``/``remember`` to work against a specific vault. Before
        picking, check the project directory you are working in for a
        ``.okto-neuron-vault`` file, or a pre-0.3.0 ``.marginalia-vault`` (JSON,
        ``{"vault": "<name>"}``) — when present
        it pins this project's vault and its name is the one to pass. The server
        deliberately does NOT read that file (one daemon serves every project and
        its working directory is not yours), so reading it is your job.

        Returns ``{vaults: [{name, current, backend}, ...]}`` where ``current``
        marks the vault THIS CONNECTION resolves to (what an ``ask``/``explore``
        without ``vault=`` would read) and ``backend`` is the pinned graph
        backend.
        """
        if state.shutting_down:
            raise RuntimeError("shutting_down: server is shutting down")
        # Registry scan + selector resolution read YAML: one store op (issue #13).
        return await store_io(_list_vaults_impl)

    def _list_vaults_impl() -> dict[str, object]:
        # DELIBERATE NON-DISCLOSURE: this surface returns NAMES ONLY. No ``path``,
        # and no ``id`` either (the id is derived from the vault path). Do not
        # "fix" this by re-adding them or by switching to VaultEntry.to_json() /
        # http._vaults_payload — both leak filesystem layout to every agent that
        # can reach the MCP port. Names are all a caller needs to pass ``vault=``.
        # ``current`` means "the vault THIS CONNECTION would use", not "the vault
        # the daemon booted with". Computing it from state.vault_path reported
        # current=false for the very vault a ?vault=<name> connection was pinned
        # to (the daemon starts with vault=(none)), so no payload anywhere told a
        # caller which vault it was talking to.
        current = Path(state.vault_path).resolve(strict=False) if state.vault_path else None
        try:
            selector, is_loopback = _mcp_request_selector()
            current = _resolve_vault_path_selector(
                selector, is_loopback=is_loopback, state=state
            ).resolve(strict=False)
        except VaultResolutionError:
            # DISCOVERY TOOL — it must keep listing. On a multi-vault server with
            # no default, resolution raises ``vault_selector_required``; that is
            # exactly when a caller most needs the list, so fall back to the
            # server-level vault rather than propagating.
            pass
        entries = _list_registry_vaults(current=current)
        return {
            "vaults": [
                {"name": entry.name, "current": entry.current, "backend": entry.backend}
                for entry in entries
            ]
        }

    @mcp.tool()
    async def init_vault(
        name: str,
        packs: str = "core",
        backend: str | None = None,
        storage_uri: str | None = None,
        storage_credential_env: str | None = None,
        storage_database: str | None = None,
        allow_remote_db: bool = False,
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        embedding_dimension: int | None = None,
        embedding_api_base: str | None = None,
        embedding_api_key_env: str | None = None,
        embedding_allow_remote: bool = False,
    ) -> dict[str, object]:
        """Create one application-managed named vault without selecting it.

        Loopback-only (local agents). ``name`` must be a valid vault name; ``packs``
        is a comma-separated list (default ``core``). The new vault is created under
        the global vault root (``~/.okto-neuron/vaults/<name>``, or ``~/.marginalia/vaults/<name>`` on an upgraded install) and adopted into the
        pool so a follow-up ``?vault=<name>`` connection resolves instantly.
        Existing browser tabs, request selectors, and the configured fallback are
        unchanged. The ownership marker makes the new vault deletable through the
        application after exact confirmation.

        ``backend`` pins the vault's graph backend (e.g. ``"grafx"``, ``"ladybug"``,
        ``"neo4j"``); defaults to the product default (``grafx``, D-94) when omitted.
        ``storage_uri``/``storage_credential_env``/``storage_database`` configure a
        server-side backend such as Neo4j (URI, name of the env var holding the
        credential, and database/keyspace name, respectively) and are ignored for an
        embedded backend. A non-loopback ``storage_uri`` requires ``allow_remote_db``
        set ``True`` to confirm remote storage egress, mirroring the REST vault-create
        route and the CLI's ``--allow-remote-db``; omitting it raises a clear error
        instead of silently connecting to a remote endpoint. Returns ``{name, path}``,
        plus ``hint`` when the new vault has no usable LLM model — creation succeeded
        but ``remember`` will refuse until ``llm.defaults.model`` is set.

        ``embedding_provider``/``embedding_model``/``embedding_dimension``/
        ``embedding_api_base``/``embedding_api_key_env``/``embedding_allow_remote`` create
        the vault with its embedding spec, so its graph is born at that width (the vector
        column is fixed-width afterwards; changing the width later needs a re-embed).
        Omit them all for the application default (a 384-wide local model). A width other
        than 384 must name the provider/model that produces it. ``embedding_api_base`` is
        loopback-only unless ``embedding_allow_remote`` is ``True``; ``embedding_api_key_env``
        must name an ``OKTO_NEURON_*`` variable. When given, the result also carries the
        ``embedding`` spec the vault was created with.
        """
        if state.draining:
            if state.shutting_down:
                raise RuntimeError("shutting_down: server is shutting down")
            raise RuntimeError("maintenance: vault maintenance is in progress; writes are paused")
        if not _mcp_kg_add_allowed():
            raise RuntimeError("forbidden: vault creation is restricted to loopback callers")

        from okto_neuron.config._vault import DEFAULT_NEW_VAULT_BACKEND, _classify_storage_endpoint
        from okto_neuron.server.http import (
            _BadRequest,
            _initialize_managed_vault,
            _parse_embedding_spec,
            _parse_packs,
        )
        from okto_neuron.store.registry import NoSuchBackendError, resolve_graph_backend
        from okto_neuron.vault_registry import (
            ensure_global_layout,
            is_vault,
            vault_path_for_name,
        )

        def _init_target(vault_name: str) -> tuple[Path, bool]:
            """Store op: ensure the app layout, map the name, probe the path."""
            ensure_global_layout()
            resolved = vault_path_for_name(vault_name).resolve(strict=False)
            return resolved, is_vault(resolved)

        resolved_backend = (backend or "").strip() or DEFAULT_NEW_VAULT_BACKEND
        try:
            # Entry-point discovery reads installed package metadata: off-loop.
            await store_io(resolve_graph_backend, resolved_backend)
        except NoSuchBackendError as exc:
            raise RuntimeError(f"bad_request: {exc}") from exc

        if storage_uri:
            try:
                endpoint_class = _classify_storage_endpoint(storage_uri.strip(), resolve=False)
            except ValueError as exc:
                raise RuntimeError(f"bad_request: {exc}") from exc
            if endpoint_class != "loopback" and not allow_remote_db:
                raise RuntimeError(
                    f"bad_request: storage_uri {storage_uri.strip()!r} is not loopback; "
                    "set allow_remote_db=True to confirm remote storage egress"
                )

        spec_fields = {
            "provider": embedding_provider,
            "model": embedding_model,
            "dimension": embedding_dimension,
            "api_base": embedding_api_base,
            "api_key_env": embedding_api_key_env,
            "allow_remote": True if embedding_allow_remote else None,
        }
        given_spec = {key: value for key, value in spec_fields.items() if value is not None}
        try:
            embedding_spec = _parse_embedding_spec(given_spec) if given_spec else None
        except _BadRequest as exc:
            raise RuntimeError(f"bad_request: {exc.detail}") from exc

        try:
            pack_list = _parse_packs(packs)
            target, exists = await store_io(_init_target, name.strip())
        except ValueError as exc:
            raise RuntimeError(f"bad_vault_name: {exc}") from exc
        if exists:
            raise RuntimeError(f"vault_exists: vault already exists at {target}")

        # REST and MCP creation mutate the same registry/ownership boundary. The
        # worker acquires the synchronous application mutation lock itself, so a
        # cancelled tool task cannot release serialization while init continues.
        async with state.config_lock:
            try:
                await store_io(
                    _initialize_managed_vault,
                    state,
                    target,
                    name=name.strip(),
                    packs=pack_list,
                    embedding_provider="fastembed",
                    backend=resolved_backend,
                    storage_uri=storage_uri.strip() if storage_uri else None,
                    storage_credential_env=(
                        storage_credential_env.strip() if storage_credential_env else None
                    ),
                    storage_database=storage_database.strip() if storage_database else None,
                    allow_remote_db=allow_remote_db,
                    embedding_spec=embedding_spec,
                )
            except FileExistsError:
                raise RuntimeError(f"vault_exists: vault already exists at {target}")
            await store_io(state.runtime_for, target, rehydrate=True)
        created: dict[str, object] = {"name": name.strip(), "path": str(target)}
        if embedding_spec:
            created["embedding"] = dict(embedding_spec)
        hint = await store_io(_vault_llm_model_hint, target)
        if hint:
            created["hint"] = hint
        return created

    return mcp


async def _run_async(
    vault_path: Path | None,
    *,
    host: str = DEFAULT_HOST,
    allow_remote: bool = False,
    rest_port: int = DEFAULT_REST_PORT,
    mcp_port: int = DEFAULT_MCP_PORT,
    ready_event: Optional[asyncio.Event] = None,
) -> None:
    if allow_remote or host not in _LOOPBACK_BIND_HOSTS:
        raise RuntimeError(
            "direct remote serving is disabled; bind to 127.0.0.1 and use an SSH tunnel"
        )
    # Before the first lease (the startup vault below): every lease this daemon takes
    # names its REST URL, so a refused CLI command points at this daemon's port.
    from okto_neuron.server._vault_pool import set_daemon_endpoint

    url_host = f"[{host}]" if ":" in host else host
    set_daemon_endpoint(f"http://{url_host}:{rest_port}")
    vault, active_vault_path, vault_warning = _open_startup_vault(vault_path)
    # Keep the compatibility state field explicit for isolated middleware policy
    # tests. Production startup rejects every remote bind/allow_remote request
    # above, and all sensitive writes remain unconditionally loopback-only.
    state = init_state(
        vault,
        active_vault_path,
        allow_remote=allow_remote,
    )
    state.vault_open_error = vault_warning
    # ``init_state`` already adopted the startup fallback vault into the pool (the
    # pool owns ALL handles, so shutdown closes it once via ``pool.close_all()``).

    # Issue #13: two bounded executors own every blocking call the server makes
    # (store/vault-file/config I/O, and long jobs/model work), so no request ever
    # blocks the loop that serves /health, REST and MCP. Shut down in the
    # ``finally`` below after the vaults close.
    configure_executors(*_configured_executor_workers())
    _configure_projection()

    # Pin the configured LLM providers' lazy imports (litellm, boto3) into this
    # process NOW, while the launch-time environment is intact — see the helper's
    # docstring for the venv-prune failure mode this prevents.
    _warm_llm_provider_dependencies(state)

    orchestrator = GracefulShutdown()
    state.shutdown = orchestrator
    state.rest_port = rest_port
    rest_app = _TrackedASGIApp(build_rest_app(state), orchestrator)
    # The MCP surface is a separate privileged ASGI app on its own port. REST/UI
    # is credential-free on loopback; MCP alone retains bearer authentication.
    from okto_neuron.server.http import AuthTokenMiddleware

    mcp_app = _TrackedASGIApp(
        AuthTokenMiddleware(_build_mcp_app(state)),
        orchestrator,
    )

    # Persist the MCP capability 0600 under the application runtime directory.
    # It is scoped to the daemon/port, never the selected vault, so changing vaults
    # cannot invalidate MCP clients. Reuse the existing value across restarts;
    # deleting the file explicitly rotates it.
    existing = _read_auth_token(active_vault_path, rest_port)
    if existing:
        state.auth_token = existing
    else:
        _write_auth_token(state.auth_token, active_vault_path, rest_port)

    # Resume crash-interrupted work for EVERY registered vault. Sidecars reset
    # processing/running entries to queued; each immutable runtime restarts its
    # own drain independently of browser selection.
    _resume_durable_runtime_work(state)

    # ADR 0009 P4: the continuous curation loop. A single debounced background task
    # auto-submits PROPOSE/DETECT sweeps to the curation queue as the user ingests,
    # so review surfaces populate and drift is detected without manual triggering.
    # PROPOSE/DETECT ONLY — never a destructive op (the allowlist is a code constant
    # in _scheduler.SWEEP_KINDS). Cancelled in the finally below, before vault close.
    from okto_neuron.server import _scheduler

    state.scheduler_task = asyncio.ensure_future(_scheduler.run_scheduler(state))
    # ADR 0025: global folder-monitoring loop. Polls ALL vaults with
    # ``folder_watch.roots`` configured; flag-gated by ``folder_watch.enabled``
    # (default False) so inert unless configured. Global — not reset by switch_vault.
    #
    # Task 4 (silent-failure visibility): the loop body is wrapped in a crash guard,
    # but if the task EVER exits with an exception (something outside the guard, e.g.
    # in the sleep/scaffolding), auto-ingest would stop forever with no signal. The
    # ``_supervise_folder_watch`` done-callback restarts a CRASHED task (bounded by
    # MAX_FOLDER_WATCH_RESTARTS) and logs at ERROR; a clean shutdown-cancel is left
    # dead so /health can report it as degraded instead of silently masking the stop.
    state.folder_watch_task = _start_supervised_folder_watch(state)

    rest_config = uvicorn.Config(
        rest_app,
        host=host,
        port=rest_port,
        log_level="info",
        access_log=False,
        lifespan="on",
    )
    mcp_config = uvicorn.Config(
        mcp_app,
        host=host,
        port=mcp_port,
        log_level="info",
        access_log=False,
        lifespan="on",
    )
    rest_server = uvicorn.Server(rest_config)
    mcp_server = uvicorn.Server(mcp_config)
    # Uvicorn 0.48 captures signals inside ``serve()`` (and replays them on
    # context exit), while older versions used ``install_signal_handlers``.
    # Disable both hooks: two server instances must share our one handler or a
    # single Ctrl-C is captured/replayed as several signals.
    rest_server.install_signal_handlers = lambda: None  # type: ignore[assignment]
    mcp_server.install_signal_handlers = lambda: None  # type: ignore[assignment]
    rest_server.capture_signals = lambda: contextlib.nullcontext()  # type: ignore[method-assign]
    mcp_server.capture_signals = lambda: contextlib.nullcontext()  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()
    shutdown_wakeup = asyncio.Event()
    shutdown_handler = _ShutdownSignalHandler(
        state,
        orchestrator,
        rest_server,
        mcp_server,
        shutdown_wakeup,
        _hard_exit,
    )

    synchronous_signal_handlers: dict[int, Any] = {}

    def _synchronous_signal_handler(signum: int, frame: Any) -> None:
        del frame
        loop.call_soon_threadsafe(shutdown_handler, signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, shutdown_handler, sig)
        except NotImplementedError:  # pragma: no cover - windows
            try:
                synchronous_signal_handlers[sig] = signal.signal(sig, _synchronous_signal_handler)
            except (OSError, ValueError):
                pass

    _LOG.info(
        "okto-neuron serve listening rest=%s:%d mcp=%s:%d vault=%s",
        host,
        rest_port,
        host,
        mcp_port,
        active_vault_path if active_vault_path is not None else "(none)",
    )
    # Never log the MCP capability. The browser opens the plain loopback URL and
    # does not receive the credential in a URL, cookie, or web storage.
    _LOG.info(
        "local UI available at http://%s:%d/; MCP credential file: %s",
        host,
        rest_port,
        auth_token_path(active_vault_path, rest_port),
    )
    if ready_event is not None:
        ready_event.set()
    # #38: the startup vault is open (_open_startup_vault), runtimes are discovered
    # (_resume_durable_runtime_work) and no request has been served yet. Freeze the long-lived heap ONCE here, then raise the
    # collector thresholds; the pause watch is installed regardless. Before the prewarm
    # so no background thread is allocating while the freeze runs.
    _gc_tuning.install_gc_watch()
    _gc_tuning.apply_gc_tuning()
    # Same point: shorter GIL switch interval so the event loop is not starved by a
    # CPU-bound audit thread (what remains once the collector pauses are gone).
    _gc_tuning.apply_switch_interval()
    # After readiness: warm the ledger indexes of the vaults that opened (#14).
    start_ledger_prewarm(state)

    # Both servers share Uvicorn's logger. Filter only the MCP task's duplicate
    # lifecycle INFO records; every warning/error still passes through.
    mcp_lifecycle_filter = _McpLifecycleNoiseFilter()
    uvicorn_error_logger = logging.getLogger("uvicorn.error")
    uvicorn_error_logger.addFilter(mcp_lifecycle_filter)
    transport_tasks = (
        asyncio.create_task(rest_server.serve(), name="okto-neuron-rest"),
        asyncio.create_task(mcp_server.serve(), name="okto-neuron-mcp"),
    )
    shutdown_waiter = asyncio.create_task(
        shutdown_wakeup.wait(), name="okto-neuron-shutdown-wakeup"
    )
    cancellation: asyncio.CancelledError | None = None
    try:
        # A signal wakes this wait immediately. A transport ending first is also
        # terminal: stop its sibling and run the same bounded teardown path.
        await asyncio.wait(
            {*transport_tasks, shutdown_waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
    except asyncio.CancelledError as exc:  # pragma: no cover - defensive
        cancellation = exc
    finally:
        if not shutdown_handler.triggered:
            shutdown_handler()
        shutdown_waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await shutdown_waiter
        try:
            await _graceful_shutdown(
                state=state,
                orchestrator=orchestrator,
                rest_server=rest_server,
                mcp_server=mcp_server,
                transport_tasks=transport_tasks,
            )
        finally:
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous = synchronous_signal_handlers.get(sig)
                if previous is not None:
                    with contextlib.suppress(OSError, ValueError):
                        signal.signal(sig, previous)
                else:
                    with contextlib.suppress(NotImplementedError):
                        loop.remove_signal_handler(sig)
            uvicorn_error_logger.removeFilter(mcp_lifecycle_filter)
            reset_state_for_tests()
            shutdown_executors()

    if cancellation is not None:  # pragma: no cover - defensive
        raise cancellation
    for task in transport_tasks:
        if task.cancelled():
            continue
        error = task.exception()
        if error is not None:
            raise error


def run(
    vault_path: str | Path | None,
    *,
    host: str = DEFAULT_HOST,
    allow_remote: bool = False,
    rest_port: int = DEFAULT_REST_PORT,
    mcp_port: int = DEFAULT_MCP_PORT,
) -> None:
    """Synchronous entry point used by the ``okto-neuron serve`` CLI command."""
    # Idempotent backstop for embedders: the CLI already ran it before any thread.
    preload_server_modules()
    try:
        asyncio.run(
            _run_async(
                Path(vault_path) if vault_path is not None else None,
                host=host,
                allow_remote=allow_remote,
                rest_port=rest_port,
                mcp_port=mcp_port,
            )
        )
    except KeyboardInterrupt:  # pragma: no cover
        _LOG.info("interrupted")


__all__ = [
    "run",
    "DEFAULT_REST_PORT",
    "DEFAULT_MCP_PORT",
    "DEFAULT_HOST",
    "VaultResolutionError",
    "resolve_vault_selector",
    "resolve_mcp_vault",
]
