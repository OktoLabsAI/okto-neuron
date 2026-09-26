# ADR 0011: Subgraph-first answer assembly (graph-RAG) with on-demand block fetch

- **Status:** Accepted; implemented as an opt-in path, default policy refined by ADR 0028
- **Date:** 2026-06-07
- **Deciders:** Alex Rivera, Marginalia core
- **Builds on:** ADR 0003 (provenance is a span, not a node), ADR 0005 (claim→entity bridge edges), ADR 0006 (source-link every entity), ADR 0008 (off-graph reconciliation, query-time fold)
- **Relates to:** ADR 0002 (chunking & provenance), ADR 0009 (curation control plane), ADR 0010 (reconciler recall overhaul)
- **Supersedes:** nothing. It re-purposes existing machinery (the 1-hop ranking walk in `query.py`) rather than replacing a prior decision.

**Lifecycle addendum — 2026-07-13.** Ego-graph expansion, typed subgraph rendering,
multi-hop control, and on-demand source fallback shipped. Evaluation did not justify
making this the universal default: `llm.ask.enable_subgraph` remains off by default, and
ADR 0028 supplies the accepted bounded efficient-hybrid policy when enabled. Historical
default-flip language below is therefore closed, not pending release work.

---

## Context

### How `ask` works today — the block-dump anti-pattern

`ask` is a thin shell over `recall`: retrieve top-k hits, read the raw source byte-slice each hit anchors to, concatenate those slices, and hand the whole blob to the LLM as "Notes."

The flow, with file:line citations:

