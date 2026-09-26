# ADR 0010: Entity Reconciliation — Recall + Judge + Corroboration Overhaul

**Status:** Accepted; P1–P5 implemented, P6 deferred  **Date:** 2026-06-05  **Deciders:** Alex Rivera, Marginalia core  **Builds on:** ADR 0007, ADR 0008 (+2026-06-05 addendum)

**Lifecycle addendum — 2026-07-13.** The CI recall harness, candidate-recall lanes,
corroboration/veto logic, judge prompt/thinking changes, and real-judge band harness
shipped. P6 abstention/shared-neighbour experiments remain explicitly gated future
product work, not incomplete `0.0.40` release work.

## Context

ADR 0008 made reconciliation off-graph and reversible, and shipped a per-variant corroboration partition that already governs auto-merge precision. A live read-only `propose` run on the NX test vault (2026-06-05, 95 `Agent` nodes → 28 candidate clusters, judge `glm-5-turbo`, `enable_thinking=false`; evidence at `.scratchpad/reconcile-findings/propose-agents-2026-06-05.json`) reframes where the real defects sit. The headline is not a single failure mode but two opposite ones, and the cheap reading of each is wrong.

**The over-merge is mostly already contained — do not re-solve it.** Cluster `89140867` `{NX Lab, Tamola, Luke Gray, Luke Gray Lab}` was judged `same@0.95` under the default **pairwise** path (`use_cluster_judge:false`) on raw titles. That is a *judge verdict*, and **no apply has run on this daemon**. The shipped gate `is_high_confidence` (`apply.py:133-145`) requires `len(corroborated_ids) >= 2`, where `corroborated_ids` is built per-variant by `_variant_corroborated` (`propose.py:123-130`). `NX Lab`↔canonical `Luke Gray Lab` has `jaro_winkler≈0.41` (no lexical match) and no relational neighbour overlap → it is **not** in `corroborated_ids` → it is queued, never folded into the authority record. `Tamola` likewise. So the cluster *would* auto-merge to at most `{Luke Gray, Luke Gray Lab}` today. The judge over-commits; the ADR-0008 partition already absorbs it. This overhaul **hardens** that partition (adds a veto, cleans the cluster) but does not claim to newly prevent the over-merge.

**The under-merge is the real, unaddressed gap.** Two of three MUST-MERGE gold pairs never reach the judge at all:

- `Nivod` and `Ari Nivod` land in *different* candidate clusters. `token_subset("Ari Nivod","Nivod")` is `False` because the lane requires a shared *first* token (`candidates.py:161`: `a_toks[0]==b_toks[0]`), and `jaro_winkler("Nivod","Ari Nivod")=0.0` (word-order). The subset lane instead paired `Ari Nivod`↔`Ari`, the useless direction. They are never co-judged.
- `Alex Rivera` and `Alex Morgan` are not even embedding-near (below `RECONCILE_RECALL_FLOOR=0.65` on the live vault), share no surname, and neither title subsets the other. No lane fires.

**The cluster is also polluted on the merge side.** `Tovrin`/`Tovrin Kalia` (correctly `same@0.95`, `corrob=lexical`, would auto-merge) swept in `Tovereign` via the lexical lane (`jaro_winkler("Tovrin","Tovereign")=0.922 ≥ 0.90`). A short-token fuzzy match welded an unrelated word into a true-merge cluster.

**The measurement signal is itself partly an artifact.** 26 of 28 clusters returned empty/truncated `reason` — but `_pairwise` only records `verdict.reason` inside the surviving merge branch (`propose.py:299-302`), so every *distinct* verdict's reason is discarded. The true judge-disengagement rate on name-only clusters is not yet observable.

The integrated conclusion: **recall is the dominant defect (under-merge + un-judged gold pairs); auto-merge precision is largely shipped.** The overhaul widens recall without losing precision, cleans polluted merge clusters, unifies corroboration into one graded signal that can both justify and veto, and instruments the judge so the next iteration can be tuned on a real signal rather than an artifact.

## Decision

Five axes, integrated so corroboration is **one mechanism**, clustering topology is **consistent** with pairwise judging, and every MUST-STAY-DISTINCT guard is owned by the apply-time partition rather than asserted at the wrong layer.

### Axis 1 — Candidate recall & de-pollution (`candidates.py`)

