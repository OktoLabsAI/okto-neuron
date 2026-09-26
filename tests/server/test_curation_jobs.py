"""Generic curation job queue (ADR 0009 P2) — model-free unit tests.

The queue helpers only touch ``state.vault_path`` / ``state.curation_jobs`` /
``state.curation_worker_active`` / ``state.writer_lock``, so a lightweight
``SimpleNamespace`` stub keeps these off the real Ladybug vault and off any LLM.
Runners are stubbed; we test serialization, durability, drain order, writer_lock
gating, and crash-recovery — never the reconcile domain logic.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_neuron.server import _curation, _jobs
from okto_neuron.server._ingest_queue import IngestItem
from okto_neuron.server._integrity import IntegrityFenceError
from okto_neuron.store.integrity import AuditStatus
from okto_neuron.store.integrity_state import GraphIntegrityState


def _state(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        vault_path=root,
        curation_jobs=[],
        curation_worker_active=False,
        curation_worker_task=None,
        draining=False,
        shutting_down=False,
    )


@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts with a clean runner registry."""
    saved = dict(_jobs._REGISTRY)
    saved_recovery = dict(_jobs._INTERRUPTED_RECOVERY)
    saved_snapshots = set(_jobs._VERIFIED_SNAPSHOT_KINDS)
    _jobs._REGISTRY.clear()
    _jobs._INTERRUPTED_RECOVERY.clear()
    _jobs._VERIFIED_SNAPSHOT_KINDS.clear()
    yield
    _jobs._REGISTRY.clear()
    _jobs._REGISTRY.update(saved)
    _jobs._INTERRUPTED_RECOVERY.clear()
    _jobs._INTERRUPTED_RECOVERY.update(saved_recovery)
    _jobs._VERIFIED_SNAPSHOT_KINDS.clear()
    _jobs._VERIFIED_SNAPSHOT_KINDS.update(saved_snapshots)


def test_submit_persists_and_enqueues(tmp_path: Path) -> None:
    _jobs.register_runner("noop", lambda s, j: {"ok": True}, writes=False)
    state = _state(tmp_path / "vault")
    # Avoid kicking the real asyncio worker (no loop here) — only test enqueue+persist.
    state.curation_worker_active = True  # ensure_worker short-circuits
    job = _jobs.submit(state, "noop", label="probe", params={"x": 1})

    assert job.status == "queued"
    assert job.kind == "noop"
    assert job.params == {"x": 1}
    sidecar = _jobs.jobs_path(state)
    assert sidecar.exists()
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["version"] == _jobs.JOBS_VERSION
    assert data["jobs"][0]["kind"] == "noop"
    assert data["jobs"][0]["status"] == "queued"


def test_submit_unknown_kind_raises(tmp_path: Path) -> None:
    state = _state(tmp_path / "vault")
    with pytest.raises(KeyError):
        _jobs.submit(state, "does-not-exist")


def test_verified_commit_schedules_propose_only_and_links_outcome(tmp_path: Path) -> None:
    _jobs.register_runner(
        "reconcile-propose",
        lambda _s, _j: {},
        writes=False,
        verified_snapshot=True,
    )
    state = _state(tmp_path / "vault")
    state.curation_worker_active = True  # inspect the durable queued job; do not drain it
    item = IngestItem(
        id="ingest-1",
        name="book.md",
        path="/book.md",
        status="done",
        outcome={"quality": "complete"},
    )
    state.ingest_queue = [item]

    outcome = _curation.attach_verified_reconciliation_outcome(
        state,
        {
            "quality": "complete",
            "integrity": {
                "status": "verified",
                "graph_generation": "generation-a",
            },
        },
        trigger="verified_file_commit",
        ingest_item_id=item.id,
    )

    assert len(state.curation_jobs) == 1
    job = state.curation_jobs[0]
    assert job.kind == "reconcile-propose"
    assert job.params == {
        "trigger": "verified_file_commit",
        "ingest_item_ids": [item.id],
        "graph_generation": "generation-a",
    }
    assert outcome["cross_document_reconciliation"] == {
        "state": "scheduled",
        "job_id": job.id,
        "trigger": "verified_file_commit",
        "coalesced": False,
    }
    assert item.status == "done"
    assert item.outcome["cross_document_reconciliation"]["state"] == "scheduled"


