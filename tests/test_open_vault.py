from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml

from okto_neuron.config._vault import CURRENT_YAML_VERSION, VaultConfig
from okto_neuron.core.schema import Node
from okto_neuron.errors import VaultPathNotADirectory
from okto_neuron.store import vault as vault_module
from okto_neuron.store.vault import _open_vault


@pytest.fixture(autouse=True)
def clear_open_vault_cache() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()


def test_open_vault_scaffolds_dirs_at_0755(tmp_path: Path) -> None:
    vault_path = tmp_path / "vaults" / "alpha"
    store = _open_vault(vault_path)

    assert not store.is_closed
    for directory in (
        vault_path,
        vault_path / "notes",
        vault_path / "refs",
        vault_path / ".marginalia",
    ):
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755


def test_open_vault_writes_default_marginalia_yaml(tmp_path: Path) -> None:
    vault_path = tmp_path / "vaults" / "alpha"

    _open_vault(vault_path)

    config_path = vault_path / "okto-neuron.yaml"
    assert config_path.is_file()
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == (
        VaultConfig.default().model_dump(mode="json", exclude_none=True)
        | {"marginalia_yaml_version": CURRENT_YAML_VERSION}
    )
    assert VaultConfig.load(vault_path) == VaultConfig.default().model_copy(
        update={"marginalia_yaml_version": CURRENT_YAML_VERSION}
    )


def test_open_vault_is_idempotent_and_returns_live_handle(tmp_path: Path) -> None:
    vault_path = tmp_path / "vaults" / "alpha"

    first = _open_vault(vault_path)
    second = _open_vault(vault_path)

    assert second is first
    second.add_node(Node(id="live-node", type="Document", title="Live"))
    assert second.get_node("live-node").title == "Live"


def test_open_vault_preserves_existing_config(tmp_path: Path) -> None:
    vault_path = tmp_path / "vaults" / "alpha"
    vault_path.mkdir(parents=True)
    config_path = vault_path / "okto-neuron.yaml"
    existing = {
        "marginalia_yaml_version": 1,
        "vault_id": "custom",
        "federation_opt_in": True,
        "packs": ["core"],
    }
    config_path.write_text(yaml.safe_dump(existing, sort_keys=False), encoding="utf-8")

    _open_vault(vault_path)

    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == existing


def test_open_vault_rejects_file_path(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault-file"
    vault_path.write_text("not a directory", encoding="utf-8")

    with pytest.raises(VaultPathNotADirectory):
        _open_vault(vault_path)
