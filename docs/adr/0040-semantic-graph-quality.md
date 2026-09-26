# ADR 0040: Semantic Graph Quality — Stable Identities, Governed Predicates, and Useful Relations

- **Status:** Proposed
- **Date:** 2026-07-16
- **Deciders:** Marginalia maintainers
- **Builds on:** ADR 0004, ADR 0008, ADR 0010, ADR 0013, ADR 0016, ADR 0017,
  ADR 0020, ADR 0022, ADR 0031, ADR 0039
- **Scope:** conceptual correctness of extracted entity identity and type, surface-form
  normalization, predicate governance, relation usefulness, cross-document reconciliation, and
  semantic evaluation
- **Prerequisite:** ADR 0039 must first establish that the graph faithfully materializes its
  durable plans. Until then, semantic analysis uses the candidate ledger and commit plans rather
  than damaged graph topology.
- **Out of scope:** provider reliability, retries, concurrency, storage integrity, changing the
  closed five-primitive schema, a full bi-temporal query model, and corpus-specific production
  rules
- **Implementation status:** in progress. The shared evaluator, semantic-write boundary,
  governance UI, rebuild/rollback gate, frozen LongMemEval-compatible adapter, one-Hobbit verified
  Phase 1b diagnostic, complete synthetic determinism arms, all temporal-chat scenario arms, and a
  manifest-pinned model-stage ablation pilot now exist. The real four-corpus adjudication,
  threshold, remaining corpus scenarios, human-qualified ablation, and public diagnostic evidence
  is still incomplete, so
  this ADR remains Proposed.

---

## Purpose

Marginalia must produce a graph that is not only technically intact, but conceptually coherent:

- the same referent should not fragment into avoidable duplicate identities;
- a person or place should not change primitive merely because it appears in another chunk;
- surface variants should remain searchable without becoming competing canonical nodes;
- relations should use a governed, intelligible predicate vocabulary;
- every committed relation should say something useful and source-grounded; and
- quality should be measured across domains rather than inferred from a few familiar names being
  present.

The product constraint remains: Marginalia may spend substantial model compute while constructing
knowledge, but ordinary recall must stay completion-free and close to zero marginal cost. Semantic
quality work must improve the materialized knowledge rather than move unresolved reasoning into the
read path.

ADR 0039 answers: **did Marginalia store what it decided?**

This ADR answers: **did Marginalia make good semantic decisions?**

The order is binding. A corrupted graph cannot honestly score entity or relationship quality, and
a semantically poor graph does not become correct merely because its writes are durable.

```text
ADR 0039: source -> extraction unit -> durable plan -> verified graph
                                                    |
                                                    v
ADR 0040: mention -> normalized surface -> type -> identity -> predicate -> useful relation
                                                    |
                                                    v
                                      cross-document reconciliation
                                                    |
                                                    v
                                          semantic quality gates
```

---

## Durable implementation TODO

- [x] Add the versioned `semantic_quality.v1` stored-graph diagnostic report.
- [x] Expose the complete-store report through `/api/v1/quality/semantic` and project it through
  `scripts/ingest_quality_check.py` without allowing a truncated graph overview to pass checks.
- [x] Instrument ordinary recall with `recall_cost.v1` and aggregate supplied samples into the
  semantic report without treating absent or partial accounting as measured evidence.
- [x] Add a lossless candidate-ledger framing scan that hashes the complete file and reports line
  scope, versions, malformed evidence, and interrupted final appends without hiding readable rows.
- [x] Add an explicit-run candidate-ledger/plan evaluator with a stable selected-record hash,
  pre-apply order evidence, and only metrics supported by current candidate and plan records.
- [x] Preserve each extracted node candidate's exact source title beside versioned exact/discovery
  keys and the selected canonical display without changing candidate identity or merge behavior.
- [x] Run the narrow pre-commit measurement slice over preserved ledger/plan evidence: the
  framing-verified ledger scan, the explicit-run candidate/plan evaluator, and anchor-shape
  reporting. This slice is not Phase 1a closure — see "These slices do **not** satisfy Phase 1a"
  below.
- [x] Persist layered `config_fingerprint`, `extraction_fingerprint`, and
  `semantic_policy_fingerprint` values on new ingest-run start records, excluding secrets and
  execution-only controls.
- [x] Emit a complete opaque `semantic_snapshot.v1` for verified graph generations and provide a
  deterministic `semantic_churn.v1` comparator for identity, predicate, and relation populations.
- [x] Implement a fail-closed `semantic_acceptance_matrix.v1` evaluator and CLI gate for the four
  corpus classes, locked per-layer thresholds, exact scenario churn, policy-change recomputation,
  byte grounding, review coverage, ablations, and the separate public diagnostic.
- [x] Add a SHA-256-pinned acceptance collection boundary that reports missing or changed evidence
  before materialization and refuses to build a matrix bundle until all 67 required artifacts are
  present and structurally valid.
- [x] Validate every pinned collection slot against its semantic schema during status inspection,
  and safely unwrap the product-owned Golden quality envelope so one immutable capture can supply
  its report and snapshot without duplicate evidence files.
- [x] Create the adversarial fixture and human-adjudication format without changing writes.
- [x] Define and synthetically verify the frozen LongMemEval-compatible adapter and reproducibility
  manifest without committing or downloading the public corpus.
- [x] Add a privacy-safe synthetic temporal-chat Golden corpus and pass its deterministic byte-hash
  preflight; owner review of its answers and semantic adjudication remains pending.
- [x] Correct Golden byte-span IoU so overlapping retrieved/context spans are unioned once and
  cannot produce impossible values above one.
- [x] Implement lossless surface normalization, explicit cross-type adjudication, governed
  predicate admission, relation grounding/usefulness gates, and decision-backed reconciliation.
- [x] Expose the current semantic-policy fingerprint, latest applied per-document policy, historical
  fingerprints, predicate registry, and projected identity decisions through a loopback-only,
  secret-free governance read model and UI.
- [x] Wire the registered semantic hard-invariant gate into fresh rebuild before swap; missing or
  unmeasured required evidence fails closed and preserves the previous live generation.
- [x] Bind each installed rebuild generation to its complete semantic fingerprint triplet and add a
  generation-selected, fail-closed rollback that preserves both the source checkpoint and the
  displaced graph; policy mismatch is rejected before swap.
- [x] Demonstrate policy-change rebuild plus policy-and-graph rollback in the isolated acceptance
  scenario and capture both semantic snapshots in the pinned collection.
- [x] Make rebuild materialization converge on one semantic-policy fingerprint triplet: discard an
  unstable staging pass, retry from an empty graph under the evolved policy, and fail closed when
  the policy does not stabilize within the bounded pass budget.
- [x] Stop the final bounded pass immediately after its first integrity-verified fingerprint drift;
  earlier discovery passes still inspect the complete corpus, but an already-unswappable final
  staging no longer spends model calls on the remaining sources.
- [x] Reuse completed curator and relation-curator decisions only for an exact config, extraction,
  semantic-policy, and fresh-graph materialization-scope match, while creating a new ingest run,
  plan, operations, and receipts for every fresh graph materialization.
- [x] Keep the first sealed exact-policy decision run authoritative across repeated fresh rebuilds,
  include replay methods in ledger snapshots, and normalize legacy replay rows so later stochastic
  redraws cannot replace the root evidence or break multi-generation replay.
- [x] Link every rebuild `after_source` audit back to the exact ingest-run id and document id, so the
  ledger's final integrity outcome carries the verified/failed audit id and staging generation
  instead of retaining the live generation's provisional pre-audit state.
- [x] Preserve the first sanitized ingest failure boundary in the curation job and
  `rebuild.state.json` without overwriting the retained staging graph, validation report, current
  source, or prior progress evidence.
- [x] Make process-interrupted rebuild cleanup crash-durable: checkpoint the exact pre-job semantic
  sidefiles, restore them only when the unchanged live generation proves the swap never began,
  retain interrupted staging, and fail closed across an ambiguous or completed swap boundary.
- [ ] Materialize and run the pinned public diagnostic; record the actual runtime and comparison
  evidence without treating it as a ship gate or a leaderboard result.
- [x] Wait for ADR 0039's verified graph generation before authoritative graph baselines.
- [x] Run the technical plus semantic gates on the isolated live Hobbit generation and retain the
  layered report and representative evidence samples.
- [x] Run a manifest-pinned single-pass pilot for the type-adjudication and relation-curation stage
  switches, retaining construction cost, ledger decisions, graph layers, and completion-free
  recall without presenting the draft fixture as human ground truth.
- [x] Replay the type-adjudication arms against byte-identical durable extraction-unit results and
  prove zero extraction-provider attempts plus manifest-v5 parity.
- [x] Materialize sampled byte-grounding evidence from cited Claim hits by reopening the immutable
  input copy, hashing the complete source and exact interval, and binding every sample to its
  manifest, graph generation, and semantic-policy fingerprint.
- [ ] Establish repeated-run variance or replay every unchanged stochastic decision stage, then
  score the arms with human-adjudicated per-layer labels before creating acceptance-eligible
  `model_stage_ablations.v1` evidence.
- [ ] Move ADR 0040 from Proposed and mark ADR 0039 operationally complete only after their
  independent exit criteria pass.

The immediate program state is therefore: **the isolated live Hobbit diagnostic and its clean,
verified one-corpus Phase 1b baseline are complete. The read-path ranking defect is corrected and
the generation-bound rollback boundary is implemented, model-free tested, and live-demonstrated.
All synthetic determinism arms are pinned. The temporal-chat baseline is now bound to a verified
generation with measured fingerprints; it preserves all five Golden spans and all 60 byte-verified
citations, and its controlled identical reingest, source-order, rebuild, restart, and rollback
materializations have zero identity, predicate, or relation churn. Predicate quality, adjudicated
entity/relation quality, acceptance-eligible model-stage ablations, remaining real-corpus scenario arms,
multi-corpus threshold locking, and the public live diagnostic remain pending. After regenerating
the three byte-grounding sidecars as sample-level evidence and normalizing the two completed policy-
change receipts against their retained recompute artifacts, the current schema-validating
collection is 34/67 ready, 33 missing, and 0 invalid
(`~/.marginalia/quality-runs/adr0040/acceptance-status-current.json`, 2026-07-18 16:43, the newest
readiness artifact). Collection readiness moved in this order:

| Readiness (ready / missing / invalid of 67) | Artifact | Timestamp |
| --- | --- | --- |
| 32 ready (missing/invalid not recorded) | `hobbit-current-diagnostic.json` from the Hobbit GLM-5.2 quality run | 2026-07-18 05:45 |
| 32 / 35 / 0 | `adr0040/temporal-chat/temporal-correction.json` | 2026-07-18 06:10 |
| 27 / 35 / 5 | `adr0040/acceptance-status.json` | 2026-07-18 10:47 |
| 29 / 33 / 5 | `adr0040/acceptance-status.json`, dated refresh copy | 2026-07-18 15:50 |
| 34 / 33 / 0 | `adr0040/acceptance-status-current.json` | 2026-07-18 16:43 |
| 28 / 33 / 6 | `adr0040/acceptance-status.json`, dated copy taken before the repoint | 2026-07-28 |
| 34 / 33 / 0 | `adr0040/acceptance-status.json`, dated copy taken after the repoint | 2026-07-28 |

The 32 → 27 step is the five legacy counter-only sidecars that the later strong evidence schemas
invalidated; the 27 → 34 recovery is those sidecars being regenerated as sample-level evidence.
This is progress evidence, not ADR acceptance.**

### 2026-07-28 addendum — evidence recovery, and what remains human-blocked

Re-running the ADR-recorded collection on 2026-07-28 reported 28 ready, 33 missing, and 6 invalid
(the dated status copy taken before the repoint), not the recorded 34/33/0. The six regressions are a path move, not evidence loss: the literary and
private organizational corpus result directories were relocated under a gitignored `_archive/`
tree, and only the slots pinned by absolute path broke. Every slot pinned by a path relative to the
collection file still resolved. Re-hashing all six artifacts at their archived locations reproduces
the pinned SHA-256 digests byte for byte, so a repointed collection
(a dated copy of the acceptance collection) restores exactly 34 ready, 33 missing, and 0 invalid
(the dated status copy taken after the repoint). The original collection and its status artifact are retained
unchanged.

**Zero new evidence slots were filled by machine work in this pass.** The 34 count is recovery of
previously recorded readiness, not progress. The two pinned literary-corpus reports carry complete
`semantic_quality.v1` layers with `integrity_status: verified` at graph generation
`8469a06c-7290-4c5b-b8d9-dabeece20e70`, and both record `adjudication: not_supplied` and
`authoritative: false` — machine-scored evidence for the `stored_report` and `snapshot:baseline`
slots, which is what those slots take, and not a substitute for the separate `adjudicated_report`
slot.

Every remaining missing slot was checked against what a read-only pass can honestly produce:

| slot class | why it cannot be machine-filled read-only |
| --- | --- |
| eight literary scenario snapshot arms | each needs a real write run — fresh rebuild, identical re-ingest, reordered sources, provider restart, policy change, rollback |
| `policy_change` (literary) | derived from the `policy_before`/`policy_after` arms above |
| `ablations` (three corpora) | needs measured model-stage ablation re-runs |
| `adjudicated_report`, `review_coverage` (four corpora) | needs a named human adjudicator; an agent verdict is never human adjudication |
| `threshold_policy` | an owner decision; no locked multi-corpus threshold policy exists yet |
| `public_diagnostic` | the public benchmark lane is cached but unmaterialized and has no aggregator |
| all private organizational corpus slots | writer-fenced by design |

Two machine-prepared inputs were produced to shorten the human step, both held outside the
repository and both marked *machine-prepared, pending owner sign-off*:

- a 33-item blind judge-calibration kit, stratified over `(tier, machine verdict)`, with the
  machine verdicts held in a separate answer key and a Cohen's kappa script whose nine-check
  synthetic self-test passes. The 105 retained judged question/answer pairs cover only 33 distinct
  questions, because the ten smoke runs re-ask one 8-question dataset; the population is therefore
  deduplicated to one occurrence per question so `n` counts independent observations, which caps
  the sample below the 40-item target. All three gating-corpus baselines ran with the judge
  skipped, so this calibrates the judge on synthetic and smoke traffic only, at an `n` small enough
  that the result is a first reading rather than a passed gate.
- one adjudication packet per corpus, carrying the pinned evidence paths with re-verified digests,
  the machine-observed semantic state, and the exact `semantic_adjudication.v1` and
  `semantic_review_coverage.v1` field contracts as owner worksheets. No adjudicated row is
  prefilled: every row is keyed by a `candidate_id` bound to the corpus candidate ledger, which the
  stored reports do not carry, and inventing one would be fabrication.

ADR 0040 therefore stays **Proposed**. The remaining distance is human adjudication, an owner
threshold decision, and write-path scenario runs — none of which machine work can supply.

### Initial stored-graph diagnostic slice

The first implementation slice is deliberately read-only and does not alter extraction, identity,
predicate, or relation decisions. `src/marginalia/semantic_quality.py` reports separate surface,
type, identity, predicate, relation, and recall-cost layers. The recall layer accepts only complete
`recall_cost.v1` samples from the ordinary read path, aggregates query-embedding and deterministic
retrieval and result-projection latency at p50 and p95 together with result counts and bytes, and
fails the hard invariant if any measured recall uses a completion or generated token. Ordinary
recall also installs a runtime prohibition whose probe is checked by every built-in completion
adapter before provider execution, so an attempted completion fails the recall instead of leaving a
literal zero in the report. Missing, fallback, or partial accounting remains explicitly
`not_measured`. The loopback-only POST acquires the server writer lock with a one-second bound and,
once acquired, holds both that lock and the store's shared audit/write lock, so direct in-process
SDK writes cannot race the
snapshot. Before reading topology, it runs ADR 0039's physical-adjacency audit for the same graph
generation under those locks. A cached `verified` sidecar is not sufficient. Only a fresh,
complete, same-generation audit proof with an independently completed physical-adjacency scan
permits stored-edge metrics such as Claim anchors, live endpoints, materialization, self-loops,
and isolation to be measured. The stored-graph evaluator also loads the vault's durable
`PredicateRegistry` inside that protected scan boundary and treats both canonical and provisional
labels as registered. A missing registry is a measured empty registry; an invalid registry aborts
the audit without returning partial semantic evidence. Placeholder predicates remain an
independent hard-invariant failure even if registry coverage is available. The mutating POST is
unavailable while the server drains.

The same complete scan emits `semantic_snapshot.v1`. It contains only sorted, kind-separated
SHA-256 member identifiers for canonical primitive identities, governed semantic predicate labels,
and active non-structural Claims; source text and titles are not exposed. Primitive members hash
the canonical primitive type plus the conservative exact surface key and a duplicate ordinal;
Claim members hash the resolved semantic triple, including the typed literal value when present.
Physical node/Claim ids and extractor-authored descriptive prose are deliberately excluded because
they are vault-local construction artifacts, not cross-run semantic identity. The declared
`member_encoding` is validated fail-closed so evidence produced by the former physical-id encoding
cannot be compared silently with canonical semantic evidence. Each snapshot also binds
the complete layered config, extraction, and post-run semantic-policy fingerprints shared by the
latest completed document runs that materialized the graph. It never substitutes the current
configuration for missing materialization evidence. A comparison is stable
only when all three populations and all three fingerprints match; missing fingerprint evidence
fails closed. A snapshot is
`not_measured` unless the graph generation passed the fresh complete ADR 0039 audit. The offline
`marginalia quality churn BEFORE AFTER` command validates both snapshots and emits
`semantic_churn.v1`, including exact additions, removals, and Jaccard-distance-style churn shares
per semantic population. `--require-stable` makes any change fail the command. This turns repeated
read, identical-reingest, source-order, rebuild, and rollback comparisons into executable evidence;
it does not claim those scenario arms have run merely because the comparator exists.

`src/marginalia/semantic_acceptance.py` is the immutable matrix-evaluation boundary. It consumes
only captured evidence and never opens a graph or calls a model. Its exact-versioned contract
requires one unique manifest-bound corpus for each gating class, a predeclared threshold policy
bound to the materialized semantic-policy fingerprint, complete fresh
technical reports, human-adjudicated per-layer metrics, completion-free recall, byte-grounding and
review coverage, a bounded chat-corpus temporal-correction proof, on/off model-stage ablations, and
every churn scenario arm. The temporal proof must preserve source time and correction order, make
the latest correction retrievable, and explicitly avoid claiming first-class valid-time support.
A material semantic-
policy change must be bound to before/after snapshots, distinct policy fingerprints and graph
generations, rejected incompatible reuse, and recomputation; hashes plus assertions alone do not
pass. A fresh-rebuild arm must also materialize a distinct graph generation. `marginalia quality
acceptance BUNDLE --require-ready` emits the deterministic result and
fails unless the internal matrix passes and the reproducible public diagnostic is complete. The
public lane remains reported separately and does not change the internal gating result. The
evaluator's existence does not satisfy Phase 7: the real four-corpus runs, locked threshold
evidence and the public diagnostic are still open.

The same module owns `semantic_acceptance_collection.v1`, the input boundary for assembling those
real runs. A collection names the four unique corpus classes and pins the raw SHA-256 of every
manifest, technical and adjudicated report, nine scenario snapshots, policy-change receipt,
byte-grounding report, review-coverage report, ablation report, temporal-correction report,
threshold policy, and public diagnostic. Paths may remain operator-local; neither
`acceptance-status` nor the materialized bundle emits them. `marginalia quality acceptance-status
COLLECTION --require-ready` distinguishes missing evidence from invalid or byte-drifted evidence.
`marginalia quality acceptance-collect COLLECTION --output BUNDLE` reopens and rehashes every
artifact, validates its JSON outer shape, materializes the path-free matrix, and then runs the same
fail-closed evaluator before writing. A status check is not a time-of-check bypass: collection
reopens and verifies every artifact again. This collector makes the 67 evidence checkpoints
executable; it does not fabricate any still-pending scenario or human-review result.

The evidence slots themselves are strongly bound rather than accepting summary counters. A
`byte_grounding_evidence.v1` artifact names its corpus, manifest, graph generation and semantic
policy and retains unique sampled relation ids, source hashes, byte ranges, excerpt hashes and
per-sample verification. `semantic_review_coverage.v1` binds unique primitive and predicate-state
sample ids plus decision hashes to the same corpus/generation, an adjudication hash, a timestamped
human reviewer, and never treats a model label as human review. Policy-change evidence records
the exact changed fields, before/after run ids, generations and fingerprints, plus independently
hash-pinned incompatible-reuse rejection and recomputation receipts. `model_stage_ablations.v1`
records both disabled and enabled config/policy fingerprints, generations, quality values,
construction calls/tokens, embedding work and elapsed ingest time; a scalar claimed incremental
cost is insufficient. Finally, `semantic_threshold_policy.v2` can be `locked` only per corpus after
human-reviewed adjudication and at least two baseline runs, with both adjudication and variance
artifacts pinned. Corpus, manifest, generation or fingerprint drift fails the associated matrix
gate. This deliberately invalidates older counter-only evidence even when its file hash is valid.

When the fresh audit fails, the evaluator does not reinterpret Ladybug's stored endpoint
properties or reader-heal the graph. It still measures the trustworthy node/facet subset: exact
same/cross-type title collisions, surface normalization flags, primitive distributions, Claim
object shape, raw predicate vocabulary/singletons, and placeholder predicates. Every
topology-dependent metric and invariant becomes `not_measured`, the edge population is `null`, and
the evidence is incomplete. Integrity-sidecar contract version 2 invalidates legacy green
sidecars that predate the physical-adjacency audit.

Edge-count and edge-type gates in `scripts/ingest_quality_check.py` consume only the population and
materialized topology captured by that same locked semantic scan. They do not borrow `/stats` or
graph-overview values from another moment. The script labels its remaining stats, overview, and
ledger aggregation as diagnostic and non-atomic; those surfaces do not become one authoritative
snapshot merely because the semantic topology proof passed.

A fresh technical verification still does not make the semantic baseline authoritative while ADR
0039's operational and realistic-scale acceptance remains open. Every binding invariant not yet
implemented is emitted as `not_measured`, so the scoped hard-invariant verdict remains
`incomplete` rather than claiming a semantic pass.

The explicit-run ledger evaluator adds the first Phase 1a semantic projection without changing
writes. It records the selected run and plan ids plus a canonical SHA-256 of the selected records;
its scope-qualified `candidate_ledger_plan.v1` report variant; run terminal/open/failed/unknown
state (including crash-recovery `abandoned` as failed); exact and discovery surface collisions;
proposed and planned type
distributions; raw, derived/proposed, and planned predicate distributions; planned relation
dispositions, object shape, candidate anchor shape, and internally observable endpoint resolution;
and optional ordinary-recall cost. Source-surface
preservation, B-cubed identity quality, type accuracy, direction, and semantic grounding remain
explicitly `not_measured`. References not present as candidates in the selected runs remain
unverifiable because they may be valid nodes from an earlier graph generation. The report never
claims authority, and does not claim complete source framing because it receives parsed records
rather than the candidate-ledger JSONL bytes.

Endpoint resolution is intentionally narrow: only a node candidate selected for `create_node` by
the selected plans is reported as internally resolved. An observed node candidate with another
disposition, and a reference absent from the selected candidates, remain separately
`not_measured`; either may already resolve through prior graph state that the ledger cannot prove.
Anchor shape distinguishes valid, contract-legal absent, malformed, and unavailable evidence.
Only malformed partial anchors fail the shape check; whether accepted relations must all carry an
anchor remains a Phase 4 policy measurement. Multiple commit plans sharing one resumed run id make
pre-apply ordering `not_proven`, because earlier terminal candidate records cannot be assigned to a
specific attempt without an additional durable marker.

These slices do **not** satisfy Phase 1a because the preserved, framing-verified incident run found
that every legacy commit plan was recorded after some terminal candidate rows, and the remaining
adjudicated measurements plus materialized public diagnostic are still open. They do **not** satisfy
Phase 1b because ADR 0039 has not published an
authoritative baseline generation and no clean graph has been compared with its pre-commit
evidence. All phase exit criteria below remain binding.

Newly extracted node candidates now carry a `surface.v1` evidence record with the extractor's
exact string, conservative exact key, broader discovery-only key, selected canonical title,
aliases, and normalization flags. The record serializes into candidate-ledger payloads while the
candidate id remains based only on type, canonical title, and content. That evidence slice has since been wired into the live identity path under the
`entity_resolution.v2` contract. `exact_surface_key()` in `src/marginalia/semantic_surface.py` is
now the single conservative key shared by intra-batch collapse
(`consolidate/_dedup.py::_normalized_title`), Tier-0 reconcile against the committed store
(`resolve/__init__.py::_normalized_title`), the off-graph authority index and its alias projection
(`reconcile/authority.py::_normalized_title` and `alias_canonical_map`), and enumerate-mode
candidate deduplication (`extract/__init__.py`). The remaining caveat is therefore narrower than
this section originally claimed, and it is now specific to the candidate-id formula: a candidate id
is still derived only from type, canonical title, and content, so neither the exact nor the
discovery key participates in candidate identity. Changing that formula, and retaining every
repeated mention that cross-block candidate deduplication collapses, remain Phase 2 work.
The explicit-run evaluator consumes this record when present, recomputes both keys and flags through
the shared contract, and fails the measured preservation check on a version/key/canonical/flag
mismatch. Legacy candidate rows without a separate record remain `not_measured`; their canonical
title collisions are still diagnostic but are not relabelled as source-surface evidence.

The candidate-ledger scan is only an evidence-integrity prerequisite for Phase 1a. Its result keeps
the parsed records together with total and non-empty line counts, bounded malformed-line samples,
trailing-partial detection, ledger versions, and a SHA-256 of the complete file. A malformed or
interrupted ledger, or one with missing or unrecognized record versions, is explicitly `incomplete`;
downstream measurement must not reinterpret its readable subset as a complete pre-commit population.
Malformed-line samples and the vault-local path are private diagnostics and require an explicit
redaction policy before entering a public API or evidence bundle. This does not yet compute any
Phase 1a semantic metric or change graph writes.

