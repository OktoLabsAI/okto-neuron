# ADR 0041: Pluggable Graph Backend — Selectable Connectors, One Pinned Backend Per Vault

- **Status:** Accepted (2026-09-09) — M1 through M6 complete; M2c and the Neptune stretch remain
 deferred, neither gating
- **Date:** 2026-09-04 (proposed) · 2026-09-09 (accepted)
- **Deciders:** Marginalia maintainers, product owner
- **Builds on:** ADR 0007 (rebuild-from-trust-root), ADR 0034 (multi-vault contract), ADR 0039
 (a backend must satisfy the same integrity contract)
- **Scope:** the storage-and-retrieval seam, a `GraphStore` protocol, a sibling `IndexStore`
 protocol, three gating backends (Ladybug, Okto Grafx, Neo4j), a third-party entry-point group, one
 shared contract suite, a snapshot format. AWS Neptune is stretch, added once the three gate (D-01).
- **Out of scope:** live dual-write, multi-backend fan-out, a hosted/managed service, any schema
 change, migrating a vault between backends (design must not preclude the latter, plan §7).
- **Implementation status:** complete. M1-M6 shipped and gated (plan §5); M2c (a documented
 subtask) and the Stretch AWS Neptune subsection remain deferred and unscheduled, neither part of
 milestone one's exit criteria (D-01). See "Final architecture and parity" below and the companion
 plan's full decision log (plan §11, D-01 through D-90) for the complete record.
- **Amendment (2026-09-09):** a post-M6 owner decision (plan §11, D-94) promotes Okto Grafx from
 M4's experimental, `--accept-experimental`-gated backend to Marginalia's **default**,
 non-experimental graph backend; Ladybug is no longer the default but remains fully supported and
 selectable, and any legacy vault whose `marginalia.yaml` predates a `storage` key still resolves
 to `ladybug`. Status stays **Accepted** — this amends the "Final architecture and parity" section
 below in place rather than reopening the ADR. See `docs/backends/grafx.md`,
 `docs/backends/ladybug.md`, and plan §11 D-94 for the full record.

---

## Purpose

Marginalia's Markdown vault is the trust root for content, but the knowledge graph built from it
is expensive derived knowledge: LLM extraction spends a large number of tokens producing it, and
today that graph can only live in one place, a single Ladybug `.lbug` file per vault. A user who
wants a server-hosted database or Okto Labs' own embedded engine has no path there without a
schema fork.

This ADR makes the graph backend a user-selectable connector, chosen once at vault creation and
recorded in `marginalia.yaml`, while the Markdown content stays the sole trust root regardless of
engine. Rebuild-from-vault stays the recovery path for a damaged graph, not the mechanism for
changing backends. A future `migrate` command (plan §7) is deliberately out of scope but not
precluded by the design (D-06).

Milestone one proves the abstraction on three backends: Ladybug (embedded, file-based), Okto Grafx
(Okto Labs' own embedded engine: pure Python, Kuzu-dialect openCypher, multi-process MVCC, native
vectors, pre-alpha), and Neo4j (server, Bolt, Cypher 5, no DDL floor on Community Edition). AWS
Neptune (cloud, openCypher over HTTPS, no DDL, IAM auth, no local emulator) is stretch, not gating:
the /goal calls it that, and D-01 moves its spike and backend into a Stretch subsection, outside
the exit criteria and definition of done, no AWS resources provisioned until that work starts.
Retrieval moves into a second, independent `IndexStore` connector: today's `search_text`/
`nodes_with_embeddings` are full unfiltered scans baked into `GraphStore` (`query.py:267,501`);
repeating that over Bolt on every `ask` would make a server-backed vault impractical.

## Durable implementation TODO

- [x] Trim `GraphStore`, extract `IndexStore`, wire `upsert()` into every write; gate on an
 ask-only LoCoMo re-run against the 2026-09-03 Ladybug baseline (D-03). — M1, complete 2026-09-07.
- [x] Ship snapshot/staging/rebuild-lock modules (dump/load/verify only, migrate not built, D-06).
 — M2a+M2b, complete 2026-09-07; M2c (a further staging refinement) deferred, not gating.
- [x] Make `StorageConfig` a discriminated union pinned at creation; wire non-loopback database
 egress consent (D-04). — M3, complete 2026-09-08.
- [x] Ship `store/grafx.py`, flagged experimental (D-12), pinned `okto-grafx>=0.0.1,<0.1`; full
 LoCoMo run as exit gate. Ship `store/neo4j.py` against `neo4j:5-community`, same gates, plus a
 CI wall-clock measurement for PR-gating placement (D-15). — M4 complete 2026-09-08, M5 complete
 2026-09-09.
