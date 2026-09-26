# ADR 0015 — Ingest throughput: concurrent curation, deterministic pre-filter, and ingest observability

- **Status:** Accepted; core implementation shipped with risky optimizations default-gated
- **Drivers:** Live profiling of a 6-file / 10.2MB WhatsApp corpus on an end-user machine
  (marginalia v0.0.9, report: `.scratchpad/mari-ingest-profile/report.html`, corpus:
  `.scratchpad/corpora/whatsapp-mariana/`).
- **Relates to:** ADR 0009 (curation control plane), ADR 0013 (durable candidate ledger).
- **Out of scope here:** P0 (macOS power assertion during active ingest) and P1
  (prompt prefix-cache reuse / Phase 2a excerpt-reorder) — both were already on
  separate tracks when this decision was written. This ADR covers P2 (concurrency/batching), P3 (deterministic
  pre-filter), and P4 (observability).

**Lifecycle addendum — 2026-07-13.** Bounded fan-out, per-call telemetry, daemon logging,
prefilter records, batch curation with fallback, streamed ledger progress, and safe resume
shipped. Batch size remains `1` by default and optional prefilter rules remain gated where
the required agreement evidence does not justify a default flip. Those are future tuning
decisions, not unfinished release work.

## Context — what the profiling showed

One 1.9MB chat file produced **10,328 candidates** and ≈ **6,300 sequential LLM calls**
(2,418 node-curator + 3,690 relation-curator, median 2.2 s each when the machine was
awake) ≈ 4 h of pure compute per file, ~20–24 h for the corpus. Three structural
properties of the current pipeline drive this:

1. **Strictly sequential curation.** Both curator loops
   (`companion/__init__.py:1155–1193` nodes, `:1468–1639` relations) make one blocking
   `LLMProvider.complete()` call per candidate. The provider protocol
   (`llm/__init__.py:66–81`) is sync-only. The LLM server meanwhile sits idle between
   round-trips.
2. **No cheap rejection of trivial candidates.** Chat content explodes into noise
   (1,297 Agent candidates, edge predicates dominated by `discusses`/`mentions`/
   `states`/`owns`), and ~50% of curator calls end in `queue`/reject — the LLM is used
   as an expensive filter for candidates a deterministic rule could have dropped.
3. **No ingest observability by default.** Daemon stdout/stderr →
   `/dev/null` (`server/lifecycle.py:301–302`); the only durable record is
   `candidate-ledger.jsonl`, which inlines full embedding vectors per candidate
   (48 MB for one file) and is re-parsed wholesale by the summary endpoints.

## Decision

### D1 (P2) — Bounded-concurrency curation fan-out

Run curator LLM calls concurrently with a bounded worker pool, preserving all
commit-ordering semantics.

- **Mechanism:** wrap the existing sync `LLMProvider.complete()` in a
  `ThreadPoolExecutor` (or `asyncio.to_thread` + semaphore) at the loop level in
  `Companion.remember()`. The provider protocol stays sync — no async rewrite.
- **Two fan-out sites, same primitive:**
  - node-curator loop (`companion/__init__.py:1155`): fan out `curator.curate()`
    over the candidate list, collect verdicts, then apply gate + ledger writes
    **in original candidate order** (submission order ≠ completion order; results are
    buffered and drained FIFO so ledger causality from ADR 0013 is preserved).
  - relation-curator loop (`:1468`): identical pattern. The endpoint gate
    (`:1473–1480`) is deterministic and already runs before the LLM call; it stays in
    the pre-fan-out pass so doomed edges never enter the pool.
- **Phase boundary unchanged:** all node verdicts land before relation curation begins
  (relation endpoint liveness depends on node gate outcomes — `:1350–1355`).
  We do **not** interleave node and relation curation.
- **Per-call hygiene is split at its owning boundaries:** the pool can apply an
  optional scheduler wait deadline and a single retry on transport error. The
  provider connection independently owns any LiteLLM request deadline. A failed
  call abstains exactly as today (`curator.py:421–422`).
- **Config** (in `ConsolidationConfig`, `config/_vault.py:492`):
  - `curation_max_concurrent: int = 1` — default 1 = today's behavior; explicit opt-in.
  - `curation_call_timeout_s: float | None = None` — no arbitrary scheduler
    deadline by default. A positive explicit value is still available as an
    operational fail-closed policy.
