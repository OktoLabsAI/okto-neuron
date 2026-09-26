from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from typing import NamedTuple

import ladybug
import pytest
import yaml

from okto_neuron.config._vault import VaultConfig
from okto_neuron.store import vault as vault_module
from okto_neuron.store._bootstrap import _bootstrap_cache
from okto_neuron.store.ladybug import VaultConnection
from okto_neuron.store.vault import _open_vault


class PathSnapshot(NamedTuple):
    kind: str
    mode: int
    sha256: str | None
    children: tuple[str, ...]


@pytest.fixture(autouse=True)
def close_vault_handles() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()
    for handle in list(_bootstrap_cache.values()):
        handle.close()
    _bootstrap_cache.clear()


def test_second_open_vault_call_is_idempotent_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_path = tmp_path / "vaults" / "alpha"
    resolved_vault_path = vault_path.resolve(strict=False)

    first = _open_vault(vault_path)
    assert not first.is_closed
    assert resolved_vault_path in _bootstrap_cache
    first_bootstrap_handle = _bootstrap_cache[resolved_vault_path]

    _assert_scaffold_exists(vault_path)
    snapshot_after_first_open = _snapshot_vault_scaffold(vault_path)

    executed_on_second_open: list[str] = []
    original_execute = ladybug.Connection.execute

    def execute_spy(
        self: ladybug.Connection,
        statement: str,
        *args: object,
        **kwargs: object,
    ) -> object:
        executed_on_second_open.append(statement)
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(ladybug.Connection, "execute", execute_spy)

    second = _open_vault(vault_path)

    assert not second.is_closed
    assert second is first
    assert _bootstrap_cache[resolved_vault_path] is first_bootstrap_handle
    assert second._graph_handle is first_bootstrap_handle
    assert _snapshot_vault_scaffold(vault_path) == snapshot_after_first_open
    assert not _create_node_table_statements(executed_on_second_open)


def _assert_scaffold_exists(vault_path: Path) -> None:
    expected_directory_modes = {
        vault_path / "notes": 0o755,
        vault_path / "refs": 0o755,
        vault_path / ".marginalia": 0o755,
    }
    for directory, expected_mode in expected_directory_modes.items():
        assert directory.is_dir()
        assert stat.S_IMODE(directory.stat().st_mode) == expected_mode

    config_path = vault_path / "okto-neuron.yaml"
    assert config_path.is_file()
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o644
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == (
        VaultConfig.default().model_dump(mode="json", exclude_none=True)
    )


def _snapshot_vault_scaffold(vault_path: Path) -> dict[str, PathSnapshot]:
    paths = (
        vault_path / "notes",
        vault_path / "refs",
        vault_path / ".marginalia",
        vault_path / "okto-neuron.yaml",
    )
    snapshots: dict[str, PathSnapshot] = {}
    for path in paths:
        for candidate in _walk_scaffold_path(path):
            relative_path = candidate.relative_to(vault_path).as_posix()
            snapshots[relative_path] = _snapshot_path(candidate)
    return snapshots


def _walk_scaffold_path(path: Path) -> tuple[Path, ...]:
    if path.is_file():
        return (path,)
    return (path, *tuple(sorted(path.rglob("*"))))


def _snapshot_path(path: Path) -> PathSnapshot:
    mode = stat.S_IMODE(path.stat().st_mode)
    if path.is_file():
        return PathSnapshot(
            kind="file",
            mode=mode,
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            children=(),
        )
    return PathSnapshot(
        kind="dir",
        mode=mode,
        sha256=None,
        children=tuple(sorted(child.name for child in path.iterdir())),
    )


def _create_node_table_statements(statements: list[str]) -> list[str]:
    return [
        statement
        for statement in statements
        if statement.lstrip().upper().startswith("CREATE NODE TABLE")
    ]
