from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_neuron.config import EmbeddingConfig, LLMConfig, LLMDefaults
from okto_neuron.onboarding import delete_user_env_secret, write_user_env_secret
from okto_neuron.providers import ProviderRegistry, litellm_proxy_models


def test_named_credential_and_provider_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    env_path = home / ".okto-neuron" / "env"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OKTO_NEURON_ENV_FILE", str(env_path))

    registry = ProviderRegistry.load()
    credential = registry.add_credential("Gemini pessoal")
    write_user_env_secret(credential.env_name, "gemini-secret")
    provider = registry.add_provider(
        name="Gemini via proxy",
        driver="litellm_proxy",
        api_base="http://127.0.0.1:4000/v1",
        credential_id=credential.id,
        parameter_mode="safe",
        request_timeout_s=900,
    )
    registry.save()

    loaded = ProviderRegistry.load()
    assert loaded.provider(provider.id).credential_id == credential.id
    assert loaded.provider(provider.id).allow_remote is True
    assert loaded.provider(provider.id).request_timeout_s == 900
    assert loaded.credential_env_for(loaded.provider(provider.id)) == credential.env_name
    assert credential.env_name in env_path.read_text(encoding="utf-8")
    assert "gemini-secret" not in loaded.path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="used by provider"):
        loaded.remove_credential(credential.id)

    loaded.remove_provider(provider.id)
    removed = loaded.remove_credential(credential.id)
    loaded.save()
    delete_user_env_secret(removed.env_name)
    assert removed.env_name not in env_path.read_text(encoding="utf-8")


def test_provider_reference_resolves_llm_and_embedding_connections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    registry = ProviderRegistry.load()
    credential = registry.add_credential("OpenAI main")
    provider = registry.add_provider(
        name="OpenAI production",
        driver="openai",
        api_base="https://api.openai.com/v1",
        credential_id=credential.id,
        parameter_mode="safe",
        request_timeout_s=900,
    )
    registry.save()

    llm = LLMConfig(
        allow_remote=False,
        defaults=LLMDefaults(provider_ref=provider.id, model="gpt-test"),
    )
    resolved = llm.resolved_defaults()
    assert resolved.provider == "openai"
    assert resolved.api_base == "https://api.openai.com/v1"
    assert resolved.api_key_env == credential.env_name
    assert resolved.parameter_mode == "safe"
    assert resolved.request_timeout_s == 900

    embedding = EmbeddingConfig(
        provider_ref=provider.id,
        model="text-embedding-test",
        dimension=8,
    ).resolved_provider()
    assert embedding.provider == "openai"
    assert embedding.api_base == "https://api.openai.com/v1"
    assert embedding.api_key_env == credential.env_name
    assert embedding.allow_remote is True


def test_provider_remote_permission_is_explicit_and_defaults_on(tmp_path: Path) -> None:
    registry = ProviderRegistry.load(tmp_path / "providers.yaml")
    remote = registry.add_provider(
        name="Private proxy",
        driver="litellm_proxy",
        api_base="http://192.0.2.10:4000",
        credential_id=None,
        parameter_mode="safe",
    )
    assert remote.allow_remote is True

    with pytest.raises(ValueError, match="not loopback"):
        registry.add_provider(
            name="Blocked private proxy",
            driver="litellm_proxy",
            api_base="http://192.0.2.10:4000",
            allow_remote=False,
            credential_id=None,
            parameter_mode="safe",
        )


def test_litellm_proxy_rejects_local_extended_mode(tmp_path: Path) -> None:
    registry = ProviderRegistry.load(tmp_path / "providers.yaml")

    with pytest.raises(ValueError, match="direct local inference"):
        registry.add_provider(
            name="Unsafe proxy",
            driver="litellm_proxy",
            api_base="http://127.0.0.1:4000/v1",
            credential_id=None,
            parameter_mode="local_extended",
        )


def test_litellm_proxy_supports_llm_and_embedding_use(tmp_path: Path) -> None:
    registry = ProviderRegistry.load(tmp_path / "providers.yaml")

    assert registry.provider_uses("litellm_proxy") == ["llm", "embedding"]