- **Server-side prerequisite (deployment note, not code):** concurrency only pays if
  the llama.cpp box runs `--parallel N` > 1. With `-np 4` and prefix-cache fixed (P1),
  expected curation speedup is ~3–4× on top of P1's ~3×.
- **Relationship to batching:** D1 pays off only when the provider has request
  slots (`-np > 1`, cloud providers, claude_cli process fan-out). On a single-slot
  server it is a no-op — that case is covered by D4 below. The two compose: batches
  can themselves be fanned out across slots.

### D4 (P2b) — Block-keyed batched curation with individual-call fallback

Evaluate K candidates per LLM request instead of one, made reliable by
construction rather than by hope:

- **Batch key = block.** A batch is one source excerpt + K candidates *from that
  same block*. Candidates never cross blocks: each is judged against the identical
  context it would have seen in a single call, so there is no cross-document
  leakage, and the dominant prompt cost (system prompt + ~9KB excerpt) is paid once
  per batch instead of once per candidate. On the profiled corpus this collapses
  3,690 relation-curator calls to ~370 at K=10 — a throughput win that, unlike D1,
  requires **no server-side changes** and works on a single slot.
- **Schema-constrained output.** Where the provider supports it (llama.cpp
  `response_format: json_schema` / GBNF grammar), constrain decoding to a JSON
  array of verdict objects keyed by `candidate_id` — unparseable output becomes
  impossible at the decoder level. Providers without grammar support still get the
  schema in-prompt plus strict parsing.
- **Deterministic validation gate + per-candidate fallback.** After each batch:
  every submitted `candidate_id` present exactly once, verdict in the allowed enum,
  no extras. Any candidate that fails validation (missing, duplicated, malformed)
  is re-run individually through the existing single-call path. The single-call
  path remains the semantic ground truth; batching is purely an optimization layer
  that degrades to today's behavior, never below it. Ledger records per candidate
  are unchanged in shape, with `method: "relation_curator"` plus a `batch_id` and
  `batch_size` so batch-vs-single provenance is auditable.
- **Quality gate for the residual risk.** Schema validation cannot catch silent
  verdict drift (position bias, anchoring across the K items). Before default-on:
  verdict-agreement A/B on a gold sample (batch vs single-call on identical
  candidates), requiring ≥97% agreement; K capped (`curation_batch_size`, default
  1 = off, recommended 8–12) and `max_tokens` sized to K. Position bias is probed
  by shuffling candidate order between A/B runs.
- **Config** (in `ConsolidationConfig`): `curation_batch_size: int = 1`.
- **Interaction with P1/D1:** batching reduces how often the shared prefix is
  re-sent at all, so it partially subsumes the prefix-cache win for curation; with
  multi-slot servers, D1 fans batches out in parallel. Step 6's benchmark matrix
  measures the combinations rather than assuming additivity.

### D2 (P3) — Deterministic pre-filter for low-value candidates

Insert a deterministic, no-LLM gate between embedding and within-batch collapse
(`companion/__init__.py:947–975`, before `collapse_duplicates`). It never deletes
information silently: every drop is a ledger `comparison` record with
`method: "prefilter"`, `verdict: "queue"` (queued, recoverable via the existing
review surface from ADR 0009 — *not* discarded).

Rules, all config-gated and conservative by default:

1. **Edge-predicate demotion list.** Raw predicates that encode conversation
   mechanics rather than knowledge — `discusses`, `mentions`, `mentioned`, `states`,
   `stated` — are queued without a relation-curator call. The canonical-predicate
   alias table already exists (`curator.py:238–290`); the list is configured, not
   hardcoded. Measured impact on the WhatsApp corpus: ~790 of 3,690 relation-curator
   calls (~21%) skipped.
2. **Low-signal node demotion.** Node candidates whose title appears exactly once in
   the document **and** whose extracted content is a single sentence ≤ N chars are
   queued (frequency threshold `min_mentions: 1` = off by default). The rule is
   content-agnostic — it keys off measurable recurrence within the document, never
   off a content-type label (see open question 2). Counting uses the per-block
   candidate sets already deduped at `companion/__init__.py:851/864`.
