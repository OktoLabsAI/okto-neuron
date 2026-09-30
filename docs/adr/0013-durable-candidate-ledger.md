# ADR 0013: Durable Candidate Ledger and Pre-Commit Curation Boundary

- **Status:** Accepted; core boundary shipped, projection cleanup remains future work
- **Date:** 2026-06-08
- **Deciders:** Alex Rivera, Marginalia core
- **Builds on:** ADR 0004, ADR 0007, ADR 0008, ADR 0009, ADR 0010
- **Relates to:** ADR 0005, ADR 0006, ADR 0011, ADR 0012
- **Supersedes:** nothing. This makes ADR 0009's ingestion-time curation boundary explicit.

**Lifecycle addendum — 2026-07-13.** Append-only candidate/run/comparison/plan/commit
records, safe resume, HTTP summaries, and UI inspection shipped. The graph write consumes
the curated commit plan. Making every legacy review file a pure ledger projection remains
future product hardening; it is not a `0.0.40` release gate.

---

## Context

Marginalia's current agentic ingest path works better than expected, but its most
important curation decisions are still mostly transient. The system can show LLM
requests/responses and per-file ingest events, yet there is no durable object that says:

- what candidate was proposed;
- what source block and model/prompt produced it;
- what same-run candidates and existing graph nodes it was compared against;
- what the judge decided and why;
- whether it became a graph write, a merge, a review item, a dead letter, or a drop.

The current implementation already has a real brake between extraction and graph writes.
It is not "LLM JSON directly writes the graph." `Companion.remember()` extracts candidates,
runs exact and judged deduplication, resolves against the store, gates by confidence, and
then commits accepted candidates or parks uncertain ones in `review_queue.json`.

The problem is that this brake is an in-memory pipeline, not a durable pre-commit workbench.
Once a file finishes, the detailed candidate lifecycle is gone except for bounded ingest logs,
committed graph state, and the small subset that was queued for review.

---

## AS IS: file to graph

The current pipeline is:

```text
file
  -> Vault.add(source)
  -> deterministic Document / Block / markdown-derived graph writes
  -> per-block extraction units
  -> one LLM extraction call per block
  -> in-memory NodeCandidate / EdgeCandidate objects
  -> in-memory exact + judged dedup / reconcile
  -> node candidate curator
  -> relationship curator for writable edges / literal claims
  -> confidence gate
  -> ConsolidationSession.commit()
  -> graph writes + review_queue.json for uncertain node candidates
```

In more concrete terms:

1. A user queues or posts a file.
2. The ingest worker calls `Companion.remember(source)`.
3. `remember()` calls `Vault.add(source)`.
4. `Vault.add()` parses markdown and writes the trust-root substrate and deterministic
   derived records into the graph.
5. `remember()` reads anchored extraction units from the stored blocks.
6. `LLMExtractor` asks the configured extraction LLM for one combined JSON shape:
   `nodes[]`, `edges[]`, and `claims[]`.
7. The parsed result becomes in-memory `NodeCandidate` and `EdgeCandidate` values.
8. The pipeline runs in-memory curation:
   exact within-file dedup, within-batch judge, exact store reconcile, store judge,
   resolve, and an LLM-backed node candidate curator.
9. The relationship curator evaluates every extracted topology edge and literal claim whose
   endpoints can still be written; unsupported or over-inferred relationships are queued or
   dead-lettered before `ConsolidationSession`.
10. Accepted candidates are committed through `ConsolidationSession.commit()`.
11. Uncertain node candidates are persisted in `review_queue.json`.
12. Relationship Claims are minted after commit when their endpoints are live.

This is safe enough for the current graph, but it has two structural blind spots:

- only queued candidates survive as inspectable process artifacts;
- the "why" behind committed, merged, remapped, dropped, and dead-lettered candidates is
  not a first-class durable record.

---

## TO BE: durable pre-commit workbench

The target pipeline is:

```text
file
  -> source anchoring
  -> extraction run creates durable candidate records
  -> resolver / curator workers update candidate states
  -> commit plan is generated
  -> approved plan is applied to the graph
  -> every decision remains inspectable
```

The architectural change is:

```text
AS IS: candidate objects are temporary Python objects.
TO BE: candidate objects are durable pre-graph artifacts with lifecycle state.
```

