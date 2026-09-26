# ADR 0018: Differentiated Retrieval Defaults — recall k=10 vs ask k=20

- **Status:** Accepted (updated 2026-06-21: k=20 ask default rigorously validated, +0.085; MCP `ask` fix; new CLI `ask` command. Extractive `_ASK_SYSTEM` answerer prompt tested and REVERTED — within the temp-0.7 noise floor.)
- **Date:** 2026-06-20
- **Deciders:** Alex Rivera, Marginalia core
- **Builds on:** ADR 0011 (subgraph-first answer assembly), ADR 0012 (user-configurable ask retrieval policy)
- **Relates to:** ADR 0009 (curation control plane), ADR 0019 (graph-native answer assembly — the path beyond the block-dump ceiling)
- **Supersedes:** nothing. This tunes defaults; it does not change retrieval mechanics or the schema.

---

## Context

`ask` began life as a thin shell over `recall`: both seeded the same number of
candidate hits. Both defaulted to `seed_k = 8`.

A grounded golden evaluation closed the loop on whether that default was right.
The dataset is 142 corpus-grounded, adversarially-verified question/answer pairs
built from the reference-eval vault. Because a 3-judge LLM panel scored only
κ = 0.14 against that grounded key (untrustworthy), every number below is scored
**deterministically against the grounded key**, never by an LLM judge.

End-to-end against the live daemon (`:7777`, answerer qwen3.6-35b on the local
box), at the old `seed_k = 8` default:

- **answer-presence (ask) = 0.677**
- recall@10 = 0.815, **recall@20 = 0.892** (not saturated)
- extraction-completeness = 0.903
- neg-control abstention = 0.917 (grounded)

The gap analysis (130 non-neg questions): OK 63% · **GENERATION 21%** ·
RETRIEVAL 11% · EXTRACTION 5%. The dominant failure was generation, and the
root cause was specific: **`ask` under-retrieved relative to `recall`.** `recall`
already defaulted to `k = 10`; `ask` defaulted to `k = 8`, so the top-ranked gold
document could fall outside `ask`'s seed window even when `/recall` ranked it #1.
The smoking gun was a cluster of questions (sl-001, qt-001, qt-003, er-005,
sl-007) where `/recall` surfaced the gold block but `/ask` declined or
hallucinated because that block never entered its narrower seed set.

## Decision

Differentiate the two defaults along the axis that actually distinguishes them —
**whether the caller can re-query.**

- **`recall` default `k` stays 10.** `recall` feeds an *agent* that can issue
  follow-up queries. The first pass should stay tight: cheap, high-precision,
  widened on demand by the agent itself.
- **`ask` default `k` becomes 20.** `ask` is *one-shot* — it synthesises a single
  answer with no agentic re-query — so it must seed wide enough to include the
  gold document on the first and only pass.
- **`ask` answerer runs with `enable_thinking = false`** (per-vault yaml, already
  set on reference-eval). Thinking at temperature 0.7 encouraged the model to *infer*
  facts (e.g. guessing a hostname from a naming convention) when context was thin;
  turning it off removes that hallucination pressure once retrieval is adequate.

These are **defaults only**. ADR 0012's per-request `AskRetrievalPolicy.seed_k`
still overrides on any individual call, and the deterministic CI floor remains the
only gated signal.

## Validation

Full-142 retest at `seed_k = 20` + `enable_thinking = false`:

- **answer-presence 0.677 → 0.754** (98/130).
- **neg-control abstention 0.917 held** (no hallucination-for-recall trade — the
  hard guard for this change).
- recall@20 (0.892) and extraction-completeness (0.903) unchanged — as expected,
  since a larger seed window cannot create gold that retrieval ranking never
  surfaces, nor facts extraction never minted.

Evidence came from an external evaluation archive that is intentionally not
retained or named in the release repository.

The k=20 win was since **confirmed by a controlled multi-sample study** (5 samples
per config, same daemon, temp 0.7): terse k=8 → terse k=20 = **+0.085**, McNemar
p=0.0018, bootstrap 95% CI [+0.035, +0.140] (excludes 0). See the Ceiling sweep
section below.

## Consequences

- **Cost.** `ask` context grows roughly with `k` (8 → 20, ~2.5×). This is
  acceptable for a one-shot call and is deliberately **not** applied to `recall`,
  which stays at 10 precisely to keep the agentic path cheap.
- **Ceiling.** k=20 is the *k-axis* ceiling (controlled answer-presence ≈ **0.72**;
  the older single run read 0.754 on a warmer daemon). It is now bounded by
  **recall@20**: ~11% of gold sits outside the top 20 (sl-001, qt-005,
  sup-104/105/106, er-007, qt-105 …). Pushing `k` higher cannot recover those — they
  are a retrieval-ranking limit, not a window-size limit. Lifting answer-presence
  further requires
  **retrieval reranking / hybrid retrieval** (which raises recall@20 itself) plus
  extraction-completeness work, not a bigger default `k`. A separate ADR will cover
  that.
- **Discoverability.** The recall-vs-ask asymmetry argues for making retrieval
  knobs *discoverable* — surfaced with their tradeoffs through the MCP tool schema
  and CLI — rather than living only as a baked default. That extends ADR 0012 and
  is tracked as follow-on work.

## Ceiling sweep results (2026-06-21)

