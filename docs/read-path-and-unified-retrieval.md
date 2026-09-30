# Read Path & Unified Retrieval — design notes

**Date:** 2026-05-28
**Status:** Implemented; historical design record · **Reconciled:** 2026-07-13
**Companions:** an internal research note on unified retrieval (cited sources, not
published), `docs/autonomous-architecture-plan.md` (the write-path build).

This doc captures the read-path problem, Alex's conceptual sketches, the take
on the open design questions, and the research-backed design for a single
unified retrieval entry point ("one search").

**Lifecycle addendum — 2026-07-13.** Unified fused retrieval, heterogeneous
entity/Claim/chunk search, byte-grounded extracted Claims, graph walk, typed subgraph rendering,
and bounded source blending ship. Block answering remains the default and subgraph/efficient
hybrid answering remains opt-in. The "build not started" baseline and §4's first two follow-ups
were superseded by the 2026-05-29 shipped addendum; remaining abstraction and scale questions are
future product research, not release tasks.

---

## 1. The problem (found by the live E2E)

The write path is built and verified: `remember(doc)` runs propose → stage →
resolve (talk-back) → gate → commit/queue on the live oMLX 35B, committing real
entities with provenance. But the **read path is fragmented**:

- `recall` / `ask` use `query_claims`, which searches **`Claim` nodes only**
  (`store.list_nodes(type="Claim")`). Committed entity nodes (Agent, Concept,
  Place, InformationObject, Activity) are **not surfaced** by the same path — so
  an `ask` over the graph misses the entities it just ingested.
- Graph traversal / Cypher is a **separate** path, not joined to semantic search.
- LLM-extracted nodes carry **document-level provenance** (no byte range), so
  `ask`'s content-grounding (`_hit_text`) falls back to titles for them, starving
  synthesis. (Deterministic Blocks have byte ranges and ground correctly.)

Net: there is no single point of search that returns the right grounded context
across entities + claims + chunks.

---

## 2. Alex's sketches (the vision) and the take

Two hand sketches, analyzed:

**Sketch 2 — the loop.** "Every LLM interaction curated → persisted → 'absorbed'
into general knowledge (correlated, analyzed, typified)." Document + interaction
+ world feed a brain-graph; a curation agent (the robot) does the absorbing.
→ This **is** the `remember()` loop we built. Confirmed, not drifting.

**Sketch 1 — the pipeline.** DOC → semantic chunking + entity extraction →
candidates → "resgate dos nós associados relevantes" → commit new node with
correlations into the graph DB.
→ This is propose → stage → **resolve(talk-back)** → commit. The "resgate dos nós
associados" is `find_similar` / `find_contradictions`.

### Take on the open questions Alex wrote

- **Chunking vs entity-extraction boundary** — not one boundary; two coexisting
  layers. Chunking = structural (`Block`, byte-anchored) = the *grounding*.
  Entity extraction = semantic (LLM propose) = the *distilled knowledge*. Keep
  both.
- **"I remember what I learned but don't memorize the book — I need to know which
  book to return to"** — the load-bearing insight, and the whole reason for
  PROV-O. Memory = Claims/entities; pointer back = `Claim → Block → (path,
  byte_start, byte_end)`. Store the fact + an anchor to re-read on demand. This
  is also exactly the `ask` grounding gap above.
