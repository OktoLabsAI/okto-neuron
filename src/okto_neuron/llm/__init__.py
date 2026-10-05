"""LLM provider layer — LiteLLM-backed, pluggable.

Okto Neuron owns its own model through one interface. The ``LiteLLMProvider``
routes any provider litellm supports; ``StubLLM`` keeps CI and offline tests
deterministic.

``get_provider(resolved)`` is the factory: pass a ``ResolvedLLM`` from
``VaultConfig.llm.resolved(step)``; it returns ``StubLLM`` for the stub
provider or ``LiteLLMProvider`` otherwise.

Phase D of docs/autonomous-architecture-plan.md.
"""

from __future__ import annotations

import importlib.util
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
import logging
import math
import re
import threading
import time
from types import UnionType
from dataclasses import dataclass
from inspect import Parameter as InspectParameter, signature
from typing import (
    TYPE_CHECKING,
    Callable,
    Literal,
    Protocol,
    Sequence,
    Union,
    get_args,
    get_origin,
    runtime_checkable,
)
from urllib.parse import urlparse

from okto_neuron._compat import getenv as _compat_getenv
from okto_neuron._compat import secret_env as _secret_env
from okto_neuron.config._vault import (
    DEFAULT_LLM_REQUEST_TIMEOUT_S,
    MANAGED_LLM_PARAMETERS,
    classify_api_base,
)
from okto_neuron._internal.completion_guard import assert_completion_allowed
from okto_neuron.errors import OktoNeuronError
from okto_neuron.providers import LOCAL_EXTENDED_DRIVERS, litellm_proxy_models

if TYPE_CHECKING:
    from okto_neuron.config._vault import ResolvedLLM

# Generation logger. INFO = one line per call (timing, tokens, finish_reason,
# previews) so a slow per-block rip can be watched for looping / runaway output;
# DEBUG = full prompt + raw completion. Tail the serve log to validate live.
logger = logging.getLogger("okto_neuron.llm")

_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)
_OPEN_THINK = re.compile(r"<think>", re.IGNORECASE)
_CLOSE_THINK = re.compile(r"</think>\s*", re.IGNORECASE)


def strip_reasoning(text: str) -> str:
    """Drop ``<think>…</think>`` blocks emitted by reasoning models.

    Two shapes exist and both leak deliberation into the answer if unhandled:

    * **Paired** — the completion carries its own ``<think>`` … ``</think>``.
    * **Dangling close** — the chat template prefilled the opening ``<think>``
      into the *prompt* (the standard Qwen-style thinking pattern), so the
      completion *begins* inside the reasoning block and only ever emits the
      closing tag. Observed live: an ``ask`` answer whose first characters were
      the model's raw chain-of-thought, with the real answer after ``</think>``.

    The dangling case is decided on the ORIGINAL text, before paired
    substitution: a ``</think>`` that has an opening tag somewhere before it
    belongs to the paired logic, and a literal ``</think>`` surviving a paired
    strip must not be re-read as an unopened one.
    """
    close = _CLOSE_THINK.search(text)
    if close is not None and _OPEN_THINK.search(text, 0, close.start()) is None:
        text = text[close.end() :]
    return _THINK.sub("", text).strip()


def _safe_get(obj: object, key: str) -> object | None:
    """Pull ``key`` off ``obj`` whether it's a pydantic object or a dict.

    Tolerates ``None`` and missing keys/attrs by returning ``None``.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _redact_api_key(text: object, api_key: str | None) -> str:
    rendered = str(text)
    if api_key:
        rendered = rendered.replace(api_key, "[redacted]")
    return re.sub(
        r"(?i)(https?://)[^/@\s]+@",
        r"\1[redacted]@",
        rendered,
    )


@dataclass(frozen=True)
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


ResponseFormat = dict[str, object]
ProviderErrorCategory = Literal[
    "timeout",
    "rate_limited",
    "authentication",
    "unavailable",
    "invalid_request",
    "cancelled",
    "malformed_output",
    "unknown",
]
_PROVIDER_ERROR_CATEGORIES = frozenset(get_args(ProviderErrorCategory))
_TRANSIENT_PROVIDER_ERROR_CATEGORIES = frozenset({"timeout", "rate_limited", "unavailable"})


@dataclass(frozen=True)
class ProviderErrorClassification:
    """Normalized provider failure policy, independent of one adapter's classes."""

    category: ProviderErrorCategory
    retry_after_s: float | None
    retryable: bool


class LLMProviderError(OktoNeuronError):
    """The LLM provider could not be reached or returned an error."""

    default_message = "LLM provider error"

    def __init__(
        self,
        message: str | None = None,
        *,
        category: ProviderErrorCategory = "unknown",
        retry_after_s: float | None = None,
        retryable: bool | None = None,
        cause: Exception | None = None,
    ) -> None:
        if category not in _PROVIDER_ERROR_CATEGORIES:
            raise ValueError(f"invalid provider error category: {category!r}")
        if retry_after_s is not None and (
            isinstance(retry_after_s, bool)
            or not math.isfinite(float(retry_after_s))
            or retry_after_s < 0
        ):
            raise ValueError("retry_after_s must be a finite non-negative number or None")
        self.category = category
        self.retry_after_s = float(retry_after_s) if retry_after_s is not None else None
        default_retryable = category in _TRANSIENT_PROVIDER_ERROR_CATEGORIES
        # An unclassified failure is never safe to retry merely because a caller
        # supplied an optimistic flag.
        self.retryable = (
            False
            if category == "unknown"
            else (default_retryable if retryable is None else bool(retryable))
        )
        super().__init__(message, cause=cause)


class LLMCallCancelled(BaseException):
    """Internal control signal raised when an owning operation cancels a call."""


def _exception_chain(exc: BaseException) -> tuple[BaseException, ...]:
    pending = [exc]
    seen: set[int] = set()
    chain: list[BaseException] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        chain.append(current)
        for nested in (
            getattr(current, "cause", None),
            current.__cause__,
            current.__context__,
        ):
            if isinstance(nested, BaseException):
                pending.append(nested)
    return tuple(chain)


def _retry_after_seconds(chain: Sequence[BaseException], *, now: datetime | None) -> float | None:
    raw_value: object | None = None
    for current in chain:
        for source in (current, getattr(current, "response", None)):
            headers = getattr(source, "headers", None)
            if headers is None:
                continue
            try:
                raw_value = headers.get("retry-after")
                if raw_value is None:
                    raw_value = headers.get("Retry-After")
            except (AttributeError, TypeError):
                continue
            if raw_value is not None:
                break
        if raw_value is None:
            raw_value = getattr(current, "retry_after", None)
        if raw_value is not None:
            break
    if isinstance(raw_value, bool) or raw_value is None:
        return None
    try:
        seconds = float(raw_value)
    except (TypeError, ValueError):
        try:
            deadline = parsedate_to_datetime(str(raw_value))
        except (TypeError, ValueError, OverflowError):
            return None
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        reference = now or datetime.now(UTC)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=UTC)
        seconds = max(0.0, (deadline - reference).total_seconds())
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


def classify_provider_exception(
    exc: BaseException,
    *,
    now: datetime | None = None,
) -> ProviderErrorClassification:
    """Purely normalize LiteLLM, transport, and CLI failures for retry policy."""

    if isinstance(exc, LLMProviderError):
        return ProviderErrorClassification(
            category=exc.category,
            retry_after_s=exc.retry_after_s,
            retryable=exc.retryable,
        )

    chain = _exception_chain(exc)
    retry_after_s = _retry_after_seconds(chain, now=now)
    category: ProviderErrorCategory = "unknown"
    retryable_override: bool | None = None

    # Classify each exception from the outside in.  Mixing all names and text
    # into one blob lets an inner transport timeout incorrectly upgrade an
    # outer authentication/invalid-request failure to retryable.
    for current in chain:
        name = type(current).__name__.casefold()
        text = str(current).casefold()
        status_codes: set[int] = set()
        for candidate in (
            getattr(current, "status_code", None),
            getattr(getattr(current, "response", None), "status_code", None),
            getattr(current, "exception_status_code", None),
        ):
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                status_codes.add(candidate)
        if isinstance(current, LLMCallCancelled) or "cancel" in name:
            category = "cancelled"
        elif (
            status_codes & {401, 403}
            or any(marker in name for marker in ("authentication", "permissiondenied"))
            or any(
                marker in text
                for marker in (
                    "unauthorized",
                    "forbidden",
                    "authentication failed",
                    "invalid api key",
                    "not logged in",
                )
            )
        ):
            category = "authentication"
        elif (
            status_codes & {400, 404, 409, 413, 422}
            or any(
                marker in name
                for marker in (
                    "badrequest",
                    "invalidrequest",
                    "notfound",
                    "contextwindow",
                    "unsupportedparams",
                    "unprocessable",
                    "rejectedrequest",
                    "contentpolicy",
                    "budgetexceeded",
                    "unknownprovider",
                )
            )
            or any(
                marker in text
                for marker in (
                    "bad model",
                    "invalid model",
                    "model not found",
                    "unknown model",
                    "unknown provider",
                    "invalid request",
                    "unsupported parameter",
                )
            )
        ):
            category = "invalid_request"
        elif isinstance(current, FileNotFoundError) or "cli not found on path" in text:
            category = "unavailable"
            retryable_override = False
        elif 408 in status_codes or "timeout" in name or "timed out" in text:
            category = "timeout"
        elif (
            429 in status_codes
            or "ratelimit" in name
            or any(
                marker in text for marker in ("rate limit", "too many requests", "quota exceeded")
            )
        ):
            category = "rate_limited"
        elif (
            any(code >= 500 for code in status_codes)
            or any(
                marker in name
                for marker in (
                    "apiconnection",
                    "connectionerror",
                    "serviceunavailable",
                    "badgateway",
                    "internalserver",
                )
            )
            or any(
                marker in text
                for marker in (
                    "connection refused",
                    "connection reset",
                    "service unavailable",
                    "temporarily unavailable",
                    "network is unreachable",
                )
            )
        ):
            category = "unavailable"
        elif any(
            marker in name
            for marker in (
                "apiresponsevalidation",
                "jsonschemavalidation",
                "jsondecode",
            )
        ) or any(marker in text for marker in ("unparseable output", "malformed output")):
            category = "malformed_output"
        if category != "unknown":
            break

    retryable = category in _TRANSIENT_PROVIDER_ERROR_CATEGORIES
    if retryable_override is not None:
        retryable = retryable_override
    if category == "unknown":
        retryable = False
    return ProviderErrorClassification(
        category=category,
        retry_after_s=retry_after_s,
        retryable=retryable,
    )


# Per-thread record of the most recent completion's token usage. Providers
# call _set_last_call_stats() after every complete(); callers that want the
# numbers (e.g. the curator, for ledger telemetry) read last_call_stats()
# immediately after the call returns. Thread-local so concurrent curation
# fan-out (ADR 0015 D1) reads its own call's stats, never a neighbour's.
_call_stats = threading.local()
_call_param_plan = threading.local()


