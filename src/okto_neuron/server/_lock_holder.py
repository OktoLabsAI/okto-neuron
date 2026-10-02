"""Who holds a vault's ``writer_lock``, so a busy answer can say so.

The writer lock is an :class:`asyncio.Lock` held for minutes by an ingest item, an MCP ``remember``
or a curation job. A caller that times out waiting for it (review approve/reject, the fail-fast
curation actions) used to get a bare "busy". The holder records an immutable descriptor on the lock
object at acquire and clears it on release; the busy response reads it.

The descriptor carries only a kind, an id and a time: never a path, a document name or text.
It is bookkeeping for the response, not a second lock: it is written by the task that just
acquired the lock and cleared by the same task before the release, so it needs no synchronisation
of its own (everything runs on the event loop that owns the lock).
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

_ATTR = "_okto_lock_holder"

#: Seconds a client should wait before retrying, by what holds the lock. A review click behind a
#: running ingest item is the common case: items take tens of seconds, jobs minutes.
_RETRY_AFTER_S = {
    "ingest-item": 15,
    "mcp-remember": 15,
    "curation-job": 30,
    "rebuild-job": 30,
    "vault-maintenance": 10,
    "review-op": 5,
}
_DEFAULT_RETRY_AFTER_S = 10


@dataclass(frozen=True)
class LockHolder:
    """Immutable descriptor of the current holder of a writer lock."""

    kind: str
    ident: str
    since_epoch: float
    since_monotonic: float

    def held_for_s(self) -> float:
        return max(0.0, time.monotonic() - self.since_monotonic)

    def retry_after_s(self) -> int:
        return _RETRY_AFTER_S.get(self.kind, _DEFAULT_RETRY_AFTER_S)

    def to_public(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "id": self.ident,
            "since": self.since_epoch,
            "held_for_s": round(self.held_for_s(), 1),
        }


def record_holder(lock: object, kind: str, ident: str = "-") -> LockHolder:
    holder = LockHolder(kind, str(ident)[:80], time.time(), time.monotonic())
    setattr(lock, _ATTR, holder)
    return holder


def clear_holder(lock: object) -> None:
    setattr(lock, _ATTR, None)


def current_holder(lock: object) -> LockHolder | None:
    return getattr(lock, _ATTR, None)


@contextlib.asynccontextmanager
async def held_lock(lock: object, kind: str, ident: str = "-") -> AsyncIterator[None]:
    """``async with lock`` that records who holds it for as long as it is held."""
    async with lock:  # type: ignore[attr-defined]
        record_holder(lock, kind, ident)
        try:
            yield
        finally:
            clear_holder(lock)


def busy_detail(base: str, holder: LockHolder | None) -> str:
    """The human sentence of a busy answer, naming the holder when it is known."""
    if holder is None:
        return base
    return (
        f"{base} (held by {holder.kind} {holder.ident} for {int(holder.held_for_s())} s; "
        f"retry in about {holder.retry_after_s()} s)"
    )
