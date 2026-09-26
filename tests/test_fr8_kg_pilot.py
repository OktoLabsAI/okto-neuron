"""Generic FR8 `kg pilot` CLI tests."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import time

from click.testing import CliRunner

from okto_neuron.cli import app

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "synthetic-vault"


def test_kg_pilot_emits_signed_log_on_pass(tmp_path: Path) -> None:
    vault_root = _copy_synthetic_vault(tmp_path)
    report_dir = tmp_path / "reports"
    result = CliRunner().invoke(
        app,
        ["pilot", str(vault_root), "--report-dir", str(report_dir)],
    )
    assert result.exit_code == 0, result.output
    logs = sorted(report_dir.glob("*pilot-log*.json"))
    assert logs
    payload = json.loads(logs[-1].read_text(encoding="utf-8"))
    assert payload["pass"] is True
    assert payload["drift_envelope"]["total"] >= 1


def test_kg_pilot_clear_error_on_missing_corpus(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["pilot", str(tmp_path / "nonexistent")])
    assert result.exit_code == 2
    stderr = getattr(result, "stderr", "") or result.output
    assert "vault" in stderr.lower() or "not found" in stderr.lower()


def test_kg_pilot_synthetic_suite_completes_within_30s(tmp_path: Path) -> None:
    vault_root = _copy_synthetic_vault(tmp_path)
    started = time.time()
    result = CliRunner().invoke(
        app,
        ["pilot", str(vault_root), "--report-dir", str(tmp_path / "reports")],
    )
    elapsed = time.time() - started
    assert result.exit_code == 0, result.output
    assert elapsed < 30.0


def _copy_synthetic_vault(tmp_path: Path) -> Path:
    vault_root = tmp_path / "vault"
    shutil.copytree(FIXTURE_ROOT, vault_root)
    return vault_root
