"""Optional MLflow GenAI trace export for every Okto Neuron LLM call.

WHY THIS EXISTS
---------------
Okto Neuron already records per-call facts in three narrow places: the
``litellm usage ...`` INFO log line, the per-thread ``last_call_stats()``
channel, and the ingest inspector's ``llm_request``/``llm_response`` events.
None of them is queryable across runs, and none of them carries the full
picture (effective request params AND prompt AND response AND finish-reason
forensics AND latency) in one record. This module exports exactly that, once
per ``complete()`` call, as an MLflow GenAI trace.

THE THREE RULES THIS MODULE OBEYS
---------------------------------
1. **Env-gated.** Everything keys off ``OKTO_NEURON_MLFLOW_TRACKING_URI``.
   When it is unset, ``enabled()`` is False, :func:`record` returns on its
   first branch, and ``mlflow`` is never imported. There is no other way to
   turn this on — no config key, no implicit default — so an operator who has
   not opted in pays nothing, not even an import.
2. **Fail-open.** A tracking server that is down, an mlflow version whose
   internals moved, a value that will not serialize: each is an observability
   problem, never a reason to fail or delay an operator's LLM call. Every
   failure path here logs at most one warning per process and returns.
3. **Off the critical path.** :func:`record` does no I/O. It hands an
   already-built, already-JSON-safe dict to a bounded queue drained by one
   daemon thread. When that queue is full the record is DROPPED, on purpose:
   blocking a completion to make room for its own telemetry would be the
   exact latency regression rule 2 forbids.

WHY THE CLIENT API AND NOT ``mlflow.start_span``
------------------------------------------------
The fluent tracing API binds a span to the *calling* thread's context. The
span is emitted from the drain thread, long after the call returned, so there
is no context to bind to and the timings would be the drain thread's, not the
model's. ``MlflowClient.start_trace``/``end_trace`` take explicit
``start_time_ns``/``end_time_ns``, which is the only way to report the real
wall-clock window of a call that has already finished.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import queue
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator
from okto_neuron._compat import TELEMETRY_ATTRIBUTE_PREFIX
from okto_neuron._compat import getenv as _compat_getenv

logger = logging.getLogger("okto_neuron.llm.telemetry")

ENV_TRACKING_URI = "OKTO_NEURON_MLFLOW_TRACKING_URI"
ENV_EXPERIMENT = "OKTO_NEURON_MLFLOW_EXPERIMENT"
#: A JSON object of extra tags stamped on EVERY span this process exports.
#:
#: The span's own tags (provider, model, step, degraded reason) say what the
#: call was. They cannot say what the RUN was, because the code making the
#: call does not know — a daemon spawned by a benchmark arm has no idea which
#: arm it is. The spawner does, and this is how it says so, in the same place
#: it already says where to export: the child's environment. Without it, one
#: experiment holding every arm would be unsliceable, and the alternative
#: (an experiment per arm) breaks every cross-arm query.
ENV_TAGS = "OKTO_NEURON_MLFLOW_TAGS"

#: Fallback experiment when ``OKTO_NEURON_MLFLOW_EXPERIMENT`` is unset. The
#: LoCoMo harness sets the env var to ``locomo``; ordinary daemon traffic
#: lands here.
DEFAULT_EXPERIMENT = "okto-neuron"

#: Bounded so a wedged tracking server cannot grow memory without limit. A
#: thousand pending spans is far more than any real backlog and still small.
_QUEUE_MAXSIZE = 1000

_STATE_LOCK = threading.Lock()
_QUEUE: queue.Queue | None = None
_WORKER: threading.Thread | None = None
_WARNED = False

# Resolved lazily ON THE DRAIN THREAD, never in ``record``: building an
# MlflowClient touches the network and config files, which is precisely the
# work rule 3 keeps off the caller.
_CLIENT: Any = None
_EXPERIMENT_ID: str | None = None
_CLIENT_FAILED = False

# Serialises the ONE-TIME client/experiment resolution in ``_client()``. Its own
# lock, not ``_STATE_LOCK``: the failure path calls ``_warn_once``, which takes
# ``_STATE_LOCK``, and ``threading.Lock`` is not reentrant.
#
# Needed because ``_client()`` is not only called from the single drain thread:
# ``trace_parent`` and ``trace_child`` resolve it synchronously on the CALLER's
# thread. Unserialised, N threads making a process's first traced call at once
# all saw no experiment, all called ``create_experiment``, and every loser got
# ``RESOURCE_ALREADY_EXISTS`` — which the except-branch below treated as a dead
# server, disabling export for the whole process. Measured before the fix, on a
# live server: 12 simultaneous first calls, 0 of 12 traces landed, 3 runs of 3.
_CLIENT_LOCK = threading.Lock()

# Bumped by ``_reset_for_tests``. A resolution publishes its result only if the
# generation it started under is still current, so a resolver still running
# from before a reset — a detached drain thread retrying a dead server, which
# ``_reset_for_tests`` cannot stop — can never write ``_CLIENT_FAILED`` into the
# state of whatever runs after it. Production never resets, so it never moves.
_CLIENT_GENERATION = 0

#: The span a call made on THIS thread should attach to, or ``None`` for a
#: call that is its own root trace.
#:
#: Thread-local, not a contextvar, for the same reason ``_call_step`` is: the
#: daemon runs each ``ask`` on its own ``asyncio.to_thread`` worker, so two
#: concurrent questions must never adopt each other's parent.
_parent = threading.local()


def current_parent() -> tuple[str, str] | None:
    """``(trace_id, span_id)`` of the span calls on this thread belong under."""
    return getattr(_parent, "ids", None)


def bind_parent(fn: Any) -> Any:
    """Wrap ``fn`` so it runs under THIS thread's parent span on another one.

    A thread-local parent is invisible to a worker thread, so every LLM call a
    pool makes would export as its own root trace and the operation that
    submitted the work would show up missing exactly the calls that did it.
    Ingest fans out in three places — extraction always, type adjudication
    always, curation as soon as its concurrency or batch knobs leave 1 — so
    without this an `ingest` trace would be empty at default config the moment
    a document had more than one block.

    Captures the parent at SUBMIT time, on the submitting thread, and
    reinstalls it for the duration of the call. Returns ``fn`` unchanged when
    there is no parent to propagate, so a non-traced run keeps the exact
    callable it had, and wrapping is safe to leave at a submit site.
    """
    parent = current_parent()
    if parent is None:
        return fn

    def _with_parent(*args: Any, **kwargs: Any) -> Any:
        previous = getattr(_parent, "ids", None)
        _parent.ids = parent
        try:
            return fn(*args, **kwargs)
        finally:
            _parent.ids = previous

    return _with_parent


def tracking_uri() -> str | None:
    """The configured MLflow tracking URI, or ``None`` when telemetry is off."""
    raw = _compat_getenv(ENV_TRACKING_URI)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def experiment_name() -> str:
    """The MLflow experiment traces are written to."""
    raw = _compat_getenv(ENV_EXPERIMENT)
    if raw is not None and raw.strip():
        return raw.strip()
    return DEFAULT_EXPERIMENT


def static_tags() -> dict[str, str]:
    """Process-wide extra span tags from ``OKTO_NEURON_MLFLOW_TAGS``.

    Fail-open like everything else here: malformed JSON, a JSON array, a
    nested object — each yields no tags and one warning, never an exception
    into a completion. Values are coerced to ``str`` because MLflow tags are
    strings and a silently dropped integer tag is worse than a stringified
    one. Read per span rather than cached, so the same ``monkeypatch.setenv``
    story that controls the rest of this module controls this too.
    """
    raw = _compat_getenv(ENV_TAGS)
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise TypeError(f"{ENV_TAGS} must be a JSON object, got {type(parsed).__name__}")
        return {str(k): str(v) for k, v in parsed.items() if v is not None}
    except Exception:  # noqa: BLE001 - rule 2
        _warn_once("Ignoring malformed %s; continuing without those tags", ENV_TAGS, exc_info=True)
        return {}


def enabled() -> bool:
    """True when an operator has opted into MLflow export.

    This is THE gate. It is read on every ``complete()`` so an operator can
    turn export on or off for a long-lived daemon by changing the environment
    of a restart, and so a test can toggle it with ``monkeypatch.setenv``
    without reaching into module state.
    """
    return tracking_uri() is not None


#: What an operator runs to get the missing dependency. The installer adds the
#: ``telemetry`` extra when this is set (or when the tracking URI is).
_MISSING_MLFLOW_FIX = "re-run the Okto Neuron installer with OKTO_NEURON_TELEMETRY=1"
_MISSING_MLFLOW_REPORTED = False


def _missing_mlflow_message(uri: str) -> str:
    return (
        f"{ENV_TRACKING_URI} is set ({uri}) but mlflow is not installed, so no "
        f"LLM call will be traced. Fix: {_MISSING_MLFLOW_FIX} (adds the "
        "'telemetry' extra), then restart the daemon."
    )


def missing_mlflow_warning() -> str | None:
    """The startup warning for "export requested, but mlflow is not installed".

    Without it that state looked like success: nothing was said until the first
    LLM call, and then as "... talking to <uri> (... check the server)" with a
    traceback, which points at the server rather than the missing package.
    Uses ``find_spec`` so the check stays free of mlflow's import cost; a
    broken-but-present install is still caught (and warned about) lazily.
    Returns ``None`` when telemetry is off or mlflow is present. Marks the
    condition reported, so the lazy path does not warn a second time.
    """
    global _MISSING_MLFLOW_REPORTED
    uri = tracking_uri()
    if uri is None:
        return None
    try:
        missing = importlib.util.find_spec("mlflow") is None
    except (ImportError, ValueError):
        missing = True
    if not missing:
        return None
    _MISSING_MLFLOW_REPORTED = True
    return _missing_mlflow_message(uri)


def _warn_once(message: str, *args: object, **kwargs: Any) -> None:
    """First failure warns; every later one drops to DEBUG.

    ``**kwargs`` forwards ``exc_info=True``. It is not decoration: without it
    every call site that passes a traceback raised ``TypeError`` INSIDE the
    drain thread's own except-handler, turning a swallowed export failure into
    an unhandled thread exception.
    """
    global _WARNED
    with _STATE_LOCK:
        if _WARNED:
            logger.debug(message, *args, **kwargs)
            return
        _WARNED = True
    logger.warning(message, *args, **kwargs)


def _jsonable(value: object, *, depth: int = 0) -> object:
    """JSON-safe copy of one attribute value, bounded in depth.

    Mirrors ``okto_neuron.llm._jsonable`` deliberately rather than importing
    it: this module must stay importable without pulling the whole provider
    module in, and the depth bound is specific to span attributes, which
    MLflow serializes eagerly.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if depth >= 6:
        return repr(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v, depth=depth + 1) for v in value]
    return repr(value)


