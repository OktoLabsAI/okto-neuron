"""Daemon-side hooks of the per-vault writer lease (#21).

Real ``ServerState`` / ``VaultPool`` / ``Vault`` on scratch vaults. A second
process (a bare interpreter holding the lease, like a CLI would) stands in for
the contending writer.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_neuron.server import http as http_mod
from okto_neuron.server._vault_pool import VaultPool, VaultPoolError
from okto_neuron.server.state import ServerState
from okto_neuron.store import writer_lease as wl
from okto_neuron.store.writer_lease import (
    WriterLeaseHeld,
    acquire_writer_lease,
    held_writer_lease,
    release_all,
    writer_guard,
)
from okto_neuron.vault import Vault

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX flock semantics")

_HOLDER = textwrap.dedent(
    """
    import sys
    from okto_neuron.store.writer_lease import acquire_writer_lease
    lease = acquire_writer_lease(sys.argv[1], role="cli", operation="kg rebuild")
    print("ready", flush=True)
    sys.stdin.read()
    """
)


@pytest.fixture(autouse=True)
def _clean_registry():
    release_all()
    yield
    release_all()


def _spawn_holder(vault: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(vault)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "ready"
    return proc


def _stop(proc: subprocess.Popen) -> None:
    assert proc.stdin is not None
    proc.stdin.close()
    proc.wait(timeout=10)
    if proc.stdout:
        proc.stdout.close()


def _foreign_acquire(path: Path) -> None:
    """Try to take the lease as another process would; raises WriterLeaseHeld."""
    code = (
        "import sys\n"
        "from okto_neuron.store.writer_lease import acquire_writer_lease, WriterLeaseHeld\n"
        "try:\n"
        "    acquire_writer_lease(sys.argv[1], role='cli', operation='probe')\n"
        "except WriterLeaseHeld:\n"
        "    sys.exit(5)\n"
    )
    rc = subprocess.run([sys.executable, "-c", code, str(path)], timeout=30).returncode
    if rc == 5:
        raise WriterLeaseHeld(path, wl.WriterLeaseHolder())
    assert rc == 0


def _new_vault(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    Vault.open(path).close()
    return path


def _state() -> ServerState:
    return ServerState(vault=None, vault_path=None, multi_vault_runtime_enabled=True)


def test_runtime_for_holds_the_lease_until_close_then_releases(tmp_path: Path) -> None:
    path = _new_vault(tmp_path, "a")
    state = _state()
    state.runtime_for(path)
    assert held_writer_lease(path) is not None
    with pytest.raises(WriterLeaseHeld):
        _foreign_acquire(path)
    state.close()
    assert held_writer_lease(path) is None
    _foreign_acquire(path)  # free again


def test_lease_survives_idle_eviction(tmp_path: Path) -> None:
    a, b = _new_vault(tmp_path, "a"), _new_vault(tmp_path, "b")
    pool = VaultPool()
    pool.max_open = 1
    with pool.lease(a):
        pass
    with pool.lease(b):  # evicts the idle handle for ``a``
        pass
    assert pool.peek(a) is None
    assert held_writer_lease(a) is not None
    with pytest.raises(WriterLeaseHeld):
        _foreign_acquire(a)
    pool.close_all()


def test_busy_vault_is_vault_busy_with_holder_pid(tmp_path: Path) -> None:
    path = _new_vault(tmp_path, "busy")
    holder = _spawn_holder(path)
    try:
        state = _state()
        with pytest.raises(VaultPoolError) as excinfo:
            state.runtime_for(path)
        assert excinfo.value.code == "vault_busy"
        assert str(holder.pid) in str(excinfo.value)
        assert "kg rebuild" in str(excinfo.value)
        assert state._vault_runtimes == {}
        with pytest.raises(VaultPoolError) as pool_exc:  # defensive pool hook
            state.vault_pool.get_or_open(path)
        assert pool_exc.value.code == "vault_busy"
        # Once the CLI is done the same state serves the vault.
        _stop(holder)
        holder = None
        state.runtime_for(path)
        state.close()
    finally:
        if holder is not None:
            _stop(holder)


def test_busy_vault_does_not_break_discovery_of_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    busy, ok = _new_vault(tmp_path, "busy"), _new_vault(tmp_path, "ok")
    monkeypatch.setattr(
        "okto_neuron.vault_registry.list_vaults",
        lambda *a, **k: [SimpleNamespace(path=busy), SimpleNamespace(path=ok)],
    )
    holder = _spawn_holder(busy)
    state = _state()
    try:
        with caplog.at_level("WARNING"):
            runtimes = state.runtimes(discover=True)
            state.runtimes(discover=True)
        assert [r.vault_path for r in runtimes] == [ok.resolve()]
        assert caplog.text.count("skipping busy vault busy") == 1  # logged once
        _stop(holder)
        holder = None
        assert {r.vault_path for r in state.runtimes(discover=True)} == {
            busy.resolve(),
            ok.resolve(),
        }
    finally:
        if holder is not None:
            _stop(holder)
        state.close()


def test_release_happens_after_store_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _new_vault(tmp_path, "order")
    state = _state()
    state.runtime_for(path)
    order: list[str] = []
    real_close_all = state.vault_pool.close_all

    def close_all() -> None:
        real_close_all()
        order.append(f"store_closed lease_held={held_writer_lease(path) is not None}")

    monkeypatch.setattr(state.vault_pool, "close_all", close_all)
    monkeypatch.setattr(
        "okto_neuron.server.state.release_all_writer_leases",
        lambda: (order.append("lease_release"), release_all()),
    )
    state.close()
    assert order == ["store_closed lease_held=True", "lease_release"]


def test_in_process_writers_are_not_blocked(tmp_path: Path) -> None:
    path = _new_vault(tmp_path, "inproc")
    state = _state()
    state.runtime_for(path)
    lease = held_writer_lease(path)
    assert lease is not None
    with writer_guard(path, "kg_reembed", role="daemon") as inner:
        assert inner is lease
    assert held_writer_lease(path) is lease and lease.held  # guard never released it
    assert acquire_writer_lease(path, role="daemon", operation="mcp remember") is lease
    state.close()


def test_degraded_filesystem_keeps_serving_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = _new_vault(tmp_path, "smb")

    def no_flock(fd: int) -> bool:
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr("okto_neuron.server.lifecycle.try_lock_fd", no_flock)
    state = _state()
    with caplog.at_level("WARNING"):
        runtime = state.runtime_for(path)
    assert runtime.vault_path == path.resolve()
    assert "DEGRADED" in caplog.text and str(path.resolve()) in caplog.text
    payload = http_mod._status_payload(state)
    reasons = [r for r in payload.get("degraded_reasons", []) if r.startswith("writer_lease_degraded")]
    assert len(reasons) == 1
    assert "smb" in reasons[0] and "file locking is unavailable" in reasons[0]
    state.close()
