# Quality gate runbook — the three tiers

One command answers "did I break knowledge quality?":

```bash
./tests/golden/bin/quality-gate.sh
```

It runs three tiers in one pass. Two of them can fail the gate; one never can.
Everything below is what each tier measures, when to run it, and what a pass
does *not* mean today.

## Tier 0 — deterministic floor (CI, zero tolerance)

Owned by `.github/workflows/eval-gate.yml`. Four model-free, daemon-free,
bit-reproducible steps over the committed `synthetic-ci` dataset and its frozen
vault: provenance byte-hash, `recall_floor` selftest, hard-recall@k +
extraction-completeness against `recall_floor_baseline.json`, and the run
manifest. Roughly five seconds of compute.

`quality-gate.sh` runs those same four steps in-process so the laptop reproduces
exactly what CI gates, and aborts everything below on any Tier 0 failure. It
does not edit that workflow and does not relax that baseline. Tier 1 failing
never excuses a Tier 0 failure, and the reverse is not a trade either.

```bash
./tests/golden/bin/quality-gate.sh --tier0-only    # CI parity, no model, no daemon
```

## Tier 1 — the laptop gate (live ingest, threshold-gated)

This is the tier that can see an extraction regression, which is the whole point:
this project's own measured finding is that retrieval is saturated and extraction
is the lever, and a frozen vault cannot see extraction move.

The gate starts a **suite-owned** daemon (isolated `HOME`/`XDG_*`, a vault under
its own temp dir, free non-advertised ports, and a server-identity assertion
against that pid and vault), then **live-ingests** the six
`datasets/semantic-adversarial/inputs/` documents into that fresh vault and runs
its thirteen questions through `/api/v1/recall` and `/api/v1/ask`. That identity
assertion reads the credential-free `/api/v1/status`, which carries the real
`pid` and `vault_path`, and it is fail-closed: if either field is missing the
gate refuses to measure the daemon rather than passing vacuously. `/health` is
deliberately liveness-only and cannot be used for it. It never
touches a user vault, `~/.okto-neuron` state, or the daemon on :7777. Provider
credentials are the one thing copied into the isolated home, because they are
application-scoped rather than vault state.

Four measurements, all gated on **counts** against
`datasets/semantic-adversarial/quality_gate_baseline.json`, the same
count-drop-fails discipline as the Tier 0 baseline:

| Metric | Source | Denominator |
|---|---|---|
| citation byte-verification | `floor_metrics.py floor-laptop`, live vault | verifiable citations |
| hard-recall@k | `recall_floor.py probe`, live vault, no daemon | gold targets |
| extraction-completeness | same probe | gold targets |
| deterministic `must_contain` | `quality_gate.py must-contain` | the 11 positive questions |
| negative-control abstention | same | the 2 negative controls |

The denominators are deliberately separate. A question with an empty
`must_contain` would score vacuously true, so it is never counted in the positive
band, and the two negative controls are scored on whether the answer *declines*
rather than on string presence. An empty answer is recorded and fails the gate;
the harness also aborts after three consecutive ask transport failures, so a dead
ask path can never be recorded as a clean run of empty results.

The pinned lineup lives at the top of the script: the private-LAN LiteLLM
gateway `http://<private-lan-gateway>:4000` with model
`desktop/qwen3.6-35b-10-parallel`, the one approved parallel-capable alias, so
extraction is not clamped to a single in-flight completion. A drifting model
would turn the gate into a model-comparison instrument, so overriding the model
is an explicit environment opt-in, not a default.

The gateway address is *not* pinned in the tracked script. It is a private-LAN
address, so `OKTO_NEURON_QUALITY_GATE_API_BASE` is **required** for Tier 1/2 and
has no default; the script exits `64` with instructions when it is unset.
`--tier0-only` is model-free and needs nothing. The report and any minted
baseline record the gateway as the literal placeholder `<private-lan-gateway>`,
never the raw address, so `--mint-baseline` cannot write a private endpoint into
version control. That meta field is outside `metric_payload_sha256`, which
covers the `metrics` object only, so redacting it does not disturb the floors.

```bash
export OKTO_NEURON_QUALITY_GATE_API_BASE=http://<private-lan-gateway>:4000
```

Embedding is deliberately *not* re-pointed at the LAN embedding alias. The gate
keeps the vault's own default local fastembed embedder for three reasons: it is
the embedder Tier 0 already gates against, it keeps the retrieval leg
deterministic and offline, and switching to a wider remote alias means replacing
the graph file `okto-neuron init` just minted, which trips the ADR 0039
generation fence and disables semantic writes for that generation. Only the LLM
legs — extraction, ask, and the advisory judge — go to the gateway.

