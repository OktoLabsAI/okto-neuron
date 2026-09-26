from __future__ import annotations

from pathlib import Path

import pytest

from okto_neuron.config import OktoNeuronConfig


_MODE_BY_LEVEL = {
    "arg": 448,
    "env": 480,
    "home": 504,
    "defaults": 0o755,
}


def _write_config(path: Path, level: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "marginalia_toml_version = 1",
                f"default_directory_mode = {_MODE_BY_LEVEL[level]}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _set_up_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured_levels: tuple[str, ...],
) -> dict[str, Path]:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    paths = {
        "arg": tmp_path / "arg.toml",
        "env": tmp_path / "env.toml",
        "home": home / ".okto-neuron" / "okto-neuron.toml",
    }

    for level in configured_levels:
        _write_config(paths[level], level)

    if "env" in configured_levels:
        monkeypatch.setenv("OKTO_NEURON_CONFIG", str(paths["env"]))

    return paths


@pytest.mark.parametrize(
    ("expected_level", "configured_levels"),
    [
        pytest.param("arg", ("home", "env", "arg"), id="arg-path"),
        pytest.param("env", ("home", "env"), id="env-var"),
        pytest.param("home", ("home",), id="home-config"),
        pytest.param("defaults", (), id="defaults"),
    ],
)
def test_marginalia_config_load_uses_highest_available_precedence_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    expected_level: str,
    configured_levels: tuple[str, ...],
) -> None:
    paths = _set_up_sources(tmp_path, monkeypatch, configured_levels)
    arg_path = paths["arg"] if "arg" in configured_levels else None

    cfg = OktoNeuronConfig.load(arg_path)

    assert cfg.default_directory_mode == _MODE_BY_LEVEL[expected_level]


def test_marginalia_config_load_arg_path_wins_when_all_sources_are_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _set_up_sources(tmp_path, monkeypatch, ("home", "env", "arg"))

    cfg = OktoNeuronConfig.load(paths["arg"])

    assert cfg.default_directory_mode == _MODE_BY_LEVEL["arg"]
