from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from okto_neuron.semantic_adjudication import (
    evaluate_ledger_adjudication,
    evidence_identity,
)
from okto_neuron.semantic_acceptance import (
    CHURN_COMPARISONS,
    QUALITY_METRICS,
    REQUIRED_CORPUS_CLASSES,
    evaluate_semantic_acceptance,
    inspect_semantic_acceptance_collection,
    materialize_semantic_acceptance_collection,
)
from okto_neuron.semantic_quality import evaluate_ledger


_ADVERSARIAL_FIXTURE = (
    Path(__file__).parent / "fixtures" / "semantic_quality" / "adversarial-ledger.v1.json"
)
_HASH_A = f"sha256:{'a' * 64}"
_HASH_B = f"sha256:{'b' * 64}"
_HASH_C = f"sha256:{'c' * 64}"


def _snapshot_dimension(*members: str) -> dict[str, object]:
    ordered = sorted(set(members))
    digest = hashlib.sha256()
    for member in ordered:
        digest.update(member.encode("ascii"))
        digest.update(b"\n")
    return {
        "count": len(ordered),
        "sha256": f"sha256:{digest.hexdigest()}",
        "members": ordered,
    }


def _acceptance_snapshot(
    generation: str,
    *,
    semantic_policy: str = _HASH_C,
    identities: tuple[str, ...] = (_HASH_A,),
) -> dict[str, object]:
    return {
        "schema_version": "semantic_snapshot.v1",
        "status": "measured",
        "graph_generation": generation,
        "member_encoding": "sha256(kind + NUL + canonical_semantic_value)",
        "fingerprints": {
            "status": "measured",
            "config": _HASH_A,
            "extraction": _HASH_B,
            "semantic_policy": semantic_policy,
        },
        "dimensions": {
            "identities": _snapshot_dimension(*identities),
            "predicates": _snapshot_dimension(_HASH_B),
            "relations": _snapshot_dimension(_HASH_C),
        },
    }


def _acceptance_report(snapshot: dict[str, object]) -> dict[str, object]:
    metric = {"accuracy": 1.0}
    return {
        "schema_version": "semantic_quality.v1",
        "evidence": {
            "complete": True,
            "technical_integrity_verified": True,
            "integrity_freshness": "fresh",
            "graph_generation": snapshot["graph_generation"],
        },
        "rebuild_gate": {"status": "passed"},
        "hard_invariants": {"status": "passed"},
        "layers": {
            "recall": {
                "status": "measured",
                "completion": {"calls_total": 0, "generated_tokens_total": 0},
            },
            "adjudication": {
                "status": "measured",
                "entity": {
                    "b3_cluster_quality": {"f1": 1.0},
                    "type_accuracy": metric,
                },
                "relation": {
                    "predicate_accuracy": metric,
                    "direction_accuracy": metric,
                    "object_kind_accuracy": metric,
                    "grounding_quality": {"f1": 1.0},
                    "usefulness_quality": {"f1": 1.0},
                },
            },
        },
        "semantic_snapshot": snapshot,
    }


def _construction_measurement(
    *,
    config_sha256: str,
    semantic_policy_fingerprint: str,
    graph_generation: str,
    evidence_sha256: str,
    quality_value: float,
    completion_calls: int,
) -> dict[str, object]:
    return {
        "config_sha256": config_sha256,
        "semantic_policy_fingerprint": semantic_policy_fingerprint,
        "graph_generation": graph_generation,
        "evidence_sha256": evidence_sha256,
        "quality_value": quality_value,
        "ingest_elapsed_ms": 1000.0 + completion_calls,
        "construction_cost": {
            "completion_calls": completion_calls,
            "input_tokens": completion_calls * 10,
            "output_tokens": completion_calls * 2,
            "embedding_calls": 1,
            "embedding_inputs": 4,
        },
    }


