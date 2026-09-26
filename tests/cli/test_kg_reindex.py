"""``okto-neuron kg reindex`` — rebuild the search index from the graph.

Covers the staleness check (up to date vs. stale after a bypass write) and
``--force``. See the internal ADR 0041 plan M1 spec part 1 section 4
and part 2 section 5.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
import pytest

from okto_neuron.cli import app
from okto_neuron.core.schema import Node
from okto_neuron.store.ladybug import LadybugStore, VaultConnection
from okto_neuron.store.vault import _open_vault


@pytest.fixture(autouse=True)
def close_vault_handles():
    yield
    VaultConnection.close_all()


def _concept(node_id: str) -> Node:
    return Node(id=node_id, type="Concept", title=node_id, content="reindex test fixture")


def test_kg_reindex_reports_up_to_date_when_index_matches_graph(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    store = _open_vault(vault_path)
    store.add_node(_concept("node-1"))
    store.add_node(_concept("node-2"))
    store.checkpoint()
    store.close()

    runner = CliRunner()
    result = runner.invoke(app, ["kg", "reindex", str(vault_path)])

    assert result.exit_code == 0, result.output
    assert "up to date" in result.output


def test_kg_reindex_rebuilds_when_a_bypass_write_makes_the_index_stale(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    store = _open_vault(vault_path)
    store.add_node(_concept("node-1"))
    store.add_node(_concept("node-2"))
    store.checkpoint()
    store.close()
    VaultConnection.close_all()

    # Bypass the index: write directly through the raw graph store.
    raw = LadybugStore(vault_path)
    raw.add_node(_concept("node-3"))
    raw.close()
    VaultConnection.close_all()

    runner = CliRunner()
    result = runner.invoke(app, ["kg", "reindex", str(vault_path)])

    assert result.exit_code == 0, result.output
    assert "up to date" not in result.output
    assert "doc_count=3" in result.output


def test_kg_reindex_force_always_rebuilds(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    store = _open_vault(vault_path)
    store.add_node(_concept("node-1"))
    store.checkpoint()
    store.close()
    VaultConnection.close_all()

    runner = CliRunner()
    result = runner.invoke(app, ["kg", "reindex", str(vault_path), "--force"])

    assert result.exit_code == 0, result.output
    assert "doc_count=1" in result.output