Two surgical recall fixes plus one precision fix. No honorific gazetteer (rejected — see Alternatives; `"ari"` is not a standard honorific and classifying it that way overfits one gold case).

- **Order-insensitive token-subset (`token_subset_unordered`).** A new predicate: proper subset of token *sets* **plus** a shared *distinctive* token (≥4 chars, not a known generic org/role word), dropping the shared-first-token requirement. `{nivod} ⊂ {ari, nivod}` qualifies (shared `nivod`) → `Nivod`/`Ari Nivod` land in one component and are judged as a pair. The ordered `token_subset` stays for the first-name/full-name chain; the unordered predicate is additive. Verified safe for the precision guard: `{aler}` is not a token-element of `{mualer, disija}` (`aler ≠ mualer`), so Aler/Mualer is never paired.
- **Length-gated lexical strong edge (de-pollution).** Keep `JW_STRONG=0.90` as the recall/ordering score, but only let a *short-token* fuzzy match form a cluster edge when token length clears a floor. `jaro_winkler("tovrin","tovereign")=0.922` on a 6-char token falls below the gate → `Tovereign` is no longer welded into the `Tovrin` component. `Tovrin`/`Tovrin Kalia` still pair via the (unordered) subset lane on the shared token `tovrin`, so the true merge is unaffected.
- **Optional shared-neighbour recall lane (WEAK).** A node-anchored blocking lane keyed on common neighbours (built from `list_edges(src=)/list_edges(dst=)` only — never a full `list_edges()` scan). This is the *only* recall path that can co-consider `Alex Rivera`/`Alex Morgan` (they share no name or embedding signal). It is recall-grade: it co-considers a pair, it does not decide it.

Minimal sketch: add `token_subset_unordered(a,b)` near `token_subset` (`candidates.py:140`) and call it as an additional `elif` in the lane loop; gate the lexical strong edge by token length where the surname/JW lane records its pair; add the shared-neighbour lane as a WEAK pair source. All reads are node-anchored; the stage returns value objects and writes nothing.

**What this axis does NOT do:** it does not make the judge reject NX Lab/Tamola. Decomposing a polluted cluster into 2-node candidates hands the judge the *same* raw-title comparison it already approved at 0.95. The distinct outcome is owned by Axis 3 at apply time, not by clustering.

### Axis 2 — Judge robustness (`resolve/__init__.py`, `propose.py`)

Sequenced, with instrumentation strictly before any retry/abstain policy.

- **(M1, ship first) Surface distinct reasons.** `_pairwise` records `verdict.reason` for *every* member it judges, merge and distinct, so judge engagement becomes observable. This converts the "26/28 empty reason" artifact into a real, measurable signal. Nothing else in this axis ships until M1's data exists.
- **(M3) Two scoped prompt exemplars** in `_VERDICT_SYSTEM` (`resolve/__init__.py:427-440`):
  - SAME (containment): a bare name that is a token-subset of a fuller name, with no conflicting surname, is SAME (e.g. `Tovrin`/`Tovrin Kalia`). Explicitly **not** "two different full names sharing a first name" — that line stays DISTINCT (it is load-bearing and prevents the documented same-first-name over-merge).
  - DISTINCT (team/role vs unrelated person): a team or org-unit vs a person bearing a related name is DISTINCT (e.g. `NX Lab` team vs `Luke Gray` person). The existing exemplar only covers company-vs-its-own-subteam (`NX`/`NX Lab`).
- **(M4) Forward `enable_thinking` in `_try_cluster_judge`** (`propose.py:321-326` passes only `temperature`/`max_tokens`). The optional cluster-judge path currently runs at provider-default thinking, re-triggering the non-committal failure ADR 0008 fixed for the pairwise path.
- **Cluster-prompt evidence parity (2026-07-14 corrective hardening).** The optional
  compare/select prompt includes each candidate's description, bounded to 400 characters just
  like the canonical description. Pairwise judging already receives both entity descriptions;
  omitting candidate descriptions from the cluster path discarded distinguishing evidence. This
  changes neither thresholds nor the pairwise fallback.
