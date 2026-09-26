# ADR 0021: Dynamic truncation-escalation extraction (auto mode)

- **Status:** Accepted (2026-06-23)
- **Date:** 2026-06-23
- **Deciders:** Alex Rivera, Marginalia core
- **Builds on:** ADR 0019 (graph-native answer assembly — the 63% extraction-gap is the bottleneck), ADR 0002/0003 (12k byte-anchored `Block`, Claim → Block anchoring)
- **Relates to:** ADR 0015 (ingest throughput — escalation only runs on the blocks that need it, so the cost stays bounded)
- **Out of scope:** changing the 5-primitive closed schema; the chunk size (fixed 12k windows stay); the enumerate (Mode B) pipeline internals, which this reuses unchanged.

---

## Context

Single-pass extraction sends a 12k `Block` to the model and parses one JSON
response. When a dense block produces more output than the response token cap
allows, the provider stops at `finish_reason="length"` and the JSON is cut
mid-object. `parse_extraction` then yields whatever objects completed and
silently drops the rest. The block looks like it extracted fine; the tail facts
are simply gone.

This is silent data loss, not a parse error — nothing surfaced it. On the dense
corpora we ingest (tables, config dumps, version manifests), measurement put the
truncated-block rate at roughly **15–20% of dense blocks**. Those are exactly the
blocks carrying the dense factual surfaces ADR 0019 identified as the 63%
extraction-gap, so the loss lands on the facts we most need minted as Claims.

A blunt fix — run every block through the slower enumerate-then-describe pipeline
(Mode B) — recovers the facts but multiplies extraction cost across the whole
vault, most of which never truncated.

## Decision

Add an `"auto"` extraction mode and make it the **effective default** when a
vault has not pinned an explicit mode. `"baseline"` (single-pass) and
`"enumerate"` (always Mode B) remain explicitly selectable; the `StepLLM.mode`
config field stays `None` so non-extraction steps are unaffected and an operator
can still pin either mode.

Auto is escalate-on-truncation, decided per block from the baseline call's
`finish_reason`:

- **`"stop"` (or no reason reported)** → accept the baseline result as-is. No
  extra cost. This is the overwhelming majority of blocks.
- **`"length"`** → the block truncated. Re-run *that block only* through the
  enumerate (Mode B) pipeline and use its result instead (INFO log identifying
  the block by a content fingerprint). The expensive path runs solely on blocks
  that demonstrably lost data.
- **anything else** (`"content_filter"`, `"tool_calls"`, a null/missing or
  provider-specific value) → the **unexpected-finish guard**: do *not*
  auto-escalate (escalation only fixes truncation). Keep the parsed result, but
  log a WARNING with the actual reason and block identity and record it on the
  result (`ExtractionResult.unexpected_finish`) so ingest counts it. This is
  awareness, not recovery.

A **second-order guard** covers the case where the enumerate escalation *itself*
still truncates: log CRITICAL and keep the `truncated` flag set so "we still lost
data on this block" stays visible rather than masked by the escalation.

The companion surfaces both anomaly classes during ingest as counters —
`unexpected_finish_blocks` (the guard fired) and `still_truncated_blocks` (the
escalation still truncated) — emitted as warnings + trace so an operator sees
data-integrity anomalies on a run.

Byte-anchoring is unchanged. Both the baseline and enumerate paths return a
normal `ExtractionResult` whose candidates the companion anchors to the parent
`Block`'s byte range identically — escalation changes how facts are *found*, not
how they are *anchored*.

## Why

Output-budget truncation was silent data loss on precisely the dense blocks that
matter most for graph-native answering. The model already tells us when it
truncated (`finish_reason="length"`); auto just listens and pays the recovery
cost only where the signal fires, instead of either ignoring it (baseline) or
paying everywhere (enumerate).

## Consequences

Measured on the extraction-completeness eval:

- **Extraction completeness 0.80 → 0.95** on dense blocks — the previously-dropped
  tail facts now get minted.
- **Subgraph answer-presence +0.035** — recovered facts become reachable Claims,
  so the graph-native answer path (ADR 0019) finds more without a block dump.
- **Block-dump parity is NOT reached.** Auto narrows the subgraph-vs-block gap but
  does not close it, so the GN-16 flip (defaulting answers to the subgraph path)
  stays deferred — the parity precondition is still unmet.
- **Cost: ~9× on escalated blocks only.** A block that escalates pays roughly the
  enumerate-pipeline cost; the ~80–85% of dense blocks that finish on `"stop"`
  pay nothing extra. Vault-wide extraction cost rises only in proportion to the
  truncated-block rate, not across the board.

The trade is a bounded cost increase concentrated on the blocks that were losing
data, in exchange for recovering those facts and making any remaining loss
loud instead of silent.

## Addendum: empty-result retry (same spirit, different failure mode)

Truncation escalation covers `finish_reason=="length"` — the JSON is cut off
mid-object. It does NOT cover a distinct, previously-silent failure: a *clean*
`"stop"` that returns valid-but-empty JSON (`{"nodes":[],"edges":[]}`) or
unparseable prose, for a block whose input text was non-trivial. `parse_extraction`
already returns an empty `ExtractionResult()` for unparseable text with no
error and no flag, so on the (rare) occasions the model flubs a trivial
single-fact block, the block's fact(s) were silently dropped with nothing to
retry against — `finish_reason` was `"stop"`, so the truncation path never
fired, and re-ingesting the same file later would usually succeed (proving the
model *can* extract it).

`LLMExtractor._extract_baseline` now applies the same "listen to the signal,
pay the cost only where it fires" principle to this case: when a call is
non-truncated and yields zero node/edge candidates on non-empty input text, it
retries **exactly once** at `temperature=0.0` (keeping all other params) before
accepting empty. If the retry produces candidates, those are used; if the retry
is itself empty (and non-truncated), the empty result is accepted but flagged
via a new `ExtractionResult.empty_after_retry: bool` field — mirroring
`truncated`/`unexpected_finish` — so a genuinely-empty block is distinguishable
from one that lost data twice in a row. If the retry truncates instead
(`finish_reason=="length"`), the existing truncation-escalation path above
handles it unchanged.

This applies inside `_extract_baseline`, so both `"baseline"` and `"auto"` mode
inherit it (`"auto"` calls baseline first regardless of outcome); it does not
touch the enumerate (Mode B) pipeline, which does not go through this parse
path. Cost impact is minimal: the retry only fires on the empty case, which is
rare — non-empty results never pay for it.

The companion counts blocks with `empty_after_retry` set (`empty_after_retry_blocks`)
alongside `unexpected_finish_blocks`/`still_truncated_blocks` in the existing
`extraction_anomalies` trace event, so an operator sees "extraction lost this
twice" rather than mistaking it for a legitimately empty block.
