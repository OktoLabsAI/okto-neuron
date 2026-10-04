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
module is what ``cli.kg._require_offline_graph_swap`` delegates to: take the
vault's writer lease first (refusing with exit code 5 while the daemon or another
command holds it; see ``store/vault_writer.py``), then the cross-process handle
lease. Lock order: writer lease, then handle lease.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Protocol

from okto_neuron.store.handle_lease import VaultHandleLease, acquire_vault_handle_lease
from okto_neuron.store.vault_writer import vault_writer


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

    def __init__(self, lease: VaultHandleLease, stack: contextlib.ExitStack) -> None:
        self._lease = lease
        self._stack = stack

    def __enter__(self) -> RebuildLockHandle:
        return _LadybugRebuildLockHandle(self._lease)

    def __exit__(self, *exc: object) -> None:
        try:
            self._lease.release()
        finally:
            self._stack.close()


def acquire_rebuild_lock(vault_path: Path, *, operation: str) -> RebuildLock:
    """Acquire exclusive cross-process ownership of one vault's graph swap.

    Takes the vault's writer lease (``WriterLeaseHeld``, exit code 5, naming the
    holder pid and the remedy, while the daemon or another command holds it),
    then the non-blocking cross-process handle lease (whose own ``VaultLockHeld``
    propagates unchanged on contention). ``operation`` is diagnostic text folded
    into both messages. Inside the daemon process the writer lease is already
    held, so the in-process ``kg_reembed`` passes straight through.
    """

    stack = contextlib.ExitStack()
    try:
        stack.enter_context(vault_writer(vault_path, operation))
        lease = acquire_vault_handle_lease(vault_path, operation=operation)
    except BaseException:
        stack.close()
        raise
    return _LadybugRebuildLock(lease, stack)


__all__ = [
    "RebuildLock",
    "RebuildLockHandle",
    "acquire_rebuild_lock",
]