- [x] Land the cross-backend parity gate: three published LoCoMo snapshots, docs, release
 readiness. This is the definition of done. — M6, complete 2026-09-09 (D-90).
- [ ] Stretch, not gating (D-01): the Neptune batching spike, then `store/neptune.py` opt-in in CI,
 once the three gating backends are green and AWS credentials/budget are provided. Deferred,
 unscheduled.

## Decision

### D1. Two ports: `GraphStore` for graph CRUD, `IndexStore` for retrieval

`GraphStore` keeps CRUD, `checkpoint()`, `close()`; gains `health()`, `generation()`, an optional
`snapshot()` context manager; `search_text`/`nodes_with_embeddings` are removed. `IndexStore` owns
`upsert`, `delete`, `search_text`, `scan_vectors` (required, untruncated, its own method so the
suite can assert by shape it never regresses into a top-k), plus optional `search_vector`/
`invalidate_by_facet`. The default `IndexStore` is push-populated with its own small local corpus
store, not a live re-scan, the only design fast enough on a server backend, and a rebuildable
cache: `kg reindex` and rebuild-on-open cover a missing or stale index at zero LLM cost, since
embeddings live on the graph node as the durable copy; ranking is pinned by a fixture corpus, exact
for BM25-parity engines, `recall@20 >= 0.98` for ANN (D-05).

### D2. Generation-scoped reads and writes close the rebuild-collision gap

Every read and write method gains an optional `generation` keyword; `None` means the live
generation. A rebuild's build phase writes into an explicit target generation, so a concurrent
ordinary write can never collide with in-flight staged rows sharing a content-addressed id;
combined with a compound `(id, _generation)` merge key on Neo4j (D5), collision becomes impossible,
not merely unlikely. `store/closed_set.py`'s already-backend-agnostic `CLOSED_NODE_TYPES` and
`require_writable_node_type()` gain a sibling `require_writable_edge_identity()`; every adapter's
`add_node`/`add_edge` calls both first, since Neo4j CE has no existence/type DDL.

### D3. Backend pinned at creation; non-loopback database endpoints get LLM-grade egress consent

`StorageConfig` becomes a discriminated union of `LadybugStorageConfig | GrafxStorageConfig |
Neo4jStorageConfig | NeptuneStorageConfig` (Neptune inert until Stretch, D-01), each
`extra="forbid"`; `LadybugStorageConfig` keeps the exact `backend`/`reason` fields already emitted,
so no existing vault is rejected. A graph database receives the whole extracted knowledge graph, at
least as sensitive as prompts sent to an LLM, so a non-loopback `storage.uri` gets the identical
consent UX as remote LLM endpoints: an interactive confirmation, or `allow_remote: true` plus
`--allow-remote-db --yes` non-interactively, via a sibling `_classify_storage_endpoint` validator,
not the LLM path itself but with identical semantics; credentials only as `MARGINALIA_*` env-var
names (D-04).

### D4. Third-party backends register through a Python entry-point group

`pyproject.toml` registers `marginalia.graph_backends`/`marginalia.index_backends`. Lookup mirrors
SQLAlchemy's `PluginLoader`: an official dict tried first, falling through to entry points, with
eager `isinstance` plus signature validation so a broken backend fails at registration, not
mid-write; every backend passes the same contract suite.

### D5. Per-backend mapping: Grafx first, then Neo4j, then Neptune as stretch

Ladybug keeps its physical file-swap, folded into `staging.py`. Okto Grafx (built first, D-02: it
shares Ladybug's Kuzu dialect, is the cheaper second backend, and Okto Labs can fix engine-side
gaps) and Neo4j both use the **same logical mechanism**: a `_generation` tag plus a singleton
pointer row, via a shared mixin implementing every read method's generation filter once, never a
filesystem rename against Grafx's exclusively-managed directory, never a "second database" relabel
Neo4j CE cannot do. Neo4j adds a compound `(id, _generation)` MERGE key
backed by a CE-available uniqueness constraint. Grafx's `GrafxWriteConflict` retry defaults to 8
attempts, full jitter from 50ms, capped 2s/30s (D-10); `checkpoint()` sits behind a
`supports_checkpoint` flag, a no-op until verified against `okto-grafx`'s own contract (D-16); its
experimental status is a hard gate covering both the format warning and the license disclosure
below (D-12). `embedding` stays an ordinary node property everywhere. Neptune (Stretch, D-01)
stores `id` as an ordinary property, never on Neptune's reserved `~id`, uses signed
openCypher-over-HTTPS rather than Bolt to avoid a documented pooled-connection defect; its
batched-write path is unverified, gated by a spike, S3 bulk-load as fallback, built once Stretch
begins.

