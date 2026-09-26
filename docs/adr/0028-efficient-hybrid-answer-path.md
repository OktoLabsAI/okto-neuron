# ADR 0028 — Efficient-Hybrid Answer Path: Always-Blend a Budgeted Source Excerpt

**Status:** Accepted
**Date:** 2026-07-07
**Depends on:** ADR 0011 (subgraph-first answer assembly), ADR 0012 (per-request retrieval policy), ADR 0019 (graph-native answer assembly)

---

## Context

The graph-native arc ran an **eight-experiment program** on the 142-question
reference-eval corpus (paired A/B, semantic judge primary): extraction volume, L1
re-extraction, answer-aware ego-graph ranking, multi-hop reach, extraction
granularity, a combined arm, seed-diversity quotas, and finally the hybrid.
Across the first seven, the subgraph arm sat at **98, 98, 98, 95, 98,
[82 confounded], 99** against a block-dump arm in the 117–121 band — volume,
ranking, reach, granularity, and seed quotas were all net-zero on the paired
verdict.

The pivotal finding was the **Tier-2 discovery** (Addendum 2 of the north-star
research note): the baseline subgraph arm's ~69% was substantially propped up
by its Tier-2 abstention fallback reading **30–66k tokens UNBOUNDED** — the
median *winning* escalation read ~50k tokens, i.e. block-dump scale. "Pure
graph-native 69%" was never pure, and the advertised ~20× context-efficiency
held only for non-escalating questions. A confounded run (`ab-seed-quality-2`)
that bundled a naive 4,000-token Tier-2 cap regressed the arm to 82/142 —
proving both that the unbounded read was load-bearing and that a dumb cap is
not the answer (≥13 losses were budget starvation from *wrong-block*
selection, not budget size).

The owner reframed the north star: not "pure graph-native beats block-dump"
but **"efficient hybrid — block-parity accuracy at a fraction of block
tokens"**.

## Decision

Replace the binary abstain→unbounded-dump cliff in the subgraph ask path
(`Companion.ask`, `src/marginalia/companion/__init__.py`) with a disciplined
budgeted hybrid:

- **Always-blend.** The default source-block policy becomes `"blend"`
  (`_ASK_SOURCE_BLOCK_POLICY_DEFAULT`): every subgraph answer context is the
  ego-graph render **plus** a budgeted source excerpt — default **6,000
  tokens** (`_ASK_SOURCE_BLOCK_BUDGET_DEFAULT`), resolved per-request policy
  (`AskRetrievalPolicy.source_block_budget_tokens`) > vault config
  (`llm.ask.source_block_budget_tokens`) > code default.
- **IDF query-term snippet selection.** Source snippets are re-ranked by
  IDF-weighted query-term coverage before the budget trim, so the budget is
  spent on the blocks that mention what the question asks (fixes the
  wrong-block selection that lost questions while reading 63k tokens of
  misses).
- **Escalation bounded at 2×.** A blended Tier-1 answer that abstains may
  re-ask **once** with the excerpt budget raised by at most
  `_ASK_ESCALATION_BUDGET_FACTOR` (2×), keeping the render in the escalated
  context; the escalation is skipped when it would yield no new source bytes.
  **No unbounded source read remains anywhere in the subgraph path.**
- **Seed-diversity quotas ship default-gated.** The fused claim+entity seed
  ranking with predicate-family diversity quotas (`seed_diversity` in
  `query.py`) measured a wash in isolation (+1,
  noise) but rescued specific flood-starved questions; it lands **default
  `False`**, opt-in per request or vault config.
- **Boot-warm provider dependencies.** `marginalia serve` imports the
  configured LLM providers' lazy dependencies once at boot
  (`server/runtime.py:_warm_llm_provider_dependencies` →
  `llm.warm_provider_dependencies`), pinning them in `sys.modules` so a
  concurrent `uv run` venv re-sync cannot break a running daemon into silent
  per-request 500s; a missing dependency is one loud boot ERROR. Paired with
  the harness abort-on-3-consecutive-ask-transport-failures guard
  (`tests/golden/bin/run-golden.sh`; see `docs/eval-gates.md`).

The block-dump arm is untouched (byte-identical pins), and the answer path
reports `retrieval.path` (`subgraph_blend` / `subgraph_blend_escalated`) plus
`context_tokens_estimate` so cost stays observable per question.

## Consequences

- **Statistical parity at 17.5% of the tokens.** ab-hybrid (142-Q semantic
  judge): subgraph-hybrid **119/142 (83.8%)** vs block **121/142 (85.2%)** —
  delta −1.4pt, McNemar p=0.81, bootstrap CI [−7.0, +4.2]. Token accounting:
  hybrid mean **8,522** / median 7,805 / **max 14,045 (hard-bounded)** vs
  block mean 48,673 / median 50,118 / max 66,427 — **~5.7× cheaper**, and
  both owner gates pass (accuracy ≥112; efficiency ≤30% of block tokens).
- **The cost cliff is gone by construction.** Worst-case context is
  budget × escalation factor, not "whatever the fallback happened to read".
  Paths observed: 123 `subgraph_blend`, 19 `subgraph_blend_escalated`.
- **Knobs.** `llm.ask.source_block_budget_tokens` (vault) and
  `source_block_budget_tokens` / `seed_diversity` (+ quota tuning fields) on
  the per-request `AskRetrievalPolicy`; escalation factor is a code constant
  by design.
- **Subgraph stays DEFAULT-OFF** (`llm.ask.enable_subgraph`): parity at 17.5%
  tokens justified the hybrid as the graph-native design while the completed
  program retained block mode as the conservative default. This is the accepted
  current policy, not an unfinished gate. A future default change would require
  a new decision and fresh multi-run evidence under `docs/eval-gates.md`.
- The program ledger closes: assembly/extraction levers alone were net-zero;
  the winning move was accepting the hybrid and making it disciplined
  (budgeted, query-aware, bounded). 13/142 remain both-wrong.
- Guarded by `tests/test_efficient_hybrid.py`, `tests/test_seed_diversity.py`,
  `tests/llm/test_warm_dependencies.py`; 433 tests green on the branch at
  merge.

## Evidence

- Research note + program ledger: `research/2026-07-06-north-star-subgraph-composition-bottleneck.md`
  — Addendum 1 (render-budget crowding refuted), Addendum 2 (quotas a wash;
  Tier-2 unbounded-read discovery), Addendum 3 (resolution: block parity at
  17.5% of tokens).
- A/B scorecard: `tests/golden/results/reference-eval/ab-hybrid/scorecard-ab.txt`
  (marginalia-hybrid worktree; arms, McNemar, bootstrap CI, per-question
  flip lists).
- Run-validity guards: `docs/eval-gates.md` (2026-07-07 section).
