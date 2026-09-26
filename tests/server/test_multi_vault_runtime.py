"""Immutable per-vault worker and sidecar ownership (ADR 0034)."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_neuron.config import FolderWatchConfig
from okto_neuron.server import (
    _folder_watch,
    _ingest_queue,
    _jobs,
    _scheduler,
    runtime as server_runtime,
)
from okto_neuron.server._ingest_queue import IngestItem
from okto_neuron.server.state import (
    ServerState,
    VaultRuntime,
    bind_vault_runtime,
    get_server_state,
    get_state,
    init_state,
    reset_state_for_tests,
)
from okto_neuron.vault import Vault


def _new_vault(tmp_path: Path, name: str) -> tuple[Vault, Path]:
    vault = Vault.init(tmp_path / name, packs=["core"])
    return vault, Path(vault.path).resolve(strict=False)


def test_runtime_identity_path_and_locks_are_isolated(tmp_path: Path) -> None:
    vault_a, path_a = _new_vault(tmp_path, "a")
    vault_b, path_b = _new_vault(tmp_path, "b")
    state = ServerState(
        vault=vault_a,
        vault_path=path_a,
        multi_vault_runtime_enabled=True,
    )
    try:
        runtime_a = state.runtime_for(path_a)
        runtime_b = state.runtime_for(path_b, vault=vault_b)

        assert state.runtime_for(path_a / ".") is runtime_a
        assert runtime_a.vault_path == path_a
        assert runtime_b.vault_path == path_b
        assert state.writer_lock is runtime_a.writer_lock
        assert runtime_a.writer_lock is not runtime_b.writer_lock
        assert runtime_a.config_lock is not runtime_b.config_lock
        assert runtime_a.ingest_queue is not runtime_b.ingest_queue
        assert runtime_a.curation_jobs is not runtime_b.curation_jobs

        state.switch_vault(vault_b, path_b)
        assert state.writer_lock is runtime_b.writer_lock
    finally:
        state.close()


def test_context_binding_scopes_legacy_get_state_reads(tmp_path: Path) -> None:
    vault_a, path_a = _new_vault(tmp_path, "a")
    vault_b, path_b = _new_vault(tmp_path, "b")
    reset_state_for_tests()
    state = init_state(vault_a, path_a)
    runtime_a = state.runtime_for(path_a)
    runtime_b = state.runtime_for(path_b, vault=vault_b)
    state.folder_watch_restart_count = 3
    try:
        assert get_state() is state
        assert get_server_state() is state
        assert runtime_b.auth_token == state.auth_token
        assert runtime_b.folder_watch_restart_count == 3

        with bind_vault_runtime(runtime_a):
            assert get_state() is runtime_a
            assert get_server_state() is state
            with bind_vault_runtime(runtime_b):
                assert get_state() is runtime_b
            assert get_state() is runtime_a
        assert get_state() is state

        runtime_b.last_ingest_at = 42.0
        assert runtime_a.last_ingest_at is None
        assert state.last_ingest_at is None

        async def observe(bound: VaultRuntime) -> VaultRuntime:
            with bind_vault_runtime(bound):
                await asyncio.sleep(0)
                selected = get_state()
                assert isinstance(selected, VaultRuntime)
                return selected

        async def concurrent_observations() -> list[VaultRuntime]:
            return list(await asyncio.gather(observe(runtime_a), observe(runtime_b)))

        assert asyncio.run(concurrent_observations()) == [
            runtime_a,
            runtime_b,
        ]
    finally:
        reset_state_for_tests()


def test_direct_state_compatibility_mirrors_at_request_boundary(tmp_path: Path) -> None:
    vault, path = _new_vault(tmp_path, "legacy")
    state = ServerState(vault=vault, vault_path=path)
    runtime = state.active_runtime
    assert runtime is not None
    queued = IngestItem(id="legacy", name="legacy.md", path="/legacy.md")
    state.ingest_queue = [queued]
    state.last_ingest_at = 12.0
    try:
        with bind_vault_runtime(runtime):
            assert runtime.ingest_queue == [queued]
            assert runtime.last_ingest_at == 12.0
            runtime.last_ingest_at = 34.0
        assert state.ingest_queue == [queued]
        assert state.last_ingest_at == 34.0
    finally:
        state.close()


def test_queue_and_job_sidecars_rehydrate_per_vault(tmp_path: Path) -> None:
    vault_a, path_a = _new_vault(tmp_path, "a")
    vault_b, path_b = _new_vault(tmp_path, "b")
    state = ServerState(
        vault=vault_a,
        vault_path=path_a,
        multi_vault_runtime_enabled=True,
    )
    try:
        runtime_a = state.runtime_for(path_a)
        runtime_b = state.runtime_for(path_b, vault=vault_b)
        runtime_a.ingest_queue = [IngestItem(id="1-a", name="a.md", path="/a.md")]
        runtime_b.ingest_queue = [IngestItem(id="7-b", name="b.md", path="/b.md")]
        runtime_a.curation_jobs = [_jobs.new_job("kind-a")]
        runtime_b.curation_jobs = [_jobs.new_job("kind-b")]
        _ingest_queue.persist(runtime_a)
        _ingest_queue.persist(runtime_b)
        _jobs.persist(runtime_a)
        _jobs.persist(runtime_b)
    finally:
        state.close()

    fresh = ServerState(vault=None, vault_path=None, multi_vault_runtime_enabled=True)
    try:
        restored_a = fresh.runtime_for(path_a)
        restored_b = fresh.runtime_for(path_b)
        assert [item.name for item in restored_a.ingest_queue] == ["a.md"]
        assert [item.name for item in restored_b.ingest_queue] == ["b.md"]
        assert [job.kind for job in restored_a.curation_jobs] == ["kind-a"]
        assert [job.kind for job in restored_b.curation_jobs] == ["kind-b"]
        assert restored_a.ingest_seq == 2
        assert restored_b.ingest_seq == 8
    finally:
        fresh.close()


def test_discovery_migrates_legacy_cross_vault_jobs(tmp_path: Path, monkeypatch) -> None:
    vault_a, path_a = _new_vault(tmp_path, "a")
    vault_b, path_b = _new_vault(tmp_path, "b")
    original = ServerState(
        vault=vault_a,
        vault_path=path_a,
        multi_vault_runtime_enabled=True,
    )
    try:
        runtime_a = original.runtime_for(path_a)
        original.runtime_for(path_b, vault=vault_b)
        runtime_a.curation_jobs = [_jobs.new_job("legacy-sweep", params={"vault": str(path_b)})]
        _jobs.persist(runtime_a)
    finally:
        original.close()

    import okto_neuron.vault_registry as registry

    entries = [SimpleNamespace(path=path_a), SimpleNamespace(path=path_b)]
    monkeypatch.setattr(registry, "list_vaults", lambda **_kwargs: entries)
    fresh = ServerState(vault=None, vault_path=None, multi_vault_runtime_enabled=True)
    try:
        runtime_a, runtime_b = fresh.runtimes(discover=True)
        assert runtime_a.vault_path == path_a
        assert runtime_b.vault_path == path_b
        assert runtime_a.curation_jobs == []
        assert [job.kind for job in runtime_b.curation_jobs] == ["legacy-sweep"]
        assert _jobs.jobs_path(runtime_a).read_text(encoding="utf-8").count("legacy-sweep") == 0
        assert "legacy-sweep" in _jobs.jobs_path(runtime_b).read_text(encoding="utf-8")
    finally:
        fresh.close()


def test_curation_worker_keeps_original_runtime_after_selection_change(
    tmp_path: Path,
) -> None:
    vault_a, path_a = _new_vault(tmp_path, "a")
    vault_b, path_b = _new_vault(tmp_path, "b")
    state = ServerState(
        vault=vault_a,
        vault_path=path_a,
        multi_vault_runtime_enabled=True,
    )
    runtime_a = state.runtime_for(path_a)
    state.runtime_for(path_b, vault=vault_b)
    started = threading.Event()
    finish = threading.Event()
    kind = "test-runtime-binding"

    def runner(runtime: VaultRuntime, _job: object) -> dict:
        started.set()
        assert finish.wait(5.0)
        return {
            "runtime": str(runtime.vault_path),
            "handle": str(Path(runtime.vault.path).resolve(strict=False)),
        }

    async def exercise() -> None:
        _jobs.register_runner(kind, runner, writes=False)
        job = _jobs.submit(runtime_a, kind)
        assert await asyncio.to_thread(started.wait, 5.0)
        assert state.vault_pool.lease_count(path_a) == 1

        state.switch_vault(vault_b, path_b)
        finish.set()
        task = runtime_a.curation_worker_task
        assert task is not None
        await task

        assert job.status == "done"
        assert job.result == {"runtime": str(path_a), "handle": str(path_a)}
        assert state.vault_path == path_b
        assert state.vault_pool.lease_count(path_a) == 0

    try:
        asyncio.run(exercise())
    finally:
        finish.set()
        _jobs._REGISTRY.pop(kind, None)
        state.close()


def test_folder_watch_routes_non_selected_vault_to_its_runtime(tmp_path: Path, monkeypatch) -> None:
    vault_a, path_a = _new_vault(tmp_path, "a")
    vault_b, path_b = _new_vault(tmp_path, "b")
    root_a = tmp_path / "root-a"
    root_b = tmp_path / "root-b"
    root_a.mkdir()
    root_b.mkdir()
    state = ServerState(
        vault=vault_a,
        vault_path=path_a,
        multi_vault_runtime_enabled=True,
    )
    runtime_a = state.runtime_for(path_a)
    runtime_b = state.runtime_for(path_b, vault=vault_b)
    entries = [SimpleNamespace(path=path_a), SimpleNamespace(path=path_b)]
    seen: list[tuple[VaultRuntime, Path]] = []

    import okto_neuron.vault_registry as registry

    monkeypatch.setattr(registry, "list_vaults", lambda **_kwargs: entries)
    monkeypatch.setattr(
        _folder_watch,
        "_load_folder_watch_config",
        lambda path: FolderWatchConfig(
            enabled=True,
            roots=[str(root_a if Path(path) == path_a else root_b)],
            poll_interval_s=1,
            quiet_debounce_s=1,
            min_interval_s=1,
        ),
    )

    async def fake_tick(
        runtime: VaultRuntime,
        vault_path: Path,
        *_args: object,
    ) -> None:
        seen.append((runtime, vault_path))

    monkeypatch.setattr(_folder_watch, "_tick_root", fake_tick)
    try:
        asyncio.run(_folder_watch._poll_tick(state, {}, {}, set()))
        assert seen == [(runtime_a, path_a), (runtime_b, path_b)]
        assert _folder_watch.get_watch_status(str(path_b))["paused_reason"] is None

        seen.clear()
        runtime_b.draining = True
        asyncio.run(_folder_watch._poll_tick(state, {}, {}, set()))
        assert seen == [(runtime_a, path_a)]
        assert "draining" in str(_folder_watch.get_watch_status(str(path_b))["paused_reason"])
    finally:
        state.close()


def test_scheduler_submits_against_each_runtime(tmp_path: Path, monkeypatch) -> None:
    vault_a, path_a = _new_vault(tmp_path, "a")
    vault_b, path_b = _new_vault(tmp_path, "b")
    state = ServerState(
        vault=vault_a,
        vault_path=path_a,
        multi_vault_runtime_enabled=True,
    )
    runtime_a = state.runtime_for(path_a)
    runtime_b = state.runtime_for(path_b, vault=vault_b)
    runtime_a.last_ingest_at = 10.0
    runtime_b.last_ingest_at = 20.0
    submitted: list[VaultRuntime] = []

    monkeypatch.setattr(state, "runtimes", lambda **_kwargs: (runtime_a, runtime_b))
    monkeypatch.setattr(_scheduler, "_should_sweep", lambda *_args: "eligible")
    monkeypatch.setattr(
        _scheduler,
        "_submit_sweeps",
        lambda runtime, *_args, **_kwargs: submitted.append(runtime),
    )
    try:
        _scheduler._tick(state, 100.0)
        assert submitted == [runtime_a, runtime_b]
    finally:
        state.close()


def test_draining_runtime_rejects_new_queue_and_job_work(tmp_path: Path) -> None:
    vault, path = _new_vault(tmp_path, "draining")
    state = ServerState(
        vault=vault,
        vault_path=path,
        multi_vault_runtime_enabled=True,
    )
    runtime = state.runtime_for(path)
    source = tmp_path / "note.md"
    source.write_text("note", encoding="utf-8")
    kind = "test-draining-runtime"
    _jobs.register_runner(kind, lambda _state, _job: {}, writes=False)
    runtime.draining = True
    try:
        with pytest.raises(RuntimeError, match="draining"):
            _ingest_queue.enqueue_paths(
                runtime,
                [source],
                path / ".marginalia" / "sources",
            )
        with pytest.raises(RuntimeError, match="draining"):
            _jobs.submit(runtime, kind)
        assert runtime.ingest_queue == []
        assert runtime.curation_jobs == []
    finally:
        _jobs._REGISTRY.pop(kind, None)
        state.close()


def test_startup_resumes_queued_work_for_every_runtime(monkeypatch) -> None:
    runtime_a = SimpleNamespace(
        ingest_queue=[SimpleNamespace(status="queued")],
        curation_jobs=[SimpleNamespace(status="done")],
    )
    runtime_b = SimpleNamespace(
        ingest_queue=[SimpleNamespace(status="done")],
        curation_jobs=[SimpleNamespace(status="queued")],
    )
    state = SimpleNamespace(runtimes=lambda *, discover: (runtime_a, runtime_b) if discover else ())
    ingest_started: list[object] = []
    jobs_started: list[object] = []

    monkeypatch.setattr(
        _ingest_queue,
        "ensure_worker",
        lambda target, _factory: ingest_started.append(target),
    )
    monkeypatch.setattr(
        _jobs,
        "ensure_worker",
        lambda target: jobs_started.append(target),
    )

    server_runtime._resume_durable_runtime_work(state)  # type: ignore[arg-type]

    assert ingest_started == [runtime_a]
    assert jobs_started == [runtime_b]


def test_shutdown_waits_for_tasks_owned_by_all_runtimes(tmp_path: Path) -> None:
    from okto_neuron.server.lifecycle import GracefulShutdown

    class FakeServer:
        should_exit = False
        force_exit = False

    state = ServerState(vault=None, vault_path=None, multi_vault_runtime_enabled=True)
    runtime_a = state.runtime_for(tmp_path / "a", rehydrate=False)
    runtime_b = state.runtime_for(tmp_path / "b", rehydrate=False)
    close_observations: list[tuple[bool, bool]] = []

    async def exercise() -> None:
        done_a = asyncio.Event()
        done_b = asyncio.Event()

        async def finish(event: asyncio.Event) -> None:
            await asyncio.sleep(0)
            event.set()

        task_a = asyncio.create_task(finish(done_a))
        task_b = asyncio.create_task(finish(done_b))
        runtime_a.maintenance_tasks.add(task_a)
        runtime_b.curation_worker_task = task_b
        state.close = lambda: close_observations.append((done_a.is_set(), done_b.is_set()))
        transports = (
            asyncio.create_task(asyncio.sleep(0)),
            asyncio.create_task(asyncio.sleep(0)),
        )

        await server_runtime._graceful_shutdown(
            state=state,
            orchestrator=GracefulShutdown(),
            rest_server=FakeServer(),  # type: ignore[arg-type]
            mcp_server=FakeServer(),  # type: ignore[arg-type]
            transport_tasks=transports,
        )

    asyncio.run(exercise())
    assert close_observations == [(True, True)]


def test_mcp_remember_holds_runtime_lease_and_writer_lock(tmp_path: Path, monkeypatch) -> None:
    from fastmcp import Client

    from okto_neuron.companion import RememberResult

    vault, path = _new_vault(tmp_path, "mcp")
    state = ServerState(
        vault=vault,
        vault_path=path,
        multi_vault_runtime_enabled=True,
    )
    runtime = state.runtime_for(path)
    observed: list[tuple[int, bool]] = []

    class FakeCompanion:
        def remember(self, _source: str, *, sensitivity: str, on_progress: object = None, **_kw: object) -> RememberResult:
            assert sensitivity == "default"
            observed.append((state.vault_pool.lease_count(path), runtime.writer_lock.locked()))
            return RememberResult(document_id="leased", committed=1, blocks_total=1)

    import okto_neuron.server.http as http

    monkeypatch.setattr(http, "companion_for", lambda _vault: FakeCompanion())
    server = server_runtime._build_mcp_server(state)

    async def exercise() -> None:
        async with Client(server) as client:
            result = await client.call_tool("remember", {"source": "lease-bound raw note"})
        assert (result.structured_content or {}).get("document_id") == "leased"

    try:
        asyncio.run(exercise())
        assert observed == [(1, True)]
        assert state.vault_pool.lease_count(path) == 0
        # A raw MCP resolver would leave a legacy pin and make this fail.
        assert state.vault_pool.release_path(path) is True
    finally:
        state.close()


def test_mcp_reads_survive_maintenance_while_remember_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp import Client

    from okto_neuron.companion import Answer

    vault, path = _new_vault(tmp_path, "mcp-maintenance")
    state = ServerState(
        vault=vault,
        vault_path=path,
        multi_vault_runtime_enabled=True,
    )
    runtime = state.runtime_for(path)

    class FakeCompanion:
        def ask(self, _question: str, **_kwargs: object) -> Answer:
            return Answer(text="old generation", citations=("claim:1",))

        def explore(self, _topic: str, **_kwargs: object) -> dict[str, object]:
            return {"nodes": [{"id": "concept:1"}], "relationships": [], "claims": []}

        def remember(self, _source: str, **_kwargs: object) -> None:
            pytest.fail("maintenance must reject remember before the write boundary")

    import okto_neuron.server.http as http

    monkeypatch.setattr(http, "companion_for", lambda _vault: FakeCompanion())
    runtime.mark_draining()
    server = server_runtime._build_mcp_server(state)

    async def exercise() -> None:
        async with Client(server) as client:
            answer = await client.call_tool("ask", {"question": "what?"})
            graph = await client.call_tool("explore", {"topic": "topic"})
            assert (answer.structured_content or {}).get("text") == "old generation"
            assert (graph.structured_content or {}).get("nodes") == [{"id": "concept:1"}]
            with pytest.raises(Exception, match="maintenance"):
                await client.call_tool("remember", {"source": "new note"})

    try:
        asyncio.run(exercise())
        assert state.vault_pool.lease_count(path) == 0
    finally:
        state.close()
