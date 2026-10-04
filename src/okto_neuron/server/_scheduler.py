"""Continuous curation loop — the debounced in-process scheduler (ADR 0009 P4).

The serve process runs one asyncio background task (started in runtime's
``_run_async`` alongside the ingest worker). It enumerates every application
runtime and auto-submits PROPOSE/DETECT SWEEP jobs to that runtime's curation
queue (:mod:`okto_neuron.server._jobs`) as content is ingested. Goal: review
surfaces populate and drift is detected without a manual trigger.

SAFETY (load-bearing). The loop is PROPOSE/DETECT ONLY. It can submit ONLY the
kinds in :data:`SWEEP_KINDS`, all reversible/read-only.
It NEVER auto-runs a destructive or
irreversible op — no apply, heal, rebuild, reembed, or reset. This mirrors ADR
0009's compiled invariant: irreversible/topology ops never auto. The allowlist is
a CODE CONSTANT, not config — a unit test asserts no other kind is ever submitted.

Why propose ≠ queue-fill (the honest reading of the goal). ``run_propose`` writes
NOTHING; it returns candidate clusters + verdicts in the job RESULT payload. Only
``run_apply`` writes ``reconcile/queue.json`` and auto-merges — and the no-auto-apply
invariant FORBIDS calling apply. So the standing reconcile review queue does NOT
auto-fill from a propose-only sweep; candidate merges surface via the propose job's
result (rendered in the dashboard) and detect-drift mints deterministic Findings.
queue.json population stays user-initiated.

Decision is a PURE function (:func:`_should_sweep`) so the debounce / coalesce /
min-interval policy is testable directly with an injected clock and stubbed submit;
the async loop is a thin wrapper.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Collection
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from okto_neuron.server._store_io import store_io

if TYPE_CHECKING:
    from okto_neuron.config import CurationSchedulerConfig
    from okto_neuron.server.state import ServerState

_LOG = logging.getLogger("okto_neuron.server.scheduler")

# Short tick so a long config interval never delays SIGTERM shutdown — the loop
# wakes every SCHED_TICK_S, re-reads config, and re-evaluates eligibility.
SCHED_TICK_S = 5.0

# The ONLY job kinds the scheduler may ever submit (propose/detect-only safety
# guard). A code constant, NOT config. All are reversible/read-only. Reconcile
# propose uses the queue's serialization bit so its graph snapshot stays behind
# the writer lock; that does not give it a graph mutation path.
SWEEP_KINDS: frozenset[str] = frozenset({"reconcile-propose", "predicate-propose", "detect-drift"})

# Rebuild-family kinds that, while queued/running, also suppress an auto sweep:
# they set that runtime's ``draining`` flag once running, but a still-queued one
# has not yet, so count it as pending too (harmless, but keeps its queue tidy).
_VAULTWIDE_KINDS: frozenset[str] = frozenset({"rebuild", "rollback", "heal", "reembed"})


def _load_scheduler_config(
    state: "ServerState", *, vault_path: "Path | str | None" = None
) -> "CurationSchedulerConfig":
    """Re-read the curation-loop config from a vault's okto-neuron.yaml so a
    toggle takes effect live without a restart. Falls back to defaults on any load
    failure (mirrors ``_curation._load_config``).

    ``vault_path`` keeps the direct-``ServerState`` compatibility branch scoped
    to its target. Application runtimes pass their own immutable path."""
    from okto_neuron.config import CurationSchedulerConfig, VaultConfig

    path = vault_path if vault_path is not None else state.vault_path
    try:
        return VaultConfig.load(path).curation
    except Exception:  # noqa: BLE001 — missing/partial/invalid config → defaults
        return CurationSchedulerConfig()


def _compatibility_vault_key(state: "ServerState") -> str | None:
    """The legacy fallback's resolved-path string, or ``None`` without one.

    The canonical key format for ``last_ingest_at_by_vault``/
    ``last_sweep_at_by_vault`` and a sweep job's ``params["vault"]`` — always
    ``str(Path(...).resolve(strict=False))`` so pool/fallback keys compare equal
    (matches ``VaultPool._key`` and ``Vault.path``, both resolved)."""
    path = getattr(state, "vault_path", None)
    if path is None:
        return None
    return str(Path(path).resolve(strict=False))


def _sweep_job_pending(state: "ServerState", vault_key: str | None = None) -> bool:
    """True iff a SWEEP-kind job (auto OR manual) is queued/running FOR THIS
    VAULT — the per-vault coalesce guard (issue #5). Counts manual runs too (no
    ``params.trigger``), so a user-initiated propose/drift suppresses an auto
    sweep, and a second auto sweep is never queued while one is in flight.

    ``vault_key`` defaults to the compatibility fallback's key. A job with no
    ``params["vault"]`` (every job submitted before this change, and any
    manually-triggered job) is attributed to that fallback, matching the
    single-vault-only behavior this replaces — it does NOT suppress a pooled
    vault's sweep.

    A queued/running runtime-wide op (rebuild/heal/reembed) suppresses a sweep
    for this runtime. In the direct-``ServerState`` compatibility branch, all
    tagged jobs still share that one legacy queue."""
    if vault_key is None:
        vault_key = _compatibility_vault_key(state)
    fallback_key = _compatibility_vault_key(state)
    for job in state.curation_jobs:
        if job.status not in ("queued", "running"):
            continue
        if job.kind in _VAULTWIDE_KINDS:
            return True
        if job.kind in SWEEP_KINDS:
            job_vault = (job.params or {}).get("vault", fallback_key)
            if job_vault == vault_key:
                return True
    return False


def _should_sweep(
    now: float,
    last_ingest_at: float | None,
    last_sweep_at: float | None,
    pending: bool,
    cfg: "CurationSchedulerConfig",
) -> str | None:
    """Pure eligibility decision. Returns a reason string when a sweep should fire,
    else ``None``. ALL conditions must hold (see TRIGGER in the P4 spec):

    1. ``cfg.enabled``.
    2. NEW ACTIVITY since the last sweep (``last_ingest_at`` set and strictly newer
       than ``last_sweep_at``) — prevents re-sweeping with no new data.
    3. QUIET / debounce: ingestion settled (``now - last_ingest_at >=
       quiet_debounce_s``).
    4. MIN-INTERVAL FLOOR: ``now - last_sweep_at >= min_interval_s`` (anti-thrash;
       first run has ``last_sweep_at`` None → treated as 0 → passes).
    5. COALESCE: no sweep-kind job currently pending.
    """
    if not cfg.enabled:
        return None
    floor = last_sweep_at or 0.0
    if last_ingest_at is None or last_ingest_at <= floor:
        return None  # no new ingest since the last sweep
    if now - last_ingest_at < cfg.quiet_debounce_s:
        return None  # ingestion has not settled
    if now - floor < cfg.min_interval_s:
        return None  # min-interval anti-thrash floor
    if pending:
        return None  # a sweep is already queued/running (coalesce)
    return (
        f"new ingest at {last_ingest_at:.0f}, quiet {cfg.quiet_debounce_s}s, "
        f"interval {cfg.min_interval_s}s elapsed"
    )


def _completed_exact_reconcile_covers_latest_ingest(
    state: "ServerState",
    last_ingest_at: float | None,
    *,
    vault_key: str | None = None,
) -> bool:
    """Return whether verified-file reconciliation already covered this ingest.

    Every verified file commit schedules one exact, coalesced reconciliation job.
    The continuous scheduler must still run drift and predicate discovery after
    the quiet period, but repeating the same full reconcile is pure duplicate
    work. A completed exact job covers the scheduler boundary only when it was
    triggered by verified file commits, carries linked item ids and a graph
    generation, and finished no earlier than the latest committed ingest.

    If another ingest commits after the exact job, ``last_ingest_at`` advances
    past ``finished_at`` and the next sweep includes reconciliation again. Vault
    attribution mirrors :func:`_sweep_job_pending` for the legacy shared queue.
    """
    if last_ingest_at is None:
        return False
    fallback_key = _compatibility_vault_key(state)
    for job in reversed(getattr(state, "curation_jobs", ())):
        if job.kind != "reconcile-propose" or job.status != "done":
            continue
        params = job.params or {}
        if params.get("trigger") != "verified_file_commit":
            continue
        linked_ids = params.get("ingest_item_ids")
        if not isinstance(linked_ids, list) or not linked_ids:
            continue
        if not str(params.get("graph_generation") or ""):
            continue
        job_vault = params.get("vault", fallback_key)
        if vault_key is not None and job_vault != vault_key:
            continue
        finished_at = job.finished_at
        if isinstance(finished_at, (int, float)) and finished_at >= last_ingest_at:
            return True
    return False


def next_eligible(
    now: float,
    last_ingest_at: float | None,
    last_sweep_at: float | None,
    cfg: "CurationSchedulerConfig",
) -> float | str:
    """The earliest time a sweep could next fire, for the UI. Returns a float epoch
    when new activity is pending, else a human string. Uses the SAME formula as
    :func:`_should_sweep` so the surfaced estimate never contradicts the policy."""
    if not cfg.enabled:
        return "disabled"
    floor = last_sweep_at or 0.0
    if last_ingest_at is None or last_ingest_at <= floor:
        return "waiting for ingest"
    return max(floor + cfg.min_interval_s, last_ingest_at + cfg.quiet_debounce_s)


def _submit_sweeps(
    state: "ServerState",
    now: float,
    reason: str,
    *,
    submit: Callable[..., object] | None = None,
    vault_key: str | None = None,
    mirror_scalar: bool = True,
    covered_kinds: Collection[str] = (),
) -> list[str]:
    """Submit sweep jobs, record the outcome, and return the submitted job ids.
    ``submit`` is injectable for tests; defaults to ``_jobs.submit``.

    ``vault_key`` (issue #5 — multi-vault sweeps) tags every submitted job's
    params with ``"vault": vault_key`` (in addition to the existing
    ``"trigger": "scheduler"``) and records ``last_sweep_at_by_vault[vault_key]``.
    ``None`` (the default — every pre-existing call site) preserves the original
    untagged ``params={"trigger": "scheduler"}`` shape exactly.

    ``mirror_scalar`` (default True) additionally mirrors onto legacy fallback
    scalars. Application runtimes call ``note_sweep`` on their own context;
    ``mirror_scalar`` only serves the direct-``ServerState`` compatibility path.

    SAFETY: only kinds in :data:`SWEEP_KINDS` are ever submitted here — the loop has
    no other submit path. (Asserted in tests.)"""
    if submit is None:
        from okto_neuron.server import _jobs

        submit = _jobs.submit

    params: dict[str, object] = {"trigger": "scheduler"}
    if vault_key is not None:
        params["vault"] = vault_key

    covered = frozenset(covered_kinds) & SWEEP_KINDS
    submitted: list[str] = []
    for kind in sorted(SWEEP_KINDS - covered):
        assert kind in SWEEP_KINDS  # belt-and-braces: never submit off-allowlist
        job = submit(state, kind, label=f"auto {kind}", params=dict(params))
        submitted.append(getattr(job, "id", str(job)))

    if vault_key is not None:
        by_vault = getattr(state, "last_sweep_at_by_vault", None)
        if by_vault is not None:
            by_vault[vault_key] = now

    outcome = {
        "at": now,
        "submitted": submitted,
        "covered": sorted(covered),
        "reason": reason,
    }
    note_sweep = getattr(state, "note_sweep", None)
    if callable(note_sweep):
        note_sweep(now, outcome)
    elif mirror_scalar:
        state.last_sweep_at = now
        state.last_sweep_outcome = outcome

    _LOG.info(
        "continuous curation: submitted sweep (%s)%s: %s",
        reason,
        f" [vault={vault_key}]" if vault_key is not None else "",
        submitted,
    )
    return submitted


def _tick(state: "ServerState", now: float) -> None:
    """Evaluate every application runtime and submit eligible scoped sweeps.

    Synchronous and injectable-clock-friendly (the async loop calls it with
    ``time.time()``). The first branch is the production application path and
    requires no selected browser tab or process fallback. The remainder retains
    the old direct-``ServerState`` shape for compatibility tests and callers."""
    if getattr(state, "multi_vault_runtime_enabled", False):
        runtimes = state.runtimes(discover=True)
        for runtime in runtimes:
            if runtime.draining:
                continue
            cfg = _load_scheduler_config(runtime)
            reason = _should_sweep(
                now,
                runtime.last_ingest_at,
                runtime.last_sweep_at,
                _sweep_job_pending(runtime),
                cfg,
            )
            if reason is not None:
                covered = (
                    {"reconcile-propose"}
                    if _completed_exact_reconcile_covers_latest_ingest(
                        runtime, runtime.last_ingest_at
                    )
                    else set()
                )
                # The queue/sidecar itself is already scoped to ``runtime``;
                # params no longer need the old cross-vault target tag.
                _submit_sweeps(runtime, now, reason, covered_kinds=covered)
        return

    if not state.has_vault:
        return
    fallback_path = Path(state.vault_path).resolve(strict=False)
    fallback_key = str(fallback_path)
    pool = getattr(state, "vault_pool", None)
    pool_paths = pool.paths() if pool is not None else []
    targets = {fallback_path} | {Path(p).resolve(strict=False) for p in pool_paths}

    ingest_by_vault = getattr(state, "last_ingest_at_by_vault", None) or {}
    sweep_by_vault = getattr(state, "last_sweep_at_by_vault", None) or {}

    for target in targets:
        key = str(target)
        is_fallback = key == fallback_key
        cfg = _load_scheduler_config(state, vault_path=target)
        if not cfg.enabled:
            continue
        if is_fallback:
            # Fall back to the scalars when the dict has no entry yet (a fresh
            # process, or a legacy state built before this change) — the fallback
            # bump sites keep both in lockstep going forward, but a fallback
            # keeps old callers/tests that only ever set the scalar working.
            last_ingest_at = ingest_by_vault.get(key, state.last_ingest_at)
            last_sweep_at = sweep_by_vault.get(key, state.last_sweep_at)
        else:
            last_ingest_at = ingest_by_vault.get(key)
            last_sweep_at = sweep_by_vault.get(key)
        pending = _sweep_job_pending(state, key)
        reason = _should_sweep(now, last_ingest_at, last_sweep_at, pending, cfg)
        if reason is not None:
            covered = (
                {"reconcile-propose"}
                if _completed_exact_reconcile_covers_latest_ingest(
                    state, last_ingest_at, vault_key=key
                )
                else set()
            )
            _submit_sweeps(
                state,
                now,
                reason,
                vault_key=key,
                mirror_scalar=is_fallback,
                covered_kinds=covered,
            )


async def run_scheduler(state: "ServerState") -> None:
    """The continuous-curation background task. Ticks every ``SCHED_TICK_S``.

    Terminates ONLY via task cancellation (runtime cancels it in its ``finally``).
    It does NOT exit on ``state.draining``: ``draining`` is overloaded — a
    rebuild/reembed sets it for the whole runtime (minutes-long) job and a heal sets it
    briefly, so a ``while not draining`` loop would silently DIE after the first
    runtime-wide op and the "continuous" loop would stop forever. Instead the loop
    runs unconditionally and SKIPS its work while draining (which also correctly
    pauses sweeps during a runtime-wide op / shutdown — we don't want to submit
    then anyway)."""
    _LOG.info("continuous curation scheduler started (tick=%.0fs)", SCHED_TICK_S)
    try:
        while True:
            if not state.draining:
                try:
                    # A tick lists the vault registry and reads every vault's
                    # config: store executor, never the event loop (issue #13).
                    await store_io(_tick, state, time.time())
                except Exception:  # noqa: BLE001 — a tick failure must not kill the loop
                    _LOG.exception("continuous curation tick failed")
            await asyncio.sleep(SCHED_TICK_S)
    except asyncio.CancelledError:
        _LOG.info("continuous curation scheduler cancelled")
        raise


__all__ = [
    "SCHED_TICK_S",
    "SWEEP_KINDS",
    "run_scheduler",
    "next_eligible",
    "_should_sweep",
    "_completed_exact_reconcile_covers_latest_ingest",
    "_submit_sweeps",
    "_tick",
    "_load_scheduler_config",
    "_sweep_job_pending",
]