`evaluate_ledger_scan` now binds that complete-file proof to the same explicit-run semantic report:
the report carries the whole-file SHA-256, framing/version/malformed/trailing state, and the selected
record SHA without exposing the vault-local path or malformed source-derived samples. A parsed-row
caller remains explicitly `not_measured` for framing; an incomplete scan fails the framing check.
Even a complete bundle remains non-authoritative for stored graph semantics and cannot satisfy
Phase 1a until the adjudicated fixture and required per-layer measurements are supplied.
The loopback-only semantic endpoint exposes this same evaluator through the explicit
`candidate_ledger` variant with required run ids; the default remains the fresh stored-graph audit.
The request holds the server writer lock plus the shared integrity scan guard, so a running ingest
inside the active daemon cannot append evidence across the scan boundary. A separate process is not
excluded by this in-process lock; the single `read_bytes` snapshot remains pinned by its full-file
hash, and a concurrently torn tail fails closed as incomplete evidence.
The offline `marginalia quality ledger VAULT --run-id RUN ...` command exposes that exact
evaluator without opening Ladybug or starting a server. It requires explicit runs, emits
deterministic JSON, and refuses to replace an existing evidence file unless `--force` is supplied.
This is the audit boundary for preserved incident snapshots; the HTTP endpoint remains the live,
in-process projection.

The versioned `semantic_adjudication.v1` contract now supplies the missing human-labelled boundary.
It strictly binds every labelled entity and relation to a proposed candidate in the selected ledger,
rejects unknown/missing fields and duplicate ids, and records fixture, instruction, and adjudicator
identity. Entity labels include closed primitive truth, expected cluster, canonical candidate,
canonical title, and rationale. Relation labels include disposition, admitted predicate, directed
subject/object truth, literal-versus-topology truth, grounding, usefulness, and rationale. A missing
plan operation is an explicit coverage failure rather than an invented prediction.

The evaluator reports type accuracy and macro-F1; B-cubed precision/recall/F1; pairwise false,
cross-type false, and missed merges; canonical, alias, and canonical-title assignment accuracy;
relation decision, predicate, direction, object-kind, and object-value accuracy; and admission,
grounding, and usefulness precision/recall/F1. These statistical measurements do not acquire an
invented threshold. Existing hard safety rules still fail immediately when labelled evidence shows
a cross-type merge, accepted ungrounded relation, wrong confirmed direction, or wrong object kind.
Every supplied adjudication document, registered-predicate set, and recall-sample set is represented
in report evidence by its canonical SHA-256, byte count, logical type, and count without echoing raw
evidence.

`tests/fixtures/semantic_quality/adversarial-ledger.v1.json` is the first self-contained executable
fixture. It covers aliases and missed aliases, same-surface cross-type senses, work/object senses,
type ambiguity, confirmed inverse direction, literals, structural noise, unsupported inference,
grounded misses, and zero-completion recall. Its intentionally flawed current plan produces a fully
measured failing baseline: type accuracy `0.9` / macro-F1 `0.733333`, B-cubed F1 `0.9`, one false
cross-type merge, one missed merge, predicate accuracy `0.857143`, direction accuracy `0.857143`,
object-kind accuracy `0.857143`, and grounding/usefulness F1 `0.8`. The fixture is a regression
instrument, not a claim that these values are acceptable. CLI callers can bind the three evidence
files with `--adjudication`, `--registered-predicates`, and `--recall-samples`; the loopback-only
candidate-ledger endpoint accepts the same values and returns invalid contracts as a client error.

### 2026-07-16 preserved pre-commit incident evidence

With no Marginalia server or writer process running, the complete 162,115,633-byte LOTR candidate
ledger was copied and byte-compared into the private incident bundle at
`.marginalia/incidents/2026-07-16-integrity-failure/semantic-0040/`. The source and snapshot both
hash to `5332b011b097fc6b932e786cb4ed23b5ffdf681ba635d20207c1ece2a1c9eaac`.
The bundle contains a captured config, a provenance manifest, and the deterministic offline report;
it does not contain a second live graph. The config hash is explicitly labelled
`captured_after_selected_runs`: legacy run-start rows did not preserve an ingest-time config or
semantic-policy fingerprint, so the bundle does not invent that missing fact.

The explicit selection covers the original four completed book runs and their four plans. The
report hashes 92,861 selected records to
`3e8bc36aabc7904d69134fd5e616e7cfff7d9af18ceb613b7d2174f392f02340`. Its framing and selection
are complete, but it is non-authoritative and its measured invariant verdict is `failed`:

| Pre-commit measurement | Result |
|---|---:|
| Unique proposed nodes | 7,688 |
| Unique proposed relations | 21,682 |
| Planned operations | 29,387 |
| Same-type exact collision groups | 973 |
| Cross-type exact collision groups | 357 |
| Raw proposed predicate vocabulary | 3,537 |
| Planned accepted predicate vocabulary | 2,855 |
| Planned accepted singleton predicates | 1,893 (66.3047%) |
| Planned accepted relations | 9,099 |
| Accepted relations with well-formed candidate anchors | 9,099 |

The first failed boundary is `commit_plan_preapply_order`: all four legacy plans followed terminal
candidate rows. This is incident evidence for the old pipeline, not a failure of the corrected
plan-before-apply implementation, whose regression tests now pin the durable plan before any graph,
queue, terminal-ledger, or receipt mutation. Registry coverage, source-surface preservation,
adjudicated type/identity/relation quality, and recall cost remain honestly `not_measured` for this
legacy selection.

### 2026-07-16 copied-vault diagnostic evidence

The corrected loopback-only POST endpoint and
`scripts/ingest_quality_check.py --require-semantic-quality` were run against an isolated copy of a
representative multi-document evaluation vault, never the live graph.
The copied graph generation `e6e3d0d7-3df5-403e-80d5-b9b90812d6b4` carried a legacy version-1
sidecar marked `verified`. The fresh ADR 0039 audit scanned the complete physical adjacency
population and found 218 `adjacency_property_mismatch` issues. Contract version 2 persisted the
generation as `failed` and writer-fenced; the strict semantic checker exited non-zero with an
`incomplete` verdict.

Only the node/facet measurements are admissible semantic diagnostics from that run:

| Measurement layer | Result |
|---|---:|
| Stored non-infrastructure nodes | 1,459 |
| Primitive entities | 352 |
| Claims | 1,059 |
| Invalid primitive/support types | 0 |
| Invalid Claim object shapes | 0 |
| Same-type exact duplicate title groups | 0 |
| Cross-type exact title conflicts | 5 |
| Raw predicate vocabulary | 103 |
| Singleton predicate labels | 42 (40.7767%) |
| Placeholder predicates | 0 |

No Claim-anchor, bridge, provenance-edge, relation-endpoint, topology-coverage, isolation, or
degree result from this copied generation is accepted as a semantic failure or pass. Those metrics
remain `not_measured` until a technically verified generation exists. This evidence validates the
authority boundary; it does not satisfy Phase 1a or Phase 1b.

### 2026-07-17 isolated live Hobbit diagnostic

One clean Hobbit ingest was completed in the isolated `hobbit-quality-glm52` vault without
enqueueing a duplicate item. Item `4-c8b25211` processed all 44 source Blocks and installed verified
graph generation `5c0a1bea-f31a-48f2-b29c-085100e75393`. The configured embedder remained
`desktop/qwen3-embedding-4b`, dimension 2,560, batch size 32, and one concurrent batch. The first
attempt had failed at an invalid literal inverse request; the owning semantic-plan boundary now
converts that impossible endpoint swap into one durable `queue_direction_conflict` relation rather
than aborting the document. The focused regression and the resumed live item both passed.

The read-only post-ingest audit scanned the complete verified graph and passed every currently
registered rebuild invariant. It found 501 nodes, 325 Claims, 131 primitive entities, 83
claim-backed topology edges, no invalid primitive/support type, no invalid Claim object shape, no
dead relation endpoint, no missing Claim bridge/provenance/topology edge, no unregistered or
placeholder predicate, no exact same-type or cross-type title collision, and no entity isolated by
the authoritative definition (no active Claim endpoint and no `schema:mentions`). The retained
report is
`ingest-quality-after-timeout-fix.json` in the Hobbit GLM-5.2 quality run, SHA-256
`55b5f105feff4a0a9c1fa2578c923ad5a7b2447a9e67d569695c0c8533720821`.

Technical correctness did not imply semantic acceptance. The same report measured 133 raw
predicate labels across 325 assertions (409.231 per 1,000), with 120 singleton labels (90.2256%).
Only 83 assertions materialized topology and 55 of 131 primitive entities had no topology edge,
although they remained properly Claim/source anchored. These are layered diagnostics, not a single
hidden quality score.

The matching private Hobbit Golden/Evolve dataset was run against the already populated endpoint
with `--never-ingest --no-judge`. All 15/15 source targets resolved and all 130/130 returned
citations passed exact byte verification. The provenance harness originally measured only its
legacy explicit `gold_spans` field while this dataset declared quote-based `gold_targets`; that gap
is now closed by resolving each unique quote to its exact hashed byte span. The corrected report
measured exact gold-span recall@10 on only 7/12 answerable questions (58.33%); those same seven spans
were intact in one retrieved Block. The missing five include both observed hard answer failures:
Bilbo taking the Arkenstone and Thorin's final reconciliation. Their gold bytes existed and were
uniquely verified, but none of the ten retrieved hits overlapped them. A subsequent complete-ledger
trace moved the first failed boundary earlier than retrieval: the raw extractor emitted all five
facts and the relation curator approved them, but inconsistent `Concept`/`Agent` assignments for
Bilbo, Gollum, and Thorin caused D7 to queue their literal Claims as `queue_type_conflict`. The
deterministic `must_contain` backstop was
6/12 on answerable questions; it remains explicitly non-authoritative because lexical wording can
mark a semantically adequate paraphrase false. The negative-control shoe-size answer correctly
abstained and is not included in either denominator.

Ordinary recall used zero completion calls and zero generated tokens. Across the 13 captured calls,
query embedding p50/p95 was 239.781/379.055 ms, deterministic retrieval p50/p95 was
352.487/464.032 ms, and total completion-free recall p50/p95 was 619.489/760.812 ms. Each query
returned ten results and projected about 119 KB at p50, so the current read path satisfies the
zero-completion invariant but is not yet evidence of near-zero latency or high precision. The
corrected byte-recall report is `deterministic-byte-recall.json`, SHA-256
`8f52a6ad37dbffd75205fd3fb232fb9453e7d83691d105be080b55ddff94b50d`.

The quality auditor's ingest-wait deadline and per-request deadline are now separate. A new
`--request-timeout-s` (300 seconds by default) applies to the potentially expensive complete-store
audit calls, and timeout errors name the exact HTTP boundary instead of escaping as a bare socket
exception. The supervised post-fix audit completed in nine seconds.

A second fresh-generation rebuild then exercised the Phase 2 type-adjudication boundary over the
same preserved Hobbit extraction evidence. All 44 extraction units were reused, 286 type decisions
were recorded (241 confirmed, 32 corrected, 13 queued for review, zero malformed), and verified
generation `8469a06c-7290-4c5b-b8d9-dabeece20e70` replaced the prior graph only after the
post-close and post-swap audits passed. The complete graph now contains 1,037 nodes, 794 Claims,
198 primitive entities, 245 claim-backed topology relations, zero dead endpoints, zero invalid
Claim shapes, zero missing Claim bridges/provenance/topology edges, and zero entities isolated by
the authoritative Claim/source-aware definition. The retained report is
`ingest-quality-type-adjudication-v2-8469a06c.json` in the Hobbit GLM-5.2 quality run,
SHA-256 `96a840bf13fcdfbf304cf1b8bdd11838ea111859891ffcd091bb122331ee6a19`.

The larger graph remains semantically diagnostic rather than clean. Fifty-six of 198 primitives
have no topology relation despite valid Claim/source anchoring. Raw predicate vocabulary increased
to 290 labels over 794 assertions; 245 labels are singletons (84.4828%). This is lower singleton
share than the first graph but substantially more raw vocabulary, so it is evidence of long-tail
fragmentation, not a quality pass.

The first read-only Golden/Evolve run on this generation still recovered only 7/12 exact gold spans
at `k=10`: three earlier misses were gained and three earlier hits were lost. All five current
misses were nevertheless materialized and appeared at ranks 14–28. The failed boundary was a
post-fusion answer-Claim partition in `query.search_claims`: after vector/BM25/title/answer RRF had
produced a score, every term-overlapping Claim was moved ahead of the fused order, so result rank
no longer matched the reported score and same-subject Claims flooded the cut. Removing that second
partition leaves answer coverage as one bounded RRF leg. The same graph then recovered 12/12 exact
gold spans, all intact, and reverified 130/130 returned citations against source bytes. The retained
deterministic report is
`tests/golden/results/hobbit-glm52/hobbit-rrf-only-8469a06c-118268fea5b2/deterministic.json`,
SHA-256 `ef9c12fa080dd156b78a477b5ab61b3b127cf8be72d35bf5ca2fd32dc8ce5d57`.

The corrected recall path still made zero completion calls and generated zero tokens across 13
captured calls. Query-embedding p50/p95 was 211.849/307.601 ms, deterministic retrieval p50/p95
was 675.841/803.553 ms, and total p50/p95 was 957.080/1,126.747 ms. This satisfies the
completion-free cost invariant but is not near-zero latency. The deterministic `must_contain`
proxy improved directionally from 9/13 to 10/13 versus the immediately preceding run, but remains
non-authoritative and underpowered; exact byte-span recall is the reliable result here.

The 2026-07-17 explicit-vault rerun closed an evaluation-target ambiguity in the Golden/Evolve
harness. `--vault-path` now propagates one `X-Marginalia-Vault` selector through dataset identity,
recall, ask, semantic-quality, and live floor requests; manifest v4 records only the selector's
SHA-256, never the personalized path. Pointing the Hobbit dataset at the LOTR vault failed at the
identity boundary with exit 74 before recall, ask, or ingest. Pointing it at the isolated Hobbit
vault completed all 13 questions under `--never-ingest --no-judge`: 12/12 answerable gold spans
were retrieved, 130/130 returned citations reopened against the source bytes, and all 13 recall
samples made zero completion calls and generated zero tokens. This rerun measured total recall
latency p50/p95 at 996.535/1,385.562 ms. The graph generation remained
`8469a06c-7290-4c5b-b8d9-dabeece20e70`, and the ingest queue remained inactive with item
`4-c8b25211` terminal `done`.

Two observability failures found during this run were corrected without changing graph decisions.
Reconciliation proposal JSON had paired `same=true` with the full permissive candidate component,
making rejected relatives and container/contained places look merge-eligible; it now exposes the
candidate, adjudicated-survivor, and corroborated subsets separately. The apply path already used
the adjudicated/corroborated subsets, and no proposal was applied to this graph. Also, semantic
quality audit requests no longer wait unboundedly behind a long verified-snapshot job: they acquire
the stable-snapshot authority for at most one second and otherwise return retryable
`409 audit_busy`, naming the blocked boundary.

The 2026-07-18 supervised read-only rerun did not enqueue another Hobbit item. Item `4-c8b25211`
remained the sole completed item, with all 44 extraction units complete, and graph generation
`8469a06c-7290-4c5b-b8d9-dabeece20e70` remained verified. The complete audit again passed queue,
structural-title, authoritative-isolation, byte-anchor, bridge, provenance, endpoint,
materialization, and registered-predicate checks. Its retained baseline hashes to
`ca9d12344a23c327d7c29b863380f8d657ba961aef2596398c9c8fd787256f21`. Requiring the final
semantic verdict correctly failed closed because owner adjudication remains absent; rerunning as a
diagnostic passed, while preserving `incomplete` as the authoritative verdict.

The matching explicit-vault Golden/Evolve rerun used `--never-ingest --no-judge`, recovered and
kept intact all 12/12 answerable gold spans, produced perfect single-source session recall through
`k=30`, and reverified 130/130 citation byte ranges. Direct inspection found all 12 generated
answers consistent with their expected facts and the shoe-size negative control correctly
abstaining, but this AI inspection is not substituted for owner adjudication. Ordinary recall made
zero completion calls and generated zero tokens; query-embedding p50/p95 was 259.658/339.159 ms,
deterministic retrieval p50/p95 was 725.471/897.764 ms, and total p50/p95 was
1,045.547/1,147.590 ms. The report, deterministic, semantic-quality, and response artifacts hash
to `14f9d8b18fad5b4ec3c1f59e43696ecfcb80a47018456a88d807efc87aa191bc`,
`322aa3115a17d6013913cf07fe473b83cb237da6306fa031043b03e15528674a`,
`69392b4b3a57e3fc12c230ea74b86502b8457ae1ae996f12ef76b893fe62fc55`, and
`e1cec450c7fbb77b65b5971c90739be20b53707eb6fa9b70ff4d4d10ef5a2c80`. Predicate
fragmentation remains the dominant measured semantic warning: 290 labels and 245 singletons
(84.4828%). The original Hobbit item predates `construction_cost.v1`, so its construction cost is
honestly unavailable rather than reconstructed from a truncated event tail.

This is a **one-Hobbit diagnostic**, not ADR 0040 acceptance. The semantic verdict remains
`incomplete`: human-adjudicated type/identity, direction, grounding/usefulness, literal/topology,
and uncertainty evidence is still absent; no repeated-ingest, source-order, semantic-policy-change,
or multi-corpus gate was run. The single-document source-session metric is necessarily trivial and
is not used as a relevance score. This diagnostic neither locks thresholds nor covers the other
required corpus classes, identical re-ingest, source-order variation, repeated-run stability, or the
live public LongMemEval-compatible comparison.

The first live `semantic-adversarial` baseline added a separate six-source synthetic corpus for
aliases, cross-type namesakes, same-display-surface senses, literal/topology distinctions, inverse
phrasing, Unicode, unsupported inference, and negative controls. Its isolated graph completed all
six ingests with 47 nodes, 77 edges, 22 Claims, 13 primitive entities, and ten claim-backed topology
relations. Every measured structural invariant passed, while human-dependent semantic checks
remained explicitly unmeasured. One real type error was retained as evidence rather than repaired:
the source defines `Cobalt Council` as an Activity, but the baseline graph typed it as a Concept.

The first Golden floor attempt exposed a query-provenance defect before semantic scoring. Document
hits carried the whole-file SHA-256 but emitted byte range `0:0`, so seven otherwise valid citations
failed exact re-slicing. The owning defect was `Vault._provenance_for_node`, which understood Block
`byte_end` but ignored a Document's `byte_length`. Document provenance now covers
`[0:byte_length]`; the fallback is type-scoped and does not make missing Claim/primitive anchors
look valid. A focused regression pins both behaviors. Repeating the same run with `--never-ingest`
kept the queue at exactly six completed items and verified 130/130 citations with zero unverifiable
results. Across 13 recall samples it made zero completion calls; total latency p50/p95 was
289.754/417.591 ms. The retained report is
`tests/golden/results/semantic-adversarial/adr0040-baseline-v1-f0e050cbb173/report.json`, SHA-256
`494a6e587c74704878fa4de9f883a81ae47759096435d3ccb936f9b2abfc894f`.

This synthetic run is also diagnostic, not acceptance. Its 17 raw predicate labels over 22
assertions and 94.1176% singleton share are expected to be unstable at this tiny scale; more
importantly, no human adjudication, scenario churn arms, policy-change evidence, stage ablations, or
locked threshold policy exists yet. The fail-closed acceptance collection therefore remains
incomplete rather than converting the technically clean baseline into a semantic pass.

The current-code identical-reingest arm was then run against the dedicated synthetic vault only
after the harness proved the complete six-document byte-hash multiset matched the dataset. The
baseline capture bound verified generation `89142fab-fcc8-465b-93af-35e1f8f22c4c`, semantic-policy
fingerprint `edcf1527bca001bda86f6c1135b124b0473859f14eada855cfb7f0615de5d0fa`, and populations of
3 identities, 2 governed predicates, and 2 relations. It measured 13 ordinary recalls with zero
completion calls and re-sliced 127/127 captured citations successfully. The explicit reingest
enqueued exactly six owned items; all six completed in 33 seconds, the corpus identity matched
again, and the graph generation and 3/2/2 semantic populations remained unchanged. The offline
comparison reports `0.0` churn for every dimension with all three fingerprints equal. The pinned
captures are under
`tests/golden/results/semantic-adversarial/adr0040-current-baseline-f0e050cbb173/` and
`adr0040-identical-reingest-current-f0e050cbb173/`; the durable comparison is
`~/.marginalia/quality-runs/adr0040/semantic-adversarial/current-identical-reingest-churn.json`.
This closes only the synthetic identical-reingest arm. It does not substitute for source-order,
provider-restart, fresh-rebuild, policy-change/rollback, ablation, or human-adjudication evidence.

The current fresh-rebuild arm then exposed and corrected a third-generation replay defect. An
initial rebuild installed verified generation `a129761f-7e7f-4c25-85b0-465258962a59`, but the
semantic snapshot fell from 3/2/2 to 0/0/0 even though all three fingerprints matched. The
physical gate passed because the graph was structurally valid; offline churn correctly failed at
`1.0` in every semantic dimension. The ledger showed that a second-generation `policy_replay` run
was selected, but `resume_snapshot()` had omitted replay methods. Historical replay rows also did
not always retain `original_method`, so the third materialization redrew curator verdicts; slightly
different predicate definitions then conflicted with the pinned registry and queued every
relation. The previous generation was restored through the generation-bound rollback before any
retry.

The owning correction makes the first sealed run under one exact document/config/extraction/
semantic-policy/scope identity authoritative, includes `resume_replay` and `policy_replay` in the
default verdict snapshot, and reconstructs missing node-versus-relation origins from the complete
structured relation evidence. Focused tests cover legacy rows, a later valid redraw that must not
replace the root, and three consecutive materializations without curator calls. Rebuild
`rebuild-cdec247dfc8e` then installed generation
`0352d4e8-485c-48f9-996b-b9bb3c212044`: 6/6 sources, final and post-swap audits verified,
`writer_fenced=false`, semantic swap gate passed, and exact 3/2/2 member hashes preserved with
zero churn. The read-only Golden capture measured 13 recalls with zero completion calls or tokens,
p50/p95 total latency of 272.159/439.087 ms, and 127/127 byte-verifiable citations. Evidence is
under `tests/golden/results/semantic-adversarial/adr0040-fresh-rebuild-fixed-f0e050cbb173/` and
`~/.marginalia/quality-runs/adr0040/semantic-adversarial/current-fresh-rebuild-fixed-churn.json`.
This closes the current synthetic fresh-rebuild slot only; the collection remains incomplete.

The first isolated source-order probe then exposed a defect in the measurement boundary before it
could be admitted as acceptance evidence. Two fresh vaults ingested the six sources serially in
ascending and descending filename order with identical provider/model/configuration, embedding
batch size 32, and one concurrent embedding batch. The original snapshot encoding reported 100%
identity and relation churn because it hashed physical node and Claim ids; those ids include
extractor-authored content and therefore differ across vaults even for the same semantic surface.
After replacing that encoding with canonical semantic values, the same stored graphs still showed
real, large drift: identity churn 0.809524, predicate churn 0.826087, and relation churn 0.925926.
Both Golden floors passed exact byte provenance and ordinary recall used zero completions, but the
snapshots lack one complete shared materialized fingerprint triplet because the provisional
predicate/authority policy evolved differently during the two fresh ingests. These are diagnostic
failure measurements, not a completed source-order arm. A valid arm must hold one materialized
semantic policy constant (or replay one immutable extraction/decision authority) before applying
the locked churn budget; the acceptance collection remains fail-closed until then.

The corrected source-order arm now does exactly that. The rebuild core exposes a private,
fail-closed acceptance seam that accepts only an exact permutation of the canonical source set;
the standalone Python wrapper forwards it only for internal acceptance callers, while the public
CLI and production rebuilds still use canonical POSIX order. Two fresh staging graphs materialized the
same six sources in ascending and descending order under one sealed `fresh_rebuild.v1` authority.
Each arm produced exactly six completed ledger runs and 41 `policy_replay` comparisons, with zero
live `curator` or `relation_curator` comparisons. The complete fingerprints match and opaque churn
is `0.0` for identities, predicates, and relations. The pinned reports are
`tests/golden/results/semantic-adversarial/adr0040-source-order-a-immutable-f0e050cbb173/`
(SHA-256 `fa0704a62f4b0be06ceff307b584da3a0b63c4e8637479b68811b95aeca52780`)
and `adr0040-source-order-b-immutable-f0e050cbb173/`
(SHA-256 `2090ccc73c6a7f71a999aadb888f9612d5339475c7b7ffb1305ba20243208b21`).
The comparison and run-level replay proof are retained under
`~/.marginalia/quality-runs/adr0040/semantic-adversarial/`. An earlier nominally stable capture
was rejected because its first arm resumed a pre-credential-failure run and made one new decision;
the accepted recapture requires replay on both sides rather than trusting the churn number alone.

The synthetic provider-client restart arm also completed without treating remote-gateway uptime as
an unstated variable. With no ingest or curation active, `marginalia dev` replaced server PID 4631
with PID 11835 and preserved `--no-open`. The new process rehydrated the managed credential, then
reingested exactly six byte-identical sources as queue items 26–31 with embedding batch size 32 and
one concurrent batch. All six completed. The post-restart snapshot retained generation
`89142fab-fcc8-465b-93af-35e1f8f22c4c`, the 3/2/2 semantic population, all fingerprints, and zero
identity/predicate/relation churn. Its pinned report is
`tests/golden/results/semantic-adversarial/adr0040-provider-restart-f0e050cbb173/semantic-quality.json`
(SHA-256 `5df4589489d8735c924dbe7a0135446d2f6bf2a4f9fbc83529451a7096778105`).
This proves Marginalia's provider-client restart arm; it does not claim that the separately managed
LiteLLM gateway process was restarted.

