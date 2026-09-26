# Chunking + Provenance Rework — Implementation Plan

**Status:** Historical plan; partially shipped and partially superseded · **Date:** 2026-05-31 · **Reconciled:** 2026-07-13
**Companion to:** `docs/adr/0002-chunking-and-provenance.md` (the decisions)
**Authored by:** Codex (code-grounded plan) + Claude (evidence-weaving), from the
golden-run autopsy and 8 research streams.

**Lifecycle addendum — 2026-07-13.** This is no longer a standing implementation plan.
The prose-JSON parser, fail-loud configuration, explicit call parameters, evaluation work,
query-time neighbor/source context, real rebuild, incremental re-ingest, fixed extraction
windows, and byte-anchored Claim spans landed through later work. ADR 0003 superseded the
proposed structure-aware `deterministic-v2` Block redesign with additive `SourceSpan` and
fixed runtime extraction windows; stored `Block` nodes remain. Native PDF/docx adapters and
the destructive Block-removal schema cut did not ship and are future product ideas, not
`0.0.40` release gates. The phase and DoD text below is preserved as design/evidence history.

Every work item carries an **Evidence:** line. Sources:
- **R04** = `research/04-evaluating-chunking-strategies-2026-05-31.md`
- **R13** = `research/13-embedder-chunk-size-coupling.md`
- **RBE** = `.documentation/research/document-chunkers-byte-exact-provenance-2026-05-31.md`
- **RRC** = `.documentation/rebuild-vs-incremental-kg-vector-index-cost-2026-05-31.md`
- **ADR** = `docs/adr/0002-chunking-and-provenance.md`
- **CODE** = verified against the repo at file:line (engineering fact, not research)

Evidence-strength labels follow the research notes: **STRONG** (peer-reviewed /
authoritative mechanism), **PRELIMINARY** (arXiv / industry / vendor), **JUDGMENT**
(engineering reasoning, no external citation), **CODE** (verified in this checkout).

---

## Verified baseline (CODE)

- Over-fragmentation: `_commonmark_blocks` emits one Block per CommonMark token
  (heading/list-item/paragraph/fence) — `src/marginalia/ingest/markdown.py:117–203`.
  Golden run = 1959 Blocks / 17 files, 4 of 14 questions missed/wrong
  (`tests/golden/results/reference-corpus/20260531-164351/readout.md`).
- Extractor is **no longer fenced-only**: `_find_json` already scans `_FENCED` +
  `_balanced_objects` (`src/marginalia/extract/__init__.py:159–192`). It does NOT use
  `JSONDecoder.raw_decode`, and tests only cover fenced wrapping
  (`tests/extract/test_llm_extractor.py:51–54`). *Issue 1 framing was partly stale.*
- LLM config can silently fall back: `LLMConfig` is `extra="forbid"`
  (`config/_vault.py:96`) but `Companion._vault_config()` catches `ConfigParseError`
  and returns defaults (`companion/__init__.py:180–183`).
- Retrieval: graph one-hop expansion EXISTS (`query.py:190–208`); contiguous
  text-block neighbor expansion does NOT, and recall filters raw Blocks out
  (`query.py:210–222`); `ask` reads one hit span (`companion/__init__.py:752–765`).
- `kg rebuild` exists but defaults to `_noop_ingest` (`cli/kg.py:68–85, 247–249`).

---

## Phase ordering & dependencies

```
Phase 1 (extractor + LLM config + eval harness)  ← unblocks measurement
   └─> Phase 2 (block-neighbor expansion)         ← independent, measurable
   └─> Phase 3 (caches + real kg rebuild)         ← BEFORE chunker (cost leverage)
          └─> Phase 4 (deterministic-v2 chunker + embedder cap)  ← the core fix
                 └─> Phase 5 (PDF/docx two-layer)  ← gated on determinism spike
```

**Why caches before the chunker (Phase 3 before 4):** rebuild cost is dominated by
LLM extraction, not chunking or embedding.
**Evidence:** RRC — extraction is **20–100× embedding cost**; research-track
measurement ~7 s/sentence (arXiv:2507.03226). Without an extraction cache, a chunker
swap re-extracts everything. *(PRELIMINARY — arXiv preprint + industry blogs.)*
This reorders the ADR's proposed sequence (ADR step 6 came late). **Disagreement with
ADR logged below.**

---

## Phase 1 — Measurement & LLM determinism

**Goal:** make extraction + judging trustworthy *before* changing retrieval/chunking,
so every later phase is measurable. **Issues:** 1, 2, 9-foundation.

