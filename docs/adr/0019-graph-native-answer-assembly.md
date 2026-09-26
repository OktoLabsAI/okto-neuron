# ADR 0019: Graph-native answer assembly — beyond the block-dump ceiling

- **Status:** Superseded / empirically resolved by ADR 0028 and ADRs 0030–0031
- **Date:** 2026-06-21
- **Implementation plan added 2026-06-21 (see below).**
- **Deciders:** Alex Rivera, Marginalia core
- **Builds on:** ADR 0011 (subgraph-first answer assembly), ADR 0018 (differentiated retrieval defaults), ADR 0012 (user-configurable ask retrieval policy)
- **Relates to:** ADR 0016 (semantic claim identity), ADR 0002/0003 (Claim → Block byte anchoring)
- **Out of scope:** changing the 5-primitive closed schema; replacing the deterministic CI floor as the only gated signal; reranking/hybrid retrieval on the seed side (a separate lever); any change to the markdown-trust-root invariant.

**Lifecycle addendum — 2026-07-13.** The measurement program ran to completion.
Ranking, reach, granularity, and seed quotas did not break the plateau; ADR 0028 shipped
the bounded efficient-hybrid assembly, and ADRs 0030–0031 lifted the upstream extraction
and retention floor. The finale reached statistical parity at 6.24x fewer context tokens.
The phased plan below is retained as experimental provenance and has no active release
step.

---

## Context

ADR 0018 raised `ask`'s default seed window (k=8 → k=20), lifting grounded-golden
answer-presence by **+0.085** (controlled 5-sample study: terse k=8 → terse k=20,
McNemar p=0.0018, CI [+0.035, +0.140]) while neg-control abstention *improved* to
0.917. (An extractive answerer prompt was also tried but **reverted** — it landed
within the temp-0.7 noise floor.) The validated block-dump answer-presence is
≈ **0.72** (terse, k=20, controlled). That number is real and shipped, but it is a
**brute-force block-dump ceiling**, not the destination. An earlier single run read
0.792 with the extractive prompt; that figure was cross-daemon noise and is not the
ceiling to beat — use the controlled ≈ 0.72.

How the block-dump path works: `ask` dumps the raw text of ~20 retrieved Blocks
(~60K tokens) into the answerer and lets a 35B model read it all. It works
because the answer is almost always *somewhere* in those bytes. It is expensive
(context grows with `k`), and it does not scale — the ceiling-sweep evidence in
ADR 0018 shows k=40 (~120K tokens) overflows the answerer and collapses to 31
empty answers. k=20 is the block-mode ceiling precisely because there is no more
context budget to spend.

The architectural north star is the opposite shape: **answer from the graph
itself** — from the nodes, Claims, and connections the ingest pipeline already
mints — rendering a compact typed subgraph instead of a wall of raw text. ADR
0011 built that path (Tier-1 ego-graph render + Tier-2 raw-block fetch on
coverage miss). The subgraph render is roughly **100× cheaper context** than the
block dump: a rendered ego-graph is ~584 tokens versus the ~60K-token block dump
for the same question. Graph-native answering is the goal because it is what
makes `ask` cheap, inspectable, and citation-native at scale; block-dump is the
crutch we lean on while the graph is too thin to stand on.

The problem is quality. On the grounded golden set the subgraph path scores
**0.6 / 0.75** (answer-presence / neg-guard, single run) versus block-dump's
**≈ 0.72 / 0.917** (terse k=20, controlled). The ~0.12 answer-presence gap is far
above the temp-0.7 noise floor, so the conclusion holds even though the subgraph
figure is a single run. To close that gap we root-caused every subgraph miss against
the grounded key.

### Root-cause of the subgraph gap

The misses split three ways:

- **63% extraction-gap.** The answer fact was *never minted as a Claim*, so no
  graph walk can ever reach it. These are the dense factual surfaces that do not
  decompose into a clean subject-predicate-object triple: table cells, config
  `key = value` lines, version / image strings, status labels. They live only in
  the raw `Block` text. The block-dump path wins on exactly these because it
  reads the raw bytes; the subgraph path is structurally blind to them.
- **29% assembly-gap.** The Claim *exists* in the graph, but the ego-graph walk
  drops it before it reaches the answerer. Two concrete code defects:
  - **Claim seeds don't register their subject entity in NODES.** In
    `subgraph.py` (the seed loop ~lines 245-260), a Claim seed routes through
    `_bridge_claim_seed` to its subject/object, but the subject entity is never
    added as a visible NODES row (`if node.type != "Claim"` skips it, and
    `_subject_name` is best-effort display only). So a Claim that *was* retrieved
    can land in the render without its subject entity anchored as a walkable node,
    and the answerer never sees the connection that makes the fact usable.
  - **`degree_cap=8` truncates by id-tiebreak, not query relevance.**
    `_ranked_bridge_claims` (`subgraph.py:160-193`) ranks an entity's bridging
    Claims by `(-confidence, id)` and truncates to `degree_cap` (default 8 —
    `_ASK_DEGREE_CAP_DEFAULT`). When many Claims share the same confidence, the
    cut is decided by an arbitrary id ascending sort — *not* by how relevant the
    Claim is to the question. A high-degree entity can therefore drop the
    answer-bearing Claim purely on id order.
- **8% wording.** The Claim is retrieved and rendered, but its phrasing diverges
  enough from the gold answer that the judge scores it a miss.

A separate finding bounds how far assembly fixes alone can go. **`hops` is not
the limiter**: re-running the subgraph path at `hops=2` recovers none of the
misses — the answer-bearing Claims are already inside the 1-hop neighbourhood
when they exist at all; the failure is dropping them (assembly) or never minting
them (extraction), not walking too shallow.

## Decision

Pursue graph-native answering as the path beyond the ≈ 0.72 block-dump ceiling,
in two ordered tiers. Land the cheap assembly fixes first (no re-ingest), then
the expensive extraction work, and be honest about the bounded ceiling of each.

### Tier 1 — assembly fixes (hours, no re-ingest, → ~0.73)

Close the 29% assembly-gap without touching the ingest pipeline:

1. **Register the claim-seed subject entity and bridge it.** When a Claim is a
   seed, add its subject entity to `ego.nodes` as a walkable NODES row (not just a
   display name), so the rendered subgraph carries the connection the answer
   needs (`subgraph.py` seed loop ~249-256).