# Provider finish reasons that litellm's ``_FINISH_REASON_MAP`` REWRITES to a
# clean ``"stop"`` even though the provider means an abnormal end, so the
# membership test in ``_finish_reason_is_unmapped`` can never see them. Z.ai
# (GLM) reports a transport failure mid-generation as ``network_error`` and
# litellm maps that to ``"stop"``; without this set it reached callers as a
# clean answer. Kept deliberately short: only reasons whose abnormal meaning
# is documented by the provider belong here.
_NATIVE_ABNORMAL_STOP_REASONS = frozenset({"network_error"})


def _finish_reason_is_unmapped(native: str) -> bool:
    """True when litellm has no mapping for this provider finish reason.

    ``map_finish_reason`` (litellm_core_utils/core_helpers.py:106-116) defaults an
    UNMAPPED reason to ``"stop"``, so an unknown abnormal stop would otherwise
    reach callers disguised as a clean one. Membership in ``_FINISH_REASON_MAP``
    is the only honest test; a missing/renamed map means we cannot tell, so we
    say "mapped" and never invent an alarm.
    """
    try:
        from litellm.litellm_core_utils.core_helpers import (  # type: ignore[attr-defined]
            _FINISH_REASON_MAP,
        )
    except Exception:  # pragma: no cover - litellm absent or internals moved
        return False
    return native not in _FINISH_REASON_MAP


def _set_last_call_stats(stats: dict[str, object] | None) -> None:
    _call_stats.value = stats


def last_call_stats() -> dict[str, object] | None:
    """Token usage of the current thread's most recent LLM call, if reported."""
    return getattr(_call_stats, "value", None)


# ADR 0039 D5 transient-provider retry policy, shared by every LLM step that
# degrades on a provider error (extraction units, the ingest judge / curator /
# relation-curator / type-adjudication / correction-judge calls, the
# predicate-propose sweep judge, and ask synthesis; a call whose failure falls
# back to one of those, like the reconcile cluster judge, is not): at most one
# automatic retry (two attempts), only for an error the provider layer
# classified as retryable, waiting the provider's Retry-After (capped) when it
# supplied one. The provider seam itself never retries, so each attempt is its
# own call span.
PROVIDER_MAX_ATTEMPTS = 2
PROVIDER_MAX_RETRY_DELAY_SECONDS = 60.0
# A timeout or dropped connection names no Retry-After, so the retry would
# otherwise fire instantly into the same dead endpoint. Back off exponentially
# (2 s, then 4 s ... capped) for those categories only (issue #24).
PROVIDER_RETRY_BACKOFF_BASE_SECONDS = 2.0
_BACKOFF_CATEGORIES = frozenset({"timeout", "connection"})
PROVIDER_ERROR_SUMMARY_MAX = 300
_RETRY_CANCEL_POLL_SECONDS = 0.25


def provider_retry_delay(exc: BaseException, attempt: int) -> float | None:
    """Seconds to wait before retrying after ``attempt`` failed with ``exc``.

    ``None`` means do not retry: the error is not retryable, or ``attempt``
    already used the last of ``PROVIDER_MAX_ATTEMPTS``.
    """
    if attempt >= PROVIDER_MAX_ATTEMPTS or not bool(getattr(exc, "retryable", False)):
        return None
    delay = max(0.0, float(getattr(exc, "retry_after_s", 0.0) or 0.0))
    if delay == 0.0 and getattr(exc, "category", None) in _BACKOFF_CATEGORIES:
        delay = PROVIDER_RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    return min(PROVIDER_MAX_RETRY_DELAY_SECONDS, delay)


def provider_error_summary(exc: BaseException) -> str:
    """Short, safe one-line summary of a provider failure.

    SECURITY: credentials are already redacted (``_redact_api_key``) before an
    ``LLMProviderError`` is constructed, so this only collapses whitespace and
    caps the length; it never adds prompts or file paths.
    """
    text = " ".join(str(exc).split())
    if not text:
        return exc.__class__.__name__
    if len(text) > PROVIDER_ERROR_SUMMARY_MAX:
        text = text[: PROVIDER_ERROR_SUMMARY_MAX - 1].rstrip() + "…"
    return text


def complete_with_retry(
    provider: "LLMProvider",
    messages: "list[Message]",
    *,
    step: str,
    retries: "list[dict[str, object]] | None" = None,
    **params: object,
) -> str:
    """``provider.complete(messages, **params)`` under the ADR 0039 D5 policy.

    A failure :func:`provider_retry_delay` calls retryable is retried once;
    anything else, and the retry's own failure, propagates exactly as a bare
    ``complete`` would, so a caller's degrade path is unchanged. Each retried
    failure is appended to ``retries`` (``attempt``, ``category``,
    ``retry_after_s``, ``delay_s``, ``error``) so the step can record, next to
    its outcome, that it needed a second attempt, including when the second
    attempt also failed.

    The per-thread stats channel is cleared before every attempt, so usage or
    a finish reason read afterwards can only have come from the attempt that
    returned. Two optional provider hooks are honoured, both implemented by
    the ingest tracing wrapper: ``note_retry(step, record)`` is told about
    each retried failure, and ``should_cancel`` (a predicate) cuts the wait
    short, after which the next ``complete`` raises the cancellation itself.
    No retry is taken when a scoped task deadline (``_scoped_call_timeout``)
    has already passed or would pass during the wait.
    """
    attempt = 0
    while True:
        attempt += 1
        _set_last_call_stats(None)
        try:
            return provider.complete(messages, **params)
        except LLMProviderError as exc:
            delay = provider_retry_delay(exc, attempt)
            if delay is None:
                raise
            # A scoped task deadline (``_scoped_call_timeout``, e.g. the
            # curation watchdog) bounds the whole step, not each attempt: a
            # retry that would start at or after it could only fail with
            # "deadline expired", so the first failure stands.
            remaining = _current_call_timeout_s()
            if remaining is not None and remaining <= delay:
                raise
            summary = provider_error_summary(exc)
            record: dict[str, object] = {
                "attempt": attempt,
                "category": exc.category,
                "retry_after_s": exc.retry_after_s,
                "delay_s": delay,
                "error": summary,
            }
            if retries is not None:
                retries.append(record)
            note_retry = getattr(provider, "note_retry", None)
            if callable(note_retry):
                try:
                    note_retry(step, record)
                except Exception:  # noqa: BLE001 - bookkeeping never blocks the retry
                    logger.debug("provider retry hook failed", exc_info=True)
            logger.warning(
                "%s provider error on attempt %d/%d (category=%s), retrying in %.1fs: %s",
                step,
                attempt,
                PROVIDER_MAX_ATTEMPTS,
                exc.category,
                delay,
                summary,
            )
            should_cancel = getattr(provider, "should_cancel", None)
            if delay > 0 and not callable(should_cancel):
                time.sleep(delay)
            elif delay > 0:
                deadline = time.monotonic() + delay
                while not should_cancel():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(_RETRY_CANCEL_POLL_SECONDS, remaining))


def sampler_overrides(resolved: object) -> dict[str, object]:
    """Constructor kwargs for a step LLM client, omitting values the vault never set.

    ``max_tokens`` and ``temperature`` are declared ``int | None`` / ``float |
    None`` in the config models, so an unconfigured vault resolves them to
    ``None``. Passing that ``None`` explicitly into a client constructor
    OVERRIDES the documented class default (``LLMExtractor`` 16000,
    ``LLMRelationCurator``/``LLMCandidateCurator``/``LLMMergeJudge``/
    ``LLMPredicateJudge`` 2000, ``LLMTypeAdjudicator`` 4000) — and a ``None``
    cap reaches the provider as *no output cap at all*, which let a degenerate
    decode loop run to the model host's own 32768-token ceiling. Omit the kwarg
    instead so the class default stands; a configured value is passed through
    unchanged.
    """

    overrides: dict[str, object] = {}
    for name in ("max_tokens", "temperature"):
        value = getattr(resolved, name, None)
        if value is not None:
            overrides[name] = value
    return overrides


def last_parameter_plan() -> dict[str, object] | None:
    """Sanitized sent/extra/dropped parameter names for the current thread."""

    return getattr(_call_param_plan, "value", None)


# Per-thread observer for the EFFECTIVE request a provider has just assembled,
# installed by the ingest tracing wrapper (``_TracingLLMProvider``) around one
# completion and invoked by the provider at the single moment the request is
# fully built and not yet issued. Same thread-local install/restore shape as
# ``_set_call_cancel_predicate`` below, and for the same reason: the wrapper
# cannot see inside ``complete()``, but what it needs to report only exists
# there.
#
# WHY THIS EXISTS: the trace used to report the tracing wrapper's OWN method
# arguments, which are pre-merge. A role configured with a raw
# ``sampling_payload`` has those arguments discarded (see the override block in
# ``LiteLLMProvider.complete``), so the inspector showed parameters that were
# never sent — e.g. the extractor's class defaults ``temperature=0.0`` /
# ``max_tokens=16000`` instead of the operator's configured payload. The merge
# has exactly one implementation and this observer reports ITS result; nothing
# re-derives the merge a second time.
_call_request_observer = threading.local()


def _set_request_observer(
    observer: Callable[[dict[str, object]], None] | None,
) -> Callable[[dict[str, object]], None] | None:
    previous = getattr(_call_request_observer, "value", None)
    _call_request_observer.value = observer
    return previous


def _jsonable(value: object) -> object:
    """JSON-safe copy of one traced parameter value.

    Traced params are persisted to the ingest history sidecar and served over
    REST, so an exotic value must degrade to its ``repr`` rather than break
    serialization for the whole event. Copying also means a later mutation of
    the live request dict can never rewrite an already-emitted event.
    """

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return repr(value)


