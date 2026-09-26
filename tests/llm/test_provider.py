"""Tests for the LLM provider layer (Stage D — LiteLLM substrate)."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import os
import socket
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from inspect import signature
from io import StringIO
from types import SimpleNamespace

import pytest

from okto_neuron.config._vault import LLMConfig, LLMDefaults, ResolvedLLM
from okto_neuron._internal.completion_guard import (
    CompletionForbiddenError,
    prohibit_completions,
)
from okto_neuron.llm import (
    LLMCallCancelled,
    LLMProvider,
    LLMProviderError,
    LiteLLMProvider,
    Message,
    StubLLM,
    _notify_request_observer,
    _set_call_cancel_predicate,
    _set_request_observer,
    _scoped_call_timeout,
    classify_provider_exception,
    get_provider,
    parameter_capabilities,
    strip_reasoning,
)
from okto_neuron.llm._litellm_process import (
    cancel_requested_litellm_calls,
    run_cancellable_completion,
)

_OMLX = "http://127.0.0.1:8123/v1"
_LIVE_API_BASE = os.environ.get("OKTO_NEURON_LLM_BASE_URL", "").strip()
_LIVE_MODEL = os.environ.get("OKTO_NEURON_REALMODEL_MODEL", "unsloth/Qwen3.6-27B-NVFP4").strip()
_LITELLM_CONTROL_PARAMS = {
    "api_base",
    "api_key",
    "drop_params",
    "messages",
    "model",
    "timeout",
}
_OPENAI_STANDARD_PARAMS = {
    "max_completion_tokens",
    "max_tokens",
    "presence_penalty",
    "response_format",
    "temperature",
    "top_p",
}


def _stub_resolved(**overrides) -> ResolvedLLM:
    """Build a ResolvedLLM with stub provider and sensible defaults."""
    base = dict(
        provider="stub",
        api_base=_OMLX,
        model="stub-model",
        api_key_env=None,
        max_tokens=1024,
        temperature=0.7,
        top_p=0.95,
        top_k=20,
        min_p=0.0,
        presence_penalty=0.0,
        enable_thinking=True,
    )
    base.update(overrides)
    return ResolvedLLM(**base)


def test_completion_guard_observes_and_rejects_provider_calls() -> None:
    with prohibit_completions("ordinary recall") as probe:
        with pytest.raises(CompletionForbiddenError, match="ordinary recall"):
            StubLLM().complete([Message(role="user", content="must not run")])

    assert probe.attempted_calls == 1


def _compat_resolved(**overrides) -> ResolvedLLM:
    """Build a ResolvedLLM with canonical OpenAI provider."""
    base = dict(
        provider="openai",
        api_base=_OMLX,
        model="Qwen3.5-0.8B",
        api_key_env=None,
        max_tokens=1024,
        temperature=0.7,
        top_p=0.95,
        top_k=20,
        min_p=0.0,
        presence_penalty=0.0,
        enable_thinking=False,
    )
    base.update(overrides)
    return ResolvedLLM(**base)


def _bedrock_resolved(**overrides) -> ResolvedLLM:
    """Build a ResolvedLLM with the LiteLLM Bedrock provider."""
    base = dict(
        provider="bedrock",
        api_base=_OMLX,
        model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        api_key_env=None,
        max_tokens=1024,
        temperature=0.7,
        top_p=0.95,
        top_k=20,
        min_p=0.0,
        presence_penalty=0.0,
        enable_thinking=False,
    )
    base.update(overrides)
    return ResolvedLLM(**base)


def test_set_call_step_returns_previous_value_for_save_restore() -> None:
    """set_call_step is the save/restore primitive callers (e.g. the
    companion's per-call step-label wrapper) use to nest scoped labels on the
    same thread without clobbering an outer caller's label: it must return
    whatever raw value was set before, so ``prev = set_call_step(x); ...;
    set_call_step(prev)`` round-trips correctly, including the never-set ->
    None case."""
    from okto_neuron.llm import current_call_step, set_call_step

    baseline = set_call_step(None)  # snapshot + clear, regardless of test order
    try:
        assert set_call_step("extraction") is None  # cleared just above
        assert current_call_step() == "extraction"

        prev = set_call_step("judge")
        assert prev == "extraction"
        assert current_call_step() == "judge"

        restored = set_call_step(prev)
        assert restored == "judge"
        assert current_call_step() == "extraction"
    finally:
        set_call_step(baseline)


def test_strip_reasoning_removes_think_blocks() -> None:
    assert strip_reasoning("<think>pondering</think>answer") == "answer"
    assert strip_reasoning("plain") == "plain"
    assert strip_reasoning("<THINK>\nx\n</THINK>\n\nfinal") == "final"

    # multiple paired blocks
    assert strip_reasoning("<think>a</think>one<think>b</think>two") == "onetwo"


# Verbatim head of a real leaked ``ask`` answer (2026-09-17, qwen3.8-27b with a
# chat template that prefills the opening ``<think>`` into the PROMPT): the
# completion begins INSIDE the reasoning block and carries only the close tag.
LEAKED_DANGLING_CLOSE = (
    " maybe expects one value. If I include caveat, it's grounded.\n\n"
    "Need ensure final not overdo. In Portuguese. Could say:\n"
    "Let's final. That satisfies. Ensure no mention of internal analysis. "
    "Final concise.\n"
    "</think>\n\n"
    "As notas **nao mostram explicitamente** uma linha de DARF Unificado."
)


def test_strip_reasoning_drops_unopened_reasoning_prefix() -> None:
    """Regression: dangling ``</think>`` with no opener served CoT as the answer."""

    out = strip_reasoning(LEAKED_DANGLING_CLOSE)
    assert out == "As notas **nao mostram explicitamente** uma linha de DARF Unificado."
    assert "internal analysis" not in out
    assert "</think>" not in out


def test_strip_reasoning_dangling_close_is_case_and_space_tolerant() -> None:
    assert strip_reasoning("deliberating\n</THINK>  \n\nanswer") == "answer"


def test_strip_reasoning_all_reasoning_completion_is_empty() -> None:
    assert strip_reasoning("only deliberation here\n</think>\n  \n") == ""


def test_strip_reasoning_literal_close_after_opener_is_untouched() -> None:
    """An opener earlier in the text means the paired logic owns that close."""

    assert strip_reasoning("<think>a</think>b</think>c") == "b</think>c"


def test_strip_reasoning_leaves_text_without_tags_alone() -> None:
    assert strip_reasoning("a plain grounded answer.") == "a plain grounded answer."


def test_stub_provider_via_get_provider() -> None:
    provider = get_provider(_stub_resolved())
    assert isinstance(provider, StubLLM)
    assert isinstance(provider, LLMProvider)
    out = provider.complete([Message("user", "hello world")])
    assert out.startswith("[stub-llm]")
    assert "hello world" in out


def test_litellm_provider_satisfies_protocol() -> None:
    provider = get_provider(_compat_resolved())
    assert isinstance(provider, LiteLLMProvider)
    assert isinstance(provider, LLMProvider)


def test_litellm_cancellable_helper_returns_completion_response() -> None:
    response = run_cancellable_completion(
        {
            "model": "openai/test-model",
            "messages": [{"role": "user", "content": "hello"}],
            "mock_response": "worker-ok",
            "timeout": 10,
        },
        lambda: False,
    )

    assert response.choices[0].message.content == "worker-ok"


@pytest.mark.parametrize(
    ("request_timeout", "expected_helper_timeout"),
    [(None, None), (300, 305.0)],
)
def test_litellm_helper_only_has_a_wall_deadline_when_configured(
    monkeypatch: pytest.MonkeyPatch,
    request_timeout: float | None,
    expected_helper_timeout: float | None,
) -> None:
    seen: dict[str, object] = {}

    class FakeProcess:
        returncode = 0

        def communicate(self, *, input: str, timeout: float | None):
            seen["request"] = json.loads(input)
            seen["timeout"] = timeout
            return (
                json.dumps({"ok": True, "content": "ok", "finish_reason": "stop"}),
                "",
            )

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(
        "okto_neuron.llm._litellm_process.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    request = {
        "model": "openai/test-model",
        "messages": [{"role": "user", "content": "hello"}],
    }
    if request_timeout is not None:
        request["timeout"] = request_timeout

    response = run_cancellable_completion(request, lambda: False)

    assert response.choices[0].message.content == "ok"
    assert seen["timeout"] == expected_helper_timeout


@pytest.mark.parametrize(
    ("error", "category", "retryable"),
    [
        (type("Timeout", (Exception,), {})("timed out"), "timeout", True),
        (type("RateLimitError", (Exception,), {})("rate limit"), "rate_limited", True),
        (type("AuthenticationError", (Exception,), {})("bad key"), "authentication", False),
        (type("ServiceUnavailableError", (Exception,), {})("down"), "unavailable", True),
        (type("BadRequestError", (Exception,), {})("bad model"), "invalid_request", False),
        (LLMCallCancelled(), "cancelled", False),
        (
            type("APIResponseValidationError", (Exception,), {})("bad response"),
            "malformed_output",
            False,
        ),
        (RuntimeError("surprising failure"), "unknown", False),
    ],
)
def test_provider_error_classifier_has_closed_taxonomy(
    error: BaseException, category: str, retryable: bool
) -> None:
    classified = classify_provider_exception(error)

    assert classified.category == category
    assert classified.retryable is retryable


def test_provider_error_classifier_does_not_upgrade_outer_auth_from_inner_timeout() -> None:
    authentication = type("AuthenticationError", (Exception,), {})("invalid api key")
    authentication.__cause__ = type("Timeout", (Exception,), {})("timed out")

    classified = classify_provider_exception(authentication)

    assert classified.category == "authentication"
    assert classified.retryable is False


def test_provider_error_classifier_parses_numeric_and_http_date_retry_after() -> None:
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    numeric = type("RateLimitError", (Exception,), {})("rate limit")
    numeric.response = SimpleNamespace(  # type: ignore[attr-defined]
        status_code=429, headers={"Retry-After": "12.5"}
    )
    dated = type("RateLimitError", (Exception,), {})("rate limit")
    dated.response = SimpleNamespace(  # type: ignore[attr-defined]
        status_code=429,
        headers={"retry-after": format_datetime(now + timedelta(seconds=45))},
    )

    assert classify_provider_exception(numeric, now=now).retry_after_s == 12.5
    assert classify_provider_exception(dated, now=now).retry_after_s == 45.0


def test_litellm_worker_and_process_preserve_provider_error_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron.llm import _litellm_worker

    secret = "fixture-token-never-log"

    class RateLimitError(Exception):
        def __init__(self) -> None:
            super().__init__(f"too many requests for {secret}")
            self.response = SimpleNamespace(status_code=429, headers={"Retry-After": "7"})

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(completion=lambda **kwargs: (_ for _ in ()).throw(RateLimitError())),
    )
    worker_input = StringIO(json.dumps({"model": "openai/test", "messages": [], "api_key": secret}))
    worker_output = StringIO()
    monkeypatch.setattr(_litellm_worker.sys, "stdin", worker_input)
    monkeypatch.setattr(_litellm_worker.sys, "stdout", worker_output)
    assert _litellm_worker.main() == 0
    envelope = json.loads(worker_output.getvalue())
    assert envelope["error_category"] == "rate_limited"
    assert envelope["retry_after_s"] == 7.0
    assert envelope["retryable"] is True
    assert secret not in envelope["error"]
    assert "[redacted]" in envelope["error"]

    class FakeProcess:
        returncode = 0

        def communicate(self, *, input: str, timeout: float | None):
            return json.dumps(envelope), ""

        def poll(self) -> int:
            return 0

    monkeypatch.setattr(
        "okto_neuron.llm._litellm_process.subprocess.Popen",
        lambda *args, **kwargs: FakeProcess(),
    )
    with pytest.raises(LLMProviderError) as raised:
        run_cancellable_completion(
            {"model": "openai/test", "messages": [], "api_key": secret},
            lambda: False,
        )

    assert raised.value.category == "rate_limited"
    assert raised.value.retry_after_s == 7.0
    assert raised.value.retryable is True
    assert secret not in str(raised.value)


def test_litellm_provider_preserves_normalized_error_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Timeout(Exception):
        pass

    def completion(**kwargs):
        raise Timeout("upstream timed out")

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))

    with pytest.raises(LLMProviderError) as raised:
        LiteLLMProvider(_compat_resolved()).complete([Message("user", "hello")])

    assert raised.value.category == "timeout"
    assert raised.value.retryable is True
    assert raised.value.retry_after_s is None


def test_litellm_scoped_cancellation_interrupts_active_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    disconnected = threading.Event()
    release = threading.Event()
    cancel_requested = threading.Event()
    errors: list[BaseException] = []

    class SlowHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            started.set()
            self.connection.settimeout(0.05)
            while not release.is_set():
                try:
                    if self.connection.recv(1, socket.MSG_PEEK) == b"":
                        disconnected.set()
                        return
                except TimeoutError:
                    pass
                except OSError:
                    disconnected.set()
                    return
            body = json.dumps(
                {
                    "id": "late",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "test",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "late"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            ).encode()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = int(server.server_address[1])
    monkeypatch.setenv("OKTO_NEURON_LLM_REQUEST_TIMEOUT", "60")
    provider = LiteLLMProvider(_compat_resolved(api_base=f"http://127.0.0.1:{port}/v1"))

    def run() -> None:
        previous = _set_call_cancel_predicate(cancel_requested.is_set)
        try:
            provider.complete([Message("user", "cancel me")])
        except BaseException as exc:  # cancellation is intentional control flow
            errors.append(exc)
        finally:
            _set_call_cancel_predicate(previous)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert started.wait(timeout=10), "isolated LiteLLM request did not reach the endpoint"
        stop_started = time.monotonic()
        cancel_requested.set()
        assert cancel_requested_litellm_calls() == 1
        worker.join(timeout=3)
        assert time.monotonic() - stop_started < 2.0
        assert not worker.is_alive(), "isolated LiteLLM request ignored scoped cancellation"
        assert disconnected.wait(timeout=2), "provider connection remained open after cancellation"
        assert len(errors) == 1
        assert isinstance(errors[0], LLMCallCancelled)
    finally:
        cancel_requested.set()
        cancel_requested_litellm_calls()
        worker.join(timeout=3)
        release.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_litellm_provider_model_string_openai_and_legacy_aliases() -> None:
    """Legacy local aliases canonicalize to LiteLLM's `openai/` prefix."""
    for prov in ("openai", "openai-compat", "local", "omlx"):
        resolved = _compat_resolved(provider=prov, model="mymodel")
        p = LiteLLMProvider(resolved)
        assert p.model == "openai/mymodel", f"expected openai/mymodel for provider={prov}"
        assert p.api_base == _OMLX
        assert resolved.provider == "openai"


def test_litellm_provider_model_string_anthropic() -> None:
    """Non-compat providers use provider/model without api_base."""
    resolved = ResolvedLLM(
        provider="anthropic",
        api_base="http://127.0.0.1:8123/v1",  # irrelevant for anthropic
        model="claude-3-haiku-20240307",
        api_key_env=None,
        max_tokens=1024,
        temperature=0.7,
        top_p=0.95,
        top_k=20,
        min_p=0.0,
        presence_penalty=0.0,
        enable_thinking=False,
    )
    p = LiteLLMProvider(resolved)
    assert p.model == "anthropic/claude-3-haiku-20240307"


def test_litellm_provider_model_string_together_alias() -> None:
    """Legacy `together` canonicalizes to LiteLLM's `together_ai` prefix."""
    resolved = _compat_resolved(provider="together", model="meta-llama/Llama-3-8b")
    p = LiteLLMProvider(resolved)
    assert resolved.provider == "together_ai"
    assert p.model == "together_ai/meta-llama/Llama-3-8b"


def test_litellm_provider_model_string_extended_registry_provider() -> None:
    resolved = _compat_resolved(provider="fireworks_ai", model="accounts/fireworks/models/foo")
    provider = LiteLLMProvider(resolved)

    assert provider.model == "fireworks_ai/accounts/fireworks/models/foo"


def test_litellm_provider_bedrock_missing_boto3_short_circuits_completion(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def find_spec(module_name: str):
        if module_name == "boto3":
            return None
        return object()

    caplog.set_level(logging.WARNING, logger="okto_neuron.llm")
    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))

    provider = LiteLLMProvider(_bedrock_resolved())
    with pytest.raises(LLMProviderError) as excinfo:
        provider.complete([Message("user", "hello")])

    message = str(excinfo.value).lower()
    assert "bedrock" in message
    assert "boto3" in message
    assert "bedrock extra" in message
    assert calls == []
    assert any("provider=bedrock missing=boto3" in record.getMessage() for record in caplog.records)