def _ensure_worker() -> queue.Queue | None:
    global _QUEUE, _WORKER
    with _STATE_LOCK:
        if _QUEUE is None:
            _QUEUE = queue.Queue(maxsize=_QUEUE_MAXSIZE)
        if _WORKER is None or not _WORKER.is_alive():
            worker = threading.Thread(
                target=_drain,
                args=(_QUEUE,),
                name="okto-neuron-mlflow-telemetry",
                daemon=True,
            )
            _WORKER = worker
            worker.start()
        return _QUEUE


def _drain(pending: queue.Queue) -> None:
    # Takes its queue as an argument rather than reading the global: a worker
    # must keep draining the queue it was started for, even if the global is
    # later swapped.
    while True:
        record = pending.get()
        try:
            if record is not None:
                _emit(record)
        except Exception:  # noqa: BLE001 - rule 2: never escape the drain loop
            _warn_once(
                "MLflow telemetry export failed; continuing without it "
                "(further failures log at DEBUG)",
                exc_info=True,
            )
        finally:
            pending.task_done()


def _is_already_exists(exc: BaseException) -> bool:
    """Whether ``exc`` is MLflow saying the experiment name is already taken.

    Keyed on MLflow's error code rather than the exception class, because the
    REST client raises ``RestException`` and the local stores raise
    ``MlflowException``; both carry ``error_code``. The message check is the
    fallback for a store that sets neither consistently.
    """
    code = getattr(exc, "error_code", None)
    return code == "RESOURCE_ALREADY_EXISTS" or "RESOURCE_ALREADY_EXISTS" in str(exc)


