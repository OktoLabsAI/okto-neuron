"""QA re-verification of the task #9 hardening controls (regression-locked).

Complements the backend's own unit tests (test_server_api_v1.py H1/L1,
test_mcp_loopback_gate.py M1) by locking the two controls those don't assert
end-to-end: the REST sensitive-write remote-peer policy that the config-PATCH
route gates on, and the CLI refusal to bind a non-loopback host without
--allow-remote. Live no-mock coverage of all four lives in
tests/acceptance/scenarios/93_security_hardening.sh.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from okto_neuron.cli import app


def _fake_request(host: str | None):
    client = types.SimpleNamespace(host=host) if host is not None else None
    return types.SimpleNamespace(client=client, headers={})


# ── M1: REST sensitive-write remote-peer policy (config PATCH gates on this) ──


def test_remote_config_gate_denies_remote_peer_when_not_allow_remote():
    """A real remote peer is always refused config writes.

    This is the exfiltration control the config-PATCH route
    (``if not remote_config_allowed(request): 403``) depends on.
    """
    from okto_neuron.server import http as http_mod

    assert http_mod.remote_config_allowed(_fake_request("203.0.113.7")) is False
    # loopback peers are always allowed
    assert http_mod.remote_config_allowed(_fake_request("127.0.0.1")) is True
    assert http_mod.remote_config_allowed(_fake_request("::1")) is True


def test_remote_config_gate_loopback_only_even_with_allow_remote(monkeypatch):
    """R1: sensitive writes stay loopback-only under compatibility state.

    ``remote_config_allowed`` does not consult ``--allow-remote``: a remote peer
    is refused config-PATCH and MCP remember/init_vault regardless. Production
    startup rejects direct remote serving entirely; this isolated state probe
    proves the compatibility field cannot reopen the write gate.
    """
    from okto_neuron.server import http as http_mod

    monkeypatch.setattr(http_mod, "_allow_remote", lambda: True)
    assert http_mod.remote_config_allowed(_fake_request("203.0.113.7")) is False
    # loopback callers still allowed even when remote access is enabled
    assert http_mod.remote_config_allowed(_fake_request("127.0.0.1")) is True


# ── L2: CLI refuses every non-loopback bind (exit 2) ─────────────────────────


def test_cli_serve_refuses_non_loopback_without_allow_remote(tmp_path):
    vault = tmp_path / "v"
    runner = CliRunner()
    result = runner.invoke(app, ["serve", "--vault", str(vault), "--host", "0.0.0.0"])
    assert result.exit_code == 2, result.output
    assert "refusing to bind to non-loopback host" in result.output
    assert "--allow-remote" in result.output


def test_cli_serve_refuses_non_loopback_even_with_allow_remote(tmp_path):
    vault = tmp_path / "v"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["serve", "--vault", str(vault), "--host", "0.0.0.0", "--allow-remote"],
    )
    assert result.exit_code == 2, result.output
    assert "Direct remote serving" in result.output
    assert "SSH tunnel" in result.output


@pytest.mark.asyncio
async def test_runtime_rejects_remote_bind_even_if_called_directly():
    from okto_neuron.server import runtime

    with pytest.raises(RuntimeError, match="direct remote serving is disabled"):
        await runtime._run_async(None, host="0.0.0.0", allow_remote=True)


# ── L3: an unexpected server fault returns a GENERIC 500 (no str(exc) leak) ────

_SECRET_LEAK = "TOPSECRET-/etc/shadow-traceback-leak"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from okto_neuron import Vault
    from okto_neuron.server import http as http_mod
    from okto_neuron.server.http import build_rest_app
    from okto_neuron.server.state import init_state, reset_state_for_tests
    from okto_neuron.store import vault as vault_module
    from okto_neuron.store.ladybug import VaultConnection

    reset_state_for_tests()
    vault = Vault.init(tmp_path / "v")
    state = init_state(vault, vault.path)
    app_ = build_rest_app(state)
    # base_url loopback so the Host guard (L1) lets requests through.
    with TestClient(app_, base_url="http://127.0.0.1") as c:
        yield c, vault, monkeypatch, http_mod
    reset_state_for_tests()
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def test_recall_unexpected_fault_is_generic_500_no_leak(client) -> None:
    c, vault, monkeypatch, _ = client

    def _boom(*_a, **_k):
        raise RuntimeError(_SECRET_LEAK)

    monkeypatch.setattr(vault, "query_with_metrics", _boom)
    r = c.post("/api/v1/recall", json={"query": "x", "k": 5})
    assert r.status_code == 500
    body = r.json()
    assert body == {"error": "internal", "detail": "internal server error", "status": 500}
    assert _SECRET_LEAK not in r.text  # no exception/traceback leak to the client
