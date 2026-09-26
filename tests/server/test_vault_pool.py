"""Unit tests for the per-process vault pool (ADR 0014).

The pool owns every open Ladybug handle (active + per-connection selected) and
must survive the path-wide close constraint: closing a handle for a path closes
ALL handles for that path, so the pool liveness-checks and reopens stale entries
and never closes a duplicate it drops.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from okto_neuron.server._vault_pool import VaultPool, VaultPoolError
from okto_neuron.vault import Vault


def _make_vault(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    Vault.init(target, packs=["core"]).close()
    return target.resolve(strict=False)


def test_same_path_returns_identical_handle(tmp_path: Path) -> None:
    path = _make_vault(tmp_path, "alpha")
    pool = VaultPool()
    try:
        first = pool.get_or_open(path)
        second = pool.get_or_open(path)
        assert first is second
    finally:
        pool.close_all()


def test_distinct_paths_are_distinct_handles(tmp_path: Path) -> None:
    a = _make_vault(tmp_path, "alpha")
    b = _make_vault(tmp_path, "beta")
    pool = VaultPool()
    try:
        va = pool.get_or_open(a)
        vb = pool.get_or_open(b)
        assert va is not vb
        assert set(pool.paths()) == {a, b}
    finally:
        pool.close_all()


def test_stale_entry_self_heals(tmp_path: Path) -> None:
    path = _make_vault(tmp_path, "alpha")
    pool = VaultPool()
    try:
        first = pool.get_or_open(path)
        # Simulate a path-wide close elsewhere (curation/reembed/wipe).
        first.close()
        assert getattr(first, "_closed", False) is True
        healed = pool.get_or_open(path)
        assert healed is not first
        assert getattr(healed, "_closed", False) is False
    finally:
        pool.close_all()


def test_pool_full_raises_at_max_open(tmp_path: Path) -> None:
    pool = VaultPool()
    pool.max_open = 2
    paths = [_make_vault(tmp_path, f"v{i}") for i in range(3)]
    try:
        pool.get_or_open(paths[0])
        pool.get_or_open(paths[1])
        with pytest.raises(VaultPoolError) as excinfo:
            pool.get_or_open(paths[2])
        assert excinfo.value.code == "pool_full"
    finally:
        pool.close_all()


def test_close_all_is_idempotent(tmp_path: Path) -> None:
    path = _make_vault(tmp_path, "alpha")
    pool = VaultPool()
    pool.get_or_open(path)
    pool.close_all()
    pool.close_all()  # must not raise
    assert pool.paths() == []


def test_adopt_returns_pooled_without_closing_duplicate(tmp_path: Path) -> None:
    path = _make_vault(tmp_path, "alpha")
    pool = VaultPool()
    try:
        pooled = pool.get_or_open(path)
        # A second open of the same path is the "duplicate". adopt must return the
        # already-pooled instance and must NOT close the duplicate — a close is
        # path-wide and would kill the pooled handle too.
        duplicate = Vault.open(path)
        returned = pool.adopt(duplicate, path)
        assert returned is pooled
        # The pooled handle stays live (the duplicate was dropped, not closed).
        assert getattr(pooled, "_closed", False) is False
    finally:
        pool.close_all()


# ── lock-free paths() snapshot ──────────────────────────────────────────────


def test_paths_snapshot_reflects_open_and_close(tmp_path: Path) -> None:
    """The lock-free snapshot backing paths() must be rebuilt at every
    mutation site (get_or_open, adopt, close_all), not just at get_or_open —
    else it silently drifts from self._vaults over the pool's lifetime."""
    a = _make_vault(tmp_path, "alpha")
    b = _make_vault(tmp_path, "beta")
    pool = VaultPool()
    assert pool.paths() == []

    pool.get_or_open(a)
    assert pool.paths() == [a]

    adopted = Vault.open(b)
    pool.adopt(adopted, b)
    assert set(pool.paths()) == {a, b}

    pool.close_all()
    assert pool.paths() == []


def test_paths_does_not_block_while_lock_held_by_another_thread(tmp_path: Path) -> None:
    """get_or_open holds self._lock across a blocking Vault.open — seconds to
    minutes on a big graph or WAL recovery, typically from the curation
    runner thread. If paths() also acquired that lock, every event-loop
    caller (the scheduler's per-tick sweep, /health) would stall on it too,
    freezing the whole daemon's REST/MCP surface for as long as the open
    takes. Simulate a long-held lock directly (standing in for a slow open)
    and prove paths() still returns promptly and correctly."""
    path = _make_vault(tmp_path, "alpha")
    pool = VaultPool()
    try:
        pool.get_or_open(path)  # populate one entry + its snapshot

        release = threading.Event()
        acquired = threading.Event()

        def _hold_lock() -> None:
            with pool._lock:
                acquired.set()
                release.wait(5.0)

        holder = threading.Thread(target=_hold_lock, daemon=True)
        holder.start()
        try:
            assert acquired.wait(2.0), "lock-holding thread never acquired the lock"
            start = time.monotonic()
            result = pool.paths()
            elapsed = time.monotonic() - start
            assert elapsed < 0.5, f"paths() blocked for {elapsed:.2f}s on a held lock"
            assert result == [path]
        finally:
            release.set()
            holder.join(5.0)
    finally:
        pool.close_all()
