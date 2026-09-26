"""Killable subprocess boundary for ingest-owned LiteLLM HTTP calls.

LiteLLM's generic async API may delegate a synchronous adapter to an executor;
cancelling that task abandons the await but leaves the HTTP thread alive. Bulk
ingest therefore isolates only the pure model request in a helper process. The
main remember thread retains every parse/gate/commit step, and Stop can terminate
the owned request process without touching an atomic graph write.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
from typing import Callable

_STDERR_TAIL = 1000
_ACTIVE_LOCK = threading.Lock()
_ACTIVE: dict[subprocess.Popen, Callable[[], bool] | None] = {}
_FORCE_CANCEL = threading.Event()


def _register(proc: subprocess.Popen, predicate: Callable[[], bool] | None) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE[proc] = predicate


def _unregister(proc: subprocess.Popen) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.pop(proc, None)


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        with contextlib.suppress(OSError):
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
            )
        with contextlib.suppress(OSError):
            proc.kill()
    elif hasattr(os, "killpg"):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    else:
        with contextlib.suppress(OSError):
            proc.kill()


def cancel_requested_litellm_calls() -> int:
    """Terminate active model helpers whose owning operation requested stop."""
    with _ACTIVE_LOCK:
        active = tuple(_ACTIVE.items())
    cancelled = 0
    for proc, predicate in active:
        if predicate is None:
            continue
        try:
            should_cancel = predicate()
        except Exception:  # noqa: BLE001 - never kill a call for a broken predicate
            continue
        if should_cancel and proc.poll() is None:
            _kill_process_tree(proc)
            cancelled += 1
    return cancelled


def cancel_active_litellm_calls() -> int:
    """Terminate every active model helper during process shutdown."""
    _FORCE_CANCEL.set()
    with _ACTIVE_LOCK:
        active = tuple(proc for proc in _ACTIVE if proc.poll() is None)
    for proc in active:
        _kill_process_tree(proc)
    return len(active)


def _response_from_payload(payload: dict) -> SimpleNamespace:
    """Rebuild the response shape ``LiteLLMProvider.complete`` reads.

    WORKER PAYLOAD CONTRACT
    -----------------------
    This facade is the ONLY thing the parent process ever sees of a daemon
    completion — inside the daemon both the ingest and ask paths install a
    cancel predicate, so ``_run_litellm_completion`` always routes through the
    worker and never calls litellm in-process. A field the worker does not
    serialize does not exist upstream, however faithfully the parent reads it.

    v1 carried content / finish_reason / prompt_tokens / completion_tokens /
    cached_tokens. v2 adds ``native_finish_reason``, ``reasoning_content``,
    ``tool_calls``, ``total_tokens`` and ``reasoning_tokens``. The addition is
    backward compatible in BOTH directions: every v2 field is read with
    ``.get`` and a v1 payload simply yields ``None``, which is the same value
    the parent saw before; and a v2 payload fed to an old parent carries extra
    keys it ignores.

    ``provider_specific_fields`` is rebuilt as a real ``dict`` (not a
    ``SimpleNamespace``) on purpose: the parent tests it with
    ``isinstance(_psf, dict)``. It is left as ``None`` when the worker
    reported no native reason, matching litellm, which populates that field
    only when it actually rewrote the reason.
    """
    details = SimpleNamespace(cached_tokens=payload.get("cached_tokens"))
    completion_details = SimpleNamespace(reasoning_tokens=payload.get("reasoning_tokens"))
    usage = SimpleNamespace(
        prompt_tokens=payload.get("prompt_tokens"),
        completion_tokens=payload.get("completion_tokens"),
        total_tokens=payload.get("total_tokens"),
        prompt_tokens_details=details,
        completion_tokens_details=completion_details,
    )
    message = SimpleNamespace(
        content=payload.get("content") or "",
        tool_calls=payload.get("tool_calls"),
    )
    reasoning_content = payload.get("reasoning_content")
    if reasoning_content:
        # Set ONLY when present, mirroring litellm, which deletes the
        # attribute outright when the provider sent none. The parent reads it
        # with ``getattr(..., None)``, so absence and ``None`` behave alike —
        # but a downstream reader that checks ``hasattr`` gets the truth.
        message.reasoning_content = reasoning_content
    native_finish_reason = payload.get("native_finish_reason")
    choice = SimpleNamespace(
        message=message,
        finish_reason=payload.get("finish_reason"),
        provider_specific_fields=(
            {"native_finish_reason": native_finish_reason}
            if native_finish_reason is not None
            else None
        ),
    )
    return SimpleNamespace(choices=[choice], usage=usage)


def run_cancellable_completion(kwargs: dict, predicate: Callable[[], bool]) -> SimpleNamespace:
    """Execute one completion in an owned process and return a response facade."""
    from okto_neuron.llm import LLMCallCancelled, LLMProviderError

    if _FORCE_CANCEL.is_set() or predicate():
        raise LLMCallCancelled()
    api_key = kwargs.get("api_key")

    def _redact(value: object) -> str:
        rendered = str(value)
        if isinstance(api_key, str) and api_key:
            rendered = rendered.replace(api_key, "[redacted]")
        return rendered

    try:
        request = json.dumps(kwargs, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise LLMProviderError(
            f"LiteLLM request is not serializable for cancellable execution: {exc}",
            category="invalid_request",
            retryable=False,
            cause=exc,
        ) from exc

    popen_kwargs: dict = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "cwd": tempfile.gettempdir(),
        "env": os.environ.copy(),
    }
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(
        [sys.executable, "-m", "okto_neuron.llm._litellm_worker"],
        **popen_kwargs,
    )
    _register(proc, predicate)
    if _FORCE_CANCEL.is_set() or predicate():
        _kill_process_tree(proc)
    configured_timeout = kwargs.get("timeout")
    request_timeout = (
        float(configured_timeout)
        if isinstance(configured_timeout, (int, float)) and not isinstance(configured_timeout, bool)
        else None
    )
    helper_timeout = request_timeout + 5.0 if request_timeout is not None else None
    try:
        try:
            stdout, stderr = proc.communicate(input=request, timeout=helper_timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_process_tree(proc)
            proc.wait()
            assert request_timeout is not None
            raise LLMProviderError(
                f"cancellable LiteLLM helper timed out after {request_timeout:.0f}s",
                category="timeout",
                retryable=True,
                cause=exc,
            ) from exc

        if _FORCE_CANCEL.is_set() or predicate():
            raise LLMCallCancelled()
        if proc.returncode != 0:
            raise LLMProviderError(
                f"cancellable LiteLLM helper exited {proc.returncode}: "
                f"{_redact((stderr or '')[-_STDERR_TAIL:]) or 'no error output'}",
                category="unknown",
                retryable=False,
            )
        try:
            payload = json.loads(stdout)
        except (TypeError, ValueError) as exc:
            raise LLMProviderError(
                "cancellable LiteLLM helper returned invalid protocol output",
                category="malformed_output",
                retryable=False,
                cause=exc,
            ) from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            error_type = (
                payload.get("error_type", "LiteLLMError")
                if isinstance(payload, dict)
                else "LiteLLMError"
            )
            error = (
                payload.get("error", "unknown provider error")
                if isinstance(payload, dict)
                else "unknown provider error"
            )
            safe_error = _redact(error)
            try:
                raise LLMProviderError(
                    f"{error_type}: {safe_error}",
                    category=payload.get("error_category", "unknown"),
                    retry_after_s=payload.get("retry_after_s"),
                    retryable=payload.get("retryable"),
                )
            except (TypeError, ValueError) as exc:
                raise LLMProviderError(
                    f"{error_type}: {safe_error}",
                    category="unknown",
                    retryable=False,
                    cause=exc,
                ) from exc
        return _response_from_payload(payload)
    finally:
        _unregister(proc)
        if proc.poll() is None:
            _kill_process_tree(proc)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2)


__all__ = [
    "cancel_active_litellm_calls",
    "cancel_requested_litellm_calls",
    "run_cancellable_completion",
]