def _acceptance_bundle() -> dict[str, object]:
    baseline = _acceptance_snapshot("generation-baseline")
    snapshots = {
        "baseline": baseline,
        "identical_reingest": _acceptance_snapshot("generation-reingest"),
        "source_order_a": _acceptance_snapshot("generation-order-a"),
        "source_order_b": _acceptance_snapshot("generation-order-b"),
        "provider_restart": _acceptance_snapshot("generation-provider-restart"),
        "fresh_rebuild": _acceptance_snapshot("generation-fresh-rebuild"),
        "rollback": _acceptance_snapshot("generation-baseline"),
        "policy_before": _acceptance_snapshot("generation-baseline"),
        "policy_after": _acceptance_snapshot(
            "generation-policy-after",
            semantic_policy=_HASH_B,
        ),
    }
    corpora = []
    for corpus_class in REQUIRED_CORPUS_CLASSES:
        corpus_id = f"fixture-{corpus_class}"
        corpus_snapshots = deepcopy(snapshots)
        stored = _acceptance_report(deepcopy(corpus_snapshots["baseline"]))
        corpora.append(
            {
                "corpus_id": corpus_id,
                "corpus_class": corpus_class,
                "manifest_sha256": _HASH_A,
                "stored_report": stored,
                "adjudicated_report": deepcopy(stored),
                "snapshots": corpus_snapshots,
                "policy_change": {
                    "schema_version": "semantic_policy_change_evidence.v1",
                    "corpus_id": corpus_id,
                    "manifest_sha256": _HASH_A,
                    "before_fingerprint": _HASH_C,
                    "after_fingerprint": _HASH_B,
                    "before_generation": "generation-baseline",
                    "after_generation": "generation-policy-after",
                    "before_run_id": f"{corpus_id}-before",
                    "after_run_id": f"{corpus_id}-after",
                    "changed_fields": ["consolidation.relation_curator_enabled"],
                    "incompatible_reuse": {
                        "attempted": True,
                        "rejected": True,
                        "evidence_sha256": _HASH_A,
                    },
                    "recompute": {"completed": True, "evidence_sha256": _HASH_B},
                },
                "byte_grounding": {
                    "schema_version": "byte_grounding_evidence.v1",
                    "corpus_id": corpus_id,
                    "manifest_sha256": _HASH_A,
                    "graph_generation": "generation-baseline",
                    "semantic_policy_fingerprint": _HASH_C,
                    "samples": [
                        {
                            "sample_id": f"{corpus_id}-relation-1",
                            "relation_claim_id": "claim-1",
                            "source_id": "source-1",
                            "source_sha256": _HASH_A,
                            "start_byte": 10,
                            "end_byte": 20,
                            "excerpt_sha256": _HASH_B,
                            "verified": True,
                        }
                    ],
                },
                "review_coverage": {
                    "schema_version": "semantic_review_coverage.v1",
                    "corpus_id": corpus_id,
                    "manifest_sha256": _HASH_A,
                    "graph_generation": "generation-baseline",
                    "adjudication_sha256": _HASH_B,
                    "reviewer": {"kind": "human", "id": "fixture-reviewer"},
                    "reviewed_at": "2026-07-18T12:00:00+00:00",
                    "primitive_samples": [
                        {
                            "sample_id": f"{corpus_id}-primitive-{primitive}",
                            "primitive": primitive,
                            "decision_sha256": _HASH_A,
                        }
                        for primitive in (
                            "Agent",
                            "Activity",
                            "InformationObject",
                            "Concept",
                            "Place",
                        )
                    ],
                    "predicate_state_samples": [
                        {
                            "sample_id": f"{corpus_id}-predicate-{state}",
                            "predicate_state": state,
                            "decision_sha256": _HASH_B,
                        }
                        for state in ("canonical", "provisional")
                    ],
                },
                "ablations": {
                    "schema_version": "model_stage_ablations.v1",
                    "corpus_id": corpus_id,
                    "manifest_sha256": _HASH_A,
                    "arms": [
                        {
                            "stage": "semantic-judge",
                            "quality_metric": "predicate_accuracy",
                            "changed_fields": ["consolidation.relation_curator_enabled"],
                            "disabled": _construction_measurement(
                                config_sha256=_HASH_A,
                                semantic_policy_fingerprint=_HASH_A,
                                graph_generation="generation-ablation-disabled",
                                evidence_sha256=_HASH_A,
                                quality_value=0.9,
                                completion_calls=0,
                            ),
                            "enabled": _construction_measurement(
                                config_sha256=_HASH_B,
                                semantic_policy_fingerprint=_HASH_B,
                                graph_generation="generation-ablation-enabled",
                                evidence_sha256=_HASH_B,
                                quality_value=1.0,
                                completion_calls=2,
                            ),
                        }
                    ],
                },
            }
        )
    diagnostic_arm = {
        "status": "measured",
        "artifact_sha256": _HASH_A,
        "case_count": 35,
    }
    return {
        "schema_version": "semantic_acceptance_matrix.v1",
        "threshold_policy": {
            "schema_version": "semantic_threshold_policy.v2",
            "policy_id": "adr0040-test-policy-v1",
            "semantic_policy_fingerprint": _HASH_C,
            "corpus_classes": {
                corpus_class: {
                    "quality_minimums": {name: 0.9 for name in QUALITY_METRICS},
                    "churn_maximums": {
                        name: {
                            dimension: 0.0
                            for dimension in ("identities", "predicates", "relations")
                        }
                        for name in CHURN_COMPARISONS
                    },
                    "derivation": {
                        "status": "locked",
                        "corpus_id": f"fixture-{corpus_class}",
                        "manifest_sha256": _HASH_A,
                        "adjudication_sha256": _HASH_B,
                        "variance_sha256": _HASH_C,
                        "labeled_sample_count": 7,
                        "baseline_run_count": 3,
                        "reviewer": {"kind": "human", "id": "fixture-reviewer"},
                        "reviewed_at": "2026-07-18T12:00:00+00:00",
                    },
                }
                for corpus_class in REQUIRED_CORPUS_CLASSES
            },
        },
        "corpora": corpora,
        "temporal_correction": {
            "schema_version": "temporal_correction.v1",
            "status": "measured",
            "corpus_id": "fixture-chat",
            "artifact_sha256": _HASH_A,
            "source_time_preserved": True,
            "correction_order_preserved": True,
            "latest_correction_retrievable": True,
            "first_class_valid_time_claimed": False,
        },
        "public_diagnostic": {
            "status": "measured",
            "manifest_sha256": _HASH_A,
            "marginalia_retrieval": deepcopy(diagnostic_arm),
            "direct_rag": deepcopy(diagnostic_arm),
            "flat_bm25": deepcopy(diagnostic_arm),
        },
    }


