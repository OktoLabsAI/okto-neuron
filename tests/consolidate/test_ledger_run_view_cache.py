"""The kind-counting run summaries never re-parse a row they already read.

A commit plan can be tens of MiB and ``json.loads`` holds the GIL for the whole call,
so a UI poll that re-parsed it every few seconds would stall the event loop. The run
view keeps a reduced copy per run, keyed by the span it covers (the ledger is
append-only), and a grown run parses only the bytes appended since.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from okto_neuron.consolidate import ledger as mod
from okto_neuron.consolidate.ledger import CandidateLedger, _sidecar_cache_drop
from tests.support._ledger_synth import build_synthetic_ledger

_BIG = 1_000_000


@pytest.fixture
def ledger(tmp_path: Path) -> CandidateLedger:
    build_synthetic_ledger(
        tmp_path,
        target_bytes=3_000_000,
        row_chars=2_000,
        open_plans=1,
        plan_ops=6,
        op_chars=200_000,
    )
    _sidecar_cache_drop()
    return CandidateLedger(tmp_path)


class _Loads:
    """Records the size of every json.loads call (str or bytes input)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sizes: list[int] = []
        real = json.loads

        def loads(s: Any, *args: Any, **kwargs: Any) -> Any:
            self.sizes.append(len(s))
            return real(s, *args, **kwargs)

        monkeypatch.setattr(mod.json, "loads", loads)

    def reset(self) -> None:
        self.sizes.clear()

    @property
    def big(self) -> int:
        return sum(1 for n in self.sizes if n > _BIG)


def _uncached(ledger: CandidateLedger, monkeypatch: pytest.MonkeyPatch, call: Any) -> Any:
    """The same question answered by the whole-file streaming readers (no run view)."""
    with monkeypatch.context() as patch:
        patch.setattr(CandidateLedger, "_prepare_sidecar", lambda self: False)
        return call(ledger)


def _append_row(ledger: CandidateLedger, run_id: str, n: int) -> None:
    ledger.record_candidate(
        run_id,
        candidate_id=f"grow-{n}",
        candidate_kind="node",
        state="proposed",
        payload={"type": "Agent", "title": f"grow {n}"},
    )


def test_warm_calls_on_an_unchanged_ledger_parse_nothing(
    ledger: CandidateLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    loads = _Loads(monkeypatch)
    first = ledger.run_progress_summary(None, limit=12)
    assert first is not None
    assert loads.big >= 1, "the synthetic run has no row above 1 MB: the test proves nothing"
    loads.reset()
    for _ in range(5):
        assert ledger.run_progress_summary(None, limit=12) == first
    assert loads.big == 0 and loads.sizes == [], loads.sizes


def test_a_grown_run_parses_only_its_new_rows_never_the_old_big_row(
    ledger: CandidateLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    loads = _Loads(monkeypatch)
    first = ledger.run_progress_summary(None, limit=12)
    assert first is not None
    run_id = first["run"]["run_id"]
    loads.reset()
    for n in range(3):
        _append_row(ledger, run_id, n)
        loads.reset()
        grown = ledger.run_progress_summary(None, limit=12)
        assert loads.big == 0, loads.sizes
        assert len(loads.sizes) == 1, f"one appended row means one parse, got {loads.sizes}"
        assert grown == _uncached(
            ledger, monkeypatch, lambda ledger: ledger.run_progress_summary(None, limit=12)
        )
    assert grown["counts"]["candidate_rows"] == first["counts"]["candidate_rows"] + 3


def test_cached_answers_equal_the_uncached_ones(
    ledger: CandidateLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    cached = [
        ledger.run_progress_summary(None, limit=12),
        ledger.run_summaries(limit=50),
        ledger.run_detail(ledger.run_summaries(limit=1)[0]["run_id"]),
    ]
    again = [
        ledger.run_progress_summary(None, limit=12),
        ledger.run_summaries(limit=50),
        ledger.run_detail(ledger.run_summaries(limit=1)[0]["run_id"]),
    ]
    reference = [
        _uncached(ledger, monkeypatch, lambda ledger: ledger.run_progress_summary(None, limit=12)),
        _uncached(ledger, monkeypatch, lambda ledger: ledger.run_summaries(limit=50)),
        _uncached(
            ledger,
            monkeypatch,
            lambda ledger: ledger.run_detail(ledger.run_summaries(limit=1)[0]["run_id"]),
        ),
    ]
    assert cached == again == reference


def test_dropping_the_in_process_state_drops_the_cache(
    ledger: CandidateLedger, monkeypatch: pytest.MonkeyPatch
) -> None:
    loads = _Loads(monkeypatch)
    ledger.run_progress_summary(None, limit=12)
    loads.reset()
    _sidecar_cache_drop(ledger.path)
    ledger.run_progress_summary(None, limit=12)
    assert loads.big >= 1, "a dropped cache must be rebuilt from the file"


def test_the_cache_is_bounded(ledger: CandidateLedger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mod, "_REDUCED_RUN_MAX_ENTRIES", 2)
    expected = ledger.run_summaries(limit=50)
    assert len(mod._REDUCED_RUNS) <= 2
    monkeypatch.setattr(mod, "_REDUCED_RUN_ENTRY_MAX_BYTES", 1)
    mod._reduced_runs_drop()
    assert ledger.run_summaries(limit=50) == expected
    assert len(mod._REDUCED_RUNS) == 0, "a run above the entry cap is read but not kept"
