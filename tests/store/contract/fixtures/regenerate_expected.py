"""Generate the contract-suite fixtures from the PRE-CHANGE retrieval code.

Run ONCE, on a checkout where LadybugStore still owns search_text and
nodes_with_embeddings (before M1 task T2). The outputs, corpus.jsonl and
expected.json, are committed and pin the scoring formula for every backend
and index engine that follows. Regenerating them requires an ADR-level
decision (the internal ADR 0041 plan, decision D-05).

    uv run python tests/store/contract/fixtures/regenerate_expected.py

Outputs were generated on 2026-09-07 from the pre-T2 tree; re-running must
reproduce them byte-for-byte or a decision row is required.
"""

from __future__ import annotations

import json
import random
import tempfile
from pathlib import Path

from okto_neuron.core.schema import Node
from okto_neuron.query import _vector_seeds
from okto_neuron.store.index import InMemoryIndexStore, reindex_all
from okto_neuron.store.ladybug import LadybugStore, VaultConnection
from okto_neuron.store.schema import DEFAULT_EMBEDDING_DIM

HERE = Path(__file__).resolve().parent
CORPUS_PATH = HERE / "corpus.jsonl"
EXPECTED_PATH = HERE / "expected.json"

TYPES = ("Concept", "Claim", "Document", "Block")
WORDS = (
    "graph", "store", "ladybug", "index", "vector", "cosine", "scan", "vault",
    "claim", "block", "document", "parity", "backend", "neo4j", "grafx", "score",
    "snapshot", "migrate", "lease", "health", "staging", "rebuild", "reembed", "port",
)
QUERIES = (
    "graph store",
    "vector index",
    "ladybug vault",
    "claim block document",
    "parity backend",
    "cosine scan score",
    "neo4j grafx",
    "graph graph index",
    "snapshot migrate",
    "lease health staging",
    "rebuild reembed port",
    "tags version label",
)
TYPE_FILTERS = (None, "Claim")
K = 20
NODE_COUNT = 60
NO_EMBEDDING = {5, 14, 23, 32, 41, 50}
EMPTY_CONTENT = {7, 26, 45}
FACET_P_NODE = 11
META_TITLE_NODE = 19


def build_corpus() -> list[dict]:
    rng = random.Random(1234)
    rows: list[dict] = []
    for i in range(NODE_COUNT):
        title = f"{WORDS[i % 8]} {WORDS[(i * 3) % len(WORDS)]}"
        if i % 6 == 0:
            title = "graph store"  # duplicated titles force BM25 score ties
        if i == META_TITLE_NODE:
            title = "tags: release notes"  # node_quality_weight metadata-title path
        content = ""
        if i not in EMPTY_CONTENT:
            content = " ".join(rng.choice(WORDS) for _ in range(rng.randint(2, 12)))
        tags = [WORDS[(i + 1) % len(WORDS)]] if i % 2 else []
        embedding = None
        if i not in NO_EMBEDDING:
            embedding = [round(rng.uniform(-1.0, 1.0), 6) for _ in range(DEFAULT_EMBEDDING_DIM)]
        facets = {"P": "person-x"} if i == FACET_P_NODE else {}
        rows.append(
            {
                "id": f"c{i:03d}",
                "type": TYPES[i % 4],
                "title": title,
                "content": content,
                "tags": tags,
                "facets": facets,
                "embedding": embedding,
            }
        )
    return rows


def build_query_embeddings() -> list[list[float]]:
    rng = random.Random(4321)
    return [
        [round(rng.uniform(-1.0, 1.0), 6) for _ in range(DEFAULT_EMBEDDING_DIM)]
        for _ in QUERIES
    ]


def main() -> None:
    rows = build_corpus()
    query_embeddings = build_query_embeddings()
    with tempfile.TemporaryDirectory() as tmp:
        store = LadybugStore(Path(tmp) / "vault")
        try:
            for row in rows:
                store.add_node(Node(**row))
            store.checkpoint()
            index = InMemoryIndexStore()
            reindex_all(store, index)
            cases = []
            for query, query_embedding in zip(QUERIES, query_embeddings):
                for node_type in TYPE_FILTERS:
                    lexical = [
                        [node_id, score]
                        for node_id, score in index.search_text(query, k=K, type=node_type)
                    ]
                    vector_all = _vector_seeds(query_embedding, index, store, node_type)
                    vector = [[node.id, score] for node, score in vector_all[:K]]
                    cases.append(
                        {
                            "query": query,
                            "type": node_type,
                            "lexical_top20": lexical,
                            "vector_top20": vector,
                            "vector_total": len(vector_all),
                        }
                    )
        finally:
            store.close()
            VaultConnection.close_all()

    with CORPUS_PATH.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False))
            handle.write("\n")
    expected = {
        "format_version": 1,
        "k": K,
        "embedding_dim": DEFAULT_EMBEDDING_DIM,
        "queries": list(QUERIES),
        "query_embeddings": query_embeddings,
        "cases": cases,
    }
    EXPECTED_PATH.write_text(json.dumps(expected, separators=(",", ":")) + "\n", encoding="utf-8")
    embedded = sum(1 for row in rows if row["embedding"] is not None)
    print(f"corpus: {len(rows)} nodes, {embedded} embedded -> {CORPUS_PATH}")
    print(f"expected: {len(cases)} cases -> {EXPECTED_PATH}")


if __name__ == "__main__":
    main()
