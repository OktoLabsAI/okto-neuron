"""Contract tests for the optional MLflow GenAI export.

These pin the three rules the feature lives or dies by — env-gated, fail-open,
off the critical path — plus the observer-chaining that keeps the ingest
inspector working underneath it. They do NOT stand in for the end-to-end proof
against a real tracking server; a green unit test here with a broken wire
format would still export nothing.
"""

from __future__ import annotations

import sys
import threading

import pytest

from okto_neuron.llm import _telemetry
from okto_neuron.llm._litellm_process import _response_from_payload


class _StubProvider:
    model = "stub-model"
    api_base = "http://example.invalid/v1"
    traces_effective_request = True

    def __init__(self, *, fail: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._fail = fail

    def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        if self._fail is not None:
            raise self._fail
        return "answer"


class _Msg:
    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


@pytest.fixture(autouse=True)
def _clean_telemetry_state():
    _telemetry._reset_for_tests()
    yield
    _telemetry._reset_for_tests()


# ── Rule 1: env-gated ────────────────────────────────────────────────────────


def test_disabled_without_env(monkeypatch):
    monkeypatch.delenv(_telemetry.ENV_TRACKING_URI, raising=False)
    assert _telemetry.enabled() is False
    assert _telemetry.tracking_uri() is None


def test_blank_env_is_off(monkeypatch):
    """An empty or whitespace value is an operator typo, not an opt-in."""
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "   ")
    assert _telemetry.enabled() is False


def test_wrap_provider_returns_same_object_when_off(monkeypatch):
    monkeypatch.delenv(_telemetry.ENV_TRACKING_URI, raising=False)
    provider = _StubProvider()
    assert _telemetry.wrap_provider(provider, object()) is provider


def test_record_is_a_noop_when_off(monkeypatch):
    monkeypatch.delenv(_telemetry.ENV_TRACKING_URI, raising=False)
    _telemetry.record(model="m", messages=[])
    assert _telemetry._QUEUE is None or _telemetry._QUEUE.unfinished_tasks == 0


def test_mlflow_is_never_imported_when_off(monkeypatch):
    """The strongest form of rule 1: not merely unused, never loaded.

    Asserted by making any import of mlflow raise. A plain
    ``"mlflow" not in sys.modules`` check would pass vacuously if something
    else in the test session had already imported it.
    """
    monkeypatch.delenv(_telemetry.ENV_TRACKING_URI, raising=False)
    monkeypatch.setitem(sys.modules, "mlflow", None)  # import mlflow -> ImportError
    provider = _StubProvider()
    wrapped = _telemetry.wrap_provider(provider, object())
    assert wrapped.complete([_Msg("user", "hi")]) == "answer"


def test_experiment_name_default_and_override(monkeypatch):
    monkeypatch.delenv(_telemetry.ENV_EXPERIMENT, raising=False)
    assert _telemetry.experiment_name() == "okto-neuron"
    monkeypatch.setenv(_telemetry.ENV_EXPERIMENT, "locomo")
    assert _telemetry.experiment_name() == "locomo"


# ── Rule 2: fail-open ────────────────────────────────────────────────────────


def test_unreachable_server_does_not_break_the_call(monkeypatch, caplog):
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://127.0.0.1:1/unreachable")
    provider = _StubProvider()
    wrapped = _telemetry.wrap_provider(provider, object())
    caplog.set_level("WARNING", logger="okto_neuron.llm.telemetry")
    assert wrapped.complete([_Msg("user", "hi")], temperature=0.1) == "answer"
    _telemetry.flush(15.0)
    # The call succeeded; the failure was reported exactly once, as a warning.
    assert provider.calls == [
        {
            "temperature": 0.1,
            "max_tokens": None,
            "top_p": None,
            "top_k": None,
            "min_p": None,
            "presence_penalty": None,
            "enable_thinking": None,
            "response_format": None,
        }
    ]


