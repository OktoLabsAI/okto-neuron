"""Finding 3.4 (marginalia-deep-review §3.4): a Block id is minted from
``sha256(path, block_hash, block_index)`` where ``path`` used to be the bare
filename (``Path.name``), dropping the directory. Two same-basename files in
different vault directories whose corresponding block bytes hash identically
then mint the SAME Block id — ``add_node``'s MERGE-on-id (Ladybug) / plain
overwrite (InMemoryStore) semantics silently reassign one file's stored byte
range to the other file's path, and every earlier Claim anchored to that
block id resolves through it to the wrong file.

Model-free: drives ``ingest_document`` (the real production entry point,
which always has a ``vault_root`` — see ``vault.py``'s ``Vault.add``) against
an InMemoryStore.
"""

from __future__ import annotations

from pathlib import Path

from okto_neuron.ingest import ingest_document
from okto_neuron.ingest.markdown import parse_markdown
from okto_neuron.store.memory import InMemoryStore


def _write_readme(vault_root: Path, subdir: str, body: str) -> Path:
    d = vault_root / subdir
    d.mkdir(parents=True, exist_ok=True)
    p = d / "README.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_same_basename_files_in_different_dirs_do_not_collide_on_block_id(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path
    store = InMemoryStore()

    # Identical body -> identical block content_hash at block_index 0 for
    # both files, so only the path component of the id hash can tell them
    # apart.
    body = "# Notes\n\nidentical body text across both files.\n"
    path_a = _write_readme(vault_root, "projA", body)
    path_b = _write_readme(vault_root, "projB", body)

    ingest_document(store, path_a, vault_root=vault_root)
    ingest_document(store, path_b, vault_root=vault_root)

    blocks = [n for n in store.list_nodes(type="Block")]
    block_ids = {b.id for b in blocks}
    assert len(blocks) == 2, (
        "each file must anchor its own Block — a collision means the second "
        "ingest_document() call overwrote the first file's stored node"
    )
    assert len(block_ids) == 2, (
        "same-basename files in different directories must NOT collide on Block id"
    )

    path_by_source = {b.facets.get("source_path"): b.facets.get("path") for b in blocks}
    assert path_by_source[str(path_a.resolve())] == "projA/README.md"
    assert path_by_source[str(path_b.resolve())] == "projB/README.md"

    # And each Document's own byte-anchored provenance is intact — the byte
    # range for A's block still points at A's bytes, not B's.
    a_block = next(b for b in blocks if b.facets.get("source_path") == str(path_a.resolve()))
    b_block = next(b for b in blocks if b.facets.get("source_path") == str(path_b.resolve()))
    a_start, a_end = a_block.facets["byte_start"], a_block.facets["byte_end"]
    b_start, b_end = b_block.facets["byte_start"], b_block.facets["byte_end"]
    assert path_a.read_bytes()[a_start:a_end] == path_b.read_bytes()[b_start:b_end]
    assert a_block.id != b_block.id


def test_block_path_is_vault_relative_for_nested_source(tmp_path: Path) -> None:
    vault_root = tmp_path
    sub = vault_root / "notes" / "sub"
    sub.mkdir(parents=True)
    p = sub / "idea.md"
    p.write_text("# hi\n\nbody\n", encoding="utf-8")

    store = InMemoryStore()
    ingest_document(store, p, vault_root=vault_root)

    blocks = list(store.list_nodes(type="Block"))
    assert blocks
    assert all(b.facets.get("path") == "notes/sub/idea.md" for b in blocks)


def test_block_path_falls_back_to_bare_name_without_vault_root(tmp_path: Path) -> None:
    """Documented fallback (no vault root known): bare filename, matching the
    historical behaviour, for a direct ``parse_markdown()`` call outside a
    vault context (a script or a test)."""
    p = tmp_path / "sub" / "note.md"
    p.parent.mkdir(parents=True)
    p.write_text("# hi\n", encoding="utf-8")

    parsed = parse_markdown(p, extraction_activity_id="t", agent_id="t")
    assert parsed.blocks[0].block.path == "note.md"
