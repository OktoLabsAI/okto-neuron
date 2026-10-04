"""SQLite storage layer for the review queue (#14).

``review_queue.json`` was rewritten whole (indent=2, fsync, replace) on every
enqueue/acknowledge and re-parsed whole on every ``ReviewQueue`` construction;
on a large vault it was >100 MB, 97% of it embeddings. This module is the
entry-agnostic row store that replaces it. It knows nothing about candidates:
``review_queue.py`` turns an entry into (payload JSON without the embedding,
embedding floats, digest) and back.

Layout ``<vault>/.marginalia/review_queue.sqlite`` (WAL, ``synchronous=FULL``)::

    meta(key PK, value)
    entries(seq INTEGER PK AUTOINCREMENT, candidate_id UNIQUE, kind, reason,
            created_at, payload TEXT, entry_sha256)
    embeddings(candidate_id PK -> entries ON DELETE CASCADE, dim, vec BLOB)

``seq`` is the stable insertion order (an upsert keeps it, like the old dict).
The embedding is stored as little-endian float64, so the round trip is
bit-exact and ``json.dumps`` of the restored floats is the same text the JSON
era produced. Connections are per operation; writes are one ``BEGIN IMMEDIATE``
transaction per row, so concurrent daemon threads serialise in SQLite instead
of overwriting each other's whole-file snapshot.
"""

from __future__ import annotations

import os
import sqlite3
import struct
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from okto_neuron.errors import OktoNeuronError

SQLITE_FILENAME = "review_queue.sqlite"
SCHEMA_VERSION = "1"
BUSY_TIMEOUT_S = 5.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entries (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('node', 'relation')),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    entry_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS embeddings (
    candidate_id TEXT PRIMARY KEY REFERENCES entries(candidate_id) ON DELETE CASCADE,
    dim INTEGER NOT NULL,
    vec BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS entries_kind_seq ON entries(kind, seq);
"""


class ReviewQueueMigrationRequired(OktoNeuronError):
    """The vault still uses ``review_queue.json``; nothing here may touch it."""

    EXIT_CODE = 4
    default_message = "review queue needs an explicit migration"


class ReviewQueueCorruption(OktoNeuronError):
    """A stored row no longer matches its recorded ``entry_sha256``."""

    EXIT_CODE = 1
    default_message = "review queue row failed its integrity check"


@dataclass(frozen=True)
class StoredRow:
    seq: int
    candidate_id: str
    kind: str
    reason: str
    created_at: str
    payload: str
    entry_sha256: str
    embedding: tuple[float, ...] | None = None


def pack_embedding(values: tuple[float, ...] | list[float]) -> bytes:
    if not all(type(value) is float for value in values):
        raise ValueError("review queue embeddings must be float64 values")
    return struct.pack(f"<{len(values)}d", *values)


def unpack_embedding(dim: int, blob: bytes) -> tuple[float, ...]:
    if len(blob) != 8 * dim:
        raise ReviewQueueCorruption("embedding blob length does not match its dimension")
    return struct.unpack(f"<{dim}d", blob)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def encode_cursor(kind: str, seq: int) -> str:
    return f"v1.{kind}.{seq}"


def decode_cursor(cursor: str) -> tuple[str, int]:
    parts = cursor.split(".")
    if len(parts) != 3 or parts[0] != "v1" or parts[1] not in ("node", "relation"):
        raise ValueError("invalid review queue cursor")
    try:
        seq = int(parts[2])
    except ValueError as exc:
        raise ValueError("invalid review queue cursor") from exc
    if seq < 0:
        raise ValueError("invalid review queue cursor")
    return parts[1], seq


def _retry_locked(operation):
    """Run a setup statement, retrying while another process holds the lock.

    ``busy_timeout`` does not cover the journal-mode switch or first-time DDL,
    so two writers creating the same file would otherwise see "database is
    locked" instead of queueing.
    """

    deadline = time.monotonic() + BUSY_TIMEOUT_S * 2
    while True:
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() > deadline:
                raise
            time.sleep(0.02)


def connect(path: Path, *, create: bool) -> sqlite3.Connection:
    """Open one connection with the queue's pragmas; ``create`` adds the schema."""

    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
    try:
        connection.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
        if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
            _retry_locked(lambda: connection.execute("PRAGMA journal_mode = WAL"))
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        if create and not _has_schema(connection):
            _retry_locked(lambda: _create_schema(connection))
    except BaseException:
        connection.close()
        raise
    return connection


def _has_schema(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN ('meta','entries','embeddings')"
        ).fetchone()[0]
        == 3
    )


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(_SCHEMA)
    connection.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    # A file born from the queue itself is authoritative; the migration
    # overwrites this with 'building' until it has verified its own file.
    connection.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('state', 'complete')")


