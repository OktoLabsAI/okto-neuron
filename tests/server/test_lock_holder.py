"""The writer lock names its holder, and a busy answer says who it is (field report: bare "busy")."""

from __future__ import annotations

import asyncio
import functools
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from okto_neuron.consolidate.ledger import CandidateLedger, LeaseBusyError, _exclusive_lock
from okto_neuron.server import http as http_mod
from okto_neuron.server._lock_holder import (
    LockHolder,
    busy_detail,
    clear_holder,
    current_holder,
    held_lock,
    record_holder,
)
from okto_neuron.server.http import _LockBusy, _writer_lock_fast, build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests
from okto_neuron.vault import Vault


def test_holder_is_recorded_while_held_and_cleared_on_release() -> None:
    async def scenario() -> None:
        lock = asyncio.Lock()
        assert current_holder(lock) is None
        async with held_lock(lock, "ingest-item", "item-7"):
            holder = current_holder(lock)
            assert holder is not None and (holder.kind, holder.ident) == ("ingest-item", "item-7")
            assert lock.locked()
        assert current_holder(lock) is None and not lock.locked()

    asyncio.run(scenario())


def test_holder_is_cleared_when_the_body_raises() -> None:
    async def scenario() -> None:
        lock = asyncio.Lock()
        with pytest.raises(RuntimeError):
            async with held_lock(lock, "curation-job", "job-1"):
                raise RuntimeError("boom")
        assert current_holder(lock) is None and not lock.locked()

    asyncio.run(scenario())


def test_ident_is_bounded_and_public_shape_has_no_other_fields() -> None:
    lock = asyncio.Lock()
    holder = record_holder(lock, "ingest-item", "x" * 500)
    assert len(holder.ident) == 80
    assert set(holder.to_public()) == {"kind", "id", "since", "held_for_s"}
    clear_holder(lock)
    assert current_holder(lock) is None


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("ingest-item", 15),
        ("mcp-remember", 15),
        ("curation-job", 30),
        ("rebuild-job", 30),
        ("vault-maintenance", 10),
        ("review-op", 5),
        ("something-new", 10),
    ],
)
def test_retry_after_depends_on_what_holds_the_lock(kind: str, expected: int) -> None:
    assert LockHolder(kind, "i", 0.0, 0.0).retry_after_s() == expected


def test_busy_detail_names_the_holder_and_falls_back_to_the_plain_sentence() -> None:
    assert busy_detail("vault is busy", None) == "vault is busy"
    text = busy_detail("vault is busy", LockHolder("ingest-item", "item-7", 0.0, 0.0))
    assert "ingest-item item-7" in text and "retry in about 15 s" in text


def test_the_fail_fast_wrapper_raises_busy_with_the_holder_of_the_lock() -> None:
    async def scenario() -> LockHolder | None:
        state = SimpleNamespace(writer_lock=asyncio.Lock(), vault=None)
        async with held_lock(state.writer_lock, "mcp-remember"):
            with pytest.raises(_LockBusy) as raised:
                async with _writer_lock_fast(state, timeout=0.05, verify_write_allowed=False):
                    pytest.fail("the lock is held; the wrapper must not enter")
        # the waiter did not disturb the holder it reported
        return raised.value.holder

    holder = asyncio.run(scenario())
    assert holder is not None and holder.kind == "mcp-remember"


def test_the_fail_fast_wrapper_records_and_clears_its_own_holder() -> None:
    async def scenario() -> None:
        state = SimpleNamespace(writer_lock=asyncio.Lock(), vault=None)
        async with _writer_lock_fast(state, timeout=0.05, verify_write_allowed=False):
            holder = current_holder(state.writer_lock)
            assert holder is not None and holder.kind == "review-op"
        assert current_holder(state.writer_lock) is None and not state.writer_lock.locked()

    asyncio.run(scenario())


