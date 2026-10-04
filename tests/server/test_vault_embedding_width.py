"""Embedding-width lifecycle of a vault (from a field report).

A vault is created at the application default width (384). Editing its own yaml to another
width afterwards makes every open fail with ``EmbeddingDimMismatch``. These tests pin:

* the client-facing error (REST and MCP): the real reason, the working remedy, no path;
* the remedy itself is not a dead end: ``POST /api/v1/vaults/reembed`` re-embeds a vault
  that cannot be opened (``POST /api/v1/curation/reembed`` cannot: it needs an open vault);
* creating a vault WITH its embedding spec (REST ``embedding`` object, MCP ``embedding_*``);
* the stored-vs-configured width report on both config PATCH routes;
* the CLI refusal naming the daemon route.

Real grafx graphs under a scratch HOME; no mocks of the width logic.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from okto_neuron.config._vault import validate_new_vault_embedding_spec
from okto_neuron.errors import EmbeddingDimMismatch
from okto_neuron.server import http as http_mod
from okto_neuron.server import runtime
from okto_neuron.server._open_failure import (
    EMBEDDING_DIM_MISMATCH_CODE,
    client_open_failure,
    mismatch_cause,
    reembed_remedy,
)
from okto_neuron.server._vault_pool import VaultPoolError
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests
from okto_neuron.store.vault_writer import remedy_for

HOME_LEAKS = ("/Users", "/private", "/var/folders", "/home/", ".okto-neuron", ".marginalia")


@pytest.fixture
def app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("OKTO_NEURON_HOME", "OKTO_NEURON_CONFIG", "OKTO_NEURON_VAULT", "MARGINALIA_HOME"):
        monkeypatch.delenv(name, raising=False)
    reset_state_for_tests()
    state = init_state(None, None)
    with TestClient(build_rest_app(state), base_url="http://127.0.0.1") as client:
        yield client, state, home
    reset_state_for_tests()


def _vault_dir(home: Path, name: str) -> Path:
    return home / ".okto-neuron" / "vaults" / name


def _open_width(state, path: Path) -> int:
    """The width the graph was built at, read from the live handle (opens the vault)."""
    runtime_ = state.runtime_for(path)
    lease = runtime_.lease_vault()
    try:
        width = http_mod._stored_embedding_width(lease.vault)
    finally:
        lease.release()
    assert width is not None
    return width


def _create(client, name: str, **extra) -> dict:
    response = client.post("/api/v1/vaults", json={"name": name, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def _set_width(home: Path, name: str, dimension: int, provider: str = "stub") -> None:
    path = _vault_dir(home, name) / "okto-neuron.yaml"
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg["embedding"] = {"provider": provider, "dimension": dimension}
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _assert_path_free(text: str, home: Path, tmp_path: Path) -> None:
    for fragment in (str(tmp_path), str(home), *HOME_LEAKS):
        assert fragment not in text, f"{fragment!r} leaked into client text: {text}"


# --------------------------------------------------------------------------------------
# (b1) the embedding spec: validation matrix
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec, expected",
    [
        ({"provider": "stub", "dimension": 4096}, {"provider": "stub", "dimension": 4096}),
        (
            {
                "provider": "openai-compat",
                "model": "Qwen3-Embedding",
                "dimension": 4096,
                "api_base": "http://localhost:8123/v1",
                "api_key_env": "OKTO_NEURON_EMBED_KEY",
            },
            {
                "provider": "openai",
                "model": "Qwen3-Embedding",
                "dimension": 4096,
                "api_base": "http://localhost:8123/v1",
                "api_key_env": "OKTO_NEURON_EMBED_KEY",
            },
        ),
        ({"provider": "fastembed"}, {"provider": "fastembed"}),
        (
            {"model": "BAAI/bge-base-en-v1.5", "dimension": 768},
            {"model": "BAAI/bge-base-en-v1.5", "dimension": 768},
        ),
        ({}, {}),
    ],
)
def test_embedding_spec_accepts_valid_specs(spec, expected):
    assert validate_new_vault_embedding_spec(spec) == expected


@pytest.mark.parametrize(
    "spec, fragment",
    [
        ("stub", "must be an object"),
        ({"dimension": 4096}, "name the embedding provider/model"),
        ({"dimension": 4096, "provider": "fastembed"}, "name the embedding provider/model"),
        (
            {"dimension": 4096, "provider": "fastembed", "model": "BAAI/bge-small-en-v1.5"},
            "default local model",
        ),
        ({"dimension": 0, "provider": "stub"}, "positive integer"),
        ({"dimension": -1, "provider": "stub"}, "positive integer"),
        ({"dimension": True, "provider": "stub"}, "positive integer"),
        ({"dimension": "4096", "provider": "stub"}, "positive integer"),
        ({"provider": "no-such-provider"}, "unknown embedding provider"),
        ({"provider": ""}, "non-empty string"),
        (
            {"provider": "openai", "model": "m", "api_base": "http://10.1.2.3:8123/v1"},
            "not loopback",
        ),
        ({"provider": "openai", "model": "m", "api_key_env": "PATH"}, "api_key_env"),
        ({"provider": "stub", "surprise": 1}, "unknown embedding field"),
        ({"provider": "stub", "allow_remote": "yes"}, "must be a boolean"),
    ],
)
def test_embedding_spec_refuses_invalid_specs(spec, fragment):
    with pytest.raises(ValueError) as excinfo:
        validate_new_vault_embedding_spec(spec)
    assert fragment in str(excinfo.value)


def test_embedding_spec_allows_a_remote_endpoint_only_when_opted_in():
    spec = {
        "provider": "openai",
        "model": "m",
        "api_base": "https://example.org/v1",
        "dimension": 8,
    }
    with pytest.raises(ValueError):
        validate_new_vault_embedding_spec(spec)
    assert validate_new_vault_embedding_spec({**spec, "allow_remote": True})["allow_remote"] is True


# --------------------------------------------------------------------------------------
# (b1) REST creation with and without the spec
# --------------------------------------------------------------------------------------


def test_rest_create_without_spec_is_unchanged(app):
    client, state, home = app
    body = _create(client, "plain")
    assert "embedding" not in body
    written = yaml.safe_load(
        (_vault_dir(home, "plain") / "okto-neuron.yaml").read_text(encoding="utf-8")
    )
    assert "dimension" not in str(written.get("embedding", {}))  # no spec written
    assert _open_width(state, _vault_dir(home, "plain")) == 384


def test_rest_create_with_spec_creates_the_graph_at_that_width(app):
    client, state, home = app
    body = _create(client, "wide", embedding={"provider": "stub", "dimension": 768})
    assert body["embedding"] == {"provider": "stub", "dimension": 768}
    written = yaml.safe_load(
        (_vault_dir(home, "wide") / "okto-neuron.yaml").read_text(encoding="utf-8")
    )
    assert written["embedding"]["dimension"] == 768
    assert _open_width(state, _vault_dir(home, "wide")) == 768
    # and it keeps opening: no mismatch against its own config
    response = client.get("/api/v1/status", headers={"X-Okto-Neuron-Vault": "wide"})
    assert response.status_code == 200, response.text


@pytest.mark.parametrize(
    "spec",
    [
        {"dimension": 4096},
        {"provider": "stub", "dimension": 0},
        {"provider": "nope"},
        {"provider": "openai", "model": "m", "api_base": "http://10.1.2.3:8123/v1"},
        "stub",
        {"provider": "stub", "extra": 1},
    ],
)
def test_rest_create_refuses_a_bad_spec_before_creating_anything(app, spec):
    client, state, home = app
    response = client.post("/api/v1/vaults", json={"name": "bad", "embedding": spec})
    assert response.status_code == 400, response.text
    assert response.json()["error"] == "bad_request"
    assert not _vault_dir(home, "bad").exists()


def test_rest_create_refuses_conflicting_embedder_and_spec_provider(app):
    client, _state, home = app
    response = client.post(
        "/api/v1/vaults",
        json={
            "name": "conflict",
            "embedder": "fastembed",
            "embedding": {"provider": "stub", "dimension": 8},
        },
    )
    assert response.status_code == 400
    assert "disagree" in response.json()["detail"]
    assert not _vault_dir(home, "conflict").exists()


# --------------------------------------------------------------------------------------
# (a) the client-facing error
# --------------------------------------------------------------------------------------


def _mismatch_exc(path: Path) -> VaultPoolError:
    cause = EmbeddingDimMismatch(
        path / "graph.grafx", stored_dim=384, configured_dim=4096, vault_path=path
    )
    pool = VaultPoolError("open_failed", f"could not open vault at {path}: {cause.user_message()}")
    pool.__cause__ = cause
    return pool


def test_mismatch_cause_is_found_through_the_chain(tmp_path):
    exc = _mismatch_exc(tmp_path / "v")
    assert isinstance(mismatch_cause(exc), EmbeddingDimMismatch)
    assert mismatch_cause(VaultPoolError("open_failed", "disk on fire")) is None
    assert client_open_failure(VaultPoolError("open_failed", "disk on fire")) is None


def test_client_message_names_widths_remedy_and_no_path(tmp_path):
    code, message = client_open_failure(
        _mismatch_exc(tmp_path / "secret-dir" / "field-vault"), base_url="http://127.0.0.1:7777"
    )
    assert code == EMBEDDING_DIM_MISMATCH_CODE == "embedding_dim_mismatch"
    assert "4096" in message and "384" in message
    assert "curl -X POST http://127.0.0.1:7777/api/v1/vaults/reembed" in message
    assert "/api/v1/curation/reembed" not in message
    assert "kg reembed" in message and "refused while the daemon holds the vault" in message
    assert "embedding.dimension back to 384" in message
    for fragment in (str(tmp_path), "secret-dir", *HOME_LEAKS):
        assert fragment not in message


def test_daemon_base_url_uses_the_serving_port_when_known(app):
    _client, state, _home = app
    state.rest_port = 7791
    assert reembed_remedy("field-vault").count("http://127.0.0.1:7791/api/v1/vaults/reembed") == 1
    state.rest_port = None
    assert "<REST port>" in reembed_remedy("field-vault")


def test_rest_open_failure_carries_reason_and_remedy_without_a_path(app, tmp_path):
    client, state, home = app
    _create(client, "mm")
    _set_width(home, "mm", 4096)
    state.rest_port = 7791
    response = client.get("/api/v1/status", headers={"X-Okto-Neuron-Vault": "mm"})
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "embedding_dim_mismatch"
    detail = body["detail"]
    assert "vault 'mm'" in detail and "4096" in detail and "384" in detail
    assert "curl -X POST http://127.0.0.1:7791/api/v1/vaults/reembed" in detail
    assert '{"vault": "mm"}' in detail
    _assert_path_free(detail, home, tmp_path)


@pytest.mark.asyncio
async def test_mcp_open_failure_carries_reason_and_remedy_without_a_path(app, tmp_path):
    from fastmcp import Client

    client, state, home = app
    _create(client, "mcpmm")
    _set_width(home, "mcpmm", 4096)
    server = runtime._build_mcp_server(state)
    async with Client(server) as mcp:
        with pytest.raises(Exception) as excinfo:
            await mcp.call_tool("explore", {"topic": "x", "vault": "mcpmm"})
    message = str(excinfo.value)
    assert message.count("embedding_dim_mismatch") >= 1
    assert "vault 'mcpmm'" in message and "4096" in message and "384" in message
    assert "/api/v1/vaults/reembed" in message
    assert "see the server log" not in message
    _assert_path_free(message, home, tmp_path)


# --------------------------------------------------------------------------------------
# the remedy is not a dead end
# --------------------------------------------------------------------------------------


def _wait_reembed_done(client, name: str, timeout: float = 60.0) -> None:
    """Wait until the vault opens again: the fence lifts only when the reembed has finished."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = client.get("/api/v1/status", headers={"X-Okto-Neuron-Vault": name})
        if last.status_code == 200:
            return
        if last.status_code == 409 and last.json().get("error") not in {"vault_fenced", "busy"}:
            break  # a different refusal: the reembed did not repair it
        time.sleep(0.2)
    raise AssertionError(
        f"reembed did not finish: {last.status_code if last else None} {last.text if last else ''}"
    )


