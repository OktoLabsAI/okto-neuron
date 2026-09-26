"""Application-scoped named credentials and provider connections."""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from okto_neuron._compat import secret_env as _secret_env
from okto_neuron.config._app_config import default_app_home

ParameterMode = Literal["safe", "local_extended"]
ProviderUse = Literal["llm", "embedding"]

_LITELLM_PROXY_MODEL_TIMEOUT_SECONDS = 5
_LITELLM_PROXY_MODEL_CACHE_SECONDS = 60.0


@dataclass(frozen=True)
class LiteLLMProxyModel:
    """One model alias advertised by a LiteLLM Gateway."""

    id: str
    mode: str | None
    supported_openai_params: frozenset[str] | None


_litellm_proxy_model_cache: dict[
    tuple[str, str | None], tuple[float, tuple[LiteLLMProxyModel, ...], bool]
] = {}
_litellm_proxy_model_lock = threading.Lock()

# Direct inference drivers whose private/loopback OpenAI-compatible endpoints
# may intentionally accept raw Qwen/vLLM sampler fields. Generic proxies are
# deliberately absent because they may route to hosted backends.
LOCAL_EXTENDED_DRIVERS = frozenset(
    {
        "custom",
        "custom_openai",
        "hosted_vllm",
        "llamafile",
        "lm_studio",
        "ollama",
        "ollama_chat",
        "oobabooga",
        "openai",
        "openai_like",
        "text-completion-openai",
        "triton",
        "vllm",
    }
)


def litellm_proxy_root(api_base: str) -> str:
    """Normalize an OpenAI-style proxy URL to the LiteLLM management root."""

    parsed = urlparse(api_base)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3].rstrip("/")
    return parsed._replace(path=path, params="", query="", fragment="").geturl().rstrip("/")


@dataclass(frozen=True)
class OpenAIBase:
    """Canonical derivation of one OpenAI-compatible endpoint base URL.

    ``root`` is the server root with no ``/v1`` suffix; ``models_url`` and
    ``api_base`` are the two derived values every discovery probe and
    completion call must use. LiteLLM's OpenAI-compatible adapters append
    ``/chat/completions`` to ``api_base``.
    """

    root: str
    models_url: str
    api_base: str


#: LiteLLM drivers whose HTTP contract is the plain OpenAI one under ``/v1``:
#: discovery at ``{root}/v1/models`` and completion at
#: ``{root}/v1/chat/completions`` (LiteLLM appends the final path to
#: ``api_base``). These — and only these — have their base URL canonicalized
#: by :func:`resolve_openai_base` on every read AND before it is persisted.
#: Providers with their own URL shapes (Anthropic ``/v1/messages``, Triton
#: ``/v2``, Azure deployment paths) are deliberately absent.
OPENAI_V1_DRIVERS = frozenset(
    {
        "custom",
        "custom_openai",
        "hosted_vllm",
        "llamafile",
        "lm_studio",
        "openai",
        "openai_like",
        "text-completion-openai",
        "vllm",
    }
)

#: Drivers that SERVE the OpenAI ``/v1`` contract for discovery but whose
#: LiteLLM runtime speaks a different URL shape (native Ollama ``/api/chat``,
#: Oobabooga's own ``{base}/v1/chat/completions`` suffix, the LiteLLM gateway
#: management API). Their discovery probe is canonicalized; the persisted
#: base and the runtime base are left exactly as entered.
OPENAI_V1_DISCOVERY_ONLY_DRIVERS = frozenset(
    {
        "litellm_proxy",
        "ollama",
        "ollama_chat",
        "oobabooga",
    }
)


#: A whole ``/v<digits>`` final path segment — see ``resolve_openai_base``.
_VERSION_SEGMENT_RE = re.compile(r"/v\d+$")


