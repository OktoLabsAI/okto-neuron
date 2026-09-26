"""Continuous curation loop (ADR 0009 P4) — model-free unit tests.

The decision is a PURE function (``_should_sweep``) and the submit step is an
injectable helper (``_submit_sweeps``), so the debounce / coalesce / min-interval
policy and the propose-only safety guard test directly with an injected clock and a
stubbed submit — no event loop, no real vault, no LLM.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_neuron.config import CurationSchedulerConfig
from okto_neuron.server import _curation, _jobs, _scheduler
from okto_neuron.server._vault_pool import VaultPoolError


# ── config parse ──────────────────────────────────────────────────────────────--
def test_scheduler_config_defaults() -> None:
    cfg = CurationSchedulerConfig()
    assert cfg.enabled is True
    assert cfg.quiet_debounce_s == 60
    assert cfg.min_interval_s == 3600


def test_scheduler_config_rejects_extra_keys() -> None:
    with pytest.raises(Exception):
        CurationSchedulerConfig(enabled=True, bogus=1)  # type: ignore[call-arg]


def test_scheduler_config_partial_roundtrips_via_yaml(tmp_path: Path) -> None:
    import yaml

    from okto_neuron.config import VaultConfig

    cfg_path = tmp_path / "okto-neuron.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "marginalia_yaml_version": 1,
                "curation": {"enabled": False, "quiet_debounce_s": 30},
            }
        ),
        encoding="utf-8",
    )
    vc = VaultConfig.load(tmp_path)
    assert vc.curation.enabled is False
    assert vc.curation.quiet_debounce_s == 30
    # min_interval_s left to default
    assert vc.curation.min_interval_s == 3600


def test_scheduler_config_min_interval_must_be_positive() -> None:
    with pytest.raises(Exception):
        CurationSchedulerConfig(min_interval_s=0)


# ── _should_sweep truth table (pure, injected clock) ────────────────────────────
def _cfg(enabled: bool = True, debounce: int = 60, interval: int = 3600) -> CurationSchedulerConfig:
    return CurationSchedulerConfig(
        enabled=enabled, quiet_debounce_s=debounce, min_interval_s=interval
    )


def test_should_sweep_eligible() -> None:
    now = 100_000.0
    # ingest 100s ago (quiet>60), no prior sweep, not pending → eligible
    reason = _scheduler._should_sweep(
        now=now,
        last_ingest_at=now - 100,
        last_sweep_at=None,
        pending=False,
        cfg=_cfg(),
    )
    assert reason is not None


def test_should_sweep_disabled() -> None:
    now = 100_000.0
    reason = _scheduler._should_sweep(now, now - 100, None, False, _cfg(enabled=False))
    assert reason is None


def test_should_sweep_debounce_not_elapsed() -> None:
    now = 100_000.0
    # ingest only 10s ago, debounce 60 → not yet quiet → None
    reason = _scheduler._should_sweep(now, now - 10, None, False, _cfg())
    assert reason is None


def test_should_sweep_min_interval_floor() -> None:
    now = 100_000.0
    # new ingest 100s ago AND quiet, but last sweep only 100s ago < 3600 floor → None
    reason = _scheduler._should_sweep(
        now, last_ingest_at=now - 100, last_sweep_at=now - 100, pending=False, cfg=_cfg()
    )
    assert reason is None


def test_should_sweep_no_new_activity() -> None:
    now = 100_000.0
    # ingest happened BEFORE the last sweep → nothing new → None
    reason = _scheduler._should_sweep(
        now, last_ingest_at=now - 5000, last_sweep_at=now - 4000, pending=False, cfg=_cfg()
    )
    assert reason is None


def test_should_sweep_no_ingest_ever() -> None:
    now = 100_000.0
    reason = _scheduler._should_sweep(now, None, None, False, _cfg())
    assert reason is None


def test_should_sweep_coalesce_pending() -> None:
    now = 100_000.0
    # everything eligible EXCEPT a sweep is already pending → None
    reason = _scheduler._should_sweep(
        now, last_ingest_at=now - 100, last_sweep_at=None, pending=True, cfg=_cfg()
    )
    assert reason is None


def test_should_sweep_min_interval_after_long_gap() -> None:
    now = 100_000.0
    # last sweep 2h ago (> 3600), fresh ingest quiet → eligible again
    reason = _scheduler._should_sweep(
        now, last_ingest_at=now - 100, last_sweep_at=now - 7200, pending=False, cfg=_cfg()
    )
    assert reason is not None


# ── next_eligible ───────────────────────────────────────────────────────────────
def test_next_eligible_disabled() -> None:
    assert _scheduler.next_eligible(100.0, 50.0, None, _cfg(enabled=False)) == "disabled"


def test_next_eligible_waiting_for_ingest() -> None:
    now = 100_000.0
    # ingest before last sweep → nothing pending
    out = _scheduler.next_eligible(
        now, last_ingest_at=now - 5000, last_sweep_at=now - 4000, cfg=_cfg()
    )
    assert out == "waiting for ingest"


def test_next_eligible_is_max_of_floor_and_quiet() -> None:
    now = 100_000.0
    last_ingest = now - 100
    last_sweep = now - 7200
    out = _scheduler.next_eligible(now, last_ingest, last_sweep, _cfg())
    assert out == max(last_sweep + 3600, last_ingest + 60)


# ── propose-only safety guard ───────────────────────────────────────────────────
def test_submit_only_allowlisted_kinds() -> None:
    """The loop's only submit path is ``_submit_sweeps``; assert it submits ONLY
    kinds in the SWEEP_KINDS allowlist (never apply/heal/rebuild/reembed/reset)."""
    submitted_kinds: list[str] = []

    def fake_submit(state, kind, *, label="", params=None):
        submitted_kinds.append(kind)
        return SimpleNamespace(id=f"{kind}-x")

    state = SimpleNamespace(last_sweep_at=None, last_sweep_outcome=None)
    ids = _scheduler._submit_sweeps(state, now=100.0, reason="probe", submit=fake_submit)

    assert set(submitted_kinds) == set(_scheduler.SWEEP_KINDS)
    assert "predicate-propose" in submitted_kinds
    assert all(k in _scheduler.SWEEP_KINDS for k in submitted_kinds)
    # Never a destructive kind.
    for forbidden in ("reconcile-apply", "predicate-apply", "heal", "rebuild", "reembed", "reset"):
        assert forbidden not in submitted_kinds
    assert len(ids) == len(_scheduler.SWEEP_KINDS)
    assert state.last_sweep_at == 100.0
    assert state.last_sweep_outcome["submitted"] == ids
    assert state.last_sweep_outcome["reason"] == "probe"


def test_submit_skips_reconcile_already_covered_by_exact_post_ingest_job() -> None:
    submitted_kinds: list[str] = []

    def fake_submit(state, kind, *, label="", params=None):
        submitted_kinds.append(kind)
        return SimpleNamespace(id=f"{kind}-x")

    state = SimpleNamespace(last_sweep_at=None, last_sweep_outcome=None)
    ids = _scheduler._submit_sweeps(
        state,
        now=100.0,
        reason="probe",
        submit=fake_submit,
        covered_kinds={"reconcile-propose"},
    )

    assert set(submitted_kinds) == {"detect-drift", "predicate-propose"}
    assert len(ids) == 2
    assert state.last_sweep_outcome["covered"] == ["reconcile-propose"]


def test_completed_exact_reconcile_covers_only_the_ingest_it_followed() -> None:
    exact = _jobs.new_job(
        "reconcile-propose",
        params={
            "trigger": "verified_file_commit",
            "ingest_item_ids": ["1-source"],
            "graph_generation": "generation-1",
        },
    )
    exact.status = "done"
    exact.finished_at = 200.0
    state = SimpleNamespace(curation_jobs=[exact], vault_path=Path("/vault"))

    assert _scheduler._completed_exact_reconcile_covers_latest_ingest(state, 199.0)
    assert not _scheduler._completed_exact_reconcile_covers_latest_ingest(state, 201.0)

    exact.params["trigger"] = "scheduler"
    assert not _scheduler._completed_exact_reconcile_covers_latest_ingest(state, 199.0)


def test_tick_submits_when_eligible_and_records_outcome() -> None:
    """Drive a tick with a stub submit; assert it fires sweeps ONLY from the
    allowlist when eligible, and tags them trigger=scheduler."""
    submitted: list[tuple[str, dict]] = []

    def fake_submit(state, kind, *, label="", params=None):
        submitted.append((kind, dict(params or {})))
        return SimpleNamespace(id=f"{kind}-x")

    now = 100_000.0
    state = SimpleNamespace(
        last_ingest_at=now - 100,
        last_sweep_at=None,
        last_sweep_outcome=None,
        curation_jobs=[],
        draining=False,
    )
    cfg = _cfg()
    pending = _scheduler._sweep_job_pending(state)
    reason = _scheduler._should_sweep(now, state.last_ingest_at, state.last_sweep_at, pending, cfg)
    assert reason is not None
    _scheduler._submit_sweeps(state, now, reason, submit=fake_submit)

    kinds = {k for k, _ in submitted}
    assert kinds == set(_scheduler.SWEEP_KINDS)
    assert "predicate-propose" in kinds
    for _, params in submitted:
        assert params.get("trigger") == "scheduler"


def test_tick_no_submit_when_not_eligible() -> None:
    called: list[str] = []

    def fake_submit(state, kind, *, label="", params=None):  # pragma: no cover - must not run
        called.append(kind)
        return SimpleNamespace(id=kind)

    now = 100_000.0
    # ingest only 5s ago → debounce blocks
    state = SimpleNamespace(
        last_ingest_at=now - 5,
        last_sweep_at=None,
        curation_jobs=[],
        draining=False,
    )
    cfg = _cfg()
    pending = _scheduler._sweep_job_pending(state)
    reason = _scheduler._should_sweep(now, state.last_ingest_at, state.last_sweep_at, pending, cfg)
    assert reason is None
    if reason is not None:  # not taken
        _scheduler._submit_sweeps(state, now, reason, submit=fake_submit)
    assert called == []


# ── coalesce / pending detection over real CurationJob objects ──────────────────
def test_sweep_job_pending_counts_manual_and_auto() -> None:
    from okto_neuron.server._jobs import new_job

    state = SimpleNamespace(curation_jobs=[])
    assert _scheduler._sweep_job_pending(state) is False

    # a queued manual propose (no trigger) suppresses an auto sweep
    j = new_job("reconcile-propose", label="manual")
    j.status = "queued"
    state.curation_jobs = [j]
    assert _scheduler._sweep_job_pending(state) is True

    # a running detect-drift counts too
    j2 = new_job("detect-drift")
    j2.status = "running"
    state.curation_jobs = [j2]
    assert _scheduler._sweep_job_pending(state) is True

    # a queued rebuild (vault-wide) suppresses too
    j3 = new_job("rebuild")
    j3.status = "queued"
    state.curation_jobs = [j3]
    assert _scheduler._sweep_job_pending(state) is True

    # a DONE sweep does NOT suppress (only queued/running)
    j4 = new_job("reconcile-propose")
    j4.status = "done"
    state.curation_jobs = [j4]
    assert _scheduler._sweep_job_pending(state) is False


# ── detect-drift runner registration (the kind would KeyError before P4) ────────-
def test_detect_drift_runner_registered() -> None:
    _curation.register_runners()
    assert "detect-drift" in _jobs.registered_kinds()
    # registry entry is read-only (writes=False) — advisory background detection
    runner, writes = _jobs._REGISTRY["detect-drift"]
    assert writes is False
    assert runner is _curation.run_detect_drift


# ── the async loop: draining-skip + survive-draining + cancel (the P4 blocker) ──-
@pytest.mark.asyncio
async def test_loop_skips_draining_survives_it_and_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Guards the LOAD-BEARING fix: while draining the loop does NO work but does
    NOT exit (a rebuild/reembed sets draining for its whole job — exiting would
    silently kill the continuous loop), it RESUMES once draining clears, and it
    terminates ONLY on cancellation. Stubs ``_submit_sweeps`` so no real
    submit/judge/LLM runs."""
    monkeypatch.setattr(_scheduler, "SCHED_TICK_S", 0.01)
    calls: list[str] = []
    monkeypatch.setattr(
        _scheduler,
        "_submit_sweeps",
        lambda s, now, reason, **k: calls.append(reason),
    )
    state = SimpleNamespace(
        has_vault=True,
        draining=True,
        last_ingest_at=time.time() - 1000,  # quiet long past debounce
        last_sweep_at=None,
        curation_jobs=[],
        vault_path=Path("/nonexistent"),  # config load fails → enabled defaults
    )
    task = asyncio.ensure_future(_scheduler.run_scheduler(state))
    try:
        await asyncio.sleep(0.05)
        assert calls == []  # draining → no sweep submitted, loop still alive
        assert not task.done()  # did NOT exit on draining

        state.draining = False
        await asyncio.sleep(0.05)
        assert calls  # resumes once draining clears (survived the draining window)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert task.cancelled() or task.done()  # clean termination only via cancel


