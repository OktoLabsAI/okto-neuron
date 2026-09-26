from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
import pytest

from okto_neuron.cli import app
from okto_neuron.config._vault import DEFAULT_NEW_VAULT_BACKEND
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture(autouse=True)
def close_vault_handles() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def test_vault_create_list_and_use_named_vaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    runner = CliRunner()

    created = runner.invoke(app, ["vault", "create", "alpha", "--embedder", "stub"])
    listed = runner.invoke(app, ["vault", "list", "--json"])
    current = runner.invoke(app, ["vault", "current"])

    vault_path = home / ".okto-neuron" / "vaults" / "alpha"
    assert created.exit_code == 0, created.output
    assert (vault_path / "okto-neuron.yaml").is_file()
    assert (vault_path / ".marginalia" / "managed-vault.json").is_file()
    assert listed.exit_code == 0, listed.output
    body = json.loads(listed.output)
    assert body["current"]["name"] == "alpha"
    assert len(body["vaults"]) == 1
    assert body["vaults"][0] == body["current"]
    assert body["vaults"][0]["name"] == "alpha"
    assert body["vaults"][0]["path"] == str(vault_path.resolve(strict=False))
    assert body["vaults"][0]["current"] is True
    assert body["vaults"][0]["managed"] is True
    assert body["vaults"][0]["deletable"] is True
    assert body["vaults"][0]["delete_reason"] is None
    assert body["vaults"][0]["backend"] == DEFAULT_NEW_VAULT_BACKEND
    assert current.exit_code == 0, current.output
    assert current.output.strip() == str(vault_path.resolve(strict=False))


def test_vault_list_plain_text_shows_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-94 follow-up: ``vault list``'s human-readable output (no ``--json``)
    shows each vault's pinned graph backend, not just the JSON payload."""
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    runner = CliRunner()

    created = runner.invoke(
        app, ["vault", "create", "beta", "--embedder", "stub", "--backend", "ladybug"]
    )
    assert created.exit_code == 0, created.output

    listed = runner.invoke(app, ["vault", "list"])
    assert listed.exit_code == 0, listed.output
    assert "[ladybug]" in listed.output
    assert "beta" in listed.output


def test_vault_use_rejects_missing_named_vault(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    result = CliRunner().invoke(app, ["vault", "use", "missing"])

    assert result.exit_code == 2
    assert "vault not found" in result.output


def test_vault_use_reports_duplicate_name_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    roots = [home / "primary-vaults", home / "secondary-vaults"]
    for root in roots:
        target = root / "duplicate"
        target.mkdir(parents=True)
        (target / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    config_path = home / ".okto-neuron" / "okto-neuron.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "marginalia_toml_version = 1\n"
        + f"vault_roots = {json.dumps([str(root) for root in roots])}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.delenv("OKTO_NEURON_VAULT", raising=False)

    result = CliRunner().invoke(app, ["vault", "use", "duplicate"])

    assert result.exit_code == 2
    assert "Error: multiple registered vaults are named 'duplicate'" in result.output
    assert "use an absolute path" in result.output
    assert "Traceback" not in result.output


def test_dev_command_help_is_available() -> None:
    result = CliRunner().invoke(app, ["dev", "--help"])

    assert result.exit_code == 0, result.output
    assert "Run a local dev server" in result.output
    assert "--no-build" in result.output


def test_dev_command_reports_invalid_vault_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    result = CliRunner().invoke(app, ["dev", "--vault", "_bad", "--no-build"])

    assert result.exit_code == 1
    assert "vault name must start" in result.output
    assert "Traceback" not in result.output
