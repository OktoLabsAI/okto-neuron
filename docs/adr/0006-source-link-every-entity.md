# ADR 0006: Source-Link Every Entity at Ingest — Node-Derived Mentions

**Status:** Accepted
**Date:** 2026-06-04
**Deciders:** Alex Rivera, Marginalia core
**Supersedes:** None
**Superseded by:** None
**Amends:** ADR 0005 (realizes its F4 `schema:mentions` safety net, which was specified but never wired live)

---

## Context

ADR 0005 bridged Claims to their entities (`rdf:subject`/`rdf:object`) and named a
`schema:mentions` (Document → entity) edge as a de-orphaning safety net (F4). After
shipping it, **302 degree-0 orphan entities remained**. Research note
`research/15-orphan-entity-handling-2026-06-03.md` measured them against the live
graph: **301 of 302 are mention-only genuine entities** that carry a `source_path`
facet resolving to an existing Document node, but never received a `schema:mentions`
edge.

Root-cause investigation (file:line evidence below) found the single defect:

- **`schema:mentions` is never minted in any LIVE ingest path.** It exists only in
  the backfill migration (`src/marginalia/migrate/bridge_edges.py:114-124`), and that
  migration is **claim-derived** — it iterates `list_nodes(type="Claim")`
  (`bridge_edges.py:95`). An entity that participates in no committed Claim is
  unreachable by the only code that mints mentions. **The live gap and the heal gap
  are the same bug.**
- The earlier premise that "the companion `remember()` path creates no Document node"
  is **false**. `remember()` calls `self._vault.add(source)`
  (`companion/__init__.py:276`) → `Vault.add` (`vault.py:149-167`) →
  `ingest_document` (`ingest/__init__.py:33-46`), which **does** create the Document
  node. Every committed entity also carries a `source_path` facet
  (`companion/__init__.py:328,338`; `consolidate/_candidates.py:53-60`;
  `_BlockAnchor.source_path` = `str(Path(source).resolve())` at `companion:626,646`).
- **The only path that births standalone entity nodes is `Companion.remember()`**.
  The current executable-plan compiler in
  [`src/marginalia/companion/__init__.py`](../../src/marginalia/companion/__init__.py)
  plans both node creation and `ensure_source_mention` operations after the gate;
  this is the implementation that superseded the former `ConsolidationSession`.
  Before this decision was implemented, the only follow-up was relationship-Claim
  minting rather than a source link for standalone entities. An
  entity that clears the gate but appears in no committed edge/Claim gets **degree 0**.
- The deterministic ingest path (`ingest_document`) births **no** standalone entities
  (only Document/Block/Claim nodes; its claims use `subject_id=document_id` +
  literal objects) — so it cannot orphan an entity.

The goal (carried from ADR 0005 / note 15): not "one giant component," but **no
entity-typed node that has a resolvable source is left unreachable** — fixed at the
root so it cannot recur, with the existing vault healed by the *same* mechanism.

## Decision

**Make `schema:mentions` node-derived and mint it both live (at the commit
chokepoint) and during heal, through one shared helper. Pure edge addition — no
schema change, no new primitive/support type.**

