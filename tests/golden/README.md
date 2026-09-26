# Golden-Dataset Test Framework

A **black-box, deterministic** harness for iterating on Okto Neuron's end-to-end
quality (ingest → graph build → recall/ask), plus a **repeatable process** for
minting golden datasets.

Two clean halves:

- **The harness is an engine.** `bin/run-golden.sh` is deterministic and
  dataset-agnostic — point it at *any* dataset directory and it runs the full
  loop and reports. It contains no question text and no answers. It touches
  Okto Neuron **only** through the public CLI + HTTP surface, exactly like a
  human. It never `import okto_neuron`.
- **A dataset is data + a documented process.** Everything under
  `datasets/<name>/` is pluggable: the source docs, the curated Q&A, and the
  audit trail of how the ground truth was derived.

## Layout

```
tests/golden/
  bin/
    quality-gate.sh   # THE laptop gate — Tier 0 (CI parity) + Tier 1 (live) + Tier 2 (advisory)
    quality_gate.py   # gate scoring: must_contain + abstention, report merge, count-drop gate
    eval-run.sh       # THE orchestrator — one resumable command for the whole chain
    run-golden.sh     # the recall/ask engine — deterministic, dataset-agnostic
    _serve.sh         # serve/stop/health helpers (real `okto-neuron serve`)
    judge.py          # black-box entry point: provenance + judge + merge + the
                      #   floor / manifest / scorecard / sweep / panel / floor-laptop subcommands
    manifest.py       # run-manifest reproducibility pin + multi-arm parity guard
    scorecard.py      # paired A/B significance + power (+ multi-seed band + auto-escalate)
    sweep.py          # model×knob comparative grid vs a baseline cell
    floor_metrics.py  # laptop floor: citation byte-verify + extraction-completeness
    new-dataset.sh    # scaffolds a new datasets/<name>/ skeleton
    archive/
      panel.py        # ARCHIVED: N-judge 2/3-vote panel (the κ=0.14 apparatus). Gates
                      #   nothing; retained for a future calibration study.
  datasets/
    <name>/
      dataset.yaml    # manifest: name, source provenance, settings (k, embedder)
      inputs/         # the source docs — the test's INPUT set (committed)
      questions.yaml  # QUESTION -> expected_answer, tier, sources, must_contain (committed)
      CREATING.md     # how THIS dataset's ground truth was derived (audit trail)
  results/            # gitignored: raw responses, measured semantic/recall cost,
                      # deterministic provenance, judge output, and report.json
```

## The gate — start here

```bash
# Tier 1/2 REQUIRE the private-LAN LiteLLM gateway; there is no committed default.
export OKTO_NEURON_QUALITY_GATE_API_BASE=http://<private-lan-gateway>:4000

./tests/golden/bin/quality-gate.sh              # all three tiers, ~8 min, laptop
./tests/golden/bin/quality-gate.sh --tier0-only # exactly what CI gates, model-free (no env needed)
./tests/golden/bin/quality-gate.sh --no-judge   # skip the advisory Tier 2 band
```

Without that variable the gate exits `64` rather than guessing an endpoint;
`--tier0-only` is model-free and needs nothing. See `docs/quality-gate-runbook.md`.

- **Tier 0** — the unchanged CI deterministic floor (`.github/workflows/eval-gate.yml`),
  re-run in-process for laptop parity. Any Tier 0 failure aborts the rest.
- **Tier 1** — a suite-owned daemon live-ingests `datasets/semantic-adversarial/inputs/`
  into a fresh vault (the only leg that can see an extraction regression), then gates
  citation byte-verify, hard-recall@k, extraction-completeness, `must_contain`, and
  negative-control abstention on COUNTS vs `quality_gate_baseline.json`.
  That baseline is **PROVISIONAL** — see `docs/quality-gate-runbook.md`.
- **Tier 2** — the semantic judge reports an advisory band and **never** fails the gate.
  It hardens only after a measured κ ≥ 0.60 against human labels is committed.

### Deep evals — opt-in, hours not minutes

Never on the default path, and none of them is the gate: `eval-run.sh <dataset> --panel`
(the multi-hour private-corpus-class run), `bin/acceptance.sh --realmodel` (scenario 56),
`bin/acceptance.sh --private` (scenarios 90/91, laptop-only, never CI),
`tests/golden/longmemeval/` (35 isolated vaults, non-gating by its own manifest, corpus
not materialized in-repo), and the ADR 0040 67-slot evidence matrix.