def _pinned_json(
    root: Path,
    name: str,
    value: object,
) -> dict[str, str]:
    rendered = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path = root / name
    path.write_bytes(rendered)
    return {
        "path": name,
        "sha256": f"sha256:{hashlib.sha256(rendered).hexdigest()}",
    }


def _acceptance_collection(tmp_path: Path) -> tuple[dict[str, object], dict[str, Path]]:
    bundle = _acceptance_bundle()
    paths: dict[str, Path] = {}

    def pin(name: str, value: object) -> dict[str, str]:
        reference = _pinned_json(tmp_path, name, value)
        paths[name] = tmp_path / name
        return reference

    corpora = []
    for corpus in bundle["corpora"]:
        corpus_id = corpus["corpus_id"]
        manifest = pin(f"{corpus_id}.manifest.json", {"corpus_id": corpus_id})
        manifest_sha256 = manifest["sha256"]
        policy_change = deepcopy(corpus["policy_change"])
        byte_grounding = deepcopy(corpus["byte_grounding"])
        review_coverage = deepcopy(corpus["review_coverage"])
        ablations = deepcopy(corpus["ablations"])
        for evidence in (policy_change, byte_grounding, review_coverage, ablations):
            evidence["manifest_sha256"] = manifest_sha256
        bundle["threshold_policy"]["corpus_classes"][corpus["corpus_class"]]["derivation"][
            "manifest_sha256"
        ] = manifest_sha256
        snapshots = {
            arm: pin(f"{corpus_id}.snapshot.{arm}.json", snapshot)
            for arm, snapshot in corpus["snapshots"].items()
        }
        corpora.append(
            {
                "corpus_id": corpus_id,
                "corpus_class": corpus["corpus_class"],
                "manifest": manifest,
                "stored_report": pin(f"{corpus_id}.stored.json", corpus["stored_report"]),
                "adjudicated_report": pin(
                    f"{corpus_id}.adjudicated.json", corpus["adjudicated_report"]
                ),
                "snapshots": snapshots,
                "policy_change": pin(f"{corpus_id}.policy-change.json", policy_change),
                "byte_grounding": pin(f"{corpus_id}.byte-grounding.json", byte_grounding),
                "review_coverage": pin(f"{corpus_id}.review-coverage.json", review_coverage),
                "ablations": pin(f"{corpus_id}.ablations.json", ablations),
            }
        )
    return (
        {
            "schema_version": "semantic_acceptance_collection.v1",
            "threshold_policy": pin("threshold-policy.json", bundle["threshold_policy"]),
            "corpora": corpora,
            "temporal_correction": pin("temporal-correction.json", bundle["temporal_correction"]),
            "public_diagnostic": pin("public-diagnostic.json", bundle["public_diagnostic"]),
        },
        paths,
    )


