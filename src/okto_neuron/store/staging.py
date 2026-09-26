"""Backend-agnostic staged-graph lifecycle: stage a build, then commit or discard it.

Implements the M2b slice of the pluggable-graph-backend plan
(the internal ADR 0041 plan, decision D-36: the shared
audit-commit-fence-publish *tail* moves into a common orchestrator while each
verb's build *head* stays put). This module owns one piece of that tail: the
physical staged-graph lifecycle (stage a candidate database, commit it onto
live atomically, or discard/restore it) behind a small :class:`StagingPort`
protocol so ``curation/orchestrate.py`` depends on a named port instead of
reaching into ``cli.kg``'s private namespace, as ``reconcile/heal.py`` and
``server/_curation.py`` do today.

``_active_graph_sidecars``, ``_move_graph_family``, ``_discard_graph_family``,
``_copy_closed_graph_checkpoint``, ``_swap_rebuilt_graph`` and their private
helper ``_fsync_parent_dir`` are a **relocation, not a rewrite**, of the
identically-named functions in ``cli/kg.py`` (today at lines 2155-2261 and
2274-2279) — the one physical swap primitive every rebuild/heal/reembed/
rollback owner already calls, duplicated only in the orchestration sequence
around it, never in the primitive itself. ``commit``'s failure semantics are
byte-for-byte unchanged: on any exception after the live graph has been moved
to its backup path, attempt to move the backup back onto live before
re-raising :class:`~okto_neuron.errors.RebuildSwapFailed`.

``cli/kg.py`` keeps thin re-exports of these names until every call site is
re-routed through this module (M2b §3); it is not edited here.
"""

from __future__ import annotations

import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Any, Protocol

from okto_neuron.errors import OktoNeuronError, RebuildSwapFailed
from okto_neuron.store._bootstrap import _GRAPH_FILE

__all__ = ["StagingPort", "LadybugStaging", "GrafxStaging", "Neo4jStaging", "staging_for"]

_GRAFX_GRAPH_DIR = "graph.grafx"
_NEO4J_MARKER_DIR = ".neo4j-generation"

_logger = logging.getLogger(__name__)


class StagingPort(Protocol):
    """Stage a candidate graph database, then commit, discard, or restore it.

    A backend implements this once and every verb (rebuild/heal/reembed/
    rollback) drives it through the same four calls instead of hand-rolling
    the stage->populate->commit sequence against backend-specific paths.
    """

    def stage_path(self, tag: str) -> Path:
        """Return the on-disk path for a staged graph tagged ``tag``.

        E.g. ``tag="rebuild"`` -> ``graph.rebuild.lbug`` next to the live graph.
        """
        ...

    def discard(self, staged: Path) -> None:
        """Remove a staged graph database and all of its sidecars.

        Used when a pre-swap audit fails: live is untouched, staging goes away.
        """
        ...

    def commit(self, staged: Path, backup_tag: str) -> Path:
        """Atomically swap ``staged`` onto the live graph.

        The current live graph is first moved to a backup path (named with
        ``backup_tag``), then ``staged`` is moved onto the live path. Returns
        the backup path. On any failure after the live graph has been moved
        to backup, attempts to move it back onto live before re-raising.
        """
        ...

    def restore(self, backup: Path) -> None:
        """Move a previously-created backup graph back onto the live path."""
        ...


class LadybugStaging:
    """Ladybug-backed :class:`StagingPort`.

    Every staged/backup path is a sibling of the live graph file
    (``<vault_path>/graph.lbug``), named by inserting a ``.<tag>`` segment
    before the ``.lbug`` suffix — the exact convention every existing owner
    (``kg rebuild``/``kg reembed``/``reconcile heal``/the daemon's curation
    runners) already uses.
    """

    def __init__(self, vault_path: Path) -> None:
        self._vault_path = vault_path
        self._graph_path = vault_path / _GRAPH_FILE

    def stage_path(self, tag: str) -> Path:
        return self._graph_path.with_name(f"{self._graph_path.stem}.{tag}{self._graph_path.suffix}")

    def discard(self, staged: Path) -> None:
        _discard_graph_family(staged)

    def commit(self, staged: Path, backup_tag: str) -> Path:
        backup_path = self._graph_path.with_name(f"{self._graph_path.name}.{backup_tag}")
        _swap_rebuilt_graph(self._vault_path, self._graph_path, staged, backup_path)
        return backup_path

    def restore(self, backup: Path) -> None:
        _move_graph_family(backup, self._graph_path)


