# ADR 0002: Document Chunking & Provenance Model

**Status:** Partially superseded / historical
**Date:** 2026-05-31
**Deciders:** Alex Rivera, Marginalia core
**Supersedes:** None
**Superseded by:** ADR 0003 for decisions D3, D4, D5, and D7

**Lifecycle addendum — 2026-07-13.** This ADR did not advance wholesale to
Accepted. ADR 0003 superseded its stored-Block sizing and query-neighbour direction;
the trust-root, drift-detection, cache, and evaluation ideas remain background for the
current implementation. The additive `SourceSpan` work and fixed extraction windows
shipped, but deleting the stored `Block` node has not. This document is not an active
release plan.

---

## Context

Marginalia's ingest cuts documents into `Block`s; each `Claim` (the atomic
provenance unit) anchors to a `Block`. The current markdown chunker
(`ingest/markdown.py::_commonmark_blocks`) emits **one Block per CommonMark token**
(heading / list-item / paragraph / fence). A black-box golden-dataset run over a
17-file corpus produced **1959 Blocks** and visibly degraded answer quality: facts
arrived at the extractor as isolated, context-stripped fragments and were either
dropped at extraction or never ranked into top-k. The provenance byte-hash gate
passed (0/14 failures) and T4 negative controls were perfect (no fabrication), so
**retrieval and grounding are sound — the quality ceiling is ingest fragmentation.**

Two concrete bugs were proven by traces:
1. Over-fragmentation in `_commonmark_blocks` (above).
2. The extractor's `_FENCED` regex only catches fenced ```json; 171 prose-wrapped
   JSON responses (only 4 truncated) were silently discarded.

Before fixing, we ran **8 research streams** (deep-research + codebase Explore +
Codex sanity). This ADR records the resulting architecture so it is not
relitigated. Evidence artifacts:
- `research/04-evaluating-chunking-strategies-2026-05-31.md`
- `research/13-embedder-chunk-size-coupling.md`
- `.documentation/research/document-chunkers-byte-exact-provenance-2026-05-31.md`
- `.documentation/rebuild-vs-incremental-kg-vector-index-cost-2026-05-31.md`

---

## Decision summary

| # | Decision | One-line rationale |
|---|---|---|
| D1 | Provenance goal = **good-enough citation + drift detection**, not tamper-evidence | The strict model was bending the whole architecture for an edge case |
| D2 | **Hybrid provenance**: byte-exact for text, two-layer for binary | Byte-exact is free for text, impossible for binary |
| D3 | **Structure-aware recursive chunking + size-targeted packing**; NOT embedding-semantic chunking | Peer-reviewed evidence: semantic chunking rarely earns its cost |
| D4 | **Document-relative ranged sizing**, max bound **derived from the embedder** | A fixed 3000-char max silently truncates bge-small (512 tok) vectors |
| D5 | **Overlap-free storage + query-time neighbor expansion** (must be built) | Overlap is a retrieval concern, not a storage one — but expansion doesn't exist yet |
| D6 | **Content-addressed caches** keyed on chunk text + config version | LLM extraction dominates rebuild cost 20–100× |
| D7 | **Versioned `deterministic-v2` chunker + `kg rebuild`**, not in-place mutation | Boundary changes change Block identity |
| D8 | **Paired gold-span evaluation** on the golden harness | 14 questions are too few for aggregate scores |

**Ratification status:** D1 and D2 were chosen directly by the user (2026-05-31).
**D3–D8 are research-backed recommendations awaiting explicit sign-off.** Three items
carry unverified assumptions flagged inline below (⚠️) that must be resolved before this
ADR moves `Proposed → Accepted`.

---

## D1 + D2 — Provenance model

**Goal (D1):** provenance answers *"where did this come from?"* (cite) and *"has the
source drifted since extraction?"* (verify). It is **not** marketed as a
tamper-evident guarantee. This frees the design from requiring every stored Block to
be a verbatim contiguous slice of original bytes.

**Hybrid by format (D2):**

- **Text (markdown / plain / code):** keep byte-exact —
  `(path, byte_start, byte_end, content_hash = sha256(raw[byte_start:byte_end]))`.
  Free, because the raw file *is* the text. Strong drift detection + exact citation.
- **Binary (PDF / docx):** **two-layer.**
  - Outer: `Document.sha256` of the original file (already exists) → drift detection.
  - Inner: persist a **canonical extracted-text artifact**. Blocks anchor into *that
    artifact's* byte offsets, with `content_hash` over the artifact slice. Optional
    page/bbox map links back to the original for human citation.
  - Byte-exact-into-the-original-binary is **impossible** (extracted text is not a
    contiguous byte range in the container) — two-layer is the only correct model.
  - ⚠️ **Unverified — extraction determinism.** PDF text extraction is notoriously
    non-deterministic across library versions and even runs (reading order, ligatures,
    whitespace). If the artifact is not byte-stable, the inner hash is meaningless and
    rebuilds churn. Must validate a specific extractor's reproducibility (pinned
    library+version) before this is relied on; otherwise the inner layer degrades to a
    text-similarity check, not a hash.