1. **HTTP surface.** `POST /api/v1/ask` takes `question` + optional `k` (default **20** per ADR 0018's validated seed_k, max `MAX_QUERY_K=100`) and proxies to `Companion.ask(question, k=k)` (`src/marginalia/server/http.py:1337-1348`). `POST /api/v1/recall` is the same retrieval path with `k` default **10** (`http.py:1298-1307`).

2. **Retrieval.** `Companion.ask()` calls `self._vault.query(question, k=k)` (`src/marginalia/companion/__init__.py:499-543`, retrieval at `:509`). `vault.query` runs `search_claims()` (`src/marginalia/query.py:176-270`), which fuses a vector leg, a lexical leg, and (for interrogatives) an IDF-weighted answer-claim leg via RRF, then maps results to `QueryHit`s with byte-range provenance.

3. **Context assembly = read the raw blocks.** `Companion.ask()` builds the answer context as a bullet list of the *grounding text* of every hit:
   ```python
   context = "\n".join(f"- {snippet}" for hit in hits if (snippet := _hit_text(hit)))
   ```
   (`companion/__init__.py:514-516`). `_hit_text()` reads the literal source byte-slice `prov.path[prov.byte_start:prov.byte_end]` and, if `query_neighbors>0`, appends adjacent **text** blocks in document order, falling back to `node.name` (`companion/__init__.py:944-968`, byte reader `_read_slice` at `:933-941`).

4. **Prompt.** "Answer the question using only the retrieved notes below." + question + the concatenated block text (`companion/__init__.py:517-520`), system prompt `_ASK_SYSTEM = "You answer grounded in the provided notes. Be concise."` (`:132`, `:525`).

**The anti-pattern:** the answer context *is* a dump of raw source chunks. Because chunks are fixed ~12 KB windows (see Evidence), k=8 dumps up to ~83 KB / ~20K tokens of mostly-redundant prose, and the graph structure the system spent extraction effort building is used only to *rank* the dump — never to *form* it. The 1-hop graph walk that exists today (`query.py:232-250`) borrows a neighbour's **score** to re-rank seeds but **never fetches the neighbour's content** into the answer (explicit in the code comment at `:233-234`). So the graph is a retrieval scorer, not an answer substrate.

### Why the schema already supports subgraph retrieval

Marginalia's provenance model is already a graph whose atomic unit is a structured assertion anchored to immutable source bytes. Nothing new needs to be invented to answer from structure:

- **`Claim` is the atomic S-P-O unit**, carrying `S_id`, `P`, `O_id` xor `O_literal`, `confidence`, and three required PROV anchors `block_id` / `extraction_activity_id` / `agent_id` (`src/marginalia/schema/support/claim.py:33-88`; PROV edges locked in `CLAIM_PROV_EDGES`). This is exactly the verbalizable triple graph-RAG systems answer from.
- **Claims are bridged to their entities.** ADR 0005 mints `rdf:subject` (always) and `rdf:object` (when the object is an entity) edges Claim→entity, and `schema:mentions` Document→entity for de-orphaning (ADR 0005 F1/F2/F4; node-derived at the `remember()` chokepoint per ADR 0006). So from any seed entity you can traverse to its claims and from claims back to other entities — a real ego-graph.
- **`Block` is the immutable byte-anchored truth root**, separate from the derived node/edge/claim layer. `Claim -[prov:wasDerivedFrom]-> Block(path, byte_start, byte_end, content_hash)` (`claim.py:33-50`, RFC.md §4.3). ADR 0003 demotes the anchor to a `SourceSpan` value-object but keeps Block as the canonical fetch target.
- **The store exposes graph traversal.** `GraphStore` provides `list_edges(src=, dst=, type=)`, `get_node(id)`, `nodes_with_embeddings(type=)`, `search_text(query, k, type=)` (`src/marginalia/store/protocol.py:1-28`). The `/api/v1/nodes/{id}/neighbors` BFS endpoint already does capped, undirected, filtered ego-graph expansion (`http.py:959-1070`).

The pieces — seed match, edge traversal, claim bridging, byte-anchored blocks — are all present. The decision is to *answer from the structure instead of from the block dump*.

### Evidence of the problem

All numbers below are retained historical benchmark summaries; raw machine-local evaluation artifacts are deliberately not release sources. **Two denominators are in play and must not be conflated:** the failure root-cause split is over **49 analyzed failures**; the 0.69 accuracy is over the full **120-question** ground-truthed eval.

**Baseline.** `ask` scores **0.69** overall on the 120-Q reference evaluation (2026-06-06). Per-tier collapse on `aggregation` (0.44) and `synthesis` (0.44); strong only on `factual` (0.85). 40% hallucination on the 10 absent-trap questions (no abstention floor). 13/120 (~11%) false "not in notes" where content exists (retrieval misses, low ~0.21–0.24 similarity).

**Failure root cause (49 analyzed failures).** Systemic (retrieval/ranking/dedup) **67% (33/49)**; LLM (reasoning/fabrication) **31% (15/49)**; KG-quality (missing node) **2% (1/49)**. Empirically, raising k 8→20 fixed 8/11 tested systemic failures. **"Information is almost never the problem" — retrieval is the bottleneck.**

**Context bloat is the mechanism.** Context size follows `k × (1 + 2·neighbours) × ~12 KB`. Measured token budgets (densegrid, char/4 × **1.117** tokenizer calibration vs the reference model host):

| Config | Avg tokens | Max tokens | Feasible (<262K)? |
|---|---|---|---|
| k8, n0 | 18,659 | 23,752 | yes |
| k12, n0 | 29,712 | 35,638 | yes |
| k20, n0 | 52,137 | 58,854 | yes |
| k8, n1 | 48,622 | 62,485 | yes |
| k8, n2 | 72,772 | 103,808 | tight |
| k20, n1 | 133,096 | 169,237 | risky |
| k20, n2 | 195,000 | **280,485** | **NO — over budget** |

Source: retained benchmark summary from the reference evaluation.

**Duplicate-window collapse.** At k=8, mean **2.5 unique windows / 8 hits** (28/49 queries ≤2 unique); near-identical `wikilink:`-derived Claim nodes from one chunk crowd top-k. Effective retrieval is ~3 unique ~11.7 KB blocks per query. Source: `data.json` systemic anatomy.

**Zero-byte Document hits.** Document nodes rank #1 on name match but resolve to an empty byte-slice (byte 0..0), wasting ≥1 of 8 slots per query (e.g. q114–q117). Source: empirical, `data.json`.

**Graph neighbours are off and mis-named.** Block-neighbour expansion is wired but **default-off** (`query_neighbors=0` → `context_spans=[]`); resolution order env `MARGINALIA_QUERY_NEIGHBORS` → `marginalia.yaml` → 0 (`src/marginalia/vault.py:416-428`). Worse, when on it expands **adjacent TEXT blocks on the same file** (`expand_block_context`, `query.py:322-365`), not graph neighbours — so "neighbours" today re-bloats context with more raw prose instead of pulling related *entities*.

**Recall curve proves the headroom (Stage A, no-LLM anchor recall).** k8/n0 recall 0.59 (below answer floor); k20/n2 recall **0.95** (knee). But the configs that reach the knee are exactly the ones that blow the token budget (k20/n2 = 280K max). So under the block-dump model, high recall and a feasible context are mutually exclusive.

**The tuning sweep stalled on this exact wall.** Stage B1 (model/thinking/prompt @ k8/n0) validated 4 configs (best 27b+think **0.742**, +5.2pt; practical 35b+think-off 0.704 @ half the hallucination). Stage B2/C (the k/neighbours axes) is **INVALID** — the reference model host OOM-crashed on 143K–220K-token contexts. Tuning k and neighbours is blocked until context stops exploding.

The conclusion: the dominant failure is retrieval, the cure (more recall) requires bigger context, and bigger context is infeasible under raw-block-dump. The way out is to make the context *small and structured* instead of *large and raw*.

---

## Decision

**Move `ask` from raw-block-dump retrieval to subgraph-first (graph-RAG) answer assembly, with on-demand block fetch.**

### The two-tier design (crisply)

- **Tier 1 — answer from structure.** Retrieve the best-matching seed nodes (semantic + lexical, as today). Expand each seed to a **relevance-capped ego-graph**: the seed, its claims, and its **graph neighbours** at n=1 (selectively n=2), reached via `rdf:subject`/`rdf:object`/`schema:mentions`/SKOS/domain edges. Render that compact structure (typed nodes + verbalized relationships + claims, each carrying its source anchor IDs) as the answer context. Answer from that.
- **Tier 2 — fetch the block on demand.** Raw source text is fetched only when needed for verification or depth, via a `fetch_block(node|claim)` tool that resolves the Block/`SourceSpan` anchor `(path, byte_start, byte_end, content_hash)` and re-validates the path against the vault root before exposing bytes. A coverage/abstain gate is the Tier-1→Tier-2 trigger.

### Trust hierarchy

- **Blocks = immutable, byte-anchored truth root.** They never change without the markdown changing; they are what citations point to and what Tier-2 quotes from.
- **Nodes + Edges + Claims = the derived, queryable layer you answer from.** This layer is rebuildable from the blocks (markdown is the trust root; `kg rebuild` re-derives it). Answering from this layer is answering from *structured, deduplicated, traversable knowledge* rather than from raw prose.

### The reframe: graph neighbours, not text neighbours

Today "neighbours" means **adjacent text blocks in the same file** (`expand_block_context`, `query.py:322-365`; gated by `query_neighbors`, `vault.py:416-428`). That is a context-bloat lever, not a knowledge lever. Under this ADR, **"neighbours" means GRAPH neighbours — related entities reached by edges.** The existing 1-hop ranking walk (`query.py:232-250`) already traverses those edges; today it borrows only the neighbour's *score*. We re-purpose that same walk to also feed the neighbour's *content* into Tier-1. `expand_block_context` is demoted to a Tier-2-only depth tool; it is no longer the meaning of "neighbour."

### Render format (worked example — illustrative, not a real query result)

Compact, typed, ID-referenced, anchor-first, truncation-safe — modeled on the schema-aware `=== NODES === / === RELATIONSHIPS ===` layout (UFPE, see External sources). Query-matched seeds are serialized first so that if the context is truncated, the periphery drops first. Each row carries its anchor IDs so Tier-2 can fetch on demand. For a question like "Who is Luke Gray and what did he work on?":

```
=== NODES ===
N1 [Agent] Luke Gray  [QUERY_MATCH]
N2 [Activity] reference evaluation kickoff workshop
N3 [Concept] handwriting recognition
N4 [InformationObject] PoC scope note  (block:b3f9…  span:notes/poc.md:1204-1390)
N5 [Agent] Carlos Leao

=== RELATIONSHIPS ===
N1 -[participated_in]-> N2          (claim:c11a… conf=0.91  block:a07c…)
N1 -[has_focus]-> N3                (claim:c2f0… conf=0.84  block:a07c…)
N1 -[authored]-> N4                 (claim:c5d1… conf=0.88  block:b3f9…)
N1 -[collaborated_with]-> N5        (claim:c9e2… conf=0.79  block:d44e…)

=== CLAIMS (verbalized) ===
- Luke Gray participated in the reference evaluation kickoff workshop. [c11a…]
- Luke Gray's focus area is handwriting recognition. [c2f0…]
- Luke Gray authored the PoC scope note. [c5d1…]
```

This renders the *same* Luke Gray knowledge in a few hundred tokens that the block-dump would spend ~20K tokens (and ~3 unique windows) to convey. Predicates are verbalized to readable phrases rather than opaque slugs (Walk&Retrieve). The model answers from this; if it needs the exact wording of the PoC scope, it calls `fetch_block(N4)`.

---

## Design details

### Retrieval pipeline (Tier 1)

1. **Seed match.** Reuse `search_claims()` (`query.py:176-270`) to get the top seed nodes by RRF over vector + lexical + answer-claim legs. Keep the existing infra filter (`is_infra`) and the untyped-Block drop (`query.py:262-263`). Seeds are entities/claims, not raw blocks.

2. **Ego-graph expansion (graph neighbours).** For each of the top `_EXPANSION_SEEDS` (currently 10, `query.py:33`), traverse `store.list_edges(src=)` / `list_edges(dst=)` to collect 1-hop neighbours and the claims bridging them (`rdf:subject`/`rdf:object`, ADR 0005). **Default n=1.** n=2 only behind a relevance gate (see below). This is the *same* walk as `query.py:232-250` — change is that we now retain the neighbour **nodes and their bridging claims as content**, not just `fused[neighbour] += seed_score * _HOP_DISCOUNT`.

3. **Hub/degree cap + relevance-ranked neighbour selection.** Naive expansion floods context from high-degree hub nodes. Mitigations, all applied at expansion time (before anything enters context):
   - **Per-node degree cap** on how many neighbours a seed may contribute (RLM-on-KG degree-bounded expansion).
   - **Relevance ranking** of candidate neighbours/claims, then a **budgeted top-set** sized to the model context (SubgraphRAG; LightRAG degree-centrality + per-stage token truncation). Rank by edge relevance and structural distance, optionally claim `confidence`.
   - **n=2 only adaptively** — gated on question type / unmet coverage, never blanket (UFPE: unpruned 2-hop regresses via *evidence dilution*, not just token overflow).
   - PPR/personalized-walk seeding (HippoRAG/TERAG) is named as the principled "next rung" beyond fixed-hop, deferred (see Open questions).

4. **Render.** Emit the `=== NODES === / === RELATIONSHIPS === / === CLAIMS ===` block above. Dedup before assembly (the 2.5-unique-of-8 collapse disappears once the unit is the entity, not the chunk). Verbalize predicates. Anchor-first ordering for truncation safety.

5. **Answer.** Feed the rendered subgraph as the grounded context. Citations remain node/claim IDs (as today, `companion/__init__.py:510`).

### Tier-2 fetch + coverage gate

- **`fetch_block(node|claim)` tool.** Resolves the anchor via the existing `_provenance_for_node` path (`vault.py:454-492`: `source_span` facet → legacy `block_id`) and reads bytes via `_read_slice` (`companion/__init__.py:933-941`). **Re-validate `prov.path` against the vault root before exposing bytes** (security_byte_read_path_revalidation; RAGA verify step). This is the existing block-read path, repackaged as an explicit on-demand tool.
- **Coverage / abstain gate = the Tier-1→Tier-2 trigger.** When the rendered subgraph does not cover the question (low seed similarity, sparse ego-graph, or a model-signaled "insufficient"), either drill to Tier-2 blocks or abstain. This gate also supplies the missing **abstention floor** (root cause of the 40% absent-trap hallucination): if neither structure nor fetched blocks support an answer, decline rather than fabricate.

### Concrete code touch-points

- `src/marginalia/query.py:232-250` — repurpose the 1-hop walk to retain neighbour content (nodes + bridging claims), not just score. New: degree cap + relevance-ranked, budgeted neighbour selection.
- `src/marginalia/companion/__init__.py:499-543` (`Companion.ask`) — replace block-dump context assembly with subgraph render; add the coverage/abstain gate and Tier-2 fetch loop.
- `src/marginalia/companion/__init__.py:944-968` (`_hit_text`) — no longer the answer substrate; becomes the Tier-2 fetch body.
- New: subgraph render function (typed nodes + verbalized edges + claims, anchor-first). Likely co-located with `query.py` or a new `subgraph.py`.
- New: `fetch_block` tool wiring (MCP + the ask loop), reusing `_provenance_for_node` (`vault.py:454-492`) + path re-validation.
- `src/marginalia/vault.py:416-428` (`_query_neighbors`) / `query.py:322-365` (`expand_block_context`) — re-scope to Tier-2 depth only; stop being the meaning of "neighbour."

---

## Consequences

### Positive

- **Context shrinks 10–100×.** An entity-centric answer drops from ~20K tokens of raw blocks (≈3 unique windows + dups) to a few hundred tokens of structure. The ≤100K budget becomes trivially met; the reference-host OOM wall that invalidated Stage B2/C disappears.
- **Precision and recall both rise without the budget conflict.** Stage A showed high recall needs k20/n2 (280K tokens, infeasible under block-dump). Subgraph render lets the ego-graph carry the same coverage at a fraction of the tokens, so the recall-vs-budget tradeoff that stalled tuning is broken.
- **Demotes the 12 KB-chunk problem.** Chunk size only matters at Tier-2 drill-down now, not on every answer. The duplicate-window collapse and zero-byte Document hits stop wasting answer slots (the unit is the entity/claim, deduped).
- **Adds the missing abstention floor** via the coverage gate — directly attacks the 40% absent-trap hallucination.
- **Smaller local model stays competitive.** Structured subgraph context lets a reference local model punch above its weight vs raw-chunk stuffing (SubgraphRAG/TERAG).

### Negative / risks

- **Extraction quality becomes load-bearing.** When you answer from claims, junk claims surface directly. Must clean: `wikilink:`-derived near-duplicate Claim nodes, 0-byte Document nodes, and low-confidence noise. This is why the rollout sequences extraction hygiene **first**.
- **Hub explosion if neighbour selection is naive.** Degree cap + relevance budget + adaptive n=2 are mandatory, not optional (RLM-on-KG, SubgraphRAG, UFPE evidence-dilution finding).
- **Multi-hop / synthesis may still need several nodes or a few blocks.** Fixed 1–2 hop is the simple version; some synthesis questions will require Tier-2 fetches or PPR-style soft expansion. The coverage gate is the safety valve.

### Neutral

- Citations stay node/claim IDs; the HTTP contract (`question`, `k`, `citations`) is unchanged at the boundary.
- Blocks remain the canonical anchor; markdown remains the trust root; `kg rebuild` remains the heal path (no graph writes added).

---

## Alternatives considered

**(a) Status quo block-dump + hyperparameter tuning (what the current sweep does).** Insufficient: tuning k/neighbours is the only lever that moves systemic recall, and it is *blocked* by context explosion (Stage B2/C OOM-crashed). Model/thinking tuning (Stage B1) bought +5pt to ~0.74 but cannot touch the 67%-of-failures retrieval bottleneck. Raising k or text-neighbours re-bloats and re-hits the 280K wall.

**(b) Pure claims-first (rank and dump claims, no topology).** Too flat. Loses the ego-graph structure that lets a model reason over relationships between entities; aggregation/synthesis (the two collapsed tiers) need topology, not a flat claim list.

**(c) Just raise k / text-neighbours.** Re-bloats context, re-hits the OOM wall (k20/n2 = 280K), and worsens duplicate-window collapse. This is the thing the ADR exists to stop doing.

**(d) Full Microsoft-GraphRAG community summarization.** Heavier and global. GraphRAG's own docs warn indexing is expensive (offline Leiden communities + LLM summaries). Marginalia's questions are mostly entity-specific (GraphRAG *Local Search* territory), and the continuous-curation posture (ADR 0009) makes a heavy offline global pass costly. Local-search-style subgraph-first is the right scope; community summarization is named as a scaling rung if global-sensemaking questions become a priority.

---

## External sources

All thirteen citations below were resolved and verified during finalization (2026-06-07): none are broken or hallucinated, and the quoted figures are verbatim from the cited sources. Three arXiv IDs (TERAG 2509.18667, RLM-on-KG 2604.17056, RAGA 2605.17072) carry dates after the assistant's knowledge cutoff; they were confirmed real and correctly characterized during that pass, and are noted as post-cutoff only for transparency.

- **Microsoft GraphRAG — "From Local to Global"** — https://arxiv.org/abs/2404.16130 ; repo https://github.com/microsoft/graphrag ; query docs https://microsoft.github.io/graphrag/query/overview/ . *Local Search* ("combines the AI-extracted knowledge graph with text chunks of the raw documents") is a near-exact precedent for subgraph-first + text-on-demand. Caveat: the heavy lift is an expensive offline community-summarization pass (offline Leiden communities + LLM summaries) — supports Alternative (d)'s "heavier/global" framing.
- **HippoRAG** — https://arxiv.org/abs/2405.14831 (NeurIPS 2024) ; code https://github.com/OSU-NLP-Group/HippoRAG . Single-step retrieval via Personalized PageRank over an OpenIE triple graph; 10–30× cheaper / 6–13× faster than iterative (IRCoT) on multi-hop (both figures verbatim from the abstract). Validates structure-first; challenges fixed-hop with PPR as the principled soft-expansion.
- **LightRAG** — https://arxiv.org/abs/2410.05779 (EMNLP-Findings 2025) ; code walkthrough https://neo4j.com/blog/developer/under-the-covers-with-lightrag-retrieval/ . Most operational blueprint: explicit **1-hop** entity expansion, degree-centrality ranking, **per-stage token truncation** (`truncate_list_by_token_size` / `max_token_size`), dedup before assembly (`process_combine_contexts`), compact CSV-formatted context, smaller vector top_k in hybrid mode (`mix_topk = min(10, top_k)`), raw chunks fetched **last** from a KV store. Directly supplies the bloat controls Marginalia lacks; the Neo4j walkthrough confirms every operational claim leaned on here.
- **SubgraphRAG (ICLR 2025) "Simple is Effective"** — https://openreview.net/forum?id=JvkuZZ04O7 . Full title: "Simple is Effective: The Roles of Graphs and Large Language Models in Knowledge-Graph-Based Retrieval-Augmented Generation" (Li, Miao, Li). Parallel triple-scoring with a flexibly-sized subgraph budget matched to query + downstream LLM; directional structural distance for relevance-ranked neighbour selection. Smaller local models (Llama3.1-8B) stay competitive — supports the reference-host argument.
- **TERAG (Token-Efficient Graph RAG)** — https://arxiv.org/html/2509.18667v3 (arXiv id post-cutoff; confirmed real, Xiao/Tsang/Bai). PPR seeded on query anchors as an LLM-free relevance ranker ("without requiring additional LLM calls"); "at least 80% of the accuracy … at only 3%–11% of the output tokens." (Note: PPR mathematically *favors* well-connected nodes; the hub-downweighting framing belongs to RLM-on-KG below, not TERAG.)
- **RLM-on-KG: Heuristics First** — https://arxiv.org/html/2604.17056v1 (arXiv id post-cutoff; confirmed real, Volpini & Raad, WordLift). "Degree-bounded expansion: Capping `expand_neighbors` by node degree and filtering by edge relevance prevents exploration drift into densely connected but uninformative entity neighborhoods." The degree-cap mechanism this ADR uses for hub control. (The paper's own headline is "Heuristics First, LLMs When Needed" — when an LLM controller beats heuristic traversal; the degree cap is a secondary optimization there, borrowed here as the hub-explosion cure.)
- **Multi-hop GraphRAG-QA (UFPE 2026 undergrad TCC, biomedical / SPOKE KG)** — https://repositorio.ufpe.br/bitstream/123456789/68413/1/TCC%20Victor%20Gabriel%20de%20Carvalho.pdf . Victor Gabriel de Carvalho, Engenharia da Computação capstone, approved 27 Jan 2026. Source of the anchor-first, truncation-safe `=== NODES === / === RELATIONSHIPS ===` render with `[QUERY_MATCH]` markers and query-matched nodes serialized first. Its empirical k-sweep is biomedical-specific and nuanced: 1-hop was best for fidelity/human-eval and on long-vignette datasets, but 2-hop *improved* accuracy on shorter fact-chaining (MedMCQA: Qwen3 56→60%, Llama3 34→43%); the paper's real conclusion is **make hop-depth adaptive, not blanket** — and unpruned 2-hop can regress via **evidence dilution** (attention fragmentation), not just truncation.
- **Walk&Retrieve** — https://arxiv.org/html/2505.16849v1 (IR-RAG 2025 @ SIGIR 2025). KG-walk traversal + "knowledge verbalization for corpus generation": avoid raw `(s,p,o)` linearization; verbalize edges into readable phrases for better LLM alignment.
- **RAGA — Read-And-Graph-building Agent** — https://arxiv.org/html/2605.17072 (arXiv id post-cutoff; confirmed real, Han & Cheng). "Read–Search–Verify–Construct" in a ReAct loop, with evidence-anchored verification linking every knowledge entry to its source text — the verify step is the drill-to-source provenance discipline (re-ground a claim against its block before answering).
- **Agentic RAG (structure-then-fetch tool pattern)** — https://www.techaheadcorp.com/blog/agentic-rag-when-llms-decide-what-and-how-to-retrieve/ and https://buzzgrewal.medium.com/ai-agents-dont-need-vector-search-anymore-inside-the-agentic-search-stack-replacing-rag-in-2026-58efcabe4f6f . Pattern for `fetch_source` as an on-demand tool call (agent decides what/when/how to retrieve, adaptive depth). Industry write-ups, not controlled benchmarks: the pattern is well-attested; the magnitude of any gain is anecdotal.
- **"GraphRAG uses up to 97% fewer tokens than naive RAG"** (Atlan) — https://atlan.com/know/what-is-a-knowledge-graph/ . The 97% figure is on the page, but Atlan attributes it to CIO.com (which cites a Microsoft MSR Podcast Corpus benchmark), not a Microsoft primary source. The direction matches TERAG; treat the exact 97% as marketing-grade rather than a measured Marginalia-relevant result.
- **RRF over neighbour rankings.** No external source: I am not aware of a paper isolating an RRF-over-KG-neighbours result. RRF is well-established for fusing ranked lists in general; applying it to fuse graph-neighbour rankings is an extrapolation, not a cited finding.

---

## Migration / rollout plan

Phased. Block-dump stays as the Tier-2 path throughout; the default flips only at the end.

1. **Extraction hygiene first.** Clean the inputs the structured answer will surface: collapse `wikilink:`-derived near-duplicate Claim nodes, drop/repair 0-byte Document nodes, prune low-confidence noise. Without this, answering from claims surfaces junk. (Ties to ADR 0010 de-pollution work.)
2. **Subgraph render behind a flag.** Implement ego-graph expansion (re-purposed `query.py:232-250` walk) + degree cap + relevance-budgeted neighbour selection + the `=== NODES/RELATIONSHIPS/CLAIMS ===` render. Gate behind a config flag; block-dump remains default. Validate on the 120-Q eval vs the 0.69 baseline.
3. **Coverage / abstain gate + Tier-2 `fetch_block`.** Add the coverage gate as the Tier-1→Tier-2 trigger and the on-demand block fetch tool (with path re-validation). Measure the absent-trap hallucination drop.
4. **Deprecate block-dump default.** Once the subgraph path beats baseline on the eval and the hypertune sweep can finally run k/neighbours within budget, flip the default. Keep the block path reachable as Tier-2 forever (it is the verification substrate).

Each phase lands with its docs sync (CLAUDE.md binding) and re-runs the eval harness for regression evidence.

---

## Open questions

1. **Fixed-hop vs PPR.** Adopt fixed 1-hop (selective 2-hop) for v1, or jump to Personalized PageRank seeding (HippoRAG/TERAG) for principled soft expansion? PPR directly attacks both multi-hop recall and hub bloat but is more machinery. Recommendation: ship fixed-hop, name PPR as the next rung.
2. **Degree cap + neighbour budget calibration.** What per-node degree cap and per-answer neighbour/claim token budget? Needs measurement on the NX vault's actual hub distribution.
3. **n=2 trigger.** What signals adaptive 2-hop expansion — question tier, unmet coverage, model request? (UFPE: must be adaptive, not blanket.)
4. **Coverage-gate threshold.** What seed-similarity / ego-graph-density threshold trips Tier-2 vs abstain? Must be calibrated against the absent-trap tier so it abstains without over-declining (13 false "not in notes" already exist).
5. **Render format A/B.** Verbalized fluent sentences (Walk&Retrieve) vs ID-referenced typed node/edge lists (UFPE) vs a hybrid. The ADR proposes the hybrid (typed lists + verbalized claims); needs an eval A/B.
6. **MCP surface for `fetch_block`.** Expose the Tier-2 fetch as an MCP tool the agent calls, or keep it internal to the ask loop? Affects the agentic posture (ADR 0009).

---

## Appendix A — Current operational state (for post-wipe continuity)

This appendix exists so the next session can resume without conversational context.

**The eval corpus (the number to beat).**
- **120 ground-truthed questions** authored from the reference evaluation vault source, spanning tiers `factual` / `aggregation` / `synthesis` plus a **10-question absent-trap** tier (questions whose answer is deliberately *not* in the vault, to test abstention). Authored + scored by the `marginalia-knowledge-quality-eval` workflow; an LLM judge scores each answer against ground truth (judge must run thinking-OFF — see the box note below).
- Baseline to beat: **0.69 overall** (2026-06-06). Per-tier: `factual` 0.85, `aggregation` 0.44, `synthesis` 0.44. 40% hallucination on the 10 absent-traps (no abstention floor). 13/120 false "not in notes."

**The live tuning sweep.**
- Workflow: `marginalia-rag-hypertune` (skill). The caller supplies the evaluation vault and model endpoint; machine-local scratch state is not retained in the source tree. The workflow tunes k / context_spans / model / thinking / prompt against the 120-Q eval with **no graph rebuild** (all knobs are live: per-query `query_neighbors` in `marginalia.yaml` + `PATCH` on `llm.ask`). A pinned judge scores each answer vs ground truth.
- A run is resumable through machine-local JSONL records; those records and rendered scorecards are operational evidence, not release-source fixtures.
- The grid was a 9-cell ≤100K-token search over k × neighbours; baseline to beat is **0.69** (120-Q reference evaluation, above).
- **Stage status:** B1 (model/thinking/prompt @ k8/n0) **valid** — best 27b+think 0.742, practical 35b+think-off 0.704. **Stage B2/C (k/neighbours axes) INVALID** — the reference model host OOM-crashed on 143K–220K-token contexts. This ADR is the structural fix that unblocks B2/C: subgraph render makes high-recall configs fit in budget.

**The reference model host.**
- The evaluation accepts a caller-supplied OpenAI-compatible endpoint and records the concrete model id in run evidence; host addresses and credentials never belong in source.
- The historical host idle-unloaded, so warmup was required. Thinking-OFF was mandatory for the pinned judge.

**Durable findings for resume:**
- The no-rebuild tuning harness established context growth of `k × (1 + 2·neighbours) × ~12 KB`, from roughly 20K tokens at k8/n0 to 220–280K at k20/n2, beyond the historical 262K cap.
- The 49-failure review established the 67% systemic / 31% LLM / 2% missing-node split, duplicate-window collapse (~2.5 unique windows per 8 hits), zero-byte Document hits, and default-off block-neighbour expansion.
- The 120-question baseline established 0.69 overall, factual 0.85, aggregation/synthesis 0.44, 40% absent-trap hallucination, and 13 false abstentions.
- Fixed raw-text **12 KB windows** are a validated invariant; do not add heading/semantic coalescing merely to address answer-context bloat.
- Any surface exposing bytes must revalidate the stored provenance path against the vault root before reading it.

**How to resume.**
1. Start an authenticated daemon and the caller-supplied reference model endpoint; warm the model if its runtime idle-unloads.
2. Land rollout Phase 1 (extraction hygiene) — needed before subgraph render is trustworthy.
3. Implement Phase 2 (subgraph render behind a flag) at the touch-points in *Design details*.
4. Re-run `marginalia-rag-hypertune` with the subgraph path enabled; the k/neighbours axes (B2/C) should now fit in budget. Compare vs 0.69.
5. Run `/sync-docs` and stage docs with the code (pre-commit gate).