2. **Relevance-aware degree cap.** Replace the id-tiebreak in
   `_ranked_bridge_claims` (`subgraph.py:160-193`) with a query-relevance signal
   — cosine similarity of each bridging Claim against the question — so the
   `degree_cap` cut keeps the *most relevant* equal-confidence Claims rather than
   the lowest-id ones.
3. **Block-colocation expansion.** Pull in Claims that share a seed Claim's
   source `block_id`. Facts extracted from the same Block are topically adjacent
   by construction; co-locating them recovers answer fragments that the strict
   edge walk misses.

These three are bounded: even with **perfect** assembly the subgraph path tops
out at roughly **~0.73** on the grounded set — at best **on par with** block-dump's
controlled ≈ 0.72, not clearly past it — because the remaining 63% extraction-gap
facts simply are not in the graph for any walk to find. (This ~0.73 is itself a
single-run estimate from the same temp-0.7 regime; treat it as indicative, not a
validated figure.) Tier 1 is worth doing anyway: it makes the cheap graph-native
path competitive and is a prerequisite for Tier 2 paying off.

### Tier 2 — extraction (days, re-ingest)

Close the 63% extraction-gap by **minting Claims for the dense factual surfaces**
the current extractor skips — table cells, config `key = value` pairs, version /
image strings, status labels — turning them into proper S-P-O Claims. This is the
only path to matching and then exceeding block-dump quality on the cheap
graph-native context, because it puts the missing facts *into the graph*.

**Binding constraint:** every newly minted Claim must preserve **Claim → Block
byte anchoring** (ADR 0002/0003) — the dense-surface facts are minted *from* their
source Block with `(path, byte_start, byte_end, content_hash)` intact, never as
free-floating assertions. Extraction quality work does not get to bypass the
provenance model.

## Consequences

- **The honest ceiling story.** The block-dump config ceiling is ≈ **0.72**
  (controlled terse k=20, ADR 0018 — not the older single-run 0.792). Tier 1
  assembly fixes lift the cheap graph-native path to roughly ~0.73, at best on par
  with block-dump but not reliably past it on the same noisy single-run regime.
  Only Tier 2 extraction is expected to clearly exceed block-dump on ~100×-cheaper
  context — at which point graph-native becomes the default and block-dump reverts
  to the fallback ADR 0011 always intended (Tier-2 raw-block fetch on coverage miss).
- **Ordering is deliberate.** Assembly is hours and reversible (read-path code,
  no graph writes); extraction is days and requires a re-ingest. Landing assembly
  first de-risks the extraction work and gives a measurable intermediate.
- **No schema pressure.** Everything here stays inside the 5-primitive closed
  schema: more Claims, better-assembled, all still anchored to Blocks. Nothing
  asks for a sixth primitive.
- **Measurement.** Every number above is scored deterministically against the
  grounded golden key (the LLM judge panel was κ=0.14 and is not trusted for
  gating — same regime as ADR 0018). Tier-1 and Tier-2 land behind the same
  deterministic floor and are validated end-to-end against the live daemon, no
  mocks.

## Code

Sites this ADR touches (all read-path / extraction-feed; none change the schema):

- `src/marginalia/subgraph.py` — seed loop (~245-260, register claim-seed subject
  entity), `_ranked_bridge_claims` (160-193, relevance-aware degree cap),
  block-colocation expansion (new).
- `src/marginalia/companion/__init__.py` — `_ASK_DEGREE_CAP_DEFAULT` and the
  subgraph-render fallbacks (Tier-1 knobs).
- Ingest extraction feed (Tier 2) — mint Claims for dense factual surfaces while
  preserving Claim → Block byte anchoring; the 12K storage Block window is
  untouched.

---

## Implementation plan (2026-06-21)

A fused, dependency-ordered task plan for the graph-native answering arc, merging five subsystem maps (chunking/ingest, extraction/curation, graph assembly/render, retrieval/answerer, eval/rollout) with this ADR.

The arc has one job: make the 100× cheaper subgraph path (currently 0.6 answer-presence single-run) match or beat the block-dump ceiling (controlled 0.72 / neg-guard 0.917) without touching the 5-primitive closed schema, the markdown trust-root, or Claim→Block byte anchoring. This ADR root-causes the gap precisely: **63% of subgraph misses are EXTRACTION-GAP** (the answer fact was never minted as a Claim — it lives only as raw bytes in tables, config `key=value`, version/status strings) and **29% are ASSEMBLY-GAP** (the Claim exists but the ego-walk drops it — Claim seeds never register their subject entity as a visible node, and the degree-cap truncates by arbitrary id order instead of query relevance). Critically, `hops=2` recovers ZERO assembly misses — the answer-bearing Claims are already in the 1-hop neighborhood when they exist, so the failure is dropping/never-minting, not shallow walking. **Do NOT spend effort on deeper walks.**

The phasing is forced by two hard lessons. First (v0.0.24 noise lesson): you cannot measure a 0.05–0.15 lift in a temp>0 regime with single runs, so the measurement instruments (claim-coverage floor metric, neg-guard gate, multi-run A/B harness with McNemar+bootstrap) MUST exist and be trusted before any code that claims to move the needle. Second (cost story): assembly fixes are hours and need NO re-ingest (pure read-path), extraction fixes are days and REQUIRE a full re-ingest of every reference vault. So: **Phase 0** builds the instruments (hours, no reingest). **Phase 1** lands the three assembly fixes collapsed into ONE canonical set (subject registration, query-relevance degree-cap, block-colocation) plus the plumbing to thread the query embedding in — targeting ~0.73, no reingest. **Phase 2** is the real wall: dense-fact extraction (deterministic table/config minting first, then an optional LLM dense-fact pass), the only path past 0.72, and it needs a re-ingest. **Phase 3** tunes the answerer (graph-structured system prompt, coverage threshold sweep) and flips the default from block-dump to subgraph ONLY behind the full three-gate ship rule.

Verified against live code: `recall_floor.py:309-310` already computes `claim_on_quote` (byte-span coverage) but never rolls it up or hashes it into the gated payload — so the claim-coverage metric is a wiring task, not net-new logic. `negctl` is parsed (line 257/325) but never gated. `scorecard.py` already has `compare_arms`, `mcnemar_exact_p`, `multiseed_bands`, and the 5-condition REAL rule — the multi-run runner is the only missing piece. The three assembly-fix descriptions appear in BOTH the graph-assembly map and the retrieval map; they are deduplicated into GN-4/5/6/7 with a single owner each. The Tier-2 dense-fact work appears as both a deterministic-regex path (chunking map) and an LLM-pass path (extraction map) — the cheap deterministic path is sequenced FIRST (GN-9/10) and the expensive LLM pass (GN-12) is gated on whether deterministic extraction alone clears the bar, per this ADR's "land the cheap thing first" discipline.

