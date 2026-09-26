from __future__ import annotations

from pathlib import Path
import json
import os

import pytest

from okto_neuron.config import OktoNeuronConfig
from okto_neuron.vault_registry import (
    AmbiguousVaultNameError,
    ensure_global_layout,
    list_vaults,
    managed_vault_delete_guard,
    mark_managed_vault,
    read_managed_vault_marker,
    resolve_vault_backend,
    resolve_vault_reference,
    set_default_vault,
    vault_path_for_name,
)


def _mark_vault(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")


def test_global_defaults_live_under_dot_marginalia(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    cfg = OktoNeuronConfig.load()

    assert cfg.vault_roots == [(home / ".okto-neuron" / "vaults").resolve()]
    assert vault_path_for_name("alpha", cfg) == home / ".okto-neuron" / "vaults" / "alpha"


def test_list_and_resolve_named_vaults_from_configured_roots(tmp_path: Path) -> None:
    root = tmp_path / "vaults"
    alpha = root / "alpha"
    beta = root / "beta"
    _mark_vault(alpha)
    _mark_vault(beta)
    cfg = OktoNeuronConfig(vault_roots=[root], default_vault=beta)

    entries = list_vaults(cfg, current=beta)

    assert [entry.name for entry in entries] == ["alpha", "beta"]
    assert [entry.current for entry in entries] == [False, True]
    assert resolve_vault_reference("alpha", config=cfg) == alpha.resolve(strict=False)
    assert resolve_vault_reference(None, config=cfg) == beta.resolve(strict=False)


def test_list_vaults_surfaces_pinned_backend_with_legacy_absent_key_default(
    tmp_path: Path,
) -> None:
    """D-94 follow-up: ``VaultEntry.backend``/``to_json()["backend"]`` reflects
    each vault's pinned ``storage.backend``, and a vault with no ``storage``
    key at all (the legacy shape ``_mark_vault`` writes here) resolves to the
    same ``"ladybug"`` default ``store/vault.py``'s own
    ``_read_pinned_backends`` resolver uses -- exercised directly via
    ``resolve_vault_backend`` too, since that is the shared resolver REST's
    ``_vault_entries`` fallback branch and ``okto-neuron status`` also call."""
    root = tmp_path / "vaults"
    legacy = root / "legacy"
    _mark_vault(legacy)
    pinned = root / "pinned"
    pinned.mkdir(parents=True)
    (pinned / "okto-neuron.yaml").write_text(
        "marginalia_yaml_version: 1\nstorage:\n  backend: grafx\n  reason: null\n",
        encoding="utf-8",
    )
    cfg = OktoNeuronConfig(vault_roots=[root])

    entries = {entry.name: entry for entry in list_vaults(cfg)}

    assert entries["legacy"].backend == "ladybug"
    assert entries["legacy"].to_json()["backend"] == "ladybug"
    assert entries["pinned"].backend == "grafx"
    assert entries["pinned"].to_json()["backend"] == "grafx"
    assert resolve_vault_backend(legacy) == "ladybug"
    assert resolve_vault_backend(pinned) == "grafx"


def test_resolve_vault_backend_never_raises_on_a_missing_or_broken_config(
    tmp_path: Path,
) -> None:
    """A status/listing surface may hold a ``vault_path`` that is not a real,
    fully-scaffolded vault (an in-memory stub state in a test, or a vault
    mid-deletion) -- ``resolve_vault_backend`` degrades to the same
    ``"ladybug"`` fallback instead of propagating ``ConfigNotFound`` or a
    YAML parse error and breaking the surrounding endpoint."""
    missing = tmp_path / "not-a-vault"
    assert resolve_vault_backend(missing) == "ladybug"

    broken = tmp_path / "broken-vault"
    broken.mkdir(parents=True)
    (broken / "okto-neuron.yaml").write_text("not: [valid, yaml", encoding="utf-8")
    assert resolve_vault_backend(broken) == "ladybug"


def test_resolve_named_vault_rejects_duplicates_across_configured_roots(
    tmp_path: Path,
) -> None:
    roots = [tmp_path / "primary", tmp_path / "secondary"]
    for root in roots:
        _mark_vault(root / "duplicate")
    cfg = OktoNeuronConfig(vault_roots=roots)

    with pytest.raises(AmbiguousVaultNameError, match="multiple registered vaults"):
        resolve_vault_reference("duplicate", config=cfg)


def test_set_default_vault_creates_global_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    vault = home / ".okto-neuron" / "vaults" / "alpha"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)

    ensure_global_layout()
    set_default_vault(vault)

    config_path = home / ".okto-neuron" / "okto-neuron.toml"
    text = config_path.read_text(encoding="utf-8")
    assert 'vault_roots = ["' in text
    assert f'default_vault = "{vault}"' in text


def test_configured_root_vaults_are_deletable_with_or_without_marker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vaults"
    managed = root / "managed"
    legacy = root / "legacy"
    external = tmp_path / "external"
    _mark_vault(managed)
    _mark_vault(legacy)
    _mark_vault(external)
    marker = mark_managed_vault(managed, name="managed")
    cfg = OktoNeuronConfig(vault_roots=[root], default_vault=external)

    entries = {entry.name: entry for entry in list_vaults(cfg)}

    assert entries["managed"].id == marker.id
    assert entries["managed"].managed is True
    assert entries["managed"].deletable is True
    assert entries["managed"].delete_reason is None
    assert entries["legacy"].id.startswith("managed-")
    assert entries["legacy"].managed is True
    assert entries["legacy"].deletable is True
    assert entries["legacy"].delete_reason is None
    assert entries["external"].managed is False
    assert entries["external"].deletable is False
    assert "configured vault root" in str(entries["external"].delete_reason)
    assert read_managed_vault_marker(managed) == marker


def test_managed_delete_guard_uses_root_boundary_and_rejects_unsafe_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "vaults"
    target = root / "target"
    _mark_vault(target)
    marker = mark_managed_vault(target)
    cfg = OktoNeuronConfig(vault_roots=[root])
    assert managed_vault_delete_guard(target, cfg) == (marker, None)

    marker_path = target / ".marginalia" / "managed-vault.json"
    payload = json.loads(marker_path.read_text(encoding="utf-8"))
    payload["name"] = "different"
    marker_path.write_text(json.dumps(payload), encoding="utf-8")
    legacy_identity, reason = managed_vault_delete_guard(target, cfg)
    assert reason is None
    assert legacy_identity is not None
    assert legacy_identity.id.startswith("managed-")
    assert legacy_identity.id != marker.id

    nested = target / "nested"
    _mark_vault(nested)
    mark_managed_vault(nested)
    assert "direct child" in str(managed_vault_delete_guard(nested, cfg)[1])

    link = root / "linked"
    os.symlink(target, link)
    assert managed_vault_delete_guard(link, cfg)[1] == "vault path is a symlink"

    secondary = tmp_path / "secondary" / "legacy"
    _mark_vault(secondary)
    secondary_identity, reason = managed_vault_delete_guard(
        secondary,
        OktoNeuronConfig(vault_roots=[root, secondary.parent]),
    )
    assert reason is None
    assert secondary_identity is not None
    assert secondary_identity.id.startswith("managed-")


def test_managed_delete_guard_rejects_a_symlinked_managed_root(tmp_path: Path) -> None:
    actual_root = tmp_path / "actual-vaults"
    linked_root = tmp_path / "linked-vaults"
    managed = actual_root / "managed"
    _mark_vault(managed)
    mark_managed_vault(managed)
    os.symlink(actual_root, linked_root)

    marker, reason = managed_vault_delete_guard(
        linked_root / "managed",
        OktoNeuronConfig(vault_roots=[linked_root]),
    )

    assert marker is None
    assert reason == "configured vault root is a symlink"