def _client() -> tuple[Any, str] | None:
    """The MlflowClient and experiment id, resolved once per process.

    Called from the drain thread AND synchronously from caller threads
    (``trace_parent``/``trace_child``), so the one-time resolution is
    double-checked under ``_CLIENT_LOCK``: the warm path is two global reads,
    and only the first use takes the lock.
    """
    if _CLIENT_FAILED:
        return None
    if _CLIENT is not None and _EXPERIMENT_ID is not None:
        return _CLIENT, _EXPERIMENT_ID
    with _CLIENT_LOCK:
        return _resolve_client_locked()


def _resolve_client_locked() -> tuple[Any, str] | None:
    """Build the client and resolve the experiment. Caller holds ``_CLIENT_LOCK``."""
    global _CLIENT, _EXPERIMENT_ID, _CLIENT_FAILED
    # Re-check: another thread may have finished (or failed) while we waited.
    if _CLIENT_FAILED:
        return None
    if _CLIENT is not None and _EXPERIMENT_ID is not None:
        return _CLIENT, _EXPERIMENT_ID
    generation = _CLIENT_GENERATION
    uri = tracking_uri()
    if uri is None:
        return None
    try:
        import mlflow  # lazy — optional dep, rule 1
        from mlflow import MlflowClient

        # The PROCESS-WIDE tracking URI, not only the client's, and this is
        # not belt-and-braces. ``client.start_trace`` writes through the
        # client, but the span itself is shipped by MLflow's global async
        # trace exporter, which reads ``mlflow.get_tracking_uri()`` and
        # ignores the client's. Constructing only the client silently created
        # a local ``mlflow.db`` in the working directory and every span failed
        # with "No Experiment with id=1 exists" — verified 2026-09-20.
        mlflow.set_tracking_uri(uri)
        client = MlflowClient(tracking_uri=uri)
        name = experiment_name()
        experiment = client.get_experiment_by_name(name)
        if experiment is not None:
            experiment_id = experiment.experiment_id
        else:
            try:
                experiment_id = client.create_experiment(name)
            except Exception as exc:  # noqa: BLE001
                # Another PROCESS created it between our lookup and our create
                # (the lock above only serialises threads in this one): a
                # benchmark harness and the daemons it spawns start against the
                # same new experiment. The name is taken, which is exactly the
                # state we wanted, so adopt it rather than failing.
                if not _is_already_exists(exc):
                    raise
                existing = client.get_experiment_by_name(name)
                if existing is None:
                    raise
                experiment_id = existing.experiment_id
    except Exception as exc:  # noqa: BLE001
        # One hard failure (mlflow not installed, server unreachable, auth)
        # marks the client dead for the process rather than retrying a network
        # round-trip per call. The message names the actual exception: the old
        # text said "could not reach <uri>" for EVERY failure, including the
        # duplicate-experiment race, where the server had answered fine.
        if generation != _CLIENT_GENERATION:
            return None  # reset while we were in flight: not ours to publish
        _CLIENT_FAILED = True
        if isinstance(exc, ImportError) and (exc.name or "").split(".", 1)[0] == "mlflow":
            if _MISSING_MLFLOW_REPORTED:
                logger.debug("MLflow telemetry disabled: mlflow is not installed")
            else:
                _warn_once("%s", _missing_mlflow_message(uri))
            return None
        _warn_once(
            "MLflow telemetry disabled for this process: %s talking to %s "
            "(install the 'telemetry' extra and check the server)",
            type(exc).__name__,
            uri,
            exc_info=True,
        )
        return None
    if generation != _CLIENT_GENERATION:
        return None  # reset while we were in flight: not ours to publish
    _CLIENT = client
    _EXPERIMENT_ID = str(experiment_id)
    return _CLIENT, _EXPERIMENT_ID


#: Span status for a call that RETURNED but whose answer is degraded —
#: truncated, abnormally stopped, or empty.
#:
#: MLflow's span vocabulary has exactly three codes (``SpanStatusCode``:
#: ``UNSET``/``OK``/``ERROR``), and ``UNSET`` looks like the natural home for
#: "returned, but we are not claiming success". IT IS NOT AVAILABLE: OpenTelemetry's
#: SDK SILENTLY IGNORES an attempt to set ``UNSET``
#: (``opentelemetry/sdk/trace/__init__.py:1008-1016``, "Ignore calls to set to
#: StatusCode.UNSET"), so ``end_trace(status="UNSET")`` is a no-op and the span
#: keeps the ``OK`` its root started with. Verified against the live server on
#: 2026-09-20: a real ``finish_reason="length"`` ask landed as ``state=OK``
#: with only the degraded attributes to show for it.
#:
#: So a degraded call is ``ERROR``. Between a status that overstates the
#: problem and one that hides it, the rule is not symmetric: an empty or
#: degraded answer is never a success, and a span that says OK while the
#: answer was truncated is the exact bug this exists to close.
#: ``marginalia.degraded_reason`` keeps the two apart —
#: ``provider_error`` means the call raised, the other three mean it returned
#: something unusable.
_DEGRADED_SPAN_STATUS = "ERROR"


def degradation_reason(record: dict[str, Any]) -> str | None:
    """Why this call must NOT be reported as a clean success — or ``None``.

    WHY THIS EXISTS
    ---------------
    A benchmark run persisted 304 answers as ``status="ok"`` with
    ``finish_reason="stop"`` while the ask trace said
    ``synthesis_status="provider_error"``. The same blind spot existed here:
    the span status was ``"ERROR" if record.get("error") else "OK"``, so a
    completion that returned truncated, abnormally stopped, or empty text was
    tagged a clean success in the MLflow UI. An empty or degraded answer is
    never a success.

    WHY IT DOES NOT READ ``synthesis_status``
    -----------------------------------------
    ``synthesis_status`` is stamped by the companion's ask path
    (``companion/__init__.py`` ``_mark_provider_error`` / ``_mark_finish_reason``)
    on the retrieval trace, one level ABOVE this wrapper and only for ``ask``.
    The telemetry layer never sees it, and must not: it instruments EVERY
    ``complete()`` (curation, judging, CLI providers), not just ask. It derives
    the same verdict from the same underlying signals — the exception, the
    finish reason, the returned text — so every LLM call gets the honesty the
    ask path gets, and the vocabulary is deliberately identical to the
    companion's (``provider_error`` / ``truncated`` / ``abnormal_stop`` /
    ``empty``) so both can be filtered with one mental model.

    Precedence mirrors ``_mark_finish_reason``: ``provider_error`` (the call
    raised) > ``truncated`` (``length``: text exists but is cut off) >
    ``abnormal_stop`` (any other non-``stop`` reason, plus a ``stop`` litellm
    normalized from an unmapped/known-abnormal native reason) > ``empty``.

    A provider that reports NO finish reason at all (the CLI pseudo-providers,
    ``finish_reason_available=False``) is NOT degraded on that ground alone:
    absent is already stated by that attribute, and flagging every CLI call
    would make everything look broken while saying nothing new. An empty answer
    from one still reports ``empty``.
    """
    if record.get("error") is not None:
        return "provider_error"
    finish_reason = record.get("finish_reason")
    if finish_reason == "length":
        return "truncated"
    if record.get("finish_reason_unmapped") or (
        isinstance(finish_reason, str) and finish_reason != "stop"
    ):
        return "abnormal_stop"
    response = record.get("response")
    if not (isinstance(response, str) and response.strip()):
        # A tool-call turn legitimately carries no prose. Calling that "empty"
        # would be the inverse error: inventing a failure.
        if record.get("tool_calls"):
            return None
        return "empty"
    return None


