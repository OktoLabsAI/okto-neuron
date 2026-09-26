"""``ask`` must never turn a provider failure into a silent empty SUCCESS.

Live incident: llama.cpp answered HTTP 500 with an empty body, the block-dump
ask path swallowed the ``LLMProviderError`` into ``text=""`` with no log, no
counter and no trace field, and the calling agent reasonably concluded "the
graph knows nothing" and abandoned the graph.

The graceful-degradation contract is KEPT (``text`` stays ``""``, nothing
raises). What is added is honesty: ``retrieval["synthesis_status"]`` is ALWAYS
present and distinguishes

  * ``"ok"``              — synthesis produced text
  * ``"provider_error"``  — the provider failed (summary in ``provider_error``)
  * ``"empty"``           — the provider answered with nothing

The block path had NO test at all before this file; both subgraph handlers did.
"""

from __future__ import annotations

import logging
import sys
import types
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.companion import (
    AskRetrievalPolicy,
    Companion,
    _provider_error_summary,
)
from okto_neuron.llm import (
    PROVIDER_ERROR_SUMMARY_MAX as _ASK_PROVIDER_ERROR_SUMMARY_MAX,
    PROVIDER_MAX_RETRY_DELAY_SECONDS,
    LLMProviderError,
    Message,
    StubLLM,
    _set_last_call_stats,
    strip_reasoning,
)
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


