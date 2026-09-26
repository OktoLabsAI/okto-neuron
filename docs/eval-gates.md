# Eval gates — the canonical Definition-of-Done for the graph-native arc

> ADR 0019 Phase-0 / GN-8. One shared set of ship gates so every task in the
> graph-native answer-assembly arc is judged against the same bar. Code is
> canonical; this doc is derived — if a path or command here drifts from the
> scripts, the scripts win.

There are **three** gates. Only **one** of them blocks CI. The other two are
laptop instruments whose *logged results* drive the flip-default decision — they
are not exit-code blockers.

A run prints all three as `[gate]` lines via `tests/golden/bin/eval-run.sh`.

---

## Gate 1 — DETERMINISTIC FLOOR (CI-provable, judge-free — the ONLY CI gate)

The trust anchor. No LLM, no daemon, bit-reproducible. A regression on any gated
metric is a hard failure (`exit 1`).

**Two legs, both deterministic:**

1. **Provenance byte-hash** — `judge.py floor` over the dataset's frozen quotes.
   A pure function of the committed `inputs/` files (no graph). For every
   gold/distractor quote, re-slice it out of the source, assert present + unique,
   record byte offsets + sha256.

2. **Recall / extraction / claim-coverage** — `recall_floor.py gate` over a
   frozen, committed Ladybug vault (`datasets/synthetic-ci/frozen-vault/`), no
   daemon, no LLM. Gated metrics, each **zero-tolerance** against
   `recall_floor_baseline.json`:
   - **hard-recall@k** (`--k 10`) — is each gold target's source file in the top-k?
   - **extraction-completeness** — is *any* Claim anchored to the gold quote's Block (file-level)?
   - **claim-coverage (NEW, GN-1)** — does a Claim whose `byte_span` *covers this
     specific quote* exist? Stricter than extraction-completeness.

   The full gated scalar payload is pinned by a single
   `metric_payload_sha256` — two runs produce a byte-identical report.

**Commands**
```bash
# leg 1 — provenance (no vault)
uv run python tests/golden/bin/judge.py floor \
  tests/golden/datasets/synthetic-ci --out floor_report.json

# leg 2 — recall + extraction + claim-coverage (frozen vault, [ladybug] extra REQUIRED)
uv run --extra ladybug python tests/golden/bin/recall_floor.py gate \
  --vault tests/golden/datasets/synthetic-ci/frozen-vault \
  --questions tests/golden/datasets/synthetic-ci/questions.yaml \
  --baseline tests/golden/datasets/synthetic-ci/recall_floor_baseline.json \
  --k 10 --out recall_floor.json

# logic selftest (no vault)
uv run python tests/golden/bin/recall_floor.py selftest
```

**CI:** `.github/workflows/eval-gate.yml` (job `floor`) runs all of the above on
every push/PR to `main`. Failure = `exit 1`. To make it *block* a merge, mark
`eval-gate / floor` a required status check on `main` with "include administrators".

**Note:** **neg-guard is NOT a floor metric.** It needs the LLM answerer to
decline an unanswerable question, so it lives in Gate 2, not here.

---

## Gate 2 — MULTI-RUN A/B SIGNIFICANCE (laptop-only, NOT CI)

Where the block-vs-subgraph (or any arm-vs-arm) flip-default decision is decided.
Temp > 0, run the same pair **N ≥ 5** times to measure variance, then apply the
**5-condition REAL rule** — a delta is `REAL` only if **all five** hold:

1. **directional** — the sign of the delta is consistent.
2. **significant** — McNemar exact two-sided p < α (default 0.05).
3. **powered** — |delta| ≥ MDE@0.80 (this N can resolve a delta this size).
4. **ci_excludes_zero** — the paired bootstrap 95% CI for the delta does not straddle 0.
5. **material** — |delta| ≥ the material threshold (default ≥ 5 pt).

Reports `median[IQR]` bands across the runs. **Neg-guard is measured here**: the
`negative_control` rows are graded by `proxy_correct_from_ask` over each
question's `must_contain` (the answerer must correctly *decline*). Used for the
flip-default decision, **never a CI blocker**.

**Command**
```bash
# multi-run band (laptop, daemon up). Pre-captured arms or run-both live capture.
uv run python tests/golden/bin/judge.py ab-runset --config '{
  "run_both": true, "endpoint": "http://127.0.0.1:7777",
  "questions": "tests/golden/datasets/<ds>/questions.yaml",
  "N_runs": 5, "seed_base": 1000 }'
```

