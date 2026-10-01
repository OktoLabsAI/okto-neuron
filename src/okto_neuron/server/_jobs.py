"""Generic in-process curation job queue (ADR 0009 P2 — the spine).

Generalizes the ingest-only worker in :mod:`okto_neuron.server._ingest_queue`
into a queue that runs any blocking curation operation on one immutable vault
runtime, one at a time, off the event loop.

Why this exists (the load-bearing premise, ADR 0007 + 0009):
  Ladybug single-writer is *in-process*. ``VaultConnection`` caches one
  ``VaultGraphHandle`` (one database) per vault path; every store op opens its
  own short-lived ``ladybug.Connection`` over that shared database. A *second
  process* opening the vault is the corruption path. The CLI reconcile/rebuild
  commands open their own ``Vault`` (a second handle while the daemon holds the
  first) which is exactly why they forced the UI down. This queue removes that:
  every job borrows its runtime's pool-owned handle inside the daemon process,
  serialized by that runtime's ``writer_lock`` for writes and executed via
  ``asyncio.to_thread`` so the event loop (and the UI poll) stay responsive.

Design (mirrors the proven ``_ingest_queue`` shape):
  * A ``CurationJob`` dataclass with id / kind / status / progress / result /
    error, serializable for a restart-durable sidecar.
  * A single in-process worker drains queued jobs FIFO. Each job's runner is a
    callable ``(state, job) -> dict`` that does the blocking work; the worker
    wraps it in ``asyncio.to_thread`` and, for write jobs, holds
    ``state.writer_lock`` for the duration.
  * Durable sidecar JSON under ``<vault>/.marginalia/curation-jobs.json`` so a
    poll survives a restart. Small idempotent jobs are re-queued after an
    interruption; vault-wide rebuild-family jobs fail terminally and require an
    explicit new submission.

This module owns NO domain logic. Reconcile/authority runners live next to their
HTTP handlers and are registered here by ``kind``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from okto_neuron.server import _integrity as graph_integrity
from okto_neuron.server._persist_coalesce import PersistCoalescer
from okto_neuron.server._store_io import acquire_off_loop, call_soon_on_loop, job_io, store_io

if TYPE_CHECKING:
    from okto_neuron.server.state import ServerState

_LOG = logging.getLogger("okto_neuron.server.jobs")

# Sidecar JSON (metadata + small result payloads only — never source content).
JOBS_FILENAME = "curation-jobs.json"
JOBS_VERSION = 1
# Cap retained terminal jobs so a long-lived server's history never grows
# unbounded; queued/running jobs are always kept.
RETENTION_CAP = 200

_TERMINAL = frozenset({"done", "error"})

# A runner takes the live ServerState + the job (for progress writes) and returns
# a JSON-serializable result dict. It runs OFF the event loop (asyncio.to_thread).
JobRunner = Callable[["ServerState", "CurationJob"], dict]
InterruptedJobRecovery = Callable[["ServerState", "CurationJob"], str | None]

# kind -> (runner, writes). ``writes`` gates whether the worker holds writer_lock.
# Reconcile/authority runners register here at import time (see http.py wiring).
_REGISTRY: dict[str, tuple[JobRunner, bool]] = {}
# Domain-owned recovery invoked before a crash-interrupted, non-resumable job is
# made terminal. The generic queue remains responsible only for orchestration;
# rebuild-specific files and invariants stay with the rebuild runner.
_INTERRUPTED_RECOVERY: dict[str, InterruptedJobRecovery] = {}
# Read-only runners in this set require a stable, verified graph snapshot. They
# take the writer lock and pass the integrity fence without being misclassified
# as graph writers.
_VERIFIED_SNAPSHOT_KINDS: set[str] = set()
# Only a trust-root rebuild may bypass a failed-generation writer fence. ``heal``
# copies the current graph and could otherwise normalize corrupted edge properties
# into a self-consistent new graph, laundering the incident instead of recovering it.
_INTEGRITY_RECOVERY_KINDS = frozenset({"rebuild", "rollback"})
# These jobs own a vault-wide staged operation whose in-memory handles, leases,
# and phase boundaries cannot be reconstructed from the generic queue sidecar.
# Retrying one implicitly after a process restart can repeat expensive model work
# or operate on a retained partial generation. Recovery is therefore explicit.
_NON_RESUMABLE_ON_RESTART_KINDS = frozenset({"rebuild", "rollback", "heal", "reembed"})


def register_runner(
    kind: str,
    runner: JobRunner,
    *,
    writes: bool,
    verified_snapshot: bool = False,
    interrupted_recovery: InterruptedJobRecovery | None = None,
) -> None:
    """Register a job runner for ``kind``. ``writes`` => the worker serializes it
    under ``state.writer_lock`` (off-graph side-file writes still serialize so two
    apply jobs never interleave their authority/queue JSON writes).
    ``verified_snapshot`` is for graph-read-only jobs that still need the same
    lock and integrity fence for one stable generation."""
    _REGISTRY[kind] = (runner, writes)
    if interrupted_recovery is None:
        _INTERRUPTED_RECOVERY.pop(kind, None)
    else:
        _INTERRUPTED_RECOVERY[kind] = interrupted_recovery
    if verified_snapshot:
        _VERIFIED_SNAPSHOT_KINDS.add(kind)
    else:
        _VERIFIED_SNAPSHOT_KINDS.discard(kind)


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)


def requires_verified_snapshot(kind: str) -> bool:
    return kind in _VERIFIED_SNAPSHOT_KINDS


@dataclass
class CurationJob:
    """One queued curation operation on its way through the worker."""

    id: str
    kind: str  # e.g. "reconcile-propose" | "reconcile-apply"
    status: str = "queued"  # queued | running | done | error
    label: str = ""
    params: dict = field(default_factory=dict)
    progress: str = ""  # free-text stage label for the UI
    result: dict | None = None  # runner return value (small, JSON-safe)
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    # Watchdog state (issue #24). ``last_progress_at`` is stamped at start and on
    # every ``progress()`` call; ``stall_after_s`` is the no-progress limit in
    # force when the job started (``None`` = watchdog off).
    last_progress_at: float | None = None
    stall_after_s: float | None = None

    @property
    def elapsed_s(self) -> float | None:
        if self.started_at is None:
            return None
        return round((self.finished_at or time.time()) - self.started_at, 1)

    def stalled_for_s(self, now: float | None = None) -> float | None:
        """Seconds without progress for a running job past its limit, else ``None``."""
        if self.status != "running" or self.stall_after_s is None:
            return None
        reference = self.last_progress_at or self.started_at
        if reference is None:
            return None
        idle = (now if now is not None else time.time()) - reference
        return idle if idle > self.stall_after_s else None

    def to_public(self) -> dict:
        """The poll shape the UI sees."""
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "label": self.label,
            "params": dict(self.params),
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_s": self.elapsed_s,
            "last_progress_at": self.last_progress_at,
        }


def new_job(kind: str, *, label: str = "", params: dict | None = None) -> CurationJob:
    return CurationJob(
        id=f"{kind}-{uuid.uuid4().hex[:12]}",
        kind=kind,
        label=label or kind,
        params=dict(params or {}),
    )


# ── durable persistence ────────────────────────────────────────────────────────
def jobs_path(state: "ServerState") -> Path:
    return Path(state.vault_path) / ".marginalia" / JOBS_FILENAME


def _persist_payload(jobs: list[CurationJob]) -> dict:
    terminal_positions = [k for k, j in enumerate(jobs) if j.status in _TERMINAL]
    drop: set[int] = set()
    if len(terminal_positions) > RETENTION_CAP:
        drop = set(terminal_positions[:-RETENTION_CAP])
    kept = [j for k, j in enumerate(jobs) if k not in drop]
    return {"version": JOBS_VERSION, "jobs": [asdict(j) for j in kept]}


_PERSIST_LOCK = threading.Lock()


def persist(state: "ServerState") -> None:
    """Atomically write the job list to the sidecar (temp + os.replace).

    Best-effort: a persistence failure never aborts an in-flight job (the queue
    stays correct in memory). Snapshots the list first because the worker runs in
    a thread and may mutate concurrently. Writers are serialized and each one
    snapshots INSIDE the lock, so the last write to land always carries the
    newest queue even when store-executor threads persist concurrently."""
    _coalescer(state).flushed()  # this write covers everything marked dirty so far
    with _PERSIST_LOCK:
        _persist_locked(state)


_COALESCER_LOCK = threading.Lock()


def _coalescer(state: "ServerState") -> PersistCoalescer:
    """The job list's coalescer (one per state/runtime), created on first use."""
    coalescer = getattr(state, "_jobs_persist_coalescer", None)
    if coalescer is None:
        with _COALESCER_LOCK:
            coalescer = getattr(state, "_jobs_persist_coalescer", None)
            if coalescer is None:
                coalescer = PersistCoalescer(lambda: persist(state), name="curation-jobs")
                try:
                    state._jobs_persist_coalescer = coalescer  # type: ignore[attr-defined]
                except AttributeError:
                    pass
    return coalescer


