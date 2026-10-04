"""Explicit, resumable ``review_queue.json`` -> SQLite migration, rollback (#14).

Never runs implicitly for an existing vault: ``ReviewQueue`` refuses a version-1
vault, and only ``kg review-queue migrate`` calls :func:`migrate`. Ordering is
chosen so an older binary (config version 1 only) can never open a half-migrated
vault; every step is a crash point and the source is never modified until the
very last, verified step::

    1. load + validate the source with the JSON-era loader (any refusal aborts)
    2. verified backup  review_queue.json.bak-v1  (never overwritten/deleted)
    3. yaml marginalia_yaml_version: 2            (from here 0.3.1 refuses)
    4. build  review_queue.sqlite.tmp-<id>        (garbage if we crash)
    5. verify: counts + every entry_sha256 recomputed from the SQLite rows
    6. os.replace -> review_queue.sqlite, meta.state=complete
    7. source renamed to review_queue.json.migrated (no second source of truth)

Rollback regenerates ``review_queue.json`` from SQLite (default) or restores the
literal ``.bak-v1``, moves the SQLite file aside (never deletes it) and sets the
yaml back to 1 LAST. Invariant: yaml 1 means the JSON is the source of truth.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from okto_neuron.config._vault import (
    CURRENT_YAML_VERSION,
    set_vault_yaml_version,
    vault_yaml_version,
)
from okto_neuron.consolidate.review_queue import (
    QUEUE_FILENAME,
    entry_digest,
    join_record,
    load_legacy_records,
    parse_legacy_entry,
    split_entry,
)
from okto_neuron.consolidate.review_queue_sqlite import (
    SQLITE_FILENAME,
    SqliteQueueStore,
    connect,
    fsync_directory,
    pack_embedding,
)
from okto_neuron.errors import OktoNeuronError

BACKUP_SUFFIX = ".bak-v1"
MIGRATED_SUFFIX = ".migrated"

# Crash-injection seam: tests set this to a callable that raises at a named step.
_FAULT: Callable[[str], None] | None = None


class ReviewQueueMigrationError(OktoNeuronError):
    EXIT_CODE = 1
    default_message = "review queue migration refused"


def _checkpoint(step: str) -> None:
    if _FAULT is not None:
        _FAULT(step)


@dataclass
class MigrationReport:
    action: str
    outcome: str
    dry_run: bool = False
    source_count: int = 0
    migrated_count: int = 0
    nodes: int = 0
    relations: int = 0
    hash_equal: int = 0
    normalized_rows: int = 0
    source_bytes: int = 0
    sqlite_bytes: int = 0
    backup: str | None = None
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _sqlite_family(path: Path) -> list[Path]:
    return [
        candidate
        for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"))
        if candidate.exists()
    ]


def _move_aside(path: Path, label: str) -> Path | None:
    """Rename a SQLite file (and its -wal/-shm) to ``<name>.<label>-<ts>``; never deletes."""

    if not path.exists():
        return None
    _checkpoint_wal(path)
    target = path.with_name(f"{path.name}.{label}-{_stamp()}")
    counter = 1
    while target.exists():
        counter += 1
        target = path.with_name(f"{path.name}.{label}-{_stamp()}.{counter}")
    for member in _sqlite_family(path):
        suffix = member.name[len(path.name):]
        os.replace(member, target.with_name(target.name + suffix))
    fsync_directory(path.parent)
    return target


def _checkpoint_wal(path: Path) -> None:
    try:
        connection = sqlite3.connect(path, timeout=5.0)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        pass


def _ensure_backup(source: Path) -> Path:
    """A verified byte copy of ``source``; an existing identical copy is reused."""

    source_hash = _sha256_file(source)
    index = 1
    while True:
        name = f"{source.name}{BACKUP_SUFFIX}" + ("" if index == 1 else f".{index}")
        target = source.with_name(name)
        if not target.exists():
            break
        if _sha256_file(target) == source_hash:
            return target
        index += 1
    temp = source.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    shutil.copyfile(source, temp)
    try:
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        os.link(temp, target)  # atomic, refuses to overwrite
    finally:
        temp.unlink(missing_ok=True)
    fsync_directory(source.parent)
    if _sha256_file(target) != source_hash:
        raise ReviewQueueMigrationError(f"backup {target.name} does not match the source bytes")
    return target


def _load_source(source: Path) -> tuple[list[dict], list]:
    records = load_legacy_records(source)
    entries = []
    seen: set[str] = set()
    for index, record in enumerate(records):
        try:
            entry = parse_legacy_entry(record)
        except (ValueError, TypeError) as exc:
            raise ReviewQueueMigrationError(
                f"review queue entry #{index} refused ({type(exc).__name__}): {exc}"
            ) from exc
        candidate_id = _entry_id_of(entry)
        if candidate_id in seen:
            raise ReviewQueueMigrationError(f"duplicate candidate id at entry #{index}")
        seen.add(candidate_id)
        entries.append(entry)
    return records, entries


def _entry_id_of(entry) -> str:
    from okto_neuron.consolidate.review_queue import _entry_id

    return _entry_id(entry)


def _build(target: Path, entries: list, *, source_sha256: str, source_count: int) -> None:
    """Write every entry into a fresh SQLite file at ``target`` (state=building)."""

    connection = connect(target, create=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        now = datetime.now(UTC).isoformat(timespec="seconds")
        for entry in entries:
            payload, embedding, digest = split_entry(entry)
            candidate_id = _entry_id_of(entry)
            connection.execute(
                "INSERT INTO entries(candidate_id, kind, reason, created_at, payload, entry_sha256)"
                " VALUES(?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    payload["kind"],
                    entry.reason,
                    now,
                    json.dumps(payload, sort_keys=True, allow_nan=False),
                    digest,
                ),
            )
            if embedding is not None:
                connection.execute(
                    "INSERT INTO embeddings(candidate_id, dim, vec) VALUES(?, ?, ?)",
                    (candidate_id, len(embedding), pack_embedding(embedding)),
                )
        for key, value in (
            ("state", "building"),
            ("source_sha256", source_sha256),
            ("source_count", str(source_count)),
        ):
            connection.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        connection.execute("COMMIT")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()


def _verify(target: Path, entries: list, report: MigrationReport) -> None:
    """Counts equal and every row's digest, recomputed from the SQLite row, equals the source's."""

    store = SqliteQueueStore(target.parent)
    store.path = target
    rows = store.rows(with_embedding=True)
    if len(rows) != len(entries):
        raise ReviewQueueMigrationError(
            f"verification failed: {len(rows)} migrated rows vs {len(entries)} source entries"
        )
    by_id = {row.candidate_id: row for row in rows}
    equal = 0
    for entry in entries:
        row = by_id.get(_entry_id_of(entry))
        if row is None:
            raise ReviewQueueMigrationError("verification failed: a source entry has no row")
        source_digest = entry_digest(entry.to_json())
        if entry_digest(join_record(row)) != source_digest or row.entry_sha256 != source_digest:
            raise ReviewQueueMigrationError(
                f"verification failed: entry_sha256 differs for row seq {row.seq}"
            )
        equal += 1
    report.migrated_count = len(rows)
    report.hash_equal = equal
    report.nodes = sum(1 for row in rows if row.kind == "node")
    report.relations = sum(1 for row in rows if row.kind == "relation")


def migrate(vault_root: Path | str, *, dry_run: bool = False) -> MigrationReport:
    """Move ``review_queue.json`` into SQLite. Idempotent and resumable (see module doc)."""

    started = time.monotonic()
    root = Path(vault_root)
    marginalia = root / ".marginalia"
    source = marginalia / QUEUE_FILENAME
    final = marginalia / SQLITE_FILENAME
    version = vault_yaml_version(root)
    if version is None:
        raise ReviewQueueMigrationError(f"{root} has no readable vault config", vault_path=root)
    report = MigrationReport(action="migrate", outcome="", dry_run=dry_run)

    if not dry_run:
        # A crashed earlier run leaves build garbage; it is never authoritative.
        for stale in marginalia.glob(f"{SQLITE_FILENAME}.tmp-*"):
            for member in (stale, stale.with_name(stale.name + "-wal"), stale.with_name(stale.name + "-shm")):
                member.unlink(missing_ok=True)

    store = SqliteQueueStore(marginalia)
    meta = store.meta() if final.exists() else {}
    complete = meta.get("state") == "complete"

    if version >= CURRENT_YAML_VERSION and complete:
        if not source.exists():
            report.outcome = "already-migrated"
            report.migrated_count = store.count()
            report.sqlite_bytes = final.stat().st_size
            return _done(report, started)
        if _sha256_file(source) == meta.get("source_sha256"):
            # crashed between the atomic replace and the source rename
            if not dry_run:
                _retire_source(source)
            report.outcome = "finished-source-retirement"
            report.migrated_count = store.count()
            return _done(report, started)
        raise ReviewQueueMigrationError(
            f"{QUEUE_FILENAME} exists next to a completed SQLite queue but is not the file "
            "that was migrated; resolve it manually (nothing was changed)",
            file_path=source,
        )

    if version == 1 and final.exists() and not dry_run:
        # yaml 1 means the JSON is the truth; a SQLite file here is stale.
        moved = _move_aside(final, "stale")
        report.notes.append(f"moved stale SQLite queue aside: {moved.name if moved else '-'}")

    if not source.exists():
        # New/empty queue: nothing to convert, just make the layout current.
        report.outcome = "no-source"
        if not dry_run:
            store.set_meta(state="complete", source_count="0", source_sha256="")
            set_vault_yaml_version(root, CURRENT_YAML_VERSION)
        return _done(report, started)

    if final.exists() and not dry_run:
        raise ReviewQueueMigrationError(
            f"{QUEUE_FILENAME} and a live {SQLITE_FILENAME} both exist; refusing to replace "
            "the SQLite queue (nothing was changed)",
            file_path=final,
        )
    records, entries = _load_source(source)
    report.source_count = len(entries)
    report.source_bytes = source.stat().st_size
    source_sha = _sha256_file(source)
    # Diagnostic only: rows whose raw record hashes differently from the
    # normalised entry (e.g. an int where a float is stored). Not a failure.
    report.normalized_rows = sum(
        1
        for record, entry in zip(records, entries, strict=True)
        if entry_digest(record) != entry_digest(entry.to_json())
    )

    if dry_run:
        with tempfile.TemporaryDirectory(prefix="rq-dryrun-") as scratch:
            tmp = Path(scratch) / SQLITE_FILENAME
            _build(tmp, entries, source_sha256=source_sha, source_count=len(entries))
            _verify(tmp, entries, report)
            report.sqlite_bytes = tmp.stat().st_size
        report.outcome = "dry-run-ok"
        return _done(report, started)

    backup = _ensure_backup(source)
    report.backup = backup.name
    _checkpoint("after_backup")
    set_vault_yaml_version(root, CURRENT_YAML_VERSION)
    _checkpoint("after_yaml")
    temp = marginalia / f"{SQLITE_FILENAME}.tmp-{uuid.uuid4().hex}"
    try:
        _build(temp, entries, source_sha256=source_sha, source_count=len(entries))
        _checkpoint("after_build")
        _verify(temp, entries, report)
        _checkpoint("after_verify")
        tmp_store = SqliteQueueStore(marginalia)
        tmp_store.path = temp
        tmp_store.set_meta(
            state="complete",
            migrated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            backup=backup.name,
        )
        _checkpoint_wal(temp)
        os.replace(temp, final)
        for suffix in ("-wal", "-shm"):
            temp.with_name(temp.name + suffix).unlink(missing_ok=True)
        fsync_directory(marginalia)
    except BaseException:
        for member in (temp, temp.with_name(temp.name + "-wal"), temp.with_name(temp.name + "-shm")):
            member.unlink(missing_ok=True)
        raise
    _checkpoint("after_replace")
    _retire_source(source)
    _checkpoint("after_rename")
    report.sqlite_bytes = final.stat().st_size
    report.outcome = "migrated"
    return _done(report, started)


def _retire_source(source: Path) -> None:
    target = source.with_name(source.name + MIGRATED_SUFFIX)
    counter = 1
    while target.exists():
        counter += 1
        target = source.with_name(f"{source.name}{MIGRATED_SUFFIX}.{counter}")
    os.replace(source, target)
    fsync_directory(source.parent)


def _done(report: MigrationReport, started: float) -> MigrationReport:
    report.seconds = round(time.monotonic() - started, 3)
    return report


def rollback(vault_root: Path | str, *, restore_backup: bool = False) -> MigrationReport:
    """Give the vault back to 0.3.1: JSON is the truth again, yaml 1 (set LAST)."""

    started = time.monotonic()
    root = Path(vault_root)
    marginalia = root / ".marginalia"
    source = marginalia / QUEUE_FILENAME
    final = marginalia / SQLITE_FILENAME
    report = MigrationReport(action="rollback-restore-backup" if restore_backup else "rollback", outcome="")
    version = vault_yaml_version(root)
    if version is None:
        raise ReviewQueueMigrationError(f"{root} has no readable vault config", vault_path=root)
    if not final.exists():
        if version == 1:
            report.outcome = "already-rolled-back"
            return _done(report, started)
        if not source.exists():
            raise ReviewQueueMigrationError("nothing to roll back: no SQLite queue and no JSON")
        # crashed after the SQLite file was moved aside: only the yaml is left
        set_vault_yaml_version(root, 1)
        report.outcome = "rolled-back"
        return _done(report, started)

    store = SqliteQueueStore(marginalia)
    rows = store.rows(with_embedding=True)
    meta = store.meta()
    temp = marginalia / f".{QUEUE_FILENAME}.{uuid.uuid4().hex}.tmp"
    try:
        if restore_backup:
            backup = marginalia / str(meta.get("backup", ""))
            if not meta.get("backup") or not backup.exists():
                raise ReviewQueueMigrationError("the recorded .bak-v1 backup is missing")
            if _sha256_file(backup) != meta.get("source_sha256"):
                raise ReviewQueueMigrationError("the backup no longer matches the migrated source")
            shutil.copyfile(backup, temp)
            expected = None
        else:
            payload = {"entries": [join_record(row) for row in rows]}
            encoded = (json.dumps(payload, indent=2, allow_nan=False) + "\n").encode()
            with temp.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            expected = {row.candidate_id: row.entry_sha256 for row in rows}
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        # (a) re-read the file we are about to install and check every digest
        entries = []
        seen = {}
        for index, record in enumerate(load_legacy_records(temp)):
            entry = parse_legacy_entry(record)
            entries.append(entry)
            seen[_entry_id_of(entry)] = entry_digest(entry.to_json())
        if expected is not None and seen != expected:
            raise ReviewQueueMigrationError("rollback verification failed: digests differ from SQLite")
        report.source_count = len(entries)
        report.hash_equal = len(entries) if expected is not None else 0
        if source.exists():
            aside = source.with_name(f"{source.name}.pre-rollback-{_stamp()}")
            os.replace(source, aside)
        _checkpoint("rollback_before_replace")
        os.replace(temp, source)  # (b)
        fsync_directory(marginalia)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    _checkpoint("rollback_after_replace")
    moved = _move_aside(final, "rolled-back")  # (c)
    report.notes.append(f"sqlite kept as {moved.name if moved else '-'}")
    _checkpoint("rollback_after_move")
    set_vault_yaml_version(root, 1)  # (d) LAST
    report.source_bytes = source.stat().st_size
    report.outcome = "rolled-back"
    return _done(report, started)
