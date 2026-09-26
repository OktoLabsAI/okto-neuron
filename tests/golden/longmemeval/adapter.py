#!/usr/bin/env python3
"""Convert pinned LongMemEval records into isolated Okto Neuron Golden datasets.

The adapter is intentionally offline. It never downloads the benchmark and it never runs a
model. Callers must provide the exact source file named and hashed by the frozen manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

import yaml


SCHEMA_VERSION = "longmemeval-marginalia-diagnostic.v1"
ADAPTER_VERSION = "longmemeval-golden-adapter.v3"
SELECTION_ALGORITHM = "stratified_sha256.v1"
QUESTION_TYPES = frozenset(
    {
        "single-session-user",
        "single-session-assistant",
        "single-session-preference",
        "temporal-reasoning",
        "knowledge-update",
        "multi-session",
    }
)
BUCKETS = QUESTION_TYPES | {"abstention"}
TIER_BY_BUCKET = {
    "single-session-user": "T1",
    "single-session-assistant": "T1",
    "single-session-preference": "T2",
    "temporal-reasoning": "T3",
    "knowledge-update": "T3",
    "multi-session": "T3",
    "abstention": "T4",
}
REPO_ROOT = Path(__file__).resolve().parents[3]


class AdapterError(ValueError):
    """Raised when source or manifest evidence violates the frozen contract."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _required_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AdapterError(f"{name} must be an object")
    return value


def _required_string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdapterError(f"{name} must be a non-empty string")
    return value


def _required_string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise AdapterError(f"{name} must be a list of non-empty strings")
    return value


def _normalized_answer(value: Any, name: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise AdapterError(f"{name} must be a string or finite JSON number")
    if isinstance(value, float) and not math.isfinite(value):
        raise AdapterError(f"{name} must be a string or finite JSON number")
    normalized = str(value)
    if not normalized.strip():
        raise AdapterError(f"{name} must not be empty")
    return normalized


def _parse_manifest(manifest_bytes: bytes, source: str) -> dict[str, Any]:
    try:
        value = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"could not parse manifest {source}: {exc}") from exc
    validate_manifest(value)
    return value


def load_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest_bytes = path.read_bytes()
    except OSError as exc:
        raise AdapterError(f"could not read manifest {path}: {exc}") from exc
    return _parse_manifest(manifest_bytes, str(path))


