"""Ladybug-backed GraphStore implementation for Okto Neuron vaults."""

from __future__ import annotations

import atexit
import base64
import contextlib
import gc
import json
import threading
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import ladybug

from okto_neuron.core.schema import Edge, Node, Provenance
from okto_neuron.errors import VaultCorrupted
from okto_neuron.store import schema
from okto_neuron.store._bootstrap import (
    VaultGraphHandle,
    _bootstrap_cache,
    bootstrap_vault_graph,
)
from okto_neuron.store.closed_set import require_same_edge_identity, require_writable_node_type
from okto_neuron.store.integrity import EdgeAdjacencyObservation
from okto_neuron.store.protocol import BackendHealth, DriftReport, RecoveryStatus


def _resolve_vault_path(path: Path | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


class VaultConnection:
    """Context-managed Ladybug connection over a cached vault database."""

    _handles: dict[Path, VaultGraphHandle] = {}
    _active: dict[Path, set["VaultConnection"]] = {}
    _lock = threading.RLock()

    def __init__(
        self,
        vault_path: Path | str,
        *,
        graph_handle: VaultGraphHandle | None = None,
    ) -> None:
        self.vault_path = _resolve_vault_path(vault_path)
        self._closed = False
        self.handle = self._get_handle(self.vault_path, graph_handle)
        self.db = self.handle.database
        self.conn = ladybug.Connection(self.db)
        with self._lock:
            self._active.setdefault(self.vault_path, set()).add(self)

    def __enter__(self) -> tuple[ladybug.Database, ladybug.Connection]:
        return self.db, self.conn

    def __exit__(self, *args: object) -> None:
        self.close()

    @classmethod
    def _get_handle(
        cls,
        vault_path: Path,
        graph_handle: VaultGraphHandle | None,
    ) -> VaultGraphHandle:
        with cls._lock:
            cached = cls._handles.get(vault_path)
            if cached is not None and not getattr(cached.database, "is_closed", False):
                return cached
            if cached is not None:
                cls._handles.pop(vault_path, None)

            handle = graph_handle or bootstrap_vault_graph(vault_path)
            if getattr(handle.database, "is_closed", False):
                handle = bootstrap_vault_graph(vault_path)
            cls._handles[vault_path] = handle
            return handle

    def close(self) -> None:
        """Close this connection. Idempotent."""
        if self._closed:
            return
        self._closed = True
        close = getattr(self.conn, "close", None)
        if callable(close) and not getattr(self.conn, "is_closed", False):
            close()
        with self._lock:
            active = self._active.get(self.vault_path)
            if active is not None:
                active.discard(self)
                if not active:
                    self._active.pop(self.vault_path, None)

    @classmethod
    def close_vault(cls, vault_path: Path | str) -> None:
        """Close every process-local connection and database for one vault."""
        resolved = _resolve_vault_path(vault_path)
        with cls._lock:
            active = list(cls._active.get(resolved, set()))
        for connection in active:
            connection.close()

        with cls._lock:
            handle = cls._handles.pop(resolved, None)
            bootstrap_handle = _bootstrap_cache.pop(resolved, None)
        candidates = [handle]
        if bootstrap_handle is not handle:
            candidates.append(bootstrap_handle)
        for candidate in candidates:
            if candidate is not None and not getattr(candidate.database, "is_closed", False):
                candidate.close()
        gc.collect()

    @classmethod
    def close_all(cls) -> None:
        """Close all process-local vault database handles."""
        with cls._lock:
            vault_paths = list(cls._handles)
            vault_paths.extend(path for path in cls._active if path not in cls._handles)
        for vault_path in vault_paths:
            cls.close_vault(vault_path)


atexit.register(VaultConnection.close_all)


def verify_ladybug_db_health(path: Path | str) -> bool:
    """Open a Ladybug graph read-only and verify its schema metadata."""
    graph_path = Path(path).expanduser().resolve(strict=False)
    database: ladybug.Database | None = None
    connection: ladybug.Connection | None = None
    try:
        database = ladybug.Database(graph_path, read_only=True)
        connection = ladybug.Connection(database)
        _read_table_inventory(connection)
        schema.verify_schema_version(connection, file_path=graph_path)
    except Exception as exc:
        raise VaultCorrupted(graph_path, cause=exc) from exc
    finally:
        if connection is not None:
            connection.close()
        if database is not None:
            database.close()
    return True


class LadybugStore:
    """Ladybug implementation of the GraphStore protocol."""

    def __init__(
        self,
        vault_path: Path | str,
        *,
        graph_handle: VaultGraphHandle | None = None,
    ) -> None:
        self.vault_path = _resolve_vault_path(vault_path)
        self._closed = False
        self._graph_handle = graph_handle or bootstrap_vault_graph(self.vault_path)
        self._semantic_write_epoch = 0
        self._pinned_connection: "ladybug.Connection | None" = None
        VaultConnection._get_handle(self.vault_path, self._graph_handle)

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def semantic_write_epoch(self) -> int:
        """Monotonic count of successful semantic mutation statements."""

        return self._semantic_write_epoch

    def add_node(self, node: Node) -> None:
        self._ensure_open()
        require_writable_node_type(node.type)
        existing = self.get_node(node.id)
        if existing is not None and _same_node_payload(existing, node):
            return
        params = _node_params(node)
        if existing is not None:
            params["created_at"] = existing.created_at
        self._execute(
            """
            MERGE (n:Node {id: $id})
            SET n.type = $type,
                n.title = $title,
                n.content = $content,
                n.tags = $tags,
                n.facets = $facets,
                n.provenance = $provenance,
                n.created_at = $created_at,
                n.schema_version = $schema_version,
                n.embedding = $embedding
            """,
            params,
        )

    def add_edge(self, edge: Edge) -> None:
        # Ladybug 0.16 can persist relationship properties against the wrong
        # physical adjacency after DELETE-by-id + CREATE churn.  Edge identity is
        # therefore immutable: an existing id always denotes the same
        # (type, src, dst).  Mutable payload is updated on the existing
        # relationship, never by reconstructing its topology.
        self._ensure_open()
        if self.get_node(edge.src) is None or self.get_node(edge.dst) is None:
            raise ValueError(f"edge endpoints missing: {edge.src} -> {edge.dst}")
        existing = self._get_edge(edge.id)
        if existing == edge:
            return
        if existing is not None:
            require_same_edge_identity(existing, edge)
            self._execute(
                """
                MATCH (:Node)-[e:Edge {id: $id}]->(:Node)
                SET e.weight = $weight,
                    e.provenance = $provenance
                """,
                _edge_params(edge),
            )
            return

        self._execute(
            """
            MATCH (s:Node {id: $src}), (d:Node {id: $dst})
            CREATE (s)-[e:Edge {
                id: $id,
                type: $type,
                src: $src,
                dst: $dst,
                weight: $weight,
                provenance: $provenance,
                created_at: $created_at
            }]->(d)
            """,
            _edge_params(edge),
        )

    def _get_edge(self, edge_id: str) -> Edge | None:
        rows = self._fetch_rows(
            """
            MATCH (:Node)-[e:Edge {id: $id}]->(:Node)
            RETURN e.id, e.type, e.src, e.dst, e.weight, e.provenance
            """,
            {"id": edge_id},
        )
        if not rows:
            return None
        return _edge_from_row(rows[0])

    def get_node(self, node_id: str, include_embedding: bool = True) -> Optional[Node]:
        self._ensure_open()
        columns = _node_columns(include_embedding)
        rows = self._fetch_rows(
            f"""
            MATCH (n:Node {{id: $id}})
            RETURN {columns}
            """,
            {"id": node_id},
        )
        if not rows:
            return None
        return _node_from_row(rows[0])

    def get_nodes(self, node_ids: Iterable[str], include_embedding: bool = False) -> list[Node]:
        self._ensure_open()
        columns = _node_columns(include_embedding)
        ids = list(dict.fromkeys(node_ids))
        if not ids:
            return []
        rows = self._fetch_rows(
            f"""
            MATCH (n:Node)
            WHERE n.id IN $ids
            RETURN {columns}
            """,
            {"ids": ids},
        )
        by_id = {node.id: node for node in (_node_from_row(row) for row in rows)}
        return [by_id[i] for i in ids if i in by_id]

    def list_nodes(
        self, type: Optional[str] = None, include_embedding: bool = False
    ) -> Iterable[Node]:
        self._ensure_open()
        columns = _node_columns(include_embedding)
        if type is None:
            rows = self._fetch_rows(
                f"""
                MATCH (n:Node)
                WHERE n.id <> $metadata_id
                RETURN {columns}
                ORDER BY n.id
                """,
                {"metadata_id": schema.SCHEMA_METADATA_NODE_ID},
            )
        else:
            rows = self._fetch_rows(
                f"""
                MATCH (n:Node)
                WHERE n.id <> $metadata_id AND n.type = $type
                RETURN {columns}
                ORDER BY n.id
                """,
                {"metadata_id": schema.SCHEMA_METADATA_NODE_ID, "type": type},
            )
        return (_node_from_row(row) for row in rows)

    def list_edges(
        self,
        src: Optional[str] = None,
        dst: Optional[str] = None,
        type: Optional[str] = None,
    ) -> Iterable[Edge]:
        self._ensure_open()
        clauses: list[str] = []
        params: dict[str, object] = {}
        if src is not None:
            clauses.append("e.src = $src")
            params["src"] = src
        if dst is not None:
            clauses.append("e.dst = $dst")
            params["dst"] = dst
        if type is not None:
            clauses.append("e.type = $type")
            params["type"] = type

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._fetch_rows(
            f"""
            MATCH (:Node)-[e:Edge]->(:Node)
            {where}
            RETURN e.id, e.type, e.src, e.dst, e.weight, e.provenance
            ORDER BY e.id
            """,
            params,
        )
        return (_edge_from_row(row) for row in rows)

    def list_edge_adjacency(self) -> Iterable[EdgeAdjacencyObservation]:
        """Read physical endpoints independently from stored edge properties."""
        self._ensure_open()
        rows = self._fetch_rows(
            """
            MATCH (s:Node)-[e:Edge]->(d:Node)
            RETURN s.id, d.id, e.src, e.dst, e.id, e.type
            ORDER BY e.id, s.id, d.id
            """
        )
        return (_edge_adjacency_from_row(row) for row in rows)

    def checkpoint(self) -> None:
        """Force pending writes into the durable ``.lbug`` file, mid-session.

        Ladybug (like Kuzu, which it wraps) only merges its write-ahead log
        into the main file on a CLEAN close — see ``_bootstrap._recover_from_
        corruption``'s docstring. A long-running writer that never cleanly
        closes (killed, crashed) leaves everything written since the last
        merge stranded in the WAL; an unclean shutdown that then tears that
        WAL loses it all, even though ``_recover_from_corruption`` recovers
        perfectly from the last good checkpoint. Calling ``CHECKPOINT`` here
        creates that checkpoint mid-session, without closing the database, so
        a caller can call this at a safe drain point (e.g. after each document
        finishes) and cap how much a later unclean shutdown can lose.

        Deliberately bypasses ``_execute``: ``CHECKPOINT`` is not a semantic
        graph mutation, and routing it through ``_execute`` would advance
        ``_semantic_write_epoch`` — which ADR 0039's write fence and BM25
        cache-generation tracking read as "new content was written". Verified
        safe to call concurrently with independent read connections (REST/UI
        reads do not take the writer lock); it must still only ever be invoked
        from the single writer's own thread/context, never a second writer.
        """
        self._ensure_open()
        with VaultConnection(self.vault_path, graph_handle=self._graph_handle) as (_, connection):
            result = connection.execute("CHECKPOINT;")
            _close_result(result)

    def close(self) -> None:
        """Release every Ladybug handle associated with this store."""
        if self._closed:
            return
        self._closed = True
        VaultConnection.close_vault(self.vault_path)

    def generation(self) -> str:
        """This store's current graph-identity stamp (see GraphStore.generation).

        Reads the identity the graph carried when this handle was opened or
        last swapped onto — the same value the pre-M2b call sites reflected
        via ``getattr(store, "_graph_handle", None).graph_generation``.
        """
        return self._graph_handle.graph_generation or ""

    @contextlib.contextmanager
    def snapshot(self) -> Iterator[str]:
        """Pin one consistent read point for the duration of the ``with`` block (M2c, D-39).

        Opens a **fresh, read-only** ``ladybug.Database`` handle on the same
        on-disk graph file this store currently points at, independent of
        the process-wide ``VaultConnection`` cache. Verified live (not
        assumed, per the plan's own explicit flag, §3.1/§3.7/§5 M2
        ``done_when``): on POSIX, ``os.replace()`` — the exact primitive
        ``staging.py::_swap_rebuilt_graph`` uses to publish a rebuild —
        unlinks the old file's directory entry but leaves an already-open
        file descriptor pointing at the old inode's bytes, so a Database
        opened *before* a concurrent swap keeps reading the pre-swap graph
        for as long as this handle stays open, while a fresh open (or the
        live cached handle other callers use) sees the post-swap graph
        immediately. That gives snapshot isolation across a concurrent
        rebuild without needing any lock here.

        While the block is open, every read this same store instance issues
        (``list_nodes``/``list_edges``/``get_node``/``get_nodes``) is routed
        through the pinned connection instead of the shared cache; nested
        entry is not supported. Yields the graph-generation id observed at
        the pinned point (``""`` for a legacy/pre-identity graph, matching
        :meth:`generation`).
        """
        self._ensure_open()
        if self._pinned_connection is not None:
            raise RuntimeError("LadybugStore.snapshot() does not support nested/concurrent use")
        graph_path = Path(self._graph_handle.database.database_path)
        database = ladybug.Database(graph_path, read_only=True)
        connection = ladybug.Connection(database)
        try:
            identity = schema.read_graph_identity(connection)
            self._pinned_connection = connection
            yield identity.graph_generation or ""
        finally:
            self._pinned_connection = None
            close = getattr(connection, "close", None)
            if callable(close) and not getattr(connection, "is_closed", False):
                close()
            database.close()

    def health(self) -> BackendHealth:
        """Self-check this store's backing ``.lbug`` file (see GraphStore.health).

        Delegates to :func:`verify_ladybug_db_health` against the handle's own
        ``ladybug.Database.database_path`` — the actual path the handle was
        opened at, not ``self.vault_path`` (which callers key on a vault
        directory in some call patterns and on the graph file itself in
        others; see the handle's ``database_path`` for the one value that is
        always correct).
        """
        graph_path = Path(self._graph_handle.database.database_path)
        try:
            verify_ladybug_db_health(graph_path)
        except VaultCorrupted as exc:
            return BackendHealth(healthy=False, detail=str(exc))
        return BackendHealth(healthy=True, detail="ok")

    def recovery_status(self) -> RecoveryStatus:
        """Whether opening this vault had to recover the graph (see GraphStore.recovery_status).

        Reads straight off the bootstrap handle's own fields: ``recovered_from_corruption``
        and ``recovered_mode`` (the real field name on ``VaultGraphHandle`` — a same-shaped
        ``recovery_mode`` elsewhere in the codebase was a pre-M3 attribute-name bug reading
        a field that never existed; this method does not repeat it).
        """
        handle = self._graph_handle
        return RecoveryStatus(
            recovered=handle.recovered_from_corruption,
            mode=handle.recovered_mode,
        )

    def detect_drift(self, expected_generation: str | None) -> DriftReport | None:
        """Compare this handle's on-disk identity against ``expected_generation``.

        Internalizes the drift check that ``server/_integrity.py``'s
        ``_handle_disk_drift_state`` and ``store/integrity_state.py``'s
        ``_require_handle_matches_disk`` each separately reimplemented against a
        hardcoded ``graph.lbug`` path; this reads the handle's own
        ``database.database_path`` instead, so it can't drift from wherever the
        graph file actually lives. Two independent signals are checked: the
        caller-observed generation against this handle's own
        ``graph_generation``, and the on-disk file identity (device, inode) at
        open time (``graph_file_identity``) against right now. Returns None
        when neither has moved and the file is still readable.
        """
        handle = self._graph_handle
        graph_path = Path(handle.database.database_path)
        try:
            stat = graph_path.stat()
            current_file_identity: tuple[int, int] | None = (int(stat.st_dev), int(stat.st_ino))
            error: str | None = None
        except OSError as exc:
            current_file_identity = None
            error = f"{type(exc).__name__}: {exc}"

        generation_changed = expected_generation != handle.graph_generation
        file_changed = (
            handle.graph_file_identity is not None
            and current_file_identity != handle.graph_file_identity
        )
        if error is None and not generation_changed and not file_changed:
            return None

        if error is not None:
            reason = f"on-disk graph identity is unreadable: {error}"
        else:
            reason = (
                "open graph handle belongs to a different on-disk generation: "
                f"handle={handle.graph_generation!r}, expected={expected_generation!r}, "
                f"file_replaced={file_changed}"
            )
        return DriftReport(reason=reason)

    def _execute(self, sql: str, params: Mapping[str, object] | None = None) -> None:
        with VaultConnection(self.vault_path, graph_handle=self._graph_handle) as (_, connection):
            result = connection.execute(sql, dict(params or {}))
            _close_result(result)
        self._semantic_write_epoch += 1

    def _fetch_rows(
        self,
        sql: str,
        params: Mapping[str, object] | None = None,
    ) -> list[object]:
        pinned = self._pinned_connection
        if pinned is not None:
            result = pinned.execute(sql, dict(params or {}))
            return list(_iter_rows(result))
        with VaultConnection(self.vault_path, graph_handle=self._graph_handle) as (_, connection):
            result = connection.execute(sql, dict(params or {}))
            return list(_iter_rows(result))

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("LadybugStore is closed")


def _node_params(node: Node) -> dict[str, object]:
    return {
        "id": node.id,
        "type": node.type,
        "title": node.title,
        "content": node.content,
        "tags": list(node.tags),
        "facets": _json_dump(node.facets),
        "provenance": _json_dump(node.provenance.model_dump(mode="json")),
        "created_at": node.created_at,
        "schema_version": schema.CURRENT_SCHEMA_VERSION,
        "embedding": node.embedding,
    }


def _edge_params(edge: Edge) -> dict[str, object]:
    return {
        "id": edge.id,
        "type": edge.type,
        "src": edge.src,
        "dst": edge.dst,
        "weight": edge.weight,
        "provenance": _json_dump(edge.provenance.model_dump(mode="json")),
        "created_at": datetime.now(timezone.utc),
    }


def _same_node_payload(existing: Node, proposed: Node) -> bool:
    """Compare persisted node semantics while preserving immutable creation time."""

    return existing.model_dump(exclude={"created_at"}) == proposed.model_dump(
        exclude={"created_at"}
    )


_NODE_COLUMNS_NO_VECTOR = (
    "n.id, n.type, n.title, n.content, n.tags, n.facets, n.provenance, n.created_at"
)


def _node_columns(include_embedding: bool) -> str:
    """The projection itself changes: a default read never selects the vector column."""
    return _NODE_COLUMNS_NO_VECTOR + (", n.embedding" if include_embedding else "")


def _node_from_row(row: object) -> Node:
    values = _row_values(row)
    data: dict[str, object] = {
        "id": values[0],
        "type": values[1],
        "title": values[2] or "",
        "content": values[3] or "",
        "tags": values[4] or [],
        "facets": _json_load(values[5]),
        "provenance": Provenance.model_validate(_json_load(values[6])),
        "embedding": values[8] if len(values) > 8 else None,
    }
    created_at = values[7]
    if created_at is not None:
        data["created_at"] = _with_utc(created_at)
    return Node.model_validate(data)


def _edge_from_row(row: object) -> Edge:
    values = _row_values(row)
    return Edge.model_validate(
        {
            "id": values[0],
            "type": values[1],
            "src": values[2],
            "dst": values[3],
            "weight": values[4] if values[4] is not None else 1.0,
            "provenance": Provenance.model_validate(_json_load(values[5])),
        }
    )


def _edge_adjacency_from_row(row: object) -> EdgeAdjacencyObservation:
    values = _row_values(row)
    return EdgeAdjacencyObservation(
        actual_src=str(values[0]),
        actual_dst=str(values[1]),
        stored_src=str(values[2]) if values[2] is not None else None,
        stored_dst=str(values[3]) if values[3] is not None else None,
        edge_id=str(values[4]) if values[4] is not None else None,
        edge_type=str(values[5]) if values[5] is not None else None,
    )


def _row_values(row: object) -> Sequence[Any]:
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        return row
    if isinstance(row, Mapping):
        return tuple(row.values())
    return (row,)


def _json_dump(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii")
    return f"json:{encoded}"


def _json_load(value: object) -> object:
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        if value.startswith("json:"):
            raw = base64.urlsafe_b64decode(value[5:].encode("ascii")).decode("utf-8")
            return json.loads(raw)
        return json.loads(value)
    return value


def _with_utc(value: object) -> object:
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _iter_rows(result: object) -> Iterator[object]:
    try:
        if result is None:
            return
        if hasattr(result, "has_next") and hasattr(result, "get_next"):
            while result.has_next():
                yield result.get_next()
            return
        if isinstance(result, Iterable) and not isinstance(result, (str, bytes, bytearray)):
            yield from result
            return
        yield result
    finally:
        _close_result(result)


def _close_result(result: object) -> None:
    if isinstance(result, list):
        for item in result:
            _close_result(item)
        return
    close = getattr(result, "close", None)
    if callable(close):
        close()


def _read_table_inventory(connection: ladybug.Connection) -> None:
    try:
        result = connection.execute("SHOW TABLES")
    except Exception:
        result = connection.execute("CALL SHOW_TABLES() RETURN *")
    rows = list(_iter_rows(result))
    if not rows:
        raise RuntimeError("Ladybug table inventory is empty")


__all__ = ["LadybugStore", "VaultConnection", "verify_ladybug_db_health"]
