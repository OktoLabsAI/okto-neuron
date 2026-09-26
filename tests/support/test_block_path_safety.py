"""ts_3ee78ea6 — Block.path safety pipeline matrix.

RFC §4.3 Block carries `path`. Pipeline order (br_33ab833d):
NUL → absolute → backslash → `..` → length ≤ 4096 → NFC normalize.
"""

from __future__ import annotations

import unicodedata

import pytest
from pydantic import ValidationError

from okto_neuron.schema.support import Block
from okto_neuron.schema.support.block import PATH_MAX_LENGTH

GOOD = "a" * 64


def _block(path):
    return Block(
        id=GOOD,
        path=path,
        block_index=0,
        byte_start=0,
        byte_end=1,
        block_kind="paragraph",
        content_hash=GOOD,
    )


@pytest.mark.parametrize(
    "bad,reason",
    [
        ("hello\x00there", "NUL"),
        ("/abs/path.md", "absolute"),
        ("C:/win.md", "drive letter"),
        ("a\\b.md", "backslash"),
        ("foo/../bar.md", "traversal"),
        ("a" * (PATH_MAX_LENGTH + 1), "too long"),
    ],
)
def test_block_path_rejects_unsafe(bad, reason):
    with pytest.raises(ValidationError):
        _block(bad)


def test_block_path_nfc_normalization_idempotent():
    # NFD precomposed 'é' vs NFC; NFC should be returned.
    raw = "idéa.md"  # 'e' + combining acute
    nfc = unicodedata.normalize("NFC", raw)
    assert nfc != raw
    blk = _block(raw)
    assert blk.path == nfc
    # Idempotence: feeding the NFC form yields the same result.
    blk2 = _block(blk.path)
    assert blk2.path == blk.path


def test_block_path_relative_simple_ok():
    blk = _block("notes/sub/idea.md")
    assert blk.path == "notes/sub/idea.md"


def test_block_path_at_max_length_ok():
    p = "a" * PATH_MAX_LENGTH
    blk = _block(p)
    assert len(blk.path) == PATH_MAX_LENGTH
