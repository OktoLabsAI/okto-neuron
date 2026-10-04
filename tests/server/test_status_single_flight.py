"""GET /api/v1/status coalesces concurrent callers, and only concurrent ones (refs #14).

Pollers (UI, CLI, health checks) used to each run the full ``_status_payload``
walk. ``api_status`` now goes through ``single_flight``: callers that overlap
share one execution; nothing is cached once it finishes.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from starlette.testclient import TestClient

from okto_neuron.server import _folder_watch as fw
from okto_neuron.server import http as http_mod
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import (
    ServerState,
    bind_vault_runtime,
    init_state,
    reset_state_for_tests,
)

N = 8


class _StubVault:
    recovered_from_corruption = False

    def close(self) -> None:
        pass


class _Gate:
    """Counts handler entries and executions; holds the execution until N callers joined."""

    def __init__(self, expected: int, *, fail_first: bool = False) -> None:
        self.expected = expected
        self.fail_first = fail_first
        self.entered = 0
        self.executions = 0
        self._lock = threading.Lock()
        self.all_entered = threading.Event()
        self._real_payload = http_mod._status_payload
        self._real_single_flight = http_mod.single_flight

    def single_flight(self, key, fn, *args, **kwargs):
        # Runs synchronously inside the handler, before it awaits the flight.
        with self._lock:
            self.entered += 1
            if self.entered >= self.expected:
                self.all_entered.set()
        return self._real_single_flight(key, fn, *args, **kwargs)

    def payload(self, state):
        with self._lock:
            self.executions += 1
            number = self.executions
        assert self.all_entered.wait(timeout=20), "callers never all entered the handler"
        if self.fail_first and number == 1:
            raise RuntimeError("status exploded")
        return self._real_payload(state)


@pytest.fixture
def state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    fw._WATCH_STATUS.clear()
    reset_state_for_tests()
    yield init_state(_StubVault(), tmp_path)
    reset_state_for_tests()


@pytest.fixture
def client(state):
    with TestClient(
        build_rest_app(state), base_url="http://127.0.0.1", raise_server_exceptions=False
    ) as c:
        yield c


def _install(monkeypatch: pytest.MonkeyPatch, gate: _Gate) -> None:
    monkeypatch.setattr(http_mod, "single_flight", gate.single_flight)
    monkeypatch.setattr(http_mod, "_status_payload", gate.payload)


def _burst(client: TestClient, n: int = N):
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(lambda _: client.get("/api/v1/status"), range(n)))


def test_overlapping_callers_share_one_execution(client, monkeypatch) -> None:
    gate = _Gate(N)
    _install(monkeypatch, gate)

    responses = _burst(client)

    assert gate.entered == N
    assert gate.executions == 1
    assert [r.status_code for r in responses] == [200] * N
    bodies = [r.json() for r in responses]
    assert all(body == bodies[0] for body in bodies)
    assert bodies[0]["scope"] == "application"
    assert all(r.headers["cache-control"] == "no-store" for r in responses)


def test_sequential_calls_are_not_cached(client, state, monkeypatch) -> None:
    gate = _Gate(1)
    _install(monkeypatch, gate)

    first = client.get("/api/v1/status").json()
    assert first["ingest"]["total"] == 0

    from okto_neuron.server._ingest_queue import IngestItem

    state.ingest_queue.append(IngestItem(id="q1", name="q1.md", path="/tmp/q1.md", status="queued"))
    second = client.get("/api/v1/status").json()

    assert gate.executions == 2
    assert second["ingest"]["total"] == 1
    assert second["ingest"]["queued"] == 1
    assert first != second


def test_application_and_vault_scope_do_not_share(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    reset_state_for_tests()
    state = init_state(_StubVault(), tmp_path / "a")
    path_b = tmp_path / "b"
    path_b.mkdir()
    runtime_a = state.runtime_for(tmp_path / "a")
    runtime_b = state.runtime_for(path_b, vault=_StubVault())
    assert isinstance(state, ServerState)

    gate = _Gate(3)
    _install(monkeypatch, gate)

    async def call(runtime):
        if runtime is None:
            return await http_mod.api_status(None)  # type: ignore[arg-type]
        with bind_vault_runtime(runtime):
            return await http_mod.api_status(None)  # type: ignore[arg-type]

    async def scenario():
        return await asyncio.gather(call(None), call(runtime_a), call(runtime_b))

    try:
        import json

        app_resp, a_resp, b_resp = asyncio.run(scenario())
    finally:
        reset_state_for_tests()

    assert gate.entered == 3
    assert gate.executions == 3
    app_body, a_body, b_body = (json.loads(r.body) for r in (app_resp, a_resp, b_resp))
    assert app_body["scope"] == "application"
    assert a_body["scope"] == "vault" and b_body["scope"] == "vault"
    assert a_body["vault_path"] != b_body["vault_path"]


def test_exception_reaches_every_waiter_then_recovers(client, monkeypatch) -> None:
    gate = _Gate(N, fail_first=True)
    _install(monkeypatch, gate)

    responses = _burst(client)

    assert gate.executions == 1
    assert [r.status_code for r in responses] == [500] * N

    # The failed flight left nothing behind: the next call executes and succeeds.
    follow_up = client.get("/api/v1/status")
    assert gate.executions == 2
    assert follow_up.status_code == 200
    assert follow_up.json()["scope"] == "application"


def test_draining_answers_before_any_flight(client, state, monkeypatch) -> None:
    gate = _Gate(1)
    _install(monkeypatch, gate)
    state.shutting_down = True
    try:
        response = client.get("/api/v1/status")
    finally:
        state.shutting_down = False

    assert response.status_code == 503
    assert gate.entered == 0 and gate.executions == 0
