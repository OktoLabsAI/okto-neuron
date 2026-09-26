# ADR 0039: Ingest Technical Correctness — Verified Commits, Honest Outcomes, and Resumable Extraction

- **Status:** Accepted
- **Date:** 2026-07-16
- **Deciders:** Marginalia maintainers
- **Builds on:** ADR 0007, ADR 0013, ADR 0015, ADR 0023, ADR 0025, ADR 0029,
  ADR 0034, ADR 0036, ADR 0037, ADR 0038
- **Relates to:** ADR 0005, ADR 0006, ADR 0016, ADR 0017, ADR 0024
- **Scope:** technical correctness of source processing, provider execution, ledgering,
  graph materialization, restart recovery, and diagnostics
- **Explicitly deferred to ADR 0040:** predicate policy, entity typing, canonicalization quality,
  and other conceptual/semantic decisions. Those are evaluated after this contract produces a
  trustworthy graph.
- **Implementation status:** Phase 1 code complete; live LOTR rebuild validation pending. The
  integrity vertical slice includes independent graph auditing, graph-generation identity, a
  durable writer fence across daemon and direct SDK writes, read-only/degraded status surfaces,
  per-source rebuild audits, close/reopen gates, generation-unique rollback artifacts, and
  post-swap verification. Integrity-sidecar contract version 2 invalidates cached verdicts from
  before physical-adjacency verification. Manual review `commit` writes now pass through the same
  fence; the former similarity-only `link` write is retired under ADR 0040. New ingest-run start
  rows also persist the structured configuration, extraction,
  and enclosing semantic-policy fingerprints defined with ADR 0040. Existing whole-run resume now
  requires exact extraction and full-policy equality; extraction-unit replay is still open.
  Commit-time plan integration and the later phases remain open.

---

## Purpose

Marginalia must be able to prove that a finished ingest faithfully materialized its durable
decision records, identify every source unit that did not finish, resume only unfinished work,
and refuse to describe a damaged graph as healthy.

This ADR is the implementation reference for that technical-correctness work. It deliberately
separates two questions:

1. **Did the system execute and persist its decisions correctly?** This ADR owns that question.
2. **Were those decisions semantically good?** ADR 0040 owns predicate vocabulary, entity identity,
   type stability, relationship usefulness, and domain-independent semantic quality. They cannot be
   judged reliably through a technically corrupted graph.

The target is not merely “the worker did not crash.” The target is an evidence-backed chain:

```text
source bytes
  -> deterministic source receipt
  -> extraction-unit records
  -> candidate ledger
  -> durable write-ahead commit plan
  -> idempotent plan application
  -> per-operation commit receipt
  -> graph-integrity verification
  -> honest terminal outcome
```

No layer may infer the success of the next layer from its own success.

---

## Durable implementation TODO