1. **Extractor prose fallback** (`extract/__init__.py:159–192`): keep fenced-first,
   replace `_balanced_objects` with `json.JSONDecoder().raw_decode` prose scan; accept
   only schema-valid payloads (nodes list, allowed types, edges reference returned
   nodes). Fits existing `parse_extraction()` (`:195–237`).
   **Evidence:** RBE recommends `raw_decode` over hand-rolled brace scanning (handles
   JSON string state correctly) + fenced-first ordering. *(JUDGMENT/industry — RBE.)*
   Golden autopsy: 171 prose-wrapped responses dropped, only 4 truncated (CODE/readout).

2. **Stop silent config fallback** (`companion/__init__.py:173–183`): missing config →
   scaffold defaults OK; **malformed** config → raise loud.
   **Evidence:** ADR D8 + user directive "any LLM we use needs explicit
   provider/model/parameters, fail-loud." Golden judge auto-picked a TTS model → 14×
   HTTP 400 (CODE/readout). *(JUDGMENT — operational correctness.)*

3. **Explicit LLM params at every call site:** `ask` add `max_tokens`
   (`companion/__init__.py:458–464`); merge judge add `max_tokens`+explicit model
   (`resolve/__init__.py:482–485`); golden judge add `max_tokens`, drop `model="auto"`
   → pin/filter to chat-capable (`tests/golden/bin/judge.py:347–356, 398–404`).
   **Evidence:** same as #2. *(JUDGMENT.)*

4. **Gold-span schema** in `questions.yaml`: add `gold_spans: [{path, byte_start,
   byte_end, quote_hash}]` (today only `expected_source_paths` + `must_contain`).
   **Evidence:** R04 evaluation methodology — the "needle/gold-span" approach: tag the
   source span that contains each answer, then measure whether the chunker keeps it
   intact + retrievable. *(STRONG for the metric concept; the dedicated chunking
   benchmark itself is PRELIMINARY — R04 notes **no peer-reviewed chunking benchmark
   exists**; Chroma is the closest, industry-only.)*

5. **Golden judge metrics** (`judge.py:235–270`): emit `gold_span_intact`,
   `gold_span_retrieved@k`, token-recall/IoU alongside the existing byte-hash check.
   **Evidence:** R04 — token-level recall/precision/IoU vs gold spans (Chroma eval
   methodology); isolate the chunker by holding retriever fixed. *(PRELIMINARY —
   industry; the underlying retrieval metrics recall@k/nDCG are STRONG: BEIR NeurIPS
   2021, MTEB EACL 2023.)*

**DoD:** judge no longer 14× unparseable; report carries gold-span + token metrics;
provenance gate stays 0 failures. No KG rebuild. (Recovering the 4 failures is NOT
required here — this phase makes recovery *measurable*.)

---

## Phase 2 — Query-time block-neighbor expansion

**Goal:** recover same-document context without storing overlap. **Issue:** 5.

1. Extend `QueryHit` with `context_spans: tuple[Provenance, ...]`
   (`models.py:47–51`).
2. `block_context_spans(store, block_id, radius=1)`: find hit's `block_id`
   (`vault.py:374–405`), load same-`source_path` Blocks, select `block_index ± k`
   (data model supports it: `schema/support/block.py:48–54`, source_path facet at
   `ingest/__init__.py:48–57`) — all CODE.
3. Populate in `_to_query_hits` (`vault.py:358–372`); expose via `_serialize_hit`
   (`server/http.py:857–867`); consume in `ask` (`companion/__init__.py:445–464`).

**Evidence:** ADR D5 + RBE/R13 — store overlap-free, recover context at read time
(small-to-big / neighbor expansion) instead of baking overlap into storage; overlap
breaks clean provenance. *(JUDGMENT + PRELIMINARY — the small-to-big pattern is
widely-used industry practice, not a single peer-reviewed result.)*
**Why this is safe to do early:** it's additive and the data model already supports it
(CODE) — does not depend on the new chunker.

**DoD:** provenance green for primary + context spans; ≥1 of the 4 failed questions
improves `gold_span_retrieved@k` with no T4 regression. No KG rebuild.

---

## Phase 3 — Content-addressed caches + real rebuild

**Goal:** make rebuild/chunker-switch affordable + versioned. **Issues:** 6, 7-foundation.

1. SQLite cache `.marginalia/cache.sqlite`: embedding `(text_hash, embed_model_id) →
   vector`; extraction `(text_hash, prompt_version, extractor_model_id, schema_version)
   → payload`.
   **Evidence:** RRC — content-addressed embedding caches ship today (LlamaIndex
   IngestionCache, LangChain RecordManager, Haystack — official docs, STRONG); the
   **extraction** cache keyed on `prompt_version` is RRC's recommended design, **not
   observed in any tool** (PRELIMINARY/novel). RRC explicitly: "prompt_version is the
   part teams forget."