## Running

### One resumable command (the orchestrator)

`bin/eval-run.sh` chains the whole pipeline and is **resumable**: each step writes
a named artifact into a stable run dir (keyed by dataset + `--id`, not a
timestamp), and a re-run **skips** completed expensive steps whose artifacts
already exist (`--force` redoes them; delete one artifact to redo just that
step). The cheap deterministic report merge always runs so newly completed
judge or sidecar evidence cannot leave `report.json` stale.

```bash
# deterministic legs only (manifest + floor): NO daemon, NO LLM — the CI-provable path
./tests/golden/bin/eval-run.sh <dataset-name-or-path> --ci

# full pipeline as a pure client of a running daemon:
#   manifest -> floor -> recall+ask -> semantic -> judge|panel -> floor-laptop -> scorecard -> report
./tests/golden/bin/eval-run.sh <dataset-name-or-path> --endpoint http://127.0.0.1:7777 \
    [--vault-path /absolute/server/vault] [--bound questions.bound.yaml] [--panel] \
    [--baseline ARM.jsonl] [--grader proxy|judge]

# read-only audit of an already-populated matching endpoint; never enqueues ingest
./tests/golden/bin/eval-run.sh <dataset-name-or-path> --endpoint http://127.0.0.1:7777 \
    --never-ingest [--no-judge]

# explicit ADR 0040 identical-reingest arm; requires an exact corpus match and
# an explicit vault selector before it enqueues anything
./tests/golden/bin/eval-run.sh <dataset-name-or-path> --endpoint http://127.0.0.1:7777 \
    --vault-path /absolute/server/vault --force-reingest [--no-judge]

# alternate/migrated questions over the same immutable input corpus
./tests/golden/bin/eval-run.sh <dataset-name-or-path> --questions /path/questions.yaml --ci
```

The chain: **1** `manifest` (reproducibility pin) · **2** `floor` (CI provenance
byte-hash gate) · **3** recall+ask (delegates to `run-golden.sh --endpoint`, which
census-skips ingest on an already-populated vault) · **4** `semantic` (posts every
captured `recall_cost.v1` sample to the server-owned semantic evaluator) · **5**
`judge` (or `--panel`) · **6** `floor-laptop` (citation byte-verify +
extraction-completeness) · **7** `scorecard` vs a baseline arm · **8** `report`
merge. Steps 1–2 are deterministic (no daemon/LLM); 3–7 are laptop-only. The LLM
judge/panel verdicts feed the scorecard but **remain non-authoritative pending the
human κ**; the deterministic legs do not.

The stable run directory is keyed by the input/question content fingerprint and
keeps `responses.jsonl`, `semantic-quality.json`, and `deterministic.json`; the final report
embeds both sidecars, so a completed report
must not show null provenance or missing recall-cost evidence. A live run fails
closed if any question lacks `recall.recall_cost`, or if the semantic endpoint
does not account for every supplied sample. A resumed run validates the retained
sidecar and regenerates only that server-owned aggregation when needed. The
sidecar is bound to the exact `responses.jsonl` bytes by SHA-256, so a same-size
or same-question-count stale capture is not resumable. The orchestrator also
retains the `litellm` dependency group on every `uv` invocation. This matters
because an exact default-group sync keeps
FastEmbed through the default `serve` group but removes LiteLLM.

Manifest version 5 pins the path-independent raw SHA-256, byte size, question count, and global
`settings.k` of the effective question file. `--questions` is valid in deterministic, single-arm,
and A/B modes, so an alternate set cannot be invisible to parity or resume evidence.

For a shared multi-vault daemon, `--vault-path` is mandatory operational discipline even though it
is optional for backward compatibility. The orchestrator propagates the selector through every
config, identity, ingest, recall, ask, semantic-quality, and floor-laptop request. Manifest v5 pins
only its SHA-256, never the personalized absolute path, and node-probe caches include the selector
so two vaults on one endpoint cannot share cached evidence.