`eval-run.sh` with `--baseline` runs a **single-pair** scorecard and logs the
5-condition classification as a directional signal; the powered band still
requires `ab-runset` with N ≥ 5.

---

## Gate 3 — COST BUDGET (tier tag, not an exit code)

How expensive a change in this arc is. Drives the flip-default decision via the
logged tier, not a pass/fail.

- **Tier-1** — read-path only, **hours**, **NO re-ingest**. Assembly-side fixes.
- **Tier-2** — **days**, **re-ingest REQUIRED**, reference-eval only (per the locked
  ADR 0019 decision). Extraction-side changes that re-mint Claims.

**How tier is tagged:** `tests/golden/bin/manifest.py` reads an optional
`tier_cost.json` in the dataset dir and pins it into the run manifest under
`run_meta.tier_cost` (excluded from `assert-arms` parity — cost tier is metadata,
not a comparability axis). Absent file → safe default `tier-1 / needs_reingest=false`.

```json
// tests/golden/datasets/<ds>/tier_cost.json  (optional)
{ "tier": "tier-2", "needs_reingest": true, "note": "re-mints Claims; reference-eval only" }
```

`eval-run.sh` emits `[gate] cost-budget tier-1 reingest=False` from that field.

---

## Binding constraints (from ADR 0019)

Every gate operates under, and must not violate, these:

- **5-primitive closed schema.** `Agent · Activity · InformationObject · Concept ·
  Place` + 6 support types. A sixth primitive needs a new RFC.
- **Claim → Block byte anchoring.** The atomic provenance unit is a `Claim`
  anchored to a `Block` at `(path, byte_start, byte_end, content_hash)`. No
  ingest path may bypass it.
- **Markdown is the trust root.** The vault is canonical; the graph is derived.
  Never delete graph files to fix a KG issue — rebuild from the vault.
- **Deterministic-floor-is-the-only-CI-gate.** The judge stays non-authoritative
  pending the human kappa. Only Gate 1's bit-reproducible legs gate CI; Gates 2
  and 3 inform decisions, they do not block merges.

---

## The three gates at a glance

| Gate | What | Where | Blocks CI? |
|------|------|-------|-----------|
| 1 — deterministic floor | provenance byte-hash + hard-recall@k + extraction-completeness + claim-coverage | `judge.py floor`, `recall_floor.py gate`; `.github/workflows/eval-gate.yml` | **YES** (`exit 1`) |
| 2 — A/B significance | 5-condition REAL rule, median[IQR], neg-guard | `judge.py ab-runset` (N≥5, laptop) | No |
| 3 — cost budget | tier-1 (read-path) vs tier-2 (re-ingest) | `manifest.py` → `run_meta.tier_cost` | No |

---

## Run-validity guards (harness + daemon; added 2026-07-07)

A gate can only judge a run that actually measured something. The 2026-07-07
`ab-seed-quality` A/B produced `"ask": {}` for all 142 questions in BOTH arms:
the daemon's shared `.venv` had `litellm` pruned out from under it by an exact
`uv run` re-sync (litellm is not in `default-groups`), every `/api/v1/ask`
500'd with `ModuleNotFoundError`, and `run-golden.sh` swallowed each HTTP
failure into `{}`. Two guards now prevent a repeat:

1. **Daemon boot warmup** (`okto_neuron.server.runtime._warm_llm_provider_dependencies`
   → `okto_neuron.llm.warm_provider_dependencies`): `okto-neuron serve` imports the
   configured LLM providers' lazy dependencies (litellm, boto3) ONCE at boot,
   pinning them in `sys.modules` for the process lifetime — a later venv
   re-sync cannot break a running daemon. A missing dependency logs a single
   loud ERROR at startup instead of a silent per-request 500.
2. **Ask transport-failure abort** (`run-golden.sh`): a single flaky ask is
   tolerated (recorded as `{}`), but **3 consecutive** `/api/v1/ask` transport
   failures abort the run with a FATAL log line — an all-empty run must die in
   seconds, not consume a 50-minute A/B measuring nothing.

Operational corollary: launch eval daemons with `uv run --group litellm
okto-neuron serve …` so the warmup finds litellm at boot; the harness's own
`uv run python3` calls may re-sync the venv afterwards without harm.
