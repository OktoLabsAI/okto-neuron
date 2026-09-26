"""ADR 0009 P3 — in-process rebuild / heal / reembed curation runner.

Model-free unit tests (NEVER load the LLM). They drive:
  1. ``run_rebuild`` swap re-sequencing with a STUB ingest against a real tmp
     Ladybug vault + a real ServerState on a live event loop: the live handle is
     readable mid-build (the build phase does NOT close it), the swap closes +
     replaces + reopens, and rebuild.state.json ends ``phase: complete``.
  2. ``run_heal`` end-to-end (deterministic, NO LLM): seed the live graph with a
     variant + canonical + an edge + a confirmed authority record, submit the heal
     job, and assert the swapped graph collapsed the variant onto the canonical with
     the edge remapped — driven through the real job queue (writer_lock + to_thread
     + marshaled-swap path exercised end to end).
  3. ingest-during-rebuild gate: the runner sets ``draining`` for its duration and
     clears it after reopen.

The deterministic copy-fold UNIT tests (node-drop, edge-remap+dedup+self-loop,
claim facet remap, stale-canonical, idempotency) live in
``tests/reconcile/test_heal_fold.py`` on the InMemoryStore.

Work on /tmp copies only; never the live vault.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import okto_neuron.store._bootstrap as bootstrap_module
from okto_neuron.cli.kg import kg_init
from okto_neuron.companion import _incremental
from okto_neuron.config import VaultConfig
from okto_neuron.core.schema.legacy import Edge, Node
from okto_neuron.errors import RebuildAuditFailed
from okto_neuron.ingest.markdown import sha256_hex
from okto_neuron.reconcile.authority import (
    AUTHORITY_DIRNAME,
    AuthorityIndex,
    AuthorityRecord,
)
from okto_neuron.predicates import (
    PredicateDecisionProvenance,
    PredicateRecord,
    PredicateRegistry,
)
from okto_neuron.server import _curation, _jobs
from okto_neuron.server.state import ServerState, reset_vault_write_lock
from okto_neuron.semantic_fingerprint import (
    load_semantic_materialization,
    publish_semantic_materialization,
    semantic_fingerprints,
    semantic_materialization_path,
    write_semantic_materialization,
)
from okto_neuron.store import vault as vault_module
from okto_neuron.store.integrity import AuditStatus
from okto_neuron.store.integrity_state import (
    GraphIntegrityState,
    load_integrity_state,
    write_integrity_state,
)
from okto_neuron.store.ladybug import VaultConnection
from okto_neuron.vault import Vault


@pytest.fixture(autouse=True)
def _clean_handles():
    reset_vault_write_lock()
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        try:
            store.close()
        except Exception:
            pass
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()
    for handle in list(bootstrap_module._bootstrap_cache.values()):
        try:
            handle.close()
        except Exception:
            pass
    bootstrap_module._bootstrap_cache.clear()
    reset_vault_write_lock()


def _make_vault(tmp_path: Path) -> Path:
    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    # The bootstrap-from-init left a handle cached; drop it so Vault.open gets a
    # fresh one (mirrors a real daemon startup).
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()
    bootstrap_module._bootstrap_cache.clear()
    (vault_path / "notes" / "a.md").write_text("# A\n", encoding="utf-8")
    (vault_path / "notes" / "b.md").write_text("# B\n", encoding="utf-8")
    return vault_path


def _state(vault_path: Path) -> ServerState:
    vault = Vault.open(vault_path)
    return ServerState(vault=vault, vault_path=vault_path)


def _publish_current_materialization(state: ServerState, vault_path: Path) -> dict[str, str]:
    cfg = VaultConfig.load(vault_path)
    measured = semantic_fingerprints(
        cfg,
        vault_path,
        ingest_config=cfg.ingest,
        effective_incremental=_incremental.incremental_enabled(cfg.ingest),
        effective_subchunk=_incremental.subchunk_enabled(cfg.ingest),
    )
    fingerprints = {
        "config": measured.config_fingerprint,
        "extraction": measured.extraction_fingerprint,
        "semantic_policy": measured.semantic_policy_fingerprint,
    }
    generation = str(state.vault.store._graph_handle.graph_generation or "")
    publish_semantic_materialization(
        vault_path,
        graph_generation=generation,
        fingerprints=fingerprints,
        source="test_seed",
    )
    return fingerprints


@pytest.mark.asyncio
async def test_vaultwide_swap_waits_for_same_vault_read_lease(tmp_path: Path) -> None:
    vault_path = _make_vault(tmp_path)
    state = _state(vault_path)
    worker_lease = state.lease_vault()
    read_lease = state.lease_vault()
    swapped: list[bool] = []

    class _Job:
        @staticmethod
        def release_vault_lease_for_swap() -> None:
            worker_lease.release()

    try:
        task = asyncio.create_task(
            _curation._swap_under_runtime_fence(
                state,
                _Job(),
                lambda: swapped.append(True),
            )
        )
        await asyncio.sleep(0.05)
        assert task.done() is False
        assert state.vault_pool.is_fenced(vault_path) is True
        assert state.vault_pool.lease_count(vault_path) == 1
        assert read_lease.vault.store.is_closed is False

        read_lease.release()
        await asyncio.wait_for(task, timeout=5)

        assert swapped == [True]
        assert state.vault_pool.is_fenced(vault_path) is False
        assert state.vault_pool.peek(vault_path) is not None
        with state.lease_vault() as reopened:
            assert reopened.store.is_closed is False
    finally:
        read_lease.release()
        worker_lease.release()
        state.close()


@pytest.mark.asyncio
async def test_vaultwide_swap_does_not_wait_for_other_vault_lease(tmp_path: Path) -> None:
    path_a = _make_vault(tmp_path / "a")
    path_b = _make_vault(tmp_path / "b")
    state = _state(path_a)
    runtime_b = state.runtime_for(path_b, vault=Vault.open(path_b))
    worker_lease = state.lease_vault()
    lease_b = runtime_b.lease_vault()

    class _Job:
        @staticmethod
        def release_vault_lease_for_swap() -> None:
            worker_lease.release()

    try:
        await asyncio.wait_for(
            _curation._swap_under_runtime_fence(state, _Job(), lambda: None),
            timeout=5,
        )
        assert state.vault_pool.lease_count(path_b) == 1
        assert lease_b.vault.store.is_closed is False
        assert state.vault_pool.peek(path_b) is lease_b.vault
    finally:
        lease_b.release()
        worker_lease.release()
        state.close()


@pytest.mark.asyncio
async def test_post_reopen_validation_failure_keeps_runtime_fenced(tmp_path: Path) -> None:
    vault_path = _make_vault(tmp_path)
    state = _state(vault_path)
    worker_lease = state.lease_vault()

    class _Job:
        @staticmethod
        def release_vault_lease_for_swap() -> None:
            worker_lease.release()

    def reject(_reopened: Vault) -> None:
        raise RuntimeError("post-swap audit failed")

    try:
        with pytest.raises(RuntimeError, match="post-swap audit failed"):
            await _curation._swap_under_runtime_fence(
                state,
                _Job(),
                lambda: None,
                validate_reopened=reject,
            )

        assert state.vault_pool.is_fenced(vault_path) is True
        assert state.draining is True
        assert state.vault_pool.peek(vault_path) is not None
    finally:
        worker_lease.release()
        state.close()


# ── 1. swap re-sequencing ───────────────────────────────────────────────────────
@pytest.mark.parametrize("reconcile_schedule_fails", [False, True])
def test_run_rebuild_keeps_live_handle_serving_then_swaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reconcile_schedule_fails: bool,
) -> None:
    """Stub ingest; assert the live handle reads mid-build, the swap reopens, and
    rebuild.state.json ends complete. Driven through the real job queue so the
    writer_lock + to_thread + marshaled-swap path is exercised end to end."""
    vault_path = _make_vault(tmp_path)

    async def _run() -> None:
        # init_state would set a module singleton; build state directly + register.
        from okto_neuron.server import state as state_mod

        st = _state(vault_path)
        state_mod._STATE = st
        try:
            _curation.register_runners()
            if reconcile_schedule_fails:
                monkeypatch.setattr(
                    _curation,
                    "schedule_cross_document_reconciliation",
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        RuntimeError("reconcile queue unavailable")
                    ),
                )
            else:
                # Exercise the real post-swap event-loop scheduling path while
                # keeping the follow-up proposal itself model-free.
                _jobs.register_runner(
                    "reconcile-propose",
                    lambda _state, _job: {
                        "count": 0,
                        "outcome": {"state": "complete"},
                    },
                    writes=False,
                    verified_snapshot=True,
                )

            observed_during_build: list[bool] = []

            def stub_ingest(path: Path, store: object) -> None:
                # We are in the BUILD phase. The DAEMON's live handle (st.vault)
                # must still be readable here — the build runs against ``store``
                # (the tmp graph), NOT st.vault.
                observed_during_build.append(not st.vault.store.is_closed)
                # write a node into the fresh tmp store so the graph is non-trivial
                rel = path.relative_to(vault_path).as_posix()
                store.add_node(Node(id=f"n-{rel}", type="Concept", title=rel))

            # Inject the stub via job params is not supported; patch the kind's
            # default ingest by passing ingest through _build_fresh_graph. The
            # runner builds its own ingest, so instead we register a thin runner
            # that calls run_rebuild with the stub via monkeypatching kg ingest.
            import okto_neuron.cli.kg as kg_cli

            orig_build = kg_cli._build_fresh_graph

            def patched_build(*args, **kwargs):
                kwargs["ingest"] = stub_ingest
                kwargs.pop("extractor", None)
                return orig_build(*args, **kwargs)

            kg_cli._build_fresh_graph = patched_build
            try:
                job = _jobs.submit(st, "rebuild", label="rebuild")
                # Drain runs as a task; await its completion.
                for _ in range(2000):
                    if job.status in ("done", "error"):
                        break
                    await asyncio.sleep(0.01)
            finally:
                kg_cli._build_fresh_graph = orig_build

            assert job.status == "done", job.error
            assert observed_during_build and all(observed_during_build), (
                "live handle must stay readable during the build phase"
            )
            assert job.result["swapped"] is True
            if reconcile_schedule_fails:
                assert job.result["cross_document_reconciliation"]["state"] == "failed"
                assert job.result["cross_document_reconciliation"]["stage"] == "schedule"
            else:
                reconciliation = job.result["cross_document_reconciliation"]
                assert reconciliation["state"] == "scheduled"
                assert "deferred_until_maintenance_end" not in reconciliation
                assert "deferred_until_restart" not in reconciliation
                scheduled_job = _jobs.get_job(st, reconciliation["job_id"])
                assert scheduled_job is not None
                assert scheduled_job.kind == "reconcile-propose"
            # state file complete
            state_path = vault_path / ".marginalia" / "rebuild.state.json"
            data = json.loads(state_path.read_text(encoding="utf-8"))
            assert data["phase"] == "complete"
            assert "sha256" in data
            assert data["final_audit"]["status"] == "verified"
            assert data["post_swap_audit"]["status"] == "verified"
            integrity = load_integrity_state(
                vault_path,
                expected_graph_generation=data["graph_generation"],
            )
            assert integrity.status is AuditStatus.VERIFIED
            assert integrity.writer_fenced is False
            if not reconcile_schedule_fails:
                assert scheduled_job.params == {
                    "trigger": "verified_rebuild",
                    "ingest_item_ids": [],
                    "graph_generation": data["graph_generation"],
                }
            # draining cleared after reopen; vault reopened + readable
            assert st.draining is False
            assert not st.vault.store.is_closed
            # the swapped graph contains the stub-ingested nodes
            ids = {n.id for n in st.vault.store.list_nodes()}
            assert "n-notes/a.md" in ids and "n-notes/b.md" in ids
            assert not _curation._rebuild_recovery_dir(vault_path, job.id).exists()
        finally:
            try:
                st.close()
            except Exception:
                pass
            state_mod._STATE = None

    asyncio.run(_run())


@pytest.mark.parametrize("policy_mismatch", [False, True])
def test_run_rollback_restores_previous_generation_or_rejects_policy_mismatch(
    tmp_path: Path,
    policy_mismatch: bool,
) -> None:
    """Rollback restores only the current generation's verified predecessor.

    The backup is copied rather than consumed, the displaced graph is retained,
    and a mismatched semantic policy fails before the live graph is touched.
    """
    vault_path = _make_vault(tmp_path)

    async def _run() -> None:
        from okto_neuron.cli import kg as kg_cli
        from okto_neuron.predicates import PredicateRegistry
        from okto_neuron.server import state as state_mod

        st = _state(vault_path)
        state_mod._STATE = st
        try:
            _curation.register_runners()
            _jobs.register_runner(
                "reconcile-propose",
                lambda _state, _job: {
                    "count": 0,
                    "outcome": {"state": "complete"},
                },
                writes=False,
                verified_snapshot=True,
            )
            registry = PredicateRegistry(vault_path)
            registry.seed_builtins()
            original_policy_bytes = registry.path.read_bytes()
            st.vault.store.add_node(Node(id="before", type="Concept", title="Before"))
            original_generation = str(st.vault.store._graph_handle.graph_generation or "")
            original_fingerprints = _publish_current_materialization(st, vault_path)

            original_build = kg_cli._build_fresh_graph

            def patched_build(*args, **kwargs):
                registry.path.write_bytes(original_policy_bytes + b" ")

                def stub_ingest(path: Path, store: object) -> None:
                    rel = path.relative_to(vault_path).as_posix()
                    store.add_node(Node(id=f"after-{rel}", type="Concept", title=rel))

                kwargs["ingest"] = stub_ingest
                kwargs.pop("extractor", None)
                return original_build(*args, **kwargs)

            kg_cli._build_fresh_graph = patched_build
            try:
                rebuild = _jobs.submit(st, "rebuild", label="rebuild")
                for _ in range(2000):
                    if rebuild.status in ("done", "error"):
                        break
                    await asyncio.sleep(0.01)
            finally:
                kg_cli._build_fresh_graph = original_build

            assert rebuild.status == "done", rebuild.error
            assert registry.path.read_bytes() != original_policy_bytes
            rebuilt_generation = str(rebuild.result["graph_generation"])
            assert rebuilt_generation != original_generation
            assert "before" not in {node.id for node in st.vault.store.list_nodes()}

            artifact_dir = vault_path / ".marginalia" / "rebuild-artifacts" / rebuilt_generation
            backup_path = artifact_dir / "previous-graph.lbug"
            target_receipt_path = artifact_dir / "previous-semantic-materialization.json"
            assert backup_path.is_file()
            if policy_mismatch:
                mismatched = dict(original_fingerprints)
                mismatched["semantic_policy"] = "sha256:" + ("0" * 64)
                write_semantic_materialization(
                    target_receipt_path,
                    graph_generation=original_generation,
                    fingerprints=mismatched,
                    source="test_policy_mismatch",
                )

            rollback = _jobs.submit(
                st,
                "rollback",
                label="rollback",
                params={"from_generation": rebuilt_generation},
            )
            for _ in range(2000):
                if rollback.status in ("done", "error"):
                    break
                await asyncio.sleep(0.01)

            if policy_mismatch:
                assert rollback.status == "error"
                assert "semantic policy checkpoint fingerprint mismatch" in str(rollback.error)
                assert st.vault.store._graph_handle.graph_generation == rebuilt_generation
                ids = {node.id for node in st.vault.store.list_nodes()}
                assert "before" not in ids
                assert "after-notes/a.md" in ids
            else:
                assert rollback.status == "done", rollback.error
                assert rollback.result["graph_generation"] == original_generation
                ids = {node.id for node in st.vault.store.list_nodes()}
                assert "before" in ids
                assert "after-notes/a.md" not in ids
                active_receipt = load_semantic_materialization(
                    semantic_materialization_path(vault_path),
                    expected_graph_generation=original_generation,
                )
                assert active_receipt is not None
                assert active_receipt["source"] == "rollback"
                assert registry.path.read_bytes() == original_policy_bytes
                displaced = Path(str(rollback.result["displaced_graph"]))
                assert displaced.is_file()
                rollback_state = json.loads(
                    (vault_path / ".marginalia" / "rollback.state.json").read_text(encoding="utf-8")
                )
                assert rollback_state["phase"] == "complete"
            assert backup_path.is_file(), "rollback must not consume its source checkpoint"
        finally:
            try:
                st.close()
            except Exception:
                pass
            state_mod._STATE = None

    asyncio.run(_run())


def test_run_rebuild_model_free_still_publishes_rollback_receipt(tmp_path: Path) -> None:
    """D-92 (reversal of D-89's ladybug caveat).

    A vault whose only history is a model-free rebuild — no prior
    ``Companion.remember()`` run ever ledgered (this test's stub ingest, like
    the deterministic-only ``Vault.add``/``kg add`` path, never touches the
    candidate ledger) and no active semantic-materialization receipt ever
    published (this is the vault's FIRST rebuild) — must still get a real,
    generation-bound rollback checkpoint. Before the fix,
    ``_ledger_materialized_semantic_fingerprints`` failed closed to an
    all-``None`` triplet whenever the ledger had zero completed runs, so
    ``_write_previous_semantic_materialization`` silently skipped writing the
    receipt and ``rollback_candidate``/``run_rollback`` had nothing to work
    with — a real, valid backup (``previous-graph.lbug``) sitting right next
    to a missing receipt, model-free-only on Ladybug (Grafx/Neo4j never
    needed one). The fingerprints are pure functions of the vault's current
    config, so the fix computes them live instead of requiring history.
    """
    vault_path = _make_vault(tmp_path)

    async def _run() -> None:
        from okto_neuron.cli import kg as kg_cli
        from okto_neuron.curation.orchestrate import rollback_candidate
        from okto_neuron.server import state as state_mod

        st = _state(vault_path)
        state_mod._STATE = st
        try:
            _curation.register_runners()
            _jobs.register_runner(
                "reconcile-propose",
                lambda _state, _job: {"count": 0, "outcome": {"state": "complete"}},
                writes=False,
                verified_snapshot=True,
            )
            original_generation = str(st.vault.store._graph_handle.graph_generation or "")
            assert original_generation
            # No candidate ledger entry and no active receipt exist yet for
            # ``original_generation`` — this is the vault's first ever build,
            # untouched since ``kg_init``.
            assert not (vault_path / ".marginalia" / "candidate-ledger.jsonl").exists()
            assert (
                load_semantic_materialization(
                    semantic_materialization_path(vault_path),
                    expected_graph_generation=original_generation,
                )
                is None
            )

            original_build = kg_cli._build_fresh_graph

            def patched_build(*args, **kwargs):
                def stub_ingest(path: Path, store: object) -> None:
                    rel = path.relative_to(vault_path).as_posix()
                    store.add_node(Node(id=f"n-{rel}", type="Concept", title=rel))

                kwargs["ingest"] = stub_ingest
                kwargs.pop("extractor", None)
                return original_build(*args, **kwargs)

            kg_cli._build_fresh_graph = patched_build
            try:
                rebuild = _jobs.submit(st, "rebuild", label="rebuild")
                for _ in range(2000):
                    if rebuild.status in ("done", "error"):
                        break
                    await asyncio.sleep(0.01)
            finally:
                kg_cli._build_fresh_graph = original_build

            assert rebuild.status == "done", rebuild.error
            rebuilt_generation = str(rebuild.result["graph_generation"])
            assert rebuilt_generation != original_generation

            artifact_dir = vault_path / ".marginalia" / "rebuild-artifacts" / rebuilt_generation
            receipt_path = artifact_dir / "previous-semantic-materialization.json"
            assert receipt_path.is_file(), (
                "the rebuild must publish a generation-bound receipt for the "
                "generation it just replaced, even with no ledger history"
            )
            receipt = load_semantic_materialization(
                receipt_path, expected_graph_generation=original_generation
            )
            assert receipt is not None

            backend_name = kg_cli._resolve_pinned_backend(vault_path)
            candidate = rollback_candidate(vault_path, backend_name, None)
            assert candidate is not None
            assert candidate.to_generation == rebuilt_generation

            rollback = _jobs.submit(
                st,
                "rollback",
                label="rollback",
                params={"from_generation": rebuilt_generation},
            )
            for _ in range(2000):
                if rollback.status in ("done", "error"):
                    break
                await asyncio.sleep(0.01)

            assert rollback.status == "done", rollback.error
            assert rollback.result["swapped"] is True
            assert rollback.result["graph_generation"] == original_generation
        finally:
            try:
                st.close()
            except Exception:
                pass
            state_mod._STATE = None

    asyncio.run(_run())


@pytest.mark.parametrize("failure_kind", ["runtime", "audit"])
def test_run_rebuild_failure_restores_decision_sidefiles(
    tmp_path: Path,
    failure_kind: str,
) -> None:
    vault_path = _make_vault(tmp_path)

    async def _run() -> None:
        from okto_neuron.cli import kg as kg_cli
        from okto_neuron.predicates import PredicateRegistry
        from okto_neuron.server import state as state_mod

        st = _state(vault_path)
        state_mod._STATE = st
        try:
            _curation.register_runners()
            registry = PredicateRegistry(vault_path)
            registry.seed_builtins()
            original = registry.path.read_bytes()
            original_build = kg_cli._build_fresh_graph

            def failed_build(*_args, **_kwargs):
                registry.path.write_bytes(original + b" ")
                if failure_kind == "audit":
                    raise RebuildAuditFailed(
                        vault_path,
                        staging_path=vault_path / "graph.rebuild.lbug",
                        audit_status="semantic_failed",
                    )
                raise RuntimeError("fixture rebuild failure")

            kg_cli._build_fresh_graph = failed_build
            try:
                job = _jobs.submit(st, "rebuild", label="rebuild")
                for _ in range(2000):
                    if job.status in ("done", "error"):
                        break
                    await asyncio.sleep(0.01)
            finally:
                kg_cli._build_fresh_graph = original_build

            assert job.status == "error"
            expected_error = (
                "rebuilt graph failed integrity verification"
                if failure_kind == "audit"
                else "fixture rebuild failure"
            )
            assert expected_error in str(job.error)
            rebuild_state = json.loads(
                (vault_path / ".marginalia" / "rebuild.state.json").read_text(encoding="utf-8")
            )
            assert rebuild_state["phase"] == "failed"
            expected_type = "RebuildAuditFailed" if failure_kind == "audit" else "RuntimeError"
            assert rebuild_state["error"].startswith(f"{expected_type}: {expected_error}")
            assert rebuild_state["started_at"]
            assert registry.path.read_bytes() == original
            assert st.vault.store.is_closed is False
            assert st.draining is False
            assert not _curation._rebuild_recovery_dir(vault_path, job.id).exists()
        finally:
            try:
                st.close()
            except Exception:
                pass
            state_mod._STATE = None

    asyncio.run(_run())


def test_rehydrate_interrupted_rebuild_restores_policy_and_retains_staging(
    tmp_path: Path,
) -> None:
    from okto_neuron.cli import kg as kg_cli

    vault_path = _make_vault(tmp_path)
    state = _state(vault_path)
    previous_generation = str(state.vault.store._graph_handle.graph_generation or "")
    state.close()

    registry = PredicateRegistry(vault_path)
    registry.seed_builtins()
    original_registry = registry.path.read_bytes()
    original_policy = kg_cli._effective_semantic_fingerprint_triplet(vault_path)["semantic_policy"]
    job = _jobs.new_job("rebuild")
    job.status = "running"
    job.started_at = 123.0
    recovery_dir = _curation._rebuild_recovery_dir(vault_path, job.id)
    recovery_dir.mkdir(parents=True)
    kg_cli._write_semantic_policy_checkpoint(
        recovery_dir,
        graph_generation=previous_generation,
        semantic_policy_fingerprint=original_policy,
        files=kg_cli._capture_semantic_policy_sidefiles(vault_path),
    )
    registry.upsert(
        PredicateRecord(
            label="interrupted_predicate",
            lifecycle="provisional",
            definition="A predicate discovered by the interrupted rebuild.",
            direction="unknown",
            symmetric=None,
            signatures=(),
            support_count=0,
            samples=(),
            confidence=0.5,
            provenance=PredicateDecisionProvenance(
                source="model",
                decision_id="interrupted-decision",
                judge_model="fixture",
                prompt_version="fixture",
                semantic_policy_fingerprint=original_policy,
                created_at="2026-07-19T00:00:00+00:00",
            ),
        )
    )
    assert registry.path.read_bytes() != original_registry

    staging_path = vault_path / "graph.rebuild.lbug"
    staging_path.write_bytes(b"partial staging")
    staging_path.with_name(f"{staging_path.name}.wal").write_bytes(b"partial wal")
    state_path = vault_path / ".marginalia" / "rebuild.state.json"
    kg_cli._write_rebuild_state(
        state_path,
        {
            "phase": "in_progress",
            "graph_generation": "staging-generation",
            "files_done": ["notes/a.md"],
            "current_file": "notes/b.md",
        },
    )
    persisted = SimpleNamespace(vault_path=vault_path, curation_jobs=[job])
    _jobs.persist(persisted)

    _curation.register_runners()
    fresh = SimpleNamespace(vault_path=vault_path, curation_jobs=[])
    _jobs.rehydrate_jobs(fresh)

    restored = fresh.curation_jobs[0]
    assert restored.status == "error"
    assert "pre-swap recovery completed" in (restored.error or "")
    assert registry.path.read_bytes() == original_registry
    assert (
        kg_cli._effective_semantic_fingerprint_triplet(vault_path)["semantic_policy"]
        == original_policy
    )
    artifact_dir = vault_path / ".marginalia" / "rebuild-artifacts" / "staging-generation"
    retained = artifact_dir / "staging.interrupted.lbug"
    assert retained.read_bytes() == b"partial staging"
    assert retained.with_name(f"{retained.name}.wal").read_bytes() == b"partial wal"
    assert (artifact_dir / "previous-semantic-policy.json").is_file()
    validation = json.loads((artifact_dir / "validation.json").read_text(encoding="utf-8"))
    assert validation["phase"] == "process_interrupted"
    assert validation["job_id"] == job.id
    assert validation["restored_semantic_policy_fingerprint"] == original_policy
    assert not staging_path.exists()
    assert not recovery_dir.exists()


def test_rehydrate_interrupted_rebuild_never_restores_across_swap_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron.cli import kg as kg_cli

    vault_path = _make_vault(tmp_path)
    state = _state(vault_path)
    previous_generation = str(state.vault.store._graph_handle.graph_generation or "")
    state.close()
    registry = PredicateRegistry(vault_path)
    registry.seed_builtins()
    original_registry = registry.path.read_bytes()
    original_policy = kg_cli._effective_semantic_fingerprint_triplet(vault_path)["semantic_policy"]
    job = _jobs.new_job("rebuild")
    job.status = "running"
    recovery_dir = _curation._rebuild_recovery_dir(vault_path, job.id)
    recovery_dir.mkdir(parents=True)
    kg_cli._write_semantic_policy_checkpoint(
        recovery_dir,
        graph_generation=previous_generation,
        semantic_policy_fingerprint=original_policy,
        files=kg_cli._capture_semantic_policy_sidefiles(vault_path),
    )
    mutated_registry = original_registry + b" "
    registry.path.write_bytes(mutated_registry)
    staging_path = vault_path / "graph.rebuild.lbug"
    staging_path.write_bytes(b"ambiguous staging")
    persisted = SimpleNamespace(vault_path=vault_path, curation_jobs=[job])
    _jobs.persist(persisted)
    monkeypatch.setattr(
        kg_cli,
        "_graph_identity_at_path",
        lambda _path: SimpleNamespace(graph_generation="new-generation"),
    )

    _curation.register_runners()
    fresh = SimpleNamespace(vault_path=vault_path, curation_jobs=[])
    _jobs.rehydrate_jobs(fresh)

    restored = fresh.curation_jobs[0]
    assert restored.status == "error"
    assert "reached or passed the swap boundary" in (restored.error or "")
    assert registry.path.read_bytes() == mutated_registry
    assert staging_path.read_bytes() == b"ambiguous staging"
    assert recovery_dir.exists()


# ── 2. run_heal end-to-end: deterministic copy-fold + swap (NO LLM) ─────────────
def test_run_heal_collapses_variant_onto_canonical_and_remaps_edge(tmp_path: Path) -> None:
    """Seed the LIVE graph with a variant + canonical + an edge between them, confirm
    an authority record merging them, then submit a heal job. The deterministic copy
    must drop the variant, keep the canonical, and remap the edge — and the swap must
    leave the daemon serving the collapsed graph. Driven through the real job queue so
    the writer_lock + to_thread + marshaled-swap path is exercised end to end."""
    vault_path = _make_vault(tmp_path)

    async def _run() -> None:
        from okto_neuron.server import state as state_mod

        st = _state(vault_path)
        state_mod._STATE = st
        try:
            _curation.register_runners()

            # Seed the LIVE graph directly through the daemon handle.
            store = st.vault.store
            store.add_node(Node(id="canon", type="Place", title="United States"))
            store.add_node(Node(id="var", type="Place", title="USA"))
            store.add_node(Node(id="city", type="Place", title="Austin"))
            store.add_edge(
                Edge(
                    id=sha256_hex("edge", "city", "located_in", "var"),
                    type="located_in",
                    src="city",
                    dst="var",
                )
            )

            # Confirm the merge off-graph (var -> canon).
            authority = AuthorityIndex(vault_path / ".marginalia" / AUTHORITY_DIRNAME)
            authority.upsert(
                AuthorityRecord(
                    cluster_id="c1",
                    canonical_id="canon",
                    canonical_name="United States",
                    member_ids=("canon", "var"),
                    variants=("USA",),
                    exact_match_pairs=(("canon", "var"),),
                )
            )

            job = _jobs.submit(st, "heal", label="heal")
            for _ in range(2000):
                if job.status in ("done", "error"):
                    break
                await asyncio.sleep(0.01)

            assert job.status == "done", job.error
            assert job.result["swapped"] is True
            assert job.result["heal"]["nodes_dropped"] == 1

            state_path = vault_path / ".marginalia" / "rebuild.state.json"
            data = json.loads(state_path.read_text(encoding="utf-8"))
            assert data["phase"] == "complete"

            assert st.draining is False
            assert not st.vault.store.is_closed
            ids = {n.id for n in st.vault.store.list_nodes()}
            assert "var" not in ids  # variant merged away
            assert {"canon", "city"} <= ids
            # the edge was remapped onto the canonical
            edges = list(st.vault.store.list_edges())
            assert any(e.src == "city" and e.dst == "canon" for e in edges)
            assert not any(e.dst == "var" for e in edges)
        finally:
            try:
                st.close()
            except Exception:
                pass
            state_mod._STATE = None

    asyncio.run(_run())


# ── 3. daemon heal is fence-equivalent to the CLI heal (ADR 0039) ───────────────
def _seed_heal_fixture(st: ServerState, vault_path: Path) -> None:
    """Variant + canonical + an edge onto the variant, with the merge confirmed."""
    store = st.vault.store
    store.add_node(Node(id="canon", type="Place", title="United States"))
    store.add_node(Node(id="var", type="Place", title="USA"))
    store.add_node(Node(id="city", type="Place", title="Austin"))
    store.add_edge(
        Edge(
            id=sha256_hex("edge", "city", "located_in", "var"),
            type="located_in",
            src="city",
            dst="var",
        )
    )
    authority = AuthorityIndex(vault_path / ".marginalia" / AUTHORITY_DIRNAME)
    authority.upsert(
        AuthorityRecord(
            cluster_id="c1",
            canonical_id="canon",
            canonical_name="United States",
            member_ids=("canon", "var"),
            variants=("USA",),
            exact_match_pairs=(("canon", "var"),),
        )
    )


async def _await_job(job) -> None:
    for _ in range(2000):
        if job.status in ("done", "error"):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("heal job never finished")


class _DirectJob:
    """Minimal job view for calling a runner directly (no queue in front of it)."""

    def progress(self, _stage: str) -> None:
        return None

    def release_vault_lease_for_swap(self) -> None:
        return None


def test_run_heal_refuses_a_fenced_generation(tmp_path: Path) -> None:
    """A generation already proven damaged must not be canonicalized into a fresh
    graph that then LOOKS clean — the same fence the CLI heal enforces.

    Called DIRECTLY, not through ``_jobs.submit``: the queue's own
    ``require_write_allowed`` gate would fence it before the runner ran, which would
    say nothing about ``run_heal`` itself.
    """
    vault_path = _make_vault(tmp_path)

    async def _run() -> None:
        from okto_neuron.store.integrity_state import IntegrityFenceError

        st = _state(vault_path)
        st.loop = asyncio.get_running_loop()
        try:
            _seed_heal_fixture(st, vault_path)
            write_integrity_state(
                vault_path,
                GraphIntegrityState(
                    status=AuditStatus.FAILED,
                    graph_generation=str(st.vault.store._graph_handle.graph_generation),
                    writer_fenced=True,
                    reason="seeded adjacency damage",
                ),
            )

            with pytest.raises(IntegrityFenceError, match="integrity_fenced"):
                await asyncio.to_thread(_curation.run_heal, st, _DirectJob())

            # Live graph untouched: the variant is still there, nothing was staged.
            assert "var" in {n.id for n in st.vault.store.list_nodes()}
            assert not (vault_path / "graph.heal.lbug").exists()
        finally:
            try:
                st.close()
            except Exception:
                pass

    asyncio.run(_run())


def test_run_heal_audits_staging_before_the_swap(tmp_path: Path) -> None:
    """A staging graph that fails the integrity audit must never reach live."""
    vault_path = _make_vault(tmp_path)

    async def _run() -> None:
        import okto_neuron.cli.kg as kg_cli
        from okto_neuron.server import state as state_mod

        st = _state(vault_path)
        state_mod._STATE = st
        audited: list[Path] = []
        orig_audit = kg_cli._audit_rebuild_graph_path

        def failing_audit(path, **kwargs):
            audited.append(Path(path))
            return (
                SimpleNamespace(verified=False, status=AuditStatus.FAILED),
                {"stage": kwargs.get("stage")},
            )

        try:
            _curation.register_runners()
            _seed_heal_fixture(st, vault_path)
            kg_cli._audit_rebuild_graph_path = failing_audit
            try:
                job = _jobs.submit(st, "heal", label="heal")
                await _await_job(job)
            finally:
                kg_cli._audit_rebuild_graph_path = orig_audit

            assert job.status == "error"
            assert "staging graph failed integrity verification" in (job.error or "")
            # The audit ran against the STAGING file, before any swap.
            assert audited == [vault_path / "graph.heal.lbug"]
            assert "var" in {n.id for n in st.vault.store.list_nodes()}
            assert not (vault_path / "graph.heal.lbug").exists()
        finally:
            try:
                st.close()
            except Exception:
                pass
            state_mod._STATE = None

    asyncio.run(_run())


def test_run_heal_publishes_the_new_generation_in_the_sidecar(tmp_path: Path) -> None:
    """After a daemon heal the sidecar must name the generation the heal MINTED —
    naming the superseded one would read back as stale and fence later writes."""
    vault_path = _make_vault(tmp_path)

    async def _run() -> None:
        from okto_neuron.server import state as state_mod

        st = _state(vault_path)
        state_mod._STATE = st
        try:
            _curation.register_runners()
            _seed_heal_fixture(st, vault_path)
            before = str(st.vault.store._graph_handle.graph_generation)

            job = _jobs.submit(st, "heal", label="heal")
            await _await_job(job)
            assert job.status == "done", job.error

            after = str(st.vault.store._graph_handle.graph_generation)
            assert after != before, "the heal must mint a fresh generation"

            sidecar = load_integrity_state(vault_path)
            assert sidecar.graph_generation == after
            assert sidecar.status is AuditStatus.VERIFIED
            assert sidecar.writer_fenced is False
            # The verdict came from the heal's OWN post-swap audit, published under
            # the runtime fence before any request could observe the new graph.
            assert st.integrity_last_audit is not None
            assert st.integrity_last_audit.verified is True
        finally:
            try:
                st.close()
            except Exception:
                pass
            state_mod._STATE = None

    asyncio.run(_run())


# ── 4. ingest-during-rebuild gate: draining set during, cleared after ───────────
def test_run_rebuild_sets_draining_during_and_clears_after(tmp_path: Path) -> None:
    vault_path = _make_vault(tmp_path)

    async def _run() -> None:
        from okto_neuron.server import state as state_mod

        st = _state(vault_path)
        state_mod._STATE = st
        draining_seen: list[bool] = []
        try:
            _curation.register_runners()
            import okto_neuron.cli.kg as kg_cli

            orig_build = kg_cli._build_fresh_graph

            def patched_build(*args, **kwargs):
                # Observe draining DURING the build phase (writer would 503 now).
                draining_seen.append(st.draining)

                def stub(path, store):
                    pass

                kwargs["ingest"] = stub
                kwargs.pop("extractor", None)
                return orig_build(*args, **kwargs)

            kg_cli._build_fresh_graph = patched_build
            try:
                job = _jobs.submit(st, "rebuild", label="rebuild")
                for _ in range(2000):
                    if job.status in ("done", "error"):
                        break
                    await asyncio.sleep(0.01)
            finally:
                kg_cli._build_fresh_graph = orig_build

            assert job.status == "done", job.error
            assert draining_seen == [True], "draining must be set during the build"
            assert st.draining is False, "draining must clear after reopen"
        finally:
            try:
                st.close()
            except Exception:
                pass
            state_mod._STATE = None

    asyncio.run(_run())


def test_interrupted_rebuild_without_checkpoint_still_terminates_cleanly(
    tmp_path: Path,
) -> None:
    """A rebuild killed before it wrote its checkpoint must not wedge the queue.

    The recovery hook fails closed (no checkpoint = it cannot prove the pre-job
    generation, so it restores nothing). The queue item must still reach a
    TERMINAL state carrying the reason, with ``finished_at`` stamped and the
    live graph left exactly as it was — no half-installed generation, and no
    job stuck in ``running`` across the restart.
    """
    from okto_neuron.cli import kg as kg_cli

    vault_path = _make_vault(tmp_path)
    state = _state(vault_path)
    live_generation = str(state.vault.store._graph_handle.graph_generation or "")
    state.close()

    registry = PredicateRegistry(vault_path)
    registry.seed_builtins()
    original_registry = registry.path.read_bytes()
    original_policy = kg_cli._effective_semantic_fingerprint_triplet(vault_path)["semantic_policy"]

    job = _jobs.new_job("rebuild")
    job.status = "running"
    job.started_at = 123.0
    # Deliberately NO recovery dir / checkpoint: the crash landed before it.
    assert not _curation._rebuild_recovery_dir(vault_path, job.id).exists()

    _jobs.persist(SimpleNamespace(vault_path=vault_path, curation_jobs=[job]))

    _curation.register_runners()
    fresh = SimpleNamespace(vault_path=vault_path, curation_jobs=[])
    _jobs.rehydrate_jobs(fresh)

    restored = fresh.curation_jobs[0]
    # Terminal, not resumable, and the failure reason is retained as evidence.
    assert restored.status == "error"
    assert restored.progress == "error"
    assert restored.finished_at is not None
    assert "interrupted by process restart" in (restored.error or "")
    assert "missing pre-job semantic checkpoint" in (restored.error or "")

    # Nothing was installed or rewritten on the way out.
    assert registry.path.read_bytes() == original_registry
    assert (
        kg_cli._effective_semantic_fingerprint_triplet(vault_path)["semantic_policy"]
        == original_policy
    )
    state_after = _state(vault_path)
    try:
        assert str(state_after.vault.store._graph_handle.graph_generation or "") == live_generation
    finally:
        state_after.close()


# ── 5. M4 grafx daemon parity — same runners, a grafx-pinned vault ──────────────
# ``pytest.importorskip("okto_grafx")`` runs inside each test (not at module
# scope) so the Ladybug tests above still run when the ``[grafx]`` extra isn't
# installed; only this section skips.


def _make_vault_grafx(tmp_path: Path) -> Path:
    vault_path = tmp_path / "vault"
    assert kg_init(vault_path, backend="grafx") == 0
    # Same cache-drop as ``_make_vault`` — ``_STORE_CACHE`` is backend-generic
    # (``store/vault.py``'s ``_open_vault``), so a lingering cached
    # ``IndexedStore``/``GrafxStore`` from ``kg_init`` must go too, mirroring a
    # real daemon startup.
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()
    bootstrap_module._bootstrap_cache.clear()
    (vault_path / "notes" / "a.md").write_text("# A\n", encoding="utf-8")
    (vault_path / "notes" / "b.md").write_text("# B\n", encoding="utf-8")
    return vault_path


def _set_stub_embedding_provider(vault_path: Path) -> None:
    """Point the vault's embedding config at the deterministic, network-free
    ``StubEmbedder`` (``okto_neuron.embed``) instead of the ``fastembed``
    default, so ``run_reembed`` never touches a real model."""
    import yaml as _yaml

    config_path = vault_path / "okto-neuron.yaml"
    data = _yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    embedding = dict(data.get("embedding") or {})
    embedding["provider"] = "stub"
    embedding["dimension"] = 384
    data["embedding"] = embedding
    config_path.write_text(_yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def test_run_rebuild_grafx_swaps_onto_grafx_backend(tmp_path: Path) -> None:
    """``run_rebuild`` against a grafx-pinned vault: builds into a staged
    ``GrafxStore`` via ``_swap_construction_for``, commits through
    ``GrafxStaging`` (not the Ladybug-only ``_swap_rebuilt_graph``), and
    reports the REAL grafx backup path (``graph.grafx.rebuild``), not the
    Ladybug-shaped literal ``_prepare_rebuild_backup`` guesses."""
    pytest.importorskip("okto_grafx")
    vault_path = _make_vault_grafx(tmp_path)

    async def _run() -> None:
        from okto_neuron.server import state as state_mod

        st = _state(vault_path)
        state_mod._STATE = st
        try:
            _curation.register_runners()
            _jobs.register_runner(
                "reconcile-propose",
                lambda _state, _job: {"count": 0, "outcome": {"state": "complete"}},
                writes=False,
                verified_snapshot=True,
            )

            def stub_ingest(path: Path, store: object) -> None:
                rel = path.relative_to(vault_path).as_posix()
                store.add_node(Node(id=f"n-{rel}", type="Concept", title=rel))

            import okto_neuron.cli.kg as kg_cli

            orig_build = kg_cli._build_fresh_graph

            def patched_build(*args, **kwargs):
                kwargs["ingest"] = stub_ingest
                kwargs.pop("extractor", None)
                return orig_build(*args, **kwargs)

            kg_cli._build_fresh_graph = patched_build
            try:
                job = _jobs.submit(st, "rebuild", label="rebuild")
                for _ in range(2000):
                    if job.status in ("done", "error"):
                        break
                    await asyncio.sleep(0.01)
            finally:
                kg_cli._build_fresh_graph = orig_build

            assert job.status == "done", job.error
            assert job.result["swapped"] is True
            backup_path = Path(str(job.result["backup_path"]))
            assert backup_path.name == "graph.grafx.rebuild", backup_path
            assert backup_path.is_dir()

            state_path = vault_path / ".marginalia" / "rebuild.state.json"
            data = json.loads(state_path.read_text(encoding="utf-8"))
            assert data["phase"] == "complete"
            assert data["final_audit"]["status"] == "verified"
            assert data["post_swap_audit"]["status"] == "verified"

            assert st.draining is False
            assert not st.vault.store.is_closed
            ids = {n.id for n in st.vault.store.list_nodes()}
            assert "n-notes/a.md" in ids and "n-notes/b.md" in ids
            assert (vault_path / "graph.grafx").is_dir()
        finally:
            try:
                st.close()
            except Exception:
                pass
            state_mod._STATE = None

    asyncio.run(_run())


def test_run_heal_grafx_collapses_variant_onto_canonical(tmp_path: Path) -> None:
    """``run_heal`` against a grafx-pinned vault: the copy-fold builds into a
    staged ``GrafxStore`` and commits through ``GrafxStaging`` — same
    collapse behavior as the Ladybug heal, on a directory-shaped graph."""
    pytest.importorskip("okto_grafx")
    vault_path = _make_vault_grafx(tmp_path)

    async def _run() -> None:
        from okto_neuron.server import state as state_mod

        st = _state(vault_path)
        state_mod._STATE = st
        try:
            _curation.register_runners()
            _seed_heal_fixture(st, vault_path)

            job = _jobs.submit(st, "heal", label="heal")
            await _await_job(job)

            assert job.status == "done", job.error
            assert job.result["swapped"] is True
            assert job.result["heal"]["nodes_dropped"] == 1

            state_path = vault_path / ".marginalia" / "rebuild.state.json"
            data = json.loads(state_path.read_text(encoding="utf-8"))
            assert data["phase"] == "complete"

            assert st.draining is False
            assert not st.vault.store.is_closed
            ids = {n.id for n in st.vault.store.list_nodes()}
            assert "var" not in ids
            assert {"canon", "city"} <= ids
            edges = list(st.vault.store.list_edges())
            assert any(e.src == "city" and e.dst == "canon" for e in edges)
            assert not any(e.dst == "var" for e in edges)

            backup_dir = vault_path / "graph.grafx.bak"
            assert backup_dir.is_dir(), "heal must report+leave its grafx backup directory"
        finally:
            try:
                st.close()
            except Exception:
                pass
            state_mod._STATE = None

    asyncio.run(_run())


def test_run_reembed_grafx_recomputes_vectors(tmp_path: Path) -> None:
    """``run_reembed`` against a grafx-pinned vault, using the deterministic
    ``StubEmbedder`` (no network/model download): the swap commits through
    ``GrafxStaging`` and the live graph keeps its nodes with fresh vectors."""
    pytest.importorskip("okto_grafx")
    vault_path = _make_vault_grafx(tmp_path)
    _set_stub_embedding_provider(vault_path)

    async def _run() -> None:
        from okto_neuron.server import state as state_mod

        st = _state(vault_path)
        state_mod._STATE = st
        try:
            _curation.register_runners()
            # A placeholder (wrong-width) vector so the copy has something to
            # RECOMPUTE — ``copy_graph_reembedding`` copies an
            # ``embedding is None`` node verbatim (never fabricates a vector
            # for one that never had one), so an already-embedded node is
            # what actually exercises the recompute path.
            st.vault.store.add_node(Node(id="n1", type="Concept", title="Alpha", embedding=[0.0] * 8))
            st.vault.store.add_node(Node(id="n2", type="Concept", title="Beta", embedding=[0.0] * 8))

            job = _jobs.submit(st, "reembed", label="reembed")
            await _await_job(job)

            assert job.status == "done", job.error
            assert job.result["swapped"] is True

            assert st.draining is False
            assert not st.vault.store.is_closed
            ids = {n.id for n in st.vault.store.list_nodes()}
            assert {"n1", "n2"} <= ids
            for node in st.vault.store.list_nodes():
                assert node.embedding is not None
                assert len(node.embedding) == 384
            assert (vault_path / "graph.grafx.bak").is_dir()
        finally:
            try:
                st.close()
            except Exception:
                pass
            state_mod._STATE = None

    asyncio.run(_run())


def test_run_rollback_grafx_restores_previous_swap_backup(tmp_path: Path) -> None:
    """``run_rollback`` against a grafx-pinned vault (``_run_rollback_non_ladybug``):
    restores the backup directory the last rebuild produced
    (``graph.grafx.rebuild``), leaves that source checkpoint intact, and
    reports a fresh displaced-graph backup for the generation it replaced —
    matching the job contract the Ladybug rollback test above asserts (202
    accepted at the HTTP layer / job status ``done`` here, generation
    reverted, content intact)."""
    pytest.importorskip("okto_grafx")
    vault_path = _make_vault_grafx(tmp_path)

    async def _run() -> None:
        import okto_neuron.cli.kg as kg_cli
        from okto_neuron.server import state as state_mod

        st = _state(vault_path)
        state_mod._STATE = st
        try:
            _curation.register_runners()
            st.vault.store.add_node(Node(id="before", type="Concept", title="Before"))
            original_generation = str(st.vault.store.generation())

            def stub_ingest(path: Path, store: object) -> None:
                rel = path.relative_to(vault_path).as_posix()
                store.add_node(Node(id=f"after-{rel}", type="Concept", title=rel))

            orig_build = kg_cli._build_fresh_graph

            def patched_build(*args, **kwargs):
                kwargs["ingest"] = stub_ingest
                kwargs.pop("extractor", None)
                return orig_build(*args, **kwargs)

            kg_cli._build_fresh_graph = patched_build
            try:
                rebuild = _jobs.submit(st, "rebuild", label="rebuild")
                await _await_job(rebuild)
            finally:
                kg_cli._build_fresh_graph = orig_build

            assert rebuild.status == "done", rebuild.error
            rebuilt_generation = str(rebuild.result["graph_generation"])
            assert rebuilt_generation != original_generation
            assert "before" not in {node.id for node in st.vault.store.list_nodes()}

            rebuild_backup = vault_path / "graph.grafx.rebuild"
            assert rebuild_backup.is_dir()
            rebuild_backup_mtime = rebuild_backup.stat().st_mtime

            rollback = _jobs.submit(
                st,
                "rollback",
                label="rollback",
                params={"from_generation": rebuilt_generation},
            )
            await _await_job(rollback)

            assert rollback.status == "done", rollback.error
            assert rollback.result["graph_generation"] == original_generation
            assert rollback.result["from_generation"] == rebuilt_generation
            ids = {node.id for node in st.vault.store.list_nodes()}
            assert "before" in ids
            assert "after-notes/a.md" not in ids

            # The rollback source checkpoint was COPIED, not consumed.
            assert rebuild_backup.is_dir()
            assert rebuild_backup.stat().st_mtime == rebuild_backup_mtime

            # The generation rollback replaced is now the new displaced backup.
            displaced = Path(str(rollback.result["displaced_graph"]))
            assert displaced.is_dir()
            assert displaced.name == "graph.grafx.rollback"

            rollback_state = json.loads(
                (vault_path / ".marginalia" / "rollback.state.json").read_text(encoding="utf-8")
            )
            assert rollback_state["phase"] == "complete"
        finally:
            try:
                st.close()
            except Exception:
                pass
            state_mod._STATE = None

    asyncio.run(_run())


# ── 6. M5 neo4j daemon parity — same runners, a neo4j-pinned vault ──────────────
# Requires a real Neo4j server (``OKTO_NEURON_TEST_NEO4J_URI`` + a credential env
# name in ``OKTO_NEURON_TEST_NEO4J_CREDENTIAL_ENV``) — skipped cleanly otherwise,
# matching the grafx section's own optional-dependency handling above.


def _make_vault_neo4j(tmp_path: Path):
    import os

    pytest.importorskip("neo4j")
    uri = os.environ.get("OKTO_NEURON_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("OKTO_NEURON_TEST_NEO4J_URI is not set")
    credential_env = os.environ.get("OKTO_NEURON_TEST_NEO4J_CREDENTIAL_ENV")

    vault_path = tmp_path / "vault"
    assert (
        kg_init(
            vault_path,
            backend="neo4j",
            storage_uri=uri,
            storage_credential_env=credential_env,
            storage_database="neo4j",
        )
        == 0
    )
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()
    bootstrap_module._bootstrap_cache.clear()
    (vault_path / "notes" / "a.md").write_text("# A\n", encoding="utf-8")
    (vault_path / "notes" / "b.md").write_text("# B\n", encoding="utf-8")
    return vault_path


def test_run_rollback_neo4j_flips_generation_pointer_back(tmp_path: Path) -> None:
    """``run_rollback`` against a neo4j-pinned vault (``_run_rollback_neo4j``):
    a rebuild swaps the live ``graph_generation`` pointer forward via
    ``Neo4jStaging.commit``, then rollback flips it back to the generation
    ``commit`` stashed under the metadata singleton's ``backup_tag`` — no
    filesystem backup, no staged copy, just the metadata pointer round-trip
    (M5 spec — see ``_run_rollback_neo4j``'s own docstring)."""
    vault_path = _make_vault_neo4j(tmp_path)

    async def _run() -> None:
        import okto_neuron.cli.kg as kg_cli
        from okto_neuron.server import state as state_mod

        st = _state(vault_path)
        state_mod._STATE = st
        try:
            _curation.register_runners()
            st.vault.store.add_node(Node(id="before", type="Concept", title="Before"))
            original_generation = str(st.vault.store.generation())

            def stub_ingest(path: Path, store: object) -> None:
                rel = path.relative_to(vault_path).as_posix()
                store.add_node(Node(id=f"after-{rel}", type="Concept", title=rel))

            orig_build = kg_cli._build_fresh_graph

            def patched_build(*args, **kwargs):
                kwargs["ingest"] = stub_ingest
                kwargs.pop("extractor", None)
                return orig_build(*args, **kwargs)

            kg_cli._build_fresh_graph = patched_build
            try:
                rebuild = _jobs.submit(st, "rebuild", label="rebuild")
                await _await_job(rebuild)
            finally:
                kg_cli._build_fresh_graph = orig_build

            assert rebuild.status == "done", rebuild.error
            rebuilt_generation = str(rebuild.result["graph_generation"])
            assert rebuilt_generation != original_generation
            assert "before" not in {node.id for node in st.vault.store.list_nodes()}

            rollback = _jobs.submit(
                st,
                "rollback",
                label="rollback",
                params={"from_generation": rebuilt_generation},
            )
            await _await_job(rollback)

            assert rollback.status == "done", rollback.error
            assert rollback.result["graph_generation"] == original_generation
            assert rollback.result["from_generation"] == rebuilt_generation
            assert str(st.vault.store.generation()) == original_generation
            ids = {node.id for node in st.vault.store.list_nodes()}
            assert "before" in ids
            assert "after-notes/a.md" not in ids

            rollback_state = json.loads(
                (vault_path / ".marginalia" / "rollback.state.json").read_text(encoding="utf-8")
            )
            assert rollback_state["phase"] == "complete"
        finally:
            try:
                st.close()
            except Exception:
                pass
            state_mod._STATE = None

    asyncio.run(_run())
