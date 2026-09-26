# ADR 0027 — Guard the Best-Effort Integral-Corrections Phase in Rebuild

**Status:** Accepted
**Date:** 2026-07-05
**Depends on:** ADR 0007 (rebuild from trust root), ADR 0022 (smarter merge judge / Lever 5 integral corrections)

---

## Context

ADR 0022 Lever 5 added a cross-file **integral-corrections** phase to
`Companion.remember` (`src/marginalia/companion/__init__.py`). After a run's
claims are committed, the phase runs the correction judge and
`supersede_contradicted` (`src/marginalia/companion/_incremental.py`) to
auto-supersede an older claim that a newer, contradicting claim corrects. It
runs on **every** ingest.

The phase was **post-commit but UNGUARDED**. The claims for the current file are
already durably committed before it runs, so it is a best-effort nicety — yet a
single throw inside it (a store mutation error in `_apply_supersede`, or a
provider timeout in the correction judge / prefilter) propagated out of
`remember` and aborted the caller. During a trust-root rebuild
(`_build_fresh_graph` in `src/marginalia/cli/kg.py`, ADR 0007), each file is
ingested in sequence, so one bad supersede on file N killed the **entire
multi-file rebuild** even though every file up to N had already committed
cleanly.

Observed live: the `reference-eval` rebuild failed at **file 34 of 48** when a bad
`_apply_supersede` threw inside the correction phase, taking down the whole run.

The failure was also **undiagnosable**: the curation job runner's per-job
isolation in `_drain` (`src/marginalia/server/_jobs.py`) caught the exception
and persisted only `job.error = str(exc)` — a terse one-liner — to the sidecar,
swallowing the traceback and the chained `__cause__`. The real failure never
reached the server log.

## Decision

Make the ADR-0022 correction phase **truly best-effort** — it must never abort a
committed ingest — and make any failure diagnosable.

Two-layer guard on the correction phase:

- **Fine-grained**, inside `supersede_contradicted`
  (`src/marginalia/companion/_incremental.py`): wrap each individual
  `_apply_supersede` call in `try/except`. A single bad supersede is logged with
  claim context (`old_claim`, `new_claim`, `source_path`) and **skipped** so the
  remaining corrections in the batch still apply.
- **Outer net**, in `Companion.remember`
  (`src/marginalia/companion/__init__.py`): wrap the whole correction phase (the
  correction-judge construction plus the `supersede_contradicted` call) so a
  failure in the judge/prefilter itself is logged and execution continues to
  `finish_run`. The claims are already committed above this block.

In **both** guards, `RebuildInterrupted` is re-raised, never swallowed — a
SIGINT/SIGTERM rebuild abort is control flow, not a best-effort correction
error.

Surface the previously swallowed tracebacks with `logger.exception` (which
attaches `exc_info` and the chained cause) at two throw sites:

- `_drain` in `src/marginalia/server/_jobs.py` — the per-job isolation handler
  now logs the full traceback + `__cause__` before persisting the terse
  `job.error`.
- `_build_fresh_graph` in `src/marginalia/cli/kg.py` — logs the real underlying
  cause at the per-file throw site before wrapping it in `IngestError`, so the
  failure is diagnosable even when a caller only surfaces the terse
  `IngestError` message.

Because this phase is post-commit, bulk-ingest cancellation does not abandon
ledger finalization here. `supersede_contradicted` accepts a stop predicate,
checks it before and after each correction-judge call, and skips the remaining
best-effort checks when requested. An active CLI judge process is registered
against that predicate so it can be terminated without affecting unrelated LLM
calls. The committed claims remain valid and the unfinished corrections can be
revisited by a later ingest.

## Consequences

- A rebuild is now **resilient** to a bad supersede: one failing correction is
  logged and skipped, the rest still apply, and the multi-file rebuild runs to
  completion instead of aborting mid-way.
- Corrections stay best-effort by design: because the claims are already
  committed, a skipped correction leaves the graph correct-but-uncorrected, not
  corrupted, and the next ingest of the source can re-apply it.
- Cancellation skips the remaining sequential correction calls but still closes
  the candidate-ledger run as completed, recording `corrections_stopped` in its
  summary.
- Failures are **diagnosable**: the correction guards log claim-level context,
  and `_drain` / `_build_fresh_graph` now emit the full traceback and cause
  chain to the server log instead of a terse one-liner.
- `RebuildInterrupted` still propagates through both guards, so rebuild
  cancellation semantics are unchanged.
- Verified end-to-end: the `reference-eval` **rebuild-2** on the fixed code cleared
  file 34 (which aborted the prior run) and completed **48/48**.
- Guarded by `tests/companion/test_incremental_ingest.py`.
