# RFC: Marginalia — A Standalone Local-First Knowledge Graph

> **Name note (2026-09-25):** this RFC was written while the product was named Marginalia. It ships as
> Okto Neuron from 0.3.0; code references below to the `marginalia` package, command and paths map to
> `okto_neuron`, `okto-neuron` and `src/okto_neuron`. See [ADR 0044](docs/adr/0044-rename-to-okto-neuron.md).

> Status: **v1.0 design baseline**; current-state layer reconciled for the Okto Neuron 0.3.0 source release ·
> Author: Alex · Original date: 2026-05-19 · Reconciled: 2026-07-14, 2026-09-25
> Working name: **Marginalia** (alternates: *Stacks*, *Verso*, *Cartulary*)
> Predecessors: two research reviews dated 2026-05-19 (v1 and v2; internal, not published)
> Requirements: `docs/requirements-v1.md`

## 0. How to read this RFC now

This RFC preserves the May 2026 design and the reasoning that produced Marginalia. The
implementation has since moved far beyond the original `v0` sketch. The following is the
authoritative current-state layer; historical forecasts later in the RFC are retained only
as decision provenance where explicitly labelled.

- **Version and distribution:** from 0.3.0 the product ships as Okto Neuron from the public
  `OktoLabsAI/okto-neuron` repository: installers, release manifest, and the wheel as a GitHub
  Release asset. A PyPI package named `okto-neuron` is planned. Versions up to 0.2.0 were published
  as Marginalia prereleases under Apache-2.0 through the `OktoLabsAI/marginalia-dist` installer
  repo, whose release manifest stays at 0.2.0. Per-version status lives in `docs/roadmap.json`.
- **Schema and provenance:** the five primitive classes and six support types remain.
  `Block` is still stored. `SourceSpan` also ships and is populated on Claims; ADR 0003's
  final Block-removal cut is not implemented.
- **Storage and vaults:** Ladybug is optional behind the `ladybug` extra. One graph and one
  immutable runtime per open vault, tab-scoped REST/UI selection, and per-connection MCP vault
  selection ship; cross-vault `MultiVault` fan-out does not.
- **Write path:** LLM extraction, durable candidate-ledger records, deterministic and LLM
  resolve/curation, fail-closed gates, commit plans, incremental re-ingest, folder watch,
  and fresh-graph rebuild/heal all ship.
- **Read path:** fused retrieval, graph walk, typed subgraph rendering, and bounded source
  blending ship. Block answering remains the default; graph-native/efficient-hybrid mode
  remains opt-in through `llm.ask.enable_subgraph`.
- **Surfaces:** the Python `Vault` API, Click CLIs, bearer-authenticated five-tool FastMCP server,
  direct-loopback REST API, and compiled React Web UI ship. `Vault.export()` supports JSON-LD/JSON-LD-star
  with the `jsonld` extra; there is no `kg export` command.
- **Dependencies:** Click and `email-validator` are core. The package keeps
  `serve` as the complete published Ladybug + FastMCP + embeddings application boundary;
  `mcp` is an identical compatibility alias for older install guidance. Lower-level `ladybug` and
  `embeddings` extras remain available to library consumers. RDFLib,
  LiteLLM, Bedrock, and Sentence Transformers remain explicit optional closures. Clean-wheel
  tests execute each public feature boundary without relying on a combined environment.
- **Release state:** every version published so far is a prerelease. Exact-artifact Linux
  Docker+tmux lifecycle rehearsals are retained for the later prereleases; no published version has
  yet passed a real interactive Windows PowerShell 5.1 lifecycle. `0.0.40` and `0.0.41` are
  immutable and permanently non-promotable (a native Windows run of 0.0.41 exposed daemon lifecycle
  defects that later versions repaired). `docs/roadmap.json` records each version's evidence and
  limits; the dated application-quality audit and the release ledger are internal records.
- **Corrective compatibility:** the successor deliberately replaces client-derived budget API
  names with `CORPUS_BUDGET_SECONDS` and the `corpus` suite key, without legacy aliases. The
  `marginalia pilot` command remains stable, while its report basename becomes
  `pilot-log.YYYY-MM-DD.json`. These privacy-neutralizing public compatibility breaks belong only
  to the corrective release; downstream Python callers and report consumers must migrate.

---

## 1. Summary

Marginalia is a standalone, local-first knowledge graph usable as a Python library,
Click CLI, bearer-authenticated FastMCP server, direct-loopback REST service, and Web UI. It is grounded in
library and information science—faceted classification, authority control, SKOS, and
provenance—combined with embeddings and LLM-assisted extraction.

The schema is closed at five primitives (`Agent`, `Activity`, `InformationObject`,
`Concept`, `Place`) over six support node types (`Document`, `Identifier`,
`Annotation`, `Claim`, `Block`, `Finding`). A `Claim` is the atomic provenance
unit, anchored to a stored `Block` and optionally carrying a byte-exact `SourceSpan`.
JSON-LD and JSON-LD-star export are library features.

Storage is one Ladybug graph and immutable runtime per vault. Each browser tab selects a vault
for every scoped REST request, and each MCP connection selects one pooled vault, but no cross-vault
fanout API ships. Pulse continues to consume its in-tree
KG; migration remains a future RFC.

---

## 2. Historical motivation and product thesis (May 2026)

This section preserves the original market framing and research-era forecasts. It is decision
provenance, not a current competitor/pricing survey or implementation checklist. The durable
thesis still holds: local file ownership, provenance-bearing memory, a closed librarian-grade
schema, and agent-facing surfaces are the product's differentiators.

The KG inside Pulse is **~20 600 LOC, ~28 % of core** and technically generic,
but its shipped schema (`Decision`, `Criterion`, `Constraint`, `Requirement`,
`Alternative`, …) is engineering-decision-flavored. A user whose work is data-,
research- or comms-centric has no use for those primitives — the graph feels
welded to a workflow they don't run.

