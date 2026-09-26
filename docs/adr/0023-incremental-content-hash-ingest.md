# ADR 0023: Incremental ingest — skip LLM extraction for unchanged blocks via content-hash

- **Status:** Accepted (2026-06-30; default-on 2026-07-02, see addendum; deterministic-claim stale-retirement gap closed 2026-09-03, see amendment)
- **Date:** 2026-06-30
- **Deciders:** Alex Rivera, Marginalia core
- **Builds on:** ADR 0002 (chunking & provenance — `content_hash` per block), ADR 0003 (provenance is a span), ADR 0007 (rebuild from the markdown trust root), ADR 0013 (durable candidate ledger — interrupted-run resume), ADR 0016 (claim semantic identity & corroboration)
- **Out of scope:** changing the 5-primitive closed schema; the chunker's fixed 12k-window policy (unchanged); making `remember()` asynchronous / non-blocking (tracked separately — see "Related: event-loop blocking"); full bulk rebuild (`kg rebuild`), which intentionally re-extracts everything from the trust root.

---

## Context

Marginalia positions itself as a **living memory** — a graph that "receives new
things" as tracked files evolve — not a static, build-once knowledge base. The
canonical write path is `Companion.remember()`
(`src/marginalia/companion/__init__.py:958`), reachable from the CLI and the MCP
`remember` tool.

Today, re-ingesting a file that already exists in the graph re-pays the **full
LLM extraction cost over every block**, even when only one line changed. The
trace:

1. `remember()` calls `self._vault.add(source)` (`companion/__init__.py:1028`) →
   `ingest_document` (`ingest/__init__.py:21`), which re-parses the **whole
   file** and unconditionally re-`add_node`s the Document and **every** Block
   (`ingest/__init__.py:46-58`). No diff, no hash comparison, no removal of stale
   blocks.
2. Block IDs are `sha256_hex(block.path, block.content_hash, i)`
   (`ingest/markdown.py:250`), and each Block already **stores** its
   `content_hash` (`ingest/markdown.py:99`). An unchanged block re-adds with the
   same ID (idempotent overwrite); a changed block gets a new ID. Nothing is
   skipped.
3. `units = _extraction_units(store, source)` (`companion/__init__.py:1068`,
   defn `:3624`) enumerates **all** current Block nodes for the `source_path` as
   `(anchor, text)` units. No hash filtering.
4. The extraction loop (`companion/__init__.py:1154-1163`) runs
   `extractor.extract(text, …)` — **one LLM call per block** — for every
   non-empty unit. The *only* short-circuit is empty/whitespace text
   (`:1159-1161`).
5. Dedup is **claim-level and post-extraction** (`_emit("dedup", …)`,
   `companion/__init__.py:1334`; ADR 0016 `semantic_claim_id` /
   `claim_object_identity`, imported `:28`). The graph stays clean via
   corroboration — but every block's LLM call has **already run**.

So the cost model today is **option (c)**: full re-chunk + full LLM
re-extraction, with duplication removed only *after* the spend. For a one-line
edit to an N-block file, that is N extraction calls to mint claims we already
have, plus the dedup pass to merge them back.

There is **no** existing incremental path. `find_resumable_run`
(`companion/__init__.py:1091`, ADR 0013) only resumes a **crashed** run by
replaying curator verdicts — it still re-runs `extractor.extract()` on all
blocks. `detect-drift` (`cli/__init__.py:1180-1218`) is a **read-only**
diagnostic that reports `+added -removed ~changed` counts; it does not re-ingest
changed regions, and `remember()` does not consult it.

Two costs follow, both load-bearing for the product thesis:

- **Compute / latency.** A trivial edit pays full-file extraction. On a local
  model this is seconds-to-minutes per file.
- **Event-loop blocking (symptom, not cause).** Because `remember()` runs
  synchronously and is LLM-bound, a multi-block re-ingest saturates the daemon:
  `/health` stops responding and the MCP transport's keepalive lapses, so
  clients mark the server disconnected (the observed "red dot"). Cutting the LLM
  work to changed blocks only sharply reduces the blocking window. (A proper
  async/offload fix is separate and out of scope here.)

## Decision

Add a **content-hash short-circuit** to the extraction loop: a block whose
`content_hash` already has **minted, current Claims attributed to it** in the
graph is skipped — its existing Claims are retained as-is, and
`extractor.extract()` is **not** called for it. Only blocks that are **new** or
whose `content_hash` **changed** are extracted.

The architecture already carries every primitive this needs — `content_hash` and
`source_path` are stored on every Block, and `_extraction_units` already
enumerates per block — so this is an additive filter, not a redesign.

### Mechanism (intended shape, not final code)