The design review and the model-free implementation slices are complete. What remains is live
proof, not model-free implementation: the deliberately bounded live-corpus acceptance run (which
uses the isolated Hobbit source rather than mutating or rebuilding the operator's LOTR vault),
corpus-scale restart/cancel/resume, retained Phase 7 acceptance evidence, and Phase 5 UI test
coverage. The items added in the 2026-07-20 status pass each name their implementing code path and
the test that fails if they regress; an item with only one of the two stays unchecked.

- [x] Preserve the current incident evidence and record the technical baseline (Phase 0).
- [x] Implement the integrity auditor, graph-generation marker, and durable writer fence (Phase 1).
- [x] Integrate graph generation and verification with the trust-root rebuild/install boundary.
- [x] Move the current aggregate `Companion.remember()` and manual-review commit plans ahead of
  their gate/queue/graph application. Plans now capture the pure intended decision, while terminal
  candidate rows and aggregate receipts remain post-application. This closes the known
  audit-after-write ordering defect without claiming the still-open executable-plan and
  per-operation-receipt work below.
- [x] Isolate the writer boundaries with the bounded model-free reproduction strategy (Phase 2).
- [x] Implement the executable write-ahead plan, writer inventory, read-before-write replay, and
  per-operation receipts (Phase 3).
- [x] Implement extraction-unit journaling, bounded retry, and honest outcomes (Phases 4–5).
- [x] Implement fresh staging rebuild, extensible technical/semantic pre-swap gates, and
  model-free crash/replay recovery (Phase 6 and the deterministic portion of Phase 7).
- [x] Hold an OS-enforced per-vault lease for every pooled graph-handle lifetime and standalone
  `kg rebuild`, `kg reembed`, and `kg reconcile heal` transaction. Final swaps re-check that
  ownership; managed in-daemon maintenance releases and fences its pool handle before claiming the
  same swap boundary.
- [x] Bind integrity verification to both graph generation and the open graph file's kernel
  identity. An atomic replacement beneath a stale handle stays writer-fenced and cannot be
  re-audited green from stale in-memory bytes.
- [x] Recover torn append tails only for reproducible extraction-unit/candidate/comparison evidence,
  using a top-level JSON `kind` scanner so nested source/model payloads cannot authorize truncation.
- [x] Normalize common structured-output transport envelopes (plain JSON, Markdown-fenced JSON,
  or one balanced JSON object surrounded by provider prose) before applying the unchanged strict
  relation-curator schema. A provider formatting quirk must not silently turn a valid grounded
  verdict into an `unparseable` abstention.
- [x] Separate temporary vault maintenance from process shutdown. Rebuild/reembed keep graph
  writers, ingest workers, and background curation paused for the complete job, while ordinary
  HTTP and MCP reads, queue/status inspection, and config reads continue against the old live
  generation. Only the brief final pool fence prevents new leases during the atomic swap, and
  maintenance responses never claim the server is shutting down.
- [x] Close the `kg reconcile heal` swap hole. Heal now runs under the same cross-process
  offline-swap lease as `kg rebuild`/`kg reembed` (`reconcile/heal.py`, `VaultHandleLease`),
  refuses to start on a fenced or unverified generation (`_require_unfenced_generation`), audits
  the durable staging bytes before the swap, re-audits the installed bytes after it, and publishes
  the resulting integrity state for the generation it minted. A staging-audit failure discards
  staging and leaves the live graph untouched; a post-swap failure leaves the installed generation
  fenced. Pinned by `tests/reconcile/test_heal_fold.py::test_heal_refuses_to_run_on_a_fenced_generation`,
  `::test_heal_refuses_swap_when_the_staging_audit_fails`,
  `::test_heal_post_swap_audit_failure_fences_the_new_generation`,
  `::test_heal_publishes_verified_state_for_the_generation_it_minted`, and
  `::test_heal_via_copy_refuses_real_cross_process_pool_handle`.
- [x] Route the SECOND heal site — the daemon runner (`server/_curation.py::run_heal`) — through
  the same fence. It reimplemented read → `copy_graph_canonicalizing` → swap inline and so ran
  with no fence check, no staging audit, and no generation publish, which left the sidecar naming
  the superseded generation after a daemon heal. Both sites now call the shared
  `store/integrity_state.py::require_unfenced_generation`, audit the durable staging bytes before
  the swap, and publish the integrity verdict for the generation the heal minted (the daemon does
  its post-swap audit inside the runtime fence, before any request observes the new graph). Pinned
  by `tests/server/test_curation_rebuild.py::test_run_heal_refuses_a_fenced_generation`,
  `::test_run_heal_audits_staging_before_the_swap`, and
  `::test_run_heal_publishes_the_new_generation_in_the_sidecar`.
- [x] Enforce T9 progress honesty end to end. `done <= total` is asserted rather than clamped: a
  violation emits a structured telemetry error (`server/_ingest_queue.py`), and a moved denominator
  is published as an explicit declared revision with its reason and comparison method
  (`consolidate/ledger.py`). Pinned by `tests/consolidate/test_ledger_telemetry.py` and
  `tests/server/test_ingest_queue_integrity.py`.
- [x] Complete the T6 store-integrity checks in `store/integrity.py`: node/edge/adjacency scans,
  live-endpoint and facet-reference validation, contract-gated deterministic edge-id reproduction,
  the `conflicting_deterministic_identity` check, and expected-artifact manifest verification
  projected from the ledger without importing ledger internals. Pinned by
  `tests/store/test_integrity.py`, `tests/store/test_ladybug_integrity.py`, and
  `tests/store/test_integrity_state.py`.
- [x] Add the `source_removed` planned operation to the executable write-ahead plan with strict
  validation (non-empty source id and reason, non-empty sorted-unique derived artifact ids) and a
  receipt that may only ride along with the retirement operations it explains
  (`consolidate/ledger.py`). Pinned by `tests/consolidate/test_executable_plan_contract.py`.
- [x] Guard the closed primitive set at the store write boundary
  (`store/closed_set.py::require_writable_node_type`), so no writer can mint a node type outside
  the locked 5 primitives + 6 support types. Pinned by `tests/store/test_closed_set.py`.
- [x] Publish the semantic-writer inventory (`docs/semantic-writer-inventory.md`) and enforce it as
  a Phase 3 exit guard: every `add_node`/`add_edge` call site under `src/marginalia` is classified
  planner-routed or non-semantic infrastructure, keyed by `(module, qualname, method)` rather than
  line numbers, and a new unclassified writer fails the build. Pinned by
  `tests/store/test_semantic_writer_inventory.py`.
- [x] Cover the crash matrix for plan/receipt durability — including the case where the artifact
  landed but its receipt never became durable — in
  `tests/consolidate/test_executable_plan_contract.py`.
- [ ] Run the instrumented isolated Hobbit rebuild with GLM 5.2 and retain the per-source audits,
  post-swap integrity state, ledger completeness, and recovery evidence before declaring Phase 1
  operationally complete.
  - Partially evidenced. The isolated Hobbit run in
    `docs/handoff-2026-07-20-adr0040-live-run.md` (see "Completed quality baseline: Hobbit")
    reached verified generation `8469a06c-7290-4c5b-b8d9-dabeece20e70` in vault
    `~/.marginalia/vaults/hobbit-quality-glm52` with 1,037 nodes, 794 Claims, 198 primitive
    entities, and 245 claim-backed topology relations, and its post-swap integrity state showed no
    dead endpoints, invalid Claim shapes, missing bridge/provenance/topology edges, or
    Claim/source-isolated entities. That establishes post-swap integrity for one corpus only. The
    run did **not** retain the per-source audits, ledger-completeness proof, or recovery evidence
    this item requires, and the handoff itself records that a one-source diagnostic is insufficient
    for acceptance. The item therefore stays open.
- [ ] Prove realistic live-model restart/cancel/resume behavior at corpus scale (Phase 7).
- [ ] Retain the Phase 7 acceptance evidence for the deterministic and live-model gates as a
  durable artifact rather than a session transcript.
- [ ] Cover the Phase 5 outcome/progress UI surfaces with tests; the honest-outcome contract is
  currently pinned only on the server side.
- [ ] Hand the verified graph generation to ADR 0040 for authoritative semantic evaluation.
  Blocked on the Hobbit item above.

The 2026-07-17 independent Fable review gave a go for the isolated Hobbit validation after its
release-blocking edge cases were closed: unsafe standalone graph swaps beside a live daemon, stale
handle audits that could verify a replaced generation, and torn non-write-ahead evidence rows that
previously wedged future ledger appends. The review's remaining findings are tracked as
post-baseline hardening work and do not relax any exit criterion.

ADR 0040 Phase 1a may measure pre-commit ledger/plan data in parallel. It must label that evidence
unverified and may not change semantic write behavior before this ADR establishes the required
technical boundary.

---

## Incident that triggered this plan

The July 15–16, 2026 LOTR vault ingested four sources overnight:

| Source | Blocks | Ledger state | Provider warning |
|---|---:|---|---|
| The Two Towers | 72 | completed | none |
| The Fellowship of the Ring | 91 | completed | none |
| The Return of the King | 97 | completed | one LiteLLM call timed out after 300 seconds |
| The Hobbit | 44 | completed | none |

Operational and provenance checks were positive:

- the queue drained to zero active items;
- all four ledger runs reached a terminal record;
- 304 processed blocks exactly matched 304 stored Blocks;
- the four source files totalled 3,612,084 bytes;
- one Claim sample per book reproduced the exact source byte-slice SHA-256;
- direct node lookup found all 21/21 named LOTR profile targets.

Those checks were not sufficient. The stored relationship topology did not match the durable
commit intent:

| Predicate | Planned accepted operations | Stored graph edges |
|---|---:|---:|
| `grows_on` | 2 | 899 |
| `judges` | 4 | 912 |
| `shows_vision_of` | 4 | 901 |
| `older_name_for` | 1 | 226 |
| `seekers_of` | 1 | 226 |

For example, 837 stored `grows_on` edges connected unrelated Claim nodes to the
`The Two Towers` InformationObject. Extraction never proposes Claim nodes as semantic relation
endpoints, so this is not ordinary model noise. The durable plan contained two legitimate
`grows_on` operations; the graph contained hundreds of unrelated edges bearing that type.
The remaining excess `grows_on` edges and the repeated excess-count pattern across predicates are
unclassified incident evidence; Phase 0 preserves and labels them rather than treating the 837-edge
sample as the whole corruption mode.

The Phase 1 auditor subsequently scanned the complete live graph: 11,227 nodes, 44,046 stored
edges, and 44,046 physical adjacencies. It found 35,916 independent
`adjacency_property_mismatch` findings in 2.81 seconds, with no incomplete population. The graph's
legacy generation is `null`; its durable state is `failed` and writer-fenced. The full audit and
sidecar were preserved under the vault-local incident bundle
`.marginalia/incidents/2026-07-16-integrity-failure/` before rebuild work began.

A later diagnostic on an isolated representative multi-document vault copy found a second
authority-boundary defect: its version-1 sidecar still said `verified`, although a fresh complete
physical-adjacency audit found 218 `adjacency_property_mismatch` issues on generation
`e6e3d0d7-3df5-403e-80d5-b9b90812d6b4`. The sidecar contract is therefore versioned at 2; older
green state loads as `unverified` and must be re-audited. The semantic-quality endpoint performs
that audit through a loopback-only POST under the same writer lock and the store's shared
audit/write lock immediately before any topology-dependent measurement. It refuses to measure
topology when verification fails.

The writer inventory review also found that the historical manual review-queue actions `commit`
and `link` wrote through `ConsolidationSession` without the live graph guard. `commit` now owns the
live graph guard. The similarity-only `link` action is retired because it inferred a generic
`relates_to` edge without predicate admission, direction validation, relation grounding, or an
anchored Claim. Public review operations are therefore `commit`, `discard`, and `merge`; only
`commit` writes the graph. Legacy `review_link` plans remain schema-readable for audit, but are not
executable. An untouched legacy plan can be durably abandoned before a supported replacement
action is sealed, while a plan with any receipt or matching graph artifact fails closed for
explicit recovery. `discard` and `merge` remain off-graph and do not invalidate the integrity
sidecar. Queue load, resolution, post-write audit, and acknowledgement are serialized by the
store's shared integrity lock; a graph-writing resolution refuses to run inside another write
guard because it must own the audit boundary. A graph-writing review item is acknowledged only
after its post-write audit succeeds and only when the requested operation actually completed.
Failed audits and dead-lettered candidates remain available for recovery while a failed generation
is fenced. The queue file itself is written by
same-directory temporary file, file `fsync`, atomic replace, and directory `fsync`, so a failed
replacement preserves the previous durable queue and its in-memory projection.

The run also exposed three execution-reporting gaps:

- a per-block provider timeout produced partial extraction but the run and queue still presented
  a normal completed/done state;
- the current counters retain a provider-failure count, but the normal ledger finish summary does
  not retain the failed Block ids needed for targeted retry;
- the quality guardrail passed despite topology corruption and falsely reported `Mithril` missing
  because domain coverage was calculated from a truncated graph overview.

### What is confirmed

- Source bytes, block anchors, and sampled Claim provenance are intact.
- The durable commit plan and stored topology diverge after curation.
- A partial provider failure is currently represented too optimistically.
- Existing quality checks do not prove graph-write fidelity.
- Progress can report `done > total`; one live relation-curator sample reached 109.8%.

### What remains unknown

The exact trigger of the topology corruption is not yet isolated. The shape resembles the
Ladybug stored-property/adjacency corruption documented by ADR 0007, but ADR 0007 also proved a
bounded incremental-ingest sample safe. The LOTR evidence therefore reopens the boundary without
overwriting the earlier evidence. Possibilities that must be distinguished experimentally include:

- reuse of a graph that was not genuinely fresh after a reset;
- scale or storage-layout sensitivity during a large semantic commit;
- a particular write phase: structural materialization, topology commit, Claim provenance,
  source mentions, or correction/supersedence;
- restart or lifecycle interaction;
- an application defect that mutates the wrong edge payload;
- an engine defect under a workload not covered by ADR 0007's incremental test.

This ADR does not select a storage fix before that reproduction identifies the owning boundary.

---

## Current architectural gaps

### 1. The commit plan is audit-after-write, not write-ahead execution

ADR 0013 requires graph writes to come from an explicit commit plan. The current sequence is
weaker:

1. `gate()` invokes `ConsolidationSession.commit()` and writes nodes/topology edges;
2. `Companion.remember()` derives and appends a `CommitPlan` from the in-memory decisions;
3. `_mint_relationship_claims()` consumes the in-memory edge candidates and writes Claims plus
   provenance edges;
4. one aggregate `CommitRecord` is appended.

The plan records intended decisions, but it is not the executable input to the writer. The
aggregate receipt reports counts and ids, not one durable outcome per planned operation. After a
crash or storage mutation, the system cannot prove exactly which planned artifacts landed.

### 2. “Transaction” currently means staged loops, not durable atomicity

`ConsolidationSession.commit()` validates and writes candidates sequentially through the store.
It has no durable write-ahead checkpoint, per-operation receipt, or rollback. A mid-loop failure
can leave a partially applied file commit. The writer lock prevents concurrent application
writers; it does not make a sequence of database calls atomic or resumable.

### 3. Topology edge identity is not uniformly reproducible

Claim provenance and bridge edges use content-addressed ids. Semantic topology edges currently
use generated ids, even though their semantic identity is `(src, predicate, dst)`. That makes
plan-to-store reconciliation and idempotent replay harder than necessary.

### 4. Extraction recovery starts too late

The ledger can resume streamed curation verdicts, but extraction results are folded in memory
before candidate ledgering. With parallel extraction, a later chunk may have completed at the
provider but still be lost if the process stops while an earlier source-ordered future is pending.
Restart then pays for completed model work again.

### 5. Lifecycle and result quality are conflated

The queue's `done` status means the worker stopped, but the UI reads it as a healthy result. A
partial provider failure is stored as `done + provider_error`; the distinction exists only for a
client that knows that convention. The ledger has similar `completed` semantics.

---

## Correctness model

Technical correctness has five independently verifiable layers:

1. **Source correctness:** the exact source bytes, source identity, chunking policy, Block spans,
   and content hashes are durable.
2. **Execution correctness:** every scheduled extraction unit has a durable terminal outcome and
   the run uses one pinned semantic configuration fingerprint.
3. **Decision correctness:** every candidate and comparison reaches an explicit plan outcome.
4. **Materialization correctness:** every planned graph artifact has a matching verified receipt,
   and the store has no topology/property/id inconsistency.
5. **Reporting correctness:** API/UI terminal labels, progress, health, and quality gates reflect
   the preceding layers without hiding partial work or incomplete reads.

A layer may be healthy while a later layer is not. The API and UI must show that distinction.

---

## Non-negotiable invariants

### T1. Source anchoring

- Every extracted unit identifies one current Block and its `(source path, byte_start, byte_end,
  content_hash)`.
- The effective chunk size and overlap used by the run are pinned in its configuration
  fingerprint.
- A Block hash mismatch aborts that unit before any provider call or graph write and records the
  terminal unit outcome `source_changed`.

### T2. Complete extraction-unit accounting

- Every scheduled extraction unit, including an empty or stale Block, has exactly one current unit
  outcome: `succeeded`, `provider_failed`, `invalid_output`, `empty_after_retry`, `source_changed`,
  `cancelled`, or `intentionally_skipped`.
- Every attempt records unit id, attempt number, timestamps/duration, normalized provider error
  class, and retry disposition.
- A run cannot have result quality `complete` while any unit is unresolved or failed.

### T3. Write-ahead intent

- The atomic plan unit is one file-level consolidation commit. It does not wait for later files in
  the same ingest batch to finish extraction.
- The complete semantic commit plan is checksummed, flushed, and `fsync`ed before its first semantic
  graph mutation. A torn or unreadable trailing plan record is an integrity failure, never proof of
  absent intent.
- The applier re-reads the durable plan bytes, or verifies their checksum against the in-memory
  object, before writing; it does not consume a parallel reconstruction.
- Every plan operation has a stable `operation_id` and exact expected artifact ids.

### T4. Idempotent application

- Reapplying a verified operation is a no-op established by reading the store for the expected
  artifact. Replay never rewrites an artifact already present merely because its receipt is
  missing.
- Reapplying an interrupted plan completes only read-verified missing operations.
- Semantic topology edge identity is deterministic from `(src, plan-recorded predicate, dst)` for
  new graphs. The predicate is canonical or provisional according to the semantic-policy snapshot
  used by the planner. The applier and replay path never consult a later predicate registry.
- Repeated assertions of the same triple corroborate the existing semantic edge/Claim identity;
  they do not mint a second topology edge. Retraction/supersedence is state on that assertion, and a
  later reassertion reactivates it with new provenance rather than colliding with a tombstone.
- Existing graphs receive this identity contract only through a fresh rebuild, never an in-place id
  migration.

### T5. Per-operation receipts

- Every attempted planned operation ends as `applied`, `already_present`, `dead_lettered`, or
  `failed`, with a structured reason and observed artifact ids. An operation prevented by
  cancellation, a writer fence, or a prior operation failure ends as `aborted` with that cause.
- `queued` is explicitly non-terminal. A run cannot be `complete` while an operation remains queued
  or failed, and its outcome discloses all dead-lettered and aborted counts.
- Aggregate counts are derived from receipts; they are not the primary proof.
- One candidate or operation cannot silently disappear between plan and receipt.

### T6. Store integrity

For Ladybug, a verified graph has all of the following:

- topological adjacency `(actual source node, actual destination node)` agrees with stored
  `e.src/e.dst` properties;
- every edge property references live nodes;
- content-addressed edge ids reproduce for provenance, bridge, source-mention, supersedence, and
  new deterministic topology edges;
- the set of expected artifacts in a completed plan is present with matching type and endpoints;
- no two stored edges claim the same deterministic semantic identity with conflicting ids.

Each graph stores an explicit identity-contract version and generation id. The auditor applies
deterministic-id checks only when that contract version declares them. Its store-facing API accepts
an expected-artifact manifest projected from the ledger; the store module does not import ledger
internals.

### T7. Integrity is a writer fence

- The audit-status vocabulary is fixed: `unverified` (no complete passing audit for this
  generation), `verifying`, `verified`, `failed` (a mismatch was proven), and `incomplete` (the
  complete population could not be checked). Every new or actively written generation is
  `unverified` until its required audit completes; only `verified` is an authoritative technical
  baseline. `failed` and `incomplete` both prevent semantic writes until a later complete audit or
  verified rebuild resolves the state.
- Integrity is checked before accepting semantic writes on startup.
- In the correctness-first rollout, a full topology audit runs after every file-level semantic
  commit because ADR 0007 proved that a bad operation may corrupt edges it did not directly touch.
- Any mismatch trips a per-vault writer fence. Later queue items do not continue writing into a
  damaged graph.
- The fence is a durable per-vault marker. It survives restart and is cleared only by a verified
  rebuild swap or an explicit operator action recorded in the ledger.
- The graph remains available for explicit read-only diagnosis, with degraded health attached to
  REST, MCP, CLI, SDK, and UI graph/read responses. It is never silently repaired in place.
- The integrity auditor composes with the existing Ladybug schema-health check, vault maintenance
  fence, and degraded-health channel; it does not create competing definitions of `healthy`.
- Full post-file audits remain mandatory until Phase 2 fixes the owning boundary and Phase 7 records
  a configured number of clean LOTR-scale runs. A later optimization may use a touched-generation
  audit plus periodic full sweep only with equivalent measured coverage and a recorded audit budget.

### T8. Honest outcomes

Lifecycle and result quality are separate axes:

| Axis | Values |
|---|---|
| Worker lifecycle | `queued`, `processing`, `done`, `error`, `cancelled` |
| Result quality | `complete`, `partial`, `failed`, `integrity_failed`, `empty`, `no_units`, `not_applicable` |

`done + partial` is rendered as **Completed with warnings**, never as an ordinary success.
`done + integrity_failed` is not allowed; an integrity failure is an error terminal and fences the
vault. Existing clients can continue reading `status`; the additive `outcome` object carries the
technical truth.

Unit outcomes map normatively to result quality:

- all required units `succeeded` or were intentionally skipped for a recorded policy reason →
  `complete`;
- at least one useful unit succeeded but another required unit is unresolved, provider-failed,
  invalid, empty-after-retry, source-changed, or cancelled → `partial`;
- no required unit produced usable output and at least one is provider-failed, invalid, or
  source-changed → `failed`;
- no required unit produced usable output, and the ONLY reason is `empty_after_retry` (zero
  provider-failed, zero invalid, zero source-changed) → `empty` — see the 2026-09-14 addendum;
- any verified graph mismatch → `integrity_failed`.

The outcome reserves a separate optional `semantic` axis populated by ADR 0040. Technical quality
must never be overwritten by a semantic verdict.

### T9. Progress is bounded and sourced once

- Every phase declares the population its denominator represents.
- `done <= total` is asserted. A violation is a telemetry error, not a percentage to clamp and
  hide.
- Dynamic populations publish an explicit revised total and reason.
- UI percentages are derived from the same server record the ledger summary exposes.

### T10. Complete diagnostics

- A quality check must paginate to a complete population or report `unknown/incomplete`; it may
  not pass a full-graph assertion from a truncated response.
- Domain-profile presence, duplicate counts, and integrity counts operate on complete datasets.
- The guardrail remains model-free after ingest and never needs another paid model call to verify
  technical correctness.

### T11. Configuration and writer ownership

- Fingerprints are structured records with two layers. The `extraction_fingerprint` contains
  provider, model, extraction prompt, extraction mode, chunk size/overlap, draw policy, semantic
  packs that affect extraction, and primitive-guide version. Unit reuse requires exact equality of
  this layer.
- The enclosing `semantic_policy_fingerprint`, completed by ADR 0040, adds normalization,
  identity-resolution, predicate-registry, relation-gate, and semantic-threshold versions. Reusing
  downstream decisions or plans requires exact equality of the full layer.
- Concurrency and embedding batch limits remain live execution policy per ADR 0036/0037 and do not
  alter the extraction fingerprint. Curation batch size belongs to the enclosing semantic policy
  because grouping changes the candidate context presented for adjudication.
- The implemented run-start fingerprint builder also excludes credentials, provider timeouts, and
  retry controls. It includes effective provider/model and semantic parameters, prompt and pack
  hashes, chunk size/overlap, extraction modes/caps, superseded-item audit policy, and versioned
  primitive/identity contracts.
  Whole-run resume compares both extraction and semantic-policy fingerprints before replaying
  curator verdicts. The later unit-ledger slice must independently compare the stored extraction
  fingerprint before reusing raw unit work.
- Exactly one semantic applier holds the per-vault writer lease. Parallelism ends at provider
  calls; graph application stays ordered.

---

## Decision

### Immutable edge identity at the Ladybug boundary

An edge id permanently identifies its `(type, src, dst)` tuple. Reusing an id
with a different tuple is an identity collision and fails before any write.
Changes to mutable edge payload (`weight` and `provenance`) update the existing
relationship in place. The store never implements an edge update as
`DELETE` followed by `CREATE`. Marginalia requires Ladybug 0.18.2 or newer in
the 0.18 line because LadybugDB issue #658 could checkpoint partial relationship
string-property scans with values detached from their physical endpoints, even
when the open handle audited clean immediately before close. The upstream fix
landed in PR #659; Marginalia retains its own close/reopen audit as the
application-level fail-closed boundary.

### D1. Add one reusable graph-integrity auditor

Create a read-only integrity service owned by the store boundary, not a one-off LOTR script. It
returns a typed summary with counts and bounded samples for:

- adjacency versus stored-property mismatches;
- missing endpoints;
- content-addressed id mismatches;
- duplicate deterministic identities;
- plan receipt versus stored-artifact mismatches;
- audit completeness, duration, graph generation, and edge population scanned.

Ladybug's implementation must query both actual relationship adjacency and stored edge
properties. A scan through `list_edges()` alone cannot prove that they agree. Memory/test stores
may use a generic implementation.

The service is consumed by:

- startup health and the writer fence;
- the commit applier's post-file verification;
- a read-only HTTP diagnostic endpoint;
- `scripts/ingest_quality_check.py`;
- `kg rebuild` before atomic swap.

There is one implementation of the invariant and several projections, not several independent
versions of “healthy.”

Phase 1 must first demonstrate a Ladybug primitive that reads relationship adjacency independently
from stored `e.src/e.dst` properties; bidirectional endpoint traversal is the fallback. The auditor
extends the existing schema-health check and degraded-health channel, while the vault-pool
maintenance fence remains the one runtime fencing mechanism. Ledger code projects plan/receipt
data into a plain expected-artifact manifest supplied to the auditor; the store does not import
ledger internals.

### D2. Make `CommitPlan` executable and write-ahead

Refactor the curation boundary into three explicit parts:

1. **Planner:** pure decisions; emits exact operations and expected artifact ids.
2. **Applier:** the only semantic graph writer; reads the durable plan and applies operations in
   deterministic order.
3. **Verifier:** reads receipts plus store state and closes the plan only after integrity passes.

`gate()` stops writing. It produces gate decisions for the planner. The existing
`ConsolidationSession` may remain as an applier implementation detail, but its public contract can
no longer imply an atomic transaction it does not provide.

Replace ambiguous `create_edge_or_claim` plan entries with explicit intent:

- `create_node`;
- `create_topology_edge`;
- `mint_claim` with expected Claim id;
- `attach_claim_provenance` with expected bridge/provenance edge ids;
- `ensure_source_mention`;
- `merge_existing`;
- `detach_edge` / `retract_claim`;
- `update_node_state` for semantic facets, supersedence, or resurrection;
- `queue_review`;
- `dead_letter`;
- `supersede`.

The planner can group related operations for display, but the applier and receipt remain exact.
Every operation carries the structured gate reason that produced it.

Source removal is one such group, not an escape hatch around the operation model. Deleting a
source compiles into exact detach/retract/state operations and closes with a `source_removed`
receipt containing the source id, the planned derived-artifact ids, and the observed survivor
count. The receipt cannot be terminal-success while any artifact derived only from that source
remains live. This makes the markdown trust-root deletion contract provable rather than inferred
from a successful API response.

#### Deferred operations (implementation note, 2026-07-28)

`source_removed` is implemented: the plan schema carries `source_id`, the sorted-unique
`derived_artifact_ids`, and the gate reason, and the applier's closing limb read-verifies every
planned artifact and refuses the plan (it raises; it never records a failure receipt that a replay
could skip) while any of them is still live. Liveness follows the ADR 0024 memory-accretes facets
`_detached` / `_superseded`, because a retired artifact is deliberately kept in the graph.

Because the receipt closes a *group* and is not an escape hatch, a plan carrying `source_removed`
must also carry the retirement operations that did the work: the plan-set validator rejects a bare
`source_removed` with no `update_node_state` or `supersede` alongside it. Requiring per-artifact-id
coverage (every `derived_artifact_ids` entry named by a retirement operation in the same plan) is
deferred: an artifact retired by an earlier run is legitimately absent from this plan, and that
case needs a run-spanning check the ledger does not offer yet.

The three remaining names in the list above are deliberately deferred, and the code and this ADR
agree on why:

- `retract_claim` — already expressed. Claim retraction is an `update_node_state` operation whose
  `reason` is `claim_detached`; that is exactly what the applier's `lifecycle_counts`
  (`claims_detached`) counts today. A second discriminator for the same intent would give one
  operation two identities.
- `merge_existing` — already expressed. Merge intent is carried by `review_merge`, which is in
  `_PLAN_OPERATION_FIELDS` and applied by the manual-review limb.
- `detach_edge` — no caller. Topology edges are never removed by any path in `src/marginalia/`
  today; detachment is stamped on Claim nodes, not on edges. It is added when a writer needs it.

There is no source-deletion (whole-file removal) handler in the codebase at all; the only removal
path is the sub-chunk `detach_orphan_removals` pass, which already routes through planned
`update_node_state` plus `append_detachment_annotation` operations. Separately, the heal — both sites, `kg reconcile heal`
(`src/marginalia/reconcile/heal.py`) and the daemon runner
(`src/marginalia/server/_curation.py::run_heal`) — is now routed through the shared integrity
fence, staging audit, and generation publish, so neither can copy a fenced generation or leave a
stale sidecar. Routing the heal through the *planner* operation set is a separate, still-open
item and remains out of scope here.

Before implementation, Phase 3 produces a repository-wide inventory of every semantic
`add_node`/`add_edge`/detach/state-update call site. Each writer is either routed through the
planner or explicitly classified as non-semantic infrastructure (for example, a vector-only
update) with a safety argument. “Only semantic writer” applies to that enumerated boundary, not
merely the current `ConsolidationSession` path.

### D3. Use logical atomicity through replay, not an unproven database transaction

The first implementation does not pretend Ladybug offers a file-level atomic transaction.
Instead:

- plan-before-write makes intent recoverable;
- deterministic operation/artifact ids make replay idempotent;
- replay read-verifies the expected artifact before issuing any write, so a lost receipt cannot
  cause Ladybug's destructive edge upsert to run again;
- receipts make partial application visible;
- restart resumes the unfinished plan under the writer lease;
- post-apply full integrity verification is required before the result becomes `complete`.

Readers may observe an active plan, as they already may observe committed graph lag during a
long ingest. Read surfaces must label the active/unverified generation. Snapshot isolation through
copy-and-swap is a fallback decision only if reproduction proves that verified in-place
application cannot be made safe.

The applier interface is storage-strategy-agnostic: Phase 2 may select verified in-place
application or staging/copy-and-swap without changing the durable plan contract.

### D4. Journal extraction units before document-level folding

Extend the append-only candidate ledger with lightweight extraction-unit records. A unit id is
derived from `(Block id, extracted span content hash, extraction fingerprint)`, so ADR 0024 hunk
extraction cannot collide with a full-Block unit. A successful unit record contains normalized
extracted candidates and anomaly flags, but no embeddings and no duplicate source text.

With parallel extraction:

- provider calls may finish out of order;
- the coordinator persists completed unit records as soon as it observes them;
- document-level folding still consumes successful units in source order;
- restart reuses successful units only when Block/span identity and extraction fingerprint match;
- failed/cancelled units are the only units resubmitted.

This extends ADR 0015's curation resume backward to the expensive extraction phase.
ADR 0023/0024 source-diff decisions run first and are recorded as explicit
`intentionally_skipped` or scheduled unit outcomes; the journal does not create a second competing
skip mechanism. ADR 0030 multi-draw calls are sub-attempts of one unit, and draw policy belongs in
the extraction fingerprint.

### D5. Make retry bounded, classified, and unit-scoped

LiteLLM Python remains the universal provider adapter. Marginalia classifies LiteLLM exceptions
into normalized technical categories without provider-specific request code:

- timeout;
- rate limited;
- authentication/authorization;
- unavailable/connection;
- invalid request/model;
- cancelled;
- malformed/unparseable provider output;
- unknown provider error.

Timeout ownership follows the provider boundary. A named provider connection may set a positive
completion deadline; when it is unset, Marginalia adds no request or helper-process deadline and
delegates transport policy to LiteLLM and the provider. Explicit Stop/shutdown still terminates the
owned helper process immediately. The curation pool's optional, explicitly configured call
watchdog is a separate task policy: it does not mutate the provider connection or affect extraction
and ask calls, but it must be propagated as a scoped deadline to that curation call's owned LiteLLM
helper. Otherwise the scheduler can report a timeout while the underlying request remains alive.
Batch calls and their single-candidate fallbacks share one total concurrency budget, and batches
are admitted through a bounded rolling window so a timed-out prefix cannot leave orphan requests
or double effective concurrency. This corrects the hidden 300-second extraction deadline observed
on *The Return of the King* without introducing an arbitrarily larger global replacement, while
still making an operator-selected curation watchdog real rather than cosmetic.

The first policy is deliberately small:

- transient provider failures receive at most one automatic retry (`2` total attempts), respecting
  `Retry-After` when supplied and cancellation immediately;
- permanent errors do not auto-retry;
- every retry is visible in the unit journal;
- an operator retry reuses successful unit records and reruns only unresolved units;
- a changed extraction fingerprint starts a new extraction run; a downstream semantic-policy
  change may reuse raw unit output but recomputes incompatible decisions and plans.

No infinite retry loop and no silent singleton fallback are permitted.

**D5 clarification (2026-07-29 review pass, plan-10 item (e)):** the equality in
`CandidateLedger.find_resumable_run` (`consolidate/ledger.py`) deliberately checks
`document_id`, `blocks_total`, `model`, `extraction_fingerprint`, **and**
`semantic_policy_fingerprint` — a policy change correctly starts a new run rather than resuming
the old one, because resume replays curator/relation-curator verdicts, which are
policy-dependent. This is intentional, not a gap. The clause above is satisfied one layer down:
unit reuse (`extraction_unit_id`, `successful_extraction_units`) is keyed only on extraction
identity (`block_id`, byte range, `content_hash`, `extraction_fingerprint`), with no
semantic-policy term, so the new run still reuses every durable successful extraction unit and
pays no re-extraction provider cost. Reusing curator verdicts *across* a policy change (i.e.
whole-run resume with an incompatible policy) is out of scope here; it is a distinct
verdict-level policy-compatibility design question, not a D5 defect, and is not currently
planned. See `tests/consolidate/test_semantic_policy_change_reuses_extraction_units.py` for the
regression pin.

### D6. Separate lifecycle from outcome everywhere

Add one `outcome` object to ledger summaries, queue items, API responses, Logs, and Config/Test
feedback. It contains:

- quality state;
- attempted/succeeded/failed/empty/skipped unit counts;
- failed unit ids and bounded source-span summaries;
- plan id, receipt status, and integrity audit id;
- provider error categories and retryability;
- graph generation/health.
- optional semantic-evaluation state supplied by ADR 0040.

The existing `provider_failures` and `empty_after_retry_blocks` counters are retained and folded
into this object. The normal finish path must persist them; today only part of that evidence
survives in the ledger summary.

The D6 queue projection stores `RememberResult.outcome` on each durable `IngestItem` and exposes it
unchanged through queue snapshots and item detail. Legacy sidecars without the additive field load
with an unknown outcome. Retrying an item clears the prior terminal outcome before work resumes.
Lifecycle compatibility remains explicit: `done + partial` stays `done`, while `failed` and
`integrity_failed` results use the existing `error` terminal. Deterministic `/add` stores report
`not_applicable`. REST `remember`/`ingest` and MCP `remember` return the same additive object. The
ingest and Logs views render quality, failed-unit counts and error classes, plus retry only when
the structured unit evidence or the legacy provider-error contract permits it. An unclassified
internal exception is also explicitly operator-retryable: it records `error_class=internal` and
`retryable=true`, while older `{quality: failed}` rows without unit classification retain that same
manual recovery path. This does not create an automatic retry loop. Classified authentication and
integrity failures with non-retryable/empty `failed_units` remain blocked. Operators do not need to
inspect raw event JSON to distinguish these outcomes.

The batch-ingest response also returns the exact additive `enqueued_item_ids` alongside
`enqueued`. Automation must supervise only those identities, not the queue-wide totals: a reused
vault can legitimately retain historical errors or unrelated active work. Evaluation clients that
intend to inspect an existing graph use an explicit no-ingest mode, require a populated endpoint,
and skip queue draining entirely because they own no ingest work. Transport failure is never
coerced to an empty queue or graph.

Queue snapshots expose fine-grained progress in the natural unit of the active stage. The existing
Block numerator/denominator remains the extraction measure; embedding reports candidates, entity
curation reports reviewed entities, and relationship curation reports reviewed relations. These
stage fields reset on a stage transition or retry and are projected directly from the same durable
progress events used by the ledger. The UI therefore never presents `44/44 chunks` as the only
signal while a long relationship-curation phase is still running.

### D7. Fence and recover; never clean topology in place

When integrity fails:

1. stop subsequent semantic writes for that vault;
2. persist the integrity report and failing plan/receipt references;
3. mark health degraded and the active item `error + integrity_failed`;
4. preserve the source files, ledger, corrupt graph, and existing safety backup;
5. diagnose on a copy;
6. fix the owning boundary;
7. rebuild from the trust root into a fresh graph;
8. run the full integrity audit before atomic swap;
9. retain the old graph as the bounded rollback artifact.

There is no in-place “repair the bad edges” path. This preserves ADR 0007.
The atomic-swap gate is extensible: the ADR 0039 integrity audit is always required, and registered
semantic acceptance gates from ADR 0040 become additional requirements once that ADR is accepted.

### D8. Make guardrails completeness-aware

`scripts/ingest_quality_check.py` must use paginated node APIs or a complete aggregate endpoint
for domain profiles and duplicates. Its result records the inspected population and fails a
required check when the source is truncated or unavailable.

Technical guardrails add thresholds that default to strict correctness:

- integrity audit complete;
- zero adjacency/property mismatches;
- zero content-addressed id mismatches;
- zero missing expected plan artifacts;
- zero unreceipted plan operations;
- zero unresolved extraction units for a `complete` result;
- progress denominators valid.

Semantic thresholds remain separate and do not block implementation of this ADR.

---

## Root-cause isolation matrix

The storage fix is chosen only after this matrix identifies the first phase that breaks an
invariant. Each cell runs the same read-only full audit after source materialization, topology
application, Claim/provenance application, source mentions, corrections, clean close, and reopen.

| Dimension | Required cases |
|---|---|
| Initial state | genuinely empty graph; prior clean graph; reset/reused vault |
| Source count | one file; two sequential files; four-file LOTR order |
| Workload size | tiny deterministic fixture; medium synthetic graph; LOTR-scale candidate plan |
| Per-commit edge-mint batch | bounded ADR 0007-safe sample; representative file; LOTR-scale file |
| Extraction concurrency | 1; representative parallel value; maximum supported bound |
| Embedding execution | sequential batch; parallel batches |
| Lifecycle | uninterrupted; graceful restart between files; cancellation; forced process loss |
| Write phase | structural only; nodes; topology; Claims/provenance; source mentions; corrections |
| Store lifecycle | audit before close; after close/reopen; after daemon restart |

The matrix is not a full Cartesian product. Start from one baseline matching the LOTR incident,
vary one dimension at a time, then expand pairwise only around the first dimension that fails. All
LOTR-scale storage experiments replay preserved candidate/commit-plan evidence and make no provider
calls. The first-priority hypothesis is Ladybug's documented destructive edge upsert interacting
with a large per-file mint or `ensure_source_mentions` batch on a populated graph.

The experiment records the exact first failing operation range. If the application payload is
wrong before `add_edge`, fix the application owner. If the payload is correct but storage changes
it, produce a minimal upstream Ladybug reproduction and choose between a proven engine update and
a Marginalia staging/swap containment. At that point Phase 2 also records a costed
`GraphStore`-engine replacement option and the evidence threshold that would select it if an
upstream repair is unavailable or containment would leave Marginalia permanently owning unsafe
engine behavior. No alternate write syntax is accepted without evidence; ADR 0007 already showed
that syntax changes alone did not cure its incident.
If the bounded matrix neither reproduces the failure nor confirms another owner, Phase 2 records
that negative result and selects fresh staging/copy-and-swap as the containment strategy; it does
not silently declare in-place application proven safe.

---

## Implementation plan and gates

Each phase is independently reviewable. A later phase does not start merely because the previous
code compiled; it starts when the listed evidence exists.

### Phase 0 — Preserve evidence and freeze destructive action

**Work**

- Treat the current LOTR graph as read-only incident evidence.
- Record source hashes, ledger run ids, queue outcomes, configuration fingerprint, and graph file
  hashes in a local/private incident bundle.
- Do not reset, delete, migrate, or rebuild the vault until the reproduction is available.

**Gate**

- Evidence is sufficient to reproduce planned-versus-stored mismatches without relying on the
  mutable UI.

### Phase 1 — Integrity auditor and guardrail integration

**Likely owners**

- `src/marginalia/store/integrity.py` (new, focused module)
- `src/marginalia/store/ladybug.py`
- `src/marginalia/server/http.py`
- `scripts/ingest_quality_check.py`
- focused store/server/script tests

**Work**

- Implement the typed complete audit and bounded samples.
- Add startup/read-only endpoint/quality-script projections.
- Correct domain-profile pagination and progress invariant reporting.
- Add a writer-fence state to the per-vault runtime, but do not change the commit pipeline yet.
- Persist the graph generation, identity-contract version, and fence marker.

**Gate**

- The auditor detects an intentionally corrupted fixture and reports a clean fresh fixture as
  clean.
- The LOTR vault fails the integrity gate for the observed mismatch class.
- The domain profile returns actual 21/21 without reading a truncated overview.
- Ladybug adjacency can be read independently of stored edge endpoint properties, directly or by
  the documented bidirectional-traversal fallback.

**Implemented containment**

- Every trust-root rebuild mints a fresh graph generation and records its identity-contract
  version and embedding width.
- The complete staging graph is audited after every source, again after a clean close/read-only
  reopen, and again after the installed graph is reopened.
- A provider-failed source, failed audit, or incomplete scan makes the candidate unswappable. The
  staging graph and bounded audit samples are retained under
  `.marginalia/rebuild-artifacts/<generation>/`.
- A successful candidate moves the previous live graph and its Ladybug sidecars to a
  generation-unique `previous-graph.lbug*` family. The previous integrity sidecar is snapshotted
  alongside it. The new generation is marked `verifying` before the post-swap audit and becomes
  writable only after `verified` is durably published.
- A post-swap failure leaves the new graph writer-fenced and retains the previous generation for
  explicit operator rollback. `heal` cannot bypass a failed-generation fence because copying a
  corrupt graph could normalize bad stored endpoints and launder the incident: it refuses to
  start when the live generation is `failed` or `incomplete`. `heal` mints its own generation for
  the copy, audits the closed staging bytes on a read-only reopen before any swap, and after the
  swap marks that generation `verifying` and publishes the post-swap verdict — so a healed graph
  is gated and attested exactly like a rebuilt one.
- Daemon entrypoints and direct `Vault.add`/`Companion.remember` writes consult the same durable
  generation-scoped state, mark the generation unverified during a semantic write, and run the
  complete audit on exit. Rebuild staging is separately audited and never inherits the live
  graph's verdict.
- The runtime exposes maintenance draining independently from permanent process shutdown. Slow
  staging work therefore leaves the old generation readable; write APIs return an explicit,
  retryable `maintenance` response, while actual shutdown remains a distinct fail-closed state.

**Known first-cut limits**

- Moving a Ladybug main file plus sidecars is not a filesystem-wide transaction. Ordinary move
  failures roll back already-moved members, but process loss between renames can still leave a
  partial family requiring operator recovery from the generation artifact.
- Rebuild artifacts are intentionally not auto-pruned while the corruption incident is open.
  Retention policy and size disclosure remain operational follow-up work.
- A daemon post-swap validation failure leaves the maintenance pool fenced for the process
  lifetime; restart reopens the failed generation for degraded read-only diagnosis while the
  durable semantic-writer fence remains in force.

### Phase 2 — Reproduce and pin the corrupting boundary

**Likely owners**

- Ladybug integration tests and a repository-local diagnostic driver
- no production workaround until the first failing phase is proven

**Work**

- Execute the isolation matrix on copied/synthetic vaults.
- Compare plan payload, call-time `Edge`, post-call stored properties, actual adjacency, clean
  close, and reopen.
- Reduce any engine failure to a minimal upstream reproduction.
- If the engine owns the failure, compare four explicit outcomes: proven engine update, verified
  in-place application, staging/copy-and-swap containment, and `GraphStore` engine replacement.
  Record migration cost, provenance/query compatibility, operational burden, and the evidence
  threshold for choosing each; evaluation does not authorize a migration by itself.

**Gate**

- One operation class and lifecycle condition deterministically reproduces the first mismatch, or
  all tested in-place paths remain clean and another confirmed owner is identified, or the bounded
  negative result selects staging/copy-and-swap containment.
- A confirmed engine fault cannot close Phase 2 without a recorded keep/update/contain/replace
  decision and its supporting evidence.

**2026-07-20 CoP evidence and engine decision**

Rebuild `rebuild-a9624f94e44d` materialized all 17 Community of Practice sources into staging
generation `17bfb665-436e-4184-80e8-215d4309b85b`. Every after-source audit was verified with zero
issues, and the final open-handle candidate contained 1,924 nodes and 7,010 edges. The mandatory
clean close/reopen audit then failed with 3,294 `adjacency_property_mismatch` findings across 2,966
relationships: 2,101 destination-only, 537 source-only, and 328 at both endpoints. Physical
adjacency remained present; redundant string properties `e.src` and `e.dst` had changed during
checkpoint/reload. The live graph was never swapped, and the failed staging family plus
`validation.json` were retained.

This boundary matches LadybugDB issue #658: a partial persisted string scan could write incorrect
dictionary indexes while checkpointing relationship properties, making the graph appear correct
until reload. LadybugDB PR #659 fixes that storage primitive and is present in 0.18.2. The selected
Phase 2 outcome is therefore a proven engine update to `ladybug>=0.18.2,<0.19`, combined with the
existing staging, close/reopen audit, and fail-closed swap gate. Marginalia does not repair or
reinterpret a corrupted graph and does not add an application-side checkpoint workaround.

**2026-07-28 one-source supervised validation**

Both 2026-07-20 rebuilds are terminal and remain retained failure evidence, not acceptance
evidence: CoP `rebuild-a5728ddd86dc` stopped at 11 of 17 verified sources and Qwen 27B High
`rebuild-49ec9dd6f444` stopped at 0, each with `EmbeddingProviderError: litellm embedding failed
for litellm_proxy/desktop/qwen3-embedding-4b`, `swap_allowed: false`, and `final_audit: null`. That
boundary was classified `upstream_unavailable` — the private-LAN model host's llama-swap process
was down and nginx returned HTTP 502 for its `/v1` route — so neither failure indicts the ingest
correctness machinery.

A deliberately narrow one-source validation on a later source commit now closes the technical
question those failures left open. It is explicitly **not** a 17-source CoP rebuild and supplies no
multi-corpus acceptance. A dedicated scratch vault `step8-onesource-validate` ran under a restarted
supervised server so the process actually loaded the capacity and embedding changes; the earlier
server predated them and could not have exercised either. Routing was the approved chain: Marginalia
to the LiteLLM Gateway to llama-swap on `<private-lan-model-host>`. The completion model was
`desktop/qwen3.6-35b-10-parallel`; `glm-5.2` was rejected by the managed credential's model
allowlist and is therefore unavailable to this deployment regardless of upstream health. Embedding
stayed `desktop/qwen3-embedding-4b`, dimension 2,560, batch size 32, one concurrent batch. Because
`desktop/qwen3.6-35b-10-parallel` is the single declared parallel-capable alias, effective
extraction and curation concurrency resolved to the configured 10 rather than clamping to one; the
`capacity` block reported `configured: 10, effective: 10` for both fan-outs with no notice, which is
the enforced policy passing a value through rather than the policy being absent.

Preflight through the same provider factory the ingest path uses returned two 2,560-wide vectors for
the embedding route and `pong` for the completion route. The first pass replayed the exact CoP source
that failed on 2026-07-20, `core-framework.md`, and completed parsing, extraction, embedding, dedup,
and commit with `provider_error: null`, 17 extracted nodes, 36 extracted edges, 25 minted Claims,
and 14 committed candidates. No `EmbeddingProviderError` occurred at any stage.

The queue-path pass then drove `cop-content-readme.md` through the durable ingest queue to a
terminal lifecycle. Item `0-610510f8` finished `status: done`, `stage: done`, and — per D6, which
keeps lifecycle and outcome separate — outcome `quality: complete` with
`receipts_complete: true`, `provider_failures: 0`, `failed_units: []`, and unit accounting
`scheduled 1 / attempted 1 / succeeded 1 / failed 0`. Its bound plan was
`8d25b7ac246841b0933bbdcbbe5c2f3b` and its post-file audit `35af9ac0c21445f082c0bae9f2df8ec2`
returned `verified` against generation `59a2ed6b-fa77-4a1a-8400-6ed3db19fb42`.

The final `GET`/`POST /api/v1/graph/integrity` audit `86a7b2baf5ef44179778828d42786277` scanned 100
nodes and 318 edges and adjacencies with `issue_count: 0`, all four completeness flags true,
`writer_fenced: false`, and `identity_contract_version: semantic_edges.v1`. The on-disk
`graph-integrity.json` sidecar carries the identical audit id, generation, status, and fence state,
so the endpoint and the durable sidecar agree rather than one reporting a stale or optimistic view.
The generation is therefore ADR 0039 verified end to end for this scope.

No production Marginalia daemon was running on either the model host or this machine, so the
standing pause-production protocol for model-slot contention reduced to a recorded no-op; nothing
was signalled and no model slots were contended. This validation proves the extraction/curation/embedding path and the verified-generation
chain on one source at the enforced capacity. It does not re-open the CoP corpus, does not replace
the full release matrix, and leaves the multi-source rebuild and ADR 0040 acceptance work untouched.

### Phase 3 — Write-ahead executable plan and receipts

**Likely owners**

- `src/marginalia/consolidate/gate.py`
- `src/marginalia/consolidate/ledger.py`
- `src/marginalia/companion/__init__.py`
- commit-plan/applier/resume tests

**Work**

- Enter only after Phase 2 has identified the owning boundary or selected the containment strategy.
- Inventory every current semantic graph writer and classify/reroute it.
- Make gate evaluation pure.
- Expand, checksum, flush, and `fsync` exact operations before writes.
- Apply only from the durable plan.
- Use deterministic topology ids for new/rebuilt graphs.
- Emit per-operation receipts and resume interrupted plans with store read-verification before any
  write.
- Compile source deletion through the same plan and emit a read-verified `source_removed` receipt.
- Run full post-file integrity verification before closing outcome as complete.

**Gate**

- Forced failure after every operation boundary resumes without duplicate artifacts.
- A crash after an artifact lands but before its receipt persists causes replay to observe and skip
  the already-correct artifact.
- Every planned operation has one terminal receipt.
- Removing a source leaves zero live artifacts derived only from that source and produces a
  durable receipt proving the checked scope.
- A tampered/misapplied artifact prevents a complete outcome and fences later writes.

**2026-07-19 relation-plan trace evidence**

CoP rebuild `rebuild-ea25f24be0dc`, running with Ladybug 0.18.2, completed eight consecutive
after-source audits with zero issues and then failed while sealing the executable plan for
`azure-ml-production.md`. The staging generation
`8097518b-ef9c-4047-a298-f58a91b93618` was retained, the live graph was not swapped, and the
failure occurred before any operation from that source's plan was applied. Candidate
`9ab660d706263367d5b26b1a9b6fc57e668d0436ba5dcef3cba7c879b9f733c4` exposed the boundary: two
raw relations converged on the same canonical candidate id; the first D7 decision committed it,
while a later redundant occurrence was correctly rejected but overwrote the in-memory pinned
trace used by the already-selected materialization. Plan validation then correctly refused the
mixed commit/reject evidence.

The owning-boundary correction keeps the validator strict. Exact duplicate extracted relations
are coalesced before curator work, and only a committed canonical relation may own its
materialization pin. Queue/reject decisions remain durable ledger evidence but cannot replace a
commit pin; a later commit may replace an earlier non-commit terminal state. Focused regressions
cover both exact duplicate extraction and distinct raw predicates that converge on one canonical
relation. The companion, relation-gate, executable-plan, and ledger-scan suites pass 177 tests.

### Phase 4 — Extraction-unit journal and targeted retry

**Likely owners**

- `src/marginalia/consolidate/ledger.py`
- `src/marginalia/companion/__init__.py`
- `src/marginalia/llm/` LiteLLM exception normalization
- `src/marginalia/server/_ingest_queue.py`
- extraction/queue/restart tests

**Work**

- Persist unit attempts/results with exact fingerprints.
- Persist explicit cancelled run terminals; unit reuse survives run supersession where the
  extraction fingerprint is equal.
- Reuse completed units across restart and operator retry.
- Add bounded transient retry and permanent/transient classification.
- Preserve provider-owned timeout policy: no Marginalia deadline unless the selected named
  provider configures one; retain explicit cancellation through the owned helper process.
- Persist failed Block ids and spans.
- Carry provider/empty-unit evidence into the normal ledger finish record.

**Gate**

- One injected timeout among many blocks yields `done + partial`, identifies the exact Block, and
  never reports complete.
- Retry calls only the failed Block and closes the result as complete after verified commit.
- An extraction-fingerprint change refuses stale unit replay. A downstream semantic-policy change
  reuses only extraction-fingerprint-equal raw units but recomputes decisions and plans.

### Phase 5 — Outcome/health UI and operational controls

**Likely owners**

- queue and ledger HTTP payloads
- Config/Logs/ingest frontend components
- frontend type definitions and API tests

**Work**

- Render complete, partial, failed, and integrity-failed distinctly.
- Show failed units, retry scope, plan/receipt state, integrity result, and graph generation.
- Disable ingest/retry/rebuild controls that would violate an active integrity fence, while
  retaining the explicit fresh rebuild recovery action.

**Gate**

- [x] The UI renders and tests distinct labels/actions for `cancelled`, `done + partial`,
  `error + integrity_failed`, and `done + complete` without opening raw JSON. Covered by
  `frontend/tests/release-smoke.mjs`'s `runIngestOutcomeGateSection` (2026-07-29, plan-10 item
  (c)): route-fixtured `/api/v1/ingest-queue*` and `/api/v1/ledger/summary*` payloads against the
  real running daemon and built bundle assert all four outcome states plus the
  `CurationProgress` T9 integrity-error row (`progress_integrity_error`, done > total) render
  distinctly, with retry shown only where the state is retryable. Run via
  `npm --prefix frontend run test:release-browser` (the existing CI leg,
  `.github/workflows/release-artifact-gate.yml`); no new test runner was introduced.

