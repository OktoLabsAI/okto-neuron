from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from okto_neuron.cli import app
from okto_neuron.consolidate.ledger import CandidateLedger
from okto_neuron.core.schema import Node
import okto_neuron.semantic_acceptance as semantic_acceptance
from okto_neuron.semantic_quality import evaluate_store
from okto_neuron.store import InMemoryStore


_ADVERSARIAL_FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "semantic_quality" / "adversarial-ledger.v1.json"
)


def _complete_ledger(vault: Path, run_id: str = "run-1") -> CandidateLedger:
    ledger = CandidateLedger(vault / ".marginalia")
    ledger.append("ingest_run", run_id=run_id, state="started")
    ledger.append(
        "candidate",
        run_id=run_id,
        candidate_id="node-1",
        candidate_kind="node",
        state="proposed",
        payload={"type": "Agent", "title": "Ari"},
    )
    ledger.append(
        "commit_plan",
        run_id=run_id,
        plan_id="plan-1",
        operations=[
            {
                "operation": "create_node",
                "candidate_kind": "node",
                "candidate_id": "node-1",
                "type": "Agent",
                "title": "Ari",
            }
        ],
    )
    ledger.append(
        "candidate",
        run_id=run_id,
        candidate_id="node-1",
        candidate_kind="node",
        state="committed",
        payload={},
    )
    ledger.append("commit_record", run_id=run_id, plan_id="plan-1", result={})
    ledger.append("ingest_run", run_id=run_id, state="completed")
    return ledger


