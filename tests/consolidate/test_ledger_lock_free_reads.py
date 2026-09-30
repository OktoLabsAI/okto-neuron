"""Whole-file ledger readers must not stall appends.

A governance read on an HTTP request path scans the ledger; if the scan held the
ledger lock for the whole pass, every ingest append behind it would wait for a
full-file read. The readers pin a snapshot under the lock, stream it without the
lock, and (for the index) take the lock again only to fold in the tail.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from okto_neuron.consolidate.ledger import (
    LEDGER_FILENAME,
    LEDGER_INDEX_FILENAME,
    CandidateLedger,
    _sidecar_cache_drop,
)
from tests.support._ledger_synth import _text, build_synthetic_ledger

_P99_BOUND_S = 0.050


class Appender(threading.Thread):
    """Appends a realistic candidate row every ~5 ms and records each call's latency."""

    def __init__(self, directory: Path) -> None:
        super().__init__(daemon=True)
        self.ledger = CandidateLedger(directory)
        self.run_id = self.ledger.start_run(
            document_id="live", source="live.md", blocks_total=1, model="m"
        )
        self.stop = threading.Event()
        self.latencies: list[float] = []
        self.count = 0
        self.error: BaseException | None = None

    def run(self) -> None:
        try:
            while not self.stop.is_set():
                started = time.perf_counter()
                self.ledger.record_candidate(
                    self.run_id,
                    candidate_id=f"live-{self.count}",
                    candidate_kind="node",
                    state="proposed",
                    payload={
                        "type": "Agent",
                        "title": f"live {self.count}",
                        "summary": _text(self.count, 2000),
                    },
                )
                self.latencies.append(time.perf_counter() - started)
                self.count += 1
                time.sleep(0.005)
        except BaseException as exc:  # noqa: BLE001
            self.error = exc

    def p99(self) -> float:
        ordered = sorted(self.latencies)
        return ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]


def _live_ids(records: Any) -> list[int]:
    return [
        int(r["candidate_id"].split("-")[1])
        for r in records
        if str(r.get("candidate_id", "")).startswith("live-")
    ]


def _prefix_truth(path: Path, size: int) -> tuple[list[dict[str, Any]], str]:
    data = path.read_bytes()[:size]
    rows = []
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows, hashlib.sha256(data).hexdigest()


