# Implementation Plan — ADR 0011: Subgraph-first Answer Assembly

> Companion to `docs/adr/0011-subgraph-first-answer-assembly.md`. File-level, drives the actual coding. Cross-checked against live code 2026-06-07; line-number corrections vs the ADR/findings are noted inline. Produced by a 7-agent confirm-and-detail workflow (`adr0011-implementation-plan`, run `wf_63a987be-1d4`) with advisor review baked in.

**Status:** CLOSED — core implementation shipped; default policy and experimental
resolution are recorded in ADR 0028 and ADRs 0030–0031 (reconciled 2026-07-13).

This is now a historical implementation record. The subgraph renderer, multi-hop walk,
coverage/abstention fallback, byte-path revalidation, and configuration surface shipped.
The planned universal default flip did not: block mode remains the conservative default,
while the efficient-hybrid path is opt-in. Stale intermediate observations are retained
with their dated corrections and are not active TODOs.

## 1. Summary

Move `ask` from raw-block-dump retrieval (concatenate ~12 KB byte-slices of top-k hits, ~20K tokens, 0.69 baseline) to a two-tier graph-RAG path: **Tier 1** answers from a relevance-capped ego-graph (seeds + bridging claims + 1-hop graph neighbours via `rdf:subject`/`rdf:object`/`schema:mentions` edges) rendered as a compact typed `=== NODES / RELATIONSHIPS / CLAIMS ===` block (~hundreds of tokens); **Tier 2** fetches raw blocks on demand behind a coverage/abstain gate. Tuning params are live `llm.ask.*` knobs the hypertune harness already PATCHes. Extraction hygiene lands first (extractor fix + `kg rebuild`, never in-place mutation) because the structured render surfaces junk claims directly. The default ask path stays block-dump until the subgraph path beats 0.69.

## 2. Phase 1 — Extraction hygiene (extractor fix + `kg rebuild`, NOT in-place)

The structured render surfaces claims directly, so the three junk-minting paths must be cleaned at the extractor and re-derived from the vault. **No in-place node/edge deletion on the populated Ladybug graph** (ADR 0007 F2: bulk in-place mutation scrambled edge adjacency, 0→1227 corruption). All cleanup propagates via `kg rebuild`, which builds a fresh graph from markdown and atomically swaps.

### 1a. Wikilink near-duplicate Claims — strip the block-body payload
- **Junk path:** `src/marginalia/ingest/markdown.py:285-297`. For every `[[target]]` in a block, mints a Claim with `predicate="links_to"` (note: predicate is `links_to`, not the finding's prose "links to") and `text=f"{title} links to {target.strip()}\n{block.text}"` (line 295) — embeds the entire ~12 KB block body. The same pattern exists for tags at `:281` (`text=f"{title} has tag {tag}\n{block.text}"`) — fix both. Each Claim becomes `Node.content` at `ingest/__init__.py:60-71`, so every wikilink/tag claim from one block carries an identical ~12 KB payload → near-identical embeddings → the observed 2.5-unique-of-8 collapse.
- **Fix:** drop the `\n{block.text}` suffix at markdown.py:295 (wikilink) and :281 (tag). New text: `f"{title} links to {target.strip()}"` / `f"{title} has tag {tag}"`. Verbalized, self-describing, no body.
- **Scope correction (advisor):** this reduces *vector* near-duplication only. `claim_id = sha256_hex(block.content_hash, S, P, O)` (markdown.py:164) is unchanged, so identical triples from different chunks still mint distinct Claim nodes. The cross-chunk structural dup is killed at **render time** in Phase 2 by dedup on `(S_id, P, O_id|O_literal)` (§3, finding 6 gotcha 3). P1 = embedding-dup reduction; P2-render = structural-dup elimination. Neither alone collapses the 2.5-unique window.

### 1b. Zero-byte Document nodes — drop from the answerable pool
- **Junk path:** `ingest/__init__.py:33-46` mints a `Document` node with `content=ingest.item.content` (full body) but **no byte anchor** (no `block_id`, no `source_span`). At query time `vault.py:_provenance_for_node` falls through to `byte_start/byte_end → 0` (lines 477-478), so the fat-content Document ranks #1 on name match but resolves to an empty slice — wasting an answer slot (ADR §Evidence, q114–q117).
- **Fix:** mirror the existing untyped-Block drop in `query.py:263` (`not (type is None and seen[node_id].type == "Block")`). Add `Document` to that exclusion so Document nodes are dropped from the untyped recall/ask pool (an explicit `type="Document"` browser query still works). Document nodes **stay in the graph** as anchor carriers for `schema:mentions` edges and source-link provenance (ADR 0006) — they just don't rank as answers. This is a query-gate change, not a graph mutation, and is also `search_claims`-internal so it must be guarded for the default path (see §3 invariant: it changes recall output, so land it as part of Phase 1's rebuild-validated change set and re-run the eval to confirm no factual regression).
- **Alternative considered, rejected:** populating Document facets with a full-range `source_span`. Cleaner to drop from the pool — the body lives in Blocks already; duplicating it as an answerable node is the bug.

