# ADR 0009: Marginalia as a Continuously-Curated Application — the Curation Control Plane

**Status:** Accepted; P1–P4 implemented, P5–P6 future product backlog
**Date:** 2026-06-05
**Deciders:** Alex Rivera, Marginalia core
**Builds on:** ADR 0007 (rebuild-from-trust-root, single-writer corruption), ADR 0008 (off-graph reconciliation)

**Multi-vault addendum — 2026-07-13.** ADR 0034 replaces the process-global
curation queue/worker with one immutable runtime per vault. The job-queue spine,
durable sidecars, and in-process single-owner rule remain; jobs and runners are
now bound to their owning vault for their complete lifetime, and each vault
serializes its own writes.

**Lifecycle addendum — 2026-07-13.** The control plane shipped and the original
nine-view concept was consolidated rather than exposed as nine top-level destinations.
The application now has seven primary views—Query, Add, Logs, Browse, Graph, Curation,
and Config. Curation is one review-first surface with **Overview**, **Review**, and
**Maintenance** tabs: Review unifies predicate folds, entity merges, and held node
candidates; Maintenance contains apply/heal, authority audit/unmerge, drift, and manual
reconcile. P1–P4 (UI, in-process jobs, deterministic heal, and continuous scheduling)
are implemented. P5 autonomy-policy/observability expansion and optional P6 in-graph
fold/MCP curation tools remain future product work, not `0.0.40` release gates. Phase
language below is preserved as the 2026-06-05 implementation sequence.

**Scheduler coverage addendum — 2026-07-18.** A completed exact
`reconcile-propose` triggered by `verified_file_commit` counts as reconciliation
coverage for the latest ingest when it retains linked ingest-item ids plus the
graph generation and finishes at or after the latest committed ingest activity.
The debounced scheduler still runs drift detection and predicate proposal, but
does not immediately repeat that same full reconciliation. A later commit moves
the ingest timestamp past the completed job and makes reconciliation eligible
again. This keeps continuous curation intact while removing duplicate model work
observed on the 17-source private organizational corpus run.

---

## Context

Marginalia is reframed from "a KG backend" into a **living application with continuous curatorship**: ingestion never stops, the graph is mutable, and curation must run as an ongoing loop while offloading most of the work from the user — who chooses *how much* to automate per operation. Today the curation machinery exists but is largely invisible and terminal-only. A grounded code map (2026-06-05) found:

- The **only continuously-running curation** is `Companion.remember()` (`companion/__init__.py:236-493`): per-block extraction, intra/within-batch dedup, store dedup tiers, a confidence gate (`consolidate/gate.py`), byte-anchored Claim minting, `ensure_source_mentions`. The only periodic loop is `kg watch` (ingest-only).
- **Two separate review queues**: companion contradiction-gate (`consolidate/review_queue.py`, exposed over HTTP `/review-queue` + `/resolve-review`, **no UI**) vs reconcile equivalence (`reconcile/queue.json`, **CLI-only**).
- **Reconcile / authority / detect-drift / rebuild / heal** have **no UI** (reconcile is CLI-only; nothing over the running MCP — `server/runtime.py` exposes zero curation tools; the rich curation MCP in `mcp_server.py` is dead code).
- **Single-writer is in-process, not OS-enforced.** `VaultConnection` caches one RW handle per vault (`store/ladybug.py:40-42`); a *second process* opening the vault is the **ADR-0007 corruption path**, not a concurrency solution. The daemon (`kg serve`) holds the one handle, which is why every CLI reconcile/rebuild forced the UI down. `server/_ingest_queue.py` is a **proven in-process, `writer_lock`-serialized worker** (`asyncio.to_thread` off the event loop).
- ~10 of ~12 admin-control-plane backing endpoints are net-new.

## Decision

1. **Curation = a step of ingestion + one debounced background sweep.** Fold cheap deterministic checks into the `remember()` commit boundary (reuse the safe incremental write path; do not build a parallel pipeline); add a single debounced in-process sweep for the expensive global passes (reconcile propose, detect-drift, anomaly/freshness).

2. **In-process curation job queue (the spine).** Generalize `server/_ingest_queue.py` into a generic `writer_lock`-serialized job queue with a durable sidecar. *Every* writing curation op (companion review commit/merge, off-graph reconcile apply, rebuild, reembed, heal) submits a job to the daemon's one handle instead of spawning a competing process. **The UI never goes down.** This is load-bearing because Ladybug single-writer is in-process — a second-process writer/reader is the corruption path (ADR 0007 + edge-id-collision incident).

3. **Per-operation autonomy levels (static policy).** `off` · `deterministic-only` · `auto-high-confidence` · `judge-gated` · `full-auto`, set per operation, mirroring the existing per-step `LLMConfig` shape (`config/_vault.py`). **Load-bearing rule: reversibility — not confidence — sets the default.** **Compiled invariant (not a config default): irreversible / topology-minting ops (edge-mint, source-mention, reset) can NEVER reach `full-auto`.** Off-graph reversible ops (reconcile apply) may auto; any graph-mutating op stays judge-gated + rebuild-routed.