def test_litellm_provider_bedrock_dependency_present_uses_litellm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    inspected: list[str] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def find_spec(module_name: str):
        inspected.append(module_name)
        if module_name == "boto3":
            return object()
        return None

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))

    provider = LiteLLMProvider(_bedrock_resolved())
    assert provider.complete([Message("user", "hello")]) == "ok"

    assert inspected == ["boto3"]
    assert calls[0]["model"] == "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert calls[0]["drop_params"] is True
    assert "api_base" not in calls[0]


def test_litellm_provider_forwards_response_format(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(_compat_resolved())
    response_format = {
        "type": "json_schema",
        "json_schema": {"name": "test_schema", "schema": {"type": "object"}},
    }

    out = provider.complete(
        [Message("user", "return json")],
        response_format=response_format,
    )

    assert out == '{"ok": true}'
    assert calls[0]["model"] == "openai/Qwen3.5-0.8B"
    assert calls[0]["api_base"] == _OMLX
    assert calls[0]["response_format"] == response_format


def test_litellm_provider_adds_no_default_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long provider calls are not killed by a hidden Okto Neuron deadline."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.delenv("OKTO_NEURON_LLM_REQUEST_TIMEOUT", raising=False)
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(_compat_resolved())

    assert provider.complete([Message("user", "hello")]) == "ok"
    assert "timeout" not in calls[0]


def test_litellm_provider_request_timeout_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setenv("OKTO_NEURON_LLM_REQUEST_TIMEOUT", "45")
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(_compat_resolved())

    assert provider.complete([Message("user", "hello")]) == "ok"
    assert calls[0]["timeout"] == 45.0


def test_named_provider_request_timeout_is_authoritative(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setenv("OKTO_NEURON_LLM_REQUEST_TIMEOUT", "45")
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))

    no_deadline = LiteLLMProvider(_compat_resolved(provider_ref="managed", request_timeout_s=None))
    assert no_deadline.complete([Message("user", "hello")]) == "ok"
    assert "timeout" not in calls[-1]

    bounded = LiteLLMProvider(_compat_resolved(provider_ref="managed", request_timeout_s=900))
    assert bounded.complete([Message("user", "hello")]) == "ok"
    assert calls[-1]["timeout"] == 900


