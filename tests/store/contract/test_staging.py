"""Contract tests for ``store/staging.py``'s ``StagingPort``/``LadybugStaging``
(M2b spec §2.1, §4 bullet 1).

``populated``/``graph_store`` (``tests/store/contract/conftest.py``) already
parametrize every contract test over both backends. ``InMemoryStore`` keeps
everything in a process-local dict — it has no on-disk graph family for
``LadybugStaging`` to stage, commit, discard, or restore, so that param is a
documented skip here (:func:`_live_vault_path_or_skip`), never a fabricated
in-memory staging concept. Only the ``ladybug`` param exercises real cases:
stage -> populate -> commit round-trips real graph content onto live and
preserves the pre-swap live graph verbatim as the backup; ``discard`` removes
a staged graph and leaves live untouched; ``restore`` moves a backup graph
back onto live; and a commit that fails partway through the physical move
restores the pre-swap live graph byte-for-byte, matching
``LadybugStaging.commit``'s documented failure semantics (a verbatim
relocation of ``cli/kg.py``'s pre-M2b ``_swap_rebuilt_graph``).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from okto_neuron.cli.kg import _bootstrap_graph_at_path, _open_live_handle
from okto_neuron.core.schema import Edge, Node
from okto_neuron.errors import RebuildSwapFailed
from okto_neuron.store.ladybug import LadybugStore
from okto_neuron.store.staging import LadybugStaging


def _live_vault_path_or_skip(store: object) -> Path:
    if not isinstance(store, LadybugStore):
        pytest.skip("InMemoryStore has no on-disk graph family to stage (M2b spec §4 bullet 1)")
    return store.vault_path


def _build_staged_store(vault_path: Path, staging: LadybugStaging, tag: str) -> tuple[Path, LadybugStore]:
    staged_path = staging.stage_path(tag)
    tmp_handle = _bootstrap_graph_at_path(vault_path, staged_path)
    return staged_path, LadybugStore(staged_path, graph_handle=tmp_handle)


def _open_existing(path: Path) -> LadybugStore:
    """Open an already-built graph file directly (a backup or restored path),
    bypassing ``LadybugStore(vault_path)``'s directory-bootstrap path — the
    same "key the store on the file, not a vault directory" pattern
    ``server/_curation.py`` uses for tmp/staging stores (see the facts
    sheet's §4 landmine note)."""
    handle = _open_live_handle(path, path)
    return LadybugStore(path, graph_handle=handle)


def test_stage_populate_commit_round_trips_real_graph_data(populated, corpus_nodes):
    vault_path = _live_vault_path_or_skip(populated)
    corpus_ids = {node.id for node in corpus_nodes}
    populated.close()  # production always closes the live handle before a swap

    staging = LadybugStaging(vault_path)
    staged_path, staged_store = _build_staged_store(vault_path, staging, "rebuild")
    assert staged_path == vault_path / "graph.rebuild.lbug"
    try:
        staged_store.add_node(Node(id="staged-only", type="Concept", title="staged content"))
        staged_store.add_node(Node(id="staged-other", type="Concept", title="second staged node"))
        staged_store.add_edge(
            Edge(id="e-staged", type="mentions", src="staged-only", dst="staged-other")
        )
        staged_store.checkpoint()
    finally:
        staged_store.close()

    backup_path = staging.commit(staged_path, backup_tag="bak")

    assert backup_path == vault_path / "graph.lbug.bak"
    assert not staged_path.exists(), "the staged path is consumed by the swap"
    assert (vault_path / "graph.lbug").exists()

    live = LadybugStore(vault_path)
    try:
        assert {n.id for n in live.list_nodes()} == {"staged-only", "staged-other"}
        assert [e.id for e in live.list_edges(src="staged-only")] == ["e-staged"]
    finally:
        live.close()

    backup = _open_existing(backup_path)
    try:
        assert {n.id for n in backup.list_nodes()} == corpus_ids
    finally:
        backup.close()


def test_discard_removes_staged_graph_and_leaves_live_untouched(populated, corpus_nodes):
    vault_path = _live_vault_path_or_skip(populated)
    corpus_ids = {node.id for node in corpus_nodes}

    staging = LadybugStaging(vault_path)
    staged_path, staged_store = _build_staged_store(vault_path, staging, "heal")
    try:
        staged_store.add_node(Node(id="would-be-discarded", type="Concept", title="discarded"))
        staged_store.checkpoint()
    finally:
        staged_store.close()
    assert staged_path.exists()

    staging.discard(staged_path)

    assert not staged_path.exists()
    # discard() never touches live: no swap happened, same corpus nodes as before.
    assert {n.id for n in populated.list_nodes()} == corpus_ids


def test_restore_moves_a_backup_graph_back_onto_live(populated, corpus_nodes):
    vault_path = _live_vault_path_or_skip(populated)
    corpus_ids = {node.id for node in corpus_nodes}
    populated.close()

    staging = LadybugStaging(vault_path)
    staged_path, staged_store = _build_staged_store(vault_path, staging, "rollback")
    try:
        staged_store.add_node(Node(id="post-swap-only", type="Concept", title="after the swap"))
        staged_store.checkpoint()
    finally:
        staged_store.close()

    backup_path = staging.commit(staged_path, backup_tag="rollback-bak")
    live = LadybugStore(vault_path)
    try:
        assert {n.id for n in live.list_nodes()} == {"post-swap-only"}
    finally:
        live.close()

    staging.restore(backup_path)

    assert not backup_path.exists()
    restored = LadybugStore(vault_path)
    try:
        assert {n.id for n in restored.list_nodes()} == corpus_ids
    finally:
        restored.close()


def test_commit_failure_mid_move_restores_the_pre_swap_live_graph_byte_for_byte(
    populated, corpus_nodes, monkeypatch: pytest.MonkeyPatch
):
    vault_path = _live_vault_path_or_skip(populated)
    populated.close()
    live_graph_path = vault_path / "graph.lbug"
    original_bytes = live_graph_path.read_bytes()

    staging = LadybugStaging(vault_path)
    staged_path, staged_store = _build_staged_store(vault_path, staging, "rebuild")
    try:
        staged_store.add_node(Node(id="never-lands-live", type="Concept", title="never lands"))
        staged_store.checkpoint()
    finally:
        staged_store.close()

    real_replace = os.replace
    tripped = {"once": False}

    def flaky_replace(src, dst, *args, **kwargs):
        # commit() does two os.replace calls with dst == live_graph_path: the
        # real tmp->live move (fail exactly this one) and, inside the
        # except-block's own recovery, backup->live (must succeed, or live
        # is never actually restored).
        if Path(dst) == live_graph_path and not tripped["once"]:
            tripped["once"] = True
            raise OSError("simulated failure moving the staged graph onto live")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr("okto_neuron.store.staging.os.replace", flaky_replace)

    with pytest.raises(RebuildSwapFailed):
        staging.commit(staged_path, backup_tag="bak")

    assert tripped["once"]
    assert live_graph_path.exists()
    assert live_graph_path.read_bytes() == original_bytes
    assert not (vault_path / "graph.lbug.bak").exists()
    assert staged_path.exists(), "the staged graph is left in place, not auto-discarded"