**Addendum — 2026-07-08 (correction, not a rewrite):** this fix landed and was then reverted for the shared default path — plan superseded, not just stale. Per `query.py:513` (`NB: ADR 0011 1b (dropping 0-byte Document nodes too) was reverted`): the eval showed it did not move accuracy and it changed the DEFAULT `search_claims` recall output (a "which doc mentions X" answer could resolve to a Document title). Anchorless Documents are instead filtered inside the subgraph builder's own quality gates (`subgraph.py`), keeping the shared `search_claims` path byte-identical for the default block-dump answer. See also the "reverted the always-on Document-drop (default path now byte-identical)" confirmation at line ~222.

### 1c. Low-confidence LLM Claims — live-knob confidence prune
- **Junk path:** `companion/__init__.py:_mint_relationship_claims` resolves `confidence = max(0.0, min(1.0, confidences.get(ecand.src_ref, baseline)))` at **line 848** (baseline `_CLAIM_BASELINE_CONFIDENCE=0.7`, line 563), then mints the `Claim()` at :873. No prune exists; sub-1.0 LLM noise surfaces. Deterministic markdown claims are always `confidence=1.0` (markdown.py:170) and must always pass.
- **Fix:** after line 848, gate: `if confidence < min_claim_confidence: continue` (skip minting). `min_claim_confidence` is a **live config knob** (`llm.ask.min_claim_confidence`, §5) read at the call site — pass it into `_mint_relationship_claims` as a param resolved from `cfg.llm.ask` (step-direct read, §3 point 2). Default `0.0` (no prune) so Phase 1 lands behaviour-neutral until tuned; calibrate against the 120-Q eval (a sharp gate over-prunes recall — better to tolerate junk than miss).

### Rebuild lands it (already correct, do not change)
- `kg rebuild` (`cli/kg.py:131`) → `_build_fresh_graph` (`:175`, comment "Build a FRESH graph") bootstraps a tmp graph (`:209`), deterministically re-ingests every markdown file (`:231-267`), health-checks (`:272`), closes (`:270`). The daemon's `run_rebuild` (`server/_curation.py:262`) builds with the live handle untouched, then atomically swaps (`:353-367`: close live → `_swap_rebuilt_graph` → reopen). No in-place deletion. Confidence prune bites only the full-pipeline ingest path (`_make_full_pipeline_ingest`, `cli/kg.py:452` → `companion.remember()` → `_mint_relationship_claims`); deterministic markdown claims are untouched.

### Verify
- Before/after on the reference vault: `kg rebuild`, then compare total Claim count, total Document count, and **unique-windows-per-8-hits** (historical baseline: 2.5). Expect: Document hits drop out of top-k (1b); wikilink/tag claim *embeddings* spread (1a); LLM claim count drops iff `min_claim_confidence>0` (1c). Re-run `marginalia-knowledge-quality-eval` to confirm no factual-tier regression from the Document drop.

## 3. Phase 2 — Subgraph render behind a flag (default stays block-dump)

Ordered, file-by-file. The default block-dump path stays byte-identical; the subgraph path is additive, gated by `llm.ask.enable_subgraph` (default `False`).

