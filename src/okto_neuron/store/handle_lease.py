"""Cross-process ownership of one vault's live Ladybug graph handle.

Ladybug handles remain attached to the graph generation they opened even when
another process replaces ``graph.lbug``.  A per-vault OS lock therefore spans
the complete lifetime of every pooled handle and every standalone graph swap.
The lock file is durable metadata only; ownership is the kernel-held lock.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import IO, Final
from uuid import uuid4

from okto_neuron.errors import VaultLockHeld

try:  # pragma: no branch - exactly one backend exists on supported platforms
    import fcntl
except ImportError:  # pragma: no cover - Windows only
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no branch - exactly one backend exists on supported platforms
    import msvcrt
except ImportError:  # pragma: no cover - POSIX only
    msvcrt = None  # type: ignore[assignment]


# Keep this outside ``.marginalia``: reset intentionally replaces that entire
# directory while the ownership lock must remain attached to the same inode.
_LOCK_RELATIVE_PATH: Final = Path(".graph-handle.lock")
_CONTENTION_ERRNOS: Final = {errno.EACCES, errno.EAGAIN}


class VaultHandleLease:
    """Idempotently releasable kernel lock for one resolved vault path."""

    def __init__(self, vault_path: Path, lock_file: IO[str], owner_token: str) -> None:
        self.vault_path = vault_path
        self._lock_file: IO[str] | None = lock_file
        self._owner_token = owner_token

    @property
    def held(self) -> bool:
        return self._lock_file is not None

    def require_held(self) -> None:
        """Fail closed if ownership was released before a destructive boundary."""
        lock_file = self._lock_file
        if lock_file is None or lock_file.closed:
            raise VaultLockHeld(
                self.vault_path,
                message="graph-handle ownership was lost before graph swap",
            )
        lock_file.seek(0)
        payload = lock_file.read().strip()
        if payload != self._owner_token:
            raise VaultLockHeld(
                self.vault_path,
                message="graph-handle ownership changed before graph swap",
            )

    def release(self) -> None:
        lock_file = self._lock_file
        if lock_file is None:
            return
        self._lock_file = None
        try:
            _unlock(lock_file)
        finally:
            lock_file.close()

    def __enter__(self) -> "VaultHandleLease":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        del exc_type, exc, traceback
        self.release()


def handle_lease_path(vault_path: Path | str) -> Path:
    return Path(vault_path).expanduser().resolve(strict=False) / _LOCK_RELATIVE_PATH


def acquire_vault_handle_lease(
    vault_path: Path | str,
    *,
    operation: str,
) -> VaultHandleLease:
    """Acquire exclusive cross-process ownership without waiting.

    The operation text is diagnostic only.  Callers retain the returned object
    until the live handle is closed or the graph swap and replacement open are
    complete.
    """
    resolved = Path(vault_path).expanduser().resolve(strict=False)
    path = handle_lease_path(resolved)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.open("a+", encoding="utf-8")
    try:
        _lock_nonblocking(lock_file)
    except OSError as exc:
        holding_pid = _read_holding_pid(lock_file)
        lock_file.close()
        if exc.errno not in _CONTENTION_ERRNOS:
            raise
        raise VaultLockHeld(
            resolved,
            holding_pid=holding_pid,
            message=(
                f"cannot {operation}; another process owns a live graph handle"
                + (f" (pid {holding_pid})" if holding_pid is not None else "")
            ),
            cause=exc,
        ) from exc

    owner_token = f"{os.getpid()}:{uuid4().hex}"
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(owner_token)
    lock_file.flush()
    os.fsync(lock_file.fileno())
    return VaultHandleLease(resolved, lock_file, owner_token)


def _read_holding_pid(lock_file: IO[str]) -> int | None:
    try:
        lock_file.seek(0)
        raw = lock_file.read().strip().split(":", 1)[0]
        pid = int(raw)
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _lock_nonblocking(lock_file: IO[str]) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    if msvcrt is None:  # pragma: no cover - every supported OS has one backend
        raise OSError(errno.ENOSYS, "no graph-handle locking backend available")
    lock_file.seek(0)
    if not lock_file.read(1):
        lock_file.write("0")
        lock_file.flush()
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock(lock_file: IO[str]) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is None:  # pragma: no cover - every supported OS has one backend
        raise OSError(errno.ENOSYS, "no graph-handle locking backend available")
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


__all__ = [
    "VaultHandleLease",
    "acquire_vault_handle_lease",
    "handle_lease_path",
]
