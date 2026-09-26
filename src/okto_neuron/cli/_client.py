"""Thin HTTP client helpers for kg subcommands.

The CLI for `add`, `query`, and `detect-drift` is a thin client over the
Okto Neuron HTTP server. There is no auto-spawn, no retries, no fallback to
opening the vault directly. If the server is unreachable, the command exits
non-zero with an exact, contract-stable error message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import httpx

from okto_neuron._compat import VAULT_HEADER
from okto_neuron._compat import getenv as _compat_getenv

DEFAULT_ENDPOINT = "http://127.0.0.1:7777"
DEFAULT_TIMEOUT_SECONDS = 30.0
ENDPOINT_ENV_VAR = "OKTO_NEURON_ENDPOINT"
AUTH_TOKEN_ENV_VAR = "OKTO_NEURON_AUTH_TOKEN"

# Exact message required by spec & implementation card. Do not edit casually —
# scripts and humans match on it.
UNREACHABLE_MESSAGE_TEMPLATE = (
    "no okto-neuron server reachable at {url} — start with `okto-neuron serve`"
)

EXIT_UNREACHABLE = 2

# ``okto-neuron ask`` got a response, but the answer is degraded (the server's
# top-level ``status`` is ``"degraded"``: no LLM configured, provider failure,
# truncated, abnormal stop or empty text). The output is still printed; the
# code only tells scripts not to treat it as a real answer. 17 is unused by
# every ``OktoNeuronError`` class (1-16 and 130 are taken).
EXIT_ASK_DEGRADED = 17


class ClientUnreachable(RuntimeError):
    """Raised when the server cannot be reached. Carries the user-facing message."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(UNREACHABLE_MESSAGE_TEMPLATE.format(url=url))


class ServerError(RuntimeError):
    """Raised when the server returns a non-2xx status."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"server error {status_code}: {detail}")


def resolve_endpoint(flag_value: str | None) -> str:
    """Endpoint precedence: --endpoint flag > OKTO_NEURON_ENDPOINT env > default."""
    if flag_value:
        return flag_value
    env_value = _compat_getenv(ENDPOINT_ENV_VAR)
    if env_value:
        return env_value
    return DEFAULT_ENDPOINT


def resolve_auth_token(
    endpoint: str,
    *,
    transport: httpx.BaseTransport | None = None,
    vault_path: Path | None = None,
) -> str | None:
    """Return only an explicitly exported legacy REST compatibility token.

    ADR 0034 makes ordinary REST credential-free and reserves the application
    bearer for FastMCP. Token-file discovery was removed because the
    credential-free REST status route does not attest ownership of an MCP token.
    """
    del endpoint, transport, vault_path
    env_token = _compat_getenv(AUTH_TOKEN_ENV_VAR)
    return env_token.strip() if env_token and env_token.strip() else None


def auth_headers(
    endpoint: str,
    transport: httpx.BaseTransport | None = None,
    *,
    token: str | None = None,
) -> dict[str, str]:
    """Build an explicitly requested legacy REST bearer header."""
    token = token or resolve_auth_token(endpoint, transport=transport)
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def request(
    endpoint: str,
    method: str,
    path: str,
    payload: Mapping[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: httpx.BaseTransport | None = None,
    auth_token: str | None = None,
    vault: str | Path | None = None,
) -> dict[str, Any]:
    """Send one JSON request. Single attempt, no retries.

    ``vault`` is an immutable request selector, not a request to change the
    daemon's compatibility fallback. The server accepts a registered vault id,
    name, or resolved path in ``X-Okto-Neuron-Vault``.

    REST/UI is loopback-origin protected and intentionally credential-free.
    ``auth_token`` remains an explicit upgrade-compatibility escape hatch only;
    normal callers never discover or send the application/MCP bearer.
    """
    url = endpoint.rstrip("/") + path
    headers = {"Authorization": f"Bearer {auth_token}"} if auth_token is not None else {}
    if vault is not None:
        headers[VAULT_HEADER] = str(vault)
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            response = client.request(
                method,
                url,
                json=dict(payload) if payload is not None else None,
                headers=headers,
            )
    except httpx.ConnectError as exc:
        raise ClientUnreachable(endpoint) from exc
    except httpx.ConnectTimeout as exc:
        raise ClientUnreachable(endpoint) from exc
    if response.status_code >= 400:
        detail = _extract_detail(response)
        raise ServerError(response.status_code, detail)
    try:
        return response.json()
    except ValueError as exc:
        raise ServerError(response.status_code, f"invalid JSON response: {exc}") from exc


def post(
    endpoint: str,
    path: str,
    payload: Mapping[str, Any],
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: httpx.BaseTransport | None = None,
    vault: str | Path | None = None,
) -> dict[str, Any]:
    """POST `payload` as JSON to `<endpoint><path>`. Single attempt, no retries."""
    return request(
        endpoint,
        "POST",
        path,
        payload,
        timeout=timeout,
        transport=transport,
        vault=vault,
    )


def _extract_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text.strip() or response.reason_phrase
    if isinstance(body, dict) and "detail" in body:
        return str(body["detail"])
    return str(body)


__all__ = [
    "ClientUnreachable",
    "ServerError",
    "DEFAULT_ENDPOINT",
    "DEFAULT_TIMEOUT_SECONDS",
    "ENDPOINT_ENV_VAR",
    "AUTH_TOKEN_ENV_VAR",
    "EXIT_UNREACHABLE",
    "UNREACHABLE_MESSAGE_TEMPLATE",
    "resolve_endpoint",
    "resolve_auth_token",
    "auth_headers",
    "request",
    "post",
]