class GrafxStaging:
    """Okto Grafx-backed :class:`StagingPort`.

    Grafx's live graph is a directory (``<vault_path>/graph.grafx``), not
    Ladybug's single file, so a directory ``os.replace`` cannot reuse
    ``_swap_rebuilt_graph``'s file-to-file rename, which always succeeds over
    an existing destination: a directory ``os.replace`` raises ``ENOTEMPTY``
    over a non-empty destination (verified live). ``commit``/``restore``
    instead run the atomic rename chain below (shepherd override O2): every
    path a rename is about to land on is first vacated onto a sibling
    ``.discard`` directory via ``os.replace`` and only reclaimed with
    ``shutil.rmtree`` once the chain has landed on its next path — never
    ``rmtree``-before-``replace``. At every instant either the live path or
    the backup path holds a valid graph directory; the only no-live window is
    between the two middle renames of ``commit``, the same window Ladybug's
    file swap has. A ``*.discard`` directory left behind by an interrupted
    prior swap is cleared by the next ``stage_path``/``commit`` call.
    """

    def __init__(self, vault_path: Path) -> None:
        self._vault_path = vault_path
        self._graph_path = vault_path / _GRAFX_GRAPH_DIR

    def stage_path(self, tag: str) -> Path:
        staged = self._graph_path.with_name(f"{self._graph_path.stem}.{tag}{self._graph_path.suffix}")
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        self._clear_stale_discards()
        return staged

    def discard(self, staged: Path) -> None:
        shutil.rmtree(staged, ignore_errors=True)

    def commit(self, staged: Path, backup_tag: str) -> Path:
        backup_path = self._backup_path(backup_tag)
        discard_path = self._discard_path(backup_path)
        if discard_path.exists():
            shutil.rmtree(discard_path, ignore_errors=True)
        if backup_path.exists():
            os.replace(backup_path, discard_path)

        live_moved_to_backup = False
        try:
            os.replace(self._graph_path, backup_path)
            live_moved_to_backup = True
            os.replace(staged, self._graph_path)
        except Exception as swap_exc:
            cause = swap_exc
            if live_moved_to_backup and backup_path.exists() and not self._graph_path.exists():
                try:
                    os.replace(backup_path, self._graph_path)
                except Exception as restore_exc:  # noqa: BLE001 - retain the rollback failure
                    cause = restore_exc
            raise RebuildSwapFailed(
                self._vault_path,
                source_path=staged,
                target_path=self._graph_path,
                cause=cause,
            ) from cause
        else:
            _fsync_parent_dir(self._graph_path)
            shutil.rmtree(discard_path, ignore_errors=True)
        return backup_path

    def restore(self, backup: Path) -> None:
        discard_path = self._graph_path.with_name(f"{self._graph_path.name}.discard")
        if discard_path.exists():
            shutil.rmtree(discard_path, ignore_errors=True)
        if self._graph_path.exists():
            os.replace(self._graph_path, discard_path)
        os.replace(backup, self._graph_path)
        _fsync_parent_dir(self._graph_path)
        shutil.rmtree(discard_path, ignore_errors=True)

    def _backup_path(self, backup_tag: str) -> Path:
        return self._graph_path.with_name(f"{self._graph_path.name}.{backup_tag}")

    def _discard_path(self, backup_path: Path) -> Path:
        return backup_path.with_name(f"{backup_path.name}.discard")

    def _clear_stale_discards(self) -> None:
        """Sweep any ``*.discard`` sibling an interrupted prior swap left behind."""
        if not self._vault_path.exists():
            return
        for candidate in self._vault_path.glob(f"{self._graph_path.name}.*.discard"):
            shutil.rmtree(candidate, ignore_errors=True)


