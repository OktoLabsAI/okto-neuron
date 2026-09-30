"""Tests for ``store/rebuild_lock.py`` — the ``RebuildLock`` port (spec §2.2).

Two cases, per M2b spec §4 bullet 2:

1. ``acquire_rebuild_lock`` refuses (exit 5, naming the holder pid) when
   another process holds the vault's writer lease (#21), before it takes the
   handle lease.
2. A lock lost between acquisition and ``require_held()`` raises before any
   commit — the fail-closed recheck spec §2.3 invariant (2) depends on
   (``lock.require_held()`` is checked immediately before a destructive
   swap, not only at acquisition).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from okto_neuron.errors import VaultLockHeld
from okto_neuron.store.handle_lease import handle_lease_path
from okto_neuron.store.rebuild_lock import acquire_rebuild_lock


def test_acquire_rebuild_lock_refuses_while_the_writer_lease_is_held_elsewhere(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from okto_neuron.store.writer_lease import acquire_writer_lease as a; "
            "a(sys.argv[1], role='daemon', operation='serve'); print('ready', flush=True); "
            "sys.stdin.read()",
            str(vault_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"
        with pytest.raises(VaultLockHeld, match="daemon pid") as exc_info:
            acquire_rebuild_lock(vault_path, operation="rebuild")
        assert exc_info.value.EXIT_CODE == 5
        assert exc_info.value.holding_pid == holder.pid
        assert not handle_lease_path(vault_path).exists()  # refused before the handle lease
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        holder.wait(timeout=10)
        holder.stdout.close()


def test_rebuild_lock_handle_require_held_fails_closed_before_commit(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    graph_path = vault_path / "graph.lbug"

    lock = acquire_rebuild_lock(vault_path, operation="rebuild")
    with lock as handle:
        handle.require_held()  # still held immediately after acquisition

        # Simulate ownership loss the same way another owner's takeover
        # would look on disk: the lease's token no longer matches what is
        # persisted at the lock path. Mirrors
        # test_kg_rebuild.py::test_standalone_rebuild_rechecks_lease_immediately_before_swap.
        handle_lease_path(vault_path).write_text("different-owner", encoding="utf-8")

        with pytest.raises(VaultLockHeld, match="ownership changed before graph swap"):
            handle.require_held()
            graph_path.write_bytes(b"would-be-commit")  # never reached

    assert not graph_path.exists()
