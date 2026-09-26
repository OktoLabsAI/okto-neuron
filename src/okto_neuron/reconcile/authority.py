"""Off-graph Authority index — the ONLY durable write of Option A.

ADR 0008. The reconciler materializes an equivalence class (a set of node ids
the judge ruled the same real-world entity) NOT as graph nodes/edges but as a
JSON side-file under ``<vault>/.marginalia/authority/index.json``. This is the
load-bearing safety property: :class:`okto_neuron.core.schema.legacy.Authority`
is a :class:`~okto_neuron.core.schema.Node` subclass, so calling
``store.add_node`` on one into a populated Ladybug graph IS the ADR-0007
edge-src/dst corruption. The Authority record is therefore strictly side-data,
consumed at read time (:func:`okto_neuron.query.search_claims`) and at heal time
(:func:`okto_neuron.reconcile.heal.heal_via_copy`).

SKOS vocabulary mapping (explicit, per the standards-alignment convention):

- ``canonical_name`` → ``skos:prefLabel``.
- each ``variants[i]``  → ``skos:altLabel``.
- each ``(canonical_id, member_id)`` → ``skos:exactMatch`` (NOT ``owl:sameAs``;
  exactMatch is the conservative, non-inferential equivalence predicate).

Reversibility is by construction: an auto-merge is undone by dropping its
record (:meth:`AuthorityIndex.remove`). Nothing in the graph changed, so there
is nothing to un-corrupt.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from okto_neuron.core.schema.legacy import Authority
from okto_neuron.semantic_surface import exact_surface_key

AUTHORITY_DIRNAME = "authority"
AUTHORITY_INDEX = "index.json"
SCHEMA_VERSION = "authority.v1"
PREDICATE = "skos:exactMatch"
_AUTHORITY_LOCK = ".index.lock"

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None  # type: ignore[assignment]


def _normalized_title(title: str) -> str:
    """The shared conservative exact key — the SAME key
    ``resolve._normalized_title`` / ``collapse_duplicates`` use, so the heal
    fold (Tier-0 ``(type, normalized-title)`` equality) agrees with what an
    alias 'is'."""
    return exact_surface_key(title)


@dataclass(frozen=True)
class AuthorityRecord:
    """One off-graph equivalence class. Serialized to ``index.json``; never a
    graph node/edge."""

    cluster_id: str
    canonical_id: str
    canonical_name: str
    member_ids: tuple[str, ...]
    variants: tuple[str, ...]
    exact_match_pairs: tuple[tuple[str, str], ...]
    verdict: str = "same"
    confidence: float = 0.0
    provenance: dict = field(default_factory=dict)

    # ── serialization ──────────────────────────────────────────────────────────
    def to_authority_node(self) -> Authority:
        """Build the SKOS Authority MODEL (canonical_name → prefLabel, variants →
        altLabel). Used for serialization shape ONLY — NEVER ``store.add_node``;
        committing this into a populated graph is the ADR-0007 corruption."""
        return Authority(
            canonical_name=self.canonical_name,
            variants=list(self.variants),
            title=self.canonical_name,
        )

    def to_json(self) -> dict:
        return {
            "cluster_id": self.cluster_id,
            "canonical_id": self.canonical_id,
            "canonical_name": self.canonical_name,
            "member_ids": list(self.member_ids),
            "variants": list(self.variants),
            "exact_match_pairs": [list(pair) for pair in self.exact_match_pairs],
            "verdict": self.verdict,
            "confidence": self.confidence,
            "provenance": dict(self.provenance),
        }

    @classmethod
    def from_json(cls, data: dict) -> "AuthorityRecord":
        return cls(
            cluster_id=str(data["cluster_id"]),
            canonical_id=str(data["canonical_id"]),
            canonical_name=str(data.get("canonical_name", "")),
            member_ids=tuple(str(m) for m in data.get("member_ids", ())),
            variants=tuple(str(v) for v in data.get("variants", ())),
            exact_match_pairs=tuple(
                (str(p[0]), str(p[1])) for p in data.get("exact_match_pairs", ())
            ),
            verdict=str(data.get("verdict", "same")),
            confidence=float(data.get("confidence", 0.0)),
            provenance=dict(data.get("provenance", {})),
        )


class AuthorityIndex:
    """The off-graph equivalence hub at ``<vault>/.marginalia/authority/``.

    Pure JSON persistence — no store reference, so it CANNOT write the graph by
    construction. ``upsert``/``remove`` keep ``index.json`` in sync; the read
    side consumes :meth:`equivalence_map`, the heal side
    :meth:`alias_canonical_map`.
    """

    def __init__(self, dir: Path | str) -> None:
        self.dir = Path(dir)
        self._records: dict[str, AuthorityRecord] = {}
        self.load()

    @property
    def path(self) -> Path:
        return self.dir / AUTHORITY_INDEX

    @property
    def lock_path(self) -> Path:
        return self.dir / _AUTHORITY_LOCK

    # ── persistence ────────────────────────────────────────────────────────────
    def load(self) -> None:
        self._records = self._read_records()

    def _read_records(self) -> dict[str, AuthorityRecord]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        records: dict[str, AuthorityRecord] = {}
        for rec in data.get("records", []):
            record = AuthorityRecord.from_json(rec)
            records[record.cluster_id] = record
        return records

    def _save(self, records: dict[str, AuthorityRecord]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "predicate": PREDICATE,
            "records": [rec.to_json() for rec in records.values()],
        }
        encoded = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
        for stale in self.dir.glob(f".{self.path.name}.*.tmp"):
            stale.unlink(missing_ok=True)
        temp_path = self.path.with_name(f".{self.path.name}.{uuid4().hex}.tmp")
        fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        handle = None
        try:
            handle = os.fdopen(fd, "wb", closefd=True)
            with handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            _replace_with_windows_retry(temp_path, self.path)
            _fsync_directory(self.dir)
            self._records = records
        except BaseException:
            if handle is None:
                os.close(fd)
            temp_path.unlink(missing_ok=True)
            raise

    # ── surface ──────────────────────────────────────────────────────────────--
    def upsert(self, rec: AuthorityRecord) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.lock_path):
            records = self._read_records()
            records[rec.cluster_id] = rec
            self._save(records)

    def remove(self, cluster_id: str) -> None:
        """REVERSIBILITY: drop a cluster's record. The auto-merge is undone with
        nothing to repair in the graph (Option A never touched it)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.lock_path):
            records = self._read_records()
            if cluster_id not in records:
                self._records = records
                return
            del records[cluster_id]
            self._save(records)

    def records(self) -> list[AuthorityRecord]:
        return list(self._records.values())

    def equivalence_map(self) -> dict[str, str]:
        """``member_id -> canonical_id`` for the query-time fold (§2). The
        canonical maps to itself so a class collapses onto one representative.

        Only positive identity decisions participate.  ``AuthorityRecord``
        predates the explicit identity-decision ledger, so legacy records with
        no ``verdict`` deserialize as ``same``; an explicit non-``same``
        verdict is never allowed to become an equivalence edge.
        """
        out: dict[str, str] = {}
        for rec in self._records.values():
            if rec.verdict != "same":
                continue
            for member_id in rec.member_ids:
                out[member_id] = rec.canonical_id
            out[rec.canonical_id] = rec.canonical_id
        return out

    def alias_canonical_map(self) -> dict[str, str]:
        """``normalized variant-title -> canonical_name``. A title→name projection of
        the records (kept for callers that need the title seam); the deterministic
        heal does NOT use this — it folds by node id via :meth:`equivalence_map`."""
        out: dict[str, str] = {}
        for rec in self._records.values():
            for variant in rec.variants:
                norm = _normalized_title(variant)
                if norm:
                    out[norm] = rec.canonical_name
        return out


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    with path.open("a+b") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - exercised on Windows
            lock_file.seek(0)
            if not lock_file.read(1):
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover
            raise OSError("no file locking backend available")
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - exercised on Windows
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _replace_with_windows_retry(source: Path, target: Path) -> None:
    attempts = 3 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(0.05 * (attempt + 1))


__all__ = [
    "AuthorityRecord",
    "AuthorityIndex",
    "AUTHORITY_DIRNAME",
    "AUTHORITY_INDEX",
    "SCHEMA_VERSION",
    "PREDICATE",
]