**The wedge is agent-portable memory with a librarian-grade spine.**
The user complaint that triggers this need, recurring across HN and
r/ObsidianMD in 2026, is: *"I want my Claude and my Cursor to remember
across sessions without paying $X/mo and without my notes living in a
proprietary store."* Marginalia is a local-first knowledge-graph MCP server
that any LLM tool (Claude Code, Cursor, custom agents) can call against one
markdown vault the user owns, shipping structured retrieval (`Document`,
`Annotation`, `Claim`, `Authority`, `SKOS`, Dublin Core) rather than
embeddings over freeform notes.

**The direct competitor is Basic Memory** (basicmachines-co), not Obsidian.
Basic Memory is OSS, MCP-shipped, markdown-canonical, and has real traction
(~2.6k stars, May 2026). It also gates its useful tier behind a $14.25/mo paid
SaaS and uses a freeform wikilink+observations schema with no Authority
control, no `Document` layer with `Annotation`/`Claim`, and no SKOS edges.
Marginalia adds, on top of that same surface area:

1. **Librarianship-grounded schema** that answers PKM complaints incumbents
   have left open — manual aliasing of "JP" vs "Jordan Lee Carter" (Authority),
   re-tagging every note when vocabulary evolves (SKOS induction), canonical
   entity merge — and avoids retrieval rot at scale, the bottleneck Basic
   Memory users hit first.
2. **Apache 2.0 with no paid tier**, full feature parity OSS forever. *(Through 0.2.0; see the 2026-09-25 license note in section 7.)*
3. **Headless library + CLI posture** so agent frameworks can embed Marginalia
   as a dependency. Basic Memory *is* the app; Marginalia is the substrate.
4. **Selectable built-in vocabulary profiles** (`core`, `research`, `personal`,
   `sdlc`) — fixed label/edge registries for common domains while the primitive
   and support-node schema remains closed. External manifest packs are not a
   production plug-in surface.

**Anytype is the medium-term threat** — their 2026 "local agents using
Anytype objects as memory" roadmap targets a structurally similar outcome on
a proprietary object store. Marginalia's survivable differentiator is
markdown-canonical: when the tool dies, the knowledge doesn't.

**Graphiti (Zep) is the closest agent-memory competitor**, not the mere backend
the early research framing assumed. By 2026 it has ~45k stars, ships its own MCP
server and REST surface, and through Zep targets agent memory head-on. The
distinction that still holds is trust-root: Graphiti is graph-canonical (Neo4j /
FalkorDB / Kuzu as the source of truth, no markdown vault), so it structurally
cannot serve the file-owned, local-first niche Marginalia is built for. Where it
genuinely leads is temporality — a peer-reviewed bi-temporal model (valid-time
vs transaction-time) that Marginalia tracks today only as transaction-time and
carries as a v1 roadmap item. The differentiator stands; the gap is named.

Pulse keeps its in-tree KG unchanged; once Marginalia is mature it can shed
28 % of core and consume Marginalia. Upside, not adoption pitch.

Design tenets (Ranganathan's Five Laws of Library Science, 1931, restated for
a personal KG): books are for use → optimize ingest friction (`kg add`); every
reader their book → retrieval latency target <100 ms p50; save the time of the
reader → no ceremony at write time; the library is a growing organism → schema
growth without migration pain. Tenets, not schema.

---

## 3. Conceptual grounding: librarianship + ML

Modern KGs frequently re-invent (badly) what cataloguers settled a century
ago. Marginalia adopts these established ideas explicitly:

This is the conceptual lineage, not a field-by-field API contract. References to `v0`,
`v0.1`, or `v0.3` in the table are May 2026 design forecasts; section 4 and the dated ADR
status addenda control what actually ships.

| Idea (source) | What it means | How Marginalia uses it |
|---|---|---|
| **PIM lineage** (Bush 1945; Engelbart 1968; Nelson; Jones 2007 *Keeping Found Things Found*) | Personal information management as a 70-year research thread. | Marginalia is the local-first, ML-native heir to this tradition. |
| **Faceted classification** (Ranganathan; modern: Notion / Airtable / Obsidian Dataview) | Orthogonal properties, not nested folders. | Tags and typed properties are facets. A note can be `topic:LLM`, `person:JP`, `place:internal`, `time:2026Q2` simultaneously. |
| **BIBFRAME 2.x Work / Instance / Item** (LC, 2016; supersedes FRBR 1998 / IFLA-LRM 2017) | Collapse the abstract idea, its embodiment, and the physical carrier. | Single optional `same_work_as` edge between `Document` nodes + optional `work_id` slot (Wikidata QID). No class hierarchy; at N=1 even Instance is implicit. |
| **Authority control** (LC, VIAF; modern: Wikidata QID hub — Heftberger 2024, Lemus-Rojas 2023) | Canonical record per entity; all variants resolve to it. | `Agent` nodes + aliases + external `Identifier` (QID, ORCID, DOI, ISBN). Embeddings propose merges; user/policy commits. |
| **SKOS** (W3C Rec, 2009-08-18) | `broader`, `narrower`, `related`, `exactMatch`, `closeMatch`, `altLabel`. | First-class v0 edge types for `Concept` hierarchies. LLM-assisted induction from folksonomy in v0.1 (SC-Taxo, TaxoAdapt ACL 2025). |
| **Dublin Core / DCMI Terms** (1995, expanded) | Minimal interoperable metadata vocabulary. | Conceptual alignment only. The live `Document` model stores carrier identity (`uri`, media type, byte length, SHA-256, discovery time); markdown `title`/`tags` and other frontmatter live on the compatibility Item/facets path rather than typed DCMI fields. |
| **PROV-O** (W3C Rec, 2013) | Provenance vocabulary: agent, activity, entity. | Every Claim carries `prov:wasDerivedFrom` → `Block`, `prov:wasGeneratedBy` → `Activity` (extraction), `prov:wasAttributedTo` → `Agent`. |
| **CIDOC-CRM E13 Attribute Assignment** (ISO 21127) | Event-centric model where a statement is itself an event. | `Annotation` borrows E13 shape ("X asserted Y about Z at time T"). We don't ship the CRM ontology. |
| **Folksonomy** (Vander Wal 2007; Trant 2009) | User-applied tags outperform expert taxonomies for personal corpora at small N. | Free-form tags are first-class v0; 30–70% of user tags fall outside any taxonomy. LLM promotes frequent tags to SKOS `Concept` nodes. |
| **Personal Knowledge Graph research** (Balog & Kenter 2019; Chakraborty et al. 2022 survey) | The PKG as a distinct sub-field of KG research. | Marginalia is a PKG implementation and cites the literature explicitly. |
| **Sowa Conceptual Graphs** (1984) | Typed nodes + typed edges as the foundational pattern. | Direct ancestor of Marginalia's primitive/relation core. |
| **schema.org / JSON-LD** (2011–; W3C JSON-LD Rec 2014) | The largest deployed structured-data vocabulary. | Export target for `kg export --format jsonld` (reserved; no CLI subcommand yet — `Vault.export()` exists as a library method, `kg export` is not implemented); cheap interop with the open web. |
| **Nanopublications** (Mons et al.; Kuhn 2013-2024) | `{assertion + provenance + pub-info}` smallest publishable unit. | Marginalia `Claim` mirrors the shape *without* trusty-URI signing (single-user vault; markdown + git is trust root). Signing is future product work. |
| **W3C Web Annotation Data Model (oa:Annotation)** (W3C Rec 2017) | Standard structure for annotations with selectors. | Marginalia `Annotation` aligns with oa selectors for surface-form occurrences. |

