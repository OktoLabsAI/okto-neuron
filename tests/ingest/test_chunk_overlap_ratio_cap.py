"""Finding 3.15 (marginalia-deep-review §3.15): chunk overlap causes
near-linear-to-exponential block-count amplification as the
overlap:chunk_size ratio approaches 1 (measured: 0.95 produced a 20x block
count vs. 0.0 on a 2.4MB fixture; short-line content is far worse). No upper
bound was enforced anywhere. ``parse_markdown`` is the runtime enforcement
point regardless of how ``chunk_size_bytes``/``chunk_overlap_bytes`` reached
it (config, env override, or a direct caller).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_neuron.ingest.markdown import parse_markdown


def test_overlap_ratio_above_half_is_rejected(tmp_path: Path) -> None:
    p = tmp_path / "note.md"
    p.write_text("hello world\n" * 50, encoding="utf-8")

    with pytest.raises(ValueError, match="overlap"):
        parse_markdown(
            p,
            extraction_activity_id="t",
            agent_id="t",
            chunk_size_bytes=100,
            chunk_overlap_bytes=51,
        )


def test_overlap_ratio_at_exactly_half_is_allowed(tmp_path: Path) -> None:
    """0.5 is the documented cap, inclusive — matches the existing
    tests/ingest/test_txt_suffix.py 10/5 configuration, which must stay
    legal."""
    p = tmp_path / "note.md"
    p.write_text("hello world\n" * 5, encoding="utf-8")

    parsed = parse_markdown(
        p,
        extraction_activity_id="t",
        agent_id="t",
        chunk_size_bytes=10,
        chunk_overlap_bytes=5,
    )
    assert parsed.blocks


def test_overlap_ratio_far_above_half_is_rejected_with_clear_message(tmp_path: Path) -> None:
    p = tmp_path / "note.md"
    p.write_text("x\n" * 100, encoding="utf-8")

    with pytest.raises(ValueError) as excinfo:
        parse_markdown(
            p,
            extraction_activity_id="t",
            agent_id="t",
            chunk_size_bytes=100,
            chunk_overlap_bytes=95,
        )
    message = str(excinfo.value)
    assert "chunk_overlap_bytes" in message
    assert "chunk_size_bytes" in message
