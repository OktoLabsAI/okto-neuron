from __future__ import annotations

from pathlib import Path
import re
import tomllib
import warnings

from packaging.requirements import Requirement
from pydantic import ValidationError
import pytest
import yaml

import okto_neuron
from okto_neuron.config import (
    ConsolidationConfig,
    EmbeddingConfig,
    IngestConfig,
    LLMConfig,
    LLMDefaults,
    OktoNeuronConfig,
    ResolvedLLM,
    StepLLM,
    VaultConfig,
)
from okto_neuron.config._vault import _EMBEDDING_PROVIDERS, _LLM_PROVIDERS
from okto_neuron.errors import ConfigNotFound, ConfigParseError, ConfigVersionUnsupported
from okto_neuron.onboarding import MANAGED_EMBEDDING_API_KEY_PROVIDERS

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_toml(path: Path, sentinel: str, vault_root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "marginalia_toml_version = 1",
                f'sentinel = "{sentinel}"',
                f'vault_roots = ["{vault_root}"]',
                "",
            ]
        ),
        encoding="utf-8",
    )


def _dependency_names(dependencies: list[str]) -> set[str]:
    return {Requirement(dependency).name.lower() for dependency in dependencies}


def _lock_dependency_names(dependencies: list[dict[str, object]]) -> set[str]:
    return {str(dependency["name"]).lower() for dependency in dependencies}


def test_marginalia_config_defaults_when_no_config_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    cfg = OktoNeuronConfig.load()

    assert cfg.marginalia_toml_version == 1
    assert cfg.vault_roots == [(home / ".okto-neuron" / "vaults").resolve()]
    assert cfg.strict_acl is False
    assert cfg.default_directory_mode == 0o755


def test_marginalia_config_load_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    home_config = home / ".okto-neuron" / "okto-neuron.toml"
    env_config = tmp_path / "env.toml"
    explicit_config = tmp_path / "explicit.toml"

    _write_toml(home_config, "home", tmp_path / "home-vaults")
    _write_toml(env_config, "env", tmp_path / "env-vaults")
    _write_toml(explicit_config, "explicit", tmp_path / "explicit-vaults")

    assert OktoNeuronConfig.load().sentinel == "home"

    monkeypatch.setenv("OKTO_NEURON_CONFIG", str(env_config))
    assert OktoNeuronConfig.load().sentinel == "env"
    assert OktoNeuronConfig.load(explicit_config).sentinel == "explicit"


def test_marginalia_config_wraps_toml_parse_errors(tmp_path: Path) -> None:
    config_path = tmp_path / "okto-neuron.toml"
    config_path.write_text("marginalia_toml_version = 1\nbad = \n", encoding="utf-8")

    with pytest.raises(ConfigParseError) as raised:
        OktoNeuronConfig.load(config_path)

    error = raised.value
    assert error.file_path == config_path.resolve()
    assert error.line == 2
    assert isinstance(error.__cause__, tomllib.TOMLDecodeError)