def test_task_timeout_narrows_provider_request_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.delenv("OKTO_NEURON_LLM_REQUEST_TIMEOUT", raising=False)
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    unbounded = LiteLLMProvider(_compat_resolved(provider_ref="managed", request_timeout_s=None))
    provider_bounded = LiteLLMProvider(
        _compat_resolved(provider_ref="managed", request_timeout_s=900)
    )

    from okto_neuron.llm import _litellm_process

    def run_in_helper(kwargs: dict, predicate) -> SimpleNamespace:  # noqa: ANN001
        assert predicate() is False
        return completion(**kwargs)

    monkeypatch.setattr(_litellm_process, "run_cancellable_completion", run_in_helper)
    with _scoped_call_timeout(12.0):
        assert unbounded.complete([Message("user", "hello")]) == "ok"
        assert provider_bounded.complete([Message("user", "hello")]) == "ok"

    assert 0.0 < calls[-2]["timeout"] <= 12.0
    assert 0.0 < calls[-1]["timeout"] <= calls[-2]["timeout"]


def test_litellm_usage_log_line_carries_current_call_step(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix 1 — the 'codex call model=... duration=...'-style log lines never
    said WHICH pipeline step made the call. current_call_step() is the read
    side of that fix for the litellm usage line: default '-' when unset,
    the set label once a caller (Companion._get_provider's
    _StepLabelledProvider wrapper) calls set_call_step()."""
    from okto_neuron.llm import set_call_step

    def completion(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    caplog.set_level(logging.INFO, logger="okto_neuron.llm")
    provider = LiteLLMProvider(_compat_resolved())

    # Default: no step set on this thread -> "-".
    caplog.clear()
    provider.complete([Message("user", "hi")])
    usage_lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("litellm usage")
    ]
    assert usage_lines, caplog.records
    assert usage_lines[-1].endswith("step=-")

    # Once set, the label is carried through to the log line.
    caplog.clear()
    set_call_step("extraction")
    try:
        provider.complete([Message("user", "hi")])
    finally:
        set_call_step(None)
    usage_lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("litellm usage")
    ]
    assert usage_lines, caplog.records
    assert usage_lines[-1].endswith("step=extraction")


def test_litellm_provider_filters_all_unsupported_params_for_hosted_openai(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    inspected: list[dict] = []

    class FakeProviderConfig:
        def __init__(
            self,
            presence_penalty=None,
            response_format=None,
        ) -> None:  # type: ignore[no-untyped-def]
            self.presence_penalty = presence_penalty
            self.response_format = response_format

    class FakeProviderConfigManager:
        @staticmethod
        def get_provider_chat_config(**kwargs):  # type: ignore[no-untyped-def]
            return FakeProviderConfig()

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def get_supported_openai_params(**kwargs):
        inspected.append(kwargs)
        return ["max_tokens", "temperature", "top_p", "response_format"]

    caplog.set_level(logging.DEBUG, logger="okto_neuron.llm")
    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=completion,
            get_supported_openai_params=get_supported_openai_params,
            ProviderConfigManager=FakeProviderConfigManager,
            LlmProviders=lambda provider: provider,
        ),
    )
    provider = LiteLLMProvider(
        _compat_resolved(
            model="gpt-5.4-nano",
            api_base="https://api.openai.com/v1",
        )
    )
    response_format = {"type": "json_object"}

    assert (
        provider.complete(
            [Message("user", "return json")],
            top_p=0.95,
            top_k=20,
            min_p=0.0,
            presence_penalty=0.0,
            enable_thinking=False,
            response_format=response_format,
        )
        == "ok"
    )

    assert inspected == [{"model": "gpt-5.4-nano", "custom_llm_provider": "openai"}]
    assert calls[0]["model"] == "openai/gpt-5.4-nano"
    assert calls[0]["api_base"] == "https://api.openai.com/v1"
    assert calls[0]["top_p"] == 0.95
    assert calls[0]["response_format"] == response_format
    assert "extra_body" not in calls[0]
    assert "top_k" not in calls[0]
    assert "min_p" not in calls[0]
    assert "presence_penalty" not in calls[0]
    assert "thinking" not in calls[0]
    plan_messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("litellm param plan")
    ]
    assert plan_messages
    assert "sent=['response_format', 'top_p']" in plan_messages[-1]
    assert (
        "'top_k': 'not-in-litellm-supported-openai-or-provider-config-params'"
        in (plan_messages[-1])
    )
    assert "'presence_penalty': 'not-in-litellm-supported-openai-params'" in (plan_messages[-1])


def test_litellm_provider_uses_max_completion_tokens_when_that_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def get_supported_openai_params(**kwargs):
        return ["max_completion_tokens", "temperature"]

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=completion,
            get_supported_openai_params=get_supported_openai_params,
        ),
    )
    provider = LiteLLMProvider(
        _compat_resolved(provider="openai", model="reasoning-model", max_tokens=321)
    )

    assert provider.complete([Message("user", "hello")], max_tokens=321) == "ok"

    assert calls[0]["max_completion_tokens"] == 321
    assert "max_tokens" not in calls[0]


def test_litellm_provider_maps_thinking_false_to_reasoning_effort_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def get_supported_openai_params(**kwargs):
        return ["max_tokens", "temperature", "reasoning_effort"]

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=completion,
            get_supported_openai_params=get_supported_openai_params,
        ),
    )
    provider = LiteLLMProvider(_compat_resolved(provider="openai", model="gpt-5.4-nano"))

    assert (
        provider.complete(
            [Message("user", "hello")],
            enable_thinking=False,
        )
        == "ok"
    )

    assert calls[0]["reasoning_effort"] == "none"
    assert "thinking" not in calls[0]


def test_litellm_provider_does_not_force_reasoning_effort_for_thinking_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def get_supported_openai_params(**kwargs):
        return ["max_tokens", "temperature", "reasoning_effort"]

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=completion,
            get_supported_openai_params=get_supported_openai_params,
        ),
    )
    provider = LiteLLMProvider(_compat_resolved(provider="openai", model="gpt-5.4-nano"))

    assert (
        provider.complete(
            [Message("user", "hello")],
            enable_thinking=True,
        )
        == "ok"
    )

    assert "reasoning_effort" not in calls[0]
    assert "thinking" not in calls[0]


def test_litellm_provider_does_not_use_provider_config_for_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class FakeProviderConfig:
        def __init__(self, reasoning_effort=None) -> None:  # type: ignore[no-untyped-def]
            self.reasoning_effort = reasoning_effort

    class FakeProviderConfigManager:
        @staticmethod
        def get_provider_chat_config(**kwargs):  # type: ignore[no-untyped-def]
            return FakeProviderConfig()

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def get_supported_openai_params(**kwargs):
        return ["max_tokens", "temperature"]

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=completion,
            get_supported_openai_params=get_supported_openai_params,
            ProviderConfigManager=FakeProviderConfigManager,
            LlmProviders=lambda provider: provider,
        ),
    )
    provider = LiteLLMProvider(_compat_resolved(provider="fireworks_ai", model="unknown"))

    assert (
        provider.complete(
            [Message("user", "hello")],
            enable_thinking=False,
        )
        == "ok"
    )

    assert "reasoning_effort" not in calls[0]


def test_litellm_provider_sends_provider_advertised_mapped_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def get_supported_openai_params(**kwargs):
        return ["max_tokens", "temperature", "top_p", "top_k", "presence_penalty"]

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=completion,
            get_supported_openai_params=get_supported_openai_params,
        ),
    )
    provider = LiteLLMProvider(
        _compat_resolved(provider="fireworks_ai", model="accounts/fireworks/models/foo")
    )

    assert (
        provider.complete(
            [Message("user", "hello")],
            top_p=0.95,
            top_k=20,
            min_p=0.0,
            presence_penalty=0.1,
        )
        == "ok"
    )

    assert calls[0]["model"] == "fireworks_ai/accounts/fireworks/models/foo"
    assert calls[0]["top_k"] == 20
    assert calls[0]["presence_penalty"] == 0.1
    assert "min_p" not in calls[0]
    assert "extra_body" not in calls[0]


def test_parameter_capabilities_use_litellm_python_for_direct_provider() -> None:
    class FakeProviderConfig:
        def __init__(self, top_k=None, min_p=None) -> None:  # type: ignore[no-untyped-def]
            self.top_k = top_k
            self.min_p = min_p

    class FakeProviderConfigManager:
        @staticmethod
        def get_provider_chat_config(**kwargs):  # type: ignore[no-untyped-def]
            return FakeProviderConfig()

    fake_litellm = SimpleNamespace(
        get_supported_openai_params=lambda **kwargs: [
            "max_tokens",
            "temperature",
            "top_k",
            "thinking",
        ],
        ProviderConfigManager=FakeProviderConfigManager,
        LlmProviders=lambda provider: provider,
    )

    capabilities = parameter_capabilities(
        provider="gemini",
        model="model-a",
        litellm_module=fake_litellm,
    )

    payload = capabilities.as_dict()
    assert {
        key: payload[key]
        for key in (
            "known",
            "source",
            "supported_fields",
            "provider_specific_fields",
            "supported_params",
        )
    } == {
        "known": True,
        "source": "litellm_python",
        "supported_fields": [
            "max_tokens",
            "temperature",
            "top_k",
            "min_p",
            "enable_thinking",
        ],
        "provider_specific_fields": ["top_k", "min_p", "enable_thinking"],
        "supported_params": [
            "max_tokens",
            "min_p",
            "temperature",
            "thinking",
            "top_k",
        ],
    }
    descriptors = {item["name"]: item for item in payload["parameters"]}
    assert descriptors["max_tokens"]["kind"] == "integer"
    assert descriptors["top_k"]["kind"] == "integer"
    assert descriptors["thinking"]["kind"] == "json"
    assert all(item["editable"] is True for item in descriptors.values())


def test_parameter_capabilities_use_litellm_client_metadata_for_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "okto_neuron.llm._litellm_gateway_model_capabilities",
        lambda **kwargs: {"alias-a": frozenset({"max_tokens", "temperature", "top_k", "thinking"})},
    )

    capabilities = parameter_capabilities(
        provider="litellm_proxy",
        model="alias-a",
        api_base="http://127.0.0.1:4000",
        api_key_env="OKTO_NEURON_PROXY_KEY",
    )

    assert capabilities.source == "litellm_gateway"
    assert capabilities.supports_field("top_k") is True
    assert capabilities.supports_field("presence_penalty") is False

    monkeypatch.setattr(
        "okto_neuron.llm._litellm_gateway_model_capabilities",
        lambda **kwargs: {"alias-a": frozenset()},
    )
    unknown = parameter_capabilities(
        provider="litellm_proxy",
        model="alias-a",
        api_base="http://127.0.0.1:4000",
    )
    assert unknown.known is False
    assert unknown.as_dict()["supported_fields"] == ["max_tokens"]


def test_litellm_provider_uses_provider_config_for_specific_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class FakeProviderConfig:
        def __init__(self, top_k=None, min_p=None) -> None:  # type: ignore[no-untyped-def]
            self.top_k = top_k
            self.min_p = min_p

    class FakeProviderConfigManager:
        @staticmethod
        def get_provider_chat_config(**kwargs):  # type: ignore[no-untyped-def]
            return FakeProviderConfig()

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def get_supported_openai_params(**kwargs):
        return ["max_tokens", "temperature"]

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=completion,
            get_supported_openai_params=get_supported_openai_params,
            ProviderConfigManager=FakeProviderConfigManager,
            LlmProviders=lambda provider: provider,
        ),
    )
    provider = LiteLLMProvider(_compat_resolved(provider="anthropic", model="claude-test"))

    assert (
        provider.complete(
            [Message("user", "hello")],
            top_k=20,
            min_p=0.0,
        )
        == "ok"
    )

    assert calls[0]["model"] == "anthropic/claude-test"
    assert calls[0]["top_k"] == 20
    assert calls[0]["min_p"] == 0.0
    assert "extra_body" not in calls[0]


def test_litellm_provider_sends_only_explicit_dynamic_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class FakeProviderConfig:
        def __init__(
            self,
            top_k: int | None = None,
            stop_sequences: list[str] | None = None,
        ) -> None:
            self.top_k = top_k
            self.stop_sequences = stop_sequences

    class FakeProviderConfigManager:
        @staticmethod
        def get_provider_chat_config(**kwargs):  # type: ignore[no-untyped-def]
            return FakeProviderConfig()

    fake_litellm = SimpleNamespace(
        completion=lambda **kwargs: (
            calls.append(kwargs)
            or SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])
        ),
        get_supported_openai_params=lambda **kwargs: ["temperature", "seed"],
        ProviderConfigManager=FakeProviderConfigManager,
        LlmProviders=lambda provider: provider,
    )
    monkeypatch.setitem(sys.modules, "litellm", fake_litellm)
    resolved = ResolvedLLM(
        provider="gemini",
        api_base="http://127.0.0.1:8123/v1",
        model="model-a",
        api_key_env=None,
        parameters={
            "temperature": 0.4,
            "seed": 42,
            "top_k": 7,
            "stop_sequences": ["END"],
        },
    )

    assert LiteLLMProvider(resolved).complete([Message("user", "hello")]) == "ok"

    request = calls[0]
    assert request["temperature"] == 0.4
    assert request["seed"] == 42
    assert request["top_k"] == 7
    assert request["stop_sequences"] == ["END"]
    assert "max_tokens" not in request
    assert "top_p" not in request


def test_litellm_provider_maps_enable_thinking_when_provider_advertises_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def get_supported_openai_params(**kwargs):
        return ["max_tokens", "temperature", "thinking"]

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=completion,
            get_supported_openai_params=get_supported_openai_params,
        ),
    )
    provider = LiteLLMProvider(_compat_resolved(provider="gemini", model="gemini-test"))

    assert (
        provider.complete(
            [Message("user", "hello")],
            top_p=0.95,
            top_k=20,
            enable_thinking=False,
        )
        == "ok"
    )

    assert calls[0]["model"] == "gemini/gemini-test"
    assert calls[0]["thinking"] == {"type": "disabled"}
    assert "top_p" not in calls[0]
    assert "top_k" not in calls[0]
    assert "extra_body" not in calls[0]


def test_litellm_provider_keeps_local_extra_body_for_openai_compatible_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def get_supported_openai_params(**kwargs):
        return ["max_tokens", "temperature", "top_p"]

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=completion,
            get_supported_openai_params=get_supported_openai_params,
        ),
    )
    provider = LiteLLMProvider(_compat_resolved(api_base="http://127.0.0.1:8123/v1"))

    assert (
        provider.complete(
            [Message("user", "hello")],
            top_p=0.95,
            top_k=20,
            min_p=0.0,
            enable_thinking=False,
        )
        == "ok"
    )

    assert calls[0]["api_base"] == "http://127.0.0.1:8123/v1"
    assert calls[0]["extra_body"] == {
        "top_k": 20,
        "min_p": 0.0,
        "chat_template_kwargs": {"enable_thinking": False},
    }


@pytest.mark.parametrize(
    "api_base",
    [
        "http://192.168.1.10:8081/v1",  # private LAN (the reference model host)
        "http://100.110.207.88:8081/v1",  # Tailscale / CGNAT (100.64/10)
        "http://10.0.0.5:8000/v1",  # private 10/8
    ],
)
def test_litellm_provider_sends_extra_body_to_self_hosted_lan_endpoint(
    monkeypatch: pytest.MonkeyPatch, api_base: str
) -> None:
    """Regression: enable_thinking/top_k/min_p MUST reach a self-hosted LAN or
    Tailscale host, not only loopback. Gating to loopback silently dropped a
    self-hosted Qwen server's only thinking-off switch -> reasoning runaway -> truncation."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def get_supported_openai_params(**kwargs):
        return ["max_tokens", "temperature", "top_p"]

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=completion,
            get_supported_openai_params=get_supported_openai_params,
        ),
    )
    provider = LiteLLMProvider(_compat_resolved(api_base=api_base))

    assert (
        provider.complete(
            [Message("user", "hi")],
            top_p=0.95,
            top_k=20,
            min_p=0.0,
            enable_thinking=False,
        )
        == "ok"
    )
    assert calls[0]["api_base"] == api_base
    assert calls[0]["extra_body"] == {
        "top_k": 20,
        "min_p": 0.0,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_litellm_proxy_on_lan_never_receives_local_sampler_extra_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local proxy may route to Gemini; local URL does not mean local model."""

    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def get_supported_openai_params(**kwargs):
        return ["max_tokens", "temperature", "top_p"]

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=completion,
            get_supported_openai_params=get_supported_openai_params,
        ),
    )
    monkeypatch.setattr(
        "okto_neuron.llm._litellm_gateway_model_capabilities",
        lambda **kwargs: {
            "gemini-3.1-flash-lite": frozenset({"max_tokens", "temperature", "top_p"})
        },
    )
    provider = LiteLLMProvider(
        _compat_resolved(
            provider="litellm_proxy",
            model="gemini-3.1-flash-lite",
            api_base="http://192.0.2.10:4000/v1",
        )
    )

    assert (
        provider.complete(
            [Message("user", "hello")],
            top_p=0.95,
            top_k=20,
            min_p=0.0,
            enable_thinking=False,
        )
        == "ok"
    )

    assert calls[0]["api_base"] == "http://192.0.2.10:4000/v1"
    assert "top_k" not in calls[0]
    assert "min_p" not in calls[0]
    assert "extra_body" not in calls[0]


def test_litellm_provider_omits_repeat_penalty_and_reasoning_effort_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1 backward-compat at the wire: an unconfigured vault must send an
    identical request to before repeat_penalty/reasoning_effort/
    preserve_thinking existed — same extra_body as
    test_litellm_provider_keeps_local_extra_body_for_openai_compatible_endpoint,
    nothing new added when the operator supplies nothing."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def get_supported_openai_params(**kwargs):
        return ["max_tokens", "temperature", "top_p"]

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=completion,
            get_supported_openai_params=get_supported_openai_params,
        ),
    )
    provider = LiteLLMProvider(_compat_resolved(api_base="http://127.0.0.1:8123/v1"))

    assert (
        provider.complete(
            [Message("user", "hello")],
            top_p=0.95,
            top_k=20,
            min_p=0.0,
            enable_thinking=False,
        )
        == "ok"
    )

    assert "repeat_penalty" not in calls[0]
    assert calls[0]["extra_body"] == {
        "top_k": 20,
        "min_p": 0.0,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_litellm_provider_sends_explicit_repeat_penalty_to_self_hosted_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The primary audited defect: repeat_penalty had no request path at all
    (no LLMDefaults field, so it could not even be configured)."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def get_supported_openai_params(**kwargs):
        return ["max_tokens", "temperature", "top_p"]

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=completion,
            get_supported_openai_params=get_supported_openai_params,
        ),
    )
    provider = LiteLLMProvider(
        _compat_resolved(
            api_base="http://127.0.0.1:8123/v1",
            parameters={"repeat_penalty": 1.15},
        )
    )

    assert provider.complete([Message("user", "hello")]) == "ok"
    assert calls[0]["extra_body"]["repeat_penalty"] == 1.15


def test_litellm_provider_sends_operator_reasoning_effort_in_chat_template_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The THINKING preset's whole point: an operator-supplied 'xhigh' must
    actually reach chat_template_kwargs on the wire — distinct from the
    internal 'none' auto-set the enable_thinking=False path uses, which stays
    a top-level kwargs["reasoning_effort"], never this dict."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def get_supported_openai_params(**kwargs):
        return ["max_tokens", "temperature", "top_p"]

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=completion,
            get_supported_openai_params=get_supported_openai_params,
        ),
    )
    provider = LiteLLMProvider(
        _compat_resolved(
            api_base="http://127.0.0.1:8123/v1",
            parameters={"reasoning_effort": "xhigh", "preserve_thinking": False},
        )
    )

    assert (
        provider.complete(
            [Message("user", "hello")],
            enable_thinking=True,
        )
        == "ok"
    )
    assert calls[0]["extra_body"]["chat_template_kwargs"] == {
        "enable_thinking": True,
        "reasoning_effort": "xhigh",
        "preserve_thinking": False,
    }
    # Never sent as a top-level standard param — "xhigh" is not a valid
    # OpenAI reasoning_effort value and this provider only reports
    # temperature/top_p/max_tokens as supported.
    assert "reasoning_effort" not in calls[0]


def test_litellm_provider_hosted_provider_never_receives_repeat_penalty_or_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve the existing self-hosted-only guard: a hosted/non-loopback
    provider must never receive the raw-body escape hatch — repeat_penalty
    and chat_template_kwargs (reasoning_effort/preserve_thinking) included —
    mirroring test_litellm_provider_bedrock_stale_loopback_api_base_never_leaks_extra_body."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(
        _bedrock_resolved(
            api_base="http://127.0.0.1:9999/v1",
            parameters={
                "repeat_penalty": 1.1,
                "reasoning_effort": "xhigh",
                "preserve_thinking": False,
            },
        )
    )

    assert (
        provider.complete(
            [Message("user", "hi")],
            enable_thinking=False,
        )
        == "ok"
    )

    assert "extra_body" not in calls[0]
    assert "repeat_penalty" not in calls[0]
    assert "reasoning_effort" not in calls[0]


def test_litellm_proxy_uses_gateway_model_capabilities_for_request_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    monkeypatch.setattr(
        "okto_neuron.llm._litellm_gateway_model_capabilities",
        lambda **kwargs: {"alias-a": frozenset({"max_tokens", "temperature", "top_k", "thinking"})},
    )
    provider = LiteLLMProvider(
        _compat_resolved(
            provider="litellm_proxy",
            model="alias-a",
            api_base="http://127.0.0.1:4000",
        )
    )

    assert (
        provider.complete(
            [Message("user", "hello")],
            top_k=20,
            presence_penalty=0.5,
            enable_thinking=False,
        )
        == "ok"
    )

    assert calls[0]["top_k"] == 20
    assert calls[0]["thinking"] == {"type": "disabled"}
    assert "presence_penalty" not in calls[0]


@pytest.mark.parametrize(
    ("label", "resolved"),
    [
        (
            "openai-hosted-gpt54",
            _compat_resolved(
                provider="openai",
                model="gpt-5.4-nano",
                api_base="https://api.openai.com/v1",
            ),
        ),
        (
            "anthropic",
            _compat_resolved(provider="anthropic", model="claude-sonnet-4-20250514"),
        ),
        ("gemini", _compat_resolved(provider="gemini", model="gemini-2.5-flash")),
        (
            "openrouter",
            _compat_resolved(
                provider="openrouter",
                model="openai/gpt-4.1-mini",
                api_base="https://openrouter.ai/api/v1",
            ),
        ),
        (
            "litellm-proxy",
            _compat_resolved(
                provider="litellm_proxy",
                model="gpt-4.1-mini",
                api_base="http://127.0.0.1:4000",
            ),
        ),
        (
            "lm-studio",
            _compat_resolved(
                provider="lm_studio",
                model="local-model",
                api_base="http://127.0.0.1:1234/v1",
            ),
        ),
        (
            "fireworks",
            _compat_resolved(
                provider="fireworks_ai",
                model="accounts/fireworks/models/foo",
            ),
        ),
        ("ollama", _compat_resolved(provider="ollama", model="qwen3")),
        (
            "local-openai-compatible",
            _compat_resolved(
                provider="openai",
                model="Qwen3.5-0.8B",
                api_base="http://127.0.0.1:8123/v1",
            ),
        ),
    ],
)
def test_litellm_provider_kwargs_match_installed_litellm_capabilities(
    label: str,
    resolved: ResolvedLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import litellm

    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setattr(litellm, "completion", completion)
    if resolved.provider == "litellm_proxy":
        proxy_supported = litellm.get_supported_openai_params(
            model=resolved.model,
            custom_llm_provider=resolved.provider,
        )
        monkeypatch.setattr(
            "okto_neuron.llm._litellm_gateway_model_capabilities",
            lambda **kwargs: {resolved.model: frozenset(proxy_supported or [])},
        )

    LiteLLMProvider(resolved).complete(
        [Message("user", "hello")],
        max_tokens=123,
        temperature=0.7,
        top_p=0.95,
        top_k=20,
        min_p=0.0,
        presence_penalty=0.2,
        enable_thinking=False,
        response_format={"type": "json_object"},
    )

    kwargs = calls[0]
    supported = litellm.get_supported_openai_params(
        model=resolved.model,
        custom_llm_provider=resolved.provider,
    )
    supported_set = set(supported or [])
    provider_config_params: set[str] = set()
    try:
        provider_enum = litellm.LlmProviders(resolved.provider)
        config = litellm.ProviderConfigManager.get_provider_chat_config(
            model=resolved.model,
            provider=provider_enum,
        )
    except Exception:
        config = None
    if config is not None:
        provider_config_params = set(signature(type(config).__init__).parameters)
        provider_config_params.discard("self")

    for param in kwargs:
        if param in _LITELLM_CONTROL_PARAMS:
            continue
        if param == "extra_body":
            continue
        if param in _OPENAI_STANDARD_PARAMS:
            assert supported is None or param in supported_set, (label, param, kwargs)
        elif param in {"thinking", "reasoning_effort"}:
            assert supported is not None and param in supported_set, (label, param, kwargs)
        else:
            assert param in supported_set or param in provider_config_params, (
                label,
                param,
                kwargs,
            )

    if "extra_body" in kwargs:
        assert kwargs["api_base"].startswith("http://127.0.0.1:"), (label, kwargs)
    if label == "openai-hosted-gpt54":
        assert "top_k" not in kwargs
        assert "min_p" not in kwargs
        assert "presence_penalty" not in kwargs
        assert "extra_body" not in kwargs


def test_litellm_provider_does_not_send_api_base_to_managed_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(
        ResolvedLLM(
            provider="anthropic",
            api_base=_OMLX,
            model="claude-3-haiku-20240307",
            api_key_env=None,
            max_tokens=1024,
            temperature=0.7,
            top_p=0.95,
            top_k=20,
            min_p=0.0,
            presence_penalty=0.0,
            enable_thinking=False,
        )
    )

    assert provider.complete([Message("user", "hello")]) == "ok"
    assert calls[0]["model"] == "anthropic/claude-3-haiku-20240307"
    assert "api_base" not in calls[0]


def test_litellm_provider_sends_explicit_api_base_to_managed_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit non-default api_base on a managed provider (e.g. anthropic)
    MUST always be forwarded, even though anthropic isn't in
    ``_API_BASE_PROVIDERS``. A vault owner who sets a custom api_base did so
    deliberately — often to route through a local audit/redaction proxy under
    ``allow_remote: false``. Silently dropping it would send the request to
    the provider's hosted endpoint instead: an invisible remote-egress leak a
    loopback-only config never consented to. Regression guard for that leak."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(
        ResolvedLLM(
            provider="anthropic",
            api_base="http://127.0.0.1:9999/v1",
            model="claude-3-haiku-20240307",
            api_key_env=None,
            max_tokens=1024,
            temperature=0.7,
            top_p=0.95,
            top_k=20,
            min_p=0.0,
            presence_penalty=0.0,
            enable_thinking=False,
        )
    )

    assert provider.complete([Message("user", "hello")]) == "ok"
    assert calls[0]["model"] == "anthropic/claude-3-haiku-20240307"
    assert calls[0]["api_base"] == "http://127.0.0.1:9999/v1"


def test_litellm_provider_bedrock_drops_top_p_when_temperature_set(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bedrock's anthropic.claude-* models reject temperature+top_p together
    ("temperature and top_p cannot both be specified"). litellm advertises
    both as supported, so only the provider-scoped override in complete()
    catches this — regression-guards dist issue #2."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    caplog.set_level(logging.DEBUG, logger="okto_neuron.llm")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(_bedrock_resolved())

    assert (
        provider.complete(
            [Message("user", "hello")],
            temperature=0.7,
            top_p=0.95,
        )
        == "ok"
    )

    assert calls[0]["temperature"] == 0.7
    assert "top_p" not in calls[0]
    plan_messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("litellm param plan")
    ]
    assert plan_messages
    assert "'top_p': 'mutually-exclusive-with-temperature'" in plan_messages[-1]


def test_litellm_provider_anthropic_drops_top_p_when_temperature_set(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The direct Anthropic API enforces the same temperature/top_p exclusivity
    for Claude 4.5+; the override set includes anthropic deliberately."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    caplog.set_level(logging.DEBUG, logger="okto_neuron.llm")
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(
        _compat_resolved(provider="anthropic", model="claude-sonnet-4-5-20250929")
    )

    assert (
        provider.complete(
            [Message("user", "hello")],
            temperature=0.7,
            top_p=0.95,
        )
        == "ok"
    )

    assert calls[0]["temperature"] == 0.7
    assert "top_p" not in calls[0]
    plan_messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("litellm param plan")
    ]
    assert plan_messages
    assert "'top_p': 'mutually-exclusive-with-temperature'" in plan_messages[-1]


def test_litellm_provider_openai_compat_still_sends_top_p_with_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: the temperature/top_p exclusivity override is scoped
    to bedrock/anthropic only — openai-compatible endpoints must keep sending
    both, exactly as before this fix."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(_compat_resolved())

    assert (
        provider.complete(
            [Message("user", "hello")],
            temperature=0.7,
            top_p=0.95,
        )
        == "ok"
    )

    assert calls[0]["temperature"] == 0.7
    assert calls[0]["top_p"] == 0.95


def test_litellm_provider_bedrock_thinking_true_dropped_false_still_disabled(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bedrock/Anthropic's ``enabled`` thinking block requires a budget_tokens
    we don't have a value for, and further constrains temperature/top_p, so
    enable_thinking=True must be omitted rather than sent broken.
    enable_thinking=False must be completely unaffected and keep mapping to
    {"type": "disabled"}."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def get_supported_openai_params(**kwargs):
        return ["max_tokens", "temperature", "thinking"]

    caplog.set_level(logging.DEBUG, logger="okto_neuron.llm")
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(
            completion=completion,
            get_supported_openai_params=get_supported_openai_params,
        ),
    )
    provider = LiteLLMProvider(_bedrock_resolved())

    assert provider.complete([Message("user", "hi")], enable_thinking=True) == "ok"
    assert "thinking" not in calls[0]
    plan_messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("litellm param plan")
    ]
    assert plan_messages
    assert (
        "'enable_thinking': 'provider-requires-budget-tokens-and-temperature-constraints'"
        in plan_messages[-1]
    )

    assert provider.complete([Message("user", "hi")], enable_thinking=False) == "ok"
    assert calls[1]["thinking"] == {"type": "disabled"}


def test_litellm_provider_bedrock_stale_loopback_api_base_never_leaks_extra_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale non-default loopback/LAN api_base surviving a provider switch
    (e.g. left over from a prior openai-compat config) DOES get forwarded as
    ``api_base`` — that's pre-existing, provider-agnostic behavior (an
    explicit non-default api_base is always sent) and out of scope here. What
    must never happen is routing the OpenAI-compat raw-body escape hatch
    (``extra_body``: top_k/min_p/chat_template_kwargs) to bedrock — it isn't
    in _API_BASE_PROVIDERS, and bedrock routes via AWS creds/region, never an
    HTTP endpoint that would understand those fields."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(_bedrock_resolved(api_base="http://127.0.0.1:9999/v1"))

    assert (
        provider.complete(
            [Message("user", "hi")],
            top_k=20,
            min_p=0.0,
            enable_thinking=False,
        )
        == "ok"
    )

    assert calls[0]["api_base"] == "http://127.0.0.1:9999/v1"
    assert "extra_body" not in calls[0]


def test_litellm_provider_sends_api_base_for_endpoint_backed_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(_compat_resolved(provider="lm_studio", model="local-model"))

    assert provider.complete([Message("user", "hello")]) == "ok"
    assert calls[0]["model"] == "lm_studio/local-model"
    assert calls[0]["api_base"] == _OMLX


def test_litellm_provider_retries_without_response_format_when_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("response_format unsupported")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(_compat_resolved())

    out = provider.complete(
        [Message("user", "hello")],
        response_format={"type": "json_object"},
    )

    assert out == "ok"
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]


def test_structured_output_retry_uses_remaining_task_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def run_in_helper(kwargs: dict, predicate) -> SimpleNamespace:  # noqa: ANN001
        assert predicate() is False
        calls.append(kwargs)
        if len(calls) == 1:
            time.sleep(0.03)
            raise RuntimeError("response_format unsupported")
        return completion(**kwargs)

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    from okto_neuron.llm import _litellm_process

    monkeypatch.setattr(_litellm_process, "run_cancellable_completion", run_in_helper)
    provider = LiteLLMProvider(_compat_resolved())

    with _scoped_call_timeout(0.2):
        out = provider.complete(
            [Message("user", "hello")],
            response_format={"type": "json_object"},
        )

    assert out == "ok"
    assert len(calls) == 2
    assert 0.0 < calls[1]["timeout"] < calls[0]["timeout"] <= 0.2


def test_litellm_provider_redacts_api_key_from_errors_and_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env_name = "OKTO_NEURON_PROVIDER_OPENAI_API_KEY"
    secret = "sk-" + "SENTINEL-provider-secret"

    def completion(**kwargs):
        raise RuntimeError(f"upstream rejected api_key={kwargs['api_key']}")

    monkeypatch.setenv(env_name, secret)
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    caplog.set_level(logging.WARNING, logger="okto_neuron.llm")
    provider = LiteLLMProvider(_compat_resolved(api_key_env=env_name))

    with pytest.raises(LLMProviderError) as raised:
        provider.complete([Message("user", "hello")])

    assert secret not in str(raised.value)
    assert secret not in raised.value.user_message()
    assert secret not in caplog.text
    assert "[redacted]" in str(raised.value)
    assert raised.value.cause is None


def test_llm_config_resolved_builds_stub_provider() -> None:
    """End-to-end: LLMConfig with stub provider resolves and builds StubLLM."""
    cfg = LLMConfig(defaults=LLMDefaults(provider="stub"))
    resolved = cfg.resolved("extraction")
    provider = get_provider(resolved)
    assert isinstance(provider, StubLLM)


# ── Raw sampling-payload override wiring (LiteLLMProvider.complete()) ───────
# Schema/resolution semantics (reserved keys, FROZEN merge, PATCH replace,
# round trip, SAMPLING_PRESETS drift guard) are covered in
# tests/config/test_sampling_payload.py. These tests cover the one thing that
# module can't: what actually reaches the mocked litellm.completion() call.


def test_litellm_provider_sends_arbitrary_raw_payload_keys_unmodified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """typical_p / stop_token_ids / a nested grammar object — none of them
    are OpenAI-standard params, so they must land in extra_body byte-for-byte,
    with no whitelist and no range validation."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    payload = {
        "typical_p": 0.92,
        "stop_token_ids": [151643, 151644],
        "grammar": {"type": "json_object", "schema": {"type": "object", "properties": {}}},
        "temperature": 0.42,
    }
    provider = LiteLLMProvider(_compat_resolved(sampling_payload=payload))

    assert provider.complete([Message("user", "hello")]) == "ok"

    assert calls[0]["temperature"] == 0.42
    assert calls[0]["extra_body"] == {
        "typical_p": 0.92,
        "stop_token_ids": [151643, 151644],
        "grammar": {"type": "json_object", "schema": {"type": "object", "properties": {}}},
    }


