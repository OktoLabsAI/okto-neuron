from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import judge  # noqa: E402
import manifest  # noqa: E402
import floor_metrics  # noqa: E402
import panel  # noqa: E402
import recall_floor  # noqa: E402
import scorecard  # noqa: E402
import semantic_judge  # noqa: E402
import sweep  # noqa: E402


def _recall_cost(*, latency_ms: float = 1.0) -> dict:
    return {
        "schema_version": "recall_cost.v1",
        "measurement_status": "measured",
        "completion_calls": 0,
        "generated_tokens": 0,
        "query_embedding_calls": 1,
        "query_embedding_provider": "stub",
        "query_embedding_model": "sha256",
        "query_embedding_execution": "local",
        "query_embedding_latency_ms": latency_ms,
        "deterministic_retrieval_latency_ms": latency_ms,
        "deterministic_projection_latency_ms": latency_ms,
        "total_latency_ms": latency_ms * 3,
        "retrieved_results": 2,
        "retrieved_bytes": 128,
        "completion_free": True,
    }


def _semantic_capture(sample_count: int) -> dict:
    return {
        "status": "ok",
        "semantic_quality": {
            "schema_version": "semantic_quality.v1",
            "evaluator_version": "semantic_quality.v1",
            "evidence": {},
            "population": {},
            "layers": {
                "surface": {},
                "type": {},
                "identity": {},
                "predicate": {},
                "relation": {},
                "recall": {
                    "schema_version": "recall_cost.aggregate.v1",
                    "status": "measured",
                    "provided_samples": sample_count,
                    "measured_samples": sample_count,
                    "rejected_samples": {"count": 0, "samples": []},
                    "completion": {},
                    "query_embedding": {},
                    "deterministic_retrieval": {},
                    "deterministic_projection": {},
                    "total_latency_ms": {},
                    "answer_generation": {},
                },
            },
            "hard_invariants": {},
            "verdict": {},
            "limitations": [],
        },
    }