The semantic endpoint currently accepts at most 1,000 recall samples, so a live
Golden dataset must contain at most 1,000 questions. The harness aborts before
`/ask` if `/recall` does not return measured `recall_cost.v1`, avoiding a paid
run that cannot produce the required evidence. It also rejects duplicate input
basenames before ingest because the HTTP source boundary flattens directories;
accepting both `a/notes.md` and `b/notes.md` would silently overwrite one source.

Regenerating a missing or invalid semantic sidecar during resume requires a
populated endpoint whose complete dataset identity was verified in that same
orchestrator run. An empty or wrong-corpus endpoint is never used to describe
the graph layers associated with retained recall responses.

The semantic capture retries only the endpoint's named, retryable `audit_busy`
409 with a bounded, visible backoff. If that contention outlives the retry
budget, the engine retains a response batch only when its post-loop completion
marker and JSONL line count agree; a resumed run then starts at semantic capture
instead of paying for the same `/ask` calls again. Partial question batches are
never promoted into the stable run directory.

`run-manifest.json`'s `run_meta.tier_cost.needs_reingest` classifies the change
being evaluated (from the optional dataset `tier_cost.json`). It does **not** say
whether this particular endpoint run ingested files. The `eval-run.log` lines
`vault empty — ingesting` or `vault populated — opening, not re-ingesting` are
the evidence for that execution-level fact. Before a populated endpoint is
reused, the harness also compares its complete `Document` byte-hash multiset to
the selected dataset and fails closed on a mismatch or unverifiable identity.
`--never-ingest` strengthens that contract for an audit run: it requires a
populated endpoint, skips queue draining because the run owns no queue items,
and fails instead of filling an empty graph. When the engine does enqueue a
batch, it waits only for the exact item IDs returned by that POST; unrelated
active work and historical queue errors cannot create either a false pass or a
false failure.

Manifest v5 also pins the effective consolidation policy, ingest chunking, and
extraction/embedding execution limits. Construction-stage ablations for
`consolidation.type_adjudication_enabled` or
`consolidation.relation_curator_enabled` may use distinct explicit vaults because
each arm must materialize an isolated graph; `assert-arms` still requires the
same corpus, code, provider/model, every non-free consolidation field, and every
execution limit. Read-path A/B comparisons continue to require the same vault.

`<dataset-name-or-path>` accepts either a name under `tests/golden/datasets/` or
an external dataset directory containing `inputs/` and `questions.yaml`.

#### Private golden datasets (laptop-only, never tracked)

A golden dataset built from private material must never enter this repository, CI, or any
tracked file. Keep it outside the checkout and point the harness at it with an opt-in
environment variable:

```bash
export OKTO_NEURON_PRIVATE_GOLDEN_DIR=/absolute/path/to/private-golden
./tests/golden/bin/run-golden.sh my-private-dataset --endpoint URL --never-ingest
```

Contract:

- `OKTO_NEURON_PRIVATE_GOLDEN_DIR` must be an absolute path and defaults to
  **unset**. Unset means no private lookup happens at all and an unknown dataset
  name still fails with `exit 64`, so CI behaviour is unchanged.
- Layout is the same as an in-repo dataset:
  `$OKTO_NEURON_PRIVATE_GOLDEN_DIR/<name>/{inputs/,questions.yaml,dataset.yaml,CREATING.md}`.
- Lookup order is unchanged and in-repo wins: an explicit directory path, then
  `tests/golden/datasets/<name>`, then the private root. The selector must be a
  bare name (no `/`, no leading `.`).
- Both `run-golden.sh` and `eval-run.sh` share one resolver
  (`bin/_dataset_dir.sh`), so the two entry points cannot drift.
- Run artifacts are still written under `tests/golden/results/<name>/…`, which
  is gitignored but *inside* the checkout. Treat that output as private too:
  never stage it, and prune it after a private run.
- Related but separate: acceptance scenarios 90/91 discover their own private
  corpus through `OKTO_NEURON_PRIVATE_CORPUS`, `OKTO_NEURON_PRIVATE_CORPUS_VAULT`,
  `OKTO_NEURON_PRIVATE_CORPUS_PROBES`, and `OKTO_NEURON_PRIVATE_CORPUS_QUESTIONS`.

### The recall/ask engine directly

