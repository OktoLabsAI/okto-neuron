"""Tests for :mod:`okto_neuron.curation.orchestrate` (M2b spec §4 bullet 3).

The spec's own suggestion for this file was a baseline/candidate A/B diff:
run "today's pre-refactor function" against a fresh fixture copy, then run
``orchestrate.*`` against another copy, and diff. That baseline is not
reachable any more — M2b's own re-routing (spec §3) means ``cli/kg.py`` and
``reconcile/heal.py`` now call *through* ``orchestrate.py`` themselves, so
there is no separately-callable "old" implementation left to diff against
(only the relocated swap primitives in ``store/staging.py`` are still
reachable as thin re-exports, and those were never rewritten — see
``tests/cli/test_kg_rebuild.py``'s own sidecar/move-family tests for that).

So this file asserts what the spec's intent actually needs instead:

* **Determinism.** Two independent runs of ``orchestrate.rebuild``/``heal``/
  ``reembed`` against two separately-built, identically-reciped fixture
  vaults produce the same graph CONTENT (nodes/edges read back through a
  fresh store, sorted by id) and structurally identical results (once the
  genuinely run-specific fields — an ``audit_id`` uuid4 and a wall-clock
  ``duration_ms`` — are stripped).

  This is content-level, not raw-file-byte-level, determinism, and that
  distinction was checked empirically rather than assumed. Real production
  bootstrapping (``cli.kg._bootstrap_graph_at_path``) mints a fresh
  ``uuid4()`` graph identity on every call (``store/_bootstrap.py``'s
  ``schema.new_graph_identity()``), which is exactly why
  ``tests/cli/test_kg_rebuild_determinism.py``'s byte-identical assertion is
  ``xfail(strict=True)`` today even with a content-hash-derived stub ingest.
  To isolate "did ``orchestrate.py`` itself introduce nondeterminism" from
  that pre-existing, tracked randomness, the tests below inject a
  *pinned-identity* bootstrap fixture (``_pinned_bootstrap``) instead of the
  real one — legitimate because ``bootstrap_graph_at_path`` is already one
  of ``orchestrate.py``'s documented injectable seams (module docstring,
  "Import boundary"). But pinning the identity turned out NOT to be enough:
  a quick direct probe (two back-to-back ``ladybug.Database`` creations,
  identical DDL, identical pinned identity, zero application rows) still
  produced two different, same-sized files differing in 54 bytes starting
  at offset 28 — a per-creation header Ladybug/Kuzu itself embeds (a file
  UUID or creation timestamp), unrelated to graph_generation, node ids, or
  anything ``orchestrate.py`` controls. So raw ``graph.lbug`` byte-hash
  equality is not achievable for a freshly-bootstrapped Ladybug graph at
  all, pinned identity or not; comparing the graph's logical content after
  reopening it is the correct (and only honest) determinism check here.
* **Shape.** The audit payload dicts embedded in ``StagedSwapResult`` carry
  the same keys ``tests/cli/test_kg_rebuild.py`` asserts on
  ``rebuild.state.json``'s ``final_audit`` section (``stage``, ``status``,
  ``nodes_scanned``, ...) — ``orchestrate.py`` itself never writes a
  ``*.state.json`` file (that stays the caller's job, spec §2.3/§6), so
  "state.json shape" here means the shape of the dict a caller would embed
  into one.
* **Invariants** (spec §2.3): a pre-swap audit failure leaves live untouched
  and discards staging; a lock lost between acquisition and commit fails
  closed before any commit; a post-swap audit failure raises
  ``RebuildAuditFailed`` naming the backup path *without* restoring it,
  leaving the bad graph live; ``publish_integrity=False`` (reembed) skips
  the whole post-swap half. The first three are exercised directly against
  :func:`~okto_neuron.curation.orchestrate.finish_staged_swap` with tiny fake
  callables and plain byte-content stub files (no real Ladybug graph is
  needed to prove these are pure control-flow/filesystem invariants — the
  swap primitives underneath, in ``store/staging.py``, never inspect graph
  content); the fourth is proven twice, once the same cheap way and once
  through the real :func:`~okto_neuron.curation.orchestrate.reembed` to show
  the asymmetry survives the real code path too.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ladybug
import pytest

from okto_neuron.cli import kg as kg_cli
from okto_neuron.core.schema import Edge, Node, Provenance
from okto_neuron.curation import orchestrate
from okto_neuron.embed import StubEmbedder
from okto_neuron.errors import RebuildAuditFailed, VaultLockHeld
from okto_neuron.store import schema
from okto_neuron.store import vault as vault_module
from okto_neuron.store._bootstrap import VaultGraphHandle
from okto_neuron.store._bootstrap import _bootstrap_cache
from okto_neuron.store.integrity import AuditStatus, IntegrityAuditResult, IntegrityIssue
from okto_neuron.store.ladybug import LadybugStore, VaultConnection
from okto_neuron.store.rebuild_lock import acquire_rebuild_lock
from okto_neuron.store.staging import LadybugStaging


# ── global-cache hygiene (mirrors tests/cli/test_kg_rebuild.py's own fixture) ──
# LadybugStore/VaultConnection/the bootstrap cache are process-wide, keyed by
# vault_path. Every helper below closes what it opens, but a test that raises
# partway through (an assertion failure, an intentionally-triggered exception)
# can still leave a handle cached; a leaked handle from one test can then make
# a *different* test's identically-shaped tmp_path collide. Clear everything
# after every test, not just on the happy path.
@pytest.fixture(autouse=True)
def _close_vault_handles() -> Any:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()
    for handle in list(_bootstrap_cache.values()):
        handle.close()
    _bootstrap_cache.clear()


_CREATED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
_PROVENANCE = Provenance(source="test", rule_id="orchestrate-fixture")
_DIM = 8

# Fields that are legitimately different on every run (a fresh audit_id
# uuid4, a real wall-clock duration_ms) and must be stripped before an
# equality comparison across two independent runs.
_RUN_SPECIFIC_AUDIT_KEYS = frozenset({"audit_id", "duration_ms"})


def _strip_run_specific(payload: dict[str, object] | None) -> dict[str, object] | None:
    if payload is None:
        return None
    return {k: v for k, v in payload.items() if k not in _RUN_SPECIFIC_AUDIT_KEYS}


# ── fixture graph content ───────────────────────────────────────────────────


def _fixture_nodes(dim: int = _DIM) -> list[Node]:
    return [
        Node(
            id="doc:alpha",
            type="Document",
            title="Alpha",
            content="Alpha content",
            embedding=[0.1] * dim,
            created_at=_CREATED_AT,
            provenance=_PROVENANCE,
        ),
        Node(
            id="doc:beta",
            type="Document",
            title="Beta",
            content="Beta content",
            embedding=[0.2] * dim,
            created_at=_CREATED_AT,
            provenance=_PROVENANCE,
        ),
        Node(
            # No stored vector: exercises copy_graph_reembedding's "copied,
            # not recomputed" branch and copy_graph_canonicalizing's
            # verbatim-embedding-preserved branch identically.
            id="doc:gamma",
            type="Document",
            title="Gamma",
            content="Gamma content",
            embedding=None,
            created_at=_CREATED_AT,
            provenance=_PROVENANCE,
        ),
    ]


def _fixture_edges() -> list[Edge]:
    return [
        Edge(
            id="edge:alpha-beta",
            type="related_to",
            src="doc:alpha",
            dst="doc:beta",
            provenance=_PROVENANCE,
        ),
    ]


# ── pinned-identity bootstrap (see module docstring: why not the real one) ──


def _pinned_bootstrap(graph_generation: str, identity_contract_version: str) -> Any:
    """A ``bootstrap_graph_at_path``-shaped fixture with a FIXED graph identity.

    Verbatim-equivalent to ``cli.kg._bootstrap_graph_at_path`` (same DDL
    helper, same handle shape) except it stamps the identity the caller
    supplies instead of always minting ``schema.new_graph_identity()`` — the
    one deliberate substitution needed to make two independent builds
    byte-identical.
    """

    def _bootstrap(vault_path: Path, graph_path: Path, dim: int | None = None) -> VaultGraphHandle:
        if dim is None:
            dim = kg_cli._resolve_configured_dim(vault_path)
        kg_cli._remove_if_exists(graph_path)
        database: ladybug.Database | None = None
        connection: Any = None
        try:
            database = ladybug.Database(graph_path)
            connection = ladybug.Connection(database)
            schema.verify_schema_version(connection, file_path=graph_path)
            kg_cli._execute_bootstrap_ddl(
                connection,
                dim,
                graph_generation=graph_generation,
                identity_contract_version=identity_contract_version,
            )
            schema.verify_schema_version(connection, file_path=graph_path)
        except Exception:
            kg_cli._close_connection(connection)
            if database is not None:
                database.close()
            raise
        kg_cli._close_connection(connection)
        return VaultGraphHandle(
            vault_path=vault_path,
            schema_version=schema.CURRENT_SCHEMA_VERSION,
            embedding_dim=dim,
            database=database,
            graph_generation=graph_generation,
            identity_contract_version=identity_contract_version,
            graph_file_identity=kg_cli._graph_file_identity(graph_path),
        )

    return _bootstrap


def _make_vault_dir(tmp_path: Path, name: str) -> Path:
    vault_path = tmp_path / name
    kg_cli._ensure_vault_directory(vault_path)
    (vault_path / ".marginalia").mkdir(parents=True, exist_ok=True)
    return vault_path


def _build_populated_vault(
    tmp_path: Path,
    name: str,
    *,
    generation: str,
    contract: str = schema.CURRENT_IDENTITY_CONTRACT_VERSION,
    dim: int = _DIM,
) -> Path:
    """A vault whose LIVE graph already carries the fixture nodes/edges."""

    vault_path = _make_vault_dir(tmp_path, name)
    graph_path = vault_path / "graph.lbug"
    handle = _pinned_bootstrap(generation, contract)(vault_path, graph_path, dim)
    store = LadybugStore(vault_path, graph_handle=handle)
    for node in _fixture_nodes(dim):
        store.add_node(node)
    for edge in _fixture_edges():
        store.add_edge(edge)
    store.checkpoint()
    store.close()
    kg_cli._close_live_graph_handles(vault_path)
    return vault_path


def _read_back_graph(vault_path: Path) -> tuple[list[Node], list[Edge], str]:
    """Reopen the live graph and return its content, sorted for comparison.

    See the module docstring: raw ``graph.lbug`` bytes are not reproducible
    across independent Ladybug database creations even with a pinned
    identity, so determinism is checked at this level instead. Uses
    ``_open_live_handle`` (bypasses the dim-guard, same as ``kg reembed``'s
    own live read) rather than a plain ``LadybugStore(vault_path)`` open,
    because these fixtures deliberately use a non-default embedding width
    (``_DIM = 8``) with no ``okto-neuron.yaml`` around to declare it, and a
    guarded open would refuse that mismatch.
    """
    graph_path = vault_path / "graph.lbug"
    identity = schema.read_graph_identity_path(graph_path)
    handle = kg_cli._open_live_handle(vault_path, graph_path)
    store = LadybugStore(vault_path, graph_handle=handle)
    try:
        nodes = sorted(store.list_nodes(), key=lambda n: n.id)
        edges = sorted(store.list_edges(), key=lambda e: e.id)
    finally:
        store.close()
    kg_cli._close_live_graph_handles(vault_path)
    return nodes, edges, identity.graph_generation or ""


class _Spy:
    """Records every call; usable directly as any of the injectable callables."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> None:
        self.calls.append((args, kwargs))


