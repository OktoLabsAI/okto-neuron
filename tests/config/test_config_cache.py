"""``VaultConfig.load`` / ``vault_yaml_version`` stat-keyed read caches (refs #14).

``GET /api/v1/status`` and the scheduler tick used to re-read and re-validate every
vault's yaml on each call. The caches key on the file's stat tuple (and the
inherited ``defaults.yaml``'s), return deep copies, and never store a failure.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from okto_neuron.config import _vault as vault_mod
from okto_neuron.config._app_config import OktoNeuronConfig
from okto_neuron.config._vault import (
    VaultConfig,
    clear_config_cache,
    set_vault_yaml_version,
    vault_yaml_version,
)
from okto_neuron.errors import ConfigParseError, ConfigVersionUnsupported
from okto_neuron.vault_registry import list_vaults


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_config_cache()
    yield
    clear_config_cache()


class Counters:
    def __init__(self) -> None:
        self.yaml_loads = 0
        self.validations = 0

    def reset(self) -> None:
        self.yaml_loads = 0
        self.validations = 0


@pytest.fixture
def counters(monkeypatch: pytest.MonkeyPatch) -> Counters:
    counts = Counters()
    real_load = vault_mod._safe_load
    real_validate = VaultConfig.model_validate

    def spy_load(stream):
        counts.yaml_loads += 1
        return real_load(stream)

    def spy_validate(*args, **kwargs):
        counts.validations += 1
        return real_validate(*args, **kwargs)

    monkeypatch.setattr(vault_mod, "_safe_load", spy_load)
    monkeypatch.setattr(VaultConfig, "model_validate", staticmethod(spy_validate))
    return counts


def _write_vault(root: Path, name: str = "v", **extra: object) -> Path:
    vault = root / name
    vault.mkdir(parents=True, exist_ok=True)
    body = {"marginalia_yaml_version": 2, "federation_opt_in": False, **extra}
    (vault / "okto-neuron.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    return vault


def _config_file(vault: Path) -> Path:
    return vault / "okto-neuron.yaml"


def test_second_load_does_no_yaml_read_and_no_validation(tmp_path, counters) -> None:
    vault = _write_vault(tmp_path)
    first = VaultConfig.load(vault)
    assert (counters.yaml_loads, counters.validations) == (1, 1)
    counters.reset()
    second = VaultConfig.load(vault)
    assert (counters.yaml_loads, counters.validations) == (0, 0)
    assert second == first
    assert second is not first


def test_content_change_triggers_exactly_one_reload(tmp_path, counters) -> None:
    vault = _write_vault(tmp_path, packs=["core"])
    assert VaultConfig.load(vault).packs == ["core"]
    counters.reset()
    _config_file(vault).write_text(
        yaml.safe_dump({"marginalia_yaml_version": 2, "packs": ["core", "research"]}),
        encoding="utf-8",
    )
    assert VaultConfig.load(vault).packs == ["core", "research"]
    assert (counters.yaml_loads, counters.validations) == (1, 1)
    counters.reset()
    VaultConfig.load(vault)
    assert (counters.yaml_loads, counters.validations) == (0, 0)


def test_same_size_rewrite_still_reloads(tmp_path, counters) -> None:
    vault = _write_vault(tmp_path, packs=["core"])
    path = _config_file(vault)
    before = path.stat()
    assert VaultConfig.load(vault).packs == ["core"]
    text = path.read_text(encoding="utf-8")
    assert "- core" in text
    path.write_text(text.replace("- core", "- pack"), encoding="utf-8")  # same length
    assert path.stat().st_size == before.st_size
    assert VaultConfig.load(vault).packs == ["pack"]


def test_same_size_same_mtime_rewrite_still_reloads(tmp_path) -> None:
    """mtime+size can repeat (os.utime restore); ctime, which userspace cannot set, cannot."""
    vault = _write_vault(tmp_path, packs=["core"])
    path = _config_file(vault)
    before = path.stat()
    assert VaultConfig.load(vault).packs == ["core"]
    path.write_text(path.read_text(encoding="utf-8").replace("- core", "- pack"), encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_mtime_ns, after.st_size) == (before.st_mtime_ns, before.st_size)
    assert VaultConfig.load(vault).packs == ["pack"]


def test_defaults_file_change_triggers_reload(tmp_path, counters) -> None:
    vault = _write_vault(tmp_path, inherits_application_defaults=True)
    defaults = VaultConfig.application_defaults_path()
    defaults.parent.mkdir(parents=True, exist_ok=True)
    defaults.write_text(yaml.safe_dump({"packs": ["core"]}), encoding="utf-8")
    assert VaultConfig.load(vault).packs == ["core"]
    counters.reset()
    assert VaultConfig.load(vault).packs == ["core"]
    assert counters.yaml_loads == 0

    defaults.write_text(yaml.safe_dump({"packs": ["core", "research"]}), encoding="utf-8")
    assert VaultConfig.load(vault).packs == ["core", "research"]
    assert counters.yaml_loads == 2  # the vault file and the defaults file
    assert counters.validations >= 1


def test_missing_defaults_file_is_a_key_and_its_creation_reloads(tmp_path, counters) -> None:
    vault = _write_vault(tmp_path, inherits_application_defaults=True)
    defaults = VaultConfig.application_defaults_path()
    assert not defaults.exists()
    assert VaultConfig.load(vault).packs == ["core", "research", "personal"]
    counters.reset()
    VaultConfig.load(vault)
    assert counters.yaml_loads == 0  # absent defaults are cached too

    defaults.parent.mkdir(parents=True, exist_ok=True)
    defaults.write_text(yaml.safe_dump({"packs": ["core"]}), encoding="utf-8")
    assert VaultConfig.load(vault).packs == ["core"]
    assert counters.yaml_loads == 2

    defaults.unlink()
    assert VaultConfig.load(vault).packs == ["core", "research", "personal"]


def test_failures_are_never_cached(tmp_path, counters) -> None:
    vault = _write_vault(tmp_path)
    path = _config_file(vault)
    path.write_text("a: [unclosed\n", encoding="utf-8")
    for expected_loads in (1, 2):
        with pytest.raises(ConfigParseError):
            VaultConfig.load(vault)
        assert counters.yaml_loads == expected_loads

    path.write_text(yaml.safe_dump({"marginalia_yaml_version": 99}), encoding="utf-8")
    for _ in range(2):
        with pytest.raises(ConfigVersionUnsupported):
            VaultConfig.load(vault)

    path.write_text(yaml.safe_dump({"marginalia_yaml_version": 2, "packs": []}), encoding="utf-8")
    for _ in range(2):
        with pytest.raises(ConfigParseError):
            VaultConfig.load(vault)

    path.write_text(yaml.safe_dump({"marginalia_yaml_version": 2}), encoding="utf-8")
    assert VaultConfig.load(vault).marginalia_yaml_version == 2


def test_missing_file_still_raises_config_not_found(tmp_path) -> None:
    from okto_neuron.errors import ConfigNotFound

    with pytest.raises(ConfigNotFound):
        VaultConfig.load(tmp_path / "nope")


def test_returned_models_share_no_mutable_state(tmp_path) -> None:
    vault = _write_vault(tmp_path, packs=["core"])
    first = VaultConfig.load(vault)
    pristine = first.model_dump(mode="json")
    first.packs.append("mutated")
    first.embedding.model = "mutated-model"
    first.llm.enabled = not first.llm.enabled
    second = VaultConfig.load(vault)
    assert second.model_dump(mode="json") == pristine
    assert second.packs == ["core"]
    second.packs.append("also-mutated")
    assert VaultConfig.load(vault).model_dump(mode="json") == pristine


def test_explicit_application_defaults_bypass_the_cache(tmp_path, counters) -> None:
    vault = _write_vault(tmp_path, inherits_application_defaults=True)
    baseline = VaultConfig.model_validate({"packs": ["core"]})
    assert VaultConfig.load(vault, application_defaults=baseline).packs == ["core"]
    counters.reset()
    baseline2 = VaultConfig.model_validate({"packs": ["research"]})
    assert VaultConfig.load(vault, application_defaults=baseline2).packs == ["research"]


def test_cache_is_bounded(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(vault_mod, "_CONFIG_CACHE_MAX", 4)
    for index in range(10):
        VaultConfig.load(_write_vault(tmp_path, f"v{index}"))
    assert len(vault_mod._LOAD_CACHE) == 4


def test_vault_yaml_version_is_cached_and_follows_rewrites(tmp_path, counters) -> None:
    vault = _write_vault(tmp_path)
    assert vault_yaml_version(vault) == 2
    assert counters.yaml_loads == 1
    for _ in range(3):
        assert vault_yaml_version(vault) == 2
    assert counters.yaml_loads == 1

    set_vault_yaml_version(vault, 1)
    assert vault_yaml_version(vault) == 1
    set_vault_yaml_version(vault, 2)
    assert vault_yaml_version(vault) == 2

    path = _config_file(vault)
    before = path.stat()
    path.write_text(path.read_text(encoding="utf-8").replace(": 2", ": 1"), encoding="utf-8")
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    assert vault_yaml_version(vault) == 1


def test_vault_yaml_version_errors_are_not_cached(tmp_path, counters) -> None:
    vault = _write_vault(tmp_path)
    path = _config_file(vault)
    path.write_text("a: [unclosed\n", encoding="utf-8")
    for _ in range(2):
        with pytest.raises(yaml.YAMLError):
            vault_yaml_version(vault)
    assert counters.yaml_loads == 2
    assert vault_yaml_version(tmp_path / "absent") is None


def test_missing_version_warning_still_fires_on_a_cache_hit(tmp_path) -> None:
    vault = tmp_path / "v"
    vault.mkdir()
    _config_file(vault).write_text(yaml.safe_dump({"packs": ["core"]}), encoding="utf-8")
    with pytest.warns(UserWarning, match="missing marginalia_yaml_version"):
        VaultConfig.load(vault)
    vault_mod._WARNED_MISSING_VERSION.clear()
    with pytest.warns(UserWarning, match="missing marginalia_yaml_version"):
        VaultConfig.load(vault)  # served from the cache, still warns


def test_concurrent_loads_of_two_files_agree(tmp_path) -> None:
    vaults = [_write_vault(tmp_path, "a", packs=["core"]), _write_vault(tmp_path, "b")]
    expected = [VaultConfig.load(v).model_dump(mode="json") for v in vaults]
    clear_config_cache()
    barrier = threading.Barrier(8)

    def hammer(index: int) -> list[bool]:
        barrier.wait(timeout=20)
        out = []
        for round_ in range(60):
            vault = vaults[(index + round_) % 2]
            loaded = VaultConfig.load(vault).model_dump(mode="json")
            out.append(loaded == expected[(index + round_) % 2])
            vault_yaml_version(vault)
        return out

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [flag for chunk in pool.map(hammer, range(8)) for flag in chunk]
    assert results and all(results)


def test_list_vaults_second_call_reads_and_validates_nothing(tmp_path, counters) -> None:
    root = tmp_path / "vaults"
    for name in ("one", "two", "three"):
        _write_vault(root, name)
    cfg = OktoNeuronConfig(vault_roots=[root])
    first = list_vaults(cfg)
    assert len(first) == 3
    assert counters.yaml_loads >= 3
    counters.reset()
    second = list_vaults(cfg)
    assert (counters.yaml_loads, counters.validations) == (0, 0)
    assert second == first

    _config_file(root / "two").write_text(
        yaml.safe_dump({"marginalia_yaml_version": 2, "storage": {"backend": "ladybug"}}),
        encoding="utf-8",
    )
    counters.reset()
    changed = list_vaults(cfg)
    assert (counters.yaml_loads, counters.validations) == (1, 1)
    assert [e.backend for e in changed] == [e.backend for e in first]


def test_status_endpoint_second_call_reads_and_validates_nothing(
    tmp_path, monkeypatch, counters
) -> None:
    from okto_neuron.server import _folder_watch as fw
    from okto_neuron.server.http import build_rest_app
    from okto_neuron.server.state import init_state, reset_state_for_tests

    class _StubVault:
        recovered_from_corruption = False

        def close(self) -> None:
            pass

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    vault = _write_vault(tmp_path, "vault")
    fw._WATCH_STATUS.clear()
    reset_state_for_tests()
    try:
        state = init_state(_StubVault(), vault)
        with TestClient(
            build_rest_app(state), base_url="http://127.0.0.1", raise_server_exceptions=False
        ) as client:
            first = client.get("/api/v1/status")
            assert first.status_code == 200
            counters.reset()
            second = client.get("/api/v1/status")
            assert second.status_code == 200
            assert (counters.yaml_loads, counters.validations) == (0, 0)
    finally:
        reset_state_for_tests()