The trust-root substrate remains separate. Marginalia should still record the source file,
document identity, block/source spans, content hashes, and other deterministic source
anchors before agentic curation. The new boundary applies to derived knowledge that needs
curation: extracted entities, relationships, propositional claims, merge decisions, and
commit decisions.

---

## Decision

Introduce a **durable candidate ledger** as the pre-commit boundary for agentic extraction.

The ledger is vault-local derived state, not a graph primitive. It lives beside other
state under its immutable vault runtime's Marginalia state directory. It is rebuildable from
the markdown trust root plus extraction configuration, and it can be discarded without
changing source files.

### Core objects

The minimum durable objects are:

- `IngestRun`: one file/source ingest attempt, with config snapshot, source path, timestamps,
  status, and LLM/provider metadata.
- `Candidate`: one proposed entity, relation, or claim, tied to an ingest run, source block,
  byte span when available, extraction prompt hash, model id, raw payload, normalized payload,
  and current state.
- `Comparison`: one candidate-vs-candidate or candidate-vs-graph comparison, with method
  (`exact`, `embedding_band`, `judge`, `curator`, `relation_curator`, `identifier`,
  `manual`), scores, target reference, verdict, reason, and optional LLM trace reference.
- `CommitPlan`: an explicit pre-write plan containing graph operations such as create node,
  create edge, mint claim, merge/remap to existing node, queue for review, drop, or dead-letter.
- `CommitRecord`: the result of applying a commit plan, with graph ids written and any
  validation failures.

### Candidate lifecycle

Candidate state should be explicit and queryable:

```text
proposed
  -> normalized
  -> resolving
  -> planned
  -> committed | queued | merged | dropped | dead_lettered
```

The exact state names may change during implementation, but the invariant is fixed:
every candidate has a durable state and a reasoned terminal outcome.

### Commit boundary

Agentic extraction must not mutate semantic graph state directly. It creates candidates.
Resolver/curator workers create or update comparisons. The gate emits a commit plan. Only
the commit-plan applier mutates the graph.

The existing `review_queue.json` becomes a projection of ledger state, not a separate source
of truth. A queued candidate is simply a candidate whose terminal state is `queued` and whose
review action is pending.

### Read path and the index sidecar

`candidate-ledger.jsonl` is append-only and grows with every ingest, so no reader may
hold it in memory. `records()` and `scan()` stream it in chunks (`iter_records()` is the
unbounded-consumer form, and `scan(kinds=...)` keeps only the row kinds a caller reads).
The run list, run detail, progress summary and the sealed-plan recovery reads do not scan
at all: they use `candidate-ledger.jsonl.index`, a small file next to the ledger.

The sidecar is a cache and never a source of truth. The ledger format is unchanged and every
fact in the sidecar is derivable from the ledger by one streaming pass, so deleting the file
is always safe. It holds, per run, the byte span of its rows and the fields that order runs,
plus the open-plan set (plans with a sealed `commit_plan` and no `commit_record` or
`plan_abandoned`). The index is maintained inside `append()` under the same lock as the
write, and checkpointed atomically (temp file, fsync, rename) every few MiB of ledger.

On first use the sidecar is trusted only if it has the current format version, a matching
checksum, and the ledger still contains the bytes it was built from (the ledger is not
shorter and the last covered bytes are identical). A ledger that is longer than the index
covers, because another process appended or a crash fell between the ledger write and the
index update, is caught up by scanning only the new tail. Anything else (missing file,
corruption, other version, truncated or rewritten ledger) rebuilds the index from the
ledger. A plan lifecycle that is not the clean plan, receipts, one terminal row sequence
sets an anomaly flag; the reader then re-derives its verdict with the whole-ledger
validation, so the errors raised for a damaged ledger are the ones it always raised.

### Agentic workflow shape

The first version is not "many agents." It is one durable workflow with worker roles:

```text
Extractor worker
  writes candidate records

Resolver worker
  compares candidates to same-run candidates and current graph state

Judge worker
  adds LLM-backed comparison verdicts only where deterministic signals are insufficient

Candidate curator worker
  evaluates every surviving node candidate before the confidence gate

Relationship curator worker
  evaluates every surviving topology edge and literal claim before graph write

Planner worker
  turns candidate state + comparisons into a commit plan

Applier worker
  applies the approved/high-confidence plan to the graph
```

