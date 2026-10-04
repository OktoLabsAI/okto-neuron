"""Durable queue of review actions taken while the vault is busy.

A review action (commit/discard/merge on a parked candidate) needs ``writer_lock`` and the
cross-process semantic writer lease. An ingest item or an MCP ``remember`` holds both through a
whole LLM extraction, which can take minutes, so the review routes do not fail when they find
the locks busy: they store the action here and answer 202. One applier task per vault runtime
applies the stored actions in arrival order once the locks are free, under the same locks the
direct path takes.

Rows live in the vault's ``review_queue.sqlite`` (table ``review_actions``), next to the queue
entries they act on, so they survive a daemon restart; startup restarts the applier for every
vault that still has queued rows.

An action records the queue entry digest (``ReviewQueue.fingerprint``) of its candidate when it
was queued. At apply time a missing entry or a different digest means somebody else already
dealt with the candidate (another action, a job) or it was parked again with new content: the
action is marked ``superseded`` with the reason and is never applied blindly.

Statuses: ``queued`` -> ``applied`` | ``superseded`` | ``failed`` | ``cancelled``. ``claimed_at``
is set while the applier works on a row; cancel only succeeds on an unclaimed queued row, and the
applier's claim only succeeds on a queued row, so the two never both win.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from okto_neuron.consolidate.review_queue_sqlite import SQLITE_FILENAME, _retry_locked, connect
from okto_neuron.server import _integrity as graph_integrity
from okto_neuron.server._lock_holder import clear_holder, record_holder
from okto_neuron.server._store_io import acquire_off_loop, call_soon_on_loop, store_io

if TYPE_CHECKING:
    from okto_neuron.server.state import VaultRuntime

_LOG = logging.getLogger(__name__)

STATUSES = ("queued", "applied", "superseded", "failed", "cancelled")
# Bound of one semantic-lease attempt by the applier; on a timeout it releases writer_lock and
# tries again after ``RETRY_PAUSE_S`` (it never parks a worker thread on the lease).
LEASE_TIMEOUT_S = 5.0
RETRY_PAUSE_S = 2.0
# Granularity of the applier's writer_lock wait, so it notices a shutdown while it waits.
_LOCK_POLL_S = 1.0

REASON_GONE = (
    "the candidate left the review queue before this action ran "
    "(another action or a job resolved it, or it was removed); nothing was changed"
)
REASON_CHANGED = (
    "the candidate changed after this action was queued (it was parked again with "
    "different content); nothing was changed"
)

_DDL = """
CREATE TABLE IF NOT EXISTS review_actions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE,
    candidate_id TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL,
    expected_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('queued', 'applied', 'superseded', 'failed', 'cancelled')),
    reason TEXT,
    claimed_at TEXT,
    finished_at TEXT,
    outcome TEXT
);
CREATE INDEX IF NOT EXISTS review_actions_status_seq ON review_actions(status, seq);
"""
_COLUMNS = (
    "seq, id, candidate_id, action, payload, expected_sha256, created_at, status, reason,"
    " claimed_at, finished_at, outcome"
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class ReviewAction:
    seq: int
    id: str
    candidate_id: str
    action: str
    payload: dict[str, Any]
    expected_sha256: str
    created_at: str
    status: str
    reason: str | None
    claimed_at: str | None
    finished_at: str | None
    outcome: dict[str, Any] | None

    @classmethod
    def from_row(cls, row: tuple) -> "ReviewAction":
        return cls(
            seq=row[0],
            id=row[1],
            candidate_id=row[2],
            action=row[3],
            payload=json.loads(row[4]),
            expected_sha256=row[5],
            created_at=row[6],
            status=row[7],
            reason=row[8],
            claimed_at=row[9],
            finished_at=row[10],
            outcome=json.loads(row[11]) if row[11] else None,
        )

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "candidate_id": self.candidate_id,
            "action": self.action,
            "status": self.status,
            "reason": self.reason,
            "created_at": self.created_at,
            "applying": self.status == "queued" and self.claimed_at is not None,
            "finished_at": self.finished_at,
            "batch_id": self.payload.get("batch_id"),
            "outcome": self.outcome,
        }


class ActionNotCancellable(Exception):
    def __init__(self, action: ReviewAction) -> None:
        super().__init__(f"review action {action.id} is {action.status}")
        self.action = action


class ReviewActionStore:
    """The ``review_actions`` table of one vault's review queue file. One connection per call."""

    def __init__(self, marginalia_dir: Path) -> None:
        self.directory = Path(marginalia_dir)
        self.path = self.directory / SQLITE_FILENAME

    @staticmethod
    def _has_table(connection: sqlite3.Connection) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='review_actions'"
            ).fetchone()
            is not None
        )

    @classmethod
    def _ensure_table(cls, connection: sqlite3.Connection) -> None:
        if not cls._has_table(connection):
            _retry_locked(lambda: connection.executescript(_DDL))

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection | None]:
        """A read connection, or None while no action was ever queued (reads never write)."""
        if not self.path.exists():
            yield None
            return
        connection = connect(self.path, create=False)
        try:
            yield connection if self._has_table(connection) else None
        finally:
            connection.close()

    @contextmanager
    def _writer(self) -> Iterator[sqlite3.Connection]:
        connection = connect(self.path, create=True)
        try:
            self._ensure_table(connection)
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
    def enqueue(
        self,
        candidate_id: str,
        action: str,
        expected_sha256: str,
        payload: dict[str, Any] | None = None,
    ) -> ReviewAction:
        action_id = f"ra_{uuid.uuid4().hex[:16]}"
        with self._writer() as connection:
            connection.execute(
                "INSERT INTO review_actions(id, candidate_id, action, payload, expected_sha256,"
                " created_at, status) VALUES(?, ?, ?, ?, ?, ?, 'queued')",
                (
                    action_id,
                    candidate_id,
                    action,
                    json.dumps(payload or {}, sort_keys=True),
                    expected_sha256,
                    _now(),
                ),
            )
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM review_actions WHERE id = ?", (action_id,)
            ).fetchone()
        return ReviewAction.from_row(row)

    def claim(self, action_id: str) -> bool:
        """Mark a queued row as being applied; False when it is no longer queued (cancelled)."""
        with self._writer() as connection:
            cursor = connection.execute(
                "UPDATE review_actions SET claimed_at = ? WHERE id = ? AND status = 'queued'",
                (_now(), action_id),
            )
            return cursor.rowcount == 1

    def unclaim(self, action_id: str) -> None:
        with self._writer() as connection:
            connection.execute(
                "UPDATE review_actions SET claimed_at = NULL WHERE id = ? AND status = 'queued'",
                (action_id,),
            )

    def finish(
        self,
        action_id: str,
        status: str,
        reason: str | None = None,
        outcome: dict[str, Any] | None = None,
    ) -> None:
        if status not in STATUSES or status == "queued":
            raise ValueError(f"not a terminal review action status: {status!r}")
        with self._writer() as connection:
            connection.execute(
                "UPDATE review_actions SET status = ?, reason = ?, outcome = ?, finished_at = ?"
                " WHERE id = ? AND status = 'queued'",
                (
                    status,
                    reason,
                    json.dumps(outcome, sort_keys=True) if outcome is not None else None,
                    _now(),
                    action_id,
                ),
            )

    def cancel(self, action_id: str) -> ReviewAction | None:
        """Cancel a queued, unclaimed action. None when unknown; ActionNotCancellable otherwise."""
        if not self.path.exists():
            return None
        with self._writer() as connection:
            cursor = connection.execute(
                "UPDATE review_actions SET status = 'cancelled', finished_at = ?,"
                " reason = 'cancelled by the user before it was applied'"
                " WHERE id = ? AND status = 'queued' AND claimed_at IS NULL",
                (_now(), action_id),
            )
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM review_actions WHERE id = ?", (action_id,)
            ).fetchone()
        if row is None:
            return None
        action = ReviewAction.from_row(row)
        if cursor.rowcount != 1:
            raise ActionNotCancellable(action)
        return action

    # -- reads ----------------------------------------------------------------
    def get(self, action_id: str) -> ReviewAction | None:
        with self._reader() as connection:
            if connection is None:
                return None
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM review_actions WHERE id = ?", (action_id,)
            ).fetchone()
        return ReviewAction.from_row(row) if row is not None else None

    def next_queued(self) -> ReviewAction | None:
        """The oldest queued action (arrival order)."""
        with self._reader() as connection:
            if connection is None:
                return None
            row = connection.execute(
                f"SELECT {_COLUMNS} FROM review_actions WHERE status = 'queued'"
                " ORDER BY seq LIMIT 1"
            ).fetchone()
        return ReviewAction.from_row(row) if row is not None else None

    def queued_count(self, before_seq: int | None = None) -> int:
        with self._reader() as connection:
            if connection is None:
                return 0
            if before_seq is None:
                sql, params = "SELECT COUNT(*) FROM review_actions WHERE status = 'queued'", ()
            else:
                sql = "SELECT COUNT(*) FROM review_actions WHERE status = 'queued' AND seq < ?"
                params = (before_seq,)
            return int(connection.execute(sql, params).fetchone()[0])

    def recent(self, *, statuses: tuple[str, ...] = (), limit: int = 50) -> list[ReviewAction]:
        """Newest first, optionally restricted to ``statuses``."""
        with self._reader() as connection:
            if connection is None:
                return []
            where = ""
            params: tuple = ()
            if statuses:
                where = f" WHERE status IN ({', '.join('?' for _ in statuses)})"
                params = tuple(statuses)
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM review_actions{where} ORDER BY seq DESC LIMIT ?",
                (*params, int(limit)),
            ).fetchall()
        return [ReviewAction.from_row(row) for row in rows]