### Phase 6 — Recovery and rebuild proof

**Work**

- Refuse atomic swap of a rebuilt graph until its full integrity audit and every registered
  acceptance gate pass.
- Prove cancellation and restart preserve the live graph and resumable build state.
- Rebuild a copied LOTR vault only after Phases 1–4 pass.

**Gate**

- Fresh rebuild: zero integrity mismatches, every source represented, every successful unit
  receipted, and no unresolved unit hidden behind a complete result.
- Failed rebuild: original live graph and backup remain usable; failed staging graph is retained
  only as bounded diagnostic evidence.

### Phase 7 — Technical acceptance at realistic scale

**Required evidence**

- focused unit and Ladybug integration tests;
- default model-free suite;
- docs generation and drift gate;
- real daemon restart/cancel/resume acceptance;
- sequential and parallel extraction/embedding matrix;
- a real multi-document provider run;
- copied LOTR-scale rebuild or equivalent workload with a full post-file audit after each source;
- measured full-audit wall time and edge population, with the configured correctness-first exit
  threshold recorded.

Only after this phase is the materialized graph suitable for **authoritative** conceptual-quality
evaluation. ADR 0040 pre-commit measurement may already be running against clearly labelled
ledger/plan evidence.

---

## Required test matrix

### Model-free correctness

