"""A cold index build started by the startup prewarm stops promptly when asked to."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from okto_neuron.consolidate import ledger as mod
from okto_neuron.consolidate.ledger import CandidateLedger, _sidecar_cache_drop
from tests.support._ledger_synth import build_synthetic_ledger

_ROW_DELAY_S = 0.005


def _slow(monkeypatch: pytest.MonkeyPatch, owner: Any, name: str) -> None:
    real = getattr(owner, name)

    def slowed(*args: Any, **kwargs: Any) -> Any:
        time.sleep(_ROW_DELAY_S)
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, slowed)


@pytest.mark.parametrize("phase", ["sidecar", "offset_index"])
def test_a_cancelled_prewarm_returns_within_a_second_and_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    build_synthetic_ledger(tmp_path, target_bytes=3_000_000, row_chars=2_000, open_plans=1)
    _sidecar_cache_drop()
    mod._invalidate_ledger_offset_index(tmp_path / mod.LEDGER_FILENAME)
    (tmp_path / mod.LEDGER_INDEX_FILENAME).unlink(missing_ok=True)
    monkeypatch.setattr(mod, "_CANCEL_CHECK_BYTES", 64 * 1024)
    if phase == "sidecar":
        _slow(monkeypatch, mod._Sidecar, "ingest_raw")
    else:
        _slow(monkeypatch, mod, "_ingest_index_row")
    ledger = CandidateLedger(tmp_path)
    cancel = threading.Event()
    done = threading.Event()

    def run() -> None:
        ledger.prewarm(cancel.is_set)
        done.set()

    thread = threading.Thread(target=run)
    thread.start()
    try:
        time.sleep(0.3)
        assert not done.is_set(), "the build finished before it could be cancelled"
        start = time.perf_counter()
        cancel.set()
        assert done.wait(timeout=1.0), "prewarm did not return within 1 s of the cancel"
        assert time.perf_counter() - start < 1.0
    finally:
        cancel.set()
        thread.join(timeout=60)

    key = str(ledger.path.resolve())
    if phase == "sidecar":
        assert mod._sidecar_cache_get(key) is None, "a cancelled pass must not publish its state"
    # In the offset-index phase the sidecar pass has already finished and is published.
    assert key not in mod._LEDGER_INDEXES, "a cancelled build must not publish an index"
    assert ledger.index_degraded_reason() is None, "a cancel is not a failure"


def _big_plan_ledger(tmp_path: Path) -> CandidateLedger:
    build_synthetic_ledger(
        tmp_path,
        target_bytes=3_000_000,
        row_chars=2_000,
        open_plans=1,
        plan_ops=6,
        op_chars=200_000,
    )
    _sidecar_cache_drop()
    (tmp_path / mod.LEDGER_INDEX_FILENAME).unlink(missing_ok=True)
    return CandidateLedger(tmp_path)


def _count_big_loads(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    sizes: list[int] = []
    real = json.loads

    def loads(s: Any, *args: Any, **kwargs: Any) -> Any:
        sizes.append(len(s))
        return real(s, *args, **kwargs)

    monkeypatch.setattr(mod.json, "loads", loads)
    return sizes


def test_prewarm_builds_the_run_summary_so_the_first_poll_parses_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _big_plan_ledger(tmp_path)
    sizes = _count_big_loads(monkeypatch)
    ledger.prewarm()
    assert any(n > 1_000_000 for n in sizes), "prewarm did not parse the big plan row"
    sizes.clear()
    assert ledger.run_progress_summary(None, limit=12) is not None
    assert sizes == [], f"the first poll after a prewarm parsed {sizes}"


def test_a_prewarm_cancelled_before_the_summary_does_not_run_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger = _big_plan_ledger(tmp_path)
    calls: list[int] = []
    real = CandidateLedger.run_progress_summary

    def spy(self: CandidateLedger, *args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(CandidateLedger, "run_progress_summary", spy)
    stop = threading.Event()
    real_offset_index = CandidateLedger._offset_index

    def offset_index_then_cancel(self: CandidateLedger) -> Any:
        result = real_offset_index(self)
        stop.set()  # the stop request arrives after the builds finished
        return result

    monkeypatch.setattr(CandidateLedger, "_offset_index", offset_index_then_cancel)
    ledger.prewarm(stop.is_set)
    assert calls == []
