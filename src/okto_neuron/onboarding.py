"""First-run onboarding helpers for provider, key, and model setup."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from okto_neuron._compat import getenv as _compat_getenv
from okto_neuron._compat import secret_env as _secret_env
from okto_neuron.config._app_config import default_app_home
from okto_neuron.config._vault import (
    _check_api_base,
    _check_api_key_env,
    api_base_is_loopback,
    classify_api_base,
)
from okto_neuron.providers import (
    OPENAI_V1_DRIVERS,
    litellm_proxy_root,
    resolve_openai_base,
)

_DEFAULT_LOCAL_API_BASE = "http://127.0.0.1:8123/v1"
_ENV_FILE_NAME = "env"
_MAX_API_KEY_LENGTH = 16_384
_WINDOWS_DPAPI_PREFIX = "dpapi-v1:"
_CRYPTPROTECT_UI_FORBIDDEN = 0x1

DiscoveryKind = Literal[
    "auto",
    "openai",
    "anthropic",
    "gemini",
    "litellm_proxy",
    "openrouter",
    "pi_cli",
    "codex_cli",
    "chatgpt",
    "none",
]


@dataclass(frozen=True)
class ProviderPreset:
    """Provider preset exposed by the first-run wizard."""

    key: str
    label: str
    provider: str
    default_model: str
    api_base: str
    api_key_env: str
    hosted: bool
    discovery: DiscoveryKind
    api_key_required: bool = True
    menu: bool = True
    note: str = ""


PROVIDER_PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        key="auto",
        label="Auto-detect local runtime",
        provider="",
        default_model="",
        api_base="",
        api_key_env="OKTO_NEURON_LOCAL_LLM_KEY",
        hosted=False,
        discovery="auto",
        api_key_required=False,
        note="Probes LM Studio, Ollama, and LiteLLM Proxy on loopback only.",
    ),
    ProviderPreset(
        key="skip",
        label="Skip LLM setup",
        provider="",
        default_model="",
        api_base="",
        api_key_env="",
        hosted=False,
        discovery="none",
        api_key_required=False,
        note="explore works now; ask and remember stay disabled until reconfigured.",
    ),
    ProviderPreset(
        key="lm_studio",
        label="LM Studio",
        provider="lm_studio",
        default_model="local-model",
        api_base="http://127.0.0.1:1234/v1",
        api_key_env="OKTO_NEURON_LM_STUDIO_API_KEY",
        hosted=False,
        discovery="openai",
        api_key_required=False,
        note="Local OpenAI-compatible endpoint.",
    ),
    ProviderPreset(
        key="ollama",
        label="Ollama",
        provider="ollama",
        default_model="llama3.1",
        api_base="http://127.0.0.1:11434/v1",
        api_key_env="OKTO_NEURON_OLLAMA_API_KEY",
        hosted=False,
        discovery="openai",
        api_key_required=False,
        note="Local OpenAI-compatible endpoint.",
    ),
    ProviderPreset(
        key="litellm_proxy",
        label="LiteLLM Proxy",
        provider="litellm_proxy",
        default_model="gpt-4.1-mini",
        api_base="http://127.0.0.1:4000",
        api_key_env="OKTO_NEURON_LITELLM_PROXY_API_KEY",
        hosted=False,
        discovery="litellm_proxy",
        api_key_required=False,
        note="Local or self-hosted proxy. Discovery tries model-info first.",
    ),
    ProviderPreset(
        key="openrouter",
        label="OpenRouter",
        provider="openrouter",
        default_model="openai/gpt-4.1-mini",
        api_base="https://openrouter.ai/api/v1",
        api_key_env="OKTO_NEURON_OPENROUTER_API_KEY",
        hosted=True,
        discovery="openrouter",
        note="Hosted multi-provider endpoint.",
    ),
    ProviderPreset(
        key="openai",
        label="OpenAI",
        provider="openai",
        default_model="gpt-4.1-mini",
        api_base="https://api.openai.com/v1",
        api_key_env="OKTO_NEURON_OPENAI_API_KEY",
        hosted=True,
        discovery="openai",
    ),
    ProviderPreset(
        key="gemini",
        label="Google Gemini",
        provider="gemini",
        default_model="gemini-1.5-pro",
        api_base="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="OKTO_NEURON_GEMINI_API_KEY",
        hosted=True,
        discovery="gemini",
        note="Runtime uses Gemini's OpenAI-compatible base; discovery uses native models.",
    ),
    ProviderPreset(
        key="anthropic",
        label="Anthropic",
        provider="anthropic",
        default_model="claude-3-5-sonnet-latest",
        api_base="https://api.anthropic.com/v1",
        api_key_env="OKTO_NEURON_ANTHROPIC_API_KEY",
        hosted=True,
        discovery="anthropic",
        note="Direct Anthropic provider.",
    ),
    ProviderPreset(
        key="pi_cli",
        label="pi CLI (local multi-provider)",
        provider="pi_cli",
        default_model="",
        api_base="",
        api_key_env="",
        hosted=False,
        discovery="pi_cli",
        api_key_required=False,
        note="Uses the local pi binary to dispatch to configured providers.",
    ),
    ProviderPreset(
        key="codex_cli",
        label="Codex CLI (subscription)",
        provider="codex_cli",
        default_model="",
        api_base="",
        api_key_env="",
        hosted=False,
        discovery="codex_cli",
        api_key_required=False,
        note=(
            "Uses the local codex binary and its existing login (ChatGPT subscription or "
            "API key). No model catalog to discover — enter a plain OpenAI-style model id "
            "manually, e.g. gpt-5.5."
        ),
    ),
    ProviderPreset(
        key="chatgpt",
        label="ChatGPT subscription (exploration only)",
        provider="chatgpt",
        default_model="gpt-5.5",
        api_base="",
        api_key_env="",
        hosted=True,
        discovery="chatgpt",
        api_key_required=False,
        # Kept OFF the first-run menu on purpose. It is opt-in
        # (OKTO_NEURON_ENABLE_CHATGPT=1), it silently drops structured output
        # and every sampler, and it has no per-token cost, so it must never be
        # something a new user picks by wandering into it.
        menu=False,
        note=(
            "EXPLORATION ONLY — never for published results. Uses a ChatGPT "
            "subscription via litellm's Responses transport, which silently "
            "drops response_format, temperature, top_p, seed and max_tokens, "
            "and reports no per-token cost. Requires OKTO_NEURON_ENABLE_CHATGPT=1 "
            "and its own credential directory (CHATGPT_TOKEN_DIR) — never share "
            "one with codex or pi. See docs/remote-providers.md."
        ),
    ),
    ProviderPreset(
        key="custom",
        label="Custom OpenAI-compatible endpoint",
        provider="openai",
        default_model="local-model",
        api_base=_DEFAULT_LOCAL_API_BASE,
        api_key_env="OKTO_NEURON_CUSTOM_LLM_KEY",
        hosted=False,
        discovery="openai",
        api_key_required=False,
        note="For a named provider above, prefer that branch over custom.",
    ),
    ProviderPreset(
        key="local",
        label="Local OpenAI-compatible (legacy)",
        provider="openai",
        # Discovery-first: no hardcoded model the endpoint may not serve;
        # the flow discovers models from the base URL, with manual entry as
        # the only fallback.
        default_model="",
        api_base=_DEFAULT_LOCAL_API_BASE,
        api_key_env="OKTO_NEURON_LOCAL_LLM_KEY",
        hosted=False,
        discovery="openai",
        api_key_required=False,
        menu=False,
    ),
)

_PRESET_BY_KEY = {preset.key: preset for preset in PROVIDER_PRESETS}
MENU_PRESETS = tuple(preset for preset in PROVIDER_PRESETS if preset.menu)
MANAGED_API_KEY_PROVIDERS = frozenset(
    {
        preset.provider
        for preset in PROVIDER_PRESETS
        if preset.provider and preset.api_key_env and preset.provider not in {"pi_cli", "codex_cli"}
    }
    | {"custom_openai", "openai_like"}
)

# Embedding providers whose authentication is one opaque API key. Providers
# that use ambient/cloud credential chains (Bedrock, OCI, Snowflake, Vertex AI),
# or a separate interactive login (GitHub Copilot), are intentionally absent:
# writing one generic key for those providers would imply a contract the runtime
# does not have.
MANAGED_EMBEDDING_API_KEY_PROVIDERS = frozenset(
    {
        "azure",
        "azure_ai",
        "cohere",
        "databricks",
        "fireworks_ai",
        "gemini",
        "gigachat",
        "llamagate",
        "libertai",
        "lm_studio",
        "mistral",
        "nebius",
        "novita",
        "ollama",
        "openai",
        "perplexity",
        "scaleway",
        "together_ai",
        "vercel_ai_gateway",
        "volcengine",
        "voyage",
    }
)


@dataclass(frozen=True)
class ModelDiscoveryResult:
    models: list[str]
    error: str | None = None
    endpoint: str | None = None


@dataclass(frozen=True)
class AutoDetectionCandidate:
    preset: ProviderPreset
    models: list[str]
    error: str | None = None


def get_provider_preset(key: str) -> ProviderPreset:
    try:
        return _PRESET_BY_KEY[key]
    except KeyError as exc:
        expected = ", ".join(preset.key for preset in PROVIDER_PRESETS)
        raise ValueError(f"unknown provider preset {key!r}; expected one of: {expected}") from exc


def user_env_file() -> Path:
    """Path to the user-owned env file created by onboarding."""

    override = _compat_getenv("OKTO_NEURON_ENV_FILE")
    if override:
        return Path(override).expanduser().resolve(strict=False)
    return default_app_home() / _ENV_FILE_NAME


def default_api_key_env(provider: str, api_base: str | None = None) -> str:
    """Return a stable, namespaced env-var name for one provider endpoint.

    UI-managed keys use a reserved namespace so they never overwrite an
    externally managed provider variable. Custom endpoints include the host
    (and explicit port) so two OpenAI-compatible services do not collide.
    """

    provider_slug = re.sub(r"[^A-Z0-9]+", "_", provider.upper()).strip("_")
    if not provider_slug:
        raise ValueError("provider is required to create an API-key reference")

    if api_base:
        classify_api_base(api_base, resolve=False)
    parsed_base = urlparse(api_base) if api_base else None
    base_host = (parsed_base.hostname or "").lower() if parsed_base else ""
    normalized_base = api_base.rstrip("/") if api_base else ""
    standard_base = ""
    for preset in PROVIDER_PRESETS:
        if preset.key == provider:
            standard_base = preset.api_base.rstrip("/")
            break

    endpoint = base_host
    if endpoint and parsed_base and parsed_base.port is not None:
        endpoint = f"{endpoint}_{parsed_base.port}"
    host_slug = re.sub(r"[^A-Z0-9]+", "_", endpoint.upper()).strip("_")
    parts = ["OKTO_NEURON", "PROVIDER", provider_slug]
    if host_slug and normalized_base != standard_base:
        endpoint_hash = hashlib.sha256(normalized_base.encode("utf-8")).hexdigest()[:8].upper()
        parts.extend([host_slug, endpoint_hash])
    parts.extend(["API", "KEY"])
    env_name = "_".join(parts)
    _check_api_key_env(env_name)
    return env_name


def _windows_secret_protection_enabled() -> bool:
    """Return whether managed secrets must use the Windows CurrentUser store."""

    return os.name == "nt"


def _windows_dpapi(payload: bytes, *, protect: bool) -> bytes:
    """Protect or unprotect bytes with DPAPI's implicit CurrentUser scope.

    Imports stay lazy so non-Windows library users never load Windows-only ctypes
    symbols. DPAPI owns the output allocation; ``LocalFree`` releases it after a
    private copy has been made. Error messages intentionally contain neither the
    input nor the protected payload.
    """

    import ctypes
    from ctypes import wintypes

    class _DataBlob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
        ]

    source = ctypes.create_string_buffer(payload)
    source_blob = _DataBlob(
        len(payload),
        ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)),
    )
    result_blob = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    blob_pointer = ctypes.POINTER(_DataBlob)
    if protect:
        operation = crypt32.CryptProtectData
        operation.argtypes = [
            blob_pointer,
            wintypes.LPCWSTR,
            blob_pointer,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            blob_pointer,
        ]
        arguments = (
            ctypes.byref(source_blob),
            "Okto Neuron managed credential",
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(result_blob),
        )
        failure = "Windows credential protection failed"
    else:
        operation = crypt32.CryptUnprotectData
        operation.argtypes = [
            blob_pointer,
            ctypes.POINTER(wintypes.LPWSTR),
            blob_pointer,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.DWORD,
            blob_pointer,
        ]
        arguments = (
            ctypes.byref(source_blob),
            None,
            None,
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(result_blob),
        )
        failure = "Windows credential recovery failed"

    operation.restype = wintypes.BOOL
    if not operation(*arguments):
        error_code = ctypes.get_last_error()
        raise OSError(error_code, failure)

    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(result_blob.pbData, result_blob.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(result_blob.pbData, ctypes.c_void_p))


def _protect_windows_secret(payload: bytes) -> bytes:
    """Test seam for the DPAPI protection call."""

    return _windows_dpapi(payload, protect=True)


def _unprotect_windows_secret(payload: bytes) -> bytes:
    """Test seam for the DPAPI recovery call."""

    return _windows_dpapi(payload, protect=False)


def _encode_secret_for_storage(secret: str) -> str:
    """Serialize one secret for the platform-owned managed credential file."""

    if not _windows_secret_protection_enabled():
        return shlex.quote(secret)
    protected = _protect_windows_secret(secret.encode("utf-8"))
    encoded = base64.urlsafe_b64encode(protected).decode("ascii")
    return f"{_WINDOWS_DPAPI_PREFIX}{encoded}"


def _decode_stored_secret(value: str) -> str:
    """Recover a managed value without ever returning an opaque envelope."""

    if not value.startswith(_WINDOWS_DPAPI_PREFIX):
        # Compatibility for values written before the Windows protected backend.
        return value
    if not _windows_secret_protection_enabled():
        raise OSError("Windows managed credential is unavailable on this platform")

    encoded = value.removeprefix(_WINDOWS_DPAPI_PREFIX)
    try:
        protected = base64.b64decode(encoded, altchars=b"-_", validate=True)
        recovered = _unprotect_windows_secret(protected)
        return recovered.decode("utf-8")
    except OSError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise OSError("Windows managed credential could not be decoded") from exc


def load_user_env_file(path: Path | None = None) -> None:
    """Load generated OKTO_NEURON_* (or pre-0.3.0 MARGINALIA_*) env vars into the process.

    Existing environment values win. The file intentionally supports only the
    simple KEY=value lines generated by :func:`write_user_env_secret`; arbitrary
    shell is never evaluated.
    """

    env_path = path or user_env_file()
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not name.startswith(("OKTO_NEURON_", "MARGINALIA_")) or name in os.environ:
            continue
        try:
            _check_api_key_env(name)
            parsed = shlex.split(value, comments=False, posix=True)
            decoded = _decode_stored_secret(parsed[0] if parsed else "")
        except (OSError, ValueError):
            continue
        os.environ[name] = decoded


def write_user_env_secret(env_name: str, secret: str, path: Path | None = None) -> Path:
    """Persist one API key in the user env file with owner-only permissions."""

    _check_api_key_env(env_name)
    if not secret:
        raise ValueError("API key must not be empty")
    if len(secret) > _MAX_API_KEY_LENGTH:
        raise ValueError("API key is too long")
    if any(character in secret for character in ("\r", "\n", "\x00")):
        raise ValueError("API key must not contain newline or NUL characters")
    env_path = path or user_env_file()
    env_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(env_path.parent, 0o700)

    try:
        old_lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        old_lines = [
            "# Okto Neuron local secrets. Loaded automatically by the okto-neuron CLI.",
            "# Values are never written to okto-neuron.yaml.",
        ]

    assignment = f"{env_name}={_encode_secret_for_storage(secret)}"
    lines: list[str] = []
    inserted = False
    for raw in old_lines:
        stripped = raw.strip()
        candidate = (
            stripped[len("export ") :].strip() if stripped.startswith("export ") else stripped
        )
        if candidate.startswith(f"{env_name}="):
            if not inserted:
                lines.append(assignment)
                inserted = True
            continue
        lines.append(raw)
    if not inserted:
        lines.append(assignment)

    content = "\n".join(lines).rstrip() + "\n"
    fd, tmp_name = tempfile.mkstemp(
        dir=str(env_path.parent), prefix=f".{env_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, env_path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    if not _windows_secret_protection_enabled():
        os.chmod(env_path, 0o600)
    os.environ[env_name] = secret
    return env_path


def delete_user_env_secret(env_name: str, path: Path | None = None) -> None:
    """Remove one managed credential without exposing or evaluating its value."""

    _check_api_key_env(env_name)
    env_path = path or user_env_file()
    try:
        old_lines = env_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        os.environ.pop(env_name, None)
        return

    lines: list[str] = []
    for raw in old_lines:
        stripped = raw.strip()
        candidate = (
            stripped[len("export ") :].strip() if stripped.startswith("export ") else stripped
        )
        if candidate.startswith(f"{env_name}="):
            continue
        lines.append(raw)

    content = "\n".join(lines).rstrip() + "\n"
    fd, tmp_name = tempfile.mkstemp(
        dir=str(env_path.parent), prefix=f".{env_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(content)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, env_path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    if not _windows_secret_protection_enabled():
        os.chmod(env_path, 0o600)
    os.environ.pop(env_name, None)


def discover_models(
    preset: ProviderPreset,
    *,
    api_base: str | None = None,
    api_key: str | None = None,
    timeout: float = 5.0,
    allow_remote: bool = False,
    remote_confirmed: bool = False,
) -> ModelDiscoveryResult:
    """Best-effort live model discovery for onboarding.

    Discovery is safety-gated before any network call. A real API key is never
    attached to a non-loopback endpoint unless the caller has explicitly allowed
    and confirmed remote egress for that exact branch.
    """

    if preset.discovery in {"none", "auto"}:
        return ModelDiscoveryResult([])

    # pi_cli is a local subprocess call, not HTTP — bypass the entire
    # _discovery_base_for / _validate_discovery_target HTTP-safety path.
    if preset.discovery == "pi_cli":
        return _discover_pi_cli_models(timeout=timeout)

    # codex_cli reads a local config file, not HTTP — same bypass as pi_cli.
    if preset.discovery == "codex_cli":
        return _discover_codex_cli_models()

    # chatgpt has NO model-listing endpoint (verified: nothing under
    # litellm/llms/chatgpt/ lists models, and ``get_complete_url`` only ever
    # builds ``/responses``). Probing ``{base}/v1/models`` the way the openai
    # branch does would 404 against the ChatGPT backend, so the catalog comes
    # out of litellm's own static table instead. No network call, so the same
    # HTTP-safety bypass as the two CLI branches applies.
    if preset.discovery == "chatgpt":
        return _discover_chatgpt_models()

    base = api_base or preset.api_base
    discovery_base = _discovery_base_for(preset, base)
    error = _validate_discovery_target(
        discovery_base,
        allow_remote=allow_remote,
        remote_confirmed=remote_confirmed,
    )
    if error:
        return ModelDiscoveryResult([], error=error, endpoint=discovery_base)

    try:
        if preset.discovery == "openai":
            return _discover_openai_models(base, api_key=api_key, timeout=timeout)
        if preset.discovery == "anthropic":
            return _discover_anthropic_models(api_key=api_key, timeout=timeout)
        if preset.discovery == "gemini":
            return _discover_gemini_models(api_key=api_key, timeout=timeout)
        if preset.discovery == "openrouter":
            return _discover_openrouter_models(api_key=api_key, timeout=timeout)
        if preset.discovery == "litellm_proxy":
            return _discover_litellm_proxy_models(base, api_key=api_key, timeout=timeout)
        return ModelDiscoveryResult([])
    except HTTPError as exc:
        return ModelDiscoveryResult([], f"HTTP {exc.code}", endpoint=discovery_base)
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        return ModelDiscoveryResult([], str(reason), endpoint=discovery_base)
    except TimeoutError:
        return ModelDiscoveryResult([], "request timed out", endpoint=discovery_base)
    except Exception as exc:  # noqa: BLE001 - onboarding must degrade to manual entry.
        return ModelDiscoveryResult([], type(exc).__name__, endpoint=discovery_base)


def auto_detect_loopback(timeout: float = 2.0) -> list[AutoDetectionCandidate]:
    """Probe well-known loopback runtimes without touching hosted providers."""

    candidates: list[AutoDetectionCandidate] = []
    for key in ("lm_studio", "ollama", "litellm_proxy"):
        preset = get_provider_preset(key)
        result = discover_models(
            preset,
            timeout=timeout,
            allow_remote=False,
            remote_confirmed=False,
        )
        if result.models:
            candidates.append(AutoDetectionCandidate(preset, result.models))
        elif result.error and key == "litellm_proxy" and result.error in {"HTTP 401", "HTTP 403"}:
            candidates.append(AutoDetectionCandidate(preset, [], error="key required"))
    return candidates


_VERIFY_COMPLETION_PROMPT = "Reply with exactly one word: pong"
_VERIFY_COMPLETION_TIMEOUT_S = 60.0
_VERIFY_ERROR_MAX_CHARS = 240


@dataclass(frozen=True)
class VerifyCompletionResult:
    """Outcome of the pre-save onboarding verify (one real minimal completion)."""

    ok: bool
    url_attempted: str
    error: str | None = None


def verify_onboarding_completion(
    preset: ProviderPreset,
    *,
    model: str,
    api_base: str,
    api_key: str | None,
    api_key_env: str | None,
    timeout: float = _VERIFY_COMPLETION_TIMEOUT_S,
) -> VerifyCompletionResult:
    """Run one real minimal completion through the canonical endpoint.

    Onboarding discovery (model listing) proves the base URL is *close*; only a
    completion proves the base the runtime will actually use works — the two
    used to disagree on the ``/v1`` path and a wrong base was saved anyway.
    This sends one trivial prompt through the exact :func:`okto_neuron.llm.
    get_provider` path every real ask/ingest call uses, on the canonical
    ``{root}/v1`` base the resolver derives (see
    :func:`okto_neuron.providers.resolve_openai_base`), so discovery and verify
    can never probe different endpoints again.

    The in-memory API key (typed moments ago, not yet persisted) is exported
    for the duration of the call only; it never appears in the returned error.
    """
    # lazy import — keeps onboarding import-light (litellm is an optional dep)
    from okto_neuron.config._vault import ResolvedLLM
    from okto_neuron.llm import (
        Message,
        _redact_api_key,
        get_provider,
    )

    try:
        canonical = resolve_openai_base(api_base)
    except ValueError as exc:
        return VerifyCompletionResult(ok=False, url_attempted=api_base, error=str(exc))
    url_attempted = f"{canonical.api_base}/chat/completions"

    injected = False
    if api_key and api_key_env and not _secret_env(api_key_env):
        os.environ[api_key_env] = api_key
        injected = True
    try:
        resolved = ResolvedLLM(
            provider=preset.provider,
            api_base=api_base,
            model=model,
            api_key_env=api_key_env,
            request_timeout_s=timeout,
        )
        provider = get_provider(resolved)
        provider.complete(
            [Message(role="user", content=_VERIFY_COMPLETION_PROMPT)],
            max_tokens=16,
        )
        return VerifyCompletionResult(ok=True, url_attempted=url_attempted)
    except Exception as exc:  # noqa: BLE001 - onboarding degrades to a clear error.
        raw = str(exc)
        safe = _redact_api_key(raw, api_key)
        if len(safe) > _VERIFY_ERROR_MAX_CHARS:
            safe = safe[: _VERIFY_ERROR_MAX_CHARS - 1] + "…"
        if "404" in safe:
            message = (
                f"endpoint answered 404 at {url_attempted} — OpenAI-compatible servers "
                f"serve the OpenAI API under /v1/; re-check the base URL"
            )
        else:
            message = f"{safe} (attempted: POST {url_attempted})"
        return VerifyCompletionResult(ok=False, url_attempted=url_attempted, error=message)
    finally:
        if injected:
            os.environ.pop(api_key_env, None)


def llm_config_patch(
    preset: ProviderPreset,
    *,
    model: str,
    api_base: str | None,
    api_key_env: str | None,
    allow_remote: bool | None = None,
) -> dict[str, Any]:
    """Build the exact ``VaultConfig.apply_patch`` body for onboarding."""

    if preset.key in {"auto", "skip"}:
        raise ValueError(f"{preset.key!r} is not a concrete provider config")
    if api_key_env:
        _check_api_key_env(api_key_env)

    # Presets with no api_base concept (e.g. pi_cli) skip URL validation.
    chosen_base = api_base or preset.api_base
    if not chosen_base:
        defaults: dict[str, Any] = {
            "provider": preset.provider,
            "model": model,
            "api_key_env": api_key_env,
        }
        return {"llm": {"enabled": True, "allow_remote": False, "defaults": defaults}}

    if allow_remote is None:
        allow_remote = not api_base_is_loopback(chosen_base)
    _check_api_base(chosen_base, allow_remote=allow_remote)
    # Persist the canonical completion base (server root + /v1) for OpenAI-
    # compatible drivers, so the saved config matches exactly what the runtime
    # and the pre-save verify used — root-form and /v1-form input both land
    # here (idempotent for bases that already end in /v1).
    if preset.provider in OPENAI_V1_DRIVERS:
        chosen_base = resolve_openai_base(chosen_base).api_base

    defaults = {
        "provider": preset.provider,
        "api_base": chosen_base,
        "model": model,
        "api_key_env": api_key_env,
    }
    return {"llm": {"enabled": True, "allow_remote": allow_remote, "defaults": defaults}}


def disabled_llm_patch() -> dict[str, Any]:
    """Patch body for explicitly disabling LLM-backed features."""

    return {"llm": {"enabled": False}}


def _discovery_base_for(preset: ProviderPreset, api_base: str) -> str:
    if preset.discovery == "gemini":
        return "https://generativelanguage.googleapis.com/v1beta"
    if preset.discovery == "openrouter":
        return "https://openrouter.ai/api/v1"
    if preset.discovery == "anthropic":
        return "https://api.anthropic.com/v1"
    return api_base


def _validate_discovery_target(
    api_base: str, *, allow_remote: bool, remote_confirmed: bool
) -> str | None:
    try:
        endpoint_class = classify_api_base(api_base, resolve=False)
    except ValueError as exc:
        return str(exc)
    if endpoint_class != "loopback" and not (allow_remote and remote_confirmed):
        return "remote endpoint requires explicit confirmation before model discovery"
    try:
        _check_api_base(api_base, allow_remote=allow_remote)
        if endpoint_class != "loopback":
            classify_api_base(api_base, resolve=True)
    except ValueError as exc:
        return str(exc)
    return None


def _discover_openai_models(
    api_base: str,
    *,
    api_key: str | None,
    timeout: float,
) -> ModelDiscoveryResult:
    """Probe the canonical OpenAI model list for one base URL.

    Uses the shared resolver so the user may type the server root or the
    versioned form — both hit the same ``{root}/v1/models`` the completion
    path's ``{root}/v1/chat/completions`` implies (see
    :func:`okto_neuron.providers.resolve_openai_base`).
    """
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    url = resolve_openai_base(api_base).models_url
    payload = _get_json(url, headers=headers, timeout=timeout)
    return ModelDiscoveryResult(_ids_from_data_list(payload), endpoint=url)


def _discover_anthropic_models(
    *,
    api_key: str | None,
    timeout: float,
) -> ModelDiscoveryResult:
    headers = {"anthropic-version": "2023-06-01"}
    if api_key:
        headers["x-api-key"] = api_key
    url = "https://api.anthropic.com/v1/models"
    payload = _get_json(url, headers=headers, timeout=timeout)
    return ModelDiscoveryResult(_ids_from_data_list(payload), endpoint=url)


def _discover_gemini_models(
    *,
    api_key: str | None,
    timeout: float,
) -> ModelDiscoveryResult:
    query = urlencode({"key": api_key}) if api_key else ""
    url = "https://generativelanguage.googleapis.com/v1beta/models"
    if query:
        url = f"{url}?{query}"
    payload = _get_json(url, headers={}, timeout=timeout)
    models = payload.get("models", [])
    ids: list[str] = []
    if isinstance(models, list):
        for entry in models:
            if not isinstance(entry, dict):
                continue
            methods = entry.get("supportedGenerationMethods")
            if isinstance(methods, list) and "generateContent" not in methods:
                continue
            name = str(entry.get("name") or "").removeprefix("models/")
            if name:
                ids.append(name)
    return ModelDiscoveryResult(sorted(dict.fromkeys(ids)), endpoint=url)


def _discover_openrouter_models(
    *,
    api_key: str | None,
    timeout: float,
) -> ModelDiscoveryResult:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    url = "https://openrouter.ai/api/v1/models"
    payload = _get_json(url, headers=headers, timeout=timeout)
    return ModelDiscoveryResult(_ids_from_data_list(payload), endpoint=url)


def _discover_litellm_proxy_models(
    api_base: str,
    *,
    api_key: str | None,
    timeout: float,
) -> ModelDiscoveryResult:
    root = litellm_proxy_root(api_base)
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    last_error: str | None = None
    for suffix in ("/v1/model/info", "/model/info", "/v1/models"):
        url = f"{root}{suffix}"
        try:
            payload = _get_json(url, headers=headers, timeout=timeout)
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise
            last_error = f"HTTP {exc.code}"
            continue
        models = (
            _ids_from_litellm_model_info(payload)
            if suffix != "/v1/models"
            else _ids_from_data_list(payload)
        )
        if models:
            return ModelDiscoveryResult(models, endpoint=url)
    return ModelDiscoveryResult([], last_error or "no models returned", endpoint=root)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise HTTPError(req.full_url, code, "redirects disabled", headers, fp)


_NO_REDIRECT_OPENER = build_opener(_NoRedirect)


def _urlopen_no_redirect(req: Request, *, timeout: float):
    return _NO_REDIRECT_OPENER.open(req, timeout=timeout)


def _get_json(url: str, *, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    req = Request(url, headers={"Accept": "application/json", **headers})
    with _urlopen_no_redirect(req, timeout=timeout) as response:
        body = response.read()
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("model list response was not a JSON object")
    return data


def _ids_from_data_list(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data", [])
    ids: list[str] = []
    if isinstance(data, list):
        for entry in data:
            if isinstance(entry, dict):
                model_id = str(entry.get("id") or "")
                if model_id:
                    ids.append(model_id)
    return sorted(dict.fromkeys(ids))


def _discover_pi_cli_models(
    *,
    timeout: float = 30.0,
) -> ModelDiscoveryResult:
    """Discover models via ``pi --list-models`` subprocess output.

    Parses the plain-text table by taking only the first two whitespace-
    separated tokens per data row, ignoring the ragged-column problem in
    the remaining columns.
    """

    pi_path = shutil.which("pi")
    if pi_path is None:
        return ModelDiscoveryResult([], error="pi CLI not found on PATH")

    try:
        proc = subprocess.run(
            [pi_path, "--list-models"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ModelDiscoveryResult([], error="pi --list-models timed out")
    except OSError as exc:
        return ModelDiscoveryResult([], error=str(exc))

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        return ModelDiscoveryResult(
            [], error=stderr or f"pi --list-models exited {proc.returncode}"
        )

    lines = proc.stdout.splitlines()
    # Skip header row (first non-blank line containing 'provider'/'model')
    # and blank lines. Take only first two tokens per data row.
    models: list[str] = []
    header_skipped = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if not header_skipped:
            tokens = stripped.split()
            if tokens[0] in ("provider", "") or "provider" in tokens[:2]:
                header_skipped = True
                continue
            header_skipped = True  # skip first non-blank line as header
            continue
        tokens = stripped.split()
        if len(tokens) < 2:
            continue  # skip malformed lines (e.g. "..." truncation)
        provider = tokens[0]
        model = tokens[1]
        if provider == "..." or model == "...":
            continue
        models.append(f"{provider}/{model}")

    return ModelDiscoveryResult(
        models=sorted(dict.fromkeys(models)),
        endpoint="pi --list-models",
    )


def _discover_codex_cli_models(*, timeout: float = 10.0) -> ModelDiscoveryResult:
    """Verify the local Codex CLI works; there is no enumerable model catalog.

    Codex has no ``models`` subcommand and no model-catalog API. An earlier
    version of this function surfaced the ``[model_providers.*]`` table keys
    from the CLI's own ``config.toml`` as "discovered models" — those are
    provider *endpoint* configs (``base_url``/``env_key``), not model ids, and
    live-testing proved it: ``codex exec -m omlx`` fails with "Model metadata
    for `omlx` not found ... model is not supported". Presenting them as
    selectable models actively misleads users into picking a value that
    breaks the provider, so this ALWAYS returns an empty model list.

    Instead this runs ``codex --version`` (mirrors ``_discover_pi_cli_models``'s
    functional check) to confirm the binary actually works. The caller must
    enter a model id manually — see the ``codex_cli`` preset's ``note`` and the
    Config UI's per-provider model-syntax hint for the expected id shape
    (plain OpenAI-style model id, e.g. ``gpt-5.5`` — no provider prefix).
    """
    binary = shutil.which("codex")
    if binary is None:
        return ModelDiscoveryResult([], error="codex CLI not found on PATH")

    try:
        proc = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ModelDiscoveryResult([], error="codex --version timed out")
    except OSError as exc:
        return ModelDiscoveryResult([], error=str(exc))

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        return ModelDiscoveryResult([], error=stderr or f"codex --version exited {proc.returncode}")

    return ModelDiscoveryResult([], endpoint=f"{binary} --version")


# Models this account was observed to REJECT, live, with "not supported when
# using Codex with a ChatGPT account". They are real entries in
# ``litellm.model_cost``, so they would otherwise be offered and fail at the
# first call. ``gpt-5.5`` is the one verified working.
CHATGPT_MODELS_REJECTED_BY_SUBSCRIPTION: frozenset[str] = frozenset(
    {"gpt-5.3-codex", "gpt-5.1-codex-mini"}
)


def _discover_chatgpt_models() -> ModelDiscoveryResult:
    """List ``chatgpt/*`` models from litellm's static cost table.

    There is no remote catalog to ask. litellm's ``model_cost`` dict is the
    only enumeration that exists for this provider, so it is the source here —
    a local dict read, no HTTP, no credential. The ``chatgpt/`` prefix is
    stripped because Okto Neuron stores the bare model id and adds the provider
    prefix itself (``LiteLLMProvider.model``).

    Models the subscription is known to refuse are filtered out rather than
    offered and left to fail at the first completion.
    """

    try:
        import litellm
    except Exception as exc:  # noqa: BLE001 - optional dep; degrade to manual entry
        return ModelDiscoveryResult([], error=f"litellm unavailable: {type(exc).__name__}")

    try:
        catalog = litellm.model_cost
    except Exception as exc:  # noqa: BLE001
        return ModelDiscoveryResult([], error=f"litellm model_cost unavailable: {exc}")

    ids = sorted(
        name
        for key in catalog
        if key.startswith("chatgpt/")
        and (name := key.split("/", 1)[1]) not in CHATGPT_MODELS_REJECTED_BY_SUBSCRIPTION
    )
    return ModelDiscoveryResult(ids, endpoint="litellm.model_cost")


def _ids_from_litellm_model_info(payload: dict[str, Any]) -> list[str]:
    data = payload.get("data", payload.get("model_info", payload.get("models", [])))
    ids: list[str] = []
    if isinstance(data, dict):
        data = list(data.values())
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            model_id = str(
                entry.get("model_name")
                or entry.get("id")
                or entry.get("name")
                or entry.get("model")
                or ""
            )
            litellm_params = entry.get("litellm_params")
            if not model_id and isinstance(litellm_params, dict):
                model_id = str(litellm_params.get("model") or "")
            model_info = entry.get("model_info")
            if not model_id and isinstance(model_info, dict):
                model_id = str(model_info.get("id") or model_info.get("model_name") or "")
            if model_id:
                ids.append(model_id)
    return sorted(dict.fromkeys(ids))


__all__ = [
    "AutoDetectionCandidate",
    "MENU_PRESETS",
    "MANAGED_EMBEDDING_API_KEY_PROVIDERS",
    "MANAGED_API_KEY_PROVIDERS",
    "ModelDiscoveryResult",
    "PROVIDER_PRESETS",
    "ProviderPreset",
    "auto_detect_loopback",
    "default_api_key_env",
    "delete_user_env_secret",
    "disabled_llm_patch",
    "discover_models",
    "get_provider_preset",
    "VerifyCompletionResult",
    "llm_config_patch",
    "load_user_env_file",
    "user_env_file",
    "verify_onboarding_completion",
    "write_user_env_secret",
    "_discover_chatgpt_models",
    "_discover_codex_cli_models",
    "_discover_pi_cli_models",
]
