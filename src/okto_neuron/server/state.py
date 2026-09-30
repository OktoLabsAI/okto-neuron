"""Application state and immutable per-vault runtime ownership (ADR 0034).

``ServerState`` owns only process-wide lifecycle, supervisors, authentication,
and the handle pool.  Every resolved vault path has one lazily-created
``VaultRuntime`` whose path never changes.  Queue items, jobs, locks, sidecars,
and maintenance flags live on that runtime, so browser selection cannot retarget
in-flight work.

The legacy fallback fields on ``ServerState`` remain for unscoped callers and
old tests. New HTTP/MCP code resolves a runtime and holds
``runtime.lease_vault()`` for the full operation.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Final, Iterator, Optional

import logging

from okto_neuron.server.lifecycle import shutdown_phase
from okto_neuron.server._vault_pool import (
    VaultLease,
    VaultPool,
    VaultPoolError,
    acquire_daemon_writer_lease,
)
from okto_neuron.store.writer_lease import release_all as release_all_writer_leases
from okto_neuron.store.handle_lease import VaultHandleLease
from okto_neuron.vault import Vault

_LOG = logging.getLogger(__name__)

if TYPE_CHECKING:
    from okto_neuron.server.lifecycle import GracefulShutdown
    from okto_neuron.store.integrity import IntegrityAuditResult

__all__ = [
    "ServerState",
    "VaultRuntime",
    "bind_vault_runtime",
    "get_server_state",
    "get_state",
    "init_state",
    "reset_state_for_tests",
    "get_vault_write_lock",
    "reset_vault_write_lock",
]


_vault_write_lock: asyncio.Lock | None = None
"""Lazily-initialized singleton; do not access from outside this module."""


def get_vault_write_lock() -> asyncio.Lock:
    """Return the no-vault compatibility lock, creating it on first call.

    A ``ServerState`` with an active vault delegates to that
    :class:`VaultRuntime`'s ``writer_lock``. This singleton only covers legacy
    callers while no vault is selected. Read handlers do not acquire either
    lock.

    Returns
    -------
    asyncio.Lock
        The same compatibility instance for the lifetime of the process (until
        :func:`reset_vault_write_lock` is called in a test).
    """
    global _vault_write_lock
    if _vault_write_lock is None:
        _vault_write_lock = asyncio.Lock()
    return _vault_write_lock


def reset_vault_write_lock() -> None:
    """Reset the singleton. Test-only; never call from production code.

    Used by unit tests that need an isolated lock per test. Production
    callers MUST treat the lock as a process-lifetime singleton.
    """
    global _vault_write_lock
    _vault_write_lock = None


# Sentinel re-export so static-analysis tools see the module-level binding
# even though the actual object is built lazily.
VAULT_WRITE_LOCK_DOC: Final[str] = (
    "No-vault compatibility lock; an active ServerState delegates to its "
    "VaultRuntime.writer_lock so each vault has exactly one writer authority."
)


@dataclass
class VaultRuntime:
    """All mutable runtime state owned by one immutable resolved vault path.

    A runtime does not permanently own a raw handle. Operations borrow the
    pool-owned handle through :meth:`lease_vault`, allowing idle LRU eviction and
    deletion fencing without invalidating in-flight work.
    """

    vault_path: Path
    server: "ServerState" = field(repr=False)
    ingest_queue: list = field(default_factory=list)
    ingest_worker_active: bool = False
    ingest_worker_task: "asyncio.Task | None" = field(default=None, repr=False)
    ingest_cancel_requested: bool = False
    ingest_seq: int = 0
    curation_jobs: list = field(default_factory=list)
    curation_worker_active: bool = False
    curation_worker_task: "asyncio.Task | None" = field(default=None, repr=False)
    maintenance_tasks: "set[asyncio.Task]" = field(default_factory=set, repr=False)
    last_ingest_at: float | None = None
    last_sweep_at: float | None = None
    last_sweep_outcome: dict | None = None
    last_degraded_reasons: tuple | None = None
    vault_open_error: dict | None = None
    vault_reembed_active: bool = False
    vault_reembed_path: str | None = None
    integrity_last_audit: "IntegrityAuditResult | None" = field(default=None, repr=False)
    loop: "asyncio.AbstractEventLoop | None" = None
    _maintenance_draining: bool = field(default=False, repr=False)
    _writer_lock: "asyncio.Lock | None" = field(default=None, init=False, repr=False)
    _config_lock: "asyncio.Lock | None" = field(default=None, init=False, repr=False)
    _rehydrated: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self.vault_path = Path(self.vault_path).expanduser().resolve(strict=False)

    def __setattr__(self, name: str, value: object) -> None:
        """Mirror active-runtime assignments to the legacy server view."""
        object.__setattr__(self, name, value)
        try:
            server = object.__getattribute__(self, "server")
            path = object.__getattribute__(self, "vault_path")
        except AttributeError:
            return
        if name not in type(server)._RUNTIME_COMPAT_FIELDS:
            return
        active_path = server.vault_path
        if active_path is None or Path(active_path).resolve(strict=False) != path:
            return
        if server._vault_runtimes.get(path) is self:
            object.__setattr__(server, name, value)

    def __getattr__(self, name: str) -> object:
        """Proxy missing process-wide reads to the owning application state.

        Request handlers historically received ``ServerState``. A ContextVar now
        binds them to this runtime; lifecycle/auth/supervisor reads remain source
        compatible while every declared mutable vault field stays runtime-owned.
        """
        server = object.__getattribute__(self, "server")
        return getattr(server, name)

    @property
    def vault_pool(self) -> VaultPool:
        return self.server.vault_pool

    @property
    def vault(self) -> Vault:
        """Compatibility access to the currently owned handle.

        New operation boundaries hold :meth:`lease_vault` first, in which case
        this is a non-opening lookup. The fallback raw open keeps older runner
        tests/callers working and pins the path until process shutdown.
        """
        current = self.vault_pool.peek(self.vault_path)
        if current is not None:
            return current
        return self.vault_pool.get_or_open(self.vault_path)

    @vault.setter
    def vault(self, value: Vault) -> None:
        self.vault_pool.adopt(value, self.vault_path, pin=False)

    def lease_vault(self) -> VaultLease[Vault]:
        """Borrow this runtime's handle for one request or background job."""
        return self.vault_pool.lease(self.vault_path)

    def install_fenced_vault(
        self,
        vault: Vault,
        *,
        ownership: VaultHandleLease,
    ) -> Vault:
        """Publish a post-maintenance handle before this runtime is unfenced.

        The pool performs the ownership checks.  If this runtime is also the
        deprecated unscoped fallback, keep that raw compatibility reference from
        pointing at the handle maintenance just closed.
        """
        owned = self.vault_pool.install_fenced(
            vault,
            self.vault_path,
            ownership=ownership,
        )
        if self.server.active_runtime is self:
            object.__setattr__(self.server, "vault", owned)
        return owned

    @property
    def writer_lock(self) -> asyncio.Lock:
        if self._writer_lock is None:
            self._writer_lock = asyncio.Lock()
        return self._writer_lock

    @property
    def config_lock(self) -> asyncio.Lock:
        if self._config_lock is None:
            self._config_lock = asyncio.Lock()
        return self._config_lock

    @property
    def shutting_down(self) -> bool:
        return self.server.shutting_down

    @property
    def maintenance_draining(self) -> bool:
        """Whether this vault is temporarily fenced from new writes."""
        return self._maintenance_draining

    @property
    def draining(self) -> bool:
        return self._maintenance_draining or self.server.shutting_down

    @draining.setter
    def draining(self, value: bool) -> None:
        self._maintenance_draining = bool(value)

    def mark_draining(self) -> None:
        self._maintenance_draining = True

    def mark_shutting_down(self) -> None:
        self.server.mark_shutting_down()

    @property
    def shutdown(self) -> "GracefulShutdown | None":
        return self.server.shutdown

    @property
    def has_vault(self) -> bool:
        return True

    def note_ingest(self, at: float | None = None) -> float:
        """Record ingest activity on this runtime and the legacy diagnostics map."""
        observed = time.time() if at is None else float(at)
        self.last_ingest_at = observed
        self.server.last_ingest_at_by_vault[str(self.vault_path)] = observed
        if self.server.vault_path == self.vault_path:
            self.server.last_ingest_at = observed
        return observed

    def note_sweep(self, at: float, outcome: dict | None = None) -> None:
        self.last_sweep_at = float(at)
        self.last_sweep_outcome = outcome
        self.server.last_sweep_at_by_vault[str(self.vault_path)] = float(at)
        if self.server.vault_path == self.vault_path:
            self.server.last_sweep_at = float(at)
            self.server.last_sweep_outcome = outcome

    def busy_work(self) -> dict[str, int | bool]:
        """Deletion/status summary for work owned by only this vault."""
        return {
            "ingest_queued": sum(
                1
                for item in self.ingest_queue
                if getattr(item, "status", None) in {"queued", "processing"}
            ),
            "curation_queued": sum(
                1
                for job in self.curation_jobs
                if getattr(job, "status", None) in {"queued", "running"}
            ),
            "ingest_worker": self.ingest_worker_active,
            "curation_worker": self.curation_worker_active,
            "maintenance": bool(self.maintenance_tasks or self._maintenance_draining),
        }

    @property
    def idle(self) -> bool:
        busy = self.busy_work()
        return not any(bool(value) for value in busy.values())

    def reset_after_wipe(self) -> None:
        """Clear derived in-memory state after this vault was wiped and reopened."""
        self.ingest_queue = []
        self.ingest_worker_active = False
        self.ingest_worker_task = None
        self.ingest_cancel_requested = False
        self.ingest_seq = 0
        self.curation_jobs = []
        self.curation_worker_active = False
        self.curation_worker_task = None
        self.last_ingest_at = None
        self.last_sweep_at = None
        self.last_sweep_outcome = None
        self.last_degraded_reasons = None
        self.vault_open_error = None
        self.vault_reembed_active = False
        self.vault_reembed_path = None
        self._rehydrated = True
        key = str(self.vault_path)
        self.server.last_ingest_at_by_vault.pop(key, None)
        self.server.last_sweep_at_by_vault.pop(key, None)


