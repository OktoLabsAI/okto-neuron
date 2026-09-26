# ADR 0020: Dense-fact predicate vocabulary

- **Status:** Accepted (locked by Alex Rivera 2026-06-21)
- **Date:** 2026-06-21
- **Deciders:** Alex Rivera, Marginalia core
- **Builds on:** ADR 0019 (graph-native answer assembly — Phase 2 dense-fact extraction; this is its prerequisite GN-8.5)
- **Relates to:** ADR 0016 (semantic claim identity — predicates bake into the claim id), ADR 0017 (judge-driven predicate canonicalization + SSSOM alias ledger), ADR 0002/0003 (Claim → Block byte anchoring)
- **Out of scope:** changing the 5-primitive closed schema; O_id entity-reference cell values (deferred to a later ADR per ADR 0019 decision 3); adding predicates not in this list (requires a follow-up ADR).

---

## Context

ADR 0019 Phase 2 closes the 63% extraction-gap by minting Claims for the dense
factual surfaces the LLM extractor skips today — table cells, config
`key = value` lines, version / image strings, status labels. These facts live
only as raw bytes in `Block` text, so no graph walk can reach them.

The minting cannot start until the predicate vocabulary is locked, because a
predicate is not a free-text label — it is **part of the Claim's identity**. Per
ADR 0016, `claim_id = sha256_hex("claim", S_id, P, O_id or literal-tag)`: the
normalized predicate `P` bakes directly into `semantic_claim_id`. It also seeds
the SSSOM alias ledger of ADR 0017, where synonyms are folded onto a canonical
form. If the dense-fact predicates churn after the first re-ingest, every claim
id derived from them changes and the ledger's canonical anchors shift — and the
locked Phase-2 plan re-ingests the reference-eval vault on every iteration. Vocabulary
churn after a re-ingest is therefore expensive and avoidable. Lock the predicates
first, before minting the first dense-fact Claim.

This vocabulary must be **small and stable**. A small set keeps claim ids
predictable across re-ingests, keeps the canonical predicate space tight (the
opposite of the ~1,061-token sprawl ADR 0017 fights), and gives the deterministic
extractor (GN-9/10) and the optional LLM dense-fact pass (GN-12) one target to
emit against.

## Decision

Define the canonical dense-fact predicate vocabulary below. Every dense-fact
Claim minted in Phase 2 uses exactly one of these predicates. Each is a Claim
with a **literal object** (O_literal) for v1 (ADR 0019 decision 3 — values are
literals, not entity nodes). Each minted Claim carries its source `Block` byte
anchor `(path, byte_start, byte_end, content_hash)` intact (ADR 0002/0003); none
are free-floating assertions.

| Predicate | Semantics | Shape `(S, P, O_literal)` | Example |
|---|---|---|---|
| `has_version` | A subject's declared version / release / build / image tag. | `(subject, has_version, "<version-string>")` | `(WidgetService, has_version, "1.4.2")` |
| `has_config` | A configuration setting the subject declares: a `key = value` or `key: value` pair from frontmatter, a code fence, or a config block. (`has_setting` folds to this.) | `(subject, has_config, "<key>=<value>")` | `(WidgetService, has_config, "timeout=30s")` |
| `has_status` | A status / state / lifecycle label the subject carries. | `(subject, has_status, "<status-label>")` | `(WidgetService, has_status, "active")` |
| `has_value` | A generic literal value attributed to a subject when no more specific predicate above fits (the fallback for a labelled scalar). | `(subject, has_value, "<value>")` | `(WidgetService, has_value, "42")` |
| `has_measurement` | A measured / quantitative literal, typically with a unit. | `(subject, has_measurement, "<quantity unit>")` | `(WidgetService, has_measurement, "512 MB")` |
| `table_cell` | A single non-header cell of a GFM table, row/column-aware so the cell is reconstructable. | `(table-or-row-subject, table_cell, "<row>:<col>=<value>")` | `(InventoryTable, table_cell, "row2:price=19.99")` |

Notes on the set:

- `table_cell` is row/column-aware on purpose: the literal encodes both the
  column (the answer's predicate-in-prose) and the row (the answer's subject-in-prose)
  so a table fact stays reconstructable without exploding into one predicate per
  column header. Where a row has a clear label entity, the deterministic minter
  (GN-9) may set the subject to that row-label entity and keep the column in the
  literal.
- `has_value` is the deliberate catch-all so the extractor never invents a new
  predicate for a one-off labelled scalar. A novel labelled scalar maps to
  `has_value`, not to a freshly coined predicate.

### Canonicalization

All predicates are lowercase `snake_case`, normalized through the existing
`normalize_predicate` path (`curator.py:316`) — the same path every other
predicate flows through, so dense-fact predicates are not a special case at
identity time.

Synonyms fold via the ADR 0017 SSSOM alias ledger with `skos:exactMatch`
(revocable, retrieval-strength), exactly as predicate synonymy folds today.
Seed-aliasable folds include, for example, `version_is → has_version`,
`version → has_version`, `setting → has_config`, `has_setting → has_config`,
`state → has_status`, `status_is → has_status`, `measurement → has_measurement`.
The deterministic extractor emits only the canonical six; the alias entries exist
so the optional LLM pass (GN-12) and any future open extraction converge onto the
canonical form rather than splitting a fact across synonym rows.

### Schema invariants preserved

- **5-primitive closed schema holds.** These are `Claim`s over
  `InformationObject` / entity subjects with literal objects — not a sixth
  primitive. No new primitive class, no manifest change.
- **Claim → Block byte anchoring holds.** Each minted Claim cites the byte span
  of the source surface within its 12K storage Block (ADR 0002/0003); the storage
  Block window is untouched (`feedback_chunking_fixed_12k`).
- **Identity composes with ADR 0016.** Because the predicate is locked, the same
  dense fact re-extracted on a re-ingest produces the same `semantic_claim_id`,
  so re-mentions corroborate rather than duplicate.

## Consequences

- **Stable claim ids across re-ingest.** Locking the predicate set before the
  first mint means dense-fact `semantic_claim_id`s do not move on subsequent
  re-ingests; the reference-eval re-ingest loop (ADR 0019 decision 1) stays comparable
  iteration to iteration, and the SSSOM ledger's canonical anchors stay put.
- **Deterministic extraction (GN-9/10) emits only these predicates.** The
  table-cell and config minters map directly to `table_cell` / `has_version` /
  `has_config` / `has_status` / `has_value`, with no LLM and no predicate
  invention — fully reproducible.
- **The LLM dense-fact pass (GN-12), if it ships, must map to this set.** It
  normalizes its output to the canonical six at extraction time (no free coinage)
  and folds known synonyms through the alias ledger. Predicates outside this set
  are rejected at minting.
- **New predicates are added by a follow-up ADR, not ad-hoc.** Any pressure to
  add a dense-fact predicate (or to promote a literal cell value to an O_id
  entity reference, per ADR 0019 decision 3) goes through a new ADR so the
  re-ingest stream and the claim-identity space stay stable. The vocabulary does
  not grow inside a sprint.