- Plan persisted before first write.
- Crash before write, mid-node writes, mid-topology writes, mid-Claim writes, and before receipt
  closure.
- Idempotent replay after each injected crash.
- Crash after an artifact lands but before its receipt is durable; replay read-verifies and does not
  rewrite the artifact.
- Torn or unreadable final plan/receipt record trips the fence.
- Missing endpoint, malformed operation, duplicate operation, and conflicting deterministic id.
- Integrity fixture with adjacency/property disagreement.
- Integrity fixture with a content-addressed id mismatch.
- Corruption of an edge not touched by the current plan is still caught by the full audit.
- Pagination across more than the graph overview cap.
- Progress population revision and `done > total` rejection.

### Provider execution

- Timeout, rate limit with `Retry-After`, authentication failure, bad model, connection failure,
  cancellation, malformed output, and empty-after-retry.
- Source hash mismatch before provider execution.
- One- and multi-draw extraction represented as one unit with durable sub-attempts.
- One failed Block among successful Blocks.
- Every Block failed.
- Later parallel Block succeeds while an earlier Block is still pending; restart reuses the later
  durable result.
- Retry with identical fingerprint versus changed model/prompt/chunking fingerprint.

### Store and lifecycle

- Empty, clean-populated, and reset/reused graphs.
- Clean close/reopen and daemon restart.
- Graceful stop and forced process loss.
- Full audit before and after each write phase.
- Rebuild staging audit failure prevents swap.
- Successful swap invalidates old handles and publishes the verified generation.

