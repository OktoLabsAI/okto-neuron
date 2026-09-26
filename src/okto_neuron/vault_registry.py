"""Vault discovery and selection helpers.

The registry is intentionally file-system based. A vault is a directory with a
``okto-neuron.yaml`` file; the global ``okto-neuron.toml`` only records discovery
roots and the default vault. This keeps existing path-based vaults working while
making ``~/.okto-neuron/vaults/<name>`` the default home for new named vaults
(``~/.marginalia/vaults/<name>`` on a machine upgraded from a pre-0.3.0 install).
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from os import PathLike
from pathlib import Path
import re
import tempfile
import uuid

from okto_neuron._compat import getenv as _compat_getenv
from okto_neuron._compat import default_vault_roots, vault_config_path
from okto_neuron.config import OktoNeuronConfig
from okto_neuron.config._app_config import _default_config_path, _select_config_path

_VAULT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# NAME_MAX: the maximum length of a single path component on every filesystem we
# target. The vault name is used verbatim as one component under the vault root
# and is never embedded in a longer filename, so 255 is the exact boundary.
_VAULT_NAME_MAX = 255
_MANAGED_MARKER = Path(".marginalia") / "managed-vault.json"
_MANAGED_MARKER_VERSION = 1


class AmbiguousVaultNameError(ValueError):
    """A registered name resolves to more than one configured vault root."""

    def __init__(self, name: str, paths: list[Path]) -> None:
        self.name = name
        self.paths = tuple(paths)
        super().__init__(f"multiple registered vaults are named {name!r}; use an absolute path")


@dataclass(frozen=True)
class VaultEntry:
    name: str
    path: Path
    current: bool = False
    id: str = ""
    managed: bool = False
    deletable: bool = False
    delete_reason: str | None = None
    # Pinned ``storage.backend`` from ``marginalia.yaml`` (legacy absent-key
    # rule: "ladybug") -- see ``resolve_vault_backend`` below, which reuses
    # ``store/vault.py``'s own ``_read_pinned_backends`` resolver rather than
    # re-deriving the absent-key default here.
    backend: str = "ladybug"

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id or _external_vault_id(self.path),
            "name": self.name,
            "path": str(self.path),
            "current": self.current,
            "managed": self.managed,
            "deletable": self.deletable,
            "delete_reason": self.delete_reason,
            "backend": self.backend,
        }


@dataclass(frozen=True)
class ManagedVaultMarker:
    """Stable identity for a vault inside a configured vault root.

    New vaults persist this identity in ``managed-vault.json``. Legacy direct
    children derive the same shape deterministically from their resolved path.
    """

    id: str
    name: str

    def to_json(self) -> dict[str, object]:
        return {
            "version": _MANAGED_MARKER_VERSION,
            "id": self.id,
            "name": self.name,
        }


def default_vault_root(config: OktoNeuronConfig | None = None) -> Path:
    cfg = config or OktoNeuronConfig.load()
    if cfg.vault_roots:
        return cfg.vault_roots[0]
    return default_vault_roots()[0]


def vault_path_for_name(name: str, config: OktoNeuronConfig | None = None) -> Path:
    _validate_name(name)
    return default_vault_root(config) / name


def list_vaults(
    config: OktoNeuronConfig | None = None,
    *,
    current: Path | None = None,
) -> list[VaultEntry]:
    cfg = config or OktoNeuronConfig.load()
    current_path = current.resolve(strict=False) if current is not None else None
    entries: dict[Path, VaultEntry] = {}

    for root in cfg.vault_roots:
        try:
            children = sorted(root.iterdir(), key=lambda path: path.name.lower())
        except OSError:
            continue
        for child in children:
            if _is_vault(child):
                resolved = child.resolve(strict=False)
                entries[resolved] = _vault_entry(
                    child.name, child, current=resolved == current_path, config=cfg
                )

    for extra in (cfg.default_vault, current_path):
        if extra is None:
            continue
        if _is_vault(extra):
            resolved = extra.resolve(strict=False)
            entries[resolved] = _vault_entry(
                resolved.name, resolved, current=resolved == current_path, config=cfg
            )

    return sorted(entries.values(), key=lambda entry: entry.name.lower())


def resolve_vault_reference(
    ref: str | PathLike[str] | None = None,
    *,
    config: OktoNeuronConfig | None = None,
) -> Path:
    """Resolve a CLI/API vault reference to a path.

    Precedence for ``None`` mirrors the historical CLI behavior where possible:
    ``OKTO_NEURON_VAULT`` first, then configured default, then current directory
    if it is itself a vault. If no vault is configured, the conventional default
    path under the default vault root (``<root>/default``) is returned; callers that require
    an existing vault should validate ``okto-neuron.yaml`` before opening.
    """
    cfg = config or OktoNeuronConfig.load()
    if ref is None:
        env_path = _compat_getenv("OKTO_NEURON_VAULT")
        if env_path:
            return resolve_vault_reference(env_path, config=cfg)
        if cfg.default_vault is not None:
            return cfg.default_vault.resolve(strict=False)
        cwd = Path.cwd()
        if _is_vault(cwd):
            return cwd.resolve(strict=False)
        discovered = list_vaults(cfg)
        if len(discovered) == 1:
            return discovered[0].path
        return vault_path_for_name("default", cfg).resolve(strict=False)

    raw = os.fspath(ref)
    if _looks_like_path(raw):
        return Path(raw).expanduser().resolve(strict=False)

    matches = [entry.path for entry in list_vaults(cfg) if entry.name == raw]
    if len(matches) > 1:
        raise AmbiguousVaultNameError(raw, matches)
    if matches:
        return matches[0]
    return vault_path_for_name(raw, cfg).resolve(strict=False)


def ensure_global_layout(config: OktoNeuronConfig | None = None) -> None:
    cfg = config or OktoNeuronConfig.load()
    config_path = _config_path_for_write()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(config_path.parent, 0o700)
    for root in cfg.vault_roots:
        root.mkdir(parents=True, exist_ok=True)
        os.chmod(root, 0o700)
    if not config_path.exists():
        _write_default_config(config_path, cfg)


def set_default_vault(path: Path | str) -> Path:
    resolved = Path(path).expanduser().resolve(strict=False)
    config_path = _config_path_for_write()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(config_path.parent, 0o700)
    if not config_path.exists():
        cfg = OktoNeuronConfig.load()
        _write_default_config(config_path, cfg, default_vault=resolved)
        return resolved

    line = f'default_vault = "{_toml_escape(str(resolved))}"\n'
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    for index, existing in enumerate(lines):
        if existing.lstrip().startswith("default_vault"):
            lines[index] = line
            break
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(line)
    config_path.write_text("".join(lines), encoding="utf-8")
    return resolved


def clear_default_vault(path: Path | str | None = None) -> bool:
    """Remove the configured default, optionally only when it matches ``path``."""

    config_path = _config_path_for_write()
    if not config_path.exists():
        return False
    expected = Path(path).expanduser().resolve(strict=False) if path is not None else None
    if expected is not None:
        configured = OktoNeuronConfig.load(config_path).default_vault
        if configured is None or configured.resolve(strict=False) != expected:
            return False
    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not line.lstrip().startswith("default_vault")]
    if kept == lines:
        return False
    config_path.write_text("".join(kept), encoding="utf-8")
    return True


def mark_managed_vault(path: Path | str, *, name: str | None = None) -> ManagedVaultMarker:
    """Mark a freshly created named vault as application-owned.

    The marker is intentionally never inferred for an existing directory. That
    makes deletion opt-in by construction: legacy and externally discovered
    vaults remain visible but cannot be removed by the application.
    """

    resolved = Path(path).expanduser().resolve(strict=False)
    marker_name = name or resolved.name
    _validate_name(marker_name)
    if marker_name != resolved.name:
        raise ValueError("managed vault name must match its directory name")
    if not _is_vault(resolved):
        raise ValueError(f"cannot mark a non-vault path as managed: {resolved}")
    marker = ManagedVaultMarker(id=str(uuid.uuid4()), name=marker_name)
    marker_path = resolved / _MANAGED_MARKER
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(marker_path.parent), prefix=f"{marker_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(marker.to_json(), handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, marker_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return marker


def read_managed_vault_marker(path: Path | str) -> ManagedVaultMarker | None:
    """Return a valid ownership marker without following a marker symlink."""

    resolved = Path(path).expanduser().resolve(strict=False)
    marker_path = resolved / _MANAGED_MARKER
    try:
        if marker_path.is_symlink() or not marker_path.is_file():
            return None
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != _MANAGED_MARKER_VERSION:
        return None
    marker_id = payload.get("id")
    name = payload.get("name")
    if not isinstance(marker_id, str) or not isinstance(name, str):
        return None
    try:
        uuid.UUID(marker_id)
        _validate_name(name)
    except ValueError:
        return None
    if name != resolved.name:
        return None
    return ManagedVaultMarker(id=marker_id, name=name)


def managed_vault_delete_guard(
    path: Path | str, config: OktoNeuronConfig | None = None
) -> tuple[ManagedVaultMarker | None, str | None]:
    """Validate the configured-root boundary for whole-vault deletion."""

    cfg = config or OktoNeuronConfig.load()
    raw = Path(path).expanduser()
    if raw.is_symlink():
        return None, "vault path is a symlink"
    resolved = raw.resolve(strict=False)
    matched_root = False
    for configured_root in cfg.vault_roots:
        raw_root = Path(configured_root).expanduser()
        root = raw_root.resolve(strict=False)
        if resolved.parent != root:
            continue
        if raw_root.is_symlink() or raw.parent.is_symlink():
            return None, "configured vault root is a symlink"
        matched_root = True
        break
    if not matched_root:
        return None, "vault is not a direct child of a configured vault root"
    home = Path.home().expanduser().resolve(strict=False)
    if resolved in {home, home.parent, Path(resolved.anchor)}:
        return None, "refusing to delete a home, ancestor, or filesystem root"
    config_file = vault_config_path(resolved)
    if config_file.is_symlink() or not config_file.is_file():
        return None, "vault is missing a regular okto-neuron.yaml (or pre-0.3.0 marginalia.yaml)"
    marker = read_managed_vault_marker(resolved)
    if marker is None:
        marker = ManagedVaultMarker(id=_managed_root_vault_id(resolved), name=resolved.name)
    return marker, None


def _managed_root_vault_id(path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()
    return f"managed-{digest[:20]}"


def _external_vault_id(path: Path) -> str:
    digest = hashlib.sha256(str(path.resolve(strict=False)).encode("utf-8")).hexdigest()
    return f"external-{digest[:20]}"


def resolve_vault_backend(path: Path) -> str:
    """The vault's pinned graph backend, or ``"ladybug"`` for the legacy
    absent-``storage``-key vault. Reuses ``store/vault.py``'s own
    ``_read_pinned_backends`` (the resolver every open path already reads
    the pin through) rather than re-deriving the absent-key rule here --
    lazily imported since ``store/vault.py`` pulls in the heavier store
    stack that this otherwise-lightweight, file-system-based registry
    module does not need at import time.

    Never raises: callers include status/listing surfaces (``okto-neuron
    status``, ``GET /api/v1/status``) that read a ``ServerState.vault_path``
    which is not always a real, fully-scaffolded vault directory -- an
    in-memory stub state in a test, or a vault mid-deletion. Any failure to
    read the pin (missing/unreadable ``okto-neuron.yaml`` included) falls
    back to the same ``"ladybug"`` legacy default as a genuinely absent
    ``storage`` key, matching ``read_managed_vault_marker``'s graceful
    degradation on a corrupt marker elsewhere in this module.
    """
    from okto_neuron.store.vault import _read_pinned_backends

    try:
        graph_backend, _index_backend, _storage_config = _read_pinned_backends(path)
    except Exception:  # noqa: BLE001
        return "ladybug"
    return graph_backend


def _vault_entry(
    name: str,
    path: Path,
    *,
    current: bool,
    config: OktoNeuronConfig,
) -> VaultEntry:
    marker, reason = managed_vault_delete_guard(path, config)
    resolved = path.resolve(strict=False)
    backend = resolve_vault_backend(resolved)
    if marker is None:
        return VaultEntry(
            name=name,
            path=resolved,
            current=current,
            id=_external_vault_id(resolved),
            managed=False,
            deletable=False,
            delete_reason=reason,
            backend=backend,
        )
    return VaultEntry(
        name=name,
        path=resolved,
        current=current,
        id=marker.id,
        managed=True,
        deletable=True,
        backend=backend,
    )


def is_vault(path: Path | str) -> bool:
    return _is_vault(Path(path).expanduser())


def _is_vault(path: Path) -> bool:
    return path.is_dir() and (vault_config_path(path)).is_file()


def _validate_name(name: str) -> None:
    if not _VAULT_NAME.fullmatch(name):
        raise ValueError(
            "vault name must start with a letter or number and contain only "
            "letters, numbers, '.', '_' or '-'"
        )
    # LENGTH IS A VALIDATION RULE, NOT A FILESYSTEM ACCIDENT. ``vault_path_for_name``
    # appends the name as ONE path component under the vault root, so the binding
    # OS constraint is NAME_MAX (255) for a single component on both Linux (ext4,
    # btrfs, xfs) and macOS (APFS/HFS+). Without this check a charset-valid but
    # over-long name reached ``os.stat`` and the raw ``OSError`` — whose text is
    # the ABSOLUTE path, i.e. the operator's home directory and the internal vault
    # layout — propagated to the MCP caller. Checking characters is exactly
    # checking bytes here: ``_VAULT_NAME`` is ASCII-only (see the pattern above),
    # so any name that survives the charset rule satisfies
    # ``len(name) == len(name.encode("utf-8"))``. That also sidesteps the HFS+
    # (255 bytes) vs APFS (255 UTF-8 characters) divergence.
    if len(name) > _VAULT_NAME_MAX:
        raise ValueError(
            f"vault name must be at most {_VAULT_NAME_MAX} characters "
            f"(got {len(name)})"
        )


def _looks_like_path(raw: str) -> bool:
    return (
        raw.startswith(("~", ".", "/"))
        or os.sep in raw
        or (os.altsep is not None and os.altsep in raw)
    )


def _config_path_for_write() -> Path:
    selected = _select_config_path(None)
    if selected is not None:
        return selected
    env_path = _compat_getenv("OKTO_NEURON_CONFIG")
    if env_path:
        return Path(env_path).expanduser().resolve(strict=False)
    return _default_config_path().resolve(strict=False)


def _write_default_config(
    path: Path,
    config: OktoNeuronConfig,
    *,
    default_vault: Path | None = None,
) -> None:
    vault_roots = ", ".join(f'"{_toml_escape(str(root))}"' for root in config.vault_roots)
    lines = [
        "marginalia_toml_version = 1\n",
        f"vault_roots = [{vault_roots}]\n",
    ]
    if default_vault is not None:
        lines.append(f'default_vault = "{_toml_escape(str(default_vault))}"\n')
    path.write_text("".join(lines), encoding="utf-8")


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


__all__ = [
    "AmbiguousVaultNameError",
    "ManagedVaultMarker",
    "VaultEntry",
    "clear_default_vault",
    "default_vault_root",
    "ensure_global_layout",
    "is_vault",
    "list_vaults",
    "managed_vault_delete_guard",
    "mark_managed_vault",
    "read_managed_vault_marker",
    "resolve_vault_reference",
    "set_default_vault",
    "vault_path_for_name",
]
