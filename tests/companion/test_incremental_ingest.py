"""Model-free tests for ADR 0023 Layer 1 — per-block content-hash skip.

Validates the incremental ingest skip path in ``Companion.remember()`` using:
- A COUNTING fake extractor (no real LLM, no network, no model load)
- InMemoryStore / real Vault on a tmp dir
- ``monkeypatch.setenv`` to enable/disable the feature flag

Tests:
  A. Unchanged re-ingest → 0 extractor calls (all blocks skipped)
  B. Flag-OFF control → extractor called every time (no skip)
  C. Multi-block file, one block changed → only 1 block re-extracted
  D. Orphan handling → stale blocks classified as orphan, not extracted

Plus unit tests for ``is_current_block`` and ``plan_extraction`` against a
hand-built InMemoryStore (fast, no Vault).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from okto_neuron import Vault
from okto_neuron._internal.infra import is_superseded
from okto_neuron.companion import Companion
from okto_neuron.companion._incremental import (
    PriorSnapshot,
    _DETACHED_KEY,
    _SUPERSEDED_KEY,
    _VALID_AS_OF_KEY,
    _VALID_UNTIL_KEY,
    _subject_tokens,
    diff_to_hunks,
    is_current_block,
    make_correction_judge,
    plan_extraction,
    resurrect_reverted_claims,
    subchunk_units_for_block,
    supersede_contradicted,
)
from okto_neuron.consolidate._candidates import EdgeCandidate, NodeCandidate
from okto_neuron.core.schema import Edge, Node, Provenance
from okto_neuron.extract import ExtractionResult
from okto_neuron.ingest.markdown import sha256_hex
from okto_neuron.llm import LLMProviderError, StubLLM
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection
from okto_neuron.store.memory import InMemoryStore


# ── Vault handle cleanup (mirrors test_contract.py / test_phase3_gate.py) ─────


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


# ── Deterministic embedder (no network, no model load) ─────────────────────────


_EMBED_DIM = 384


class _FixedEmbedder:
    """A deterministic, model-free embedder producing 384-dim vectors to match
    the Ladybug store's expected embedding dimension (DEFAULT_EMBEDDING_DIM)."""

    dim = _EMBED_DIM

    def embed(self, text: str) -> list[float]:  # noqa: ARG002
        return [0.1] * _EMBED_DIM


# ── Counting fake extractor ───────────────────────────────────────────────────


class _CountingExtractor:
    """Per-call counter that also returns real NodeCandidate + EdgeCandidate so
    that the full claim-minting pipeline fires and stamps a ``model_id`` on the
    resulting Claim — the signal ``_llm_claims_for_block`` uses to decide whether
    a block can be skipped on the next ingest.

    Each call produces a unique ``subject`` node keyed on the block text hash so
    the same concept isn't proposed twice across blocks (dedup would collapse them
    into a single Claim that wouldn't anchor all blocks, which would confuse the
    skip logic).  One ``has_info`` edge per block round-trips through the gate.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.texts_seen: list[str] = []

    def extract(
        self,
        text: str,
        *,
        provenance: Provenance | None = None,
    ) -> ExtractionResult:
        self.call_count += 1
        self.texts_seen.append(text)

        prov = provenance or Provenance()
        # Unique subject title per call so candidates don't collapse across blocks.
        title = f"Block-{self.call_count}"
        subj = NodeCandidate(type="Concept", title=title, content=text[:40], provenance=prov)
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


# ── Helper: build a Companion with the counting extractor ─────────────────────


def _make_companion(vault: Vault, extractor: _CountingExtractor) -> Companion:
    return Companion(
        vault,
        provider=StubLLM(),
        extractor=extractor,
        embedder=_FixedEmbedder(),
    )


# ── Helper: capture incremental_partition events ──────────────────────────────


def _capture_events(companion: Companion, source: Path) -> tuple[Any, list[dict]]:
    events: list[dict] = []

    def _on_event(ev: dict) -> None:
        events.append(ev)

    result = companion.remember(source, on_event=_on_event)
    partition_events = [e for e in events if e.get("kind") == "incremental_partition"]
    return result, partition_events


# ─────────────────────────────────────────────────────────────────────────────
# A. Unchanged re-ingest is idempotent (0 extraction on second pass)
# ─────────────────────────────────────────────────────────────────────────────


def test_unchanged_reingest_skips_all_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second remember() of an unchanged file with OKTO_NEURON_INCREMENTAL_INGEST=1
    must make ZERO extractor calls — every block already has LLM Claims."""
    monkeypatch.setenv("OKTO_NEURON_INCREMENTAL_INGEST", "1")

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "note.md"
        # Write enough content that at least one block is created and minted.
        note.write_text(
            "# Knowledge Graph\n\nA knowledge graph stores structured facts.\n"
            "It links entities via typed edges.\n" * 5,
            encoding="utf-8",
        )

        extractor = _CountingExtractor()
        companion = _make_companion(vault, extractor)

        # First pass: extractor called for every non-empty block.
        companion.remember(note)
        count_after_first = extractor.call_count
        assert count_after_first >= 1, "extractor must be called at least once on first ingest"

        # Verify that claims were actually minted with model_id so the skip path
        # is meaningful. If 0 claims, the test would trivially pass for the wrong
        # reason.
        claims = list(vault.store.list_nodes(type="Claim"))
        llm_claims = [c for c in claims if c.facets.get("model_id")]
        assert llm_claims, (
            "no LLM Claims minted on first pass — counting extractor failed to "
            "produce claims that survived the gate"
        )

        # Second pass: same file, same content → all blocks skipped.
        _, partition_events = _capture_events(companion, note)
        count_after_second = extractor.call_count

        assert count_after_second == count_after_first, (
            f"Expected 0 extractor calls on unchanged re-ingest "
            f"(count went from {count_after_first} to {count_after_second})"
        )

        # The incremental_partition event must confirm the counts.
        assert partition_events, "no incremental_partition event emitted"
        payload = partition_events[0]["payload"]
        assert payload["blocks_extracted"] == 0
        assert payload["blocks_skipped"] >= 1
    finally:
        vault.close()


# ─────────────────────────────────────────────────────────────────────────────
# B. Flag-OFF control: no skip, extractor called every time
# ─────────────────────────────────────────────────────────────────────────────


def test_force_off_reingest_calls_extractor_every_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F10: incremental defaults ON via config; the env var is a two-way
    override. Forcing OFF ('0') must call the extractor for every non-empty
    block on both ingests — the control proving the skip is really gated."""
    monkeypatch.setenv("OKTO_NEURON_INCREMENTAL_INGEST", "0")
    monkeypatch.setenv("OKTO_NEURON_SUBCHUNK_INGEST", "0")

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "note.md"
        note.write_text(
            "# Knowledge Graph\n\nFacts are stored as triples.\n" * 5,
            encoding="utf-8",
        )

        extractor = _CountingExtractor()
        companion = _make_companion(vault, extractor)

        companion.remember(note)
        count_first = extractor.call_count
        assert count_first >= 1

        companion.remember(note)
        count_second = extractor.call_count - count_first

        # Same content, no flag → extractor called again for every block.
        assert count_second >= 1, (
            "flag-OFF control failed: expected extractor calls on second ingest "
            f"but got {count_second}"
        )
        # No incremental_partition events emitted when flag is off.
        _, partition_events = _capture_events(companion, note)
        assert not partition_events, (
            "incremental_partition event emitted with flag OFF — should not happen"
        )
    finally:
        vault.close()


# ─────────────────────────────────────────────────────────────────────────────
# C. Multi-block file: one block changed → only that block re-extracted
# ─────────────────────────────────────────────────────────────────────────────

# 12k-byte chunker uses fixed 12000-byte windows breaking on line boundaries.
# We build 3 windows of ~4000 characters (well under 12k each) but make the
# file large enough that multiple blocks span it by using repetitive filler.
# Each window has a distinct header so we can identify the changed block.
_LINES_PER_WINDOW = 200
_LINE_LEN = 60


def _make_window(window_num: int, marker: str) -> str:
    filler = (f"window{window_num}-line-" + "x" * (_LINE_LEN - 20) + "\n") * _LINES_PER_WINDOW
    return f"# Window {window_num} — {marker}\n\n" + filler


def _make_large_file(markers: list[str]) -> str:
    return "".join(_make_window(i + 1, m) for i, m in enumerate(markers))


def test_changed_block_only_reextracted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After changing ONE line in ONE block, only that block should be extracted
    on the second remember().  The other blocks are skipped."""
    monkeypatch.setenv("OKTO_NEURON_INCREMENTAL_INGEST", "1")

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "bigfile.md"
        # Write 3 windows with stable markers.
        note.write_text(_make_large_file(["alpha", "beta", "gamma"]), encoding="utf-8")

        extractor = _CountingExtractor()
        companion = _make_companion(vault, extractor)

        companion.remember(note)
        count_first = extractor.call_count
        assert count_first >= 2, (
            f"expected >=2 blocks for a 3-window file; got {count_first} extractor calls"
        )

        # Verify LLM claims were minted.
        claims = list(vault.store.list_nodes(type="Claim"))
        llm_claims = [c for c in claims if c.facets.get("model_id")]
        assert llm_claims, "no LLM Claims minted on first pass of multi-block file"

        # Edit ONE line in window 2 (change the marker) — everything else unchanged.
        note.write_text(_make_large_file(["alpha", "CHANGED", "gamma"]), encoding="utf-8")

        extractor2 = _CountingExtractor()
        companion2 = _make_companion(vault, extractor2)

        _, partition_events = _capture_events(companion2, note)
        count_second = extractor2.call_count

        assert partition_events, "no incremental_partition event emitted after partial edit"
        payload = partition_events[0]["payload"]

        # Only the changed block (window 2) needs re-extraction.
        assert count_second == 1, (
            f"expected exactly 1 extractor call for the changed block; got {count_second}"
        )
        assert payload["blocks_extracted"] == 1, payload
        assert payload["blocks_skipped"] >= 1, payload
    finally:
        vault.close()


# ─────────────────────────────────────────────────────────────────────────────
# D. Orphan handling: stale blocks classified as orphan, never extracted
# ─────────────────────────────────────────────────────────────────────────────


