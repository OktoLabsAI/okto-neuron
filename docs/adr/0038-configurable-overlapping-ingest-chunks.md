# ADR 0038: Configurable Overlapping Ingest Chunks

- **Status:** Accepted (overlap:chunk_size ratio capped 2026-09-03, see amendment)
- **Date:** 2026-07-15
- **Deciders:** Marginalia maintainers
- **Relates to:** ADR 0023, ADR 0024, ADR 0036

## Context

Markdown and plain-text ingestion currently uses a fixed 12,000-byte extraction
window with no overlap. The size is hidden in the parser, so operators cannot
tune context for a model or corpus. Adding overlap naively would also let stored
Blocks from an old partition appear beside a new partition after re-ingestion,
because Blocks are append/upsert state and a still-valid byte slice is not an
orphan merely because the chunking policy changed.

## Decision

### 1. Chunking is typed ingest configuration

`ingest.chunk_size_bytes` defaults to `12000` and accepts `256..1000000`.
`ingest.chunk_overlap_bytes` defaults to `0`, must be non-negative, and must
be strictly smaller than the configured chunk size. The defaults reproduce the
existing parser byte-for-byte.

Both fields are application defaults that vaults may inherit or override. They
are exposed together in Config > Ingestion. A save affects the next ingestion;
existing derived Blocks and extraction results change only when their sources
are re-ingested.

### 2. Windows remain byte-anchored and line-safe

The parser builds windows from whole source lines. When overlap is enabled, the
next window begins at the first line boundary whose suffix is no larger than
the requested overlap. Progress is always at least one line, so overlap cannot
create an infinite loop.

Every Block continues to hash the exact source byte slice identified by
`byte_start` and `byte_end`.

### 3. Blocks record their chunking policy

Materialized Block facets include `chunk_size_bytes` and
`chunk_overlap_bytes`. Legacy Blocks without these facets are interpreted as
the historical `12000/0` policy. Extraction and incremental snapshots select
only Blocks matching the effective policy; other partitions are classified as
stale partition state and never extracted alongside the current partition.

### 4. Saving is non-destructive

Changing either setting takes effect on the next ingest and does not restart
the daemon or invalidate embedding vectors by itself. Existing sources are not
silently rewritten. The UI and PATCH response state that already-ingested
sources must be reingested to rebuild their Block partition under the new
policy.

## Consequences

- Different vaults can tune extraction context without code changes.
- Overlap improves boundary context while retaining exact byte provenance.
- The default remains byte-for-byte compatible with existing vaults.
- A chunking change deliberately incurs extraction cost on the next reingest,
  but remains cheap and non-destructive at config-write time.
- Byte provenance stays exact; no token estimate or normalized text offset is
  used as an anchor.

## Amendment — 2026-09-03: overlap:chunk_size ratio is capped at 50%

Finding 3.15 of a 2026-09-03 deep review measured that `chunk_overlap_bytes`
approaching `chunk_size_bytes` (ratio → 1.0) makes each window barely advance
past the last, causing near-exponential block-count amplification: a 0.95
ratio produced a **~20x block-count blowup** on a 2.4MB fixture. Decision §1's
original constraint — overlap "must be non-negative, and must be strictly
smaller than the configured chunk size" — allowed that pathological range; a
ratio under 1.0 was never actually safe.

`parse_markdown` (`src/marginalia/ingest/markdown.py`) now also rejects any
`chunk_overlap_bytes / chunk_size_bytes` ratio **greater than 50%**
(`_MAX_CHUNK_OVERLAP_RATIO = 0.5`; exactly 0.5 is still allowed), raising
`ValueError` and naming both configured values plus the computed ratio. This
tightens, not replaces, the existing "strictly smaller than chunk size"
check — both must hold. The cap is enforced in the parser itself, so it
applies uniformly regardless of caller: `IngestConfig`/`PATCH /api/v1/config`
writes, direct library calls, and `kg rebuild`.