def _emit(record: dict[str, Any]) -> None:
    resolved = _client()
    if resolved is None:
        return
    client, experiment_id = resolved
    attributes: dict[str, Any] = {
        # MLflow's own GenAI keys, so the UI renders model/provider/usage in
        # its dedicated columns instead of as anonymous custom attributes.
        "mlflow.llm.model": record.get("model"),
        "mlflow.llm.provider": record.get("provider"),
    }
    usage = record.get("usage") or {}
    token_usage = {
        key: value
        for key, value in (
            ("input_tokens", usage.get("prompt_tokens")),
            ("output_tokens", usage.get("completion_tokens")),
            ("cache_read_input_tokens", usage.get("cached_tokens")),
            ("total_tokens", usage.get("total_tokens")),
            # litellm's worker payload v2 and codex's ``turn.completed``
            # (``reasoning_output_tokens``) both report this.
            ("output_reasoning_tokens", usage.get("reasoning_tokens")),
        )
        if isinstance(value, int)
    }
    if "total_tokens" not in token_usage and {"input_tokens", "output_tokens"} <= set(token_usage):
        token_usage["total_tokens"] = token_usage["input_tokens"] + token_usage["output_tokens"]
    if token_usage:
        attributes["mlflow.chat.tokenUsage"] = token_usage
    for key in (
        "api_base",
        "step",
        "finish_reason",
        "native_finish_reason",
        "finish_reason_unmapped",
        "finish_reason_available",
        "latency_ms",
        "params",
        "extra_body",
        "omitted_params",
        "sampling_payload_applied",
        "params_source",
        "reasoning_content_present",
        "reasoning_stripped_chars",
        "total_cost_usd",
        # The CLI pseudo-providers' own terminal signal. Distinct from
        # ``finish_reason`` on purpose: claude's ``terminal_reason`` describes
        # the CLI turn, not the model's last message.
        "cli_terminal_reason",
        "cost_unavailable_reason",
        "error",
        "error_type",
        "error_category",
        "retryable",
    ):
        value = record.get(key)
        if value is not None:
            attributes[TELEMETRY_ATTRIBUTE_PREFIX + key] = value
    degraded = degradation_reason(record)
    # Both an attribute and a tag, on purpose. The attribute keeps the reason
    # beside the rest of the call's forensics; the TAG is what the MLflow UI's
    # trace list and ``search_traces`` filter on
    # (``tags."marginalia.degraded_reason" = 'truncated'``), which is the whole
    # point of recording it — a degraded call nobody can select for is a
    # degraded call nobody will find.
    attributes["marginalia.degraded"] = degraded is not None
    if degraded is not None:
        attributes["marginalia.degraded_reason"] = degraded
    # Run-identity tags first, so a per-span fact always wins over a
    # process-wide one that happens to share its name.
    tags = dict(static_tags())
    tags.update(
        {
            str(k): str(v)
            for k, v in (record.get("tags") or {}).items()
            if v is not None
        }
    )
    tags.update(
        {
            str(k): str(v)
            for k, v in (
                ("marginalia.provider", record.get("provider")),
                ("marginalia.model", record.get("model")),
                ("marginalia.step", record.get("step")),
                ("marginalia.degraded_reason", degraded),
            )
            if v is not None
        }
    )
    inputs = {"messages": record.get("messages"), "params": record.get("params")}
    outputs: dict[str, Any] = {"content": record.get("response")}
    if record.get("tool_calls") is not None:
        outputs["tool_calls"] = record["tool_calls"]
    # OK is reserved for a clean, non-empty, normally-finished answer;
    # everything degraded is ERROR, with ``marginalia.degraded_reason`` saying
    # which kind. See ``_DEGRADED_SPAN_STATUS``.
    status = "OK" if degraded is None else _DEGRADED_SPAN_STATUS
    parent_trace_id = record.get("parent_trace_id")
    parent_span_id = record.get("parent_span_id")
    if parent_trace_id and parent_span_id:
        # A child of a caller-opened span. ``start_span`` takes no tags and no
        # experiment_id — both belong to the trace, which the parent already
        # owns — so the identity that was a tag here is an attribute instead,
        # and the parent carries the filterable copy (see ``trace_parent``).
        attributes.update(tags)
        span = client.start_span(
            name=record.get("name") or "llm",
            trace_id=parent_trace_id,
            parent_id=parent_span_id,
            span_type="LLM",
            inputs=inputs,
            attributes=attributes,
            start_time_ns=record.get("start_time_ns"),
        )
        client.end_span(
            parent_trace_id,
            span.span_id,
            outputs=outputs,
            status=status,
            end_time_ns=record.get("end_time_ns"),
        )
        return
    span = client.start_trace(
        name=record.get("name") or "llm",
        span_type="LLM",
        inputs=inputs,
        attributes=attributes,
        tags=tags,
        experiment_id=experiment_id,
        start_time_ns=record.get("start_time_ns"),
    )
    client.end_trace(
        span.trace_id,
        outputs=outputs,
        status=status,
        end_time_ns=record.get("end_time_ns"),
    )


