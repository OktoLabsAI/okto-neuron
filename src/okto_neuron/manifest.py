"""N2 vault manifest models."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_hex64(value: str, field_name: str) -> str:
    if HEX64_RE.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a 64-character lowercase hex sha256")
    return value


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _format_utc_second(value: datetime) -> str:
    return _to_utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class N2FileEntry(BaseModel):
    """One file entry in an N2 manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relpath: str
    sha256: str
    size_bytes: int = Field(ge=0)
    mtime_utc: datetime
    skipped_reason: str | None = None

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        return _validate_hex64(value, "sha256")

    @field_validator("mtime_utc")
    @classmethod
    def _normalize_mtime_utc(cls, value: datetime) -> datetime:
        return _to_utc(value)

    @field_serializer("mtime_utc", when_used="json")
    def _serialize_mtime_utc(self, value: datetime) -> str:
        return _format_utc_second(value)


class N2Manifest(BaseModel):
    """N2 vault manifest.

    Corpus hash algorithm (TR4):
    Sort files by relpath ASCII ascending.
    Build line-delimited UTF-8 string: for each entry, emit f"{relpath}\t{sha256}\n" (literal tab, literal newline).
    Return sha256(string.encode('utf-8')).hexdigest().
    Must be a pure function of (relpath, sha256) pairs only — independent of OS listing order, mtime, vault_root, generated_at.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_version: Literal["n2.1"]
    vault_name: str
    vault_root: Path
    generated_at: datetime
    corpus_hash: str
    file_count: int = Field(ge=0)
    files: list[N2FileEntry]
    ingest_include: list[str]
    ingest_exclude: list[str]

    @field_validator("vault_name")
    @classmethod
    def _normalize_vault_name(cls, value: str) -> str:
        return unicodedata.normalize("NFC", value)

    @field_validator("vault_root")
    @classmethod
    def _resolve_vault_root(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("generated_at")
    @classmethod
    def _normalize_generated_at(cls, value: datetime) -> datetime:
        return _to_utc(value)

    @field_validator("corpus_hash")
    @classmethod
    def _validate_corpus_hash(cls, value: str) -> str:
        return _validate_hex64(value, "corpus_hash")

    @field_serializer("generated_at", when_used="json")
    def _serialize_generated_at(self, value: datetime) -> str:
        return _format_utc_second(value)

    @model_validator(mode="after")
    def _validate_file_count(self) -> Self:
        if self.file_count != len(self.files):
            raise ValueError("file_count must equal len(files)")
        return self

    @classmethod
    def compute_corpus_hash(cls, files: list[N2FileEntry]) -> str:
        lines = "".join(
            f"{entry.relpath}\t{entry.sha256}\n"
            for entry in sorted(files, key=lambda entry: entry.relpath.encode("utf-8"))
        )
        return hashlib.sha256(lines.encode("utf-8")).hexdigest()

    def to_canonical_json(self) -> str:
        payload = self.model_dump(mode="json")
        return (
            json.dumps(payload, sort_keys=True, separators=(",", ": "), ensure_ascii=False) + "\n"
        )

    @classmethod
    def from_vault(
        cls,
        vault_name: str,
        vault_root: Path,
        ingest_include: list[str],
        ingest_exclude: list[str],
        file_paths: list[Path],
        generated_at: datetime | None = None,
    ) -> "N2Manifest":
        root = vault_root.expanduser().resolve()
        entries: list[N2FileEntry] = []

        for raw_path in file_paths:
            path = raw_path if raw_path.is_absolute() else root / raw_path
            path = path.expanduser().resolve()
            stat = path.stat()
            entries.append(
                N2FileEntry(
                    relpath=path.relative_to(root).as_posix(),
                    sha256=_sha256_file(path),
                    size_bytes=stat.st_size,
                    mtime_utc=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
                )
            )

        corpus_hash = cls.compute_corpus_hash(entries)
        return cls(
            manifest_version="n2.1",
            vault_name=vault_name,
            vault_root=root,
            generated_at=generated_at or datetime.now(timezone.utc),
            corpus_hash=corpus_hash,
            file_count=len(entries),
            files=entries,
            ingest_include=ingest_include,
            ingest_exclude=ingest_exclude,
        )
