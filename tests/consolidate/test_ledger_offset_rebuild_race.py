"""The offset-index rebuild must survive appends that land while it runs.

The offset index is valid for the ledger prefix ``[0, cut)`` it was built from;
an append beyond ``cut`` can never invalidate it. A reader therefore never fails
because the ledger grew (a bounded retry would still lose under sustained ingest).

Two guards: a deterministic one that forces an append between the rebuild and the
reader's next step, and a stochastic one with an appender PROCESS writing at max
rate while a child process rebuilds the index in a loop.
"""

from __future__ import annotations

import bisect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import okto_neuron
from okto_neuron.consolidate import ledger as mod
from okto_neuron.consolidate.ledger import LEDGER_FILENAME, CandidateLedger, _sidecar_cache_drop
from tests.support._ledger_synth import build_synthetic_ledger

_APPENDER = r"""
import os, sys, time
from pathlib import Path
from okto_neuron.consolidate.ledger import CandidateLedger
from tests.support._ledger_synth import _text

directory, stamps = Path(sys.argv[1]), sys.argv[2]
fd = os.open(stamps, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
ledger = CandidateLedger(directory)
run_id = ledger.start_run(document_id="live", source="live.md", blocks_total=1, model="m")
os.write(1, b"ready\n")
i = 0
while True:
    ledger.record_candidate(
        run_id,
        candidate_id=f"live-{i}",
        candidate_kind="node",
        state="proposed",
        payload={"type": "Agent", "title": f"live {i}", "summary": _text(i, 2000)},
    )
    os.write(fd, time.time_ns().to_bytes(8, "little"))
    i += 1
"""

_REBUILDER = r"""
import json, sys, time
from pathlib import Path
from okto_neuron.consolidate import ledger as mod
from okto_neuron.consolidate.ledger import CandidateLedger

ledger = CandidateLedger(Path(sys.argv[1]))
for _ in range(int(sys.argv[2])):
    mod._invalidate_ledger_offset_index(ledger.path)
    start = time.time_ns()
    try:
        ledger._open_runs()
        error = None
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
    print(json.dumps({"start": start, "end": time.time_ns(), "error": error}), flush=True)
"""


def _env() -> dict[str, str]:
    root = str(Path(okto_neuron.__file__).resolve().parent.parent)
    return {**os.environ, "PYTHONPATH": os.pathsep.join([root, str(Path(__file__).parents[2])])}