Initially, the existing single extraction prompt may continue to produce nodes, edges, and
claims together. The extra LLM work is curation over proposed artifacts, not specialist
proposal. Splitting extraction into specialist LLM calls is a later optimization that should
be driven by ledger evidence, not assumed up front.

---

## Why not split extraction first

Breaking extraction into separate "find entities", "find relations", and "find claims" agents
is tempting, but it is not the safest first structural change.

Without a durable candidate ledger, more LLM calls would produce more hidden intermediate
state and more ways for partial outputs to disagree. The ledger gives us the measurement
surface needed to decide whether specialist extraction actually helps:

- Which candidate types are noisy?
- Which source blocks produce bad relationships?
- Which judge calls reverse or merge extractor output?
- Which claims are dropped because endpoints are missing?
- Which prompt variants improve commit quality instead of just increasing volume?

The first system improvement is therefore observability and a durable curation boundary.
Specialized extraction workers come after the workbench exists.

---

## Consequences

### Positive

- Every graph write has an inspectable pre-write lineage.
- The UI can show file -> chunk -> LLM request/response -> candidate -> comparisons -> plan
  -> graph write.
- Ingest becomes resumable at the candidate/plan boundary instead of only at the per-file
  queue boundary.
- Review becomes broader than "low-confidence node candidate"; users can inspect committed,
  merged, dropped, and dead-lettered outcomes too.
- Future specialist agents have a shared substrate instead of ad hoc hidden handoffs.
- Evaluation can measure candidate quality, resolver quality, and commit quality separately.

### Negative / risks

- More local state is written per ingest.
- LLM traces and candidate payloads may contain sensitive source text, so retention and
  redaction policy are required.
- The implementation must avoid turning process artifacts into graph primitives.
- The UI can become noisy unless it defaults to summaries and drills down on demand.
- Rebuild semantics need care: a fresh graph rebuild should either rebuild the ledger or mark
  old ledger records as belonging to an obsolete graph generation.

### Invariants

- Markdown/source files remain the trust root.
- Source anchors are still deterministic and byte/source-span grounded.
- Candidate ledger records are process artifacts, not schema primitives.
- No agentic semantic graph write occurs without a commit-plan entry.
- A merge must be reversible or at least explainable from durable comparison records.
- The system must preserve the existing single-writer safety model from ADR 0007 and ADR 0009.

---

## Rollout

