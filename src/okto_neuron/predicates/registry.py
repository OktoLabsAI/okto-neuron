"""Strict, off-graph predicate lifecycle registry for ADR 0040 D5.

Alias and inverse relationships remain in :mod:`okto_neuron.predicates.index`.
This sibling store records the governed predicates themselves.  It has no graph,
LLM, planner, or review-queue dependency and therefore cannot change live graph
semantics merely by being read or updated.
"""

from __future__ import annotations

import json
import math
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, ClassVar, Iterable, Iterator, Literal, Mapping, TypeAlias
from uuid import uuid4

from okto_neuron.curator import (
    _CORE_PREDICATE_ALIASES,
    _SDLC_PREDICATE_ALIASES,
    normalize_predicate,
)

from .index import PREDICATE_DIRNAME

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None  # type: ignore[assignment]

PREDICATE_REGISTRY = "registry.json"
PREDICATE_REGISTRY_SCHEMA_VERSION = "predicate_registry.v1"
_PREDICATE_REGISTRY_LOCK = ".registry.lock"

PrimitiveType: TypeAlias = Literal["Agent", "Activity", "InformationObject", "Concept", "Place"]
PredicateObjectType: TypeAlias = PrimitiveType | Literal["literal"]
PredicateLifecycle: TypeAlias = Literal["canonical", "provisional"]
PredicateDirection: TypeAlias = Literal["subject_to_object", "symmetric", "unknown"]
PredicateDecisionSource: TypeAlias = Literal["core", "pack", "human", "model"]

_PRIMITIVE_TYPES = frozenset({"Agent", "Activity", "InformationObject", "Concept", "Place"})
_OBJECT_TYPES = _PRIMITIVE_TYPES | {"literal"}
_LIFECYCLES = frozenset({"canonical", "provisional"})
_DIRECTIONS = frozenset({"subject_to_object", "symmetric", "unknown"})
_DECISION_SOURCES = frozenset({"core", "pack", "human", "model"})
_MAX_SAMPLES = 20
DENIED_PREDICATE_LABELS = frozenset(
    {
        "unknown",
        "none",
        "null",
        "n_a",
        "na",
        "undefined",
        "and",
        "or",
        "the",
        "of",
        "to",
        "is",
        "was",
        "were",
        "with",
        "from",
        "by",
    }
)

_CORE_PREDICATE_DEFINITIONS: Mapping[str, tuple[str, PredicateDirection, bool | None]] = {
    "author_of": ("The subject agent authored the object work.", "subject_to_object", False),
    "authored_by": (
        "The subject work was authored by the object agent.",
        "subject_to_object",
        False,
    ),
    "compiled_by": (
        "The subject work was compiled by the object agent.",
        "subject_to_object",
        False,
    ),
    "compiler_of": ("The subject agent compiled the object work.", "subject_to_object", False),
    "constrains": ("The subject limits or bounds the object.", "subject_to_object", False),
    "contrasts_with": ("The subject is explicitly contrasted with the object.", "symmetric", True),
    "contributor_to": (
        "The subject agent contributed to the object work.",
        "subject_to_object",
        False,
    ),
    "defines": ("The subject supplies the definition of the object.", "subject_to_object", False),
    "describes": ("The subject describes the object.", "subject_to_object", False),
    "edited_by": ("The subject work was edited by the object agent.", "subject_to_object", False),
    "editor_of": ("The subject agent edited the object work.", "subject_to_object", False),
    "example": (
        "The subject is presented as an example of the object.",
        "subject_to_object",
        False,
    ),
    "includes": (
        "The subject includes the object as a member or part.",
        "subject_to_object",
        False,
    ),
    "progresses_to": (
        "The subject stage progresses to the object stage.",
        "subject_to_object",
        False,
    ),
    "recommended_approach": (
        "The subject recommends the object as an approach.",
        "subject_to_object",
        False,
    ),
    "requires": ("The subject requires the object.", "subject_to_object", False),
    "uses": ("The subject uses the object.", "subject_to_object", False),
    "wrote_to": (
        "The subject agent wrote an addressed communication to the object.",
        "subject_to_object",
        False,
    ),
    "has_value": ("The subject has the stated literal value.", "subject_to_object", False),
}

