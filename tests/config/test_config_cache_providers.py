"""``VaultConfig.load`` cache key covers ``providers.yaml`` (refs #14).

A vault yaml with ``llm.defaults.provider_ref`` is validated against the provider
registry, so a registry edit must invalidate the cached load immediately. The
file is part of every vault's key (as ``defaults.yaml`` is), referenced or not.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from okto_neuron.config import _vault as vault_mod
from okto_neuron.config._vault import VaultConfig, clear_config_cache
from okto_neuron.errors import ConfigParseError
from okto_neuron.providers import ProviderRegistry


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_config_cache()
    yield
    clear_config_cache()


class Counters:
    def __init__(self) -> None:
        self.yaml_loads = 0

    def reset(self) -> None:
        self.yaml_loads = 0


@pytest.fixture
def counters(monkeypatch: pytest.MonkeyPatch) -> Counters:
    counts = Counters()
    real_load = vault_mod._safe_load

    def spy_load(stream):
        counts.yaml_loads += 1
        return real_load(stream)

    monkeypatch.setattr(vault_mod, "_safe_load", spy_load)
    return counts


def _registry_with_profile(api_base: str = "http://127.0.0.1:4000/v1") -> str:
    registry = ProviderRegistry.load()  # scratch HOME from conftest
    profile = registry.add_provider(
        name="Proxy",
        driver="litellm_proxy",
        api_base=api_base,
        credential_id=None,
        parameter_mode="safe",
    )
    registry.save()
    return profile.id


def _write_vault(root: Path, provider_ref: str | None) -> Path:
    vault = root / "v"
    vault.mkdir(parents=True, exist_ok=True)
    body: dict[str, object] = {"marginalia_yaml_version": 2, "federation_opt_in": False}
    if provider_ref is not None:
        body["llm"] = {"defaults": {"provider_ref": provider_ref, "model": "m"}}
    (vault / "okto-neuron.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    return vault


def _providers_path() -> Path:
    return ProviderRegistry.path_for_user()


def test_providers_yaml_content_change_reloads_exactly_once(tmp_path, counters) -> None:
    ref = _registry_with_profile()
    vault = _write_vault(tmp_path, ref)
    VaultConfig.load(vault)
    counters.reset()
    VaultConfig.load(vault)
    assert counters.yaml_loads == 0

    registry = ProviderRegistry.load()
    registry.update_provider(ref, {"request_timeout_s": 123.0})
    registry.save()

    VaultConfig.load(vault)
    assert counters.yaml_loads == 1
    VaultConfig.load(vault)
    assert counters.yaml_loads == 1


def test_providers_yaml_same_size_same_mtime_rewrite_reloads(tmp_path, counters) -> None:
    ref = _registry_with_profile("http://127.0.0.1:4000/v1")
    vault = _write_vault(tmp_path, ref)
    path = _providers_path()
    before = path.stat()
    assert VaultConfig.load(vault).llm.resolved_defaults().api_base == "http://127.0.0.1:4000/v1"
    counters.reset()

    path.write_text(
        path.read_text(encoding="utf-8").replace("127.0.0.1:4000", "127.0.0.1:4001"),
        encoding="utf-8",
    )
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)

    loaded = VaultConfig.load(vault)
    assert counters.yaml_loads == 1
    assert loaded.llm.resolved_defaults().api_base == "http://127.0.0.1:4001/v1"


def test_creating_and_deleting_providers_yaml_invalidates(tmp_path, counters) -> None:
    vault = _write_vault(tmp_path, None)
    path = _providers_path()
    assert not path.exists()
    VaultConfig.load(vault)
    counters.reset()
    VaultConfig.load(vault)
    assert counters.yaml_loads == 0

    _registry_with_profile()  # creates the file
    VaultConfig.load(vault)
    assert counters.yaml_loads == 1
    VaultConfig.load(vault)
    assert counters.yaml_loads == 1

    # Deleting returns to the "absent" key already cached for the no-ref vault, so
    # that is legitimately a hit. A vault that names the (now missing) profile
    # must see the deletion immediately.
    ref = ProviderRegistry.load().document.providers[0].id
    ref_vault = _write_vault(tmp_path / "ref", ref)
    VaultConfig.load(ref_vault)
    path.unlink()
    with pytest.raises(ConfigParseError):
        VaultConfig.load(ref_vault)


def test_vault_without_provider_ref_caches_and_reloads_on_registry_change(
    tmp_path, counters
) -> None:
    ref = _registry_with_profile()
    vault = _write_vault(tmp_path, None)
    VaultConfig.load(vault)
    counters.reset()
    VaultConfig.load(vault)
    assert counters.yaml_loads == 0  # untouched providers.yaml: cache hit

    registry = ProviderRegistry.load()
    registry.update_provider(ref, {"request_timeout_s": 5.0})
    registry.save()
    VaultConfig.load(vault)  # documented: always part of the key, referenced or not
    assert counters.yaml_loads == 1


def test_registry_edit_that_invalidates_the_ref_raises_like_an_uncached_load(tmp_path) -> None:
    ref = _registry_with_profile()
    vault = _write_vault(tmp_path, ref)
    VaultConfig.load(vault)  # cached

    registry = ProviderRegistry.load()
    registry.remove_provider(ref)
    registry.save()

    with pytest.raises(Exception) as cached_path:
        VaultConfig.load(vault)
    clear_config_cache()
    with pytest.raises(Exception) as uncached_path:
        VaultConfig.load(vault)
    assert type(cached_path.value) is type(uncached_path.value)
    assert str(cached_path.value) == str(uncached_path.value)
    assert isinstance(cached_path.value, ConfigParseError)
    assert ref in str(cached_path.value.__cause__)