### UI/API

- Backward-compatible queue status plus additive outcome.
- Complete versus partial versus integrity-failed rendering.
- Failed-unit retry scope is explicit.
- Graph reads surface degraded/in-progress health.
- No secret values or source excerpts appear in integrity summaries.

---

## Observability contract

Every file-level run exposes a compact summary:

```json
{
  "lifecycle": "done",
  "quality": "partial",
  "source": {
    "document_id": "...",
    "blocks_total": 97,
    "source_hash": "sha256:..."
  },
  "extraction": {
    "attempted": 97,
    "succeeded": 96,
    "failed": 1,
    "failed_unit_ids": ["..."]
  },
  "commit": {
    "plan_id": "...",
    "operations": 10418,
    "receipted": 10418,
    "failed": 0
  },
  "integrity": {
    "status": "verified",
    "audit_id": "...",
    "graph_generation": "...",
    "identity_contract": "semantic_edges.v1",
    "edges_scanned": 45422,
    "mismatches": 0
  },
  "semantic": null
}
```

Raw provider errors remain redacted through the existing provider boundary. Integrity samples
contain ids/types and bounded titles only; they do not include source chunks, prompt bodies, API
keys, or embeddings.

---

## Compatibility and migration

- Existing source files and ledger records remain readable.
- New record kinds/fields are additive and versioned.
- Old runs without unit journals report extraction completeness as `unknown`, not retroactively
  complete.
