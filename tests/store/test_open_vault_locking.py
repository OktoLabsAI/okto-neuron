"""``_open_vault`` serialises per vault path, not process-wide (issue #40)."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from okto_neuron.store import vault as vault_mod
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture
def slow_digest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Park the index open of ``tmp_path/'slow'`` until the test releases it."""
    slow_path = (tmp_path / "slow").resolve(strict=False)
    gate = threading.Event()
    entered = threading.Event()
    real_open_index = vault_mod._open_index

    def _open_index(vault_path: Path, store: object, backend_name: str) -> object:
        if vault_path == slow_path:
            entered.set()
            assert gate.wait(15), "test never released the slow digest"
        return real_open_index(vault_path, store, backend_name)

    monkeypatch.setattr(vault_mod, "_open_index", _open_index)
    opened: list[Path] = []
    try:
        yield slow_path, gate, entered, opened
    finally:
        gate.set()
        for path in opened:
            cached = vault_mod._STORE_CACHE.pop(path, None)
            if cached is not None:
                cached.close()
            VaultConnection.close_vault(path)


def _open_in_thread(path: Path, results: list[Any]) -> threading.Thread:
    def _run() -> None:
        try:
            results.append(vault_mod._open_vault(path))
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            results.append(exc)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


def test_slow_open_of_one_vault_does_not_delay_opening_or_resetting_another(
    tmp_path: Path, slow_digest
) -> None:
    slow_path, gate, entered, opened = slow_digest
    fast_path = (tmp_path / "fast").resolve(strict=False)
    opened.extend([slow_path, fast_path])

    slow_results: list[Any] = []
    slow_thread = _open_in_thread(slow_path, slow_results)
    assert entered.wait(10)

    started = time.perf_counter()
    store = vault_mod._open_vault(fast_path)
    open_s = time.perf_counter() - started
    started = time.perf_counter()
    vault_mod.wipe_vault(fast_path)
    wipe_s = time.perf_counter() - started
    assert slow_thread.is_alive(), "the slow digest should still be running"
    assert open_s < 5 and wipe_s < 5, (open_s, wipe_s)
    assert store is not None

    gate.set()
    slow_thread.join(15)
    assert not slow_thread.is_alive()
    assert not isinstance(slow_results[0], BaseException), slow_results


def test_two_threads_opening_the_same_path_share_one_store(tmp_path: Path, slow_digest) -> None:
    slow_path, gate, entered, opened = slow_digest
    opened.append(slow_path)
    results: list[Any] = []
    first = _open_in_thread(slow_path, results)
    assert entered.wait(10)
    second = _open_in_thread(slow_path, results)
    time.sleep(0.2)
    assert second.is_alive(), "the second opener must wait on the per-path lock"

    gate.set()
    first.join(15)
    second.join(15)
    assert not first.is_alive() and not second.is_alive()
    assert len(results) == 2 and results[0] is results[1]