def _failed_audit(stage: str) -> tuple[IntegrityAuditResult, dict[str, object]]:
    result = IntegrityAuditResult(
        status=AuditStatus.FAILED,
        nodes_scanned=0,
        edges_scanned=0,
        adjacency_scanned=0,
        expected_artifacts_checked=0,
        issue_count=1,
        issues=(
            IntegrityIssue(
                code="fixture_forced_failure",
                artifact_kind="graph",
                artifact_id="graph",
            ),
        ),
        nodes_complete=False,
        edges_complete=False,
        adjacency_complete=False,
        manifest_complete=True,
        duration_ms=0.5,
    )
    payload = {"stage": stage, "status": result.status.value, "issue_count": result.issue_count}
    return result, payload


# =============================================================================
# Group A — finish_staged_swap invariants, against plain stub files.
#
# _swap_rebuilt_graph/_move_graph_family/_discard_graph_family (store/
# staging.py) never inspect graph content, only move/unlink files by path —
# so these invariants are provable with arbitrary bytes standing in for a
# graph database, no real Ladybug file needed.
# =============================================================================


def test_pre_swap_audit_failure_leaves_live_untouched_and_discards_staging(
    tmp_path: Path,
) -> None:
    vault_path = _make_vault_dir(tmp_path, "vault")
    live_path = vault_path / "graph.lbug"
    live_path.write_bytes(b"ORIGINAL-LIVE-GRAPH")

    staging = LadybugStaging(vault_path)
    staged_path = staging.stage_path("t")
    staged_path.write_bytes(b"BAD-STAGED-GRAPH")

    commit_spy = _Spy()
    mark_spy = _Spy()
    publish_spy = _Spy()

    def fake_audit(graph_path: Path, **_: object) -> tuple[IntegrityAuditResult, dict[str, object]]:
        assert graph_path == staged_path
        return _failed_audit("t_pre")

    with acquire_rebuild_lock(vault_path, operation="test") as lock:
        with pytest.raises(RebuildAuditFailed) as excinfo:
            orchestrate.finish_staged_swap(
                vault_path,
                staging,
                staged_path,
                lock=lock,
                backup_tag="t",
                expected_identity=schema.GraphIdentity("gen-1", "contract-1"),
                dim=_DIM,
                stage_prefix="t",
                audit_graph_path=fake_audit,
                mark_generation_verifying=mark_spy,
                publish_integrity_result=publish_spy,
                pre_swap_stage="t_pre",
                commit=commit_spy,
            )

    assert "live graph left unchanged" in str(excinfo.value)
    assert commit_spy.calls == []  # never reached the commit step
    assert mark_spy.calls == []  # never reached the post-swap half either
    assert publish_spy.calls == []
    assert live_path.read_bytes() == b"ORIGINAL-LIVE-GRAPH"  # untouched
    assert not staged_path.exists()  # discarded


