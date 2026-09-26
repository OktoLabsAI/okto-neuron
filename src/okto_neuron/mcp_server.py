"""Retired compatibility entry points for the pre-runtime MCP server.

The production MCP surface is owned exclusively by ``okto-neuron serve``. The
old flat module built a second, unauthenticated eight-tool server and allowed an
arbitrary bind host, bypassing the production five-tool capability-token and
loopback-only contract. Imports remain valid for compatibility, but attempting
to construct or run that retired server fails closed.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

if TYPE_CHECKING:
    from okto_neuron.vault import Vault


_RETIRED_MESSAGE = (
    "the legacy okto_neuron.mcp_server API is retired because it bypassed the "
    "production authentication and loopback-only contract; run `okto-neuron serve` "
    "after installing okto-neuron[serve]"
)


def _refuse_legacy_server() -> NoReturn:
    raise RuntimeError(_RETIRED_MESSAGE)


def kg_query_natural(vault: Vault, text: str, k: int = 10) -> list[dict[str, object]]:
    """Retain the legacy pure query serializer without constructing a server."""
    return [
        {
            "claim_id": hit.claim_id,
            "score": hit.score,
            "path": hit.path,
            "byte_start": hit.byte_start,
            "byte_end": hit.byte_end,
            "content_hash": hit.content_hash,
            "title": hit.node.title,
            "name": hit.node.name,
        }
        for hit in vault.query(text, k=k)
    ]


marginalia_kg_query = kg_query_natural


def build_app(vault_path: str | Path) -> NoReturn:
    """Fail closed instead of constructing the retired independent server."""
    del vault_path
    _refuse_legacy_server()


def run(vault_path: str | Path, host: str = "127.0.0.1", port: int = 8201) -> NoReturn:
    """Fail closed instead of binding the retired independent server."""
    del vault_path, host, port
    _refuse_legacy_server()


__all__ = ["build_app", "kg_query_natural", "marginalia_kg_query", "run"]
