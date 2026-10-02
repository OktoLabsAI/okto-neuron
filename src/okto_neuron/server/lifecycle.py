"""Server lifecycle primitives for ``okto-neuron serve`` / ``okto-neuron stop``.

This module owns the cross-cutting concerns that the REST + MCP surface needs
to share (see coordinating card fe117e9f):

* A PID file under the supplied lifecycle root that is created on start and
  removed on clean shutdown. The application CLI owns one root at
  ``~/.okto-neuron/runtime``; the primitives still accept an explicit root so
  older vault-scoped records can be stopped during migration.
* Idempotent start semantics: refuses to start if an existing PID file points
  at a live process; transparently reclaims stale PID files when the
  recorded process is gone.
* Structured JSON logs on stdout with a fixed envelope
  (``ts, level, component, vault, request_id, event, msg``) so log lines can
  be parsed by harnesses without ad-hoc regex.
* ``daemonize()`` helper used by ``serve --daemon`` to detach into the
  background while keeping the PID file accurate.

The HTTP/MCP server card (fe117e9f) imports the public surface re-exported
from :mod:`okto_neuron.server` so all transports go through one lifecycle
implementation.
"""

from __future__ import annotations

import contextlib
import errno
import http.client
import json
import logging
import logging.handlers
import os
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from okto_neuron._compat import (
    CLI_MODULE_NAMES,
    app_home as default_app_home,
    getenv as _compat_getenv,
    is_console_script,
    version_from_payload,
)


PID_RELATIVE = Path(".marginalia") / "server.pid"
"""PID file location relative to a lifecycle root."""

SIGNAL_RELATIVE = Path(".marginalia") / "server.signal"
"""Instance-targeted stop request consumed by the PID-file owner."""

SHUTDOWN_OUTCOME_CAPABILITY = "shutdown_outcome"
"""Advertised in the PID record by a daemon that writes ``server.outcome`` on every clean
stop. ``stop`` reads it from the record (still on disk after the daemon exited), so a
0.3.1 daemon, which never writes the file, is told apart from a new one that crashed."""

OUTCOME_RELATIVE = Path(".marginalia") / "server.outcome"
"""Final status a daemon leaves when its store close was skipped (read once by ``stop``)."""

PID_RECORD_VERSION = 1
_PID_FILE_LIMIT = 16 * 1024
_PID_LOCK_OFFSET = _PID_FILE_LIMIT
_PID_ACQUIRE_ATTEMPTS = 20
_PROCESS_START_TOKEN_ATTEMPTS = 3
_PROCESS_START_TOKEN_RETRY_SECONDS = 0.05
_SIGNAL_POLL_SECONDS = 0.05
# Windows refuses to delete or replace a file while any process (including this
# one) holds an open handle without FILE_SHARE_DELETE, which is how CRT
# ``os.open`` opens files. Short-lived readers (``okto-neuron stop`` polling the
# PID record, the stop-request watcher) can therefore make a removal fail with
# a sharing violation for a few milliseconds; retry briefly before giving up.
_REMOVE_ATTEMPTS = 20
_REMOVE_RETRY_SECONDS = 0.05

_LOG = logging.getLogger("okto_neuron.server.lifecycle")

# Default log envelope keys, in canonical order.
_LOG_KEYS = ("ts", "level", "component", "vault", "request_id", "event", "msg")

_request_id_var: ContextVar[str | None] = ContextVar("okto_neuron_request_id", default=None)


class LifecycleError(RuntimeError):
    """Base class for server lifecycle failures."""


class StaleLockError(LifecycleError):
    """The OS lock is already owned by another live process."""

    def __init__(self, pid: int | None, path: Path) -> None:
        owner = str(pid) if pid is not None else "unknown"
        super().__init__(f"Okto Neuron server already running (pid={owner}, pid_file={path})")
        self.pid = pid
        self.path = path


def pid_file_path(root: Path) -> Path:
    """Return the canonical PID file path for a lifecycle ``root``."""
    return Path(root) / PID_RELATIVE


def signal_file_path(root: Path) -> Path:
    """Return the instance-targeted stop-request path for a lifecycle ``root``."""
    return Path(root) / SIGNAL_RELATIVE


def outcome_file_path(root: Path) -> Path:
    """Return the shutdown-outcome file path for a lifecycle ``root``."""
    return Path(root) / OUTCOME_RELATIVE


_OUTCOME_ROOT: Path | None = None


def write_shutdown_outcome(outcome: str, calls_in_flight: dict[str, int] | None = None) -> None:
    """Record how this daemon's shutdown ended (``closed`` right after the store
    close completed, ``close_skipped`` just before the hard exit).

    Best effort (the exit path must not raise): ``stop`` reads and removes the
    file and exits 0 only for ``closed``.
    """
    root = _OUTCOME_ROOT
    if root is None:
        return
    calls_in_flight = calls_in_flight or {}
    payload = {
        "outcome": outcome,
        "pid": os.getpid(),
        "calls_in_flight": sum(calls_in_flight.values()),
        "vaults": sorted(name for name, count in calls_in_flight.items() if count),
    }
    try:
        path = outcome_file_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        _LOG.warning("could not write the shutdown outcome file", exc_info=True)


def write_close_skipped_outcome(calls_in_flight: dict[str, int]) -> None:
    write_shutdown_outcome("close_skipped", calls_in_flight)


def pid_record_capabilities(root: Path) -> frozenset[str]:
    """Capabilities the daemon advertised in its PID record (empty for a legacy one)."""
    record, _legacy = _read_pid_payload(pid_file_path(root))
    return frozenset(record.capabilities) if record is not None else frozenset()


def consume_stop_outcome(root: Path, pid: int) -> dict[str, Any] | None:
    """Read and remove the outcome file left by daemon ``pid``.

    Returns the payload only when it is well-formed, says ``closed`` or
    ``close_skipped`` and belongs to ``pid``. A missing, corrupt or stale (other pid) file yields
    ``None``; any file found is removed so it cannot leak into a later stop.
    """
    path = outcome_file_path(root)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    _remove_path(path, what="shutdown-outcome file")
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("outcome") not in ("closed", "close_skipped")
        or payload.get("pid") != pid
        or not isinstance(payload.get("calls_in_flight"), int)
    ):
        return None
    return payload


@dataclass(frozen=True)
class _PidRecord:
    pid: int
    start_token: str
    owner_id: str
    version: int = PID_RECORD_VERSION
    capabilities: tuple[str, ...] = ()

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "version": self.version,
            "pid": self.pid,
            "start_token": self.start_token,
            "owner_id": self.owner_id,
        }
        if self.capabilities:
            payload["capabilities"] = list(self.capabilities)
        return (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )


def _parse_pid_record(raw: str) -> tuple[_PidRecord | None, int | None]:
    """Return ``(versioned_record, legacy_pid)`` for one PID-file payload."""
    text = raw.strip()
    if not text:
        return None, None
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        try:
            version = int(payload.get("version"))
            pid = int(payload.get("pid"))
            start_token = str(payload.get("start_token") or "")
            owner_id = str(payload.get("owner_id") or "")
        except (TypeError, ValueError):
            return None, None
        raw_caps = payload.get("capabilities")
        capabilities = (
            tuple(str(item) for item in raw_caps) if isinstance(raw_caps, list) else ()
        )
        if version == PID_RECORD_VERSION and pid > 0 and start_token and owner_id:
            return _PidRecord(
                pid=pid,
                start_token=start_token,
                owner_id=owner_id,
                version=version,
                capabilities=capabilities,
            ), None
        return None, None
    try:
        pid = int(text.splitlines()[0].strip())
    except (ValueError, IndexError):
        return None, None
    return (None, pid) if pid > 0 else (None, None)


