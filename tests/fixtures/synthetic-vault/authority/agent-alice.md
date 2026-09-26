---
type: authority
created_at: 2025-11-01T00:00:00Z
agent: agent:alice
authority_id: authority-agent-alice
role: reviewer
---

# Authority Record for agent:alice

This fabricated authority record identifies agent:alice as a reviewer for the synthetic markdown vault. The role gives tests a stable actor who can sign a Statement of Work, create decisions, and own concept notes.

The record is intentionally plain. It does not contain a real surname, email address, domain, or organizational identity. It only establishes a canonical CURIE that other fixture files can reference.

agent:alice appears in the commitment drift fixture, the Q1 Statement of Work, the decision chain, and both concept notes. Those references should resolve to this authority record when a parser builds actor relationships.

## Authority Scope

The authority scope is limited to fixture review. The actor may approve note structure, record synthetic decisions, and explain concepts. The actor does not represent a real person or any external account.

This file should remain non-conflicting. Unlike the bob-engineer alias fixture, agent:alice has a single canonical authority record and no alias redirection.