def request_persist(state: "ServerState") -> None:
    """Mark the job list dirty; ONE background flush writes it within a couple of seconds.

    For runner progress ticks only. Submit, every worker status transition and the linked
    terminal outcome still call :func:`persist` directly, so a crash loses at most the
    flush interval of progress text, never a state transition."""
    _coalescer(state).mark_dirty()


def shutdown_flush(state: "ServerState") -> bool:
    """Stop the background flush and write once more if anything is pending."""
    return _coalescer(state).close()


def _persist_locked(state: "ServerState") -> None:
    path = jobs_path(state)
    payload = _persist_payload(list(state.curation_jobs))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f"{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, indent=2))
            os.replace(tmp_name, path)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            return
    except OSError:
        return


def rehydrate_jobs(state: "ServerState") -> None:
    """Restore ``state.curation_jobs`` on startup.

    Crash-interrupted idempotent jobs are reset to ``queued``. Rebuild-family
    jobs are terminal because the generic queue record cannot prove a safe phase
    resume; an operator must inspect retained evidence and submit a new job.
    Unknown keys are ignored so an older sidecar loads.
    """
    path = jobs_path(state)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return
    known = {f.name for f in fields(CurationJob)}
    restored: list[CurationJob] = []
    changed = False
    for entry in data.get("jobs", []) if isinstance(data, dict) else []:
        if not isinstance(entry, dict):
            continue
        kwargs = {k: v for k, v in entry.items() if k in known}
        if not {"id", "kind"} <= set(kwargs):
            continue
        try:
            job = CurationJob(**kwargs)
        except TypeError:
            continue
        if job.status == "running":
            changed = True
            if job.kind in _NON_RESUMABLE_ON_RESTART_KINDS:
                recovery_note: str | None = None
                recovery = _INTERRUPTED_RECOVERY.get(job.kind)
                if recovery is not None:
                    try:
                        recovery_note = recovery(state, job)
                    except Exception as exc:  # noqa: BLE001 - retain terminal evidence
                        recovery_note = f"interrupted recovery failed: {type(exc).__name__}: {exc}"
                job.status = "error"
                job.progress = "error"
                job.error = (
                    "job interrupted by process restart; inspect retained state "
                    "and submit a new job explicitly"
                )
                if recovery_note:
                    job.error = f"{job.error}; {recovery_note}"
                job.finished_at = time.time()
            else:
                job.status = "queued"
                job.started_at = None
                job.progress = ""
        restored.append(job)
    state.curation_jobs = restored
    if changed:
        persist(state)


