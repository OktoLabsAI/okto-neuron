# ADR 0004: Within-Batch Entity Resolution — Judge Batch Siblings, Not Just the Store

**Status:** Accepted
**Date:** 2026-06-02
**Deciders:** Alex Rivera, Marginalia core
**Supersedes:** None
**Superseded by:** None
**Amends:** None
**Amended:** 2026-06-02 — relationship context for the judge (F8–F10, see below)

---

## Context

A user inspecting the Agent nodes found duplicate twins of the same real-world
entity: a bare form and a namespace-prefixed form side by side — `alex` and
`user:alex`, `alice` and `agent:alice`, `bob-engineer` and
`agent:bob-engineer`. Each twin pair is extracted from the **same source file**
(the frontmatter block yields the bare name; a prose block yields the prefixed
name).

Entity resolution at the time ran three tiers, all comparing a candidate against
the **already-committed store**:

- **Tier 0** `reconcile_against_store` — exact `(type, normalized-title)`, no LLM.
- **Tier 1** `find_similar` — cosine ≥ 0.82, same-type, vs the committed store.
- **Tier 2** `judge_against_store` + `LLMMergeJudge` — conservative same/distinct
  verdict, merge only at confidence ≥ 0.8.

Plus one intra-batch pass, `collapse_duplicates`, which is **exact
normalized-title only** (no cosine leg) and so cannot bridge `alice` ≠
`agent:alice`.

Read-only probes against the live graph overturned the obvious hypotheses:

| Hypothesis | Probe | Verdict |
|---|---|---|
| Recall miss (cosine < 0.82 → judge never asked) | stored cosines: alice/agent:alice **0.8454**, alex/user:alex **0.8751**, bob/agent:bob **0.8234** | **Rejected** — all clear 0.82 |
| Judge mis-rules the prefix pair | alice×5 @ temp 0.7 → MERGE 4/5 | **Mostly no** — the lone distinct was temperature noise |
| **Within-batch gap** | `judge_against_store` calls `find_similar(cand, store)` — STORE only | **Confirmed root cause** |

Both twins are born in one `remember()` batch and are both novel-to-store, so the
store-facing judge is **never asked "are these two the same?"** — and
`collapse_duplicates` can't bridge a non-exact title. Both commit side by side.

## Decision

**Add a within-batch (candidate-vs-candidate) merge-judge pass, and give the
judge its own temperature.**

`judge_within_batch` slots into `companion.remember` **after**
`collapse_duplicates` and **before** `reconcile_against_store`. It compares
surviving candidates to their batch siblings and merges the later into the
earlier, reusing the conservative `LLMMergeJudge` already built for the store
pass.

| # | Decision | Rationale |
|---|---|---|
| F1 | **Within-batch judge pass** comparing each candidate to kept batch siblings, not only the committed store. | Closes the structural gap: twins born in one batch are now adjudicated against each other. |
| F2 | **Gate identically to the store tiers:** same-type + candidate-embedding cosine ≥ 0.82 band before any LLM call. | Bounds judge calls to genuine look-alikes; reuses the one calibrated threshold, no second magic number. |
| F3 | **Reuse the `collapse_duplicates` edge-connected guard** — never merge two candidates that are the endpoints of one edge. | A relationship's subject and object are distinct by construction (e.g. `bob-engineer alias_of agent:robert-engineer`); the guard makes the alias trap unfusable. |
| F4 | **Survivor = first occurrence.** Frontmatter precedes prose, so the bare form wins by file byte-order. | A conscious, accepted call. Which surface form is canonical is deferred; first-occurrence is deterministic for a given file. |
| F5 | **Merge only on `same` AND confidence ≥ 0.8; distinct when unsure.** Reuse the conservative judge; any LLM failure → distinct. | Precision over recall: a false merge collapses two real entities. The pass can only ever *reduce* duplication. |
| F6 | **Remap edges onto the surviving candidate id** (`_remap_edges`); transitivity falls out because only kept survivors are comparison targets (A~B~C → one survivor A). | A re-mention's relationship still lands and still mints its byte-anchored Claim. |
| F7 | **Dedicated `llm.judge_temperature` (default 0.2)** for both judge passes, instead of inheriting extraction's 0.7. | The judge is a binary decision, not generative prose: at 0.7 the verdict flickered (~4/5) and occasionally ran the reasoning block to the `max_tokens` cap. Greedy 0.0 stays banned for Qwen3 (repetition). |

## Consequences

**Positive**
- Twins extracted in one file now collapse: validated on the `_smoke` fixture,
  `user:alex`→`alex` and `agent:bob-engineer`→`bob-engineer` merge to a
  single node; the user's reported `alex`/`agent.Marcus` duplicate is resolved.
- **Precision preserved.** The deliberate `_smoke` alias trap
  (`agent:bob-engineer` *alias_of* `agent:robert-engineer`) held:
  `agent:robert-engineer` stays a separate node, the `alias_of` relationship is
  kept as a Claim, and the edge-connected guard actively vetoes the merge. All
  distinct entities remained distinct — no false merges.