Honest effort accounting: Phase 0 ~2–3 days. Phase 1 ~3–4 days (the fixes are S/M but validation against a noisy 120-Q judge is the long pole). Phase 2 is the real cost: 1–2 weeks of build plus a full re-ingest cycle per reference vault per iteration, and it is gated on a human go/no-go because it burns the re-ingest budget. Phase 3 ~3–4 days plus a production A/B soak.

> **The deterministic floor (claim-coverage + recall + neg-guard, all judge-free) remains the ONLY CI gate; the multi-run A/B is a laptop-only fleet-decision instrument, never a CI blocker.**

### Phase 0 — Measurement instruments (judge-free floor + trusted A/B)

Hours, NO re-ingest.

**Goal:** Build and pin the instruments that can detect a 0.05–0.15 lift in a temp>0 regime BEFORE any code claims to move the needle. Per the v0.0.24 lesson, single-run numbers are noise; nothing downstream is believable until claim-coverage, neg-guard, and a multi-run McNemar A/B harness exist and are validated on synthetic-ci fixtures. This phase isolates extraction-quality (is the fact a Claim?) from query-quality (did we retrieve it?) so later phases can attribute every miss to the right tier.

**Gate:** On the frozen synthetic-ci vault: (a) `recall_floor.py` emits a NEW gated `claim_coverage` rate (claim byte-span covers gold quote) AND `neg_guard_rate`, both folded into `metric_payload_sha256` and checked by `compare_to_baseline` with zero tolerance; baselines re-pinned and CI green. (b) `scorecard ab-runset` runs N=5 block-vs-subgraph pairs end-to-end on synthetic-ci, emits median[IQR] delta + McNemar p + 5-condition REAL vote, and its selftest passes. (c) `docs/eval-gates.md` enumerates the three ship gates. No `src/marginalia/` behavior changed; deterministic floor still the only CI gate.

| ID | Title | What / Detail | Files | DoD | Effort | Re-ingest? | Depends-on |
|----|-------|---------------|-------|-----|--------|------------|------------|
| GN-1 | Roll up + gate claim-coverage metric in the deterministic floor | `recall_floor.py` already computes `claim_on_quote` at lines 309-310 (a Claim whose byte_span fully covers the gold quote bytes) but only uses it for the per-q `extraction_present` bool; it is never rolled up as its own rate nor in the gated `metric_payload_sha256` (which hashes only `hard_recall_at_k` + `extraction_completeness`, lines 392-413). Add a `claim_coverage` roll-up `{claim_present, claim_total, rate}` distinct from `extraction_completeness` (which only checks "any Claim on the Block"). `claim_coverage` answers the stricter "does THIS specific answering Claim exist and cover the quote span" — the true extraction-quality floor. Fold its rate into `_metric_payload_sha256` and `compare_to_baseline` (zero tolerance, regression on drop). Quantifies Phase-2 extraction-gap closure. | `tests/golden/bin/recall_floor.py` (run_probe ~194-333: claim_coverage roll-up after per_q loop; `_metric_payload_sha256` ~385-420; `compare_to_baseline` ~428-516; `cmd_gate` report + selftest ~601-731) | selftest passes proving `claim_present ≤ extraction_present ≤ hard_recall`. cmd_gate report shows `claim_present/claim_total`. synthetic-ci baseline re-pinned with new payload sha; a deliberately-mangled byte-span fixture trips the gate (exit 1). Pure Python over frozen `graph.lbug`, no LLM/daemon. | M | No | none |
| GN-2 | neg-guard lives in the answer-eval/A-B layer, NOT the deterministic floor | The deterministic floor (`recall_floor.py`) is retrieval/extraction only — it calls `vault.query()` and never the LLM answerer. neg-guard ("did the answerer correctly decline on a false-premise question?") is an LLM-answerer property: it requires running `/ask` and grading the returned `text`. It belongs in the answer-eval / A-B layer, graded as `all_mc` over `negative_control` rows via the answer harness (GN-3 ab-runset), exactly as the v0.0.24 ceiling study measured block-dump neg-guard 0.917. No neg-guard metric is added to `recall_floor.py`. | `tests/golden/bin/scorecard.py` (graded via `all_mc` in the answer-eval harness, GN-3) | neg-guard is measured and reported as part of the GN-3 multi-run A/B harness, not the CI floor. The deterministic floor remains judge-free (no `/ask`, no LLM). | M | No | GN-1,GN-3 |
| GN-3 | Multi-run A/B harness (cmd_ab_runset) for subgraph-vs-block with significance | `scorecard.py` already has `compare_arms` (~329), `mcnemar_exact_p` (~137), `multiseed_bands`, and the 5-condition REAL rule (~379-394). The ONLY missing piece is orchestration that runs the SAME block-vs-subgraph pair N≥5 times under temp>0 sampling and aggregates. Add `cmd_ab_runset`: input `{run_both, endpoint, questions, N_runs, seed_base}` OR `{block_arm, subgraph_arm, runs}`. Per run i: obtain block-mode JSONL (`MARGINALIA_QUERY_NEIGHBORS=0`) and subgraph-mode (`=1`), grade both, `compare_arms → scorecard_i`; after N, `multiseed_bands → median[IQR]` delta, escalate_votes, real_votes, classification breakdown. Makes Phase-1/3 deltas believable; laptop-only, NOT a CI gate. | `tests/golden/bin/scorecard.py` (new `cmd_ab_runset` reusing `compare_arms` + `multiseed_bands`; wire judge.py dispatch `scorecard ab-runset`); reuse `run-golden.sh` QUERY_NEIGHBORS toggle (~269-270) | scorecard selftest extended + green. `judge.py scorecard ab-runset --config ...` runs 3 independent runs on synthetic-ci, emits well-formed `{runs, bands, escalate_votes, real_votes}` with non-pathological variance band (<10pt). Manual: block-vs-subgraph on a real laptop vault registers the delta + McNemar p for N=5. LLM seed/model pinning documented so reruns reproduce. | L | No | none |
| GN-8 | Document the three ship gates (eval-gates.md) + re-ingest cost budget metadata | Write `docs/eval-gates.md` naming the three gates so the arc shares one DoD: (1) DETERMINISTIC FLOOR (CI-provable, judge-free): hard-recall@k, extraction-completeness, claim-coverage (GN-1), neg-guard (GN-2) — no regression on any. (2) MULTI-RUN A/B SIGNIFICANCE (laptop-only): 5-condition REAL rule on ≥5 runs via GN-3 — directional + McNemar p<0.05 + powered (delta≥MDE) + CI excludes 0 + material (≥5pt). (3) COST BUDGET: Tier-1 = hours, NO re-ingest (read-path); Tier-2 = days, re-ingest REQUIRED per reference vault. Add a `tier_cost/needs_reingest` field to the worklist/manifest. `eval-run.sh` logs each gate result explicitly; the flip-default decision is driven by the log, not an exit code. | NEW `docs/eval-gates.md`; `tests/golden/bin/recall_floor.py` or manifest module (tier_cost metadata); `tests/golden/eval-run.sh` (log `[gate] deterministic-floor PASS`, `[gate] A/B REAL|NOT_REAL`, `[gate] cost-budget tier-N`) | eval-gates.md cites the exact CI gate (recall_floor exit 1) + the A/B 5-condition rule. eval-run.sh prints all three gate lines on a synthetic-ci run. manifest emits tier_cost. Reviewed for alignment with ADR 0019 binding constraints (5-primitive closed, byte anchoring, deterministic-floor-is-only-CI-gate, markdown trust-root). | M | No | GN-1,GN-2,GN-3 |

