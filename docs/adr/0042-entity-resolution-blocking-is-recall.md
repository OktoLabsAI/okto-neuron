# ADR 0042: Entity-Resolution Blocking Is a Recall Stage — Disjunctive Lanes, Judge-Owned Precision

- **Status:** Accepted
- **Date:** 2026-09-14
- **Deciders:** Marginalia maintainers
- **Builds on:** ADR 0004 (within-batch entity resolution), ADR 0008 (retroactive entity
  reconciliation), ADR 0010 (reconciler recall + judge + corroboration overhaul), ADR 0013
  (durable candidate ledger), ADR 0022 (smarter merge judge — review queue as clerical-review
  band), ADR 0040 (semantic graph quality — the `exact_surface_key`/`discovery_surface_key`
  contract and cross-type identity adjudication)
- **Scope:** the recall (blocking) stage of ingest-time entity resolution in
  `Companion.remember` — within-batch judging and against-store judging — plus the
  `exact_surface_key`/`discovery_surface_key` contract in `semantic_surface.py` and the
  discovery-key bucketing used by cross-type identity adjudication
- **Out of scope:** the merge-judge verdict prompt and model itself (ADR 0022 owns that); the
  off-graph retroactive reconciliation *pipeline* (ADR 0008/0010) — its clustering, oversize-
  cluster cap-splitting, corroboration, and authority/apply machinery are not touched or
  redesigned here. Only its pairwise lexical lane *predicates* are reused, at a new call site
  with a different comparison population (within-batch candidates and committed-store nodes at
  ingest time, not only committed-store nodes against each other); changing the closed
  five-primitive schema; phonetic matching and nickname gazetteers (recorded below as
  deliberately deferred)
- **Implementation status:** the `discovery_surface_key` diacritic fold and the cross-type
  identity-adjudication rebucketing onto that key are in the working tree
  (`semantic_surface.py`, `companion/__init__.py`). The disjunctive ingest-time recall lanes that
  reuse `reconcile/candidates.py`, and the corresponding inversion of two `tests/resolve/` tests
  that previously encoded a low-cosine veto, are landing concurrently in `resolve/__init__.py`
  and `tests/resolve/`.

---

## Purpose

Ingest-time entity resolution (`Companion.remember`'s within-batch and against-store judging,
`resolve/__init__.py`) recalls candidate duplicates through a single lane: same-type embedding
cosine at or above `JUDGE_BAND_THRESHOLD`/`SIMILAR_THRESHOLD` (0.82 in the pre-change design),
plus one narrow lexical lane for legal-suffix aliases (`co`/`inc`/`ltd`/etc.). (Constant names and
values in this ADR describe that pre-change design as documented outside `resolve/__init__.py`
itself; this ADR's own change lands concurrently in that file and does not depend on those exact
values surviving unchanged.) That is a **conjunctive** gate: unless a pair's cosine clears the
band, or it happens to match the suffix rule, the merge judge is never asked. A pair below the
line is not judged *distinct* — it is never *compared* at all, which is a different and less
recoverable failure.

Contrast this with the already-shipped retroactive reconciliation path (ADR 0008/0010), which
recalls duplicates in the committed store through a **union** of independent lanes: a widened
embedding band, lexical surname/Jaro-Winkler matching, ordered token-subset (first-name/full-name
containment), unordered token-subset (shared distinctive token regardless of word order),
email-handle decomposition, and an optional nickname gazetteer. None of those lanes can veto
another; each only proposes, and the conservative judge decides. That design is regression-gated
(ADR 0010 Axis 5) and has already fixed real recall misses in production. Ingest-time blocking
never got the same treatment — it kept the single-band design from before ADR 0008 existed.

This ADR settles that asymmetry: **ingest-time blocking must be disjunctive and recall-oriented,
the same way retroactive reconciliation already is. A low embedding score must never veto a pair
that another lane would recall. Precision belongs entirely to the merge judge, not to blocking.**

