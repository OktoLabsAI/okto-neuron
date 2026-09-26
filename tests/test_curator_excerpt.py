"""Task-13 E2 — curator source excerpt must cover the FULL extraction block.

The reference-eval audit found curator-queue FALSE NEGATIVES ("the source excerpt
does not contain the literal ...") on markdown table cells: the excerpt was
``block.content[:9000]`` while production blocks are fixed ~12k-char windows,
so any candidate anchored in the final ~3k chars — table rows especially —
was verified against an excerpt that structurally could not contain its
literal. These tests pin the fix: the excerpt limit covers a full 12k block,
so a table-cell literal past the old 9000 cut is present in the verification
excerpt, while pathological oversized blocks stay bounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.curator import (
    CURATION_SOURCE_EXCERPT_LIMIT,
    _edge_source_excerpt,
    _source_excerpt,
)

# A synthetic markdown table (no corpus strings) whose cell literals are the
# facts under verification.
_SYNTH_TABLE = (
    "| Setting | Series | Movies |\n"
    "|---|---|---|\n"
    "| Minimum subtitle score | 91% | 72% |\n"
    "| Provider fanout | 63+ | 63+ |\n"
)


@dataclass
class _StubBlock:
    content: str
    id: str = "block-1"
    type: str = "Block"
    title: str = "block"
    facets: dict = field(default_factory=dict)


class _StubStore:
    def __init__(self, block: _StubBlock) -> None:
        self._block = block

    def get_node(self, node_id: str):
        return self._block if node_id == self._block.id else None


def _block_with_table_past_9000() -> _StubBlock:
    # Mirror a production 12k window: filler prose for ~10k chars, then the
    # table — its cells sit beyond the old 9000-char excerpt cut.
    filler = ("filler prose line about nothing in particular.\n" * 220)[:10_000]
    text = filler + "\n## Config table\n\n" + _SYNTH_TABLE
    assert len(text) < 12_000
    assert text.find("91%") > 9_000
    return _StubBlock(content=text)


def test_edge_excerpt_includes_table_cell_literal_past_old_9000_cut() -> None:
    block = _block_with_table_past_9000()
    store = _StubStore(block)
    claim = EdgeCandidate(
        type="minimum_subtitle_score",
        src_ref="subject-ref",
        dst_literal="91%",
        block_id=block.id,
    )

    excerpt = _edge_source_excerpt(claim, store)

    # The literal the relation curator must verify is IN the excerpt now.
    assert "91%" in excerpt
    assert "72%" in excerpt
    assert "| Minimum subtitle score | 91% | 72% |" in excerpt
    # Full block, no truncation marker.
    assert excerpt == block.content.strip()


def test_node_excerpt_includes_table_cell_literal_past_old_9000_cut() -> None:
    block = _block_with_table_past_9000()
    store = _StubStore(block)
    candidate = NodeCandidate(
        type="Concept",
        title="Minimum subtitle score",
        content="a per-media-type score threshold",
        facets={"block_id": block.id},
    )

    excerpt = _source_excerpt(candidate, store)

    assert "91%" in excerpt
    assert excerpt == block.content.strip()


def test_excerpt_limit_covers_a_full_production_window() -> None:
    # Production extraction windows are fixed ~12k chars
    # (marginalia.ingest.markdown._WINDOW_BYTES = 12000); the excerpt limit
    # must never truncate a standard window again.
    from okto_neuron.ingest.markdown import _WINDOW_BYTES

    assert CURATION_SOURCE_EXCERPT_LIMIT >= _WINDOW_BYTES


def test_pathological_oversized_block_is_still_bounded() -> None:
    # A single-line mega-block beyond the limit still truncates with a marker.
    block = _StubBlock(content="x" * (CURATION_SOURCE_EXCERPT_LIMIT + 5_000))
    store = _StubStore(block)
    claim = EdgeCandidate(
        type="has_value",
        src_ref="subject-ref",
        dst_literal="v",
        block_id=block.id,
    )

    excerpt = _edge_source_excerpt(claim, store)

    assert excerpt.endswith("\n...")
    assert len(excerpt) <= CURATION_SOURCE_EXCERPT_LIMIT + len("\n...")
