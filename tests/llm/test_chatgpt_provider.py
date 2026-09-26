"""The ``chatgpt`` provider: opt-in gated, narrowed, and honest about cost.

This provider reaches a ChatGPT SUBSCRIPTION through litellm's Responses
transport. litellm advertises the full OpenAI parameter surface for it (because
``ChatGPTConfig`` subclasses ``OpenAIConfig``) while its request-body whitelist
transmits a fraction of it. These tests pin the correction, not litellm's
behaviour.
"""

from __future__ import annotations

import pytest

from okto_neuron.config._vault import ResolvedLLM
from okto_neuron.llm import (
    LLMProviderError,
    Message,
    _CHATGPT_TRANSMITTED_PARAMS,
    get_provider,
    parameter_capabilities,
)
from okto_neuron.llm._chatgpt import (
    CHATGPT_BACKEND,
    ENV_OPT_IN,
    ORIGINATOR,
    ChatGPTProvider,
    pin_provider_env,
)


def _resolved(**overrides) -> ResolvedLLM:
    base = dict(
        provider="chatgpt",
        api_base="http://127.0.0.1:8123/v1",  # the vault default; NOT where calls go
        model="gpt-5.5",
        api_key_env=None,
    )
    base.update(overrides)
    return ResolvedLLM(**base)


@pytest.fixture
def opted_in(monkeypatch):
    monkeypatch.setenv(ENV_OPT_IN, "1")
    monkeypatch.delenv("CHATGPT_ORIGINATOR", raising=False)
    monkeypatch.delenv("CHATGPT_DEFAULT_INSTRUCTIONS", raising=False)


# ── the opt-in gate ────────────────────────────────────────────────────


def test_provider_refuses_to_construct_without_the_opt_in(monkeypatch) -> None:
    monkeypatch.delenv(ENV_OPT_IN, raising=False)
    with pytest.raises(LLMProviderError) as excinfo:
        get_provider(_resolved())
    message = str(excinfo.value)
    assert ENV_OPT_IN in message
    # The refusal must state the three facts that make results from it
    # unpublishable, not merely that a flag is missing.
    assert "response_format" in message
    assert "subscription" in message.lower()
    assert excinfo.value.retryable is False


def test_opt_in_routes_to_the_dedicated_provider(opted_in) -> None:
    provider = get_provider(_resolved())
    assert isinstance(provider, ChatGPTProvider)


@pytest.mark.parametrize("value", ["", "0", "no", "false", "off"])
def test_a_falsey_opt_in_is_still_off(monkeypatch, value) -> None:
    monkeypatch.setenv(ENV_OPT_IN, value)
    with pytest.raises(LLMProviderError):
        get_provider(_resolved())


# ── capability narrowing (the important one) ───────────────────────────


def test_capabilities_are_narrowed_to_what_litellm_actually_transmits() -> None:
    capabilities = parameter_capabilities(provider="chatgpt", model="gpt-5.5")
    assert capabilities.supported_openai_params == _CHATGPT_TRANSMITTED_PARAMS
    assert capabilities.source == "litellm_chatgpt_narrowed"


@pytest.mark.parametrize(
    "param", ["temperature", "top_p", "seed", "response_format", "max_tokens"]
)
def test_narrowing_drops_the_params_litellm_silently_discards(param: str) -> None:
    """Each of these is advertised by ``ChatGPTConfig`` and never transmitted.

    ``response_format`` is the dangerous one: litellm maps it to
    ``request["text"]``, and ``text`` is not on the eleven-key whitelist, so a
    curation step that believes it is in JSON mode would get prose. Okto Neuron
    must not believe it.
    """

    capabilities = parameter_capabilities(provider="chatgpt", model="gpt-5.5")
    assert param not in (capabilities.supported_openai_params or frozenset())


def test_narrowing_is_not_read_off_the_litellm_config() -> None:
    """The plain openai path still advertises the full superset.

    If this ever stops being true the narrowing has become redundant — but
    while it IS true, it is the whole justification for the override.
    """

    openai_caps = parameter_capabilities(provider="openai", model="gpt-4o")
    assert "temperature" in (openai_caps.supported_openai_params or frozenset())
    assert "response_format" in (openai_caps.supported_openai_params or frozenset())


def test_configurable_fields_reflect_the_narrowing() -> None:
    """What the Config UI offers must match what the wire carries."""

    payload = parameter_capabilities(provider="chatgpt", model="gpt-5.5").as_dict()
    assert "temperature" not in payload["supported_fields"]
    assert "max_tokens" not in payload["supported_fields"]


# ── originator, instructions, endpoint ─────────────────────────────────


def test_originator_is_pinned_away_from_litellms_codex_impersonation(monkeypatch) -> None:
    """litellm defaults to ``codex_cli_rs``, claiming to be OpenAI's own CLI."""

    monkeypatch.setenv("CHATGPT_ORIGINATOR", "codex_cli_rs")
    monkeypatch.delenv("CHATGPT_DEFAULT_INSTRUCTIONS", raising=False)
    pinned = pin_provider_env()
    import os

    assert os.environ["CHATGPT_ORIGINATOR"] == ORIGINATOR == "marginalia"
    assert pinned["CHATGPT_ORIGINATOR"] == "marginalia"


