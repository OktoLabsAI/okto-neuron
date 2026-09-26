"""Two real OS processes racing ``GrafxStore.add_node`` on one brand-new id
(M4 spec §4 bullet 1 -- the plan's ``done_when`` case for D-10's retry
policy: "handle optimistic write conflicts with retry").

Deliberately **two real OS processes**, not two threads or two transactions
from one process: the production scenario D-10 defends against is two
separate ``marginalia``/``kg`` invocations (or a daemon plus a CLI call)
writing to the same vault at once, each with its own ``okto_grafx.connect()``
handle -- a same-process simulation would prove only that Grafx's Python
bindings serialize correctly under the GIL, not that two independent
connections against one on-disk directory actually collide and recover the
way the M4 spec's live verification found ("first commit wins, loser raises
``GrafxWriteConflict`` (``retryable=True``), row never lands").

Rather than hoping OS scheduling collides two unsynchronized ``add_node``
calls (flaky either way it lands, and -- verified empirically while building
this test -- a writer merely *paused before opening* its transaction never
actually conflicts: by the time it begins, the winner's write is already
committed and visible, so it just performs an uncontended `MERGE`), the
"loser" process opens a real write transaction, issues its `MERGE`, and only
then pauses -- holding that transaction open while the "winner" process
performs a complete, uncontended add_node (open, write, commit) and signals
back. Only then does the loser attempt its own ``commit()``, which is where
Grafx's own MVCC detects the overlap and refuses it. The loser's
``GrafxWriteConflict`` is caught here (not just assumed) and reported back
through the result queue, so this test fails loudly if the conflict
mechanism itself ever stops firing instead of silently degenerating into an
uncontended sequential write.

Both processes' nodes carry identical payloads (type/title/content/tags/
facets/provenance/embedding) so the loser's retry hits
``GrafxStore.add_node``'s ``_same_node_payload`` short-circuit exactly the
way two concurrent writers producing the same logical node would in
practice -- the retry's re-read adopts the winner's row outright rather than
re-writing it, which is what "the loser's retry leaves ``created_at`` equal
to the first landed write" (this file's assertion) actually verifies.
"""

from __future__ import annotations

import multiprocessing as mp
from datetime import datetime
from pathlib import Path

import pytest

pytest.importorskip("okto_grafx")

from okto_neuron.core.schema import Node, Provenance  # noqa: E402
from okto_neuron.store.grafx import GrafxStore  # noqa: E402

_RACE_ID = "race-node-brand-new"

#: Generous but bounded -- CI machines are slower than a dev laptop, but a
#: hang here means the synchronization primitives themselves are broken, not
#: that Grafx is merely slow.
_WAIT_SECONDS = 20
_JOIN_SECONDS = 30


def _race_node() -> Node:
    return Node(
        id=_RACE_ID,
        type="Concept",
        title="racing concept",
        content="written by a concurrent add_node race",
        tags=["race"],
        facets={"probe": "write-race"},
        provenance=Provenance(source="ingest", layer="deterministic"),
    )