def test_broken_emit_never_escapes(monkeypatch):
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://example.invalid:5000")
    monkeypatch.setattr(
        _telemetry, "_emit", lambda record: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    provider = _StubProvider()
    wrapped = _telemetry.wrap_provider(provider, object())
    assert wrapped.complete([_Msg("user", "hi")]) == "answer"
    assert _telemetry.flush(5.0) is True


def test_provider_error_is_recorded_and_re_raised(monkeypatch):
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://example.invalid:5000")
    captured: list[dict] = []
    monkeypatch.setattr(_telemetry, "_emit", captured.append)
    boom = RuntimeError("provider exploded")
    wrapped = _telemetry.wrap_provider(_StubProvider(fail=boom), object())
    with pytest.raises(RuntimeError, match="provider exploded"):
        wrapped.complete([_Msg("user", "hi")])
    _telemetry.flush(5.0)
    assert len(captured) == 1
    assert captured[0]["error"] == "provider exploded"
    assert captured[0]["error_type"] == "RuntimeError"


# ── Rule 3: off the critical path ────────────────────────────────────────────


def test_record_does_not_block_when_queue_is_full(monkeypatch):
    """A wedged exporter must cost a dropped span, never a stalled call."""
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://example.invalid:5000")
    release = threading.Event()
    monkeypatch.setattr(_telemetry, "_emit", lambda record: release.wait(30.0))
    monkeypatch.setattr(_telemetry, "_QUEUE_MAXSIZE", 2)
    try:
        wrapped = _telemetry.wrap_provider(_StubProvider(), object())
        for _ in range(20):
            assert wrapped.complete([_Msg("user", "hi")]) == "answer"
    finally:
        release.set()
        _telemetry.flush(30.0)


# ── The observer chain ───────────────────────────────────────────────────────


def test_existing_request_observer_still_fires(monkeypatch):
    """The single-slot observer must be chained, not replaced.

    ``_TracingLLMProvider`` installs its observer OUTSIDE this wrapper. If
    this wrapper overwrote it, the ingest inspector's
    one-``llm_request``-event-per-call invariant would break silently.
    """
    from okto_neuron.llm import _notify_request_observer, _set_request_observer

    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://example.invalid:5000")
    captured: list[dict] = []
    monkeypatch.setattr(_telemetry, "_emit", captured.append)

    outer_seen: list[dict] = []

    class _ReportingProvider(_StubProvider):
        def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            _notify_request_observer(
                kwargs={"model": "stub-model", "temperature": 0.5},
                extra_body={"thinking": {"type": "disabled"}},
                omitted={"top_k": "unsupported"},
                sampling_payload_applied=True,
            )
            return super().complete(messages, **kwargs)

    wrapped = _telemetry.wrap_provider(_ReportingProvider(), object())
    previous = _set_request_observer(outer_seen.append)
    try:
        assert wrapped.complete([_Msg("user", "hi")]) == "answer"
    finally:
        _set_request_observer(previous)

    assert len(outer_seen) == 1, "the outer (inspector) observer must still fire"
    _telemetry.flush(5.0)
    assert len(captured) == 1
    record = captured[0]
    assert record["params"]["temperature"] == 0.5
    assert record["params_source"] == "effective"
    assert record["extra_body"] == {"thinking": {"type": "disabled"}}
    assert record["omitted_params"] == {"top_k": "unsupported"}
    assert record["sampling_payload_applied"] is True


def test_observer_slot_is_restored(monkeypatch):
    from okto_neuron.llm import _set_request_observer

    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://example.invalid:5000")
    monkeypatch.setattr(_telemetry, "_emit", lambda record: None)
    sentinel = lambda payload: None  # noqa: E731
    previous = _set_request_observer(sentinel)
    try:
        wrapped = _telemetry.wrap_provider(_StubProvider(), object())
        wrapped.complete([_Msg("user", "hi")])
        from okto_neuron.llm import _call_request_observer

        assert _call_request_observer.value is sentinel
    finally:
        _set_request_observer(previous)


def test_wrapper_delegates_capability_flags(monkeypatch):
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://example.invalid:5000")
    wrapped = _telemetry.wrap_provider(_StubProvider(), object())
    assert wrapped.traces_effective_request is True
    assert wrapped.model == "stub-model"
    assert wrapped.api_base == "http://example.invalid/v1"


# ── Honest reporting of what is NOT known ────────────────────────────────────


def test_absent_finish_reason_is_reported_as_absent(monkeypatch):
    """A CLI provider reports no finish reason. Say so; never invent "stop"."""
    from okto_neuron.llm import _set_last_call_stats

    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://example.invalid:5000")
    captured: list[dict] = []
    monkeypatch.setattr(_telemetry, "_emit", captured.append)

    class _CliLike(_StubProvider):
        def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            _set_last_call_stats({"prompt_tokens": 7, "completion_tokens": 3})
            return super().complete(messages, **kwargs)

    wrapped = _telemetry.wrap_provider(_CliLike(), object())
    wrapped.complete([_Msg("user", "hi")])
    _telemetry.flush(5.0)
    assert captured[0]["finish_reason_available"] is False
    assert "finish_reason" not in captured[0]
    _set_last_call_stats(None)


def test_native_finish_reason_reaches_the_span(monkeypatch):
    from okto_neuron.llm import _set_last_call_stats

    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://example.invalid:5000")
    captured: list[dict] = []
    monkeypatch.setattr(_telemetry, "_emit", captured.append)

    class _Litellmish(_StubProvider):
        def complete(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            _set_last_call_stats(
                {
                    "prompt_tokens": 11,
                    "completion_tokens": 2,
                    "finish_reason": "stop",
                    "native_finish_reason": "network_error",
                    "finish_reason_unmapped": True,
                }
            )
            return super().complete(messages, **kwargs)

    wrapped = _telemetry.wrap_provider(_Litellmish(), object())
    wrapped.complete([_Msg("user", "hi")])
    _telemetry.flush(5.0)
    record = captured[0]
    assert record["finish_reason"] == "stop"
    assert record["native_finish_reason"] == "network_error"
    assert record["finish_reason_unmapped"] is True
    assert record["finish_reason_available"] is True
    _set_last_call_stats(None)


# ── Worker payload contract (v1 ⇄ v2) ────────────────────────────────────────


def test_v1_worker_payload_still_decodes():
    """Backward compatibility is not optional: an old worker must still work."""
    response = _response_from_payload(
        {
            "ok": True,
            "content": "hello",
            "finish_reason": "stop",
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "cached_tokens": 2,
        }
    )
    choice = response.choices[0]
    assert choice.message.content == "hello"
    assert choice.finish_reason == "stop"
    # v1 knows nothing of these, and must yield exactly what it used to: None.
    assert choice.provider_specific_fields is None
    assert getattr(choice.message, "reasoning_content", None) is None
    assert choice.message.tool_calls is None
    assert response.usage.prompt_tokens == 10
    assert response.usage.total_tokens is None


def test_v2_worker_payload_carries_native_finish_reason():
    """The gap this closes: in the daemon the parent could NEVER see it."""
    response = _response_from_payload(
        {
            "ok": True,
            "protocol": 2,
            "content": "hello",
            "finish_reason": "stop",
            "native_finish_reason": "network_error",
            "reasoning_content": "thinking...",
            "tool_calls": [{"id": "c1", "type": "function", "name": "f", "arguments": "{}"}],
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "cached_tokens": 2,
            "reasoning_tokens": 5,
        }
    )
    choice = response.choices[0]
    assert choice.provider_specific_fields == {"native_finish_reason": "network_error"}
    assert choice.message.reasoning_content == "thinking..."
    assert choice.message.tool_calls[0]["name"] == "f"
    assert response.usage.total_tokens == 14
    assert response.usage.completion_tokens_details.reasoning_tokens == 5
    assert response.usage.prompt_tokens_details.cached_tokens == 2


# ── Degraded calls must never read as a clean success ────────────────────────


class _FakeSpan:
    trace_id = "tid"


class _FakeClient:
    """Captures exactly what ``_emit`` hands MLflow, with no server."""

    def __init__(self) -> None:
        self.started: dict = {}
        self.ended: dict = {}

    def start_trace(self, **kwargs):  # type: ignore[no-untyped-def]
        self.started = kwargs
        return _FakeSpan()

    def end_trace(self, trace_id, **kwargs):  # type: ignore[no-untyped-def]
        self.ended = {"trace_id": trace_id, **kwargs}


def _emit_with_fake_client(monkeypatch, record: dict) -> _FakeClient:
    client = _FakeClient()
    monkeypatch.setattr(_telemetry, "_client", lambda: (client, "7"))
    _telemetry._emit(record)
    return client


def test_clean_answer_is_ok_and_not_degraded(monkeypatch):
    """The control: a normal call must still look perfectly healthy."""
    client = _emit_with_fake_client(
        monkeypatch, {"response": "an answer", "finish_reason": "stop"}
    )
    assert client.ended["status"] == "OK"
    assert client.started["attributes"]["marginalia.degraded"] is False
    assert "marginalia.degraded_reason" not in client.started["attributes"]
    assert "marginalia.degraded_reason" not in client.started["tags"]


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        ({"response": "", "error": "boom", "finish_reason": "stop"}, "provider_error"),
        ({"response": "cut off he", "finish_reason": "length"}, "truncated"),
        ({"response": "text", "finish_reason": "content_filter"}, "abnormal_stop"),
        (
            {"response": "text", "finish_reason": "stop", "finish_reason_unmapped": True},
            "abnormal_stop",
        ),
        ({"response": "   \n ", "finish_reason": "stop"}, "empty"),
        ({"response": None, "finish_reason": "stop"}, "empty"),
    ],
)
def test_degraded_calls_carry_a_reason_and_are_not_ok(monkeypatch, record, reason):
    client = _emit_with_fake_client(monkeypatch, dict(record))
    assert _telemetry.degradation_reason(record) == reason
    attributes = client.started["attributes"]
    assert attributes["marginalia.degraded"] is True
    assert attributes["marginalia.degraded_reason"] == reason
    # Queryable from the UI / ``search_traces``, which filter on TAGS.
    assert client.started["tags"]["marginalia.degraded_reason"] == reason
    assert client.ended["status"] != "OK"


