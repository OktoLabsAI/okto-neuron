# ADR 0022: A smarter merge judge — select-framing, neighbourhood context, clustering, and temporal invalidation

- **Status:** Accepted and implemented (reconciled 2026-07-13)
- **Date:** 2026-06-24
- **Deciders:** Alex Rivera, Marginalia core
- **Builds on:** ADR 0004 (within-batch entity resolution), ADR 0008/0010 (retroactive reconciliation, recall overhaul), ADR 0016 (claim semantic identity & corroboration), ADR 0017 (judge-driven predicate canonicalization), ADR 0007 (rebuild from the markdown trust root)
- **Grounding research:** `research/entity-resolution-merge-judge.md`
- **Out of scope:** changing the 5-primitive closed schema; the embedding-band blocking stage (`JUDGE_BAND_THRESHOLD`); the off-graph authority-fold mechanism (ADR 0008 Option A).

**Lifecycle addendum — 2026-07-13.** Select-framing, neighbourhood context, cluster
support, and guarded integral-correction behavior shipped with regression coverage.
Later tuning remains product quality work, not an open release gate.

---

## Context

Marginalia adjudicates entity sameness with an LLM. `LLMMergeJudge`
(`src/marginalia/resolve/__init__.py:545`) decides one candidate against one
existing node — a **pairwise** call (`.judge()`, :578) — biased "distinct when
unsure" (`_VERDICT_SYSTEM`, :479), acting on a `same` verdict only when
confidence clears `MERGE_CONFIDENCE`. `judge_within_batch` (:815) and
`judge_against_store` (:737) loop that pairwise judge over the top-`JUDGE_K`
band-neighbours (`JUDGE_K = 3`, :444) and **merge into the first** adjudged
`same`. The confidence gate (`src/marginalia/consolidate/gate.py:83`, `decide`)
then routes contradictions to review (`REVIEW_ON_CONTRADICTION = True`, :37) and
everything below `AUTO_COMMIT_THRESHOLD = 0.75` (:33) to the
`ReviewQueue` (`src/marginalia/consolidate/review_queue.py:96`).

The grounding research (`research/entity-resolution-merge-judge.md`) shows four
weaknesses against the literature, each independently fixable:

1. **Pairwise framing is the weakest.** Wang et al. (NAACL 2024,
   arXiv:2405.16884) measure isolated pairwise *Match* at 64.0 F1 vs *Select*
   (judge the whole candidate set together) at 81.6 F1 (+17.6), with documented
   position bias. Our judge is the 64% framing.
2. **The neighbourhood is computed but not load-bearing.** The judge already
   accepts `candidate_context` / `existing_context` (the `MergeJudge` protocol,
   :461) populated with each entity's relationships, capped at `_REL_CAP = 5`
   (:615) — but they are *supporting* evidence, not decisive. Collective ER
   (Bhattacharya & Getoor, TKDD 2007, DOI 10.1145/1217299.1217304) shows
   shared-neighbour evidence is what rescues true matches *and* blocks
   coincidental same-name merges.
3. **Greedy "first same" fractures entities** — the documented transitive-merge
   gap. Correlation clustering (Bansal et al., 2004,
   DOI 10.1023/B:MACH.0000033116.57574.95) optimizes the whole partition and can
   keep a–b and b–c while refusing a–c.
4. **Genuine conflict is parked, not dated.** `gate.decide` has no supersession
   or temporal validity; the `freshness` field is stored-but-unused. Graphiti's
   bi-temporal `valid_at`/`invalid_at` (arXiv:2501.13956) and Mem0-graph's
   obsolete-marking (arXiv:2504.19413) date conflicts instead of parking them.

**The asymmetry that makes a higher-recall operating point safe.** Markdown is
the trust root and the graph is derived (ADR 0007): a bad merge is **recoverable**
via `kg rebuild`, unlike Graphiti where the graph is canonical and a bad merge is
permanent data loss. So Marginalia can run the judge at a *higher-recall*
operating point — with the review queue as the Fellegi-Sunter clerical-review
band (JASA 1969, DOI 10.1080/01621459.1969.10501049) and markdown as the floor —
where a canonical-graph system could not.