def record(**fields: Any) -> None:
    """Queue one LLM call for export. Never raises, never blocks, never I/Os."""
    if not enabled():
        return
    try:
        payload = {key: _jsonable(value) for key, value in fields.items()}
        # Timestamps are ints and must survive ``_jsonable`` untouched; they do
        # (int passthrough), but keep them out of the attribute dict entirely.
        payload["start_time_ns"] = fields.get("start_time_ns")
        payload["end_time_ns"] = fields.get("end_time_ns")
        # Captured HERE, on the caller's thread, because the drain thread
        # cannot see a thread-local set by whoever made the call. Two plain
        # strings, which survive ``_jsonable`` untouched.
        parent = current_parent()
        if parent is not None:
            payload["parent_trace_id"], payload["parent_span_id"] = parent
        pending = _ensure_worker()
        if pending is None:
            return
        pending.put_nowait(payload)
    except queue.Full:
        # Rule 3: dropping is the correct answer. Blocking here would add the
        # tracking server's latency to the operator's completion.
        logger.debug("MLflow telemetry queue full; dropping one span")
    except Exception:  # noqa: BLE001
        _warn_once("MLflow telemetry record failed; continuing without it", exc_info=True)


def flush(timeout_s: float = 10.0) -> bool:
    """Block until queued spans have been exported. Tests and shutdown only.

    Returns True when the queue drained within ``timeout_s``. Nothing on the
    request path calls this.
    """
    pending = _QUEUE
    if pending is None:
        return True
    deadline = time.monotonic() + max(timeout_s, 0.0)
    while time.monotonic() < deadline:
        if pending.unfinished_tasks == 0:
            return True
        time.sleep(0.05)
    return pending.unfinished_tasks == 0


def _reset_for_tests() -> None:
    """Drop cached client/experiment/warning state AND the drain queue.

    Test helper only. The queue is detached rather than emptied: the drain
    thread blocks forever on ``get()``, so a test whose exporter is slow or
    wedged would otherwise leave every later test queued behind it. Detaching
    gives the next test a fresh queue and worker; the orphaned daemon thread
    holds only the old queue and dies with the process.
    """
    global _CLIENT, _EXPERIMENT_ID, _CLIENT_FAILED, _WARNED, _QUEUE, _WORKER
    global _CLIENT_LOCK, _CLIENT_GENERATION, _MISSING_MLFLOW_REPORTED
    # A detached drain thread may still be inside a slow resolution, holding the
    # old lock and about to publish. A fresh lock means the next test never
    # queues behind it; the generation bump means its result is discarded.
    _CLIENT_LOCK = threading.Lock()
    _CLIENT_GENERATION += 1
    _CLIENT = None
    _EXPERIMENT_ID = None
    _CLIENT_FAILED = False
    _WARNED = False
    _MISSING_MLFLOW_REPORTED = False
    _QUEUE = None
    _WORKER = None


class TelemetryLLMProvider:
    """Innermost provider wrapper that exports one MLflow span per call.

    WHY IT WRAPS AT ``get_provider`` AND NOT INSIDE ``LiteLLMProvider``
    ------------------------------------------------------------------
    ``get_provider`` is the one seam every provider passes through, including
    the three CLI pseudo-providers (``claude_cli``, ``codex_cli``,
    ``pi_cli``). Instrumenting ``LiteLLMProvider.complete`` alone would have
    left them untraced, and they are exactly the providers with the WORST
    existing observability — neither reports a finish reason at all.

    WHY IT IS INNERMOST
    -------------------
    The companion stacks ``_StepLabelledProvider`` (sets the step label) and
    ``_TracingLLMProvider`` (ingest inspector events) OUTSIDE whatever
    ``get_provider`` returned. Being innermost means the step label is already
    set when this wrapper reads it, and means the latency measured here is the
    provider's, not another wrapper's bookkeeping.

    THE OBSERVER CHAIN IS LOAD-BEARING
    ----------------------------------
    ``okto_neuron.llm._set_request_observer`` is a SINGLE-SLOT thread-local.
    ``_TracingLLMProvider`` installs its own observer before calling into this
    wrapper; if this wrapper simply overwrote it, the inspector's
    ``llm_request`` event would stop firing and the
    one-request-event-per-call invariant would break silently. So the observer
    installed here forwards to whatever was already there, and restores it.
    """

    def __init__(self, provider: Any, resolved: Any) -> None:
        self._provider = provider
        self._telemetry_provider_name = str(getattr(resolved, "provider", "") or "unknown")
        # The PROVIDER's base wins over the resolved config's. They are the
        # same object for every litellm provider, but ``ChatGPTProvider``
        # overwrites it with the real hosted backend, because ``resolved``
        # still holds the vault's unrelated loopback default and a span that
        # named that would make a hosted run look local for good.
        self._telemetry_api_base = getattr(provider, "api_base", None) or getattr(
            resolved, "api_base", None
        )
        self.model = str(getattr(provider, "model", getattr(resolved, "model", "unknown")))
        if hasattr(provider, "api_base"):
            self.api_base = provider.api_base

    def __getattr__(self, name: str) -> Any:
        # Delegates the capability flags other wrappers probe for —
        # ``traces_effective_request`` above all — plus anything a future
        # provider grows. Only reached for names not set in ``__init__``.
        return getattr(self._provider, name)

    def complete(
        self,
        messages: Any,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        presence_penalty: float | None = None,
        enable_thinking: bool | None = None,
        response_format: Any = None,
    ) -> str:
        if not enabled():
            # The whole point of rule 1: an operator who has not opted in gets
            # a plain delegation and nothing else — no timing, no capture, no
            # import.
            return self._provider.complete(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                top_k=top_k,
                min_p=min_p,
                presence_penalty=presence_penalty,
                enable_thinking=enable_thinking,
                response_format=response_format,
            )

        from okto_neuron.llm import (
            _set_request_observer,
            current_call_step,
            last_call_stats,
        )

        effective: dict[str, Any] = {}

        previous_observer = None

        def _observer(payload: dict) -> None:
            effective.update(payload)
            if previous_observer is not None:
                previous_observer(payload)

        previous_observer = _set_request_observer(_observer)
        step = current_call_step()
        start_ns = time.time_ns()
        started = time.monotonic()
        error: BaseException | None = None
        response = ""
        try:
            response = self._provider.complete(
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                top_k=top_k,
                min_p=min_p,
                presence_penalty=presence_penalty,
                enable_thinking=enable_thinking,
                response_format=response_format,
            )
        except BaseException as exc:  # noqa: BLE001 - re-raised below, unchanged
            error = exc
            raise
        finally:
            _set_request_observer(previous_observer)
            end_ns = time.time_ns()
            latency_ms = (time.monotonic() - started) * 1000.0
            try:
                stats = dict(last_call_stats() or {}) if error is None else {}
                requested = {
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "top_p": top_p,
                    "top_k": top_k,
                    "min_p": min_p,
                    "presence_penalty": presence_penalty,
                    "enable_thinking": enable_thinking,
                    "response_format": response_format,
                }
                params = dict(effective.get("params") or {}) or requested
                fields: dict[str, Any] = {
                    "name": f"llm.{step}",
                    "model": self.model,
                    "provider": self._telemetry_provider_name,
                    "api_base": self._telemetry_api_base,
                    "step": step,
                    "messages": [
                        {
                            "role": getattr(m, "role", ""),
                            "content": getattr(m, "content", ""),
                        }
                        for m in (messages or ())
                    ],
                    "params": params,
                    "params_source": "effective" if effective.get("params") else "requested",
                    "extra_body": effective.get("extra_body") or None,
                    "omitted_params": effective.get("omitted") or None,
                    "sampling_payload_applied": effective.get("sampling_payload_applied"),
                    "response": response,
                    "usage": stats or None,
                    "latency_ms": round(latency_ms, 3),
                    "start_time_ns": start_ns,
                    "end_time_ns": end_ns,
                }
                for key in (
                    "finish_reason",
                    "native_finish_reason",
                    "finish_reason_unmapped",
                    "reasoning_content_present",
                    "reasoning_stripped_chars",
                    "total_cost_usd",
                    "tool_calls",
                    # Set by the CLI providers (``_claude_cli`` /
                    # ``_codex_cli`` / ``_pi_cli``), which report a native
                    # reason even where no OpenAI-shaped mapping exists.
                    "cli_terminal_reason",
                    # Set by ChatGPTProvider: an explicit statement that cost
                    # is unknown, which a zero would have hidden.
                    "cost_unavailable_reason",
                ):
                    if key in stats:
                        fields[key] = stats[key]
                # An ABSENT finish reason is a fact, not a clean stop. The CLI
                # providers report none at all; saying so explicitly is the
                # difference between "the model finished normally" and "we do
                # not know how this call ended".
                fields["finish_reason_available"] = "finish_reason" in stats
                if error is not None:
                    fields["error"] = str(error)
                    fields["error_type"] = type(error).__name__
                    category = getattr(error, "category", None)
                    if category is not None:
                        fields["error_category"] = category
                    retryable = getattr(error, "retryable", None)
                    if retryable is not None:
                        fields["retryable"] = retryable
                record(**fields)
            except Exception:  # noqa: BLE001 - rule 2, absolutely never propagate
                _warn_once("MLflow telemetry capture failed; continuing", exc_info=True)
        return response


