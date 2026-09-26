from __future__ import annotations

import json
from pathlib import Path

import pytest

from okto_neuron.consolidate.ledger import CandidateLedger
from okto_neuron.semantic_quality import evaluate_ledger, evaluate_ledger_scan
from okto_neuron.semantic_surface import build_surface_record


_HASH = "a" * 64


def _complete_scan_records(run_id: str = "run-1") -> list[dict[str, object]]:
    return [
        {"ledger_version": 2, "kind": "ingest_run", "run_id": run_id, "state": "started"},
        _candidate(run_id, "n1", "node", {"type": "Agent", "title": "Ari"}),
        {
            "ledger_version": 2,
            "kind": "commit_plan",
            "run_id": run_id,
            "plan_id": "plan-1",
            "operations": [
                {
                    "operation": "create_node",
                    "candidate_kind": "node",
                    "candidate_id": "n1",
                    "type": "Agent",
                    "title": "Ari",
                }
            ],
        },
        _candidate(run_id, "n1", "node", {}, state="committed"),
        {
            "ledger_version": 2,
            "kind": "commit_record",
            "run_id": run_id,
            "plan_id": "plan-1",
            "result": {},
        },
        {"ledger_version": 2, "kind": "ingest_run", "run_id": run_id, "state": "completed"},
    ]


def test_evaluate_ledger_scan_carries_lossless_file_framing(tmp_path) -> None:
    ledger = CandidateLedger(tmp_path)
    for row in _complete_scan_records():
        kind = str(row["kind"])
        payload = {
            key: value for key, value in row.items() if key not in {"kind", "ledger_version"}
        }
        ledger.append(kind, **payload)

    report = evaluate_ledger_scan(ledger.scan(), ["run-1"])

    framing = report["evidence"]["source_framing"]
    assert report["evidence"]["complete"] is True
    assert framing["status"] == "measured"
    assert framing["complete"] is True
    assert len(framing["file_sha256"]) == 64
    assert "path" not in framing
    assert "malformed_lines" not in framing
    assert "malformed_line_numbers" not in framing
    framing_check = next(
        row
        for row in report["hard_invariants"]["checks"]
        if row["code"] == "candidate_ledger_source_framing"
    )
    assert framing_check["status"] == "passed"
    assert report["verdict"]["authoritative_pass"] is False


def test_evaluate_ledger_scan_fails_closed_on_malformed_jsonl(tmp_path) -> None:
    ledger = CandidateLedger(tmp_path)
    lines = [json.dumps(row, separators=(",", ":")) for row in _complete_scan_records()]
    ledger.path.write_text("\n".join([*lines, "{interrupted"]), encoding="utf-8")

    report = evaluate_ledger_scan(ledger.scan(), ["run-1"])

    assert report["evidence"]["complete"] is False
    assert report["evidence"]["source_framing"]["status"] == "incomplete"
    assert report["hard_invariants"]["status"] == "failed"
    framing_check = next(
        row
        for row in report["hard_invariants"]["checks"]
        if row["code"] == "candidate_ledger_source_framing"
    )
    assert framing_check["status"] == "failed"


def test_evaluate_ledger_scan_labels_malformed_line_numbers_as_samples(
    tmp_path: Path,
) -> None:
    ledger = CandidateLedger(tmp_path)
    valid = [json.dumps(row, separators=(",", ":")) for row in _complete_scan_records()]
    ledger.path.write_text(
        "\n".join([*valid, *("bad" for _ in range(12))]) + "\n",
        encoding="utf-8",
    )

    report = evaluate_ledger_scan(ledger.scan(), ["run-1"])

    framing = report["evidence"]["source_framing"]
    assert framing["malformed_line_count"] == 12
    assert len(framing["sampled_malformed_line_numbers"]) == 8
    assert framing["malformed_samples_truncated"] is True


