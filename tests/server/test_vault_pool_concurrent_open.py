"""A slow open of one vault must not stall anyone else (issue #40).

``Vault.open`` is replaced by a gate-controlled stand-in for one vault; the other
vaults open for real. Waiters for the slow vault join the one in-flight open and,
when they arrive through ``acquire_off_loop``, wait on the event loop instead of
parking a store/job worker.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest

from okto_neuron.errors import OktoNeuronError
from okto_neuron.server._store_io import (
    acquire_off_loop,
    configure_executors,
    job_io,
    shutdown_executors,
    store_io,
)
from okto_neuron.server._vault_pool import VaultPool, VaultPoolError
from okto_neuron.vault import Vault

_LEASE_P99_S = 0.05


def _vault_path(tmp_path: Path, name: str) -> Path:
    vault = Vault.init(tmp_path / name, packs=["core"])
    path = Path(vault.path).resolve(strict=False)
    vault.close()
    return path


class _SlowOpen:
    """Stand-in for ``Vault.open``: ``slow`` paths park on ``gate`` (or raise)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, slow: set[Path]) -> None:
        self.slow = slow
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.fail_with: Exception | None = None
        self.calls: list[Path] = []
        self.events: list[str] = []
        real_open = Vault.open

        def _open(path: Any, *args: Any, **kwargs: Any) -> Vault:
            resolved = Path(path).resolve(strict=False)
            self.calls.append(resolved)
            self.events.append(f"open:{resolved.name}")
            if resolved in self.slow:
                self.entered.set()
                assert self.gate.wait(15), "test never released the slow open"
                if self.fail_with is not None:
                    raise self.fail_with
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(Vault, "open", staticmethod(_open))

    def opens_of(self, path: Path) -> int:
        return sum(1 for call in self.calls if call == path)


def _in_thread(fn: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    outcome: list[Any] = []

    def _run() -> None:
        try:
            outcome.append(fn())
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            outcome.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread, outcome


def _join(*threads: threading.Thread, timeout: float = 15) -> None:
    """Join with a bound: a stuck thread fails the test in seconds, not minutes."""
    for thread in threads:
        thread.join(timeout)
        assert not thread.is_alive(), "a pool thread is stuck"


async def _entered(slow: "_SlowOpen") -> None:
    assert await asyncio.get_running_loop().run_in_executor(None, slow.entered.wait, 10)


async def _done(*awaitables: Any, **kwargs: Any) -> Any:
    return await asyncio.wait_for(asyncio.gather(*awaitables, **kwargs), 15)


def _p99(samples: list[float]) -> float:
    return sorted(samples)[int(len(samples) * 0.99) - 1]


@pytest.fixture
def pool():
    created = VaultPool()
    try:
        yield created
    finally:
        created.close_all()


def test_slow_open_of_one_vault_does_not_delay_leases_of_another(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool: VaultPool
) -> None:
    a, b = _vault_path(tmp_path, "a"), _vault_path(tmp_path, "b")
    slow = _SlowOpen(monkeypatch, {b})
    pool.lease(a).release()

    opener, outcome = _in_thread(lambda: pool.lease(b))
    assert slow.entered.wait(10)

    latencies: list[float] = []
    for _ in range(200):
        started = time.perf_counter()
        pool.lease(a).release()
        latencies.append(time.perf_counter() - started)
    assert opener.is_alive(), "the slow open should still be running"
    slow.gate.set()
    _join(opener)

    assert _p99(latencies) < _LEASE_P99_S, f"p99 {_p99(latencies) * 1000:.1f} ms"
    outcome[0].release()
    assert slow.opens_of(b) == 1


def test_concurrent_leases_of_one_slow_vault_open_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool: VaultPool
) -> None:
    b = _vault_path(tmp_path, "b")
    slow = _SlowOpen(monkeypatch, {b})
    threads = [_in_thread(lambda: pool.lease(b)) for _ in range(6)]
    assert slow.entered.wait(10)
    time.sleep(0.2)
    slow.gate.set()
    _join(*(thread for thread, _ in threads))

    leases = [outcome[0] for _, outcome in threads]
    assert all(hasattr(lease, "release") for lease in leases), leases
    assert slow.opens_of(b) == 1
    assert len({id(lease.vault) for lease in leases}) == 1
    assert pool.lease_count(b) == 6
    for lease in leases:
        lease.release()


def test_get_or_open_joins_an_open_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool: VaultPool
) -> None:
    b = _vault_path(tmp_path, "b")
    slow = _SlowOpen(monkeypatch, {b})
    opener, leased = _in_thread(lambda: pool.lease(b))
    assert slow.entered.wait(10)
    compat, raw = _in_thread(lambda: pool.get_or_open(b))
    time.sleep(0.1)
    slow.gate.set()
    _join(opener, compat)

    assert raw[0] is leased[0].vault
    assert slow.opens_of(b) == 1
    leased[0].release()


