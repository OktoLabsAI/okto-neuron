---
type: authority
created_at: 2025-11-01T00:00:00Z
agent: agent:bob-engineer
authority_id: authority-agent-bob
role: proposal-author
---

# Authority Record for agent:bob-engineer

This fabricated authority record identifies agent:bob-engineer as the proposal author for the synthetic markdown vault. The actor is used by the ACME proposal and the kickoff transcript.

The record deliberately presents `agent:bob-engineer` as a canonical authority. That is important because `drift_alias.md` declares the same CURIE as an alias of `agent:robert-engineer`, creating a controlled collision for the authority alias detector.

No real surname, email address, domain, or public identity is included. The name is a fixture label only, chosen to be easy to search and easy to distinguish from agent:alice.

## Authority Scope

agent:bob-engineer may author proposals, attend synthetic planning meetings, and comment on identifier hygiene. The actor does not approve Statements of Work in this vault.

The conflicting alias relationship should remain unresolved. Tests that inspect authority records should see both this canonical record and the alias declaration, then report the collision rather than silently choosing one.