## Decision

Redesign the merge judge along the four levers, **sequenced so each is
measurable before the next ships**.

### Lever 1 — SELECT-framing (replaces pairwise looping)

Add a cluster-judge call that scores a candidate against **all** its
band-neighbours in one prompt and returns the best match (or none), replacing the
"loop pairwise, merge into first `same`" behaviour in `judge_within_batch` /
`judge_against_store`. Follow ComEM (arXiv:2405.16884): the embedding band is
already the cheap filter, so the LLM only selects over the short list — bounded
cost, position-bias mitigated by the short list. The pairwise `LLMMergeJudge`
stays as the fallback and the unit-test surface; `MergeVerdict` (:451) is
unchanged. This is contained to the resolve call sites.

### Lever 2 — make neighbourhood context load-bearing

Keep the existing `candidate_context` / `existing_context` seam (:461) but
promote shared-neighbour overlap from supporting to decisive in the select
prompt, and feed it the **byte-anchored Claim side-information** Marginalia
uniquely has: two candidates whose Claims (`semantic_claim_id`,
`src/marginalia/consolidate/_claim_identity.py:30`) cite overlapping Blocks, or
that share corroborated relationships, get a sameness boost; disjoint
neighbourhoods with a name collision get a distinctness penalty. Raise `_REL_CAP`
only as the select prompt's budget allows.

### Lever 3 — cluster-level B³ in the deterministic eval floor

Before any clustering change, add a **cluster-level B³ / merge-distance** metric
(arXiv:2404.05622) to the CI-gated deterministic floor, scored over a fixture
partition. Pairwise P/R/F1 cannot see a fractured partition; B³ can. This makes
the transitive gap (and any regression from Lever 1/2) visible.

### Lever 4 — correlation clustering over the band graph

Replace greedy "merge into first `same`" with a correlation-clustering pass
(Bansal et al., 2004) over the candidate band graph, using the select verdicts as
edge weights, so the whole partition is optimized at once and the transitive-merge
gap is retired. Sequenced **after** Lever 3 so the win is measurable.

### Lever 5 — temporal invalidation for genuine conflict

Give the gate a third fate beyond commit/review: **supersede**. When a new Claim
contradicts an existing one on the same (S, P) but with a fresher source, set the
old Claim's `valid_until` (activating the stored `freshness` field) rather than
parking the new one. This maps onto the existing Claim/corroboration model
(ADR 0016) — supersession is a temporal edge, not a delete — and is the one
field that closes the conflict-parking recall leak. Reversible (drop the
`valid_until`), consistent with the rebuildable-graph posture.

> **Implemented (2026-06-30, via ADR 0024).** Lever 5 ships as an INTEGRAL
> post-mint correction pass considered on every `remember()`
> (`companion/_incremental.py::supersede_contradicted`, wired in
> `companion/__init__.py`). It generalises the original "same (S, P)" trigger:
> because extraction drifts subject/predicate/object phrasing between runs, a
> lexical prefilter (shared subject id or same-document subject-title token,
> different value)
> bounds candidates and the judge LLM confirms a genuine correction
> semantically. The old Claim is stamped `_superseded` + `valid_until` (filtered
> from recall via `_internal.infra.is_superseded`) with a `supersedes` edge
> new→old. Default is auto-supersede (user decision 2026-06-30), not park.

## Why

Each lever attacks a measured weakness with a contained change, and the
markdown-trust-root asymmetry is what lets us spend recall aggressively: the
worst case of a too-eager merge is a `kg rebuild`, not lost data. The sequencing
puts the cheap, high-leverage judge-prompt changes first, makes the gain visible
with a cluster-level metric, then pays for the heavier clustering and temporal
work only once it's measurable.

## Consequences