def test_degraded_status_is_error_because_unset_is_unreachable(monkeypatch):
    """OTel silently drops an UNSET status, so a degraded span must be ERROR.

    ``opentelemetry/sdk/trace/__init__.py`` ignores ``set_status(UNSET)``, so
    ending the span "UNSET" leaves it OK — i.e. reading as a clean success,
    which is the bug. The reason attribute, not the status, separates a failed
    call from a degraded one.
    """
    failed = _emit_with_fake_client(monkeypatch, {"response": "", "error": "boom"})
    assert failed.ended["status"] == "ERROR"
    truncated = _emit_with_fake_client(
        monkeypatch, {"response": "half", "finish_reason": "length"}
    )
    assert truncated.ended["status"] == _telemetry._DEGRADED_SPAN_STATUS == "ERROR"
    assert truncated.started["attributes"]["marginalia.degraded_reason"] == "truncated"


def test_tool_call_turn_with_no_prose_is_not_degraded():
    """Inventing a failure is the same sin as inventing a success."""
    record = {"response": "", "finish_reason": "stop", "tool_calls": [{"id": "a"}]}
    assert _telemetry.degradation_reason(record) is None


def test_absent_finish_reason_alone_is_not_degradation():
    """CLI providers report none; that is stated, not a fault."""
    record = {"response": "an answer", "finish_reason_available": False}
    assert _telemetry.degradation_reason(record) is None