```bash
./tests/golden/bin/run-golden.sh <dataset-name-or-path>              # full live run
./tests/golden/bin/run-golden.sh <dataset-name-or-path> --no-judge   # live /ask; skip only grader
./tests/golden/bin/run-golden.sh <dataset-name-or-path> --keep-vault # keep temp vault for inspection
./tests/golden/bin/run-golden.sh <dataset-name-or-path> --endpoint URL --never-ingest
```

`--no-judge` does **not** make a run deterministic or LLM-free: ingest and every
`/api/v1/ask` call still use the configured live model. It disables only the
separate scripted judge/panel grading leg. Use `eval-run.sh ... --ci` for the
daemon-free, LLM-free deterministic floor.

Every HTTP operation is bounded independently so a restarted daemon or stalled
provider cannot pin the whole harness behind one `curl`. Defaults are 5 seconds
to connect, 30 seconds for control requests, 60 seconds for `/recall`, 180
seconds for `/ask`, and 5,400 seconds for synchronous ingest. Override them with
`OKTO_NEURON_GOLDEN_HTTP_CONNECT_TIMEOUT_S`, `OKTO_NEURON_GOLDEN_HTTP_TIMEOUT_S`,
`OKTO_NEURON_GOLDEN_RECALL_TIMEOUT_S`, `OKTO_NEURON_GOLDEN_ASK_TIMEOUT_S`, and
`OKTO_NEURON_GOLDEN_INGEST_TIMEOUT_S`; all values must be positive integers.
These are client-harness supervision bounds, not Okto Neuron provider timeouts.

Every non-negative question must declare at least one `gold_target`. The harness
rejects the dataset before scoring otherwise, so an empty 0/0 evidence set can
never pass. Negative controls are the only questions allowed to omit targets.
Question IDs must be non-empty and unique. `questions.yaml` `settings.k` is the
single top-k value for the dataset: per-question overrides, CLI values, retained
responses, deterministic evidence, reports, and recall baselines must all match
it. Judge crashes and incomplete semantic verdicts are infrastructure failures;
only an explicit `--no-judge` is a requested grader omission. Panel and
floor-laptop artifacts are schema-checked before resume; incomplete votes or a
failed/unmeasured citation floor stop the run instead of becoming sticky cache.

### Standalone subcommands (`judge.py` entry point)

Run them through the repository's `uv` environment. PyYAML is Okto Neuron's
authoritative YAML parser; the harness intentionally fails closed when that core
dependency is unavailable rather than approximating questions or byte-exact
evidence with a partial parser.

```bash
# DETERMINISTIC, CI-safe (no daemon/LLM/vault):
judge.py floor <dataset-dir> --out floor_report.json [--baseline frozen.json]
judge.py manifest <dataset-dir> --out run-manifest.json [--free-variable model]

# LIVE DAEMON, deterministic aggregation owned by the application:
judge.py semantic-quality --endpoint URL --responses responses.jsonl \
  --out semantic-quality.json

# DETERMINISTIC ADR 0040 evidence from an already-captured live run:
judge.py byte-grounding --corpus-id ID --inputs <dataset-dir>/inputs \
  --responses responses.jsonl --manifest run-manifest.json \
  --semantic-quality semantic-quality.json --out byte-grounding.json

# DETERMINISTIC, CI-safe (frozen vault, no daemon/LLM) — recall + extraction floor:
recall_floor.py gate --vault <dataset-dir>/frozen-vault \
  --questions <dataset-dir>/questions.yaml \
  --baseline  <dataset-dir>/recall_floor_baseline.json --k 10 --out recall_floor.json

# LAPTOP-ONLY (live vault + LLM):
judge.py floor-laptop --responses R.jsonl --bound B.yaml --endpoint URL --out floor_laptop.json
judge.py scorecard --a A.jsonl --b B.jsonl --grader proxy            # paired A/B significance
judge.py scorecard-multiseed --runs runs.json                        # per-metric median[IQR] band
judge.py panel --responses R.jsonl --questions Q.yaml --out panel.json

# selftests (no network): scorecard.py selftest · floor_metrics.py selftest · recall_floor.py selftest
```

