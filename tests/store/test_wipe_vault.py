"""Unit tests for the shared ``wipe_vault`` helper (store/vault.py).

``wipe_vault`` is the single source of truth behind the CLI ``kg init --wipe``
and the API ``POST /api/v1/reset``. These tests pin its contract directly,
model-free (no LLM, no embedder): path-safety refusal, the ``keep_config`` gate
on config *writing*, idempotency, and that derived/user content is emptied while
the markdown trust root is re-scaffolded clean.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_neuron.errors import VaultNotFoundError
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection
from okto_neuron.store.vault import _scaffold_vault, wipe_vault


@pytest.fixture(autouse=True)
def close_vault_handles() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def _make_vault(root: Path) -> Path:
    """Scaffold a bootstrapped vault on disk (config present), model-free."""
    vault_module._open_vault(root).close()
    assert (root / "okto-neuron.yaml").is_file()
    return root


# ── (a) path-safety: never wipe a directory that is not a vault ──────────────


def test_wipe_vault_refuses_non_vault(tmp_path: Path) -> None:
    not_a_vault = tmp_path / "plain_dir"
    not_a_vault.mkdir()
    (not_a_vault / "keep.txt").write_text("precious", encoding="utf-8")

    with pytest.raises(VaultNotFoundError):
        wipe_vault(not_a_vault)

    # nothing was touched
    assert (not_a_vault / "keep.txt").read_text(encoding="utf-8") == "precious"
    assert not (not_a_vault / "okto-neuron.yaml").exists()


def test_wipe_vault_refuses_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(VaultNotFoundError):
        wipe_vault(tmp_path / "does_not_exist")


# ── (b) keep_config=True preserves okto-neuron.yaml byte-for-byte ─────────────


def test_wipe_vault_keep_config_preserves_bytes(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "v")
    config_path = vault / "okto-neuron.yaml"
    # Mutate the config with a sentinel so "preserved" is distinguishable from
    # "rewrote the default".
    sentinel = config_path.read_bytes() + b"\n# sentinel-keep-config-marker\n"
    config_path.write_bytes(sentinel)
    before = config_path.read_bytes()

    wipe_vault(vault, keep_config=True)

    assert config_path.is_file()
    assert config_path.read_bytes() == before


def test_wipe_vault_keep_config_is_default(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "v")
    config_path = vault / "okto-neuron.yaml"
    sentinel = config_path.read_bytes() + b"\n# default-keep-config-marker\n"
    config_path.write_bytes(sentinel)
    before = config_path.read_bytes()

    wipe_vault(vault)  # default keep_config=True

    assert config_path.read_bytes() == before


def test_wipe_vault_rebuilds_schema_from_preserved_embedding_config(tmp_path: Path) -> None:
    """Start fresh uses the saved target width, not the deleted graph's width."""
    from okto_neuron.config import VaultConfig
    from okto_neuron.store._bootstrap import bootstrap_vault_graph

    vault = _make_vault(tmp_path / "v")
    VaultConfig.apply_patch(
        vault,
        {"embedding": {"provider": "stub", "model": "stub-2560", "dimension": 2560}},
    )

    wipe_vault(vault, keep_config=True)

    assert bootstrap_vault_graph(vault).embedding_dim == 2560


# ── (c) keep_config=False leaves okto-neuron.yaml ABSENT on return ────────────


def test_wipe_vault_drop_config_leaves_config_absent(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "v")
    config_path = vault / "okto-neuron.yaml"
    assert config_path.exists()

    wipe_vault(vault, keep_config=False)

    # Absent so a subsequent Vault.init() passes its config-absent guard and
    # applies freshly requested packs/embedder.
    assert not config_path.exists()
    # The graph was still re-scaffolded (the vault dir is otherwise live).
    assert (vault / "graph.lbug").exists()


# ── (d) idempotency ──────────────────────────────────────────────────────────


def test_wipe_vault_keep_config_is_idempotent(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "v")

    wipe_vault(vault, keep_config=True)
    # A second wipe must not error: config survived the first wipe.
    wipe_vault(vault, keep_config=True)

    assert (vault / "okto-neuron.yaml").is_file()
    assert (vault / "graph.lbug").exists()


def test_wipe_vault_drop_config_second_call_refuses(tmp_path: Path) -> None:
    """keep_config=False removes the config, so a repeat is (correctly) refused.

    This is the contract, not a bug: once the config is gone the directory is no
    longer a vault, and wipe_vault must not delete a non-vault.
    """
    vault = _make_vault(tmp_path / "v")

    wipe_vault(vault, keep_config=False)
    assert not (vault / "okto-neuron.yaml").exists()

    with pytest.raises(VaultNotFoundError):
        wipe_vault(vault, keep_config=False)


# ── (e) notes / refs / .marginalia derived state are emptied ─────────────────


def test_wipe_vault_empties_notes_refs_and_sources(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "v")

    # Populate user content + durable ingest queue first, otherwise "emptied"
    # proves nothing.
    (vault / "notes").mkdir(exist_ok=True)
    (vault / "refs").mkdir(exist_ok=True)
    sources = vault / ".marginalia" / "sources"
    sources.mkdir(parents=True, exist_ok=True)

    note = vault / "notes" / "a.md"
    ref = vault / "refs" / "paper.pdf"
    queued = sources / "pending.json"
    note.write_text("# note\n", encoding="utf-8")
    ref.write_bytes(b"%PDF-1.4 fake")
    queued.write_text("{}", encoding="utf-8")

    wipe_vault(vault, keep_config=True)

    # User content is gone; the dirs are re-scaffolded empty.
    assert (vault / "notes").is_dir()
    assert (vault / "refs").is_dir()
    assert list((vault / "notes").iterdir()) == []
    assert list((vault / "refs").iterdir()) == []
    assert not note.exists()
    assert not ref.exists()

    # The durable ingest queue is wiped (not re-scaffolded by _scaffold_vault).
    assert not queued.exists()
    if sources.exists():
        assert list(sources.iterdir()) == []