@pytest.fixture(scope="module")
def source_ledger(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("lockfree-src")
    build_synthetic_ledger(directory, target_bytes=25_000_000)
    return directory


@pytest.fixture()
def ledger_dir(source_ledger: Path, tmp_path: Path) -> Path:
    _sidecar_cache_drop()
    target = tmp_path / "ledger"
    shutil.copytree(source_ledger, target)
    return target


def _run_under_appends(directory: Path, work: Any, repeats: int = 5) -> tuple[Appender, list[Any]]:
    appender = Appender(directory)
    appender.start()
    time.sleep(0.05)
    results = []
    try:
        for _ in range(repeats):
            results.append(work())
    finally:
        appender.stop.set()
        appender.join(timeout=30)
    assert appender.error is None
    assert len(appender.latencies) > 5
    return appender, results


def test_scan_does_not_stall_appends_and_returns_its_snapshot(ledger_dir: Path) -> None:
    ledger = CandidateLedger(ledger_dir)
    appender, results = _run_under_appends(ledger_dir, ledger.scan)
    # in-process the parse competes for the GIL, so only a loose bound here; the
    # flock itself is timed against a reader in a child process below
    assert appender.p99() < 10 * _P99_BOUND_S, appender.p99()
    path = ledger_dir / LEDGER_FILENAME
    for scan in results:
        rows, digest = _prefix_truth(path, scan.file_size_bytes)
        assert list(scan.parsed_records) == rows  # exactly the prefix at its snapshot
        assert scan.file_sha256 == digest
        assert scan.completeness_status == "complete"
        live = _live_ids(scan.parsed_records)
        assert live == list(range(len(live)))  # a contiguous prefix of the appends
    last = _live_ids(results[-1].parsed_records)
    # rows appended during the pass are not in it, but the next call sees them
    assert len(last) < appender.count
    after = _live_ids(ledger.scan().parsed_records)
    assert after == list(range(appender.count))


def test_iter_records_in_a_thread_returns_while_appends_continue(ledger_dir: Path) -> None:
    ledger = CandidateLedger(ledger_dir)
    appender, results = _run_under_appends(
        ledger_dir, lambda: sum(1 for _ in ledger.iter_records())
    )
    assert appender.p99() < 10 * _P99_BOUND_S, appender.p99()
    assert all(n > 0 for n in results)


_CHILD = r"""
import sys
from pathlib import Path
from okto_neuron.consolidate import ledger as mod
from okto_neuron.consolidate.ledger import CandidateLedger, LEDGER_INDEX_FILENAME

directory, mode, repeats = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
ledger = CandidateLedger(directory)
for _ in range(repeats):
    if mode == "scan":
        ledger.scan()
    elif mode == "iter_records":
        sum(1 for _ in ledger.iter_records())
    elif mode == "index_rebuild":
        (directory / LEDGER_INDEX_FILENAME).unlink(missing_ok=True)
        mod._sidecar_cache_drop()
        ledger.unreceipted_commit_plans()
    elif mode == "offset_rebuild":
        mod._invalidate_ledger_offset_index(ledger.path)
        ledger._open_runs()
    else:
        raise SystemExit(mode)
"""


def _reader_process(directory: Path, mode: str, repeats: int = 4) -> Appender:
    """Appends run here; the rebuilding reader runs in a child process.

    A child keeps the GIL out of the measurement: what is timed is the ledger's
    flock, the thing a reader in another process (or thread) could hold.
    """
    import os
    import subprocess
    import sys

    import okto_neuron

    env = {**os.environ, "PYTHONPATH": str(Path(okto_neuron.__file__).resolve().parent.parent)}
    appender = Appender(directory)
    appender.start()
    time.sleep(0.05)
    try:
        subprocess.run(
            [sys.executable, "-c", _CHILD, str(directory), mode, str(repeats)],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
    finally:
        appender.stop.set()
        appender.join(timeout=30)
    assert appender.error is None
    assert len(appender.latencies) > 20
    return appender


@pytest.mark.parametrize("mode", ["scan", "iter_records"])
def test_reader_process_does_not_stall_appends(ledger_dir: Path, mode: str) -> None:
    appender = _reader_process(ledger_dir, mode)
    assert appender.p99() < _P99_BOUND_S, (mode, appender.p99())


def test_index_rebuild_does_not_stall_appends(ledger_dir: Path) -> None:
    appender = _reader_process(ledger_dir, "index_rebuild")
    assert appender.p99() < _P99_BOUND_S, appender.p99()
    # the rebuilt index equals a fresh scan even though appends landed mid-rebuild
    from tests.consolidate.test_ledger_index import assert_index_is_truth

    _sidecar_cache_drop()
    assert_index_is_truth(CandidateLedger(ledger_dir))


def test_old_offset_index_rebuild_does_not_stall_appends(ledger_dir: Path) -> None:
    appender = _reader_process(ledger_dir, "offset_rebuild")
    assert appender.p99() < _P99_BOUND_S, appender.p99()


def test_torn_tail_at_the_snapshot_boundary_stops_at_the_last_complete_line(
    tmp_path: Path,
) -> None:
    ledger = CandidateLedger(tmp_path)
    run = ledger.start_run(document_id="d", source="s", blocks_total=1, model="m")
    for i in range(3):
        ledger.record_candidate(
            run,
            candidate_id=f"live-{i}",
            candidate_kind="node",
            state="proposed",
            payload={"type": "Agent", "title": str(i)},
        )
    with ledger.path.open("ab") as fh:
        fh.write(
            b'{"kind":"candidate","run_id":"' + run.encode() + b'","candidate_id":"live-3","pay'
        )
    scan = ledger.scan()
    assert _live_ids(scan.parsed_records) == [0, 1, 2]
    assert scan.trailing_partial and scan.completeness_status == "incomplete"
    assert scan.file_size_bytes == ledger.path.stat().st_size
    assert scan.malformed_line_count == 1
    assert not any(
        r["kind"] == "candidate" and r["candidate_id"] == "live-3" for r in scan.parsed_records
    )


@pytest.mark.slow
@pytest.mark.perf
@pytest.mark.parametrize("reader", ["scan", "iter_records", "index_rebuild"])
def test_append_p99_on_a_300mb_ledger(
    tmp_path_factory: pytest.TempPathFactory, reader: str
) -> None:
    directory = tmp_path_factory.mktemp("lockfree-big", numbered=True)
    _sidecar_cache_drop()
    try:
        build_synthetic_ledger(directory, target_bytes=300_000_000, row_chars=16_000)
        ledger = CandidateLedger(directory)

        def work() -> Any:
            if reader == "scan":
                return ledger.scan(kinds=frozenset({"ingest_run"})).file_size_bytes
            if reader == "iter_records":
                return sum(1 for _ in ledger.iter_records())
            (directory / LEDGER_INDEX_FILENAME).unlink(missing_ok=True)
            _sidecar_cache_drop()
            return len(ledger.unreceipted_commit_plans())

        appender, _ = _run_under_appends(directory, work, repeats=2)
        assert appender.p99() < _P99_BOUND_S, (reader, appender.p99(), max(appender.latencies))
    finally:
        shutil.rmtree(directory, ignore_errors=True)
