# Semantic writer inventory

ADR 0039 Phase 3 exit artifact.

Every path in `src/okto_neuron` that mutates the graph does so through
`GraphStore.add_node` / `add_edge`. This document enumerates all of them and
classifies each as **planner-routed** or **non-semantic infrastructure**, with the
argument for why it is safe.

The inventory is machine-enforced: `tests/store/test_semantic_writer_inventory.py`
AST-scans `src/okto_neuron` for `add_node` / `add_edge` call sites and fails when an
unclassified one appears, when a classified one disappears, or when a classified
site is missing from this document. Call sites are keyed by
`(module, enclosing qualname, method)` — never line numbers.

## What the two classes mean

**Planner-routed.** The write applies a *sealed plan*: adjudication already
happened, was recorded durably in the candidate ledger, and the applier performs no
semantic re-decision. These sites run under the vault write lease and the
generation fence, so a stale or concurrent writer is refused rather than allowed to
half-apply.

**Non-semantic infrastructure.** The write mints no new knowledge. It is either
deterministic scaffolding derived byte-exactly from the vault (the trust root), a
lifecycle facet stamp on an existing node, or a whole-graph copy that preserves
node and edge identity. No LLM is involved, and re-running the path on unchanged
input is a no-op.

Both classes are additionally bounded by the closed-schema write guard
(`okto_neuron.store.closed_set.require_writable_node_type`): no writer, of either
class, can persist a node type outside the 5 primitives + 6 support types (plus the
internal `SchemaMetadata` bookkeeping row).

## Planner-routed writers

| Call site | Safety argument |
| --- | --- |
| `companion/__init__.py` · `_seed_planning_overlay` | Materializes *already-planned* artifacts into the in-memory planning overlay, not the durable store. Replay is LLM-free and the overlay is discarded if the plan is not sealed. |
| `companion/__init__.py` · `_apply_sealed_semantic_plan` | The single durable applier. Applies or resumes one sealed plan "without semantic re-adjudication" — every node and edge it writes was decided and recorded before the lease was taken. Holds the write lease and revalidates the generation fence. |

When a planned node is already on disk under its id, `_apply_sealed_semantic_plan` records
it `already_present` and does not rewrite it (ADR 0039 T4). It first checks that the stored
node is the artifact the plan names by comparing exactly what the id is derived from, in the
form the id derives it: type, the folded title (`exact_surface_key`), and content. A later
mention that differs only in display casing ("turtles" arriving after "Turtles" was stored)
shares the id by design (ADR 0040), so it passes and the stored display title is
kept. A type or content difference under the same id still raises "node artifact differs from
sealed plan". Comparing raw titles here used to reject that re-mention, leave the plan
unreceipted, and fail every later ingest on the vault behind it.

A manual review `commit` (`resolve_review`) applies through the same function and uses the
same check (`_is_stored_node_artifact`). Between parking a candidate and committing it,
another document can commit a node with the same id: same type and content, with its own
provenance, facets, and possibly different display casing. The review commit treats that
stored node as the artifact, acknowledges the queue entry, and leaves the stored node
untouched. Only the pinned node itself is still compared in full against the queued
candidate, so a plan whose pinned artifact disagrees with its queue entry still raises
"manual review node differs from its pinned candidate", and a stored node under the id with
a different type or content still raises "manual review node differs from the sealed
artifact". The whole-node comparison that used to run against the stored node failed on a
node another document had written, left the manual plan unreceipted, and blocked every
later ingest on the vault.

