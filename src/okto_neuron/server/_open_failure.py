"""Client-facing text for a vault open that failed on an embedding-width mismatch.

A vault whose graph was created at one embedding width and whose config now asks
for another cannot be opened (``EmbeddingDimMismatch``; the vector column is
fixed-width). The pool wraps that as ``VaultPoolError("open_failed", ...)`` and the
MCP/REST boundaries used to drop the reason (the message embeds an absolute path)
and answer "vault ... is not available; see the server log". The cause and the
remedy are not secrets and are not paths, so they are rebuilt here, path-free, for
the client. The full original stays in the server log.

The remedy is ``POST /api/v1/vaults/reembed``: it is the route that works while the
vault cannot be opened. ``POST /api/v1/curation/reembed`` runs against an OPEN
vault, so on a mismatched vault the request is answered 409 before the route runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from okto_neuron.errors import EmbeddingDimMismatch

#: Stable machine code for this failure on every client surface.
EMBEDDING_DIM_MISMATCH_CODE = "embedding_dim_mismatch"

_DEFAULT_REST_PORT_HINT = "<REST port>"


def mismatch_cause(exc: BaseException | None) -> EmbeddingDimMismatch | None:
    """Return the ``EmbeddingDimMismatch`` behind ``exc`` (itself or its cause chain)."""
    seen: set[int] = set()
    current = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, EmbeddingDimMismatch):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def registered_vault_name(vault_path: Path | str | None) -> str | None:
    """The registry name of ``vault_path``, or ``None`` (a name is safe to show; a path is not)."""
    if vault_path is None:
        return None
    try:
        from okto_neuron.vault_registry import list_vaults

        target = Path(vault_path).expanduser().resolve(strict=False)
        for entry in list_vaults():
            if entry.path.resolve(strict=False) == target:
                return entry.name
    except Exception:  # noqa: BLE001 - a label is never worth failing the error path
        return None
    return None


def daemon_base_url() -> str:
    """The loopback REST base URL of this daemon, or a placeholder when unknown."""
    port: Any = None
    try:
        from okto_neuron.server.state import get_server_state

        port = getattr(get_server_state(), "rest_port", None)
    except Exception:  # noqa: BLE001
        port = None
    return f"http://127.0.0.1:{port if port else _DEFAULT_REST_PORT_HINT}"


def reembed_remedy(vault_name: str | None, *, base_url: str | None = None) -> str:
    """The copy-pasteable remedy for a vault that cannot open at its configured width."""
    base = base_url or daemon_base_url()
    ident = vault_name or "<vault name>"
    return (
        "Rebuild its vectors at the configured width through the running daemon (loopback only; "
        "this route works while the vault cannot be opened): "
        f"curl -X POST {base}/api/v1/vaults/reembed -H 'Content-Type: application/json' "
        f'-d \'{{"vault": "{ident}"}}\'  (or use the vault manager\'s Re-embed button). '
        "`okto-neuron kg reembed` is refused while the daemon holds the vault."
    )


def mismatch_client_message(
    cause: EmbeddingDimMismatch,
    *,
    vault_name: str | None = None,
    base_url: str | None = None,
) -> str:
    """Path-free text naming the real reason and the remedy (no absolute path, no home dir)."""
    who = f"vault '{vault_name}'" if vault_name else "the selected vault"
    widths = (
        f"the config asks for embedding width {cause.configured_dim} but the stored graph "
        f"width is {cause.stored_dim}"
        if cause.configured_dim is not None and cause.stored_dim is not None
        else "the configured embedding width differs from the stored graph width"
    )
    keep = (
        f" To keep the stored width instead, set embedding.dimension back to {cause.stored_dim} "
        "in that vault's okto-neuron.yaml."
        if cause.stored_dim is not None
        else ""
    )
    return (
        f"{who} cannot be opened: {widths}. {reembed_remedy(vault_name, base_url=base_url)}{keep}"
    )


def client_open_failure(
    exc: BaseException,
    *,
    vault_path: Path | str | None = None,
    base_url: str | None = None,
) -> tuple[str, str] | None:
    """``(code, message)`` for a client when ``exc`` is a width-mismatch open failure, else ``None``."""
    cause = mismatch_cause(exc)
    if cause is None:
        return None
    name = registered_vault_name(vault_path or cause.vault_path)
    return EMBEDDING_DIM_MISMATCH_CODE, mismatch_client_message(
        cause, vault_name=name, base_url=base_url
    )


__all__ = [
    "EMBEDDING_DIM_MISMATCH_CODE",
    "client_open_failure",
    "daemon_base_url",
    "mismatch_cause",
    "mismatch_client_message",
    "registered_vault_name",
    "reembed_remedy",
]
