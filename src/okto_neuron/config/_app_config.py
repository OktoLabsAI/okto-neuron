"""Global app config loader (``okto-neuron.toml``; pre-0.3.0 ``marginalia.toml`` is read)."""

from __future__ import annotations

from pathlib import Path
import re
import tomllib
from typing import Any, Self
import warnings

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from okto_neuron import _compat
from okto_neuron._compat import getenv as _compat_getenv
from okto_neuron.errors import ConfigNotFound, ConfigParseError, ConfigVersionUnsupported

SUPPORTED_TOML_VERSIONS = (1,)
_WARNED_MISSING_VERSION: set[Path] = set()


def default_app_home() -> Path:
    """Default user-owned app home (``~/.okto-neuron``), shared by config and vault discovery.

    Pre-0.3.0 installs used ``~/.marginalia``; see :mod:`okto_neuron._compat` for how
    its files are still found and why its vaults are never moved.
    """
    return _compat.app_home()


def _default_config_path() -> Path:
    return _compat.app_file(_compat.APP_CONFIG_FILENAME, _compat.LEGACY_APP_CONFIG_FILENAME)


def _default_vault_roots() -> list[Path]:
    return _compat.default_vault_roots()


def _resolve_path(path: Path | str) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _toml_error_line(error: tomllib.TOMLDecodeError) -> int | None:
    line = getattr(error, "lineno", None)
    if line is not None:
        return line
    match = re.search(r"line (\d+)", str(error))
    if match is None:
        return None
    return int(match.group(1))


def _warn_missing_version_once(path: Path) -> None:
    resolved = _resolve_path(path)
    if resolved in _WARNED_MISSING_VERSION:
        return
    _WARNED_MISSING_VERSION.add(resolved)
    warnings.warn(
        "missing marginalia_toml_version; treating config as version 1",
        stacklevel=3,
    )


class OktoNeuronConfig(BaseModel):
    """Typed global configuration loaded from okto-neuron.toml (or a pre-0.3.0 marginalia.toml)."""

    model_config = ConfigDict(extra="allow")

    marginalia_toml_version: int = 1
    vault_roots: list[Path] = Field(default_factory=_default_vault_roots)
    default_vault: Path | None = None
    strict_acl: bool = False
    default_directory_mode: int = 0o755

    @field_validator("vault_roots", mode="after")
    @classmethod
    def _expand_vault_roots(cls, value: list[Path]) -> list[Path]:
        return [_resolve_path(path) for path in value]

    @field_validator("default_vault", mode="after")
    @classmethod
    def _expand_default_vault(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return _resolve_path(value)

    @classmethod
    def load(cls, path: Path | str | None = None) -> Self:
        """Load the first existing config in the required precedence chain."""
        selected = _select_config_path(path)
        if selected is None:
            return cls()
        return cls._load_file(selected)

    @classmethod
    def _load_file(cls, path: Path | str) -> Self:
        resolved = _resolve_path(path)
        try:
            with resolved.open("rb") as handle:
                data = tomllib.load(handle)
        except FileNotFoundError as error:
            raise ConfigNotFound(resolved, cause=error) from error
        except tomllib.TOMLDecodeError as error:
            raise ConfigParseError(resolved, line=_toml_error_line(error), cause=error) from error
        except OSError as error:
            raise ConfigParseError(resolved, cause=error) from error

        return cls._validate_data(data, resolved)

    @classmethod
    def _validate_data(cls, data: dict[str, Any], path: Path) -> Self:
        if "marginalia_toml_version" not in data:
            _warn_missing_version_once(path)

        found_version = data.get("marginalia_toml_version", 1)
        if found_version not in SUPPORTED_TOML_VERSIONS:
            raise ConfigVersionUnsupported(
                path,
                found_version,
                supported_versions=SUPPORTED_TOML_VERSIONS,
            )

        try:
            return cls.model_validate(data)
        except ValidationError as error:
            raise ConfigParseError(path, cause=error) from error


def _select_config_path(path: Path | str | None) -> Path | None:
    candidates: list[Path] = []
    if path is not None:
        candidates.append(_resolve_path(path))

    env_path = _compat_getenv("OKTO_NEURON_CONFIG")
    if env_path:
        candidates.append(_resolve_path(env_path))

    candidates.append(_default_config_path())

    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate
        except OSError as error:
            raise ConfigParseError(candidate, cause=error) from error
    return None


__all__ = ["OktoNeuronConfig", "default_app_home"]