def validate_manifest(manifest: Any) -> None:
    root = _required_mapping(manifest, "manifest")
    if root.get("schema_version") != SCHEMA_VERSION:
        raise AdapterError(f"unsupported schema_version: {root.get('schema_version')!r}")
    if root.get("adapter_version") != ADAPTER_VERSION:
        raise AdapterError(f"unsupported adapter_version: {root.get('adapter_version')!r}")

    benchmark = _required_mapping(root.get("benchmark"), "benchmark")
    if benchmark.get("name") != "LongMemEval":
        raise AdapterError("benchmark.name must be LongMemEval")
    _required_string(benchmark.get("paper_url"), "benchmark.paper_url")
    _required_string(benchmark.get("conference_paper_url"), "benchmark.conference_paper_url")
    code = _required_mapping(benchmark.get("code"), "benchmark.code")
    _required_string(code.get("url"), "benchmark.code.url")
    if not re.fullmatch(r"[0-9a-f]{40}", str(code.get("revision"))):
        raise AdapterError("benchmark.code.revision must be a 40-character Git commit hash")

    status = _required_mapping(root.get("status"), "status")
    if status.get("gating") is not False:
        raise AdapterError("status.gating must remain false")
    if status.get("purpose") != "public_procurement_diagnostic":
        raise AdapterError("status.purpose must be public_procurement_diagnostic")
    if status.get("score_claims_allowed") is not False:
        raise AdapterError("status.score_claims_allowed must remain false")

    dataset = _required_mapping(root.get("dataset"), "dataset")
    for field in (
        "repository",
        "url",
        "revision",
        "file",
        "file_sha256",
        "split",
        "license",
        "language",
    ):
        _required_string(dataset.get(field), f"dataset.{field}")
    if dataset["license"] != "MIT":
        raise AdapterError("dataset.license must preserve the pinned MIT release")
    if not re.fullmatch(r"[0-9a-f]{40}", str(dataset["revision"])):
        raise AdapterError("dataset.revision must be a 40-character lowercase Git commit hash")
    if not re.fullmatch(r"[0-9a-f]{64}", str(dataset["file_sha256"])):
        raise AdapterError("dataset.file_sha256 must be a lowercase SHA-256")
    for field in ("file_size_bytes", "expected_instances"):
        if not isinstance(dataset.get(field), int) or dataset[field] <= 0:
            raise AdapterError(f"dataset.{field} must be a positive integer")
    required_fields = _required_string_list(
        dataset.get("required_fields"), "dataset.required_fields"
    )
    expected_fields = {
        "question_id",
        "question_type",
        "question",
        "answer",
        "question_date",
        "haystack_session_ids",
        "haystack_dates",
        "haystack_sessions",
        "answer_session_ids",
    }
    if set(required_fields) != expected_fields:
        raise AdapterError("dataset.required_fields does not match the adapter schema")

    selection = _required_mapping(root.get("selection"), "selection")
    if selection.get("algorithm") != SELECTION_ALGORITHM:
        raise AdapterError(f"selection.algorithm must be {SELECTION_ALGORITHM}")
    _required_string(selection.get("seed"), "selection.seed")
    if not isinstance(selection.get("per_bucket"), int) or selection["per_bucket"] <= 0:
        raise AdapterError("selection.per_bucket must be a positive integer")
    buckets = _required_string_list(selection.get("required_buckets"), "selection.required_buckets")
    if len(buckets) != len(set(buckets)) or set(buckets) != BUCKETS:
        raise AdapterError("selection.required_buckets must contain each supported bucket once")
    if selection.get("materialization") != "derive_from_pinned_revision_at_conversion":
        raise AdapterError("selection.materialization must derive from the pinned source")

    mapping = _required_mapping(root.get("mapping"), "mapping")
    if mapping.get("isolation") != "one_question_per_golden_dataset_and_vault":
        raise AdapterError("mapping.isolation must preserve one question per vault")
    if mapping.get("ingestion_unit") != "timestamped_session_markdown":
        raise AdapterError("mapping.ingestion_unit must preserve timestamped sessions")
    if mapping.get("ingested_session_identity") != "opaque_ordinal_only":
        raise AdapterError("mapping.ingested_session_identity must prevent source-id leakage")
    if mapping.get("ingested_turn_roles") != ["user", "assistant"]:
        raise AdapterError("mapping.ingested_turn_roles must contain user and assistant")
    if mapping.get("label_fields_ingested") is not False:
        raise AdapterError("mapping.label_fields_ingested must remain false")
    if mapping.get("query_field") != "question" or mapping.get("expected_answer_field") != "answer":
        raise AdapterError("mapping query/answer fields do not match LongMemEval")
    if mapping.get("query_rendering") != "current_date_then_question.v1":
        raise AdapterError("mapping.query_rendering must include the benchmark current date")
    if mapping.get("session_reference_field") != "answer_session_ids":
        raise AdapterError("mapping.session_reference_field must be answer_session_ids")
    if mapping.get("turn_reference_field") != "haystack_sessions[].has_answer":
        raise AdapterError("mapping.turn_reference_field must identify has_answer labels")
    if mapping.get("abstention_rule") != "question_id_suffix__abs":
        raise AdapterError("mapping.abstention_rule must preserve the upstream suffix rule")
    if mapping.get("temporal_reasoning_evaluation") != "retrieval_only_valid_time_qa_unsupported":
        raise AdapterError(
            "mapping.temporal_reasoning_evaluation must preserve the valid-time deferral"
        )

    runtime = _required_mapping(root.get("runtime_placeholders"), "runtime_placeholders")
    for field in (
        "marginalia_revision",
        "llm_provider",
        "llm_model",
        "embedder_provider",
        "embedder_model",
        "judge_provider",
        "judge_model",
    ):
        if runtime.get(field) != "UNSET":
            raise AdapterError(f"runtime_placeholders.{field} must remain UNSET")

    baselines = _required_mapping(root.get("baselines"), "baselines")
    upstream = _required_mapping(
        baselines.get("upstream_plain_retrieval_reference"),
        "baselines.upstream_plain_retrieval_reference",
    )
    if (
        upstream.get("implementation") != "flat-bm25"
        or upstream.get("granularity") != "session"
        or upstream.get("indexed_roles") != ["user"]
    ):
        raise AdapterError("upstream baseline must preserve the pinned flat-BM25 reference")
    recall = _required_mapping(
        baselines.get("marginalia_plain_retrieval"), "baselines.marginalia_plain_retrieval"
    )
    if recall.get("surface") != "/api/v1/recall" or any(
        recall.get(field) is not False
        for field in ("answer_generation", "query_expansion", "reranking", "subgraph_expansion")
    ):
        raise AdapterError("Okto Neuron plain retrieval baseline must remain completion-free")
    rag = _required_mapping(baselines.get("marginalia_plain_rag"), "baselines.marginalia_plain_rag")
    if rag.get("surface") != "/api/v1/ask" or rag.get("reading_strategy") != "direct":
        raise AdapterError("Okto Neuron plain RAG baseline must remain the direct /api/v1/ask arm")

    reporting = _required_mapping(root.get("reporting"), "reporting")
    expected_reporting_lists = {
        "implemented_marginalia_retrieval_metrics": [
            "gold_span_retrieved_at_k",
            "gold_span_intact",
            "mean_byte_iou",
            "mean_token_recall",
        ],
        "upstream_session_reference_metrics": [
            "recall_any_at_k",
            "recall_all_at_k",
            "ndcg_at_k",
        ],
        "pending_comparability_work": [
            "pinned_upstream_baseline_run",
        ],
        "latency_metrics": [
            "retrieval_p50_ms",
            "retrieval_p95_ms",
            "qa_p50_ms",
            "qa_p95_ms",
        ],
        "construction_cost_metrics": [
            "embedding_calls",
            "completion_calls",
            "input_tokens",
            "output_tokens",
        ],
        "required_disclosures": [
            "selected_question_ids_sha256",
            "source_file_sha256",
            "marginalia_revision",
            "provider_and_model_ids",
            "prompt_or_policy_fingerprint",
            "failures_and_exclusions",
        ],
    }
    for field, expected in expected_reporting_lists.items():
        if reporting.get(field) != expected:
            raise AdapterError(f"reporting.{field} does not match the frozen contract")
    if reporting.get("qa_metric") != "reference_guided_judge_accuracy":
        raise AdapterError("reporting.qa_metric does not match the frozen contract")
    privacy = _required_mapping(root.get("privacy_and_license"), "privacy_and_license")
    if privacy.get("derived_content_commit_allowed") is not False:
        raise AdapterError("derived benchmark content must not be commit-able")
    _required_string(privacy.get("result_claim"), "privacy_and_license.result_claim")