## Evidence that triggered this ADR

Read-only inspection of a live vault's candidate ledger and comparison log surfaced three findings.
Personal names below are described structurally rather than reproduced, per this repository's
handling of vault content.

| Finding | Observation | Reading |
|---|---|---|
| **Same-entity pairs never compared** | Four separate people were each stored as two `Agent` nodes — a bare given name and a fuller form of the same name (given name + surname, or given name + a longer multi-token name). None of the four short/full pairs was compared by any ingest-time tier — not Tier 0 exact, not the embedding band, not the suffix lane. | The only ingest-time recall lane is embedding cosine. The lexical lanes that would trivially catch a name/full-name containment (token-subset, shared surname) exist only in `reconcile/candidates.py`, not in the ingest path. |
| **Cosine carries no discriminating signal for short names** | One of those short given names *was* compared — against an unrelated person's short given name — at cosine 1.0, while never being compared against its own four-token full form by any tier. | The embedding model in use (`BAAI/bge-small-en-v1.5`) embeds short personal names near-identically regardless of identity. Cosine is not merely noisy here; it carries essentially no information, so which pairs happen to clear a single-band gate is arbitrary. A blocking design that depends on that score to *decide* recall is building on a signal that does not exist for this class of title. |
| **The judge was never the problem** | When the judge is actually asked, it rules correctly: a company's bare acronym vs. the same company's `INC`-suffixed legal form → same, confidence 0.95; a full Brazilian tax-regime document title vs. its lowercase acronym → same, confidence 0.95; two people sharing only superficial name similarity → distinct, confidence 1.0; two distinct Brazilian bookkeeping-system acronyms (`ECD`/`ECF`) → distinct, confidence 1.0. | The classifier is sound. Every miss above is a blocking miss, not a judgment miss. Fixing the judge would not have fixed any of the pairs above, because none of them ever reached it. |

## Decision

Ingest-time blocking (within-batch and against-store) changes from a single conjunctive
embedding-cosine gate to a **disjunctive union of recall lanes**, mirroring ADR 0008/0010's
retroactive design. Any lane firing is sufficient to propose a pair to the merge judge; no lane's
absence, and no other lane's low score, can veto a pair another lane proposed.

