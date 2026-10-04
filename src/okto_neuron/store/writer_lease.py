"""Per-vault writer lease: one process at a time may write a vault.

The application daemon holds this lease for every vault it serves, for the life
of the process. A CLI command that writes the same vault either takes the lease
itself (and so refuses while the daemon holds it) or refuses up front with the
API/UI equivalent. Readers take no lease.

The lease is the kernel-held ``flock`` on ``<vault>/.okto-neuron-writer.lock``
(Windows: ``msvcrt.locking``), so it dies with its holder and a crash never
leaves a stale lock. The file is never unlinked, and its one JSON line
(``pid``, ``start_token``, ``role``, ``operation``, ``endpoint``,
``acquired_at``) is diagnostic: a holder that cannot be verified is still
honoured, because a live kernel lock is the only proof that matters. A dead
holder's record never blocks a new one; if the lock is free the old record is
overwritten (``writer_lease.stale_reclaimed``), even when its pid was recycled.

Inside one process the lease is re-entrant through a registry keyed by the lock
file's ``(st_dev, st_ino)``, so the daemon's own jobs (an in-process reembed, the
folder watcher, an MCP ``remember``) pass through :func:`writer_guard` without
contending with themselves, and symlink or case variants of a path collapse to
one lease.

Lock order: writer lease, then ``.graph-handle.lock``, then engine locks.

On a filesystem without working ``flock`` (some NFS/SMB mounts) the lease runs
degraded: nothing excludes a second writer, and :func:`degraded_leases` exposes
the reason so ``/health`` and the startup log can say so loudly.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from okto_neuron.errors import VaultLockHeld

_LOG = logging.getLogger("okto_neuron.store.writer_lease")

LEASE_FILENAME = ".okto-neuron-writer.lock"
RECORD_VERSION = 1
_UNSUPPORTED_ERRNOS = {
    code
    for code in (
        getattr(errno, "ENOTSUP", None),
        getattr(errno, "EOPNOTSUPP", None),
        getattr(errno, "ENOLCK", None),
        getattr(errno, "ENOSYS", None),
    )
    if code is not None
}
_RECORD_READ_ATTEMPTS = 5
_RECORD_READ_RETRY_S = 0.04
_OPEN_ATTEMPTS = 10


def lease_path(vault_path: Path | str) -> Path:
    return Path(vault_path).expanduser().resolve(strict=False) / LEASE_FILENAME


@dataclass(frozen=True)
class WriterLeaseHolder:
    """What the lease file says about the process that holds it."""

    pid: int | None = None
    start_token: str | None = None
    role: str | None = None
    operation: str | None = None
    endpoint: str | None = None
    acquired_at: str | None = None
    verified: bool = False
    """True only when the recorded pid is alive with the recorded start token."""


class WriterLeaseHeld(VaultLockHeld):
    """Another process holds this vault's writer lease (exit code 5)."""

    default_message = "another process holds this vault's writer lease"

    def __init__(
        self,
        vault_path: Path | str,
        holder: WriterLeaseHolder,
        *,
        message: str | None = None,
        remedy: str | None = None,
    ) -> None:
        self.holder = holder
        self.remedy = remedy
        super().__init__(
            vault_path,
            holding_pid=holder.pid if holder.verified else None,
            message=message or _describe_holder(holder),
        )

    def user_message(self) -> str:
        lines = [self.message]
        if self.remedy:
            lines.extend(self.remedy.splitlines())
        lines.append(f"vault: {self.vault_path}")
        return "\n".join(lines)


def _describe_holder(holder: WriterLeaseHolder) -> str:
    what = f"{holder.role or 'process'} pid {holder.pid}" if holder.pid else "a process"
    if holder.operation:
        what += f" ({holder.operation})"
    if holder.verified:
        return f"this vault is being written by {what}"
    return (
        f"this vault's writer lease is held by an unverifiable holder (the lease file "
        f"names {what}, but that identity could not be confirmed); refusing because "
        "the OS lock is still held"
    )


