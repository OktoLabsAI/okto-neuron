"""Public Vault open shim over the Ladybug-backed store opener."""

from __future__ import annotations

from os import PathLike
from pathlib import Path

from okto_neuron.store.vault import _open_vault as _store_open_vault


def _open_vault(path: str | PathLike[str]):
    return _store_open_vault(Path(path).expanduser().resolve(strict=False))


__all__ = ["_open_vault"]
