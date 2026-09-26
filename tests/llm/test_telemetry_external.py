"""Contract tests for ``trace_external_completion`` — the public seam.

The provider path is traced by wrapping ``get_provider``. Some callers never
reach it: the LoCoMo judge (``benchmarks/locomo/score.py``) POSTs to the judge
endpoint itself, offline, with no vault and no daemon. This seam exists so
those calls land in the same experiment, with the same span vocabulary, as the
daemon's own.

What these pin is therefore mostly SAMENESS: the same env gate, the same
fail-open behaviour, the same attributes and tags, the same
``degradation_reason`` precedence. A helper that exported a *different* shape
would be worse than no helper, because the whole point is one query that finds
both kinds of call.
"""

from __future__ import annotations

import sys

import pytest

from okto_neuron.llm import _telemetry
from okto_neuron.llm import trace_external_completion


@pytest.fixture(autouse=True)
def _clean_telemetry_state():
    _telemetry._reset_for_tests()
    yield
    _telemetry._reset_for_tests()


@pytest.fixture()
def captured(monkeypatch):
    """Every ``record()`` payload the block produced, without a server."""
    payloads: list[dict] = []
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://tracking.invalid")
    monkeypatch.setattr(_telemetry, "record", lambda **fields: payloads.append(fields))
    return payloads


_MESSAGES = [{"role": "user", "content": "is this answer correct?"}]


class _FakeSpan:
    trace_id = "tid"


class _FakeClient:
    def __init__(self) -> None:
        self.started: dict = {}

    def start_trace(self, **kwargs):  # type: ignore[no-untyped-def]
        self.started = kwargs
        return _FakeSpan()

    def end_trace(self, trace_id, **kwargs):  # type: ignore[no-untyped-def]
        return None


def _emit_with_fake(monkeypatch, record: dict) -> _FakeClient:
    """Run one record through the REAL ``_emit``, with no server behind it."""
    client = _FakeClient()
    monkeypatch.setattr(_telemetry, "_client", lambda: (client, "7"))
    _telemetry._emit(record)
    return client


