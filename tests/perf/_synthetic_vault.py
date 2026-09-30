"""Synthetic real-scale vault for the daemon responsiveness load test (#13/#18).

Shape (defaults, all synthetic content): 4,000 nodes (300 Document, 600 Block,
600 entity nodes, 2,500 Claim), 13,000 edges, and 2,000 review-queue items whose
evidence points at synthetic Blocks. Predicates come from a fixed 60-label
vocabulary so the upkeep predicate scan has real work to do.

Build path. ``GraphStore.add_edge`` is one read-check + one write transaction
per edge (measured ~110 ms/edge on grafx 0.0.7, 24 minutes for this shape), and
``kg snapshot load`` replays through the same per-edge ``add_edge``. On grafx the
generator therefore writes through the store's own MERGE/CREATE statements and
parameter builders, ``_BATCH`` statements per write transaction, and keeps the
node index in sync exactly like ``IndexedStore.add_node`` does. Any other backend
falls back to the public per-item API.

Caching. The built vault is cached under ``$OKTO_NEURON_PERF_CACHE`` (default:
``<system temp>/okto-neuron-perf``) keyed by ``FIXTURE_VERSION`` and backend, and
copied for each run, so it is built once per machine (or per CI cache key).
Regenerate by deleting the cache directory or bumping ``FIXTURE_VERSION``::

    uv run python -m tests.perf._synthetic_vault /tmp/synthetic-vault --backend grafx
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIXTURE_VERSION = "1"
_BATCH = 500
_SEED = 13


@dataclass(frozen=True)
class Shape:
    documents: int = 300
    blocks: int = 600
    entities: int = 600
    claims: int = 2500
    related_edges: int = 4900
    review_items: int = 2000

    @property
    def nodes(self) -> int:
        return self.documents + self.blocks + self.entities + self.claims

    @property
    def edges(self) -> int:
        # block->document, claim->block, claim->subject, claim->object, related
        return self.blocks + 3 * self.claims + self.related_edges


FULL = Shape()


def scaled(fraction: float) -> Shape:
    """``FULL`` scaled down (smoke runs of the harness itself, never a result)."""
    if fraction >= 1.0:
        return FULL
    return Shape(
        **{
            name: max(2, int(getattr(FULL, name) * fraction))
            for name in Shape.__dataclass_fields__
        }
    )
_ENTITY_TYPES = ("Concept", "Agent", "Place", "Activity")
_PREDICATES = tuple(f"synthetic_relation_{index:02d}" for index in range(60))


def _graph_parts(vault_path: Path, shape: Shape) -> tuple[list[Any], list[Any]]:
    from okto_neuron.core.schema import Edge, Node

    rng = random.Random(_SEED)
    sources = vault_path / ".marginalia" / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    nodes: list[Any] = []
    edges: list[Any] = []

    for index in range(shape.documents):
        source = sources / f"synthetic-{index:04d}.md"
        source.write_text(f"# Synthetic document {index}\n\nfiller text {index}\n", "utf-8")
        nodes.append(
            Node(
                id=f"doc:{index:04d}",
                type="Document",
                title=f"Synthetic document {index}",
                facets={"path": str(source)},
            )
        )
    for index in range(shape.blocks):
        document = index % shape.documents
        nodes.append(
            Node(
                id=f"block:{index:04d}",
                type="Block",
                title=f"Block {index}",
                content=f"Synthetic block {index} about topic {index % 97}.",
                facets={
                    "source_path": str(sources / f"synthetic-{document:04d}.md"),
                    "document_id": f"doc:{document:04d}",
                    "byte_start": 0,
                    "byte_end": 32,
                },
            )
        )
        edges.append(
            Edge(id=f"e:bd:{index:05d}", type="part_of", src=f"block:{index:04d}", dst=f"doc:{document:04d}")
        )
    for index in range(shape.entities):
        nodes.append(
            Node(
                id=f"entity:{index:04d}",
                type=_ENTITY_TYPES[index % len(_ENTITY_TYPES)],
                title=f"Synthetic entity {index}",
            )
        )
    for index in range(shape.claims):
        subject = rng.randrange(shape.entities)
        obj = rng.randrange(shape.entities)
        block = rng.randrange(shape.blocks)
        predicate = _PREDICATES[rng.randrange(len(_PREDICATES))]
        claim_id = f"claim:{index:05d}"
        nodes.append(
            Node(
                id=claim_id,
                type="Claim",
                title=f"entity {subject} {predicate} entity {obj}",
                facets={
                    "S_id": f"entity:{subject:04d}",
                    "P": predicate,
                    "O_id": f"entity:{obj:04d}",
                    "block_id": f"block:{block:04d}",
                    "confidence": round(rng.uniform(0.5, 1.0), 3),
                },
            )
        )
        edges.append(
            Edge(id=f"e:cb:{index:05d}", type="prov:wasDerivedFrom", src=claim_id, dst=f"block:{block:04d}")
        )
        edges.append(
            Edge(id=f"e:cs:{index:05d}", type="rdf:subject", src=claim_id, dst=f"entity:{subject:04d}")
        )
        edges.append(
            Edge(id=f"e:co:{index:05d}", type="rdf:object", src=claim_id, dst=f"entity:{obj:04d}")
        )
    for index in range(shape.related_edges):
        src = rng.randrange(shape.entities)
        dst = (src + 1 + rng.randrange(shape.entities - 1)) % shape.entities
        edges.append(
            Edge(
                id=f"e:rel:{index:05d}",
                type=_PREDICATES[index % len(_PREDICATES)],
                src=f"entity:{src:04d}",
                dst=f"entity:{dst:04d}",
            )
        )
    return nodes, edges


def _write_grafx_batched(store: Any, nodes: list[Any], edges: list[Any]) -> None:
    """Many MERGE/CREATE statements per grafx write transaction (see module doc)."""
    from okto_neuron.store import grafx as grafx_store

    graph = getattr(store, "graph", store)
    index = getattr(store, "index", None)

    def _run(statements: list[tuple[str, dict[str, object]]]) -> None:
        for start in range(0, len(statements), _BATCH):
            txn = graph._db.begin("write")
            try:
                for statement, params in statements[start : start + _BATCH]:
                    txn.execute(statement, params)
                txn.commit()
            except Exception:
                if txn.active:
                    txn.rollback()
                raise

    _run(
        [
            (grafx_store._NODE_MERGE_SQL, graph._node_write_params(node, created_at=node.created_at))
            for node in nodes
        ]
    )
    if index is not None:
        for node in nodes:
            index.upsert(node)
    _run([(grafx_store._EDGE_CREATE_SQL, graph._edge_create_params(edge)) for edge in edges])
    store.checkpoint()


def _write_review_queue(vault_path: Path, shape: Shape) -> None:
    from okto_neuron.consolidate import NodeCandidate
    from okto_neuron.consolidate.review_queue import ReviewQueue, _NodeEntry

    queue = ReviewQueue(vault_path / ".marginalia", store=None)  # type: ignore[arg-type]
    entries: dict[str, Any] = {}
    for index in range(shape.review_items):
        candidate = NodeCandidate(
            type=_ENTITY_TYPES[index % len(_ENTITY_TYPES)],
            title=f"Synthetic review candidate {index}",
            facets={"block_id": f"block:{index % shape.blocks:04d}"},
        )
        entries[candidate.candidate_id] = _NodeEntry(
            candidate=candidate,
            reason="low_confidence" if index % 3 else "contradiction",
            correlations=(),
        )
    queue._save(entries)


def build_synthetic_vault(path: Path, *, backend: str = "grafx", shape: Shape = FULL) -> Path:
    """Create a fresh synthetic vault at ``path`` and return it."""
    from okto_neuron import Vault

    vault = Vault.init(path, embedder="stub", backend=backend)
    try:
        nodes, edges = _graph_parts(Path(vault.path), shape)
        graph = getattr(vault.store, "graph", vault.store)
        if backend == "grafx" and hasattr(graph, "_db"):
            _write_grafx_batched(vault.store, nodes, edges)
        else:
            for node in nodes:
                vault.store.add_node(node)
            for edge in edges:
                vault.store.add_edge(edge)
            vault.store.checkpoint()
        _write_review_queue(Path(vault.path), shape)
        from okto_neuron.predicates import PredicateRegistry

        PredicateRegistry(vault.path).seed_builtins()
    finally:
        vault.close()
    return Path(path)


def cache_root() -> Path:
    raw = os.environ.get("OKTO_NEURON_PERF_CACHE")
    return Path(raw) if raw else Path(tempfile.gettempdir()) / "okto-neuron-perf"


def cached_synthetic_vault(
    dest: Path, *, backend: str = "grafx", shape: Shape = FULL
) -> tuple[Path, float | None]:
    """Copy the cached fixture to ``dest``, building it first when missing.

    Returns the copied vault path and the build time in seconds (``None`` when
    the cache was reused)."""
    suffix = "" if shape == FULL else f"-{shape.nodes}n"
    cached = cache_root() / f"synthetic-v{FIXTURE_VERSION}-{backend}{suffix}"
    built_s: float | None = None
    if not (cached / ".complete").exists():
        shutil.rmtree(cached, ignore_errors=True)
        staging = cached.with_name(cached.name + ".building")
        shutil.rmtree(staging, ignore_errors=True)
        started = time.perf_counter()
        build_synthetic_vault(staging, backend=backend, shape=shape)
        built_s = time.perf_counter() - started
        (staging / ".complete").write_text(f"{built_s:.1f}\n", encoding="utf-8")
        staging.rename(cached)
    shutil.copytree(cached, dest, symlinks=True)
    (dest / ".complete").unlink(missing_ok=True)
    return dest, built_s


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dest", type=Path)
    parser.add_argument("--backend", default="grafx", choices=["grafx", "ladybug"])
    args = parser.parse_args()
    started = time.perf_counter()
    build_synthetic_vault(args.dest, backend=args.backend)
    print(f"built {FULL.nodes} nodes / {FULL.edges} edges in {time.perf_counter() - started:.1f}s")


if __name__ == "__main__":
    main()
