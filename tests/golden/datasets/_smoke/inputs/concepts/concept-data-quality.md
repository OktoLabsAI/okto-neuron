---
type: concept
created_at: 2026-01-05T00:00:00Z
agent: agent:alice
concept_id: data-quality
---

# Data Quality

Data quality in this synthetic vault means that a markdown note is structured enough to support retrieval, review, and drift detection. The minimum standard is simple: valid frontmatter, a clear type, an ISO8601 creation timestamp, and a responsible actor in CURIE form.

The concept does not require every note to become a database record. It only asks that the note carry enough stable metadata for tests to distinguish commitments, decisions, proposals, transcripts, authority records, and concepts.

Good data quality also means the body text should not contradict the frontmatter. If a proposal says it was authored by one agent in the body and another agent in metadata, the vault becomes harder to reason about. This fixture keeps those statements aligned unless a drift detector intentionally needs a conflict.

## Practical Checks

Reviewers can inspect data quality by checking line endings, trailing whitespace, encoding, and required keys. Those checks are deliberately mechanical so the fixture stays useful across parsers and operating systems.

The concept pairs with the provenance concept. Data quality describes whether a note is shaped correctly; provenance describes whether a reader can understand where the note came from and why it exists.

## Fixture Examples

A high-quality commitment note has a commitment type, a created timestamp, an actor, a committed timestamp, and a due date. If it has no closure record after the due date, that absence should remain explicit so drift checks can report it.

A high-quality decision note has a decision identifier and a clear head flag. If it supersedes another decision, the relationship should be declared in frontmatter rather than buried only in prose.