def test_failed_open_reaches_every_waiter_and_is_not_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool: VaultPool
) -> None:
    b = _vault_path(tmp_path, "b")
    slow = _SlowOpen(monkeypatch, {b})
    slow.fail_with = OktoNeuronError("graph is unreadable")
    threads = [_in_thread(lambda: pool.lease(b)) for _ in range(4)]
    assert slow.entered.wait(10)
    time.sleep(0.2)
    slow.gate.set()
    _join(*(thread for thread, _ in threads))

    for _, outcome in threads:
        assert isinstance(outcome[0], VaultPoolError)
        assert outcome[0].code == "open_failed"
    assert slow.opens_of(b) == 1
    assert pool.paths() == []
    assert not pool._opening
    assert b not in pool._handle_leases

    slow.fail_with = None
    with pool.lease(b) as vault:
        assert vault is not None
    assert slow.opens_of(b) == 2


def test_eviction_never_picks_a_vault_that_is_still_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool: VaultPool
) -> None:
    pool.max_open = 2
    a, b, c = (_vault_path(tmp_path, name) for name in "abc")
    slow = _SlowOpen(monkeypatch, {b})
    held = pool.lease(a)

    opener, outcome = _in_thread(lambda: pool.lease(b))
    assert slow.entered.wait(10)
    # a (leased) + b (opening) fill the pool: a third vault has nothing to evict.
    with pytest.raises(VaultPoolError) as full:
        pool.lease(c)
    assert full.value.code == "pool_full"
    assert b in pool._opening and set(pool.paths()) == {a}

    slow.gate.set()
    _join(opener)
    assert set(pool.paths()) == {a, b}
    assert len(pool.paths()) <= pool.max_open
    held.release()
    outcome[0].release()


def test_evicted_vault_released_during_its_close_is_reopened_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool: VaultPool
) -> None:
    pool.max_open = 3
    a, c, x, b = (_vault_path(tmp_path, name) for name in ("a", "c", "x", "b"))
    slow = _SlowOpen(monkeypatch, set())
    close_gate = threading.Event()
    close_started = threading.Event()
    real_close = Vault.close
    closing_handles: list[Vault] = []

    def _close(self: Vault, *args: Any, **kwargs: Any) -> Any:
        if Path(self.path).resolve(strict=False) == c and not close_started.is_set():
            closing_handles.append(self)
            slow.events.append("close:c:start")
            close_started.set()
            assert close_gate.wait(15)
            result = real_close(self, *args, **kwargs)
            slow.events.append("close:c:done")
            return result
        return real_close(self, *args, **kwargs)

    monkeypatch.setattr(Vault, "close", _close)
    pool.lease(c).release()  # c is the least recently used idle handle
    pool.lease(x).release()
    held = pool.lease(a)

    opener, opened = _in_thread(lambda: pool.lease(b))  # evicts c, parks in its close
    assert close_started.wait(10)
    releaser, relet = _in_thread(lambda: pool.lease(c))
    time.sleep(0.2)
    assert releaser.is_alive(), "the re-lease must wait for the close, not take the closing handle"
    assert slow.opens_of(c) == 1
    close_gate.set()
    _join(opener, releaser)

    fresh = relet[0]
    assert fresh.vault is not closing_handles[0]
    assert slow.opens_of(c) == 2
    assert slow.events.index("close:c:done") < len(slow.events) - 1 - slow.events[::-1].index(
        "open:c"
    ), slow.events
    held.release()
    opened[0].release()
    fresh.release()


def test_fence_during_an_open_hands_the_new_handle_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool: VaultPool
) -> None:
    b = _vault_path(tmp_path, "b")
    slow = _SlowOpen(monkeypatch, {b})
    opener, outcome = _in_thread(lambda: pool.lease(b))
    assert slow.entered.wait(10)

    assert pool.fence(b) == 0
    releaser, released = _in_thread(lambda: pool.release_path(b, require_fenced=True))
    time.sleep(0.2)
    assert releaser.is_alive(), "release_path must wait for the open instead of racing it"
    slow.gate.set()
    _join(opener, releaser)

    assert isinstance(outcome[0], VaultPoolError) and outcome[0].code == "vault_fenced"
    assert released == [False]
    assert pool.paths() == [] and pool.lease_count(b) == 0
    assert b not in pool._handle_leases and not pool._opening

    pool.unfence(b)
    with pool.lease(b):
        pass


# --- waiters that arrive through acquire_off_loop never pin a worker ---------


@pytest.fixture
def executors():
    configure_executors(store_workers=4, job_workers=2)
    try:
        yield
    finally:
        shutdown_executors()