After the k-axis decision landed, a follow-up study pushed each remaining knob to
find the true block-dump ceiling — and, critically, replaced earlier single-sample
numbers with a **rigorous 5-sample-per-config study** to separate real effects from
temperature jitter. Every number is scored **deterministically against the grounded
golden key** (the 3-judge LLM panel was κ=0.14 and is not trusted for gating) —
artifacts retained outside the release repository, validated end-to-end against
the live daemon (`:7777`, answerer qwen3.6-35b).

**Single-run answer-presence at temp 0.7 is NOT a reliable metric.** Per-config
run-to-run sd is ≈ 0.013–0.019, and ~7–10% of the 130 questions flip answer between
identical reruns. The trustworthy signals are the **deterministic floor (recall@k)**
and this **multi-sample study** with significance tests — not any single ask run.

**The controlled 5-sample study (15 runs, same daemon, temp 0.7):**

| Config | answer-presence (mean ± sd) | neg-control abstention |
|---|---|---|
| terse, k=8 (T8) | 0.635 ± 0.004 | 0.883 |
| terse, k=20 (T20) | **0.720 ± 0.017** | 0.917 |
| extractive, k=20 (E20) | 0.738 ± 0.012 | 0.917 |

**k=20 over k=8 is a REAL win.** T8 → T20 = **+0.085**, McNemar **p=0.0018**,
bootstrap 95% CI **[+0.035, +0.140]** (excludes 0), Cohen's d ≈ 6.9, and all 25
cross-run pairings are positive. neg-control abstention even *improves*
(0.883 → 0.917). **KEEP k=20.**

**The extractive `_ASK_SYSTEM` prompt is NOT a real win — REVERTED.** terse → extractive
at k=20 = **+0.019**, bootstrap 95% CI **[-0.006, +0.046]** (spans 0), McNemar
**p=0.22** — squarely **within the temp-0.7 noise floor**. The earlier single-run
"0.754 → 0.792" extractive win was **cross-daemon noise** (two runs on different
daemon processes), not a real effect; the clean same-daemon multi-sample shows the
prompt delta is indistinguishable from temperature jitter. Because it buys nothing
measurable and costs a longer prompt, the extractive variant was reverted to the
terse default `"You answer grounded in the provided notes. Be concise."` in
`companion/__init__.py:555`.

**Daemon-offset caveat.** A restarted daemon runs ~0.03–0.05 lower in absolute
answer-presence than the pre-restart daemon for identical configs (e.g. terse k=20 =
**0.720** controlled vs **0.754** in the earlier single run) — a warm-state / KV-cache
offset. Use the controlled 5-sample numbers (**0.635 / 0.720**) as the validated
figures; treat older single-run absolutes (0.677 / 0.754 / 0.792) as **indicative
only**, not directly comparable across daemon restarts.

**k=40 is infeasible in block mode.** Pushing the seed window to 40 dumps ~120K
tokens of raw Block text into the 35B answerer, which overflows the context and
returns **31 empty answers** — the resulting 0.654 is an overflow artifact, not a
real score. **k=20 is the block-mode ceiling**: there is no more context budget to
spend without breaking the answerer.

**Block-dump ≫ subgraph, *for now*.** On the same grounded set, block-dump
(terse k=20 ≈ **0.72**) beats the ADR 0011 subgraph path (≈ **0.6**, single run) by
~0.12 — far above the noise floor, so the conclusion holds even though the subgraph
number is a single run. neg-guard on the subgraph path (~0.75) was clearly worse. The
reason is not that subgraph is the wrong architecture — it is that the graph is
currently **extraction-thin**: many answer facts were never minted as Claims, so no
graph walk can reach them, while the raw-block dump reads them directly.

**0.72(0) is a brute-force block-dump ceiling, not the goal.** It comes from dumping
~60K tokens of raw text per question. The architectural direction is graph-native
answering — from nodes, Claims, and connections — which is ~100× cheaper context
(a rendered subgraph is ~584 tokens versus the ~60K block dump). Moving past this
ceiling on cheap context is the subject of **ADR 0019 (graph-native answer assembly)**:
it root-causes the subgraph gap (63% extraction / 29% assembly / 8% wording) and lays
out the Tier-1 assembly fixes (→ ~0.73) and Tier-2 extraction work that take
graph-native past this ceiling.

## Code

- `src/marginalia/server/http.py` — both `/api/v1/ask` handlers:
  `k = payload.get("k", 20)`.
- `src/marginalia/companion/__init__.py` — `def ask(..., k: int = 20, ...)`;
  `_ASK_SYSTEM` stays the **terse default** (`"You answer grounded in the provided
  notes. Be concise."`) — the extractive variant was tested and **reverted** (within
  the temp-0.7 noise floor, see Ceiling sweep). Overridable via `llm.ask.system_prompt`.
- `src/marginalia/server/runtime.py` — **MCP `ask` fix.** The MCP tool's default
  was still `k=8` (the ADR 0018 k-axis change had missed the MCP path); it is now
  `k=20`. It also previously forced `enable_subgraph=True`, pinning every MCP `ask`
  to the *worse* subgraph path (0.6) — that force is dropped, so MCP `ask` uses the
  validated block mode (config default) like every other surface. The `ask` /
  `explore` docstrings are rewritten to teach the recall-vs-ask asymmetry (ask is
  one-shot → seeds wide at k=20; explore is agentic → seeds tight and re-queries).
- `src/marginalia/cli/__init__.py` — **new CLI `ask` command** (one-shot answer,
  k=20) plus amended `query --k` help, surfacing the same asymmetry: `ask`
  one-shot at k=20 vs `recall`/`query` agentic at k=10.
- `recall` unchanged: `Companion.recall(k=10)`; CLI `query --k` default 10.
