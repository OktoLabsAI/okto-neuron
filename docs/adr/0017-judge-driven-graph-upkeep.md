# ADR 0017 — Judge-driven continuous graph upkeep (predicate canonicalization)

- **Status:** Accepted (implemented & validated live 2026-06-12)
- **Date:** 2026-06-12
- **Deciders:** Alex Rivera
- **Relates to:** ADR 0008/0010 (entity reconciliation — the pattern this generalizes), ADR 0009 (curation control plane & scheduler), ADR 0016 (semantic claim identity — the fold composes with it)
- **Out of scope:** claim contradiction/staleness detection (future upkeep pass); cross-vault upkeep; auto-applying entity merges (stays ADR 0010); changing the scheduler's PROPOSE-ONLY contract.

## Problem

Open LLM extraction coins predicates freely. The live playground vault holds **~1,061 distinct predicate tokens over 3,023 claims** (probe 2026-06-12): `located_in`/`located_at`/`was_at`/`is_at`/`arrived_at`, `complained_about`/`complains_about`/`reports_issue`, `shared_link`/`shared_link_to`/`shared_link_about`, `wrote_to`/`correspondent_of`/`communicates_with`… ADR 0016 stopped exact-duplicate claims, but synonym predicates keep the same fact split across rows, and nothing maintains the graph between ingests. The product premise is a *living* graph: upkeep must be a recurring, budgeted, confidence-gated process — not a one-off script.

