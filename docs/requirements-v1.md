# Marginalia — Requirements v1

> Captured: 2026-05-19 · Source: 5-round requirements interview with Alex
> Status: **Frozen historical requirements input; reconciled 2026-07-13**

This document records the product requirements that drove the RFC and research rounds; it is
not the current release checklist. The closed primitive model, byte-anchored provenance,
local/remote-per-step providers, MCP query surface, authority resolution, drift detection, and
one-vault trust boundary informed the shipped system. Several items originally listed as out of
v0—notably REST/Web UI and scheduled curation—later shipped. ADR 0034 subsequently replaced the
browser-authentication ceremony with a direct loopback application and per-vault runtimes, while cross-vault
fanout, shared multi-user vaults, two-way markdown sync, and Pulse migration remain future product
work. The open research questions were resolved or dispositioned in `RFC.md` and the ADRs.
Current release state and the product backlog live in [`roadmap.json`](roadmap.json).

---

## 1. Audience and posture

- **Primary persona:** Alex (founder, consulting / presales workflow).
- **Product owner:** OktoLabs AI (next release shipped in same form as Pulse).
- **Revenue:** desired, but not near-term. OSS-first.
- **Release model:** private on OktoLabs org during development; flip public when v0 ships.
- **License:** Apache 2.0. *(Dated note, 2026-09-25: this held through 0.2.0, published as
  Marginalia, and those releases stay Apache-2.0. From 0.3.0 Okto Neuron is licensed under the
  Elastic License 2.0 with the Okto Labs addendum; see `LICENSE`.)*

## 2. The real job-to-be-done

> *"Make the web of knowledge in my head and in the infinite artifacts I have into a system that I can rely on, that will allow LLMs to REALLY have context about things."*

- **#1 pain today: provenance traceability.** Every claim/decision must trace to source artifact + date + speaker.
- **Highest-leverage agent win: anomaly + drift detection.** Surface contradictions and supersedence chains across the corpus.
- **Aspirational corpus:** a private multi-client ExamplePortfolio snapshot — 21+ client folders,
  5,426 markdown files, 907K lines. Structure per client: `research/`, `artifacts/`,
  `transcripts/`, `docs/`, `tracking/`, `proposals/`, `rfp-documents/`, `requirements/`,
  `.state/`. The machine-specific source location is deliberately not part of this durable record.
- **Inspiration:** the `presales-toolkit` Claude Code plugin already operating on ExamplePortfolio. **It is a blueprint, not a dependency.** Marginalia is a generic substrate that *could* replace what presales-toolkit attempts, abstracted enough not to be "presales-flavored."

## 3. Success criteria (6-month)

All of:
- Daily personal use for knowledge work.
- Agents (Claude Code etc.) query it as long-term memory across sessions.
- OSS traction — installs, stars, PRs.
- Eventual stress test: full ExamplePortfolio ingest (NOT v0; future milestone).

## 4. Conceptual constraints (deferred to research team)

The user explicitly defers these to research — *"I don't even have the words to describe what would be conceptually ideal."* Trust the team to propose standards-grounded answers.

- **Entity abstraction.** Generic primitives that map down to Decision / RFP / SOW / Proposal / Requirement / Meeting / Conversation / Action Item / Client / Stakeholder — without being presales-flavored.
- **Provenance unit.** *"Whatever is considered ATOMIC."* The principled smallest meaningful unit (RDF triple? RDF-star quoted statement? nanopublication? CIDOC-CRM E13?). Research must recommend.
- **Vault model.** Single vault vs per-client vaults vs federated vs hybrid. Must work for ~21 client folders with cross-client query needs.

## 5. Functional requirements (v0)

| # | Requirement |
|---|---|
| F1 | Ingest markdown files from a directory into the graph. |
| F2 | Atomic provenance: every retrievable assertion carries source Document + atomic-unit identifier (offset/span/section per research recommendation) + date + agent. |
| F3 | Query via MCP from Claude Code (and other MCP clients). |
| F4 | Query returns results with provenance round-trip — every claim cites its atomic source unit. |
| F5 | Anomaly / drift detection runs in three modes, all configurable: (a) on-query, (b) on-ingest, (c) background sweeps. |
| F6 | Authority control: canonical entity records with variants/aliases/external IDs. |
| F7 | Local-first: works fully offline with local embeddings + local LLM. |
| F8 | Opt-in API: any pipeline step can be configured to use a remote model (Anthropic/OpenAI/etc.) on a per-corpus or per-step basis. |

## 6. Non-functional requirements

| # | Requirement |
|---|---|
| N1 | Confidentiality: client-folder data never leaves the machine unless explicitly opted in per step + per corpus. |
| N2 | Reproducibility: ingest is deterministic given (corpus, schema, model versions). Re-running yields same graph IDs where possible. |
| N3 | Survives dormancy: code/repo/data can sit untouched for weeks and still work. |
| N4 | Time investment is "relative" — no fixed weekly hours. Design must respect burst-mode + dormancy. |

## 7. Build approach

- **Codex-heavy for grind work**, Claude for design + integration + review (per Alex's prior memory `feedback_delegate_to_codex.md`).
- **Research-lab team** re-engaged for v2 round at major design decisions (schema lock, provenance unit, anomaly algorithm, vault model, local LLM stack).
- **Test corpus for v0:** one client folder — **`examplecorp`** from ExamplePortfolio. Real production data, bounded scope.

## 8. MVP definition (v0 "done")

ALL of:
1. End-to-end ingest of `examplecorp/` → graph.
2. MCP query from Claude Code returns useful answers with full provenance round-trip (every claim → atomic source unit).
3. Anomaly detection (on-query mode at minimum) surfaces at least one real drift/contradiction in the examplecorp corpus that wasn't manually spotted.
4. First OSS release published on OktoLabs org (public, Apache 2.0, README that holds up to scrutiny). *(2026-09-25: the first public source release is 0.3.0, under ELv2 plus the Okto Labs addendum.)*

## 9. Out of v0 (explicitly)

- ExamplePortfolio full ingest (5,426 files). Future stress test only.
- Multi-user / shared vaults / auth.
- Web UI. Obsidian / VS Code plugin.
- Two-way markdown sync.
- Pulse migration.
- Hosted / cloud service.
- Anomaly detection on-ingest + background sweeps (modes (b)+(c)) — design must accommodate; v0 ships (a) only.

## 10. Open research questions (research-lab v2 brief)

1. **Atomic provenance unit** — principled smallest meaningful unit. Survey RDF-star, nanopublications, CIDOC-CRM E13, PROV-O. Recommend unit + extraction strategy from markdown.
2. **Conceptual entity abstraction** — the 3-7 primitives that earn v0 inclusion. Survey Sowa Conceptual Graphs, PROV-O Entity/Activity/Agent, CIDOC-CRM top-level, schema.org Thing, BFO/DOLCE upper ontologies.
3. **Vault model** — single vs federated vs hybrid for ~21 client folders. Survey Wikibase federation, RDF named graphs, Neo4j multi-DB, data mesh.
4. **Anomaly/drift detection algorithms** — for (a) on-query, (b) on-ingest, (c) background sweep modes. Survey contradiction detection, supersedence validation, semantic drift, nanopublication conflict resolution.
5. **Local-first LLM/embedding stack 2026** — dense embeddings (BGE/Nomic/GIST), entity extraction (spaCy/GLiNER/Ollama-LLM), assertion extraction (OpenIE/LLM). Recommend v0 local stack with API fallback paths.
