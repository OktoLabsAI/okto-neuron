"""Vault graph bootstrap for Ladybug-backed Okto Neuron vaults."""

from __future__ import annotations

import errno
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import ladybug

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None  # type: ignore[assignment]

from okto_neuron.errors import (
    BootstrapPartial,
    EmbeddingDimMismatch,
    SchemaVersionMismatch,
    VaultLockHeld,
)
from okto_neuron.store import schema
from okto_neuron.store.integrity_state import (
    initialize_integrity_state,
    invalidate_integrity_state,
)

_MARGINALIA_DIR = ".marginalia"
_BOOTSTRAP_LOCK = ".bootstrap.lock"
_GRAPH_FILE = "graph.lbug"
_CORRUPT_DIR = "corrupt-graph"
# Okto Neuron's own pre-operation safety backup (heal/rebuild/reconcile write
# `graph.lbug.bak`). It is NOT a ladybug WAL sidecar — never quarantine it, so it
# stays available as a manual fallback next to a recovered/empty graph.
_PRESERVED_SIDECAR_SUFFIXES = (".bak",)
_LOCK_CONTENTION_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}
_LOCK_CONTENTION_MARKERS = (
    "could not set lock",
    "conflicting lock is held",
    "failed to acquire lock",
    "database is locked",
    "lock is already held",
)

_LOG = logging.getLogger("okto_neuron.store")

# Substrings (lowercased) that classify an open/bootstrap failure as on-disk
# corruption — a `kill -9` mid-write leaves a torn graph or WAL that ladybug
# refuses to open. Recovery (quarantine + fresh graph) is keyed off these.
# ladybug emits e.g. "Corrupted wal file...", "Checksum verification failed, the
# WAL file is corrupted.", and for a clobbered main file "The file is not a
# valid Lbug database file!".
_CORRUPTION_MARKERS = (
    "corrupt",
    "malformed",
    "invalid wal",
    "not a valid lbug",
    # Ladybug 0.16 can surface a torn WAL as an internal parser assertion
    # instead of a checksum/corruption message. The source file is the stable,
    # WAL-specific discriminator; a generic UNREACHABLE_CODE elsewhere must not
    # authorize quarantine.
    "wal_record.cpp",
)


@dataclass
class VaultGraphHandle:
    """Process-local handle for a bootstrapped vault graph."""

    vault_path: Path
    schema_version: int
    embedding_dim: int | None
    database: ladybug.Database
    graph_generation: str | None = None
    identity_contract_version: str | None = None
    graph_file_identity: tuple[int, int] | None = None
    recovered_from_corruption: bool = False
    quarantine_path: Path | None = None
    # "checkpoint" when the last good .lbug checkpoint was recovered (claims kept);
    # "empty" when the checkpoint was itself torn and a blank graph was started.
    recovered_mode: str | None = None

    def close(self) -> None:
        self.database.close()


_bootstrap_cache: dict[Path, VaultGraphHandle] = {}


def bootstrap_vault_graph(vault_path: Path | str) -> VaultGraphHandle:
    """Create or verify the vault Ladybug graph and return a cached handle."""
    resolved_vault_path = Path(vault_path).expanduser().resolve(strict=False)
    cached = _bootstrap_cache.get(resolved_vault_path)
    if cached is not None and not getattr(cached.database, "is_closed", False):
        return cached
    if cached is not None:
        _bootstrap_cache.pop(resolved_vault_path, None)

    resolved_vault_path.mkdir(parents=True, exist_ok=True)
    marginalia_dir = resolved_vault_path / _MARGINALIA_DIR
    marginalia_dir.mkdir(parents=True, exist_ok=True)

    lock_path = marginalia_dir / _BOOTSTRAP_LOCK
    graph_path = resolved_vault_path / _GRAPH_FILE
    configured_dim = _resolve_configured_dim(resolved_vault_path)
    recovered = False
    recovered_mode: str | None = None
    quarantine_dir: Path | None = None
    graph_identity = schema.GraphIdentity(None, None)
    with _bootstrap_lock(resolved_vault_path, lock_path):
        try:
            database = _open_and_bootstrap(
                graph_path,
                resolved_vault_path,
                configured_dim=configured_dim,
            )
        except (SchemaVersionMismatch, EmbeddingDimMismatch):
            raise
        except BootstrapPartial as exc:
            if not _is_corruption(exc):
                raise
            database, recovered_mode, quarantine_dir = _recover_from_corruption(
                resolved_vault_path,
                graph_path,
                exc,
                configured_dim=configured_dim,
            )
            recovered = True
        try:
            graph_identity = _read_graph_identity(database)
            if recovered:
                invalidate_integrity_state(
                    resolved_vault_path,
                    graph_generation=graph_identity.graph_generation,
                    reason=(
                        "graph recovered from a corrupt WAL/checkpoint; the recovered "
                        "generation has not been re-audited"
                    ),
                )
            else:
                initialize_integrity_state(
                    resolved_vault_path,
                    graph_generation=graph_identity.graph_generation,
                )
        except Exception:
            # Any failure past this point means the caller never receives (and
            # therefore can never close) this database handle. Close it here so
            # a post-open failure can't leak an open ladybug.Database.
            database.close()
            raise

    try:
        handle = VaultGraphHandle(
            vault_path=resolved_vault_path,
            schema_version=schema.CURRENT_SCHEMA_VERSION,
            embedding_dim=configured_dim,
            database=database,
            graph_generation=graph_identity.graph_generation,
            identity_contract_version=graph_identity.identity_contract_version,
            graph_file_identity=_graph_file_identity(graph_path),
            recovered_from_corruption=recovered,
            quarantine_path=quarantine_dir,
            recovered_mode=recovered_mode,
        )
    except Exception:
        database.close()
        raise
    _bootstrap_cache[resolved_vault_path] = handle
    return handle