def _adjudication() -> dict[str, object]:
    return {
        "schema_version": "semantic_adjudication.v1",
        "fixture_id": "adversarial-smoke",
        "instructions_version": "adr0040.v1",
        "adjudicator": "fixture-author",
        "entities": [
            {
                "candidate_id": "aragorn",
                "expected_type": "Agent",
                "expected_cluster_id": "aragorn-sense",
                "expected_canonical_candidate_id": "aragorn",
                "expected_canonical_title": "Aragorn",
                "rationale": "Named person.",
            },
            {
                "candidate_id": "strider",
                "expected_type": "Agent",
                "expected_cluster_id": "aragorn-sense",
                "expected_canonical_candidate_id": "aragorn",
                "expected_canonical_title": "Aragorn",
                "rationale": "Alias of Aragorn in this source.",
            },
            {
                "candidate_id": "work",
                "expected_type": "InformationObject",
                "expected_cluster_id": "work-sense",
                "expected_canonical_candidate_id": "work",
                "expected_canonical_title": "The Ring",
                "rationale": "A named work, not the object.",
            },
            {
                "candidate_id": "object",
                "expected_type": "Concept",
                "expected_cluster_id": "object-sense",
                "expected_canonical_candidate_id": "object",
                "expected_canonical_title": "The Ring",
                "rationale": "The in-world object, not the work.",
            },
        ],
        "relations": [
            {
                "candidate_id": "lives",
                "expected_disposition": "accept",
                "expected_predicate": "lives_in",
                "expected_src_ref": "aragorn",
                "expected_object_kind": "topology",
                "expected_dst_ref": "place",
                "expected_dst_literal": None,
                "grounded": True,
                "useful": True,
                "rationale": "The source states the directed fact.",
            },
            {
                "candidate_id": "noise",
                "expected_disposition": "reject",
                "expected_predicate": "mentions",
                "expected_src_ref": "work",
                "expected_object_kind": "literal",
                "expected_dst_ref": None,
                "expected_dst_literal": "chapter",
                "grounded": False,
                "useful": False,
                "rationale": "Document structure is not semantic topology.",
            },
        ],
    }


def _inputs() -> dict[str, object]:
    return {
        "proposed_nodes": [
            {"candidate_id": "aragorn", "payload": {"type": "Agent", "title": "Aragorn"}},
            {"candidate_id": "strider", "payload": {"type": "Agent", "title": "Strider"}},
            {
                "candidate_id": "work",
                "payload": {"type": "InformationObject", "title": "The Ring"},
            },
            {"candidate_id": "object", "payload": {"type": "Concept", "title": "The Ring"}},
        ],
        "proposed_relations": [
            {
                "candidate_id": "lives",
                "payload": {
                    "type": "lives_in",
                    "src_ref": "aragorn",
                    "dst_ref": "place",
                },
            },
            {
                "candidate_id": "noise",
                "payload": {
                    "type": "mentions",
                    "src_ref": "work",
                    "dst_literal": "chapter",
                },
            },
        ],
        "planned_node_operations": [
            {
                "operation": "create_node",
                "candidate_id": "aragorn",
                "type": "Agent",
                "title": "Aragorn",
            },
            {
                "operation": "supersede_candidate",
                "candidate_id": "strider",
                "type": "Agent",
                "title": "Strider",
                "target_ref": "aragorn",
            },
            {
                "operation": "create_node",
                "candidate_id": "work",
                "type": "InformationObject",
                "title": "The Ring",
            },
            {
                "operation": "supersede_candidate",
                "candidate_id": "object",
                "type": "Concept",
                "title": "The Ring",
                "target_ref": "work",
            },
        ],
        "planned_relation_operations": [
            {
                "operation": "create_edge_or_claim",
                "candidate_id": "lives",
                "type": "lives_in",
                "src_ref": "aragorn",
                "dst_ref": "place",
                "dst_literal": None,
            },
            {
                "operation": "create_edge_or_claim",
                "candidate_id": "noise",
                "type": "mentions",
                "src_ref": "work",
                "dst_ref": None,
                "dst_literal": "chapter",
            },
        ],
    }