def test_evaluate_ledger_measures_and_validates_recorded_surface_evidence() -> None:
    surface = build_surface_record("  Cafe\u0301_Notes  ", "Cafe_Notes")
    records = _complete_scan_records()
    records[1] = _candidate(
        "run-1",
        "n1",
        "node",
        {
            "type": "Agent",
            "title": "Cafe_Notes",
            "surface": surface.model_dump(mode="json"),
        },
    )

    report = evaluate_ledger(records, ["run-1"])

    preservation = report["layers"]["surface"]["source_surface_preservation"]
    assert preservation["status"] == "measured"
    assert preservation["observed_count"] == 1
    surface_check = next(
        row
        for row in report["hard_invariants"]["checks"]
        if row["code"] == "source_surface_preservation"
    )
    assert surface_check["status"] == "passed"

    tampered = [dict(row) for row in records]
    tampered_payload = dict(tampered[1]["payload"])
    tampered_surface = dict(tampered_payload["surface"])
    tampered_surface["exact_key"] = "wrong"
    tampered_payload["surface"] = tampered_surface
    tampered[1] = {**tampered[1], "payload": tampered_payload}

    invalid = evaluate_ledger(tampered, ["run-1"])
    invalid_preservation = invalid["layers"]["surface"]["source_surface_preservation"]
    assert invalid_preservation["status"] == "incomplete"
    assert invalid_preservation["invalid_count"] == 1
    invalid_check = next(
        row
        for row in invalid["hard_invariants"]["checks"]
        if row["code"] == "source_surface_preservation"
    )
    assert invalid_check["status"] == "failed"

    mixed = [
        *tampered,
        _candidate(
            "run-1",
            "legacy",
            "node",
            {"type": "Agent", "title": "Legacy"},
        ),
    ]
    mixed_report = evaluate_ledger(mixed, ["run-1"])
    mixed_preservation = mixed_report["layers"]["surface"]["source_surface_preservation"]
    assert mixed_preservation["invalid_count"] == 1
    assert mixed_preservation["missing_count"] == 1


def _candidate(
    run_id: str,
    candidate_id: str,
    candidate_kind: str,
    payload: dict[str, object],
    *,
    state: str = "proposed",
) -> dict[str, object]:
    return {
        "ledger_version": 2,
        "kind": "candidate",
        "run_id": run_id,
        "candidate_id": candidate_id,
        "candidate_kind": candidate_kind,
        "state": state,
        "payload": payload,
    }


def _recall_sample() -> dict[str, object]:
    return {
        "schema_version": "recall_cost.v1",
        "measurement_status": "measured",
        "completion_calls": 0,
        "generated_tokens": 0,
        "query_embedding_calls": 1,
        "query_embedding_latency_ms": 1.5,
        "query_embedding_provider": "fastembed",
        "query_embedding_model": "test",
        "query_embedding_execution": "local",
        "deterministic_retrieval_latency_ms": 2.0,
        "deterministic_projection_latency_ms": 0.5,
        "total_latency_ms": 4.0,
        "retrieved_results": 3,
        "retrieved_bytes": 120,
        "completion_free": True,
    }


