"""The server's two bounded off-loop executors (issue #13).

The REST and MCP servers run on ONE asyncio event loop in one thread. Any
synchronous graph read, sidecar/JSON read, YAML load or pool-lock wait inside an
async handler freezes ``/health``, REST and MCP together. Nothing in
``okto_neuron.server`` offloads to the default executor; every blocking call
goes to one of two named pools:

* :class:`StoreExecutor` (``[server] store_workers`` in ``okto-neuron.toml``,
  default 4) via :func:`store_io` / :func:`single_flight`: short store,
  vault-file and config I/O a request or background loop needs (recall's one
  query embedding included).
* :class:`JobExecutor` (``[server] job_workers``, default 2) via
  :func:`job_io`: long-running and model-bound work — curation job runners,
  ingest/remember extraction, answer synthesis, re-embed, and the provider /
  model / CLI probes behind the config page's Test buttons. It is separate so
  a burst of jobs can never starve UI reads, and it is the seam a future worker
  process replaces.

Both pools:

* run work in a copy of the caller's ``contextvars`` context, so the
  request-bound :class:`~okto_neuron.server.state.VaultRuntime` and the request
  id travel with it exactly as they do through ``asyncio.to_thread``;
* count executing calls so shutdown can wait for them (bounded) before vault
  handles close, then cancel anything still queued.

Further:

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
import json
import logging
import os
import threading
import time
import weakref
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Hashable, TypeVar

if TYPE_CHECKING:
    from starlette.responses import Response

_LOG = logging.getLogger("okto_neuron.server.store_io")

DEFAULT_STORE_WORKERS = 4
DEFAULT_JOB_WORKERS = 2

_T = TypeVar("_T")


class BoundedExecutor:
    """A named, bounded thread pool with in-flight accounting."""

    kind = "bounded"

    def __init__(self, max_workers: int) -> None:
        if max_workers < 1:
            raise ValueError(f"{self.kind} executor needs at least one worker")
        self.max_workers = max_workers
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"okto-neuron-{self.kind}",
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
        # Submitted-but-not-finished calls, so shutdown can cancel the ones
        # still queued (a running call cannot be interrupted).
        self._pending: set[concurrent.futures.Future[Any]] = set()
        self._pending_lock = threading.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def busy(self) -> int:
        """Calls executing right now."""
        return self._busy

    async def run(self, fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
        """Run ``fn(*args, **kwargs)`` on a worker and await its result."""
        loop = asyncio.get_running_loop()
        context = contextvars.copy_context()
        call = functools.partial(context.run, self._tracked, loop, fn, args, kwargs)
        future = self._pool.submit(call)
        with self._pending_lock:
            self._pending.add(future)
        future.add_done_callback(self._forget_pending)
        return await asyncio.wrap_future(future, loop=loop)

    def _forget_pending(self, future: concurrent.futures.Future[Any]) -> None:
        with self._pending_lock:
            self._pending.discard(future)

    def cancel_queued(self) -> int:
        """Cancel calls that have not started; running calls are left alone.

        Returns how many were cancelled. A cancelled call's awaiting task gets
        ``CancelledError``; nothing here can (or tries to) stop a call already
        executing on a worker.
        """
        with self._pending_lock:
            pending = tuple(self._pending)
        return sum(1 for future in pending if future.cancel())

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


class StoreExecutor(BoundedExecutor):
    """Short store, vault-file and config I/O (default 4 workers)."""

    kind = "store"

    def __init__(self, max_workers: int = DEFAULT_STORE_WORKERS) -> None:
        super().__init__(max_workers)


class JobExecutor(BoundedExecutor):
    """Long-running jobs, extraction and model work (default 2 workers)."""

    kind = "job"

    def __init__(self, max_workers: int = DEFAULT_JOB_WORKERS) -> None:
        super().__init__(max_workers)


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
    or was not dispatched by :func:`store_io` / :func:`job_io` (the caller runs it directly, as
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


_EXECUTORS: dict[str, BoundedExecutor] = {}
_EXECUTOR_LOCK = threading.Lock()
_FACTORIES: dict[str, Callable[[int], BoundedExecutor]] = {
    "store": StoreExecutor,
    "job": JobExecutor,
}
_DEFAULTS = {"store": DEFAULT_STORE_WORKERS, "job": DEFAULT_JOB_WORKERS}


def configure_executors(
    store_workers: int = DEFAULT_STORE_WORKERS, job_workers: int = DEFAULT_JOB_WORKERS
) -> None:
    """Install the daemon's two pools, shutting down any previous ones."""
    with _EXECUTOR_LOCK:
        previous = list(_EXECUTORS.values())
        _EXECUTORS["store"] = StoreExecutor(store_workers)
        _EXECUTORS["job"] = JobExecutor(job_workers)
    for executor in previous:
        executor.shutdown(wait=False)
    _LOG.info(
        "executors started: store=%d worker(s), job=%d worker(s)", store_workers, job_workers
    )


def _get(kind: str) -> BoundedExecutor:
    # In-process test apps never run ``okto-neuron serve``; they get default
    # pools on first use so handlers behave the same as in the daemon.
    with _EXECUTOR_LOCK:
        executor = _EXECUTORS.get(kind)
        if executor is None or executor.closed:
            executor = _FACTORIES[kind](_DEFAULTS[kind])
            _EXECUTORS[kind] = executor
        return executor


def get_store_executor() -> StoreExecutor:
    return _get("store")  # type: ignore[return-value]


def get_job_executor() -> JobExecutor:
    return _get("job")  # type: ignore[return-value]