@pytest.mark.asyncio
async def test_waiters_for_a_slow_open_hold_no_store_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool: VaultPool, executors: None
) -> None:
    a, b = _vault_path(tmp_path, "a"), _vault_path(tmp_path, "b")
    slow = _SlowOpen(monkeypatch, {b})
    (await acquire_off_loop(pool.lease, a)).release()

    waiters = [asyncio.create_task(acquire_off_loop(pool.lease, b)) for _ in range(6)]
    await _entered(slow)
    await asyncio.sleep(0.2)  # every waiter has been dispatched and parked

    latencies: list[float] = []
    for _ in range(200):
        started = time.perf_counter()
        (await acquire_off_loop(pool.lease, a)).release()
        latencies.append(time.perf_counter() - started)
    # The opener holds one store worker; the five joiners must hold none.
    for _ in range(3):
        assert await asyncio.wait_for(store_io(lambda: "free"), 2) == "free"
    assert await asyncio.wait_for(job_io(lambda: "free"), 2) == "free"
    assert not any(task.done() for task in waiters)

    slow.gate.set()
    leases = await _done(*waiters)
    assert slow.opens_of(b) == 1
    assert _p99(latencies) < _LEASE_P99_S, f"p99 {_p99(latencies) * 1000:.1f} ms"
    assert pool.lease_count(b) == 6
    for lease in leases:
        lease.release()


@pytest.mark.asyncio
async def test_cancelled_waiter_leaks_nothing_and_does_not_abort_the_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool: VaultPool, executors: None
) -> None:
    b = _vault_path(tmp_path, "b")
    slow = _SlowOpen(monkeypatch, {b})
    opener = asyncio.create_task(acquire_off_loop(pool.lease, b))
    await _entered(slow)
    cancelled = asyncio.create_task(acquire_off_loop(pool.lease, b))
    survivor = asyncio.create_task(acquire_off_loop(pool.lease, b))
    await asyncio.sleep(0.2)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled

    slow.gate.set()
    first, second = await _done(opener, survivor)
    assert slow.opens_of(b) == 1
    assert pool.lease_count(b) == 2
    first.release()
    second.release()
    assert pool.lease_count(b) == 0


@pytest.mark.asyncio
async def test_failed_open_raises_for_waiters_on_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pool: VaultPool, executors: None
) -> None:
    b = _vault_path(tmp_path, "b")
    slow = _SlowOpen(monkeypatch, {b})
    slow.fail_with = OktoNeuronError("graph is unreadable")
    tasks = [asyncio.create_task(acquire_off_loop(pool.lease, b)) for _ in range(4)]
    await _entered(slow)
    await asyncio.sleep(0.2)
    slow.gate.set()
    results = await _done(*tasks, return_exceptions=True)

    assert all(isinstance(r, VaultPoolError) and r.code == "open_failed" for r in results)
    assert slow.opens_of(b) == 1
    assert pool.paths() == []


@pytest.mark.asyncio
async def test_concurrent_asks_for_a_slow_open_share_it_and_pin_no_job_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, executors: None
) -> None:
    from okto_neuron.companion import Companion
    from okto_neuron.llm import StubLLM
    from okto_neuron.server import http as http_mod
    from okto_neuron.server import runtime as runtime_mod
    from okto_neuron.server.state import init_state, reset_state_for_tests

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.delenv("OKTO_NEURON_VAULT", raising=False)
    reset_state_for_tests()
    try:
        vault = Vault.init(tmp_path / "v", embedder="stub")
        vault_path = Path(vault.path).resolve(strict=False)
        state = init_state(vault, vault_path)
        monkeypatch.setattr(
            http_mod, "companion_for", lambda vault: Companion(vault, provider=StubLLM())
        )
        other = _vault_path(tmp_path, "other")
        pool = state.vault_pool
        (await acquire_off_loop(pool.lease, other)).release()
        assert pool.release_path(vault_path, force_legacy_pin=True)  # make the next lease cold
        slow = _SlowOpen(monkeypatch, {vault_path})

        server = runtime_mod._build_mcp_server(state)
        tool = await server.get_tool("ask")
        asks = [
            asyncio.create_task(tool.fn(question="what is this?", k=3)) for _ in range(6)
        ]
        await _entered(slow)
        await asyncio.sleep(0.3)

        latencies: list[float] = []
        for _ in range(100):
            started = time.perf_counter()
            (await acquire_off_loop(pool.lease, other)).release()
            latencies.append(time.perf_counter() - started)
        assert await asyncio.wait_for(job_io(lambda: "free"), 2) == "free"
        for _ in range(3):
            assert await asyncio.wait_for(store_io(lambda: "free"), 2) == "free"
        assert not any(task.done() for task in asks)

        slow.gate.set()
        results = await _done(*asks, return_exceptions=True)
        assert not [r for r in results if isinstance(r, BaseException)], results
        assert slow.opens_of(vault_path) == 1
        assert _p99(latencies) < _LEASE_P99_S, f"p99 {_p99(latencies) * 1000:.1f} ms"
        assert pool.lease_count(vault_path) == 0
    finally:
        reset_state_for_tests()