### Phase 1 — Tier-1 assembly fixes (read-path only)

Days, NO re-ingest, target ~0.73.

**Goal:** Close the 29% assembly-gap by keeping already-minted Claims in the ego-graph, making their subject entities walkable, and ranking the degree-cap cut by query relevance instead of arbitrary id order. Cheapest win in the arc: pure read-path, no graph writes, no re-ingest, schema untouched. The ceiling here is capped at ~0.73 (still inside controlled-block-dump 0.72 territory) because the 63% extraction-gap is untouched — that is fine; the goal is to recover the assembly misses cheaply and prove the instruments work on a real delta.

**Gate:** On the 120-Q grounded-golden set via GN-3 multi-run A/B (N≥5): subgraph answer-presence improves over the ~0.6 single-run baseline toward ~0.73 with the delta classified at least DIRECTIONAL (REAL is a stretch at this magnitude/noise — report honestly). Deterministic floor (claim-coverage + recall + extraction + neg-guard) shows ZERO regression. Per-fix ablation (disable each of GN-5/6/7) attributes the lift. neg_guard stays ≥0.91. All changes read-path; a clean ingest is reused, not rebuilt.

| ID | Title | What / Detail | Files | DoD | Effort | Re-ingest? | Depends-on |
|----|-------|---------------|-------|-----|--------|------------|------------|
| GN-4 | Thread query embedding into build_ego_graph (plumbing for query-aware assembly) | Pure signature/plumbing extension so GN-5 has a query signal at assembly time. Add `query_embedding: list[float] | None = None` (and optional `query:str` for tracing) to `build_ego_graph` (`subgraph.py:212`) and thread through `_bridge_entity_seed` (~445), `_bridge_claim_seed` (~482), `_ranked_bridge_claims` (160-193). In `_subgraph_context` (caller, ~3189), compute query_embedding via the vault-configured embedder (same provider/normalization as ingest+store) and pass it down. Guard None embedder → None → `_ranked_bridge_claims` falls back to existing `(-confidence, id)` sort (backward-compatible). No logic change yet; GN-5 consumes it. | `src/marginalia/subgraph.py:212-221` (build_ego_graph sig), `160-193` (_ranked_bridge_claims sig), `445-505` (_bridge_*_seed threading); `src/marginalia/companion/__init__.py:~3189` (_subgraph_context computes + passes embedding) | Type-checks (`list[float]|None`). End-to-end ask with subgraph path threads query_embedding without error. Backward-compat proven: pass None → byte-identical to pre-patch (id sort). Model-free unit test in `tests/test_subgraph.py` asserts param accepted + defaults safely. No schema/file writes. | S | No | none |
| GN-5 | Query-relevance degree-cap for bridging Claims (replace id tiebreak) | `_ranked_bridge_claims` sorts by `(-confidence, id)` at line 192; when many Claims share a confidence band (common for a high-degree entity like "Acme" bridged by 20 Claims at 0.85), the degree_cap cut keeps the lowest ids, dropping the answer-bearing Claim purely on lexicography. Change sort key to `(-confidence, -cosine_to_query, id)` using the existing `_cosine` helper (`query.py:205-214`) against the GN-4 query_embedding + each Claim's stored embedding. Missing embeddings degrade to id tiebreak. Determinism preserved. Single highest-leverage assembly fix per ADR 0019:59-74. | `src/marginalia/subgraph.py:160-193` (_ranked_bridge_claims sort key); reuse `src/marginalia/query.py:205-214` (_cosine) | Unit test: entity with 10 bridging Claims at conf 0.85-0.90, degree_cap=8 keeps the 8 highest-cosine-to-query, not first 8 by id. GN-3 A/B microbench on 120-Q shows directional lift attributable to this fix (ablation: revert to id-only → lift disappears). Deterministic floor not regressed; neg_guard preserved. Backward-compat: query_embedding=None → identical to old behavior. | M | No | GN-4 |
| GN-6 | Register Claim-seed subject entity in ego.nodes (make the fact walkable) | When a Claim is a seed, `_bridge_claim_seed` (`subgraph.py:482-505`) reads S_id and routes to the object neighbour but NEVER adds the subject entity to `ego.nodes` as a visible NODES row (comment at 251-253 justifies this to avoid bloat — ADR 0019:62-66 calls it a defect). Result: a bridging Claim row with no anchor node for the answerer to pivot from, e.g. "Alice founded Acme" surfaces Acme but drops Alice. Fix: after reading subject_id, `store.get_node(subject_id)`; if it passes `_passes_gates`, register via `_node_record(subject_node, is_seed=False, score=0.0)`. Guard None/infra/low-quality (reuse existing gates). Pure visibility addition; routing unchanged. | `src/marginalia/subgraph.py:245-256` (seed loop), `482-505` (_bridge_claim_seed), `508-516` (_subject_name); gates via existing `_passes_gates` | Unit test: Claim seed "Alice founded Acme" surfaces BOTH Alice + Acme as visible NODES rows (mirror `test_entity_seed_reaches_related_entity_via_topology_claim`). GN-3 A/B on 120-Q: no answer-presence regression on the deterministic floor; spot-audit 5 Claim-seed answers confirm subject node appears + enables link chain. Malformed/missing S_id → silent skip (no-op). | S | No | GN-4 |
| GN-7 | Block-colocation expansion for co-extracted Claims | Facts extracted from the same source Block are topically adjacent by construction (a config block yields 3-4 key=value Claims from the same bytes), but the strict edge-walk + degree_cap drops all but the highest-confidence per entity. Tier-1: when a Claim seed (or bridged Claim) is kept, pull its sibling Claims sharing the same `block_id` facet. In `_bridge_claim_seed`/`_bridge_entity_seed`, after the ranked bridge, read block_id, enumerate same-block Claims (filter: confidence≥min, predicate allowed, triple not in `seen_triples` dedup set), add via `_route_claim`. Co-location is a SECONDARY pass that does NOT consume degree_cap; cap loosely (e.g. +3 or max-20-per-block sorted by `(-confidence, -cosine_to_query)`) and let the renderer token budget trim. Read-only; block_id + source_span already on every Claim. | `src/marginalia/subgraph.py:445-505` (_bridge_*_seed colocation loop), `318-360` (_record_topology_claim dedup), `store/protocol.py` (use existing list/iterate by facet; add helper only if none exists) | Unit test: seed Claim at block B123 with subject Alice + 3 sibling Claims at B123 (same subject, different predicates) → all 3 appear in ego.claims/relationships (not dropped by edge-walk cap), dedup prevents doubles. GN-3 A/B on 120-Q: factual/extraction-heavy questions lift, compounding with GN-5/GN-6 toward ~0.73. Per-block cap prevents O(n) runaway on a 100-Claim block. Deterministic floor not regressed. | M | No | GN-4,GN-6 |

