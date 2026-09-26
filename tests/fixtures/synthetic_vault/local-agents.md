---
title: Local Agents
tags: [agents, local-first, mcp]
---

# Local Agents Need Memory

Local coding agents need durable context across sessions without sending private notes to a hosted service.
Marginalia exposes the graph through [[MCP]] so Claude Code, Cursor, and custom tools can query the same vault.

- Agents should cite byte offsets for every answer.
- Agents should prefer deterministic claims before model-assisted extraction.
