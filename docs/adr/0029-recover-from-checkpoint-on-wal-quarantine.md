# ADR 0029 — Recover From the Last Checkpoint on WAL Quarantine (WAL-Durability, P0)

**Status:** Accepted
**Date:** 2026-07-07
**Depends on:** ADR 0007 (rebuild from trust root)

---

## Context

Ladybug merges its write-ahead log (`graph.lbug.wal`) into the main checkpoint
file (`graph.lbug`) **only on a clean `Database.close()`**, or on an explicit
`CHECKPOINT` statement (see the 2026-07-29 addendum below — at the time this
ADR was written there was no periodic-flush or explicit-checkpoint call
anywhere in the ingest path). The daemon holds
one `ladybug.Database` open for its whole life, so every ingest appends to the
WAL and the WAL is not folded into the checkpoint until a clean shutdown. A
`kill -9` mid-write therefore leaves a **torn WAL next to an intact checkpoint**.

`bootstrap_vault_graph` (`src/marginalia/store/_bootstrap.py`) already detected
this corruption and quarantined the graph, but `_quarantine_graph` moved
**every** sibling starting with `graph.lbug` — the intact `graph.lbug`
checkpoint **and** the torn `graph.lbug.wal` — into `.marginalia/corrupt-graph`,
then started a FRESH empty graph. The last good checkpoint was discarded
alongside the torn WAL, so the store came back reporting **0 claims** even
though the checkpoint was fully readable.

Observed live: the E2/E3 reference-eval replay was killed mid-write; its next open
quarantined a **60 MB checkpoint holding 5389 claims** (117 blocks, 59
documents, 1165 concepts) together with an 863 KB torn WAL, and booted an empty
1.15 MB graph — a silent, total loss of a recoverable graph. Confirmed by
reopening the quarantined checkpoint alone (WAL removed): it opens cleanly and
returns all 5389 `Node[type=Claim]` rows.

Prod exposure: the live demo-vault daemon writes the WAL on every ingest and holds
the database open for its lifetime, so pre-fix a `kill -9`/crash mid-write would
have booted it EMPTY on the next open. This was a real P0 durability gap, not a
finale-only artifact.

## Decision

On WAL/graph corruption, **recover from the last good checkpoint** before wiping
anything. `_recover_from_corruption` replaces the wholesale
`_quarantine_graph` with a two-step recovery:

