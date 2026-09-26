"""scripts/release_manifest.py: the SHA-256 manifest the public installers verify."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _module():
    spec = importlib.util.spec_from_file_location("release_manifest", ROOT / "scripts" / "release_manifest.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _wheel(tmp_path: Path, *, name: str = "okto-neuron", version: str = VERSION) -> Path:
    path = tmp_path / f"okto_neuron-{version}-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            f"okto_neuron-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n",
        )
    return path


def test_write_produces_the_five_key_immutable_release_manifest(tmp_path: Path) -> None:
    rm = _module()
    wheel = _wheel(tmp_path)
    out = tmp_path / "release-manifest.json"
    assert rm.main(["write", str(wheel), "--source-commit", COMMIT, "--output", str(out)]) == 0
    manifest = json.loads(out.read_text(encoding="utf-8"))
    assert list(manifest) == ["version", "wheel", "wheel_url", "sha256", "source_commit"]
    assert manifest["version"] == VERSION
    assert manifest["wheel"] == f"okto_neuron-{VERSION}-py3-none-any.whl"
    assert manifest["wheel_url"] == (
        f"https://github.com/OktoLabsAI/okto-neuron/releases/download/v{VERSION}/{manifest['wheel']}"
    )
    assert manifest["sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert manifest["source_commit"] == COMMIT


def test_installers_bake_the_manifest_defaults_for_this_version(tmp_path: Path) -> None:
    rm = _module()
    manifest = rm.build_manifest(_wheel(tmp_path), COMMIT)
    assert rm.installer_default_errors(manifest) == []
    # check() ties manifest, wheel and installer defaults together.
    path = tmp_path / "release-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    rm.check(path, _wheel(tmp_path))


@pytest.mark.parametrize(
    ("kwargs", "commit", "message"),
    [
        ({"name": "marginalia"}, COMMIT, "METADATA name"),
        ({}, "abc123", "40-character"),
    ],
)
def test_write_refuses_a_wrong_wheel_or_commit(
    tmp_path: Path, kwargs: dict[str, str], commit: str, message: str
) -> None:
    rm = _module()
    with pytest.raises(rm.ManifestError, match=message):
        rm.build_manifest(_wheel(tmp_path, **kwargs), commit)


def test_check_detects_a_tampered_checksum(tmp_path: Path) -> None:
    rm = _module()
    wheel = _wheel(tmp_path)
    manifest = rm.build_manifest(wheel, COMMIT)
    manifest["sha256"] = "0" * 64
    path = tmp_path / "release-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(rm.ManifestError, match="does not match"):
        rm.check(path, wheel)
