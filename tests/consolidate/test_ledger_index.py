"""The ledger index sidecar: maintenance, rebuild, crash tolerance, parity.

The sidecar (``candidate-ledger.jsonl.index``) is a cache. These tests pin the
contract that makes it safe: it always equals a fresh scan of the ledger, any
doubt rebuilds it, deleting it changes no answer, and the ledger bytes stay
exactly what the writer always produced.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

import okto_neuron
from okto_neuron.consolidate import ledger as ledger_mod
from okto_neuron.consolidate.ledger import (
    LEDGER_FILENAME,
    LEDGER_INDEX_FILENAME,
    CandidateLedger,
    _exclusive_lock,
    _RowReader,
    _Sidecar,
    _sidecar_cache_drop,
)
from tests.support._ledger_capture import capture
from tests.support._ledger_synth import close_plan, dead_letter_operation

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ledger_replay"
IDS = json.loads((FIXTURES / "ids.json").read_text())
_COMPARED = (
    "size",
    "records",
    "malformed",
    "unrecognized",
    "exotic",
    "anomaly",
    "next_order",
    "runs",
    "plan_runs",
    "open_plans",
    "closed",
)


def _lock(ledger: CandidateLedger) -> Any:
    return _exclusive_lock(Path(ledger.dir) / ".candidate-ledger.lock")


def live_state(ledger: CandidateLedger) -> dict[str, Any]:
    with _lock(ledger):
        state = ledger._sync_sidecar()
    assert state is not None
    return {name: getattr(state, name) for name in _COMPARED}


def rescan_state(ledger: CandidateLedger) -> dict[str, Any]:
    """What a from-scratch streaming scan of the ledger says (the source of truth)."""
    state = _Sidecar()
    reader = _RowReader(ledger.path)
    try:
        with ledger.path.open("rb") as handle:
            state.catch_up(handle, reader)
    finally:
        reader.close()
    return {name: getattr(state, name) for name in _COMPARED}


def assert_index_is_truth(ledger: CandidateLedger) -> dict[str, Any]:
    live = live_state(ledger)
    assert live == rescan_state(ledger)
    return live


def _open_ids(ledger: CandidateLedger) -> list[str]:
    return [plan.plan_id for plan in ledger.unreceipted_commit_plans()]


def build_mixed(ledger: CandidateLedger) -> dict[str, Any]:
    """Interleaved runs; one committed, one abandoned and two open plans."""
    run_a = ledger.start_run(document_id="doc-a", source="a.md", blocks_total=1, model="m")
    run_b = ledger.start_run(document_id="doc-b", source="b.md", blocks_total=1, model="m")
    run_c = ledger.start_run(document_id="doc-c", source="c.md", blocks_total=1, model="m")
    for run in (run_a, run_b, run_c):
        ledger.record_candidate(
            run,
            candidate_id=f"n-{run[:4]}",
            candidate_kind="node",
            state="proposed",
            payload={"type": "Agent", "title": run[:4]},
        )
    committed = ledger.record_commit_plan(
        run_a,
        operations=[dead_letter_operation("e-1"), dead_letter_operation("e-2")],
        context={"document_id": "doc-a"},
    )
    open_b = ledger.record_commit_plan(
        run_b, operations=[dead_letter_operation("e-3")], context={"document_id": "doc-b"}
    )
    abandoned = ledger.record_commit_plan(
        run_c, operations=[dead_letter_operation("e-4")], context={"document_id": "doc-c"}
    )
    close_plan(ledger, run_a, committed)
    plan_c = next(p for p in ledger.unreceipted_commit_plans() if p.plan_id == abandoned)
    ledger.record_plan_abandoned(
        run_c, plan_id=abandoned, plan_hash=plan_c.plan_hash, reason="source_changed"
    )
    open_a = ledger.record_commit_plan(
        run_a,
        operations=[dead_letter_operation("e-5"), dead_letter_operation("e-6")],
        context={"document_id": "doc-a"},
    )
    plan_a = next(p for p in ledger.unreceipted_commit_plans() if p.plan_id == open_a)
    op = plan_a.operations[0]
    ledger.record_operation_receipt(
        run_a,
        plan_id=open_a,
        plan_hash=plan_a.plan_hash,
        operation_id=op["operation_id"],
        operation=op["operation"],
        status="dead_lettered",
        result={"candidate_id": "e-5"},
    )
    ledger.finish_run(run_a, state="completed", summary={"outcome": {"quality": "complete"}})
    return {
        "runs": [run_a, run_b, run_c],
        "committed": committed,
        "abandoned": abandoned,
        "open": [open_b, open_a],
    }


@pytest.fixture()
def ledger(tmp_path: Path) -> CandidateLedger:
    _sidecar_cache_drop()
    return CandidateLedger(tmp_path)


@pytest.fixture()
def catch_up_starts(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Ledger size already covered each time the index scans: 0 means a full rebuild."""
    starts: list[int] = []
    original = _Sidecar.catch_up

    def spy(self: _Sidecar, handle: Any, read_row: Any) -> None:
        starts.append(self.size)
        original(self, handle, read_row)

    monkeypatch.setattr(_Sidecar, "catch_up", spy)
    return starts