def test_adjudication_measures_b3_type_alias_and_relation_errors() -> None:
    result = evaluate_ledger_adjudication(
        _adjudication(),
        **_inputs(),
        sample_limit=10,
    )

    assert result["status"] == "measured"
    assert len(result["sha256"]) == 64
    assert len(result["normalized_sha256"]) == 64
    assert result["coverage"]["entity_plan_operations"]["status"] == "complete"
    assert result["entity"]["type_accuracy"]["accuracy"] == 1.0
    assert result["entity"]["b3_cluster_quality"] == {
        "status": "measured",
        "precision": 0.75,
        "recall": 1.0,
        "f1": 0.857143,
    }
    pairwise = result["entity"]["pairwise_cluster_errors"]
    assert pairwise["false_merges"]["count"] == 1
    assert pairwise["cross_type_false_merges"]["count"] == 1
    assert pairwise["missed_merges"]["count"] == 0
    assert result["entity"]["alias_assignment_accuracy"]["accuracy"] == 1.0
    assert result["relation"]["decision_accuracy"]["accuracy"] == 0.5
    assert result["relation"]["grounding_quality"]["false_positive"] == 1
    assert result["relation"]["usefulness_quality"]["false_positive"] == 1


def test_adjudication_missing_plan_operation_is_measured_as_incomplete() -> None:
    inputs = _inputs()
    inputs["planned_node_operations"] = list(inputs["planned_node_operations"])[1:]

    result = evaluate_ledger_adjudication(
        _adjudication(),
        **inputs,
        sample_limit=10,
    )

    coverage = result["coverage"]["entity_plan_operations"]
    assert coverage["status"] == "incomplete"
    assert coverage["missing_samples"] == ["aragorn"]
    assert result["entity"]["type_accuracy"]["accuracy"] == 0.75


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(schema_version="semantic_adjudication.v2"),
        lambda value: value.update(extra=True),
        lambda value: value["entities"].append(deepcopy(value["entities"][0])),
        lambda value: value["relations"][0].update(expected_dst_literal="also-set"),
        lambda value: value["entities"][0].update(expected_type="Document"),
    ],
)
def test_adjudication_fails_closed_on_invalid_contract(mutate) -> None:
    adjudication = _adjudication()
    mutate(adjudication)

    with pytest.raises(ValueError):
        evaluate_ledger_adjudication(
            adjudication,
            **_inputs(),
            sample_limit=10,
        )


def test_adjudication_must_bind_to_selected_candidate_population() -> None:
    adjudication = _adjudication()
    adjudication["entities"][0]["candidate_id"] = "absent"

    with pytest.raises(ValueError, match="absent from selected ledger"):
        evaluate_ledger_adjudication(
            adjudication,
            **_inputs(),
            sample_limit=10,
        )


def test_external_evidence_identity_is_canonical_and_never_echoes_values() -> None:
    left = evidence_identity([{"b": 2, "a": 1}], logical_type="recall_cost.v1[]")
    right = evidence_identity([{"a": 1, "b": 2}], logical_type="recall_cost.v1[]")

    assert left == right
    assert left["count"] == 1
    assert "a" not in left
    with pytest.raises(ValueError, match="JSON-serializable"):
        evidence_identity({"bad": object()}, logical_type="invalid")


def test_adjudication_raw_and_normalized_hashes_are_distinct_and_explicit() -> None:
    adjudication = _adjudication()
    adjudication["fixture_id"] = "  adversarial-smoke  "

    result = evaluate_ledger_adjudication(
        adjudication,
        **_inputs(),
        sample_limit=10,
    )

    assert (
        result["sha256"]
        == evidence_identity(
            adjudication,
            logical_type="semantic_adjudication.v1",
        )["sha256"]
    )
    assert result["normalized_sha256"] != result["sha256"]


def test_repository_adversarial_fixture_is_self_contained_and_reproducible() -> None:
    fixture = json.loads(_ADVERSARIAL_FIXTURE.read_text(encoding="utf-8"))
    assert set(fixture) == {
        "fixture_schema",
        "run_ids",
        "registered_predicates",
        "records",
        "recall_samples",
        "adjudication",
    }
    assert fixture["fixture_schema"] == "semantic_quality_fixture.v1"

    report = evaluate_ledger(
        fixture["records"],
        fixture["run_ids"],
        registered_predicates=fixture["registered_predicates"],
        recall_samples=fixture["recall_samples"],
        adjudication=fixture["adjudication"],
    )

    assert report["layers"]["surface"]["source_surface_preservation"]["status"] == ("measured")
    assert report["layers"]["type"]["type_accuracy"]["accuracy"] == 0.9
    assert report["layers"]["type"]["type_accuracy"]["macro_f1"] == 0.733333
    identity = report["layers"]["identity"]
    assert identity["b3_cluster_quality"]["f1"] == 0.9
    assert identity["cluster_quality"]["pairwise_cluster_errors"]["false_merges"]["count"] == 1
    assert identity["cluster_quality"]["pairwise_cluster_errors"]["missed_merges"]["count"] == 1
    relation = report["layers"]["relation"]["adjudicated_quality"]
    assert relation["predicate_accuracy"]["accuracy"] == 0.857143
    assert relation["direction_accuracy"]["accuracy"] == 0.857143
    assert relation["object_kind_accuracy"]["accuracy"] == 0.857143
    assert relation["object_value_accuracy"]["accuracy"] == 0.714286
    assert relation["grounding_quality"]["f1"] == 0.8
    assert report["layers"]["recall"]["completion"]["calls_total"] == 0
    assert report["hard_invariants"]["status"] == "failed"

    repeated = evaluate_ledger(
        deepcopy(fixture["records"]),
        fixture["run_ids"],
        registered_predicates=reversed(fixture["registered_predicates"]),
        recall_samples=deepcopy(fixture["recall_samples"]),
        adjudication=deepcopy(fixture["adjudication"]),
    )
    assert report["evidence"]["external_inputs"] == repeated["evidence"]["external_inputs"]
    assert report["layers"]["adjudication"] == repeated["layers"]["adjudication"]