The synthetic semantic-policy-change arm is now sealed against the same canonical baseline. The
application configuration changed only `consolidation.auto_commit_threshold`, from `0.75` to
`0.80`; governance immediately changed the effective semantic-policy fingerprint from
`edcf1527bca001bda86f6c1135b124b0473859f14eada855cfb7f0615de5d0fa` to
`289e23231ae6a91664ba87a66451e5581986d0f7ab09af526e3e486ee47cb033` and reported the existing
generation as requiring rebuild. Rebuild `rebuild-8d270c022997` completed all six sources in one
stable pass, installed generation `b9b06c91-7b3f-4f5d-997a-af6612b29488`, and passed both final
and post-swap audits with zero issues. Its six new ledger runs contain 122 comparisons: zero
`policy_replay`, 20 live `curator`, and 21 live `relation_curator` comparisons. The old
policy-incompatible verdicts were therefore not reused. The pinned before/after reports are
`tests/golden/results/semantic-adversarial/adr0040-policy-before-f0e050cbb173/semantic-quality.json`
(SHA-256 `74880e11b13b440ed0a4a8150f8774f11de7b57950ef8712e3c7341a635a94c8`) and
`adr0040-policy-after-f0e050cbb173/semantic-quality.json`
(SHA-256 `eff8dd51b7d6a885dc1c4e864ecca7b543f89a3d955de9be3f3072076f4846eb`). After capture, the
threshold was restored to `0.75` and public rollback job `rollback-7ea8053ad71e` returned the graph
to generation `89142fab-fcc8-465b-93af-35e1f8f22c4c`; governance again reports matching current
and materialized policy, no rebuild requirement, verified integrity, and an unfenced writer. The
displaced policy-change graph remains retained in the rollback artifact.

The Hobbit diagnostic was recaptured read-only after the literal-endpoint correction, without a
second ingest. Queue item `4-c8b25211` remains the only successful item, and embedding remains
batch size 32 with one concurrent batch. The failed boundary was an internally inconsistent
relation-curator verdict: `inverse_direction_required=true` cannot apply to a scalar object. The
strict `PredicateProposal` contract still rejects that shape, while its owning caller now converts
only the offending relation to durable `queue_direction_conflict` review instead of aborting the
document. All 35 focused admission and ingest regressions pass.

The current report contains 1,037 nodes, 2,281 semantic edges, 794 Claims, 198 primitive identities,
and 290 raw predicate labels. Its 13 completion-free recalls retrieved all 12 answerable Golden
spans intact, reverified 130/130 returned citations against source bytes, and measured p50/p95 total
recall latency of 1,010.919/1,134.235 ms. It is pinned under
`tests/golden/results/hobbit-glm52/hobbit-adr0040-current-readonly-118268fea5b2/`; the corresponding
single-corpus receipt is
`hobbit-current-diagnostic.json` in the Hobbit GLM-5.2 quality run, SHA-256
`337505d48d83e01028855e0d4587ed84deaec3bc1ca4109e0890071fdb2caeb5`. The structural rebuild gate
passes, but predicate fragmentation remains material (245 singleton labels, 84.4828%) and 56
primitive entities have no topology relation. At that checkpoint the acceptance collection was
still `incomplete` at 32/67 ready artifacts
(the same run's `hobbit-current-diagnostic.json`,
2026-07-18 05:45): this remains a one-book diagnostic because human semantic
evidence, Hobbit replay/ablation arms, and the other corpus classes are absent. During an earlier
capture, an unbounded harness `curl` survived a dev-server restart and appeared hung. Every Golden
HTTP operation now has a validated connect and total timeout, with separate recall, answer, and
ingest ceilings; those are harness supervision bounds, not provider timeouts.

The post-correction Hobbit audit was repeated once more with manifest v5 and the explicit
`--never-ingest` contract. The harness verified the one-document byte identity before scoring,
recorded embedding batch size 32 and one concurrent batch, completed all 13 questions, recovered
all 12 answerable spans intact, and verified 130/130 citation ranges. No queue item was created.
Completion-free recall again used zero completions and zero generated tokens; query embedding,
deterministic retrieval, and total latency measured p50/p95 at 299.822/395.738,
729.843/891.581, and 1,078.697/1,232.634 ms respectively. Codex-only readback found 12 answers
fully matched the expected fact; the Bard answer was substantively correct but called the weapon
the “final arrow” rather than preserving the expected “black arrow” detail. This is diagnostic AI
review, not human adjudication. The run is retained at
under `tests/golden/results/hobbit-glm52/` as the v5 read-only audit of 2026-07-18; its v5
manifest and responses hash to `c80809378f8faec220049d81c617dab044189f1867736d8f0a37af812b7b3e3a`
and `dc03ce0c82e9ff70f02af54dc66286c6a2e5ff49fc7ae7a02fa1e7a265720f95`.
The nominal live audit artifact now also points to generation
`8469a06c-7290-4c5b-b8d9-dabeece20e70` and hashes to
`ecda47013f90d840b77a1a89b28201d9ef06564145e0db760849fa1e744008df`.
Its structural rebuild gate passes, while `--require-semantic-quality` intentionally exits nonzero
because the authoritative semantic verdict remains `incomplete` without owner adjudication.

The first temporal-chat incremental capture retrieved all five Golden spans, but its semantic
snapshot correctly reported fingerprints as `not_measured`: its three sequential ingests evolved
the predicate registry while materializing the graph, so no single complete fingerprint triplet
described that generation. It is retained as diagnostic evidence and is not the acceptance
baseline. Golden byte-span IoU was also corrected during this capture. The old judge unioned the
retrieved ranges for the denominator but summed pairwise overlaps for the numerator, double-counting
overlapping windows and permitting IoU above one. Both sides now use byte-range union length, with a
regression test for overlapping retrieval spans.

The first fingerprint-authoritative temporal rebuild, `rebuild-8be422a19b58`, installed verified
generation `c2f847a8-a359-45d7-8a65-a0eb7d5d1296` but exposed a semantic admission defect: primitive
identities fell from 10 to 5 and Golden retrieval fell from 5/5 to 2/5. The first failed boundary
was predicate reuse. The relation curator can paraphrase the prose definition of a registered
predicate; exact string equality incorrectly classified that harmless paraphrase as
`queue_mapping_conflict`. The resulting queued relations then caused the relationship-liveness gate
to queue their endpoint entities. The predicate registry is now authoritative for an existing
label's definition. Reuse ignores definition paraphrase while still failing closed on direction or
symmetry conflict. Focused tests cover both the accepted paraphrase and retained direction guard.

With that owning-boundary fix, rebuild `rebuild-39711e370584` converged from empty staging in three
bounded passes and installed verified generation `e57f0c4d-a117-4bed-864e-0f7dc1f0a745`. Final and
post-swap audits were clean, the writer was unfenced, and the materialized configuration,
extraction, and semantic-policy fingerprints were measured. The canonical read-only capture is
`tests/golden/results/temporal-chat/adr0040-rebuild-fixed-baseline-21cb4204818a/`. It retrieves all
5/5 answerable spans with mean token recall 1.0, verifies 60/60 citations against source bytes,
performs zero recall completions, and records corrected mean byte IoU 0.1332. The six deterministic
answers preserve the owner, venue, date, alias, correction history, and absence-of-budget facts.
This is still a privacy-safe diagnostic corpus until the named owner reviews its expected answers
and semantic adjudication.

One controlled identical reingest then added exactly three terminal queue items, each with zero
committed or queued semantic operations. The semantic populations remained 11 identities, 13
predicates, and 15 relations with identical member hashes and zero churn. Its immutable capture is
`tests/golden/results/temporal-chat/adr0040-identical-reingest-21cb4204818a/`; the comparison is
`~/.marginalia/quality-runs/adr0040/temporal-chat/identical-reingest-churn.json`. No second Hobbit
ingest was performed, and the application embedding defaults remain batch size 32 with one
concurrent batch.

The temporal provider-client restart arm then terminated only the Python server child while all
ingest and curation queues were idle. This exposed a development-supervisor gap: `marginalia dev`
waited for a source edit after any child exit. ADR 0034 now defines and the implementation enforces
the intended keep-alive contract: clean or signal-driven exits restart automatically, while a
positive failure exit waits for a source change instead of crash-looping. The repaired supervisor
started PID `61225`, rehydrated the managed credential, and one controlled three-source reingest
completed as queue items 6--8 with no writes or review rows. The capture at
`tests/golden/results/temporal-chat/adr0040-provider-restart-21cb4204818a/` retained the same
generation, fingerprints, and 11/13/15 semantic populations with zero churn. The comparison is
`~/.marginalia/quality-runs/adr0040/temporal-chat/provider-restart-churn.json`.

Fresh rebuild `rebuild-6b46b6a01370` then replayed the same three sources under the sealed policy
in one stable pass. It installed generation `0ed18845-af4b-47c4-be5c-dd44f6f58ccb`, retained a
generation-specific previous-graph backup, passed every per-source, final, semantic, and post-swap
gate, and published an unfenced verified integrity sidecar. The read-only capture at
`tests/golden/results/temporal-chat/adr0040-fresh-rebuild-21cb4204818a/` preserved all five Golden
spans, 60/60 byte-verified citations, all six deterministic answers, zero recall completions, and
the exact 11/13/15 semantic populations. Its generation differs from the baseline while all
fingerprints and opaque members match; churn is zero in
`~/.marginalia/quality-runs/adr0040/temporal-chat/fresh-rebuild-churn.json`.

The first temporal source-order materializations then exposed one remaining owning-boundary defect.
Identity membership was stable, but the active literal owner depended on traversal order: canonical
order kept Malik Daro while reverse order kept Rina Vale. The first diagnostic harness also reported
zero live curator calls, but that counter read `record_type` while ledger rows use `kind`; that
zero-call claim is therefore rejected and must be recaptured with the corrected counter. All three durable
source copies had the same file mtime, so the F8 day-granularity `asserted_at` gate tied and the later
ingested Claim won. Increasing mtime precision, sorting filenames, or comparing Claim ids would only
replace the accidental tie-breaker. The correction path now preserves an unambiguous timestamped
relationship line as the exact Claim `source_span`, records its normalized `source_asserted_at` plus
byte evidence, and compares that signal only when both Claims carry it. Equal explicit times remain
ambiguous and never auto-supersede. If the incoming Claim is explicitly older, a confirmed correction
dates that incoming historical Claim as superseded by the already-live newer Claim; otherwise the
existing file-recency gate remains unchanged. This is evidence-backed correction ordering, not
first-class valid-time query semantics.

The same diagnostic then isolated a second source-order boundary. Exact-title reconciliation can
map the same semantic relation onto different physical endpoint ids because proposal ids include
descriptive node content. Relation-curator authority was keyed only by the resulting edge candidate
id, so the same supported `Jo Merek — instructed — Use South Annex` evidence could miss its sealed
`instructs` verdict and be redrawn as `states` when the source order changed. Candidate ids remain
the primary replay key. A completed exact-policy authority now adds a conservative semantic fallback
key comprising the exact source block, endpoint primitive plus exact normalized title, raw predicate,
and typed object. It replays only when every matching authority has the same structured verdict;
missing endpoint identity fails closed to ordinary curation, while conflicting sealed verdicts
produce a deterministic queued verdict without another model call. Exact sealed node outcomes also
replay for the same candidate id. When deterministic type correction or identity reconciliation
derives a new edge id, the decision follows only its explicit, unambiguous derivation chain;
conflicting ancestry is poisoned and cannot replay. Focused tests cover endpoint-id drift, chained
endpoint remaps, terminal node outcomes, and conflicting authority.

The corrected v12 source-order arm rebuilt disposable staging graphs in canonical and reverse order
from the same sealed extraction and decision authority. Both final audits were verified and both
were independently swap-eligible. Each materialized exactly 11 identities, 13 predicates, and 15
relations, with `0.0` churn in all three dimensions and zero live `curator` or `relation_curator`
comparisons. The reverse arm replayed seven exact terminal node outcomes and four relations through
explicit derivation lineage. It also made one separately reported type-adjudication call; therefore
this result proves zero live candidate/relation curation, not zero total LLM work. The staging run
never swapped the live graph, and the graph, WAL, ledger, review queue, and predicate registry were
restored to their authority hashes. The pinned evidence is under
`~/.marginalia/quality-runs/adr0040/temporal-chat/source-order-corrected-v12/`.

The temporal policy-change arm then raised only `consolidation.auto_commit_threshold` from `0.75`
to `0.80`. The effective semantic-policy fingerprint changed from `e403f156...` to `1603e470...`
while the extraction fingerprint stayed fixed. Rebuild `rebuild-30dd57f043e5` reused all three
sealed extraction units but recomputed 21 candidate-curator and 25 relation-curator decisions; no
old-policy `policy_replay` row was used. Verified generation
`8528fff8-0efa-4c9d-b04a-2cc32db5f2cd` retained 11 identities, 13 predicates, and 15 relations,
but one governed predicate and one relation member changed, so the policy-change comparison is
intentionally not semantically stable. After restoring the threshold to `0.75`, versioned rollback
`rollback-dab19806e7d6` restored generation `0ed18845-af4b-47c4-be5c-dd44f6f58ccb`, its original
policy receipt, all 11/13/15 members, verified pre/post-swap audits, and an unfenced writer. The
displaced policy-change graph remains retained in the unique rollback artifact.

The final temporal-correction materialization corrected two evidence boundaries exposed by the
Golden capture. Relation adjudication now treats endpoint type and canonical title as identity
context but excludes mutable endpoint summaries, so an old summary cannot veto a directly grounded
later correction. That input contract is fingerprinted. When the same Claim gains a corroboration
with a newer explicit source time, the newer exact line also becomes its public primary anchor while
all prior Blocks remain attached through `prov:wasDerivedFrom`. Rebuild `rebuild-f9b844e4803e`
installed verified, unfenced generation `01aaf900-849c-4e61-8e49-42e8dc33f55c` under semantic
policy `f7dc4d79...`. The current ownership Claim has two source Blocks and exposes the 6 April
confirmation span as primary evidence; the initial Rina Vale literal Claim is superseded by the
5 April Malik Daro correction. The read-only Golden run
`tests/golden/results/temporal-chat/adr0040-source-time-01aaf900-21cb4204818a/` recovered every span
for all five positive question groups, verified 60/60 citations, returned the expected current and
historical answers, and made zero completion calls. The pinned receipt at
`~/.marginalia/quality-runs/adr0040/temporal-chat/temporal-correction.json` explicitly declines to
claim first-class valid-time semantics.

After pinning that temporal-correction receipt
(`~/.marginalia/quality-runs/adr0040/temporal-chat/temporal-correction.json`, 2026-07-18 06:10),
the then-current fail-closed collection reported 32 ready artifacts, 35 missing artifacts, and zero
invalid artifacts. The later strong evidence schemas intentionally invalidated five legacy
counter-only sidecars, which `~/.marginalia/quality-runs/adr0040/acceptance-status.json`
(2026-07-18 10:47) recorded as 27 ready, 35 missing, and 5 invalid. The 2026-07-18 read-only
refresh, after adding the CoP stored report and baseline snapshot, reports 29 ready, 33 missing, and
5 invalid artifacts; the durable refresh is
a dated copy of `~/.marginalia/quality-runs/adr0040/acceptance-status.json` (2026-07-18 15:50). All three
are superseded by `acceptance-status-current.json` (2026-07-18 16:43) at 34 ready, 33 missing, and
0 invalid; see the ordered readiness table in the status summary above. The collection remains
incomplete by design.

---

## Evidence that triggered this ADR

The July 15–16 LOTR audit found a strong source and candidate layer alongside two separate classes
of failure. ADR 0039 owns the technical topology corruption. The durable pre-commit evidence also
exposed semantic fragmentation that remains even after storage is repaired:

- the four commit plans contained 9,099 accepted relationship operations using 2,855 distinct raw
  predicate labels;
- 199 exact titles appeared under more than one primitive type;
- frequently recurring referents such as Frodo, Gandalf, Aragorn, Bilbo, Sam, Mordor, Rivendell,
  and Minas Tirith appeared in competing primitive types;
- title surfaces included underscore variants, inconsistent possessive casing, damaged Unicode,
  and mojibake-like forms;
- all 21/21 named LOTR profile targets were found, proving that domain coverage alone can pass while
  identity, type, and predicate quality remain poor.

The predicate result is the same failure class ADR 0017 previously observed at smaller scale:
open extraction can generate a long tail of synonymous, overly specific, and one-use relation
labels. The cross-type result is also not new: ADR 0013 made same-title/multiple-type conflicts
visible; ADR 0008/0022 and the orphan-entity research rejected blind cross-type merges.

The new requirement is to make these observations one coherent, measurable semantic-quality
contract rather than a collection of local prompt fixes and corpus-specific checks.

The LOTR measurements are incident evidence, not permanent product thresholds. They must be
recomputed from a technically verified rebuild before they become a baseline.
Phase 0 records the source vault id, ledger/plan ids, exact audit command, configuration fingerprint,
and private incident-bundle location so these figures remain reproducible without committing the
copyrighted corpus.

---

## Existing contracts that remain binding

This ADR extends existing architecture; it does not replace it.

1. **Closed primitive model.** Knowledge entities use exactly `Agent`, `Activity`,
   `InformationObject`, `Concept`, or `Place`. A sixth primitive requires a separate RFC.
2. **Markdown trust root.** Source files remain canonical; the graph is derived and rebuildable.
3. **Byte-anchored provenance.** Every Claim remains traceable to a source Block and exact byte
   range.
4. **Conservative identity resolution.** Same-type exact resolution and bounded LLM adjudication
   remain the automatic merge path. Cross-type collisions never auto-merge.
5. **Semantic Claim identity.** A relation Claim is identified by its resolved semantic triple.
   Predicate canonicalization occurs before Claim identity against the registry snapshot pinned by
   that plan. Later mapping changes converge through heal/rebuild; they do not rewrite the id on
   read.
6. **Reversible upkeep.** Authority and predicate mappings live outside the graph. They apply
   non-mutating folds at query time and prospectively during admission/ingest. Retroactive graph
   materialization happens only through a fresh rebuild or explicit heal path.
7. **Predicate semantics matter.** `same`, `inverse`, `narrower`, and `distinct` are different
   decisions. A symmetric or broader label must not erase direction or specificity.
8. **Generic core, optional packs.** Production extraction must not learn LOTR, SDLC, or another
   corpus as a hidden global ontology. Packs may add explicit domain guidance.

---

## Quality model

Semantic quality is evaluated at five distinct layers. A single aggregate “quality score” may be
shown for convenience, but it must never hide the failing layer.

| Layer | Question | Typical failure |
|---|---|---|
| Surface | Did we read and preserve the name correctly? | mojibake, punctuation/case drift, underscore title |
| Type | Did we choose the correct primitive for this sense? | Frodo as `Concept`; Mordor as `Agent` |
| Identity | Did mentions resolve to the right real-world referent? | duplicate Gandalf nodes; false merge of namesakes |
| Predicate | Does the relation use a stable, correctly directed label? | thousands of nonce synonyms; inverse folded as same |
| Relation | Is the fact useful, grounded, and connected to the right endpoints? | grammatical glue, unsupported inference, orphan endpoint |

Each layer produces its own decision record, metrics, and abstention path. A later layer may use
earlier evidence but may not silently repair an earlier decision.

---

## Binding semantic invariants

### Q1 — Technical integrity is a precondition

Semantic reports over a materialized graph are authoritative only when ADR 0039's integrity audit
passes for the same graph generation. Before that, the evaluator may inspect durable extraction,
candidate, comparison, and commit-plan records, but must label the report `pre_commit` or
the corresponding ADR 0039 audit state such as `unverified`. ADR 0039 owns audit-status vocabulary
and graph-generation identifiers; this ADR consumes them verbatim.

### Q2 — Source form and identity are different data

The text found in a source is a mention surface, not automatically the canonical entity title.
Marginalia preserves the original source form for provenance and builds a separate normalization
key for matching. Normalization must never overwrite source bytes.

### Q3 — Type is part of the semantic decision

Type is not merely a convenient blocking key. A candidate is typed before final entity resolution,
and same-surface candidates across primitives are an explicit conflict set. They are adjudicated as
one of:

- **same referent, one primitive wins** — correct the candidate type before identity resolution;
- **different senses** — keep separate typed identities and record the distinction; or
- **uncertain** — queue for review and do not auto-merge.

Cross-type nodes are never destructively merged as a shortcut.

### Q4 — Canonicalization precedes Claim identity

The relation pipeline resolves endpoint identities and canonicalizes the predicate before computing
the ADR 0016 semantic Claim id. Otherwise a later alias decision changes the meaning of an already
minted identifier and fragments corroboration.

### Q5 — Novel predicates are governed, not forbidden

An open-domain system cannot ship one exhaustive ontology. A novel, precise, source-grounded
predicate may enter as a recorded provisional predicate. It may not enter as an untracked string.
The registry distinguishes stable canonicals, aliases, provisional labels, inverse mappings,
narrower mappings, and rejected/distinct decisions.

### Q6 — No generic relation as a lossy fallback

Marginalia does not replace a meaningful unknown relation with `related_to`, `has`, or another
catch-all merely to pass validation. It either retains a governed provisional predicate, expresses
a literal Claim, or abstains.

### Q7 — Every relation must earn its endpoints

A committed topology relation must have two accepted entity endpoints, an accepted canonical or
provisional predicate, and source evidence that supports the stated direction. Literal propositions
remain literal Claims rather than forcing values into artificial entity nodes.

### Q8 — Semantic changes remain reversible

Alias decisions, type corrections, and predicate mappings are durable comparison/authority records.
They do not mutate the live graph in place. A fresh rebuild applies the chosen semantic policy and
validates the result before replacing the prior graph.
Query-time folds remain non-mutating, and prospective admission may apply an accepted mapping to a
new plan. Only retroactive changes to already materialized artifacts require heal/rebuild.

### Q9 — Ordinary recall remains completion-free

The normal recall path may compute one query embedding and perform deterministic local vector,
lexical, graph, filtering, fusion, and projection work. It may not invoke an LLM completion,
semantic judge, agent loop, or provider reranker. Optional answer generation is a separate product
surface and must not be reported as recall cost or latency. Any future learned reranker must be an
explicit, locally runnable, separately measured option that preserves the completion-free default.

---

## Decisions

### D1 — One staged semantic-decision pipeline

All extraction paths use the same ordered pipeline:

```text
raw candidate mention
  -> preserve source surface and byte anchor
  -> compute normalized matching surface
  -> validate/adjudicate primitive type
  -> resolve canonical entity identity
  -> validate relation direction and endpoint senses
  -> map or register predicate
  -> apply semantic relation gate
  -> compute semantic Claim identity
  -> emit durable commit-plan operation
```

The HTTP ingest, folder watcher, CLI, rebuild, and retry paths already converge substantially on
`Companion.remember`; this decision preserves that funnel. The unification target is the remaining
normalization, reconcile-context, and diagnostic divergence. Runtime code exposes one
semantic-decision service; callers provide candidates and policy context, not their own rules.

### D2 — Shared, lossless surface normalization

Introduce one pure normalization contract used by extraction dedup, store reconciliation,
cross-type conflict discovery, authority matching, and diagnostics.

The normalizer returns two explicit comparison keys:

- **exact key:** NFC, trimmed/collapsed whitespace, casefold, then NFC again because Unicode
  casefold output is not always normalization-closed. Only this conservative key may feed
  deterministic Tier-0 auto-merge;
- **discovery key:** may additionally normalize common apostrophe/dash variants, recognize
  accidental underscore separators, or apply justified compatibility folding. It feeds candidate
  generation, conflict discovery, and search only; it never causes a no-judge merge.

Across both keys, the contract:

- applies Unicode normalization (`NFC` for preserved/display text, compatibility folding only in a
  separate search key when justified);
- trims and collapses whitespace;
- case-folds for comparison;
- versions every feature and records whether it belongs to exact or discovery matching;
- preserves language diacritics in the canonical display form; and
- never guesses a repair for suspected mojibake.

The record retains:

```text
source_surface     exact extracted text
exact_key          conservative deterministic merge key
discovery_key      broader candidate/search key
canonical_title    selected display form
aliases            other accepted display forms
normalization_flags e.g. underscore, mixed_case, suspected_mojibake
```

Suspected mojibake is queued or re-extracted from the original bytes. It is not silently converted
by a heuristic that could invent a name.

Canonical display selection uses the strongest source-grounded, correctly encoded mention. It does
not use “first occurrence wins.” This generalizes the source-grounding rule already used for exact
within-batch duplicate survivors.

### D3 — Primitive type guide plus conflict adjudication

The extractor and judge share a short, versioned primitive guide derived from the locked schema:

| Primitive | Decision question |
|---|---|
| `Agent` | Can this referent act or bear responsibility as a person, group, organization, or system? |
| `Activity` | Is this an event, action, or process with temporal extent? |
| `InformationObject` | Is this identifiable symbolic/propositional content or a work? |
| `Concept` | Is this an abstract categorical or topical anchor not better represented above? |
| `Place` | Is this a spatial location, region, site, or jurisdiction? |

The guide does not introduce domain examples into the core prompt. A pack may add examples while
remaining mapped to these five primitives.

Before same-type identity resolution, a type-conflict detector compares the candidate against:

- other candidates in the extraction unit and file;
- accepted candidates in the active run; and
- canonical identities in the AuthorityIndex/store.

High-confidence correction changes the candidate's primitive before final identity and records the
reason, evidence, model/prompt version, and prior type. Ambiguous cases enter the existing review
workflow. A legitimate same-title/different-sense decision is negative-cache evidence so it is not
re-judged every run.

The closed model may expose recurring concepts that fit none of the five primitives cleanly. This
ADR does not hide that pressure by adding a type. It records the ambiguity; a future schema RFC may
use accumulated evidence to justify a change.

### D4 — Canonical identity is separate from mentions and aliases

A canonical entity represents one adjudicated sense. Mentions retain source-specific text and point
to it through provenance/authority records. Aliases strengthen retrieval but do not mint competing
nodes.

Current behavior and the target are explicit:

| Stage | Current | ADR 0040 target |
|---|---|---|
| exact survivor | within-batch source-grounded; store existing/first wins | evidence-quality order below |
| alias/identifier | candidate signal routed to judge | deterministic only for verified identifiers/accepted aliases |
| select/cluster judge | retroactive reconcile path, off by default | measured and enabled only after B³ gates |
| ingest resolution | exact + embedding-band/pairwise paths | shared precision-first funnel |

On acceptance, this survivor policy supersedes ADR 0004 F4's unconditional first occurrence and
ADR 0008's degree-only canonical selection. Until then, those accepted contracts describe current
behavior.

The target identity-resolution order is:

1. exact normalized surface, same primitive, no structural contradiction;
2. deterministic alias/identifier match;
3. embedding-band candidate generation;
4. select-framed LLM adjudication with source and neighborhood context;
5. cluster-level resolution for transitive consistency; and
6. review on uncertainty or contradiction.

