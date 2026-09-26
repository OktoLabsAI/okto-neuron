# ADR 0031 — Retention: Full-Window Curator Excerpt + Edge-Endpoint Anchor Promotion

**Status:** Accepted
**Date:** 2026-07-08
**Depends on:** ADR 0019 (graph-native answer assembly — Fix A `_salience:low` anchor promotion for dead-subject Claims), ADR 0020 (dense-fact predicate vocabulary)
**Relates to:** ADR 0016 (relation-claim dedup — the same-entity sibling carve-out reuses the dedup remap machinery)

---

## Context

Two retention leaks were surfaced by the reference-eval retention audit — grounded,
byte-anchored facts that were extracted but never committed, dying at a gate
they should have passed.

**E2 — the curator source excerpt was truncated before the literal.** The node
and relation curators verify a candidate against a source *excerpt* of its
`Block`. That excerpt was `block[:9000]` of a ~12k block, so any candidate
anchored in the final ~3k characters — **markdown table cells especially** —
drew a structurally-guaranteed "excerpt does not contain the literal" **queue**
verdict. The fact was correct and grounded; the curator simply never saw it.

**E3 — the endpoint gate dropped grounded edges with one dead endpoint.** ADR
0019 Fix A auto-promotes the dead *subject* of a literal-fact Claim as a
`_salience:low` anchor rather than dropping the fact. But a **topology edge**
whose *object* (dst) was merely queued still dead-lettered at the endpoint gate.
The retention audit found grounded facts dying here because the far endpoint was
a weak structural node — e.g. a group-email node and a stack-label node that a
real edge pointed at.

## Decision

**E2 — cover the full extraction window in the curator excerpt.** Introduce
`CURATION_SOURCE_EXCERPT_LIMIT = 16000` (`src/marginalia/curator.py`) and use it
for the curator source excerpt so it spans the whole 12k extraction window (with
headroom), instead of `block[:9000]`. The limit is still bounded for
pathological single-line blocks, and the excerpt stays **byte-identical per
block**, so prompt-prefix cache reuse is unchanged.

**E3 — extend Fix-A promotion to topology edges with exactly one dead
endpoint.** In the endpoint gate (`src/marginalia/companion/__init__.py`), an
edge with exactly **one** dead endpoint (src *or* dst) promotes that endpoint as
a `_salience:low` anchor under the same guards as the Fix-A subject promotion:

- the endpoint was **extracted this run**,
- it was merely **queued, not contradicted**, and
- there is **no accepted same-entity sibling** — a queued exact-title duplicate
  stays with the dedup remap machinery (ADR 0016) and is never promoted.

An edge with **both** endpoints dead still dead-letters. Promotion is
necessary-not-sufficient: it only lets the relation curator *judge* the edge; it
never mints an orphan node.

## Consequences

- **Grounded table-cell and edge facts are retained** instead of being dropped
  for a mechanical reason. This is part of the finale stack-level retention lift
  (reference-eval re-audit **E 70.0% → 82.3%**, +16 facts).
- **Precision is preserved.** E3 promotion keeps the Fix-A guards — extracted
  this run, not contradicted, no accepted same-entity sibling — so it recovers
  grounded edges without minting orphan or duplicate nodes, and both-endpoints-
  dead edges still dead-letter.
- **Cache behavior is unchanged.** The wider excerpt is byte-identical per block,
  so prompt-prefix reuse is not disturbed.
- The `_salience:low` anchors stay filtered from recall / ask / subgraph, so a
  promoted structural endpoint holds its fact without ever surfacing as an
  entity. (Trust-boundary note: `_salience`, like `is_infra`, is a reserved
  top-level facet — see the standing hardening task.)
- Guarded by `tests/test_curator_excerpt.py` (E2) and
  `tests/consolidate/test_endpoint_promote.py` +
  `tests/consolidate/test_endpoint_pregate.py` (E3).
