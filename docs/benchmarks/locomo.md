# LoCoMo benchmark: methodology and results

This page explains how Okto Neuron was measured on the LoCoMo long-conversation memory
benchmark and what the numbers do and do not show. Every number here comes from
[`okto-neuron-locomo-bundle.json`](okto-neuron-locomo-bundle.json) in this directory. The bundle
is the published record; this page only reads it.

The runs were made on development builds between v0.1.0 and v0.2.0, when the product was still
named Marginalia. Each arm is labelled with its code version as "v0.1.0 + N commits" (v0.2.0 is
v0.1.0 + 59 commits). No arm was re-run on 0.3.0, so these are not 0.3.0 quality numbers.

## What was measured

- **Dataset.** LoCoMo: 10 long multi-session conversations with 1,986 questions (A. Maharana et
  al., "Evaluating Very Long-Term Conversational Memory of LLM Agents", ACL 2024). The dataset is
  CC BY-NC 4.0, so no LoCoMo text is published here, only aggregate scores and per-question
  verdict labels.
- **Pipeline.** Each conversation's sessions are ingested into their own vault with the normal
  ingest pipeline, then every question goes through `ask` against that vault.
- **Scope.** Categories 1 to 4, n = 1,540 questions (282 / 321 / 96 / 841). Category 5
  (adversarial, unanswerable) is excluded because the reference scorer accepts only two literal
  refusal phrases, which measures phrasing rather than memory.
- **Judge.** An LLM judge labels each answer CORRECT or WRONG against the gold answer. The judge
  is a local qwen3.8-27b at temperature 0 for every arm, with the prompt versioned
  `mem0-memgpt-v1` (reproduced below).
- **Metrics.** Pooled = correct / judged over all category 1-4 questions. Macro = the mean of the
  four per-category percentages, so the small category 3 (n = 96) weighs as much as category 4
  (n = 841). A question whose judge call returned no label is dropped from the denominator.
- **Paired test.** Two arms are compared question by question. The discordant pairs are the
  questions one arm got right and the other got wrong; the exact two-sided McNemar test runs on
  those. Questions without a clean label in either run are left out of the pair.
- **Runs.** One run per arm, except qwen3.8-27b, which has three runs with identical settings on
  three nearby development commits.

## Results

All scores are LLM-judge %, categories 1 to 4.

| Arm | Code version | Ingest model | Ask model | Self-judged | Macro | Pooled | Cost |
|---|---|---|---|---|---:|---:|---|
| `qwen-run1` | v0.1.0 + 8 | qwen3.8-27b | qwen3.8-27b | yes | 56.58 | 60.95 | $0 (local) |
| `qwen-run2` (baseline) | v0.1.0 + 16 | qwen3.8-27b | qwen3.8-27b | yes | 56.27 | 60.99 | $0 (local) |
| `qwen-run3` | v0.1.0 + 35 | qwen3.8-27b | qwen3.8-27b | yes | 55.88 | 59.97 | $0 (local) |
| `glm-5.3` | v0.1.0 + 17 | glm-5.3 | glm-5.3 | no | 72.36 | 79.39 | $71.34 measured |
| `glm-graph-qwen-ask` | v0.1.0 + 23 | glm-5.3 (reused vaults) | qwen3.8-27b | yes | 69.89 | 77.24 | not run end to end |
| `gpt-5.6-luna` | v0.1.0 + 44 | gpt-5.6-luna | gpt-5.6-luna | no | 65.39 | 73.96 | flat-rate subscription |
| `gemma4-26b-thinking-off` | v0.1.0 + 27 | gemma4-26b | gemma4-26b | no | 35.06 | 42.32 | $0 (local) |
| `gemma4-26b-thinking-on` | v0.1.0 + 27 | gemma4-26b | gemma4-26b | no | 45.18 | 53.90 | $0 (local) |

Per category (judge %):

| Arm | Cat 1 (n=282) | Cat 2 (n=321) | Cat 3 (n=96) | Cat 4 (n=841) |
|---|---:|---:|---:|---:|
| `qwen-run1` | 61.57 | 55.14 | 44.79 | 64.80 |
| `qwen-run2` | 60.71 | 55.45 | 43.75 | 65.16 |
| `qwen-run3` | 58.16 | 57.19 | 44.79 | 63.38 |
| `glm-5.3` | 68.33 | 81.93 | 54.17 | 85.00 |
| `glm-graph-qwen-ask` | 67.50 | 74.77 | 53.13 | 84.19 |
| `gpt-5.6-luna` | 57.09 | 69.47 | 51.04 | 83.95 |
| `gemma4-26b-thinking-off` | 44.29 | 25.31 | 20.00 | 50.65 |
| `gemma4-26b-thinking-on` | 52.67 | 38.75 | 26.04 | 63.26 |

Category 3 has n = 96, so a single question moves it by about one point.

### Paired comparisons