**Cross-cutting rule:** offsets are a **re-validate-at-read-time locator, never
blindly trusted** — consistent with the existing `security_byte_read_path_revalidation`
guidance. Recall/ask must re-validate `prov.path` vs vault root and re-check the hash
before exposing a slice.

**⚠️ RFC positioning tension (call-out).** `RFC.md` positions Claim-anchored byte
provenance as a *differentiator* vs Basic Memory / Graphiti. D1 deliberately demotes
that from "tamper-evident guarantee" to "good-enough citation + drift detection." This
is a product/positioning decision (chosen by the user 2026-05-31), recorded here so a
future reader does not treat the RFC's framing and this ADR as contradictory. If
rigorous provenance later becomes a marketed claim, this ADR must be revisited — note
that byte-exact is retained for text, so the differentiator is weakened, not abandoned.

**Trust-root note:** anchoring binary Blocks to a derived extracted-text artifact bends
"markdown is the trust root." Accepted: for binary inputs the canonical text artifact
*is* the trust root for the graph; the original binary remains the upstream source,
linked by hash.

---

## D3 + D4 — Chunking strategy & sizing

**Strategy (D3): structure-aware recursive splitting → size-targeted packing.**
Do **not** use embedding-boundary semantic chunking by default. Qu et al.
(arXiv:2410.13070) show its gains over recursive/fixed are inconsistent and rarely
worth the per-document embedding cost. Semantic *scoring* may inform boundary choice
later, but **sizing must stay a pure function of bytes** (determinism — see D7).

**Pipeline:**
```
raw bytes
  → format adapter → candidate units WITH byte spans
      (markdown: CommonMark tokens; text: sentences; code: tree-sitter nodes)
  → split oversized units at safe byte boundaries
  → packer merges adjacent units toward target, within [min, max]
  → stamp Blocks: cut raw[start:end], hash it, assert
```

**Sizing (D4): document-relative range, max derived from the embedder.**
- Absolute max is **derived, not hardcoded**:
  `max_chars = floor(embedder.max_tokens × chars_per_token × 0.9)`,
  `chars_per_token ≈ 3.5` (English) / `3.0` (code). For the default
  `bge-small-en-v1.5` (512 tok) → **~1,600 char hard cap**; target band **~900–1,400**.
  (The earlier ~3,000 proposal silently truncated vectors — rejected.)
  ⚠️ **Unverified — `chars_per_token`.** 3.5/3.0 and the 0.9 margin are heuristics, not
  measured against this corpus. Calibrate against the real tokenizer and add a defensive
  token-exact recheck on produced chunks before trusting the cap on token-dense content.
- Within the embedder ceiling, derive a per-document `(min, target, max)` from the
  document's own unit-size distribution, so a meeting transcript (many short turns)
  packs turns together and a dense spec stays smaller — instead of one fixed budget.
- Detect **transcript-like / flat-wall** docs deterministically from text stats
  (speaker-line ratio, line/unit length percentiles, heading count) and switch the
  presplit unit (speaker turns / sentences / structure) accordingly. This is what
  fixes both failure modes: 1-block-per-line **and** 1-giant-block.

**Code files (D3):** chunk by structure via **tree-sitter** (native `start_byte`/
`end_byte`, verbatim slice) — the one clean byte-exact library win. Language + symbol
name go in **Node facets, not new types** (schema stays closed).

**Library policy:** build-on-top, not adopt-whole. No OSS chunker preserves byte-exact
provenance end-to-end. Use libraries for **boundary detection only**, stamp bytes
ourselves. Approved: tree-sitter (code), `semantic-text-splitter` / `semchunk` (prose
boundaries, char offsets we map to bytes). **Rejected** for offset use: LangChain
`add_start_index` (uses `str.find`, mislocates on repeats), LlamaIndex
`start_char_idx` (maintainer-deprecated as "frequently incorrect"), Unstructured /
Docling (normalize text → break sha256-of-raw-slice).

---

## D5 — Overlap-free storage + query-time neighbor expansion

Store Blocks **contiguous and overlap-free.** RAG-style overlap is a retrieval/prompt
tactic, not a storage tactic, and overlap breaks clean provenance. Recover context at
**read time** by expanding a retrieved Block to its same-document neighbors
(`path` + adjacent `block_index`).

**Missing dependency (verified in code):**
- Graph one-hop expansion **exists** (`query.py:190`, hops entity→claim edges).
- Contiguous **text-block** neighbor expansion **does NOT exist**, and recall
  *filters raw Blocks out* (`query.py:210`); `ask` reads only the single hit's span
  (`companion/__init__.py:752`).
