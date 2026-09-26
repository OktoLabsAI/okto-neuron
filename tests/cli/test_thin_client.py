"""Tests for the CLI thin-client routing introduced for add/query/detect-drift."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from click.testing import CliRunner
import httpx
import pytest

from okto_neuron.cli import _client
from okto_neuron.cli._client import (
    AUTH_TOKEN_ENV_VAR,
    DEFAULT_ENDPOINT,
    DEFAULT_TIMEOUT_SECONDS,
    ENDPOINT_ENV_VAR,
    EXIT_UNREACHABLE,
    UNREACHABLE_MESSAGE_TEMPLATE,
    ClientUnreachable,
    resolve_auth_token,
    resolve_endpoint,
)
from okto_neuron.cli import app as cli_app


# --- endpoint precedence -----------------------------------------------------


def test_resolve_endpoint_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENDPOINT_ENV_VAR, raising=False)
    assert resolve_endpoint(None) == DEFAULT_ENDPOINT
    assert DEFAULT_ENDPOINT == "http://127.0.0.1:7777"


def test_resolve_endpoint_env_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENDPOINT_ENV_VAR, "http://env:9000")
    assert resolve_endpoint(None) == "http://env:9000"


def test_resolve_endpoint_flag_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENDPOINT_ENV_VAR, "http://env:9000")
    assert resolve_endpoint("http://flag:1234") == "http://flag:1234"


# --- unreachable error -------------------------------------------------------


def test_unreachable_message_format() -> None:
    exc = ClientUnreachable("http://127.0.0.1:7777")
    assert str(exc) == (
        "no okto-neuron server reachable at http://127.0.0.1:7777 — start with `okto-neuron serve`"
    )
    assert UNREACHABLE_MESSAGE_TEMPLATE.format(url="http://x") == (
        "no okto-neuron server reachable at http://x — start with `okto-neuron serve`"
    )


def test_post_raises_client_unreachable_on_connect_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    transport = httpx.MockTransport(handler)
    with pytest.raises(ClientUnreachable) as info:
        _client.post("http://127.0.0.1:7777", "/add", {"a": 1}, transport=transport)
    assert info.value.url == "http://127.0.0.1:7777"


def test_legacy_rest_auth_does_not_discover_token_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(AUTH_TOKEN_ENV_VAR, raising=False)
    vault = tmp_path / "vault"
    token_file = vault / ".marginalia" / "daemon.token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("local-capability\n", encoding="utf-8")

    token = resolve_auth_token(
        "http://127.0.0.1:7777",
        vault_path=vault,
    )
    assert token is None


def test_remote_auth_never_discovers_or_reads_local_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(AUTH_TOKEN_ENV_VAR, raising=False)
    token_file = tmp_path / ".marginalia" / "daemon.token"
    token_file.parent.mkdir(parents=True)
    token_file.write_text("must-not-leak", encoding="utf-8")
    requests: list[httpx.Request] = []

    def malicious_health(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"vault_path": str(tmp_path)})

    token = resolve_auth_token(
        "https://remote.example:7777",
        transport=httpx.MockTransport(malicious_health),
    )
    assert token is None
    assert requests == []


def test_remote_auth_accepts_only_explicit_environment_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(AUTH_TOKEN_ENV_VAR, "explicit-remote-token")
    assert resolve_auth_token("https://remote.example:7777") == "explicit-remote-token"


def test_remote_rest_request_does_not_discover_or_send_a_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(AUTH_TOKEN_ENV_VAR, raising=False)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok"})

    body = _client.request(
        "https://remote.example:7777",
        "GET",
        "/api/v1/vaults",
        transport=httpx.MockTransport(handler),
    )

    assert body == {"status": "ok"}
    assert len(requests) == 1
    assert "Authorization" not in requests[0].headers


def test_request_sends_vault_selector_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "ok"})

    body = _client.request(
        "http://127.0.0.1:7777",
        "POST",
        "/query",
        {"query": "x"},
        vault="research",
        transport=httpx.MockTransport(handler),
    )

    assert body == {"status": "ok"}
    assert seen[0].headers["X-Okto-Neuron-Vault"] == "research"
    assert "Authorization" not in seen[0].headers


# --- CLI exit behaviour ------------------------------------------------------


def _patch_post(monkeypatch: pytest.MonkeyPatch, fn) -> None:
    monkeypatch.setattr("okto_neuron.cli._client.post", fn)
    monkeypatch.setattr("okto_neuron.cli._client_post", fn)


def test_query_unreachable_exits_with_exact_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENDPOINT_ENV_VAR, raising=False)

    def fail_post(
        endpoint: str, path: str, payload: dict[str, Any], *, timeout: float = 0, transport=None
    ):  # noqa: ARG001
        raise ClientUnreachable(endpoint)

    _patch_post(monkeypatch, fail_post)
    result = CliRunner().invoke(cli_app, ["query", "hello"])
    assert result.exit_code != 0
    expected = (
        "no okto-neuron server reachable at http://127.0.0.1:7777 — start with `okto-neuron serve`"
    )
    assert expected in result.output


def test_ui_unreachable_uses_shared_exit_code_and_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`okto-neuron ui` must share the EXIT_UNREACHABLE / message contract with
    every other thin-client command instead of reimplementing it out of band
    (deep review 3.25)."""
    monkeypatch.delenv(ENDPOINT_ENV_VAR, raising=False)
    monkeypatch.setattr("okto_neuron.cli._wait_for_server_health", lambda *args, **kwargs: False)

    result = CliRunner().invoke(cli_app, ["ui"])

    assert result.exit_code == EXIT_UNREACHABLE
    expected = UNREACHABLE_MESSAGE_TEMPLATE.format(url=DEFAULT_ENDPOINT)
    assert expected in result.output