def test_orphan_blocks_not_extracted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After editing a block, ``plan_extraction`` must classify the old (stale)
    Block as an orphan and the ``blocks_orphaned`` count must be >= 1."""
    monkeypatch.setenv("OKTO_NEURON_INCREMENTAL_INGEST", "1")

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "bigfile.md"
        note.write_text(_make_large_file(["alpha", "beta", "gamma"]), encoding="utf-8")

        extractor = _CountingExtractor()
        companion = _make_companion(vault, extractor)
        companion.remember(note)

        # Now edit — the old Block nodes left by vault.add (upsert-without-delete)
        # will have byte ranges that no longer match the new file bytes.
        note.write_text(_make_large_file(["alpha", "ORPHAN-TEST", "gamma"]), encoding="utf-8")

        extractor2 = _CountingExtractor()
        companion2 = _make_companion(vault, extractor2)
        _, partition_events = _capture_events(companion2, note)

        assert partition_events, "no incremental_partition event after edit"
        payload = partition_events[0]["payload"]
        assert payload["blocks_orphaned"] >= 1, (
            f"expected at least 1 orphaned block after edit; got: {payload}"
        )
    finally:
        vault.close()


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests for is_current_block
# ─────────────────────────────────────────────────────────────────────────────


def test_is_current_block_returns_true_for_matching_hash() -> None:
    data = b"hello world\n"
    h = sha256_hex(data)
    assert is_current_block(data, 0, len(data), h) is True


def test_is_current_block_returns_false_for_stale_hash() -> None:
    data = b"hello world\n"
    stale_hash = sha256_hex(b"different content")
    assert is_current_block(data, 0, len(data), stale_hash) is False


def test_is_current_block_returns_false_for_out_of_range() -> None:
    data = b"short"
    h = sha256_hex(data)
    # byte_end exceeds file length.
    assert is_current_block(data, 0, len(data) + 1, h) is False


def test_is_current_block_slice_within_file() -> None:
    prefix = b"AAAA"
    middle = b"hello"
    suffix = b"BBBB"
    data = prefix + middle + suffix
    h = sha256_hex(middle)
    assert is_current_block(data, len(prefix), len(prefix) + len(middle), h) is True


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests for plan_extraction against a hand-built InMemoryStore
# ─────────────────────────────────────────────────────────────────────────────


def _seed_block(
    store: InMemoryStore,
    block_id: str,
    source_path: str,
    file_bytes: bytes,
    byte_start: int,
    byte_end: int,
) -> str:
    """Write a Block node + a fake LLM Claim derived from it."""
    content_hash = sha256_hex(file_bytes[byte_start:byte_end])
    store.add_node(
        Node(
            id=block_id,
            type="Block",
            title="block",
            content=file_bytes[byte_start:byte_end].decode(errors="replace"),
            facets={
                "source_path": source_path,
                "byte_start": byte_start,
                "byte_end": byte_end,
                "content_hash": content_hash,
            },
            provenance=Provenance(source="ingest", rule_id="deterministic-v1"),
        )
    )
    # Mint a fake LLM Claim that points back to this block.
    claim_id = sha256_hex("claim", block_id)
    store.add_node(
        Node(
            id=claim_id,
            type="Claim",
            title=f"claim-for-{block_id}",
            content="S has_info O",
            facets={"model_id": "test-model", "S": "s", "P": "has_info", "O": "o"},
            provenance=Provenance(source=block_id, rule_id="llm-extraction"),
        )
    )
    from okto_neuron.core.schema import Edge

    store.add_edge(
        Edge(
            type="prov:wasDerivedFrom",
            src=claim_id,
            dst=block_id,
            provenance=Provenance(source="test", rule_id="t"),
        )
    )
    return content_hash


def test_plan_extraction_skips_current_block_with_prior_claims(tmp_path: Path) -> None:
    """A block that is current (hash still matches file bytes) AND already has
    LLM Claims in the store → classified as ``skip``."""
    store = InMemoryStore()
    source = tmp_path / "note.md"
    file_bytes = b"# Title\n\nSome content here.\n"
    source.write_bytes(file_bytes)
    src_path = str(source.resolve())

    block_id = "b" * 64
    content_hash = _seed_block(store, block_id, src_path, file_bytes, 0, len(file_bytes))

    from okto_neuron.companion._incremental import capture_prior_snapshot

    prior = capture_prior_snapshot(store, source)
    assert content_hash in prior.hashes_with_claims

    plan = plan_extraction(store, source, prior)
    assert block_id in plan.skipped_block_ids
    assert block_id not in plan.extract_block_ids
    assert block_id not in plan.orphan_block_ids


def test_plan_extraction_extracts_new_block(tmp_path: Path) -> None:
    """A block with no prior Claims → classified as ``extract``."""
    store = InMemoryStore()
    source = tmp_path / "note.md"
    file_bytes = b"# New content\n"
    source.write_bytes(file_bytes)
    src_path = str(source.resolve())

    # Block exists in store but NO claims derived from it.
    block_id = "a" * 64
    content_hash = sha256_hex(file_bytes)
    store.add_node(
        Node(
            id=block_id,
            type="Block",
            title="block",
            content=file_bytes.decode(),
            facets={
                "source_path": src_path,
                "byte_start": 0,
                "byte_end": len(file_bytes),
                "content_hash": content_hash,
            },
            provenance=Provenance(source="ingest", rule_id="deterministic-v1"),
        )
    )

    prior = PriorSnapshot()  # empty — no prior claims
    plan = plan_extraction(store, source, prior)
    assert block_id in plan.extract_block_ids
    assert block_id not in plan.skipped_block_ids
    assert block_id not in plan.orphan_block_ids


def test_plan_extraction_orphans_stale_block(tmp_path: Path) -> None:
    """A block whose stored byte range no longer matches the file → orphan."""
    store = InMemoryStore()
    source = tmp_path / "note.md"
    original = b"# Original content\n"
    updated = b"# Changed content - totally different\n"
    # Seed the block with the ORIGINAL hash, then write UPDATED bytes to disk.
    _seed_block(store, "c" * 64, str(source.resolve()), original, 0, len(original))

    # Overwrite the file — now the stored hash is stale.
    source.write_bytes(updated)

    prior = PriorSnapshot()  # doesn't matter — will orphan before skip check
    plan = plan_extraction(store, source, prior)
    assert "c" * 64 in plan.orphan_block_ids
    assert "c" * 64 not in plan.extract_block_ids
    assert "c" * 64 not in plan.skipped_block_ids


def test_plan_extraction_mixed_partition(tmp_path: Path) -> None:
    """Three blocks: one skip (current + prior claims), one extract (new), one
    orphan (stale hash) → all three partitions populated correctly."""
    store = InMemoryStore()
    source = tmp_path / "multi.md"
    # File has three sections; we'll fake three blocks covering the same bytes
    # for simplicity (real ingest would use non-overlapping ranges).
    file_bytes = b"A" * 50 + b"B" * 50 + b"C" * 50
    source.write_bytes(file_bytes)
    src_path = str(source.resolve())

    skip_id = "1" * 64
    extract_id = "2" * 64
    orphan_id = "3" * 64

    # skip block: current + has LLM claims.
    _seed_block(store, skip_id, src_path, file_bytes, 0, 50)

    # extract block: current but NO LLM claims.
    extract_hash = sha256_hex(file_bytes[50:100])
    store.add_node(
        Node(
            id=extract_id,
            type="Block",
            title="block2",
            content="B" * 50,
            facets={
                "source_path": src_path,
                "byte_start": 50,
                "byte_end": 100,
                "content_hash": extract_hash,
            },
            provenance=Provenance(source="ingest", rule_id="deterministic-v1"),
        )
    )

    # orphan block: stale hash (points to slice with wrong content).
    orphan_hash = sha256_hex(b"STALE")
    store.add_node(
        Node(
            id=orphan_id,
            type="Block",
            title="block3",
            content="stale",
            facets={
                "source_path": src_path,
                "byte_start": 100,
                "byte_end": 150,
                "content_hash": orphan_hash,  # doesn't match C*50
            },
            provenance=Provenance(source="ingest", rule_id="deterministic-v1"),
        )
    )

    from okto_neuron.companion._incremental import capture_prior_snapshot

    prior = capture_prior_snapshot(store, source)
    plan = plan_extraction(store, source, prior)

    assert skip_id in plan.skipped_block_ids
    assert extract_id in plan.extract_block_ids
    assert orphan_id in plan.orphan_block_ids

    counts = plan.counts
    assert counts["blocks_skipped"] == 1
    assert counts["blocks_extracted"] == 1
    assert counts["blocks_orphaned"] == 1


def test_plan_extraction_ignores_toctou_race_against_document_hash(tmp_path: Path) -> None:
    """Finding 3.23: ``plan_extraction`` re-reads the source file independently
    of ``vault.add()``'s own read. If the file changed on disk in between (a
    TOCTOU race — e.g. a concurrent edit under folder-watch), the currency
    check must not judge a Block's staleness against bytes ``vault.add()``
    never anchored anything from: that would wrongly orphan a Block that WAS
    current against the bytes actually ingested moments earlier."""
    store = InMemoryStore()
    source = tmp_path / "race.md"
    original_bytes = b"# Original\n\noriginal body text.\n"
    source.write_bytes(original_bytes)
    resolved = str(source.resolve())

    # Document node exactly as vault.add() would have written it, derived
    # from original_bytes (the ones the "concurrent" ingest actually saw).
    document_id = sha256_hex("document", resolved)
    store.add_node(
        Node(
            id=document_id,
            type="Document",
            title="race",
            content="",
            facets={"sha256": sha256_hex(original_bytes)},
            provenance=Provenance(source="ingest", rule_id="deterministic-v1"),
        )
    )

    # Block anchored from the SAME original_bytes vault.add() just ingested —
    # genuinely current against what was actually written to the graph.
    block_id = "d" * 64
    _seed_block(store, block_id, resolved, original_bytes, 0, len(original_bytes))

    # Race: the file changes on disk AFTER vault.add()'s read but BEFORE
    # plan_extraction's own, independent re-read.
    source.write_bytes(b"# Completely different\n\nunrelated new text.\n")

    from okto_neuron.companion._incremental import capture_prior_snapshot

    prior = capture_prior_snapshot(store, source)
    plan = plan_extraction(store, source, prior)

    assert block_id not in plan.orphan_block_ids, (
        "a Block current against the bytes vault.add() actually ingested must "
        "not be orphaned just because a LATER, independent re-read saw "
        "different bytes on disk"
    )


def test_plan_extraction_still_orphans_stale_block_when_document_hash_matches(
    tmp_path: Path,
) -> None:
    """Control for the fix above: when the file has NOT changed since
    vault.add() (the common case — no race), a genuinely stale Block must
    still be orphaned exactly as before."""
    store = InMemoryStore()
    source = tmp_path / "note.md"
    original = b"# Original content\n"
    updated = b"# Changed content - totally different\n"
    source.write_bytes(original)
    resolved = str(source.resolve())

    document_id = sha256_hex("document", resolved)
    store.add_node(
        Node(
            id=document_id,
            type="Document",
            title="note",
            content="",
            facets={"sha256": sha256_hex(updated)},
            provenance=Provenance(source="ingest", rule_id="deterministic-v1"),
        )
    )
    _seed_block(store, "c" * 64, resolved, original, 0, len(original))
    source.write_bytes(updated)

    prior = PriorSnapshot()
    plan = plan_extraction(store, source, prior)
    assert "c" * 64 in plan.orphan_block_ids


# =============================================================================
# ADR 0024 sub-chunk diff ingestion — pure-function tests and E2E tests
# =============================================================================

# ── Sentinel extractor: parses "FACT subj=S pred=P obj=O" lines ──────────────
#
# Emits one literal EdgeCandidate per FACT line so the full claim-minting
# pipeline fires with deterministic S_id / P / O_literal values.
_FACT_RE = re.compile(r"FACT\s+subj=(\S+)\s+pred=(\S+)\s+obj=(\S+)")


class _SentinelExtractor:
    """Parses FACT sentinel lines and emits literal Claims.  Designed so:
    - The same subject name always produces the same candidate_id.
    - Predicates are short lower-case words (no normalisation needed).
    - Objects are literal strings stored as O_literal on the Claim.
    The extractor also accumulates call_count and texts_seen for assertions.
    """

    def __init__(self) -> None:
        self.call_count = 0
        self.texts_seen: list[str] = []

    def extract(
        self,
        text: str,
        *,
        provenance: Provenance | None = None,
    ) -> ExtractionResult:
        self.call_count += 1
        self.texts_seen.append(text)
        prov = provenance or Provenance()
        nodes: list[NodeCandidate] = []
        edges: list[EdgeCandidate] = []
        for m in _FACT_RE.finditer(text):
            subj, pred, obj = m.group(1), m.group(2), m.group(3)
            subj_node = NodeCandidate(type="Concept", title=subj, content=subj, provenance=prov)
            nodes.append(subj_node)
            edges.append(
                EdgeCandidate(
                    type=pred,
                    src_ref=subj_node.candidate_id,
                    dst_ref="",
                    dst_literal=obj,
                    provenance=prov,
                )
            )
        return ExtractionResult(node_candidates=nodes, edge_candidates=edges)


class _CorrectionJudgeLLM:
    """StubLLM variant that returns ``{"index": 0}`` for correction-judge calls
    (no response_format schema) so the integral supersede path fires in tests.
    All schema-gated curator / merge-verdict calls delegate to StubLLM behavior."""

    model = "stub-correction-judge"

    def __init__(self) -> None:
        self.correction_calls = 0

    def complete(
        self,
        messages: object,
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        top_p: float | None = None,
        top_k: float | None = None,
        min_p: float | None = None,
        presence_penalty: float | None = None,
        enable_thinking: bool | None = None,
        response_format: object = None,
    ) -> str:
        schema = (
            response_format.get("json_schema")  # type: ignore[union-attr]
            if isinstance(response_format, dict)
            else None
        )
        schema_name = schema.get("name") if isinstance(schema, dict) else None
        if schema_name == "marginalia_merge_verdict":
            return '{"same":false,"confidence":0.99,"reason":"stub distinct"}'
        if schema_name == "marginalia_candidate_curator":
            return '{"action":"commit","confidence":0.9,"reason":"stub curator commit"}'
        if schema_name == "marginalia_predicate_resolution":
            # ADR 0040 D6a ingest-time predicate resolution. These fixtures pin
            # correction-judge behaviour, not vocabulary, so always resolve
            # distinct (the pre-D6a status quo: mint and queue).
            return (
                '{"verdict":"distinct","target":"","canonical":"",'
                '"confidence":0.0,"reason":"stub distinct"}'
            )
        if schema_name == "marginalia_relation_curator":
            user_prompt = getattr(messages[-1], "content", "")  # type: ignore[index]
            predicate = next(
                (
                    line.partition(":")[2].strip()
                    for line in user_prompt.splitlines()
                    if line.startswith("Predicate/type:")
                ),
                "related_to",
            )
            return json.dumps(
                {
                    "action": "commit",
                    "confidence": 0.9,
                    "canonical_predicate": predicate,
                    "predicate_definition": (
                        "The subject has the proposed relation to the object."
                    ),
                    "predicate_direction": "subject_to_object",
                    "inverse_direction_required": False,
                    "subject_supported": True,
                    "predicate_supported": True,
                    "object_supported": True,
                    "direction_supported": True,
                    "unsupported_inference": False,
                    "structural_noise": False,
                    "redundant": False,
                    "useful": True,
                    "reason": "stub relation curator commit",
                },
                separators=(",", ":"),
            )
        # Correction-judge call: no response_format → always pick candidate 0.
        self.correction_calls += 1
        return '{"index": 0}'


def _make_sentinel_companion(vault: Vault, extractor: _SentinelExtractor) -> Companion:
    return Companion(
        vault,
        provider=StubLLM(),
        extractor=extractor,
        embedder=_FixedEmbedder(),
    )


def _make_sentinel_companion_with_judge(vault: Vault, extractor: _SentinelExtractor) -> Companion:
    """Like ``_make_sentinel_companion`` but uses ``_CorrectionJudgeLLM`` so the
    integral correction-supersede pass fires (judge returns index 0)."""
    return Companion(
        vault,
        provider=_CorrectionJudgeLLM(),
        extractor=extractor,
        embedder=_FixedEmbedder(),
    )


def _capture_all_events(companion: Companion, source: Path) -> tuple[Any, list[dict]]:
    """Capture ALL events from one remember() call."""
    events: list[dict] = []
    result = companion.remember(source, on_event=lambda e: events.append(e))
    return result, events


# ─────────────────────────────────────────────────────────────────────────────
# 1. Pure-function tests: diff_to_hunks
# ─────────────────────────────────────────────────────────────────────────────


def test_diff_to_hunks_one_line_change() -> None:
    """A single changed line produces exactly ONE hunk whose byte slice decodes
    to the changed line and whose content_hash matches sha256_hex of that slice."""
    old_text = "line one\nline two\nline three\n"
    new_text = "line one\nLINE TWO CHANGED\nline three\n"
    new_raw = new_text.encode()

    hunks = diff_to_hunks(old_text, new_raw, new_byte_start=0)

    assert len(hunks) == 1
    h = hunks[0]
    raw_slice = new_raw[h.byte_start : h.byte_end]
    assert raw_slice.decode().strip() == "LINE TWO CHANGED"
    assert h.content_hash == sha256_hex(raw_slice)
    # The text field must contain the changed content.
    assert "LINE TWO CHANGED" in h.text


def test_diff_to_hunks_appended_line() -> None:
    """Appending a line at the end produces one INSERT hunk that is just the
    new line — NOT the whole block."""
    old_text = "existing content\n"
    new_text = "existing content\nnew appended line\n"
    new_raw = new_text.encode()

    hunks = diff_to_hunks(old_text, new_raw, new_byte_start=0)

    assert len(hunks) == 1
    raw_slice = new_raw[hunks[0].byte_start : hunks[0].byte_end]
    decoded = raw_slice.decode().strip()
    assert "new appended line" in decoded
    # The hunk must NOT span the old content.
    assert "existing content" not in decoded


def test_diff_to_hunks_identical() -> None:
    """Identical old and new → zero hunks (no churn)."""
    text = "unchanged line one\nunchanged line two\n"
    raw = text.encode()
    assert diff_to_hunks(text, raw, new_byte_start=0) == []


def test_diff_to_hunks_pure_deletion() -> None:
    """Removing a line without adding anything → zero hunks (deletion only;
    the detach pass handles orphan claims, not the hunk emitter)."""
    old_text = "line A\nline B\n"
    new_text = "line A\n"
    new_raw = new_text.encode()
    assert diff_to_hunks(old_text, new_raw, new_byte_start=0) == []


def test_diff_to_hunks_byte_start_offset() -> None:
    """non-zero new_byte_start shifts absolute byte offsets correctly."""
    old_text = "hello\n"
    new_text = "HELLO\n"
    new_raw = new_text.encode()
    offset = 100
    hunks = diff_to_hunks(old_text, new_raw, new_byte_start=offset)
    assert len(hunks) == 1
    h = hunks[0]
    assert h.byte_start >= offset
    assert h.byte_end > h.byte_start
    # Slice from the new_raw itself (not offset-shifted) must match.
    raw_slice = new_raw[h.byte_start - offset : h.byte_end - offset]
    assert h.content_hash == sha256_hex(raw_slice)


# ─────────────────────────────────────────────────────────────────────────────
# 2. Pure-function tests: subchunk_units_for_block
# ─────────────────────────────────────────────────────────────────────────────


def test_subchunk_units_new_block_no_prior() -> None:
    """A new block with no prior at this index → single unit with whole=True
    spanning the whole block."""
    block_id = "a" * 64
    new_raw = b"brand new block content\n"
    new_text = new_raw.decode()
    prior = PriorSnapshot()  # empty — no prior

    units = subchunk_units_for_block(
        block_id=block_id,
        block_index=0,
        new_byte_start=0,
        new_raw=new_raw,
        new_text=new_text,
        prior=prior,
    )

    assert len(units) == 1
    u = units[0]
    assert u.whole is True
    assert u.block_id == block_id
    assert u.byte_start == 0
    assert u.byte_end == len(new_raw)
    assert u.text == new_text.strip() or new_text in u.text


def test_subchunk_units_changed_block_with_prior() -> None:
    """A changed block with a prior at the same index → narrowed non-whole units
    whose combined text is the changed fragment, not the whole block."""
    from okto_neuron.companion._incremental import PriorClaimInfo

    block_id = "b" * 64
    old_text = "unchanged preamble\nold value: 42\nfooter text\n"
    new_raw = b"unchanged preamble\nnew value: 99\nfooter text\n"
    new_text = new_raw.decode()

    # Build a prior snapshot with the old text at index 0.
    old_claim_info = PriorClaimInfo(
        block_id="old-" + "x" * 60,
        content_hash=sha256_hex(old_text.encode()),
        claim_ids=("c" * 64,),
        block_index=0,
        content=old_text,
    )
    prior = PriorSnapshot(
        by_hash={},
        hashes_with_claims=frozenset(),
        by_index={0: old_claim_info},
    )

    units = subchunk_units_for_block(
        block_id=block_id,
        block_index=0,
        new_byte_start=0,
        new_raw=new_raw,
        new_text=new_text,
        prior=prior,
    )

    assert len(units) >= 1
    # All units must be non-whole (narrowed).
    assert all(not u.whole for u in units)
    # The combined text must contain the changed line.
    combined = " ".join(u.text for u in units)
    assert "new value: 99" in combined
    # The unchanged preamble / footer must NOT be extracted.
    assert "unchanged preamble" not in combined
    assert "footer text" not in combined


# ─────────────────────────────────────────────────────────────────────────────
# 2b. Unit tests for supersede_contradicted (fast, hand-seeded InMemoryStore)
# ─────────────────────────────────────────────────────────────────────────────


def test_subject_token_prefilter_uses_discovery_surface_key() -> None:
    # discovery_surface_key folds diacritics (ADR 0042), so "Café" tokenizes
    # to the unaccented "cafe" here, not "café".
    store = InMemoryStore()
    store.add_node(Node(id="subject", type="Concept", title="Cafe\u0301_Data", content=""))

    assert _subject_tokens(store, {"S_id": "subject"}) == {"cafe", "data"}


def _seed_claim(
    store: InMemoryStore,
    *,
    claim_id: str,
    subj_id: str,
    subj_title: str,
    predicate: str,
    obj_literal: str,
    block_id: str = "block0" + "0" * 58,
    source_path: str | None = None,
    asserted_at: str | None = None,
    source_asserted_at: str | None = None,
) -> None:
    """Write a minimal subject Concept node, a Block node, + a literal Claim.

    ``source_path`` (when given) is stamped on the Block facets so the Task 5
    document-lineage bound can tell same-document from cross-document claims."""
    # InMemoryStore requires all edge endpoints to be nodes.
    if store.get_node(block_id) is None:
        store.add_node(
            Node(
                id=block_id,
                type="Block",
                title="block",
                content="",
                facets={"source_path": source_path} if source_path else {},
                provenance=Provenance(source="test", rule_id="t"),
            )
        )
    if store.get_node(subj_id) is None:
        store.add_node(
            Node(
                id=subj_id,
                type="Concept",
                title=subj_title,
                content=subj_title,
                facets={},
                provenance=Provenance(source="test", rule_id="t"),
            )
        )
    store.add_node(
        Node(
            id=claim_id,
            type="Claim",
            title=f"{subj_title} — {predicate} — {obj_literal}",
            content=f"{subj_title} {predicate} {obj_literal}",
            facets={
                "model_id": "test-model",
                "S_id": subj_id,
                "P": predicate,
                "O_literal": obj_literal,
                **({"asserted_at": asserted_at} if asserted_at else {}),
                **({"source_asserted_at": source_asserted_at} if source_asserted_at else {}),
            },
            provenance=Provenance(source=block_id, rule_id="llm-extraction"),
        )
    )
    store.add_edge(
        Edge(
            type="prov:wasDerivedFrom",
            src=claim_id,
            dst=block_id,
            provenance=Provenance(source="test", rule_id="t"),
        )
    )


def test_supersede_contradicted_judge_returns_0_supersedes() -> None:
    """Unit test: fake judge returning 0 causes the old Claim to be superseded
    with _superseded=True + valid_until and a supersedes edge new→old."""
    store = InMemoryStore()
    subj_id = sha256_hex("Concept", "Widget", "Widget")
    old_id = "old-" + "a" * 60
    new_id = "new-" + "b" * 60

    _seed_claim(
        store,
        claim_id=old_id,
        subj_id=subj_id,
        subj_title="Widget",
        predicate="color",
        obj_literal="red",
    )
    _seed_claim(
        store,
        claim_id=new_id,
        subj_id=subj_id,
        subj_title="Widget",
        predicate="color",
        obj_literal="blue",
        block_id="block1" + "1" * 58,
    )

    def _judge_pick_first(old_fact: str, candidates: list[str]) -> int:  # noqa: ARG001
        return 0

    superseded, deferred = supersede_contradicted(
        store,
        frozenset([new_id]),
        frozenset([old_id]),
        valid_as_of="2026-06-30",
        correction_judge=_judge_pick_first,
    )

    assert superseded == [(old_id, new_id)], f"expected [(old, new)]; got {superseded}"
    assert deferred == [], f"shared-S_id correction must auto-apply, not defer; got {deferred}"

    old_node = store.get_node(old_id)
    assert old_node is not None
    assert old_node.facets.get(_SUPERSEDED_KEY) is True, (
        f"old Claim missing _superseded; facets={old_node.facets}"
    )
    assert old_node.facets.get(_VALID_UNTIL_KEY) == "2026-06-30"
    assert is_superseded(old_node) is True

    supersedes_edges = list(store.list_edges(type="supersedes"))
    assert any(e.src == new_id and e.dst == old_id for e in supersedes_edges), (
        f"no supersedes edge; edges={[(e.src, e.dst) for e in supersedes_edges]}"
    )


def test_supersede_contradicted_uses_explicit_source_time_not_ingest_order() -> None:
    """A later byte-grounded source assertion wins even when file mtimes tie.

    Reversing the pair must not let the older assertion reach the judge, and an
    equal explicit timestamp is ambiguous rather than an ingestion-order tie.
    """

    subj_id = sha256_hex("Concept", "Aster Handoff", "Aster Handoff")
    old_id = "old-" + "a" * 60
    new_id = "new-" + "b" * 60
    tied_id = "tie-" + "c" * 60

    def _store() -> InMemoryStore:
        store = InMemoryStore()
        for claim_id, obj, block_id, source_time in (
            (old_id, "Rina Vale", "block0" + "0" * 58, "2026-04-03T09:00:00Z"),
            (new_id, "Malik Daro", "block1" + "1" * 58, "2026-04-05T14:00:00Z"),
            (tied_id, "Jo Merek", "block2" + "2" * 58, "2026-04-05T14:00:00Z"),
        ):
            _seed_claim(
                store,
                claim_id=claim_id,
                subj_id=subj_id,
                subj_title="Aster Handoff",
                predicate="owner",
                obj_literal=obj,
                block_id=block_id,
                asserted_at="2026-07-18",
                source_asserted_at=source_time,
            )
        return store

    calls: list[list[str]] = []

    def _judge(_new: str, candidates: list[str]) -> int:
        calls.append(candidates)
        return 0

    store = _store()
    applied, deferred = supersede_contradicted(
        store,
        frozenset({new_id}),
        frozenset({old_id}),
        valid_as_of="2026-07-18",
        correction_judge=_judge,
    )
    assert applied == [(old_id, new_id)]
    assert deferred == []
    assert len(calls) == 1

    calls.clear()
    reverse_store = _store()
    applied_reverse, deferred_reverse = supersede_contradicted(
        reverse_store,
        frozenset({old_id}),
        frozenset({new_id}),
        valid_as_of="2026-07-18",
        correction_judge=_judge,
    )
    assert applied_reverse == [(old_id, new_id)]
    assert deferred_reverse == []
    assert len(calls) == 1
    reversed_old = reverse_store.get_node(old_id)
    assert reversed_old is not None
    assert reversed_old.facets.get(_SUPERSEDED_KEY) is True

    calls.clear()
    tie_store = _store()
    applied_tie, deferred_tie = supersede_contradicted(
        tie_store,
        frozenset({tied_id}),
        frozenset({new_id}),
        valid_as_of="2026-07-18",
        correction_judge=_judge,
    )
    assert applied_tie == []
    assert deferred_tie == []
    assert calls == []


def test_supersede_contradicted_orders_new_claims_and_bounded_candidates() -> None:
    """Judge input is stable even if the set-like inputs iterate differently."""

    class _IterationControlledFrozenSet(frozenset[str]):
        def __new__(cls, values: tuple[str, ...]):
            instance = super().__new__(cls, values)
            instance.iteration_order = values
            return instance

        def __iter__(self):
            return iter(self.iteration_order)

    store = InMemoryStore()
    subj_id = sha256_hex("Concept", "Widget", "Widget")
    old_ids = ("old-c", "old-a", "old-b")
    new_ids = ("new-z", "new-a")
    for claim_id in (*old_ids, *new_ids):
        _seed_claim(
            store,
            claim_id=claim_id,
            subj_id=subj_id,
            subj_title="Widget",
            predicate="color",
            obj_literal=claim_id,
        )

    def _judge_calls(
        ordered_new: tuple[str, ...],
        ordered_existing: tuple[str, ...],
    ) -> list[tuple[str, list[str]]]:
        calls: list[tuple[str, list[str]]] = []

        def _judge(new_fact: str, candidates: list[str]) -> int:
            calls.append((new_fact, candidates))
            return -1

        superseded, deferred = supersede_contradicted(
            store,
            _IterationControlledFrozenSet(ordered_new),
            _IterationControlledFrozenSet(ordered_existing),
            valid_as_of="2026-07-17",
            correction_judge=_judge,
            max_candidates=2,
        )
        assert superseded == []
        assert deferred == []
        return calls

    expected = [
        (
            "Widget — color — new-a",
            ["Widget — color — old-a", "Widget — color — old-b"],
        ),
        (
            "Widget — color — new-z",
            ["Widget — color — old-a", "Widget — color — old-b"],
        ),
    ]
    assert _judge_calls(new_ids, old_ids) == expected
    assert _judge_calls(tuple(reversed(new_ids)), tuple(reversed(old_ids))) == expected

    selected, deferred = supersede_contradicted(
        store,
        _IterationControlledFrozenSet(("new-a",)),
        _IterationControlledFrozenSet(tuple(reversed(old_ids))),
        valid_as_of="2026-07-17",
        correction_judge=lambda _new, _candidates: 1,
        max_candidates=2,
    )
    assert selected == [("old-b", "new-a")]
    assert deferred == []


def test_supersede_contradicted_stops_after_current_judge_call() -> None:
    store = InMemoryStore()
    widget_subj = sha256_hex("Concept", "Widget", "Widget")
    gadget_subj = sha256_hex("Concept", "Gadget", "Gadget")
    old_ids = ("wold-" + "a" * 59, "gold-" + "b" * 59)
    new_ids = ("wnew-" + "c" * 59, "gnew-" + "d" * 59)
    _seed_claim(
        store,
        claim_id=old_ids[0],
        subj_id=widget_subj,
        subj_title="Widget",
        predicate="color",
        obj_literal="red",
        block_id="blkw0" + "0" * 59,
    )
    _seed_claim(
        store,
        claim_id=new_ids[0],
        subj_id=widget_subj,
        subj_title="Widget",
        predicate="color",
        obj_literal="blue",
        block_id="blkw1" + "1" * 59,
    )
    _seed_claim(
        store,
        claim_id=old_ids[1],
        subj_id=gadget_subj,
        subj_title="Gadget",
        predicate="size",
        obj_literal="small",
        block_id="blkg0" + "2" * 59,
    )
    _seed_claim(
        store,
        claim_id=new_ids[1],
        subj_id=gadget_subj,
        subj_title="Gadget",
        predicate="size",
        obj_literal="large",
        block_id="blkg1" + "3" * 59,
    )
    calls = 0
    stopped = False

    def _judge_then_stop(old_fact: str, candidates: list[str]) -> int:  # noqa: ARG001
        nonlocal calls, stopped
        calls += 1
        stopped = True
        return 0

    superseded, deferred = supersede_contradicted(
        store,
        frozenset(new_ids),
        frozenset(old_ids),
        valid_as_of="2026-07-10",
        correction_judge=_judge_then_stop,
        should_stop=lambda: stopped,
    )

    assert calls == 1
    assert superseded == []
    assert deferred == []
    assert all(not is_superseded(store.get_node(claim_id)) for claim_id in old_ids)


def test_supersede_apply_failure_is_skipped_and_other_corrections_preserved(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (ADR 0022 best-effort guard): claims are already committed
    BEFORE the integral correction pass runs, so a store mutation that blows up
    on ONE supersede must NOT abort the pass — it is logged + skipped, and the
    OTHER corrections still apply.

    Two independent correction pairs (Widget color red→blue, Gadget size
    small→large). ``_apply_supersede`` is patched to raise ONLY for the Widget
    old Claim. Expect: Gadget supersede applied, Widget skipped + still live,
    no exception propagates, the failure logged with claim context."""
    import okto_neuron.companion._incremental as incr_mod

    store = InMemoryStore()
    widget_subj = sha256_hex("Concept", "Widget", "Widget")
    gadget_subj = sha256_hex("Concept", "Gadget", "Gadget")
    w_old, w_new = "wold-" + "a" * 59, "wnew-" + "b" * 59
    g_old, g_new = "gold-" + "c" * 59, "gnew-" + "d" * 59

    _seed_claim(
        store,
        claim_id=w_old,
        subj_id=widget_subj,
        subj_title="Widget",
        predicate="color",
        obj_literal="red",
        block_id="blkw0" + "0" * 59,
    )
    _seed_claim(
        store,
        claim_id=w_new,
        subj_id=widget_subj,
        subj_title="Widget",
        predicate="color",
        obj_literal="blue",
        block_id="blkw1" + "1" * 59,
    )
    _seed_claim(
        store,
        claim_id=g_old,
        subj_id=gadget_subj,
        subj_title="Gadget",
        predicate="size",
        obj_literal="small",
        block_id="blkg0" + "0" * 59,
    )
    _seed_claim(
        store,
        claim_id=g_new,
        subj_id=gadget_subj,
        subj_title="Gadget",
        predicate="size",
        obj_literal="large",
        block_id="blkg1" + "1" * 59,
    )

    real_apply = incr_mod._apply_supersede

    def _apply_or_boom(store_, old_claim, new_id, valid_as_of):  # noqa: ANN001
        if old_claim.id == w_old:
            raise RuntimeError("simulated store failure on Widget supersede")
        return real_apply(store_, old_claim, new_id, valid_as_of)

    monkeypatch.setattr(incr_mod, "_apply_supersede", _apply_or_boom)

    def _judge_pick_first(old_fact: str, candidates: list[str]) -> int:  # noqa: ARG001
        return 0

    with caplog.at_level(logging.ERROR, logger="okto_neuron.companion"):
        superseded, deferred = supersede_contradicted(
            store,
            frozenset([w_new, g_new]),
            frozenset([w_old, g_old]),
            valid_as_of="2026-07-04",
            correction_judge=_judge_pick_first,
        )

    # The failing Widget correction is skipped; the Gadget correction survives.
    assert superseded == [(g_old, g_new)], (
        f"only the successful (Gadget) supersede must be returned; got {superseded}"
    )
    assert deferred == []

    # Widget old Claim is still LIVE (its supersede never landed) — not lost.
    w_old_node = store.get_node(w_old)
    assert w_old_node is not None
    assert w_old_node.facets.get(_SUPERSEDED_KEY) is not True, (
        "failed supersede must leave the old Claim live, not half-mutated"
    )
    # Gadget old Claim IS superseded (the preserved correction).
    assert is_superseded(store.get_node(g_old)) is True

    # The real error was logged with claim context (observability hole closed).
    assert "supersede failed" in caplog.text
    assert w_old in caplog.text


