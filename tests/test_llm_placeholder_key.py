"""The LLM provider must not fail keyless self-hosted endpoints (regression).

litellm's openai path raises "Missing credentials" when no api_key is present,
which Okto Neuron otherwise swallows into silent zero-extraction. For a custom
api_base (a local/self-hosted server) we inject a placeholder key so keyless
setups work; for a hosted endpoint with no custom api_base we must NOT mask a
genuinely missing key.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from okto_neuron.config._vault import ResolvedLLM
from okto_neuron.llm import _PLACEHOLDER_API_KEY, LiteLLMProvider, Message


def _resolved(*, provider: str, api_base: str, api_key_env: str | None = None) -> ResolvedLLM:
    return ResolvedLLM(
        provider=provider,
        api_base=api_base,
        model="some-model",
        api_key_env=api_key_env,
        max_tokens=64,
        temperature=0.0,
        top_p=1.0,
        top_k=0,
        min_p=0.0,
        presence_penalty=0.0,
        enable_thinking=False,
    )


@pytest.fixture
def fake_litellm(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Install a fake ``litellm`` module that records the completion kwargs."""
    mod = types.ModuleType("litellm")
    call = MagicMock()
    msg = MagicMock()
    msg.content = "ok"
    choice = MagicMock()
    choice.message = msg
    call.return_value = MagicMock(choices=[choice])
    mod.completion = call  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", mod)
    return call


def test_placeholder_key_injected_for_loopback_custom_api_base(
    fake_litellm: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = LiteLLMProvider(_resolved(provider="openai", api_base="http://127.0.0.1:8080/v1"))
    provider.complete([Message("user", "hi")])
    kwargs = fake_litellm.call_args.kwargs
    assert kwargs["api_key"] == _PLACEHOLDER_API_KEY
    assert kwargs["api_base"] == "http://127.0.0.1:8080/v1"


def test_placeholder_key_injected_for_private_lan_custom_api_base(
    fake_litellm: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = LiteLLMProvider(_resolved(provider="openai", api_base="http://192.168.0.9:8080/v1"))
    provider.complete([Message("user", "hi")])
    kwargs = fake_litellm.call_args.kwargs
    assert kwargs["api_key"] == _PLACEHOLDER_API_KEY
    assert kwargs["api_base"] == "http://192.168.0.9:8080/v1"


def test_real_configured_key_is_not_overwritten(
    fake_litellm: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OKTO_NEURON_LLM_KEY", "real-secret")
    provider = LiteLLMProvider(
        _resolved(
            provider="openai",
            api_base="http://192.168.0.9:8080/v1",
            api_key_env="OKTO_NEURON_LLM_KEY",
        )
    )
    provider.complete([Message("user", "hi")])
    kwargs = fake_litellm.call_args.kwargs
    assert kwargs["api_key"] == "real-secret"


def test_no_placeholder_for_hosted_default_endpoint(
    fake_litellm: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hosted provider that does NOT use a custom api_base (here the sentinel
    # default base, and a provider outside the api-base set) must not get a
    # placeholder — a genuinely missing key should still fail loud upstream.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = LiteLLMProvider(_resolved(provider="anthropic", api_base="http://127.0.0.1:8123/v1"))
    provider.complete([Message("user", "hi")])
    kwargs = fake_litellm.call_args.kwargs
    assert "api_key" not in kwargs


def test_no_placeholder_for_explicit_managed_provider_api_base(
    fake_litellm: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = LiteLLMProvider(
        _resolved(provider="anthropic", api_base="https://api.anthropic.com/v1")
    )
    provider.complete([Message("user", "hi")])
    kwargs = fake_litellm.call_args.kwargs
    assert kwargs["api_base"] == "https://api.anthropic.com/v1"
    assert "api_key" not in kwargs


def test_no_placeholder_for_openai_hosted_api_base(
    fake_litellm: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = LiteLLMProvider(_resolved(provider="openai", api_base="https://api.openai.com/v1"))
    provider.complete([Message("user", "hi")])
    kwargs = fake_litellm.call_args.kwargs
    assert kwargs["api_base"] == "https://api.openai.com/v1"
    assert "api_key" not in kwargs


def test_no_placeholder_for_public_custom_api_base(
    fake_litellm: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = LiteLLMProvider(_resolved(provider="openai", api_base="https://8.8.8.8/v1"))
    provider.complete([Message("user", "hi")])
    kwargs = fake_litellm.call_args.kwargs
    assert kwargs["api_base"] == "https://8.8.8.8/v1"
    assert "api_key" not in kwargs
