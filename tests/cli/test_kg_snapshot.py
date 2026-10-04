"""``okto-neuron kg snapshot`` — dump / verify / load a logical graph snapshot.

Covers the dump -> verify -> load round trip into a fresh vault (reopened
through ``_open_vault`` with a graph/index generation that already agree) and
the offline single-writer lease guard that ``kg snapshot dump`` reuses from
``kg reindex``. See the internal ADR 0041 plan M2a spec section 4
(CLI) and section 5 (tests, CLI block).
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
import pytest

from okto_neuron.cli import app
from okto_neuron.core.schema import Edge, Node
from okto_neuron.store.handle_lease import acquire_vault_handle_lease
from okto_neuron.store.index import compute_graph_generation
from okto_neuron.store.ladybug import VaultConnection
from okto_neuron.store.vault import _open_vault


@pytest.fixture(autouse=True)
def close_vault_handles():
    yield
    VaultConnection.close_all()


def _concept(node_id: str) -> Node:
    return Node(id=node_id, type="Concept", title=node_id, content=f"snapshot fixture {node_id}")


def _populated_vault(vault_path: Path) -> None:
    """Open a fresh vault, write three nodes and one edge, then close it."""
    store = _open_vault(vault_path)
    store.add_node(_concept("node-1"))
    store.add_node(_concept("node-2"))
    store.add_node(_concept("node-3"))
    store.add_edge(Edge(type="related_to", src="node-1", dst="node-2"))
    store.checkpoint()
    store.close()
    VaultConnection.close_all()


def test_kg_snapshot_dump_verify_load_round_trip(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    _populated_vault(vault_path)

    dest = tmp_path / "snapshot"
    runner = CliRunner()

    dump_result = runner.invoke(app, ["kg", "snapshot", "dump", str(vault_path), str(dest)])
    assert dump_result.exit_code == 0, dump_result.output
    assert (dest / "manifest.json").exists()

    verify_result = runner.invoke(app, ["kg", "snapshot", "verify", str(dest)])
    assert verify_result.exit_code == 0, verify_result.output

    new_vault = tmp_path / "restored"
    load_result = runner.invoke(app, ["kg", "snapshot", "load", str(dest), str(new_vault)])
    assert load_result.exit_code == 0, load_result.output

    VaultConnection.close_all()
    restored = _open_vault(new_vault)
    try:
        node_ids = sorted(node.id for node in restored.list_nodes())
        assert node_ids == ["node-1", "node-2", "node-3"]
        assert compute_graph_generation(restored.graph) == restored.index.generation()
    finally:
        restored.close()


def test_kg_snapshot_dump_onto_live_handle_exits_5_with_lock_message(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    _populated_vault(vault_path)

    dest = tmp_path / "snapshot"
    runner = CliRunner()

    with acquire_vault_handle_lease(vault_path, operation="test"):
        result = runner.invoke(app, ["kg", "snapshot", "dump", str(vault_path), str(dest)])

    assert result.exit_code == 5
    assert "another process owns a live graph handle" in result.output