2. **Cache keys use text hash, NOT `Block.id`** (extraction text from
   `_extraction_units` `companion/__init__.py:532–564`; embeddings at `:314–320`,
   `:718–727`). Reuse existing `model_id`/prompt-hash fingerprints (`:567–611`,
   `:696–708`) — CODE.
   **Evidence:** RRC + ADR D6 — keying on text hash decouples cache reuse from Block
   identity, so the caches work regardless of `Block.id` composition. *(JUDGMENT, RRC.)*
3. **Wire `kg rebuild` to real ingest** — remove `_noop_ingest`
   (`cli/kg.py:68–85, 247–249`); record chunker version in rebuild state
   (`:101–109, 216–224`) — CODE.

**DoD:** zero output change on v1 chunker; cache keys never use `Block.id`; `kg
rebuild` actually ingests; provenance green; second golden run reports cache hits.
No Block-id migration.

---

## Phase 4 — Deterministic-v2 chunker + embedder-derived cap

**Goal:** replace token-fragmentation with structure-aware, byte-exact packed Blocks.
**Issues:** 3, 4, 7.

1. **Strategy = structure-aware recursive split → size-targeted packer → byte stamper.
   NOT embedding-semantic chunking.**
   **Evidence:** R04 — Qu et al., *"Is Semantic Chunking Worth the Computational
   Cost?"* (**arXiv:2410.13070**, 2024): gains over fixed/recursive are inconsistent
   and rarely justify the cost. *(PRELIMINARY — credible controlled negative, single
   study; R04 flags no peer-reviewed chunking benchmark exists.)*

