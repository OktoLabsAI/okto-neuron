"""Review actions taken while the vault is busy are queued durably and applied later (field report).

Approving or rejecting a candidate used to fail with 503 when an ingest item or an MCP remember
held the writer lock for minutes. The review routes now answer 202, store the action in the
review queue's SQLite file, and one applier per vault applies the actions in arrival order once
the locks are free, superseding any action whose candidate changed or left the queue meanwhile.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
import pytest

from okto_neuron.consolidate import NodeCandidate
from okto_neuron.consolidate.review_queue import ReviewQueue
from okto_neuron.server import _review_actions
from okto_neuron.server import http as http_mod
from okto_neuron.server import runtime as server_runtime
from okto_neuron.server._lock_holder import held_lock
from okto_neuron.server._review_actions import (
    REASON_CHANGED,
    REASON_GONE,
    ActionNotCancellable,
    ReviewActionStore,
)
from okto_neuron.server.http import _writer_lock_fast, build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests
from okto_neuron.vault import Vault

RESOLVE = "/api/v1/resolve-review"
BATCH = "/api/v1/review-queue/batch"
ACTIONS = "/api/v1/review-actions"


def _candidate(i: int) -> NodeCandidate:
    return NodeCandidate(type="Concept", title=f"item {i}", facets={"block_id": f"b{i}"})


def _queue(vault: Vault) -> ReviewQueue:
    return ReviewQueue(Path(vault.path) / ".marginalia", vault.store)


def _seed(vault: Vault, n: int) -> list[str]:
    queue = _queue(vault)
    return [queue.enqueue(_candidate(i), "low_confidence").candidate_id for i in range(n)]


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        http_mod, "_writer_lock_fast", functools.partial(_writer_lock_fast, timeout=0.1)
    )
    monkeypatch.setattr(http_mod, "_CURATION_LOCK_TIMEOUT_S", 0.1)
    monkeypatch.setattr(_review_actions, "LEASE_TIMEOUT_S", 0.1)
    monkeypatch.setattr(_review_actions, "RETRY_PAUSE_S", 0.05)
    monkeypatch.setattr(_review_actions, "_LOCK_POLL_S", 0.05)


Scenario = Callable[[httpx.AsyncClient, Vault, object, list[str]], Awaitable[object]]


def _run(tmp_path: Path, n: int, body: Scenario) -> object:
    async def scenario() -> object:
        reset_state_for_tests()
        vault = Vault.init(tmp_path / "v", packs=["core"])
        ids = _seed(vault, n)
        state = init_state(vault, vault.path)
        transport = httpx.ASGITransport(app=build_rest_app(state))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            return await body(client, vault, state, ids)

    try:
        return asyncio.run(scenario())
    finally:
        reset_state_for_tests()


async def _settled(client: httpx.AsyncClient, timeout: float = 10.0) -> list[dict]:
    """Poll the actions endpoint until nothing is queued; the rows, oldest first."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        body = (await client.get(ACTIONS)).json()
        if body["queued"] == 0:
            return list(reversed(body["items"]))
        assert asyncio.get_running_loop().time() < deadline, body
        await asyncio.sleep(0.05)


@contextlib.asynccontextmanager
async def _busy(state: object) -> AsyncIterator[None]:
    async with held_lock(state.writer_lock, "mcp-remember", "-"):  # type: ignore[attr-defined]
        yield


# ── the store ────────────────────────────────────────────────────────────────


def test_store_keeps_arrival_order_and_terminal_states(tmp_path: Path) -> None:
    store = ReviewActionStore(tmp_path)
    assert store.next_queued() is None and store.recent() == []
    first = store.enqueue("c1", "commit", "sha1")
    second = store.enqueue("c2", "discard", "sha2", {"batch_id": "rb_1"})
    assert store.next_queued().id == first.id  # type: ignore[union-attr]
    assert store.queued_count() == 2 and store.queued_count(second.seq) == 1
    assert store.claim(first.id)
    store.finish(first.id, "applied", outcome={"ok": True})
    assert store.next_queued().id == second.id  # type: ignore[union-attr]
    row = store.get(first.id)
    assert row is not None and row.status == "applied" and row.finished_at
    assert row.outcome == {"ok": True}
    # a terminal row is never rewritten
    store.finish(first.id, "failed", "late")
    assert store.get(first.id).status == "applied"  # type: ignore[union-attr]
    assert [r.id for r in store.recent()] == [second.id, first.id]
    assert [r.id for r in store.recent(statuses=("queued",))] == [second.id]
    assert store.get(second.id).to_public()["batch_id"] == "rb_1"  # type: ignore[union-attr]


