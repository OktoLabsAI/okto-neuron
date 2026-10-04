"""Per-vault writer lease: contention, stale reclaim, re-entrancy, identity."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from okto_neuron.store import writer_lease as wl
from okto_neuron.store.writer_lease import (
    LEASE_FILENAME,
    WriterLeaseHeld,
    acquire_writer_lease,
    held_writer_lease,
    release_all,
    writer_guard,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX flock semantics")

_HOLDER = textwrap.dedent(
    """
    import sys
    from okto_neuron.store.writer_lease import acquire_writer_lease
    lease = acquire_writer_lease(sys.argv[1], role="daemon", operation="serve",
                                 endpoint="http://127.0.0.1:1")
    print("ready", flush=True)
    sys.stdin.read()
    """
)


@pytest.fixture(autouse=True)
def _clean_registry():
    release_all()
    yield
    release_all()


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    path = tmp_path / "vault"
    path.mkdir()
    return path


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


def _stop(proc: subprocess.Popen, *, kill: bool = False) -> None:
    if kill:
        proc.kill()
    else:
        assert proc.stdin is not None
        proc.stdin.close()
    proc.wait(timeout=10)
    if proc.stdout:
        proc.stdout.close()


def test_contention_names_the_verified_holder(vault: Path) -> None:
    proc = _spawn_holder(vault)
    try:
        with pytest.raises(WriterLeaseHeld) as excinfo:
            acquire_writer_lease(vault, role="cli", operation="kg rebuild")
        holder = excinfo.value.holder
        assert holder.pid == proc.pid
        assert holder.verified is True
        assert holder.role == "daemon"
        assert holder.endpoint == "http://127.0.0.1:1"
        assert excinfo.value.EXIT_CODE == 5
        assert str(proc.pid) in excinfo.value.user_message()
    finally:
        _stop(proc)


def test_stale_reclaim_after_holder_is_killed(vault: Path, caplog: pytest.LogCaptureFixture) -> None:
    proc = _spawn_holder(vault)
    dead_pid = proc.pid
    _stop(proc, kill=True)
    with caplog.at_level("WARNING", logger="okto_neuron.store.writer_lease"):
        lease = acquire_writer_lease(vault, role="cli", operation="init")
    assert lease.held
    record = json.loads((vault / LEASE_FILENAME).read_text().splitlines()[0])
    assert record["pid"] == os.getpid()
    assert any(getattr(r, "event", "") == "writer_lease.stale_reclaimed" for r in caplog.records)
    assert str(dead_pid) in caplog.text


def test_in_process_reacquire_and_guard_pass_through(vault: Path) -> None:
    daemon = acquire_writer_lease(vault, role="daemon", operation="serve")
    assert acquire_writer_lease(vault, role="cli", operation="kg reembed") is daemon
    assert held_writer_lease(vault) is daemon
    with writer_guard(vault, "kg reembed") as inner:
        assert inner is daemon
    # A pass-through guard never releases the daemon's lease.
    assert daemon.held
    assert held_writer_lease(vault) is daemon


def test_guard_takes_and_releases_when_not_held(vault: Path) -> None:
    with writer_guard(vault, "snapshot load") as lease:
        assert lease.held
        assert held_writer_lease(vault) is lease
    assert held_writer_lease(vault) is None
    # Released: another acquisition succeeds at once.
    acquire_writer_lease(vault, role="cli", operation="again")


def test_mangled_start_token_is_still_refused_as_unverifiable(vault: Path) -> None:
    proc = _spawn_holder(vault)
    try:
        path = vault / LEASE_FILENAME
        record = json.loads(path.read_text().splitlines()[0])
        record["start_token"] = "posix:not the real birth time"
        path.write_text(json.dumps(record) + "\n")
        with pytest.raises(WriterLeaseHeld) as excinfo:
            acquire_writer_lease(vault, role="cli", operation="watch")
        assert excinfo.value.holder.verified is False
        assert "unverifiable holder" in excinfo.value.message
    finally:
        _stop(proc)


def test_unreadable_record_is_refused_as_unverifiable(vault: Path) -> None:
    proc = _spawn_holder(vault)
    try:
        (vault / LEASE_FILENAME).write_text("not json\n")
        with pytest.raises(WriterLeaseHeld) as excinfo:
            acquire_writer_lease(vault, role="cli", operation="watch")
        assert excinfo.value.holder.pid is None
        assert "unverifiable holder" in excinfo.value.message
    finally:
        _stop(proc)


def test_pid_reuse_record_does_not_block_a_free_lock(vault: Path) -> None:
    """A record naming a live, unrelated pid with its real start token is stale."""
    from okto_neuron.server.lifecycle import process_start_token

    parent = os.getppid()
    token = process_start_token(parent)
    assert token
    (vault / LEASE_FILENAME).write_text(
        json.dumps({"version": 1, "pid": parent, "start_token": token, "role": "daemon"}) + "\n"
    )
    lease = acquire_writer_lease(vault, role="cli", operation="init")
    assert lease.held
    assert json.loads((vault / LEASE_FILENAME).read_text().splitlines()[0])["pid"] == os.getpid()


def test_symlink_variants_share_one_lease(tmp_path: Path, vault: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(vault, target_is_directory=True)
    first = acquire_writer_lease(vault, role="daemon", operation="serve")
    assert acquire_writer_lease(link, role="cli", operation="watch") is first
    assert acquire_writer_lease(link / ".." / "vault", role="cli", operation="watch") is first


def test_case_variant_path_is_one_lease_on_a_case_insensitive_filesystem(
    tmp_path: Path, vault: Path
) -> None:
    variant = vault.parent / vault.name.upper()
    if not variant.exists():
        pytest.skip("case-sensitive filesystem")
    first = acquire_writer_lease(vault, role="daemon", operation="serve")
    assert acquire_writer_lease(variant, role="cli", operation="watch") is first


def test_contention_across_symlink_from_another_process(tmp_path: Path, vault: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(vault, target_is_directory=True)
    proc = _spawn_holder(vault)
    try:
        with pytest.raises(WriterLeaseHeld):
            acquire_writer_lease(link, role="cli", operation="watch")
    finally:
        _stop(proc)


def test_unsupported_filesystem_degrades_loudly(
    vault: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import errno

    def no_flock(fd: int) -> bool:
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr("okto_neuron.server.lifecycle.try_lock_fd", no_flock)
    with caplog.at_level("WARNING", logger="okto_neuron.store.writer_lease"):
        lease = acquire_writer_lease(vault, role="daemon", operation="serve")
    assert lease.degraded and "file locking is unavailable" in lease.degraded
    assert str(vault.resolve()) in caplog.text
    assert vault.resolve() in wl.degraded_leases()
    release_all()
    assert wl.degraded_leases() == {}


def test_leftovers_from_0_3_1_are_ignored(vault: Path) -> None:
    (vault / ".graph-handle.lock").write_text("123:abc")
    (vault / ".marginalia").mkdir()
    (vault / ".marginalia" / "reembed.state.json").write_text("{}")
    lease = acquire_writer_lease(vault, role="daemon", operation="serve")
    assert lease.held
    assert (vault / ".graph-handle.lock").read_text() == "123:abc"
