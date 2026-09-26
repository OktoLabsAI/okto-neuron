from __future__ import annotations

from pathlib import Path
import tomllib

import pytest
import yaml

from okto_neuron.config import OktoNeuronConfig, VaultConfig
from okto_neuron.errors import ConfigParseError


def test_malformed_toml_surfaces_config_parse_error_with_file_line_and_cause(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "okto-neuron.toml"
    config_path.write_text(
        "marginalia_toml_version = 1\nbad = \n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigParseError) as raised:
        OktoNeuronConfig.load(config_path)

    error = raised.value
    resolved = config_path.resolve()

    assert isinstance(error, ConfigParseError)
    assert error.file_path == resolved
    assert isinstance(error.line, int)
    assert error.line == 2
    assert isinstance(error.__cause__, tomllib.TOMLDecodeError)

    message = error.user_message()
    assert str(resolved) in message
    assert str(error.line) in message


def test_malformed_yaml_surfaces_config_parse_error_with_file_line_and_cause(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    config_path = vault_path / "okto-neuron.yaml"
    config_path.write_text(
        "marginalia_yaml_version: 1\nbad:\n\tchild: value\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigParseError) as raised:
        VaultConfig.load(vault_path)

    error = raised.value
    resolved = config_path.resolve()

    assert isinstance(error, ConfigParseError)
    assert error.file_path == resolved
    assert isinstance(error.line, int)
    assert error.line == 3
    assert isinstance(error.__cause__, yaml.YAMLError)

    message = error.user_message()
    assert str(resolved) in message
    assert str(error.line) in message
