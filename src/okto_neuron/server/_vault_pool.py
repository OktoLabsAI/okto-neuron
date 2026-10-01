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

import concurrent.futures
import logging
import threading
import time
from pathlib import Path
from typing import Any, Generic, NamedTuple, TypeVar

from okto_neuron.errors import OktoNeuronError
from okto_neuron.server._store_io import NONBLOCKING, WouldBlock
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

_LOG = logging.getLogger(__name__)


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


class _Evicted(NamedTuple):
    """A handle unlinked from the pool whose path-wide close is still to run."""

    key: Path
    vault: Vault
    handle_lease: VaultHandleLease | None
    done: "concurrent.futures.Future[None]"


class VaultPool:
    """Own live vault handles, scoped borrows, idle eviction, and delete fences.

    ``_lock`` only guards the bookkeeping dicts and is never held across I/O.
    A cold open (a full-graph index digest can take many seconds) and an
    eviction close run outside it: the opener registers a future in
    ``_opening``, concurrent callers for the same path join it instead of
    opening again, and leases of every other vault proceed meanwhile. An evicted
    or released handle sits in ``_closing`` until its path-wide close finishes,
    so a re-lease of that path waits for the close and then opens fresh; two
    handles are never open on one on-disk store. At most ``max_concurrent_opens``
    opens run at once. The lock-free :meth:`paths` snapshot keeps health and
    scheduler reads responsive.
    """

    max_open = 8
    max_concurrent_opens = 2

    def __init__(self) -> None:
        self._vaults: dict[Path, Vault] = {}
        self._handle_leases: dict[Path, VaultHandleLease] = {}
        self._leases: dict[Path, int] = {}
        self._last_used: dict[Path, int] = {}
        self._legacy_pins: set[Path] = set()
        self._fenced: set[Path] = set()
        self._opening: dict[Path, concurrent.futures.Future[Vault]] = {}
        self._closing: dict[Path, concurrent.futures.Future[None]] = {}
        self._open_slots = threading.BoundedSemaphore(self.max_concurrent_opens)
        self._clock = 0
        self._lock = threading.Lock()
        # Lease-count increments/decrements only. Releasing a borrow must never
        # wait behind ``_lock`` (issue #13); the event loop releases request leases.
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

    def _acquire_handle_lease(self, key: Path) -> VaultHandleLease:
        """:meth:`_acquire_handle_lease_locked` without holding ``_lock`` across the file locks."""
        acquire_daemon_writer_lease(key)
        with self._lock:
            existing = self._handle_leases.get(key)
        if existing is not None and existing.held:
            return existing
        lease = acquire_vault_handle_lease(key, operation="open vault graph")
        with self._lock:
            self._handle_leases[key] = lease
        return lease

    def _evict_one_idle_locked(self) -> _Evicted:
        """Unlink the least-recently-used idle handle; the caller closes it with
        :meth:`_finish_close` after releasing ``_lock``."""
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
        return self._unlink_locked(victim)

    def _unlink_locked(self, key: Path) -> _Evicted:
        """Drop ``key`` from the pool and mark it closing until the caller closes it."""
        vault = self._vaults.pop(key)
        handle_lease = self._handle_leases.pop(key, None)
        self._leases.pop(key, None)
        self._last_used.pop(key, None)
        self._legacy_pins.discard(key)
        self._refresh_snapshot_locked()
        done: concurrent.futures.Future[None] = concurrent.futures.Future()
        self._closing[key] = done
        return _Evicted(key, vault, handle_lease, done)

    def _finish_close(self, evicted: _Evicted) -> None:
        try:
            self._close(evicted.vault)
            if evicted.handle_lease is not None:
                evicted.handle_lease.release()
        finally:
            with self._lock:
                self._closing.pop(evicted.key, None)
            evicted.done.set_result(None)

    def _occupied_locked(self) -> int:
        return len(self._vaults) + len(self._opening)

    def _pending_locked(self, key: Path) -> concurrent.futures.Future[Any] | None:
        """The in-flight open or close of ``key``, if any."""
        return self._opening.get(key) or self._closing.get(key)

    def _claim_locked(self, key: Path, vault: Vault, *, leased: bool, pin: bool) -> Any:
        self._touch_locked(key)
        if pin:
            self._legacy_pins.add(key)
        if not leased:
            return vault
        with self._count_lock:
            self._leases[key] = self._leases.get(key, 0) + 1
        self._touch_locked(key)
        return VaultLease(self, key, vault)

    def _acquire(self, key: Path, *, evict_idle: bool, leased: bool, pin: bool, wait: bool) -> Any:
        """Return a lease (``leased``) or the raw handle for ``key``, opening it if needed.

        Another thread's in-flight open of ``key`` is joined, never repeated:
        blocking callers wait on its future, ``wait=False`` callers get
        :class:`WouldBlock` carrying it. Only the caller that registers the
        future opens, and it does so with ``_lock`` released.
        """
        while True:
            with self._lock:
                self._check_fence_locked(key)
                existing = self._vaults.get(key)
                if existing is not None and _is_live(existing):
                    return self._claim_locked(key, existing, leased=leased, pin=pin)
                pending = self._pending_locked(key)
                if pending is None:
                    evicted, opening = self._begin_open_locked(key, existing, evict_idle)
                    break
            if not wait:
                raise WouldBlock(pending)
            pending.result()  # an open that failed raises its error here
        return self._open_unlocked(key, opening, evicted, leased=leased, pin=pin)

    def _check_fence_locked(self, key: Path) -> None:
        if key in self._fenced:
            raise VaultPoolError("vault_fenced", f"vault is fenced for deletion or release: {key}")

    def _begin_open_locked(
        self, key: Path, existing: Vault | None, evict_idle: bool
    ) -> tuple[_Evicted | None, concurrent.futures.Future[Vault]]:
        evicted: _Evicted | None = None
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
            self._refresh_snapshot_locked()
        elif self._occupied_locked() >= self.max_open:
            if not evict_idle:
                raise VaultPoolError(
                    "pool_full",
                    f"vault pool is at capacity ({self.max_open} open vaults); "
                    "use a scoped lease so an idle handle can be evicted",
                )
            evicted = self._evict_one_idle_locked()
        opening: concurrent.futures.Future[Vault] = concurrent.futures.Future()
        self._opening[key] = opening
        return evicted, opening

    def _open_unlocked(
        self,
        key: Path,
        opening: concurrent.futures.Future[Vault],
        evicted: _Evicted | None,
        *,
        leased: bool,
        pin: bool,
    ) -> Any:
        """Do the slow part of an open (``_opening[key]`` is already registered)."""
        handle_lease: VaultHandleLease | None = None
        try:
            if evicted is not None:
                self._finish_close(evicted)
            from okto_neuron.consolidate.review_queue import layout_refusal

            refusal = layout_refusal(key)
            if refusal is not None:
                # Refuse THIS vault only: no handle lease, no graph open, no write.
                raise VaultPoolError(
                    "review_queue_migration_required",
                    f"{refusal['detail']}; remedy: {refusal['remedy']}",
                )
            with self._open_slots:
                started = time.perf_counter()
                handle_lease = self._acquire_handle_lease(key)
                try:
                    vault = Vault.open(key)
                except OktoNeuronError as exc:
                    raise VaultPoolError(
                        "open_failed", f"could not open vault at {key}: {exc}"
                    ) from exc
                elapsed = time.perf_counter() - started
            with self._lock:
                self._opening.pop(key, None)
                fenced = key in self._fenced
                if not fenced:
                    self._vaults[key] = vault
                    self._leases.setdefault(key, 0)
                    self._refresh_snapshot_locked()
                    claimed = self._claim_locked(key, vault, leased=leased, pin=pin)
            if fenced:
                # fence() arrived while this open was running: nothing was
                # published, so hand the new handle straight back.
                with self._lock:
                    self._handle_leases.pop(key, None)
                self._close(vault)
                handle_lease.release()
                raise VaultPoolError(
                    "vault_fenced", f"vault is fenced for deletion or release: {key}"
                )
        except BaseException as exc:
            owned = False
            with self._lock:
                self._opening.pop(key, None)
                owned = handle_lease is not None and self._handle_leases.get(key) is handle_lease
                if owned:
                    self._handle_leases.pop(key, None)
            if owned:
                handle_lease.release()
            opening.set_exception(exc)
            raise
        _LOG.info("vault open %s took %.2fs", key.name, elapsed)
        opening.set_result(vault)
        return claimed

    def lease(self, path: Path) -> VaultLease[Vault]:
        """Borrow ``path`` until the returned lease is released.

        A deletion fence rejects new work.  At capacity, the least-recently-used
        unleased, unfenced, non-compatibility handle is closed before opening the
        requested path.  Inside an :func:`acquire_off_loop` worker a path another
        thread is opening raises :class:`WouldBlock` instead of waiting.
        """
        return self._acquire(
            self._key(path),
            evict_idle=True,
            leased=True,
            pin=False,
            wait=not NONBLOCKING.get(),
        )

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
        return self._acquire(self._key(path), evict_idle=False, leased=False, pin=True, wait=True)

    def adopt(self, vault: Vault, path: Path, *, pin: bool = True) -> Vault:
        """Register an already-open handle and return the pool-owned instance.

        ``pin=True`` preserves the historical raw-handle contract. Runtime
        bootstrap code passes ``pin=False`` and immediately uses scoped leases.
        A live duplicate is dropped without closing it because close is path-wide.
        """
        key = self._key(path)
        evicted: _Evicted | None = None
        try:
            while True:
                with self._lock:
                    self._check_fence_locked(key)
                    pending = self._pending_locked(key)
                    if pending is None:
                        existing = self._vaults.get(key)
                        if existing is not None and existing is not vault and _is_live(existing):
                            if pin:
                                self._legacy_pins.add(key)
                            self._touch_locked(key)
                            return existing
                        if existing is None and self._occupied_locked() >= self.max_open:
                            evicted = self._evict_one_idle_locked()
                        self._acquire_handle_lease_locked(key)
                        self._vaults[key] = vault
                        self._leases.setdefault(key, 0)
                        if pin:
                            self._legacy_pins.add(key)
                        self._touch_locked(key)
                        self._refresh_snapshot_locked()
                        return vault
                # An open or close of this very path is in flight: wait it out so
                # the adopted handle is never published next to (or under) another.
                concurrent.futures.wait([pending])
        finally:
            if evicted is not None:
                self._finish_close(evicted)

    def claim_fenced_ownership(self, path: Path) -> VaultHandleLease:
        """Claim the OS boundary after the fenced pool handle was released."""
        key = self._key(path)
        with self._lock:
            if key not in self._fenced:
                raise VaultPoolError(
                    "vault_fenced", f"vault must be fenced before ownership claim: {key}"
                )
            if key in self._vaults or self._leases.get(key, 0) or self._pending_locked(key):
                raise VaultPoolError(
                    "vault_in_use", f"vault still has an owned handle, lease, or open: {key}"
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
            if key in self._vaults or self._pending_locked(key):
                raise VaultPoolError(
                    "vault_in_use",
                    f"vault still has a pool-owned handle or open and cannot be replaced: {key}",
                )
            if ownership.vault_path != key or not ownership.held:
                raise VaultPoolError("vault_in_use", f"replacement ownership is invalid for: {key}")
            evicted = (
                self._evict_one_idle_locked() if self._occupied_locked() >= self.max_open else None
            )
            self._vaults[key] = vault
            self._handle_leases[key] = ownership
            self._leases[key] = 0
            self._touch_locked(key)
            self._refresh_snapshot_locked()
        if evicted is not None:
            self._finish_close(evicted)
        return vault

    # ``peek``, ``is_fenced`` and ``lease_count`` are point-in-time reads that
    # deliberately skip ``_lock`` (issue #13). The event loop calls these on every
    # request (``VaultRuntime.vault`` peeks), and a single dict/set
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
        while True:
            with self._lock:
                if require_fenced and key not in self._fenced:
                    raise VaultPoolError(
                        "vault_fenced", f"vault must be fenced before release: {key}"
                    )
                pending = self._pending_locked(key)
                if pending is None:
                    count = self._leases.get(key, 0)
                    if count:
                        raise VaultPoolError(
                            "vault_in_use", f"vault has {count} active lease(s): {key}"
                        )
                    if key in self._legacy_pins and not force_legacy_pin:
                        raise VaultPoolError(
                            "legacy_pinned",
                            f"vault has an unscoped compatibility owner and cannot be released: {key}",
                        )
                    if key not in self._vaults:
                        handle_lease = self._handle_leases.pop(key, None)
                        self._leases.pop(key, None)
                        self._last_used.pop(key, None)
                        self._legacy_pins.discard(key)
                        evicted = None
                    else:
                        evicted = self._unlink_locked(key)
                    break
            # An open (it sees the fence once it finishes and hands its handle
            # back) or a close of this path is in flight: let it settle first.
            concurrent.futures.wait([pending])
        if evicted is None:
            if handle_lease is not None:
                handle_lease.release()
            return False
        self._finish_close(evicted)
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
        while True:
            with self._lock:
                in_flight = [*self._opening.values(), *self._closing.values()]
                if not in_flight:
                    vaults = list(self._vaults.items())
                    handle_leases = list(self._handle_leases.values())
                    self._vaults.clear()
                    self._handle_leases.clear()
                    self._leases.clear()
                    self._last_used.clear()
                    self._legacy_pins.clear()
                    self._fenced.clear()
                    self._refresh_snapshot_locked()
                    break
            # An open publishing after the snapshot would leak its handle.
            concurrent.futures.wait(in_flight)
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
