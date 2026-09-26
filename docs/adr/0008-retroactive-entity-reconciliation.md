# ADR 0008: Retroactive Entity Reconciliation — Off-Graph Authority (Option A default) + Periodic Rebuild Heal (Option B)

**Status:** Accepted
**Date:** 2026-06-04
**Deciders:** Alex Rivera, Marginalia core
**Builds on:** ADR 0004 (within-batch entity resolution), ADR 0007 (rebuild from trust root; retire bulk in-place migrations)
**Amends:** None

---

## Context

`resolve`'s entity resolution runs **pre-commit** — Tier 0 exact `(type, normalized-title)`, Tier 1 embedding band, Tier 2 LLM judge, plus within-batch (ADR 0004). It catches duplicates *as they are ingested*. It does **not** reconcile entities that have **already landed** in the graph and only later read as the same real-world thing. The NX test vault carries exactly these: a `Alex Rivera` rich record and a thin `alex` / `taylor.nguyen`-style handle that the 0.82 'similar' floor never paired (the Alex cluster sits at cosine **0.7177**, below 0.82).

The naive fix — mint `owl:sameAs` / `skos:exactMatch` edges over the live graph, or bulk-insert `Authority` hub nodes — is **forbidden by ADR 0007**. A bulk in-place edge mint into a populated Ladybug graph corrupts edge `src`/`dst` + adjacency (deterministic 0 → 1227). And `Authority` is a `Node` subclass (`core/schema/legacy.py`), so a bulk `store.add_node(Authority(...))` into a populated graph **is itself that corruption**.

So retroactive reconciliation needs a path that consolidates entities **without writing the live graph at all**.

---

## Decision

### 1. Option A (DEFAULT) — query-time off-graph consolidation

`kg reconcile apply` writes equivalence classes to a JSON side-file at `<vault>/.marginalia/authority/index.json` (`AuthorityIndex`), **never** to the graph. The read chokepoint `query.search_claims` (reached via `vault.query` → recall / ask / `/query` / MCP) folds each variant onto its canonical at query time:

- `search_claims(..., equivalence: dict[str,str] | None = None)` — a new optional param, default `None` (existing behavior byte-for-byte). When provided, a pure post-results transform groups by `equivalence.get(node.id, node.id)`, keeps one representative per class (canonical if present, else highest-scoring member), carries the class-max score, preserves desc order.
- `vault.query` loads `AuthorityIndex(...).equivalence_map()` **fresh per query** (fast missing-file path → `None` → fold is a no-op) and passes it in. `query.py` stays filesystem-free.

**Reversible by construction:** an auto-merge is undone with `AuthorityIndex.remove(cluster_id)`. Nothing in the graph changed, so there is nothing to un-corrupt.

### 2. Option B (PERIODIC) — fold into a fresh graph via `kg rebuild`

`kg reconcile heal` materializes the equivalence classes into the graph topology the ONE corruption-free way: load `alias_canonical_map()` (normalized variant-title → canonical_name), rewrite variant-titled candidates to the canonical title before extraction commit, and let the existing `reconcile_against_store` Tier-0 `(type, normalized-title)` equality + `_remap_edges` fold collapse them — all in a **fresh** graph built by `kg_rebuild` + atomic swap. **No in-place edge mint anywhere.**

### 3. Autonomy — conservative auto-merge, queue the rest

`apply_reconciliation` splits adjudicated clusters:

- **AUTO-MERGE** (off-graph `AuthorityIndex`) iff ALL: verdict `same` **AND** confidence ≥ `RECONCILE_AUTO_CONFIDENCE` (0.9; deliberately above `MERGE_CONFIDENCE` 0.8) **AND** positive corroboration **evaluated PER-VARIANT** (§8). The minted record scopes to the corroborated subset only.
- **QUEUE** the rest: `same` clusters that fall below the gate, AND the uncorroborated survivors of an otherwise-auto-merged cluster (partitioned off so one corroborated survivor never drags an uncorroborated co-member into the merge). Goes to `ReconcileQueue` (`<vault>/.marginalia/reconcile/queue.json`) for `kg reconcile review`.
- **SKIP** `distinct` verdicts.

Precision dominates recall — a false merge collapses two real entities and (for Option A) is reversible but for Option B is baked into the rebuild. So the gate is tuned conservatively.

### 4. Predicate: `skos:exactMatch`, NOT `owl:sameAs`