def _linux_process_start_token(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        # ``comm`` is parenthesized and may itself contain spaces/parentheses;
        # fields after its final ')' begin at proc-stat field 3. Start time is
        # field 22, hence index 19 in this remainder.
        remainder = raw[raw.rfind(")") + 2 :].split()
        start_ticks = remainder[19]
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except (OSError, IndexError, ValueError):
        return None
    return f"linux:{boot_id}:{start_ticks}"


def _windows_process_start_token(pid: int) -> str | None:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None
        created_at = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return f"windows:{created_at}"
    finally:
        kernel32.CloseHandle(handle)


def _posix_process_start_token(pid: int) -> str | None:
    environment = os.environ.copy()
    # ``ps lstart`` is localized on macOS.  A daemon started from a Portuguese
    # login shell and stopped from a C-locale installer otherwise describes the
    # same birth time with different text and fails the identity check.
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-o", "comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = " ".join(result.stdout.split())
    if result.returncode != 0 or not value:
        return None
    return f"posix:{value}"


def _process_start_token(pid: int) -> str | None:
    """Kernel/process-table birth fingerprint used to reject PID reuse."""
    if pid <= 0:
        return None
    if os.name == "nt":
        return _windows_process_start_token(pid)
    if sys.platform.startswith("linux"):
        return _linux_process_start_token(pid)
    return _posix_process_start_token(pid)


def _process_start_token_with_retry(pid: int) -> str | None:
    """Retry only temporarily unavailable process-identity reads."""
    for attempt in range(_PROCESS_START_TOKEN_ATTEMPTS):
        start_token = _process_start_token(pid)
        if start_token is not None:
            return start_token
        if attempt + 1 < _PROCESS_START_TOKEN_ATTEMPTS:
            time.sleep(_PROCESS_START_TOKEN_RETRY_SECONDS)
    return None


def _process_alive(pid: int) -> bool:
    """Return True iff ``pid`` is a live process this user can signal."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by someone else; treat as alive.
        return True
    except OSError as exc:  # pragma: no cover - defensive
        if exc.errno == errno.ESRCH:
            return False
        return True
    return True


def _windows_process_alive(pid: int) -> bool:
    import ctypes

    process_query_limited_information = 0x1000
    still_active = 259
    access_denied = 5

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return ctypes.get_last_error() == access_denied
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _open_pid_fd(path: Path, *, create: bool) -> int:
    flags = os.O_RDWR | (os.O_CREAT if create else 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    return os.open(path, flags, 0o600)


def _try_lock_pid_fd(fd: int) -> bool:
    """Acquire the process-lifetime PID lock without blocking."""
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        # Windows byte-range locks deny reads from the locked range. Keep the
        # ownership byte immediately beyond the bounded PID payload so other
        # processes can validate the record while the daemon owns the lock.
        os.lseek(fd, _PID_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return False
            raise
        return True

    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock_pid_fd(fd: int) -> None:
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        import msvcrt

        os.lseek(fd, _PID_LOCK_OFFSET, os.SEEK_SET)
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    with contextlib.suppress(OSError):
        fcntl.flock(fd, fcntl.LOCK_UN)


# Public names for the cross-process OS-lock primitives, shared with the
# per-vault writer lease (``okto_neuron.store.writer_lease``). POSIX uses
# ``flock(LOCK_EX | LOCK_NB)``; Windows locks one byte past the bounded record so
# the record itself stays readable while the lock is held.
LOCK_RECORD_LIMIT = _PID_FILE_LIMIT


def try_lock_fd(fd: int) -> bool:
    """Take the exclusive, non-blocking OS lock on ``fd``; False on contention."""
    return _try_lock_pid_fd(fd)


def unlock_fd(fd: int) -> None:
    """Release the lock taken by :func:`try_lock_fd` (idempotent, never raises)."""
    _unlock_pid_fd(fd)


def process_start_token(pid: int) -> str | None:
    """Birth fingerprint of ``pid`` (retrying transient reads); ``None`` when unknown."""
    return _process_start_token_with_retry(pid)


def _read_pid_fd(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    return os.read(fd, _PID_FILE_LIMIT).decode("utf-8", errors="replace")


def _write_pid_fd(fd: int, record: _PidRecord) -> None:
    payload = record.to_json().encode("utf-8")
    os.lseek(fd, 0, os.SEEK_SET)
    os.ftruncate(fd, 0)
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        view = view[written:]
    os.fsync(fd)
    if hasattr(os, "fchmod"):
        with contextlib.suppress(OSError):
            os.fchmod(fd, 0o600)


def _fd_matches_path(fd: int, path: Path) -> bool:
    try:
        opened = os.fstat(fd)
        current = path.stat()
    except OSError:
        return False
    return (opened.st_dev, opened.st_ino) == (current.st_dev, current.st_ino)


def _read_pid_payload(path: Path) -> tuple[_PidRecord | None, int | None]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None, None
    except OSError:
        return None, None
    return _parse_pid_record(raw)


def _windows_file_sharing() -> bool:
    """True where an open handle blocks deleting or replacing the file."""
    return os.name == "nt"


def _remove_attempts() -> int:
    return _REMOVE_ATTEMPTS if _windows_file_sharing() else 1


def _log_remove_failure(path: Path, what: str, exc: BaseException | None) -> None:
    _LOG.warning(
        "could not remove %s %s: %s",
        what,
        path,
        exc,
        extra={"component": "lifecycle", "event": "lifecycle.remove_failed"},
    )


def _remove_path(path: Path, *, what: str) -> bool:
    """Delete ``path``; retry Windows sharing violations and log a final failure.

    Returns True when the file is gone. A failure is never silent: it is logged
    so a left-behind lifecycle file can be traced to its cause.
    """
    attempts = _remove_attempts()
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return True
        except PermissionError as exc:
            last_error = exc
        except OSError as exc:
            last_error = exc
            break
        if attempt + 1 < attempts:
            time.sleep(_REMOVE_RETRY_SECONDS)
    _log_remove_failure(path, what, last_error)
    return False


def _remove_unlocked_pid_record(path: Path, expected_raw: str) -> bool:
    """Delete an unowned PID record after the caller closed its own handle.

    Windows only. The caller cannot delete the record while holding it open, and
    once it closes that handle another daemon may claim the same file. Deleting
    is therefore conditional: the record must still be unlocked and carry the
    exact payload the caller validated. Windows sharing rules close the race
    between that check and the delete: a newcomer keeps its handle open for its
    whole lifetime, so the delete fails with a sharing violation and the next
    attempt sees the newcomer's lock and leaves its record alone.

    Returns True when the record is gone, False when it was left in place (a new
    owner took it over, or the delete kept failing, which is logged).
    """
    expected = expected_raw.strip()
    attempts = _remove_attempts()
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            if not _pid_record_unlocked_and_equal(path, expected):
                return False
            path.unlink(missing_ok=True)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            # A sharing violation (a reader has the file open) or a
            # delete-pending file refusing new opens; both are transient.
            last_error = exc
        if attempt + 1 < attempts:
            time.sleep(_REMOVE_RETRY_SECONDS)
    _log_remove_failure(path, "PID record", last_error)
    return False


def _pid_record_unlocked_and_equal(path: Path, expected: str) -> bool:
    """True when no process holds ``path``'s lock and it still holds ``expected``."""
    fd = _open_pid_fd(path, create=False)
    try:
        if not _try_lock_pid_fd(fd):
            return False
        try:
            return _read_pid_fd(fd).strip() == expected
        finally:
            _unlock_pid_fd(fd)
    finally:
        os.close(fd)


def read_pid(vault: Path) -> int | None:
    """Read either a versioned or legacy PID file; never implies ownership."""
    record, legacy_pid = _read_pid_payload(pid_file_path(vault))
    return record.pid if record is not None else legacy_pid


@dataclass(frozen=True)
class StopRequest:
    """What the last owner-bound stop request asked for (``stop --timeout/--force``)."""

    force: bool = False
    drain_timeout: float | None = None


_LAST_STOP_REQUEST: StopRequest | None = None


def last_stop_request() -> StopRequest | None:
    """The stop request the watcher most recently turned into a signal, if any.

    The signal handler reads this to size the drain budget for ``stop --timeout``
    and to tell ``stop --force`` from the first, graceful request. A plain
    SIGTERM/SIGINT (no request file) leaves it ``None``.
    """
    return _LAST_STOP_REQUEST


class PidFile:
    """Context manager that owns ``<vault>/.marginalia/server.pid``.

    Usage::

        with PidFile(vault):
            run_server()

    ``acquire`` holds an OS file lock for the process lifetime, then writes a
    versioned PID + process-birth fingerprint + random owner identity. The lock
    is the atomic single-daemon primitive; stale contents alone never block a
    replacement process after a crash.

    On ``__exit__`` the PID file is removed if it still points at us.
    """

    def __init__(self, vault: Path, *, pid: int | None = None) -> None:
        self.vault = Path(vault)
        self.path = pid_file_path(self.vault)
        self._pid = pid if pid is not None else os.getpid()
        self._owned = False
        self._fd: int | None = None
        self._record: _PidRecord | None = None
        self._watch_stop = threading.Event()
        self._watch_thread: threading.Thread | None = None

    @property
    def pid(self) -> int:
        return self._pid

    def __enter__(self) -> "PidFile":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()

    def acquire(self) -> None:
        if self._owned:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(_PID_ACQUIRE_ATTEMPTS):
            fd = _open_pid_fd(self.path, create=True)
            if not _try_lock_pid_fd(fd):
                if not _fd_matches_path(fd, self.path):
                    os.close(fd)
                    continue
                record, legacy_pid = _parse_pid_record(_read_pid_fd(fd))
                os.close(fd)
                owner_pid = record.pid if record is not None else legacy_pid
                raise StaleLockError(owner_pid, self.path)
            if not _fd_matches_path(fd, self.path):
                _unlock_pid_fd(fd)
                os.close(fd)
                continue

            # A legacy process did not hold an OS lock. Preserve upgrade safety:
            # refuse a still-live legacy PID rather than starting a second daemon,
            # but never use that unverified PID as a signal target.
            previous, legacy_pid = _parse_pid_record(_read_pid_fd(fd))
            if legacy_pid is not None and legacy_pid != self._pid and _process_alive(legacy_pid):
                _unlock_pid_fd(fd)
                os.close(fd)
                raise StaleLockError(legacy_pid, self.path)
            stale_pid = previous.pid if previous is not None else legacy_pid
            if stale_pid is not None:
                # Nobody holds the lock, so the recorded process is gone (or a
                # reused PID). Its record is stale; it is overwritten below.
                _LOG.info(
                    "reclaiming stale PID record %s (pid=%s)",
                    self.path,
                    stale_pid,
                    extra={"component": "lifecycle", "event": "lifecycle.stale_pid_reclaimed"},
                )

            start_token = _process_start_token(self._pid)
            if start_token is None:
                _unlock_pid_fd(fd)
                os.close(fd)
                raise LifecycleError(
                    f"could not determine process identity for pid {self._pid}; "
                    "refusing to create an unsafe daemon lock"
                )
            record = _PidRecord(
                pid=self._pid,
                start_token=start_token,
                owner_id=secrets.token_hex(16),
                capabilities=(
                    (SHUTDOWN_OUTCOME_CAPABILITY,) if self._pid == os.getpid() else ()
                ),
            )
            _write_pid_fd(fd, record)
            _remove_path(signal_file_path(self.vault), what="stop-request file")
            _remove_path(outcome_file_path(self.vault), what="shutdown-outcome file")
            self._fd = fd
            self._record = record
            self._owned = True
            if self._pid == os.getpid():
                global _OUTCOME_ROOT
                _OUTCOME_ROOT = self.vault
                self._start_signal_watcher()
            return
        raise LifecycleError(f"could not atomically acquire daemon lock at {self.path}")

    def _start_signal_watcher(self) -> None:
        self._watch_stop.clear()
        self._watch_thread = threading.Thread(
            target=self._watch_signal_requests,
            name="okto-neuron-stop-signal",
            daemon=True,
        )
        self._watch_thread.start()

    def _watch_signal_requests(self) -> None:
        record = self._record
        if record is None:
            return
        path = signal_file_path(self.vault)
        last_request_id: str | None = None
        delivered_first = False
        while not self._watch_stop.wait(_SIGNAL_POLL_SECONDS):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            request_id = str(payload.get("request_id") or "")
            owner_id = str(payload.get("owner_id") or "")
            try:
                requested_signal = int(payload.get("signal"))
            except (TypeError, ValueError):
                continue
            if (
                not request_id
                or request_id == last_request_id
                or owner_id != record.owner_id
                or requested_signal not in {signal.SIGTERM, signal.SIGINT}
            ):
                continue
            # The process holding the lock signals itself. No external caller
            # ever sends a signal to a numeric PID, eliminating PID-reuse kills.
            if os.getpid() != record.pid or _process_start_token(record.pid) != record.start_token:
                return
            force = payload.get("force") is True
            # Only the first request and explicit ``--force`` requests become
            # signals. A repeat graceful request must never be read by the
            # daemon as the operator's second signal (#22); a real second
            # SIGTERM/Ctrl-C still forces through the signal handler.
            if delivered_first and not force:
                last_request_id = request_id
                continue
            drain_timeout: float | None
            try:
                raw_timeout = payload.get("drain_timeout")
                drain_timeout = float(raw_timeout) if raw_timeout is not None else None
            except (TypeError, ValueError):
                drain_timeout = None
            if drain_timeout is not None and not (0.0 <= drain_timeout < 86400.0):
                drain_timeout = None
            global _LAST_STOP_REQUEST
            _LAST_STOP_REQUEST = StopRequest(force=force, drain_timeout=drain_timeout)
            delivered_first = True
            last_request_id = request_id
            # ``raise_signal`` targets this process directly (and invokes the
            # Python handler on Windows, where ``os.kill(SIGTERM)`` would call
            # TerminateProcess and skip graceful teardown).
            with contextlib.suppress(OSError):
                signal.raise_signal(requested_signal)

    def release(self) -> None:
        if not self._owned:
            return
        fd = self._fd
        record = self._record
        owned_raw: str | None = None
        try:
            self._watch_stop.set()
            watcher = self._watch_thread
            if watcher is not None and watcher is not threading.current_thread():
                watcher.join(timeout=0.5)
            _remove_path(signal_file_path(self.vault), what="stop-request file")
            if fd is not None and record is not None and _fd_matches_path(fd, self.path):
                raw = _read_pid_fd(fd)
                current, _ = _parse_pid_record(raw)
                if current is not None and current.owner_id == record.owner_id:
                    if _windows_file_sharing():
                        # Windows refuses to delete a file this process still
                        # holds open, so the delete waits until the handle is
                        # closed below (see _remove_unlocked_pid_record).
                        owned_raw = raw
                    else:
                        # POSIX: unlink while the lock is still held, so a
                        # newcomer can never claim the path in between.
                        _remove_path(self.path, what="PID record")
        finally:
            if fd is not None:
                _unlock_pid_fd(fd)
                os.close(fd)
            self._fd = None
            self._record = None
            self._watch_thread = None
            self._owned = False
        if owned_raw is not None:
            _remove_unlocked_pid_record(self.path, owned_raw)


# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def request_id(value: str) -> Iterator[str]:
    """Bind ``request_id`` for the duration of the block (contextvar-scoped)."""
    token = _request_id_var.set(value)
    try:
        yield value
    finally:
        _request_id_var.reset(token)


def current_request_id() -> str | None:
    return _request_id_var.get()


class JsonLogFormatter(logging.Formatter):
    """Emit one JSON object per log line with the Okto Neuron envelope.

    Fields:
        ts          ISO-8601 UTC timestamp with microseconds and ``Z`` suffix.
        level       logging level name (lowercased).
        component   logger name (caller controls; defaults to ``record.name``).
        vault       vault path bound at configuration time (may be ``null``).
        request_id  contextvar (or ``record.request_id``) or ``null``.
        event       short slug provided as ``extra={"event": "..."}``.
        msg         human-readable message (``record.getMessage()``).
    """

    def __init__(self, *, vault: str | None = None) -> None:
        super().__init__()
        self._vault = vault

    def format(self, record: logging.LogRecord) -> str:
        envelope: dict[str, Any] = {
            "ts": _iso_now(record.created),
            "level": record.levelname.lower(),
            "component": getattr(record, "component", record.name),
            "vault": getattr(record, "vault", self._vault),
            "request_id": getattr(record, "request_id", None) or current_request_id(),
            "event": getattr(record, "event", None),
            "msg": record.getMessage(),
        }
        if record.exc_info:
            envelope["exc_info"] = self.formatException(record.exc_info)
        # Preserve a stable key order for grep-ability.
        ordered = {k: envelope.get(k) for k in _LOG_KEYS}
        for k, v in envelope.items():
            if k not in ordered:
                ordered[k] = v
        return json.dumps(ordered, default=str, ensure_ascii=False)


def _iso_now(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}Z"


DEFAULT_LOG_ROTATE_BYTES = 10 * 1024 * 1024
DEFAULT_LOG_ROTATE_BACKUPS = 3


def default_daemon_log_path() -> Path:
    """Default log file for daemonized servers: ``~/.okto-neuron/logs/okto-neuron-serve.log``."""
    return default_app_home() / "logs" / "okto-neuron-serve.log"


def stream_is_file(stream: Any, path: Path) -> bool:
    """True when ``stream`` is an open handle on the same file as ``path`` (same device and inode).

    The detached daemon child has its stdout redirected to the log file by the parent; a second file
    handler on that file would write every record twice and rotate it away from the raw stdout fd.
    """
    try:
        a = os.fstat(stream.fileno())
        b = os.stat(Path(path).expanduser())
    except (OSError, ValueError, AttributeError):
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


class _FdFollowingRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """Rotating handler for a log file that the process's stdout/stderr (fd 1 and 2) also point at.

    The detached daemon child inherits fd 1/2 on the log file (uvicorn and tracebacks write there
    directly). A plain rotation renames the file and leaves those fds on the renamed backup, so after
    the first rollover the active file would only get this handler's records. After each rollover the
    fds that were on the old file are re-pointed at the new active file.
    """

    def doRollover(self) -> None:
        followers: list[int] = []
        if self.stream is not None:
            try:
                ident = os.fstat(self.stream.fileno())
                for fd in (1, 2):
                    st = os.fstat(fd)
                    if (st.st_dev, st.st_ino) == (ident.st_dev, ident.st_ino):
                        followers.append(fd)
            except OSError:
                followers = []
        super().doRollover()
        if followers and self.stream is None:  # delay=True leaves the new file unopened until the next emit
            self.stream = self._open()
        if self.stream is not None:
            for fd in followers:
                try:
                    os.dup2(self.stream.fileno(), fd)
                except OSError:
                    pass


def configure_logging(
    vault: Path | None,
    *,
    level: int | str = logging.INFO,
    stream: Any = None,
    log_file: Path | None = None,
    rotate_max_bytes: int = DEFAULT_LOG_ROTATE_BYTES,
    rotate_backups: int = DEFAULT_LOG_ROTATE_BACKUPS,
    logger_name: str = "okto_neuron",
    also_stream: Any = None,
    follow_std_fds: bool = False,
) -> logging.Logger:
    """Install :class:`JsonLogFormatter` on ``logger_name``.

    Writes to ``stream`` (default stdout), or — when ``log_file`` is given —
    to a size-rotated file instead (``/dev/null`` and other character devices
    are written without rotation). ``also_stream`` additionally tees every
    record to that stream (the console) when a file is used.

    A log file that cannot be opened (missing permission, read-only volume) never
    stops the server: one warning goes to stderr and the logger falls back to the
    stream handler.

    Idempotent: removes pre-existing handlers we previously installed so that
    repeated invocations (tests, daemonization re-init) do not duplicate
    output.
    """
    # OKTO_NEURON_LOG_LEVEL (e.g. DEBUG) overrides the default so an operator can
    # surface full LLM prompts + raw completions live without a code change.
    env_level = _compat_getenv("OKTO_NEURON_LOG_LEVEL", "").strip().upper()
    if env_level:
        level = env_level
    logger = logging.getLogger(logger_name)
    logger.setLevel(level)
    logger.propagate = False
    # Drop any existing JSON handlers we previously attached.
    for handler in list(logger.handlers):
        if getattr(handler, "_okto_neuron_json", False):
            logger.removeHandler(handler)
    formatter = JsonLogFormatter(vault=str(vault) if vault is not None else None)
    handlers: list[logging.Handler] = []
    if log_file is not None:
        log_file = log_file.expanduser()
        try:
            if log_file.exists() and not log_file.is_file():
                # Character devices (/dev/null) cannot be size-rotated.
                file_handler: logging.Handler = logging.FileHandler(log_file, delay=True)
            else:
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with open(log_file, "ab"):  # fail now, not on the first record
                    pass
                rotating_cls = (
                    _FdFollowingRotatingFileHandler if follow_std_fds else logging.handlers.RotatingFileHandler
                )
                file_handler = rotating_cls(
                    log_file,
                    maxBytes=rotate_max_bytes,
                    backupCount=rotate_backups,
                    delay=True,
                )
            handlers.append(file_handler)
        except OSError as exc:
            print(
                f"okto-neuron: cannot write the log file {log_file} ({type(exc).__name__}: "
                f"{exc.strerror or exc}); logging to the console only",
                file=sys.stderr,
            )
        if also_stream is not None or not handlers:
            handlers.append(
                logging.StreamHandler(also_stream if also_stream is not None else stream or sys.stdout)
            )
    else:
        handlers.append(logging.StreamHandler(stream if stream is not None else sys.stdout))
    for handler in handlers:
        handler.setFormatter(formatter)
        handler._okto_neuron_json = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    return logger


# ---------------------------------------------------------------------------
# Daemonization + stop
# ---------------------------------------------------------------------------


def daemonize(*, stdout: Path | None = None, stderr: Path | None = None) -> int:
    """Detach the server and return the spawned child's PID to the parent.

    The detached child re-runs ``okto-neuron serve`` in the foreground. Returning
    its PID lets the parent wait for readiness and print an honest, actionable
    startup summary instead of exiting with only a log-file path.

    macOS fork-safety: we deliberately do NOT use the classic in-process
    double-``fork()`` (fork-without-exec) here. A process that forks without
    exec is flagged by Darwin as fork-unsafe, so the first use of a
    CoreFoundation/XPC-backed facility in the child aborts the process via the
    kernel fork guard (EXC_GUARD / "crashed on child side of fork pre-exec").
    In practice the daemon died the moment a request triggered an outbound HTTP
    call, because the macOS proxy lookup (``_scproxy.get_proxies`` →
    ``SCDynamicStoreCopyProxiesWithOptions``) routes through SystemConfiguration
    XPC. recall/add survived (no outbound HTTP); ask/ingest hard-crashed the
    daemon with no Python traceback. fork+exec gives the child a fresh process
    image that is never fork-marked, so CF/XPC (proxies, TLS trust, ...) is safe.
    """
    return _spawn_detached_daemon(stdout=stdout, stderr=stderr)


def _spawn_detached_daemon(*, stdout: Path | None = None, stderr: Path | None = None) -> int:
    """POSIX fork+exec daemon spawn (Darwin/Linux): re-exec ``okto-neuron serve``
    in a new session with std streams redirected. ``subprocess.Popen`` performs
    fork+exec, so the child is a clean image — never fork-marked, XPC-safe."""
    if os.name != "posix":
        return _spawn_windows_daemon(stdout=stdout, stderr=stderr)

    child_args = _daemon_child_args(sys.argv[1:])
    stdout_target = Path(stdout) if stdout else Path(os.devnull)
    stderr_target = Path(stderr) if stderr else stdout_target
    stdout_target.parent.mkdir(parents=True, exist_ok=True)
    if stderr_target != stdout_target:
        stderr_target.parent.mkdir(parents=True, exist_ok=True)

    with (
        open(os.devnull, "rb", buffering=0) as stdin_fh,
        open(stdout_target, "ab", buffering=0) as stdout_fh,
    ):
        if stderr_target == stdout_target:
            proc = subprocess.Popen(
                child_args,
                stdin=stdin_fh,
                stdout=stdout_fh,
                stderr=subprocess.STDOUT,
                close_fds=True,
                start_new_session=True,
            )
            return proc.pid
        with open(stderr_target, "ab", buffering=0) as stderr_fh:
            proc = subprocess.Popen(
                child_args,
                stdin=stdin_fh,
                stdout=stdout_fh,
                stderr=stderr_fh,
                close_fds=True,
                start_new_session=True,
            )
            return proc.pid


def _spawn_windows_daemon(*, stdout: Path | None = None, stderr: Path | None = None) -> int:
    """Spawn a detached Windows child equivalent to ``serve --daemon``."""
    child_args = _daemon_child_args(sys.argv[1:])
    stdout_target = Path(stdout) if stdout else Path(os.devnull)
    stderr_target = Path(stderr) if stderr else stdout_target
    stdout_target.parent.mkdir(parents=True, exist_ok=True)
    if stderr_target != stdout_target:
        stderr_target.parent.mkdir(parents=True, exist_ok=True)

    creationflags = 0
    for flag_name in ("CREATE_NEW_PROCESS_GROUP", "DETACHED_PROCESS"):
        creationflags |= int(getattr(subprocess, flag_name, 0))

    with (
        open(os.devnull, "rb", buffering=0) as stdin_fh,
        open(stdout_target, "ab", buffering=0) as stdout_fh,
    ):
        if stderr_target == stdout_target:
            proc = subprocess.Popen(
                child_args,
                stdin=stdin_fh,
                stdout=stdout_fh,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=creationflags,
            )
            return proc.pid
        with open(stderr_target, "ab", buffering=0) as stderr_fh:
            proc = subprocess.Popen(
                child_args,
                stdin=stdin_fh,
                stdout=stdout_fh,
                stderr=stderr_fh,
                close_fds=True,
                creationflags=creationflags,
            )
            return proc.pid


def _daemon_child_args(argv: list[str]) -> list[str]:
    """Build the re-exec argv for the detached daemon: the same invocation with
    ``--daemon`` stripped so the child runs in the foreground (no re-daemonize,
    no infinite spawn). The parent owns the one browser launch after readiness;
    the detached child is always headless."""
    child = [arg for arg in argv if arg not in {"--daemon", "--open"}]
    if "--no-open" not in child:
        child.append("--no-open")
    return [sys.executable, "-m", "okto_neuron.cli", *child]


class _OwnerGone(Exception):
    pass


class _OwnerChanged(Exception):
    def __init__(self) -> None:
        super().__init__("daemon owner changed while validating; refusing to signal")


class _LegacyOwner(Exception):
    def __init__(self, pid: int) -> None:
        super().__init__(pid)
        self.pid = pid


class _IdentityUnverifiable(LifecycleError):
    """The owner's process-birth identity could not be read on this attempt.

    Raised only when the lifecycle lock is still held by the recorded owner
    (it has not exited) but a single process-table read -- e.g. ``ps`` on
    macOS -- was transiently unavailable, so the caller cannot currently
    confirm identity either way. This still fails closed like a genuine
    mismatch for callers deciding whether to *signal* a process (see
    ``test_expected_locked_owner_with_unavailable_identity_remains_fail_
    closed``), but it is distinct from :class:`LifecycleError` raised for a
    proven identity mismatch: a caller that is merely polling an
    already-signalled target may retry instead of treating it as fatal.
    """


class _IdentityMismatch(_IdentityUnverifiable):
    """The owner's birth token read differently from the one captured at stop time.

    While the lifecycle lock is still held by the recorded owner this is not a
    different process (only the lock holder can write that record): it is the
    same daemon whose process-table entry reads differently while it tears down.
    Like :class:`_IdentityUnverifiable` it fails closed for callers deciding
    whether to signal, and a caller that is only polling keeps polling.
    """


@dataclass(frozen=True)
class _LegacyTarget:
    pid: int
    start_token: str


def _active_pid_record(
    vault: Path,
    *,
    expected_owner_id: str | None = None,
    expected_start_token: str | None = None,
) -> _PidRecord:
    """Return the lock-owning daemon record or raise without trusting its PID."""
    path = pid_file_path(vault)
    for _ in range(_PID_ACQUIRE_ATTEMPTS):
        try:
            fd = _open_pid_fd(path, create=False)
        except FileNotFoundError as exc:
            raise _OwnerGone(f"no Okto Neuron server PID file at {path}") from exc
        acquired = False
        # Windows: raw payload of a stale record to delete once ``fd`` is closed.
        stale_raw: str | None = None
        try:
            if not _fd_matches_path(fd, path):
                continue
            acquired = _try_lock_pid_fd(fd)
            if acquired:
                # No process owns the new lifecycle lock. A live legacy number
                # is handed to the bounded migration proof below; it is never
                # signalled merely because the numeric PID is alive.
                raw = _read_pid_fd(fd)
                record, legacy_pid = _parse_pid_record(raw)
                if legacy_pid is not None and _process_alive(legacy_pid):
                    raise _LegacyOwner(legacy_pid)
                if _fd_matches_path(fd, path):
                    stale_raw = _remove_stale_record_locked(path, raw)
                    _remove_path(signal_file_path(vault), what="stop-request file")
                stale_pid = record.pid if record is not None else legacy_pid
                raise _OwnerGone(f"stale PID file (pid={stale_pid or 'unknown'}); removed")

            record, legacy_pid = _parse_pid_record(_read_pid_fd(fd))
            if record is None:
                owner = legacy_pid if legacy_pid is not None else "unknown"
                raise LifecycleError(
                    f"daemon lock at {path} is held by pid {owner}, but its identity "
                    "record is missing or corrupt; refusing to signal"
                )
            if expected_owner_id is not None and record.owner_id != expected_owner_id:
                raise _OwnerChanged
            current_start = _process_start_token_with_retry(record.pid)
            if expected_start_token is not None:
                if current_start is None:
                    # The owner can finish shutdown after our initial lock
                    # probe but before a comparatively slow process-table read
                    # (notably ``ps`` on macOS).  Revalidate the exact path and
                    # lock before classifying the missing token as an identity
                    # mismatch.  A still-locked path remains fail-closed.
                    if not _fd_matches_path(fd, path):
                        raise _OwnerGone(
                            f"daemon owner {record.pid} exited during identity validation"
                        )
                    acquired = _try_lock_pid_fd(fd)
                    if acquired:
                        if _fd_matches_path(fd, path):
                            stale_raw = _remove_stale_record_locked(path, _read_pid_fd(fd))
                            _remove_path(signal_file_path(vault), what="stop-request file")
                        raise _OwnerGone(f"daemon owner {record.pid} released its lifecycle lock")
                    # Lock is still held by the recorded owner (it has not
                    # exited) but the process-birth read itself was
                    # transiently unavailable. This is not proof of a PID
                    # identity change -- see _IdentityUnverifiable.
                    raise _IdentityUnverifiable(
                        f"PID identity mismatch for {record.pid}; refusing to signal an "
                        "unrelated or unverifiable process"
                    )
                if current_start != expected_start_token:
                    raise _IdentityMismatch(
                        f"PID identity mismatch for {record.pid}; refusing to signal an "
                        "unrelated or unverifiable process"
                    )
                return _PidRecord(
                    pid=record.pid,
                    start_token=current_start,
                    owner_id=record.owner_id,
                    version=record.version,
                    capabilities=record.capabilities,
                )
            if current_start == record.start_token:
                return record
            # Version 1 records created before locale normalization stored the
            # localized output of ``ps lstart``.  The OS lock and random owner id
            # still target the exact daemon, and new-format shutdown never sends
            # a numeric signal: it writes an owner-bound request that only the
            # lock holder can consume.  Accept this one safe migration only when
            # the recorded PID is still an Okto Neuron serve process, then carry
            # the caller's stable observation through subsequent stop polls.
            if (
                expected_start_token is None
                and record.start_token.startswith("posix:")
                and current_start is not None
                and current_start.startswith("posix:")
                and _legacy_serve_port(_process_command(record.pid) or []) is not None
                and _process_start_token_with_retry(record.pid) == current_start
            ):
                return _PidRecord(
                    pid=record.pid,
                    start_token=current_start,
                    owner_id=record.owner_id,
                    version=record.version,
                    capabilities=record.capabilities,
                )
            raise LifecycleError(
                f"PID identity mismatch for {record.pid}; refusing to signal an "
                "unrelated or unverifiable process"
            )
        finally:
            if acquired:
                _unlock_pid_fd(fd)
            os.close(fd)
            if stale_raw is not None:
                _remove_unlocked_pid_record(path, stale_raw)
    raise LifecycleError(f"daemon PID file changed repeatedly while inspecting {path}")


def _remove_stale_record_locked(path: Path, raw: str) -> str | None:
    """Remove a stale PID record while its lock is held by the caller.

    POSIX deletes immediately (the held lock keeps newcomers out). Windows
    cannot delete a file the caller holds open, so it returns ``raw`` for
    :func:`_remove_unlocked_pid_record` to delete after the handle is closed.
    """
    if _windows_file_sharing():
        return raw
    _remove_path(path, what="stale PID record")
    return None


def _process_command(pid: int) -> list[str] | None:
    if sys.platform.startswith("linux"):
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return None
        command = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
        return command or None
    if os.name == "nt":  # pragma: no cover - exercised on Windows CI
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}').CommandLine",
                ],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            command = shlex.split(result.stdout.strip(), posix=False)
        except ValueError:
            command = result.stdout.strip().split()
        return [part.strip('"') for part in command]
    try:
        result = subprocess.run(
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        return shlex.split(result.stdout.strip())
    except ValueError:
        return result.stdout.strip().split()


def _legacy_serve_port(command: list[str]) -> int | None:
    module_index = next(
        (
            index
            for index in range(len(command) - 1)
            if command[index] == "-m" and command[index + 1] in CLI_MODULE_NAMES
        ),
        None,
    )
    invocation_args: list[str] | None = None
    if module_index is not None:
        invocation_args = command[module_index + 2 :]
    else:
        console_index = next(
            (
                index
                for index, value in enumerate(command)
                if is_console_script(value)
            ),
            None,
        )
        if console_index is not None:
            invocation_args = command[console_index + 1 :]
    if invocation_args is None or "serve" not in invocation_args:
        return None
    serve_args = invocation_args[invocation_args.index("serve") + 1 :]
    for index, value in enumerate(serve_args):
        if value.startswith("--port="):
            raw_port = value.partition("=")[2]
        elif value == "--port" and index + 1 < len(serve_args):
            raw_port = serve_args[index + 1]
        else:
            continue
        try:
            port = int(raw_port)
        except ValueError:
            return None
        return port if 1 <= port <= 65535 else None
    return 7777


def _legacy_http_json(port: int, path: str) -> tuple[int, dict[str, Any]] | None:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.75)
    try:
        connection.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
        response = connection.getresponse()
        raw = response.read(_PID_FILE_LIMIT)
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return (response.status, payload) if isinstance(payload, dict) else None


