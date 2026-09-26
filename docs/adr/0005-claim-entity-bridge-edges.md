# ADR 0005: Bridge Claims to Their Subject/Object — Reify-and-Link

**Status:** Accepted
**Date:** 2026-06-03
**Deciders:** Alex Rivera, Marginalia core
**Supersedes:** None
**Superseded by:** None
**Amends:** None

---

## Context

The new full-graph visualization surfaced a structural defect. Pulled live from a
real 2,324-node vault (`/api/v1/graph?limit=5000`), the graph is effectively
**two disjoint graphs**:

- **Semantic layer** — direct entity→entity predicate edges (`NX -owns-> AWS`):
  one connected component of **311 entity nodes**, with **zero Claims** in it.
- **Provenance layer** — each of the **1,489 Claims** links *only* to its source
  `Block` + extraction `Activity`/`Agent` (PROV-O edges), forming ~1,489
  disconnected "Block + its Claims" flower islands.
- **395 fully orphaned (degree-0) entities** — real domain entities (177 Concept,
  125 InformationObject, 41 Agent, 19 Activity, 17 Place, **all 15 Documents**).

Root cause in code: a `Claim` reifies an S-P-O assertion but stores its subject
and object as **inert string facets** (`S_id`, `O_id`) with no edge to them.

- `src/marginalia/companion/__init__.py:849-851` writes `S_id`/`P`/`O_id` as Claim
  data fields.
- `src/marginalia/companion/__init__.py:715-728` (`_add_claim_provenance_edges`)
  mints **only** the three PROV-O edges — never an edge to `S_id`/`O_id`.
- The semantic entity→entity edge was minted **separately** by the former
  consolidation session. Its current owner is `_plan_relationship_claims` in
  [`src/marginalia/companion/__init__.py`](../../src/marginalia/companion/__init__.py):
  entity objects receive a topology edge while literal-object assertions do
  not. The deterministic ingest path mirrors this
  (`src/marginalia/ingest/__init__.py:105-119`).

This is the failure mode reification exists to avoid: we paid reification's full
cost (a node per fact) and got none of its benefit (navigability). Classic RDF
reification's `rdf:subject`/`rdf:object` **are edges to the referents**
([W3C](https://www.w3.org/wiki/RdfReification)); comparable agent-memory KGs
(Zep/Graphiti) link every source episode to the entities it references via
`MENTIONS` edges ([arXiv:2501.13956](https://arxiv.org/html/2501.13956v1)). The
literature-backed analysis is `research/14-claim-entity-bridge-graph-connectivity-2026-06-03.md`.

The **goal is not "one giant component."** Disconnected components are expected
for unrelated topics. We optimize two things: (1) **fact→evidence navigability**
(traverse from an assertion to its source and back) and (2) **entity
discoverability** (real entities should be reachable). The smell is *degree-0
nodes that should have edges* — not the component count.

## Decision

**Make the Claim the bridge between the semantic and provenance layers by minting
edges from each Claim to the entities it asserts, using canonical reification
vocabulary. Pure edge addition — no schema change, no new primitive, no new
support type.**

| # | Decision | Rationale |
|---|---|---|
| F1 | **Mint `rdf:subject` (Claim → subject entity) ALWAYS**, including literal-object claims (those with `O_literal`). | The subject is always an entity. This single edge rescues the largest orphan bucket: subjects whose only assertions had literal objects (e.g. "SOW v0.2 status approved") and so were never edged. |
| F2 | **Mint `rdf:object` (Claim → object entity) only when the object is an entity** (`O_id` present, not a literal). | Literals stay as the `O_literal` attribute. Do not fabricate object nodes for literal values — property-graph best practice keeps literals as attributes ([Neo4j](https://neo4j.com/blog/knowledge-graph/rdf-vs-property-graphs-knowledge-graphs/)). |
| F3 | **Keep PROV-O edges and the direct entity→entity edge unchanged.** Accept mild redundancy (a fact is now reachable both via its Claim and via the direct `S→O` edge). | Direct edge = cheap semantic traversal; Claim edges = evidence-linked traversal. Removing either loses a real access path. |
| F4 | **Mint `schema:mentions` (Document → entity) at extraction time** as a de-orphaning safety net. | Covers the bucket reification can't reach: entities (incl. the 15 isolated Documents) mentioned in a source but in no committed entity-object Claim. The canonical schema.org `mentions`, the Graphiti `MENTIONS` analogue. |
| F5 | **Backfill existing vaults via an idempotent migration** that re-derives F1/F2 edges from the `S_id`/`O_id` facets already stored on every committed Claim. No re-ingest, no LLM. | The 1,489 Claims already carry the ids — the edges are pure derivation. Existing vaults (the NX vault) are valued and must connect without rebuild ([[project_dev_state_break_compat]]). |
| F6 | **Idempotent minting keyed on `(src, type, dst)`.** Re-running ingest or the migration never duplicates an edge. | Backfill + live ingest must converge to the same graph; re-runs must be safe. |
| F7 | **Canonical vocabulary only:** `rdf:subject`, `rdf:object` for the bridge; `schema:mentions` for the source link; `prov:*` unchanged. No coined predicate. | Consistent with the existing namespaced predicates in code (`prov:`, `schema:`, `skos:`) and the standards-alignment rule in CLAUDE.md. |

### Compatibility with prior decisions

- **ADR 0003 (provenance is a span, not a node):** untouched. Byte-span anchoring
  of Claims to Blocks is unchanged; F1/F2 add *outgoing semantic* edges, not a new
  provenance mechanism.
- **ADR 0002 (provenance = good-enough citation + drift detection, NOT
  tamper-evidence):** held. Navigation edges add no cryptographic machinery.
- This **realizes** intent already written but never delivered: ADR 0003 calls the
  product "the Claims that **bind** them"; `RFC.md:172` calls a Claim "a rendering
  of an RDF-star quoted triple." The code bound nothing; F1–F4 make it true.

## Consequences

**Positive**
- The Claim becomes the hub fusing the two graphs: up to evidence (PROV-O),
  across to entities (`rdf:subject`/`rdf:object`). Flower islands fold into the
  entity cloud.
- The largest orphan bucket (literal-object subjects) connects for free via F1.
- Documents and remaining mention-only entities de-orphan via F4.
- Fact→evidence becomes navigable in both directions (entity → its asserting
  Claim → source Block).

**Costs / risks**
- **Edge-count growth.** Up to ~2× Claim count in new bridge edges (1 `rdf:subject`
  always + 1 `rdf:object` when the object is an entity), plus `schema:mentions`.
  Bounded and linear; the graph store already holds 5k+ edges.
- **Redundancy (F3).** A relationship is reachable two ways. Accepted; a single
  canonical traversal could be chosen later without data loss.
- **Visualization density.** Bridged flowers thicken the connected component. The
  Graph view's existing cap/filter/LOD controls absorb this; no view change is
  required for correctness.

## Open questions (not blockers)

- **True noise orphans.** Entities in no claim and unreachable by `schema:mentions`
  (dead-lettered unresolved refs) remain degree-0. Pruning vs. authority-linking
  (`owl:sameAs`, SKOS `exactMatch`) is deferred to an entity-resolution follow-up;
  do not promise zero orphans.
- **Predicate node for `P`.** `rdf:predicate` (Claim → a node for the predicate
  term) is intentionally **not** minted in F1–F4; the predicate stays the `P`
  attribute. Revisit only if predicate-as-entity navigation is needed.