Recovering a vault the old check already wedged: every `remember` on it fails with
"a different sealed semantic plan must be resumed before new ingest work: manual review
<candidate_id>". No file edit or ledger surgery is needed. Re-run the same resolution, with
the same candidate id and the same action, on a build with this fix. It resumes the open
sealed plan instead of sealing a new one, receipts it, and later ingests go through again. A
different action on that id is refused (over REST, a 500 whose detail is "manual review action
differs from the open sealed plan") and changes nothing. Over REST (loopback only, default port `7777`):

```bash
curl -sS -X POST http://127.0.0.1:7777/resolve-review \
  -H 'Content-Type: application/json' \
  -d '{"candidate_id": "<candidate_id from the error>", "action": "commit"}'
```

`/api/v1/resolve-review` takes the same body. From Python it is
`Companion.resolve_review("<candidate_id>", "commit")`. The response outcome is `committed`.
`tests/companion/test_sealed_plan_case_variant.py::test_rerunning_the_review_resolution_heals_a_wedged_vault`
reproduces the wedged state and the recovery.

If the same id was re-proposed to the review queue after the plan was sealed (a new queue
entry with its own provenance and reason), the resumed plan receipts `already_present` and
leaves that new entry queued, so `GET /review-queue` still lists the id after the call above
returns `committed`. That is deliberate. The plan is bound to the one entry it pinned
(`ReviewQueue.resolution_scope`), and ADR 0039 acknowledges a review item only when its own
requested operation completed, so the plan has no authority to close an entry the operator
never resolved, and closing it would drop that entry's evidence unseen. Resolve the new entry
like any other. The node already exists, so `commit` receipts it as a re-mention and
`discard` drops it, and neither touches the graph
(`test_rerun_keeps_a_reproposed_entry_it_did_not_pin`).

## Non-semantic infrastructure writers

| Call site | Safety argument |
| --- | --- |
| `ingest/__init__.py` · `ingest_document` | Deterministic Document/Block/Claim scaffolding derived byte-exactly from the file under the vault. Content-addressed ids make re-ingest of unchanged bytes a no-op. |
| `ingest/__init__.py` · `_add_claim_provenance_edges` | PROV-O edges (`wasDerivedFrom`, `wasGeneratedBy`, `wasAttributedTo`) from a Claim to its Block, extraction Activity, and Agent. Purely structural; endpoints are the nodes just written. |
| `ingest/__init__.py` · `_ensure_system_nodes` | Idempotent seed of the fixed `Agent`/`Activity` provenance pair, guarded by a `get_node` presence check and marked with the infra facet. |
| `companion/_incremental.py` · `_apply_supersede` | Stamps `_superseded` + `valid_until` on an *existing* Claim and writes the `supersedes` edge new→old. Asserts nothing new; only dates what is already there and keeps history walkable. |
| `companion/_incremental.py` · `_apply_detach` | Stamps `_detached` + `valid_as_of` on an existing Claim whose source text is gone. Knowledge is not erased, only marked source-absent and filtered from recall. |
| `companion/_incremental.py` · `resurrect_reverted_claims` | Deterministic REVERT leg: a superseded/detached Claim whose anchoring Block id matches file bytes again has its lifecycle facets stripped. Triggered by content-addressed Block identity, not by a model. |
| `ingest/__init__.py` · `_retire_stale_deterministic_claims` | Stamps `_superseded` + `valid_until` on an *existing* deterministic (`has_tag`/`has_heading`/`links_to`) Claim whose id is absent from the current ingest's fresh claim set. Asserts nothing new; only dates a fact the trust root (the markdown file) no longer carries. |
| `store/reembed.py` · `copy_graph_reembedding` | Whole-graph copy into a fresh store, replacing only embedding vectors. Node and edge ids, types, and payloads are preserved verbatim. |
| `store/reembed.py` · `copy_graph_canonicalizing` | Whole-graph copy applying a precomputed equivalence map and predicate alias folds. Both maps are inputs decided upstream; the copy itself makes no judgement. |
| `migrate/bridge_edges.py` · `ensure_source_mentions` | ADR 0006 D1 structural bridge: mints `Document --schema:mentions--> entity` for nodes carrying a `source_path` facet that resolves to an existing Document. Derived from facets already present; adds no assertion. |
| `store/index/indexed.py` · `IndexedStore.add_node` | M1 write-through wrapper: forwards the node verbatim to the wrapped `GraphStore.add_node`, then upserts the same node into the retrieval index. Mints nothing; the graph write is the one already classified at the caller's own site. |
| `store/index/indexed.py` · `IndexedStore.add_edge` | M1 write-through wrapper: forwards the edge verbatim to the wrapped `GraphStore.add_edge`. Edges never touch the index. Mints nothing beyond what the underlying store call already asserts. |
| `store/snapshot.py` · `load` | M2a snapshot replay: `verify()` must pass first, then every node and edge from a checksummed `nodes.jsonl`/`edges.jsonl` is replayed verbatim in ascending-id order. A backend-agnostic whole-graph copy that mints no new knowledge, identical in kind to the `store/reembed.py` copies above. |

## Excluded from the scan

`store/protocol.py`, `store/memory.py`, and `store/ladybug.py` are skipped: they
*define* `add_node` / `add_edge` rather than call them. The closed-schema guard is
enforced inside the two store implementations, so it covers every site above.

## Adding a writer

1. Decide the class honestly. If a write depends on a model's judgement and is not
   applying a previously sealed plan, it is neither class — it must be routed
   through the planner first.
2. Add the `(module, qualname, method)` key to `CLASSIFIED_WRITERS` in
   `tests/store/test_semantic_writer_inventory.py`.
3. Add a row to the matching table above with its one-line safety argument.
