"""Bounded off-loop executor for store, vault-file and config I/O (issue #13).

The REST and MCP servers run on ONE asyncio event loop in one thread. Any
synchronous graph read, sidecar/JSON read, YAML load or pool-lock wait inside an
async handler freezes ``/health``, REST and MCP together. Every such call goes
through :func:`store_io`, which runs it on the daemon's single
:class:`StoreExecutor` instead of the loop.

Design:

* One bounded ``ThreadPoolExecutor`` per daemon (``[server] store_workers`` in
  ``okto-neuron.toml``, default 4). LLM, extraction and embedding offloads keep
  using their own executors so a slow model call cannot starve graph reads.
* Work runs in a copy of the caller's ``contextvars`` context, so the
  request-bound :class:`~okto_neuron.server.state.VaultRuntime` and the request
  id travel with it exactly as they do through ``asyncio.to_thread``.
* :func:`single_flight` collapses concurrent identical calls (same key, same
  event loop) into one execution; every waiter receives the same result or the
  same exception, and a cancelled waiter never cancels the shared execution.
* Code running on a store worker may need to start a loop-owned background task
  (for example ``_jobs.ensure_worker``). :func:`call_soon_on_loop` hands that call
  back to the event loop that dispatched the work.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import functools
import logging
import threading
import weakref
from typing import Any, Awaitable, Callable, Hashable, TypeVar

_LOG = logging.getLogger("okto_neuron.server.store_io")

DEFAULT_STORE_WORKERS = 4

_T = TypeVar("_T")


class StoreExecutor:
    """A bounded thread pool dedicated to store and vault-file work."""

    def __init__(self, max_workers: int = DEFAULT_STORE_WORKERS) -> None:
        if max_workers < 1:
            raise ValueError("store executor needs at least one worker")
        self.max_workers = max_workers
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="okto-neuron-store",
        )
        # Per-loop in-flight table: a future belongs to the loop that created it,
        # and tests (Starlette TestClient) run requests on short-lived loops.
        self._inflight: weakref.WeakKeyDictionary[
            asyncio.AbstractEventLoop, dict[Hashable, asyncio.Future[Any]]
        ] = weakref.WeakKeyDictionary()
        self._closed = False
        # Calls currently executing on a worker. A worker keeps running after
        # its awaiting task is cancelled, so vault close must wait for this to
        # reach zero or a native store call can run against a closed database.
        self._busy = 0
        self._idle = threading.Condition()

    @property
    def closed(self) -> bool:
        return self._closed

    async def run(self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        """Run ``fn(*args, **kwargs)`` on a store worker and await its result."""
        loop = asyncio.get_running_loop()
        context = contextvars.copy_context()
        call = functools.partial(context.run, self._tracked, loop, fn, args, kwargs)
        return await loop.run_in_executor(self._pool, call)

    def _tracked(
        self,
        loop: asyncio.AbstractEventLoop,
        fn: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        with self._idle:
            self._busy += 1
        try:
            return _run_dispatched(loop, fn, args, kwargs)
        finally:
            with self._idle:
                self._busy -= 1
                if not self._busy:
                    self._idle.notify_all()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until no call is executing (or ``timeout``); True when idle."""
        with self._idle:
            return self._idle.wait_for(lambda: self._busy == 0, timeout=timeout)

    async def single_flight(
        self, key: Hashable, fn: Callable[..., _T], /, *args: Any, **kwargs: Any
    ) -> _T:
        """Run ``fn`` once for every concurrent caller that passes the same ``key``.

        The first caller starts the execution; callers that arrive while it is in
        flight await the same result (or exception). The execution is shielded,
        so one waiter's cancellation never aborts it for the others.
        """
        loop = asyncio.get_running_loop()
        table = self._inflight.setdefault(loop, {})
        future = table.get(key)
        if future is None:
            future = asyncio.ensure_future(self.run(fn, *args, **kwargs))
            table[key] = future

            def _forget(done: asyncio.Future[Any], *, _key: Hashable = key) -> None:
                if table.get(_key) is done:
                    del table[_key]
                if not done.cancelled():
                    # Consume the exception so an execution nobody awaits any
                    # more (every waiter was cancelled) is not logged as lost.
                    done.exception()

            future.add_done_callback(_forget)
        return await asyncio.shield(future)

    def shutdown(self, *, wait: bool = False) -> None:
        """Stop accepting work and cancel queued (not yet running) calls."""
        self._closed = True
        self._pool.shutdown(wait=wait, cancel_futures=True)