@pytest.mark.parametrize("predicates", [[1], [" uses "]])
def test_registered_predicate_evidence_fails_closed_before_measurement(predicates) -> None:
    fixture = json.loads(_ADVERSARIAL_FIXTURE.read_text(encoding="utf-8"))

    with pytest.raises(ValueError, match="registered_predicates"):
        evaluate_ledger(
            fixture["records"],
            fixture["run_ids"],
            registered_predicates=predicates,
            adjudication=fixture["adjudication"],
        )


def test_semantic_acceptance_requires_complete_multi_corpus_evidence() -> None:
    result = evaluate_semantic_acceptance(_acceptance_bundle())

    assert result["gating_status"] == "passed"
    assert result["public_diagnostic_status"] == "complete"
    assert result["temporal_correction_status"] == "complete"
    assert result["acceptance_ready"] is True
    assert result["failed_codes"] == []
    assert len(result["corpora"]) == 4


def test_semantic_acceptance_keeps_public_diagnostic_out_of_internal_gate() -> None:
    bundle = _acceptance_bundle()
    bundle["public_diagnostic"]["status"] = "not_measured"
    for name in ("marginalia_retrieval", "direct_rag", "flat_bm25"):
        bundle["public_diagnostic"][name] = {
            "status": "not_measured",
            "artifact_sha256": None,
            "case_count": 0,
        }

    result = evaluate_semantic_acceptance(bundle)

    assert result["gating_status"] == "passed"
    assert result["public_diagnostic_status"] == "incomplete"
    assert result["acceptance_ready"] is False


def test_semantic_acceptance_requires_bounded_temporal_correction_evidence() -> None:
    bundle = _acceptance_bundle()
    bundle["temporal_correction"] = {
        "schema_version": "temporal_correction.v1",
        "status": "not_measured",
        "corpus_id": "fixture-chat",
        "artifact_sha256": None,
        "source_time_preserved": None,
        "correction_order_preserved": None,
        "latest_correction_retrievable": None,
        "first_class_valid_time_claimed": None,
    }

    result = evaluate_semantic_acceptance(bundle)

    assert result["gating_status"] == "failed"
    assert result["temporal_correction_status"] == "incomplete"
    assert "matrix:temporal_correction" in result["failed_codes"]


def test_semantic_acceptance_fails_quality_and_exact_churn_regressions() -> None:
    bundle = _acceptance_bundle()
    literary = bundle["corpora"][1]
    literary["adjudicated_report"]["layers"]["adjudication"]["entity"]["type_accuracy"][
        "accuracy"
    ] = 0.8
    literary["snapshots"]["identical_reingest"] = _acceptance_snapshot(
        "generation-reingest",
        identities=(_HASH_A, _HASH_B),
    )

    result = evaluate_semantic_acceptance(bundle)

    assert result["gating_status"] == "failed"
    assert result["acceptance_ready"] is False
    assert "corpus:fixture-literary:threshold:type_accuracy" in result["failed_codes"]
    assert "corpus:fixture-literary:churn:identical_reingest:identities" in result["failed_codes"]


def test_semantic_acceptance_binds_policy_change_to_materialized_snapshots() -> None:
    bundle = _acceptance_bundle()
    bundle["corpora"][0]["policy_change"]["after_fingerprint"] = _HASH_A

    result = evaluate_semantic_acceptance(bundle)

    assert result["gating_status"] == "failed"
    assert "corpus:fixture-synthetic_adversarial:policy_change_recomputed" in result["failed_codes"]


