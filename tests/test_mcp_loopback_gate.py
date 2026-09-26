"""Unit tests for the MCP sensitive-write gate (task #9 / M1).

The legacy-named ``runtime._mcp_kg_add_allowed`` helper now gates the current
MCP ``remember``/``init_vault`` writes and delegates to the backend's shared
``remote_config_allowed`` loopback-peer policy, so the MCP sensitive-write gate
and the REST config-PATCH gate enforce one rule. Direct remote serving is
disabled; these tests pin the deny path so the control cannot silently fail
open.
"""

from __future__ import annotations

import types

import okto_neuron.server.runtime as runtime


def _patch_request(monkeypatch, host: str | None):
    client = types.SimpleNamespace(host=host) if host is not None else None
    request = types.SimpleNamespace(client=client, headers={})
    monkeypatch.setattr("fastmcp.server.dependencies.get_http_request", lambda: request)


def test_loopback_ipv4_allowed(monkeypatch):
    _patch_request(monkeypatch, "127.0.0.1")
    assert runtime._mcp_kg_add_allowed() is True


def test_loopback_ipv6_allowed(monkeypatch):
    _patch_request(monkeypatch, "::1")
    assert runtime._mcp_kg_add_allowed() is True


def test_remote_peer_denied_without_allow_remote(monkeypatch):
    _patch_request(monkeypatch, "1.2.3.4")
    assert runtime._mcp_kg_add_allowed() is False


def test_no_http_context_is_local(monkeypatch):
    def _raise():
        raise RuntimeError("no request context")

    monkeypatch.setattr("fastmcp.server.dependencies.get_http_request", _raise)
    assert runtime._mcp_kg_add_allowed() is True