- `judge_temperature=0.2` stabilizes verdicts: alice×5 went 5/5 consistent with
  **0** `max_tokens` runaways (was 4/5 + 1 runaway at 0.7).
- Provenance gate (deterministic byte-hash) passes unchanged.

**Costs / risks**
- **Recall is content-dependent, by design.** A twin only merges when the
  extracted content makes sameness clear to a conservative judge. On `_smoke`,
  `alice`/`agent:alice` carried content describing both as "synthetic entities
  sharing only a common name," so the judge stably ruled them *distinct*. That is
  the conservative bias working as intended (precision-safe), not a regression —
  it points at extraction content quality, not the resolution pass.
- Extra LLM calls per batch, bounded by the 0.82 band + same-type gate (rare for
  novel content).
- `judge_temperature` changes the **store** judge globally too; isolated from the
  structural change during validation (Option B validated at the prior temp
  first, temperature changed and re-validated separately).

## Open questions (not blockers)
- **Canonical surface form.** F4 lets file order pick bare-vs-prefixed. A
  deterministic canonicalization (prefer the CURIE, or the longest form) is a
  later refinement.
- **Content-driven recall.** When extraction content under-determines identity,
  the judge stays conservative. Sharper identity signals in the extraction prompt
  (or a cheap deterministic CURIE-prefix pre-merge) could lift recall without
  touching precision. *(Addressed by the 2026-06-02 amendment below.)*

---

## Amendment (2026-06-02): Relationship context for the judge

**Status:** Accepted. Extends F1–F7; changes no prior decision.

### Why

The "content-driven recall" open question above was load-bearing on `_smoke`:
`alice`/`agent:alice` carried bare descriptions ("alice is a reviewer") that gave
the conservative judge nothing to merge on, so it stably ruled them *distinct*
even though they are one entity. A deterministic CURIE-prefix pre-merge was
considered and **rejected** — too narrow (only catches the prefix shape) and
risky (it can fuse two genuinely different entities that happen to share a stem).

### What

Feed the judge each entity's **relationships** as supporting evidence, in both
the within-batch and against-store passes. A relationship line reads
`subject --predicate--> object` (object refs resolve to titles; propositional
literals render as their value), capped at five per entity.

| # | Decision | Rationale |
|---|---|---|
| F8 | `MergeJudge.judge()` gains optional `candidate_context` / `existing_context` strings; both default `""`. With the defaults the prompt is **byte-for-byte** the prior name+description prompt. | Any verdict shift is attributable to the new signal, not prompt drift. Stubs and old call sites keep working. |
| F9 | **Relationships only — never source path.** A `remember()` batch processes one source file, so `source_path` is constant across all candidates in a within-batch pass: zero discriminating power, pure merge-pressure. Relationships are two-way (a shared neighbour corroborates *sameness*; an `alias_of` / `distinguished_from` edge reveals *distinctness*). | Only signals that can argue *both* directions are admitted. A one-way-toward-merge signal can only erode precision. |
| F10 | Context is framed in-prompt as **SUPPORTING, not deciding** evidence; the conservative `_VERDICT_SYSTEM` and the merge-confidence gate are unchanged. | Recall lift must not come at precision's expense. |
| F11 | **Distinguishing predicates are cap-resistant.** Relationships are capped at five per entity; before the cap, a stable sort pulls distinguishing predicates (`distinguished_from`, `alias_of`, `different_from`, `not_same_as`) to the front. | A distinguishing edge typically arrives as an *incoming* edge, which sorts last and would be the first line dropped on a high-degree entity (>5 relationships) — silently removing the exact signal that keeps a look-alike apart. On `_smoke` totals were ≤5 so it never bit; in production it would. The sort makes the precision-critical edge un-droppable while the cap still bounds prompt size. |

### Evidence

A controlled spike on the live `_smoke` graph (real 35B judge, `judge_temperature=0.2`, 5 runs each) isolated the effect of relationship context:

| Pair | Bare name+desc | +Relationships | Note |
|---|---|---|---|
| `alice` / `agent:alice` (same) | MERGE **2/5** (flickery) | MERGE **5/5** | corroborated by shared Q1 SOW + reviewer role |
| `alice` / `bob-engineer` (distinct) | — | DISTINCT **5/5** @ conf 1.00 | held |
| `agent:alice` / `agent:bob-engineer` (distinct) | — | DISTINCT **5/5** @ conf 1.00 | held; judge cites the `distinguished_from` edge |

The full sealed golden re-run confirmed it end-to-end: provenance gate pass,
judge tally unchanged at 7 correct / 1 partial / 0 wrong / 0 missed, and the
committed graph collapsed both CURIE twins (`agent:alice`→`alice`,
`agent:bob-engineer`→`bob-engineer`) **while** the alias-trap entity
`robert-engineer` stayed a separate node. Recall up, precision intact.

### Consequence for the open questions

"Content-driven recall" is largely **closed** for the common case (twins that
share relationships). It remains open only for genuinely isolated twins with no
corroborating edges — for those the judge is correctly conservative. The
rejected CURIE-prefix pre-merge is **not** revisited; relationships subsume it
and generalize beyond the prefix shape (e.g. `Marcus` / `alex`).
