"""S2 regression tests: the sampler surface must actually reach the model.

Two independent defects let extraction and curation run against the model host
with NO output cap and at the host's default temperature:

1. Config declares ``max_tokens``/``temperature`` as optional, so an
   unconfigured vault resolved them to ``None`` — and every daemon call site
   passed that ``None`` *explicitly* into the client constructor, overriding the
   documented class defaults (16000 / 2000 / 4000). A ``None`` cap is then
   dropped before the request, so nothing bounded a degenerate decode loop; two
   calls ran to the host's own 32768-token ceiling.
2. The unknown-gateway-alias narrowing kept only the output cap, so a configured
   ``temperature`` never reached the model.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

import pytest

from okto_neuron.config._vault import LLMConfig, LLMDefaults, ResolvedLLM
from okto_neuron.llm import LiteLLMProvider, Message, sampler_overrides


def _resolved(**overrides) -> ResolvedLLM:
    base = {
        "provider": "openai",
        "api_base": "http://127.0.0.1:8123/v1",
        "model": "test-model",
        "api_key_env": None,
    }
    base.update(overrides)
    return ResolvedLLM(**base)


# --------------------------------------------------------------------------
# B1 — an unset config value must not override a class default
# --------------------------------------------------------------------------


def test_sampler_overrides_omits_unset_values() -> None:
    assert sampler_overrides(_resolved()) == {}


def test_sampler_overrides_passes_configured_values_through() -> None:
    assert sampler_overrides(_resolved(max_tokens=1234, temperature=0.3)) == {
        "max_tokens": 1234,
        "temperature": 0.3,
    }


def _stub_provider() -> SimpleNamespace:
    return SimpleNamespace(model="test-model")


def test_none_config_leaves_extractor_class_default() -> None:
    from okto_neuron.extract import LLMExtractor

    extractor = LLMExtractor(_stub_provider(), **sampler_overrides(_resolved()))
    assert extractor._max_tokens == 16000
    assert extractor._temperature == 0.0


@pytest.mark.parametrize(
    ("factory_path", "expected_max_tokens"),
    [
        ("okto_neuron.curator:LLMRelationCurator", 2000),
        ("okto_neuron.curator:LLMCandidateCurator", 2000),
        ("okto_neuron.resolve:LLMMergeJudge", 2000),
        ("okto_neuron.predicates.judge:LLMPredicateJudge", 2000),
        ("okto_neuron.reconcile.type_adjudication:LLMTypeAdjudicator", 4000),
    ],
)
def test_none_config_leaves_client_class_defaults(
    factory_path: str, expected_max_tokens: int
) -> None:
    module_name, _, attr = factory_path.partition(":")
    module = __import__(module_name, fromlist=[attr])
    client = getattr(module, attr)(_stub_provider(), **sampler_overrides(_resolved()))
    assert client._max_tokens == expected_max_tokens
    assert client._temperature is not None


def test_companion_build_extractor_uses_class_default_when_config_unset(
    tmp_path,
) -> None:
    """The full daemon seam: config -> resolved() -> constructor.

    This is the site that regressed. Constructing ``LLMExtractor`` directly with
    no kwargs would pass on the broken code too, so it is exercised through
    ``Companion._build_extractor``.
    """
    from okto_neuron.companion import Companion

    cfg = SimpleNamespace(
        llm=LLMConfig(defaults=LLMDefaults()),
        packs=("core",),
    )
    assert cfg.llm.resolved("extraction").max_tokens is None

    companion = Companion.__new__(Companion)
    companion._vault_config = lambda: cfg  # type: ignore[method-assign]
    extractor = companion._build_extractor(provider=_stub_provider())
    assert extractor._max_tokens == 16000


# --------------------------------------------------------------------------
# B2/B4 — the unknown-gateway narrowing keeps the cap AND temperature
# --------------------------------------------------------------------------


def _gateway_provider(monkeypatch: pytest.MonkeyPatch, calls: list[dict]):
    def completion(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(completion=completion))
    # No per-alias metadata for this alias -> capabilities.known is False and the
    # narrowing branch runs.
    monkeypatch.setattr(
        "okto_neuron.llm._litellm_gateway_model_capabilities",
        lambda **kwargs: {},
    )
    return LiteLLMProvider(
        _resolved(
            provider="litellm_proxy",
            model="unknown-alias",
            api_base="http://127.0.0.1:4000",
        )
    )


def test_gateway_narrowing_still_forwards_max_tokens_and_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []
    provider = _gateway_provider(monkeypatch, calls)

    assert (
        provider.complete(
            [Message("user", "hello")],
            max_tokens=16000,
            temperature=0.0,
            top_k=20,
            enable_thinking=False,
        )
        == "ok"
    )

    assert calls[0]["max_tokens"] == 16000
    assert calls[0]["temperature"] == 0.0
    # Vendor-specific samplers stay narrowed out (thinking control is deferred).
    assert "top_k" not in calls[0]
    assert "thinking" not in calls[0]


def test_gateway_narrowing_warns_once_for_dropped_configured_params(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import okto_neuron.llm as llm_module

    monkeypatch.setattr(llm_module, "_GATEWAY_DROP_WARNED", set())
    calls: list[dict] = []
    provider = _gateway_provider(monkeypatch, calls)

    with caplog.at_level(logging.WARNING, logger="okto_neuron.llm"):
        for _ in range(3):
            provider.complete(
                [Message("user", "hello")],
                max_tokens=16000,
                temperature=0.0,
                top_k=20,
            )

    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "top_k" in r.getMessage()
    ]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "unknown-alias" in warnings[0].getMessage()
