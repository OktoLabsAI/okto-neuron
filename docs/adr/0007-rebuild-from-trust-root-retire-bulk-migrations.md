# ADR 0007: `kg rebuild` Re-Extracts the Trust Root into a Fresh Graph; Retire Bulk In-Place Migrations

**Status:** Accepted
**Date:** 2026-06-04
**Deciders:** Alex Rivera, Marginalia core
**Supersedes:** None
**Amends:** ADR 0005, ADR 0006 (retires their in-place *backfill* heal path; the LIVE per-commit `ensure_source_mentions` they introduced stays)

---

## Context

The NX test vault showed "rogue nodes" — ~150 Claims reading as degree-0/1 orphans. Investigation went through **three wrong, walked-back theories** (edge-id collision → read-path skew → "any populated write corrupts") before the real cause was pinned. The lesson that shaped the method: every wrong verdict came from a database read that was never independently validated. The final root cause was established only with measurements that **cannot be contaminated by the bug under investigation**:

- **id-bound reads validated against node-anchored reads** (two different access paths agree 50/50 → the per-id read is trustworthy).
- **the markdown trust root** — the source files themselves, which no Ladybug read bug can touch.

### What is proven

- **The content-addressed edge `id` (`sha256_hex("edge", src, type, dst)`) is ground truth.** For all 1023 "corrupt" claim-edges, the id reproduces from each Claim's OWN facets (`S_id`/`O_id`/`block_id`), markdown-verified. **Zero** deep id corruption.
- **The casualties are the stored scalar columns `e.src`/`e.dst`/`e.type` AND the FROM/TO adjacency** — they disagree with the id on corrupt edges. Only the id survives.
- **The corruptor is a BULK whole-store in-place migration pass** run over an already-populated graph: `backfill_bridge_edges` / `ensure_source_mentions(store, node_ids=None)` in `migrate/bridge_edges.py`, exposed as `kg migrate bridge-edges`. **Deterministic reproduction:** copy a clean backup (0 corrupt) → run the HEAD migration → **0 → 1227** non-reproducing edges (321 mints corrupt 1227, *including edges the migration never touched* — a storage-reorganization signature).
- **It is NOT a write-Cypher-form bug.** DELETE-by-id+CREATE, CREATE-only, MERGE, and buffered writes all produce identical `0 → 1227`. Changing `add_edge`'s upsert does nothing.
- **It is NOT a reader bug.** On a clean vault, the full-scan `list_edges()` agrees with id-bound reads 50/50. Every earlier "full-scan skew" was the reader faithfully reporting genuinely-corrupt on-disk data.
- **Incremental ingest is SAFE.** Real `Companion.remember()` added +424 edges across 6 files onto existing high-degree entities (incl. the live `ensure_source_mentions(node_ids=<committed set>)` call) with id-bound non-reproducing staying `0 → 0` throughout, past the ~321 threshold. High-degree attachment was refuted as the trigger (synthetic 400-edge hub attach = `0 → 0`).

### Open (honest unknown — does not block the fix)

The engine-internal reason the bulk migration pass corrupts while incremental writes and synthetic 400-edge hub writes do not is **unpinned** — a specific-node / storage-layout-dependent Ladybug defect when bulk-minting into a populated graph. We pinned *which* operations corrupt (deterministic, reproducible), not the engine *why*. → file an upstream Ladybug reproduction. The fix retires the offending operations regardless of the engine internals.

---

## Decision

1. **`kg rebuild` becomes a real trust-root rebuild.** It re-extracts every source markdown file (`.marginalia/sources/*.md` plus `notes/` and `refs/`) through the **same full `Companion.remember()` pipeline** live ingest uses (deterministic Document/Block/Claim **plus** LLM entity/relationship extraction) into a **fresh, empty graph**, then atomic-swaps it onto LIVE (reusing the existing bootstrap + `os.replace` swap + handle-invalidation machinery). Building fresh and ingesting is proven corruption-free.

2. **Retire the bulk in-place migrations.** Remove the `kg migrate bridge-edges` CLI command and the whole-store `ensure_source_mentions(store, node_ids=None)` heal call. **Keep** the safe per-commit `ensure_source_mentions(store, committed_node_ids)` call in `remember()` — that path is proven safe and remains the live source-link mechanism (ADR 0006).

3. **Principle (guard).** Never bulk-mutate edges in place on a populated Ladybug graph; route all heals through `kg rebuild` (fresh-graph). A note to this effect sits at `add_edge` (the destructive DELETE-by-id-then-CREATE upsert).

4. **The reader is left unchanged** — it has no bug.

---

## Consequences

- **Heal proof (real NX-vault-scale copy, id-bound + node-anchored):** corruption **1227 → 0** inconsistent content-addressed edges, **1680 → 0** adjacency/stored-property mismatches, **150 → 0** degree-≤1 Claims. The rebuilt graph is rich, not a skeleton: **2201 nodes / 8629 edges / 547 primitive entities**, full PROV-O + RDF provenance scaffold + 240+ semantic relationship types; `schema:mentions` (the type that corrupted) present and 100% consistent. Wall time ≈ 45 min (GLM extraction latency over 17 source files).
- **Rebuild is a regenerate-from-source, not a byte-for-byte restore.** LLM re-extraction is non-deterministic, so node/edge counts differ from the prior graph (and topology edges carry random `_uid` ids). This is inherent to "markdown is the trust root; the graph is derived," and is consistent with the already-`xfail`ed rebuild-determinism test.
- **The markdown trust root (`.marginalia/sources/`) is now load-bearing for recovery** — it must be preserved (backed up) since it is the only thing a rebuild can reconstruct from.
- **Follow-ups:** (a) file an upstream Ladybug reproduction for the bulk-in-place corruption; (b) consider a content-addressed id scheme for topology/relationship edges so rebuild becomes deterministic.

---

## Evidence

- id-bound vs node-anchored validation; markdown trust-root grounding (1023/1023 ids correct); deterministic clean-backup → migration repro (0 → 1227); write-form isolation; incremental-ingest safety (+424, 0 → 0); full-scale heal proof (1227 → 0). Probes retained under `.scratchpad/verify_*.py` during the investigation.
