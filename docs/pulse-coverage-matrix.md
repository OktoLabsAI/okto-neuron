# Marginalia RFC v1.0 → Pulse Story Coverage Matrix

> **STATUS: CLOSED 2026-05-19.** All P0/P1/P2 gaps resolved. RFC §1–§10 + MVP #1–#4 + F1–F8 + N1–N4 fully bound to topics on board `OktoLabs-KG-Independent`.

**Maintainer:** librarian · **Last updated:** 2026-05-19 (post cross-cut wave 4 — R5 pack-loader confirmation)
**RFC under coverage:** repository-root `RFC.md` (v1.0, 480 lines)
**Pulse board:** `OktoLabs-KG-Independent` (`5dd28195-bf3d-4b05-8e4d-b2af13dc506c`)

Rows = every RFC section + every MVP success criterion + every functional
(F1–F8) and non-functional (N1–N4) requirement from `requirements-v1.md`.
Columns = covering topic(s), current active story count, status.
Any row with `active_count = 0` on its covering topic is a **GAP**.

---

## A. Board-level totals snapshot

| Topic | Name | Active | Archived | Δ since v1 matrix |
|---|---|---|---|---|
| 01 | Core Schema | 18 | 0 | +2 (incl. pack-manifest loader assembly `6e594a1c` confirmed by R5 — absorbed §4.2a from topic 10 scope-cut) |
| 02 | Storage and Vaults | 12 | 0 | +2 |
| 03 | Ingest Pipeline | 14 | 0 | unchanged (librarian-owned) |
| 04 | Local Model Stack | 10 | 0 | unchanged |
| 05 | Anomaly and Drift Detection | 15 | 0 | +1 (story `2dbdadbe` — §4.7 IngestPreCommitHook Protocol added by R4) |
| 06 | Provenance and Atomic Unit | 14 | 4 | 4 archived to 01 by R3 |
| 07 | Surface APIs (Python) | 13 | 0 | +2 (Vault.export jsonld + jsonld-star) |
| 08 | CLI | 20 | 0 | +1 (kg export, librarian) |
| 09 | MCP Server | 14 | 0 | +2 |
| 10 | Type Packs and Distribution | 3 | 7 | 7 archived in R2 scope-cut |
| 11 | examplecorp Pilot and v0 OSS Release | 12 | 0 | librarian v1 matrix miscount — actually 11 all along; +1 (story `78b98853` — N3 dormancy validation by R4) |
| 12 | v0.2+ Roadmap | 25 | 3 | +21 from R3 |
| | **Total** | **170** | **14** | |

---

## B. RFC section coverage

