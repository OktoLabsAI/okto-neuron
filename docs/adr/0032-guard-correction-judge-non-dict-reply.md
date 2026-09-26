# ADR 0032 — Guard the Correction Judge Against a Non-Dict JSON Reply

**Status:** Accepted
**Date:** 2026-07-08
**Depends on:** ADR 0027 (guard the best-effort integral-corrections phase), ADR 0022 (smarter merge judge / Lever 5 integral corrections)

---

## Context

ADR 0027 made the ADR-0022 integral-corrections phase best-effort at the *phase*
level: an outer net in `Companion.remember` and a per-supersede guard in
`supersede_contradicted` keep one bad correction from aborting a committed
ingest. But the **correction judge call itself** had two un-handled reply shapes
that could still throw *before* those guards saw a per-item failure (this is the
earlier non-dict-reply robustness family — a large local judge returning malformed JSON).

The judge parser (`make_correction_judge` in
`src/marginalia/companion/_incremental.py`) expected either an `"index": N`
match or a JSON object it could `.get("index")`. The 35B judge sometimes replies
with a **bare scalar** — `"2"` instead of `{"index": 2}`, especially on a
truncated `finish_reason="length"` reply. `json.loads("2")` returns a bare
`int`, and `int(...).get(...)` raises `AttributeError`. A list / string / bool
reply is a different malformed shape with the same fatal outcome. Because the
throw happened inside the judge call rather than inside a per-item body, it could
still surface as an uncaught error in the corrections pass.

## Decision

Two narrow guards, both in `src/marginalia/companion/_incremental.py`:

- **Parse-shape guard** in `make_correction_judge`: after `json.loads(reply)`,
  branch on the parsed type. A **bare `int` is the index**; a **dict** carries it
  under `"index"`; **anything else** (list / str / `None` / bool) → **no
  correction** (`-1`). `bool` is excluded *before* the `int` leg (it is an `int`
  subclass, so `True` must not be read as index 1). The returned index is
  range-checked against the candidate count.
- **Best-effort call guard** in `supersede_contradicted`: wrap the
  `correction_judge(...)` invocation in `try/except`. An unexpected raise
  (non-dict JSON reply, transport crash) is logged with claim context
  (`new_claim`, `source_path`) via `logger.exception` and the **single
  correction is skipped** — the claims for this run are already committed, and
  the pass continues applying the rest. (`RebuildInterrupted` still propagates,
  per ADR 0027.)

## Consequences

- A malformed judge reply is now a **skipped correction**, never a crash: the
  graph is correct-but-uncorrected for that one pair, and the next ingest of the
  source can re-apply it.
- The bare-int and bool cases are handled *correctly*, not just defensively — a
  bare `"2"` is read as index 2, so a valid correction from a terse judge is
  still applied rather than discarded.
- Failures stay **diagnosable**: the call guard logs claim-level context and the
  full traceback, consistent with ADR 0027's diagnosability goal.
- Guarded by `tests/companion/test_incremental_ingest.py`.
