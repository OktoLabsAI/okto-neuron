"""CLI side of the per-vault writer lease (#21).

``vault_writer(path, operation)`` takes the vault's writer lease before a command
opens the vault and releases it on exit. When the daemon (or another CLI command)
already holds it the command is refused with :class:`WriterLeaseHeld` (exit code
5) whose message names the holder's pid and says what to do instead: the
equivalent call on the running daemon, or stop the daemon first. The CLI never
proxies a refused command to the daemon on the user's behalf.

Commands that only read a vault open it without a lease and never call this.
Commands that create a new vault path call it too (nobody else holds a brand-new
path, so it simply succeeds).

Re-entrant: when this process already holds the lease (the daemon running
``kg_reembed`` in-process) the guard passes through.
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Iterator

from okto_neuron.errors import VaultPathNotADirectory, VaultPathNotWritable
from okto_neuron.store.writer_lease import (
    WriterLease,
    WriterLeaseHeld,
    acquire_writer_lease,
    held_writer_lease,
)

_STOP = "stop the daemon first (`okto-neuron stop`), then re-run this command"

# Operation -> the running daemon's equivalent, when it has one. Operations not
# listed have no API equivalent and fall back to "stop the daemon first".
_API_EQUIVALENT: dict[str, str] = {
    "rebuild": "POST /api/v1/curation/rebuild",
    "reembed": "POST /api/v1/vaults/reembed",
    "reconcile heal": "POST /api/v1/curation/heal",
    "reconcile propose": "POST /api/v1/reconcile/propose",
    "reconcile apply": "POST /api/v1/reconcile/apply",
    "reconcile review confirm": "POST /api/v1/reconcile/review/confirm",
    "reconcile review reject": "POST /api/v1/reconcile/review/reject",
    "init --wipe": "POST /api/v1/reset",
    "onboard": "PATCH /api/v1/config",
    "plans resume": "POST /api/v1/ingest (with the plan's source)",
}


# server/runtime.py DEFAULT_REST_PORT (kept literal: store/ never imports server/)
_DEFAULT_REST_PORT = 7777


_LOOPBACK_ENDPOINT = re.compile(r"http://(127\.0\.0\.1|localhost|\[::1\]):\d{1,5}")


def _reembed_remedy(vault_path: Path | str | None, endpoint: str | None = None) -> str:
    """The daemon-side re-embed, as a copy-pasteable line.

    ``POST /api/v1/vaults/reembed`` is named (not ``/api/v1/curation/reembed``) because it is
    the route that works while the vault CANNOT be opened, which is exactly when a re-embed is
    needed: the embedding width in the config differs from the stored graph width, and every
    request that borrows the vault is answered 409 before the curation route can run.

    ``endpoint`` is the holding daemon's REST URL from its lease record; a lease written
    without one (an older daemon) falls back to the default port. The REST surface takes no
    token (loopback only), so the line needs no credential.
    """
    ident = str(Path(vault_path).expanduser().resolve(strict=False)) if vault_path else "<vault>"
    if endpoint and _LOOPBACK_ENDPOINT.fullmatch(endpoint):
        base, port_note = endpoint, "(the REST URL of the daemon holding this vault)"
    else:
        base = f"http://127.0.0.1:{_DEFAULT_REST_PORT}"
        port_note = (
            f"({_DEFAULT_REST_PORT} is the default REST port; use your daemon's if you changed it)"
        )
    return (
        "use the running daemon instead (it works even while the vault cannot be opened because "
        "its embedding width differs from the config):\n"
        f"  curl -X POST {base}/api/v1/vaults/reembed "
        f"-H 'Content-Type: application/json' -d '{json.dumps({'vault': ident})}'\n"
        f"{port_note}, or the Re-embed button in the vault manager\n"
        f"or {_STOP}"
    )


def remedy_for(
    operation: str,
    holder_role: str | None,
    *,
    vault_path: Path | str | None = None,
    endpoint: str | None = None,
) -> str:
    """The "what to do instead" lines appended to a refusal."""
    if holder_role != "daemon":
        return (
            "another okto-neuron command is writing this vault; wait for it to finish, "
            "then re-run this command"
        )
    if operation == "reembed":
        return _reembed_remedy(vault_path, endpoint)
    api = _API_EQUIVALENT.get(operation)
    if api is None:
        return f"{_STOP}. This operation has no API equivalent."
    return f"use the running daemon instead: {api}\nor {_STOP}"


@contextlib.contextmanager
def vault_writer(
    vault_path: Path | str,
    operation: str,
    *,
    remedy: str | None = None,
) -> Iterator[WriterLease]:
    """Hold ``vault_path``'s writer lease for one CLI write, or refuse (exit 5)."""
    existing = held_writer_lease(vault_path)
    if existing is not None:
        yield existing
        return
    resolved = Path(vault_path).expanduser().resolve(strict=False)
    if resolved.exists() and not resolved.is_dir():
        raise VaultPathNotADirectory(resolved)
    try:
        lease = acquire_writer_lease(vault_path, role="cli", operation=operation)
    except PermissionError as exc:
        raise VaultPathNotWritable(resolved, cause=exc) from exc
    except WriterLeaseHeld as exc:
        raise WriterLeaseHeld(
            exc.vault_path,
            exc.holder,
            message=f"cannot {operation}: {exc.message}",
            remedy=remedy
            or remedy_for(
                operation, exc.holder.role, vault_path=resolved, endpoint=exc.holder.endpoint
            ),
        ) from None
    try:
        yield lease
    finally:
        lease.release()


__all__ = ["remedy_for", "vault_writer"]