Same names are not sufficient evidence. Relationship endpoints that explicitly distinguish two
candidates remain a structural “do not merge” signal. Cross-document corroboration increases
confidence only when type, source context, and neighborhood agree.

The canonical survivor is selected by deterministic evidence quality: valid identifier, source
grounding, encoding quality, corroboration, then stable tie-break. Ingest order is the last tie-break,
not the identity policy.

### D5 — One predicate registry and lifecycle

Extend the ADR 0017 predicate alias ledger into the authoritative predicate registry rather than
creating a second ontology mechanism. The registry has two record types:

1. existing SSSOM-shaped **mapping records**, retaining orthogonal `mapping`
   (`exact_match`, `sub_property_of`, `inverse_of`, `distinct`) and `status`
   (`auto`, `confirmed`, `rejected`, `queued`);
2. new **predicate records** with `canonical` or `provisional` lifecycle state, definition,
   direction/symmetry, observed type signatures, support, evidence samples, and provenance.

Mapping records remain in `.marginalia/predicates/aliases.json`; predicate records use a sibling
versioned registry file under the same directory. Every predicate visible to commit has a predicate
record, while relationships between predicates stay mapping records:

| State | Meaning | Commit behavior |
|---|---|---|
| `canonical` | stable relation label with definition and direction | commit directly |
| `provisional` | novel, valid, grounded label awaiting vocabulary upkeep | commit with registry record |

A predicate record includes the normalized label, one-line definition, direction, symmetry where
known, observed primitive-domain/range signatures, support count, sample Claim/source ids,
confidence, and decision provenance. Alias/inverse/narrower/distinct semantics remain on mapping
records rather than being overloaded into predicate lifecycle state.

The registry is assembled from the fixed core aliases, active vault mappings, and explicit pack
vocabulary. Core mappings keep precedence. Packs may add vocabulary but cannot silently change a
core mapping.

### D6 — Predicate admission happens before graph commit

For each proposed relation, the predicate mapper must choose exactly one outcome:

1. map to an existing canonical predicate;
2. apply a confirmed inverse mapping and swap endpoints;
3. retain a narrower or novel precise label as registered provisional;
4. express the information as a literal Claim when it is not an entity-to-entity relation; or
5. abstain/queue.

The registry is consulted only during admission/planning. The durable plan records the chosen label
and endpoint direction; ADR 0039 applier/replay never reconsults the registry. Later mapping changes
affect new plans prospectively and prior artifacts only through heal/rebuild.

Admission rejects placeholders (`unknown`, `none`, `null`, `n/a`), malformed labels,
sentence-shaped labels, and grammatical glue with no stable relational meaning. Low frequency alone
does not reject a predicate: an uncommon fact can be legitimate. Low frequency makes the label
ineligible to anchor automatic synonym clusters and increases its review priority.

The relation curator may propose a canonical label, but the registry/mapping service owns the final
normalization. Prompt text is not the source of truth.
Phase 3 removes the current raw-label fallback when normalization fails and keeps the placeholder
denylist in registry policy, not prompt prose. The governed literal predicate `has_value` remains a
literal representation tool, not a generic topology fallback.

Direction evidence is representation-aware. An inverse mapping can swap only two entity endpoints;
a literal can never become a subject. If a curator nevertheless marks
`inverse_direction_required=true` for a literal Claim, the strict predicate value object continues
to reject that impossible shape, while the Companion orchestration boundary records
`queue_direction_conflict` and routes that one candidate to review. It must not silently discard the
flag, invent an entity for the literal, or abort the rest of an otherwise valid ingest. This behavior
is fingerprinted as `predicate_admission.v2`; older full-policy verdict/plan replay is deliberately
ineligible, while extraction-layer artifacts remain independently reusable.

### D7 — Semantic relation quality is an explicit gate

Extend the current relationship-liveness gate into a named semantic relation gate. A topology
relation commits only when all are true:

- **grounded:** the cited Block supports the subject, predicate meaning, object, and direction;
- **typed:** both endpoints have accepted primitive decisions;
- **live:** both endpoints are committed or resolved canonical entities;
- **specific:** the predicate conveys more than document structure or grammatical attachment;
- **bounded:** the relation does not smuggle a sentence or explanation into the predicate;
- **non-fabricated:** neither endpoint nor relation relies on unstated world knowledge; and
- **non-redundant:** the proposal is not merely an existing Claim restated with weaker wording.

The gate returns structured reasons, not a single boolean. At minimum:

```text
commit
queue_type_conflict
queue_predicate
queue_grounding
reject_placeholder
reject_structural_noise
reject_unsupported_inference
reject_dead_endpoint
reject_redundant
```

Broader human-perceived usefulness is measured as reviewer agreement; it is not an automatic commit
criterion until a falsifiable rule and labelled gate exist.

Gate reasons map durably into ADR 0039 operations:

| Semantic reason | Plan operation |
|---|---|
| `commit` | `create_topology_edge` + `mint_claim` |
| any `queue_*` | `queue_review` |
| any `reject_*` | `dead_letter` with the original structured reason |

Plan operations and receipts preserve that reason verbatim.

The gate stays domain-independent. LOTR or SDLC profile coverage may diagnose outcomes but cannot
override it.

### Owning boundaries and implementation map for Phases 2–4

This section routes implementation; it does not introduce another semantic pipeline. The HTTP,
watcher, CLI, rebuild, and retry paths continue to converge on `Companion.remember`, which
orchestrates pure decisions and hands their pinned results to the ADR 0039 planner/applier boundary.

- `semantic_surface.py` remains the only title-normalization contract. Phase 2 migrates exact
  decision paths to `exact_surface_key` and recall-only lanes to `discovery_surface_key` without
  changing `NodeCandidate.candidate_id`. Enabling the new exact key is a fingerprinted policy change
  and requires a fresh rebuild. The operational Phase 2 identity path is versioned as
  `entity_resolution.v2`.
- `AuthorityIndex` remains the positive, off-graph equivalence authority. New type-correction,
  ambiguous, and `distinct` negative-cache records live in a sibling versioned
  `.marginalia/authority/decisions.json`, owned by the new `reconcile/decisions.py`; they never enter
  `equivalence_map()`. The fold also filters explicitly for `verdict == "same"` before any new
  decision writer ships. The first model-free Phase 2 contract slice implements this boundary as
  `identity_decisions.v1`: ordered, typed `TypeCorrection`, `DistinctDecision`, and
  `AmbiguousReview` records are atomically appended and read without a graph or `ReviewQueue`
  dependency. A cross-process file lock and re-read inside the lock prevent lost updates between
  concurrent writers. Missing files mean no prior decision; corrupt, duplicate-id, unknown-kind,
  unknown-field, and unsupported-version files fail closed, so extending the record shape requires
  a new schema version. `DistinctDecision` normalizes its unordered id pair and exposes a
  direction-independent negative-cache lookup. Legacy Authority v1 rows with no verdict retain
  their historical implicit `same` meaning, while every explicit non-`same` verdict is excluded
  from the equivalence fold.
- `Companion.remember` loads and stability-checks the identity-decision snapshot at the run-start
  fingerprint boundary, before extraction, so a corrupt sidecar fails before LLM spend and a later
  append cannot change the decisions applied by that run. It applies that pinned snapshot after
  extraction and embedding but before exact, fuzzy, store, or judge-based identity resolution. It
  folds an ordered
  `TypeCorrection` chain over each closed-primitive candidate, derives the corrected candidate id
  through the unchanged `(type, title, content)` formula, preserves the complete `SurfaceRecord`,
  remaps candidate edge endpoints, and records both the comparison and derived artifacts in the
  candidate ledger. If a correction would derive the same id as a candidate protected by an
  explicit `DistinctDecision`, the negative decision wins: the correction is not applied and the
  candidate is queued with a structured conflict intent. Non-primitive records such as `Claim` do
  not enter this type-correction path.
- `DistinctDecision` is an authoritative negative cache. One merge-veto callback is threaded
  through within-batch exact collapse, within-batch judge resolution, exact and judged store
  reconciliation, post-curator exact canonicalization, and retroactive propose/apply reconciliation.
  A cache hit skips the merge and the LLM call; it never becomes a positive authority edge.
- After corrections, `Companion.remember` compares `exact_surface_key` across the active run and
  non-infrastructure closed-primitive store nodes. An unresolved cross-type collision cannot merge:
  its new candidate is forced into the existing review queue and a structured
  `identity_cross_type_exact_collision` intent is retained in the ledger. An explicit type correction
  or `DistinctDecision` resolves that conflict for replay. A candidate id already present in the
  store is an established re-mention, not new review work, so it does not churn the queue on every
  ingest. Ingest deliberately does **not** append an
  `AmbiguousReview` while the run is active: `decisions.json` is part of the pinned semantic-policy
  fingerprint, so mutating it mid-run would make the run describe a different policy than the one it
  executed. A later review action may append the decision for a subsequent run or rebuild.
- Existing predicate mapping records and `.marginalia/predicates/aliases.json` remain unchanged.
  The new `predicates/registry.py` owns sibling predicate records in `registry.json`, while the new
  `predicates/admission.py` owns the one D6 admission decision. The curator supplies evidence and a
  label proposal only. Confirmed inverse admission pins the final predicate and swapped endpoint
  direction in the plan; replay never infers direction from mapping order or reconsults the registry.
  A curator's impossible inverse request for a literal is a granular
  `queue_direction_conflict`, not a run-level exception.
- The new `consolidate/relation_gate.py` owns the pure D7 decision and structured reason. It consumes
  accepted type/identity decisions plus the predicate-admission result and returns final
  subject/predicate/object-or-literal semantics. `consolidate/gate.py` remains the node-confidence
  gate, and `Companion` remains orchestration rather than semantic policy.
- Manual review cannot manufacture a relation from entity similarity. The historical `link`
  action, which wrote a generic `relates_to` edge without D6 admission or the D7 gate, is retired;
  public node-review actions are `commit`, `discard`, and `merge`. Legacy `review_link` ledger plans
  remain readable for audit but are never executable. The resolver may abandon an untouched legacy
  plan before sealing a supported replacement action, while any receipt or matching graph artifact
  makes the plan a fail-closed recovery case. Relation review remains read/acknowledge-only until a
  future action can seal the complete admitted predicate, direction, grounding, endpoints, and
  anchored Claim through the shared semantic plan.
- No parallel plan format or graph writer is introduced. Typed operations, stable expected artifact
  ids, and per-operation receipts extend the existing ADR 0039 ledger/planner/applier boundary.
  The current critical gap is that `ConsolidationSession` can commit a topology edge before
  `_mint_relationship_claims` later rejects or fails its Claim. The correction is to plan and receipt
  `create_topology_edge` and `mint_claim` separately as one semantic decision, then have Claim
  minting consume the pinned plan semantics rather than normalize or consult mappings again.
- The existing durable `ReviewQueue` gains backward-compatible tagged `node` and `relation` entries
  instead of a second queue. Missing tags in legacy records mean `node`; relation entries retain the
  original `queue_*` reason and expose only actions valid for that kind.

The complete new-module set is therefore limited to `reconcile/decisions.py`,
`predicates/registry.py`, `predicates/admission.py`, and `consolidate/relation_gate.py`. Authority v1
and predicate-alias v1 files remain readable, candidate ids remain stable, plan/receipt readers
continue to accept legacy ledger rows, and review API changes are additive. Predicate direction,
Claim identity, normalization, or survivor-policy changes apply only to a new semantic-policy
fingerprint and fresh graph generation; existing graphs are not migrated in place.

Implementation sequence is: first land pure contracts, legacy readers, and parity/measurement tests;
then complete and verify the ADR 0039 executable-plan and per-operation-receipt boundary; only then
enable each Phase 2–4 decision-path change behind its new policy fingerprint and validate a fresh
rebuild before swap. This ordering prevents semantic cleanup from hiding technical write divergence.

### D8 — Cross-document reconciliation is a first-class phase

Per-file resolution is insufficient for a multi-book or long-lived vault. After each verified file
commit, and during an explicit rebuild, Marginalia proposes cross-document reconciliation against
the AuthorityIndex and committed store.

The phase composes existing machinery and explicit new work:

- deterministic blocking and recall lanes from ADR 0008/0010;
- embedding candidate generation;
- distinguishing-edge/corroboration evidence from ADR 0010 plus source excerpts;
- select framing and cluster-level consistency from ADR 0022;
- new entity-level negative caching of `distinct` decisions in a versioned off-graph side-store;
- predicate negative caching from ADR 0017; and
- off-graph authority records followed by rebuild materialization.

Automatic reconciliation remains same-primitive. A cross-type collision routes to D3. Predicate
upkeep runs after endpoint identity has stabilized, because shared argument pairs are evidence for
predicate synonymy only when those arguments are canonical.

The job is hosted by ADR 0039's queue lifecycle after a verified file commit and during rebuild. It
is blocked by an integrity fence, writes only off-graph records, and reports its own failure in the
outcome without retroactively changing the technical quality of an already verified file commit.
Per-commit proposal cadence is new behavior; apply remains explicit.

The first D8 runtime slice now schedules the existing `reconcile-propose` job immediately after
Companion's post-write integrity outcome is `verified` (and after an explicit rebuild's verified
post-swap audit). Queued passes park until the durable ingest batch is quiet and coalesce across its
files; a pass already running is never reused for a later commit because it may have captured an
earlier generation snapshot. Proposal
runs under the vault writer lock and integrity fence for a stable graph view, but writes only its
durable curation-job result. `reconcile-apply` remains operator-triggered. Schedule and proposal
failures are recorded in the additive `cross_document_reconciliation` outcome while the verified
file/rebuild result remains technically successful. An operator-disabled continuous-curation policy
records this follow-up as `skipped` rather than silently enqueuing work. If shutdown begins in the
narrow interval after commit verification, the proposal is persisted as queued without reopening a
worker and resumes through ordinary curation-job rehydration at the next startup. A rebuild proposal
is queued only after the verified swap has cleared maintenance draining, so it runs in the active
worker rather than being misreported as waiting for a process restart.

### D9 — Semantic policy is versioned and replay-safe

Fingerprinting uses ADR 0039's shared structured schema:

- `extraction_fingerprint` is owned and persisted by ADR 0039; this ADR contributes the
  primitive-guide version and semantic packs only when they affect extraction;
- `semantic_policy_fingerprint` encloses the extraction layer and adds:
  - surface-normalizer version;
  - entity resolver/judge prompt and model identity;
  - predicate-registry version;
  - relation-gate version;
  - enabled semantic packs that affect downstream decisions; and
  - thresholds that can change decisions.

Execution-only settings such as concurrency remain outside both layers. Raw extraction-unit reuse
requires exact extraction-layer equality. Reuse of type, identity, predicate, gate decisions, or a
commit plan requires exact full-policy equality. “Compatible” never means an opaque heuristic over
one flat hash. A policy change never silently mixes old and new decisions in one asserted-quality
baseline.

The first D9 implementation slice now computes these values from one reusable structured builder.
It resolves provider/model and semantic request parameters for extraction, judge, candidate
curation, and relation curation; hashes effective prompts and pack definitions; records effective
chunking and extraction caps/modes; includes embedding identity, semantic consolidation knobs,
contract versions, and the exact predicate-alias sidefile hash; and excludes credentials,
timeouts, retries, concurrency, and embedding request batching. Curation batch size remains in the
full semantic policy because grouping changes the context adjudicated by the curator. New
`ingest_run` start rows persist all three hashes. Resume now fails closed for legacy rows and
requires exact extraction and full-policy fingerprints because the current resume path replays
curator and relation-curator verdicts. Superseded-node/relation LLM audit flags are part of the
full policy because they change those verdicts and review-queue outcomes. Extraction-unit reuse
remains separately guarded by exact extraction equality.

Fresh rebuild adds two distinct D9 boundaries. First, it treats the fingerprint triplet as a pass
invariant rather than end-of-run metadata. Sidefile evolution invalidates the staging pass, so a
receipt can never describe a graph assembled under multiple semantic policies. Second, when a
prior completed run matches the exact full policy, it may supply curator verdicts only after the
ledger proves complete framing, terminal quality, a current-format sealed plan, complete operation
receipts, and unchanged post-run policy. This is decision replay, not plan or write replay: the new
graph gets a new run and sealed plan and re-executes every deterministic admission and write gate.
The first sealed qualifying run is the immutable authority for that exact identity; a later
stochastic redraw under the same fingerprints cannot replace it. Replay snapshots include original
and replay methods, and each replay row retains or reconstructs its original method, timestamp, and
source run id so chains remain auditable across arbitrary rebuild generations.
Exact candidate ids remain the preferred lookup. For relations only, exact-policy completed replay
may additionally use the conservative semantic candidate key defined above when endpoint
reconciliation changes only physical ids. The ledger snapshot supplies candidate payloads and an
unambiguous historical node-id-to-primitive/title index; conflicting identities or structured
verdicts never choose by ledger order: structured verdict conflicts become a deterministic queue
without a model call. Exact sealed node terminal outcomes can also supply their original commit or
queue action. Relation authority follows explicit candidate derivation lineage across deterministic
type correction and endpoint reconciliation; missing, cyclic, or conflicting lineage fails closed.
Fresh rebuild also reuses the newest successful extraction-unit payload for the exact document,
content-addressed unit, and extraction fingerprint. Extraction is a pure function of those source
bytes and the fingerprinted extraction policy; redrawing a stochastic provider during each source
order arm creates a different candidate population before semantic replay can act. Reuse never
replays a plan or graph write: candidates are re-embedded as needed and every identity,
predicate, relation, liveness, seal, apply, receipt, and post-write gate runs again. Missing,
malformed, invalidated, or fingerprint-mismatched extraction evidence is extracted normally.
Completed replay additionally requires the exact `fresh_rebuild.v1` materialization scope. An
incremental run is evaluated against an already-populated graph and is therefore not portable to
an empty staging graph even when every semantic fingerprint matches. Incremental runs remain valid
for crash resume inside their own open run, but cannot become completed-decision authority for a
fresh rebuild. Legacy rows without a scope fail closed.

### D10 — One semantic evaluator, multiple callers

Create one read-only semantic evaluator used by:

- pre-commit diagnostics over ledger/plan data;
- post-commit validation over an ADR 0039-verified graph;
- `scripts/ingest_quality_check.py`;
- rebuild acceptance;
- the Config/Curation UI; and
- deterministic and live evaluation harnesses.

Domain profiles are pack- or file-supplied diagnostic data, not hard-coded production ontology.
The evaluator consumes ADR 0039's audit status and generation id rather than defining a parallel
health vocabulary.

This avoids separate metric definitions in the UI, scripts, and server. The report includes scope,
graph generation, integrity status, semantic-policy fingerprint, completeness/truncation, per-layer
metrics, conflict samples, and the exact evidence set used.

### D11 — Measure recall cost as an architectural invariant

The evaluator records recall independently from answer generation:

- completion/model calls and generated tokens: exactly zero for ordinary recall;
- query-embedding calls, latency, and provider/local execution;
- deterministic store and projection latency at p50 and p95;
- retrieved result count and bytes; and
- a separate optional answer-generation cost when that surface is evaluated.

This is a non-regression contract, not a claim that embeddings or local computation are literally
free. It makes “near-zero recall cost” falsifiable and prevents a semantic-quality improvement from
quietly paying for an LLM at query time.

### D12 — Preserve temporal evidence now; defer full valid-time semantics explicitly

Marginalia currently records transaction/source-recency and supersedence lifecycle evidence
(`asserted_at`, `valid_as_of`, and `valid_until` in the existing contracts), but does not yet expose
Graphiti-style valid-time versus transaction-time query semantics. ADR 0040 does not pretend that a
correction, an extraction error, and a fact that was historically true are the same event:

- extraction and adjudication preserve any explicit source time expression and its byte evidence;
- when an unchanged Claim gains a corroboration with a newer explicit source time, that mention
  becomes the Claim's primary byte anchor; earlier supporting Blocks remain attached through
  `prov:wasDerivedFrom` rather than competing with the current evidence projection;
- relation adjudication receives endpoint type and canonical title, but not mutable endpoint
  summaries: the source excerpt is authoritative for relation grounding, while a stored summary may
  legitimately describe an earlier state of the same stable identity;
- this relation-evidence input contract is versioned in the semantic-policy fingerprint, so changing
  it invalidates prior curator replay instead of silently reusing decisions made from older prompt
  evidence;
- temporal correction fixtures test the existing supersedence path only;
- evaluators label true valid-time questions unsupported rather than scoring them as ordinary
  correction failures; and
- the roadmap's “Bi-temporal claims” item remains the owner of any future first-class valid-time
  facets, interval queries, and invalidate-not-delete behavior.

This is an explicit deferral, not a rejection. A full temporal model needs its own ADR because it
changes Claim/query semantics beyond the predicate and identity cleanup authorized here.

---

## Measurement contract

### Hard invariants

The following are zero-tolerance on a graph advertised as semantically verified:

- committed knowledge entity outside the closed primitive set;
- relation Claim without valid Block/source anchoring;
- topology relation without two live accepted endpoints;
- placeholder or malformed committed predicate;
- predicate absent from the registry (including provisional state);
- confirmed inverse materialized without the required endpoint swap;
- exact cross-type collision silently auto-merged;
- unresolved semantic decision represented as an automatic high-confidence success; or
- semantic report produced from incomplete/truncated data without saying so; or
- ordinary recall that invokes an LLM completion, semantic judge, agent loop, or provider reranker.

### Entity metrics

| Metric | Why it exists |
|---|---|
| primitive type accuracy and macro-F1 | prevents a dominant type from hiding weak minority types |
| exact normalized collision count by same/cross type | exposes fragmentation and type instability |
| B³ precision/recall/F1 | measures whole identity clusters, including fragmentation |
| false-merge and missed-merge counts | keeps precision/recall trade-off explicit |
| canonical-title and alias accuracy | ensures normalization helps without corrupting display names |
| unresolved type-conflict rate | measures abstention/review load |
| canonical identity churn across rebuilds | detects unstable clustering |

B³ originates in ADR 0022 Lever 3. It is exact on the synthetic fixture with complete gold
clusters and estimated on real corpora only over the human-adjudicated population; every report
records the sample definition and size.

### Predicate metrics

| Metric | Why it exists |
|---|---|
| raw and canonical vocabulary size per 1,000 accepted relations | makes corpus sizes comparable |
| raw-to-canonical compression | shows whether mappings reduce fragmentation |
| singleton and provisional share | exposes the uncontrolled long tail |
| mapping precision by `same` / `inverse` / `narrower` | catches destructive over-folding |
| direction/symmetry error rate | prevents semantically reversed facts |
| predicate churn across repeated ingest | detects prompt/model instability |
| unregistered/placeholder rate | hard-policy compliance |

Vocabulary size is a diagnostic, not a target by itself. A tiny vocabulary can be as wrong as a
huge one if it collapses meaningful distinctions.

### Relation metrics

| Metric | Why it exists |
|---|---|
| grounded relation precision | verifies the source supports the triple |
| endpoint correctness | separates good wording from wrong identities |
| usefulness acceptance agreement | measures whether the gate preserves navigable facts |
| literal-vs-topology classification accuracy | prevents artificial entities/edges |
| corroboration rate across sources | measures living-graph convergence |
| isolated accepted knowledge nodes | exposes failed liveness/connectivity |
| relation-set stability across repeated ingest | detects nondeterministic semantic drift |

Named-domain coverage remains a smoke signal. It cannot, by itself, pass semantic quality.

### Recall-cost metrics

| Metric | Why it exists |
|---|---|
| completion calls and generated tokens per ordinary recall | hard pin at zero |
| query-embedding calls and execution location | exposes the only allowed model-like read cost |
| recall p50/p95 latency | prevents quality work from hiding read-path regressions |
| deterministic retrieval bytes/results | makes latency comparisons interpretable |
| optional answer cost reported separately | keeps retrieval and generation economics honest |

### Threshold policy

This ADR intentionally does not invent numeric quality thresholds from the corrupted LOTR graph.
Thresholds are set in this order:

1. implement measurement-only reporting;
2. rebuild representative corpora after ADR 0039 passes;
3. label an adjudication set with human-reviewed type, identity, predicate, and relation outcomes;
4. measure baseline variance across repeated runs;
5. lock per-layer minimums and maximum regression budgets before changing defaults; and
6. version threshold changes with the semantic policy.

Hard invariants are immediate. Statistical quality thresholds become binding only after the clean
baseline is recorded.

Before a new model-backed extraction, judge, redraw, or curation stage becomes a default, its
on/off ablation records the incremental change in the relevant locked quality metrics together with
incremental ingest latency, model calls/tokens, or local compute. This ADR sets no universal ROI
ratio: expensive construction is allowed, but an unmeasured or no-lift stage is not promoted to the
default path.

The implemented ablation boundary is configuration-owned, fingerprinted, and fail-closed.
`consolidation.type_adjudication_enabled` and
`consolidation.relation_curator_enabled` default to `true`. When type adjudication is disabled,
exact-surface cross-type conflicts go directly to the existing review path and the type provider is
never instantiated or called. When relationship curation is disabled, non-prefiltered relations
receive a synthetic queue verdict before prompt construction or policy replay, no provisional
predicate can be minted, and superseded raw relations remain reviewable instead of being silently
accepted. Both flags change the semantic-policy fingerprint but not the extraction fingerprint.
The Config UI exposes them as advanced semantic stages and the write API returns `applied=rebuild`
plus the affected vaults, because saving the policy must never imply that an existing graph has
already been rematerialized.

---

## Evaluation corpus and acceptance matrix

No single corpus may approve the design. The minimum evaluation set is:

| Corpus | What it stresses |
|---|---|
| synthetic adversarial fixture | namesakes, inverses, type ambiguity, Unicode, literals, exact expected clusters |
| LOTR/Hobbit literary corpus | recurring characters/places, aliases, books vs in-world senses, long cross-document narrative |
| SDLC / organizational knowledge | dense taxonomy, domain pack vocabulary, similar organizational concepts |
| chat or WhatsApp corpus | informal aliases, repeated facts, temporal correction, sparse context |
| public LongMemEval-compatible diagnostic | externally reproducible retrieval comparison; never an internal ship gate |

The acceptance run records provider/model/prompt fingerprints and includes:

