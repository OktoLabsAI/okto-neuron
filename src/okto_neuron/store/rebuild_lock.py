"""RebuildLock port — cross-process exclusivity for an offline graph swap.

M2b's Ladybug-only slice (spec §2.2): a thin wrapper around the existing
handle-lease primitives so ``curation/orchestrate.py``'s offline call sites
depend on a named port, not ``store.handle_lease`` directly. This module is
already backend-agnostic — ``acquire_rebuild_lock`` is keyed purely on
``vault_path`` with no Ladybug-specific logic — so it covers Okto Grafx (M4)
unmodified: Grafx exposes no public cross-transaction exclusive-lease
primitive to delegate to (verified live), so there is nothing
backend-specific to wrap. A future backend that does own its own exclusive
lease (e.g. Neo4j) would add a new ``RebuildLock`` implementation of the same
two Protocols with zero call-site changes; only the Ladybug-backed adapter
lives here today.

Deliberately does not import ``okto_neuron.cli.kg`` — ``store/`` is a lower
layer than ``cli/``, and importing up out of it risks a circular import. This
module instead reimplements ``cli.kg._require_offline_graph_swap``'s two
checks (refuse while a server owns the vault, else acquire the cross-process
handle lease) by importing the exact same primitives that function uses:
``okto_neuron.server.lifecycle.active_server_pid`` and
``okto_neuron.store.handle_lease.acquire_vault_handle_lease``. Behavior,
ordering, and the raised ``VaultLockHeld`` (including its message text) are
unchanged from today's CLI path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from okto_neuron.errors import VaultLockHeld
from okto_neuron.store.handle_lease import VaultHandleLease, acquire_vault_handle_lease


class RebuildLockHandle(Protocol):
    def require_held(self) -> None:
        """Fail closed if ownership was lost before a destructive boundary."""
        ...


class RebuildLock(Protocol):
    def __enter__(self) -> RebuildLockHandle: ...
    def __exit__(self, *exc: object) -> None: ...


class _LadybugRebuildLockHandle:
    """``RebuildLockHandle`` that delegates to a held ``VaultHandleLease``."""

    def __init__(self, lease: VaultHandleLease) -> None:
        self._lease = lease

    def require_held(self) -> None:
        self._lease.require_held()


class _LadybugRebuildLock:
    """``RebuildLock`` around one ``VaultHandleLease`` acquired for a vault."""

    def __init__(self, lease: VaultHandleLease) -> None:
        self._lease = lease

    def __enter__(self) -> RebuildLockHandle:
        return _LadybugRebuildLockHandle(self._lease)

    def __exit__(self, *exc: object) -> None:
        self._lease.release()


def acquire_rebuild_lock(vault_path: Path, *, operation: str) -> RebuildLock:
    """Acquire exclusive cross-process ownership of one vault's graph swap.

    Mirrors ``cli.kg._require_offline_graph_swap`` exactly: refuse with
    ``VaultLockHeld`` naming the owning pid while a server owns this vault,
    else take the non-blocking cross-process handle lease (whose own
    ``VaultLockHeld`` propagates unchanged on contention). ``operation`` is
    diagnostic text folded into both messages, same as today.
    """

    from okto_neuron.server.lifecycle import active_server_pid

    owner_pid = active_server_pid(vault_path)
    if owner_pid is not None:
        raise VaultLockHeld(
            vault_path,
            holding_pid=owner_pid,
            message=(
                f"cannot {operation} while the Okto Neuron server is running for this vault "
                f"(pid {owner_pid}); stop the server first"
            ),
        )
    lease = acquire_vault_handle_lease(vault_path, operation=operation)
    return _LadybugRebuildLock(lease)


__all__ = [
    "RebuildLock",
    "RebuildLockHandle",
    "acquire_rebuild_lock",
]
