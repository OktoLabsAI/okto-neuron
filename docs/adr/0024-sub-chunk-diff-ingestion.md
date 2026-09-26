# ADR 0024: Sub-chunk diff ingestion — extract only the changed fragment, and let memory accrete

- **Status:** Accepted (2026-06-30)
- **Date:** 2026-06-30
- **Deciders:** Alex Rivera, Marginalia core
- **Builds on:** ADR 0023 (per-block content-hash skip — the Layer 1 partition this extends), ADR 0002 (12k-window chunker), ADR 0003 (provenance is a `SourceSpan`), ADR 0007 (rebuild from the markdown trust root), ADR 0016 (semantic claim identity & corroboration), ADR 0022 (temporal invalidation, anticipated here)
- **Out of scope:** changing the 5-primitive closed schema; changing the chunker's fixed 12k-window policy; making `remember()` non-blocking (tracked separately).

---

## Context

ADR 0023 added a **block-level** skip: a 12k Block whose `content_hash` already
carries live LLM Claims is not re-extracted. But the chunker uses **fixed
12k-byte windows** (`ingest/markdown.py`, `_WINDOW_BYTES = 12000`), so a one-line
edit to a small file changes the *whole block's* hash. For the common
single-block file, ADR 0023 therefore yields **zero** win — the entire block is
"changed" and re-extracted.

Alex's framing: *"although we have the chunk set at 12,000, there is very
little need to always use the 12,000."* The real lever is to extract only the
**changed fragment** of a changed block, not the whole window. Measured: a
one-line add to a 5KB file re-extracts ≈ the whole block today (and under
ADR 0023); a sub-chunk diff extracts ≈ the ~150-byte changed line — roughly
**30–40× less LLM work**.

Two grounding facts (verified against live code) make this an additive narrowing,
not new plumbing:

- **Byte anchoring already exists at block granularity.** The extraction loop
  copies `anchor.byte_start/byte_end/content_hash` onto every `EdgeCandidate`
  (`companion/__init__.py`), and `_build_source_span` turns them into a populated
  `SourceSpan`. Narrowing the anchor range to a hunk reuses this — it does not
  build anything missing.
- **LLM-path claim identity is content-hash independent.** `semantic_claim_id`
  is `sha256("claim", S, P, O)` (`consolidate/_claim_identity.py`). Re-extracting
  a changed fragment never churns claim ids; an identical re-minted fact merges
  by corroboration (ADR 0016).

## Decision

When `MARGINALIA_SUBCHUNK_INGEST` is set (default off; implies the Layer 1
`MARGINALIA_INCREMENTAL_INGEST` partition), for each **changed** Block:

1. **Reconstruct the OLD block text** from the prior Block node's stored
   `content`, captured BEFORE `vault.add` overwrites it.
2. **Diff** old text vs the NEW raw block bytes on **line boundaries** with
   `difflib.SequenceMatcher.get_opcodes()`. The diff anchors over **raw bytes**
   (anchors index raw; stored `.content` is stripped), comparing rstripped lines
   so trailing-whitespace churn at window edges does not spuriously re-extract.
3. **Extract only `replace`/`insert` hunks** — the changed fragment. `equal` runs
   are never re-extracted (zero churn). A brand-new Block with no prior at its
   index falls back to whole-block extraction (correctness never depends on a
   clean diff).
4. **Anchor** minted claims with a hunk-scoped anchor: `block_id` = the parent
   12k Block (unchanged); `byte_start/byte_end/content_hash` = the **hunk**. The
   existing anchor-copy then narrows `source_span` automatically.

### Memory accretes — it does not mirror (the load-bearing decision)

When an edit **removes** content, the knowledge it produced is **not erased**:
*"removed files don't necessarily mean the knowledge there has been erased… it
was at least true."* (Alex, 2026-06-30, confirmed at kickoff.) This
deliberately relaxes ADR 0023's strict graph-equivalence to: **graph-equivalent
for additions and corrections; additive for removals, with durable provenance so
rebuild still reproduces the memory.** Since the `GraphStore` protocol has **no
delete API**, all invalidation is facet-based and filtered at recall (the same
mechanism as `infra` / `_salience`):

- **Removal** (a source line is deleted, no replacement re-asserts the same
  subject+predicate): the Claim stays **live in recall**, stamped
  `_detached: True` + `valid_as_of: <date>`. A durable annotation is written to
  `<vault>/.marginalia/detached/<source-hash>.jsonl` so a future `kg rebuild`
  re-derives the detachment — keeping retained knowledge inside the trust root
  (preserves ADR 0007).