def _notify_request_observer(
    *,
    kwargs: dict,
    extra_body: dict,
    omitted: dict[str, str],
    sampling_payload_applied: bool,
) -> None:
    """Report the effective, secret-free request to an installed observer.

    Never raises: a broken or slow tracer is an observability problem, not a
    reason to fail the operator's LLM call.
    """

    observer = getattr(_call_request_observer, "value", None)
    if observer is None:
        return
    try:
        observer(
            {
                # ONE filter, BOTH containers. ``api_key`` reaches the observer
                # only through ``kwargs`` today (``_check_sampling_payload``
                # refuses it, so it cannot ride a raw payload into
                # ``extra_body``), but that guard lives in another module and
                # only inspects TOP-LEVEL payload names. A scrub that trusts a
                # distant validator is the kind of split condition this
                # codebase keeps paying for, so both containers are filtered
                # here, identically, with no second key list to drift.
                "params": {
                    name: _jsonable(value)
                    for name, value in kwargs.items()
                    if name not in _OBSERVER_EXCLUDED_REQUEST_KEYS
                },
                "extra_body": {
                    name: _jsonable(value)
                    for name, value in extra_body.items()
                    if name not in _OBSERVER_EXCLUDED_REQUEST_KEYS
                },
                "omitted": dict(omitted),
                "sampling_payload_applied": sampling_payload_applied,
            }
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("llm request observer raised; ignoring", exc_info=True)


# Per-thread label for the pipeline step currently making an LLM call
# ("extraction"/"judge"/"curator"/"relation_curator"/"predicate_judge"/"ask"/
# …). Set by the one seam that knows which step is calling — the
# ``Companion._get_provider`` wrapper (and the equivalent judge-builders in
# ``server/_curation.py``) — via ``set_call_step`` on every ``complete()``
# entry, never relying on a caller to reset it afterwards. Every per-call log
# line (the litellm usage line and each CLI provider's call line) reads it
# back via ``current_call_step()`` and appends it as ``step=%s`` so a live
# serve log can be filtered by pipeline stage instead of reading as one
# undifferentiated stream of "codex call model=... duration=..." lines.
# Thread-local for the same reason as ``_call_stats``: concurrent curation
# fan-out (ADR 0015 D1) must never leak one worker thread's step label onto
# another thread's log line.
_call_step = threading.local()


def set_call_step(step: str | None) -> str | None:
    """Set the current thread's pipeline-step label.

    Returns the previous raw value (``None`` if it was never set), so a
    caller can save/restore the label around a scoped region — e.g. a wrapper
    that labels one ``complete()`` call and must put back whatever the caller
    had before it (not unconditionally reset to ``None``), so nested/re-entrant
    callers on the same thread don't clobber each other's label.
    """
    previous = getattr(_call_step, "value", None)
    _call_step.value = step
    return previous


def current_call_step() -> str:
    """The current thread's pipeline-step label, or ``"-"`` when unset."""
    return getattr(_call_step, "value", None) or "-"


# Optional cooperative-cancellation predicate for the current LLM call. The
# ingest tracing wrapper installs it around a provider completion; CLI providers
# copy it into their active-process registry so the Stop Bulk Ingest action can
# terminate only the subprocess that belongs to the cancelled ingest (not an
# unrelated ask/curation call running at the same time).
_call_cancel_predicate = threading.local()

# Optional per-call task deadline. Pipeline stages such as curation can impose
# a narrower watchdog than the reusable provider connection without mutating
# that connection's persisted transport policy. LiteLLM receives the smaller
# of both values, so its owned helper process is terminated at the same
# boundary the stage reports as timed out.
_call_deadline = threading.local()


def _set_call_cancel_predicate(
    predicate: Callable[[], bool] | None,
) -> Callable[[], bool] | None:
    previous = getattr(_call_cancel_predicate, "value", None)
    _call_cancel_predicate.value = predicate
    return previous


def _current_call_cancel_predicate() -> Callable[[], bool] | None:
    return getattr(_call_cancel_predicate, "value", None)


@contextmanager
def _scoped_call_timeout(timeout_s: float | None):  # type: ignore[no-untyped-def]
    """Install a monotonic task deadline and restore the exact outer scope."""

    previous = getattr(_call_deadline, "value", None)
    if timeout_s is None:
        deadline = previous
    else:
        proposed = time.monotonic() + max(float(timeout_s), 0.0)
        deadline = min(previous, proposed) if previous is not None else proposed
    _call_deadline.value = deadline
    try:
        yield
    finally:
        _call_deadline.value = previous


def _current_call_timeout_s() -> float | None:
    deadline = getattr(_call_deadline, "value", None)
    if deadline is None:
        return None
    return max(float(deadline) - time.monotonic(), 0.0)


def _consume_completion_stream(litellm: object, stream: object, on_token) -> object:
    """Iterate a litellm streaming response into a non-stream-equivalent one.

    ``on_token`` sees each content delta as it arrives (never the assembled
    text). The chunks are handed to ``litellm.stream_chunk_builder``, which
    produces the SAME ModelResponse shape a non-stream call returns —
    choices/message/finish_reason/usage — so every downstream extraction
    (usage stats, truncation flags, native finish reasons) behaves identically
    on the stream and non-stream paths.
    """
    chunks: list[object] = []
    for chunk in stream:
        chunks.append(chunk)
        try:
            delta = chunk.choices[0].delta.content
        except (AttributeError, IndexError):
            delta = None
        if delta:
            try:
                on_token(delta)
            except Exception:  # noqa: BLE001 — telemetry must never fail the call
                logger.debug("on_token callback failed", exc_info=True)
    return litellm.stream_chunk_builder(chunks)  # type: ignore[attr-defined]


def _run_litellm_completion(litellm: object, kwargs: dict) -> object:
    """Run ingest-owned HTTP work in a killable helper, never an orphan thread."""
    call_kwargs = dict(kwargs)
    task_timeout = _current_call_timeout_s()
    if task_timeout is not None:
        if task_timeout <= 0.0:
            raise LLMProviderError(
                "LLM task deadline expired before provider execution",
                category="timeout",
                retryable=True,
            )
        configured_timeout = call_kwargs.get("timeout")
        call_kwargs["timeout"] = (
            min(float(configured_timeout), task_timeout)
            if isinstance(configured_timeout, (int, float))
            and not isinstance(configured_timeout, bool)
            else task_timeout
        )
    predicate = _current_call_cancel_predicate()
    if predicate is None and task_timeout is None:
        return litellm.completion(**call_kwargs)  # type: ignore[attr-defined]
    from okto_neuron.llm._litellm_process import run_cancellable_completion

    return run_cancellable_completion(call_kwargs, predicate or (lambda: False))


@runtime_checkable
class LLMProvider(Protocol):
    model: str

    def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        presence_penalty: float | None = None,
        enable_thinking: bool | None = None,
        response_format: ResponseFormat | None = None,
    ) -> str: ...


# LiteLLM providers where an explicit endpoint URL is part of the normal call
# shape in Okto Neuron's supported subset. Hosted providers such as Anthropic,
# Gemini, Groq, Mistral, DeepSeek, OpenRouter, and Together AI use their managed
# endpoints by default and are selected by the ``<provider>/<model>`` prefix alone.
_API_BASE_PROVIDERS = frozenset(
    {
        "azure",
        "azure_ai",
        "azure_text",
        "custom",
        "custom_openai",
        "databricks",
        "hosted_vllm",
        "litellm_proxy",
        "llamafile",
        "lm_studio",
        "ollama",
        "ollama_chat",
        "oobabooga",
        "openai",
        "openai_like",
        "text-completion-openai",
        "triton",
        "vllm",
    }
)
# Providers whose Claude models hard-reject sending ``temperature`` and
# ``top_p`` together ("temperature and top_p cannot both be specified") and
# reject an ``enabled`` thinking block with no ``budget_tokens``. LiteLLM's
# ``get_supported_openai_params``/provider-config registries advertise BOTH
# params as supported for these provider/model combos, so the generic
# capability-driven send path in ``complete()`` can't tell they're mutually
# exclusive here — this set is a deliberate, provider-scoped override, not a
# change to the general param-capability rules. Bedrock's ``anthropic.claude-*``
# models enforce this; the direct Anthropic API does too for Claude 4.5+.
_TEMPERATURE_TOP_P_EXCLUSIVE_PROVIDERS = frozenset({"bedrock", "anthropic"})

_DEFAULT_API_BASE = "http://127.0.0.1:8123/v1"

# Placeholder API key for keyless self-hosted endpoints. OpenAI-compatible
# servers (vLLM, llama.cpp, LM Studio, Ollama, …) usually accept any key — but
# litellm's openai path REQUIRES one and otherwise raises "Missing credentials"
# (which Okto Neuron surfaces as a silent zero-extraction). When the call targets
# a custom api_base and no real key is configured, we send this placeholder so
# keyless loopback and private-LAN setups work out of the box. Public and known
# hosted endpoints never get it — a genuinely missing key there must still fail loud.
_PLACEHOLDER_API_KEY = "sk-no-key-required"
_HOSTED_API_BASE_HOSTS = frozenset(
    {
        "api.openai.com",
        "api.anthropic.com",
        "generativelanguage.googleapis.com",
        "openrouter.ai",
    }
)
_OPENAI_STANDARD_PARAMS = frozenset(
    {
        "max_completion_tokens",
        "max_tokens",
        "presence_penalty",
        "response_format",
        "temperature",
        "top_p",
    }
)
_LITELLM_CAPABILITY_ONLY_PARAMS = frozenset({"reasoning_effort", "thinking"})
_LITELLM_CONTROL_PARAMS = frozenset(
    {"api_base", "api_key", "drop_params", "max_retries", "messages", "model", "timeout"}
)
# Request keys that must never reach a request observer (see
# ``_notify_request_observer``). ``_LITELLM_CONTROL_PARAMS`` is the same set
# ``_param_accounting_summary`` treats as non-sampling, reused rather than
# restated so the two can't drift: it carries ``api_key`` (the real environment
# credential, or the keyless placeholder injected for a keyless self-hosted
# endpoint) and ``api_base`` (which can embed userinfo credentials).
# ``extra_body`` is excluded from the top-level view only because it is
# reported separately, in full.
_OBSERVER_EXCLUDED_REQUEST_KEYS = _LITELLM_CONTROL_PARAMS | {"extra_body"}
_CONFIGURABLE_PARAMETER_FIELDS = {
    "max_tokens": ("max_tokens", "max_completion_tokens"),
    "temperature": ("temperature",),
    "top_p": ("top_p",),
    "top_k": ("top_k",),
    "min_p": ("min_p",),
    "presence_penalty": ("presence_penalty",),
    "enable_thinking": ("thinking", "reasoning_effort"),
}
_PROVIDER_SPECIFIC_PARAMETER_FIELDS = frozenset({"top_k", "min_p", "enable_thinking"})

ParameterKind = Literal["integer", "number", "boolean", "string", "json"]


@dataclass(frozen=True)
class LLMParameterDescriptor:
    """UI-safe description of one parameter advertised by LiteLLM."""

    name: str
    source: Literal["openai", "provider", "okto-neuron"]
    kind: ParameterKind
    editable: bool
    description: str = ""
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "source": self.source,
            "kind": self.kind,
            "editable": self.editable,
            "description": self.description,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": list(self.choices),
        }


