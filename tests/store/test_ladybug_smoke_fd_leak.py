from __future__ import annotations

import gc
from pathlib import Path

import psutil

from okto_neuron.core.schema import Node
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection
from okto_neuron.store.vault import _open_vault


def test_ladybug_100_node_round_trip_releases_open_files(tmp_path: Path) -> None:
    store = _open_vault(tmp_path / "v")
    process = psutil.Process()
    before = process.open_files()

    try:
        for index in range(100):
            store.add_node(
                Node(
                    id=f"node-{index:03d}",
                    type="Concept",
                    title=f"Node {index}",
                    content="bulk smoke test",
                )
            )

        nodes = list(store.list_nodes(type="Concept"))

        assert len(nodes) == 100
    finally:
        store.close()
        VaultConnection.close_all()
        vault_module._STORE_CACHE.clear()
        gc.collect()

    after = process.open_files()

    assert len(after) <= len(before)