- one fresh ingest and one identical re-ingest;
- two source orders for the same multi-document corpus;
- provider retry/restart without semantic-policy change;
- a semantic-policy change that forces incompatible decisions to recompute;
- reviewer-confirmed samples from each primitive and predicate state;
- exact byte-grounding verification for sampled relations; and
- completion-free recall cost/latency evidence separated from optional answer generation;
- a fresh rebuild proving the off-graph decisions materialize reproducibly.

The public benchmark lane records the exact dataset revision, subset, ingestion mapping, query
surface, model/embedding versions, judge prompt when a judge is used, and a plain retrieval/RAG
baseline. Vendor headline scores are not accepted as comparable unless the harness and judge match.
This lane is procurement evidence and regression context, not permission to optimize away
Marginalia's provenance or graph-quality contracts.

### Privacy-safe temporal chat draft

`tests/golden/datasets/temporal-chat/` supplies the missing internal chat class without copying a
private WhatsApp export into the repository. Its three fictional messages preserve an initial
owner/place, a timestamped correction, a later confirmation, an informal alias, a literal date,
all five primitive kinds, and a negative budget control. The 2026-07-17 deterministic
Golden/Evolve preflight passed with three input files, 1,425 bytes, and corpus identity
`sha256:c643bbadc969c3c05b39f424f64dbc41e389cc75834d101afb89a4967d73c8b7`.
`CREATING.md` records that the dataset is an automated draft. Until the owner validates its expected
answers and semantic adjudication, it is diagnostic evidence only and cannot satisfy the human-
reviewed matrix gate.

### Four-corpus deterministic preflight

Manifest v4 pins the effective question-set SHA-256, byte count, question count, and one global
`settings.k`, in addition to the corpus identity. On 2026-07-17 the synthetic adversarial,
isolated Hobbit literary, SDLC/organizational private CoP, and privacy-safe temporal
chat corpora all passed the deterministic Golden floor under manifest v4. These runs make the four
inputs reproducible, but do not supply live scenario arms or human adjudication.

The external private organizational corpus (CoP) required a question-contract migration rather than a
copy of its private inputs. The operator-local v2 question file preserves the reviewed expected
answers, uses global `k=10`, and defines 25 exact `gold_targets` across 14 questions. Its preflight
resolved 25/25 targets over 17 files and 165,431 bytes; the corpus hash is
`sha256:92735afc38fcdce6b559eb8eb80aeb08b07793d25f9a647a7a59d720136099da` and the
question-set hash is
`sha256:0c697bfa3515b7c69650f291b9f8edc65fa345e97709284f45ca8164f8b69ee2`.
The harness accepts an explicit `--questions` path in deterministic, single-arm, and A/B modes, so
this alternate contract remains hash-bound without changing or duplicating the source corpus.

### Frozen LongMemEval-compatible diagnostic

`tests/golden/longmemeval/frozen-manifest.json` pins the official cleaned LongMemEval S file at
dataset revision `98d7416c24c778c2fee6e6f3006e7a073259d48f`, the upstream code at
`9e0b455f4ef0e2ab8f2e582289761153549043fc`, the source byte size and SHA-256, and a
`stratified_sha256.v1` selection of five questions from each of six official question types plus
five abstention cases. The selected ids are derived only from the pinned seed, bucket, and
`question_id`; JSON array order cannot change the 35-case subset.

`tests/golden/longmemeval/adapter.py` verifies the source pin, validates the complete question
id/type index, and strictly validates every selected record before producing one Golden dataset and
therefore one vault per question. It maps each timestamped session to one Markdown input containing
both roles under an opaque ordinal name; upstream `answer_*` session ids remain reference-only.
Reference answers, answer-session ids, and per-turn `has_answer` labels never enter the corpus. The
query renders the official `question_date` as its current date, numeric answers become text, and
abstention cases remain negative controls even when the source retains answer-session metadata.
Labels remain outside ingestion as expected answers, exact hashed evidence turns, byte-offset gold
spans, and a case reference record. The materialization manifest records the selected-id hash,
every case-file hash, the derived corpus-tree hash, and unfilled runtime placeholders.

Adapter v3 marks every `temporal-reasoning` question with
`unsupported_capability: valid_time_queries`. Retrieval, exact-span, and provenance measurements
still run, but both Golden judges emit an explicit `UNSUPPORTED` verdict without a judge call and
exclude that row from answer-quality scorecards. This is the executable D12 boundary: session-time
evidence remains measurable without claiming the first-class valid-time query semantics deferred
to the bi-temporal ADR.

The pinned source was materialized locally on 2026-07-17 with adapter v3. The source matched
revision `98d7416c24c778c2fee6e6f3006e7a073259d48f`, size `277383467`, and SHA-256
`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`. The 35 selected ids
hash to `a6f80487a4f3e06fa4390c398e22cf9448b4f760693c29f2165146bc15ce020c`; the frozen v3
manifest hashes to `c59ce922bf0c374ae03d11c2c82a6aa22096688c1cbc6d4dd88aeb208784b8b3`; the derived output
tree hashes to `a32e2cba7ff1bd5d3d9d5774571e8d0f297086c5abd6ae9c32a62fe1c6f7d5c6`. All 35
deterministic floors passed question validation and byte provenance, resolving all 58/58 gold
targets. This proves source pinning, mapping, evidence isolation, and the temporal capability
exclusion.

`tests/golden/longmemeval/diagnostic.py` now owns the long live execution. It persists exact
per-case ingest item ids before waiting, watches only those ids, and then discovers the unique
cross-document reconciliation jobs linked from their durable outcomes. It persists and supervises
only those job ids, ignores unrelated historical failures, and does not begin byte-identity or
question evaluation until each linked proposal job is complete. It verifies full Document-byte
identity before evaluation and resumes without submitting a duplicate batch. One application
serves the isolated managed vaults; each inherits the application provider configuration. The
first case pins the effective provider/model runtime for every later case, and the runner refuses
to proceed unless embedding remains batch size 32 with one concurrent batch. Every subprocess has
a hard process-group timeout and every long stage emits a heartbeat at most 30 seconds apart.

Construction cost is a durable boundary rather than an estimate from the ingest event tail.
`Companion.remember` records one thread-safe `construction_cost.v1` aggregate across extraction,
type adjudication, candidate and relation curation, merge/correction judges, candidate embeddings,
and Claim embeddings. The cross-document proposal runner records its own aggregate. A cost is
`measured` only when every successful completion and embedding request reports usage; missing
provider usage is `partial`, never zero. The diagnostic sums each owned ingest item once and each
unique coalesced proposal job once, so a job linked to many documents is neither omitted nor
multiplied by the number of links. The 80-event per-item inspection cap therefore does not affect
cost evidence. Existing Documents without persisted owned ids, and runs produced before these
aggregates existed, fail attribution and require a fresh isolated diagnostic vault.

The first live smoke case, `07b6f563--ce2765af8fed`, completed all 52 exact ingest items without a
duplicate submission. It exposed that the coalesced proposal job
`reconcile-propose-419a66f198bb` was still adjudicating 163 clusters when the old runner began
identity validation; the loaded server took about 28 seconds to answer a status request and the
identity subprocess hit its shorter HTTP deadline. After that job completed, resume correctly
skipped ingestion but failed because the identity artifact directory did not exist. The runner now
creates that directory and supervises reconciliation explicitly. This smoke is pre-cost incident
evidence, not a measured Marginalia result; a fresh isolated case is required for cost-bearing
evidence.

The upstream-compatible flat-BM25 arm ran on 2026-07-18 across all 35 selected cases. It uses the
pinned session/user-only transformation, literal-space tokenization, `rank-bm25==0.2.2`, ranking,
and metric definitions from upstream revision
`9e0b455f4ef0e2ab8f2e582289761153549043fc`. The retrieval and evaluation source hashes are
`efd7fc5969a904717741fadca3c7dc73611ddbb2aaf3ef33117ebb6943b3e346` and
`c98b8d1096877a15aa755c9de44fe33c195298466a2eb6f3c0f9f6bde8c72349`. Because the pinned
`eval_utils.py` uses `numpy.asfarray`, removed in NumPy 2, the comparison used only the equivalent
`numpy.asarray(..., dtype=float)` compatibility alias; all per-case metrics matched upstream
`eval_utils.py` exactly. The measured artifact is local/private and hashes to
`eab612de4e032075553ad5aa4b04bda40f5b88ff7ab6d9d7420806128dc82960`.

Five abstention cases and four cases without user-side target labels are excluded from upstream
retrieval averages, leaving 26 eligible cases. Measured `recall_any` is 0.6923/0.8077/0.9231/
0.9231/1.0000 at k=1/3/5/10/30; `recall_all` is 0.2692/0.6538/0.7692/0.8846/1.0000. These are
reference-BM25 diagnostic measurements, not Marginalia results or a leaderboard claim. The 35
isolated Marginalia live ingests, completion-free retrieval, direct RAG, and latency/cost
aggregation remain open until the resumable run and its receipt complete.

The frozen manifest names three separate comparison configurations: the upstream flat-BM25
session/user-only retrieval setup as a reference pending a run with pinned upstream code,
completion-free Marginalia `/api/v1/recall`, and direct-reader `/api/v1/ask`. It deliberately
contains no embedded scores; measurements live in access-controlled run artifacts. Synthetic
schema-compatible tests establish conversion determinism, source-pin
failure, label isolation, exact evidence hashes/spans, official numeric/abstention/evidence-label
shapes, and fail-closed selected-record handling without downloading or committing the 277 MB
dataset. The current Golden harness can report span retrieval, intactness, byte-IoU, and token
recall. It now also collapses node hits to the first unique source session and reports session-level
recall-any, recall-all, and binary NDCG at 1/3/5/10/30, matching the upstream comparison
granularity without pretending node rank equals session rank. Approved-provider handling and
runtime pinning are implemented by the runner; retrieval/RAG execution and the final combined
receipt remain open. All remain non-gating. The result is not a LongMemEval leaderboard submission
and cannot satisfy ADR 0040 acceptance by itself.

Production logic fails review if a rule is justified only by improving one corpus while degrading
another without an explicit pack boundary.

---

## Implementation plan

### Phase 0 — Preserve evidence and define the authority boundary

- Preserve the current LOTR ledger/plans as incident evidence.
- Record the evidence provenance required by this ADR.
- Label every report with ADR 0039 integrity/completeness state and graph generation where one
  exists.
- Treat completion of ADR 0039 as the gate for authoritative graph baselines, not for pre-commit
  measurement work.

**Exit:** pre-commit evidence is preserved and cannot be mistaken for a verified graph report.

### Phase 1a — Pre-commit measurement only (may start with ADR 0039 in progress)

- Implement the shared semantic report contract without changing commit decisions.
- Add surface, type-conflict, entity-cluster, predicate-vocabulary, and relation-grounding metrics.
- Add recall-cost instrumentation that proves zero completion calls on the ordinary read path.
- Report raw versus canonical counts and complete evidence scope from ledger/plan data.
- Create the synthetic adversarial fixture and human adjudication format.
- Define the public benchmark adapter and freeze its reproducibility manifest; running the full
  benchmark may wait for the verified graph generation.

**Exit:** current pre-commit behavior has reproducible per-layer measurements and failure samples,
all labelled non-authoritative.

### Phase 1b — Authoritative graph baseline (requires ADR 0039 verified generation)

- Complete ADR 0039 through clean copied-vault rebuild and integrity verification.
- Run the same evaluator over the complete verified graph generation.
- Compare graph metrics with Phase 1a ledger/plan evidence and explain every material delta.
- Extend the quality script and UI to show authoritative raw/canonical counts.

**Exit:** semantic observations are no longer confounded by storage corruption, and clean baselines
exist before any semantic-write default changes.

### Phase 2 — Surface normalization and type adjudication

- [x] Land the off-graph typed decision contract and positive-only Authority fold without changing
  candidate-id formulas.
- [x] Replace identity-path title-normalization variants with the shared lossless contract and
  preserve source surface, aliases, flags, and normalizer version through correction and dedup.
- [x] Apply ordered type-correction chains before identity resolution, derive corrected candidates,
  remap their edges, and retain the full derivation in the ledger.
- [x] Adjudicate unresolved exact-surface type conflicts from bounded source evidence before identity
  resolution; apply only confidence >= 0.95 corrections, remap their relations, and retain malformed,
  partial, low-confidence, or still-cross-type cases in review without blind merge.
- [x] Detect same-surface cross-type conflicts against the active run and safe store, force unresolved
  candidates to review, and never auto-merge them.
- [x] Enforce `DistinctDecision` as a negative cache across exact, fuzzy/judge, store, post-curator,
  and retroactive reconciliation paths.
- [ ] Re-run entity metrics without enabling cross-type auto-merge.

**Exit:** no cross-type conflict disappears silently, and encoding/name cleanup is reversible.

### Phase 3 — Predicate admission and registry

- Preserve `PredicateAliasIndex` mapping records and add the sibling per-predicate registry record.
- Route all relation labels through one mapping/admission service.
- Ensure canonicalization and confirmed inverse handling happen before Claim identity.
- Record definition, direction, type signatures, support, samples, and decision provenance.
- Keep novel grounded relations as provisional rather than replacing them with a generic edge.

**Exit:** every committed predicate is governed and replayable; placeholder rate is zero.

### Phase 4 — Semantic relation gate

- Return structured relation-gate reasons.
- Separate endpoint, grounding, predicate, structural-noise, and unsupported-inference failures.
- Preserve gate reasons through ADR 0039 plan operations and receipts.
- Validate literal-versus-topology classification.
- Prove the liveness gate cannot retain an entity solely through a rejected relation.

**Exit:** a reviewer can trace every accepted/rejected relation to one explicit policy decision.

### Phase 5 — Cross-document reconciliation

- [x] Emit complete opaque per-generation semantic snapshots and an offline exact-churn comparator.
- Run identity reconciliation after verified file commits and during rebuild.
- Use canonical identities before predicate shared-pair analysis.
- Preserve aliases/corroboration and negative decisions in off-graph ledgers.
- Measure identity and predicate stability across source order and identical re-ingest.

**Exit:** repeated referents converge without cross-type destructive merges or ingest-order drift.

### Phase 6 — Review and configuration UX

Keep normal usage simple: provider, model, source, ingest. Advanced semantic controls live in
Config/Curation and show:

- semantic policy/fingerprint;
- type-conflict queue;
- identity clusters and aliases;
- predicate registry, provisional tail, and mapping proposals;
- relation-gate reasons and source excerpts;
- per-layer quality scorecard; and
- rebuild-required warning for material semantic-policy changes.

The UI edits durable decisions and policy; it does not patch live graph rows.

**Exit:** an operator can understand, confirm, reject, and rebuild semantic decisions without
editing JSON or YAML manually.

### Phase 7 — Default flip and fresh rebuild

- Run the full acceptance matrix on the four gating corpus classes; run the public benchmark lane
  as a non-gating diagnostic.
- Lock thresholds and regression budgets from clean evidence.
- Record on/off quality and construction-cost ablations for every new model-backed default stage.
- Review false merges, inverse mistakes, and rejected useful relations manually.
- Rebuild fresh graphs from markdown plus the accepted off-graph mappings.
- Swap only after ADR 0039 technical integrity plus every registered semantic hard invariant passes.
- Flip defaults only after the new policy meets every hard invariant and locked quality gate.

**Exit:** semantic verification is a supported product state, not an exploratory report.

---

## Rollback and recovery

- A normalizer, type-guide, judge, registry, or relation-gate regression is rolled back by restoring
  the prior semantic-policy version and selecting the current generation's verified predecessor.
- Every successful rebuild publishes `semantic-materialization.json`, binding its graph generation
  to the complete config, extraction, and semantic-policy fingerprint triplet. The predecessor's
  receipt is retained beside `previous-graph.lbug` under the replacement generation's artifact
  directory. The same artifact holds `previous-semantic-policy.json`, an exact-byte, hashed
  checkpoint of predicate registry/aliases and authority/identity decisions. Live writes invalidate
  the active receipt rather than allowing stale policy identity.
- `POST /api/v1/curation/rollback` accepts no filesystem path. It captures the live generation,
  stages a copy of that generation's predecessor, runs physical integrity and registered semantic
  gates, requires the effective config/extraction fingerprints to match the predecessor, restores
  the hashed decision checkpoint, reproduces the complete semantic-policy fingerprint, and swaps
  only under the runtime fence. The original predecessor remains intact and the displaced graph is
  preserved under a unique rollback-job artifact directory.
- A policy mismatch or pre-swap audit failure leaves the live graph untouched. A post-swap audit
  failure leaves the runtime writer-fenced for investigation rather than labeling unverified bytes
  healthy.
- Authority and predicate records are versioned and revocable; removing or rejecting a mapping
  changes the next rebuild without graph surgery.
- The previous verified graph remains available until the replacement rebuild passes technical and
  semantic validation.
- Legacy graphs remain readable. Their predicates are classified as governed, provisional, or
  unregistered during audit. Query-time result folding remains a non-mutating, version-visible
  projection; underlying graph rows are not rewritten on read.
- A rebuild failure or quality regression leaves the old graph active and emits an explicit failed
  candidate-generation result.

Focused model-free evidence on 2026-07-17 covers successful generation restoration, preserved
checkpoint bytes, displaced-generation retention, published rollback materialization, policy
mismatch refusal before mutation, API evidence preflight, and queue/runtime integration. This is
backed by a live six-source synthetic demonstration refreshed on 2026-07-18: policy generation
`b9b06c91-7b3f-4f5d-997a-af6612b29488` rolled back to
`89142fab-fcc8-465b-93af-35e1f8f22c4c`; predicate-registry SHA-256 returned exactly to
`198c90b1081cb2a61e771aed094a8868590a0deeec202b72ee1dc2ba444edd9f`; the semantic fingerprint
returned to `edcf1527bca001bda86f6c1135b124b0473859f14eada855cfb7f0615de5d0fa`; both physical audits and
the semantic gate passed; the writer remained unfenced; and the displaced graph was retained. The
durable receipt is
`~/.marginalia/quality-runs/adr0040/semantic-adversarial/policy-rollback-evidence.json`.

The first apparent same-policy comparison changed identities by 70%, predicates by 60%, and
relations by 60%, but subsequent pass-level evidence proved that comparison inadmissible: its
baseline had evolved the predicate/authority sidefiles during materialization and the rebuild
receipt had stamped only the final fingerprint over the mixed-policy graph. A convergent rebuild
boundary now observes the full fingerprint triplet before the pass, after every source, and at the
end. Any drift discards the entire staging graph and starts a new empty pass; only one stable pass
may publish a report, receipt, or swap. The bounded budget separates up to three policy-changing
discovery passes from one required full-corpus validation pass; consuming the discovery budget no
longer removes the only pass capable of proving stability. Drift in that final validation pass
fails closed at the first verified source. Focused model-free rebuild and server suites cover both
stabilization and bounded failure.

After that correction, one valid equal-policy comparison still exposed a second owning defect:
the final fingerprints matched, extraction units were reused, but completed curator and relation-
curator decisions were redrawn. The two graphs changed identities by 57.1429% and predicates and
relations by 80%. Completed semantic-decision replay now requires exact document identity, model,
block count, config fingerprint, extraction fingerprint, semantic-policy fingerprint, terminal
complete outcome, current-format sealed plan, and complete operation receipts. It reuses only the
immutable verdict evidence. Every materialization creates a distinct run, plan, operations, and
receipts, and deterministic endpoint/predicate gates execute again. Missing, malformed, partial,
legacy, policy-mismatched, or open evidence is never treated as a cache hit.

Live generation `fb70a2d9-367f-469f-bc06-726af8ef7cb0` then rebuilt all six synthetic sources in
one stable pass under the same config, extraction, and semantic-policy fingerprints as generation
`2b2afcac-29aa-4a78-9190-da0307c751de`. All six source audits plus final and post-swap audits were
verified, the semantic gate passed, and 40 node/relation verdicts were replayed with explicit
origin-run evidence. Population remained 5 identities, 3 predicates, and 3 relations, with zero
added or removed members and `0.0` churn in every dimension. The retained report and comparison are
`~/.marginalia/quality-runs/adr0040/semantic-adversarial/policy-replay-report.json` and
`policy-replay-churn.json`. That single repeat was necessary but not sufficient. A later identical
incremental reingest ran against the already-populated `fb70…` graph and became the newest
exact-policy completed run. Rebuild generation `fe405dda-cacd-4402-b10e-0fbb023445d6` then
incorrectly projected that populated-graph evidence into empty staging and retained only 2
identities, 1 predicate, and 1 relation. Its backup proves the immediately previous `fb70…`
generation contained 5/3/3; this was a replay-eligibility defect, not model-backed curator drift.

Completed-decision selection now records and requires `fresh_rebuild.v1`. Live baseline generation
`b75f2851-877c-4871-9805-f5e34473269b` established new scoped evidence, and repeat generation
`89142fab-fcc8-465b-93af-35e1f8f22c4c` replayed 41 node/relation verdicts; every origin run carried
that same scope. Both generations passed final integrity and the semantic swap gate. Population was
3 identities, 2 predicates, and 2 relations in both runs, with identical member hashes, no added or
removed members, and `0.0` churn in every dimension. The durable comparison is
`~/.marginalia/quality-runs/adr0040/semantic-adversarial/fresh-scope-repeat-churn.json`. This closes
the synthetic replay-context stability defect; it does not establish semantic-quality acceptance
or replace the still-pending locked multi-corpus stability budget.

A subsequent third rebuild found one more replay-boundary failure: the ledger selector chose a
valid prior replay run, but the snapshot read only `curator` and `relation_curator` rows, excluding
the selected run's `policy_replay` rows. It therefore redrew verdicts, and predicate-definition
variance caused a semantically empty but physically valid 0/0/0 graph. Generation-bound rollback
restored `89142fab-fcc8-465b-93af-35e1f8f22c4c`. The selector now treats the first sealed exact-
policy run as canonical, snapshots include both replay methods, and legacy rows recover the
original method from structured relation evidence. Corrected generation
`0352d4e8-485c-48f9-996b-b9bb3c212044` preserved the same 3/2/2 member hashes with zero churn,
while final/post-swap audits and completion-free recall remained clean. This is the current
fresh-rebuild evidence pinned by the collection.

---

### CoP corpus baseline — 2026-07-18

The first complete 17-source CoP diagnostic ingested all 165,431 source bytes
without an item error under `desktop/qwen3-embedding-4b`, dimension 2,560, embedding batch size 32,
and one concurrent embedding batch. All 17 outcomes were technically complete and integrity-
verified on graph generation `3d44be95-597a-43f0-9858-35fa196f30d3`. Construction cost was 68
embedding calls over 1,717 inputs and 921 completions over 1,394,776 input plus 1,101,355 output
tokens. The exact post-ingest reconciliation covered all 17 item ids, adjudicated 99 clusters, and
added 260 completions. The continuous scheduler then submitted another 99-cluster reconcile after
the exact job completed. That was duplicate scheduling, not required semantic work; ADR 0009 now
defines a completed, generation-bound exact job as coverage while retaining drift and predicate
sweeps.

The stable Golden/Evolve capture contains 14/14 responses. Deterministic provenance reported zero
failures, all 12 answerable gold-span questions retrieved their spans at `k=10`, all spans were
intact with token recall 1.0, and all 140 cited hits reopened and byte-verified. Ordinary recall
used zero completions, one query embedding per question, and measured 1,510 ms p50 / 2,065 ms p95
total latency. The graph had 420 primitive entities, 1,390 predicate assertions, 368 relation
claims, no dead endpoints, missing topology edges, self-loops, unanchored claims, exact duplicate
groups, or cross-type title conflicts. It also had 56 raw predicates, 14 singleton predicates, 55
historically superseded claims, 11 inactive topology edges, and 108 topology-isolated entities.

This is a clean technical baseline, not ADR 0040 acceptance. The semantic evaluator correctly
returned `incomplete`: type accuracy, identity-cluster precision/recall, literal-versus-topology
classification, relation grounding, inverse-direction evidence, uncertainty abstention, and exact
source-byte reopening still lack the required adjudicated evidence. The question set itself also
needs repair before it can serve as human ground truth. Its test-runner negative control says the
exact message is absent, but `azure-ml-production.md` contains `ERROR: Use ./run_tests.sh instead`
verbatim. Its cross-document “same obstacle” question admits both a trust/verification reading and
a trust/cultural-resistance reading while prescribing only the latter. Neither item may be counted
automatically as a system failure or success until the fixture is corrected and adjudicated.

The capture also exposed a harness boundary: the server returned its named retryable
`409 audit_busy` while a duplicated reconciliation held the stable-snapshot lock, after all paid
question calls had completed. Semantic capture now retries only that named condition with bounded,
visible backoff. If contention outlives the budget, a complete response batch is retained only when
its post-loop marker and JSONL count agree, so resume starts at the missing semantic sidecar rather
than repeating `/ask`. Partial batches remain ineligible. These corrections preserve evidence and
cost without weakening the semantic fail-closed gate.

The same run retained at least 495 Correction Judge requests in its bounded UI event history, and
the per-source construction totals show that this stage dominated several first-time source
ingests. First ingestion cannot correct a prior version of the same source. ADR 0022 therefore now
limits incremental correction candidates to unsupported Claims anchored on Blocks orphaned by the
exact edit; cross-document comparison remains the job of exact reconciliation. This is an
architectural candidate-boundary correction, not a lower-quality call cap. Its construction-cost
effect still requires a measured fresh ablation before acceptance.

A subsequent fingerprint-authoritative rebuild, `rebuild-5c585bb27764`, exercised the bounded
stabilization failure path on the full corpus. Its third pass registered the legitimate provisional
predicate `derived_from` after the first source, changing the pass-start semantic policy and making
that staging unswappable. Before the then-current implementation reached the end-of-pass gate, an
independent exception occurred while sealing `azure-ml-production.md`; the retained generation
`f24ed706-78df-497f-b895-15d5cc47d5e2` contains eight source-complete zero-issue audits and no final
audit or swap receipt. The live generation remained unchanged, no previous-graph backup was
created, the staging database was retained under `rebuild-artifacts/`, and policy sidefiles were
restored to the pre-job fingerprint. This incident motivated two owning-boundary corrections:
the final pass now stops after its first verified drift, and the same per-source audit is appended
to the source ingest run as its definitive integrity outcome. Focused tests cover verified and
failed audit linkage plus two complete discovery passes followed by a one-source final-pass stop.
The incident also exposed an observability defect: the curation job and top-level state replaced the
wrapped exception with the generic `ingest callable raised an error`, even though the retained
staging artifact still identified the current source. Rebuild now persists the sanitized concrete
exception boundary in both surfaces and merges terminal state onto the last durable build evidence
instead of replacing it. A later isolated replay reached extraction and then stopped at an expired
gateway credential before the historical failure point, so it does not establish the original
exception category; subsequent preflights passed only after the development server reloaded the
updated credential.