def test_cancel_wins_only_on_an_unclaimed_queued_action(tmp_path: Path) -> None:
    store = ReviewActionStore(tmp_path)
    a = store.enqueue("c1", "commit", "sha1")
    b = store.enqueue("c2", "commit", "sha2")
    cancelled = store.cancel(a.id)
    assert cancelled is not None and cancelled.status == "cancelled"
    assert not store.claim(a.id)  # the applier loses against the cancel
    assert store.claim(b.id)
    with pytest.raises(ActionNotCancellable):  # being applied
        store.cancel(b.id)
    store.finish(b.id, "applied")
    with pytest.raises(ActionNotCancellable):
        store.cancel(b.id)
    assert store.cancel("ra_unknown") is None


def test_reads_never_create_the_table(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v", packs=["core"])
    try:
        _seed(vault, 1)
        store = ReviewActionStore(Path(vault.path) / ".marginalia")
        assert store.queued_count() == 0 and store.recent() == []
        with sqlite3.connect(store.path) as connection:
            tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        assert "review_actions" not in tables and "entries" in tables
    finally:
        vault.close()


# ── the routes and the applier ───────────────────────────────────────────────


def test_a_busy_approve_is_queued_and_applied_when_the_lock_frees(tmp_path: Path) -> None:
    async def body(client, vault, state, ids):  # type: ignore[no-untyped-def]
        async with _busy(state):
            response = await client.post(RESOLVE, json={"candidate_id": ids[0], "action": "commit"})
            assert response.status_code == 202, response.text
            queued = response.json()
            listing = (await client.get(ACTIONS)).json()
            assert listing["queued"] == 1 and listing["items"][0]["status"] == "queued"
            assert _queue(vault).fingerprint(ids[0]) is not None  # not applied while busy
        return queued, await _settled(client), _queue(vault).fingerprint(ids[0])

    queued, rows, after = _run(tmp_path, 1, body)  # type: ignore[misc]
    assert queued["status"] == "queued" and queued["queued_ahead"] == 0
    assert queued["holder"]["kind"] == "mcp-remember" and queued["retry_after_s"] == 15
    assert queued["action"]["candidate_id"] and queued["action"]["status"] == "queued"
    assert [r["status"] for r in rows] == ["applied"], rows
    assert rows[0]["outcome"] is not None
    assert after is None  # the item left the review queue


def test_actions_apply_in_arrival_order_and_a_later_one_on_the_same_item_is_superseded(
    tmp_path: Path,
) -> None:
    async def body(client, vault, state, ids):  # type: ignore[no-untyped-def]
        async with _busy(state):
            for cid, action in ((ids[0], "discard"), (ids[1], "commit"), (ids[0], "commit")):
                response = await client.post(RESOLVE, json={"candidate_id": cid, "action": action})
                assert response.status_code == 202, response.text
        return await _settled(client), [_queue(vault).fingerprint(i) for i in ids]

    rows, after = _run(tmp_path, 2, body)  # type: ignore[misc]
    assert [(r["action"], r["status"]) for r in rows] == [
        ("discard", "applied"),
        ("commit", "applied"),
        ("commit", "superseded"),
    ], rows
    assert rows[2]["reason"] == REASON_GONE
    assert after == [None, None]


def test_a_direct_action_queues_behind_waiting_ones(tmp_path: Path) -> None:
    """Arrival order holds even when the lock happens to be free for a newer action."""

    async def body(client, vault, state, ids):  # type: ignore[no-untyped-def]
        store = _review_actions.store_for(state)
        # an action left queued by a previous process, its applier not started yet
        store.enqueue(ids[0], "discard", _queue(vault).fingerprint(ids[0]))
        response = await client.post(RESOLVE, json={"candidate_id": ids[0], "action": "commit"})
        assert response.status_code == 202, response.text
        return await _settled(client)

    rows = _run(tmp_path, 1, body)
    assert [(r["action"], r["status"]) for r in rows] == [  # type: ignore[union-attr]
        ("discard", "applied"),
        ("commit", "superseded"),
    ]


def test_an_item_resolved_elsewhere_before_the_apply_is_superseded(tmp_path: Path) -> None:
    async def body(client, vault, state, ids):  # type: ignore[no-untyped-def]
        async with _busy(state):
            response = await client.post(RESOLVE, json={"candidate_id": ids[0], "action": "commit"})
            assert response.status_code == 202
            # another path (a job) resolves the item while the action waits
            _queue(vault).acknowledge(ids[0])
        return await _settled(client), vault.store.get_node(ids[0])

    rows, node = _run(tmp_path, 1, body)  # type: ignore[misc]
    assert [r["status"] for r in rows] == ["superseded"] and rows[0]["reason"] == REASON_GONE
    assert node is None  # the commit was not applied


def test_an_item_that_changed_since_it_was_queued_is_superseded_not_applied(
    tmp_path: Path,
) -> None:
    """The mutation check: without the fingerprint comparison this action would be applied."""

    async def body(client, vault, state, ids):  # type: ignore[no-untyped-def]
        async with _busy(state):
            response = await client.post(
                RESOLVE, json={"candidate_id": ids[0], "action": "discard"}
            )
            assert response.status_code == 202
            # the item is parked again with different content (same id, new digest)
            _queue(vault).enqueue(_candidate(0), "contradiction")
        return await _settled(client), _queue(vault).fingerprint(ids[0])

    rows, after = _run(tmp_path, 1, body)  # type: ignore[misc]
    assert [r["status"] for r in rows] == ["superseded"], rows
    assert rows[0]["reason"] == REASON_CHANGED
    assert after is not None  # still in the queue: the user decides again on what is there now


def test_a_failing_apply_is_marked_failed_with_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from okto_neuron.companion import Companion

    def boom(self, candidate_id, action, *, lease_timeout=None):  # type: ignore[no-untyped-def]
        raise RuntimeError("disk on fire")

    async def body(client, vault, state, ids):  # type: ignore[no-untyped-def]
        async with _busy(state):
            response = await client.post(RESOLVE, json={"candidate_id": ids[0], "action": "commit"})
            assert response.status_code == 202
            monkeypatch.setattr(Companion, "resolve_review", boom)
        return await _settled(client)

    rows = _run(tmp_path, 1, body)
    assert [r["status"] for r in rows] == ["failed"]  # type: ignore[index]
    assert rows[0]["reason"] == "RuntimeError: disk on fire"  # type: ignore[index]


def test_cancel_route_cancels_a_queued_action_only(tmp_path: Path) -> None:
    async def body(client, vault, state, ids):  # type: ignore[no-untyped-def]
        async with _busy(state):
            response = await client.post(RESOLVE, json={"candidate_id": ids[0], "action": "commit"})
            action_id = response.json()["action"]["id"]
            cancelled = await client.post(f"{ACTIONS}/{action_id}/cancel", json={})
            again = await client.post(f"{ACTIONS}/{action_id}/cancel", json={})
            unknown = await client.post(f"{ACTIONS}/ra_nope/cancel", json={})
        rows = await _settled(client)
        return cancelled, again, unknown, rows, _queue(vault).fingerprint(ids[0])

    cancelled, again, unknown, rows, after = _run(tmp_path, 1, body)  # type: ignore[misc]
    assert cancelled.status_code == 200 and cancelled.json()["action"]["status"] == "cancelled"
    assert again.status_code == 409 and again.json()["error"] == "not_cancellable"
    assert unknown.status_code == 404
    assert [r["status"] for r in rows] == ["cancelled"]
    assert after is not None  # never applied


def test_batch_while_busy_queues_every_known_item_and_skips_unknown_ones(tmp_path: Path) -> None:
    async def body(client, vault, state, ids):  # type: ignore[no-untyped-def]
        async with _busy(state):
            response = await client.post(
                BATCH, json={"candidate_ids": [*ids, "missing"], "action": "discard"}
            )
            assert response.status_code == 202, response.text
        return response.json(), await _settled(client), [_queue(vault).fingerprint(i) for i in ids]

    queued, rows, after = _run(tmp_path, 2, body)  # type: ignore[misc]
    assert queued["status"] == "queued" and queued["resolved"] == 0 and queued["skipped"] == 1
    assert len(queued["actions"]) == 2
    assert {a["batch_id"] for a in queued["actions"]} == {queued["batch_id"]}
    assert [r["status"] for r in rows] == ["applied", "applied"]
    assert after == [None, None]


def test_busy_answer_for_an_unknown_item_is_404_not_queued(tmp_path: Path) -> None:
    async def body(client, vault, state, ids):  # type: ignore[no-untyped-def]
        async with _busy(state):
            response = await client.post(RESOLVE, json={"candidate_id": "nope", "action": "commit"})
        return response, (await client.get(ACTIONS)).json()

    response, listing = _run(tmp_path, 1, body)  # type: ignore[misc]
    assert response.status_code == 404 and listing == {"items": [], "queued": 0}


def test_actions_listing_validates_its_query(tmp_path: Path) -> None:
    async def body(client, vault, state, ids):  # type: ignore[no-untyped-def]
        return (
            await client.get(ACTIONS, params={"status": "bogus"}),
            await client.get(ACTIONS, params={"limit": "x"}),
            await client.get(ACTIONS, params={"status": "queued,applied", "limit": "5"}),
        )

    bad_status, bad_limit, ok = _run(tmp_path, 0, body)  # type: ignore[misc]
    assert bad_status.status_code == 400 and bad_limit.status_code == 400
    assert ok.status_code == 200 and ok.json() == {"items": [], "queued": 0}


def test_queued_actions_are_applied_after_a_restart(tmp_path: Path) -> None:
    """A row left queued by a stopped daemon is picked up by the startup resume."""
    vault = Vault.init(tmp_path / "v", packs=["core"])
    ids = _seed(vault, 1)
    store = ReviewActionStore(Path(vault.path) / ".marginalia")
    left = store.enqueue(ids[0], "discard", _queue(vault).fingerprint(ids[0]))  # type: ignore[arg-type]
    # the previous process had claimed it when it stopped: still queued, so it is applied again
    assert store.claim(left.id)

    async def scenario() -> None:
        state = init_state(vault, vault.path)
        server_runtime._resume_durable_runtime_work(state)
        runtime = state.runtime_for(Path(vault.path))
        assert runtime.review_action_task is not None
        await asyncio.wait_for(runtime.review_action_task, 10)

    reset_state_for_tests()
    try:
        asyncio.run(scenario())
        row = store.get(left.id)
        assert row is not None and row.status == "applied", row
        assert _queue(vault).fingerprint(ids[0]) is None
    finally:
        reset_state_for_tests()


def test_the_applier_backs_off_on_a_busy_lease_and_applies_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from okto_neuron.companion import Companion
    from okto_neuron.consolidate.ledger import LeaseBusyError

    real = Companion.resolve_review
    calls: list[int] = []

    def flaky(self, candidate_id, action, *, lease_timeout=None):  # type: ignore[no-untyped-def]
        calls.append(1)
        if len(calls) < 3:
            raise LeaseBusyError(in_process=False)
        return real(self, candidate_id, action, lease_timeout=lease_timeout)

    async def body(client, vault, state, ids):  # type: ignore[no-untyped-def]
        async with _busy(state):
            await client.post(RESOLVE, json={"candidate_id": ids[0], "action": "discard"})
            monkeypatch.setattr(Companion, "resolve_review", flaky)
        return await _settled(client)

    rows = _run(tmp_path, 1, body)
    assert [r["status"] for r in rows] == ["applied"] and len(calls) == 3  # type: ignore[index]