@pytest.mark.asyncio
async def test_tick_glue_submits_when_eligible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the real ``_tick`` glue (load config → pending check → submit), not
    just its parts. Stub ``_submit_sweeps`` to keep the real judge/LLM out."""
    submitted: list[str] = []
    monkeypatch.setattr(
        _scheduler, "_submit_sweeps", lambda s, now, reason, **k: submitted.append(reason)
    )
    now = time.time()
    state = SimpleNamespace(
        has_vault=True,
        last_ingest_at=now - 1000,
        last_sweep_at=None,
        curation_jobs=[],
        vault_path=Path("/nonexistent"),  # → CurationSchedulerConfig() enabled defaults
    )
    _scheduler._tick(state, now)
    assert len(submitted) == 1  # eligible → exactly one submit-sweeps invocation


# ── config fallback on bad load ─────────────────────────────────────────────────
def test_load_scheduler_config_falls_back_to_defaults(tmp_path: Path) -> None:
    # No okto-neuron.yaml in tmp_path → VaultConfig.load raises → defaults returned.
    state = SimpleNamespace(vault_path=tmp_path / "nonexistent")
    cfg = _scheduler._load_scheduler_config(state)
    assert isinstance(cfg, CurationSchedulerConfig)
    assert cfg.enabled is True


def test_load_scheduler_config_accepts_explicit_vault_path(tmp_path: Path) -> None:
    """issue #5: a POOLED vault's own config, not the active vault's."""
    import yaml

    active_dir = tmp_path / "active"
    pooled_dir = tmp_path / "pooled"
    active_dir.mkdir()
    pooled_dir.mkdir()
    (pooled_dir / "okto-neuron.yaml").write_text(
        yaml.safe_dump({"marginalia_yaml_version": 1, "curation": {"enabled": False}}),
        encoding="utf-8",
    )
    state = SimpleNamespace(vault_path=active_dir)  # active has no config → defaults
    active_cfg = _scheduler._load_scheduler_config(state)
    pooled_cfg = _scheduler._load_scheduler_config(state, vault_path=pooled_dir)
    assert active_cfg.enabled is True
    assert pooled_cfg.enabled is False