- Existing graph topology is not assigned deterministic ids in place. The new identity contract
  begins on a fresh graph/rebuild, whose generation and identity-contract version are persisted.
- Existing queue clients continue to receive known lifecycle statuses; new clients use the
  additive outcome object.
- A vault that fails startup integrity remains readable for diagnosis and recoverable through a
  fresh rebuild, but semantic writes are fenced.
- The verified generation produced by ADR 0039 is a valid technical baseline. ADR 0040 may later
  supersede it with a new semantically re-materialized generation through the same validated swap.

---

## Risks and trade-offs

- A full audit after every file adds latency. Correctness takes precedence initially because the
  known corruption can affect untouched edges. Optimization requires evidence that a cheaper
  detector has identical coverage. Audit duration and scanned-edge counts are budgeted and retained
  so the correctness-first exit rule is measurable rather than discretionary.
- Unit journaling adds ledger volume. Store normalized candidates and hashes, not embeddings,
  source text, or full provider payloads.
- Deterministic topology ids change rebuild output identity. The graph is derived state and the
  change applies only through a fresh rebuild.
- Bounded automatic retry can increase cost. One transient retry is explicit, observable, and
  cancellable; operators can still choose no retry.
- Writer fencing makes failures louder. This is intentional: continuing after an integrity
  failure increases recovery cost and destroys evidence.
