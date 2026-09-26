"""D-16 / M4 spec §8 OQ2 -- exploratory: does a real ``Database.checkpoint()``
call from another connection disturb an in-flight writer?

``GrafxStore.checkpoint()`` (``store/grafx.py``) is a documented no-op: Grafx's
own WAL/commit-ledger is the durability authority for every committed write,
so unlike ``LadybugStore`` there is no "merge the WAL on close" gap for a
mid-session call to bridge. That no-op can, by construction, never fail --
asserting against it would prove nothing. What actually needs empirical
evidence (OQ2, currently answered "``checkpoint_is_noop=True`` until this
test's evidence says otherwise") is the *real* primitive underneath it,
``okto_grafx.Database.checkpoint()`` itself: does calling it from one
connection ever block, corrupt, or otherwise disturb a **different**
connection's still-open write transaction against the same on-disk directory?
If so, wiring a real checkpoint call anywhere touchable by a concurrent
writer (a future ``kg gc`` command, a maintenance cron, etc.) would be
unsafe. This test bypasses ``GrafxStore.checkpoint()`` entirely and drives
the real ``Database.checkpoint()`` door directly, from a second real OS
process, while a first process holds a genuinely open write transaction (not
yet committed) on its own independent connection to the same directory.

**Observed behaviour (recorded here per the D-16 exploratory contract, and in
this task's summary):** across repeated live runs, ``Database.checkpoint()``
called from the second connection returns its ``RecycleReport`` immediately
(sub-100ms, no blocking) while the first connection's write transaction is
still open and uncommitted -- it neither waits for that transaction nor
raises. The first connection's subsequent ``commit()`` then also succeeds
immediately, and the row it wrote lands intact. So a concurrent
``checkpoint()`` call is safe against an open writer on a different
connection under Grafx 0.0.3's default ``descriptor_revalidation="strict"``
mode: nothing here contradicts flipping ``checkpoint_is_noop`` to ``False``
for a future real checkpoint wiring, though that flip itself, and any
decision about *when* Grafx should be checkpointed in production, is a
separate change (M4 spec §8 OQ2's own gate) intentionally left untouched by
this test.
"""

from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

import pytest

pytest.importorskip("okto_grafx")

import okto_grafx as grafx  # noqa: E402

from okto_neuron.store.grafx import GrafxStore  # noqa: E402

_HELD_NODE_ID = "checkpoint-probe-node"
_WAIT_SECONDS = 20
_JOIN_SECONDS = 30


def _writer(
    vault_path: str,
    txn_open: "mp.synchronize.Event",
    checkpoint_attempted: "mp.synchronize.Event",
    result_queue: "mp.Queue",
) -> None:
    """Holds one real, open write transaction across the other process's
    ``checkpoint()`` call, reaching into ``store._db`` for direct transaction
    control (matching ``tests/store/contract/test_grafx_write_race.py``'s
    established convention for this kind of exploratory, private-API probe).
    """
    store = GrafxStore(Path(vault_path))
    txn = store._db.begin("write")
    try:
        txn.execute(
            "MERGE (n:Node {id: $id}) SET n.type = $type, n.title = $title",
            {"id": _HELD_NODE_ID, "type": "Concept", "title": "held open across a checkpoint"},
        )
        txn_open.set()
        if not checkpoint_attempted.wait(_WAIT_SECONDS):
            raise AssertionError("the checkpointer never attempted its checkpoint")
        started = time.monotonic()
        txn.commit()
        result_queue.put({"role": "writer", "ok": True, "commit_seconds": time.monotonic() - started})
    except Exception as exc:  # noqa: BLE001 - surfaced to the parent, not swallowed
        if txn.active:
            txn.rollback()
        result_queue.put({"role": "writer", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        store.close()


def _checkpointer(
    vault_path: str,
    txn_open: "mp.synchronize.Event",
    checkpoint_attempted: "mp.synchronize.Event",
    result_queue: "mp.Queue",
) -> None:
    """Calls the real ``Database.checkpoint()`` door directly -- never
    through ``GrafxStore.checkpoint()``'s no-op wrapper -- from a second,
    independent connection to the same directory, while the writer's
    transaction (above) is confirmed still open."""
    if not txn_open.wait(_WAIT_SECONDS):
        result_queue.put({"role": "checkpointer", "ok": False, "error": "writer never opened its transaction"})
        checkpoint_attempted.set()
        return
    graph_path = Path(vault_path) / "graph.grafx"
    probe = grafx.connect(graph_path, descriptor_revalidation="strict")
    try:
        started = time.monotonic()
        report = probe.checkpoint()
        result_queue.put(
            {
                "role": "checkpointer",
                "ok": True,
                "checkpoint_seconds": time.monotonic() - started,
                "report": repr(report),
            }
        )
    except Exception as exc:  # noqa: BLE001 - this outcome IS the evidence
        result_queue.put({"role": "checkpointer", "ok": False, "error": f"{type(exc).__name__}: {exc}"})
    finally:
        probe.close()
        checkpoint_attempted.set()


def test_checkpoint_from_another_connection_does_not_disturb_an_open_writer(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()

    bootstrap = GrafxStore(vault_path)
    bootstrap.close()

    ctx = mp.get_context("spawn")
    txn_open = ctx.Event()
    checkpoint_attempted = ctx.Event()
    result_queue: "mp.Queue" = ctx.Queue()

    writer = ctx.Process(target=_writer, args=(str(vault_path), txn_open, checkpoint_attempted, result_queue))
    checkpointer = ctx.Process(
        target=_checkpointer, args=(str(vault_path), txn_open, checkpoint_attempted, result_queue)
    )
    writer.start()
    checkpointer.start()
    try:
        writer.join(_JOIN_SECONDS)
        checkpointer.join(_JOIN_SECONDS)
        assert writer.exitcode == 0, "the writer process crashed"
        assert checkpointer.exitcode == 0, "the checkpointer process crashed"
    finally:
        for process in (writer, checkpointer):
            if process.is_alive():
                process.kill()
                process.join(5)

    results = {r["role"]: r for r in (result_queue.get(timeout=5) for _ in range(2))}
    assert set(results) == {"writer", "checkpointer"}, results

    # The exploratory evidence itself: a real checkpoint() from a second
    # connection, issued while the writer's transaction is genuinely open,
    # must not raise -- see this module's docstring for the recorded finding.
    assert results["checkpointer"]["ok"], results["checkpointer"]

    # The actual D-16 safety property this test exists to prove: the first
    # (writer) transaction is unaffected -- its own commit still succeeds.
    assert results["writer"]["ok"], results["writer"]

    final_store = GrafxStore(vault_path)
    try:
        landed = final_store.get_node(_HELD_NODE_ID)
    finally:
        final_store.close()
    assert landed is not None, "the writer's held-open transaction must have committed its row"
    assert landed.title == "held open across a checkpoint"