What modern processing power changes:

- **Embeddings collapse synonymy** that authority control used to require human labor for.
- **LLMs do extraction at ingest** (entities, relations, claims) that cataloguers did by hand.
- **Hybrid retrieval** (BM25 + dense + graph traversal) supplants either-or debates.
- **Faceted ≠ free-for-all**: type packs provide just enough structure for queryability.

The thesis: *catalogue like a librarian, retrieve like a 2026 ML system.*

---

## 4. Architecture

### 4.1 Layered design

```text
┌─────────────────────────────────────────────────────────────────────┐
│ Surfaces: Python Vault API · Click CLIs · FastMCP · REST · React UI │
├─────────────────────────────────────────────────────────────────────┤
│ Runtime: daemon lifecycle/auth · vault pool · config · job scheduler│
├─────────────────────────────────────────────────────────────────────┤
│ Services: ingest/extract · ledger · resolve/curate · retrieve/ask   │
│           drift detection · rebuild/heal/reembed                    │
├─────────────────────────────────────────────────────────────────────┤
│ Knowledge: built-in packs · five primitives · support/provenance    │
├─────────────────────────────────────────────────────────────────────┤
│ Storage: InMemoryStore (tests) · LadybugStore (one graph per vault) │
└─────────────────────────────────────────────────────────────────────┘
```

The Python API is synchronous. The server owns asynchronous scheduling, serialized writes,
and MCP/HTTP transport. ADR 0014's `VaultPool` selects one vault per request; it is not a
cross-vault query engine.

### 4.2 Core schema

**Five primitives (closed structural set):**

| Primitive | Standards alignment | Covers |
|---|---|---|
| `Agent` | PROV-O / CIDOC CRM / FOAF | Humans, organizations, AI systems, roles |
| `Activity` | PROV-O / CIDOC CRM | Meetings, events, decisions-as-acts, actions |
| `InformationObject` | CIDOC CRM / FRBR-LRM | Documents, claims, requirements, notes |
| `Concept` | SKOS | Topics, categories, controlled vocabulary |
| `Place` | CIDOC CRM / Schema.org | Locations |

**Six support node types:**

| Type | Purpose |
|---|---|
| `Document` | File/URL/message carrier and durable source identity. |
| `Identifier` | Typed external identifiers such as QID, ORCID, DOI, ISBN, email, and domain. |
| `Annotation` | A surface occurrence anchored to source content. |
| `Claim` | One S-P-O assertion with provenance anchors. |
| `Block` | Stored byte-anchored source unit. |
| `Finding` | Deterministic detector result value with evidence, severity, and status. |

`SourceSpan` also ships as a frozen value object on Claims and Annotations; it is not a
seventh graph node type. Legacy compatibility classes remain exported separately from this
closed primitive/support model.

The live pack path resolves the built-in `core`, `research`, `sdlc`, and `personal`
registries named by vault configuration. Unknown pack names fail. The May 2026 external
manifest/`kind_of` composition sketch did not become a production plug-in interface, and
the legacy manifest validator is not on the ingest, CLI, or server path. Primitive closure
is therefore structural: adding a primitive requires a reviewed Python code change; a data
pack cannot add one at runtime.

### 4.3 Atomic provenance unit

A `Claim` is the smallest meaningful assertion:

```text
Claim(S, P, exactly one of O_id or O_literal)
  ├─ wasDerivedFrom → Block
  ├─ wasGeneratedBy → ExtractionActivity
  └─ wasAttributedTo → Agent
```

Every Claim carries the three provenance identifiers, confidence, and optional model/prompt
metadata. Its optional `SourceSpan` records a vault-relative source path, byte start/end,
and content hash while the stored `Block` remains the canonical graph anchor. ADR 0003's
future Block-removal cut has not been accepted or implemented.

Claim identities are deterministic on the path that creates them. Deterministic markdown
claims include the Block content hash; resolved extracted relationships use stable semantic
S-P-O identity. Incremental ingest validates byte slices against their content hashes,
re-anchors changed content, and records supersedence rather than silently discarding history.