class _ScriptedProvider:
    """Answers from a queue; raises ``error`` on the calls listed in ``raise_at``."""

    model = "fake/ask-model"
    api_base = "http://127.0.0.1:0/v1"

    def __init__(
        self,
        answers: list[str] | None = None,
        *,
        raise_at: set[int] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._answers = list(answers or [])
        self._raise_at = raise_at or set()
        self._error = error or LLMProviderError("simulated provider down")
        self.calls = 0

    def complete(self, messages: Sequence[Message], **_: object) -> str:
        self.calls += 1
        if self.calls in self._raise_at:
            raise self._error
        return self._answers.pop(0) if self._answers else ""


def _seeded_vault(tmp_path: Path) -> Vault:
    vault = Vault.init(tmp_path / "v")
    note = Path(vault.path) / "note.md"
    note.write_text(
        "# Alpha\n\nAlice founded [[Acme]] in 2019. The budget was 5 million. #funding\n",
        encoding="utf-8",
    )
    Companion(vault, provider=StubLLM()).remember(note)
    return vault


def _enable_subgraph(vault: Vault) -> None:
    (Path(vault.path) / "okto-neuron.yaml").write_text(
        "marginalia_yaml_version: 1\nllm:\n  ask:\n    enable_subgraph: true\n",
        encoding="utf-8",
    )


# ── block path (the handler that had no test) ────────────────────────────────


def test_block_path_provider_error_is_marked_not_silent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    vault = _seeded_vault(tmp_path)
    try:
        provider = _ScriptedProvider(
            raise_at={1},
            error=LLMProviderError("litellm completion failed for openai/llama-3: HTTP 500"),
        )
        with caplog.at_level(logging.WARNING, logger="okto_neuron.companion"):
            answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.hits  # retrieval worked — only synthesis failed
        # contract preserved: nothing raised, text degraded to ""
        assert answer.text == ""
        trace = answer.retrieval
        assert trace["synthesis_status"] == "provider_error"
        assert "HTTP 500" in str(trace["provider_error"])
        # existing keys untouched
        assert trace["path"] in {"block_with_sources", "block_without_sources"}
        assert trace["mode"] == "block"
        # and it is no longer invisible in the logs
        assert any(
            "provider error" in rec.getMessage() and "HTTP 500" in rec.getMessage()
            for rec in caplog.records
        )
    finally:
        vault.close()


def test_block_path_empty_completion_is_distinct_from_provider_error(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    try:
        # The provider SUCCEEDS but yields nothing (llm/__init__.py turns a
        # missing ``content`` into ""; strip_reasoning can reduce an all-<think>
        # reply to ""). Different cause, different fix — different status.
        provider = _ScriptedProvider(["   "])
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.text == ""
        assert answer.retrieval["synthesis_status"] == "empty"
        assert "provider_error" not in answer.retrieval
    finally:
        vault.close()


def test_block_path_all_reasoning_completion_reports_empty(tmp_path: Path) -> None:
    """An all-reasoning completion must land on ``empty``, never ``ok``.

    The dangling-close shape (chat template prefills the opening ``<think>``
    into the prompt) reduces to "" once stripped; the backstop has to classify
    that as an absent answer, not a good one.
    """

    vault = _seeded_vault(tmp_path)
    try:
        raw = "weighing the options, no mention of internal analysis.\n</think>\n  \n"
        # what the real provider returns after its own strip_reasoning pass
        provider = _ScriptedProvider([strip_reasoning(raw)])
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.text == ""
        assert answer.retrieval["synthesis_status"] == "empty"
        assert "provider_error" not in answer.retrieval
    finally:
        vault.close()


def test_block_path_success_sets_status_ok(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    try:
        provider = _ScriptedProvider(["Alice founded Acme in 2019."])
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.text
        # ALWAYS present — a field that only appears on failure is one clients
        # forget to check.
        assert answer.retrieval["synthesis_status"] == "ok"
        assert "provider_error" not in answer.retrieval
    finally:
        vault.close()


# ── transient provider errors: bounded retry (ADR 0039 D5, shared policy) ────


def _transient_error(*, retry_after_s: float | None = None) -> LLMProviderError:
    """The shape the LLM layer raised for the ChatGPT backend's 503.

    Built from a real ``litellm.ServiceUnavailableError`` classified by the
    provider layer's own classifier, so the test depends on the same
    category/retryable decision the daemon makes, not a hand-set flag.
    """
    import litellm

    from okto_neuron.llm import classify_provider_exception

    cause = litellm.ServiceUnavailableError(
        message='ChatgptException - {"detail":"Unable to verify access. Please try again."}',
        llm_provider="chatgpt",
        model="gpt-5.6-luna",
    )
    classification = classify_provider_exception(cause)
    assert classification.category == "unavailable" and classification.retryable
    return LLMProviderError(
        f"litellm completion failed for chatgpt/gpt-5.6-luna: {cause}",
        category=classification.category,
        retry_after_s=retry_after_s,
        retryable=classification.retryable,
        cause=cause,
    )


def test_block_path_transient_error_is_retried_and_recovers(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    vault = _seeded_vault(tmp_path)
    try:
        provider = _ScriptedProvider(
            ["Alice founded Acme in 2019."], raise_at={1}, error=_transient_error()
        )
        with caplog.at_level(logging.WARNING, logger="okto_neuron.companion"):
            answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert provider.calls == 2
        assert answer.text == "Alice founded Acme in 2019."
        assert answer.retrieval["synthesis_status"] == "ok"
        assert "provider_error" not in answer.retrieval
        (retry,) = answer.retrieval["synthesis_retries"]
        assert retry["attempt"] == 1
        assert retry["category"] == "unavailable"
        assert retry["delay_s"] == 0.0
        assert "Please try again" in retry["error"]
        assert any("retrying" in rec.getMessage() for rec in caplog.records)
        assert not any("degraded to empty text" in rec.getMessage() for rec in caplog.records)
    finally:
        vault.close()


def test_block_path_transient_error_on_every_attempt_degrades_as_before(
    tmp_path: Path,
) -> None:
    vault = _seeded_vault(tmp_path)
    try:
        provider = _ScriptedProvider(raise_at={1, 2, 3}, error=_transient_error())
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        # bounded: two attempts total, never a third
        assert provider.calls == 2
        assert answer.text == ""
        assert answer.retrieval["synthesis_status"] == "provider_error"
        assert "Please try again" in answer.retrieval["provider_error"]
        assert len(answer.retrieval["synthesis_retries"]) == 1
    finally:
        vault.close()


def test_block_path_non_retryable_error_is_not_retried(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    try:
        provider = _ScriptedProvider(
            ["never reached"],
            raise_at={1},
            error=LLMProviderError("invalid api key", category="authentication"),
        )
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert provider.calls == 1
        assert answer.text == ""
        assert answer.retrieval["synthesis_status"] == "provider_error"
        assert "synthesis_retries" not in answer.retrieval
    finally:
        vault.close()


def test_transient_retry_honours_capped_retry_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okto_neuron.companion as companion_module

    slept: list[float] = []
    monkeypatch.setattr(companion_module.time, "sleep", slept.append)
    vault = _seeded_vault(tmp_path)
    try:
        provider = _ScriptedProvider(
            ["ok"], raise_at={1}, error=_transient_error(retry_after_s=7.5)
        )
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.retrieval["synthesis_status"] == "ok"
        assert slept == [7.5]
        assert answer.retrieval["synthesis_retries"][0]["retry_after_s"] == 7.5
        slept.clear()
        provider = _ScriptedProvider(
            ["ok"], raise_at={1}, error=_transient_error(retry_after_s=3600.0)
        )
        Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert slept == [PROVIDER_MAX_RETRY_DELAY_SECONDS]
    finally:
        vault.close()


def test_subgraph_path_transient_error_is_retried_and_recovers(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    _enable_subgraph(vault)
    try:
        provider = _ScriptedProvider(
            ["Alice founded Acme in 2019."], raise_at={1}, error=_transient_error()
        )
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert provider.calls == 2
        assert answer.text == "Alice founded Acme in 2019."
        assert answer.retrieval["synthesis_status"] == "ok"
        assert answer.retrieval["path"] != "subgraph_provider_error"
        assert len(answer.retrieval["synthesis_retries"]) == 1
    finally:
        vault.close()


# ── subgraph paths: the existing marker survives, status is added ────────────


def test_subgraph_blend_provider_error_keeps_path_and_adds_status(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    _enable_subgraph(vault)
    try:
        provider = _ScriptedProvider(raise_at={1})
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert provider.calls == 1  # provider down != abstention — still no retry
        assert answer.text == ""
        # the key tests already assert on stays exactly as it was
        assert answer.retrieval["path"] == "subgraph_provider_error"
        assert answer.retrieval["synthesis_status"] == "provider_error"
        assert answer.retrieval["provider_error"]
    finally:
        vault.close()


def test_subgraph_never_policy_provider_error_marks_status(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    _enable_subgraph(vault)
    try:
        provider = _ScriptedProvider(raise_at={1})
        answer = Companion(vault, provider=provider).ask(
            "who founded acme?",
            k=5,
            retrieval_policy=AskRetrievalPolicy(source_block_policy="never"),
        )
        assert answer.text == ""
        assert answer.retrieval["path"] == "subgraph_provider_error"
        assert answer.retrieval["synthesis_status"] == "provider_error"
    finally:
        vault.close()


def test_subgraph_always_policy_provider_error_is_not_reported_as_empty(tmp_path: Path) -> None:
    """The ``always`` leg returns ``"", trace`` with no ``path`` rewrite. Without
    an explicit mark it would fall through to the text-derived backstop and be
    labelled ``"empty"`` — a provider outage reported as "the model said
    nothing", exactly the confusion this fix exists to kill."""
    vault = _seeded_vault(tmp_path)
    _enable_subgraph(vault)
    try:
        provider = _ScriptedProvider(raise_at={1})
        answer = Companion(vault, provider=provider).ask(
            "who founded acme?",
            k=5,
            retrieval_policy=AskRetrievalPolicy(source_block_policy="always"),
        )
        assert answer.text == ""
        assert answer.retrieval["synthesis_status"] == "provider_error"
    finally:
        vault.close()


def test_subgraph_success_sets_status_ok(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    _enable_subgraph(vault)
    try:
        provider = _ScriptedProvider(["Alice founded Acme in 2019."])
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.retrieval["synthesis_status"] == "ok"
    finally:
        vault.close()


# ── the summary must stay safe and bounded ───────────────────────────────────


def test_provider_error_summary_is_bounded_and_single_line() -> None:
    summary = _provider_error_summary(LLMProviderError("boom\n  spread   over\nlines " + "x" * 900))
    assert len(summary) <= _ASK_PROVIDER_ERROR_SUMMARY_MAX
    assert "\n" not in summary
    assert summary.startswith("boom spread over lines")


def test_trace_provider_error_carries_no_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the REAL llm layer so the error is built the way production builds
    it: ``_redact_api_key`` scrubs the credential BEFORE ``LLMProviderError``
    exists, and the companion trace only ever passes that redacted string on."""
    from okto_neuron.config._vault import ResolvedLLM
    from okto_neuron.llm import LiteLLMProvider

    # Assembled at runtime so the release source-tree secret scanner
    # (test_dependency_contract) does not see an OpenAI-shaped literal.
    fake_key = "-".join(("sk", "fake", "DEADBEEF", "0123456789"))
    monkeypatch.setenv("OKTO_NEURON_TEST_FAKE_KEY", fake_key)

    mod = types.ModuleType("litellm")

    def _boom(**kwargs: object) -> object:
        raise RuntimeError(f"upstream rejected request with api_key={fake_key}")

    mod.completion = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", mod)

    real = LiteLLMProvider(
        ResolvedLLM(
            provider="openai",
            api_base="http://127.0.0.1:1/v1",
            model="some-model",
            api_key_env="OKTO_NEURON_TEST_FAKE_KEY",
            max_tokens=64,
            temperature=0.0,
            top_p=1.0,
            top_k=0,
            min_p=0.0,
            presence_penalty=0.0,
            enable_thinking=False,
        )
    )
    with pytest.raises(LLMProviderError) as excinfo:
        real.complete([Message("user", "hi")])
    provider_exc = excinfo.value
    assert fake_key not in str(provider_exc)  # redacted at the source, as designed

    vault = _seeded_vault(tmp_path)
    try:
        provider = _ScriptedProvider(raise_at={1}, error=provider_exc)
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        summary = str(answer.retrieval["provider_error"])
        assert fake_key not in summary
        assert "[redacted]" in summary
        assert len(summary) <= _ASK_PROVIDER_ERROR_SUMMARY_MAX
    finally:
        vault.close()


# ── abnormal finish reasons: any non-"stop" stop is a potential problem ───────


class _FinishReasonProvider(_ScriptedProvider):
    """A scripted provider that ALSO reports a finish reason the way a real
    provider does — through the per-thread ``_set_last_call_stats`` channel the
    companion reads (no change to ``_complete_ask``'s return type)."""

    def __init__(
        self,
        answers: list[str] | None = None,
        *,
        finish_reasons: list[str] | None = None,
        native_finish_reasons: list[str | None] | None = None,
        unmapped: bool = False,
        raise_at: set[int] | None = None,
        error: Exception | None = None,
    ) -> None:
        super().__init__(answers, raise_at=raise_at, error=error)
        self._finish_reasons = list(finish_reasons or [])
        self._native = list(native_finish_reasons or [])
        self._unmapped = unmapped

    def complete(self, messages: Sequence[Message], **kwargs: object) -> str:
        index = self.calls  # 0-based for this call
        text = super().complete(messages, **kwargs)
        stats: dict[str, object] = {}
        if index < len(self._finish_reasons):
            stats["finish_reason"] = self._finish_reasons[index]
        if index < len(self._native) and self._native[index] is not None:
            stats["native_finish_reason"] = self._native[index]
            if self._unmapped:
                stats["finish_reason_unmapped"] = True
        _set_last_call_stats(stats or None)
        return text


def test_block_path_length_finish_reason_reports_truncated(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    try:
        provider = _FinishReasonProvider(
            ["Alice founded Acme in"], finish_reasons=["length"]
        )
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.text  # there IS text — it is just incomplete
        assert answer.retrieval["synthesis_status"] == "truncated"
        assert answer.retrieval["finish_reason"] == "length"
    finally:
        vault.close()


def test_block_path_truncated_beats_empty(tmp_path: Path) -> None:
    """A truncated completion whose text stripped to nothing reports
    ``truncated``, not ``empty``: the cause is known and actionable, while
    ``empty`` would imply the model chose to say nothing."""
    vault = _seeded_vault(tmp_path)
    try:
        provider = _FinishReasonProvider(["   "], finish_reasons=["length"])
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.text == ""
        assert answer.retrieval["synthesis_status"] == "truncated"
    finally:
        vault.close()


def test_block_path_provider_error_beats_truncated(tmp_path: Path) -> None:
    """The failing call never reaches the stamp, and a stale ``length`` from an
    earlier completion must not downgrade ``provider_error``."""
    vault = _seeded_vault(tmp_path)
    try:
        _set_last_call_stats({"finish_reason": "length"})
        provider = _ScriptedProvider(raise_at={1})
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.retrieval["synthesis_status"] == "provider_error"
    finally:
        _set_last_call_stats(None)
        vault.close()


def test_block_path_content_filter_reports_abnormal_stop(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    try:
        provider = _FinishReasonProvider(
            ["Alice foun"], finish_reasons=["content_filter"]
        )
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.retrieval["synthesis_status"] == "abnormal_stop"
        assert answer.retrieval["finish_reason"] == "content_filter"
    finally:
        vault.close()


def test_block_path_unmapped_native_reason_is_not_reported_ok(tmp_path: Path) -> None:
    """litellm's ``map_finish_reason`` defaults an UNMAPPED provider reason to
    ``"stop"`` and preserves the raw value as ``native_finish_reason``. Trusting
    the normalized value alone would report a clean ``ok``."""
    vault = _seeded_vault(tmp_path)
    try:
        provider = _FinishReasonProvider(
            ["Alice founded Acme."],
            finish_reasons=["stop"],
            native_finish_reasons=["server_overloaded"],
            unmapped=True,
        )
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.retrieval["synthesis_status"] == "abnormal_stop"
        assert answer.retrieval["finish_reason"] == "stop"
        assert answer.retrieval["native_finish_reason"] == "server_overloaded"
    finally:
        vault.close()


def test_block_path_clean_stop_is_still_ok(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    try:
        provider = _FinishReasonProvider(
            ["Alice founded Acme in 2019."], finish_reasons=["stop"]
        )
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.retrieval["synthesis_status"] == "ok"
        assert answer.retrieval["finish_reason"] == "stop"
        assert "native_finish_reason" not in answer.retrieval
    finally:
        vault.close()


def test_subgraph_path_length_finish_reason_reports_truncated(tmp_path: Path) -> None:
    vault = _seeded_vault(tmp_path)
    _enable_subgraph(vault)
    try:
        provider = _FinishReasonProvider(
            ["Alice founded Acme in"], finish_reasons=["length"]
        )
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.retrieval["synthesis_status"] == "truncated"
        assert answer.retrieval["finish_reason"] == "length"
    finally:
        vault.close()


def test_stub_provider_reports_no_finish_reason(tmp_path: Path) -> None:
    """A provider that reports nothing must not inherit a neighbouring call's
    reason: ``_complete_ask`` clears the per-thread stats first."""
    vault = _seeded_vault(tmp_path)
    try:
        _set_last_call_stats({"finish_reason": "length"})
        provider = _ScriptedProvider(["Alice founded Acme in 2019."])
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.retrieval["synthesis_status"] == "ok"
        assert "finish_reason" not in answer.retrieval
    finally:
        _set_last_call_stats(None)
        vault.close()


def test_block_path_known_stop_alias_stays_ok(tmp_path: Path) -> None:
    """A native reason litellm KNOWS is a stop alias (``end_turn`` and friends)
    is a clean stop — flagging it would mark every Anthropic/Gemini/Cohere
    answer abnormal."""
    vault = _seeded_vault(tmp_path)
    try:
        provider = _FinishReasonProvider(
            ["Alice founded Acme in 2019."],
            finish_reasons=["stop"],
            native_finish_reasons=["end_turn"],
        )
        answer = Companion(vault, provider=provider).ask("who founded acme?", k=5)
        assert answer.retrieval["synthesis_status"] == "ok"
        assert answer.retrieval["native_finish_reason"] == "end_turn"
    finally:
        vault.close()


# ── no usable LLM (S1): explicit degraded status, and no provider call ───────


def _forbid_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    import okto_neuron.llm as llm_mod

    def _no_call(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("ask built an LLM provider although none is usable")

    monkeypatch.setattr(llm_mod, "get_provider", _no_call)


@pytest.mark.parametrize("subgraph", [False, True])
def test_no_model_configured_reports_no_llm_without_a_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, subgraph: bool
) -> None:
    vault = _seeded_vault(tmp_path)
    try:
        if subgraph:
            _enable_subgraph(vault)
        _forbid_provider(monkeypatch)
        answer = Companion(vault).ask("who founded acme?", k=5)
        assert answer.hits
        assert answer.text == ""
        assert answer.retrieval["synthesis_status"] == "no_llm"
        assert "llm.defaults.model" in answer.retrieval["no_llm_reason"]
        assert "provider_error" not in answer.retrieval
    finally:
        vault.close()


def test_llm_disabled_reports_no_llm_without_a_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _seeded_vault(tmp_path)
    try:
        (Path(vault.path) / "okto-neuron.yaml").write_text(
            "marginalia_yaml_version: 1\nllm:\n  enabled: false\n", encoding="utf-8"
        )
        _forbid_provider(monkeypatch)
        answer = Companion(vault).ask("who founded acme?", k=5)
        assert answer.retrieval["synthesis_status"] == "no_llm"
        assert "llm.enabled is false" in answer.retrieval["no_llm_reason"]
    finally:
        vault.close()


def test_configured_model_still_calls_the_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okto_neuron.llm as llm_mod

    vault = _seeded_vault(tmp_path)
    try:
        (Path(vault.path) / "okto-neuron.yaml").write_text(
            "marginalia_yaml_version: 1\nllm:\n  defaults:\n    model: some-model\n",
            encoding="utf-8",
        )
        built: list[str] = []

        def _fake(resolved: object) -> object:
            built.append(str(getattr(resolved, "model", "")))
            return _ScriptedProvider(["Alice founded Acme."])

        monkeypatch.setattr(llm_mod, "get_provider", _fake)
        answer = Companion(vault).ask("who founded acme?", k=5)
        assert built == ["some-model"]
        assert answer.retrieval["synthesis_status"] == "ok"
    finally:
        vault.close()


@pytest.mark.parametrize(
    ("synthesis_status", "expected"),
    [
        ("ok", "ok"),
        ("no_llm", "degraded"),
        ("provider_error", "degraded"),
        ("truncated", "degraded"),
        ("abnormal_stop", "degraded"),
        ("empty", "degraded"),
        (None, "degraded"),
    ],
)
def test_ask_status_is_ok_only_for_a_clean_answer(
    synthesis_status: str | None, expected: str
) -> None:
    from okto_neuron.companion import ask_status

    assert ask_status({"synthesis_status": synthesis_status}) == expected