### Phase 2 — Tier-2 dense-fact extraction

Days-to-weeks, REQUIRES re-ingest, target past 0.72.

**Goal:** Close the 63% extraction-gap — the real wall and the only path beyond the block-dump ceiling. Mint Claims for the dense factual surfaces (table cells, config key=value, version/image strings, status labels) that today live only as raw bytes and are invisible to any graph walk. Do the CHEAP deterministic path first (regex table/config minting, zero LLM cost, fully reproducible, byte-anchored) and only escalate to a second LLM extraction pass if deterministic minting alone fails to clear the bar. Every new Claim is still a Claim (no sixth primitive) and MUST carry `block_id` + `byte_start/byte_end`. This phase burns the re-ingest budget on every iteration, so it is gated on a human go/no-go.

**Gate:** On the 120-Q grounded-golden set after a full re-ingest of the reference vaults: `claim_coverage` (GN-1 metric) rises measurably (the pipeline now KNOWS the dense facts), and GN-3 multi-run A/B shows subgraph answer-presence on dense-fact questions classified REAL by the 5-condition rule and at/above controlled block-dump 0.72. Deterministic floor green (claim-coverage UP, neg-guard held ≥0.91). Re-ingest is idempotent: a second rebuild produces identical Claim ids, only corroboration deltas (ADR 0016). Byte-anchoring verified — zero free-floating Claims.

