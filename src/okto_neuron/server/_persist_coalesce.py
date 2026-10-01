"""Coalesced background persistence for the ingest queue and the curation jobs (#37).

Both sidecars are rewritten whole on every persist. A burst of progress events used to
trigger hundreds of those rewrites, some of them inside the companion's event lock. Here
a producer only marks the queue dirty (cheap, any thread); ONE flush per queue runs on a
store worker at most every ``interval`` seconds. Transitions that must be durable (enqueue,
an item's terminal status, cancel, daemon shutdown) still call the real persist, which
also clears the dirty flag.

A crash can therefore lose at most ``interval`` seconds of progress EVENTS, never a state
transition.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable

from okto_neuron.server._store_io import call_soon_on_loop, store_io

_LOG = logging.getLogger("okto_neuron.server.persist_coalesce")

DEFAULT_INTERVAL_S = 2.0


class PersistCoalescer:
    """Dirty flag plus at most one pending background flush."""

    def __init__(
        self, flush: Callable[[], None], *, name: str, interval: float = DEFAULT_INTERVAL_S
    ) -> None:
        self._flush = flush
        self.name = name
        self.interval = interval
        self._lock = threading.Lock()
        self._dirty = False
        self._scheduled = False
        self._closed = False
        self._task: asyncio.Task[None] | None = None

    @property
    def dirty(self) -> bool:
        return self._dirty

    def flushed(self) -> None:
        """Called by every real persist, before it snapshots: what came before is covered."""
        with self._lock:
            self._dirty = False

    def mark_dirty(self) -> None:
        """Record that the sidecar is stale; make sure a flush is coming. Any thread."""
        with self._lock:
            self._dirty = True
            write_now = self._closed
            schedule = not (self._closed or self._scheduled)
            if schedule:
                self._scheduled = True
        if write_now:
            self._flush()
        elif schedule and not self._start():
            # Neither on an event loop nor dispatched by one (a plain thread or a
            # synchronous caller): nobody to wait for, so write now.
            with self._lock:
                self._scheduled = False
            self._flush()

    def _start(self) -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return call_soon_on_loop(self._spawn)
        self._spawn()
        return True

    def _spawn(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name=f"okto-neuron-persist-{self.name}"
            )

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval)
                with self._lock:
                    if not self._dirty or self._closed:
                        self._scheduled = False
                        return
                    self._dirty = False  # marks that land during the write re-dirty it
                try:
                    await store_io(self._flush)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - a persist failure never aborts an ingest
                    _LOG.warning("coalesced %s persist failed", self.name, exc_info=True)
        finally:
            with self._lock:
                if self._task is asyncio.current_task():
                    self._scheduled = False
                    self._task = None

    def close(self) -> bool:
        """Stop the background flush and write once more if anything is pending.

        Returns True when a final write ran. Later ``mark_dirty`` calls write at once.
        """
        with self._lock:
            self._closed = True
            task, pending = self._task, self._dirty
            self._dirty = False
        if task is not None and not task.done():
            task.cancel()
        if pending:
            self._flush()
        return pending