_PACK_PREDICATE_DEFINITIONS: Mapping[
    str, Mapping[str, tuple[str, PredicateDirection, bool | None]]
] = {
    "sdlc": {
        "enables": ("The subject makes the object possible.", "subject_to_object", False),
        "impact": ("The subject has the stated impact on the object.", "subject_to_object", False),
        "mitigates": (
            "The subject reduces or counters the object risk.",
            "subject_to_object",
            False,
        ),
        "risk": ("The subject presents the object as a risk.", "subject_to_object", False),
        "validated_by": ("The subject is validated by the object.", "subject_to_object", False),
    }
}
_BUILTIN_CREATED_AT = "2026-07-16T00:00:00+00:00"
_BUILTIN_POLICY_ID = "builtin:predicate_registry.v1"


class PredicateRegistryError(ValueError):
    """The predicate registry or one proposed record is invalid."""


def render_registry_block(
    records: Mapping[str, PredicateRecord],
    *,
    cap: int = 200,
) -> str:
    """Render the live registry as byte-stable prompt text.

    Ordering is deterministic and independent of ``records`` insertion order:
    canonical before provisional, then support descending, then label ascending.
    When ``cap`` would truncate, every canonical record is retained (they are
    policy, not evidence) and the highest-support provisionals fill the rest;
    a trailing marker states how many rows were dropped so the reader never
    mistakes a truncated list for the whole vocabulary.
    """

    if not isinstance(records, Mapping):
        raise TypeError("records must be a mapping of label to PredicateRecord")
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < 1:
        raise PredicateRegistryError("cap must be an integer >= 1")
    ordered = sorted(
        records.values(),
        key=lambda record: (
            0 if record.lifecycle == "canonical" else 1,
            -record.support_count,
            record.label,
        ),
    )
    dropped = 0
    if len(ordered) > cap:
        canonicals = [record for record in ordered if record.lifecycle == "canonical"]
        provisionals = [record for record in ordered if record.lifecycle != "canonical"]
        room = max(cap - len(canonicals), 0)
        dropped = len(provisionals) - room
        ordered = canonicals + provisionals[:room]
    lines = [
        f"{record.label} | {record.direction} | support={record.support_count} | "
        f"{record.definition}"
        for record in ordered
    ]
    if dropped > 0:
        lines.append(f"... {dropped} further low-support predicates omitted")
    return "\n".join(lines)


