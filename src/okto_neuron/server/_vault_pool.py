"""Lease-aware per-process ownership of Ladybug vault handles (ADR 0034).

``Vault.close()`` is path-wide: closing one handle invalidates every user of
that vault path.  The pool therefore owns exactly one live handle per resolved
path.  New code borrows that handle through :meth:`VaultPool.lease`; the lease
count is the proof that idle eviction or managed-vault deletion is safe.

``get_or_open`` and ``adopt`` remain as compatibility entry points for callers
that have not yet been converted to scoped leases.  Their paths are pinned and
cannot be evicted or released accidentally.  Application/runtime code should
use ``lease`` (or ``adopt(..., pin=False)`` for a handle opened during startup).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Generic, TypeVar

from okto_neuron.errors import OktoNeuronError
from okto_neuron.server.lifecycle import shutdown_phase
from okto_neuron.store.handle_lease import (
    VaultHandleLease,
    acquire_vault_handle_lease,
)
from okto_neuron.store.writer_lease import (
    WriterLease,
    WriterLeaseHeld,
    acquire_writer_lease,
)
from okto_neuron.vault import Vault


class VaultPoolError(OktoNeuronError):
    """A vault could not be served or released from the pool.

    Stable ``code`` values are ``pool_full``, ``open_failed``, ``vault_fenced``,
    ``vault_in_use``, ``vault_busy`` (another process holds the vault's writer
    lease), and ``legacy_pinned``.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def acquire_daemon_writer_lease(path: Path | str) -> WriterLease:
    """Take (idempotently, without waiting) the writer lease for a vault this daemon serves.

    The daemon holds it for the life of the process: idle eviction and handle
    closes never release it (only shutdown and managed delete do). Another
    process holding it surfaces as ``VaultPoolError("vault_busy")`` naming the
    holder's pid and operation.
    """
    try:
        return acquire_writer_lease(path, role="daemon", operation="serve")
    except WriterLeaseHeld as exc:
        raise VaultPoolError("vault_busy", exc.message) from exc


def _is_live(vault: Vault) -> bool:
    """True when ``vault`` is still usable at both wrapper and store layers."""
    if getattr(vault, "_closed", False):
        return False
    store = getattr(vault, "store", None)
    if store is not None and getattr(store, "is_closed", False):
        return False
    return True


_VaultT = TypeVar("_VaultT", bound=Vault)


class VaultLease(Generic[_VaultT]):
    """One idempotently releasable borrow of a pool-owned vault handle."""

    def __init__(self, pool: "VaultPool", path: Path, vault: _VaultT) -> None:
        self._pool = pool
        self.path = path
        self.vault = vault
        self._released = False

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._pool._release_lease(self.path)

    def __enter__(self) -> _VaultT:
        return self.vault

    def __exit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
        del exc_type, exc, traceback
        self.release()