# ── issue #5: multi-vault tick ───────────────────────────────────────────────────
def test_tick_sweeps_pooled_vault_tagged_and_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tick must sweep a POOLED (``?vault=``) vault's ingest activity too, not
    just the active vault's — the bug this fixes. The submitted sweep must be
    tagged with the pooled vault's OWN path and must NOT mirror onto the
    active-vault scalars (that would make the active-vault status surface lie
    about what just happened)."""
    from okto_neuron.server.state import ServerState
    from okto_neuron.vault import Vault

    active = Vault.init(tmp_path / "active", packs=["core"])
    pooled = Vault.init(tmp_path / "pooled", packs=["core"])
    try:
        state = ServerState(vault=active, vault_path=(tmp_path / "active").resolve())
        state.vault_pool.adopt(active, state.vault_path)
        pooled_path = (tmp_path / "pooled").resolve(strict=False)
        state.vault_pool.adopt(pooled, pooled_path)

        now = time.time()
        # Active vault: never ingested → not eligible. Pooled vault: ingested
        # well past the debounce window, never swept → eligible.
        state.last_ingest_at_by_vault[str(pooled_path)] = now - 120

        calls: list[dict] = []
        monkeypatch.setattr(
            _scheduler,
            "_submit_sweeps",
            lambda s, now, reason, **k: calls.append(k),
        )
        _scheduler._tick(state, now)

        assert len(calls) == 1  # ONLY the pooled vault fired, not the active one
        assert calls[0]["vault_key"] == str(pooled_path)
        assert calls[0]["mirror_scalar"] is False  # never mirrors a pooled sweep
        # The active-vault scalar surface is untouched by the pooled sweep.
        assert state.last_sweep_at is None
    finally:
        state.close()


def test_tick_active_vault_sweep_mirrors_scalar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The active vault's own sweep still mirrors onto the legacy scalars (the
    ``/health``/``/api/v1/curation/scheduler`` surface) — unchanged behavior."""
    from okto_neuron.server.state import ServerState
    from okto_neuron.vault import Vault

    active = Vault.init(tmp_path / "active", packs=["core"])
    try:
        state = ServerState(vault=active, vault_path=(tmp_path / "active").resolve())
        state.vault_pool.adopt(active, state.vault_path)
        now = time.time()
        state.last_ingest_at_by_vault[str(state.vault_path)] = now - 120

        calls: list[dict] = []
        monkeypatch.setattr(
            _scheduler,
            "_submit_sweeps",
            lambda s, now, reason, **k: calls.append(k),
        )
        _scheduler._tick(state, now)

        assert len(calls) == 1
        assert calls[0]["vault_key"] == str(state.vault_path)
        assert calls[0]["mirror_scalar"] is True
    finally:
        state.close()


