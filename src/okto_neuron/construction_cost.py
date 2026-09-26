"""Durable construction-cost accounting shared by ingest and reconciliation."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from typing import Any

CONSTRUCTION_COST_SCHEMA = "construction_cost.v1"


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


class ConstructionCostTracker:
    """Thread-safe accounting for successful model calls and reported tokens.

    A call is counted when its provider returns successfully. ``status`` is
    ``measured`` only when every successful completion and embedding request
    reported input/output token usage; missing provider accounting stays
    visible as ``partial`` rather than being silently treated as zero.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._completion_calls = 0
        self._completion_calls_with_usage = 0
        self._embedding_calls = 0
        self._embedding_calls_with_usage = 0
        self._embedding_inputs = 0
        self._input_tokens = 0
        self._output_tokens = 0

    def record_completion(self, usage: Mapping[str, Any] | None) -> None:
        prompt_tokens = _non_negative_int((usage or {}).get("prompt_tokens"))
        completion_tokens = _non_negative_int((usage or {}).get("completion_tokens"))
        with self._lock:
            self._completion_calls += 1
            if prompt_tokens is not None and completion_tokens is not None:
                self._completion_calls_with_usage += 1
                self._input_tokens += prompt_tokens
                self._output_tokens += completion_tokens

    def record_embedding(self, usage: Mapping[str, Any]) -> None:
        calls = _non_negative_int(usage.get("embedding_calls"))
        calls_with_usage = _non_negative_int(usage.get("embedding_calls_with_usage"))
        inputs = _non_negative_int(usage.get("embedding_inputs"))
        input_tokens = _non_negative_int(usage.get("input_tokens"))
        if None in (calls, calls_with_usage, inputs, input_tokens):
            raise ValueError("embedding usage contains invalid counters")
        assert calls is not None
        assert calls_with_usage is not None
        assert inputs is not None
        assert input_tokens is not None
        if calls_with_usage > calls:
            raise ValueError("embedding usage coverage exceeds call count")
        with self._lock:
            self._embedding_calls += calls
            self._embedding_calls_with_usage += calls_with_usage
            self._embedding_inputs += inputs
            self._input_tokens += input_tokens

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            complete = (
                self._completion_calls == self._completion_calls_with_usage
                and self._embedding_calls == self._embedding_calls_with_usage
            )
            return {
                "schema_version": CONSTRUCTION_COST_SCHEMA,
                "status": "measured" if complete else "partial",
                "embedding_calls": self._embedding_calls,
                "embedding_calls_with_usage": self._embedding_calls_with_usage,
                "embedding_inputs": self._embedding_inputs,
                "completion_calls": self._completion_calls,
                "completion_calls_with_usage": self._completion_calls_with_usage,
                "input_tokens": self._input_tokens,
                "output_tokens": self._output_tokens,
            }


def combine_construction_costs(costs: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Combine measured/partial cost rows without inventing missing usage."""

    fields = (
        "embedding_calls",
        "embedding_calls_with_usage",
        "embedding_inputs",
        "completion_calls",
        "completion_calls_with_usage",
        "input_tokens",
        "output_tokens",
    )
    totals = {field: 0 for field in fields}
    statuses: list[str] = []
    for index, cost in enumerate(costs):
        if cost.get("schema_version") != CONSTRUCTION_COST_SCHEMA:
            raise ValueError(f"construction cost row {index} has an unsupported schema")
        status = cost.get("status")
        if status not in {"measured", "partial"}:
            raise ValueError(f"construction cost row {index} has an invalid status")
        statuses.append(str(status))
        for field in fields:
            value = _non_negative_int(cost.get(field))
            if value is None:
                raise ValueError(f"construction cost row {index}.{field} is invalid")
            totals[field] += value
    return {
        "schema_version": CONSTRUCTION_COST_SCHEMA,
        "status": "measured" if all(status == "measured" for status in statuses) else "partial",
        **totals,
    }


__all__ = [
    "CONSTRUCTION_COST_SCHEMA",
    "ConstructionCostTracker",
    "combine_construction_costs",
]