Rebuild `rebuild-291083bee149` then exercised those corrections over the same 17-source corpus.
Both discovery passes completed all 17 sources. The final pass remained stable through its first
ten sources, registered the new provisional predicate `participation_note` while processing
`cop-kickoff-2026-05-05.md`, and stopped immediately after that source's verified audit at 11/17.
The retained staging generation `3189b25e-10ec-409c-97eb-c559ebbcc4dc` passed its complete
structural audit with 1,240 nodes, 4,415 edges, and zero structural issues, but failed the semantic
swap gate because its policy fingerprint differed from the pass-start fingerprint. No live graph
swap or previous-graph backup occurred, and the source's `after_source` audit remained linked to
the same ingest run. This is live evidence that bounded final-pass early termination and audit
linkage work; it is not a successful CoP materialization or an ADR 0040 acceptance arm.

That fail-closed run exposed a separate pre-swap restoration defect. `run_rebuild` handled
`RebuildAuditFailed` in a dedicated exception branch that re-raised before the generic `not
swapped` cleanup, so the graph stayed unchanged while the evolved predicate registry leaked out of
the rejected staging pass. Restoration now depends only on whether a swap occurred, not on the
exception class, and focused tests cover both ordinary runtime failure and semantic-audit failure.
For the already-failed run, the first `expected_before` value for every predicate touched by the
job's ledger operation receipts reconstructed the exact 69-record pre-job registry. Restoring it
returned the live semantic-policy fingerprint to
`sha256:3b2fc071d99016f43d1da25c9cda4c3ae272ad4316a0112543d27f66c73da94b`; the server governance
read model confirms the same fingerprint and no `participation_note` record. A new CoP rebuild is
still required before this corpus can supply a verified fresh-generation quality or Golden arm.
The run also proved that the old three-pass total conflated the mutation budget with the required
stable proof: CoP legitimately changed policy in all three passes, so no pass remained to
materialize the settled third-pass policy. The rebuild budget now allows three policy-changing
discovery passes and reserves a fourth pass for full-corpus validation; drift in that validation
pass still stops and fails closed at its first verified source.

The subsequent process restart exposed two additional lifecycle boundaries. First, the generic
curation queue treated every interrupted runner as an idempotent upsert and automatically requeued
the expensive vault-wide rebuild. Rebuild, rollback, heal, and re-embedding are now terminal after
a process interruption and require a new explicit submission; small idempotent reconcile jobs keep
their existing retry behavior. Second, restoring the live policy after a failed semantic pass also
discarded technically verified discovery, causing an explicit retry to rediscover the same
predicates. A bounded semantic failure now writes a separate, secret-free pending checkpoint bound
to the exact ordered source-byte manifest and the complete pre-attempt fingerprint triplet. The
live graph and live policy are still restored unchanged. A later explicit rebuild may seed its
private build window from that checkpoint only when all bindings still match, and the checkpoint
is removed only after a verified graph and semantic materialization are installed together. Stale,
corrupt, or mismatched checkpoints are ignored and removed; they can never authorize a graph swap.

Explicit rebuild `rebuild-adc9404a1cdd` then completed 13/17 source audits with zero structural
issues on staging generation `d7ffba11-cc1c-4355-a4ca-c4c6f5cde804` before the development-server
process and its supervisor disappeared while processing `project-to-universal.md`. The LiteLLM
gateway remained healthy; the failure boundary was local process lifetime, not a provider timeout
or graph audit. No swap occurred and live generation
`3d44be95-597a-43f0-9858-35fa196f30d3` remained unchanged. Because a process death cannot execute
Python `except` or `finally`, the in-memory cleanup contract could not restore semantic sidefiles.
The pre-job registry was reconstructed from the job's durable operation receipts, restoring the
exact fingerprint
`sha256:3b2fc071d99016f43d1da25c9cda4c3ae272ad4316a0112543d27f66c73da94b`; the partial graph family
and 13 verified per-source audits were retained under that staging generation's artifact directory.
A second already-submitted attempt, `rebuild-d5af9e244798`, was interrupted at source 1/17 by the
first source-code reload and was retained separately as staging generation
`de982f0f-b435-48a8-ba7a-44f22e572554`. Neither attempt is a successful materialization or
acceptance arm.

Rebuild now writes an exact, job-bound pre-mutation semantic-policy checkpoint before entering
maintenance. Startup rehydration invokes a rebuild-owned recovery callback before making an
interrupted job terminal. When the live graph generation still equals the checkpoint's pre-job
generation, recovery verifies and restores the exact sidefile bytes, recomputes the expected
semantic-policy fingerprint, moves the closed staging graph family to
`rebuild-artifacts/<generation>/staging.interrupted.lbug`, and records
`phase=process_interrupted`. If the live generation differs, recovery does not apply old policy to
possibly new graph bytes; it leaves the checkpoint and staging untouched and reports a fail-closed
swap-boundary error for explicit audit. Normal success and handled pre-swap failure remove the
ephemeral job checkpoint. Focused queue and rebuild suites cover callback ordering, exact pre-swap
restoration, staging retention, normal cleanup, and the no-restore post-swap fence.

The repeated CoP attempts also grew `candidate-ledger.jsonl` to 1,252,336,218 bytes. Operational
replay queries were each calling `scan()` or `records()`, so a single source boundary could read and
deserialize the complete append-only history several times. The JSONL remains the durable authority;
the runtime now builds one validated, memory-bounded offset index per immutable file signature and
extends it after in-process appends. Resume, extraction replay, plan validation, receipt lookup, and
decision replay parse only the required record kinds and run ids. The index stores offsets and a
small candidate-identity projection rather than retaining the 1.2 GB payload as Python objects.
External changes invalidate it by file identity, malformed or unknown-version rows remain
fail-closed, and the full `scan()` path remains authoritative for quality evidence.

The first full CoP retry after the Ladybug 0.18.2 upgrade, `rebuild-ea25f24be0dc`, demonstrated a
separate plan-identity defect rather than an engine or semantic-quality result. Its retained
staging generation `8097518b-ef9c-4047-a298-f58a91b93618` passed eight source-complete structural
audits with zero issues before a canonical relation received both a commit and a later redundant
rejection under the same candidate id. The executable-plan validator failed closed because the
materialization still selected the committed candidate while its pinned trace had been overwritten
by the rejection. No swap or previous-graph backup occurred. ADR 0039 owns the correction: exact
duplicates are coalesced before curation and only the committed relation owns the materialization
trace. This failed run is technical-boundary evidence only and contributes no ADR 0040 acceptance
measurement.

The corrected retry, `rebuild-a5728ddd86dc`, proved that plan-identity repair across the earlier
failure point: `azure-ml-production.md` completed with a verified after-source audit in each reached
pass. Discovery pass one completed all 17 sources and changed semantic policy from the restored
pre-job fingerprint; discovery pass two also completed all 17 sources and settled four additional
provisional predicates. Validation pass three then completed 11/17 verified source audits on
staging generation `c0c877ab-aaa4-4c62-8d76-8c964ad27064` before embedding failed while processing
`core-framework.md`. The sanitized application boundary was `EmbeddingProviderError: litellm
embedding failed for litellm_proxy/desktop/qwen3-embedding-4b`. A controlled two-input call through
the same provider reproduced HTTP 502, while the LiteLLM gateway health endpoint remained HTTP 200.
The registered embedding deployment pointed to a private-LAN model host `/v1` route, whose `/v1/models`
endpoint independently returned nginx HTTP 502. This is a route-specific embedding-backend outage,
not a graph audit, decision replay, dimension, or gateway-process failure.

The rebuild again failed closed before swap. Live generation
`3d44be95-597a-43f0-9858-35fa196f30d3` remained at 1,768 nodes and 6,202 edges; no previous-graph
backup was created. The 32,423,936-byte failed staging database and its 11 zero-issue after-source
audits remain retained under that staging generation's artifact directory. The job-bound recovery
checkpoint was removed after exact restoration of the pre-job semantic-policy fingerprint
`sha256:3b2fc071d99016f43d1da25c9cda4c3ae272ad4316a0112543d27f66c73da94b`. Embedding configuration
remained `litellm_proxy/desktop/qwen3-embedding-4b`, dimension 2,560, batch size 32, and one
concurrent batch. No retry is admissible until that exact embedding deployment passes a fresh
preflight. This retained attempt is technical failure-boundary evidence only; it supplies neither a
fresh CoP materialization nor ADR 0040 multi-corpus acceptance evidence.

At the 2026-07-20 14:37 UTC follow-up, the deployment's `/v1/models` endpoint had recovered to
HTTP 200. The exact LiteLLM route then returned two 2,560-wide vectors, and `glm-5.2` returned the
required `pong` with no extra request parameters. The gateway's aggregate `/health` request timed
out once at ten seconds, but both owned model routes completed successfully. The curation queue had
no queued or running job and retained `rebuild-a5728ddd86dc` as terminal `error`; the monitor-only
boundary therefore did not submit a replacement rebuild. At 15:07 UTC the deployment's
`/v1/models` endpoint regressed to HTTP 502 while the curation queue remained empty, proving that
the apparent recovery was transient and that a replacement rebuild would have been unsafe.

---

### Model-stage ablation pilot — 2026-07-18

Three isolated six-source semantic-adversarial vaults ran sequentially against the same corpus,
code, LiteLLM provider/model, embedding model, and execution limits. Embedding stayed pinned to
`desktop/qwen3-embedding-4b`, dimension 2,560, batch size 32, and one concurrent batch. Manifest v5
pins the full consolidation, execution, and ingest surface while allowing exactly one declared
construction-stage flag to differ across isolated vaults. Both the type and relation arm parity
checks passed. All 18 source items completed without a provider or integrity error.

The all-on arm produced 18 stored primitives, 29 predicate assertions, and 11 relation Claims at a
construction cost of 24 completions, 69,127 input tokens, 11,582 output tokens, 12 embedding calls,
and 47 embedding inputs. The type-adjudication-off arm produced 14 primitives, 23 predicate
assertions, and 7 relation Claims with 18 completions. Its one cross-type collision followed the
configured deterministic review path and made zero type-adjudicator calls. In the enabled arm the
one type-adjudicator call also returned `queue_review` because confidence 0.90 was below the 0.95
threshold. Since the provider generated materially different extracted node and relation sets in
the two arms despite the same extraction fingerprint, this single pair does not isolate or prove a
type-quality lift.

The relation-curator-off result is fail-closed by design, not a node-curator failure. The candidate
curator returned `commit` for 19 nodes and `queue` for 3. Disabling the relationship curator then
synthetically queued all 31 relations before prompt construction. With no accepted relationship,
the downstream relationship-liveness gate queued 21 otherwise admissible nodes; all 22 proposed
nodes and all 31 relations therefore ended terminally queued, leaving only Document/Block
structure in the stored graph. The all-on arm instead recorded 24 relation-curator commits, 23
accepted relationships, and 18 stored primitives. This proves that the switch and its fail-closed
dependency path are wired correctly; without adjudicated relation labels it does not prove the
enabled stage's semantic accuracy.

All three arms retrieved all 11 draft gold spans at `k=10` with zero recall completions, including
the arm with no semantic primitives. That result is useful but non-discriminating: byte-grounded
Document and Block retrieval can recover source spans even when semantic construction admits
nothing. It must not be substituted for type, identity, predicate, or relation quality. The local
diagnostic artifact is
`tests/golden/results/semantic-adversarial/adr0040-model-stage-ablation-diagnostic.json`; it is
explicitly `acceptance_eligible=false` and records the graph generations, fingerprints, evidence
hashes, costs, and blockers.

A second same-vault type probe then exercised the existing fresh-rebuild extraction replay. Both
arms rehydrated the same six durable extraction payloads with attempt zero and made no extraction-
provider call. Manifest v5 found no difference outside
`consolidation.type_adjudication_enabled`. The disabled generation
`fdb75934-aafc-4bc5-a70e-4d184b1ee81f` and enabled generation
`a8cdf828-a103-4ecf-9800-ce01ddefea31` both passed final and post-swap integrity audits. This closed
the extraction confound but exposed a narrower one: changing the broad semantic-policy fingerprint
also invalidates and reruns unchanged stochastic candidate and relation curators. The disabled arm
recorded 18 candidate-curator commits and 5 queues; the enabled arm recorded 19 commits and 4
queues. Its only type-adjudicator call returned `queue_review` at confidence 0.90 below the 0.95
threshold, producing the same terminal identity-policy queue as the disabled path. The resulting
graph differences therefore cannot be attributed as a type-quality lift.

A valid acceptance arm now requires human per-layer labels plus either repeated-run variance or a
test boundary that replays every unchanged stochastic stage while reevaluating only the named free
stage. Extraction replay alone is necessary but insufficient. This is an evaluation-isolation
requirement; weakening production's broad fail-closed policy fingerprint would be the wrong fix.

---

### One-source supervised validation — 2026-07-28

This entry records a technical-path validation and is deliberately **not** ADR 0040 acceptance
evidence. It measures no entity, predicate, relation, or recall-cost metric against the threshold
policy, covers one source rather than the four-corpus matrix, and is `acceptance_eligible=false` by
the same rule that governs the earlier diagnostic slices.

Its purpose is narrower: the two 2026-07-20 rebuilds both ended terminal `error` at the shared
embedding boundary, classified `upstream_unavailable` after the private-LAN model host's llama-swap
process went down behind an nginx HTTP 502. Because they died on infrastructure, they left open
whether the construction pipeline itself could still complete under the newly enforced capacity
policy. Phase 1b requires an ADR 0039 verified generation before any authoritative graph baseline,
and neither failed rebuild produced one — both retained a failed staging graph with
`swap_allowed: false` and `final_audit: null`.

On the same source commit as ADR 0039's one-source validation, a restarted supervised server ran a scratch vault
`step8-onesource-validate` with packs `core, research, personal, sdlc` through the approved
Marginalia to LiteLLM Gateway to llama-swap chain on `<private-lan-model-host>`. The completion
model was `desktop/qwen3.6-35b-10-parallel`; `glm-5.2` is not accessible to the managed credential
and was rejected at the gateway, so it is not a usable arm for this deployment. Embedding remained
pinned to `desktop/qwen3-embedding-4b`, dimension 2,560, batch size 32, one concurrent batch, matching
every prior arm. Effective extraction and curation concurrency resolved to 10 because that alias is
the one declared parallel-capable model.

The queue item reached terminal `done` with outcome `quality: complete`, `receipts_complete: true`,
zero provider failures, and a post-file audit of `verified`. Its recorded construction cost was 7
completion calls, 27,636 input tokens, 8,522 output tokens, 2 embedding calls, and 38 embedding
inputs for one source, all with usage present, so the cost accounting is `measured` rather than
partial. Cross-document reconciliation ran to `state: complete` at the `propose` stage as job
`reconcile-propose-9f5d9c36ba8c`, triggered by `verified_file_commit`, and proposed 5 clusters
against generation `59a2ed6b-fa77-4a1a-8400-6ed3db19fb42`. Those clusters are proposals awaiting
adjudication; they are not merges and carry no quality claim.

The resulting generation passed a full audit — 100 nodes, 318 edges and adjacencies, zero issues,
`writer_fenced: false` — with the endpoint and the on-disk sidecar reporting the identical audit id
`86a7b2baf5ef44179778828d42786277`. Phase 1b's technical precondition is therefore demonstrably
reachable again on current code and current infrastructure. Phase 1b itself remains open: it needs a
verified generation over a real corpus, not one source, and the semantic metrics still have no
authoritative baseline.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| over-canonicalization erases meaningful relation nuance | separate exact, inverse, narrower, and distinct; keep provisional labels |
| false entity merge collapses namesakes | same-type blocking, structural contradictions, select judge, review band, reversible authority records |
| type correction relabels a legitimate alternate sense | cross-type adjudication can return different senses; negative cache; never blind merge |
| normalization corrupts multilingual names | preserve source/display text; compare through a separate key; no heuristic mojibake rewrite |
| vocabulary remains huge | measure canonical/provisional tail, bounded predicate upkeep, cluster only around supported anchors |
| vocabulary becomes too small | relation-specific review and mapping precision; no `related_to` fallback |
| LLM judge drift changes identities between runs | semantic-policy fingerprint, repeated-run stability metrics, deterministic candidates, human-reviewed fixtures |
| incremental decisions leak populated-graph context into a rebuild | scope completed replay to fresh-graph materialization; legacy or incremental evidence fails closed |
| rules overfit LOTR or SDLC | multi-corpus acceptance and explicit pack boundary |
| Claim ids churn after predicate decisions | canonicalize before id; materialize policy changes through fresh rebuild |
| review queue becomes unmanageable | deterministic exact tiers first, negative cache, evidence-ranked batches, bounded jobs |
| semantic quality adds hidden LLM cost to recall | zero-completion hard invariant, retrieval/generation split, p50/p95 and call-count evidence |
| temporal correction overwrites historically true facts | preserve source time evidence; label valid-time unsupported; keep full bi-temporal work in its own ADR |

---

## Rejected alternatives

### Hard-code a complete global predicate list

Rejected because Marginalia is open-domain. It would trade vocabulary fragmentation for lost facts
and corpus bias. A governed provisional lifecycle preserves openness without accepting arbitrary
strings as stable ontology.

### Let LiteLLM or the selected model define the ontology

Rejected. LiteLLM adapts model/provider capabilities; it does not own Marginalia's entity,
predicate, provenance, or quality contracts. Model outputs are proposals evaluated by application
policy.

### Fix type conflicts by merging across primitives

Rejected because it converts classification uncertainty into destructive identity decisions and
can collapse legitimate same-title senses. Type is adjudicated upstream; uncertain collisions stay
reversible and reviewable.

### Normalize names by overwriting titles

Rejected because source spelling and Unicode are provenance. Matching keys and canonical display
forms are separate data.

### Use only named-entity coverage as the quality gate

Rejected because the LOTR audit found 21/21 targets while predicate and type fragmentation remained
severe. Coverage is necessary for recall diagnostics, not sufficient for graph quality.

### Repair the live graph in place

Rejected because markdown is the trust root and semantic mappings are probabilistic. Off-graph
decisions plus fresh validated rebuilds are reversible and auditable.

---

## Exit criteria

This ADR may move from Proposed to Accepted only when:

- ADR 0039's integrity contract passes on the evaluated graph generation;
- one shared semantic evaluator serves pre-commit, post-commit, script, UI, and rebuild paths;
- source surface, conservative exact key, broader discovery key, canonical title, and aliases are
  distinct and losslessly recorded;
- cross-type conflicts produce explicit correction, distinct-sense, or review decisions and never
  auto-merge;
- entity cluster quality is measured with B³ plus false/missed merge samples;
- every committed predicate is canonical, mapped, or registered provisional;
- predicate lifecycle records and predicate-to-predicate mapping records are separate, versioned,
  and compose through one registry boundary;
- predicate alias, inverse, narrower, and distinct decisions are separately represented;
- predicate canonicalization occurs before semantic Claim identity;
- every committed relation passes grounding, endpoint, predicate, structural, and non-redundancy
  checks, while broader usefulness is measured separately;
- extraction and semantic-policy fingerprints are layered, structured, and enforce stage-specific
  reuse equality;
- ordinary recall makes zero LLM completion/judge calls, reports query-embedding and deterministic
  retrieval cost separately, and keeps optional answer-generation cost outside recall metrics;
- temporal correction tests do not claim first-class valid-time support, and explicit time evidence
  remains available for the future bi-temporal ADR;
- the public benchmark diagnostic has a reproducible manifest and a plain retrieval/RAG baseline;
- every new model-backed default stage has a recorded on/off quality and incremental construction-
  cost ablation;
- the multi-corpus acceptance matrix passes the hard invariants and locked per-layer thresholds;
- identical re-ingest and source-order variation stay within the locked identity/predicate/relation
  churn budgets;
- accepted off-graph decisions reproduce on a fresh rebuild; and
- the rebuild passes ADR 0039 integrity plus registered semantic hard invariants before swap;
- rollback to the prior policy and graph generation is demonstrated without editing source files or
  live graph rows.

---

## Relationship to ADR 0039

ADR 0039 and ADR 0040 are the two durable references for the LOTR follow-up program:

| Order | ADR | Definition of correctness |
|---:|---|---|
| 1 | ADR 0039 | every source unit, decision, write, receipt, and graph artifact is complete, replayable, and verifiably consistent |
| 1a (parallel) | ADR 0040 | pre-commit ledger/plan quality is measured and explicitly labelled non-authoritative |
| 2 | ADR 0040 | every entity, type, predicate, and relation decision is coherent, grounded, governed, and measurably stable on a verified graph |

Implementation must not use semantic cleanup to hide a technical mismatch, or technical integrity
to claim semantic quality. The first ADR establishes trustworthy evidence; the second improves the
meaning expressed by that evidence.

---

## Addendum — 2026-07-28 measured CI regressions from the landed slice

The ADR 0039/0040 working tree landed on `main` as one commit (the landing commit). Two required
workflows went red on that exact SHA. Both are recorded here as measurements, not
as accepted outcomes; neither has an owner decision yet.

### 1. `search_claims` answer-partition removal regresses the deterministic floor

`src/marginalia/query.py` removed the final `_answer_ranked` partition that ran after
predicate-alias folding, on the stated grounds that it discards vector/BM25/title
agreement, floods top-k with siblings from one subject, and makes the public score
disagree with result order. That rationale is only an in-code comment; no ADR states
it, and no measurement against the eval gate accompanied it.

Measured against the committed `synthetic-ci` frozen vault at `k=10` (no daemon, no
LLM, tolerance 0):

| Metric | Baseline | Landed | Delta |
| --- | --- | --- | --- |
| hard-recall@10 hits | 15 / 32 | 14 / 32 | −1 |
| MRR@10 | 0.4348 | 0.2606 | −0.1742 |
| precision@10 | 0.2000 | 0.1909 | −0.0091 |
| extraction completeness | 0.5625 | 0.5625 | unchanged |
| claim coverage | 0.5625 | 0.5625 | unchanged |

Extraction and coverage are unchanged, so this is purely a ranking effect. Causality
is exact: reinstating only that partition on the landed commit reproduces the baseline
`metric_payload_sha256` `c3761cc556735f3179b2f025245d7ff98046e0b314c819b324cfc0ffb434655f`
byte-for-byte. Isolating the golden-harness rewrite alone (commit `8028868`, unchanged
source) is green, so the harness rewrite is not the cause.

The removal is deliberate and contracted in tests: `test_flooded_store_old_gate_
reproduces_shipped_ordering` was renamed to `test_flooded_store_default_preserves_
fused_score_order` with its assertion inverted, and `test_interrogative_results_
preserve_fused_score_order` was added to pin monotonic score order. Reinstating the
partition therefore fails both. The deterministic floor is the only CI-gated quality
metric and its tolerance is zero, so the two positions cannot both hold. This is an
owner decision between reverting the ranking change and adopting it with a measured
justification and an explicit re-baseline. It must not be closed by silently moving
the baseline.

### 2. Acceptance scenario `30_drift_synthetic` falls below the graph-size floor

The deterministic acceptance suite reports `REGRESSION` for `30_drift_synthetic`:
after ingesting the full 14-file synthetic corpus, `graph.lbug` is 126,976 bytes
against a `min=200000` floor. Every other scenario passes. The plausible owner is the
ADR 0039/0040 admission path — the closed-set write guard, predicate admission, and
relation gate all reduce what reaches the graph. Whether the smaller graph is correct
tightening or lost knowledge is unmeasured; the floor is doing exactly what it exists
to do, and the answer determines whether the fix is in the admission policy or in the
threshold.

### 3. The exact-wheel browser release smoke still pins the removed per-vault embedder selector

`release-artifact-gate` cleared its dependency audit and then failed later, in the
exact-wheel browser smoke, with `vault manager must expose one embedder selector
(0 !== 1)` at `frontend/tests/release-smoke.mjs:111`.

The cause is an intended UI change meeting an un-updated gate. `frontend/src/App.tsx`
dropped the per-vault `Embedder` `<select>` (and its `stub` option) from the
vault-create form, replacing it with "Uses the application defaults. You can override
them after selecting the vault." That is ADR 0034 §4a behaviour: embedding
configuration is now an application default rather than a vault-creation input.
`frontend/tests/release-smoke.mjs` was last touched by an earlier release commit and still drives the
old form.

The smoke is not merely stale in wording. It selected `stub` specifically so the
release smoke never downloads a real embedding model. With the selector gone, the
replacement has to preseed a deterministic embedder through the application defaults
before creating the vault, or the gate starts pulling `fastembed` weights. Choosing
that path is a release-gate design decision, so the test is left failing rather than
relaxed.

### Correction to item 2 — `30_drift_synthetic` was a measurement artefact, not lost knowledge

Item 2 above attributed the acceptance failure to the new admission path "plausibly"
reducing what reaches the graph. That attribution was written before the scenario was
measured and is **wrong**. Correcting it here rather than editing the claim, so the
mistake stays visible.

The scenario was reproduced locally. It fails on exactly the same assertion and the
same number: `graph.lbug size=126976 min=200000`, where 126,976 is precisely
31 × 4096 — a page-allocation boundary, not a content count. Inspecting the very same
vault after the run shows `graph.lbug` at 1,540,096 bytes holding 89 nodes: 45 Claims,
28 Blocks, 14 Documents, 1 Agent, 1 Activity. The corpus is 14 Markdown files and the
graph holds 14 Documents. Nothing was dropped.

The real cause is that `assert_min_bytes` measured page-allocated bytes at the instant
ingest returned, and ADR 0039's deferred-checkpoint work moved when those bytes land in
the main file. The assertion was a proxy for "the graph got populated" and the proxy
broke while the property it stood for held.