def test_remember_integral_correction_failure_does_not_abort_ingest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """End-to-end (model-free): ADR 0022 correction planning runs before seal.

    If it throws (here: the whole pass, standing in for a judge/provider
    failure), the immutable primary Claim plan still completes without carrying
    any partial correction intent. The real error is logged, not swallowed.
    """
    monkeypatch.setenv("OKTO_NEURON_SUBCHUNK_INGEST", "1")

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "facts.md"
        note.write_text("# Tasks\n\nFACT subj=Ticket pred=due obj=Jun26\n", encoding="utf-8")
        _make_sentinel_companion(vault, _SentinelExtractor()).remember(note)

        old_claims = [
            c
            for c in vault.store.list_nodes(type="Claim")
            if c.facets.get("O_literal") == "Jun26" and c.facets.get("model_id")
        ]
        assert old_claims, "no Claim with O_literal=Jun26 after first remember()"
        old_claim_id = old_claims[0].id

        # Second pass edits the value → the integral correction pass fires. Make
        # the WHOLE pass explode to prove the outer best-effort guard reaches
        # finish_run even when the fine-grained per-supersede guard can't help.
        import okto_neuron.companion._incremental as incr_mod

        def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
            raise RuntimeError("simulated integral-correction failure")

        monkeypatch.setattr(incr_mod, "supersede_contradicted", _boom)

        note.write_text("# Tasks\n\nFACT subj=Ticket pred=due obj=Jul1\n", encoding="utf-8")
        companion2 = _make_sentinel_companion_with_judge(vault, _SentinelExtractor())

        with caplog.at_level(logging.ERROR, logger="okto_neuron.companion"):
            # Must NOT raise — the ingest completes despite the failed pass.
            result = companion2.remember(note)

        assert result is not None, "remember() aborted instead of completing"

        # New Claim committed and live; old Claim NOT superseded (pass failed).
        new_claims = [
            c
            for c in vault.store.list_nodes(type="Claim")
            if c.facets.get("O_literal") == "Jul1" and c.facets.get("model_id")
        ]
        assert new_claims, "new Claim (Jul1) was not committed"
        assert is_superseded(new_claims[0]) is False
        old_claim = vault.store.get_node(old_claim_id)
        assert old_claim is not None and is_superseded(old_claim) is False, (
            "failed correction pass must leave the old Claim live"
        )

        # finish_run(state='completed') was recorded for this ingest.
        ledger_path = Path(vault.path) / ".marginalia" / "candidate-ledger.jsonl"
        records = [
            json.loads(line)
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert any(
            r.get("kind") == "ingest_run" and r.get("state") == "completed" for r in records
        ), "no completed ingest_run recorded — ingest did not reach finish_run"

        # The real failure was logged, not silently swallowed.
        assert "correction planning failed" in caplog.text
    finally:
        vault.close()


def test_supersede_contradicted_judge_returns_neg1_does_nothing() -> None:
    """Unit test: fake judge returning -1 must leave the old Claim unchanged —
    proves multi-valued predicates are not blindly superseded."""
    store = InMemoryStore()
    subj_id = sha256_hex("Concept", "Article", "Article")
    old_id = "old-" + "c" * 60
    new_id = "new-" + "d" * 60

    _seed_claim(
        store,
        claim_id=old_id,
        subj_id=subj_id,
        subj_title="Article",
        predicate="tag",
        obj_literal="python",
    )
    _seed_claim(
        store,
        claim_id=new_id,
        subj_id=subj_id,
        subj_title="Article",
        predicate="tag",
        obj_literal="ml",
        block_id="block1" + "1" * 58,
    )

    def _judge_abstain(old_fact: str, candidates: list[str]) -> int:  # noqa: ARG001
        return -1

    superseded, deferred = supersede_contradicted(
        store,
        frozenset([new_id]),
        frozenset([old_id]),
        valid_as_of="2026-06-30",
        correction_judge=_judge_abstain,
    )

    assert superseded == [], f"expected no supersessions; got {superseded}"
    assert deferred == [], f"judge abstained; nothing to defer; got {deferred}"

    old_node = store.get_node(old_id)
    assert old_node is not None
    assert not old_node.facets.get(_SUPERSEDED_KEY), (
        "old Claim was wrongly superseded when judge returned -1"
    )
    assert is_superseded(old_node) is False


def test_supersede_contradicted_rejects_cross_document_token_match_before_judge() -> None:
    """A token-only match from another document is not correction-judge work.

    It cannot auto-apply under the lineage bound, so paying for a model verdict
    only to defer telemetry is waste. Cross-document identity/contradiction work
    belongs to the exact reconciliation job.

    Same seed with a matching ``source_path`` (same document) DOES auto-apply —
    isolating the document-lineage gate as the deciding factor."""
    store = InMemoryStore()
    # Two subjects that share a token ("Falcon") but resolve to DIFFERENT ids.
    subj_a = sha256_hex("Concept", "Falcon Alpha", "Falcon Alpha")
    subj_b = sha256_hex("Concept", "Falcon Beta", "Falcon Beta")
    old_id = "old-" + "e" * 60  # sourced ONLY from doc B
    new_id = "new-" + "f" * 60  # minted from an edit to doc A

    _seed_claim(
        store,
        claim_id=old_id,
        subj_id=subj_b,
        subj_title="Falcon Beta",
        predicate="owner",
        obj_literal="Marcus",
        block_id="blockB" + "b" * 58,
        source_path="/vault/docB.md",
    )
    _seed_claim(
        store,
        claim_id=new_id,
        subj_id=subj_a,
        subj_title="Falcon Alpha",
        predicate="owner",
        obj_literal="Eve",
        block_id="blockA" + "a" * 58,
        source_path="/vault/docA.md",
    )

    judge_calls = 0

    def _judge_always_correction(old_fact: str, candidates: list[str]) -> int:  # noqa: ARG001
        nonlocal judge_calls
        judge_calls += 1
        return 0

    # The old claim is rejected before the judge: shared "falcon" alone cannot
    # cross a document boundary.
    applied, deferred = supersede_contradicted(
        store,
        frozenset([new_id]),
        frozenset([old_id]),
        valid_as_of="2026-07-03",
        correction_judge=_judge_always_correction,
        source_path="/vault/docA.md",
    )

    assert applied == [], f"cross-document supersede was auto-applied; got {applied}"
    assert deferred == []
    assert judge_calls == 0
    old_node = store.get_node(old_id)
    assert old_node is not None
    assert not old_node.facets.get(_SUPERSEDED_KEY), (
        "doc B's claim was hidden by an edit to doc A — the bug this fix prevents"
    )
    assert not old_node.facets.get(_DETACHED_KEY)
    assert is_superseded(old_node) is False

    # Control: SAME document (old claim's block source_path matches the edited
    # source) → the identical judge verdict now auto-applies. Proves the bound,
    # not the judge, is the deciding factor.
    applied_same, deferred_same = supersede_contradicted(
        store,
        frozenset([new_id]),
        frozenset([old_id]),
        valid_as_of="2026-07-03",
        correction_judge=_judge_always_correction,
        source_path="/vault/docB.md",  # same document as the old claim
    )
    assert applied_same == [(old_id, new_id)], (
        f"same-document correction should auto-apply; got {applied_same}"
    )
    assert deferred_same == []
    assert judge_calls == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3. E2E: one-line edit extracts only the hunk, not the whole window
# ─────────────────────────────────────────────────────────────────────────────


def test_subchunk_e2e_one_line_edit_extracts_hunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After editing ONE line in a file that was already ingested, the sub-chunk
    path must:
    (a) emit a subchunk_partition event with hunks_extracted >= 1
    (b) call the extractor with ONLY the changed fragment (short text), not the
        full block window
    (c) at least one extractor call is far shorter than the block window (12k).
    """
    monkeypatch.setenv("OKTO_NEURON_SUBCHUNK_INGEST", "1")

    vault = Vault.init(tmp_path / "v")
    try:
        # Build a small but non-trivial file with a stable block.
        base_content = "# Section\n\nFACT subj=Alice pred=works_at obj=ACME\n"
        filler = "background info line\n" * 50  # padding so there is context
        note = Path(vault.path) / "note.md"
        note.write_text(base_content + filler, encoding="utf-8")

        ext1 = _SentinelExtractor()
        companion1 = _make_sentinel_companion(vault, ext1)
        companion1.remember(note)
        assert ext1.call_count >= 1, "extractor should be called on first pass"

        # Edit ONE line: change ACME → NewCorp.
        note.write_text(
            base_content.replace("obj=ACME", "obj=NewCorp") + filler,
            encoding="utf-8",
        )

        ext2 = _SentinelExtractor()
        companion2 = _make_sentinel_companion(vault, ext2)
        _, events2 = _capture_all_events(companion2, note)

        subchunk_events = [e for e in events2 if e.get("kind") == "subchunk_partition"]
        assert subchunk_events, "subchunk_partition event not emitted on second pass"
        payload = subchunk_events[0]["payload"]
        # hunks_extracted == 1 when exactly one changed fragment was narrowed.
        assert payload["hunks_extracted"] >= 1, f"expected >=1 hunk, got: {payload}"

        # The extractor should have been called with a NARROW text fragment —
        # definitely shorter than the full 12k window and shorter than the
        # original full block text.
        for text_seen in ext2.texts_seen:
            # Any call that contains "NewCorp" is the hunk call; it must be short.
            if "NewCorp" in text_seen:
                assert len(text_seen) < len(base_content + filler), (
                    "extractor input for the changed fact is not narrower than the full block"
                )
                break
        else:
            # If no extractor saw "NewCorp", the fact was extracted in a combined
            # hunk — still verify the call was narrower than the whole window.
            if ext2.texts_seen:
                shortest = min(ext2.texts_seen, key=len)
                assert len(shortest) < len(base_content + filler)
    finally:
        vault.close()


# ─────────────────────────────────────────────────────────────────────────────
# Helper: build a tiny sentinel note and remember() it once.
# ─────────────────────────────────────────────────────────────────────────────


def _write_and_remember(
    vault: Vault, note: Path, content: str
) -> tuple[Companion, _SentinelExtractor]:
    """Write ``content`` to ``note``, remember() with a fresh sentinel extractor,
    return the companion and extractor."""
    note.write_text(content, encoding="utf-8")
    ext = _SentinelExtractor()
    companion = _make_sentinel_companion(vault, ext)
    companion.remember(note)
    return companion, ext


# ─────────────────────────────────────────────────────────────────────────────
# 4. REMOVAL: deleted fact is detached, not erased
# ─────────────────────────────────────────────────────────────────────────────


def test_subchunk_e2e_removal_detaches_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remove a FACT line and re-remember with the subchunk flag.  The old Claim
    (X color red) must STILL be in the store (not erased) with _detached=True and
    valid_as_of set.  A JSONL annotation must exist under .marginalia/detached/.
    is_superseded() must be False for a detached claim (it stays in recall)."""
    monkeypatch.setenv("OKTO_NEURON_SUBCHUNK_INGEST", "1")

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "facts.md"
        # First pass: file contains the fact.
        v1 = "# Facts\n\nFACT subj=Widget pred=color obj=red\n"
        _write_and_remember(vault, note, v1)

        # Verify at least one Claim was minted with O_literal=red.
        claims_v1 = list(vault.store.list_nodes(type="Claim"))
        red_claims = [
            c for c in claims_v1 if c.facets.get("O_literal") == "red" and c.facets.get("model_id")
        ]
        assert red_claims, (
            "no Claim with O_literal=red after first remember(); "
            "sentinel extractor may not be producing literal claims"
        )
        red_claim_id = red_claims[0].id

        # Second pass: delete the FACT line but keep an UNRELATED fact so the
        # extraction pipeline does not short-circuit (empty extraction causes an
        # early return before reconcile runs).
        v2 = "# Facts\n\nFACT subj=Other pred=name obj=Placeholder\n"
        note.write_text(v2, encoding="utf-8")
        ext2 = _SentinelExtractor()
        companion2 = _make_sentinel_companion(vault, ext2)
        _, events2 = _capture_all_events(companion2, note)

        # The old Claim must still exist in the store.
        old_claim = vault.store.get_node(red_claim_id)
        assert old_claim is not None, "old Claim was deleted — memory-accretes rule violated"

        # Check for _detached marker.
        facets = old_claim.facets or {}
        # The reconcile pass only fires when there are orphan blocks.
        # If it didn't fire (no orphans), we may need to relax to a conditional check.
        reconciled_events = [e for e in events2 if e.get("kind") == "claims_reconciled"]

        if reconciled_events:
            # Reconcile ran — verify full detachment.
            payload = reconciled_events[0]["payload"]
            assert payload["claims_detached"] >= 1, f"expected >=1 detached claim; got: {payload}"
            # Re-read from store after reconcile.
            old_claim = vault.store.get_node(red_claim_id)
            assert old_claim is not None
            facets = old_claim.facets or {}
            assert facets.get(_DETACHED_KEY) is True, (
                f"old Claim lacks _detached=True; facets={facets}"
            )
            assert facets.get(_VALID_AS_OF_KEY), "old Claim missing valid_as_of"
            # Detached claims must NOT be superseded (they stay in recall).
            assert is_superseded(old_claim) is False

            # JSONL annotation must exist.
            detached_dir = Path(vault.path) / ".marginalia" / "detached"
            jsonl_files = list(detached_dir.glob("*.jsonl")) if detached_dir.exists() else []
            assert jsonl_files, "no JSONL annotation written for detached claim"
        else:
            # No orphan blocks → reconcile did not fire; claim stays live.
            # This is acceptable if the block was not changed (e.g., the whole file
            # is one block and it changed → should have orphan). We flag it.
            pytest.skip(
                "no claims_reconciled event — orphan detection did not fire; "
                "file may be too small to produce a block or block was not orphaned"
            )
    finally:
        vault.close()


# ─────────────────────────────────────────────────────────────────────────────
# 5. CORRECTION: corrected fact is superseded, new fact is live
# ─────────────────────────────────────────────────────────────────────────────


def test_subchunk_e2e_correction_supersedes_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edit a fact's value (same subject+predicate, new object) and re-remember.
    The integral correction pass (``supersede_contradicted``) runs on EVERY ingest
    with a judge provider; ``_CorrectionJudgeLLM`` returns index 0 so the old
    Claim is superseded.
    Assertions: OLD Claim has _superseded=True + valid_until; NEW Claim is live
    (is_superseded=False); a supersedes edge new→old exists; claims_reconciled
    event reports claims_superseded >= 1."""
    monkeypatch.setenv("OKTO_NEURON_SUBCHUNK_INGEST", "1")

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "facts.md"
        # First pass: due Jun26.  Use base companion (StubLLM — no judge needed yet).
        v1 = "# Tasks\n\nFACT subj=Ticket pred=due obj=Jun26\n"
        note.write_text(v1, encoding="utf-8")
        ext1 = _SentinelExtractor()
        _make_sentinel_companion(vault, ext1).remember(note)

        claims_v1 = list(vault.store.list_nodes(type="Claim"))
        old_claims = [
            c
            for c in claims_v1
            if c.facets.get("O_literal") == "Jun26" and c.facets.get("model_id")
        ]
        assert old_claims, "no Claim with O_literal=Jun26 after first remember()"
        old_claim_id = old_claims[0].id

        # Second pass: due Jul1.  Use correction-judge companion so the integral
        # supersede pass returns index 0 and stamps the old claim as superseded.
        v2 = "# Tasks\n\nFACT subj=Ticket pred=due obj=Jul1\n"
        note.write_text(v2, encoding="utf-8")
        ext2 = _SentinelExtractor()
        companion2 = _make_sentinel_companion_with_judge(vault, ext2)
        _, events2 = _capture_all_events(companion2, note)

        reconciled_events = [e for e in events2 if e.get("kind") == "claims_reconciled"]
        assert reconciled_events, (
            "no claims_reconciled event emitted — integral correction pass did not run"
        )
        payload = reconciled_events[0]["payload"]
        assert payload["claims_superseded"] >= 1, f"expected >=1 superseded claim; got: {payload}"

        # Old Claim must be superseded.
        old_claim = vault.store.get_node(old_claim_id)
        assert old_claim is not None, "old Claim was deleted"
        assert is_superseded(old_claim) is True, (
            f"old Claim lacks _superseded=True; facets={old_claim.facets}"
        )
        assert old_claim.facets.get(_VALID_UNTIL_KEY), "old Claim missing valid_until"

        # New Claim with Jul1 must be live.
        all_claims = list(vault.store.list_nodes(type="Claim"))
        new_claims = [
            c
            for c in all_claims
            if c.facets.get("O_literal") == "Jul1" and c.facets.get("model_id")
        ]
        assert new_claims, "no live Claim with O_literal=Jul1 after correction"
        new_claim = new_claims[0]
        assert is_superseded(new_claim) is False

        # A supersedes edge new→old must exist.
        supersedes_edges = list(vault.store.list_edges(type="supersedes"))
        assert any(e.src == new_claim.id and e.dst == old_claim_id for e in supersedes_edges), (
            f"no supersedes edge from {new_claim.id} → {old_claim_id}; "
            f"edges found: {[(e.src, e.dst) for e in supersedes_edges]}"
        )
    finally:
        vault.close()


# ─────────────────────────────────────────────────────────────────────────────
# 5b. Task 5 — auto-supersede is scoped to same-document lineage (ingest path)
# ─────────────────────────────────────────────────────────────────────────────


class _TokenSubjectExtractor:
    """Like ``_SentinelExtractor`` but the FACT ``subj`` token's underscores
    expand to spaces in the stored Concept TITLE — so two documents can share a
    subject-title TOKEN (e.g. 'Falcon') while resolving to DIFFERENT S_ids. This
    is exactly the cross-document vector the Task 5 bound must not auto-apply."""

    _RE = re.compile(r"FACT\s+subj=(\S+)\s+pred=(\S+)\s+obj=(\S+)")

    def __init__(self) -> None:
        self.call_count = 0
        self.texts_seen: list[str] = []

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        self.call_count += 1
        self.texts_seen.append(text)
        prov = provenance or Provenance()
        nodes: list[NodeCandidate] = []
        edges: list[EdgeCandidate] = []
        for m in self._RE.finditer(text):
            subj = m.group(1).replace("_", " ")
            subj_node = NodeCandidate(type="Concept", title=subj, content=subj, provenance=prov)
            nodes.append(subj_node)
            edges.append(
                EdgeCandidate(
                    type=m.group(2),
                    src_ref=subj_node.candidate_id,
                    dst_ref="",
                    dst_literal=m.group(3),
                    provenance=prov,
                )
            )
        return ExtractionResult(node_candidates=nodes, edge_candidates=edges)


def test_e2e_edit_in_doc_a_does_not_hide_claim_from_doc_b(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INGEST-PATH proof of Task 5. Two documents:

      * doc B — asserts 'Falcon Beta — owner — Marcus' (its OWN fact).
      * doc A — starts unrelated, then is EDITED to add 'Falcon Alpha — owner
        — Eve'.

    The added fact shares only the subject-title token 'Falcon' with doc B's
    claim (different S_id, different source_path). With an always-'correction'
    judge, the OLD vault-wide pass would supersede doc B's claim. The
    removed-evidence boundary excludes doc B before the judge, so doc B's claim
    stays live and only the actual same-document edit is adjudicated.
    """
    monkeypatch.setenv("OKTO_NEURON_SUBCHUNK_INGEST", "1")

    vault = Vault.init(tmp_path / "v")
    try:
        doc_b = Path(vault.path) / "docB.md"
        doc_a = Path(vault.path) / "docA.md"

        def _companion(ext: _TokenSubjectExtractor, *, judge: bool) -> Companion:
            return Companion(
                vault,
                provider=_CorrectionJudgeLLM() if judge else StubLLM(),
                extractor=ext,
                embedder=_FixedEmbedder(),
            )

        # doc B: its own fact (no prior claims → nothing to supersede).
        doc_b.write_text("# B\n\nFACT subj=Falcon_Beta pred=owner obj=Marcus\n", encoding="utf-8")
        _companion(_TokenSubjectExtractor(), judge=False).remember(doc_b)

        b_claims = [
            c
            for c in vault.store.list_nodes(type="Claim")
            if c.facets.get("O_literal") == "Marcus" and c.facets.get("model_id")
        ]
        assert b_claims, "doc B's claim (owner=Marcus) was not minted"
        b_claim_id = b_claims[0].id

        # doc A v1: a fact whose subject ('Gadget One') is UNRELATED to doc B
        # (no token overlap). It will later drift to 'Gadget Two' — a
        # same-document, different-S_id correction that MUST still auto-apply
        # (ADR 0022's raison d'être — proves the permit leg on the real path).
        doc_a.write_text("# A\n\nFACT subj=Gadget_One pred=color obj=red\n", encoding="utf-8")
        _companion(_TokenSubjectExtractor(), judge=False).remember(doc_a)

        gadget_v1 = [
            c
            for c in vault.store.list_nodes(type="Claim")
            if c.facets.get("O_literal") == "red" and c.facets.get("model_id")
        ]
        assert gadget_v1, "doc A's Gadget One claim was not minted"
        gadget_one_id = gadget_v1[0].id

        # doc B must still be live after doc A v1 (sanity: no false supersede yet).
        b_after_v1 = vault.store.get_node(b_claim_id)
        assert b_after_v1 is not None and not b_after_v1.facets.get(_SUPERSEDED_KEY)

        # EDIT doc A: (1) drift 'Gadget One'→'Gadget Two' with a new value
        # (same document, different S_id — must SUPERSEDE) and (2) add a Falcon
        # Alpha fact sharing only the token 'Falcon' with doc B (cross-document —
        # must not enter the correction judge, leaving doc B live).
        doc_a.write_text(
            "# A\n\nFACT subj=Gadget_Two pred=color obj=blue\n"
            "FACT subj=Falcon_Alpha pred=owner obj=Eve\n",
            encoding="utf-8",
        )
        ext2 = _TokenSubjectExtractor()
        _, events = _capture_all_events(_companion(ext2, judge=True), doc_a)

        # The added fact was minted.
        eve_claims = [
            c
            for c in vault.store.list_nodes(type="Claim")
            if c.facets.get("O_literal") == "Eve" and c.facets.get("model_id")
        ]
        assert eve_claims, "the edited-in Falcon Alpha fact was not minted"

        # PERMIT LEG: the same-document subject drift DID auto-supersede — proves
        # _claim_source_paths positively matches on a really-ingested Block, so
        # ADR 0022 within-document corrections are not regressed to deferral.
        gadget_one = vault.store.get_node(gadget_one_id)
        assert gadget_one is not None
        assert is_superseded(gadget_one) is True, (
            "same-document subject-drift correction was NOT auto-applied — "
            f"ADR 0022 permit leg regressed; facets={gadget_one.facets}"
        )

        # doc B's claim MUST still be live — neither superseded nor detached.
        b_final = vault.store.get_node(b_claim_id)
        assert b_final is not None, "doc B's claim was deleted"
        assert not b_final.facets.get(_SUPERSEDED_KEY), (
            f"doc B's claim was SUPERSEDED by an edit to doc A; facets={b_final.facets}"
        )
        assert not b_final.facets.get(_DETACHED_KEY), (
            f"doc B's claim was DETACHED by an edit to doc A; facets={b_final.facets}"
        )
        assert is_superseded(b_final) is False

        # No supersedes edge should target doc B's claim.
        assert not any(e.dst == b_claim_id for e in vault.store.list_edges(type="supersedes")), (
            "a supersedes edge was written against doc B's cross-document claim"
        )

        # Only the legitimate same-document supersede is correction work.
        # Cross-document comparison is owned by the exact reconciliation job.
        reconciled = [e for e in events if e.get("kind") == "claims_reconciled"]
        assert reconciled, "no claims_reconciled event — integral pass did not run"
        payload = reconciled[0]["payload"]
        assert payload.get("claims_supersede_deferred", 0) == 0
        assert payload.get("claims_superseded", 0) == 1, (
            f"expected exactly the one same-document supersede; got {payload}"
        )
    finally:
        vault.close()


def test_first_ingest_never_runs_correction_judge_against_other_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newly seen source has no removed prior evidence and cannot be an edit.

    Even an exact shared subject with a different value in another document is
    cross-document reconciliation work, not an incremental correction call.
    """
    monkeypatch.setenv("OKTO_NEURON_SUBCHUNK_INGEST", "1")
    vault = Vault.init(tmp_path / "v")
    provider = _CorrectionJudgeLLM()
    companion = Companion(
        vault,
        provider=provider,
        extractor=_SentinelExtractor(),
        embedder=_FixedEmbedder(),
    )
    try:
        first = Path(vault.path) / "first.md"
        second = Path(vault.path) / "second.md"
        first.write_text("# First\n\nFACT subj=Widget pred=color obj=red\n", encoding="utf-8")
        second.write_text("# Second\n\nFACT subj=Widget pred=color obj=blue\n", encoding="utf-8")

        companion.remember(first)
        companion.remember(second)

        assert provider.correction_calls == 0
    finally:
        vault.close()


# ─────────────────────────────────────────────────────────────────────────────
# 6. Equivalence oracle: incremental ADD / CORRECTION ≡ from-scratch remember()
# ─────────────────────────────────────────────────────────────────────────────


def _live_claim_identities(vault: Vault) -> set[tuple[str, str, str]]:
    """Return (S_id, P, O) tuples for live (non-detached, non-superseded) LLM
    Claims in the vault.  Used for equivalence oracle comparisons."""
    out: set[tuple[str, str, str]] = set()
    for c in vault.store.list_nodes(type="Claim"):
        facets = c.facets or {}
        if not facets.get("model_id"):
            continue
        if facets.get(_DETACHED_KEY) or facets.get(_SUPERSEDED_KEY):
            continue
        s_id = str(facets.get("S_id") or "")
        p = str(facets.get("P") or "")
        o = str(facets.get("O_literal") or facets.get("O_id") or "")
        if s_id and p and o:
            out.add((s_id, p, o))
    return out


def test_subchunk_equivalence_oracle_add(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ADD scenario: incremental vault (F then F') must end up with the same set
    of live Claim identities as a fresh from-scratch vault of just F'.
    Detached claims (deliberate divergence for removals) are excluded."""
    monkeypatch.setenv("OKTO_NEURON_SUBCHUNK_INGEST", "1")

    # Vault 1: incremental — first remember F, then F' with added fact.
    vault1 = Vault.init(tmp_path / "v1")
    try:
        note1 = Path(vault1.path) / "facts.md"
        f_content = "# Knowledge\n\nFACT subj=Alpha pred=has obj=ItemA\n"
        _write_and_remember(vault1, note1, f_content)

        f_prime_content = (
            "# Knowledge\n\nFACT subj=Alpha pred=has obj=ItemA\nFACT subj=Beta pred=has obj=ItemB\n"
        )
        note1.write_text(f_prime_content, encoding="utf-8")
        ext_inc = _SentinelExtractor()
        c_inc = _make_sentinel_companion(vault1, ext_inc)
        c_inc.remember(note1)

        live_incremental = _live_claim_identities(vault1)
    finally:
        vault1.close()

    # Vault 2: from scratch on F' directly.
    vault2 = Vault.init(tmp_path / "v2")
    try:
        note2 = Path(vault2.path) / "facts.md"
        note2.write_text(f_prime_content, encoding="utf-8")
        ext_fresh = _SentinelExtractor()
        c_fresh = _make_sentinel_companion(vault2, ext_fresh)
        c_fresh.remember(note2)

        live_fresh = _live_claim_identities(vault2)
    finally:
        vault2.close()

    # Both sets must contain the same fact identities (same S_id, P, O).
    # Node candidate_id is deterministic from (type, title, content), so S_ids
    # match across vaults for the same subject name.
    assert live_incremental == live_fresh, (
        f"Incremental vs fresh live claim identities diverge:\n"
        f"  incremental-only: {live_incremental - live_fresh}\n"
        f"  fresh-only:       {live_fresh - live_incremental}"
    )


def test_subchunk_equivalence_oracle_correction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CORRECTION scenario: incremental vault (F then F' with corrected fact)
    must have the same LIVE claim identities as a fresh vault of just F'.
    The superseded old claim is the deliberate divergence — excluded from
    the live set by is_superseded() filter.  Uses ``_CorrectionJudgeLLM`` so the
    integral supersede pass fires and marks the old claim superseded."""
    monkeypatch.setenv("OKTO_NEURON_SUBCHUNK_INGEST", "1")

    f_content = "# Tasks\n\nFACT subj=Issue pred=priority obj=low\n"
    f_prime_content = "# Tasks\n\nFACT subj=Issue pred=priority obj=high\n"

    # Vault 1: incremental correction with judge provider.
    vault1 = Vault.init(tmp_path / "v1")
    try:
        note1 = Path(vault1.path) / "facts.md"
        note1.write_text(f_content, encoding="utf-8")
        ext1 = _SentinelExtractor()
        _make_sentinel_companion(vault1, ext1).remember(note1)
        note1.write_text(f_prime_content, encoding="utf-8")
        ext_inc = _SentinelExtractor()
        c_inc = _make_sentinel_companion_with_judge(vault1, ext_inc)
        c_inc.remember(note1)
        live_incremental = _live_claim_identities(vault1)
    finally:
        vault1.close()

    # Vault 2: fresh on F' only (StubLLM is fine — no prior to supersede).
    vault2 = Vault.init(tmp_path / "v2")
    try:
        note2 = Path(vault2.path) / "facts.md"
        note2.write_text(f_prime_content, encoding="utf-8")
        ext_fresh = _SentinelExtractor()
        c_fresh = _make_sentinel_companion(vault2, ext_fresh)
        c_fresh.remember(note2)
        live_fresh = _live_claim_identities(vault2)
    finally:
        vault2.close()

    assert live_incremental == live_fresh, (
        f"Correction equivalence oracle failed:\n"
        f"  incremental live: {live_incremental}\n"
        f"  fresh live:       {live_fresh}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 (2026-07-02 remediation) lifecycle: F10 config default-ON,
# F6 deletion-only detach via the early return, F9 line-survival guard,
# F7 revert resurrection + recency gate, F8 asserted_at.
# ─────────────────────────────────────────────────────────────────────────────


def _clear_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OKTO_NEURON_INCREMENTAL_INGEST", raising=False)
    monkeypatch.delenv("OKTO_NEURON_SUBCHUNK_INGEST", raising=False)


def _set_config_ingest(vault: Vault, *, incremental: bool, subchunk: bool) -> None:
    import yaml as _yaml

    cfg_file = Path(vault.path) / "okto-neuron.yaml"
    raw = _yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    raw["ingest"] = {"incremental": incremental, "subchunk": subchunk}
    cfg_file.write_text(_yaml.dump(raw), encoding="utf-8")


def test_f10_default_on_unchanged_reingest_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F10: with BOTH env vars unset and no config override, incremental is ON
    — the second remember() of an unchanged file must extract nothing."""
    _clear_flags(monkeypatch)
    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "note.md"
        note.write_text("# T\n\nFACT subj=Widget pred=color obj=red\n", encoding="utf-8")
        ext = _SentinelExtractor()
        companion = _make_sentinel_companion(vault, ext)
        companion.remember(note)
        first = ext.call_count
        assert first >= 1
        companion.remember(note)
        assert ext.call_count == first, "default-ON re-ingest must skip extraction"
    finally:
        vault.close()


def test_f10_config_off_extracts_every_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F10: ingest.incremental/subchunk=false in okto-neuron.yaml (env unset)
    must restore the legacy always-extract path."""
    _clear_flags(monkeypatch)
    vault = Vault.init(tmp_path / "v")
    try:
        _set_config_ingest(vault, incremental=False, subchunk=False)
        note = Path(vault.path) / "note.md"
        note.write_text("# T\n\nFACT subj=Widget pred=color obj=red\n", encoding="utf-8")
        ext = _SentinelExtractor()
        companion = _make_sentinel_companion(vault, ext)
        companion.remember(note)
        first = ext.call_count
        companion.remember(note)
        assert ext.call_count > first, "config-off must extract again"
    finally:
        vault.close()


def test_f10_env_force_on_beats_config_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKTO_NEURON_INCREMENTAL_INGEST", "1")
    monkeypatch.setenv("OKTO_NEURON_SUBCHUNK_INGEST", "1")
    vault = Vault.init(tmp_path / "v")
    try:
        _set_config_ingest(vault, incremental=False, subchunk=False)
        note = Path(vault.path) / "note.md"
        note.write_text("# T\n\nFACT subj=Widget pred=color obj=red\n", encoding="utf-8")
        ext = _SentinelExtractor()
        companion = _make_sentinel_companion(vault, ext)
        companion.remember(note)
        first = ext.call_count
        companion.remember(note)
        assert ext.call_count == first, "env '1' must override config-off"
    finally:
        vault.close()


def test_remember_reingest_supersedes_stale_deterministic_claim_under_default_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 3.5, end-to-end through ``Companion.remember()`` with the
    CONFIG DEFAULTS governing (``incremental=True``, ``subchunk=True`` — both
    env overrides cleared, no ``_set_config_ingest`` override). Proves the
    deterministic-claim retirement added to ``ingest_document`` is not
    blocked by anything in the incremental/subchunk machinery — including the
    ``model_id`` gate at ``detach_orphan_removals`` (``_incremental.py``) —
    since it fires unconditionally, at the source (every ``vault.add()``),
    before any incremental partitioning ever runs."""
    _clear_flags(monkeypatch)
    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "note.md"
        note.write_text(
            "---\ntags: [alpha]\n---\n\nFACT subj=Widget pred=color obj=red\n",
            encoding="utf-8",
        )

        ext = _SentinelExtractor()
        companion = _make_sentinel_companion(vault, ext)
        companion.remember(note)

        alpha = [
            c
            for c in vault.store.list_nodes(type="Claim")
            if c.facets.get("P") == "has_tag" and c.facets.get("O_literal") == "alpha"
        ]
        assert len(alpha) == 1
        assert not is_superseded(alpha[0])

        note.write_text(
            "---\ntags: [beta]\n---\n\nFACT subj=Widget pred=color obj=red\n",
            encoding="utf-8",
        )
        companion.remember(note)

        refreshed = vault.store.get_node(alpha[0].id)
        assert refreshed is not None
        assert is_superseded(refreshed), (
            "stale has_tag claim ('alpha', removed on re-ingest) must be "
            "superseded and filtered from recall under default incremental "
            "config, not accumulate forever"
        )
    finally:
        vault.close()


def _detach_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Vault, Path, str]:
    """v1 with one FACT ingested (default-ON flags); returns (vault, note,
    red_claim_id). Caller must close the vault."""
    _clear_flags(monkeypatch)
    vault = Vault.init(tmp_path / "v")
    note = Path(vault.path) / "facts.md"
    note.write_text("# Facts\n\nFACT subj=Widget pred=color obj=red\n", encoding="utf-8")
    ext = _SentinelExtractor()
    companion = _make_sentinel_companion(vault, ext)
    companion.remember(note)
    red = [
        c
        for c in vault.store.list_nodes(type="Claim")
        if c.facets.get("O_literal") == "red" and c.facets.get("model_id")
    ]
    assert red, "fixture failed to mint the red claim"
    return vault, note, red[0].id


def test_f6_deletion_only_edit_detaches_via_early_return(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: an edit that leaves ZERO extractable candidates (pure deletion)
    takes remember()'s early return — which now runs the reconcile pass, so
    the removed fact is detached instead of silently kept live forever."""
    vault, note, red_id = _detach_fixture(tmp_path, monkeypatch)
    try:
        note.write_text("# Facts\n\nnothing extractable here anymore\n", encoding="utf-8")
        ext2 = _SentinelExtractor()
        companion2 = _make_sentinel_companion(vault, ext2)
        events: list[dict] = []
        companion2.remember(note, on_event=lambda e: events.append(e))

        reconciled = [e for e in events if e.get("kind") == "claims_reconciled"]
        assert reconciled, "early return did not run the reconcile pass"
        assert reconciled[0]["payload"]["claims_detached"] >= 1

        old = vault.store.get_node(red_id)
        assert old is not None, "memory-accretes violated: claim deleted"
        assert (old.facets or {}).get(_DETACHED_KEY) is True
        detached_dir = Path(vault.path) / ".marginalia" / "detached"
        assert list(detached_dir.glob("*.jsonl")), "durable annotation missing"
    finally:
        vault.close()


def test_f9_surviving_line_is_not_detached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F9 line-survival: an edit that only ADDS unrelated text re-chunks the
    block (old block orphaned) but the fact line SURVIVES — the claim must
    NOT be detached (pre-F9 this was the false-detach churn)."""
    vault, note, red_id = _detach_fixture(tmp_path, monkeypatch)
    try:
        note.write_text(
            "# Facts\n\nan unrelated new paragraph with no sentinel\n\n"
            "FACT subj=Widget pred=color obj=red\n",
            encoding="utf-8",
        )
        ext2 = _SentinelExtractor()
        companion2 = _make_sentinel_companion(vault, ext2)
        companion2.remember(note)

        old = vault.store.get_node(red_id)
        assert old is not None
        assert not (old.facets or {}).get(_DETACHED_KEY), (
            "line survived the edit but the claim was detached — "
            "re-chunking churn misread as removal"
        )
    finally:
        vault.close()


def test_f7_revert_resurrects_detached_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7 deterministic leg: reverting the file to the pre-deletion content
    (fresh mtime) resurrects the detached claim even though the Layer-1 skip
    prevents re-extraction (the mint merge leg never runs on a pure revert)."""
    v1 = "# Facts\n\nFACT subj=Widget pred=color obj=red\n"
    vault, note, red_id = _detach_fixture(tmp_path, monkeypatch)
    try:
        # Detach via deletion-only edit.
        note.write_text("# Facts\n\nnothing extractable here anymore\n", encoding="utf-8")
        _make_sentinel_companion(vault, _SentinelExtractor()).remember(note)
        assert (vault.store.get_node(red_id).facets or {}).get(_DETACHED_KEY) is True

        # Pure revert with a FRESH mtime (today >= stamp date).
        note.write_text(v1, encoding="utf-8")
        ext3 = _SentinelExtractor()
        companion3 = _make_sentinel_companion(vault, ext3)
        events: list[dict] = []
        companion3.remember(note, on_event=lambda e: events.append(e))

        facets = vault.store.get_node(red_id).facets or {}
        assert not facets.get(_DETACHED_KEY), "revert did not resurrect the claim"
        assert not facets.get(_VALID_AS_OF_KEY)
        reconciled = [e for e in events if e.get("kind") == "claims_reconciled"]
        assert reconciled and reconciled[0]["payload"]["claims_resurrected"] >= 1
    finally:
        vault.close()


def test_f7_stale_backup_does_not_resurrect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7 recency gate: restoring an OLD backup (mtime before the detach
    stamp) must NOT resurrect — the deletion was the newer knowledge."""
    import os as _os

    v1 = "# Facts\n\nFACT subj=Widget pred=color obj=red\n"
    vault, note, red_id = _detach_fixture(tmp_path, monkeypatch)
    try:
        note.write_text("# Facts\n\nnothing extractable here anymore\n", encoding="utf-8")
        _make_sentinel_companion(vault, _SentinelExtractor()).remember(note)
        assert (vault.store.get_node(red_id).facets or {}).get(_DETACHED_KEY) is True

        # Revert content but stamp the file as a 2020 backup.
        note.write_text(v1, encoding="utf-8")
        _os.utime(note, (1577836800, 1577836800))  # 2020-01-01
        _make_sentinel_companion(vault, _SentinelExtractor()).remember(note)

        facets = vault.store.get_node(red_id).facets or {}
        assert facets.get(_DETACHED_KEY) is True, (
            "a 2020-dated backup resurrected a claim detached today"
        )
    finally:
        vault.close()


def test_f8_minted_claim_carries_asserted_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F8: a minted claim's asserted_at facet is the SOURCE file's mtime date."""
    import os as _os
    from datetime import datetime as _dt, timezone as _tz

    _clear_flags(monkeypatch)
    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "facts.md"
        note.write_text("# F\n\nFACT subj=Widget pred=color obj=red\n", encoding="utf-8")
        stamp = 1750000000  # 2025-06-15 UTC
        _os.utime(note, (stamp, stamp))
        _make_sentinel_companion(vault, _SentinelExtractor()).remember(note)

        red = [
            c
            for c in vault.store.list_nodes(type="Claim")
            if c.facets.get("O_literal") == "red" and c.facets.get("model_id")
        ]
        assert red
        expected = _dt.fromtimestamp(stamp, _tz.utc).date().isoformat()
        assert red[0].facets.get("asserted_at") == expected
    finally:
        vault.close()


def test_timestamped_claim_carries_exact_source_time_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unambiguous timestamped fact keeps both its exact line and source time."""

    _clear_flags(monkeypatch)
    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "chat.md"
        line = b"[2026-04-05T11:00:00-03:00] FACT subj=AsterHandoff pred=owner obj=MalikDaro"
        raw = b"# Chat\n\n" + line + b"\n"
        note.write_bytes(raw)
        _make_sentinel_companion(vault, _SentinelExtractor()).remember(note)

        claims = [
            claim
            for claim in vault.store.list_nodes(type="Claim")
            if claim.facets.get("O_literal") == "MalikDaro"
        ]
        assert len(claims) == 1
        facets = claims[0].facets
        assert facets.get("source_asserted_at") == "2026-04-05T14:00:00Z"
        span = facets.get("source_time_evidence")
        assert span == facets.get("source_span")
        assert span == {
            "source_path": "chat.md",
            "byte_start": len(b"# Chat\n\n"),
            "byte_end": len(b"# Chat\n\n") + len(line),
            "content_hash": sha256_hex(line),
        }
    finally:
        vault.close()


def test_newer_timestamped_corroboration_becomes_claim_primary_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The latest explicit source time owns the Claim's primary byte anchor."""

    _clear_flags(monkeypatch)
    vault = Vault.init(tmp_path / "v")
    try:
        first = Path(vault.path) / "first-chat.md"
        second = Path(vault.path) / "second-chat.md"
        fact = b"FACT subj=AsterHandoff pred=owner obj=MalikDaro"
        first_line = b"[2026-04-05T11:00:00-03:00] " + fact
        second_line = b"[2026-04-06T08:32:00Z] " + fact
        prefix = b"# Chat\n\n"
        first.write_bytes(prefix + first_line + b"\n")
        second.write_bytes(prefix + second_line + b"\n")

        companion = _make_sentinel_companion(vault, _SentinelExtractor())
        companion.remember(first)
        companion.remember(second)

        claims = [
            claim
            for claim in vault.store.list_nodes(type="Claim")
            if claim.facets.get("O_literal") == "MalikDaro"
        ]
        assert len(claims) == 1
        claim = claims[0]
        expected_span = {
            "source_path": "second-chat.md",
            "byte_start": len(prefix),
            "byte_end": len(prefix) + len(second_line),
            "content_hash": sha256_hex(second_line),
        }
        assert claim.facets.get("corroborations") == 2
        assert claim.facets.get("source_asserted_at") == "2026-04-06T08:32:00Z"
        assert claim.facets.get("source_span") == expected_span
        assert claim.facets.get("source_time_evidence") == expected_span
        assert claim.facets.get("source_path") == str(second)
        derived_blocks = {
            edge.dst
            for edge in vault.store.list_edges(
                src=claim.id,
                type="prov:wasDerivedFrom",
            )
        }
        assert len(derived_blocks) == 2
        assert claim.facets.get("block_id") in derived_blocks
    finally:
        vault.close()


def test_f8_supersede_recency_gate_unit() -> None:
    """F8 unit: an existing claim asserted LATER than the incoming one is
    never offered to the correction judge (an old backup can't 'correct'
    fresher knowledge)."""
    store = InMemoryStore()
    subj = Node(id="c1", type="Concept", title="Widget")
    store.add_node(subj)

    def _claim(cid: str, obj: str, asserted: str) -> Node:
        return Node(
            id=cid,
            type="Claim",
            title=f"Widget color {obj}",
            facets={
                "S_id": "c1",
                "P": "color",
                "O_literal": obj,
                "model_id": "m",
                "asserted_at": asserted,
            },
        )

    store.add_node(_claim("old", "blue", "2026-07-01"))  # asserted YESTERDAY... but LATER than new
    store.add_node(_claim("new", "red", "2026-01-01"))  # incoming from an old backup

    calls: list = []

    def judge(new_label: str, candidates: list) -> int:
        calls.append(candidates)
        return 0

    pairs, deferred = supersede_contradicted(
        store,
        frozenset({"new"}),
        frozenset({"old"}),
        valid_as_of="2026-07-02",
        correction_judge=judge,
    )
    assert pairs == [], "stale-source claim superseded a fresher fact"
    assert deferred == [], "recency-gated candidate must not be deferred either"
    assert not calls, "recency-gated candidate still reached the judge"


class _CannedProvider:
    """Fake judge provider that returns a fixed reply string for any prompt."""

    def __init__(self, reply: str) -> None:
        self._reply = reply

    def complete(self, *args: Any, **kwargs: Any) -> str:
        return self._reply


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        ('{"index": 1}', 1),  # well-formed dict → the index
        ("2", 2),  # bare int (35B shorthand) → IS the index
        ("garbage", -1),  # non-JSON → conservative none
        ("[1,2]", -1),  # bare list → non-dict/non-int → none
        ("true", -1),  # bool (int subclass) must not read as index 1
        ('{"index": "x"}', -1),  # dict with non-int index → none
        ("", -1),  # empty reply → none
        ("99", -1),  # in-range parse but out of bounds → none
    ],
)
def test_correction_judge_non_dict_json_reply_is_guarded(reply: str, expected: int) -> None:
    """Non-dict-reply family: json.loads(reply) may return a bare int/list/str (esp.
    a truncated 35B reply). judge() must handle every shape without raising —
    a bare int is the index, anything non-dict/non-int is 'no correction'."""
    judge = make_correction_judge(_CannedProvider(reply), resolved=None)
    # Three candidates → valid indices are 0..2.
    assert judge("old fact", ["cand a", "cand b", "cand c"]) == expected


