"""Tests for ``store/rebuild_lock.py`` — the ``RebuildLock`` port (spec §2.2).

Two cases, per M2b spec §4 bullet 2:

1. ``acquire_rebuild_lock`` refuses with today's exact ``VaultLockHeld``
   message when a server owns the vault — mirrors
   ``tests/cli/test_kg_rebuild.py::test_standalone_graph_swap_refuses_live_vault_daemon``,
   since ``acquire_rebuild_lock`` is meant to be a behavior-preserving thin
   wrapper around ``cli.kg._require_offline_graph_swap``'s same check.
2. A lock lost between acquisition and ``require_held()`` raises before any
   commit — the fail-closed recheck spec §2.3 invariant (2) depends on
   (``lock.require_held()`` is checked immediately before a destructive
   swap, not only at acquisition).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_neuron.errors import VaultLockHeld
from okto_neuron.store.handle_lease import handle_lease_path
from okto_neuron.store.rebuild_lock import acquire_rebuild_lock


def test_acquire_rebuild_lock_refuses_live_vault_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    monkeypatch.setattr("okto_neuron.server.lifecycle.active_server_pid", lambda _vault: 4242)

    with pytest.raises(VaultLockHeld, match="stop the server first") as exc_info:
        acquire_rebuild_lock(vault_path, operation="rebuild")

    assert exc_info.value.holding_pid == 4242


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