- **(M2, deferred, gated on M1 measurement) Reason-required abstain.** A third disposition: an empty/whitespace/unparseable verdict on a gold-class pair is non-engagement in *both* directions, routed to the review queue rather than silently collapsed to distinct. **This is deferred, not shipped in the first phase**, because (a) the disengagement rate is unmeasured until M1 lands, and (b) a naive reason-forcing retry can manufacture a confident invented `same` on a name-only pair — exactly the 0.95 failure mode. If M2 ships, it carries a mandatory apply-layer fix: `apply.py:176-178` currently does `if not verdict.same: skipped; continue` **before** any queue write, so an abstain (`same=False` with review members) would silently drop. The skip branch must be guarded to route `same=False`-with-review-members to a queue write.

Judge config (`config/_vault.py:339`: `temperature=0.2, enable_thinking=false`) is measured and correct (greedy `0.0` is banned for the Qwen3 family); it is unchanged.

### Axis 3 — Corroboration as the deciding rail, unified into ONE graded signal

Corroboration becomes the single apply-time precision rail and absorbs the "evidence" concept from the hard-case axis. There is **one** corroboration mechanism, graded into three tiers, replacing the boolean `_variant_corroborated`.

The unification turns on splitting the current `_lexical_match` (`propose.py:107-120`) by *determinism*, not by lane name:

| Evidence | Source | Strength |
|---|---|---|
| **identity-grade** | deterministic token containment (exact token-subset sharing a distinctive token) **or** email-handle decomposition equality (`a_tok == b_title`, `propose.py:114-119`) **or** shared strong Identifier (future, gated on ingest) | **sufficient** for auto-merge |
| **relational** | neighbour-set Jaccard ≥ `REL_JACCARD` (0.25), computed from `list_edges(src=)/list_edges(dst=)` only | **sufficient** for auto-merge |
| **fuzzy-supporting** | `jaro_winkler ≥ 0.90` with **no** shared distinctive token | **supporting only — never sufficient alone** |

This is the synthesis that satisfies every adversarial review at once, verified against the gold cases:

- `Tovrin Kalia`↔`Tovrin` share the token `tovrin` → identity-grade → stays corroborated → **merges** (a MUST-MERGE that passes today via `corrob=lexical` is preserved — we do **not** adopt a blanket "lexical never sufficient", which would regress it to queued).
- `Tovereign`↔`Tovrin` share no token; `jaro_winkler=0.922` is fuzzy-supporting only → **not** sufficient → dropped from the auto subset (precision win).
- `taylor.nguyen`↔`Taylor Nguyen` is handle-decomposition equality → identity-grade → **stays auto** (the documented thin-handle invariant, `propose.py:16-18`, is preserved).
- `Luke Gray Lab`↔`Luke Gray` share `luke`+`gray` tokens → identity-grade → merges; `NX Lab`/`Tamola` share neither token nor neighbour → no evidence → queued.

**The veto.** Add a negative tier: if either node carries a `_DISTINGUISHING_PREDICATES` edge (`distinguished_from / alias_of / different_from / not_same_as`, `resolve/__init__.py`) toward the other, corroboration is **negative** and the variant is *removed* from the cluster regardless of the judge verdict. This is the new power — corroboration can subtract a member the judge wrongly merged, not only narrow a merge.

The gate stays exactly where ADR 0008 put it: `is_high_confidence` requires `same ∧ confidence ≥ RECONCILE_AUTO_CONFIDENCE ∧ len(corroborated_ids) ≥ 2`, and the auto-merge `AuthorityRecord` is minted over `corroborated_ids` only (`apply.py:179-201`). The change is in how `corroborated_ids` is built: identity-grade or relational survives, fuzzy-only is excluded, negative is removed.

Minimal sketch: refactor `_variant_corroborated` into a graded `_variant_evidence(store, canonical, variant) -> {"identity","relational","fuzzy","negative"}`; `corroborated_ids` keeps variants whose set intersects `{identity, relational}` and excludes any with `negative`. All reads node-anchored; all writes stay `authority.upsert`/`queue.enqueue`.

### Axis 4 — Different-surname coreference (the Alex case): honest scoping

The hardest pair, `Alex Rivera == Alex Morgan`, has **no sound auto-merge path under this design**. It is below the embedding floor, shares no surname, neither subsets the other, and `_VERDICT_SYSTEM` correctly rules "two different people who share a first name" DISTINCT. The only signal that could justify the merge is a shared strong Identifier (same email/handle) — and the Identifier ingest path is **structurally dead**: `ingest/markdown.py` emits zero `Identifier` references and no pack defines an `Agent → Identifier` predicate. There is no data for the decisive arm to read.

