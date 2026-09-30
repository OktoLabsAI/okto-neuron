"""Per-vault okto-neuron.yaml loader."""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import warnings
from pathlib import Path
from typing import Annotated, Any, ClassVar, Literal, Self
from urllib.parse import urlparse

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    JsonValue,
    Tag,
    ValidationError,
    field_validator,
    model_validator,
)

from okto_neuron._compat import is_vault_config_filename, vault_config_path
from okto_neuron.config._capacity import DEFAULT_PARALLEL_CAPABLE_MODELS
from okto_neuron.config._app_config import default_app_home
from okto_neuron.errors import ConfigNotFound, ConfigParseError, ConfigVersionUnsupported

SUPPORTED_YAML_VERSIONS = (1,)
_WARNED_MISSING_VERSION: set[Path] = set()


def _resolve_path(path: Path | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _config_file_for(path: Path | str) -> Path:
    candidate = _resolve_path(path)
    if is_vault_config_filename(candidate.name):
        return candidate
    return vault_config_path(candidate)


def _yaml_error_line(error: yaml.YAMLError) -> int | None:
    mark = getattr(error, "problem_mark", None) or getattr(error, "context_mark", None)
    if mark is None:
        return None
    return mark.line + 1


def _warn_missing_version_once(path: Path) -> None:
    resolved = _resolve_path(path)
    if resolved in _WARNED_MISSING_VERSION:
        return
    _WARNED_MISSING_VERSION.add(resolved)
    warnings.warn(
        "missing marginalia_yaml_version; treating config as version 1",
        stacklevel=3,
    )


# Embedding providers the embed layer can route. ``stub`` (CI/model-free),
# ``fastembed`` (local default), and ``sentence-transformers`` run in-process; the
# rest are literal LiteLLM provider prefixes. Legacy Okto Neuron aliases are accepted
# by the validator and canonicalized on load, but are not written by the UI.
_EMBEDDING_PROVIDERS = frozenset(
    {
        "stub",
        "fastembed",
        "sentence-transformers",
        "azure",
        "azure_ai",
        "bedrock",
        "cohere",
        "databricks",
        "fireworks_ai",
        "gemini",
        "gigachat",
        "github_copilot",
        "llamagate",
        "libertai",
        "lm_studio",
        "litellm_proxy",
        "mistral",
        "nebius",
        "novita",
        "oci",
        "ollama",
        "openai",
        "perplexity",
        "scaleway",
        "snowflake",
        "together_ai",
        "vercel_ai_gateway",
        "vertex_ai",
        "volcengine",
        "voyage",
    }
)

_EMBEDDING_PROVIDER_ALIASES = {
    "default": "fastembed",
    "openai-compat": "openai",
    "local": "openai",
}


def _check_embedding_provider(value: str | None) -> str | None:
    if value is None:
        return None
    value = _EMBEDDING_PROVIDER_ALIASES.get(value, value)
    if value not in _EMBEDDING_PROVIDERS:
        raise ValueError(
            f"unknown embedding provider: {value!r}; expected one of {sorted(_EMBEDDING_PROVIDERS)}"
        )
    return value


DEFAULT_LLM_REQUEST_TIMEOUT_S = 300.0
"""Wall/read deadline for one LiteLLM completion when neither the provider
connection nor ``OKTO_NEURON_LLM_REQUEST_TIMEOUT`` sets one (issue #24)."""
DEFAULT_CURATION_CALL_TIMEOUT_S = 600.0
"""Wall-clock deadline for one curation/judge call, enclosing its one retry."""
DEFAULT_JOB_STALL_TIMEOUT_S = 900.0
"""No-progress watchdog for a running curation job."""

class EmbeddingConfig(BaseModel):
    """Embedding provider settings for a vault.

    Mirrors :class:`LLMDefaults`: a concrete provider/model plus the typed knobs an
    external (LiteLLM-routed) endpoint needs. ``dimension`` is load-bearing — the
    vector column is fixed-width, so this value is baked into the graph at bootstrap
    and a change requires ``kg reembed`` (see ``REEMBED_FIELDS``). ``api_base`` is
    loopback-gated by this block's own ``allow_remote`` (config *writes* stay
    loopback-only regardless, enforced at the API layer)."""

    model_config = ConfigDict(extra="forbid")

    provider_ref: str | None = None
    provider: str = "fastembed"
    api_base: str | None = None
    model: str = "BAAI/bge-small-en-v1.5"
    api_key_env: str | None = None
    allow_remote: bool = False
    dimension: int = Field(default=384, gt=0)
    # Execution policy only: these values do not change vector semantics and
    # therefore are deliberately absent from ``REEMBED_FIELDS``.
    batch_size: int = Field(default=32, ge=1, le=256)
    max_concurrent_batches: int = Field(default=1, ge=1, le=32)

    @field_validator("provider", mode="after")
    @classmethod
    def _v_provider(cls, value: str) -> str:
        return _check_embedding_provider(value) or value

    @field_validator("api_key_env", mode="after")
    @classmethod
    def _v_api_key_env(cls, value: str | None) -> str | None:
        return _check_api_key_env(value)

    @model_validator(mode="after")
    def _validate_api_base(self) -> Self:
        # The server later POSTs to this URL (LiteLLM-routed providers), so a
        # non-loopback host is an SSRF vector — refuse unless allow_remote. Local
        # in-process providers leave api_base unset and skip the check.
        if self.api_base is not None:
            _check_api_base(self.api_base, self.allow_remote)
        return self

    def resolved_provider(self) -> Self:
        """Resolve an application provider reference into runtime connection fields."""

        if self.provider_ref is None:
            return self
        from okto_neuron.providers import ProviderRegistry, resolve_provider

        profile, credential_env = resolve_provider(self.provider_ref)
        registry = ProviderRegistry.load()
        if "embedding" not in registry.provider_uses(profile.driver):
            raise ValueError(f"provider {self.provider_ref!r} does not support embeddings")
        payload = self.model_dump()
        payload.update(
            provider=profile.driver,
            api_base=profile.api_base,
            api_key_env=credential_env,
            allow_remote=profile.allow_remote,
        )
        return type(self).model_validate(payload)


class LadybugStorageConfig(BaseModel):
    """Storage backend metadata for the built-in Ladybug graph backend.

    Field shape matches what ``Vault._write_config`` has always emitted
    (``{"backend": "ladybug", "reason": None}``), so ``extra="forbid"`` does
    not reject an existing pre-M3 ``okto-neuron.yaml``.
    """

    model_config = ConfigDict(extra="forbid")

    backend: Literal["ladybug"] = "ladybug"
    reason: str | None = None


class RetryConfig(BaseModel):
    """Optimistic-write-conflict retry policy (M4 spec §2, D-10).

    Backend-neutral: a backend whose writes can lose a concurrent-write race
    under MVCC (Grafx today; a future Neo4j/Neptune backend later) shares
    this one policy shape and ``store/_retry.py``'s
    ``retry_with_backoff`` helper rather than each rolling its own backoff
    loop. Values are the M4 spec's own defaults -- full-jitter exponential
    backoff from ``backoff_base_ms`` up to ``backoff_cap_ms``, bounded by
    both ``max_attempts`` and a wall-clock ``total_cap_s`` ceiling, whichever
    is hit first.
    """

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=8, ge=1)
    backoff_base_ms: int = Field(default=50, ge=0)
    backoff_cap_ms: int = Field(default=2000, ge=0)
    total_cap_s: float = Field(default=30, ge=0)


_SIZE_UNITS = {
    "b": 1,
    "kb": 1000, "mb": 1000**2, "gb": 1000**3,
    "kib": 1024, "mib": 1024**2, "gib": 1024**3,
}
_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*$")

#: Bounds for an explicit ``storage.buffer_budget`` (grafx buffer pool).
BUFFER_BUDGET_MIN_BYTES = 16 * 1024**2
BUFFER_BUDGET_MAX_BYTES = 8 * 1024**3


def parse_byte_size(value: object) -> int:
    """Parse ``268435456`` or ``"256MiB"`` / ``"1.5GiB"`` / ``"512MB"`` into bytes."""
    if isinstance(value, bool):
        raise ValueError("a size must be bytes (int) or a string like '256MiB'")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        match = _SIZE_RE.match(value)
        if match is not None:
            unit = _SIZE_UNITS.get((match.group(2) or "b").lower())
            if unit is not None:
                return int(float(match.group(1)) * unit)
    raise ValueError(
        f"invalid size {value!r}: use bytes or a string like '256MiB' "
        "(units: B, KB, MB, GB, KiB, MiB, GiB)"
    )


class GrafxStorageConfig(BaseModel):
    """Storage backend metadata for the (not-yet-shipped) Grafx backend."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["grafx"]
    reason: str | None = None
    retry: RetryConfig = Field(default_factory=RetryConfig)
    buffer_budget: int | None = None
    """Grafx buffer-pool size (``buffer_budget_bytes`` on ``okto_grafx.connect``).
    Bytes or a string such as ``256MiB``; 16 MiB to 8 GiB. ``None`` (default)
    means computed: max(256 MiB, 1.5 x graph size), capped at 1 GiB."""

    @field_validator("buffer_budget", mode="before")
    @classmethod
    def _v_buffer_budget(cls, value: object) -> int | None:
        if value is None:
            return None
        size = parse_byte_size(value)
        if not BUFFER_BUDGET_MIN_BYTES <= size <= BUFFER_BUDGET_MAX_BYTES:
            raise ValueError(
                f"buffer_budget must be between {BUFFER_BUDGET_MIN_BYTES} (16MiB) and "
                f"{BUFFER_BUDGET_MAX_BYTES} (8GiB) bytes, got {size}"
            )
        return size


class Neo4jStorageConfig(BaseModel):
    """Storage backend metadata for the (not-yet-shipped) Neo4j backend."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["neo4j"]
    uri: str
    database: str = "neo4j"
    credential_env: str | None = None
    allow_remote: bool = False
    retry: RetryConfig = Field(default_factory=RetryConfig)
    vault_id: str | None = None

    @field_validator("credential_env", mode="after")
    @classmethod
    def _v_credential_env(cls, value: str | None) -> str | None:
        return _check_api_key_env(value)