_dispatch = threading.local()


def _run_dispatched(
    loop: asyncio.AbstractEventLoop,
    fn: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    previous = getattr(_dispatch, "loop", None)
    _dispatch.loop = loop
    try:
        return fn(*args, **kwargs)
    finally:
        _dispatch.loop = previous


def call_soon_on_loop(fn: Callable[..., Any], /, *args: Any) -> bool:
    """Schedule ``fn(*args)`` on the dispatching event loop when called off-loop.

    Returns ``True`` when the call was handed to the loop (the caller must not
    run it itself), ``False`` when the caller is already on an event loop thread
    or was not dispatched by :func:`store_io` (the caller runs it directly, as
    before).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return False
    loop = getattr(_dispatch, "loop", None)
    if loop is None or loop.is_closed():
        return False
    loop.call_soon_threadsafe(fn, *args)
    return True


_EXECUTOR: StoreExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()


def configure_store_executor(max_workers: int = DEFAULT_STORE_WORKERS) -> StoreExecutor:
    """Install the daemon's executor, replacing (and shutting down) any previous one."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        previous = _EXECUTOR
        _EXECUTOR = StoreExecutor(max_workers)
    if previous is not None:
        previous.shutdown(wait=False)
    _LOG.info("store executor started with %d worker(s)", max_workers)
    return _EXECUTOR


def get_store_executor() -> StoreExecutor:
    """Return the daemon's executor, lazily creating a default one.

    In-process test apps never run ``okto-neuron serve``; they get a default
    executor on first use so handlers behave the same as in the daemon.
    """
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None or _EXECUTOR.closed:
            _EXECUTOR = StoreExecutor(DEFAULT_STORE_WORKERS)
        return _EXECUTOR


def shutdown_store_executor(*, wait: bool = False) -> None:
    """Shut the daemon's executor down (server stop). Safe to call twice."""
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        executor, _EXECUTOR = _EXECUTOR, None
    if executor is not None:
        executor.shutdown(wait=wait)


def wait_store_idle(timeout: float | None = None) -> bool:
    """Wait for in-flight store calls to finish before closing vault handles.

    Returns True when no call is executing (also when no executor exists)."""
    with _EXECUTOR_LOCK:
        executor = _EXECUTOR
    return True if executor is None else executor.wait_idle(timeout)


def store_io(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> Awaitable[_T]:
    """Await ``fn(*args, **kwargs)`` on the store executor, off the event loop."""
    return get_store_executor().run(fn, *args, **kwargs)


def single_flight(
    key: Hashable, fn: Callable[..., _T], /, *args: Any, **kwargs: Any
) -> Awaitable[_T]:
    """Collapse concurrent identical store calls into one execution (see
    :meth:`StoreExecutor.single_flight`)."""
    return get_store_executor().single_flight(key, fn, *args, **kwargs)


def _exit_context(resource: Any) -> None:
    resource.__exit__(None, None, None)


async def acquire_off_loop(
    fn: Callable[..., _T],
    /,
    *args: Any,
    release: Callable[[_T], Any] = _exit_context,
) -> _T:
    """:func:`store_io` for a call that hands back an owned resource (a vault lease).

    If the awaiting task is cancelled after the worker already acquired the
    resource, it is released as soon as the worker returns instead of leaking
    (a leaked lease would block vault deletion and maintenance forever).
    ``release`` defaults to exiting the resource as a context manager.
    """
    future = asyncio.ensure_future(store_io(fn, *args))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:

        def _release_orphan(done: asyncio.Future[_T]) -> None:
            if done.cancelled() or done.exception() is not None:
                return
            try:
                release(done.result())
            except Exception:  # noqa: BLE001 - best-effort cleanup of an orphan
                _LOG.exception("could not release a resource acquired after cancellation")

        future.add_done_callback(_release_orphan)
        raise


__all__ = [
    "DEFAULT_STORE_WORKERS",
    "acquire_off_loop",
    "StoreExecutor",
    "call_soon_on_loop",
    "configure_store_executor",
    "get_store_executor",
    "shutdown_store_executor",
    "single_flight",
    "store_io",
    "wait_store_idle",
]