- Copy-and-swap per commit would provide stronger isolation but may be prohibitively expensive.
  It remains a containment option only if the reproduction proves safe in-place application is
  impossible.

---

## Exit criteria

This ADR is implemented only when all of the following are true:

- the integrity auditor is the single implementation used by startup, commit verification,
  rebuild, HTTP diagnostics, and the quality script;
- every semantic graph write comes from a durable plan recorded before the write;
- the semantic-writer inventory is complete, with every exception explicitly classified;
- every plan operation has one durable terminal receipt;
- source removal is planned, receipted, and verified to leave no source-exclusive derived artifact;
- plan and receipt durability includes checksum, flush/`fsync`, and torn-record behavior;
- plan replay is idempotent after a crash at every tested operation boundary;
- replay never reissues an edge upsert for a read-verified existing artifact;
- a full post-file audit detects both touched and untouched-edge corruption;
- an integrity failure fences subsequent writes and cannot be presented as success;
- every extraction Block has a durable terminal outcome and failed Block ids are retryable;
- a partial provider failure is visibly partial in ledger, queue, API, and UI;
- restart and retry reuse successful extraction units with the same extraction fingerprint;
- changed extraction configuration never replays stale unit output, while downstream semantic
  changes reuse only extraction-fingerprint-equal raw units and recompute decisions;
- progress never silently exceeds its denominator;
- full-graph quality checks cannot pass from truncated data;
- rebuild validates the staging graph before swap and preserves the prior live graph on failure;
- rebuild runs all registered technical and semantic acceptance gates before swap;
- the realistic multi-document acceptance matrix finishes with zero integrity mismatches;
- the copied LOTR corpus, or an equivalently demanding fixture, completes with verified source,
  unit, plan, receipt, and graph layers.

---

## Conceptual-quality program deferred to ADR 0040

After technical correctness is proven on a rebuilt graph, ADR 0040 evaluates:

- predicate vocabulary size and canonicalization policy;
- entity type stability and cross-type identity;
- alias/name/encoding normalization;
- relationship usefulness and domain-independent retention rules;
- cross-document reconciliation and corroboration;
- semantic evaluation datasets and quality thresholds.

This ADR may expose metrics needed by that program, but it must not silently normalize, delete,
or reinterpret semantic content. The technical layer proves what was decided and stored; the
conceptual layer decides what should have been proposed.

---

## Addendum · 2026-09-14 — `empty` is not `failed`

