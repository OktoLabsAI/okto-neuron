"""Task 4: silent failures must be visible on /api/v1/status.

Three silent-failure paths used to report ``status: ok`` — a kill-9 boot of an
empty graph, a folder-watch task that died on an unguarded exception (auto-ingest
stops forever), and an ingest queue full of errors. These tests pin the derived
/api/v1/status fields + the ``degraded`` flip, the request_id binding (it was ``null``
everywhere in the live log), and the folder-watch crash-restart supervisor.

Model-free: a stubbed in-memory ServerState, no real Ladybug vault, no LLM.
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from okto_neuron.server import _folder_watch as fw
from okto_neuron.server import runtime as runtime_mod
from okto_neuron.server._ingest_queue import IngestItem
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.lifecycle import JsonLogFormatter
from okto_neuron.server.state import init_state, reset_state_for_tests


class _StubVault:
    """Minimal vault double. ``recovered_from_corruption`` mirrors the real Vault
    property so /api/v1/status can read the corruption-recovery signal without Ladybug."""

    def __init__(self, *, recovered: bool = False) -> None:
        self.recovered_from_corruption = recovered
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def make_client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    created: list = []
    # The folder-watch status snapshot is a module global; clear it so a prior
    # test's populated snapshot never leaks a stale poll age into these tests.
    fw._WATCH_STATUS.clear()

    def _factory(*, recovered: bool = False):
        reset_state_for_tests()
        state = init_state(_StubVault(recovered=recovered), tmp_path)
        app = build_rest_app(state)
        client = TestClient(app, base_url="http://127.0.0.1")
        client.__enter__()
        created.append(client)
        return client, state

    yield _factory
    for client in created:
        client.__exit__(None, None, None)
    reset_state_for_tests()


# ── health degraded triggers ────────────────────────────────────────────────


def test_health_ok_exposes_new_fields(make_client):
    c, state = make_client()
    body = c.get("/api/v1/status").json()
    assert body["status"] == "ok"
    # All four derived fields are present on the healthy path.
    assert body["recovered_from_corruption"] is False
    assert body["seconds_since_last_ingest"] is None
    assert body["folder_watch_last_poll_age"] is None
    assert body["queue_error_count"] == 0
    assert body["folder_watch_running"] is True
    assert "degraded_reasons" not in body


def test_health_degraded_on_corruption_recovery(make_client):
    c, state = make_client(recovered=True)
    body = c.get("/api/v1/status").json()
    assert body["status"] == "degraded"
    assert body["recovered_from_corruption"] is True
    assert any("recovered_from_corruption" in r for r in body["degraded_reasons"])


def test_health_degraded_on_queue_errors(make_client):
    c, state = make_client()
    state.ingest_queue.append(
        IngestItem(id="1-abc", name="a.md", path="/tmp/a.md", status="error", error="boom")
    )
    # A "done" item with a provider_error (partial yield) also counts.
    state.ingest_queue.append(
        IngestItem(id="2-def", name="b.md", path="/tmp/b.md", status="done", provider_error="429")
    )
    body = c.get("/api/v1/status").json()
    assert body["status"] == "degraded"
    assert body["queue_error_count"] == 2
    assert any("queue_errors" in r for r in body["degraded_reasons"])


def test_health_not_degraded_by_empty_outcome_items(make_client):
    """2026-09-14 fix: a document whose extraction legitimately found nothing
    (every unit ``empty_after_retry``, zero provider failures) lands at
    ``status=done`` with ``outcome.quality=empty`` and no ``provider_error`` —
    it must not inflate ``queue_error_count`` or degrade health, unlike a
    genuine ``error``/provider-degraded item."""
    c, state = make_client()
    state.ingest_queue.append(
        IngestItem(
            id="1-empty",
            name="checksums.md",
            path="/tmp/checksums.md",
            status="done",
            outcome={"quality": "empty", "units": {"empty_after_retry": 1}},
        )
    )
    body = c.get("/api/v1/status").json()
    assert body["status"] == "ok"
    assert body["queue_error_count"] == 0
    assert "degraded_reasons" not in body


def test_health_seconds_since_last_ingest_is_surfaced_not_tripped(make_client):
    """An idle vault has a large seconds_since_last_ingest — that alone must NOT
    degrade (else every quiet daemon false-trips)."""
    c, state = make_client()
    state.last_ingest_at = 1.0  # ancient epoch → huge age
    body = c.get("/api/v1/status").json()
    assert body["status"] == "ok"
    assert body["seconds_since_last_ingest"] > 1_000_000


def test_health_degraded_when_folder_watch_task_dead(make_client):
    """The DoD scenario: kill the watch task → /api/v1/status flips to degraded with a
    populated reason and ``folder_watch_running: false``."""
    c, state = make_client()

    async def _make_dead_task() -> asyncio.Task:
        task = asyncio.ensure_future(asyncio.sleep(3600))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return task

    state.folder_watch_task = asyncio.run(_make_dead_task())
    body = c.get("/api/v1/status").json()
    assert body["status"] == "degraded"
    assert body["folder_watch_running"] is False
    assert any("folder_watch_stopped" in r for r in body["degraded_reasons"])


def test_health_degraded_when_folder_watch_stalled(make_client):
    """A live task that hasn't polled any configured root past the stall threshold
    degrades even though the task object is still running."""
    c, state = make_client()
    # No folder_watch_task (None → treated as running); inject an ancient poll ts.
    fw._WATCH_STATUS[str(state.vault_path)] = {"enabled": True, "last_poll_ts": 1.0}
    body = c.get("/api/v1/status").json()
    assert body["status"] == "degraded"
    assert body["folder_watch_last_poll_age"] > _stall_threshold()
    assert any("folder_watch_stalled" in r for r in body["degraded_reasons"])


def _stall_threshold() -> float:
    from okto_neuron.server.http import _FOLDER_WATCH_STALL_S

    return _FOLDER_WATCH_STALL_S


# ── health-degraded log dedup (transition-only, not per-poll) ──────────────


def test_health_degraded_second_identical_poll_does_not_relog(make_client, caplog):
    """Live incident: hundreds of identical ERROR lines hours after a recovery —
    a degraded daemon logged on every single poll. Only the transition (first
    degraded poll, or a reason-set change) should log."""
    c, state = make_client(recovered=True)
    with caplog.at_level(logging.ERROR, logger="okto_neuron.server.http"):
        c.get("/api/v1/status")
        first_count = sum(1 for r in caplog.records if r.message.startswith("health degraded"))
        c.get("/api/v1/status")
        second_count = sum(1 for r in caplog.records if r.message.startswith("health degraded"))
    assert first_count == 1
    assert second_count == 1  # unchanged — no new log on the identical second poll


def test_health_degraded_reason_value_mutation_same_key_does_not_relog(
    make_client, caplog, monkeypatch
):
    """Regression: folder_watch_stalled's rendered message embeds the LIVE
    stall age (``f"folder_watch_stalled: no poll for {age:.0f}s..."``), which
    changes on every single poll while the watcher stays stuck. Comparing the
    FULL rendered reason string (rather than a stable key) would make every
    poll look like a "transition" and defeat the log-once fix exactly when it
    matters most: a daemon stuck degraded for hours."""
    c, state = make_client()
    fw._WATCH_STATUS[str(state.vault_path)] = {"enabled": True, "last_poll_ts": 0.0}

    from okto_neuron.server import http as http_mod

    real_time = http_mod.time

    class _FakeTime:
        """Proxies everything except time() to the real module — only health()'s
        ``now = time.time()`` call needs to be controlled here; other request
        machinery (e.g. duration tracking elsewhere in the process) must see
        real wall-clock values."""

        def __init__(self, values: list[float]) -> None:
            self._values = iter(values)

        def time(self) -> float:
            return next(self._values)

        def __getattr__(self, name: str):
            return getattr(real_time, name)

    # Rebind the module-level ``time`` NAME inside http.py only (not the real
    # ``time`` module globally), so other modules' clocks stay untouched.
    monkeypatch.setattr(http_mod, "time", _FakeTime([1_000.0, 2_000.0, 3_000.0]))

    with caplog.at_level(logging.ERROR, logger="okto_neuron.server.http"):
        first = c.get("/api/v1/status").json()
        second = c.get("/api/v1/status").json()
        third = c.get("/api/v1/status").json()

    assert first["status"] == "degraded"
    assert second["status"] == "degraded"
    assert third["status"] == "degraded"
    # Prove this actually exercises the mutable-value case: the rendered
    # reason strings genuinely differ poll-to-poll (the embedded stall age).
    assert first["degraded_reasons"] != second["degraded_reasons"]
    assert second["degraded_reasons"] != third["degraded_reasons"]
    assert all("folder_watch_stalled" in r for r in first["degraded_reasons"])

    degraded_logs = [r for r in caplog.records if r.message.startswith("health degraded")]
    assert len(degraded_logs) == 1


def test_health_degraded_reason_change_relogs(make_client, caplog):
    """A different reason set (not just a repeat) is a real transition and must
    log again."""
    c, state = make_client(recovered=True)
    with caplog.at_level(logging.ERROR, logger="okto_neuron.server.http"):
        c.get("/api/v1/status")
        state.ingest_queue.append(
            IngestItem(id="1-abc", name="a.md", path="/tmp/a.md", status="error", error="boom")
        )
        c.get("/api/v1/status")
    degraded_logs = [r for r in caplog.records if r.message.startswith("health degraded")]
    assert len(degraded_logs) == 2


def test_health_recovery_logs_once(make_client, caplog):
    """When degraded reasons clear, emit exactly one INFO 'health recovered' line —
    not one per subsequent healthy poll."""
    c, state = make_client()
    state.ingest_queue.append(
        IngestItem(id="1-abc", name="a.md", path="/tmp/a.md", status="error", error="boom")
    )
    with caplog.at_level(logging.INFO, logger="okto_neuron.server.http"):
        body = c.get("/api/v1/status").json()
        assert body["status"] == "degraded"
        state.ingest_queue.clear()
        body = c.get("/api/v1/status").json()
        assert body["status"] == "ok"
        body = c.get("/api/v1/status").json()
        assert body["status"] == "ok"
    recovered_logs = [r for r in caplog.records if r.message == "health recovered"]
    assert len(recovered_logs) == 1


# ── request_id binding ──────────────────────────────────────────────────────


def test_request_id_response_header_present(make_client):
    c, _ = make_client()
    r = c.get("/api/v1/status")
    assert r.headers.get("x-request-id")


def test_request_id_honors_inbound_header(make_client):
    c, _ = make_client()
    r = c.get("/api/v1/status", headers={"X-Request-ID": "trace-42"})
    assert r.headers["x-request-id"] == "trace-42"


def test_request_id_bound_in_log_line(make_client):
    """The degraded-health ERROR log line must carry a non-null request_id (it was
    null everywhere in the live log). Proves the contextvar crosses the middleware
    task hop into the endpoint's logging."""
    c, _ = make_client(recovered=True)

    records: list[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(JsonLogFormatter().format(record))

    handler = _CaptureHandler()
    logger = logging.getLogger("okto_neuron.server.http")
    logger.addHandler(handler)
    try:
        c.get("/api/v1/status", headers={"X-Request-ID": "trace-log-7"})
    finally:
        logger.removeHandler(handler)

    degraded = [json.loads(line) for line in records if "health degraded" in line]
    assert degraded, "expected a 'health degraded' log line"
    assert degraded[-1]["request_id"] == "trace-log-7"


# ── folder-watch crash-restart supervisor (runtime) ─────────────────────────


class _FakeTask:
    """Stand-in for an asyncio.Task in a settled state for supervisor unit tests."""

    def __init__(self, *, cancelled: bool = False, exc: BaseException | None = None) -> None:
        self._cancelled = cancelled
        self._exc = exc

    def cancelled(self) -> bool:
        return self._cancelled

    def exception(self) -> BaseException | None:
        if self._cancelled:
            raise asyncio.CancelledError
        return self._exc


def _supervisor_state() -> SimpleNamespace:
    return SimpleNamespace(draining=False, folder_watch_restart_count=0, folder_watch_task=None)


def test_supervisor_leaves_cancelled_task_dead():
    """A deliberate external cancel is NOT a crash → do not resurrect (so /api/v1/status
    keeps reporting degraded)."""
    state = _supervisor_state()
    runtime_mod._supervise_folder_watch(state, _FakeTask(cancelled=True))
    assert state.folder_watch_restart_count == 0
    assert state.folder_watch_task is None


def test_supervisor_ignores_draining_shutdown():
    state = _supervisor_state()
    state.draining = True
    runtime_mod._supervise_folder_watch(state, _FakeTask(cancelled=True))
    assert state.folder_watch_restart_count == 0


def test_supervisor_leaves_clean_exit_stopped():
    """The loop is infinite, so a clean return (no exception) while not draining is
    unexpected: leave it stopped and visible on /api/v1/status, do not restart."""
    state = _supervisor_state()
    runtime_mod._supervise_folder_watch(state, _FakeTask(exc=None))
    assert state.folder_watch_restart_count == 0
    assert state.folder_watch_task is None


def test_supervisor_restarts_on_crash(monkeypatch):
    """An exception that escaped the loop guard is a genuine crash → restart once,
    bump the counter, and install a fresh live task."""
    import okto_neuron.server._folder_watch as fw

    async def _immortal(_state) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(fw, "run_folder_watch", _immortal)

    async def _drive() -> None:
        state = _supervisor_state()
        runtime_mod._supervise_folder_watch(state, _FakeTask(exc=RuntimeError("boom")))
        assert state.folder_watch_restart_count == 1
        assert state.folder_watch_task is not None
        assert not state.folder_watch_task.done()
        state.folder_watch_task.cancel()
        try:
            await state.folder_watch_task
        except asyncio.CancelledError:
            pass

    asyncio.run(_drive())


def test_supervisor_honors_restart_cap(monkeypatch):
    """Once the restart cap is hit, a re-crash is left dead (surfaces as degraded)
    instead of hot-looping ensure_future."""
    import okto_neuron.server._folder_watch as fw

    monkeypatch.setattr(fw, "run_folder_watch", lambda _s: None)  # never called

    state = _supervisor_state()
    state.folder_watch_restart_count = runtime_mod.MAX_FOLDER_WATCH_RESTARTS
    runtime_mod._supervise_folder_watch(state, _FakeTask(exc=RuntimeError("boom")))
    # No restart: counter unchanged and no new task installed.
    assert state.folder_watch_restart_count == runtime_mod.MAX_FOLDER_WATCH_RESTARTS
    assert state.folder_watch_task is None