# ── enqueue / lookup / snapshot ──────────────────────────────────────────────--
def submit(
    state: "ServerState", kind: str, *, label: str = "", params: dict | None = None
) -> CurationJob:
    """Enqueue a job and ensure the worker is running. Raises ``KeyError`` for an
    unregistered kind (caller maps to a 400/500)."""
    if kind not in _REGISTRY:
        raise KeyError(f"no runner registered for job kind {kind!r}")
    if getattr(state, "draining", False):
        raise RuntimeError("vault runtime is draining; cannot submit curation work")
    job = new_job(kind, label=label, params=params)
    state.curation_jobs.append(job)
    persist(state)
    ensure_worker(state)
    return job


def enqueue_during_drain(
    state: "ServerState",
    kind: str,
    *,
    label: str = "",
    params: dict | None = None,
) -> CurationJob:
    """Persist work discovered while maintenance or shutdown blocks submission.

    This narrow seam is for follow-up work whose owning graph commit already
    verified while the runtime was draining. The active worker resumes it when
    maintenance clears; process shutdown leaves it queued for startup rehydration.
    """
    if kind not in _REGISTRY:
        raise KeyError(f"no runner registered for job kind {kind!r}")
    if not getattr(state, "draining", False):
        raise RuntimeError("enqueue_during_drain requires a draining runtime")
    job = new_job(kind, label=label, params=params)
    state.curation_jobs.append(job)
    persist(state)
    return job


