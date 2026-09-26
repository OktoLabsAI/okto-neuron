from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from okto_neuron import Vault
from okto_neuron.errors import (
    ConfigParseError,
    VaultClosedError,
    VaultLockedError,
    VaultNotFoundError,
)
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture(autouse=True)
def close_vault_handles() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def test_ts_f295d0b1_open_missing_vault_raises_not_found(tmp_path: Path) -> None:
    with pytest.raises(VaultNotFoundError):
        Vault.open(tmp_path / "missing")


def test_ts_f295d0b1_open_surfaces_bootstrap_lock_contention(tmp_path: Path) -> None:
    vault_path = tmp_path / "v"
    lock_path = vault_path / ".marginalia" / ".bootstrap.lock"
    lock_path.parent.mkdir(parents=True)
    proc = _start_lock_holder(lock_path)

    try:
        with pytest.raises(VaultLockedError):
            Vault.open(vault_path)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_ts_9b83a2a8_init_creates_vault_dir_at_0700(tmp_path: Path) -> None:
    vault_path = tmp_path / "v"
    vault = Vault.init(vault_path)

    try:
        assert vault_path.is_dir()
        assert stat.S_IMODE(os.stat(vault_path).st_mode) == 0o700
    finally:
        vault.close()


def test_ts_171e8093_context_manager_closes_after_exception_and_releases_lock(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "v"
    initialized = Vault.init(vault_path)
    initialized.close()

    vault: Vault | None = None
    with pytest.raises(RuntimeError, match="boom"):
        with Vault.open(vault_path) as vault:
            raise RuntimeError("boom")

    assert vault is not None
    with pytest.raises(VaultClosedError):
        vault.add(vault_path / "notes" / "closed.md")

    reopened = Vault.open(vault_path)
    try:
        assert reopened.path == vault_path.resolve(strict=False)
    finally:
        reopened.close()


def test_explicit_embedding_misconfiguration_never_falls_back_to_fastembed(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "configured"
    vault_path.mkdir()
    (vault_path / "okto-neuron.yaml").write_text(
        yaml.safe_dump(
            {
                "marginalia_yaml_version": 1,
                "embedding": {
                    "provider": "not-a-provider",
                    "model": "example",
                    "dimension": 2,
                },
            }
        ),
        encoding="utf-8",
    )
    vault = Vault(vault_path, object())

    with pytest.raises(ConfigParseError) as raised:
        _ = vault.embedder

    assert raised.value.cause is not None
    assert "unknown embedding provider" in str(raised.value.cause)


def test_live_store_vault_wrappers_share_integrity_boundary(tmp_path: Path) -> None:
    class _MutableLiveStore:
        def __init__(self) -> None:
            self.vault_path = tmp_path
            self._graph_handle = object()

    store = _MutableLiveStore()
    first = Vault(tmp_path, store)
    second = Vault(tmp_path, store)

    assert first._integrity_sync_lock is second._integrity_sync_lock
    assert first._integrity_guard_local is second._integrity_guard_local


def test_immutable_live_store_without_shared_integrity_boundary_fails_closed(
    tmp_path: Path,
) -> None:
    class _ImmutableLiveStore:
        __slots__ = ("vault_path", "_graph_handle")

        def __init__(self) -> None:
            self.vault_path = tmp_path
            self._graph_handle = object()

    with pytest.raises(TypeError, match="shared integrity synchronization"):
        Vault(tmp_path, _ImmutableLiveStore())


def test_hot_embedding_dimension_change_fails_before_graph_write(tmp_path: Path) -> None:
    """An open 384-wide graph rejects a freshly configured 2560 embedder."""
    from types import SimpleNamespace

    from okto_neuron.embed import StubEmbedder
    from okto_neuron.errors import EmbeddingDimMismatch

    class _Store:
        _graph_handle = SimpleNamespace(embedding_dim=384)

        def add_node(self, _node: object) -> None:
            pytest.fail("dimension mismatch reached a graph write")

    vault = Vault(tmp_path, _Store(), embedder=StubEmbedder(dim=2560))

    with pytest.raises(EmbeddingDimMismatch) as raised:
        _ = vault.embedder

    assert raised.value.stored_dim == 384
    assert raised.value.configured_dim == 2560
    assert "kg reembed" in str(raised.value)


def _start_lock_holder(lock_path: Path) -> subprocess.Popen[str]:
    script = """
from __future__ import annotations

import fcntl
import os
import pathlib
import sys
import time

path = pathlib.Path(sys.argv[1])
with path.open("a+", encoding="utf-8") as lock_file:
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(str(os.getpid()))
    lock_file.flush()
    os.fsync(lock_file.fileno())
    print("ready", flush=True)
    time.sleep(30)
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(lock_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    return proc