def test_tick_no_active_vault_skips_pooled_too() -> None:
    """KNOWN LIMITATION (documented, not fixed): with no active vault there is
    nowhere to persist a job, so the WHOLE tick — including pooled vaults —
    is skipped."""
    state = SimpleNamespace(has_vault=False)
    # Must not raise even though state has none of the tick's other attributes —
    # has_vault=False short-circuits before anything else is touched.
    _scheduler._tick(state, time.time())


# ── issue #5: per-vault sweep-pending coalesce ───────────────────────────────────
def test_sweep_job_pending_does_not_cross_suppress_vaults() -> None:
    """A pending sweep tagged for vault A must not suppress vault B's sweep."""
    job_a = _jobs.new_job(
        "reconcile-propose", params={"trigger": "scheduler", "vault": "/vaults/a"}
    )
    job_a.status = "queued"
    state = SimpleNamespace(curation_jobs=[job_a], vault_path=Path("/vaults/active"))

    assert _scheduler._sweep_job_pending(state, "/vaults/a") is True
    assert _scheduler._sweep_job_pending(state, "/vaults/b") is False


def test_sweep_job_pending_legacy_untagged_job_is_active_only() -> None:
    """A job with no ``params['vault']`` (every job submitted before issue #5,
    and any manually-triggered job) is attributed to the ACTIVE vault only —
    matching the single-vault behavior this replaces. It must NOT suppress a
    pooled vault's sweep."""
    job = _jobs.new_job("detect-drift")  # no vault param
    job.status = "running"
    active = Path("/vaults/active").resolve(strict=False)
    other = Path("/vaults/other").resolve(strict=False)
    state = SimpleNamespace(curation_jobs=[job], vault_path=active)

    assert _scheduler._sweep_job_pending(state, str(active)) is True
    assert _scheduler._sweep_job_pending(state, str(other)) is False