def get_job(state: "ServerState", job_id: str) -> CurationJob | None:
    return next((j for j in state.curation_jobs if j.id == job_id), None)


def latest_of_kind(state: "ServerState", kind: str) -> CurationJob | None:
    matches = [j for j in state.curation_jobs if j.kind == kind]
    return matches[-1] if matches else None


def summary(state: "ServerState", *, kind: str | None = None) -> dict:
    """Job summary counts only: one pass, no ``to_public`` payloads (status hot path)."""
    counts: Counter[str] = Counter()
    for j in state.curation_jobs:
        if kind is None or j.kind == kind:
            counts[j.status] += 1
    return {
        "total": sum(counts.values()),
        "queued": counts["queued"],
        "running": counts["running"],
        "done": counts["done"],
        "error": counts["error"],
        "active": state.curation_worker_active,
    }


def snapshot(state: "ServerState", *, kind: str | None = None) -> dict:
    jobs = [j for j in state.curation_jobs if kind is None or j.kind == kind]
    return {
        "status": "ok",
        "summary": summary(state, kind=kind),
        "jobs": [j.to_public() for j in jobs],
    }


# ── worker ─────────────────────────────────────────────────────────────────────
def ensure_worker(state: "ServerState") -> None:
    """Start and retain the drain worker if one is not already running.

    Safe to call from a store-executor thread: the start is handed back to the
    event loop that dispatched that work."""
    if call_soon_on_loop(ensure_worker, state):
        return
    existing = getattr(state, "curation_worker_task", None)
    if state.curation_worker_active or (existing is not None and not existing.done()):
        return
    if state.draining:
        return
    state.curation_worker_active = True
    state.curation_worker_task = asyncio.ensure_future(_drain(state))


def _set_progress(state: "ServerState", job: CurationJob, stage: str) -> None:
    """Progress callback handed to runners. Mutates the live job (so a poll sees
    it immediately) and marks the list dirty; the coalesced flush writes it. Runs in
    the to_thread worker; marking is cheap and thread-safe."""
    job.progress = stage
    job.last_progress_at = time.time()
    request_persist(state)


class JobStalledError(RuntimeError):
    """A read-only job reported no progress for longer than its watchdog limit."""


def _job_stall_timeout(state: "ServerState") -> float | None:
    from okto_neuron.server._scheduler import _load_scheduler_config

    return _load_scheduler_config(state).job_stall_timeout_s


def stalled_jobs(state: "ServerState", now: float | None = None) -> list[CurationJob]:
    """Running jobs past their no-progress limit (for the status payload)."""
    return [job for job in state.curation_jobs if job.stalled_for_s(now) is not None]


async def _run_watched(
    job: CurationJob,
    run: Callable[[], dict],
    *,
    abandon_ok: bool,
    orphans: list["asyncio.Future[dict]"],
) -> dict:
    """Run a job's runner on the job pool under the no-progress watchdog.

    A runner that goes ``job.stall_after_s`` seconds without calling
    ``progress()`` is presumed stuck (a model server that accepted the
    connection and never answered). A read-only job is then failed and
    *abandoned*: its worker thread cannot be interrupted, so it is handed to
    ``orphans`` and the caller releases the writer lock. A job that writes is
    never abandoned (a thread still writing while another writer starts would
    corrupt state); it is left running and stays visible as
    ``curation_job_stalled`` until its own deadlines end it.
    """
    task = asyncio.ensure_future(job_io(run))
    stall_s = job.stall_after_s
    if stall_s is None:
        return await task
    poll = min(5.0, max(0.02, stall_s / 4))
    warned = False
    while True:
        done, _ = await asyncio.wait({task}, timeout=poll)
        if done:
            return task.result()
        idle = job.stalled_for_s()
        if idle is None:
            continue
        if abandon_ok:
            orphans.append(task)
            task.add_done_callback(lambda fut: fut.cancelled() or fut.exception())
            raise JobStalledError(
                f"job made no progress for {idle:.0f}s (limit {stall_s:.0f}s) and was abandoned"
            )
        if not warned:
            warned = True
            _LOG.error(
                "curation job %s (%s) has made no progress for %.0fs; it writes, so it is "
                "not abandoned",
                job.id,
                job.kind,
                idle,
            )


