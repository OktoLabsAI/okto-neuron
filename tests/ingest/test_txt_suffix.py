"""Regression: .txt sources ingest end-to-end (2026-07-03).

The ingest queue enqueues .txt as ingestible (TEXT_SUFFIXES), but
ingest_document rejected anything but .md/.markdown. The old flat durable-
copy scheme force-renamed .txt→.md and masked the mismatch; the F11
suffix-preserving tree copies exposed it — 3 real transcripts failed the
demo-vault rebuild-from-zero until the gate matched the queue contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.config import VaultConfig
from okto_neuron.ingest.markdown import parse_markdown


def test_txt_source_ingests(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        p = Path(vault.path) / "transcript.txt"
        p.write_text("# Call\n\nKevin asked about SLAs.\n", encoding="utf-8")
        doc = vault.add(p)
        assert doc.id
        blocks = [n for n in vault.store.list_nodes(type="Block")]
        assert blocks, "no Block anchored for the .txt source"
    finally:
        vault.close()


def test_unsupported_suffix_still_rejected(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        p = Path(vault.path) / "image.png"
        p.write_bytes(b"fake")
        from okto_neuron.errors import IngestError

        with pytest.raises(IngestError):
            vault.add(p)
    finally:
        vault.close()


def test_configurable_chunks_overlap_on_whole_line_byte_boundaries(tmp_path: Path) -> None:
    source = tmp_path / "lines.txt"
    source.write_bytes(b"aaaa\nbbbb\ncccc\ndddd\n")

    parsed = parse_markdown(
        source,
        extraction_activity_id="test",
        agent_id="test",
        chunk_size_bytes=10,
        chunk_overlap_bytes=5,
    )

    assert [block.raw for block in parsed.blocks] == [
        b"aaaa\nbbbb\n",
        b"bbbb\ncccc\n",
        b"cccc\ndddd\n",
    ]
    raw = source.read_bytes()
    for block in parsed.blocks:
        assert raw[block.block.byte_start : block.block.byte_end] == block.raw


def test_vault_ingest_records_and_selects_the_effective_chunk_policy(
    tmp_path: Path,
) -> None:
    from okto_neuron.companion import _extraction_units

    vault = Vault.init(tmp_path / "v")
    try:
        source = Path(vault.path) / "long.txt"
        source.write_text("".join(f"line {i:03d} " + "x" * 40 + "\n" for i in range(30)))

        VaultConfig.apply_patch(
            vault.path,
            {"ingest": {"chunk_size_bytes": 256, "chunk_overlap_bytes": 0}},
        )
        vault.add(source)

        VaultConfig.apply_patch(
            vault.path,
            {"ingest": {"chunk_size_bytes": 512, "chunk_overlap_bytes": 100}},
        )
        vault.add(source)

        blocks = [
            node
            for node in vault.store.list_nodes(type="Block")
            if node.facets.get("source_path") == str(source.resolve(strict=False))
        ]
        assert {node.facets["chunk_size_bytes"] for node in blocks} == {256, 512}

        units = _extraction_units(
            vault.store,
            source,
            chunk_size_bytes=512,
            chunk_overlap_bytes=100,
        )
        assert units
        assert all(
            vault.store.get_node(anchor.block_id).facets["chunk_size_bytes"] == 512
            for anchor, _text in units
            if anchor is not None
        )
    finally:
        vault.close()
