"""Task #12 — Ladybug durability: checkpoint during long live ingests.

Background: Ladybug only merges its write-ahead log into ``graph.lbug`` on a
CLEAN close. A long live ingest that is killed (crash, OOM, ``kill -9``, a
daemon restart) before it ever closes cleanly leaves everything written since
the last merge stranded in the WAL. If that WAL is then torn by the kill
(a partial record at the tail), the existing recovery path
(``_recover_from_corruption``) correctly falls back to the last good
checkpoint — but if NO checkpoint was ever taken during the run, "the last
good checkpoint" is the pre-ingest empty graph, and the whole run is lost even
though recovery itself worked exactly as designed.

``LadybugStore.checkpoint()`` (this module's subject) lets a long-running
writer force that merge mid-session, at safe drain points, without closing
the database — capping how much an unclean shutdown can lose to "whatever was
written since the last checkpoint" instead of "the whole run".

Confirmed experimentally before writing these tests: a REAL ``SIGKILL`` of a
process that finished writing (no in-flight record) leaves an INTACT,
untorn WAL that Ladybug replays cleanly on next open — so a naive
"kill after ingest, reopen, assert data present" test passes even at HEAD
and proves nothing. The incident's forensics (quarantined WALs) show the
real failure mode is a genuinely TORN WAL. Test B below reproduces that
precisely, the same way the existing corruption-recovery suite
(``tests/test_bootstrap.py``) does: hand-write torn WAL bytes after a real,
uncontrolled process death has released every fd on the file.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import signal
import time
from pathlib import Path

import pytest

from okto_neuron.core.schema import Node
from okto_neuron.store._bootstrap import reset_bootstrap_cache_for_tests
from okto_neuron.store.ladybug import LadybugStore


# ---------------------------------------------------------------------------
# Test A: checkpoint() merges the WAL synchronously, without closing anything.
# ---------------------------------------------------------------------------


def test_checkpoint_merges_wal_without_clean_close(tmp_path: Path) -> None:
    """The mechanism, in isolation: writes land in the WAL only, then
    ``checkpoint()`` (with the database still open, no ``close()`` call
    anywhere in this test) merges them into ``graph.lbug`` and empties the
    WAL. This is what today's HEAD cannot do — ``LadybugStore`` has no
    ``checkpoint`` method, so this test fails with ``AttributeError`` before
    the fix and passes after it.
    """
    vault_path = tmp_path / "vault"
    wal_path = vault_path / "graph.lbug.wal"
    graph_path = vault_path / "graph.lbug"

    store = LadybugStore(vault_path)
    try:
        for i in range(20):
            store.add_node(Node(id=f"claim-{i}", type="Claim", title=f"claim {i}"))

        # Pending writes live in the WAL only — not yet merged.
        assert wal_path.exists()
        assert wal_path.stat().st_size > 0
        graph_size_before = graph_path.stat().st_size

        store.checkpoint()

        # Merged: the WAL is gone/empty and the main file grew to hold it —
        # all WITHOUT ever calling store.close() or database.close().
        assert not wal_path.exists() or wal_path.stat().st_size == 0
        assert graph_path.stat().st_size >= graph_size_before

        # And the data is readable through the still-open store, unaffected.
        assert len(list(store.list_nodes("Claim"))) == 20
    finally:
        store.close()


def test_checkpoint_is_a_noop_for_in_memory_store() -> None:
    """The GraphStore protocol gained ``checkpoint()``; the in-memory backend
    (already fully durable in-process) implements it as a harmless no-op."""
    from okto_neuron.store.memory import InMemoryStore

    store = InMemoryStore()
    store.checkpoint()  # must not raise
    store.close()


# ---------------------------------------------------------------------------
# Test B: real SIGKILL + a genuinely torn post-checkpoint WAL.
# ---------------------------------------------------------------------------


def _ingest_two_documents_then_wait(vault_path: str, ready: "mp.synchronize.Event") -> None:
    """Child process body (must be a real, killable OS process — see module
    docstring for why an in-process simulation cannot exercise this).

    Writes "doc1" then checkpoints (simulating the fix's post-drain
    checkpoint), then writes "doc2" with NO further checkpoint (simulating
    the next document, still mid-ingest when the kill lands) before
    signalling readiness and blocking until killed.
    """
    store = LadybugStore(Path(vault_path))
    for i in range(10):
        store.add_node(Node(id=f"doc1-claim-{i}", type="Claim", title=f"doc1 claim {i}"))
    store.checkpoint()
    for i in range(10):
        store.add_node(Node(id=f"doc2-claim-{i}", type="Claim", title=f"doc2 claim {i}"))
    ready.set()
    time.sleep(30)  # killed well before this returns


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only SIGKILL semantics")
def test_sigkill_after_checkpoint_survives_a_torn_post_checkpoint_wal(
    tmp_path: Path,
) -> None:
    """The end-to-end durability proof the task asked for.

    A real OS process is SIGKILLed mid-run (after doc1's checkpoint, with
    doc2 still WAL-only). We then hand-tear the WAL exactly like the real
    incident (a kill mid-write corrupts the tail record) — this is safe only
    because the child is truly dead and has released every fd on the file, a
    precondition a same-process simulation cannot offer. Restarting must
    recover doc1's checkpointed claims even though doc2 (never checkpointed)
    is legitimately gone with the torn WAL.

    Fails on today's HEAD: with no ``checkpoint()`` call available, doc1 and
    doc2 both live only in the single WAL that gets torn and quarantined —
    0 claims survive. Passes with the fix: doc1's 10 claims are recovered.
    """
    vault_path = tmp_path / "vault"
    wal_path = vault_path / "graph.lbug.wal"

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    process = ctx.Process(
        target=_ingest_two_documents_then_wait,
        args=(str(vault_path), ready),
    )
    process.start()
    try:
        assert ready.wait(20), "child never reached the post-checkpoint wait point"
        # Give the second batch of writes a moment to actually land on disk
        # before we tear the WAL (they are unmerged either way; this just
        # avoids a flaky "wal file doesn't exist yet" race).
        deadline = time.monotonic() + 5.0
        while not wal_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        os.kill(process.pid, signal.SIGKILL)
        process.join(10)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)

    # kill -9 mid-write leaves a torn WAL — the exact incident forensics
    # shape (a WAL quarantined for failing checksum verification).
    assert wal_path.exists(), "expected doc2's post-checkpoint writes in a WAL"
    wal_path.write_bytes(b"\xde\xad\xbe\xef" * 200)

    reset_bootstrap_cache_for_tests(vault_path)
    store = LadybugStore(vault_path)
    try:
        from okto_neuron.vault import Vault

        vault = Vault(vault_path, store)
        assert vault.recovered_from_corruption is True
        assert vault.recovery_mode == "checkpoint"

        doc1_claims = [n for n in store.list_nodes("Claim") if n.id.startswith("doc1-claim-")]
        assert len(doc1_claims) == 10, (
            "doc1 was checkpointed before the kill; it must survive the torn "
            "post-checkpoint WAL that swallowed doc2"
        )
    finally:
        store.close()