`Vault.export()` serializes JSON-LD or JSON-LD-star and preserves provenance. Trusty-URI
and nanopublication signing remain future product ideas.

### 4.4 Storage layout and vault isolation

```text
~/.marginalia/
├── marginalia.toml
├── env
└── vaults/
    └── <name>/
        ├── marginalia.yaml
        ├── graph.lbug (+ checkpoint/WAL sidecars)
        ├── notes/
        ├── refs/
        └── .marginalia/
            ├── sources/
            ├── candidate-ledger.jsonl
            └── queue, curation, and maintenance sidecars
```

Markdown and durable source copies are the trust root; the graph is derived. `kg rebuild`
builds a fresh graph and swaps it into place atomically. One application daemon owns a lazy,
immutable runtime per registered vault. Browser tabs, CLI calls, and MCP requests select a
runtime explicitly; each runtime owns its queues, jobs, locks, and sidecars, while `VaultPool`
leases the matching Ladybug handle for the operation. This is request routing, not cross-vault
query fanout. No `MultiVault`, authority-overlay, or catalog-federation API has shipped.

REST, MCP and `/health` share one asyncio event loop, so no handler may do store, sidecar or
YAML I/O on it, and nothing in `okto_neuron.server` uses the default executor. Every blocking
call goes to one of two bounded pools in `server/_store_io.py`, sized in `okto-neuron.toml`:

| Pool | Key (default) | Entry point | Work |
|---|---|---|---|
| StoreExecutor | `[server] store_workers` (4) | `store_io`, `single_flight` | short store, sidecar and YAML I/O for requests and background loops, recall's query embedding |
| JobExecutor | `[server] job_workers` (2) | `job_io` | curation job runners, ingest and remember extraction, answer synthesis, re-embed, provider/model test probes |

Large responses (review queue, graph and node reads, ledger, predicate snapshot, queue and
authority lists, drift and quality reports) are JSON-encoded on the worker, in pieces so the
GIL is released between elements (a single `json.dumps` of a multi-megabyte payload would hold
it for the whole call), and returned as pre-encoded bytes identical to `JSONResponse`.
Single-flight caches those bytes. `single_flight` collapses concurrent identical full scans (upkeep predicates, graph stats,
integrity summary, ledger runs/summary) into one execution. Long jobs never occupy a store
worker, so they cannot starve UI reads; the JobExecutor is also where a separate worker
process will plug in. On shutdown both pools finish calls already executing (bounded by the
drain deadline) before vault handles close, then cancel anything still queued. Two deliberate
exceptions remain: the graph swap at the end of rebuild/heal/reembed jobs runs on the loop as
one no-await block (reads see a short latency blip, never a half-swapped graph), and the
companion-triage LLM fan-out uses its own thread pool inside a job runner.
Model calls are bounded so a wedged endpoint cannot hold a vault's writer lock: a completion
defaults to a 300 s deadline with SDK retries off (retry policy is ours: one retry, backing off
2 s after a timeout), `llm.curation_call_timeout_s` defaults to 600 s and puts every judge call
in the killable helper process, and a curation job watchdog (`curation.job_stall_timeout_s`,
default 900 s) fails a read-only job that reports no progress and shows `curation_job_stalled`,
with `elapsed_s` and `last_progress_at` per job, on `/api/v1/status`. Reconcile-propose keeps
its writer lock across model calls: the lock is what pins one verified graph generation for the
whole pass, and the pass only checks that generation at its start.
`tests/server/test_event_loop_guard.py` fails any route or MCP tool that blocks the loop for
more than 50 ms against a deliberately slow store, and `test_no_default_executor.py` fails on
any new default-executor offload.
Graph reads on grafx retry a transient driver error inside the store adapter: when another
process publishes a commit between grafx's view snapshot and its exact read, the driver raises a
retryable error (`index_view_changed`), and `GrafxStore` retries the read (at most 6 attempts,
jittered backoff from 10 ms to 200 ms, 2 s total) so `get_node`, `get_nodes` and `list_*` never
surface it as a 500. A failure the driver does not flag retryable surfaces at once, and an
exhausted budget reaches the caller as `GraphBackendError` with `retryable=True`.

Node reads take `include_embedding`. `list_nodes` and `get_nodes` default to `False`: the vector column is
not selected at all (grafx and ladybug drop `n.embedding` from the `RETURN`, the neo4j mixin returns a map
projection without it), so a scan of a 4096-dimension vault no longer drags every vector through the engine
and starves the writer. `get_node` defaults to `True` because it is the read-modify-write read and a single
vector is cheap. Callers that use vectors or write a node back pass `True`: vector ranking, reembed, rebuild
and heal copies, snapshot dump, index rebuild and generation stamps, reconcile clustering, resolve similarity,
and the companion's supersede/detach/revert helper. Edges carry no vector. A backend registered through the
`marginalia.graph_backends` entry point must accept the new keyword.

Writes are the other half of that contract. `add_node` on a node whose `embedding` is `None` PRESERVES
the vector already stored for that id (grafx, ladybug and neo4j use an upsert statement without the
`embedding` SET; the in-memory store, `IndexedStore`'s index and the companion planning overlay keep the
existing vector), so a node read without its vector and written back can no longer erase it. Erasing is
explicit: `add_node(node, clear_embedding=True)`. A new node with no vector simply has none, and the
graph generation digest does not change when a vector is preserved. No in-tree caller clears a vector
today: the copy paths (reembed, rebuild, heal, snapshot load) write into fresh stores. A backend
registered through the entry point must honour the same semantics and accept `clear_embedding`.