### D6. Durability: a Logical Graph Snapshot, generic over `GraphStore`; migrate documented, not built

`store/snapshot.py` provides `dump`/`load`/`verify`, calling only `list_nodes`/`list_edges`/
`add_node`/`add_edge` plus `snapshot()` when available, zero per-backend code: a JSON manifest,
plain-JSON node/edge JSONL, a skippable embeddings file, a checksum file. `dump()` pins one
consistent read point so a concurrent rebuild swap never describes a state that never existed, the
safety net for the pre-alpha Grafx engine and the interchange format a future `migrate` needs.
Milestone one ships dump/load/verify; `migrate` itself stays documented only (D-06).

### D7. Licensing is resolved explicitly, not left implicit

Okto Grafx ships under the Elastic License 2.0 plus an Okto Labs SaaS/Branding Addendum, not
Apache-2.0. The Addendum frames Grafx as "an EMBEDDED LIBRARY, no server, no network service,"
restricting only reselling Grafx itself, not building on top of it; `marginalia[grafx]` as an
in-tree extra is the permitted case (D-14), disclosed at the same hard gate as the experimental
warning (D-12), flagged for legal review as a note, not a blocker. AWS Neptune's contract-suite run
needs no narrower reading either: it is Stretch (D-01), so any credentialed AWS run happens only
once that starts.

### D8. Roadmap, docs, and release location

The ADR lives at `docs/adr/0041-pluggable-graph-backend.md`, registered in `THEMES`; its
implementation plan is kept internally. There is also one `core`-track
`roadmap.json` item, status "now" (D-07, D-08).

## Implementation plan and gates

Phases run in order; M1 lands before any backend-specific phase needs `ask` working end to end.
Every gating milestone's LoCoMo run is a shepherd-run exit gate on the private LAN endpoint with
qwen3.8-27b, never a CI job (D-03, D-17).

- **M1: Port split.** Gate: an ask-only LoCoMo re-run reusing the 2026-09-03 vaults, index
 reindexed from the graph, within noise.
- **M2: Snapshot, staging, fenced rebuild lock.** Replaces three duplicate implementations;
 dump/load/verify ship, `migrate` does not (D-06).
- **M3: Config, registry, capability flags, health/error surfaces**, egress consent for
 non-loopback database endpoints (D-04).
- **M4: Okto Grafx end to end**, flagged experimental (D-12); gate: contract suite, acceptance
 harness, a full LoCoMo run.
- **M5: Neo4j end to end**, CI-tested against `neo4j:5-community`, same gates, plus a wall-clock
 measurement deciding PR-gating placement (D-15).