So the honest deliverable for Alex is: the shared-neighbour recall lane (Axis 1) **co-considers** the pair and surfaces it to the **review queue with its relational context attached** (shared colleagues/projects), never silently dropped and never auto-merged. **Auto-merging Alex is future work, gated on an Identifier ingest path** (Open Question Q1). The corroboration-as-recall-lane idea (pair any two Agents touching a common Identifier) is deferred to that same future work.

### Axis 5 — Regression eval harness (`tests/reconcile/`)

A real-fixture, real-embedder gate, mirroring `tests/eval/golden.py`. Reframed from "no gate exists" — `tests/reconcile/test_candidates.py` already asserts gold outcomes at the *candidate* layer with synthetic 2-vector stand-ins. The gap is an **end-to-end gate on the real fixture with the real embedder**, plus a judge-stability band.

- **Tier A (CI-safe, no LLM):** run `generate_candidate_clusters()` against a checked-in NX-content fixture with the real fastembed embedder. Assert co-cluster (recall) and exclusion (precision). **Reserve hard-asserts for string-deterministic cells** (Tovrin token-subset; NX-Lab surname collision). **Use tolerance bands for embedding-dependent cells** (Alex, Aler/Mualer, Tamola) — fastembed cosines drift cross-platform, so exact-membership hard-asserts on embedding outputs are brittle (the sibling recall-quality harness already uses a tolerance gate for this reason). Precondition: assert embeddings are non-null on gold nodes before scoring, or every embedding-dependent assertion silently degrades.
- **Tier B (laptop-only, real judge):** run `apply_reconciliation` K times under the shipping judge config; score *rates* (must-merge `same`-rate ≥ band, must-distinct ≤ band) and a hard `== 0` auto-merge guard on NX Lab/Tamola across all K, on both judge paths. **Fresh `authority`/`queue` side-files per K iteration** (an `upsert` persists; shared side-files corrupt the rate denominator).
- **Red-aware glue:** GREEN-today assertions are hard gates; RED-today gaps are `xfail(strict=True)` so they XPASS loudly the instant a sibling axis closes them. **Alex is measure-first, not pre-pinned RED** — its live cosine is genuinely <0.65 while `test_candidates.py:73` asserts a hand-built 0.7177 co-cluster; these are different quantities, and the redness must be measured on the fixture before any xfail reason is written.

No production-code edits; pure test/fixture additions under `tests/`.

## Gold-set walkthrough

| Gold case | Recall (Axis 1) | Judge (Axis 2) | Corroboration / apply (Axis 3) | Net |
|---|---|---|---|---|
| **Nivod == Ari Nivod** (merge) | `token_subset_unordered` shares `nivod` → co-clustered as a pair (today: separate clusters, never judged) | SAME-containment exemplar → SAME with a real reason | shared token `nivod` = identity-grade → corroborated | **Merges** (was: never considered) |
| **Tovrin == Tovrin Kalia** (merge) | subset lane on shared `tovrin`; `Tovereign` no longer welded in (length-gated lexical) | already `same@0.95`; exemplar makes it principled | shared token `tovrin` = identity-grade → corroborated; `Tovereign` is fuzzy-only → dropped | **Merges clean**, `Tovereign` queued separately |
| **Alex Rivera == Alex Morgan** (merge) | shared-neighbour lane co-considers (no name/embedding signal) | "different full names, same first name" → DISTINCT on names alone | no shared token, no Identifier (ingest dead) → no identity evidence → **queued with relational context** | **Queued for review; auto-merge is open (Q1)** — not claimed to merge |
| **Aler Dalvic != Mualer Disija** (distinct guard) | no lane fires (`aler` not a token of `{mualer,disija}`; JW low; subset false) | n/a | n/a | **Stays distinct** |
| **{NX Lab, Tamola, Luke Gray, Luke Gray Lab}** → ≤ {Luke Gray, Luke Gray Lab} | length-gate + token lanes; NX Lab/Tamola reach the cluster only weakly | judge may still over-commit `same@0.95` (this axis only nudges) | NX Lab↔canonical: no shared token, no neighbour overlap → not in `corroborated_ids` → **queued, never folded**. Luke Gray Lab: shared tokens → folded | **Collapses to ≤ {Luke Gray, Luke Gray Lab}** (guard owned by the shipped partition, hardened by the veto) |

