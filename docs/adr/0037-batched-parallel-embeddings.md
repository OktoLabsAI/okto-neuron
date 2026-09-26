# ADR 0037: Batched, Bounded Parallel Embeddings

- **Status:** Accepted
- **Date:** 2026-07-15
- **Deciders:** Marginalia maintainers
- **Relates to:** ADR 0015, ADR 0036

## Context

Marginalia's embedding providers expose only `embed(text)`. Ingest therefore
sends one request for every unique extracted node and every new relationship
Claim, while vectors-only re-embedding repeats the same singleton loop. For a
remote LiteLLM provider this creates thousands of small HTTP requests even
though the universal embeddings API and the local providers accept lists.

Launching every singleton call concurrently would move the bottleneck into
provider rate limits and make ordering and failure handling harder. The useful
unit of parallel work is a bounded batch, not an individual text.

## Decision

### 1. Providers own native multi-input embedding

The embedding boundary gains an optional native `embed_many(texts)` operation.
LiteLLM receives one list-valued `input`; FastEmbed and Sentence Transformers
receive their native list inputs; the deterministic stub maps the same stable
function over the list. A shared compatibility adapter falls back to `embed()`
for injected or third-party providers that implement only the historical
single-text protocol.

Every bulk result is checked before use: response cardinality must equal input
cardinality, response indices must be complete and unique when present, and
every vector must match the configured dimension. A bad batch fails loud; the
pipeline does not silently retry as singleton calls.

### 2. Batching precedes concurrency

`embedding.batch_size` controls the maximum inputs in one provider request
(default `32`, range `1..256`). `embedding.max_concurrent_batches` controls how
many requests may be in flight (default `1`, range `1..32`). The conservative
concurrency default is portable across hosted, gateway, and local providers;
operators can raise it when their endpoint has capacity.

Results are written in original input order regardless of completion order.
Only provider calls run concurrently. Candidate folding, ledger writes,
curation, graph writes, and graph swaps remain ordered on their owning thread.

### 3. All bulk write paths share the operation

Extraction first collects and deduplicates node candidates, then runs an actual
embedding stage over those candidates. Relationship Claim construction first
plans new Claim nodes, bulk-embeds them, then writes them in edge order while
preserving existing-claim merge and provenance semantics. Explicit vectors-only
re-embedding uses the same batching operation. Query-time embeddings remain
single-input because there is only one query.

### 4. Execution policy is live and vector-space neutral

Batch size and concurrent-batch limits are execution settings. They are not
sent as model parameters and are not part of `REEMBED_FIELDS`; changing them
never invalidates stored vectors. Long-running embedding schedulers re-read the
effective values at each scheduling boundary. Raising the limit fills new
slots; lowering it stops new submissions without cancelling requests already in
flight.

The Embedding config tab exposes both settings under Advanced execution. Its
Test action submits two inputs and requires two valid vectors, proving the
selected provider/gateway's real batch path rather than only singleton liveness.

## Consequences

- Remote embedding overhead falls primarily through fewer requests, with
  bounded parallel batches available as a second throughput lever.
- Batch size `1` and concurrency `1` reproduce historical scheduling without a
  separate compatibility mode.
- Provider-specific batch limits remain explicit operational policy because
  LiteLLM does not expose one universal maximum for every upstream model.
- A provider that cannot honor list input fails during Test or embedding with a
  clear batch contract error instead of silently degrading to the old request
  pattern.
