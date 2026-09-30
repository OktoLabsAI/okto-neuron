"""Okto Grafx-backed ``GraphStore`` implementation for Okto Neuron vaults.

M4 (ADR 0041 and its internal plan) shipped this as the
second graph backend behind ``okto-neuron[grafx]``, initially flagged
experimental; a later owner decision retired that flag and made Grafx the
default, non-experimental graph backend (D-12 below is now historical).
Relevant plan decisions this module implements:

- **D-02**: Grafx is M4 (moved ahead of Neo4j) precisely because it is
  embedded — no server process, no network dependency — so it closes the
  "second backend" gap with the least new infrastructure.
- **D-10**: the retry policy for optimistic write conflicts is 8 attempts,
  full-jitter exponential backoff from 50ms, capped at 2s per wait and 30s
  total — `config/_vault.py`'s `RetryConfig` pins the defaults,
  `store/_retry.py`'s backend-neutral `retry_with_backoff` implements the
  backoff loop, and :func:`GrafxStore._run_with_retry` is this backend's
  thin wrapper around both.
- **D-12 (retired)**: Grafx originally shipped as a hard onboarding UX gate
  (pre-alpha on-disk format, non-OSI license), enforced by CLI/onboarding
  code outside this module. The owner decision making Grafx the default,
  non-experimental backend removed that gate (``cli/__init__.py``'s former
  ``_confirm_experimental_backend`` no longer exists); the on-disk format and
  Elastic License 2.0 + Addendum license are unchanged, they are just no
  longer consent-gated. This module still has no opinion on consent, it only
  refuses to open quietly broken state.
- **D-16**: :meth:`GrafxStore.checkpoint` is a no-op, matching
  ``BackendCapabilities.checkpoint_is_noop`` for this backend (set outside this
  module, in ``store/capabilities.py``). Grafx's own WAL/commit-ledger is the
  durability authority for every committed write; there is no Ladybug-style
  "merge WAL into the durable file only on clean close" gap to bridge here.

**Divergence from the plan's original design, confirmed at M4 spec time.** The
plan (the internal ADR 0041 plan S3.3/S3.7) originally proposed a
*logical* generation tag (a `_generation` property + singleton pointer row,
shared with the future Neo4j/Neptune backends via a `_generation_tag_base.py`
mixin) specifically to avoid ever renaming/replacing Grafx's own managed
directory. The M4 spec (S2, S8 OQ3) supersedes that: Grafx's default
`descriptor_revalidation="strict"` mode (unlike the opt-in `"generation"` mode)
carries no such exclusive-directory prerequisite, so `store/staging.py`'s
`GrafxStaging` (a sibling of `LadybugStaging`, not implemented in this file)
does a **physical directory swap** — the same `os.replace` shape Ladybug's
rebuild/heal/reembed pipeline already uses, just directory-to-directory instead
of file-to-file. `_generation_tag_base.py` stays unbuilt for M4; this store
therefore has no `_generation` property or pointer-row concept at all — see
:meth:`GrafxStore.generation` for the identity this backend actually exposes
instead (an ordinary schema-metadata row, field-compatible with Ladybug's).

**Dependency isolation.** `okto_neuron.store.ladybug` and `okto_neuron.store.
_bootstrap` both do a hard, module-level `import ladybug` — a real dependency
on the optional `[ladybug]` extra. `okto-neuron[grafx]` must stay importable
and usable with *only* `okto-grafx` installed (no `[ladybug]` extra), so this
module never imports from either of those two modules, even for trivial
shared logic (the JSON envelope helpers, the configured-embedding-dimension
lookup): each such helper is duplicated here in miniature, with a docstring
note at the duplication site. See ``tests/store/contract`` (Gate 2, M4 spec
S5) — its `--extra grafx` invocation carries no `--extra ladybug`.

**Cypher-dialect gaps closed against what Ladybug's `store/ladybug.py` issues**
(M4 spec S1/S2, empirically verified against published `okto-grafx==0.0.3`):

- No `IF NOT EXISTS` / `ALTER TABLE` — this backend bootstraps its schema
  exactly once, gated on an empty catalog (:meth:`GrafxStore._create_schema`).
- No `STRING[]` column type — `tags` is JSON-string-encoded exactly like
  `facets`/`provenance` already are on Ladybug.
- No `MERGE ... ON CREATE SET` / `ON MATCH SET` — every write is an
  unconditional bare `MERGE ... SET`, identical in shape to what
  `LadybugStore.add_node` already issues; Grafx's own MVCC still raises
  `GrafxWriteConflict` (`retryable=True`) for two transactions racing the same
  row, which is what the retry loop below actually depends on for
  correctness — verified live under a real two-transaction race.
- No inline relationship property map (`-[e:Edge {id: $id}]->` is rejected by
  the planner with "write the condition in WHERE instead"); every edge lookup
  here matches the relationship bare and filters with a `WHERE e.id = $id`
  clause instead — this is a genuine dialect divergence from Ladybug's own
  Cypher, not called out explicitly in the M4 spec body, found empirically
  while building this module.
- `CHECKPOINT` is a Python method (`Database.checkpoint()`), not a statement;
  irrelevant here since D-16 makes this backend's `checkpoint()` a no-op.
- A `VECTOR(space)` column rejects a plain `list[float]` outright
  (`SchemaMismatchError`) — every embedding write wraps the vector in
  `okto_grafx.VectorValue(tuple(...), space_id, dtype="float64")` instead (M4
  spec S8 OQ... shepherd override O3's empirical finding). `storage_dtype`
  is pinned to `"float64"` (not Grafx's `"float32"` default) to preserve the
  same fidelity Ladybug's `DOUBLE[dim]` column already gives.
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, TypedDict, TypeVar

import okto_grafx as grafx
from okto_grafx.domain import errors as grafx_errors

from okto_neuron.config._vault import RetryConfig
from okto_neuron.core.schema import Edge, Node, Provenance
from okto_neuron.errors import EmbeddingDimMismatch, GraphBackendError, GraphWriteExhausted
from okto_neuron.store import schema
from okto_neuron.store._retry import retry_with_backoff
from okto_neuron.store.closed_set import require_same_edge_identity, require_writable_node_type
from okto_neuron.store.integrity import EdgeAdjacencyObservation
from okto_neuron.store.protocol import BackendHealth, DriftReport, RecoveryStatus

_T = TypeVar("_T")

#: Bounded retry policy for READS that hit a retryable grafx error (for example
#: ``index_view_changed`` when another process publishes a commit between the
#: view snapshot and the exact read). Deliberately small: a view change heals on
#: the next attempt, so this adds at most ~2 s to a pathological read and
#: nothing to a healthy one. Writes keep the D-10 policy in ``_retry_policy``.
_READ_RETRY_POLICY = RetryConfig(
    max_attempts=6, backoff_base_ms=10, backoff_cap_ms=200, total_cap_s=2.0
)


class _IdentityRow(TypedDict):
    """The shape `_bootstrap_or_adopt_metadata`/`_read_metadata_row` resolve
    to -- whichever of the two lands (an adopted, already-committed row, or
    this attempt's own just-committed write) always carries all four fields.
    """

    graph_generation: str
    identity_contract_version: str
    embedding_dim: int
    schema_version: int


#: Name of the single vector space every embedding column binds to. One space
#: covers the one embedding column this backend's DDL declares (`Node.embedding`);
#: a second embedded vector column would need a second named space.
_EMBED_SPACE_NAME = "node_embed"

#: Preserves Ladybug's `DOUBLE[dim]` fidelity; Grafx's own default is float32.
_EMBED_STORAGE_DTYPE = "float64"


def _read_retry_sleep(seconds: float) -> None:
    """Indirection so tests can observe/skip the read-retry backoff."""
    time.sleep(seconds)


class GrafxWriteExhausted(GraphWriteExhausted):
    """A Grafx write did not land after exhausting the D-10 retry budget.

    Mirrors D-41's backend-neutral `GRAPH_WRITE_FAILED_CODE` semantics without
    needing that constant's plumbing directly. It is a `OktoNeuronError` (via
    `GraphWriteExhausted`) so CLI callers get a clean, typed exit path, and the
    shared base lets `server/http.py` log it as a failed graph write on REST and
    MCP without importing this optional driver.
    """

    default_message = "graph write failed: retry budget exhausted under sustained write conflict"


_LOG = logging.getLogger("okto_neuron.store.grafx")

#: Default grafx buffer pool: max(256 MiB, 1.5 x graph size), capped at 1 GiB.
#: grafx's own default is 64 MiB, which thrashes on a ~180 MB graph.
BUFFER_BUDGET_FLOOR_BYTES = 256 * 1024**2
BUFFER_BUDGET_CAP_BYTES = 1024**3


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        return 0
    return total


def default_buffer_budget(graph_size_bytes: int) -> int:
    """max(256 MiB, 1.5 x ``graph_size_bytes``), capped at 1 GiB."""
    return min(
        BUFFER_BUDGET_CAP_BYTES,
        max(BUFFER_BUDGET_FLOOR_BYTES, int(graph_size_bytes * 1.5)),
    )


def _resolve_buffer_budget(config: Any, vault_path: Path, graph_path: Path) -> tuple[int, int, str]:
    """Return ``(budget_bytes, graph_size_bytes, source)``.

    ``source`` is ``"config"`` when ``storage.buffer_budget`` is set (directly
    on ``config``, or, for a staged/rebuild open that carries no config, in the
    vault's own yaml, which already folds in ``defaults.yaml`` inheritance),
    else ``"default"``.
    """
    size = _dir_size_bytes(graph_path) if graph_path.exists() else 0
    configured = getattr(config, "buffer_budget", None)
    if configured is None and config is None:
        try:
            from okto_neuron.config import VaultConfig

            configured = getattr(VaultConfig.load(vault_path).storage, "buffer_budget", None)
        except Exception:
            configured = None
    if configured is not None:
        return int(configured), size, "config"
    return default_buffer_budget(size), size, "default"


def _resolve_retry_policy(config: Any) -> RetryConfig:
    """Read D-10 knobs off ``config.retry`` when present, else the defaults.

    ``config`` is normally a real ``GrafxStorageConfig`` (`config/_vault.py`)
    once the vault is actually pinned to this backend, whose ``retry`` field
    is itself a ``RetryConfig`` -- structurally exactly what
    ``store/_retry.py``'s ``retry_with_backoff`` wants for its ``policy``
    argument, so it is passed straight through. Reading it duck-typed (rather
    than asserting the concrete type) means a caller passing no config, or a
    config object with no ``retry`` attribute (e.g. a bare test double), both
    fall through to ``RetryConfig()``'s own D-10 defaults instead of raising.
    """
    retry = getattr(config, "retry", None)
    return retry if retry is not None else RetryConfig()


def _resolve_vault_path(path: Path | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


#: Live graph directory name, matching `store/staging.py`'s `GrafxStaging`.
_GRAPH_DIR_NAME = "graph.grafx"


def _is_graph_directory_path(path: Path) -> bool:
    """True when ``path`` already names a Grafx graph directory itself —
    the live ``graph.grafx``, or a staged sibling like ``graph.<tag>.grafx``
    (`store/staging.py`'s ``GrafxStaging.stage_path``) — rather than a vault
    root a graph directory should be derived from by appending
    `_GRAPH_DIR_NAME`.

    This backend's constructor is called both ways (M4 spec S2): the normal
    open path (`store/vault.py`'s `_construct_backend` -> `from_vault`)
    always passes a genuine vault root, but the staged-rebuild path
    (`cli/kg.py`'s `_open_grafx_staged_store`, reused by the daemon's
    `run_heal`/`run_reembed` via `_swap_construction_for`) constructs
    ``GrafxStore(staged_path, embedding_dim=dim)`` directly (the caller's
    already-resolved width — see the constructor's own `embedding_dim`
    docstring), where ``staged_path`` is already the graph directory
    `GrafxStaging.stage_path` named — mirrors
    `LadybugStore`'s own dual-purpose first argument (a real vault path for
    the live open; an already-resolved handle for a staged one), collapsed
    onto one positional argument here since Grafx has no separate handle
    object to inject. A vault root a user deliberately named `graph.grafx`
    or `graph.<x>.grafx` would misfire this heuristic; an Okto Neuron vault
    root is chosen by the user and is never named that way in practice.
    """
    name = path.name
    return name == _GRAPH_DIR_NAME or (name.startswith("graph.") and name.endswith(".grafx"))


def _resolve_configured_dim(vault_path: Path) -> int:
    """Resolve the configured embedding width from ``okto-neuron.yaml``.

    A miniature, deliberately-duplicated copy of `store/_bootstrap.py`'s
    identically-named helper — not imported, because that module does
    `import ladybug` at top level (see the module docstring's dependency-
    isolation note). Falls back to the shared default width exactly like the
    original: a bare bootstrap in tests, or a vault scaffolded before its
    config is written, must never crash an open on a missing/partial config.
    """
    try:
        from okto_neuron.config import VaultConfig

        return int(VaultConfig.load(vault_path).embedding.dimension)
    except Exception:
        return schema.DEFAULT_EMBEDDING_DIM


def _json_dump(value: object) -> str:
    """Identical encoding to `store/ladybug.py`'s helper of the same name
    (json + base64, ``json:``-prefixed) — duplicated rather than imported for
    the dependency-isolation reason in the module docstring. Any drift between
    the two copies would show up immediately as a decode failure in
    `tests/store/contract`, which both backends run unmodified.
    """
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


def _to_grafx_timestamp(value: datetime) -> Any:
    return grafx.Timestamp.from_wall(value.timestamp())


def _from_grafx_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value.to_wall(), tz=timezone.utc)  # type: ignore[union-attr]


def _ddl_statements(dim: int) -> tuple[str, str, str]:
    """DDL for the two live tables (`Node`/`Edge`) plus their vector space.

    Ports only `Node`/`Edge` — M4 spec S1/S8 OQ1: a repo-wide grep found zero
    read/write call sites against Ladybug's other 6 keep-listed-but-dead
    tables (`Authority`, `Annotation`, `Reference`, `Work`, `Document`,
    `Block`), so this backend's `bootstrap_fresh()`-equivalent replicates what
    the write/read path actually issues, not every table a prior design
    declared and never used.

    `dim` is interpolated as a literal int, never bound as `$dim` — Grafx's
    planner rejects a parameterized `CREATE VECTOR SPACE` outright
    ("declared with constants, not with parameters"), verified live. The
    space statement must precede the table statements (verified live: the
    reverse order fails to resolve `VECTOR(node_embed)` in the column list).
    """
    if isinstance(dim, bool) or not isinstance(dim, int):
        raise TypeError(f"embedding dimension must be a plain int, got {dim!r}")
    space_ddl = (
        f"CREATE VECTOR SPACE {_EMBED_SPACE_NAME} "
        f"{{dimension: {int(dim)}, metric: 'cosine', storage_dtype: '{_EMBED_STORAGE_DTYPE}'}}"
    )
    node_ddl = (
        "CREATE NODE TABLE Node("
        "id STRING, type STRING, title STRING, content STRING, tags STRING, "
        "facets STRING, provenance STRING, created_at TIMESTAMP, schema_version INT64, "
        "embedding_dim INT64, graph_generation STRING, identity_contract_version STRING, "
        f"embedding VECTOR({_EMBED_SPACE_NAME}), PRIMARY KEY(id))"
    )
    edge_ddl = (
        "CREATE REL TABLE Edge("
        "FROM Node TO Node, id STRING, type STRING, src STRING, dst STRING, "
        "weight DOUBLE, provenance STRING, created_at TIMESTAMP)"
    )
    return space_ddl, node_ddl, edge_ddl


# Same 9 assignments `LadybugStore.add_node` issues — never touches
# `embedding_dim`/`graph_generation`/`identity_contract_version`, which stay
# NULL on every ordinary data node; only the schema-metadata row (see
# `_METADATA_MERGE_SQL` below) sets those three.
_NODE_MERGE_SQL = """
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
"""

_NODE_READ_COLUMNS = (
    "n.id AS id, n.type AS type, n.title AS title, n.content AS content, "
    "n.tags AS tags, n.facets AS facets, n.provenance AS provenance, "
    "n.created_at AS created_at, n.embedding AS embedding"
)

_EDGE_READ_COLUMNS = (
    "e.id AS id, e.type AS type, e.src AS src, e.dst AS dst, "
    "e.weight AS weight, e.provenance AS provenance"
)

_EDGE_CREATE_SQL = """
MATCH (s:Node {id: $src}), (d:Node {id: $dst})
CREATE (s)-[e:Edge {
    id: $id, type: $type, src: $src, dst: $dst,
    weight: $weight, provenance: $provenance, created_at: $created_at
}]->(d)
"""

_EDGE_UPDATE_SQL = """
MATCH (:Node)-[e:Edge]->(:Node)
WHERE e.id = $id
SET e.weight = $weight, e.provenance = $provenance
"""

_METADATA_MERGE_SQL = """
MERGE (m:Node {id: $id})
SET m.type = $type,
    m.title = $title,
    m.schema_version = $schema_version,
    m.embedding_dim = $embedding_dim,
    m.graph_generation = $graph_generation,
    m.identity_contract_version = $identity_contract_version,
    m.created_at = $created_at
"""

_METADATA_READ_SQL = (
    "MATCH (m:Node {id: $id}) "
    "RETURN m.graph_generation AS graph_generation, "
    "m.identity_contract_version AS identity_contract_version, "
    "m.embedding_dim AS embedding_dim, m.schema_version AS schema_version"
)


_ID_QUERY_BATCH = 1000
"""Ids per ``IN $ids`` query, under Grafx's ``MAX_LIST_ELEMENTS`` (1024) cap.

``query._vector_seeds`` deliberately passes EVERY embedded node id in one call —
its untruncated scan is the documented recall floor, so that a node with a
non-trivial cosine is reachable even with zero token overlap. Unbatched, that
made ``ask`` fail outright on any vault holding more than 1024 embedded nodes
with ``GrafxConfigurationError: A query list may hold at most 1024 elements``.
Batch here rather than truncating there: the caller's recall contract is
preserved and the backend's limit stays the backend's concern.
"""


class GrafxStore:
    """Okto Grafx implementation of the `GraphStore` protocol (the default backend)."""

    def __init__(
        self,
        vault_path: Path | str,
        *,
        config: Any = None,
        embedding_dim: int | None = None,
        _skip_dim_guard: bool = False,
    ) -> None:
        """``embedding_dim`` is an explicit override for the width a *fresh*
        graph is bootstrapped at, used only when this construction is the one
        that creates the schema (below). It never affects an already-existing
        graph: a stored graph keeps whatever dimension it was created with,
        and the mismatch check further down still compares that stored value
        against ``okto-neuron.yaml``'s configured width exactly as before —
        the override does not participate in that comparison. Callers that
        need a specific width regardless of what's currently on disk (e.g.
        `cli/kg.py`'s `_open_grafx_staged_store`, whose caller already
        resolved the live graph's actual dimension or a `reembed` target
        width that may not be written to `okto-neuron.yaml` yet) pass it
        explicitly instead of relying on the vault's config file, which is
        only ever a fallback.

        ``_skip_dim_guard`` is the private raw-open switch used only by
        :meth:`open_for_live_read` (below) — mirrors `LadybugStore`'s
        `_open_live_handle` raw-handle path (`cli/kg.py`), which reads an
        existing graph at whatever width it already has without tripping the
        dim-guard. It exists because `kg reembed`'s own live-graph read (the
        read that copies old-width vectors into the newly configured width)
        would otherwise construct through this same guarded path and reject
        itself before it can do the one thing it exists to do.
        """
        given = _resolve_vault_path(vault_path)
        if _is_graph_directory_path(given):
            # Staged/rebuild construction — see `_is_graph_directory_path`.
            # The real vault root, needed only to resolve the configured
            # embedding width below, is this directory's own parent.
            self.graph_path = given
            self.vault_path = given.parent
        else:
            self.vault_path = given
            self.graph_path = given / _GRAPH_DIR_NAME
        self._closed = False
        # In-flight grafx calls (#22): every statement/transaction this store
        # issues runs under ``_grafx_call``, so shutdown can tell "a thread is
        # parked in an LLM wait" (count 0, safe to close around) from "a thread
        # is inside a grafx statement" (count > 0, closing underneath it is
        # unproven). ``close`` waits for the count to reach zero and refuses
        # new calls once it has started.
        self._call_cond = threading.Condition()
        self._calls_in_flight = 0
        self._closing = False
        self._retry_policy = _resolve_retry_policy(config)
        budget, graph_size, source = _resolve_buffer_budget(
            config, self.vault_path, self.graph_path
        )
        self.buffer_budget_bytes = budget
        self.buffer_budget_source = source
        _LOG.info(
            "grafx open: vault=%s graph_bytes=%d buffer_budget_bytes=%d source=%s",
            self.vault_path.name,
            graph_size,
            budget,
            source,
        )
        self._db = grafx.connect(
            self.graph_path,
            descriptor_revalidation="strict",
            buffer_budget_bytes=budget,
        )

        fresh = not self._db.catalog.catalog.table_definitions
        configured_dim = _resolve_configured_dim(self.vault_path)
        effective_dim = configured_dim if embedding_dim is None else embedding_dim
        if fresh:
            self._create_schema(effective_dim)

        metadata = self._bootstrap_or_adopt_metadata(effective_dim)
        self._graph_generation: str = str(metadata["graph_generation"])
        self._identity_contract_version: str = str(metadata["identity_contract_version"])
        self._embedding_dim: int = int(metadata["embedding_dim"])
        self._embed_space_id: int = self._lookup_embed_space_id()

        if not fresh and not _skip_dim_guard and self._embedding_dim != configured_dim:
            # Mirrors LadybugStore/`schema.verify_embedding_dim`'s fail-loud
            # contract: the vector column is fixed-width once bootstrapped, so
            # a model/dimension change on an existing Grafx graph can't be
            # applied in place. `kg reembed` is the documented remedy there
            # too — and it is also the caller that must pass
            # ``_skip_dim_guard=True`` (via :meth:`open_for_live_read`) to
            # perform the very read this guard would otherwise block.
            raise EmbeddingDimMismatch(
                self.graph_path,
                stored_dim=self._embedding_dim,
                configured_dim=configured_dim,
                vault_path=self.vault_path,
            )

    @classmethod
    def from_vault(cls, vault_path: Path | str, config: Any = None) -> "GrafxStore":
        """Preferred construction path for `store/vault.py`'s `_construct_backend`.

        Called positionally (`from_vault(vault_path, config)`) — see
        `store/vault.py:_construct_backend`'s docstring.
        """
        return cls(vault_path, config=config)

    @classmethod
    def open_for_live_read(cls, vault_path: Path | str, config: Any = None) -> "GrafxStore":
        """Open an EXISTING graph for reading, bypassing the dim-guard.

        Mirrors `cli/kg.py`'s `_open_live_handle` (the Ladybug raw-handle
        path) for this backend: this just opens the on-disk graph so its
        nodes/edges can be read at whatever width they were written, rather
        than going through the guarded :meth:`from_vault`. Used by
        `cli/kg.py`'s `_open_live_store` for `kg reembed`'s (and `kg heal`'s
        pre-swap) live-graph read — the whole purpose of that read is to
        pull an old-width graph's rows so they can be recomputed at a new
        configured width, which is exactly the case the ordinary
        constructor's dim-guard exists to refuse.
        """
        return cls(vault_path, config=config, _skip_dim_guard=True)

    # ------------------------------------------------------------------
    # GraphStore protocol
    # ------------------------------------------------------------------

    @property
    def is_closed(self) -> bool:
        return self._closed

    def add_node(self, node: Node) -> None:
        self._ensure_open()
        require_writable_node_type(node.type)

        def attempt() -> None:
            existing = self.get_node(node.id)
            if existing is not None and _same_node_payload(existing, node):
                return
            created_at = existing.created_at if existing is not None else node.created_at
            params = self._node_write_params(node, created_at=created_at)
            self._execute_write(_NODE_MERGE_SQL, params)

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
                self._execute_write(_EDGE_UPDATE_SQL, self._edge_update_params(edge))
                return
            self._execute_write(_EDGE_CREATE_SQL, self._edge_create_params(edge))

        self._run_with_retry(attempt)

    def get_node(self, node_id: str) -> Optional[Node]:
        self._ensure_open()
        rows = self._query(
            f"MATCH (n:Node {{id: $id}}) RETURN {_NODE_READ_COLUMNS}",
            {"id": node_id},
        )
        if not rows:
            return None
        return self._node_from_row(rows[0])

    def get_nodes(self, node_ids: Iterable[str]) -> list[Node]:
        self._ensure_open()
        ids = list(dict.fromkeys(node_ids))
        if not ids:
            return []
        by_id: dict[str, Node] = {}
        for start in range(0, len(ids), _ID_QUERY_BATCH):
            batch = ids[start : start + _ID_QUERY_BATCH]
            rows = self._query(
                f"MATCH (n:Node) WHERE n.id IN $ids RETURN {_NODE_READ_COLUMNS}",
                {"ids": batch},
            )
            by_id.update({row["id"]: self._node_from_row(row) for row in rows})
        return [by_id[i] for i in ids if i in by_id]

    def list_nodes(self, type: Optional[str] = None) -> Iterable[Node]:
        self._ensure_open()
        if type is None:
            rows = self._query(
                f"MATCH (n:Node) WHERE n.id <> $metadata_id "
                f"RETURN {_NODE_READ_COLUMNS} ORDER BY n.id",
                {"metadata_id": schema.SCHEMA_METADATA_NODE_ID},
            )
        else:
            rows = self._query(
                f"MATCH (n:Node) WHERE n.id <> $metadata_id AND n.type = $type "
                f"RETURN {_NODE_READ_COLUMNS} ORDER BY n.id",
                {"metadata_id": schema.SCHEMA_METADATA_NODE_ID, "type": type},
            )
        return (self._node_from_row(row) for row in rows)

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
        rows = self._query(
            f"MATCH (:Node)-[e:Edge]->(:Node) {where} RETURN {_EDGE_READ_COLUMNS} ORDER BY e.id",
            params,
        )
        return (self._edge_from_row(row) for row in rows)

    def list_edge_adjacency(self) -> Iterable[EdgeAdjacencyObservation]:
        """Read physical endpoints independently from stored edge properties.

        Not part of the core `GraphStore` protocol — a separate, optional
        `EdgeAdjacencyReader` protocol (`store/integrity.py`) that
        `store/integrity.py`'s `_scan_adjacency` duck-types via
        `isinstance`/`getattr`, degrading gracefully (skipping the
        physical-adjacency audit, not crashing) when a backend omits it.
        Implemented here for full audit-coverage parity with Ladybug.
        """
        self._ensure_open()
        rows = self._query(
            "MATCH (s:Node)-[e:Edge]->(d:Node) "
            "RETURN s.id AS actual_src, d.id AS actual_dst, "
            "e.src AS stored_src, e.dst AS stored_dst, "
            "e.id AS edge_id, e.type AS edge_type "
            "ORDER BY e.id, s.id, d.id"
        )
        return (
            EdgeAdjacencyObservation(
                actual_src=str(row["actual_src"]),
                actual_dst=str(row["actual_dst"]),
                stored_src=str(row["stored_src"]) if row.get("stored_src") is not None else None,
                stored_dst=str(row["stored_dst"]) if row.get("stored_dst") is not None else None,
                edge_id=str(row["edge_id"]) if row.get("edge_id") is not None else None,
                edge_type=str(row["edge_type"]) if row.get("edge_type") is not None else None,
            )
            for row in rows
        )

    def checkpoint(self) -> None:
        """No-op (D-16).

        Grafx's own WAL/commit-ledger is the durability authority for every
        committed write — unlike Ladybug, which only merges its WAL into the
        durable file on a clean close, Grafx has no equivalent gap for a
        mid-session call here to close. `okto_grafx.Database.checkpoint()`
        (a maintenance/space-reclaim operation returning a `RecycleReport`,
        not a durability boundary) is deliberately never called from this
        no-op; `store/capabilities.py`'s `checkpoint_is_noop` flag for
        `"grafx"` is set to match, outside this module.
        """
        self._ensure_open()

    @property
    def calls_in_flight(self) -> int:
        """Grafx statements/transactions executing right now (#22)."""
        with self._call_cond:
            return self._calls_in_flight

    @contextlib.contextmanager
    def _grafx_call(self) -> Iterator[None]:
        """Count one grafx call; refuse new ones once close has begun."""
        with self._call_cond:
            if self._closing or self._closed:
                raise GraphBackendError(
                    "graph store is closed",
                    backend="grafx",
                    vault_path=self.vault_path,
                )
            self._calls_in_flight += 1
        try:
            yield
        finally:
            with self._call_cond:
                self._calls_in_flight -= 1
                if not self._calls_in_flight:
                    self._call_cond.notify_all()

    def close(self) -> None:
        """Close the database once no grafx call is executing.

        Blocks until every in-flight call has finished (new calls are refused
        from the moment close starts), so the database is never closed under a
        running statement. Shutdown polls :attr:`calls_in_flight` first and
        skips the close, relying on WAL recovery, when a call is still running
        at the hard deadline.
        """
        with self._call_cond:
            if self._closed:
                return
            self._closing = True
            self._call_cond.wait_for(lambda: self._calls_in_flight == 0)
            self._closed = True
        if not self._db.closed:
            self._db.close()

    def generation(self) -> str:
        """This store's current graph-identity stamp (see `GraphStore.generation`).

        Backed by an ordinary schema-metadata `Node` row (id
        `schema.SCHEMA_METADATA_NODE_ID`, type `"SchemaMetadata"`) using
        Ladybug's exact field names (`graph_generation`,
        `identity_contract_version`), read once at open/bootstrap time and
        cached — not the plan's originally-proposed `_generation` property
        tag (see the module docstring's divergence note).
        """
        return self._graph_generation

    def health(self) -> BackendHealth:
        """Self-check this store's backing Grafx directory (see `GraphStore.health`).

        Opens a FRESH, independent connection to the durable bytes on disk —
        matching `curation/orchestrate.py`'s `_require_healthy` contract
        (called AFTER `close()` for both heal and reembed: "A backend's
        health() opens its own fresh read-only connection independent of
        this (now-closed) handle") — rather than short-circuiting on
        `self._closed`, which would report this *handle's* state, not
        whether the durable graph on disk is actually readable. Verified
        live: a `read_only=True` reopen right after close raises
        `GrafxUnsupportedOperation` ("cannot prove a checkpoint-complete
        database from commit.state and the WAL"); a plain writable reopen
        (this store's own constructor shape) runs recovery/checkpoint and
        succeeds, so that is what this probe uses too.
        """
        try:
            probe = grafx.connect(self.graph_path, descriptor_revalidation="strict")
            try:
                probe.execute(
                    "MATCH (m:Node {id: $id}) RETURN m.id AS id",
                    {"id": schema.SCHEMA_METADATA_NODE_ID},
                )
            finally:
                probe.close()
        except Exception as exc:  # noqa: BLE001 - health check reports, never raises
            return BackendHealth(healthy=False, detail=f"{type(exc).__name__}: {exc}")
        return BackendHealth(healthy=True, detail="ok")

    def recovery_status(self) -> RecoveryStatus:
        """Always unrecovered (see `GraphStore.recovery_status`).

        M4 spec S8 OQ2 (accepted, shepherd override O4): a read-only
        diagnostic that nothing branches destructively on, deliberately
        hardcoded rather than wired to `Database.recovery_report.outcome`
        (a real, cheap signal this module's own smoke testing found exists —
        left for a future reversal if that signal is ever needed).
        """
        return RecoveryStatus(recovered=False)

    def detect_drift(self, expected_generation: str | None) -> DriftReport | None:
        """Always None (see `GraphStore.detect_drift`).

        Drift detection against Grafx's own on-disk identity is not
        implemented for M4 (out of scope, mirrors `InMemoryStore`'s same
        deliberate no-op); a Grafx vault does not yet get the "graph file
        silently replaced under a live handle" protection Ladybug's
        `graph_file_identity` check gives.
        """
        return None

    # ------------------------------------------------------------------
    # Internal: schema bootstrap
    # ------------------------------------------------------------------

    def _create_schema(self, dim: int) -> None:
        with self._grafx_call():
            txn = self._db.begin("write")
            try:
                for statement in _ddl_statements(dim):
                    txn.execute(statement)
                txn.commit()
            except Exception:
                if txn.active:
                    txn.rollback()
                raise

    def _read_metadata_row(self) -> _IdentityRow | None:
        rows = self._query(
            _METADATA_READ_SQL, {"id": schema.SCHEMA_METADATA_NODE_ID}
        )
        if not rows:
            return None
        row = rows[0]
        graph_generation = row.get("graph_generation")
        identity_contract_version = row.get("identity_contract_version")
        if graph_generation is None or identity_contract_version is None:
            return None
        return _IdentityRow(
            graph_generation=str(graph_generation),
            identity_contract_version=str(identity_contract_version),
            embedding_dim=int(row.get("embedding_dim") or 0),  # type: ignore[arg-type]
            schema_version=int(row.get("schema_version") or 0),  # type: ignore[arg-type]
        )

    def _bootstrap_or_adopt_metadata(self, dim: int) -> _IdentityRow:
        """Write the schema-metadata row once, or adopt whoever wrote it first.

        Grafx has no `MERGE ... ON CREATE SET` (M4 spec S1/S2), so this can't
        be a single idempotent DDL statement the way Ladybug's
        `_schema_metadata_ddl` is. Instead: read first; a present row wins
        outright (adopt its identity, write nothing). An absent row attempts
        an unconditional `MERGE ... SET` with a freshly-minted identity; if a
        concurrent process's own attempt already landed between the read and
        this commit, Grafx's MVCC raises `GrafxWriteConflict` (verified live
        under a real two-transaction race on a brand-new id) and the retry
        loop re-reads — this time finding the row the other process just
        committed — and adopts it instead of retrying its own write. Net
        effect: whichever identity lands first always wins, matching
        Ladybug's `ON CREATE SET` semantics exactly, built from primitives
        Grafx actually supports.
        """

        def attempt() -> _IdentityRow:
            existing = self._read_metadata_row()
            if existing is not None:
                return existing
            identity = schema.new_graph_identity()
            params = {
                "id": schema.SCHEMA_METADATA_NODE_ID,
                "type": "SchemaMetadata",
                "title": f"Marginalia schema v{schema.CURRENT_SCHEMA_VERSION}",
                "schema_version": schema.CURRENT_SCHEMA_VERSION,
                "embedding_dim": int(dim),
                "graph_generation": identity.graph_generation,
                "identity_contract_version": identity.identity_contract_version,
                "created_at": _to_grafx_timestamp(datetime.now(timezone.utc)),
            }
            self._execute_write(_METADATA_MERGE_SQL, params)
            # schema.new_graph_identity() always mints both fields non-None
            # (a fresh uuid4() + the current contract constant); str(...)
            # here is a type-narrowing no-op at runtime, not a real fallback.
            return _IdentityRow(
                graph_generation=str(identity.graph_generation),
                identity_contract_version=str(identity.identity_contract_version),
                embedding_dim=int(dim),
                schema_version=schema.CURRENT_SCHEMA_VERSION,
            )

        return self._run_with_retry(attempt)

    def _lookup_embed_space_id(self) -> int:
        for space in self._db.catalog.catalog.space_definitions:
            if space.name == _EMBED_SPACE_NAME:
                return int(space.space_id)
        raise RuntimeError(f"Grafx vector space {_EMBED_SPACE_NAME!r} missing after bootstrap")

    # ------------------------------------------------------------------
    # Internal: write plumbing (D-10 retry)
    # ------------------------------------------------------------------

    def _query(
        self, statement: str, params: Mapping[str, object] | None = None
    ) -> list[dict[str, Any]]:
        """Run a read and translate this backend's driver failures.

        Every read funnels through here so an ``okto_grafx`` exception never
        escapes the store abstraction: callers catch
        :class:`~okto_neuron.errors.GraphBackendError` and the driver stays this
        adapter's private business.

        Deliberately NOT used on the write path. ``_run_with_retry`` duck-types
        ``.retryable`` on the raw driver exception to decide what to retry, so
        translating before the retry loop would make every write look
        non-retryable. Writes are translated after retry gives up instead.
        """
        def read() -> list[dict[str, Any]]:
            with self._grafx_call():
                if params is None:
                    # list_edge_adjacency passes no parameters; _db.execute
                    # treats them as optional and so must this wrapper.
                    return self._db.execute(statement).dictionaries()
                return self._db.execute(statement, params).dictionaries()

        try:
            # Reads are idempotent, so a driver-flagged retryable failure is
            # retried here with jittered backoff; callers never see a transient
            # view change. Anything not flagged retryable surfaces immediately.
            return retry_with_backoff(
                read,
                policy=_READ_RETRY_POLICY,
                is_retryable=lambda exc: bool(getattr(exc, "retryable", False)),
                sleep=_read_retry_sleep,
            )
        except grafx_errors.GrafxError as exc:
            raise GraphBackendError(
                str(exc),
                backend="grafx",
                vault_path=self.vault_path,
                cause=exc,
                retryable=bool(getattr(exc, "retryable", False)),
            ) from exc

    def _execute_write(self, statement: str, params: Mapping[str, object]) -> None:
        with self._grafx_call():
            txn = self._db.begin("write")
            try:
                txn.execute(statement, params)
                txn.commit()
            except Exception:
                if txn.active:
                    txn.rollback()
                raise

    def _run_with_retry(self, attempt: Callable[[], _T]) -> _T:
        """D-10's default policy via the shared `store/_retry.py` helper —
        retrying only exceptions the backend itself flags retryable
        (`GrafxWriteConflict` et al. all carry a real `.retryable` attribute;
        anything without one, or with it False/None — including this
        module's own `ValueError` validation failures — propagates
        immediately, unretried).

        `retry_with_backoff` itself re-raises the final attempt's exception
        unchanged; the only way a *retryable* exception ever escapes it is
        genuine exhaustion (`max_attempts`/`total_cap_s` hit), so that one
        case is recoverable from the caught exception's own `.retryable`
        flag with no extra bookkeeping, and gets wrapped in
        :class:`GrafxWriteExhausted` for a clean, typed CLI exit path.
        """
        policy = self._retry_policy
        try:
            return retry_with_backoff(
                attempt,
                policy=policy,
                is_retryable=lambda exc: bool(getattr(exc, "retryable", False)),
            )
        except Exception as exc:
            if not getattr(exc, "retryable", False):
                if isinstance(exc, grafx_errors.GrafxError):
                    # Non-retryable driver failure on the write path: translate
                    # here, AFTER the retry loop has had its look at the raw
                    # exception's own .retryable flag.
                    raise GraphBackendError(
                        str(exc), backend="grafx", vault_path=self.vault_path, cause=exc
                    ) from exc
                raise
            raise GrafxWriteExhausted(
                f"graph write did not land after {policy.max_attempts} attempt(s) "
                "under sustained write conflict",
                vault_path=self.vault_path,
                cause=exc,
            ) from exc

    # ------------------------------------------------------------------
    # Internal: row <-> model conversion
    # ------------------------------------------------------------------

    def _to_vector_value(self, embedding: list[float] | None) -> Any:
        if embedding is None:
            return None
        return grafx.VectorValue(
            tuple(float(x) for x in embedding), self._embed_space_id, dtype=_EMBED_STORAGE_DTYPE
        )

    def _node_write_params(self, node: Node, *, created_at: datetime) -> dict[str, object]:
        return {
            "id": node.id,
            "type": node.type,
            "title": node.title,
            "content": node.content,
            "tags": _json_dump(list(node.tags)),
            "facets": _json_dump(node.facets),
            "provenance": _json_dump(node.provenance.model_dump(mode="json")),
            "created_at": _to_grafx_timestamp(created_at),
            "schema_version": schema.CURRENT_SCHEMA_VERSION,
            "embedding": self._to_vector_value(node.embedding),
        }

    def _node_from_row(self, row: Mapping[str, object]) -> Node:
        embedding_value = row.get("embedding")
        embedding = list(embedding_value.values) if embedding_value is not None else None  # type: ignore[union-attr]
        tags_raw = row.get("tags")
        tags = _json_load(tags_raw) if tags_raw else []
        if not isinstance(tags, list):
            tags = []
        data: dict[str, object] = {
            "id": row["id"],
            "type": row["type"],
            "title": row.get("title") or "",
            "content": row.get("content") or "",
            "tags": tags,
            "facets": _json_load(row.get("facets")),
            "provenance": Provenance.model_validate(_json_load(row.get("provenance"))),
            "embedding": embedding,
        }
        created_at = _from_grafx_timestamp(row.get("created_at"))
        if created_at is not None:
            data["created_at"] = created_at
        return Node.model_validate(data)

    def _get_edge(self, edge_id: str) -> Edge | None:
        rows = self._query(
            f"MATCH (:Node)-[e:Edge]->(:Node) WHERE e.id = $id RETURN {_EDGE_READ_COLUMNS}",
            {"id": edge_id},
        )
        if not rows:
            return None
        return self._edge_from_row(rows[0])

    def _edge_from_row(self, row: Mapping[str, object]) -> Edge:
        return Edge.model_validate(
            {
                "id": row["id"],
                "type": row["type"],
                "src": row["src"],
                "dst": row["dst"],
                "weight": row["weight"] if row.get("weight") is not None else 1.0,
                "provenance": Provenance.model_validate(_json_load(row.get("provenance"))),
            }
        )

    def _edge_create_params(self, edge: Edge) -> dict[str, object]:
        return {
            "id": edge.id,
            "type": edge.type,
            "src": edge.src,
            "dst": edge.dst,
            "weight": edge.weight,
            "provenance": _json_dump(edge.provenance.model_dump(mode="json")),
            "created_at": _to_grafx_timestamp(datetime.now(timezone.utc)),
        }

    def _edge_update_params(self, edge: Edge) -> dict[str, object]:
        return {
            "id": edge.id,
            "weight": edge.weight,
            "provenance": _json_dump(edge.provenance.model_dump(mode="json")),
        }

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("GrafxStore is closed")


def _same_node_payload(existing: Node, proposed: Node) -> bool:
    """Compare persisted node semantics while preserving immutable creation time.

    Identical logic to `store/ladybug.py`'s helper of the same name —
    duplicated, not imported, for the dependency-isolation reason in the
    module docstring.
    """
    return existing.model_dump(exclude={"created_at"}) == proposed.model_dump(
        exclude={"created_at"}
    )


__all__ = ["GrafxStore", "GrafxWriteExhausted"]