The through-line: Axis 1 fixes co-consideration (Nivod, Alex), Axis 1's length-gate cleans the Tovrin pollution, and the apply-time partition (Axis 3, already shipped + now graded/vetoed) holds every distinct guard. Alex is the one case the gold set needs a capability (Identifier ingest) not yet justified — it is an open question, not a claimed merge.

## Precision invariants

These hold in all cases and are non-negotiable:

1. **Off-graph only (ADR 0008).** Reconciliation writes ONLY `.marginalia/authority/index.json` and `.marginalia/reconcile/queue.json`. No path calls `store.add_node` / `store.add_edge`. The `apply_reconciliation` unit-spy asserting 0 graph writes stays green. A merge is an off-graph equivalence record folded at query time, not a graph mutation.
2. **No bulk in-place graph migration (ADR 0007).** Nothing here re-extracts or backfills a populated graph. Any full re-extract is `kg rebuild` into a fresh graph + atomic swap.
3. **Reads stay up; single in-process writer.** No path opens a second handle/process on a daemon-held vault.
4. **Node-anchored reads only.** Corroboration and degree use `list_edges(src=)/list_edges(dst=)` (the filtered, node-anchored form). The full-scan `list_edges()` mis-projects `e.src`/`e.dst` and is never used. There is no `store.neighbors()` method; a convenience wrapper is optional, not a correctness fix. Corrupted graphs are an ADR-0007 *rebuild* precondition — no read recovers truth on a corrupted graph; the NX vault is healed (1227→0, 2026-06-04).
5. **Propose is read-only; apply is off-graph and reversible** via `authority.remove(cluster_id)` / unmerge.
6. **Fuzzy similarity recalls but never auto-acts alone.** Auto-merge requires identity-grade or relational evidence per variant. A short-token fuzzy match (`jaro_winkler ≥ 0.90`, no shared token) is supporting only.
7. **The auto-merge subset is the corroborated subset.** An uncorroborated co-member of a judged cluster is queued, never folded; a vetoed member is removed.

## Phased rollout

Each phase is independently shippable and verifiable; cadence mirrors ADR 0008/0009.

- **P1 — Eval harness first (Axis 5, Tier A + judge instrumentation M1).** Land the real-fixture deterministic gate and `_pairwise` reason-surfacing. This is the baseline: it makes recall and judge-engagement measurable before any behavior changes. Verifiable: Tier A green/red map matches the live propose run; distinct reasons now appear.
- **P2 — Recall (Axis 1: `token_subset_unordered` + lexical length-gate).** Nivod/Ari Nivod co-cluster; Tovereign de-welded. Verifiable: Tier A xfail(strict) on Nivod XPASSes and flips to a hard gate; Tovrin cluster no longer contains Tovereign.
- **P3 — Corroboration unification + veto (Axis 3).** Replace boolean corroboration with the graded `_variant_evidence`; add the distinguishing-edge veto. Verifiable: `_variant_corroborated` regression tests (taylor.nguyen stays identity-grade; Tovereign drops to fuzzy); NX Lab/Tamola never enter `corroborated_ids`.
- **P4 — Judge prompt exemplars + cluster-path `enable_thinking` (Axis 2: M3, M4).** Verifiable: Tier B `same`-rate bands; cluster-judge path no longer disengages.
- **P5 — Tier B band gate (Axis 5).** K-run rate bands + NX Lab/Tamola `== 0` guard, fresh side-files per K. Laptop-only.
- **P6 (deferred, gated) — Judge abstain (Axis 2: M2) + shared-neighbour recall lane.** Only after P1's instrumentation gives a real disengagement rate. Carries the `apply.py:176-178` skip-branch guard fix. The Identifier-based Alex auto-merge stays out of scope until ingest lands (Q1).

## Evaluation