def test_default_instructions_replace_the_codex_preamble(monkeypatch) -> None:
    """Unset, litellm prepends ~1.6K tokens of Codex CLI system prompt.

    Measured live: 1638 prompt tokens for a six-word prompt. Left in place,
    ``prompt_tokens`` would be mostly somebody else's prompt while telemetry
    reported it as ours.
    """

    monkeypatch.delenv("CHATGPT_DEFAULT_INSTRUCTIONS", raising=False)
    pinned = pin_provider_env()
    import os

    assert os.environ["CHATGPT_DEFAULT_INSTRUCTIONS"] == pinned[
        "CHATGPT_DEFAULT_INSTRUCTIONS"
    ]
    # Short enough that the operator's own prompt dominates the token count.
    assert len(pinned["CHATGPT_DEFAULT_INSTRUCTIONS"]) < 400


def test_an_operators_own_instructions_are_not_overwritten(monkeypatch) -> None:
    monkeypatch.setenv("CHATGPT_DEFAULT_INSTRUCTIONS", "mine")
    assert pin_provider_env()["CHATGPT_DEFAULT_INSTRUCTIONS"] == "mine"


def test_the_real_endpoint_is_recorded_not_the_vault_default(opted_in) -> None:
    """A span must be able to identify an affected run afterwards.

    ``resolved.api_base`` is the vault's unrelated loopback default, so a trace
    that used it would make a hosted run look local forever.
    """

    provider = get_provider(_resolved())
    assert provider.api_base == CHATGPT_BACKEND
    assert provider.api_base.startswith("https://")


def test_provider_is_kept_off_the_first_run_menu() -> None:
    from okto_neuron.onboarding import MENU_PRESETS, get_provider_preset

    preset = get_provider_preset("chatgpt")
    assert preset.provider == "chatgpt"
    assert preset not in MENU_PRESETS
    assert "EXPLORATION ONLY" in preset.note


def test_models_come_from_litellms_table_not_a_models_endpoint() -> None:
    """``chatgpt`` has no ``/v1/models``; probing one would 404."""

    from okto_neuron.onboarding import (
        CHATGPT_MODELS_REJECTED_BY_SUBSCRIPTION,
        _discover_chatgpt_models,
    )

    result = _discover_chatgpt_models()
    assert result.error is None
    assert result.endpoint == "litellm.model_cost"
    assert "gpt-5.5" in result.models
    # Models this account was observed to refuse are not offered.
    assert not (set(result.models) & CHATGPT_MODELS_REJECTED_BY_SUBSCRIPTION)


def test_every_chatgpt_model_really_has_no_price(opted_in) -> None:
    """The justification for the explicit cost branch, asserted not assumed."""

    import litellm

    priced = [
        key
        for key, info in litellm.model_cost.items()
        if key.startswith("chatgpt/") and info.get("input_cost_per_token") is not None
    ]
    assert priced == []


# ── credential preflight: never let litellm start an OAuth flow ────────


def test_missing_credential_is_refused_before_litellm_can_start_a_login(
    opted_in, monkeypatch, tmp_path
) -> None:
    """Observed live: with no auth file, litellm PRINTS A DEVICE CODE AND BLOCKS.

    It does not raise. In ``okto-neuron serve`` that is a completion that never
    returns; in a benchmark it is a wedged run. So the provider checks first.
    """

    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path / "absent"))
    provider = get_provider(_resolved())
    with pytest.raises(LLMProviderError) as excinfo:
        provider.complete([Message(role="user", content="hi")])
    message = str(excinfo.value)
    assert "device-code" in message
    assert "~/.codex" in message, "the refusal must warn against sharing a credential file"
    assert excinfo.value.category == "authentication"
    assert excinfo.value.retryable is False


def test_a_codex_shaped_auth_file_is_rejected_rather_than_half_read(
    opted_in, monkeypatch, tmp_path
) -> None:
    """codex nests its tokens under ``tokens``; litellm expects them flat."""

    import json

    token_dir = tmp_path / "codexish"
    token_dir.mkdir()
    (token_dir / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": None, "tokens": {"access_token": "x"}})
    )
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(token_dir))
    provider = get_provider(_resolved())
    with pytest.raises(LLMProviderError) as excinfo:
        provider.complete([Message(role="user", content="hi")])
    assert "access_token" in str(excinfo.value)


def test_the_preflight_never_creates_or_touches_the_token_dir(
    opted_in, monkeypatch, tmp_path
) -> None:
    """Constructing litellm's Authenticator would makedirs and rewrite the file."""

    from okto_neuron.llm._chatgpt import assert_credential_present, credential_path

    target = tmp_path / "untouched"
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(target))
    assert credential_path() == str(target / "auth.json")
    with pytest.raises(LLMProviderError):
        assert_credential_present()
    assert not target.exists(), "the preflight must be read-only"