def test_ledger_bytes_are_unchanged_by_the_index(tmp_path: Path, monkeypatch) -> None:
    """Re-appending a pre-change ledger's rows reproduces its bytes exactly."""
    source = (FIXTURES / "clean" / LEDGER_FILENAME).read_bytes()
    rows = [json.loads(line) for line in source.splitlines()]
    timestamps = iter(row["ts"] for row in rows)
    monkeypatch.setattr(ledger_mod, "_now", lambda: next(timestamps))
    target = CandidateLedger(tmp_path)
    for row in rows:
        payload = {k: v for k, v in row.items() if k not in ("kind", "ts", "ledger_version")}
        target.append(row["kind"], **payload)
    assert target.path.read_bytes() == source
    _sidecar_cache_drop()
    assert_index_is_truth(target)


def test_index_equals_a_full_scan_after_every_kind_of_append(ledger: CandidateLedger) -> None:
    build_mixed(ledger)
    live = assert_index_is_truth(ledger)
    assert live["anomaly"] is False
    assert len(live["runs"]) == 3
    assert sorted(live["closed"].values()) == ["a", "c"]


def test_open_plan_set_follows_the_plan_lifecycle(ledger: CandidateLedger) -> None:
    ids = build_mixed(ledger)
    assert _open_ids(ledger) == ids["open"]
    plan = next(p for p in ledger.unreceipted_commit_plans() if p.plan_id == ids["open"][0])
    close_plan(ledger, plan.run_id, plan.plan_id)
    assert _open_ids(ledger) == [ids["open"][1]]
    assert set(live_state(ledger)["open_plans"]) == {ids["open"][1]}
    assert_index_is_truth(ledger)


def test_run_spans_cover_exactly_the_runs_rows(ledger: CandidateLedger) -> None:
    ids = build_mixed(ledger)
    live = live_state(ledger)
    raw = ledger.path.read_bytes()
    for run_id in ids["runs"]:
        entry = live["runs"][run_id]
        span = raw[entry["first"] : entry["end"]].splitlines()
        rows = [json.loads(line) for line in span]
        assert rows[0].get("run_id") == run_id and rows[-1].get("run_id") == run_id
        by_run = [
            r for r in (json.loads(line) for line in raw.splitlines()) if r["run_id"] == run_id
        ]
        assert [r for r in rows if r["run_id"] == run_id] == by_run


def test_checkpoint_survives_a_restart_without_a_rebuild(
    ledger: CandidateLedger, catch_up_starts: list[int]
) -> None:
    ids = build_mixed(ledger)
    ledger.write_index_checkpoint()
    index = ledger.index_path
    assert index.name == LEDGER_INDEX_FILENAME and index.is_file()
    size = ledger.path.stat().st_size
    _sidecar_cache_drop()  # a new process
    catch_up_starts.clear()
    assert _open_ids(CandidateLedger(ledger.dir)) == ids["open"]
    assert catch_up_starts == [size]  # loaded from disk, nothing left to scan
    header = json.loads(index.read_bytes().splitlines()[0])
    assert header["index_version"] == 1