A live vault surfaced 4 of 76 ingested documents at `status=error`,
`outcome.quality=failed`, `error_class=empty_after_retry`, with `provider_failures: 0`. Three were
checksum manifests (lines of `"<sha256>  <filename>"`); the fourth was a small task ticket. The
extractor ran, made its one empty-result retry (ADR 0021's addendum), and correctly found nothing
entity-grade in any of them — nothing broke. The original T8 mapping above nonetheless classified
"no required unit produced usable output" as `failed` regardless of *why* every unit was
unresolved, so a document with zero provider/transport/parse problems inflated `queue_errors` and
held `/health` permanently degraded exactly as if extraction were broken. The same shape is also
inherently nondeterministic — one checksum-manifest sibling in the same batch yielded 3 nodes while
three identically-shaped others yielded zero — so operators saw a flapping error count for files
that never changed.

`empty` is added as a fifth result-quality value, computed in
`Companion.remember()` (`src/marginalia/companion/__init__.py`, the `technical_quality` local)
strictly ABOVE the `failed` arm: when every unresolved unit's reason is `empty_after_retry` and
zero units are `provider_failed`/`invalid_output`/`source_changed`, quality is `empty`, not
`failed`. Any genuine provider, transport, or parse failure — even one, alongside any number of
`empty_after_retry` units — still maps to `failed` or `partial` exactly as before; this addendum
narrows `failed`, it does not soften it. `_ingest_queue.py`'s error-lifecycle gate
(`outcome_quality in {"failed", "integrity_failed"}`) and `http.py`'s health `queue_error_count`
(`status == "error"`, or `status == "done"` with a `provider_error`) both needed no change: an
`empty` document lands at `status=done` with `provider_error=None`, which already falls outside
both checks. The document is still committed (Document + Block + any structural
has_tag/has_heading/links_to claims — ADR 0038) and its `empty` outcome stays visible in the queue
item, the ingest inspector events, and the REST/UI outcome badge; it is simply no longer counted as
an ingest error or a health degradation. Cross-document reconciliation scheduling
(`server/_curation.py::attach_verified_reconciliation_outcome`) deliberately still runs for `empty`
outcomes — it is gated on graph integrity, not extraction yield, and an empty document has nothing
for it to find, so this is a harmless no-op rather than a new exclusion to maintain.

---

## Addendum · 2026-09-18 — `no_units` is not `empty`, and the sub-stage ticks are not a block population

**Implementation status:** in the working tree; not in any published artifact.

### `no_units`, a seventh result quality

T8's mapping above had no arm for a run that did *nothing*. A run that scheduled zero units,
succeeded at zero, reused zero extractions and recorded zero intentional skips fell through every
guard and landed on `complete` — the one value a caller reads as "this document is fully
ingested". That is exactly the shape the sub-chunk narrowing defect produced: a prior `Block`
carrying no live LLM Claims, re-planned into `extract`, then silently dropped by the narrowing
pass with neither a ledger row nor an `intentionally_skipped_units` entry. The run reported
success and the block stayed permanently un-ingestable at that content hash.

`no_units` is computed in `Companion.remember()` (`src/marginalia/companion/__init__.py`, the
`technical_quality` local) strictly BELOW the `partial` arm and ABOVE `complete`: when
`scheduled_units == 0`, `successful_extraction_units == 0`, no extractions were reused and
`skipped_units == 0`, quality is `no_units`.

`skipped_units` is the discriminator, and it is the whole reason this is a distinct value rather
than a widening of an existing one. A healthy incremental no-op — every block unchanged and
already extracted — also has scheduled, succeeded and reused all at zero, but carries
`skipped_units > 0` and legitimately stays `complete`. Without the discriminator the two shapes
are indistinguishable.

**It is deliberately not `empty`.** The 2026-09-14 addendum reserves `empty` for "every unresolved
unit's reason is `empty_after_retry`" — the extractor ran, retried, and honestly found nothing
entity-grade. Under `no_units` there are no unresolved units at all, because there were no units:
nothing was attempted, so nothing can be said about yield. Folding the two together would make
`empty` mean both "the model read this and there was nothing in it" and "we never looked", and the
first of those is a finding about the document while the second is a finding about the run.

**It is also not `failed` or `integrity_failed`.** Nothing technically broke — no provider error,
no invalid output, no graph mismatch — so `no_units` stays outside the error lifecycle:
`_ingest_queue.py`'s gate (`outcome_quality in {"failed", "integrity_failed"}`) and `http.py`'s
health `queue_error_count` are unchanged, and the document lands at `status=done`. What it does do
is fail the `quality == "complete"` replay/short-circuit guard in `consolidate.ledger`, so a
zero-operation run can never be mistaken for a finished one on the next pass.

`receipts_complete` follows the same rule on both the early-apply and sealed-plan paths. A
zero-operation plan satisfies `len(plan_receipts) == len(sealed_plan.operations)` vacuously
(`0 == 0`), so it used to claim complete receipts for a run that performed no operation at all.
It is now `and not nothing_happened`. A *healthy* zero-operation plan — every block skipped as
unchanged — keeps `receipts_complete: true`, again on the `skipped_units` discriminator.

### T9 clarification — only block-counting stages report a blocks population

T9 says "every phase declares the population its denominator represents" and asserts
`done <= total`. The dedup and curation phases now emit keep-alive progress ticks, and they have
no block population to count: they report an **item ordinal with an explicitly undeclared total**
(`0` — T9's "no denominator exists yet" case), never a `blocks_done`/`blocks_total` pair.

The server side makes that explicit rather than inferring it. `server/_ingest_queue.py` records a
blocks population only for `_BLOCK_POPULATION_STAGES` (`parsing`, `extracting`, `embedding`); the
`dedup` and `committing` ticks bypass `_record_progress` entirely. Folding them in would both
clobber the displayed block counts and trip the T9 `done <= total` integrity check on every MCP
ingest of a small file — ordinal 212 against 3 blocks is a telemetry error under T9, and it would
have been a spurious one. Fine-grained curation progress already reaches the UI through the
`dedup_progress` / `curator_progress` / `relation_curator_progress` events, which carry their own
populations; the sub-stage ticks exist only to keep the live stage, and an MCP client's idle
timer, fresh.

The tick gate is "every `SUBSTAGE_PROGRESS_EVERY` (5) items OR `SUBSTAGE_PROGRESS_INTERVAL_S`
(20.0) seconds since the last tick, whichever comes first". Item count alone cannot bound the
silence, because N items are N unbounded serial LLM calls; the clock is what makes the bound real,
and the count floor is what stops a fast phase from flooding the client.

### Measured ingest cost — curation dominates, and it is serial by default

Recorded because T9 is about honest progress and this is what the progress is measuring. One live
MCP ingest of a 15,882-byte / 3-block Markdown file, end to end, against the **default local
endpoint** (`http://127.0.0.1:8123/v1`, qwen3.8-27b on a Mac):

| | |
|---|---|
| Total wall clock | 1,536 s |
| Extraction | 271 s (18%) |
| Curation | 1,266 s (82%) |
| Completion calls | 177 |
| Input / output tokens | 438,464 / 28,495 |
| Embedding calls | 5 |
| Committed / queued | 25 / 29 |
| Nodes / edges extracted | 54 / 105 |
| Claims minted | 40 |

**Endpoint qualification (2026-09-19).** The same file, same model (qwen3.8-27b), re-run on a LAN
inference server, vault recreated: **297 s** end to end, 255 s curation, 163
completion calls, 407,279 / 26,837 tokens, same `units {scheduled:3, attempted:3, succeeded:3}`
and `quality: complete`. Per-call latency on an identical 2,139-token payload was 6.0–9.3 s on
the Mac endpoint against 1.14–2.26 s on the LAN inference server. The endpoint alone is a ~5.2× lever on wall
clock; the 1,536 s figure is the Mac endpoint's number, not the product's. What does not change
between the two is the shape: curation is the bulk of the time at either end, and it is serial.

Three blocks, twenty-five minutes on the default endpoint. Curation is four and a half times extraction because it is
**serial by default**: `curation_max_concurrent` is `1`, `curation_batch_size` is `1`, and
`curation_call_timeout_s` is `None`/unbounded (`config/_vault.py:1358`, `:1366`, `:1362` — the
deliberate ADR 0015 D1/D4 opt-in defaults). Before the keep-alive ticks and the MCP progress
bridge, the calling MCP client aborted this ingest at roughly 300 s of silence and discarded the
payload of a call the daemon was still working on; with them it completes.

The trap worth knowing: raising `curation_max_concurrent` may do nothing.
`curation_effective_max_concurrent` (`config/_capacity.py:133`) clamps the configured value back
to the floor of `1` unless **every** model used by a curation step appears in
`llm.parallel_capable_models`. A vault can be configured for four-way curation fan-out and run
strictly sequentially with no error — `capacity_notice` is the only thing that says so, and it
names only the steps that actually failed the allowlist.

**`curation_batch_size`, one knob at a time (2026-09-19).** Same file, same LAN endpoint
(the remote inference server, qwen3.8-27b), vault recreated per run, everything else at defaults:

| `curation_batch_size` | Total | Curation | Calls | Input / output tokens |
|---|---|---|---|---|
| 1 (default) | 297 s | 255 s | 163 | 407,279 / 26,837 |
| 8 | 313 s | 275 s | 58 | 144,654 / 32,495 |
| 32 | 350 s | 297 s | 56 | 136,778 / 38,328 |

Wall clock worsens monotonically as the batch grows; input tokens fall 66%; output tokens rise
43%; call count floors at ~56–58. The knob trades prompt overhead for generation length, and on a
local model generation is what costs time — so it helps when input tokens are the cost (a paid
API, a context-window budget), not local wall clock. Quality is **not** concluded from this:
`claims_minted` came out 40 / 30 / 58 across the three runs, but that is n=1 per condition
with no repeat-pair variance measured for this file, so the spread cannot be attributed to the
knob. `curation_max_concurrent` remains the untested lever;
it is also the one that silently clamps to 1 without `llm.parallel_capable_models` (above).
