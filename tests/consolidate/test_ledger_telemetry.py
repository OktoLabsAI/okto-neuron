"""ADR 0015 D3.3 — per-call LLM telemetry in ledger comparisons."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from okto_neuron.consolidate._candidates import EdgeCandidate, NodeCandidate
from okto_neuron.consolidate.ledger import (
    CandidateLedger,
    _population_revision,
    _progress,
    _timing_stats,
)
from okto_neuron.curator import LLMCandidateCurator, LLMRelationCurator
from okto_neuron.llm import Message, _set_last_call_stats
from okto_neuron.resolve import ResolveOutcome
from okto_neuron.store.memory import InMemoryStore


class _FakeProvider:
    """Returns a fixed curator verdict and reports token usage."""

    model = "fake"

    def __init__(self, reply: str = '{"action": "commit", "confidence": 0.9, "reason": "ok"}'):
        self._reply = reply

    def complete(self, messages: Sequence[Message], **kwargs: object) -> str:
        _set_last_call_stats(
            {"prompt_tokens": 5400, "completion_tokens": 80, "cached_tokens": 4096}
        )
        return self._reply


def test_node_curator_verdict_carries_telemetry() -> None:
    curator = LLMCandidateCurator(_FakeProvider())
    candidate = NodeCandidate(type="Agent", title="Alex", content="a participant")
    verdict = curator.curate(
        candidate,
        ResolveOutcome(correlations=(), confidence=0.5),
        store=InMemoryStore(),
        edges=[],
    )
    assert verdict.action == "commit"
    assert verdict.duration_s is not None and verdict.duration_s >= 0
    assert verdict.usage == {
        "prompt_tokens": 5400,
        "completion_tokens": 80,
        "cached_tokens": 4096,
    }


def test_relation_curator_verdict_carries_telemetry() -> None:
    curator = LLMRelationCurator(_FakeProvider())
    candidate = EdgeCandidate(type="uses", src_ref="a", dst_ref="b")
    verdict = curator.curate(candidate, store=InMemoryStore(), node_candidates={})
    assert verdict.duration_s is not None and verdict.duration_s >= 0
    assert verdict.usage is not None and verdict.usage["prompt_tokens"] == 5400


def test_run_summary_aggregates_llm_timing(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    run_id = ledger.start_run(document_id="d1", source="/v/a.md", blocks_total=1, model="m")
    for i, duration in enumerate((1.0, 2.0, 3.0)):
        ledger.record_comparison(
            run_id,
            candidate_id=f"c{i}",
            method="relation_curator",
            verdict="commit",
            payload={
                "duration_s": duration,
                "usage": {"prompt_tokens": 100, "completion_tokens": 10},
                "proposed_terminal_action": "create_edge_or_claim",
            },
        )
    summary = ledger.run_progress_summary(run_id=run_id)
    assert summary is not None
    timing = summary["llm_timing"]["relation_curator"]
    assert timing["calls"] == 3
    assert timing["total_s"] == 6.0
    assert timing["p50_s"] == 2.0
    assert timing["tokens"] == {"prompt_tokens": 300, "completion_tokens": 30}


def test_ledger_v2_strips_embeddings_on_write(tmp_path: Path) -> None:
    """ADR 0015 D3.1 — candidate rows store embedding_dim, never the vector."""
    ledger = CandidateLedger(tmp_path)
    run_id = ledger.start_run(document_id="d1", source="/v/a.md", blocks_total=1, model="m")
    ledger.record_candidate(
        run_id,
        candidate_id="n1",
        candidate_kind="node",
        state="proposed",
        payload={"type": "Agent", "title": "Alex", "embedding": [0.1] * 384},
    )
    raw = (tmp_path / "candidate-ledger.jsonl").read_text(encoding="utf-8")
    rows = [r for r in map(str.strip, raw.splitlines()) if r]
    import json as _json

    candidate = next(r for r in map(_json.loads, rows) if r["kind"] == "candidate")
    assert candidate["ledger_version"] == 2
    assert "embedding" not in candidate["payload"]
    assert candidate["payload"]["embedding_dim"] == 384
    # v1 rows (inlined vectors written by older versions) still read fine.
    assert ledger.run_progress_summary(run_id=run_id) is not None


def test_timing_stats_shape() -> None:
    stats = _timing_stats([0.5])
    assert stats == {
        "calls": 1,
        "total_s": 0.5,
        "p50_s": 0.5,
        "p90_s": 0.5,
        "max_s": 0.5,
    }


# ── ADR 0039 T9: progress population revision and done>total rejection ────────


def test_progress_reports_named_population_and_stays_bounded() -> None:
    record = _progress(3, 10, population="node_candidates")
    assert record == {
        "done": 3,
        "total": 10,
        "remaining": 7,
        "fraction": 0.3,
        "population": "node_candidates",
    }
    assert "progress_integrity_error" not in record


def test_progress_rejects_done_greater_than_total_instead_of_clamping() -> None:
    record = _progress(12, 10, population="node_candidates")
    # No clamp-and-hide: the true arithmetic and an unbounded fraction survive.
    assert record["remaining"] == -2
    assert record["fraction"] == 1.2
    assert record["progress_integrity_error"] == {
        "code": "progress_done_exceeds_total",
        "population": "node_candidates",
        "done": 12,
        "total": 10,
        "overflow": 2,
    }


def test_progress_flags_work_against_an_empty_population() -> None:
    record = _progress(4, 0, population="edge_candidates")
    assert record["fraction"] is None
    assert record["progress_integrity_error"]["overflow"] == 4


def test_progress_publishes_declared_population_revision() -> None:
    revision = _population_revision(
        previous_total=10, total=6, reason="active_population_recomputed", source="prefilter"
    )
    record = _progress(6, 6, population="node_candidates", revision=revision)
    assert record["population_revision"] == {
        "previous_total": 10,
        "total": 6,
        "reason": "active_population_recomputed",
        "source": "prefilter",
    }
    assert "progress_integrity_error" not in record


def test_run_summary_declares_a_shrunken_node_population(tmp_path: Path) -> None:
    """Prefilter shrinking the live population is declared, not absorbed."""
    ledger = CandidateLedger(tmp_path)
    run_id = ledger.start_run(document_id="d1", source="/v/a.md", blocks_total=1, model="m")
    for i in range(4):
        ledger.record_candidate(
            run_id,
            candidate_id=f"n{i}",
            candidate_kind="node",
            state="proposed",
            payload={"type": "Agent", "title": f"n{i}"},
        )
    ledger.record_comparison(
        run_id,
        candidate_id="n0",
        method="prefilter",
        verdict="kept",
        payload={"survivors": ["n0", "n1"]},
    )
    summary = ledger.run_progress_summary(run_id=run_id)
    assert summary is not None
    node_progress = summary["progress"]["node_curator"]
    assert node_progress["population"] == "node_candidates"
    assert node_progress["total"] == 2
    assert node_progress["population_revision"] == {
        "previous_total": 4,
        "total": 2,
        "reason": "active_population_recomputed",
        "source": "prefilter",
    }
    assert "progress_integrity_error" not in node_progress