def _text(value: object, field_name: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise PredicateRegistryError(f"{field_name} must be text")
    if not allow_empty and not value.strip():
        raise PredicateRegistryError(f"{field_name} must be non-empty text")
    if value != value.strip():
        raise PredicateRegistryError(f"{field_name} must be trimmed")
    return value


def _reject_unknown_fields(
    value: Mapping[str, object], expected: frozenset[str], field_name: str
) -> None:
    fields = set(value)
    missing = sorted(expected - fields)
    unknown = sorted(fields - expected)
    if missing or unknown:
        raise PredicateRegistryError(
            f"{field_name} fields invalid: missing={missing}, unknown={unknown}"
        )


@dataclass(frozen=True, order=True)
class PredicateTypeSignature:
    subject_type: PrimitiveType
    object_type: PredicateObjectType
    count: int

    _FIELDS: ClassVar[frozenset[str]] = frozenset({"subject_type", "object_type", "count"})

    def __post_init__(self) -> None:
        if self.subject_type not in _PRIMITIVE_TYPES:
            raise PredicateRegistryError("subject_type must be a closed primitive")
        if self.object_type not in _OBJECT_TYPES:
            raise PredicateRegistryError("object_type must be a closed primitive or literal")
        if not isinstance(self.count, int) or isinstance(self.count, bool) or self.count < 1:
            raise PredicateRegistryError("signature count must be an integer >= 1")

    def to_json(self) -> dict[str, object]:
        return {
            "subject_type": self.subject_type,
            "object_type": self.object_type,
            "count": self.count,
        }

    @classmethod
    def from_json(cls, value: object) -> "PredicateTypeSignature":
        if not isinstance(value, dict):
            raise PredicateRegistryError("type signature must be an object")
        _reject_unknown_fields(value, cls._FIELDS, "type signature")
        return cls(
            subject_type=value["subject_type"],  # type: ignore[arg-type]
            object_type=value["object_type"],  # type: ignore[arg-type]
            count=value["count"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, order=True)
class PredicateEvidenceSample:
    claim_id: str
    source_id: str

    _FIELDS: ClassVar[frozenset[str]] = frozenset({"claim_id", "source_id"})

    def __post_init__(self) -> None:
        _text(self.claim_id, "claim_id")
        _text(self.source_id, "source_id")

    def to_json(self) -> dict[str, str]:
        return {"claim_id": self.claim_id, "source_id": self.source_id}

    @classmethod
    def from_json(cls, value: object) -> "PredicateEvidenceSample":
        if not isinstance(value, dict):
            raise PredicateRegistryError("predicate sample must be an object")
        _reject_unknown_fields(value, cls._FIELDS, "predicate sample")
        return cls(
            claim_id=_text(value["claim_id"], "claim_id"),
            source_id=_text(value["source_id"], "source_id"),
        )


@dataclass(frozen=True)
class PredicateDecisionProvenance:
    source: PredicateDecisionSource
    decision_id: str
    judge_model: str
    prompt_version: str
    semantic_policy_fingerprint: str
    created_at: str

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "source",
            "decision_id",
            "judge_model",
            "prompt_version",
            "semantic_policy_fingerprint",
            "created_at",
        }
    )

    def __post_init__(self) -> None:
        if self.source not in _DECISION_SOURCES:
            raise PredicateRegistryError(f"unknown predicate decision source: {self.source!r}")
        _text(self.decision_id, "decision_id")
        _text(self.judge_model, "judge_model", allow_empty=True)
        _text(self.prompt_version, "prompt_version", allow_empty=True)
        _text(self.semantic_policy_fingerprint, "semantic_policy_fingerprint")
        created_at = _text(self.created_at, "created_at")
        try:
            timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PredicateRegistryError("created_at must be ISO-8601") from exc
        if timestamp.tzinfo is None:
            raise PredicateRegistryError("created_at must include a timezone")
        if self.source == "model" and (not self.judge_model or not self.prompt_version):
            raise PredicateRegistryError("model decisions require judge_model and prompt_version")

    def to_json(self) -> dict[str, str]:
        return {
            "source": self.source,
            "decision_id": self.decision_id,
            "judge_model": self.judge_model,
            "prompt_version": self.prompt_version,
            "semantic_policy_fingerprint": self.semantic_policy_fingerprint,
            "created_at": self.created_at,
        }

    @classmethod
    def from_json(cls, value: object) -> "PredicateDecisionProvenance":
        if not isinstance(value, dict):
            raise PredicateRegistryError("predicate decision provenance must be an object")
        _reject_unknown_fields(value, cls._FIELDS, "predicate decision provenance")
        return cls(
            source=value["source"],  # type: ignore[arg-type]
            decision_id=_text(value["decision_id"], "decision_id"),
            judge_model=_text(value["judge_model"], "judge_model", allow_empty=True),
            prompt_version=_text(value["prompt_version"], "prompt_version", allow_empty=True),
            semantic_policy_fingerprint=_text(
                value["semantic_policy_fingerprint"], "semantic_policy_fingerprint"
            ),
            created_at=_text(value["created_at"], "created_at"),
        )