def test_litellm_provider_empty_sampling_payload_sends_drop_params_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def get_supported_openai_params(**kwargs):
        return ["max_tokens", "temperature", "top_p"]

    monkeypatch.setitem(
        sys.modules,
        "litellm",
        SimpleNamespace(completion=completion, get_supported_openai_params=get_supported_openai_params),
    )
    provider = LiteLLMProvider(_compat_resolved(sampling_payload={}))

    assert provider.complete([Message("user", "hello")]) == "ok"

    assert calls[0]["drop_params"] is True


def test_litellm_provider_nonempty_sampling_payload_sends_drop_params_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Decision 5: a non-empty raw payload stops asking litellm to silently
    drop unsupported params, so the backend's own rejection surfaces."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(_compat_resolved(sampling_payload={"typical_p": 0.9}))

    assert provider.complete([Message("user", "hello")]) == "ok"

    assert calls[0]["drop_params"] is False


def test_litellm_provider_nonempty_sampling_payload_surfaces_backend_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With drop_params=False, a key the backend itself rejects must raise —
    not be silently dropped, and not be reshaped by the structured-output
    retry (the raw payload here carries no response_format)."""

    def completion(**kwargs):
        raise RuntimeError("400 Bad Request: unknown parameter 'typical_p'")

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(_compat_resolved(sampling_payload={"typical_p": 0.9}))

    with pytest.raises(LLMProviderError, match="typical_p"):
        provider.complete([Message("user", "hello")])


def test_litellm_provider_sampling_payload_overrides_per_call_sampler_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role that opted into a raw payload owns the whole request: this
    method's own per-call temperature=/max_tokens=/… arguments (e.g. an
    extractor's hardcoded class default, or its empty-result cold retry) must
    not leak back into a key the payload already sets."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(_compat_resolved(sampling_payload={"temperature": 0.42}))

    provider.complete([Message("user", "hello")], temperature=0.0, max_tokens=999)

    assert calls[0]["temperature"] == 0.42
    assert "max_tokens" not in calls[0]


def test_litellm_provider_sampling_payload_response_format_still_applies_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """response_format is Okto Neuron's structured-output contract, not a
    sampling preference: the caller's schema must still reach the wire when
    the raw payload doesn't already set its own."""
    calls: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(_compat_resolved(sampling_payload={"typical_p": 0.9}))
    schema = {"type": "json_schema", "json_schema": {"name": "x", "schema": {}}}

    provider.complete([Message("user", "hello")], response_format=schema)

    assert calls[0]["response_format"] == schema