def _write_deterministic(tmp_path: Path, response_ids: list[str]) -> Path:
    path = tmp_path / "deterministic.json"
    path.write_text(
        json.dumps(
            {
                "questions": len(response_ids),
                "k": 10,
                "provenance_gate_pass": True,
                "total_failures": 0,
                "failures": [],
                "per_question": [
                    {"id": qid, "provenance_ok": True, "hits_checked": 0} for qid in response_ids
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_semantic(tmp_path: Path, responses: Path, sample_count: int) -> Path:
    capture = _semantic_capture(sample_count)
    capture["capture"] = {
        "schema_version": "golden.semantic_quality_capture.v1",
        "responses_sha256": hashlib.sha256(responses.read_bytes()).hexdigest(),
        "sample_count": sample_count,
    }
    path = tmp_path / "semantic-quality.json"
    path.write_text(json.dumps(capture), encoding="utf-8")
    return path


def test_positive_question_without_gold_target_is_invalid() -> None:
    errors = judge.question_validation_errors(
        {"questions": [{"id": "q-empty", "negative_control": False}]}
    )
    assert errors == ["q-empty: non-negative question requires at least one gold_target"]


def test_negative_control_may_omit_gold_target() -> None:
    assert (
        judge.question_validation_errors(
            {"questions": [{"id": "q-negative", "negative_control": True}]}
        )
        == []
    )


def test_question_validation_rejects_empty_or_malformed_documents() -> None:
    assert judge.question_validation_errors({"questions": []}) == [
        "questions must contain at least one question"
    ]
    assert judge.question_validation_errors({"questions": {}}) == ["questions must be a list"]
    assert judge.question_validation_errors([]) == ["questions document must be a map"]


def test_question_validation_requires_unique_ids_and_one_pinned_k() -> None:
    errors = judge.question_validation_errors(
        {
            "settings": {"k": 10},
            "questions": [
                {"id": "same", "negative_control": True},
                {"id": "same", "negative_control": True, "k": 20},
            ],
        }
    )

    assert "duplicate question id: same" in errors
    assert "same: k=20 differs from pinned settings.k=10" in errors


def test_question_validation_restricts_unsupported_capability() -> None:
    errors = judge.question_validation_errors(
        {
            "questions": [
                {
                    "id": "q-unsupported",
                    "negative_control": True,
                    "unsupported_capability": "invented_capability",
                }
            ]
        }
    )

    assert errors == ["q-unsupported: unsupported_capability must be one of valid_time_queries"]


def test_valid_time_questions_are_reported_unsupported_and_excluded_from_qa(
    tmp_path: Path,
) -> None:
    questions = tmp_path / "questions.yaml"
    questions.write_text(
        "questions:\n"
        "  - id: q-time\n"
        "    question: What was true at the requested valid time?\n"
        "    expected_answer: historical value\n"
        "    unsupported_capability: valid_time_queries\n"
        "    gold_targets:\n"
        "      - source_path: history.md\n"
        "        quote: historical value\n",
        encoding="utf-8",
    )
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        json.dumps({"id": "q-time", "ask": {"text": "historical value"}}) + "\n",
        encoding="utf-8",
    )

    scripted_path = tmp_path / "scripted.json"
    scripted_result = judge.cmd_judge(
        argparse.Namespace(
            questions=str(questions),
            responses=str(responses),
            base_url="http://127.0.0.1:1",
            model="auto",
            max_tokens=32,
            out=str(scripted_path),
        )
    )
    scripted = json.loads(scripted_path.read_text())

    semantic_path = tmp_path / "semantic.json"
    semantic_result = semantic_judge.cmd_judge_file(
        argparse.Namespace(
            questions=str(questions),
            responses=str(responses),
            limit=0,
            base_url="http://127.0.0.1:1",
            model="auto",
            max_tokens=32,
            out=str(semantic_path),
        )
    )
    semantic = json.loads(semantic_path.read_text())
    arm_path = tmp_path / "arm.jsonl"
    arm_result = semantic_judge.cmd_to_arm(
        argparse.Namespace(
            judge=str(semantic_path),
            out=str(arm_path),
            partial_as_correct=False,
        )
    )

    assert scripted_result == semantic_result == arm_result == 0
    assert scripted["tally"]["unsupported"] == 1
    assert scripted["verdicts"][0] == {
        "id": "q-time",
        "tier": None,
        "verdict": "unsupported",
        "rationale": (
            "ADR 0040 preserves temporal evidence but does not support valid-time query semantics"
        ),
        "score_eligible": False,
        "unsupported_capability": "valid_time_queries",
    }
    assert semantic["tally"]["UNSUPPORTED"] == 1
    assert semantic["verdicts"][0]["verdict"] == "UNSUPPORTED"
    assert semantic["verdicts"][0]["score_eligible"] is False
    assert arm_path.read_text() == ""


def test_authoritative_yaml_loader_parses_committed_smoke_questions() -> None:
    document = judge.load_yaml(Path("tests/golden/datasets/_smoke/questions.yaml"))

    assert len(document["questions"]) == 8
    assert judge.question_validation_errors(document) == []
    ladybug = next(q for q in document["questions"] if q["id"] == "t2-ladybug-decision")
    assert ladybug["gold_targets"][0]["quote"].startswith("states that")
    assert not ladybug["gold_targets"][0]["quote"].endswith("\n")


def test_authoritative_yaml_loader_preserves_exact_multiline_text(tmp_path: Path) -> None:
    import yaml

    expected = {
        "questions": [
            {
                "id": "q-generated",
                "question": "Heading\n# literal heading\nmiddle # literal suffix\n" + "long " * 300,
                "gold_targets": [
                    {
                        "source_path": "session-0001.md",
                        "quote": "Sam said: 'I will go.' Literal \\n marker.",
                    }
                ],
            }
        ]
    }
    path = tmp_path / "questions.yaml"
    path.write_text(
        yaml.safe_dump(expected, sort_keys=False, allow_unicode=True, width=1000),
        encoding="utf-8",
    )

    assert judge.load_yaml(path) == expected


def test_authoritative_yaml_loader_reports_malformed_yaml(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text('broken: "\n', encoding="utf-8")

    with pytest.raises(judge.GoldenYamlError, match="invalid YAML"):
        judge.load_yaml(path)


def test_scorecard_yaml_path_fails_closed_without_pyyaml(tmp_path: Path) -> None:
    questions = tmp_path / "questions.yaml"
    questions.write_text(
        "questions:\n- id: q-1\n  must_contain: [token]\n",
        encoding="utf-8",
    )
    arm = tmp_path / "arm.jsonl"
    arm.write_text('{"id":"q-1","ask":{"text":"token"}}\n', encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-S",
            str(Path(judge.__file__).resolve()),
            "scorecard",
            "--a",
            str(arm),
            "--b",
            str(arm),
            "--grader",
            "proxy",
            "--questions",
            str(questions),
            "--bootstrap",
            "10",
            "--json",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert json.loads(completed.stderr)["error"] == "golden_yaml_error"


def test_input_validation_rejects_paths_flattened_to_same_basename(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    (inputs / "a").mkdir(parents=True)
    (inputs / "b").mkdir()
    (inputs / "a" / "notes.md").write_text("alpha", encoding="utf-8")
    (inputs / "b" / "NOTES.md").write_text("beta", encoding="utf-8")

    collisions = judge.input_basename_collisions(inputs)

    assert collisions == [["a/notes.md", "b/NOTES.md"]]


def test_input_validation_allows_unique_nested_basenames(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    (inputs / "a").mkdir(parents=True)
    (inputs / "b").mkdir()
    (inputs / "a" / "first.md").write_text("alpha", encoding="utf-8")
    (inputs / "b" / "second.txt").write_text("beta", encoding="utf-8")

    assert judge.input_basename_collisions(inputs) == []


def test_session_reference_metrics_collapse_node_hits_to_unique_sessions() -> None:
    hits = [
        {"provenance": {"path": "/vault/.marginalia/sources/session-a.md"}},
        {"provenance": {"path": "/vault/.marginalia/sources/session-a.md"}},
        {"provenance": {"path": "/vault/.marginalia/sources/distractor.md"}},
        {"provenance": {"path": "/vault/.marginalia/sources/session-b.md"}},
    ]

    report = judge.session_reference_metrics(
        ["session-a.md", "session-b.md"], hits, k_values=(1, 2, 3)
    )

    assert report["status"] == "measured"
    assert report["expected_sessions"] == 2
    assert report["ranked_unique_sessions"] == 3
    assert report["metrics"]["1"] == {
        "recall_any_at_k": 1,
        "recall_all_at_k": 0,
        "ndcg_at_k": 1.0,
        "relevant_sessions_retrieved": 1,
    }
    assert report["metrics"]["2"]["recall_all_at_k"] == 0
    assert report["metrics"]["3"] == {
        "recall_any_at_k": 1,
        "recall_all_at_k": 1,
        "ndcg_at_k": 0.919721,
        "relevant_sessions_retrieved": 2,
    }


def test_session_reference_metrics_exclude_abstention_without_zero_over_zero() -> None:
    assert judge.session_reference_metrics([], [])["status"] == "not_applicable"


def test_provenance_measures_quote_based_gold_targets_as_byte_spans(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    source = inputs / "source.md"
    source.write_text("prefix exact answer suffix", encoding="utf-8")
    questions = tmp_path / "questions.yaml"
    questions.write_text(
        "settings:\n"
        "  k: 1\n"
        "questions:\n"
        "  - id: found\n"
        "    question: What is the answer?\n"
        "    expected_answer: exact answer\n"
        "    expected_source_paths: [source.md]\n"
        "    gold_targets:\n"
        "      - source_path: source.md\n"
        "        quote: exact answer\n"
        "  - id: missed\n"
        "    question: What is the answer?\n"
        "    expected_answer: exact answer\n"
        "    expected_source_paths: [source.md]\n"
        "    gold_targets:\n"
        "      - source_path: source.md\n"
        "        quote: exact answer\n",
        encoding="utf-8",
    )
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "id": "found",
                        "k": 1,
                        "recall": {
                            "hits": [
                                {
                                    "provenance": {
                                        "path": "/vault/.marginalia/sources/source.md",
                                        "byte_start": 0,
                                        "byte_end": 26,
                                    }
                                }
                            ]
                        },
                        "ask": {"citations": []},
                    }
                ),
                json.dumps(
                    {
                        "id": "missed",
                        "k": 1,
                        "recall": {
                            "hits": [
                                {
                                    "provenance": {
                                        "path": "/vault/.marginalia/sources/source.md",
                                        "byte_start": 0,
                                        "byte_end": 6,
                                    }
                                }
                            ]
                        },
                        "ask": {"citations": []},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    output = tmp_path / "deterministic.json"

    result = judge.cmd_provenance(
        argparse.Namespace(
            inputs=str(inputs),
            responses=str(responses),
            questions=str(questions),
            out=str(output),
        )
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in report["per_question"]}

    assert result == 0
    assert report["gold_span_summary"]["questions_with_gold_spans"] == 2
    assert report["gold_span_summary"]["retrieved_at_k"] == 1
    assert by_id["found"]["gold_span_source"] == "gold_targets"
    assert by_id["found"]["gold_spans"]["gold_span_retrieved_at_k"] is True
    assert by_id["missed"]["gold_spans"]["gold_span_retrieved_at_k"] is False


def test_gold_span_iou_counts_overlapping_retrieval_bytes_once(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    source = inputs / "source.md"
    source.write_text("0123456789abcdefghij", encoding="utf-8")
    gold_spans = [{"path": "source.md", "byte_start": 5, "byte_end": 15}]
    repeated_range = {
        "path": "/vault/.marginalia/sources/source.md",
        "byte_start": 0,
        "byte_end": 20,
    }
    hits = [
        {"provenance": repeated_range, "context_spans": [repeated_range]},
        {"provenance": repeated_range, "context_spans": []},
    ]

    metrics = judge.gold_span_metrics(inputs, gold_spans, hits, k=2)

    assert metrics is not None
    # Deprecated alias: arithmetic preserved, but this is a dilution ratio,
    # not a quality score. See mean_gold_byte_dilution for the honest name.
    assert metrics["per_span"][0]["byte_iou"] == 0.5
    assert 0.0 <= metrics["mean_byte_iou"] <= 1.0
    # gold span is 10 bytes (5-15); retrieved union is the full 20-byte block.
    # A wide-block/narrow-gold pair should read as high token_recall (the gold
    # text is fully contained) with high dilution (20/10 == 2.0x more bytes
    # retrieved than the gold quote) -- pinning the honest interpretation.
    span = metrics["per_span"][0]
    assert span["gold_bytes"] == 10
    assert span["retrieved_bytes"] == 20
    assert span["byte_dilution_ratio"] == 2.0
    assert metrics["mean_gold_byte_dilution"] == 2.0


def test_floor_rejects_zero_over_zero_positive_dataset(tmp_path: Path) -> None:
    dataset = tmp_path / "external-dataset"
    (dataset / "inputs").mkdir(parents=True)
    (dataset / "inputs" / "source.md").write_text("known fact\n", encoding="utf-8")
    (dataset / "questions.yaml").write_text(
        "questions:\n  - id: q-empty\n    negative_control: false\n    question: What is known?\n",
        encoding="utf-8",
    )
    output = tmp_path / "floor.json"

    result = judge.cmd_floor(
        argparse.Namespace(dataset=str(dataset), out=str(output), baseline=None)
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert result == 1
    assert report["gold_targets_resolved"] == 0
    assert report["gold_targets_total"] == 0
    assert report["question_validation_pass"] is False
    assert report["provenance_gate_pass"] is False


def test_endpoint_dataset_identity_matches_source_bytes(tmp_path: Path, monkeypatch) -> None:
    dataset = tmp_path / "outside-repo"
    inputs = dataset / "inputs"
    inputs.mkdir(parents=True)
    source_a = b"alpha\n"
    source_b = b"beta\n"
    (inputs / "a.md").write_bytes(source_a)
    (inputs / "nested").mkdir()
    (inputs / "nested" / "b.txt").write_bytes(source_b)

    details = {
        "doc-a": {"sha256": hashlib.sha256(source_a).hexdigest(), "byte_length": len(source_a)},
        "doc-b": {"sha256": hashlib.sha256(source_b).hexdigest(), "byte_length": len(source_b)},
    }

    def fake_endpoint_json(_endpoint: str, path: str) -> dict:
        if path.startswith("/api/v1/nodes?type=Document"):
            return {
                "total": 2,
                "nodes": [{"id": "doc-a"}, {"id": "doc-b"}],
            }
        node_id = path.rsplit("/", 1)[-1]
        return {"node": {"facets": details[node_id]}}

    monkeypatch.setattr(manifest, "_endpoint_json", fake_endpoint_json)

    result = manifest.compare_endpoint_dataset(dataset, "http://example.test")

    assert result["matches"] is True
    assert result["expected"]["document_count"] == 2
    assert result["actual"]["content_fingerprint"] == result["expected"]["content_fingerprint"]


def test_endpoint_identity_headers_include_explicit_vault(monkeypatch) -> None:
    monkeypatch.setenv("OKTO_NEURON_AUTH_TOKEN", "identity-token")
    monkeypatch.setenv("OKTO_NEURON_VAULT_PATH", "/vaults/identity")

    assert manifest._endpoint_headers() == {
        "Accept": "application/json",
        "Authorization": "Bearer identity-token",
        "X-Okto-Neuron-Vault": "/vaults/identity",
    }


def test_endpoint_dataset_identity_rejects_wrong_populated_graph(
    tmp_path: Path, monkeypatch
) -> None:
    dataset = tmp_path / "dataset"
    inputs = dataset / "inputs"
    inputs.mkdir(parents=True)
    (inputs / "expected.md").write_bytes(b"expected\n")
    wrong = b"wrong\n"

    def fake_endpoint_json(_endpoint: str, path: str) -> dict:
        if path.startswith("/api/v1/nodes?type=Document"):
            return {"total": 1, "nodes": [{"id": "wrong-doc"}]}
        return {
            "node": {
                "facets": {
                    "sha256": hashlib.sha256(wrong).hexdigest(),
                    "byte_length": len(wrong),
                }
            }
        }

    monkeypatch.setattr(manifest, "_endpoint_json", fake_endpoint_json)

    result = manifest.compare_endpoint_dataset(dataset, "http://example.test")

    assert result["matches"] is False
    assert result["actual"]["document_count"] == 1


def test_semantic_quality_capture_submits_every_recall_cost(tmp_path: Path, monkeypatch) -> None:
    responses = tmp_path / "responses.jsonl"
    rows = [
        {"id": "q1", "recall": {"recall_cost": _recall_cost(latency_ms=1.0)}},
        {"id": "q2", "recall": {"recall_cost": _recall_cost(latency_ms=2.0)}},
    ]
    responses.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    submitted: list[dict] = []

    def fake_post(_endpoint: str, samples: list[dict], *, timeout_s: float) -> dict:
        submitted.extend(samples)
        assert timeout_s == 12.0
        return _semantic_capture(len(samples))

    monkeypatch.setattr(judge, "_post_semantic_quality", fake_post)

    payload = judge.capture_semantic_quality(
        "http://127.0.0.1:7777",
        responses,
        timeout_s=12.0,
    )

    assert submitted == [row["recall"]["recall_cost"] for row in rows]
    assert payload["semantic_quality"]["layers"]["recall"]["measured_samples"] == 2


def test_semantic_quality_transport_posts_only_samples_to_loopback(
    monkeypatch,
) -> None:
    samples = [_recall_cost()]
    captured: dict = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(_semantic_capture(1)).encode("utf-8")

    def fake_urlopen(request, *, timeout: float):
        headers = {key.lower(): value for key, value in request.header_items()}
        captured["url"] = request.full_url
        captured["method"] = request.method
        captured["body"] = json.loads(request.data)
        captured["authorization"] = request.get_header("Authorization")
        captured["vault"] = headers.get("x-okto-neuron-vault")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("OKTO_NEURON_AUTH_TOKEN", "local-test-token")
    monkeypatch.setenv("OKTO_NEURON_VAULT_PATH", "/vaults/semantic")
    monkeypatch.setattr(judge.urllib.request, "urlopen", fake_urlopen)

    judge._post_semantic_quality(
        "http://127.0.0.1:7777/",
        samples,
        timeout_s=9.0,
    )

    assert captured == {
        "url": "http://127.0.0.1:7777/api/v1/quality/semantic",
        "method": "POST",
        "body": {"recall_samples": samples},
        "authorization": "Bearer local-test-token",
        "vault": "/vaults/semantic",
        "timeout": 9.0,
    }


def test_semantic_quality_transport_retries_only_named_audit_busy(monkeypatch, capsys) -> None:
    import io

    samples = [_recall_cost()]
    calls = 0
    sleeps: list[float] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(_semantic_capture(1)).encode("utf-8")

    def fake_urlopen(request, *, timeout: float):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise urllib.error.HTTPError(
                request.full_url,
                409,
                "Conflict",
                {},
                io.BytesIO(b'{"error":"audit_busy"}'),
            )
        return FakeResponse()

    monkeypatch.setattr(judge.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(judge.time, "sleep", sleeps.append)

    payload = judge._post_semantic_quality("http://127.0.0.1:7777", samples, timeout_s=9.0)

    assert payload["status"] == "ok"
    assert calls == 2
    assert sleeps == [1.0]
    assert "audit busy; retry 2/7 in 1s" in capsys.readouterr().err


def test_semantic_quality_capture_rejects_missing_per_question_cost(tmp_path: Path) -> None:
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        json.dumps({"id": "q-missing", "recall": {}}) + "\n",
        encoding="utf-8",
    )

    try:
        judge.capture_semantic_quality(
            "http://127.0.0.1:7777",
            responses,
            timeout_s=12.0,
        )
    except ValueError as exc:
        assert "q-missing" in str(exc)
    else:
        raise AssertionError("missing recall cost must fail closed")


def test_semantic_quality_capture_rejects_partial_endpoint_accounting(
    tmp_path: Path, monkeypatch
) -> None:
    responses = tmp_path / "responses.jsonl"
    rows = [
        {"id": "q1", "recall": {"recall_cost": _recall_cost()}},
        {"id": "q2", "recall": {"recall_cost": _recall_cost()}},
    ]
    responses.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        judge,
        "_post_semantic_quality",
        lambda *_args, **_kwargs: _semantic_capture(1),
    )

    try:
        judge.capture_semantic_quality(
            "http://127.0.0.1:7777",
            responses,
            timeout_s=12.0,
        )
    except ValueError as exc:
        assert "does not match responses.jsonl" in str(exc)
    else:
        raise AssertionError("partial semantic accounting must fail closed")


def test_report_embeds_complete_semantic_quality_sidecar(tmp_path: Path) -> None:
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        json.dumps(
            {
                "id": "q1",
                "k": 10,
                "tier": "T1",
                "question": "What is known?",
                "recall": {"recall_cost": _recall_cost()},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    semantic = tmp_path / "semantic-quality.json"
    capture = _semantic_capture(1)
    capture["capture"] = {
        "schema_version": "golden.semantic_quality_capture.v1",
        "responses_sha256": hashlib.sha256(responses.read_bytes()).hexdigest(),
        "sample_count": 1,
    }
    semantic.write_text(json.dumps(capture), encoding="utf-8")
    deterministic = _write_deterministic(tmp_path, ["q1"])
    output = tmp_path / "report.json"

    result = judge.cmd_report(
        argparse.Namespace(
            dataset="fixture",
            timestamp="run-1",
            responses=str(responses),
            deterministic=str(deterministic),
            judge=None,
            node_types=None,
            semantic_quality=str(semantic),
            out=str(output),
        )
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert result == 0
    assert report["k"] == 10
    assert report["semantic_quality"] == capture["semantic_quality"]
    assert report["semantic_quality_capture"] == capture["capture"]


def test_report_refuses_missing_deterministic_sidecar(tmp_path: Path) -> None:
    responses = tmp_path / "responses.jsonl"
    responses.write_text(json.dumps({"id": "q1", "k": 10}) + "\n", encoding="utf-8")
    semantic = _write_semantic(tmp_path, responses, 1)

    result = judge.cmd_report(
        argparse.Namespace(
            dataset="fixture",
            timestamp="run-1",
            responses=str(responses),
            deterministic=None,
            judge=None,
            node_types=None,
            semantic_quality=str(semantic),
            out=str(tmp_path / "report.json"),
        )
    )

    assert result == 1
    assert not (tmp_path / "report.json").exists()


def test_report_refuses_missing_semantic_sidecar(tmp_path: Path) -> None:
    responses = tmp_path / "responses.jsonl"
    responses.write_text(json.dumps({"id": "q1", "k": 10}) + "\n", encoding="utf-8")
    deterministic = _write_deterministic(tmp_path, ["q1"])

    result = judge.cmd_report(
        argparse.Namespace(
            dataset="fixture",
            timestamp="run-1",
            responses=str(responses),
            deterministic=str(deterministic),
            judge=None,
            node_types=None,
            semantic_quality=None,
            out=str(tmp_path / "report.json"),
        )
    )

    assert result == 1
    assert not (tmp_path / "report.json").exists()


def test_retained_semantic_sidecar_is_bound_to_exact_responses(tmp_path: Path) -> None:
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        json.dumps({"id": "q-new", "recall": {"recall_cost": _recall_cost()}}) + "\n",
        encoding="utf-8",
    )
    stale = _semantic_capture(1)
    stale["capture"] = {
        "schema_version": "golden.semantic_quality_capture.v1",
        "responses_sha256": hashlib.sha256(b"old response bytes\n").hexdigest(),
        "sample_count": 1,
    }
    sidecar = tmp_path / "semantic-quality.json"
    sidecar.write_text(json.dumps(stale), encoding="utf-8")

    result = judge.cmd_validate_semantic_quality(
        argparse.Namespace(
            responses=str(responses),
            semantic_quality=str(sidecar),
        )
    )

    assert result == 1


def test_retained_semantic_sidecar_validation_happy_path(tmp_path: Path) -> None:
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        json.dumps({"id": "q1", "k": 10, "recall": {"recall_cost": _recall_cost()}}) + "\n",
        encoding="utf-8",
    )
    capture = _semantic_capture(1)
    capture["capture"] = {
        "schema_version": "golden.semantic_quality_capture.v1",
        "responses_sha256": hashlib.sha256(responses.read_bytes()).hexdigest(),
        "sample_count": 1,
    }
    sidecar = tmp_path / "semantic-quality.json"
    sidecar.write_text(json.dumps(capture), encoding="utf-8")

    result = judge.cmd_validate_semantic_quality(
        argparse.Namespace(responses=str(responses), semantic_quality=str(sidecar))
    )

    assert result == 0


def _write_byte_grounding_fixture(
    tmp_path: Path,
    *,
    captured_hash: str | None = None,
    cited: bool = True,
) -> tuple[Path, Path, Path, Path]:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    source = inputs / "source.md"
    source.write_bytes(b"prefix grounded relation suffix")
    start = len(b"prefix ")
    end = start + len(b"grounded relation")
    excerpt_hash = hashlib.sha256(source.read_bytes()[start:end]).hexdigest()
    claim_id = "claim-1"
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        json.dumps(
            {
                "id": "q1",
                "ask": {"citations": [claim_id] if cited else []},
                "recall": {
                    "hits": [
                        {
                            "node": {"id": claim_id, "type": "Claim"},
                            "provenance": {
                                "path": "/detached/vault/source.md",
                                "byte_start": start,
                                "byte_end": end,
                                "content_hash": captured_hash or f"sha256:{excerpt_hash}",
                            },
                        },
                        {
                            "node": {"id": "agent-1", "type": "Agent"},
                            "provenance": {
                                "path": "/detached/vault/source.md",
                                "byte_start": start,
                                "byte_end": end,
                                "content_hash": f"sha256:{excerpt_hash}",
                            },
                        },
                    ]
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "run-manifest.json"
    manifest.write_text('{"manifest_version":5}\n', encoding="utf-8")
    semantic = tmp_path / "semantic-quality.json"
    semantic.write_text(
        json.dumps(
            {
                "semantic_quality": {
                    "evidence": {"graph_generation": "generation-1"},
                    "semantic_snapshot": {
                        "fingerprints": {"semantic_policy": f"sha256:{'a' * 64}"}
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return inputs, responses, manifest, semantic


def test_byte_grounding_materializes_only_cited_claims_from_trust_copy(
    tmp_path: Path,
) -> None:
    inputs, responses, manifest, semantic = _write_byte_grounding_fixture(tmp_path)

    first = judge.build_byte_grounding_evidence(
        corpus_id="fixture",
        inputs_root=inputs,
        responses_path=responses,
        manifest_path=manifest,
        semantic_quality_path=semantic,
    )
    second = judge.build_byte_grounding_evidence(
        corpus_id="fixture",
        inputs_root=inputs,
        responses_path=responses,
        manifest_path=manifest,
        semantic_quality_path=semantic,
    )

    assert first == second
    assert first["schema_version"] == "byte_grounding_evidence.v1"
    assert first["manifest_sha256"] == (
        f"sha256:{hashlib.sha256(manifest.read_bytes()).hexdigest()}"
    )
    assert first["graph_generation"] == "generation-1"
    assert first["semantic_policy_fingerprint"] == f"sha256:{'a' * 64}"
    assert len(first["samples"]) == 1
    sample = first["samples"][0]
    assert sample["relation_claim_id"] == "claim-1"
    assert sample["source_id"] == "source:source.md"
    assert sample["source_sha256"] == (
        f"sha256:{hashlib.sha256((inputs / 'source.md').read_bytes()).hexdigest()}"
    )
    assert sample["verified"] is True


def test_byte_grounding_fails_closed_on_excerpt_hash_mismatch(tmp_path: Path) -> None:
    inputs, responses, manifest, semantic = _write_byte_grounding_fixture(
        tmp_path,
        captured_hash=f"sha256:{'0' * 64}",
    )

    with pytest.raises(ValueError, match="excerpt hash does not match"):
        judge.build_byte_grounding_evidence(
            corpus_id="fixture",
            inputs_root=inputs,
            responses_path=responses,
            manifest_path=manifest,
            semantic_quality_path=semantic,
        )


def test_byte_grounding_requires_a_cited_claim(tmp_path: Path) -> None:
    inputs, responses, manifest, semantic = _write_byte_grounding_fixture(
        tmp_path,
        cited=False,
    )

    with pytest.raises(ValueError, match="no cited Claim"):
        judge.build_byte_grounding_evidence(
            corpus_id="fixture",
            inputs_root=inputs,
            responses_path=responses,
            manifest_path=manifest,
            semantic_quality_path=semantic,
        )


def test_semantic_capture_rejects_endpoint_rejected_samples(tmp_path: Path) -> None:
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        json.dumps({"id": "q1", "k": 10, "recall": {"recall_cost": _recall_cost()}}) + "\n",
        encoding="utf-8",
    )
    capture = _semantic_capture(1)
    capture["capture"] = {
        "schema_version": "golden.semantic_quality_capture.v1",
        "responses_sha256": hashlib.sha256(responses.read_bytes()).hexdigest(),
        "sample_count": 1,
    }
    capture["semantic_quality"]["layers"]["recall"]["rejected_samples"] = {
        "count": 1,
        "samples": [{"index": 0, "reason": "invalid"}],
    }

    errors = judge.semantic_quality_capture_errors(
        capture,
        expected_samples=1,
        expected_responses_sha256=hashlib.sha256(responses.read_bytes()).hexdigest(),
    )

    assert "recall aggregate rejected one or more samples" in errors


def test_report_refuses_sha_mismatched_semantic_sidecar(tmp_path: Path) -> None:
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        json.dumps({"id": "q1", "k": 10, "recall": {"recall_cost": _recall_cost()}}) + "\n",
        encoding="utf-8",
    )
    capture = _semantic_capture(1)
    capture["capture"] = {
        "schema_version": "golden.semantic_quality_capture.v1",
        "responses_sha256": "0" * 64,
        "sample_count": 1,
    }
    sidecar = tmp_path / "semantic-quality.json"
    sidecar.write_text(json.dumps(capture), encoding="utf-8")
    deterministic = _write_deterministic(tmp_path, ["q1"])

    result = judge.cmd_report(
        argparse.Namespace(
            dataset="fixture",
            timestamp="run-1",
            responses=str(responses),
            deterministic=str(deterministic),
            judge=None,
            node_types=None,
            semantic_quality=str(sidecar),
            out=str(tmp_path / "report.json"),
        )
    )

    assert result == 1


def test_semantic_judge_rejects_missing_response_ids_before_network(tmp_path: Path) -> None:
    questions = tmp_path / "questions.yaml"
    questions.write_text(
        "questions:\n  - id: q1\n    question: What is known?\n    negative_control: true\n",
        encoding="utf-8",
    )
    responses = tmp_path / "responses.jsonl"
    responses.write_text(json.dumps({"question": "What is known?"}) + "\n", encoding="utf-8")

    result = semantic_judge.cmd_judge_file(
        argparse.Namespace(
            questions=str(questions),
            responses=str(responses),
            limit=0,
            base_url="http://127.0.0.1:1",
            model="auto",
            max_tokens=32,
            out=str(tmp_path / "judge.json"),
        )
    )

    assert result == 2
    assert not (tmp_path / "judge.json").exists()


def test_semantic_judge_skip_is_incomplete_and_cannot_become_arm(
    tmp_path: Path, monkeypatch
) -> None:
    questions = tmp_path / "questions.yaml"
    questions.write_text(
        "questions:\n  - id: q1\n    question: What is known?\n    negative_control: true\n",
        encoding="utf-8",
    )
    responses = tmp_path / "responses.jsonl"
    responses.write_text(json.dumps({"id": "q1", "ask": {}}) + "\n", encoding="utf-8")
    skipped = tmp_path / "judge.json"
    monkeypatch.setattr(semantic_judge, "_llm_models_reachable", lambda _url: False)

    result = semantic_judge.cmd_judge_file(
        argparse.Namespace(
            questions=str(questions),
            responses=str(responses),
            limit=0,
            base_url="http://127.0.0.1:1",
            model="auto",
            max_tokens=32,
            out=str(skipped),
        )
    )
    arm_result = semantic_judge.cmd_to_arm(
        argparse.Namespace(
            judge=str(skipped),
            out=str(tmp_path / "arm.jsonl"),
            partial_as_correct=False,
        )
    )

    assert result == 75
    assert arm_result == 2
    assert not (tmp_path / "arm.jsonl").exists()


def test_scripted_judge_rejects_unknown_response_ids_before_network(tmp_path: Path) -> None:
    questions = tmp_path / "questions.yaml"
    questions.write_text(
        "questions:\n  - id: q1\n    question: What is known?\n    negative_control: true\n",
        encoding="utf-8",
    )
    responses = tmp_path / "responses.jsonl"
    responses.write_text(json.dumps({"id": "other", "ask": {}}) + "\n", encoding="utf-8")

    result = judge.cmd_judge(
        argparse.Namespace(
            questions=str(questions),
            responses=str(responses),
            base_url="http://127.0.0.1:1",
            model="auto",
            max_tokens=32,
            out=str(tmp_path / "judge.json"),
        )
    )

    assert result == 1
    assert not (tmp_path / "judge.json").exists()


def test_scripted_judge_unreachable_is_incomplete(tmp_path: Path, monkeypatch) -> None:
    questions = tmp_path / "questions.yaml"
    questions.write_text(
        "questions:\n  - id: q1\n    question: What is known?\n    negative_control: true\n",
        encoding="utf-8",
    )
    responses = tmp_path / "responses.jsonl"
    responses.write_text(json.dumps({"id": "q1", "ask": {}}) + "\n", encoding="utf-8")
    output = tmp_path / "judge.json"
    monkeypatch.setattr(judge, "_llm_models_reachable", lambda _url: False)

    result = judge.cmd_judge(
        argparse.Namespace(
            questions=str(questions),
            responses=str(responses),
            base_url="http://127.0.0.1:1",
            model="auto",
            max_tokens=32,
            out=str(output),
        )
    )

    assert result == 75
    assert judge.judge_sidecar_errors(json.loads(output.read_text()))


def test_scripted_judge_all_unparseable_is_not_complete(tmp_path: Path, monkeypatch) -> None:
    questions = tmp_path / "questions.yaml"
    questions.write_text(
        "questions:\n  - id: q1\n    question: What is known?\n    negative_control: true\n",
        encoding="utf-8",
    )
    responses = tmp_path / "responses.jsonl"
    responses.write_text(json.dumps({"id": "q1", "ask": {}}) + "\n", encoding="utf-8")
    output = tmp_path / "judge.json"
    monkeypatch.setattr(judge, "_llm_models_reachable", lambda _url: True)
    monkeypatch.setattr(judge, "_llm_chat", lambda *_args, **_kwargs: "not a verdict")

    result = judge.cmd_judge(
        argparse.Namespace(
            questions=str(questions),
            responses=str(responses),
            base_url="http://example.test",
            model="fixed",
            max_tokens=32,
            out=str(output),
        )
    )

    assert result == 2
    assert json.loads(output.read_text())["complete"] is False


@pytest.mark.parametrize("transport", [judge, semantic_judge])
def test_judge_openai_transport_sends_managed_bearer_token(
    transport,
    monkeypatch,
) -> None:
    captured: list[tuple[str, str | None, str | None]] = []

    class FakeResponse:
        status = 200

        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self, *_args) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(request, *, timeout: float):
        captured.append(
            (
                request.full_url,
                request.get_header("Authorization"),
                request.get_header("Content-type"),
            )
        )
        if request.full_url.endswith("/models"):
            return FakeResponse({"data": [{"id": "desktop/qwen3.6-35b"}]})
        return FakeResponse({"choices": [{"message": {"content": "judge reply"}}]})

    monkeypatch.setenv("OPENAI_API_KEY", "managed-test-key")
    monkeypatch.setattr(transport.urllib.request, "urlopen", fake_urlopen)

    base_url = "http://gateway.test/v1"
    assert transport._llm_models_reachable(base_url) is True
    assert transport._select_chat_model(base_url) == "desktop/qwen3.6-35b"
    assert (
        transport._llm_chat(
            base_url,
            "desktop/qwen3.6-35b",
            "system",
            "user",
            max_tokens=32,
        )
        == "judge reply"
    )
    assert captured == [
        ("http://gateway.test/v1/models", "Bearer managed-test-key", None),
        ("http://gateway.test/v1/models", "Bearer managed-test-key", None),
        (
            "http://gateway.test/v1/chat/completions",
            "Bearer managed-test-key",
            "application/json",
        ),
    ]


def test_recall_floor_rejects_missing_baseline_metrics() -> None:
    with pytest.raises(ValueError, match="invalid recall baseline"):
        recall_floor.compare_to_baseline(
            {"k": 10, "hard_recall_at_k": {}, "extraction_completeness": {}},
            {"k": 10},
            tolerance=0,
        )


def test_recall_floor_rejects_baseline_for_different_k() -> None:
    baseline = json.loads(
        Path("tests/golden/datasets/synthetic-ci/recall_floor_baseline.json").read_text(
            encoding="utf-8"
        )
    )
    errors = recall_floor.baseline_schema_errors(baseline, expected_k=20)

    assert "baseline.k=10 differs from current k=20" in errors


def test_recall_floor_rejects_cli_k_that_differs_from_questions() -> None:
    with pytest.raises(ValueError, match="differs from questions settings.k=10"):
        recall_floor._load_questions(
            Path("tests/golden/datasets/synthetic-ci/questions.yaml"), requested_k=20
        )


def test_citation_floor_does_not_pass_without_citations() -> None:
    report = floor_metrics.verify_citation_bytes(
        [{"id": "q1", "ask": {"citations": [], "hits": []}}], None
    )

    assert report["citation_floor_pass"] is False
    assert report["hard_failures"] == ["no citations captured; citation floor is unmeasured"]


def test_panel_marks_item_incomplete_with_fewer_than_two_valid_votes() -> None:
    class FakeJudge:
        def __init__(self, family: str, payload: str) -> None:
            self.family = family
            self.payload = payload

        def __call__(self, _system: str, _user: str) -> str:
            return self.payload

    valid = json.dumps(
        {
            "correctness": 2,
            "groundedness": 2,
            "completeness": 2,
            "hallucination": False,
            "rationale": "ok",
        }
    )
    report = panel.run_panel(
        [
            {
                "id": "q1",
                "question": "What?",
                "ask_text": "Answer",
                "hits": [],
                "negative_control": False,
                "tier": "T1",
                "category": "lookup",
            }
        ],
        {"q1": {"expected_answer": "Answer", "gold_targets": []}},
        [FakeJudge("one", valid), FakeJudge("two", "not-json")],
        answerer_family=None,
        probe=False,
    )

    assert report["complete"] is False
    assert report["incomplete_items"] == ["q1"]
    assert report["panel"]["items_graded"] == 0
    assert report["per_item"][0]["valid_votes"] == 1
    assert panel.panel_report_errors(report)


def test_ab_runset_rejects_removed_run_both_mode() -> None:
    with pytest.raises(SystemExit, match="run_both is not supported"):
        scorecard.cmd_ab_runset(
            argparse.Namespace(
                config=json.dumps({"run_both": True}),
                questions=None,
                grader="proxy",
                alpha=0.05,
                material_pt=0.05,
                bootstrap=10,
                bootstrap_seed=1,
                json=True,
            )
        )


def test_run_golden_checks_yaml_conversion_and_no_longer_swallows_failures() -> None:
    script = Path("tests/golden/bin/run-golden.sh").read_text(encoding="utf-8")

    qjson_check = 'if ! QJSON="$(PY yaml2json --file "$QUESTIONS")"; then'
    assert qjson_check in script
    assert script.index(qjson_check) < script.index("# ── 1. settings from manifest")
    assert '--out "$RESULTS_DIR/deterministic.json" || true' not in script
    assert '--model "$JUDGE_MODEL" || true' not in script
    assert 'log "artifacts: $RESULTS_DIR"' in script
    assert "responses.complete" in script


def test_eval_run_preserves_complete_responses_when_a_later_sidecar_fails() -> None:
    script = Path("tests/golden/bin/eval-run.sh").read_text(encoding="utf-8")

    assert "golden_artifact_dir()" in script
    assert "golden_responses_complete()" in script
    assert "RG_STATUS=$?" in script
    assert "preserving it and resuming from the first missing sidecar" in script
    assert "rg_status=$?" in script


def test_run_golden_scopes_external_queue_and_explicit_ingest_modes_fail_closed() -> None:
    script = Path("tests/golden/bin/run-golden.sh").read_text(encoding="utf-8")
    orchestrator = Path("tests/golden/bin/eval-run.sh").read_text(encoding="utf-8")

    assert "--never-ingest requires --endpoint" in script
    assert "--force-reingest requires --endpoint" in script
    assert "--force-reingest requires --vault-path" in script
    assert "--force-reingest and --never-ingest are mutually exclusive" in script
    assert "--vault-path requires --endpoint" in script
    assert "X-Okto-Neuron-Vault: $OKTO_NEURON_VAULT_PATH" in script
    assert "endpoint is empty and --never-ingest forbids populating it" in script
    assert "d.get('enqueued_item_ids',[])" in script
    assert "wanted=json.loads(sys.argv[2])" in script
    assert "no ingest owned by this run — skipping queue wait" in script
    assert 'IDENTITY_BEFORE_REINGEST="$RESULTS_DIR/dataset-identity-before-reingest.json"' in script
    assert "refusing forced re-ingest" in script
    assert "get_json /api/v1/ingest-queue || echo '{}'" not in script
    assert "get_json /api/v1/node-types || echo '{}'" not in script
    assert '--connect-timeout "$GOLDEN_HTTP_CONNECT_TIMEOUT_S"' in script
    assert '--max-time "$timeout_s"' in script
    assert 'post_json /api/v1/recall "$recall_body" "$GOLDEN_RECALL_TIMEOUT_S"' in script
    assert 'post_json /api/v1/ask "$ask_body" "$GOLDEN_ASK_TIMEOUT_S"' in script
    assert 'post_json /api/v1/ingest "$body" "$GOLDEN_INGEST_TIMEOUT_S"' in script
    assert "python3 -c 'import time; print(time.monotonic_ns())'" in script
    assert "'schema_version':'golden_http_timing.v1'" in script
    assert "'recall_elapsed_ms':int(sys.argv[7])" in script
    assert "'ask_elapsed_ms':int(sys.argv[8])" in script
    assert "--never-ingest requires --endpoint" in orchestrator
    assert "--force-reingest requires --endpoint" in orchestrator
    assert "--force-reingest requires --vault-path" in orchestrator
    assert "--force-reingest and --never-ingest are mutually exclusive" in orchestrator
    assert "--vault-path requires --endpoint" in orchestrator
    assert "headers['X-Okto-Neuron-Vault'] = vault_path" in orchestrator
    assert "RG_ARGS+=(--never-ingest)" in orchestrator
    assert "RG_ARGS+=(--force-reingest)" in orchestrator

    for path in (
        "tests/golden/bin/run-golden.sh",
        "tests/golden/bin/eval-run.sh",
    ):
        completed = subprocess.run(
            ["bash", path, "_smoke", "--vault-path", "/vaults/explicit"],
            capture_output=True,
            check=False,
            text=True,
        )
        assert completed.returncode == 64
        assert "--vault-path requires --endpoint" in completed.stderr

    for path in (
        "tests/golden/bin/run-golden.sh",
        "tests/golden/bin/eval-run.sh",
    ):
        completed = subprocess.run(
            ["bash", path, "_smoke", "--force-reingest"],
            capture_output=True,
            check=False,
            text=True,
        )
        assert completed.returncode == 64
        assert "--force-reingest requires --endpoint" in completed.stderr


def test_eval_run_pins_and_routes_explicit_questions_in_every_mode() -> None:
    orchestrator = Path("tests/golden/bin/eval-run.sh").read_text(encoding="utf-8")

    assert "--questions is supported only with --ab-subgraph" not in orchestrator
    assert 'MF_ARGS+=("--questions" "$QUESTIONS_OVERRIDE")' in orchestrator
    assert 'FLOOR_ARGS+=("--questions" "$QUESTIONS_OVERRIDE")' in orchestrator
    assert 'RG_ARGS+=(--questions "$QUESTIONS_OVERRIDE")' in orchestrator
    assert 'rg_args+=(--questions "$QUESTIONS_OVERRIDE")' in orchestrator


def test_manifest_v4_pins_effective_question_bytes_count_and_k(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    inputs = dataset / "inputs"
    inputs.mkdir(parents=True)
    (inputs / "note.md").write_text("alpha target\n", encoding="utf-8")
    default_questions = dataset / "questions.yaml"
    default_questions.write_text(
        "settings:\n  k: 3\nquestions:\n  - id: default\n",
        encoding="utf-8",
    )
    override = tmp_path / "override.yaml"
    override.write_text(
        "settings:\n  k: 10\nquestions:\n  - id: one\n  - id: two\n",
        encoding="utf-8",
    )

    default = manifest.build_manifest(
        dataset,
        free_variable=None,
        repo=tmp_path,
    )
    selected = manifest.build_manifest(
        dataset,
        free_variable=None,
        repo=tmp_path,
        questions_path=override,
    )

    assert selected["manifest_version"] == 5
    assert selected["questions"] == {
        "sha256": f"sha256:{hashlib.sha256(override.read_bytes()).hexdigest()}",
        "bytes": len(override.read_bytes()),
        "count": 2,
    }
    assert selected["retrieval"]["k"] == 10
    parity = manifest.assert_arms(default, selected)
    assert parity["ok"] is False
    assert any("questions" in difference for difference in parity["diffs"])


def test_manifest_v5_hashes_explicit_vault_selector_without_echoing_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dataset = tmp_path / "dataset"
    inputs = dataset / "inputs"
    inputs.mkdir(parents=True)
    (inputs / "note.md").write_text("alpha\n", encoding="utf-8")
    (dataset / "questions.yaml").write_text(
        "settings:\n  k: 10\nquestions: []\n",
        encoding="utf-8",
    )
    selector = "/Users/operator/private-vault"
    monkeypatch.setenv("OKTO_NEURON_VAULT_PATH", selector)

    result = manifest.build_manifest(dataset, free_variable=None, repo=tmp_path)

    assert result["target"] == {
        "explicit_vault": True,
        "vault_selector_sha256": (f"sha256:{hashlib.sha256(selector.encode('utf-8')).hexdigest()}"),
    }
    assert selector not in json.dumps(result)


def test_manifest_v5_compares_isolated_construction_arms_on_the_declared_stage_only(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dataset = tmp_path / "dataset"
    inputs = dataset / "inputs"
    inputs.mkdir(parents=True)
    (inputs / "note.md").write_text("alpha target\n", encoding="utf-8")
    (dataset / "questions.yaml").write_text(
        "settings:\n  k: 10\nquestions: []\n",
        encoding="utf-8",
    )
    runtime = {
        "embedding": {
            "provider_ref": "gateway",
            "provider": "litellm_proxy",
            "model": "embed-model",
            "dimension": 2560,
            "batch_size": 32,
            "max_concurrent_batches": 1,
        },
        "llm": {
            "defaults": {
                "provider_ref": "gateway",
                "provider": "litellm_proxy",
                "model": "answer-model",
            },
            "extraction": {"max_concurrent": 4},
        },
        "consolidation": {
            "type_adjudication_enabled": True,
            "relation_curator_enabled": True,
            "curation_max_concurrent": 4,
            "curation_batch_size": 15,
        },
        "ingest": {"chunk_size_bytes": 12000, "chunk_overlap_bytes": 0},
    }
    monkeypatch.setenv("OKTO_NEURON_VAULT_PATH", "/vaults/all-on")
    enabled = manifest.build_manifest(
        dataset,
        free_variable="consolidation.type_adjudication_enabled",
        repo=tmp_path,
        runtime_config=runtime,
    )
    disabled_runtime = json.loads(json.dumps(runtime))
    disabled_runtime["consolidation"]["type_adjudication_enabled"] = False
    monkeypatch.setenv("OKTO_NEURON_VAULT_PATH", "/vaults/type-off")
    disabled = manifest.build_manifest(
        dataset,
        free_variable="consolidation.type_adjudication_enabled",
        repo=tmp_path,
        runtime_config=disabled_runtime,
    )

    assert enabled["manifest_version"] == 5
    assert enabled["execution"] == {
        "extraction_max_concurrent": 4,
        "embedding_batch_size": 32,
        "embedding_max_concurrent_batches": 1,
    }
    assert manifest.assert_arms(enabled, disabled)["ok"] is True

    disabled["consolidation"]["relation_curator_enabled"] = False
    parity = manifest.assert_arms(enabled, disabled)
    assert parity["ok"] is False
    assert any(
        "consolidation.relation_curator_enabled" in difference for difference in parity["diffs"]
    )


def test_manifest_v5_does_not_excuse_a_different_vault_for_read_path_arms(
    tmp_path: Path,
    monkeypatch,
) -> None:
    dataset = tmp_path / "dataset"
    inputs = dataset / "inputs"
    inputs.mkdir(parents=True)
    (inputs / "note.md").write_text("alpha\n", encoding="utf-8")
    (dataset / "questions.yaml").write_text(
        "settings:\n  k: 10\nquestions: []\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OKTO_NEURON_VAULT_PATH", "/vaults/a")
    arm_a = manifest.build_manifest(dataset, free_variable="retrieval.k", repo=tmp_path)
    monkeypatch.setenv("OKTO_NEURON_VAULT_PATH", "/vaults/b")
    arm_b = manifest.build_manifest(dataset, free_variable="retrieval.k", repo=tmp_path)

    parity = manifest.assert_arms(arm_a, arm_b)
    assert parity["ok"] is False
    assert any("target.vault_selector_sha256" in item for item in parity["diffs"])


def test_floor_uses_explicit_question_file(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    inputs = dataset / "inputs"
    inputs.mkdir(parents=True)
    (inputs / "note.md").write_text("unique alpha target\n", encoding="utf-8")
    (dataset / "questions.yaml").write_text(
        "questions:\n  - id: invalid-default\n",
        encoding="utf-8",
    )
    override = tmp_path / "override.yaml"
    override.write_text(
        "settings:\n"
        "  k: 10\n"
        "questions:\n"
        "  - id: valid\n"
        "    question: where\n"
        "    expected_answer: alpha\n"
        "    expected_source_paths: [note.md]\n"
        "    must_contain: [alpha]\n"
        "    gold_targets:\n"
        "      - source_path: note.md\n"
        "        quote: unique alpha target\n",
        encoding="utf-8",
    )
    output = tmp_path / "floor.json"

    exit_code = judge.cmd_floor(
        argparse.Namespace(
            dataset=str(dataset),
            questions=str(override),
            out=str(output),
            baseline=None,
        )
    )

    assert exit_code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["provenance_gate_pass"] is True
    assert report["questions"] == 1


def test_standalone_semantic_judge_returns_typed_yaml_error(tmp_path: Path) -> None:
    questions = tmp_path / "broken.yaml"
    questions.write_text('questions: ["\n', encoding="utf-8")
    responses = tmp_path / "responses.jsonl"
    responses.write_text(json.dumps({"id": "q1"}) + "\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(Path(semantic_judge.__file__).resolve()),
            "judge-file",
            "--questions",
            str(questions),
            "--responses",
            str(responses),
            "--out",
            str(tmp_path / "judge.json"),
            "--base-url",
            "http://127.0.0.1:1",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stderr)["error"] == "golden_yaml_error"


def test_standalone_sweep_returns_typed_yaml_error(tmp_path: Path) -> None:
    questions = tmp_path / "broken.yaml"
    questions.write_text('questions: ["\n', encoding="utf-8")
    matrix = tmp_path / "matrix.json"
    matrix.write_text(
        json.dumps(
            {
                "questions": str(questions),
                "cells": [
                    {"label": "a", "source": "run", "out": str(tmp_path / "a.jsonl")},
                    {"label": "b", "source": "run", "out": str(tmp_path / "b.jsonl")},
                ],
            }
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [sys.executable, str(Path(sweep.__file__).resolve()), "run", "--matrix", str(matrix)],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 2
    assert json.loads(completed.stderr)["error"] == "golden_yaml_error"


def test_eval_resume_revalidates_dataset_floor_and_uses_reachable_real_verdict() -> None:
    script = Path("tests/golden/bin/eval-run.sh").read_text(encoding="utf-8")

    assert 'dataset_floor_valid "$FLOOR_OUT" "$AB_SUBGRAPH"' in script
    assert 'if [[ "$AB_SUBGRAPH" != "1" && "$FLOOR_PASS" != "True" ]]' in script
    assert script.count("grep -q '^  VERDICT: REAL improvement'") == 2
    assert "classification[^A-Za-z]*REAL" not in script


def test_semantic_report_validation_applies_the_same_limit_as_judging(tmp_path: Path) -> None:
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        json.dumps({"id": "q1"}) + "\n" + json.dumps({"id": "q2"}) + "\n",
        encoding="utf-8",
    )
    report = tmp_path / "semantic-judge.json"
    report.write_text(
        json.dumps(
            {
                "complete": True,
                "skipped": False,
                "verdicts": [{"id": "q1", "verdict": "CORRECT"}],
            }
        ),
        encoding="utf-8",
    )

    limited = semantic_judge.cmd_validate_report(
        argparse.Namespace(
            judge=str(report),
            responses=str(responses),
            limit=1,
        )
    )
    unlimited = semantic_judge.cmd_validate_report(
        argparse.Namespace(
            judge=str(report),
            responses=str(responses),
            limit=0,
        )
    )

    assert limited == 0
    assert unlimited == 2


def test_floor_node_probe_propagates_auth_and_does_not_cache_transport_failure(
    monkeypatch,
) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self, *_args) -> bytes:
            return b'{"node":{"id":"n1"}}'

    captured: list[tuple[str | None, str | None]] = []

    def succeed(request, *, timeout: float):
        assert timeout == 10.0
        headers = {key.lower(): value for key, value in request.header_items()}
        captured.append(
            (
                request.get_header("Authorization"),
                headers.get("x-okto-neuron-vault"),
            )
        )
        return FakeResponse()

    floor_metrics._NODE_CACHE.clear()
    monkeypatch.setenv("OKTO_NEURON_AUTH_TOKEN", "floor-token")
    monkeypatch.setenv("OKTO_NEURON_VAULT_PATH", "/vaults/floor")
    monkeypatch.setattr(floor_metrics.urllib.request, "urlopen", succeed)

    assert floor_metrics._get_node("http://example.test", "n1") == {"node": {"id": "n1"}}
    assert captured == [("Bearer floor-token", "/vaults/floor")]

    calls = 0

    def fail(_request, *, timeout: float):
        nonlocal calls
        calls += 1
        raise urllib.error.URLError("temporary outage")

    monkeypatch.setattr(floor_metrics.urllib.request, "urlopen", fail)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="temporary outage"):
            floor_metrics._get_node("http://example.test", "transient")

    assert calls == 2
    assert (
        "http://example.test",
        "/vaults/floor",
        "transient",
    ) not in floor_metrics._NODE_CACHE
    floor_metrics._NODE_CACHE.clear()


def test_floor_laptop_validation_accepts_stable_sorted_question_order() -> None:
    report = {
        "report": "floor_laptop",
        "citation_byte_verification": {
            "citation_floor_pass": True,
            "per_question": [{"id": "q1"}, {"id": "q2"}],
        },
    }

    assert (
        floor_metrics.floor_laptop_report_errors(report, expected_response_ids=["q2", "q1"]) == []
    )


def test_recall_floor_probe_failure_is_typed_infrastructure_error(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "graph.lbug").write_bytes(b"not-a-real-graph")
    monkeypatch.setattr(
        recall_floor,
        "run_probe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("corrupt graph")),
    )

    result = recall_floor.cmd_gate(
        argparse.Namespace(
            vault=str(vault),
            questions="tests/golden/datasets/_smoke/questions.yaml",
            k=10,
            baseline=None,
            tolerance=0.0,
            rate_tolerance=0.0,
            out=str(tmp_path / "recall.json"),
        )
    )

    assert result == 2
    assert not (tmp_path / "recall.json").exists()


# ── S1 harness trust floor: venv provenance + sealed api_key_env ────────────

_BIN_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BIN_DIR.parents[2]


def _call_guard(*, venv_dir: str, path_prefix: str | None = None) -> subprocess.CompletedProcess:
    """Invoke golden_assert_repo_binary from _serve.sh in a real bash shell."""
    import os

    env = dict(os.environ)
    if path_prefix:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    env.pop("UV_PROJECT_ENVIRONMENT", None)
    return subprocess.run(
        [
            "bash",
            "-c",
            f'source "{_BIN_DIR}/_serve.sh"; golden_assert_repo_binary "{venv_dir}" "{_REPO_ROOT}"',
        ],
        capture_output=True,
        text=True,
        env=env,
    )


def _derive_venv() -> str:
    proc = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{_BIN_DIR}/_serve.sh"; golden_resolve_env "{_REPO_ROOT}"',
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        pytest.skip("uv-managed environment not derivable in this context")
    return proc.stdout.strip()


def test_venv_guard_rejects_a_marginalia_from_outside_the_managed_env(
    tmp_path: Path,
) -> None:
    """The global-tool hazard: a decoy `okto-neuron` earlier on PATH must abort."""
    venv_dir = _derive_venv()
    decoy_dir = tmp_path / "decoy-bin"
    decoy_dir.mkdir()
    decoy = decoy_dir / "okto-neuron"
    decoy.write_text("#!/bin/sh\necho 0.0.1\n")
    decoy.chmod(0o755)

    proc = _call_guard(venv_dir=venv_dir, path_prefix=str(decoy_dir))

    assert proc.returncode != 0
    assert "provenance check FAILED" in proc.stderr
    assert "outside the uv-managed env" in proc.stderr
    assert str(decoy) in proc.stderr  # the diagnostic names the decoy


def test_venv_guard_accepts_the_uv_managed_binary() -> None:
    venv_dir = _derive_venv()
    if not (Path(venv_dir) / "bin" / "marginalia").exists():
        pytest.skip("managed env has no marginalia console script")

    proc = _call_guard(venv_dir=venv_dir, path_prefix=f"{venv_dir}/bin")

    assert proc.returncode == 0, proc.stderr


def _sealed_llm_writer_source() -> str:
    """Extract the exact sealed-mode YAML writer snippet shipped in run-golden.sh."""
    text = (_BIN_DIR / "run-golden.sh").read_text()
    marker = '"$SEALED_API_KEY_ENV" <<' + "'PY'\n"
    start = text.index(marker) + len(marker)
    end = text.index("\nPY\n", start)
    return text[start:end]


@pytest.mark.parametrize("name", ["", "OKTO_NEURON_PROVIDER_OPENAI_API_KEY"])
def test_sealed_writer_emits_api_key_env_name_only_and_validates(tmp_path: Path, name: str) -> None:
    import yaml as _yaml

    cfg_path = tmp_path / "okto-neuron.yaml"
    cfg_path.write_text("{}\n")
    snippet = tmp_path / "writer.py"
    snippet.write_text(_sealed_llm_writer_source())

    proc = subprocess.run(
        [
            sys.executable,
            str(snippet),
            str(cfg_path),
            "http://127.0.0.1:9/v1",
            "some-model",
            name,
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr

    written = _yaml.safe_load(cfg_path.read_text())
    defaults = written["llm"]["defaults"]
    if name:
        assert defaults["api_key_env"] == name
    else:
        assert "api_key_env" not in defaults
    # The NAME may be written; a VALUE never may be.
    assert "sk-" not in cfg_path.read_text()

    # Round-trip through the real strict (extra="forbid") config model.
    from okto_neuron.config._vault import LLMConfig

    resolved = LLMConfig.model_validate(written["llm"])
    assert resolved.defaults.api_key_env == (name or None)


def test_sealed_writer_rejects_an_out_of_namespace_api_key_env_name(
    tmp_path: Path,
) -> None:
    """The harness pre-validates, but the config model is the backstop."""
    from okto_neuron.config._vault import LLMConfig

    with pytest.raises(Exception) as exc:
        LLMConfig.model_validate(
            {
                "allow_remote": False,
                "defaults": {
                    "provider": "openai",
                    "api_base": "http://127.0.0.1:9/v1",
                    "model": "m",
                    "api_key_env": "OPENAI_API_KEY",
                },
            }
        )
    assert "OKTO_NEURON_" in str(exc.value)


def test_venv_guard_rejects_a_base_interpreter_prefix(tmp_path: Path) -> None:
    """A prefix with no pyvenv.cfg is a system/pyenv install, not a managed env."""
    fake = tmp_path / "not-a-venv"
    (fake / "bin").mkdir(parents=True)

    proc = _call_guard(venv_dir=str(fake))

    assert proc.returncode != 0
    assert "not a virtualenv (no pyvenv.cfg)" in proc.stderr
