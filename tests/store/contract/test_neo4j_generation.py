"""Neo4j-specific generation/staging/consent contract cases (M5 spec §4).

Skips cleanly (module-level ``pytest.importorskip`` + env-var guard,
mirroring ``conftest.py``'s ``graph_store`` fixture's own ``"neo4j"`` branch)
unless the ``neo4j`` driver is installed AND ``OKTO_NEURON_TEST_NEO4J_URI`` (+
a paired credential env var name) is set. All cases open real ``Neo4jStore``/
``Neo4jStaging`` handles against a real, already-running Neo4j server -- no
mocks, per this repo's "no mocks in acceptance/contract" convention.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

pytest.importorskip("neo4j")

_URI = os.environ.get("OKTO_NEURON_TEST_NEO4J_URI")
if not _URI:
    pytest.skip("OKTO_NEURON_TEST_NEO4J_URI is not set", allow_module_level=True)
_CREDENTIAL_ENV = os.environ.get("OKTO_NEURON_TEST_NEO4J_CREDENTIAL_ENV")

from okto_neuron.core.schema import Edge, Node, Provenance  # noqa: E402
from okto_neuron.store.neo4j import Neo4jStore  # noqa: E402
from okto_neuron.store.staging import Neo4jStaging  # noqa: E402


class _Cfg:
    backend = "neo4j"
    database = "neo4j"

    def __init__(self) -> None:
        self.uri = _URI
        self.credential_env = _CREDENTIAL_ENV
        self.allow_remote = False


def _node(node_id: str, *, title: str = "n") -> Node:
    return Node(
        id=node_id,
        type="Concept",
        title=title,
        content=f"content for {node_id}",
        tags=["m5"],
        facets={},
        provenance=Provenance(source="ingest", layer="deterministic"),
    )


def _fresh_vault_path(tmp_path: Path) -> Path:
    # Each test gets a distinct vault_id (hash of the path, see
    # `neo4j.vault_id_for`) so vaults never collide against the shared CE
    # instance, matching the shepherd override's vault_id scoping contract.
    return tmp_path / f"vault-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def live_store(tmp_path: Path):
    store = Neo4jStore(_fresh_vault_path(tmp_path), config=_Cfg())
    yield store
    store.close()


# ---------------------------------------------------------------------------
# id-collision-resolves-via-MERGE sanity (Neptune risk-3 precedent).
# ---------------------------------------------------------------------------


def test_add_node_same_id_twice_is_idempotent_merge(live_store: Neo4jStore) -> None:
    node = _node("dup-node")
    live_store.add_node(node)
    first = live_store.get_node("dup-node")
    assert first is not None

    # Re-adding the identical payload must MERGE onto the same row, not
    # create a second one under the same (id, vault_id, _generation) key --
    # the composite uniqueness constraint (M5 spec §2) is the enforcement,
    # this asserts the observable behavior on top of it.
    live_store.add_node(_node("dup-node"))
    nodes = [n for n in live_store.list_nodes() if n.id == "dup-node"]
    assert len(nodes) == 1
    assert nodes[0].created_at == first.created_at


# ---------------------------------------------------------------------------
# bulk add_nodes / add_edges (shepherd override: optional bulk members).
# ---------------------------------------------------------------------------


def test_bulk_add_nodes_and_add_edges(live_store: Neo4jStore) -> None:
    nodes = [_node(f"bulk-{i}") for i in range(5)]
    live_store.add_nodes(nodes)  # type: ignore[attr-defined]
    for node in nodes:
        assert live_store.get_node(node.id) is not None

    edges = [
        Edge(
            id=f"bulk-edge-{i}",
            type="relates_to",
            src=f"bulk-{i}",
            dst=f"bulk-{(i + 1) % 5}",
            weight=1.0,
            provenance=Provenance(source="ingest", layer="deterministic"),
        )
        for i in range(5)
    ]
    live_store.add_edges(edges)  # type: ignore[attr-defined]
    listed = list(live_store.list_edges())
    listed_ids = {e.id for e in listed}
    for edge in edges:
        assert edge.id in listed_ids


# ---------------------------------------------------------------------------
# Concurrent-write race: two handles racing add_node on the same brand-new
# id. Neo4j's own compound constraint + the outer retry_with_backoff loop
# (D-10, M5 spec §2 "the outer loop is what retries the compound-constraint-
# violation case") must resolve this to exactly one landed row, never a
# duplicate and never an uncaught exception from either thread.
# ---------------------------------------------------------------------------


def test_concurrent_add_node_same_new_id_lands_exactly_once(tmp_path: Path) -> None:
    import threading

    vault_path = _fresh_vault_path(tmp_path)
    bootstrap = Neo4jStore(vault_path, config=_Cfg())
    bootstrap.close()

    node_id = "race-" + uuid.uuid4().hex[:8]
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def _writer() -> None:
        store = Neo4jStore(vault_path, config=_Cfg())
        try:
            barrier.wait(timeout=10)
            store.add_node(_node(node_id, title="racer"))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            store.close()

    threads = [threading.Thread(target=_writer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"a racing writer raised: {errors}"

    verify = Neo4jStore(vault_path, config=_Cfg())
    try:
        landed = [n for n in verify.list_nodes() if n.id == node_id]
    finally:
        verify.close()
    assert len(landed) == 1, f"expected exactly one landed row under the raced id, got {len(landed)}"


# ---------------------------------------------------------------------------
# Generation-swap isolation (M5 spec §4, risk-9's realization) and
# non-Ladybug rollback via generation restore -- both via Neo4jStaging
# directly, the same seam `cli/kg.py`'s `_swap_construction_for` drives.
#
# The Neo4j metadata node is a per-vault SINGLETON, always tagged with the
# fixed `schema.NEO4J_METADATA_GENERATION` sentinel rather than a real graph
# generation (`Neo4jStore._bootstrap_or_adopt_metadata`) -- a build-mode open
# adopts that same row instead of minting a second one, so `Neo4jStaging.
# commit`/`restore`'s pointer-flip lookup always matches exactly one record.
# ---------------------------------------------------------------------------


def test_generation_swap_isolation_then_rollback(tmp_path: Path) -> None:
    vault_path = _fresh_vault_path(tmp_path)
    live = Neo4jStore(vault_path, config=_Cfg())
    try:
        live.add_node(_node("swap-x", title="live payload"))
        live_generation_before = live.generation()

        staging = Neo4jStaging(vault_path, store=live)
        build_tag_literal = "build-" + uuid.uuid4().hex[:8]
        staged_path = staging.stage_path(build_tag_literal)
        # stage_path always mints a unique per-call generation tag (never
        # the bare caller literal) -- see Neo4jStaging.stage_path's own
        # docstring for why (the same-literal-tag live-deletion defect).
        build_tag = staged_path.name
        assert build_tag != build_tag_literal
        assert build_tag.startswith(build_tag_literal)

        build_store = Neo4jStore(staged_path, config=_Cfg())
        try:
            build_store.add_node(_node("swap-x", title="build payload"))
            # Live read is unaffected by the isolated build-mode write.
            still_live = live.get_node("swap-x")
            assert still_live is not None
            assert still_live.title == "live payload"

            backup_path = staging.commit(staged_path, backup_tag="backup-" + uuid.uuid4().hex[:8])
        finally:
            build_store.close()

        # Re-open live (its own cached generation tag is stale post-commit).
        live.close()
        live = Neo4jStore(vault_path, config=_Cfg())
        assert live.generation() == build_tag
        committed = live.get_node("swap-x")
        assert committed is not None
        assert committed.title == "build payload"

        staging = Neo4jStaging(vault_path, store=live)
        staging.restore(backup_path)
        live.close()
        live = Neo4jStore(vault_path, config=_Cfg())
        assert live.generation() == live_generation_before
        reverted = live.get_node("swap-x")
        assert reverted is not None
        assert reverted.title == "live payload"
    finally:
        live.close()


# ---------------------------------------------------------------------------
# Consent regression: `onboard --backend neo4j --storage-uri <remote>`
# rejected non-interactively without `--allow-remote-db --yes`, succeeds
# with consent. Exercises the CLI end to end (not just config parsing) via
# CliRunner -- no live remote server is required since M5's `onboard` never
# opens a `Neo4jStore` connection itself, only writes `okto-neuron.yaml`
# (verified empirically; see `tests/acceptance/scenarios/99_backend_selection.sh`
# Part 5/6 for the acceptance-level equivalent of this same regression).
# ---------------------------------------------------------------------------


def test_onboard_neo4j_remote_uri_requires_consent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from click.testing import CliRunner

    from okto_neuron.cli import app

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OKTO_NEURON_TEST_NEO4J_DUMMY", "dummy-secret-value")

    runner = CliRunner()

    # Without consent: exit 1, names the confirming flags.
    result = runner.invoke(
        app,
        [
            "onboard",
            "--vault",
            "consent-regress-vault",
            "--backend",
            "neo4j",
            "--storage-uri",
            "bolt://remote.example:7687",
            "--non-interactive",
            "--provider",
            "skip",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "--allow-remote-db" in result.output
    assert "--yes" in result.output

    # With consent: succeeds (M5 registers neo4j -- see 99_backend_selection.sh
    # Part 6 for the full config-shape assertion at the acceptance level).
    result = runner.invoke(
        app,
        [
            "onboard",
            "--vault",
            "consent-regress-vault-2",
            "--backend",
            "neo4j",
            "--storage-uri",
            "bolt://remote.example:7687",
            "--storage-credential-env",
            "OKTO_NEURON_TEST_NEO4J_DUMMY",
            "--allow-remote-db",
            "--yes",
            "--non-interactive",
            "--provider",
            "skip",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "dummy-secret-value" not in result.output


# ---------------------------------------------------------------------------
# Same-literal-tag repeat-verb regression (the live-graph-deletion defect):
# every offline owner (`rebuild`/`heal`/`reembed`) calls
# `staging.stage_path(<fixed literal>)` then unconditionally
# `staging.discard(tmp_graph_path)` right before building, on EVERY run of
# that verb -- not just the first. With `stage_path` returning the bare
# literal as the Neo4j generation tag, the second run's pre-build discard
# deleted every node tagged with that literal, which by then was the LIVE
# graph the first run had just committed. This drives the same two-call
# sequence `cli/kg.py`/`server/_curation.py` do (stage_path -> discard
# -> populate -> commit, twice with the identical literal tag) and asserts
# the live node count never drops to zero between the second run's discard
# and its commit, that rollback still works after two full cycles, and that
# retained generations stay bounded (live + at most one backup) per vault.
# ---------------------------------------------------------------------------


def _run_verb_cycle(
    vault_path: Path, live: Neo4jStore, *, literal_tag: str, node_title: str, backup_tag: str
) -> Path:
    """One `rebuild`-shaped cycle: stage_path -> discard -> populate -> commit.

    Mirrors the exact sequence every offline owner drives (`cli/kg.py`
    lines ~507-528/698, `server/_curation.py` lines ~1564/2536/2797) against
    the SAME fixed literal tag every single call -- the shape that exposed
    the defect.
    """
    staging = Neo4jStaging(vault_path, store=live)
    tmp_graph_path = staging.stage_path(literal_tag)
    # Pre-build cleanup: every real caller does this unconditionally, before
    # populating the staged generation, to clear any stale leftovers from an
    # interrupted prior run of the same verb.
    staging.discard(tmp_graph_path)

    build_store = Neo4jStore(tmp_graph_path, config=_Cfg())
    try:
        build_store.add_node(_node("cycle-node", title=node_title))
    finally:
        build_store.close()

    return staging.commit(tmp_graph_path, backup_tag)


def test_repeated_same_literal_tag_never_drops_live_to_zero(tmp_path: Path) -> None:
    vault_path = _fresh_vault_path(tmp_path)
    live = Neo4jStore(vault_path, config=_Cfg())
    try:
        live.add_node(_node("sentinel", title="pre-existing live data"))
        assert len(live.list_nodes()) >= 1

        # First `heal` cycle (matches the literal `"heal"` tag every real
        # caller passes -- see cli/kg.py:1675, server/_curation.py:2536).
        _run_verb_cycle(vault_path, live, literal_tag="heal", node_title="heal-run-1", backup_tag="bak")

        live.close()
        live = Neo4jStore(vault_path, config=_Cfg())
        after_first = live.list_nodes()
        assert len(after_first) >= 1, "live graph must not be empty after the first heal cycle"

        # Second `heal` cycle, SAME literal tag -- this is exactly the
        # sequence that used to nuke the graph the first cycle just
        # committed, via the second cycle's own pre-build `discard`.
        _run_verb_cycle(vault_path, live, literal_tag="heal", node_title="heal-run-2", backup_tag="bak")

        live.close()
        live = Neo4jStore(vault_path, config=_Cfg())
        after_second = live.list_nodes()
        assert len(after_second) >= 1, (
            "live graph dropped to zero nodes after a second same-literal-tag heal "
            "cycle's pre-build discard -- the defect this test guards against"
        )
        committed = live.get_node("cycle-node")
        assert committed is not None
        assert committed.title == "heal-run-2"

        # Rollback still works after two cycles: restore() flips back onto
        # whatever generation `commit` last stashed as the backup (the
        # first cycle's committed generation), not an empty/garbage-
        # collected one.
        staging = Neo4jStaging(vault_path, store=live)
        # backup_path's own name is inert for restore (per Neo4jStaging.restore's
        # docstring); reconstruct it exactly as commit() would have returned.
        backup_path = vault_path / ".neo4j-generation" / "bak"
        staging.restore(backup_path)
        live.close()
        live = Neo4jStore(vault_path, config=_Cfg())
        reverted = live.get_node("cycle-node")
        assert reverted is not None
        assert reverted.title == "heal-run-1", "rollback after two cycles must land on the first cycle's commit"
        assert len(live.list_nodes()) >= 1

        # Bounded garbage: at most live + one backup generation retained
        # for this vault_id -- the generation that was neither live nor
        # backup after the second commit must have been garbage-collected,
        # not accumulated forever.
        with live._driver.session(database=live._database) as session:  # noqa: SLF001
            distinct_generations = session.run(
                "MATCH (n:Node {vault_id: $vault_id}) "
                "WHERE n._generation <> $meta_generation "
                "RETURN DISTINCT n._generation AS generation",
                {"vault_id": live.vault_id, "meta_generation": "__meta__"},
            ).value("generation")
        assert len(set(distinct_generations)) <= 2, (
            f"expected at most live + one backup generation retained, got {distinct_generations}"
        )
    finally:
        live.close()