3. **Near-dup literal collapse.** Claim candidates from the same block whose
   normalized literals are near-identical (whitespace/case/punctuation-folded) keep
   one representative; the rest are marked `superseded` (the dedup machinery in
   `consolidate/_dedup.py` already does this for titles; this extends it to claim
   literals).
4. **Established-entity fast path.** The node curator exists to verify that
   extractor-written content is grounded in the source excerpt — it is a
   hallucination firewall, not an existence check. Once an entity is already
   curated and live in the store, re-verifying every subsequent re-mention adds
   nothing: in a 10-year chat corpus the same handful of Agents recur in nearly
   every block, each costing a full curator round-trip. Rule: when the resolver
   produces a **Tier-0 exact store match** (`exact_store`, `resolve/__init__.py:382`)
   against a node that was itself curator-committed (not merely queued), and the
   candidate carries no novel content beyond a re-mention (its content sentence is a
   near-dup of facets/claims already on the store node), record
   `method: "prefilter"`, `verdict: "fastpath_commit"` and skip the curator call.
   The merge/facet-union path runs exactly as it does today for resolver collapses.
   First sightings, fuzzy matches (`judge_store`), and candidates carrying novel
   content always go through the curator. On the WhatsApp corpus this targets the
   dominant cost: recurring participants re-confirmed hundreds of times.

- **Config** (new block in `ConsolidationConfig`):

  ```yaml
  consolidation:
    prefilter:
      enabled: false          # opt-in until evaluated
      demote_predicates: [discusses, mentions, mentioned, states, stated]
      min_mentions: 1         # 1 = off
      max_trivial_node_chars: 160
      established_entity_fastpath: true   # rule 4; safe-by-construction, on by default
  ```

  Rules 1–3 default off pending the eval gate; rule 4 defaults **on** because it only
  fires where the candidate would merge into an already-curated node with no novel
  content — the curator verdict is a foregone conclusion, so skipping it cannot
  change graph content, only cost. The eval comparison should still confirm this.

- **Quality gate before default-on:** run the golden eval
  (`marginalia-knowledge-quality-eval`) and the acceptance harness with
  prefilter on vs off; flip the default only if recall on the gold set is unchanged.
  The WhatsApp corpus in `.scratchpad/corpora/whatsapp-mariana/` becomes the
  chat-stress fixture for this comparison.

### D3 (P4) — Ingest observability

Three changes; only the ledger one is an architecture decision (amends ADR 0013):

1. **Ledger: stop inlining embedding vectors.** Candidate rows store
   `embedding_dim` + content hash only; vectors live exclusively in the store. The
   read path already strips them (`ledger.py:51–65 _without_heavy_values`) — nothing
   downstream consumes inlined vectors. Bump `ledger_version` to 2; readers accept
   both. Expected: 48 MB/file → ~3–5 MB, and `run_summaries()` (which loads the whole
   JSONL into memory, `ledger.py:259–272`) stops being a memory hazard on long runs.
2. **Daemon log file by default.** `daemonize()` (`server/lifecycle.py:265–305`)
   gains `--log-file` with default `~/.marginalia/logs/marginalia-serve.log` via
   `RotatingFileHandler` (10 MB × 3), wired through `configure_logging()`
   (`:226–257`). `/dev/null` remains available as explicit opt-out. No ADR needed for
   this beyond recording it here.
3. **Per-call LLM timing in the ledger.** Each curator `comparison` record gains
   `duration_s` and, when the provider returns usage, `prompt_tokens` /
   `completion_tokens` / `cached_tokens`. This is what made this profiling session
   possible at all — it should not require archaeology next time, and it is the
   direct measurement for P1/P2 regression tracking.

### D5 (P5) — Streamed verdict ledgering + ledger-native mid-file resume

Added 2026-06-11 after observing the first post-D1 live run. Two coupled fixes,
neither of which gives back any of the D1/D4 performance win:

