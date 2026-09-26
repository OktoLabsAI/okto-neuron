"""Typed, off-graph identity decisions that are not equivalence classes.

ADR 0040 keeps positive ``same`` decisions in :mod:`okto_neuron.reconcile.authority`.
Type corrections, explicit ``distinct`` pairs, and ambiguous review evidence live
beside that index in ``decisions.json``.  This module deliberately has no graph or
review-queue dependency: reading or appending a decision cannot mutate either.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Iterator, Literal, Mapping, TypeAlias
from uuid import uuid4

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None  # type: ignore[assignment]

IDENTITY_DECISIONS = "decisions.json"
IDENTITY_DECISIONS_SCHEMA_VERSION = "identity_decisions.v1"
_IDENTITY_DECISIONS_LOCK = ".decisions.lock"

PrimitiveType: TypeAlias = Literal["Agent", "Activity", "InformationObject", "Concept", "Place"]

_PRIMITIVE_TYPES = frozenset({"Agent", "Activity", "InformationObject", "Concept", "Place"})
_COMMON_FIELDS = frozenset(
    {
        "kind",
        "decision_id",
        "reason",
        "evidence",
        "judge_model",
        "prompt_version",
        "semantic_policy_fingerprint",
        "created_at",
    }
)


class IdentityDecisionStoreError(ValueError):
    """The identity-decision side-file is corrupt or has an unsupported schema."""


def _nonempty(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IdentityDecisionStoreError(f"{field_name} must be a non-empty string")
    return value


def _primitive(value: object, field_name: str) -> PrimitiveType:
    raw = _nonempty(value, field_name)
    if raw not in _PRIMITIVE_TYPES:
        raise IdentityDecisionStoreError(f"{field_name} is not a closed primitive type: {raw!r}")
    return raw  # type: ignore[return-value]


def _evidence(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise IdentityDecisionStoreError("evidence must be an object")
    return deepcopy(value)


def _string_tuple(value: object, field_name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise IdentityDecisionStoreError(f"{field_name} must be an array")
    items = tuple(_nonempty(item, field_name) for item in value)
    if not items and not allow_empty:
        raise IdentityDecisionStoreError(f"{field_name} must not be empty")
    return items


def _reject_unknown_fields(data: Mapping[str, object], allowed: frozenset[str]) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise IdentityDecisionStoreError(f"unknown identity decision fields: {unknown!r}")


@dataclass(frozen=True)
class TypeCorrection:
    """A source-grounded correction from one closed primitive to another."""

    decision_id: str
    candidate_id: str
    previous_type: PrimitiveType
    corrected_type: PrimitiveType
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    judge_model: str = ""
    prompt_version: str = ""
    semantic_policy_fingerprint: str = ""
    created_at: str = ""

    kind: ClassVar[Literal["type_correction"]] = "type_correction"

    def __post_init__(self) -> None:
        _nonempty(self.decision_id, "decision_id")
        _nonempty(self.candidate_id, "candidate_id")
        _primitive(self.previous_type, "previous_type")
        _primitive(self.corrected_type, "corrected_type")
        _nonempty(self.reason, "reason")
        if self.previous_type == self.corrected_type:
            raise IdentityDecisionStoreError("a type correction must change the primitive type")
        object.__setattr__(self, "evidence", _evidence(self.evidence))

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "decision_id": self.decision_id,
            "candidate_id": self.candidate_id,
            "previous_type": self.previous_type,
            "corrected_type": self.corrected_type,
            "reason": self.reason,
            "evidence": dict(self.evidence),
            "judge_model": self.judge_model,
            "prompt_version": self.prompt_version,
            "semantic_policy_fingerprint": self.semantic_policy_fingerprint,
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> "TypeCorrection":
        _reject_unknown_fields(
            data,
            _COMMON_FIELDS | {"candidate_id", "previous_type", "corrected_type"},
        )
        return cls(
            decision_id=_nonempty(data.get("decision_id"), "decision_id"),
            candidate_id=_nonempty(data.get("candidate_id"), "candidate_id"),
            previous_type=_primitive(data.get("previous_type"), "previous_type"),
            corrected_type=_primitive(data.get("corrected_type"), "corrected_type"),
            reason=_nonempty(data.get("reason"), "reason"),
            evidence=_evidence(data.get("evidence")),
            judge_model=str(data.get("judge_model") or ""),
            prompt_version=str(data.get("prompt_version") or ""),
            semantic_policy_fingerprint=str(data.get("semantic_policy_fingerprint") or ""),
            created_at=str(data.get("created_at") or ""),
        )


@dataclass(frozen=True)
class DistinctDecision:
    """An unordered entity pair adjudicated as different senses.

    The ids are normalized on construction, making the persisted negative-cache
    key independent of comparison direction.
    """

    decision_id: str
    left_id: str
    right_id: str
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)
    judge_model: str = ""
    prompt_version: str = ""
    semantic_policy_fingerprint: str = ""
    created_at: str = ""

    kind: ClassVar[Literal["distinct"]] = "distinct"

    def __post_init__(self) -> None:
        _nonempty(self.decision_id, "decision_id")
        left = _nonempty(self.left_id, "left_id")
        right = _nonempty(self.right_id, "right_id")
        if left == right:
            raise IdentityDecisionStoreError("a distinct decision requires two different ids")
        left, right = sorted((left, right))
        object.__setattr__(self, "left_id", left)
        object.__setattr__(self, "right_id", right)
        _nonempty(self.reason, "reason")
        object.__setattr__(self, "evidence", _evidence(self.evidence))

    @property
    def pair(self) -> tuple[str, str]:
        return (self.left_id, self.right_id)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "decision_id": self.decision_id,
            "left_id": self.left_id,
            "right_id": self.right_id,
            "reason": self.reason,
            "evidence": dict(self.evidence),
            "judge_model": self.judge_model,
            "prompt_version": self.prompt_version,
            "semantic_policy_fingerprint": self.semantic_policy_fingerprint,
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> "DistinctDecision":
        _reject_unknown_fields(data, _COMMON_FIELDS | {"left_id", "right_id"})
        return cls(
            decision_id=_nonempty(data.get("decision_id"), "decision_id"),
            left_id=_nonempty(data.get("left_id"), "left_id"),
            right_id=_nonempty(data.get("right_id"), "right_id"),
            reason=_nonempty(data.get("reason"), "reason"),
            evidence=_evidence(data.get("evidence")),
            judge_model=str(data.get("judge_model") or ""),
            prompt_version=str(data.get("prompt_version") or ""),
            semantic_policy_fingerprint=str(data.get("semantic_policy_fingerprint") or ""),
            created_at=str(data.get("created_at") or ""),
        )


@dataclass(frozen=True)
class AmbiguousReview:
    """Evidence retained when type or identity cannot be decided safely."""

    decision_id: str
    candidate_ids: tuple[str, ...]
    reason: str
    possible_types: tuple[PrimitiveType, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)
    judge_model: str = ""
    prompt_version: str = ""
    semantic_policy_fingerprint: str = ""
    created_at: str = ""

    kind: ClassVar[Literal["ambiguous_review"]] = "ambiguous_review"

    def __post_init__(self) -> None:
        _nonempty(self.decision_id, "decision_id")
        candidate_ids = tuple(sorted(set(_string_tuple(self.candidate_ids, "candidate_ids"))))
        object.__setattr__(self, "candidate_ids", candidate_ids)
        possible_types = tuple(
            sorted(set(_primitive(value, "possible_types") for value in self.possible_types))
        )
        object.__setattr__(self, "possible_types", possible_types)
        _nonempty(self.reason, "reason")
        object.__setattr__(self, "evidence", _evidence(self.evidence))

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "decision_id": self.decision_id,
            "candidate_ids": list(self.candidate_ids),
            "reason": self.reason,
            "possible_types": list(self.possible_types),
            "evidence": dict(self.evidence),
            "judge_model": self.judge_model,
            "prompt_version": self.prompt_version,
            "semantic_policy_fingerprint": self.semantic_policy_fingerprint,
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> "AmbiguousReview":
        _reject_unknown_fields(data, _COMMON_FIELDS | {"candidate_ids", "possible_types"})
        possible_types = tuple(
            _primitive(value, "possible_types")
            for value in _string_tuple(
                data.get("possible_types", ()), "possible_types", allow_empty=True
            )
        )
        return cls(
            decision_id=_nonempty(data.get("decision_id"), "decision_id"),
            candidate_ids=_string_tuple(data.get("candidate_ids"), "candidate_ids"),
            reason=_nonempty(data.get("reason"), "reason"),
            possible_types=possible_types,
            evidence=_evidence(data.get("evidence")),
            judge_model=str(data.get("judge_model") or ""),
            prompt_version=str(data.get("prompt_version") or ""),
            semantic_policy_fingerprint=str(data.get("semantic_policy_fingerprint") or ""),
            created_at=str(data.get("created_at") or ""),
        )


IdentityDecision: TypeAlias = TypeCorrection | DistinctDecision | AmbiguousReview

_DECISION_TYPES = (TypeCorrection, DistinctDecision, AmbiguousReview)


def _decision_from_json(value: object) -> IdentityDecision:
    if not isinstance(value, dict):
        raise IdentityDecisionStoreError("each decision record must be an object")
    kind = value.get("kind")
    if kind == TypeCorrection.kind:
        return TypeCorrection.from_json(value)
    if kind == DistinctDecision.kind:
        return DistinctDecision.from_json(value)
    if kind == AmbiguousReview.kind:
        return AmbiguousReview.from_json(value)
    raise IdentityDecisionStoreError(f"unknown identity decision kind: {kind!r}")


class IdentityDecisionIndex:
    """Append-only ``identity_decisions.v1`` side-store in the authority directory."""

    def __init__(self, authority_dir: Path | str) -> None:
        self.dir = Path(authority_dir)
        self._records: list[IdentityDecision] = []
        self._distinct_pairs: frozenset[tuple[str, str]] = frozenset()
        self.load()

    @property
    def path(self) -> Path:
        return self.dir / IDENTITY_DECISIONS

    @property
    def lock_path(self) -> Path:
        return self.dir / _IDENTITY_DECISIONS_LOCK

    def load(self) -> None:
        """Load all decisions or reject the whole file on any corrupt record.

        A missing file is the safe legacy state: no corrections, no ambiguity,
        and no negative-cache hit.  Corrupt or future schemas never degrade to
        that state silently.
        """

        records = self._read_records_checked()
        self._publish(records)

    def append(self, decision: IdentityDecision) -> None:
        """Atomically append one typed decision, preserving record order."""

        if not isinstance(decision, _DECISION_TYPES):
            raise TypeError(f"unsupported identity decision: {type(decision).__name__}")
        self.dir.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.lock_path):
            records = self._read_records_checked()
            if any(existing.decision_id == decision.decision_id for existing in records):
                raise IdentityDecisionStoreError(
                    f"duplicate identity decision id: {decision.decision_id!r}"
                )
            updated = [*records, decision]
            _validate_type_correction_chains(updated)
            self._save(updated)

    def records(self) -> tuple[IdentityDecision, ...]:
        return tuple(self._records)

    def distinct_pairs(self) -> frozenset[tuple[str, str]]:
        """All direction-independent ``distinct`` negative-cache keys."""

        return self._distinct_pairs

    def corrected_type(self, candidate_id: str, current_type: PrimitiveType) -> PrimitiveType:
        """Fold a candidate's ordered type-correction chain.

        Each correction must start at the type produced by the preceding step.
        A stale or contradictory chain fails closed instead of guessing which
        correction should govern replay.
        """

        candidate_id = _nonempty(candidate_id, "candidate_id")
        resolved = _primitive(current_type, "current_type")
        for decision in self._records:
            if not isinstance(decision, TypeCorrection):
                continue
            if decision.candidate_id != candidate_id:
                continue
            if decision.previous_type != resolved:
                raise IdentityDecisionStoreError(
                    "type-correction chain does not match the current primitive"
                )
            resolved = decision.corrected_type
        return resolved

    def unresolved_ambiguous_candidate_ids(self) -> frozenset[str]:
        """Return candidates whose latest off-graph decision still requires review.

        Later explicit type corrections or ``distinct`` decisions resolve the
        corresponding earlier ambiguity without rewriting append-only history.
        """

        active: dict[str, AmbiguousReview] = {}
        for decision in self._records:
            if isinstance(decision, AmbiguousReview):
                active[decision.decision_id] = decision
            elif isinstance(decision, TypeCorrection):
                active = {
                    review_id: review
                    for review_id, review in active.items()
                    if not (review.possible_types and decision.candidate_id in review.candidate_ids)
                }
            elif isinstance(decision, DistinctDecision):
                active = {
                    review_id: review
                    for review_id, review in active.items()
                    if not (
                        not review.possible_types
                        and frozenset(review.candidate_ids) == frozenset(decision.pair)
                    )
                }
        return frozenset(
            candidate_id for review in active.values() for candidate_id in review.candidate_ids
        )

    def is_distinct(self, left_id: str, right_id: str) -> bool:
        """Return a fail-closed negative-cache hit without consulting a queue."""

        pair = tuple(sorted((_nonempty(left_id, "left_id"), _nonempty(right_id, "right_id"))))
        return pair in self._distinct_pairs

    def _read_records(self) -> list[IdentityDecision]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise IdentityDecisionStoreError("identity decisions root must be an object")
        _reject_unknown_fields(payload, frozenset({"schema_version", "records"}))
        if payload.get("schema_version") != IDENTITY_DECISIONS_SCHEMA_VERSION:
            raise IdentityDecisionStoreError(
                f"unsupported identity decisions schema: {payload.get('schema_version')!r}"
            )
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            raise IdentityDecisionStoreError("identity decisions records must be an array")
        records = [_decision_from_json(value) for value in raw_records]
        ids = [record.decision_id for record in records]
        if len(ids) != len(set(ids)):
            raise IdentityDecisionStoreError("duplicate identity decision id")
        _validate_type_correction_chains(records)
        return records

    def _read_records_checked(self) -> list[IdentityDecision]:
        try:
            return self._read_records()
        except IdentityDecisionStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise IdentityDecisionStoreError(f"invalid identity decisions file: {exc}") from exc

    def _publish(self, records: list[IdentityDecision]) -> None:
        self._records = records
        self._distinct_pairs = frozenset(
            decision.pair for decision in records if isinstance(decision, DistinctDecision)
        )

    def _save(self, records: list[IdentityDecision]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": IDENTITY_DECISIONS_SCHEMA_VERSION,
            "records": [record.to_json() for record in records],
        }
        encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
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
            self._publish(records)
        except BaseException:
            if handle is None:
                os.close(fd)
            temp_path.unlink(missing_ok=True)
            raise


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    """Serialize whole-file decision appends across processes."""

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
        else:  # pragma: no cover - every supported OS has one backend
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


def _validate_type_correction_chains(records: list[IdentityDecision]) -> None:
    latest: dict[str, PrimitiveType] = {}
    for decision in records:
        if not isinstance(decision, TypeCorrection):
            continue
        prior = latest.get(decision.candidate_id)
        if prior is not None and decision.previous_type != prior:
            raise IdentityDecisionStoreError(
                "type-correction chain does not match the prior corrected primitive"
            )
        latest[decision.candidate_id] = decision.corrected_type


__all__ = [
    "AmbiguousReview",
    "DistinctDecision",
    "IDENTITY_DECISIONS",
    "IDENTITY_DECISIONS_SCHEMA_VERSION",
    "IdentityDecision",
    "IdentityDecisionIndex",
    "IdentityDecisionStoreError",
    "PrimitiveType",
    "TypeCorrection",
]
