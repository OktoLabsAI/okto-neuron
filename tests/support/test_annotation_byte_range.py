"""ts_3499d9fd — Annotation byte range.

RFC §4.2 oa:Annotation: byte_end >= byte_start. Zero-length point anchors are
valid (br_ec5a750c).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from okto_neuron.schema.support import Annotation

BLOCK_ID = "b" * 64


def _ann(**o):
    payload = {
        "id": "a-1",
        "block_id": BLOCK_ID,
        "target_id": "agent:1",
        "byte_start": 0,
        "byte_end": 4,
    }
    payload.update(o)
    return Annotation(**payload)


def test_annotation_zero_length_is_point_anchor():
    a = _ann(byte_start=10, byte_end=10)
    assert a.byte_start == a.byte_end == 10


def test_annotation_normal_range_ok():
    a = _ann(byte_start=2, byte_end=8)
    assert a.byte_end > a.byte_start


def test_annotation_inverted_range_rejected():
    with pytest.raises(ValidationError):
        _ann(byte_start=10, byte_end=5)


def test_annotation_negative_offsets_rejected():
    with pytest.raises(ValidationError):
        _ann(byte_start=-1, byte_end=5)
    with pytest.raises(ValidationError):
        _ann(byte_start=0, byte_end=-1)
