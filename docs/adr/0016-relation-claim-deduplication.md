# ADR 0016 — Relation/Claim identity and deduplication (living-graph reconciliation for edges)

- **Status:** Accepted (implemented & validated live 2026-06-12)
- **Date:** 2026-06-12
- **Deciders:** Alex Rivera
- **Relates to:** ADR 0005 (claim-entity bridge), ADR 0008/0010 (node reconciliation), ADR 0009 (heal control plane), ADR 0015 (ingest throughput & ledger)
- **Out of scope:** predicate synonymy judging (`wrote_to` ≈ `correspondent_of`) — noted as a future tier; LLM-judged near-duplicate relations; node dedup (already covered by ADR 0008/0010).

## Problem

After ingesting two WhatsApp exports sequentially into one vault, the graph holds many **exact duplicate relation Claims** — five separate Claim nodes named "Alex Rivera wrote_to Mariana Mathias", three "… correspondent_of …", etc. (observed live 2026-06-12 on `~/marginalia-playground`, runs B+C).

Root cause (verified in code):

1. **Claim identity is mention-scoped, not fact-scoped.** `claim_id = sha256(content_hash, src_ref, type, object_term, model_id, prompt_hash)` (`companion/__init__.py:3454–3461`). The same S-P-O extracted from a different block (different `content_hash`) gets a different id, so the existing `store.get_node(claim_id)` pre-mint check (`companion/__init__.py:3493`) can never catch a re-mention.
2. **No edge dedup tier existed.** The 4-tier node machinery only remapped edge
   endpoints when nodes merged. The former consolidation session appended and
   committed curated edge candidates with no existence check. The accepted
   implementation now lives in the executable-plan path in
   [`src/marginalia/companion/__init__.py`](../../src/marginalia/companion/__init__.py),
   including exact-edge folding and semantic Claim identity.

The product premise is a **living graph**: every ingest must reconcile against the whole existing knowledge, exactly as nodes already do. Duplicate mentions of a fact should *strengthen* one Claim (corroboration), not mint clutter.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **Claim identity = the semantic triple.** `claim_id = sha256_hex("claim", S_id, P, O_id or literal-tag)` computed from *resolved* endpoints + normalized predicate. `content_hash`/`model_id`/`prompt_hash` leave the identity and remain provenance-only. | Same fact ⇒ same id, across blocks, files, and runs. Makes store-level dedup a plain `get_node(claim_id)`. |
| 2 | **Re-mentions corroborate, never duplicate.** When minting hits an existing Claim, attach the new mention as additional PROV edges (`prov:wasDerivedFrom` → new block, `prov:wasGeneratedBy` → activity, `prov:wasAttributedTo` → agent) and bump a `corroborations` facet (int, default 1). The Claim's facet anchor (block_id etc., required by `schema/support/claim.py:33–76`) stays the *first* mention; schema unchanged. | PROV-O models multi-derivation natively; one Claim, N sources = strictly more trustworthy citation. No schema break. |
| 3 | **Tier E0 — within-run exact edge collapse.** After relation curation accepts candidates, group by resolved `(src_ref, normalized P, dst_ref/dst_literal)`; one survivor proceeds, the rest fold their provenance into it. Ledger method `exact_edge_batch`, verdict `merge`. | Five in-file mentions become one candidate before commit; mirrors node Tier `collapse_duplicates`. |
| 4 | **Tier E1 — exact reconcile against the store.** At mint time, decision 1 makes this `get_node(claim_id)`; on hit, apply decision 2 instead of creating. Ledger method `exact_edge_store`, verdict `merge`. Plain graph edges get the same guard: skip `add_edge` when `list_edges(src, type, dst)` is non-empty (`store/protocol.py:12`). | Cross-file dedup against the whole living graph; mirrors node `reconcile_against_store`. |
| 5 | **Retroactive heal pass.** Extend `copy_graph_canonicalizing` (`store/reembed.py:82–195`) to collapse existing Claim nodes by semantic triple (read S_id/P/O from facets): keep first, drop variants, re-point PROV + `rdf:subject`/`rdf:object` edges to the survivor, sum corroborations. New stats `claims_in/kept/dropped`. Runs through the existing heal path (ADR 0009 P3) — no new entry point. | Cleans vaults already polluted (playground has 5× dups); also migrates old mention-scoped claim ids to the new identity. Markdown stays the trust root; heal is reads-up. |
| 6 | **Ledger + UI visibility.** `exact_edge_batch`/`exact_edge_store` appear in `comparison_methods` and run summary gains `relations_corroborated`. | Same observability contract as every other tier (ADR 0015). |