@dataclass(frozen=True)
class PredicateRecord:
    label: str
    lifecycle: PredicateLifecycle
    definition: str
    direction: PredicateDirection
    symmetric: bool | None
    signatures: tuple[PredicateTypeSignature, ...]
    support_count: int
    samples: tuple[PredicateEvidenceSample, ...]
    confidence: float
    provenance: PredicateDecisionProvenance

    _FIELDS: ClassVar[frozenset[str]] = frozenset(
        {
            "label",
            "lifecycle",
            "definition",
            "direction",
            "symmetric",
            "signatures",
            "support_count",
            "samples",
            "confidence",
            "provenance",
        }
    )

    def __post_init__(self) -> None:
        label = _text(self.label, "label")
        if normalize_predicate(label) != label:
            raise PredicateRegistryError("label must be canonical lower_snake_case")
        if label in DENIED_PREDICATE_LABELS:
            raise PredicateRegistryError("label is denied by predicate registry policy")
        if self.lifecycle not in _LIFECYCLES:
            raise PredicateRegistryError(f"unknown predicate lifecycle: {self.lifecycle!r}")
        definition = _text(self.definition, "definition")
        if "\n" in definition or "\r" in definition or len(definition) > 500:
            raise PredicateRegistryError("definition must be one line of at most 500 characters")
        if self.direction not in _DIRECTIONS:
            raise PredicateRegistryError(f"unknown predicate direction: {self.direction!r}")
        if self.symmetric is not None and not isinstance(self.symmetric, bool):
            raise PredicateRegistryError("symmetric must be boolean or null")
        expected_symmetry = {
            "subject_to_object": False,
            "symmetric": True,
            "unknown": None,
        }[self.direction]
        if self.symmetric is not expected_symmetry:
            raise PredicateRegistryError("direction and symmetric state are inconsistent")
        if not isinstance(self.support_count, int) or isinstance(self.support_count, bool):
            raise PredicateRegistryError("support_count must be an integer")
        if self.support_count < 0:
            raise PredicateRegistryError("support_count must be >= 0")
        if not all(isinstance(value, PredicateTypeSignature) for value in self.signatures):
            raise PredicateRegistryError("signatures must contain PredicateTypeSignature values")
        signature_keys = [(value.subject_type, value.object_type) for value in self.signatures]
        if len(signature_keys) != len(set(signature_keys)):
            raise PredicateRegistryError("duplicate predicate type signature")
        signatures = tuple(sorted(self.signatures))
        if sum(value.count for value in signatures) > self.support_count:
            raise PredicateRegistryError("signature counts cannot exceed support_count")
        object.__setattr__(self, "signatures", signatures)
        if not all(isinstance(value, PredicateEvidenceSample) for value in self.samples):
            raise PredicateRegistryError("samples must contain PredicateEvidenceSample values")
        if len(self.samples) != len(set(self.samples)):
            raise PredicateRegistryError("duplicate predicate evidence sample")
        samples = tuple(sorted(self.samples))
        if len(samples) > _MAX_SAMPLES:
            raise PredicateRegistryError(f"samples must contain at most {_MAX_SAMPLES} rows")
        if len({sample.claim_id for sample in samples}) > self.support_count:
            raise PredicateRegistryError("sample claim count cannot exceed support_count")
        object.__setattr__(self, "samples", samples)
        if (
            not isinstance(self.confidence, (int, float))
            or isinstance(self.confidence, bool)
            or not math.isfinite(float(self.confidence))
            or not 0.0 <= float(self.confidence) <= 1.0
        ):
            raise PredicateRegistryError("confidence must be a finite number in [0, 1]")
        object.__setattr__(self, "confidence", float(self.confidence))
        if not isinstance(self.provenance, PredicateDecisionProvenance):
            raise PredicateRegistryError("provenance must be PredicateDecisionProvenance")

    def to_json(self) -> dict[str, object]:
        return {
            "label": self.label,
            "lifecycle": self.lifecycle,
            "definition": self.definition,
            "direction": self.direction,
            "symmetric": self.symmetric,
            "signatures": [value.to_json() for value in self.signatures],
            "support_count": self.support_count,
            "samples": [value.to_json() for value in self.samples],
            "confidence": self.confidence,
            "provenance": self.provenance.to_json(),
        }

    @classmethod
    def from_json(cls, value: object) -> "PredicateRecord":
        if not isinstance(value, dict):
            raise PredicateRegistryError("predicate record must be an object")
        _reject_unknown_fields(value, cls._FIELDS, "predicate record")
        raw_signatures = value["signatures"]
        raw_samples = value["samples"]
        if not isinstance(raw_signatures, list):
            raise PredicateRegistryError("signatures must be an array")
        if not isinstance(raw_samples, list):
            raise PredicateRegistryError("samples must be an array")
        return cls(
            label=_text(value["label"], "label"),
            lifecycle=value["lifecycle"],  # type: ignore[arg-type]
            definition=_text(value["definition"], "definition"),
            direction=value["direction"],  # type: ignore[arg-type]
            symmetric=value["symmetric"],  # type: ignore[arg-type]
            signatures=tuple(PredicateTypeSignature.from_json(row) for row in raw_signatures),
            support_count=value["support_count"],  # type: ignore[arg-type]
            samples=tuple(PredicateEvidenceSample.from_json(row) for row in raw_samples),
            confidence=value["confidence"],  # type: ignore[arg-type]
            provenance=PredicateDecisionProvenance.from_json(value["provenance"]),
        )


