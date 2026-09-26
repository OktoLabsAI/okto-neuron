"""FR4 fixture-backed drift detector tests."""

from __future__ import annotations

import json
from pathlib import Path
import shutil

import pytest
from click.testing import CliRunner

from okto_neuron.cli import app
from okto_neuron.detectors import run_detector
from okto_neuron.vault import Vault

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "synthetic-vault"

DETECTORS = [
    "commitment_temporal_shacl",
    "supersedence_stale_head",
    "authority_alias_collision",
]


@pytest.fixture(scope="module")
def synthetic_vault(tmp_path_factory: pytest.TempPathFactory) -> Vault:
    vault_root = tmp_path_factory.mktemp("fr4") / "synthetic-vault"
    shutil.copytree(FIXTURE_ROOT, vault_root)
    vault = Vault.init(vault_root, embedding_provider="stub")
    for path in sorted(vault_root.rglob("*.md")):
        vault.add(path)
    return vault


@pytest.mark.parametrize("detector", DETECTORS)
def test_drift_fixture_yields_exactly_one_finding(
    synthetic_vault: Vault,
    detector: str,
) -> None:
    findings = run_detector(detector, synthetic_vault)
    assert len(findings) == 1
    f = findings[0]
    assert f.kind == detector
    assert len(f.evidence_claim_ids) >= 1


def test_detect_drift_cli_json_counts(synthetic_vault: Vault, cli_inprocess_server) -> None:
    # Route the thin-client CLI at an in-process server bound to this vault.
    cli_inprocess_server(synthetic_vault.root)
    result = CliRunner().invoke(
        app,
        ["detect-drift", "--vault", str(synthetic_vault.root), "--json"],
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["schema_version"] == "drift.v1"
    assert envelope["counts"] == {
        "commitment_temporal_shacl": 1,
        "supersedence_stale_head": 1,
        "authority_alias_collision": 1,
    }
    assert envelope["total"] == 3