2. **Embedder-derived max** — extend `EmbeddingProvider` with `model_id`, `max_tokens`,
   `count_tokens` (`embed/__init__.py:7–9`); resolve from config (default
   `bge-small-en-v1.5`, `config/_vault.py:55–62`) — CODE.
   `max_chunk_chars = floor(max_tokens × chars_per_token × 0.9)`, ~3.5 cpt English /
   3.0 code → **~1,600 char hard cap** for bge-small (512 tok), target ~900–1,400.
   **Evidence:** R13 — the 512-token wall causes **silent truncation** (sentence-
   transformers docs + issue #1269 — STRONG mechanism); per-model windows from HF model
   cards (STRONG); a chunk past the window is strictly worse (same vector cost, lost
   tail); mean-pooling dilution favors targeting *below* the wall (LongEmbed EMNLP 2024
   arXiv:2404.12096 — peer-reviewed; Chroma ~400-tok recall peak — industry).
   ⚠️ `chars_per_token` 3.5/3.0 are **uncalibrated heuristics** — R13 + ADR flag this;
   add a defensive token-exact recheck and calibrate on the real corpus before locking.

3. **Document-relative ranged sizing + transcript/flat detection** from text stats
   (speaker-ratio, line/unit percentiles, heading count) → switch presplit unit.
   **Evidence:** R13 dilution + R04 size-is-task-dependent; the transcript-handling
   design is **JUDGMENT** (Codex synthesis), no direct citation — flag as preliminary.

4. **Code → tree-sitter** (`Node.start_byte/end_byte`, verbatim slice); language/symbol
   in **facets, not new types** (`BlockKind` closed, `schema/support/block.py:23–31`).
   **Evidence:** RBE — tree-sitter is the one clean byte-exact win (MIT, mature);
   *(STRONG for the offset guarantee — it's a structural property, not a benchmark.)*

5. **Prose boundaries via `semantic-text-splitter` / `semchunk`** (offsets we map to
   bytes), **stamp bytes ourselves**. **Reject** LangChain `add_start_index` (uses
   `str.find`, mislocates on repeats), LlamaIndex `start_char_idx` (maintainer-
   deprecated as "frequently incorrect"), Unstructured/Docling (normalize → break
   sha256-of-raw-slice).
   **Evidence:** RBE — per-library offset audit + the build-on-top verdict.
   *(STRONG/verified — RBE checked the libraries' offset behavior directly.)*
   ⚠️ RBE requires a **round-trip property test** that the prose splitter is byte-exact
   at whitespace boundaries before trusting it.

6. **Keep `Block.id = sha256(path, content_hash, block_index)` unchanged.**
   **Evidence:** CODE — `block_id` is a FK across Claim/Finding/Annotation/store/
   extract/query; dropping `index` collides repeated paragraphs
   (`schema/support/block.py:33–38`, `ingest/markdown.py:104–113`). ADR D6 agrees;
   **contradicts ADR implementation-step-6** (see disagreement below).

7. **Encode library/grammar versions in `CHUNKER_VERSION`** (markdown-it-py, tree-sitter
   grammars) so rebuilds are reproducible (`pyproject.toml:20–27`) — CODE.
   **Evidence:** ADR D7 determinism requirement.

**DoD:** Block count drops materially from 1959 without giant chunks; all 4 failed ids
(`t2_decomposition_analogy`, `t2_maturity_jump`, `t3_verification_degradation`,
`t3_shared_obstacle`) improve to ≥ gold-span-retrieved; T4 stays 2/2; provenance green.
**Requires `kg rebuild`.**

---

## Phase 5 — PDF/docx two-layer provenance

**Goal:** binary ingest, only after canonical-text determinism is proven. **Issue:** 8.

1. **Determinism spike FIRST** — same file + pinned extractor version, repeated runs,
   compare canonical-text bytes + page-map bytes. **Do not proceed unless byte-stable.**
   **Evidence:** ADR ⚠️ + RBE — PDF extraction is notoriously non-deterministic
   (reading order, ligatures, whitespace) across versions/runs; if unstable the inner
   hash is meaningless. *(STRONG caution — RBE documents this directly.)*
2. Add format adapters (today rejects non-`.md`, `ingest/__init__.py:21–25`); persist
   original binary as `Document` (sha256 already on `Document`,
   `schema/support/document.py:17–29`); persist canonical extracted text as a second
   `Document` under `.marginalia/extracted/`; anchor Blocks into THAT; link via
   `SAME_WORK_AS_EDGE` (`document.py:32–40`); page/bbox as facets — CODE.
   **Evidence:** ADR D2 + RBE — two-layer (file hash + extracted-text artifact) is how
   real provenance-sensitive systems (legal/eDiscovery, Docling, Unstructured) cite
   back to binary sources; byte-exact-into-original is impossible. *(JUDGMENT +
   industry — RBE.)*

**DoD:** binary ingest stays disabled until the spike passes; markdown metrics don't
regress; reports distinguish original-binary hash from extracted-text-slice hash.

---

## Risks / ⚠️ assumptions to verify before building on them

| Risk | Verify how | Evidence flag |
|---|---|---|
| PDF/docx extraction determinism | repeated-extraction byte-diff spike, pinned lib+version (Phase 5 gate) | RBE + ADR ⚠️ — **STRONG caution** |
| `chars_per_token` 3.5/3.0 | calibrate on golden corpus w/ real tokenizer + token-exact recheck (Phase 4) | R13 + ADR ⚠️ — **uncalibrated** |
| prose splitter round-trip | byte-exact whitespace-boundary property test before adoption (Phase 4) | RBE ⚠️ |
| `Block.id` composition | DO NOT drop `block_index` (FK + collision) | CODE + ADR D6 |

---

## ADR corrections after reading code (Codex)

1. **ADR implementation-step-6 self-contradicts:** it says `Block.id = pure
   content_hash`, contradicting its own D6 ("leave Block.id alone") and the schema/code.
   **Resolution: keep `block_index` in the id.** *(The ADR's D6 decision table is
   correct; only the step-6 prose was wrong — already partly corrected, fix fully.)*
2. **Phase order:** caches belong BEFORE the new chunker (extraction is the cost
   leverage point — RRC 20–100×), not after as the ADR sequence implied.
3. **Issue-1 framing partly stale:** the parser already has a balanced-object fallback;
   the real gap is `raw_decode` + schema validation, not "no prose handling at all."

---

## Evidence-strength summary

| Phase | Rests on | Strength |
|---|---|---|
| 1 — extractor/config/eval | RBE (raw_decode), R04 (gold-span/token metrics), retrieval metrics (BEIR/MTEB) | **Mixed**: metrics STRONG, chunking-benchmark PRELIMINARY, config JUDGMENT |
| 2 — neighbor expansion | small-to-big pattern, ADR D5 | **PRELIMINARY/JUDGMENT** (industry practice, data model verified CODE) |
| 3 — caches | RRC (extraction-dominates STRONG-ish; embedding caches STRONG/official; extraction cache NOVEL) | **Mixed** |
| 4 — chunker + cap | R04 Qu et al. (PRELIMINARY), R13 512-wall (STRONG mechanism) + LongEmbed (peer-reviewed) + Chroma (industry), RBE tree-sitter (STRONG), transcript-detection (JUDGMENT) | **Mixed — strongest where it matters (the cap), weakest on transcript heuristics** |
| 5 — PDF/docx | RBE determinism caution (STRONG), two-layer pattern (industry) | **PRELIMINARY, gated on a spike** |

**Headline honesty:** there is **no peer-reviewed, dedicated chunking benchmark**
(R04). The plan's *strongest* evidence is the embedder-window cap (R13, mechanism-level
STRONG) and the byte-exact library audit (RBE). The chunking *strategy* choice
(structure over semantic) rests on one credible arXiv negative result (Qu et al.) — good
enough to justify "don't pay for semantic," not a proof. Everything semantic/transcript/
two-layer is preliminary and should be validated by the golden harness itself, which is
exactly why Phase 1 (measurement) lands first.
