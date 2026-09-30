"""A crash-torn tail repaired between a reader's snapshot and its read.

State: the ledger ends in a torn row T at bytes [c, S). A reader pins the ledger
(under the lock), releases it, and starts reading. Meanwhile a SHORT append takes
the lock, truncates the torn tail back to ``c`` and writes a new row R that is
shorter than T, so all of R lands inside [c, S) and the file size does not
reveal the change. The reader must still return exactly the rows of its snapshot
(nothing from R), report the torn tail it saw, and the next call must see R.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from okto_neuron.consolidate import ledger as mod
from okto_neuron.consolidate.ledger import (
    LEDGER_INDEX_FILENAME,
    CandidateLedger,
    _Sidecar,
    _sidecar_cache_drop,
    _sidecar_cache_get,
)
from tests.consolidate.test_ledger_index import assert_index_is_truth

TORN_BYTES = 4096


def _torn_ledger(tmp_path: Path) -> tuple[CandidateLedger, str, bytes, int]:
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
    cut = ledger.path.stat().st_size
    head = b'{"kind":"candidate","run_id":"' + run.encode() + b'","candidate_id":"torn","payload":"'
    torn = head + b"x" * (TORN_BYTES - len(head))
    with ledger.path.open("ab") as fh:
        fh.write(torn)
    return ledger, run, torn, cut


def _late_append(directory: Path, run: str) -> None:
    """The racing writer: a fresh ledger object, like another request or process."""
    CandidateLedger(directory).record_candidate(
        run,
        candidate_id="late",
        candidate_kind="node",
        state="proposed",
        payload={"type": "Agent", "title": "late"},
    )


def _ids(records: Any) -> list[str]:
    return [r["candidate_id"] for r in records if r.get("kind") == "candidate"]


class _Race:
    """Fires the late append once, right after the reader has released the lock."""

    def __init__(self, directory: Path, run: str) -> None:
        self.directory, self.run, self.fired = directory, run, 0

    def fire(self) -> None:
        if not self.fired:
            self.fired += 1
            _late_append(self.directory, self.run)


def _hook_snapshot(monkeypatch: pytest.MonkeyPatch, race: _Race) -> None:
    original = CandidateLedger._snapshot

    def hooked(self: CandidateLedger) -> Any:
        ctx = original(self)

        class Wrapper:
            def __enter__(inner) -> Any:  # noqa: N805
                value = ctx.__enter__()  # snapshot taken, lock released
                race.fire()
                return value

            def __exit__(inner, *exc: Any) -> Any:  # noqa: N805
                return ctx.__exit__(*exc)

        return Wrapper()

    monkeypatch.setattr(CandidateLedger, "_snapshot", hooked)


def test_scan_returns_its_snapshot_when_the_torn_tail_is_repaired_mid_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, run, torn, cut = _torn_ledger(tmp_path)
    before = ledger.path.read_bytes()
    race = _Race(tmp_path, run)
    _hook_snapshot(monkeypatch, race)

    scan = ledger.scan()

    assert race.fired == 1
    assert _ids(scan.parsed_records) == ["c-0", "c-1", "c-2"]  # R is not in the snapshot
    assert scan.trailing_partial and scan.completeness_status == "incomplete"
    # exactly the bytes the snapshot saw: the complete prefix plus the torn tail as it was
    assert scan.file_size_bytes == len(before)
    assert scan.file_sha256 == hashlib.sha256(before).hexdigest()
    assert before[:cut] + torn == before

    monkeypatch.undo()
    after = ledger.scan()
    assert _ids(after.parsed_records) == ["c-0", "c-1", "c-2", "late"]
    assert after.trailing_partial is False and after.completeness_status == "complete"


def test_iter_records_returns_its_snapshot_when_the_torn_tail_is_repaired_mid_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, run, _torn, _cut = _torn_ledger(tmp_path)
    race = _Race(tmp_path, run)
    _hook_snapshot(monkeypatch, race)

    seen = list(ledger.iter_records())

    assert race.fired == 1
    assert _ids(seen) == ["c-0", "c-1", "c-2"]
    monkeypatch.undo()
    assert _ids(ledger.iter_records()) == ["c-0", "c-1", "c-2", "late"]


def test_sidecar_rebuild_never_covers_a_row_appended_after_its_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, run, _torn, cut = _torn_ledger(tmp_path)
    (tmp_path / LEDGER_INDEX_FILENAME).unlink(missing_ok=True)
    _sidecar_cache_drop()
    race = _Race(tmp_path, run)
    original = _Sidecar.catch_up

    def hooked(self: _Sidecar, handle: Any, read_row: Any, limit: int | None = None) -> None:
        if limit is not None:
            race.fire()  # the lock-free pass has started; a short append repairs the tail
        original(self, handle, read_row, limit)

    monkeypatch.setattr(_Sidecar, "catch_up", hooked)
    ledger._prepare_sidecar()
    monkeypatch.undo()

    assert race.fired == 1
    published = _sidecar_cache_get(str(ledger.path.resolve()))
    assert published is not None and published.size == cut  # stops at the snapshot's last newline
    assert assert_index_is_truth(ledger)["records"] == 5  # run + 3 candidates + late


def test_old_offset_index_rebuild_never_reads_stale_tail_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, run, _torn, _cut = _torn_ledger(tmp_path)
    mod._invalidate_ledger_offset_index(ledger.path)
    race = _Race(tmp_path, run)
    real_lock = mod._exclusive_lock
    state = {"in_build": False, "acquired": 0}
    real_build = mod._build_ledger_offset_index

    def build(directory: Path, path: Path) -> Any:
        state["in_build"], state["acquired"] = True, 0
        try:
            return real_build(directory, path)
        finally:
            state["in_build"] = False

    def lock(path: Path) -> Any:
        if state["in_build"]:
            state["acquired"] += 1
            if state["acquired"] == 2:  # between the snapshot pass and the catch-up
                state["in_build"] = False
                race.fire()
                state["in_build"] = True
        return real_lock(path)

    monkeypatch.setattr(mod, "_build_ledger_offset_index", build)
    monkeypatch.setattr(mod, "_exclusive_lock", lock)
    rows = ledger._indexed_records(kinds={"candidate"})
    monkeypatch.undo()

    assert race.fired == 1
    assert _ids(rows) == ["c-0", "c-1", "c-2", "late"]  # never the torn row's stale bytes
    assert json.loads(ledger.path.read_bytes().splitlines()[-1])["candidate_id"] == "late"


def test_scan_does_not_copy_a_torn_tail_above_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ledger, _run, _torn, cut = _torn_ledger(tmp_path)
    size = ledger.path.stat().st_size
    assert size - cut == TORN_BYTES
    monkeypatch.setattr(mod, "_SCAN_TAIL_CAP", TORN_BYTES - 1)

    with caplog.at_level("WARNING", logger=mod.__name__):
        scan = ledger.scan()

    assert _ids(scan.parsed_records) == ["c-0", "c-1", "c-2"]  # rows up to the last newline
    assert scan.trailing_partial is True and scan.unterminated_final_line is True
    assert scan.completeness_status == "incomplete"
    assert scan.file_sha256 is None  # no claim about bytes that were never read
    assert scan.file_size_bytes == size  # the snapshot size is known without reading
    assert scan.malformed_line_count == 1
    assert [m.reason for m in scan.malformed_lines] == ["tail_over_read_cap"]
    assert any(f"{TORN_BYTES}-byte unterminated tail" in r.getMessage() for r in caplog.records)

    monkeypatch.setattr(mod, "_SCAN_TAIL_CAP", TORN_BYTES)  # exactly at the cap: copied
    assert ledger.scan().file_sha256 == hashlib.sha256(ledger.path.read_bytes()).hexdigest()


def test_over_cap_torn_tail_counts_like_an_under_cap_one(tmp_path: Path) -> None:
    """15 MiB (copied) and 17 MiB (over the 16 MiB cap, not read) count identically."""
    results = {}
    for name, tail_bytes in (("under", 15 * 1024 * 1024), ("over", 17 * 1024 * 1024)):
        directory = tmp_path / name
        directory.mkdir()
        _sidecar_cache_drop()
        ledger = CandidateLedger(directory)
        run = ledger.start_run(document_id="d", source="s", blocks_total=1, model="m")
        head = b'{"kind":"candidate","run_id":"' + run.encode() + b'","candidate_id":"torn","p":"'
        with ledger.path.open("ab") as fh:
            fh.write(head + b"x" * (tail_bytes - len(head)))
        scan = ledger.scan()
        results[name] = scan
        assert scan.file_size_bytes == ledger.path.stat().st_size
    under, over = results["under"], results["over"]
    for field in (
        "malformed_line_count",
        "nonempty_lines",
        "total_lines",
        "trailing_partial",
        "unterminated_final_line",
        "completeness_status",
        "completeness_reason",
    ):
        assert getattr(under, field) == getattr(over, field), field
    assert under.file_sha256 is not None and over.file_sha256 is None