def test_a_review_action_behind_an_ingest_item_gets_the_holder_and_retry_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        http_mod, "_writer_lock_fast", functools.partial(_writer_lock_fast, timeout=0.1)
    )

    async def scenario() -> httpx.Response:
        reset_state_for_tests()
        vault = Vault.init(tmp_path / "v", packs=["core"])
        state = init_state(vault, vault.path)
        transport = httpx.ASGITransport(app=build_rest_app(state))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            async with held_lock(state.writer_lock, "ingest-item", "item-7"):
                return await client.post(
                    "/api/v1/reconcile/review/confirm", json={"cluster_id": "cl:q1"}
                )

    try:
        response = asyncio.run(scenario())
    finally:
        reset_state_for_tests()
    assert response.status_code == 503, (
        response.text
    )  # status and code unchanged for existing clients
    body = response.json()
    assert body["error"] == "busy" and body["status"] == 503
    assert "ingest-item item-7" in body["detail"]
    assert body["holder"]["kind"] == "ingest-item" and body["holder"]["id"] == "item-7"
    assert body["holder"]["held_for_s"] >= 0
    assert body["retry_after_s"] == 15
    assert response.headers["Retry-After"] == "15"
    leaked = json.dumps(body)
    assert str(tmp_path) not in leaked and "/" not in body["holder"]["id"]


def test_a_busy_answer_without_a_known_holder_still_carries_retry_after() -> None:
    response = http_mod._lock_busy_response(_LockBusy(None), 503, "busy", "vault is busy")
    assert response.status_code == 503 and response.headers["Retry-After"] == "10"
    body = json.loads(response.body)
    assert "holder" not in body and body["detail"] == "vault is busy"


# ── review routes: batch fast-fail and the bounded semantic-lease wait ───────────────────────

_REVIEW_PATHS = ("/api/v1/resolve-review", "/api/v1/review-queue/batch")
_BODIES = {
    "/api/v1/resolve-review": {"candidate_id": "missing", "action": "discard"},
    "/api/v1/review-queue/batch": {"candidate_ids": ["missing"], "action": "discard"},
}


def _fast(monkeypatch: pytest.MonkeyPatch, timeout: float = 0.2) -> None:
    monkeypatch.setattr(
        http_mod, "_writer_lock_fast", functools.partial(_writer_lock_fast, timeout=timeout)
    )
    monkeypatch.setattr(http_mod, "_CURATION_LOCK_TIMEOUT_S", timeout)


def _post(tmp_path: Path, path: str, hold) -> tuple[httpx.Response, float]:  # type: ignore[no-untyped-def]
    """POST one review route while ``hold(state)`` (an async CM factory) is in effect."""

    async def scenario() -> tuple[httpx.Response, float]:
        reset_state_for_tests()
        vault = Vault.init(tmp_path / "v", packs=["core"])
        state = init_state(vault, vault.path)
        transport = httpx.ASGITransport(app=build_rest_app(state))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            async with hold(state):
                started = time.monotonic()
                response = await client.post(path, json=_BODIES[path])
                return response, time.monotonic() - started

    try:
        return asyncio.run(scenario())
    finally:
        reset_state_for_tests()


@pytest.mark.parametrize("path", _REVIEW_PATHS)
def test_both_review_routes_fail_fast_with_holder_and_retry_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _fast(monkeypatch)
    response, took = _post(
        tmp_path, path, lambda state: held_lock(state.writer_lock, "mcp-remember", "-")
    )
    assert took < 3, took  # the batch route used to wait without bound
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["error"] == "busy"
    assert body["holder"]["kind"] == "mcp-remember"
    assert body["retry_after_s"] == 15 and response.headers["Retry-After"] == "15"


@pytest.mark.parametrize("path", _REVIEW_PATHS)
def test_a_review_with_free_locks_still_works(tmp_path: Path, path: str) -> None:
    import contextlib

    @contextlib.asynccontextmanager
    async def nothing(state):  # type: ignore[no-untyped-def]
        yield

    response, _ = _post(tmp_path, path, nothing)
    if path.endswith("/batch"):
        assert response.status_code == 200, response.text
        assert response.json() == {"status": "ok", "resolved": 0, "skipped": 1, "errors": []}
    else:
        assert response.status_code == 404 and response.json()["error"] == "review_item_not_found"


