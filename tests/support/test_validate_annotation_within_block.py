"""ts_69b6f8e0 — validate_annotation_within_block helper.

Cross-model invariant (br_c5e7498a / br_48cf59ef): an Annotation must lie
within its parent Block's byte range.
"""

from __future__ import annotations

import pytest

from okto_neuron.schema.support import (
    Annotation,
    Block,
    validate_annotation_within_block,
)

GOOD = "a" * 64
OTHER = "b" * 64


def _block(start=10, end=100, block_id=GOOD, ch=GOOD):
    return Block(
        id=block_id,
        path="notes/idea.md",
        block_index=0,
        byte_start=start,
        byte_end=end,
        block_kind="paragraph",
        content_hash=ch,
    )


def _ann(start, end, block_id=GOOD, target="agent:1"):
    return Annotation(
        id="a-1",
        block_id=block_id,
        target_id=target,
        byte_start=start,
        byte_end=end,
    )


def test_within_block_ok():
    b = _block()
    a = _ann(20, 40)
    assert validate_annotation_within_block(a, b) is None


def test_block_id_mismatch_rejected():
    b = _block()
    a = _ann(20, 40, block_id=OTHER)
    with pytest.raises(ValueError):
        validate_annotation_within_block(a, b)


def test_byte_start_before_block_rejected():
    b = _block()
    a = _ann(5, 40)
    with pytest.raises(ValueError):
        validate_annotation_within_block(a, b)


def test_byte_end_past_block_rejected():
    b = _block()
    a = _ann(20, 200)
    with pytest.raises(ValueError):
        validate_annotation_within_block(a, b)


def test_annotation_aligned_with_block_edges_ok():
    b = _block(start=10, end=100)
    a = _ann(10, 100)
    validate_annotation_within_block(a, b)