# --------------------------------------------------------------------------
# Public seam for a completion THIS process did not make through a provider.
# --------------------------------------------------------------------------
#
# WHY THIS EXISTS
# ---------------
# ``wrap_provider`` traces at ``get_provider``, which covers every call that
# goes through Okto Neuron's provider layer. Some callers legitimately do not:
# the LoCoMo benchmark judge (a separate harness) runs offline against an
# already-written ``answers.jsonl``, with no vault and no daemon, and POSTs to
# the judge endpoint itself. Its calls are the ones most worth comparing
# against daemon calls — same models, same endpoints, same degradation
# vocabulary — and they were invisible.
#
# So this exposes the SAME record/queue/drain/emit machinery to an external
# caller. It deliberately adds no second implementation: it builds the same
# field dict ``TelemetryLLMProvider.complete`` builds and hands it to the same
# :func:`record`, so a judge span and a daemon span are the same kind of
# object and a single ``search_traces`` filter finds both.


class _NullExternalCompletion:
    """What :func:`trace_external_completion` yields while telemetry is off.

    Every method is a no-op, so a caller writes the same code whether or not
    ``OKTO_NEURON_MLFLOW_TRACKING_URI`` is set, and pays nothing when it is not
    — no timing, no capture, no mlflow import.
    """

    enabled = False

    def set_response(self, *args: Any, **kwargs: Any) -> None:
        return None

    def set_openai_response(self, body: Any) -> None:
        return None

    def set_error(self, error: object) -> None:
        return None


class ExternalCompletion:
    """Handle for one externally-made completion, exported when it closes.

    The caller reports the outcome exactly once, by whichever of the three
    setters matches what it has: :meth:`set_openai_response` for a raw
    OpenAI-shaped response body, :meth:`set_response` for already-extracted
    pieces, :meth:`set_error` for a failure it swallowed itself. An exception
    that escapes the ``with`` block is recorded as ``provider_error`` without
    any call, because a call that raised is the one case the caller cannot
    forget to report.
    """

    enabled = True

    def __init__(self, fields: dict[str, Any]) -> None:
        self._fields = fields
        self._outcome: dict[str, Any] = {}

    def set_response(
        self,
        *,
        text: str | None = None,
        usage: dict[str, Any] | None = None,
        finish_reason: str | None = None,
        native_finish_reason: str | None = None,
        tool_calls: Any = None,
    ) -> None:
        """Report an outcome from already-extracted pieces."""
        outcome: dict[str, Any] = {}
        if text is not None:
            outcome["response"] = text
        if usage is not None:
            outcome["usage"] = usage
        if finish_reason is not None:
            outcome["finish_reason"] = finish_reason
        if native_finish_reason is not None:
            outcome["native_finish_reason"] = native_finish_reason
        if tool_calls is not None:
            outcome["tool_calls"] = tool_calls
        self._outcome.update(outcome)

    def set_openai_response(self, body: Any) -> None:
        """Report an outcome from a raw OpenAI-shaped response body.

        Tolerates every shape it is handed — a dict, a litellm
        ``ModelResponse``, something malformed — because a telemetry helper
        that raises on an odd response would turn an observability gap into an
        outage (rule 2). Whatever it cannot read is simply absent, and an
        absent answer is then reported as ``empty`` by
        :func:`degradation_reason`, which is the honest verdict.
        """
        try:
            payload = body if isinstance(body, dict) else _as_dict(body)
            choices = payload.get("choices") or ()
            choice = choices[0] if choices else {}
            if not isinstance(choice, dict):
                choice = _as_dict(choice)
            message = choice.get("message")
            message = message if isinstance(message, dict) else _as_dict(message)
            usage = payload.get("usage")
            usage = usage if isinstance(usage, dict) else _as_dict(usage)
            self.set_response(
                text=message.get("content"),
                usage=_normalize_usage(usage),
                finish_reason=choice.get("finish_reason"),
                native_finish_reason=choice.get("native_finish_reason"),
                tool_calls=message.get("tool_calls"),
            )
        except Exception:  # noqa: BLE001 - rule 2
            _warn_once("MLflow telemetry response capture failed; continuing", exc_info=True)

    def set_error(self, error: object) -> None:
        """Report a failure the caller caught and handled itself.

        ``provider_error`` outranks every other degradation reason, so a call
        that failed never lands as a clean span even if a partial body was
        also reported.
        """
        self._outcome["error"] = str(error)
        self._outcome["error_type"] = (
            type(error).__name__ if isinstance(error, BaseException) else "error"
        )


