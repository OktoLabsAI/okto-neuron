"""Lease, fence, and idle-eviction contract for the ADR 0034 vault pool."""

from __future__ import annotations

from pathlib import Path
import os

import pytest

from okto_neuron.server._vault_pool import VaultPool, VaultPoolError
from okto_neuron.server import _integrity as graph_integrity
from okto_neuron.server.state import ServerState
from okto_neuron.store import schema
from okto_neuron.store.integrity import AuditStatus
from okto_neuron.store.integrity_state import (
    GraphIntegrityState,
    IntegrityFenceError,
    load_integrity_state,
    write_integrity_state,
)
from okto_neuron.vault import Vault


def _vault_path(tmp_path: Path, name: str) -> Path:
    vault = Vault.init(tmp_path / name, packs=["core"])
    path = Path(vault.path).resolve(strict=False)
    vault.close()
    return path


def test_lease_release_is_counted_and_idempotent(tmp_path: Path) -> None:
    path = _vault_path(tmp_path, "alpha")
    pool = VaultPool()
    try:
        lease = pool.lease(path)
        assert lease.path == path
        assert pool.lease_count(path) == 1

        lease.release()
        lease.release()
        assert lease.released is True
        assert pool.lease_count(path) == 0
    finally:
        pool.close_all()


def test_fence_rejects_new_work_until_active_lease_finishes(tmp_path: Path) -> None:
    path = _vault_path(tmp_path, "alpha")
    pool = VaultPool()
    lease = pool.lease(path)
    handle = lease.vault

    assert pool.fence(path) == 1
    with pytest.raises(VaultPoolError, match="fenced") as fenced:
        pool.lease(path)
    assert fenced.value.code == "vault_fenced"

    with pytest.raises(VaultPoolError) as in_use:
        pool.release_path(path, require_fenced=True)
    assert in_use.value.code == "vault_in_use"
    assert getattr(handle, "_closed", False) is False

    lease.release()
    assert pool.release_path(path, require_fenced=True) is True
    assert getattr(handle, "_closed", False) is True
    assert pool.paths() == []


def test_lru_evicts_only_idle_lease_managed_handles(tmp_path: Path) -> None:
    paths = [_vault_path(tmp_path, name) for name in ("a", "b", "c")]
    pool = VaultPool()
    pool.max_open = 2
    try:
        active = pool.lease(paths[0])
        idle = pool.lease(paths[1])
        idle_handle = idle.vault
        idle.release()

        newest = pool.lease(paths[2])
        assert set(pool.paths()) == {paths[0], paths[2]}
        assert getattr(active.vault, "_closed", False) is False
        assert getattr(idle_handle, "_closed", False) is True

        newest.release()
        active.release()
    finally:
        pool.close_all()


def test_compatibility_raw_handle_is_not_released_or_evicted(tmp_path: Path) -> None:
    path = _vault_path(tmp_path, "alpha")
    pool = VaultPool()
    handle = pool.get_or_open(path)
    try:
        with pytest.raises(VaultPoolError) as pinned:
            pool.release_path(path)
        assert pinned.value.code == "legacy_pinned"
        assert getattr(handle, "_closed", False) is False

        assert pool.release_path(path, force_legacy_pin=True) is True
        assert getattr(handle, "_closed", False) is True
    finally:
        pool.close_all()


def test_stale_handle_does_not_erase_an_active_lease(tmp_path: Path) -> None:
    path = _vault_path(tmp_path, "alpha")
    pool = VaultPool()
    lease = pool.lease(path)
    lease.vault.close()
    try:
        with pytest.raises(VaultPoolError) as in_use:
            pool.lease(path)
        assert in_use.value.code == "vault_in_use"
        assert pool.lease_count(path) == 1

        replacement = Vault.open(path)
        assert pool.adopt(replacement, path, pin=False) is replacement
        assert pool.lease_count(path) == 1
    finally:
        lease.release()
        pool.close_all()


def test_fenced_replacement_is_installed_before_new_leases(tmp_path: Path) -> None:
    path = _vault_path(tmp_path, "alpha")
    pool = VaultPool()
    original = pool.lease(path)
    original.release()
    pool.fence(path)
    assert pool.release_path(path, require_fenced=True) is True

    ownership = pool.claim_fenced_ownership(path)
    replacement = Vault.open(path)
    try:
        assert pool.install_fenced(replacement, path, ownership=ownership) is replacement
        ownership = None
        assert pool.peek(path) is replacement
        with pytest.raises(VaultPoolError) as fenced:
            pool.lease(path)
        assert fenced.value.code == "vault_fenced"

        pool.unfence(path)
        with pool.lease(path) as borrowed:
            assert borrowed is replacement
    finally:
        if ownership is not None:
            ownership.release()
        pool.close_all()


def test_fenced_replacement_requires_prior_release(tmp_path: Path) -> None:
    path = _vault_path(tmp_path, "alpha")
    pool = VaultPool()
    lease = pool.lease(path)
    try:
        pool.fence(path)
        with pytest.raises(VaultPoolError) as active:
            pool.claim_fenced_ownership(path)
        assert active.value.code == "vault_in_use"
        assert pool.lease_count(path) == 1
    finally:
        lease.release()
        pool.close_all()


def test_stale_open_handle_cannot_verify_or_write_after_raw_external_swap(
    tmp_path: Path,
) -> None:
    live_path = _vault_path(tmp_path, "live")
    replacement_path = _vault_path(tmp_path, "replacement")
    replacement_identity = schema.read_graph_identity_path(replacement_path / "graph.lbug")
    state = ServerState(vault=Vault.open(live_path), vault_path=live_path)
    runtime = state.active_runtime
    assert runtime is not None
    stale = runtime.vault
    stale_generation = stale.store._graph_handle.graph_generation  # noqa: SLF001
    assert replacement_identity.graph_generation != stale_generation

    try:
        # Deliberately bypass the lease API to model a legacy/external rename.
        # The open handle still points to the old inode while the path names a
        # different valid graph generation.
        os.replace(replacement_path / "graph.lbug", live_path / "graph.lbug")
        write_integrity_state(
            live_path,
            GraphIntegrityState(
                status=AuditStatus.UNVERIFIED,
                graph_generation=replacement_identity.graph_generation,
                writer_fenced=True,
                reason="external swap has not been audited by this process",
            ),
        )

        integrity, result = graph_integrity.run_audit(
            runtime,
            stale,
            preserve_terminal_fence=False,
        )

        assert result is None
        assert integrity.status is AuditStatus.UNVERIFIED
        assert integrity.writer_fenced is True
        assert integrity.graph_generation == replacement_identity.graph_generation
        assert "different on-disk generation" in str(integrity.reason)
        assert load_integrity_state(live_path) == integrity
        with pytest.raises(IntegrityFenceError, match="integrity_fenced"):
            graph_integrity.require_write_allowed(runtime, stale)
    finally:
        state.close()
