"""A failed lock-free snapshot pass of the index prepare step is never silent."""

from __future__ import annotations

import logging
import threading
import time
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


def test_a_failed_snapshot_pass_never_becomes_a_whole_file_pass_under_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lock-free summary read answers from the whole-file readers, not an in-lock rebuild.

    Any ``catch_up`` without a limit is the in-lock sync. It is made to take 0.5 s, so an
    append that waits behind it is unmistakable, and every such call is recorded.
    """
    ledger = _cold_ledger(tmp_path)

    def no_records(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the degraded fallback must stream, never call records()")

    monkeypatch.setattr(CandidateLedger, "records", no_records)
    real = mod._Sidecar.catch_up
    in_lock_calls: list[int] = []

    def catch_up(self: Any, handle: Any, read_row: Any, limit: int | None = None) -> None:
        if limit is not None:
            raise RuntimeError("injected snapshot-pass failure")
        in_lock_calls.append(self.size)
        time.sleep(0.5)
        real(self, handle, read_row, limit)

    monkeypatch.setattr(mod._Sidecar, "catch_up", catch_up)

    run_id = ledger.start_run(document_id="live", source="live.md", blocks_total=1, model="m")
    stop = threading.Event()
    latencies: list[float] = []

    def appender() -> None:
        i = 0
        while not stop.is_set():
            start = time.perf_counter()
            ledger.record_candidate(
                run_id,
                candidate_id=f"live-{i}",
                candidate_kind="node",
                state="proposed",
                payload={"type": "Agent", "title": f"live {i}"},
            )
            latencies.append(time.perf_counter() - start)
            i += 1

    thread = threading.Thread(target=appender)
    thread.start()
    try:
        time.sleep(0.05)
        answer = ledger.run_progress_summary(None, limit=12)
    finally:
        stop.set()
        thread.join()

    assert in_lock_calls == [], f"in-lock catch_up ran from offsets {in_lock_calls}"
    assert latencies and max(latencies) < 0.25, f"slowest append {max(latencies) * 1000:.0f} ms"
    assert answer is not None and "run" in answer


def test_a_failing_prefix_is_attempted_once_and_reported_as_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ledger = _cold_ledger(tmp_path)
    expected = ledger.run_progress_summary(None, limit=12)
    assert expected is not None and "ledger_index_degraded" not in expected
    _sidecar_cache_drop()
    (tmp_path / LEDGER_INDEX_FILENAME).unlink(missing_ok=True)

    def no_records(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the degraded fallback must stream, never call records()")

    monkeypatch.setattr(CandidateLedger, "records", no_records)
    real = mod._Sidecar.catch_up
    snapshot_passes: list[int] = []

    def catch_up(self: Any, handle: Any, read_row: Any, limit: int | None = None) -> None:
        if limit is not None:
            snapshot_passes.append(limit)
            raise RuntimeError("injected snapshot-pass failure")
        real(self, handle, read_row, limit)

    monkeypatch.setattr(mod._Sidecar, "catch_up", catch_up)

    with caplog.at_level(logging.INFO, logger=mod.__name__):
        answers = [ledger.run_progress_summary(None, limit=12) for _ in range(5)]
    assert len(snapshot_passes) == 1, snapshot_passes
    infos = [r for r in caplog.records if r.name == mod.__name__ and "snapshot pass" in r.message]
    assert len(infos) == 1
    assert ledger.index_degraded_reason() == "RuntimeError: injected snapshot-pass failure"
    for answer in answers:
        assert answer is not None
        assert answer["ledger_index_degraded"] == ledger.index_degraded_reason()
        assert {k: v for k, v in answer.items() if k != "ledger_index_degraded"} == expected

    # The ledger grows past the failing prefix: exactly one more attempt, then memoized again.
    run_id = ledger.start_run(document_id="live", source="live.md", blocks_total=1, model="m")
    ledger.record_candidate(
        run_id,
        candidate_id="live-0",
        candidate_kind="node",
        state="proposed",
        payload={"type": "Agent", "title": "live 0"},
    )
    for _ in range(3):
        ledger.run_progress_summary(None, limit=12)
    assert len(snapshot_passes) == 2, snapshot_passes
    assert snapshot_passes[1] > snapshot_passes[0]

    # A healthy pass clears the flag.
    monkeypatch.setattr(mod._Sidecar, "catch_up", real)
    _sidecar_cache_drop()
    healed = ledger.run_progress_summary(None, limit=12)
    assert healed is not None and "ledger_index_degraded" not in healed
    assert ledger.index_degraded_reason() is None