class VaultPool:
    """Own live vault handles, scoped borrows, idle eviction, and delete fences.

    Opening remains serialized under one thread lock.  Vault opens are rare and
    can be slow; serializing them prevents duplicate handles for one path.  The
    lock-free :meth:`paths` snapshot keeps health/scheduler reads responsive.
    """

    max_open = 8

    def __init__(self) -> None:
        self._vaults: dict[Path, Vault] = {}
        self._handle_leases: dict[Path, VaultHandleLease] = {}
        self._leases: dict[Path, int] = {}
        self._last_used: dict[Path, int] = {}
        self._legacy_pins: set[Path] = set()
        self._fenced: set[Path] = set()
        self._clock = 0
        self._lock = threading.Lock()
        # Lease-count increments/decrements only. Releasing a borrow must never
        # wait behind ``_lock``, which is held across a (possibly slow) open of an
        # unrelated vault; the event loop releases request leases (issue #13).
        # Every other ``_leases`` mutation happens under ``_lock`` while that
        # path's count is zero, so it cannot race a release.
        self._count_lock = threading.Lock()
        self._paths_snapshot: tuple[Path, ...] = ()

    @staticmethod
    def _key(path: Path) -> Path:
        return Path(path).expanduser().resolve(strict=False)

    def _touch_locked(self, key: Path) -> None:
        self._clock += 1
        self._last_used[key] = self._clock

    def _refresh_snapshot_locked(self) -> None:
        self._paths_snapshot = tuple(self._vaults)

    @staticmethod
    def _close(vault: Vault) -> None:
        try:
            vault.close()
        except Exception:  # noqa: BLE001 - shutdown/eviction is best-effort
            pass

    def _acquire_handle_lease_locked(self, key: Path) -> VaultHandleLease:
        # Lock order: writer lease, then the graph-handle lease.
        acquire_daemon_writer_lease(key)
        existing = self._handle_leases.get(key)
        if existing is not None and existing.held:
            return existing
        lease = acquire_vault_handle_lease(key, operation="open vault graph")
        self._handle_leases[key] = lease
        return lease

    def _evict_one_idle_locked(self) -> None:
        candidates = [
            path
            for path in self._vaults
            if self._leases.get(path, 0) == 0
            and path not in self._legacy_pins
            and path not in self._fenced
        ]
        if not candidates:
            raise VaultPoolError(
                "pool_full",
                f"vault pool is at capacity ({self.max_open} open vaults); "
                "every handle is leased, fenced, or held by a compatibility caller",
            )
        victim = min(candidates, key=lambda path: self._last_used.get(path, 0))
        vault = self._vaults.pop(victim)
        handle_lease = self._handle_leases.pop(victim, None)
        self._leases.pop(victim, None)
        self._last_used.pop(victim, None)
        self._refresh_snapshot_locked()
        # Closing while holding the ownership lock prevents a new open for the
        # same path until the path-wide close has completed.
        self._close(vault)
        if handle_lease is not None:
            handle_lease.release()

    def _get_or_open_locked(self, key: Path, *, evict_idle: bool) -> Vault:
        existing = self._vaults.get(key)
        if existing is not None and _is_live(existing):
            self._touch_locked(key)
            return existing
        if existing is not None:
            # A maintenance path may have closed the handle out from under the
            # pool. Never silently discard an active lease count: the worker
            # that owns that lease must install the replacement explicitly via
            # ``adopt`` before any other caller can borrow this path again.
            active = self._leases.get(key, 0)
            if active:
                raise VaultPoolError(
                    "vault_in_use",
                    f"vault handle is closed while {active} lease(s) still own it: {key}",
                )
            # With no owner, reopening the same key safely reuses its slot.
            self._vaults.pop(key, None)
            stale_lease = self._handle_leases.pop(key, None)
            if stale_lease is not None:
                stale_lease.release()
            self._leases.pop(key, None)
            self._last_used.pop(key, None)
            self._legacy_pins.discard(key)
        elif len(self._vaults) >= self.max_open:
            if evict_idle:
                self._evict_one_idle_locked()
            else:
                raise VaultPoolError(
                    "pool_full",
                    f"vault pool is at capacity ({self.max_open} open vaults); "
                    "use a scoped lease so an idle handle can be evicted",
                )
        from okto_neuron.consolidate.review_queue import layout_refusal

        refusal = layout_refusal(key)
        if refusal is not None:
            # Refuse THIS vault only: no handle lease, no graph open, no write.
            raise VaultPoolError(
                "review_queue_migration_required",
                f"{refusal['detail']}; remedy: {refusal['remedy']}",
            )
        handle_lease = self._acquire_handle_lease_locked(key)
        try:
            vault = Vault.open(key)
        except OktoNeuronError as exc:
            self._handle_leases.pop(key, None)
            handle_lease.release()
            raise VaultPoolError("open_failed", f"could not open vault at {key}: {exc}") from exc
        except BaseException:
            self._handle_leases.pop(key, None)
            handle_lease.release()
            raise
        self._vaults[key] = vault
        self._leases.setdefault(key, 0)
        self._touch_locked(key)
        self._refresh_snapshot_locked()
        return vault

    def lease(self, path: Path) -> VaultLease[Vault]:
        """Borrow ``path`` until the returned lease is released.

        A deletion fence rejects new work.  At capacity, the least-recently-used
        unleased, unfenced, non-compatibility handle is closed before opening the
        requested path.
        """
        key = self._key(path)
        with self._lock:
            if key in self._fenced:
                raise VaultPoolError(
                    "vault_fenced", f"vault is fenced for deletion or release: {key}"
                )
            vault = self._get_or_open_locked(key, evict_idle=True)
            with self._count_lock:
                self._leases[key] = self._leases.get(key, 0) + 1
            self._touch_locked(key)
            return VaultLease(self, key, vault)

    def _release_lease(self, path: Path) -> None:
        key = self._key(path)
        with self._count_lock:
            count = self._leases.get(key, 0)
            if count <= 0:
                return
            self._leases[key] = count - 1
            self._touch_locked(key)

    def get_or_open(self, path: Path) -> Vault:
        """Compatibility raw-handle access.

        The path is pinned because the pool cannot observe when this caller has
        finished. New application code must use :meth:`lease` instead.
        """
        key = self._key(path)
        with self._lock:
            if key in self._fenced:
                raise VaultPoolError(
                    "vault_fenced", f"vault is fenced for deletion or release: {key}"
                )
            vault = self._get_or_open_locked(key, evict_idle=False)
            self._legacy_pins.add(key)
            return vault

    def adopt(self, vault: Vault, path: Path, *, pin: bool = True) -> Vault:
        """Register an already-open handle and return the pool-owned instance.

        ``pin=True`` preserves the historical raw-handle contract. Runtime
        bootstrap code passes ``pin=False`` and immediately uses scoped leases.
        A live duplicate is dropped without closing it because close is path-wide.
        """
        key = self._key(path)
        with self._lock:
            if key in self._fenced:
                raise VaultPoolError(
                    "vault_fenced", f"vault is fenced for deletion or release: {key}"
                )
            existing = self._vaults.get(key)
            if existing is not None and existing is not vault and _is_live(existing):
                if pin:
                    self._legacy_pins.add(key)
                self._touch_locked(key)
                return existing
            if existing is None and len(self._vaults) >= self.max_open:
                self._evict_one_idle_locked()
            self._acquire_handle_lease_locked(key)
            self._vaults[key] = vault
            self._leases.setdefault(key, 0)
            if pin:
                self._legacy_pins.add(key)
            self._touch_locked(key)
            self._refresh_snapshot_locked()
            return vault

    def claim_fenced_ownership(self, path: Path) -> VaultHandleLease:
        """Claim the OS boundary after the fenced pool handle was released."""
        key = self._key(path)
        with self._lock:
            if key not in self._fenced:
                raise VaultPoolError(
                    "vault_fenced", f"vault must be fenced before ownership claim: {key}"
                )
            if key in self._vaults or self._leases.get(key, 0):
                raise VaultPoolError(
                    "vault_in_use", f"vault still has an owned handle or lease: {key}"
                )
            return acquire_vault_handle_lease(key, operation="replace vault graph")

    def install_fenced(
        self,
        vault: Vault,
        path: Path,
        *,
        ownership: VaultHandleLease,
    ) -> Vault:
        """Install a maintenance replacement while ``path`` remains fenced.

        Destructive maintenance must close the pool-owned live handle before a
        Ladybug graph swap, then publish the replacement *before* admitting new
        leases.  ``adopt`` intentionally rejects fenced paths, so this narrower
        primitive is the only safe way for the fence owner to complete that
        hand-off.  It refuses active leases, compatibility pins, and duplicate
        handles; callers must first use ``release_path(..., require_fenced=True)``.
        """
        key = self._key(path)
        with self._lock:
            if key not in self._fenced:
                raise VaultPoolError("vault_fenced", f"vault must be fenced before install: {key}")
            count = self._leases.get(key, 0)
            if count:
                raise VaultPoolError("vault_in_use", f"vault has {count} active lease(s): {key}")
            if key in self._legacy_pins:
                raise VaultPoolError(
                    "legacy_pinned",
                    f"vault has an unscoped compatibility owner: {key}",
                )
            if key in self._vaults:
                raise VaultPoolError(
                    "vault_in_use",
                    f"vault still has a pool-owned handle and cannot be replaced: {key}",
                )
            if len(self._vaults) >= self.max_open:
                self._evict_one_idle_locked()
            if ownership.vault_path != key or not ownership.held:
                raise VaultPoolError("vault_in_use", f"replacement ownership is invalid for: {key}")
            self._vaults[key] = vault
            self._handle_leases[key] = ownership
            self._leases[key] = 0
            self._touch_locked(key)
            self._refresh_snapshot_locked()
            return vault

    # ``peek``, ``is_fenced`` and ``lease_count`` are point-in-time reads that
    # deliberately skip ``_lock`` (issue #13). The lock is held across
    # ``Vault.open``/eviction close, which can take seconds; the event loop calls
    # these on every request (``VaultRuntime.vault`` peeks), and a single dict/set
    # lookup is atomic under the GIL. The answer can be stale the instant it is
    # returned either way, so the lock never made it stronger for the caller.

    def peek(self, path: Path) -> Vault | None:
        """Return the currently owned live handle without opening or leasing it."""
        key = self._key(path)
        vault = self._vaults.get(key)
        return vault if vault is not None and _is_live(vault) else None

    def fence(self, path: Path) -> int:
        """Reject future leases for ``path`` and return its current lease count."""
        key = self._key(path)
        with self._lock:
            self._fenced.add(key)
            return self._leases.get(key, 0)

    def unfence(self, path: Path) -> None:
        """Undo a fence after an aborted deletion/release attempt."""
        key = self._key(path)
        with self._lock:
            self._fenced.discard(key)

    def is_fenced(self, path: Path) -> bool:
        key = self._key(path)
        return key in self._fenced

    def lease_count(self, path: Path) -> int:
        key = self._key(path)
        return self._leases.get(key, 0)

    def release_path(
        self,
        path: Path,
        *,
        require_fenced: bool = False,
        force_legacy_pin: bool = False,
    ) -> bool:
        """Close and forget one idle path; return whether a handle was present.

        Managed deletion should first call :meth:`fence` and then pass
        ``require_fenced=True``.  This makes the check-and-remove atomic with
        respect to new leases. Compatibility raw handles cannot be released
        unless their owner explicitly opts into ``force_legacy_pin``.
        """
        key = self._key(path)
        with self._lock:
            if require_fenced and key not in self._fenced:
                raise VaultPoolError("vault_fenced", f"vault must be fenced before release: {key}")
            count = self._leases.get(key, 0)
            if count:
                raise VaultPoolError("vault_in_use", f"vault has {count} active lease(s): {key}")
            if key in self._legacy_pins and not force_legacy_pin:
                raise VaultPoolError(
                    "legacy_pinned",
                    f"vault has an unscoped compatibility owner and cannot be released: {key}",
                )
            vault = self._vaults.pop(key, None)
            handle_lease = self._handle_leases.pop(key, None)
            self._leases.pop(key, None)
            self._last_used.pop(key, None)
            self._legacy_pins.discard(key)
            self._refresh_snapshot_locked()
        if vault is None:
            if handle_lease is not None:
                handle_lease.release()
            return False
        self._close(vault)
        if handle_lease is not None:
            handle_lease.release()
        return True

    def calls_in_flight(self) -> dict[str, int]:
        """Grafx calls executing per open vault (``{}`` for other backends).

        Reads without taking the pool lock (it can be held across a slow open);
        a dict snapshot is atomic under the GIL. Shutdown uses this to tell a
        thread parked in a non-store wait (count 0) from one inside a grafx
        statement or transaction (count > 0).
        """
        counts: dict[str, int] = {}
        for path, vault in tuple(self._vaults.items()):
            value = getattr(getattr(vault, "store", None), "calls_in_flight", 0)
            counts[path.name] = counts.get(path.name, 0) + int(value)
        return counts

    def close_all(self) -> None:
        """Close every pooled handle once. Idempotent; used after task drain."""
        with self._lock:
            vaults = list(self._vaults.items())
            handle_leases = list(self._handle_leases.values())
            self._vaults.clear()
            self._handle_leases.clear()
            self._leases.clear()
            self._last_used.clear()
            self._legacy_pins.clear()
            self._fenced.clear()
            self._refresh_snapshot_locked()
        for path, vault in vaults:
            with shutdown_phase("vault_close", vault=path.name):
                self._close(vault)
        with shutdown_phase("handle_lease_release", leases=len(handle_leases)):
            for handle_lease in handle_leases:
                handle_lease.release()

    def paths(self) -> list[Path]:
        """Return an eventual-consistency path snapshot without taking the lock."""
        return list(self._paths_snapshot)


__all__ = ["VaultLease", "VaultPool", "VaultPoolError", "acquire_daemon_writer_lease"]