**Problem.** The D1 restructure (pre-pass → fan-out → post-pass) moved ALL
ledger comparison writes to after the fan-out completes. Two consequences
observed live: (a) progress/telemetry/inspect-UI sit at 0/N for the entire
curator phase (~45 min for 1,247 node candidates) and then flood at once;
(b) the durability window got worse — a crash mid-phase loses every verdict
held in memory, where pre-D1 each verdict was at least recorded as it
happened. Independently, mid-file resume has never existed: `remember()`
never reads the ledger back, so any crash (process, provider, power) restarts
the file from extraction. ADR 0013 made the ledger durable and replayable
"for future tooling"; this is that tooling.

**D5a — streamed ledgering (restores observability + durability).** Drain
fan-out verdicts as an *ordered prefix*: the post-pass consumes
`futures[i].result()` in submission order and performs gate decision + ledger
write for item *i* as soon as items `0..i` are complete, instead of waiting
for all N. Ledger append order is unchanged (still original candidate order —
the ADR 0013 guarantee holds verbatim); throughput is unchanged (workers never
wait on the ledger; only the consuming main thread does, and JSONL appends are
micro-seconds against multi-second LLM calls). Sequential mode (`max_concurrent: 1`)
naturally degenerates to the pre-D1 behavior: write after each call. Progress
events and `llm_timing` tick live again.

