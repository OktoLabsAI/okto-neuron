"""``ask`` and ``explore`` take their vault lease before dispatching to a worker (issue #40).

Cancelling the awaiting tool call while the worker is mid-flight must never leak
a lease: a leaked lease blocks vault deletion and maintenance forever.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from okto_neuron import Vault
from okto_neuron.companion import Companion
from okto_neuron.llm import StubLLM
from okto_neuron.server import http as http_mod
from okto_neuron.server import runtime as runtime_mod
from okto_neuron.server._store_io import wait_executors_idle_async
from okto_neuron.server.state import init_state, reset_state_for_tests


@pytest.fixture
def mcp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.delenv("OKTO_NEURON_VAULT", raising=False)
    reset_state_for_tests()
    vault = Vault.init(tmp_path / "v", embedder="stub")
    state = init_state(vault, Path(vault.path))
    try:
        yield state, Path(vault.path)
    finally:
        reset_state_for_tests()


class _Gate:
    """Parks the worker inside the companion call until the test lets it go."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.proceed = threading.Event()

    def wait(self) -> None:
        self.entered.set()
        assert self.proceed.wait(30)


async def _wait_for(event: threading.Event) -> None:
    deadline = time.monotonic() + 30
    while not event.is_set():
        assert time.monotonic() < deadline, "worker never reached the companion call"
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["ask", "explore"])
async def test_cancelled_tool_call_does_not_leak_the_lease(
    mcp_state, monkeypatch: pytest.MonkeyPatch, tool_name: str
) -> None:
    state, vault_path = mcp_state
    gate = _Gate()

    class _GatedCompanion(Companion):
        def ask(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            gate.wait()
            return super().ask(*args, **kwargs)

        def explore(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
            gate.wait()
            return super().explore(*args, **kwargs)

    monkeypatch.setattr(
        http_mod, "companion_for", lambda vault: _GatedCompanion(vault, provider=StubLLM())
    )
    server = runtime_mod._build_mcp_server(state)
    tool = await server.get_tool(tool_name)
    args: dict[str, Any] = (
        {"question": "what is this?", "k": 3}
        if tool_name == "ask"
        else {"topic": "knowledge graph", "k": 3}
    )
    pool = state.vault_pool

    task = asyncio.create_task(tool.fn(**args))
    await _wait_for(gate.entered)
    assert pool.lease_count(vault_path) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.proceed.set()

    assert await wait_executors_idle_async(30)
    assert pool.lease_count(vault_path) == 0, "a cancelled call leaked its vault lease"


@pytest.mark.asyncio
async def test_ask_cancelled_while_queued_releases_its_lease(
    mcp_state, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lease is taken before dispatch, so an ask that never reaches a job
    worker (cancelled while queued behind a busy one) must still hand it back."""
    from okto_neuron.server._store_io import configure_executors, job_io

    state, vault_path = mcp_state
    monkeypatch.setattr(
        http_mod, "companion_for", lambda vault: Companion(vault, provider=StubLLM())
    )
    configure_executors(store_workers=2, job_workers=1)
    release_worker = threading.Event()
    parked = threading.Event()

    def _park() -> None:
        parked.set()
        assert release_worker.wait(30)

    server = runtime_mod._build_mcp_server(state)
    tool = await server.get_tool("ask")
    pool = state.vault_pool
    blocker = asyncio.ensure_future(job_io(_park))
    try:
        await _wait_for(parked)
        task = asyncio.create_task(tool.fn(question="what is this?", k=3))
        deadline = time.monotonic() + 30
        while pool.lease_count(vault_path) == 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        assert pool.lease_count(vault_path) == 1, "ask should hold its lease while queued"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release_worker.set()
        await blocker
    assert await wait_executors_idle_async(30)
    deadline = time.monotonic() + 30
    while pool.lease_count(vault_path) and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    assert pool.lease_count(vault_path) == 0, "an ask cancelled while queued leaked its lease"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["ask", "explore"])
async def test_lease_is_never_taken_on_the_event_loop_thread(
    mcp_state, monkeypatch: pytest.MonkeyPatch, tool_name: str
) -> None:
    from okto_neuron.server._vault_pool import VaultPool

    state, vault_path = mcp_state
    monkeypatch.setattr(
        http_mod, "companion_for", lambda vault: Companion(vault, provider=StubLLM())
    )
    loop_thread = threading.get_ident()
    leased_on: list[int] = []
    real_lease = VaultPool.lease

    def _recording_lease(self: VaultPool, path: Path):  # type: ignore[no-untyped-def]
        leased_on.append(threading.get_ident())
        return real_lease(self, path)

    monkeypatch.setattr(VaultPool, "lease", _recording_lease)
    server = runtime_mod._build_mcp_server(state)
    tool = await server.get_tool(tool_name)
    args: dict[str, Any] = (
        {"question": "what is this?", "k": 3}
        if tool_name == "ask"
        else {"topic": "knowledge graph", "k": 3}
    )
    await tool.fn(**args)
    assert leased_on, "the tool never leased the vault"
    assert loop_thread not in leased_on, f"{tool_name} leased the vault on the event loop thread"
    assert state.vault_pool.lease_count(vault_path) == 0