def _delayed_writer(vault_path: str, about_to_write, release_write, result_queue: "mp.Queue") -> None:
    """The forced loser: holds an OPEN write transaction across the pause.

    A genuine optimistic-concurrency conflict needs both transactions
    simultaneously *open* (``db.begin("write")`` + ``execute()``), not
    merely two writes issued back-to-back -- a paused-before-``begin``
    writer's transaction would start strictly after the winner's already
    committed and just uncontendedly update the row (verified empirically
    while building this test: that shape never raises ``GrafxWriteConflict``
    at all). So this duplicates ``GrafxStore._execute_write``'s own
    begin/execute/commit body (reaching into the private ``store._db``
    handle, matching ``tests/store/test_grafx_store_dim.py``'s existing
    convention) and inserts the pause between ``execute`` and ``commit`` --
    exactly where ``okto_grafx``'s own concurrency tests
    (``tests/api/test_public_boundary_concurrency.py``'s ``_conflicted_writer``
    in the ``okto-grafx`` checkout) hold two transactions open across a
    winner's commit to provoke the same conflict, just across two real
    connections in two real OS processes here instead of two transactions on
    one connection.
    """
    store = GrafxStore(Path(vault_path))
    original_execute_write = store._execute_write
    state = {"paused_once": False, "conflict_observed": False}

    def paused_execute_write(statement: str, params: object):
        if state["paused_once"]:
            return original_execute_write(statement, params)
        state["paused_once"] = True
        txn = store._db.begin("write")
        try:
            txn.execute(statement, params)
            about_to_write.set()
            if not release_write.wait(_WAIT_SECONDS):
                raise AssertionError("winner never released the paused writer")
            txn.commit()
        except Exception as exc:
            if txn.active:
                txn.rollback()
            if getattr(exc, "retryable", False):
                state["conflict_observed"] = True
            raise
        return None

    store._execute_write = paused_execute_write  # instance shadow, no source edit
    node = _race_node()
    try:
        store.add_node(node)
        result_queue.put(
            {
                "role": "loser",
                "ok": True,
                "created_at": node.created_at.isoformat(),
                "conflict_observed": state["conflict_observed"],
            }
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the parent, not swallowed
        result_queue.put({"role": "loser", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        store.close()


def _prompt_writer(vault_path: str, about_to_write, release_write, result_queue: "mp.Queue") -> None:
    """The forced winner: writes uncontended, then releases the paused loser."""
    if not about_to_write.wait(_WAIT_SECONDS):
        result_queue.put({"role": "winner", "ok": False, "error": "loser never reached its pause point"})
        return
    store = GrafxStore(Path(vault_path))
    node = _race_node()
    try:
        store.add_node(node)
        result_queue.put({"role": "winner", "ok": True, "created_at": node.created_at.isoformat()})
    except Exception as exc:  # noqa: BLE001
        result_queue.put({"role": "winner", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        store.close()
        release_write.set()


def test_two_processes_racing_add_node_on_a_brand_new_id_exactly_one_wins(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    # Bootstrap the schema/metadata row once, up front, in the parent
    # process -- that bootstrap has its own (already-covered) adopt-or-write
    # race in GrafxStore._bootstrap_or_adopt_metadata; this test isolates the
    # add_node race on an already-open graph, matching the plan's done_when
    # wording ("add_node the same brand-new id").
    bootstrap = GrafxStore(vault_path)
    bootstrap.close()

    ctx = mp.get_context("spawn")
    about_to_write = ctx.Event()
    release_write = ctx.Event()
    result_queue: "mp.Queue" = ctx.Queue()

    loser = ctx.Process(
        target=_delayed_writer,
        args=(str(vault_path), about_to_write, release_write, result_queue),
    )
    winner = ctx.Process(
        target=_prompt_writer,
        args=(str(vault_path), about_to_write, release_write, result_queue),
    )
    loser.start()
    winner.start()
    try:
        loser.join(_JOIN_SECONDS)
        winner.join(_JOIN_SECONDS)
        assert loser.exitcode == 0, "the delayed (loser) writer process crashed"
        assert winner.exitcode == 0, "the prompt (winner) writer process crashed"
    finally:
        for process in (loser, winner):
            if process.is_alive():
                process.kill()
                process.join(5)

    results = {r["role"]: r for r in (result_queue.get(timeout=5) for _ in range(2))}
    assert set(results) == {"loser", "winner"}, results
    assert results["winner"]["ok"], results["winner"]
    assert results["loser"]["ok"], results["loser"]

    # The core correctness claim (D-10): the loser's commit was genuinely
    # refused by Grafx's own MVCC, not merely skipped by scheduling luck.
    assert results["loser"]["conflict_observed"] is True, (
        "the forced-late writer must hit a real GrafxWriteConflict on its "
        "first commit attempt, or this test isn't exercising the retry path"
    )

    final_store = GrafxStore(vault_path)
    try:
        landed = final_store.get_node(_RACE_ID)
    finally:
        final_store.close()

    assert landed is not None, "exactly one row must land under the raced id"
    winner_created_at = datetime.fromisoformat(results["winner"]["created_at"])
    loser_created_at = datetime.fromisoformat(results["loser"]["created_at"])

    # The loser's retry adopted the winner's already-committed row instead of
    # overwriting it (identical payloads short-circuit on `created_at` alone)
    # -- the stored created_at is the winner's, preserved through the retry.
    assert abs((landed.created_at - winner_created_at).total_seconds()) < 0.01, (
        landed.created_at,
        winner_created_at,
    )
    assert abs((landed.created_at - loser_created_at).total_seconds()) > 0.001, (
        "the loser's own created_at must not have landed -- its retry must "
        "have adopted the winner's row rather than overwriting it",
        landed.created_at,
        loser_created_at,
    )