# ── Effective-request observer (what the ingest trace reports) ──────────────
# The tracing wrapper used to build its llm_request event from its OWN method
# arguments, which a raw sampling_payload discards inside complete(). The
# provider now reports the assembled request through this observer at the one
# moment it exists, so the merge has exactly one implementation and the trace
# reports ITS result. Wrapper-side behaviour is covered in
# tests/companion/test_progress.py; these pin the provider end of the seam.


def test_request_observer_receives_effective_params_with_credentials_scrubbed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The observer sees what the backend will see, minus anything that could
    leak a credential: the assembled request carries api_key (and api_base,
    which can embed userinfo) right next to the sampler params."""

    calls: list[dict] = []
    seen: list[dict] = []

    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    monkeypatch.setenv("OKTO_NEURON_TEST_OBSERVER_KEY", "sk-observer-sentinel")
    provider = LiteLLMProvider(
        _compat_resolved(
            api_key_env="OKTO_NEURON_TEST_OBSERVER_KEY",
            sampling_payload={"temperature": 0.2, "max_tokens": 32768, "top_k": 20},
        )
    )

    previous = _set_request_observer(seen.append)
    try:
        provider.complete([Message("user", "hello")], temperature=0.0, max_tokens=16000)
    finally:
        _set_request_observer(previous)

    assert len(seen) == 1
    reported = seen[0]
    assert reported["sampling_payload_applied"] is True
    assert reported["params"]["temperature"] == 0.2
    assert reported["params"]["max_tokens"] == 32768
    assert reported["extra_body"] == {"top_k": 20}
    # The call itself still receives the credential; only the report is scrubbed.
    assert calls[0]["api_key"] == "sk-observer-sentinel"
    assert "sk-observer-sentinel" not in json.dumps(reported)
    for control in ("api_key", "api_base", "messages", "model", "drop_params"):
        assert control not in reported["params"]


def test_request_observer_scrubs_credentials_from_extra_body_too() -> None:
    """The scrub covers BOTH containers, not just the top-level kwargs.

    ``_check_sampling_payload`` refuses a top-level ``api_key``, so today a
    credential cannot ride a raw payload into ``extra_body`` — but that guard
    lives in another module and inspects only top-level names. Called directly
    here rather than through a config round trip, because a config-shaped test
    would be prevented from ever reaching this path by the very validator the
    scrub must not depend on.
    """

    seen: list[dict] = []
    previous = _set_request_observer(seen.append)
    try:
        _notify_request_observer(
            kwargs={"model": "openai/x", "temperature": 0.2},
            extra_body={"api_key": "sk-extra-body-sentinel", "top_k": 20},
            omitted={},
            sampling_payload_applied=True,
        )
    finally:
        _set_request_observer(previous)

    assert len(seen) == 1
    assert seen[0]["extra_body"] == {"top_k": 20}
    assert "sk-extra-body-sentinel" not in json.dumps(seen[0])


def test_request_observer_that_raises_never_fails_the_llm_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The observer is an observability hook installed by the ingest tracer. A
    broken tracer must not take the operator's extraction down with it."""

    def completion(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def exploding(_reported):
        raise RuntimeError("tracer is broken")

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    provider = LiteLLMProvider(_compat_resolved(sampling_payload={"temperature": 0.2}))

    previous = _set_request_observer(exploding)
    try:
        assert provider.complete([Message("user", "hello")]) == "ok"
    finally:
        _set_request_observer(previous)


def _live_model_reachable() -> bool:
    if not _LIVE_API_BASE:
        return False
    try:
        import httpx

        httpx.get(f"{_LIVE_API_BASE.rstrip('/')}/models", timeout=2.0).raise_for_status()
        return True
    except Exception:
        return False


@pytest.mark.skipif(
    not _LIVE_API_BASE,
    reason="OKTO_NEURON_LLM_BASE_URL is required for realmodel tests",
)
@pytest.mark.skipif(not _live_model_reachable(), reason="configured live model is unreachable")
@pytest.mark.realmodel
@pytest.mark.slow
def test_live_completion_against_configured_model() -> None:
    resolved = _compat_resolved(
        api_base=_LIVE_API_BASE,
        model=_LIVE_MODEL,
        enable_thinking=False,
    )
    provider = LiteLLMProvider(resolved)
    out = provider.complete(
        [Message("user", "Reply with the single word: pong")],
        max_tokens=64,
        temperature=0.7,
        top_p=0.95,
        top_k=20,
        min_p=0.0,
        presence_penalty=0.0,
        enable_thinking=True,
    )
    assert isinstance(out, str)
    assert out.strip() != ""


# ── server-side reasoning-split diagnostics (2026-09 leak incident) ───────────


def _fake_litellm_response(
    content: str,
    *,
    reasoning_content: str | None = None,
    finish_reason: str = "stop",
    native_finish_reason: str | None = None,
) -> SimpleNamespace:
    """A ModelResponse-shaped stand-in.

    ``reasoning_content`` is OMITTED from the message when absent, mirroring
    litellm, which DELETES the attribute rather than setting it to None
    (types/utils.py:1233-1241).
    """
    message = SimpleNamespace(content=content)
    if reasoning_content is not None:
        message.reasoning_content = reasoning_content
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    if native_finish_reason is not None:
        choice.provider_specific_fields = {"native_finish_reason": native_finish_reason}
    return SimpleNamespace(choices=[choice], usage=None)


def _complete_with(monkeypatch: pytest.MonkeyPatch, response: SimpleNamespace) -> str:
    monkeypatch.setitem(
        sys.modules, "litellm", SimpleNamespace(completion=lambda **_: response)
    )
    return LiteLLMProvider(_compat_resolved()).complete([Message("user", "hi")])


# The leaked tail from the live incident, shortened. No opening <think>, a
# dangling </think>, then the real answer.
_MISBOUNDED = "still weighing the options here\n</think>\nAlice founded Acme in 2019."


def test_misbounded_reasoning_split_warns_and_records_stats(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from okto_neuron.llm import last_call_stats

    with caplog.at_level(logging.WARNING, logger="okto_neuron.llm"):
        text = _complete_with(
            monkeypatch,
            _fake_litellm_response(_MISBOUNDED, reasoning_content="a" * 11294),
        )

    assert text == "Alice founded Acme in 2019."
    stats = last_call_stats() or {}
    assert stats["reasoning_content_present"] is True
    assert stats["reasoning_stripped_chars"] == len(_MISBOUNDED) - len(text)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("reasoning split MISBOUNDED" in m for m in warnings)
    assert any("Qwen3.5-0.8B" in m for m in warnings)


def test_misbounded_split_warning_and_stats_never_carry_the_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Neither the reasoning nor the stripped content may be logged or stored:
    both can be long and can contain vault data."""
    from okto_neuron.llm import last_call_stats

    secret_reasoning = "PRIVATE-REASONING-MARKER"
    stripped_marker = "STRIPPED-CONTENT-MARKER"
    raw = f"{stripped_marker}\n</think>\nthe answer."
    with caplog.at_level(logging.DEBUG, logger="okto_neuron.llm"):
        _complete_with(
            monkeypatch,
            _fake_litellm_response(raw, reasoning_content=secret_reasoning),
        )

    stats = last_call_stats() or {}
    assert secret_reasoning not in repr(stats)
    assert stripped_marker not in repr(stats)
    for record in caplog.records:
        if record.levelno >= logging.WARNING:
            assert secret_reasoning not in record.getMessage()
            assert stripped_marker not in record.getMessage()


def test_clean_content_with_reasoning_content_does_not_warn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from okto_neuron.llm import last_call_stats

    with caplog.at_level(logging.WARNING, logger="okto_neuron.llm"):
        text = _complete_with(
            monkeypatch,
            _fake_litellm_response("Alice founded Acme.", reasoning_content="thinking"),
        )

    assert text == "Alice founded Acme."
    stats = last_call_stats() or {}
    assert stats.get("reasoning_content_present") is True
    assert "reasoning_stripped_chars" not in stats
    assert not [
        r for r in caplog.records if "MISBOUNDED" in r.getMessage()
    ]


def test_no_reasoning_content_attribute_is_unchanged_behaviour(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """litellm DELETES ``reasoning_content`` when absent — reading it must not
    raise, and stripping alone must not warn."""
    from okto_neuron.llm import last_call_stats

    with caplog.at_level(logging.WARNING, logger="okto_neuron.llm"):
        text = _complete_with(monkeypatch, _fake_litellm_response(_MISBOUNDED))

    assert text == "Alice founded Acme in 2019."
    stats = last_call_stats() or {}
    assert "reasoning_content_present" not in stats
    assert stats["reasoning_stripped_chars"] == len(_MISBOUNDED) - len(text)
    assert not [r for r in caplog.records if "MISBOUNDED" in r.getMessage()]


def test_native_finish_reason_is_recorded_when_it_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """litellm's map_finish_reason defaults an UNMAPPED reason to "stop" and
    keeps the raw value in provider_specific_fields["native_finish_reason"]."""
    from okto_neuron.llm import last_call_stats

    _complete_with(
        monkeypatch,
        _fake_litellm_response(
            "partial", finish_reason="stop", native_finish_reason="server_overloaded"
        ),
    )
    stats = last_call_stats() or {}
    assert stats["finish_reason"] == "stop"
    assert stats["native_finish_reason"] == "server_overloaded"
    assert stats["finish_reason_unmapped"] is True


def test_known_stop_alias_is_not_flagged_as_unmapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``end_turn``/``COMPLETE``/``eos_token``/``STOP`` are ALIASES of a clean
    stop in litellm's _FINISH_REASON_MAP. litellm still records them as
    ``native_finish_reason`` because it rewrote the value, so treating any
    native value as abnormal would flag every clean Anthropic/Cohere/Gemini
    answer."""
    from okto_neuron.llm import last_call_stats

    for alias in ("end_turn", "COMPLETE", "eos_token", "STOP"):
        _complete_with(
            monkeypatch,
            _fake_litellm_response("done.", finish_reason="stop", native_finish_reason=alias),
        )
        stats = last_call_stats() or {}
        assert stats["native_finish_reason"] == alias
        assert "finish_reason_unmapped" not in stats


def test_network_error_mapped_to_stop_by_litellm_is_still_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Z.ai/GLM reports a mid-generation transport failure as
    ``network_error``; litellm's _FINISH_REASON_MAP rewrites that to a clean
    "stop", so the unmapped test alone would let it through. The explicit
    ``_NATIVE_ABNORMAL_STOP_REASONS`` set must catch it."""
    from litellm.litellm_core_utils.core_helpers import _FINISH_REASON_MAP

    from okto_neuron.llm import last_call_stats

    assert _FINISH_REASON_MAP.get("network_error") == "stop"  # the premise this guards
    _complete_with(
        monkeypatch,
        _fake_litellm_response("cut", finish_reason="stop", native_finish_reason="network_error"),
    )
    stats = last_call_stats() or {}
    assert stats["finish_reason"] == "stop"
    assert stats["native_finish_reason"] == "network_error"
    assert stats["finish_reason_unmapped"] is True


def test_separate_reasoning_content_never_reaches_the_returned_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider that returns its chain of thought in a separate
    ``reasoning_content`` field (Z.ai/GLM, DeepSeek) must have that field
    dropped: only ``content`` is the answer."""
    from okto_neuron.llm import last_call_stats

    text = _complete_with(
        monkeypatch,
        _fake_litellm_response(
            "Paris.", reasoning_content="The user asks for the capital of France..."
        ),
    )
    assert text == "Paris."
    assert "capital of France" not in text
    stats = last_call_stats() or {}
    assert stats["reasoning_content_present"] is True


def test_real_litellm_choices_alias_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Built through the REAL ``litellm.types.utils.Choices``, so the
    normalization and native-value behaviour is litellm's, not the fake's."""
    from litellm.types.utils import Choices, Message as LiteLLMMessage

    from okto_neuron.llm import last_call_stats

    for raw, expect_unmapped in (("end_turn", False), ("wedged_by_proxy", True)):
        choice = Choices(
            finish_reason=raw, message=LiteLLMMessage(content="done.", role="assistant")
        )
        assert choice.finish_reason == "stop"  # both normalize to a clean stop
        _complete_with(
            monkeypatch, SimpleNamespace(choices=[choice], usage=None)
        )
        stats = last_call_stats() or {}
        assert stats["native_finish_reason"] == raw
        assert stats.get("finish_reason_unmapped", False) is expect_unmapped