- **Lever 1+2** are contained to `resolve/__init__.py` call sites and the gate
  prompt; expected to be the dominant quality win at modest cost (ComEM-style
  filter→select), per arXiv:2405.16884.
- **Lever 3** adds a CI-gated metric — no behaviour change, pure visibility — and
  is the precondition for trusting Lever 4.
- **Lever 4** changes the partition algorithm; risk is contained because B³
  (Lever 3) gates it and a bad partition is recoverable via rebuild.
- **Lever 5** adds one field (`valid_until`) and a third gate fate; it shifts
  conflict handling from parking to dating, which raises effective recall without
  loosening the auto-commit threshold.
- **Higher-recall operating point is now defensible** because of ADR 0007: a
  false merge is recoverable, the review queue remains the abstain band, and the
  closed 5-primitive schema still forbids cross-primitive merges (type-aware
  blocking is a free precision win).
- **Honest bound:** the literature numbers (e.g. +17.6 F1 for select-framing) are
  from frontier-model / benchmark setups; the transfer to Marginalia's local
  judge is directional, not guaranteed. Lever 3's eval floor exists precisely to
  measure the real delta before flipping defaults.

## Addendum — 2026-07-02 remediation (F8): recency-aware corrections

The integral correction pass (Lever 5) was ingestion-order-blind: re-ingesting
an old backup could supersede fresher facts. Every LLM-minted Claim now
carries `asserted_at` — the SOURCE file's mtime date at ingest (the folder
watcher's `copy2` durable copy preserves mtime; fallback: ingest date). The
supersede candidate prefilter drops any existing claim asserted LATER than
the incoming source, corroboration bumps keep `asserted_at = max(old, new)`,
and the ADR 0024 revert/resurrection legs gate on it.

ADR 0040 adds a narrower temporal ordering signal without redefining
`asserted_at` or claiming complete valid-time support. When one unique source
line contains both relationship endpoint surfaces and one explicitly zoned RFC
3339 timestamp, the Claim stores that exact byte-verified line as its
`source_span`, plus `source_asserted_at` and `source_time_evidence`. The
supersedence gate compares this signal only when both Claims possess it; an
equal explicit timestamp is ambiguous and cannot auto-supersede. Otherwise the
mtime-based F8 gate remains authoritative. Filenames, Claim ids, and source
traversal order are never temporal tie-breakers. The comparison is symmetric:
when an older Claim arrives after an already-live newer Claim, the correction
judge may confirm the same conflict and the incoming historical Claim is marked
superseded by the existing newer Claim. It is not merely prevented from
superseding the newer fact, because leaving both active would still make recall
depend on ingestion order.

*Accepted residual (review-confirmed):* claims minted BEFORE this change
carry no `asserted_at`, so the recency prefilter cannot protect them — an old
backup could still offer corrections against legacy claims (the judge remains
the guard). Accepted pre-alpha: the demo-vault vault rebuilds from zero, so all
its claims carry the facet; other vaults heal as claims re-mint.

## Addendum — 2026-07-18 correction scope and cost boundary

The private organizational corpus (CoP) baseline proved that the correction pass was still
fed every model Claim already in the vault. On first ingestion of later source
files, there is no prior version of that source to correct, yet shared central
subjects caused hundreds of Correction Judge calls. Cross-document token-only
verdicts could never auto-apply and were retained only as telemetry, while the
exact reconciliation job already owns cross-document comparison.

The owning boundary is now the source edit itself. Correction candidates are
only live model Claims anchored to Blocks orphaned by this exact source change,
excluding Claims that retain support from a current Block. A first ingestion or
pure addition has no removed evidence and makes zero Correction Judge calls.
Within an actual edit, shared resolved subjects remain eligible; subject-title
drift is eligible only inside the edited document. Cross-document work remains
in the generation-bound reconciliation stage. The prompt now states its real
direction explicitly: one NEW fact selects the OLD removed fact it corrects.
This narrows impossible work without weakening any auto-supersede case that was
permitted by the document-lineage rule.
