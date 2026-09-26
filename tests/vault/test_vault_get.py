from __future__ import annotations

from pathlib import Path

import pytest

from okto_neuron import Node, Vault
from okto_neuron.core.schema import Node as StoreNode
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture(autouse=True)
def close_vault_handles() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def test_ts_fd47f193_get_returns_none_on_miss_and_node_on_hit(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    vault.store.add_node(StoreNode(id="real-id", type="Document", title="Hydrated node"))

    try:
        assert vault.get("nonexistent") is None
        node = vault.get("real-id")

        assert isinstance(node, Node)
        assert node.id == "real-id"
        assert node.type == "Document"
        assert node.name == "Hydrated node"
    finally:
        vault.close()
