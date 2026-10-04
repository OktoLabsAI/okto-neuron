"""Logical graph snapshot: dump / verify / load.

Implements the M2a slice of the pluggable-graph-backend plan
(the internal ADR 0041 plan, decisions D-29, D-30, D-31): a
backend-agnostic, on-disk snapshot format for a :class:`~okto_neuron.store.protocol.GraphStore`
so a vault's durable knowledge can be dumped, verified, and reloaded into a
fresh store of any kind without depending on a specific backend's on-disk
representation.

This module is generic over :class:`~okto_neuron.store.protocol.GraphStore` and
never imports Ladybug: it only calls the five protocol methods
(``list_nodes``, ``list_edges``, ``add_node``, ``add_edge``, ``checkpoint``)
that every backend implements. Callers (the ``kg snapshot`` CLI in
``cli/kg.py``) are responsible for backend-specific bootstrap, the
single-writer handle lease, and reading ``okto-neuron.yaml``.

Format (``format_version`` 1), written under ``<dest>/``::

    manifest.json       origin metadata, counts, content_sha256 — written last
    schema.json         schema_version, embedding_dim, closed/internal node types
    nodes.jsonl         one Node per line (no embedding key), ascending id
    edges.jsonl         one Edge per line, ascending id
    embeddings.jsonl    {id, embedding} for every embedded node, ascending id
    sources/            copy of <vault>/.marginalia/sources/ (D-31); may be empty
    CHECKSUMS.sha256    "<sha256hex>  <relative path>" per tracked file

Source-path facet rewrite (D-31 follow-on): ingest stamps ``Block``/``Document``
facets and the redundant top-level ``Claim`` facet with an *absolute*
``source_path`` under ``<origin vault>/.marginalia/sources/...`` (read back
as-is by ``vault.py::_provenance_for_node``'s legacy branch, only accepted
when it resolves under the currently open vault's root). Claims are also
guarded by a *vault-relative* ``facets["source_span"]["source_path"]`` that
self-heals on relocation and needs no rewrite. Because the absolute form does
not self-heal, ``load`` rewrites ``facets["source_path"]`` whenever it
contains the recorded ``origin_sources_prefix`` anchor, replacing everything
up to and including that anchor with the caller-supplied ``sources_dest`` —
otherwise every Block/Document hit (and any Claim relying on its own direct
``source_path``) silently loses source-block grounding after a load into a
different absolute path.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from okto_neuron.core.schema import Edge, Node
from okto_neuron.store.closed_set import CLOSED_NODE_TYPES, INTERNAL_NODE_TYPES
from okto_neuron.store.protocol import GraphStore
from okto_neuron.store.schema import SCHEMA_METADATA_NODE_ID

#: Relative form of the vault's source-blob directory, recorded on every
#: manifest and used as the rewrite anchor by :func:`load` (see module
#: docstring). Fixed by convention (``<vault>/.marginalia/sources/``), not
#: derived per-dump.
ORIGIN_SOURCES_PREFIX = ".marginalia/sources"

_JSON_LINE_KWARGS: dict[str, Any] = {
    "sort_keys": True,
    "ensure_ascii": False,
    "separators": (",", ":"),
}


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    """``manifest.json`` contents (spec section 2)."""

    format_version: int
    marginalia_version: str
    schema_version: int
    vault_id: str
    origin_backend: str
    origin_graph_generation: str | None
    origin_identity_contract_version: str | None
    origin_sources_prefix: str
    embedding: dict[str, Any]
    packs: list[str]
    node_count: int
    edge_count: int
    embedded_count: int
    source_file_count: int
    created_at: str
    content_sha256: str


@dataclass(frozen=True, slots=True)
class VerifyReport:
    """Result of :func:`verify`: ``problems`` is empty exactly when ``ok``."""

    ok: bool
    manifest: SnapshotManifest | None
    problems: list[str]
    node_count: int
    edge_count: int
    embedded_count: int


@dataclass(frozen=True, slots=True)
class LoadReport:
    """Result of :func:`load`."""

    nodes_written: int
    edges_written: int
    embeddings_applied: int
    sources_copied: int
    skipped_embeddings: bool


def _iso_utc(value: datetime) -> str:
    """Render ``value`` as a UTC ISO-8601 string, treating naive input as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def _dump_json_line(obj: dict[str, Any]) -> str:
    return json.dumps(obj, **_JSON_LINE_KWARGS) + "\n"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _count_lines(path: Path) -> int:
    count = 0
    with path.open("rb") as fh:
        for _ in fh:
            count += 1
    return count