# ---------------------------------------------------------------------------
# First-use race in _client(): N threads resolving the experiment at once.
#
# trace_parent/trace_child call _client() synchronously on caller threads, so a
# process's first traced calls can resolve the client concurrently. Before the
# fix every thread saw no experiment, all called create_experiment, the losers
# got RESOURCE_ALREADY_EXISTS, and _CLIENT_FAILED disabled export for the whole
# process (measured live: 0 of 12 traces landed, 3 runs of 3).


class _AlreadyExists(Exception):
    """Shaped like MLflow's RestException for a duplicate experiment name."""

    error_code = "RESOURCE_ALREADY_EXISTS"


def _fake_mlflow(*, lookup_delay_s: float, preexisting_after_create: bool = False):
    """A stand-in ``mlflow`` module whose server state is shared by all clients.

    ``lookup_delay_s`` widens the lookup-then-create window so that, without a
    lock, every thread passes the lookup before any of them creates.
    ``preexisting_after_create`` models ANOTHER PROCESS winning the create: our
    lookup misses, our create is refused, and a second lookup finds it.
    """
    import time
    import types

    state = {"experiments": {}, "creates": 0, "lock": threading.Lock()}

    class _Experiment:
        def __init__(self, experiment_id: str) -> None:
            self.experiment_id = experiment_id

    class MlflowClient:
        def __init__(self, tracking_uri=None):  # type: ignore[no-untyped-def]
            self.tracking_uri = tracking_uri

        def get_experiment_by_name(self, name):  # type: ignore[no-untyped-def]
            # Snapshot on ARRIVAL, then pay the latency — the way a real server
            # answers from its state at request time. Reading after the sleep
            # let the first thread to wake create the experiment before the
            # others read, so this fake passed on the unfixed code.
            with state["lock"]:
                found = state["experiments"].get(name)
            time.sleep(lookup_delay_s)
            return _Experiment(found) if found is not None else None

        def create_experiment(self, name):  # type: ignore[no-untyped-def]
            with state["lock"]:
                state["creates"] += 1
                if preexisting_after_create or name in state["experiments"]:
                    state["experiments"][name] = "7"
                    raise _AlreadyExists(f"Experiment(name={name}) already exists")
                state["experiments"][name] = "7"
                return "7"

    module = types.ModuleType("mlflow")
    module.MlflowClient = MlflowClient  # type: ignore[attr-defined]
    module.set_tracking_uri = lambda uri: None  # type: ignore[attr-defined]
    return module, state


