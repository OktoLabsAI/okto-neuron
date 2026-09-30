"""A failed lock-free snapshot pass of the index prepare step is never silent."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from okto_neuron.consolidate import ledger as mod
from okto_neuron.consolidate.ledger import (
    LEDGER_INDEX_FILENAME,
    CandidateLedger,
    _sidecar_cache_drop,
)
from tests.support._ledger_synth import build_synthetic_ledger


def _cold_ledger(tmp_path: Path) -> CandidateLedger:
    build_synthetic_ledger(tmp_path, target_bytes=2_000_000, row_chars=2_000, open_plans=1)
    _sidecar_cache_drop()
    (tmp_path / LEDGER_INDEX_FILENAME).unlink(missing_ok=True)
    return CandidateLedger(tmp_path)


def _fail_snapshot_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    real = mod._Sidecar.catch_up

    def catch_up(self: Any, handle: Any, read_row: Any, limit: int | None = None) -> None:
        if limit is not None:  # only the lock-free snapshot pass passes a limit
            raise RuntimeError("injected snapshot-pass failure")
        real(self, handle, read_row, limit)

    monkeypatch.setattr(mod._Sidecar, "catch_up", catch_up)


def test_a_failed_snapshot_pass_logs_the_fallback_at_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ledger = _cold_ledger(tmp_path)
    _fail_snapshot_pass(monkeypatch)
    with caplog.at_level(logging.INFO, logger=mod.__name__):
        ledger._prepare_sidecar()
    messages = [r.getMessage() for r in caplog.records if r.name == mod.__name__]
    assert any(
        "snapshot" in m and "RuntimeError" in m and "injected snapshot-pass failure" in m
        for m in messages
    ), messages
    assert all(r.levelno == logging.INFO for r in caplog.records if r.name == mod.__name__)