def test_evaluate_ledger_measures_only_selected_candidate_plan_evidence() -> None:
    run_id = "run-1"
    records: list[dict[str, object]] = [
        {"kind": "ingest_run", "run_id": run_id, "state": "started"},
        _candidate(run_id, "n1", "node", {"type": "Agent", "title": "Frodo"}),
        _candidate(run_id, "n2", "node", {"type": "Agent", "title": "  frodo "}),
        _candidate(run_id, "n3", "node", {"type": "Concept", "title": "Frodo"}),
        _candidate(run_id, "n4", "node", {"type": "ImaginaryType", "title": "Bad"}),
        _candidate(
            run_id,
            "e1",
            "edge",
            {
                "type": "uses",
                "src_ref": "n1",
                "dst_ref": "n3",
                "block_id": "block-1",
                "byte_start": 0,
                "byte_end": 10,
                "content_hash": _HASH,
            },
        ),
        _candidate(
            run_id,
            "e2",
            "edge",
            {
                "type": "unknown",
                "src_ref": "n1",
                "dst_literal": "ring bearer",
            },
        ),
        _candidate(
            run_id,
            "e3",
            "edge",
            {
                "type": "related_to",
                "src_ref": "n1",
                "dst_ref": "existing-node",
                "block_id": "block-1",
                "byte_start": 11,
                "byte_end": 20,
                "content_hash": _HASH,
            },
        ),
        _candidate(run_id, "e4", "edge", {"type": "discarded", "src_ref": "n1"}),
        _candidate(run_id, "e5", "edge", {"type": "old", "src_ref": "n1"}),
        {
            "kind": "commit_plan",
            "run_id": run_id,
            "plan_id": "plan-1",
            "operations": [
                {
                    "operation": "create_node",
                    "candidate_kind": "node",
                    "candidate_id": "n1",
                    "type": "Agent",
                    "title": "Frodo",
                },
                {
                    "operation": "create_node",
                    "candidate_kind": "node",
                    "candidate_id": "n4",
                    "type": "ImaginaryType",
                    "title": "Bad",
                },
                {
                    "operation": "create_node",
                    "candidate_kind": "node",
                    "candidate_id": "n3",
                    "type": "Concept",
                    "title": "Frodo",
                },
                {
                    "operation": "create_edge_or_claim",
                    "candidate_kind": "edge",
                    "candidate_id": "e1",
                    "type": "uses",
                    "src_ref": "n1",
                    "dst_ref": "n3",
                    "dst_literal": None,
                },
                {
                    "operation": "queue_review",
                    "candidate_kind": "edge",
                    "candidate_id": "e2",
                    "type": "unknown",
                },
                {
                    "operation": "create_edge_or_claim",
                    "candidate_kind": "edge",
                    "candidate_id": "e3",
                    "type": "related_to",
                    "src_ref": "n1",
                    "dst_ref": "existing-node",
                    "dst_literal": None,
                },
                {
                    "operation": "dead_letter",
                    "candidate_kind": "edge",
                    "candidate_id": "e4",
                    "type": "discarded",
                },
                {
                    "operation": "supersede_candidate",
                    "candidate_kind": "edge",
                    "candidate_id": "e5",
                    "type": "old",
                },
            ],
        },
    ]
    records.extend(
        _candidate(
            run_id,
            candidate_id,
            "node" if candidate_id.startswith("n") else "edge",
            {},
            state="committed" if candidate_id in {"n1", "n4", "e1", "e3"} else "queued",
        )
        for candidate_id in ("n1", "n3", "n4", "e1", "e2", "e3", "e4", "e5")
    )
    records.extend(
        (
            {"kind": "commit_record", "run_id": run_id, "plan_id": "plan-1", "result": {}},
            {"kind": "ingest_run", "run_id": run_id, "state": "completed"},
            {"kind": "ingest_run", "run_id": "other-run", "state": "completed"},
        )
    )

    report = evaluate_ledger(
        records,
        [run_id],
        registered_predicates={"uses"},
        recall_samples=[_recall_sample()],
    )

    assert report["schema_version"] == "semantic_quality.v1"
    assert report["report_variant"] == "candidate_ledger_plan.v1"
    evidence = report["evidence"]
    assert evidence["scope"] == "candidate_ledger_plan"
    assert evidence["authoritative"] is False
    assert evidence["selected_run_ids"] == [run_id]
    assert evidence["selected_plan_ids"] == ["plan-1"]
    assert len(evidence["selected_record_sha256"]) == 64
    assert evidence["run_state"]["terminal_run_ids"] == [run_id]
    assert evidence["plan_order"]["status"] == "supported"

    surface = report["layers"]["surface"]
    assert surface["same_type_exact_duplicate_groups"]["count"] == 1
    assert surface["cross_type_exact_conflict_groups"]["count"] == 1
    assert surface["source_surface_preservation"]["status"] == "not_measured"

    type_layer = report["layers"]["type"]
    assert type_layer["proposed"]["primitive_counts"] == {
        "Agent": 2,
        "Concept": 1,
        "ImaginaryType": 1,
    }
    assert type_layer["proposed"]["invalid_entity_types"]["count"] == 1
    assert type_layer["planned_create"]["invalid_entity_types"]["count"] == 1

    predicate = report["layers"]["predicate"]
    assert predicate["proposed"]["raw_vocabulary_size"] == 5
    assert predicate["proposed"]["placeholder_predicates"]["count"] == 1
    assert predicate["planned_accept"]["raw_vocabulary_size"] == 2
    assert predicate["compression"]["planned_to_proposed_ratio"] == pytest.approx(0.4)
    assert predicate["planned_accept"]["registry_coverage"]["unregistered_samples"] == [
        "related_to"
    ]

    relation = report["layers"]["relation"]
    assert relation["planned_decisions"] == {
        "accept": 2,
        "queue": 1,
        "reject": 1,
        "supersede": 1,
        "unknown": 0,
    }
    assert relation["accepted_object_kind"] == {"literal": 0, "topology": 2, "invalid": 0}
    assert relation["accepted_anchor_shape"]["valid_count"] == 2
    assert relation["endpoint_resolution"]["internal_candidate_refs"]["resolved_count"] == 3
    assert relation["endpoint_resolution"]["external_or_unobserved_refs"]["count"] == 1
    assert (
        relation["endpoint_resolution"]["external_or_unobserved_refs"]["status"] == "not_measured"
    )
    assert report["layers"]["recall"]["completion"]["calls_total"] == 0
    assert report["hard_invariants"]["status"] == "failed"
    assert report["verdict"]["authoritative_pass"] is False