## Tier 2 — advisory semantic band (never gates)

The reference-guided semantic judge grades the same thirteen answers in the same
run. Its tally is written into `quality_gate_report.json` under `tier2_advisory`
and is **never** part of the pass/fail decision.

This is not a preference for deterministic scoring. Semantic grading is the
metric we want; the problem is calibration. The open-ended judge panel measured
Fleiss kappa 0.14, and the reference-guided judge that replaced it has never had
its kappa measured against human labels. **Tier 2 hardens into a gate only after
a measured kappa >= 0.60 versus human labels is committed to this repository.**
Until then, reporting an uncalibrated band is honest and gating on one would be
worse than the deterministic signal it replaced.

## The baseline is a floor across runs, not one lucky run

Extraction is stochastic. Five runs of the *identical* tree on 2026-07-28
produced 11/11, 7/11, 11/11 and 10/11 gold targets — a ~36% swing with no code
change at all. A baseline minted from one good run therefore fails the next
unchanged run, which is how a gate gets ignored.

So `--mint-baseline` mints the **element-wise minimum** across every retained
run report of the same source SHA and lineup, and records how many runs went
into it. The gate then answers "has any run ever been worse than this?", which
is a question a count-drop gate can actually defend. Wall time is roughly
170 seconds per run, so re-minting across several runs costs minutes, not hours.

That run-to-run variance is itself a finding worth carrying into the ADR 0040
work: on a six-document corpus, single-run answer counts are not a stable
quality signal, and any claim built on one run should be treated accordingly.

The first committed baseline, minted 2026-07-28 from the then-current source across six
runs at roughly 170 seconds each:

| Metric | Floor | Observed across the six runs |
|---|---|---|
| citation byte-verification | 130 / 130, floor pass | stable at 130 / 130 |
| hard-recall@k (k=10) | 7 / 11 | 7–11 |
| extraction-completeness | 7 / 11 | 7–11 |
| claim coverage | 7 / 11 | 7–11 |
| `must_contain` | 7 / 11 | 7–11 |
| negative-control abstention | 1 / 2 | 1–2 |
| Tier 2 advisory (never gated) | not gated | 9–13 CORRECT of 13 |

Citation byte-verification is the one metric that did not move at all, which is
what you would expect from a deterministic byte-level check and is a useful
sanity signal in its own right: if that one drops, the problem is provenance,
not sampling.

## The baseline is PROVISIONAL

`quality_gate_baseline.json` carries `"provisional": true`. It was minted on a
tree with open ADR 0039/0040 findings still under adjudication, so a Tier 1 pass
means "no regression against a tree with known open findings", not "quality
proven". **It re-mints once ADR 0040 adjudication closes**, from a blessed run on
that tree:

```bash
./tests/golden/bin/quality-gate.sh --mint-baseline
```

The report records the source SHA, the dataset, and the pinned lineup, so a
baseline can always be traced to the tree and stack that produced it.

## When to run what

- Before declaring any substantive change done: the full gate. Measured wall
  time on the reference laptop is about two and a half minutes (146s and 158s on
  two consecutive cold runs of the same commit), consistent with the ~170s per run the
  baseline was minted at. The script prints its own wall time and warns past nine
  minutes; that warning threshold is deliberately loose headroom for a busy model
  host, not the expected duration.
- To reproduce what CI will do, cheaply: `--tier0-only`.
- To skip the advisory band when the model host is busy: `--no-judge`. Nothing
  gated is lost, because Tier 2 gates nothing.

## Deep evals — opt-in, hours not minutes

None of these are on the default path, and none of them is the gate:

- `eval-run.sh <dataset> --panel` — the multi-hour private-corpus-class run.
- `bin/acceptance.sh --realmodel` (scenario 56) — live-model extraction acceptance.
- `bin/acceptance.sh --private` (scenarios 90/91) — private corpus, laptop-only, never CI.
- `tests/golden/longmemeval/` — 35 isolated vaults; non-gating by its own manifest,
  and its corpus is not materialized in-repo.
- The ADR 0040 67-slot evidence matrix — release-evidence collection, not a
  per-change gate.

`tests/golden/bin/archive/` holds retired apparatus kept for reference:
`panel.py`, the kappa=0.14 N-judge panel. It gates nothing and is retained for a
future calibration study.
