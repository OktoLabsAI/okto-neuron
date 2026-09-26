# Autonomous Companion — Architecture Adjustment Plan

**Status:** ACCEPTED AND IMPLEMENTED (core program) · **Original date:** 2026-05-23
· **Reconciled:** 2026-07-13
**Owner:** Alex Rivera
The original local walkthrough remains an ignored scratchpad artifact and is not
part of the tracked release documentation.

## Implementation closeout — 2026-07-13

The core adjustment this plan proposed is shipped. `Companion.remember()` now runs the
extract → durable candidate ledger → resolve/judge/curate → commit-plan → graph-write
pipeline; `recall`, `ask`, and review/curation surfaces are exposed through the appropriate
SDK, HTTP, CLI, and Web UI contracts; the current MCP contract is the smaller
`ask`/`explore`/`remember`/`list_vaults`/`init_vault` surface; and continuous folder monitoring provides
the ambient path. The
implementation evolved beyond the original sketch:

- LiteLLM and local CLI providers replaced the proposed single OpenAI-compatible client;
- the durable candidate ledger and commit-plan boundary are implemented incrementally
  under ADR 0013, with review projections still a future product-hardening concern;
- `VaultPool` supplies per-connection MCP vault routing, while cross-vault query fan-out
  remains a separate future feature;
- graph-walk/subgraph retrieval shipped behind configuration and the bounded efficient-
  hybrid path is accepted in ADR 0028; block mode remains the default;
- the Web UI, REST API, curation control plane, and daemon lifecycle all shipped after this
  original plan; ADR 0034 subsequently replaced the browser bootstrap/cookie flow with a direct
  loopback application and replaced process-global selection with immutable per-vault runtimes; and
- the historical `GLiNERNER` and flat `marginalia.mcp_server` entries below never became the
  production paths. The corrective post-`0.0.40` source candidate retires the GLiNER dependency,
  manifest, and executable model path behind a deprecated fail-closed import tombstone, and makes
  both legacy MCP constructors fail closed; only `marginalia serve` owns the authenticated
  four-tool MCP surface.

Sections 2, 4, and 7 below are preserved as the 2026-05-23 baseline and implementation
proposal. Their `Current`, `NEW`, and sequencing labels are historical, not active tasks.
Future product opportunities named in sections 5 and 8 are roadmap backlog and are not
`0.0.40` release gates.

## 1. Goal

Turn Marginalia from a manually-driven ingest+query library into an **autonomous
memory companion**: you hand it a document, it extracts knowledge, resolves it
against what it already knows, surfaces correlations, and commits — on its own,
in the background, interrupting you only when it is genuinely unsure.

This **ports Pulse's KG consolidation pattern** (begin → add candidates →
find-similar / find-contradictions → reconcile → commit, plus a dead-letter
queue) onto Marginalia's **generic 5-primitive spine** (`Agent`, `Activity`,
`InformationObject`, `Concept`, `Place` + `Claim`) instead of Pulse's SDLC
types. We reuse the *shape* of a proven system and change the type vocabulary.

Two responsibilities, kept separate:

- **Propose** (extraction) — pluggable, runs an LLM **inside** Marginalia.
- **Resolve + curate** — deterministic dedup/link/embed/anomaly signals stay core-owned,
  then LLM judge/curator calls evaluate ambiguous identity and candidate quality before
  the commit plan.

Extraction never writes directly; everything goes through a **staging
transaction** with a **confidence gate** (auto-commit high confidence, queue the
rest for review). The markdown vault stays the trust root, so every autonomous
write is reversible.

**Update 2026-06-08 — ADR 0013 refinement.** The shipped loop has the correct
shape, but most candidate decisions are still transient Python state. ADR 0013
makes the next boundary explicit: extracted entities, relationships, and claims
become durable pre-graph candidate records; resolver/judge work writes comparison
records; candidate-curator and relationship-curator work evaluates every proposed
node / relationship artifact, including audit-only verdicts for candidates later
removed by deterministic remap/reconcile paths; the gate emits a commit plan; and the
graph write applies that plan. Cross-reference-only sibling targets are queued as
navigation metadata unless the excerpt substantively defines them. This is a refinement
of the staging transaction, not a new primitive and not a requirement to split extraction
into multiple LLM proposal calls yet.