def test_evaluate_ledger_binds_and_projects_adjudicated_layer_metrics() -> None:
    run_id = "run-adjudicated"
    records = [
        {
            "kind": "ingest_run",
            "run_id": run_id,
            "state": "started",
            "semantic_policy_fingerprint": "policy-1",
            "config_fingerprint": "config-1",
        },
        _candidate(run_id, "person", "node", {"type": "Agent", "title": "Joram"}),
        _candidate(run_id, "place", "node", {"type": "Place", "title": "Joram"}),
        _candidate(
            run_id,
            "noise",
            "edge",
            {
                "type": "mentions",
                "src_ref": "person",
                "dst_ref": "place",
                "block_id": "block-1",
                "byte_start": 0,
                "byte_end": 5,
                "content_hash": _HASH,
            },
        ),
        {
            "kind": "commit_plan",
            "run_id": run_id,
            "plan_id": "plan-adjudicated",
            "operations": [
                {
                    "operation": "create_node",
                    "candidate_kind": "node",
                    "candidate_id": "person",
                    "type": "Agent",
                    "title": "Joram",
                },
                {
                    "operation": "supersede_candidate",
                    "candidate_kind": "node",
                    "candidate_id": "place",
                    "type": "Place",
                    "title": "Joram",
                    "target_ref": "person",
                },
                {
                    "operation": "create_edge_or_claim",
                    "candidate_kind": "edge",
                    "candidate_id": "noise",
                    "type": "mentions",
                    "src_ref": "person",
                    "dst_ref": "place",
                    "dst_literal": None,
                },
            ],
        },
        {"kind": "ingest_run", "run_id": run_id, "state": "completed"},
    ]
    adjudication = {
        "schema_version": "semantic_adjudication.v1",
        "fixture_id": "cross-type-and-noise",
        "instructions_version": "adr0040.v1",
        "adjudicator": "fixture-author",
        "entities": [
            {
                "candidate_id": "person",
                "expected_type": "Agent",
                "expected_cluster_id": "person-sense",
                "expected_canonical_candidate_id": "person",
                "expected_canonical_title": "Joram",
                "rationale": "Person sense.",
            },
            {
                "candidate_id": "place",
                "expected_type": "Place",
                "expected_cluster_id": "place-sense",
                "expected_canonical_candidate_id": "place",
                "expected_canonical_title": "Joram",
                "rationale": "Place sense.",
            },
        ],
        "relations": [
            {
                "candidate_id": "noise",
                "expected_disposition": "reject",
                "expected_predicate": "mentions",
                "expected_src_ref": "person",
                "expected_object_kind": "topology",
                "expected_dst_ref": "place",
                "expected_dst_literal": None,
                "grounded": False,
                "useful": False,
                "rationale": "Document attachment is not a semantic relation.",
            }
        ],
    }

    report = evaluate_ledger(
        records,
        [run_id],
        registered_predicates=["mentions"],
        recall_samples=[_recall_sample()],
        adjudication=adjudication,
    )

    assert report["layers"]["identity"]["b3_cluster_quality"]["f1"] == pytest.approx(2 / 3)
    assert report["layers"]["type"]["type_accuracy"]["accuracy"] == 1.0
    assert report["layers"]["relation"]["semantic_grounding"]["false_positive"] == 1
    checks = {row["code"]: row for row in report["hard_invariants"]["checks"]}
    assert checks["cross_type_merge_safety"]["status"] == "failed"
    assert checks["relation_grounding"]["status"] == "failed"
    external = report["evidence"]["external_inputs"]
    assert external["adjudication"]["sha256"] == report["layers"]["adjudication"]["sha256"]
    assert external["registered_predicates"]["count"] == 1
    assert external["recall_samples"]["count"] == 1
    assert "mentions" not in json.dumps(external)