def _graph_file_identity(graph_path: Path) -> tuple[int, int]:
    """Stable kernel identity changed by atomic graph replacement, not writes."""
    stat = graph_path.stat()
    return int(stat.st_dev), int(stat.st_ino)


def _open_and_bootstrap(
    graph_path: Path,
    vault_path: Path,
    *,
    configured_dim: int | None = None,
) -> ladybug.Database:
    """Open the graph, connect, verify + apply DDL + verify, and return the db.

    The ``ladybug.Database(graph_path)`` open AND the connect/verify/ddl block are
    both inside corruption handling: a torn main file throws on open, a torn WAL
    throws on connect/replay. ``SchemaVersionMismatch`` propagates unchanged; any
    other failure (open or bootstrap) closes whatever was opened and is wrapped in
    ``BootstrapPartial`` so the caller can classify + recover.
    """
    database: ladybug.Database | None = None
    connection: Any | None = None
    configured_dim = configured_dim or _resolve_configured_dim(vault_path)
    is_fresh_graph = not graph_path.exists()
    new_identity = (
        schema.new_graph_identity() if is_fresh_graph else schema.GraphIdentity(None, None)
    )
    try:
        database = ladybug.Database(graph_path)
        connection = _connect(database)
        schema.verify_schema_version(connection, file_path=graph_path)
        # Dim-guard: refuse to open a graph whose stored vector width disagrees
        # with the configured model. A fresh/never-bootstrapped graph has no
        # recorded width yet (accepted); the DDL below records it. Reembed's
        # re-width path reads the live graph via a raw handle (no DDL, no guard),
        # so this never fires on an intentional re-width.
        schema.verify_embedding_dim(connection, configured_dim, file_path=graph_path)
        _execute_bootstrap_ddl(
            connection,
            configured_dim,
            graph_generation=new_identity.graph_generation,
            identity_contract_version=new_identity.identity_contract_version,
        )
        schema.verify_schema_version(connection, file_path=graph_path)
    except (SchemaVersionMismatch, EmbeddingDimMismatch):
        _close_connection(connection)
        if database is not None:
            database.close()
        raise
    except Exception as exc:
        _close_connection(connection)
        if database is not None:
            database.close()
        raise BootstrapPartial(
            vault_path,
            file_path=graph_path,
            cause=exc,
        ) from exc
    _close_connection(connection)
    return database


def _is_corruption(exc: Exception) -> bool:
    """Classify a bootstrap failure as on-disk corruption.

    Inspects the exception text AND its ``cause`` (ladybug's raw open/WAL error is
    wrapped in ``BootstrapPartial(cause=...)``). ``SchemaVersionMismatch`` and
    ``VaultLockHeld`` are deliberately NOT corruption — they propagate.
    """
    if isinstance(exc, SchemaVersionMismatch) or _is_lock_contention(exc):
        return False
    haystack = str(exc).lower() + " " + str(getattr(exc, "cause", "")).lower()
    return any(marker in haystack for marker in _CORRUPTION_MARKERS)