def test_query_endpoint_flag_used(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def capture(
        endpoint: str, path: str, payload: dict[str, Any], *, timeout: float = 0, transport=None
    ):  # noqa: ARG001
        seen["endpoint"] = endpoint
        seen["path"] = path
        seen["payload"] = payload
        seen["timeout"] = timeout
        return {"status": "ok", "results": []}

    _patch_post(monkeypatch, capture)
    result = CliRunner().invoke(
        cli_app,
        ["query", "ai safety", "--endpoint", "http://h:1/", "--k", "5", "--timeout", "7.5"],
    )
    assert result.exit_code == 0, result.output
    assert seen["endpoint"] == "http://h:1/"
    assert seen["path"] == "/query"
    assert seen["payload"] == {"query": "ai safety", "k": 5}
    assert seen["timeout"] == 7.5


def test_query_vault_option_is_forwarded_as_request_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def capture(
        endpoint: str,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float = 0,
        transport=None,
        vault=None,
    ):
        del endpoint, path, payload, timeout, transport
        seen["vault"] = vault
        return {"status": "ok", "results": []}

    _patch_post(monkeypatch, capture)
    result = CliRunner().invoke(cli_app, ["query", "x", "--vault", "research"])

    assert result.exit_code == 0, result.output
    assert seen["vault"] == Path("research")


def test_ask_vault_option_is_forwarded_as_request_selector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def capture(
        endpoint: str,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float = 0,
        transport=None,
        vault=None,
    ):
        del endpoint, path, payload, timeout, transport
        seen["vault"] = vault
        return {"status": "ok", "text": "answer", "citations": []}

    _patch_post(monkeypatch, capture)
    result = CliRunner().invoke(cli_app, ["ask", "x", "--vault", "research"])

    assert result.exit_code == 0, result.output
    assert seen["vault"] == Path("research")



_DEGRADED_BODY = {
    "status": "degraded",
    "text": "",
    "citations": [{"path": "notes/a.md"}],
    "retrieval": {
        "synthesis_status": "no_llm",
        "no_llm_reason": "no LLM model is configured for this vault",
    },
}


@pytest.mark.parametrize("fmt", ["text", "json"])
def test_ask_degraded_answer_exits_17(monkeypatch: pytest.MonkeyPatch, fmt: str) -> None:
    from okto_neuron.cli._client import EXIT_ASK_DEGRADED

    _patch_post(monkeypatch, lambda *a, **k: dict(_DEGRADED_BODY))
    result = CliRunner().invoke(cli_app, ["ask", "x", "--format", fmt])

    assert EXIT_ASK_DEGRADED == 17
    assert result.exit_code == 17, result.output
    if fmt == "json":
        assert json.loads(result.stdout)["status"] == "degraded"
    else:
        assert "(no answer)" in result.stdout
        assert "notes/a.md" in result.stdout
        assert "answer degraded: no_llm (no LLM model is configured" in result.stderr


def test_ask_ok_answer_exits_0(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"status": "ok", "text": "answer", "citations": [], "retrieval": {"synthesis_status": "ok"}}
    _patch_post(monkeypatch, lambda *a, **k: dict(body))
    result = CliRunner().invoke(cli_app, ["ask", "x", "--format", "json"])
    assert result.exit_code == 0, result.output


def test_query_endpoint_env_used(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENDPOINT_ENV_VAR, "http://from-env:8000")
    seen: dict[str, Any] = {}

    def capture(endpoint: str, *args: Any, **kwargs: Any):  # noqa: ARG001
        seen["endpoint"] = endpoint
        return {"status": "ok", "results": []}

    _patch_post(monkeypatch, capture)
    result = CliRunner().invoke(cli_app, ["query", "x"])
    assert result.exit_code == 0
    assert seen["endpoint"] == "http://from-env:8000"


def test_query_default_timeout_is_30s(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def capture(
        endpoint: str, path: str, payload: dict[str, Any], *, timeout: float = 0, transport=None
    ):  # noqa: ARG001
        seen["timeout"] = timeout
        return {"status": "ok", "results": []}

    _patch_post(monkeypatch, capture)
    result = CliRunner().invoke(cli_app, ["query", "x"])
    assert result.exit_code == 0
    assert seen["timeout"] == DEFAULT_TIMEOUT_SECONDS == 30.0


def test_add_posts_file_content(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    note = tmp_path / "n.md"
    note.write_text("hello", encoding="utf-8")
    seen: dict[str, Any] = {}

    def capture(
        endpoint: str, path: str, payload: dict[str, Any], *, timeout: float = 0, transport=None
    ):  # noqa: ARG001
        seen.update(path=path, payload=payload)
        return {"status": "ok", "document_id": "doc_123", "chunks_ingested": 4}

    _patch_post(monkeypatch, capture)
    result = CliRunner().invoke(cli_app, ["add", str(note)])
    assert result.exit_code == 0, result.output
    assert seen["path"] == "/add"
    assert seen["payload"]["path"] == str(note)
    assert seen["payload"]["content"] == "hello"
    assert "doc_123" in result.output


def test_detect_drift_posts_corpus_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def capture(
        endpoint: str, path: str, payload: dict[str, Any], *, timeout: float = 0, transport=None
    ):  # noqa: ARG001
        seen.update(path=path, payload=payload)
        return {"status": "ok", "added": 0, "removed": 0, "changed": 0, "actions": []}

    _patch_post(monkeypatch, capture)
    result = CliRunner().invoke(cli_app, ["detect-drift", "--corpus-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert seen["path"] == "/detect-drift"
    assert seen["payload"]["corpus_root"] == str(tmp_path.resolve())
    assert seen["payload"]["dry_run"] is False


def test_add_does_not_open_vault_directly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Hard cutover: `add` must never call Vault.open."""
    called: list[Any] = []

    def fake_open(*args: Any, **kwargs: Any):  # noqa: ARG001
        called.append((args, kwargs))
        raise AssertionError("Vault.open must not be called from thin-client `add`")

    monkeypatch.setattr("okto_neuron.vault.Vault.open", classmethod(fake_open))
    monkeypatch.setattr(
        "okto_neuron.cli._client.post",
        lambda *a, **kw: {"status": "ok", "document_id": "x", "chunks_ingested": 0},
    )
    monkeypatch.setattr(
        "okto_neuron.cli._client_post",
        lambda *a, **kw: {"status": "ok", "document_id": "x", "chunks_ingested": 0},
    )
    note = tmp_path / "n.md"
    note.write_text("x", encoding="utf-8")
    result = CliRunner().invoke(cli_app, ["add", str(note)])
    assert result.exit_code == 0, result.output
    assert called == []
