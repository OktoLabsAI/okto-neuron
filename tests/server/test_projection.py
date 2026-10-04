"""The maintained per-vault projection behind graph/stats and upkeep/predicates (issue #12).

Structural assertions (scan counts, statuses, thread names, flags), not timings: the host these
run on is shared and timing assertions would only measure its load.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from okto_neuron import Vault
from okto_neuron._internal.infra import is_infra
from okto_neuron.cli import app as cli_app
from okto_neuron.core.schema import Edge, Node
from okto_neuron.predicates import build_predicate_stats
from okto_neuron.server import _projection
from okto_neuron.server._projection import (
    SIDECAR_RELATIVE,
    SIDECAR_VERSION,
    ProjectionManager,
    compute_graph_stats,
    is_structural_claim,
)
from okto_neuron.server._store_io import configure_executors, shutdown_executors
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests
from okto_neuron.store.closed_set import CLOSED_NODE_TYPES
from okto_neuron.store.index import InMemoryIndexStore
from okto_neuron.store.index.indexed import IndexedStore
from okto_neuron.store.memory import InMemoryStore
from tests._settled import get_settled


@pytest.fixture(autouse=True)
def _executors():
    configure_executors(4, 2)
    _projection.reset_for_tests()
    yield
    _projection.reset_for_tests()
    shutdown_executors(wait=True)


def _store() -> IndexedStore:
    store = IndexedStore(InMemoryStore(), InMemoryIndexStore())
    for node_id in ("alice", "bob", "carol"):
        store.add_node(Node(id=node_id, type="Agent", title=node_id))
    store.add_edge(Edge(id="e1", type="alpha", src="alice", dst="bob"))
    store.add_node(
        Node(id="c1", type="Claim", title="t", facets={"S_id": "alice", "O_id": "bob", "P": "beta"})
    )
    store.add_node(
        Node(id="c2", type="Claim", title="h", facets={"P": "has_heading", "S_id": "alice"})
    )
    return store


def _state(tmp_path: Path, store: IndexedStore) -> Any:
    vault = SimpleNamespace(store=store)
    return SimpleNamespace(
        vault_path=tmp_path, vault=vault, vault_pool=SimpleNamespace(peek=lambda path: vault)
    )


def _manager(tmp_path: Path, **kwargs: Any) -> ProjectionManager:
    kwargs.setdefault("min_interval_s", 0.0)
    return ProjectionManager(tmp_path, **kwargs)


async def _settle(manager: ProjectionManager, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while manager.rebuilding:
        assert time.monotonic() < deadline, "rebuild never finished"
        await asyncio.sleep(0.01)


def _reference_graph_stats(store: Any, include_structural: bool) -> dict[str, Any]:
    """The pre-projection ``_graph_stats_payload`` logic, kept here as the oracle."""
    node_counts = {t: 0 for t in CLOSED_NODE_TYPES}
    edge_counts: dict[str, int] = {}
    total_nodes = total_edges = 0
    for node in store.list_nodes():
        if is_infra(node) or (not include_structural and is_structural_claim(node)):
            continue
        t = str(node.type)
        if t in node_counts:
            node_counts[t] += 1
            total_nodes += 1
    for edge in store.list_edges():
        edge_counts[str(edge.type)] = edge_counts.get(str(edge.type), 0) + 1
        total_edges += 1
    return {
        "counts": {t: n for t, n in node_counts.items() if n},
        "edges": dict(sorted(edge_counts.items())),
        "total_nodes": total_nodes,
        "total_edges": total_edges,
    }


def _summarise(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "counts": {row["type"]: row["count"] for row in payload["node_types"]},
        "edges": {row["type"]: row["count"] for row in payload["edge_types"]},
        "total_nodes": payload["total_nodes"],
        "total_edges": payload["total_edges"],
    }


# ---------------------------------------------------------------- equivalence


def test_projection_equals_the_old_full_scans(tmp_path: Path) -> None:
    store = _store()
    projection = _manager(tmp_path).build(store)
    assert projection.stats == build_predicate_stats(store)
    for flag in (False, True):
        assert _summarise(projection.graph_stats[flag]) == _reference_graph_stats(store, flag)
        assert json.loads(projection.graph_stats_json[flag]) == projection.graph_stats[flag]
    assert projection.graph_stats[True]["total_nodes"] == projection.graph_stats[False]["total_nodes"] + 1
    assert projection.key is not None and projection.key.write_seq == store.write_seq


def test_projection_scans_read_no_vectors(tmp_path: Path) -> None:
    store = _store()
    seen: list[bool] = []
    for name in ("list_nodes", "get_nodes"):
        original = getattr(store, name)

        def spy(*args: Any, _o: Any = original, include_embedding: bool = False, **kw: Any) -> Any:
            seen.append(include_embedding)
            return _o(*args, include_embedding=include_embedding, **kw)

        setattr(store, name, spy)
    _manager(tmp_path).build(store)
    assert seen and not any(seen)


# ------------------------------------------------------------------ staleness


def test_each_mutator_makes_the_projection_stale(tmp_path: Path) -> None:
    store = _store()
    manager = _manager(tmp_path)
    projection = manager.build(store)
    assert not manager.is_stale(store, projection)
    store.add_node(Node(id="dave", type="Agent", title="dave"))
    assert manager.is_stale(store, projection)
    projection = manager.build(store)
    assert not manager.is_stale(store, projection)
    store.add_edge(Edge(id="e2", type="alpha", src="bob", dst="carol"))
    assert manager.is_stale(store, projection)


def test_a_reopened_store_is_a_new_identity(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    projection = manager.build(_store())
    assert manager.is_stale(_store(), projection)


def test_max_age_backstop_marks_an_unwritten_projection_stale(tmp_path: Path) -> None:
    store = _store()
    manager = _manager(tmp_path, max_age_s=0.05)
    projection = manager.build(store)
    assert not manager.is_stale(store, projection)
    time.sleep(0.1)
    assert manager.is_stale(store, projection)
    before = manager.builds
    assert manager.build(store) is not projection  # an aged projection is not reused
    assert manager.builds == before + 1


# -------------------------------------------------------------- single flight


@pytest.mark.asyncio
async def test_fifty_concurrent_reads_trigger_at_most_one_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store()
    state = _state(tmp_path, store)
    manager = _manager(tmp_path)
    scans: list[str] = []
    real = _projection.build_predicate_stats

    def slow(s: Any) -> Any:
        scans.append(threading.current_thread().name)
        time.sleep(0.2)
        return real(s)

    monkeypatch.setattr(_projection, "build_predicate_stats", slow)
    reads = await asyncio.gather(*[manager.read(state) for _ in range(50)])
    assert all(r.projection is None and r.stale and r.rebuilding for r in reads)
    await _settle(manager)
    assert manager.builds == 1 and len(scans) == 1
    assert scans[0].startswith("okto-neuron-job"), f"rebuild ran on {scans[0]!r}, not the JobExecutor"
    later = await asyncio.gather(*[manager.read(state) for _ in range(50)])
    assert all(r.projection is not None and not r.stale and not r.rebuilding for r in later)
    assert manager.builds == 1


@pytest.mark.asyncio
async def test_writes_during_a_rebuild_collapse_into_one_follow_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store()
    state = _state(tmp_path, store)
    manager = _manager(tmp_path)
    started = threading.Event()
    release = threading.Event()
    real = _projection.build_predicate_stats
    calls = 0

    def gated(s: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            assert release.wait(30)
        return real(s)

    monkeypatch.setattr(_projection, "build_predicate_stats", gated)
    await manager.read(state)
    assert await asyncio.to_thread(started.wait, 30)
    for i in range(12):  # twelve writes while the first rebuild is scanning
        store.add_node(Node(id=f"w{i}", type="Agent", title=f"w{i}"))
        await manager.read(state)  # each read during the rebuild is a no-op, not a new rebuild
    release.set()
    await _settle(manager)
    assert manager.builds == 2, "the pending writes must collapse into exactly one more rebuild"
    assert not manager.is_stale(store)
    assert manager.projection.stats == build_predicate_stats(store)
    assert manager.projection.graph_stats[True]["total_nodes"] == len(list(store.list_nodes())) - 0


@pytest.mark.asyncio
async def test_reads_never_wait_for_a_rebuild_and_serve_the_last_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store()
    state = _state(tmp_path, store)
    manager = _manager(tmp_path)
    manager.build(store)
    first = manager.projection
    started, release = threading.Event(), threading.Event()
    real = _projection.build_predicate_stats

    def gated(s: Any) -> Any:
        started.set()
        assert release.wait(30)
        return real(s)

    monkeypatch.setattr(_projection, "build_predicate_stats", gated)
    store.add_node(Node(id="late", type="Agent", title="late"))
    read = await asyncio.wait_for(manager.read(state), timeout=5)  # returns while the build is gated
    assert read.projection is first and read.stale and read.rebuilding
    assert await asyncio.to_thread(started.wait, 30)
    again = await asyncio.wait_for(manager.read(state), timeout=5)
    assert again.projection is first and again.stale and again.rebuilding
    release.set()
    await _settle(manager)
    assert manager.projection is not first and not manager.is_stale(store)


@pytest.mark.asyncio
async def test_rebuilds_are_spaced_from_the_end_of_the_last_one(tmp_path: Path) -> None:
    store = _store()
    state = _state(tmp_path, store)
    manager = _manager(tmp_path, min_interval_s=0.4)
    await manager.read(state)
    await _settle(manager)
    finished = time.monotonic()
    store.add_node(Node(id="x", type="Agent", title="x"))
    await manager.read(state)
    await _settle(manager)
    assert time.monotonic() - finished >= 0.35, "second rebuild ignored the minimum interval"
    assert manager.builds == 2


@pytest.mark.asyncio
async def test_forced_rebuild_skips_the_spacing_and_rescans_even_when_current(tmp_path: Path) -> None:
    store = _store()
    state = _state(tmp_path, store)
    manager = _manager(tmp_path, min_interval_s=60.0)
    await manager.read(state)
    await _settle(manager)
    assert manager.builds == 1 and not manager.is_stale(store)
    assert manager.ensure(state, force=True) is True
    await _settle(manager)
    assert manager.builds == 2


def test_a_job_joins_the_build_in_flight_instead_of_scanning_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store()
    manager = _manager(tmp_path)
    started, release = threading.Event(), threading.Event()
    real = _projection.build_predicate_stats

    def gated(s: Any) -> Any:
        started.set()
        assert release.wait(30)
        return real(s)

    monkeypatch.setattr(_projection, "build_predicate_stats", gated)
    results: list[Any] = []
    first = threading.Thread(target=lambda: results.append(manager.build(store)))
    first.start()
    assert started.wait(30)
    second = threading.Thread(target=lambda: results.append(manager.stats_for_job(store)))
    second.start()
    time.sleep(0.2)
    release.set()
    first.join(30)
    second.join(30)
    assert manager.builds == 1
    assert results[1] == results[0].stats


# -------------------------------------------------------------------- sidecar


@pytest.mark.asyncio
async def test_sidecar_is_written_off_loop_and_restored_as_stale(tmp_path: Path) -> None:
    store = _store()
    state = _state(tmp_path, store)
    manager = _manager(tmp_path)
    writers: list[str] = []
    original = manager._persist

    def recording(projection: Any, force: bool = False) -> None:
        writers.append(threading.current_thread().name)
        original(projection, force)

    manager._persist = recording  # type: ignore[method-assign]
    await manager.read(state)
    await _settle(manager)
    assert writers and writers[0].startswith("okto-neuron-store"), writers
    sidecar = tmp_path / SIDECAR_RELATIVE
    assert json.loads(sidecar.read_text())["version"] == SIDECAR_VERSION

    restored = _manager(tmp_path)
    await restored.read(state)
    assert restored.projection is not None
    assert restored.projection.key is None  # counters are per process
    await _settle(restored)
    assert restored.builds == 1, "a sidecar from an earlier process must still trigger a rebuild"
    assert restored.projection.key is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "",
        json.dumps({"version": 999, "built_at": 0, "duration_s": 0, "stats": {}, "graph_stats": {}}),
        json.dumps({"version": SIDECAR_VERSION}),
        json.dumps({"version": SIDECAR_VERSION, "built_at": 1, "duration_s": 1,
                    "stats": {"vocabulary": {"a": "x"}}, "graph_stats": {"false": {}, "true": {}}}),
    ],
)
async def test_corrupt_or_old_sidecar_is_ignored_and_rebuilt(tmp_path: Path, content: str) -> None:
    sidecar = tmp_path / SIDECAR_RELATIVE
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(content)
    store = _store()
    state = _state(tmp_path, store)
    manager = _manager(tmp_path)
    read = await manager.read(state)
    assert read.projection is None and read.stale and read.rebuilding  # cold: builds in background
    await _settle(manager)
    assert manager.builds == 1 and manager.projection.key is not None
    manager._persist(manager.projection, True)
    assert json.loads(sidecar.read_text())["version"] == SIDECAR_VERSION  # repaired on disk


# ------------------------------------------------------------------ HTTP + CLI


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[TestClient, Vault]]:
    reset_state_for_tests()
    vault = Vault.init(tmp_path / "v")
    store = vault.store
    for node_id in ("alice", "bob"):
        store.add_node(Node(id=node_id, type="Agent", title=node_id))
    store.add_edge(Edge(id="e1", type="alpha", src="alice", dst="bob"))
    state = init_state(vault, vault.path)
    _projection.configure(min_interval_s=0.0, max_age_s=600.0)
    with TestClient(build_rest_app(state), base_url="http://127.0.0.1") as client:
        yield client, vault
    reset_state_for_tests()


def test_cold_vault_answers_202_then_settles(served: tuple[TestClient, Vault]) -> None:
    client, _vault = served
    first = client.get("/api/v1/graph/stats")
    assert first.status_code == 202 and first.json() == {"status": "building"}
    settled = get_settled(client, "/api/v1/graph/stats")
    body = settled.json()
    assert body["status"] == "ok" and body["stale"] is False and body["rebuilding"] is False
    assert body["total_edges"] == 1 and body["total_nodes"] == 2
    structural = get_settled(client, "/api/v1/graph/stats?include_structural=1").json()
    assert structural["total_nodes"] == 2


def test_write_makes_the_served_projection_stale_until_it_rebuilds(
    served: tuple[TestClient, Vault], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, vault = served
    get_settled(client, "/api/v1/graph/stats")
    gate = threading.Event()
    real = _projection.build_predicate_stats

    def gated(s: Any) -> Any:
        assert gate.wait(30)
        return real(s)

    monkeypatch.setattr(_projection, "build_predicate_stats", gated)
    vault.store.add_node(Node(id="carol", type="Agent", title="carol"))
    during = client.get("/api/v1/graph/stats")
    assert during.status_code == 200
    body = during.json()
    assert body["stale"] is True and body["rebuilding"] is True
    assert body["total_nodes"] == 2, "the last projection is served, not a fresh scan"
    gate.set()
    assert get_settled(client, "/api/v1/graph/stats").json()["total_nodes"] == 3


def test_predicates_endpoint_keeps_its_shape_and_adds_flags(served: tuple[TestClient, Vault]) -> None:
    client, _vault = served
    cold = client.get("/api/v1/upkeep/predicates")
    assert cold.status_code == 202 and cold.json() == {"status": "building"}
    body = get_settled(client, "/api/v1/upkeep/predicates").json()
    for key in ("status", "vocabulary_size", "records", "counts", "last_propose", "last_apply", "worker_active"):
        assert key in body, key
    assert body["vocabulary_size"] == 1 and body["stale"] is False and body["rebuilding"] is False


def test_rebuild_endpoint_forces_a_rescan(served: tuple[TestClient, Vault]) -> None:
    client, vault = served
    get_settled(client, "/api/v1/graph/stats")
    manager = _projection.manager_for(vault.path)
    builds = manager.builds
    response = client.post("/api/v1/upkeep/rebuild-stats", json={})
    assert response.status_code == 202
    assert response.json()["status"] == "rebuilding"
    get_settled(client, "/api/v1/graph/stats")
    deadline = time.monotonic() + 30
    while manager.builds == builds and time.monotonic() < deadline:
        time.sleep(0.02)
    assert manager.builds == builds + 1, "a forced rebuild must rescan even a current projection"


def test_cli_rebuild_stats_is_a_thin_client(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def fake_post(endpoint: str, path: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        seen.append((path, payload))
        return {"status": "rebuilding", "started": len(seen) == 1}

    monkeypatch.setattr("okto_neuron.cli._client_post", fake_post)
    started = CliRunner().invoke(cli_app, ["upkeep", "rebuild-stats"])
    assert started.exit_code == 0, started.output
    assert "rebuild started" in started.output
    joined = CliRunner().invoke(cli_app, ["upkeep", "rebuild-stats"])
    assert joined.exit_code == 0 and "already running" in joined.output
    assert seen[0][0] == "/api/v1/upkeep/rebuild-stats"


def test_compute_graph_stats_handles_an_empty_store() -> None:
    both = compute_graph_stats(IndexedStore(InMemoryStore(), InMemoryIndexStore()))
    for flag in (False, True):
        assert both[flag]["total_nodes"] == 0 and both[flag]["node_types"] == []
