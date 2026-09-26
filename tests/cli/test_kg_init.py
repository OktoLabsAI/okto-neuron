from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
import pytest
import yaml

from okto_neuron.cli import app
import okto_neuron.cli.kg as kg_module
from okto_neuron.config._vault import VaultConfig
from okto_neuron.errors import VaultPathNotADirectory
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture(autouse=True)
def close_vault_handles() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def test_kg_init_first_call_scaffolds_and_prints_layout(tmp_path: Path) -> None:
    vault_path = tmp_path / "vaults" / "alpha"

    result = CliRunner().invoke(app, ["kg", "init", str(vault_path)])

    assert result.exit_code == 0, result.output
    assert (vault_path / "notes").is_dir()
    assert (vault_path / "refs").is_dir()
    assert (vault_path / ".marginalia").is_dir()
    config_path = vault_path / "okto-neuron.yaml"
    assert config_path.is_file()
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == (
        VaultConfig.default().model_dump(mode="json", exclude_none=True)
    )
    _assert_layout(result.output, vault_path)


def test_kg_init_second_call_is_idempotent_noop(tmp_path: Path) -> None:
    vault_path = tmp_path / "vaults" / "alpha"
    runner = CliRunner()

    first = runner.invoke(app, ["kg", "init", str(vault_path)])
    config_before = (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8")
    second = runner.invoke(app, ["kg", "init", str(vault_path)])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert (vault_path / "okto-neuron.yaml").read_text(encoding="utf-8") == config_before
    assert second.output == first.output
    _assert_layout(second.output, vault_path)


def test_kg_init_vault_path_not_directory_exits_2_with_user_message(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault-file"
    vault_path.write_text("not a directory", encoding="utf-8")

    result = CliRunner().invoke(app, ["kg", "init", str(vault_path)])

    assert result.exit_code == 2
    stderr = result.stderr or result.output
    assert "VaultPathNotADirectory" in stderr
    assert "vault path is a file, not a directory" in stderr
    assert f"vault: {vault_path.resolve(strict=False)}" in stderr


def test_kg_init_debug_prints_chained_cause_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_path = tmp_path / "vault"

    def raise_typed_error(path: Path) -> object:
        try:
            raise RuntimeError("low-level bootstrap failure")
        except RuntimeError as exc:
            raise VaultPathNotADirectory(path, cause=exc) from exc

    monkeypatch.setattr(kg_module, "_open_vault", raise_typed_error)

    result = CliRunner().invoke(app, ["--debug", "kg", "init", str(vault_path)])

    assert result.exit_code == 2
    stderr = result.stderr or result.output
    assert "VaultPathNotADirectory" in stderr
    assert "Chained cause traceback:" in stderr
    assert "Traceback" in stderr
    assert "RuntimeError: low-level bootstrap failure" in stderr


def _assert_layout(output: str, vault_path: Path) -> None:
    assert f"{vault_path.resolve(strict=False)}/" in output
    for entry in ("notes/", "refs/", ".marginalia/", "okto-neuron.yaml"):
        assert entry in output
