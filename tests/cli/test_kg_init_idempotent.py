from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
import pytest

from okto_neuron.cli import app
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture(autouse=True)
def close_vault_handles() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def test_kg_init_is_idempotent_and_prints_scaffolded_layout(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    runner = CliRunner()

    first = runner.invoke(app, ["kg", "init", str(vault_path)])
    second = runner.invoke(app, ["kg", "init", str(vault_path)])

    assert first.exit_code == 0, first.output
    assert first.stderr == ""
    assert _layout_entries(first.output) >= {
        "notes/",
        "refs/",
        ".marginalia/",
        "okto-neuron.yaml",
    }

    assert second.exit_code == 0, second.output
    assert second.stderr == ""
    assert _layout_entries(second.output) >= {
        "notes/",
        "refs/",
        ".marginalia/",
        "okto-neuron.yaml",
    }


def _layout_entries(output: str) -> set[str]:
    return {line.strip() for line in output.splitlines() if line.strip()}