def resolve_openai_base(api_base: str) -> OpenAIBase:
    """Canonicalize an OpenAI-compatible base URL to its derived endpoints.

    Accepts the server root (``http://host:port``, with or without a trailing
    slash) and the versioned form (``http://host:port/v1``, with or without a
    trailing slash, including subpath variants such as
    ``http://host:port/proxy/v1``) and derives the SAME two values either way,
    so the form the user typed can never change which endpoint discovery or a
    completion hits (idempotent normalization):

    - ``models_url`` = ``{root}/v1/models``
    - ``api_base``   = ``{root}/v1`` — the base LiteLLM's OpenAI-compatible
      adapters append ``/chat/completions`` (or ``/completions``) to.

    This is the single source of truth shared by onboarding discovery,
    onboarding verify, the Config UI's test/validate endpoints, the config
    save paths, and the runtime provider — replacing the previous split where
    discovery probed ``{base}/models`` (only 200 when the server also serves a
    root-level ``/models``) while completions posted ``{base}/chat/completions`
    (only 200 when the user remembered to include ``/v1``).

    A query string on the base (Azure-style ``?api-version=``) rides along on
    every derived URL — such bases need it on each request — always kept at
    the end of the full URL.

    An explicit trailing version segment other than ``/v1`` (Z.ai's
    ``https://api.z.ai/api/coding/paas/v4``, whose chat endpoint is
    ``/v4/chat/completions``) is kept as the version: ``api_base`` stays
    ``{root}/v4`` and discovery probes ``{root}/v4/models``. Appending ``/v1``
    there produced ``/v4/v1/chat/completions`` (2026-09-19). Only a whole
    ``/v<digits>`` final segment counts; ``/v1x`` is still a plain subpath.
    """

    parsed = urlparse(api_base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"unresolvable api_base {api_base!r}; expected an http(s) URL with a host")
    path = parsed.path.rstrip("/")
    version = "/v1"
    version_match = _VERSION_SEGMENT_RE.search(path)
    if version_match:
        version = version_match.group(0)
        path = path[: version_match.start()].rstrip("/")

    def derived(suffix: str) -> str:
        from urllib.parse import urlunparse

        combined = path + suffix
        if not combined:
            combined = "/"
        return urlunparse(
            (
                parsed.scheme,
                parsed.netloc,
                combined,
                parsed.params,
                parsed.query,
                parsed.fragment,
            )
        ).rstrip("/")

    return OpenAIBase(
        root=derived(""),
        models_url=derived(f"{version}/models"),
        api_base=derived(version),
    )


def litellm_proxy_models(
    *,
    api_base: str,
    api_key_env: str | None,
    refresh: bool = False,
) -> tuple[LiteLLMProxyModel, ...]:
    """Return the gateway's model catalog through LiteLLM's official client."""

    root = litellm_proxy_root(api_base)
    cache_key = (root, api_key_env)
    now = time.monotonic()
    with _litellm_proxy_model_lock:
        cached = _litellm_proxy_model_cache.get(cache_key)
        if (
            not refresh
            and cached is not None
            and now - cached[0] < _LITELLM_PROXY_MODEL_CACHE_SECONDS
        ):
            if cached[2]:
                raise RuntimeError("LiteLLM gateway model catalog unavailable")
            return cached[1]

        from litellm.proxy.client import Client

        try:
            client = Client(
                base_url=root,
                api_key=_secret_env(api_key_env) if api_key_env else None,
                timeout=_LITELLM_PROXY_MODEL_TIMEOUT_SECONDS,
            )
            payload = client.http.request("GET", "/model_group/info", allow_redirects=False)
        except Exception:
            _litellm_proxy_model_cache[cache_key] = (now, (), True)
            raise

        entries = payload.get("data", []) if isinstance(payload, dict) else []
        models: list[LiteLLMProxyModel] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            model_id = entry.get("model_group")
            mode = entry.get("mode")
            model_info = entry.get("model_info")
            if not isinstance(mode, str) and isinstance(model_info, dict):
                mode = model_info.get("mode")
            params = entry.get("supported_openai_params")
            if not isinstance(model_id, str) or not model_id:
                continue
            models.append(
                LiteLLMProxyModel(
                    id=model_id,
                    mode=mode if isinstance(mode, str) else None,
                    supported_openai_params=(
                        frozenset(param for param in params if isinstance(param, str))
                        if isinstance(params, list)
                        else None
                    ),
                )
            )

        result = tuple(models)
        _litellm_proxy_model_cache[cache_key] = (now, result, False)
        return result


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug:
        raise ValueError("name must contain at least one letter or number")
    return slug


class CredentialRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    env_name: str
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)

    @field_validator("id", "name", "env_name", mode="after")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("credential fields must not be blank")
        return value


class ProviderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    driver: str
    api_base: str | None = None
    allow_remote: bool = True
    credential_id: str | None = None
    parameter_mode: ParameterMode = "safe"
    # Optional Okto Neuron-owned wall/read deadline for one LiteLLM completion.
    # ``None`` deliberately delegates timeout policy to LiteLLM/the provider;
    # ingest cancellation remains available through the owned helper process.
    request_timeout_s: float | None = Field(default=None, gt=0.0)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)

    @field_validator("id", "name", "driver", mode="after")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("provider fields must not be blank")
        return value


class ProviderRegistryDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    credentials: list[CredentialRecord] = Field(default_factory=list)
    providers: list[ProviderProfile] = Field(default_factory=list)


class ProviderRegistry:
    """Small YAML registry; secret values remain in the existing env store."""

    def __init__(self, document: ProviderRegistryDocument, path: Path) -> None:
        self.document = document
        self.path = path

    @classmethod
    def path_for_user(cls) -> Path:
        return default_app_home() / "providers.yaml"

    @classmethod
    def load(cls, path: Path | None = None) -> "ProviderRegistry":
        selected = path or cls.path_for_user()
        try:
            raw = yaml.safe_load(selected.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            raw = {}
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(f"could not read provider registry: {exc}") from exc
        try:
            document = ProviderRegistryDocument.model_validate(raw)
        except ValueError as exc:
            raise ValueError(f"invalid provider registry: {exc}") from exc
        registry = cls(document, selected)
        registry._validate_references()
        return registry

    def save(self) -> None:
        self._validate_references()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        payload = self.document.model_dump(mode="json", exclude_none=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                yaml.safe_dump(payload, handle, sort_keys=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def credential(self, credential_id: str) -> CredentialRecord:
        for record in self.document.credentials:
            if record.id == credential_id:
                return record
        raise ValueError(f"credential not found: {credential_id}")

    def provider(self, provider_id: str) -> ProviderProfile:
        for profile in self.document.providers:
            if profile.id == provider_id:
                return profile
        raise ValueError(f"provider not found: {provider_id}")

    def add_credential(self, name: str) -> CredentialRecord:
        credential_id = _slug(name)
        if any(record.id == credential_id for record in self.document.credentials):
            raise ValueError(f"credential already exists: {credential_id}")
        env_name = f"OKTO_NEURON_CREDENTIAL_{credential_id.upper().replace('-', '_')}"
        record = CredentialRecord(id=credential_id, name=name.strip(), env_name=env_name)
        self.document.credentials.append(record)
        return record

    def update_credential_name(self, credential_id: str, name: str) -> CredentialRecord:
        record = self.credential(credential_id)
        record.name = name.strip()
        if not record.name:
            raise ValueError("credential name must not be blank")
        record.updated_at = _now()
        return record

    def remove_credential(self, credential_id: str) -> CredentialRecord:
        record = self.credential(credential_id)
        consumers = [p.id for p in self.document.providers if p.credential_id == credential_id]
        if consumers:
            raise ValueError(f"credential is used by provider(s): {', '.join(sorted(consumers))}")
        self.document.credentials = [
            existing for existing in self.document.credentials if existing.id != credential_id
        ]
        return record

    def add_provider(
        self,
        *,
        name: str,
        driver: str,
        api_base: str | None,
        allow_remote: bool = True,
        credential_id: str | None,
        parameter_mode: ParameterMode,
        request_timeout_s: float | None = None,
    ) -> ProviderProfile:
        provider_id = _slug(name)
        if any(profile.id == provider_id for profile in self.document.providers):
            raise ValueError(f"provider already exists: {provider_id}")
        if api_base is not None and driver in OPENAI_V1_DRIVERS:
            # Canonicalize at save time so the registry (and what the Config UI
            # shows) holds the same base the runtime uses: server root + /v1.
            api_base = resolve_openai_base(api_base).api_base
        profile = ProviderProfile(
            id=provider_id,
            name=name.strip(),
            driver=driver,
            api_base=api_base,
            allow_remote=allow_remote,
            credential_id=credential_id,
            parameter_mode=parameter_mode,
            request_timeout_s=request_timeout_s,
        )
        self._validate_profile(profile)
        self.document.providers.append(profile)
        return profile

    def update_provider(self, provider_id: str, patch: dict[str, object]) -> ProviderProfile:
        profile = self.provider(provider_id)
        unknown = set(patch) - {
            "name",
            "driver",
            "api_base",
            "allow_remote",
            "credential_id",
            "parameter_mode",
            "request_timeout_s",
        }
        if unknown:
            raise ValueError(f"unknown provider field(s): {sorted(unknown)}")
        updated_patch = dict(patch)
        new_driver = updated_patch.get("driver", profile.driver)
        if (
            isinstance(new_driver, str)
            and new_driver in OPENAI_V1_DRIVERS
            and isinstance(updated_patch.get("api_base"), str)
        ):
            # Same canonicalization as add_provider (server root + /v1).
            updated_patch["api_base"] = resolve_openai_base(updated_patch["api_base"]).api_base
        updated = profile.model_copy(update={**updated_patch, "updated_at": _now()})
        updated = ProviderProfile.model_validate(updated.model_dump())
        self._validate_profile(updated)
        index = self.document.providers.index(profile)
        self.document.providers[index] = updated
        return updated

    def remove_provider(self, provider_id: str) -> ProviderProfile:
        profile = self.provider(provider_id)
        self.document.providers = [
            existing for existing in self.document.providers if existing.id != provider_id
        ]
        return profile

    def provider_uses(self, driver: str) -> list[ProviderUse]:
        from okto_neuron.config._vault import _EMBEDDING_PROVIDERS, _LLM_PROVIDERS

        uses: list[ProviderUse] = []
        if driver in _LLM_PROVIDERS:
            uses.append("llm")
        if driver in _EMBEDDING_PROVIDERS:
            uses.append("embedding")
        return uses

    def credential_env_for(self, profile: ProviderProfile) -> str | None:
        if profile.credential_id is None:
            return None
        return self.credential(profile.credential_id).env_name

    def _validate_references(self) -> None:
        credential_ids = {record.id for record in self.document.credentials}
        provider_ids: set[str] = set()
        for profile in self.document.providers:
            if profile.id in provider_ids:
                raise ValueError(f"duplicate provider id: {profile.id}")
            provider_ids.add(profile.id)
            if profile.credential_id is not None and profile.credential_id not in credential_ids:
                raise ValueError(
                    f"provider {profile.id} references missing credential {profile.credential_id}"
                )
            self._validate_profile(profile)

    def _validate_profile(self, profile: ProviderProfile) -> None:
        uses = self.provider_uses(profile.driver)
        if not uses:
            raise ValueError(f"unknown provider driver: {profile.driver}")
        if profile.credential_id is not None:
            self.credential(profile.credential_id)
        from okto_neuron.config._vault import (
            _check_api_base,
            _check_remote_driver,
            classify_api_base,
        )

        # Driver-name gate FIRST, and outside the ``api_base`` branch below.
        # ``_check_api_base`` can only judge a URL; a driver that never sets one
        # (the CLI pseudo-providers, ``chatgpt``) used to skip the gate
        # altogether and egress under ``allow_remote: false``.
        _check_remote_driver(
            profile.driver, profile.allow_remote, where="this provider's allow_remote"
        )
        if profile.api_base is not None:
            endpoint_class = classify_api_base(profile.api_base, resolve=False)
            _check_api_base(profile.api_base, allow_remote=profile.allow_remote)
        else:
            endpoint_class = None
        if profile.parameter_mode == "local_extended":
            if profile.driver not in LOCAL_EXTENDED_DRIVERS:
                raise ValueError("local_extended is only valid for a direct local inference driver")
            if endpoint_class not in {"loopback", "private"}:
                raise ValueError("local_extended requires a loopback or private provider endpoint")


def resolve_provider(provider_id: str) -> tuple[ProviderProfile, str | None]:
    registry = ProviderRegistry.load()
    profile = registry.provider(provider_id)
    return profile, registry.credential_env_for(profile)


__all__ = [
    "CredentialRecord",
    "LiteLLMProxyModel",
    "LOCAL_EXTENDED_DRIVERS",
    "OPENAI_V1_DISCOVERY_ONLY_DRIVERS",
    "OPENAI_V1_DRIVERS",
    "OpenAIBase",
    "ParameterMode",
    "ProviderProfile",
    "ProviderRegistry",
    "ProviderRegistryDocument",
    "litellm_proxy_root",
    "litellm_proxy_models",
    "resolve_openai_base",
    "resolve_provider",
]
