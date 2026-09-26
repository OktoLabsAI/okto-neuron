"""Regression test for deep-review finding 3.22.

``bootstrap_vault_graph`` opens a ``ladybug.Database`` and then runs several
post-open steps (reading graph identity, initializing/invalidating integrity
state, building the ``VaultGraphHandle``). Before the fix, none of that was
guarded: any exception raised after the database was opened left the handle
open with no reference anywhere that could ever close it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import okto_neuron.store._bootstrap as bootstrap_module


@pytest.fixture(autouse=True)
def reset_cache() -> None:
    yield
    bootstrap_module._bootstrap_cache.clear()


def test_bootstrap_closes_database_when_post_open_step_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def failing_read_graph_identity(database: object) -> object:
        captured["database"] = database
        raise RuntimeError("boom: simulated post-open failure")

    monkeypatch.setattr(bootstrap_module, "_read_graph_identity", failing_read_graph_identity)

    vault_path = tmp_path / "vault"
    with pytest.raises(RuntimeError, match="boom: simulated post-open failure"):
        bootstrap_module.bootstrap_vault_graph(vault_path)

    # The failure must not have left an entry in the process cache.
    resolved = vault_path.expanduser().resolve(strict=False)
    assert resolved not in bootstrap_module._bootstrap_cache

    database = captured["database"]
    assert database is not None
    # A closed ladybug.Database raises from check_for_database_close(); an
    # open (leaked) one does not. This is the direct, driver-level way to
    # observe the leak instead of relying on reopen succeeding, which the
    # deep review confirmed happens either way (ladybug takes no OS-level
    # exclusive lock).
    with pytest.raises(Exception):
        database.check_for_database_close()  # type: ignore[attr-defined]