def test_lock_lost_before_commit_fails_closed_without_committing(tmp_path: Path) -> None:
    vault_path = _make_vault_dir(tmp_path, "vault")
    live_path = vault_path / "graph.lbug"
    live_path.write_bytes(b"ORIGINAL-LIVE-GRAPH")

    staging = LadybugStaging(vault_path)
    staged_path = staging.stage_path("t")
    staged_path.write_bytes(b"NEW-STAGED-GRAPH")

    class _LostLock:
        def require_held(self) -> None:
            raise VaultLockHeld(vault_path, message="lock lost between acquire and commit")

    commit_spy = _Spy()

    with pytest.raises(VaultLockHeld):
        orchestrate.finish_staged_swap(
            vault_path,
            staging,
            staged_path,
            lock=_LostLock(),
            backup_tag="t",
            expected_identity=schema.GraphIdentity("gen-1", "contract-1"),
            dim=_DIM,
            stage_prefix="t",
            commit=commit_spy,
            publish_integrity=False,  # isolate invariant 2 from invariant 3/4
        )

    assert commit_spy.calls == []
    assert live_path.read_bytes() == b"ORIGINAL-LIVE-GRAPH"
    assert staged_path.exists()  # no pre_swap_stage given -> never discarded either


def test_post_swap_audit_failure_raises_names_backup_and_leaves_bad_graph_live(
    tmp_path: Path,
) -> None:
    vault_path = _make_vault_dir(tmp_path, "vault")
    live_path = vault_path / "graph.lbug"
    live_path.write_bytes(b"OLD-GOOD-GRAPH")

    staging = LadybugStaging(vault_path)
    staged_path = staging.stage_path("t")
    staged_path.write_bytes(b"NEW-BAD-GRAPH")

    mark_spy = _Spy()
    publish_spy = _Spy()

    def fake_audit(graph_path: Path, *, stage: str, **_: object) -> tuple[IntegrityAuditResult, dict[str, object]]:
        assert graph_path == live_path  # post-swap audit reopens the now-live path
        assert stage == "t_after_swap_reopen"
        return _failed_audit(stage)

    with acquire_rebuild_lock(vault_path, operation="test") as lock:
        with pytest.raises(RebuildAuditFailed) as excinfo:
            orchestrate.finish_staged_swap(
                vault_path,
                staging,
                staged_path,
                lock=lock,
                backup_tag="t",
                expected_identity=schema.GraphIdentity("gen-2", "contract-1"),
                dim=_DIM,
                stage_prefix="t",
                audit_graph_path=fake_audit,
                mark_generation_verifying=mark_spy,
                publish_integrity_result=publish_spy,
                # pre_swap_stage=None: rebuild-shaped, no pre-swap audit.
            )

    backup_path = vault_path / "graph.lbug.t"
    assert str(backup_path) in str(excinfo.value)  # invariant 3: names the backup
    assert mark_spy.calls != []  # generation was fenced VERIFYING before the audit ran
    assert publish_spy.calls != []  # the (failing) verdict was still published
    # invariant 3: NOT auto-restored — the bad graph stays live.
    assert live_path.read_bytes() == b"NEW-BAD-GRAPH"
    assert backup_path.read_bytes() == b"OLD-GOOD-GRAPH"
    assert not staged_path.exists()  # consumed by the commit


