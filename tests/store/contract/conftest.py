"""Contract suite fixtures: one pinned corpus, two graph stores, two index engines."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from okto_neuron.core.schema import Node
from okto_neuron.store.index import DefaultIndexStore, InMemoryIndexStore, reindex_all
from okto_neuron.store.ladybug import LadybugStore, VaultConnection
from okto_neuron.store.memory import InMemoryStore
from okto_neuron.store.registry import NoSuchBackendError, resolve_graph_backend

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(autouse=True)
def _close_vault_connections():
    yield
    VaultConnection.close_all()


@pytest.fixture(scope="session")
def corpus_rows() -> list[dict]:
    text = (FIXTURES / "corpus.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@pytest.fixture
def corpus_nodes(corpus_rows) -> list[Node]:
    return [Node(**row) for row in corpus_rows]


@pytest.fixture(scope="session")
def expected() -> dict:
    return json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))


@pytest.fixture(params=["ladybug", "memory", "stub", "grafx", "neo4j"])
def graph_store(request, tmp_path: Path):
    if request.param == "ladybug":
        store = LadybugStore(tmp_path / "vault")
    elif request.param == "memory":
        store = InMemoryStore()
    elif request.param == "grafx":
        # Optional backend behind the `[grafx]` extra (M4 spec §4 bullet 1) —
        # skip cleanly, mirroring the `stub` branch's own optional-dependency
        # handling, rather than making okto-grafx a hard dependency of the
        # contract suite. Imported lazily (not at module top) so collection
        # never fails when the extra isn't installed.
        pytest.importorskip("okto_grafx")
        from okto_neuron.store.grafx import GrafxStore

        store = GrafxStore(tmp_path / "vault")
    elif request.param == "neo4j":
        # Requires a real Neo4j server and no filesystem graph of its own
        # (M5 spec) -- skip cleanly when neither the driver nor a test
        # server URI is available, matching the `grafx`/`stub` branches'
        # own optional-dependency handling.
        import os

        pytest.importorskip("neo4j")
        uri = os.environ.get("OKTO_NEURON_TEST_NEO4J_URI")
        if not uri:
            pytest.skip("OKTO_NEURON_TEST_NEO4J_URI is not set")
        credential_env = os.environ.get("OKTO_NEURON_TEST_NEO4J_CREDENTIAL_ENV")

        class _Neo4jTestConfig:
            backend = "neo4j"
            database = "neo4j"

            def __init__(self) -> None:
                self.uri = uri
                self.credential_env = credential_env
                self.allow_remote = False

        from okto_neuron.store.neo4j import Neo4jStore

        store = Neo4jStore(tmp_path / "vault", config=_Neo4jTestConfig())
    else:
        # Resolved through the real marginalia.graph_backends entry point
        # (tests/fixtures/stub_backend_pkg), never imported in-tree — skip
        # cleanly if the dev-group stub package isn't installed rather than
        # fabricating a substitute (M3 spec §4/§2.11).
        try:
            store_cls = resolve_graph_backend("stub")
        except NoSuchBackendError:
            pytest.skip("stub_backend_pkg is not installed (dev-group only)")
        store = store_cls()
    yield store
    store.close()


@pytest.fixture(params=["default", "memory"])
def index_factory(request, tmp_path: Path):
    def _make():
        if request.param == "default":
            return DefaultIndexStore(tmp_path / "vault")
        return InMemoryIndexStore()

    return _make


@pytest.fixture
def populated(graph_store, corpus_nodes):
    for node in corpus_nodes:
        graph_store.add_node(node)
    graph_store.checkpoint()
    return graph_store


@pytest.fixture
def populated_index(populated, index_factory):
    index = index_factory()
    reindex_all(populated, index)
    return index