## Consequences

- Claim ids change derivation ⇒ old vaults hold legacy ids until healed; heal (decision 5) converges them. Acceptable per dev-state policy (graphs migrate via rebuild/heal, config-free).
- `claims_minted` counts will drop sharply on chat-like corpora (expected: that's the bug being fixed). `corroborations` becomes the signal that was previously spread across dup nodes.
- Predicate synonymy (`wrote_to` vs `correspondent_of`) remains visible after this ADR — explicitly deferred; a future LLM-judge tier mirroring `judge_against_store` can fold synonyms onto canonical predicates (`normalize_predicate`, `curator.py:316`, already provides the alias hook).

## Addendum — 2026-09-15: the pre-curation accumulator was keying on `dst_ref` alone

Decision 1 puts the object term inside Claim identity, and decision 3 groups the
within-run exact collapse by `(src_ref, normalized P, dst_ref/dst_literal)`. Both
are implemented through `claim_object_identity`
(`src/marginalia/consolidate/_claim_identity.py`), which `semantic_claim_id` and
`_semantic_edge_key` (`src/marginalia/companion/__init__.py`) fold in correctly.

One earlier site did not. `Companion.remember`'s per-block extraction accumulator
— the dict that folds each block's `EdgeCandidate`s before anything downstream
sees them — keyed on `(type, src_ref, dst_ref, block_id)`. A literal-object
Claim carries its value in `dst_literal` and leaves `dst_ref` empty (built that
way in `src/marginalia/extract/__init__.py`), so every literal claim sharing a
predicate, a subject and a block collapsed into whichever one the model emitted
first. The loss happened *above* Tier E0: the discarded candidates never reached
curation, never got a `proposed` ledger row, and never appeared in the
`exact_edge_batch` merge counts that decision 6 promised would make folding
visible.

Measured by replaying the stored extraction payloads of a real 76-document vault
(`ingest-history.json`): 358 literal claims extracted, 193 survived the old key,
**165 dropped (46%)**. Keying on `(type, src_ref, claim_object_identity(...),
block_id)` keeps all 358 — and zero residual exact duplicates, so on that corpus
the accumulator had never once folded a true duplicate. It was pure loss.
Worst-hit documents went from 4 surviving claims to 54, 5 to 35, and 5 to 17.

Corrections, in the spirit of this ADR rather than beyond it:

1. The accumulator now derives its object term from `claim_object_identity`
   (literal when `dst_literal` is set, otherwise `dst_ref`), so the *only* place
   that treated the object as optional now matches the identity convention
   decision 1 established. The predicate is deliberately **not** normalized here:
   alias folding is a post-curation concern (`normalize_predicate` needs the
   pack/alias context that is not in scope at extraction time), and
   `_semantic_edge_key`'s "drop candidates with an empty term" guard is
   deliberately **not** ported — this accumulator must never drop a candidate
   on identity grounds.
2. The fold is no longer silent. A genuine same-key re-emission increments a
   run counter and, when non-zero, emits one `extraction_edge_collapse` trace
   event carrying `collapsed` and `accumulated` (both document-wide totals; the
   key is per-block, so a fold in one block sits alongside every other block's
   survivors). It is a plain collapse, not a
   data-integrity anomaly, so it stays out of `extraction_anomalies` and out of
   the warning that block logs; and it stays out of the candidate ledger, since
   a candidate folded here has no `proposed` row to transition and the existing
   comparison rows are keyed to sentinel ids (`"batch"`, `"store"`), not to
   synthesized edge ids.

Regression coverage lives with the other `Companion.remember` edge tests in
`tests/companion/test_loop.py`: distinct literals on one block all survive,
identical literals still fold to one (with `collapsed: 1` reported), and
topology edges keep deduping on `dst_ref` exactly as before.
