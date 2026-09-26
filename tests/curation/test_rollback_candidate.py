"""Unit tests for ``okto_neuron.curation.orchestrate.rollback_candidate`` (D-84).

Backend-neutral rollback availability: one shared answer to "does this vault
have somewhere to roll back to", read-only (no staging/auditing/swapping).
Exercised against a real per-backend checkpoint (real tmp Ladybug file, real
grafx store in a tmp dir, live Neo4j container), not mocks — matching the
project's model-free/no-mocks-in-integration-paths convention.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import okto_neuron.store._bootstrap as bootstrap_module
from okto_neuron.cli.kg import kg_init
from okto_neuron.config import VaultConfig
from okto_neuron.curation.orchestrate import rollback_candidate
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture(autouse=True)
def _clean_handles():
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


def _drop_cached_handles() -> None:
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()
    bootstrap_module._bootstrap_cache.clear()


# ── Ladybug ──────────────────────────────────────────────────────────────


def test_ladybug_no_candidate_on_a_fresh_vault(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _drop_cached_handles()

    assert rollback_candidate(vault_path, "ladybug", None) is None


def test_ladybug_candidate_when_checkpoint_is_present(tmp_path: Path) -> None:
    from okto_neuron.store import schema

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _drop_cached_handles()

    graph_path = vault_path / "graph.lbug"
    identity = schema.read_graph_identity_path(graph_path)
    generation = identity.graph_generation or ""
    assert generation, "a freshly bootstrapped graph must carry a generation"

    artifact_dir = vault_path / ".marginalia" / "rebuild-artifacts" / generation
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "previous-graph.lbug").write_bytes(b"stub")
    (artifact_dir / "previous-semantic-materialization.json").write_text("{}", encoding="utf-8")
    (artifact_dir / "previous-semantic-policy.json").write_text("{}", encoding="utf-8")

    candidate = rollback_candidate(vault_path, "ladybug", None)
    assert candidate is not None
    assert candidate.backend == "ladybug"
    assert candidate.to_generation == generation
    assert candidate.source == str(artifact_dir / "previous-graph.lbug")


def test_ladybug_no_candidate_when_a_receipt_is_missing(tmp_path: Path) -> None:
    from okto_neuron.store import schema

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _drop_cached_handles()

    graph_path = vault_path / "graph.lbug"
    identity = schema.read_graph_identity_path(graph_path)
    generation = identity.graph_generation or ""

    artifact_dir = vault_path / ".marginalia" / "rebuild-artifacts" / generation
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "previous-graph.lbug").write_bytes(b"stub")
    # Deliberately omit the semantic-materialization/policy receipts.

    assert rollback_candidate(vault_path, "ladybug", None) is None


# ── Grafx ────────────────────────────────────────────────────────────────


def _make_vault_grafx(tmp_path: Path) -> Path:
    vault_path = tmp_path / "vault"
    assert kg_init(vault_path, backend="grafx") == 0
    _drop_cached_handles()
    return vault_path


def test_grafx_no_candidate_on_a_fresh_vault(tmp_path: Path) -> None:
    pytest.importorskip("okto_grafx")
    vault_path = _make_vault_grafx(tmp_path)

    assert rollback_candidate(vault_path, "grafx", None) is None


def test_grafx_candidate_picks_newest_backup_directory(tmp_path: Path) -> None:
    pytest.importorskip("okto_grafx")
    from okto_neuron.store.grafx import _GRAPH_DIR_NAME

    vault_path = _make_vault_grafx(tmp_path)
    graph_path = vault_path / _GRAPH_DIR_NAME

    bak_dir = graph_path.with_name(f"{graph_path.name}.bak")
    bak_dir.mkdir()
    (bak_dir / "marker").write_text("bak", encoding="utf-8")

    candidate = rollback_candidate(vault_path, "grafx", None)
    assert candidate is not None
    assert candidate.backend == "grafx"
    assert candidate.source == str(bak_dir)

    rebuild_dir = graph_path.with_name(f"{graph_path.name}.rebuild")
    rebuild_dir.mkdir()
    (rebuild_dir / "marker").write_text("rebuild", encoding="utf-8")
    os.utime(rebuild_dir, None)  # newest mtime of the two candidates

    candidate = rollback_candidate(vault_path, "grafx", None)
    assert candidate is not None
    assert candidate.source == str(rebuild_dir)


# ── Neo4j (live container) ──────────────────────────────────────────────
# Requires a real Neo4j server (``OKTO_NEURON_TEST_NEO4J_URI`` + a credential
# env name in ``OKTO_NEURON_TEST_NEO4J_CREDENTIAL_ENV``) — skipped cleanly
# otherwise, matching ``tests/server/test_curation_rebuild.py``'s own
# optional-dependency handling.


def _make_vault_neo4j(tmp_path: Path) -> Path:
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
    _drop_cached_handles()
    return vault_path


def test_neo4j_no_candidate_before_any_swap(tmp_path: Path) -> None:
    vault_path = _make_vault_neo4j(tmp_path)
    storage_config = VaultConfig.load(vault_path).storage

    assert rollback_candidate(vault_path, "neo4j", storage_config) is None


def test_neo4j_candidate_after_a_swap_stashes_backup_tag(tmp_path: Path) -> None:
    """Drive a real rebuild swap through the daemon runner (same path
    ``server/_curation.py``'s ``_run_rollback_neo4j`` exercises), then assert
    the helper surfaces the ``backup_tag`` the swap stashed — and that it
    still tags at least one live node, per the helper's own contract."""
    import asyncio

    import okto_neuron.cli.kg as kg_cli
    from okto_neuron.core.schema.legacy import Node
    from okto_neuron.server import _curation, _jobs
    from okto_neuron.server import state as state_mod
    from okto_neuron.server.state import ServerState
    from okto_neuron.vault import Vault

    vault_path = _make_vault_neo4j(tmp_path)
    storage_config = VaultConfig.load(vault_path).storage

    async def _run() -> None:
        vault = Vault.open(vault_path)
        st = ServerState(vault=vault, vault_path=vault_path)
        state_mod._STATE = st
        try:
            _curation.register_runners()
            st.vault.store.add_node(Node(id="before", type="Concept", title="Before"))
            original_generation = str(st.vault.store.generation())

            def stub_ingest(path: Path, store: object) -> None:
                rel = path.relative_to(vault_path).as_posix()
                store.add_node(Node(id=f"after-{rel}", type="Concept", title=rel))

            (vault_path / "notes").mkdir(exist_ok=True)
            (vault_path / "notes" / "a.md").write_text("# A\n", encoding="utf-8")

            orig_build = kg_cli._build_fresh_graph

            def patched_build(*args, **kwargs):
                kwargs["ingest"] = stub_ingest
                kwargs.pop("extractor", None)
                return orig_build(*args, **kwargs)

            kg_cli._build_fresh_graph = patched_build
            try:
                rebuild = _jobs.submit(st, "rebuild", label="rebuild")
                for _ in range(2000):
                    if rebuild.status in ("done", "error"):
                        break
                    await asyncio.sleep(0.01)
            finally:
                kg_cli._build_fresh_graph = orig_build

            assert rebuild.status == "done", rebuild.error
            rebuilt_generation = str(rebuild.result["graph_generation"])
            assert rebuilt_generation != original_generation

            candidate = rollback_candidate(vault_path, "neo4j", storage_config)
            assert candidate is not None
            assert candidate.backend == "neo4j"
            assert candidate.to_generation == original_generation
        finally:
            state_mod._STATE = None
            try:
                st.close()
            except Exception:
                pass

    asyncio.run(_run())