def _parse_source(
    source_bytes: bytes,
    manifest: dict[str, Any],
    *,
    verify_pin: bool,
) -> list[dict[str, Any]]:
    dataset = manifest["dataset"]
    if verify_pin and len(source_bytes) != dataset["file_size_bytes"]:
        raise AdapterError(
            f"source size mismatch: expected {dataset['file_size_bytes']}, got {len(source_bytes)}"
        )
    digest = _sha256(source_bytes)
    if verify_pin and digest != dataset["file_sha256"]:
        raise AdapterError(
            f"source SHA-256 mismatch: expected {dataset['file_sha256']}, got {digest}"
        )
    try:
        value = json.loads(source_bytes)
    except json.JSONDecodeError as exc:
        raise AdapterError(f"source is not valid JSON: {exc}") from exc
    if not isinstance(value, list) or any(not isinstance(record, dict) for record in value):
        raise AdapterError("source must be a JSON array of objects")
    if verify_pin and len(value) != dataset["expected_instances"]:
        raise AdapterError(
            f"source instance mismatch: expected {dataset['expected_instances']}, got {len(value)}"
        )
    validate_record_index(value)
    return value


def load_source(
    path: Path, manifest: dict[str, Any], *, verify_pin: bool = True
) -> list[dict[str, Any]]:
    try:
        source_bytes = path.read_bytes()
    except OSError as exc:
        raise AdapterError(f"could not read source {path}: {exc}") from exc
    return _parse_source(source_bytes, manifest, verify_pin=verify_pin)