def test_sweep_job_pending_vaultwide_kind_suppresses_every_vault() -> None:
    """rebuild/heal/reembed suppress a sweep for ALL vaults, regardless of the
    ``vault_key`` being checked — unchanged from the single-vault behavior."""
    job = _jobs.new_job("rebuild")
    job.status = "queued"
    state = SimpleNamespace(curation_jobs=[job], vault_path=Path("/vaults/active"))

    assert _scheduler._sweep_job_pending(state, "/vaults/active") is True
    assert _scheduler._sweep_job_pending(state, "/vaults/other") is True


# ── issue #5: _submit_sweeps vault tagging ───────────────────────────────────────
def test_submit_sweeps_without_vault_key_preserves_original_params_shape() -> None:
    """Every pre-existing call site (no ``vault_key``) must keep the EXACT
    original params shape — no added key, regression guard for existing
    ``recent``/dashboard consumers."""
    submitted_params: list[dict] = []

    def fake_submit(state, kind, *, label="", params=None):
        submitted_params.append(dict(params or {}))
        return SimpleNamespace(id=f"{kind}-x")

    state = SimpleNamespace(last_sweep_at=None, last_sweep_outcome=None)
    _scheduler._submit_sweeps(state, now=100.0, reason="probe", submit=fake_submit)

    assert submitted_params  # sanity: something was submitted
    assert all(params == {"trigger": "scheduler"} for params in submitted_params)


