"""Maintained per-vault projection behind the expensive read endpoints (issue #12).

``GET /api/v1/upkeep/predicates`` (vocabulary size) and ``GET /api/v1/graph/stats`` used to run
a full graph scan per cache miss, with no in-flight de-dupe: a scan slower than the UI poll
interval started another one per poll until every executor thread was scanning. They now read
ONE :class:`VaultProjection` per vault: the predicate stats (:class:`~okto_neuron.predicates.
PredicateStats`, everything candidate generation needs) and the graph census (node and edge
counts by type, with and without the structural Claims), built together from set-based scans
that never read vectors.

Currency. A projection is built at a key ``(store.generation(), instance_token, write_seq)``.
``instance_token`` is a uuid minted per store object and ``write_seq`` counts completed
mutations (``store/index/indexed.py``), so the projection is current exactly while they match.
Writes from another process cannot move the counter, hence the max-age backstop.

Reads NEVER wait for a rebuild. :meth:`ProjectionManager.read` returns the last projection
with ``stale``/``rebuilding`` flags and schedules a rebuild when one is due. A vault with no
projection at all answers HTTP 202 ``{"status": "building"}`` and starts the first one.

Rebuilds are single-flight per vault: at most one runs (on the job executor, never on the event
loop) plus one pending. When a rebuild finishes and the key has moved, it runs once more ("latest
wins"), spaced by ``min_interval_s``; an explicit rebuild skips the spacing. Builds are also
serialised by a lock, so a predicate-propose job that needs stats joins a rebuild already in
flight instead of scanning again.

The projection is persisted to ``.marginalia/vault-projection.json`` (versioned). A sidecar from
an earlier process is served as stale until the first rebuild completes; a missing, corrupt or
old-version sidecar just means the first read answers 202 and builds.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from okto_neuron._internal.infra import is_infra
from okto_neuron.predicates import PredicateStats, build_predicate_stats
from okto_neuron.server._store_io import encode_json, job_io, store_io
from okto_neuron.store.closed_set import (
    CLOSED_NODE_TYPES,
    PRIMITIVE_NODE_TYPES,
    SUPPORT_NODE_TYPES,
)

_LOG = logging.getLogger("okto_neuron.server.projection")

SIDECAR_RELATIVE: Final = Path(".marginalia") / "vault-projection.json"
SIDECAR_VERSION: Final = 1
DEFAULT_MIN_INTERVAL_S: Final = 5.0
DEFAULT_MAX_AGE_S: Final = 600.0
_PERSIST_MIN_INTERVAL_S: Final = 60.0

# Deterministic document-structure Claims (a heading, tag or wikilink restated as a Claim):
# hidden from the browse counts unless ``?include_structural=1`` asks for them back. Keyed on
# the Claim predicate, NOT the ``_salience`` facet (that also marks genuine auto-promoted
# entities).
STRUCTURAL_CLAIM_PREDICATES: Final = frozenset({"has_heading", "has_tag", "links_to"})


def is_structural_claim(node: Any) -> bool:
    if str(getattr(node, "type", "")) != "Claim":
        return False
    facets = getattr(node, "facets", None) or {}
    return facets.get("P") in STRUCTURAL_CLAIM_PREDICATES


def compute_graph_stats(store: Any) -> dict[bool, dict[str, Any]]:
    """Node-type and edge-type census for both ``include_structural`` variants, in one pass
    over the nodes and one over the edges (no vectors are read)."""
    node_counts = {flag: {t: 0 for t in CLOSED_NODE_TYPES} for flag in (False, True)}
    total_nodes = {False: 0, True: 0}
    for node in store.list_nodes():
        if is_infra(node):
            continue
        node_type = str(getattr(node, "type", ""))
        if node_type not in node_counts[True]:
            continue
        node_counts[True][node_type] += 1
        total_nodes[True] += 1
        if not is_structural_claim(node):
            node_counts[False][node_type] += 1
            total_nodes[False] += 1
    edge_counts: dict[str, int] = {}
    total_edges = 0
    for edge in store.list_edges():
        edge_type = str(getattr(edge, "type", ""))
        edge_counts[edge_type] = edge_counts.get(edge_type, 0) + 1
        total_edges += 1
    edge_types = [{"type": t, "count": edge_counts[t]} for t in sorted(edge_counts)]
    return {
        flag: {
            "status": "ok",
            "node_types": [
                {"type": t, "count": node_counts[flag][t]}
                for t in (*PRIMITIVE_NODE_TYPES, *SUPPORT_NODE_TYPES)
                if node_counts[flag][t] > 0
            ],
            "edge_types": edge_types,
            "total_nodes": total_nodes[flag],
            "total_edges": total_edges,
        }
        for flag in (False, True)
    }


@dataclass(frozen=True)
class ProjectionKey:
    generation: str
    instance_token: str | None
    write_seq: int


@dataclass(frozen=True)
class VaultProjection:
    key: ProjectionKey | None
    """None for a projection restored from a sidecar: the counters are per process."""
    built_at: float
    """``time.time()`` when the build finished."""
    duration_s: float
    stats: PredicateStats
    graph_stats: dict[bool, dict[str, Any]]
    graph_stats_json: dict[bool, bytes]
    """``graph_stats`` pre-encoded on the worker that built it, so serving the endpoint never
    serialises a big body on the event loop."""


def _encode_graph_stats(graph_stats: dict[bool, dict[str, Any]]) -> dict[bool, bytes]:
    return {flag: encode_json(graph_stats[flag]) for flag in (False, True)}


@dataclass(frozen=True)
class ProjectionRead:
    projection: VaultProjection | None
    stale: bool
    rebuilding: bool


def _identity(store: Any) -> tuple[str, int] | None:
    """``(instance_token, write_seq)`` of a live store, or None when it has no counter."""
    token = getattr(store, "instance_token", None)
    if token is None:
        return None
    return str(token), int(getattr(store, "write_seq", 0))


def _store_key(store: Any) -> ProjectionKey:
    """The full key. Calls ``store.generation()``: worker threads only."""
    token = getattr(store, "instance_token", None)
    return ProjectionKey(
        generation=str(store.generation()),
        instance_token=None if token is None else str(token),
        write_seq=int(getattr(store, "write_seq", 0)),
    )


def _peek_store(state: Any) -> Any | None:
    """The vault's store if its handle is already open (an in-memory lookup, never opens)."""
    pool = getattr(state, "vault_pool", None)
    peek = getattr(pool, "peek", None)
    if not callable(peek):
        return None
    vault = peek(state.vault_path)
    return getattr(vault, "store", None)


