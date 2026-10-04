"""A stuck curation job must fail in bounded time and free the writer lock (#24).

The real ``_jobs._drain`` and ``_ingest_queue._drain`` run against a real
``asyncio.Lock`` writer lock; only the model server is a stub. The stub accepts
TCP connections and never answers, which is exactly how a wedged local LLM
server behaves. Time-valued config is shrunk so the tests take seconds.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_neuron.server import _ingest_queue as iq
from okto_neuron.server import _jobs
from okto_neuron.server._ingest_queue import IngestItem


class _SilentServer:
    """Accepts connections, reads nothing back, never answers."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self.accepted = 0
        self._held: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.1)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            self.accepted += 1
            self._held.append(conn)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        for conn in self._held:
            with contextlib.suppress(OSError):
                conn.close()
        self._sock.close()


@pytest.fixture
def silent_server():
    server = _SilentServer()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture(autouse=True)
def _clean_registry():
    saved = dict(_jobs._REGISTRY)
    saved_snapshots = set(_jobs._VERIFIED_SNAPSHOT_KINDS)
    _jobs._REGISTRY.clear()
    _jobs._VERIFIED_SNAPSHOT_KINDS.clear()
    yield
    _jobs._REGISTRY.clear()
    _jobs._REGISTRY.update(saved)
    _jobs._VERIFIED_SNAPSHOT_KINDS.clear()
    _jobs._VERIFIED_SNAPSHOT_KINDS.update(saved_snapshots)


def _state(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        vault_path=root,
        curation_jobs=[],
        curation_worker_active=False,
        curation_worker_task=None,
        ingest_queue=[],
        ingest_worker_active=False,
        ingest_worker_task=None,
        ingest_cancel_requested=False,
        last_ingest_at=None,
        last_ingest_at_by_vault={},
        draining=False,
        shutting_down=False,
        writer_lock=asyncio.Lock(),
    )


class _Companion:
    def __init__(self, remembered: list[str]) -> None:
        self._remembered = remembered

    def remember(self, path, *, on_progress=None, on_event=None, should_cancel=None):  # type: ignore[no-untyped-def]
        self._remembered.append(path)
        return SimpleNamespace(
            committed=1,
            queued=0,
            blocks_total=1,
            nodes_extracted=0,
            edges_extracted=0,
            claims_minted=0,
            provider_error=None,
            outcome={"quality": "complete"},
            outcomes=[],
        )


async def _until(predicate, timeout_s: float) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_unanswering_model_server_fails_the_job_and_frees_the_writer_lock(
    tmp_path: Path, silent_server: _SilentServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the real drains: a judge call against a server that
    never answers is killed at the scoped deadline, the job ends ``error``, the
    snapshot job's writer lock is released, and a queued ingest then proceeds."""
    from okto_neuron.config._vault import ResolvedLLM
    from okto_neuron.llm import LiteLLMProvider, Message, _scoped_call_timeout

    monkeypatch.setattr(_jobs, "_job_stall_timeout", lambda state: 120.0)
    resolved = ResolvedLLM(
        provider="openai",
        api_base=f"http://127.0.0.1:{silent_server.port}/v1",
        model="stub-model",
        api_key_env=None,
    )

    def run_judge(state, job):  # type: ignore[no-untyped-def]
        job.progress("judging")
        # What run_propose does for every cluster (curation_call_timeout_s, shrunk).
        with _scoped_call_timeout(2.0):
            LiteLLMProvider(resolved).complete([Message("user", "same entity?")])
        return {"never": "reached"}

    _jobs.register_runner("reconcile-propose", run_judge, writes=False, verified_snapshot=True)
    state = _state(tmp_path / "vault")
    remembered: list[str] = []

    started = time.monotonic()
    job = _jobs.submit(state, "reconcile-propose")
    await _until(lambda: state.writer_lock.locked(), 15)  # the snapshot job holds it
    state.ingest_queue.append(IngestItem(id="q", name="q.md", path=str(tmp_path / "q.md")))
    iq.ensure_worker(state, lambda _s: _Companion(remembered))

    await _until(lambda: job.status in {"error", "done"}, 60)
    elapsed = time.monotonic() - started
    assert job.status == "error", job.result
    assert elapsed < 45, f"job took {elapsed:.0f}s; the deadline should bound it to seconds"
    assert silent_server.accepted >= 1, "the stub server was never actually contacted"
    assert "reached" not in str(job.result)

    await _until(lambda: state.ingest_queue[0].status == "done", 30)
    assert remembered == [str(tmp_path / "q.md")], "the queued ingest did not run"
    assert not state.writer_lock.locked()
    # Sanity on the error itself: a timeout, not some unrelated failure.
    assert "timeout" in (job.error or "").lower() or "timed out" in (job.error or "").lower()


@pytest.mark.asyncio
async def test_watchdog_abandons_a_stalled_read_only_job_and_releases_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_jobs, "_job_stall_timeout", lambda state: 0.3)
    release = threading.Event()

    def stuck(state, job):  # type: ignore[no-untyped-def]
        job.progress("about to hang")
        release.wait(timeout=30)
        return {"late": True}

    _jobs.register_runner("reconcile-propose", stuck, writes=False, verified_snapshot=True)
    state = _state(tmp_path / "vault")
    remembered: list[str] = []
    try:
        job = _jobs.submit(state, "reconcile-propose")
        await _until(lambda: state.writer_lock.locked(), 10)
        state.ingest_queue.append(IngestItem(id="q", name="q.md", path=str(tmp_path / "q.md")))
        iq.ensure_worker(state, lambda _s: _Companion(remembered))

        await _until(lambda: job.status == "error", 15)
        assert "no progress" in (job.error or "")
        assert job.elapsed_s is not None and job.elapsed_s < 15
        public = job.to_public()
        assert public["elapsed_s"] is not None and public["last_progress_at"] is not None
        await _until(lambda: state.ingest_queue[0].status == "done", 15)
        assert remembered, "the queued ingest did not proceed after the watchdog fired"
        assert not release.is_set(), "the runner thread was still blocked when the lock was freed"
    finally:
        release.set()


