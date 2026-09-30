"""Grafx buffer-pool budget: default computation, config, connect kwarg, status field."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

pytest.importorskip("okto_grafx")

from starlette.testclient import TestClient  # noqa: E402

from okto_neuron.config import VaultConfig  # noqa: E402
from okto_neuron.config._vault import GrafxStorageConfig, parse_byte_size  # noqa: E402
from okto_neuron.server.http import build_rest_app  # noqa: E402
from okto_neuron.server.state import init_state, reset_state_for_tests  # noqa: E402
from okto_neuron.store import grafx as grafx_mod  # noqa: E402
from okto_neuron.store.grafx import GrafxStore, default_buffer_budget  # noqa: E402

MIB = 1024**2


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (0, 256 * MIB),
        (10 * MIB, 256 * MIB),
        (400_000_000, 600_000_000),
        (2_000_000_000, 1024**3),
    ],
)
def test_default_budget(size: int, expected: int) -> None:
    assert default_buffer_budget(size) == expected


def test_size_parsing_and_validation() -> None:
    assert parse_byte_size("256MiB") == 256 * MIB
    assert parse_byte_size("1GiB") == 1024**3
    assert parse_byte_size(1000) == 1000
    assert GrafxStorageConfig(backend="grafx", buffer_budget="512MiB").buffer_budget == 512 * MIB
    assert GrafxStorageConfig(backend="grafx").buffer_budget is None
    for bad in ("lots", "1TB", 0, -5, "1MiB", "9GiB", True):
        with pytest.raises(ValidationError):
            GrafxStorageConfig(backend="grafx", buffer_budget=bad)


def _write_vault_yaml(vault: Path, storage: dict, *, inherit: bool = False) -> None:
    data = {"marginalia_yaml_version": 1, "storage": storage}
    if inherit:
        data["inherits_application_defaults"] = True
    (vault / "okto-neuron.yaml").write_text(yaml.safe_dump(data))


def test_vault_yaml_and_defaults_inheritance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    (home / ".okto-neuron").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OKTO_NEURON_HOME", str(home / ".okto-neuron"))
    vault = tmp_path / "v"
    vault.mkdir()
    _write_vault_yaml(vault, {"backend": "grafx", "buffer_budget": "300MiB"})
    assert VaultConfig.load(vault).storage.buffer_budget == 300 * MIB

    defaults = VaultConfig.application_defaults_path()
    defaults.write_text(
        yaml.safe_dump(
            {
                "marginalia_yaml_version": 1,
                "storage": {"backend": "grafx", "buffer_budget": "512MiB"},
            }
        )
    )
    _write_vault_yaml(vault, {"backend": "grafx"}, inherit=True)
    assert VaultConfig.load(vault).storage.buffer_budget == 512 * MIB
    _write_vault_yaml(vault, {"backend": "grafx", "buffer_budget": "64MiB"}, inherit=True)
    assert VaultConfig.load(vault).storage.buffer_budget == 64 * MIB


def _spy_connect(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []
    real = grafx_mod.grafx.connect

    def spy(path, **kwargs):
        calls.append(dict(kwargs))
        return real(path, **kwargs)

    monkeypatch.setattr(grafx_mod.grafx, "connect", spy)
    return calls


def test_default_reaches_connect_and_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = _spy_connect(monkeypatch)
    vault = tmp_path / "v"
    vault.mkdir()
    with caplog.at_level(logging.INFO, logger="okto_neuron.store.grafx"):
        store = GrafxStore(vault, embedding_dim=8)
    try:
        assert calls[0]["buffer_budget_bytes"] == 256 * MIB
        assert store.buffer_budget_bytes == 256 * MIB
        assert store.buffer_budget_source == "default"
        assert any(
            "buffer_budget_bytes=268435456" in r.getMessage() and "source=default" in r.getMessage()
            for r in caplog.records
        )
    finally:
        store.close()


def test_configured_budget_reaches_connect_and_real_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_connect(monkeypatch)
    vault = tmp_path / "v"
    vault.mkdir()
    cfg = GrafxStorageConfig(backend="grafx", buffer_budget="32MiB")
    store = GrafxStore(vault, config=cfg, embedding_dim=8)
    try:
        assert calls[0]["buffer_budget_bytes"] == 32 * MIB
        assert store.buffer_budget_source == "config"
        # Real-store smoke: the opened database's own config holds the budget.
        assert store._db.pool.budget_bytes == 32 * MIB
    finally:
        store.close()


def test_staged_open_without_config_reads_vault_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    calls = _spy_connect(monkeypatch)
    vault = tmp_path / "v"
    vault.mkdir()
    _write_vault_yaml(vault, {"backend": "grafx", "buffer_budget": "48MiB"})
    staged = vault / "graph.rebuild.grafx"
    store = GrafxStore(staged, embedding_dim=8)
    try:
        assert calls[0]["buffer_budget_bytes"] == 48 * MIB
    finally:
        store.close()


def test_status_payload_exposes_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from okto_neuron.vault import Vault

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    vault_dir = tmp_path / "v"
    vault_dir.mkdir()
    cfg = GrafxStorageConfig(backend="grafx", buffer_budget="40MiB")
    store = GrafxStore(vault_dir, config=cfg, embedding_dim=8)
    vault = Vault(vault_dir, store)
    reset_state_for_tests()
    state = init_state(vault, vault_dir)
    client = TestClient(build_rest_app(state), base_url="http://127.0.0.1")
    try:
        body = client.get("/api/v1/status").json()
    finally:
        client.close()
        store.close()
        reset_state_for_tests()
    assert [v["grafx_buffer_budget_bytes"] for v in body["vaults"]] == [40 * MIB]
