# ADR 0033 — The Eval Harness Pins ONE Judge Model Across Both A/B Arms

**Status:** Accepted
**Date:** 2026-07-08
**Relates to:** ADR 0028 (efficient hybrid answer path — the subgraph-vs-block A/B this harness runs), ADR 0019 (graph-native answer assembly — the extraction-gap the A/B measures)

---

## Context

The golden eval harness compares two arms — subgraph-assembled answers vs
block-dump answers — on one daemon and one graph, with a **semantic judge** LLM
grading each answer against ground truth. If the judge model is resolved
*per-arm* (or the provider's model lineup shifts mid-run), the two arms are
graded by two different judges, and the A/B verdict is confounded by a
**judge-lineup shift** rather than reflecting the arms themselves. A parity claim
("subgraph ≈ block") is only trustworthy if a single, recorded judge graded both
sides.

`--judge-model auto` is convenient — resolve the largest available chat model —
but "auto" evaluated twice can resolve to two different models across a run.

## Decision

The A/B orchestrator (`tests/golden/bin/eval-run.sh`) **pins ONE judge model for
both arms**, resolved **exactly once at run start**:

- `JUDGE_MODEL` defaults to `"auto"`, but `"auto"` is resolved a **single time**
  before either arm runs — inside the `--ab-subgraph` branch of
  `tests/golden/bin/eval-run.sh`, the `if [[ "$JUDGE_MODEL" == "auto" ]]`
  guard calls `semantic_judge._select_chat_model(LLM_BASE)` once, assigns the
  result to `RESOLVED`, and reassigns it into `JUDGE_MODEL` — the same
  variable is then used for **both** arms. (Referenced by name, not line
  number, because this file is never regenerated and the script's line
  numbers drift as it's edited.)
- The **resolved** judge model (never the literal `"auto"`) is recorded in the
  run manifest (`tests/golden/bin/manifest.py`), so an A/B result carries which
  judge actually graded it and the reproducibility pin is honest.

The rule: **the eval harness never leaves the judge as `auto` at grading time,
and always records the resolved judge**, so a parity verdict cannot be silently
confounded by a lineup shift.

## Consequences

- **Un-confounded A/B verdicts.** The finale subgraph-vs-block A/B graded both
  arms with one pinned judge: subgraph **122/142** vs block **120/142**
  (statistical parity, p = 0.80) at **6.24× fewer answer tokens**. Because the
  judge was pinned and recorded, the parity claim is attributable to the arms,
  not to a mid-run judge change.
- **Reproducible.** The recorded judge id lets a later re-run reconstruct the
  exact grading configuration; a manifest that read `auto` would not.
- **No behavior change for single-arm runs.** Pinning only matters when two arms
  are graded in one run; the default `auto`-resolve-once path is unchanged for a
  normal single-arm golden run.
