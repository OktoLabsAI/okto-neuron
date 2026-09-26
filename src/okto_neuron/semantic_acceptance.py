"""Fail-closed ADR 0040 multi-corpus acceptance evaluation.

The evaluator consumes already-captured, immutable quality evidence. It never
opens a graph, calls a model, or invents thresholds from the evidence under
test. Collection remains the responsibility of the live acceptance driver.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from okto_neuron.semantic_quality import compare_semantic_snapshots

SEMANTIC_ACCEPTANCE_SCHEMA: Final = "semantic_acceptance_matrix.v1"
SEMANTIC_ACCEPTANCE_RESULT_SCHEMA: Final = "semantic_acceptance_result.v1"
SEMANTIC_ACCEPTANCE_COLLECTION_SCHEMA: Final = "semantic_acceptance_collection.v1"
SEMANTIC_ACCEPTANCE_COLLECTION_STATUS_SCHEMA: Final = "semantic_acceptance_collection_status.v1"
SEMANTIC_THRESHOLD_POLICY_SCHEMA: Final = "semantic_threshold_policy.v2"
POLICY_CHANGE_EVIDENCE_SCHEMA: Final = "semantic_policy_change_evidence.v1"
BYTE_GROUNDING_EVIDENCE_SCHEMA: Final = "byte_grounding_evidence.v1"
REVIEW_COVERAGE_EVIDENCE_SCHEMA: Final = "semantic_review_coverage.v1"
MODEL_STAGE_ABLATIONS_SCHEMA: Final = "model_stage_ablations.v1"
TEMPORAL_CORRECTION_SCHEMA: Final = "temporal_correction.v1"

REQUIRED_CORPUS_CLASSES: Final[tuple[str, ...]] = (
    "synthetic_adversarial",
    "literary",
    "sdlc_organizational",
    "chat",
)
REQUIRED_PRIMITIVES: Final[frozenset[str]] = frozenset(
    {"Agent", "Activity", "InformationObject", "Concept", "Place"}
)
REQUIRED_PREDICATE_STATES: Final[frozenset[str]] = frozenset({"canonical", "provisional"})
QUALITY_METRICS: Final[dict[str, tuple[str, ...]]] = {
    "b3_f1": ("layers", "adjudication", "entity", "b3_cluster_quality", "f1"),
    "type_accuracy": (
        "layers",
        "adjudication",
        "entity",
        "type_accuracy",
        "accuracy",
    ),
    "predicate_accuracy": (
        "layers",
        "adjudication",
        "relation",
        "predicate_accuracy",
        "accuracy",
    ),
    "direction_accuracy": (
        "layers",
        "adjudication",
        "relation",
        "direction_accuracy",
        "accuracy",
    ),
    "literal_topology_accuracy": (
        "layers",
        "adjudication",
        "relation",
        "object_kind_accuracy",
        "accuracy",
    ),
    "grounding_f1": (
        "layers",
        "adjudication",
        "relation",
        "grounding_quality",
        "f1",
    ),
    "usefulness_f1": (
        "layers",
        "adjudication",
        "relation",
        "usefulness_quality",
        "f1",
    ),
}
CHURN_COMPARISONS: Final[dict[str, tuple[str, str]]] = {
    "identical_reingest": ("baseline", "identical_reingest"),
    "source_order": ("source_order_a", "source_order_b"),
    "provider_restart": ("baseline", "provider_restart"),
    "fresh_rebuild": ("baseline", "fresh_rebuild"),
    "rollback": ("baseline", "rollback"),
}
CHURN_DIMENSIONS: Final[tuple[str, ...]] = ("identities", "predicates", "relations")
POLICY_SNAPSHOT_ARMS: Final[tuple[str, ...]] = ("policy_before", "policy_after")
REQUIRED_SNAPSHOT_ARMS: Final[frozenset[str]] = frozenset(
    {
        "baseline",
        *POLICY_SNAPSHOT_ARMS,
        *{item for pair in CHURN_COMPARISONS.values() for item in pair},
    }
)

_COLLECTION_CORPUS_FIELDS: Final[set[str]] = {
    "corpus_id",
    "corpus_class",
    "manifest",
    "stored_report",
    "adjudicated_report",
    "snapshots",
    "policy_change",
    "byte_grounding",
    "review_coverage",
    "ablations",
}


def inspect_semantic_acceptance_collection(
    collection: Mapping[str, Any],
    *,
    base_dir: Path,
) -> dict[str, Any]:
    """Inspect pinned ADR 0040 artifacts without opening a graph or calling a model."""

    _require_mapping(collection, "collection")
    _require_exact_fields(
        collection,
        {
            "schema_version",
            "threshold_policy",
            "corpora",
            "temporal_correction",
            "public_diagnostic",
        },
        "collection",
    )
    if collection.get("schema_version") != SEMANTIC_ACCEPTANCE_COLLECTION_SCHEMA:
        raise ValueError(
            f"collection.schema_version must be {SEMANTIC_ACCEPTANCE_COLLECTION_SCHEMA}"
        )
    raw_corpora = collection.get("corpora")
    if not isinstance(raw_corpora, list):
        raise ValueError("collection.corpora must be a list")

    rows: list[dict[str, Any]] = []
    _probe_collection_artifact(
        rows,
        code="threshold_policy",
        reference=collection.get("threshold_policy"),
        base_dir=base_dir,
        expected="object",
        validator="threshold_policy",
    )

    by_class: dict[str, Mapping[str, Any]] = {}
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_corpora):
        corpus = _require_mapping(raw, f"collection.corpora[{index}]")
        _require_exact_fields(corpus, _COLLECTION_CORPUS_FIELDS, f"collection.corpora[{index}]")
        corpus_id = _required_text(
            corpus.get("corpus_id"),
            f"collection.corpora[{index}].corpus_id",
        )
        corpus_class = _required_text(
            corpus.get("corpus_class"),
            f"collection.corpora[{index}].corpus_class",
        )
        if corpus_class not in REQUIRED_CORPUS_CLASSES:
            raise ValueError(
                f"collection.corpora[{index}].corpus_class is not an ADR 0040 corpus class"
            )
        if corpus_id in seen_ids:
            raise ValueError(f"collection corpus_id is duplicated: {corpus_id}")
        if corpus_class in by_class:
            raise ValueError(f"collection corpus_class is duplicated: {corpus_class}")
        seen_ids.add(corpus_id)
        by_class[corpus_class] = corpus

    for corpus_class in REQUIRED_CORPUS_CLASSES:
        corpus = by_class.get(corpus_class)
        if corpus is None:
            rows.append(
                {
                    "code": f"corpus:{corpus_class}",
                    "expected": "corpus",
                    "status": "missing",
                }
            )
            continue
        prefix = f"corpus:{corpus['corpus_id']}"
        _probe_collection_artifact(
            rows,
            code=f"{prefix}:manifest",
            reference=corpus.get("manifest"),
            base_dir=base_dir,
            expected="object",
        )
        for field in (
            "stored_report",
            "adjudicated_report",
            "policy_change",
            "byte_grounding",
            "review_coverage",
        ):
            _probe_collection_artifact(
                rows,
                code=f"{prefix}:{field}",
                reference=corpus.get(field),
                base_dir=base_dir,
                expected="object",
                validator={
                    "stored_report": "quality_report",
                    "adjudicated_report": "quality_report",
                    "policy_change": "policy_change",
                    "byte_grounding": "byte_grounding",
                    "review_coverage": "review_coverage",
                }[field],
            )
        _probe_collection_artifact(
            rows,
            code=f"{prefix}:ablations",
            reference=corpus.get("ablations"),
            base_dir=base_dir,
            expected="object",
            validator="ablations",
        )
        snapshots = _require_mapping(corpus.get("snapshots"), f"{prefix}:snapshots")
        if set(snapshots) != set(REQUIRED_SNAPSHOT_ARMS):
            raise ValueError(f"{prefix}:snapshots must define every ADR 0040 scenario arm")
        for arm in sorted(REQUIRED_SNAPSHOT_ARMS):
            _probe_collection_artifact(
                rows,
                code=f"{prefix}:snapshot:{arm}",
                reference=snapshots.get(arm),
                base_dir=base_dir,
                expected="object",
                validator="snapshot",
            )

    for field in ("temporal_correction", "public_diagnostic"):
        _probe_collection_artifact(
            rows,
            code=field,
            reference=collection.get(field),
            base_dir=base_dir,
            expected="object",
            validator=field,
        )

    counts = {
        status: sum(row["status"] == status for row in rows)
        for status in ("ready", "missing", "invalid")
    }
    return {
        "schema_version": SEMANTIC_ACCEPTANCE_COLLECTION_STATUS_SCHEMA,
        "status": "ready" if counts["missing"] == 0 and counts["invalid"] == 0 else "incomplete",
        "counts": {**counts, "total": len(rows)},
        "artifacts": rows,
    }


def materialize_semantic_acceptance_collection(
    collection: Mapping[str, Any],
    *,
    base_dir: Path,
) -> dict[str, Any]:
    """Materialize one immutable acceptance bundle from a complete pinned collection."""

    status = inspect_semantic_acceptance_collection(collection, base_dir=base_dir)
    if status["status"] != "ready":
        failures = [
            f"{row['code']}={row['status']}"
            for row in status["artifacts"]
            if row["status"] != "ready"
        ]
        detail = ", ".join(failures[:5])
        if len(failures) > 5:
            detail += f", and {len(failures) - 5} more"
        raise ValueError(f"acceptance collection is incomplete: {detail}")

    threshold_policy, _ = _load_collection_artifact(
        collection["threshold_policy"], base_dir=base_dir, expected="object"
    )
    corpora: list[dict[str, Any]] = []
    by_class = {row["corpus_class"]: row for row in collection["corpora"]}
    for corpus_class in REQUIRED_CORPUS_CLASSES:
        row = by_class[corpus_class]
        _, manifest_sha256 = _load_collection_artifact(
            row["manifest"], base_dir=base_dir, expected="object"
        )
        snapshots = {
            arm: _extract_semantic_snapshot(
                _load_collection_artifact(
                    row["snapshots"][arm], base_dir=base_dir, expected="object"
                )[0],
                name=f"corpus:{row['corpus_id']}:snapshot:{arm}",
            )
            for arm in sorted(REQUIRED_SNAPSHOT_ARMS)
        }
        corpus = {
            "corpus_id": row["corpus_id"],
            "corpus_class": corpus_class,
            "manifest_sha256": manifest_sha256,
            "stored_report": _extract_quality_report(
                _load_collection_artifact(
                    row["stored_report"], base_dir=base_dir, expected="object"
                )[0],
                name=f"corpus:{row['corpus_id']}:stored_report",
            ),
            "adjudicated_report": _extract_quality_report(
                _load_collection_artifact(
                    row["adjudicated_report"], base_dir=base_dir, expected="object"
                )[0],
                name=f"corpus:{row['corpus_id']}:adjudicated_report",
            ),
            "snapshots": snapshots,
            "policy_change": _load_collection_artifact(
                row["policy_change"], base_dir=base_dir, expected="object"
            )[0],
            "byte_grounding": _load_collection_artifact(
                row["byte_grounding"], base_dir=base_dir, expected="object"
            )[0],
            "review_coverage": _load_collection_artifact(
                row["review_coverage"], base_dir=base_dir, expected="object"
            )[0],
            "ablations": _load_collection_artifact(
                row["ablations"], base_dir=base_dir, expected="object"
            )[0],
        }
        corpora.append(corpus)

    temporal_correction, _ = _load_collection_artifact(
        collection["temporal_correction"], base_dir=base_dir, expected="object"
    )
    public_diagnostic, _ = _load_collection_artifact(
        collection["public_diagnostic"], base_dir=base_dir, expected="object"
    )
    bundle = {
        "schema_version": SEMANTIC_ACCEPTANCE_SCHEMA,
        "threshold_policy": threshold_policy,
        "corpora": corpora,
        "temporal_correction": temporal_correction,
        "public_diagnostic": public_diagnostic,
    }
    evaluate_semantic_acceptance(bundle)
    return bundle


def _probe_collection_artifact(
    rows: list[dict[str, Any]],
    *,
    code: str,
    reference: object,
    base_dir: Path,
    expected: str,
    validator: str | None = None,
) -> None:
    if reference is None:
        rows.append({"code": code, "expected": expected, "status": "missing"})
        return
    try:
        value, digest = _load_collection_artifact(
            reference,
            base_dir=base_dir,
            expected=expected,
        )
        _validate_collection_artifact(value, validator=validator, name=code)
    except ValueError as exc:
        rows.append(
            {
                "code": code,
                "expected": expected,
                "status": "invalid",
                "reason": str(exc),
            }
        )
        return
    rows.append(
        {
            "code": code,
            "expected": expected,
            "status": "ready",
            "sha256": digest,
        }
    )


def _load_collection_artifact(
    reference: object,
    *,
    base_dir: Path,
    expected: str,
) -> tuple[Any, str]:
    row = _require_mapping(reference, "artifact reference")
    _require_exact_fields(row, {"path", "sha256"}, "artifact reference")
    raw_path = _required_text(row.get("path"), "artifact reference.path")
    expected_digest = _required_sha256(row.get("sha256"), "artifact reference.sha256")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"artifact is unreadable: {path.name}: {exc.strerror or exc}") from exc
    actual_digest = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    if actual_digest != expected_digest:
        raise ValueError(
            f"artifact sha256 mismatch: {path.name}: expected {expected_digest}, got {actual_digest}"
        )
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"artifact is not valid UTF-8 JSON: {path.name}: {exc}") from exc
    valid = isinstance(value, dict) if expected == "object" else isinstance(value, list)
    if not valid:
        raise ValueError(f"artifact {path.name} must contain a JSON {expected}")
    return value, actual_digest


def _validate_collection_artifact(value: object, *, validator: str | None, name: str) -> None:
    """Validate the semantic shape of one hash-pinned collection slot."""

    if validator is None:
        return
    if validator == "threshold_policy":
        _validate_threshold_policy(value)
    elif validator == "quality_report":
        _extract_quality_report(value, name=name)
    elif validator == "snapshot":
        _extract_semantic_snapshot(value, name=name)
    elif validator == "policy_change":
        _validate_policy_change(value, name=name)
    elif validator == "byte_grounding":
        _validate_byte_grounding(value, name=name)
    elif validator == "review_coverage":
        _validate_review_coverage(value, name=name)
    elif validator == "ablations":
        _validate_ablations(value, name=name, require_valid=True)
    elif validator == "temporal_correction":
        _validate_temporal_correction(value)
    elif validator == "public_diagnostic":
        _validate_public_diagnostic(value)
    else:  # pragma: no cover - all callers use the closed set above
        raise AssertionError(f"unknown collection validator: {validator}")


def _extract_quality_report(value: object, *, name: str) -> dict[str, Any]:
    """Accept a raw report or the product-owned Golden capture envelope."""

    row = _require_mapping(value, name)
    if row.get("schema_version") == "semantic_quality.v1":
        report: object = row
    else:
        report = row.get("semantic_quality")
    if not isinstance(report, Mapping):
        raise ValueError(f"{name} must contain a semantic_quality.v1 report")
    return _quality_report(report, name)


def _extract_semantic_snapshot(value: object, *, name: str) -> dict[str, Any]:
    """Accept a raw snapshot, quality report, or Golden capture envelope."""

    row = _require_mapping(value, name)
    if row.get("schema_version") == "semantic_snapshot.v1":
        snapshot: object = row
    elif isinstance(row.get("semantic_snapshot"), Mapping):
        snapshot = row["semantic_snapshot"]
    elif isinstance(row.get("semantic_quality"), Mapping):
        snapshot = row["semantic_quality"].get("semantic_snapshot")
    else:
        snapshot = None
    if not isinstance(snapshot, Mapping):
        raise ValueError(f"{name} has no semantic_snapshot object")
    # The comparator is the single semantic_snapshot.v1 validator.  Comparing
    # one snapshot with itself adds no evidence; it only fail-closes malformed
    # dimensions, fingerprints, member hashes, and population digests.
    compare_semantic_snapshots(snapshot, snapshot)
    return dict(snapshot)


def evaluate_semantic_acceptance(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one complete ADR 0040 acceptance evidence bundle."""

    _require_mapping(bundle, "bundle")
    _require_exact_fields(
        bundle,
        {
            "schema_version",
            "threshold_policy",
            "corpora",
            "temporal_correction",
            "public_diagnostic",
        },
        "bundle",
    )
    if bundle.get("schema_version") != SEMANTIC_ACCEPTANCE_SCHEMA:
        raise ValueError(f"schema_version must be {SEMANTIC_ACCEPTANCE_SCHEMA}")
    policy = _validate_threshold_policy(bundle["threshold_policy"])
    raw_corpora = bundle["corpora"]
    if not isinstance(raw_corpora, list):
        raise ValueError("corpora must be a list")

    checks: list[dict[str, Any]] = []
    corpus_results: list[dict[str, Any]] = []
    seen_classes: set[str] = set()
    seen_ids: set[str] = set()
    class_ids: dict[str, str] = {}
    for index, raw in enumerate(raw_corpora):
        corpus = _validate_corpus(raw, index=index)
        corpus_id = corpus["corpus_id"]
        corpus_class = corpus["corpus_class"]
        _check(checks, f"corpus:{corpus_id}:unique_id", corpus_id not in seen_ids)
        _check(
            checks,
            f"corpus:{corpus_id}:unique_class",
            corpus_class not in seen_classes,
        )
        seen_ids.add(corpus_id)
        seen_classes.add(corpus_class)
        class_ids.setdefault(corpus_class, corpus_id)
        result = _evaluate_corpus(
            corpus,
            thresholds=policy["corpus_classes"][corpus_class],
            threshold_policy_fingerprint=policy["semantic_policy_fingerprint"],
            checks=checks,
        )
        corpus_results.append(result)

    missing_classes = sorted(set(REQUIRED_CORPUS_CLASSES) - seen_classes)
    unexpected_classes = sorted(seen_classes - set(REQUIRED_CORPUS_CLASSES))
    _check(
        checks,
        "matrix:required_corpus_classes",
        not missing_classes and not unexpected_classes and len(raw_corpora) == 4,
        detail={"missing": missing_classes, "unexpected": unexpected_classes},
    )

    temporal = _validate_temporal_correction(bundle["temporal_correction"])
    temporal_complete = (
        temporal["status"] == "measured"
        and temporal["corpus_id"] == class_ids.get("chat")
        and temporal["source_time_preserved"] is True
        and temporal["correction_order_preserved"] is True
        and temporal["latest_correction_retrievable"] is True
        and temporal["first_class_valid_time_claimed"] is False
    )
    _check(
        checks,
        "matrix:temporal_correction",
        temporal_complete,
        detail={
            "status": temporal["status"],
            "corpus_id": temporal["corpus_id"],
            "expected_corpus_id": class_ids.get("chat"),
        },
    )

    public = _validate_public_diagnostic(bundle["public_diagnostic"])
    public_complete = public["status"] == "measured" and all(
        public[name]["status"] == "measured"
        for name in ("marginalia_retrieval", "direct_rag", "flat_bm25")
    )
    diagnostic_checks = [
        {
            "code": "public_diagnostic:reproducible_baselines",
            "status": "passed" if public_complete else "incomplete",
        }
    ]

    failed = [check for check in checks if check["status"] == "failed"]
    gating_status = "passed" if not failed else "failed"
    acceptance_ready = gating_status == "passed" and public_complete
    return {
        "schema_version": SEMANTIC_ACCEPTANCE_RESULT_SCHEMA,
        "threshold_policy_id": policy["policy_id"],
        "gating_status": gating_status,
        "public_diagnostic_status": "complete" if public_complete else "incomplete",
        "temporal_correction_status": "complete" if temporal_complete else "incomplete",
        "acceptance_ready": acceptance_ready,
        "corpora": corpus_results,
        "checks": checks,
        "diagnostic_checks": diagnostic_checks,
        "failed_codes": [check["code"] for check in failed],
    }