**Implementation note 2026-06-08.** Steps 1-4 and the first UI drill-down are in
place without changing graph-write semantics. `CandidateLedger` persists append-only
JSONL at `<vault>/.marginalia/candidate-ledger.jsonl`, and `Companion.remember()`
records run start/finish, proposed node/edge candidates, dedup/resolve comparison
records, gate-derived commit-plan operations, edge terminal outcomes, and commit
records. The loop now also records LLM curator comparisons for every proposed node
candidate and relationship-curator comparisons for every final proposed topology edge /
literal claim candidate. Candidates that deterministic reconcile/remap paths remove before
the final gate receive deterministic audit-only comparison records by default
(`llm_skipped: true`) and terminal `superseded` state without blocking commit. Vaults that
need exhaustive LLM adjudication of those superseded/raw candidates can opt in with
`consolidation.audit_superseded_nodes_with_llm: true` and
`consolidation.audit_superseded_relations_with_llm: true`; in that mode, a curator `commit`
verdict permits terminal `superseded`, while any non-commit verdict (`queue`, malformed
output, or provider failure/abstain) routes the candidate to review. Remapped edge
candidates still receive their own derived `proposed` record before relation review, so the
ledger has no comparison/terminal record whose candidate id was never introduced. The
curator prompt now includes the deterministic resolver proposal
(for example: pass to gate, supersede/remap, or dead-letter after endpoint review),
making the mechanical resolver an evidence layer rather than the semantic evaluator.
Relationships whose endpoints did not survive node curation are still sent to the
relation curator, then terminally dead-lettered by a separate endpoint gate instead of
bypassing LLM review or silently vanishing.
Relationship curator abstention also fails closed: the relation remains a queued ledger
candidate and no topology edge or literal Claim is written from resolver evidence alone.
The relationship curator now also returns a structured `canonical_predicate`; committed
relations are rewritten through that LLM-approved predicate before graph write / Claim
minting, with the original relation candidate marked `superseded` and the canonicalized
candidate recorded as a derived proposal. This keeps predicate cleanup inside the
curation boundary instead of doing silent deterministic rewrites. When a commit verdict
omits `canonical_predicate`, Marginalia now canonicalizes the original extracted predicate
before writing, so observed local variants such as `uses_analogy`, `failure_mode`,
`red_flag`, and `constraint` still collapse to the preferred graph vocabulary.
The curator prompts also treat cross-reference-only sibling targets as navigation
metadata, so references like "Pillar N: X" are queued unless the current excerpt
substantively defines that pillar. Review actions append ledger records so a queued
candidate's manual resolution remains visible. HTTP exposes `GET /api/v1/ledger/runs`,
`GET /api/v1/ledger/runs/{run_id}`, and compact `GET /api/v1/ledger/summary` for
committed-vs-pending UI surfaces. The web UI's Logs view now has a **Ledger** mode for
run/candidate/decision/plan inspection, and the Graph view shows a live banner when the
committed graph is behind an active file-level commit, including pending accepted node /
relationship counts and relation-curator progress. `GET /api/v1/ledger/summary` keeps raw
`candidate_kinds` separate from post-dedup `active_candidate_kinds`; progress denominators
use the active counts so deterministic exact/judged dedup does not make curator progress
look artificially stalled. Before a file reaches the ledger phase, the Graph banner also
surfaces extraction-candidate telemetry from the ingest queue detail events, and newer queue
snapshots expose cumulative `extracted_nodes`, `extracted_edges`, and `extracted_claims`
counters. The compatibility `review_queue.json` file still exists and is not yet a pure
projection of ledger state. Bulk-queue cancellation now stops at safe stage/model-call boundaries
before the confidence gate and leaves the open ledger run resumable; explicit candidate-level
operator resume controls remain future work.

**Implementation note 2026-07-17.** `review_queue.json` now carries additive `kind` tags. Legacy
rows without a tag remain node rows; newly written node rows are tagged `node`, and relation rows
are tagged `relation`. A relation row preserves its complete `EdgeCandidate`, exact D6/D7
`queue_*` reason, full predicate-admission decision, and pinned semantic gate input, including raw
and admitted predicates, admission reason/state, inverse swap, endpoint decisions, object
representation, source-grounding evidence, and noise/redundancy state. The same pinned proposal
also retains the curator's predicate definition/direction, inverse requirement, and usefulness
signal. Relation entries have explicit enqueue, list, read, and acknowledge operations. They cannot
execute a graph-writing review action inside `ReviewQueue`; a later Companion orchestration
boundary must apply and audit a resolved relation before acknowledging its queue record. The JSON
replacement remains atomic and strict readers reject corrupt framing, duplicate ids, unknown kinds,
and inconsistent reason/input pairs.

**Implementation note 2026-06-09.** Extraction tuning is now explicitly split between a
corpus-agnostic core prompt and optional pack guidance. The base extractor keeps the locked
five primitives plus literal Claims and does not carry reference-corpus examples by
default. SDLC-specific examples such as `Epic`, `Story`, `Task`, Definition of Ready, and
Definition of Done live behind the existing `sdlc` pack and are only applied to vaults that
enable that pack. Candidate and relationship curator prompts follow the same boundary. A
repeatable live guardrail, `scripts/ingest_quality_check.py`, now exercises the pipeline
through HTTP against real vaults: the CoP vault reset/reingest verifies SDLC concepts,
canonical predicates, curator ledger coverage, and no extracted structural title noise; the
LOTR vault reset/reingest verifies that a very different literary corpus extracts Tolkien /
publication-history knowledge without leaking SDLC taxonomy. The guardrail reports raw graph
nodes separately from knowledge nodes so provenance `Document` / `Block` anchors are not
mistaken for extracted concepts, and it now reports retained active-ingest extraction events
separately from committed graph state. That active-ingest layer counts node mentions,
topology relation candidates, literal claim candidates, and optional domain-profile coverage
before a file reaches the ledger/commit boundary.