def _validate_legacy_target(vault: Path, pid: int) -> _LegacyTarget:
    """One-release migration proof for an integer-only v0.0.39 PID file."""
    record, legacy_pid = _read_pid_payload(pid_file_path(vault))
    if record is not None or legacy_pid != pid:
        raise _OwnerChanged
    start_token = _process_start_token(pid)
    command = _process_command(pid)
    port = _legacy_serve_port(command or [])
    if start_token is None or command is None or port is None:
        raise LifecycleError(
            f"legacy PID {pid} cannot be proven to be an Okto Neuron serve process; "
            "refusing to signal it"
        )

    health_result = _legacy_http_json(port, "/health")
    version_result = _legacy_http_json(port, "/version")
    if health_result is None or version_result is None:
        raise LifecycleError(
            f"legacy PID {pid} did not provide Okto Neuron health/version identity; "
            "refusing to signal it"
        )
    health_status, health = health_result
    version_status, version = version_result
    if (
        health_status not in {200, 503}
        or version_status != 200
        or health.get("pid") != pid
        or not version_from_payload(version)
    ):
        raise LifecycleError(
            f"legacy PID {pid} health/version identity did not match; refusing to signal it"
        )

    from okto_neuron.config._app_config import default_app_home

    requested_root = Path(vault).expanduser().resolve(strict=False)
    global_runtime_root = (default_app_home() / "runtime").resolve(strict=False)
    if requested_root != global_runtime_root:
        health_vault = health.get("vault_path")
        if not isinstance(health_vault, str) or (
            Path(health_vault).expanduser().resolve(strict=False) != requested_root
        ):
            raise LifecycleError(
                f"legacy PID {pid} serves a different vault; refusing to signal it"
            )

    # Bound the network proof with a second birth check. If the process changed
    # during validation, this migration path fails closed.
    if _process_start_token(pid) != start_token:
        raise LifecycleError(f"legacy PID {pid} changed during validation; refusing to signal")
    return _LegacyTarget(pid=pid, start_token=start_token)