| ID | Title | What / Detail | Files | DoD | Effort | Re-ingest? | Depends-on |
|----|-------|---------------|-------|-----|--------|------------|------------|
| GN-8.5 | Write predicate-vocabulary ADR | PREREQUISITE for all dense-fact minting (decision 2, resolved 2026-06-21). Write a short ADR canonicalizing the dense-fact predicates (`has_version`, `has_config`, `has_status`, `has_value`, `has_measurement`, `table_cell`, etc.) BEFORE GN-9 mints the first dense-fact Claim. Once minted, these predicates bake into `semantic_claim_id` and the SSSOM alias ledger (ADR 0017); locking the vocabulary up front keeps claim-identity + the ledger stable across re-ingests and avoids expensive vocabulary churn after a re-ingest. | NEW `docs/adr/00XX-dense-fact-predicate-vocabulary.md`; cross-link from ADR 0017 (SSSOM ledger) | ADR enumerates the canonical dense-fact predicates + their definitions and literal-object expectations. Reviewed for alignment with ADR 0016 (semantic claim identity) + ADR 0017 (predicate canonicalization / SSSOM ledger). Landed before any GN-9/10/12 code mints a dense-fact Claim. | S | No | none |
| GN-9 | Deterministic GFM table-cell Claim minting (no LLM, byte-anchored) | Add a deterministic secondary extraction pass over the raw 12K window (window stays at `_WINDOW_BYTES=12000`, `markdown.py:22` — locked per `feedback_chunking_fixed_12k`; do NOT touch storage Block boundaries). Detect GFM pipe-tables via a state machine (`_tables_in_text` returning byte spans + parsed header/rows, no external lib), then mint one Claim per non-header cell: S=document(or row-label entity), P=`table_cell` (Claim with literal object — NOT a new primitive), O_literal=`{row}:{col}={value}`, byte-anchored to the cell's bytes within the window, confidence 1.0. Tables split across two windows yield partial Claims per window (coarse-but-honest, ADR 0003 E4). Handle pipe-in-cell via backslash-escape detection or fall back to row/col indices. | `src/marginalia/ingest/markdown.py:259-317` (deterministic claim loop; add `_tables_in_text` + minting alongside `_headings_in_text` at ~278); `tests/ingest/test_table_extraction.py` (new) + `test_claim_minting.py` | Gold-span test: a 3×3 table yields ≥9 Claims (one per non-header cell), each byte span verifiable back to source bytes. End-to-end: ingest markdown with a GFM table, confirm Claims created, render a subgraph + see cell values in CLAIMS rows. Measure token reduction vs raw 12K block-dump. `claim_coverage` (GN-1) detects new covered quotes. Re-ingest deterministic (same cell → same `semantic_claim_id`). | M | Yes | GN-1,GN-8.5 |
| GN-10 | Deterministic code-fence + config key=value Claim minting (no LLM, byte-anchored) | Within fenced code blocks and the frontmatter code_block, deterministically detect single-line `key=value`/`key: value` patterns that are dense facts (versions, statuses, settings) via a permissive regex (e.g. `^\s*([\w._-]+)\s*[:=]\s*(.+)$`). Mint a Claim per pair: S=named subject/document, P from a small canonical vocabulary (`has_version`, `has_config`, `has_status`, `has_value`), O_literal=`key=value` (or just value), byte-anchored to the pair's bytes, confidence 1.0. Conservative: single-line only, skip multi-line/indented YAML + JSON arrays (those are GN-12 territory). `_headings_in_text` (`markdown.py:153-171`) already proves fence-awareness — reuse it. Reject placeholder values (null, n/a, none, unknown). | `src/marginalia/ingest/markdown.py:153-171` (fence detection reuse) + `259-317` (claim loop; add `_config_pairs_in_text`); `tests/ingest/test_config_extraction.py` (new) + `test_claim_minting.py` | A fence with `version=1.2.3` + `status=live` yields 2 Claims, each byte-anchored. End-to-end golden: "what version is deployed?" where the version is buried in a code block now answerable from the graph (`claim_coverage` detects it). Conservative parsing: indented multi-line YAML does NOT mis-mint. Deterministic across re-ingest. | M | Yes | GN-1 |
| GN-11 | Validate deterministic extraction-gap closure + idempotent re-ingest/dedup | Two jobs: (1) Measure how much of the 63% extraction-gap the cheap deterministic path (GN-9+GN-10) closes, using the GN-1 claim-coverage floor metric + GN-3 multi-run A/B (McNemar + paired bootstrap) on a vault re-ingested with table+config minting. Report per-question deltas + the honest conclusion: "deterministic extraction closes X% of the gap; combined with Phase-1 assembly, graph-native reaches ~Y answer-presence." (2) Prove re-ingest idempotency: dense-fact extraction must be deterministic (temp 0, locked logic) so `semantic_claim_id` (`consolidate/_claim_identity.py`) dedups re-mentions via ADR 0016 corroboration rather than minting duplicates. Run `ingest_quality_check` after a double rebuild: zero new Claims, only corroboration deltas; byte anchors stable. | `scripts/ingest_quality_check.py` (dense-fact metrics); `tests/test_table_config_extraction_golden.py` (new); `src/marginalia/companion/__init__.py:~3577-3591` (semantic_claim_id corroboration path); `consolidate/_claim_identity.py` (idempotence) | Golden test ingests a doc with table+config, verifies Claims minted + byte-anchored, renders subgraph, answers "what is the status in row 2?" / "what version is specified?" + confirms the fact is now present (was absent before). GN-3 A/B reports mean delta, McNemar p, effect-size CI on ≥25 dense-fact questions. Double-rebuild shows identical Claim ids + corroboration-only deltas (no dupes). **HUMAN GO/NO-GO recorded here:** does deterministic extraction clear the bar, or is GN-12 needed? | L | Yes | GN-9,GN-10 |
| GN-12 | GATED: LLM dense-fact extraction pass (only if GN-11 misses the bar) | ONLY pursue if GN-11's human go/no-go concludes deterministic table+config minting alone does NOT clear controlled-0.72 on dense-fact questions. Add a second LLM extraction step specialized for dense surfaces the regex path can't reach (multi-line config, indented YAML, prose-embedded versions/measurements, status labels in sentences). Emits ONLY Claims (no nodes/edges), normalizes predicates to the canonical vocabulary at extraction time (`has_version/has_config/has_attribute/has_value/has_status/has_measurement`), byte-anchors every Claim. Run after entity extraction, before the relationship curator; track dense-fact candidates separately in the ledger. ALSO tune the curator (`curator.py:107-222`) to treat metadata predicates (currently demoted by the prefilter at companion ~2011-2042 / extract `_is_metadata_predicate`) as VALID for literal-object dense-fact Claims, and reject placeholder literals. Risk: +1 LLM call per remember (cost/latency) + prompt-cache pressure — must argue prompt-cache reuse (stable prefix, small delta) per the efficiency principle. Must stay deterministic enough (temp 0) for re-ingest dedup. | `src/marginalia/extract/dense_facts.py` (new) + `extract/__init__.py` (integrate, add worked examples to `_BASE_SYSTEM`); `src/marginalia/curator.py:107-222` (dense-fact grounding rules + examples); `src/marginalia/consolidate/prefilter.py` (exempt literal-object dense-fact Claims from metadata-predicate demotion); `companion/__init__.py` (wire pass before claim minting + ledger `dense_facts` kind) | Dense-fact extractor emits ≥N verified byte-anchored Claims from a test block with multi-line config/prose versions the regex path missed. Curator accepts ≥90% of valid dense-fact literal Claims with metadata predicates from grounded excerpts; prefilter no longer demotes them. GN-3 A/B on ≥30 dense-fact questions: answer-presence classified REAL + at/above 0.72; neg-guard held. Re-ingest idempotent (temp 0, dedup via `semantic_claim_id`). Cost delta measured + prompt-cache reuse demonstrated. | XL | Yes | GN-11 |

### Phase 3 — Answerer/render tuning + safe default flip

Days + production soak, NO re-ingest.

**Goal:** Make the answerer parse the typed NODES/RELATIONSHIPS/CLAIMS render natively, tune the coverage threshold against the now-improved graph, and flip the default from block-dump to subgraph ONLY once all three ship gates hold. The flip is the payoff of the whole arc (100× cheaper context at parity-or-better quality), but flipping early is the worst failure mode (silent quality regression for every user), so it sits behind a feature flag, a deterministic-floor gate, a multi-run REAL classification, and a production A/B soak.

**Gate:** FULL THREE-GATE SHIP RULE all true: (1) deterministic floor green — claim-coverage + recall + extraction + neg-guard no regression; (2) GN-3 multi-run A/B classifies subgraph-vs-block delta as REAL (5 conditions) at/above controlled 0.72 with neg-guard ≥0.91; (3) context savings verified ≥100× cheaper. Plus a production A/B soak behind the flag showing no regression before the code default changes. If any gate fails, the flag stays False and the arc holds at Phase-2 quality with block-dump default.