def test_vaults_reembed_repairs_a_vault_that_cannot_open_and_curation_reembed_cannot(app):
    client, state, home = app
    _create(client, "repair")
    _set_width(home, "repair", 4096)
    header = {"X-Okto-Neuron-Vault": "repair"}

    # The route the OLD remedy named needs an open vault: it is answered before it runs.
    dead_end = client.post("/api/v1/curation/reembed", json={}, headers=header)
    assert dead_end.status_code == 409
    assert dead_end.json()["error"] == "embedding_dim_mismatch"

    # The route the remedy names now: loopback, works while the vault cannot be opened.
    started = client.post("/api/v1/vaults/reembed", json={"vault": "repair"})
    assert started.status_code == 202, started.text
    _wait_reembed_done(client, "repair")

    opened = client.get("/api/v1/status", headers=header)
    assert opened.status_code == 200, opened.text
    assert _open_width(state, _vault_dir(home, "repair")) == 4096
    assert (_vault_dir(home, "repair") / "graph.grafx.bak").exists()


def test_a_vault_without_a_mismatch_is_unaffected_by_the_remedy_route_and_still_opens(app):
    client, state, home = app
    _create(client, "healthy")
    assert (
        client.get("/api/v1/status", headers={"X-Okto-Neuron-Vault": "healthy"}).status_code == 200
    )
    assert _open_width(state, _vault_dir(home, "healthy")) == 384