**Update 2026-06-09 — generic core, pack-specific guidance.** The extraction/curation
workflow must stay broad by default. The current implementation keeps the base extractor on
the five primitives plus literal Claims, with SDLC examples and local taxonomy normalization
enabled only through the `sdlc` pack. Quality tuning is validated against at least two
different explicit vaults: the reference corpus proves the SDLC pack behavior,
while the LOTR corpus proves the generic path does not inherit software-delivery concepts.
`scripts/ingest_quality_check.py` is the repeatable live guardrail for these checks.

**Update 2026-06-09 — relationship liveness as a write invariant.** The commit gate now
feeds relationship-curation results back into node write eligibility. After topology edges
and literal claims pass through the relationship curator, a novel node candidate must be
supported by at least one accepted relationship or accepted literal claim; otherwise it is
queued with a `relationship_liveness_gate` comparison instead of being committed as an
isolated knowledge node. This is still generic process tuning, not corpus-specific tuning:
the same gate passed both the SDLC reference vault and the literary LOTR
vault with zero isolated non-provenance knowledge nodes. Fresh 2026-06-09 reports:
LOTR `79` nodes / `274` edges / `50` Claims, CoP `491` nodes / `1673` edges /
`357` Claims.

## 2. Historical baseline (2026-05-23)

| Concern | Where | What it does today |
|---|---|---|
| Public API | `vault.py` (`Vault`) | `open/init`, `add(source)→Document`, `query()`, `get()`, `export()`, `get_provenance()` |
| Ingest | `ingest/markdown.py` | Deterministic only: frontmatter, `#tags`, `[[wikilinks]]`, `#headings` → `Block` + tag/heading `Claim` |
| Extraction contract | `models/ner.py` | `EntityExtractor` protocol + `GLiNERNER` (zero-shot NER). **Not wired into ingest.** |
| Embedding | `embed/__init__.py`, `models/embed.py` | `EmbeddingProvider` protocol; fastembed `bge-small-en-v1.5`, 384-dim |
| Retrieval | `query.py` | `query_claims()` — keyword + cosine over node embeddings (flat, no graph walk) |
| Anomaly | `detectors.py` | `run_detector()` → `Finding`s (supersedence stale head, authority alias collision, commitment temporal) |
| Storage | `store/protocol.py`, `ladybug.py`, `memory.py` | `GraphStore`: `add_node/add_edge/get_node/list_nodes/list_edges/search_text` |
| Types | `packs/*.py` | `core/research/personal/sdlc` packs declare node/edge type names |
| MCP | `mcp_server.py` | 3 tools: `kg_add`, `kg_query_natural`, `kg_get_provenance` |
| HTTP | `server/` | minimal |
| Config | `config/`, `marginalia.yaml` | packs list, embedding provider/model, storage backend |

**Historical gap in one line:** there was no LLM-backed extraction, no candidate staging, no
resolution/curation loop, no autonomy, and no LLM provider abstraction. `add()`
goes straight from file to deterministic claims.

## 3. Target architecture

```
Consumers:   Claude Code (MCP)   CLI (kg)   other agents (HTTP)   [Claude hook — optional]
Surface:     remember(doc) · recall(query) · ask(question) · review_queue()    (MCP + HTTP)
                                   │
                       ┌───────────┴───────────┐
                       │   autonomous loop      │   (one remember() call runs all of this)
                       │  intake → propose →    │
                       │  stage → resolve →     │
                       │  talk-back → gate →    │
                       │  commit / review-queue │
                       └───────────┬───────────┘
Brain:        LiteLLM-backed provider (per-step config): local models | big providers
Spine:        5 primitives + Claim   ·   packs = optional named views
Trust root:   markdown vault (graph is rebuildable)
```

## 4. Historical implementation plan — what was adjusted, by component

Each item: **Current → Target**, the concrete change, files to touch, new
dependencies, and rough effort (S/M/L).

### A. LLM provider layer (OpenAI-compatible) — NEW · M