Literature grounding (full citations in the knowledge base):
- Canonicalization needs **side information**, not embedding similarity alone — argument-type signatures, shared argument pairs *with order*, source context (CESI WWW'18; CMVC KDD'22; Galárraga CIKM'14).
- The LLM-era shape is **define-then-match + LLM verify** (EDC, arXiv:2404.03868).
- Embedding prefilters **propose inverse pairs by construction** (symmetric scorers can't represent antisymmetry — ComplEx, arXiv:1606.06357), and antonyms sit close in embedding space → the judge's verdict set must distinguish inverse/narrower from same.
- LLM judges have **acquiescence bias on yes/no "are these the same?"** (arXiv:2509.08480) and position bias (arXiv:2410.02736) → multi-option verdicts, symmetric prompting, vote agreement over stated confidence.
- Production KGs (Wikidata) gate identity-level changes on humans; **wrong merges cost far more than missed merges** → folds must be off-graph and revocable (the v0.0.5 Option-A pattern), exactly like entity exactMatch today.
- Durable decisions should be **SSSOM-shaped mapping records** (arXiv:2112.07051) with confidence + justification + provenance; `skos:exactMatch` for alias (revocable, retrieval-strength), `rdfs:subPropertyOf` for narrower, `owl:inverseOf` for direction — never `owl:equivalentProperty` from a probabilistic judge.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **Predicate alias ledger** at `.marginalia/predicates/aliases.json` (`PredicateAliasIndex`, mirroring `AuthorityIndex` `authority.py:105`): SSSOM-shaped records `{id, subject_predicate, mapping: exact_match\|sub_property_of\|inverse_of, object_predicate (canonical), confidence, justification, evidence: {counts, shared_pairs, sample_claim_ids}, judge_model, votes, status: auto\|confirmed\|rejected, created_at}`. Survives rebuild like the AuthorityIndex; de-merge = ledger edit, never graph surgery. | Durable, standards-aligned, revocable. |
| 2 | **Candidate generation is deterministic and cheap**: enumerate predicate vocabulary from `store.list_edges()` + Claim facets `P` with counts; embed predicate names with the vault embedder; cluster (cosine, threshold configurable, default 0.80); within clusters, rank pairs by (string affinity, shared-argument-pair overlap computed in both orders). Hard caps: `max_pairs_per_run` (default 30), `min_support` (default 2 uses; singletons only join clusters as members, never anchor one). | ~1K predicates ⇒ all-pairs (~500K) impossible; clustering + caps keep each run bounded. Order-aware overlap is the inverse-pair tell. |
| 3 | **Predicate judge** (mirrors `LLMMergeJudge`): multi-option verdict `{verdict: same\|inverse\|narrower\|distinct, canonical: <pred>, confidence, reason}` — never yes/no. Evidence-grounded prompt: per-predicate counts, EDC-style one-line definitions the judge writes first, argument-type signatures, ≤3 sample claims each with source-Block excerpts, shared-pair table with order. **Symmetric prompting**: each pair judged in both orders; verdicts must agree or the pair degrades to `queue`. `enable_thinking=false` (judge default, `_vault.py:465`). | Bias mitigations from the LLM-judge literature; evidence over parametric knowledge. |
| 4 | **Confidence gating, Wikidata-style**: `same` with consistent both-order verdicts and confidence ≥ `auto_fold_threshold` (default 0.85) → auto record (`status: auto`); anything else (`inverse`, `narrower`, low-confidence `same`) → review queue for human confirm via the existing reconcile-review UX pattern. `distinct` decisions are **also recorded** (negative cache) so pairs are never re-judged. | Wrong merges are the expensive failure; negative cache makes the loop converge instead of re-spending LLM calls. |
| 5 | **Fold semantics compose with ADR 0016**: (a) *query-time* — predicate equivalence folds in the read path exactly where entity exactMatch folds today; (b) *heal-time* — `copy_graph_canonicalizing` rewrites `edge.type` and Claim facet `P` through the alias map (and swaps S/O for confirmed `inverse_of`) **before** the ADR 0016 semantic-triple collapse, so folded claims merge and corroborations sum with zero new machinery; (c) *ingest-time* — `normalize_predicate` (`curator.py:316`) gains a vault-local alias table loaded from the ledger, so future extractions normalize at the source and rebuilds converge. `narrower` records fold nothing (subsumption is metadata for retrieval expansion). | One decision, enforced at all three lifecycle points; heal reuses 0016's collapse. |
| 6 | **Scheduler integration honors ADR 0009's PROPOSE-ONLY contract**: new job kind `predicate-propose` joins `SWEEP_KINDS` (`_scheduler.py:47`) — scheduled runs only *propose* (and write auto-eligible records as proposals, not folds). `predicate-apply` (writes ledger `auto` records + triggers nothing else) and heal remain explicit user actions, same as reconcile apply today. New config block `upkeep:` (writable via PATCH): `{enabled, max_pairs_per_run, min_support, cluster_threshold, auto_fold_threshold}`. | Continuous but never silently destructive; consistent with the rest of the control plane. |
| 7 | **Observability**: propose/apply runs are curation jobs (visible in dashboard + `curation-jobs.json` rehydration); ledger comparisons logged with method `predicate_judge`, verdicts recorded with per-call timing/tokens like every other judge (ADR 0015 contract). UI: upkeep panel on the Curation dashboard (vocabulary size, pending proposals, recent folds) + review queue list with confirm/reject. | Same visibility bar as reconcile/heal. |

## Edge cases & limits (binding)

- **Inverse pairs** (`shared_by` vs `shared_with`): auto-fold forbidden; `inverse` verdicts always queue. Confirmed inverse folds rewrite S/O at heal with the rewrite recorded on a judge Activity.
- **Directional → symmetric collapse is forbidden** (OOPS! P05/P21): `wrote_to` may be recorded `narrower` than `communicates_with` but never `same`.
- **Granularity**: when one predicate is strictly more specific, the verdict is `narrower`, which folds nothing.
- **Antonyms/near-embeddings** (`arrives_at` vs `departs_from`): survive the prefilter by design; the judge's evidence (shared pairs, excerpts) and the `distinct` negative cache handle them.
- **Singleton tail** (~700 single-use predicates): they cluster onto anchors with `min_support ≥ 2`; a singleton pair never spends a judge call on its own.
- **Budget**: a run never exceeds `max_pairs_per_run × 2` judge calls (symmetric prompting); with defaults that's ≤60 calls/run.
- **Convergence**: every judged pair (incl. `distinct`) is cached in the ledger keyed by unordered pair; re-runs skip judged pairs unless `--rejudge`.
- **Rebuild**: ledger survives; ingest-time normalization applies confirmed/auto aliases during re-extraction; heal converges the rest.
- **Conflicting chains** (A same→B, B same→C): canonical resolution follows union-find to a single root; cycles broken by highest count wins.
- **Core alias table precedence**: `_CORE_PREDICATE_ALIASES` (`curator.py:243`) applies first; the vault-local table may not override core mappings.

## Consequences

- The graph gains a self-maintaining loop: ingest → (debounced) scheduled propose → human-or-threshold gate → heal materializes — the living-graph behavior entity reconciliation already has, extended to relations.
- Judge spend is bounded and convergent (negative cache); the predicate vocabulary should shrink toward a stable canonical set over successive runs.
- The review queue gains a second proposal type; UI must keep entity merges and predicate folds visually distinct.
- Future upkeep passes (claim staleness, contradiction detection) plug into the same `upkeep:` config + job-kind pattern without new architecture.

## Addendum — 2026-09-15: scope note, ingest-time predicate resolution is not this sweep

ADR 0040 D6a adds a resolution step at **ingestion**: when admission returns
`queue_unregistered` and the relation curator's verdict would otherwise mint a novel
provisional label, one call per novel label per run compares that label's *definition*
against the live registry and may fold it onto an incumbent before the mint.

That is a different concern from this ADR, and nothing here changes:

- D6's propose-only boundary governs **sweeps over committed graph state**. It stands.
  `predicate-apply` remains an explicit user action and is still absent from
  `SWEEP_KINDS` (`server/_scheduler.py:52`), pinned by
  `tests/server/test_scheduler.py::test_sweep_kinds_still_exclude_predicate_apply`.
- `LLMPredicateJudge`, its system prompt, its response schema, and its verdict→status
  mapping are untouched, pinned by
  `tests/predicates/test_judge.py::test_maintenance_judge_prompt_and_schema_are_unchanged`.
  The ingest resolver is a sibling module (`predicates/resolve.py`), not an edit to this
  one: `PredicateCandidate` (`candidates.py`) has no definition field and every field it
  carries derives from `_scan_store` over committed state, so a never-committed proposal
  has no side-A evidence to give it.
- The two share *artifacts*, deliberately: the same `PredicateAliasRecord`, the same
  `exact_match`/`sub_property_of`/`inverse_of` mapping vocabulary, the same
  `auto`/`queued` statuses, and the same deterministic `_record_id`, so a fold written at
  ingest and one written by this sweep are the same row and `upsert` stays idempotent
  across both writers.
- D6a's two non-`same` verdicts and its supersession case write **queued** records that
  this sweep's review surface picks up. Ingest proposes; the human confirms here.

The reason this note exists: a future reader should not read the ingest-time resolver as
licence to make `predicate-apply` automatic.