def test_reembed_route_does_not_bypass_the_guard_for_ordinary_requests(app):
    """While the mismatch stands, only the reembed route opens the vault: every other call is refused."""
    client, _state, home = app
    _create(client, "guarded")
    _set_width(home, "guarded", 4096)
    header = {"X-Okto-Neuron-Vault": "guarded"}
    for path in ("/api/v1/status", "/api/v1/graph/stats", "/api/v1/curation/jobs"):
        response = client.get(path, headers=header)
        assert response.status_code == 409, (path, response.text)
        assert response.json()["error"] == "embedding_dim_mismatch"


# --------------------------------------------------------------------------------------
# (c) config PATCH reports stored vs configured widths
# --------------------------------------------------------------------------------------


def test_vault_patch_reports_an_open_vaults_stored_width_and_the_remedy(app, tmp_path):
    client, state, home = app
    _create(client, "pw")
    header = {"X-Okto-Neuron-Vault": "pw"}
    assert client.get("/api/v1/status", headers=header).status_code == 200  # opens the graph (384)
    response = client.patch(
        "/api/v1/config",
        json={"embedding": {"provider": "stub", "dimension": 4096}},
        headers=header,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == "reembed"
    [entry] = body["embedding_width"]
    assert entry["vault"] == "pw"
    assert entry["stored"] == 384 and entry["checked"] is True
    assert entry["configured_after"] == 4096 and entry["refuses_to_open"] is True
    notes = " ".join(body["notes"])
    assert "stored graph width 384" in notes and "4096" in notes
    assert "refuse to open" in notes and "/api/v1/vaults/reembed" in notes
    _assert_path_free(notes, home, tmp_path)


def test_vault_patch_without_a_width_change_adds_no_width_report(app):
    client, _state, _home = app
    _create(client, "quiet")
    header = {"X-Okto-Neuron-Vault": "quiet"}
    response = client.patch(
        "/api/v1/config", json={"embedding": {"batch_size": 16}}, headers=header
    )
    assert response.status_code == 200, response.text
    assert "embedding_width" not in response.json()


def test_defaults_patch_reports_inheriting_vaults_not_open_as_unchecked(app, tmp_path):
    client, _state, home = app
    _create(client, "inheritor")
    response = client.patch(
        "/api/v1/config/defaults", json={"embedding": {"provider": "stub", "dimension": 1024}}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == "reembed"
    entry = next(item for item in body["embedding_width"] if item["vault"] == "inheritor")
    assert entry["checked"] is False and entry["stored"] is None
    assert entry["configured_before"] == 384 and entry["configured_after"] == 1024
    notes = " ".join(body["notes"])
    assert "was not read" in notes and "inheritor" in notes and "/api/v1/vaults/reembed" in notes
    _assert_path_free(notes, home, tmp_path)


def test_stored_width_is_read_from_the_open_handle_not_by_opening(app):
    client, state, home = app
    _create(client, "cold")
    path = _vault_dir(home, "cold")
    assert (
        http_mod._stored_embedding_width(state.vault_pool.peek(path)) is None
    )  # not open: not read
    assert client.get("/api/v1/status", headers={"X-Okto-Neuron-Vault": "cold"}).status_code == 200
    assert http_mod._stored_embedding_width(state.vault_pool.peek(path)) == 384


# --------------------------------------------------------------------------------------
# MCP init_vault with the spec
# --------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_init_vault_with_spec_creates_the_graph_at_that_width(app):
    from fastmcp import Client

    _client, state, home = app
    server = runtime._build_mcp_server(state)
    async with Client(server) as mcp:
        result = await mcp.call_tool(
            "init_vault",
            {"name": "mcpwide", "embedding_provider": "stub", "embedding_dimension": 512},
        )
        data = result.structured_content or {}
        assert data["embedding"] == {"provider": "stub", "dimension": 512}
        with pytest.raises(Exception) as excinfo:
            await mcp.call_tool("init_vault", {"name": "mcpbad", "embedding_dimension": 4096})
        assert "bad_request" in str(excinfo.value)
        assert "name the embedding provider/model" in str(excinfo.value)
    assert _open_width(state, _vault_dir(home, "mcpwide")) == 512
    assert not _vault_dir(home, "mcpbad").exists()


@pytest.mark.asyncio
async def test_mcp_init_vault_without_spec_has_no_embedding_echo(app):
    from fastmcp import Client

    _client, state, home = app
    server = runtime._build_mcp_server(state)
    async with Client(server) as mcp:
        result = await mcp.call_tool("init_vault", {"name": "mcpplain"})
        assert "embedding" not in (result.structured_content or {})
    assert _open_width(state, _vault_dir(home, "mcpplain")) == 384


# --------------------------------------------------------------------------------------
# (d) the CLI refusal names the daemon route
# --------------------------------------------------------------------------------------


def test_cli_reembed_refusal_names_the_route_that_works_on_an_unopenable_vault(tmp_path):
    text = remedy_for("reembed", "daemon", vault_path=tmp_path / "field-vault")
    assert "curl -X POST http://127.0.0.1:7777/api/v1/vaults/reembed" in text
    assert '"vault":' in text and "field-vault" in text
    assert "/api/v1/curation/reembed" not in text
    assert "Re-embed button" in text and "okto-neuron stop" in text
    # other operations keep their generic daemon equivalent
    assert "POST /api/v1/curation/rebuild" in remedy_for("rebuild", "daemon")
    assert "another okto-neuron command" in remedy_for("reembed", "cli")