@dataclass
class WriterLease:
    path: Path
    """The lock file."""
    role: str
    operation: str
    endpoint: str | None = None
    degraded: str | None = None
    """Reason the OS lock could not be taken (unsupported filesystem), else ``None``."""
    _fd: int | None = field(default=None, repr=False)
    _identity: tuple[int, int] | None = field(default=None, repr=False)

    @property
    def held(self) -> bool:
        return self._identity is not None and _REGISTRY.get(self._identity) is self

    def release(self) -> None:
        """Release the OS lock and forget the lease (idempotent)."""
        with _REGISTRY_LOCK:
            identity, self._identity = self._identity, None
            if identity is not None and _REGISTRY.get(identity) is self:
                del _REGISTRY[identity]
            fd, self._fd = self._fd, None
        if fd is not None:
            from okto_neuron.server.lifecycle import unlock_fd

            unlock_fd(fd)
            with contextlib.suppress(OSError):
                os.close(fd)

    def __enter__(self) -> "WriterLease":
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


_REGISTRY: dict[tuple[int, int], WriterLease] = {}
_REGISTRY_LOCK = threading.RLock()


def _identity_of(fd: int) -> tuple[int, int]:
    st = os.fstat(fd)
    return (st.st_dev, st.st_ino)


def _read_record(fd: int) -> dict[str, Any] | None:
    from okto_neuron.server.lifecycle import LOCK_RECORD_LIMIT

    for attempt in range(_RECORD_READ_ATTEMPTS):
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, LOCK_RECORD_LIMIT).decode("utf-8", errors="replace").strip()
        if raw:
            try:
                payload = json.loads(raw.splitlines()[0])
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                return payload
        # The holder truncates then rewrites; give it a moment before giving up.
        if attempt + 1 < _RECORD_READ_ATTEMPTS:
            time.sleep(_RECORD_READ_RETRY_S)
    return None


def _holder_from_record(record: dict[str, Any] | None) -> WriterLeaseHolder:
    from okto_neuron.server.lifecycle import process_start_token

    if record is None:
        return WriterLeaseHolder()
    try:
        pid = int(record.get("pid"))
    except (TypeError, ValueError):
        pid = None
    token = record.get("start_token")
    token = token if isinstance(token, str) and token else None
    verified = False
    if pid is not None and pid > 0 and token is not None:
        verified = process_start_token(pid) == token
    return WriterLeaseHolder(
        pid=pid if pid and pid > 0 else None,
        start_token=token,
        role=str(record["role"]) if record.get("role") else None,
        operation=str(record["operation"]) if record.get("operation") else None,
        endpoint=str(record["endpoint"]) if record.get("endpoint") else None,
        acquired_at=str(record["acquired_at"]) if record.get("acquired_at") else None,
        verified=verified,
    )