def shutdown_executors(*, wait: bool = False) -> None:
    """Shut both pools down (server stop): queued calls are cancelled; calls
    already executing finish on their own thread. Safe to call twice."""
    global _ABANDONED_WORKERS
    with _EXECUTOR_LOCK:
        executors = list(_EXECUTORS.values())
        _EXECUTORS.clear()
    for executor in executors:
        if not wait:
            _ABANDONED_WORKERS += executor.busy
        executor.shutdown(wait=wait)


_ABANDONED_WORKERS = 0


def abandoned_workers() -> int:
    """Worker threads still executing a call when the pools were shut down."""
    return _ABANDONED_WORKERS


def exit_if_workers_abandoned(exit_code: int = 0) -> None:
    """Exit now when a worker is stuck, so it cannot hold the interpreter open.

    ``concurrent.futures`` joins every worker at interpreter exit; a thread
    parked in an LLM or network wait would hang a stop that already closed the
    stores. Call this last, after the stores are closed and the PID file is
    released.
    """
    stuck = _ABANDONED_WORKERS
    if not stuck:
        return
    _LOG.warning("exiting with %d stuck worker thread(s) abandoned", stuck)
    logging.shutdown()
    os._exit(exit_code)


def cancel_queued_work() -> int:
    """Cancel every not-yet-started call on both pools (drain budget exhausted).

    Unlike :func:`shutdown_executors` this keeps the pools registered, so
    :func:`wait_executors_idle` still sees the calls that are executing.
    """
    with _EXECUTOR_LOCK:
        executors = list(_EXECUTORS.values())
    return sum(executor.cancel_queued() for executor in executors)


def _executing() -> int:
    with _EXECUTOR_LOCK:
        executors = list(_EXECUTORS.values())
    return sum(executor.busy for executor in executors)


def wait_executors_idle(timeout: float | None = None) -> bool:
    """Block until no call executes on either pool (or ``timeout``).

    Used before vault handles close so a native store call never runs against
    a closed database. Returns True when idle."""
    with _EXECUTOR_LOCK:
        executors = list(_EXECUTORS.values())
    deadline = None if timeout is None else time.monotonic() + timeout
    for executor in executors:
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not executor.wait_idle(remaining):
            return False
    return True


async def wait_executors_idle_async(timeout: float) -> bool:
    """:func:`wait_executors_idle` for the event loop, without borrowing a thread."""
    deadline = time.monotonic() + timeout
    while _executing():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.02)
    return True


def store_io(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> Awaitable[_T]:
    """Await ``fn(*args, **kwargs)`` on the store executor, off the event loop."""
    return get_store_executor().run(fn, *args, **kwargs)


def job_io(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> Awaitable[_T]:
    """Await long-running ``fn(*args, **kwargs)`` on the job executor."""
    return get_job_executor().run(fn, *args, **kwargs)


def single_flight(
    key: Hashable, fn: Callable[..., _T], /, *args: Any, **kwargs: Any
) -> Awaitable[_T]:
    """Collapse concurrent identical store calls into one execution (see
    :meth:`BoundedExecutor.single_flight`)."""
    return get_store_executor().single_flight(key, fn, *args, **kwargs)


_CHUNK_ITEMS = 32
"""Containers larger than this are encoded element by element."""


def _encode(obj: Any) -> str:
    return json.dumps(
        obj, ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":")
    )


def _encode_chunked(obj: Any) -> str:
    """Same text as :func:`_encode`, built piece by piece for big containers.

    ``json.dumps`` runs in C without releasing the GIL, so encoding a
    multi-megabyte payload in ONE call stalls the event loop for the whole
    encode even on a worker thread. Encoding a big list or dict element by
    element goes through Python bytecode between elements, which lets the loop
    thread take the GIL back every few milliseconds.
    """
    if isinstance(obj, list) and len(obj) > _CHUNK_ITEMS:
        return "[" + ",".join(_encode_chunked(item) for item in obj) + "]"
    if (
        isinstance(obj, dict)
        and len(obj) > 1
        and all(type(key) is str for key in obj)
        and any(isinstance(v, (list, dict)) and len(v) > _CHUNK_ITEMS for v in obj.values())
    ):
        return "{" + ",".join(f"{_encode(k)}:{_encode_chunked(v)}" for k, v in obj.items()) + "}"
    return _encode(obj)


def encode_json(obj: Any) -> bytes:
    """The bytes ``starlette.responses.JSONResponse(obj)`` would render."""
    return _encode_chunked(obj).encode("utf-8")


def encode_op(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> bytes:
    """Run a store op and JSON-encode its result, all on the calling worker."""
    return encode_json(fn(*args, **kwargs))


def json_bytes_response(
    content: bytes, status_code: int = 200, headers: dict[str, str] | None = None
) -> "Response":
    """A response carrying pre-encoded JSON (identical headers to JSONResponse)."""
    from starlette.responses import Response

    return Response(
        content=content,
        status_code=status_code,
        headers=headers,
        media_type="application/json",
    )


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
    "DEFAULT_JOB_WORKERS",
    "DEFAULT_STORE_WORKERS",
    "BoundedExecutor",
    "JobExecutor",
    "StoreExecutor",
    "acquire_off_loop",
    "abandoned_workers",
    "call_soon_on_loop",
    "cancel_queued_work",
    "exit_if_workers_abandoned",
    "encode_json",
    "encode_op",
    "json_bytes_response",
    "configure_executors",
    "get_job_executor",
    "get_store_executor",
    "job_io",
    "shutdown_executors",
    "single_flight",
    "store_io",
    "wait_executors_idle",
    "wait_executors_idle_async",
]
