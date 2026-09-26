"""Deterministic semantic-quality evaluation over one complete graph store.

The evaluator owns metric definitions; HTTP, CLI, scripts, and rebuild gates are
projections of this report rather than independent implementations.  It is
measurement-only: no graph row, authority record, or predicate mapping is
changed here.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any, Final

from okto_neuron._internal.infra import is_infra, is_superseded
from okto_neuron.consolidate.ledger import LedgerScanResult
from okto_neuron.primitives import PRIMITIVE_NAMES
from okto_neuron.semantic_adjudication import (
    evaluate_ledger_adjudication,
    evidence_identity,
)
from okto_neuron.semantic_surface import (
    NORMALIZER_VERSION,
    discovery_surface_key,
    exact_surface_key,
    surface_normalization_flags,
)
from okto_neuron.store.protocol import GraphStore

SEMANTIC_QUALITY_SCHEMA: Final = "semantic_quality.v1"
SEMANTIC_SNAPSHOT_SCHEMA: Final = "semantic_snapshot.v1"
SEMANTIC_MEMBER_ENCODING: Final = "sha256(kind + NUL + canonical_semantic_value)"
SEMANTIC_CHURN_SCHEMA: Final = "semantic_churn.v1"
# These are the graph-local hard invariants that the current evaluator can
# completely measure on an isolated rebuild candidate.  Human-labelled quality
# metrics and recall-cost samples remain acceptance evidence, not graph-swap
# predicates, until their thresholds are explicitly registered by a later ADR
# revision.
SEMANTIC_REBUILD_GATE_VERSION: Final = "semantic_rebuild_gate.v1"
SEMANTIC_REBUILD_GATE_CODES: Final[tuple[str, ...]] = (
    "complete_store_scan",
    "closed_primitive_set",
    "claim_object_shape",
    "placeholder_predicates",
    "claim_source_anchors",
    "claim_bridge_materialization",
    "claim_provenance_materialization",
    "live_relation_endpoints",
    "relation_materialization",
    "topology_claim_coverage",
    "registered_predicates",
)
# Deterministic markdown/support assertions are owned by the ingest schema, not
# by the governed semantic predicate lifecycle. Their subjects are support
# types (for example Document), which the registry deliberately does not admit.
_STRUCTURAL_INGEST_PREDICATES: Final = frozenset({"has_heading", "has_tag", "links_to"})
SUPPORT_NODE_TYPES: Final[frozenset[str]] = frozenset(
    {"Document", "Identifier", "Annotation", "Claim", "Block", "Finding"}
)
PLACEHOLDER_PREDICATES: Final[frozenset[str]] = frozenset(
    {"", "unknown", "none", "null", "n/a", "na", "undefined"}
)
INTERNAL_NODE_TYPES: Final[frozenset[str]] = frozenset({"SchemaMetadata"})

_CONTENT_HASH = re.compile(r"^[0-9a-f]{64}$")
_SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


def evaluate_store(
    store: GraphStore,
    *,
    integrity: Mapping[str, Any] | None = None,
    registered_predicates: Iterable[str] | None = None,
    recall_samples: Iterable[Mapping[str, Any]] | None = None,
    config_fingerprint: str | None = None,
    extraction_fingerprint: str | None = None,
    semantic_policy_fingerprint: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Evaluate one complete store scan and return the versioned quality report.

    ``integrity`` must be the ADR 0039 verdict for the same open generation.
    Predicate registration stays explicitly unmeasured until ADR 0040 Phase 3
    supplies the registry; absence never silently passes the invariant.
    ``recall_samples`` accepts complete ``recall_cost.v1`` mappings captured by
    ordinary recall; missing or partial accounting remains ``not_measured``.
    """

    integrity_payload = dict(integrity or {})
    registered = _materialize_registered_predicates(registered_predicates)
    recall_rows = None if recall_samples is None else tuple(recall_samples)
    fingerprints = _semantic_snapshot_fingerprints(
        config=config_fingerprint,
        extraction=extraction_fingerprint,
        semantic_policy=semantic_policy_fingerprint,
    )
    store_generation = getattr(getattr(store, "_graph_handle", None), "graph_generation", None)
    technical_integrity_verified = _integrity_is_freshly_verified(
        integrity_payload,
        store_generation=store_generation,
    )
    nodes = list(store.list_nodes())
    # Ladybug's bulk edge projection is not semantic evidence when the physical
    # adjacency audit failed (ADRs 0007/0010).  Do not reinterpret or reader-heal
    # it here: preserve the trustworthy node/facet measurements and explicitly
    # leave topology-dependent metrics unmeasured.
    edges = list(store.list_edges()) if technical_integrity_verified else []
    report = _evaluate_graph(
        nodes,
        edges,
        integrity=integrity_payload,
        technical_integrity_verified=technical_integrity_verified,
        registered_predicates=registered,
        recall_samples=recall_rows,
        fingerprints=fingerprints,
        sample_limit=sample_limit,
    )
    report["evidence"]["external_inputs"] = _external_input_evidence(
        registered_predicates=registered,
        recall_samples=recall_rows,
        adjudication=None,
    )
    report["rebuild_gate"] = evaluate_rebuild_gate(report)
    return report


def evaluate_rebuild_gate(report: Mapping[str, Any]) -> dict[str, Any]:
    """Project the explicitly registered, fully measurable rebuild invariants.

    Missing, duplicate, or unmeasured checks fail closed.  This deliberately
    does not reinterpret the report's broader ``incomplete`` verdict: B3,
    human relation judgements, recall samples, and statistical thresholds are
    separate acceptance lanes and cannot be invented during a graph swap.
    """

    hard_invariants = report.get("hard_invariants")
    raw_checks = hard_invariants.get("checks") if isinstance(hard_invariants, Mapping) else None
    indexed: dict[str, Mapping[str, Any]] = {}
    duplicates: set[str] = set()
    if isinstance(raw_checks, list):
        for value in raw_checks:
            if not isinstance(value, Mapping):
                continue
            code = str(value.get("code") or "")
            if not code:
                continue
            if code in indexed:
                duplicates.add(code)
            indexed[code] = value

    checks: list[dict[str, Any]] = []
    for code in SEMANTIC_REBUILD_GATE_CODES:
        source = indexed.get(code)
        source_status = str(source.get("status") or "") if source is not None else "missing"
        passed = source is not None and code not in duplicates and source_status == "passed"
        checks.append(
            {
                "code": code,
                "status": "passed" if passed else "failed",
                "source_status": source_status,
                "count": source.get("count") if source is not None else None,
                "reason": (
                    None
                    if passed
                    else "duplicate invariant check"
                    if code in duplicates
                    else "registered invariant is missing from the semantic report"
                    if source is None
                    else source.get("reason")
                    or f"registered invariant source status is {source_status}"
                ),
                "samples": list(source.get("samples") or [])[:20] if source is not None else [],
            }
        )
    failed = [check for check in checks if check["status"] != "passed"]
    return {
        "schema_version": SEMANTIC_REBUILD_GATE_VERSION,
        "status": "passed" if not failed else "failed",
        "swap_allowed": not failed,
        "registered_codes": list(SEMANTIC_REBUILD_GATE_CODES),
        "checks": checks,
        "failed_codes": [str(check["code"]) for check in failed],
    }