class SqliteQueueStore:
    """Row store for one vault's review queue. Stateless: one connection per call."""

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / SQLITE_FILENAME

    def exists(self) -> bool:
        return self.path.exists()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection | None]:
        if not self.path.exists():
            yield None
            return
        connection = connect(self.path, create=False)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _writer(self) -> Iterator[sqlite3.Connection]:
        self.directory.mkdir(parents=True, exist_ok=True)
        connection = connect(self.path, create=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()

    # -- writes ---------------------------------------------------------------
    def put(
        self,
        *,
        candidate_id: str,
        kind: str,
        reason: str,
        payload: str,
        entry_sha256: str,
        embedding: tuple[float, ...] | list[float] | None,
    ) -> None:
        """Upsert one entry and its embedding in one transaction."""

        blob = pack_embedding(embedding) if embedding is not None else None
        with self._writer() as connection:
            existing = connection.execute(
                "SELECT kind FROM entries WHERE candidate_id = ?", (candidate_id,)
            ).fetchone()
            if existing is not None and existing[0] != kind:
                raise ValueError(
                    f"candidate id is already occupied by a {existing[0]} review entry"
                )
            connection.execute(
                "INSERT INTO entries(candidate_id, kind, reason, created_at, payload, entry_sha256)"
                " VALUES(?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(candidate_id) DO UPDATE SET reason = excluded.reason,"
                " payload = excluded.payload, entry_sha256 = excluded.entry_sha256",
                (candidate_id, kind, reason, _now(), payload, entry_sha256),
            )
            connection.execute("DELETE FROM embeddings WHERE candidate_id = ?", (candidate_id,))
            if blob is not None:
                connection.execute(
                    "INSERT INTO embeddings(candidate_id, dim, vec) VALUES(?, ?, ?)",
                    (candidate_id, len(embedding), blob),  # type: ignore[arg-type]
                )

    def delete(self, candidate_id: str) -> bool:
        if not self.path.exists():
            return False
        with self._writer() as connection:
            cursor = connection.execute(
                "DELETE FROM entries WHERE candidate_id = ?", (candidate_id,)
            )
            return cursor.rowcount > 0

    # -- reads ----------------------------------------------------------------
    def count(self, kind: str | None = None) -> int:
        with self._reader() as connection:
            if connection is None:
                return 0
            if kind is None:
                return int(connection.execute("SELECT COUNT(*) FROM entries").fetchone()[0])
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM entries WHERE kind = ?", (kind,)
                ).fetchone()[0]
            )

    def get(self, candidate_id: str, *, with_embedding: bool) -> StoredRow | None:
        rows = self._select("WHERE e.candidate_id = ?", (candidate_id,), None, with_embedding)
        return rows[0] if rows else None

    def rows(
        self,
        *,
        kind: str | None = None,
        after: tuple[str, int] | None = None,
        limit: int | None = None,
        with_embedding: bool = False,
    ) -> list[StoredRow]:
        """Rows ordered ``(kind, seq)`` -- nodes first, then relations, insertion order."""

        clauses: list[str] = []
        params: list[object] = []
        if kind is not None:
            clauses.append("e.kind = ?")
            params.append(kind)
        if after is not None:
            clauses.append("(e.kind, e.seq) > (?, ?)")
            params.extend(after)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        return self._select(where, tuple(params), limit, with_embedding)

    def _select(
        self, where: str, params: tuple, limit: int | None, with_embedding: bool
    ) -> list[StoredRow]:
        columns = "e.seq, e.candidate_id, e.kind, e.reason, e.created_at, e.payload, e.entry_sha256"
        join = ""
        if with_embedding:
            columns += ", m.dim, m.vec"
            join = " LEFT JOIN embeddings m ON m.candidate_id = e.candidate_id"
        sql = f"SELECT {columns} FROM entries e{join} {where} ORDER BY e.kind, e.seq"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._reader() as connection:
            if connection is None:
                return []
            fetched = connection.execute(sql, params).fetchall()
        rows: list[StoredRow] = []
        for record in fetched:
            embedding = None
            if with_embedding and record[7] is not None:
                embedding = unpack_embedding(record[7], record[8])
            rows.append(StoredRow(*record[:7], embedding=embedding))
        return rows

    def meta(self) -> dict[str, str]:
        with self._reader() as connection:
            if connection is None:
                return {}
            return dict(connection.execute("SELECT key, value FROM meta").fetchall())

    def set_meta(self, **values: str) -> None:
        with self._writer() as connection:
            for key, value in values.items():
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES(?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )


def fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