def _record_bucket(record: dict[str, Any]) -> str:
    if record["question_id"].endswith("_abs"):
        return "abstention"
    return record["question_type"]


def validate_record_index(records: list[dict[str, Any]]) -> None:
    """Validate the complete population needed for deterministic stratification."""

    seen_ids: set[str] = set()
    for index, record in enumerate(records):
        prefix = f"record[{index}]"
        question_id = _required_string(record.get("question_id"), f"{prefix}.question_id")
        if question_id in seen_ids:
            raise AdapterError(f"duplicate question_id: {question_id}")
        seen_ids.add(question_id)
        question_type = _required_string(record.get("question_type"), f"{prefix}.question_type")
        if question_type not in QUESTION_TYPES:
            raise AdapterError(f"{prefix}.question_type is unsupported: {question_type}")


def validate_records(records: list[dict[str, Any]], manifest: dict[str, Any]) -> None:
    required = set(manifest["dataset"]["required_fields"])
    validate_record_index(records)
    for index, record in enumerate(records):
        prefix = f"record[{index}]"
        missing = sorted(required - record.keys())
        if missing:
            raise AdapterError(f"{prefix} missing required fields: {', '.join(missing)}")
        for field in ("question", "question_date"):
            _required_string(record[field], f"{prefix}.{field}")
        _normalized_answer(record["answer"], f"{prefix}.answer")

        session_ids = _required_string_list(
            record["haystack_session_ids"], f"{prefix}.haystack_session_ids"
        )
        session_dates = _required_string_list(record["haystack_dates"], f"{prefix}.haystack_dates")
        sessions = record["haystack_sessions"]
        if not isinstance(sessions, list):
            raise AdapterError(f"{prefix}.haystack_sessions must be a list")
        if not (len(session_ids) == len(session_dates) == len(sessions)):
            raise AdapterError(f"{prefix} haystack session ids, dates, and sessions must align")
        if not sessions:
            raise AdapterError(f"{prefix}.haystack_sessions must not be empty")

        labeled_session_ids: set[str] = set()
        for session_index, turns in enumerate(sessions):
            if not isinstance(turns, list) or not turns:
                raise AdapterError(
                    f"{prefix}.haystack_sessions[{session_index}] must contain turns"
                )
            for turn_index, turn in enumerate(turns):
                turn_name = f"{prefix}.haystack_sessions[{session_index}][{turn_index}]"
                if not isinstance(turn, dict):
                    raise AdapterError(f"{turn_name} must be an object")
                if turn.get("role") not in {"user", "assistant"}:
                    raise AdapterError(f"{turn_name}.role must be user or assistant")
                if not isinstance(turn.get("content"), str):
                    raise AdapterError(f"{turn_name}.content must be a string")
                if "has_answer" in turn and not isinstance(turn["has_answer"], bool):
                    raise AdapterError(f"{turn_name}.has_answer must be boolean when present")
                if turn.get("has_answer") is True:
                    labeled_session_ids.add(session_ids[session_index])

        answer_ids = _required_string_list(
            record["answer_session_ids"], f"{prefix}.answer_session_ids"
        )
        if len(answer_ids) != len(set(answer_ids)):
            raise AdapterError(f"{prefix}.answer_session_ids contains duplicates")
        unknown_answer_ids = sorted(set(answer_ids) - set(session_ids))
        if unknown_answer_ids:
            raise AdapterError(
                f"{prefix}.answer_session_ids reference unknown sessions: "
                + ", ".join(unknown_answer_ids)
            )
        if _record_bucket(record) != "abstention" and not answer_ids:
            raise AdapterError(
                f"{prefix} answerable case must identify at least one answer session"
            )
        if not labeled_session_ids.issubset(set(answer_ids)):
            raise AdapterError(
                f"{prefix} has_answer turns must occur only inside answer_session_ids"
            )
        if _record_bucket(record) != "abstention" and not labeled_session_ids:
            raise AdapterError(
                f"{prefix} answerable case must contain at least one has_answer turn"
            )


