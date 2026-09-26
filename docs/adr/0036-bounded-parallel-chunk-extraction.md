# ADR 0036: Bounded Parallel Chunk Extraction

- **Status:** Accepted
- **Date:** 2026-07-15
- **Deciders:** Marginalia maintainers
- **Relates to:** ADR 0015, ADR 0021, ADR 0030

## Context

`Companion.remember()` extracts one anchored chunk at a time. Each chunk is
independent until its candidates are folded into the document-level proposal
set, so a remote provider or LiteLLM gateway spends most of an ingest waiting
for avoidably serialized network round trips.

Parallelizing the whole ingest pipeline would be unsafe. Candidate folding,
embedding, deduplication, ledger writes, curation, and graph commits share
mutable state and have deterministic ordering contracts. Extraction tracing
also previously used one loop-local context, which would attribute concurrent
requests to the wrong chunk, while the ingest-history callback was not
serialized for calls from multiple workers.

## Decision

### 1. Concurrency is an extraction orchestration setting

`llm.extraction.max_concurrent` controls the maximum number of chunks whose
extractor calls may be in flight at once. Its effective default is `1` and its
validated range is `1..32`. The default preserves existing behaviour and lets
operators raise the value beyond eight when their provider has capacity.

The setting belongs to the Extraction LLM card, next to provider and model. It
is not a LiteLLM generation parameter and is never sent to a model.

### 2. Only independent chunk calls run concurrently

Workers execute `extractor.extract(text, provenance=...)` for distinct chunks.
The rounds inside one chunk — including auto escalation, enumerate/describe,
empty-result retry, and multi-sample union — remain sequential. This prevents
hidden multiplication of the configured concurrency bound.

Results are consumed in original chunk order. Candidate embedding and folding,
anomaly accounting, progress, deduplication, ledger operations, curation, and
graph writes remain on the caller thread. Provider failures retain the existing
per-chunk partial-failure semantics.

### 3. Observability is concurrency-safe

Each worker installs its chunk context in thread-local storage before calling
the extractor and restores the previous context afterwards. The ingest event
callback is serialized, so concurrent request/response events cannot race while
mutating or persisting queue history. Ordered extraction-result events and
progress remain emitted by the caller thread.

### 4. The concurrency limit is a live execution policy

The extractor owns a lazily populated worker pool at the validated ceiling and
submits work through a bounded sliding window. While waiting for the next
source-ordered result, it re-reads only the effective
`llm.extraction.max_concurrent` value every 250 milliseconds and also re-reads it
at every completed-chunk scheduling boundary. Model, prompt, sampling, and all
other semantic settings remain fixed for the current `remember()` call.

Raising the limit fills the new slots immediately. Lowering it stops new
submissions until the number of outstanding calls falls under the new limit;
already-running provider requests are not force-cancelled. A transient invalid
config keeps the last valid limit and emits one visible reload-error event
instead of aborting the ingest.

## Consequences

- Remote providers and gateways can overlap chunk latency without changing
  graph-write semantics.
- Local or rate-limited providers can keep the default of one.
- A configured value of 32 is a bound, not a guarantee: fewer non-empty chunks
  create fewer workers.
- An active extraction adopts a saved concurrency change within 250 milliseconds
  while waiting on a provider, or at its next completed-chunk scheduling boundary,
  without a daemon restart.
- Cancellation remains cooperative. Pending calls are cancelled when possible;
  an already-running provider request must return or observe the existing
  cancellation predicate before its worker exits.