**D5b — ledger-native resume (requires D5a).** On entering curation for a
document whose ledger already holds a `started` run with the same
`document_id`: build the set of candidate_ids that already have a curator /
relation_curator comparison in that run, and skip their LLM calls, replaying
the recorded verdict into the gate instead. Candidate ids are content hashes,
so re-extraction reproduces matching ids deterministically (same chunker, same
extractor output cached? — no: extraction re-runs, but candidates that hash
identically match; candidates that don't are simply re-judged). Guard rails:
- resume applies only when extraction parameters match (same `blocks_total`,
  same model string recorded on the run) — otherwise start a fresh run;
- replayed verdicts are recorded as `method: "resume_replay"` comparisons
  referencing the original record, so the audit trail shows what was skipped;
- the known v0.0.9 failure mode (duplicate candidate rows appended after a
  daemon restart, observed ~1,000 dup rows on the mari run) is closed by
  recording a candidate row only when the id is not already present in the run.

**Performance interaction: none, by construction.** D5a touches only the
consuming side of the fan-out (workers are oblivious); D5b only *removes* LLM
calls on the crash-recovery path and costs one ledger scan at file start
(seconds, and ledger v2 made the file ~10× smaller). Composes with D4: batch
members that already have verdicts are dropped from their batch before the
call; a batch whose members are all replayed never runs. Expected user-visible
effect: "crash = redo ~3h" becomes "crash = redo the in-flight call/batch".



- Default behavior is unchanged until knobs are flipped (`curation_max_concurrent: 1`,
  `prefilter.enabled: false`) — old vaults and configs keep working; graphs need no
  migration. Ledger v2 is the only format change and is read-compatible.
- Ledger ordering guarantee is restated, not weakened: records append in candidate
  iteration order even under concurrency (buffered FIFO drain).
- Pre-filter introduces a non-LLM gate that demotes candidates; the mitigation is
  that demotion = `queue` (reviewable), never silent discard, and default-off until
  the eval gate passes.
- Combined expectation on the profiled corpus (with P0+P1 from the other tracks):
  ~7–8 days wall-clock → **~3–5 hours** (P0 ≈ 8× duty cycle, P1 ≈ 3× per call,
  D1 ≈ 3–4× parallel, D2 ≈ 1.3–2× fewer calls).

### Run-derived reliability fixes (shipped alongside D5, 2026-06-11)

Two defects surfaced by the first instrumented live run, fixed outside the
numbered decisions because they are bug fixes, not design changes:

- **Config PATCH now invalidates the cached embedder** (`Vault.invalidate_runtime_caches()`,
  called from the PATCH handler under the writer lock). Previously the vault's
  lazily-cached embedder survived config writes, so changing `embedding.model`
  silently kept the old model until a daemon restart — the cause of a fully
  errored 6-file run. In-flight calls finish on the old instance; the
  embedding-dimension guard (`embedding_dim_mismatch`) is not bypassed.
- **Ingest-queue hygiene:** `POST /api/v1/ingest-queue/{id}/retry` (errored →
  queued, error cleared) and `DELETE /api/v1/ingest-queue/{id}` (remove
  terminal items). Both loopback-only like config PATCH. Previously errored
  rows were permanent UI residue with no recovery path.

## Implementation plan

Ordered so each step ships and validates independently. D3.2 first because every
later step benefits from logs existing.

| # | Step | Touches | Validation |
|---|------|---------|------------|
| 1 | ✅ shipped — D3.2 daemon log file (`--log-file`, rotating default) | `server/lifecycle.py`, `cli/__init__.py:628` | `marginalia serve --daemon` writes log; `/dev/null` opt-out works |
| 2 | ✅ shipped — D3.3 per-call timing + token usage into ledger comparisons | `curator.py`, `consolidate/ledger.py` | ledger rows carry `duration_s`; summary endpoint surfaces p50/p90 |
| 3 | ✅ shipped — D3.1 ledger v2 (strip embeddings) | `consolidate/ledger.py` | v1 files still readable; new file ≤ 10% of v1 size on the WhatsApp fixture |
| 4 | ✅ shipped — D1 worker-pool fan-out for node + relation curator loops, config knobs (`curation_max_concurrent`, `curation_call_timeout_s`; prompts prebuilt serially via `build_prompt`, verdicts drained FIFO) | `companion/__init__.py`, `config/_vault.py`, `curator.py` | identical verdict set vs sequential pinned in tests; ledger order preserved; `-np 4` box test pending step 7 |
| 5 | ✅ shipped — D2 prefilter rules + ledger `prefilter` comparison method (rules 1–3 behind `prefilter.enabled`, rule 4 fast path on by default). Note: Tier-0 exact-store matches are merged away pre-curator, so rule 4's ledger record lands in the superseded-node audit path; "previously curator-committed" is not yet derivable from store rows (open point in `prefilter.py`) | `companion/__init__.py`, new `consolidate/prefilter.py`, `config/_vault.py` | golden eval + acceptance unchanged with prefilter on (pending step 7); per-rule + integration tests in `tests/consolidate/test_prefilter.py` |
| 6 | ✅ shipped (code) — D4 batched curation: block-keyed batch builder, schema-constrained request, strict validation gate + single-call fallback, `batch_id`/`batch_size` in ledger payloads (`curation_batch_size`, default 1 = off). Usage attribution: batch tokens on first member only so summaries count each batch once; excerpt prefix byte-identical to single-call prompts (cache-friendly, pinned by test) | new `curator_batch.py`, `companion/__init__.py`, `config/_vault.py` | fallback + order alignment + inert-at-1 pinned in tests; **verdict-agreement A/B ≥97% on gold sample still required before default-on (step 7)** |
| 7 | A/B benchmark matrix on `.scratchpad/corpora/whatsapp-mariana/` (baseline vs P1 vs +D1 vs +D4 vs +D1+D4 vs full+D2) | bench script in `.scratchpad/` | scorecard: wall-clock, calls, tokens, verdict deltas |
| 8 | ✅ shipped — D5a streamed verdict ledgering (`_iter_fan_out_verdicts` ordered-prefix generator; node `curator_progress` event every 25). Limit RESOLVED (step 10): the D4 batch path now streams per batch via `iter_batched_curation` | `companion/__init__.py` | streaming pinned by tests (prefix written while slow call in flight; sequential = write-per-call); throughput unchanged |
| 9 | ✅ shipped — D5b ledger-native mid-file resume (`find_resumable_run` guard-railed on document_id+blocks_total+model; one-scan `ResumeSnapshot`; `resume_replay` comparisons with `replayed_from_ts`; candidate-row dedup closes the v0.0.9 duplicate-rows bug; replayed candidates never enter D4 batches) | `companion/__init__.py`, `consolidate/ledger.py` | e2e: KeyboardInterrupt mid-curation → restart reuses run_id, zero provider calls for judged candidates, run completes; blocks_total mismatch ⇒ fresh run |
| 10 | ✅ shipped — D5a×D4 per-batch verdict streaming: `iter_batched_curation` generator yields ordered `(verdict, batch_meta)` pairs as each batch returns; per-member fallback runs immediately after its batch; node/relation post-passes consume one streaming iterator for both modes, so ledger rows land per batch (live `/api/v1/ledger/summary` progress) and D5b resumes from the last completed batch. Motivated by run B (2026-06-11): UI showed 0/N for the entire 98-min curation window | `curator_batch.py`, `companion/__init__.py` | crash-mid-batched-phase resume test (first batch durable, only remaining batch re-judged); ledger row content identical to phase-end flush |
| 11 | ✅ shipped — endpoint pre-gate: relation candidates whose endpoints were queued/rejected in node curation are dead-lettered (`endpoint_gate`/`skipped_endpoint`, identical record shape) BEFORE prompt build or any LLM call; post-curation gate kept as safety net. Motivated by run B: 771/3,229 relations (24%) were fully curated then dead-lettered ≈ 18 min + ~700K tokens wasted | `companion/__init__.py` | pregate test: dead-endpoint relation reaches zero LLM calls, appears as `skipped_endpoint`; total ledger semantics unchanged |
| 12 | ✅ shipped — UI exposure: Config tab "Consolidation / Performance" card edits `curation_max_concurrent`, `curation_batch_size`, `curation_call_timeout_s`, `prefilter.enabled`, `prefilter.established_entity_fastpath` (GET `/api/v1/config` now returns them; nested PATCH already worked); new `CurationProgress` panel polls ledger summary every 5 s during processing (phase, per-curator progress bars, last-activity recency) | `server/http.py`, `frontend/src/components/config/ConfigPanel.tsx`, new `frontend/src/components/ingest/CurationProgress.tsx`, `frontend/src/components/ingest/BulkImport.tsx` | GET/PATCH/bounds tests in `tests/test_server_api_v1.py`; `./build.sh` clean |
| 13 | ✅ shipped — timeout ownership correction after the LOTR run: curation wait is nullable/unbounded by default, while the named provider connection owns the optional LiteLLM request deadline. Stop/shutdown still terminate the owned request process, so removing the hidden deadline does not remove cancellation | `providers.py`, `llm/__init__.py`, `llm/_litellm_process.py`, Config Providers/Curation UI | Return-of-the-King 300s helper failure pinned by provider/helper timeout regressions; provider create/update round-trip covered |

Dependencies: step 4 wants the `-np` change on the inference server and is most valuable after
P1 lands (cache reuse × concurrency interact: verify llama.cpp prefix cache behavior
with multiple slots — per-slot caches may reduce P1's win; measure in step 6).

## Open questions (for the next session)

1. `-np 4` on the box halves per-slot KV budget at 163k ctx — acceptable, or cap
   concurrency at 2?
2. Where should `min_mentions` live so it stays content-agnostic? **Constraint
   (explicit project direction):** the system is being tuned generic — no
   content-type profiles ("chat mode", "fiction mode"); nx, lotr, and the WhatsApp
   corpus must all flow through the same pipeline with the same defaults. If a
   threshold needs to adapt, it must key off **measurable corpus statistics** (e.g.
   candidate-per-block density, title-recurrence distribution) that any corpus
   exhibits — never off a guessed or user-declared content type.
3. Once D4 saturates the server, is the next lever extraction-side (reducing
   candidate over-emission per block)? Same constraint as Q2 applies: any such
   change must be a generic mechanism (e.g. extractor emission caps already scale
   with block size) and must hold or improve the nx/lotr eval numbers, not a
   conversational-text special case.
4. D4 ordering within a batch: should candidates be sorted (e.g. by kind/predicate)
   for prompt stability, or shuffled to wash out position bias? The A/B in step 6
   should answer this empirically.

## Addendum (2026-09-22): D4 agreement gate measured, relation batches capped

The D4 verdict-agreement A/B was run on conv-49 through the `chatgpt` provider
(`gpt-5.6-luna`, reasoning off). No batch size met the ≥97% gate for either
curator: relation batches agree with single calls 60–65% (single-vs-single
floor 86%), node batches 85–88% (floor 96%), and a batch of one already loses
most of that, so the batch format rather than K is the cause. Relation batches
above 4 flip whole documents between commit-all and queue-all, so relation
curation is now capped at `RELATION_BATCH_MAX = 4` whatever
`curation_batch_size` says, with the clamp reported by `capacity_report()` and
a `relation_batch_capped` ingest event. The "recommended 8–12" above is
superseded. Tables, variants tried and the recommendation are in
`docs/remote-providers.md` section 3.