class Neo4jStaging:
    """Neo4j-backed :class:`StagingPort` -- "paths" become generation tags.

    Neo4j has no filesystem graph at all (M5 spec §2): ``stage_path``
    returns a synthetic marker path (``<vault_path>/.neo4j-generation/<tag>``,
    never written to disk) that :class:`~okto_neuron.store.neo4j.Neo4jStore`
    recognizes and unpacks into a build-mode open. ``commit``/``restore``
    are an atomic pointer flip on the per-vault metadata singleton's
    ``graph_generation``/``backup_tag`` properties, not a byte copy —
    stronger than Ladybug's/Grafx's rename-chain commit (no no-live window).

    Requires a live :class:`~okto_neuron.store.protocol.GraphStore` handle
    (the optional ``store`` constructor arg) since there is no path-only way
    to reach the database — ``staging_for``'s signature widens accordingly.
    """

    def __init__(self, vault_path: Path, store: Any = None) -> None:
        self._vault_path = vault_path
        if store is None:
            raise OktoNeuronError(
                "Neo4jStaging requires a live Neo4jStore handle (pass store=...)",
                vault_path=vault_path,
            )
        self._store = store

    def stage_path(self, tag: str) -> Path:
        """Return a fresh, unique marker path for a staged build tagged ``tag``.

        The returned path always encodes a per-call-unique generation
        (``<tag>-<hex>``), never the bare caller literal. Two verbs of the
        same name (e.g. two ``"heal"`` runs back to back, or a normal run
        racing a retry of the same verb) MUST NOT collide on one Neo4j
        ``_generation`` tag: the pre-build cleanup every caller does
        (``staging.discard(tmp_graph_path)`` right after computing this
        path, before populating it -- see ``cli/kg.py``/``server/_curation.py``)
        deletes every node stamped with that tag. Ladybug/Grafx are immune
        to this because their staged and live databases are physically
        separate files; Neo4j's staged and live rows share one graph
        distinguished only by ``_generation``, so a fixed literal tag means
        the *second* run of the same verb discards the *live* graph the
        first run just committed under that same literal. A unique suffix
        per call closes that hole: the discard right before a build only
        ever targets that call's own (empty) generation.
        """
        unique_tag = f"{tag}-{uuid.uuid4().hex[:12]}"
        return self._vault_path / _NEO4J_MARKER_DIR / unique_tag

    def discard(self, staged: Path) -> None:
        """Delete every node/relationship tagged with the parsed generation.

        Relationships first, to avoid racing the node cascade -- mirrors
        the M5 spec's ordering note.

        Refuses (no-op, logged) when ``staged``'s tag is the metadata
        singleton's *current* ``graph_generation`` -- i.e. the live graph.
        This should never legitimately happen (``stage_path`` hands out a
        unique tag per call, so a genuine pre-build cleanup discard only
        ever targets that call's own never-yet-populated generation), but
        it is the backstop against exactly the failure this guard exists
        for: something upstream passing back the live path/tag by mistake
        must never delete the live graph.
        """
        tag = staged.name
        store = self._store
        with store._driver_boundary(), store._driver.session(database=store._database) as session:  # noqa: SLF001
            live_record = session.run(
                "MATCH (m:Node {id: $id, vault_id: $vault_id, _generation: $meta_generation}) "
                "RETURN m.graph_generation AS graph_generation",
                {
                    "id": _schema_metadata_node_id(),
                    "vault_id": store.vault_id,
                    "meta_generation": _meta_generation(),
                },
            ).single()
            if live_record is not None and live_record["graph_generation"] == tag:
                _logger.warning(
                    "Neo4jStaging.discard refused: tag %r is the live graph_generation "
                    "for vault_id=%r -- refusing to delete the live graph",
                    tag,
                    store.vault_id,
                )
                return
            _delete_generation(session, store.vault_id, tag)

    def commit(self, staged: Path, backup_tag: str) -> Path:
        """Read the current live generation off the metadata singleton,
        stash it under ``m.backup_tag`` for :meth:`restore` to flip back to,
        then overwrite ``graph_generation`` to the build tag -- one
        transaction.

        Neo4j never copies bytes (the pointer-flip model this class's own
        docstring describes) -- the previous live generation's nodes/edges
        stay put in the graph, still tagged with their own ``_generation``,
        so "backup" here is purely the metadata row remembering which
        generation tag to point back at. ``backup_tag`` (the caller's naming
        convention for the returned marker path, e.g. every offline owner's
        fixed ``"bak"``) is NOT what gets restored -- storing the caller's
        arbitrary label as ``m.backup_tag`` instead of the actual prior
        generation would make :meth:`restore` flip live onto that label
        string rather than back onto the real previous generation.
        """
        build_tag = staged.name
        store = self._store
        stale_generation: str | None = None
        new_backup_generation: str | None = None

        def _tx_fn(tx: Any) -> None:
            nonlocal stale_generation, new_backup_generation
            record = tx.run(
                "MATCH (m:Node {id: $id, vault_id: $vault_id, _generation: $meta_generation}) "
                "RETURN m.graph_generation AS graph_generation, m.backup_tag AS backup_tag",
                {
                    "id": _schema_metadata_node_id(),
                    "vault_id": store.vault_id,
                    "meta_generation": _meta_generation(),
                },
            ).single()
            if record is None:
                raise OktoNeuronError(
                    "no live metadata row to commit against", vault_path=self._vault_path
                )
            current_live = record["graph_generation"]
            # The generation that was the rollback backup going INTO this
            # commit is about to be superseded by ``current_live`` (this
            # commit's new backup) -- it is no longer live nor the backup
            # after this transaction, so it is garbage. Bound retained
            # generations to live + one backup per vault (scoped by
            # ``vault_id``) instead of accumulating one orphaned generation
            # per commit forever.
            stale_generation = record["backup_tag"]
            new_backup_generation = current_live
            tx.run(
                "MATCH (m:Node {id: $id, vault_id: $vault_id, _generation: $meta_generation}) "
                "SET m.backup_tag = $previous_generation, m.graph_generation = $build_tag",
                {
                    "id": _schema_metadata_node_id(),
                    "vault_id": store.vault_id,
                    "meta_generation": _meta_generation(),
                    "previous_generation": current_live,
                    "build_tag": build_tag,
                },
            ).consume()

        with store._driver_boundary(), store._driver.session(database=store._database) as session:  # noqa: SLF001
            session.execute_write(_tx_fn)
            if (
                stale_generation is not None
                and stale_generation != build_tag
                and stale_generation != new_backup_generation
            ):
                _delete_generation(session, store.vault_id, stale_generation)
        backup_path = self._vault_path / _NEO4J_MARKER_DIR / backup_tag
        return backup_path

    def restore(self, backup: Path) -> None:
        """Reverse :meth:`commit`'s write: flip ``graph_generation`` back to
        whatever generation ``commit`` stashed under ``m.backup_tag`` on the
        metadata singleton -- ``backup``'s own path/name is inert here
        (unlike Ladybug/Grafx's byte-copy backups, Neo4j's "backup" is
        entirely the metadata row; see :meth:`commit`'s docstring for why
        the caller's naming convention is never what gets restored).
        """
        store = self._store

        def _tx_fn(tx: Any) -> None:
            record = tx.run(
                "MATCH (m:Node {id: $id, vault_id: $vault_id, _generation: $meta_generation}) "
                "RETURN m.backup_tag AS backup_tag",
                {
                    "id": _schema_metadata_node_id(),
                    "vault_id": store.vault_id,
                    "meta_generation": _meta_generation(),
                },
            ).single()
            if record is None or record["backup_tag"] is None:
                raise OktoNeuronError(
                    "no backed-up generation to restore", vault_path=self._vault_path
                )
            previous_generation = record["backup_tag"]
            tx.run(
                "MATCH (m:Node {id: $id, vault_id: $vault_id, _generation: $meta_generation}) "
                "SET m.graph_generation = $previous_generation, m.backup_tag = null",
                {
                    "id": _schema_metadata_node_id(),
                    "vault_id": store.vault_id,
                    "meta_generation": _meta_generation(),
                    "previous_generation": previous_generation,
                },
            ).consume()

        with store._driver_boundary(), store._driver.session(database=store._database) as session:  # noqa: SLF001
            session.execute_write(_tx_fn)