def _legacy_target_still_same(vault: Path, target: _LegacyTarget) -> bool:
    record, legacy_pid = _read_pid_payload(pid_file_path(vault))
    if record is not None or legacy_pid != target.pid:
        return False
    if _process_start_token(target.pid) != target.start_token:
        return False
    return _legacy_serve_port(_process_command(target.pid) or []) is not None


def _signal_legacy_target(vault: Path, target: _LegacyTarget, sig: int) -> None:
    if not _legacy_target_still_same(vault, target):
        raise _OwnerGone
    try:
        os.kill(target.pid, sig)
    except ProcessLookupError as exc:
        raise _OwnerGone from exc


def _write_signal_request(
    vault: Path,
    record: _PidRecord,
    sig: int,
    *,
    force: bool = False,
    drain_timeout: float | None = None,
) -> None:
    if sig not in {signal.SIGTERM, signal.SIGINT}:
        raise LifecycleError(f"unsupported daemon stop signal: {sig}")
    path = signal_file_path(vault)
    path.parent.mkdir(parents=True, exist_ok=True)
    request_id = secrets.token_hex(16)
    payload = (
        json.dumps(
            {
                "version": 1,
                "owner_id": record.owner_id,
                "pid": record.pid,
                "start_token": record.start_token,
                "signal": int(sig),
                "request_id": request_id,
                "force": bool(force),
                "drain_timeout": drain_timeout,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{request_id}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        attempts = _remove_attempts()
        for attempt in range(attempts):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows: the owner's watcher may be reading the previous
                # request at this instant (sharing violation). Retry briefly.
                if attempt + 1 == attempts:
                    raise
                time.sleep(_REMOVE_RETRY_SECONDS)
    finally:
        tmp.unlink(missing_ok=True)


def _request_stop(
    vault: Path,
    *,
    sig: int,
    expected_owner_id: str | None = None,
    expected_pid: int | None = None,
    expected_start_token: str | None = None,
    force: bool = False,
    drain_timeout: float | None = None,
) -> _PidRecord:
    record = _active_pid_record(
        vault,
        expected_owner_id=expected_owner_id,
        expected_start_token=expected_start_token,
    )
    if expected_pid is not None and record.pid != expected_pid:
        raise _OwnerChanged
    _write_signal_request(vault, record, sig, force=force, drain_timeout=drain_timeout)
    return record


def _request_stop_target(
    vault: Path,
    *,
    sig: int,
    expected: _PidRecord | _LegacyTarget | None = None,
    expected_pid: int | None = None,
    force: bool = False,
    drain_timeout: float | None = None,
) -> _PidRecord | _LegacyTarget:
    if isinstance(expected, _PidRecord):
        return _request_stop(
            vault,
            sig=sig,
            expected_owner_id=expected.owner_id,
            expected_pid=expected.pid,
            expected_start_token=expected.start_token,
            force=force,
            drain_timeout=drain_timeout,
        )
    if isinstance(expected, _LegacyTarget):
        _signal_legacy_target(vault, expected, _legacy_signal(sig, force))
        return expected
    try:
        return _request_stop(
            vault,
            sig=sig,
            expected_pid=expected_pid,
            force=force,
            drain_timeout=drain_timeout,
        )
    except _LegacyOwner as legacy:
        target = _validate_legacy_target(vault, legacy.pid)
        if expected_pid is not None and target.pid != expected_pid:
            raise _OwnerChanged
        _signal_legacy_target(vault, target, _legacy_signal(sig, force))
        return target


def _legacy_signal(sig: int, force: bool) -> int:
    """A pre-lock daemon has no force request: ``--force`` is SIGKILL for it."""
    return signal.SIGKILL if force and os.name == "posix" else sig


def send_stop(
    vault: Path,
    *,
    sig: int = signal.SIGTERM,
    expected_pid: int | None = None,
) -> int:
    """Request ``sig`` from the verified PID-file owner and return its PID.

    New-format daemons validate the random owner identity and process-birth
    fingerprint, then signal themselves; the caller never invokes
    ``os.kill(recorded_pid)``. The one-release legacy bridge uses a numeric
    signal only after command, health, version, vault, and birth proofs all
    agree, and rechecks the captured birth identity before escalation.
    """
    try:
        return _request_stop_target(vault, sig=sig, expected_pid=expected_pid).pid
    except (_OwnerGone, _OwnerChanged) as exc:
        raise LifecycleError(str(exc)) from exc


def active_server_pid(vault: Path) -> int | None:
    """Return the safely verified server PID for ``vault``, if one exists.

    Discovery deliberately ignores stale or unprovable PID metadata.  Explicit
    stop still reports the underlying lifecycle error, but a dead legacy file
    must not make bare ``okto-neuron stop`` claim that multiple daemons exist.
    """
    try:
        return _active_pid_record(vault).pid
    except _LegacyOwner as legacy:
        try:
            return _validate_legacy_target(vault, legacy.pid).pid
        except LifecycleError:
            return None
    except (LifecycleError, _OwnerGone, _OwnerChanged):
        return None


def close_budget(drain_timeout: float) -> float:
    """Seconds reserved after the drain budget for the store close (#22).

    ``max(5 s, 25 %)`` of the drain budget: the drain may use all of
    ``drain_timeout``, and the close then still has this long before the hard
    deadline (``drain_timeout + close_budget``).
    """
    return max(_MIN_CLOSE_BUDGET_SECONDS, _CLOSE_BUDGET_FRACTION * max(0.0, drain_timeout))


_MIN_CLOSE_BUDGET_SECONDS = 5.0
_CLOSE_BUDGET_FRACTION = 0.25
_STOP_EXIT_GRACE_SECONDS = 5.0


def stop_server(
    vault: Path,
    *,
    sig: int = signal.SIGTERM,
    timeout: float = 30.0,
    poll_interval: float = 0.1,
    force: bool = False,
) -> int:
    """Ask the server to stop once and wait for it to exit.

    Exactly ONE request is sent: ``stop`` never escalates on its own (#22). The
    request carries ``timeout`` as the daemon's drain budget; the daemon then
    has a further :func:`close_budget` to close its stores, so this waits up to
    ``timeout`` plus that budget plus a small exit grace. Success is the owner
    releasing its lifecycle lock (or the PID being gone). An identity read that
    is unavailable or differs while the lock is still held is not an exit and
    not an error: keep polling.

    ``force=True`` sends the force request instead (the daemon skips the
    drain; a legacy daemon gets SIGKILL).

    Returns the PID that was signalled. Raises :class:`LifecycleError` on
    timeout.
    """
    try:
        target = _request_stop_target(vault, sig=sig, force=force, drain_timeout=timeout)
    except _OwnerGone as exc:
        raise LifecycleError(str(exc)) from exc
    pid = target.pid
    started = time.monotonic()
    wait = timeout + close_budget(timeout) + _STOP_EXIT_GRACE_SECONDS
    deadline = started + wait
    while time.monotonic() < deadline:
        if isinstance(target, _PidRecord):
            try:
                _active_pid_record(
                    vault,
                    expected_owner_id=target.owner_id,
                    expected_start_token=target.start_token,
                )
            except (_OwnerGone, _OwnerChanged):
                return pid
            except _IdentityUnverifiable:
                # Covers a transiently unreadable process-birth identity and a
                # birth token that reads differently while the owner still
                # holds the lock. Neither is proof the owner exited, and
                # neither is an error: we only poll a target we already
                # signalled. Only a confirmed exit above, or the deadline
                # below, ends the wait. A corrupt or missing PID record still
                # raises the base ``LifecycleError``.
                pass
        elif not _legacy_target_still_same(vault, target):
            return pid
        time.sleep(poll_interval)
    path = pid_file_path(vault)
    raise LifecycleError(f"server did not exit within {wait:.1f}s (pid={pid}, pid_file={path})")


# ---------------------------------------------------------------------------
# Graceful shutdown orchestrator (card 9242f29d / br_430ba18c)
# ---------------------------------------------------------------------------


_DEFAULT_DRAIN_TIMEOUT = 30.0
"""Seconds we wait for in-flight requests to finish on SIGTERM/SIGINT."""


_SHUTDOWN_LOG = logging.getLogger("okto_neuron.server.shutdown")
_SHUTDOWN_HARD_DEADLINE: float | None = None


def set_shutdown_hard_deadline(deadline: float | None) -> None:
    """Record the absolute (monotonic) hard deadline ``shutdown.phase`` reports against."""
    global _SHUTDOWN_HARD_DEADLINE
    _SHUTDOWN_HARD_DEADLINE = deadline


def flush_before_exit(timeout_s: float = 5.0) -> dict[str, int]:
    """Flush what ``os._exit`` would drop: telemetry, logs, stdout/stderr.

    ``os._exit`` skips ``atexit`` and thread joins, so the pending MLflow span
    queue, buffered log handlers and stdio are flushed explicitly, in that
    order, with telemetry bounded to ``timeout_s``. Spans still queued after
    the bound are given up on and their count is logged, then logging is shut
    down (flushing every handler) and stdio flushed. Returns
    ``{"telemetry_dropped": n}``.
    """
    dropped = 0
    try:
        from okto_neuron.llm import _telemetry

        pending = _telemetry._QUEUE
        if pending is not None and not _telemetry.flush(timeout_s):
            dropped = int(pending.unfinished_tasks)
    except Exception:  # noqa: BLE001 - the exit path must not raise
        _SHUTDOWN_LOG.warning("telemetry flush failed before exit", exc_info=True)
    mlflow = sys.modules.get("mlflow")
    flush_async = getattr(mlflow, "flush_trace_async_logging", None)
    if callable(flush_async):
        try:
            flush_async()
        except Exception:  # noqa: BLE001
            _SHUTDOWN_LOG.warning("mlflow trace flush failed before exit", exc_info=True)
    if dropped:
        _SHUTDOWN_LOG.warning(
            "shutdown.telemetry_dropped count=%d (export queue not drained in %.1fs)",
            dropped,
            timeout_s,
        )
    logging.shutdown()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()
    return {"telemetry_dropped": dropped}


@contextlib.contextmanager
def shutdown_phase(name: str, **fields: Any) -> Iterator[dict[str, Any]]:
    """Log one ``shutdown.phase`` line (name, duration_ms, remaining_s, status).

    The yielded dict lets a phase attach extra ``key=value`` detail (for example
    ``result``) that is included in the line. An exception is logged with
    ``status=error`` and re-raised.
    """
    started = time.monotonic()
    detail: dict[str, Any] = dict(fields)
    status = "ok"
    try:
        yield detail
    except BaseException:
        status = "error"
        raise
    finally:
        now = time.monotonic()
        hard = _SHUTDOWN_HARD_DEADLINE
        remaining = max(0.0, hard - now) if hard is not None else -1.0
        extra = "".join(f" {key}={value}" for key, value in detail.items())
        _SHUTDOWN_LOG.info(
            "shutdown.phase name=%s duration_ms=%d remaining_s=%.1f status=%s%s",
            name,
            int((now - started) * 1000),
            remaining,
            status,
            extra,
            extra={"component": "server", "event": "shutdown.phase", "phase": name},
        )


class GracefulShutdown:
    """Cross-transport coordinator for SIGTERM-clean shutdown.

    Lifecycle (single owner — one per ``okto-neuron serve`` process):

    1. Workers wrap each request in :meth:`track_request`, which increments
       an in-flight counter while the body runs.
    2. A SIGTERM/SIGINT handler installed via :func:`install_shutdown_handlers`
       calls :meth:`request_shutdown`, which sets ``shutdown_event`` so the
       transport layer can stop accepting new requests.
    3. The ``serve`` entrypoint calls :meth:`shutdown` from its ``finally``
       block. That method waits up to ``drain_timeout`` seconds for the
       in-flight counter to reach zero, then closes the vault (or store)
       exactly once.

    The orchestrator is idempotent: extra signals or extra ``shutdown``
    calls do not re-trigger drain or re-close the vault. This matters
    because operators routinely send a second SIGTERM if the first
    appears slow.

    SIGKILL is intentionally outside this contract — orphan-lock recovery
    on the next startup is owned by :class:`PidFile`'s staleness check.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_flight = 0
        self._zero_event = threading.Event()
        self._zero_event.set()  # initially nothing in flight
        self._shutdown_event = threading.Event()
        self._force_event = threading.Event()
        self._deadline: float | None = None
        self._drain_timeout: float | None = None
        self._closed = False

    @property
    def drain_timeout(self) -> float | None:
        """The first shutdown request's drain budget in seconds, if set."""
        with self._lock:
            return self._drain_timeout

    @property
    def shutdown_event(self) -> threading.Event:
        return self._shutdown_event

    @property
    def shutdown_requested(self) -> bool:
        return self._shutdown_event.is_set()

    @property
    def force_requested(self) -> bool:
        return self._force_event.is_set()

    @property
    def deadline(self) -> float | None:
        """The first shutdown request's absolute monotonic deadline, if set."""
        with self._lock:
            return self._deadline

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def request_shutdown(self, *, timeout: float | None = None) -> None:
        """Signal that shutdown was requested. Idempotent.

        When ``timeout`` is supplied, the first caller establishes one absolute
        monotonic deadline. Repeated calls never extend it. Calls without a
        timeout remain safe for the small synchronous signal-handler helper.
        """
        if timeout is not None:
            candidate = time.monotonic() + max(0.0, timeout)
            with self._lock:
                if self._deadline is None:
                    self._deadline = candidate
                    self._drain_timeout = max(0.0, timeout)
        self._shutdown_event.set()

    def request_force_shutdown(self) -> None:
        """Record an operator escalation without extending the first deadline."""
        self._force_event.set()
        self._shutdown_event.set()

    def remaining(self, *, default_timeout: float | None = None) -> float:
        """Seconds left on the shared deadline, optionally creating it.

        Runtime teardown calls this before every wait so transports, workers,
        locks, and close all consume the same finite budget.
        """
        if default_timeout is not None:
            self.request_shutdown(timeout=default_timeout)
        with self._lock:
            deadline = self._deadline
        if deadline is None:
            return max(0.0, default_timeout or 0.0)
        return max(0.0, deadline - time.monotonic())

    @contextlib.contextmanager
    def track_request(self) -> Iterator[None]:
        """Increment the in-flight counter for the duration of the block."""
        with self._lock:
            self._in_flight += 1
            self._zero_event.clear()
        try:
            yield
        finally:
            with self._lock:
                self._in_flight -= 1
                if self._in_flight <= 0:
                    self._in_flight = 0
                    self._zero_event.set()

    def wait_for_drain(self, timeout: float) -> bool:
        """Block until the in-flight counter reaches 0 or ``timeout`` elapses.

        Returns True when fully drained, False on timeout.
        """
        return self._zero_event.wait(timeout=timeout)

    def shutdown(
        self,
        *,
        vault: Any | None = None,
        store: Any | None = None,
        drain_timeout: float = _DEFAULT_DRAIN_TIMEOUT,
        logger: logging.Logger | None = None,
    ) -> dict[str, Any]:
        """Run the orchestrated shutdown sequence exactly once.

        Steps (each guarded so a second call is a no-op):

        1. Mark shutdown as requested (in case it wasn't already).
        2. Wait up to ``drain_timeout`` seconds for in-flight requests
           to finish.
        3. Close ``vault`` and/or ``store`` exactly once via their
           ``close()`` method. ``Vault.close()`` already delegates to
           the underlying store close, so passing the vault alone is
           sufficient on the standard path.

        PID-file removal is NOT done here — it is owned by the
        :class:`PidFile` context manager in the ``serve`` entrypoint.
        Centralizing teardown in one place avoids double-unlink races
        when both the signal handler and the context manager exit.

        Returns a small dict describing what happened, suitable for
        logging.
        """
        if self._closed:
            return {"drained": True, "closed": False, "in_flight": 0, "already": True}
        self._closed = True
        self.request_shutdown(timeout=drain_timeout)
        drained = self.wait_for_drain(
            timeout=min(drain_timeout, self.remaining(default_timeout=drain_timeout))
        )
        if logger is not None:
            logger.info(
                "server drain complete" if drained else "server drain timed out",
                extra={
                    "component": "server",
                    "event": "server.drain",
                    "drained": drained,
                    "in_flight": self.in_flight,
                },
            )
        closed = False
        for target in (vault, store):
            if target is None:
                continue
            close = getattr(target, "close", None)
            if not callable(close):
                continue
            try:
                close()
                closed = True
            except Exception as exc:  # pragma: no cover - defensive
                if logger is not None:
                    logger.error(
                        f"error closing {type(target).__name__}: {exc}",
                        extra={"component": "server", "event": "server.close_error"},
                    )
        return {
            "drained": drained,
            "closed": closed,
            "in_flight": self.in_flight,
            "already": False,
        }


def install_shutdown_handlers(
    orchestrator: GracefulShutdown,
    *,
    signals: tuple[int, ...] = (signal.SIGTERM, signal.SIGINT),
) -> Callable[[], None]:
    """Wire ``signals`` to ``orchestrator.request_shutdown()``.

    Returns a callable that restores the previous handlers. Must be
    called from the main thread (POSIX requirement for ``signal.signal``).

    The handler itself is async-signal-safe: it only sets a
    :class:`threading.Event`. The actual drain + close happens in
    :meth:`GracefulShutdown.shutdown`, which the ``serve`` entrypoint
    invokes from its ``finally`` block once the transport loop has
    returned.
    """
    previous: dict[int, Any] = {}

    def _handle(signum: int, frame: Any) -> None:  # pragma: no cover - exercised in tests
        del frame
        orchestrator.request_shutdown()

    for sig in signals:
        try:
            previous[sig] = signal.signal(sig, _handle)
        except (ValueError, OSError):
            # Not on main thread or signal not supported on this platform.
            continue

    def _restore() -> None:
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)

    return _restore