| A | B | What it tests | A right, B wrong | B right, A wrong | Macro delta (B − A) | McNemar p |
|---|---|---|---:|---:|---:|---:|
| `qwen-run1` | `qwen-run2` | identical settings, nearby builds (not pure noise; code versions differ) | 266 | 266 | −0.31 | 1.0 |
| `qwen-run1` | `qwen-run3` | identical settings, nearby builds (not pure noise; code versions differ) | 273 | 257 | −0.70 | 0.51 |
| `qwen-run2` | `qwen-run3` | identical settings, nearby builds (not pure noise; code versions differ) | 251 | 236 | −0.39 | 0.53 |
| `qwen-run2` | `glm-5.3` | hosted model, end to end | 109 | 392 | +16.09 | 1.7e-38 |
| `qwen-run2` | `glm-graph-qwen-ask` | graph built by glm-5.3 (curation batch differs) | 120 | 369 | +13.63 | 1.7e-30 |
| `glm-graph-qwen-ask` | `glm-5.3` | unattributed remainder | 42 | 77 | +2.46 | 0.0017 |
| `qwen-run1` | `gpt-5.6-luna` | subscription model | 149 | 350 | +8.81 | 1.1e-19 |
| `qwen-run2` | `gpt-5.6-luna` | subscription model | 155 | 355 | +9.12 | 4.3e-19 |
| `qwen-run3` | `gpt-5.6-luna` | subscription model | 146 | 362 | +9.51 | 3.3e-22 |
| `glm-5.3` | `gpt-5.6-luna` | subscription vs hosted | 210 | 127 | −6.97 | 7.2e-06 |
| `qwen-run2` | `gemma4-26b-thinking-off` | weaker local model | 456 | 170 | −21.21 | 4.0e-31 |
| `qwen-run2` | `gemma4-26b-thinking-on` | weaker local model | 336 | 226 | −11.09 | 4.0e-06 |
| `gemma4-26b-thinking-off` | `gemma4-26b-thinking-on` | thinking off vs on | 171 | 347 | +10.12 | 8.2e-15 |

Every pair in the bundle also lists its confounds (see below).

## How to read these numbers

- **Run-to-run variation is large per question and small in aggregate.** Between the qwen runs with identical
  settings, about a third of verdicts flip (34.6% between `qwen-run1` and `qwen-run2`), yet the
  macro scores stay within 0.70 points and no difference was detected by McNemar. That is why
  every comparison above shows its discordant counts, not only the delta. The three qwen runs are
  on three different development commits with changes under `src/` between them, so they are
  "runs with identical settings", not identical runs.
- **Most of the hosted-model gain is built at ingest.** Answering with local qwen3.8-27b from the
  graph that glm-5.3 built (`glm-graph-qwen-ask`) is +13.63 macro over the baseline. That graph
  was also curated with a different batch size (32 against 1), so the +13.63 is not a pure graph
  effect. The remaining
  +2.46 to the full glm-5.3 arm is not attributed to one cause: it mixes the answer model, the
  sampling preset, and judge self-preference (the qwen judge scores its own answers in
  `glm-graph-qwen-ask` but not in `glm-5.3`).
- **The subscription arm wins some categories and loses one.** `gpt-5.6-luna` (reasoning off)
  is +8.81 to +9.51 macro over the three qwen runs, loses category 1 by 1.1 to 4.5 points, and wins
  categories 2 and 4 by 12 to 21 points. It is 6.97 macro under glm-5.3. It ran on a later
  development commit (v0.1.0 + 44) than the other arms, and its transport sends the vendor's
  default sampling rather than the qwen baselines' instruct preset.
- **A weaker model hurts, and we show it.** gemma4-26b scores 35.06 with thinking off and 45.18
  with thinking on. The thinking-off arm also lost 3 of 29 session documents in one conversation at
  ingest (a defect fixed later), so it answered from a thinner graph.
- **Cost.** glm-5.3's $71.34 is the harness's recorded token counts times Z.ai's published GLM-5.3
  rates ($59.90 ask, $11.45 ingest; the judge ran locally). `glm-graph-qwen-ask` reused the glm-5.3
  vaults, so its only ingest cost is that $11.45, and it was not run end to end. The
  `gpt-5.6-luna` arm ran on a flat-rate ChatGPT subscription with no per-call price, so it has no
  dollar figure. The qwen and gemma arms ran on a local model with no provider spend.

## Confounds and known defects

- **Self-judging.** The judge is qwen3.8-27b, so arms whose answers come from qwen3.8-27b
  (`qwen-run1..3`, `glm-graph-qwen-ask`) are self-judged; the others are not.
- **Sampling presets.** The qwen arms use the `instruct` preset. glm-5.3, gpt-5.6-luna and both
  gemma4 arms use the vendor's defaults (for gpt-5.6-luna because its transport drops sampling
  fields).
- **Code version.** Arms ran on different development commits (v0.1.0 + 8 to + 44). The bundle
  records each arm's commit distance; the private commit identities are not published.
- **Claim counts differ a lot by ingest model** (1,053 to 1,107 for qwen, 2,798 for glm-5.3, 4,202
  for gpt-5.6-luna), which is part of what the graph comparison measures.