The materialized equivalence predicate is `skos:exactMatch` — a conservative, non-inferential equivalence appropriate to a curated knowledge base. `owl:sameAs` carries full logical identity (every property transfers, transitively) and is the wrong, over-strong claim for "these two records name the same person." Stored only as the `exact_match_pairs` field, never as a graph edge.

### 5. Authority wiring as the off-graph SKOS hub

`Authority(canonical_name, variants)` is used as a **serialization model only**: `canonical_name` → `skos:prefLabel`, each `variants[i]` → `skos:altLabel`. It is NEVER `store.add_node`'d (that is the ADR-0007 corruption). The off-graph `index.json` is the canonical equivalence hub, consumed at query time (§1) and heal time (§2).

### 6. Candidate generation defeats the prefix trap

Reconciliation targets the **5 entity primitives ONLY** (`ENTITY_TYPES` = Agent, Activity, InformationObject, Concept, Place). Support types are **never** reconciled — `type=None` scans the 5 primitives, and an explicit support `type` yields no clusters. This is load-bearing for `Claim`: a Claim is the atomic provenance unit with distinct source anchors, so deduping identical claim sentences would collapse legitimately-separate assertions (an early live `apply` without the scope guard wrongly deduped Claims like `"Riley Chen member_of NX"` ×3 — fixed in `candidates.py`).

Recall lanes union into per-type clusters; every lane only PROPOSES (none auto-merges on a string match):

1. **Widened embedding band** — cosine ≥ `RECONCILE_RECALL_FLOOR` (0.65, below the 0.82 that missed Alex at 0.7177).
2. **Lexical surname/prefix** — Jaro-Winkler ≥ `JW_STRONG` (0.90) OR shared surname token. Jaro-Winkler is an **ordering/precision** score, **NEVER a substring test** — `Aler` vs `Mualer` scores low → never paired.
3. **Token-subset** — pairs same-type entities whose token sets are in a proper-containment chain *and* share the first token: `{alex} ⊂ {alex, rivera} ⊂ {alex, jordan, rivera, blake}`. Whole-token, never a substring test, so `NX` vs `HPX` never qualifies. The shared-first-token guard keeps it off the precision traps: `{aler} ⊄ {mualer, disija}` AND first tokens differ → Aler/Mualer never paired. It only PROPOSES — an org family like `NX Legal` ⊂ `NX minors legal/privacy team` is still rejected by the conservative judge + degree-based canonical. Its real job is to raise variant-variant pair scores (JW 0.80–0.875) above weak embedding pollutant edges (≤0.74) so the cap's score-ordered packer keeps a first-name/full-name chain together instead of shredding it.
4. **Email-handle decomposition** — `taylor.nguyen` → `"taylor nguyen"` matches `Taylor Nguyen`. Needs NO edges, so thin handle entities are recalled.
5. **Optional nickname gazetteer** — off by default.

A transitive class-size cap (`RECONCILE_CLASS_CAP` = 8) splits oversize components deterministically by descending pair-score so no blob explodes the judge budget. The split **conserves members** — it bounds bucket size but never silently drops a member (leftover/orphaned nodes are reattached to their strongest under-cap host).

**Root cause this defeats (the Alex split):** the widened 0.65 floor transitively connects ~most agents into one blob; the cap then shreds it into arbitrary 8-buckets, scattering the 4 Alex variants. The subset lane is the fix — not by *connecting* the variants (embedding already does) but by score-priority, so the variants pack into one clean bucket with a *variant* (not a high-degree org pollutant) as canonical.

### 7. Decision gate REUSES the conservative judge

Adjudication reuses `LLMMergeJudge` / `_VERDICT_SYSTEM` / `parse_verdict` verbatim — already hard-coding "NX vs NX Lab distinct" and "two people sharing a first name distinct", defaulting DISTINCT on an unparseable reply. Default path is **pairwise** (each member vs the canonical). An optional `--cluster-judge` compare/select prompt (SOTA ~16% higher precision) asks the judge to pick same-entity members in one prompt, with a DISTINCT-on-unparseable parse and a fall-back to pairwise. Pairwise is always the safe default.

### 8. Corroboration is graded / supporting — NOT a hard precondition