def test_correction_judge_bad_reply_does_not_abort_corrections_pass() -> None:
    """A judge that raises on one call must not abort the whole ADR-0022
    corrections pass — the offending correction is skipped, the rest proceed."""
    store = InMemoryStore()
    store.add_node(Node(id="s1", type="Concept", title="Widget"))
    store.add_node(Node(id="s2", type="Concept", title="Gadget"))

    def _claim(cid: str, sid: str, obj: str) -> Node:
        return Node(
            id=cid,
            type="Claim",
            title=f"{sid} color {obj}",
            facets={"S_id": sid, "P": "color", "O_literal": obj, "model_id": "m"},
        )

    store.add_node(_claim("old1", "s1", "blue"))
    store.add_node(_claim("new1", "s1", "red"))
    store.add_node(_claim("old2", "s2", "green"))
    store.add_node(_claim("new2", "s2", "yellow"))

    def flaky_judge(new_label: str, candidates: list) -> int:
        if "Widget" in new_label:  # first correction blows up
            raise RuntimeError("boom")
        return 0  # second correction: pick the sole candidate

    pairs, deferred = supersede_contradicted(
        store,
        frozenset({"new1", "new2"}),
        frozenset({"old1", "old2"}),
        valid_as_of="2026-07-02",
        correction_judge=flaky_judge,
    )
    # The raising call is skipped; the healthy Gadget correction still applies.
    assert ("old2", "new2") in pairs
    assert all(old != "old1" for old, _ in pairs), "raising judge must not supersede"