def test_submit_sweeps_vault_key_tags_params_and_records_per_vault() -> None:
    """A vault-tagged submit carries ``params["vault"]`` (in addition to the
    existing ``trigger``), records ``last_sweep_at_by_vault``, and — with
    ``mirror_scalar=False`` — leaves the active-vault scalars untouched."""
    submitted_params: list[dict] = []

    def fake_submit(state, kind, *, label="", params=None):
        submitted_params.append(dict(params or {}))
        return SimpleNamespace(id=f"{kind}-x")

    state = SimpleNamespace(last_sweep_at=None, last_sweep_outcome=None, last_sweep_at_by_vault={})
    _scheduler._submit_sweeps(
        state,
        now=100.0,
        reason="probe",
        submit=fake_submit,
        vault_key="/vaults/a",
        mirror_scalar=False,
    )

    assert all(
        params.get("trigger") == "scheduler" and params.get("vault") == "/vaults/a"
        for params in submitted_params
    )
    assert state.last_sweep_at_by_vault["/vaults/a"] == 100.0
    assert state.last_sweep_at is None  # not mirrored
    assert state.last_sweep_outcome is None  # not mirrored


# ── issue #5: _job_vault sweep-runner target resolution ─────────────────────────
def test_job_vault_defaults_to_active_when_no_vault_param() -> None:
    state = SimpleNamespace(vault="ACTIVE_VAULT_SENTINEL", vault_path=Path("/vaults/active"))
    job = SimpleNamespace(params={})
    vault, path = _curation._job_vault(state, job)
    assert vault == "ACTIVE_VAULT_SENTINEL"
    assert path == Path("/vaults/active")


def test_job_vault_active_path_named_explicitly_returns_active_handle() -> None:
    active_path = Path("/vaults/active")
    state = SimpleNamespace(vault="ACTIVE_VAULT_SENTINEL", vault_path=active_path)
    job = SimpleNamespace(params={"vault": str(active_path)})
    vault, path = _curation._job_vault(state, job)
    assert vault == "ACTIVE_VAULT_SENTINEL"