def test_publish_integrity_false_skips_post_swap_half_entirely(tmp_path: Path) -> None:
    """Invariant 4 (reembed's documented asymmetry), against finish_staged_swap directly."""

    vault_path = _make_vault_dir(tmp_path, "vault")
    live_path = vault_path / "graph.lbug"
    live_path.write_bytes(b"OLD-GRAPH")

    staging = LadybugStaging(vault_path)
    staged_path = staging.stage_path("t")
    staged_path.write_bytes(b"NEW-GRAPH")

    audit_spy = _Spy()

    with acquire_rebuild_lock(vault_path, operation="test") as lock:
        result = orchestrate.finish_staged_swap(
            vault_path,
            staging,
            staged_path,
            lock=lock,
            backup_tag="t",
            expected_identity=schema.GraphIdentity("gen-3", "contract-1"),
            dim=_DIM,
            stage_prefix="reembed",
            audit_graph_path=audit_spy,  # must never be called
            publish_integrity=False,
        )

    assert audit_spy.calls == []
    assert result.post_swap_audit is None
    assert result.pre_swap_audit == {}
    assert result.graph_generation == "gen-3"
    assert live_path.read_bytes() == b"NEW-GRAPH"  # committed anyway
    assert (vault_path / "graph.lbug.t").read_bytes() == b"OLD-GRAPH"


