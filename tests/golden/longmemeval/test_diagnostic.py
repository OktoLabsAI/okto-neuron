from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import time
from urllib import error

import pytest

from okto_neuron.semantic_acceptance import _validate_public_diagnostic


_DIAGNOSTIC_PATH = Path(__file__).with_name("diagnostic.py")
_SPEC = importlib.util.spec_from_file_location("longmemeval_diagnostic", _DIAGNOSTIC_PATH)
assert _SPEC is not None and _SPEC.loader is not None
diagnostic = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(diagnostic)


def test_upstream_session_corpus_preserves_user_only_and_answer_labels() -> None:
    corpus, ids = diagnostic._upstream_session_corpus(
        {
            "haystack_session_ids": ["answer-a", "answer-b", "filler"],
            "haystack_sessions": [
                [
                    {"role": "user", "content": "kept", "has_answer": True},
                    {"role": "assistant", "content": "excluded"},
                ],
                [{"role": "user", "content": "not evidence", "has_answer": False}],
                [{"role": "user", "content": "ordinary"}],
            ],
        }
    )

    assert corpus == ["kept", "not evidence", "ordinary"]
    assert ids == ["answer-a", "noans-b", "filler"]


def test_ranking_metrics_match_pinned_shifted_dcg() -> None:
    metrics = diagnostic._ranking_metrics([2, 0, 1], {"a", "c"}, ["a", "b", "c"])

    assert metrics["recall_any@1"] == 1.0
    assert metrics["recall_all@1"] == 0.0
    assert metrics["recall_all@3"] == 1.0
    # The pinned upstream dcg weights both rank zero and rank one as 1.
    assert metrics["ndcg_any@3"] == 1.0


def test_wait_for_ingest_tracks_only_owned_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        diagnostic,
        "_api_json",
        lambda *_args, **_kwargs: {
            "items": [
                {"id": "owned-a", "status": "done"},
                {"id": "owned-b", "status": "done"},
                {"id": "historical-error", "status": "error", "error": "old"},
            ]
        },
    )
    diagnostic._wait_for_ingest(
        "http://127.0.0.1:7777",
        "/vault",
        ["owned-a", "owned-b"],
        timeout_s=1,
        heartbeat_s=1,
    )


