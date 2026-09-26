"""Small API smoke tests for the MVP Claim/Block contract."""

from pathlib import Path

from okto_neuron import Vault


def test_init_add_query(tmp_path: Path) -> None:
    vault = Vault.init(
        tmp_path / "v", packs=["core", "research", "personal"], embedding_provider="stub"
    )
    note = Path(vault.path) / "n.md"
    note.write_text(
        "---\ntitle: First note\ntags: [llm, kg]\n---\n"
        "# Knowledge Graphs\n\n"
        "This is a note about provenance and [[JL Carter]] with #traceability.\n",
        encoding="utf-8",
    )
    item = vault.add(note)
    assert item.title == "First note"
    assert "llm" in item.tags

    hits = vault.query("provenance traceability")
    assert hits
    # Claims-first short-circuit was removed: a non-Claim node (e.g. the raw
    # Block) can now legitimately outrank Claims. The contract is that a Claim
    # is PRESENT in the top-k, anchored with valid byte-range provenance.
    claim_hit = next((h for h in hits if h.claim_id), None)
    assert claim_hit is not None
    assert claim_hit.path == str(note.resolve())
    assert claim_hit.byte_end > claim_hit.byte_start
    assert len(claim_hit.content_hash) == 64

    provenance = vault.get_provenance(claim_hit.claim_id)
    assert provenance is not None
    assert provenance["block"]["type"] == "Block"


def test_packs_loaded() -> None:
    from okto_neuron.packs.registry import load_packs

    reg = load_packs(["core", "research"])
    assert "Note" in reg.all_node_types()
    assert "mentions" in reg.all_edge_types()
