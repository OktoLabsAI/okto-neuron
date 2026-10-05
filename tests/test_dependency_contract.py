"""Release dependency-envelope and lockfile regression checks."""

from __future__ import annotations

import json
import hashlib
import os
import pathlib
import re
import shutil
import socket
import stat
import subprocess
import time
import tomllib
import unicodedata
import urllib.error
import urllib.request
import zipfile
from email.parser import Parser

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


ROOT = pathlib.Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text())
LOCK = tomllib.loads((ROOT / "uv.lock").read_text())
WHEEL_ENV = "OKTO_NEURON_WHEEL"

# These are SHA-256 fingerprints of normalized token n-grams supplied by the private
# release audit. The source tree deliberately retains neither the values nor identifying
# labels. Width is the number of normalized tokens in the fingerprint; punctuation,
# separator, case, accent, and common escaped-Unicode variants therefore converge.
PRIVATE_RESOURCE_FINGERPRINTS = (
    ("private marker 01", 1, "42861c6ab40652bd793dc646d8b24f55012e01c5b78da5d25ca3813aed9a3f73"),
    ("private marker 02", 1, "d919fe625aae69f4becb9aac04b8a213df186e9262e864d27b026719a8e8b984"),
    ("private marker 03", 1, "0c873ecbd3c57a0116ca9190d67c9d72bc0154efc4f81cca5d11c62181846184"),
    ("private marker 04", 2, "7f20af231d625edd8daf802e72da22d1b071908726c75ba8b16d6d195892c474"),
    ("private marker 05", 3, "559b22ed29ca607a3fe0c7b0f85400ee151953316f31c5c69402b5c20feab6e0"),
    ("private marker 06", 2, "68bbbca5f9610c19a065518db4180cb524a2b5f72f08d85858d15b6285af4b25"),
    ("private marker 07", 3, "7df88bc43a853fc7f7afb479e997aa30837d0e390c4c131ceda1e024dfa0f607"),
    ("private marker 08", 1, "c138a2935620b6bfa2684519be47accb25ee436a15a6204d84cf33524833977b"),
    ("private marker 09", 1, "43f1efecd33031b0ccd142b1c5cccc44ea19ad3e7a947965c5b0c16a632b5d7b"),
    ("private marker 10", 1, "9d1eab832937cb738c5d537a2566376cc937c7964beea3852892ab01e00bb229"),
    ("private marker 11", 1, "623f0b5584eb86d9f905e52679a9ce3bca0bd91a950a03e0eaa1b2f8bb3e9908"),
    ("private marker 12", 2, "12ab6f17ecce1ddf8317014a6b582e1a422e820414d8d26ba8eb9308e844b290"),
    ("private marker 13", 1, "ae0fbd3485c763872972b9056b558084b1addb8b99fd6808c26c3ffbedaed0a7"),
    ("private marker 14", 3, "43dd46235f92491f5c985aba87e20366edf1a7924b8b92aa8c48c87c1e415c05"),
    ("private marker 15", 2, "be23946c4607126cdbc6e791d155571d3ca9597a694ef5e87218da4d1472b28f"),
    ("private marker 16", 3, "65e11c0a5ed1e83f9046a560396c10ff4516315004591ef1881f8284c3c2aadf"),
    ("private marker 17", 1, "c604dabc9591840978d2585238a908898b22e3337b207dc722ae208eb204c291"),
    ("private marker 18", 1, "a9e1e346756161703f7ebe0a9ecbefdfc8fdfd4657d6280ef63f6c6aa7334a3b"),
    ("private marker 19", 1, "d68a1a8205ac51dd1b96fc0d17b6d3d03f7873916fa86fb4ae75b5125a0adeea"),
    ("private marker 20", 1, "87a2703c7347daabe36cbaa096514fcb5ebd01d3c51c83288a4c0025ff70b93b"),
    ("private marker 21", 1, "2216e16af044ae890832644892492b0f2320c581dc6d779d5b5cb97b91b5f38b"),
    ("private marker 22", 1, "e338b772ab3dd07c55a914950fed57da374128474df13db7408ef6320c42741f"),
    ("private marker 23", 2, "26068db2dc37f531501b47f400cd9ae166c4e1d12622aaefb61ba71b769022aa"),
    ("private marker 24", 1, "f7e264dda4f4f8863c67cc20950a76e06f9920bf61562ed65effb205266623c8"),
    ("private marker 25", 1, "0c20532ac1cd908a39b4ae6f83baad89ef629935c9db85c43186be0b5b9ad9ed"),
    ("private marker 26", 1, "f8e8d04cccc645eb6f364b24a882a0747697ab17df260009b483c15d5e9ff56d"),
    ("private marker 27", 1, "da36a29c49f218ac68ea4d047db4709a38ff7c21e60cbd015cdb330c335f100d"),
    ("private marker 28", 2, "e8222f4f4de1e2ced0448013eb7da9bba39c732b3446e57316447d8eec964797"),
    ("private marker 29", 2, "e47e689167bf46fe30ac333b1cc22f64636c6cc6b44b3f1243792211dac83fea"),
    ("private marker 30", 3, "86abf7ce9287b032e5b49338b93f4bdee7b310c26056cdc88883474cb47ce31e"),
    ("private marker 31", 2, "7fe61915da1b53885be8a506042ac717f3c596c3900d46650f99357edec51988"),
    ("private marker 32", 2, "bc4f6203877bb139128636c2ed80cba5ab1da5df175b30c20ab48c3402138a84"),
    ("private marker 33", 2, "c3a1c303fe33678a9a10e2c3c6171b67610c8f02db9b26af7f25bb9e9eebb6f0"),
    ("private marker 34", 2, "de1b58f1726d08f1ffbf1729b1ac993c35327bdb18d35079b1f2256aad7ddd2a"),
    ("private marker 35", 2, "7133aed1ef9600e2c37424e3bba64b0066d9cb9dd7bbf21bdfa3ab98ea0f1b59"),
    ("private marker 36", 2, "6dc0f1e4cfa346ca65dcd903902cdb668d9340293940b076049b262327a6d769"),
    ("private marker 37", 2, "c83fe2e923e8ba22a31eb35ae1673d4107957dd5b7c31e8ac34d1f8f7898366c"),
    ("private marker 38", 2, "6e5b1b8e2a188dd766c8958ea012c3b6ffa2ad4370ddf02f69025fe850cbefe7"),
    ("private marker 39", 2, "56efb5285b08ae5ee3ca4d333686c12776d490bf0e941f163815a8ca32157b9a"),
    ("private marker 40", 2, "d50ab143920424dbd34c4a8af4add43261856af0749265f6c31bfa7fd6f65420"),
    ("private marker 41", 2, "1283531277d9e2dab0935329a3998c934185835101e27064ea2a4dcc0f1e7e5c"),
    ("private marker 42", 1, "eb1e7c198c4d064bd427e9e4952bc82c47875e2a11a4824d555d826256e82377"),
    ("private marker 43", 1, "d5c16d5f970c84f6e28cdd2ddda97d802d6c077bf17a02dd4c335675291fad78"),
    ("private marker 44", 1, "65d9d6c5c7c2d5c29e38b777edf8da1dbd764f67deeeaa230724967cafe4e684"),
    ("private marker 45", 4, "add7a1001bdb95499be9cd84cff6305298f9fd2c54591d5c19b8dcc9b67ca13b"),
    ("private marker 46", 5, "e24be678df5131dda6c92936e04825d8e74e5e68958e9f3f95375edf2c0e15b5"),
    ("private marker 47", 1, "87015056b3e74a4b5b07ccef523a1760eec74cd27102e367eda5f1397c149da2"),
    ("private marker 48", 2, "bb9038b2ba93b2bd530eeb9b8259de013a7f8497f05b125b1b295252484def72"),
    ("private marker 49", 3, "df771abeae326f096686958e46237bba2730ddf6c9c9ea0f520367ceb805a374"),
    ("private marker 50", 1, "9bb6100fcd473b5b9c8b910167d668f4919c4e2e1a8f648efdea9c81fdcce1da"),
    ("private marker 51", 1, "e36a081fea3179476d09d42c664296b2834de7a4b8ca224fd18bb39574a1878c"),
    ("private marker 52", 2, "52d3489717d5e72a5f668de6e3a795d59f98987e7e7db32e6a8202618adf76f5"),
    ("private marker 53", 2, "023fac016e80a4b1d6351f57b65ffaf893b1f9176af93363292483d7a56f226e"),
    ("private marker 54", 3, "4edc5db49b4f7cd9e560445c53ae85d65b1030f77e769bbdfa57072b2fb7c99d"),
    ("private marker 55", 1, "a2a1e69c4f8340f60ff88d1484f30c77dc7f46ae32b443db8c0bc373679feba7"),
    ("private marker 56", 1, "316303c1ae35d72e1d167f4a26209e166654c02a530853e9d5d0c8a2dbd781c0"),
    ("private marker 57", 1, "423d7b0835bf083f1669662c2d95fc5f892bb38a2f538ddab22b3ff6d069d892"),
    ("private marker 58", 2, "fa7a965549a32198e5a96a77b7625ef4f3a52b54730c198194172fdeefa3ca77"),
    ("private marker 59", 2, "c7e59f56a3deb9109c69bd9594b0213066ddb1981b1811d6ae4450a295e753d0"),
    ("private marker 60", 4, "b78b3422b3b6ac9018e18be2cde52dd269e0ea14f801b474d65514f7a3f2648c"),
)
PRIVATE_RESOURCE_PATTERNS = {
    "POSIX home path": re.compile(rb"/(?:Users|home)/[A-Za-z0-9._-]+/"),
    "Windows home path": re.compile(rb"[A-Za-z]:\\{1,2}Users\\{1,2}[^\\\r\n]+\\{1,2}"),
    "OpenAI-style secret": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "Google API key": re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"),
    "xAI API key": re.compile(rb"\bxai-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(rb"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{20,}\b"),
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "private key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

# Generic example home paths are useful fixtures in source, but wheel payloads must not
# embed any absolute home path. Source scanning therefore uses secret patterns, private
# fingerprints, and an explicit absolute-symlink check; the exact-wheel gate adds home paths.
SOURCE_PRIVATE_RESOURCE_PATTERNS = {
    label: PRIVATE_RESOURCE_PATTERNS[label]
    for label in PRIVATE_RESOURCE_PATTERNS
    if label not in {"POSIX home path", "Windows home path"}
}

_ESCAPED_UNICODE = re.compile(r"\\u([0-9a-fA-F]{4})")
_ESCAPED_BYTE = re.compile(r"\\x([0-9a-fA-F]{2})")


def _normalized_privacy_tokens(payload: bytes) -> list[str]:
    text = payload.decode("utf-8", errors="ignore")
    text = _ESCAPED_UNICODE.sub(lambda match: chr(int(match.group(1), 16)), text)
    text = _ESCAPED_BYTE.sub(lambda match: chr(int(match.group(1), 16)), text)
    folded = unicodedata.normalize("NFKD", text.casefold())
    ascii_folded = "".join(
        character for character in folded if not unicodedata.combining(character)
    )
    return re.findall(r"[a-z0-9]+", ascii_folded)


def _private_fingerprint_labels(
    payload: bytes,
    fingerprints: tuple[tuple[str, int, str], ...] = PRIVATE_RESOURCE_FINGERPRINTS,
) -> list[str]:
    tokens = _normalized_privacy_tokens(payload)
    by_width: dict[int, dict[str, str]] = {}
    for label, width, digest in fingerprints:
        by_width.setdefault(width, {})[digest] = label

    matched: set[str] = set()
    for width, targets in by_width.items():
        for offset in range(0, len(tokens) - width + 1):
            candidate = " ".join(tokens[offset : offset + width]).encode()
            label = targets.get(hashlib.sha256(candidate).hexdigest())
            if label is not None:
                matched.add(label)
    return sorted(matched)


def test_private_resource_patterns_have_positive_controls() -> None:
    controls = {
        "POSIX home path": b"/Users/" + b"example/private/file.txt",
        "Windows home path": b"C:\\" + b"Users\\example\\private\\file.txt",
        "OpenAI-style secret": b"sk" + b"-abcdefghijklmnopqrstuvwxyz123456",
        "Google API key": b"AI" + b"za0123456789abcdefghijklmnopqrstuvwxy",
        "xAI API key": b"xai" + b"-abcdefghijklmnopqrstuvwxyz123456",
        "GitHub token": b"ghp" + b"_abcdefghijklmnopqrstuvwxyz123456",
        "AWS access key": b"AK" + b"IAABCDEFGHIJKLMNOP",
        "private key": b"-----BEGIN " + b"PRIVATE KEY-----",
    }
    for label, sample in controls.items():
        assert PRIVATE_RESOURCE_PATTERNS[label].search(sample), label
    assert PRIVATE_RESOURCE_PATTERNS["Windows home path"].search(
        b"C:\\\\" + b"Users\\\\example\\\\private\\\\file.txt"
    )
    assert PRIVATE_RESOURCE_PATTERNS["AWS access key"].search(b"AS" + b"IAABCDEFGHIJKLMNOP")


def test_private_resource_fingerprints_normalize_text_without_plaintext_controls() -> None:
    normalized = b"fictional privacy marker"
    control = ("synthetic marker", 3, hashlib.sha256(normalized).hexdigest())

    assert _private_fingerprint_labels(b"FICTIONAL.privacy_marker", fingerprints=(control,)) == [
        "synthetic marker"
    ]
    assert _private_fingerprint_labels(
        "fíctional-privacy marker".encode(), fingerprints=(control,)
    ) == ["synthetic marker"]
    assert _private_fingerprint_labels(
        b"fictional\\u0020privacy-marker", fingerprints=(control,)
    ) == ["synthetic marker"]


def test_release_source_tree_excludes_private_content_and_absolute_symlinks() -> None:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    violations: list[str] = []
    scanned: set[str] = set()

    for encoded_relative in listed.split(b"\0"):
        if not encoded_relative:
            continue
        relative = os.fsdecode(encoded_relative)
        path = ROOT / relative
        if not os.path.lexists(path):
            # `git ls-files` still reports an index entry deleted in the worktree.
            continue

        if path.is_symlink():
            target = os.readlink(path)
            payload = os.fsencode(target)
            if pathlib.Path(target).is_absolute() or pathlib.PureWindowsPath(target).is_absolute():
                violations.append(f"{relative}: absolute symlink target {target!r}")
        elif path.is_file():
            payload = path.read_bytes()
        else:
            continue
        scanned.add(relative)

        normalized_name = re.sub(rb"[-_.]+", b" ", encoded_relative.lower())
        normalized_payload = re.sub(rb"[-_.]+", b" ", payload.lower())
        for label in _private_fingerprint_labels(encoded_relative):
            violations.append(f"{relative}: filename {label}")
        for label in _private_fingerprint_labels(payload):
            violations.append(f"{relative}: {label}")
        for label, pattern in SOURCE_PRIVATE_RESOURCE_PATTERNS.items():
            if pattern.search(encoded_relative) or pattern.search(normalized_name):
                violations.append(f"{relative}: filename {label}")
            if pattern.search(payload) or pattern.search(normalized_payload):
                violations.append(f"{relative}: {label}")

    assert "tests/test_dependency_contract.py" in scanned
    assert not violations, "private content found in release source tree:\n" + "\n".join(violations)


def _isolated_env(tmp_path: pathlib.Path, name: str) -> dict[str, str]:
    home = tmp_path / name
    home.mkdir()
    env = os.environ.copy()
    for variable in tuple(env):
        if variable.startswith(("OKTO_NEURON_", "PYTHON", "UV_")) or variable == "VIRTUAL_ENV":
            env.pop(variable)
    env.update(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
        }
    )
    return env


def test_clean_wheel_environment_removes_ambient_runtime_inputs(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OKTO_NEURON_PACK_PATH", "/private/ambient-pack")
    monkeypatch.setenv("OKTO_NEURON_GLM_API_KEY", "private-provider-key")
    monkeypatch.setenv("OKTO_NEURON_REFERENCE_DATE", "1900-01-01")
    monkeypatch.setenv("PYTHONPATH", "/private/ambient-python")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/private/ambient-venv")

    env = _isolated_env(tmp_path, "hostile-environment-home")

    assert not any(key.startswith("OKTO_NEURON_") for key in env)
    assert not any(key.startswith("PYTHON") for key in env)
    assert not any(key.startswith("UV_") for key in env)
    assert "VIRTUAL_ENV" not in env
    assert env["HOME"] == str(tmp_path / "hostile-environment-home")


def _requirements(entries: list[str]) -> dict[str, Requirement]:
    result: dict[str, Requirement] = {}
    for entry in entries:
        requirement = Requirement(entry)
        name = canonicalize_name(requirement.name)
        assert name not in result, f"duplicate direct requirement: {name}"
        result[name] = requirement
    return result


def _requirement_signature(requirement: Requirement) -> tuple[str, str, str]:
    return (
        canonicalize_name(requirement.name),
        str(requirement.specifier),
        str(requirement.marker or ""),
    )


def _declared_release_requirements() -> list[tuple[str, Requirement]]:
    declared = [
        ("build-system", Requirement(entry)) for entry in PYPROJECT["build-system"]["requires"]
    ]
    declared.extend(
        ("project", Requirement(entry)) for entry in PYPROJECT["project"]["dependencies"]
    )
    for extra, entries in PYPROJECT["project"]["optional-dependencies"].items():
        declared.extend((f"extra:{extra}", Requirement(entry)) for entry in entries)
    for group, entries in PYPROJECT["dependency-groups"].items():
        declared.extend((f"group:{group}", Requirement(entry)) for entry in entries)
    return declared


def _expected_wheel_requirements() -> set[tuple[str, str, str]]:
    expected = {
        _requirement_signature(Requirement(entry)) for entry in PYPROJECT["project"]["dependencies"]
    }
    for extra, entries in PYPROJECT["project"]["optional-dependencies"].items():
        for entry in entries:
            requirement = Requirement(f"{entry}; extra == '{extra}'")
            expected.add(_requirement_signature(requirement))
    return expected


def test_all_direct_dependencies_have_a_compatibility_ceiling() -> None:
    unbounded: list[str] = []
    upper_bound_operators = {"<", "<=", "==", "===", "~="}

    for source, requirement in _declared_release_requirements():
        if not any(spec.operator in upper_bound_operators for spec in requirement.specifier):
            unbounded.append(f"{source}: {requirement}")

    assert not unbounded, "direct dependencies without a compatibility ceiling:\n" + "\n".join(
        unbounded
    )


def test_uv_groups_match_their_published_extras() -> None:
    extras = PYPROJECT["project"]["optional-dependencies"]
    groups = PYPROJECT["dependency-groups"]

    assert _requirements(extras["mcp"]) == _requirements(extras["serve"])
    assert _requirements(groups["serve"]) == _requirements(extras["serve"])
    for name in ("jsonld", "litellm", "bedrock", "sentence-transformers"):
        assert _requirements(groups[name]) == _requirements(extras[name])

    # `dev`'s uv group carries exactly one intentional exception beyond its
    # published extra: `stub-backend-pkg`, a `[tool.uv.sources]` local-path
    # test fixture (M3 §2.11) that must never be published as part of the
    # `okto-neuron[dev]` PyPI extra — a path-only requirement would fail to
    # resolve for an external `pip install okto-neuron[dev]` user. Every other
    # `dev` requirement still needs exact parity with the published extra.
    dev_group = _requirements(groups["dev"])
    dev_extra = _requirements(extras["dev"])
    canonical_stub_name = canonicalize_name("stub-backend-pkg")
    assert canonical_stub_name in dev_group
    assert canonical_stub_name not in dev_extra
    dev_group_without_stub = {
        name: requirement for name, requirement in dev_group.items() if name != canonical_stub_name
    }
    assert dev_group_without_stub == dev_extra


def test_runtime_import_boundaries_have_declared_install_paths() -> None:
    project = _requirements(PYPROJECT["project"]["dependencies"])
    extras = {
        name: _requirements(entries)
        for name, entries in PYPROJECT["project"]["optional-dependencies"].items()
    }

    assert "click" in project
    assert "email-validator" in project
    assert set(extras["embeddings"]) >= {"fastembed", "numpy"}
    assert "ladybug" in extras["ladybug"]
    assert set(extras["mcp"]) >= {
        "fastembed",
        "fastmcp",
        "ladybug",
        "numpy",
        "starlette",
        "uvicorn",
    }
    assert set(extras["serve"]) >= {
        "fastembed",
        "fastmcp",
        "ladybug",
        "numpy",
        "starlette",
        "uvicorn",
    }
    assert "rdflib" in extras["jsonld"]
    assert "sentence-transformers" in extras["sentence-transformers"]
    assert {"boto3", "litellm"} <= set(extras["bedrock"])


def test_lock_versions_are_inside_the_declared_envelope() -> None:
    locked_versions = {
        canonicalize_name(package["name"]): Version(package["version"])
        for package in LOCK["package"]
        if "version" in package
    }
    runtime_requirements = [Requirement(entry) for entry in PYPROJECT["project"]["dependencies"]]
    for entries in PYPROJECT["project"]["optional-dependencies"].values():
        runtime_requirements.extend(Requirement(entry) for entry in entries)
    for entries in PYPROJECT["dependency-groups"].values():
        runtime_requirements.extend(Requirement(entry) for entry in entries)

    errors: list[str] = []
    for requirement in runtime_requirements:
        name = canonicalize_name(requirement.name)
        version = locked_versions.get(name)
        if version is None:
            errors.append(f"{name}: absent from uv.lock")
        elif version not in requirement.specifier:
            errors.append(f"{name}=={version}: outside {requirement.specifier}")

    assert not errors, "lockfile drifted outside the release dependency envelope:\n" + "\n".join(
        errors
    )


# Transitive packages with a released, pip-audit-confirmed CVE fix as of the review that
# raised this floor. None of these are direct project dependencies (see
# `test_all_direct_dependencies_have_a_compatibility_ceiling`), so nothing in `pyproject.toml`
# pins them — a routine `uv lock` re-resolve is free to drift them back below the patched
# floor the moment an upstream extra loosens its own bound. This floor is deliberately a
# separate, explicit check rather than folded into the declared-envelope test above, because
# that test only verifies *declared* pyproject bounds, and these packages have none.
CVE_PATCHED_VERSION_FLOORS = {
    # authlib/joserfc/pyjwt/secretstorage -> cryptography; keyword: cryptography CVE fix.
    "cryptography": Version("50.0.0"),
    # fastembed -> pillow; keyword: pillow CVE fix.
    "pillow": Version("12.3.0"),
    # fastmcp-slim -> mcp; keyword: mcp CVE fix.
    "mcp": Version("1.28.1"),
}


def test_lock_pins_transitive_deps_above_their_cve_patched_floor() -> None:
    locked_versions = {
        canonicalize_name(package["name"]): Version(package["version"])
        for package in LOCK["package"]
        if "version" in package
    }

    errors: list[str] = []
    for name, floor in CVE_PATCHED_VERSION_FLOORS.items():
        version = locked_versions.get(canonicalize_name(name))
        if version is None:
            errors.append(f"{name}: absent from uv.lock")
        elif version < floor:
            errors.append(f"{name}=={version}: below the CVE-patched floor {floor}")

    assert not errors, (
        "uv.lock regressed a transitive dep below its CVE-patched floor:\n" + "\n".join(errors)
    )


def test_lock_metadata_matches_published_dependency_metadata() -> None:
    project_pkg = next(package for package in LOCK["package"] if package["name"] == "okto-neuron")
    actual = {
        _requirement_signature(
            Requirement(
                f"{requirement['name']}{requirement.get('specifier', '')}"
                + (f"; {requirement['marker']}" if requirement.get("marker") else "")
            )
        )
        for requirement in project_pkg["metadata"]["requires-dist"]
    }
    assert actual == _expected_wheel_requirements()


def test_built_wheel_metadata_matches_project() -> None:
    wheel_path = os.environ.get(WHEEL_ENV)
    if not wheel_path:
        pytest.skip(f"set {WHEEL_ENV}=/path/to/wheel to validate a release artifact")

    wheel = pathlib.Path(wheel_path)
    with zipfile.ZipFile(wheel) as archive:
        assert "okto_neuron/marginalia.config.yaml" not in archive.namelist()
        metadata_path = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = Parser().parsestr(archive.read(metadata_path).decode())
        manifest = tomllib.loads(archive.read("okto_neuron/models/manifest.toml").decode())

    wheel_requirements = [Requirement(entry) for entry in metadata.get_all("Requires-Dist", [])]
    actual = {_requirement_signature(requirement) for requirement in wheel_requirements}
    assert actual == _expected_wheel_requirements()
    assert all(
        canonicalize_name(requirement.name) != "gliner" for requirement in wheel_requirements
    )
    assert {entry["model_id"] for entry in manifest["model"]} == {"BAAI/bge-small-en-v1.5"}
    assert all("gliner" not in entry["model_id"].lower() for entry in manifest["model"])
    assert metadata["Requires-Python"] == PYPROJECT["project"]["requires-python"]
    assert set(metadata.get_all("Provides-Extra", [])) == set(
        PYPROJECT["project"]["optional-dependencies"]
    )


def test_built_wheel_members_exclude_private_source_content() -> None:
    wheel_path = os.environ.get(WHEEL_ENV)
    if not wheel_path:
        pytest.skip(f"set {WHEEL_ENV}=/path/to/wheel to validate a release artifact")

    wheel = pathlib.Path(wheel_path)
    inspected: list[str] = []
    violations: list[str] = []
    with zipfile.ZipFile(wheel) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            name = member.filename
            inspected.append(name)
            encoded_name = name.encode("utf-8")
            payload = archive.read(name)
            normalized = re.sub(rb"[-_.]+", b" ", payload.lower())
            normalized_name = re.sub(rb"[-_.]+", b" ", encoded_name.lower())
            for label in _private_fingerprint_labels(encoded_name):
                violations.append(f"{name}: filename {label}")
            for label in _private_fingerprint_labels(payload):
                violations.append(f"{name}: {label}")
            for label, pattern in PRIVATE_RESOURCE_PATTERNS.items():
                if pattern.search(encoded_name) or pattern.search(normalized_name):
                    violations.append(f"{name}: filename {label}")
                if pattern.search(payload) or pattern.search(normalized):
                    violations.append(f"{name}: {label}")

    assert "okto_neuron/__init__.py" in inspected
    assert "okto_neuron/core/schema/_builtin_packs/builtin.yaml" in inspected
    assert "okto_neuron/models/manifest.toml" in inspected
    assert any(name.startswith("okto_neuron/_webui/assets/") for name in inspected)
    assert any(name.endswith(".dist-info/METADATA") for name in inspected)
    assert not violations, "private content found in wheel resources:\n" + "\n".join(violations)


def test_base_wheel_imports_and_cli_entrypoints_in_clean_environment(
    tmp_path: pathlib.Path,
) -> None:
    wheel_path = os.environ.get(WHEEL_ENV)
    if not wheel_path:
        pytest.skip(f"set {WHEEL_ENV}=/path/to/wheel to validate a release artifact")

    uv = shutil.which("uv")
    assert uv is not None, "uv is required to validate the clean-wheel contract"
    wheel = pathlib.Path(wheel_path).resolve()
    assert wheel.is_file(), f"wheel does not exist: {wheel}"

    venv = tmp_path / "base-wheel-venv"
    subprocess.run(
        [uv, "venv", str(venv), "--python", "3.12"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    suffix = ".exe" if os.name == "nt" else ""
    python = scripts / f"python{suffix}"
    okto_neuron_cli = scripts / f"okto-neuron{suffix}"
    marginalia = scripts / f"marginalia{suffix}"
    kg = scripts / f"kg{suffix}"
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), str(wheel)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    env = _isolated_env(tmp_path, "base-wheel-home")
    expected_version = PYPROJECT["project"]["version"]
    imported = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import importlib.metadata as md; import okto_neuron; "
                "assert md.version('okto-neuron') == okto_neuron.__version__; "
                "print(okto_neuron.__version__)"
            ),
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert imported.stdout.strip() == expected_version

    email_identifier = subprocess.run(
        [
            str(python),
            "-c",
            (
                "from okto_neuron.schema.support import Identifier; "
                "item = Identifier(scheme='EMAIL', value='user@example.com', "
                "owner_id='agent:x'); print(item.value)"
            ),
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert email_identifier.stdout.strip() == "user@example.com"

    optional_providers = subprocess.run(
        [
            str(python),
            "-c",
            """
from okto_neuron.config._vault import EmbeddingConfig
from okto_neuron.embed import EmbeddingProviderError, get_provider

checks = (
    ("fastembed", "okto-neuron[embeddings]", False),
    ("sentence-transformers", "okto-neuron[sentence-transformers]", False),
    ("openai", "okto-neuron[litellm]", True),
)
for provider, expected, call_embed in checks:
    try:
        resolved = get_provider(EmbeddingConfig(provider=provider))
        if call_embed:
            resolved.embed("dependency contract")
    except EmbeddingProviderError as exc:
        assert expected in str(exc), (provider, str(exc))
    else:
        raise AssertionError(f"{provider} silently ran without its advertised extra")
print("optional embedding boundaries fail loud")
""",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert optional_providers.stdout.strip() == "optional embedding boundaries fail loud"

    for executable, program in (
        (okto_neuron_cli, "okto-neuron"),
        (kg, "kg"),
        (marginalia, "marginalia"),
    ):
        result = subprocess.run(
            [str(executable), "--version"],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        # First line "<command> <version>", then the Okto Labs attribution.
        assert result.stdout.splitlines() == [
            f"{program} {expected_version}",
            "Okto Neuron by Okto Labs",
        ]
        if program == "marginalia":
            assert "the 'marginalia' command is now 'okto-neuron'" in result.stderr
        help_result = subprocess.run(
            [str(executable), "--help"],
            cwd=tmp_path,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        help_payload = f"{help_result.stdout}\n{help_result.stderr}".encode()
        normalized_help_payload = re.sub(rb"[-_.]+", b" ", help_payload.lower())
        help_violations = _private_fingerprint_labels(help_payload)
        for label, pattern in PRIVATE_RESOURCE_PATTERNS.items():
            if pattern.search(help_payload) or pattern.search(normalized_help_payload):
                help_violations.append(label)
        assert not help_violations, (
            f"private content found in exact-wheel {program} --help: " + ", ".join(help_violations)
        )
        if program == "okto-neuron":
            assert "generic corpus pilot gate" in help_result.stdout.lower()

    for executable in (okto_neuron_cli, kg):
        missing_server = subprocess.run(
            [str(executable), "serve", "--vault", str(tmp_path / "base-wheel-vault")],
            cwd=tmp_path,
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        assert missing_server.returncode != 0
        assert "install okto-neuron[serve]" in missing_server.stderr
        assert "Traceback" not in missing_server.stderr

    # D-88 moved `ladybug` (the default index engine's dependency) from the
    # [ladybug] extra into the base wheel's unconditional dependencies, so a
    # base-wheel `kg rebuild` can no longer fail with "extra not installed" --
    # ladybug always imports. What the base wheel still surfaces cleanly here
    # is the "no vault at this location yet" case (ConfigNotFound), not a
    # traceback.
    missing_graph = subprocess.run(
        [str(kg), "rebuild"],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert missing_graph.returncode != 0
    assert "ConfigNotFound" in missing_graph.stderr
    assert "Traceback" not in missing_graph.stderr

    missing_jsonld = subprocess.run(
        [
            str(python),
            "-c",
            """
from pathlib import Path
from okto_neuron import Vault
from okto_neuron.errors import ExportError

class EmptyStore:
    def list_nodes(self):
        return []

try:
    Vault(Path.cwd(), EmptyStore()).export()
except ExportError as exc:
    assert "install okto-neuron[jsonld]" in str(exc)
else:
    raise AssertionError("JSON-LD export silently ran without its advertised extra")
print("JSON-LD boundary fails loud")
""",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert missing_jsonld.stdout.strip() == "JSON-LD boundary fails loud"

    retired_mcp = subprocess.run(
        [
            str(python),
            "-c",
            """
from pathlib import Path
from okto_neuron.mcp.server import build_app
from okto_neuron.mcp_server import run

for callback in (lambda: build_app(Path.cwd()), lambda: run(Path.cwd(), host="0.0.0.0")):
    try:
        callback()
    except RuntimeError as exc:
        message = str(exc)
        assert "authentication and loopback-only contract" in message
        assert "okto-neuron[serve]" in message
    else:
        raise AssertionError("retired MCP server did not fail closed")
print("legacy MCP entry points fail closed")
""",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert retired_mcp.stdout.strip() == "legacy MCP entry points fail closed"

    retired_gliner = subprocess.run(
        [
            str(python),
            "-c",
            """
import importlib.util
import warnings

assert importlib.util.find_spec("gliner") is None
from okto_neuron.models.ner import GLiNERNER

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    extractor = GLiNERNER()
assert any(item.category is DeprecationWarning for item in caught)
try:
    extractor.extract("Fictional demo text", ["Concept"])
except RuntimeError as exc:
    assert "configured LLM extraction pipeline" in str(exc)
else:
    raise AssertionError("retired GLiNER path remained executable")
print("GLiNER retirement is import-compatible and fails actionably")
""",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert (
        retired_gliner.stdout.strip()
        == "GLiNER retirement is import-compatible and fails actionably"
    )

    retired_personal_config = subprocess.run(
        [
            str(python),
            "-c",
            """
from importlib import resources
import re
import okto_neuron.config as config

# Generic template exports and corpus-specific convenience exports are both retired.
retired_exact = (
    "CONFIG_TEMPLATE_NAME",
    "load_config_template",
)
retired_shape = re.compile(r"^get_[a-z0-9_]+_(?:exclude_globs|transcript_caps)$")
exported = dir(config)
assert all(name not in exported for name in retired_exact), retired_exact
assert not any(retired_shape.fullmatch(name) for name in exported), exported
assert not resources.files("okto_neuron").joinpath("marginalia.config.yaml").is_file()
print("personalized config template surface is retired")
""",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert (
        retired_personal_config.stdout.strip() == "personalized config template surface is retired"
    )


def test_ladybug_extra_executes_graph_boundary_in_clean_environment(
    tmp_path: pathlib.Path,
) -> None:
    wheel_path = os.environ.get(WHEEL_ENV)
    if not wheel_path:
        pytest.skip(f"set {WHEEL_ENV}=/path/to/wheel to validate a release artifact")

    uv = shutil.which("uv")
    assert uv is not None, "uv is required to validate the clean-wheel contract"
    wheel = pathlib.Path(wheel_path).resolve()
    venv = tmp_path / "ladybug-wheel-venv"
    subprocess.run(
        [uv, "venv", str(venv), "--python", "3.12"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    suffix = ".exe" if os.name == "nt" else ""
    python = scripts / f"python{suffix}"
    marginalia = scripts / f"marginalia{suffix}"
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), f"{wheel}[ladybug]"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    env = _isolated_env(tmp_path, "ladybug-wheel-home")
    vault = tmp_path / "ladybug-vault"
    # DEFAULT_NEW_VAULT_BACKEND is "grafx" (D-94); this test exercises the
    # ladybug graph-store boundary specifically, so it must pin the backend
    # explicitly rather than rely on the (no longer ladybug) default.
    subprocess.run(
        [str(marginalia), "init", "--embedder", "stub", "--backend", "ladybug", str(vault)],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    result = subprocess.run(
        [
            str(python),
            "-c",
            """
from okto_neuron import Vault
from okto_neuron.store.ladybug import LadybugStore

vault = Vault.open("ladybug-vault")
# M1 (D-20) wraps every backend's GraphStore in an IndexedStore that keeps a
# rebuildable IndexStore cache in sync; `.graph` is the wrapper's sanctioned
# escape hatch back to the raw, backend-specific store (see indexed.py).
assert isinstance(vault.store.graph, LadybugStore)
vault.close()
print("Ladybug feature boundary executed")
""",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "Ladybug feature boundary executed"


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.parametrize("server_extra", ["mcp", "serve"])
def test_server_extras_start_direct_ui_and_authenticated_mcp_exact_wheel_boundary(
    tmp_path: pathlib.Path, server_extra: str
) -> None:
    wheel_path = os.environ.get(WHEEL_ENV)
    if not wheel_path:
        pytest.skip(f"set {WHEEL_ENV}=/path/to/wheel to validate a release artifact")

    uv = shutil.which("uv")
    assert uv is not None, "uv is required to validate the clean-wheel contract"
    wheel = pathlib.Path(wheel_path).resolve()
    assert wheel.is_file(), f"wheel does not exist: {wheel}"

    venv = tmp_path / f"{server_extra}-wheel-venv"
    subprocess.run(
        [uv, "venv", str(venv), "--python", "3.12"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    suffix = ".exe" if os.name == "nt" else ""
    python = scripts / f"python{suffix}"
    marginalia = scripts / f"marginalia{suffix}"
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), f"{wheel}[{server_extra}]"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    home_name = f"{server_extra}-wheel-home"
    env = _isolated_env(tmp_path, home_name)

    subprocess.run(
        [
            str(python),
            "-c",
            (
                "import fastembed, fastmcp, ladybug, numpy, starlette, uvicorn; "
                "import okto_neuron.server.http, okto_neuron.server.runtime; "
                "print('serve feature imports closed')"
            ),
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    rest_port = _free_loopback_port()
    mcp_port = _free_loopback_port()
    while mcp_port == rest_port:
        mcp_port = _free_loopback_port()
    process = subprocess.Popen(
        [
            str(marginalia),
            "serve",
            "--port",
            str(rest_port),
            "--mcp-port",
            str(mcp_port),
            "--no-open",
        ],
        cwd=tmp_path,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 20.0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate()
                pytest.fail(
                    f"okto-neuron[{server_extra}] exited before becoming healthy:\n"
                    f"stdout:\n{stdout}\nstderr:\n{stderr}"
                )
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{rest_port}/health", timeout=0.5
                ) as response:
                    assert response.status == 200
                    assert response.read() == b'{"status":"ok"}'
                break
            except (OSError, urllib.error.URLError) as exc:
                last_error = exc
                time.sleep(0.1)
        else:
            pytest.fail(f"okto-neuron[{server_extra}] did not become healthy: {last_error}")

        with urllib.request.urlopen(
            f"http://127.0.0.1:{rest_port}/version", timeout=2.0
        ) as response:
            assert response.status == 200
            assert PYPROJECT["project"]["version"].encode() in response.read()

        # The exact wheel must expose REST/UI directly to a plain local browser,
        # without a bootstrap URL or cookie session.
        for path in ("/", "/api/v1/vaults", "/api/v1/status"):
            with urllib.request.urlopen(
                f"http://127.0.0.1:{rest_port}{path}", timeout=2.0
            ) as response:
                assert response.status == 200
                assert response.headers.get("Set-Cookie") is None

        token_path = pathlib.Path(env["HOME"]) / ".okto-neuron" / f"daemon-{rest_port}.token"
        token = token_path.read_text(encoding="utf-8").strip()
        assert token
        if os.name != "nt":
            assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
        unauthenticated = urllib.request.Request(
            f"http://127.0.0.1:{mcp_port}/mcp",
            data=json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "artifact-gate", "version": "1"},
                    },
                }
            ).encode(),
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as unauthorized:
            urllib.request.urlopen(unauthenticated, timeout=2.0)
        assert unauthorized.value.code in {401, 403}

        mcp_env = env.copy()
        mcp_env["OKTO_NEURON_AUTH_TOKEN"] = token
        mcp_result = subprocess.run(
            [
                str(python),
                "-c",
                """
import asyncio
import os
import sys
from fastmcp import Client

async def main():
    async with Client(sys.argv[1], auth=os.environ["OKTO_NEURON_AUTH_TOKEN"]) as client:
        tools = sorted(tool.name for tool in await client.list_tools())
        assert tools == [
            "ask",
            "explore",
            "ingest_status",
            "init_vault",
            "list_vaults",
            "remember",
        ], tools
        print(",".join(tools))

asyncio.run(main())
""",
                f"http://127.0.0.1:{mcp_port}/mcp",
            ],
            cwd=tmp_path,
            env=mcp_env,
            check=True,
            capture_output=True,
            text=True,
        )
        assert (
            mcp_result.stdout.strip()
            == "ask,explore,ingest_status,init_vault,list_vaults,remember"
        )
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=15.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)


def test_jsonld_extra_executes_export_boundary_in_clean_environment(
    tmp_path: pathlib.Path,
) -> None:
    wheel_path = os.environ.get(WHEEL_ENV)
    if not wheel_path:
        pytest.skip(f"set {WHEEL_ENV}=/path/to/wheel to validate a release artifact")

    uv = shutil.which("uv")
    assert uv is not None, "uv is required to validate the clean-wheel contract"
    wheel = pathlib.Path(wheel_path).resolve()
    venv = tmp_path / "jsonld-wheel-venv"
    subprocess.run(
        [uv, "venv", str(venv), "--python", "3.12"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    scripts = venv / ("Scripts" if os.name == "nt" else "bin")
    suffix = ".exe" if os.name == "nt" else ""
    python = scripts / f"python{suffix}"
    subprocess.run(
        [uv, "pip", "install", "--python", str(python), f"{wheel}[jsonld]"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    env = _isolated_env(tmp_path, "jsonld-wheel-home")
    result = subprocess.run(
        [
            str(python),
            "-c",
            """
import json
from pathlib import Path
from okto_neuron import Vault

class EmptyStore:
    def list_nodes(self):
        return []

payload = json.loads(Vault(Path.cwd(), EmptyStore()).export())
assert payload["@graph"] == []
print("JSON-LD feature boundary executed")
""",
        ],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "JSON-LD feature boundary executed"
