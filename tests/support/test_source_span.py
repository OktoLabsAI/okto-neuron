"""SourceSpan value-object — ADR 0003 Phase A (additive).

Covers: valid construction, shared path-safety reuse, byte-range validation,
immutability, value-object (non-node) status, and optional wiring into Claim
and Annotation.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from okto_neuron.schema.support import (
    Annotation,
    Claim,
    SourceSpan,
    SupportBase,
)
from okto_neuron.schema.support._common import PATH_MAX_LENGTH

GOOD = "a" * 64


def _span(path: str = "notes/idea.md") -> SourceSpan:
    return SourceSpan(
        source_path=path,
        byte_start=0,
        byte_end=10,
        content_hash=GOOD,
    )


def test_valid_construction():
    span = _span()
    assert span.source_path == "notes/idea.md"
    assert span.byte_start == 0
    assert span.byte_end == 10
    assert span.content_hash == GOOD


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
def test_path_safety_reused(bad, reason):
    with pytest.raises(ValidationError):
        SourceSpan(
            source_path=bad,
            byte_start=0,
            byte_end=1,
            content_hash=GOOD,
        )


def test_path_safety_uses_sourcespan_label():
    with pytest.raises(ValidationError) as exc:
        SourceSpan(source_path="/abs.md", byte_start=0, byte_end=1, content_hash=GOOD)
    assert "SourceSpan.source_path" in str(exc.value)


def test_byte_end_before_start_rejected():
    with pytest.raises(ValidationError):
        SourceSpan(
            source_path="notes/idea.md",
            byte_start=10,
            byte_end=5,
            content_hash=GOOD,
        )


def test_zero_length_span_ok():
    span = SourceSpan(source_path="notes/idea.md", byte_start=5, byte_end=5, content_hash=GOOD)
    assert span.byte_start == span.byte_end


def test_frozen_immutable():
    span = _span()
    with pytest.raises(ValidationError):
        span.byte_start = 3


def test_path_nfc_normalized():
    import unicodedata

    raw = "idéa.md"  # 'e' + combining acute
    nfc = unicodedata.normalize("NFC", raw)
    assert nfc != raw
    span = SourceSpan(source_path=raw, byte_start=0, byte_end=1, content_hash=GOOD)
    assert span.source_path == nfc


def test_sourcespan_is_not_a_node():
    # It is a plain pydantic BaseModel value-object, never a graph node.
    assert issubclass(SourceSpan, BaseModel)
    assert not issubclass(SourceSpan, SupportBase)
    assert SourceSpan not in SupportBase.__subclasses__()
    assert SourceSpan.model_config.get("frozen") is True


def test_claim_accepts_optional_source_span():
    claim = Claim(
        id=GOOD,
        S_id="s1",
        P="relatedTo",
        O_id="o1",
        confidence=0.9,
        block_id=GOOD,
        extraction_activity_id="act1",
        agent_id="agent1",
    )
    assert claim.source_span is None

    claim2 = claim.model_copy(update={"source_span": _span()})
    assert claim2.source_span == _span()
    # Existing canonical anchor is untouched.
    assert claim2.block_id == GOOD


def test_annotation_accepts_optional_source_span():
    ann = Annotation(
        id="ann1",
        block_id=GOOD,
        target_id="t1",
        byte_start=0,
        byte_end=5,
    )
    assert ann.source_span is None

    ann2 = ann.model_copy(update={"source_span": _span()})
    assert ann2.source_span == _span()
    assert ann2.block_id == GOOD
