from __future__ import annotations

import pytest

from okto_neuron.construction_cost import ConstructionCostTracker, combine_construction_costs


def test_construction_cost_is_measured_only_with_complete_provider_usage() -> None:
    tracker = ConstructionCostTracker()
    tracker.record_completion({"prompt_tokens": 11, "completion_tokens": 3})
    tracker.record_embedding(
        {
            "embedding_calls": 2,
            "embedding_calls_with_usage": 2,
            "embedding_inputs": 64,
            "input_tokens": 128,
        }
    )

    assert tracker.snapshot() == {
        "schema_version": "construction_cost.v1",
        "status": "measured",
        "embedding_calls": 2,
        "embedding_calls_with_usage": 2,
        "embedding_inputs": 64,
        "completion_calls": 1,
        "completion_calls_with_usage": 1,
        "input_tokens": 139,
        "output_tokens": 3,
    }


def test_construction_cost_keeps_missing_usage_visible_as_partial() -> None:
    tracker = ConstructionCostTracker()
    tracker.record_completion(None)
    tracker.record_embedding(
        {
            "embedding_calls": 1,
            "embedding_calls_with_usage": 0,
            "embedding_inputs": 4,
            "input_tokens": 0,
        }
    )

    cost = tracker.snapshot()
    assert cost["status"] == "partial"
    assert cost["completion_calls"] == 1
    assert cost["completion_calls_with_usage"] == 0
    assert cost["embedding_calls"] == 1
    assert cost["embedding_calls_with_usage"] == 0


def test_combining_costs_sums_rows_without_hiding_partial_coverage() -> None:
    measured = ConstructionCostTracker()
    measured.record_completion({"prompt_tokens": 7, "completion_tokens": 2})
    partial = ConstructionCostTracker()
    partial.record_completion(None)

    combined = combine_construction_costs([measured.snapshot(), partial.snapshot()])

    assert combined["status"] == "partial"
    assert combined["completion_calls"] == 2
    assert combined["completion_calls_with_usage"] == 1
    assert combined["input_tokens"] == 7
    assert combined["output_tokens"] == 2


def test_embedding_usage_rejects_impossible_coverage() -> None:
    tracker = ConstructionCostTracker()

    with pytest.raises(ValueError, match="coverage exceeds"):
        tracker.record_embedding(
            {
                "embedding_calls": 1,
                "embedding_calls_with_usage": 2,
                "embedding_inputs": 1,
                "input_tokens": 1,
            }
        )