# LiteLLM reliably supplies supported names, not a complete presentation schema.
# Keep provider-independent input semantics here; provider support still comes
# exclusively from LiteLLM's model-aware capability registries.
_PARAMETER_UI_METADATA: dict[str, tuple[ParameterKind, float | None, float | None, str]] = {
    "candidate_count": ("integer", 1, None, "Number of candidates to generate."),
    "frequency_penalty": ("number", -2, 2, "Penalize repeated token frequency."),
    "enable_thinking": ("boolean", None, None, "Enable model thinking on a local endpoint."),
    "logit_bias": ("json", None, None, "Token-to-bias mapping."),
    "max_completion_tokens": ("integer", 1, None, "Maximum completion token budget."),
    "max_output_tokens": ("integer", 1, None, "Provider output token budget."),
    "max_tokens": ("integer", 1, None, "Maximum completion token budget."),
    "min_p": ("number", 0, 1, "Minimum probability sampler threshold."),
    "presence_penalty": ("number", -2, 2, "Penalize tokens already present."),
    "reasoning_effort": ("string", None, None, "Provider reasoning effort level."),
    "seed": ("integer", None, None, "Best-effort deterministic sampling seed."),
    "service_tier": ("string", None, None, "Provider service tier."),
    "stop": ("json", None, None, "Stop sequence or sequence list."),
    "stop_sequences": ("json", None, None, "Provider stop sequence list."),
    "temperature": ("number", 0, 2, "Sampling temperature."),
    "thinking": ("json", None, None, "Provider thinking configuration object."),
    "top_k": ("integer", 0, None, "Top-k sampling cutoff."),
    "top_p": ("number", 0, 1, "Nucleus sampling cutoff."),
}
_PROVIDER_OPTIONAL_DEPENDENCIES = {"bedrock": ("boto3",)}
_PROVIDER_OPTIONAL_DEPENDENCY_HINTS = {
    "bedrock": (
        "Install Okto Neuron with the bedrock extra, for example: "
        'uv tool install --force "okto-neuron[litellm,bedrock]"'
    )
}


def _is_hosted_api_base(api_base: str) -> bool:
    return (urlparse(api_base).hostname or "") in _HOSTED_API_BASE_HOSTS


def _missing_provider_optional_dependencies(provider: str) -> tuple[str, ...]:
    missing = []
    for module_name in _PROVIDER_OPTIONAL_DEPENDENCIES.get(provider, ()):
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, ValueError, AttributeError):
            spec = None
        if spec is None:
            missing.append(module_name)
    return tuple(missing)


def _preflight_provider_optional_dependencies(provider: str) -> None:
    missing = _missing_provider_optional_dependencies(provider)
    if not missing:
        return

    missing_text = ", ".join(missing)
    hint = _PROVIDER_OPTIONAL_DEPENDENCY_HINTS[provider]
    logger.warning(
        "LLM provider optional dependency preflight failed provider=%s missing=%s",
        provider,
        missing_text,
    )
    raise LLMProviderError(
        f"LLM provider '{provider}' requires optional dependency {missing_text}. {hint}",
        category="invalid_request",
        retryable=False,
    )


def warm_provider_dependencies(provider: str) -> None:
    """Eagerly import ``provider``'s runtime dependencies (daemon-boot warmup).

    ``LiteLLMProvider.complete`` imports ``litellm`` lazily per call, which makes
    a long-running daemon vulnerable to its environment mutating underneath it:
    an exact ``uv run``/``uv sync`` in the same checkout prunes non-default
    dependency groups (litellm) from the shared ``.venv`` while the server is up,
    turning EVERY subsequent ask/ingest into a per-request
    ``ModuleNotFoundError`` 500 (observed 2026-07-07: 284/284 empty A/B answers).
    Importing at startup binds the modules into ``sys.modules`` for the process
    lifetime, so a running daemon keeps working regardless of later venv
    mutation — and a broken install fails loud at boot instead of silently
    failing per request.

    ``stub``, ``claude_cli``, ``pi_cli``, and ``codex_cli`` need no optional
    imports; every other provider is litellm-backed (see :func:`get_provider`).
    Raises :class:`LLMProviderError` when a required dependency is missing.
    """
    if provider in ("stub", "claude_cli", "pi_cli", "codex_cli"):
        return
    _preflight_provider_optional_dependencies(provider)
    try:
        import litellm  # noqa: F401 — bind into sys.modules for process lifetime
    except ImportError as exc:
        raise LLMProviderError(
            f"LLM provider '{provider}' requires the 'litellm' package, which is "
            "not importable in this environment. Install with the litellm extra "
            "(e.g. `uv run --group litellm okto-neuron serve` or "
            '`uv tool install --force "okto-neuron[litellm]"`).',
            category="invalid_request",
            retryable=False,
        ) from exc
    for module_name in _PROVIDER_OPTIONAL_DEPENDENCIES.get(provider, ()):
        importlib.import_module(module_name)


def _allows_placeholder_api_key(api_base: str) -> bool:
    if _is_hosted_api_base(api_base):
        return False
    try:
        return classify_api_base(api_base) in ("loopback", "private")
    except ValueError:
        return False


def _litellm_supported_openai_params(
    litellm: object, *, model: str, provider: str
) -> frozenset[str] | None:
    """Ask LiteLLM which standard OpenAI params this provider/model accepts.

    ``None`` means LiteLLM cannot answer for this provider, so callers should keep
    their existing params and let ``drop_params=True`` handle provider-side gaps.
    """
    get_supported = getattr(litellm, "get_supported_openai_params", None)
    if get_supported is None:
        return None
    try:
        params = get_supported(model=model, custom_llm_provider=provider)
    except Exception as exc:
        logger.debug(
            "could not inspect litellm supported params for provider=%s model=%s: %s",
            provider,
            model,
            exc,
        )
        return None
    return frozenset(params) if params is not None else None


def _litellm_provider_config(litellm: object, *, model: str, provider: str) -> dict[str, object]:
    """Return provider-config field annotations exposed by LiteLLM, if available."""
    manager = getattr(litellm, "ProviderConfigManager", None)
    providers = getattr(litellm, "LlmProviders", None)
    if manager is None or providers is None:
        return {}
    try:
        provider_enum = providers(provider)
        config = manager.get_provider_chat_config(model=model, provider=provider_enum)
    except Exception as exc:
        logger.debug(
            "could not inspect litellm provider config for provider=%s model=%s: %s",
            provider,
            model,
            exc,
        )
        return {}
    if config is None:
        return {}
    try:
        params = dict(signature(type(config).__init__).parameters)
    except (TypeError, ValueError) as exc:
        logger.debug(
            "could not inspect litellm provider config signature for provider=%s model=%s: %s",
            provider,
            model,
            exc,
        )
        return {}
    params.pop("self", None)
    return {name: param.annotation for name, param in params.items()}


def _parameter_kind(annotation: object) -> tuple[ParameterKind, tuple[str, ...]]:
    """Infer a conservative editor kind from a LiteLLM config annotation."""

    if annotation is InspectParameter.empty:
        return "json", ()
    origin = get_origin(annotation)
    if origin in (Union, UnionType):
        members = tuple(item for item in get_args(annotation) if item is not type(None))
        if len(members) == 1:
            return _parameter_kind(members[0])
        return "json", ()
    if origin is Literal:
        choices = tuple(str(item) for item in get_args(annotation))
        return "string", choices
    if annotation is bool:
        return "boolean", ()
    if annotation is int:
        return "integer", ()
    if annotation is float:
        return "number", ()
    if annotation is str:
        return "string", ()
    if origin in (list, tuple, dict, set) or annotation in (list, tuple, dict, set):
        return "json", ()
    return "json", ()


def parameter_descriptor(
    name: str,
    *,
    source: Literal["openai", "provider", "okto-neuron"],
    annotation: object = InspectParameter.empty,
) -> LLMParameterDescriptor:
    metadata = _PARAMETER_UI_METADATA.get(name)
    choices: tuple[str, ...] = ()
    if metadata is None:
        kind, choices = _parameter_kind(annotation)
        minimum = maximum = None
        description = "Provider-specific parameter exposed by LiteLLM."
    else:
        kind, minimum, maximum, description = metadata
    return LLMParameterDescriptor(
        name=name,
        source=source,
        kind=kind,
        editable=name not in MANAGED_LLM_PARAMETERS,
        description=description,
        minimum=minimum,
        maximum=maximum,
        choices=choices,
    )


@dataclass(frozen=True)
class LLMParameterCapabilities:
    """LiteLLM-owned parameter capabilities for one configured model."""

    source: str
    supported_openai_params: frozenset[str] | None
    provider_config_params: frozenset[str] = frozenset()
    provider_config_annotations: dict[str, object] | None = None

    @property
    def known(self) -> bool:
        if self.source == "litellm_gateway":
            return bool(self.supported_openai_params)
        return self.supported_openai_params is not None or bool(self.provider_config_params)

    def supports_field(self, field: str) -> bool:
        if self.source == "litellm_gateway" and not self.supported_openai_params:
            return field == "max_tokens"
        wire_params = _CONFIGURABLE_PARAMETER_FIELDS.get(field, ())
        if field == "max_tokens":
            return any(
                _supports_standard_param(self.supported_openai_params, param)
                for param in wire_params
            )
        return any(
            _supports_mapped_param(
                self.supported_openai_params,
                self.provider_config_params,
                param,
            )
            for param in wire_params
        )

    def descriptor(self, name: str) -> LLMParameterDescriptor | None:
        in_openai = (
            self.supported_openai_params is not None and name in self.supported_openai_params
        )
        if not in_openai and name not in self.provider_config_params:
            return None
        annotations = self.provider_config_annotations or {}
        return parameter_descriptor(
            name,
            source="openai" if in_openai else "provider",
            annotation=annotations.get(name, InspectParameter.empty),
        )

    def as_dict(self) -> dict[str, object]:
        supported_fields = [
            field for field in _CONFIGURABLE_PARAMETER_FIELDS if self.supports_field(field)
        ]
        supported_params = sorted(
            (self.supported_openai_params or frozenset()) | self.provider_config_params
        )
        descriptors = [
            descriptor.as_dict()
            for name in supported_params
            if (descriptor := self.descriptor(name)) is not None
        ]
        return {
            "known": self.known,
            "source": self.source,
            "supported_fields": supported_fields,
            "provider_specific_fields": [
                field for field in supported_fields if field in _PROVIDER_SPECIFIC_PARAMETER_FIELDS
            ],
            "supported_params": supported_params,
            "parameters": descriptors,
        }


def _parameter_value_matches(descriptor: LLMParameterDescriptor, value: object) -> bool:
    if descriptor.kind == "boolean":
        valid = isinstance(value, bool)
    elif descriptor.kind == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif descriptor.kind == "number":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif descriptor.kind == "string":
        valid = isinstance(value, str)
    else:
        valid = value is not None
    if not valid:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if descriptor.minimum is not None and value < descriptor.minimum:
            return False
        if descriptor.maximum is not None and value > descriptor.maximum:
            return False
    return not descriptor.choices or str(value) in descriptor.choices


def _litellm_gateway_model_capabilities(
    *,
    api_base: str,
    api_key_env: str | None,
    refresh: bool = False,
) -> dict[str, frozenset[str] | None]:
    """Project the shared gateway catalog into LLM parameter capabilities."""

    return {
        model.id: model.supported_openai_params
        for model in litellm_proxy_models(
            api_base=api_base,
            api_key_env=api_key_env,
            refresh=refresh,
        )
    }