1. **Anchor extraction by `content_hash`.** When a block's Claims are minted,
   ensure they are traceable to the block's `content_hash` (already true via the
   Block anchor / provenance span; confirm the lookup is keyed by hash, not just
   block-id-by-index, so re-indexing a file doesn't orphan the link).
2. **Pre-extraction diff.** In `_extraction_units` (or a wrapper around it),
   partition blocks into:
   - **unchanged** — `content_hash` already present in the graph **with**
     non-empty current Claims → **skip extraction**, keep existing Claims;
   - **changed / new** — hash absent, or present but with no live Claims →
     **extract**.
3. **Stale-claim retirement.** When a block's `content_hash` disappears from the
   file (the line was edited away), its previously-minted Claims must be
   retired/superseded so the graph reflects the trust root (consistent with
   ADR 0007's "markdown is canonical"). This is the subtle half: incremental add
   is easy; incremental *removal* must not leave orphaned Claims.
4. **Dedup still runs** over the (smaller) set of newly-extracted claims —
   ADR 0016 corroboration is unchanged; it simply has far less to merge.

### Correctness invariant

> For any file state F, `remember(F)` must converge to the **same graph** whether
> reached by a full extraction or by an incremental diff from a prior state F'.

This is the acceptance bar: incremental ingest is a pure optimization and must be
**graph-equivalent** to full re-extraction. It is validated by re-ingesting the
same file twice (idempotent: second run does ~0 LLM work, graph unchanged) and by
diff-then-full equivalence on a changed file.

## Options considered

- **A — Content-hash skip (chosen).** Skip extraction for unchanged blocks;
  extract changed/new; retire claims for removed blocks. Graph-equivalent.
  Biggest win for the common case (small edits to existing files), minimal
  surface area. Risk concentrated in stale-claim retirement.
- **B — Whole-file hash skip.** If the file's overall hash is unchanged, skip the
  whole ingest. Trivial to build, but only helps the *no-op* case (re-ingesting
  an untouched file). Does nothing for the actual use case — a small change. Can
  ship as a cheap pre-check **alongside** A, not instead of it.
- **C — Wire `detect-drift` into `remember()`.** Reuse the existing
  added/removed/changed diff to drive extraction. Attractive (no new diff code)
  but `detect-drift` is block-region diagnostic, not claim-aware; it would still
  need the claim-retention/retirement logic from A. Fold its diff in as the
  implementation of A's step 2 rather than a separate option.
- **D — Status quo + post-hoc dedup.** Keep paying full-file extraction; rely on
  ADR 0016 to merge. Rejected: it spends compute to recompute known claims and is
  the direct cause of the daemon-blocking symptom; it scales the wrong way as
  vaults grow and re-ingest frequency rises.

## Consequences

**Positive**

- A one-line append to an existing file becomes ~one block of LLM work instead of
  the whole file. Re-ingesting an unchanged file becomes ~zero LLM work
  (idempotent), which also makes `remember()` safe to call liberally — the
  behaviour the new "remember is the main memory" rule assumes.
- Shrinks the synchronous blocking window, indirectly easing the MCP "red dot"
  saturation without yet touching the async question.
- Reinforces ADR 0007: the trust root stays canonical; the graph is re-derived,
  now *incrementally*.

**Negative / risks**

- **Stale-claim retirement is the hard part.** Skipping extraction is safe;
  failing to retire claims for deleted content silently keeps the graph ahead of
  the trust root. Needs explicit tests for the "line removed" and "block edited
  in place" cases.
- **Hash granularity vs. chunk granularity.** With the 12k-window chunker, a
  small file is one block, so the skip is all-or-nothing at the file level until
  a file spans multiple blocks. The win grows with file size / block count.
- **Trust-boundary check.** `content_hash` is computed from file bytes; ensure a
  collision or a hash-only check can't be used to *suppress* extraction of
  genuinely new content (defensive: treat "hash present but no live claims" as
  changed, never skip).

**Neutral**

- `kg rebuild` is unaffected — it deliberately re-extracts everything from the
  trust root and remains the integrity backstop.

## Implementation plan (phased)

1. **Phase 0 — Equivalence harness (no behaviour change).** Add a test that
   ingests a file, mutates one line, re-ingests, and asserts the resulting graph
   equals a from-scratch ingest of the final file. This is the acceptance oracle
   for everything below.
2. **Phase 1 — Whole-file no-op skip (Option B, cheap).** If the file's content
   is byte-identical to what's anchored in the graph, short-circuit before the
   extraction loop. Lands the idempotent-re-ingest win immediately, low risk.
3. **Phase 2 — Per-block skip for unchanged blocks (Option A core).** Partition
   in/around `_extraction_units`; skip `extractor.extract()` for blocks whose
   `content_hash` already has live Claims. Gate behind a flag
   (`MARGINALIA_INCREMENTAL_INGEST`, default off) until equivalence is proven.
4. **Phase 3 — Stale-claim retirement.** Retire/supersede Claims for blocks whose
   `content_hash` no longer appears in the file. Make the equivalence harness
   cover removals. Flip the flag default on once green.
5. **Phase 4 — Observability.** Emit per-ingest counts (`blocks_total`,
   `blocks_skipped`, `blocks_extracted`, `claims_retired`) so the win is
   measurable and regressions visible.

## Validation (Definition of Done)

- Equivalence harness green for: unchanged re-ingest (0 extractions), one-line
  add (1 block extracted), in-place edit (only edited block re-extracted), line
  removed (claims retired, graph matches full re-ingest).
- End-to-end on a real vault (demo-vault): re-ingest a tracking file after a
  one-line edit, confirm `blocks_extracted == 1` (or the count of changed
  blocks) and that an `ask` over the changed fact reflects the new content with
  no duplicate/stale claims.
- No regression in `kg rebuild` full-extraction behaviour.

## Related: event-loop blocking (not solved here)

The observed MCP "red dot" during ingest is `remember()` blocking the daemon's
async event loop while LLM-bound. This ADR **reduces** the blocking window by
cutting LLM work, but does not make `remember()` non-blocking. A separate change
(offloading extraction to a worker / making the MCP handler await without
holding the loop) should follow; tracked independently.

## Addendum — 2026-07-02 remediation (F10): default ON via config

Incremental ingest (this ADR) and the sub-chunk diff (ADR 0024) are now
config-driven and DEFAULT ON: `ingest.incremental` / `ingest.subchunk` in
`marginalia.yaml` (new `IngestConfig`, writable via `PATCH /api/v1/config`).
The `MARGINALIA_INCREMENTAL_INGEST` / `MARGINALIA_SUBCHUNK_INGEST` env vars
became two-way overrides: `1` forces on, `0` forces off, unset defers to the
config. Library callers without a vault config inherit the ON default.

## Amended 2026-09-03 — deterministic-claim half of "no removal of stale blocks" closed

The Context section above still accurately describes `ingest_document`'s
original behaviour: "No diff, no hash comparison, no removal of stale
blocks." A 2026-09-03 deep review (finding 3.5) reconfirmed this live — an
edited document's `has_tag`/`has_heading`/`links_to` Claims from a prior
ingest were never retired, so they doubled on every edit and stayed directly
searchable via BM25 with no staleness filter. `companion/_incremental.py`'s
`detach_orphan_removals` (the retirement path this ADR's Phase 3 shipped)
never covered them either — it explicitly skips any Claim without a
`model_id` facet, i.e. exactly the deterministic ones.

A follow-up commit closed that half of the gap: `ingest/__init__.py`'s
`_retire_stale_deterministic_claims()` now runs unconditionally at the end of
every `ingest_document()` call (every `vault.add()`, independent of the
`ingest.incremental`/`ingest.subchunk` flags above). It walks the
document's `rdf:subject` edges, and for every `has_tag`/`has_heading`/
`links_to` Claim (`provenance.rule_id == "deterministic-v1"`) whose id is
absent from the freshly-parsed set, it stamps `facets["_superseded"] = True`
and `facets["valid_until"] = today` — the same lifecycle facets `query.py`'s
`is_superseded` recall gate already excludes elsewhere.

This is deliberately **supersede, not detach**, and that is a semantic
difference from the LLM-claim path this ADR/ADR 0024 built:
`_apply_detach` marks an LLM-minted Claim `_detached` because its source text
merely went missing (the fact might still be true, just unconfirmed, and
`resurrect_reverted_claims` un-detaches it if the text comes back).
A removed `#tag`, heading, or `[[wikilink]]` is different: once the document
no longer carries it, `has_tag: X` is now definitively **false** about that
document, not merely unconfirmed — so retirement supersedes it outright, with
no `supersedes` edge (this is a pure removal, not a correction with a
replacement claim). Reverting to byte-identical content still resurrects the
claim for free, since its id is content-hash-derived and reappears in the
fresh set.

**Status of the original gap:** the deterministic-claim half (`has_tag`,
`has_heading`, `links_to`) is closed by that commit. LLM-minted claim
retirement for genuinely removed source lines remains the pre-existing
`detach_orphan_removals` / `_apply_detach` path (ADR 0024), which keeps a
different, deliberately weaker semantic (detached, not superseded) — that is
not a residual gap, it is the documented design for that claim class.
