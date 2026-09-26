"""Tests for ``store/staging.py``'s Grafx-backed ``StagingPort``, ``GrafxStaging``
(M4 spec §2/§4 bullet 3).

Grafx-specific sibling of the M2b Ladybug suite
(``tests/store/contract/test_staging.py``), not folded into it: Grafx's live
graph is a *directory* (``graph.grafx``), not Ladybug's single file, so
``commit``/``restore`` use a materially different rename-chain implementation
(see ``GrafxStaging``'s own docstring) with a failure mode Ladybug's
file-to-file ``os.replace`` never faces -- a directory ``os.replace`` raises
``ENOTEMPTY`` over a non-empty destination, which is exactly why ``commit``
needs the ``*.discard``-vacate step this file's cases exercise directly.
``tests/store/contract/test_staging.py``'s own Grafx contract-suite
parametrization (``tests/store/contract/conftest.py``) skips this backend
outright for that reason (``isinstance(store, LadybugStore)``), so those
cases are not duplicated coverage.

Every case here builds real Grafx directories through the real ``GrafxStore``
write path (never fabricated fixture bytes), matching the Ladybug suite's own
"real graph content, not mocks" convention. Reading a *backup* or *staged*
directory's content by id (``_list_node_ids``) goes around ``GrafxStore``
entirely, through a raw ``okto_grafx.connect()`` -- ``GrafxStore.__init__``'s
own ``_is_graph_directory_path`` heuristic only recognizes the live name
(``graph.grafx``) and the staged-tag shape (``graph.<tag>.grafx``), not a
committed backup's ``graph.grafx.<tag>`` shape, so a backup directory is not
directly openable through ``GrafxStore`` -- exactly why the real
``_run_rollback_non_ladybug`` copies a backup into a staged-shaped path
before ever opening it as a store, and why this test does not construct a
``GrafxStore`` over a raw backup path either.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("okto_grafx")

import okto_grafx as grafx  # noqa: E402

from okto_neuron.core.schema import Edge, Node  # noqa: E402
from okto_neuron.errors import RebuildSwapFailed  # noqa: E402
from okto_neuron.store import schema  # noqa: E402
from okto_neuron.store.grafx import GrafxStore  # noqa: E402
from okto_neuron.store.staging import GrafxStaging  # noqa: E402


def _build_staged_store(staging: GrafxStaging, tag: str) -> tuple[Path, GrafxStore]:
    staged_path = staging.stage_path(tag)
    return staged_path, GrafxStore(staged_path)


def _list_node_ids(graph_dir: Path) -> set[str]:
    """Read a Grafx directory's node ids directly, bypassing ``GrafxStore``
    (see module docstring: a backup's ``graph.grafx.<tag>`` name isn't a
    shape ``GrafxStore.__init__`` recognizes as an already-open graph dir).
    """
    db = grafx.connect(graph_dir, descriptor_revalidation="strict")
    try:
        rows = db.execute(
            "MATCH (n:Node) WHERE n.id <> $metadata_id RETURN n.id AS id",
            {"metadata_id": schema.SCHEMA_METADATA_NODE_ID},
        ).dictionaries()
        return {row["id"] for row in rows}
    finally:
        db.close()


def _snapshot_directory_bytes(path: Path) -> dict[str, bytes]:
    return {str(p.relative_to(path)): p.read_bytes() for p in sorted(path.rglob("*")) if p.is_file()}


def test_stage_populate_commit_round_trips_real_graph_data(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    live = GrafxStore(vault_path)
    live.add_node(Node(id="live-only-1", type="Concept", title="pre-existing live node one"))
    live.add_node(Node(id="live-only-2", type="Concept", title="pre-existing live node two"))
    live.close()  # production always closes the live handle before a swap

    staging = GrafxStaging(vault_path)
    staged_path, staged_store = _build_staged_store(staging, "rebuild")
    assert staged_path == vault_path / "graph.rebuild.grafx"
    try:
        staged_store.add_node(Node(id="staged-only", type="Concept", title="staged content"))
        staged_store.add_node(Node(id="staged-other", type="Concept", title="second staged node"))
        staged_store.add_edge(Edge(id="e-staged", type="mentions", src="staged-only", dst="staged-other"))
    finally:
        staged_store.close()

    backup_path = staging.commit(staged_path, backup_tag="bak")

    assert backup_path == vault_path / "graph.grafx.bak"
    assert not staged_path.exists(), "the staged directory is consumed by the swap"
    assert (vault_path / "graph.grafx").exists()

    live_after = GrafxStore(vault_path)
    try:
        assert {n.id for n in live_after.list_nodes()} == {"staged-only", "staged-other"}
        assert [e.id for e in live_after.list_edges(src="staged-only")] == ["e-staged"]
    finally:
        live_after.close()

    assert _list_node_ids(backup_path) == {"live-only-1", "live-only-2"}


def test_two_consecutive_commits_with_the_same_backup_tag_both_succeed(tmp_path: Path) -> None:
    """Mirrors ``heal`` running right after ``reembed``, both tagged ``"bak"``
    -- the ``ENOTEMPTY`` failure mode ``GrafxStaging``'s docstring names for a
    naive directory ``os.replace`` over an already-occupied backup path.
    """
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    live = GrafxStore(vault_path)
    live.add_node(Node(id="gen0", type="Concept", title="generation zero"))
    live.close()

    staging = GrafxStaging(vault_path)

    staged1_path, staged1_store = _build_staged_store(staging, "reembed")
    try:
        staged1_store.add_node(Node(id="gen1", type="Concept", title="generation one"))
    finally:
        staged1_store.close()
    backup1 = staging.commit(staged1_path, backup_tag="bak")
    assert backup1 == vault_path / "graph.grafx.bak"
    assert _list_node_ids(backup1) == {"gen0"}

    staged2_path, staged2_store = _build_staged_store(staging, "heal")
    try:
        staged2_store.add_node(Node(id="gen2", type="Concept", title="generation two"))
    finally:
        staged2_store.close()
    # The second commit reuses the exact same backup path/tag as the first --
    # must not raise ENOTEMPTY over the still-occupied graph.grafx.bak.
    backup2 = staging.commit(staged2_path, backup_tag="bak")
    assert backup2 == vault_path / "graph.grafx.bak"
    assert _list_node_ids(backup2) == {"gen1"}, "the second commit's backup must hold gen1, not gen0"

    live_final = GrafxStore(vault_path)
    try:
        assert {n.id for n in live_final.list_nodes()} == {"gen2"}
    finally:
        live_final.close()

    assert not (vault_path / "graph.grafx.bak.discard").exists(), "no stray .discard sibling left behind"


def test_restore_moves_a_backup_graph_back_onto_live(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    live = GrafxStore(vault_path)
    live.add_node(Node(id="pre-swap", type="Concept", title="before the swap"))
    live.close()

    staging = GrafxStaging(vault_path)
    staged_path, staged_store = _build_staged_store(staging, "rollback")
    try:
        staged_store.add_node(Node(id="post-swap-only", type="Concept", title="after the swap"))
    finally:
        staged_store.close()

    backup_path = staging.commit(staged_path, backup_tag="rollback-bak")
    live_after_commit = GrafxStore(vault_path)
    try:
        assert {n.id for n in live_after_commit.list_nodes()} == {"post-swap-only"}
    finally:
        live_after_commit.close()

    staging.restore(backup_path)

    assert not backup_path.exists()
    restored = GrafxStore(vault_path)
    try:
        assert {n.id for n in restored.list_nodes()} == {"pre-swap"}
    finally:
        restored.close()


def test_commit_failure_mid_chain_restores_the_pre_swap_live_directory_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    live = GrafxStore(vault_path)
    live.add_node(Node(id="never-replaced", type="Concept", title="must survive a failed swap"))
    live.close()

    live_graph_path = vault_path / "graph.grafx"
    original_bytes = _snapshot_directory_bytes(live_graph_path)

    staging = GrafxStaging(vault_path)
    staged_path, staged_store = _build_staged_store(staging, "rebuild")
    try:
        staged_store.add_node(Node(id="never-lands-live", type="Concept", title="never lands"))
    finally:
        staged_store.close()

    real_replace = os.replace
    tripped = {"once": False}

    def flaky_replace(src, dst, *args, **kwargs):
        # commit() does two os.replace calls whose dst is the live graph
        # path: the real staged->live move (fail exactly this one) and,
        # inside the except-block's own recovery, backup->live (must
        # succeed, or live is never actually restored).
        if Path(dst) == live_graph_path and not tripped["once"]:
            tripped["once"] = True
            raise OSError("simulated failure moving the staged graph onto live")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr("okto_neuron.store.staging.os.replace", flaky_replace)

    with pytest.raises(RebuildSwapFailed):
        staging.commit(staged_path, backup_tag="bak")

    assert tripped["once"]
    assert live_graph_path.exists()
    assert _snapshot_directory_bytes(live_graph_path) == original_bytes
    assert not (vault_path / "graph.grafx.bak").exists()
    assert staged_path.exists(), "the staged graph is left in place, not auto-discarded"

    live_readable = GrafxStore(vault_path)
    try:
        assert {n.id for n in live_readable.list_nodes()} == {"never-replaced"}
    finally:
        live_readable.close()


def test_leftover_discard_directory_from_an_interrupted_prior_swap_is_cleared_on_next_stage(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    live = GrafxStore(vault_path)
    live.close()

    staging = GrafxStaging(vault_path)
    stray_discard = vault_path / "graph.grafx.bak.discard"
    stray_discard.mkdir()
    (stray_discard / "leftover.txt").write_text("stale from an interrupted prior swap", encoding="utf-8")
    assert stray_discard.exists()

    staged_path = staging.stage_path("rebuild")

    assert not stray_discard.exists(), "an interrupted prior swap's .discard sibling must be swept"
    assert staged_path == vault_path / "graph.rebuild.grafx"
