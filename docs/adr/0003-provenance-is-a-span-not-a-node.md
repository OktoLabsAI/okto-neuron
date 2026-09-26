# ADR 0003: Provenance Is a Span, Not a Node — Demote Block

**Status:** Partially implemented; final schema cut not accepted
**Date:** 2026-06-01
**Deciders:** Alex Rivera, Marginalia core
**Supersedes:** None
**Superseded by:** None
**Amends:** ADR 0002 (supersedes D3, D4, D5, D7; keeps D1, D2, D6, D8)

**Lifecycle addendum — 2026-07-13.** `SourceSpan`, Claim span population, span-based
provenance reads, and fixed extraction windows shipped additively. `Block` remains a
stored support node and query-time source-context machinery still exists, so E2/E5 and
the destructive schema cut are not implemented. The remaining cut is future product
architecture, not a `0.0.40` release gate.

---

## Context

ADR 0002 treated `Block` as a first-class stored graph node and spent its
decisions (D3 structure-aware chunker, D4 embedder-derived sizing, D5 neighbor
expansion, D7 `deterministic-v2` + `kg rebuild`) trying to make that node the
right size. A golden run produced **1959 Block nodes, ~80% empty**, because one
Block was minted per CommonMark token and the LLM extracted from each lonely
fragment in isolation.

Stepping back surfaced the actual mistake: **we promoted the receipt to a
citizen.** A Block was never knowledge — it is a *citation*: "this claim came
from bytes 4012–4180 of `meeting.md`." The product is the extracted knowledge
(Agents, Activities, Concepts, InformationObjects, Places, and the Claims that
bind them). Block was pure provenance, and retrieval already never searches it
(`query.py` excludes `type="Block"`; ranking runs over extracted entities). We
were optimizing the filing system for the staples.

Two facts make the simpler model safe:
1. **No live data matters** — we can make a clean schema cut, no migration.
2. **Nothing reads Block at query time** — it is not in the retrieval path.

## Decision

**Provenance is a value-object carried on the assertion, not a stored node.**
Delete the `Block` graph node. A `Claim` (and `Annotation`) anchors to a
`SourceSpan`: `(source_path, byte_start, byte_end, content_hash)`, alongside the
existing PROV-O who/how (`extraction_activity_id`, `agent_id`, `model_id`,
`prompt_hash`). The source file is already a `Document` node; `prov:wasDerivedFrom`
points the claim at that Document, qualified by the byte span.

| # | Decision | Rationale |
|---|---|---|
| E1 | **Delete the Block stored node.** Support types drop 6 → 5 (Document, Identifier, Annotation, Claim, Finding). | Block was provenance, never knowledge; nothing queries it. |
| E2 | **`SourceSpan` value-object** = `(source_path, byte_start, byte_end, content_hash)`. Carried on Claim/Annotation; never a node. | Traceability is a field, not a graph object. |
| E3 | **Extraction windows are runtime-only.** Ingest reads a document as text and feeds the LLM sized, overlapping windows; windows are never stored. | "Ingest like a book." Decouples extraction context from any stored unit. |
| E4 | **Claim anchors to its extraction window's span** (coarse-but-honest default). Finer evidence-quote→byte attribution is a later refinement, not a blocker. | Provenance precision = window size; start simple, sharpen later. |
| E5 | **Delete query-time neighbor expansion** (`build_block_index`, `expand_block_context`, `ContextSpan`, `MARGINALIA_QUERY_NEIGHBORS`, `_query_neighbors`). Context at read time = re-slice the source file by bytes. | Phase 2 solved a problem this model doesn't have. With no Block nodes there are no neighbors to stitch. |
| E6 | **`Claim.id` derivation unchanged** — still `sha256(content_hash ‖ S ‖ P ‖ O ‖ optional(model ‖ prompt))`. `content_hash` now hashes the span bytes. | No identity churn; the formula already keyed on content_hash. |

### Kept from ADR 0002
- **D1/D2** — provenance goal is good-enough citation + drift detection (not
  tamper-evidence); byte-exact for text, two-layer for binary.
- **D6** — content-addressed extraction caches (extraction still dominates cost).
- **D8** — paired gold-span evaluation on the golden harness.

### Superseded from ADR 0002
- **D3, D4, D5, D7** — there is no Block identity to preserve, no boundary churn,
  no neighbor machinery to build, no `deterministic-v2` chunker. The "chunker
  project" largely dissolves.

## Consequences

**Positive**
- Graph holds only knowledge + receipts attached to it. 1959 Block nodes vanish.
- No Block.id / Claim.id churn across rebuilds.
- Phase 2 neighbor-expansion machinery is deleted, not maintained.
- Smaller, clearer schema (5 support types).

**Costs / risks**
- **Reverts shipped Phase 2 work** (`ContextSpan`, `expand_block_context`, env
  flag). Honest sunk cost.
- **Coarser provenance by default** — a claim cites its whole extraction window,
  not a tight sentence span. E4 accepts this; evidence-quote attribution can
  sharpen it later without schema change.
- Touches the RFC's locked support-type set (6 → 5). Removing a support type is
  not a new-primitive event, but the RFC's schema section must be updated to match.

## Open question (not a blocker)
- **Attribution granularity.** Default is window-span (E4). If/when we want
  sentence-tight spans, the extractor returns its supporting quote and we locate
  it within the window's byte range — deterministic because the search space is
  one window, not the whole document.
