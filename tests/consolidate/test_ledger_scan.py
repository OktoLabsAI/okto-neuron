"""Lossless candidate-ledger evidence scanning for ADR 0040 Phase 1a."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from okto_neuron.consolidate import NodeCandidate
from okto_neuron.consolidate.ledger import CandidateLedger, extraction_unit_id
from okto_neuron.semantic_surface import build_surface_record


def _row(version: int, kind: str) -> bytes:
    return json.dumps({"ledger_version": version, "kind": kind}).encode("utf-8")


def _dead_letter_operation(candidate_id: str = "edge-1") -> dict[str, object]:
    return {
        "operation": "dead_letter",
        "candidate_kind": "edge",
        "candidate_id": candidate_id,
        "candidate": {
            "type": "mentions",
            "src_ref": "node-1",
            "dst_ref": "node-2",
        },
        "reason": "test_fixture",
    }


def _receipt_only_operation(ledger: CandidateLedger, plan_id: str) -> None:
    plan = next(plan for plan in ledger.unreceipted_commit_plans() if plan.plan_id == plan_id)
    operation = plan.operations[0]
    ledger.record_operation_receipt(
        plan.run_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        operation_id=str(operation["operation_id"]),
        operation=str(operation["operation"]),
        status="dead_lettered",
        result={"candidate_id": operation["candidate_id"]},
    )


def test_scan_reports_absent_ledger(tmp_path: Path) -> None:
    result = CandidateLedger(tmp_path).scan()

    assert result.completeness_status == "absent"
    assert result.completeness_reason == "ledger_file_absent"
    assert result.parsed_records == ()
    assert result.file_sha256 is None


def test_surface_evidence_survives_candidate_ledger_round_trip(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    candidate = NodeCandidate(
        type="Agent",
        title="Café",
        surface=build_surface_record("Cafe\u0301", "Café"),
    )
    run_id = ledger.start_run(
        document_id="doc",
        source="source.md",
        blocks_total=1,
        model="fixture",
    )

    ledger.record_candidate(
        run_id,
        candidate_id=candidate.candidate_id,
        candidate_kind="node",
        state="proposed",
        payload=candidate.model_dump(mode="json"),
    )

    record = next(row for row in ledger.scan().parsed_records if row["kind"] == "candidate")
    assert record["payload"]["surface"] == candidate.surface.model_dump(mode="json")


def test_post_write_integrity_outcome_closes_finished_run_summary(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    run_id = ledger.start_run(
        document_id="doc",
        source="source.md",
        blocks_total=1,
        model="fixture",
    )
    ledger.finish_run(
        run_id,
        state="completed",
        summary={
            "outcome": {
                "quality": "complete",
                "integrity": {"status": "pending_post_write_audit", "audit_id": None},
            }
        },
    )
    integrity = {
        "status": "verified",
        "audit_id": "audit-1",
        "graph_generation": "generation-1",
    }

    ledger.record_integrity_outcome(
        run_id,
        document_id="doc",
        integrity=integrity,
    )

    summary = ledger.run_summaries(limit=1)[0]
    assert summary["summary"]["outcome"]["quality"] == "complete"
    assert summary["summary"]["outcome"]["integrity"] == integrity
    assert summary["integrity"] == integrity
    detail = ledger.run_detail(run_id)
    assert detail is not None
    assert detail["integrity_outcomes"][-1]["integrity"] == integrity


def test_scan_hashes_full_file_and_reports_versions_and_line_scope(tmp_path: Path) -> None:
    data = _row(1, "ingest_run") + b"\n\n" + _row(2, "candidate") + b"\n"
    ledger = CandidateLedger(tmp_path)
    ledger.path.write_bytes(data)

    result = ledger.scan()

    assert result.completeness_status == "complete"
    assert result.completeness_reason == "all_nonempty_lines_parsed"
    assert result.total_lines == 3
    assert result.nonempty_lines == 2
    assert result.parsed_record_count == 2
    assert result.ledger_versions == (1, 2)
    assert result.file_size_bytes == len(data)
    assert result.file_sha256 == hashlib.sha256(data).hexdigest()
    assert result.malformed_line_count == 0
    assert result.trailing_partial is False


def test_scan_keeps_valid_records_and_bounds_malformed_line_evidence(tmp_path: Path) -> None:
    valid = _row(2, "candidate")
    data = b"\n".join((valid, b"not json", b"[]", b"{broken", b""))
    ledger = CandidateLedger(tmp_path)
    ledger.path.write_bytes(data)

    result = ledger.scan(max_malformed_samples=2)

    assert result.completeness_status == "incomplete"
    assert result.completeness_reason == "malformed_ledger_lines"
    assert result.parsed_records == ({"ledger_version": 2, "kind": "candidate"},)
    assert result.malformed_line_count == 3
    assert [(row.line_number, row.reason) for row in result.malformed_lines] == [
        (2, "invalid_json"),
        (3, "record_not_object"),
    ]
    assert result.malformed_samples_truncated is True
    assert result.trailing_partial is False
    # The compatibility reader still returns the readable object rows only.
    assert ledger.records() == list(result.parsed_records)


def test_scan_identifies_interrupted_final_append(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.path.write_bytes(_row(2, "ingest_run") + b"\n" + b'{"ledger_version":2')

    result = ledger.scan()

    assert result.completeness_status == "incomplete"
    assert result.completeness_reason == ("malformed_ledger_lines+trailing_partial_record")
    assert result.unterminated_final_line is True
    assert result.trailing_partial is True
    assert result.trailing_partial_line_number == 2
    assert result.malformed_line_count == 1


def test_scan_does_not_mislabel_terminated_malformed_line_before_blank_tail(
    tmp_path: Path,
) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.path.write_bytes(_row(2, "candidate") + b"\nbad-json\n   ")

    result = ledger.scan()

    assert result.completeness_status == "incomplete"
    assert result.completeness_reason == "malformed_ledger_lines"
    assert result.unterminated_final_line is True
    assert result.trailing_partial is False
    assert result.trailing_partial_line_number is None


def test_scan_does_not_call_parseable_unterminated_json_partial(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.path.write_bytes(_row(2, "candidate"))

    result = ledger.scan()

    assert result.completeness_status == "complete"
    assert result.unterminated_final_line is True
    assert result.trailing_partial is False


def test_scan_reports_invalid_utf8_without_losing_following_records(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.path.write_bytes(b'{"ledger_version":2,"title":"\xff"}\n' + _row(2, "candidate") + b"\n")

    result = ledger.scan()

    assert result.completeness_status == "incomplete"
    assert result.malformed_line_count == 1
    assert result.malformed_lines[0].reason == "invalid_utf8"
    assert result.parsed_record_count == 1


def test_scan_marks_unrecognized_record_versions_incomplete(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.path.write_text(
        '{"kind":"candidate"}\n{"kind":"candidate","ledger_version":"2"}\n',
        encoding="utf-8",
    )

    result = ledger.scan()

    assert result.parsed_record_count == 2
    assert result.ledger_versions == ()
    assert result.unrecognized_version_record_count == 2
    assert result.completeness_status == "incomplete"
    assert result.completeness_reason == "unrecognized_ledger_versions"


def test_scan_rejects_unknown_future_integer_version(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.path.write_text(
        '{"kind":"candidate","ledger_version":999}\n',
        encoding="utf-8",
    )

    result = ledger.scan()

    assert result.ledger_versions == (999,)
    assert result.unrecognized_version_record_count == 1
    assert result.completeness_status == "incomplete"
    assert result.completeness_reason == "unrecognized_ledger_versions"


def test_scan_marks_individual_sample_truncation(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.path.write_text("x" * 500 + "\n", encoding="utf-8")

    result = ledger.scan()

    assert len(result.malformed_lines[0].sample) == 200
    assert result.malformed_lines[0].sample_truncated is True


def test_append_does_not_allow_payload_to_override_ledger_version(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)

    ledger.append("ingest_run", ledger_version=999)

    assert ledger.scan().ledger_versions == (2,)


def test_operational_offset_index_is_reused_and_extended_across_instances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron.consolidate import ledger as ledger_module

    ledger = CandidateLedger(tmp_path)
    ledger.append(
        "extraction_unit",
        document_id="doc",
        extraction_fingerprint="policy",
        unit_id="one",
        status="succeeded",
        result={"nodes": [], "edges": []},
    )
    builds = 0
    original_build = ledger_module._build_ledger_offset_index

    def counted_build(directory: Path, path: Path):  # type: ignore[no-untyped-def]
        nonlocal builds
        builds += 1
        return original_build(directory, path)

    monkeypatch.setattr(ledger_module, "_build_ledger_offset_index", counted_build)
    first = CandidateLedger(tmp_path).successful_extraction_units(
        document_id="doc",
        extraction_fingerprint="policy",
    )
    assert set(first) == {"one"}
    assert builds == 1

    CandidateLedger(tmp_path).append(
        "extraction_unit",
        document_id="doc",
        extraction_fingerprint="policy",
        unit_id="two",
        status="succeeded",
        result={"nodes": [], "edges": []},
    )
    second = CandidateLedger(tmp_path).successful_extraction_units(
        document_id="doc",
        extraction_fingerprint="policy",
    )
    assert set(second) == {"one", "two"}
    assert builds == 1


def test_extraction_unit_identity_includes_span_and_fingerprint() -> None:
    base = {
        "block_id": "block",
        "content_hash": "same-content",
        "extraction_fingerprint": "policy-a",
    }

    first = extraction_unit_id(byte_start=10, byte_end=20, **base)
    same = extraction_unit_id(byte_start=10, byte_end=20, **base)
    other_span = extraction_unit_id(byte_start=30, byte_end=40, **base)
    other_policy = extraction_unit_id(
        byte_start=10,
        byte_end=20,
        **{**base, "extraction_fingerprint": "policy-b"},
    )

    assert first == same
    assert len({first, other_span, other_policy}) == 3


def test_successful_extraction_unit_is_durable_lightweight_and_replayable(
    tmp_path: Path,
) -> None:
    ledger = CandidateLedger(tmp_path)
    identity = {
        "block_id": "block",
        "byte_start": 0,
        "byte_end": 12,
        "content_hash": "content-hash",
        "extraction_fingerprint": "extract-v1",
    }
    unit_id = extraction_unit_id(**identity)
    ledger.record_extraction_unit(
        "run",
        document_id="doc",
        unit_id=unit_id,
        source_path="/vault/note.md",
        attempt=1,
        status="succeeded",
        result={
            "nodes": [
                {
                    "type": "Concept",
                    "title": "Durable unit",
                    "content": "",
                    "embedding": [1.0, 2.0],
                }
            ],
            "edges": [],
        },
        **identity,
    )

    replay = ledger.successful_extraction_units(
        document_id="doc",
        extraction_fingerprint="extract-v1",
    )
    record = replay[unit_id]
    assert record["result"]["nodes"][0]["embedding_dim"] == 2
    assert "embedding" not in record["result"]["nodes"][0]
    assert "text" not in record
    assert ledger.path.read_bytes().endswith(b"\n")


def test_torn_extraction_unit_is_not_replay_evidence(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_bytes(b'{"kind":"extraction_unit","ledger_version":2')

    assert (
        ledger.successful_extraction_units(
            document_id="doc",
            extraction_fingerprint="extract-v1",
        )
        == {}
    )
    assert ledger.unreceipted_commit_plans() == ()
    scan = ledger.scan()
    assert scan.completeness_status == "complete"
    assert scan.parsed_records == ()


def test_newest_successful_payload_wins_for_same_stochastic_unit(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    identity = {
        "block_id": "block-1",
        "byte_start": 0,
        "byte_end": 12,
        "content_hash": "a" * 64,
        "extraction_fingerprint": "sha256:" + "b" * 64,
    }
    unit_id = extraction_unit_id(**identity)
    for title in ("First draw", "Second draw"):
        ledger.record_extraction_unit(
            "run-1",
            document_id="doc-1",
            unit_id=unit_id,
            source_path="/vault/source.md",
            attempt=1,
            status="succeeded",
            result={"nodes": [{"type": "Concept", "title": title}], "edges": []},
            **identity,
        )

    replay = ledger.successful_extraction_units(
        document_id="doc-1",
        extraction_fingerprint=str(identity["extraction_fingerprint"]),
    )

    assert replay[unit_id]["result"]["nodes"][0]["title"] == "Second draw"


def test_writer_realistic_torn_extraction_tail_is_repaired_before_next_append(
    tmp_path: Path,
) -> None:
    seed = CandidateLedger(tmp_path / "seed")
    identity = {
        "block_id": "block-1",
        "byte_start": 0,
        "byte_end": 12,
        "content_hash": "a" * 64,
        "extraction_fingerprint": "sha256:" + "b" * 64,
    }
    unit_id = extraction_unit_id(**identity)
    seed.record_extraction_unit(
        "run-1",
        document_id="doc-1",
        unit_id=unit_id,
        source_path="/vault/source.md",
        attempt=1,
        status="succeeded",
        result={"nodes": [{"type": "Concept", "title": "Durable"}], "edges": []},
        **identity,
    )
    row = seed.path.read_bytes().removesuffix(b"\n")
    marker_end = row.index(b'"kind":"extraction_unit"') + len(b'"kind":"extraction_unit"')
    cuts = sorted({marker_end, marker_end + 1, min(len(row) - 1, marker_end + 128), len(row) - 1})

    for index, cut in enumerate(cuts):
        ledger = CandidateLedger(tmp_path / f"case-{index}")
        ledger.path.parent.mkdir(parents=True, exist_ok=True)
        ledger.path.write_bytes(row[:cut])

        ledger.append("ingest_run", run_id=f"recovered-{index}", state="completed")

        scan = ledger.scan()
        assert scan.completeness_status == "complete"
        assert scan.malformed_line_count == 0
        assert [record["kind"] for record in scan.parsed_records] == ["ingest_run"]
        assert ledger.path.read_bytes().endswith(b"\n")


@pytest.mark.parametrize("kind", ["candidate", "comparison"])
def test_torn_non_write_ahead_evidence_tail_is_repaired_before_append(
    tmp_path: Path,
    kind: str,
) -> None:
    ledger = CandidateLedger(tmp_path / kind)
    row = json.dumps(
        {
            "kind": kind,
            "padding": "x" * 2_000,
            "ledger_version": 2,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    marker_end = row.index(f'"kind":"{kind}"'.encode()) + len(f'"kind":"{kind}"')
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_bytes(row[: marker_end + 100])

    ledger.append("ingest_run", run_id="recovered", state="completed")

    assert [record["kind"] for record in ledger.scan().parsed_records] == ["ingest_run"]


@pytest.mark.parametrize("kind", ["ingest_run", "candidate", "comparison"])
def test_apply_resume_repairs_torn_replayable_tail(tmp_path: Path, kind: str) -> None:
    ledger = CandidateLedger(tmp_path / kind)
    row = json.dumps(
        {"kind": kind, "padding": "x" * 2_000, "ledger_version": 2},
        separators=(",", ":"),
    ).encode("utf-8")
    marker_end = row.index(f'"kind":"{kind}"'.encode()) + len(f'"kind":"{kind}"')
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_bytes(row[: marker_end + 100])

    assert ledger.unreceipted_commit_plans() == ()
    scan = ledger.scan()
    assert scan.completeness_status == "complete"
    assert scan.parsed_records == ()


def test_apply_resume_does_not_trust_nested_safe_kind_in_torn_plan(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    torn = b'{"kind":"commit_plan","context":{"kind":"extraction_unit"},"operations":['
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_bytes(torn)

    with pytest.raises(ValueError, match="invalid candidate ledger"):
        ledger.unreceipted_commit_plans()

    assert ledger.path.read_bytes() == torn


def test_nested_safe_kind_does_not_make_unknown_torn_tail_discardable(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    torn = b'{"payload":{"kind":"extraction_unit"},"other":"unfinished'
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_bytes(torn)

    with pytest.raises(ValueError, match="unsafe torn record"):
        ledger.append("ingest_run", run_id="must-not-append", state="completed")

    assert ledger.path.read_bytes() == torn


def test_legacy_sorted_extraction_tail_is_detected_beyond_512_bytes(
    tmp_path: Path,
) -> None:
    ledger = CandidateLedger(tmp_path)
    legacy = json.dumps(
        {
            "anomalies": {"padding": "x" * 700},
            "attempt": 1,
            "block_id": "block-1",
            "byte_end": 10,
            "byte_start": 0,
            "content_hash": "a" * 64,
            "document_id": "doc-1",
            "duration_ms": 1.0,
            "error_class": None,
            "extraction_fingerprint": "sha256:" + "b" * 64,
            "kind": "extraction_unit",
            "ledger_version": 2,
            "result": {"nodes": [], "edges": []},
            "run_id": "run-1",
            "source_path": "/vault/source.md",
            "status": "succeeded",
            "unit_id": "unit-1",
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    marker_end = legacy.index(b'"kind":"extraction_unit"') + len(b'"kind":"extraction_unit"')
    assert marker_end > 512
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_bytes(legacy[:marker_end])

    assert ledger.unreceipted_commit_plans() == ()
    ledger.append("ingest_run", run_id="recovered", state="completed")

    assert ledger.scan().completeness_status == "complete"


def test_append_refuses_to_guess_away_torn_write_ahead_record(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    torn = b'{"kind":"commit_record","ledger_version":2'
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger.path.write_bytes(torn)

    with pytest.raises(ValueError, match="unsafe torn record"):
        ledger.append("ingest_run", run_id="must-not-land", state="failed")

    assert ledger.path.read_bytes() == torn


def test_append_preserves_complete_json_row_missing_only_its_newline(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.append("ingest_run", run_id="first", state="started")
    ledger.path.write_bytes(ledger.path.read_bytes().removesuffix(b"\n"))

    ledger.append("ingest_run", run_id="second", state="completed")

    scan = ledger.scan()
    assert scan.completeness_status == "complete"
    assert [record["run_id"] for record in scan.parsed_records] == ["first", "second"]


def test_commit_plan_and_receipt_are_fsynced_before_return(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import okto_neuron.consolidate.ledger as ledger_module

    calls: list[int] = []
    real_fsync = ledger_module.os.fsync

    def record_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(ledger_module.os, "fsync", record_fsync)
    ledger = CandidateLedger(tmp_path)
    run_id = "run"

    plan_id = ledger.record_commit_plan(run_id, operations=[])
    plan_fsyncs = len(calls)
    ledger.record_commit(run_id, plan_id=plan_id, result={})

    # First plan creation fsyncs the file and its directory; the existing-file
    # receipt needs a file fsync. Platform directory handling stays abstract.
    assert plan_fsyncs >= 1
    assert len(calls) > plan_fsyncs


def test_unreceipted_plan_is_self_authenticating_and_independent_of_started_row(
    tmp_path: Path,
) -> None:
    ledger = CandidateLedger(tmp_path)
    plan_id = ledger.record_commit_plan(
        "run-without-start",
        operations=[_dead_letter_operation("edge-1")],
        context={"document_id": "doc-1", "semantic_policy_pre": "policy-a"},
    )

    plans = ledger.unreceipted_commit_plans(document_id="doc-1")

    assert len(plans) == 1
    assert plans[0].run_id == "run-without-start"
    assert plans[0].plan_id == plan_id
    assert plans[0].plan_hash.startswith("sha256:")
    assert plans[0].operations[0]["candidate_id"] == "edge-1"
    assert ledger.unreceipted_commit_plans(document_id="other") == ()


def test_commit_receipt_removes_plan_from_apply_resume_lane(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    plan_id = ledger.record_commit_plan(
        "run",
        operations=[_dead_letter_operation("edge-1")],
        context={"document_id": "doc"},
    )
    assert len(ledger.unreceipted_commit_plans(document_id="doc")) == 1

    _receipt_only_operation(ledger, plan_id)
    ledger.record_commit("run", plan_id=plan_id, result={"operations": []})

    assert ledger.unreceipted_commit_plans(document_id="doc") == ()


def test_tampered_commit_plan_fails_closed(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.record_commit_plan(
        "run",
        operations=[_dead_letter_operation("edge-1")],
        context={"document_id": "doc"},
    )
    records = ledger.records()
    records[0]["context"]["document_id"] = "tampered"
    ledger.path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    import pytest

    with pytest.raises(ValueError, match="digest mismatch"):
        ledger.unreceipted_commit_plans(document_id="doc")


def test_finish_run_records_post_state_semantic_fingerprint(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.finish_run(
        "run",
        state="completed",
        summary={},
        post_semantic_policy_fingerprint="sha256:post",
    )

    record = ledger.records()[0]
    assert record["post_semantic_policy_fingerprint"] == "sha256:post"


def test_durable_commit_receipt_closes_run_even_if_terminal_row_is_lost(
    tmp_path: Path,
) -> None:
    ledger = CandidateLedger(tmp_path)
    run_id = ledger.start_run(
        document_id="doc",
        source="source.md",
        blocks_total=1,
        model="model",
        extraction_fingerprint="extract",
        semantic_policy_fingerprint="policy",
    )
    plan_id = ledger.record_commit_plan(
        run_id,
        operations=[],
        context={"document_id": "doc"},
    )
    ledger.record_commit(run_id, plan_id=plan_id, result={})

    assert ledger.has_open_run(document_id="doc") is False
    assert (
        ledger.find_resumable_run(
            document_id="doc",
            blocks_total=1,
            model="model",
            extraction_fingerprint="extract",
            semantic_policy_fingerprint="policy",
        )
        is None
    )


def test_apply_resume_rejects_a_torn_final_receipt(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.record_commit_plan(
        "run",
        operations=[_dead_letter_operation("edge-1")],
        context={"document_id": "doc"},
    )
    with ledger.path.open("ab") as handle:
        handle.write(b'{"kind":"commit_record"')

    import pytest

    with pytest.raises(ValueError, match="invalid candidate ledger"):
        ledger.unreceipted_commit_plans(document_id="doc")


def test_apply_resume_rejects_malformed_ledger_before_sealed_plan(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.path.write_text("broken\n", encoding="utf-8")
    ledger.record_commit_plan(
        "run",
        operations=[_dead_letter_operation("edge-1")],
        context={"document_id": "doc"},
    )

    import pytest

    with pytest.raises(ValueError, match="invalid candidate ledger"):
        ledger.unreceipted_commit_plans(document_id="doc")


def test_apply_resume_rejects_duplicate_plan_ids(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.record_commit_plan(
        "run",
        operations=[_dead_letter_operation("edge-1")],
        context={"document_id": "doc"},
    )
    record = ledger.records()[0]
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")

    import pytest

    with pytest.raises(ValueError, match="duplicate commit plan id"):
        ledger.unreceipted_commit_plans(document_id="doc")
