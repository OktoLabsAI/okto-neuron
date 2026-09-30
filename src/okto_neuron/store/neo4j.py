"""Neo4j-backed ``GraphStore`` implementation for Okto Neuron vaults (M5).

Implements M5 of the internal ADR 0041 plan and the M5
spec's design (§2). Relevant decisions this module realizes:

- **D-51 / GenerationScopedBackendMixin**: every read (`get_node`,
  `get_nodes`, `list_nodes`, `list_edges`) is implemented exactly once on
  `store/_generation_tag_base.py`'s mixin, filtered on this store's own
  `_generation_tag` -- see that module's docstring. `Neo4jStore` supplies
  only the driver/session/Cypher-dialect layer.
- **Generation scheme**: a build-mode open (constructed by
  `Neo4jStaging.stage_path`'s synthetic marker path) stamps every write with
  an isolated `_generation` tag instead of the live graph's current tag,
  giving the build phase a genuinely isolated read/write view with no
  `GraphStore` protocol change (§2, §7 -- no public `generation=` kwarg).
- **vault_id scoping (shepherd override)**: Neo4j Community Edition has
  exactly one user database, so every node/relationship this store writes
  also carries `vault_id` (derived deterministically from the resolved
  vault path), and every read/write/constraint/wipe this module issues is
  additionally scoped by it -- otherwise two vaults pinned to the same CE
  instance (e.g. an acceptance vault and a LoCoMo vault) would collide.
- **D-10 retry**: every write runs inside `session.execute_write(...)` (the
  driver's own managed-transaction retry) wrapped again by
  `store/_retry.py`'s `retry_with_backoff`, mirroring
  `GrafxStore._run_with_retry`'s two-layer shape.
- **Schema shape**: `(:Node {id, vault_id, _generation, type, title,
  content, tags, facets, provenance, created_at, schema_version,
  embedding, ...})` and `(s:Node)-[e:EDGE {id, vault_id, _generation, type,
  src, dst, weight, provenance, created_at}]->(d:Node)` -- a genuine
  relationship, not edge-as-node (§2).
- **CE constraint gap**: a composite node uniqueness constraint on
  `(id, vault_id, _generation)` is CE-available and bootstrapped here.
  Neo4j 5-Community has no relationship uniqueness constraint category at
  all in the general case, but the shepherd override directs this module to
  also attempt one (probed compatible against a 5.26 CE server); if the
  server rejects it, bootstrap falls back to the Python-side `_get_edge`
  read-then-branch guard alone (mirroring `grafx.py:434-449`), never
  crashing the open.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

import yaml
from neo4j import GraphDatabase
from neo4j.exceptions import ClientError, DriverError, Neo4jError, ServiceUnavailable

from okto_neuron._compat import secret_env as _secret_env
from okto_neuron._compat import vault_config_path
from okto_neuron.config._vault import RetryConfig
from okto_neuron.core.schema import Edge, Node, Provenance
from okto_neuron.errors import (
    EmbeddingDimMismatch,
    GraphBackendError,
    GraphWriteExhausted,
    OktoNeuronError,
)
from okto_neuron.store import schema
from okto_neuron.store._generation_tag_base import GenerationScopedBackendMixin
from okto_neuron.store._retry import retry_with_backoff
from okto_neuron.store.closed_set import require_same_edge_identity, require_writable_node_type
from okto_neuron.store.integrity import EdgeAdjacencyObservation
from okto_neuron.store.protocol import BackendHealth, DriftReport, RecoveryStatus

_T = TypeVar("_T")

#: Synthetic marker directory a build-mode open recognizes -- never written
#: to disk (this backend has no filesystem graph at all). Mirrors
#: `grafx.py`'s `_GRAPH_DIR_NAME` naming convention, dialect analogue of
#: `_is_graph_directory_path`. See `store/staging.py`'s `Neo4jStaging`.
_GENERATION_MARKER_DIR = ".neo4j-generation"

#: Reserved marker tag (`cli/kg.py`'s `_live_graph_path` neo4j branch, used
#: as `finish_staged_swap`'s `live_graph_path` for the post-swap reopen-and-
#: audit step) that means "open normally, adopting whatever generation the
#: metadata singleton's pointer currently names" -- NOT a build-mode open
#: pinned to the literal string "live" (no real generation is ever tagged
#: that; every build tag is a real staging verb like "rebuild"/"heal"/
#: "reembed"). Without this, reopening `.neo4j-generation/live` right after
#: a commit would filter every read by `_generation: "live"` and find
#: nothing, since the just-committed nodes are actually tagged with the
#: real build tag -- failing the post-swap identity/integrity audit on
#: every rebuild, not just a second one.
LIVE_MARKER_TAG = "live"

#: Fixed CE username (M5 spec §8 OQ1) -- CE has no multi-user roles.
_DEFAULT_USERNAME = "neo4j"

#: UNWIND batch size for the optional bulk helpers (not part of the
#: GraphStore protocol -- used internally by `snapshot.load` and
#: rebuild/copy loops per the shepherd override).
_BULK_BATCH_SIZE = 500

#: The two roots of the driver's exception hierarchy. ``Neo4jError`` is a
#: failure the SERVER reported (syntax, constraint, transient); ``DriverError``
#: is one the client side raised on its own (``Driver closed``, session
#: expired, service unavailable, result already consumed). They are sibling
#: classes, so catching only ``Neo4jError`` lets every client-side failure
#: escape the store abstraction raw. Every translation site uses this tuple.
_DRIVER_ERRORS: tuple[type[Exception], ...] = (Neo4jError, DriverError)


class Neo4jWriteExhausted(GraphWriteExhausted):
    """A Neo4j write did not land after exhausting the D-10 retry budget.

    Mirrors `GrafxWriteExhausted`'s role for this backend.
    """

    default_message = "graph write failed: retry budget exhausted under sustained write conflict"


def _resolve_retry_policy(config: Any) -> RetryConfig:
    retry = getattr(config, "retry", None)
    return retry if retry is not None else RetryConfig()


def _resolve_vault_path(path: Path | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _is_generation_marker_path(path: Path) -> tuple[bool, str | None]:
    """True + the encoded generation tag when ``path`` is a synthetic marker
    path this backend's constructor should treat as a build-mode open, i.e.
    ``<vault_path>/.neo4j-generation/<tag>`` (see `Neo4jStaging.stage_path`).
    ``LIVE_MARKER_TAG`` is the one reserved tag that is NOT a build-mode
    request -- it resolves the vault root exactly like any other marker but
    reports no generation override, so the constructor falls through to a
    normal open that adopts whatever the metadata singleton's pointer
    currently names (see ``LIVE_MARKER_TAG``'s own docstring for why).

    Dialect analogue of `grafx.py`'s `_is_graph_directory_path` -- the
    real vault root, needed to resolve `okto-neuron.yaml`, is this path's
    grandparent in that case.
    """
    if path.parent.name == _GENERATION_MARKER_DIR:
        if path.name == LIVE_MARKER_TAG:
            return True, None
        return True, path.name
    return False, None


def vault_id_for(vault_path: Path) -> str:
    """Deterministic per-vault scoping id (shepherd override).

    Neo4j Community Edition has exactly one user database, so every node
    and relationship this store writes is tagged with this id and every
    read/write/constraint/wipe is scoped by it, keeping two vaults pinned
    to the same CE instance from colliding.
    """
    raw = str(vault_path).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _persist_legacy_vault_id(vault_path: Path, vault_id: str) -> None:
    """Adopt a legacy path-hash id into ``marginalia.yaml``'s ``storage``
    block, once (D-83) -- so a vault opened before ``storage.vault_id``
    existed keeps resolving to the same graph it already wrote under the
    path hash, and a later ``cp -r``/move of the vault directory no longer
    disconnects it (the whole point of persisting an id instead of
    re-deriving it from the path on every open).

    Best-effort and idempotent: if the config can't be read/parsed here,
    or another writer already wrote a ``storage.vault_id``, this is a
    silent no-op -- the caller already has a working ``vault_id`` for this
    process either way. Atomic replace (`tempfile.mkstemp` + `os.replace`)
    so a crash mid-write never leaves a truncated ``okto-neuron.yaml``.
    """
    config_path = vault_config_path(vault_path)
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return
    if not isinstance(raw, dict):
        return
    storage = raw.get("storage")
    if not isinstance(storage, dict) or storage.get("vault_id"):
        return
    storage["vault_id"] = vault_id
    raw["storage"] = storage
    fd, tmp_name = tempfile.mkstemp(
        dir=str(vault_path), prefix=f".{config_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(raw, fh, sort_keys=False)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, config_path)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)


def _resolve_vault_id(vault_path: Path, config: Any) -> str:
    """The scoping id to tag/filter this vault's nodes and relationships
    with (D-83). A configured ``storage.vault_id`` always wins -- it is
    portable across a ``cp -r``/move of the vault directory, unlike the
    legacy path hash. When absent (a pre-D-83 vault), fall back to the
    deterministic path hash for this open, and adopt it into
    ``okto-neuron.yaml`` so future opens read the same id back explicitly
    instead of re-deriving it (and so a subsequent copy/move of the
    directory is portable from then on).
    """
    configured = getattr(config, "vault_id", None)
    if configured:
        return str(configured)
    legacy = vault_id_for(vault_path)
    _persist_legacy_vault_id(vault_path, legacy)
    return legacy


def _resolve_configured_dim(vault_path: Path) -> int:
    """Duplicated in miniature from `store/_bootstrap.py`, exactly like
    `grafx.py`'s copy -- this module must stay importable with only the
    `[neo4j]` extra installed, never pulling in Ladybug.
    """
    try:
        from okto_neuron.config import VaultConfig

        return int(VaultConfig.load(vault_path).embedding.dimension)
    except Exception:
        return schema.DEFAULT_EMBEDDING_DIM


def _json_dump(value: object) -> str:
    """Identical JSON encoding convention to `store/ladybug.py`/`grafx.py`
    (plain JSON here -- no base64 envelope needed since Neo4j properties are
    already native strings, not a fixed-width column type)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _json_load(value: object) -> object:
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        return json.loads(value)
    return value


def _to_epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _from_epoch_ms(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


_NODE_MERGE_CYPHER = """
MERGE (n:Node {id: $id, vault_id: $vault_id, _generation: $generation})
SET n.type = $type,
    n.title = $title,
    n.content = $content,
    n.tags = $tags,
    n.facets = $facets,
    n.provenance = $provenance,
    n.created_at = $created_at,
    n.schema_version = $schema_version,
    n.embedding = $embedding
"""

# Upsert that leaves the stored vector alone (a node written with embedding=None).
_NODE_MERGE_CYPHER_KEEP_VECTOR = _NODE_MERGE_CYPHER.replace(
    ",\n    n.embedding = $embedding", ""
)
assert _NODE_MERGE_CYPHER_KEEP_VECTOR != _NODE_MERGE_CYPHER

_EDGE_CREATE_CYPHER = """
MATCH (s:Node {id: $src, vault_id: $vault_id, _generation: $generation})
MATCH (d:Node {id: $dst, vault_id: $vault_id, _generation: $generation})
CREATE (s)-[e:EDGE {
    id: $id, vault_id: $vault_id, _generation: $generation, type: $type,
    src: $src, dst: $dst, weight: $weight, provenance: $provenance,
    created_at: $created_at
}]->(d)
"""

_EDGE_UPDATE_CYPHER = """
MATCH (:Node)-[e:EDGE]->(:Node)
WHERE e.id = $id AND e.vault_id = $vault_id AND e._generation = $generation
SET e.weight = $weight, e.provenance = $provenance
"""

_METADATA_MERGE_CYPHER = """
MERGE (m:Node {id: $id, vault_id: $vault_id, _generation: $generation})
SET m.type = $type,
    m.title = $title,
    m.schema_version = $schema_version,
    m.embedding_dim = $embedding_dim,
    m.graph_generation = $graph_generation,
    m.identity_contract_version = $identity_contract_version,
    m.backup_tag = $backup_tag,
    m.created_at = $created_at
"""

_METADATA_READ_CYPHER = """
MATCH (m:Node {id: $id, vault_id: $vault_id, _generation: $generation})
RETURN m.graph_generation AS graph_generation,
       m.identity_contract_version AS identity_contract_version,
       m.embedding_dim AS embedding_dim,
       m.schema_version AS schema_version,
       m.backup_tag AS backup_tag
"""


class Neo4jStore(GenerationScopedBackendMixin):
    """Neo4j implementation of the `GraphStore` protocol."""

    def __init__(
        self,
        vault_path: Path | str,
        *,
        config: Any = None,
        generation: str | None = None,
        _skip_dim_guard: bool = False,
    ) -> None:
        """``generation`` is an internal build-mode override (not part of the
        public `GraphStore` protocol, mirrors `GrafxStore.__init__`'s
        `embedding_dim` override) -- a normal open omits it and adopts the
        metadata singleton's current `graph_generation`; a staged/build-mode
        open (constructed via a `Neo4jStaging.stage_path` marker, or passed
        explicitly by `cli/kg.py`'s Neo4j `_swap_construction_for` closure)
        passes the target tag explicitly.

        ``_skip_dim_guard`` is the private raw-open switch used only by
        :meth:`open_for_live_read` (below) -- mirrors `GrafxStore`'s same
        switch (see its docstring), which itself mirrors `LadybugStore`'s
        `_open_live_handle` raw-handle path (`cli/kg.py`). `kg reembed`'s
        own live-graph read needs to read an existing, differently-dimensioned
        graph without tripping the guard that exists specifically to refuse
        that in every other caller.
        """
        given = _resolve_vault_path(vault_path)
        is_marker, marker_tag = _is_generation_marker_path(given)
        if is_marker:
            # Real vault root is this marker path's grandparent:
            # <vault_path>/.neo4j-generation/<tag>
            self.vault_path = given.parent.parent
            generation = generation or marker_tag
        else:
            self.vault_path = given

        self._closed = False
        self._retry_policy = _resolve_retry_policy(config)
        self.vault_id = _resolve_vault_id(self.vault_path, config)

        uri, database, auth = self._resolve_connection(config)
        self._uri = uri
        self._auth = auth
        self._database = database
        with _translate_driver_errors(self.vault_path):
            self._driver = GraphDatabase.driver(uri, auth=auth)
        try:
            self._driver.verify_connectivity()
        except Exception as exc:  # pragma: no cover - depends on live server
            self._driver.close()
            raise OktoNeuronError(
                f"failed to connect to Neo4j at {uri!r}: {exc}",
                vault_path=self.vault_path,
                cause=exc if isinstance(exc, Exception) else None,
            ) from exc

        self._bootstrap_constraints()

        configured_dim = _resolve_configured_dim(self.vault_path)
        metadata = self._bootstrap_or_adopt_metadata(
            configured_dim, requested_generation=generation
        )
        self._generation_tag: str = str(metadata["graph_generation"] if generation is None else generation)
        self._graph_generation: str = str(metadata["graph_generation"])
        self._identity_contract_version: str = str(metadata["identity_contract_version"])
        self._embedding_dim: int = int(metadata["embedding_dim"])
        self._metadata_node_id = schema.SCHEMA_METADATA_NODE_ID

        if (
            metadata["embedding_dim"]
            and not _skip_dim_guard
            and self._embedding_dim != configured_dim
        ):
            raise EmbeddingDimMismatch(
                self.vault_path,
                stored_dim=self._embedding_dim,
                configured_dim=configured_dim,
                vault_path=self.vault_path,
            )

    @classmethod
    def from_vault(cls, vault_path: Path | str, config: Any = None) -> "Neo4jStore":
        """Preferred construction path for `store/vault.py`'s `_construct_backend`."""
        return cls(vault_path, config=config)

    @classmethod
    def open_for_live_read(cls, vault_path: Path | str, config: Any = None) -> "Neo4jStore":
        """Open an EXISTING graph for reading, bypassing the dim-guard.

        Mirrors `GrafxStore.open_for_live_read` (see its docstring) and
        `cli/kg.py`'s Ladybug `_open_live_handle`. Used by `cli/kg.py`'s
        `_open_live_store` for `kg reembed`'s (and `kg heal`'s pre-swap)
        live-graph read.
        """
        return cls(vault_path, config=config, _skip_dim_guard=True)

    # ------------------------------------------------------------------
    # Connection resolution
    # ------------------------------------------------------------------

    def _resolve_connection(self, config: Any) -> tuple[str, str, tuple[str, str]]:
        uri = getattr(config, "uri", None)
        database = getattr(config, "database", None) or "neo4j"
        credential_env = getattr(config, "credential_env", None)
        if uri is None:
            raise OktoNeuronError(
                "Neo4jStore requires config.uri to be set", vault_path=self.vault_path
            )
        password = _secret_env(credential_env, "") if credential_env else ""
        return str(uri), str(database), (_DEFAULT_USERNAME, password)

    # ------------------------------------------------------------------
    # GraphStore protocol
    # ------------------------------------------------------------------

    @property
    def is_closed(self) -> bool:
        return self._closed

    def add_node(self, node: Node, clear_embedding: bool = False) -> None:
        self._ensure_open()
        require_writable_node_type(node.type)
        keep_vector = node.embedding is None and not clear_embedding

        def attempt() -> None:
            existing = self.get_node(node.id)
            effective = (
                node.model_copy(update={"embedding": existing.embedding})
                if keep_vector and existing is not None
                else node
            )
            if existing is not None and _same_node_payload(existing, effective):
                return
            created_at = existing.created_at if existing is not None else node.created_at
            params = self._node_write_params(node, created_at=created_at)
            if keep_vector:
                params.pop("embedding")
                self._execute_write(_NODE_MERGE_CYPHER_KEEP_VECTOR, params)
            else:
                self._execute_write(_NODE_MERGE_CYPHER, params)

        self._run_with_retry(attempt)

    def add_edge(self, edge: Edge) -> None:
        self._ensure_open()

        def attempt() -> None:
            if self.get_node(edge.src) is None or self.get_node(edge.dst) is None:
                raise ValueError(f"edge endpoints missing: {edge.src} -> {edge.dst}")
            existing = self._get_edge(edge.id)
            if existing == edge:
                return
            if existing is not None:
                require_same_edge_identity(existing, edge)
                self._execute_write(_EDGE_UPDATE_CYPHER, self._edge_update_params(edge))
                return
            self._execute_write(_EDGE_CREATE_CYPHER, self._edge_create_params(edge))

        self._run_with_retry(attempt)

    def add_nodes(self, nodes: Iterable[Node]) -> None:
        """Bulk write via UNWIND batching (not part of `GraphStore`'s
        protocol -- an internal helper used by `snapshot.load` and the
        rebuild/copy loops, per the shepherd override).
        """
        self._ensure_open()
        batch: list[Node] = []
        for node in nodes:
            require_writable_node_type(node.type)
            batch.append(node)
            if len(batch) >= _BULK_BATCH_SIZE:
                self._write_node_batch(batch)
                batch = []
        if batch:
            self._write_node_batch(batch)

    def add_edges(self, edges: Iterable[Edge]) -> None:
        """Bulk edge write via UNWIND batching (same scope note as `add_nodes`)."""
        self._ensure_open()
        batch: list[Edge] = []
        for edge in edges:
            batch.append(edge)
            if len(batch) >= _BULK_BATCH_SIZE:
                self._write_edge_batch(batch)
                batch = []
        if batch:
            self._write_edge_batch(batch)

    def checkpoint(self) -> None:
        """No-op -- `checkpoint_is_noop=True` (`store/capabilities.py`).

        Neo4j's own transaction log / commit durability is the durability
        authority for every committed write; there is no Ladybug-style
        "merge WAL into the durable file only on clean close" gap here.
        """
        self._ensure_open()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._driver_boundary():
            self._driver.close()

    def generation(self) -> str:
        return self._generation_tag

    def health(self) -> BackendHealth:
        """Opens a FRESH, independent DRIVER (not just a fresh session on
        ``self._driver``) -- matching `GrafxStore.health`'s contract (never
        reads `self._closed`/`self._driver`'s cached state). `orchestrate.
        heal`/`orchestrate.reembed` deliberately call this AFTER
        ``store.close()`` on the just-built staged store (`_require_healthy`
        durability-checks the closed, durably-flushed graph); a session on
        the already-closed ``self._driver`` would raise Neo4j's own
        ``DriverError: Driver closed`` instead of a clean health verdict, so
        this spins up its own short-lived driver against the same
        connection params every time.
        """
        try:
            driver = GraphDatabase.driver(self._uri, auth=self._auth)
            try:
                with driver.session(database=self._database) as session:
                    session.run("RETURN 1").consume()
                    session.run(
                        "MATCH (m:Node {id: $id, vault_id: $vault_id}) RETURN m.id AS id LIMIT 1",
                        {"id": schema.SCHEMA_METADATA_NODE_ID, "vault_id": self.vault_id},
                    ).consume()
            finally:
                driver.close()
        except Exception as exc:  # noqa: BLE001 - health check reports, never raises
            return BackendHealth(healthy=False, detail=f"{type(exc).__name__}: {exc}")
        return BackendHealth(healthy=True, detail="ok")

    def recovery_status(self) -> RecoveryStatus:
        """Always unrecovered -- not observable over Bolt (D-16/OQ2 precedent,
        same as `GrafxStore.recovery_status`)."""
        return RecoveryStatus(recovered=False)

    def detect_drift(self, expected_generation: str | None) -> DriftReport | None:
        """Always None -- same precedent as `GrafxStore.detect_drift`."""
        return None

    def list_edge_adjacency(self) -> Iterable[EdgeAdjacencyObservation]:
        """Physical-adjacency audit parity with Grafx/Ladybug -- reads real
        endpoints independently from stored edge properties."""
        self._ensure_open()
        rows = self._run_read(
            "MATCH (s:Node)-[e:EDGE]->(d:Node) "
            "WHERE e.vault_id = $vault_id AND e._generation = $generation "
            "RETURN s.id AS actual_src, d.id AS actual_dst, "
            "e.src AS stored_src, e.dst AS stored_dst, "
            "e.id AS edge_id, e.type AS edge_type "
            "ORDER BY e.id, s.id, d.id",
            {"vault_id": self.vault_id, "generation": self._generation_tag},
        )
        return [
            EdgeAdjacencyObservation(
                actual_src=str(row["actual_src"]),
                actual_dst=str(row["actual_dst"]),
                stored_src=str(row["stored_src"]) if row.get("stored_src") is not None else None,
                stored_dst=str(row["stored_dst"]) if row.get("stored_dst") is not None else None,
                edge_id=str(row["edge_id"]) if row.get("edge_id") is not None else None,
                edge_type=str(row["edge_type"]) if row.get("edge_type") is not None else None,
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Internal: schema bootstrap
    # ------------------------------------------------------------------

    def _bootstrap_constraints(self) -> None:
        """Composite node uniqueness constraint (CE-available) on
        ``(id, vault_id, _generation)``. Also attempts the relationship
        composite uniqueness constraint per the shepherd override (probed
        compatible against Neo4j 5.26 CE); falls back silently if the
        server rejects it (older/incompatible CE, or Enterprise-only
        syntax variance), relying on the Python-side `_get_edge` guard
        alone in that case.
        """
        with self._driver_boundary(), self._driver.session(database=self._database) as session:
            session.run(
                "CREATE CONSTRAINT marginalia_node_identity IF NOT EXISTS "
                "FOR (n:Node) REQUIRE (n.id, n.vault_id, n._generation) IS UNIQUE"
            ).consume()
            try:
                session.run(
                    "CREATE CONSTRAINT marginalia_edge_identity IF NOT EXISTS "
                    "FOR ()-[e:EDGE]-() REQUIRE (e.id, e.vault_id, e._generation) IS UNIQUE"
                ).consume()
            except (ClientError, Neo4jError):
                # Relationship uniqueness constraints are not guaranteed on
                # every CE build (M5 spec §8 OQ5) -- the Python-side
                # `_get_edge` read-then-branch guard is the documented
                # fallback enforcement, never a fatal open failure.
                pass

    def _read_metadata_row(self, generation: str) -> dict[str, Any] | None:
        rows = self._run_read(
            _METADATA_READ_CYPHER,
            {
                "id": schema.SCHEMA_METADATA_NODE_ID,
                "vault_id": self.vault_id,
                "generation": generation,
            },
        )
        if not rows:
            return None
        row = rows[0]
        graph_generation = row.get("graph_generation")
        identity_contract_version = row.get("identity_contract_version")
        if graph_generation is None or identity_contract_version is None:
            return None
        return {
            "graph_generation": str(graph_generation),
            "identity_contract_version": str(identity_contract_version),
            "embedding_dim": int(row.get("embedding_dim") or 0),
            "schema_version": int(row.get("schema_version") or 0),
            "backup_tag": row.get("backup_tag"),
        }

    def _bootstrap_or_adopt_metadata(
        self, dim: int, *, requested_generation: str | None
    ) -> dict[str, Any]:
        """Mirrors `GrafxStore._bootstrap_or_adopt_metadata`'s read-first,
        MERGE-race-adopt shape, scoped by `vault_id` only -- the metadata
        node is a per-vault SINGLETON, always tagged with the fixed
        `schema.NEO4J_METADATA_GENERATION` sentinel rather than a real graph
        generation (unlike every other `(id, vault_id, _generation)` node),
        so a build-mode open (`requested_generation` set) reads/adopts the
        SAME row a normal open does instead of minting a second one --
        `Neo4jStaging.commit`/`restore`'s pointer-flip lookup depends on
        there only ever being one.

        `requested_generation` no longer selects which row to read (there is
        only one); it is accepted for the build-mode caller's benefit only
        to preserve this method's existing call shape from `__init__`.
        """
        del requested_generation
        existing = self._read_metadata_row(schema.NEO4J_METADATA_GENERATION)
        if existing is not None:
            return existing
        identity = schema.new_graph_identity()
        assert identity.graph_generation is not None
        return self._mint_metadata_row(dim, identity=identity)

    def _mint_metadata_row(self, dim: int, *, identity: Any = None) -> dict[str, Any]:
        def attempt() -> dict[str, Any]:
            existing = self._read_metadata_row(schema.NEO4J_METADATA_GENERATION)
            if existing is not None:
                return existing
            ident = identity or schema.new_graph_identity()
            graph_generation = ident.graph_generation
            params = {
                "id": schema.SCHEMA_METADATA_NODE_ID,
                "vault_id": self.vault_id,
                "generation": schema.NEO4J_METADATA_GENERATION,
                "type": "SchemaMetadata",
                "title": f"Marginalia schema v{schema.CURRENT_SCHEMA_VERSION}",
                "schema_version": schema.CURRENT_SCHEMA_VERSION,
                "embedding_dim": int(dim),
                "graph_generation": graph_generation,
                "identity_contract_version": ident.identity_contract_version,
                "backup_tag": None,
                "created_at": _to_epoch_ms(datetime.now(timezone.utc)),
            }
            self._execute_write(_METADATA_MERGE_CYPHER, params)
            return {
                "graph_generation": str(graph_generation),
                "identity_contract_version": str(ident.identity_contract_version),
                "embedding_dim": int(dim),
                "schema_version": schema.CURRENT_SCHEMA_VERSION,
                "backup_tag": None,
            }

        return self._run_with_retry(attempt)

    # ------------------------------------------------------------------
    # Internal: session/transaction plumbing (D-10 retry)
    # ------------------------------------------------------------------

    def _run_read(self, cypher: str, params: Mapping[str, object]) -> list[dict[str, Any]]:
        """Structural primitive `GenerationScopedBackendMixin` calls.

        Ordinary `MATCH ... RETURN n` statements bind the whole node/rel as
        `n`/`e`; this helper flattens each record into a plain dict of
        column -> value (a raw property map for `n`/`e`, or a scalar for
        any other aliased column) so callers can treat the result
        uniformly regardless of which shape the statement returned.
        """
        # A driver exception must not escape the store abstraction; see
        # GraphBackendError. Reads are not retried here, so translating is
        # unconditional. A read that runs INSIDE a write attempt (add_node's
        # get_node probe) stays retryable anyway: _is_retryable_neo4j_error
        # looks through the translation at the raw driver exception.
        with self._driver_boundary(), self._driver.session(database=self._database) as session:
            result = session.run(cypher, dict(params))
            rows: list[dict[str, Any]] = []
            for record in result:
                row: dict[str, Any] = {}
                for key in record.keys():
                    value = record[key]
                    row[key] = dict(value) if hasattr(value, "items") else value
                rows.append(row)
            return rows

    def _driver_boundary(self) -> contextlib.AbstractContextManager[None]:
        """Translate any driver exception raised in the block into
        :class:`~okto_neuron.errors.GraphBackendError`.

        Also used by ``store/staging.py``'s ``Neo4jStaging`` and
        ``curation/orchestrate.py``'s rollback probe, which open sessions on
        this store's driver directly. Not for the write path's inner attempt:
        ``_run_with_retry`` must see the raw exception first.
        """
        return _translate_driver_errors(self.vault_path)

    def _execute_write(self, cypher: str, params: Mapping[str, object]) -> None:
        def _tx_fn(tx: Any) -> None:
            tx.run(cypher, dict(params)).consume()

        with self._driver.session(database=self._database) as session:
            session.execute_write(_tx_fn)

    def _write_node_batch(self, nodes: list[Node]) -> None:
        rows = [self._node_write_params(node, created_at=node.created_at) for node in nodes]
        cypher = """
        UNWIND $rows AS row
        MERGE (n:Node {id: row.id, vault_id: row.vault_id, _generation: row.generation})
        SET n.type = row.type, n.title = row.title, n.content = row.content,
            n.tags = row.tags, n.facets = row.facets, n.provenance = row.provenance,
            n.created_at = row.created_at, n.schema_version = row.schema_version,
            n.embedding = coalesce(row.embedding, n.embedding)
        """

        def attempt() -> None:
            self._execute_write(cypher, {"rows": rows})

        self._run_with_retry(attempt)

    def _write_edge_batch(self, edges: list[Edge]) -> None:
        rows = [self._edge_create_params(edge) for edge in edges]
        cypher = """
        UNWIND $rows AS row
        MATCH (s:Node {id: row.src, vault_id: row.vault_id, _generation: row.generation})
        MATCH (d:Node {id: row.dst, vault_id: row.vault_id, _generation: row.generation})
        MERGE (s)-[e:EDGE {id: row.id, vault_id: row.vault_id, _generation: row.generation}]->(d)
        SET e.type = row.type, e.weight = row.weight, e.provenance = row.provenance,
            e.created_at = row.created_at, e.src = row.src, e.dst = row.dst
        """

        def attempt() -> None:
            self._execute_write(cypher, {"rows": rows})

        self._run_with_retry(attempt)

    def _run_with_retry(self, attempt: Callable[[], _T]) -> _T:
        policy = self._retry_policy
        try:
            return retry_with_backoff(
                attempt,
                policy=policy,
                is_retryable=_is_retryable_neo4j_error,
            )
        except Exception as exc:
            if not _is_retryable_neo4j_error(exc):
                if isinstance(exc, _DRIVER_ERRORS):
                    # Translated only AFTER the retry loop has classified the
                    # raw exception; translating inside the attempt would make
                    # a ServiceUnavailable/TransientError look non-retryable.
                    raise GraphBackendError(
                        str(exc), backend="neo4j", vault_path=self.vault_path, cause=exc
                    ) from exc
                raise
            raise Neo4jWriteExhausted(
                f"graph write did not land after {policy.max_attempts} attempt(s) "
                "under sustained write conflict",
                vault_path=self.vault_path,
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Internal: row <-> model conversion
    # ------------------------------------------------------------------

    def _node_write_params(self, node: Node, *, created_at: datetime) -> dict[str, object]:
        return {
            "id": node.id,
            "vault_id": self.vault_id,
            "generation": self._generation_tag,
            "type": node.type,
            "title": node.title,
            "content": node.content,
            "tags": _json_dump(list(node.tags)),
            "facets": _json_dump(node.facets),
            "provenance": _json_dump(node.provenance.model_dump(mode="json")),
            "created_at": _to_epoch_ms(created_at),
            "schema_version": schema.CURRENT_SCHEMA_VERSION,
            "embedding": [float(x) for x in node.embedding] if node.embedding is not None else None,
        }

    def _node_from_row(self, row: Mapping[str, object]) -> Node:
        props = row["n"] if "n" in row else row
        assert isinstance(props, Mapping)
        embedding_value = props.get("embedding")
        embedding = list(embedding_value) if embedding_value is not None else None  # type: ignore[arg-type]
        tags_raw = props.get("tags")
        tags = _json_load(tags_raw) if tags_raw else []
        if not isinstance(tags, list):
            tags = []
        data: dict[str, object] = {
            "id": props["id"],
            "type": props["type"],
            "title": props.get("title") or "",
            "content": props.get("content") or "",
            "tags": tags,
            "facets": _json_load(props.get("facets")),
            "provenance": Provenance.model_validate(_json_load(props.get("provenance"))),
            "embedding": embedding,
        }
        created_at = _from_epoch_ms(props.get("created_at"))
        if created_at is not None:
            data["created_at"] = created_at
        return Node.model_validate(data)

    def _get_edge(self, edge_id: str) -> Edge | None:
        rows = self._run_read(
            "MATCH (:Node)-[e:EDGE]->(:Node) "
            "WHERE e.id = $id AND e.vault_id = $vault_id AND e._generation = $generation "
            "RETURN e",
            {"id": edge_id, "vault_id": self.vault_id, "generation": self._generation_tag},
        )
        if not rows:
            return None
        return self._edge_from_row(rows[0])

    def _edge_from_row(self, row: Mapping[str, object]) -> Edge:
        props = row["e"] if "e" in row else row
        assert isinstance(props, Mapping)
        return Edge.model_validate(
            {
                "id": props["id"],
                "type": props["type"],
                "src": props["src"],
                "dst": props["dst"],
                "weight": props["weight"] if props.get("weight") is not None else 1.0,
                "provenance": Provenance.model_validate(_json_load(props.get("provenance"))),
            }
        )

    def _edge_create_params(self, edge: Edge) -> dict[str, object]:
        return {
            "id": edge.id,
            "vault_id": self.vault_id,
            "generation": self._generation_tag,
            "type": edge.type,
            "src": edge.src,
            "dst": edge.dst,
            "weight": edge.weight,
            "provenance": _json_dump(edge.provenance.model_dump(mode="json")),
            "created_at": _to_epoch_ms(datetime.now(timezone.utc)),
        }

    def _edge_update_params(self, edge: Edge) -> dict[str, object]:
        return {
            "id": edge.id,
            "vault_id": self.vault_id,
            "generation": self._generation_tag,
            "weight": edge.weight,
            "provenance": _json_dump(edge.provenance.model_dump(mode="json")),
        }

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("Neo4jStore is closed")

    # ------------------------------------------------------------------
    # Internal: wipe (used by `store/vault.py`'s `_erase_backend_storage`)
    # ------------------------------------------------------------------

    def wipe(self) -> None:
        """Erase every node/relationship this vault owns, all generations.

        Deliberately not generation-scoped (unlike every read/write method
        above) -- a full vault wipe must clear build-mode leftovers too, not
        just the live generation. Scoped only by `vault_id` so other
        vaults sharing this CE instance are untouched. Drops this vault's
        own constraints last (best-effort: a constraint named for another
        vault is never touched since the name is instance-global but the
        `IF EXISTS` drop is idempotent).
        """
        with self._driver_boundary(), self._driver.session(database=self._database) as session:
            session.run(
                "MATCH (:Node {vault_id: $vault_id})-[e:EDGE]->(:Node {vault_id: $vault_id}) "
                "DELETE e",
                {"vault_id": self.vault_id},
            ).consume()
            session.run(
                "MATCH (n:Node {vault_id: $vault_id}) DELETE n", {"vault_id": self.vault_id}
            ).consume()


@contextlib.contextmanager
def _translate_driver_errors(vault_path: Path) -> Iterator[None]:
    """Re-raise a raw driver exception as ``GraphBackendError`` (``from exc``,
    so the driver exception survives as ``__cause__``). Anything that is not a
    driver exception -- ``OktoNeuronError`` raised inside a transaction
    function, a caller-input ``ValueError`` -- passes through untouched."""
    try:
        yield
    except _DRIVER_ERRORS as exc:
        raise GraphBackendError(
            str(exc), backend="neo4j", vault_path=vault_path, cause=exc
        ) from exc


def _is_retryable_neo4j_error(exc: Exception) -> bool:
    """A compound-constraint-violation race (two writers MERGE-ing the same
    new id) surfaces as a `ClientError`/`ConstraintError` from the driver;
    a transient service hiccup surfaces as `ServiceUnavailable`. Both are
    worth the outer `retry_with_backoff` loop's retry (§2's "outer loop is
    what retries the compound-constraint-violation case").

    A ``GraphBackendError`` from a read inside a write attempt is judged by
    the raw driver exception it wraps, so translating reads at the boundary
    never changes what a write retries.
    """
    if isinstance(exc, GraphBackendError) and isinstance(exc.__cause__, _DRIVER_ERRORS):
        exc = exc.__cause__
    if isinstance(exc, ServiceUnavailable):
        return True
    if isinstance(exc, Neo4jError):
        code = getattr(exc, "code", "") or ""
        return "ConstraintValidationFailed" in code or "TransientError" in code or "DeadlockDetected" in code
    return False


def _same_node_payload(existing: Node, proposed: Node) -> bool:
    """Duplicated from `grafx.py`'s helper of the same name -- see that
    module's dependency-isolation note; this module never imports Grafx."""
    return existing.model_dump(exclude={"created_at"}) == proposed.model_dump(
        exclude={"created_at"}
    )


__all__ = ["Neo4jStore", "Neo4jWriteExhausted", "vault_id_for"]