def test_job_vault_resolves_pooled_target_via_pool(tmp_path: Path) -> None:
    """The runner target-resolution helper fetches a POOLED vault through
    ``state.vault_pool`` when the job names a non-active path."""
    pooled_path = tmp_path / "pooled"
    pooled_path.mkdir()

    class _StubPool:
        def __init__(self) -> None:
            self.requested: list[Path] = []

        def get_or_open(self, path: Path):
            self.requested.append(path)
            return "POOLED_VAULT_SENTINEL"

    pool = _StubPool()
    state = SimpleNamespace(
        vault="ACTIVE_VAULT_SENTINEL",
        vault_path=tmp_path / "active",
        vault_pool=pool,
    )
    job = SimpleNamespace(params={"vault": str(pooled_path)})
    vault, path = _curation._job_vault(state, job)

    assert vault == "POOLED_VAULT_SENTINEL"
    assert path == pooled_path.resolve(strict=False)
    assert pool.requested == [pooled_path.resolve(strict=False)]


def test_job_vault_propagates_vault_pool_error_for_vanished_pooled_vault() -> None:
    """A stale job naming a vault that no longer exists must raise
    ``VaultPoolError`` — callers (the sweep runners) catch this so ONE job
    fails, not the drain worker."""

    class _StubPool:
        def get_or_open(self, path: Path):
            raise VaultPoolError("open_failed", "vault vanished")

    state = SimpleNamespace(
        vault="ACTIVE_VAULT_SENTINEL",
        vault_path=Path("/vaults/active"),
        vault_pool=_StubPool(),
    )
    job = SimpleNamespace(params={"vault": "/vaults/gone"})
    with pytest.raises(VaultPoolError):
        _curation._job_vault(state, job)


# ── issue #5: end-to-end runner regression guard ────────────────────────────────
def test_run_detect_drift_runs_against_pooled_vault_not_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact bug the investigation flagged as the likeliest subtle failure:
    a pooled-vault-tagged sweep runner must operate against the POOLED vault,
    not silently fall back to ``state.vault`` (the active one)."""
    import okto_neuron.detectors as detectors_mod
    from okto_neuron.server.state import ServerState
    from okto_neuron.vault import Vault

    active = Vault.init(tmp_path / "active", packs=["core"])
    pooled = Vault.init(tmp_path / "pooled", packs=["core"])
    try:
        state = ServerState(vault=active, vault_path=(tmp_path / "active").resolve())
        state.vault_pool.adopt(active, state.vault_path)
        pooled_path = (tmp_path / "pooled").resolve(strict=False)
        state.vault_pool.adopt(pooled, pooled_path)

        seen_vaults: list[object] = []
        monkeypatch.setattr(
            detectors_mod,
            "run_detector",
            lambda name, vault: seen_vaults.append(vault) or [],
        )

        job = _jobs.new_job("detect-drift", params={"vault": str(pooled_path)})
        view = _jobs._JobView(job, lambda stage: None)
        result = _curation.run_detect_drift(state, view)

        assert result["total"] == 0
        assert seen_vaults, "run_detector was never called"
        assert all(v is pooled for v in seen_vaults)
        assert all(v is not active for v in seen_vaults)
    finally:
        state.close()


def test_sweep_kinds_still_exclude_predicate_apply() -> None:
    """ADR 0017 D6 boundary pin.

    ADR 0040 D6a adds ingest-time predicate folding. That is a different
    concern from the maintenance sweep over committed graph state, which stays
    propose-only: `predicate-apply` is registered as a writing runner but is
    never scheduled automatically.
    """

    assert _scheduler.SWEEP_KINDS == frozenset(
        {"reconcile-propose", "predicate-propose", "detect-drift"}
    )
    assert "predicate-apply" not in _scheduler.SWEEP_KINDS

    _curation.register_runners()
    assert _jobs._REGISTRY["predicate-propose"][1] is False
    assert _jobs._REGISTRY["predicate-apply"][1] is True