The regression harness (Axis 5) is the gate. Tier A pins string-deterministic recall/precision cells as hard asserts and embedding-dependent cells as tolerance bands, all against a checked-in NX-content fixture with the real embedder. Tier B pins judge-stability rate bands and the irreversible-path `== 0` auto-merge guard with per-K side-file isolation. Strict-xfail converts every closed gap into a hard gate automatically, so the overhaul can neither silently regress a guard nor silently fail to land a fix. The `apply_reconciliation` zero-write spy continues to guard the off-graph invariant independently.

## Risks & open questions

- **Q1 (gates Alex):** Alex Rivera == Alex Morgan cannot auto-merge without a shared Identifier, and the `Agent → Identifier` ingest path emits nothing today. Does the gold harness score "queued" as a pass for a MUST-MERGE pair? If it demands auto-merge, Alex *fails until Identifier ingest exists*. This is the cleanest example of the gold set needing a capability not yet justified — it is open, not claimed.
- **Q2:** `REL_JACCARD=0.25` was tuned as a *supporting* signal; promoting relational to *sufficient-for-auto-merge* may need a higher floor (e.g. 0.4) so a person and a same-org team don't auto-merge on incidental neighbour overlap. Calibrate on the gold set.
- **Q3:** Identifier-as-hub over-merge — if a shared `@domain` or role mailbox (`info@`, `support@`) were modeled as one Identifier node, it would weld a whole org. When the Identifier arm lands, require a *specific* identifier (full email/handle), never a domain or role account.
- **Q4:** M2 retry policy — one reason-forcing retry can manufacture a confident `same` on a name-only pair. Size and shape it only after M1's measured disengagement rate; do not tune blind.
- **Q5:** Whether the optional cluster-judge path should also get reason-required/abstain after M4, or stay the strictly-optional fast path with pairwise fallback. Default-pairwise is untouched; defer.

## Alternatives considered

- **Honorific/affix gazetteer normalization** (strip `ari`, `lab`, etc. in a shared tokenizer) to fix Nivod and de-pollute NX Lab. Rejected: `"ari"` is not a standard honorific, so encoding it reverse-engineers one gold case; and a curated suffix list risks corrupting a real name that collides with a suffix word. `token_subset_unordered` recovers Nivod without any gazetteer, and the length-gated lexical edge de-pollutes Tovrin without one.
- **Cluster-level judging as the fix for the over-merge.** Rejected: the over-merge already happened under *pairwise*; topology is not the lever. The default stays pairwise; the cluster path is an optional precision optimization with a pairwise fallback.
- **"Lexical corroboration never sufficient"** (blanket demotion). Rejected: it regresses the documented thin-handle invariant (taylor.nguyen) and converts Tovrin == Tovrin Kalia, a MUST-MERGE passing today, into a queued item. The deterministic-vs-fuzzy split keeps token/handle containment sufficient while demoting only short-token fuzzy matches.
- **Treating the 2-node clustering decomposition as a precision fix.** Rejected: handing the judge an isolated `NX Lab`↔`Luke Gray Lab` pair presents the same raw-title comparison it already approved at 0.95. Precision is owned by the apply-time corroboration partition, not by clustering.
- **Shipping judge abstain in the first phase.** Rejected for sequencing: the disengagement rate is unmeasured until M1 surfaces distinct reasons, and abstain without the `apply.py` skip-branch fix silently drops the very pairs it is meant to rescue. M1 ships first; abstain is gated on its data.

---

## Addendum — 2026-06-05: real-vault validation (offline `propose` on a `cp -R` copy)

After P1–P5 landed (all synthetically green; Tier B real-judge gate passed on a synthetic `InMemoryStore`), the fix was validated against the **actual NX vault** — the data that started this — via the sanctioned offline path: `cp -R` the live vault, then `kg reconcile propose <copy> --type Agent` (read-only, new code, judge = `qwen3.6-35b`). This caught what the synthetic gate structurally could not. The duplicated `kg kg` spelling in an earlier copy of this record was a documentation typo, not the command that ran.

**Confirmed fixed on real data:**
- **`Tovrin` / `Tovrin Kalia` → `same` 0.95, clean.** `Tovereign` is de-welded (P2 length-gate); the M3 SAME-containment exemplar fired (judge reason cites *"'Tovrin' is a token-subset of 'Tovrin Kalia'"*). Auto-merges. M1 working (real reasons on distinct verdicts). `Aler`≠`Mualer` guard holds.