def _node_row(node: Node) -> dict[str, Any]:
    return {
        "id": node.id,
        "type": node.type,
        "title": node.title,
        "content": node.content,
        "tags": node.tags,
        "facets": node.facets,
        "provenance": node.provenance.model_dump(),
        "created_at": _iso_utc(node.created_at),
    }


def _edge_row(edge: Edge) -> dict[str, Any]:
    return {
        "id": edge.id,
        "type": edge.type,
        "src": edge.src,
        "dst": edge.dst,
        "weight": edge.weight,
        "provenance": edge.provenance.model_dump(),
    }


def dump(
    store: GraphStore,
    dest: Path,
    *,
    vault_id: str,
    origin_backend: str,
    origin_identity: tuple[str | None, str | None],
    embedding: dict[str, Any],
    packs: list[str],
    sources_dir: Path | None,
    marginalia_version: str,
    schema_version: int,
    embedding_dim: int,
) -> SnapshotManifest:
    """Dump ``store`` into a fresh snapshot directory at ``dest``.

    ``origin_identity`` is ``(graph_generation, identity_contract_version)``,
    the two fields of ``store.schema.GraphIdentity`` in that order.
    ``embedding`` carries ``{"provider", "model", "dimension"}`` read from the
    origin ``okto-neuron.yaml``; its ``dimension`` must equal ``embedding_dim``
    (the graph's own stored width) or the dump is refused. ``dest`` must not
    exist, or must be an empty directory. Everything is staged under
    ``<dest>.partial`` and atomically renamed into place last, with
    ``manifest.json`` written after every other file so a torn dump never has
    one.
    """
    configured_dim = embedding.get("dimension")
    if configured_dim != embedding_dim:
        raise ValueError(
            f"embedding dimension mismatch: okto-neuron.yaml says {configured_dim!r}, "
            f"the graph's stored width is {embedding_dim!r}"
        )
    for key in ("provider", "model", "dimension"):
        if key not in embedding:
            raise ValueError(f"embedding dict missing required key: {key!r}")

    if dest.exists():
        if not dest.is_dir():
            raise ValueError(f"dump destination exists and is not a directory: {dest}")
        if any(dest.iterdir()):
            raise ValueError(f"dump destination exists and is not empty: {dest}")

    partial = dest.parent / f"{dest.name}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir(parents=True)

    # M2c (D-39): pin one consistent read point for the whole node+edge scan
    # when the backend supports it (see protocol.py's module docstring and
    # LadybugStore.snapshot()), so a concurrent rebuild's swap() can never
    # produce a manifest describing a state that existed at no single
    # instant. A backend without snapshot() falls back to whatever
    # consistency its caller already guarantees (D-30: today that is the
    # offline vault handle lease every `kg snapshot dump` caller holds).
    snapshot_cm = store.snapshot() if hasattr(store, "snapshot") else nullcontext(None)
    with snapshot_cm as pinned_generation:
        nodes = sorted(
            (node for node in store.list_nodes(include_embedding=True) if node.id != SCHEMA_METADATA_NODE_ID),
            key=lambda node: node.id,
        )
        embedded_count = 0
        nodes_path = partial / "nodes.jsonl"
        embeddings_path = partial / "embeddings.jsonl"
        with nodes_path.open("w", encoding="utf-8") as nodes_fh, embeddings_path.open(
            "w", encoding="utf-8"
        ) as embeddings_fh:
            for node in nodes:
                nodes_fh.write(_dump_json_line(_node_row(node)))
                if node.embedding is not None:
                    embeddings_fh.write(
                        _dump_json_line({"id": node.id, "embedding": node.embedding})
                    )
                    embedded_count += 1
        node_count = len(nodes)

        edges = sorted(store.list_edges(), key=lambda edge: edge.id)
        edges_path = partial / "edges.jsonl"
        with edges_path.open("w", encoding="utf-8") as edges_fh:
            for edge in edges:
                edges_fh.write(_dump_json_line(_edge_row(edge)))
        edge_count = len(edges)

    if pinned_generation:
        origin_identity = (pinned_generation, origin_identity[1])

    sources_root = partial / "sources"
    sources_root.mkdir()
    if sources_dir is not None and sources_dir.is_dir():
        shutil.copytree(sources_dir, sources_root, dirs_exist_ok=True)
    source_file_count = sum(1 for path in sources_root.rglob("*") if path.is_file())

    schema_path = partial / "schema.json"
    schema_doc = {
        "schema_version": schema_version,
        "embedding_dim": embedding_dim,
        "closed_node_types": sorted(CLOSED_NODE_TYPES),
        "internal_node_types": sorted(INTERNAL_NODE_TYPES),
    }
    schema_path.write_text(
        json.dumps(schema_doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    checksum_entries: list[tuple[str, Path]] = [
        ("schema.json", schema_path),
        ("nodes.jsonl", nodes_path),
        ("edges.jsonl", edges_path),
        ("embeddings.jsonl", embeddings_path),
    ]
    source_files = sorted(
        str(path.relative_to(partial)) for path in sources_root.rglob("*") if path.is_file()
    )
    checksum_entries.extend((rel, partial / rel) for rel in source_files)
    checksums_path = partial / "CHECKSUMS.sha256"
    with checksums_path.open("w", encoding="utf-8") as checksums_fh:
        for rel, path in checksum_entries:
            checksums_fh.write(f"{_sha256_file(path)}  {rel}\n")

    content_digest = hashlib.sha256()
    content_digest.update(nodes_path.read_bytes())
    content_digest.update(edges_path.read_bytes())
    content_digest.update(embeddings_path.read_bytes())

    manifest = SnapshotManifest(
        format_version=1,
        marginalia_version=marginalia_version,
        schema_version=schema_version,
        vault_id=vault_id,
        origin_backend=origin_backend,
        origin_graph_generation=origin_identity[0],
        origin_identity_contract_version=origin_identity[1],
        origin_sources_prefix=ORIGIN_SOURCES_PREFIX,
        embedding={
            "provider": embedding["provider"],
            "model": embedding["model"],
            "dimension": embedding["dimension"],
        },
        packs=list(packs),
        node_count=node_count,
        edge_count=edge_count,
        embedded_count=embedded_count,
        source_file_count=source_file_count,
        created_at=_iso_utc(datetime.now(timezone.utc)),
        content_sha256=content_digest.hexdigest(),
    )
    (partial / "manifest.json").write_text(
        json.dumps(asdict(manifest), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    os.replace(partial, dest)
    return manifest


def read_manifest(src: Path) -> SnapshotManifest:
    """Parse ``<src>/manifest.json`` into a :class:`SnapshotManifest`."""
    data = json.loads((src / "manifest.json").read_text(encoding="utf-8"))
    return SnapshotManifest(**data)


def verify(src: Path) -> VerifyReport:
    """Recompute every checksum and count and compare them against the manifest."""
    problems: list[str] = []
    try:
        manifest = read_manifest(src)
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError, TypeError) as exc:
        return VerifyReport(
            ok=False,
            manifest=None,
            problems=[f"failed to read manifest.json: {exc}"],
            node_count=0,
            edge_count=0,
            embedded_count=0,
        )

    checksums_path = src / "CHECKSUMS.sha256"
    try:
        checksum_lines = checksums_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        problems.append("CHECKSUMS.sha256 missing")
        checksum_lines = []

    for line in checksum_lines:
        if not line.strip():
            continue
        try:
            expected_hex, rel_path = line.split("  ", 1)
        except ValueError:
            problems.append(f"malformed CHECKSUMS.sha256 line: {line!r}")
            continue
        file_path = src / rel_path
        if not file_path.is_file():
            problems.append(f"missing file listed in CHECKSUMS.sha256: {rel_path}")
            continue
        actual_hex = _sha256_file(file_path)
        if actual_hex != expected_hex:
            problems.append(f"checksum mismatch for {rel_path}")

    nodes_path = src / "nodes.jsonl"
    edges_path = src / "edges.jsonl"
    embeddings_path = src / "embeddings.jsonl"

    node_count = edge_count = embedded_count = 0
    for path, label in (
        (nodes_path, "nodes.jsonl"),
        (edges_path, "edges.jsonl"),
        (embeddings_path, "embeddings.jsonl"),
    ):
        if not path.is_file():
            problems.append(f"{label} missing")

    if nodes_path.is_file():
        node_count = _count_lines(nodes_path)
        if node_count != manifest.node_count:
            problems.append(
                f"node_count mismatch: manifest={manifest.node_count} actual={node_count}"
            )
    if edges_path.is_file():
        edge_count = _count_lines(edges_path)
        if edge_count != manifest.edge_count:
            problems.append(
                f"edge_count mismatch: manifest={manifest.edge_count} actual={edge_count}"
            )
    if embeddings_path.is_file():
        embedded_count = _count_lines(embeddings_path)
        if embedded_count != manifest.embedded_count:
            problems.append(
                f"embedded_count mismatch: manifest={manifest.embedded_count} "
                f"actual={embedded_count}"
            )

    if nodes_path.is_file() and edges_path.is_file() and embeddings_path.is_file():
        content_digest = hashlib.sha256()
        content_digest.update(nodes_path.read_bytes())
        content_digest.update(edges_path.read_bytes())
        content_digest.update(embeddings_path.read_bytes())
        if content_digest.hexdigest() != manifest.content_sha256:
            problems.append("content_sha256 mismatch")

    return VerifyReport(
        ok=not problems,
        manifest=manifest,
        problems=problems,
        node_count=node_count,
        edge_count=edge_count,
        embedded_count=embedded_count,
    )


def _rewrite_source_path_facet(
    facets: dict[str, Any], anchor: str, new_prefix: Path
) -> dict[str, Any]:
    """Rewrite an absolute ``facets["source_path"]`` onto ``new_prefix``.

    ``anchor`` is the relative form recorded as ``origin_sources_prefix``
    (e.g. ``.marginalia/sources``); the origin vault's absolute root is never
    known here (and is never written to the manifest), so the rewrite locates
    the anchor as a substring of the stored absolute path and replaces
    everything up to and including it with ``new_prefix``. Leaves ``facets``
    untouched when the key is absent, not a string, or does not contain the
    anchor (e.g. a value already relocated, or unrelated to source blocks).
    """
    value = facets.get("source_path")
    if not isinstance(value, str):
        return facets
    idx = value.find(anchor)
    if idx == -1:
        return facets
    remainder = value[idx + len(anchor) :]
    rewritten = dict(facets)
    rewritten["source_path"] = str(new_prefix) + remainder
    return rewritten


def load(
    store: GraphStore,
    src: Path,
    *,
    skip_embeddings: bool = False,
    sources_dest: Path | None = None,
) -> LoadReport:
    """Verify the snapshot at ``src`` and replay it onto ``store``.

    Refuses (raising ``ValueError``) if verification finds any problem.
    Otherwise applies every node then every edge in file (ascending-id) order,
    checkpoints, and — when ``sources_dest`` is given and ``src/sources``
    exists — copies the source blobs into it (merging via
    ``dirs_exist_ok=True``) and rewrites each node's absolute
    ``facets["source_path"]`` onto that new location (see module docstring).
    Idempotent: replaying the same snapshot onto the same store again yields
    the same graph, because ``add_node``/``add_edge`` are upserts with pinned
    ``created_at`` and immutable edge identity, and the facet rewrite is a
    pure function of the unchanged snapshot content.
    """
    report = verify(src)
    if not report.ok:
        raise ValueError(
            f"refusing to load snapshot at {src}: " + "; ".join(report.problems)
        )
    manifest = report.manifest
    assert manifest is not None  # guaranteed by verify() when ok

    embeddings: dict[str, list[float]] = {}
    if not skip_embeddings:
        with (src / "embeddings.jsonl").open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                embeddings[row["id"]] = row["embedding"]

    anchor = manifest.origin_sources_prefix
    nodes_written = 0
    embeddings_applied = 0
    with (src / "nodes.jsonl").open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if sources_dest is not None:
                facets = row.get("facets")
                if isinstance(facets, dict):
                    row["facets"] = _rewrite_source_path_facet(facets, anchor, sources_dest)
            embedding = None if skip_embeddings else embeddings.get(row["id"])
            if embedding is not None:
                embeddings_applied += 1
            node = Node(**row, embedding=embedding)
            store.add_node(node)
            nodes_written += 1

    edges_written = 0
    with (src / "edges.jsonl").open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            edge = Edge(**row)
            try:
                store.add_edge(edge)
            except ValueError as exc:
                raise ValueError(
                    f"edge {edge.id} ({edge.src} -> {edge.dst}) failed to load: {exc}"
                ) from exc
            edges_written += 1

    store.checkpoint()

    sources_copied = 0
    src_sources = src / "sources"
    if sources_dest is not None and src_sources.is_dir():
        shutil.copytree(src_sources, sources_dest, dirs_exist_ok=True)
        sources_copied = sum(1 for path in src_sources.rglob("*") if path.is_file())

    return LoadReport(
        nodes_written=nodes_written,
        edges_written=edges_written,
        embeddings_applied=embeddings_applied,
        sources_copied=sources_copied,
        skipped_embeddings=skip_embeddings,
    )
