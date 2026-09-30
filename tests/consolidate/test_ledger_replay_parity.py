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

from okto_neuron.consolidate.ledger import LEDGER_FILENAME, CandidateLedger
from tests.support._ledger_capture import capture, dump as _dump

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "ledger_replay"
IDS = json.loads((FIXTURES / "ids.json").read_text())


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