def builtin_predicate_records(packs: Iterable[str] = ()) -> tuple[PredicateRecord, ...]:
    """Return deterministic canonical records required by core and enabled packs."""

    core_targets = set(_CORE_PREDICATE_ALIASES.values())
    if not core_targets <= set(_CORE_PREDICATE_DEFINITIONS):
        missing = sorted(core_targets - set(_CORE_PREDICATE_DEFINITIONS))
        raise PredicateRegistryError(f"core predicate definitions missing: {missing}")

    definitions: dict[str, tuple[str, PredicateDirection, bool | None, PredicateDecisionSource]] = {
        label: (*definition, "core") for label, definition in _CORE_PREDICATE_DEFINITIONS.items()
    }
    for pack in sorted({str(value).strip().casefold() for value in packs if str(value).strip()}):
        pack_definitions = _PACK_PREDICATE_DEFINITIONS.get(pack, {})
        pack_targets = set(_SDLC_PREDICATE_ALIASES.values()) if pack == "sdlc" else set()
        if not pack_targets <= (set(definitions) | set(pack_definitions)):
            missing = sorted(pack_targets - set(definitions) - set(pack_definitions))
            raise PredicateRegistryError(
                f"pack predicate definitions missing for {pack!r}: {missing}"
            )
        for label, definition in pack_definitions.items():
            definitions.setdefault(label, (*definition, "pack"))

    records: list[PredicateRecord] = []
    for label in sorted(definitions):
        definition, direction, symmetric, source = definitions[label]
        records.append(
            PredicateRecord(
                label=label,
                lifecycle="canonical",
                definition=definition,
                direction=direction,
                symmetric=symmetric,
                signatures=(),
                support_count=0,
                samples=(),
                confidence=1.0,
                provenance=PredicateDecisionProvenance(
                    source=source,
                    decision_id=f"{source}:{label}:v1",
                    judge_model="",
                    prompt_version="",
                    semantic_policy_fingerprint=_BUILTIN_POLICY_ID,
                    created_at=_BUILTIN_CREATED_AT,
                ),
            )
        )
    return tuple(records)


