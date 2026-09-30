"""In-flight call accounting for a store adapter's native database (#22).

Shutdown must tell "a thread is parked in an LLM or network wait" (no database
call running; safe to close around) from "a thread is inside a database
statement or transaction" (closing underneath it is unproven). An adapter wraps
every native call in :meth:`InflightGate.call` and closes through
:meth:`InflightGate.close`, which refuses new calls from the moment it starts
and waits for the running ones.

Adopting it is one line per native call site plus routing ``close()`` through
the gate. Only the grafx adapter does so today; the ladybug and neo4j adapters
need it before the daemon's clean-close path can serve them.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Callable, Iterator


class InflightGate:
    """Counts running native calls; closes only when none is running."""

    def __init__(self, refusal: Callable[[], Exception]) -> None:
        self._refusal = refusal
        self._cond = threading.Condition()
        self._count = 0
        self._closing = False

    @property
    def count(self) -> int:
        """Native calls executing right now."""
        with self._cond:
            return self._count

    @contextlib.contextmanager
    def call(self, *, allow_closed: bool = False) -> Iterator[None]:
        """Count one native call.

        Once :meth:`close` has begun a new call raises ``refusal()``, unless
        ``allow_closed`` (a probe on its own independent connection, which
        still must not overlap the close).
        """
        with self._cond:
            if self._closing and not allow_closed:
                raise self._refusal()
            self._count += 1
        try:
            yield
        finally:
            with self._cond:
                self._count -= 1
                if not self._count:
                    self._cond.notify_all()

    def close(self, closer: Callable[[], None]) -> None:
        """Refuse new calls, wait for running ones, then run ``closer`` once.

        Blocks while a call is running (shutdown decides separately whether to
        give up on that). A second call is a no-op.
        """
        with self._cond:
            if self._closing:
                return
            self._closing = True
            self._cond.wait_for(lambda: self._count == 0)
        closer()
