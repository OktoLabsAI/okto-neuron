"""CI-safe deterministic recall-quality eval over the fixed synthetic vault.

This is the regression-catching variant of the eval harness: it uses the REAL
fastembed embedder (deterministic, offline, no LLM) against the fixed
synthetic-vault fixture and scores the same three metrics the real-model
acceptance scenario (92_eval_recall_provenance.sh) scores:

  - expected-entity-present@k   (gate: >= 5/6)
  - provenance-valid            (every Claim hit's bytes sha256 back to source)
  - partner-recall              (designated hard-gate query never regresses)

No mocks: the embedder and similarity search are real. The LLM `ask` path is
NOT exercised here (it needs the 35B model) — that lives in the acceptance
scenario. This module is the deterministic floor that runs everywhere.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from okto_neuron.vault import Vault
from tests.eval import golden


@pytest.fixture(scope="module")
def eval_vault(tmp_path_factory: pytest.TempPathFactory) -> Vault:
    vault_root = tmp_path_factory.mktemp("eval") / "synthetic-vault"
    shutil.copytree(golden.FIXTURE_ROOT, vault_root)
    vault = Vault.open(vault_root)
    for path in sorted(vault_root.rglob("*.md")):
        vault.add(path)
    return vault


@pytest.fixture(scope="module")
def report(eval_vault: Vault) -> dict:
    def query_fn(q: str, k: int):
        return eval_vault.query(q, k=k)

    return golden.score(Path(eval_vault.root), query_fn, k=golden.K)


@pytest.mark.eval
def test_expected_entity_at_k_gate(report: dict) -> None:
    detail = "\n".join(
        f"  {r['id']}: present={r['entity_present']} (expect {r['expect_path']}, {r['n_hits']} hits)"
        for r in report["per_query"]
    )
    assert report["entity_gate_pass"], (
        f"expected-entity@{report['k']} = {report['entity_hits']}/{report['total_queries']} "
        f"({report['entity_at_k']:.2f}) < gate {report['entity_at_k_min']:.2f}\n{detail}"
    )


@pytest.mark.eval
def test_provenance_bytes_hash_back_to_source(report: dict) -> None:
    assert report["provenance_gate_pass"], (
        "provenance byte-range validation failed:\n  " + "\n  ".join(report["provenance_failures"])
    )


@pytest.mark.eval
def test_partner_recall_hard_gate(report: dict) -> None:
    assert report["partner_recall_gate_pass"], "partner-recall gate failed:\n  " + "\n  ".join(
        report["partner_recall_failures"]
    )


@pytest.mark.eval
def test_overall_eval_pass(report: dict) -> None:
    assert report["overall_pass"], f"eval report: {report}"