def test_bedrock_extra_closes_litellm_and_aws_dependencies() -> None:
    pyproject = tomllib.loads((_PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    optional = pyproject["project"]["optional-dependencies"]
    groups = pyproject["dependency-groups"]
    aws_sdk_packages = {"boto3", "botocore"}

    assert _dependency_names(optional["bedrock"]) == {"boto3", "litellm"}
    assert _dependency_names(groups["bedrock"]) == {"boto3", "litellm"}

    non_bedrock_sets = {
        "base": pyproject["project"]["dependencies"],
        "optional-litellm": optional["litellm"],
        "optional-dev": optional["dev"],
        "group-serve": groups["serve"],
        "group-litellm": groups["litellm"],
        "group-dev": groups["dev"],
    }
    for label, dependencies in non_bedrock_sets.items():
        assert aws_sdk_packages.isdisjoint(_dependency_names(dependencies)), label


def test_uv_lock_bedrock_metadata_matches_pyproject() -> None:
    lock = tomllib.loads((_PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    project_pkg = next(package for package in lock["package"] if package["name"] == "okto-neuron")
    aws_sdk_packages = {"boto3", "botocore"}

    assert _lock_dependency_names(project_pkg["optional-dependencies"]["bedrock"]) == {
        "boto3",
        "litellm",
    }
    assert _lock_dependency_names(project_pkg["dev-dependencies"]["bedrock"]) == {
        "boto3",
        "litellm",
    }
    assert "bedrock" in project_pkg["metadata"]["provides-extras"]
    assert any(
        requirement["name"] == "boto3" and requirement.get("marker") == "extra == 'bedrock'"
        for requirement in project_pkg["metadata"]["requires-dist"]
    )
    assert any(
        requirement["name"] == "litellm" and requirement.get("marker") == "extra == 'bedrock'"
        for requirement in project_pkg["metadata"]["requires-dist"]
    )

    non_bedrock_sets = {
        "base": project_pkg["dependencies"],
        "optional-litellm": project_pkg["optional-dependencies"]["litellm"],
        "metadata-requires-dev-litellm": project_pkg["metadata"]["requires-dev"]["litellm"],
        "group-serve": project_pkg["dev-dependencies"]["serve"],
        "group-litellm": project_pkg["dev-dependencies"]["litellm"],
        "group-dev": project_pkg["dev-dependencies"]["dev"],
    }
    for label, dependencies in non_bedrock_sets.items():
        assert aws_sdk_packages.isdisjoint(_lock_dependency_names(dependencies)), label


def test_marginalia_config_unknown_version_raises_typed_error(tmp_path: Path) -> None:
    config_path = tmp_path / "okto-neuron.toml"
    config_path.write_text("marginalia_toml_version = 99\n", encoding="utf-8")

    with pytest.raises(ConfigVersionUnsupported) as raised:
        OktoNeuronConfig.load(config_path)

    assert raised.value.file_path == config_path.resolve()
    assert raised.value.found_version == 99
    assert raised.value.supported_versions == (1,)


def test_vault_config_default_is_v1_and_federation_opt_out() -> None:
    cfg = VaultConfig.default()

    assert cfg.marginalia_yaml_version == 1
    assert cfg.federation_opt_in is False
    assert cfg.llm.defaults.provider == "openai"


def test_llm_default_model_is_empty_discovery_first() -> None:
    """0.0.48 companion item: the baseline model default must stay empty.

    A hardcoded model name is the staleness defect being removed — defaults
    may never claim a model the configured endpoint may not serve. Onboarding
    and the UI start from model discovery against the chosen endpoint and
    fall back to manual entry only when discovery returns nothing.
    """
    assert LLMDefaults().model == ""
    assert LLMConfig().defaults.model == ""
    assert VaultConfig.default().llm.defaults.model == ""
    assert VaultConfig.default().llm.resolved_defaults().model == ""
    assert "model" in LLMDefaults().model_dump(mode="json")


def test_extraction_max_concurrent_defaults_to_one_and_accepts_above_eight() -> None:
    default = LLMConfig()
    assert default.extraction.max_concurrent is None
    assert (default.extraction.max_concurrent or 1) == 1

    configured = LLMConfig.model_validate({"extraction": {"max_concurrent": 16}})
    assert configured.extraction.max_concurrent == 16
    assert (
        LLMConfig.model_validate({"extraction": {"max_concurrent": 32}}).extraction.max_concurrent
        == 32
    )

    for invalid in (0, 33):
        with pytest.raises(ValidationError):
            LLMConfig.model_validate({"extraction": {"max_concurrent": invalid}})


def test_embedding_batch_execution_defaults_bounds_and_vector_neutrality() -> None:
    default = EmbeddingConfig()
    assert default.batch_size == 32
    assert default.max_concurrent_batches == 1

    configured = EmbeddingConfig(batch_size=128, max_concurrent_batches=16)
    assert configured.batch_size == 128
    assert configured.max_concurrent_batches == 16

    for payload in (
        {"batch_size": 0},
        {"batch_size": 257},
        {"max_concurrent_batches": 0},
        {"max_concurrent_batches": 33},
    ):
        with pytest.raises(ValidationError):
            EmbeddingConfig.model_validate(payload)

    assert "embedding.batch_size" not in VaultConfig.REEMBED_FIELDS
    assert "embedding.max_concurrent_batches" not in VaultConfig.REEMBED_FIELDS


def test_ingest_chunking_defaults_and_overlap_bounds() -> None:
    default = IngestConfig()
    assert default.chunk_size_bytes == 6_000
    assert default.chunk_overlap_bytes == 0

    configured = IngestConfig(chunk_size_bytes=32_000, chunk_overlap_bytes=4_000)
    assert configured.chunk_size_bytes == 32_000
    assert configured.chunk_overlap_bytes == 4_000

    for payload in (
        {"chunk_size_bytes": 255},
        {"chunk_size_bytes": 1_000_001},
        {"chunk_overlap_bytes": -1},
        {"chunk_size_bytes": 1024, "chunk_overlap_bytes": 1024},
        {"chunk_size_bytes": 1024, "chunk_overlap_bytes": 2048},
    ):
        with pytest.raises(ValidationError):
            IngestConfig.model_validate(payload)


def test_vault_config_loads_defaults_and_warns_once_for_missing_version(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    (vault_path / "okto-neuron.yaml").write_text(
        "vault_id: local\npacks:\n  - core\n",
        encoding="utf-8",
    )

    with pytest.warns(UserWarning, match="missing marginalia_yaml_version"):
        cfg = VaultConfig.load(vault_path)

    assert cfg.marginalia_yaml_version == 1
    assert cfg.federation_opt_in is False
    assert cfg.packs == ["core"]

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        VaultConfig.load(vault_path)

    assert caught == []


def test_vault_config_loads_versioned_yaml_file_path(tmp_path: Path) -> None:
    config_path = tmp_path / "okto-neuron.yaml"
    config_path.write_text(
        "\n".join(
            [
                "marginalia_yaml_version: 1",
                "vault_id: configured",
                "federation_opt_in: true",
                "packs:",
                "  - core",
                "embedding:",
                "  provider: stub",
                "  model: stub",
                "",
            ]
        ),
        encoding="utf-8",
    )

    cfg = VaultConfig.load(config_path)

    assert cfg.vault_id == "configured"
    assert cfg.federation_opt_in is True
    assert cfg.embedding.provider == "stub"
    assert cfg.embedding.model == "stub"


def test_application_defaults_are_inherited_only_by_opted_in_vaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    defaults, changed = VaultConfig.apply_application_defaults_patch(
        {
            "embedding": {
                "provider": "stub",
                "model": "application-model",
                "dimension": 64,
            }
        }
    )
    assert defaults.embedding.model == "application-model"
    assert "embedding.model" in changed

    inherited = tmp_path / "inherited"
    inherited.mkdir()
    (inherited / "okto-neuron.yaml").write_text(
        "marginalia_yaml_version: 1\ninherits_application_defaults: true\n",
        encoding="utf-8",
    )
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    (standalone / "okto-neuron.yaml").write_text(
        "marginalia_yaml_version: 1\n",
        encoding="utf-8",
    )

    assert VaultConfig.load(inherited).embedding.model == "application-model"
    assert VaultConfig.load(inherited).embedding.dimension == 64
    assert VaultConfig.load(standalone).embedding.model != "application-model"


def test_inheriting_vault_patch_persists_only_explicit_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    VaultConfig.apply_application_defaults_patch(
        {"embedding": {"provider": "stub", "model": "baseline", "dimension": 64}}
    )
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "okto-neuron.yaml").write_text(
        "marginalia_yaml_version: 1\ninherits_application_defaults: true\n",
        encoding="utf-8",
    )

    updated, changed = VaultConfig.apply_patch(vault, {"embedding": {"model": "vault-model"}})

    raw = VaultConfig.load_raw(vault)
    assert raw["embedding"] == {"model": "vault-model"}
    assert updated.embedding.provider == "stub"
    assert updated.embedding.dimension == 64
    assert changed == ["embedding.model"]


def test_llm_provider_aliases_canonicalize_to_litellm_prefixes() -> None:
    assert LLMDefaults(provider="openai-compat").provider == "openai"
    assert StepLLM(provider="omlx").provider == "openai"
    assert StepLLM(provider="local").provider == "openai"
    assert StepLLM(provider="fireworks_ai").provider == "fireworks_ai"
    assert StepLLM(provider="vertex_ai_beta").provider == "vertex_ai_beta"
    assert (
        ResolvedLLM(
            provider="together",
            api_base="http://127.0.0.1:8123/v1",
            model="meta-llama/Llama-3-8b",
            api_key_env=None,
            max_tokens=1024,
            temperature=0.7,
            top_p=0.95,
            top_k=20,
            min_p=0.0,
            presence_penalty=0.0,
            enable_thinking=False,
        ).provider
        == "together_ai"
    )


def test_embedding_provider_aliases_canonicalize_to_litellm_prefixes() -> None:
    assert EmbeddingConfig(provider="default").provider == "fastembed"
    assert EmbeddingConfig(provider="openai-compat").provider == "openai"
    assert EmbeddingConfig(provider="local").provider == "openai"
    assert EmbeddingConfig(provider="voyage").provider == "voyage"
    assert EmbeddingConfig(provider="vertex_ai").provider == "vertex_ai"


def test_llm_provider_allowlist_covers_installed_litellm_registry() -> None:
    litellm = pytest.importorskip("litellm")

    assert set(litellm.LITELLM_CHAT_PROVIDERS) <= _LLM_PROVIDERS


def test_embedding_provider_allowlist_covers_installed_litellm_registry() -> None:
    litellm = pytest.importorskip("litellm")
    unsupported_prefixes = {
        # LiteLLM metadata buckets, not prefixes accepted by the provider resolver.
        "fireworks_ai-embedding-models",
        "vertex_ai-embedding-models",
        "vertex_ai-language-models",
    }
    providers = {
        str(meta.get("litellm_provider") or meta.get("custom_llm_provider"))
        for meta in litellm.model_cost.values()
        if isinstance(meta, dict)
        and str(meta.get("mode")) == "embedding"
        and (meta.get("litellm_provider") or meta.get("custom_llm_provider"))
    }

    assert providers - unsupported_prefixes <= _EMBEDDING_PROVIDERS


def test_embedding_provider_ui_matches_backend_contract() -> None:
    source = (_PROJECT_ROOT / "frontend/src/components/config/ConfigPanel.tsx").read_text(
        encoding="utf-8"
    )
    provider_block = source.split("const EMBEDDING_PROVIDERS = [", 1)[1].split("\n]", 1)[0]
    managed_block = source.split("const MANAGED_EMBEDDING_API_KEY_PROVIDERS = new Set([", 1)[
        1
    ].split("\n])", 1)[0]

    ui_providers = set(re.findall(r"value: '([^']+)'", provider_block))
    ui_managed_providers = set(re.findall(r"^\s*'([^']+)',", managed_block, re.MULTILINE))

    assert ui_providers == _EMBEDDING_PROVIDERS
    assert ui_managed_providers == MANAGED_EMBEDDING_API_KEY_PROVIDERS


def test_llm_resolved_uses_canonical_provider() -> None:
    cfg = LLMConfig(defaults=LLMDefaults(provider="openai-compat"))

    assert cfg.defaults.provider == "openai"
    assert cfg.resolved("extraction").provider == "openai"
    assert cfg.resolved("curator").provider == "openai"
    assert cfg.resolved("relation_curator").provider == "openai"


def test_llm_resolved_parameters_are_empty_until_explicitly_configured() -> None:
    """``ResolvedLLM.parameters`` (the only surviving ``parameters`` map —
    decision A, 2026-09-15 removed the free-form ``LLMDefaults``/``StepLLM``
    field entirely, no migration) stays empty until one of the three
    parameters-only typed fields (repeat_penalty/reasoning_effort/
    preserve_thinking) is set."""
    cfg = LLMConfig()

    assert cfg.defaults.max_tokens is None
    assert cfg.defaults.temperature is None
    assert cfg.resolved("extraction").parameters == {}


def test_resolved_llm_parameters_still_rejects_request_owned_fields() -> None:
    """The free-form ``LLMDefaults``/``StepLLM.parameters`` map is gone, but
    ``ResolvedLLM.parameters`` stays — it is still the fold target for
    repeat_penalty/reasoning_effort/preserve_thinking, and is still directly
    settable by the ``/llm/test-completion`` REST probe's request body — so
    ``MANAGED_LLM_PARAMETERS``/``_check_llm_parameters`` must still guard it."""
    with pytest.raises(ValidationError, match="managed by Okto Neuron"):
        ResolvedLLM(
            provider="openai",
            api_base="http://127.0.0.1:8123/v1",
            model="m",
            api_key_env=None,
            parameters={"api_key": "must-not-be-stored"},
        )


def test_llm_defaults_default_unset_repeat_penalty_reasoning_fields() -> None:
    """B1 backward-compat: an existing vault config with none of the three new
    fields set must resolve exactly as before — no new keys anywhere."""
    cfg = LLMConfig()

    assert cfg.defaults.repeat_penalty is None
    assert cfg.defaults.reasoning_effort is None
    assert cfg.defaults.preserve_thinking is None
    assert cfg.extraction.repeat_penalty is None
    resolved = cfg.resolved("extraction")
    assert resolved.parameters == {}
    assert not hasattr(resolved, "repeat_penalty")
    assert not hasattr(resolved, "reasoning_effort")
    assert not hasattr(resolved, "preserve_thinking")


def test_llm_defaults_accepts_repeat_penalty_reasoning_effort_preserve_thinking() -> None:
    """Previously ``LLMDefaults(extra='forbid')`` had no field for these three
    names, so writing them raised instead of passing through (the audited
    defect). They must now validate and resolve into ``ResolvedLLM.parameters``
    — the same generic map every other provider-specific sampler travels
    through, not a new dedicated ``ResolvedLLM`` field."""
    cfg = LLMConfig(
        defaults=LLMDefaults(
            repeat_penalty=1.1,
            reasoning_effort="xhigh",
            preserve_thinking=False,
        )
    )

    resolved = cfg.resolved("extraction")
    assert resolved.parameters == {
        "repeat_penalty": 1.1,
        "reasoning_effort": "xhigh",
        "preserve_thinking": False,
    }


def test_llm_defaults_repeat_penalty_rejects_negative_values() -> None:
    with pytest.raises(ValidationError):
        LLMDefaults(repeat_penalty=-0.1)


def test_llm_step_overrides_repeat_penalty_and_reasoning_effort() -> None:
    cfg = LLMConfig(
        defaults=LLMDefaults(repeat_penalty=1.0, reasoning_effort="none"),
        extraction=StepLLM(repeat_penalty=1.2, reasoning_effort="xhigh"),
    )

    assert cfg.resolved("extraction").parameters == {
        "repeat_penalty": 1.2,
        "reasoning_effort": "xhigh",
    }
    # A step that does not override inherits the defaults unchanged.
    assert cfg.resolved("judge").parameters == {
        "repeat_penalty": 1.0,
        "reasoning_effort": "none",
    }


def test_llm_resolved_only_inherits_credentials_for_the_same_endpoint() -> None:
    default_key = "OKTO_NEURON_PROVIDER_OPENAI_API_KEY"
    cfg = LLMConfig(
        defaults=LLMDefaults(api_key_env=default_key),
        extraction=StepLLM(),
        judge=StepLLM(provider="anthropic"),
        curator=StepLLM(api_base="http://127.0.0.1:9999/v1"),
        relation_curator=StepLLM(
            provider="anthropic",
            api_key_env="OKTO_NEURON_PROVIDER_ANTHROPIC_API_KEY",
        ),
    )

    assert cfg.resolved("extraction").api_key_env == default_key
    assert cfg.resolved("judge").api_key_env is None
    assert cfg.resolved("curator").api_key_env is None
    assert cfg.resolved("relation_curator").api_key_env == "OKTO_NEURON_PROVIDER_ANTHROPIC_API_KEY"


def test_consolidation_superseded_audit_defaults_are_non_blocking() -> None:
    cfg = ConsolidationConfig()

    assert cfg.audit_superseded_nodes_with_llm is False
    assert cfg.audit_superseded_relations_with_llm is False
    assert cfg.curation_call_timeout_s == 600.0  # finite by default (issue #24)


def test_vault_config_unknown_version_raises_typed_error(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    config_path = vault_path / "okto-neuron.yaml"
    config_path.write_text("marginalia_yaml_version: 99\n", encoding="utf-8")

    with pytest.raises(ConfigVersionUnsupported) as raised:
        VaultConfig.load(vault_path)

    assert raised.value.file_path == config_path.resolve()
    assert raised.value.found_version == 99
    assert raised.value.supported_versions == (1, 2)


def test_vault_config_missing_file_raises_config_not_found(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"

    with pytest.raises(ConfigNotFound) as raised:
        VaultConfig.load(vault_path)

    assert raised.value.file_path == (vault_path / "okto-neuron.yaml").resolve()


def test_vault_config_wraps_yaml_parse_errors(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    config_path = vault_path / "okto-neuron.yaml"
    config_path.write_text(
        "marginalia_yaml_version: 1\nbad:\n\tchild: value\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigParseError) as raised:
        VaultConfig.load(vault_path)

    error = raised.value
    assert error.file_path == config_path.resolve()
    assert error.line == 3
    assert isinstance(error.__cause__, yaml.YAMLError)


def test_config_models_are_not_exported_from_package_root() -> None:
    assert not hasattr(okto_neuron, "OktoNeuronConfig")
    assert not hasattr(okto_neuron, "VaultConfig")