- **M6: Cross-backend parity gate, docs, release readiness (the definition of done).** Three
 published LoCoMo snapshots side by side; PR-gating CI covers Ladybug+Grafx (+Neo4j if D-15's met).
- **Stretch: AWS Neptune, not gating (D-01).** Batching spike then backend, opt-in in CI, once the
 three gating backends are green and AWS resources are provisioned.

## Risks

The plan's risk register (13 entries) is authoritative. Four stand out: logical generation-tagging
needs GC and snapshot-consistent pagination (mitigated by one shared mixin implementing every read
filter once, plus an explicit, operator-only `gc-generations`, D-09); Grafx is pre-alpha with no
format guarantee (mitigated by the hard gate D-12, a pinned version range, the snapshot as safety
net); Neo4j CE's read-committed isolation allows concurrent-write duplicates without a uniqueness
constraint (mitigated by the composite `(id, _generation)` constraint, D2/D5); Neptune's
batched-write design rests on unverified HTTPS batching, but no longer needs mitigating inside
milestone one, since Neptune is Stretch (D-01).

## Rejected alternatives

Four proposals were judged. **Query IR** scored lowest: it never builds the goal-mandated
index-store connector; its frozen-shape idea survives as the rationale for `scan_vectors` being
its own required method. **Hexagonal Ports** (seven named ports) was thorough, its capability-flag
and retry-normalization ideas are adopted, but its staging design assumes an atomic relabel Neo4j
CE cannot do, and it ships an unused `BulkPort` for a named non-goal. **Narrow Typed Protocol**
scored highest among the non-winners; its two-port split and closed_set reuse are adopted, but it
defers a durable interchange format to unbuilt follow-on work, a real gap against the owner's
framing that the graph "must keep [it] durable." **Snapshot-first**, the winner, ships a working
checksummed format now instead.

## Final architecture and parity (2026-09-09, M6 close-out)

Three gating backends shipped behind the identical `GraphStore`/`IndexStore` port pair (D1), each
pinned per vault at creation and enforced on every re-open (`VaultBackendMismatch`, D3): **Okto
Grafx** (`store/grafx.py`, embedded, multi-process MVCC, pre-alpha on-disk format, **default as of
the post-M6 owner decision, D-94** — no `--accept-experimental` required; originally shipped
experimental under D-12, which D-94 retires), **Ladybug** (`store/ladybug.py`, embedded,
file-based, no experimental flag, the backend for any legacy vault whose `marginalia.yaml` predates
a `storage` key), and **Neo4j** (`store/neo4j.py`, server, Bolt/Cypher 5, not experimental,
`vault_id`-scoped since D-83). All three implement the same logical-generation staging shape (D2/D5) and pass one shared
contract suite (`tests/store/contract/`). Per-backend usage, config, and the local Neo4j test
container recipe are documented in `docs/backends/ladybug.md`, `docs/backends/grafx.md`, and
`docs/backends/neo4j.md`; each of `grafx` and `neo4j` also pins the default `IndexStore` engine's
`ladybug` dependency so either extra installs and runs standalone (D-88).

**Five-surface parity (CLI, MCP, REST/UI, SDK) is proven identical across all three backends** by
acceptance scenario 85 (D-89): equal node/edge counts, equal ranked `document_id` order on CLI/
SDK/REST, MCP `explore` reaching the same top hit, and REST curation rebuild/rollback returning a
real `202` + generation flip on Grafx and Neo4j (Ladybug's rollback is the one documented,
model-free-testing-only exception: it needs an LLM-published semantic-materialization receipt this
scenario's `--disable-llm` constraint cannot produce, D-89).

**LoCoMo quality parity** (plan §5, M6 Parity; full detail and the pairwise-delta table there) holds
within judge noise across all three backends and the pre-port baseline — pooled J within 2 points of
each other, the one higher-variance metric (category 1) confirmed by an independent second sample
(D-81/D-82) rather than left as a single-run outlier. Four redacted snapshots are kept side by side
in the private benchmark harness: the Ladybug parity run, the full Grafx run, the full Neo4j run, and
the Neo4j category-1 rerun.

CI enforces this on every change: `model-free-tests.yml` runs the contract suite against Ladybug and
Grafx on every PR; `release-artifact-gate.yml`'s `neo4j-backend-gate` job adds Neo4j (against a
service-container database) before every tag (D-15, D-73, D-79, D-87). A throwaway third-party
backend package proves the entry-point seam (`marginalia.graph_backends`, D4) with zero in-tree
changes.

Deferred, not gating: M2c (a further staging refinement) and the Stretch AWS Neptune subsection
(batching spike, then `store/neptune.py`, opt-in, credentialed) — both remain unscheduled future
work, explicitly outside milestone one's exit criteria (D-01).

## Exit criteria — met (2026-09-09)

Milestone one is complete: PR CI runs the contract suite against Ladybug and Grafx, joined by
Neo4j on the pre-tag gate; CLI, MCP, REST/UI, SDK behave identically across the three gating
backends (scenario 85, D-89); a throwaway third-party backend passes the contract suite via
`pip install` with zero in-tree changes; LoCoMo scores are recorded for all three, equal within
noise of each other and the 2026-09-03 baseline, published as redacted snapshots side by side
(plan §5 M6 Parity); this ADR is registered in `THEMES` and the generated docs site. AWS Neptune
stays outside these criteria and the definition of done (D-01), remaining unscheduled future work
through the same entry-point path.

## Decisions delegated to the shepherd

The product owner delegated every open question in this ADR's earlier draft to the shepherd on
2026-09-04, under full autonomy, with instructions to answer and log each one. Every `D-NN`
decision cited above (D-01 through D-17) is recorded in full, with rationale and reversal, in the
companion plan's decision log (plan §11), authoritative for why scope changed from four gating
backends to three, Neptune moved to stretch. The same log grew to D-90 across implementation
(M1-M6), covering every defect found and fixed along the way (staging data-loss D-80, vault-id
portability D-83, rollback parity D-84, docs-gate/scenario coverage D-85/D-86, release-skill
coverage D-87, extra self-sufficiency D-88, five-surface parity D-89) and the milestone's own
close-out (D-90).