# What a ``chatgpt/*`` request body may actually contain.
#
# Verified against the INSTALLED litellm 1.87.0, not its docs:
# ``litellm/llms/chatgpt/responses/transformation.py:94-108`` filters the
# assembled request down to exactly eleven keys::
#
#     allowed_keys = {"model", "input", "instructions", "stream", "store",
#                     "include", "tools", "tool_choice", "reasoning",
#                     "previous_response_id", "truncation"}
#     return {k: v for k, v in request.items() if k in allowed_keys}
#
# Everything else is dropped by that dict comprehension — no error, no warning.
# Two consequences are easy to miss because the drop happens one layer below
# where the parameter was named:
#
# * ``response_format`` is first mapped to ``request["text"]``
#   (``completion_extras/litellm_responses_transformation/transformation.py:327-330``)
#   and ``text`` is not whitelisted, so JSON mode is silently discarded.
# * ``max_tokens``/``max_completion_tokens`` become ``max_output_tokens``,
#   also not whitelisted, so the token cap is discarded too.
#
# ``ChatGPTConfig`` subclasses ``OpenAIConfig`` and does not override
# ``get_supported_openai_params``, so litellm ADVERTISES 26 parameters
# (temperature, top_p, seed, response_format, max_tokens, …) while
# transmitting at most these. Okto Neuron believing that advertisement is the
# dangerous part: curation would report itself as running in JSON mode, at
# temperature 0, with a token cap, while the request carried none of the
# three. ``model``/``input``/``stream``/``store``/``include`` are set by
# litellm itself and are not caller-configurable, so they are not listed.
_CHATGPT_TRANSMITTED_PARAMS = frozenset(
    {"tools", "tool_choice", "reasoning_effort", "previous_response_id", "truncation"}
)


def parameter_capabilities(
    *,
    provider: str,
    model: str,
    api_base: str = "",
    api_key_env: str | None = None,
    refresh: bool = False,
    litellm_module: object | None = None,
) -> LLMParameterCapabilities:
    """Return the capabilities used by both request shaping and the Web UI.

    Direct providers are described by the installed LiteLLM Python adapter.
    A LiteLLM Proxy alias is described by the proxy's model-group metadata,
    accessed through LiteLLM's own Python client.
    """

    if provider == "litellm_proxy":
        try:
            models = _litellm_gateway_model_capabilities(
                api_base=api_base,
                api_key_env=api_key_env,
                refresh=refresh,
            )
            supported = models.get(model)
        except Exception as exc:
            logger.debug(
                "could not inspect LiteLLM gateway capabilities base=%s model=%s: %s",
                api_base,
                model,
                exc,
            )
            supported = None
        return LLMParameterCapabilities(
            source="litellm_gateway",
            supported_openai_params=supported,
        )

    if provider == "chatgpt":
        # Narrowed to what survives litellm's own whitelist. Deliberately NOT
        # derived from ``ChatGPTConfig``: that class inherits OpenAIConfig's
        # answer, which is the thing that is wrong. See
        # ``_CHATGPT_TRANSMITTED_PARAMS``.
        return LLMParameterCapabilities(
            source="litellm_chatgpt_narrowed",
            supported_openai_params=_CHATGPT_TRANSMITTED_PARAMS,
        )

    if litellm_module is None:
        import litellm as litellm_module

    provider_config = _litellm_provider_config(litellm_module, model=model, provider=provider)
    return LLMParameterCapabilities(
        source="litellm_python",
        supported_openai_params=_litellm_supported_openai_params(
            litellm_module, model=model, provider=provider
        ),
        provider_config_params=frozenset(provider_config),
        provider_config_annotations=provider_config,
    )


def _supports_standard_param(supported_openai_params: frozenset[str] | None, name: str) -> bool:
    """Standard OpenAI params can fall back to LiteLLM's own drop logic."""
    return name in _OPENAI_STANDARD_PARAMS and (
        supported_openai_params is None or name in supported_openai_params
    )


def _supports_mapped_param(
    supported_openai_params: frozenset[str] | None,
    provider_config_params: frozenset[str],
    name: str,
) -> bool:
    """Non-standard params must be explicitly advertised by LiteLLM."""
    supported_by_openai_mapping = (
        supported_openai_params is not None and name in supported_openai_params
    )
    if name in _LITELLM_CAPABILITY_ONLY_PARAMS:
        return supported_by_openai_mapping
    return supported_by_openai_mapping or name in provider_config_params


def _add_supported_param(
    kwargs: dict,
    supported_openai_params: frozenset[str] | None,
    provider_config_params: frozenset[str],
    name: str,
    value: object,
) -> bool:
    if value is None:
        return False
    if name in _OPENAI_STANDARD_PARAMS:
        if not _supports_standard_param(supported_openai_params, name):
            return False
        kwargs[name] = value
        return True
    if _supports_mapped_param(supported_openai_params, provider_config_params, name):
        kwargs[name] = value
        return True
    return False


def _add_supported_max_tokens(
    kwargs: dict,
    supported_openai_params: frozenset[str] | None,
    max_tokens: int | None,
) -> None:
    if max_tokens is None:
        return
    if _supports_standard_param(supported_openai_params, "max_tokens"):
        kwargs["max_tokens"] = max_tokens
    elif _supports_standard_param(supported_openai_params, "max_completion_tokens"):
        kwargs["max_completion_tokens"] = max_tokens


def _thinking_param(enable_thinking: bool) -> dict[str, object]:
    if enable_thinking:
        return {"type": "enabled"}
    return {"type": "disabled"}


def thinking_request_params(
    *,
    provider: str,
    enable_thinking: object,
    supported_openai_params: frozenset[str] | None,
    provider_config_params: frozenset[str],
) -> tuple[dict[str, object], str | None]:
    """Top-level request params DERIVED from ``enable_thinking``.

    ``enable_thinking`` is an Okto Neuron switch, not a provider field, so a
    hosted provider never receives it as such: it is translated. Where the
    provider takes a ``thinking`` block it becomes one; where it only takes
    ``reasoning_effort``, thinking OFF becomes ``reasoning_effort: "none"``.
    That second translation is the one that matters: on a real reasoning
    model (``chatgpt/gpt-5.6-*``, measured 2026-09-22) it takes reasoning
    tokens from ~34 to 0, while nothing in the operator's config ever
    mentions reasoning effort at all.

    One function for both sides of the record — ``LiteLLMProvider.complete``
    builds the request from it, and the LoCoMo harness's recorded wire view
    (in the private benchmark repository) reports from it —
    so what a run SAYS it sent cannot drift from what it sent.

    Returns ``(params, dropped_reason)``: ``params`` to merge into the
    request (possibly empty), and a reason string when the value is dropped
    outright instead.
    """

    if not isinstance(enable_thinking, bool):
        return {}, "invalid-boolean-value"
    if enable_thinking is True and provider in _TEMPERATURE_TOP_P_EXCLUSIVE_PROVIDERS:
        return {}, "provider-requires-budget-tokens-and-temperature-constraints"
    if _supports_mapped_param(supported_openai_params, provider_config_params, "thinking"):
        return {"thinking": _thinking_param(enable_thinking)}, None
    if enable_thinking is False and _supports_mapped_param(
        supported_openai_params, provider_config_params, "reasoning_effort"
    ):
        return {"reasoning_effort": "none"}, None
    return {}, None


def _supports_self_hosted_extra_body(*, api_base: str, uses_custom_api_base: bool) -> bool:
    """Escape hatch for SELF-HOSTED OpenAI-compatible endpoints.

    LiteLLM cannot know the sampler surface of an arbitrary self-hosted model
    server, so we pass Qwen/vLLM/LM-Studio-style raw-body params
    (``top_k``/``min_p``/``chat_template_kwargs``) through ``extra_body``. The
    ONLY thinking-off switch on a self-hosted Qwen server is ``chat_template_kwargs:enable_thinking``
    — so this gate decides whether ``enable_thinking=False`` actually reaches the
    model.

    "Self-hosted" = a custom, non-hosted ``api_base`` on a loopback OR private/CGNAT
    address. This deliberately includes LAN (``192.168/16``, ``10/8`` …) and
    Tailscale/CGNAT (``100.64/10``) endpoints, not just loopback — a reference dev host
    is reached over the LAN/Tailscale, and gating to loopback silently dropped its
    sampler params (thinking stayed ON → reasoning runaway → truncation before JSON).
    Known hosted APIs and public endpoints are excluded so they never receive
    unsupported raw-body fields.
    """
    if not uses_custom_api_base or _is_hosted_api_base(api_base):
        return False
    try:
        return classify_api_base(api_base) in ("loopback", "private")
    except ValueError:
        return False


def _unsupported_reason(
    *,
    name: str,
    supported_openai_params: frozenset[str] | None,
    provider_config_params: frozenset[str],
) -> str:
    if name in _OPENAI_STANDARD_PARAMS:
        if supported_openai_params is None:
            return "litellm-supported-openai-params-unavailable"
        return "not-in-litellm-supported-openai-params"
    if name in _LITELLM_CAPABILITY_ONLY_PARAMS:
        return "not-in-litellm-model-capabilities"
    if supported_openai_params is None and not provider_config_params:
        return "litellm-provider-capabilities-unavailable"
    return "not-in-litellm-supported-openai-or-provider-config-params"


_GATEWAY_DROP_WARNED: set[tuple[str, tuple[str, ...]]] = set()


def _warn_gateway_narrowed_drops(
    *,
    model: str,
    configured: dict[str, object],
    dropped: dict[str, str],
) -> None:
    """Warn once when the unknown-gateway narrowing discards configured samplers.

    Silent divergence between the configured sampler surface and the bytes that
    actually reach the model is what made months of runs uninterpretable. A
    single run makes thousands of calls, so the warning is deduped per
    ``(model, dropped names)`` rather than emitted per call.
    """

    names = tuple(sorted(name for name in dropped if name in configured))
    if not names:
        return
    key = (model, names)
    if key in _GATEWAY_DROP_WARNED:
        return
    _GATEWAY_DROP_WARNED.add(key)
    logger.warning(
        "litellm gateway alias %s has no per-alias parameter metadata; "
        "configured parameter(s) %s were NOT sent. The model ran with its own "
        "defaults for those. Pin per-alias metadata on the gateway or set the "
        "value on the model host to remove this divergence.",
        model,
        ", ".join(names),
    )


def _param_accounting_summary(
    *,
    kwargs: dict,
    extra_body: dict,
    configured: dict[str, object],
    dropped: dict[str, str],
) -> tuple[list[str], list[str], dict[str, str]]:
    sent = sorted(k for k in kwargs if k not in _LITELLM_CONTROL_PARAMS)
    extra = sorted(extra_body)
    for name in configured:
        if name in kwargs or name in extra_body:
            continue
        if name == "max_tokens" and ("max_tokens" in kwargs or "max_completion_tokens" in kwargs):
            continue
        if name == "enable_thinking" and (
            "thinking" in kwargs
            or "reasoning_effort" in kwargs
            or "chat_template_kwargs" in extra_body
        ):
            continue
        if name in ("reasoning_effort", "preserve_thinking") and name in extra_body.get(
            "chat_template_kwargs", {}
        ):
            continue
        dropped.setdefault(name, "not-sent")
    return sent, extra, dropped


