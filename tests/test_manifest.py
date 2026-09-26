from __future__ import annotations

import json
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from okto_neuron.manifest import N2FileEntry, N2Manifest


FIXED_TIME = datetime(2026, 1, 2, 3, 4, 5, 987654, tzinfo=timezone.utc)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _entry(relpath: str = "notes/a.md", sha256: str = "a" * 64) -> N2FileEntry:
    return N2FileEntry(
        relpath=relpath,
        sha256=sha256,
        size_bytes=1,
        mtime_utc=FIXED_TIME,
    )


def _manifest(**overrides: object) -> N2Manifest:
    files = [_entry()]
    payload = {
        "manifest_version": "n2.1",
        "vault_name": "vault",
        "vault_root": Path("."),
        "generated_at": FIXED_TIME,
        "corpus_hash": N2Manifest.compute_corpus_hash(files),
        "file_count": len(files),
        "files": files,
        "ingest_include": ["**/*.md"],
        "ingest_exclude": [".marginalia/**"],
    }
    payload.update(overrides)
    return N2Manifest(**payload)


def test_corpus_hash_deterministic(tmp_path: Path) -> None:
    files = [
        _write(tmp_path / "notes" / "a.md", "alpha"),
        _write(tmp_path / "notes" / "b.md", "bravo"),
    ]

    first = N2Manifest.from_vault("vault", tmp_path, ["**/*.md"], [], files, FIXED_TIME)
    second = N2Manifest.from_vault("vault", tmp_path, ["**/*.md"], [], files, FIXED_TIME)

    assert first.corpus_hash == second.corpus_hash


def test_corpus_hash_order_independent(tmp_path: Path) -> None:
    files = [
        _write(tmp_path / "notes" / "a.md", "alpha"),
        _write(tmp_path / "notes" / "b.md", "bravo"),
    ]

    first = N2Manifest.from_vault("vault", tmp_path, ["**/*.md"], [], files, FIXED_TIME)
    second = N2Manifest.from_vault(
        "vault", tmp_path, ["**/*.md"], [], list(reversed(files)), FIXED_TIME
    )

    assert first.corpus_hash == second.corpus_hash


def test_corpus_hash_changes_on_content(tmp_path: Path) -> None:
    path = _write(tmp_path / "notes" / "a.md", "alpha")
    before = N2Manifest.from_vault("vault", tmp_path, ["**/*.md"], [], [path], FIXED_TIME)

    path.write_text("changed", encoding="utf-8")
    after = N2Manifest.from_vault("vault", tmp_path, ["**/*.md"], [], [path], FIXED_TIME)

    assert before.corpus_hash != after.corpus_hash


def test_canonical_json_byte_stable() -> None:
    manifest = _manifest()

    assert manifest.to_canonical_json().encode("utf-8") == manifest.to_canonical_json().encode(
        "utf-8"
    )


def test_canonical_json_datetime_format() -> None:
    payload = _manifest().to_canonical_json()
    data = json.loads(payload)

    assert data["generated_at"] == "2026-01-02T03:04:05Z"
    assert data["files"][0]["mtime_utc"] == "2026-01-02T03:04:05Z"
    assert "+00:00" not in payload
    assert ".987654" not in payload


def test_vault_name_nfc() -> None:
    manifest = _manifest(vault_name="Cafe\u0301")

    assert manifest.vault_name == unicodedata.normalize("NFC", "Cafe\u0301")


def test_file_count_invariant() -> None:
    with pytest.raises(ValidationError):
        _manifest(file_count=2)
