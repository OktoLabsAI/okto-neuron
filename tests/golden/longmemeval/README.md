# LongMemEval public diagnostic adapter

This directory defines a reproducible, **non-gating** procurement diagnostic. It does not contain
the LongMemEval corpus, an Okto Neuron score, or a leaderboard claim. The adapter only converts the
official cleaned LongMemEval schema into isolated datasets understood by the existing Golden
harness.

Primary sources:

- paper: <https://arxiv.org/abs/2410.10813> and the
  [ICLR 2025 paper](https://proceedings.iclr.cc/paper_files/paper/2025/file/d813d324dbf0598bbdc9c8e79740ed01-Paper-Conference.pdf);
- official code: <https://github.com/xiaowu0162/LongMemEval>, pinned to
  `9e0b455f4ef0e2ab8f2e582289761153549043fc`;
- official cleaned dataset: <https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned>,
  pinned to revision `98d7416c24c778c2fee6e6f3006e7a073259d48f` and released as MIT.

`frozen-manifest.json` pins the cleaned S file, its byte size and SHA-256, the deterministic subset
algorithm, the ingestion mapping, runtime placeholders, and the comparisons that a report must
disclose. The frozen diagnostic selects five records from each official question category plus
five abstention cases: 35 isolated cases. Within each bucket, selection sorts by
`sha256(seed + NUL + bucket + NUL + question_id)`. Source array order therefore cannot change the
subset.

## Materialize locally

Download the pinned 277 MB source into a private, ephemeral location. Do not add it or the derived
conversation files to Git:

```bash
SOURCE="$HOME/.cache/marginalia-eval/longmemeval_s_cleaned.json"
mkdir -p "$(dirname "$SOURCE")"
curl --fail --location \
  "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/98d7416c24c778c2fee6e6f3006e7a073259d48f/longmemeval_s_cleaned.json" \
  --output "$SOURCE"

uv run python tests/golden/longmemeval/adapter.py validate-manifest \
  --manifest tests/golden/longmemeval/frozen-manifest.json
uv run python tests/golden/longmemeval/adapter.py convert \
  --manifest tests/golden/longmemeval/frozen-manifest.json \
  --source "$SOURCE" \
  --out "$HOME/.cache/marginalia-eval/longmemeval-marginalia-v3"
```

Conversion verifies the pinned source bytes before parsing. It emits one directory per question:

```text
longmemeval-marginalia-v3/
  reproducibility-manifest.json
  cases/<question-id-and-hash>/
    dataset.yaml
    questions.yaml
    reference.json
    inputs/<ordered-session>.md
```

Each case is a separate Golden dataset and must be ingested into a separate vault. This preserves
LongMemEval's per-question memory boundary. Session Markdown contains timestamps and both user and
assistant turns, but uses only opaque ordinal session names; upstream ids such as `answer_*` remain
in `reference.json` and cannot become a retrieval shortcut. `has_answer`, answer-session labels,
and the reference answer never enter the ingested documents. The emitted query includes the
official `question_date` as its current date, and numeric reference answers are normalized to text.
Exact evidence turns are represented as both byte-hashed `gold_targets` and byte-offset
`gold_spans`.

The `temporal-reasoning` cases retain their timestamped sessions and remain eligible for retrieval,
span, and provenance metrics. Their question records carry
`unsupported_capability: valid_time_queries`: both Golden judges report `UNSUPPORTED` without a
judge call and exclude those rows from QA scorecards. This prevents a text-retrieval success or
failure from being misreported as first-class valid-time semantics, which ADR 0040 explicitly
defers.

The official cleaned file contains repeated upstream session ids. Those ids are reference
metadata rather than unique ingestion keys: the adapter preserves every aligned ordinal
occurrence as its own opaque `session-NNNN.md` input and records the one-to-many reference mapping.
It never collapses or overwrites occurrences merely because their upstream ids match.
The pinned source also contains an empty conversational turn. The adapter preserves its role and
ordinal with an empty Markdown body; it does not delete the turn or invent replacement content.

Run the deterministic floor or a local diagnostic on a single isolated case by passing its
directory to the standard harness:

```bash
CASE="$HOME/.cache/marginalia-eval/longmemeval-marginalia-v3/cases/<case>"
./tests/golden/bin/eval-run.sh "$CASE" --ci
./tests/golden/bin/run-golden.sh "$CASE"
```

## Run the complete diagnostic

`diagnostic.py` is the resumable orchestrator. It creates one inheriting managed vault per case,
persists the exact `/api/v1/ingest-batch` item ids before waiting, ignores unrelated historical
queue failures, then discovers and persists only the reconciliation job ids linked from those
items. It waits for every unique linked job before verifying complete Document byte identity and
asking a question. If the client is killed, the server keeps its owned work; re-running the same
command watches the retained ids and never submits a second batch. Each subprocess has a hard
process-group timeout, and every long stage emits a heartbeat at least every 30 seconds.

Construction accounting does not read the bounded ingest-event log. Every successful `remember`
stores one `construction_cost.v1` aggregate in its durable outcome, and every shared
cross-document proposal job stores its own aggregate in the job result. The runner adds every
owned item once and every unique linked job once, writes a per-case construction artifact, and
fails closed if any successful completion or embedding request omitted token usage. A vault that
already contains Documents but has no persisted owned item ids cannot be adopted: its construction
cost cannot be attributed safely. A run produced by a server version without these aggregates must
use a fresh isolated diagnostic vault rather than silently reporting zero cost.

The effective provider/model configuration is pinned from the first case and must remain identical
for the rest of the run. The runner also fails unless embedding remains `batch_size=32` and
`max_concurrent_batches=1`.

Use a new `--run-root` together with a new `--vault-prefix` for every intentionally independent
measurement. The prefix is persisted in `state.json` and cannot change on resume, so an old vault
or an earlier server version can never be silently adopted. `--case-id` may be repeated to run an
exact smoke subset before committing to all 35 cases; the state still retains the complete frozen
case set and a later invocation can resume the remaining cases without duplicate ingestion.

```bash
MATERIALIZED="$HOME/.cache/marginalia-eval/longmemeval-marginalia-v3"
SOURCE="$HOME/.cache/marginalia-eval/longmemeval_s_cleaned.json"
UPSTREAM="$HOME/.cache/marginalia-eval/LongMemEval-pinned"
RUN_ROOT="$HOME/.marginalia/quality-runs/adr0040/longmemeval"

# Checkout the exact official code revision outside the repository.
git clone https://github.com/xiaowu0162/LongMemEval.git "$UPSTREAM"
git -C "$UPSTREAM" checkout --detach 9e0b455f4ef0e2ab8f2e582289761153549043fc

# Upstream-compatible flat BM25, session/user-only, rank-bm25 0.2.2.
uv run --with rank-bm25==0.2.2 python tests/golden/longmemeval/diagnostic.py \
  flat-bm25 --source "$SOURCE" --materialized "$MATERIALIZED" \
  --upstream "$UPSTREAM" --out "$RUN_ROOT/flat-bm25.json"

# Run or resume the 35 isolated Okto Neuron cases against the live application.
uv run python tests/golden/longmemeval/diagnostic.py run-marginalia \
  --materialized "$MATERIALIZED" --endpoint http://127.0.0.1:7777 \
  --run-root "$RUN_ROOT/marginalia"

# Fresh isolated one-case construction-accounting smoke. This does not reuse or
# delete the complete-run vaults above.
uv run python tests/golden/longmemeval/diagnostic.py run-marginalia \
  --materialized "$MATERIALIZED" --endpoint http://127.0.0.1:7777 \
  --run-root "$RUN_ROOT/measured-smoke" \
  --vault-prefix adr0040-lme-measured \
  --case-id 0a34ad58--f7c6b941b12a

uv run python tests/golden/longmemeval/diagnostic.py status \
  --run-root "$RUN_ROOT/marginalia"

# These commands fail closed until all 35 cases and all three arms are measured.
uv run python tests/golden/longmemeval/diagnostic.py aggregate \
  --materialized "$MATERIALIZED" --run-root "$RUN_ROOT/marginalia" \
  --out-dir "$RUN_ROOT"
uv run python tests/golden/longmemeval/diagnostic.py receipt \
  --materialized "$MATERIALIZED" \
  --marginalia-retrieval "$RUN_ROOT/marginalia-retrieval.json" \
  --direct-rag "$RUN_ROOT/direct-rag.json" --flat-bm25 "$RUN_ROOT/flat-bm25.json" \
  --out "$RUN_ROOT/public-diagnostic.json"
```

The BM25 arm runs the pinned upstream session/user-only transformation, `str.split(" ")`
tokenization, `rank-bm25==0.2.2` ordering, and `eval_utils.py` metrics while avoiding the official
entrypoint's unused CUDA and dense-retrieval imports. The runner records the exact revision and
both upstream source hashes. The pinned `eval_utils.py` calls `numpy.asfarray`, removed in NumPy 2;
the verification comparison uses only the equivalent `numpy.asarray(..., dtype=float)` alias and
must match every per-case metric exactly.

The final report fills runtime placeholders, retains the selected-ID and output-tree hashes,
discloses failures/exclusions, and keeps plain retrieval separate from generated answers. The
existing harness computes gold-span retrieval/intactness, byte-IoU, and token-recall from the
emitted offsets. It also collapses Okto Neuron's node hits to the first occurrence of each unique
source session and reports session-level `recall_any`, `recall_all`, and binary NDCG at
1/3/5/10/30. Okto Neuron's plain `/api/v1/recall` and direct `/api/v1/ask` arms are not
automatically comparable to vendor or paper headline scores.

Treat the conversations as sensitive even though the upstream authors describe human PII and
offensive-content screening. Only send derived data to an approved provider, preserve upstream MIT
attribution, and keep corpus material and results in local or access-controlled storage.