def test_wipe_vault_removes_all_marginalia_sidecars(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "v")
    marginalia_dir = vault / ".marginalia"

    stale_files = [
        marginalia_dir / "sources" / "imported.md",
        marginalia_dir / "ingest-history.json",
        marginalia_dir / "curation-jobs.json",
        marginalia_dir / "rebuild.state.json",
        marginalia_dir / "reembed.state.json",
        marginalia_dir / "authority" / "index.json",
        marginalia_dir / "reconcile" / "queue.json",
        marginalia_dir / "corrupt-graph-20260608" / "graph.lbug",
        marginalia_dir / "unknown-derived.bin",
    ]
    for path in stale_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stale", encoding="utf-8")

    wipe_vault(vault, keep_config=True)

    assert marginalia_dir.is_dir()
    for path in stale_files:
        assert not path.exists(), path

    remaining_files = {
        path.relative_to(marginalia_dir) for path in marginalia_dir.rglob("*") if path.is_file()
    }
    # Bootstrap creates only the lock plus the conservative integrity state for
    # the new graph generation; all stale sidecars from the prior generation are gone.
    assert remaining_files <= {
        Path(".bootstrap.lock"),
        Path("graph-integrity.json"),
    }


def test_wipe_vault_removes_graph_wal_siblings(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "v")
    # Simulate a stray WAL/shadow sibling left by the store.
    stray = vault / "graph.lbug.wal"
    stray.write_bytes(b"stale-wal")

    wipe_vault(vault, keep_config=True)

    # The stale WAL is removed; bootstrap legitimately re-creates a fresh empty
    # graph (and its WAL) at the same path, so assert on content, not existence.
    assert not stray.exists() or stray.read_bytes() != b"stale-wal"
    # A fresh empty graph is bootstrapped in its place.
    assert (vault / "graph.lbug").exists()


# ── (f) M4: grafx-pinned vault (directory-shaped graph storage) ──────────────


def _make_vault_grafx(root: Path) -> Path:
    """Scaffold a grafx-pinned vault (config present, ``graph.grafx/``
    bootstrapped), model-free. Mirrors ``_make_vault`` above but pins the
    backend explicitly first, the same way every other M4 grafx test fixture
    does (``tests/server/test_curation_rebuild.py``'s ``_make_vault_grafx``).
    """
    from okto_neuron.cli.kg import kg_init

    assert kg_init(root, backend="grafx") == 0
    # kg_init's own handle is already closed; drop the process-wide cache so
    # the fresh direct opens below in the caller don't collide with a
    # lingering cached entry (same hygiene as the sibling daemon fixture).
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()
    assert (root / "okto-neuron.yaml").is_file()
    assert (root / "graph.grafx").is_dir()
    return root


def test_wipe_vault_grafx_deletes_graph_dir_and_reopens_empty(tmp_path: Path) -> None:
    """M4 spec section 4: against a grafx-pinned vault, ``wipe_vault``
    deletes the directory-shaped ``graph.grafx/`` (not Ladybug's file glob --
    ``_erase_backend_storage``'s backend-dispatched branch, store/vault.py)
    and leaves a fresh, empty store openable in its place. The existing
    Ladybug cases above stay green (nothing in this test touches them)."""
    pytest.importorskip("okto_grafx")
    from okto_neuron.core.schema import Node

    vault = _make_vault_grafx(tmp_path / "v")
    store = vault_module._open_vault(vault)
    store.add_node(Node(id="n1", type="Concept", title="one"))
    store.close()
    vault_module._STORE_CACHE.clear()
    assert (vault / "graph.grafx").is_dir()

    wipe_vault(vault, keep_config=True)

    # The old directory (holding "n1") is gone; a fresh one is re-bootstrapped
    # at the same path -- content-empty, not path-absent (mirrors
    # test_wipe_vault_removes_graph_wal_siblings' own "assert on content, not
    # existence" reasoning for Ladybug's WAL sibling).
    assert (vault / "graph.grafx").is_dir()
    reopened = vault_module._open_vault(vault)
    try:
        assert list(reopened.list_nodes()) == []
    finally:
        reopened.close()


def test_wipe_vault_grafx_removes_swap_sidecars(tmp_path: Path) -> None:
    """A stray ``.bak``/``.discard`` sibling left by an interrupted
    ``GrafxStaging`` swap (store/staging.py) is swept up by the same
    ``graph.grafx*`` glob that removes the live directory -- mirrors
    ``test_wipe_vault_removes_graph_wal_siblings``'s Ladybug WAL-sibling
    case for Grafx's own sidecar shape."""
    pytest.importorskip("okto_grafx")

    vault = _make_vault_grafx(tmp_path / "v")
    stray = vault / "graph.grafx.bak"
    stray.mkdir()
    (stray / "sentinel").write_text("stale", encoding="utf-8")

    wipe_vault(vault, keep_config=True)

    assert not stray.exists()
    assert (vault / "graph.grafx").is_dir()


def test_scaffold_helper_importable() -> None:
    # Sanity: the helper wipe_vault relies on is the same one tests scaffold with.
    assert callable(_scaffold_vault)