1. **Quarantine only the torn WAL/sidecars** (`_quarantine_sidecars` moves every
   sibling starting with `graph.lbug` **except** `graph.lbug` itself and
   marginalia's own `graph.lbug.bak` safety backup), leave the main checkpoint on
   disk, and retry the open. If it succeeds, every claim in the last checkpoint is
   recovered — `recovered_mode = "checkpoint"`. Writes made after the last
   checkpoint are genuinely lost with the torn WAL.
2. **Only if the main file itself is torn** (no checkpoint to recover) fall back
   to the old behavior: move the checkpoint (and any fresh partial WAL the failed
   retry dropped) aside too and start a FRESH empty graph —
   `recovered_mode = "empty"`.

`recovered_from_corruption` stays `True` in both modes (a hard kill happened and
the operator must know); the new `recovered_mode` field distinguishes what was
recovered. `/health` reports `status: degraded` in both cases with a
mode-accurate reason — the checkpoint case tells the operator to re-ingest
recent sources or run `kg rebuild` for the WAL-only writes; the empty case tells
them to reconstruct fully with `kg rebuild` from the durable markdown (the trust
root, ADR 0007).

## Consequences

- A `kill -9` mid-write over an intact checkpoint now recovers the checkpointed
  graph instead of booting empty. Verified end-to-end on the **real** E2/E3
  replay files: recovery yields **5389 claims** (was 0), quarantining only
  `graph.lbug.wal` while the checkpoint stays in place.
- Recovery is honest about staleness: because Ladybug only checkpoints on clean
  close, the recovered checkpoint reflects the last clean shutdown, and writes
  since then are lost with the torn WAL. `/health` surfaces this as `degraded`
  so the operator re-ingests or rebuilds; `kg rebuild` from markdown remains the
  full-recovery path.
- The torn-main-file case is unchanged (empty fallback + `kg rebuild`); the
  quarantine layout stays deterministic (`corrupt-graph`, `-2`, `-3`, …).
- marginalia's `graph.lbug.bak` (written by heal/rebuild/reconcile) is **preserved
  in place** across recovery, so it stays a manual fallback next to a recovered or
  empty graph instead of being swept into quarantine.
- Guarded by `tests/test_bootstrap.py`
  (`test_torn_wal_recovers_checkpointed_claims`,
  `test_bootstrap_recovers_from_corrupt_wal`,
  `test_bootstrap_recovers_from_corrupt_graph_file`).

## Addendum — 2026-07-29: periodic mid-session checkpoint + graph-verified receipts

This ADR's recovery is only as good as the last checkpoint, and until now the
only checkpoint was the one taken on a clean `Database.close()` — exactly the
event an unclean shutdown skips. A long live ingest (tonight's incident: a
~70-minute run against the ADR 0040 CoP golden vault built on 2026-07-29) that never
closed cleanly therefore had its ENTIRE graph in the WAL; one `kill -9`
quarantined two torn WALs (707 KB and 11.1 MB) against a 124 KB pre-ingest
`graph.lbug`, and the "recovered" checkpoint was the empty starting state —
recovery worked exactly as designed above, there was simply nothing better to
recover to. Meanwhile `ingest-history.json` (a sidecar the drain worker writes
as it goes, not the graph) still reported 17/17 items `done`,
`receipts_complete`, and integrity `verified`, because a sidecar has no way to
observe that the graph it describes no longer exists.

Two fixes, both minimal and inside this ADR's existing recovery model:

1. **`GraphStore.checkpoint()`** (`LadybugStore.checkpoint`,
   `src/marginalia/store/ladybug.py`) issues Ladybug's `CHECKPOINT;` statement
   directly on the writer's own connection — the merge this ADR's Context
   section said had "no periodic-flush or explicit-checkpoint call" now has
   one, invoked mid-session, without closing the database. The ingest drain
   worker (`_drain` in `src/marginalia/server/_ingest_queue.py`) calls it once
   a document reaches a terminal state, still inside `writer_lock` so no
   second writer is ever involved. This does not change how corruption is
   *classified or recovered* — it only makes "the last good checkpoint" land
   far more often than "server shutdown," capping what an unclean shutdown can
   lose to "since the last document finished" instead of "the whole run."
   Best-effort: a checkpoint failure logs and is skipped, never flips an
   already-committed document to `error`.
   Proven with a real `SIGKILL` + hand-torn post-checkpoint WAL (the same
   torn-WAL technique this ADR's own tests use) in
   `tests/store/test_checkpoint_durability.py`.
2. **Live-graph receipt verification** (`verify_receipt` in
   `src/marginalia/server/_ingest_queue.py`) stops trusting the sidecar's
   historical claim at face value: at receipt-check time (`GET
   /api/v1/ingest-queue/{id}`, and before evaluating retryability) it checks
   the graph directly for the item's `Document` node and at least one `Block`.
   A `done` item whose graph content is missing is durably corrected to
   `outcome.quality = "graph_missing"`, `receipts_complete = False`, and made
   explicitly retryable — so it surfaces the divergence instead of repeating
   the sidecar's now-false claim, and can be re-queued through the existing
   `retry_item` API with no separate recovery path.
   Covered by `tests/server/test_ingest_queue_graph_receipts.py`, including
   the exact tonight shape (sidecar `done` + `receipts_complete`, graph
   empty). A caught divergence also appends a `graph_verification_failed`
   event to the item's inspector history, so it stays visible after the
   correction rather than only flipping a field silently.
   The check only fires for a `done` item whose outcome actually asserts a
   `quality` (mirroring `_item_retryable`'s own "empty outcome means
   legacy/unclassified" treatment) — a bare placeholder that never claimed
   anything complete has nothing to cross-check and stays untouched.
   Verified against the real 17 tonight-incident sidecar entries (every one
   carries `"quality": "complete"`, so the gate above does not exempt them)
   and, end to end, against a real `Companion.remember()` run (not just the
   deterministic `ingest_document` helper — `remember()`'s structural pass
   goes through the same `vault.add()` → `ingest_document` path, confirmed by
   a Companion+StubLLM test rather than assumed from reading the call chain).
   Scope: this check runs at the per-item read (`GET
   /api/v1/ingest-queue/{id}`) and on `retry_item`, not on the bulk
   `GET /api/v1/ingest-queue` list poll — a full-graph Block scan on every
   item on every poll of a large retained queue was judged too expensive for
   a background gate; the list view can still show a stale `receipts_complete`
   for an item nobody has opened yet, self-correcting once it is.