@dataclass
class ServerState:
    """Process-wide singleton held for the lifetime of ``okto-neuron serve``.

    ``vault``/``vault_path`` and their adjacent queue fields are compatibility
    mirrors for unscoped callers. Client-scoped application code resolves a
    :class:`VaultRuntime` instead and never mutates this fallback selection.
    """

    vault: Vault | None
    vault_path: Path | None
    vault_pool: VaultPool = field(default_factory=VaultPool, repr=False)
    """Owns every open Ladybug handle for the process — the compatibility
    fallback and every request-selected runtime. Single owner because
    ``Vault.close()`` is path-wide; closed exactly once via ``close_all()`` at
    shutdown. ``switch_vault`` adopts the new handle here instead of closing the
    old one (an MCP connection may still be using the old)."""
    _maintenance_draining: bool = field(default=False, repr=False)
    """Temporary write/read gate used by rebuild, reembed, reset, and heal."""
    shutting_down: bool = False
    """Permanent process-lifecycle gate set by the first shutdown signal.

    This is deliberately separate from maintenance draining: maintenance jobs
    clear ``state.draining`` when they finish, which must never reopen the server
    after SIGTERM has started teardown.
    """
    allow_remote: bool = False
    """Compatibility field retained for request-policy code. Direct non-loopback
    serving is rejected before startup, so production state remains False and
    the localhost-only trust boundary cannot be widened by a CLI flag."""
    auth_token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    """Application-scoped FastMCP capability token.

    ADR 0034's loopback REST/UI surface intentionally has no browser credential.
    The separate MCP port remains bearer-authenticated, and the token is stored
    once under the application runtime directory rather than under a vault.
    """
    started_at: float = field(default_factory=time.monotonic)
    pid: int = field(default_factory=os.getpid)
    multi_vault_runtime_enabled: bool = False
    """True in the application daemon after :func:`init_state`.

    Direct ``ServerState(...)`` construction keeps the legacy single-active
    behavior unless a test/caller opts in. This is a temporary compatibility
    seam while HTTP handlers migrate to explicit runtimes.
    """
    ingest_queue: list = field(default_factory=list)
    """Bulk-ingest job queue (list of ``_ingest_queue.IngestItem``). Mutated only
    on the event loop — the drain worker and the status handler share it."""
    ingest_worker_active: bool = False
    """True while the single drain worker coroutine is running."""
    ingest_worker_task: "asyncio.Task | None" = field(default=None, repr=False)
    """Owned drain-worker task, awaited during server shutdown before vault close."""
    ingest_cancel_requested: bool = False
    """True while a requested bulk-ingest stop is winding down. Queued files are
    cancelled immediately; the processing file stops at its next safe pre-commit
    checkpoint, while an atomic commit already in progress is allowed to finish."""
    ingest_seq: int = 0
    """Monotonic id counter for ``IngestItem.id`` (``<seq>-<pathhash8>``).
    State-scoped (not module-level) because ``switch_vault`` re-rehydrates the
    queue per vault; seeded by ``rehydrate_queue`` to max(persisted id)+1 so a
    restart never reuses a live id. The previous len()-based scheme minted
    duplicate ids once items were deleted from the queue history."""
    curation_jobs: list = field(default_factory=list)
    """Generic curation job queue (list of ``_jobs.CurationJob``) — ADR 0009 P2.
    Every writing curation op (reconcile apply, review confirm/reject) runs through
    this runtime's owned handle instead of a competing process. Mutated only on
    the event loop; this runtime's drain worker and status handler share it."""
    curation_worker_active: bool = False
    """True while this runtime's curation drain worker coroutine is running."""
    curation_worker_task: "asyncio.Task | None" = field(default=None, repr=False)
    """Owned curation worker, retained so shutdown can await it before vault close."""
    maintenance_tasks: "set[asyncio.Task]" = field(default_factory=set, repr=False)
    """Owned background maintenance tasks started by HTTP handlers.

    These wrap blocking workers with ``asyncio.to_thread`` and remain strongly
    referenced until completion. Shutdown waits for them under the same absolute
    deadline before closing the vault.
    """
    last_ingest_at: float | None = None
    """``time.time()`` of the most recent IN-PROCESS ingest completion (ADR 0009
    P4). Bumped at every current in-process ingest chokepoint (the ambient bulk
    drain, deterministic REST /add, and REST/MCP remember writes). The
    continuous-curation scheduler debounces sweeps off this signal. ``None`` until
    the first in-process ingest. NOTE: ``kg watch`` runs in a SEPARATE process and
    is NOT observable here, so the in-process scheduler cannot debounce off it."""
    last_sweep_at: float | None = None
    """``time.time()`` of the last auto sweep the scheduler submitted (ADR 0009 P4).
    ``None`` until the loop has fired once; the min-interval floor treats None as 0."""
    last_sweep_outcome: dict | None = None
    """The last auto sweep's outcome for surfacing: ``{at, submitted: [job_ids],
    reason}``. ``None`` until the loop has fired once."""
    last_ingest_at_by_vault: dict = field(default_factory=dict)
    """Per-vault ``time.time()`` of the most recent in-process ingest, keyed by
    ``str(resolved vault path)``. Unlike the compatibility scalar above, this
    records ingest into ANY pooled ``?vault=`` vault so the continuous-curation
    scheduler can debounce sweeps per vault. Mutated only on the event loop.
    Deliberately NOT cleared by ``switch_vault`` — request-scoped activity must
    survive a compatibility-fallback change."""
    last_sweep_at_by_vault: dict = field(default_factory=dict)
    """Per-vault ``time.time()`` of the last scheduler-submitted sweep, keyed by
    ``str(resolved vault path)``. Companion to ``last_ingest_at_by_vault``; the
    compatibility scalars above remain as the legacy status mirror."""
    last_degraded_reasons: tuple | None = None
    """The degraded-reason KEY tuple (stable prefix before ':' of each reason
    string, e.g. ``folder_watch_stalled`` not the full rendered message) most
    recently LOGGED by the operational status handler. ``None`` until the first
    degraded poll. Lets status polling log the transition once per reason-KEY-set
    change instead of an ERROR line on every poll (a sticky flag like
    recovered_from_corruption otherwise floods the log with identical lines
    for hours) — keyed rather than exact-string so a reason whose rendered
    text embeds a live-changing value (e.g. a stall age in seconds) doesn't
    look like a new transition on every single poll."""
    vault_open_error: dict | None = None
    """Recoverable startup-fallback open failure surfaced to the web UI. Used when
    serve starts without that fallback because the configured/default vault
    needs operator repair, for example after an embedding model width change."""
    vault_reembed_active: bool = False
    """True while manager-initiated repair reembed runs for a selected vault."""
    vault_reembed_path: str | None = None
    """Absolute path currently being repaired by the vault manager reembed action."""
    scheduler_task: "asyncio.Task | None" = field(default=None, repr=False)
    """The continuous-curation scheduler background task (ADR 0009 P4). Created in
    runtime's ``_run_async`` and cancelled in its ``finally`` before vault close."""
    folder_watch_task: "asyncio.Task | None" = field(default=None, repr=False)
    """The global folder-monitoring background task (ADR 0025). Created once at
    daemon boot alongside ``scheduler_task``; it iterates every application runtime
    independently of browser selection. Cancelled in runtime's ``finally`` before
    vault close."""
    folder_watch_restart_count: int = 0
    """How many times the global folder-watch task has been auto-restarted after an
    unexpected crash (Task 4). Bumped by the done-callback in runtime when the loop
    exits with an exception (never on a clean shutdown cancel). Surfaced on
    credential-free /api/v1/status
    so a silently-dying watcher is visible instead of stalling ingest forever."""
    loop: "asyncio.AbstractEventLoop | None" = None
    """The server's running event loop, captured when the curation worker starts
    (ADR 0009 P3). A curation runner executes off-loop inside ``asyncio.to_thread``
    and so cannot call ``get_running_loop()`` itself; the rebuild/heal runner needs
    a loop reference to marshal its atomic graph-swap back onto the loop as a single
    no-await block (so no synchronous read handler can observe a half-swapped graph).
    ``None`` until the worker has run at least once."""
    shutdown: "GracefulShutdown | None" = field(default=None, repr=False)
    """Cross-transport request/deadline coordinator installed by runtime."""
    _vault_runtimes: dict[Path, VaultRuntime] = field(default_factory=dict, init=False, repr=False)
    _runtimes_lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False
    )
    """Guards ``_vault_runtimes``. Runtime resolution runs on store-executor
    threads (issue #13), so two requests for a new path must still agree on ONE
    runtime (and therefore one writer lock)."""
    _busy_logged: set[Path] = field(default_factory=set, init=False, repr=False)
    # Vaults discovery refused because their review queue is still v1 (#14):
    # no runtime, no lease, no write; shown in /api/v1/status until migrated.
    _queue_refused: dict[Path, dict[str, str]] = field(
        default_factory=dict, init=False, repr=False
    )
    """Vaults already reported busy (another process holds the writer lease)."""
    _closed: bool = field(default=False, init=False, repr=False)

    def __setattr__(self, name: str, value: object) -> None:
        """Mirror explicit legacy active-field assignments into that runtime.

        Lists are normally shared by identity, but tests and compatibility
        callers also replace fields or assign scalar timestamps directly. Keep
        those explicit writes coherent without making the application daemon
        read from process-global fallback state.
        """
        object.__setattr__(self, name, value)
        if name not in type(self)._RUNTIME_COMPAT_FIELDS:
            return
        runtimes = self.__dict__.get("_vault_runtimes")
        path = self.__dict__.get("vault_path")
        if not isinstance(runtimes, dict) or path is None:
            return
        key = Path(path).expanduser().resolve(strict=False)
        runtime = runtimes.get(key)
        if runtime is not None and getattr(runtime, name) is not value:
            setattr(runtime, name, value)

    def __post_init__(self) -> None:
        if (self.vault is None) != (self.vault_path is None):
            raise RuntimeError("vault and vault_path must both be set, or both be None")
        if self.vault_path is None:
            return
        self.vault_path = Path(self.vault_path).expanduser().resolve(strict=False)
        assert self.vault is not None
        self.vault = self.vault_pool.adopt(self.vault, self.vault_path, pin=False)
        runtime = VaultRuntime(vault_path=self.vault_path, server=self)
        self._copy_compat_to_runtime(runtime)
        self._vault_runtimes[self.vault_path] = runtime

    _RUNTIME_COMPAT_FIELDS: ClassVar[tuple[str, ...]] = (
        "ingest_queue",
        "ingest_worker_active",
        "ingest_worker_task",
        "ingest_cancel_requested",
        "ingest_seq",
        "curation_jobs",
        "curation_worker_active",
        "curation_worker_task",
        "maintenance_tasks",
        "last_ingest_at",
        "last_sweep_at",
        "last_sweep_outcome",
        "last_degraded_reasons",
        "vault_open_error",
        "vault_reembed_active",
        "vault_reembed_path",
        "loop",
    )

    def _copy_compat_to_runtime(self, runtime: VaultRuntime) -> None:
        """Snapshot legacy active fields into their owning runtime.

        Used only at initialization and by the deprecated process-wide switch
        compatibility path. New client-scoped work mutates the runtime directly.
        """
        for name in self._RUNTIME_COMPAT_FIELDS:
            setattr(runtime, name, getattr(self, name))
        runtime._maintenance_draining = self._maintenance_draining

    def _copy_runtime_to_compat(self, runtime: VaultRuntime) -> None:
        """Point legacy active fields at one runtime without changing other contexts."""
        self.vault_path = runtime.vault_path
        self.vault = runtime.vault
        for name in self._RUNTIME_COMPAT_FIELDS:
            setattr(self, name, getattr(runtime, name))
        self._maintenance_draining = runtime._maintenance_draining

    def runtime_for(
        self,
        vault_path: Path | str,
        *,
        vault: Vault | None = None,
        rehydrate: bool = True,
    ) -> VaultRuntime:
        """Return the one immutable runtime for ``vault_path``.

        Creating a runtime reads only its durable queue/job sidecars; it does not
        open the graph. Supplying an already-open ``vault`` transfers that handle
        to the pool without a compatibility pin.
        """
        key = Path(vault_path).expanduser().resolve(strict=False)
        with self._runtimes_lock:
            runtime = self._vault_runtimes.get(key)
            if runtime is None and self.vault_pool.is_fenced(key):
                raise VaultPoolError(
                    "vault_fenced",
                    f"vault runtime is unavailable after deletion or maintenance: {key}",
                )
            if runtime is None and vault is None and key.is_dir():
                # BEFORE the writer lease: a v1 vault is refused untouched (the
                # lease creates .okto-neuron-writer.lock inside it). Only reads yaml.
                self._refuse_v1_layout(key)
            if runtime is None and key.is_dir():
                # Raises VaultPoolError("vault_busy") when a CLI holds the vault.
                acquire_daemon_writer_lease(key)
            if vault is not None:
                self.vault_pool.adopt(vault, key, pin=False)
            if runtime is None:
                runtime = VaultRuntime(vault_path=key, server=self)
                self._vault_runtimes[key] = runtime
            if rehydrate and not runtime._rehydrated:
                from okto_neuron.server import _ingest_queue, _jobs

                # Mark first so a malformed/best-effort sidecar cannot cause every
                # status poll to repeat disk work forever.
                runtime._rehydrated = True
                _ingest_queue.rehydrate_queue(runtime)
                _jobs.rehydrate_jobs(runtime)
            return runtime

    def _refuse_v1_layout(self, key: Path) -> None:
        from okto_neuron.consolidate.review_queue import (
            clear_layout_refusal_log,
            layout_refusal,
            log_layout_refusal_once,
        )

        refusal = layout_refusal(key)
        if refusal is None:
            self._queue_refused.pop(key, None)
            clear_layout_refusal_log(key)
            return
        self._queue_refused[key] = refusal
        log_layout_refusal_once(key, refusal)
        raise VaultPoolError(
            refusal["code"], f"{refusal['detail']}; remedy: {refusal['remedy']}"
        )

    def queue_refusals(self) -> dict[Path, dict[str, str]]:
        """Vaults refused for a v1 review queue that have no runtime (snapshot)."""

        return dict(self._queue_refused)

    @property
    def active_runtime(self) -> VaultRuntime | None:
        if self.vault_path is None:
            return None
        return self._vault_runtimes.get(Path(self.vault_path).expanduser().resolve(strict=False))

    def runtimes(self, *, discover: bool = False) -> tuple[VaultRuntime, ...]:
        """Return a stable path-sorted snapshot of owned runtimes.

        ``discover=True`` creates lightweight contexts for every registered
        vault without opening graph handles. The scheduler/folder watcher use it
        so no browser selection is required for background work to resume.
        """
        if discover:
            from okto_neuron.vault_registry import list_vaults

            entries = list_vaults(
                current=self.vault_path if self.vault_path is not None else None
            )
            with self._runtimes_lock:
                for entry in entries:
                    key = entry.path.resolve(strict=False)
                    if key not in self._vault_runtimes and self.vault_pool.is_fenced(key):
                        continue
                    try:
                        self.runtime_for(entry.path)
                    except VaultPoolError as exc:
                        if exc.code == "review_queue_migration_required":
                            continue  # refused alone; logged once, shown in status
                        if exc.code != "vault_busy":
                            raise
                        # One busy vault must not break the others; the next
                        # discovery pass retries it. Log once per vault.
                        if key not in self._busy_logged:
                            self._busy_logged.add(key)
                            _LOG.warning("skipping busy vault %s for now: %s", key.name, exc)
                        continue
                    self._busy_logged.discard(key)
                self._migrate_legacy_targeted_jobs()
        # No lock for the snapshot itself: copying the dict is atomic under the
        # GIL, and the event loop must never wait behind a thread that holds the
        # lock while it rehydrates sidecars from disk.
        snapshot = dict(self._vault_runtimes)
        return tuple(snapshot[path] for path in sorted(snapshot))

    def _migrate_legacy_targeted_jobs(self) -> None:
        """Move live pre-ADR-0034 targeted jobs to their owning runtime.

        Older schedulers stored a targeted job in the process fallback's sidecar
        and added ``params.vault`` as a cross-vault target. New workers refuse
        that mutable routing. Discovery performs a one-time, graph-free sidecar
        migration before startup workers resume.
        """
        from okto_neuron.server import _jobs

        changed: set[Path] = set()
        for owner_path, owner in tuple(self._vault_runtimes.items()):
            for job in list(owner.curation_jobs):
                if getattr(job, "status", None) not in {"queued", "running"}:
                    continue
                params = getattr(job, "params", None)
                raw_target = params.get("vault") if isinstance(params, dict) else None
                if not raw_target:
                    continue
                target_path = Path(raw_target).expanduser().resolve(strict=False)
                if target_path == owner_path:
                    continue
                target = self._vault_runtimes.get(target_path)
                if target is None:
                    continue
                owner.curation_jobs.remove(job)
                if not any(existing.id == job.id for existing in target.curation_jobs):
                    target.curation_jobs.append(job)
                changed.update({owner_path, target_path})
        for path in changed:
            _jobs.persist(self._vault_runtimes[path])

    def drop_runtime(self, vault_path: Path | str) -> bool:
        """Forget an idle, handle-free runtime after managed deletion.

        The deletion coordinator must fence and release the pool path first.
        """
        key = Path(vault_path).expanduser().resolve(strict=False)
        with self._runtimes_lock:
            runtime = self._vault_runtimes.get(key)
            if runtime is None:
                return False
            if not runtime.idle:
                raise RuntimeError(f"vault runtime still has active work: {key}")
            if self.vault_pool.peek(key) is not None:
                raise RuntimeError(
                    f"vault handle must be released before dropping runtime: {key}"
                )
            self._vault_runtimes.pop(key, None)
            return True

    def runtime_tasks(self) -> set[asyncio.Task]:
        """Every background task owned by all vault contexts."""
        tasks: set[asyncio.Task] = set()
        for runtime in tuple(self._vault_runtimes.values()):
            for task in (runtime.ingest_worker_task, runtime.curation_worker_task):
                if task is not None:
                    tasks.add(task)
            tasks.update(runtime.maintenance_tasks)
        return tasks

    @property
    def writer_lock(self) -> asyncio.Lock:
        """Return the active vault's lock, or the no-vault compatibility lock.

        Direct ``ServerState`` callers and request-bound ``VaultRuntime`` callers
        must serialize against the same lock. Returning an independent global
        lock here creates two writer authorities for the active Ladybug handle.
        """
        runtime = self.active_runtime
        return runtime.writer_lock if runtime is not None else get_vault_write_lock()

    def lease_vault(self) -> VaultLease[Vault]:
        """Borrow the compatibility fallback through its immutable runtime."""
        runtime = self.active_runtime
        if runtime is None:
            raise RuntimeError("no compatibility vault is configured")
        return runtime.lease_vault()

    def install_fenced_vault(
        self,
        vault: Vault,
        *,
        ownership: VaultHandleLease,
    ) -> Vault:
        """Publish a maintenance replacement for the compatibility fallback."""
        runtime = self.active_runtime
        if runtime is None:
            raise RuntimeError("no compatibility vault is configured")
        return runtime.install_fenced_vault(vault, ownership=ownership)

    _config_lock: "asyncio.Lock | None" = field(default=None, init=False, repr=False)
    _application_mutation_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False
    )

    @property
    def config_lock(self) -> asyncio.Lock:
        """Serializes okto-neuron.yaml writes against EACH OTHER only — deliberately
        NOT the graph writer_lock. An ingest item holds writer_lock for its entire
        run (minutes+ on dense files), and config changes are already tolerant of
        in-flight work by design ('in-flight work keeps its old instance; the next
        use constructs fresh'), so queueing a Save behind an ingest item only
        starved the UI (observed live: Save spinner hung for the whole item).
        Lazily created so the dataclass never touches the event loop at import."""
        if self._config_lock is None:
            self._config_lock = asyncio.Lock()
        return self._config_lock

    def run_application_mutation(self, operation: Callable[[], Any]) -> Any:
        """Run one blocking application mutation under its synchronous lock.

        Callers invoke this method inside ``asyncio.to_thread``. Unlike an
        ``asyncio.Lock`` owned by the request task, this lock remains held when
        that task is cancelled because the worker thread continues to own both
        the operation and the lock until the filesystem mutation finishes.
        """

        with self._application_mutation_lock:
            return operation()

    @property
    def has_vault(self) -> bool:
        return self.vault is not None and self.vault_path is not None

    @property
    def maintenance_draining(self) -> bool:
        """Whether the active vault is temporarily fenced from new writes."""
        return self._maintenance_draining

    @property
    def draining(self) -> bool:
        """Whether requests/work must stop for maintenance or process shutdown."""
        return self._maintenance_draining or self.shutting_down

    @draining.setter
    def draining(self, value: bool) -> None:
        # Existing maintenance code assigns ``state.draining = False`` in
        # ``finally`` blocks. Only clear the maintenance half so an overlapping
        # SIGTERM remains sticky through the rest of process teardown.
        self._maintenance_draining = bool(value)

    def mark_draining(self) -> None:
        """Enter a temporary maintenance drain (legacy public name)."""
        self._maintenance_draining = True

    def mark_shutting_down(self) -> None:
        """Enter the permanent process shutdown drain."""
        self.shutting_down = True

    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self.started_at)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Close every pooled vault handle exactly once.

        The pool owns ALL open handles (active + per-connection selected), so a
        single ``vault_pool.close_all()`` closes them. Safe to call from both the
        SIGTERM-driven shutdown path and the ``finally`` block in
        :mod:`okto_neuron.server.runtime` — second and subsequent calls are no-ops.
        """
        if self._closed:
            return
        self._closed = True
        with shutdown_phase("store_close"):
            self.vault_pool.close_all()
        # Stores are closed; only now let another process write these vaults.
        with shutdown_phase("writer_lease_release"):
            release_all_writer_leases()

    def switch_vault(self, vault: Vault, vault_path: Path) -> None:
        """Change only the deprecated unscoped compatibility fallback.

        ADR 0034 browser selection never calls this method. Existing callers keep
        their historical default-vault behavior, but queues/jobs remain owned by
        their immutable runtimes and are not cleared or retargeted.
        """
        current = self.active_runtime
        if current is not None and not self.multi_vault_runtime_enabled:
            # In the legacy shape, callers mutate scalar compatibility fields
            # directly, so snapshot them before changing the fallback. In the
            # application daemon the runtime is authoritative; copying stale
            # compatibility mirrors back could erase an in-flight worker task.
            self._copy_compat_to_runtime(current)
        target = self.runtime_for(vault_path, vault=vault, rehydrate=True)
        bound = _BOUND_RUNTIME.get()
        if bound is target and bound.vault_path == target.vault_path:
            # The only same-runtime switch from a bound request is the explicit
            # reset/wipe flow. Its sidecars are gone, so stale in-memory derived
            # queues and timestamps must not survive the reopened graph.
            target.reset_after_wipe()
        self._copy_runtime_to_compat(target)
        self._closed = False


_STATE: Optional[ServerState] = None
_BOUND_RUNTIME: contextvars.ContextVar[VaultRuntime | None] = contextvars.ContextVar(
    "okto_neuron_bound_vault_runtime", default=None
)


def get_server_state() -> ServerState:
    """Return process-global application state, ignoring request runtime binding."""
    if _STATE is None:
        raise RuntimeError("ServerState not initialized; call init_state() first")
    return _STATE


def get_state() -> ServerState | VaultRuntime:
    """Return the request-bound runtime, or process state outside a binding."""
    runtime = _BOUND_RUNTIME.get()
    return runtime if runtime is not None else get_server_state()


@contextlib.contextmanager
def bind_vault_runtime(runtime: VaultRuntime) -> Iterator[VaultRuntime]:
    """Bind one immutable runtime to this sync/async request context."""
    server = runtime.server
    legacy_active = not server.multi_vault_runtime_enabled and server.active_runtime is runtime
    if legacy_active:
        # Direct ServerState fixtures/callers historically mutate active scalar
        # and list fields between requests. Import those values at the boundary,
        # then export handler changes on exit. The application daemon never uses
        # this mirror path: its VaultRuntime is authoritative at all times.
        server._copy_compat_to_runtime(runtime)
    token = _BOUND_RUNTIME.set(runtime)
    try:
        yield runtime
    finally:
        _BOUND_RUNTIME.reset(token)
        if legacy_active:
            server._copy_runtime_to_compat(runtime)


def init_state(
    vault: Vault | None, vault_path: Path | None, *, allow_remote: bool = False
) -> ServerState:
    """Initialize the process-wide :class:`ServerState` (once per process).

    ``allow_remote`` remains an internal compatibility input for policy tests.
    Production startup rejects it before state initialization, preserving the
    localhost-only trust boundary.
    """
    global _STATE
    if _STATE is not None:
        raise RuntimeError("ServerState already initialized for this process")
    if (vault is None) != (vault_path is None):
        raise RuntimeError("vault and vault_path must both be set, or both be None")
    resolved = Path(vault_path).resolve(strict=False) if vault_path is not None else None
    _STATE = ServerState(
        vault=vault,
        vault_path=resolved,
        allow_remote=allow_remote,
        multi_vault_runtime_enabled=True,
    )
    if resolved is None:
        return _STATE
    runtime = _STATE.runtime_for(resolved, vault=vault, rehydrate=True)
    _STATE._copy_runtime_to_compat(runtime)
    return _STATE


def reset_state_for_tests() -> None:
    """Drop the module singleton; close any underlying vault. Test-only."""
    global _STATE
    from okto_neuron.server import _projection

    _projection.reset_for_tests()
    if _STATE is not None:
        # A store-executor call abandoned by a cancelled request (or a worker
        # task torn down with its test loop) may still be inside the graph;
        # never close a vault underneath it.
        from okto_neuron.server._store_io import wait_executors_idle

        wait_executors_idle(timeout=30.0)
        try:
            _STATE.close()
        except Exception:  # noqa: BLE001
            pass
    _STATE = None
    _BOUND_RUNTIME.set(None)
    reset_vault_write_lock()