| RFC section | Covering topic(s) | Active count | Status |
|---|---|---|---|
| §1 Summary | cross-cutting; primary anchors: 01, 02, 06, 07, 08, 09 | n/a | ✅ |
| §2 Motivation (wedge, Basic Memory, Anytype, differentiators) | 11 (README/positioning) | 12 | ✅ |
| §3 Conceptual grounding (15 standards rows) | 01, 03, 06, 10, 12 | 18/14/14/3/25 | ✅ |
| §4.1 Layered design | 01, 02, 04, 07, 08, 09, 10 | 18/12/10/13/20/14/3 | ✅ |
| §4.2 Core schema (5 primitives + 6 support types + composition + pack-type→primitive table) | 01 | 18 | ✅ |
| §4.2a Pack manifest (research-pack.yaml example) | **01** (loader assembly `6e594a1c` + YAML parser `1214302f` + kind_of validator `ff6b7087` + closed-set enforcer `60e6b640` + extends URI validator `c20d7ad6` + standards-mapping registry `0a9b7b73`) + 10 (pack content) | 18 + 3 | ✅ — absorbed into topic 01 via R5 |
| §4.3 Atomic provenance unit | 06 (+ 12 for LLM v0.2 pass) | 14 (+25) | ✅ |
| §4.4 Storage layout (vault tree, three N1 fences, fan-out, v0→v1, markdown canonical) | 02 | 12 | ✅ |
| §4.5 Local model stack | 04 | 10 | ✅ |
| §4.6 Surface APIs — Python | 07 | 13 | ✅ (now includes Vault.export) |
| §4.6 Surface APIs — CLI | 08 | 20 | ✅ |
| §4.6 Surface APIs — MCP | 09 | 14 | ✅ |
| §4.7 Ingest pipeline (loader → AST → Block/Annotation/Claim → authority → Ladybug txn → optional on-ingest hook) | 03 (deterministic v0) + 06 (Claim/PROV) + 12 (LLM v0.2 + non-md loaders) + 05 (IngestPreCommitHook contract) | 14 + 14 + 25 + 15 | ✅ — hook contract `2dbdadbe` |
| §5 Anomaly + drift detection (4 v0 algos + 4 reserved hooks + Finding contract + 3 modes + MVP gate) | 05 (+ 12 for non-on-query modes) | 15 (+25) | ✅ |
| §6 What's NOT in v0 (12 deferred bullets) | 12 | 25 | ✅ — expanded coverage |
| §7 Naming, licensing, repo | 11 | 12 | ✅ |
| §8 Roadmap v0.0.1 (scaffold — shipped) | n/a | n/a | ✅ |
| §8 Roadmap v0.1.0 | 02, 03, 04, 05, 06, 11 | 12/14/10/15/14/12 | ✅ |
| §8 Roadmap v0.2.0+ | 12 | 25 | ✅ |
| §9 Open questions | 08 (Q2), 09 (Q3), 10 (Q4, Q6) | 20/14/3 | ✅ |
| §10 Decision record (DEC-001..010) | see anchors below | n/a | ✅ |

### Decision-record anchors

| DEC | Topic |
|---|---|
| DEC-001 green-field, defer Pulse migration | 11, 12 |
| DEC-002 two-layer schema | 01, 10 |
| DEC-003 markdown canonical, graph derived | 02, 08 |
| DEC-004 sync core API + async MCP wrapper | 07, 09 |
| DEC-005 local-first default + opt-in API per step per corpus | 04 |
| DEC-006 Apache 2.0 | 11 |
| DEC-007 librarianship as conceptual spine | 01, 03, 06, 10 |
| DEC-008 five-primitive closed core | 01 |
| DEC-009 Claim = atomic unit, RDF-star wire | 06 |
| DEC-010 one Ladybug graph per client + opt-in fan-out | 02 |

---

## C. MVP success criteria (requirements-v1.md §8)

| MVP criterion | Topic(s) | Active count | Status |
|---|---|---|---|
| #1 End-to-end ingest of `examplecorp/` → graph | 11 + 03 + 02 | 12 + 14 + 12 | ✅ |
| #2 MCP query from Claude Code with full provenance round-trip | 09 + 06 + 07 | 14 + 14 + 13 | ✅ |
| #3 Anomaly detection surfaces ≥1 real drift in examplecorp | 05 + 11 | 15 + 12 | ✅ |
| #4 First OSS release on OktoLabs org (Apache 2.0 + README) | 11 | 12 | ✅ |

---

## D. Functional requirements

| Req | Statement | Topic(s) | Status |
|---|---|---|---|
| F1 | Ingest markdown files into the graph | 03, 08 | ✅ |
| F2 | Atomic provenance per assertion | 06, 01, 03 | ✅ |
| F3 | Query via MCP from Claude Code | 09, 08 | ✅ |
| F4 | Provenance round-trip on results | 07, 09, 06 | ✅ |
| F5 | Three drift modes (on-query, on-ingest, background) | 05 + 12 + 08 | ✅ |
| F6 | Authority control with variants/aliases/external IDs | 01 + 03 + 08 | ✅ |
| F7 | Local-first offline operation | 04 | ✅ |
| F8 | Opt-in API per step per corpus | 04 | ✅ |

---

## E. Non-functional requirements

| Req | Statement | Topic(s) | Status |
|---|---|---|---|
| N1 | Confidentiality (three fences + api-egress log) | 02 + 04 | ✅ |
| N2 | Reproducibility (deterministic ingest) | 03 + 06 + 04 | ✅ |
| N3 | Survives dormancy | 11 (story `78b98853` — manual procedure + dormancy-rehearsal.yml CI + recovery runbook + pre-publish rehearsal) | ✅ |
| N4 | Time investment "relative" | informational | ✅ |