def test_semantic_acceptance_binds_thresholds_and_fresh_rebuild_generation() -> None:
    bundle = _acceptance_bundle()
    bundle["threshold_policy"]["semantic_policy_fingerprint"] = _HASH_A
    bundle["corpora"][0]["snapshots"]["fresh_rebuild"] = _acceptance_snapshot("generation-baseline")

    result = evaluate_semantic_acceptance(bundle)

    assert "corpus:fixture-synthetic_adversarial:threshold_policy_binding" in result["failed_codes"]
    assert "corpus:fixture-synthetic_adversarial:fresh_rebuild_generation" in result["failed_codes"]


def test_semantic_acceptance_treats_malformed_ablation_as_failed_evidence() -> None:
    bundle = _acceptance_bundle()
    bundle["corpora"][0]["ablations"]["arms"] = [1]

    result = evaluate_semantic_acceptance(bundle)

    assert "corpus:fixture-synthetic_adversarial:model_stage_ablations" in result["failed_codes"]


def test_semantic_acceptance_rejects_incomplete_threshold_contract() -> None:
    bundle = _acceptance_bundle()
    del bundle["threshold_policy"]["corpus_classes"]["chat"]["quality_minimums"]["grounding_f1"]

    with pytest.raises(ValueError, match="quality_minimums"):
        evaluate_semantic_acceptance(bundle)


def test_semantic_acceptance_rejects_malformed_review_coverage() -> None:
    bundle = _acceptance_bundle()
    bundle["corpora"][0]["review_coverage"]["primitive_samples"] = [{}]

    with pytest.raises(ValueError, match="review_coverage.primitive_samples"):
        evaluate_semantic_acceptance(bundle)


def test_semantic_acceptance_rejects_non_human_or_single_run_threshold_lock() -> None:
    bundle = _acceptance_bundle()
    derivation = bundle["threshold_policy"]["corpus_classes"]["literary"]["derivation"]
    derivation["reviewer"]["kind"] = "model"

    with pytest.raises(ValueError, match="kind must be human"):
        evaluate_semantic_acceptance(bundle)

    bundle = _acceptance_bundle()
    bundle["threshold_policy"]["corpus_classes"]["literary"]["derivation"]["baseline_run_count"] = 1
    with pytest.raises(ValueError, match="baseline_run_count must be an integer >= 2"):
        evaluate_semantic_acceptance(bundle)


def test_semantic_acceptance_fails_corpus_unbound_evidence() -> None:
    bundle = _acceptance_bundle()
    literary = bundle["corpora"][1]
    literary["byte_grounding"]["corpus_id"] = "different-corpus"
    literary["review_coverage"]["graph_generation"] = "different-generation"
    literary["ablations"]["manifest_sha256"] = _HASH_B

    result = evaluate_semantic_acceptance(bundle)

    assert "corpus:fixture-literary:byte_grounding" in result["failed_codes"]
    assert "corpus:fixture-literary:review_coverage" in result["failed_codes"]
    assert "corpus:fixture-literary:model_stage_ablations" in result["failed_codes"]


def test_semantic_acceptance_collection_materializes_only_pinned_artifacts(
    tmp_path: Path,
) -> None:
    collection, _ = _acceptance_collection(tmp_path)

    status = inspect_semantic_acceptance_collection(collection, base_dir=tmp_path)
    bundle = materialize_semantic_acceptance_collection(collection, base_dir=tmp_path)

    assert status["status"] == "ready"
    assert status["counts"] == {
        "ready": 67,
        "missing": 0,
        "invalid": 0,
        "total": 67,
    }
    assert str(tmp_path) not in json.dumps(status)
    assert evaluate_semantic_acceptance(bundle)["acceptance_ready"] is True
    for corpus, reference in zip(bundle["corpora"], collection["corpora"], strict=True):
        assert corpus["manifest_sha256"] == reference["manifest"]["sha256"]


def test_semantic_acceptance_collection_invalidates_counter_only_grounding(
    tmp_path: Path,
) -> None:
    collection, _ = _acceptance_collection(tmp_path)
    collection["corpora"][0]["byte_grounding"] = _pinned_json(
        tmp_path,
        "counter-only-byte-grounding.json",
        {"sample_count": 130, "verified_count": 130},
    )

    status = inspect_semantic_acceptance_collection(collection, base_dir=tmp_path)

    invalid = [row for row in status["artifacts"] if row["status"] == "invalid"]
    assert [row["code"] for row in invalid] == [
        "corpus:fixture-synthetic_adversarial:byte_grounding"
    ]
    assert "samples" in invalid[0]["reason"]
    assert "sample_count" in invalid[0]["reason"]


