"""Ladybug graph schema DDL for Okto Neuron vault storage.

This module is intentionally small: the keep-list below is the source of truth
for graph tables and vector indexes. Bootstrap code should execute
``ddl_statements()`` in order, then call ``verify_schema_version()`` before any
write path that could touch an existing vault.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from uuid import uuid4

from okto_neuron.errors import EmbeddingDimMismatch, SchemaVersionMismatch

CURRENT_SCHEMA_VERSION = 1
SCHEMA_METADATA_NODE_ID = "__marginalia_schema__"
# Neo4j's per-vault metadata singleton (Neo4jStore's pointer row carrying
# graph_generation/backup_tag) is deliberately NOT generation-scoped like
# every other `(id, vault_id, _generation)` node -- there is exactly one of
# it per vault, ever, tagged with this fixed sentinel instead of a real
# graph generation, so a staged/build-mode open never mints a second row
# that a later `Neo4jStaging.commit`/`restore`'s unscoped-by-generation
# lookup could collide with (see `Neo4jStore._bootstrap_or_adopt_metadata`).
NEO4J_METADATA_GENERATION = "__meta__"
LEGACY_IDENTITY_CONTRACT_VERSION = "legacy.v0"
# Fresh graphs and rebuilds mint this contract, which additionally promises that
# every deterministic (content-addressed) edge id equals
# ``sha256_hex("edge", src, type, dst)`` and that no two edges claim the same
# deterministic semantic identity under different ids. The integrity auditor runs
# those checks only for contracts in ``DETERMINISTIC_EDGE_IDENTITY_CONTRACTS``;
# legacy graphs predate the promise and are audited without them.
CURRENT_IDENTITY_CONTRACT_VERSION = "semantic_edges.v1"
DETERMINISTIC_EDGE_IDENTITY_CONTRACTS: frozenset[str] = frozenset({"semantic_edges.v1"})


@dataclass(frozen=True, slots=True)
class GraphIdentity:
    """Generation-scoped graph identity stored on the schema metadata node."""

    graph_generation: str | None
    identity_contract_version: str | None

    @property
    def is_unset(self) -> bool:
        return self.graph_generation is None or self.identity_contract_version is None

    @property
    def is_legacy(self) -> bool:
        return self.identity_contract_version in {None, LEGACY_IDENTITY_CONTRACT_VERSION}


def new_graph_identity() -> GraphIdentity:
    """Mint the identity persisted by a genuinely fresh graph or rebuild."""
    return GraphIdentity(str(uuid4()), CURRENT_IDENTITY_CONTRACT_VERSION)


# Default embedding width (fastembed BAAI/bge-small-en-v1.5). The vector column is
# fixed-width in Ladybug, so the configured model's ``dimension`` is baked into the
# DDL at bootstrap and recorded on the schema-metadata node; ``verify_embedding_dim``
# refuses to open a graph whose stored width disagrees with the configured one.
DEFAULT_EMBEDDING_DIM = 384

# Sentinel column type for the per-node vector. ``_column_sql`` rewrites it to
# ``DOUBLE[<dim>]`` so the width is parameterized in one place instead of hardcoded.
_EMBEDDING_COLUMN_PLACEHOLDER = "DOUBLE[]"

_COMMON_NODE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "STRING PRIMARY KEY"),
    ("type", "STRING"),
    ("title", "STRING"),
    ("content", "STRING"),
    ("tags", "STRING[]"),
    ("facets", "STRING"),
    ("provenance", "STRING"),
    ("created_at", "TIMESTAMP"),
    ("schema_version", "INT64"),
    # Set only on the schema-metadata node (NULL on data nodes), mirroring how
    # ``schema_version`` is meaningful only there. Records the vector width the
    # graph was built at so the dim-guard can compare it against config.
    ("embedding_dim", "INT64"),
    # These are set only when a new graph is created under the current identity
    # contract. Additive migration creates nullable columns but never assigns an
    # identity to a legacy graph in place.
    ("graph_generation", "STRING"),
    ("identity_contract_version", "STRING"),
    ("embedding", _EMBEDDING_COLUMN_PLACEHOLDER),
)

_COMMON_EDGE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("id", "STRING"),
    ("type", "STRING"),
    ("src", "STRING"),
    ("dst", "STRING"),
    ("weight", "DOUBLE"),
    ("provenance", "STRING"),
    ("created_at", "TIMESTAMP"),
)

# Single source of truth for the schema inventory. Keep table order stable:
# node tables must exist before relation tables that reference them.
KEEP_LIST: dict[str, Mapping[str, object]] = {
    "Node": {
        "kind": "node",
        "columns": _COMMON_NODE_COLUMNS,
    },
    "Edge": {
        "kind": "rel",
        "from": "Node",
        "to": "Node",
        "columns": _COMMON_EDGE_COLUMNS,
    },
    "Authority": {
        "kind": "node",
        "columns": (
            *_COMMON_NODE_COLUMNS,
            ("canonical_name", "STRING"),
            ("variants", "STRING[]"),
        ),
        "vector_index": "authority_embedding_idx",
    },
    "Annotation": {
        "kind": "node",
        "columns": (
            *_COMMON_NODE_COLUMNS,
            ("block_id", "STRING"),
            ("target_id", "STRING"),
            ("byte_start", "INT64"),
            ("byte_end", "INT64"),
            ("surface_form", "STRING"),
        ),
        "vector_index": "annotation_embedding_idx",
    },
    "Reference": {
        "kind": "rel",
        "from": "Document",
        "to": "Document",
        "columns": (
            *_COMMON_EDGE_COLUMNS,
            ("locator", "STRING"),
            ("citation_text", "STRING"),
        ),
    },
    "Work": {
        "kind": "node",
        "columns": (
            *_COMMON_NODE_COLUMNS,
            ("work_uri", "STRING"),
        ),
    },
    "Document": {
        "kind": "node",
        "columns": (
            *_COMMON_NODE_COLUMNS,
            ("uri", "STRING"),
            ("media_type", "STRING"),
            ("byte_length", "INT64"),
            ("sha256", "STRING"),
            ("discovered_at", "TIMESTAMP"),
            ("work_id", "STRING"),
        ),
        "vector_index": "document_embedding_idx",
    },
    "Block": {
        "kind": "node",
        "columns": (
            *_COMMON_NODE_COLUMNS,
            ("document_id", "STRING"),
            ("path", "STRING"),
            ("block_index", "INT64"),
            ("byte_start", "INT64"),
            ("byte_end", "INT64"),
            ("block_kind", "STRING"),
            ("content_hash", "STRING"),
        ),
        "vector_index": "block_embedding_idx",
    },
}

PULSE_ONLY_TABLES: frozenset[str] = frozenset(
    {
        "Decision",
        "Criterion",
        "Constraint",
        "Requirement",
        "Alternative",
        "Spec",
        "Card",
        "Sprint",
        "Ideation",
        "Refinement",
        "Topic",
        "Story",
        "Guideline",
        "BoardMeta",
    }
)


def table_names() -> tuple[str, ...]:
    """Return graph table names exactly as a fresh vault should declare them."""
    return tuple(KEEP_LIST)


def vector_index_names() -> tuple[str, ...]:
    """Return HNSW vector index names derived from ``KEEP_LIST``."""
    return tuple(
        str(config["vector_index"]) for config in KEEP_LIST.values() if "vector_index" in config
    )


def ddl_statements(
    dim: int = DEFAULT_EMBEDDING_DIM,
    *,
    graph_generation: str | None = None,
    identity_contract_version: str | None = None,
) -> tuple[str, ...]:
    """Return idempotent Ladybug/Kuzu DDL statements for the keep-list schema.

    ``dim`` parameterizes the per-node vector column width and is recorded on the
    schema-metadata node so a later open can detect a model/width change.
    """
    statements: list[str] = []
    for table_name, config in _configs_by_kind("node"):
        statements.append(_build_node_ddl(table_name, config["columns"], dim))
    for table_name, config in _configs_by_kind("rel"):
        statements.append(
            _build_rel_ddl(
                table_name,
                str(config["from"]),
                str(config["to"]),
                config["columns"],
                dim,
            )
        )
    statements.extend(("INSTALL VECTOR", "LOAD VECTOR"))
    for table_name, config in KEEP_LIST.items():
        index_name = config.get("vector_index")
        if index_name is not None:
            statements.append(_build_vector_index_ddl(table_name, str(index_name)))
    # Bring a pre-existing graph's tables up to the current column set before any
    # statement references a newly-added column. ``CREATE NODE TABLE IF NOT EXISTS``
    # never alters a table that already exists, so a vault created before
    # ``embedding_dim`` existed keeps tables without it; the metadata MERGE below
    # then references ``m.embedding_dim`` and the binder fails. These idempotent
    # ALTERs add it (the executor swallows "already exists" on a fresh graph).
    statements.extend(_migration_ddl(dim))
    statements.append(
        _schema_metadata_ddl(
            dim,
            graph_generation=graph_generation,
            identity_contract_version=identity_contract_version,
        )
    )
    return tuple(statements)


# Columns introduced after the initial schema shipped. CREATE TABLE IF NOT EXISTS
# cannot add them to an already-created table, so each is ALTER-ADDed idempotently.
_MIGRATION_NODE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("embedding_dim", "INT64"),
    ("graph_generation", "STRING"),
    ("identity_contract_version", "STRING"),
)


def _migration_ddl(dim: int) -> list[str]:
    statements: list[str] = []
    for table_name, _config in _configs_by_kind("node"):
        for col_name, col_type in _MIGRATION_NODE_COLUMNS:
            statements.append(
                f"ALTER TABLE {table_name} ADD {col_name} {_render_column_type(col_type, dim)}"
            )
    return statements


def _configs_by_kind(kind: str) -> Iterator[tuple[str, Mapping[str, object]]]:
    for table_name, config in KEEP_LIST.items():
        if config["kind"] == kind:
            yield table_name, config


def verify_schema_version(connection: Any, *, file_path: Path | str | None = None) -> None:
    """Raise ``SchemaVersionMismatch`` when stored schema metadata is too new.

    Missing metadata is accepted for first bootstrap. Numeric versions lower
    than or equal to ``CURRENT_SCHEMA_VERSION`` are accepted; future versions
    fail loud because 2A ships no automatic migration path.
    """
    stored_version = _read_schema_version(connection)
    if stored_version is None:
        return

    parsed_version = _coerce_version(stored_version)
    if parsed_version is not None and parsed_version <= CURRENT_SCHEMA_VERSION:
        return
    if parsed_version is None and str(stored_version) == str(CURRENT_SCHEMA_VERSION):
        return

    raise SchemaVersionMismatch(
        file_path or _connection_file_path(connection),
        # The stored cell comes back untyped from the driver; the error carries a
        # version scalar, so anything exotic is reported as its text form.
        stored_version if isinstance(stored_version, (int, str)) else str(stored_version),
        CURRENT_SCHEMA_VERSION,
    )


def verify_embedding_dim(
    connection: Any,
    configured_dim: int,
    *,
    file_path: Path | str | None = None,
) -> None:
    """Raise ``EmbeddingDimMismatch`` when the stored vector width disagrees with
    the configured embedding ``dimension``.

    Missing metadata (a graph built before this column existed, or one never
    bootstrapped) is accepted — there is no migration path, and a fresh bootstrap
    records the width going forward. The remedy named in the error is ``kg
    reembed``, which rebuilds the graph at the configured width.
    """
    stored_dim = _read_embedding_dim(connection)
    if stored_dim is None:
        return
    if int(stored_dim) == int(configured_dim):
        return
    raise EmbeddingDimMismatch(
        file_path or _connection_file_path(connection),
        stored_dim=int(stored_dim),
        configured_dim=int(configured_dim),
    )


def read_graph_identity(connection: Any) -> GraphIdentity:
    """Read generation metadata, returning an explicit legacy/unset identity.

    Old graphs may lack either the columns or the metadata values. Both cases are
    represented as ``None`` and are never upgraded by this read path.
    """
    try:
        result = connection.execute(
            "MATCH (m:Node {id: $metadata_id}) "
            "RETURN m.graph_generation AS graph_generation, "
            "m.identity_contract_version AS identity_contract_version",
            {"metadata_id": SCHEMA_METADATA_NODE_ID},
        )
    except Exception:
        return GraphIdentity(None, None)
    rows = tuple(_iter_rows(result))
    row = rows[0] if rows else None
    if isinstance(row, Mapping):
        generation = _optional_metadata_string(row.get("graph_generation"))
        contract = _optional_metadata_string(row.get("identity_contract_version"))
    elif isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        generation = _optional_metadata_string(row[0] if len(row) > 0 else None)
        contract = _optional_metadata_string(row[1] if len(row) > 1 else None)
    else:
        generation = None
        contract = None
    return GraphIdentity(generation, contract)


def read_graph_identity_path(graph_path: Path | str) -> GraphIdentity:
    """Read the identity currently named by ``graph_path`` through a fresh handle."""
    import ladybug

    database = ladybug.Database(Path(graph_path), read_only=True)
    connection = ladybug.Connection(database)
    try:
        return read_graph_identity(connection)
    finally:
        connection.close()
        database.close()


def _read_embedding_dim(connection: Any) -> int | None:
    try:
        result = connection.execute(
            "MATCH (m:Node {id: $metadata_id}) RETURN m.embedding_dim AS embedding_dim",
            {"metadata_id": SCHEMA_METADATA_NODE_ID},
        )
    except Exception:
        # A graph created before the ``embedding_dim`` column existed errors on the
        # unknown property — treat as "no recorded width" and accept.
        return None
    value = _first_scalar(result)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _build_node_ddl(
    table_name: str,
    columns: object,
    dim: int,
) -> str:
    return f"CREATE NODE TABLE IF NOT EXISTS {table_name} ({_column_sql(columns, dim)})"


def _build_rel_ddl(
    table_name: str,
    from_table: str,
    to_table: str,
    columns: object,
    dim: int,
) -> str:
    return (
        f"CREATE REL TABLE IF NOT EXISTS {table_name} "
        f"(FROM {from_table} TO {to_table}, {_column_sql(columns, dim)})"
    )


def _build_vector_index_ddl(table_name: str, index_name: str) -> str:
    return (
        f"CALL CREATE_VECTOR_INDEX('{table_name}', '{index_name}', 'embedding', metric := 'cosine')"
    )


def _schema_metadata_ddl(
    dim: int,
    *,
    graph_generation: str | None,
    identity_contract_version: str | None,
) -> str:
    assignments = [
        "m.type = 'SchemaMetadata'",
        f"m.title = 'Marginalia schema v{CURRENT_SCHEMA_VERSION}'",
        f"m.schema_version = {CURRENT_SCHEMA_VERSION}",
        f"m.embedding_dim = {int(dim)}",
    ]
    if graph_generation is not None:
        _validate_metadata_value("graph_generation", graph_generation)
        assignments.append(f"m.graph_generation = {_cypher_string(graph_generation)}")
    if identity_contract_version is not None:
        _validate_metadata_value("identity_contract_version", identity_contract_version)
        assignments.append(
            f"m.identity_contract_version = {_cypher_string(identity_contract_version)}"
        )
    assignments.append("m.created_at = current_timestamp()")
    return f"MERGE (m:Node {{id: '{SCHEMA_METADATA_NODE_ID}'}}) ON CREATE SET " + ", ".join(
        assignments
    )


def _cypher_string(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


# ``_schema_metadata_ddl`` hand-interpolates ``graph_generation`` and
# ``identity_contract_version`` into a Cypher literal via ``_cypher_string``
# above instead of a bound parameter: the DDL statements this module builds
# are returned as bare strings and executed by callers (``_bootstrap.py`` and
# ``cli/kg.py``) with no params channel, so there is nowhere to bind a
# parameter without changing that shared return contract across both call
# sites. As defense in depth against a bug in the hand-rolled escaper, both
# values are validated against a strict allow-list before interpolation —
# they are always either a ``uuid4()`` string (``graph_generation``) or one
# of the fixed ``*_IDENTITY_CONTRACT_VERSION`` constants above
# (``identity_contract_version``), never arbitrary or user-controlled text.
_SAFE_METADATA_VALUE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _validate_metadata_value(field: str, value: str) -> None:
    if not _SAFE_METADATA_VALUE.fullmatch(value):
        raise ValueError(
            f"unsafe schema metadata value for {field!r}: {value!r} "
            f"(must match {_SAFE_METADATA_VALUE.pattern!r})"
        )


def _optional_metadata_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _column_sql(columns: object, dim: int) -> str:
    typed_columns = _typed_columns(columns)
    return ", ".join(
        f"{name} {_render_column_type(column_type, dim)}" for name, column_type in typed_columns
    )


def _render_column_type(column_type: str, dim: int) -> str:
    """Rewrite the embedding-vector placeholder to the configured width."""
    if column_type == _EMBEDDING_COLUMN_PLACEHOLDER:
        return f"DOUBLE[{int(dim)}]"
    return column_type


def _typed_columns(columns: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(columns, tuple):
        raise TypeError("schema columns must be stored as tuples")
    return tuple((str(name), str(column_type)) for name, column_type in columns)


def _read_schema_version(connection: Any) -> object | None:
    try:
        result = connection.execute(
            "MATCH (m:Node {id: $metadata_id}) RETURN m.schema_version AS schema_version",
            {"metadata_id": SCHEMA_METADATA_NODE_ID},
        )
    except Exception:
        return None
    return _first_scalar(result)


def _first_scalar(result: object) -> object | None:
    for row in _iter_rows(result):
        if isinstance(row, Mapping):
            if "schema_version" in row:
                return row["schema_version"]
            for value in row.values():
                return value
        if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
            return row[0] if row else None
        return row
    return None


@runtime_checkable
class _CursorResult(Protocol):
    """The driver cursor shape ``_iter_rows`` drains.

    Runtime-checkable so the isinstance test stays exactly the old
    ``hasattr(has_next) and hasattr(get_next)`` duck-type check, while the
    branch body is properly typed."""

    def has_next(self) -> bool: ...

    def get_next(self) -> object: ...


def _iter_rows(result: object) -> Iterator[object]:
    if result is None:
        return
    if isinstance(result, _CursorResult):
        while result.has_next():
            yield result.get_next()
        close = getattr(result, "close", None)
        if callable(close):
            close()
        return
    if isinstance(result, Iterable) and not isinstance(result, (str, bytes, bytearray)):
        yield from result
        return
    yield result


def _coerce_version(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _connection_file_path(connection: Any) -> Path:
    database = getattr(connection, "database", None)
    for candidate in (
        getattr(connection, "database_path", None),
        getattr(connection, "path", None),
        getattr(database, "database_path", None),
        getattr(database, "path", None),
    ):
        if candidate:
            return Path(candidate)
    return Path("graph.lbug")


__all__ = [
    "CURRENT_IDENTITY_CONTRACT_VERSION",
    "CURRENT_SCHEMA_VERSION",
    "DEFAULT_EMBEDDING_DIM",
    "DETERMINISTIC_EDGE_IDENTITY_CONTRACTS",
    "LEGACY_IDENTITY_CONTRACT_VERSION",
    "GraphIdentity",
    "KEEP_LIST",
    "PULSE_ONLY_TABLES",
    "SCHEMA_METADATA_NODE_ID",
    "NEO4J_METADATA_GENERATION",
    "ddl_statements",
    "new_graph_identity",
    "read_graph_identity",
    "table_names",
    "vector_index_names",
    "verify_embedding_dim",
    "verify_schema_version",
]