@pytest.fixture(scope="module")
def source_ledger(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("offset-race-src")
    build_synthetic_ledger(directory, target_bytes=6_000_000, row_chars=2_000, open_plans=1)
    return directory


def _run_race(source: Path, work: Path, rebuilds: int) -> tuple[list[dict[str, Any]], list[int]]:
    import shutil

    _sidecar_cache_drop()
    directory = work / "ledger"
    shutil.copytree(source, directory)
    stamps = work / "stamps.bin"
    appender = subprocess.Popen(
        [sys.executable, "-c", _APPENDER, str(directory), str(stamps)],
        env=_env(),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert appender.stdout is not None and appender.stdout.readline().strip() == "ready"
        done = subprocess.run(
            [sys.executable, "-c", _REBUILDER, str(directory), str(rebuilds)],
            env=_env(),
            capture_output=True,
            text=True,
            timeout=600,
        )
    finally:
        appender.kill()
        appender.wait()
    assert done.returncode == 0, done.stderr
    windows = [json.loads(line) for line in done.stdout.splitlines()]
    data = stamps.read_bytes()
    times = [int.from_bytes(data[i : i + 8], "little") for i in range(0, len(data) - 7, 8)]
    return windows, times


def _assert_no_failures(windows: list[dict[str, Any]], times: list[int]) -> None:
    assert len(windows) > 0
    failures = [w["error"] for w in windows if w["error"]]
    overlapped = [
        bisect.bisect_right(times, w["end"]) - bisect.bisect_left(times, w["start"])
        for w in windows
    ]
    summary = (
        f"rebuilds={len(windows)} failures={len(failures)} "
        f"overlapped={sum(1 for n in overlapped if n)} appends_in_windows={sum(overlapped)}"
    )
    print(summary)
    assert not failures, f"{summary}; first: {failures[0]}"
    assert any(overlapped), (
        f"no rebuild overlapped an append: the race was not exercised; {summary}"
    )


def test_offset_rebuild_survives_a_max_rate_appender(source_ledger: Path, tmp_path: Path) -> None:
    windows, times = _run_race(source_ledger, tmp_path, rebuilds=40)
    _assert_no_failures(windows, times)


@pytest.mark.slow
@pytest.mark.perf
def test_offset_rebuild_survives_a_max_rate_appender_200(
    source_ledger: Path, tmp_path: Path
) -> None:
    windows, times = _run_race(source_ledger, tmp_path, rebuilds=200)
    _assert_no_failures(windows, times)


def _candidate_ids(rows: list[dict[str, Any]]) -> list[str]:
    return [str(r["candidate_id"]) for r in rows]


def _seed(tmp_path: Path) -> tuple[CandidateLedger, str]:
    _sidecar_cache_drop()
    ledger = CandidateLedger(tmp_path)
    run = ledger.start_run(document_id="d", source="s", blocks_total=1, model="m")
    for i in range(3):
        ledger.record_candidate(
            run,
            candidate_id=f"c-{i}",
            candidate_kind="node",
            state="proposed",
            payload={"type": "Agent", "title": str(i)},
        )
    mod._invalidate_ledger_offset_index(ledger.path)
    return ledger, run


def test_append_landing_right_after_every_rebuild_never_fails_the_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic: each rebuild is followed at once by an append from another writer."""
    ledger, run = _seed(tmp_path)
    real_build = mod._build_ledger_offset_index
    late = {"n": 0}

    def build(directory: Path, path: Path) -> Any:
        built = real_build(directory, path)
        CandidateLedger(directory).record_candidate(
            run,
            candidate_id=f"late-{late['n']}",
            candidate_kind="node",
            state="proposed",
            payload={"type": "Agent", "title": "late"},
        )
        late["n"] += 1
        return built

    monkeypatch.setattr(mod, "_build_ledger_offset_index", build)
    rows = ledger._indexed_records(kinds={"candidate"})
    monkeypatch.undo()

    assert late["n"] >= 1
    assert _candidate_ids(rows)[:3] == ["c-0", "c-1", "c-2"]  # the snapshot, never torn
    after = _candidate_ids(ledger._indexed_records(kinds={"candidate"}))
    assert after[:3] == ["c-0", "c-1", "c-2"] and "late-0" in after  # the next read sees it


def test_rows_appended_by_another_process_are_caught_up_without_a_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, run = _seed(tmp_path)
    builds = {"n": 0}
    real_build = mod._build_ledger_offset_index

    def counted(directory: Path, path: Path) -> Any:
        builds["n"] += 1
        return real_build(directory, path)

    monkeypatch.setattr(mod, "_build_ledger_offset_index", counted)
    assert _candidate_ids(ledger._indexed_records(kinds={"candidate"})) == ["c-0", "c-1", "c-2"]
    # Bypass this process's append hook, like a writer in another process.
    row = {
        "kind": "candidate",
        "run_id": run,
        "candidate_id": "foreign",
        "candidate_kind": "node",
        "state": "proposed",
        "payload": {"type": "Agent", "title": "f"},
        "ledger_version": mod.LEDGER_VERSION,
    }
    with (tmp_path / LEDGER_FILENAME).open("ab") as fh:
        fh.write(json.dumps(row).encode() + b"\n")
    assert _candidate_ids(ledger._indexed_records(kinds={"candidate"})) == [
        "c-0",
        "c-1",
        "c-2",
        "foreign",
    ]
    assert builds["n"] == 1  # folded in as a tail merge, not a whole-file rebuild


# ---------------------------------------------------------------------------
# Foreign appends are a tail catch-up; only real divergence rebuilds.
# ---------------------------------------------------------------------------

_FOREIGN = r"""
import sys
from pathlib import Path
from okto_neuron.consolidate.ledger import CandidateLedger

ledger = CandidateLedger(Path(sys.argv[1]))
for i in range(int(sys.argv[3])):
    ledger.record_candidate(
        sys.argv[2],
        candidate_id=f"foreign-{sys.argv[4]}-{i}",
        candidate_kind="node",
        state="proposed",
        payload={"type": "Agent", "title": f"foreign {i}"},
    )
"""


def _foreign_append(directory: Path, run: str, count: int, tag: str = "a") -> None:
    subprocess.run(
        [sys.executable, "-c", _FOREIGN, str(directory), run, str(count), tag],
        env=_env(),
        check=True,
        capture_output=True,
        text=True,
    )


def _big_seed(tmp_path: Path) -> tuple[CandidateLedger, str]:
    ledger, run = _seed(tmp_path)
    for i in range(200):  # make a body much larger than the tail that follows
        ledger.record_candidate(
            run,
            candidate_id=f"body-{i}",
            candidate_kind="node",
            state="proposed",
            payload={"type": "Agent", "title": f"body {i}", "summary": "x" * 400},
        )
    return ledger, run


class _Scanned:
    """Bytes the two indexes read while catching up (instrumented row ingestion)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.offset = 0
        self.sidecar = 0
        real_offset = mod._ingest_index_row
        real_sidecar = mod._Sidecar.ingest_raw

        def offset_row(index: Any, offset: int, raw: bytes) -> None:
            self.offset += len(raw)
            real_offset(index, offset, raw)

        def sidecar_row(state: Any, raw: bytes, read_row: Any) -> Any:
            self.sidecar += len(raw)
            return real_sidecar(state, raw, read_row)

        monkeypatch.setattr(mod, "_ingest_index_row", offset_row)
        monkeypatch.setattr(mod._Sidecar, "ingest_raw", sidecar_row)


def _warm(ledger: CandidateLedger) -> None:
    ledger._indexed_records(kinds={"candidate"})
    ledger.run_summaries()


def _truth_ids(ledger: CandidateLedger) -> list[str]:
    return [str(r["candidate_id"]) for r in ledger.iter_records() if r.get("kind") == "candidate"]


def _assert_both_indexes_are_truth(ledger: CandidateLedger) -> None:
    from tests.consolidate.test_ledger_index import assert_index_is_truth

    assert _candidate_ids(ledger._indexed_records(kinds={"candidate"})) == _truth_ids(ledger)
    assert_index_is_truth(ledger)


def test_foreign_appends_scan_only_the_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, run = _big_seed(tmp_path)
    _warm(ledger)
    before = ledger.path.stat().st_size
    _foreign_append(tmp_path, run, 25)
    # This process appends after the foreign rows: its caches now lag by a gap.
    ledger.record_candidate(
        run,
        candidate_id="foreign-a-25",
        candidate_kind="node",
        state="proposed",
        payload={"type": "Agent", "title": "foreign 25"},
    )
    tail = ledger.path.stat().st_size - before
    assert tail > 0 and before > 20 * tail  # the tail really is small next to the body
    scanned = _Scanned(monkeypatch)
    builds = {"n": 0}
    real_build = mod._build_ledger_offset_index

    def counted(directory: Path, path: Path) -> Any:
        builds["n"] += 1
        return real_build(directory, path)

    monkeypatch.setattr(mod, "_build_ledger_offset_index", counted)
    rows = ledger._indexed_records(kinds={"candidate"})
    ledger.run_summaries()
    monkeypatch.undo()

    assert builds["n"] == 0
    assert scanned.offset <= tail + 4096, (scanned.offset, tail)
    assert scanned.sidecar <= tail + 4096, (scanned.sidecar, tail)
    assert scanned.offset > 0 and scanned.sidecar > 0  # the tail was actually folded in
    assert [r["candidate_id"] for r in rows][-26:] == [f"foreign-a-{i}" for i in range(26)]
    _assert_both_indexes_are_truth(ledger)


def _full_rebuild_happens(
    ledger: CandidateLedger, monkeypatch: pytest.MonkeyPatch, body: int
) -> None:
    scanned = _Scanned(monkeypatch)
    builds = {"n": 0}
    real_build = mod._build_ledger_offset_index

    def counted(directory: Path, path: Path) -> Any:
        builds["n"] += 1
        return real_build(directory, path)

    monkeypatch.setattr(mod, "_build_ledger_offset_index", counted)
    _assert_both_indexes_are_truth(ledger)
    monkeypatch.undo()
    assert builds["n"] >= 1
    assert scanned.offset >= body and scanned.sidecar >= body  # a whole-file scan each


def test_inode_change_rebuilds_the_offset_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _run = _big_seed(tmp_path)
    _warm(ledger)
    body = ledger.path.stat().st_size
    copy = tmp_path / "copy"
    copy.write_bytes(ledger.path.read_bytes())
    os.replace(copy, ledger.path)  # same bytes, new inode
    scanned = _Scanned(monkeypatch)
    builds = {"n": 0}
    real_build = mod._build_ledger_offset_index

    def counted(directory: Path, path: Path) -> Any:
        builds["n"] += 1
        return real_build(directory, path)

    monkeypatch.setattr(mod, "_build_ledger_offset_index", counted)
    assert _candidate_ids(ledger._indexed_records(kinds={"candidate"})) == _truth_ids(ledger)
    monkeypatch.undo()
    assert builds["n"] == 1 and scanned.offset >= body


def test_truncation_below_the_cut_rebuilds_both_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, run = _big_seed(tmp_path)
    _warm(ledger)
    data = ledger.path.read_bytes()
    keep = data.rfind(b"\n", 0, len(data) // 2) + 1
    with ledger.path.open("r+b") as fh:
        fh.truncate(keep)
    _foreign_append(tmp_path, run, 400, "t")  # the file is longer than before again
    assert ledger.path.stat().st_size > len(data) - 1
    _full_rebuild_happens(ledger, monkeypatch, body=keep)


def test_append_offset_below_the_cached_size_drops_the_caches(tmp_path: Path) -> None:
    """A row landing below the cached size means the file shrank: drop, never fold it in."""
    ledger, _run = _big_seed(tmp_path)
    _warm(ledger)
    key = str(ledger.path.resolve())
    state = mod._sidecar_cache_get(key)
    assert state is not None
    data = ledger.path.read_bytes()
    start = data.rfind(b"\n", 0, len(data) - 1) + 1
    row = data[start:]
    assert start < state.size
    ledger._sidecar_after_append(start, row, json.loads(row), repaired_tail=False)
    assert mod._sidecar_cache_get(key) is None


def test_prefix_hash_mismatch_rebuilds_both_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, run = _big_seed(tmp_path)
    _warm(ledger)
    data = bytearray(ledger.path.read_bytes())
    marker = data.rfind(b"body-")  # same size, different bytes inside the last 4 KiB
    data[marker : marker + 4] = b"BODY"
    ledger.path.write_bytes(bytes(data))  # same inode, same size
    _foreign_append(tmp_path, run, 3, "h")
    _full_rebuild_happens(ledger, monkeypatch, body=len(data))