@pytest.mark.asyncio
async def test_watchdog_never_abandons_a_writing_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thread still writing must not lose its lock to another writer."""
    monkeypatch.setattr(_jobs, "_job_stall_timeout", lambda state: 0.2)
    release = threading.Event()

    def slow_writer(state, job):  # type: ignore[no-untyped-def]
        job.progress("writing")
        release.wait(timeout=30)
        return {"ok": True}

    _jobs.register_runner("reconcile-apply", slow_writer, writes=True)
    state = _state(tmp_path / "vault")
    try:
        job = _jobs.submit(state, "reconcile-apply")
        await _until(lambda: job.status == "running", 10)
        await _until(lambda: job.stalled_for_s() is not None, 10)
        assert [j.id for j in _jobs.stalled_jobs(state)] == [job.id]
        await asyncio.sleep(0.6)
        assert job.status == "running"
        assert state.writer_lock.locked()
    finally:
        release.set()
    await _until(lambda: job.status == "done", 10)


def test_stalled_reason_reaches_the_status_payload(tmp_path: Path) -> None:
    from okto_neuron.server import http as http_mod

    job = _jobs.CurationJob(id="j", kind="reconcile-propose", status="running")
    job.started_at = time.time() - 100
    job.last_progress_at = time.time() - 100
    job.stall_after_s = 10.0
    assert job.stalled_for_s() is not None
    job.last_progress_at = time.time()
    assert job.stalled_for_s() is None
    source = Path(http_mod.__file__).read_text(encoding="utf-8")
    assert "curation_job_stalled" in source


def test_timeout_defaults_and_backoff() -> None:
    from okto_neuron.config._vault import (
        DEFAULT_CURATION_CALL_TIMEOUT_S,
        DEFAULT_JOB_STALL_TIMEOUT_S,
        DEFAULT_LLM_REQUEST_TIMEOUT_S,
        ConsolidationConfig,
        CurationSchedulerConfig,
    )
    from okto_neuron.llm import LLMProviderError, provider_retry_delay

    assert DEFAULT_LLM_REQUEST_TIMEOUT_S == 300.0
    assert ConsolidationConfig().curation_call_timeout_s == DEFAULT_CURATION_CALL_TIMEOUT_S == 600.0
    assert CurationSchedulerConfig().job_stall_timeout_s == DEFAULT_JOB_STALL_TIMEOUT_S
    # The watchdog is only a backstop: the call deadline must fire first.
    assert DEFAULT_JOB_STALL_TIMEOUT_S > DEFAULT_CURATION_CALL_TIMEOUT_S

    timeout = LLMProviderError("t", category="timeout", retryable=True)
    assert provider_retry_delay(timeout, 1) == 2.0
    assert provider_retry_delay(timeout, 2) is None  # bounded: two attempts
    unavailable = LLMProviderError("u", category="unavailable", retryable=True)
    assert provider_retry_delay(unavailable, 1) == 0.0
    waiting = LLMProviderError("r", category="timeout", retryable=True, retry_after_s=7.0)
    assert provider_retry_delay(waiting, 1) == 7.0
