"""Okto Neuron v0 MVP end-to-end coverage."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

from click.testing import CliRunner

from okto_neuron.cli import app
from okto_neuron.vault import Vault

FIXTURES = Path(__file__).parent / "fixtures" / "synthetic_vault"


def test_kg_init_add_query_provenance_round_trip(tmp_path: Path, cli_inprocess_server) -> None:
    runner = CliRunner()
    vault_path = tmp_path / "vault"
    files = sorted(FIXTURES.glob("*.md"))
    assert 10 <= len(files) <= 15

    init_result = runner.invoke(app, ["init", str(vault_path), "--embedder", "stub"])
    assert init_result.exit_code == 0, init_result.output
    assert (vault_path / "okto-neuron.yaml").exists()
    assert (vault_path.stat().st_mode & 0o777) == 0o700

    # Route the thin-client CLI (add/query) at an in-process server bound to
    # THIS vault, so the round-trip is hermetic.
    cli_inprocess_server(vault_path)

    for file_path in files:
        result = runner.invoke(app, ["add", str(file_path), "--vault", str(vault_path)])
        assert result.exit_code == 0, result.output

    query_result = runner.invoke(
        app,
        [
            "query",
            "provenance Claim Block byte offsets content hash",
            "--vault",
            str(vault_path),
            "--k",
            "5",
            "--format",
            "json",
        ],
    )
    assert query_result.exit_code == 0, query_result.output
    hits = json.loads(query_result.output)
    assert hits
    # Claims-first short-circuit was removed: a Document/entity can outrank a
    # Claim. The contract is that a provenance-bearing Claim is present in top-k.
    first = next((hit for hit in hits if hit["claim_id"]), None)
    assert first is not None
    assert first["path"]
    assert first["byte_end"] > first["byte_start"]
    assert len(first["content_hash"]) == 64

    vault = Vault.open(vault_path)
    assert vault.storage_info.backend == "json-fallback"
    provenance = vault.get_provenance(first["claim_id"])
    assert provenance is not None
    assert provenance["claim"]["type"] == "Claim"
    assert provenance["block"]["type"] == "Block"
    edge_types = {edge["type"] for edge in provenance["edges"]}
    assert edge_types == {
        "prov:wasDerivedFrom",
        "prov:wasGeneratedBy",
        "prov:wasAttributedTo",
    }

    block_facets = provenance["block"]["facets"]
    source_bytes = Path(block_facets["source_path"]).read_bytes()
    block_bytes = source_bytes[block_facets["byte_start"] : block_facets["byte_end"]]
    assert hashlib.sha256(block_bytes).hexdigest() == block_facets["content_hash"]

    # Idempotency: re-ingest the same files through the SAME entrypoint the
    # first pass used (the CLI/server). Re-adding via direct vault.add(original
    # fixture path) would re-home from a different source_path than the server's
    # persisted copy and spuriously mint new ids — that mixes ingest paths, not
    # an idempotency violation. Reopen from disk to observe the server's writes.
    claim_ids_before = {node.id for node in vault.store.list_nodes(type="Claim")}
    for file_path in files:
        result = runner.invoke(app, ["add", str(file_path), "--vault", str(vault_path)])
        assert result.exit_code == 0, result.output
    claim_ids_after = {node.id for node in Vault.open(vault_path).store.list_nodes(type="Claim")}
    assert claim_ids_after == claim_ids_before

    mode = stat.S_IMODE(vault_path.stat().st_mode)
    assert mode == 0o700