def test_wait_for_ingest_survives_transient_status_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: list[object] = [
        diagnostic.TransientApiError("GET /api/v1/ingest-queue failed: timed out"),
        {"items": [{"id": "owned", "status": "done"}]},
    ]

    def api(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(diagnostic, "_api_json", api)
    monkeypatch.setattr(diagnostic.time, "sleep", lambda _seconds: None)

    diagnostic._wait_for_ingest(
        "http://127.0.0.1:7777",
        "/vault",
        ["owned"],
        timeout_s=1,
        heartbeat_s=1,
    )


def test_api_json_classifies_transport_failure_as_transient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def urlopen(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise error.URLError("timed out")

    monkeypatch.setattr(diagnostic.request, "urlopen", urlopen)

    with pytest.raises(diagnostic.TransientApiError, match="timed out"):
        diagnostic._api_json("http://127.0.0.1:7777", "/api/v1/ingest-queue")


def _measured_cost(*, completions: int, input_tokens: int, output_tokens: int) -> dict:
    return {
        "schema_version": "construction_cost.v1",
        "status": "measured",
        "embedding_calls": 0,
        "embedding_calls_with_usage": 0,
        "embedding_inputs": 0,
        "completion_calls": completions,
        "completion_calls_with_usage": completions,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def test_reconciliation_and_cost_track_only_owned_unique_jobs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    shared = "reconcile-propose-shared"
    items = [
        {
            "id": item_id,
            "status": "done",
            "outcome": {
                "construction_cost": _measured_cost(
                    completions=1,
                    input_tokens=10,
                    output_tokens=2,
                ),
                "cross_document_reconciliation": {
                    "state": "complete",
                    "job_id": shared,
                },
            },
        }
        for item_id in ("owned-a", "owned-b")
    ]
    jobs = [
        {
            "id": shared,
            "status": "done",
            "progress": "adjudicating 3/3",
            "result": {
                "outcome": {"state": "complete"},
                "construction_cost": _measured_cost(
                    completions=3,
                    input_tokens=30,
                    output_tokens=6,
                ),
            },
        },
        {"id": "historical-error", "status": "error", "error": "old"},
    ]

    def api(_endpoint: str, path: str, **_kwargs):  # type: ignore[no-untyped-def]
        return {"jobs": jobs} if path.startswith("/api/v1/curation/jobs") else {"items": items}

    monkeypatch.setattr(diagnostic, "_api_json", api)

    job_ids = diagnostic._reconciliation_job_ids(
        "http://127.0.0.1:7777", "/vault", ["owned-a", "owned-b"]
    )
    assert job_ids == [shared]
    diagnostic._wait_for_reconciliation(
        "http://127.0.0.1:7777",
        "/vault",
        job_ids,
        timeout_s=1,
        heartbeat_s=1,
    )
    out = tmp_path / "construction" / "case.json"
    diagnostic._capture_construction_cost(
        "http://127.0.0.1:7777",
        "/vault",
        ["owned-a", "owned-b"],
        job_ids,
        out,
    )

    artifact = diagnostic._load_json(out)
    assert artifact["owned_reconciliation_job_ids"] == [shared]
    assert artifact["totals"]["completion_calls"] == 5
    assert artifact["totals"]["input_tokens"] == 50
    assert artifact["totals"]["output_tokens"] == 10


def test_wait_for_reconciliation_survives_transient_status_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses: list[object] = [
        diagnostic.TransientApiError("GET /api/v1/curation/jobs failed: timed out"),
        [
            {
                "id": "owned-job",
                "status": "done",
                "result": {"outcome": {"state": "complete"}},
            }
        ],
    ]

    def rows(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(diagnostic, "_curation_job_rows", rows)
    monkeypatch.setattr(diagnostic.time, "sleep", lambda _seconds: None)

    diagnostic._wait_for_reconciliation(
        "http://127.0.0.1:7777",
        "/vault",
        ["owned-job"],
        timeout_s=1,
        heartbeat_s=1,
    )


def test_partial_construction_cost_fails_closed() -> None:
    partial = _measured_cost(completions=1, input_tokens=1, output_tokens=1)
    partial["status"] = "partial"

    with pytest.raises(diagnostic.DiagnosticError, match="is partial"):
        diagnostic._validated_cost(partial, "probe")


def test_identity_check_creates_artifact_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "missing" / "identity.json"

    def run_process(command, **_kwargs):  # type: ignore[no-untyped-def]
        assert out.parent.is_dir()
        assert command[-1] == str(out)
        return []

    monkeypatch.setattr(diagnostic, "_run_process", run_process)
    diagnostic._identity_check(
        "http://127.0.0.1:7777",
        "/vault",
        tmp_path / "case",
        out,
        tmp_path / "case.log",
        heartbeat_s=1,
    )


def test_state_vault_prefix_is_durable_and_cannot_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = tmp_path / "reproducibility-manifest.json"
    manifest_path.write_text("{}\n", encoding="utf-8")
    cases = [
        {
            "case_id": "case-a",
            "question_id": "question-a",
            "selection_bucket": "bucket-a",
        }
    ]
    monkeypatch.setattr(diagnostic, "_case_rows", lambda *_args: cases)

    state = diagnostic._initial_state(
        tmp_path,
        {},
        vault_prefix="adr0040-lme-measured",
    )
    assert state["vault_prefix"] == "adr0040-lme-measured"
    assert state["cases"]["case-a"]["vault_name"] == "adr0040-lme-measured-case-a"

    state_path = tmp_path / "state.json"
    diagnostic._write_json(state_path, state)
    loaded = diagnostic._load_state(
        state_path,
        tmp_path,
        {},
        vault_prefix="adr0040-lme-measured",
    )
    assert loaded["vault_prefix"] == "adr0040-lme-measured"
    with pytest.raises(diagnostic.DiagnosticError, match="vault prefix"):
        diagnostic._load_state(
            state_path,
            tmp_path,
            {},
            vault_prefix="adr0040-lme-other",
        )


def test_case_selection_is_exact_ordered_and_rejects_unknown_ids() -> None:
    cases = [
        {"case_id": "case-a"},
        {"case_id": "case-b"},
        {"case_id": "case-c"},
    ]

    assert diagnostic._select_case_rows(cases, ["case-c", "case-a"], None) == [
        {"case_id": "case-a"},
        {"case_id": "case-c"},
    ]
    assert diagnostic._select_case_rows(cases, None, 2) == cases[:2]
    with pytest.raises(diagnostic.DiagnosticError, match="case-missing"):
        diagnostic._select_case_rows(cases, ["case-missing"], None)


def test_run_parser_accepts_isolated_vault_prefix_and_case_id() -> None:
    args = diagnostic._parser().parse_args(
        [
            "run-marginalia",
            "--materialized",
            "/materialized",
            "--run-root",
            "/run",
            "--vault-prefix",
            "adr0040-lme-measured",
            "--case-id",
            "case-a",
            "--case-id",
            "case-b",
        ]
    )

    assert args.vault_prefix == "adr0040-lme-measured"
    assert args.case_id == ["case-a", "case-b"]


def test_effective_runtime_enforces_serial_embedding_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {
        "llm": {
            "defaults": {
                "provider": "litellm_proxy",
                "model": "desktop/qwen",
                "api_base": "http://127.0.0.1:4000",
                "api_key_env": "OKTO_NEURON_KEY",
            }
        },
        "embedding": {
            "provider": "litellm_proxy",
            "model": "desktop/embed",
            "dimension": 2560,
            "batch_size": 32,
            "max_concurrent_batches": 4,
        },
    }
    monkeypatch.setattr(diagnostic, "_api_json", lambda *_args, **_kwargs: config)

    with pytest.raises(diagnostic.DiagnosticError, match="max_concurrent_batches=1"):
        diagnostic._effective_runtime("http://127.0.0.1:7777", "/vault")


def test_silent_process_is_killed_at_the_hard_timeout(tmp_path: Path) -> None:
    started = time.monotonic()

    with pytest.raises(diagnostic.DiagnosticError, match="exceeded"):
        diagnostic._run_process(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            env={},
            log_path=tmp_path / "silent.log",
            timeout_s=0.2,
            heartbeat_s=0.05,
        )

    assert time.monotonic() - started < 2


def test_http_timing_is_read_from_the_client_measurement() -> None:
    response = {
        "timing": {
            "schema_version": "golden_http_timing.v1",
            "recall_elapsed_ms": 12,
            "ask_elapsed_ms": 34,
        },
        "recall": {"recall_cost": {"latency_ms": 999}},
    }

    assert diagnostic._latency_ms(response) == 12
    assert diagnostic._ask_latency_ms(response) == 34


def test_public_receipt_matches_semantic_acceptance_contract(tmp_path: Path) -> None:
    materialized = tmp_path / "materialized"
    materialized.mkdir()
    (materialized / "reproducibility-manifest.json").write_text("{}\n", encoding="utf-8")
    artifacts = {}
    for name in ("retrieval", "rag", "bm25"):
        path = tmp_path / f"{name}.json"
        path.write_text(
            '{"status":"measured","case_count":35}\n',
            encoding="utf-8",
        )
        artifacts[name] = path
    out = tmp_path / "receipt.json"
    args = type(
        "Args",
        (),
        {
            "materialized": materialized,
            "marginalia_retrieval": artifacts["retrieval"],
            "direct_rag": artifacts["rag"],
            "flat_bm25": artifacts["bm25"],
            "out": out,
        },
    )()

    assert diagnostic.receipt_command(args) == 0
    receipt = diagnostic._load_json(out)
    assert "schema_version" not in receipt
    assert _validate_public_diagnostic(receipt)["status"] == "measured"