def store_for(runtime: "VaultRuntime") -> ReviewActionStore:
    return ReviewActionStore(Path(runtime.vault_path) / ".marginalia")


def has_queued(runtime: "VaultRuntime") -> bool:
    try:
        return store_for(runtime).queued_count() > 0
    except Exception:  # noqa: BLE001 - a broken queue file must not stop startup
        _LOG.exception(
            "could not read the queued review actions of %s", getattr(runtime, "vault_path", "?")
        )
        return False


# ── the applier ──────────────────────────────────────────────────────────────


def ensure_applier(runtime: "VaultRuntime") -> None:
    """Start the vault's applier task, or tell a running one to look again.

    Safe to call from a store-executor thread (the start is handed back to the event loop).
    """
    if call_soon_on_loop(ensure_applier, runtime):
        return
    task = runtime.review_action_task
    if task is not None and not task.done():
        # The running drain may have just read "nothing queued"; make it read once more.
        runtime.review_action_kick = True
        return
    if runtime.draining:
        return
    runtime.review_action_task = asyncio.ensure_future(_drain(runtime))


async def _drain(runtime: "VaultRuntime") -> None:
    store = store_for(runtime)
    while not runtime.draining:
        runtime.review_action_kick = False
        action = await store_io(store.next_queued)
        if action is None:
            if runtime.review_action_kick:
                continue
            return
        try:
            retry = await _apply_next(runtime, store, action)
        except Exception:  # noqa: BLE001 - the applier must outlive one bad row
            _LOG.exception("review action %s could not be applied", action.id)
            retry = True
        if retry:
            await _pause(runtime, RETRY_PAUSE_S)