| ID | Title | What / Detail | Files | DoD | Effort | Re-ingest? | Depends-on |
|----|-------|---------------|-------|-----|--------|------------|------------|
| GN-13 | Graph-native answerer system prompt variant (_ASK_SYSTEM_GRAPH) | `_ASK_SYSTEM` (`companion/__init__.py:555`) is generic prose-aware ("You answer grounded in the provided notes. Be concise.") and all three paths share it. Add `_ASK_SYSTEM_GRAPH` (~200 tokens, still terse per ADR 0018 to avoid extractive verbosity) that primes the LLM to: parse the typed NODES index (N0, N1) + edge arrow notation, trace subject-predicate-object chains + 1-hop entailment, cite by claim_id/block_id from facets (not byte offsets), and skip rows below the confidence threshold unless asked. Pass it from `_ask_subgraph` when the context is a typed render; expose `llm.ask.system_prompt_graph` as an override (code default the new prompt). A/B it (GN-3) against the generic prompt on the same k before adopting; revert if no lift. | `src/marginalia/companion/__init__.py:555-565` (_ASK_SYSTEM) + new `_ASK_SYSTEM_GRAPH` ~566; `_complete_ask` ~3015 (accept graph_native flag); `_ask_subgraph` ~3087 (pass system_prompt when typed); config `llm.ask.system_prompt_graph` knob | Graph-native subgraph path uses `_ASK_SYSTEM_GRAPH`. GN-3 A/B on the grounded set: answer-presence unchanged-or-improved vs generic prompt at the same k, neg-guard not regressed. Output is plain text (markdown trust-root unaffected); no graph writes. | S | No | GN-3 |
| GN-14 | Coverage threshold sweep + optional entropy-aware density gate | `_ASK_COVERAGE_DEFAULT=0.4` (`companion:574`) is an old ADR-0011 constant, never swept against answer-presence on the improved graph. Run a deterministic A/B sweep of `{0.2, 0.4, 0.6, 0.8}` via GN-3 (same regime as ADR 0018), measuring Tier-1 acceptance rate vs neg-guard at each; lock the threshold that maximizes answer-presence while holding neg-guard >0.91. Optionally add `_subgraph_context_density_entropy(context)->float` and, when confidence variance is high on a sparse render, raise the effective floor (sparse-high-confidence is trustworthy; sparse-mixed is risky) behind `llm.ask.coverage_entropy_aware` (default False). Watch the Tier-1/Tier-2 split: a high threshold pushes more Tier-2 fallback = more cost; measure end-to-end cost + accept the tradeoff explicitly. | `src/marginalia/companion/__init__.py:574` (_ASK_COVERAGE_DEFAULT), `625` (_subgraph_context_thin), `~3099-3110` (_ask_subgraph gate) + optional entropy knob | Threshold locked to best-performing value on the grounded set via GN-3 multi-run. Subgraph answer-presence at the locked threshold matches-or-exceeds block-dump 0.72; neg-guard >0.91. Tier-1/Tier-2 split + cost delta reported. Optional entropy gate reduces fallback on sparse/uncertain renders without regressing answer-presence. Read-path only. | M | No | GN-3 |
| GN-15 | Hybrid/lexical re-seeding for thin entity handles | RRF (`query.py:255-386`) is drift-blind: a strong lexical seed can be buried by a noisy vector top-5, and there is no recovery when top seeds are semantically thin (handle/acronym variants). Add a post-RRF thin-seed recovery pass (do NOT change the 5-leg formula): for each top-5 vector seed, check store degree against a `_THIN_HANDLE_DEGREE_FLOOR`; if flagged, boost lexical+title ranks by a fixed multiplier and re-fuse only the top-20. Keep `_RRF_K` unchanged + the re-fuse deterministic (stable tiebreak via seed ids). Lower priority than GN-13/14 — sequence after them and only ship if GN-3 shows it lifts recall@20 without hurting neg-guard. | `src/marginalia/query.py:236-242` (_rrf), `289-291` (vector path), `companion ~3189` (query_seeds caller); add thin-handle detector + conditional re-rank | A query with a handle variant retrieves both forms in top-20. recall@20 unchanged-or-improved on the full eval set via GN-3; abstention/neg-guard preserved. Re-fuse deterministic (re-run yields identical order). Pure read-path, schema untouched. | M | No | GN-5 |
| GN-16 | Flip default block-dump → subgraph behind the three-gate ship rule | `enable_subgraph` defaults False (`companion:127`, read STEP-DIRECT at ~2845). Flipping is the arc's payoff but the highest-risk action. Add a config knob `llm.ask.enable_subgraph_by_default` (default False, locked). Flip the code default to True ONLY after the FULL three-gate rule (deterministic floor green + GN-3 multi-run REAL at/above 0.72 + ≥100× context savings verified) AND a production A/B soak behind the flag shows no regression. Keep the knob for opt-out; keep block-dump as the safe fallback on provider error or coverage miss. The deterministic eval floor (judge-free CI) is the gate of record — never a single-run number, never a bare A/B p-value. | `src/marginalia/companion/__init__.py:127` + `~2842-2848` (enable_subgraph default + STEP-DIRECT read); `src/marginalia/config/_vault.py` (enable_subgraph_by_default knob); integration tests for both paths + error fallback | Subgraph path validated ≥ block-dump on answer-presence AND ≥100× cheaper context, with neg-guard ≥0.91, via GN-3 multi-run REAL classification. Code default configurable + safe (falls back to block-dump on error/coverage-miss). Production A/B soak recorded clean before the default changes. `eval-run.sh` logs `>> FLIP DEFAULT: READY` only when all three gates pass. | L | No | GN-8,GN-11,GN-13,GN-14 |

### Sequencing

**CRITICAL ORDERING — measurement before motion.** Phase 0 (GN-1,2,3,8) MUST land first and be trusted. The v0.0.24 lesson is binding: a 0.05–0.15 answer-presence lift under temp>0 is invisible to single runs, so claim-coverage (GN-1), neg-guard (GN-2), and the multi-run McNemar A/B (GN-3) are prerequisites for believing ANY Phase 1/2/3 delta. GN-1 first because GN-2 extends the same metric-payload/baseline-pinning machinery and GN-9/10/11 all measure extraction-gap closure with the GN-1 metric.

**DEDUPLICATION performed:** the three assembly fixes appear in BOTH the graph-assembly map and the retrieval/answerer map with near-identical descriptions. Collapsed to one owner each: subject registration = GN-6, query-relevance degree-cap = GN-5, block-colocation = GN-7, plus the plumbing GN-4 that both maps implied. GN-4 is the hard gate for GN-5 (no query embedding → no relevance ranking) and is cheap/backward-compatible, so it goes first in Phase 1. GN-6 before GN-7 because colocation expansion piggybacks on the subject-registration path. GN-5/6/7 are independent enough to parallelize after GN-4, but validate together (their lifts compound and ablation needs all three present).

