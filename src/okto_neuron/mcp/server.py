"""Fail-closed compatibility wrapper for the retired flat MCP server module."""

from __future__ import annotations

from okto_neuron.mcp_server import build_app, run

__all__ = ["build_app", "run"]