def _body(
    content: str | None = "CORRECT",
    finish_reason: str = "stop",
    **extra,
) -> dict:
    return {
        "choices": [
            {"message": {"content": content}, "finish_reason": finish_reason, **extra}
        ],
        "usage": {
            "prompt_tokens": 40,
            "completion_tokens": 4,
            "total_tokens": 44,
            "prompt_tokens_details": {"cached_tokens": 7},
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }


# ── Rule 1: the same env gate, no mlflow import ──────────────────────────────


def test_off_yields_a_noop_handle(monkeypatch):
    monkeypatch.delenv(_telemetry.ENV_TRACKING_URI, raising=False)
    recorded: list[dict] = []
    monkeypatch.setattr(_telemetry, "record", lambda **f: recorded.append(f))
    with trace_external_completion(model="m", step="judge") as span:
        assert span.enabled is False
        span.set_openai_response(_body())
        span.set_response(text="x")
        span.set_error(RuntimeError("boom"))
    assert recorded == []


def test_off_never_imports_mlflow(monkeypatch):
    monkeypatch.delenv(_telemetry.ENV_TRACKING_URI, raising=False)
    monkeypatch.delitem(sys.modules, "mlflow", raising=False)
    with trace_external_completion(model="m", step="judge") as span:
        span.set_openai_response(_body())
    assert "mlflow" not in sys.modules


# ── The span vocabulary must match the provider path's ───────────────────────


def test_openai_body_fills_the_provider_path_vocabulary(captured):
    with trace_external_completion(
        model="glm-5.3-flash",
        step="judge",
        provider="zai",
        api_base="https://api.z.ai/api/coding/paas/v4",
        messages=_MESSAGES,
        params={"temperature": 0.0},
    ) as span:
        span.set_openai_response(_body())

    (fields,) = captured
    assert fields["name"] == "llm.judge"
    assert fields["step"] == "judge"
    assert fields["model"] == "glm-5.3-flash"
    assert fields["provider"] == "zai"
    assert fields["api_base"] == "https://api.z.ai/api/coding/paas/v4"
    assert fields["messages"] == [{"role": "user", "content": "is this answer correct?"}]
    assert fields["params"] == {"temperature": 0.0}
    # The caller built the request itself; claiming an on-the-wire capture
    # would be a lie the provider path does not tell.
    assert fields["params_source"] == "requested"
    assert fields["response"] == "CORRECT"
    assert fields["finish_reason"] == "stop"
    assert fields["finish_reason_available"] is True
    assert fields["latency_ms"] >= 0
    assert fields["end_time_ns"] >= fields["start_time_ns"]


def test_usage_is_translated_to_last_call_stats_keys(captured):
    """``_emit`` reads ``last_call_stats()``'s names, not OpenAI's nesting."""
    with trace_external_completion(model="m", step="judge") as span:
        span.set_openai_response(_body())
    (fields,) = captured
    assert fields["usage"] == {
        "prompt_tokens": 40,
        "completion_tokens": 4,
        "total_tokens": 44,
        "cached_tokens": 7,
        "reasoning_tokens": 3,
    }


def test_provider_defaults_to_external(captured):
    with trace_external_completion(model="m", step="judge") as span:
        span.set_response(text="ok", finish_reason="stop")
    assert captured[0]["provider"] == "external"


def test_native_finish_reason_survives(captured):
    with trace_external_completion(model="m", step="judge") as span:
        span.set_openai_response(_body(finish_reason="stop", native_finish_reason="MAX_TOKENS"))
    assert captured[0]["native_finish_reason"] == "MAX_TOKENS"


# ── Degradation: same precedence, judged by the same function ────────────────


@pytest.mark.parametrize(
    ("finish_reason", "content", "reason"),
    [
        ("length", "half an ans", "truncated"),
        ("content_filter", "blocked", "abnormal_stop"),
        ("stop", "", "empty"),
        ("stop", "a real answer", None),
    ],
)
def test_degradation_matches_the_provider_path(captured, finish_reason, content, reason):
    with trace_external_completion(model="m", step="judge") as span:
        span.set_openai_response(_body(content=content, finish_reason=finish_reason))
    assert _telemetry.degradation_reason(captured[0]) == reason


def test_reported_error_outranks_everything(captured):
    with trace_external_completion(model="m", step="judge") as span:
        span.set_openai_response(_body())
        span.set_error(RuntimeError("401 Unauthorized"))
    (fields,) = captured
    assert fields["error"] == "401 Unauthorized"
    assert fields["error_type"] == "RuntimeError"
    assert _telemetry.degradation_reason(fields) == "provider_error"


def test_escaping_exception_is_recorded_and_re_raised(captured):
    """A raise the caller did NOT catch is the one outcome it cannot forget."""
    with pytest.raises(ValueError, match="boom"):
        with trace_external_completion(model="m", step="judge"):
            raise ValueError("boom")
    (fields,) = captured
    assert fields["error"] == "boom"
    assert fields["error_type"] == "ValueError"
    assert fields["finish_reason_available"] is False
    assert _telemetry.degradation_reason(fields) == "provider_error"


# ── Rule 2: fail-open, always ────────────────────────────────────────────────


def test_a_malformed_response_body_never_raises(captured):
    with trace_external_completion(model="m", step="judge") as span:
        span.set_openai_response("not a response at all")
        span.set_openai_response({"choices": "nonsense"})
    # No exception, and the absent answer is reported honestly.
    assert _telemetry.degradation_reason(captured[0]) == "empty"


def test_a_broken_record_never_escapes(monkeypatch):
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://tracking.invalid")

    def _explode(**fields):
        raise RuntimeError("telemetry is on fire")

    monkeypatch.setattr(_telemetry, "record", _explode)
    with trace_external_completion(model="m", step="judge") as span:
        span.set_response(text="ok", finish_reason="stop")


def test_the_callers_own_exception_wins_over_a_telemetry_failure(monkeypatch):
    """Telemetry must never replace the error the caller is trying to see."""
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://tracking.invalid")
    monkeypatch.setattr(
        _telemetry, "record", lambda **f: (_ for _ in ()).throw(RuntimeError("secondary"))
    )
    with pytest.raises(ValueError, match="primary"):
        with trace_external_completion(model="m", step="judge"):
            raise ValueError("primary")


# ── Message shapes ───────────────────────────────────────────────────────────


def test_message_objects_normalize_like_provider_messages(captured):
    class _Msg:
        role = "system"
        content = "judge this"

    with trace_external_completion(model="m", step="judge", messages=[_Msg()]) as span:
        span.set_response(text="ok", finish_reason="stop")
    assert captured[0]["messages"] == [{"role": "system", "content": "judge this"}]


# ── Draining before exit ─────────────────────────────────────────────────────


def test_flush_telemetry_is_true_when_there_is_nothing_to_drain(monkeypatch):
    """A caller may flush unconditionally; off is not a failure to drain."""
    from okto_neuron.llm import flush_telemetry

    monkeypatch.delenv(_telemetry.ENV_TRACKING_URI, raising=False)
    assert flush_telemetry(0.1) is True


def test_flush_telemetry_drains_a_queued_span(monkeypatch):
    """The drain thread is a daemon, so a short-lived judge run needs this."""
    from okto_neuron.llm import flush_telemetry

    emitted: list[dict] = []
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://tracking.invalid")
    monkeypatch.setattr(_telemetry, "_emit", emitted.append)
    with trace_external_completion(model="m", step="judge") as span:
        span.set_response(text="ok", finish_reason="stop")
    assert flush_telemetry(5.0) is True
    assert [record["step"] for record in emitted] == ["judge"]


# ── Run identity: one experiment, sliced by tags ─────────────────────────────


def test_static_env_tags_reach_every_span(monkeypatch):
    """How a spawner tells a daemon which run it is part of."""
    monkeypatch.setenv(_telemetry.ENV_TAGS, '{"locomo.run_name": "arm-a", "locomo.phase": "ask"}')
    client = _emit_with_fake(monkeypatch, {"response": "ok", "finish_reason": "stop"})
    assert client.started["tags"]["locomo.run_name"] == "arm-a"
    assert client.started["tags"]["locomo.phase"] == "ask"


def test_per_span_tags_win_over_env_tags(monkeypatch, captured):
    monkeypatch.setenv(_telemetry.ENV_TAGS, '{"locomo.phase": "ingest"}')
    with trace_external_completion(
        model="m", step="judge", tags={"locomo.phase": "score"}
    ) as span:
        span.set_response(text="ok", finish_reason="stop")
    client = _emit_with_fake(monkeypatch, captured[0])
    assert client.started["tags"]["locomo.phase"] == "score"


def test_span_identity_tags_are_not_overridable(monkeypatch, captured):
    """``marginalia.step`` must mean the step, whatever a spawner exported."""
    monkeypatch.setenv(_telemetry.ENV_TAGS, '{"marginalia.step": "lies"}')
    with trace_external_completion(model="m", step="judge") as span:
        span.set_response(text="ok", finish_reason="stop")
    client = _emit_with_fake(monkeypatch, captured[0])
    assert client.started["tags"]["marginalia.step"] == "judge"


@pytest.mark.parametrize("raw", ["not json", "[1, 2]", '"a string"', "   "])
def test_malformed_env_tags_are_ignored_not_fatal(monkeypatch, raw):
    monkeypatch.setenv(_telemetry.ENV_TAGS, raw)
    assert _telemetry.static_tags() == {}


def test_env_tag_values_are_stringified(monkeypatch):
    monkeypatch.setenv(_telemetry.ENV_TAGS, '{"locomo.limit": 50, "locomo.tier": null}')
    assert _telemetry.static_tags() == {"locomo.limit": "50"}