- **Three deterministic CI gates (all NO-LLM, NO-DAEMON).**
  1. **`judge.py floor`** — provenance byte-hash. A pure function of `inputs/` +
     `questions.yaml`, byte-stable, no vault at all.
  2. **`recall_floor.py gate`** — **hard-recall@k** + **MRR@k** + **precision@k** +
     **extraction-completeness** over the committed **frozen vault**
     (`datasets/<name>/frozen-vault/graph.lbug`). All ranking metrics run the SAME
     retrieval `/recall` runs (`Vault.query` → `_search` → `query.search_claims`)
     and share ONE relevance rule (a hit's source file is a gold source file):
     hard-recall asks if each gold target's source file is in the top-k; MRR@k =
     mean over answerable questions of 1/(rank of first gold-relevant hit), 0 if
     none; precision@k = mean of (#gold-relevant hits in top-k)/k. The
     near-duplicate `distractors` in each question keep MRR/precision **< 1.0** so
     the floor cannot saturate. Extraction-completeness asks if a `Claim` is
     anchored to the gold quote's bytes (retrieval-independent). The gate compares
     against a committed `recall_floor_baseline.json`: a drop in hard-recall/
     extraction **count** (`--tolerance`, default 0) or MRR/precision **rate**
     (`--rate-tolerance`, default 0.0 — the ranking is deterministic so it gates
     exactly) ⇒ exit 1 (exit 2 = infra). The committed `.lbug` is opened on a
     **throwaway copy**, never in place, so a gate run leaves `git status` clean
     (`Vault.open` is read/write and would otherwise dirty the file). Needs the
     `[ladybug]` extra (`uv run --extra ladybug …`) because `Vault.open` loads the
     `.lbug` store.
- **Why a frozen `.lbug`, not a serialized JSON.** The faithful `/recall` ranking
  is `LadybugStore`'s fused RRF (BM25 + full-scan cosine + graph expansion).
  `InMemoryStore.search_text` is a different token-overlap scorer, so a
  JSON→InMemoryStore probe would gate the *wrong algorithm*. The ~5 MB `.lbug` is
  the only faithful form; it is force-added past `.gitignore`'s `*.lbug` rule and
  committed with the 14 source copies that extraction re-slices off disk.
- **Bit-reproducibility (proven).** The recall/extraction/ranking probe was run in
  independent processes over the frozen graph → byte-identical reports (sha256
  stable; per-question ranks incl. explicit `None` misses stable). The report
  carries a `metric_payload_sha256` over the GATED scalars (hard-recall + MRR@k +
  precision@k + extraction), so two runs hash identically iff every gated number
  matches. No ANN randomness (`query._vector_seeds` is a full cosine scan),
  deterministic fastembed, `LadybugStore.nodes_with_embeddings` ends in `ORDER BY
  n.id` + Python stable sort. The rates are NON-saturating on the LLM-ingested
  graph (hard-recall `0.4688`, MRR@10 `0.4348`, precision@10 `0.2`, extraction
  `0.5625`) — the old "recall saturates trivially" objection was about the *no-LLM*
  vault.
- **`floor-laptop` (still laptop-only).** The live-vault variants of these two
  metrics over a daemon are reported **separately**: (i) **citation
  byte-verification** re-slices every `/ask` citation's Block bytes and asserts
  `sha256 == content_hash`; (ii) **extraction-completeness** confirms a live
  `Claim` is anchored to each bound gold target's Block. Deterministic byte-hash /
  graph-membership checks, independent of the LLM judge. A citation floor with
  zero captured citations is explicitly unmeasured and fails; absence of evidence
  cannot become a vacuous pass.
- **`scorecard-multiseed`** reports each metric as a `median[IQR]` band across N
  grader runs (`--runs`) or N bootstrap seeds (`--a/--b --seeds N`), and the
  scorecard now emits an explicit **"ESCALATE to confirmation set (N≈…)"**
  recommendation when a delta is directional but under-powered (`|delta| < MDE`).

Exit code: `0` = the harness ran clean end-to-end. **Answer quality is NOT the
exit code** — it lives in the report. Non-zero means an infra/harness failure
(server died, ingest errored, queue stuck).

### Timing — this is a LONG-RUNNING test

**Ingest runs real LLM extraction synchronously** (often minutes per file, depending on the
explicitly configured model and endpoint).
A ~15–17 file dataset takes **~45–75 min end-to-end** — ingest dominates;
recall/ask is seconds each, judging is a few minutes.

- Declare the expectation per dataset in `dataset.yaml`:
  - `settings.ingest_timeout_s` — hard ceiling on the queue-drain wait
    (default **5400s / 90 min**). Raise it if your model is slower.
  - `expected_duration:` — a human-readable estimate block so nobody assumes a
    fast run.
- The harness logs **per-file ingest progress + live ETA** while draining the
  queue. A run that looks idle is almost always mid-extraction, **not hung** —
  check `server.log` before assuming failure.
- Exit `72` ("queue did not drain") means the build genuinely exceeded
  `ingest_timeout_s`, not that a few minutes elapsed. Run unattended.

What a run does (deterministic, no question text baked in):

1. Reads dataset settings and, in `--endpoint` mode, pins the server's effective
   LLM and embedding provider/model/dimension in `run-manifest.json`.
2. Creates a **fresh vault** in a run-scoped temp dir (never durable `/tmp`),
   `okto-neuron init`, with isolated HOME/XDG config, tokens, caches, and locks.
3. Starts the real server on loopback (free REST + MCP ports), waits for
   `/health`.
4. Ingests every file under `inputs/` via `POST /api/v1/ingest` (the human-like
   content-upload path).
5. Watches `GET /api/v1/ingest-queue` until `summary.active == false` and
   `summary.error == 0`.
6. Snapshots graph shape via `GET /api/v1/node-types`.
7. For each question (tier order): `POST /api/v1/recall` and `POST /api/v1/ask`,
   recording hits + provenance + answer text + citations into
   `results/<dataset>/<ts>/responses.jsonl`.
8. **Deterministic checks** (`judge.py provenance`): every byte-anchored hit's
   `(path, byte_start, byte_end)` is re-sliced from `inputs/` and must
   `sha256` back to `content_hash`.
9. **Scripted LLM judge** (`judge.py judge`, optional): sends expected answer +
   system answer + retrieved nodes to the OpenAI-compatible model explicitly configured through
   `OKTO_NEURON_LLM_BASE_URL` → verdict `{correct|partial|wrong|missed}`. A live-model run refuses
   to select an ambient endpoint implicitly.
10. Merges everything into `results/<dataset>/<ts>/report.json`.

## Grading is two layers

1. **Scripted** — deterministic provenance + the LLM-judge verdicts in
   `report.json`. Unattended / CI-friendly.
2. **Claude review readout** — after a run, Claude reads `report.json` + the raw
   responses and writes `readout.md`: a per-question table (tier, verdict,
   provenance ok?, what was right/wrong/missed, suspected cause) plus an overall
   tally and concrete next-iteration suggestions. This is the nuanced layer.

## Creating a new dataset (the repeatable, scalable process)

```bash
./tests/golden/bin/new-dataset.sh <name>
```

Then:

1. **Scaffold** (the command above) creates `datasets/<name>/` with an empty
   `inputs/`, template `dataset.yaml`, 4-tier `questions.yaml`, and `CREATING.md`.
2. **Select corpus** (~15 files): content-rich, self-contained, interconnected
   (shared people / concepts / decisions), **no secrets/PII** — screen during
   selection. Copy into `inputs/`. Record source paths + rationale in
   `dataset.yaml` and `CREATING.md`.
3. **Derive ground truth** — Claude reads the actual files and writes, per
   question: the exact answer, the tier, `expected_source_paths`, and
   `must_contain` key facts, with byte-anchored evidence noted in `CREATING.md`.
   Aim for 12–16 questions across the four tiers:
   - **T1** single-fact recall
   - **T2** single-doc synthesis
   - **T3** multi-doc / multi-hop reasoning
   - **T4** negative / absent ("not in the notes")
4. **User validates** the QUESTION → expected-answer set before it is frozen.
5. **Freeze** `questions.yaml`. The dataset is now re-runnable forever.

## Guardrails

- **Black-box only.** No `import okto_neuron` anywhere under `bin/`
  (`grep -r "import okto_neuron" tests/golden/bin` → empty). Drives HTTP + CLI.
- **No durable `/tmp` vaults.** The harness uses a run-scoped `mktemp -d` working
  dir and cleans it up on exit (`--keep-vault` to retain for debugging).
- **Loopback only.** Server binds `127.0.0.1`; ingest is loopback-restricted.
- **No PII** in `inputs/`.
- `results/` is gitignored. `inputs/` + `questions.yaml` are committed — they are
  the golden artifact.
- This is a test framework: it changes **no** product code, schema, or APIs.