**Bug the synthetic gate missed → fixed here (`IDENTITY_EDGE_SCORE`):**
- The 0.65 embedding floor fuses **80 of 95 Agents into ONE oversize component** on the real vault. `_split_oversize` (the `RECONCILE_CLASS_CAP=8` cap) packs **strongest-score edges first**, and the `subset_unordered` `Nivod`↔`Ari Nivod` bridge scored `jaro_winkler=0.0` (word-order variants always do) — so it was the *weakest* edge and was dropped, severing the pair into different clusters. The synthetic Tier A used a string-only store (no embeddings → no blob → no cap pressure), so it never exercised this. **Fix:** deterministic token-containment edges (`token_subset`, `token_subset_unordered`, handle) now score `IDENTITY_EDGE_SCORE=1.0`, above the embedding band, so the cap keeps the identity bridge and sheds embedding pollutants. Regression test: `tests/reconcile/test_candidates.py::test_cap_keeps_identity_bridge_over_embedding_pollutants` (load-bearing — reproduces the bug at score 0.0).

**Still open after the fix — `Nivod`/`Ari Nivod` recall ≠ merge:**
- With the cap fix the pair now **co-clusters**, but the **pairwise** judge (canonical `Nivod`, degree 69) still returns **distinct**: `"Nivod"` vs `"Ari Nivod"` is genuinely name-ambiguous — the bare name is the *trailing* token (is `Ari` a surname or an honorific/role?), unlike the M3 exemplar's *leading*-token `Tovrin`/`Tovrin Kalia`. The **cluster-judge** path (`--cluster-judge`), seeing the whole cluster at once, returns **`same` 0.85** (reads `Ari` as a role, recognises the containment) — so the pair is reachable but currently **queued**, not auto-merged. This is closer to the Alex case than Tovrin: name alone is insufficient. Open levers (P6/future, not yet decided): (a) default reconcile to cluster-judge for containment recall; (b) a corroboration-OVERRIDES-distinct path (relational evidence rescues a pairwise-distinct pair — the positive mirror of P3's veto), since a `distinct` verdict currently short-circuits before corroboration runs (`apply.py:176`); (c) a trailing-token/honorific SAME exemplar.
- **`Alex Rivera`/`Alex Morgan`** unchanged: not co-clustered (below floor, no shared token) — Q1 (Identifier ingest) stands.

**Applied to the live vault (later 2026-06-05).** The daemon was restarted onto the new code and reconcile applied via the job queue (cluster-judge): `Tovrin`/`Tovrin Kalia` and `Nivod`/`Ari Nivod` both auto-merged off-graph (Browse 95→89 Agents; node count 2322 unchanged; guards held). Then, on request, the graph was **rebuilt from zero** (`POST /api/v1/curation/rebuild` — fresh re-extraction, 2379 nodes) and reconciled again. The richer re-extraction surfaced bridging full names (`Alex Jordan Rivera Blake`) and consolidated Nivod (3 variants), Tovrin, Luke Gray; `Aler`≠`Mualer` held.

**Q1 update + a new open item (transitive merge / confirm semantics).** The reingest *advanced* Alex: the bridging full name let the reconciler cluster + judge the Alex variants `same@0.85` and queue them. But confirming the queue folded only `Alex Rivera ← {Alex Jordan Rivera Blake, Alex Rivera Blake}` (share `rivera`); `Alex Morgan` (shares only the first name `alex` with the canonical) and bare `Alex` stayed **split** — Alex went 5 nodes → 3, not 1. Two coupled limitations surfaced, both **open**:
- **Confirm respects the corroboration partition.** A human `confirm` folds only the per-variant-corroborated subset, NOT every member the human confirmed — so an explicit "these are the same" does not override the partition. Arguably wrong: human confirm is authority.
- **No transitive / chain merge.** Corroboration is pairwise against the canonical, so a bridging variant `C` (containing both surnames) that folds into `A` does not pull its surname-mate `B` in — the `A–C–B` chain breaks. This is the same shape as Nivod's recall≠merge and is the cleanest path to fully closing Q1: either make confirm fold all members, or add connected-component/transitive folding. (Tracked in cross-session memory `project_transitive_merge_gap.md`.)

(Note: `apply` is off-graph + reversible via `authority/unmerge`; a rebuild 503s reads for the whole job, unlike heal.)