class LiteLLMProvider:
    """LiteLLM-backed provider. Routes any litellm-supported backend.

    ``model`` carries the canonical litellm-prefixed string (e.g.
    ``openai/Qwen3.6-…`` for an OpenAI-compatible local endpoint, or
    ``anthropic/<model>`` for Anthropic). ``api_base`` is exposed for the
    locality-safety check in the companion (``_is_local_provider``).

    The API key is intentionally read from the environment AT CALL TIME (never
    stored on the instance or logged) so that rotating it does not require a
    restart and it never appears in repr/logs.
    """

    # This provider reports its assembled request through
    # ``_notify_request_observer`` before issuing it, so a tracing wrapper can
    # show effective parameters instead of its own pre-merge arguments. A
    # provider without this flag is traced from the caller's arguments exactly
    # as before — no silent behaviour change for the CLI providers or StubLLM.
    traces_effective_request = True

    def __init__(self, resolved: "ResolvedLLM") -> None:
        self._resolved = resolved
        self.api_base: str = resolved.api_base
        self._api_key_env: str | None = resolved.api_key_env
        self._provider = resolved.provider
        self.model: str = f"{resolved.provider}/{resolved.model}"

    def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        presence_penalty: float | None = None,
        enable_thinking: bool | None = None,
        response_format: ResponseFormat | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> str:
        assert_completion_allowed()
        _preflight_provider_optional_dependencies(self._provider)

        import litellm  # lazy import — optional dep, not in core

        # Read API key at call time. Never store or log the value.
        api_key = _secret_env(self._api_key_env) if self._api_key_env else None
        uses_custom_api_base = (
            self._provider in _API_BASE_PROVIDERS or self.api_base != _DEFAULT_API_BASE
        )
        capabilities = parameter_capabilities(
            provider=self._provider,
            model=self._resolved.model,
            api_base=self.api_base,
            api_key_env=self._api_key_env,
            litellm_module=litellm,
        )
        supported_openai_params = capabilities.supported_openai_params
        provider_config_params = capabilities.provider_config_params
        gateway_narrowed = capabilities.source == "litellm_gateway" and not capabilities.known
        if gateway_narrowed:
            # The proxy adapter itself intentionally advertises an OpenAI-wide
            # superset. Without per-alias gateway metadata that is not evidence
            # that a *vendor-specific* sampler is supported, so keep only the
            # output cap plus ``temperature``. Retaining temperature does not
            # rest on the proxy's superset claim: it is part of the baseline
            # OpenAI chat-completions request body that any OpenAI-compatible
            # gateway accepts, and dropping it silently ran extraction and
            # curation at the host's default temperature instead of the
            # configured one. Thinking control stays narrowed out — that one IS
            # vendor-specific and is tracked separately.
            supported_openai_params = frozenset(
                {"max_tokens", "max_completion_tokens", "temperature"}
            )

        # A named provider connection owns its request policy. An explicit
        # positive deadline is forwarded through LiteLLM; ``None`` falls back to
        # ``DEFAULT_LLM_REQUEST_TIMEOUT_S`` (300 s) so no call can wait forever
        # on an endpoint that accepts the connection and never answers.
        # The env var remains only as a compatibility override for legacy
        # inline connections that do not reference a named provider.
        request_timeout = self._resolved.request_timeout_s
        if request_timeout is None and self._resolved.provider_ref is None:
            timeout_env = _compat_getenv("OKTO_NEURON_LLM_REQUEST_TIMEOUT")
            if timeout_env:
                try:
                    parsed_timeout = float(timeout_env)
                except ValueError:
                    logger.warning(
                        "ignoring invalid OKTO_NEURON_LLM_REQUEST_TIMEOUT=%r",
                        timeout_env,
                    )
                else:
                    if parsed_timeout > 0:
                        request_timeout = parsed_timeout
                    else:
                        logger.warning(
                            "ignoring non-positive OKTO_NEURON_LLM_REQUEST_TIMEOUT=%r",
                            timeout_env,
                        )
        if request_timeout is None:
            request_timeout = DEFAULT_LLM_REQUEST_TIMEOUT_S
        task_timeout = _current_call_timeout_s()
        if task_timeout is not None:
            request_timeout = min(request_timeout, task_timeout)

        # Frozen raw sampling-payload override for this resolved role (see
        # StepLLM/LLMDefaults.sampling_payload). Empty == feature untouched.
        raw_payload: dict[str, object] = dict(
            getattr(self._resolved, "sampling_payload", None) or {}
        )

        kwargs: dict = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            # Empty payload: today's behaviour — silently drop params the
            # chosen provider/model doesn't support (e.g. presence_penalty on
            # some local servers). A NON-empty raw payload (decision 5) is a
            # literal, operator-owned request: stop asking litellm to drop
            # anything, so an unsupported key surfaces the BACKEND's own
            # rejection instead of vanishing silently — the same as a
            # hand-written curl request would fail loud.
            "drop_params": not raw_payload,
        }
        # LiteLLM natively owns provider-specific timeout adaptation. The value
        # becomes its completion HTTP timeout; the same value also bounds Okto
        # Neuron's cancellable helper process. ``max_retries=0`` stops the SDK
        # from silently multiplying that deadline: retry policy is ours
        # (``complete_with_retry``), bounded and visible.
        kwargs["timeout"] = request_timeout
        kwargs["max_retries"] = 0

        extra_body: dict = {}

        if raw_payload:
            # RAW SAMPLING PAYLOAD OVERRIDE. Every reserved key
            # (model/messages/api_base/api_key/drop_params/timeout) was
            # already rejected at config-validation time
            # (``_check_sampling_payload``), so every remaining key here is
            # safe to forward untouched: no whitelist, no range validation, no
            # capability probing — the backend is the validator, deliberately.
            # Route OpenAI-standard chat-completions keys top-level (matching
            # what a normal client sends) and everything else through
            # extra_body — the same split ``_supports_self_hosted_extra_body``
            # uses today for top_k/min_p/chat_template_kwargs, just applied
            # unconditionally rather than gated on a self-hosted endpoint: an
            # operator who configured a raw payload made that call explicitly,
            # for whichever backend they pointed this role at.
            #
            # The payload is authoritative over this method's own per-call
            # sampler arguments below (temperature=/max_tokens=/… — e.g. the
            # extractor class's hardcoded defaults, or its empty-result cold
            # retry): a role that opted into a raw payload owns the WHOLE
            # request, so an Okto Neuron-injected value never leaks back in for
            # a key the payload already sets. Concretely this means the
            # empty-result cold retry and similar per-call sampler nudges have
            # no effect on a raw-payload role — an accepted tradeoff of full
            # manual control, not an oversight. ``response_format`` is the one
            # exception: it is not a sampling preference, it is Okto Neuron's
            # own structured-output contract with the parser, so the caller's
            # schema still applies whenever the payload doesn't already set one.
            for name, value in raw_payload.items():
                if name in _OPENAI_STANDARD_PARAMS:
                    kwargs[name] = value
                else:
                    extra_body[name] = value
            configured: dict[str, object] = dict(raw_payload)
            dropped_params: dict[str, str] = {}
        else:
            configured = dict(getattr(self._resolved, "parameters", {}) or {})
            # Per-call task policy is the final override (for example the extractor's
            # one-shot cold retry). Normal calls pass ``None`` and therefore preserve
            # the user's configured parameter map.
            configured.update(
                {
                    name: value
                    for name, value in (
                        ("temperature", temperature),
                        ("max_tokens", max_tokens),
                        ("top_p", top_p),
                        ("top_k", top_k),
                        ("min_p", min_p),
                        ("presence_penalty", presence_penalty),
                        ("enable_thinking", enable_thinking),
                    )
                    if value is not None
                }
            )

            dropped_params = {}
            top_k_sent = False
            min_p_sent = False
            repeat_penalty_sent = False
            thinking_sent = False
            for name, value in configured.items():
                if name in MANAGED_LLM_PARAMETERS:
                    dropped_params[name] = "managed-by-okto-neuron"
                    continue
                if name in ("reasoning_effort", "preserve_thinking"):
                    # Operator-supplied chat-template knobs for a self-hosted
                    # endpoint (e.g. Qwen's ``reasoning_effort: "xhigh"``), sent
                    # exclusively via extra_body.chat_template_kwargs below —
                    # never as a top-level ``kwargs["reasoning_effort"]``, which
                    # is reserved for the unrelated internal thinking-off
                    # auto-set ("none") a real reasoning-capable hosted model
                    # understands. Accounted for in _param_accounting_summary.
                    continue
                if name == "max_tokens":
                    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                        dropped_params[name] = "invalid-integer-value"
                        continue
                    before = set(kwargs)
                    _add_supported_max_tokens(
                        kwargs,
                        supported_openai_params,
                        value,
                    )
                    if set(kwargs) == before:
                        dropped_params[name] = _unsupported_reason(
                            name=name,
                            supported_openai_params=supported_openai_params,
                            provider_config_params=provider_config_params,
                        )
                    continue
                if name == "enable_thinking":
                    derived, dropped_reason = thinking_request_params(
                        provider=self._provider,
                        enable_thinking=value,
                        supported_openai_params=supported_openai_params,
                        provider_config_params=provider_config_params,
                    )
                    if dropped_reason is not None:
                        dropped_params[name] = dropped_reason
                    elif derived:
                        kwargs.update(derived)
                        thinking_sent = True
                    continue
                descriptor = capabilities.descriptor(name)
                if descriptor is not None and not _parameter_value_matches(descriptor, value):
                    dropped_params[name] = f"invalid-{descriptor.kind}-value"
                    continue
                added = _add_supported_param(
                    kwargs,
                    supported_openai_params,
                    provider_config_params,
                    name,
                    value,
                )
                if name == "top_k":
                    top_k_sent = added
                elif name == "min_p":
                    min_p_sent = added
                elif name == "repeat_penalty":
                    repeat_penalty_sent = added
                if not added:
                    dropped_params[name] = _unsupported_reason(
                        name=name,
                        supported_openai_params=supported_openai_params,
                        provider_config_params=provider_config_params,
                    )

            if (
                self._provider in _TEMPERATURE_TOP_P_EXCLUSIVE_PROVIDERS
                and "temperature" in kwargs
                and kwargs.pop("top_p", None) is not None
            ):
                # Bedrock/Anthropic Claude reject the pair outright; keep temperature
                # (the caller's primary sampling knob) and drop top_p instead of
                # letting the call fail.
                dropped_params["top_p"] = "mutually-exclusive-with-temperature"

            # LiteLLM's supported-param registry is authoritative for known providers.
            # For local OpenAI-compatible servers, keep a scoped raw-body escape hatch:
            # Qwen/vLLM/LM Studio-style samplers are intentionally endpoint-specific.
            parameter_mode = getattr(self._resolved, "parameter_mode", "auto")
            local_extended = parameter_mode == "local_extended" or (
                parameter_mode == "auto" and self._provider in LOCAL_EXTENDED_DRIVERS
            )
            if local_extended and _supports_self_hosted_extra_body(
                api_base=self.api_base, uses_custom_api_base=uses_custom_api_base
            ):
                configured_top_k = configured.get("top_k")
                configured_min_p = configured.get("min_p")
                configured_repeat_penalty = configured.get("repeat_penalty")
                configured_thinking = configured.get("enable_thinking")
                configured_reasoning_effort = configured.get("reasoning_effort")
                configured_preserve_thinking = configured.get("preserve_thinking")
                if configured_top_k is not None and not top_k_sent:
                    extra_body["top_k"] = configured_top_k
                    dropped_params.pop("top_k", None)
                if configured_min_p is not None and not min_p_sent:
                    extra_body["min_p"] = configured_min_p
                    dropped_params.pop("min_p", None)
                if (
                    isinstance(configured_repeat_penalty, (int, float))
                    and not isinstance(configured_repeat_penalty, bool)
                    and not repeat_penalty_sent
                ):
                    extra_body["repeat_penalty"] = configured_repeat_penalty
                    dropped_params.pop("repeat_penalty", None)
                # enable_thinking/reasoning_effort/preserve_thinking all live in the
                # same raw ``chat_template_kwargs`` body object on a Qwen-style
                # self-hosted endpoint, so they accumulate into one dict rather than
                # each claiming extra_body["chat_template_kwargs"] independently.
                chat_template_kwargs: dict[str, object] = {}
                if isinstance(configured_thinking, bool) and not thinking_sent:
                    chat_template_kwargs["enable_thinking"] = configured_thinking
                    dropped_params.pop("enable_thinking", None)
                if isinstance(configured_reasoning_effort, str) and configured_reasoning_effort:
                    chat_template_kwargs["reasoning_effort"] = configured_reasoning_effort
                    dropped_params.pop("reasoning_effort", None)
                if isinstance(configured_preserve_thinking, bool):
                    chat_template_kwargs["preserve_thinking"] = configured_preserve_thinking
                    dropped_params.pop("preserve_thinking", None)
                if chat_template_kwargs:
                    extra_body["chat_template_kwargs"] = chat_template_kwargs

        if (
            api_key is None
            and uses_custom_api_base
            and self._provider in _API_BASE_PROVIDERS
            and _allows_placeholder_api_key(self.api_base)
        ):
            # Keyless self-hosted endpoint: send a placeholder so litellm's openai
            # path doesn't fail with "Missing credentials". The server ignores it.
            api_key = _PLACEHOLDER_API_KEY
        if api_key is not None:
            kwargs["api_key"] = api_key
        # Deliberately unconditional (NOT gated on _API_BASE_PROVIDERS): a vault
        # config that sets a non-default api_base did so explicitly, often to
        # route through a local audit/redaction proxy under allow_remote:false.
        # Silently dropping it for providers outside _API_BASE_PROVIDERS would
        # send the full prompt + API key straight to that provider's hosted
        # endpoint instead — an invisible remote-egress leak the vault owner
        # never consented to. The extra_body escape hatch below stays scoped to
        # _API_BASE_PROVIDERS (that's about raw OpenAI-compat body params, a
        # different and unrelated concern).
        if uses_custom_api_base:
            kwargs["api_base"] = self.api_base
        if extra_body:
            kwargs["extra_body"] = extra_body
        if raw_payload:
            if response_format is not None and "response_format" not in kwargs:
                kwargs["response_format"] = response_format
        else:
            _add_supported_param(
                kwargs,
                supported_openai_params,
                provider_config_params,
                "response_format",
                response_format,
            )
        sent_params, extra_body_params, dropped_params = _param_accounting_summary(
            kwargs=kwargs,
            extra_body=extra_body,
            configured=configured,
            dropped=dropped_params,
        )
        _call_param_plan.value = {
            "sent": sent_params,
            "extra_body": extra_body_params,
            "omitted": dropped_params,
        }
        # The request is now fully assembled and not yet issued. This is the
        # ONLY place the effective parameters exist, so it is the only place
        # they are reported from — the ingest trace shows what this call
        # actually sends, including a raw ``sampling_payload`` that has just
        # overridden this method's own sampler arguments above. Reporting here
        # rather than after the call keeps the trace live during a slow call
        # and keeps a FAILED call traced.
        #
        # Known divergence, accepted: the structured-output fallback below may
        # reissue without ``response_format`` after this fires, so the trace
        # then names a ``response_format`` the successful retry did not send.
        # A second notification would break the one-request-event-per-call
        # invariant the tracing wrapper guarantees; the retry logs its own
        # warning instead.
        _notify_request_observer(
            kwargs=kwargs,
            extra_body=extra_body,
            omitted=dropped_params,
            sampling_payload_applied=bool(raw_payload),
        )

        if gateway_narrowed:
            _warn_gateway_narrowed_drops(
                model=self.model,
                configured=configured,
                dropped=dropped_params,
            )

        prompt_chars = sum(len(m.content) for m in messages)
        logger.debug(
            "litellm param plan model=%s sent=%s extra_body=%s dropped=%s",
            self.model,
            sent_params,
            extra_body_params,
            dropped_params,
        )
        logger.debug(
            "litellm request model=%s configured=%s structured=%s prompt_chars=%d\n"
            "--- prompt ---\n%s",
            self.model,
            sorted(configured),
            response_format is not None,
            prompt_chars,
            "\n".join(f"[{m.role}] {m.content}" for m in messages),
        )
        # P2 streaming: a caller that supplied ``on_token`` wants token-level
        # progress. The cancellable helper-process path (deadline/cancel
        # predicate active) does NOT support streaming across the process
        # boundary, so it deliberately falls back to the non-stream request
        # — the heartbeat that accompanies streaming callers covers the idle
        # timeout in that case, and the debug log names the fallback.
        streaming = on_token is not None and (
            _current_call_timeout_s() is None
            and _current_call_cancel_predicate() is None
        )
        if on_token is not None and not streaming:
            logger.debug(
                "token streaming requested for %s but a task deadline/cancel "
                "predicate is active; falling back to non-stream",
                self.model,
            )
        try:
            try:
                if streaming:
                    stream_kwargs = dict(kwargs)
                    stream_kwargs["stream"] = True
                    # Ask for usage in the stream's final chunk so the
                    # assembled response carries the same usage a non-stream
                    # call would have returned.
                    stream_kwargs.setdefault(
                        "stream_options", {"include_usage": True}
                    )
                    response = _consume_completion_stream(
                        litellm, _run_litellm_completion(litellm, stream_kwargs), on_token
                    )
                else:
                    response = _run_litellm_completion(litellm, kwargs)
            except Exception as exc:
                if response_format is None or not _looks_like_response_format_rejection(exc):
                    raise
                logger.warning(
                    "litellm structured output rejected for %s; retrying without "
                    "response_format: %s",
                    self.model,
                    _redact_api_key(exc, api_key),
                )
                retry_kwargs = dict(kwargs)
                retry_kwargs.pop("response_format", None)
                response = _run_litellm_completion(litellm, retry_kwargs)
            # `response` is a non-streaming ModelResponse here (we never pass
            # stream=True), but litellm's return type is the union with the
            # streaming wrapper. Access defensively so the type-checker is happy
            # and a surprise streaming object can't crash the capture path.
            choices = getattr(response, "choices", None) or []
            first = choices[0] if choices else None
            message = getattr(first, "message", None)
            content: str = (getattr(message, "content", None) or "") if message else ""
            # getattr WITH a default is mandatory: litellm DELETES
            # ``reasoning_content`` from the Message when the provider did not
            # send one (types/utils.py:1233-1241), so plain attribute access
            # raises AttributeError on every ordinary completion.
            reasoning_content = (
                getattr(message, "reasoning_content", None) if message else None
            )
            finish_reason = getattr(first, "finish_reason", None)
            # litellm's map_finish_reason (litellm_core_utils/core_helpers.py:109-116)
            # silently defaults any UNMAPPED provider finish reason to "stop"; the
            # raw value survives only as provider_specific_fields["native_finish_reason"]
            # (types/utils.py:1400-1408). Read it so an unknown abnormal stop cannot
            # reach callers disguised as a clean "stop".
            _psf = getattr(first, "provider_specific_fields", None)
            native_finish_reason = (
                _psf.get("native_finish_reason") if isinstance(_psf, dict) else None
            )
            # Okto Neuron sends no ``tools`` today, so this is normally empty.
            # It is captured anyway because a tool call is the one response
            # shape where ``content`` is legitimately empty, and an empty
            # answer with no recorded reason is exactly the failure mode this
            # codebase keeps re-learning.
            tool_calls = getattr(message, "tool_calls", None) if message else None
        except Exception as exc:
            safe_error = _redact_api_key(exc, api_key)
            logger.warning("litellm completion failed for %s: %s", self.model, safe_error)
            classification = classify_provider_exception(exc)
            raise LLMProviderError(
                f"litellm completion failed for {self.model}: {safe_error}",
                category=classification.category,
                retry_after_s=classification.retry_after_s,
                retryable=classification.retryable,
            ) from None

        logger.debug("--- raw completion ---\n%s", content)

        # Phase 1b: observe server-side prefix caching. Read usage defensively —
        # it may be a pydantic object or a dict, and cached_tokens lives under
        # prompt_tokens_details when the provider reports it.
        usage = getattr(response, "usage", None)
        prompt_tokens = _safe_get(usage, "prompt_tokens")
        completion_tokens = _safe_get(usage, "completion_tokens")
        details = _safe_get(usage, "prompt_tokens_details")
        cached_tokens = _safe_get(details, "cached_tokens")
        stats: dict[str, object] = {
            k: v
            for k, v in (
                ("prompt_tokens", prompt_tokens),
                ("completion_tokens", completion_tokens),
                ("cached_tokens", cached_tokens),
            )
            if isinstance(v, int)
        }
        # Surface output-cap truncation. When the provider stopped because it hit
        # max_tokens (OpenAI-compat finish_reason="length"), the JSON is almost
        # certainly cut mid-object and the extractor will silently parse 0 nodes.
        # Record the reason so callers can flag truncation; warn loudly here.
        if finish_reason is not None:
            stats["finish_reason"] = finish_reason
        if tool_calls:
            stats["tool_calls"] = _jsonable(tool_calls)
        if native_finish_reason is not None:
            stats["native_finish_reason"] = native_finish_reason
            # litellm sets native_finish_reason for ANY value it rewrote, which
            # includes plain ALIASES of a clean stop ("end_turn", "COMPLETE",
            # "eos_token", "STOP" all map to "stop" — see _FINISH_REASON_MAP).
            # Only a genuinely UNMAPPED reason is a hidden abnormal stop, so
            # record that distinction here where litellm's own map is available;
            # callers must not re-derive it.
            # ``_NATIVE_ABNORMAL_STOP_REASONS`` covers the inverse case: a reason
            # litellm DOES map, but to a misleading clean "stop".
            if (
                _finish_reason_is_unmapped(native_finish_reason)
                or native_finish_reason in _NATIVE_ABNORMAL_STOP_REASONS
            ):
                stats["finish_reason_unmapped"] = True
        # Detect a BAD server-side reasoning split. llama.cpp (and friends) split
        # the reasoning out into ``reasoning_content`` but can get the boundary
        # wrong, spilling the tail of the chain-of-thought into ``content`` — the
        # 2026-09 incident where 2,642 chars of raw reasoning reached a user.
        # ``strip_reasoning`` rescues it (its dangling-</think> branch is strictly
        # more capable than litellm's own handling, which short-circuits on
        # ``reasoning_content`` and returns content verbatim), but the rescue was
        # invisible. Warn when BOTH happened.
        #
        # Do NOT enable litellm's ``merge_reasoning_content_in_choices``: it is
        # STREAMING-only and it INJECTS <think> tags into content — the opposite
        # of what we want here.
        stripped = strip_reasoning(content)
        stripped_chars = len(content) - len(stripped)
        if reasoning_content and stripped_chars > 0:
            # Never log the reasoning text or the stripped text: both can be long
            # and can contain vault data. Counts and the model name only.
            logger.warning(
                "litellm reasoning split MISBOUNDED model=%s — provider populated "
                "reasoning_content yet %d chars of reasoning remained in content and "
                "had to be stripped; the server-side split boundary is wrong",
                self.model,
                stripped_chars,
            )
        # Conditional so a usage-free response still records stats=None (callers
        # branch on falsiness); never write zero/False placeholders.
        if stripped_chars > 0:
            stats["reasoning_stripped_chars"] = stripped_chars
        if reasoning_content:
            stats["reasoning_content_present"] = True
        if finish_reason == "length":
            logger.warning(
                "litellm completion TRUNCATED (finish_reason=length) model=%s "
                "prompt_tokens=%s completion_tokens=%s max_tokens=%s — JSON likely "
                "cut mid-object; extractor may yield 0 candidates",
                self.model,
                prompt_tokens,
                completion_tokens,
                max_tokens,
            )
        _set_last_call_stats(stats or None)
        logger.info(
            "litellm usage model=%s prompt_tokens=%s completion_tokens=%s cached_tokens=%s step=%s",
            self.model,
            prompt_tokens,
            completion_tokens,
            cached_tokens,
            current_call_step(),
        )
        return stripped


