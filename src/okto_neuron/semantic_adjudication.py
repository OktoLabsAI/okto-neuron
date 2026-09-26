"""Strict human-adjudication contract and deterministic semantic metrics.

This module measures candidate-ledger decisions.  It never changes a candidate,
plan, graph, registry, or authority record.  The caller supplies the complete
adjudication document and the already selected candidate/plan rows; malformed or
mis-bound evidence fails closed before any score is returned.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any, Final

from okto_neuron.primitives import PRIMITIVE_NAMES

SEMANTIC_ADJUDICATION_SCHEMA: Final = "semantic_adjudication.v1"

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "fixture_id",
        "instructions_version",
        "adjudicator",
        "entities",
        "relations",
    }
)
_ENTITY_FIELDS = frozenset(
    {
        "candidate_id",
        "expected_type",
        "expected_cluster_id",
        "expected_canonical_candidate_id",
        "expected_canonical_title",
        "rationale",
    }
)
_RELATION_FIELDS = frozenset(
    {
        "candidate_id",
        "expected_disposition",
        "expected_predicate",
        "expected_src_ref",
        "expected_object_kind",
        "expected_dst_ref",
        "expected_dst_literal",
        "grounded",
        "useful",
        "rationale",
    }
)
_DISPOSITIONS = frozenset({"accept", "queue", "reject", "supersede"})
_OPERATION_DISPOSITIONS = {
    "create_edge_or_claim": "accept",
    "queue_review": "queue",
    "dead_letter": "reject",
    "supersede_candidate": "supersede",
}


def evaluate_ledger_adjudication(
    adjudication: Mapping[str, Any],
    *,
    proposed_nodes: Sequence[Mapping[str, Any]],
    proposed_relations: Sequence[Mapping[str, Any]],
    planned_node_operations: Sequence[Mapping[str, Any]],
    planned_relation_operations: Sequence[Mapping[str, Any]],
    sample_limit: int,
) -> dict[str, Any]:
    """Validate and score one adjudication document against selected plans."""

    if sample_limit < 1:
        raise ValueError("sample_limit must be >= 1")
    document = _validate_document(adjudication)
    entities = document["entities"]
    relations = document["relations"]

    proposed_node_by_id = _unique_candidate_rows(proposed_nodes, "node")
    proposed_relation_by_id = _unique_candidate_rows(proposed_relations, "relation")
    node_operation_by_id = _unique_operation_rows(planned_node_operations, "node")
    relation_operation_by_id = _unique_operation_rows(planned_relation_operations, "relation")
    _require_bound_candidates(entities, proposed_node_by_id, "entity")
    _require_bound_candidates(relations, proposed_relation_by_id, "relation")

    entity_metrics = _entity_metrics(
        entities,
        proposed_node_by_id=proposed_node_by_id,
        operation_by_id=node_operation_by_id,
        sample_limit=sample_limit,
    )
    relation_metrics = _relation_metrics(
        relations,
        proposed_relation_by_id=proposed_relation_by_id,
        operation_by_id=relation_operation_by_id,
        sample_limit=sample_limit,
    )
    evidence_bytes = _canonical_json(adjudication)
    normalized_bytes = _canonical_json(document)
    return {
        "schema_version": SEMANTIC_ADJUDICATION_SCHEMA,
        "status": "measured",
        "fixture_id": document["fixture_id"],
        "instructions_version": document["instructions_version"],
        "adjudicator": document["adjudicator"],
        "sha256": hashlib.sha256(evidence_bytes).hexdigest(),
        "bytes": len(evidence_bytes),
        "normalized_sha256": hashlib.sha256(normalized_bytes).hexdigest(),
        "population": {
            "entities": len(entities),
            "relations": len(relations),
        },
        "coverage": {
            "entity_plan_operations": entity_metrics.pop("coverage"),
            "relation_plan_operations": relation_metrics.pop("coverage"),
        },
        "entity": entity_metrics,
        "relation": relation_metrics,
    }


def evidence_identity(value: Any, *, logical_type: str) -> dict[str, Any]:
    """Return a content identity for an external evidence value without echoing it."""

    canonical = _canonical_json(value)
    count = len(value) if isinstance(value, Sequence) and not isinstance(value, str) else 1
    return {
        "status": "supplied",
        "logical_type": logical_type,
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "bytes": len(canonical),
        "count": count,
    }


def _validate_document(adjudication: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(adjudication, Mapping):
        raise ValueError("adjudication must be a mapping")
    _require_exact_fields(adjudication, _TOP_LEVEL_FIELDS, "adjudication")
    if adjudication.get("schema_version") != SEMANTIC_ADJUDICATION_SCHEMA:
        raise ValueError(f"adjudication.schema_version must be {SEMANTIC_ADJUDICATION_SCHEMA!r}")
    document = dict(adjudication)
    for field in ("fixture_id", "instructions_version", "adjudicator"):
        document[field] = _required_text(document.get(field), f"adjudication.{field}")
    document["entities"] = _validate_entities(document.get("entities"))
    document["relations"] = _validate_relations(document.get("relations"))
    if not document["entities"]:
        raise ValueError("adjudication.entities must not be empty")
    if not document["relations"]:
        raise ValueError("adjudication.relations must not be empty")
    return document


def _validate_entities(value: object) -> list[dict[str, Any]]:
    rows = _required_list(value, "adjudication.entities")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        name = f"adjudication.entities[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{name} must be a mapping")
        _require_exact_fields(raw, _ENTITY_FIELDS, name)
        row = dict(raw)
        for field in (
            "candidate_id",
            "expected_cluster_id",
            "expected_canonical_candidate_id",
            "expected_canonical_title",
            "rationale",
        ):
            row[field] = _required_text(row.get(field), f"{name}.{field}")
        row["expected_type"] = _required_text(row.get("expected_type"), f"{name}.expected_type")
        if row["expected_type"] not in PRIMITIVE_NAMES:
            raise ValueError(f"{name}.expected_type is not a closed primitive")
        if row["candidate_id"] in seen:
            raise ValueError(f"duplicate adjudicated entity {row['candidate_id']!r}")
        seen.add(row["candidate_id"])
        result.append(row)
    return result


def _validate_relations(value: object) -> list[dict[str, Any]]:
    rows = _required_list(value, "adjudication.relations")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        name = f"adjudication.relations[{index}]"
        if not isinstance(raw, Mapping):
            raise ValueError(f"{name} must be a mapping")
        _require_exact_fields(raw, _RELATION_FIELDS, name)
        row = dict(raw)
        for field in (
            "candidate_id",
            "expected_predicate",
            "expected_src_ref",
            "expected_object_kind",
            "expected_disposition",
            "rationale",
        ):
            row[field] = _required_text(row.get(field), f"{name}.{field}")
        if row["expected_disposition"] not in _DISPOSITIONS:
            raise ValueError(f"{name}.expected_disposition is unsupported")
        if row["expected_object_kind"] not in {"literal", "topology"}:
            raise ValueError(f"{name}.expected_object_kind must be literal or topology")
        for field in ("grounded", "useful"):
            if not isinstance(row.get(field), bool):
                raise ValueError(f"{name}.{field} must be boolean")
        dst_ref = _optional_text(row.get("expected_dst_ref"))
        dst_literal = _optional_text(row.get("expected_dst_literal"))
        if (dst_ref is None) == (dst_literal is None):
            raise ValueError(f"{name} must define exactly one expected object")
        if row["expected_object_kind"] == "topology" and dst_ref is None:
            raise ValueError(f"{name} topology relation requires expected_dst_ref")
        if row["expected_object_kind"] == "literal" and dst_literal is None:
            raise ValueError(f"{name} literal relation requires expected_dst_literal")
        row["expected_dst_ref"] = dst_ref
        row["expected_dst_literal"] = dst_literal
        if row["candidate_id"] in seen:
            raise ValueError(f"duplicate adjudicated relation {row['candidate_id']!r}")
        seen.add(row["candidate_id"])
        result.append(row)
    return result


def _entity_metrics(
    entities: Sequence[Mapping[str, Any]],
    *,
    proposed_node_by_id: Mapping[str, Mapping[str, Any]],
    operation_by_id: Mapping[str, Mapping[str, Any]],
    sample_limit: int,
) -> dict[str, Any]:
    actual_cluster: dict[str, str] = {}
    actual_type: dict[str, str | None] = {}
    actual_canonical_title: dict[str, str | None] = {}
    missing_operations: list[str] = []

    title_by_candidate: dict[str, str] = {}
    for candidate_id, proposal in proposed_node_by_id.items():
        payload = _payload(proposal)
        title_by_candidate[candidate_id] = str(payload.get("title") or "")
    for candidate_id, operation in operation_by_id.items():
        if _optional_text(operation.get("title")) is not None:
            title_by_candidate[candidate_id] = str(operation["title"])

    for truth in entities:
        candidate_id = str(truth["candidate_id"])
        proposal = proposed_node_by_id[candidate_id]
        operation = operation_by_id.get(candidate_id)
        if operation is None:
            missing_operations.append(candidate_id)
            actual_cluster[candidate_id] = f"__missing__:{candidate_id}"
            actual_type[candidate_id] = None
            actual_canonical_title[candidate_id] = None
            continue
        target = _optional_text(operation.get("target_ref")) or candidate_id
        actual_cluster[candidate_id] = target
        actual_type[candidate_id] = _optional_text(operation.get("type")) or _optional_text(
            _payload(proposal).get("type")
        )
        actual_canonical_title[candidate_id] = title_by_candidate.get(target)

    gold_cluster = {str(row["candidate_id"]): str(row["expected_cluster_id"]) for row in entities}
    b3 = _b3(gold_cluster, actual_cluster)
    pairwise = _pairwise_clusters(entities, gold_cluster, actual_cluster, sample_limit)
    type_quality = _classification(
        [str(row["expected_type"]) for row in entities],
        [actual_type[str(row["candidate_id"])] for row in entities],
    )
    canonical_rows = []
    alias_rows = []
    title_rows = []
    for truth in entities:
        candidate_id = str(truth["candidate_id"])
        expected_canonical = str(truth["expected_canonical_candidate_id"])
        actual_canonical = actual_cluster[candidate_id]
        correct = actual_canonical == expected_canonical
        canonical_rows.append(correct)
        if candidate_id != expected_canonical:
            alias_rows.append(correct)
        title_rows.append(
            actual_canonical_title[candidate_id] == str(truth["expected_canonical_title"])
        )

    return {
        "coverage": _coverage(len(entities), missing_operations, sample_limit),
        "type_accuracy": {
            "status": "measured",
            **type_quality,
        },
        "b3_cluster_quality": {"status": "measured", **b3},
        "pairwise_cluster_errors": pairwise,
        "canonical_assignment_accuracy": _accuracy(canonical_rows),
        "alias_assignment_accuracy": _accuracy(alias_rows),
        "canonical_title_accuracy": _accuracy(title_rows),
    }


def _relation_metrics(
    relations: Sequence[Mapping[str, Any]],
    *,
    proposed_relation_by_id: Mapping[str, Mapping[str, Any]],
    operation_by_id: Mapping[str, Mapping[str, Any]],
    sample_limit: int,
) -> dict[str, Any]:
    missing_operations: list[str] = []
    disposition_correct: list[bool] = []
    predicate_correct: list[bool] = []
    object_kind_correct: list[bool] = []
    object_value_correct: list[bool] = []
    direction_correct: list[bool] = []
    expected_accept: list[bool] = []
    actual_accept: list[bool] = []
    expected_grounded: list[bool] = []
    expected_useful: list[bool] = []
    false_accepts: list[str] = []
    missed_accepts: list[str] = []

    for truth in relations:
        candidate_id = str(truth["candidate_id"])
        operation = operation_by_id.get(candidate_id)
        proposal = proposed_relation_by_id[candidate_id]
        combined = {**_payload(proposal), **dict(operation or {})}
        operation_name = str((operation or {}).get("operation") or "")
        disposition = _OPERATION_DISPOSITIONS.get(operation_name, "missing")
        if operation is None:
            missing_operations.append(candidate_id)
        expected_disposition = str(truth["expected_disposition"])
        disposition_correct.append(disposition == expected_disposition)
        predicate_correct.append(
            str(combined.get("type") or "").strip() == str(truth["expected_predicate"])
        )

        dst_ref = _optional_text(combined.get("dst_ref"))
        dst_literal = _optional_text(combined.get("dst_literal"))
        actual_kind = (
            "topology"
            if dst_ref is not None and dst_literal is None
            else "literal"
            if dst_literal is not None and dst_ref is None
            else "invalid"
        )
        object_kind_correct.append(actual_kind == truth["expected_object_kind"])
        source_correct = _optional_text(combined.get("src_ref")) == truth["expected_src_ref"]
        object_value_correct.append(
            dst_ref == truth["expected_dst_ref"] and dst_literal == truth["expected_dst_literal"]
        )
        direction_correct.append(
            source_correct
            and (
                dst_ref == truth["expected_dst_ref"]
                if truth["expected_object_kind"] == "topology"
                else True
            )
        )

        gold_accept = expected_disposition == "accept"
        predicted_accept = disposition == "accept"
        expected_accept.append(gold_accept)
        actual_accept.append(predicted_accept)
        expected_grounded.append(bool(truth["grounded"]))
        expected_useful.append(bool(truth["useful"]))
        if predicted_accept and not gold_accept:
            false_accepts.append(candidate_id)
        if gold_accept and not predicted_accept:
            missed_accepts.append(candidate_id)

    return {
        "coverage": _coverage(len(relations), missing_operations, sample_limit),
        "decision_accuracy": _accuracy(disposition_correct),
        "predicate_accuracy": _accuracy(predicate_correct),
        "object_kind_accuracy": _accuracy(object_kind_correct),
        "object_value_accuracy": _accuracy(object_value_correct),
        "direction_accuracy": _accuracy(direction_correct),
        "admission_quality": _binary_quality(expected_accept, actual_accept),
        "grounding_quality": _binary_quality(expected_grounded, actual_accept),
        "usefulness_quality": _binary_quality(expected_useful, actual_accept),
        "false_accepts": {
            "count": len(false_accepts),
            "samples": false_accepts[:sample_limit],
        },
        "missed_accepts": {
            "count": len(missed_accepts),
            "samples": missed_accepts[:sample_limit],
        },
    }


def _b3(gold: Mapping[str, str], predicted: Mapping[str, str]) -> dict[str, float]:
    gold_members: dict[str, set[str]] = defaultdict(set)
    predicted_members: dict[str, set[str]] = defaultdict(set)
    for candidate_id in gold:
        gold_members[gold[candidate_id]].add(candidate_id)
        predicted_members[predicted[candidate_id]].add(candidate_id)
    precisions: list[float] = []
    recalls: list[float] = []
    for candidate_id in gold:
        intersection = gold_members[gold[candidate_id]] & predicted_members[predicted[candidate_id]]
        precisions.append(len(intersection) / len(predicted_members[predicted[candidate_id]]))
        recalls.append(len(intersection) / len(gold_members[gold[candidate_id]]))
    precision = sum(precisions) / len(precisions)
    recall = sum(recalls) / len(recalls)
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return {"precision": _rounded(precision), "recall": _rounded(recall), "f1": _rounded(f1)}


def _pairwise_clusters(
    entities: Sequence[Mapping[str, Any]],
    gold: Mapping[str, str],
    predicted: Mapping[str, str],
    sample_limit: int,
) -> dict[str, Any]:
    false_merges: list[dict[str, Any]] = []
    missed_merges: list[dict[str, Any]] = []
    cross_type_false_merges: list[dict[str, Any]] = []
    expected_type = {str(row["candidate_id"]): str(row["expected_type"]) for row in entities}
    candidate_ids = sorted(gold)
    for index, left in enumerate(candidate_ids):
        for right in candidate_ids[index + 1 :]:
            gold_same = gold[left] == gold[right]
            predicted_same = predicted[left] == predicted[right]
            row = {"left": left, "right": right}
            if predicted_same and not gold_same:
                false_merges.append(row)
                if expected_type[left] != expected_type[right]:
                    cross_type_false_merges.append(row)
            elif gold_same and not predicted_same:
                missed_merges.append(row)
    return {
        "false_merges": {"count": len(false_merges), "samples": false_merges[:sample_limit]},
        "cross_type_false_merges": {
            "count": len(cross_type_false_merges),
            "samples": cross_type_false_merges[:sample_limit],
        },
        "missed_merges": {"count": len(missed_merges), "samples": missed_merges[:sample_limit]},
    }


def _classification(expected: list[str], actual: list[str | None]) -> dict[str, Any]:
    accuracy = sum(left == right for left, right in zip(expected, actual, strict=True)) / len(
        expected
    )
    per_type: dict[str, dict[str, float | int]] = {}
    f1_values: list[float] = []
    for type_ in sorted(set(expected)):
        tp = sum(
            left == type_ and right == type_ for left, right in zip(expected, actual, strict=True)
        )
        fp = sum(
            left != type_ and right == type_ for left, right in zip(expected, actual, strict=True)
        )
        fn = sum(
            left == type_ and right != type_ for left, right in zip(expected, actual, strict=True)
        )
        quality = _counts_quality(tp, fp, fn)
        per_type[type_] = quality
        f1_values.append(float(quality["f1"]))
    return {
        "accuracy": _rounded(accuracy),
        "macro_f1": _rounded(sum(f1_values) / len(f1_values)),
        "per_type": per_type,
    }


def _binary_quality(expected: list[bool], actual: list[bool]) -> dict[str, float | int | str]:
    tp = sum(left and right for left, right in zip(expected, actual, strict=True))
    fp = sum(not left and right for left, right in zip(expected, actual, strict=True))
    fn = sum(left and not right for left, right in zip(expected, actual, strict=True))
    return {"status": "measured", **_counts_quality(tp, fp, fn)}


def _counts_quality(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return {
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": _rounded(precision),
        "recall": _rounded(recall),
        "f1": _rounded(f1),
    }


def _accuracy(rows: Sequence[bool]) -> dict[str, Any]:
    if not rows:
        return {"status": "not_measured", "reason": "no labelled population", "count": 0}
    correct = sum(rows)
    return {
        "status": "measured",
        "count": len(rows),
        "correct": correct,
        "incorrect": len(rows) - correct,
        "accuracy": _rounded(correct / len(rows)),
    }


def _coverage(total: int, missing: list[str], sample_limit: int) -> dict[str, Any]:
    return {
        "status": "complete" if not missing else "incomplete",
        "labelled": total,
        "observed": total - len(missing),
        "missing_count": len(missing),
        "missing_samples": sorted(missing)[:sample_limit],
    }


def _unique_candidate_rows(
    rows: Sequence[Mapping[str, Any]], kind: str
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        candidate_id = _required_text(row.get("candidate_id"), f"{kind}.candidate_id")
        if candidate_id in result:
            raise ValueError(f"duplicate proposed {kind} candidate {candidate_id!r}")
        result[candidate_id] = row
    return result


def _unique_operation_rows(
    rows: Sequence[Mapping[str, Any]], kind: str
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        candidate_id = _required_text(row.get("candidate_id"), f"planned {kind}.candidate_id")
        if candidate_id in result:
            raise ValueError(f"multiple planned {kind} operations for {candidate_id!r}")
        result[candidate_id] = row
    return result


def _require_bound_candidates(
    truth: Sequence[Mapping[str, Any]],
    proposals: Mapping[str, Mapping[str, Any]],
    kind: str,
) -> None:
    missing = sorted(
        str(row["candidate_id"]) for row in truth if row["candidate_id"] not in proposals
    )
    if missing:
        raise ValueError(
            f"adjudicated {kind} candidates are absent from selected ledger: {missing}"
        )


def _payload(row: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = row.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _require_exact_fields(value: Mapping[str, Any], expected: frozenset[str], name: str) -> None:
    fields = set(value)
    missing = sorted(expected - fields)
    unknown = sorted(fields - expected)
    if missing or unknown:
        raise ValueError(f"{name} fields invalid: missing={missing}, unknown={unknown}")


def _required_list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("evidence must be finite, JSON-serializable data") from exc


def _rounded(value: float) -> float:
    return round(value, 6)


__all__ = [
    "SEMANTIC_ADJUDICATION_SCHEMA",
    "evaluate_ledger_adjudication",
    "evidence_identity",
]