### 3.0 Config knobs — `llm.ask.*` (NOT a new top-level block)
**Surface decision (advisor, resolves finding 3's internal contradiction):** put knobs on `StepLLM.ask`, NOT a new top-level `query` block. `llm` is already in `WRITABLE_BLOCKS` (`config/_vault.py:468`), so this is a **zero-`WRITABLE_BLOCKS`-change** and the existing hypertune `set_ask_llm` PATCH path sweeps it. A new `query.*` block needs a new writable entry and a new setter — reject finding 3's Option A, take its Option B.

- **File:** `src/marginalia/config/_vault.py`, class `StepLLM` (lines 284-303). Add optional fields:
  - `enable_subgraph: bool | None = None`
  - `max_degree_per_seed: int | None = Field(default=None, gt=0)`
  - `neighbour_budget_tokens: int | None = Field(default=None, gt=0)`
  - `hops: int | None = Field(default=None, ge=1, le=2)`
  - `coverage_threshold: float | None = Field(default=None, ge=0.0, le=1.0)`
  - `render_format: str | None = None`
  - `min_claim_confidence: float | None = Field(default=None, ge=0.0, le=1.0)` (Phase 1c)
- **Read pattern (advisor, load-bearing):** these are ask-only retrieval knobs with no generation semantics and no meaning for extraction/judge. Do **NOT** route them through `cfg.llm.resolved("ask")` — `resolved()` iterates `self.defaults.model_dump()` keys only (`config/_vault.py:344-350`), so a field present only on `StepLLM` is silently dropped from `ResolvedLLM`. Read **step-direct** with a code-default fallback, exactly as `system_prompt` is read at `companion/__init__.py:525` (`cfg.llm.ask.system_prompt or _ASK_SYSTEM`). One field-add per knob; `LLMDefaults`/`ResolvedLLM` stay clean. Define code-default constants in `companion/__init__.py` (e.g. `_ASK_DEGREE_CAP_DEFAULT=8`, `_ASK_NEIGHBOUR_BUDGET_DEFAULT=2000`, `_ASK_HOPS_DEFAULT=1`, `_ASK_COVERAGE_DEFAULT=0.4`, `_ASK_RENDER_DEFAULT="typed_nodes"`).
- Live-read happens per call: `cfg = self._vault_config()` is already inside `ask()` (`companion/__init__.py:521`), so PATCH lands instantly. **Do not move config load to `__init__`.**

### 3.1 New module `src/marginalia/subgraph.py` — dataclasses + builder + renderer
Co-located new module (ADR §155 names `query.py` or a new `subgraph.py`; new module keeps `query.py` filesystem-free and the renderer pure).

**Dataclasses** (`NodeRecord`, `RelRecord`, `ClaimRecord`, `EgoGraph`) per finding 1 §3 — `EgoGraph(nodes: dict[str, NodeRecord], relationships: list[RelRecord], claims: list[ClaimRecord])`.

**Builder — id-based, not Node-object-based (advisor point 3):**
```python
def build_ego_graph(
    seed_ids: list[tuple[str, float]],   # (node_id, score) from search_claims, NOT QueryHit
    store: GraphStore,
    *,
    degree_cap: int,
    neighbour_budget_tokens: int,
    hops: int = 1,
) -> EgoGraph: ...
```
- Takes seed **ids** + scores + store, re-fetches every node (seeds included) via `store.get_node` so it always reads **raw `.facets`** (`S_id/P/O_id/O_literal/confidence/block_id/source_span`). This sidesteps the unverified question of whether `_public_node` (held by `QueryHit`) preserves facets, and keeps Phase A byte-identical. Claim/neighbour fetch goes through `get_node` anyway, so seeds doing the same is free.
- **Edge traversal (findings 1+2, load-bearing direction):** for each seed call `store.list_edges(src=seed_id)` and `store.list_edges(dst=seed_id)` (`store/protocol.py:7-27`), filter `edge.type` to the bridge set `{"rdf:subject", "rdf:object", "schema:mentions"}` plus domain edges. Direction is reversed by leg: `Claim -[rdf:subject]-> Subject` and `Claim -[rdf:object]-> Object` always have **Claim as `edge.src`**; `schema:mentions` has **Document as `edge.src`**. An entity seed is `dst` of its bridges → use `list_edges(dst=seed_id)` to find the Claims mentioning it, then those Claims' `rdf:object` edges (`list_edges(src=claim_id)`) to reach related entities. A Claim seed → `rdf:subject`/`rdf:object` to its subject/object entities.
- **Claim bridging is a second fetch:** the edge carries provenance/`block_id` facet; the Claim node (carrying `confidence` + `S_id`/`O_id`) is separate — `get_node(edge.src)` for each `rdf:*` bridge.
- **Degree cap (mandatory, RLM-on-KG hub-explosion blocker):** cap neighbours per seed at `degree_cap` **during collection**, before anything enters content. Rank candidates by edge-type relevance + Claim `confidence` (read from facets, default `_CLAIM_BASELINE_CONFIDENCE=0.7` if missing) before truncating. `_degree` (`reconcile/propose.py:89-92`) is O(E) full scan, acceptable at `_EXPANSION_SEEDS=10`.
- **Neighbour budget:** `neighbour_budget_tokens` is a *total* token budget across all seeds, applied at assembly — if seed 1 claims 1.5K, seed 2 gets 500.
- **Quality gates carried over (do not regress):** apply `is_infra()` (`query.py:262`) and `node_quality_weight` (`query.py:258`) so infra/low-quality nodes never enter the render.
- **n=2 is adaptive, never blanket:** `hops` param supports it but defaults 1; 2-hop is only turned on by the Phase 3 coverage gate (UFPE evidence-dilution finding). Do not ship 2-hop as a default.

**Addendum — 2026-07-08 (correction, not a rewrite):** this bullet is stale on all three counts. The live default is **2, not 1** (`_ASK_HOPS_DEFAULT = 2`, `companion/__init__.py:652`; the adjacent code comment: "Default ego-graph depth. 2 (was 1) so the answering claim can sit one entity hop over from the seed and still enter the pool"). It is applied directly in `build_ego_graph` on the main `ask()` path (`companion/__init__.py:4137`) — **not** gated behind any Phase 3 coverage-gate trigger; the render-budget cap, not a gate, is what bounds reach. The knob range is also wider than specced here: `hops: int | None = Field(default=None, ge=1, le=5)` (`config/_vault.py:586`, mirrored at `companion/__init__.py:132`), not `le=2`. The §3.0 field spec (line 46), the §5 knobs table `hops` row, and the §8 OQ3 row ("never blanket 2-hop") are the same stale `ge=1,le=2` / gate-only claim — see this addendum for the current values rather than duplicating the correction at each site.

**Renderer — pure, deterministic, no store/config/LLM calls (finding 6):**
```python
def render_ego_subgraph(
    ego: EgoGraph,
    *,
    max_token_budget: int,
    token_factor: float = 1.117,   # char/4 × 1.117, reference-host calibrated; tunable
) -> str: ...
```
- Emits three sections (ADR §99-121): `=== NODES ===` (`N{i} [Type] Name [QUERY_MATCH]?  (seed|neigh… block:… span:…)`), `=== RELATIONSHIPS ===` (`O_id` claims: `N{src} -[predicate]-> N{dst}  (claim:c11a… conf=…  block:a07c…)`), `=== CLAIMS (verbalized) ===` (`O_literal` claims: `- {subject} {predicate} {literal}. [c11a…]`).
- **`O_id` XOR `O_literal` branch is load-bearing** (`claim.py:63-76`): `O_id`→RELATIONSHIPS row, `O_literal`→CLAIMS row. Missing the branch silences half the knowledge.
- **Dedup on `(S_id, P, O_id|O_literal)` triple** before assembly (caller/builder responsibility) — this is what kills the 2.5-unique collapse (§2 note).
- **Drop metadata predicates** silently: `_METADATA_PREDICATES` = `{kind_of, tag, tags, label, labels, version}` (already imported in `query.py:19` from `extract/__init__.py`).
- **Verbalize predicates:** `_` → space, lowercase (`participated_in` → "participated in"). Pure string transform, no LLM.
- **Truncation safety:** seeds/NODES first and never trimmed (the index); over budget, drop lowest-confidence neighbour claims first. Anchor IDs (`claim:`/`block:`/`span:`) on every row for Tier-2 fetch without re-parsing.
- Read all fields from `node.facets` (not a `Claim` pydantic model) — `facets.get("S_id")` precedent at `vault.py:488`.

### 3.2 `Companion.ask()` injection — `companion/__init__.py:499-543`
- Retrieval (`hits = self._vault.query(question, k=k)`, line 509) and `citations = tuple(hit.node.id for hit in hits)` (line 510) stay **before** the branch. Citations remain original hit node ids — **never** expanded subgraph nodes (HTTP contract, `Answer.text/citations/hits`).
- After `cfg = self._vault_config()` (line 521), branch on `cfg.llm.ask.enable_subgraph` (step-direct, code-default `False`):
  - **Subgraph ON:** build seed ids from the raw `search_claims` output. The renderer needs raw `(node_id, score)` and raw facets — so expose the pre-`QueryHit` seeds. Simplest: add a thin `Vault.query_seeds(question, k)` returning `list[tuple[str, float]]` (the `search_claims` result ids+scores, before `_to_query_hits`), OR have `ask` call `search_claims` via a vault accessor. Then `ego = build_ego_graph(seed_ids, self._vault.store, degree_cap=…, neighbour_budget_tokens=…, hops=…)`; `context = render_ego_subgraph(ego, max_token_budget=…)`. Token guard: if rendered context somehow >100K tokens, truncate (seeds first) or abstain — assertion guard (subgraph is normally ~100-500 tokens vs 18K-280K block-dump).
  - **Subgraph OFF (default):** keep lines 514-520 unchanged (`_hit_text` bullet-dump).
- Same prompt scaffold (lines 517-520) + `_ASK_SYSTEM` (line 132 / 525) wrap either context.

### 3.3 Re-scope `expand_block_context` to Tier-2 only — `vault.py:399-405` / `query.py:322-365`
- `expand_block_context` (`query.py:322-365`) expands **adjacent same-file text blocks**, not graph neighbours — a context-bloat lever. Keep the function identical; **move its call site** out of `_to_query_hits` (`vault.py`, the `context_spans` assembly gated by `_query_neighbors`, `vault.py:416-428`). When `enable_subgraph=True`, do not populate `context_spans` from text neighbours; `expand_block_context` becomes a Tier-2-only depth tool (Phase 3). When `enable_subgraph=False`, leave the legacy `query_neighbors` text-expansion path exactly as-is (backward compat). Make the two orthogonal: subgraph on ⇒ ignore `query_neighbors`.

## 4. Phase 3 (planned, may defer) — coverage/abstain gate + Tier-2 `fetch_block` + path re-validation

Design-level only; full signatures deferred until Phase 2 beats baseline.

- **Coverage/abstain gate** = the Tier-1→Tier-2 trigger. Signal: seed similarity / ego-graph density / claim coverage of interrogative terms (reuse `_question_terms`, `query.py:54-68`). Below `coverage_threshold` (knob §5, code-default 0.4): drill to Tier-2 blocks **or** abstain. The gate supplies the missing **abstention floor** (root cause of 40% absent-trap hallucination). **Keep abstain orthogonal to provider-down** (advisor): abstain is a deliberate zero-return; `LLMProviderError → text=""` (line 540-541) is graceful degrade — separate paths, do not conflate.
- **`fetch_block(node|claim)` (Tier-2):** resolve anchor via `vault.py:_provenance_for_node` (454-492) + `_read_slice` (`companion/__init__.py:933-941`), repackaged as an explicit on-demand tool. Adaptive 2-hop (`hops=2`) is turned on here, gated per-question, never blanket.
- **Security — path re-validation (load-bearing gap):** `_read_slice` (`companion/__init__.py:933-941`) calls `Path(path).read_bytes()[start:end]` with **zero validation**. The span path is re-validated in `vault.py:_anchor_from_span` (449: `abs_path.relative_to(root)`), but the **legacy `block_id` fallback branch** (`vault.py:461-478`) yields `path` with **no `relative_to(root)` guard**. Phase 3 must add `root = self._vault.path.resolve(strict=False); Path(abs_path).resolve().relative_to(root)` before any Tier-2 byte read, on **both** the span and block_id paths (security_byte_read_path_revalidation). The legacy-branch gap is the load-bearing fix.
- **MCP surface for `fetch_block`** is an architecture decision (ADR OQ6), not a knob — deferred.

**Addendum — 2026-07-08 (correction, not a rewrite):** this section's "planned, may defer" header and the "Design-level only; full signatures deferred" line above are stale — Phase 3 (coverage/abstain gate + Tier-2 on-demand fetch) shipped 2026-06-07; see "Phase 3 SHIPPED" at line ~220 and its 0.45→0.61 eval delta. The `fetch_block(node|claim)` bullet's line refs are also stale: `vault.py:_provenance_for_node (454-492)` is now `vault.py:643-694` (legacy `block_id` re-validation at :668-680), and `_read_slice` is now at `companion/__init__.py:4872-4890`, not `933-941`. The bullet's "Adaptive 2-hop (`hops=2`) is turned on here, gated per-question, never blanket" is also stale — `hops` defaults to 2 for every `ask()` call unconditionally (`_ASK_HOPS_DEFAULT = 2`, `companion/__init__.py:652`), not adaptively behind this gate (see the §3.1 addendum, ~line 77, for the full correction). The MCP-surface deferral in the bullet immediately above is still accurate as of this pass — no `fetch_block` MCP tool exists in `mcp_server.py`; the Tier-2 fetch is internal to `ask()` only.

## 5. Config knobs table (all live `llm.ask.*`, PATCH `/api/v1/config`, hypertune-sweepable)

| Knob (`llm.ask.*`) | Default (code-fallback) | Read at | Phase | Sweeps OQ |
|---|---|---|---|---|
| `enable_subgraph` | `False` | `companion.ask` step-direct | 2 | flag |
| `max_degree_per_seed` | `8` | `build_ego_graph` | 2 | OQ2 |
| `neighbour_budget_tokens` | `2000` | `build_ego_graph` / render | 2 | OQ2 |
| `hops` | `1` (`ge=1,le=2`) | `build_ego_graph`; n=2 only via Phase 3 gate | 2/3 | OQ3 |
| `coverage_threshold` | `0.4` | Phase 3 gate | 3 | OQ4 |
| `render_format` | `"typed_nodes"` (`"verbalized"`/`"hybrid"` later) | `render_ego_subgraph` | 2/3 | OQ5 |
| `min_claim_confidence` | `0.0` (no prune) | `_mint_relationship_claims:848` | 1 | — (extractor; rebuild-gated) |

All on `StepLLM.ask`; `llm` already in `WRITABLE_BLOCKS` (no `WRITABLE_BLOCKS`/`REEMBED_FIELDS` change). The machine-local hypertune helper already PATCHes `llm.ask.*` — extend its kwargs to carry the new keys; no new setter, no YAML rewrite. Note: `min_claim_confidence` is extractor-time — changing it requires a `kg rebuild` to take effect, unlike the runtime Tier-1 knobs.

## 6. Test plan

- **Baseline to beat: 0.69 overall** (120-Q reference evaluation, 2026-06-06). Per-tier: factual 0.85, **aggregation 0.44, synthesis 0.44**; 40% absent-trap hallucination; 13/120 false "not in notes."
- **Workflow:** `marginalia-knowledge-quality-eval` for the scorecard; `marginalia-rag-hypertune` to sweep the new `llm.ask.*` knobs with **no graph rebuild** (Tier-1 knobs are all live). Daemon up on :7777, reference model host warm.
- **Judge: thinking-OFF mandatory** (a capable model; 0.8B judges emit all-unparseable). The caller supplies and records the concrete provider endpoint and model id.
- **Watch:** (1) **aggregation + synthesis tiers** — these collapsed tiers are the ones topology should lift; if subgraph render doesn't move them, the ego-graph isn't carrying the relationships. (2) **absent-trap hallucination** — should not regress in Phase 2 (the abstention floor is Phase 3); Phase 3 must measurably drop it. (3) **factual tier** — must not regress from the Document-drop (1b). (4) **context token budget** — the whole point: subgraph must fit where k20/n2 block-dump (280K) OOM'd the box, unblocking the stalled Stage B2/C k-sweep.
- **Regression invariant:** with `enable_subgraph=False`, the full default eval must be byte-identical to today (Phase A immutability). Pin this before flipping any default.

## 7. Risks & invariants

- **ADR-0007 in-place-mutation hazard (hardest constraint):** never bulk-delete/mutate nodes or edges on the populated Ladybug graph — it scrambles edge adjacency (proven 0→1227, `ladybug.py` add_edge destructive-upsert warning at :198-202). All Phase 1 hygiene routes through extractor-fix + `kg rebuild` (fresh graph + atomic swap, `_curation.py:353-367`). Markdown is the trust root.
- **Hub explosion:** degree cap + relevance-rank-before-truncate + token budget are **mandatory**, applied during neighbour collection (RLM-on-KG). A naive walk lets a hub Document with 100+ `schema:mentions` bloom the context.
- **Citation-contract stability:** `Answer.text/citations/hits` and HTTP serialization (`http.py:537-562`) unchanged. Citations stay original hit node ids (line 510), never expanded subgraph nodes. If a config field is ever put on `QueryHit`, it must be non-serialized (HTTP `_serialize_hit` risk) — the chosen design avoids this by reading config via `self._vault_config()` on demand, not threading it through the hit.

**Addendum — 2026-07-08 (correction, not a rewrite):** the `http.py:537-562` citation above is stale. `_serialize_hit` is now defined at `http.py:2209` (call sites `:2693`, `:2738`); lines 537-562 are `health()`'s folder-watch/queue-error checks, unrelated to serialization.
- **`resolved()` blind spot:** ask-only knobs read step-direct (`cfg.llm.ask.X or default`), never via `resolved("ask")` (which drops `StepLLM`-only fields).
- **Security path re-validation:** Tier-2 `fetch_block` must add `relative_to(vault_root)` on the legacy `block_id` branch (`vault.py:461-478`) before exposing bytes — the gap `_anchor_from_span` already closed for spans (:449).

**Addendum — 2026-07-08 (correction, not a rewrite):** the bullet above is stale — this was already shipped on 2026-06-07. There is no `fetch_block` function in `vault.py`; the legacy `block_id` branch lives in `_provenance_for_node` (`vault.py:643-694`, the re-validation check itself at `:668-680`), which resolves the path against the vault root and blanks it on failure. The span-branch guard cited here as `_anchor_from_span (:449)` is now at `vault.py:619`. See the later confirmation ~line 220 ("Security path re-validation added on both the span and legacy block_id read paths (reviewed clean)") for the accurate status.
- **Phase-1 ordering:** hygiene fixes must land + rebuild before subgraph render ships; the structured render makes junk claims visible where block-dump buried them.

## 8. Open questions → empirical resolution

| ADR OQ | Resolution path |
|---|---|
| **OQ1** Fixed-hop vs PPR | **Architecture decision, not a knob** — ship fixed-hop (`hops` knob), name PPR (HippoRAG/TERAG) as the next rung. Deferred. |
| **OQ2** Degree cap + neighbour budget | Sweep `max_degree_per_seed` × `neighbour_budget_tokens` against the NX vault hub distribution via hypertune. |
| **OQ3** n=2 trigger | Sweep `hops` + the Phase-3 adaptive-gate signal (question tier / unmet coverage); never blanket 2-hop. |
| **OQ4** Coverage-gate threshold | Sweep `coverage_threshold` against the absent-trap tier — abstain without over-declining (13 false "not in notes" already exist). |
| **OQ5** Render format A/B | Sweep `render_format` (`typed_nodes` vs `verbalized` vs `hybrid`). v1 ships `typed_nodes` only. |
| **OQ6** MCP surface for `fetch_block` | **Architecture decision** (agentic posture, ADR 0009), not sweepable — deferred to Phase 3. |

---

**Line-number / finding corrections vs ADR & findings (verified against live code 2026-06-07):**
- ADR cites the 1-hop walk at `query.py:232-250`; live code confirms the walk is at **235-250** (edge discard at 237-240, score-borrow at 250). Accurate enough; finding 1 is correct.
- Markdown wikilink predicate is **`links_to`** (markdown.py:288), not the finding's prose "links to". The text-payload bug (markdown.py:295) and the parallel tag bug (markdown.py:281) are both real and confirmed.
- Config: finding 3 *recommends* a new top-level `query` block (Option A); this plan rejects it for `llm.ask.*` (its Option B) because `llm` is already writable (`_vault.py:468`) and hypertune already PATCHes it. Resolves the finding's internal contradiction with the hard constraint.
- `resolved()` only carries `LLMDefaults` keys (`_vault.py:344-350`) — ask-only knobs must be read step-direct, not via `resolved("ask")`. Findings 1/3 imply `cfg.llm.resolved("ask").X`; that silently drops the knob. Corrected.
- `_read_slice` (`companion/__init__.py:933-941`) and the legacy `block_id` branch (`vault.py:461-478`) both confirmed to lack the `relative_to(root)` guard that `_anchor_from_span` (:449) has. Security fix confirmed load-bearing.

---

## Eval results — 2026-06-07 (first end-to-end test)

**Setup.** Healed graph (reembed, no re-extraction → **hygiene NOT applied**), box backend `qwen3.6-27b` (codex fixed the box-side thinking-runaway: `reasoning=off`), thinking-off, k=20, judge = sonnet over the 120-Q NX eval. `hops=2` is a **no-op today** (builder is hardcoded 1-hop; 2-hop is Phase 3), so only `degree`/`budget` were live.

| Config | overall | factual | multihop | aggregation | absent-trap | missed | hallucinated |
|---|---|---|---|---|---|---|---|
| **Subgraph** `d8/b2000/1-hop` | **0.44** | 0.67 | 0.25 | 0.13 | 0.80 | 41 | 2 |
| **Subgraph** `d20/b8000/1-hop` | **0.45** | 0.68 | 0.20 | 0.13 | 0.85 | 43 | 1 |
| Block-dump k20 (box, today) | **0.854** | 0.95 | 0.78 | 0.56 | 0.70 | — | ~3 |
| Orig baseline k8 (2026-06-06) | 0.69 | 0.85 | — | 0.44 | 0.60 | 13 | 40% |

**Findings (load-bearing):**
1. **Mechanism validated, context 133× smaller** — subgraph render median **488 tok** vs block-dump **66,061 tok** (no-LLM measurement, `context_size.py`). Latency ~4s/q.
2. **Safest config yet** — only **1–2 hallucinations** total (vs block-dump's 40% absent-trap rate) and the **best abstention** (0.80–0.85 on traps). When the ego-graph carries the fact, the answer is correct.
3. **But it under-recalls: ~0.44–0.45 overall, dominated by ~41–43 "missed"** (declines on facts that exist). Tanks multihop (0.20–0.25) and aggregation (0.13).
4. **The gap is STRUCTURAL, not budget** — 4× degree+budget (`d8/b2000`→`d20/b8000`) moved overall by **+0.01**. More 1-hop neighbours don't help when the answer needs (a) a **2-hop chain** (Phase 3, unwired) or (b) a **hygiene rebuild** (claims this graph never extracted cleanly).

**Conclusion.** The 1-hop subgraph on the un-hygiened graph is the **floor** (~0.45), not the ceiling. It is mechanically sound and far safer than block-dump, but to beat 0.854 it needs the two unpulled levers: **wire 2-hop traversal** (Phase 3 in `build_ego_graph`, currently `_ = hops`) and **apply Phase-1 hygiene via a full `kg rebuild`** (now feasible — the inference host is fixed). Knob-sweeping degree/budget is a dead end on its own.

Artifacts: machine-local answer JSONL, context-size measurements, subgraph evaluation output, and judge records. These are operational evidence and are not retained as release-source fixtures.

### Update — 2-hop + N-hop parametrization (2026-06-07, same session)

Wired real N-hop expansion in `build_ego_graph` (was hardcoded 1-hop, `_ = hops`); `hops` is now a live N-depth knob (`llm.ask.hops`, `ge=1 le=5`), per-level degree cap tightens with depth (`degree_cap // level`), frontier bounded.

**Addendum — 2026-07-07 (correction, not a rewrite):** the per-level degree-cap formula in the line above (`degree_cap // level`) is stale. As of the 2026-07-05 commit ("feat(subgraph): multi-hop ego-graph reach... bounded by answer-aware ranking + budget cap"), the live formula in `src/marginalia/subgraph.py:412-418` is `level_cap = max(degree_cap // 2, 4)` — a floor, not a per-level tightening. The code's own comment explains why: depth-2 hub expansion is exactly where a genuine cross-entity chain lands, so tightening further as `degree_cap // level` would defeat the reason to walk past hop-1; the render budget, not this cap, is the hard bound on output size. Per this repo's convention, the original line above is left as-is and this correction is appended instead of rewriting it.

| Config | overall | factual | multihop | aggregation | missed | halluc |
|---|---|---|---|---|---|---|
| 1-hop d8/b2000 | 0.44 | 0.67 | 0.25 | 0.13 | 41 | 2 |
| 1-hop d20/b8000 | 0.45 | 0.68 | 0.20 | 0.13 | 43 | 1 |
| **2-hop d8/b2000** | **0.45** | 0.69 | 0.25 | 0.13 | 40 | 1 |
| block-dump k20 | 0.854 | 0.95 | 0.78 | 0.56 | — | ~3 |

**DECISIVE FINDING: degree, budget, AND hop-depth all plateau the subgraph at ~0.45.** The bottleneck is NOT retrieval traversal — it is that the **structured claim layer doesn't carry the answerable detail the raw 66K-token block-dump does** (Tier-1 compresses 66K→488 tokens and loses winning facts). Knob-sweeping is a confirmed dead end.

**Therefore the two remaining levers, in likely-impact order:**
1. **Tier-2 `fetch_block` on demand (ADR Phase 3, UNIMPLEMENTED)** — the ADR's own mechanism to recover raw-text detail when Tier-1 is thin. Almost certainly the real gap-closer; the current eval is Tier-1-only.
2. **Hygiene rebuild** — cleans/dedups claims but does NOT add facts; likely nudges precision, not the 40-pt recall gap.

**Addendum — 2026-07-08 (correction, not a rewrite):** item 1 above ("UNIMPLEMENTED") is stale — Tier-2 `fetch_block` on-demand fetch shipped later the same day (2026-06-07); see "Phase 3 SHIPPED" a few lines below (~line 220) for the shipped design and the 0.45→0.61 eval delta it produced.

The honest standing trade today: subgraph Tier-1 = 0.45 at 133× smaller context + near-zero hallucination + best abstention; block-dump = 0.854 at 66K tokens + 40% absent-trap hallucination. Tier-1 alone is a safety/cost win, not an accuracy win. Closing accuracy needs Tier-2.

**Rebuild blocker (corrected):** the earlier "stall" was a self-inflicted `max_tokens=4000` cut truncating extraction JSON → 0 nodes (NOT the `MAX_NODES_PER_BLOCK=12` breaker, which scales to 150 for 12KB). At 16000 tokens, 27b extracts 19 nodes/12KB-chunk in ~85s → rebuild viable but ~1hr; 35b-A3B is slower, no win.

### Update — Tier-2 fetch_block + coverage/abstain gate (Phase 3 SHIPPED, 2026-06-07)

Implemented the ADR's Tier-2: when Tier-1's subgraph answer is an abstention (or the render is thin), the coverage gate fetches the raw blocks of the SAME hits (block-dump, path-revalidated) and re-answers; abstention floor holds if Tier-2 also declines. Security path re-validation added on both the span and legacy block_id read paths (reviewed clean). Two majors fixed in review: gate over-decline (`return tier1` when Tier-2 abstains) + reverted the always-on Document-drop (default path now byte-identical).

| Config | overall | factual | multihop | aggregation | absent-trap | missed | halluc |
|---|---|---|---|---|---|---|---|
| Tier-1 only (best knobs/hops) | 0.45 | 0.69 | 0.25 | 0.13 | 0.85 | 40 | 1 |
| **Tier-2 (subgraph + on-demand fetch)** | **0.61** | 0.76 | 0.58 | 0.19 | 0.70 | 21 | 3 |
| block-dump k20 | 0.854 | 0.95 | 0.78 | 0.56 | 0.70 | — | ~3 |
| orig baseline k8 | 0.69 | 0.85 | — | 0.44 | 0.60 | 13 | 40% |

**Result: Tier-2 = 0.61, +0.16 over Tier-1.** Abstention crashed 46%→8%; "missed" 40→21 (recovered ~19 retrieval misses). multihop 0.25→0.58. The ADR's two-tier design is validated end-to-end.

**Why it lands at 0.61, not block-dump's 0.854:**
1. **The gate only escalates on EXPLICIT abstentions.** ~28 Tier-1 answers were *partial* (confident-but-incomplete) — these never trip the gate, so they stay at Tier-1 quality. A confidence/completeness-based gate (not just abstention-string) is the next lever (OQ4, `coverage_threshold` tuning).
2. **Tier-2 added cost:** incorrect 7→9, hallucinated 1→3, absent-trap 0.85→0.70 — the block-dump retry occasionally fabricates on absent-traps (block-dump's own 40%-hallucination weakness leaking into Tier-2).
3. **aggregation (0.19) stays low** — synthesis across many blocks is hard for both tiers.

**The honest standing:** the full ADR 0011 (Tier-1 subgraph + Tier-2 on-demand fetch) is implemented & validated. It buys a tunable safety/cost/accuracy midpoint: 0.61 at far smaller average context + far less hallucination than block-dump's 0.854. Closing the rest needs a smarter gate (escalate on partial/low-confidence, not just abstention) — the designed `coverage_threshold` knob is the handle.