- **Graphs vs embeddings — role of each** — embeddings = fuzzy associative recall
  (find entry points by meaning); graph = precise traversal + provenance (how X
  ties to Y, and where's the source). Use both in sequence: vector to land,
  graph to expand + ground. (The GraphRAG thesis — see §3.)
- **"Are graphs the best answer for how we learn?"** — graphs nail structured,
  queryable, provenanced memory. Human learning also does **abstraction** (macro
  lesson from micro instances — Alex's "lição local vs geral"). The graph holds
  facts; forming `Concept`s from repeated patterns is the harder, least-built
  part — the real frontier, lives in the curation agent + SKOS broader/narrower.
- **"Don't reinvent the wheel — librarianship / information science"** — already
  done: the RFC's stack is BIBFRAME → SKOS → PROV-O → Dublin Core → CiTO (37-source
  survey). SKOS is the answer for the macro/micro Concept hierarchy.

### The hard bet (push-back)
"Every LLM interaction curated and registered" is a **firehose**. "Absorbed /
correlated / typified" only works if write-time curation has high-precision dedup
+ abstraction — else the graph rots into noise. The review queue is the
semi-autonomous safety valve, but you can't review every interaction. The real
product bet: **can write-time curation be mostly-autonomous AND precise?** That's
the unsolved hard problem — not the plumbing.

---

## 3. Research-backed design: the single point of search

Full sources + caveats are in the internal research note on unified retrieval
(note: that deep-research run's verifier malfunctioned; findings salvaged
manually; some `2603.*` arXiv IDs unverified — named systems are real).

**Consensus pattern:** one hybrid retriever fuses vector + lexical + graph
traversal behind a single call, returns heterogeneous node types on the same
path, and fuses by **rank** (not score concatenation).

Reference systems that already do this:
- **Neo4j GraphRAG `VectorCypherRetriever`** — vector search → seed nodes → Cypher
  traversal, in one call. Our exact shape (Ladybug + Cypher + node vectors).
- **Zep / Graphiti** (closest analog) — cosine vector + BM25 + graph BFS in one
  search; **RRF** + MMR + graph reranking; returns entities + edges + communities
  together; source anchoring via "episodes" (= our Block provenance).
- **HippoRAG** — Personalized PageRank over a graph indexing entities + passages
  as one mechanism.
- **A-MEM** — memories as structured notes (desc/keywords/tags) + write-time
  auto-linking.

Key techniques:
1. **Per-type embeddings** are the mechanism that lets one path match across
   entities, claims, chunks — directly fixes "recall searches only Claims."
2. **Normalize before fusing** — vector cosine vs graph/PPR scores are
   distributionally incomparable; use **RRF** (standard, rank-based) or
   percentile/CDF normalization. Don't fuse raw scores.
3. **Don't concatenate contexts** — naive hybrid concat had the *lowest* context
   relevance in a 2025 GraphRAG study (dilution) despite highest correctness;
   fuse by ranking + cap.
4. **Route by query shape** — vector for easy/semantic; graph for multi-hop /
   relational / not-explicitly-stated.
5. **Text2Cypher needs a correction loop** — not one-shot.

### Recommended Okto Neuron `search()`
A single entry point that:
1. embeds the query; runs **per-type vector search** across entity nodes +
   Claims + Blocks (fixes recall-only-Claims);
2. takes top seeds, does a **1–2 hop Cypher expansion** for connected context;
3. **fuses by RRF** across vector hits + graph-expansion hits (not concat);
4. returns typed results each carrying **provenance** (Block byte-range) so `ask`
   grounds on real source text;
5. optionally routes: short factual → vector-only; relational → expand.

This is `VectorCypherRetriever` + Graphiti fusion on the 5-primitive spine.

---

## 4. Follow-ups / backlog

- **Build unified `search()`** per §3 (the read-path fix). Replaces/augments
  `query_claims`. Highest value next step.
- **Fix LLM-node grounding**: give extracted nodes a usable grounding anchor
  (carry the source Block byte-range onto LLM candidates, or ground via the
  source Document) so `ask` synthesises from real text, not titles.
- **Abstraction layer**: form `Concept`s (SKOS broader/narrower) from repeated
  micro-instances — the "how we learn" frontier; the least-built part.
- **Curation precision at firehose scale** — the hard product bet (§2).
- Deferred (named earlier): bi-temporal claims (valid-time vs transaction-time);
  per-Block extraction (currently whole-document, ~60s/doc on 35B, token-heavy);
  gate tuning; `OutcomeAction` has no `discarded` value.

## 5. Known tooling issue
The `deep-research` workflow's adversarial-verification phase failed: verifier
sub-agents completed without calling `StructuredOutput` (2 nudges), so all 25
claims defaulted to a spurious `0-0` "refuted" and the run reported
"inconclusive" despite high-quality findings. The schema-output contract for
those verifier agents needs hardening before relying on the verdict.

---

## Addendum — 2026-05-29: shipped (both gaps closed)

The two limitations this doc framed as current are now **solved and verified
end-to-end** (real 35B oMLX + fastembed). Status above ("design captured; build
not started") is superseded; the design landed.

- **§1 fragmented read path → unified retrieval shipped.** `recall`/`ask` no
  longer search `Claim` nodes only. A single fused `search()` ranks entities +
  claims + chunks together via a unified `VectorCypherRetriever` with RRF
  (vector ∪ graph). One entry point; `recall` and `ask` share it.
- **§1/§2.5 document-level provenance → byte-anchored Claims shipped.**
  `remember()` now mints **one byte-anchored Claim per extracted S-P-O
  relationship**, anchored to its source `Block` (`(path, byte_start, byte_end,
  content_hash)`) and carrying `confidence + model_id + prompt_hash`. LLM facts
  are Claims, not bare edges — so `ask`'s content-grounding reads real source
  bytes, not titles.
- **§4 per-Block extraction** is in: extraction now runs once per anchored Block
  so every candidate/edge carries its Block's byte range.

Verification: acceptance scenario `56_remember_anchored_claims` (no mocks) —
`recall("who is the partner on Okto Neuron?")` surfaces **Jordan Lee Carter** as
both an Agent and a byte-anchored `Claim`, with `path + byte_start/byte_end`
provenance into the note; multiple Claims are minted and embedded. The release
gate explicitly sets `llm.extraction.samples=2` and unions two independent real-model
draws under ADR 0030, because one non-zero-temperature draw is intentionally lossy
and produced a red/green 1-of-2 result during final validation. This makes the gate
test the shipped recovery boundary instead of treating sampling luck as release evidence.

## Addendum — 2026-09-17: the recall floor meets a backend list cap

`query._vector_seeds` is a deliberate full scan — "there is no ANN index, so
this is a deliberate full scan: any node with a non-trivial cosine to the query
is reachable even with zero token overlap. The recall floor is the untruncated
`IndexStore.scan_vectors`." It therefore hands EVERY embedded node id to
`GraphStore.get_nodes` in a single call.

Grafx rejects a query list holding more than `MAX_LIST_ELEMENTS` (1024)
elements. `GrafxStore.get_nodes` built one `MATCH (n:Node) WHERE n.id IN $ids`
query from the whole list, so `ask` failed outright on any Grafx vault holding
more than 1024 embedded nodes:

```
okto_grafx.domain.errors.GrafxConfigurationError:
    A query list may hold at most 1024 elements. [code=configuration_error]
```

Found on a real 1,477-embedded-node vault running the published 0.0.50 wheel.
Every release gate passed because the acceptance vaults all stay under the cap.

The fix batches inside `GrafxStore.get_nodes` (`_ID_QUERY_BATCH = 1000`) rather
than truncating the seed set. That ordering matters: capping `_vector_seeds`
would silently trade recall — the thing this document exists to protect — for a
storage-layer limit. The store's contract is unchanged; callers still get their
ids back deduplicated and in request order, only the number of round trips
differs.

Two related gaps remain open:

- `GrafxConfigurationError` subclasses `GrafxError -> Exception`, never
  `MarginaliaError`, so it bypasses `api_ask`'s `except MarginaliaError ->
  "ask_failed"` branch and surfaces as a bare `internal server error` with the
  real cause visible only in the daemon's stderr. Backend errors should be
  mapped at the store boundary.
- `get_nodes` was the only `IN $list` site in `store/grafx.py`, but the other
  backends (`ladybug`, `neo4j`) have not been swept for the same shape.

## Addendum — 2026-09-30: set-based store reads (#11)

Several library loops read the graph one node at a time: a `get_node` per edge
endpoint, per Claim argument or per candidate id. On Grafx every `get_node` is
its own read transaction, so the cost grows with the graph, not with the
answer. The predicate vocabulary scan was the worst case: it resolved both
endpoints of every edge and the arguments of every Claim just to return a
`Counter` of predicate names.

The rule now is to read sets, not ids: collect the ids first, then make one
`GraphStore.get_nodes` call (Grafx splits it into `_ID_QUERY_BATCH` chunks) and
look nodes up in a dict. A missing id is simply absent, which keeps the old
`get_node(...) is None` handling. Where a scan was repeated per item, it is
read once before the loop.

- `predicates.collect_predicate_vocabulary` makes two reads, one `list_edges()`
  and one `list_nodes("Claim")`, with no node lookups. `shared_argument_evidence`
  makes the same two. `generate_predicate_candidates` adds a single `get_nodes`
  over every endpoint, Claim argument and sample Block id. The outputs match the
  old per-node scan byte for byte, including dict and `Counter` insertion order.
  `tests/predicates/test_candidates_batch_reads.py` checks this against a copy
  of the old scan on the in-memory, Grafx and Ladybug backends.
- The sub-chunk detach and revert passes in `companion/_incremental.py` read
  their blocks in one call. Each block's derived Claims and their object nodes
  also come back in one call per block.
- `subgraph` reads bridging and sibling Claims in one call. The bridge scan cap
  still bounds how many ids get read.
- `migrate.bridge_edges.ensure_source_mentions` reads the committed nodes and
  their Documents in two calls.
- `reconcile.propose.adjudicate_cluster` reads cluster members in one call.
- `resolve.find_contradictions` and `resolve.resolve` take an optional
  `claims=` snapshot, and the two resolve loops in `Companion.remember` read
  the Claim list once per loop, not once per Claim candidate.

On a synthetic Grafx store (4,000 nodes, 13,000 edges, 2,500 Claims, local
laptop), `collect_predicate_vocabulary` went from a 53.6 s median to 0.39 s,
and the full candidate scan from 50.0 s to 0.70 s, with identical outputs.
These are synthetic numbers, not a measurement of any real vault.