def test_concurrent_first_use_resolves_one_experiment_and_stays_enabled(monkeypatch):
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://mlflow.invalid:5000")
    monkeypatch.setenv(_telemetry.ENV_EXPERIMENT, "race-test")
    fake, state = _fake_mlflow(lookup_delay_s=0.05)
    monkeypatch.setitem(sys.modules, "mlflow", fake)

    n = 16
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i: int) -> None:
        barrier.wait()
        results[i] = _telemetry._client()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert _telemetry._CLIENT_FAILED is False
    assert all(result is not None for result in results)
    assert {result[1] for result in results} == {"7"}
    # Serialised: exactly one thread did the lookup-then-create.
    assert state["creates"] == 1


def test_experiment_created_by_another_process_is_adopted(monkeypatch):
    # The lock only serialises threads in THIS process; a harness and the
    # daemons it spawns can still race on the same new experiment name.
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://mlflow.invalid:5000")
    monkeypatch.setenv(_telemetry.ENV_EXPERIMENT, "race-test")
    fake, state = _fake_mlflow(lookup_delay_s=0.0, preexisting_after_create=True)
    monkeypatch.setitem(sys.modules, "mlflow", fake)

    resolved = _telemetry._client()

    assert resolved is not None
    assert resolved[1] == "7"
    assert _telemetry._CLIENT_FAILED is False
    assert state["creates"] == 1


def test_a_genuine_client_failure_still_disables_export(monkeypatch):
    # Adopting RESOURCE_ALREADY_EXISTS must not swallow real failures.
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://mlflow.invalid:5000")
    fake, _ = _fake_mlflow(lookup_delay_s=0.0)

    def _unreachable(self, name):  # type: ignore[no-untyped-def]
        raise ConnectionError("connection refused")

    fake.MlflowClient.get_experiment_by_name = _unreachable  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mlflow", fake)

    assert _telemetry._client() is None
    assert _telemetry._CLIENT_FAILED is True


# ── Tracking URI set, mlflow missing: a degraded state must never look like success ──


def test_missing_mlflow_warning_is_none_when_off(monkeypatch):
    monkeypatch.delenv(_telemetry.ENV_TRACKING_URI, raising=False)
    monkeypatch.setitem(sys.modules, "mlflow", None)
    assert _telemetry.missing_mlflow_warning() is None


def test_missing_mlflow_warning_names_the_fix(monkeypatch):
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://mlflow.example:5000")
    monkeypatch.setitem(sys.modules, "mlflow", None)  # find_spec -> None, import -> ImportError
    message = _telemetry.missing_mlflow_warning()
    assert message is not None
    assert "OKTO_NEURON_MLFLOW_TRACKING_URI is set (http://mlflow.example:5000)" in message
    assert "mlflow is not installed" in message
    assert "OKTO_NEURON_TELEMETRY=1" in message


def test_lazy_path_does_not_repeat_a_reported_missing_mlflow(monkeypatch, caplog):
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://mlflow.example:5000")
    monkeypatch.setitem(sys.modules, "mlflow", None)
    assert _telemetry.missing_mlflow_warning() is not None
    caplog.set_level("DEBUG", logger="okto_neuron.llm.telemetry")
    assert _telemetry._client() is None
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []


def test_lazy_path_names_the_missing_package_not_the_server(monkeypatch, caplog):
    """Without the startup check, the first call used to say "... talking to <uri>
    (... check the server)" with a traceback: it pointed at the wrong thing."""
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://mlflow.example:5000")
    monkeypatch.setitem(sys.modules, "mlflow", None)
    caplog.set_level("WARNING", logger="okto_neuron.llm.telemetry")
    assert _telemetry._client() is None
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "mlflow is not installed" in warnings[0].getMessage()
    assert "talking to" not in warnings[0].getMessage()


def test_cli_warns_once_on_stderr_when_mlflow_is_missing(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from okto_neuron.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(_telemetry.ENV_TRACKING_URI, "http://mlflow.example:5000")
    monkeypatch.setitem(sys.modules, "mlflow", None)
    result = CliRunner().invoke(
        app, ["status", "--endpoint", "http://127.0.0.1:1", "--timeout", "0.2"]
    )
    assert result.stderr.count("mlflow is not installed") == 1, result.stderr
    assert "warning: OKTO_NEURON_MLFLOW_TRACKING_URI is set" in result.stderr
    assert "mlflow is not installed" not in result.stdout


def test_cli_is_quiet_when_telemetry_is_off(monkeypatch, tmp_path):
    from click.testing import CliRunner

    from okto_neuron.cli import app

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv(_telemetry.ENV_TRACKING_URI, raising=False)
    monkeypatch.delenv("MARGINALIA_MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.setitem(sys.modules, "mlflow", None)
    result = CliRunner().invoke(
        app, ["status", "--endpoint", "http://127.0.0.1:1", "--timeout", "0.2"]
    )
    assert "mlflow" not in result.stderr