def test_semantic_acceptance_collection_reuses_product_quality_envelopes(
    tmp_path: Path,
) -> None:
    collection, _ = _acceptance_collection(tmp_path)
    bundle = _acceptance_bundle()
    first = collection["corpora"][0]
    source = bundle["corpora"][0]

    first["stored_report"] = _pinned_json(
        tmp_path,
        "fixture-envelope.stored.json",
        {"status": "ok", "semantic_quality": source["stored_report"], "capture": {}},
    )
    first["adjudicated_report"] = _pinned_json(
        tmp_path,
        "fixture-envelope.adjudicated.json",
        {
            "status": "ok",
            "semantic_quality": source["adjudicated_report"],
            "capture": {},
        },
    )
    for arm, snapshot in source["snapshots"].items():
        first["snapshots"][arm] = _pinned_json(
            tmp_path,
            f"fixture-envelope.snapshot.{arm}.json",
            {
                "status": "ok",
                "semantic_quality": _acceptance_report(snapshot),
                "capture": {},
            },
        )

    status = inspect_semantic_acceptance_collection(collection, base_dir=tmp_path)
    materialized = materialize_semantic_acceptance_collection(
        collection,
        base_dir=tmp_path,
    )

    assert status["status"] == "ready"
    assert materialized["corpora"][0]["stored_report"]["schema_version"] == ("semantic_quality.v1")
    assert materialized["corpora"][0]["snapshots"]["baseline"]["schema_version"] == (
        "semantic_snapshot.v1"
    )


def test_semantic_acceptance_status_rejects_hash_valid_wrong_slot_schema(
    tmp_path: Path,
) -> None:
    collection, _ = _acceptance_collection(tmp_path)
    collection["corpora"][0]["stored_report"] = _pinned_json(
        tmp_path,
        "wrong-stored-schema.json",
        {"schema_version": "not_semantic_quality.v1"},
    )

    status = inspect_semantic_acceptance_collection(collection, base_dir=tmp_path)

    assert status["status"] == "incomplete"
    invalid = [row for row in status["artifacts"] if row["status"] == "invalid"]
    assert [row["code"] for row in invalid] == [
        "corpus:fixture-synthetic_adversarial:stored_report"
    ]
    assert "semantic_quality.v1" in invalid[0]["reason"]


def test_semantic_acceptance_collection_reports_missing_slots_without_materializing(
    tmp_path: Path,
) -> None:
    collection, _ = _acceptance_collection(tmp_path)
    collection["corpora"][3]["snapshots"]["rollback"] = None

    status = inspect_semantic_acceptance_collection(collection, base_dir=tmp_path)

    assert status["status"] == "incomplete"
    assert status["counts"]["missing"] == 1
    assert {row["code"] for row in status["artifacts"] if row["status"] == "missing"} == {
        "corpus:fixture-chat:snapshot:rollback"
    }
    with pytest.raises(ValueError, match="snapshot:rollback=missing"):
        materialize_semantic_acceptance_collection(collection, base_dir=tmp_path)


def test_semantic_acceptance_collection_rejects_artifact_drift(tmp_path: Path) -> None:
    collection, paths = _acceptance_collection(tmp_path)
    paths["fixture-literary.stored.json"].write_text("{}\n", encoding="utf-8")

    status = inspect_semantic_acceptance_collection(collection, base_dir=tmp_path)

    assert status["status"] == "incomplete"
    invalid = [row for row in status["artifacts"] if row["status"] == "invalid"]
    assert [row["code"] for row in invalid] == ["corpus:fixture-literary:stored_report"]
    assert "sha256 mismatch" in invalid[0]["reason"]
    with pytest.raises(ValueError, match="stored_report=invalid"):
        materialize_semantic_acceptance_collection(collection, base_dir=tmp_path)


def test_semantic_acceptance_collection_reports_absent_corpus_class(tmp_path: Path) -> None:
    collection, _ = _acceptance_collection(tmp_path)
    collection["corpora"] = collection["corpora"][:-1]

    status = inspect_semantic_acceptance_collection(collection, base_dir=tmp_path)

    assert status["status"] == "incomplete"
    assert status["artifacts"][-3] == {
        "code": "corpus:chat",
        "expected": "corpus",
        "status": "missing",
    }