def test_verified_commits_coalesce_only_into_a_queued_snapshot(tmp_path: Path) -> None:
    _jobs.register_runner(
        "reconcile-propose",
        lambda _s, _j: {},
        writes=False,
        verified_snapshot=True,
    )
    state = _state(tmp_path / "vault")
    state.curation_worker_active = True
    first = IngestItem(id="one", name="one.md", path="/one.md", status="done")
    second = IngestItem(id="two", name="two.md", path="/two.md", status="done")
    third = IngestItem(id="three", name="three.md", path="/three.md", status="done")
    state.ingest_queue = [first, second, third]
    verified = {"integrity": {"status": "verified", "graph_generation": "g"}}

    _curation.attach_verified_reconciliation_outcome(
        state, verified, trigger="verified_file_commit", ingest_item_id=first.id
    )
    second_outcome = _curation.attach_verified_reconciliation_outcome(
        state, verified, trigger="verified_file_commit", ingest_item_id=second.id
    )

    assert len(state.curation_jobs) == 1
    assert state.curation_jobs[0].params["ingest_item_ids"] == ["one", "two"]
    assert second_outcome["cross_document_reconciliation"]["coalesced"] is True

    # A running proposal may have captured its graph snapshot before the third
    # commit. It cannot represent that commit, so the new boundary gets a later job.
    state.curation_jobs[0].status = "running"
    third_outcome = _curation.attach_verified_reconciliation_outcome(
        state, verified, trigger="verified_file_commit", ingest_item_id=third.id
    )
    assert len(state.curation_jobs) == 2
    assert state.curation_jobs[1].params["ingest_item_ids"] == ["three"]
    assert third_outcome["cross_document_reconciliation"]["coalesced"] is False


def test_reconciliation_schedule_failure_does_not_reclassify_verified_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path / "vault")
    item = IngestItem(
        id="ingest-1",
        name="book.md",
        path="/book.md",
        status="done",
        outcome={"quality": "complete"},
    )
    state.ingest_queue = [item]

    def fail_submit(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("queue unavailable")

    monkeypatch.setattr(_jobs, "submit", fail_submit)
    outcome = _curation.attach_verified_reconciliation_outcome(
        state,
        {"quality": "complete", "integrity": {"status": "verified"}},
        trigger="verified_file_commit",
        ingest_item_id=item.id,
    )

    semantic = outcome["cross_document_reconciliation"]
    assert semantic["state"] == "failed"
    assert semantic["stage"] == "schedule"
    assert item.status == "done"
    assert item.outcome["quality"] == "complete"
    assert item.outcome["cross_document_reconciliation"]["state"] == "failed"


def test_disabled_curation_records_skipped_without_enqueuing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path / "vault")
    item = IngestItem(
        id="ingest-1",
        name="book.md",
        path="/book.md",
        status="done",
        outcome={"quality": "complete"},
    )
    state.ingest_queue = [item]
    monkeypatch.setattr(
        _curation,
        "_load_config",
        lambda _state: SimpleNamespace(curation=SimpleNamespace(enabled=False)),
    )

    outcome = _curation.attach_verified_reconciliation_outcome(
        state,
        {"quality": "complete", "integrity": {"status": "verified"}},
        trigger="verified_file_commit",
        ingest_item_id=item.id,
    )

    assert state.curation_jobs == []
    assert outcome["cross_document_reconciliation"]["state"] == "skipped"
    assert item.status == "done"


