from pathlib import Path

import pytest

from okto_neuron.core.schema import PackLoader, UnresolvedImportError


def test_ts13_registry_wins_over_search_path(tmp_path):
    path_dir = tmp_path / "path"
    path_dir.mkdir()
    _write_pack(path_dir / "priority.yaml", "priority", "PathThing")

    loader = PackLoader(
        registry={
            "priority": {
                "id": "priority",
                "version": "0.1.0",
                "compatRange": ">=0.0.1",
                "types": [{"name": "RegistryThing", "kind_of": "Concept"}],
            }
        },
        search_paths=(path_dir,),
    )

    assert "RegistryThing" in loader.load("priority").types


def test_ts13_search_path_wins_over_env_path(tmp_path, monkeypatch):
    path_dir = tmp_path / "path"
    env_dir = tmp_path / "env"
    path_dir.mkdir()
    env_dir.mkdir()
    _write_pack(path_dir / "order.yaml", "order", "PathThing")
    _write_pack(env_dir / "order.yaml", "order", "EnvThing")
    monkeypatch.setenv("OKTO_NEURON_PACK_PATH", str(env_dir))

    pack = PackLoader(search_paths=(path_dir,)).load("order")

    assert "PathThing" in pack.types


def test_ts13_env_path_wins_over_builtin(tmp_path, monkeypatch):
    env_dir = tmp_path / "env"
    env_dir.mkdir()
    _write_pack(env_dir / "builtin.yaml", "builtin", "EnvThing")
    monkeypatch.setenv("OKTO_NEURON_PACK_PATH", str(env_dir))

    pack = PackLoader().load("builtin")

    assert "EnvThing" in pack.types


def test_ts13_builtin_resolves_after_env_exhaustion(monkeypatch):
    monkeypatch.delenv("OKTO_NEURON_PACK_PATH", raising=False)

    pack = PackLoader().load("builtin")

    assert "BuiltinThing" in pack.types


def test_ts13_unresolved_import_on_exhaustion(monkeypatch):
    monkeypatch.delenv("OKTO_NEURON_PACK_PATH", raising=False)

    with pytest.raises(UnresolvedImportError):
        PackLoader().load("does-not-exist")


def _write_pack(path: Path, pack_id: str, type_name: str) -> None:
    path.write_text(
        f"""
id: {pack_id}
version: "0.1.0"
compatRange: ">=0.0.1"
types:
  - name: {type_name}
    kind_of: Concept
"""
    )