Every store the daemon serves is an `IndexedStore`, which carries a change counter for derived
projections: `instance_token` (a uuid minted per store object, so a reopen or swap is a new one) and
`write_seq`, bumped once after each completed mutation (`add_node`, `add_edge`, and the backend bulk
writers `add_nodes`, `add_edges`, `wipe` reached through delegation; a write that raised also counts,
because it may have applied partially). A projection built at `(generation, instance_token, write_seq)`
is current exactly while all three still match. `tests/store/test_write_seq.py` enumerates the
`GraphStore` protocol and fails when a member is not classified as read-only or mutating. Predicate
vocabulary, shared-argument pairs, argument signatures and samples are one `PredicateStats` value
(`predicates.build_predicate_stats`), which candidate generation accepts through `stats=` instead of
rescanning the graph.

Grafx's buffer pool defaults to 64 MiB, which thrashes once a full scan's working set (about 165 MiB
on a 179 MB production graph) exceeds it. `GrafxStore` therefore passes `buffer_budget_bytes` to
`okto_grafx.connect`: `storage.buffer_budget` in the vault yaml (bytes or a string such as `256MiB`,
16 MiB to 8 GiB; `defaults.yaml` supplies it when the vault inherits application defaults), else
max(256 MiB, 1.5 x the graph size) capped at 1 GiB. The open is logged at INFO with the graph size,
the chosen budget and its source, and `/api/v1/status` reports it per vault as
`grafx_buffer_budget_bytes`. Neuron stays on grafx exclusive mode; no sharing option is involved.

One process at a time writes a vault. The daemon takes a per-vault writer lease
(`<vault>/.okto-neuron-writer.lock`, an OS file lock that dies with its holder, never deleted;
one JSON line records pid, process start token, role, operation, endpoint and time) for every
vault it serves and keeps it for the life of the process, idle eviction included. A CLI command
that writes (`watch`, `pilot`, `init --wipe`, `kg init` on an existing vault, `kg rebuild`,
`reembed`, `reindex`, `reconcile propose/apply/review confirm/review reject/heal`,
`snapshot dump`, `onboard`, which takes it before the backend-pin check or any default-vault or config write) takes the lease or refuses with exit 5 while the daemon holds it,
naming the daemon pid and the API call that does the same thing, or telling you to stop the
daemon first; it never proxies. `init` on a new path, `vault create` and `snapshot load` take
the lease themselves. Readers (`review list`, `quality *`, `snapshot verify`) take no lease. A
live OS lock is always honoured: a record whose pid or start token does not check out is reported
as an unverifiable holder, and a record left by a dead process never blocks (the lock is free, so
the lease is reclaimed and `writer_lease.stale_reclaimed` is logged). Inside the daemon the lease
is re-entrant, so its own jobs never contend with it. On a filesystem without working file locks
(some NFS or SMB mounts) the lease degrades: `/api/v1/status` reports `writer_lease_degraded`
for that vault and the daemon logs a startup warning naming it, because nothing then stops a
second writer. Lock order is writer lease, then `.graph-handle.lock`, then engine locks.

On the CLI side, every writer goes through one helper (`store/vault_writer.py::vault_writer`)
that takes the lease before the command opens the vault and releases it on exit. A refusal prints
`cannot <operation>: this vault is being written by daemon pid <pid> (serve)`, then either the
API call on the running daemon (`POST /api/v1/reset`, `/api/v1/curation/rebuild`, `reembed`,
`heal`, `/api/v1/reconcile/propose|apply|review/confirm|review/reject`, `PATCH /api/v1/config`)
or `stop the daemon first (okto-neuron stop)`; a CLI holder gets "wait for it to finish". The
per-vault pid check in `kg rebuild`/`reembed`/`reindex`/`snapshot dump` and the `init --wipe`
pid probe are gone: the lease is the only gate. `kg reconcile review list` reads the review queue
and the authority index straight from their JSON files and never opens the graph store, so it
runs while the daemon holds the lease. The `.graph-handle.lock`, `.marginalia/.bootstrap.lock`
and `reembed.state.json` left behind by a failed 0.3.1 `kg reembed` are ignored by the guard.

The daemon takes the lease before it registers a runtime, opens a startup vault or opens a pooled
handle. If a CLI command holds it, the request gets a 409 `vault_busy` naming the holder's pid
and operation, and vault discovery skips that vault (logged once) and retries on its next pass,
so one busy vault never hides the others. The lease is released only at shutdown, after the
stores are closed and before the pid file is removed, and on managed delete just before the
directory is removed (re-acquired if the delete rolls back).

