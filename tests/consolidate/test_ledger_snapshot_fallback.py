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


def test_a_lagging_cached_state_is_extended_not_rebuilt_from_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader more than the inline tail behind scans only the gap, never the whole ledger."""
    build_synthetic_ledger(tmp_path, target_bytes=2_000_000, row_chars=2_000, open_plans=1)
    _sidecar_cache_drop()
    ledger = CandidateLedger(tmp_path)
    ledger.run_progress_summary(None, limit=12)  # the in-process state now covers the file
    base_size = ledger.path.stat().st_size
    monkeypatch.setattr(mod, "_SIDECAR_INLINE_TAIL", 64 * 1024)
    run_id = ledger.start_run(document_id="live", source="live.md", blocks_total=1, model="m")
    with monkeypatch.context() as foreign:  # appends by another process: our state does not follow
        foreign.setattr(CandidateLedger, "_sidecar_after_append", lambda *a, **k: None)
        for n in range(200):
            ledger.record_candidate(
                run_id,
                candidate_id=f"gap-{n}",
                candidate_kind="node",
                state="proposed",
                payload={"type": "Agent", "title": f"gap {n}", "summary": "x" * 2_000},
            )
    gap = ledger.path.stat().st_size - base_size
    assert gap > 64 * 1024

    scanned: list[int] = []
    real = mod._Sidecar.ingest_raw

    def counting(self: Any, raw: bytes, read_row: Any) -> None:
        scanned.append(len(raw))
        real(self, raw, read_row)

    monkeypatch.setattr(mod._Sidecar, "ingest_raw", counting)
    answer = ledger.run_progress_summary(None, limit=12)
    assert answer is not None
    assert sum(scanned) <= gap + 4096, f"scanned {sum(scanned)} bytes for a {gap} byte gap"
    with monkeypatch.context() as fallback:
        fallback.setattr(CandidateLedger, "_prepare_sidecar", lambda self: False)
        assert answer == ledger.run_progress_summary(None, limit=12)


def test_a_row_appended_during_the_lock_free_extend_leaves_the_published_state_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_synthetic_ledger(tmp_path, target_bytes=2_000_000, row_chars=2_000, open_plans=1)
    _sidecar_cache_drop()
    ledger = CandidateLedger(tmp_path)
    ledger.run_progress_summary(None, limit=12)
    monkeypatch.setattr(mod, "_SIDECAR_INLINE_TAIL", 64 * 1024)
    run_id = ledger.start_run(document_id="live", source="live.md", blocks_total=1, model="m")
    with monkeypatch.context() as foreign:
        foreign.setattr(CandidateLedger, "_sidecar_after_append", lambda *a, **k: None)
        for n in range(200):
            ledger.record_candidate(
                run_id,
                candidate_id=f"gap-{n}",
                candidate_kind="node",
                state="proposed",
                payload={"type": "Agent", "title": f"gap {n}", "summary": "x" * 2_000},
            )
    real = mod._Sidecar.catch_up
    injected: list[int] = []

    def catch_up(self: Any, handle: Any, read_row: Any, limit: int | None = None) -> None:
        if limit is not None and not injected:  # the lock-free extend: an append lands in it
            injected.append(1)
            ledger.record_candidate(
                run_id,
                candidate_id="during-extend",
                candidate_kind="node",
                state="proposed",
                payload={"type": "Agent", "title": "during extend"},
            )
        real(self, handle, read_row, limit)

    monkeypatch.setattr(mod._Sidecar, "catch_up", catch_up)
    answer = ledger.run_progress_summary(None, limit=12)
    assert injected == [1]
    with monkeypatch.context() as fallback:
        fallback.setattr(CandidateLedger, "_prepare_sidecar", lambda self: False)
        assert answer == ledger.run_progress_summary(None, limit=12)
    assert answer["counts"]["candidate_rows"] == 201


@pytest.mark.slow
def test_no_in_lock_section_scans_more_than_the_inline_tail_under_a_fast_appender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_synthetic_ledger(tmp_path, target_bytes=2_000_000, row_chars=2_000, open_plans=1)
    _sidecar_cache_drop()
    ledger = CandidateLedger(tmp_path)
    ledger.run_progress_summary(None, limit=12)
    tail = 256 * 1024
    monkeypatch.setattr(mod, "_SIDECAR_INLINE_TAIL", tail)
    run_id = ledger.start_run(document_id="live", source="live.md", blocks_total=1, model="m")

    in_lock = {"active": 0, "scanned": 0, "worst": 0, "from_zero": 0}
    real_sync = CandidateLedger._sync_sidecar
    real_ingest = mod._Sidecar.ingest_raw

    def sync(self: CandidateLedger) -> Any:
        in_lock["scanned"] = 0
        in_lock["active"] = 1
        try:
            return real_sync(self)
        finally:
            in_lock["active"] = 0
            in_lock["worst"] = max(in_lock["worst"], in_lock["scanned"])

    def ingest(self: Any, raw: bytes, read_row: Any) -> None:
        if in_lock["active"]:
            in_lock["scanned"] += len(raw)
            if self.size == 0:
                in_lock["from_zero"] += 1
        real_ingest(self, raw, read_row)

    monkeypatch.setattr(CandidateLedger, "_sync_sidecar", sync)
    monkeypatch.setattr(mod._Sidecar, "ingest_raw", ingest)

    stop = threading.Event()

    def appender() -> None:
        n = 0
        while not stop.is_set():
            ledger.record_candidate(
                run_id,
                candidate_id=f"fast-{n}",
                candidate_kind="node",
                state="proposed",
                payload={"type": "Agent", "title": f"fast {n}", "summary": "x" * 2_000},
            )
            n += 1

    thread = threading.Thread(target=appender)
    thread.start()
    try:
        deadline = time.monotonic() + 8.0
        calls = 0
        while time.monotonic() < deadline:
            assert ledger.run_progress_summary(None, limit=12) is not None
            calls += 1
    finally:
        stop.set()
        thread.join()
    assert calls > 0
    assert in_lock["from_zero"] == 0, "a whole-ledger rebuild ran under the ledger lock"
    assert in_lock["worst"] <= tail + 64 * 1024, in_lock


def test_stale_sidecar_temps_are_removed_and_a_live_writers_temp_is_kept(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys
    import time

    dead = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True
    )
    dead_pid = int(dead.stdout)
    index = tmp_path / mod.LEDGER_INDEX_FILENAME
    stale = [tmp_path / f"{index.name}.{dead_pid}.{tid}.tmp" for tid in (1, 2, 3)]
    live = tmp_path / f"{index.name}.{os.getpid()}.999.tmp"
    old_live = tmp_path / f"{index.name}.{os.getpid()}.998.tmp"
    unrelated = tmp_path / f"{index.name}.notapid.x.tmp"
    for temp in [*stale, live, old_live, unrelated]:
        temp.write_bytes(b"x")
    two_hours_ago = time.time() - 7200
    os.utime(old_live, (two_hours_ago, two_hours_ago))

    mod._write_sidecar_file(index, b"payload")

    assert index.read_bytes() == b"payload"
    assert [temp.exists() for temp in stale] == [False, False, False]
    assert live.exists(), "a live writer's recent temp must be kept"
    assert not old_live.exists(), "a temp over an hour old is stale even if its pid is alive"
    assert unrelated.exists(), "names that are not <pid>.<tid> are left alone"
    assert not list(
        tmp_path.glob(f"{index.name}.{os.getpid()}.{__import__('threading').get_ident()}.tmp")
    )