**Implementation note 2026-06-09b.** The final node gate now receives feedback from
relationship curation. After the relationship curator approves or queues topology edges and
literal claims, Marginalia computes which node candidates are supported by at least one
accepted relationship or accepted literal claim. A novel node candidate that would otherwise
auto-commit but has no accepted relationship is forced to review and recorded as a
`relationship_liveness_gate` comparison. This closes the orphan-write gap exposed by the
LOTR guardrail: contributors stayed connected once their cited work was committed, and
revision-fragment concepts whose only relation was rejected no longer entered the graph as
standalone knowledge. The same gate still permits grounded standalone subjects when they
have an accepted literal claim, and permits topology endpoints when the approved relation
connects them. The extractor and curator prompts also now handle concrete cited works and
contributors generically: named books, articles, reports, bibliographies, datasets, and
editions should stay live when grounded authorship/editor/contributor relations depend on
them.

**Implementation note 2026-06-09c.** The LOTR live ingest exposed that exhaustive
LLM review of superseded/raw audit-only relationships can dominate wall-clock time after
the final graph-writing relationship decisions are already complete. The default is now
non-blocking audit for superseded candidates: the ledger still records comparison and
terminal state, but skips the extra LLM call unless the vault explicitly opts into
`consolidation.audit_superseded_*_with_llm`. The repeatable guardrail report also now
distinguishes `llm_reviewed` versus `llm_skipped` audit rows and emits a
`pending_commit_preview`, now also exposed through `GET /api/v1/ledger/summary`, so a live
run can show the semantic node/relation shape before the commit plan is written.

**Implementation note 2026-06-10.** The full LOTR run confirmed why the graph view must
not be treated as the extractor's current output: while Fellowship was still in extraction,
the committed graph was still only one Document, one heading Claim, and Block anchors, but
the retained ingest-event window already showed hundreds of LOTR node mentions, topology
relations, and literal claims. `scripts/ingest_quality_check.py` now includes an
`ingest_extraction` section and threshold flags for those retained events, so live checks
can say "the extractor is producing LOTR candidates" before the candidate ledger starts
receiving proposed rows for the file. That same retained-event inspection exposed
placeholder predicates such as `unknown`; the parser now drops predicates that normalize to
`unknown`, `none`, `null`, or `n/a` before they can become topology edges or literal claims.
The same full-run inspection also exposed misleading progress denominators: Fellowship
produced 2,308 raw node candidates and 3,137 raw edge candidates, but exact/judged dedup
reduced the active review set to 880 nodes and 3,125 edges. The compact ledger summary and
repeatable guardrail now expose `active_candidate_kinds`, and node/relation curator progress
uses those active totals. The node-review guardrail also reports same-title/multiple-type
candidate conflicts, so type instability such as `Anduril` appearing as both `Agent` and
`Concept` is visible before commit. The ingest inspector now emits coarse
`relation_curator_progress` events during long relation-review runs, while batching durable
queue-history writes for high-volume LLM request/response artifacts; the detailed artifacts
remain available in the live in-memory inspector without forcing thousands of sidecar writes.
The compact summary now also carries sample accepted node candidates and relationship triples,
which lets the Logs screen show a pending KG preview before the file-level commit plan lands.
The repeatable guardrail mirrors that pending layer with `--min-pending-nodes` and
`--min-pending-relations`, so a live run can fail fast if the accepted pre-commit KG is tiny
even though extraction appeared active. The guardrail also reports pending domain-profile
coverage and missing diagnostics for expected entities that were proposed but not accepted,
so profile gaps can be traced to extraction, dedup representative selection, curation, or
endpoint gating.

**Implementation note 2026-06-10b.** The LOTR diagnostics traced several missing expected
entities to exact duplicate representative selection. The extractor proposed `Frodo`,
`Moria`, `Lothlorien`, and `Mordor` repeatedly, but the first exact-title survivor for each
group could be a weak local mention; stronger later mentions were superseded before normal
curation could accept them. Exact within-file duplicate collapse now receives the extraction
unit text for each candidate and, only inside same-type/same-title duplicate groups, keeps the
candidate whose title/content are best grounded in its own source text. Non-duplicate ordering
stays stable, and same-title endpoints connected by a staged edge are excluded from this
reordering because they are distinct by construction.