- **Current:** no LLM client anywhere.
- **Target:** one `LLMProvider` protocol with an OpenAI-compatible
  implementation. Config-driven `base_url` + `api_key` + `model` so the same
  loop runs against local servers (LM Studio / Ollama / vLLM) or hosted
  providers (OpenAI / Anthropic-compatible gateways).
- **Changes:** new `src/marginalia/llm/__init__.py` with `LLMProvider`
  (Protocol) + `LiteLLMProvider`. Add a `[litellm]` extra (the `litellm` client);
  the model string carries the provider prefix (e.g. `openai/<model>`). Mirror the
  existing `EmbeddingProvider` pattern in `embed/`. *(Shipped this way:
  `LiteLLMProvider` replaced an earlier `OpenAICompatProvider`, with `StubLLM`
  kept for CI.)*
- **Config:** add an `llm:` block to `marginalia.yaml`. *(Shipped as per-step
  config: an `llm.defaults` baseline plus independent `llm.extraction` /
  `llm.judge` / `llm.curator` / `llm.relation_curator` / `llm.ask` override
  blocks, each inheriting any unset field from defaults — so extraction can run a
  big local model while judge/curator calls run something cheaper.)* Default to a
  local `api_base`.
- **Policy hook:** a per-call `sensitivity` flag → sensitive content is pinned
  to a local provider, never sent to a hosted one (load-bearing for the ExampleCorp
  vault, which holds data marked "must NOT be recorded or shared externally").

### B. Extraction → candidate producer — ADAPT · M

- **Current:** `GLiNERNER` exists but unused; ingest is deterministic.
- **Target:** an `Extractor` that takes anchored `Block`s and emits **candidate**
  nodes/edges/claims on the 5-primitive spine. Two implementations:
  deterministic (today's logic) and **LLM-based generic** (S-P-O Claims +
  Agent/Concept/Activity candidates), composable.
- **Changes:** generalize `models/ner.py` into an `extract/` package producing
  `Candidate*` objects (not graph writes). Keep `Block` anchoring intact
  (provenance is non-negotiable). The extracting `Agent` + `Activity` are
  recorded on every candidate (the data already shows this pattern:
  "Agent: extraction system" / "Activity: deterministic-v1").
- **Note:** generic LLM extraction is the genuinely new capability; GLiNER can
  remain as a cheap first pass.

### C. Staging transaction — NEW · L (port from Pulse)

- **Current:** none. `add_node`/`add_edge` write directly.
- **Target:** a consolidation **session**: `begin → add_node_candidate /
  add_edge_candidate → (resolve) → commit / abort`, with a **dead-letter queue**
  for candidates that fail validation. This is the structural enabler of
  write-time curation.
- **Changes:** new `src/marginalia/consolidate/` (session, candidate store,
  dead-letter). Extend `GraphStore` (`store/protocol.py`) with a staging area or
  keep candidates in a side table the `MultiVault`/`LadybugStore` understands.
  Model the candidate as a first-class object (not a primitive — no schema
  pressure; see §6).
- **Reference:** mirror Pulse's `kg_begin_consolidation`,
  `kg_add_node_candidate`, `kg_add_edge_candidate`, `kg_commit_consolidation`,
  `kg_dead_letter_*`.

### D. Resolve services — MIXED · M

- **embed** — exists. Reuse to vectorize each candidate before matching. **S.**
- **authority-resolve** — **build.** Today only a detector for alias collisions
  (`detectors.py:_detect_authority_alias_collision`). Target: a real service
  that, given a candidate, finds the canonical existing node (dedup/merge) via
  embedding + identifier match. New `src/marginalia/resolve/authority.py`. **M.**
- **anomaly / contradiction** — exists (`detectors.py` → `Finding`s). Wire it to
  run *against a candidate* at resolve time (not just whole-vault sweeps).
  Refactor `run_detector()` to accept a candidate scope. **S–M.**
- **correlation talk-back** — assemble `{similar, contradicts, answers}` for a
  candidate from embed + authority + anomaly + a neighborhood lookup. New
  `resolve/correlate.py`. **M.**

### E. Confidence gate + review queue — NEW · M

