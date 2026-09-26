"""Backend-neutral optimistic-write-conflict retry helper (M4 spec §2, D-10).

A backend whose writes can lose a concurrent-write race under MVCC (Grafx
today; a future Neo4j/Neptune backend later) needs the same shape of retry:
call a read-modify-write operation, back off and retry on a losing commit,
give up after a bounded number of attempts or a bounded elapsed time. This
module owns that shape once so no backend reimplements its own backoff loop.

Deliberately takes no dependency on any backend's exception types, nor on
``okto_neuron.config`` -- the caller supplies ``is_retryable`` and a
duck-typed ``policy`` (anything exposing ``max_attempts``/
``backoff_base_ms``/``backoff_cap_ms``/``total_cap_s``, e.g.
``okto_neuron.config._vault.RetryConfig``), so this stays reusable by a
future backend without importing this one's config shape.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Protocol, TypeVar

T = TypeVar("T")


class RetryPolicy(Protocol):
    """The four knobs :func:`retry_with_backoff` needs.

    Structural only (no ``@runtime_checkable``, never used with
    ``isinstance``) -- any object with these attributes works, including
    ``okto_neuron.config._vault.RetryConfig``.
    """

    max_attempts: int
    backoff_base_ms: int
    backoff_cap_ms: int
    total_cap_s: float


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy,
    is_retryable: Callable[[Exception], bool],
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``fn()``, retrying on a retryable exception with full-jitter backoff.

    "Full jitter" (AWS's "Exponential Backoff And Jitter"): each retry's
    delay is drawn uniformly from ``[0, min(backoff_cap_ms, backoff_base_ms *
    2 ** (attempts_so_far - 1))]`` milliseconds -- spreading concurrent
    retriers apart instead of having them collide again in lockstep.

    A retry is taken only while all three hold: the exception is retryable
    per ``is_retryable``, fewer than ``policy.max_attempts`` attempts have
    run, and less than ``policy.total_cap_s`` wall-clock seconds have
    elapsed since the first attempt -- either cap can end the loop before
    the other does, so a small ``backoff_base_ms``/``backoff_cap_ms``
    pairing can't spin past the time budget even with attempts to spare.

    ``fn`` is called fresh on every attempt, never memoized -- a caller
    doing read-modify-write must re-read *inside* ``fn`` itself, so a
    decision cached from a losing prior attempt is never reused (exactly the
    bug a naive retry-the-same-write loop would have).

    Whatever exception the final attempt raises propagates unchanged: the
    bare ``raise`` inside each ``except`` block preserves that exception's
    own traceback rather than wrapping it in a synthetic "retries
    exhausted" error, so a caller sees the real failure.
    """
    started = time.monotonic()
    attempts = 0
    while True:
        attempts += 1
        try:
            return fn()
        except Exception as exc:
            if not is_retryable(exc):
                raise
            if attempts >= policy.max_attempts:
                raise
            if time.monotonic() - started >= policy.total_cap_s:
                raise
            ceiling_ms = min(policy.backoff_cap_ms, policy.backoff_base_ms * (2 ** (attempts - 1)))
            delay_ms = random.uniform(0, ceiling_ms) if ceiling_ms > 0 else 0.0
            sleep(delay_ms / 1000)


__all__ = ["RetryPolicy", "retry_with_backoff"]