def _evaluate_corpus(
    corpus: dict[str, Any],
    *,
    thresholds: dict[str, Any],
    threshold_policy_fingerprint: str,
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    corpus_id = corpus["corpus_id"]
    prefix = f"corpus:{corpus_id}"
    stored = corpus["stored_report"]
    adjudicated = corpus["adjudicated_report"]
    stored_snapshot = stored.get("semantic_snapshot")
    adjudicated_snapshot = adjudicated.get("semantic_snapshot")

    _check(
        checks,
        f"{prefix}:technical_integrity",
        _path(stored, "evidence", "complete") is True
        and _path(stored, "evidence", "technical_integrity_verified") is True
        and _path(stored, "evidence", "integrity_freshness") == "fresh"
        and _path(stored, "rebuild_gate", "status") == "passed",
    )
    _check(
        checks,
        f"{prefix}:completion_free_recall",
        _path(stored, "layers", "recall", "status") == "measured"
        and _path(stored, "layers", "recall", "completion", "calls_total") == 0
        and _path(stored, "layers", "recall", "completion", "generated_tokens_total") == 0,
    )
    _check(
        checks,
        f"{prefix}:adjudicated_hard_invariants",
        _path(adjudicated, "evidence", "complete") is True
        and _path(adjudicated, "evidence", "technical_integrity_verified") is True
        and _path(adjudicated, "evidence", "integrity_freshness") == "fresh"
        and _path(adjudicated, "rebuild_gate", "status") == "passed"
        and _path(adjudicated, "hard_invariants", "status") == "passed"
        and _path(adjudicated, "layers", "adjudication", "status") == "measured",
    )

    metrics: dict[str, float | None] = {}
    for name, path in QUALITY_METRICS.items():
        value = _path(adjudicated, *path)
        measured = _unit_float(value)
        metrics[name] = measured
        minimum = thresholds["quality_minimums"][name]
        _check(
            checks,
            f"{prefix}:threshold:{name}",
            measured is not None and measured >= minimum,
            detail={"actual": measured, "minimum": minimum},
        )

    snapshots = corpus["snapshots"]
    baseline_binding = compare_semantic_snapshots(stored_snapshot, snapshots["baseline"])
    adjudicated_binding = compare_semantic_snapshots(
        adjudicated_snapshot,
        snapshots["baseline"],
    )
    baseline_generation = snapshots["baseline"]["graph_generation"]
    _check(
        checks,
        f"{prefix}:baseline_snapshot_binding",
        baseline_binding["stable"] is True
        and adjudicated_binding["stable"] is True
        and _path(stored, "evidence", "graph_generation") == baseline_generation
        and _path(adjudicated, "evidence", "graph_generation") == baseline_generation,
    )
    _check(
        checks,
        f"{prefix}:threshold_policy_binding",
        _path(snapshots["baseline"], "fingerprints", "semantic_policy")
        == threshold_policy_fingerprint
        and thresholds["derivation"]["corpus_id"] == corpus_id
        and thresholds["derivation"]["manifest_sha256"] == corpus["manifest_sha256"]
        and thresholds["derivation"]["adjudication_sha256"]
        == corpus["review_coverage"]["adjudication_sha256"],
    )
    churn_results: dict[str, Any] = {}
    for name, (left_name, right_name) in CHURN_COMPARISONS.items():
        churn = compare_semantic_snapshots(snapshots[left_name], snapshots[right_name])
        churn_results[name] = churn
        for dimension in CHURN_DIMENSIONS:
            actual = churn["dimensions"][dimension]["churn_share"]
            maximum = thresholds["churn_maximums"][name][dimension]
            policy_ok = churn["fingerprints"]["all_equal"] is True
            _check(
                checks,
                f"{prefix}:churn:{name}:{dimension}",
                policy_ok and actual <= maximum,
                detail={
                    "actual": actual,
                    "maximum": maximum,
                    "fingerprints_equal": policy_ok,
                },
            )

    _check(
        checks,
        f"{prefix}:fresh_rebuild_generation",
        snapshots["fresh_rebuild"]["graph_generation"] != baseline_generation,
    )

    policy_change = corpus["policy_change"]
    policy_before = snapshots["policy_before"]
    policy_after = snapshots["policy_after"]
    policy_baseline = compare_semantic_snapshots(snapshots["baseline"], policy_before)
    policy_comparison = compare_semantic_snapshots(policy_before, policy_after)
    before_policy_fingerprint = _path(
        policy_before,
        "fingerprints",
        "semantic_policy",
    )
    after_policy_fingerprint = _path(
        policy_after,
        "fingerprints",
        "semantic_policy",
    )
    _check(
        checks,
        f"{prefix}:policy_change_recomputed",
        policy_baseline["stable"] is True
        and before_policy_fingerprint == policy_change["before_fingerprint"]
        and after_policy_fingerprint == policy_change["after_fingerprint"]
        and policy_change["before_fingerprint"] != policy_change["after_fingerprint"]
        and policy_comparison["fingerprints"]["all_equal"] is False
        and policy_before["graph_generation"] != policy_after["graph_generation"]
        and policy_change["corpus_id"] == corpus_id
        and policy_change["manifest_sha256"] == corpus["manifest_sha256"]
        and policy_change["before_generation"] == policy_before["graph_generation"]
        and policy_change["after_generation"] == policy_after["graph_generation"]
        and policy_change["incompatible_reuse"]["attempted"] is True
        and policy_change["incompatible_reuse"]["rejected"] is True
        and policy_change["recompute"]["completed"] is True,
    )
    grounding = corpus["byte_grounding"]
    grounding_samples = grounding["samples"]
    _check(
        checks,
        f"{prefix}:byte_grounding",
        grounding["corpus_id"] == corpus_id
        and grounding["manifest_sha256"] == corpus["manifest_sha256"]
        and grounding["graph_generation"] == baseline_generation
        and grounding["semantic_policy_fingerprint"] == threshold_policy_fingerprint
        and bool(grounding_samples)
        and all(sample["verified"] is True for sample in grounding_samples),
        detail=grounding,
    )
    review = corpus["review_coverage"]
    reviewed_primitives = {row["primitive"] for row in review["primitive_samples"]}
    reviewed_predicate_states = {
        row["predicate_state"] for row in review["predicate_state_samples"]
    }
    _check(
        checks,
        f"{prefix}:review_coverage",
        review["corpus_id"] == corpus_id
        and review["manifest_sha256"] == corpus["manifest_sha256"]
        and review["graph_generation"] == baseline_generation
        and review["reviewer"]["kind"] == "human"
        and reviewed_primitives == REQUIRED_PRIMITIVES
        and reviewed_predicate_states == REQUIRED_PREDICATE_STATES,
    )
    ablations = corpus["ablations"]
    ablation_arms = ablations["arms"]
    ablation_keys = [
        (str(row.get("stage") or "").strip(), str(row.get("quality_metric") or "").strip())
        if isinstance(row, Mapping)
        else ("", "")
        for row in ablation_arms
    ]
    _check(
        checks,
        f"{prefix}:model_stage_ablations",
        ablations["corpus_id"] == corpus_id
        and ablations["manifest_sha256"] == corpus["manifest_sha256"]
        and bool(ablation_arms)
        and len(ablation_keys) == len(set(ablation_keys))
        and all(_valid_ablation(row) for row in ablation_arms),
    )
    return {
        "corpus_id": corpus_id,
        "corpus_class": corpus["corpus_class"],
        "manifest_sha256": corpus["manifest_sha256"],
        "metrics": metrics,
        "churn": {
            name: {
                "stable": result["stable"],
                "dimensions": {
                    key: value["churn_share"] for key, value in result["dimensions"].items()
                },
            }
            for name, result in churn_results.items()
        },
    }


def _validate_threshold_policy(value: object) -> dict[str, Any]:
    policy = _require_mapping(value, "threshold_policy")
    _require_exact_fields(
        policy,
        {
            "schema_version",
            "policy_id",
            "semantic_policy_fingerprint",
            "corpus_classes",
        },
        "threshold_policy",
    )
    if policy.get("schema_version") != SEMANTIC_THRESHOLD_POLICY_SCHEMA:
        raise ValueError(
            f"threshold_policy.schema_version must be {SEMANTIC_THRESHOLD_POLICY_SCHEMA}"
        )
    policy_id = _required_text(policy.get("policy_id"), "threshold_policy.policy_id")
    semantic_policy_fingerprint = _required_sha256(
        policy.get("semantic_policy_fingerprint"),
        "threshold_policy.semantic_policy_fingerprint",
    )
    raw_classes = _require_mapping(policy.get("corpus_classes"), "threshold_policy.corpus_classes")
    if set(raw_classes) != set(REQUIRED_CORPUS_CLASSES):
        raise ValueError("threshold_policy must define exactly the four required corpus classes")
    classes: dict[str, Any] = {}
    for corpus_class in REQUIRED_CORPUS_CLASSES:
        row = _require_mapping(raw_classes[corpus_class], f"threshold_policy.{corpus_class}")
        _require_exact_fields(
            row,
            {"quality_minimums", "churn_maximums", "derivation"},
            f"threshold_policy.{corpus_class}",
        )
        quality = _require_mapping(row["quality_minimums"], "quality_minimums")
        if set(quality) != set(QUALITY_METRICS):
            raise ValueError("quality_minimums must define every ADR 0040 quality metric")
        quality_values = {name: _required_unit_float(quality[name], name) for name in quality}
        churn = _require_mapping(row["churn_maximums"], "churn_maximums")
        if set(churn) != set(CHURN_COMPARISONS):
            raise ValueError("churn_maximums must define every ADR 0040 comparison")
        churn_values: dict[str, Any] = {}
        for comparison, raw_limits in churn.items():
            limits = _require_mapping(raw_limits, f"churn_maximums.{comparison}")
            if set(limits) != set(CHURN_DIMENSIONS):
                raise ValueError(f"{comparison} must define all churn dimensions")
            churn_values[comparison] = {
                name: _required_unit_float(limit, name) for name, limit in limits.items()
            }
        derivation = _require_mapping(
            row["derivation"],
            f"threshold_policy.{corpus_class}.derivation",
        )
        _require_exact_fields(
            derivation,
            {
                "status",
                "corpus_id",
                "manifest_sha256",
                "adjudication_sha256",
                "variance_sha256",
                "labeled_sample_count",
                "baseline_run_count",
                "reviewer",
                "reviewed_at",
            },
            f"threshold_policy.{corpus_class}.derivation",
        )
        if derivation.get("status") != "locked":
            raise ValueError(f"threshold_policy.{corpus_class}.derivation.status must be locked")
        reviewer = _required_human_reviewer(
            derivation.get("reviewer"),
            f"threshold_policy.{corpus_class}.derivation.reviewer",
        )
        classes[corpus_class] = {
            "quality_minimums": quality_values,
            "churn_maximums": churn_values,
            "derivation": {
                "status": "locked",
                "corpus_id": _required_text(
                    derivation.get("corpus_id"),
                    f"threshold_policy.{corpus_class}.derivation.corpus_id",
                ),
                "manifest_sha256": _required_sha256(
                    derivation.get("manifest_sha256"),
                    f"threshold_policy.{corpus_class}.derivation.manifest_sha256",
                ),
                "adjudication_sha256": _required_sha256(
                    derivation.get("adjudication_sha256"),
                    f"threshold_policy.{corpus_class}.derivation.adjudication_sha256",
                ),
                "variance_sha256": _required_sha256(
                    derivation.get("variance_sha256"),
                    f"threshold_policy.{corpus_class}.derivation.variance_sha256",
                ),
                "labeled_sample_count": _required_positive_int(
                    derivation.get("labeled_sample_count"),
                    f"threshold_policy.{corpus_class}.derivation.labeled_sample_count",
                ),
                "baseline_run_count": _required_int_at_least(
                    derivation.get("baseline_run_count"),
                    f"threshold_policy.{corpus_class}.derivation.baseline_run_count",
                    minimum=2,
                ),
                "reviewer": reviewer,
                "reviewed_at": _required_timestamp(
                    derivation.get("reviewed_at"),
                    f"threshold_policy.{corpus_class}.derivation.reviewed_at",
                ),
            },
        }
    return {
        "policy_id": policy_id,
        "semantic_policy_fingerprint": semantic_policy_fingerprint,
        "corpus_classes": classes,
    }


def _validate_corpus(value: object, *, index: int) -> dict[str, Any]:
    name = f"corpora[{index}]"
    row = _require_mapping(value, name)
    _require_exact_fields(
        row,
        {
            "corpus_id",
            "corpus_class",
            "manifest_sha256",
            "stored_report",
            "adjudicated_report",
            "snapshots",
            "policy_change",
            "byte_grounding",
            "review_coverage",
            "ablations",
        },
        name,
    )
    corpus_id = _required_text(row["corpus_id"], f"{name}.corpus_id")
    corpus_class = _required_text(row["corpus_class"], f"{name}.corpus_class")
    manifest_sha256 = _required_sha256(row["manifest_sha256"], f"{name}.manifest_sha256")
    if corpus_class not in REQUIRED_CORPUS_CLASSES:
        raise ValueError(f"{name}.corpus_class is not an ADR 0040 corpus class")
    stored = _extract_quality_report(row["stored_report"], name=f"{name}.stored_report")
    adjudicated = _extract_quality_report(
        row["adjudicated_report"],
        name=f"{name}.adjudicated_report",
    )
    snapshots = _require_mapping(row["snapshots"], f"{name}.snapshots")
    if set(snapshots) != set(REQUIRED_SNAPSHOT_ARMS):
        raise ValueError(f"{name}.snapshots must define every ADR 0040 scenario arm")
    normalized_snapshots = {
        snapshot_name: _extract_semantic_snapshot(
            snapshot,
            name=f"{name}.snapshots.{snapshot_name}",
        )
        for snapshot_name, snapshot in snapshots.items()
    }
    policy_change = _validate_policy_change(
        row["policy_change"],
        name=f"{name}.policy_change",
    )
    grounding = _validate_byte_grounding(
        row["byte_grounding"],
        name=f"{name}.byte_grounding",
    )
    review = _validate_review_coverage(
        row["review_coverage"],
        name=f"{name}.review_coverage",
    )
    ablations = _validate_ablations(
        row["ablations"],
        name=f"{name}.ablations",
        require_valid=False,
    )
    return {
        "corpus_id": corpus_id,
        "corpus_class": corpus_class,
        "manifest_sha256": manifest_sha256,
        "stored_report": stored,
        "adjudicated_report": adjudicated,
        "snapshots": normalized_snapshots,
        "policy_change": policy_change,
        "byte_grounding": grounding,
        "review_coverage": review,
        "ablations": ablations,
    }


def _validate_policy_change(value: object, *, name: str) -> dict[str, Any]:
    policy_change = _require_mapping(value, name)
    _require_exact_fields(
        policy_change,
        {
            "schema_version",
            "corpus_id",
            "manifest_sha256",
            "before_fingerprint",
            "after_fingerprint",
            "before_generation",
            "after_generation",
            "before_run_id",
            "after_run_id",
            "changed_fields",
            "incompatible_reuse",
            "recompute",
        },
        name,
    )
    if policy_change.get("schema_version") != POLICY_CHANGE_EVIDENCE_SCHEMA:
        raise ValueError(f"{name}.schema_version must be {POLICY_CHANGE_EVIDENCE_SCHEMA}")
    for key in ("before_fingerprint", "after_fingerprint"):
        _required_sha256(policy_change[key], f"{name}.{key}")
    changed_fields = _required_unique_texts(
        policy_change.get("changed_fields"),
        f"{name}.changed_fields",
    )
    incompatible = _require_mapping(
        policy_change.get("incompatible_reuse"),
        f"{name}.incompatible_reuse",
    )
    _require_exact_fields(
        incompatible,
        {"attempted", "rejected", "evidence_sha256"},
        f"{name}.incompatible_reuse",
    )
    recompute = _require_mapping(policy_change.get("recompute"), f"{name}.recompute")
    _require_exact_fields(
        recompute,
        {"completed", "evidence_sha256"},
        f"{name}.recompute",
    )
    for parent_name, row, fields in (
        ("incompatible_reuse", incompatible, ("attempted", "rejected")),
        ("recompute", recompute, ("completed",)),
    ):
        for field in fields:
            if not isinstance(row.get(field), bool):
                raise ValueError(f"{name}.{parent_name}.{field} must be boolean")
        _required_sha256(row.get("evidence_sha256"), f"{name}.{parent_name}.evidence_sha256")
    return {
        "schema_version": POLICY_CHANGE_EVIDENCE_SCHEMA,
        "corpus_id": _required_text(policy_change.get("corpus_id"), f"{name}.corpus_id"),
        "manifest_sha256": _required_sha256(
            policy_change.get("manifest_sha256"), f"{name}.manifest_sha256"
        ),
        "before_fingerprint": policy_change["before_fingerprint"],
        "after_fingerprint": policy_change["after_fingerprint"],
        "before_generation": _required_text(
            policy_change.get("before_generation"), f"{name}.before_generation"
        ),
        "after_generation": _required_text(
            policy_change.get("after_generation"), f"{name}.after_generation"
        ),
        "before_run_id": _required_text(
            policy_change.get("before_run_id"), f"{name}.before_run_id"
        ),
        "after_run_id": _required_text(policy_change.get("after_run_id"), f"{name}.after_run_id"),
        "changed_fields": changed_fields,
        "incompatible_reuse": dict(incompatible),
        "recompute": dict(recompute),
    }


def _validate_byte_grounding(value: object, *, name: str) -> dict[str, Any]:
    grounding = _require_mapping(value, name)
    _require_exact_fields(
        grounding,
        {
            "schema_version",
            "corpus_id",
            "manifest_sha256",
            "graph_generation",
            "semantic_policy_fingerprint",
            "samples",
        },
        name,
    )
    if grounding.get("schema_version") != BYTE_GROUNDING_EVIDENCE_SCHEMA:
        raise ValueError(f"{name}.schema_version must be {BYTE_GROUNDING_EVIDENCE_SCHEMA}")
    raw_samples = grounding.get("samples")
    if not isinstance(raw_samples, list) or not raw_samples:
        raise ValueError(f"{name}.samples must be a non-empty list")
    samples: list[dict[str, Any]] = []
    sample_ids: set[str] = set()
    for index, raw in enumerate(raw_samples):
        sample_name = f"{name}.samples[{index}]"
        sample = _require_mapping(raw, sample_name)
        _require_exact_fields(
            sample,
            {
                "sample_id",
                "relation_claim_id",
                "source_id",
                "source_sha256",
                "start_byte",
                "end_byte",
                "excerpt_sha256",
                "verified",
            },
            sample_name,
        )
        sample_id = _required_text(sample.get("sample_id"), f"{sample_name}.sample_id")
        if sample_id in sample_ids:
            raise ValueError(f"{name}.samples contains duplicate sample_id {sample_id!r}")
        sample_ids.add(sample_id)
        start = _required_nonnegative_int(sample.get("start_byte"), f"{sample_name}.start_byte")
        end = _required_nonnegative_int(sample.get("end_byte"), f"{sample_name}.end_byte")
        if end <= start:
            raise ValueError(f"{sample_name}.end_byte must be greater than start_byte")
        if not isinstance(sample.get("verified"), bool):
            raise ValueError(f"{sample_name}.verified must be boolean")
        samples.append(
            {
                "sample_id": sample_id,
                "relation_claim_id": _required_text(
                    sample.get("relation_claim_id"), f"{sample_name}.relation_claim_id"
                ),
                "source_id": _required_text(sample.get("source_id"), f"{sample_name}.source_id"),
                "source_sha256": _required_sha256(
                    sample.get("source_sha256"), f"{sample_name}.source_sha256"
                ),
                "start_byte": start,
                "end_byte": end,
                "excerpt_sha256": _required_sha256(
                    sample.get("excerpt_sha256"), f"{sample_name}.excerpt_sha256"
                ),
                "verified": sample["verified"],
            }
        )
    return {
        "schema_version": BYTE_GROUNDING_EVIDENCE_SCHEMA,
        "corpus_id": _required_text(grounding.get("corpus_id"), f"{name}.corpus_id"),
        "manifest_sha256": _required_sha256(
            grounding.get("manifest_sha256"), f"{name}.manifest_sha256"
        ),
        "graph_generation": _required_text(
            grounding.get("graph_generation"), f"{name}.graph_generation"
        ),
        "semantic_policy_fingerprint": _required_sha256(
            grounding.get("semantic_policy_fingerprint"),
            f"{name}.semantic_policy_fingerprint",
        ),
        "samples": samples,
    }


def _validate_review_coverage(value: object, *, name: str) -> dict[str, Any]:
    review = _require_mapping(value, name)
    _require_exact_fields(
        review,
        {
            "schema_version",
            "corpus_id",
            "manifest_sha256",
            "graph_generation",
            "adjudication_sha256",
            "reviewer",
            "reviewed_at",
            "primitive_samples",
            "predicate_state_samples",
        },
        name,
    )
    if review.get("schema_version") != REVIEW_COVERAGE_EVIDENCE_SCHEMA:
        raise ValueError(f"{name}.schema_version must be {REVIEW_COVERAGE_EVIDENCE_SCHEMA}")
    reviewer = _required_human_reviewer(review.get("reviewer"), f"{name}.reviewer")
    sample_ids: set[str] = set()

    def samples(field: str, category: str) -> list[dict[str, str]]:
        raw_rows = review.get(field)
        if not isinstance(raw_rows, list) or not raw_rows:
            raise ValueError(f"{name}.{field} must be a non-empty list")
        rows: list[dict[str, str]] = []
        for index, raw in enumerate(raw_rows):
            row_name = f"{name}.{field}[{index}]"
            row = _require_mapping(raw, row_name)
            _require_exact_fields(row, {"sample_id", category, "decision_sha256"}, row_name)
            sample_id = _required_text(row.get("sample_id"), f"{row_name}.sample_id")
            if sample_id in sample_ids:
                raise ValueError(f"{name} contains duplicate sample_id {sample_id!r}")
            sample_ids.add(sample_id)
            rows.append(
                {
                    "sample_id": sample_id,
                    category: _required_text(row.get(category), f"{row_name}.{category}"),
                    "decision_sha256": _required_sha256(
                        row.get("decision_sha256"), f"{row_name}.decision_sha256"
                    ),
                }
            )
        return rows

    return {
        "schema_version": REVIEW_COVERAGE_EVIDENCE_SCHEMA,
        "corpus_id": _required_text(review.get("corpus_id"), f"{name}.corpus_id"),
        "manifest_sha256": _required_sha256(
            review.get("manifest_sha256"), f"{name}.manifest_sha256"
        ),
        "graph_generation": _required_text(
            review.get("graph_generation"), f"{name}.graph_generation"
        ),
        "adjudication_sha256": _required_sha256(
            review.get("adjudication_sha256"), f"{name}.adjudication_sha256"
        ),
        "reviewer": reviewer,
        "reviewed_at": _required_timestamp(review.get("reviewed_at"), f"{name}.reviewed_at"),
        "primitive_samples": samples("primitive_samples", "primitive"),
        "predicate_state_samples": samples("predicate_state_samples", "predicate_state"),
    }


def _validate_ablations(
    value: object,
    *,
    name: str,
    require_valid: bool,
) -> dict[str, Any]:
    row = _require_mapping(value, name)
    _require_exact_fields(
        row,
        {"schema_version", "corpus_id", "manifest_sha256", "arms"},
        name,
    )
    if row.get("schema_version") != MODEL_STAGE_ABLATIONS_SCHEMA:
        raise ValueError(f"{name}.schema_version must be {MODEL_STAGE_ABLATIONS_SCHEMA}")
    arms = row.get("arms")
    if not isinstance(arms, list):
        raise ValueError(f"{name}.arms must be a list")
    if require_valid and any(not _valid_ablation(arm) for arm in arms):
        raise ValueError(f"{name} contains a malformed model-stage ablation")
    return {
        "schema_version": MODEL_STAGE_ABLATIONS_SCHEMA,
        "corpus_id": _required_text(row.get("corpus_id"), f"{name}.corpus_id"),
        "manifest_sha256": _required_sha256(row.get("manifest_sha256"), f"{name}.manifest_sha256"),
        "arms": list(arms),
    }


def _validate_public_diagnostic(value: object) -> dict[str, Any]:
    row = _require_mapping(value, "public_diagnostic")
    _require_exact_fields(
        row,
        {"status", "manifest_sha256", "marginalia_retrieval", "direct_rag", "flat_bm25"},
        "public_diagnostic",
    )
    if row["status"] not in {"measured", "not_measured"}:
        raise ValueError("public_diagnostic.status must be measured or not_measured")
    _required_sha256(row["manifest_sha256"], "public_diagnostic.manifest_sha256")
    result = dict(row)
    for name in ("marginalia_retrieval", "direct_rag", "flat_bm25"):
        arm = _require_mapping(row[name], f"public_diagnostic.{name}")
        _require_exact_fields(
            arm,
            {"status", "artifact_sha256", "case_count"},
            f"public_diagnostic.{name}",
        )
        if arm.get("status") not in {"measured", "not_measured"}:
            raise ValueError(f"public_diagnostic.{name}.status is invalid")
        case_count = arm.get("case_count")
        if not isinstance(case_count, int) or isinstance(case_count, bool) or case_count < 0:
            raise ValueError(f"public_diagnostic.{name}.case_count must be an integer >= 0")
        artifact = arm.get("artifact_sha256")
        if arm["status"] == "measured":
            _required_sha256(artifact, f"public_diagnostic.{name}.artifact_sha256")
            if case_count == 0:
                raise ValueError(f"public_diagnostic.{name}.case_count must be > 0 when measured")
        elif artifact is not None or case_count != 0:
            raise ValueError(f"public_diagnostic.{name} must not claim evidence when not measured")
        result[name] = dict(arm)
    return result


def _validate_temporal_correction(value: object) -> dict[str, Any]:
    row = _require_mapping(value, "temporal_correction")
    expected = {
        "schema_version",
        "status",
        "corpus_id",
        "artifact_sha256",
        "source_time_preserved",
        "correction_order_preserved",
        "latest_correction_retrievable",
        "first_class_valid_time_claimed",
    }
    _require_exact_fields(row, expected, "temporal_correction")
    if row.get("schema_version") != TEMPORAL_CORRECTION_SCHEMA:
        raise ValueError(f"temporal_correction.schema_version must be {TEMPORAL_CORRECTION_SCHEMA}")
    status = row.get("status")
    if status not in {"measured", "not_measured"}:
        raise ValueError("temporal_correction.status must be measured or not_measured")
    corpus_id = _required_text(row.get("corpus_id"), "temporal_correction.corpus_id")
    evidence_fields = (
        "source_time_preserved",
        "correction_order_preserved",
        "latest_correction_retrievable",
        "first_class_valid_time_claimed",
    )
    if status == "measured":
        artifact_sha256: str | None = _required_sha256(
            row.get("artifact_sha256"),
            "temporal_correction.artifact_sha256",
        )
        if any(not isinstance(row.get(name), bool) for name in evidence_fields):
            raise ValueError("measured temporal_correction evidence fields must be boolean")
    else:
        artifact_sha256 = None
        if row.get("artifact_sha256") is not None or any(
            row.get(name) is not None for name in evidence_fields
        ):
            raise ValueError("not_measured temporal_correction must not claim evidence")
    return {
        "schema_version": TEMPORAL_CORRECTION_SCHEMA,
        "status": status,
        "corpus_id": corpus_id,
        "artifact_sha256": artifact_sha256,
        **{name: row.get(name) for name in evidence_fields},
    }


def _quality_report(value: object, name: str) -> dict[str, Any]:
    row = _require_mapping(value, name)
    if row.get("schema_version") != "semantic_quality.v1":
        raise ValueError(f"{name}.schema_version must be semantic_quality.v1")
    return dict(row)


def _valid_ablation(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "stage",
        "quality_metric",
        "changed_fields",
        "disabled",
        "enabled",
    }:
        return False
    metric = value["quality_metric"]
    changed_fields = value["changed_fields"]
    disabled = value["disabled"]
    enabled = value["enabled"]
    if not isinstance(disabled, Mapping) or not isinstance(enabled, Mapping):
        return False
    measurement_fields = {
        "config_sha256",
        "semantic_policy_fingerprint",
        "graph_generation",
        "evidence_sha256",
        "quality_value",
        "ingest_elapsed_ms",
        "construction_cost",
    }
    if set(disabled) != measurement_fields or set(enabled) != measurement_fields:
        return False
    cost_fields = {
        "completion_calls",
        "input_tokens",
        "output_tokens",
        "embedding_calls",
        "embedding_inputs",
    }

    def valid_measurement(row: Mapping[str, Any]) -> bool:
        cost = row.get("construction_cost")
        return (
            _is_sha256(row.get("config_sha256"))
            and _is_sha256(row.get("semantic_policy_fingerprint"))
            and isinstance(row.get("graph_generation"), str)
            and bool(str(row.get("graph_generation") or "").strip())
            and _is_sha256(row.get("evidence_sha256"))
            and _unit_float(row.get("quality_value")) is not None
            and _nonnegative_float(row.get("ingest_elapsed_ms")) is not None
            and isinstance(cost, Mapping)
            and set(cost) == cost_fields
            and all(_nonnegative_int(cost.get(field)) is not None for field in cost_fields)
        )

    return (
        isinstance(value["stage"], str)
        and bool(value["stage"].strip())
        and isinstance(metric, str)
        and metric in QUALITY_METRICS
        and isinstance(changed_fields, list)
        and bool(changed_fields)
        and all(isinstance(field, str) and bool(field.strip()) for field in changed_fields)
        and len(changed_fields) == len(set(changed_fields))
        and valid_measurement(disabled)
        and valid_measurement(enabled)
        and disabled["config_sha256"] != enabled["config_sha256"]
        and disabled["semantic_policy_fingerprint"] != enabled["semantic_policy_fingerprint"]
        and disabled["evidence_sha256"] != enabled["evidence_sha256"]
        and float(enabled["quality_value"]) > float(disabled["quality_value"])
    )


def _check(
    checks: list[dict[str, Any]],
    code: str,
    ok: bool,
    *,
    detail: object | None = None,
) -> None:
    row: dict[str, Any] = {"code": code, "status": "passed" if ok else "failed"}
    if detail is not None:
        row["detail"] = detail
    checks.append(row)


def _path(value: object, *parts: str) -> object:
    current = value
    for part in parts:
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current


def _unit_float(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    result = float(value)
    return result if 0.0 <= result <= 1.0 else None


def _nonnegative_float(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    result = float(value)
    return result if result >= 0.0 else None


def _nonnegative_int(value: object) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _required_nonnegative_int(value: object, name: str) -> int:
    result = _nonnegative_int(value)
    if result is None:
        raise ValueError(f"{name} must be an integer >= 0")
    return result


def _required_positive_int(value: object, name: str) -> int:
    return _required_int_at_least(value, name, minimum=1)


def _required_int_at_least(value: object, name: str, *, minimum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _required_unit_float(value: object, name: str) -> float:
    result = _unit_float(value)
    if result is None:
        raise ValueError(f"{name} must be a number between 0 and 1")
    return result


def _required_sha256(value: object, name: str) -> str:
    text = _required_text(value, name)
    if not _is_sha256(text):
        raise ValueError(f"{name} must be a sha256 identifier")
    return text


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )


def _required_unique_texts(value: object, name: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValueError(f"{name} must contain non-empty text values")
    normalized = [item.strip() for item in value]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{name} must contain unique values")
    return normalized


def _required_human_reviewer(value: object, name: str) -> dict[str, str]:
    reviewer = _require_mapping(value, name)
    _require_exact_fields(reviewer, {"kind", "id"}, name)
    if reviewer.get("kind") != "human":
        raise ValueError(f"{name}.kind must be human")
    return {"kind": "human", "id": _required_text(reviewer.get("id"), f"{name}.id")}


def _required_timestamp(value: object, name: str) -> str:
    text = _required_text(value, name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return text


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _require_mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return value


def _require_exact_fields(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if set(value) != expected:
        missing = sorted(expected - set(value))
        unknown = sorted(set(value) - expected)
        raise ValueError(f"{name} fields invalid: missing={missing}, unknown={unknown}")


__all__ = [
    "BYTE_GROUNDING_EVIDENCE_SCHEMA",
    "CHURN_COMPARISONS",
    "MODEL_STAGE_ABLATIONS_SCHEMA",
    "POLICY_SNAPSHOT_ARMS",
    "POLICY_CHANGE_EVIDENCE_SCHEMA",
    "QUALITY_METRICS",
    "REQUIRED_CORPUS_CLASSES",
    "REQUIRED_SNAPSHOT_ARMS",
    "REVIEW_COVERAGE_EVIDENCE_SCHEMA",
    "SEMANTIC_ACCEPTANCE_COLLECTION_SCHEMA",
    "SEMANTIC_ACCEPTANCE_COLLECTION_STATUS_SCHEMA",
    "SEMANTIC_ACCEPTANCE_RESULT_SCHEMA",
    "SEMANTIC_ACCEPTANCE_SCHEMA",
    "SEMANTIC_THRESHOLD_POLICY_SCHEMA",
    "TEMPORAL_CORRECTION_SCHEMA",
    "evaluate_semantic_acceptance",
    "inspect_semantic_acceptance_collection",
    "materialize_semantic_acceptance_collection",
]
