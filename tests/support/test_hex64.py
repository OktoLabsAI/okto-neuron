"""ts_985a9295 — HEX64 rejection matrix.

RFC §4.3 anchors Claim.id and Block.content_hash to sha256; the schema layer
enforces 64-char lowercase hex.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from okto_neuron.schema.support import Block

GOOD = "a" * 64


def _block(**overrides):
    payload = {
        "id": GOOD,
        "path": "notes/idea.md",
        "block_index": 0,
        "byte_start": 0,
        "byte_end": 1,
        "block_kind": "paragraph",
        "content_hash": GOOD,
    }
    payload.update(overrides)
    return Block(**payload)


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "abc",  # short
        "A" * 64,  # uppercase
        "g" * 64,  # non-hex char
        "a" * 63,  # too short by one
        "a" * 65,  # too long
        " " + "a" * 63,  # leading whitespace
    ],
)
def test_hex64_rejects_bad_content_hash(bad):
    with pytest.raises(ValidationError):
        _block(content_hash=bad)


@pytest.mark.parametrize("bad", ["", "abc", "A" * 64, "z" * 64, "a" * 63, "a" * 65])
def test_hex64_rejects_bad_id(bad):
    with pytest.raises(ValidationError):
        _block(id=bad)


def test_hex64_accepts_lowercase_64_hex():
    blk = _block()
    assert blk.content_hash == GOOD