def _as_dict(value: object) -> dict[str, Any]:
    """Best-effort dict view of an SDK response object. Never raises."""
    if isinstance(value, dict):
        return value
    for attr in ("model_dump", "dict", "to_dict"):
        method = getattr(value, attr, None)
        if callable(method):
            try:
                result = method()
            except Exception:  # noqa: BLE001
                continue
            if isinstance(result, dict):
                return result
    return {}


def _normalize_usage(usage: dict[str, Any]) -> dict[str, Any] | None:
    """The token keys ``_emit`` already knows how to read.

    ``_emit`` speaks ``last_call_stats()``'s vocabulary
    (``prompt_tokens``/``completion_tokens``/``cached_tokens``/
    ``total_tokens``/``reasoning_tokens``). OpenAI-shaped bodies use the same
    names except that the cached count hides in ``prompt_tokens_details`` and
    the reasoning count in ``completion_tokens_details``; lifting those two is
    the whole translation.
    """
    if not usage:
        return None
    out: dict[str, Any] = {
        key: usage[key]
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if isinstance(usage.get(key), int)
    }
    prompt_details = usage.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else _as_dict(prompt_details)
    if isinstance(prompt_details.get("cached_tokens"), int):
        out["cached_tokens"] = prompt_details["cached_tokens"]
    completion_details = usage.get("completion_tokens_details")
    completion_details = (
        completion_details if isinstance(completion_details, dict) else _as_dict(completion_details)
    )
    if isinstance(completion_details.get("reasoning_tokens"), int):
        out["reasoning_tokens"] = completion_details["reasoning_tokens"]
    return out or None