`okto-neuron stop` sends one request and never escalates. The request carries `--timeout` as the
daemon's drain budget (default 30 s); the daemon reserves a further close budget of `max(5 s, 25%
of the drain budget)`, and `stop` waits for both plus a short exit grace. `stop --force` sends the
force request instead. The watcher delivers only the first request and forced ones, so a second
`stop` is not read as the operator's second signal, while a real second SIGTERM or Ctrl-C still
forces. An identity read that is unavailable or differs while the lock is still held is polled
through, never reported as an error. Inside the daemon, when the drain budget runs out the workers
and queued executor calls are cancelled and the stores STILL close inside the close budget, from a
dedicated thread so a wedged executor cannot block it. Every grafx statement, transaction and
health probe runs under an in-flight counter (`store/_inflight.py`), and the close refuses new
calls and waits for running ones. A thread parked in an LLM or network wait holds no grafx call,
so the close goes ahead around it. If a grafx call is still running at the hard deadline (drain
plus close budget), the daemon does not close under it: it logs `store close skipped: N grafx
calls in flight, relying on WAL recovery`, flushes telemetry and logs, and exits; the next open
recovers from the WAL. The order is store close, then writer-lease release, then the pid file.
The daemon leaves its verdict in `.marginalia/server.outcome` (`outcome=closed` right after the store
close completed, `outcome=close_skipped` with the calls in flight and vault names just before the hard
exit; both carry its pid, written atomically). `okto-neuron stop` reads and removes it and exits 0 only
for `closed`. `close_skipped` prints `stopped, but the store close was skipped (N grafx calls in flight);
the next start recovers from the WAL` and exits 3. When the daemon is gone and left no outcome (killed
mid-stop, crashed, or `--force`), a daemon that advertises the `shutdown_outcome` capability in its pid
record gets `stopped, but the daemon left no shutdown outcome (crash or forced exit); the next start
recovers from the WAL` and exit 3. A 0.3.1 daemon never writes the file and does not advertise the
capability (read from the pid record before the stop, since it is gone after), so `stop` keeps exit 0 for
it. An outcome from another pid, a corrupt one, or an unknown value counts as no outcome.
Every phase logs `shutdown.phase name=... duration_ms=... remaining_s=...` (each vault's close
included) and a final `shutdown.summary` line. The ladybug and neo4j adapters do not count their
native calls yet, so they need `store/_inflight.py` before the daemon's clean-close path can
serve them.

Right-to-erasure is available in the application for idle vaults that Marginalia created and
marked as managed. Deletion requires exact-name confirmation, revalidates root membership and
symlink/path safety, fences new leases, drains existing work, releases the owned handle, and
removes only the confirmed vault directory. External, legacy, busy, or unowned vaults are never
deletable through this flow. Installers and updaters preserve every vault directory.

### 4.5 Provider, embedding, and dependency stack

| Layer | Shipped default | Optional alternatives |
|---|---|---|
| Embeddings | `fastembed` with `BAAI/bge-small-en-v1.5`, 384 dimensions | OpenAI-compatible external embeddings or Sentence Transformers |
| LLM pipeline | Per-step `llm.defaults` plus extraction/judge/curator/relation-curator/ask overrides; loopback OpenAI-compatible preset | LiteLLM providers, Bedrock, Anthropic, hosted OpenAI-compatible endpoints, pi CLI, Codex CLI |
| Storage | In-memory for tests; Ladybug for persisted vaults | One Ladybug graph per vault |
| Server | Starlette/Uvicorn plus FastMCP | API-only library/CLI use without server extras |

The base wheel declares only the direct core closure. In v0.0.41, the public `serve` extra composes
the full application boundary from Ladybug,
FastMCP/Starlette/Uvicorn, FastEmbed, and NumPy. Lower-level
optional paths remain explicit extras: `ladybug`, `jsonld` (RDFLib), `embeddings`,
`litellm`, `bedrock`, `sentence-transformers`, and `dev`. The source-development lock remains
exact, while wheel metadata uses compatibility bounds. The release gate executes the base wheel,
both equivalent `serve`/`mcp` live server installs, JSON-LD export, and lower-level runtime extras
independently so a combined environment cannot hide a missing direct dependency.

The `0.0.41` wheel is 784,817 bytes. Its warmed clean-install footprint measured 362,987,647 bytes;
model-server weights are external and
are not bundled into the wheel. Embedding width is recorded in graph metadata and
`kg reembed` performs a vectors-only fresh-graph swap after model/dimension changes.

Provider configuration is owned by `marginalia onboard` and the validated Config API. LLM and
embedding single-key providers share one managed credential endpoint; POSIX stores values in an
owner-only file and Windows stores CurrentUser-DPAPI envelopes. YAML stores only generated
`MARGINALIA_*` environment-variable names, not secrets. Remote model egress is explicit; the
server itself remains loopback-only, with SSH tunnelling as the
supported remote-access path.

**Historical design disposition.** The original GLiNER + Qwen3-4B/Ollama two-stage
pipeline, bundled-model estimate, `embedding_epoch`/old-vector coexistence, `kg
reindex`, and per-call egress-log proposal were not the architecture that shipped.
Extraction is single-stage LLM propose/resolve, GLiNER is not in the production call
chain, and model changes use explicit `kg reembed`.
See [ADR 0001 — Rejected Models](docs/adr/0001-rejected-models.md) for the historical
model-selection record.

### 4.6 Surface APIs

**Python library (synchronous):**

```python
from marginalia import Vault