4. **Equivalence stays OFF-GRAPH; the UI folds everywhere.** Keep ADR-0008 reversible off-graph `skos:exactMatch`. Close the ADR-0008 gap by routing the Browse/Graph read surfaces (`server/http.py` `api_nodes_list`, `api_node`, `api_graph`) through the same query-time equivalence fold, so the UI looks deduped everywhere — **not** just in search. True in-graph topology collapse is deferred until a collision-safe edge-id rebuild exists (P6).

5. **Historical admin control-plane target (nine conceptual views).** Curation Dashboard (health + run-now + kill-switch), Companion Review Queue, Reconcile Review Queue, Authority/Equivalence Records (with un-merge), Reconcile Run & Status, Rebuild/Heal (progress), Integrity/Health, Ingestion Status, Autonomy Config. Every status visible, every action executable. "If it's not in the UI, it doesn't exist." The shipped consolidation is recorded in the lifecycle addendum above.

6. **HTTP + UI first; MCP curation tools later** (the running MCP has none; the dead curation MCP needs wiring and HTTP is the prerequisite).

## Phasing (each independently shippable; smallest valuable first)

- **P1** — Tier-1 UI wins, zero new concurrency risk: SPA views over endpoints that ALREADY exist (`/detect-drift`, `/review-queue` + `/resolve-review`) + a Curation Dashboard tile over `/graph/stats` + `/health`. *(2026-06-05 plan; now shipped)*
- **P2** — The job-queue spine: generalize `_ingest_queue.py`; reconcile + authority over new `/api/v1/reconcile` + `/api/v1/authority` with UI views; run reconcile apply on the daemon handle (removes daemon-down); route Browse/Graph through the equivalence fold. Off-graph only. *(2026-06-05 plan; now shipped)*
- **P3** — Rebuild / reembed / heal in-process via tmp-build + `writer_lock` swap (handle unavailable only at the swap instant; resolve the start-of-rebuild handle-close timing). UI trigger + progress from `rebuild-state.json`.
- **P4** — Continuous loop + debounced scheduler feeding the job queue; explicit per-item state machine (Pending/In-Progress/Approved/Rejected/Needs-Info + timeouts); fold cheap blocking into the `remember()` commit boundary.
- **P5** — Per-operation `CurationPolicy` (static) evaluated as the gate; Autonomy Config view; data-observability dashboard (freshness/volume/distribution/integrity/lineage).
- **P6 (optional, gated)** — collision-safe edge-id rebuild → true in-graph equivalence fold; revive curation MCP tools; (later) overturn-rate self-tuning, bi-temporal invalidate-not-delete, small-LM cost tier.

## Decisions locked this round (2026-06-05)

Build **P1 + P2**; equivalence **off-graph + UI-fold everywhere**; **static** per-operation autonomy policy; **HTTP + UI first**; keep the two review queues **separate** for now.

## Update 2026-06-05 — P3 heal mechanism (built)

The P3 topology-collapse heal is a **deterministic graph→fresh-graph canonicalizing copy (NO LLM)**, not a markdown re-extraction. It reuses the `kg reembed` copy scaffold: read the live graph, write a FRESH graph applying `AuthorityIndex.equivalence_map()` (drop variant nodes, remap edge endpoints to canonical, recompute content-addressed edge ids, dedup, drop self-loops, remap Claim `S_id`/`O_id` facets), then atomic-swap. It is fast (~3 min vs a ~4 h full re-extraction that was tried and rejected — re-running the LLM to apply already-known deterministic merges is the wrong tool), fresh-graph-safe (ADR-0007; append-only dedup, never the in-place delete-by-id upsert), and reversible (off-graph authority records persist; `kg rebuild` re-derives from markdown). **Reads stay up for the entire copy** — the runner holds the `writer_lock` (serializing ingest behind it, no lost-write race) but does NOT `mark_draining`; draining is scoped to the single no-await swap block, so no read ever observes a 503. Full markdown re-extraction remains `kg rebuild` (trust-root re-derivation), a separate slower path.

## Update 2026-06-08 — P5 lineage substrate is ADR 0013

ADR 0009 named data observability and lineage as part of P5. ADR 0013 now makes
that lineage substrate concrete: a durable pre-commit candidate ledger. Instead
of treating ingest logs as the durable curation record, extracted entities,
relations, and claims become vault-local candidate records; resolver/judge work
writes comparison records; the gate emits a commit plan; and the graph write is
the application of that plan. This keeps the existing curation control-plane
decision intact while tightening the ingestion-time boundary: no agentic semantic
write should land without an inspectable candidate and commit-plan lineage.

## Consequences / risks (from the adversarial critique)

- Rebuild closes the live handle at **start**, not just the swap instant (`cli/kg.py:108`) — P3 must redesign the in-process swap so the daemon serves until the last moment, and define the policy for `remember()` calls arriving mid-rebuild.
- The `full-auto`-never-for-irreversible rule must be a **compiled invariant**, not a `CurationPolicy` default.
- The background-execution model is a **load-bearing premise** grounded in the ADR-0007 + edge-id-collision incident, not an OS guarantee.
- ~10/12 admin endpoints are net-new — real backend scope, sequenced across P1–P5.
- Over-engineering guard: the 5-level taxonomy + observability tiles are heavy; P1 is the true minimal slice and ships value alone.
