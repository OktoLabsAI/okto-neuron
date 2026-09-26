"""Finding 3.7 (marginalia-deep-review §3.7): chunk overlap (ADR 0038,
operator-tunable, default off) re-detects the same real ``#tag``/``[[wikilink]]``
mention once per overlapping window it falls in, minting a full-salience
duplicate ``has_tag``/``links_to`` Claim per window for a single real mention
(unlike ``has_heading``, which is positional and intentionally NOT deduped
here).
"""

from __future__ import annotations

from pathlib import Path

from okto_neuron.ingest.markdown import parse_markdown


def test_overlapping_windows_mint_one_tag_claim_per_real_mention(tmp_path: Path) -> None:
    p = tmp_path / "note.md"
    # A single #uniquetagxyz mention, padded with enough lines that a small
    # chunk_size/overlap makes it visible in more than one window.
    p.write_text(
        "padding line one\n"
        "padding line two\n"
        "#uniquetagxyz appears exactly once here\n"
        "padding line three\n"
        "padding line four\n",
        encoding="utf-8",
    )

    parsed = parse_markdown(
        p,
        extraction_activity_id="t",
        agent_id="t",
        chunk_size_bytes=80,
        chunk_overlap_bytes=40,
    )

    # Sanity: the overlap config actually produced more than one window,
    # AND the #uniquetagxyz mention itself lands inside more than one of
    # them (not just adjacent, non-overlapping windows) — otherwise this
    # test wouldn't exercise the overlap path at all. The mention line has
    # to stay short enough (<=40 bytes incl. newline, at these chunk sizes)
    # that window 2's overlap floor reaches back into window 1 far enough
    # to include it; lengthening it silently turns this into a no-op check.
    assert len(parsed.blocks) > 1
    assert sum(1 for b in parsed.blocks if "uniquetagxyz" in b.text) > 1

    tag_claims = [
        c for c in parsed.claims if c.claim.P == "has_tag" and c.claim.O_literal == "uniquetagxyz"
    ]
    assert len(tag_claims) == 1, (
        f"expected exactly one has_tag Claim for one real #uniquetagxyz "
        f"mention across overlapping windows, got {len(tag_claims)}"
    )


def test_overlapping_windows_mint_one_wikilink_claim_per_real_mention(tmp_path: Path) -> None:
    p = tmp_path / "note.md"
    p.write_text(
        "padding line one\n"
        "padding line two\n"
        "[[UniqueTarget]] appears once here\n"
        "padding line three\n"
        "padding line four\n",
        encoding="utf-8",
    )

    parsed = parse_markdown(
        p,
        extraction_activity_id="t",
        agent_id="t",
        chunk_size_bytes=80,
        chunk_overlap_bytes=40,
    )
    # Same straddle requirement as the tag test above: the mention must
    # actually land inside more than one window, not just produce more
    # than one window.
    assert len(parsed.blocks) > 1
    assert sum(1 for b in parsed.blocks if "UniqueTarget" in b.text) > 1

    link_claims = [
        c for c in parsed.claims if c.claim.P == "links_to" and c.claim.O_literal == "UniqueTarget"
    ]
    assert len(link_claims) == 1


def test_distinct_tags_in_different_windows_each_still_get_a_claim(tmp_path: Path) -> None:
    """The dedup must key on the object literal, not collapse ALL has_tag
    Claims for a document down to one."""
    p = tmp_path / "note.md"
    p.write_text("#alpha here\n\n" + ("filler " * 20) + "\n\n#beta here\n", encoding="utf-8")

    parsed = parse_markdown(
        p, extraction_activity_id="t", agent_id="t", chunk_size_bytes=50, chunk_overlap_bytes=20
    )
    tags = {c.claim.O_literal for c in parsed.claims if c.claim.P == "has_tag"}
    assert tags == {"alpha", "beta"}
