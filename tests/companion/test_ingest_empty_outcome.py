"""2026-09-14 fix: distinguish "no extractable knowledge" from "extraction
failed" in the ``outcome.quality`` axis (ADR 0039 T8).

A live vault had 4 of 76 documents (three checksum manifests, one short task
ticket) end at ``status=error``, ``outcome.quality=failed``,
``error_class=empty_after_retry``, with ``provider_failures: 0`` — nothing
actually broke, the extractor legitimately found no entity-grade content.
That shape (every unresolved unit is ``empty_after_retry``, zero
provider/invalid/source-changed failures) now gets its own result-quality
value, ``"empty"``, computed in ``Companion.remember()``
(``technical_quality`` in ``src/okto_neuron/companion/__init__.py``) strictly
above the ``failed`` arm. A genuine provider failure — even mixed with
``empty_after_retry`` units — must still report ``failed``; a partial mix of
succeeded and ``empty_after_retry`` units (zero provider failures) must still
report ``partial``, unchanged from before this fix.

Model-free: a fake extractor over a real ``Companion``/``Vault``, mirroring
``tests/companion/test_loop.py``'s ``_AlwaysFailingExtractor`` pattern.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.companion import Companion
from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.core.schema import Provenance
from okto_neuron.extract import ExtractionResult
from okto_neuron.llm import LLMProviderError, StubLLM
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection

# Long enough to split into 2+ 6000-byte chunking windows (ADR 0038 default),
# so a "mixed" scenario can give different blocks different extraction
# outcomes.
_MULTI_BLOCK = (
    "# Notes\n\n"
    "First paragraph with some real content.\n\n"
    + ("Padding text to force a second chunking window. " * 400)
    + "\n"
)


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def _doc(vault: Vault, text: str = "# Note\n\nsome body text.\n") -> Path:
    p = Path(vault.path) / "note.md"
    p.write_text(text, encoding="utf-8")
    return p


class _AlwaysEmptyExtractor:
    """Every block comes back empty_after_retry -- the extractor ran (twice,
    per ADR 0021's empty-result retry), found nothing entity-grade, and
    never raised. Mirrors what a checksum manifest or a two-line ticket
    actually produces against a real model."""

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        self.calls += 1
        return ExtractionResult(empty_after_retry=True)


class _FirstCallFailsThenEmptyExtractor:
    """A genuine provider failure on block 0, empty_after_retry on the rest --
    a REAL technical problem must still classify as ``failed``, never
    ``empty``, no matter how many other units were legitimately empty."""

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        self.calls += 1
        if self.calls == 1:
            raise LLMProviderError("simulated transient provider failure")
        return ExtractionResult(empty_after_retry=True)


class _FirstCallSucceedsThenEmptyExtractor:
    """One useful unit succeeds, the rest are empty_after_retry, zero
    provider failures -- this must stay ``partial`` (unchanged from before
    this fix), never get reclassified as ``empty``."""

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        self.calls += 1
        if self.calls == 1:
            return ExtractionResult(
                node_candidates=[NodeCandidate(type="Concept", title="real-concept")]
            )
        return ExtractionResult(empty_after_retry=True)


def test_all_units_empty_after_retry_with_zero_provider_failures_is_empty_not_failed(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        extractor = _AlwaysEmptyExtractor()
        companion = Companion(vault, provider=StubLLM(), extractor=extractor)

        result = companion.remember(_doc(vault))

        assert extractor.calls == 1  # single-block note -> one attempt
        assert result.provider_failures == 0
        assert result.empty_after_retry_blocks == 1
        assert result.outcome["quality"] == "empty"
        # Still a visible, honest terminal state -- not silently indistinguishable
        # from a plain success with no signal at all.
        assert result.outcome["empty_after_retry_blocks"] == 1
        assert result.outcome["provider_failures"] == 0
    finally:
        vault.close()


def test_a_genuine_provider_failure_mixed_with_empty_units_still_reports_failed(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        extractor = _FirstCallFailsThenEmptyExtractor()
        companion = Companion(vault, provider=StubLLM(), extractor=extractor)

        result = companion.remember(_doc(vault, _MULTI_BLOCK))

        # Not a TOTAL provider failure (only one of several attempted blocks
        # failed with a provider error; the rest were empty_after_retry, not
        # provider-failed), so this stays a normal RememberResult rather than
        # raising LLMUnavailableError -- but the outcome must still be
        # ``failed``, never ``empty``, because a real technical failure
        # occurred.
        assert extractor.calls >= 2
        assert result.provider_failures == 1
        assert result.empty_after_retry_blocks >= 1
        assert result.outcome["quality"] == "failed"
    finally:
        vault.close()


def test_partial_success_mixed_with_empty_units_stays_partial(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        extractor = _FirstCallSucceedsThenEmptyExtractor()
        companion = Companion(vault, provider=StubLLM(), extractor=extractor)

        result = companion.remember(_doc(vault, _MULTI_BLOCK))

        assert extractor.calls >= 2
        assert result.provider_failures == 0
        assert result.empty_after_retry_blocks >= 1
        assert result.outcome["quality"] == "partial"
    finally:
        vault.close()


# ─────────────────────────────────────────────────────────────────────────────
# Fix A2: a run that scheduled nothing, extracted nothing, reused nothing AND
# recorded no intentional skip must not report "complete".
# ─────────────────────────────────────────────────────────────────────────────


class _OneNodeExtractor:
    """Mints a real Claim (node pair + edge) so the block carries a ``model_id``
    stamped LLM Claim — the signal the incremental skip path reads."""

    def __init__(self) -> None:
        self.calls = 0

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        self.calls += 1
        prov = provenance or Provenance()
        subj = NodeCandidate(
            type="Concept", title=f"concept-{self.calls}", content=text[:40], provenance=prov
        )
        obj = NodeCandidate(
            type="Concept", title="KnowledgeGraph", content="knowledge graph", provenance=prov
        )
        edge = EdgeCandidate(
            type="has_info",
            src_ref=subj.candidate_id,
            dst_ref=obj.candidate_id,
            provenance=prov,
        )
        return ExtractionResult(node_candidates=[subj, obj], edge_candidates=[edge])


def test_zero_operation_run_reports_no_units_not_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The silent-data-loss shape: every unit is dropped before extraction with
    no ledger row and no ``intentionally_skipped_units`` entry (scheduled ==
    succeeded == reused == skipped == 0). It must be distinguishable from a
    healthy no-op — quality ``"no_units"``, receipts NOT complete.

    The drop is simulated by neutralising the sub-chunk narrowing pass (which is
    exactly how the live defect manifested) so the assertion is about the
    outcome contract, not about one code path's arithmetic."""
    monkeypatch.setenv("OKTO_NEURON_INCREMENTAL_INGEST", "1")
    monkeypatch.setenv("OKTO_NEURON_SUBCHUNK_INGEST", "1")

    vault = Vault.init(tmp_path / "v")
    try:
        note = _doc(vault, "# Note\n\nAlpha beta gamma delta.\n")

        # Run 1 fails extraction for every block: the Block nodes are committed
        # by vault.add, no LLM Claim is minted.
        with pytest.raises(Exception):
            Companion(
                vault, provider=StubLLM(), extractor=_FirstCallFailsThenEmptyExtractor()
            ).remember(note)

        # Run 2 over the SAME bytes, with the narrowing pass neutralised so it
        # drops the block exactly as the live defect did: no ledger row, no
        # intentionally-skipped entry.
        from okto_neuron.companion import _incremental

        monkeypatch.setattr(_incremental, "subchunk_units_for_block", lambda **kwargs: [])

        result = Companion(
            vault, provider=StubLLM(), extractor=_OneNodeExtractor()
        ).remember(note)

        units = result.outcome["units"]
        assert units["scheduled"] == 0
        assert units["succeeded"] == 0
        assert units["reused"] == 0
        assert units["skipped"] == 0
        assert result.outcome["quality"] == "no_units"
        assert result.outcome["receipts_complete"] is False
    finally:
        vault.close()


def test_healthy_run_still_reports_complete(tmp_path: Path) -> None:
    """No regression: a run that actually extracted something stays
    ``"complete"`` with complete receipts."""
    vault = Vault.init(tmp_path / "v")
    try:
        companion = Companion(vault, provider=StubLLM(), extractor=_OneNodeExtractor())
        result = companion.remember(_doc(vault))

        assert result.outcome["quality"] == "complete"
        assert result.outcome["receipts_complete"] is True
    finally:
        vault.close()


def test_healthy_incremental_no_op_still_reports_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The discriminator is ``skipped``: an unchanged re-ingest also has
    scheduled/succeeded/reused all zero, but every block is accounted for as an
    intentional skip — that is a healthy no-op and stays ``"complete"`` with
    complete receipts (the ledger's replay short-circuit depends on it)."""
    monkeypatch.setenv("OKTO_NEURON_INCREMENTAL_INGEST", "1")

    vault = Vault.init(tmp_path / "v")
    try:
        note = _doc(vault, "# Note\n\nAlpha beta gamma delta.\n")
        Companion(vault, provider=StubLLM(), extractor=_OneNodeExtractor()).remember(note)

        result = Companion(
            vault, provider=StubLLM(), extractor=_OneNodeExtractor()
        ).remember(note)

        units = result.outcome["units"]
        assert units["scheduled"] == 0
        assert units["succeeded"] == 0
        assert units["skipped"] >= 1
        assert result.outcome["quality"] == "complete"
        assert result.outcome["receipts_complete"] is True
    finally:
        vault.close()