A cluster's `corroboration` is `relational` (neighbour-set Jaccard ≥ `REL_JACCARD` = 0.25), `lexical` (Jaro-Winkler ≥ 0.90 or email-handle match), `both`, or `none`. **Thin entities (taylor.nguyen, few/no edges) qualify via the lexical lane alone and are NEVER gated out for missing relational overlap.**

Auto-merge requires *positive* corroboration evaluated **PER-VARIANT**, not a cluster-level `any()`: each surviving variant must carry its OWN relational-or-lexical evidence to be minted into the equivalence record. `ClusterVerdict.corroborated_ids` holds canonical + the corroborated survivors; `apply_reconciliation` mints the `AuthorityRecord` over THAT subset (its `member_ids` and `exact_match_pairs` both — the query-time fold keys on `member_ids`) and queues the uncorroborated survivors as their own sub-cluster. This stops one corroborated survivor from dragging an uncorroborated co-member into auto-merge: e.g. `taylor.nguyen` (handle-corroborated) auto-merges onto `Taylor Nguyen` while a bare `Taylor` in the same recalled cluster is queued for review. Name-only chains with no independent corroboration (the Alex variants: relational Jaccard 0.006–0.028, no JW≥0.90 lexical) are recalled into one cluster, judged `same`, and **queued** — never silently dropped or split.

### 9. Canonical selection

The cluster member with the highest node degree wins (the richest existing entity); ties → longest title, then lexicographically smallest id. Keeps thin handles as variants of the rich record (Alex / taylor.nguyen cases).

---

## The ADR-0007 off-graph safety rationale (load-bearing)

The whole design exists to satisfy one invariant: **Option A writes NOTHING to the live graph.** `apply_reconciliation` and every path it touches write only `.marginalia/authority/index.json` and `.marginalia/reconcile/queue.json`. The unit test spies BOTH `store.add_node` and `store.add_edge` and asserts each `== 0` across a full apply run. `ReconcileQueue` has no `store` reference at all — a graph write is impossible by construction. Because `Authority` is a `Node`, the *only* safe place for it is side-data.

---

## Consequences

### Positive

- Retroactive consolidation with **zero graph-write risk** (Option A) — reversible, ADR-0007-safe.
- Reuses the proven conservative judge and recall machinery; no new identity heuristic to mis-tune.
- Thin handle entities (the recall blind spot) are first-class via the email-handle and widened-band lanes.

### Known gap (explicit v0.0.5 behavior, not an oversight)

The **browse / visualization** read surfaces — `api_nodes_list` (`server/http.py:633`), `api_node_detail` edges (`706-713`), `api_graph` (`828+`) — are **NOT** routed through the equivalence fold. They intentionally show the **un-consolidated** graph (the trust-root truth), because consolidation is a recall-time *view*, not a graph mutation. Only the recall/ask chokepoint (`vault.query` → `search_claims`) folds. This is the v0.0.5 decision; routing browse surfaces through the fold (or a `?consolidate=` toggle) is possible later.

### Risk / follow-up

- **Judge `enable_thinking` = false (applied).** The reconciliation E2E measured that with thinking ON the GLM judge returns non-committal "distinct" (confidence 0.0, empty reason) on NAME-ONLY pairs, making merges like `taylor.nguyen`↔`Taylor Nguyen` flaky across draws. The seeded `judge` step default is now `enable_thinking=false` (`config/_vault.py`), matching `extraction`; `ask` keeps thinking on. A separate judge limitation remains: it can over-confidently return "same" on distinct people with superficially similar names — contained by the per-variant corroboration + review-queue gate (never auto-merged); do not relax the corroboration requirement without re-measuring precision.
- **Option B production seam.** The injectable model-free alias-aware ingest (and the `canonicalize_title` + `reconcile_against_store` Tier-0 fold) is fully implemented and tested. The *default production* `kg reconcile heal` rebuilds the graph corruption-free but does not yet rewrite candidate titles inside the full `Companion.remember()` pipeline (no clean public hook there; carving one would be invasive). Until that seam lands, Option B production heal = a plain trust-root rebuild; the alias fold is exercised via the injectable seam. Tracked as follow-up.

---

## Alternatives considered

- **In-place `owl:sameAs` / `skos:exactMatch` edge mint** — rejected: ADR-0007 corruption + over-strong predicate.
- **Bulk `Authority` hub nodes via `add_node`** — rejected: `Authority` is a `Node`; bulk add_node into a populated graph is the corruption.
- **Incremental single in-place edge mint as the act path** — rejected: still an in-place edge mutation on a populated graph.
- **Substring/prefix candidate matching** — rejected: pairs `Aler`/`Mualer`; Jaro-Winkler ordering is the precision-preserving alternative.