def test_evaluate_ledger_reports_contradicted_plan_order() -> None:
    records = [
        {"kind": "ingest_run", "run_id": "run-1", "state": "started"},
        _candidate("run-1", "n1", "node", {"type": "Agent", "title": "Frodo"}),
        _candidate("run-1", "n1", "node", {}, state="committed"),
        {
            "kind": "commit_plan",
            "run_id": "run-1",
            "plan_id": "plan-1",
            "operations": [
                {
                    "operation": "create_node",
                    "candidate_kind": "node",
                    "candidate_id": "n1",
                    "type": "Agent",
                }
            ],
        },
        {"kind": "commit_record", "run_id": "run-1", "plan_id": "plan-1"},
        {"kind": "ingest_run", "run_id": "run-1", "state": "completed"},
    ]

    report = evaluate_ledger(records, ["run-1"])

    assert report["evidence"]["plan_order"]["status"] == "contradicted"
    order_check = next(
        row
        for row in report["hard_invariants"]["checks"]
        if row["code"] == "commit_plan_preapply_order"
    )
    assert order_check["status"] == "failed"


def test_evaluate_ledger_keeps_unproven_open_and_failed_runs_explicit() -> None:
    records = [
        {"kind": "ingest_run", "run_id": "open", "state": "started"},
        {"kind": "commit_plan", "run_id": "open", "plan_id": "p-open", "operations": []},
        {"kind": "ingest_run", "run_id": "failed", "state": "started"},
        {"kind": "commit_plan", "run_id": "failed", "plan_id": "p-failed", "operations": []},
        {"kind": "ingest_run", "run_id": "failed", "state": "failed"},
    ]

    report = evaluate_ledger(records, ["failed", "open"])

    assert report["evidence"]["run_state"]["open_run_ids"] == ["open"]
    assert report["evidence"]["run_state"]["failed_run_ids"] == ["failed"]
    assert report["evidence"]["plan_order"]["status"] == "not_proven"


def test_evaluate_ledger_selection_hash_is_canonical_and_requires_run_ids() -> None:
    records = [
        {"kind": "ingest_run", "run_id": "run-1", "state": "started"},
        {"kind": "commit_plan", "run_id": "run-1", "plan_id": "plan-1", "operations": []},
    ]
    reordered_keys = [dict(reversed(list(row.items()))) for row in records]

    first = evaluate_ledger(records, ["run-1"])
    second = evaluate_ledger(reordered_keys, ["run-1"])

    assert (
        first["evidence"]["selected_record_sha256"] == second["evidence"]["selected_record_sha256"]
    )
    with pytest.raises(ValueError, match="at least one"):
        evaluate_ledger(records, [])


def test_evaluate_ledger_does_not_hide_malformed_plan_operations() -> None:
    records = [
        {"kind": "ingest_run", "run_id": "run-1", "state": "started"},
        {
            "kind": "commit_plan",
            "run_id": "run-1",
            "plan_id": "plan-1",
            "operations": [{"operation": "create_node", "candidate_id": "n1"}, "bad"],
        },
    ]

    report = evaluate_ledger(records, ["run-1"])

    issues = report["population"]["plan_shape_issues"]
    assert issues["count"] == 2
    assert report["evidence"]["selection_complete"] is False
    shape_check = next(
        row
        for row in report["hard_invariants"]["checks"]
        if row["code"] == "candidate_ledger_plan_shape"
    )
    assert shape_check["status"] == "failed"


def test_evaluate_ledger_does_not_call_queued_candidate_endpoint_resolved() -> None:
    records = [
        {"kind": "ingest_run", "run_id": "run-1", "state": "started"},
        _candidate("run-1", "n1", "node", {"type": "Agent", "title": "Frodo"}),
        _candidate(
            "run-1",
            "e1",
            "edge",
            {"type": "speaks", "src_ref": "n1", "dst_literal": "hello"},
        ),
        {
            "kind": "commit_plan",
            "run_id": "run-1",
            "plan_id": "plan-1",
            "operations": [
                {
                    "operation": "queue_review",
                    "candidate_kind": "node",
                    "candidate_id": "n1",
                    "type": "Agent",
                },
                {
                    "operation": "create_edge_or_claim",
                    "candidate_kind": "edge",
                    "candidate_id": "e1",
                    "type": "speaks",
                    "src_ref": "n1",
                    "dst_literal": "hello",
                },
            ],
        },
    ]

    report = evaluate_ledger(records, ["run-1"])
    endpoints = report["layers"]["relation"]["endpoint_resolution"]

    assert endpoints["internal_candidate_refs"]["resolved_count"] == 0
    assert endpoints["observed_candidate_refs_not_planned_create"]["count"] == 1
    assert endpoints["observed_candidate_refs_not_planned_create"]["status"] == "not_measured"
    anchors = report["layers"]["relation"]["accepted_anchor_shape"]
    assert anchors["absent_count"] == 1
    anchor_check = next(
        row
        for row in report["hard_invariants"]["checks"]
        if row["code"] == "planned_relation_anchor_well_formed"
    )
    assert anchor_check["status"] == "passed"