def test_shutdown_interrupted_schedule_defers_durable_job_until_restart(
    tmp_path: Path,
) -> None:
    _jobs.register_runner(
        "reconcile-propose",
        lambda _state, _job: {},
        writes=False,
        verified_snapshot=True,
    )
    state = _state(tmp_path / "vault")
    state.ingest_queue = [
        IngestItem(
            id="ingest-1",
            name="book.md",
            path="/book.md",
            status="done",
            outcome={"quality": "complete"},
        )
    ]
    state.draining = True
    state.shutting_down = True

    interrupted = _curation.attach_verified_reconciliation_outcome(
        state,
        {"quality": "complete", "integrity": {"status": "verified"}},
        trigger="verified_file_commit",
        ingest_item_id="ingest-1",
    )
    semantic = interrupted["cross_document_reconciliation"]
    assert semantic["state"] == "scheduled"
    assert semantic["deferred_until_restart"] is True
    assert len(state.curation_jobs) == 1
    assert state.curation_worker_task is None

    fresh = _state(tmp_path / "vault")
    _jobs.rehydrate_jobs(fresh)
    assert len(fresh.curation_jobs) == 1
    assert fresh.curation_jobs[0].status == "queued"
    assert fresh.curation_jobs[0].id == semantic["job_id"]


@pytest.mark.parametrize("status", ["unverified", "verifying", "incomplete", "failed"])
def test_unverified_graph_outcome_never_schedules(
    tmp_path: Path,
    status: str,
) -> None:
    state = _state(tmp_path / "vault")

    outcome = _curation.attach_verified_reconciliation_outcome(
        state,
        {"quality": "complete", "integrity": {"status": status}},
        trigger="verified_file_commit",
    )

    assert state.curation_jobs == []
    assert "cross_document_reconciliation" not in outcome


@pytest.mark.parametrize("quality", ["failed", "integrity_failed"])
def test_failed_quality_never_schedules_even_with_verified_generation(
    tmp_path: Path,
    quality: str,
) -> None:
    state = _state(tmp_path / "vault")

    outcome = _curation.attach_verified_reconciliation_outcome(
        state,
        {"quality": quality, "integrity": {"status": "verified"}},
        trigger="verified_file_commit",
    )

    assert state.curation_jobs == []
    assert "cross_document_reconciliation" not in outcome