def compare_semantic_snapshots(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    *,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Compare two complete semantic snapshots without exposing graph content.

    The snapshot members are salted-by-kind SHA-256 identifiers.  They let the
    acceptance harness detect identity, predicate-vocabulary, and relation
    churn across identical reingest, source-order, restart, rebuild, and
    rollback arms without treating aggregate counts as proof of stability.
    """

    if not isinstance(sample_limit, int) or isinstance(sample_limit, bool) or sample_limit < 0:
        raise ValueError("sample_limit must be an integer >= 0")
    left = _validated_semantic_snapshot(before, name="before")
    right = _validated_semantic_snapshot(after, name="after")
    dimensions: dict[str, Any] = {}
    for dimension in ("identities", "predicates", "relations"):
        left_members = set(left[dimension]["members"])
        right_members = set(right[dimension]["members"])
        added = sorted(right_members - left_members)
        removed = sorted(left_members - right_members)
        union_count = len(left_members | right_members)
        changed = len(added) + len(removed)
        dimensions[dimension] = {
            "before_count": len(left_members),
            "after_count": len(right_members),
            "added_count": len(added),
            "removed_count": len(removed),
            "symmetric_difference_count": changed,
            "union_count": union_count,
            "churn_share": round(changed / union_count, 6) if union_count else 0.0,
            "stable": changed == 0,
            "added_samples": added[:sample_limit],
            "removed_samples": removed[:sample_limit],
            "samples_truncated": len(added) > sample_limit or len(removed) > sample_limit,
        }
    populations_stable = all(value["stable"] for value in dimensions.values())
    left_fingerprints = left["fingerprints"]
    right_fingerprints = right["fingerprints"]
    if left_fingerprints["status"] == right_fingerprints["status"] == "measured":
        fingerprint_matches = {
            key: left_fingerprints[key] == right_fingerprints[key]
            for key in ("config", "extraction", "semantic_policy")
        }
        fingerprint_comparison = {
            "status": "measured",
            "all_equal": all(fingerprint_matches.values()),
            "matches": fingerprint_matches,
        }
    else:
        fingerprint_comparison = {
            "status": "not_measured",
            "all_equal": False,
            "matches": {},
            "reason": "both snapshots require complete layered semantic fingerprints",
        }
    stable = populations_stable and fingerprint_comparison["all_equal"]
    return {
        "schema_version": SEMANTIC_CHURN_SCHEMA,
        "status": "measured",
        "before_generation": left["graph_generation"],
        "after_generation": right["graph_generation"],
        "population_stable": populations_stable,
        "fingerprints": fingerprint_comparison,
        "stable": stable,
        "dimensions": dimensions,
    }


def evaluate_ledger(
    records: Iterable[Mapping[str, Any]],
    run_ids: Iterable[str],
    *,
    registered_predicates: Iterable[str] | None = None,
    recall_samples: Iterable[Mapping[str, Any]] | None = None,
    adjudication: Mapping[str, Any] | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Evaluate explicit candidate-ledger runs without claiming graph authority.

    ADR 0040 Phase 1a needs the same versioned report before graph mutation, but
    the candidate ledger does not contain stored topology or adjudicated identity
    truth.  This evaluator therefore measures only candidate/plan facts present in
    the selected runs and keeps every unavailable conclusion ``not_measured``.

    ``run_ids`` is deliberately explicit: silently selecting the latest run would
    make a baseline non-reproducible when ingestion resumes or interleaves sources.
    """

    selected_run_ids = sorted({_required_text(value, "run_id") for value in run_ids})
    if not selected_run_ids:
        raise ValueError("run_ids must select at least one candidate-ledger run")
    if sample_limit < 1:
        raise ValueError("sample_limit must be >= 1")

    provided_rows = list(records)
    input_rows = [dict(row) for row in provided_rows if isinstance(row, Mapping)]
    rejected_input_records = len(provided_rows) - len(input_rows)
    selected_rows = [row for row in input_rows if str(row.get("run_id") or "") in selected_run_ids]
    selected_sha = _selected_record_sha256(selected_rows)
    selected_run_set = set(selected_run_ids)
    present_run_ids = {
        str(row.get("run_id") or "") for row in selected_rows if str(row.get("run_id") or "")
    }

    missing_run_ids = sorted(selected_run_set - present_run_ids)

    plan_rows = [row for row in selected_rows if row.get("kind") == "commit_plan"]
    selected_plan_ids = sorted(
        {str(row.get("plan_id") or "") for row in plan_rows if str(row.get("plan_id") or "")}
    )
    runs_without_plans = sorted(
        run_id
        for run_id in selected_run_ids
        if not any(str(row.get("run_id") or "") == run_id for row in plan_rows)
    )

    proposed_nodes = _ledger_proposals(selected_rows, candidate_kind="node")
    proposed_edges = _ledger_proposals(selected_rows, candidate_kind="edge")
    plan_operations = _ledger_plan_operations(plan_rows)
    plan_shape_issues = _ledger_plan_shape_issues(plan_rows, sample_limit=sample_limit)
    planned_node_operations = [
        row for row in plan_operations if row.get("candidate_kind") == "node"
    ]
    planned_relation_operations = [
        row for row in plan_operations if row.get("candidate_kind") == "edge"
    ]

    surface_layer = _ledger_surface_layer(proposed_nodes, sample_limit=sample_limit)
    type_layer = _ledger_type_layer(
        proposed_nodes,
        planned_node_operations,
        surface_layer=surface_layer,
        sample_limit=sample_limit,
    )
    registered = _materialize_registered_predicates(registered_predicates)
    recall_rows = None if recall_samples is None else tuple(recall_samples)
    predicate_layer = _ledger_predicate_layer(
        proposed_edges,
        planned_relation_operations,
        registered_predicates=registered,
        sample_limit=sample_limit,
    )
    relation_layer = _ledger_relation_layer(
        proposed_nodes,
        proposed_edges,
        planned_node_operations,
        planned_relation_operations,
        sample_limit=sample_limit,
    )
    recall_layer = _recall_layer(recall_rows, sample_limit=sample_limit)
    adjudication_layer = (
        None
        if adjudication is None
        else evaluate_ledger_adjudication(
            adjudication,
            proposed_nodes=proposed_nodes,
            proposed_relations=proposed_edges,
            planned_node_operations=planned_node_operations,
            planned_relation_operations=planned_relation_operations,
            sample_limit=sample_limit,
        )
    )
    if adjudication_layer is not None:
        type_layer["type_accuracy"] = adjudication_layer["entity"]["type_accuracy"]
        predicate_layer["adjudicated_mapping_accuracy"] = adjudication_layer["relation"][
            "predicate_accuracy"
        ]
        relation_layer["semantic_grounding"] = adjudication_layer["relation"]["grounding_quality"]
        relation_layer["direction_accuracy"] = adjudication_layer["relation"]["direction_accuracy"]
        relation_layer["literal_topology_accuracy"] = adjudication_layer["relation"][
            "object_kind_accuracy"
        ]
        relation_layer["adjudicated_quality"] = adjudication_layer["relation"]
        identity_layer: dict[str, Any] = {
            "same_type_exact_duplicate_groups": surface_layer["same_type_exact_duplicate_groups"],
            "cross_type_exact_conflict_groups": surface_layer["cross_type_exact_conflict_groups"],
            "cluster_quality": adjudication_layer["entity"],
            "b3_cluster_quality": adjudication_layer["entity"]["b3_cluster_quality"],
        }
    else:
        identity_layer = {
            "same_type_exact_duplicate_groups": surface_layer["same_type_exact_duplicate_groups"],
            "cross_type_exact_conflict_groups": surface_layer["cross_type_exact_conflict_groups"],
            "cluster_quality": {
                "status": "not_measured",
                "reason": "no human-adjudicated identity clusters were supplied",
            },
            "b3_cluster_quality": {
                "status": "not_measured",
                "reason": "no human-adjudicated identity clusters were supplied",
            },
        }
    run_state = _ledger_run_state(selected_rows, selected_run_ids)
    semantic_policy_evidence = _ledger_run_fingerprint(
        selected_rows,
        selected_run_ids,
        field="semantic_policy_fingerprint",
    )
    config_fingerprint_evidence = _ledger_run_fingerprint(
        selected_rows,
        selected_run_ids,
        field="config_fingerprint",
    )
    plan_order = _ledger_plan_order(selected_rows, sample_limit=sample_limit)
    missing_run_evidence = sorted(set(missing_run_ids) | set(run_state["missing_run_ids"]))

    record_kind_counts = Counter(str(row.get("kind") or "") for row in selected_rows)
    candidate_state_counts = Counter(
        str(row.get("state") or "") for row in selected_rows if row.get("kind") == "candidate"
    )
    candidate_kind_counts = Counter(
        str(row.get("candidate_kind") or "")
        for row in selected_rows
        if row.get("kind") == "candidate"
    )
    operation_counts = Counter(
        str(operation.get("operation") or "") for operation in plan_operations
    )

    invalid_proposed = type_layer["proposed"]["invalid_entity_types"]
    invalid_planned = type_layer["planned_create"]["invalid_entity_types"]
    raw_placeholders = predicate_layer["raw_proposed"]["placeholder_predicates"]
    planned_placeholders = predicate_layer["planned_accept"]["placeholder_predicates"]
    invalid_relations = relation_layer["invalid_planned_relation_shapes"]
    checks = [
        _check(
            "candidate_ledger_input_shape",
            rejected_input_records == 0,
            rejected_input_records,
        ),
        _check(
            "selected_ledger_runs_present",
            not missing_run_evidence,
            len(missing_run_evidence),
            missing_run_evidence[:sample_limit],
        ),
        _check(
            "candidate_ledger_plans_present",
            not runs_without_plans,
            len(runs_without_plans),
            runs_without_plans[:sample_limit],
        ),
        _check(
            "candidate_ledger_run_state_known",
            not run_state["unknown_run_ids"],
            len(run_state["unknown_run_ids"]),
            run_state["unknown_run_ids"][:sample_limit],
        ),
        _check(
            "candidate_ledger_plan_shape",
            not plan_shape_issues["count"],
            plan_shape_issues["count"],
            plan_shape_issues["samples"],
        ),
        _check(
            "closed_proposed_primitive_set",
            not invalid_proposed["count"],
            invalid_proposed["count"],
            invalid_proposed["samples"],
        ),
        _check(
            "closed_planned_primitive_set",
            not invalid_planned["count"],
            invalid_planned["count"],
            invalid_planned["samples"],
        ),
        _check(
            "placeholder_proposed_predicates",
            not raw_placeholders["count"],
            raw_placeholders["count"],
            raw_placeholders["samples"],
        ),
        _check(
            "placeholder_planned_predicates",
            not planned_placeholders["count"],
            planned_placeholders["count"],
            planned_placeholders["samples"],
        ),
        _check(
            "planned_relation_shape",
            not invalid_relations["count"],
            invalid_relations["count"],
            invalid_relations["samples"],
        ),
    ]
    anchor_shape = relation_layer["accepted_anchor_shape"]
    anchor_violations = anchor_shape["invalid_count"]
    checks.append(
        _check(
            "planned_relation_anchor_well_formed",
            not anchor_violations,
            anchor_violations,
            anchor_shape["invalid_samples"],
        )
    )
    if plan_order["status"] == "supported":
        checks.append(_check("commit_plan_preapply_order", True, 0))
    elif plan_order["status"] == "contradicted":
        checks.append(
            _check(
                "commit_plan_preapply_order",
                False,
                plan_order["contradicted_count"],
                plan_order["contradicted_samples"],
            )
        )
    else:
        checks.append(_unmeasured("commit_plan_preapply_order", plan_order["reason"]))
    if semantic_policy_evidence["status"] == "measured":
        checks.append(_check("single_semantic_policy", True, 0))
    elif semantic_policy_evidence["status"] == "inconsistent":
        checks.append(
            _check(
                "single_semantic_policy",
                False,
                len(semantic_policy_evidence["fingerprints"]),
                semantic_policy_evidence["runs"],
            )
        )
    else:
        checks.append(_unmeasured("single_semantic_policy", semantic_policy_evidence["reason"]))

    registry_coverage = predicate_layer["planned_accept"]["registry_coverage"]
    if registry_coverage["status"] == "measured":
        checks.append(
            _check(
                "registered_predicates",
                not registry_coverage["unregistered_count"],
                registry_coverage["unregistered_count"],
                registry_coverage["unregistered_samples"],
            )
        )
    else:
        checks.append(_unmeasured("registered_predicates", registry_coverage["reason"]))
    if recall_layer["status"] == "measured":
        completion_violations = recall_layer["completion"]["violations"]
        checks.append(
            _check(
                "completion_free_recall",
                not completion_violations,
                len(completion_violations),
                completion_violations,
            )
        )
    else:
        checks.append(_unmeasured("completion_free_recall", recall_layer["reason"]))
    checks.append(
        _unmeasured(
            "candidate_ledger_source_framing",
            "the evaluator receives parsed records, not the source JSONL bytes",
        )
    )
    surface_preservation = surface_layer["source_surface_preservation"]
    if surface_preservation["status"] == "measured":
        checks.append(_check("source_surface_preservation", True, 0))
    elif surface_preservation["status"] == "incomplete":
        checks.append(
            _check(
                "source_surface_preservation",
                False,
                surface_preservation["invalid_count"],
                surface_preservation["invalid_samples"],
            )
        )
    else:
        checks.append(
            _unmeasured(
                "source_surface_preservation",
                surface_preservation["reason"],
            )
        )
    checks.append(
        _unmeasured(
            "planned_relation_anchor_coverage",
            "anchor-less EdgeCandidate rows remain contract-legal until Phase 4 sets policy",
        )
    )
    if adjudication_layer is None:
        checks.extend(
            (
                _unmeasured(
                    "identity_cluster_quality",
                    "no human-adjudicated identity clusters were supplied",
                ),
                _unmeasured(
                    "cross_type_merge_safety",
                    "candidate comparisons do not retain adjudicated cross-type identity truth",
                ),
                _unmeasured(
                    "type_accuracy",
                    "no human-adjudicated primitive labels were supplied",
                ),
                _unmeasured(
                    "confirmed_inverse_direction",
                    "predicate lifecycle decisions and endpoint-swap receipts are unavailable",
                ),
                _unmeasured(
                    "relation_grounding",
                    "anchor shape does not prove semantic support for a proposed relation",
                ),
                _unmeasured(
                    "literal_topology_classification",
                    "no human-adjudicated literal-versus-topology fixture was supplied",
                ),
            )
        )
    else:
        entity_coverage = adjudication_layer["coverage"]["entity_plan_operations"]
        relation_coverage = adjudication_layer["coverage"]["relation_plan_operations"]
        false_cross_type = adjudication_layer["entity"]["pairwise_cluster_errors"][
            "cross_type_false_merges"
        ]
        direction = adjudication_layer["relation"]["direction_accuracy"]
        grounding = adjudication_layer["relation"]["grounding_quality"]
        object_kind = adjudication_layer["relation"]["object_kind_accuracy"]
        checks.extend(
            (
                _check(
                    "adjudicated_entity_plan_coverage",
                    entity_coverage["status"] == "complete",
                    entity_coverage["missing_count"],
                    entity_coverage["missing_samples"],
                ),
                _check(
                    "adjudicated_relation_plan_coverage",
                    relation_coverage["status"] == "complete",
                    relation_coverage["missing_count"],
                    relation_coverage["missing_samples"],
                ),
                _check("identity_cluster_quality", True, 0),
                _check(
                    "cross_type_merge_safety",
                    not false_cross_type["count"],
                    false_cross_type["count"],
                    false_cross_type["samples"],
                ),
                _check("type_accuracy", True, 0),
                _check(
                    "confirmed_inverse_direction",
                    not direction["incorrect"],
                    direction["incorrect"],
                ),
                _check(
                    "relation_grounding",
                    not grounding["false_positive"],
                    grounding["false_positive"],
                    adjudication_layer["relation"]["false_accepts"]["samples"],
                ),
                _check(
                    "literal_topology_classification",
                    not object_kind["incorrect"],
                    object_kind["incorrect"],
                ),
            )
        )

    failed = [row for row in checks if row["status"] == "failed"]
    unmeasured = [row for row in checks if row["status"] == "not_measured"]
    invariant_status = "failed" if failed else "incomplete" if unmeasured else "passed"
    verdict = "failed" if failed else "incomplete"
    selection_complete = (
        bool(selected_rows)
        and not missing_run_evidence
        and not runs_without_plans
        and not run_state["unknown_run_ids"]
        and not plan_shape_issues["count"]
        and rejected_input_records == 0
    )
    return {
        "schema_version": SEMANTIC_QUALITY_SCHEMA,
        "evaluator_version": SEMANTIC_QUALITY_SCHEMA,
        "report_variant": "candidate_ledger_plan.v1",
        "evidence": {
            "scope": "candidate_ledger_plan",
            "integrity_status": "pre_commit",
            "graph_generation": None,
            "complete": False,
            "selection_complete": selection_complete,
            "authoritative": False,
            "reason": (
                "candidate-ledger plans are Phase 1a measurement evidence; source-file framing "
                "and stored-graph authority are outside this selected-record evaluation"
            ),
            "selected_run_ids": selected_run_ids,
            "selected_plan_ids": selected_plan_ids,
            "selected_record_count": len(selected_rows),
            "selected_record_sha256": selected_sha,
            "missing_run_ids": missing_run_evidence,
            "runs_without_plans": runs_without_plans,
            "source_framing": {
                "status": "not_measured",
                "reason": "the evaluator receives parsed records, not the source JSONL bytes",
            },
            "run_state": run_state,
            "semantic_policy_fingerprint": semantic_policy_evidence["fingerprint"],
            "semantic_policy": semantic_policy_evidence,
            "config_fingerprint": config_fingerprint_evidence["fingerprint"],
            "config": config_fingerprint_evidence,
            "plan_order": plan_order,
            "external_inputs": _external_input_evidence(
                registered_predicates=registered,
                recall_samples=recall_rows,
                adjudication=adjudication,
            ),
        },
        "population": {
            "records": len(selected_rows),
            "provided_records": len(provided_rows),
            "rejected_non_mapping_records": rejected_input_records,
            "records_by_kind": dict(sorted(record_kind_counts.items())),
            "candidate_records_by_kind": dict(sorted(candidate_kind_counts.items())),
            "candidate_records_by_state": dict(sorted(candidate_state_counts.items())),
            "unique_proposed_nodes": len(proposed_nodes),
            "unique_proposed_relations": len(proposed_edges),
            "plan_operations": len(plan_operations),
            "plan_operations_by_action": dict(sorted(operation_counts.items())),
            "plan_shape_issues": plan_shape_issues,
        },
        "layers": {
            "surface": surface_layer,
            "type": type_layer,
            "identity": identity_layer,
            "predicate": predicate_layer,
            "relation": relation_layer,
            "recall": recall_layer,
            "adjudication": (
                adjudication_layer
                if adjudication_layer is not None
                else {
                    "status": "not_measured",
                    "reason": "no semantic_adjudication.v1 evidence was supplied",
                }
            ),
        },
        "hard_invariants": {
            "status": invariant_status,
            "measured_pass": not failed,
            "complete": not unmeasured,
            "checks": checks,
        },
        "verdict": {
            "scope": "candidate_ledger_plan_measurements",
            "status": verdict,
            "authoritative_pass": False,
            "reason": (
                "one or more precommit measurements failed"
                if failed
                else "Phase 1a evidence and required semantic measurements are incomplete"
            ),
        },
        "limitations": [
            "candidate-ledger evidence cannot prove stored graph topology",
            *(
                ["selected runs do not preserve one complete semantic-policy fingerprint"]
                if semantic_policy_evidence["status"] != "measured"
                else []
            ),
            *(
                ["source surface preservation and identity quality require adjudicated evidence"]
                if adjudication_layer is None
                else []
            ),
            "external or previously stored relation endpoints are not verifiable from the ledger",
            *(
                ["predicate lifecycle is unavailable until ADR 0040 Phase 3"]
                if registered_predicates is None
                else []
            ),
            *(
                ["recall cost requires supplied recall_cost.v1 samples"]
                if recall_layer["status"] != "measured"
                else []
            ),
        ],
    }


def evaluate_ledger_scan(
    scan: LedgerScanResult,
    run_ids: Iterable[str],
    *,
    registered_predicates: Iterable[str] | None = None,
    recall_samples: Iterable[Mapping[str, Any]] | None = None,
    adjudication: Mapping[str, Any] | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Evaluate selected runs while retaining the complete JSONL framing proof.

    :func:`evaluate_ledger` remains useful for callers that already hold parsed
    rows, but it cannot prove what the source file omitted.  This entry point is
    the Phase 1a evidence boundary: the complete-file hash and lossless scan
    travel with the same selected-record measurements.  Raw malformed samples
    and the vault-local path are deliberately excluded from the report.
    """

    report = evaluate_ledger(
        scan.parsed_records,
        run_ids,
        registered_predicates=registered_predicates,
        recall_samples=recall_samples,
        adjudication=adjudication,
        sample_limit=sample_limit,
    )
    framing_complete = scan.completeness_status == "complete"
    framing = {
        "status": "measured" if framing_complete else "incomplete",
        "complete": framing_complete,
        "completeness_status": scan.completeness_status,
        "completeness_reason": scan.completeness_reason,
        "file_sha256": scan.file_sha256,
        "file_size_bytes": scan.file_size_bytes,
        "total_lines": scan.total_lines,
        "nonempty_lines": scan.nonempty_lines,
        "parsed_record_count": scan.parsed_record_count,
        "malformed_line_count": scan.malformed_line_count,
        "sampled_malformed_line_numbers": [row.line_number for row in scan.malformed_lines],
        "malformed_samples_truncated": scan.malformed_samples_truncated,
        "unrecognized_version_record_count": scan.unrecognized_version_record_count,
        "ledger_versions": list(scan.ledger_versions),
        "unterminated_final_line": scan.unterminated_final_line,
        "trailing_partial": scan.trailing_partial,
        "trailing_partial_line_number": scan.trailing_partial_line_number,
    }

    evidence = dict(report["evidence"])
    evidence["source_framing"] = framing
    evidence["complete"] = bool(framing_complete and evidence["selection_complete"])
    evidence["reason"] = (
        "complete lossless ledger framing with explicit selected-run plan evidence; "
        "the pre-commit report remains non-authoritative for stored graph semantics"
        if evidence["complete"]
        else "ledger framing or selected-run plan evidence is incomplete"
    )
    report["evidence"] = evidence

    replacement = _check(
        "candidate_ledger_source_framing",
        framing_complete,
        0 if framing_complete else 1,
        []
        if framing_complete
        else [
            {
                "completeness_status": scan.completeness_status,
                "reason": scan.completeness_reason,
                "malformed_line_count": scan.malformed_line_count,
                "unrecognized_version_record_count": (scan.unrecognized_version_record_count),
                "trailing_partial": scan.trailing_partial,
            }
        ],
    )
    checks = [
        replacement if row["code"] == "candidate_ledger_source_framing" else row
        for row in report["hard_invariants"]["checks"]
    ]
    failed = [row for row in checks if row["status"] == "failed"]
    unmeasured = [row for row in checks if row["status"] == "not_measured"]
    report["hard_invariants"] = {
        "status": "failed" if failed else "incomplete" if unmeasured else "passed",
        "measured_pass": not failed,
        "complete": not unmeasured,
        "checks": checks,
    }
    report["verdict"] = {
        **report["verdict"],
        "status": "failed" if failed else "incomplete",
        "reason": (
            "one or more precommit measurements failed"
            if failed
            else "Phase 1a semantic measurements remain incomplete"
        ),
    }
    limitations = [
        limitation
        for limitation in report["limitations"]
        if limitation != "candidate-ledger evidence cannot prove stored graph topology"
    ]
    report["limitations"] = [
        "candidate-ledger evidence cannot prove stored graph topology",
        *([] if framing_complete else ["candidate-ledger source framing is incomplete"]),
        *limitations,
    ]
    return report


def _evaluate_graph(
    nodes: Iterable[Any],
    edges: Iterable[Any],
    *,
    integrity: Mapping[str, Any] | None = None,
    technical_integrity_verified: bool,
    registered_predicates: Iterable[str] | None = None,
    recall_samples: Iterable[Mapping[str, Any]] | None = None,
    fingerprints: Mapping[str, Any],
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Pure semantic evaluator used by store-backed and fixture callers."""

    all_node_rows = list(nodes)
    all_node_type_by_id = {
        _node_id(node): _node_type(node) for node in all_node_rows if _node_id(node)
    }
    all_edge_rows = list(edges)
    node_rows = [
        node
        for node in all_node_rows
        if not is_infra(node) and _node_type(node) not in INTERNAL_NODE_TYPES
    ]
    node_by_id = {_node_id(node): node for node in node_rows if _node_id(node)}
    edge_rows = [
        edge
        for edge in all_edge_rows
        if _edge_src(edge) in node_by_id and _edge_dst(edge) in node_by_id
    ]
    node_type_by_id = {node_id: _node_type(node) for node_id, node in node_by_id.items()}
    primitive_ids = {
        node_id for node_id, type_ in node_type_by_id.items() if type_ in PRIMITIVE_NAMES
    }
    support_ids = {
        node_id for node_id, type_ in node_type_by_id.items() if type_ in SUPPORT_NODE_TYPES
    }

    surface = _surface_layer(node_by_id, primitive_ids, sample_limit=sample_limit)
    type_layer = _type_layer(node_by_id, primitive_ids, support_ids, surface)
    (
        predicate_layer,
        relation_claims,
        historical_relation_claims,
        live_claim_entity_ids,
    ) = _predicate_layer(
        node_by_id,
        registered_predicates=registered_predicates,
        sample_limit=sample_limit,
    )
    integrity_payload = dict(integrity or {})
    integrity_status = str(integrity_payload.get("status") or "unverified")
    graph_generation = integrity_payload.get("graph_generation")
    fresh_for_semantic_scan = integrity_payload.get("fresh_for_semantic_scan") is True
    integrity_freshness = (
        "fresh"
        if technical_integrity_verified
        else "rejected"
        if fresh_for_semantic_scan
        else "cached"
        if integrity_status == "verified"
        else "unavailable"
    )
    topology_reason = (
        "ADR 0039 integrity was not freshly verified for this semantic scan"
        if integrity_status == "verified"
        else f"ADR 0039 integrity is {integrity_status} for this semantic scan"
    )

    relation_layer = _relation_layer(
        node_by_id,
        node_type_by_id,
        edge_rows,
        primitive_ids,
        relation_claims,
        historical_relation_claims,
        live_claim_entity_ids,
        structural_edges=all_edge_rows,
        all_node_type_by_id=all_node_type_by_id,
        sample_limit=sample_limit,
    )
    relation_layer = {
        "topology_source": "stored_edge_properties",
        "topology_evidence_status": "measured",
        **relation_layer,
    }
    if not technical_integrity_verified:
        relation_layer = _without_untrusted_topology(relation_layer, topology_reason)
    recall_layer = _recall_layer(recall_samples, sample_limit=sample_limit)
    evidence_authoritative = technical_integrity_verified and bool(
        integrity_payload.get("semantic_baseline_authoritative")
    )

    checks = [
        (
            _check("complete_store_scan", True, 0)
            if technical_integrity_verified
            else _unmeasured("complete_store_scan", topology_reason)
        ),
        _check(
            "closed_primitive_set",
            not type_layer["invalid_entity_types"]["count"],
            type_layer["invalid_entity_types"]["count"],
            type_layer["invalid_entity_types"]["samples"],
        ),
        _check(
            "claim_object_shape",
            not relation_layer["invalid_claim_object_shapes"]["count"],
            relation_layer["invalid_claim_object_shapes"]["count"],
            relation_layer["invalid_claim_object_shapes"]["samples"],
        ),
        _check(
            "placeholder_predicates",
            not predicate_layer["placeholder_predicates"]["count"],
            predicate_layer["placeholder_predicates"]["count"],
            predicate_layer["placeholder_predicates"]["samples"],
        ),
    ]
    topology_checks = (
        ("claim_source_anchors", "unanchored_claims"),
        ("claim_bridge_materialization", "missing_claim_bridges"),
        ("claim_provenance_materialization", "missing_claim_provenance"),
        ("live_relation_endpoints", "dead_endpoint_claims"),
        ("relation_materialization", "missing_topology_edges"),
        ("topology_claim_coverage", "topology_edges_without_claims"),
    )
    for code, metric_name in topology_checks:
        if technical_integrity_verified:
            metric = relation_layer[metric_name]
            checks.append(_check(code, not metric["count"], metric["count"], metric["samples"]))
        else:
            checks.append(_unmeasured(code, topology_reason))
    registry_coverage = predicate_layer["registry_coverage"]
    if registry_coverage["status"] == "measured":
        checks.append(
            _check(
                "registered_predicates",
                not registry_coverage["unregistered_count"],
                registry_coverage["unregistered_count"],
                registry_coverage["unregistered_samples"],
            )
        )
    else:
        checks.append(_unmeasured("registered_predicates", registry_coverage["reason"]))
    if recall_layer["status"] == "measured":
        completion_violations = recall_layer["completion"]["violations"]
        checks.append(
            _check(
                "completion_free_recall",
                not completion_violations,
                len(completion_violations),
                completion_violations,
            )
        )
    else:
        checks.append(_unmeasured("completion_free_recall", recall_layer["reason"]))
    checks.extend(
        (
            _unmeasured(
                "cross_type_merge_safety",
                "source surfaces and identity decisions are not retained in the stored graph",
            ),
            _unmeasured(
                "confirmed_inverse_direction",
                "predicate registry decisions and endpoint-swap receipts are unavailable",
            ),
            _unmeasured(
                "predicate_syntax_policy",
                "Claim accepts both application labels and CURIEs; Phase 3 must own one policy",
            ),
            _unmeasured(
                "uncertainty_abstention",
                "semantic decision outcomes are not projected into this stored-graph report",
            ),
            _unmeasured(
                "relation_grounding",
                "byte anchors prove provenance shape, not semantic support for each relation",
            ),
            _unmeasured(
                "exact_byte_provenance",
                "this report validates anchor shape but does not reopen and hash source bytes",
            ),
            _unmeasured(
                "literal_topology_classification",
                "no human-adjudicated literal-versus-topology fixture was supplied",
            ),
        )
    )

    failed = [row for row in checks if row["status"] == "failed"]
    unmeasured = [row for row in checks if row["status"] == "not_measured"]
    measured_pass = not failed
    if failed:
        invariant_status = "failed"
    elif unmeasured:
        invariant_status = "incomplete"
    else:
        invariant_status = "passed"

    if invariant_status == "failed":
        verdict = "failed"
    elif not evidence_authoritative or invariant_status != "passed":
        verdict = "incomplete"
    else:
        verdict = "passed"

    semantic_snapshot = _semantic_snapshot(
        node_by_id,
        primitive_ids,
        graph_generation=graph_generation,
        fingerprints=fingerprints,
        measured=technical_integrity_verified,
        reason=topology_reason,
    )

    return {
        "schema_version": SEMANTIC_QUALITY_SCHEMA,
        "evaluator_version": SEMANTIC_QUALITY_SCHEMA,
        "evidence": {
            "scope": "stored_graph",
            "complete": technical_integrity_verified,
            "graph_generation": graph_generation,
            "integrity_status": integrity_status,
            "integrity_freshness": integrity_freshness,
            "technical_integrity_verified": technical_integrity_verified,
            "topology_source": "stored_edge_properties",
            "topology_evidence_status": (
                "measured" if technical_integrity_verified else "not_measured"
            ),
            **(
                {"integrity_audit": _compact_integrity_audit(integrity_payload["last_audit"])}
                if isinstance(integrity_payload.get("last_audit"), Mapping)
                else {}
            ),
            "authoritative": evidence_authoritative,
            "reason": (
                None
                if evidence_authoritative
                else "ADR 0039 has not published an authoritative semantic-baseline generation"
                if technical_integrity_verified
                else topology_reason
            ),
        },
        "population": {
            "nodes": len(node_rows),
            "edges": len(edge_rows) if technical_integrity_verified else None,
            "primitive_entities": len(primitive_ids),
            "support_nodes": len(support_ids),
            "claims": sum(1 for node in node_rows if _node_type(node) == "Claim"),
        },
        "layers": {
            "surface": surface,
            "type": type_layer,
            "identity": {
                "same_type_exact_duplicate_groups": surface["same_type_exact_duplicate_groups"],
                "cross_type_conflict_groups": surface["cross_type_conflict_groups"],
                "cluster_quality": {
                    "status": "not_measured",
                    "reason": "no human-adjudicated identity clusters were supplied",
                },
            },
            "predicate": predicate_layer,
            "relation": relation_layer,
            "recall": recall_layer,
        },
        "semantic_snapshot": semantic_snapshot,
        "hard_invariants": {
            "status": invariant_status,
            "measured_pass": measured_pass,
            "complete": not unmeasured,
            "checks": checks,
        },
        "verdict": {
            "scope": "registered_semantic_hard_invariants",
            "status": verdict,
            "authoritative_pass": verdict == "passed",
            "reason": (
                None
                if verdict == "passed"
                else "one or more semantic invariants failed"
                if failed
                else "evidence or required semantic measurements are incomplete"
            ),
        },
        "limitations": [
            *(
                ["predicate lifecycle is unavailable until ADR 0040 Phase 3"]
                if registered_predicates is None
                else []
            ),
            "identity precision/recall requires a human-adjudicated fixture",
            *(
                ["recall cost requires supplied recall_cost.v1 samples"]
                if recall_layer["status"] != "measured"
                else []
            ),
        ],
    }


def _recall_layer(
    recall_samples: Iterable[Mapping[str, Any]] | None,
    *,
    sample_limit: int,
) -> dict[str, Any]:
    """Aggregate complete ``recall_cost.v1`` samples without inventing evidence."""

    rows = list(recall_samples or ())
    if not rows:
        return _unmeasured_recall("no recall_cost.v1 samples were supplied")

    measured: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            rejected.append({"index": index, "reason": "sample must be a mapping"})
            continue
        reason = _invalid_recall_sample_reason(row)
        if reason is not None:
            rejected.append({"index": index, "reason": reason})
            continue
        measured.append(dict(row))

    if rejected:
        return {
            **_unmeasured_recall(
                "one or more supplied recall samples lack complete recall_cost.v1 accounting"
            ),
            "provided_samples": len(rows),
            "measured_samples": len(measured),
            "rejected_samples": {
                "count": len(rejected),
                "samples": rejected[:sample_limit],
            },
        }

    completion_violations = [
        {
            "index": index,
            "completion_calls": row["completion_calls"],
            "generated_tokens": row["generated_tokens"],
        }
        for index, row in enumerate(measured)
        if row["completion_calls"] != 0
        or row["generated_tokens"] != 0
        or row["completion_free"] is not True
    ]
    return {
        "schema_version": "recall_cost.aggregate.v1",
        "sample_schema_version": "recall_cost.v1",
        "percentile_method": "nearest_rank",
        "status": "measured",
        "reason": None,
        "provided_samples": len(rows),
        "measured_samples": len(measured),
        "rejected_samples": {"count": 0, "samples": []},
        "completion": {
            "calls_total": sum(row["completion_calls"] for row in measured),
            "calls_max_per_recall": max(row["completion_calls"] for row in measured),
            "generated_tokens_total": sum(row["generated_tokens"] for row in measured),
            "generated_tokens_max_per_recall": max(row["generated_tokens"] for row in measured),
            "violations": completion_violations[:sample_limit],
        },
        "query_embedding": {
            "calls_total": sum(row["query_embedding_calls"] for row in measured),
            "calls_per_recall": _distribution([row["query_embedding_calls"] for row in measured]),
            "latency_ms": _distribution([row["query_embedding_latency_ms"] for row in measured]),
            "providers": dict(
                sorted(Counter(row["query_embedding_provider"] for row in measured).items())
            ),
            "models": dict(
                sorted(Counter(row["query_embedding_model"] for row in measured).items())
            ),
            "execution": dict(
                sorted(Counter(row["query_embedding_execution"] for row in measured).items())
            ),
        },
        "deterministic_retrieval": {
            "latency_ms": _distribution(
                [row["deterministic_retrieval_latency_ms"] for row in measured]
            ),
            "results": _distribution([row["retrieved_results"] for row in measured]),
            "bytes": _distribution([row["retrieved_bytes"] for row in measured]),
        },
        "deterministic_projection": {
            "latency_ms": _distribution(
                [row["deterministic_projection_latency_ms"] for row in measured]
            ),
        },
        "total_latency_ms": _distribution([row["total_latency_ms"] for row in measured]),
        "answer_generation": {
            "status": "not_in_scope",
            "reason": "ordinary recall cost excludes optional answer generation",
        },
    }


def _unmeasured_recall(reason: str) -> dict[str, Any]:
    return {
        "schema_version": "recall_cost.aggregate.v1",
        "sample_schema_version": "recall_cost.v1",
        "percentile_method": "nearest_rank",
        "status": "not_measured",
        "reason": reason,
        "provided_samples": 0,
        "measured_samples": 0,
        "rejected_samples": {"count": 0, "samples": []},
    }


def _invalid_recall_sample_reason(row: Mapping[str, Any]) -> str | None:
    if row.get("schema_version") != "recall_cost.v1":
        return "schema_version must be recall_cost.v1"
    measurement_status = row.get("measurement_status")
    if measurement_status == "not_measured":
        return "sample is explicitly not_measured"
    if measurement_status not in {None, "measured"}:
        return "measurement_status must be measured when supplied"
    for key in (
        "completion_calls",
        "generated_tokens",
        "query_embedding_calls",
        "retrieved_results",
        "retrieved_bytes",
    ):
        value = row.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return f"{key} must be an integer >= 0"
    for key in (
        "query_embedding_latency_ms",
        "deterministic_retrieval_latency_ms",
        "deterministic_projection_latency_ms",
        "total_latency_ms",
    ):
        value = row.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
            return f"{key} must be a number >= 0"
    for key in (
        "query_embedding_provider",
        "query_embedding_model",
        "query_embedding_execution",
    ):
        value = row.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"{key} must be a non-empty string"
    if row.get("query_embedding_execution") not in {"local", "remote"}:
        return "query_embedding_execution must be local or remote"
    if not isinstance(row.get("completion_free"), bool):
        return "completion_free must be a boolean"
    return None


def _distribution(values: list[int | float]) -> dict[str, int | float]:
    ordered = sorted(values)
    return {
        "total": round(sum(ordered), 3),
        "p50": ordered[math.ceil(0.50 * len(ordered)) - 1],
        "p95": ordered[math.ceil(0.95 * len(ordered)) - 1],
    }


def _without_untrusted_topology(
    relation_layer: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Keep facet-only relation facts and suppress conclusions from bad adjacency."""

    result = dict(relation_layer)
    result["topology_evidence_status"] = "not_measured"
    result["materialized_topology_edges"] = None
    result["materialized_topology_edge_types"] = {
        "status": "not_measured",
        "counts": {},
        "reason": reason,
    }
    for name in (
        "dead_endpoint_claims",
        "missing_topology_edges",
        "topology_edges_without_claims",
        "inactive_topology_edges",
        "unanchored_claims",
        "missing_claim_bridges",
        "missing_claim_provenance",
        "self_loop_edges",
        "topology_isolated_entities",
        "isolated_entities",
    ):
        result[name] = {
            "status": "not_measured",
            "count": None,
            "samples": [],
            "reason": reason,
        }
    result["top_degree_entities"] = {
        "status": "not_measured",
        "samples": [],
        "reason": reason,
    }
    return result


def _integrity_is_freshly_verified(
    integrity: Mapping[str, Any],
    *,
    store_generation: object | None = None,
) -> bool:
    """Accept only the current complete audit proof, never a bare green sidecar."""

    audit = integrity.get("last_audit")
    graph_generation = integrity.get("graph_generation")
    if not isinstance(audit, Mapping):
        return False
    if store_generation is not None and graph_generation != store_generation:
        return False
    return bool(
        integrity.get("status") == "verified"
        and integrity.get("fresh_for_semantic_scan") is True
        and audit.get("status") == "verified"
        and audit.get("graph_generation") == graph_generation
        and audit.get("nodes_complete") is True
        and audit.get("edges_complete") is True
        and audit.get("adjacency_complete") is True
        and audit.get("manifest_complete") is True
    )


def _compact_integrity_audit(audit: Mapping[str, Any]) -> dict[str, Any]:
    """Project only the diagnostics needed to explain the semantic gate."""

    issues = audit.get("issues")
    first_issue = issues[0] if isinstance(issues, (list, tuple)) and issues else None
    return {
        "status": audit.get("status"),
        "issue_count": audit.get("issue_count"),
        "adjacency_complete": audit.get("adjacency_complete"),
        "first_issue": first_issue if isinstance(first_issue, Mapping) else None,
    }


def _surface_layer(
    node_by_id: Mapping[str, Any],
    primitive_ids: set[str],
    *,
    sample_limit: int,
) -> dict[str, Any]:
    by_exact: dict[str, list[dict[str, str]]] = defaultdict(list)
    flag_counts: Counter[str] = Counter()
    flag_samples: dict[str, list[str]] = defaultdict(list)
    titles_by_type: dict[str, list[str]] = defaultdict(list)

    for node_id in sorted(primitive_ids):
        node = node_by_id[node_id]
        title = _node_title(node)
        type_ = _node_type(node)
        titles_by_type[type_].append(title)
        key = exact_surface_key(title)
        if key:
            by_exact[key].append({"id": node_id, "title": title, "type": type_})
        flags = _surface_flags(title)
        for flag in flags:
            flag_counts[flag] += 1
            if len(flag_samples[flag]) < sample_limit:
                flag_samples[flag].append(title)

    same_type: list[dict[str, Any]] = []
    cross_type: list[dict[str, Any]] = []
    for key, members in by_exact.items():
        by_type: dict[str, list[dict[str, str]]] = defaultdict(list)
        for member in members:
            by_type[member["type"]].append(member)
        for type_, typed_members in by_type.items():
            if len(typed_members) > 1:
                same_type.append(
                    {
                        "exact_key": key,
                        "type": type_,
                        "count": len(typed_members),
                        "members": typed_members[:sample_limit],
                    }
                )
        if len(by_type) > 1:
            cross_type.append(
                {
                    "exact_key": key,
                    "types": sorted(by_type),
                    "count": len(members),
                    "members": members[:sample_limit],
                }
            )

    same_type.sort(key=lambda row: (row["exact_key"], row["type"]))
    cross_type.sort(key=lambda row: row["exact_key"])
    return {
        "normalizer_version": NORMALIZER_VERSION,
        "primitive_entities": len(primitive_ids),
        "same_type_exact_duplicate_groups": {
            "count": len(same_type),
            "samples": same_type[:sample_limit],
        },
        "cross_type_conflict_groups": {
            "count": len(cross_type),
            "samples": cross_type[:sample_limit],
        },
        "normalization_flags": {
            "counts": dict(sorted(flag_counts.items())),
            "samples": {key: values for key, values in sorted(flag_samples.items())},
        },
        "sample_titles_by_type": {
            type_: sorted(titles, key=str.casefold)[:sample_limit]
            for type_, titles in sorted(titles_by_type.items())
        },
    }


def _type_layer(
    node_by_id: Mapping[str, Any],
    primitive_ids: set[str],
    support_ids: set[str],
    surface: Mapping[str, Any],
) -> dict[str, Any]:
    invalid = [
        {
            "id": node_id,
            "title": _node_title(node),
            "type": _node_type(node),
        }
        for node_id, node in node_by_id.items()
        if node_id not in primitive_ids and node_id not in support_ids
    ]
    primitive_counts = Counter(_node_type(node_by_id[node_id]) for node_id in primitive_ids)
    return {
        "primitive_counts": dict(sorted(primitive_counts.items())),
        "invalid_entity_types": {"count": len(invalid), "samples": invalid[:20]},
        "cross_type_conflict_groups": surface["cross_type_conflict_groups"],
        "type_accuracy": {
            "status": "not_measured",
            "reason": "no human-adjudicated primitive labels were supplied",
        },
    }


def _predicate_layer(
    node_by_id: Mapping[str, Any],
    *,
    registered_predicates: Iterable[str] | None,
    sample_limit: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], set[str]]:
    relation_claims: list[dict[str, Any]] = []
    historical_relation_claims: list[dict[str, Any]] = []
    live_claim_entity_ids: set[str] = set()
    predicate_counts: Counter[str] = Counter()
    placeholders: list[str] = []
    superseded_claims = 0

    for claim_id, node in node_by_id.items():
        if _node_type(node) != "Claim":
            continue
        facets = _facets(node)
        if is_superseded(node):
            superseded_claims += 1
            object_id = _optional_text(facets.get("O_id"))
            if object_id is not None:
                historical_relation_claims.append(
                    {
                        "claim_id": claim_id,
                        "subject_id": str(facets.get("S_id") or ""),
                        "predicate": str(facets.get("P") or "").strip(),
                        "object_id": object_id,
                        "block_id": str(facets.get("block_id") or ""),
                    }
                )
            continue
        subject_id = _optional_text(facets.get("S_id"))
        if subject_id is not None:
            live_claim_entity_ids.add(subject_id)
        predicate = str(facets.get("P") or "").strip()
        predicate_counts[predicate] += 1
        if predicate.casefold() in PLACEHOLDER_PREDICATES:
            placeholders.append(predicate)

        object_id = _optional_text(facets.get("O_id"))
        if object_id is None:
            continue
        live_claim_entity_ids.add(object_id)
        relation_claims.append(
            {
                "claim_id": claim_id,
                "subject_id": str(facets.get("S_id") or ""),
                "predicate": predicate,
                "object_id": object_id,
                "block_id": str(facets.get("block_id") or ""),
            }
        )

    counts = dict(sorted(predicate_counts.items(), key=lambda item: (-item[1], item[0])))
    singleton_labels = sorted(label for label, count in predicate_counts.items() if count == 1)
    total = sum(predicate_counts.values())
    if registered_predicates is None:
        registry_coverage = {
            "status": "not_measured",
            "reason": "predicate registry is not implemented",
            "unregistered_count": None,
            "unregistered_samples": [],
        }
    else:
        registered = {str(value) for value in registered_predicates}
        unregistered = sorted(set(predicate_counts) - registered - _STRUCTURAL_INGEST_PREDICATES)
        structural = sorted(set(predicate_counts) & _STRUCTURAL_INGEST_PREDICATES)
        registry_coverage = {
            "status": "measured",
            "reason": None,
            "unregistered_count": len(unregistered),
            "unregistered_samples": unregistered[:sample_limit],
            "excluded_structural_predicates": structural,
        }
    return (
        {
            "predicate_assertions": total,
            "relation_claims": len(relation_claims),
            "historical_superseded_claims": superseded_claims,
            "raw_vocabulary_size": len(predicate_counts),
            "raw_vocabulary_per_1000_assertions": (
                round((len(predicate_counts) * 1000) / total, 3) if total else 0.0
            ),
            "predicate_counts": dict(list(counts.items())[:sample_limit]),
            "singletons": {
                "count": len(singleton_labels),
                "share": round(len(singleton_labels) / len(predicate_counts), 6)
                if predicate_counts
                else 0.0,
                "samples": singleton_labels[:sample_limit],
            },
            "placeholder_predicates": {
                "count": len(placeholders),
                "samples": sorted(set(placeholders))[:sample_limit],
            },
            "syntax_policy": {
                "status": "not_measured",
                "reason": "predicate syntax belongs to the Phase 3 registry contract",
            },
            "registry_coverage": registry_coverage,
        },
        relation_claims,
        historical_relation_claims,
        live_claim_entity_ids,
    )


def _relation_layer(
    node_by_id: Mapping[str, Any],
    node_type_by_id: Mapping[str, str],
    edges: list[Any],
    primitive_ids: set[str],
    relation_claims: list[dict[str, Any]],
    historical_relation_claims: list[dict[str, Any]],
    live_claim_entity_ids: set[str],
    *,
    structural_edges: list[Any],
    all_node_type_by_id: Mapping[str, str],
    sample_limit: int,
) -> dict[str, Any]:
    edge_keys = {
        (_edge_src(edge), _edge_type(edge), _edge_dst(edge))
        for edge in edges
        if _edge_src(edge) and _edge_dst(edge)
    }
    prov_anchors = {
        (_edge_src(edge), _edge_dst(edge))
        for edge in structural_edges
        if _edge_type(edge) == "prov:wasDerivedFrom"
    }
    structural_edge_keys = {
        (_edge_src(edge), _edge_type(edge), _edge_dst(edge))
        for edge in structural_edges
        if _edge_src(edge) and _edge_dst(edge)
    }

    dead_endpoints: list[dict[str, Any]] = []
    missing_topology: list[dict[str, Any]] = []
    for claim in relation_claims:
        subject = claim["subject_id"]
        object_id = claim["object_id"]
        predicate = claim["predicate"]
        if subject not in primitive_ids or object_id not in primitive_ids:
            dead_endpoints.append(claim)
        if (subject, predicate, object_id) not in edge_keys:
            missing_topology.append(claim)

    unanchored: list[dict[str, str]] = []
    invalid_claim_object_shapes: list[dict[str, str]] = []
    missing_claim_bridges: list[dict[str, str]] = []
    missing_claim_provenance: list[dict[str, str]] = []
    for claim_id, node in node_by_id.items():
        if _node_type(node) != "Claim":
            continue
        facets = _facets(node)
        subject_id = _optional_text(facets.get("S_id"))
        object_id = _optional_text(facets.get("O_id"))
        has_literal = facets.get("O_literal") is not None
        if subject_id is None or ((object_id is not None) == has_literal):
            invalid_claim_object_shapes.append({"claim_id": claim_id})
        if subject_id is None or (claim_id, "rdf:subject", subject_id) not in structural_edge_keys:
            missing_claim_bridges.append(
                {"claim_id": claim_id, "edge_type": "rdf:subject", "target_id": subject_id or ""}
            )
        if (
            object_id is not None
            and (claim_id, "rdf:object", object_id) not in structural_edge_keys
        ):
            missing_claim_bridges.append(
                {"claim_id": claim_id, "edge_type": "rdf:object", "target_id": object_id}
            )
        block_id = str(facets.get("block_id") or "")
        block = node_by_id.get(block_id)
        block_facets = _facets(block) if block is not None else {}
        has_block = block is not None and node_type_by_id.get(block_id) == "Block"
        has_prov_edge = (claim_id, block_id) in prov_anchors
        has_source_anchor = _valid_source_anchor(block_facets)
        if not (has_block and has_prov_edge and has_source_anchor):
            unanchored.append({"claim_id": claim_id, "block_id": block_id})
        for facet_key, edge_type, expected_type in (
            ("extraction_activity_id", "prov:wasGeneratedBy", "Activity"),
            ("agent_id", "prov:wasAttributedTo", "Agent"),
        ):
            target_id = _optional_text(facets.get(facet_key))
            if (
                target_id is None
                or all_node_type_by_id.get(target_id) != expected_type
                or (claim_id, edge_type, target_id) not in structural_edge_keys
            ):
                missing_claim_provenance.append(
                    {
                        "claim_id": claim_id,
                        "edge_type": edge_type,
                        "target_id": target_id or "",
                    }
                )

    topology_edges_all = [
        edge
        for edge in edges
        if _edge_src(edge) in primitive_ids and _edge_dst(edge) in primitive_ids
    ]
    active_claim_keys = {
        (claim["subject_id"], claim["predicate"], claim["object_id"]) for claim in relation_claims
    }
    historical_claim_keys = {
        (claim["subject_id"], claim["predicate"], claim["object_id"])
        for claim in historical_relation_claims
    }
    all_claim_keys = active_claim_keys | historical_claim_keys
    topology_without_claims = [
        {
            "src": _edge_src(edge),
            "predicate": _edge_type(edge),
            "dst": _edge_dst(edge),
        }
        for edge in topology_edges_all
        if (_edge_src(edge), _edge_type(edge), _edge_dst(edge)) not in all_claim_keys
    ]
    inactive_topology_edges = [
        {
            "src": _edge_src(edge),
            "predicate": _edge_type(edge),
            "dst": _edge_dst(edge),
        }
        for edge in topology_edges_all
        if (_edge_src(edge), _edge_type(edge), _edge_dst(edge)) in historical_claim_keys
        and (_edge_src(edge), _edge_type(edge), _edge_dst(edge)) not in active_claim_keys
    ]
    topology_edges = [
        edge
        for edge in topology_edges_all
        if (_edge_src(edge), _edge_type(edge), _edge_dst(edge)) in active_claim_keys
    ]
    semantic_degree: Counter[str] = Counter()
    self_loops: list[dict[str, str]] = []
    for edge in topology_edges:
        src = _edge_src(edge)
        dst = _edge_dst(edge)
        semantic_degree[src] += 1
        semantic_degree[dst] += 1
        if src == dst:
            self_loops.append({"src": src, "predicate": _edge_type(edge), "dst": dst})
    topology_isolated = [
        {
            "id": node_id,
            "title": _node_title(node_by_id[node_id]),
            "type": node_type_by_id[node_id],
        }
        for node_id in sorted(primitive_ids)
        if semantic_degree[node_id] == 0
    ]
    mentioned_entity_ids = {
        _edge_dst(edge)
        for edge in edges
        if _edge_type(edge) == "schema:mentions" and _edge_dst(edge) in primitive_ids
    }
    knowledge_connected_ids = live_claim_entity_ids | mentioned_entity_ids
    orphan_entities = [
        {
            "id": node_id,
            "title": _node_title(node_by_id[node_id]),
            "type": node_type_by_id[node_id],
        }
        for node_id in sorted(primitive_ids)
        if node_id not in knowledge_connected_ids
    ]
    top_degree = [
        {
            "id": node_id,
            "title": _node_title(node_by_id[node_id]),
            "type": node_type_by_id[node_id],
            "semantic_degree": degree,
        }
        for node_id, degree in sorted(
            semantic_degree.items(), key=lambda item: (-item[1], item[0])
        )[:sample_limit]
    ]
    return {
        "relation_claims": len(relation_claims),
        "materialized_topology_edges": len(topology_edges),
        "materialized_topology_edge_types": {
            "status": "measured",
            "counts": dict(sorted(Counter(_edge_type(edge) for edge in topology_edges).items())),
        },
        "dead_endpoint_claims": {
            "count": len(dead_endpoints),
            "samples": dead_endpoints[:sample_limit],
        },
        "missing_topology_edges": {
            "count": len(missing_topology),
            "samples": missing_topology[:sample_limit],
        },
        "topology_edges_without_claims": {
            "count": len(topology_without_claims),
            "samples": topology_without_claims[:sample_limit],
        },
        "inactive_topology_edges": {
            "count": len(inactive_topology_edges),
            "samples": inactive_topology_edges[:sample_limit],
        },
        "unanchored_claims": {
            "count": len(unanchored),
            "samples": unanchored[:sample_limit],
        },
        "invalid_claim_object_shapes": {
            "count": len(invalid_claim_object_shapes),
            "samples": invalid_claim_object_shapes[:sample_limit],
        },
        "missing_claim_bridges": {
            "count": len(missing_claim_bridges),
            "samples": missing_claim_bridges[:sample_limit],
        },
        "missing_claim_provenance": {
            "count": len(missing_claim_provenance),
            "samples": missing_claim_provenance[:sample_limit],
        },
        "self_loop_edges": {"count": len(self_loops), "samples": self_loops[:sample_limit]},
        "topology_isolated_entities": {
            "count": len(topology_isolated),
            "share": round(len(topology_isolated) / len(primitive_ids), 6)
            if primitive_ids
            else 0.0,
            "samples": topology_isolated[:sample_limit],
        },
        "isolated_entities": {
            "definition": "no active Claim endpoint and no schema:mentions evidence",
            "count": len(orphan_entities),
            "share": round(len(orphan_entities) / len(primitive_ids), 6) if primitive_ids else 0.0,
            "samples": orphan_entities[:sample_limit],
        },
        "top_degree_entities": top_degree,
    }


def _surface_flags(title: str) -> tuple[str, ...]:
    return surface_normalization_flags(title)


def _valid_source_anchor(facets: Mapping[str, Any]) -> bool:
    source_path = facets.get("source_path")
    byte_start = facets.get("byte_start")
    byte_end = facets.get("byte_end")
    content_hash = facets.get("content_hash")
    return (
        isinstance(source_path, str)
        and bool(source_path.strip())
        and isinstance(byte_start, int)
        and not isinstance(byte_start, bool)
        and isinstance(byte_end, int)
        and not isinstance(byte_end, bool)
        and 0 <= byte_start <= byte_end
        and isinstance(content_hash, str)
        and _CONTENT_HASH.fullmatch(content_hash) is not None
    )


def _semantic_snapshot(
    node_by_id: Mapping[str, Any],
    primitive_ids: set[str],
    *,
    graph_generation: object,
    fingerprints: Mapping[str, Any],
    measured: bool,
    reason: str,
) -> dict[str, Any]:
    """Build an opaque, complete semantic population snapshot for churn gates."""

    if not measured:
        return {
            "schema_version": SEMANTIC_SNAPSHOT_SCHEMA,
            "status": "not_measured",
            "graph_generation": graph_generation,
            "reason": reason,
            "dimensions": {},
        }

    identity_values = _semantic_identity_values(node_by_id, primitive_ids)
    identities = [
        _opaque_semantic_member("identity", identity_values[node_id]) for node_id in primitive_ids
    ]
    predicates: set[str] = set()
    relations: list[str] = []
    for node in node_by_id.values():
        if _node_type(node) != "Claim" or is_superseded(node):
            continue
        predicate = str(_facets(node).get("P") or "").strip()
        if predicate not in _STRUCTURAL_INGEST_PREDICATES:
            predicates.add(predicate)
            relations.append(
                _opaque_semantic_member(
                    "relation",
                    _semantic_claim_value(
                        node,
                        node_by_id=node_by_id,
                        identity_values=identity_values,
                    ),
                )
            )
    dimensions = {
        "identities": _snapshot_dimension(identities),
        "predicates": _snapshot_dimension(
            _opaque_semantic_member("predicate", predicate) for predicate in predicates
        ),
        "relations": _snapshot_dimension(relations),
    }
    return {
        "schema_version": SEMANTIC_SNAPSHOT_SCHEMA,
        "status": "measured",
        "graph_generation": graph_generation,
        "member_encoding": SEMANTIC_MEMBER_ENCODING,
        "fingerprints": dict(fingerprints),
        "dimensions": dimensions,
    }


def _opaque_semantic_member(kind: str, value: object) -> str:
    digest = hashlib.sha256()
    digest.update(kind.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(str(value).encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def _semantic_identity_values(
    node_by_id: Mapping[str, Any],
    primitive_ids: set[str],
) -> dict[str, str]:
    """Return vault-independent identity values while retaining multiplicity.

    Physical node ids include extractor-authored descriptive content.  They are
    stable inside one vault but are not semantic identities across fresh runs.
    Grouping by the conservative type/surface key makes cross-vault scenario
    arms comparable.  The ordinal retains duplicate multiplicity so an
    unresolved same-type duplicate cannot disappear from the population set.
    """

    grouped: dict[str, list[str]] = defaultdict(list)
    for node_id in primitive_ids:
        node = node_by_id[node_id]
        grouped[_semantic_node_value(node)].append(node_id)

    values: dict[str, str] = {}
    for base_value, node_ids in sorted(grouped.items()):
        for ordinal, node_id in enumerate(sorted(node_ids), start=1):
            values[node_id] = _canonical_semantic_value(
                {"identity": json.loads(base_value), "ordinal": ordinal}
            )
    return values


def _semantic_node_value(node: Any) -> str:
    return _canonical_semantic_value(
        {
            "type": _node_type(node),
            "surface": exact_surface_key(_node_title(node)),
        }
    )


def _semantic_claim_value(
    claim: Any,
    *,
    node_by_id: Mapping[str, Any],
    identity_values: Mapping[str, str],
) -> str:
    facets = _facets(claim)

    def endpoint_value(node_id: object) -> object:
        identifier = str(node_id or "")
        if identifier in identity_values:
            return json.loads(identity_values[identifier])
        node = node_by_id.get(identifier)
        if node is not None:
            return json.loads(_semantic_node_value(node))
        # Missing endpoints are rejected by a hard invariant.  Keep the churn
        # value deterministic without leaking or depending on a physical id.
        return {"missing": True}

    if facets.get("O_id") is not None:
        object_value: object = {
            "kind": "entity",
            "value": endpoint_value(facets.get("O_id")),
        }
    else:
        literal = facets.get("O_literal")
        object_value = {
            "kind": "literal",
            "type": type(literal).__name__,
            "value": literal,
        }
    return _canonical_semantic_value(
        {
            "subject": endpoint_value(facets.get("S_id")),
            "predicate": str(facets.get("P") or "").strip(),
            "object": object_value,
        }
    )


def _canonical_semantic_value(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _snapshot_dimension(values: Iterable[str]) -> dict[str, Any]:
    members = sorted(set(values))
    digest = hashlib.sha256()
    for member in members:
        digest.update(member.encode("ascii"))
        digest.update(b"\n")
    return {
        "count": len(members),
        "sha256": f"sha256:{digest.hexdigest()}",
        "members": members,
    }


def _validated_semantic_snapshot(
    value: Mapping[str, Any],
    *,
    name: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} semantic snapshot must be a mapping")
    if value.get("schema_version") != SEMANTIC_SNAPSHOT_SCHEMA:
        raise ValueError(
            f"{name} semantic snapshot schema_version must be {SEMANTIC_SNAPSHOT_SCHEMA}"
        )
    if value.get("status") != "measured":
        raise ValueError(f"{name} semantic snapshot must be measured")
    if value.get("member_encoding") != SEMANTIC_MEMBER_ENCODING:
        raise ValueError(
            f"{name} semantic snapshot member_encoding must be {SEMANTIC_MEMBER_ENCODING}"
        )
    generation = value.get("graph_generation")
    if not isinstance(generation, str) or not generation.strip():
        raise ValueError(f"{name} semantic snapshot graph_generation must be non-empty")
    raw_dimensions = value.get("dimensions")
    if not isinstance(raw_dimensions, Mapping) or set(raw_dimensions) != {
        "identities",
        "predicates",
        "relations",
    }:
        raise ValueError(f"{name} semantic snapshot dimensions are invalid")
    raw_fingerprints = value.get("fingerprints")
    if not isinstance(raw_fingerprints, Mapping):
        raise ValueError(f"{name} semantic snapshot fingerprints are invalid")
    fingerprint_status = raw_fingerprints.get("status")
    if fingerprint_status == "measured":
        if set(raw_fingerprints) != {
            "status",
            "config",
            "extraction",
            "semantic_policy",
        } or any(
            not isinstance(raw_fingerprints.get(key), str)
            or not _SHA256_ID.fullmatch(str(raw_fingerprints[key]))
            for key in ("config", "extraction", "semantic_policy")
        ):
            raise ValueError(f"{name} semantic snapshot fingerprints are invalid")
        fingerprints = dict(raw_fingerprints)
    elif fingerprint_status == "not_measured" and set(raw_fingerprints) == {
        "status",
        "reason",
    }:
        fingerprints = dict(raw_fingerprints)
    else:
        raise ValueError(f"{name} semantic snapshot fingerprints are invalid")

    dimensions: dict[str, dict[str, Any]] = {}
    for dimension in ("identities", "predicates", "relations"):
        raw = raw_dimensions[dimension]
        if not isinstance(raw, Mapping):
            raise ValueError(f"{name}.{dimension} must be a mapping")
        members = raw.get("members")
        if (
            not isinstance(members, list)
            or any(
                not isinstance(member, str) or not _SHA256_ID.fullmatch(member)
                for member in members
            )
            or members != sorted(set(members))
        ):
            raise ValueError(f"{name}.{dimension}.members must be sorted unique SHA-256 ids")
        if raw.get("count") != len(members):
            raise ValueError(f"{name}.{dimension}.count does not match members")
        expected = _snapshot_dimension(members)["sha256"]
        if raw.get("sha256") != expected:
            raise ValueError(f"{name}.{dimension}.sha256 does not match members")
        dimensions[dimension] = {
            "count": len(members),
            "sha256": expected,
            "members": members,
        }
    return {
        "graph_generation": generation,
        "fingerprints": fingerprints,
        **dimensions,
    }


def _semantic_snapshot_fingerprints(
    *,
    config: str | None,
    extraction: str | None,
    semantic_policy: str | None,
) -> dict[str, Any]:
    values = {
        "config": config,
        "extraction": extraction,
        "semantic_policy": semantic_policy,
    }
    supplied = {key: value for key, value in values.items() if value is not None}
    for key, value in supplied.items():
        if not isinstance(value, str) or not _SHA256_ID.fullmatch(value):
            raise ValueError(f"{key}_fingerprint must be a sha256 identifier")
    if len(supplied) != len(values):
        return {
            "status": "not_measured",
            "reason": "complete config, extraction, and semantic-policy fingerprints were not supplied",
        }
    return {"status": "measured", **values}


def _check(
    code: str,
    ok: bool,
    count: int,
    samples: list[Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "status": "passed" if ok else "failed",
        "count": int(count),
        "samples": list(samples or []),
    }


def _unmeasured(code: str, reason: str) -> dict[str, Any]:
    return {
        "code": code,
        "status": "not_measured",
        "count": None,
        "samples": [],
        "reason": reason,
    }


def _node_id(node: Any) -> str:
    return str(getattr(node, "id", "") or "")


def _node_type(node: Any) -> str:
    return str(getattr(node, "type", "") or "")


def _node_title(node: Any) -> str:
    return str(getattr(node, "title", None) or getattr(node, "name", "") or "")


def _facets(node: Any) -> dict[str, Any]:
    raw = getattr(node, "facets", None)
    return dict(raw) if isinstance(raw, Mapping) else {}


def _edge_src(edge: Any) -> str:
    return str(getattr(edge, "src", "") or "")


def _edge_dst(edge: Any) -> str:
    return str(getattr(edge, "dst", "") or "")


def _edge_type(edge: Any) -> str:
    return str(getattr(edge, "type", "") or "")


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _required_text(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} must be a non-empty string")
    return text


def _selected_record_sha256(records: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for record in records:
        digest.update(
            json.dumps(
                dict(record),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _external_input_evidence(
    *,
    registered_predicates: tuple[str, ...] | None,
    recall_samples: tuple[Mapping[str, Any], ...] | None,
    adjudication: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Bind every caller-supplied semantic input without projecting raw values."""

    return {
        "registered_predicates": (
            {"status": "not_supplied"}
            if registered_predicates is None
            else evidence_identity(
                registered_predicates,
                logical_type="registered_predicate_set.v1",
            )
        ),
        "recall_samples": (
            {"status": "not_supplied"}
            if recall_samples is None
            else evidence_identity(recall_samples, logical_type="recall_cost.v1[]")
        ),
        "adjudication": (
            {"status": "not_supplied"}
            if adjudication is None
            else evidence_identity(
                adjudication,
                logical_type="semantic_adjudication.v1",
            )
        ),
    }


def _materialize_registered_predicates(
    values: Iterable[str] | None,
) -> tuple[str, ...] | None:
    if values is None:
        return None
    materialized = tuple(values)
    for value in materialized:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError("registered_predicates must contain only non-empty, trimmed strings")
    return tuple(sorted(set(materialized)))


def _ledger_candidate_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    if not isinstance(payload, Mapping):
        return {}
    candidate = payload.get("candidate")
    if isinstance(candidate, Mapping):
        return dict(candidate)
    return dict(payload)


def _ledger_proposals(
    records: Iterable[Mapping[str, Any]],
    *,
    candidate_kind: str,
) -> list[dict[str, Any]]:
    """Return one stable proposed observation per candidate id (last row wins)."""

    by_id: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(records):
        if (
            record.get("kind") != "candidate"
            or record.get("candidate_kind") != candidate_kind
            or record.get("state") != "proposed"
        ):
            continue
        candidate_id = str(record.get("candidate_id") or f"missing-candidate-id:{index}")
        by_id[candidate_id] = {
            "candidate_id": candidate_id,
            "run_id": str(record.get("run_id") or ""),
            "payload": _ledger_candidate_payload(record),
        }
    return [by_id[candidate_id] for candidate_id in sorted(by_id)]


def _ledger_plan_operations(plan_rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    for plan in plan_rows:
        raw_operations = plan.get("operations")
        if not isinstance(raw_operations, list):
            continue
        for operation_index, raw_operation in enumerate(raw_operations):
            if not isinstance(raw_operation, Mapping):
                continue
            operations.append(
                {
                    **dict(raw_operation),
                    "_run_id": str(plan.get("run_id") or ""),
                    "_plan_id": str(plan.get("plan_id") or ""),
                    "_operation_index": operation_index,
                }
            )
    return operations


def _ledger_plan_shape_issues(
    plan_rows: Iterable[Mapping[str, Any]],
    *,
    sample_limit: int,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    for plan_index, plan in enumerate(plan_rows):
        plan_id = str(plan.get("plan_id") or "")
        if not plan_id:
            issues.append({"plan_index": plan_index, "reason": "plan_id is missing"})
        operations = plan.get("operations")
        if not isinstance(operations, list):
            issues.append(
                {
                    "plan_id": plan_id,
                    "plan_index": plan_index,
                    "reason": "operations must be a list",
                }
            )
            continue
        for operation_index, operation in enumerate(operations):
            prefix = {
                "plan_id": plan_id,
                "operation_index": operation_index,
            }
            if not isinstance(operation, Mapping):
                issues.append({**prefix, "reason": "operation must be a mapping"})
                continue
            for field in ("operation", "candidate_id"):
                if not str(operation.get(field) or "").strip():
                    issues.append({**prefix, "reason": f"{field} is missing"})
            if operation.get("candidate_kind") not in {"node", "edge"}:
                issues.append({**prefix, "reason": "candidate_kind must be node or edge"})
    return {"count": len(issues), "samples": issues[:sample_limit]}


def _ledger_surface_layer(
    proposed_nodes: list[dict[str, Any]],
    *,
    sample_limit: int,
) -> dict[str, Any]:
    members: list[dict[str, str]] = []
    flag_counts: Counter[str] = Counter()
    flag_samples: dict[str, list[str]] = defaultdict(list)
    titles_by_type: dict[str, list[str]] = defaultdict(list)
    missing_surface_count = 0
    invalid_surface: list[dict[str, Any]] = []
    for proposal in proposed_nodes:
        payload = proposal["payload"]
        title = str(payload.get("title") or "")
        type_ = str(payload.get("type") or "")
        source_surface = title
        exact_key = exact_surface_key(title)
        discovery_key = discovery_surface_key(title)
        flags = _surface_flags(title)
        raw_surface = payload.get("surface")
        if not isinstance(raw_surface, Mapping):
            missing_surface_count += 1
        else:
            raw_source = raw_surface.get("source_surface")
            raw_exact = raw_surface.get("exact_key")
            raw_discovery = raw_surface.get("discovery_key")
            raw_canonical = raw_surface.get("canonical_title")
            raw_version = raw_surface.get("normalizer_version")
            raw_aliases = raw_surface.get("aliases")
            raw_flags = raw_surface.get("normalization_flags")
            reasons: list[str] = []
            if not isinstance(raw_source, str):
                reasons.append("source_surface is not a string")
            else:
                source_surface = raw_source
                exact_key = exact_surface_key(raw_source)
                discovery_key = discovery_surface_key(raw_source)
                flags = _surface_flags(raw_source)
            if raw_version != NORMALIZER_VERSION:
                reasons.append("normalizer_version mismatch")
            if raw_exact != exact_key:
                reasons.append("exact_key mismatch")
            if raw_discovery != discovery_key:
                reasons.append("discovery_key mismatch")
            if raw_canonical != title:
                reasons.append("canonical_title mismatch")
            if not isinstance(raw_aliases, (list, tuple)) or any(
                not isinstance(alias, str) for alias in raw_aliases
            ):
                reasons.append("aliases must be strings")
            if not isinstance(raw_flags, (list, tuple)) or tuple(raw_flags) != flags:
                reasons.append("normalization_flags mismatch")
            if reasons:
                invalid_surface.append(
                    {
                        "candidate_id": proposal["candidate_id"],
                        "reasons": reasons,
                    }
                )
        member = {
            "candidate_id": proposal["candidate_id"],
            "title": title,
            "type": type_,
            "source_surface": source_surface,
            "exact_key": exact_key,
            "discovery_key": discovery_key,
        }
        members.append(member)
        titles_by_type[type_].append(title)
        for flag in flags:
            flag_counts[flag] += 1
            if len(flag_samples[flag]) < sample_limit:
                flag_samples[flag].append(source_surface)

    exact_groups = _ledger_collision_groups(
        members,
        key_name="exact_key",
        sample_limit=sample_limit,
    )
    discovery_groups = _ledger_collision_groups(
        members,
        key_name="discovery_key",
        sample_limit=sample_limit,
    )
    invalid_count = len(invalid_surface)
    if not members:
        preservation = {
            "status": "not_measured",
            "reason": "the selected runs contain no proposed node candidates",
            "observed_count": 0,
            "missing_count": 0,
            "invalid_count": 0,
            "invalid_samples": [],
        }
    elif invalid_surface:
        preservation = {
            "status": "incomplete",
            "reason": "one or more surface evidence records failed contract validation",
            "observed_count": len(members) - missing_surface_count,
            "missing_count": missing_surface_count,
            "invalid_count": invalid_count,
            "invalid_samples": invalid_surface[:sample_limit],
        }
    elif missing_surface_count:
        preservation = {
            "status": "not_measured",
            "reason": "one or more candidate rows predate separate surface evidence",
            "observed_count": len(members) - missing_surface_count,
            "missing_count": missing_surface_count,
            "invalid_count": 0,
            "invalid_samples": [],
        }
    else:
        preservation = {
            "status": "measured",
            "observed_count": len(members),
            "missing_count": 0,
            "invalid_count": 0,
            "invalid_samples": [],
        }
    return {
        "normalizer_version": NORMALIZER_VERSION,
        "proposed_candidates": len(members),
        "distinct_exact_keys": len({row["exact_key"] for row in members if row["exact_key"]}),
        "distinct_discovery_keys": len(
            {row["discovery_key"] for row in members if row["discovery_key"]}
        ),
        "candidate_surface_samples": members[:sample_limit],
        "same_type_exact_duplicate_groups": exact_groups["same_type"],
        "cross_type_exact_conflict_groups": exact_groups["cross_type"],
        "same_type_discovery_collision_groups": discovery_groups["same_type"],
        "cross_type_discovery_collision_groups": discovery_groups["cross_type"],
        "normalization_flags": {
            "counts": dict(sorted(flag_counts.items())),
            "samples": {key: values for key, values in sorted(flag_samples.items())},
        },
        "sample_titles_by_type": {
            type_: sorted(titles, key=str.casefold)[:sample_limit]
            for type_, titles in sorted(titles_by_type.items())
        },
        "source_surface_preservation": preservation,
    }


def _ledger_collision_groups(
    members: list[dict[str, str]],
    *,
    key_name: str,
    sample_limit: int,
) -> dict[str, dict[str, Any]]:
    by_key: dict[str, list[dict[str, str]]] = defaultdict(list)
    for member in members:
        key = member[key_name]
        if key:
            by_key[key].append(member)

    same_type: list[dict[str, Any]] = []
    cross_type: list[dict[str, Any]] = []
    for key, grouped_members in by_key.items():
        by_type: dict[str, list[dict[str, str]]] = defaultdict(list)
        for member in grouped_members:
            by_type[member["type"]].append(member)
        for type_, typed_members in by_type.items():
            if len(typed_members) > 1:
                same_type.append(
                    {
                        "key": key,
                        "type": type_,
                        "count": len(typed_members),
                        "members": typed_members[:sample_limit],
                    }
                )
        if len(by_type) > 1:
            cross_type.append(
                {
                    "key": key,
                    "types": sorted(by_type),
                    "count": len(grouped_members),
                    "members": grouped_members[:sample_limit],
                }
            )
    same_type.sort(key=lambda row: (row["key"], row["type"]))
    cross_type.sort(key=lambda row: row["key"])
    return {
        "same_type": {"count": len(same_type), "samples": same_type[:sample_limit]},
        "cross_type": {"count": len(cross_type), "samples": cross_type[:sample_limit]},
    }


def _ledger_type_layer(
    proposed_nodes: list[dict[str, Any]],
    planned_node_operations: list[dict[str, Any]],
    *,
    surface_layer: Mapping[str, Any],
    sample_limit: int,
) -> dict[str, Any]:
    proposed_types = [str(row["payload"].get("type") or "") for row in proposed_nodes]
    planned_create = [
        row for row in planned_node_operations if row.get("operation") == "create_node"
    ]
    planned_types = [str(row.get("type") or "") for row in planned_create]
    proposed_invalid = [
        {
            "candidate_id": row["candidate_id"],
            "type": str(row["payload"].get("type") or ""),
            "title": str(row["payload"].get("title") or ""),
        }
        for row in proposed_nodes
        if str(row["payload"].get("type") or "") not in PRIMITIVE_NAMES
    ]
    planned_invalid = [
        {
            "candidate_id": str(row.get("candidate_id") or ""),
            "type": str(row.get("type") or ""),
            "title": str(row.get("title") or ""),
            "plan_id": row.get("_plan_id"),
        }
        for row in planned_create
        if str(row.get("type") or "") not in PRIMITIVE_NAMES
    ]
    by_operation: dict[str, Counter[str]] = defaultdict(Counter)
    for row in planned_node_operations:
        by_operation[str(row.get("operation") or "")][str(row.get("type") or "")] += 1
    return {
        "proposed": {
            "count": len(proposed_nodes),
            "primitive_counts": dict(sorted(Counter(proposed_types).items())),
            "invalid_entity_types": {
                "count": len(proposed_invalid),
                "samples": proposed_invalid[:sample_limit],
            },
        },
        "planned_create": {
            "count": len(planned_create),
            "primitive_counts": dict(sorted(Counter(planned_types).items())),
            "invalid_entity_types": {
                "count": len(planned_invalid),
                "samples": planned_invalid[:sample_limit],
            },
        },
        "planned_operations": {
            "count": len(planned_node_operations),
            "type_counts_by_operation": {
                operation: dict(sorted(counts.items()))
                for operation, counts in sorted(by_operation.items())
            },
        },
        "cross_type_exact_conflict_groups": surface_layer["cross_type_exact_conflict_groups"],
        "type_accuracy": {
            "status": "not_measured",
            "reason": "no human-adjudicated primitive labels were supplied",
        },
    }


def _ledger_predicate_layer(
    proposed_edges: list[dict[str, Any]],
    planned_relation_operations: list[dict[str, Any]],
    *,
    registered_predicates: Iterable[str] | None,
    sample_limit: int,
) -> dict[str, Any]:
    raw_proposed_labels = [
        str(row["payload"].get("type") or "").strip()
        for row in proposed_edges
        if row["payload"].get("derived") is not True
    ]
    proposed_labels = [str(row["payload"].get("type") or "").strip() for row in proposed_edges]
    planned_accept = [
        row for row in planned_relation_operations if row.get("operation") == "create_edge_or_claim"
    ]
    planned_labels = [str(row.get("type") or "").strip() for row in planned_accept]
    raw_proposed = _ledger_predicate_metrics(
        raw_proposed_labels,
        registered_predicates=registered_predicates,
        sample_limit=sample_limit,
    )
    proposed = _ledger_predicate_metrics(
        proposed_labels,
        registered_predicates=registered_predicates,
        sample_limit=sample_limit,
    )
    planned = _ledger_predicate_metrics(
        planned_labels,
        registered_predicates=registered_predicates,
        sample_limit=sample_limit,
    )
    raw_vocabulary = raw_proposed["raw_vocabulary_size"]
    proposed_vocabulary = proposed["raw_vocabulary_size"]
    planned_vocabulary = planned["raw_vocabulary_size"]
    compression = {
        "status": "measured" if raw_vocabulary else "not_measured",
        "reason": None if raw_vocabulary else "no raw proposed predicates were observed",
        "raw_proposed_vocabulary_size": raw_vocabulary,
        "proposed_vocabulary_size": proposed_vocabulary,
        "planned_vocabulary_size": planned_vocabulary,
        "raw_to_planned_vocabulary_reduction": raw_vocabulary - planned_vocabulary,
        "proposed_to_planned_vocabulary_reduction": (proposed_vocabulary - planned_vocabulary),
        "planned_to_raw_ratio": (
            round(planned_vocabulary / raw_vocabulary, 6) if raw_vocabulary else None
        ),
        "planned_to_proposed_ratio": (
            round(planned_vocabulary / proposed_vocabulary, 6) if proposed_vocabulary else None
        ),
    }
    return {
        "raw_proposed": raw_proposed,
        "proposed": proposed,
        "planned_accept": planned,
        "compression": compression,
        "syntax_policy": {
            "status": "not_measured",
            "reason": "predicate syntax belongs to the Phase 3 registry contract",
        },
    }


def _ledger_predicate_metrics(
    labels: list[str],
    *,
    registered_predicates: Iterable[str] | None,
    sample_limit: int,
) -> dict[str, Any]:
    counts = Counter(labels)
    singletons = sorted(label for label, count in counts.items() if count == 1)
    placeholders = [label for label in labels if label.casefold() in PLACEHOLDER_PREDICATES]
    if registered_predicates is None:
        registry_coverage = {
            "status": "not_measured",
            "reason": "predicate registry is not implemented",
            "unregistered_count": None,
            "unregistered_samples": [],
        }
    else:
        registered = {str(value) for value in registered_predicates}
        unregistered = sorted(set(counts) - registered - _STRUCTURAL_INGEST_PREDICATES)
        structural = sorted(set(counts) & _STRUCTURAL_INGEST_PREDICATES)
        registry_coverage = {
            "status": "measured",
            "reason": None,
            "unregistered_count": len(unregistered),
            "unregistered_samples": unregistered[:sample_limit],
            "excluded_structural_predicates": structural,
        }
    return {
        "predicate_assertions": len(labels),
        "raw_vocabulary_size": len(counts),
        "raw_vocabulary_per_1000_assertions": (
            round((len(counts) * 1000) / len(labels), 3) if labels else 0.0
        ),
        "predicate_counts": dict(
            sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:sample_limit]
        ),
        "singletons": {
            "count": len(singletons),
            "share": round(len(singletons) / len(counts), 6) if counts else 0.0,
            "samples": singletons[:sample_limit],
        },
        "placeholder_predicates": {
            "count": len(placeholders),
            "samples": sorted(set(placeholders))[:sample_limit],
        },
        "registry_coverage": registry_coverage,
    }


def _ledger_relation_layer(
    proposed_nodes: list[dict[str, Any]],
    proposed_edges: list[dict[str, Any]],
    planned_node_operations: list[dict[str, Any]],
    planned_relation_operations: list[dict[str, Any]],
    *,
    sample_limit: int,
) -> dict[str, Any]:
    action_names = {
        "create_edge_or_claim": "accept",
        "queue_review": "queue",
        "dead_letter": "reject",
        "supersede_candidate": "supersede",
    }
    decisions: Counter[str] = Counter()
    unknown_operations: list[dict[str, Any]] = []
    for operation in planned_relation_operations:
        action = str(operation.get("operation") or "")
        decision = action_names.get(action)
        if decision is None:
            decisions["unknown"] += 1
            unknown_operations.append(
                {
                    "plan_id": operation.get("_plan_id"),
                    "candidate_id": operation.get("candidate_id"),
                    "operation": action,
                }
            )
        else:
            decisions[decision] += 1

    proposed_by_id = {row["candidate_id"]: row["payload"] for row in proposed_edges}
    candidate_node_ids = {row["candidate_id"] for row in proposed_nodes}
    planned_create_node_ids = {
        str(row.get("candidate_id") or "")
        for row in planned_node_operations
        if row.get("operation") == "create_node" and str(row.get("candidate_id") or "")
    }
    accepted = [
        row for row in planned_relation_operations if row.get("operation") == "create_edge_or_claim"
    ]
    literal_topology = Counter()
    invalid_shapes: list[dict[str, Any]] = list(unknown_operations)
    anchor_valid: list[str] = []
    anchor_absent: list[str] = []
    anchor_invalid: list[dict[str, Any]] = []
    anchor_unavailable: list[str] = []
    internal_refs: list[dict[str, str]] = []
    observed_unplanned_refs: list[dict[str, str]] = []
    external_refs: list[dict[str, str]] = []
    missing_refs: list[dict[str, str]] = []

    for operation in accepted:
        candidate_id = str(operation.get("candidate_id") or "")
        payload = proposed_by_id.get(candidate_id)
        combined = {**(payload or {}), **operation}
        src_ref = str(combined.get("src_ref") or "")
        dst_ref = str(combined.get("dst_ref") or "")
        has_literal = combined.get("dst_literal") is not None
        has_topology = bool(dst_ref)
        if has_literal == has_topology:
            literal_topology["invalid"] += 1
            invalid_shapes.append(
                {
                    "plan_id": operation.get("_plan_id"),
                    "candidate_id": candidate_id,
                    "reason": "relation must have exactly one of dst_ref or dst_literal",
                }
            )
        elif has_literal:
            literal_topology["literal"] += 1
        else:
            literal_topology["topology"] += 1

        if not src_ref:
            missing_refs.append({"candidate_id": candidate_id, "slot": "src_ref"})
        else:
            _classify_ledger_ref(
                src_ref,
                candidate_id=candidate_id,
                slot="src_ref",
                candidate_node_ids=candidate_node_ids,
                planned_create_node_ids=planned_create_node_ids,
                internal=internal_refs,
                observed_unplanned=observed_unplanned_refs,
                external=external_refs,
            )
        if has_topology:
            _classify_ledger_ref(
                dst_ref,
                candidate_id=candidate_id,
                slot="dst_ref",
                candidate_node_ids=candidate_node_ids,
                planned_create_node_ids=planned_create_node_ids,
                internal=internal_refs,
                observed_unplanned=observed_unplanned_refs,
                external=external_refs,
            )

        if payload is None:
            anchor_unavailable.append(candidate_id)
        else:
            anchor_status = _candidate_anchor_status(payload)
            if anchor_status == "valid":
                anchor_valid.append(candidate_id)
            elif anchor_status == "absent":
                anchor_absent.append(candidate_id)
            else:
                anchor_invalid.append(
                    {
                        "candidate_id": candidate_id,
                        "block_id": payload.get("block_id"),
                        "byte_start": payload.get("byte_start"),
                        "byte_end": payload.get("byte_end"),
                        "content_hash": payload.get("content_hash"),
                    }
                )

    invalid_shapes.extend(missing_refs)
    return {
        "planned_relations": len(planned_relation_operations),
        "planned_decisions": {
            "accept": decisions["accept"],
            "queue": decisions["queue"],
            "reject": decisions["reject"],
            "supersede": decisions["supersede"],
            "unknown": decisions["unknown"],
        },
        "accepted_object_kind": {
            "literal": literal_topology["literal"],
            "topology": literal_topology["topology"],
            "invalid": literal_topology["invalid"],
        },
        "accepted_anchor_shape": {
            "status": "measured",
            "valid_count": len(anchor_valid),
            "absent_count": len(anchor_absent),
            "invalid_count": len(anchor_invalid),
            "unavailable_count": len(anchor_unavailable),
            "absent_samples": anchor_absent[:sample_limit],
            "invalid_samples": anchor_invalid[:sample_limit],
            "unavailable_samples": anchor_unavailable[:sample_limit],
        },
        "endpoint_resolution": {
            "internal_candidate_refs": {
                "status": "measured",
                "count": len(internal_refs),
                "resolved_count": len(internal_refs),
                "samples": internal_refs[:sample_limit],
                "definition": "endpoint candidate ids selected for create_node in the same plan set",
            },
            "observed_candidate_refs_not_planned_create": {
                "status": "not_measured",
                "count": len(observed_unplanned_refs),
                "samples": observed_unplanned_refs[:sample_limit],
                "reason": (
                    "the ledger observes these node candidates but does not prove the selected "
                    "plans will create them or that an equivalent endpoint is already live"
                ),
            },
            "external_or_unobserved_refs": {
                "status": "not_measured",
                "count": len(external_refs),
                "samples": external_refs[:sample_limit],
                "reason": (
                    "the candidate ledger cannot distinguish an existing graph endpoint from "
                    "an absent candidate"
                ),
            },
            "missing_refs": {
                "status": "measured",
                "count": len(missing_refs),
                "samples": missing_refs[:sample_limit],
            },
        },
        "invalid_planned_relation_shapes": {
            "count": len(invalid_shapes),
            "samples": invalid_shapes[:sample_limit],
        },
        "semantic_grounding": {
            "status": "not_measured",
            "reason": "anchor shape does not prove semantic support for a proposed relation",
        },
        "direction_accuracy": {
            "status": "not_measured",
            "reason": "no adjudicated relation-direction fixture was supplied",
        },
    }


def _classify_ledger_ref(
    ref: str,
    *,
    candidate_id: str,
    slot: str,
    candidate_node_ids: set[str],
    planned_create_node_ids: set[str],
    internal: list[dict[str, str]],
    observed_unplanned: list[dict[str, str]],
    external: list[dict[str, str]],
) -> None:
    row = {"candidate_id": candidate_id, "slot": slot, "ref": ref}
    if ref in planned_create_node_ids:
        internal.append(row)
    elif ref in candidate_node_ids:
        observed_unplanned.append(row)
    else:
        external.append(row)


def _valid_candidate_anchor(payload: Mapping[str, Any]) -> bool:
    block_id = payload.get("block_id")
    byte_start = payload.get("byte_start")
    byte_end = payload.get("byte_end")
    content_hash = payload.get("content_hash")
    return (
        isinstance(block_id, str)
        and bool(block_id.strip())
        and isinstance(byte_start, int)
        and not isinstance(byte_start, bool)
        and isinstance(byte_end, int)
        and not isinstance(byte_end, bool)
        and 0 <= byte_start <= byte_end
        and isinstance(content_hash, str)
        and _CONTENT_HASH.fullmatch(content_hash) is not None
    )


def _candidate_anchor_status(payload: Mapping[str, Any]) -> str:
    fields = ("block_id", "byte_start", "byte_end", "content_hash")
    if all(payload.get(field) is None for field in fields):
        return "absent"
    return "valid" if _valid_candidate_anchor(payload) else "invalid"


def _ledger_run_state(
    selected_rows: list[dict[str, Any]],
    selected_run_ids: list[str],
) -> dict[str, Any]:
    failed_states = {"abandoned", "aborted", "cancelled", "error", "failed"}
    terminal_states = {"completed"}
    runs: list[dict[str, Any]] = []
    for run_id in selected_run_ids:
        run_records = [
            row
            for row in selected_rows
            if row.get("kind") == "ingest_run" and str(row.get("run_id") or "") == run_id
        ]
        if not run_records:
            runs.append({"run_id": run_id, "state": None, "status": "missing"})
            continue
        state = str(run_records[-1].get("state") or "")
        normalized_state = state.casefold()
        if normalized_state == "started":
            status = "open"
        elif normalized_state in failed_states:
            status = "failed"
        elif normalized_state in terminal_states:
            status = "terminal"
        else:
            status = "unknown"
        runs.append({"run_id": run_id, "state": state, "status": status})
    return {
        "runs": runs,
        "open_run_ids": [row["run_id"] for row in runs if row["status"] == "open"],
        "terminal_run_ids": [row["run_id"] for row in runs if row["status"] == "terminal"],
        "failed_run_ids": [row["run_id"] for row in runs if row["status"] == "failed"],
        "missing_run_ids": [row["run_id"] for row in runs if row["status"] == "missing"],
        "unknown_run_ids": [row["run_id"] for row in runs if row["status"] == "unknown"],
    }


def _ledger_run_fingerprint(
    records: Iterable[Mapping[str, Any]],
    selected_run_ids: Iterable[str],
    *,
    field: str,
) -> dict[str, Any]:
    """Summarize one immutable run-start fingerprint without guessing legacy data."""

    selected = tuple(selected_run_ids)
    values: dict[str, str] = {}
    for record in records:
        if record.get("kind") != "ingest_run" or record.get("state") != "started":
            continue
        run_id = str(record.get("run_id") or "")
        if run_id not in selected:
            continue
        value = str(record.get(field) or "").strip()
        if value:
            values[run_id] = value
    missing = sorted(set(selected) - set(values))
    fingerprints = sorted(set(values.values()))
    runs = [{"run_id": run_id, "fingerprint": values.get(run_id)} for run_id in sorted(selected)]
    if missing:
        return {
            "status": "not_measured",
            "fingerprint": None,
            "fingerprints": fingerprints,
            "missing_run_ids": missing,
            "runs": runs,
            "reason": f"one or more selected run-start rows lack {field}",
        }
    if len(fingerprints) != 1:
        return {
            "status": "inconsistent",
            "fingerprint": None,
            "fingerprints": fingerprints,
            "missing_run_ids": [],
            "runs": runs,
            "reason": f"selected runs contain multiple {field} values",
        }
    return {
        "status": "measured",
        "fingerprint": fingerprints[0],
        "fingerprints": fingerprints,
        "missing_run_ids": [],
        "runs": runs,
        "reason": None,
    }


def _ledger_plan_order(
    selected_rows: list[dict[str, Any]],
    *,
    sample_limit: int,
) -> dict[str, Any]:
    plans: list[dict[str, Any]] = []
    plans_per_run = Counter(
        str(row.get("run_id") or "") for row in selected_rows if row.get("kind") == "commit_plan"
    )
    for plan_index, plan in enumerate(selected_rows):
        if plan.get("kind") != "commit_plan":
            continue
        plan_id = str(plan.get("plan_id") or "")
        run_id = str(plan.get("run_id") or "")
        raw_operations = plan.get("operations")
        operations = raw_operations if isinstance(raw_operations, list) else []
        candidate_ids = {
            str(operation.get("candidate_id") or "")
            for operation in operations
            if isinstance(operation, Mapping) and str(operation.get("candidate_id") or "")
        }
        terminal_positions = [
            index
            for index, row in enumerate(selected_rows)
            if row.get("kind") == "candidate"
            and str(row.get("run_id") or "") == run_id
            and str(row.get("candidate_id") or "") in candidate_ids
            and str(row.get("state") or "") not in {"", "proposed"}
        ]
        receipt_positions = [
            index
            for index, row in enumerate(selected_rows)
            if row.get("kind") == "commit_record"
            and str(row.get("run_id") or "") == run_id
            and str(row.get("plan_id") or "") == plan_id
        ]
        terminal_before = sum(index < plan_index for index in terminal_positions)
        terminal_after = sum(index > plan_index for index in terminal_positions)
        receipts_before = sum(index < plan_index for index in receipt_positions)
        receipts_after = sum(index > plan_index for index in receipt_positions)
        if receipts_before:
            status = "contradicted"
            preapply = False
            reason = "a matching commit receipt precedes the plan"
        elif terminal_before and plans_per_run[run_id] > 1:
            status = "not_proven"
            preapply = None
            reason = (
                "multiple plans reuse this run id, so earlier terminal candidate records cannot "
                "be assigned to one attempt"
            )
        elif terminal_before:
            status = "contradicted"
            preapply = False
            reason = "one or more terminal candidate records precede the only plan for this run"
        elif receipts_after and (terminal_after or not candidate_ids):
            status = "supported"
            preapply = True
            reason = "the plan precedes the selected terminal candidate and commit receipts"
        else:
            status = "not_proven"
            preapply = None
            reason = "ledger order lacks both post-plan terminal candidate and commit receipts"
        plans.append(
            {
                "run_id": run_id,
                "plan_id": plan_id,
                "status": status,
                "preapply": preapply,
                "reason": reason,
                "plan_record_index": plan_index,
                "terminal_candidate_records_before": terminal_before,
                "terminal_candidate_records_after": terminal_after,
                "commit_receipts_before": receipts_before,
                "commit_receipts_after": receipts_after,
            }
        )
    contradicted = [row for row in plans if row["status"] == "contradicted"]
    supported = [row for row in plans if row["status"] == "supported"]
    not_proven = [row for row in plans if row["status"] == "not_proven"]
    if contradicted:
        status = "contradicted"
        reason = "at least one plan follows a terminal candidate or commit receipt"
    elif plans and all(row["status"] == "supported" for row in plans):
        status = "supported"
        reason = None
    else:
        status = "not_proven"
        reason = "one or more plans lack durable post-plan terminal/apply receipts"
    return {
        "status": status,
        "reason": reason,
        "plans": plans[:sample_limit],
        "plan_count": len(plans),
        "supported_count": len(supported),
        "contradicted_count": len(contradicted),
        "not_proven_count": len(not_proven),
        "contradicted_samples": contradicted[:sample_limit],
    }


__all__ = [
    "SEMANTIC_QUALITY_SCHEMA",
    "SEMANTIC_REBUILD_GATE_CODES",
    "SEMANTIC_REBUILD_GATE_VERSION",
    "discovery_surface_key",
    "evaluate_ledger",
    "evaluate_ledger_scan",
    "evaluate_rebuild_gate",
    "evaluate_store",
    "exact_surface_key",
]