def test_litellm_proxy_model_catalog_preserves_mode_and_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_module = pytest.importorskip("litellm.proxy.client")
    calls: list[dict[str, object]] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)
            self.http = SimpleNamespace(
                request=lambda *args, **kwargs: {
                    "data": [
                        {
                            "model_group": "chat-alias",
                            "mode": "chat",
                            "supported_openai_params": ["temperature"],
                        },
                        {
                            "model_group": "embedding-alias",
                            "mode": "embedding",
                            "supported_openai_params": None,
                        },
                    ]
                }
            )

    monkeypatch.setattr(client_module, "Client", FakeClient)
    monkeypatch.setenv("OKTO_NEURON_TEST_PROXY_KEY", "secret")

    models = litellm_proxy_models(
        api_base="http://127.0.0.1:41991/v1",
        api_key_env="OKTO_NEURON_TEST_PROXY_KEY",
    )

    assert [(model.id, model.mode) for model in models] == [
        ("chat-alias", "chat"),
        ("embedding-alias", "embedding"),
    ]
    assert models[0].supported_openai_params == frozenset({"temperature"})
    assert calls[0]["base_url"] == "http://127.0.0.1:41991"
    assert calls[0]["api_key"] == "secret"


def test_resolve_openai_base_all_input_forms_yield_identical_values() -> None:
    """The user's exact repro base in every input form must derive the same
    two values: discovery at {root}/v1/models and the completion base
    {root}/v1 (idempotent normalization)."""
    from okto_neuron.providers import resolve_openai_base

    forms = (
        "http://llama-swap.example.com",
        "http://llama-swap.example.com/",
        "http://llama-swap.example.com/v1",
        "http://llama-swap.example.com/v1/",
    )
    first = resolve_openai_base(forms[0])
    assert first.root == "http://llama-swap.example.com"
    assert first.models_url == "http://llama-swap.example.com/v1/models"
    assert first.api_base == "http://llama-swap.example.com/v1"
    for form in forms[1:]:
        assert resolve_openai_base(form) == first, form
    # Applying the resolver to its own derived base is a no-op.
    assert resolve_openai_base(first.api_base) == first


def test_resolve_openai_base_subpath_variant() -> None:
    """A /v1 under a subpath (e.g. a reverse proxy) resolves to the subpath
    root; the bare root form resolves to the same derived values."""
    from okto_neuron.providers import resolve_openai_base

    assert resolve_openai_base("http://host:9000/proxy") == resolve_openai_base(
        "http://host:9000/proxy/v1"
    ) == resolve_openai_base(
        "http://host:9000/proxy/v1/"
    )
    resolved = resolve_openai_base("http://host:9000/proxy")
    assert resolved.root == "http://host:9000/proxy"
    assert resolved.models_url == "http://host:9000/proxy/v1/models"
    assert resolved.api_base == "http://host:9000/proxy/v1"
    # A /v1 that is only a prefix of a longer path segment is not a version
    # suffix and must not be stripped.
    assert resolve_openai_base("http://host:9000/v1x").api_base == "http://host:9000/v1x/v1"


def test_resolve_openai_base_keeps_an_explicit_non_v1_version_segment() -> None:
    """Z.ai's OpenAI-compatible coding endpoint is versioned ``/v4``; its chat
    route is ``/v4/chat/completions``. Appending ``/v1`` there 404s."""
    from okto_neuron.providers import resolve_openai_base

    resolved = resolve_openai_base("https://api.z.ai/api/coding/paas/v4")
    assert resolved.root == "https://api.z.ai/api/coding/paas"
    assert resolved.models_url == "https://api.z.ai/api/coding/paas/v4/models"
    assert resolved.api_base == "https://api.z.ai/api/coding/paas/v4"
    # Idempotent, trailing slash tolerated, and /v1 behaviour unchanged.
    assert resolve_openai_base("https://api.z.ai/api/coding/paas/v4/") == resolved
    assert resolve_openai_base(resolved.api_base) == resolved
    assert resolve_openai_base("http://host/v1").api_base == "http://host/v1"


