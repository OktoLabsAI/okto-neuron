"""Tests for FR10 wall-clock budget enforcement."""

from __future__ import annotations

import pytest

from okto_neuron.budgets import (
    CORPUS_BUDGET_SECONDS,
    SYNTHETIC_BUDGET_SECONDS,
    budget_check,
    median_of_three,
)


def test_synthetic_budget_is_30s():
    assert SYNTHETIC_BUDGET_SECONDS == 30.0


def test_corpus_budget_is_300s():
    assert CORPUS_BUDGET_SECONDS == 300.0


def test_median_of_three_basic():
    assert median_of_three([1.0, 2.0, 3.0]) == 2.0
    assert median_of_three([3.0, 1.0, 2.0]) == 2.0
    assert median_of_three([5.0, 5.0, 5.0]) == 5.0


def test_median_of_three_rejects_wrong_count():
    with pytest.raises(ValueError):
        median_of_three([1.0, 2.0])
    with pytest.raises(ValueError):
        median_of_three([1.0, 2.0, 3.0, 4.0])


def test_budget_check_synthetic_within():
    ok, m, lim = budget_check("synthetic", [10.0, 15.0, 20.0])
    assert ok is True
    assert m == 15.0
    assert lim == 30.0


def test_budget_check_synthetic_exceeded():
    ok, m, lim = budget_check("synthetic", [40.0, 45.0, 50.0])
    assert ok is False
    assert m == 45.0
    assert lim == 30.0


def test_budget_check_corpus_within():
    ok, m, lim = budget_check("corpus", [100.0, 150.0, 200.0])
    assert ok is True
    assert m == 150.0
    assert lim == 300.0


def test_budget_check_corpus_exceeded():
    ok, m, lim = budget_check("corpus", [350.0, 400.0, 450.0])
    assert ok is False
    assert m == 400.0
    assert lim == 300.0


def test_budget_check_unknown_suite():
    with pytest.raises(ValueError):
        budget_check("nonsense", [1.0, 2.0, 3.0])


def test_xfail_message_format():
    suite = "synthetic"
    m = 45.12
    limit = 30.0
    msg = f"budget_exceeded suite={suite} median_s={m:.2f} limit_s={limit}"
    assert msg == "budget_exceeded suite=synthetic median_s=45.12 limit_s=30.0"