**Implementation note 2026-06-10c.** The live LOTR guardrail report now compacts queue item
state before printing JSON. It keeps queue summary, active item stage/progress, counts, and
last-event metadata, but omits large `last_event.payload` objects such as LLM messages and
source excerpts. Detailed artifacts remain inspectable through Logs and the per-item endpoint;
the repeatable guardrail output stays small enough to review and archive.

Latest live evidence:

- LOTR live reingest:
  `python scripts/ingest_quality_check.py --vault LOTR --include-ledger-detail
  --domain-profile lotr --max-queue-items 4 --min-pending-nodes 500
  --min-pending-relations 400 --min-pending-domain-profile-coverage 0.75`
  (visible graph still only structural anchors while Fellowship is in relation review; the
  recent live samples showed `621` accepted pending nodes, `811+` accepted pending relations,
  `880/880` node curator progress, and relation curator progress past `2010/3125`, with sample
  triples such as `J.R.R. Tolkien author_of The Lord of the Rings` and
  `The Lord of the Rings includes The Fellowship of the Ring`. Pending LOTR profile coverage
  is `17/21`; the remaining expected gaps are `Frodo`, `Moria`, `Lothlorien`, and `Mordor`.
  Diagnostics show these were extracted repeatedly but the accepted representative did not
  survive curation (`Frodo`: 88 matching candidates, 85 superseded, 3 queued), which points
  at dedup representative selection / superseded duplicate audit rather than extraction
  absence).
- Reference-corpus reset/reingest:
  the final CoP quality report of 2026-06-09, kept privately
  (`491` nodes, `1673` edges, `357` Claim nodes, required SDLC concepts present, zero isolated
  knowledge nodes).
- Full automated suite: `pytest -q` = `1124 passed, 21 skipped, 1 xfailed`; `ruff check .`
  clean.

1. Define the ledger data model and storage location.
2. Persist `IngestRun` and `Candidate` records from the existing extraction path without
   changing graph write behavior.
3. Persist comparison records for exact dedup, within-batch judge, store reconcile, store
   judge, resolve correlations, candidate curator decisions, relationship curator decisions,
   and gate decisions.
4. Generate a `CommitPlan` from the existing gate/session path while still applying it
   immediately.
5. Change the review queue to read from ledger state.
6. Add UI drill-down: run list, source chunks, candidates, comparisons, plan, commit result.
7. Add resumability/cancellation at safe boundaries. **Implemented:** interrupted ledger runs
   resume automatically, and bulk stop winds down at stage/model-call boundaries before commit.
8. Only after the ledger is useful, evaluate specialist extraction workers for entity,
   relation, and claim proposal.

---

## Non-goals

- This ADR does not change the closed primitive set.
- This ADR does not require splitting extraction into multiple LLM calls.
- This ADR does not make all vaults queryable as one graph.
- This ADR does not move source files out of the markdown trust-root model.
- This ADR does not make low-confidence writes automatic.
- This ADR does not replace ADR 0008 off-graph authority records or ADR 0009 curation jobs.

---

## Exit criteria

The implementation is complete when:

- every agentic ingest has a durable `IngestRun`;
- every extracted entity, relation, and claim is represented as a durable candidate before
  commit;
- every final proposed entity, relation, and claim has an LLM curator or relationship-curator
  comparison record before graph write;
- candidates removed or remapped before final graph-write review have deterministic
  audit-only comparison records by default, with optional per-vault exhaustive LLM audit;
- graph writes require an explicit curator/relation-curator `commit` verdict; superseded
  audit-only candidates require either deterministic remap evidence or, when exhaustive audit
  is enabled, an explicit curator/relation-curator `commit` verdict;
- novel node writes require at least one accepted relationship or literal claim after
  relationship curation; otherwise the node queues as a `relationship_liveness_gate`
  decision instead of entering the graph isolated;
- every candidate has a terminal state or a pending review state, including candidates
  superseded by deterministic remap/collapse before graph write;
- dedup, judge, resolve, and gate decisions are visible as comparison/decision records;
- graph writes are applied from an explicit commit plan;
- the UI can inspect the full file-to-graph lineage for at least one ingest run;
- existing `remember()` behavior is preserved for old clients;
- tests cover ledger persistence, state transitions, commit-plan application, review queue
  projection, and restart/rebuild behavior.