def test_resolve_openai_base_rejects_unusable_input() -> None:
    from okto_neuron.providers import resolve_openai_base

    with pytest.raises(ValueError, match="unresolvable api_base"):
        resolve_openai_base("not-a-url")
    with pytest.raises(ValueError, match="unresolvable api_base"):
        resolve_openai_base("ftp://host:21/v1")


def test_resolved_llm_canonicalizes_openai_compat_base() -> None:
    """Runtime choke point: root-form and /v1-form configs must resolve to
    the same completion base, and non-OpenAI drivers are left untouched."""
    from okto_neuron.config._vault import ResolvedLLM

    def resolved(provider: str, api_base: str) -> str:
        return ResolvedLLM(
            provider=provider, api_base=api_base, model="m", api_key_env=None
        ).api_base

    assert resolved("custom_openai", "http://llama-swap.example.com") == (
        resolved("custom_openai", "http://llama-swap.example.com/v1/")
    )
    assert resolved("openai_like", "http://llama-swap.example.com") == "http://llama-swap.example.com/v1"
    assert resolved("openai", "http://127.0.0.1:1234/v1") == "http://127.0.0.1:1234/v1"
    # Non-OpenAI-contract drivers keep their base exactly as configured.
    assert resolved("anthropic", "http://mirror.example") == "http://mirror.example"
    assert resolved("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/") == (
        "https://generativelanguage.googleapis.com/v1beta/openai/"
    )


def test_llm_defaults_and_step_canonicalize_openai_compat_base() -> None:
    """Write-time canonicalization: the saved config is the same base the
    runtime uses, for every step shape (explicit provider, inherited
    provider, other drivers untouched)."""
    from okto_neuron.config._vault import LLMConfig, LLMDefaults, StepLLM

    defaults = LLMDefaults(provider="openai", api_base="http://127.0.0.1:9999")
    assert defaults.api_base == "http://127.0.0.1:9999/v1"

    step_explicit = StepLLM(provider="custom_openai", api_base="http://127.0.0.1:9999/")
    assert step_explicit.api_base == "http://127.0.0.1:9999/v1"

    # A step without its own driver is canonicalized at merge time, when the
    # concrete provider is known.
    step_inherited = StepLLM(api_base="http://127.0.0.1:9999")
    assert step_inherited.api_base == "http://127.0.0.1:9999"

    cfg = LLMConfig(
        allow_remote=True,
        defaults=LLMDefaults(provider="openai", api_base="http://127.0.0.1:9999"),
        ask=StepLLM(api_base="http://127.0.0.1:9999/v1"),
    )
    # A step that inherits the defaults' provider is canonicalized at merge
    # time, when the concrete provider is known.
    assert cfg.resolved("ask").api_base == "http://127.0.0.1:9999/v1"
    assert cfg.resolved_defaults().api_base == "http://127.0.0.1:9999/v1"

    other = LLMDefaults(provider="anthropic", api_base="http://127.0.0.1:9999")
    assert other.api_base == "http://127.0.0.1:9999"


def test_registry_add_and_update_provider_canonicalize_openai_compat_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The named-provider save path persists the canonical base for
    OpenAI-compatible drivers and leaves other drivers verbatim."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    registry = ProviderRegistry.load()
    added = registry.add_provider(
        name="llama-swap",
        driver="custom_openai",
        api_base="http://127.0.0.1:9999",
        credential_id=None,
        parameter_mode="safe",
    )
    assert added.api_base == "http://127.0.0.1:9999/v1"
    registry.save()

    loaded = ProviderRegistry.load()
    updated = loaded.update_provider(added.id, {"api_base": "http://127.0.0.1:9999"})
    assert updated.api_base == "http://127.0.0.1:9999/v1"
    loaded.save()
    assert ProviderRegistry.load().provider(added.id).api_base == "http://127.0.0.1:9999/v1"

    anthropic = loaded.add_provider(
        name="anthropic loopback mirror",
        driver="anthropic",
        api_base="http://127.0.0.1:9999",
        credential_id=None,
        parameter_mode="safe",
    )
    # A non-OpenAI-contract driver keeps its base exactly as entered.
    assert anthropic.api_base == "http://127.0.0.1:9999"
