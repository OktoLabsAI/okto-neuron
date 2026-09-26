---
type: authority
created_at: 2026-04-01T12:00:00Z
agent: agent:bob-engineer
alias_of: agent:robert-engineer
---

# Colliding Authority Alias

This synthetic authority note declares `agent:bob-engineer` as an alias of `agent:robert-engineer`. The vault also contains `authority/agent-bob.md`, which presents `agent:bob-engineer` as a direct authority record rather than a subordinate alias.

The mismatch is intentional. A resolver that treats authority identifiers as canonical should notice that the same agent CURIE is represented both as a primary authority and as an alias pointing somewhere else. That condition is the collision target for `authority_alias_collision`.

The names are invented placeholders for a fixture and do not represent real people. The record avoids emails, external domains, and organization names so tests can focus on identifier shape rather than entity enrichment.

## Collision Scenario

In the fabricated workflow, one operator imported an old nickname map while another operator created a clean authority card. The two records now disagree about whether `agent:bob-engineer` is the canonical actor or a redirect. The fixture should remain unresolved so the detector has a stable negative state to inspect.