- The data model supports it cheaply (sequential `block_index` per `path`).

**→ Action:** build same-document block-adjacency expansion (e.g.
`expand_block_context(block, store, k_neighbors)`) before relying on overlap-free
storage. Do not treat graph one-hop as a substitute — they are different operations.

---

## D6 + D7 — Caching, identity & rebuild

**Cost reality:** in a rebuild, **LLM extraction dominates by 20–100×** (re-chunk
cheap, re-embed minutes, re-extract days at 100k chunks on a local 35B). So full
re-extraction must be rare; routine changes must reuse cached work.

**D6 — Content-addressed caches + identity fix:**
- Embedding cache key: `text_hash + embed_model_id`.
- Extraction cache key: `text_hash + extraction_prompt_version + extractor_model_id
  + schema_version`. (No OSS tool ships the extraction cache — it's our design;
  `prompt_version` is the part teams forget.) **Note:** the cache key is the chunk's
  *text hash*, independent of `Block.id` — so the caches work regardless of how Block
  identity is computed. This decouples D6's payoff from the identity question below.
- Outer incremental loop: mtime/size → hash-candidates so an edit re-processes only
  the changed file's changed chunks.

⚠️ **Open question (NOT decided) — `Block.id` composition.** Today
`id = sha256(path, content_hash, index)`. Including `index` means a reflow that doesn't
change a block's bytes still changes its id. Dropping `index` was proposed but is **not
safe as stated**: `block_id` is a foreign key across `Claim`, `Finding`, `Annotation`,
`store`, `extract`, `query`, `companion`, and two byte-identical blocks in one document
(e.g. repeated paragraphs) would **collide** to one id without `index`. Because the
caches key on text hash (above), they do **not** need this change. Leave `Block.id` as
is for now; revisit only if a concrete need arises, with a collision-safe scheme
(e.g. `content_hash + occurrence_ordinal`).

**D7 — Versioned rebuild, not mutation:** ship the new chunker as `deterministic-v2`;
changing it is a `kg rebuild` from the vault (the trust root), gated on a config
version. Sizing/identity inputs must be a **pure function of raw bytes + pinned
config** (no embeddings, LLM output, randomness, clock, or path in the *sizing*
decision) so the same file always yields the same Blocks/hashes on rebuild.
**"Pinned config" must explicitly include library/grammar versions** — tree-sitter
grammar output and sentence-segmentation can shift across versions, so the chunker
version string must encode them or rebuilds are not reproducible.

---

## D8 — Evaluation protocol

Prove a chunker change helped with **paired, per-question** comparison on the golden
harness — not aggregate answer scores (confounded by retriever/LLM, and 14 questions
are too few for an aggregate signal).

- Tag each golden question's answer with **gold byte-span(s)** in the source.
- Per question, per chunker, log: **gold-span survives chunking** (intact in one
  Block), **gold-span retrieved@k**, token-level recall/IoU@k, answer verdict.
- Old-vs-new are paired trials; test discordant binary outcomes with **McNemar /
  sign test**, deltas with **paired bootstrap / permutation CI**.
- Grow the set append-only from new corpora and real failures, frozen before
  measurement. Fix the scripted judge first (it must **pin an explicit chat model** —
  this run it auto-grabbed a TTS model → 14× HTTP 400; every LLM call site needs
  explicit `{provider, model, parameters}`, fail-loud if unset).

---

## Consequences

**Positive:** fixes the dominant quality ceiling (fragmentation); generalizes ingest
to text/PDF/docx/code without the binary case dictating the architecture; makes
rebuilds affordable; gives a defensible "did it help?" signal; keeps the schema closed.

**Negative / accepted:** binary-doc provenance anchors to a derived artifact (bends
"markdown is the trust root"); a new chunker forces a one-time `kg rebuild`; neighbor
expansion, the two caches, and per-format adapters are net-new code; the strong
byte-exact guarantee is intentionally *not* a marketed differentiator.

**Out of scope (this ADR):** embedding-semantic chunking, late chunking, RAPTOR,
propositions, contextual retrieval — promising but preliminary (arXiv/industry only);
revisit per the evidence notes if a future need justifies the cost.

---

## Implementation order (proposed, not part of the decision)

1. Extractor prose-JSON recovery (`raw_decode` scan, fenced-first) — cheap, isolated.
2. Pin explicit LLM config at every call site (incl. judge) — unblocks valid grading.
3. Gold-span eval harness additions (D8) — so every later step is measurable.
4. `deterministic-v2` chunker: format adapter + ranged packer + byte stamping (D3/D4/D7).
5. Block-neighbor expansion in query/ask (D5).
6. Content-addressed embedding + extraction caches keyed on *text hash* (D6); leave
   `Block.id` composition unchanged (the `index` drop is rejected as collision-unsafe — see D6).
7. Binary adapters (PDF/docx) + canonical extracted-text artifact (D2).