# =============================================================================
# Group B — the real rebuild()/heal()/reembed() wrappers against a populated
# Ladybug fixture: determinism (byte-identical graph + structurally identical
# result across two independent runs) and result/stats shape.
# =============================================================================


def test_rebuild_two_runs_on_identical_fixtures_match_bytes_and_shape(tmp_path: Path) -> None:
    prev_generation = "prev-fixed-generation"
    next_generation = "next-fixed-generation"
    contract = schema.CURRENT_IDENTITY_CONTRACT_VERSION

    def _build(
        vault_path: Path,
        staged_path: Path,
        *,
        ingest: object | None = None,
        source_files: object | None = None,
        interrupt_check: object | None = None,
    ) -> dict[str, object]:
        handle = _pinned_bootstrap(next_generation, contract)(vault_path, staged_path, _DIM)
        store = LadybugStore(vault_path, graph_handle=handle)
        for node in _fixture_nodes(_DIM):
            store.add_node(node)
        for edge in _fixture_edges():
            store.add_edge(edge)
        store.checkpoint()
        store.close()
        return {
            "graph_generation": next_generation,
            "identity_contract_version": contract,
            "embedding_dim": _DIM,
        }

    results = []
    graph_contents = []
    for copy_name in ("a", "b"):
        vault_path = _build_populated_vault(tmp_path, copy_name, generation=prev_generation)
        with acquire_rebuild_lock(vault_path, operation="rebuild") as lock:
            result = orchestrate.rebuild(
                vault_path,
                lock,
                staging=LadybugStaging(vault_path),
                build=_build,
                require_candidate=lambda built: None,
                audit_graph_path=kg_cli._audit_rebuild_graph_path,
                mark_generation_verifying=kg_cli._mark_rebuild_generation_verifying,
                publish_integrity_result=kg_cli._publish_integrity_result,
                close_live_handles=kg_cli._close_live_graph_handles,
            )
        results.append(result)
        graph_contents.append(_read_back_graph(vault_path))

    # Determinism: identical fixture in, identical graph content out (see
    # module docstring for why this is content-level, not raw-byte-level).
    assert graph_contents[0] == graph_contents[1]
    assert graph_contents[0][2] == next_generation

    first, second = results
    assert first.graph_generation == second.graph_generation == next_generation
    assert first.backup_path.name == second.backup_path.name == "graph.lbug.rebuild"
    # rebuild has no pre-swap audit today (kg.py's _kg_rebuild_owned has none;
    # its only pre-swap gate is require_candidate) — invariant preserved.
    assert first.pre_swap_audit == second.pre_swap_audit == {}

    # Shape: matches tests/cli/test_kg_rebuild.py's rebuild.state.json
    # final_audit assertions (state["final_audit"]["stage"], ["status"]).
    for result in (first, second):
        assert result.post_swap_audit is not None
        assert result.post_swap_audit["stage"] == "rebuild_after_swap_reopen"
        assert result.post_swap_audit["status"] == AuditStatus.VERIFIED.value
        assert {"stage", "status", "nodes_scanned", "edges_scanned", "issue_count"} <= set(
            result.post_swap_audit
        )
    assert _strip_run_specific(first.post_swap_audit) == _strip_run_specific(second.post_swap_audit)