def test_evaluate_ledger_knows_abandoned_and_does_not_overread_resumed_order() -> None:
    records = [
        {"kind": "ingest_run", "run_id": "run-1", "state": "started"},
        _candidate("run-1", "n1", "node", {"type": "Agent", "title": "Frodo"}),
        {
            "kind": "commit_plan",
            "run_id": "run-1",
            "plan_id": "plan-1",
            "operations": [
                {
                    "operation": "create_node",
                    "candidate_kind": "node",
                    "candidate_id": "n1",
                    "type": "Agent",
                }
            ],
        },
        _candidate("run-1", "n1", "node", {}, state="committed"),
        {
            "kind": "commit_plan",
            "run_id": "run-1",
            "plan_id": "plan-2",
            "operations": [
                {
                    "operation": "create_node",
                    "candidate_kind": "node",
                    "candidate_id": "n1",
                    "type": "Agent",
                }
            ],
        },
        {"kind": "ingest_run", "run_id": "run-1", "state": "abandoned"},
    ]

    report = evaluate_ledger(records, ["run-1"])

    assert report["evidence"]["run_state"]["failed_run_ids"] == ["run-1"]
    assert report["evidence"]["run_state"]["unknown_run_ids"] == []
    assert report["evidence"]["plan_order"]["status"] == "not_proven"
    assert report["evidence"]["plan_order"]["contradicted_count"] == 0


def test_evaluate_ledger_fails_closed_on_missing_run_plan_and_input_row() -> None:
    records: list[object] = [
        {"kind": "ingest_run", "run_id": "run-1", "state": "completed"},
        "malformed",
    ]

    report = evaluate_ledger(records, ["missing", "run-1"])  # type: ignore[arg-type]

    assert report["evidence"]["selection_complete"] is False
    assert report["evidence"]["missing_run_ids"] == ["missing"]
    assert report["evidence"]["runs_without_plans"] == ["missing", "run-1"]
    checks = {row["code"]: row for row in report["hard_invariants"]["checks"]}
    assert checks["candidate_ledger_input_shape"]["status"] == "failed"
    assert checks["selected_ledger_runs_present"]["status"] == "failed"
    assert checks["candidate_ledger_plans_present"]["status"] == "failed"


def test_evaluate_ledger_separates_malformed_and_unavailable_anchor_evidence() -> None:
    records = [
        {"kind": "ingest_run", "run_id": "run-1", "state": "started"},
        _candidate("run-1", "n1", "node", {"type": "Agent", "title": "Frodo"}),
        _candidate(
            "run-1",
            "e1",
            "edge",
            {
                "type": "speaks",
                "src_ref": "n1",
                "dst_literal": "hello",
                "block_id": "block-1",
            },
        ),
        {
            "kind": "commit_plan",
            "run_id": "run-1",
            "plan_id": "plan-1",
            "operations": [
                {
                    "operation": "create_node",
                    "candidate_kind": "node",
                    "candidate_id": "n1",
                    "type": "Agent",
                },
                {
                    "operation": "create_edge_or_claim",
                    "candidate_kind": "edge",
                    "candidate_id": "e1",
                    "type": "speaks",
                    "src_ref": "n1",
                    "dst_literal": "hello",
                },
                {
                    "operation": "create_edge_or_claim",
                    "candidate_kind": "edge",
                    "candidate_id": "e2",
                    "type": "knows",
                    "src_ref": "n1",
                    "dst_literal": "Sam",
                },
            ],
        },
    ]

    report = evaluate_ledger(records, ["run-1"])

    anchors = report["layers"]["relation"]["accepted_anchor_shape"]
    assert anchors["invalid_count"] == 1
    assert anchors["unavailable_count"] == 1
    anchor_check = next(
        row
        for row in report["hard_invariants"]["checks"]
        if row["code"] == "planned_relation_anchor_well_formed"
    )
    assert anchor_check["status"] == "failed"


def test_evaluate_ledger_rejects_invalid_sample_limit() -> None:
    with pytest.raises(ValueError, match="sample_limit"):
        evaluate_ledger([], ["run-1"], sample_limit=0)