---

## Addendum — 2026-06-05: empirical recall/judge findings on the NX vault

A live, read-only `propose` (`POST /api/v1/reconcile/propose {"type":"Agent"}`, 95 Agent nodes → 28 candidate clusters, judge `glm-5-turbo`, `enable_thinking=false`) was run on the NX test vault after Alex flagged that **three real people appear as six nodes**. Raw evidence: `.scratchpad/reconcile-findings/propose-agents-2026-06-05.json`. The run **revised the mental model** the original ADR implied ("conservative tuning under-merges"). The true bottleneck is **candidate-cluster quality + judge reliability**, and the same looseness drives **over-merges**, not just under-merges.

### Ground truth (now a reconciler eval gold set)

- `Nivod` == `Ari Nivod` · `Tovrin` == `Tovrin Kalia` · `Alex Rivera` == `Alex Morgan` (three people, six nodes)
- Must stay distinct: `Aler Dalvic` ≠ `Mualer Disija` (the precision guard)

### Per-pair empirical verdict

| Pair | Co-clustered? | Judge | Diagnosis |
|---|---|---|---|
| `Nivod` / `Ari Nivod` | **No** — different clusters | n/a | Embedding split them: `Nivod`→{Corina, Arun}; `Ari Nivod`→{`Ari`, Aimeng Ji} (subset lane paired `Ari Nivod`↔`Ari`, not ↔`Nivod`). **Clustering miss, not the first-token guard, not the judge.** |
| `Tovrin` / `Tovrin Kalia` | Yes (`embedding`+`lexical`) | **`same` 0.95, corrob=lexical** | Judge **accepts** — would auto-merge on `apply`. Still two nodes only because no `apply` has run on this daemon (the 22 prior authority folds predate this pair). The cluster also swept in `Tovereign` (likely a wrong third member). |
| `Alex Rivera` / `Alex Morgan` | **No** — not even embedding-near | n/a | Structurally hardest: different surname, name-only signal is zero. Solvable only via **identity context** (shared identifier / dense shared neighbourhood), not any name lane. |
| `Aler Dalvic` / `Mualer Disija` | Never together | n/a | ✅ Guard holds. |

### Over-merge risk (the more important finding)

The loose embedding recall floor (`RECONCILE_RECALL_FLOOR=0.65`) over-groups. Two clusters were judged `same` @ 0.95:

- `{Tovrin, Tovereign, Tovrin Kalia}` → canonical `Tovrin` (sweeps in `Tovereign`)
- `{NX Lab, Tamola, Luke Gray, Luke Gray Lab}` → canonical `Luke Gray` — **a team + two unrelated people bundled with the one real dup**

The per-variant corroboration partition (`reconcile/apply.py`) is *expected* to contain these to the lexically-corroborated subset (`{Luke Gray, Luke Gray Lab}`), but the cluster-level `same` @ 0.95 verdict shows recall + judge are both contributing to false-positive pressure — **do not relax corroboration** (this ADR's original warning, now with live evidence).

### Judge reliability

Most of the 28 clusters returned confidence `0.0` with an **empty or truncated `reason`**, even with `enable_thinking=false` — the judge frequently disengages on name-only clusters (consistent with the §Risk note above), yet occasionally over-commits (`0.95` on a mixed cluster). Cluster-level judging of loose, multi-member clusters amplifies both failure modes vs. pairwise judging.

### Corrected model → design follow-up

The fix is **not** a threshold tweak. It is a recall + judge + corroboration overhaul, designed under ultracode (a separate design ADR follows). Design axes: (1) tighter, identity-aware **candidate clustering** so true dups co-cluster and unrelated names don't; (2) **judge robustness** (pairwise vs cluster, empty-reason retry, missing SAME exemplars for bare-first-name→full-name); (3) **corroboration as the deciding rail** (shared edges / identifiers — the only path that can both merge `Alex R.`/`Alex M.` *and* reject `NX Lab`/`Tamola`); (4) a **regression eval** pinned on the gold set above (recall on the 3 pairs, precision guard on `Aler`≠`Mualer` + the `NX Lab`/`Tamola` non-merge). Captured in cross-session memory: `project_reconciler_undermerge_finding.md`.
