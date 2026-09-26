from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

from okto_neuron.store import vault as vault_module
from okto_neuron.store.index.indexed import IndexedStore
from okto_neuron.store.ladybug import LadybugStore, VaultConnection
from okto_neuron.store.vault import _open_vault


@pytest.fixture(autouse=True)
def close_vault_handles() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def test_open_vault_scaffolds_and_bootstraps_on_first_call(tmp_path: Path) -> None:
    vault_path = tmp_path / "v_new"
    vault_path.mkdir()
    assert list(vault_path.iterdir()) == []

    store = _open_vault(vault_path)

    try:
        assert isinstance(store, IndexedStore)
        assert isinstance(store.graph, LadybugStore)
        for directory in (
            vault_path,
            vault_path / "notes",
            vault_path / "refs",
            vault_path / ".marginalia",
        ):
            assert directory.is_dir()
            assert stat.S_IMODE(directory.stat().st_mode) == 0o755

        config_path = vault_path / "okto-neuron.yaml"
        assert config_path.is_file()
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert config["federation_opt_in"] is False
        assert config["marginalia_yaml_version"] == 1

        assert (vault_path / "graph.lbug").exists()
    finally:
        store.close()

    assert store.is_closed
