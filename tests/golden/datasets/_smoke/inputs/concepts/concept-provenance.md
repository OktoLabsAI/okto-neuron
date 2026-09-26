---
type: concept
created_at: 2026-01-05T00:00:00Z
agent: agent:alice
concept_id: provenance
---

# Provenance

Provenance in this synthetic vault means the visible history of a note: who created it, when it was created, and what relationship it has to other notes. The frontmatter gives the first layer, while the body explains why the record exists.

The fixture uses provenance fields in a limited way. Decisions can point to earlier decisions through `supersedes`, commitments can carry committed and due dates, and authority notes can identify canonical actors. Those relationships let tests evaluate graph behavior without using real organizational data.

Provenance is especially important for stale records. A decision marked `head: false` may still matter because it explains how the current rule evolved. An overdue commitment may still matter because it shows a promised action that has not been closed.

## Fixture Role

This concept note gives search and embedding tests a neutral explanatory artifact. It should not trigger the drift detectors by itself, but it should give natural-language context for why the detector-specific notes exist.

The examples are fabricated and intentionally local to the vault. There are no external sites, real accounts, email addresses, or production identifiers in this concept.

## Relationship Examples

The Statement of Work shows provenance through its signer and signed timestamp. The proposal shows provenance through its author and authored timestamp. The transcript shows provenance through its meeting date and participant list.

These examples let tests compare different artifact types without inventing a large domain model. Each note has just enough structure to explain its origin and just enough prose to behave like a realistic markdown file.