class ProjectionManager:
    """Owns one vault's projection, its rebuild task and its sidecar."""

    def __init__(
        self,
        vault_path: Path | str,
        *,
        min_interval_s: float = DEFAULT_MIN_INTERVAL_S,
        max_age_s: float = DEFAULT_MAX_AGE_S,
    ) -> None:
        self.vault_path = Path(vault_path).expanduser().resolve(strict=False)
        self.min_interval_s = float(min_interval_s)
        self.max_age_s = float(max_age_s)
        self._projection: VaultProjection | None = None
        self._lock = threading.Lock()
        self._build_lock = threading.Lock()
        self._sidecar_tried = False
        self._task: asyncio.Task[None] | None = None
        self._force = False
        self._last_done: float | None = None
        self._last_persist: float | None = None
        #: Builds that actually scanned the graph (tests assert single-flight on this).
        self.builds = 0
        self.last_error: str | None = None

    # ------------------------------------------------------------------ status

    @property
    def projection(self) -> VaultProjection | None:
        return self._projection

    @property
    def rebuilding(self) -> bool:
        task = self._task
        return task is not None and not task.done()

    def sidecar_path(self) -> Path:
        return self.vault_path / SIDECAR_RELATIVE

    def is_stale(self, store: Any | None, projection: VaultProjection | None = None) -> bool:
        """True when ``projection`` (default: the current one) is missing, older than the
        max age, restored from a sidecar, or built at a different store identity. Cheap and
        loop-safe: no store call, only the in-memory counters."""
        projection = projection if projection is not None else self._projection
        if projection is None or projection.key is None:
            return True
        if time.time() - projection.built_at > self.max_age_s:
            return True
        identity = _identity(store) if store is not None else None
        if identity is None:
            return False
        return identity != (projection.key.instance_token, projection.key.write_seq)

    # -------------------------------------------------------------------- reads

    async def read(self, state: Any) -> ProjectionRead:
        """The last projection plus flags; schedules a rebuild when one is due. Never waits
        for a rebuild (the only await is the one-off sidecar load on the store executor)."""
        if not self._sidecar_tried:
            await store_io(self.load_sidecar)
        projection = self._projection
        stale = self.is_stale(_peek_store(state), projection)
        if stale:
            self.ensure(state)
        return ProjectionRead(projection, stale, self.rebuilding)

    def ensure(self, state: Any, *, force: bool = False) -> bool:
        """Start the single rebuild task unless one is running (loop thread only). A running
        task that is told to ``force`` skips its spacing and rescans. Returns True when a task
        was started by this call."""
        loop = asyncio.get_running_loop()
        task = self._task
        if task is not None and not task.done() and task.get_loop() is loop:
            if force:
                self._force = True
            return False
        self._force = force
        self._task = loop.create_task(self._run(state), name="okto-neuron-projection")
        _TASKS.add(self._task)
        self._task.add_done_callback(_TASKS.discard)
        return True

    # ------------------------------------------------------------------- builds

    async def _run(self, state: Any) -> None:
        try:
            while True:
                forced, self._force = self._force, False
                if not forced and self._last_done is not None:
                    # Spaced from the END of the last build, so a scan slower than the
                    # interval under continuous writes cannot keep a worker at 100%.
                    wait = self._last_done + self.min_interval_s - time.monotonic()
                    if wait > 0:
                        await asyncio.sleep(wait)
                        forced, self._force = forced or self._force, False
                projection = await job_io(self._build_for_state, state, forced)
                self._last_done = time.monotonic()
                await store_io(self._persist, projection, forced)
                if not self.is_stale(_peek_store(state), projection):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the last projection, retry on next read
            self.last_error = f"{type(exc).__name__}: {exc}"
            _LOG.exception("projection rebuild failed for %s", self.vault_path)

    def _build_for_state(self, state: Any, force: bool) -> VaultProjection:
        return self.build(state.vault.store, force=force)

    def build(self, store: Any, *, force: bool = False) -> VaultProjection:
        """Scan and publish a projection (worker threads only). Serialised: a caller that
        arrives while a build is running waits for it and reuses the result when it is
        current, so concurrent demand never produces concurrent scans."""
        with self._build_lock:
            current = self._projection
            if (
                not force
                and current is not None
                and current.key is not None
                and current.key == _store_key(store)
                and time.time() - current.built_at <= self.max_age_s
            ):
                return current
            key = _store_key(store)  # read BEFORE the scans: a write during them moves it
            started = time.monotonic()
            stats = build_predicate_stats(store)
            graph_stats = compute_graph_stats(store)
            projection = VaultProjection(
                key=key,
                built_at=time.time(),
                duration_s=time.monotonic() - started,
                stats=stats,
                graph_stats=graph_stats,
                graph_stats_json=_encode_graph_stats(graph_stats),
            )
            with self._lock:
                self._projection = projection
                self.builds += 1
                self.last_error = None
            return projection

    def stats_for_job(self, store: Any) -> PredicateStats:
        """Predicate stats for a curation job (worker thread): the current projection, or one
        build joined/started now. Replaces the two full scans the propose run used to do."""
        return self.build(store).stats

    # ------------------------------------------------------------------ sidecar

    def load_sidecar(self) -> None:
        """Restore the last persisted projection, once, as a stale one. Any problem (missing,
        corrupt, old version) leaves the manager cold so the first read builds."""
        with self._lock:
            if self._sidecar_tried:
                return
            self._sidecar_tried = True
        path = self.sidecar_path()
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            _LOG.warning("cannot read %s (%s); rebuilding", path, exc)
            return
        try:
            payload = json.loads(raw)
            if payload.get("version") != SIDECAR_VERSION:
                _LOG.info("ignoring %s: version %r, expected %d", path, payload.get("version"), SIDECAR_VERSION)
                return
            graph_stats = {
                False: dict(payload["graph_stats"]["false"]),
                True: dict(payload["graph_stats"]["true"]),
            }
            projection = VaultProjection(
                key=None,
                built_at=float(payload["built_at"]),
                duration_s=float(payload["duration_s"]),
                stats=PredicateStats.from_payload(payload["stats"]),
                graph_stats=graph_stats,
                graph_stats_json=_encode_graph_stats(graph_stats),
            )
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            _LOG.warning("corrupt projection sidecar %s (%s); rebuilding", path, exc)
            return
        with self._lock:
            if self._projection is None:
                self._projection = projection

    def _persist(self, projection: VaultProjection, force: bool = False) -> None:
        now = time.monotonic()
        if (
            not force
            and self._last_persist is not None
            and now - self._last_persist < _PERSIST_MIN_INTERVAL_S
        ):
            return  # the sidecar is a restart convenience; do not rewrite MBs on every rebuild
        self._last_persist = now
        payload = {
            "version": SIDECAR_VERSION,
            "generation": projection.key.generation if projection.key else None,
            "built_at": projection.built_at,
            "duration_s": projection.duration_s,
            "stats": projection.stats.to_payload(),
            "graph_stats": {
                "false": projection.graph_stats[False],
                "true": projection.graph_stats[True],
            },
        }
        path = self.sidecar_path()
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            _LOG.warning("could not persist %s", path, exc_info=True)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