**TIER-2 dual-path resolution:** the chunking map proposes deterministic regex table/config minting; the extraction map proposes an LLM dense-fact pass. ADR 0019 says land the cheap thing first. So GN-9 (tables) + GN-10 (config) are deterministic, zero-LLM, fully reproducible, and run before GN-11 measures whether they alone clear the bar. GN-12 (the expensive LLM pass + curator tuning) is GATED on GN-11's human go/no-go — do NOT build it speculatively; it is XL, costs an LLM call per remember, and pressures prompt-cache reuse.

**RE-INGEST DISCIPLINE:** only Phase-2 tasks (GN-9,10,11,12) set `needs_reingest=true`. Each Phase-2 iteration burns a full re-ingest of every reference vault, so batch GN-9+GN-10 into ONE re-ingest for GN-11's measurement rather than re-ingesting per task. GN-3 must be in hand before any re-ingest so the post-reingest delta is measurable, not anecdotal.

**PHASE 3 internal order:** GN-13 (graph prompt) and GN-14 (threshold sweep) are independent and can run in parallel after the graph is improved; GN-15 (re-seeding) is lower-priority and only ships if it pays off. GN-16 (the flip) depends on the gate doc (GN-8), the extraction validation (GN-11), and the answerer tuning (GN-13/14) — it is the last action and is itself gated on a production soak, not just CI.

The design-only sketches from the chunking map (sub-window strategy, gleaning memo) are intentionally DROPPED from the build plan — they are speculative Tier-3 and ADR 0019 explicitly warns sub-windowing is days of work to be pursued only if Tier-2 dense extraction misses; fold them into the GN-11 go/no-go discussion instead of pre-committing tasks.

### Decisions (resolved 2026-06-21)

1. **Re-ingest scope (Phase 2):** reference-eval vault ONLY — it is the only vault with the 142-Q grounded golden key, so the only place extraction-gap closure is measurable. Generalize to other corpora later, after it works.

2. **Predicate vocabulary:** LOCK FIRST — write a short predicate-vocabulary ADR (canonicalizing `has_version` / `has_config` / `has_status` / `has_value` / `has_measurement` / `table_cell`, etc.) BEFORE GN-9 mints the first dense-fact Claim, so claim-identity + the SSSOM ledger (ADR 0017) stay stable across re-ingests. This is a new Phase-2 PREREQUISITE task (GN-8.5: "Write predicate-vocabulary ADR", effort S, blocks GN-9/10/12).

3. **Cell-value representation:** O_literal for v1 (e.g. Claim subject=Radarr predicate=`has_version` O_literal=`6.0.4.10291`). Defer O_id entity-references to a later ADR only if entity-dense surfaces appear in real corpora.

4. **Flip-the-default bar:** PARITY at ~100× cheaper context with neg-guard held (NOT the 5-condition REAL-beat rule). So Phase 1 alone (~0.73 ≈ controlled 0.72) can justify flipping block→subgraph. neg-guard non-regression is the hard gate.

## Phase 1 — Implemented and measured (2026-06-21)

GN-4/5/6/7 landed in `src/marginalia/subgraph.py` (claim-seed subject registration, query-relevance degree-cap via cosine similarity, block-colocation cap-3) plus query-embedding plumbing in `src/marginalia/companion/__init__.py`. Tests: 18/18 subgraph tests green.

**Measured result (5×5 robust A/B, reference-eval vault, qwen3.6-35b on a LAN inference server):**

| Path | answer-presence (mean±sd) | neg-guard (all_mc) |
|---|---|---|
| block-dump k=20 | 0.726 ± 0.006 | 0.917 |
| subgraph (Phase-1 fixes) | 0.517 ± 0.021 | 0.883 |

Delta: −0.209. Verdict: **assembly fixes alone do NOT reach block parity.** The flip-bar (parity@cheaper) is NOT met by Phase 1. The 63% extraction gap (facts never minted as Claims) is the dominant lever. Phase 2 (dense-fact extraction + reingest) is required. Phase-1 code is committed as a validated checkpoint; the default remains block-dump until Phase 2+3 close the gap.

One measured service-entity case confirmed a retrieval-seed gap (the entity was never seeded as a Claim subject), not an assembly gap — the initial ADR diagnosis over-labeled it.

5. **Production A/B soak:** there is no production telemetry; "soak" = a laptop multi-run A/B (GN-3 harness) vs real vaults. GN-16's soak collapses into the laptop fleet decision.

## Phases 2 & 3 — measured results (2026-06-22, reference-eval, qwen3.6-35b, 142 golden Qs)

**Phase 2 (LLM-extraction lever, GN-9 reframed):** the L1 dense-fact prompt (`extract/__init__.py` `_BASE_SYSTEM`) types concrete assertions as `has_version`/`has_config`/`has_status`/`has_measurement`/`has_value` Claims. Reingest minted +352 has_* Claims (the 5 target predicates ~0→129; net Claims +6 — a re-typing pass, not volume growth). 5×5 A/B vs the Phase-1 baseline: **subgraph answer-presence 0.517±0.021 → 0.615±0.007 (+0.098, significant)**, neg-guard 0.850; block 0.726→0.675 (same-daemon variance). The subgraph↔block gap narrowed −0.209 → −0.060. Deterministic dense-fact extraction (original GN-9/11) was abandoned per direction; the LLM is the lever.

**Phase 3 (GN-13/GN-14, committed this change):** GN-13 `_ASK_SYSTEM_GRAPH` was active in the Phase-2 subgraph runs (the `_ask_subgraph` path always selects it). Isolation A/B on the L1 graph: arm-A GN-13 prompt **0.615±0.007 (n=5)** vs arm-B generic `_ASK_SYSTEM` **0.554±0.006 (n=3)** → **GN-13 prompt contributes +0.061, significant**. So the +0.098 subgraph lift decomposes ≈ GN-13 prompt +0.061 + L1 extraction +0.037. GN-14 coverage gate shipped default-off (0.0).

**GN-16 flip decision:** subgraph 0.615 < block 0.675 → **default stays block-dump** (parity@cheaper not yet met). Remaining gap −0.060; next lever is L2 gleaning (extraction completeness), per `research/chunking-for-extraction-completeness.md`. All four phases (0–3) now have measured results over the v0.0.24 baseline.