class NeptuneStorageConfig(BaseModel):
    """Storage backend metadata for the (not-yet-shipped) Neptune backend."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["neptune"]
    endpoint: str
    auth_mode: Literal["iam"] = "iam"
    region: str


class CustomStorageConfig(BaseModel):
    """Storage backend metadata for any backend outside the four typed variants above.

    The four variants above are the backends this codebase knows the shape
    of; a third-party or test-only backend registered only via the
    ``marginalia.graph_backends`` entry-point group (M3 spec §2.11's
    ``stub_backend_pkg``, or a real future out-of-tree backend) has no typed
    config here. Without this fallback, ``VaultConfig.load()`` would raise
    ``union_tag_invalid`` on any ``storage.backend`` value other than the
    four literals, so a vault pinned to such a backend could never be
    reopened after the config that pinned it was written — a registry that
    can resolve a backend but a config model that can never round-trip its
    name is a broken seam, not a narrower one. Backend-name *validity* stays
    the registry's job (``resolve_graph_backend``, checked at every creation
    and open call site) — this model only pins the field shape so an
    otherwise-resolvable name can persist and reload.
    """

    model_config = ConfigDict(extra="forbid")

    backend: str
    reason: str | None = None


def _storage_config_discriminator(value: object) -> str:
    if isinstance(value, BaseModel):
        backend = getattr(value, "backend", None)
    elif isinstance(value, dict):
        backend = value.get("backend")
    else:
        backend = None
    if backend in {"ladybug", "grafx", "neo4j", "neptune"}:
        return backend
    return "custom"


# Discriminated by a callable, not a bare ``Literal`` field, so an unknown
# ``backend`` name falls through to ``CustomStorageConfig`` instead of
# failing to parse (see its docstring). ``ladybug``, ``grafx`` (M4), and
# ``neo4j`` (M5) are registry-reachable; ``neptune`` parses and validates but
# resolving it raises ``NoSuchBackendError`` until its own milestone
# registers a real class — same as any other name that lands in the
# ``custom`` branch.
StorageConfig = Annotated[
    Annotated[LadybugStorageConfig, Tag("ladybug")]
    | Annotated[GrafxStorageConfig, Tag("grafx")]
    | Annotated[Neo4jStorageConfig, Tag("neo4j")]
    | Annotated[NeptuneStorageConfig, Tag("neptune")]
    | Annotated[CustomStorageConfig, Tag("custom")],
    Discriminator(_storage_config_discriminator),
]

# The product-facing default backend for a genuinely NEW vault (owner
# decision: Okto Grafx is the default, non-experimental graph backend).
# Every CLI vault-creation entry point (`okto-neuron init`, `vault create`,
# `onboard`; `kg init` only when a fresh vault gets no explicit --backend)
# passes this explicitly into `Vault._write_config`/`_write_kg_init_vault_config`
# so the resulting `okto-neuron.yaml` always carries a literal
# `storage.backend: grafx`.
#
# This is deliberately NOT the same thing as `VaultConfig.storage`'s own
# field default (`None`, on `VaultConfig` below) or `VaultConfig.default()`'s
# baseline (also storage-less) -- those two stay untouched so a legacy
# `okto-neuron.yaml` with no `storage` key at all keeps resolving to
# ``"ladybug"`` (the pre-M3/pre-this-decision fallback every read site
# still applies: `vault.py::_check_backend_pin`, `store/vault.py::
# _read_pinned_backends`, `cli/kg.py::_resolve_pinned_backend`,
# `reconcile/heal.py`). `VaultConfig.default()` also serves as `load()`'s
# deep-merge baseline for EVERY existing vault (below), so giving it a
# non-None `storage` would silently repin every legacy no-storage-key vault
# to grafx on next read -- exactly the bug this constant exists to avoid.
# In short: "absent storage key" always means ladybug (legacy); "freshly
# created, no explicit --backend" always means DEFAULT_NEW_VAULT_BACKEND
# (grafx), pinned explicitly and eagerly at creation time instead of ever
# relying on the absent-key fallback.
DEFAULT_NEW_VAULT_BACKEND = "grafx"


class IndexConfig(BaseModel):
    """Index backend metadata captured at vault creation time."""

    model_config = ConfigDict(extra="forbid")

    backend: str = "ladybug_bm25+vector_scan"


_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
EndpointClass = Literal["loopback", "private", "public"]
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")
_ENCODED_IP_RE = re.compile(r"^(?:0x[0-9a-fA-F]+|\d+)$")

# Provider names the LLM layer can route. ``stub`` is special (StubLLM, model-free,
# for CI/tests); every other value is a literal LiteLLM provider prefix. Legacy
# Okto Neuron aliases are accepted by validators and canonicalized on load.
# Kept as an allowlist so a UI/config typo fails loud instead of silently picking a
# wrong backend, but broad enough to cover the providers used for permutation testing.
# Single source of truth for the provider dropdown (the UI mirrors this exact set).
_LLM_PROVIDERS = frozenset(
    {
        "stub",
        "ai21",
        "ai21_chat",
        "amazon_nova",
        "anthropic",
        "anthropic_text",
        "azure",
        "azure_ai",
        "azure_text",
        "baseten",
        "bedrock",
        "bytez",
        "cerebras",
        "chatgpt",
        "claude_cli",
        "clarifai",
        "cloudflare",
        "codestral",
        "codex_cli",
        "cohere",
        "cohere_chat",
        "cometapi",
        "custom",
        "custom_openai",
        "dashscope",
        "databricks",
        "datarobot",
        "deepinfra",
        "deepseek",
        "docker_model_runner",
        "empower",
        "featherless_ai",
        "fireworks_ai",
        "friendliai",
        "galadriel",
        "gemini",
        "gigachat",
        "github",
        "github_copilot",
        "gradient_ai",
        "groq",
        "helicone",
        "heroku",
        "hosted_vllm",
        "huggingface",
        "inception",
        "lambda_ai",
        "lemonade",
        "litellm_proxy",
        "llamafile",
        "lm_studio",
        "maritalk",
        "meta_llama",
        "mistral",
        "moonshot",
        "morph",
        "nebius",
        "nlp_cloud",
        "novita",
        "nscale",
        "nvidia_nim",
        "oci",
        "ollama",
        "ollama_chat",
        "oobabooga",
        "openai",
        "openai_like",
        "openrouter",
        "ovhcloud",
        "perplexity",
        "pi_cli",
        "petals",
        "predibase",
        "publicai",
        "replicate",
        "sagemaker",
        "sagemaker_chat",
        "sagemaker_nova",
        "sambanova",
        "text-completion-codestral",
        "text-completion-inception",
        "text-completion-openai",
        "together_ai",
        "triton",
        "v0",
        "vercel_ai_gateway",
        "vertex_ai",
        "vertex_ai_beta",
        "vllm",
        "volcengine",
        "wandb",
        "watsonx",
        "watsonx_text",
        "xai",
    }
)

_LLM_PROVIDER_ALIASES = {
    "claude-code": "claude_cli",
    "claude_code": "claude_cli",
    "codex": "codex_cli",
    "openai-compat": "openai",
    "omlx": "openai",
    "local": "openai",
    "pi": "pi_cli",
    "together": "together_ai",
}

# Env-var names the config-write surface is allowed to reference for an LLM API
# key. WITHOUT this allowlist a config PATCH could set ``api_key_env`` to any
# server env var (e.g. AWS_SECRET_ACCESS_KEY) and, combined with allow_remote +
# an attacker-controlled api_base, exfiltrate it on the next ask/remember. The
# key value itself is never stored — only the name — but the name must be scoped
# to this application's own namespace. The pre-0.3.0 ``MARGINALIA_`` namespace is
# the same application's, so names already stored in existing configs stay valid.
_API_KEY_ENV_PATTERN = re.compile(r"^(?:OKTO_NEURON_|MARGINALIA_)[A-Z0-9_]+$")
_LLM_PARAMETER_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# These values belong to Okto Neuron's request contract, connection security, or
# response parser. They can be advertised by LiteLLM, but a user preference must
# never override them through the generic advanced-parameter map.
MANAGED_LLM_PARAMETERS = frozenset(
    {
        "additional_drop_params",
        "allowed_openai_params",
        "audio",
        "api_base",
        "api_key",
        "base_url",
        "candidate_count",
        "drop_params",
        "extra_body",
        "extra_headers",
        "function_call",
        "functions",
        "include_server_side_tool_invocations",
        "logprobs",
        "messages",
        "model",
        "modalities",
        "n",
        "parallel_tool_calls",
        "response_format",
        "response_mime_type",
        "response_schema",
        "stream",
        "stream_options",
        "system",
        "template",
        "timeout",
        "top_logprobs",
        "tool_choice",
        "tools",
        "web_search_options",
    }
)
# LLMDefaults/StepLLM typed fields with no matching ResolvedLLM field: they
# exist only for YAML ergonomics and are folded into ``ResolvedLLM.parameters``
# by ``LLMConfig._resolved_connection`` (see that method and the fields'
# declarations on LLMDefaults for why). ``ResolvedLLM.parameters`` is now
# populated ONLY from these three fields — the generic user-writable
# ``parameters`` map on ``LLMDefaults``/``StepLLM`` was removed (decision A,
# 2026-09-15); ``ResolvedLLM.parameters`` itself stays (see that field's
# docstring for its two remaining callers).
_PARAMETERS_ONLY_LLM_FIELDS = ("repeat_penalty", "reasoning_effort", "preserve_thinking")


def _check_llm_parameters(
    values: dict[str, JsonValue], *, allow_tombstones: bool
) -> dict[str, JsonValue]:
    for name, value in values.items():
        if not _LLM_PARAMETER_NAME_PATTERN.fullmatch(name):
            raise ValueError(
                f"invalid LLM parameter name {name!r}; expected a Python keyword-style name"
            )
        if name in MANAGED_LLM_PARAMETERS:
            raise ValueError(
                f"LLM parameter {name!r} is managed by Okto Neuron and cannot be overridden"
            )
        if value is None and not allow_tombstones:
            raise ValueError(
                f"LLM default parameter {name!r} cannot be null; remove it to use the provider default"
            )
    return values


# ── Raw sampling-payload override ───────────────────────────────────────────
# Unlike ``parameters``/``MANAGED_LLM_PARAMETERS`` above (a whitelist-and-range-
# validated map of Okto Neuron-recognized sampler names), ``sampling_payload`` is
# a deliberately unconstrained escape hatch: an operator running a self-hosted
# model can send ANY key the backend understands (``typical_p``,
# ``stop_token_ids``, a nested ``grammar`` object, …) without Okto Neuron
# whitelisting it first — the backend itself is the validator. Every genuine
# sampling parameter stays unrestricted; only two categories of key are
# refused, for two different reasons:
#
# CONNECTION-OWNED: the model string, the message list, the connection's
# api_base/api_key, and the drop/timeout transport knobs. These belong to
# Okto Neuron's own request contract, connection routing, or transport policy
# and must never be operator-settable through a generic sampling map. This
# mirrors ``okto_neuron.llm._LITELLM_CONTROL_PARAMS`` (the set Okto Neuron itself
# always supplies to every ``litellm.completion()`` call) — kept as an
# independent constant here rather than imported, since ``okto_neuron.llm``
# imports FROM this module, not the other way around.
#
# RESPONSE-SHAPE: ``stream``/``n``/``tools``/``extra_body`` change the SHAPE of
# the response Okto Neuron has to parse, not the sampling of it — the same
# category as blocking ``messages``, not a whitelist on tuning. Setting
# ``stream: true`` or ``n: 3`` would not produce a clean backend 400; it would
# produce a parse failure downstream, which is exactly the opaque failure this
# feature exists to eliminate. ``extra_body`` is refused for a related, more
# mechanical reason: it is the container ``LiteLLMProvider.complete()`` itself
# builds by routing every non-OpenAI-standard payload key into it (see that
# method) — a literal ``extra_body`` key in the payload would collide with
# that container instead of reaching the backend as the operator intends.
_CONNECTION_OWNED_SAMPLING_PAYLOAD_KEYS = frozenset(
    {"api_base", "api_key", "drop_params", "messages", "model", "timeout"}
)
_RESPONSE_SHAPE_SAMPLING_PAYLOAD_KEYS = frozenset({"extra_body", "n", "stream", "tools"})
RESERVED_SAMPLING_PAYLOAD_KEYS = frozenset(
    _CONNECTION_OWNED_SAMPLING_PAYLOAD_KEYS | _RESPONSE_SHAPE_SAMPLING_PAYLOAD_KEYS
)


def _check_sampling_payload(values: dict[str, JsonValue]) -> dict[str, JsonValue]:
    for name in values:
        if name in _CONNECTION_OWNED_SAMPLING_PAYLOAD_KEYS:
            raise ValueError(
                f"sampling_payload key {name!r} is owned by the connection config "
                "(model/messages/api_base/api_key/drop_params/timeout) and cannot "
                "be set through a raw sampling payload"
            )
        if name in _RESPONSE_SHAPE_SAMPLING_PAYLOAD_KEYS:
            raise ValueError(
                f"sampling_payload key {name!r} controls the response shape "
                "(extra_body/n/stream/tools) and is owned by Okto Neuron's request "
                "handling, not sampling, so it cannot be set through a raw sampling payload"
            )
    return values


# Named, committed sampling presets for the local Qwen 3.8 27b reference
# self-hosted model (owner-supplied, 2026-09-09). This is the single source of
# truth: the LoCoMo benchmark harness (kept in a private repository) imports
# this exact object rather than
# redefining its own copy, so a recorded LoCoMo run's sampling configuration
# can never silently drift out from under the benchmark history it was
# measured against. Every key here is also a valid ``sampling_payload`` entry
# (none collide with ``RESERVED_SAMPLING_PAYLOAD_KEYS``), though the benchmark
# harness itself still threads these values through the older per-vault typed
# fields, not through ``sampling_payload`` — see that module for why.
SAMPLING_PRESETS: dict[str, dict[str, Any]] = {
    "instruct": {
        "temperature": 0.7,
        "max_tokens": 32768,
        "top_p": 0.80,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repeat_penalty": 1.0,
        "enable_thinking": False,
        "reasoning_effort": "none",
        "preserve_thinking": False,
        "chat_template_kwargs": {
            "enable_thinking": False,
            "preserve_thinking": False,
            "reasoning_effort": "none",
        },
    },
    "thinking": {
        "temperature": 1.0,
        "max_tokens": 32768,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repeat_penalty": 1.0,
        "enable_thinking": True,
        "reasoning_effort": "xhigh",
        "preserve_thinking": False,
        "chat_template_kwargs": {
            "enable_thinking": True,
            "preserve_thinking": False,
            "reasoning_effort": "xhigh",
        },
    },
}


# The LLM moments in the orchestrator chain. Each resolves its own concrete
# config (defaults + that step's overrides) so e.g. extraction can run a big local
# model while merge adjudication and candidate curation run cheaper/smaller.
StepName = Literal["extraction", "judge", "curator", "relation_curator", "ask"]
_STEP_NAMES: tuple[StepName, ...] = (
    "extraction",
    "judge",
    "curator",
    "relation_curator",
    "ask",
)

# Connection fields are shared across LLMDefaults / StepLLM / ResolvedLLM.
# Optional generation values use the typed fields below (a compatibility
# surface for older YAML) or the raw ``sampling_payload`` escape hatch; both
# are absent by default.


def _check_provider(value: str | None) -> str | None:
    if value is None:
        return None
    value = _LLM_PROVIDER_ALIASES.get(value, value)
    if value not in _LLM_PROVIDERS:
        raise ValueError(
            f"unknown llm provider: {value!r}; expected one of {sorted(_LLM_PROVIDERS)}"
        )
    return value


def _check_api_key_env(value: str | None) -> str | None:
    if value is None:
        return None
    if not _API_KEY_ENV_PATTERN.fullmatch(value):
        raise ValueError(
            f"llm.api_key_env {value!r} is not allowed; it must name an env var "
            "in this application's namespace (pattern: ^OKTO_NEURON_[A-Z0-9_]+$, or the "
            "pre-0.3.0 ^MARGINALIA_[A-Z0-9_]+$). "
            "This prevents a config write from pointing the API key at an "
            "arbitrary server environment variable."
        )
    return value


def _canonical_openai_api_base(provider: str | None, api_base: str | None) -> str | None:
    """Canonical completion base for an OpenAI-compatible driver, else unchanged.

    One canonical base-URL behavior (see ``okto_neuron.providers.resolve_openai_base``):
    for drivers whose contract is the plain OpenAI one under ``/v1``, the base is
    always the server root plus ``/v1`` regardless of the form the user typed — so
    ``http://host:port`` and ``http://host:port/v1`` are the same configuration.
    Any other driver (Anthropic, Gemini, Azure, …) or a base the resolver cannot
    parse is returned exactly as given; malformed bases still fail in the existing
    ``_check_api_base`` validation with its own message.
    """

    if not api_base or provider is None:
        return api_base
    from okto_neuron.providers import OPENAI_V1_DRIVERS, resolve_openai_base

    if provider not in OPENAI_V1_DRIVERS:
        return api_base
    try:
        return resolve_openai_base(api_base).api_base
    except ValueError:
        return api_base


def classify_api_base(url: str, *, resolve: bool = False) -> EndpointClass:
    """Classify an LLM endpoint after normalizing obvious SSRF tricks.

    ``resolve`` is intentionally opt-in. Config loading should not depend on DNS,
    but onboarding/config writes use it for non-loopback ``http://`` hostnames so
    public plaintext endpoints cannot slip through as opaque names.
    """

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"llm api_base scheme must be http or https, got {parsed.scheme!r}")
    if parsed.username or parsed.password:
        raise ValueError("llm api_base must not include username or password")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("llm api_base must include a host")
    normalized = host.strip("[]").lower()
    if normalized in _LOCAL_HOSTS:
        return "loopback"

    literal = _parse_ip_literal(normalized)
    if literal is not None:
        return _classify_ip_address(literal, original_host=host)
    if _looks_like_encoded_ip_host(normalized):
        raise ValueError(
            f"llm api_base host {host!r} looks like an encoded IP literal; use a "
            "normal hostname or dotted/colon IP address"
        )
    if not resolve:
        return "public"

    classes = {
        _classify_ip_address(address, original_host=host)
        for address in _resolve_host_addresses(normalized, parsed.port)
    }
    if not classes:
        raise ValueError(f"llm api_base host {host!r} did not resolve")
    if classes == {"loopback"}:
        return "loopback"
    if classes <= {"loopback", "private"}:
        return "private"
    return "public"


# Full Neo4j driver scheme set, wider than any single backend needs this
# milestone: no Neo4j backend ships in M3 (types only, see ``StorageConfig``),
# so being permissive here costs nothing and saves a future scheme-allowlist
# change when M5 lands.
_STORAGE_ENDPOINT_SCHEMES = frozenset(
    {"bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc", "http", "https"}
)


def _classify_storage_endpoint(url: str, *, resolve: bool = False) -> EndpointClass:
    """Classify a storage-backend endpoint after normalizing obvious SSRF tricks.

    Sibling of ``classify_api_base``: reuses the same host/IP/SSRF helpers
    but accepts the wider storage-driver scheme set (``bolt``/``neo4j`` plus
    their TLS variants, alongside ``http``/``https``) instead of hard-limiting
    to ``{http, https}``. ``resolve`` is opt-in for the same reason as
    ``classify_api_base``: config loading should not depend on DNS.
    """

    parsed = urlparse(url)
    if parsed.scheme not in _STORAGE_ENDPOINT_SCHEMES:
        raise ValueError(
            f"storage endpoint scheme must be one of {sorted(_STORAGE_ENDPOINT_SCHEMES)}, "
            f"got {parsed.scheme!r}"
        )
    if parsed.username or parsed.password:
        raise ValueError("storage endpoint must not include username or password")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("storage endpoint must include a host")
    normalized = host.strip("[]").lower()
    if normalized in _LOCAL_HOSTS:
        return "loopback"

    literal = _parse_ip_literal(normalized)
    if literal is not None:
        return _classify_ip_address(literal, original_host=host)
    if _looks_like_encoded_ip_host(normalized):
        raise ValueError(
            f"storage endpoint host {host!r} looks like an encoded IP literal; use a "
            "normal hostname or dotted/colon IP address"
        )
    if not resolve:
        return "public"

    classes = {
        _classify_ip_address(address, original_host=host)
        for address in _resolve_host_addresses(normalized, parsed.port)
    }
    if not classes:
        raise ValueError(f"storage endpoint host {host!r} did not resolve")
    if classes == {"loopback"}:
        return "loopback"
    if classes <= {"loopback", "private"}:
        return "private"
    return "public"


def api_base_is_loopback(url: str) -> bool:
    """Return whether ``url`` is a syntactic loopback endpoint."""

    return classify_api_base(url, resolve=False) == "loopback"


# Drivers that ALWAYS reach a hosted third-party API, with no ``api_base`` for
# ``_check_api_base`` to inspect.
#
# The loopback/remote gate is a URL check: it reads ``api_base`` and classifies
# the host. A driver that never sets ``api_base`` therefore skipped the gate
# entirely and egressed under ``allow_remote: false`` — the check was not
# failing open on a bad URL, it was simply never reached (``providers.py``
# ``_validate_profile``: the call sits inside ``if profile.api_base is not
# None:``; ``LLMConfig._validate_api_bases``: the resolved base falls back to
# the vault's own loopback default, which passes vacuously).
#
# These four are remote by construction, so the honest gate is the driver name:
#
# * ``chatgpt`` — litellm's ``ChatGPTConfig`` injects OpenAI's base URL
#   dynamically, so the profile shows no ``api_base`` while every call still
#   goes to ``chatgpt.com``/``api.openai.com``.
# * ``codex_cli`` / ``claude_cli`` / ``pi_cli`` — pseudo-providers that shell
#   out to a local binary. The binary is local; the inference is not. Each
#   forwards the prompt to its vendor's hosted API under whatever subscription
#   or key the CLI already holds, which is exactly the egress
#   ``allow_remote: false`` exists to refuse.
#
# Value is the sentence appended to the rejection, so the error says WHY rather
# than only WHICH.
UNCONDITIONALLY_REMOTE_DRIVERS: dict[str, str] = {
    "chatgpt": (
        "it sends every prompt to OpenAI's ChatGPT backend; litellm injects that "
        "base URL itself, so the profile's empty api_base is not evidence of a "
        "local endpoint"
    ),
    "codex_cli": (
        "the local `codex` binary is only a shell — it forwards every prompt to "
        "OpenAI under the ChatGPT subscription or API key it already holds"
    ),
    "claude_cli": (
        "the local `claude` binary is only a shell — it forwards every prompt to "
        "Anthropic under the subscription or API key it already holds"
    ),
    "pi_cli": (
        "the local `pi` binary is only a shell — it forwards every prompt to "
        "whichever hosted provider ~/.pi/agent/settings.json selects"
    ),
}


def _check_remote_driver(driver: str, allow_remote: bool, *, where: str) -> None:
    """Refuse an inherently-remote driver when remote egress is not allowed.

    ``where`` names the knob the operator has to flip, because the two callers
    read a DIFFERENT flag: a provider profile carries its own ``allow_remote``,
    while a bare ``llm.provider`` is gated by the vault-level
    ``llm.allow_remote``. Naming the wrong one sends the operator editing a
    field that will not change the outcome.

    This is a deliberate behaviour change: a vault that ran a CLI provider
    under ``allow_remote: false`` was egressing, and now fails loudly at config
    validation instead of at the first completion (or never).
    """

    if allow_remote:
        return
    reason = UNCONDITIONALLY_REMOTE_DRIVERS.get(driver)
    if reason is None:
        return
    raise ValueError(
        f"provider driver {driver!r} always sends prompts to a remote service: "
        f"{reason}. This vault sets {where}: false, which forbids that. "
        f"It has no api_base to inspect, so the refusal is by driver name rather "
        f"than by URL. Set {where}: true to accept that the prompts leave this "
        f"machine, or pick a driver that talks to an endpoint you control "
        f"(e.g. `openai` against a local inference server)."
    )


def _check_api_base(url: str, allow_remote: bool) -> None:
    """Validate an LLM ``api_base`` before it can be persisted or used.

    Loopback endpoints are always allowed. Remote endpoints require
    ``allow_remote``; dangerous local-network metadata/link-local/unspecified
    addresses are rejected regardless; public endpoints must use HTTPS.
    """

    parsed = urlparse(url)
    resolve = parsed.scheme == "http" and (parsed.hostname or "").lower() not in _LOCAL_HOSTS
    endpoint_class = classify_api_base(url, resolve=resolve)
    host = parsed.hostname or ""
    if endpoint_class != "loopback" and not allow_remote:
        raise ValueError(
            f"llm api_base host {host!r} is not loopback; set llm.allow_remote: true "
            "to opt in to a remote endpoint"
        )
    if endpoint_class == "public" and parsed.scheme != "https":
        raise ValueError(
            f"llm api_base host {host!r} is public; public LLM endpoints must use https"
        )


def _parse_ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _looks_like_encoded_ip_host(host: str) -> bool:
    if _ENCODED_IP_RE.fullmatch(host):
        return True
    parts = host.split(".")
    if len(parts) <= 1:
        return False
    return all(_ENCODED_IP_RE.fullmatch(part) for part in parts)


def _resolve_host_addresses(
    host: str, port: int | None
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"llm api_base host {host!r} did not resolve") from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        address = _parse_ip_literal(str(sockaddr[0]).lower())
        if address is not None:
            addresses.append(address)
    return addresses


def _classify_ip_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, *, original_host: str
) -> EndpointClass:
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if address.is_loopback:
        return "loopback"
    if (
        address.is_unspecified
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
    ):
        raise ValueError(
            f"llm api_base host {original_host!r} resolves to disallowed address {address}"
        )
    if address.is_private or (isinstance(address, ipaddress.IPv4Address) and address in _CGNAT_V4):
        return "private"
    return "public"


class ResolvedLLM(BaseModel):
    """Fully-merged LLM settings for one call site (defaults + step overrides).

    Connection fields are concrete; optional generation fields may be absent so
    the provider/model can apply its own defaults. The system prompt is *not* part of this block: it is
    read step-level only (``StepLLM.system_prompt``), falling back to the call
    site's own code default (``_SYSTEM`` / ``_VERDICT_SYSTEM`` / ``_ASK_SYSTEM``)."""

    model_config = ConfigDict(extra="forbid")

    provider_ref: str | None = None
    parameter_mode: Literal["auto", "safe", "local_extended"] = "auto"
    provider: str
    api_base: str
    model: str
    api_key_env: str | None
    # Transport policy inherited from a named provider connection. ``None``
    # means the bounded default ``DEFAULT_LLM_REQUEST_TIMEOUT_S`` applies.
    request_timeout_s: float | None = Field(default=None, gt=0.0)
    # Deprecated typed fields stay at this boundary so existing YAML and call
    # sites remain readable.
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=0)
    min_p: float | None = Field(default=None, ge=0.0, le=1.0)
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    enable_thinking: bool | None = None
    # Internal-only generic map — NOT user-writable via ``LLMDefaults``/
    # ``StepLLM`` any more (decision A, 2026-09-15 removed that free-form
    # surface entirely; no migration, no deprecation period). Two things still
    # populate/consume it: (1) ``LLMConfig._resolved_connection`` folds
    # ``_PARAMETERS_ONLY_LLM_FIELDS`` (``repeat_penalty``/``reasoning_effort``/
    # ``preserve_thinking``) into it, since those typed fields have no
    # dedicated ``ResolvedLLM`` field of their own; (2) the
    # ``/llm/test-completion`` REST probe can still set it directly in its
    # request body for an ad hoc, non-persisted connectivity/sampler test —
    # unrelated to any stored vault config. ``_check_llm_parameters``/
    # ``MANAGED_LLM_PARAMETERS`` keep guarding both of those paths.
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    # Frozen raw sampling-payload override, already resolved for this call site
    # (see ``LLMDefaults.sampling_payload`` / ``StepLLM.sampling_payload`` for
    # the per-role resolution rule). Empty means "no override" — everything
    # above governs the request as before. Non-empty means
    # ``LiteLLMProvider.complete()`` sends this payload's keys untouched
    # instead of the capability-filtered ``parameters``/typed-field path.
    sampling_payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("provider", mode="after")
    @classmethod
    def _v_provider(cls, value: str) -> str:
        return _check_provider(value) or value

    @field_validator("parameters", mode="after")
    @classmethod
    def _v_parameters(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _check_llm_parameters(value, allow_tombstones=False)

    @field_validator("sampling_payload", mode="after")
    @classmethod
    def _v_sampling_payload(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _check_sampling_payload(value)

    @model_validator(mode="after")
    def _v_canonical_api_base(self) -> Self:
        # Runtime choke point: every completion (ask/ingest, the Config UI's
        # test-completion probe, onboarding verify) resolves through a
        # ResolvedLLM, so the canonical base here is the one behavior.
        canonical = _canonical_openai_api_base(self.provider, self.api_base)
        if canonical is not None and canonical != self.api_base:
            self.api_base = canonical
        return self


class LLMDefaults(BaseModel):
    """Baseline connection and explicit parameter overrides inherited by every step."""

    model_config = ConfigDict(extra="forbid")

    provider_ref: str | None = None
    provider: str = "openai"
    api_base: str = "http://127.0.0.1:8123/v1"
    # Discovery-first baseline: an empty model, so the defaults (and
    # /api/v1/config/defaults) never claim a model the configured endpoint
    # may not serve. Onboarding and the UI start from model discovery
    # against the chosen endpoint and fall back to manual entry.
    model: str = ""
    api_key_env: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=0)
    min_p: float | None = Field(default=None, ge=0.0, le=1.0)
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    enable_thinking: bool | None = None
    # Self-hosted raw-sampler/chat-template knobs, same "compatibility surface"
    # status as the fields above: an Okto Neuron-owned typed field for YAML
    # ergonomics, folded into ``ResolvedLLM.parameters`` at resolution time
    # (``LLMConfig._resolved_connection``) rather than carried as their own
    # ``ResolvedLLM`` field — ``LiteLLMProvider.complete()`` reads them off the
    # generic parameters map, the same as any other provider-specific sampler.
    # ``repeat_penalty`` reaches the wire through the same self-hosted
    # ``extra_body`` escape hatch as ``top_k``/``min_p``; ``reasoning_effort``/
    # ``preserve_thinking`` are written into ``extra_body.chat_template_kwargs``
    # alongside ``enable_thinking``. ``None`` for all three preserves today's
    # behaviour exactly.
    repeat_penalty: float | None = Field(default=None, ge=0.0)
    reasoning_effort: str | None = None
    preserve_thinking: bool | None = None
    # Raw sampling-payload override baseline (see ``RESERVED_SAMPLING_PAYLOAD_KEYS``
    # / ``_check_sampling_payload``). Every step inherits this WHOLE dict
    # verbatim unless it sets its own ``StepLLM.sampling_payload`` — there is no
    # per-key merge for this field, only whole-object substitution
    # (``LLMConfig.resolved``'s generic per-field merge already implements
    # that: a step's own non-``None`` dict always wins outright, a step that
    # never set one falls back to exactly this dict). Empty (the default)
    # means no role gets a raw override and every call behaves exactly as
    # before this field existed.
    sampling_payload: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("provider", mode="after")
    @classmethod
    def _v_provider(cls, value: str) -> str:
        return _check_provider(value) or value

    @field_validator("api_key_env", mode="after")
    @classmethod
    def _v_api_key_env(cls, value: str | None) -> str | None:
        return _check_api_key_env(value)

    @field_validator("sampling_payload", mode="after")
    @classmethod
    def _v_sampling_payload(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _check_sampling_payload(value)

    @model_validator(mode="after")
    def _v_canonical_api_base(self) -> Self:
        # Canonicalize at write time too, so the saved YAML (and what the
        # Config UI shows) is the same canonical base the runtime uses.
        canonical = _canonical_openai_api_base(self.provider, self.api_base)
        if canonical is not None and canonical != self.api_base:
            self.api_base = canonical
        return self


class StepLLM(BaseModel):
    """Per-step overrides. Every field is optional — an unset (``None``) field
    inherits from :class:`LLMDefaults`. Editing a step in the UI writes only the
    fields that differ, so the yaml stays minimal and a permutation swap is a tiny
    diff."""

    model_config = ConfigDict(extra="forbid")

    provider_ref: str | None = None
    provider: str | None = None
    api_base: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    top_k: int | None = Field(default=None, ge=0)
    min_p: float | None = Field(default=None, ge=0.0, le=1.0)
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    enable_thinking: bool | None = None
    # Same self-hosted raw-sampler/chat-template knobs as ``LLMDefaults`` — see
    # that class's comment. ``None`` inherits the default (or the endpoint's
    # own default when the default is also unset).
    repeat_penalty: float | None = Field(default=None, ge=0.0)
    reasoning_effort: str | None = None
    preserve_thinking: bool | None = None
    # FROZEN raw sampling-payload override for this one step (see
    # ``LLMDefaults.sampling_payload``). ``None`` (the default — distinct from
    # ``{}``!) means this step never set its own payload, so it inherits
    # ``defaults.sampling_payload`` WHOLE. Any concrete dict, including an
    # explicit ``{}``, means this step owns a complete standalone payload:
    # ``LLMConfig.resolved`` swaps it in outright and never merges it with
    # ``defaults.sampling_payload`` — a later change to the default payload
    # can never alter a step that already has its own. This is a whole-value
    # substitution, never a per-key merge or tombstone.
    sampling_payload: dict[str, JsonValue] | None = None
    system_prompt: str | None = None

    @field_validator("sampling_payload", mode="after")
    @classmethod
    def _v_sampling_payload(
        cls, value: dict[str, JsonValue] | None
    ) -> dict[str, JsonValue] | None:
        if value is None:
            return None
        return _check_sampling_payload(value)

    @model_validator(mode="after")
    def _v_canonical_api_base(self) -> Self:
        # Only canonicalize when the step names its own driver; a step that
        # inherits the defaults' provider is canonicalized at merge time
        # (ResolvedLLM._v_canonical_api_base sees the concrete provider).
        canonical = _canonical_openai_api_base(self.provider, self.api_base)
        if canonical is not None and canonical != self.api_base:
            self.api_base = canonical
        return self

    # ── extraction-only mode (Mode B: enumerate-then-describe) ─────────────────
    # Read STEP-DIRECT from cfg.llm.extraction.mode (NOT via resolved(), which
    # only iterates LLMDefaults keys). "baseline" = today's single-pass extract;
    # "enumerate" = enumerate handles over a cached prefix, then describe handles
    # in batches (extraction-completeness Mode B). "auto" = dynamic: run the
    # baseline single pass and, ONLY for blocks the model truncated
    # (finish_reason="length"), re-run THAT block via the enumerate pipeline —
    # so cost stays near baseline while truncating blocks still recover facts.
    # Field default stays None so non-extraction steps are unaffected and
    # explicit "baseline"/"enumerate" remain selectable; when unset, the
    # EFFECTIVE extraction default is "auto" (escalate-on-truncation), resolved
    # at the extraction read site. Only consulted for the extraction step.
    mode: str | None = None
    # Mode B runaway caps (per 12k Block): hard ceiling on enumerated handles and
    # on describe batches, so one pathological synthetic block with hundreds of
    # handles cannot stall a whole-vault reingest.
    enumerate_max_handles: int | None = Field(default=None, gt=0)
    enumerate_describe_batch: int | None = Field(default=None, gt=0)
    enumerate_max_describe_batches: int | None = Field(default=None, gt=0)
    # ── E6 multi-sample union (``samples`` = k independent draws per block) ─────
    # Read STEP-DIRECT off cfg.llm.extraction.samples (extraction-only; not an
    # LLMDefaults key). k independent extractor draws per block whose candidate
    # sets are UNIONED (deduped) before the companion's dedup/curation. Default
    # None → 1 = today's single-pass extract, byte-identical. Set to 2 for the
    # finale (each block drawn twice at the vault's configured sampling temp;
    # the union recovers facts a single colder draw missed). Only meaningful at
    # temperature > 0 — at temp 0 the draws are identical and the union is a
    # deliberate no-op. Only consulted for the extraction step.
    samples: int | None = Field(default=None, gt=0)
    # ADR 0036: maximum independent chunks with an extractor call in flight.
    # This is Okto Neuron orchestration policy, not a model parameter, so it is
    # read STEP-DIRECT and never included in LiteLLM request kwargs. None -> 1
    # preserves the historical sequential path. The cap matches curation's
    # existing bounded fan-out and deliberately permits values above eight.
    max_concurrent: int | None = Field(default=None, ge=1, le=32)

    @field_validator("mode", mode="after")
    @classmethod
    def _v_mode(cls, value: str | None) -> str | None:
        if value is not None and value not in {"baseline", "enumerate", "auto"}:
            raise ValueError(
                f"extraction mode must be 'baseline', 'enumerate', or 'auto', got {value!r}"
            )
        return value

    # ── ADR 0011 ask-only retrieval knobs (subgraph-first answer assembly) ──────
    # These are read STEP-DIRECT (``cfg.llm.ask.X or default``), NEVER via
    # ``cfg.llm.resolved("ask")`` — ``resolved()`` iterates ``LLMDefaults`` keys
    # only and silently drops StepLLM-only fields. They have no generation
    # semantics and no meaning for extraction/judge. Code-default fallbacks live in
    # ``companion/__init__.py`` (``_ASK_*_DEFAULT`` constants).
    enable_subgraph: bool | None = None
    max_degree_per_seed: int | None = Field(default=None, gt=0)
    neighbour_budget_tokens: int | None = Field(default=None, gt=0)
    hops: int | None = Field(default=None, ge=1, le=5)  # N-hop ego-graph depth (ADR 0011)
    coverage_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    render_format: str | None = None
    min_claim_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    # ── GN-13 (ADR 0019 Phase 3): graph-native answerer prompt ─────────────────
    # Override the graph-native system prompt (_ASK_SYSTEM_GRAPH) per-vault.
    # Only consulted when enable_subgraph=True.  None → code default.
    system_prompt_graph: str | None = None
    # ── GN-14 (ADR 0019 Phase 3): coverage-fallback threshold (graph-native) ───
    # When the subgraph render falls below this density floor the graph-native
    # path short-circuits to Tier-2 raw-block fallback.  0.0 = disabled (default
    # preserves existing behaviour).  Swept via GN-3 harness post Phase 2.
    coverage_threshold_graph: float | None = Field(default=None, ge=0.0, le=1.0)
    # ── Fix B (task-12): seed-quality diversification (ask/explore seed cut) ────
    # Read STEP-DIRECT like the other ask-only knobs. ``seed_diversity`` None →
    # effective default follows enable_subgraph (diversified seeds ride the
    # subgraph path only; default recall stays byte-identical). The quota knobs
    # override the ``_SEED_*`` constants in ``okto_neuron/query.py``; None → code
    # default (subject_cap=3, entity_min=6, rel_min=4, scalar_max=10).
    seed_diversity: bool | None = None
    seed_subject_cap: int | None = Field(default=None, gt=0)
    seed_entity_min: int | None = Field(default=None, ge=0)
    seed_rel_min: int | None = Field(default=None, ge=0)
    seed_scalar_max: int | None = Field(default=None, ge=0)
    # ── Efficient-hybrid (task-12 fix A, owner reframe): blend budget knob ─────
    # Token budget for the source-block excerpt blended into every subgraph
    # answer context; the abstention escalation re-ask is bounded at 2x this.
    # Read STEP-DIRECT like the other ask-only knobs. None → code default
    # (``_ASK_SOURCE_BLOCK_BUDGET_DEFAULT`` in ``companion/__init__.py``, 6000).
    source_block_budget_tokens: int | None = Field(default=None, gt=0)

    @field_validator("provider", mode="after")
    @classmethod
    def _v_provider(cls, value: str | None) -> str | None:
        return _check_provider(value)

    @field_validator("api_key_env", mode="after")
    @classmethod
    def _v_api_key_env(cls, value: str | None) -> str | None:
        return _check_api_key_env(value)


class LLMConfig(BaseModel):
    """Per-step LLM configuration: a ``defaults`` baseline plus independent override
    blocks for each orchestrator moment (``extraction`` / ``judge`` /
    ``curator`` / ``relation_curator`` / ``ask``).

    Optional generation parameters are absent by default. Provider/model defaults
    apply until the user adds an override to a typed sampler field or a
    ``sampling_payload`` (defaults or a step). Legacy typed fields remain loadable
    but no longer materialize hidden values.

    ``allow_remote`` is a single vault-level gate: any resolved ``api_base`` (default
    or per-step) that is non-loopback requires it. Config *writes* stay loopback-only
    regardless (enforced at the API layer), so this flag only widens where the server
    may POST, never who may set config."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    allow_remote: bool = False
    # Backend capacity declaration. Aliases listed here are the ONLY ones
    # allowed to exceed one in-flight completion; every other alias clamps to 1
    # for both extraction and curation fan-out. The Gateway does not advertise
    # per-model parallel slots (``/model_group/info`` carries mode and supported
    # params only), so this is an explicit, user-visible boundary rather than an
    # inference from the model name. Clamping happens at the consumption sites
    # via :mod:`okto_neuron.config._capacity`, never by rewriting stored config,
    # so the UI can show configured vs effective and existing vault YAML stays
    # loadable.
    parallel_capable_models: list[str] = Field(
        default_factory=lambda: list(DEFAULT_PARALLEL_CAPABLE_MODELS)
    )
    defaults: LLMDefaults = Field(default_factory=LLMDefaults)
    extraction: StepLLM = Field(default_factory=StepLLM)
    judge: StepLLM = Field(default_factory=StepLLM)
    curator: StepLLM = Field(default_factory=StepLLM)
    relation_curator: StepLLM = Field(default_factory=StepLLM)
    ask: StepLLM = Field(default_factory=StepLLM)

    def _resolved_connection(self, merged: dict[str, Any]) -> ResolvedLLM:
        # Fold the parameters-only typed fields (see _PARAMETERS_ONLY_LLM_FIELDS)
        # into the generic map and drop them from ``merged`` — ResolvedLLM has
        # no matching field and is ``extra="forbid"``.
        parameters = dict(merged.get("parameters") or {})
        for name in _PARAMETERS_ONLY_LLM_FIELDS:
            value = merged.pop(name, None)
            if value is not None and name not in parameters:
                parameters[name] = value
        merged["parameters"] = parameters
        provider_ref = merged.get("provider_ref")
        if isinstance(provider_ref, str) and provider_ref:
            from okto_neuron.providers import ProviderRegistry, resolve_provider

            profile, credential_env = resolve_provider(provider_ref)
            registry = ProviderRegistry.load()
            if "llm" not in registry.provider_uses(profile.driver):
                raise ValueError(f"provider {provider_ref!r} does not support LLM calls")
            merged.update(
                provider=profile.driver,
                api_base=profile.api_base or merged["api_base"],
                api_key_env=credential_env,
                parameter_mode=profile.parameter_mode,
                request_timeout_s=profile.request_timeout_s,
            )
        else:
            merged["parameter_mode"] = "auto"
        return ResolvedLLM(**merged)

    def resolved_defaults(self) -> ResolvedLLM:
        """Return the defaults with an optional provider reference resolved."""

        return self._resolved_connection(self.defaults.model_dump())

    def resolved(self, step: StepName) -> ResolvedLLM:
        """Merge a step's overrides onto ``defaults`` into a concrete config."""
        base = self.defaults.model_dump()
        step_config = getattr(self, step)
        override = step_config.model_dump()
        # Per-field whole-value substitution: a step field left ``None``
        # inherits the default's value outright, a concrete step value wins
        # outright. This is also exactly the FROZEN resolution rule
        # ``sampling_payload`` needs (no per-key merge — see
        # ``StepLLM.sampling_payload``'s docstring): a step's own dict
        # (including ``{}``) replaces the default's dict wholesale here, and
        # a step that left it ``None`` inherits the default's dict wholesale.
        merged = {
            key: (base_val if override.get(key) is None else override[key])
            for key, base_val in base.items()
        }
        # Credentials are coupled to their provider endpoint. A step that
        # changes either provider or base URL must opt into a credential
        # reference explicitly; inheriting the default key could send it to a
        # different service.
        endpoint_changed = (
            step_config.provider is not None and step_config.provider != self.defaults.provider
        ) or (step_config.api_base is not None and step_config.api_base != self.defaults.api_base)
        if endpoint_changed and step_config.api_key_env is None:
            merged["api_key_env"] = None
        if endpoint_changed and step_config.provider_ref is None:
            merged["provider_ref"] = None

        return self._resolved_connection(merged)

    @model_validator(mode="after")
    def _validate_api_bases(self) -> Self:
        if not self.enabled:
            return self
        # Validate the resolved api_base of every step (a step may override it),
        # plus the bare defaults (covers the case where every step overrides it).
        resolved_defaults = self.resolved_defaults()
        _check_api_base(
            resolved_defaults.api_base,
            self.allow_remote or resolved_defaults.provider_ref is not None,
        )
        # A driver with no api_base of its own inherits THIS vault's default
        # base, which is loopback, so the URL check above passes vacuously and
        # the call still egresses. Gate those by driver name. A step that names
        # a ``provider_ref`` is exempt for the same reason as the URL check: the
        # profile carries its own ``allow_remote``, already enforced in
        # ``ProviderRegistry._validate_profile``.
        _check_remote_driver(
            resolved_defaults.provider,
            self.allow_remote or resolved_defaults.provider_ref is not None,
            where="llm.allow_remote",
        )
        for step in _STEP_NAMES:
            resolved = self.resolved(step)
            _check_api_base(
                resolved.api_base,
                self.allow_remote or resolved.provider_ref is not None,
            )
            _check_remote_driver(
                resolved.provider,
                self.allow_remote or resolved.provider_ref is not None,
                where="llm.allow_remote",
            )
        return self


class PrefilterConfig(BaseModel):
    """ADR 0015 D2 — deterministic pre-filter for low-value candidates.

    Rules 1–3 (predicate demotion, low-signal node demotion, near-dup literal
    collapse) run only when ``enabled`` is True (opt-in until evaluated).
    Rule 4 (``established_entity_fastpath``) is gated independently and defaults
    ON: it only fires where the candidate would merge into an already-live
    store node with no novel content, so skipping the curator verdict cannot
    change graph content, only cost. Demotion always means *queue* (reviewable
    via the existing review surface) — never silent discard."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    demote_predicates: list[str] = Field(
        default_factory=lambda: ["discusses", "mentions", "mentioned", "states", "stated"]
    )
    min_mentions: int = Field(default=1, ge=1)
    max_trivial_node_chars: int = Field(default=160, gt=0)
    established_entity_fastpath: bool = True


class ConsolidationConfig(BaseModel):
    """Confidence-gate policy (net-new; defaults mirror
    :class:`okto_neuron.consolidate.gate.GateConfig`)."""

    model_config = ConfigDict(extra="forbid")

    auto_commit_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    review_on_contradiction: bool = True
    # ADR 0040 ablation controls. Defaults preserve the production semantic
    # pipeline. Disabling either stage is fail-closed: affected candidates are
    # queued for review and the corresponding provider is never called.
    type_adjudication_enabled: bool = True
    relation_curator_enabled: bool = True
    audit_superseded_nodes_with_llm: bool = False
    audit_superseded_relations_with_llm: bool = False
    # ADR 0015 D1 — bounded-concurrency curation fan-out. 1 = today's strictly
    # sequential behavior (default). Raising it only pays when the LLM endpoint
    # accepts concurrent requests (llama.cpp needs --parallel N > 1).
    curation_max_concurrent: int = Field(default=1, ge=1, le=32)
    # Wall-clock deadline for one curation/judge call (issue #24). Any finite
    # value routes the call through the killable helper process, so a server that
    # accepts the connection and never answers cannot hold a writer lock forever.
    # ``None`` opts out (unbounded), which is only safe for a trusted local model.
    curation_call_timeout_s: float | None = Field(default=DEFAULT_CURATION_CALL_TIMEOUT_S, gt=0.0)
    # ADR 0015 D4 — block-keyed batched curation. 1 = off (today's per-candidate
    # calls, default). >1 evaluates up to K same-block candidates per LLM call
    # with schema-constrained output and per-candidate single-call fallback.
    curation_batch_size: int = Field(default=1, ge=1, le=32)
    # ADR 0015 D2 — deterministic pre-filter for low-value candidates.
    prefilter: PrefilterConfig = Field(default_factory=PrefilterConfig)


class CurationSchedulerConfig(BaseModel):
    """Continuous-curation-loop policy (ADR 0009 P4).

    The serve process runs a debounced in-process scheduler that auto-submits
    PROPOSE/DETECT sweeps (``reconcile-propose`` + ``detect-drift``) to the
    curation job queue as the user ingests, so review surfaces populate and drift
    is detected without manual triggering. The loop is PROPOSE/DETECT ONLY — it
    NEVER auto-runs a destructive/irreversible op (no apply/heal/rebuild/reembed/
    reset; that allowlist is a code constant, not config).

    Flat shape (mirrors :class:`ConsolidationConfig`, NOT the per-step
    :class:`LLMConfig`). Re-read live per scheduler tick, so a toggle takes effect
    without a restart.

    Conservative-but-ON defaults so curation is offloaded out of the box:
    - ``enabled`` True — the loop runs by default.
    - ``quiet_debounce_s`` 60 — wait 60s of ingest quiet before sweeping (avoids
      sweeping mid-bulk-import).
    - ``min_interval_s`` 3600 — at most one auto sweep per hour (anti-thrash floor;
      bounds the judge-LLM cost of auto-propose).
    Conservative means low frequency + read-only-only, NOT off."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    quiet_debounce_s: int = Field(default=60, ge=0)
    min_interval_s: int = Field(default=3600, gt=0)
    # Watchdog (issue #24): a running curation job that reports no progress for
    # this many seconds is failed (read-only jobs) and surfaced as
    # ``curation_job_stalled`` on /api/v1/status. ``None`` disables it. Keep it
    # above ``consolidation.curation_call_timeout_s`` so the call deadline fires
    # first and the watchdog is only the backstop.
    job_stall_timeout_s: float | None = Field(default=DEFAULT_JOB_STALL_TIMEOUT_S, gt=0.0)


class UpkeepConfig(BaseModel):
    """Judge-driven graph upkeep policy (ADR 0017 workstream B)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_pairs_per_run: int = Field(default=30, ge=1, le=200)
    min_support: int = Field(default=2, ge=1, le=100)
    cluster_threshold: float = Field(default=0.80, ge=0.5, le=0.99)
    auto_fold_threshold: float = Field(default=0.85, ge=0.5, le=1.0)


# Directory names pruned from every watched-root walk (and from one-shot
# folder ingest) before descent: hidden dirs (.state, .remember, .git, ...)
# and tooling caches. Matching is fnmatch on each path COMPONENT — a junk
# dir prunes its entire subtree without stat-walking it.
DEFAULT_IGNORE_DIR_GLOBS: tuple[str, ...] = (".*", "__pycache__", "node_modules")


class IngestConfig(BaseModel):
    """Ingestion policy (ADR 0023 block-hash skip, ADR 0024 sub-chunk diff,
    and ADR 0038 configurable byte windows). Default ON since the 2026-07-02
    remediation: re-ingesting
    an unchanged source skips extraction entirely, an edited source extracts
    only its changed hunks, and removals detach their claims.

    The ``OKTO_NEURON_INCREMENTAL_INGEST`` / ``OKTO_NEURON_SUBCHUNK_INGEST`` env
    vars are two-way overrides: ``1`` forces on, ``0`` forces off, unset
    defers to this config."""

    model_config = ConfigDict(extra="forbid")

    incremental: bool = True
    subchunk: bool = True
    chunk_size_bytes: int = Field(default=6_000, ge=256, le=1_000_000)
    chunk_overlap_bytes: int = Field(default=0, ge=0, le=999_999)

    @model_validator(mode="after")
    def _check_chunk_overlap(self) -> Self:
        if self.chunk_overlap_bytes >= self.chunk_size_bytes:
            raise ValueError("ingest.chunk_overlap_bytes must be smaller than chunk_size_bytes")
        return self


class FolderWatchConfig(BaseModel):
    """Continuous folder-monitoring policy (ADR 0025).

    The serve process runs a global polling loop that auto-ingests files when
    they change in any registered watched root. The loop is vault-aware: it
    iterates ALL vaults (not just the active one) that declare ``roots`` here.

    Conservative-but-OFF defaults so the feature is inert unless configured:
    - ``enabled`` False — the loop is off by default; set True to activate.
    - ``poll_interval_s`` 5 — re-stat every file every 5s.
    - ``quiet_debounce_s`` 3 — file must have stopped changing for 3s before ingest.
    - ``min_interval_s`` 10 — minimum gap between two ingests of the same file.
    - ``recursive`` True — walk subdirectories.
    - ``ignore_globs`` — editor temp/swap patterns skipped without hashing.
    - ``ignore_dir_globs`` — directory-name patterns pruned before descent
      (hidden dirs, caches). Keeps internal state dirs (``.state/``,
      ``.remember/``, ``.git/``) out of the graph entirely.
    - ``roots`` — absolute paths to watch (empty by default; add via ``okto-neuron
      watch-folder add <path>`` or by editing okto-neuron.yaml directly).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    poll_interval_s: float = Field(default=5.0, gt=0.0)
    quiet_debounce_s: float = Field(default=3.0, ge=0.0)
    min_interval_s: float = Field(default=10.0, ge=0.0)
    recursive: bool = True
    ignore_globs: list[str] = Field(default_factory=lambda: ["*.swp", "*.tmp", "4913", "*~", ".#*"])
    ignore_dir_globs: list[str] = Field(default_factory=lambda: list(DEFAULT_IGNORE_DIR_GLOBS))
    roots: list[str] = Field(default_factory=list)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge: nested mappings merge field-wise, scalars/lists replace.

    Needed so a two-level ``llm`` patch (``{"llm": {"extraction": {"temperature": 0.5}}}``)
    updates only that one field instead of clobbering the whole ``extraction`` block.
    """
    out = dict(base)
    for key, value in patch.items():
        if key == "sampling_payload":
            # sampling_payload is one replaceable value, never deep-merged:
            # recursively merging it would silently violate the field's
            # frozen, no-deep-merge semantics (a PATCH that sets a new
            # payload must replace the stored one wholesale, never fold its
            # keys into what was there).
            out[key] = value
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _project_patch(patch: Any, canonical: Any) -> Any:
    """Keep the patch shape while taking canonicalized values from a validated config."""

    if isinstance(patch, dict) and isinstance(canonical, dict):
        return {key: _project_patch(value, canonical.get(key)) for key, value in patch.items()}
    return canonical


class VaultConfig(BaseModel):
    """Typed per-vault configuration loaded from okto-neuron.yaml."""

    model_config = ConfigDict(extra="allow")

    marginalia_yaml_version: int = 1
    vault_id: str | None = None
    inherits_application_defaults: bool = False
    federation_opt_in: bool = False
    packs: list[str] = Field(default_factory=lambda: ["core", "research", "personal"])
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    consolidation: ConsolidationConfig = Field(default_factory=ConsolidationConfig)
    # ADR 0009 P4 continuous-curation loop. Top-level key ``curation:`` — named to
    # leave room for P5's per-operation policy to nest under or beside it.
    curation: CurationSchedulerConfig = Field(default_factory=CurationSchedulerConfig)
    upkeep: UpkeepConfig = Field(default_factory=UpkeepConfig)
    # ADR 0025 continuous folder monitoring. Top-level key ``folder_watch:``.
    # Off by default; set ``folder_watch.enabled: true`` and list ``roots`` to activate.
    folder_watch: FolderWatchConfig = Field(default_factory=FolderWatchConfig)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    # Stays ``None`` here deliberately (do not default this to
    # ``GrafxStorageConfig``): this same field default is also what
    # ``load()`` below falls back to for a real vault whose on-disk
    # ``marginalia.yaml`` has no ``storage`` key at all -- a legacy vault
    # that predates any of ladybug/grafx/neo4j being pinnable. Every read
    # site's own fallback (``vault.py::_check_backend_pin``, ``store/
    # vault.py::_read_pinned_backends``, ``cli/kg.py::_resolve_pinned_backend``,
    # ``reconcile/heal.py``) already treats ``storage is None`` as
    # ``"ladybug"``, so this field default must keep meaning "unpinned /
    # legacy", never "pinned to the new default". A genuinely NEW vault gets
    # ``storage`` written explicitly at creation time instead -- see
    # ``DEFAULT_NEW_VAULT_BACKEND`` above.
    storage: StorageConfig | None = None
    index: IndexConfig | None = None

    @field_validator("packs", mode="after")
    @classmethod
    def _require_packs(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("packs must contain at least one pack")
        return value

    @classmethod
    def default(cls) -> Self:
        """Return the v1 default used when scaffolding a new vault.

        Intentionally storage-less (``storage`` stays its field default,
        ``None``) even though ``DEFAULT_NEW_VAULT_BACKEND`` is ``"grafx"``:
        this classmethod doubles as ``load()``'s own deep-merge baseline for
        EVERY existing vault below, not only a brand-new one. Giving it a
        non-None ``storage`` would silently re-pin any legacy no-``storage``-
        key vault to grafx the next time it is merely opened. A real fresh
        vault gets ``storage`` written explicitly by its creation entry
        point (``Vault._write_config``, ``cli/kg.py``'s
        ``_write_kg_init_vault_config``) using ``DEFAULT_NEW_VAULT_BACKEND``
        -- never through this classmethod.
        """
        return cls(marginalia_yaml_version=1, federation_opt_in=False)

    @classmethod
    def application_defaults_path(cls) -> Path:
        return default_app_home() / "defaults.yaml"

    @classmethod
    def load_application_defaults(cls) -> Self:
        """Load the application baseline, falling back to code defaults."""

        path = cls.application_defaults_path()
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except FileNotFoundError:
            return cls.default()
        except yaml.YAMLError as error:
            raise ConfigParseError(path, line=_yaml_error_line(error), cause=error) from error
        except OSError as error:
            raise ConfigParseError(path, cause=error) from error
        return cls._validate_data(data, path)

    @classmethod
    def load(
        cls,
        vault_path: Path | str,
        *,
        application_defaults: Self | None = None,
    ) -> Self:
        """Load a vault config, extending application defaults only when opted in."""
        config_path = _config_file_for(vault_path)
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except FileNotFoundError as error:
            raise ConfigNotFound(config_path, cause=error) from error
        except yaml.YAMLError as error:
            raise ConfigParseError(
                config_path,
                line=_yaml_error_line(error),
                cause=error,
            ) from error
        except OSError as error:
            raise ConfigParseError(config_path, cause=error) from error

        if not isinstance(data, dict):
            return cls._validate_data(data, config_path)
        if "marginalia_yaml_version" not in data:
            _warn_missing_version_once(config_path)
        if data.get("inherits_application_defaults") is True:
            baseline = application_defaults or cls.load_application_defaults()
        else:
            baseline = cls.default()
        merged = _deep_merge(
            baseline.model_dump(mode="json", exclude_none=True),
            data,
        )
        return cls._validate_data(merged, config_path)

    # ── writable surface (web-UI config-write) ──────────────────────────────
    WRITABLE_BLOCKS: ClassVar[tuple[str, ...]] = (
        "embedding",
        "llm",
        "consolidation",
        "upkeep",
        "packs",
        "folder_watch",
        "ingest",
    )
    # Embedding fields whose change requires a vectors-only re-embed (the stored
    # vectors are now computed by a different model/width and would score wrong).
    # The transport knobs (api_base/api_key_env/allow_remote) only affect *where*
    # the embedder runs, not the vector space, so they take effect live.
    REEMBED_FIELDS: ClassVar[tuple[str, ...]] = (
        "embedding.provider_ref",
        "embedding.provider",
        "embedding.model",
        "embedding.dimension",
    )
    # These fields change graph materialization policy rather than vector
    # compatibility. Saving is allowed, but existing sources need a semantic
    # rebuild before the stored graph represents the new policy.
    SEMANTIC_REBUILD_FIELDS: ClassVar[tuple[str, ...]] = (
        "consolidation.type_adjudication_enabled",
        "consolidation.relation_curator_enabled",
    )

    @classmethod
    def load_raw(cls, vault_path: Path | str) -> dict[str, Any]:
        """Return the raw yaml mapping, preserving keys this model does not type.

        Used by the config-write path so unknown top-level keys (``vault_id``,
        ``storage``, compat flags) survive a partial PATCH untouched.
        """
        config_path = _config_file_for(vault_path)
        try:
            with config_path.open("r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle) or {}
        except FileNotFoundError:
            return {}
        except yaml.YAMLError as error:
            raise ConfigParseError(
                config_path, line=_yaml_error_line(error), cause=error
            ) from error
        except OSError as error:
            raise ConfigParseError(config_path, cause=error) from error
        if not isinstance(data, dict):
            error = TypeError("okto-neuron.yaml must contain a mapping")
            raise ConfigParseError(config_path, cause=error) from error
        return data

    @classmethod
    def enable_application_inheritance(cls, vault_path: Path | str) -> Self:
        """Compact a newly scaffolded vault to metadata plus inherited defaults."""

        raw = cls.load_raw(vault_path)
        compact = {key: value for key, value in raw.items() if key not in cls.WRITABLE_BLOCKS}
        compact["marginalia_yaml_version"] = 1
        compact["inherits_application_defaults"] = True
        config_path = _config_file_for(vault_path)
        config_path.write_text(yaml.safe_dump(compact, sort_keys=False), encoding="utf-8")
        return cls.load(vault_path)

    @classmethod
    def _validate_writable_patch(cls, patch: dict[str, Any]) -> None:
        unknown = set(patch) - set(cls.WRITABLE_BLOCKS)
        if unknown:
            raise ValueError(
                f"unknown config field(s): {sorted(unknown)}; "
                f"writable blocks are {list(cls.WRITABLE_BLOCKS)}"
            )

        if "packs" not in patch:
            return
        from okto_neuron.packs import BUILTIN

        requested = patch["packs"]
        if not isinstance(requested, list) or not all(isinstance(pack, str) for pack in requested):
            raise ValueError("packs must be a list of strings")
        bad = [pack for pack in requested if pack not in BUILTIN]
        if bad:
            raise ValueError(f"unknown pack(s): {bad}; built-in packs are {sorted(BUILTIN)}")

    @classmethod
    def apply_application_defaults_patch(cls, patch: dict[str, Any]) -> tuple[Self, list[str]]:
        """Validate and persist the application-wide baseline."""

        cls._validate_writable_patch(patch)
        before = cls.load_application_defaults()
        merged = before.model_dump(mode="json", exclude_none=False)
        for block, value in patch.items():
            merged[block] = (
                _deep_merge(merged[block], value)
                if isinstance(value, dict) and isinstance(merged.get(block), dict)
                else value
            )
        validated = cls.model_validate(merged)
        persisted = validated.model_dump(mode="json", exclude_none=True)
        for metadata in ("vault_id", "inherits_application_defaults", "storage"):
            persisted.pop(metadata, None)
        path = cls.application_defaults_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        path.write_text(yaml.safe_dump(persisted, sort_keys=False), encoding="utf-8")
        os.chmod(path, 0o600)
        return validated, _changed_paths(before, validated)

    @classmethod
    def apply_patch(cls, vault_path: Path | str, patch: dict[str, Any]) -> tuple[Self, list[str]]:
        """Validate ``patch`` against the writable surface, persist to
        ``okto-neuron.yaml``, and return ``(reloaded_config, changed_paths)``.

        ``patch`` is a partial mapping keyed by writable block name. Nested blocks
        (``embedding``/``llm``/``consolidation``) merge **recursively** — so a
        two-level ``llm`` patch touches one step field without clobbering the rest.
        ``packs`` replaces wholesale. Unknown top-level keys are rejected.
        ``changed_paths`` is the dotted list of fields whose value actually changed
        (used by the caller to decide live-vs-reembed).
        """
        cls._validate_writable_patch(patch)

        raw = cls.load_raw(vault_path)
        before = cls.load(vault_path)
        effective = before.model_dump(mode="json", exclude_none=False)
        for block, value in patch.items():
            effective[block] = (
                _deep_merge(effective[block], value)
                if isinstance(value, dict) and isinstance(effective.get(block), dict)
                else value
            )

        # Validate the full merged document so range/scheme/enum rules fire and
        # an invalid PATCH never reaches disk.
        validated = cls.model_validate(effective)
        canonical = validated.model_dump(mode="json", exclude_none=False)

        # Persist only the fields explicitly patched. Inheriting vaults therefore
        # continue to receive later application-default changes for untouched fields.
        persisted = dict(raw)
        for block, value in patch.items():
            projected = _project_patch(value, canonical[block])
            if isinstance(projected, dict) and isinstance(persisted.get(block), dict):
                persisted[block] = _deep_merge(persisted[block], projected)
            else:
                persisted[block] = projected
        persisted.setdefault("marginalia_yaml_version", validated.marginalia_yaml_version)

        config_path = _config_file_for(vault_path)
        config_path.write_text(yaml.safe_dump(persisted, sort_keys=False), encoding="utf-8")

        changed = _changed_paths(before, validated)
        return validated, changed

    @classmethod
    def _validate_data(cls, data: Any, path: Path) -> Self:
        if not isinstance(data, dict):
            error = TypeError("okto-neuron.yaml must contain a mapping")
            raise ConfigParseError(path, cause=error) from error

        if "marginalia_yaml_version" not in data:
            _warn_missing_version_once(path)

        found_version = data.get("marginalia_yaml_version", 1)
        if found_version not in SUPPORTED_YAML_VERSIONS:
            raise ConfigVersionUnsupported(
                path,
                found_version,
                supported_versions=SUPPORTED_YAML_VERSIONS,
            )

        try:
            return cls.model_validate(data)
        except ValidationError as error:
            raise ConfigParseError(path, cause=error) from error


def _diff_paths(before: Any, after: Any, prefix: str) -> list[str]:
    """Dotted paths whose value differs, recursing into nested mappings."""
    if isinstance(before, dict) or isinstance(after, dict):
        b = before if isinstance(before, dict) else {}
        a = after if isinstance(after, dict) else {}
        out: list[str] = []
        for key in sorted(set(b) | set(a)):
            out.extend(_diff_paths(b.get(key), a.get(key), f"{prefix}.{key}"))
        return out
    return [prefix] if before != after else []


def _changed_paths(before: VaultConfig, after: VaultConfig) -> list[str]:
    """Dotted field paths whose value differs between two configs (writable blocks)."""
    changed: list[str] = []
    for block in VaultConfig.WRITABLE_BLOCKS:
        if block == "packs":
            if list(before.packs) != list(after.packs):
                changed.append("packs")
            continue
        b = getattr(before, block).model_dump(mode="json")
        a = getattr(after, block).model_dump(mode="json")
        changed.extend(_diff_paths(b, a, block))
    return changed


__all__ = [
    "ConsolidationConfig",
    "CurationSchedulerConfig",
    "CustomStorageConfig",
    "DEFAULT_NEW_VAULT_BACKEND",
    "EndpointClass",
    "EmbeddingConfig",
    "GrafxStorageConfig",
    "IndexConfig",
    "LLMConfig",
    "LLMDefaults",
    "LadybugStorageConfig",
    "Neo4jStorageConfig",
    "NeptuneStorageConfig",
    "ResolvedLLM",
    "RetryConfig",
    "StepLLM",
    "StepName",
    "StorageConfig",
    "UNCONDITIONALLY_REMOTE_DRIVERS",
    "UpkeepConfig",
    "VaultConfig",
    "api_base_is_loopback",
    "classify_api_base",
]
