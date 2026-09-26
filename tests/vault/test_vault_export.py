from __future__ import annotations

import builtins
import importlib.util
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.core.schema import Node
from okto_neuron.errors import ExportError
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture(autouse=True)
def close_vault_handles() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def test_jsonld_export_missing_extra_is_actionable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault.init(tmp_path / "v")
    real_import = builtins.__import__

    def import_without_rdflib(name: str, *args: object, **kwargs: object) -> object:
        if name == "rdflib":
            raise ModuleNotFoundError("No module named 'rdflib'", name="rdflib")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_rdflib)
    try:
        with pytest.raises(ExportError, match=r"install okto-neuron\[jsonld\]"):
            vault.export(format="jsonld")
    finally:
        vault.close()


@pytest.mark.skipif(
    importlib.util.find_spec("rdflib") is None,
    reason="rdflib is required for JSON-LD export",
)
def test_ts_2b365fbc_jsonld_export_is_byte_deterministic(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    vault.store.add_node(Node(id="b", type="Document", title="Second", facets={"rank": 2}))
    vault.store.add_node(Node(id="a", type="Document", title="First", facets={"rank": 1}))

    try:
        first = vault.export(format="jsonld")
        second = vault.export(format="jsonld")

        assert isinstance(first, str)
        assert second == first
    finally:
        vault.close()
