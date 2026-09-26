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

PID_RECORD_VERSION = 1
_PID_FILE_LIMIT = 16 * 1024
_PID_LOCK_OFFSET = _PID_FILE_LIMIT
_PID_ACQUIRE_ATTEMPTS = 20
_PROCESS_START_TOKEN_ATTEMPTS = 3
_PROCESS_START_TOKEN_RETRY_SECONDS = 0.05
_SIGNAL_POLL_SECONDS = 0.05

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


@dataclass(frozen=True)
class _PidRecord:
    pid: int
    start_token: str
    owner_id: str
    version: int = PID_RECORD_VERSION

    def to_json(self) -> str:
        return (
            json.dumps(
                {
                    "version": self.version,
                    "pid": self.pid,
                    "start_token": self.start_token,
                    "owner_id": self.owner_id,
                },
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
        if version == PID_RECORD_VERSION and pid > 0 and start_token and owner_id:
            return _PidRecord(
                pid=pid,
                start_token=start_token,
                owner_id=owner_id,
                version=version,
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


def read_pid(vault: Path) -> int | None:
    """Read either a versioned or legacy PID file; never implies ownership."""
    record, legacy_pid = _read_pid_payload(pid_file_path(vault))
    return record.pid if record is not None else legacy_pid


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
            _, legacy_pid = _parse_pid_record(_read_pid_fd(fd))
            if legacy_pid is not None and legacy_pid != self._pid and _process_alive(legacy_pid):
                _unlock_pid_fd(fd)
                os.close(fd)
                raise StaleLockError(legacy_pid, self.path)

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
            )
            _write_pid_fd(fd, record)
            with contextlib.suppress(OSError):
                signal_file_path(self.vault).unlink(missing_ok=True)
            self._fd = fd
            self._record = record
            self._owned = True
            if self._pid == os.getpid():
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
        try:
            self._watch_stop.set()
            watcher = self._watch_thread
            if watcher is not None and watcher is not threading.current_thread():
                watcher.join(timeout=0.5)
            with contextlib.suppress(OSError):
                signal_file_path(self.vault).unlink(missing_ok=True)
            if fd is not None and record is not None and _fd_matches_path(fd, self.path):
                current, _ = _parse_pid_record(_read_pid_fd(fd))
                if current is not None and current.owner_id == record.owner_id:
                    with contextlib.suppress(OSError):
                        self.path.unlink(missing_ok=True)
        finally:
            if fd is not None:
                _unlock_pid_fd(fd)
                os.close(fd)
            self._fd = None
            self._record = None
            self._watch_thread = None
            self._owned = False


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


def configure_logging(
    vault: Path | None,
    *,
    level: int | str = logging.INFO,
    stream: Any = None,
    log_file: Path | None = None,
    rotate_max_bytes: int = DEFAULT_LOG_ROTATE_BYTES,
    rotate_backups: int = DEFAULT_LOG_ROTATE_BACKUPS,
    logger_name: str = "okto_neuron",
) -> logging.Logger:
    """Install :class:`JsonLogFormatter` on ``logger_name``.

    Writes to ``stream`` (default stdout), or — when ``log_file`` is given —
    to a size-rotated file instead (``/dev/null`` and other character devices
    are written without rotation).

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
    handler: logging.Handler
    if log_file is not None:
        log_file = log_file.expanduser()
        if log_file.exists() and not log_file.is_file():
            # Character devices (/dev/null) cannot be size-rotated.
            handler = logging.FileHandler(log_file, delay=True)
        else:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                log_file,
                maxBytes=rotate_max_bytes,
                backupCount=rotate_backups,
                delay=True,
            )
    else:
        handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(JsonLogFormatter(vault=str(vault) if vault is not None else None))
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
        try:
            if not _fd_matches_path(fd, path):
                continue
            acquired = _try_lock_pid_fd(fd)
            if acquired:
                # No process owns the new lifecycle lock. A live legacy number
                # is handed to the bounded migration proof below; it is never
                # signalled merely because the numeric PID is alive.
                record, legacy_pid = _parse_pid_record(_read_pid_fd(fd))
                if legacy_pid is not None and _process_alive(legacy_pid):
                    raise _LegacyOwner(legacy_pid)
                if _fd_matches_path(fd, path):
                    with contextlib.suppress(OSError):
                        path.unlink(missing_ok=True)
                    with contextlib.suppress(OSError):
                        signal_file_path(vault).unlink(missing_ok=True)
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
                            with contextlib.suppress(OSError):
                                path.unlink(missing_ok=True)
                            with contextlib.suppress(OSError):
                                signal_file_path(vault).unlink(missing_ok=True)
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
                    raise LifecycleError(
                        f"PID identity mismatch for {record.pid}; refusing to signal an "
                        "unrelated or unverifiable process"
                    )
                return _PidRecord(
                    pid=record.pid,
                    start_token=current_start,
                    owner_id=record.owner_id,
                    version=record.version,
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
                )
            raise LifecycleError(
                f"PID identity mismatch for {record.pid}; refusing to signal an "
                "unrelated or unverifiable process"
            )
        finally:
            if acquired:
                _unlock_pid_fd(fd)
            os.close(fd)
    raise LifecycleError(f"daemon PID file changed repeatedly while inspecting {path}")


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


def _write_signal_request(vault: Path, record: _PidRecord, sig: int) -> None:
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
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _request_stop(
    vault: Path,
    *,
    sig: int,
    expected_owner_id: str | None = None,
    expected_pid: int | None = None,
    expected_start_token: str | None = None,
) -> _PidRecord:
    record = _active_pid_record(
        vault,
        expected_owner_id=expected_owner_id,
        expected_start_token=expected_start_token,
    )
    if expected_pid is not None and record.pid != expected_pid:
        raise _OwnerChanged
    _write_signal_request(vault, record, sig)
    return record


def _request_stop_target(
    vault: Path,
    *,
    sig: int,
    expected: _PidRecord | _LegacyTarget | None = None,
    expected_pid: int | None = None,
) -> _PidRecord | _LegacyTarget:
    if isinstance(expected, _PidRecord):
        return _request_stop(
            vault,
            sig=sig,
            expected_owner_id=expected.owner_id,
            expected_pid=expected.pid,
            expected_start_token=expected.start_token,
        )
    if isinstance(expected, _LegacyTarget):
        _signal_legacy_target(vault, expected, sig)
        return expected
    try:
        return _request_stop(vault, sig=sig, expected_pid=expected_pid)
    except _LegacyOwner as legacy:
        target = _validate_legacy_target(vault, legacy.pid)
        if expected_pid is not None and target.pid != expected_pid:
            raise _OwnerChanged
        _signal_legacy_target(vault, target, sig)
        return target


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


def stop_server(
    vault: Path,
    *,
    sig: int = signal.SIGTERM,
    timeout: float = 30.0,
    poll_interval: float = 0.1,
) -> int:
    """Signal the server and wait for the PID file to be removed.

    A server that has not stopped after ten seconds receives the same signal a
    second time. The runtime treats that repeat as an explicit force request;
    this makes ``okto-neuron stop`` deterministic without requiring the operator
    to discover the PID and send another signal manually.

    Returns the PID that was signalled. Raises :class:`LifecycleError` on
    timeout.
    """
    try:
        target = _request_stop_target(vault, sig=sig)
    except _OwnerGone as exc:
        raise LifecycleError(str(exc)) from exc
    pid = target.pid
    started = time.monotonic()
    deadline = started + timeout
    escalation_at = started + min(10.0, max(0.1, timeout / 2.0))
    escalated = False
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
                # ``_active_pid_record`` fails closed (refuses to vouch for the
                # owner) whenever a single process-birth read is transiently
                # unavailable, e.g. a ``ps`` call timing out under system load
                # -- see test_expected_locked_owner_with_unavailable_identity_
                # remains_fail_closed. That is correct when deciding whether to
                # *signal* a process, but here we are only polling whether the
                # target we already signalled is still there. An inconclusive
                # read is not proof the owner exited, so keep waiting rather
                # than aborting the whole stop attempt; only a confirmed
                # _OwnerGone/_OwnerChanged above, or the deadline below, ends
                # the wait. A genuine, provable identity mismatch (or a
                # corrupt/missing record) still raises the base
                # ``LifecycleError`` and is not caught here.
                pass
        elif not _legacy_target_still_same(vault, target):
            return pid
        if not escalated and time.monotonic() >= escalation_at:
            try:
                escalation_signal = (
                    signal.SIGKILL
                    if isinstance(target, _LegacyTarget) and os.name == "posix"
                    else sig
                )
                _request_stop_target(
                    vault,
                    sig=escalation_signal,
                    expected=target,
                )
            except (_OwnerGone, _OwnerChanged):
                return pid
            except _IdentityUnverifiable:
                # Same transient-read race as above, but here it happened
                # while trying to deliver the escalation signal itself: no
                # signal was written (the identity check runs before
                # ``_write_signal_request``), so leave ``escalated`` False
                # and retry escalation on a later iteration instead of
                # silently dropping the second signal.
                pass
            else:
                escalated = True
        time.sleep(poll_interval)
    path = pid_file_path(vault)
    raise LifecycleError(f"server did not exit within {timeout:.1f}s (pid={pid}, pid_file={path})")


# ---------------------------------------------------------------------------
# Graceful shutdown orchestrator (card 9242f29d / br_430ba18c)
# ---------------------------------------------------------------------------


_DEFAULT_DRAIN_TIMEOUT = 30.0
"""Seconds we wait for in-flight requests to finish on SIGTERM/SIGINT."""


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
        self._closed = False

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