def _waits_for_ingest_quiet(state: "ServerState", job: CurationJob) -> bool:
    """Coalesce per-file D8 triggers until the current durable batch drains."""
    return bool(
        job.kind == "reconcile-propose"
        and job.params.get("trigger") == "verified_file_commit"
        and getattr(state, "ingest_worker_active", False)
    )


def _reconcile_failure_outcome(job: CurationJob, exc: Exception) -> dict[str, object]:
    return {
        "state": "failed",
        "stage": "propose",
        "job_id": job.id,
        "trigger": str(job.params.get("trigger") or "manual"),
        "error_category": type(exc).__name__,
        "error": str(exc)[:500],
    }


async def _publish_linked_terminal_outcome(state: "ServerState", job: CurationJob) -> None:
    """Publish after ``to_thread`` returns, while queue ownership is on-loop.

    The ingest items are updated here on the loop; only the sidecar write goes
    to the store executor."""
    if job.kind != "reconcile-propose" or not isinstance(job.result, dict):
        return
    outcome = job.result.get("outcome")
    if not isinstance(outcome, dict):
        return
    from okto_neuron.server import _ingest_queue
    from okto_neuron.server._curation import _publish_linked_reconcile_outcome

    if _publish_linked_reconcile_outcome(
        state,
        job.params.get("ingest_item_ids"),
        outcome,
        expected_job_id=job.id,
        persist_now=False,
    ):
        await store_io(_ingest_queue.persist, state)


