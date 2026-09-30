"""Replay parity: ledgers written by the pre-index code read back identically.

The fixtures under ``tests/fixtures/ledger_replay`` were produced by the ledger
implementation as it was before the read-path rework (base commit of
``fix/14b-ledger-index``): the ledger bytes plus every public reader's output,
captured from that code. Each test copies a fixture into a temp dir, runs the
current readers and requires byte-identical JSON. ``damaged`` carries blank,
non-object, non-JSON and unknown-version rows; ``torn_tail`` ends in an
unterminated row that the apply-resume reader repairs.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from okto_neuron.consolidate.ledger import (
    FRESH_REBUILD_MATERIALIZATION_SCOPE,
    LEDGER_FILENAME,
    CandidateLedger,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ledger_replay"
IDS = json.loads((FIXTURES / "ids.json").read_text())


def _dump(obj: Any) -> Any:
    return json.loads(
        json.dumps(
            obj,
            default=lambda v: (
                str(v)
                if isinstance(v, Path)
                else dataclasses.asdict(v)
                if dataclasses.is_dataclass(v)
                else sorted(v)
                if isinstance(v, set)
                else repr(v)
            ),
        )
    )


def _snap(plan: Any) -> dict[str, Any]:
    return dict(
        run_id=plan.run_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        operations=[dict(op) for op in plan.operations],
        context=plan.context,
    )


def capture(ledger: CandidateLedger, runs: list[str]) -> dict[str, Any]:
    """Every public reader, in the order the fixtures were captured."""
    res: dict[str, Any] = {}
    res["records"] = ledger.records()
    scan = dataclasses.asdict(ledger.scan())
    scan["path"] = "<path>"
    res["scan"] = _dump(scan)
    res["run_summaries"] = {str(n): ledger.run_summaries(limit=n) for n in (1, 2, 500)}
    res["run_detail"] = {r: ledger.run_detail(r) for r in runs}
    res["run_detail"]["missing"] = ledger.run_detail("missing")
    res["progress"] = {r: ledger.run_progress_summary(r, limit=5) for r in runs}
    res["progress"]["latest"] = ledger.run_progress_summary(None, limit=5)
    res["progress"]["missing"] = ledger.run_progress_summary("missing", limit=5)
    try:
        res["unreceipted"] = {
            "all": [_snap(p) for p in ledger.unreceipted_commit_plans()],
            "doc-b": [_snap(p) for p in ledger.unreceipted_commit_plans(document_id="doc-b")],
            "doc-a": [_snap(p) for p in ledger.unreceipted_commit_plans(document_id="doc-a")],
        }
        res["receipts"] = {
            p.plan_id: ledger.operation_receipts(p) for p in ledger.unreceipted_commit_plans()
        }
        res["decision_runs"] = list(
            ledger.find_completed_decision_runs(
                document_id="doc-a",
                blocks_total=2,
                model="m1",
                config_fingerprint="cf1",
                extraction_fingerprint="ef1",
                semantic_policy_fingerprint="sp1",
                materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
            )
        )
    except ValueError as exc:
        res["unreceipted_error"] = str(exc)
    res["open_runs"] = {k: v.get("document_id") for k, v in ledger._open_runs().items()}
    res["size_after"] = ledger.path.stat().st_size
    return _dump(res)


def _load(name: str, tmp_path: Path) -> tuple[CandidateLedger, dict[str, Any]]:
    shutil.copy(FIXTURES / name / LEDGER_FILENAME, tmp_path / LEDGER_FILENAME)
    expected = json.loads((FIXTURES / name / "expected.json").read_text())
    return CandidateLedger(tmp_path), expected


def test_clean_ledger_replays_identically(tmp_path: Path) -> None:
    ledger, expected = _load("clean", tmp_path)
    got = capture(ledger, IDS["runs"])
    assert set(got) == set(expected)
    for key in expected:
        assert got[key] == expected[key], key
    # the open plans of the fixture are the two unreceipted ones
    assert [p["plan_id"] for p in got["unreceipted"]["all"]] == [IDS["plans"][1], IDS["plans"][3]]


def test_damaged_ledger_replays_identically(tmp_path: Path) -> None:
    ledger, expected = _load("damaged", tmp_path)
    got = capture(ledger, IDS["runs"])
    assert got["unreceipted_error"].startswith("cannot resume apply from an invalid")
    for key in expected:
        assert got[key] == expected[key], key


def test_torn_tail_replays_identically(tmp_path: Path) -> None:
    ledger, expected = _load("torn_tail", tmp_path)
    scan = dataclasses.asdict(ledger.scan())
    scan["path"] = "<path>"
    assert _dump(scan) == expected["scan_before_repair"]
    assert ledger.records() == expected["records_before_repair"]
    got = capture(ledger, IDS["runs"])
    for key in expected["after"]:
        assert got[key] == expected["after"][key], key


@pytest.mark.parametrize("name", ["clean", "damaged"])
def test_replay_is_repeatable_on_one_instance(name: str, tmp_path: Path) -> None:
    """A second pass over the same ledger (warm caches) answers the same.

    ``torn_tail`` is excluded: its first pass repairs the file by design.
    """
    ledger, _ = _load(name, tmp_path)
    first = capture(ledger, IDS["runs"])
    second = capture(ledger, IDS["runs"])
    first.pop("size_after"), second.pop("size_after")
    assert first == second
