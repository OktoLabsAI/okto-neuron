# ADR 0012: User-Configurable Ask Retrieval and Source Fetch Policy

- **Status:** Accepted and implemented
- **Date:** 2026-06-07
- **Deciders:** Alex Rivera, Marginalia core
- **Builds on:** ADR 0003, ADR 0005, ADR 0011
- **Relates to:** ADR 0002, ADR 0009, ADR 0010
- **Supersedes:** nothing. This is a control-plane refinement of ADR 0011, not a reversal.

**Lifecycle addendum — 2026-07-13.** The ask retrieval/source policy is represented in
validated vault configuration and surfaced through the HTTP Config API and Web UI. The
conservative block default and opt-in subgraph policy are intentional shipped behavior;
ADR 0028 adds the bounded blend semantics. This ADR has no open release action.

---

## Context

ADR 0011 moved `ask` toward subgraph-first answer assembly: retrieve relevant seed nodes,
expand graph neighbours, render compact typed structure, and fetch source blocks only when
coverage is missing.

That direction is still right, but the live eval evidence changed the immediate priority.
The current block-dump path still wins on answer quality, while the subgraph path is much
smaller but incomplete:

- default block-dump path: `0.854` on the 120-question eval;
- subgraph Tier 1 alone: about `0.45`;
- subgraph with current Tier 2 source fetch: about `0.61`;
- compact subgraph render: about `488` tokens versus about `66K` tokens for block dump.

The conclusion is not "go back to block retrieval." It is also not "commit harder to
subgraph-only." The system needs a way to sweep retrieval policy directly from the UI so
we can observe where subgraph-first works, where it needs source text, and which knobs
actually move quality.

Hidden YAML tuning is too slow for this phase. The Query UI should act as the retrieval
lab: each ask request can carry a typed retrieval policy, the backend owns the policy
semantics, and the response reports which path actually ran.

---

## Decision

Expose ask retrieval behavior as a **typed, request-scoped retrieval policy** controlled
from the Query UI.

The policy is request-scoped on purpose:

- experimenting in the UI does not rewrite `marginalia.yaml`;
- defaults stay conservative;
- old clients that send only `{question, k}` keep working;
- the backend remains the source of truth for retrieval semantics.

### Policy fields

The UI may send:

- `enable_subgraph`: use subgraph-first answer assembly for this request;
- `seed_k`: number of initial retrieval seeds;
- `hops`: graph expansion depth;
- `max_degree_per_seed`: hub cap per seed;
- `neighbour_budget_tokens`: subgraph render token budget;
- `relationship_types`: optional predicate allowlist to traverse/render;
- `min_claim_confidence`: minimum Claim confidence allowed into the subgraph;
- `max_nodes`: maximum rendered node rows;
- `max_relationships`: maximum rendered relationship rows;
- `max_claims`: maximum rendered propositional claim rows;
- `source_block_policy`: `never`, `on_coverage_miss`, or `always`;
- `source_block_budget_tokens`: token budget for fetched source blocks;
- `coverage_threshold`: cheap density threshold for subgraph coverage gating.

The backend validates this as one object. Unknown fields are rejected.

### Source block policy

Blocks remain source receipts and verification/detail payload. They are not promoted
back into the primary knowledge unit.

`source_block_policy` has three modes:

- `never`: answer from the selected structured context only;
- `on_coverage_miss`: first try structured context, then fetch source blocks only if
  the answer abstains or the rendered subgraph is thin;
- `always`: include source blocks in the first ask context.

When subgraph mode is off, any policy other than `never` uses the legacy block context.
That preserves the current conservative default.

### Observability

Every API response includes retrieval trace metadata:

- mode/path used;
- effective `k`, hop depth, degree cap, budgets, confidence floor;
- source block policy;
- whether source blocks were actually used;
- estimated context tokens.

This is not a scoring harness. It is the minimum visibility needed to make manual UI
sweeps meaningful before another eval run.

---

## Why this is first

Extraction density is the next architectural bottleneck: subgraph-first can only answer
from facts that were extracted into Claims and bridged to entities.

But improving extraction before exposing retrieval policy makes it harder to tell which
change helped. The first change is therefore configurability and observability. After
that, extraction-density work can be measured against multiple retrieval policies instead
of argued from intuition.

The next priority after this ADR is:

1. improve Claim minting;
2. normalize predicates;
3. improve `O_literal` coverage for quantitative and propositional facts;
4. keep Claim/entity bridge edges complete;
5. harden authority/entity resolution so graph neighbours are not polluted.

**Update 2026-06-08.** ADR 0013 inserts a measurement boundary before that
extraction-density work: durable candidate records, comparison records, and
commit plans should land before splitting extraction into specialist workers or
making broad prompt changes. The goal is to know which candidate classes,
comparisons, and commit decisions are actually failing before increasing LLM
call count or extractor complexity.

---

## Consequences

### Positive

- The UI becomes a live retrieval lab.
- The default can stay conservative while subgraph behavior is explored safely.
- Source-block inclusion becomes an explicit policy, not an accidental branch.
- Subgraph, source fetch, and block-dump behavior can be compared request by request.
- The user can directly test the suspicion that dropping block context loses answer-bearing
  details.

### Negative / risks

- More knobs can create noisy experiments unless eval reports the effective policy.
- `source_block_policy=always` can hide graph sparsity by reintroducing prose.
- `source_block_policy=never` can make answers worse until extraction density improves.
- Relationship allowlists can accidentally filter out needed evidence.

### Invariants

- Backwards-compatible clients keep working.
- Blocks remain receipts and source detail, not the primary knowledge model.
- Subgraph-first does not become default until eval evidence beats or matches the current
  block baseline with provenance and hallucination metrics included.
- Policy knobs are validated in the backend; the UI only edits the typed policy object.

---

## Rollout

1. Add the typed request policy and backend validation.
2. Add Query UI controls for every policy field.
3. Add retrieval trace metadata to ask responses.
4. Keep `enable_subgraph` default off.
5. Run manual UI sweeps to identify promising policies.
6. Re-run the 120-question eval for:
   - block-dump;
   - subgraph-only;
   - subgraph plus `on_coverage_miss`;
   - subgraph plus `always` source blocks.
7. Use those results to prioritize extraction-density work.

---

## Non-goals

- This ADR does not flip subgraph-first on by default.
- This ADR does not claim subgraph-only is ready.
- This ADR does not solve extraction density.
- This ADR does not replace ADR 0011's target architecture.
- This ADR does not add persistent YAML writes from the Query UI.

---

## Exit criteria

The implementation is complete when:

- the Query UI can set every policy field listed above;
- `/api/v1/ask` accepts the policy as a single object;
- invalid policy fields fail with a `400`;
- responses expose retrieval trace metadata;
- existing `{question, k}` ask clients still work;
- tests cover policy validation, source-block modes, and subgraph render caps/filters.