def _is_lock_contention(exc: Exception) -> bool:
    """Recognize an already-open Ladybug graph before WAL-path heuristics.

    Ladybug reports process lock contention as a generic open error. Its message
    includes the graph path, so a vault directory containing ``wal`` used to
    match :data:`_CORRUPTION_MARKERS` and trigger destructive quarantine. Walk
    both Okto Neuron's explicit ``cause`` and Python's exception chain so typed
    OS contention and Ladybug's stable lock messages always win classification.
    """

    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, VaultLockHeld):
            return True
        if isinstance(current, OSError) and current.errno in _LOCK_CONTENTION_ERRNOS:
            return True
        if any(marker in str(current).lower() for marker in _LOCK_CONTENTION_MARKERS):
            return True
        for nested in (
            getattr(current, "cause", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return False


def _recover_from_corruption(
    vault_path: Path,
    graph_path: Path,
    exc: Exception,
    *,
    configured_dim: int,
) -> tuple[ladybug.Database, str, Path]:
    """Recover a corrupt on-disk graph, PREFERRING the last good checkpoint.

    Ladybug merges the WAL into the main ``.lbug`` only on a clean close, so a
    ``kill -9`` mid-write most often tears just the WAL while the main file stays a
    valid checkpoint. The old behavior quarantined the *whole* graph (main file
    included) and booted EMPTY — silently discarding every claim in an intact
    checkpoint (proven: a torn-WAL replay dropped 5389 recoverable claims to 0).

    So recover in two steps:

    1. Quarantine ONLY the torn WAL/sidecars, leave the main checkpoint in place,
       and retry the open. If it succeeds we recovered every claim in the last
       checkpoint (``recovered_mode="checkpoint"``). Writes since that checkpoint
       are genuinely lost with the torn WAL — surfaced via ``/health`` so the
       operator can re-ingest recent sources or run ``kg rebuild``.
    2. Only if the main file itself is torn (no checkpoint to recover) do we
       quarantine it too and start a FRESH empty graph (``recovered_mode="empty"``);
       ``kg rebuild`` from the durable markdown (the trust root) is the last resort.

    Returns ``(database, recovered_mode, quarantine_dir)``.
    """
    quarantine_dir = _new_quarantine_dir(vault_path)
    # Step 1: quarantine the torn WAL/sidecars; keep the main checkpoint on disk.
    _quarantine_sidecars(graph_path, quarantine_dir)
    try:
        database = _open_and_bootstrap(
            graph_path,
            vault_path,
            configured_dim=configured_dim,
        )
    except (SchemaVersionMismatch, EmbeddingDimMismatch):
        raise
    except BootstrapPartial as retry_exc:
        if not _is_corruption(retry_exc):
            raise
    else:
        _LOG.warning(
            "vault graph at %s had a CORRUPT WAL (%s); quarantined the WAL to %s "
            "and RECOVERED from the last checkpoint. Writes since the last "
            "checkpoint were lost with the torn WAL — re-ingest recent sources or "
            "run `kg rebuild`.",
            graph_path,
            exc,
            quarantine_dir,
        )
        return database, "checkpoint", quarantine_dir

    # Step 2: the checkpoint is unrecoverable. Move the main file (and any fresh
    # partial WAL the failed retry dropped) aside and start a FRESH empty graph.
    if graph_path.exists():
        os.replace(graph_path, quarantine_dir / graph_path.name)
    _quarantine_sidecars(graph_path, quarantine_dir)
    _LOG.warning(
        "vault graph at %s was CORRUPT (%s) and the checkpoint is unrecoverable; "
        "quarantined to %s and started a FRESH empty graph. Run `kg rebuild` to "
        "reconstruct from sources.",
        graph_path,
        exc,
        quarantine_dir,
    )
    database = _open_and_bootstrap(
        graph_path,
        vault_path,
        configured_dim=configured_dim,
    )
    return database, "empty", quarantine_dir


def _new_quarantine_dir(vault_path: Path) -> Path:
    """Reserve a fresh ``.marginalia/corrupt-graph`` (``-2``, ``-3``, …) slot.

    Scans for a free deterministic slot (a prior recovery may already hold one) —
    no timestamps/random so the layout stays deterministic.
    """
    base = vault_path / _MARGINALIA_DIR
    target = base / _CORRUPT_DIR
    suffix = 2
    while target.exists():
        target = base / f"{_CORRUPT_DIR}-{suffix}"
        suffix += 1
    target.mkdir(parents=True)
    return target


def _quarantine_sidecars(graph_path: Path, target: Path) -> None:
    """Move ladybug WAL/shadow sidecars into ``target``, LEAVING the main file (and
    Okto Neuron's ``.bak`` safety backup) in place.

    Moves every sibling whose name starts with ``graph.lbug`` (``.wal``, ``.shadow``,
    …) EXCEPT ``graph.lbug`` itself and any ``.bak`` — the main checkpoint must stay
    for checkpoint recovery, and the ``.bak`` is a legitimate fallback we must not
    relocate. ``os.replace`` keeps it atomic on the same filesystem.
    """
    parent = graph_path.parent
    if not parent.exists():
        return
    for sibling in sorted(parent.iterdir()):
        if sibling.name == graph_path.name:
            continue
        if sibling.suffix in _PRESERVED_SIDECAR_SUFFIXES:
            continue
        if sibling.name.startswith(graph_path.name):
            os.replace(sibling, target / sibling.name)


def reset_bootstrap_cache_for_tests(vault_path: Path | str) -> None:
    """Drop one cached bootstrap handle and close its Ladybug database."""
    resolved_vault_path = Path(vault_path).expanduser().resolve(strict=False)
    handle = _bootstrap_cache.pop(resolved_vault_path, None)
    if handle is not None:
        handle.close()


@contextmanager
def _bootstrap_lock(vault_path: Path, lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            _lock_file_nonblocking(lock_file)
        except OSError as exc:
            if exc.errno not in _LOCK_CONTENTION_ERRNOS:
                raise
            raise VaultLockHeld(
                vault_path,
                holding_pid=_read_lock_pid(lock_file),
                cause=exc,
            ) from exc

        try:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.write(str(os.getpid()))
            lock_file.flush()
            os.fsync(lock_file.fileno())
            yield
        finally:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.flush()
            os.fsync(lock_file.fileno())
            _unlock_file(lock_file)


def _lock_file_nonblocking(lock_file: Any) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    if msvcrt is None:  # pragma: no cover - every supported OS has one path.
        raise OSError(errno.ENOSYS, "no file locking backend available")
    lock_file.seek(0)
    if not lock_file.read(1):
        lock_file.write("0")
        lock_file.flush()
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)


def _unlock_file(lock_file: Any) -> None:
    if fcntl is not None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is None:  # pragma: no cover - every supported OS has one path.
        raise OSError(errno.ENOSYS, "no file locking backend available")
    lock_file.seek(0)
    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _connect(database: ladybug.Database) -> Any:
    connect = getattr(database, "connect", None)
    if callable(connect):
        return connect()
    return ladybug.Connection(database)


def _resolve_configured_dim(vault_path: Path) -> int:
    """Resolve the configured embedding width from ``okto-neuron.yaml``.

    Falls back to the default width when no config is present (a bare bootstrap in
    tests, or a vault scaffolded before its config is written) so opens never crash
    on a missing/partial config — they just assume the default width.
    """
    try:
        from okto_neuron.config import VaultConfig

        return int(VaultConfig.load(vault_path).embedding.dimension)
    except Exception:
        return schema.DEFAULT_EMBEDDING_DIM


def _execute_bootstrap_ddl(
    connection: Any,
    dim: int,
    *,
    graph_generation: str | None = None,
    identity_contract_version: str | None = None,
) -> None:
    for statement in schema.ddl_statements(
        dim,
        graph_generation=graph_generation,
        identity_contract_version=identity_contract_version,
    ):
        try:
            result = connection.execute(statement)
        except Exception as exc:
            if _is_idempotent_skip(statement, exc):
                continue
            raise
        _close_result(result)


def _read_graph_identity(database: ladybug.Database) -> schema.GraphIdentity:
    connection = _connect(database)
    try:
        return schema.read_graph_identity(connection)
    finally:
        _close_connection(connection)


def _is_idempotent_skip(statement: str, exc: Exception) -> bool:
    """True for DDL whose already-present failure is the no-op case.

    A re-bootstrap of an already-current graph re-issues CREATE_VECTOR_INDEX (fails
    'already exists') and the migration ALTERs (fail 'already has property'); both
    are safe to skip.
    """
    message = str(exc).lower()
    if statement.startswith("CALL CREATE_VECTOR_INDEX("):
        return "already exists" in message
    if statement.startswith("ALTER TABLE "):
        return "already has property" in message or "already exists" in message
    return False


def _read_lock_pid(lock_file: Any) -> int | None:
    lock_file.seek(0)
    raw_pid = lock_file.read().strip()
    if not raw_pid:
        return None
    try:
        return int(raw_pid)
    except ValueError:
        return None


def _close_connection(connection: Any | None) -> None:
    if connection is None:
        return
    close = getattr(connection, "close", None)
    if callable(close):
        close()


def _close_result(result: Any) -> None:
    if isinstance(result, list):
        for item in result:
            _close_result(item)
        return
    close = getattr(result, "close", None)
    if callable(close):
        close()


__all__ = [
    "VaultGraphHandle",
    "_bootstrap_cache",
    "bootstrap_vault_graph",
    "reset_bootstrap_cache_for_tests",
]