vault = Vault.open("~/.marginalia/vaults/demo")
document = vault.add("~/.marginalia/vaults/demo/notes/idea.md")
hits = vault.query("what did the team decide about pricing?", k=5)
jsonld = vault.export(format="jsonld")  # requires marginalia[jsonld]
vault.close()
```

`Vault.export()` supports `jsonld` and `jsonld-star`. It is a library method, not a
`kg export` subcommand. `MultiVault` has not shipped.

**CLI:** `marginalia` is the primary entrypoint. `kg` is a compatibility entrypoint that
flattens graph-maintenance commands at its top level.

```console
marginalia onboard
marginalia serve
marginalia add notes/idea.md
marginalia query "what did the team decide about pricing?" --k 10
marginalia ask "what did the team decide about pricing?"
marginalia kg rebuild [VAULT]
marginalia kg reembed [VAULT]
marginalia kg reconcile --help
kg rebuild [VAULT]
kg reembed [VAULT]
kg reconcile --help
```

Other primary commands cover vault management, status/stop, UI, folder watching, pilot,
model inspection, and drift detection. `add`, `query`, `ask`, and most operational commands
are thin clients of the running server. There is no public `cypher`, `snapshot`, or
`drift-report` command.

**MCP:** `marginalia serve` registers exactly five tools:

- `ask(question, k=20, hops=1, ...)` — grounded prose plus citations.
- `explore(topic, node_id, hops=1, k=12)` — structured graph traversal.
- `remember(source, sensitivity="default")` — durable ingest and curation.
- `list_vaults()` — the vault NAMES this server can reach, with `current` and `backend`;
  names only, never paths or ids (ADR 0043 D2).
- `init_vault(name, packs="core")` — create and pool a named vault.

The same server provides the production `/api/v1/*` REST surface and serves the compiled
React application. API writes remain loopback-only; remote use is through a local tunnel.

### 4.7 Ingest pipeline

```text
file or durable raw-text source
  → loader and byte-anchored Block/SourceSpan windows
  → LLM extraction with structured validation
  → durable candidate ledger
  → entity resolution, deduplication, and curation
  → fail-closed quality gate and commit plan
  → Ladybug atomic commit
  → incremental watch and scheduled maintenance
```

The production path is LLM propose/resolve; there is no GLiNER pre-pass. Candidate state is
durable before graph mutation, and retries are idempotent. Markdown and copied source files
remain the recoverable trust root. Rebuild, heal, re-embed, rollback, and curation operations
preserve the fresh-graph/atomic-swap safety boundary.

---

## 5. Anomaly and drift detection

The shipped detector registry is a closed set of three deterministic checks:

| Detector | Current behavior |
|---|---|
| `commitment_temporal_shacl` | Finds overdue commitment documents without referenced closure evidence. Despite the retained identifier, this is frontmatter/date logic, not a pySHACL engine. |
| `supersedence_stale_head` | Finds a decision marked as a non-head when another document supersedes its decision ID. |
| `authority_alias_collision` | Finds an authority alias whose agent name collides with a primary authority. |

Each detector reads the runtime bound to its request or scheduled job and returns `Finding` values. A finding has a
stable ID, detector kind, `info|warn|error` severity, `open|acknowledged|resolved`
status, at least one evidence Block/Document ID, a message, and a detection timestamp.
The shipped detectors currently emit open warnings; the drift endpoint serializes them
but does not persist graph nodes.

Detection is available through `marginalia detect-drift`, the
`POST /api/v1/detect-drift` and compatibility `POST /detect-drift` routes, and the
scheduled curation job. The CLI accepts `on-query`, `on-ingest`, and `background`
mode labels for compatibility; the server runs the configured closed detector set and
reports a `drift.v1` envelope.

There is no pySHACL dependency, contradiction `Finding` detector, versioned
`snapshot`/`drift-report` command, `list-findings` command, or MCP findings tool.
Contradiction matching elsewhere in reconciliation produces correlation candidates,
not drift findings. Those ideas remain possible future product work, not release gates.

---

## 6. Historical v0 exclusions and current disposition

The original list in this section described the May 2026 scaffold boundary. It is not a
current limitation list. LLM extraction, durable consolidation/curation, on-ingest and
background maintenance, REST, and the compiled Web UI all shipped after that baseline. ADR 0034
subsequently replaced browser-token authentication with a direct-loopback local-application
boundary and immutable per-vault runtimes.

The following boundaries are still real:

- Markdown/`.markdown`/text ingest ships; native PDF, HTML, and email loaders do not.
- Markdown and copied sources feed the graph; two-way graph-to-markdown synchronization
  does not ship.
- Per-connection vault selection ships, but cross-vault query fanout, cross-vault writes,
  `MultiVault`, an authority-overlay vault, and catalog federation do not.
- The direct-loopback REST/UI application has strict Host and write-origin checks but no browser
  credential or session cookie. FastMCP keeps an application-scoped bearer token on its separate
  port. Neither surface is a shared, multi-user vault service.
- JSON-LD and JSON-LD-star export ship; trusty-URI and nanopublication signing do not.
- Pulse migration remains a separate future RFC.

These are future product boundaries, not unfinished `0.0.40` release work.

---

## 7. Naming, licensing, repository, and distribution

> **License note (2026-09-25):** releases up to 0.2.0 (published as Marginalia) are
> Apache-2.0, and that grant stays in force for them. From 0.3.0 Okto Neuron is licensed under
> the Elastic License 2.0 with the Okto Labs SaaS, Competing Service, Internal Use, and Branding
> addendum (see `LICENSE`). The Apache-2.0 statements below describe the original decision and are
> kept as written.

- **Name and package:** the product and wheel are `marginalia`. An unrelated project owns
  the PyPI name, so the supported public channel is not PyPI.
- **License:** Apache-2.0.
- **Source repository:** `OktoLabsAI/marginalia` is private.
- **Public distribution:** `OktoLabsAI/marginalia-dist` is public and carries the
  installer, exact wheel, evidence, and GitHub release.
- **Storage dependency:** Ladybug is deliberately optional behind
  `marginalia[ladybug]` and bounded to `>=0.16,<0.17`. Tests and the clean-wheel
  matrix validate extras independently.
- **Current release (as written):** `v0.0.41` was then the public corrective prerelease; it is
  permanently non-promotable, as is `v0.0.40`. Current per-version status is in `docs/roadmap.json`.

---

## 8. Roadmap disposition

| Milestone | Current status |
|---|---|
| `v0.0.1` scaffold | Historical and complete: closed primitives, support schema, in-memory path, initial CLI/MCP, tests, and RFC. |
| `v0.0.37` north-star quality | Complete: extraction/resolve improvements lifted measured extraction recall to 82.3%, and the efficient-hybrid evaluation reached answer parity at 6.24x fewer context tokens. |
| `v0.0.40` immutable prerelease record | Publishing, installer transaction lifecycle, public dist rebake, raw-URL validation, and Linux Docker+tmux evidence are complete and retained for that exact artifact. Later feature-boundary testing invalidated promotion: the recommended server extra is incomplete and dormant legacy MCP code bypasses the production security contract. Keep it prerelease permanently. |
| `v0.0.41` immutable prerelease record | `[serve]` closes the dependency boundary, legacy MCP construction fails closed, the dormant GLiNER path and personalized resources are removed, and the compatibility changes are recorded. Exact source, wheel, distribution, and Linux gates are green. Native Windows testing exposed three daemon-ownership defects in the immutable wheel, so it must never be promoted. |
| `v0.0.42` authorized successor | Assigned source contains the Windows daemon and keyless private-LAN repairs. Publication requires a complete exact-source, artifact, distribution, Linux, and native Windows rerun. |
| Future product work | Tracked in [`docs/roadmap.json`](docs/roadmap.json). Product backlog remains separate from release blockers. |

The original `v0.1.0`/`v0.2.0`/`v0.3.0` forecasts were planning hypotheses, not
the release history. Implemented features moved on the `0.0.x` line; unimplemented ideas
were reclassified into the live product roadmap instead of remaining implicit RFC tasks.

---

## 9. Resolved decisions and future product questions

Resolved in the implementation:

1. FastEmbed `BAAI/bge-small-en-v1.5`, 384 dimensions, is the default embedding path.
2. The CLIs use Click.
3. The runtime MCP surface uses FastMCP and intentionally exposes five tools.
4. Core/research/personal/sdlc packs ship built in.
5. Ladybug remains the persisted property-graph store; in-memory storage supports tests.
6. `Vault.export()` supports both JSON-LD and JSON-LD-star behind the `jsonld` extra.
7. Claim provenance is byte-anchored; `Block` remains stored while `SourceSpan` is
   populated on Claims.

Still-open product questions:

1. Whether vault scale ever justifies a `MultiVault` fanout/catalog layer.
2. Whether the final provenance model should remove stored Blocks in favor of spans only.
3. Whether to add nanopublication/trusty-URI signing.
4. Whether to add bitemporal semantics and contradiction findings.
5. Whether a future package registry channel is worth pursuing alongside GitHub Releases.

None of these product questions caused the release block. The separately documented artifact
dependency/security defects make `0.0.40` ineligible for promotion.

---

## 10. Decision record

Adopted from advisor input, research-lab v1 + v2 rounds, and user direction (2026-05-19):
the original intent is preserved below, with 2026-07-13 implementation amendments where
the shipped architecture diverged.

- **DEC-001:** Green-field, defer Pulse migration. *Why:* lets v0 ship without destabilizing Pulse.
- **DEC-002 (historical design, amended 2026-07-13):** Two-layer schema
  (5 primitives plus domain vocabulary profiles). *Why:* a labels-filed-off Pulse
  schema fails the personal-knowledge use case; a closed primitive set keeps
  cross-domain interop precise. **Implementation amendment:** production loads only
  the fixed built-in `core`/`research`/`personal`/`sdlc` registries. The external
  manifest/`kind_of`/`extends` composition sketch is not a runtime plug-in interface.
- **DEC-003:** Markdown tree is canonical, graph is derived. *Why:* survival of knowledge beyond the tool.
- **DEC-004:** Sync core API, async MCP wrapper. *Why:* CLI users shouldn't need `asyncio.run`.
- **DEC-005 (historical design, amended 2026-07-13):** Local-first default
  (`bge-small` + GLiNER + Qwen3-4B); API opt-in per step per corpus. *Why:*
  consulting-grade confidentiality posture. **Implementation amendment:** the local-first
  and explicit-egress principles stand, but GLiNER is not in the production extraction
  chain. `marginalia onboard` configures provider/model choices, including a loopback
  OpenAI-compatible preset, and per-step overrides own remote-provider selection.
- **DEC-006:** Apache 2.0. *Why:* matches ecosystem; permits any use. *Superseded 2026-09-25 for 0.3.0 and later by ELv2 plus the Okto Labs addendum; 0.2.0 and earlier remain Apache-2.0.*
- **DEC-007:** Librarianship as conceptual spine — specifically Authority control (Entity/Identifier split via `Agent`+`Identifier`), SKOS edge vocabulary, Dublin Core item metadata, Folksonomy-first tagging, and PROV-O provenance. FRBR/BIBFRAME/Topic Maps/CIDOC-CRM inform but are not instantiated in v0.
- **DEC-008 (v2):** Five-primitive closed core (`Agent`, `Activity`, `InformationObject`, `Concept`, `Place`). *Why:* drawn from PROV-O + CIDOC-CRM + SKOS convergence; small enough to memorize, expressive enough for the pilot mappings; closure prevents schema sprawl.
- **DEC-009 (v2):** `Claim` is the atomic unit of provenance, anchored to `Block` via PROV-O edges; RDF-star on wire. *Why:* principled smallest meaningful unit (per user requirement); nanopublication shape without v0 signing cost.
- **DEC-010 (v2 design, partially implemented):** One Ladybug graph per client plus
  opt-in `MultiVault` read-only fan-out. *Why:* satisfy N1 confidentiality through
  independent fences. **Implementation amendment:** one Ladybug graph and immutable
  runtime per vault ship, with client-scoped selection across browser, CLI, and MCP;
  `MultiVault` fanout does not. Guarded managed-vault deletion provides right-to-erasure
  without requiring daemon shutdown or exposing external vaults to deletion.
  **Addendum — 2026-09-04:** ADR 0041 (proposed) makes the graph engine a per-vault
  connector, chosen once at creation and recorded in `marginalia.yaml`, rather than a
  single hardwired implementation. Ladybug stays the default and the shipped one-`.lbug`
  -per-vault shape is unchanged; Okto Grafx and Neo4j are additional selectable
  implementations of the same `GraphStore` protocol, and AWS Neptune is a stretch backend
  behind the same seam. The Markdown vault remains the sole trust root regardless of
  engine — a selectable backend does not make Marginalia graph-canonical the way Graphiti
  is (see the Graphiti/Neo4j distinction above); rebuild-from-vault stays the recovery
  path, not a mechanism for changing backends. See ADR 0041.
  **Addendum — 2026-09-07:** retrieval moved behind a separate `IndexStore` port;
  the graph node embedding is the durable copy and the index is a rebuildable cache
  with a content stamp; scoring is pinned by `tests/store/contract`; ADR 0041 owns the
  backend-selection design.