def test_heal_two_runs_on_identical_fixtures_match_bytes_stats_and_shape(tmp_path: Path) -> None:
    prev_generation = "prev-fixed-generation"
    healed_generation = "healed-fixed-generation"
    contract = schema.CURRENT_IDENTITY_CONTRACT_VERSION

    results = []
    stats_list = []
    graph_contents = []
    for copy_name in ("a", "b"):
        vault_path = _build_populated_vault(tmp_path, copy_name, generation=prev_generation)
        with acquire_rebuild_lock(vault_path, operation="reconcile heal") as lock:
            result, stats = orchestrate.heal(
                vault_path,
                lock,
                live_nodes=_fixture_nodes(_DIM),
                live_edges=_fixture_edges(),
                staging=LadybugStaging(vault_path),
                bootstrap_graph_at_path=_pinned_bootstrap(healed_generation, contract),
                audit_graph_path=kg_cli._audit_rebuild_graph_path,
                mark_generation_verifying=kg_cli._mark_rebuild_generation_verifying,
                publish_integrity_result=kg_cli._publish_integrity_result,
                close_live_handles=kg_cli._close_live_graph_handles,
            )
        results.append(result)
        stats_list.append(stats)
        graph_contents.append(_read_back_graph(vault_path))

    assert graph_contents[0] == graph_contents[1]
    assert graph_contents[0][2] == healed_generation
    assert stats_list[0] == stats_list[1]
    # An (empty-authority) heal on 3 nodes/1 edge is a verbatim copy — no
    # equivalence to fold, matching heal_via_copy's documented degenerate case.
    assert stats_list[0]["nodes_kept"] == 3
    assert stats_list[0]["nodes_dropped"] == 0
    assert stats_list[0]["edges_kept"] == 1

    first, second = results
    assert first.graph_generation == second.graph_generation == healed_generation
    assert first.backup_path.exists() and second.backup_path.exists()
    assert first.pre_swap_audit != {}  # heal DOES run a pre-swap audit (unlike rebuild)
    assert first.post_swap_audit is not None
    for result in (first, second):
        assert {"stage", "status"} <= set(result.pre_swap_audit)
        assert result.pre_swap_audit["status"] == AuditStatus.VERIFIED.value
        assert {"stage", "status"} <= set(result.post_swap_audit)
        assert result.post_swap_audit["status"] == AuditStatus.VERIFIED.value
    assert _strip_run_specific(first.pre_swap_audit) == _strip_run_specific(second.pre_swap_audit)
    assert _strip_run_specific(first.post_swap_audit) == _strip_run_specific(second.post_swap_audit)