async def _drain(state: "ServerState") -> None:
    # Capture the running loop once (ADR 0009 P3). A runner executes off-loop in
    # ``asyncio.to_thread`` and cannot call ``get_running_loop`` itself; the
    # rebuild/heal runner reads ``state.loop`` to marshal its atomic graph-swap
    # back onto this loop as a single no-await block. No deadlock: while the runner
    # blocks on that marshaled call, ``_drain`` is suspended at ``await to_thread``,
    # so the loop is free to run the swap coroutine.
    state.loop = asyncio.get_running_loop()
    try:
        while not state.draining:
            job = None
            parked_changed = False
            for candidate in state.curation_jobs:
                if candidate.status != "queued":
                    continue
                if _waits_for_ingest_quiet(state, candidate):
                    if candidate.progress != "waiting for ingest batch to finish":
                        candidate.progress = "waiting for ingest batch to finish"
                        parked_changed = True
                    continue
                job = candidate
                break
            if parked_changed:
                await store_io(persist, state)
            if job is None:
                break
            entry = _REGISTRY.get(job.kind)
            if entry is None:
                exc = RuntimeError(f"no runner registered for kind {job.kind!r}")
                job.status = "error"
                job.error = str(exc)
                if job.kind == "reconcile-propose":
                    job.result = {"outcome": _reconcile_failure_outcome(job, exc)}
                job.finished_at = time.time()
                await _publish_linked_terminal_outcome(state, job)
                await store_io(persist, state)
                continue
            runner, writes = entry
            verified_snapshot = job.kind in _VERIFIED_SNAPSHOT_KINDS
            job.status = "running"
            job.started_at = time.time()
            job.last_progress_at = job.started_at
            job.progress = "starting"
            try:
                job.stall_after_s = await store_io(_job_stall_timeout, state)
            except Exception:  # noqa: BLE001 - a config read must not fail the job
                job.stall_after_s = None
            await store_io(persist, state)

            try:
                lease_factory = getattr(state, "lease_vault", None)
                # Leasing may open the vault under the pool lock; never on the loop.
                lease_context = (
                    await acquire_off_loop(lease_factory)
                    if callable(lease_factory)
                    else contextlib.nullcontext()
                )

                def _run() -> dict:
                    # Closure over the worker's job; runners get a progress setter.
                    def progress(stage: str) -> None:
                        _set_progress(state, job, stage)

                    return runner(state, _JobView(job, progress, lease_context))

                # The job's immutable VaultRuntime owns both its sidecar and its
                # graph lease. Selection changes in a browser cannot retarget it.
                orphans: list[asyncio.Future[dict]] = []
                stack = contextlib.ExitStack()
                leased_vault = stack.enter_context(lease_context)
                try:
                    if writes or verified_snapshot:
                        async with state.writer_lock:
                            vault = leased_vault or getattr(state, "vault", None)
                            if vault is not None and job.kind not in _INTEGRITY_RECOVERY_KINDS:
                                await store_io(
                                    graph_integrity.require_write_allowed,
                                    state,
                                    vault,
                                )
                            result = await _run_watched(
                                job, _run, abandon_ok=not writes, orphans=orphans
                            )
                    else:
                        result = await _run_watched(
                            job, _run, abandon_ok=not writes, orphans=orphans
                        )
                finally:
                    if orphans:
                        # The abandoned worker thread may still be reading the
                        # graph: keep its lease until it actually returns.
                        orphans[0].add_done_callback(lambda _fut: stack.close())
                    else:
                        stack.close()
                job.result = result if isinstance(result, dict) else {"result": result}
                job.status = "done"
                job.progress = "done"
            except Exception as exc:  # noqa: BLE001 — per-job isolation
                # Record the full traceback + chained cause, not just str(exc).
                # ``job.error`` is a terse one-liner persisted to the sidecar;
                # without this the real failure (e.g. an IngestError's __cause__)
                # was lost everywhere. logger.exception attaches exc_info so the
                # cause chain lands in the server log for diagnosis.
                _LOG.exception(
                    "curation job %s (%s) failed: %s",
                    getattr(job, "id", "?"),
                    getattr(job, "kind", "?"),
                    exc,
                )
                job.status = "error"
                job.error = str(exc)
                job.progress = "error"
                if job.kind == "reconcile-propose":
                    job.result = {"outcome": _reconcile_failure_outcome(job, exc)}
            job.finished_at = time.time()
            await _publish_linked_terminal_outcome(state, job)
            await store_io(persist, state)
            await asyncio.sleep(0)  # yield so polls stay responsive
    finally:
        state.curation_worker_active = False
        if getattr(state, "curation_worker_task", None) is asyncio.current_task():
            state.curation_worker_task = None


class _JobView:
    """Thin view passed to a runner: the job plus a ``progress(stage)`` setter.

    Keeps runners decoupled from the worker's persistence — they call
    ``job.progress("…")`` and read ``job.params`` without touching the queue."""

    def __init__(
        self,
        job: CurationJob,
        progress: Callable[[str], None],
        vault_lease: object | None = None,
    ) -> None:
        self._job = job
        self._progress = progress
        self._vault_lease = vault_lease

    @property
    def id(self) -> str:
        return self._job.id

    @property
    def kind(self) -> str:
        return self._job.kind

    @property
    def params(self) -> dict:
        return self._job.params

    def progress(self, stage: str) -> None:
        self._progress(stage)

    def release_vault_lease_for_swap(self) -> None:
        """Release this job's graph borrow after its target path is fenced.

        Vault-wide runners call this at their final swap boundary. The worker's
        surrounding context manager is idempotent, so its later ``__exit__`` is
        harmless. Ordinary runners never call this and retain their full-job
        lease as before.
        """
        release = getattr(self._vault_lease, "release", None)
        if callable(release):
            release()


__all__ = [
    "CurationJob",
    "InterruptedJobRecovery",
    "JobRunner",
    "JobStalledError",
    "stalled_jobs",
    "JOBS_FILENAME",
    "JOBS_VERSION",
    "RETENTION_CAP",
    "register_runner",
    "registered_kinds",
    "requires_verified_snapshot",
    "new_job",
    "submit",
    "enqueue_during_drain",
    "get_job",
    "latest_of_kind",
    "snapshot",
    "ensure_worker",
    "jobs_path",
    "persist",
    "rehydrate_jobs",
]