class StubLLM:
    """Deterministic offline provider. Echoes a marker; for CI / contract tests."""

    model = "stub"

    def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        presence_penalty: float | None = None,
        enable_thinking: bool | None = None,
        response_format: ResponseFormat | None = None,
    ) -> str:
        assert_completion_allowed()
        _call_param_plan.value = {
            "sent": [],
            "extra_body": [],
            "omitted": {},
        }
        schema = response_format.get("json_schema") if isinstance(response_format, dict) else None
        schema_name = schema.get("name") if isinstance(schema, dict) else None
        if schema_name == "marginalia_merge_verdict":
            return '{"same":false,"confidence":0.99,"reason":"stub distinct"}'
        if schema_name == "marginalia_candidate_curator":
            return '{"action":"commit","confidence":0.9,"reason":"stub curator commit"}'
        if schema_name == "marginalia_relation_curator":
            last = messages[-1].content if messages else ""
            predicate = "uses"
            for line in last.splitlines():
                if line.startswith("Predicate/type:"):
                    candidate = line.partition(":")[2].strip()
                    if candidate:
                        predicate = candidate
                    break
            return (
                '{"action":"commit","confidence":0.9,'
                f'"canonical_predicate":"{predicate}",'
                '"predicate_definition":"The subject has the proposed relation to the object.",'
                '"predicate_direction":"subject_to_object",'
                '"inverse_direction_required":false,'
                '"subject_supported":true,"predicate_supported":true,'
                '"object_supported":true,"direction_supported":true,'
                '"unsupported_inference":false,"structural_noise":false,'
                '"redundant":false,"useful":true,'
                '"reason":"stub relation curator commit"}'
            )
        last = messages[-1].content if messages else ""
        return f"[stub-llm] {last[:200]}"