| # | Decision | Rationale |
|---|---|---|
| D1 | **Blocking becomes disjunctive.** A pair reaches the merge judge if the embedding band fires, OR any reused lexical lane fires. A low cosine on a pair that a lexical lane already proposed is not evidence against comparing it — it is simply a lane that did not fire. | This is the textbook three-stage record-linkage pipeline — blocking, comparison, classification (Fellegi & Sunter 1969) — with blocking tuned the way the standard reference on the topic prescribes: a pair a blocking scheme never emits can never be corrected by any later stage, so blocking is optimized for recall and classification is where precision is enforced (Christen 2012, ch. 4). |
| D2 | **Reuse, don't reimplement, the already-tested lexical lanes in `reconcile/candidates.py`** — length-gated Jaro-Winkler, exact shared surname, ordered token-subset (first-name/full-name containment), and unordered token-subset (shared distinctive token, order-insensitive) — as additional ingest-time recall lanes alongside the existing embedding band. | These predicates are already regression-gated against a gold merge/distinct set (ADR 0010 Axis 5). Reimplementing the same logic inside `resolve/__init__.py` would fork two copies of one predicate and let them drift; multi-pass ("disjunctive") block building from independently-sourced keys is itself standard practice for improving blocking recall (Papadakis, Skoutas, Thanos & Palpanas 2020), and sorted-neighborhood-style multi-pass blocking has the same motivation going back to Hernández & Stolfo (1995). |
| D3 | **`exact_surface_key` stays diacritic-sensitive** (NFC normalize, whitespace-collapse, casefold, NFC again — closed over diacritics by construction). **`discovery_surface_key` now also folds diacritics** (NFKD decompose, drop combining marks, recompose NFC), on top of the punctuation/underscore normalization it already did. | Per ADR 0040, source spelling and Unicode form are provenance: the exact/decision key must never silently rewrite what a source actually wrote. But the discovery key exists specifically to widen recall, and a corpus that mixes accented and unaccented spellings of the same name — routine in Portuguese- and Spanish-language content — needs the fold there, or the two spellings never even reach the merge judge. Normalizing before blocking is the standard first stage of the record-linkage pipeline (Christen 2012's preprocessing/standardization chapter precedes its blocking chapter for exactly this reason); Winkler's string-comparator work for the Fellegi-Sunter model (1990) is the same tradition of building comparison keys that tolerate exactly the surface variation exact matching must not. |
| D4 | **Cross-type identity adjudication now buckets on the discovery key, not the exact key.** | With the exact key diacritic-sensitive, an accented and unaccented spelling of the same name landed in different exact-key buckets and never produced a type-conflict review at all — the discovery key is the recall-only key everywhere else in this pipeline (ADR 0040), so cross-type bucketing should recall on the same broadened key. The LLM-driven type adjudication that already exists supplies precision; the bucketing key's job is only to make sure the conflict is ever surfaced to it. |
| D5 | **A proposed pair always reaches the same conservative merge judge at the same confidence gate.** Disjunctive recall changes what gets compared; it does not touch how "same" is decided. | Precision is the judge's job, full stop. Widening recall without touching the decision procedure is what keeps this change from trading a recall problem for a precision regression. |

## Precision invariants

These hold before and after this change and are not up for renegotiation by this ADR:

1. **No lane auto-merges.** Every recall lane, existing or newly reused, only proposes a pair.
   The merge judge decides "same," acted on only at the existing `MERGE_CONFIDENCE` gate.
2. **Tier 0 exact match is untouched.** `exact_surface_key` remains the only key eligible for a
   no-LLM merge. Nothing here lowers that bar or routes discovery-key matches through Tier 0.
3. **A negative decision still short-circuits before any judge call, in both directions.** The
   distinguishing-edge / negative-decision machinery from prior ADRs continues to veto a proposed
   pair outright. This ADR widens what gets *proposed*; it does not touch what can still *defeat*
   a proposal before the judge is even asked.
4. **Scoping is preserved.** Every lane still operates within one type and one
   batch-or-committed-store comparison scope, exactly as the existing tiers do. Nothing here
   compares across types or reaches outside the existing candidate/store population.

## Consequences

**Positive**

- Closes the exact class of miss in the evidence above: a short given name and the same person's
  fuller name now reach the judge through the token-subset/shared-surname lanes even when their
  embedding cosine sits well under 0.82, or coincidentally near an unrelated pair's cosine.
- One shared lane implementation for both ingest-time and retroactive recall. A future fix or new
  lane in `reconcile/candidates.py` now benefits both call sites instead of only the retroactive
  path, and the existing reconcile regression harness continues to exercise the same predicates
  the ingest path now relies on.
- Makes explicit, and now enforces in code, the same asymmetry ADR 0010 already established for
  the *merge* direction (similarity recalls but never auto-merges alone): similarity, in either
  direction, is a recall signal only. It cannot decide "same," and after this ADR it cannot decide
  "distinct" either.

**Costs / risks**

- **More merge-judge calls per ingest batch.** This is a real cost on a single-slot local model.
  The embedding-band side of recall is still bounded by the existing top-`JUDGE_K` band-neighbour
  limit; the added lexical lanes are pairwise over same-type candidates within one batch-or-store
  comparison scope, so their cost scales with how many same-type candidates one `remember()` call
  considers, not with the size of the whole graph. This ADR does not introduce a new cap on that
  count. If lexical-lane pair volume becomes a problem in practice, bounding it — for example
  reusing the reconcile side's oversize-cluster split — is future work, not solved here.
- **The judge's verdict prompt is now the sole precision mechanism for every pair any lane
  proposes.** A regression in the judge is no longer partially masked by a conservative blocking
  stage that kept marginal pairs from ever reaching it.
- **Two tests in `tests/resolve/` that encoded "a low cosine score vetoes a judge merge" are being
  inverted** as part of this change, landing concurrently. They asserted exactly the conjunctive
  behavior this ADR rejects.

## Known gaps deliberately not taken now

- **Phonetic encoding** (Double Metaphone, or Beider-Morse for non-Anglo names) would catch
  spelling/transliteration variants that share no token and no Unicode-normalizable diacritic —
  a case none of the lanes above cover. Deferred: it adds a new tunable false-positive surface
  across the languages a vault may mix, and should be evaluated against measured production
  behavior of the lanes landing in this ADR before adding another one.
- **A nickname gazetteer.** `generate_candidate_clusters` in `reconcile/candidates.py` already
  accepts a `nickname_gazetteer` parameter for exactly this purpose, but no caller anywhere in the
  codebase populates it today. Building and maintaining a nickname table (necessarily
  per-locale) is separate curation work this ADR does not take on.

## Rejected alternatives

- **Raise or otherwise retune the single 0.82 embedding threshold.** Rejected: the evidence above
  shows cosine carries no discriminating signal at all for short personal names — a true-duplicate
  pair and an unrelated pair can sit at the same cosine, so no single threshold value separates
  them. Tuning a signal that isn't there does not fix a recall gap.
- **Ship phonetic matching and/or a nickname gazetteer now, alongside the lexical lanes.**
  Rejected as scope creep on this decision: reusing the already-gated lexical lanes closes the
  demonstrated miss without introducing a new tunable or a new false-positive surface; both are
  recorded above as deliberate future work instead.
- **Keep ingest-time blocking conjunctive and rely on periodic `kg reconcile` to clean up
  misses.** Rejected. Incremental entity resolution, by construction, makes each decision on less
  evidence than a full batch pass and requires a periodic batch re-resolution pass to correct it
  (Whang & Garcia-Molina 2014) — which is exactly what `kg reconcile` (ADR 0008) already is.
  Reconcile and ingest-time resolution are complementary, not substitutes for each other: a pair
  the judge never saw at ingest time still determines what the graph and the review queue look
  like in between reconcile runs, and reconcile is a periodic correction, not a live substitute
  for giving ingest-time blocking a fair shot at recall in the first place.

## Relationship to ADR 0008, 0010, 0022, and 0040

ADR 0008 established that periodic, off-graph reconciliation is the batch re-resolution pass an
incremental pipeline needs. ADR 0010 built the disjunctive lexical recall lanes this ADR imports
into the ingest path rather than re-deriving. ADR 0022 already framed Marginalia's review queue as
the Fellegi-Sunter clerical-review band and justified operating the judge at a higher-recall point
because a bad merge is recoverable via rebuild from the markdown trust root — this ADR extends
that same recall-first posture one stage earlier, to blocking itself. ADR 0040 owns the
`exact_surface_key`/`discovery_surface_key` contract this ADR extends (exact stays sensitive,
discovery now also folds diacritics) and the cross-type identity adjudication this ADR retargets
onto the discovery key.

## Addendum (2026-09-14): the judge-call-volume risk materialized; LEXICAL_ALIAS_CAP added

The "Costs / risks" section above flagged that ingest-time lexical-lane recall could raise
merge-judge call volume, did not cap it, and named the reconcile side's oversize-cluster split
(`RECONCILE_CLASS_CAP` / `_split_oversize`) as the future-work escape hatch "if lexical-lane pair
volume becomes a problem in practice." It became a problem within the same release. On a live
76-document ingest, `step=judge` counts for the SAME first 3 documents rose from 25 (cycle 2,
before this ADR's lane widened) to 118 (cycle 3, after), a 4.7x increase, and the third document
sat in the `committing` stage for 17+ minutes against roughly 6 minutes/document previously. Root
cause: the lexical lane in `judge_against_store` (`src/marginalia/resolve/__init__.py`) fed an
UNCAPPED `store.list_nodes()` scan — every additional same-type committed node the widened
disjunctive rule matched cost one more LLM judge call, so volume grew with STORE SIZE rather than
candidate count, exactly the failure mode this ADR's risk note anticipated.

The fix taken is the one already named in that risk note: mirror
`reconcile/candidates.py`'s own answer to the identical problem for its cluster-size budget. A new
module constant, `LEXICAL_ALIAS_CAP` (5), bounds how many of the lexical lane's RECALLED matches
are offered to the judge per candidate in `judge_against_store`. Each match is scored
(`_lexical_alias_score`, which delegates non-suffix lanes to `_structural_alias_score`):
`IDENTITY_EDGE_SCORE` for a deterministic containment/surname/suffix match, the raw Jaro-Winkler
value for the fuzzy lane — mirroring `reconcile/candidates.IDENTITY_EDGE_SCORE`'s own reasoning
that a token-identity match is stronger evidence than an edit-distance estimate, so it must rank
above a merely-similar spelling. Matches are ranked strongest-first with a deterministic
tie-break (node id, ascending) and only the top `LEXICAL_ALIAS_CAP` are kept. Recall itself is
untouched: `_lexical_alias_score` still admits every pair any lane proposes, no matter how many —
exactly the recall-is-never-vetoed contract this ADR requires — the cap sits strictly downstream,
bounding judge-call volume rather than blocking recall. Combined with the embedding lane's
existing `JUDGE_K` (3), a candidate's total judge budget in `judge_against_store` is now
`JUDGE_K + LEXICAL_ALIAS_CAP` = 8 per candidate, independent of store size.
`judge_within_batch`'s JUDGE-CALL VOLUME needed no change: it already bounds calls per candidate
with its pre-existing `scored[:k]`, over a population limited to kept batch survivors rather than
the whole store, so it cannot reproduce this defect. Its RANKING is coarser than the fix above and
was deliberately left alone (out of scope for this defect): it still treats any lexical-alias hit,
fuzzy or identity-grade, as `score = 1.0` via the plain `_is_lexical_alias_pair` boolean rather than
`_lexical_alias_score`'s graded value, and its final sort has no explicit tie-break, so a tie at 1.0
resolves by list order. Neither trait is new — both predate this ADR's lane widening — and aligning
`judge_within_batch`'s ranking with `judge_against_store`'s is a reasonable follow-up, not something
this fix's problem statement (uncapped judge-call volume) requires.

Regression coverage: `tests/resolve/test_merge_judge.py::TestLexicalAliasCapBudget` seeds a store
with 50 same-type nodes that all lexically match one candidate and asserts the judge is called at
most `LEXICAL_ALIAS_CAP` times (not once per store node), plus a companion test proving a genuine
short-name/full-name pair still reaches the judge — ranked ahead of 50 weaker matches — once the
cap binds.

## Addendum (2026-09-15): a surname is a word — the numeric-token blocking collision

The shared-surname lane reused by D2 read the trailing token of a title as a surname:
`_surname` in `src/marginalia/reconcile/candidates.py` returned `toks[-1]` for any title with
two or more tokens. For a person name that token is a surname. For an identifier or a dated
title it is a number, so `_surname("TASK-01") == "01"`, `_surname("Sprint 01") == "01"`, and
`_surname("ECD 2026") == _surname("DARF Unificado jul/2026") == "2026"` — every numbered or
dated item in a corpus welded into one blocking family by a token that carries no identity at
all.

That would be tolerable if the lane were merely noisy recall, which is what this ADR's own
"blocking is recall" contract invites. It is not, because of where the lane's output goes.
`_structural_alias_score` returns `IDENTITY_EDGE_SCORE` (1.0) for a shared surname, so the
collisions arrive tied with genuine token-containment matches, and the `LEXICAL_ALIAS_CAP`
ranking added in the 2026-09-14 addendum above breaks a tie at 1.0 on node id, ascending.
Measured on a live 386-node graph: `DARF Unificado jul/2026` drew 15 candidates all tied at
1.0, nearly all of them pure numeric-token collisions (`ECD 2026`,
`Duplicidade NFS-e 32/33 (maio/2026)`, `Complementares Q2/2026`), and the cap then kept five of
them by id hash. The true match was crowded out and never reached the judge. The same graph
carries the fragmentation this produced: 23 `TASK-NN` numbers stored as two nodes each (a bare
`TASK-NN [Concept]` beside the full `TASK-NN: <title> [Activity]`), the same split for
US-02/03/04/09, and a three-way DARF split across two types.

`_surname` now returns `None` when the trailing token contains no letter. A surname is a word;
an all-digit trailing token is an identifier or a date fragment, and it is not evidence of
anything.

This is a precision fix to a blocking SIGNAL, not a blocking-time veto, and the distinction
matters for anyone reading it against this ADR's "blocking must never veto a pair a lane would
propose" rule. No pair a genuine lane proposes is lost: a true short-form/long-form pair rides
`token_subset` / `token_subset_unordered` on its shared WORD tokens, both untouched here, so
`TASK-01` / `TASK-01: Add is_recurrent Property to Expense Model` and `darf-unificado` /
`DARF Unificado jul/2026` are still admitted, as are `Casey Lee` / `Dr Casey Lee` (alphabetic
surname) and every short-given-name pair from the evidence table above. The pairs that stop
being admitted are ones no other lane ever proposed — their Jaro-Winkler scores measure 0.39 to
0.68, far below `JW_STRONG` (0.90) — so the surname lane reading a number was the only thing
admitting them.

Regression coverage: `tests/reconcile/test_candidates.py::test_surname_is_a_word_not_a_trailing_number`
pins `_surname` directly and
`::test_numbered_titles_are_not_welded_by_a_shared_trailing_number` pins the clustering
consequence (the true `TASK-01` variant pair still co-clusters; the numeric collisions do not
cluster at all). On the ingest side,
`tests/resolve/test_merge_judge.py::TestLexicalAliasRecall::test_identifier_variants_still_reach_the_judge`
pins what must stay admitted and `::test_shared_trailing_number_is_not_a_shared_surname` pins
what must not.

## Sources

- Fellegi, I. P. & Sunter, A. B. (1969). "A Theory for Record Linkage." *Journal of the American
  Statistical Association*, 64(328). DOI: 10.1080/01621459.1969.10501049.
- Christen, P. (2012). *Data Matching: Concepts and Techniques for Record Linkage, Entity
  Resolution, and Duplicate Detection*. Springer.
- Elmagarmid, A. K., Ipeirotis, P. G. & Verykios, V. S. (2007). "Duplicate Record Detection: A
  Survey." *IEEE Transactions on Knowledge and Data Engineering*, 19(1).
- Papadakis, G., Skoutas, D., Thanos, E. & Palpanas, T. (2020). "Blocking and Filtering Techniques
  for Entity Resolution: A Survey." *ACM Computing Surveys*, 53(2).
- Hernández, M. A. & Stolfo, S. J. (1995). "The Merge/Purge Problem for Large Databases."
  Proceedings of ACM SIGMOD.
- Winkler, W. E. (1990). "String Comparator Metrics and Enhanced Decision Rules in the
  Fellegi-Sunter Model of Record Linkage." Proceedings of the Section on Survey Research Methods,
  American Statistical Association.
- Whang, S. E. & Garcia-Molina, H. (2014). "Incremental Entity Resolution on Rules and Data." *The
  VLDB Journal*, 23(1).