---

## F. Open RFC text not bound to a topic (provisional)

| Item | Where in RFC | Status |
|---|---|---|
| `kg export --format jsonld` | §3 schema.org row | **Resolved 2026-05-19** — story 08-20 (`kg export`) + topic-07 `Vault.export()`. **Addendum 2026-07-07 (correction, not a rewrite):** confirmed via `grep` across `src/marginalia/cli/*.py` that no `export` subcommand ships; only `Vault.export(format: Literal["jsonld","jsonld-star"])` exists as a library method (`src/marginalia/vault.py:297`). `kg export` is reserved, not implemented as a CLI subcommand — see the same qualifier at `RFC.md:112`. |
| §4.7 "[if on-ingest mode] anomaly detector hooks" | §4.7 | **Resolved 2026-05-19** — R4 story `2dbdadbe` on topic 05: `IngestPreCommitHook` Protocol + `PendingBatch`/`IngestCtx`/`PreCommitResult` dataclasses + behavior contract + error semantics + ordering + test matrix |
| Topic 10 thinned to 3 active after R2 scope-cut | §4.2a + §3 extends URIs | **Resolved 2026-05-19** — R5 confirmed pack-manifest machinery lives in topic 01 (6 stories: loader assembly `6e594a1c`, YAML parser `1214302f`, kind_of validator `ff6b7087`, closed-set enforcer `60e6b640` [DEC-008], extends URI validator `c20d7ad6`, standards-mapping registry `0a9b7b73`). JSON-LD URI-emit logic lives in topic 07 Vault.export stories `72186753` + `ef9bc31d`. Topic 10 retains pack *content* only. |

---

## G. GAPS summary (sorted by severity)

| Sev | Gap | Status |
|---|---|---|
| 🔴 P0 | Topic 11 examplecorp/OSS release | ✅ **CLOSED** — 12 stories (incl. R4 N3 dormancy `78b98853`). Librarian v1 matrix was stale; topic 11 had 11 stories all along — R4 added 1 more. |
| 🟡 P1 | Topic 12 thin vs §6 deferrals | ✅ **CLOSED** — 25 stories |
| 🟡 P1 | §4.7 on-ingest hook contract | ✅ **CLOSED** — R4 story `2dbdadbe` on topic 05 |
| 🟢 P2 | Topic 10 scope-cut absorption | ✅ **CLOSED** — R5 confirmed pack-manifest machinery (6 stories) lives in topic 01 |
| 🟢 P2 | Cross-topic interface contracts | ✅ **CLOSED for matrix purposes** — to be verified per topic during ideation (03↔06 Claim emission API, 08↔09 build_app, 08↔05 algos identifier list, 08↔07 Vault.export signature for story 08-20). Not gaps — handoff items for spec stage. |

---

## H. Topic 03 + 08 story IDs (librarian-owned)

### Topic 03 — Ingest Pipeline (14 stories)

