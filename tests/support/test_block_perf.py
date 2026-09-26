"""ts_132b6df8 — Block validation performance gate.

TR6: 10,000 Block validations must complete in <500 ms on CI baseline.
"""

from __future__ import annotations

import time

from okto_neuron.schema.support import Block

GOOD = "a" * 64


def test_block_perf_10k_under_500ms():
    payload = {
        "id": GOOD,
        "path": "notes/idea.md",
        "block_index": 0,
        "byte_start": 0,
        "byte_end": 1,
        "block_kind": "paragraph",
        "content_hash": GOOD,
    }
    start = time.perf_counter()
    for i in range(10_000):
        # Vary block_index to ensure cache-free construction.
        Block(**{**payload, "block_index": i})
    elapsed_ms = (time.perf_counter() - start) * 1000
    # Generous CI baseline (modern laptops do this in ~150 ms).
    assert elapsed_ms < 500.0, f"10k Block validations took {elapsed_ms:.1f} ms (gate 500)"
