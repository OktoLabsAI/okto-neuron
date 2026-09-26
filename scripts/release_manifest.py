"""Write or check ``release-manifest.json`` for an Okto Neuron release.

The public installers (``install.sh``, ``install.ps1``) download the wheel from
an immutable GitHub Release URL and refuse it unless its SHA-256 matches this
manifest. The manifest has exactly five keys::

    {
      "version": "0.3.0",
      "wheel": "okto_neuron-0.3.0-py3-none-any.whl",
      "wheel_url": "https://github.com/OktoLabsAI/okto-neuron/releases/download/v0.3.0/okto_neuron-0.3.0-py3-none-any.whl",
      "sha256": "<64 lowercase hex>",
      "source_commit": "<40 lowercase hex, the commit tag v0.3.0 points at>"
    }

Usage::

    # after building the wheel from the clean tagged commit
    python scripts/release_manifest.py write dist/okto_neuron-0.3.0-py3-none-any.whl \
        --source-commit "$(git rev-parse v0.3.0^{commit})"

    # verify a committed manifest against a wheel file and the installers' baked defaults
    python scripts/release_manifest.py check dist/okto_neuron-0.3.0-py3-none-any.whl

``write`` refuses a wheel whose METADATA name/version do not match its file name
or ``pyproject.toml``. ``check`` fails when the manifest, the wheel and the
installer defaults disagree. Neither command touches the network.
"""

from __future__ import annotations

import argparse
import email
import hashlib
import json
import re
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = "OktoLabsAI/okto-neuron"
DIST_NAME = "okto-neuron"
KEYS = ("version", "wheel", "wheel_url", "sha256", "source_commit")
_VERSION = re.compile(r"\d+\.\d+\.\d+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")


class ManifestError(Exception):
    pass


def wheel_filename(version: str) -> str:
    return f"okto_neuron-{version}-py3-none-any.whl"


def wheel_url(version: str) -> str:
    return f"https://github.com/{REPO}/releases/download/v{version}/{wheel_filename(version)}"


def _wheel_metadata(wheel: Path) -> tuple[str, str]:
    with zipfile.ZipFile(wheel) as archive:
        name = next(
            (n for n in archive.namelist() if n.endswith(".dist-info/METADATA")), None
        )
        if name is None:
            raise ManifestError(f"{wheel.name} has no .dist-info/METADATA")
        metadata = email.message_from_bytes(archive.read(name))
    return str(metadata["Name"]), str(metadata["Version"])


def _project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def build_manifest(wheel: Path, source_commit: str) -> dict[str, str]:
    if not _COMMIT.fullmatch(source_commit):
        raise ManifestError("--source-commit must be a full 40-character lowercase commit SHA")
    name, version = _wheel_metadata(wheel)
    if name != DIST_NAME:
        raise ManifestError(f"wheel METADATA name is {name!r}, expected {DIST_NAME!r}")
    if not _VERSION.fullmatch(version):
        raise ManifestError(f"wheel version {version!r} is not a plain X.Y.Z release")
    if wheel.name != wheel_filename(version):
        raise ManifestError(f"wheel file is {wheel.name!r}, expected {wheel_filename(version)!r}")
    project_version = _project_version()
    if version != project_version:
        raise ManifestError(
            f"wheel version {version} does not match pyproject.toml version {project_version}"
        )
    return {
        "version": version,
        "wheel": wheel.name,
        "wheel_url": wheel_url(version),
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "source_commit": source_commit,
    }


def validate_manifest(manifest: dict[str, object]) -> None:
    if tuple(manifest) != KEYS:
        raise ManifestError(f"manifest keys must be exactly {list(KEYS)} in that order")
    version = str(manifest["version"])
    if not _VERSION.fullmatch(version):
        raise ManifestError(f"manifest version {version!r} is not X.Y.Z")
    if manifest["wheel"] != wheel_filename(version):
        raise ManifestError("manifest wheel does not match its version")
    if manifest["wheel_url"] != wheel_url(version):
        raise ManifestError("manifest wheel_url is not the immutable GitHub Release URL")
    if not _SHA256.fullmatch(str(manifest["sha256"])):
        raise ManifestError("manifest sha256 must be 64 lowercase hex characters")
    if not _COMMIT.fullmatch(str(manifest["source_commit"])):
        raise ManifestError("manifest source_commit must be a 40-character lowercase SHA")


def installer_default_errors(manifest: dict[str, object]) -> list[str]:
    """The installers bake the release's wheel URL and version as defaults."""
    version = str(manifest["version"])
    url = str(manifest["wheel_url"])
    shell = (ROOT / "install.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
    expected = {
        "install.sh wheel URL": (f"OKTO_NEURON_DEFAULT_WHEEL_URL:-{url}", shell),
        "install.sh version": (f"OKTO_NEURON_EXPECTED_VERSION:-{version}", shell),
        "install.ps1 wheel URL": (f'"{url}"', powershell),
        "install.ps1 version": (f'else {{ "{version}" }}', powershell),
    }
    return [label for label, (needle, text) in expected.items() if needle not in text]


def check(manifest_path: Path, wheel: Path | None) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_manifest(manifest)
    if wheel is not None:
        built = build_manifest(wheel, str(manifest["source_commit"]))
        if built != manifest:
            diff = {k: (manifest.get(k), built[k]) for k in KEYS if manifest.get(k) != built[k]}
            raise ManifestError(f"manifest does not match {wheel.name}: {diff}")
    missing = installer_default_errors(manifest)
    if missing:
        raise ManifestError("installer defaults do not match the manifest: " + ", ".join(missing))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    write_cmd = sub.add_parser("write", help="write release-manifest.json for a built wheel")
    write_cmd.add_argument("wheel", type=Path)
    write_cmd.add_argument("--source-commit", required=True)
    write_cmd.add_argument("--output", type=Path, default=ROOT / "release-manifest.json")
    check_cmd = sub.add_parser("check", help="check a manifest against a wheel and the installers")
    check_cmd.add_argument("wheel", type=Path, nargs="?")
    check_cmd.add_argument("--manifest", type=Path, default=ROOT / "release-manifest.json")
    args = parser.parse_args(argv)
    try:
        if args.command == "write":
            manifest = build_manifest(args.wheel, args.source_commit)
            args.output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(manifest, indent=2))
            missing = installer_default_errors(manifest)
            if missing:
                print(
                    "note: bake these installer defaults before committing: " + ", ".join(missing),
                    file=sys.stderr,
                )
        else:
            check(args.manifest, args.wheel)
            print(f"release manifest verified: {args.manifest}")
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