def _delete_generation(session: Any, vault_id: str, generation: str) -> None:
    """Delete every node/relationship tagged with ``generation`` for ``vault_id``.

    Relationships first, to avoid racing the node cascade -- mirrors the M5
    spec's ordering note. Shared by :meth:`Neo4jStaging.discard` (deleting a
    just-abandoned staged build) and :meth:`Neo4jStaging.commit` (garbage-
    collecting the generation a new commit supersedes past live + one
    backup). Callers are responsible for ensuring ``generation`` is neither
    the live nor the (new) backup generation before calling this.
    """
    session.run(
        "MATCH (:Node {vault_id: $vault_id, _generation: $generation})"
        "-[e:EDGE]->(:Node {vault_id: $vault_id, _generation: $generation}) DELETE e",
        {"vault_id": vault_id, "generation": generation},
    ).consume()
    session.run(
        "MATCH (n:Node {vault_id: $vault_id, _generation: $generation}) DELETE n",
        {"vault_id": vault_id, "generation": generation},
    ).consume()


def _schema_metadata_node_id() -> str:
    from okto_neuron.store import schema

    return schema.SCHEMA_METADATA_NODE_ID


def _meta_generation() -> str:
    """The fixed sentinel ``_generation`` tag Neo4j's per-vault metadata
    singleton is always stamped with (``Neo4jStore._bootstrap_or_adopt_metadata``)
    -- scopes ``commit``/``restore``'s pointer-flip lookup to that one row
    instead of matching every generation's own metadata row.
    """
    from okto_neuron.store import schema

    return schema.NEO4J_METADATA_GENERATION