def test_reconcile_propose_runtime_failure_is_linked_but_commit_stays_done(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = IngestItem(
        id="ingest-1",
        name="book.md",
        path="/book.md",
        status="done",
        outcome={"quality": "complete"},
    )
    state = _state(tmp_path / "vault")
    state.ingest_queue = [item]
    state.vault = SimpleNamespace(
        store=SimpleNamespace(_graph_handle=SimpleNamespace(graph_generation="generation-a")),
        embedder=object(),
    )
    state.writer_lock = asyncio.Lock()
    state.ingest_worker_active = False
    monkeypatch.setattr(
        _curation,
        "_build_judge",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    monkeypatch.setattr(
        _jobs.graph_integrity,
        "require_write_allowed",
        lambda _state, _vault: None,
    )
    _jobs.register_runner(
        "reconcile-propose",
        _curation.run_propose,
        writes=False,
        verified_snapshot=True,
    )

    async def run_job() -> _jobs.CurationJob:
        job = _jobs.submit(
            state,
            "reconcile-propose",
            params={
                "trigger": "verified_file_commit",
                "ingest_item_ids": [item.id],
                "graph_generation": "generation-a",
            },
        )
        item.outcome["cross_document_reconciliation"] = {
            "state": "scheduled",
            "job_id": job.id,
        }
        assert state.curation_worker_task is not None
        await state.curation_worker_task
        return job

    job = asyncio.run(run_job())

    assert job.status == "error"
    assert job.result is not None
    assert job.result["outcome"]["state"] == "failed"
    assert item.status == "done"
    assert item.outcome["quality"] == "complete"
    semantic = item.outcome["cross_document_reconciliation"]
    assert semantic["state"] == "failed"
    assert semantic["stage"] == "propose"
    assert semantic["job_id"] == job.id


def test_late_proposal_result_does_not_attach_to_retried_item(tmp_path: Path) -> None:
    item = IngestItem(
        id="ingest-1",
        name="book.md",
        path="/book.md",
        status="queued",
        outcome={},  # retry_item clears the old run's outcome before reprocessing
    )
    state = _state(tmp_path / "vault")
    state.ingest_queue = [item]

    _curation._publish_linked_reconcile_outcome(
        state,
        [item.id],
        {"state": "complete", "job_id": "old-job"},
        expected_job_id="old-job",
    )

    assert item.outcome == {}


def test_reconcile_propose_rejects_stale_graph_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path / "vault")
    state.writer_lock = asyncio.Lock()
    state.ingest_worker_active = False
    state.vault = SimpleNamespace(
        store=SimpleNamespace(_graph_handle=SimpleNamespace(graph_generation="generation-new")),
        embedder=object(),
    )
    item = IngestItem(
        id="ingest-1",
        name="book.md",
        path="/book.md",
        status="done",
    )
    state.ingest_queue = [item]
    monkeypatch.setattr(
        _jobs.graph_integrity,
        "require_write_allowed",
        lambda _state, _vault: None,
    )
    _jobs.register_runner(
        "reconcile-propose",
        _curation.run_propose,
        writes=False,
        verified_snapshot=True,
    )

    async def run_job() -> _jobs.CurationJob:
        job = _jobs.submit(
            state,
            "reconcile-propose",
            params={
                "trigger": "verified_file_commit",
                "ingest_item_ids": [item.id],
                "graph_generation": "generation-old",
            },
        )
        item.outcome = {
            "cross_document_reconciliation": {
                "state": "scheduled",
                "job_id": job.id,
            }
        }
        assert state.curation_worker_task is not None
        await state.curation_worker_task
        return job

    job = asyncio.run(run_job())

    assert job.status == "error"
    assert "generation changed" in (job.error or "")
    assert item.outcome["cross_document_reconciliation"]["state"] == "failed"


def test_reconcile_propose_is_serialized_and_integrity_fenced() -> None:
    _curation.register_runners()
    runner, writes = _jobs._REGISTRY["reconcile-propose"]
    assert runner is _curation.run_propose
    assert writes is False
    assert _jobs.requires_verified_snapshot("reconcile-propose") is True


def test_rehydrate_resets_running_to_queued(tmp_path: Path) -> None:
    state = _state(tmp_path / "vault")
    j = _jobs.new_job("reconcile-apply", label="apply")
    j.status = "running"
    j.started_at = 123.0
    j.progress = "reconciling"
    state.curation_jobs = [j]
    _jobs.persist(state)

    fresh = _state(tmp_path / "vault")
    _jobs.rehydrate_jobs(fresh)
    assert len(fresh.curation_jobs) == 1
    restored = fresh.curation_jobs[0]
    # A crash-interrupted job is re-queued (runners are idempotent off-graph upserts).
    assert restored.status == "queued"
    assert restored.started_at is None
    assert restored.progress == ""


@pytest.mark.parametrize("kind", ["rebuild", "rollback", "heal", "reembed"])
def test_rehydrate_fails_interrupted_rebuild_family_durably(
    tmp_path: Path,
    kind: str,
) -> None:
    state = _state(tmp_path / "vault")
    job = _jobs.new_job(kind)
    job.status = "running"
    job.started_at = 123.0
    job.progress = "materializing source 12/17"
    state.curation_jobs = [job]
    _jobs.persist(state)

    fresh = _state(tmp_path / "vault")
    _jobs.rehydrate_jobs(fresh)

    restored = fresh.curation_jobs[0]
    assert restored.status == "error"
    assert restored.started_at == 123.0
    assert restored.finished_at is not None
    assert restored.progress == "error"
    assert "submit a new job explicitly" in (restored.error or "")

    durable = json.loads(_jobs.jobs_path(fresh).read_text(encoding="utf-8"))
    assert durable["jobs"][0]["status"] == "error"
    assert durable["jobs"][0]["progress"] == "error"


def test_rehydrate_runs_domain_recovery_before_failing_interrupted_job(
    tmp_path: Path,
) -> None:
    recovered: list[tuple[Path, str]] = []

    def recover(state, job) -> str:
        recovered.append((Path(state.vault_path), job.id))
        return "domain recovery complete"

    _jobs.register_runner(
        "rebuild",
        lambda _state, _job: {},
        writes=True,
        interrupted_recovery=recover,
    )
    state = _state(tmp_path / "vault")
    job = _jobs.new_job("rebuild")
    job.status = "running"
    state.curation_jobs = [job]
    _jobs.persist(state)

    fresh = _state(tmp_path / "vault")
    _jobs.rehydrate_jobs(fresh)

    assert recovered == [(state.vault_path, job.id)]
    restored = fresh.curation_jobs[0]
    assert restored.status == "error"
    assert "domain recovery complete" in (restored.error or "")


def test_retention_caps_terminal_keeps_active(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_jobs, "RETENTION_CAP", 2)
    state = _state(tmp_path / "vault")

    def mk(i: str, status: str):
        job = _jobs.new_job("noop", label=i)
        job.id = i
        job.status = status
        return job

    state.curation_jobs = [
        mk("d1", "done"),
        mk("q1", "queued"),
        mk("e1", "error"),
        mk("r1", "running"),
        mk("d2", "done"),
        mk("d3", "done"),
    ]
    _jobs.persist(state)
    data = json.loads(_jobs.jobs_path(state).read_text(encoding="utf-8"))
    ids = [j["id"] for j in data["jobs"]]
    assert "q1" in ids and "r1" in ids  # active always kept
    terminal = [j["id"] for j in data["jobs"] if j["status"] in ("done", "error")]
    assert len(terminal) == 2  # capped
    assert "d1" not in ids  # oldest terminal dropped


def test_snapshot_summary_and_kind_filter(tmp_path: Path) -> None:
    state = _state(tmp_path / "vault")

    def mk(kind: str, status: str):
        job = _jobs.new_job(kind)
        job.status = status
        return job

    state.curation_jobs = [
        mk("reconcile-propose", "done"),
        mk("reconcile-apply", "queued"),
        mk("reconcile-apply", "running"),
    ]
    full = _jobs.snapshot(state)
    assert full["summary"]["total"] == 3
    assert full["summary"]["queued"] == 1
    assert full["summary"]["running"] == 1

    only_apply = _jobs.snapshot(state, kind="reconcile-apply")
    assert only_apply["summary"]["total"] == 2
    assert all(j["kind"] == "reconcile-apply" for j in only_apply["jobs"])


def test_drain_runs_runner_and_records_result(tmp_path: Path) -> None:
    calls: list[str] = []

    def runner(state, job):  # type: ignore[no-untyped-def]
        job.progress("midway")
        calls.append(job.params.get("tag", ""))
        return {"echo": job.params.get("tag")}

    _jobs.register_runner("noop", runner, writes=False)

    async def _run() -> _jobs.CurationJob:
        state = _state(tmp_path / "vault")
        state.writer_lock = asyncio.Lock()
        job = _jobs.submit(state, "noop", params={"tag": "A"})
        for _ in range(200):
            if job.status in ("done", "error"):
                break
            await asyncio.sleep(0.005)
        return job

    job = asyncio.run(_run())
    assert job.status == "done"
    assert job.result == {"echo": "A"}
    assert calls == ["A"]
    assert job.finished_at is not None


@pytest.mark.asyncio
async def test_verified_file_proposal_waits_for_ingest_batch_to_be_quiet(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    def runner(_state, _job):  # type: ignore[no-untyped-def]
        calls.append("ran")
        return {"ok": True}

    _jobs.register_runner(
        "reconcile-propose",
        runner,
        writes=False,
        verified_snapshot=True,
    )
    _jobs.register_runner(
        "maintenance",
        lambda _state, _job: calls.append("maintenance") or {"ok": True},
        writes=False,
    )
    state = _state(tmp_path / "vault")
    state.writer_lock = asyncio.Lock()
    state.ingest_worker_active = True
    job = _jobs.submit(
        state,
        "reconcile-propose",
        params={"trigger": "verified_file_commit"},
    )
    maintenance = _jobs.submit(state, "maintenance")
    assert state.curation_worker_task is not None
    await state.curation_worker_task

    assert job.status == "queued"
    assert job.progress == "waiting for ingest batch to finish"
    assert maintenance.status == "done"
    assert calls == ["maintenance"]

    state.ingest_worker_active = False
    _jobs.ensure_worker(state)
    assert state.curation_worker_task is not None
    await state.curation_worker_task
    assert job.status == "done"
    assert calls == ["maintenance", "ran"]


@pytest.mark.asyncio
async def test_worker_task_is_owned_until_runner_finishes(tmp_path: Path) -> None:
    import threading

    entered = threading.Event()
    release = threading.Event()

    def runner(state, job):  # type: ignore[no-untyped-def]
        entered.set()
        release.wait(timeout=2.0)
        return {"ok": True}

    _jobs.register_runner("owned", runner, writes=False)
    state = _state(tmp_path / "vault")
    state.writer_lock = asyncio.Lock()
    job = _jobs.submit(state, "owned")
    task = state.curation_worker_task

    for _ in range(200):
        if entered.is_set():
            break
        await asyncio.sleep(0.005)

    assert entered.is_set()
    assert task is not None
    assert not task.done()

    release.set()
    await task
    assert job.status == "done"
    assert state.curation_worker_task is None
    assert state.curation_worker_active is False


def test_drain_isolates_failing_job(tmp_path: Path) -> None:
    def boom(state, job):  # type: ignore[no-untyped-def]
        raise RuntimeError("kaboom")

    def ok(state, job):  # type: ignore[no-untyped-def]
        return {"ran": True}

    _jobs.register_runner("boom", boom, writes=False)
    _jobs.register_runner("ok", ok, writes=False)

    async def _run():
        state = _state(tmp_path / "vault")
        state.writer_lock = asyncio.Lock()
        bad = _jobs.submit(state, "boom")
        good = _jobs.submit(state, "ok")
        for _ in range(300):
            if good.status in ("done", "error") and bad.status in ("done", "error"):
                break
            await asyncio.sleep(0.005)
        return bad, good

    bad, good = asyncio.run(_run())
    assert bad.status == "error"
    assert "kaboom" in (bad.error or "")
    # One bad job never kills the queue — the next still runs.
    assert good.status == "done"


@pytest.mark.parametrize(
    ("writes", "verified_snapshot"),
    [(True, False), (False, True)],
    ids=["writer", "verified-read-snapshot"],
)
def test_serialized_job_holds_writer_lock(
    tmp_path: Path,
    writes: bool,
    verified_snapshot: bool,
) -> None:
    """Prove both writer and verified-read policies hold ``writer_lock``.

    The earlier version of this test asserted nothing about the lock: its runner
    returned instantly, so the critical section had no duration and the contender
    raced (it could acquire before OR after). Crucially, the single FIFO ``_drain``
    loop serializes jobs by itself, so a two-write-jobs test would pass even with
    the lock deleted. To pin the LOCK specifically we give the runner real duration
    via a ``threading.Event`` and, while it is parked inside the critical section,
    assert that ``writer_lock.acquire()`` TIMES OUT (i.e. is held). This test fails
    if the ``async with state.writer_lock`` around the serialized job is removed."""
    import threading

    timeline: list[str] = []
    runner_entered = threading.Event()
    release_runner = threading.Event()

    def blocking_writer(state, job):  # type: ignore[no-untyped-def]
        timeline.append("enter")
        runner_entered.set()  # signal the event loop the critical section is live
        # Park INSIDE the runner so the worker keeps holding writer_lock. Bounded
        # so a bug can't hang the suite — the test sets release_runner well before.
        release_runner.wait(timeout=5.0)
        timeline.append("exit")
        return {}

    _jobs.register_runner(
        "w",
        blocking_writer,
        writes=writes,
        verified_snapshot=verified_snapshot,
    )

    async def _run():
        state = _state(tmp_path / "vault")
        state.writer_lock = asyncio.Lock()

        job = _jobs.submit(state, "w")

        # Wait until the runner is actually executing (lock held by the worker).
        # Poll cooperatively so the to_thread runner gets scheduled.
        for _ in range(200):
            if runner_entered.is_set():
                break
            await asyncio.sleep(0.005)
        assert runner_entered.is_set(), "runner never entered its critical section"

        # The lock MUST be held right now: a concurrent acquire times out. 3.12's
        # wait_for cancels the timed-out acquire cleanly (no stolen lock).
        timeline.append("probe-start")
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(state.writer_lock.acquire(), 0.15)
        timeline.append("probe-blocked")

        # Let the runner finish → worker releases the lock → it becomes acquirable.
        release_runner.set()
        for _ in range(200):
            if job.status in ("done", "error"):
                break
            await asyncio.sleep(0.005)

        # Now the lock is free: a fresh acquire succeeds promptly.
        await asyncio.wait_for(state.writer_lock.acquire(), 1.0)
        state.writer_lock.release()
        timeline.append("acquired-after")
        return job

    job = asyncio.run(_run())
    assert job.status == "done", job.error
    # Ordering proof: the runner was inside its critical section (enter) and STILL
    # running (no exit yet) when the concurrent acquire was blocked, and only ran
    # to completion (exit) afterwards — i.e. the write held the lock throughout.
    assert timeline == [
        "enter",
        "probe-start",
        "probe-blocked",
        "exit",
        "acquired-after",
    ], timeline


@pytest.mark.asyncio
async def test_integrity_fence_allows_only_trust_root_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def runner(_state, job):  # type: ignore[no-untyped-def]
        calls.append(job.kind)
        return {"ran": job.kind}

    fenced = GraphIntegrityState(
        status=AuditStatus.FAILED,
        graph_generation="generation-a",
        writer_fenced=True,
        reason="adjacency mismatch",
    )

    def reject_write(_state, _vault):  # type: ignore[no-untyped-def]
        raise IntegrityFenceError(fenced)

    monkeypatch.setattr(_jobs.graph_integrity, "require_write_allowed", reject_write)
    for kind in ("companion-triage", "rebuild", "heal"):
        _jobs.register_runner(kind, runner, writes=True)
    _jobs.register_runner(
        "reconcile-propose",
        runner,
        writes=False,
        verified_snapshot=True,
    )

    state = _state(tmp_path / "vault")
    state.writer_lock = asyncio.Lock()
    state.vault = object()
    triage = _jobs.submit(state, "companion-triage")
    rebuild = _jobs.submit(state, "rebuild")
    heal = _jobs.submit(state, "heal")
    proposal = _jobs.submit(
        state,
        "reconcile-propose",
        params={
            "trigger": "verified_file_commit",
            "ingest_item_ids": ["ingest-1"],
        },
    )
    linked_item = IngestItem(
        id="ingest-1",
        name="book.md",
        path="/book.md",
        status="done",
        outcome={
            "quality": "complete",
            "cross_document_reconciliation": {
                "state": "scheduled",
                "job_id": proposal.id,
            },
        },
    )
    state.ingest_queue = [linked_item]

    assert state.curation_worker_task is not None
    await state.curation_worker_task

    assert triage.status == "error"
    assert "integrity_fenced" in (triage.error or "")
    assert rebuild.status == "done"
    assert heal.status == "error"
    assert "integrity_fenced" in (heal.error or "")
    assert proposal.status == "error"
    assert "integrity_fenced" in (proposal.error or "")
    assert linked_item.status == "done"
    assert linked_item.outcome["quality"] == "complete"
    assert linked_item.outcome["cross_document_reconciliation"]["state"] == "failed"
    assert calls == ["rebuild"]
