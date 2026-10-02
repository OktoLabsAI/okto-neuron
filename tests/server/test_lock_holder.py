"""The writer lock names its holder, and a busy answer says who it is (field report: bare "busy")."""

from __future__ import annotations

import asyncio
import functools
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

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
    [("ingest-item", 15), ("mcp-remember", 15), ("curation-job", 30), ("rebuild-job", 30),
     ("vault-maintenance", 10), ("review-op", 5), ("something-new", 10)],
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
    assert response.status_code == 503, response.text  # status and code unchanged for existing clients
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