def test_reembed_two_runs_on_identical_fixtures_match_bytes_stats_and_asymmetry(
    tmp_path: Path,
) -> None:
    prev_generation = "prev-fixed-generation"
    reembedded_generation = "reembedded-fixed-generation"
    contract = schema.CURRENT_IDENTITY_CONTRACT_VERSION

    results = []
    stats_list = []
    graph_contents = []
    for copy_name in ("a", "b"):
        vault_path = _build_populated_vault(tmp_path, copy_name, generation=prev_generation)
        with acquire_rebuild_lock(vault_path, operation="reembed") as lock:
            result, stats = orchestrate.reembed(
                vault_path,
                lock,
                live_nodes=_fixture_nodes(_DIM),
                live_edges=_fixture_edges(),
                embedder=StubEmbedder(dim=_DIM),
                dim=_DIM,
                staging=LadybugStaging(vault_path),
                bootstrap_graph_at_path=_pinned_bootstrap(reembedded_generation, contract),
                close_live_handles=kg_cli._close_live_graph_handles,
            )
        results.append(result)
        stats_list.append(stats)
        graph_contents.append(_read_back_graph(vault_path))

    assert graph_contents[0] == graph_contents[1]
    assert graph_contents[0][2] == reembedded_generation
    assert stats_list[0] == stats_list[1]
    # Shape matches _kg_reembed_owned's final state.json write
    # ({"phase": "complete", ..., **stats}): nodes/edges/recomputed/copied.
    assert set(stats_list[0]) == {"nodes", "edges", "recomputed", "copied"}
    assert stats_list[0]["nodes"] == 3
    assert stats_list[0]["edges"] == 1
    assert stats_list[0]["recomputed"] == 2  # alpha, beta carried a vector
    assert stats_list[0]["copied"] == 1  # gamma had none

    first, second = results
    assert first.graph_generation == second.graph_generation == reembedded_generation
    assert first.backup_path.exists() and second.backup_path.exists()
    # Invariant 4, through the real code path this time: no post-swap audit,
    # no generation fence/publish for reembed.
    assert first.post_swap_audit is None
    assert second.post_swap_audit is None
    assert first.pre_swap_audit == {}
    assert second.pre_swap_audit == {}


# =============================================================================
# Group C — a real verb's pre-swap audit failure, through the real heal()
# wrapper (not just finish_staged_swap directly): live untouched, staging
# discarded, even with heal's own build head (bootstrap + canonicalizing
# copy) having already run.
# =============================================================================


def test_heal_pre_swap_audit_failure_leaves_live_untouched_and_discards_staging(
    tmp_path: Path,
) -> None:
    prev_generation = "prev-fixed-generation"
    contract = schema.CURRENT_IDENTITY_CONTRACT_VERSION
    vault_path = _build_populated_vault(tmp_path, "vault", generation=prev_generation)
    live_path = vault_path / "graph.lbug"
    original_live_bytes = live_path.read_bytes()

    staging = LadybugStaging(vault_path)
    staged_path = staging.stage_path("heal")

    def always_fails(graph_path: Path, **_: object) -> tuple[IntegrityAuditResult, dict[str, object]]:
        return _failed_audit("heal_pre_swap_fixture_failure")

    mark_spy = _Spy()
    publish_spy = _Spy()

    with acquire_rebuild_lock(vault_path, operation="reconcile heal") as lock:
        with pytest.raises(RebuildAuditFailed):
            orchestrate.heal(
                vault_path,
                lock,
                live_nodes=_fixture_nodes(_DIM),
                live_edges=_fixture_edges(),
                staging=staging,
                bootstrap_graph_at_path=_pinned_bootstrap("would-be-healed-generation", contract),
                audit_graph_path=always_fails,
                mark_generation_verifying=mark_spy,
                publish_integrity_result=publish_spy,
                close_live_handles=kg_cli._close_live_graph_handles,
            )

    assert live_path.read_bytes() == original_live_bytes  # untouched
    assert not staged_path.exists()  # staged graph discarded
    assert mark_spy.calls == []  # never reached the post-swap half
    assert publish_spy.calls == []
