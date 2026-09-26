"""Tests for the pure confidence policy."""

from __future__ import annotations

from okto_neuron.companion import Correlation
from okto_neuron.consolidate.gate import GateConfig, decide
from okto_neuron.resolve import ResolveOutcome


def test_decide_high_confidence_commits() -> None:
    commit, reason = decide(ResolveOutcome(confidence=0.9))
    assert commit is True
    assert reason is None


def test_decide_low_confidence_routes_to_review() -> None:
    commit, reason = decide(ResolveOutcome(confidence=0.5))
    assert commit is False
    assert reason == "low_confidence"


def test_decide_contradiction_takes_precedence_over_confidence() -> None:
    contradiction = Correlation(kind="contradicts", target_id="n1", score=1.0)
    commit, reason = decide(ResolveOutcome(correlations=(contradiction,), confidence=0.99))
    assert commit is False
    assert reason == "contradiction"


def test_decide_uses_explicit_policy() -> None:
    commit, reason = decide(
        ResolveOutcome(confidence=0.5),
        GateConfig(auto_commit_threshold=0.4),
    )
    assert commit is True
    assert reason is None


def test_decide_can_allow_high_confidence_contradiction() -> None:
    contradiction = Correlation(kind="contradicts", target_id="n1", score=1.0)
    commit, reason = decide(
        ResolveOutcome(correlations=(contradiction,), confidence=0.99),
        GateConfig(review_on_contradiction=False),
    )
    assert commit is True
    assert reason is None