@contextmanager
def trace_child(
    name: str,
    *,
    span_type: str = "UNKNOWN",
    inputs: Any = None,
    attributes: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Time one NON-LLM step as a child of the current :func:`trace_parent`.

    Retrieval is not a completion, so it never passes the provider seam and
    has no span of its own — which leaves the most interesting question about
    an answer, how much of its time went to finding the evidence versus
    writing the prose, unanswerable from the trace. This closes that.

    A no-op when telemetry is off or when nothing on this thread has opened a
    parent, so it is safe to leave in a hot path.
    """
    parent = current_parent()
    if not enabled() or parent is None:
        yield _NullParentSpan()
        return
    resolved = _client()
    if resolved is None:
        yield _NullParentSpan()
        return
    client, _ = resolved
    trace_id, parent_id = parent
    span = None
    try:
        span = client.start_span(
            name=name,
            trace_id=trace_id,
            parent_id=parent_id,
            span_type=span_type,
            inputs=inputs,
            attributes=dict(attributes or {}),
            start_time_ns=time.time_ns(),
        )
    except Exception:  # noqa: BLE001 - rule 2
        _warn_once("MLflow child span failed to open; continuing untraced", exc_info=True)
        yield _NullParentSpan()
        return
    handle = _ParentSpan(client, span)
    try:
        yield handle
    finally:
        try:
            client.end_span(
                trace_id,
                span.span_id,
                outputs=handle.outputs,
                attributes=handle.attributes or None,
                status=handle.status,
                end_time_ns=time.time_ns(),
            )
        except Exception:  # noqa: BLE001 - rule 2
            _warn_once("MLflow child span failed to close; continuing", exc_info=True)


@contextmanager
def trace_parent(
    name: str,
    *,
    span_type: str = "CHAIN",
    inputs: Any = None,
    attributes: dict[str, Any] | None = None,
    tags: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Open a span that every LLM call on this thread becomes a CHILD of.

    Without this, each ``complete()`` is its own root trace and the UI shows a
    flat list: one row per call, with the shape of the operation that made
    them — a question's retrieval and its synthesis, or the several tiers a
    subgraph answer runs — left for the reader to reconstruct from timestamps.
    With it, that operation is one trace with the calls nested under it.

    Unlike everything else in this module the span is opened SYNCHRONOUSLY on
    the caller's thread, because a child needs its parent's ids before it can
    be queued. Measured against a real server: the first call in a process
    pays MLflow's lazy client setup (~90-140 ms), every later one ~0.04 ms,
    and neither the open nor the close blocks on the export itself, which the
    background exporter still does off the critical path.

    Children may be exported LONG after this closes — the drain thread is
    asynchronous and the LLM span is queued, not sent — and that is fine:
    verified against a live server, a child created and ended after its
    parent's ``end_trace`` still lands correctly parented.

    Also sets the thread's pipeline-step label (``okto_neuron.llm.set_call_step``)
    to ``name`` for the duration of the block, restoring whatever was there on
    exit — the same save/restore pattern ``Companion``'s ``_StepLabelledProvider``
    already uses around one ``complete()`` call. Without this, a completion made
    directly through ``get_provider()`` — outside ``Companion``, which is the
    only other place that ever calls ``set_call_step`` — names its span
    ``llm.-``: the step label is never set, so every such call looks
    identical and unfilterable in the trace view no matter what operation made
    it. A ``Companion`` step nested inside this block (extraction, curator, ask,
    …) still wins for the duration of ITS OWN call, because ``_StepLabelledProvider``
    does the identical save/restore around each individual ``complete()`` — this
    only fills in the gap between such calls, and for calls made with no step
    labelling of their own at all.

    The same three rules hold: off unless ``OKTO_NEURON_MLFLOW_TRACKING_URI``
    is set (the body still runs, with no parent), fail-open (a telemetry
    failure yields an untraced body, never a raised operation), and the
    caller's own work is never gated on the export.

    Yields a handle with ``set_attribute``/``set_status``, or a no-op handle
    when telemetry is off.
    """
    if not enabled():
        yield _NullParentSpan()
        return
    resolved = _client()
    if resolved is None:
        yield _NullParentSpan()
        return
    client, experiment_id = resolved
    span = None
    try:
        span = client.start_trace(
            name=name,
            span_type=span_type,
            inputs=inputs,
            attributes=dict(attributes or {}),
            # Tags live on the TRACE, and a child span cannot carry any, so
            # what the trace list filters on has to be decided here.
            tags={**static_tags(), **{str(k): str(v) for k, v in (tags or {}).items()}},
            experiment_id=experiment_id,
            start_time_ns=time.time_ns(),
        )
    except Exception:  # noqa: BLE001 - rule 2: never into the caller
        _warn_once("MLflow parent span failed to open; continuing untraced", exc_info=True)
        yield _NullParentSpan()
        return

    from okto_neuron.llm import set_call_step  # lazy — see TelemetryLLMProvider.complete

    previous = getattr(_parent, "ids", None)
    _parent.ids = (span.trace_id, span.span_id)
    previous_step = set_call_step(name)
    handle = _ParentSpan(client, span)
    try:
        yield handle
    finally:
        _parent.ids = previous
        set_call_step(previous_step)
        try:
            client.end_trace(
                span.trace_id,
                outputs=handle.outputs,
                attributes=handle.attributes or None,
                status=handle.status,
                end_time_ns=time.time_ns(),
            )
        except Exception:  # noqa: BLE001 - rule 2
            _warn_once("MLflow parent span failed to close; continuing", exc_info=True)


class _ParentSpan:
    """Caller-facing handle for the span opened by :func:`trace_parent`."""

    def __init__(self, client: Any, span: Any) -> None:
        self._client = client
        self._span = span
        self.attributes: dict[str, Any] = {}
        self.outputs: Any = None
        self.status: str = "OK"

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def set_outputs(self, outputs: Any) -> None:
        self.outputs = outputs

    def set_status(self, status: str) -> None:
        self.status = status


class _NullParentSpan:
    """What :func:`trace_parent` yields when telemetry is off."""

    attributes: dict[str, Any] = {}
    outputs: Any = None
    status = "OK"

    def set_attribute(self, key: str, value: Any) -> None:
        return None

    def set_outputs(self, outputs: Any) -> None:
        return None

    def set_status(self, status: str) -> None:
        return None


@contextmanager
def trace_external_completion(
    *,
    model: str,
    step: str,
    provider: str | None = None,
    api_base: str | None = None,
    messages: Any = None,
    params: dict[str, Any] | None = None,
    name: str | None = None,
    tags: dict[str, Any] | None = None,
) -> Iterator[Any]:
    """Trace one completion the CALLER made, using the provider path's span.

    The same three rules hold as for every other export here: env-gated on
    ``OKTO_NEURON_MLFLOW_TRACKING_URI`` (off = a no-op handle and no mlflow
    import), fail-open (nothing raised into the caller, at most one warning),
    and off the critical path (:func:`record` only queues).

    ``step`` is the label the span is named and tagged with
    (``marginalia.step``), so external calls are selectable alongside the
    daemon's own steps — ``judge`` for the LoCoMo judge.
    """
    if not enabled():
        yield _NullExternalCompletion()
        return

    fields: dict[str, Any] = {
        "name": name or f"llm.{step}",
        "model": model,
        "provider": provider or "external",
        "api_base": api_base,
        "step": step,
        "messages": _normalize_messages(messages),
        "params": params or {},
        # There is no request observer on this path: the caller built the
        # request itself, so what it passed IS the effective request. Saying
        # ``effective`` would claim a wire-level capture that did not happen.
        "params_source": "requested",
        # Merged UNDER the span's own provider/model/step tags by ``_emit``,
        # and over ``OKTO_NEURON_MLFLOW_TAGS``.
        "tags": dict(tags) if tags else None,
    }
    handle = ExternalCompletion(fields)
    start_ns = time.time_ns()
    started = time.monotonic()
    error: BaseException | None = None
    try:
        yield handle
    except BaseException as exc:  # noqa: BLE001 - re-raised below, unchanged
        error = exc
        raise
    finally:
        end_ns = time.time_ns()
        try:
            fields.update(handle._outcome)
            fields["latency_ms"] = round((time.monotonic() - started) * 1000.0, 3)
            fields["start_time_ns"] = start_ns
            fields["end_time_ns"] = end_ns
            fields["finish_reason_available"] = "finish_reason" in fields
            if error is not None and "error" not in fields:
                fields["error"] = str(error)
                fields["error_type"] = type(error).__name__
            record(**fields)
        except Exception:  # noqa: BLE001 - rule 2, absolutely never propagate
            _warn_once("MLflow telemetry capture failed; continuing", exc_info=True)


def _normalize_messages(messages: Any) -> list[dict[str, Any]]:
    """``[{role, content}]`` from either dicts or ``Message``-shaped objects."""
    out: list[dict[str, Any]] = []
    for message in messages or ():
        if isinstance(message, dict):
            out.append(
                {"role": message.get("role", ""), "content": message.get("content", "")}
            )
        else:
            out.append(
                {
                    "role": getattr(message, "role", ""),
                    "content": getattr(message, "content", ""),
                }
            )
    return out


def wrap_provider(provider: Any, resolved: Any) -> Any:
    """Wrap ``provider`` for MLflow export when, and only when, it is enabled.

    Returns the provider unchanged while ``OKTO_NEURON_MLFLOW_TRACKING_URI`` is
    unset, so a non-telemetry deployment does not even carry the extra frame.
    """
    if not enabled():
        return provider
    return TelemetryLLMProvider(provider, resolved)


__all__ = [
    "DEFAULT_EXPERIMENT",
    "ExternalCompletion",
    "TelemetryLLMProvider",
    "degradation_reason",
    "wrap_provider",
    "ENV_EXPERIMENT",
    "ENV_TAGS",
    "ENV_TRACKING_URI",
    "enabled",
    "experiment_name",
    "flush",
    "record",
    "static_tags",
    "trace_external_completion",
    "bind_parent",
    "trace_child",
    "trace_parent",
    "tracking_uri",
]