- **Correction** (a fact's value changes): the OLD Claim is `_superseded: True`
  + `valid_until: <date>` (dated, **filtered from recall** via `is_superseded`),
  and a `supersedes` edge new→old keeps the history walkable. The NEW Claim is
  minted live. **This is handled INTEGRALLY, not only on the sub-chunk path** —
  see "Integral corrections" below.
- **Addition**: a new Claim is minted from the cheap sub-chunk extract.

A Claim still derived from a present Block (re-corroborated) is left untouched.

### Integral corrections (ADR 0022 Lever 5) — runs on EVERY ingest

Correction detection is **not** confined to the sub-chunk diff. Per the user's
directive (2026-06-30, *"this should already be an integral part of the
ingestion"*) and ADR 0022's proposed "Lever 5 — temporal invalidation," a
post-mint pass runs on **every** `remember()`: each Claim minted this run is
checked against the pre-existing graph, and if it **corrects** a prior fact (same
real-world attribute, changed value) the old Claim is superseded. This catches a
correction even when the source file is different from the one that asserted the
old fact.

The hard part is that extraction drifts the subject, predicate, *and* object
phrasing between runs (`Atlas Project / has_value / milestone due Jun 26` →
`Atlas Milestone / has_due_date / due Jul 1`), so exact `(subject, predicate)`
matching fails. Detection therefore is:

1. **Cheap lexical prefilter** — candidate old Claims share the new Claim's
   subject id OR a subject-title token, and assert a *different* object value.
2. **Semantic confirmation** — the existing judge LLM provider decides whether a
   candidate is genuinely a correction (single-valued attribute updated) vs an
   *additional* value (multi-valued predicate like `has_tag`, where both stay
   live). Conservative: the judge biases "not a correction," so it only ever
   hides a genuine stale value, and `supersedes` edges make it reversible.

This is the "check what is already written" behaviour the user expects of
ingestion. Default (user-chosen 2026-06-30): **auto-supersede** on a confirmed
correction (rather than parking it in review).

### Correctness invariant (relaxed from ADR 0023)

> For additions and corrections, the set of **live** LLM Claims after an
> incremental F′→F equals the set after a full re-extraction of F. For removals,
> the old Claim is retained (detached) rather than dropped — a deliberate,
> durably-recorded divergence.

## Consequences

**Positive**
- One-line edit ⇒ ~one hunk of LLM work instead of a whole 12k block (~30–40×
  less for the common case). Directly serves the "living memory that receives new
  things" thesis.
- Corrections supersede cleanly (recall shows the current value); removed
  knowledge persists as dated memory instead of vanishing.

**Negative / risks**
- **Integral-corrections scan cost.** The post-mint correction pass scans the
  vault's Claims (snapshot before mint, diff after) and reads each existing
  Claim's subject to build the lexical prefilter — O(claims) per ingest. The
  expensive part (the judge LLM call) stays bounded by the prefilter (shared
  subject id/token + different value), but the scan grows with vault size. A
  subject→claims index is a future optimisation; acceptable for v1 vault sizes.
- Hunk-to-claim attribution for removals is only as precise as the claim's
  anchor. Claims minted by whole-block extraction (pre-0024) anchor to the whole
  block; the reconcile pass therefore operates at "orphan block" granularity —
  it detaches/supersedes claims whose **only** derivation is a now-absent Block.
  Fine-grained line attribution improves as sub-chunk anchoring spreads.
- The diff trusts the stored (stripped) prior `.content` as the old side; edge
  whitespace differences can produce an extra edge hunk (still far cheaper than
  the whole block). Bounded, never incorrect.

**Neutral**
- `kg rebuild` still re-extracts everything from the trust root; the detachment
  JSONL annotations are the durable record it replays to reproduce retained
  removals.

## Validation (Definition of Done)

- Pure diff: one-line change → exactly one hunk with the correct byte range and
  `content_hash`; identical/deleted-only → zero hunks.
- E2E one-line edit: `hunks_extracted == 1`, the extractor sees only the changed
  fragment (not the 12k window), and the minted Claim's `source_span` is the
  edited line's byte range.
- Removal: old Claim retained, `_detached`/`valid_as_of` stamped, JSONL written,
  still in recall.
- Correction: old Claim `_superseded`/`valid_until` and filtered from recall, new
  Claim live, `supersedes` edge present.
- Additions/corrections equivalence vs a from-scratch ingest of final F.

## Addendum — 2026-07-02 live-run remediation (Phase 4)

The first production folder-watch run exposed three defects in this ADR's
removal/reconcile pass; all are fixed in the remediation series:

- **F6 — pure deletions never detached.** `remember()`'s zero-candidate early
  return (an edit whose remnants extract nothing — exactly what a deletion
  produces) returned before the reconcile block. The detach half is extracted
  into `_reconcile_removals(...)` and now runs on BOTH the normal commit path
  and the early return, emitting `claims_reconciled` with
  `claims_detached` in the run summary either way.
- **F9 — re-chunking churn detached live facts.** The detach pass judged only
  block-derivation liveness, so a fact whose line merely MOVED to a re-sliced
  Block was detached. Each orphan Block now gets an `OrphanDiff` (its stored
  content line-diffed against the union of current Blocks' content — never
  `prior.by_index`), and a claim is detached only when at least one of its
  anchored lines actually disappeared (`_claim_anchored_lines` resolves the
  claim's `source_span` inside the orphan block; whole-block fallback).
  Zero-removal diffs (pure churn) detach nothing.
- **F7 — reverts now resurrect.** A claim stamped `_superseded`/`_detached`
  whose content returns is resurrected (lifecycle facets stripped) through two
  legs: the mint-path merge leg (`_maybe_resurrect` when the claim identity is
  re-minted) and a deterministic `resurrect_reverted_claims` pass over current
  Blocks — load-bearing because the Layer-1 skip prevents re-extraction on a
  pure revert. Both legs are RECENCY-GATED on `asserted_at` (source mtime at
  ingest, ADR 0022 addendum): a restored old backup does not resurrect facts
  corrected later. A resurrected previously-superseded claim flags
  `stale_source: true` in the `claims_reconciled` payload (future inbox
  surfacing of reverse-supersedes).
