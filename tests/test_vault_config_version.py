from __future__ import annotations

from pathlib import Path
import warnings

import pytest

from okto_neuron.config import VaultConfig
from okto_neuron.errors import ConfigVersionUnsupported


def test_vault_config_load_missing_yaml_version_defaults_to_v1_and_warns(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "okto-neuron.yaml"
    config_path.write_text("vault_id: local\npacks:\n  - core\n", encoding="utf-8")

    with pytest.warns(UserWarning, match="missing marginalia_yaml_version"):
        cfg = VaultConfig.load(config_path)

    assert cfg.marginalia_yaml_version == 1
    assert cfg.vault_id == "local"
    assert cfg.packs == ["core"]


def test_vault_config_load_unknown_yaml_version_raises_unsupported(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "okto-neuron.yaml"
    config_path.write_text("marginalia_yaml_version: 99\n", encoding="utf-8")

    with pytest.raises(ConfigVersionUnsupported) as raised:
        VaultConfig.load(config_path)

    error = raised.value
    assert error.EXIT_CODE == 4
    assert error.file_path == config_path.resolve()
    assert error.found_version == 99
    assert error.supported_versions == (1, 2)


def test_vault_config_load_explicit_yaml_version_one_succeeds_without_warning(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "okto-neuron.yaml"
    config_path.write_text(
        "\n".join(
            [
                "marginalia_yaml_version: 1",
                "vault_id: explicit",
                "packs:",
                "  - core",
                "",
            ]
        ),
        encoding="utf-8",
    )

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cfg = VaultConfig.load(config_path)

    assert caught == []
    assert cfg.marginalia_yaml_version == 1
    assert cfg.vault_id == "explicit"
    assert cfg.packs == ["core"]