def test_quality_ledger_is_offline_deterministic_and_explicit(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    ledger = _complete_ledger(vault)
    output = tmp_path / "report.json"
    runner = CliRunner()

    result = runner.invoke(
        app,
        ["quality", "ledger", str(vault), "--run-id", "run-1", "--output", str(output)],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["report_variant"] == "candidate_ledger_plan.v1"
    assert report["evidence"]["selected_run_ids"] == ["run-1"]
    assert report["evidence"]["source_framing"]["file_sha256"] == ledger.scan().file_sha256
    first_bytes = output.read_bytes()

    refused = runner.invoke(
        app,
        ["quality", "ledger", str(vault), "--run-id", "run-1", "--output", str(output)],
    )
    assert refused.exit_code != 0
    assert "output already exists" in refused.output

    replaced = runner.invoke(
        app,
        [
            "quality",
            "ledger",
            str(vault),
            "--run-id",
            "run-1",
            "--output",
            str(output),
            "--force",
        ],
    )
    assert replaced.exit_code == 0, replaced.output
    assert output.read_bytes() == first_bytes


def test_quality_ledger_refuses_implicit_or_missing_evidence(tmp_path: Path) -> None:
    runner = CliRunner()
    vault = tmp_path / "vault"

    no_run = runner.invoke(app, ["quality", "ledger", str(vault)])
    assert no_run.exit_code != 0
    assert "--run-id" in no_run.output

    missing = runner.invoke(
        app,
        ["quality", "ledger", str(vault), "--run-id", "run-1"],
    )
    assert missing.exit_code != 0
    assert "candidate ledger not found" in missing.output


def test_quality_ledger_binds_explicit_adjudication_registry_and_recall(tmp_path: Path) -> None:
    fixture = json.loads(_ADVERSARIAL_FIXTURE.read_text(encoding="utf-8"))
    vault = tmp_path / "vault"
    ledger = CandidateLedger(vault / ".marginalia")
    for row in fixture["records"]:
        payload = {key: value for key, value in row.items() if key != "kind"}
        ledger.append(row["kind"], **payload)
    adjudication = tmp_path / "adjudication.json"
    predicates = tmp_path / "predicates.json"
    recall = tmp_path / "recall.json"
    adjudication.write_text(json.dumps(fixture["adjudication"]), encoding="utf-8")
    predicates.write_text(json.dumps(fixture["registered_predicates"]), encoding="utf-8")
    recall.write_text(json.dumps(fixture["recall_samples"]), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "quality",
            "ledger",
            str(vault),
            "--run-id",
            "adversarial-run-v1",
            "--adjudication",
            str(adjudication),
            "--registered-predicates",
            str(predicates),
            "--recall-samples",
            str(recall),
        ],
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["layers"]["adjudication"]["status"] == "measured"
    assert report["layers"]["identity"]["b3_cluster_quality"]["f1"] == 0.9
    assert report["layers"]["recall"]["completion"]["calls_total"] == 0
    external = report["evidence"]["external_inputs"]
    assert len(external["adjudication"]["sha256"]) == 64
    assert len(external["registered_predicates"]["sha256"]) == 64
    assert len(external["recall_samples"]["sha256"]) == 64


def test_quality_ledger_rejects_wrong_evidence_shape(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    _complete_ledger(vault)
    adjudication = tmp_path / "adjudication.json"
    adjudication.write_text("[]", encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "quality",
            "ledger",
            str(vault),
            "--run-id",
            "run-1",
            "--adjudication",
            str(adjudication),
        ],
    )

    assert result.exit_code != 0
    assert "must contain a JSON object" in result.output


def _snapshot_report(generation: str, *, expanded: bool = False) -> dict[str, object]:
    store = InMemoryStore()
    store.add_node(Node(id="alice", type="Agent", title="Alice"))
    if expanded:
        store.add_node(Node(id="bob", type="Agent", title="Bob"))
    integrity = {
        "status": "verified",
        "graph_generation": generation,
        "fresh_for_semantic_scan": True,
        "last_audit": {
            "status": "verified",
            "graph_generation": generation,
            "nodes_complete": True,
            "edges_complete": True,
            "adjacency_complete": True,
            "manifest_complete": True,
        },
    }
    return evaluate_store(
        store,
        integrity=integrity,
        registered_predicates=set(),
        config_fingerprint=f"sha256:{'1' * 64}",
        extraction_fingerprint=f"sha256:{'2' * 64}",
        semantic_policy_fingerprint=f"sha256:{'3' * 64}",
    )


def test_quality_churn_compares_reports_and_can_gate_exact_stability(tmp_path: Path) -> None:
    before = tmp_path / "before.json"
    same = tmp_path / "same.json"
    changed = tmp_path / "changed.json"
    before.write_text(
        json.dumps({"semantic_quality": _snapshot_report("before")}),
        encoding="utf-8",
    )
    same.write_text(json.dumps(_snapshot_report("same")), encoding="utf-8")
    changed_report = _snapshot_report("changed", expanded=True)
    changed.write_text(
        json.dumps(changed_report["semantic_snapshot"]),
        encoding="utf-8",
    )
    runner = CliRunner()

    stable = runner.invoke(
        app,
        ["quality", "churn", str(before), str(same), "--require-stable"],
    )
    assert stable.exit_code == 0, stable.output
    assert json.loads(stable.output)["stable"] is True

    unstable = runner.invoke(
        app,
        ["quality", "churn", str(before), str(changed), "--require-stable"],
    )
    assert unstable.exit_code == 1, unstable.output
    report = json.loads(unstable.output)
    assert report["stable"] is False
    assert report["dimensions"]["identities"]["added_count"] == 1


def test_quality_acceptance_cli_emits_and_gates_result(tmp_path: Path, monkeypatch) -> None:
    bundle = tmp_path / "acceptance.json"
    bundle.write_text("{}", encoding="utf-8")
    ready = {
        "schema_version": "semantic_acceptance_result.v1",
        "acceptance_ready": True,
    }
    monkeypatch.setattr(
        semantic_acceptance,
        "evaluate_semantic_acceptance",
        lambda payload: ready,
    )

    passed = CliRunner().invoke(
        app,
        ["quality", "acceptance", str(bundle), "--require-ready"],
    )
    assert passed.exit_code == 0, passed.output
    assert json.loads(passed.output) == ready

    not_ready = {**ready, "acceptance_ready": False}
    monkeypatch.setattr(
        semantic_acceptance,
        "evaluate_semantic_acceptance",
        lambda payload: not_ready,
    )
    failed = CliRunner().invoke(
        app,
        ["quality", "acceptance", str(bundle), "--require-ready"],
    )
    assert failed.exit_code == 1, failed.output
    assert json.loads(failed.output) == not_ready


def test_quality_acceptance_collection_cli_reports_and_materializes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    collection = tmp_path / "collection.json"
    collection.write_text("{}", encoding="utf-8")
    ready = {
        "schema_version": "semantic_acceptance_collection_status.v1",
        "status": "ready",
        "counts": {"ready": 1, "missing": 0, "invalid": 0, "total": 1},
        "artifacts": [],
    }
    bundle = {
        "schema_version": "semantic_acceptance_matrix.v1",
        "threshold_policy": {},
        "corpora": [],
        "temporal_correction": {},
        "public_diagnostic": {},
    }
    monkeypatch.setattr(
        semantic_acceptance,
        "inspect_semantic_acceptance_collection",
        lambda payload, *, base_dir: ready,
    )
    monkeypatch.setattr(
        semantic_acceptance,
        "materialize_semantic_acceptance_collection",
        lambda payload, *, base_dir: bundle,
    )
    runner = CliRunner()

    status = runner.invoke(
        app,
        ["quality", "acceptance-status", str(collection), "--require-ready"],
    )
    assert status.exit_code == 0, status.output
    assert json.loads(status.output) == ready

    output = tmp_path / "bundle.json"
    collected = runner.invoke(
        app,
        ["quality", "acceptance-collect", str(collection), "--output", str(output)],
    )
    assert collected.exit_code == 0, collected.output
    assert json.loads(output.read_text(encoding="utf-8")) == bundle


def test_quality_acceptance_status_cli_fails_closed_when_incomplete(
    tmp_path: Path,
    monkeypatch,
) -> None:
    collection = tmp_path / "collection.json"
    collection.write_text("{}", encoding="utf-8")
    incomplete = {
        "schema_version": "semantic_acceptance_collection_status.v1",
        "status": "incomplete",
        "counts": {"ready": 0, "missing": 1, "invalid": 0, "total": 1},
        "artifacts": [{"code": "corpus:chat", "expected": "corpus", "status": "missing"}],
    }
    monkeypatch.setattr(
        semantic_acceptance,
        "inspect_semantic_acceptance_collection",
        lambda payload, *, base_dir: incomplete,
    )

    result = CliRunner().invoke(
        app,
        ["quality", "acceptance-status", str(collection), "--require-ready"],
    )

    assert result.exit_code == 1, result.output
    assert json.loads(result.output) == incomplete