_HOLD_FLOCK = (
    "import fcntl,sys,time\n"
    "f=open(sys.argv[1],'a+b'); fcntl.flock(f.fileno(), fcntl.LOCK_EX)\n"
    "print('held',flush=True); time.sleep(float(sys.argv[2]))\n"
)


def _hold_flock_in_other_process(path: Path, seconds: float) -> subprocess.Popen:  # type: ignore[type-arg]
    path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD_FLOCK, str(path), str(seconds)],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
    return proc


@pytest.mark.parametrize("path", _REVIEW_PATHS)
def test_a_flock_held_by_another_process_surfaces_as_busy_external_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    import contextlib

    _fast(monkeypatch)
    lock_path = tmp_path / "v" / ".marginalia" / ".semantic-writer.lock"
    proc = _hold_flock_in_other_process(lock_path, 30)
    try:

        @contextlib.asynccontextmanager
        async def nothing(state):  # type: ignore[no-untyped-def]
            yield

        response, took = _post(tmp_path, path, nothing)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    assert took < 3, took
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["holder"] == {"kind": "external-process", "id": "-"}
    assert body["retry_after_s"] == 15 and response.headers["Retry-After"] == "15"
    assert "external-process" in body["detail"]


def test_the_flock_is_released_after_a_timeout_and_usable_once_the_holder_exits(
    tmp_path: Path,
) -> None:
    ledger = CandidateLedger(tmp_path / ".marginalia")
    proc = _hold_flock_in_other_process(ledger.dir / ".semantic-writer.lock", 30)
    try:
        started = time.monotonic()
        with pytest.raises(LeaseBusyError) as err:
            with ledger.semantic_writer_lease(timeout=0.2):
                pytest.fail("must not be acquired")
        assert not err.value.in_process and time.monotonic() - started < 3
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    # nothing is left held by the timed-out attempt: the process-local lock is free and the file
    # lock can be taken again immediately
    with ledger.semantic_writer_lease(timeout=1):
        pass
    with ledger.semantic_writer_lease():
        pass


def test_a_lease_held_by_another_thread_of_this_process_times_out_as_in_process(
    tmp_path: Path,
) -> None:
    ledger = CandidateLedger(tmp_path / ".marginalia")
    entered, release = threading.Event(), threading.Event()

    def hold() -> None:
        with ledger.semantic_writer_lease():
            entered.set()
            release.wait(10)

    thread = threading.Thread(target=hold)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(LeaseBusyError) as err:
            with ledger.semantic_writer_lease(timeout=0.2):
                pytest.fail("must not be acquired")
        assert err.value.in_process
    finally:
        release.set()
        thread.join(10)
    with ledger.semantic_writer_lease(timeout=1):
        pass


def test_the_unbounded_lease_still_waits_for_its_holder(tmp_path: Path) -> None:
    """Non-review callers pass no timeout and keep the blocking behaviour."""
    ledger = CandidateLedger(tmp_path / ".marginalia")
    entered = threading.Event()
    order: list[str] = []

    def hold() -> None:
        with _exclusive_lock(ledger.dir / ".semantic-writer.lock"):
            entered.set()
            time.sleep(0.4)
            order.append("holder-done")

    ledger.dir.mkdir(parents=True, exist_ok=True)
    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(5)
    with ledger.semantic_writer_lease():
        order.append("waiter-in")
    thread.join(10)
    assert order == ["holder-done", "waiter-in"]


def test_one_process_holding_both_locks_does_not_deadlock_a_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A remember holds writer_lock then the lease; the review waits on writer_lock first, answers
    busy within the window, and the lease holder is untouched."""
    _fast(monkeypatch)
    ledger = CandidateLedger(tmp_path / "v" / ".marginalia")
    import contextlib

    @contextlib.asynccontextmanager
    async def both(state):  # type: ignore[no-untyped-def]
        async with held_lock(state.writer_lock, "mcp-remember", "-"):
            with ledger.semantic_writer_lease():
                yield

    response, took = _post(tmp_path, "/api/v1/resolve-review", both)
    assert took < 3 and response.status_code == 503
    assert response.json()["holder"]["kind"] == "mcp-remember"