def _looks_like_response_format_rejection(exc: Exception) -> bool:
    """True when a backend rejected LiteLLM/OpenAI structured-output params."""
    text = f"{type(exc).__name__}: {exc}".casefold()
    return any(
        marker in text
        for marker in (
            "response_format",
            "json_schema",
            "response schema",
            "structured output",
            "unsupported parameter",
            "unsupportedparams",
            "unknown parameter",
            "unexpected keyword",
            "strict",
        )
    )


def get_provider(resolved: "ResolvedLLM") -> LLMProvider:
    """Build a provider from a :class:`~okto_neuron.config._vault.ResolvedLLM`.

    ``resolved.provider == "stub"`` returns :class:`StubLLM` (offline/CI).
    All other providers are routed through :class:`LiteLLMProvider`.
    """
    provider = _build_provider(resolved)
    # Optional MLflow GenAI export, wrapped at the ONE seam every provider
    # passes through so the CLI pseudo-providers are traced too. Returns
    # ``provider`` untouched — same object, no extra frame — unless
    # ``OKTO_NEURON_MLFLOW_TRACKING_URI`` is set. See ``llm/_telemetry.py`` and
    # docs/observability.md.
    from okto_neuron.llm._telemetry import wrap_provider

    return wrap_provider(provider, resolved)


def trace_external_completion(**kwargs: object) -> object:
    """Trace a completion the CALLER made itself, not one this layer routed.

    A thin re-export of ``okto_neuron.llm._telemetry.trace_external_completion``
    so an external caller has one public name to import
    (``from okto_neuron.llm import trace_external_completion``) and does not
    reach into a private module. It is a function rather than a module-level
    ``from ... import`` on purpose: ``okto_neuron.llm`` is on the import path of
    every CLI invocation, and this keeps even the telemetry module out of it
    until something actually asks to trace.

    Off unless ``OKTO_NEURON_MLFLOW_TRACKING_URI`` is set, in which case it
    yields a no-op handle. See ``llm/_telemetry.py`` and docs/observability.md.
    """
    from okto_neuron.llm._telemetry import trace_external_completion as _impl

    return _impl(**kwargs)  # type: ignore[arg-type]


def trace_parent(name: str, **kwargs: object) -> object:
    """Open a span that LLM calls on this thread become CHILDREN of.

    Thin re-export of ``okto_neuron.llm._telemetry.trace_parent``, lazy for the
    same reason as ``trace_external_completion``: ``okto_neuron.llm`` is on the
    import path of every CLI invocation and the telemetry module should stay
    off it until something asks to trace.

    Use it around an operation whose SHAPE matters — one question's retrieval
    and synthesis, one document's extraction and curation — so the trace view
    shows that operation as a tree instead of its calls as unrelated rows. A
    no-op context manager when telemetry is off.
    """
    from okto_neuron.llm._telemetry import trace_parent as _impl

    return _impl(name, **kwargs)  # type: ignore[arg-type]


def bind_parent(fn: object) -> object:
    """Wrap a callable so it keeps THIS thread's parent span on a worker thread.

    Thin re-export of ``okto_neuron.llm._telemetry.bind_parent``. Apply at the
    ``pool.submit`` site: a thread-local parent is invisible inside the pool,
    so without it every call a fan-out makes exports as its own root trace and
    the operation that submitted the work appears to have made none.

    Returns ``fn`` unchanged when telemetry is off or no parent is open.
    """
    from okto_neuron.llm._telemetry import bind_parent as _impl

    return _impl(fn)


def trace_child(name: str, **kwargs: object) -> object:
    """Time one NON-LLM step inside the current :func:`trace_parent`.

    For the work that never reaches a provider and so has no span of its own —
    retrieval above all, whose share of an answer's latency is otherwise
    invisible. A no-op when telemetry is off or no parent is open.
    """
    from okto_neuron.llm._telemetry import trace_child as _impl

    return _impl(name, **kwargs)  # type: ignore[arg-type]


def flush_telemetry(timeout_s: float = 10.0) -> bool:
    """Block until queued MLflow spans have been exported. Shutdown only.

    The export queue is drained by a DAEMON thread, so a short-lived process
    that exits immediately after its last completion can drop the spans still
    in flight — which is exactly the shape of the LoCoMo judge, whose whole
    run is a burst of completions followed by an exit. Nothing on a request
    path calls this; a long-running daemon never needs it.

    Returns True when the queue drained within ``timeout_s`` (trivially so
    when telemetry is off, since there is nothing to drain).
    """
    from okto_neuron.llm._telemetry import flush

    return flush(timeout_s)


def _build_provider(resolved: "ResolvedLLM") -> LLMProvider:
    if resolved.provider == "stub":
        return StubLLM()
    if resolved.provider == "claude_cli":
        from okto_neuron.llm._claude_cli import ClaudeCliProvider  # lazy — keeps core import-light

        return ClaudeCliProvider(resolved)
    if resolved.provider == "pi_cli":
        from okto_neuron.llm._pi_cli import PiCliProvider  # lazy — keeps core import-light

        return PiCliProvider(resolved)
    if resolved.provider == "codex_cli":
        from okto_neuron.llm._codex_cli import CodexCliProvider  # lazy — keeps core import-light

        return CodexCliProvider(resolved)
    if resolved.provider == "chatgpt":
        # EXPLORATION ONLY and opt-in gated; the constructor refuses unless
        # OKTO_NEURON_ENABLE_CHATGPT is set. See llm/_chatgpt.py and
        # docs/remote-providers.md.
        from okto_neuron.llm._chatgpt import ChatGPTProvider  # lazy — keeps core import-light

        return ChatGPTProvider(resolved)
    return LiteLLMProvider(resolved)


__all__ = [
    "Message",
    "LLMParameterCapabilities",
    "LLMProvider",
    "LLMProviderError",
    "ProviderErrorCategory",
    "ProviderErrorClassification",
    "LiteLLMProvider",
    "ResponseFormat",
    "StubLLM",
    "flush_telemetry",
    "get_provider",
    "classify_provider_exception",
    "complete_with_retry",
    "parameter_capabilities",
    "provider_error_summary",
    "provider_retry_delay",
    "sampler_overrides",
    "strip_reasoning",
    "thinking_request_params",
    "bind_parent",
    "trace_child",
    "trace_external_completion",
    "trace_parent",
    "warm_provider_dependencies",
]
