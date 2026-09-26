"""Off-graph predicate alias ledger.

Predicate folds follow the same safety rule as entity Authority records: durable
decisions live beside the vault, not in the graph. Removing a record reverses the
fold because no graph edges or Claim facets were rewritten by this index.
"""

from __future__ import annotations

import json
import math
import os
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal
from uuid import uuid4

from okto_neuron.curator import _CORE_PREDICATE_ALIASES

PREDICATE_DIRNAME = "predicates"
PREDICATE_ALIASES = "aliases.json"
PREDICATE_SCHEMA_VERSION = "predicate_aliases.v1"
PREDICATE_EXACT_MATCH = "skos:exactMatch"
_PREDICATE_ALIASES_LOCK = ".aliases.lock"

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None  # type: ignore[assignment]

PredicateMapping = Literal["exact_match", "sub_property_of", "inverse_of", "distinct"]
PredicateStatus = Literal["auto", "confirmed", "rejected", "queued"]

_ACTIVE_STATUSES = frozenset({"auto", "confirmed"})
_MAPPINGS = frozenset({"exact_match", "sub_property_of", "inverse_of", "distinct"})
_STATUSES = frozenset({"auto", "confirmed", "rejected", "queued"})
_RECORD_FIELDS = frozenset(
    {
        "id",
        "subject_predicate",
        "mapping",
        "object_predicate",
        "confidence",
        "justification",
        "evidence",
        "judge_model",
        "votes",
        "status",
        "created_at",
    }
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pair_key(a: str, b: str) -> tuple[str, str]:
    left = str(a)
    right = str(b)
    return (left, right) if left <= right else (right, left)


@dataclass(frozen=True)
class PredicateAliasRecord:
    """One SSSOM-shaped predicate mapping decision."""

    id: str
    subject_predicate: str
    mapping: PredicateMapping
    object_predicate: str
    confidence: float
    justification: str
    evidence: dict = field(default_factory=dict)
    judge_model: str = ""
    votes: dict = field(default_factory=dict)
    status: PredicateStatus = "queued"
    created_at: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        for field_name in ("id", "subject_predicate", "object_predicate", "created_at"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"predicate alias {field_name} must be non-empty trimmed text")
        for field_name in ("justification", "judge_model"):
            if not isinstance(getattr(self, field_name), str):
                raise ValueError(f"predicate alias {field_name} must be text")
        try:
            created = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("predicate alias created_at must be ISO-8601") from exc
        if created.tzinfo is None:
            raise ValueError("predicate alias created_at must include a timezone")
        if self.mapping not in _MAPPINGS:
            raise ValueError(f"unknown predicate mapping: {self.mapping!r}")
        if self.status not in _STATUSES:
            raise ValueError(f"unknown predicate status: {self.status!r}")
        if (
            not isinstance(self.confidence, (int, float))
            or isinstance(self.confidence, bool)
            or not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise ValueError("predicate alias confidence must be finite and in [0, 1]")
        if not isinstance(self.evidence, dict) or not isinstance(self.votes, dict):
            raise ValueError("predicate alias evidence and votes must be objects")
        # Frozen records must not retain mutable dictionaries owned by callers.
        object.__setattr__(self, "evidence", deepcopy(self.evidence))
        object.__setattr__(self, "votes", deepcopy(self.votes))

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "subject_predicate": self.subject_predicate,
            "mapping": self.mapping,
            "object_predicate": self.object_predicate,
            "confidence": self.confidence,
            "justification": self.justification,
            "evidence": dict(self.evidence),
            "judge_model": self.judge_model,
            "votes": dict(self.votes),
            "status": self.status,
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, data: dict) -> "PredicateAliasRecord":
        if not isinstance(data, dict) or set(data) != _RECORD_FIELDS:
            raise ValueError("predicate alias record has missing or unknown fields")
        return cls(
            id=data["id"],
            subject_predicate=data["subject_predicate"],
            mapping=data["mapping"],  # type: ignore[arg-type]
            object_predicate=data["object_predicate"],
            confidence=data["confidence"],
            justification=data["justification"],
            evidence=data["evidence"],
            judge_model=data["judge_model"],
            votes=data["votes"],
            status=data["status"],  # type: ignore[arg-type]
            created_at=data["created_at"],
        )


class PredicateAliasIndex:
    """JSON predicate alias ledger at ``<vault>/.marginalia/predicates/``."""

    def __init__(self, vault: Path | str) -> None:
        self.vault = Path(vault)
        self.dir = self.vault / ".marginalia" / PREDICATE_DIRNAME
        self._records: dict[str, PredicateAliasRecord] = {}
        self.load()

    @property
    def path(self) -> Path:
        return self.dir / PREDICATE_ALIASES

    def load(self) -> None:
        self._records = self._read_records()

    @property
    def lock_path(self) -> Path:
        return self.dir / _PREDICATE_ALIASES_LOCK

    def _read_records(self) -> dict[str, PredicateAliasRecord]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict) or set(data) != {
            "schema_version",
            "predicate",
            "records",
        }:
            raise ValueError("predicate alias ledger has missing or unknown fields")
        if data["schema_version"] != PREDICATE_SCHEMA_VERSION:
            raise ValueError(f"unsupported predicate alias schema: {data['schema_version']!r}")
        if data["predicate"] != PREDICATE_EXACT_MATCH:
            raise ValueError("predicate alias ledger declares an unexpected predicate")
        if not isinstance(data["records"], list):
            raise ValueError("predicate alias records must be an array")
        records: dict[str, PredicateAliasRecord] = {}
        for raw in data["records"]:
            record = PredicateAliasRecord.from_json(raw)
            if record.id in records:
                raise ValueError(f"duplicate predicate alias id: {record.id}")
            records[record.id] = record
        return records

    def _save(self, records: dict[str, PredicateAliasRecord]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": PREDICATE_SCHEMA_VERSION,
            "predicate": PREDICATE_EXACT_MATCH,
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

    def upsert(self, rec: PredicateAliasRecord) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.lock_path):
            records = self._read_records()
            records[rec.id] = rec
            self._save(records)

    def remove(self, record_id: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.lock_path):
            records = self._read_records()
            if record_id not in records:
                self._records = records
                return
            del records[record_id]
            self._save(records)

    def records(self) -> list[PredicateAliasRecord]:
        return list(self._records.values())

    def judged_pairs(self) -> set[tuple[str, str]]:
        """Unordered predicate-pair cache, including rejected negatives."""
        return {
            _pair_key(rec.subject_predicate, rec.object_predicate) for rec in self._records.values()
        }

    def alias_map(self) -> dict[str, str]:
        """Return active exact-match folds as ``predicate -> canonical``.

        Only ``auto``/``confirmed`` exact matches participate. Union-find collapses
        chains into one root; conflicting cycles choose the predicate with the
        highest evidence count. Core aliases keep precedence and cannot be
        overwritten by vault-local records.
        """
        records = [
            rec
            for rec in self._records.values()
            if rec.status in _ACTIVE_STATUSES and rec.mapping == "exact_match"
        ]
        if not records:
            return {}

        parent: dict[str, str] = {}
        evidence_counts: dict[str, int] = {}
        first_seen: dict[str, int] = {}

        def ensure(pred: str) -> None:
            if pred not in parent:
                parent[pred] = pred
                first_seen[pred] = len(first_seen)

        def find(pred: str) -> str:
            ensure(pred)
            while parent[pred] != pred:
                parent[pred] = parent[parent[pred]]
                pred = parent[pred]
            return pred

        def union(a: str, b: str) -> None:
            root_a = find(a)
            root_b = find(b)
            if root_a != root_b:
                parent[root_b] = root_a

        for rec in records:
            ensure(rec.subject_predicate)
            ensure(rec.object_predicate)
            for pred, count in _record_evidence_counts(rec).items():
                evidence_counts[pred] = max(evidence_counts.get(pred, 0), count)
            union(rec.subject_predicate, rec.object_predicate)

        components: dict[str, list[str]] = {}
        for pred in parent:
            components.setdefault(find(pred), []).append(pred)

        out: dict[str, str] = {}
        for members in components.values():
            root = max(
                members,
                key=lambda pred: (
                    evidence_counts.get(pred, 0),
                    -first_seen[pred],
                    pred,
                ),
            )
            canonical = _CORE_PREDICATE_ALIASES.get(root, root)
            for pred in sorted(members):
                if pred in _CORE_PREDICATE_ALIASES:
                    out[pred] = _CORE_PREDICATE_ALIASES[pred]
                    continue
                if pred != canonical:
                    out[pred] = canonical
        return out

    def inverse_map(self) -> dict[str, str]:
        """Return confirmed inverse pairs, rejecting ambiguous direction policy."""

        targets: dict[str, set[str]] = {}
        for rec in self._records.values():
            if rec.status != "confirmed" or rec.mapping != "inverse_of":
                continue
            targets.setdefault(rec.subject_predicate, set()).add(rec.object_predicate)
            targets.setdefault(rec.object_predicate, set()).add(rec.subject_predicate)
        conflicts = {
            predicate: sorted(values) for predicate, values in targets.items() if len(values) != 1
        }
        if conflicts:
            raise ValueError(f"conflicting confirmed predicate inverses: {conflicts}")
        return {predicate: next(iter(values)) for predicate, values in targets.items()}


def _record_evidence_counts(rec: PredicateAliasRecord) -> dict[str, int]:
    raw = rec.evidence.get("counts") if isinstance(rec.evidence, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    aliases = {
        "subject": rec.subject_predicate,
        "subject_predicate": rec.subject_predicate,
        "object": rec.object_predicate,
        "object_predicate": rec.object_predicate,
    }
    for key, value in raw.items():
        pred = aliases.get(str(key), str(key))
        try:
            count = int(value)
        except (TypeError, ValueError):
            continue
        out[pred] = max(out.get(pred, 0), count)
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
    "PREDICATE_ALIASES",
    "PREDICATE_DIRNAME",
    "PREDICATE_EXACT_MATCH",
    "PREDICATE_SCHEMA_VERSION",
    "PredicateAliasIndex",
    "PredicateAliasRecord",
    "PredicateMapping",
    "PredicateStatus",
]