| # | Story ID | Title |
|---|---|---|
| 03-01 | 595f5af2-6fe9-4f31-a82f-8042b4a4ba10 | Ingest: markdown file loader |
| 03-02 | 4d9f7553-bda3-4242-a828-dae3f8137e08 | Ingest: CommonMark AST parser integration |
| 03-03 | eebf0351-91d0-4091-b2bf-fc008c8a7580 | Ingest: Block extraction with content_hash and block_kind |
| 03-04 | e4287633-6ada-44bc-95ae-d538166730f2 | Ingest: frontmatter Annotation + Claim extraction |
| 03-05 | 43ea2834-de17-41c1-ba75-975f5419369f | Ingest: heading Annotation extraction |
| 03-06 | db7ab941-5ec2-4764-a5e4-02f7c2b61e8a | Ingest: #tag Annotation + Concept extraction |
| 03-07 | ea85c9e0-be0d-418d-9716-a9b12cf31263 | Ingest: [[wikilink]] Annotation + cross-document edge extraction |
| 03-08 | 47abfbe7-9619-43d4-bbb4-6f89c86d90ab | Ingest: reproducible Claim ID hashing |
| 03-09 | db0788eb-a150-4350-8ab2-4b0e1eab062c | Ingest: PROV-O edge emission for every Claim |
| 03-10 | 532bf434-471c-4e7f-a4ae-dd186430bbfe | Ingest: deterministic authority resolution |
| 03-11 | 5f30322a-998d-4bbd-974d-02bdb3bbc134 | Ingest: Ladybug atomic transaction wrapper |
| 03-12 | dc961824-c07d-4f4b-b0bb-1969b21cb03d | Ingest: Ladybug write batching |
| 03-13 | d8089e97-0a28-444d-88ee-9ea2794a990f | Ingest: content_hash-driven re-anchor on markdown drift |
| 03-14 | 75afbdeb-7442-49af-94c8-9aee8c114ac8 | Ingest: pipeline orchestrator (deterministic v0) |

### Topic 08 — CLI (20 stories)

| # | Story ID | Title |
|---|---|---|
| 08-01 | a55de320-a941-4f8a-a747-4d9937f9c04d | CLI: Typer app scaffolding |
| 08-02 | 625ce216-7e15-4da8-a6d3-087dc6b5b357 | CLI: global --vault flag + vault resolution |
| 08-03 | 84a9d75d-8c2e-4916-a52a-b086bb442129 | CLI: global config loader (~/marginalia/marginalia.toml) |
| 08-04 | bc14d11f-62c5-4915-afbc-a916e71f58b5 | CLI: help text + --version |
| 08-05 | 9e2d66b3-62e7-4924-9ea5-5bfb09b4ef7e | CLI: exit code conventions |
| 08-06 | f7b74f9e-d248-4a2a-adaa-da8a129ffec7 | CLI: `kg init` command |
| 08-07 | 80e522b1-386c-40bc-b4cb-e49be0b3cb8b | CLI: `kg add` command (binding → topic-03 orchestrator) |
| 08-08 | 71229c3b-eb4b-4e5c-840f-a14afe9e8696 | CLI: `kg query` command |
| 08-09 | 5e502a46-69ed-493a-992a-432f22a7b57f | CLI: `kg cypher` command |
| 08-10 | b30081ce-dc43-4481-aa3c-9b55d3945b62 | CLI: `kg rebuild` command (binding → topic-03 rebuild flow) |
| 08-11 | 43753425-140a-46fc-b62a-1f0772938b6e | CLI: `kg detect-drift` command |
| 08-12 | d6f4bf08-f9c4-44fe-9d92-5cf46b819f5e | CLI: `kg snapshot` command |
| 08-13 | 75cd05e2-fe17-4947-9924-dffacc27262e | CLI: `kg drift-report` command |
| 08-14 | 3d9d4c6b-c0f4-4469-878f-a4f4916a84ca | CLI: `kg serve` command |
| 08-15 | c1021f8b-ce9f-4413-9327-4119a317ca30 | CLI: `kg reindex` command |
| 08-16 | ee3cfcc6-576e-4623-8419-ef1d711ed946 | CLI: `kg list-findings` command |
| 08-17 | 9491a748-bff9-4759-87f7-658fbd660251 | CLI: `kg authority resolve` command |
| 08-18 | 9ae8a021-404e-4184-b2c4-bd6a9be0bfc6 | CLI: `kg pack list` command |
| 08-19 | 1a5058d4-1fe4-41b7-bb07-811834c28b3b | CLI: global `--format text|json|jsonld` flag |
| 08-20 | 75608d63-e0da-4cba-b70c-b6499da6db59 | CLI: `kg export` command (link-dep → topic-07 Vault.export) |

---

## Update protocol

Anyone adding/changing stories on board `OktoLabs-KG-Independent` should ping
the librarian (SendMessage `to: librarian`) with the topic + delta so this
matrix stays in sync. The matrix is the 100%+ guarantee artefact for the
Marginalia v0 scope handed to Alex.
