"""Live-vault audit finding: a Document's title used to be the bare file stem
(``p.stem``), so every same-named file anywhere in the ingest tree rendered
identically in any list, search result, or graph view — one live vault had 69
Document nodes but only 56 distinct titles (``SPRINT`` x7, ``sha256`` x4,
``catalogo`` x4, ``README`` x2). The Document *id* was already unique (hashed
from the resolved absolute path), so this was a display-only bug.

The title is now the document's path relative to the INGEST ROOT it was added
under, extension dropped — an explicit frontmatter ``title:`` still wins, and
the bare stem is still the fallback when no root is known.

Model-free: drives ``ingest_document`` (the real production entry point, which
always has a ``vault_root`` — see ``vault.py``'s ``Vault.add``) against an
InMemoryStore, mirroring ``tests/ingest/test_block_path_vault_relative.py``.
"""

from __future__ import annotations

from pathlib import Path

from okto_neuron.ingest import ingest_document
from okto_neuron.ingest.markdown import parse_markdown
from okto_neuron.store.memory import InMemoryStore


def _write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_same_basename_files_in_different_dirs_get_different_titles(tmp_path: Path) -> None:
    """Two files sharing a basename in different real vault directories (the
    plain ``okto-neuron add`` in-vault case, not durable-copy mirrored) must
    get distinct, path-derived titles."""
    vault_root = tmp_path
    store = InMemoryStore()

    path_a = _write(vault_root / "projects" / "alpha" / "SPRINT.md", "# Sprint\n\nbody a\n")
    path_b = _write(vault_root / "projects" / "beta" / "SPRINT.md", "# Sprint\n\nbody b\n")

    ingest_document(store, path_a, vault_root=vault_root)
    ingest_document(store, path_b, vault_root=vault_root)

    docs = {d.facets.get("path"): d.title for d in store.list_nodes(type="Document")}
    title_a = docs[str(path_a.resolve())]
    title_b = docs[str(path_b.resolve())]
    assert title_a != title_b
    assert title_a == "projects/alpha/SPRINT"
    assert title_b == "projects/beta/SPRINT"


def test_same_basename_durable_copies_under_sources_get_different_titles(tmp_path: Path) -> None:
    """The normal watched-folder/CLI-add shape: the durable copy lives under
    ``.marginalia/sources/<root-key>/<relpath>`` (F11's ``durable_copy_path``),
    where ``root-key`` is 16 lowercase hex characters. The title must recover
    the ORIGINAL relative tree — the root-key directory is durable-copy
    scaffolding, not something the user organized."""
    vault_root = tmp_path
    store = InMemoryStore()
    root_key = "a1b2c3d4e5f60718"  # 16 hex chars like durable_copy_path mints; gitleaks:allow

    path_a = _write(
        vault_root / ".marginalia" / "sources" / root_key / "projects" / "alpha" / "SPRINT.md",
        "# Sprint\n\nbody a\n",
    )
    path_b = _write(
        vault_root / ".marginalia" / "sources" / root_key / "projects" / "beta" / "SPRINT.md",
        "# Sprint\n\nbody b\n",
    )

    ingest_document(store, path_a, vault_root=vault_root)
    ingest_document(store, path_b, vault_root=vault_root)

    docs = {d.facets.get("path"): d.title for d in store.list_nodes(type="Document")}
    title_a = docs[str(path_a.resolve())]
    title_b = docs[str(path_b.resolve())]
    assert title_a != title_b
    # The root-key directory is stripped; what remains is the original tree.
    assert title_a == "projects/alpha/SPRINT"
    assert title_b == "projects/beta/SPRINT"


def test_frontmatter_title_wins_over_derived_path(tmp_path: Path) -> None:
    vault_root = tmp_path
    store = InMemoryStore()
    p = _write(
        vault_root / "notes" / "sub" / "note.md",
        "---\ntitle: My Custom Title\n---\n\nbody\n",
    )

    ingest_document(store, p, vault_root=vault_root)

    doc = next(iter(store.list_nodes(type="Document")))
    assert doc.title == "My Custom Title"


def test_title_falls_back_to_bare_stem_without_vault_root(tmp_path: Path) -> None:
    """Documented fallback (no vault root known): bare file stem, matching the
    historical behaviour, for a direct ``parse_markdown()`` call outside a
    vault context (a script, a test, or ``okto-neuron add /tmp/foo.md`` with
    ``allow_external_sources``)."""
    p = tmp_path / "sub" / "foo.md"
    p.parent.mkdir(parents=True)
    p.write_text("# hi\n", encoding="utf-8")

    ingest = parse_markdown(p, extraction_activity_id="t", agent_id="t")
    assert ingest.item.title == "foo"