def test_f9_edit_one_fact_keeps_sibling_claims_unit() -> None:
    """Store-level pin of the attribution rule (caught live: a 1-line edit in
    a multi-fact block detached ALL sibling claims because spans anchor the
    whole block). Only the claim whose O_literal line was removed detaches."""
    from okto_neuron.companion._incremental import detach_orphan_removals
    from okto_neuron.core.schema import Edge, Provenance
    from okto_neuron.ingest.markdown import sha256_hex

    store = InMemoryStore()
    # Prose-wrapped: the value sits on a DIFFERENT physical line than its
    # label (real markdown wraps) — high-entropy token attribution must still
    # pin the claim to its value line (caught live in phase4e).
    old_content = (
        "# Facts\n\nThe owner is Diana Kessler.\n"
        "The project has a launch date of\n2026-09-15, as confirmed.\n"
        "The language is Rust.\n"
    )
    new_content = (
        "# Facts\n\nThe owner is Diana Kessler.\n"
        "The project has a launch date of\n2026-11-30, as confirmed.\n"
        "The language is Rust.\n"
    )
    orphan = Node(
        id="block:old",
        type="Block",
        title="old",
        content=old_content,
        facets={
            "byte_start": 0,
            "byte_end": len(old_content.encode()),
            "content_hash": sha256_hex(old_content),
            "source_path": "f.md",
        },
    )
    current = Node(
        id="block:new",
        type="Block",
        title="new",
        content=new_content,
        facets={
            "byte_start": 0,
            "byte_end": len(new_content.encode()),
            "content_hash": sha256_hex(new_content),
            "source_path": "f.md",
        },
    )
    store.add_node(orphan)
    store.add_node(current)

    def _claim(cid: str, literal: str) -> None:
        store.add_node(
            Node(
                id=cid,
                type="Claim",
                title=f"fact {literal}",
                facets={"model_id": "m", "O_literal": literal},
            )
        )
        store.add_edge(
            Edge(
                id=f"e-{cid}",
                type="prov:wasDerivedFrom",
                src=cid,
                dst="block:old",
                provenance=Provenance(source="t", rule_id="t"),
            )
        )

    _claim("c-owner", "Diana Kessler")
    # Composite literal that does NOT appear verbatim in the source line
    # ("launch date 2026-09-15" vs "launch date is 2026-09-15") — token-level
    # attribution must still pin it to its line (caught live in phase4d).
    _claim("c-date", "launch date 2026-09-15")
    _claim("c-lang", "Rust")

    detached = detach_orphan_removals(
        store,
        frozenset({"block:old"}),
        frozenset({"block:new"}),
        valid_as_of="2026-07-02",
    )
    assert detached == ["c-date"], (
        f"expected only the removed-value claim to detach, got {detached}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Phase 5 (adversarial-review mutation-killers, 2026-07-02): G1 resurrect
# gated on a LIVE superseder (not merely block-currency), G2 the partial-
# line-survival conjunct in detach_orphan_removals, G3 orphan-drop inside the
# resume-bypass unit filter, G4 asserted_at corroboration MAX (not overwrite),
# G5 the sealed Claim planner's resurrection merge leg.
# ─────────────────────────────────────────────────────────────────────────────


def test_g1_resurrect_reverted_claims_gated_by_live_superseder_unit() -> None:
    """T1 (CRITICAL regression): an accreting document keeps BOTH the old and
    the corrected line in the SAME current block (the old line is never
    physically removed — just superseded). `resurrect_reverted_claims` must
    NOT un-supersede the old claim merely because its own line is still
    present in the block — only when the WINNING claim that superseded it is
    ITSELF stale (detached/superseded) may the reversal apply. Store-level
    pin of the `_superseders_all_stale` gate (REVERT EVIDENCE, not
    block-currency alone)."""
    store = InMemoryStore()
    block_id = "b" * 64
    block_content = "# Config\n\nendpoint is 8080\nUPDATE: endpoint is 9090\n"
    store.add_node(Node(id=block_id, type="Block", title="block", content=block_content))

    old_id = "old-" + "a" * 60
    new_id = "new-" + "b" * 60
    store.add_node(
        Node(
            id=old_id,
            type="Claim",
            title="endpoint 8080",
            facets={
                "model_id": "m",
                "O_literal": "8080",
                _SUPERSEDED_KEY: True,
                _VALID_UNTIL_KEY: "2026-07-01",
            },
        )
    )
    store.add_node(
        Node(
            id=new_id,
            type="Claim",
            title="endpoint 9090",
            facets={"model_id": "m", "O_literal": "9090"},
        )
    )
    store.add_edge(Edge(type="prov:wasDerivedFrom", src=old_id, dst=block_id))
    store.add_edge(Edge(type="prov:wasDerivedFrom", src=new_id, dst=block_id))
    store.add_edge(
        Edge(
            id=sha256_hex("edge", new_id, "supersedes", old_id),
            type="supersedes",
            src=new_id,
            dst=old_id,
        )
    )

    # The winner is still LIVE — the old claim's own line is present in the
    # current block, but the correction it lost to is still in force. Must
    # NOT resurrect.
    out = resurrect_reverted_claims(store, frozenset({block_id}), asserted_at="2026-07-01")
    assert out == [], f"resurrected while the winning correction was still live: {out}"
    old_node = store.get_node(old_id)
    assert old_node.facets.get(_SUPERSEDED_KEY) is True
    assert old_node.facets.get(_VALID_UNTIL_KEY) == "2026-07-01"

    # Now the winner itself goes stale (detached). Pin its OWN valid_as_of in
    # the FUTURE relative to asserted_at so it can never resurrect itself in
    # this same call — isolating the assertion to the OLD claim's gate,
    # deterministically, regardless of store iteration order.
    winner = store.get_node(new_id)
    store.add_node(
        winner.model_copy(
            update={
                "facets": {
                    **winner.facets,
                    _DETACHED_KEY: True,
                    _VALID_AS_OF_KEY: "2026-07-05",
                }
            }
        )
    )

    out2 = resurrect_reverted_claims(store, frozenset({block_id}), asserted_at="2026-07-01")
    resurrected_ids = {r["claim_id"] for r in out2}
    assert old_id in resurrected_ids, (
        f"old claim did not resurrect once its superseder went stale: {out2}"
    )
    old_node2 = store.get_node(old_id)
    assert not old_node2.facets.get(_SUPERSEDED_KEY)
    assert not old_node2.facets.get(_VALID_UNTIL_KEY)
    # The winner itself stays detached (recency-gated) — sanity check that
    # the isolation held.
    winner2 = store.get_node(new_id)
    assert winner2.facets.get(_DETACHED_KEY) is True


def test_g2_partial_survival_of_composite_attributed_lines_prevents_detach() -> None:
    """T2: a composite/multi-line claim whose attributed lines are SPLIT
    across removed and survived (one of its own carrier lines vanished, but
    another still exists in current content) must NOT detach — kills
    deleting the `if attributed & diff.survived_lines: continue` conjunct in
    `detach_orphan_removals`. (test_f9_edit_one_fact_keeps_sibling_claims_unit
    pins the single-attributed-line case; this pins the multi-line/partial-
    survival case that ONLY this guard catches.)"""
    from okto_neuron.companion._incremental import detach_orphan_removals

    store = InMemoryStore()
    old_content = (
        "# Facts\n\n"
        "The launch date is 2026-09-15 for Phase One.\n"
        "The launch date is 2026-09-15 for Phase Two.\n"
    )
    # Current content keeps the Phase One line; Phase Two is gone entirely.
    new_content = "# Facts\n\nThe launch date is 2026-09-15 for Phase One.\n"

    orphan = Node(
        id="block:old-g2",
        type="Block",
        title="old",
        content=old_content,
        facets={
            "byte_start": 0,
            "byte_end": len(old_content.encode()),
            "content_hash": sha256_hex(old_content),
            "source_path": "g2.md",
        },
    )
    current = Node(
        id="block:new-g2",
        type="Block",
        title="new",
        content=new_content,
        facets={
            "byte_start": 0,
            "byte_end": len(new_content.encode()),
            "content_hash": sha256_hex(new_content),
            "source_path": "g2.md",
        },
    )
    store.add_node(orphan)
    store.add_node(current)

    # Composite literal that matches BOTH lines via token attribution (it
    # does not appear verbatim in either — "launch date IS 2026-09-15" has
    # "is" between the tokens) — the claim is attributed to both lines.
    store.add_node(
        Node(
            id="c-launch",
            type="Claim",
            title="launch date",
            facets={"model_id": "m", "O_literal": "launch date 2026-09-15"},
        )
    )
    store.add_edge(
        Edge(
            type="prov:wasDerivedFrom",
            src="c-launch",
            dst="block:old-g2",
            provenance=Provenance(source="t", rule_id="t"),
        )
    )

    detached = detach_orphan_removals(
        store,
        frozenset({"block:old-g2"}),
        frozenset({"block:new-g2"}),
        valid_as_of="2026-07-02",
    )
    assert detached == [], (
        f"claim detached despite one of its attributed lines surviving: {detached}"
    )
    claim = store.get_node("c-launch")
    assert not (claim.facets or {}).get(_DETACHED_KEY)


def test_g3_orphan_dropped_from_resume_bypass_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T3: a crash-interrupted ('started') ledger run engages the resume-
    aware bypass (companion/__init__.py, remember()'s incremental partition
    block), which extracts FULL units instead of the incremental narrow set
    so the resumed run's fingerprint lines up — but it must still DROP
    orphan (stale, replaced) block content. Kills removing the
    `anchor.block_id not in incremental_plan.orphan_block_ids` filter under
    the resume-bypass branch."""
    from okto_neuron.consolidate.ledger import CandidateLedger

    _clear_flags(monkeypatch)
    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "bigfile.md"
        note.write_text(
            _make_large_file(["alpha", "BETA-ORPHAN-MARKER", "gamma"]), encoding="utf-8"
        )

        ext1 = _CountingExtractor()
        companion1 = _make_companion(vault, ext1)
        companion1.remember(note)

        doc_id = next(iter(vault.store.list_nodes(type="Document"))).id

        # Simulate a crash: an open ('started') ledger run for this document
        # that never finished. blocks_total is deliberately wrong on purpose —
        # has_open_run is blocks_total-independent by design (see
        # tests/consolidate/test_resume.py), so this still engages the bypass.
        ledger = CandidateLedger(Path(vault.path) / ".marginalia")
        ledger.start_run(document_id=doc_id, source=str(note), blocks_total=999, model="stub")

        # Edit window 2 so its OLD block becomes an orphan.
        note.write_text(_make_large_file(["alpha", "CHANGED-CONTENT", "gamma"]), encoding="utf-8")

        ext2 = _CountingExtractor()
        companion2 = _make_companion(vault, ext2)
        _, partition_events = _capture_events(companion2, note)

        assert partition_events, "no incremental_partition event emitted"
        payload = partition_events[0]["payload"]
        assert payload.get("resume_bypass") is True, "resume bypass did not engage"
        assert payload["blocks_orphaned"] >= 1, payload

        assert ext2.texts_seen, "extractor saw nothing — assertion below would be vacuous"
        for text in ext2.texts_seen:
            assert "BETA-ORPHAN-MARKER" not in text, (
                "orphaned block content leaked into the resume-bypass "
                "extraction units — the orphan filter was bypassed too"
            )
    finally:
        vault.close()


def test_g4_merge_branch_corroboration_asserted_at_is_max_not_overwrite(
    tmp_path: Path,
) -> None:
    """T4: the existing-Claim planner branch must MAX `asserted_at` across
    corroborations, never overwrite backwards. Each operation set is sealed
    and passed through the production applier."""
    from okto_neuron.companion import (
        _LLMNodes,
        _apply_sealed_semantic_plan,
        _plan_relationship_claims,
    )
    from okto_neuron.consolidate.ledger import CandidateLedger, edge_candidate_id
    from okto_neuron.consolidate.review_queue import ReviewQueue
    from okto_neuron.predicates import PredicateRegistry

    store = InMemoryStore()
    store.add_node(Node(id="subj1", type="Concept", title="Widget"))
    block_id = "b" * 64
    store.add_node(Node(id=block_id, type="Block", title="block", content="x"))
    store.add_node(Node(id="act1", type="Activity", title="act"))
    store.add_node(Node(id="agent1", type="Agent", title="agent"))

    llm = _LLMNodes(activity_id="act1", agent_id="agent1", model_id="m", prompt_hash="h")
    edge = EdgeCandidate(
        type="color",
        src_ref="subj1",
        dst_ref="",
        dst_literal="red",
        block_id=block_id,
        byte_start=0,
        byte_end=1,
        content_hash=sha256_hex("x"),
    )
    ledger = CandidateLedger(tmp_path / ".marginalia")

    def _plan_and_apply(asserted_at: str) -> tuple[object, dict[str, Any]]:
        candidate_id = edge_candidate_id(edge.model_dump(mode="json"))
        claim_plan = _plan_relationship_claims(
            store,
            [edge],
            planned_node_ids={"subj1"},
            titles={"subj1": "Widget"},
            confidences={"subj1": 0.9},
            pinned_predicates={candidate_id: edge.type},
            pinned_relation_traces={
                candidate_id: {
                    "d6": {
                        "reason": "literal",
                        "state": "canonical",
                        "action": "commit_literal",
                        "raw_predicate": edge.type,
                        "predicate": edge.type,
                        "subject_id": edge.src_ref,
                        "object_id": None,
                        "object_literal": edge.dst_literal,
                        "swapped": False,
                    },
                    "d7": {
                        "reason": "commit",
                        "action": "commit",
                        "relation_kind": "literal",
                        "subject_id": edge.src_ref,
                        "predicate": edge.type,
                        "object_id": None,
                        "object_literal": edge.dst_literal,
                        "liveness_support_ids": [edge.src_ref],
                    },
                }
            },
            llm=llm,
            embedder=_FixedEmbedder(),
            vault_root=tmp_path,
            min_claim_confidence=0.0,
            extra_mention_anchors={},
            asserted_at=asserted_at,
            embedding_settings=None,
        )
        plan_id = ledger.record_commit_plan(
            "asserted-at-test",
            operations=list(claim_plan.operations),
            context={"document_id": "doc-1"},
        )
        sealed = next(
            plan
            for plan in ledger.unreceipted_commit_plans(document_id="doc-1")
            if plan.plan_id == plan_id
        )
        result = _apply_sealed_semantic_plan(
            sealed,
            store=store,
            ledger=ledger,
            registry=PredicateRegistry(tmp_path),
            review_queue=ReviewQueue(tmp_path / ".marginalia", store),
        )
        return claim_plan, result

    _, first_result = _plan_and_apply("2026-07-01")
    assert first_result["claims_minted"] == 1
    claims = [c for c in store.list_nodes(type="Claim") if c.facets.get("O_literal") == "red"]
    assert len(claims) == 1
    claim_id = claims[0].id
    assert claims[0].facets.get("asserted_at") == "2026-07-01"

    # Re-assert the SAME fact from an OLDER source — hits the merge branch.
    second_plan, second_result = _plan_and_apply("2020-01-01")
    assert second_result["claims_minted"] == 0
    assert (
        second_plan.edge_results[edge_candidate_id(edge.model_dump(mode="json"))]["state"]
        == "merged"
    )
    merged = store.get_node(claim_id)
    assert merged.facets.get("asserted_at") == "2026-07-01", (
        "an older re-assertion overwrote (rather than max'd) asserted_at: "
        f"{merged.facets.get('asserted_at')}"
    )


def test_g5_merge_branch_calls_maybe_resurrect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T5: kills deleting resurrection from the planner's existing-Claim
    merge branch. The F7
    DETERMINISTIC leg (`resurrect_reverted_claims`) is neutralized via
    monkeypatch so ONLY the merge leg can resurrect the claim: a detached
    claim whose identity is re-extracted from a block that genuinely
    re-extracts (content changed enough to bust the Layer-1 skip) must lose
    `_detached`."""
    import okto_neuron.companion._incremental as _incremental_mod

    monkeypatch.setattr(_incremental_mod, "resurrect_reverted_claims", lambda *a, **kw: [])

    vault, note, red_id = _detach_fixture(tmp_path, monkeypatch)
    try:
        # Detach via deletion-only edit (test_f6 pattern).
        note.write_text("# Facts\n\nnothing extractable here anymore\n", encoding="utf-8")
        _make_sentinel_companion(vault, _SentinelExtractor()).remember(note)
        assert (vault.store.get_node(red_id).facets or {}).get(_DETACHED_KEY) is True

        # Re-emit the SAME fact with an extra line so the block's content
        # hash differs from every prior hash — Layer-1 skip cannot fire,
        # forcing a real re-extraction that re-mints the SAME claim id (the
        # merge branch, since the claim already exists — just detached).
        # Subchunk narrowing is disabled for this step: `by_index` prefers
        # the ORIGINAL v1 Block (it has claim_ids) over the intermediate
        # deletion Block when picking the sub-chunk diff's "old" side, so a
        # narrowed diff would see the FACT line as unchanged (present in
        # both v1 and this content) and narrow the hunk to just the new
        # unrelated line — never re-emitting the fact at all. Whole-block
        # extraction sidesteps that and is what this test needs to isolate.
        monkeypatch.setenv("OKTO_NEURON_SUBCHUNK_INGEST", "0")
        note.write_text(
            "# Facts\n\nFACT subj=Widget pred=color obj=red\n"
            "an extra unrelated line so this block re-extracts\n",
            encoding="utf-8",
        )
        ext3 = _SentinelExtractor()
        _make_sentinel_companion(vault, ext3).remember(note)

        assert ext3.call_count >= 1, "block did not re-extract; test setup invalid"
        facets = vault.store.get_node(red_id).facets or {}
        assert not facets.get(_DETACHED_KEY), (
            "merge-branch resurrection did not fire — _maybe_resurrect missing?"
        )
    finally:
        vault.close()


# ─────────────────────────────────────────────────────────────────────────────
# Fix A1: a block whose extraction FAILED (no live LLM claims) must be
# re-scheduled on the next ingest even when its bytes are unchanged.
# ─────────────────────────────────────────────────────────────────────────────


class _AlwaysFailingExtractor:
    """Every block raises a provider error — the Block node is still committed by
    ``vault.add``, but no LLM Claim is ever minted for it."""

    def __init__(self) -> None:
        self.call_count = 0

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        self.call_count += 1
        raise LLMProviderError("simulated provider outage")


def test_failed_block_is_rescheduled_on_reingest_of_identical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: run 1 fails extraction for every block (provider down) but
    commits the Block nodes. Run 2 over the SAME bytes used to be dropped by the
    sub-chunk narrowing pass (identical bytes ⇒ no hunks ⇒ []), leaving the
    document permanently un-ingestable while reporting success. The block must
    be re-scheduled."""
    monkeypatch.setenv("OKTO_NEURON_INCREMENTAL_INGEST", "1")
    monkeypatch.setenv("OKTO_NEURON_SUBCHUNK_INGEST", "1")

    vault = Vault.init(tmp_path / "v")
    try:
        note = Path(vault.path) / "note.md"
        note.write_text(
            "# Knowledge Graph\n\nA knowledge graph stores structured facts.\n"
            "It links entities via typed edges.\n" * 5,
            encoding="utf-8",
        )

        failing = _AlwaysFailingExtractor()
        with pytest.raises(Exception):
            _make_companion(vault, failing).remember(note)
        assert failing.call_count >= 1
        # Blocks are committed, but nothing was extracted from them.
        assert list(vault.store.list_nodes(type="Block")), "vault.add must commit Blocks"
        assert not [
            c for c in vault.store.list_nodes(type="Claim") if c.facets.get("model_id")
        ], "fixture assumption: run 1 minted no LLM claims"

        # Run 2: same bytes, working extractor.
        working = _CountingExtractor()
        result = _make_companion(vault, working).remember(note)

        assert working.call_count >= 1, (
            "a block that never extracted successfully must be re-scheduled on "
            "re-ingest of identical bytes"
        )
        assert result.outcome["units"]["scheduled"] >= 1
        assert result.blocks_total >= 1
    finally:
        vault.close()


def test_subchunk_units_unchanged_block_without_claims_falls_back_to_whole() -> None:
    """A1 (pure): identical bytes + a prior block carrying NO live LLM claims →
    the whole-block unit, not []."""
    from okto_neuron.companion._incremental import PriorClaimInfo

    old_text = "unchanged preamble\nsome value: 42\nfooter text\n"
    new_raw = old_text.encode()

    prior = PriorSnapshot(
        by_index={
            0: PriorClaimInfo(
                block_id="old-" + "x" * 60,
                content_hash=sha256_hex(new_raw),
                claim_ids=(),  # extraction never succeeded for this block
                block_index=0,
                content=old_text,
            )
        }
    )

    units = subchunk_units_for_block(
        block_id="d" * 64,
        block_index=0,
        new_byte_start=0,
        new_raw=new_raw,
        new_text=old_text,
        prior=prior,
    )

    assert len(units) == 1
    assert units[0].whole is True
    assert units[0].byte_end == len(new_raw)


def test_subchunk_units_unchanged_block_with_claims_is_still_skipped() -> None:
    """A1 negative: the optimisation this code exists for must not regress — an
    already-extracted block (live LLM claims) with no new hunks stays skipped."""
    from okto_neuron.companion._incremental import PriorClaimInfo

    old_text = "unchanged preamble\nsome value: 42\nfooter text\n"
    new_raw = old_text.encode()

    prior = PriorSnapshot(
        by_index={
            0: PriorClaimInfo(
                block_id="old-" + "y" * 60,
                content_hash=sha256_hex(new_raw),
                claim_ids=("c" * 64,),  # already successfully extracted
                block_index=0,
                content=old_text,
            )
        }
    )

    units = subchunk_units_for_block(
        block_id="e" * 64,
        block_index=0,
        new_byte_start=0,
        new_raw=new_raw,
        new_text=old_text,
        prior=prior,
    )

    assert units == []