The fix replaces the proxy with the property. The scenario now reads
`/api/v1/graph/stats` and asserts total node count and that every corpus file produced a
`Document`, via a new `assert_min_count` helper in the acceptance library. On the same
tree the scenario passes with 10 assertions (up from 8), logging
`total_nodes=87 documents=14 (corpus=14 files)`. This is strictly stronger than the byte
floor it replaces and cannot drift with storage layout or checkpoint timing.

No admission-path change is implicated. Items 1 and 3 stand as written.

### Status of the three items as of 2026-07-28

- **Item 1 — `search_claims` answer-partition removal: CLOSED by owner decision
  (2026-07-28), reinstated.** It was the only remaining red workflow on `main`. It was
  not resolvable mechanically because the removal is contracted in two deliberately
  rewritten tests, so reverting it and keeping the zero-tolerance floor were mutually
  exclusive positions. The measurement and the byte-identical restore proof above were
  the input to that decision. See the addendum "Owner decision — reinstate the
  `search_claims` answer partition" below for the resolution, the per-question A/B, and
  the one open question it leaves.
- **Item 2 — `30_drift_synthetic`: CLOSED.** Was a byte-size proxy breaking on
  checkpoint timing, not lost knowledge. The scenario asserts graph content now and
  `model-free-tests` is green.
- **Item 3 — exact-wheel browser smoke: CLOSED.** The correction above said it was left
  failing; that is no longer true. It turned out to be six stale UI/API expectations,
  each repaired against the current contract rather than relaxed, and the smoke gained
  an embedder-inheritance assertion, an explicit batch-vector-count assertion, and a
  guard that a destructive re-embed cannot be reached in one unconfirmed click.
  `release-artifact-gate` is green.

### Addendum — 2026-07-28 follow-up fixes on the landed slice

Three narrow corrections landed after the items above, each verified against the live
code rather than the doc:

- **T9 reaches the UI.** `frontend/src/components/ingest/CurationProgress.tsx` clamped
  every curator bar with `Math.min(100, …)`, which is precisely the reassurance T9
  forbids: the server publishes an unbounded `fraction` plus an explicit
  `progress_integrity_error` record when `done > total`, and the UI erased it. Both
  clamps are gone. `LedgerProgressRow` now carries `population`,
  `population_revision`, and the optional error record, and a row in that state renders
  as an error (code, real `done`/`total`, overflow, unbounded fraction) instead of a
  full bar. The empty-total early return no longer hides an integrity error either,
  since `done > total == 0` is exactly the state worth surfacing. Caveat worth
  recording: `frontend/` has no component-test harness (only the Playwright
  release smoke, which never renders `CurationProgress`), so nothing in CI fails
  if the clamp is reintroduced. The removal is guarded by review only.
- **The `answers` probe was dead code and is removed.** `resolve.find_answers` probed
  `store.search_text(..., type="Question")`, but `Question` is outside the closed
  schema (`store/closed_set.py`), both stores refuse it in `add_node`, and no writer in
  `src/marginalia` has ever minted one — so the probe returned `()` unconditionally
  while costing one `search_text` per candidate, and `_is_open_question` was
  unreachable. `find_answers`, `_is_open_question`, `_CLOSED_STATUSES`,
  `ANSWERS_THRESHOLD`, the `answers_threshold` parameter, and the two strict-xfail
  tests that pinned the dead behaviour are deleted. `resolve()` keeps its remaining
  behaviour exactly: a candidate could only ever gain an `answers` correlation from a
  `Question` node no store can hold, so no existing vault loses a correlation.
  `resolve` is now two probes, similar and contradicts.
- **Type regressions from the heal/queue fixes are closed.** `reconcile/heal.py`
  narrows the staging embedding width the way `kg rebuild` does instead of passing
  `int | None` into an `int` audit parameter; `server/_ingest_queue.py` fails loudly on
  a vault-less history path, proves `lease_vault()` returned a context manager before
  entering it, types the companion factory through a `SupportsRemember` protocol, and
  narrows the reconciliation payload; `store/schema.py` narrows the driver cursor
  behind a runtime-checkable protocol and the untyped stored schema version. Pyright on
  the four files drops from 13 errors to 1. The remaining one — passing a `ServerState`
  where `require_write_allowed` declares `VaultRuntime` — is left open on purpose: both
  runtime shapes really are passed there, so the honest fix widens `_integrity`'s own
  contract and that is a behaviour-bearing decision, not a typing cleanup.

### Addendum — 2026-07-28 owner decision: reinstate the `search_claims` answer partition

Item 1 above is resolved. The owner's decision, taken on the A/B below rather than on the
in-code rationale, is to **reinstate the final `_answer_ranked` partition and not move the
baseline**.

The block removed in the landing commit is restored verbatim at its original site in
`src/marginalia/query.py` — after equivalence and predicate-alias folding, before the
`seed_diversity` branch. Only that block is restored; the `metrics` instrumentation added
in the same commit (`query_embedding_*`, `deterministic_retrieval_latency_ms`, the
`final_results` binding) is untouched.

**A/B method.** Two isolated `git worktree` checkouts with separate
`UV_PROJECT_ENVIRONMENT`s, both reading the same committed frozen vault
(`tests/golden/datasets/synthetic-ci/frozen-vault`). Arm A = partition reinstated,
Arm B = `main` as landed. No daemon, no LLM, no rebuild: this is a retrieval-time-only
change. The gate is the eval-gate floor command at `k=10`, tolerance 0.

| Metric | Arm A (reinstated) | Arm B (removed) | Delta A−B |
| --- | --- | --- | --- |
| hard-recall@10 hits | **15 / 32** (0.4688) | 14 / 32 (0.4375) | +1 |
| MRR@10 | **0.4348** (RR sum 9.5667) | 0.2606 (RR sum 5.7333) | **+0.1742** |
| precision@10 | **0.2000** | 0.1909 | +0.0091 |
| extraction completeness | 0.5625 | 0.5625 | unchanged |
| claim coverage | 0.5625 | 0.5625 | unchanged |
| gate verdict | **PASS** (`regressions: []`) | FAIL (3 regressions) | — |

Arm A reproduces the committed baseline `metric_payload_sha256`
`c3761cc556735f3179b2f025245d7ff98046e0b314c819b324cfc0ffb434655f` **byte-for-byte**, and
does so at the current `main` tip, not only at the landing commit. The capacity commits that landed
after the measurement did not move ranking; this single block is the whole delta.

**The win is broad, not one lucky question.** Over the 25-question dataset (22 answerable,
32 gold targets), per-question reciprocal rank splits **8 better, 1 worse, 16 identical**.
Arm A puts the gold file at rank 1 for `er_dev_is_devran`, `er_priya_rao_anand`,
`mh_calibrator_reports_to`, `mh_schema_authors_roles` (which misses top-10 entirely in
Arm B) and `sl_headquarters`. The single regression is `pit_director_2022` (0.200 vs
0.333). Controls are unchanged: all three negative controls (`nc_bodl_v3`,
`nc_membership_fee`, `nc_third_director`) return 10 hits at precision 0.0 in **both** arms,
so the partition does not make an absent-answer trap start retrieving.

**Cost.** Exactly two tests, both of which encoded the unmeasured removal rationale, and
nothing else. Targeted retrieval modules were 2 failed / 54 passed in Arm A against
56 passed in Arm B; no third test depends on the removed order. Both are restored to
assert the shipped ordering:

- `tests/test_seed_diversity.py::test_flooded_store_default_preserves_fused_score_order`
  is renamed back to `test_flooded_store_old_gate_reproduces_shipped_ordering` with its
  inverted assertion reverted.
- `tests/test_search_claims.py::test_interrogative_results_preserve_fused_score_order`,
  which pinned monotonic fused-score order, is replaced by
  `test_interrogative_answer_partition_outranks_higher_fused_score`. That replacement is a
  **positive pin that the partition IS applied**, so a future silent removal fails CI: a
  zero-coverage Claim that wins the vector leg is partitioned last despite not carrying
  the lowest fused score, an equal-coverage sibling with a strictly lower fused score
  outranks a higher-scored one, and the public order is asserted to be non-monotonic in
  fused score. Removing the block again was verified to fail both tests.

**Open question — the ask/explore seed path remains unmeasured.** The reinstated block
sorts `results` **before** the `seed_diversity` branch, so it also changes the input
ordering into `_diversify_seeds` — that is the ask/explore seed path, not only default
recall. The deterministic floor exercises the default `/recall` path only, and no live
semantic run was performed for either arm, so ask/explore ordering under the reinstated
code is **unmeasured**. The removal's stated rationale was specifically about seed
flooding, and the pre-removal comment asserted that the diversified cut "subsumes its
flooding damage on the ask/explore seed path only" — a claim neither arm tested. **Any
future removal of this partition requires a measured seed-path test first**, not an
in-code rationale.

Two further caveats the decision is taken in full knowledge of: `synthetic-ci` is the only
dataset with a frozen vault, so this A/B is single-dataset and generalisation beyond it is
unmeasured; and no semantic-judge score was produced for either arm. The residual concern
in the original removal comment — that the public score disagrees with result order — is a
presentation problem, and addressing it (by exposing the partition rank, or suppressing
the score in ordered output) is a separate, currently unmeasured change.

## Addendum — 2026-07-28: `feat/extraction-granularity` land-or-retire A/B (LAND)

The parked branch `feat/extraction-granularity` (single commit, originally B0,
2026-07-06) was adjudicated by a measured A/B on the extraction-sensitive Tier 1 quality
gate. This addendum records the decision, the numbers behind it, and the commit lineage.

**Decision: LAND.** Rebased onto the then-current `main` (L) as B2 and fast-forwarded into `main`.
Commit labels replace private-history SHAs. Lineage: B0 (original) → B1 (first rebase, onto
M1, the tree the A/B was measured on) → B2 (final rebase onto current
`main`; the intervening commits were docs-only and left `src/` and the gate datasets
byte-identical).

### What the commit changes

Four separable pieces, all ported; none were subsumed by the union-k2 / composite-literal
work already on `main`:

1. **SCALAR SWEEP** rule in `_BASE_SYSTEM` — a final completeness re-scan requiring every
   number-with-unit, port, version tag, date, path, filename, URL and `key: value` to
   appear verbatim in exactly one claim literal. Applied without conflict; `main`'s
   composite-literal rule constrains *grouping*, not *coverage*, so the two are
   complementary rather than duplicative.
2. **VERBATIM-STRING literals** rule plus **Example 17** — an exact technical string must
   survive entity extraction character-exact in the claim literal, alongside the topology
   edge. `main` had grown to Example 16, so the new example appended cleanly at 17.
3. **Tolerant claim-subject binding** (`_normalize_title_key`) — casefold /
   whitespace-collapsed fallback lookup so a re-spelled subject does not silently discard
   the claim. Exact matches still win.
4. **Silent-drop counters** — `claims_dropped_no_subject` / `claims_dropped_metadata` on
   `ExtractionResult`.

Two conflicts were resolved during the first rebase. In `_BASE_SYSTEM`, `main`'s
privacy-scrubbed Example 16 was kept and Example 17 appended after it (the branch carried
the pre-scrub Example 16 text). In `ExtractionResult`, `main`'s newer `parse_failed` field
and the branch's two drop counters were both kept. `uv.lock` took `main`'s side.
`tests/extract` passes 127 passed / 1 skipped on the rebased tree.

### A/B method

Three `./tests/golden/bin/quality-gate.sh` runs per arm against the `semantic-adversarial`
dataset (live ingest of 6 docs, 13 asks), model `desktop/qwen3.6-35b-10-parallel` via the
`<private-lan-gateway>` LiteLLM gateway. Arms were isolated by checkout: each ran from its
own `.venv`, verified to import its own `src/marginalia/extract/__init__.py`.
`quality_gate_baseline.json` and `uv.lock` were confirmed byte-identical across arms, and
neither arm was run with `--mint-baseline`. Counts, not rates, are compared — the gate
gates on counts.

### Per-run gated metrics

| Arm | Run | source_sha | hard_recall | extraction_completeness | claim_coverage | must_contain | negative_control | citation verify | Tier 2 |
|-----|-----|-----------|------------:|------------------------:|---------------:|-------------:|-----------------:|----------------:|--------|
| main | 1 | M1 | 11/11 | 11/11 | 11/11 | 11/11 | 2/2 | 130/130 | 13 CORRECT |
| main | 2 | M1 | 11/11 | 11/11 | 11/11 | 10/11 | 2/2 | 130/130 | 13 CORRECT |
| main | 3 | M2 | 9/11 | 9/11 | 9/11 | 10/11 | 0/2 | 130/130 | 12 CORRECT, 1 PARTIAL |
| branch | 1 | B1 | 11/11 | 9/11 | 9/11 | 11/11 | 2/2 | 130/130 | 13 CORRECT |
| branch | 2 | B1 | 11/11 | 11/11 | 11/11 | 10/11 | 2/2 | 130/130 | 13 CORRECT |
| branch | 3 | B1 | 11/11 | 10/11 | 10/11 | 10/11 | 2/2 | 130/130 | 13 CORRECT |

`main` run 3 ran at M2 rather than M1 because a concurrent agent landed two
docs-only commits mid-A/B; the diff from M1 to L over `src/` and `tests/golden/datasets/` is
empty, so the tree under measurement was unchanged.

### Floors and means (counts, n=3)

| Metric | main floor | main mean | branch floor | branch mean | verdict |
|--------|-----------:|----------:|-------------:|------------:|---------|
| hard_recall@k | 9 | 10.33 | **11** | **11.00** | improved (floor and mean) |
| extraction_completeness | 9 | 10.33 | 9 | 10.00 | floor equal, mean −0.33 |
| claim_coverage | 9 | 10.33 | 9 | 10.00 | floor equal, mean −0.33 |
| must_contain | 10 | 10.33 | 10 | 10.33 | equal |
| negative_control | 0 | 1.33 | **2** | **2.00** | improved, no degradation |
| citation byte-verification | 130 | 130.00 | 130 | 130.00 | equal |

Tier 2 (advisory, non-gating): `main` 13/13/12 CORRECT with one PARTIAL; branch 13/13/13
CORRECT, zero INCORRECT/UNSUPPORTED/unparseable in either arm.

### Applying the owner-delegated rule

RETIRE if any gated floor is below `main`'s, or if negative-control abstention degrades at
all; LAND if every floor is ≥ `main`'s and at least one floor or mean improves; RETIRE on
exact equality everywhere. No branch floor is below `main`'s, negative-control abstention
improves rather than degrades (floor 0 → 2, mean 1.33 → 2.00), and `hard_recall@k` improves
on both floor (9 → 11) and mean (10.33 → 11.00). The result is not an exact tie. **LAND.**

### Caveats recorded with the decision

- **n=3 per arm is underpowered.** The gate's own baseline notes that 7/11 and 11/11 gold
  targets were both observed on an unchanged tree. Three of `main`'s six floors are set by
  a single run (run 3); without it `main`'s floors would be 11/11/11/10/2/130 and the rule
  would have returned RETIRE on `extraction_completeness` and `claim_coverage`. The rule
  was applied as written rather than relitigated, but the verdict should be read as "no
  measured regression, with a real improvement on two metrics", not as a strong effect.