def builtin_predicate_aliases(packs: Iterable[str] = ()) -> dict[str, str]:
    """Return the exact aliases contributed by core and enabled packs.

    Core mappings are immutable policy. Packs may add aliases, but cannot
    replace a core source mapping.
    """

    aliases = dict(_CORE_PREDICATE_ALIASES)
    enabled = {str(value).strip().casefold() for value in packs if str(value).strip()}
    if "sdlc" in enabled:
        for source, target in _SDLC_PREDICATE_ALIASES.items():
            aliases.setdefault(source, target)
    return aliases


class PredicateRegistry:
    """Atomic ``predicate_registry.v1`` side-store under one vault.

    Records reject fixed core aliases. Pack and vault mappings are contextual and
    therefore remain the responsibility of the D6 admission snapshot.
    """

    def __init__(self, vault: Path | str) -> None:
        self.vault = Path(vault)
        self.dir = self.vault / ".marginalia" / PREDICATE_DIRNAME
        self._records: dict[str, PredicateRecord] = {}
        self.load()

    @property
    def path(self) -> Path:
        return self.dir / PREDICATE_REGISTRY

    @property
    def lock_path(self) -> Path:
        return self.dir / _PREDICATE_REGISTRY_LOCK

    def load(self) -> None:
        self._publish([])
        self._publish(self._read_records_checked())

    def records(self) -> tuple[PredicateRecord, ...]:
        return tuple(self._records[label] for label in sorted(self._records))

    def policy_projection(self, packs: Iterable[str] = ()) -> dict[str, object]:
        """Return only fields that can change D6 admission semantics.

        Evidence and provenance remain durable in the same registry file, but
        their ordinary growth must not invalidate unrelated in-flight runs.
        Deterministic core/pack records are projected even before bootstrap has
        materialized them, so first-run seeding cannot change the run policy.
        """

        records = {record.label: record for record in builtin_predicate_records(packs)}
        records.update({record.label: record for record in self.records()})

        return {
            "schema_version": PREDICATE_REGISTRY_SCHEMA_VERSION,
            "records": [
                {
                    "label": record.label,
                    "lifecycle": record.lifecycle,
                    "definition": record.definition,
                    "direction": record.direction,
                    "symmetric": record.symmetric,
                }
                for record in (records[label] for label in sorted(records))
            ],
        }

    def labels(self) -> frozenset[str]:
        return frozenset(self._records)

    def get(self, label: str) -> PredicateRecord | None:
        if not isinstance(label, str) or not label or label != label.strip():
            return None
        return self._records.get(label)

    def upsert(self, record: PredicateRecord) -> None:
        if not isinstance(record, PredicateRecord):
            raise TypeError("record must be a PredicateRecord")
        self.dir.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.lock_path):
            records = self._read_records_checked()
            by_label = {value.label: value for value in records}
            by_label[record.label] = record
            self._save([by_label[label] for label in sorted(by_label)])

    def seed_builtins(self, packs: Iterable[str] = ()) -> tuple[str, ...]:
        """Atomically add missing core/pack records without replacing decisions.

        Callers must run this bootstrap before computing a run fingerprint; it
        is not a mid-run registrar.
        """

        desired = builtin_predicate_records(packs)
        self.dir.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.lock_path):
            records = self._read_records_checked()
            by_label = {value.label: value for value in records}
            added: list[str] = []
            for record in desired:
                if record.label not in by_label:
                    by_label[record.label] = record
                    added.append(record.label)
            if added:
                self._save([by_label[label] for label in sorted(by_label)])
            else:
                self._publish(records)
            return tuple(added)

    def apply_planned(
        self,
        record: PredicateRecord,
        *,
        expected_before: PredicateRecord | None,
    ) -> PredicateRecord:
        """Compare-and-set one absolute planned record, idempotently.

        A lost receipt may replay the exact post-state safely. Any unrelated
        sidefile drift fails closed rather than overwriting a newer decision.
        """

        if not isinstance(record, PredicateRecord):
            raise TypeError("record must be a PredicateRecord")
        if expected_before is not None and not isinstance(expected_before, PredicateRecord):
            raise TypeError("expected_before must be a PredicateRecord or None")
        if expected_before is not None and expected_before.label != record.label:
            raise PredicateRegistryError("planned predicate cannot rename a record")
        if (
            expected_before is not None
            and expected_before.lifecycle == "canonical"
            and record.lifecycle == "provisional"
        ):
            raise PredicateRegistryError("planned predicate cannot downgrade canonical")

        self.dir.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.lock_path):
            records = self._read_records_checked()
            by_label = {value.label: value for value in records}
            current = by_label.get(record.label)
            if current == record:
                self._publish(records)
                return current
            if current != expected_before:
                raise PredicateRegistryError(
                    f"planned predicate precondition changed: {record.label!r}"
                )
            by_label[record.label] = record
            self._save([by_label[label] for label in sorted(by_label)])
            return record

    def update(
        self,
        label: str,
        transform: Callable[[PredicateRecord], PredicateRecord],
    ) -> PredicateRecord:
        """Atomically transform one existing label under the cross-process lock.

        ``transform`` must be a pure in-memory callback; it must not call registry
        mutation methods recursively while this lock is held.
        """

        label = _text(label, "label")
        if not callable(transform):
            raise TypeError("transform must be callable")
        self.dir.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.lock_path):
            records = self._read_records_checked()
            by_label = {value.label: value for value in records}
            current = by_label.get(label)
            if current is None:
                raise PredicateRegistryError(f"predicate is not registered: {label!r}")
            replacement = transform(current)
            if not isinstance(replacement, PredicateRecord):
                raise TypeError("transform must return a PredicateRecord")
            if replacement.label != label:
                raise PredicateRegistryError("atomic update cannot rename a predicate")
            by_label[label] = replacement
            self._save([by_label[key] for key in sorted(by_label)])
            return replacement

    def _read_records_checked(self) -> list[PredicateRecord]:
        try:
            return self._read_records()
        except PredicateRegistryError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise PredicateRegistryError(f"invalid predicate registry: {exc}") from exc

    def _read_records(self) -> list[PredicateRecord]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise PredicateRegistryError("predicate registry root must be an object")
        _reject_unknown_fields(
            payload,
            frozenset({"schema_version", "records"}),
            "predicate registry",
        )
        if payload["schema_version"] != PREDICATE_REGISTRY_SCHEMA_VERSION:
            raise PredicateRegistryError(
                f"unsupported predicate registry schema: {payload['schema_version']!r}"
            )
        raw_records = payload["records"]
        if not isinstance(raw_records, list):
            raise PredicateRegistryError("predicate registry records must be an array")
        records = [PredicateRecord.from_json(value) for value in raw_records]
        labels = [record.label for record in records]
        if len(labels) != len(set(labels)):
            raise PredicateRegistryError("duplicate predicate registry label")
        return records

    def _save(self, records: list[PredicateRecord]) -> None:
        payload = {
            "schema_version": PREDICATE_REGISTRY_SCHEMA_VERSION,
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

    def _publish(self, records: list[PredicateRecord]) -> None:
        self._records = {record.label: record for record in records}


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


__all__ = [
    "DENIED_PREDICATE_LABELS",
    "PREDICATE_REGISTRY",
    "PREDICATE_REGISTRY_SCHEMA_VERSION",
    "PredicateDecisionProvenance",
    "PredicateDecisionSource",
    "PredicateDirection",
    "PredicateEvidenceSample",
    "PredicateLifecycle",
    "PredicateObjectType",
    "PredicateRecord",
    "PredicateRegistry",
    "PredicateRegistryError",
    "PredicateTypeSignature",
    "builtin_predicate_aliases",
    "builtin_predicate_records",
    "render_registry_block",
]