- **Current:** none.
- **Target:** after resolve, score each candidate; **auto-commit** above a
  threshold, **route to a review queue** below it or on contradiction. The queue
  is the only thing that interrupts the user.
- **Changes:** `consolidate/gate.py` (thresholds from config), a `review_queue`
  persisted alongside the staging area, and a `review_queue()` surface function
  (§F). Thresholds and the auto-commit policy live in `marginalia.yaml`.

### F. Public surface — EXTEND · M

- **Current:** `Vault.add/query/get_provenance`; MCP `kg_add/kg_query_natural/
  kg_get_provenance`.
- **Target functions** (the contract; hooks are *not* required to use them):
  - `remember(doc, *, sensitivity=...)` — runs the full autonomous loop; returns
    a summary (committed N, queued M, with the talk-back).
  - `recall(query, k=...)` — retrieval (wraps `query.py`, later graph-walk).
  - `ask(question)` — recall + LLM answer over the retrieved subgraph.
  - `review_queue()` / `resolve_review(id, action)` — list and act on parked
    candidates.
- **Changes:** add methods to `Vault` (`vault.py`); expose each as an MCP tool
  (`mcp_server.py`) and an HTTP endpoint (`server/`). Keep `add()`/`query()` as
  thin back-compat aliases.

### G. Ambient runner — NEW · M

- **Current:** nothing runs unless called.
- **Target:** an optional background worker that watches for new/changed vault
  files (or an inbox) and calls `remember()` automatically — the "companion
  working while you work." Confidence gate keeps it safe.
- **Changes:** `src/marginalia/runner/` (a watch loop; reuse the existing
  `.marginalia/incoming` inbox seen in the vault layout). Runs as part of
  `marginalia serve` or as a standalone `kg watch`.

### H. Config additions — S

- `llm:` block (provider, base_url, model, api_key_env, sensitivity default).
- `consolidation:` block (auto_commit_threshold, review on contradiction).
- `runner:` block (watch on/off, inbox path).

## 5. Future product backlog (originally deferred to v2)

- **Bi-temporal claims** — source-recency `asserted_at` and reconciliation
  `valid_as_of` / `valid_until` facets now ship. A complete first-class split between
  *valid-time* (when a fact held) and *transaction-time* (when it was learned), including
  query semantics, remains additive future product work and does not block the release.
- **Graph-walk retrieval — shipped.** Ego-graph expansion and subgraph rendering now
  exist behind `llm.ask.enable_subgraph`; ADR 0028 adds the bounded always-blend source
  path. Because the block path still wins as the conservative default, this is an
  implemented opt-in capability rather than unfinished core architecture.

## 6. Schema impact

None of this introduces a sixth primitive. Candidates, sessions, dead-letter,
and the review queue are **process/metadata** concepts, not graph primitives —
they stage *instances of the existing 5 primitives + support types*. Per the
repo's hard rule, no ADR is required unless the work later pressures the closed
set. Flag early if it does.

## 7. Historical sequencing (completed)

1. **LLM provider (A)** + config (H-llm) — unblocks everything; verify local
   `base_url` round-trip first.
2. **Candidate model + staging transaction (C)** — the structural spine.
3. **Extractor → candidates (B)** — start deterministic, add LLM generic.
4. **Resolve (D)** + **gate/review (E)** — the talk-back and the brake.
5. **Surface (F)** — `remember/recall/ask/review_queue` on Vault + MCP + HTTP.
6. **Ambient runner (G)** — autonomy on top.
7. Hooks (optional, Claude-Code-only) — last.

MVP = steps 1–5 callable as `remember(doc)` → talk-back → gated commit, on a
local model. Steps 6–7 make it ambient.

## 8. Future product questions (not release gates)

- Local model floor: which local model is good enough for generic S-P-O
  extraction, and where do we escalate to a provider (and how to mark
  non-sensitive content as escalation-eligible)?
- Review queue UX: polling and the review-first Curation UI shipped; push notification
  remains an optional future enhancement.
- Dedup aggressiveness: how confident before auto-merging two entities vs.
  linking them as related?
- Ambient behavior is now content-hash incremental and folder-watch driven; future policy
  work may tune how aggressively changed files are re-resolved.