- **`main` run 3 was verified clean, not degenerate**: 13/13 questions answered, no empty
  answers, no server errors, all 6 documents ingested. Its `negative_control` 0/2 comes
  from the deterministic lexical `abstains()` detector missing a correctly-abstaining
  phrasing ("Nobody. The notes state that the presence of The Lantern manual does not mean
  Maren authored it."), which the Tier 2 semantic judge scored as correct. That is genuine
  variance in the gated metric as defined, and it can strike either arm.
- The two mean regressions on `extraction_completeness` and `claim_coverage` (10.33 → 10.00,
  a single gold target on a single run) are within the documented variance band and are not
  RETIRE triggers under the rule, but they are the reason this addendum does not claim an
  extraction-completeness win.

## Addendum — 2026-07-29: golden judge rubric partial-credit calibration (n=14)

The golden `judge.py` rubric gained explicit partial-credit rules after an owner-vs-judge
calibration on the retained CoP run of 2026-07-29 (14 items, judge
`desktop/qwen3.6-35b-10-parallel`, temperature 0, thinking OFF). The v1 rubric agreed with
the owner on 11/14 items, Cohen's kappa **0.3226**; every disagreement was the judge being
*harsher* than the owner, grading hedges and omitted colour as severely as fabrication.

Three owner rulings were encoded as general clauses (no item id, question text, or answer
content from the calibration set appears in the prompt):

1. A key fact that is correctly stated but wrapped in a hedge or disclaimer — including a
   fact quoted inside a code block while the prose denies having it — is capped at
   `partial`, never `wrong`.
2. Naming an entity or aspect that appears in the expected answer or the retrieved nodes,
   but slotting it into the wrong role or answering a different facet, is a wrong-slot
   fill: `partial`, not `wrong`. `contradicts` is now defined as asserting something the
   expected answer states to be false.
3. Omitting a *secondary* descriptive attribute (role, title, sponsor, affiliation) of an
   entity the answer already identifies correctly does not lower the verdict. A fact is
   *key* only when it is the thing the question asks for.

**Fabrication strictness was verified adversarially, not asserted.** The concession in
rule 3 covers omission only; the rubric states explicitly that inventing a secondary
attribute is still `wrong`. Five answers that both rubrics grade favourably were mutated
with invented names, bodies, venues, dates and participant counts, then re-judged under
both rubrics:

| probe row | v1 | v2 |
| --- | --- | --- |
| `t4_cloud_benchmark` (the only `negative: true` row) | wrong | wrong |
| `t1_sota_score` (invented awarding body + reviewer) | wrong | wrong |
| `t2_maturity_jump` (invented steering board + officer) | correct | **wrong** |
| `t1_vuln_rate` (invented audit team + venue) | partial | **correct** |
| `t1_metr_slower` (invented lab, cohort size, funder) | wrong | **correct** |

The negative control is unchanged, which is the row that matters most. The other four show
that *neither* rubric reliably rejects invented colour wrapped around a correctly stated
key fact, and that the v1→v2 movement is **bidirectional** (one row got stricter, two got
more lenient) rather than a uniform loosening. This is recorded as a known limitation of
the judge rather than tuned away: catching embellishment is a weakness of the scripted
judge on both sides of this change, not a property introduced by the partial-credit rules.
A dedicated fabrication-detection clause is out of scope for this calibration.

An earlier draft of the rubric did introduce a genuine one-way regression
(`t1_sota_score` wrong → partial); the fabrication carve-out in rule 3 was added to close
it, and that carve-out changed **zero** of the 14 real verdicts.

Result on the same owner sheet: agreement 13/14, kappa **0.72**. The single remaining
disagreement is `t3_shared_obstacle`, where the judge sees only the expected answer and the
retrieved node names and cannot confirm that the system's alternative obstacle is
source-supported.

**Overfitting caveat.** This rubric was calibrated against n=14 owner labels and those three
rulings. Kappa 0.72 is an in-sample fit statistic on the calibration set and carries no
generalization claim; the rubric earns out-of-sample evidence only from a future unseen
judged run.

## Addendum — 2026-07-29: golden judge rubric v3 — structured verdict, excerpt injection, and a measured kappa regression (session S4)

Rubric v3 replaced the single scalar `verdict` field with two independently-judged fields
composed in Python, per `plan-5-judge-fabrication.md`:

- `key_facts ∈ {conveyed, hedged, partial, missing, contradicted}` — coverage of the
  question's key fact(s).
- `fabrication: bool` + `fabricated_items: [...]` — whether the system stated an added
  name, body, date, venue, figure, or citation absent from both the expected answer and
  the (now-injected) source excerpts.

`judge.py::compose_verdict(key_facts, fabrication, *, negative)` is a pure, unit-tested
function that turns those two fields into the legacy scalar; the model is never asked to
resolve the hedge-vs-fabrication precedence itself.

**Owner ruling (Q1) changed the plan's truth table.** Fabrication now CAPS the verdict at
`partial` — a fabricated-detail answer can never be `correct`, but fabrication alone never
auto-downgrades to `wrong`. The one path that still reaches `wrong` through
fabrication-adjacent behavior is `key_facts="contradicted"`: a substantive, specific answer
to a NEGATIVE/ABSENT question directly contradicts the expected answer's assertion of
absence (ground truth says "not present", the answer says "present, and here are the
details" — those cannot both be true), so that case is routed through contradiction, not
through the fabrication flag, and is unaffected by the Q1 cap.

**Excerpt injection (owner Q2: 1200-byte cap).** `cmd_judge` now accepts `--inputs` and
builds a bounded `SOURCE EXCERPTS` block from the top-5 recalled hits' `provenance`
(`build_excerpt_block`), resolved through `_strict_input_source` (fails loud on a basename
collision), deduped by `block_id` before the byte cap so hits sharing one block cost one
excerpt. This gives the fabrication rule the evidence its prompt references instead of
asking the model to test membership against bare node names it was never shown.
`run-golden.sh` now threads `--inputs "$DATASET_DIR/inputs"` through automatically.

**§3D fixture (owner Q4/Q5).** `tests/golden/bin/test_judge_fabrication.py` is a NEW,
separately-labelled baseline: 5 synthesized fictional structural analogues mirroring the
prior embellishment table's shapes one-for-one (negative+fabricated, invented awarding
body, invented governance body, invented audit venue, invented lab/cohort/funder), plus 4
hold-the-line rows (hedged-in-code-block, wrong-slot-fill, secondary-omission-still-correct,
plain-correct). It does not reproduce the historical 5-row table above, whose source texts
were never retained. Layer 1 (pure-Python `compose_verdict` truth table + parser shapes) is
CI-safe and green — 22 tests. Layer 2 (`-m slow`, live) is laptop-only by construction.

### Layer-3 result: re-judging the retained CoP run, and what it found

The retained CoP run's `responses.jsonl` (2026-07-29) was re-judged end-to-end under rubric v3 with
`--inputs` against the CoP dataset, model `qwen3.6-35b-16k-batch` (the same model family as
the original run), temperature 0, thinking OFF. Sidecar `judge-v3.json` is retained
alongside `judge-v1-control.json` and `judge-v2.json` (never committed to this repo).

| item | owner label | v1 | v2 | v3 |
| --- | --- | --- | --- | --- |
| `t3_shared_obstacle` | PARTIAL | wrong | wrong | wrong |
| `t4_test_runner_message` | PARTIAL | wrong | **partial** | **wrong** |
| (other 12 items) | CORRECT | correct | correct | correct |

Kappa against the 14-row owner sheet (`adr0040-kappa-kit/labeling-sheet.OWNER-FILLED.immutable.csv`,
reusing the existing `build_cop_sheet_v2.py` / `kappa.py` tooling unchanged): **0.4615**
("moderate"), against v2's 0.72 — **this fails the plan's Layer-3 non-regression criterion
(kappa ≥ 0.72)**. It is not reported as a pass.

**Root cause, verified interactively, not assumed.** `t4_test_runner_message`'s system
answer states in prose that the exact error message is not provided, then quotes it
verbatim inside a code block — textbook "hedged" per the rubric's own hedge rule, and both
the owner and v2 graded it `partial`. Isolated single-question probes against the live
judge show the model DOES classify it `key_facts="hedged"` when given only the
question/expected/system-answer triple; adding the `SOURCE EXCERPTS` block (which, for this
question's top-5 recalled hits, does not happen to contain the file with the exact error
text within the 1200-byte cap) flips the same model's classification to
`key_facts="contradicted"` — every time, across three rubric wordings tried, including one
written specifically to say "never let source-excerpt absence override a hedge/conveyed
reading already settled by the SYSTEM ANSWER and EXPECTED ANSWER". The bleed is between the
`SOURCE EXCERPTS` block and the hedge-vs-contradiction judgment, not between fabrication and
`key_facts` as the plan's §7 risk anticipated, but it is the same class of risk the plan
named: *"excerpts make the judge lenient/strict by over-supplying/under-supplying context"*.

**This is exactly the plan's own stated escalation trigger.** Plan §3C: "Adopt two-pass
only if structured single-call verdicts show measurable bleed (e.g. `fabrication` flipping
with correctness on rows where the invented content is unchanged)." This session measured
bleed — `key_facts` flipping between two rubric-clarification attempts on an unchanged
row, driven purely by whether the excerpt block is present — where the plan's own quoted
example anticipated it in the fabrication field instead of `key_facts`, but the underlying
failure mode (excerpt presence perturbing a judgment it shouldn't touch) is the one named.

**Disposition, recorded rather than smoothed over:**

- Rubric v3's `compose_verdict` composition and its unit tests are correct and shipped —
  the Q1 truth table, both `negative` branches, and the parser back-compat path all hold.
- Excerpt injection (3B) is shipped and unit-tested, and is a net win for the fabrication
  check specifically (the 4 non-negative fabrication analogues correctly land on `partial`
  with non-empty `fabricated_items` in the live fixture run) — the regression is a
  side-effect on the unrelated hedge/contradiction judgment, not evidence 3B itself is
  wrong to ship.
- The kappa non-regression gate is **NOT met** on this run. Task #5 (judge
  fabrication-detection clause) stays open, now with a two-pass (3C) escalation path
  backed by measured evidence rather than the plan's speculative framing.
- Immediate mitigating option for a future session, not applied here: gate excerpt
  injection on the fabrication judgment only by giving the model a THIRD explicit
  instruction to answer `key_facts` before it is shown SOURCE EXCERPTS at all (two-pass,
  or a two-message conversation) — i.e. 3C, exactly as the plan anticipated deferring to.

## Addendum — 2026-07-29: the CoP judged run, owner adjudication, and what it does *not* unlock

This records the first real judged run behind an ADR 0040 corpus packet, the owner's
adjudication of it, and — importantly — the acceptance slots that remain unfilled
despite it. The judge-rubric side of the same event is in the preceding addendum; this
one is the run and the adjudication scope.

### The run

The CoP corpus, in the run of 2026-07-29, an owner-owned real external
private corpus. 17 inputs ingested with 0 errors, 14 questions at `k=10`, wall time
**38.1 min**. Verified graph generation `3d550128-0b6b-4c51-8496-082f3fd7ac5e`,
integrity `verified` (freshness `fresh`, adjacency complete, 0 issues), provenance gate
pass with 0 failures. Judge `desktop/qwen3.6-35b-10-parallel`, thinking OFF,
`judge_skipped: false` — rubric-v1 tally **11 correct · 1 partial · 2 wrong**, with 0
missed, 0 unsupported, 0 unparseable.

This is the first ADR 0040 packet not derived from synthetic or smoke traffic. The other
three corpora (`semantic-adversarial`, `temporal-chat`, `hobbit-glm52`) were judged with
`judge_skipped: true` and carry no judged-answer evidence.

### The owner adjudication, and its exact scope

On 2026-07-29 the owner adjudicated the 14 judged answers, labelling each and recording a
verbatim note per row. Owner tally: **12 CORRECT · 2 PARTIAL · 0 INCORRECT**.

Against rubric-v1 that is 3 disagreements, kappa **0.3226** ("fair"), and the judge was
**stricter than the owner in all three** — there is no row where the judge was more
lenient. Under the recalibrated rubric-v2 one disagreement remains, kappa **0.72**
("substantial"), still stricter-only. Both kappa reports are now durable artifacts rather
than a number in a commit message.

**This is question-level answer-quality adjudication and nothing more.** It does not
satisfy either human-review slot in the acceptance contract:

- `adjudicated_report` takes a `semantic_adjudication.v1` document whose `entities` and
  `relations` lists must both be non-empty and whose every row is keyed by a
  `candidate_id` the validator binds against the corpus candidate ledger. The schema has
  no partial state. 14 question-level verdicts cannot produce one legal row without
  inventing identifiers, so the slot stays **missing** — deliberately, because pinning a
  non-conforming document would move it to `invalid`, which is strictly worse.
- `review_coverage` takes a `semantic_review_coverage.v1` document requiring
  `manifest_sha256` — this run emitted no `run-manifest.json`, and no other digest is a
  legal substitute — plus owner-reviewed `primitive_samples` and
  `predicate_state_samples`, which were not produced.

Acceptance collection slot counts are therefore **unchanged**: 34 ready · 33 missing · 0
invalid, before and after. No new dated collection file was minted, because no slot
legitimately changed. The corpus packet's §3 is now signed with an explicitly scoped
sign-off naming §3 only; §4 and §5 stay marked PENDING and the packet remains unpinnable.

Named unblocking steps, so "pending" is actionable rather than a refusal:

- `adjudicated_report` needs a candidate-ledger export from the corpus vault at
  generation `3d550128-0b6b-4c51-8496-082f3fd7ac5e`.
- `review_coverage` needs a re-run that emits a run manifest, plus owner-reviewed
  primitive and predicate-state sample sets.

### Quality signals from the run — named open follow-ups

The run passed its hard invariants but surfaced three signals that are recorded here as
open follow-ups, not as resolved findings.

1. **Five node types came back empty.** Stored counts were `Activity` 0, `Place` 0,
   `Identifier` 0, `Annotation` 0, `Finding` 0, against `Agent` 17,
   `InformationObject` 33, `Concept` 315, `Document` 17, `Claim` 958, `Block` 31. An
   organizational corpus of meetings and case studies that yields zero `Activity` and
   zero `Finding` nodes is an extraction-coverage signal, not a property of the corpus.
   `closed_primitive_set` passing means nothing *outside* the closed set was written; it
   says nothing about types never populated.
2. **Two extractions hit the 32k completion ceiling.** Both logged
   `finish_reason=length` at `completion_tokens=32768`; one escalated to the enumerate
   pipeline, and truncated JSON risks yielding zero candidates from the affected block.
   This is an unbounded-output hazard in extraction, independent of chunk size.
3. **Byte-level provenance is far looser than token-level recall.** Gold-span summary
   over 13 questions with gold spans: `mean_token_recall` **0.8449** but `mean_byte_IoU`
   **0.0266**, with 13/13 anchors OK and 10 spans intact. Retrieval finds the right
   content while the returned byte ranges overlap the gold spans only marginally — the
   citation is directionally right and byte-imprecise. `exact_byte_provenance` is still
   `not_measured`, so this is currently the only quantitative read on it.

None of these three block the ADR; all three are quality debt that a future run should
move rather than re-observe.

## Addendum — 2026-07-29: Activity prompt rule (session S5), and Step 0 re-collection

Follow-up #1 from the prior addendum ("Five node types came back empty" — `Activity` 0) is
addressed here at the propose side, no new ADR required per owner ruling.

### A1 — the extractor never proposed a single Activity

Replaying the CoP judged run's own LLM trace (1817 captured calls,
the `llm_trace/*.json` files in that run's private results directory) and counting `"type"`
values in the 45 extraction calls' `actual_response_content` gave `Counter({'Concept': 1952,
'InformationObject': 47, 'Agent': 43, 'Activity': 0, 'Place': 0})`. Zero proposals, not zero
survivors: `closed_primitive_set` passed with count 0, and the extraction request schema
(`llm_trace` seq 1152) permits `Activity` and `Place` at the enum level, so curation, the
relation gate, and the closed-set guard are ruled out. The corpus does contain bounded events
(a dated kickoff, a named recurring workshop) that match the extractor's own Activity
description. `Place = 0` is recorded as a **PASS** — this corpus has no named locations, so the
absence is corpus-correct, not a defect, and nothing was changed for it.

Root cause: `_BASE_SYSTEM` (`src/marginalia/extract/__init__.py`) gave `Activity` exactly one
descriptive line and then spent roughly forty paragraphs of completeness rules and worked
examples steering candidates toward `Concept` (named physical artifacts, eponymous
methods/frameworks, taxonomy chains, table/list rows). There was no completeness rule and no
worked example for `Activity` anywhere in the prompt — the observed distribution is exactly the
shape that asymmetry implies.

**Fix landed:** one new completeness rule in `_BASE_SYSTEM`, placed immediately after the
`Place` line in the type list, symmetric in structure with the existing Concept completeness
rules: a dated or named meeting, kickoff, workshop, review, launch, sprint, retro, or run with
temporal extent is an `Activity`, not a `Concept`, even when it also names a topic it covers;
bind participants with directional edges. One worked example is included in the same style as
the file's other worked examples. Kept short and additive-only (appended, not restructured) to
protect the prompt's prompt-cache-stable prefix.

**Measurement is explicitly deferred, not skipped.** A1 cannot be measured cleanly while the
transport-layer sampler drop (max_tokens/temperature silently discarded on the gateway path)
was still live, because temperature confounds Activity/Concept type choice; that transport fix
landed separately in session S2 before this prompt change. The re-run that proves
`Activity > 0` on this same CoP corpus, with minted titles traceable to the dated-kickoff and
team-coaching source files, is owned by session S6's manifest-emitting `eval-run.sh --endpoint`
hinge run — not run here. Until that run lands, treat this as a landed propose-side fix awaiting
its own closing measurement, not as a verified-closed finding.

### Step 0 — re-derive stale CoP/hobbit pinned artifacts, then re-collect (no drop below 34)

Resolving every pinned path in the external `acceptance-collection.json`
(`~/.marginalia/quality-runs/adr0040/`) against the filesystem showed three CoP corpus slots
(`manifest`, `stored_report`, `snapshot:baseline`) and one `hobbit-glm52` slot
(`manifest`) pointing at `tests/golden/results/**` paths that no longer exist in the working
tree; a re-homed `hobbit-glm52` snapshot also carried a stale `member_encoding` label
(`"sha256(kind + NUL + value)"`) that predates the current `SEMANTIC_MEMBER_ENCODING` constant
in `semantic_quality.py` (a descriptive-string drift only — the underlying member hashes were
untouched). A naive re-collect today would have dropped ready 34 → ~33 or lower.

Per explicit owner ruling (overriding this task's own plan's default of accepting that drop),
the stale artifacts were re-derived first, so the re-collected count never goes below 34:

- The CoP corpus's `run-manifest.json` and `hobbit-glm52/run-manifest.json` were emitted
  deterministically via `judge.py manifest <dataset dir> --questions questions.yaml` (no live
  daemon, no model calls) against each corpus's retained dataset directory.
- The CoP `stored_report`/`snapshot:baseline` slot was re-pointed at the retained
  `semantic-quality.json` in that run's private results directory (the same run
  this addendum's A1 evidence and the prior addendum's owner-adjudication evidence come from).
- The hobbit `stored_report`/`snapshot:baseline` slot kept its existing re-homed file with only
  the stale `member_encoding` label corrected to the current constant string.

Fresh `uv run marginalia quality acceptance-status` against the updated collection:
`{'invalid': 0, 'missing': 33, 'ready': 34, 'total': 67}` — unchanged in total count from the
prior addendum, but now backed by artifacts that resolve on disk instead of stale absolute
paths. Per MASTER-PLAN §4, **37 is not reachable on this path and was not booked.**
`review_coverage` stays `missing` for every corpus, including CoP — no zero-node "reviewed,
correctly absent" primitive row was fabricated to fill it, and it remains blocked pending a
post-A1-fix run showing `Activity > 0` for CoP (session S6's hinge run carries that proof).
Working artifacts and the full re-derivation log live under
`~/.marginalia/quality-runs/adr0040/` (external, laptop-only; never committed to this repo).

## Addendum — 2026-09-14: work-item type stability + assignee-is-separate-entity prompt rule

### Symptom (private-corpus diagnostic, not reproduced in this repo)

A private 76-file software/finance vault (external, laptop-only; never committed) produced 94
nodes whose titles matched an identifier-shaped work-item pattern (`TASK-n`, `US-n`), typed
inconsistently: 54 Concept, 35 Activity, 5 Agent. A handful of bare codes with no trailing title
were typed Agent while the same codes carrying a descriptive trailing title were typed Activity.

### Root cause

Confirmed by direct investigation, not inferred:

- No code validated a candidate's type against its title/content. The four `title`-only
  structural filters in `extract/__init__.py` — `_is_structural_noise`, `_is_filename_noise`,
  `_is_low_value_title`, `_is_generic_token_noise` — never consult the emitted `ntype`, so a
  wrongly-typed candidate rides straight through.
- `_BASE_SYSTEM_TEXT`'s Agent definition — "a NAMED person, character, animate actor, group,
  organization, team, or system that can act or bear responsibility" — did not exclude an
  identifier-shaped work-item code from matching "system."
- The prompt never distinguished a document's own identity from a person/system named as its
  assignee/owner: grepping the whole prompt for `assignee|ownership|owner|responsible` returned
  one unrelated hit. Source docs in the corpus commonly carry an `**Assignee**: <name>` line
  directly under a `TASK-n` heading, which plausibly drove the Agent misclassification.
- No later stage can self-correct it: type adjudication (Q3/D3 above) buckets on
  `discovery_surface_key`, so a bare `task-15` and a fuller `task-15: <title>` never share a
  bucket and are never compared against each other; `candidate_id` hashes the type, so there is
  no post-commit re-type path; `kg reconcile` clusters one type at a time.

### Owner's decision and fix landed (prompt-only, no deterministic coercion)

Per explicit owner ruling, this is fixed at the propose side only — a previous deterministic
title-coercion regex (`_PHYSICAL_ARTIFACT_RE`) had caused surprises, so no equivalent
work-item-code coercion was added. Two additions to `_BASE_SYSTEM_TEXT` in
`src/marginalia/extract/__init__.py`:

1. A new "Completeness for work items" paragraph, placed immediately after the existing
   "Completeness for named events" paragraph — i.e. directly following the five primitive-type
   bullets, ahead of the roughly forty paragraphs of Concept-steering completeness rules (the
   same positional lesson as the session-S5 Activity-starvation fix above: a rule competing with
   that much downstream material needs to sit early). It types any ticket/issue/story/task
   identified by an identifier-shaped code as an Activity — never a Concept, never an Agent — and
   explicitly distinguishes this from (a) the generic taxonomy category words
   `Epic`/`Story`/`Task` naming a decomposition level, which stay Concepts, and (b) a bare
   priority/req code with no described work behind it, which is still structural noise. The same
   paragraph states that a person or system named as the item's assignee/owner/reporter/reviewer
   is a SEPARATE entity that never decides the item's type, and should be extracted as its own
   Agent linked by a directional edge such as `assigned_to` when it is a real named actor.
2. The Agent bullet itself gained a negative clause: an identifier-shaped work-item/ticket code is
   explicitly excluded, even when the document names someone as that code's assignee — while
   keeping the positive "system that can act" case intact (a genuinely named service/bot, e.g.
   "Azure OpenAI", is still an Agent).

Deviation from the S5 convention: S5 kept its change "additive-only (appended, not restructured)"
to protect the prompt-cache-stable prefix. This fix instead inserts inline at the
primitive-definitions region — a one-time prefix-cache invalidation on next deploy, not an
ongoing cost — because the failure mode is positional: a rule appended at the end of an
already-long Concept-steering prompt would face the same drowning-out risk the S5 addendum
diagnosed for Activity itself.

A model-free regression test (`tests/extract/test_work_item_typing_prompt.py`) pins both
additions as substrings of `_BASE_SYSTEM`/`_BASE_SYSTEM_TEXT`, including their placement ahead of
the Concept-steering tail and their survival through `extraction_system_prompt(packs=["sdlc"])`.
Full suite: `uv run pytest -q -p no:randomly` — 3795 passed, 90 skipped, 1 xfailed (baseline
3788/90/1 plus the 7 new tests), no regressions.

**Measurement is explicitly deferred, not skipped**, following the same discipline as A1 above:
this addendum proves the instruction is present in the prompt, not that a live model obeys it.
Confirming that the private vault's `TASK-n`/`US-n` nodes re-type as Activity — and that a named
assignee becomes a separate Agent rather than retyping the item — requires a fresh live ingest
against the fixed prompt, which is not run here.

## Addendum — 2026-09-15: D6a — ingest-time predicate resolution

### What was measured

On the live `personal-assistant` vault, after roughly 20 documents: 78 predicates, 57%
singletons, 1.47 *new* predicates per extraction block — no saturation. `has_value` sits
canonical at support 91 while `has_status` (49), `has_measurement` (20) and `has_config`
(19) — the vocabulary ADR 0020 *locks* — sit permanently provisional.

Three verified causes, all of them ours:

1. The relation curator was shown a **hard-coded ~50-label string** baked into
   `_BASE_RELATION_CURATOR_SYSTEM`. `relation_curator_system(packs)` takes only `packs`;
   it had no access to the registry or the vault, so on document 20 the curator could not
   know `has_status` had already been minted 49 times. Twenty-six of those 49 advertised
   labels were seeded in no registry at all, so the prompt itself manufactured the
   `queue_unregistered` mint loop it was meant to prevent. This is the "hard-code a
   complete global predicate list" alternative this ADR rejects by name — shipped by
   accident, in prompt prose.
2. **There was no judge for predicates.** Entities get blocking → MergeJudge →
   merge/queue. `admit_predicate` is a lookup plus a shape check; when it returned
   `queue_unregistered` the ingest mint path just registered the new label. Nothing ever
   asked "is this the same as something I already have?".
3. `seed_builtins` seeds only `has_value` of ADR 0020's six, so the other five are minted
   as model provisionals by whichever document reaches them first.

Causes 1 and 2 are what this addendum fixes. Cause 3 is **recorded and deliberately not
acted on** — see "Deferred: the catch-all dense-fact predicates" below.

### D6a — Ingest-time predicate resolution

**D6a.1** A novel predicate proposed at ingestion is resolved against the live registry
before it is minted. When admission returns `queue_unregistered` and the curator verdict
would otherwise mint a provisional record, **one resolution call per novel label per run**
compares the proposed label's DEFINITION — not its surface form — against every registered
predicate's definition, direction and support. The definition is the identity; the label is
close to arbitrary and frequently not even in English. This is also what makes the
Portuguese-label problem mostly dissolve on its own.

**D6a.2** Four verdicts, matching binding contract 7 and ADR 0017 D3: `same`, `inverse`,
`narrower`, `distinct`. Only `same` folds. `inverse` and `narrower` fold nothing at ingest
and record a queued mapping. `distinct` mints as before.

**D6a.3** A `same` verdict whose confidence meets the fold gate and whose target is an
exact label in the run's registry is re-admitted under that target, and — only if that
re-admission still commits — writes one `exact_match` mapping record at status `auto`.
This is a PROSPECTIVE fold, authorized by binding contract 6: it affects only future
occurrences of a label that has never been committed, it rewrites no existing claim, and a
human can delete the record. It is not `predicate-apply`; ADR 0017 D6's propose-only
boundary is unchanged.

The ordering is load-bearing and is the one place this decision can leak. `auto` is one of
the two statuses `alias_map` acts on, so the record is binding on every later run; staging
it at resolution time, before the direction guard of D6a.8 has ruled on the re-admission,
means a fold THIS run refuses is applied silently by the NEXT one — the relation queues
once, then commits wrong forever after. The fold record is therefore held per novel label
and staged only after the re-admitted relation survives every gate. The `queued` records of
D6a.5 and the non-`same` verdicts are staged immediately: `alias_map` ignores `queued` by
construction, so they bind nothing.

Re-admission also moves the audit candidate to the folded label. `_RelationEntry` enforces
`candidate.type == pinned_proposal.raw_predicate` and the pinned proposal carries the
post-fold `raw_predicate`; leaving the pre-fold label on the candidate is invisible while
the relation commits (the commit path rebuilds the semantic candidate from
`admission.predicate`) and aborts the whole ingest the moment anything downstream queues a
folded relation — which D6a.8 does by design. The model's own proposed label is not lost:
the `predicate_resolution` ledger row of D6a.6 keeps it explicitly.

**D6a.4** "What folds" is ONE function. The ingest caller and the record builder both ask
`folds_onto_incumbent`, rather than each re-deriving the condition: verdict `same`, a
non-empty target that is not the proposal itself, confidence at or above the gate, and a
`canonical` that is either empty or the target. Two parallel conditions disagree on the
case a model actually produces — a `canonical` naming some THIRD label, neither incumbent
nor proposal — where the caller declines to fold and a separately-derived record builder
would still write `auto`, folding the label on every later run. That verdict now writes no
record at all.

Every ingest-written fold record carries evidence counts chosen so `alias_map`'s
root election deterministically elects the incumbent, and no fold record may name a
`_CORE_PREDICATE_ALIASES` key as its target (D5: core mappings keep precedence). The
election detail matters and is easy to get wrong: the union-find inserts the record's
*subject* first and breaks count ties toward it, so truthful raw counts of
`{novel: 1, incumbent: 0}` — the ordinary shape on a fresh vault whose canonical was just
seeded — would elect the NOVEL label and silently invert the fold. The novel label's
committed-occurrence count is genuinely 0 (it never committed, and after the fold it never
will), and the incumbent carries a floor of 1 encoding the checkable fact that it is
registered and the proposal is not. The real in-run proposal count is kept beside it for
audit rather than folded into the election.

**D6a.5** Superseding an incumbent is **proposed, never applied**. When the resolver names
the novel label as canonical against a registered incumbent, the novel label mints and an
`exact_match` record is written at status `queued` for the maintenance sweep. Automatic
supersession of a supported incumbent is out of scope, and lifecycle demotion is explicitly
not the mechanism: canonical and provisional both map to `commit_topology`, so a demotion
would be a functional no-op.

**D6a.6** Every resolution decision is recorded as one durable ledger comparison row under
`method="predicate_resolution"`, keyed `predicate:<label>` and carrying the model's own
proposed label, its definition, the chosen target, the gate and the confidence. A resumed
or replayed run consults those rows and issues no LLM call, preserving ADR 0015 D5b. The
row is required and is not substitutable by the alias file: a `distinct` verdict writes no
alias record, so without the row every later run re-asks the judge the same question — the
exact non-determinism being fixed.

**D6a.7** `predicate_admission` advances to **v3**. `admit_predicate` itself stays pure and
unchanged; the admission *decision procedure* its ingest caller applies does not. Without
the bump, full-policy replay of a v2 run would reuse a mint that v3 policy would have
folded. Older full-policy verdict/plan replay is deliberately ineligible.

**D6a.8 — one direction rule, one place.** The direction guard in the relation loop widens
from `state == "provisional"` to also cover `state == "canonical"`, and the resolver does
NOT re-check direction itself. Without the widening, `planned_predicate_records` keeps
`base.direction`/`base.definition` verbatim and only advances signatures/support/samples/
confidence — so a curator that called a directed CANONICAL predicate symmetric committed
silently, with no trace. It now queues with reason `queue_mapping_conflict`, and the fold
path of D6a.3 is covered by the same single rule.

The widened arm fires on direction CONFLICT only. `unknown` is
`CuratorVerdict.predicate_direction`'s own default, so it means the curator asserted
nothing about direction — absence of evidence, not disagreement — and must not queue every
relation whose predicate the vault already governs. (The pre-existing `provisional` arm
still treats absence as a conflict; whether it should is an older question this addendum
deliberately does not reopen.)

**Fail-closed framing, stated rather than assumed.** "Mint" here means *register a
provisional label and queue the RELATION for review*
(`_ACTION_BY_REASON["queue_unregistered"] == "queue_review"`), never *commit*. A provider
error, an unparseable reply, a target absent from the registry, or a confidence below the
gate all resolve `distinct` and fall back to that status quo. The only irreversible-ish act
is the `auto` fold, and it is gated.

**Why one call, not the maintenance judge's symmetric two votes.** Not cost. The fold is
prospective and reversible, and the pair is not symmetric: side A is a never-committed
proposal carrying a definition, side B is a registry row, so reversing them asks a
different question. Precision comes instead from four cheap deterministic guards — the
target must be an exact label in the snapshot, confidence ≥ gate, the single direction
guard above, and the core-alias refusal. The gate starts at 0.85 (stricter of
`MERGE_CONFIDENCE` 0.8 and the maintenance judge's `auto_fold_threshold` 0.85) and is a
named module constant awaiting a measured re-set from a full ingest.

**Recall, and where the registry block lives.** The frozen label list is deleted from
`_BASE_RELATION_CURATOR_SYSTEM`; the live registry is rendered once per run from the
start-of-run snapshot into the relation curator's USER prompt. Two independent reasons it
cannot go in the system prompt: `effective_relation_curator_system` discards the base
prompt entirely when a vault sets a custom one, so a system-prompt append would silently
vanish for those vaults; and the system prompt is hashed into `config_fingerprint`, so a
registry-bearing system prompt would move the fingerprint on every mint, mid-run, killing
resume and decision replay. The eight collapse rules stay in the system prompt because all
eight resolve in `_CORE_PREDICATE_ALIASES` and are applied by `normalize_predicate`
regardless of model output — stable policy, not vault state.

The block is byte-stable for the whole run *on purpose*, even as the run mints: a block
that grew with each mint would break the provider's prefix cache on every novel predicate.
The in-run registry the resolver sees is the live, growing one, so a fold is binding against
labels this same run already coined. That asymmetry is deliberate and is commented at both
sites.

For the batch path the block must sit inside the prefix `build_batch_user_prompt` strips.
That strip is `prompt[len(prefix):] if prompt.startswith(prefix) else prompt` — it fails
**silently**, and the prompt still "works", so the placement is pinned by test rather than
by review: measured at k=4 with a 16,000-char excerpt, inside the stripped prefix gives
16,582 chars / 1 excerpt copy, outside gives 80,654 chars / 5 copies. Latent today
(`curation_batch_size` defaults to 1) and a landmine the moment batching is enabled.

**What this buys, and what it does not.** It improves the vocabulary of documents ingested
AFTER it lands. It does nothing to the records already in the live vault: `P` bakes into
`semantic_claim_id` and binding contract 6 confines retroactive materialization to
heal/rebuild. Cleaning the existing vault is a separate, explicit owner-triggered heal.
And `inverse` produces **zero predicate-count reduction at ingest, by construction**:
`inverse_map` reads only `status == "confirmed"` and ADR 0017 requires inverse verdicts to
queue. The ingest step writes the queued record that makes `apply_confirmed_inverse`
reachable one human confirmation later; it does not revive that dead code now.

### Scope, explicitly

No new ADR: this is Phase 3 plus D6, and binding contract 6 already authorizes "prospective
folds … during admission/ingest". A new ADR standing up parallel machinery would itself
violate D5's ban on a second ontology mechanism. ADR 0017's `predicate-propose`/
`predicate-apply` maintenance sweep is untouched and stays propose-only; see that ADR's
2026-09-15 scope note.

The word "demote" is deliberately avoided for the supersession case. `prefilter.demote_predicates`
is an existing, separately-fingerprinted config denylist of conversation-mechanics predicates,
and two things called "demote" in one pipeline is how a parallel mechanism gets built by accident.

### Deferred: the catch-all dense-fact predicates

An earlier draft of this addendum also seeded and promoted ADR 0020's remaining dense-fact
labels (`has_version`, `has_config`, `has_status`, `has_measurement`, `table_cell`). That
work is **withdrawn from this change** at the owner's direction, unimplemented, pending a
prior question: whether these catch-all predicates should exist at all.

The evidence that prompted the question, recorded so the later decision starts from it
rather than from scratch:

- `has_value` holds 102 claims in the live vault spanning identifiers, deadlines,
  expiries, schedules and file counts — one label doing the work of at least five
  relations, which makes it useless as a retrieval handle.
- It also drives **under-decomposition** at extraction time. A single observed claim,
  `US-09 Category View has_value coverage 0%, priority SHOULD, status NOT STARTED`, packs
  three independent facts into one literal because one catch-all predicate was available to
  absorb them.

Seeding a catch-all as core canonical is an endorsement, and endorsing the wrong ones is
harder to walk back than leaving them provisional. So `seed_builtins` still seeds only
`has_value` (unchanged and pre-existing), the bootstrap performs no promotion of stored
provisionals, and nothing in this addendum changes which dense-fact labels a vault holds.
`recommended_rule` likewise stays unseeded; it is backed by no ADR and its only occurrence
in `src/` was the frozen prompt string D6a deletes.

Note the interaction, because it is the reason this is not urgent: D6a's resolution step
now runs on exactly these labels. A vault that has already minted `has_status` will have a
later proposal of the same meaning folded onto it, whatever its lifecycle — the fold gate
reads the registry, not the `core`/`model` provenance.

### Measurement is deferred, not skipped

This addendum proves the mechanism exists and is pinned by model-free tests. The numbers
that decide whether the gate is right — novel predicates per extraction block, fold rate,
`auto` vs `queued` record counts — need a fresh full ingest against the landed code.
Baseline to beat: **1.47 new predicates per extraction block, 57% singletons.**
