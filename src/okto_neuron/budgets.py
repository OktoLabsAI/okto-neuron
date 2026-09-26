"""Okto Neuron wall-clock budget constants + helpers (FR10).

Hardware envelope: Mac Pro M2 baseline. Budgets are wall-clock seconds.
"""

from __future__ import annotations

from statistics import median
from typing import Iterable

SYNTHETIC_BUDGET_SECONDS: float = 30.0
CORPUS_BUDGET_SECONDS: float = 300.0


def median_of_three(samples: Iterable[float]) -> float:
    """Return the median of exactly three wall-clock samples."""
    vals = list(samples)
    if len(vals) != 3:
        raise ValueError(f"median_of_three requires exactly 3 samples, got {len(vals)}")
    return float(median(vals))


def budget_check(suite: str, samples: Iterable[float]) -> tuple[bool, float, float]:
    """Return (within_budget, median_seconds, limit_seconds) for the named suite.

    suite must be ``synthetic`` or ``corpus``.
    """
    if suite == "synthetic":
        limit = SYNTHETIC_BUDGET_SECONDS
    elif suite == "corpus":
        limit = CORPUS_BUDGET_SECONDS
    else:
        raise ValueError(f"unknown suite: {suite!r}")
    m = median_of_three(samples)
    return (m <= limit, m, limit)