- **Per-arm defects** (from the bundle's `known_defects`):
  - `glm-graph-qwen-ask`: did not ingest; it reuses the ten vaults built by `glm-5.3`.
  - `gpt-5.6-luna`: one category-5 question failed at ask and was re-asked; scores use the last
    row, and its trace stage counts predate the re-ask.
  - `gemma4-26b-thinking-off`: 3 of conv-42's 29 session documents were lost at ingest (a defect
    fixed later).
  - `gemma4-26b-thinking-on`: 2 of 272 ingest units hit the 600 s per-unit timeout and committed
    nothing.

## Failure anatomy

For each arm except `glm-graph-qwen-ask` (never traced), the bundle includes trace-stage counts:
an offline walk of each question's evidence that records the first stage where it broke
(`extraction_miss`, `retrieval_miss`, `citation_miss`, `reasoning_miss`, `no_evidence`,
`ask_error`). `ok` means extracted, retrieved, cited, and judged correct. These are not judge
scores: an answer the judge marked CORRECT without the traced evidence counts as a miss, so the
`ok` count is smaller than the judge-correct count.

| Arm | ok | retrieval_miss | reasoning_miss | extraction_miss | no_evidence | Judge-correct |
|---|---:|---:|---:|---:|---:|---:|
| `qwen-run1` | 881 | 454 | 197 | 4 | 4 | 938 |
| `qwen-run2` | 870 | 475 | 191 | 0 | 4 | 938 |
| `qwen-run3` | 868 | 452 | 212 | 4 | 4 | 923 |
| `glm-5.3` | 1,184 | 189 | 163 | 0 | 4 | 1,221 |
| `gpt-5.6-luna` | 1,097 | 228 | 210 | 1 | 4 | 1,139 |
| `gemma4-26b-thinking-off` | 584 | 670 | 264 | 18 | 4 | 650 |
| `gemma4-26b-thinking-on` | 768 | 524 | 243 | 1 | 4 | 829 |

Categories 1 to 4 only (n = 1,540).

## What we do not claim

- No ranking against other memory systems (Mem0, Zep, Letta or others), and none of their
  self-reported numbers next to ours.
- No F1 or recall@k figures from other papers, no LongMemEval, no category 5.
- No "state of the art" and no speed claims.
- No meaning for any delta inside the band of the three qwen runs shown above.
- `glm-graph-qwen-ask`'s $11.45 is not an end-to-end cost.
- These are development-build numbers, not a statement about 0.3.0 quality.

## What is published

The bundle (`schema_version` 3) carries, per arm: arm id, code version, models, judge model and
prompt version, self-judged flag, sampling preset, provider extra body, n / macro / pooled and
per-category scores, errors, ingest counts, token counts where recorded, cost and its basis, known
defects, failure anatomy, and the comparison against the baseline. It also carries every paired
comparison with its confounds, and one verdict label per question per arm (`C` correct, `W` wrong,
`E` judge error, `A` ask error) keyed by LoCoMo question id. It contains no LoCoMo text.

The benchmark harness itself is not published. Its scorer is a port of LoCoMo's CC BY-NC 4.0
scoring code, which cannot ship in this repository. Reproducing the numbers therefore means
re-implementing the protocol described here against the public LoCoMo release.

## Judge prompt (`mem0-memgpt-v1`)

The judge prompt reproduces, character for character, the one used in the Mem0 paper's LoCoMo
evaluation, which follows the MemGPT evaluation lineage (P. Chhikara et al., "Mem0: Building
Production-Ready AI Agents with Scalable Long-Term Memory", arXiv:2504.19413, 2025; C. Packer et
al., "MemGPT: Towards LLMs as Operating Systems", arXiv:2310.08560, 2023). It is credited to its
authors. See `THIRD_PARTY_NOTICES.md`.

```text
Your task is to label an answer to a question as "CORRECT" or "WRONG". You will be given
the following data: (1) a question (posed by one user to another user), (2) a 'gold'
(ground truth) answer, (3) a generated answer which you will score as CORRECT/WRONG.

The point of the question is to ask about something one user should know about the other
user based on their prior conversations. The gold answer will usually be a concise and
short answer that includes the referenced topic, for example:
Question: Do you remember what I got the last time I went to Hawaii?
Gold answer: A shell necklace
The generated answer might be much longer, but you should be generous with your grading -
as long as it touches on the same topic as the gold answer, it should be counted as
CORRECT.

For time related questions, the gold answer will be a specific date, month, year, etc. The
generated answer might be much longer or use relative time references (like 'last Tuesday'
or 'next month'), but you should be generous with your grading - as long as it refers to
the same date or time period as the gold answer, it should be counted as CORRECT. Even if
the format differs (e.g., 'May 7th' vs '7 May'), consider it CORRECT if it's the same date.

Now it's time for the real question:
Question: {question}
Gold answer: {gold_answer}
Generated answer: {generated_answer}

First, provide a short (one sentence) explanation of your reasoning, then finish with
CORRECT or WRONG. Do NOT include both CORRECT and WRONG in your response, or it will break
the evaluation script.

Just return the label CORRECT or WRONG in a json format with the key as "label".
```
