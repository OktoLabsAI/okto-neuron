"""ts_319644fe — Finding.evidence_claim_ids min_length=1 + order-preserving dedup.

RFC §4.2: Finding carries detector ID + evidence Claims. Order matters for
narrative chains (dec_496fbb2d / br_9219bcbb).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from okto_neuron.schema.support import Finding

A = "a" * 64
B = "b" * 64
C = "c" * 64


def _finding(ids):
    return Finding(
        id="f-1",
        kind="contradiction",
        severity="warn",
        evidence_claim_ids=ids,
        message="m",
        detected_at=datetime(2026, 5, 19, tzinfo=timezone.utc),
    )


def test_finding_rejects_empty_evidence():
    with pytest.raises(ValidationError):
        _finding([])


def test_finding_dedup_preserves_first_seen_order():
    f = _finding([B, A, B, C, A])
    assert f.evidence_claim_ids == [B, A, C]


def test_finding_passes_unique_ordered_list():
    f = _finding([A, B, C])
    assert f.evidence_claim_ids == [A, B, C]


def test_finding_rejects_non_hex64_evidence():
    with pytest.raises(ValidationError):
        _finding(["not-hex"])