| # | Decision | Rationale |
|---|---|---|
| D1 | **`schema:mentions` becomes node-derived, via one reusable helper** `ensure_source_mentions(store, node_ids: Iterable[str] \| None)` (None = whole store). For each **primitive-entity-typed** node carrying a `source_path` that resolves to an existing Document, mint `Document --schema:mentions--> entity`. Idempotent on `(src, type, dst)`; type-allowlisted to the 5 primitives. | The migration's claim-derived loop (`bridge_edges.py:95`) structurally cannot reach entities in no Claim. Iterating entity nodes by `source_path` does. One helper = the live mint and the heal are literally the same code, not a parallel patch. |
| D2 | **Mint live at the commit chokepoint:** call `ensure_source_mentions(store, committed_node_ids)` in `remember()` immediately after `_mint_relationship_claims` (`companion/__init__.py:475`). | `committed_node_ids` (`:458-465`) is exactly the set this `remember()` landed. This guarantees every newly-committed entity is source-linked at creation, in the only path that creates entities. |
| D3 | **Deterministic ingest gets no new mint site.** State explicitly: `ingest_document` births no standalone entities, so the helper is a no-op there and the invariant holds trivially. | Do not invent a mint site to satisfy a symmetrical-looking "both paths" requirement; there is nothing to link. |
| D4 | **Heal the existing vault with the SAME `kg migrate bridge-edges`**, now carrying the node-derived mention pass (D1). | Identical code path to the live mint (`ensure_source_mentions`), not a one-off. Reaches all 301 mention-only orphans the claim-derived version missed. |
| D5 | **Pin the path-absoluteness invariant.** The `source_path` facet on entities MUST be the absolute resolved path, byte-identical to the Document-id input (`sha256("document", str(p.resolve()))`, `markdown.py:190`; resolver `bridge_edges.py:54-59`). | The mint works only because entity facet, Document id, and resolver agree on the absolute path. If any site ever stores `source_path` vault-relative, the resolver mints nothing and **fails silently** (cf. the gold-span all-zeros class of bug). |
| D6 | **Model-free regression guard.** A test (InMemoryStore, no LLM) asserting: every primitive-entity node carrying a `source_path` that resolves to an existing Document has degree ≥ 1 after `ensure_source_mentions`. Gate on **sourced-orphan count, not component count.** | Prevents recurrence regardless of future call sites — the guard, not the call-site choice, is what makes this future-proof. Must assert real Document resolution (catches D5 drift). Model-free per the "no reflexive full pytest" rule. |
| D7 | **Define the orphan-legitimate residue.** The invariant is scoped to entity-typed nodes whose `source_path` resolves to a Document. It EXCLUDES Documents/Blocks (support types), zero-extraction sources (e.g. a file that produced no entities), and genuinely standalone entities. Zero orphans is **not** the target. | Forcing an edge onto a source that yielded nothing fabricates connectivity the vault doesn't support — violates "markdown is the trust root." A small explained residue is correct. |

### Compatibility with prior decisions

- **ADR 0005:** this realizes F4 (`schema:mentions`) — specified there, wired here.
- **ADR 0004 (entity resolution):** unaffected. Mentions are source links, not merges;
  no threshold change, no auto-merge. Cross-type / same-type duplicate collisions
  (note 15 bucket d) remain a separate follow-up (an upstream extractor
  type-assignment concern, not something a downstream merge should mask).
- **ADR 0002 (good-enough provenance):** held. Mention edges add no cryptographic
  machinery.

## Consequences

**Positive**
- `remember()` can no longer orphan a sourced entity — the defect is closed at the
  root, in the one path that creates entities.
- The existing vault heals via the same command/helper; ~301 mention-only orphans
  connect to their source Documents.
- D6 makes regression detectable without an LLM.

**Costs / risks**
- **R1 — silent no-op on path drift (D5).** Mitigated by the ADR pin + D6 asserting
  real Document resolution.
- **R2 — type allowlist.** `ensure_source_mentions` must mint only for the 5 primitive
  entity types, never `Document→Document` / `Document→Block`. (The migration already
  guards `entity_id == document_id`, `bridge_edges.py:121`; extend to an allowlist.)
- **R3 — single-writer.** The heal migration must run against a vault no server holds
  open (already in the CLI help).
- **R4 — call-site layering.** D2 mints in `remember()`, not the generic
  `ConsolidationSession.commit()`. Accepted: the session is a storage transaction with
  no `source_path`→Document semantics; D6 is the real enforcement, and the helper is
  promotable to a post-commit hook if a third entity-creating surface ever appears.

## Open questions (not blockers)

- **Same-type duplicate orphans** (note 15 bucket d, a handful) — defer to a
  conservative ADR-0004 resolution pass; not in this ADR's scope.
- **Cross-type name collisions** (Place "NX" vs Agent "NX") — never auto-merge; route
  to review. Likely an upstream extractor type-assignment fix, tracked separately.
- **Noise nodes** (note 15 bucket b, 2) — heal by `kg rebuild` from the vault, not by
  graph mutation.
