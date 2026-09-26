"""Track A: per-window extraction tags candidates with the right Block anchor.

``Companion.remember`` must call the extractor ONCE PER anchored Block (now a
fixed ~12k-char window) and stamp every produced relationship with that window's
byte-range, so a minted Claim anchors to the window its text came from — not the
whole document, and not a foreign window. The doc below is sized to split into
TWO windows so distinct per-window anchoring is genuinely exercised.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.core.schema import Provenance
from okto_neuron.extract import ExtractionResult
from okto_neuron.llm import StubLLM
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


class _RecordingExtractor:
    """One relationship per window; records every text it was handed."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        prov = provenance or Provenance()
        self.seen.append(text)
        key = text.strip()[:24]
        subj = NodeCandidate(type="Concept", title=f"subj {key}", content=text, provenance=prov)
        obj = NodeCandidate(type="Concept", title=f"obj {key}", content=text, provenance=prov)
        edge = EdgeCandidate(
            type="relates_to",
            src_ref=subj.candidate_id,
            dst_ref=obj.candidate_id,
            provenance=prov,
        )
        return ExtractionResult(node_candidates=[subj, obj], edge_candidates=[edge])


# Two ~9.6k-char single-line paragraphs: together >12k so they split into two
# windows; each alone <12k so neither splits further. Distinct 24-char prefixes
# give the recording extractor distinct relationship keys per window.
_DOC = "AAA-first-window. " + "alpha " * 1600 + "\n\nBBB-second-window. " + "omega " * 1600 + "\n"


def test_extractor_called_once_per_block_and_claims_anchor_distinctly(tmp_path: Path) -> None:
    from okto_neuron.companion import Companion

    vault = Vault.init(tmp_path / "v")
    try:
        path = Path(vault.path) / "note.md"
        path.write_text(_DOC, encoding="utf-8")

        extractor = _RecordingExtractor()
        companion = Companion(vault, provider=StubLLM(), extractor=extractor)
        companion.remember(path)

        # The extractor saw each Block's text, not the whole document in one shot.
        blocks = [n for n in vault.store.list_nodes(type="Block")]
        block_texts = {b.content for b in blocks}
        assert block_texts  # the doc produced anchored blocks
        # Every block's text was handed to the extractor exactly.
        assert block_texts.issubset(set(extractor.seen))
        # No single call carried the concatenated document body.
        assert _DOC.strip() not in extractor.seen

        # Each minted Claim anchors to a distinct, real Block.
        claims = list(vault.store.list_nodes(type="Claim"))
        anchored = {c.facets["block_id"] for c in claims}
        block_ids = {b.id for b in blocks}
        assert len(anchored) == len(claims)  # distinct anchors
        assert anchored.issubset(block_ids)
    finally:
        vault.close()


def test_relationship_spanning_no_block_is_not_minted(tmp_path: Path) -> None:
    """A relationship whose endpoints never co-occur in a single block does not
    mint a cross-block Claim, and unsupported standalone nodes queue for review."""
    from okto_neuron.companion import Companion

    vault = Vault.init(tmp_path / "v")
    try:
        path = Path(vault.path) / "note.md"
        path.write_text("only one short paragraph here.\n", encoding="utf-8")

        class _NoEdgeExtractor:
            def extract(
                self, text: str, *, provenance: Provenance | None = None
            ) -> ExtractionResult:
                prov = provenance or Provenance()
                node = NodeCandidate(type="Concept", title="lonely", content=text, provenance=prov)
                return ExtractionResult(node_candidates=[node], edge_candidates=[])

        companion = Companion(vault, provider=StubLLM(), extractor=_NoEdgeExtractor())
        result = companion.remember(path)

        assert result.committed == 0
        assert result.queued == 1
        assert vault.store.get_node(result.outcomes[0].candidate_id) is None
        assert list(vault.store.list_nodes(type="Claim")) == []
    finally:
        vault.close()