async def _pause(runtime: "VaultRuntime", seconds: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not runtime.draining and loop.time() < deadline:
        await asyncio.sleep(min(0.25, max(0.0, deadline - loop.time())))


async def _apply_next(
    runtime: "VaultRuntime", store: ReviewActionStore, action: ReviewAction
) -> bool:
    """Apply ``action`` under writer_lock + the semantic lease. True means "busy, try later"."""
    from okto_neuron.server._integrity import IntegrityFenceError

    writer_lock = runtime.writer_lock
    while True:
        if runtime.draining:
            return False
        try:
            await asyncio.wait_for(writer_lock.acquire(), timeout=_LOCK_POLL_S)
            break
        except asyncio.TimeoutError:
            continue
    record_holder(writer_lock, "review-queue", action.id)
    try:
        lease_context = await acquire_off_loop(runtime.lease_vault)
        with lease_context as vault:
            try:
                await store_io(graph_integrity.require_write_allowed, runtime, vault)
            except IntegrityFenceError as exc:
                await store_io(store.finish, action.id, "failed", f"graph integrity fence: {exc}")
                return False
            return await store_io(_apply_one, runtime, vault, store, action)
    finally:
        clear_holder(writer_lock)
        writer_lock.release()


def _apply_one(
    runtime: "VaultRuntime", vault: Any, store: ReviewActionStore, action: ReviewAction
) -> bool:
    """Store op (writer_lock held): check the candidate state, then apply or supersede."""
    from okto_neuron.companion import ReviewItemNotFoundError
    from okto_neuron.consolidate.ledger import LeaseBusyError
    from okto_neuron.consolidate.review_queue import ReviewQueue
    from okto_neuron.server.http import companion_for

    if not store.claim(action.id):
        return False  # cancelled while it waited for the locks
    current = ReviewQueue(Path(vault.path) / ".marginalia", vault.store).fingerprint(
        action.candidate_id
    )
    if current is None:
        store.finish(action.id, "superseded", REASON_GONE)
        return False
    if current != action.expected_sha256:
        store.finish(action.id, "superseded", REASON_CHANGED)
        return False
    try:
        outcome = companion_for(vault).resolve_review(
            action.candidate_id,
            action.action,  # type: ignore[arg-type]
            lease_timeout=LEASE_TIMEOUT_S,
        )
    except LeaseBusyError:
        store.unclaim(action.id)
        return True
    except ReviewItemNotFoundError:
        store.finish(action.id, "superseded", REASON_GONE)
        return False
    except Exception as exc:  # noqa: BLE001 - recorded on the action, the drain goes on
        _LOG.exception("review action %s failed", action.id)
        store.finish(action.id, "failed", f"{type(exc).__name__}: {exc}"[:500])
        return False
    store.finish(action.id, "applied", outcome=outcome.model_dump(mode="json"))
    return False