# ------------------------------------------------------------------ registry

_CONFIG: dict[str, float] = {
    "min_interval_s": DEFAULT_MIN_INTERVAL_S,
    "max_age_s": DEFAULT_MAX_AGE_S,
}
_MANAGERS: dict[str, ProjectionManager] = {}
_TASKS: set[asyncio.Task[None]] = set()


def configure(*, min_interval_s: float, max_age_s: float) -> None:
    """Apply ``[server] projection_min_interval_s`` / ``projection_max_age_s`` (serve start)."""
    _CONFIG["min_interval_s"] = float(min_interval_s)
    _CONFIG["max_age_s"] = float(max_age_s)
    for manager in _MANAGERS.values():
        manager.min_interval_s = _CONFIG["min_interval_s"]
        manager.max_age_s = _CONFIG["max_age_s"]


def manager_for(vault_path: Path | str) -> ProjectionManager:
    key = str(Path(vault_path).expanduser().resolve(strict=False))
    manager = _MANAGERS.get(key)
    if manager is None:
        manager = _MANAGERS.setdefault(
            key,
            ProjectionManager(
                key,
                min_interval_s=_CONFIG["min_interval_s"],
                max_age_s=_CONFIG["max_age_s"],
            ),
        )
    return manager


def projection_tasks() -> set[asyncio.Task[None]]:
    """Live rebuild tasks, for the shutdown orchestrator to cancel."""
    return {task for task in _TASKS if not task.done()}


def reset_for_tests() -> None:
    _MANAGERS.clear()
    _TASKS.clear()
    _CONFIG.update(min_interval_s=DEFAULT_MIN_INTERVAL_S, max_age_s=DEFAULT_MAX_AGE_S)