def staging_for(vault_path: Path, backend_name: str, store: Any = None) -> StagingPort:
    """Resolve the :class:`StagingPort` implementation for a pinned backend.

    ``orchestrate.py``/``cli/kg.py``/``server/_curation.py`` construct
    ``LadybugStaging(vault_path)`` directly today (M2b); this factory is the
    seam a future backend-dispatch caller uses instead, without touching any
    of those existing call sites. ``store`` is required only for
    ``"neo4j"`` (a server backend has no filesystem path to stage from
    alone) -- backward compatible with every existing filesystem-backed
    caller, which never passes it.
    """
    if backend_name == "ladybug":
        return LadybugStaging(vault_path)
    if backend_name == "grafx":
        return GrafxStaging(vault_path)
    if backend_name == "neo4j":
        return Neo4jStaging(vault_path, store)
    raise ValueError(f"no StagingPort implementation registered for graph backend {backend_name!r}")


# --- Relocated swap primitives (verbatim from cli/kg.py:2155-2261, 2274-2279) ---
# cli/kg.py keeps these names as thin re-exports until M2b's re-routing (§3)
# removes its last internal caller.


def _active_graph_sidecars(graph_path: Path) -> list[Path]:
    if not graph_path.parent.exists():
        return []
    return [
        sibling
        for sibling in sorted(graph_path.parent.iterdir())
        if sibling.name.startswith(f"{graph_path.name}.") and ".bak" not in sibling.suffixes
    ]


def _move_graph_family(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    siblings = _active_graph_sidecars(source)
    moved: list[tuple[Path, Path]] = []
    try:
        if source.exists():
            os.replace(source, target)
            moved.append((source, target))
        for sibling in siblings:
            tail = sibling.name[len(source.name) :]
            moved_target = target.with_name(f"{target.name}{tail}")
            os.replace(sibling, moved_target)
            moved.append((sibling, moved_target))
    except Exception as move_exc:
        rollback_error: Exception | None = None
        for original, relocated in reversed(moved):
            if not relocated.exists() or original.exists():
                continue
            try:
                os.replace(relocated, original)
            except Exception as exc:  # noqa: BLE001 - retain the rollback failure
                rollback_error = exc
        _fsync_parent_dir(source)
        if target.parent != source.parent:
            _fsync_parent_dir(target)
        if rollback_error is not None:
            raise rollback_error from move_exc
        raise
    else:
        _fsync_parent_dir(source)
        if target.parent != source.parent:
            _fsync_parent_dir(target)


def _discard_graph_family(graph_path: Path) -> None:
    """Remove one rebuild-owned database and all of its Ladybug sidecars."""

    for sidecar in _active_graph_sidecars(graph_path):
        sidecar.unlink(missing_ok=True)
    graph_path.unlink(missing_ok=True)
    if graph_path.parent.exists():
        _fsync_parent_dir(graph_path)


def _copy_closed_graph_checkpoint(source: Path, target: Path) -> None:
    """Durably stage one closed checkpoint without consuming its backup."""

    if not source.is_file():
        raise FileNotFoundError(source)
    sidecars = _active_graph_sidecars(source)
    if sidecars:
        raise RuntimeError(
            "rollback checkpoint has active sidecars: " + ", ".join(path.name for path in sidecars)
        )
    _discard_graph_family(target)
    shutil.copy2(source, target)
    with target.open("rb") as handle:
        os.fsync(handle.fileno())
    _fsync_parent_dir(target)


def _swap_rebuilt_graph(
    vault_path: Path,
    graph_path: Path,
    tmp_graph_path: Path,
    backup_graph_path: Path,
) -> None:
    staging_sidecars = _active_graph_sidecars(tmp_graph_path)
    if staging_sidecars:
        raise RebuildSwapFailed(
            vault_path,
            source_path=tmp_graph_path,
            target_path=graph_path,
            cause=RuntimeError(
                "staging graph still has live sidecars after close: "
                + ", ".join(path.name for path in staging_sidecars)
            ),
        )
    live_moved_to_backup = False
    try:
        _move_graph_family(graph_path, backup_graph_path)
        live_moved_to_backup = True
        os.replace(tmp_graph_path, graph_path)
        _fsync_parent_dir(graph_path)
    except Exception as swap_exc:
        cause = swap_exc
        if live_moved_to_backup and backup_graph_path.exists() and not graph_path.exists():
            try:
                _move_graph_family(backup_graph_path, graph_path)
            except Exception as restore_exc:
                cause = restore_exc
        raise RebuildSwapFailed(
            vault_path,
            source_path=tmp_graph_path,
            target_path=graph_path,
            cause=cause,
        ) from cause


def _fsync_parent_dir(path: Path) -> None:
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
