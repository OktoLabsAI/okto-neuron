from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


_ADAPTER_PATH = Path(__file__).with_name("adapter.py")
_SPEC = importlib.util.spec_from_file_location("longmemeval_adapter", _ADAPTER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
adapter = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(adapter)


def _record(question_type: str, ordinal: int, *, abstention: bool = False) -> dict[str, object]:
    question_id = f"{question_type}-{ordinal}"
    if abstention:
        question_id += "_abs"
    session_id = f"answer_{question_type}_{ordinal}"
    answer: object = (
        "The information provided is not enough."
        if abstention
        else (ordinal if question_type == "temporal-reasoning" else f"Answer for {question_id}.")
    )
    has_answer = not abstention or ordinal % 2 == 1
    return {
        "question_id": question_id,
        "question_type": question_type,
        "question": f"What happened in {question_id}?",
        "answer": answer,
        "question_date": "2026/01/02 (Fri) 10:00",
        "haystack_session_ids": [session_id],
        "haystack_dates": ["2026/01/01 (Thu) 09:00"],
        "haystack_sessions": [
            [
                {"role": "user", "content": f"Private prompt for {question_id}."},
                {
                    "role": "assistant",
                    "content": f"Answer for {question_id}. Literal code marker: \\n.",
                    "has_answer": has_answer,
                },
            ]
        ],
        "answer_session_ids": [session_id],
    }


def _records() -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for question_type in sorted(adapter.QUESTION_TYPES):
        records.extend(_record(question_type, ordinal) for ordinal in (1, 2))
    records.extend(_record("single-session-user", ordinal, abstention=True) for ordinal in (3, 4))
    first_answerable = records[0]
    first_answerable["haystack_session_ids"].append("answer_unlabeled_evidence")
    first_answerable["haystack_dates"].append("2025/12/31 (Wed) 08:00")
    first_answerable["haystack_sessions"].append(
        [{"role": "user", "content": "Relevant evidence without a turn-level label."}]
    )
    first_answerable["answer_session_ids"].append("answer_unlabeled_evidence")
    return records


def _source(path: Path, records: list[dict[str, object]]) -> bytes:
    data = (json.dumps(records, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(data)
    return data


def _manifest(path: Path, source_data: bytes, record_count: int) -> dict[str, object]:
    frozen = json.loads(Path(__file__).with_name("frozen-manifest.json").read_text())
    frozen["dataset"]["file"] = "synthetic-schema-compatible.json"
    frozen["dataset"]["file_sha256"] = hashlib.sha256(source_data).hexdigest()
    frozen["dataset"]["file_size_bytes"] = len(source_data)
    frozen["dataset"]["expected_instances"] = record_count
    frozen["selection"]["per_bucket"] = 1
    path.write_text(json.dumps(frozen, indent=2, sort_keys=True) + "\n")
    return frozen


def test_frozen_manifest_is_valid_and_fail_closed() -> None:
    manifest = adapter.load_manifest(Path(__file__).with_name("frozen-manifest.json"))
    assert manifest["status"]["gating"] is False

    invalid = copy.deepcopy(manifest)
    invalid["status"]["gating"] = True
    with pytest.raises(adapter.AdapterError, match="gating must remain false"):
        adapter.validate_manifest(invalid)

    invalid = copy.deepcopy(manifest)
    invalid["runtime_placeholders"]["llm_model"] = "unfrozen-model"
    with pytest.raises(adapter.AdapterError, match="llm_model must remain UNSET"):
        adapter.validate_manifest(invalid)

    invalid = copy.deepcopy(manifest)
    del invalid["privacy_and_license"]["result_claim"]
    with pytest.raises(adapter.AdapterError, match="result_claim"):
        adapter.validate_manifest(invalid)

    invalid = copy.deepcopy(manifest)
    invalid["reporting"]["required_disclosures"] = []
    with pytest.raises(adapter.AdapterError, match="reporting.required_disclosures"):
        adapter.validate_manifest(invalid)


def test_conversion_is_order_independent_and_keeps_labels_out_of_inputs(tmp_path: Path) -> None:
    records = _records()
    source_a = tmp_path / "source-a.json"
    source_b = tmp_path / "source-b.json"
    data_a = _source(source_a, records)
    data_b = _source(source_b, list(reversed(records)))
    manifest_a = tmp_path / "manifest-a.json"
    manifest_b = tmp_path / "manifest-b.json"
    _manifest(manifest_a, data_a, len(records))
    _manifest(manifest_b, data_b, len(records))

    result_a = adapter.convert(source_a, manifest_a, tmp_path / "output-a")
    result_b = adapter.convert(source_b, manifest_b, tmp_path / "output-b")

    assert result_a["selected_question_ids"] == result_b["selected_question_ids"]
    assert result_a["selected_question_ids_sha256"] == result_b["selected_question_ids_sha256"]
    assert result_a["output_tree_sha256"] == result_b["output_tree_sha256"]
    assert result_a["selected_count"] == len(adapter.BUCKETS)
    assert result_a["frozen_manifest_sha256"] == hashlib.sha256(manifest_a.read_bytes()).hexdigest()

    for case in result_a["cases"]:
        case_root = tmp_path / "output-a" / "cases" / case["case_id"]
        questions = yaml.safe_load((case_root / "questions.yaml").read_text())
        assert len(questions["questions"]) == 1
        question = questions["questions"][0]
        assert question["question"].startswith("Current Date: 2026/01/02 (Fri) 10:00\nQuestion: ")
        inputs = "\n".join(path.read_text() for path in (case_root / "inputs").iterdir())
        assert "has_answer" not in inputs
        assert "Session date:" in inputs
        assert "## Turn 1 — user" in inputs
        assert "## Turn 2 — assistant" in inputs
        reference = json.loads((case_root / "reference.json").read_text())
        assert all(session_id not in inputs for session_id in reference["answer_session_ids"])
        assert all(path.name.startswith("session-") for path in (case_root / "inputs").iterdir())
        if case["selection_bucket"] == "temporal-reasoning":
            assert isinstance(question["expected_answer"], str)
            assert question["expected_answer"].isdigit()
            assert question["unsupported_capability"] == "valid_time_queries"
            assert case["qa_score_eligible"] is False
            assert reference["qa_evaluation"] == "unsupported_valid_time_queries"
        else:
            assert case["qa_score_eligible"] is True
            assert reference["qa_evaluation"] == "score_eligible"
        if case["selection_bucket"] == "abstention":
            assert question["negative_control"] is True
            assert question["expected_source_paths"] == []
            assert question["gold_targets"] == []
            assert question["gold_spans"] == []


def test_gold_quote_is_an_exact_hashed_input_slice(tmp_path: Path) -> None:
    records = _records()
    source = tmp_path / "source.json"
    source_data = _source(source, records)
    manifest = tmp_path / "manifest.json"
    _manifest(manifest, source_data, len(records))
    result = adapter.convert(source, manifest, tmp_path / "output")
    answerable = next(case for case in result["cases"] if case["selection_bucket"] != "abstention")
    case_root = tmp_path / "output" / "cases" / answerable["case_id"]
    question = yaml.safe_load((case_root / "questions.yaml").read_text())["questions"][0]
    assert question["expected_source_paths"]
    assert question["gold_targets"]
    for target in question["gold_targets"]:
        document = (case_root / "inputs" / target["source_path"]).read_text()
        assert target["quote"] in document
        expected_hash = hashlib.sha256(target["quote"].encode()).hexdigest()
        assert target["quote_hash"] == f"sha256:{expected_hash}"
        assert "\\n" in target["quote"]

    for span in question["gold_spans"]:
        document = (case_root / "inputs" / span["path"]).read_bytes()
        evidence = document[span["byte_start"] : span["byte_end"]]
        assert span["quote_hash"] == f"sha256:{hashlib.sha256(evidence).hexdigest()}"

    reference = json.loads((case_root / "reference.json").read_text())
    assert reference["labels_ingested"] is False
    assert reference["turn_references"]

    judge = Path(__file__).parents[1] / "bin" / "judge.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(judge),
            "floor",
            str(case_root),
            "--out",
            str(tmp_path / "floor-report.json"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    floor = json.loads((tmp_path / "floor-report.json").read_text())
    assert floor["question_validation_pass"] is True
    assert floor["provenance_gate_pass"] is True


def test_duplicate_source_session_ids_preserve_every_ordinal_occurrence(tmp_path: Path) -> None:
    records = _records()
    for record in records:
        duplicate_id = record["haystack_session_ids"][0]
        record["haystack_session_ids"].append(duplicate_id)
        record["haystack_dates"].append("2025/12/30 (Tue) 07:00")
        record["haystack_sessions"].append(
            [{"role": "user", "content": "Repeated upstream id, distinct ordinal occurrence."}]
        )

    source = tmp_path / "source.json"
    source_data = _source(source, records)
    manifest = tmp_path / "manifest.json"
    _manifest(manifest, source_data, len(records))
    result = adapter.convert(source, manifest, tmp_path / "output")

    for case in result["cases"]:
        case_root = tmp_path / "output" / "cases" / case["case_id"]
        dataset = yaml.safe_load((case_root / "dataset.yaml").read_text())
        reference = json.loads((case_root / "reference.json").read_text())
        assert reference["schema_version"] == "longmemeval-case-reference.v3"
        assert reference["duplicate_session_ids"]
        duplicate_id, count = next(iter(reference["duplicate_session_ids"].items()))
        duplicate_paths = reference["source_paths_by_session_id"][duplicate_id]
        assert count == len(duplicate_paths) == 2
        assert len(dataset["files"]) == len(list((case_root / "inputs").iterdir()))
        assert all((case_root / "inputs" / path).is_file() for path in duplicate_paths)


def test_empty_turn_content_is_preserved_without_placeholder(tmp_path: Path) -> None:
    records = _records()
    for record in records:
        record["haystack_sessions"][0][0]["content"] = ""

    source = tmp_path / "source.json"
    source_data = _source(source, records)
    manifest = tmp_path / "manifest.json"
    _manifest(manifest, source_data, len(records))
    result = adapter.convert(source, manifest, tmp_path / "output")

    for case in result["cases"]:
        case_root = tmp_path / "output" / "cases" / case["case_id"]
        first_input = sorted((case_root / "inputs").iterdir())[0].read_text()
        assert "## Turn 1 — user\n\n\n\n## Turn 2 — assistant" in first_input
        assert "[empty]" not in first_input


def test_generated_questions_fail_closed_without_core_yaml_dependency(tmp_path: Path) -> None:
    records = _records()
    source = tmp_path / "source.json"
    source_data = _source(source, records)
    manifest = tmp_path / "manifest.json"
    _manifest(manifest, source_data, len(records))
    result = adapter.convert(source, manifest, tmp_path / "output")
    answerable = next(case for case in result["cases"] if case["selection_bucket"] != "abstention")
    questions = tmp_path / "output" / "cases" / answerable["case_id"] / "questions.yaml"
    judge = Path(__file__).parents[1] / "bin" / "judge.py"
    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            str(judge),
            "validate-questions",
            "--questions",
            str(questions),
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert json.loads(completed.stderr) == {
        "error": "golden_yaml_error",
        "detail": (
            "PyYAML is required for Golden YAML; run through the uv-managed Okto Neuron environment"
        ),
    }


def test_source_pin_is_verified_before_conversion(tmp_path: Path) -> None:
    records = _records()
    source = tmp_path / "source.json"
    source_data = _source(source, records)
    manifest_path = tmp_path / "manifest.json"
    _manifest(manifest_path, source_data, len(records))
    source.write_bytes(source.read_bytes().replace(b"Private prompt", b"Altered prompt", 1))

    with pytest.raises(adapter.AdapterError, match="source SHA-256 mismatch"):
        adapter.convert(source, manifest_path, tmp_path / "output")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("duplicate-question", "duplicate question_id"),
        ("unaligned-sessions", "must align"),
        ("unknown-answer-session", "unknown sessions"),
        ("answerless-answerable", "at least one answer session"),
        ("labeled-outside-answer-session", "only inside answer_session_ids"),
        ("no-labeled-turn", "at least one has_answer turn"),
    ],
)
def test_source_schema_rejects_ambiguous_reference_shapes(mutation: str, message: str) -> None:
    records = _records()
    if mutation == "duplicate-question":
        records[1]["question_id"] = records[0]["question_id"]
    elif mutation == "unaligned-sessions":
        records[0]["haystack_dates"] = []
    elif mutation == "unknown-answer-session":
        records[0]["answer_session_ids"] = ["missing-session"]
    elif mutation == "answerless-answerable":
        records[0]["answer_session_ids"] = []
    elif mutation == "labeled-outside-answer-session":
        records[0]["answer_session_ids"] = [records[0]["haystack_session_ids"][0]]
        records[0]["haystack_sessions"][1][0]["has_answer"] = True
    elif mutation == "no-labeled-turn":
        for session in records[0]["haystack_sessions"]:
            for turn in session:
                turn.pop("has_answer", None)
    else:  # pragma: no cover - guarded by the parametrization above
        raise AssertionError(mutation)

    frozen = json.loads(Path(__file__).with_name("frozen-manifest.json").read_text())
    with pytest.raises(adapter.AdapterError, match=message):
        adapter.validate_records(records, frozen)


def test_full_schema_validation_applies_to_selected_records_only(tmp_path: Path) -> None:
    records = _records()
    selection_manifest = json.loads(Path(__file__).with_name("frozen-manifest.json").read_text())
    selection_manifest["selection"]["per_bucket"] = 1
    selected_ids = {
        record["question_id"] for record in adapter.select_records(records, selection_manifest)
    }
    unselected = next(record for record in records if record["question_id"] not in selected_ids)
    del unselected["answer"]

    source = tmp_path / "source.json"
    source_data = _source(source, records)
    manifest = tmp_path / "manifest.json"
    _manifest(manifest, source_data, len(records))
    result = adapter.convert(source, manifest, tmp_path / "output")

    assert set(result["selected_question_ids"]) == selected_ids


def test_conversion_refuses_nonempty_output(tmp_path: Path) -> None:
    records = _records()
    source = tmp_path / "source.json"
    source_data = _source(source, records)
    manifest = tmp_path / "manifest.json"
    _manifest(manifest, source_data, len(records))
    output = tmp_path / "output"
    output.mkdir()
    (output / "keep.txt").write_text("owned by caller")

    with pytest.raises(adapter.AdapterError, match="must be empty"):
        adapter.convert(source, manifest, output)
    assert (output / "keep.txt").read_text() == "owned by caller"


def test_conversion_refuses_derived_corpus_inside_repository(tmp_path: Path) -> None:
    records = _records()
    source = tmp_path / "source.json"
    source_data = _source(source, records)
    manifest = tmp_path / "manifest.json"
    _manifest(manifest, source_data, len(records))

    with pytest.raises(adapter.AdapterError, match="outside the repository"):
        adapter.convert(source, manifest, adapter.REPO_ROOT / "derived-longmemeval-test-output")
    assert not (adapter.REPO_ROOT / "derived-longmemeval-test-output").exists()
