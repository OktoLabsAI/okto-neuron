"""Streaming ledger readers: same answers as the whole-file readers, bounded memory."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import okto_neuron
from okto_neuron.consolidate.ledger import (
    LEDGER_FILENAME,
    CandidateLedger,
    _stream_lines,
)
from tests.support._ledger_synth import build_synthetic_ledger

_SEPARATORS_BYTES = [b"\n", b"\r", b"\r\n", b"\x0b", b"\x0c", b"\x1c", b"\x85", b""]
_SEPARATORS_TEXT = ["\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c\x1d", "\x85", " ", " ", ""]


def _chunked(data: Any, rng: random.Random) -> list[Any]:
    out = []
    i = 0
    while i < len(data):
        step = rng.choice([1, 2, 3, 7, 64])
        out.append(data[i : i + step])
        i += step
    return out


@pytest.mark.parametrize("seed", range(40))
def test_stream_lines_equals_splitlines_for_any_chunking(seed: int) -> None:
    """The chunked splitter is ``bytes``/``str.splitlines`` whatever the read sizes."""
    rng = random.Random(seed)
    pieces = [rng.choice(["a", "bc", "json", "x y", ""]) for _ in range(rng.randint(0, 25))]
    data_b = b"".join(p.encode() + rng.choice(_SEPARATORS_BYTES) for p in pieces)
    data_t = "".join(p + rng.choice(_SEPARATORS_TEXT) for p in pieces)
    assert list(_stream_lines(_chunked(data_b, rng))) == data_b.splitlines()
    assert list(_stream_lines(_chunked(data_t, rng))) == data_t.splitlines()
    assert list(_stream_lines([])) == []


def test_stream_lines_holds_one_long_line_without_quadratic_joins() -> None:
    chunk = b"x" * 65536
    lines = list(_stream_lines([chunk] * 200 + [b"\n", b"tail"]))
    assert [len(line) for line in lines] == [65536 * 200, 4]


def _write(tmp_path: Path, payload: bytes) -> CandidateLedger:
    (tmp_path / LEDGER_FILENAME).write_bytes(payload)
    return CandidateLedger(tmp_path)


def test_iter_records_matches_records_and_skips_what_records_skips(tmp_path: Path) -> None:
    ledger = _write(
        tmp_path,
        b'{"kind":"a","n":1}\n\n   \n[1]\n"s"\n{bad\n{"kind":"b","n":2}\r\n'
        b'{"kind":"c","n":3}\x0b{"kind":"d"}\n{"kind":"e","n":5}',
    )
    expected = ledger.records()
    # str.splitlines also breaks on \x0b, exactly as the old read_text().splitlines() did.
    assert [r["kind"] for r in expected] == ["a", "b", "c", "d", "e"]
    assert list(ledger.iter_records()) == expected
    assert isinstance(ledger.records(), list)


def test_iter_records_is_lazy_and_absent_ledger_is_empty(tmp_path: Path) -> None:
    assert list(CandidateLedger(tmp_path).iter_records()) == []
    ledger = _write(tmp_path, b'{"kind":"a"}\n{"kind":"b"}\n')
    iterator = ledger.iter_records()
    assert next(iterator)["kind"] == "a"


def test_invalid_utf8_fails_like_the_whole_file_reader(tmp_path: Path) -> None:
    """``records()`` historically raises on invalid UTF-8; it still does."""
    ledger = _write(tmp_path, b'{"kind":"a"}\n{"kind":"\xff\xfe"}\n')
    with pytest.raises(UnicodeDecodeError):
        ledger.records()
    with pytest.raises(UnicodeDecodeError):
        list(ledger.iter_records())


def test_scan_kinds_filters_retained_rows_only(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    run_id = ledger.start_run(document_id="d", source="s", blocks_total=1, model="m")
    ledger.record_candidate(
        run_id, candidate_id="c1", candidate_kind="node", state="proposed", payload={"t": "x"}
    )
    with (tmp_path / LEDGER_FILENAME).open("ab") as fh:
        fh.write(b"{broken\n")
    full = ledger.scan()
    only_runs = ledger.scan(kinds=frozenset({"ingest_run"}))
    assert [r["kind"] for r in only_runs.parsed_records] == ["ingest_run"]
    assert [r["kind"] for r in full.parsed_records] == ["ingest_run", "candidate"]
    for field in (
        "total_lines",
        "nonempty_lines",
        "malformed_line_count",
        "file_size_bytes",
        "file_sha256",
        "completeness_status",
        "completeness_reason",
        "ledger_versions",
        "unrecognized_version_record_count",
        "trailing_partial",
    ):
        assert getattr(full, field) == getattr(only_runs, field), field


_PROBE = r"""
import json, resource, sys
from pathlib import Path
from okto_neuron.consolidate.ledger import CandidateLedger

directory, mode = Path(sys.argv[1]), sys.argv[2]
ledger = CandidateLedger(directory)

def rss():
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024  # darwin: bytes, linux: KiB

base = rss()
if mode == "iter_records":
    n = sum(1 for _ in ledger.iter_records())
elif mode == "scan_runs":
    n = ledger.scan(kinds=frozenset({"ingest_run"})).nonempty_lines
else:
    raise SystemExit("unknown mode " + mode)
print(json.dumps({"base": base, "peak": rss(), "n": n}))
"""


def probe(directory: Path, mode: str) -> dict[str, int]:
    """Run one reader in a fresh interpreter and report its peak-RSS growth."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(okto_neuron.__file__).resolve().parent.parent)
    out = subprocess.run(
        [sys.executable, "-c", _PROBE, str(directory), mode],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    row = json.loads(out.stdout.strip().splitlines()[-1])
    return {"growth": row["peak"] - row["base"], "n": row["n"]}


@pytest.fixture(scope="module")
def quick_ledger(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("quick-ledger")
    build_synthetic_ledger(directory, target_bytes=25_000_000)
    return directory


_QUICK_BOUND = 40 * 1024 * 1024


@pytest.mark.parametrize("mode", ["iter_records", "scan_runs"])
def test_streaming_readers_stay_bounded_on_a_25mb_ledger(quick_ledger: Path, mode: str) -> None:
    size = (quick_ledger / LEDGER_FILENAME).stat().st_size
    assert size >= 25_000_000
    result = probe(quick_ledger, mode)
    assert result["n"] > 0
    assert result["growth"] < _QUICK_BOUND, (mode, result, size)