def _write_record(fd: int, *, role: str, operation: str, endpoint: str | None) -> None:
    from okto_neuron.server.lifecycle import process_start_token

    payload = json.dumps(
        {
            "version": RECORD_VERSION,
            "pid": os.getpid(),
            "start_token": process_start_token(os.getpid()) or "",
            "owner_id": f"{os.getpid()}:{time.time_ns()}",
            "role": role,
            "operation": operation,
            "endpoint": endpoint,
            "acquired_at": datetime.now(timezone.utc).isoformat(),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    view = memoryview(payload)
    while view:
        view = view[os.write(fd, view) :]
    os.fsync(fd)


def held_writer_lease(vault_path: Path | str) -> WriterLease | None:
    """The lease this process already holds for ``vault_path``, if any."""
    path = lease_path(vault_path)
    try:
        st = path.stat()
    except OSError:
        return None
    with _REGISTRY_LOCK:
        return _REGISTRY.get((st.st_dev, st.st_ino))


def acquire_writer_lease(
    vault_path: Path | str,
    *,
    role: str,
    operation: str,
    endpoint: str | None = None,
) -> WriterLease:
    """Take ``vault_path``'s writer lease without waiting.

    Returns the lease this process already holds if there is one (re-entrant, so
    do not release a lease you did not take: use :func:`writer_guard`). Raises
    :class:`WriterLeaseHeld` when another process holds it, whether or not that
    process can be verified. On a filesystem with no working ``flock`` returns a
    degraded lease instead (see module docstring).
    """
    from okto_neuron.server.lifecycle import try_lock_fd, unlock_fd

    resolved = Path(vault_path).expanduser().resolve(strict=False)
    path = resolved / LEASE_FILENAME
    resolved.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    for _ in range(_OPEN_ATTEMPTS):
        fd = os.open(path, flags, 0o600)
        keep = False  # True once a lease owns ``fd``
        try:
            identity = _identity_of(fd)
            with _REGISTRY_LOCK:
                existing = _REGISTRY.get(identity)
                if existing is not None:
                    return existing
                try:
                    locked = try_lock_fd(fd)
                except OSError as exc:
                    if exc.errno not in _UNSUPPORTED_ERRNOS:
                        raise
                    keep = True
                    return _register_degraded(fd, path, identity, exc, role, operation, endpoint)
                if not locked:
                    raise WriterLeaseHeld(resolved, _holder_from_record(_read_record(fd)))
                # The file could have been replaced between open and lock (a
                # vault deleted and recreated); a lock on an orphan proves nothing.
                try:
                    current = path.stat()
                except OSError:
                    current = None
                if current is None or (current.st_dev, current.st_ino) != identity:
                    unlock_fd(fd)
                    continue
                previous = _read_record_once(fd)
                _write_record(fd, role=role, operation=operation, endpoint=endpoint)
                lease = WriterLease(
                    path=path,
                    role=role,
                    operation=operation,
                    endpoint=endpoint,
                    _fd=fd,
                    _identity=identity,
                )
                _REGISTRY[identity] = lease
                keep = True
                _log_reclaim(resolved, previous)
                return lease
        finally:
            if not keep:
                with contextlib.suppress(OSError):
                    os.close(fd)
    raise OSError(errno.EAGAIN, f"could not open a stable writer lease file at {path}")


def _read_record_once(fd: int) -> dict[str, Any] | None:
    from okto_neuron.server.lifecycle import LOCK_RECORD_LIMIT

    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, LOCK_RECORD_LIMIT).decode("utf-8", errors="replace").strip()
    if not raw:
        return None
    try:
        payload = json.loads(raw.splitlines()[0])
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _log_reclaim(vault: Path, previous: dict[str, Any] | None) -> None:
    if not previous:
        return
    old_pid = previous.get("pid")
    if old_pid == os.getpid():
        return
    _LOG.warning(
        "writer lease for %s reclaimed from a dead holder (pid=%s role=%s operation=%s)",
        vault,
        old_pid,
        previous.get("role"),
        previous.get("operation"),
        extra={"component": "writer_lease", "event": "writer_lease.stale_reclaimed"},
    )


_DEGRADED: dict[Path, str] = {}


def _register_degraded(
    fd: int,
    path: Path,
    identity: tuple[int, int],
    exc: OSError,
    role: str,
    operation: str,
    endpoint: str | None,
) -> WriterLease:
    vault = path.parent
    reason = (
        f"file locking is unavailable on this filesystem ({errno.errorcode.get(exc.errno, exc.errno)}: "
        f"{exc.strerror}); nothing prevents a second process from writing {vault}"
    )
    lease = WriterLease(
        path=path,
        role=role,
        operation=operation,
        endpoint=endpoint,
        degraded=reason,
        _fd=fd,
        _identity=identity,
    )
    _REGISTRY[identity] = lease
    _DEGRADED[vault] = reason
    _LOG.warning(
        "writer lease DEGRADED for vault %s: %s",
        vault,
        reason,
        extra={"component": "writer_lease", "event": "writer_lease.degraded"},
    )
    return lease


def degraded_leases() -> dict[Path, str]:
    """Vaults whose lease is running without an OS lock, with the reason."""
    with _REGISTRY_LOCK:
        live = {lease.path.parent for lease in _REGISTRY.values() if lease.degraded}
        return {vault: reason for vault, reason in _DEGRADED.items() if vault in live}


def release_all() -> None:
    """Release every lease this process holds (daemon shutdown, tests)."""
    with _REGISTRY_LOCK:
        leases = list(_REGISTRY.values())
    for lease in leases:
        lease.release()
    with _REGISTRY_LOCK:
        _DEGRADED.clear()


@contextlib.contextmanager
def writer_guard(
    vault_path: Path | str,
    operation: str,
    *,
    role: str = "cli",
    endpoint: str | None = None,
) -> Iterator[WriterLease]:
    """Hold the writer lease around a write; a no-op when this process holds it."""
    existing = held_writer_lease(vault_path)
    if existing is not None:
        yield existing
        return
    lease = acquire_writer_lease(vault_path, role=role, operation=operation, endpoint=endpoint)
    try:
        yield lease
    finally:
        lease.release()


__all__ = [
    "LEASE_FILENAME",
    "WriterLease",
    "WriterLeaseHeld",
    "WriterLeaseHolder",
    "acquire_writer_lease",
    "degraded_leases",
    "held_writer_lease",
    "lease_path",
    "release_all",
    "writer_guard",
]