def _corrupt_truncate(index: Path) -> None:
    data = index.read_bytes()
    index.write_bytes(data[: len(data) // 2])


def _corrupt_garbage(index: Path) -> None:
    index.write_bytes(os.urandom(512))


def _corrupt_empty(index: Path) -> None:
    index.write_bytes(b"")


def _corrupt_version(index: Path) -> None:
    header, body, _ = index.read_bytes().split(b"\n")
    bumped = json.loads(header)
    bumped["index_version"] = 99
    index.write_bytes(json.dumps(bumped).encode() + b"\n" + body + b"\n")


def _corrupt_body_flip(index: Path) -> None:
    header, body, _ = index.read_bytes().split(b"\n")
    index.write_bytes(header + b"\n" + body.replace(b"doc", b"dox", 1) + b"\n")
    if index.read_bytes().split(b"\n")[1] == body:  # no "doc" in the body: flip a digit
        index.write_bytes(header + b"\n" + body.replace(b"0", b"1", 1) + b"\n")


def _corrupt_wrong_types(index: Path) -> None:
    header, body, _ = index.read_bytes().split(b"\n")
    doc = json.loads(body)
    doc["runs"] = ["not", "a", "table"]
    new_body = json.dumps(doc, separators=(",", ":")).encode()
    head = json.loads(header)
    import hashlib

    head["body_sha256"] = hashlib.sha256(new_body).hexdigest()  # checksum is right, shape is not
    index.write_bytes(json.dumps(head).encode() + b"\n" + new_body + b"\n")


def _missing(index: Path) -> None:
    index.unlink()


@pytest.mark.parametrize(
    "damage",
    [
        _missing,
        _corrupt_truncate,
        _corrupt_garbage,
        _corrupt_empty,
        _corrupt_version,
        _corrupt_body_flip,
        _corrupt_wrong_types,
    ],
    ids=lambda fn: fn.__name__.lstrip("_"),
)
def test_missing_or_damaged_index_is_rebuilt_from_the_ledger(
    ledger: CandidateLedger, catch_up_starts: list[int], damage: Any
) -> None:
    ids = build_mixed(ledger)
    ledger.write_index_checkpoint()
    truth = rescan_state(ledger)
    ledger_bytes = ledger.path.read_bytes()
    damage(ledger.index_path) if ledger.index_path.exists() else None
    _sidecar_cache_drop()
    catch_up_starts.clear()

    fresh = CandidateLedger(ledger.dir)
    assert _open_ids(fresh) == ids["open"]

    assert catch_up_starts[0] == 0  # full streaming rebuild, not a partial trust
    assert live_state(fresh) == truth
    assert ledger.path.read_bytes() == ledger_bytes  # the ledger is never touched
    assert _Sidecar.from_bytes(ledger.index_path.read_bytes()) is not None  # rewritten, valid


def test_deleting_the_index_changes_no_answer(ledger: CandidateLedger) -> None:
    ids = build_mixed(ledger)
    ledger.write_index_checkpoint()
    with_index = capture(ledger, ids["runs"])
    ledger.index_path.unlink()
    _sidecar_cache_drop()
    without = capture(CandidateLedger(ledger.dir), ids["runs"])
    assert with_index == without


@pytest.mark.parametrize("lost", ["commit_plan", "operation_receipt", "commit_record"])
def test_crash_between_ledger_append_and_index_update(
    ledger: CandidateLedger,
    catch_up_starts: list[int],
    monkeypatch: pytest.MonkeyPatch,
    lost: str,
) -> None:
    """The row is durable but the index update dies: the next open catches up."""
    run = ledger.start_run(document_id="doc-x", source="x.md", blocks_total=1, model="m")
    closed = ledger.record_commit_plan(
        run, operations=[dead_letter_operation("x-1")], context={"document_id": "doc-x"}
    )
    ledger.write_index_checkpoint()
    original = _Sidecar.apply

    def dying(self: _Sidecar, offset: int, length: int, record: dict[str, Any], rr: Any) -> None:
        if record.get("kind") == lost:
            raise RuntimeError("process died between the ledger write and the index update")
        original(self, offset, length, record, rr)

    with monkeypatch.context() as patched:
        patched.setattr(_Sidecar, "apply", dying)
        if lost == "commit_plan":
            new_plan = ledger.record_commit_plan(  # append() must not surface the index failure
                run, operations=[dead_letter_operation("x-2")], context={"document_id": "doc-x"}
            )
            expected_open = [closed, new_plan]
        elif lost == "operation_receipt":
            plan = ledger.unreceipted_commit_plans()[0]
            op = plan.operations[0]
            ledger.record_operation_receipt(
                run,
                plan_id=plan.plan_id,
                plan_hash=plan.plan_hash,
                operation_id=op["operation_id"],
                operation=op["operation"],
                status="dead_lettered",
                result={},
            )
            expected_open = [closed]
        else:
            close_plan(ledger, run, closed)
            expected_open = []

    _sidecar_cache_drop()  # restart
    catch_up_starts.clear()
    reopened = CandidateLedger(ledger.dir)
    assert _open_ids(reopened) == expected_open
    assert catch_up_starts and catch_up_starts[0] > 0  # only the tail was scanned
    assert_index_is_truth(reopened)


def test_ledger_row_written_behind_the_indexs_back_is_picked_up(ledger: CandidateLedger) -> None:
    ids = build_mixed(ledger)
    ledger.write_index_checkpoint()
    _sidecar_cache_drop()
    # A crashed writer: the row reached the file, the index never saw it.
    plan = next(p for p in ledger.unreceipted_commit_plans() if p.plan_id == ids["open"][0])
    row = {
        "kind": "plan_abandoned",
        "ts": "t",
        "run_id": plan.run_id,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "reason": "manual",
        "evidence": {},
        "ledger_version": 2,
    }
    with ledger.path.open("ab") as fh:
        fh.write(json.dumps(row, separators=(",", ":")).encode() + b"\n")
    _sidecar_cache_drop()
    assert _open_ids(CandidateLedger(ledger.dir)) == [ids["open"][1]]
    assert_index_is_truth(ledger)


def test_a_process_local_state_catches_up_with_foreign_appends(ledger: CandidateLedger) -> None:
    ids = build_mixed(ledger)
    assert _open_ids(ledger) == ids["open"]  # warm in-process state
    script = (
        "import sys; from pathlib import Path\n"
        "from okto_neuron.consolidate.ledger import CandidateLedger\n"
        "led = CandidateLedger(Path(sys.argv[1]))\n"
        "run = led.start_run(document_id='doc-z', source='z.md', blocks_total=1, model='m')\n"
        "plan = led.record_commit_plan(run, operations=[{'operation': 'dead_letter',"
        " 'candidate_kind': 'edge', 'candidate_id': 'z', 'candidate': {'type': 't'},"
        " 'reason': 'r'}], context={'document_id': 'doc-z'})\n"
        "print(plan)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(okto_neuron.__file__).resolve().parent.parent)}
    out = subprocess.run(
        [sys.executable, "-c", script, str(ledger.dir)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert _open_ids(ledger) == [*ids["open"], out.stdout.strip()]
    assert_index_is_truth(ledger)


def _rows(ledger: CandidateLedger) -> list[bytes]:
    return ledger.path.read_bytes().splitlines(keepends=True)


def test_a_shorter_ledger_forces_a_rebuild(
    ledger: CandidateLedger, catch_up_starts: list[int]
) -> None:
    ids = build_mixed(ledger)
    ledger.write_index_checkpoint()
    rows = _rows(ledger)
    # drop the last two rows (the receipt of an open plan and the run-completed row)
    ledger.path.write_bytes(b"".join(rows[:-2]))
    _sidecar_cache_drop()
    catch_up_starts.clear()
    fresh = CandidateLedger(ledger.dir)
    assert _open_ids(fresh) == ids["open"]
    assert catch_up_starts[0] == 0
    assert_index_is_truth(fresh)


@pytest.mark.parametrize("longer", [False, True])
def test_a_rewritten_ledger_forces_a_rebuild(
    ledger: CandidateLedger, catch_up_starts: list[int], longer: bool
) -> None:
    build_mixed(ledger)
    ledger.write_index_checkpoint()
    rows = _rows(ledger)
    # rewrite: swap the last two rows' bytes for other (still valid) content
    last = json.loads(rows[-1])
    last["ts"] = "rewritten"
    body = b"".join(rows[:-1]) + json.dumps(last, separators=(",", ":")).encode() + b"\n"
    if longer:
        body += body[len(body) // 2 :]  # grows, and the covered tail no longer matches
    ledger.path.write_bytes(body)
    _sidecar_cache_drop()
    catch_up_starts.clear()
    fresh = CandidateLedger(ledger.dir)
    try:
        _open_ids(fresh)
    except ValueError:
        pass  # the duplicated half may legitimately be an invalid ledger (plans twice)
    assert catch_up_starts[0] == 0
    assert live_state(fresh) == rescan_state(fresh)


def test_in_process_state_notices_a_truncation(ledger: CandidateLedger) -> None:
    ids = build_mixed(ledger)
    assert _open_ids(ledger) == ids["open"]  # warm state covers the whole ledger
    rows = _rows(ledger)
    ledger.path.write_bytes(b"".join(rows[:-2]))  # truncated behind the cache
    assert_index_is_truth(ledger)


def test_unterminated_tail_is_left_uncovered(ledger: CandidateLedger) -> None:
    build_mixed(ledger)
    with ledger.path.open("ab") as fh:
        fh.write(b'{"kind":"candidate","run_id":"r","candidate_id":"c-torn","pay')
    live = live_state(ledger)
    assert live["size"] == ledger.path.stat().st_size - len(
        b'{"kind":"candidate","run_id":"r","candidate_id":"c-torn","pay'
    )
    # whole-file readers answer; the apply-resume reader repairs the tail and continues
    assert len(ledger.run_summaries(limit=50)) == 3
    assert len(ledger.unreceipted_commit_plans()) == 2
    assert_index_is_truth(ledger)


def test_run_readers_do_not_scan_the_whole_ledger(
    ledger: CandidateLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = build_mixed(ledger)
    expected = capture(ledger, ids["runs"])
    calls: list[str] = []

    def forbidden(self: CandidateLedger) -> Any:
        calls.append("whole-file read")
        raise AssertionError("the run readers must seek to the run's offsets")

    monkeypatch.setattr(CandidateLedger, "iter_records", forbidden)
    monkeypatch.setattr(CandidateLedger, "records", forbidden)
    reads: list[str] = []
    original = ledger_mod._RunView.records

    def spy(self: Any, run_id: str) -> Any:
        reads.append(run_id)
        return original(self, run_id)

    original_reduced = ledger_mod._RunView.reduced_records

    def spy_reduced(self: Any, run_id: str) -> Any:
        reads.append(run_id)
        return original_reduced(self, run_id)

    monkeypatch.setattr(ledger_mod._RunView, "records", spy)
    monkeypatch.setattr(ledger_mod._RunView, "reduced_records", spy_reduced)
    top = ledger.run_summaries(limit=1)
    assert len(top) == 1 and len(reads) == 1  # only the newest run's span was read
    reads.clear()
    assert ledger.run_detail(ids["runs"][1]) is not None
    assert reads and set(reads) == {ids["runs"][1]}
    assert not calls
    monkeypatch.undo()
    assert capture(ledger, ids["runs"]) == expected


@pytest.mark.parametrize("variant", ["present", "missing", "corrupt"])
@pytest.mark.parametrize("name", ["clean", "damaged"])
def test_fixture_replay_is_identical_with_any_index_state(
    tmp_path: Path, name: str, variant: str
) -> None:
    _sidecar_cache_drop()
    shutil.copy(FIXTURES / name / LEDGER_FILENAME, tmp_path / LEDGER_FILENAME)
    expected = json.loads((FIXTURES / name / "expected.json").read_text())
    ledger = CandidateLedger(tmp_path)
    if variant != "missing":
        ledger.write_index_checkpoint()
    if variant == "corrupt":
        ledger.index_path.write_bytes(b"\x00\x01not an index")
    _sidecar_cache_drop()
    got = capture(CandidateLedger(tmp_path), IDS["runs"])
    for key in expected:
        assert got[key] == expected[key], (name, variant, key)


# --- anomalies: the exact verdicts of the whole-ledger validation ------------


def _append_raw(ledger: CandidateLedger, row: dict[str, Any]) -> None:
    with ledger.path.open("ab") as fh:
        fh.write(json.dumps(row, separators=(",", ":")).encode() + b"\n")


def _plan_rows(ledger: CandidateLedger) -> list[dict[str, Any]]:
    return [json.loads(line) for line in _rows(ledger) if b'"commit_plan"' in line]


def test_anomaly_duplicate_plan_id_raises_the_original_error(ledger: CandidateLedger) -> None:
    ids = build_mixed(ledger)
    _append_raw(ledger, _plan_rows(ledger)[-1])
    _sidecar_cache_drop()
    with pytest.raises(ValueError, match=f"duplicate commit plan id: {ids['open'][1]}"):
        CandidateLedger(ledger.dir).unreceipted_commit_plans()


def test_anomaly_receipt_without_plan_raises_the_original_error(ledger: CandidateLedger) -> None:
    build_mixed(ledger)
    row = next(json.loads(x) for x in _rows(ledger) if b'"operation_receipt"' in x)
    row["plan_id"] = "ghost"
    _append_raw(ledger, row)
    with pytest.raises(ValueError, match="operation receipt precedes or lacks plan: ghost"):
        ledger.unreceipted_commit_plans()


def test_anomaly_late_receipt_after_commit_raises_the_original_error(
    ledger: CandidateLedger,
) -> None:
    ids = build_mixed(ledger)
    late = next(
        json.loads(x)
        for x in _rows(ledger)
        if b'"operation_receipt"' in x and ids["committed"].encode() in x
    )
    _append_raw(ledger, late)
    with pytest.raises(ValueError, match=f"duplicate operation receipt: {ids['committed']}"):
        ledger.unreceipted_commit_plans()


def test_anomaly_second_commit_record_raises_the_original_error(ledger: CandidateLedger) -> None:
    ids = build_mixed(ledger)
    commit = next(json.loads(x) for x in _rows(ledger) if b'"commit_record"' in x)
    _append_raw(ledger, commit)
    with pytest.raises(ValueError, match=f"duplicate commit receipt: {ids['committed']}"):
        ledger.unreceipted_commit_plans()


def test_anomaly_forged_plan_digest_raises_when_the_plan_is_closed(
    ledger: CandidateLedger,
) -> None:
    """A bad closed plan is reported by every later call, like the old full validation."""
    run = ledger.start_run(document_id="d", source="s", blocks_total=1, model="m")
    plan_id = ledger.record_commit_plan(
        run, operations=[dead_letter_operation("f-1")], context={"document_id": "d"}
    )
    plan = ledger.unreceipted_commit_plans()[0]
    op = plan.operations[0]
    ledger.record_operation_receipt(
        run,
        plan_id=plan_id,
        plan_hash=plan.plan_hash,
        operation_id=op["operation_id"],
        operation=op["operation"],
        status="dead_lettered",
        result={},
    )
    ledger.record_commit(run, plan_id=plan_id, result={})
    lines = _rows(ledger)
    idx = next(i for i, x in enumerate(lines) if b'"commit_plan"' in x)
    row = json.loads(lines[idx])
    row["context"]["tampered"] = True  # digest no longer matches
    lines[idx] = json.dumps(row, separators=(",", ":")).encode() + b"\n"
    ledger.path.write_bytes(b"".join(lines))
    for _ in range(2):
        _sidecar_cache_drop()
        with pytest.raises(ValueError, match=f"commit plan digest mismatch: {plan_id}"):
            CandidateLedger(ledger.dir).unreceipted_commit_plans()


def test_anomaly_commit_after_abandon_raises_the_original_error(ledger: CandidateLedger) -> None:
    ids = build_mixed(ledger)
    commit = next(json.loads(x) for x in _rows(ledger) if b'"commit_record"' in x)
    plan_c = next(r for r in _plan_rows(ledger) if r["plan_id"] == ids["abandoned"])
    commit.update(plan_id=ids["abandoned"], run_id=plan_c["run_id"])
    _append_raw(ledger, commit)
    with pytest.raises(ValueError, match=f"commit receipt hash mismatch: {ids['abandoned']}"):
        ledger.unreceipted_commit_plans()


def test_anomaly_receipt_after_abandon_raises_the_original_error(ledger: CandidateLedger) -> None:
    ids = build_mixed(ledger)
    receipt = next(json.loads(x) for x in _rows(ledger) if b'"operation_receipt"' in x)
    plan_c = next(r for r in _plan_rows(ledger) if r["plan_id"] == ids["abandoned"])
    op = plan_c["operations"][0]
    receipt.update(
        run_id=plan_c["run_id"],
        plan_id=ids["abandoned"],
        plan_hash=plan_c["plan_hash"],
        operation_id=op["operation_id"],
        operation=op["operation"],
    )
    _append_raw(ledger, receipt)
    with pytest.raises(
        ValueError, match=f"abandoned plan has operation receipts: {ids['abandoned']}"
    ):
        ledger.unreceipted_commit_plans()


# --- concurrency -------------------------------------------------------------


def test_concurrent_appends_keep_the_index_equal_to_a_rescan(ledger: CandidateLedger) -> None:
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            led = CandidateLedger(ledger.dir)
            for i in range(6):
                run = led.start_run(
                    document_id=f"doc-{n}-{i}", source="s", blocks_total=1, model="m"
                )
                led.record_candidate(
                    run,
                    candidate_id=f"c-{n}-{i}",
                    candidate_kind="node",
                    state="proposed",
                    payload={"type": "Agent", "title": f"{n}-{i}"},
                )
                plan_id = led.record_commit_plan(
                    run,
                    operations=[dead_letter_operation(f"e-{n}-{i}")],
                    context={"document_id": f"doc-{n}-{i}"},
                )
                if i % 2 == 0:
                    close_plan(led, run, plan_id)
                    led.finish_run(run, state="completed", summary={})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    live = assert_index_is_truth(ledger)
    assert len(live["open_plans"]) == 6 * 3
    assert len(ledger.unreceipted_commit_plans()) == 18
    _sidecar_cache_drop()
    assert live_state(CandidateLedger(ledger.dir)) == live


def test_appends_from_two_processes_and_threads_keep_the_index_consistent(
    ledger: CandidateLedger,
) -> None:
    script = (
        "import sys; from pathlib import Path\n"
        "from okto_neuron.consolidate.ledger import CandidateLedger\n"
        "led = CandidateLedger(Path(sys.argv[1]))\n"
        "for i in range(15):\n"
        "    run = led.start_run(document_id=f'p-{i}', source='s', blocks_total=1, model='m')\n"
        "    led.record_commit_plan(run, operations=[{'operation': 'dead_letter',"
        " 'candidate_kind': 'edge', 'candidate_id': f'p-{i}', 'candidate': {'type': 't'},"
        " 'reason': 'r'}], context={'document_id': f'p-{i}'})\n"
    )
    env = {**os.environ, "PYTHONPATH": str(Path(okto_neuron.__file__).resolve().parent.parent)}
    proc = subprocess.Popen([sys.executable, "-c", script, str(ledger.dir)], env=env)

    def threaded() -> None:
        led = CandidateLedger(ledger.dir)
        for i in range(15):
            run = led.start_run(document_id=f"t-{i}", source="s", blocks_total=1, model="m")
            plan_id = led.record_commit_plan(
                run, operations=[dead_letter_operation(f"t-{i}")], context={"document_id": "t"}
            )
            close_plan(led, run, plan_id)
            led.unreceipted_commit_plans()  # readers interleave with the other process

    threads = [threading.Thread(target=threaded) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert proc.wait(timeout=120) == 0
    live = assert_index_is_truth(ledger)
    assert len(live["open_plans"]) == 15
    assert len(ledger.unreceipted_commit_plans()) == 15
    _sidecar_cache_drop()
    assert live_state(CandidateLedger(ledger.dir)) == live