def select_records(records: list[dict[str, Any]], manifest: dict[str, Any]) -> list[dict[str, Any]]:
    selection = manifest["selection"]
    seed = selection["seed"]
    per_bucket = selection["per_bucket"]
    selected: list[dict[str, Any]] = []
    for bucket in selection["required_buckets"]:
        candidates = [record for record in records if _record_bucket(record) == bucket]
        if len(candidates) < per_bucket:
            raise AdapterError(
                f"bucket {bucket!r} has {len(candidates)} records; {per_bucket} required"
            )
        ranked = sorted(
            candidates,
            key=lambda record: (
                _sha256(f"{seed}\0{bucket}\0{record['question_id']}".encode()),
                record["question_id"],
            ),
        )
        selected.extend(ranked[:per_bucket])
    return selected


def _slug(value: str, *, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-.").lower()
    return (slug or fallback)[:80]


def _case_id(question_id: str) -> str:
    return f"{_slug(question_id, fallback='question')}--{_sha256(question_id.encode())[:12]}"


def _session_path(index: int) -> str:
    return f"session-{index + 1:04d}.md"


def _turn_text(turn_index: int, role: str, content: str) -> str:
    return f"## Turn {turn_index + 1} — {role}\n\n{content}"


def _session_markdown(session_index: int, session_date: str, turns: list[dict[str, Any]]) -> str:
    body = "\n\n".join(
        _turn_text(index, turn["role"], turn["content"]) for index, turn in enumerate(turns)
    )
    return (
        f"# Conversation session {session_index + 1:04d}\n\n"
        f"Session date: {session_date}\n\n"
        f"{body}\n"
    )


def _case_files(record: dict[str, Any], manifest: dict[str, Any]) -> dict[str, bytes]:
    # Upstream session ids are reference metadata, not stable unique keys. The
    # pinned cleaned dataset contains repeated ids for distinct ordinal
    # occurrences. Preserve every occurrence instead of collapsing content
    # through a dict keyed by the source id.
    session_paths = [
        _session_path(index) for index, _session_id in enumerate(record["haystack_session_ids"])
    ]
    source_paths_by_session_id: dict[str, list[str]] = {}
    for session_id, source_path in zip(record["haystack_session_ids"], session_paths):
        source_paths_by_session_id.setdefault(session_id, []).append(source_path)
    files: dict[str, bytes] = {}
    bucket = _record_bucket(record)
    gold_targets: list[dict[str, str]] = []
    gold_spans: list[dict[str, Any]] = []
    turn_references: list[dict[str, Any]] = []
    for session_index, session_id in enumerate(record["haystack_session_ids"]):
        turns = record["haystack_sessions"][session_index]
        source_path = session_paths[session_index]
        document = _session_markdown(session_index, record["haystack_dates"][session_index], turns)
        document_bytes = document.encode("utf-8")
        files[f"inputs/{source_path}"] = document_bytes
        for turn_index, turn in enumerate(turns):
            if turn.get("has_answer") is not True:
                continue
            quote = _turn_text(turn_index, turn["role"], turn["content"])
            quote_bytes = quote.encode("utf-8")
            quote_hash = _sha256(quote_bytes)
            if bucket != "abstention":
                byte_start = document_bytes.find(quote_bytes)
                if byte_start < 0 or document_bytes.find(quote_bytes, byte_start + 1) >= 0:
                    raise AdapterError(
                        f"question {record['question_id']} has a non-unique evidence turn"
                    )
                byte_end = byte_start + len(quote_bytes)
                gold_targets.append(
                    {
                        "source_path": source_path,
                        "quote": quote,
                        "quote_hash": f"sha256:{quote_hash}",
                    }
                )
                gold_spans.append(
                    {
                        "path": source_path,
                        "byte_start": byte_start,
                        "byte_end": byte_end,
                        "quote_hash": f"sha256:{quote_hash}",
                    }
                )
            turn_references.append(
                {
                    "session_id": session_id,
                    "turn_index": turn_index,
                    "role": turn["role"],
                    "source_path": source_path,
                    "quote_sha256": quote_hash,
                }
            )

    expected_sources = (
        []
        if bucket == "abstention"
        else [
            source_path
            for session_id in record["answer_session_ids"]
            for source_path in source_paths_by_session_id[session_id]
        ]
    )
    question = {
        "id": record["question_id"],
        "category": bucket,
        "tier": TIER_BY_BUCKET[bucket],
        "question": f"Current Date: {record['question_date']}\nQuestion: {record['question']}",
        "expected_answer": _normalized_answer(record["answer"], "answer"),
        "expected_source_paths": expected_sources,
        "must_contain": [],
        "gold_targets": gold_targets,
        "gold_spans": gold_spans,
    }
    if bucket == "abstention":
        question["negative_control"] = True
    elif bucket == "temporal-reasoning":
        # Session timestamps and exact evidence remain valid retrieval targets,
        # but ADR 0040 deliberately does not claim valid-time query semantics.
        # Golden judges exclude this answer verdict from QA aggregates.
        question["unsupported_capability"] = "valid_time_queries"
    questions = {"settings": {"k": 30}, "questions": [question]}
    dataset = {
        "name": _case_id(record["question_id"]),
        "description": "One isolated LongMemEval diagnostic case; non-gating procurement evidence.",
        "source": {
            "origin": manifest["dataset"]["url"],
            "revision": manifest["dataset"]["revision"],
            "file": manifest["dataset"]["file"],
            "question_id": record["question_id"],
            "license": manifest["dataset"]["license"],
        },
        "settings": {"k": 30},
        "files": session_paths,
    }
    reference = {
        "schema_version": "longmemeval-case-reference.v3",
        "question_id": record["question_id"],
        "question_type": record["question_type"],
        "selection_bucket": bucket,
        "question_date": record["question_date"],
        "answer_session_ids": record["answer_session_ids"],
        "source_paths_by_session_id": source_paths_by_session_id,
        "duplicate_session_ids": {
            session_id: len(paths)
            for session_id, paths in source_paths_by_session_id.items()
            if len(paths) > 1
        },
        "turn_references": turn_references,
        "labels_ingested": False,
        "qa_evaluation": (
            "unsupported_valid_time_queries" if bucket == "temporal-reasoning" else "score_eligible"
        ),
    }
    files["dataset.yaml"] = yaml.safe_dump(
        dataset, sort_keys=False, allow_unicode=True, width=1000
    ).encode("utf-8")
    files["questions.yaml"] = yaml.safe_dump(
        questions, sort_keys=False, allow_unicode=True, width=1000
    ).encode("utf-8")
    files["reference.json"] = (
        json.dumps(reference, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    ).encode("utf-8")
    return files


def _tree_hash(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for path, data in sorted(files.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(data)
        digest.update(b"\0")
    return digest.hexdigest()


def _validated_output_path(output_path: Path) -> Path:
    resolved = output_path.expanduser().resolve()
    if resolved == REPO_ROOT or resolved.is_relative_to(REPO_ROOT):
        raise AdapterError("derived benchmark content must be written outside the repository")
    if resolved.exists() and not resolved.is_dir():
        raise AdapterError(f"output path is not a directory: {resolved}")
    if resolved.exists() and any(resolved.iterdir()):
        raise AdapterError(f"output directory must be empty: {resolved}")
    return resolved


def convert(source_path: Path, manifest_path: Path, output_path: Path) -> dict[str, Any]:
    manifest_bytes = manifest_path.read_bytes()
    manifest = _parse_manifest(manifest_bytes, str(manifest_path))
    output_path = _validated_output_path(output_path)
    source_bytes = source_path.read_bytes()
    records = _parse_source(source_bytes, manifest, verify_pin=True)
    selected = select_records(records, manifest)
    validate_records(selected, manifest)

    case_ids = [_case_id(record["question_id"]) for record in selected]
    if len(case_ids) != len(set(case_ids)):
        raise AdapterError("selected question ids collide after case-id derivation")

    output_path.mkdir(parents=True, exist_ok=True)

    selected_ids = [record["question_id"] for record in selected]
    selected_ids_bytes = json.dumps(
        selected_ids, ensure_ascii=False, separators=(",", ":")
    ).encode()
    all_case_files: dict[str, bytes] = {}
    cases: list[dict[str, Any]] = []
    for record in selected:
        case_id = _case_id(record["question_id"])
        case_files = _case_files(record, manifest)
        file_hashes: dict[str, str] = {}
        for relative_path, data in case_files.items():
            full_relative_path = f"cases/{case_id}/{relative_path}"
            destination = output_path / full_relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            all_case_files[full_relative_path] = data
            file_hashes[relative_path] = _sha256(data)
        cases.append(
            {
                "case_id": case_id,
                "question_id": record["question_id"],
                "selection_bucket": _record_bucket(record),
                "qa_score_eligible": _record_bucket(record) != "temporal-reasoning",
                "files": dict(sorted(file_hashes.items())),
            }
        )

    result = {
        "schema_version": "longmemeval-materialization.v3",
        "adapter_version": ADAPTER_VERSION,
        "gating": False,
        "source": {
            "path_basename": source_path.name,
            "sha256": _sha256(source_bytes),
            "size_bytes": len(source_bytes),
            "dataset_revision": manifest["dataset"]["revision"],
        },
        "frozen_manifest_sha256": _sha256(manifest_bytes),
        "selected_question_ids": selected_ids,
        "selected_question_ids_sha256": _sha256(selected_ids_bytes),
        "selected_count": len(selected_ids),
        "output_tree_sha256": _tree_hash(all_case_files),
        "cases": cases,
        "runtime": dict(manifest["runtime_placeholders"]),
        "claim": manifest["privacy_and_license"]["result_claim"],
    }
    (output_path / "reproducibility-manifest.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return result


def _validate_manifest_command(args: argparse.Namespace) -> None:
    manifest = load_manifest(args.manifest)
    print(
        json.dumps(
            {
                "schema_version": manifest["schema_version"],
                "adapter_version": manifest["adapter_version"],
                "valid": True,
                "gating": False,
            },
            sort_keys=True,
        )
    )


def _convert_command(args: argparse.Namespace) -> None:
    result = convert(args.source, args.manifest, args.out)
    print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate-manifest")
    validate.add_argument("--manifest", type=Path, required=True)
    validate.set_defaults(handler=_validate_manifest_command)
    convert_parser = commands.add_parser("convert")
    convert_parser.add_argument("--manifest", type=Path, required=True)
    convert_parser.add_argument("--source", type=Path, required=True)
    convert_parser.add_argument("--out", type=Path, required=True)
    convert_parser.set_defaults(handler=_convert_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        args.handler(args)
    except (AdapterError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
