"""Persistent review evidence for the sealed semantic-write orchestrator.

Phase E of ``docs/autonomous-architecture-plan.md``. The queue is the only thing
that interrupts the user: a candidate that the confidence gate could not commit
(low confidence, or a contradiction) is parked here with its reason and the
correlations the graph talked back with, and waits for curation.

Persistence is SQLite under ``<dir>/review_queue.sqlite`` (``<dir>`` is typically a
vault's ``.marginalia/``; see ``review_queue_sqlite``), so the queue survives
restarts. Vaults at config version 1 still hold ``review_queue.json`` and are
moved over only by the explicit ``kg review-queue migrate``. The full candidate
and its pinned review evidence are stored. Legacy rows have no ``kind`` and
continue to mean ``node``. New relation rows are tagged ``relation`` and are
deliberately read/acknowledge only: graph-writing resolution remains an
orchestration concern outside this persistence boundary.

Surface::

    queue = ReviewQueue(dir, store)
    queue.enqueue(candidate, reason, correlations)
    queue.list() -> list[ReviewItem]
    queue.enqueue_relation(candidate, reason, pinned_proposal)
    queue.list_relations() -> list[RelationReviewItem]
    queue.read(candidate_id) -> ReviewItem | RelationReviewItem
    queue.acknowledge(candidate_id)

This module never writes the graph. Resolution belongs to the companion's
sealed, resumable plan applier; acknowledgement only removes evidence after
that owning durability boundary has completed.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TypeAlias

from okto_neuron.companion import (
    Correlation,
    ReviewItem,
    ReviewItemNotFoundError,
    ReviewReason,
)
from okto_neuron.consolidate._candidates import EdgeCandidate, NodeCandidate
from okto_neuron.consolidate.ledger import edge_candidate_id
from okto_neuron.consolidate.relation_gate import (
    EndpointDecision,
    LiteralObject,
    PinnedPredicateAdmission,
    RelationGateInput,
    SourceGrounding,
    TopologyObject,
    decide_relation,
)
from okto_neuron.predicates.admission import PredicateAdmissionDecision
from okto_neuron.config._vault import vault_yaml_version
from okto_neuron.consolidate.review_queue_sqlite import (
    ReviewQueueCorruption,
    ReviewQueueMigrationRequired,
    SqliteQueueStore,
    StoredRow,
    decode_cursor,
    encode_cursor,
)
from okto_neuron.store.protocol import GraphStore

QUEUE_FILENAME = "review_queue.json"

RelationReviewReason: TypeAlias = Literal[
    "queue_type_conflict",
    "queue_predicate",
    "queue_grounding",
    "queue_unregistered",
    "queue_mapping_conflict",
    "queue_direction_conflict",
    "queue_verbose_label",
    "queue_supersession_audit",
]

_RELATION_REASON_TO_GATE_REASON = {
    "queue_type_conflict": "queue_type_conflict",
    "queue_predicate": "queue_predicate",
    "queue_grounding": "queue_grounding",
    # Legacy D6-specific queue reasons remain readable. New writes preserve
    # the D7 reason verbatim and retain the D6 detail in the pinned proposal.
    "queue_unregistered": "queue_predicate",
    "queue_mapping_conflict": "queue_predicate",
    "queue_direction_conflict": "queue_predicate",
    "queue_verbose_label": "queue_predicate",
}
_D6_QUEUE_REASONS = frozenset(
    {
        "queue_unregistered",
        "queue_mapping_conflict",
        "queue_direction_conflict",
        "queue_verbose_label",
    }
)
PinnedRelationDirection: TypeAlias = Literal["subject_to_object", "symmetric", "unknown"]


@dataclass(frozen=True)
class PinnedRelationProposal:
    """Replay-stable D6/D7 semantics and curator evidence for one relation."""

    admission: PredicateAdmissionDecision
    gate_input: RelationGateInput
    predicate_definition: str
    predicate_direction: PinnedRelationDirection
    inverse_direction_required: bool
    useful: bool

    def __post_init__(self) -> None:
        if not isinstance(self.admission, PredicateAdmissionDecision):
            raise ValueError("admission must be a PredicateAdmissionDecision")
        if not isinstance(self.gate_input, RelationGateInput):
            raise ValueError("gate_input must be a RelationGateInput")
        if self.admission.action == "reject":
            raise ValueError("a rejected predicate admission cannot enter relation review")
        if not isinstance(self.predicate_definition, str):
            raise ValueError("predicate_definition must be text")
        if (
            self.predicate_definition != self.predicate_definition.strip()
            or "\n" in self.predicate_definition
            or "\r" in self.predicate_definition
            or len(self.predicate_definition) > 500
        ):
            raise ValueError("predicate_definition must be one trimmed line of at most 500 chars")
        if self.predicate_direction not in {"subject_to_object", "symmetric", "unknown"}:
            raise ValueError(f"unknown predicate_direction: {self.predicate_direction!r}")
        if not isinstance(self.inverse_direction_required, bool):
            raise ValueError("inverse_direction_required must be boolean")
        if self.inverse_direction_required and self.predicate_direction != "subject_to_object":
            raise ValueError("inverse direction requires subject_to_object direction")
        if not isinstance(self.useful, bool):
            raise ValueError("useful must be boolean")
        self._validate_admission_gate_parity()

    def _validate_admission_gate_parity(self) -> None:
        admission = self.admission
        gate_input = self.gate_input
        expected_status = admission.state
        if admission.state not in {"canonical", "provisional", "queued"}:
            raise ValueError("reviewable admission must be canonical, provisional, or queued")
        if gate_input.predicate.predicate != admission.predicate:
            raise ValueError("admitted and gate-input predicates differ")
        if gate_input.predicate.status != expected_status:
            raise ValueError("admission state and gate-input predicate status differ")
        if gate_input.subject.entity_id != admission.subject_id:
            raise ValueError("admission and gate-input subjects differ")
        if isinstance(gate_input.object, TopologyObject):
            if admission.object_id != gate_input.object.endpoint.entity_id:
                raise ValueError("admission and gate-input topology objects differ")
        elif not _same_literal(admission.object_literal, gate_input.object.value):
            raise ValueError("admission and gate-input literal objects differ")

    def to_json(self) -> dict:
        return _relation_proposal_to_json(self)

    @classmethod
    def from_json(cls, data: object) -> "PinnedRelationProposal":
        return _relation_proposal_from_json(data)

    @property
    def raw_predicate(self) -> str:
        return self.admission.raw_predicate

    @property
    def admitted_predicate(self) -> str:
        return self.admission.predicate

    @property
    def admission_reason(self) -> str:
        return self.admission.reason

    @property
    def admission_state(self) -> str:
        return self.admission.state

    @property
    def swapped(self) -> bool:
        return self.admission.swapped


@dataclass(frozen=True)
class RelationReviewItem:
    """Full read model for a parked relation; it never exposes a write action."""

    candidate_id: str
    reason: RelationReviewReason
    candidate: EdgeCandidate
    pinned_proposal: PinnedRelationProposal
    kind: Literal["relation"] = "relation"


@dataclass(frozen=True)
class _NodeEntry:
    """One node record. A missing persisted kind is the legacy representation."""

    candidate: NodeCandidate
    reason: ReviewReason
    correlations: tuple[Correlation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, NodeCandidate):
            raise ValueError("node review candidate must be a NodeCandidate")
        if self.reason not in {"low_confidence", "contradiction"}:
            raise ValueError(f"unknown node review reason: {self.reason!r}")
        if not isinstance(self.correlations, tuple) or not all(
            isinstance(correlation, Correlation) for correlation in self.correlations
        ):
            raise ValueError("node review correlations must be Correlation values")

    def to_json(self) -> dict:
        return {
            "kind": "node",
            "candidate": self.candidate.model_dump(mode="json"),
            "reason": self.reason,
            "correlations": [c.model_dump(mode="json") for c in self.correlations],
        }

    @classmethod
    def from_json(cls, data: dict) -> "_NodeEntry":
        _require_record_fields(
            data,
            required={"candidate", "reason", "correlations"},
            optional={"kind"},
            name="node review entry",
        )
        if data.get("kind", "node") != "node":
            raise ValueError("node review entry kind must be 'node'")
        reason = data["reason"]
        if reason not in {"low_confidence", "contradiction"}:
            raise ValueError(f"unknown node review reason: {reason!r}")
        correlations = data["correlations"]
        if not isinstance(correlations, list):
            raise ValueError("node review entry correlations must be a list")
        return cls(
            candidate=NodeCandidate.model_validate(data["candidate"]),
            reason=reason,
            correlations=tuple(Correlation.model_validate(c) for c in correlations),
        )

    def to_item(self) -> ReviewItem:
        facets = self.candidate.facets or {}

        def _text(name: str) -> str | None:
            value = facets.get(name)
            return str(value) if value is not None and value != "" else None

        def _offset(name: str) -> int | None:
            value = facets.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        return ReviewItem(
            candidate_id=self.candidate.candidate_id,
            type=self.candidate.type,
            title=self.candidate.title,
            confidence=_confidence_of(self.correlations),
            reason=self.reason,
            correlations=self.correlations,
            source_path=_text("source_path"),
            block_id=_text("block_id"),
            byte_start=_offset("byte_start"),
            byte_end=_offset("byte_end"),
            content_hash=_text("content_hash"),
        )


@dataclass(frozen=True)
class _RelationEntry:
    """One relation candidate plus the complete pinned D6/D7 review input."""

    candidate: EdgeCandidate
    reason: RelationReviewReason
    pinned_proposal: PinnedRelationProposal

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, EdgeCandidate):
            raise ValueError("relation review candidate must be an EdgeCandidate")
        if not isinstance(self.reason, str):
            raise ValueError("relation review reason must be text")
        if not isinstance(self.pinned_proposal, PinnedRelationProposal):
            raise ValueError("pinned proposal must be a PinnedRelationProposal")
        decision = decide_relation(self.pinned_proposal.gate_input)
        if self.reason == "queue_supersession_audit":
            if decision.action == "commit":
                raise ValueError("supersession audit review must pin a non-committable proposal")
        else:
            expected_gate_reason = _RELATION_REASON_TO_GATE_REASON.get(self.reason)
            if expected_gate_reason is None:
                raise ValueError(f"unknown relation review reason: {self.reason!r}")
            if decision.action != "queue" or decision.reason != expected_gate_reason:
                raise ValueError(
                    "relation review reason does not match the pinned proposal decision"
                )
        if (
            self.reason in _D6_QUEUE_REASONS
            and self.reason != self.pinned_proposal.admission_reason
        ):
            raise ValueError("D6 review reason does not match the pinned admission reason")
        if self.candidate.type != self.pinned_proposal.raw_predicate:
            raise ValueError("relation candidate type and raw predicate differ")
        if self.candidate.block_id != self.pinned_proposal.gate_input.grounding.block_id:
            raise ValueError("relation candidate and grounding block ids differ")
        literal_candidate = self.candidate.dst_literal is not None
        literal_proposal = isinstance(self.pinned_proposal.gate_input.object, LiteralObject)
        if literal_candidate != literal_proposal:
            raise ValueError("relation candidate and pinned proposal object kinds differ")
        if literal_candidate and not _same_literal(
            self.candidate.dst_literal,
            self.pinned_proposal.gate_input.object.value,
        ):
            raise ValueError("relation candidate and pinned proposal literals differ")
        self._validate_candidate_admission_parity()

    def _validate_candidate_admission_parity(self) -> None:
        admission = self.pinned_proposal.admission
        candidate = self.candidate
        if admission.object_id is None:
            if candidate.dst_ref:
                raise ValueError("literal relation candidate must have an empty dst_ref")
            if candidate.src_ref != admission.subject_id:
                raise ValueError("literal candidate and admitted subject differ")
            return
        expected_src = admission.object_id if admission.swapped else admission.subject_id
        expected_dst = admission.subject_id if admission.swapped else admission.object_id
        if candidate.src_ref != expected_src or candidate.dst_ref != expected_dst:
            raise ValueError("relation candidate and admitted endpoint direction differ")

    @property
    def candidate_id(self) -> str:
        return edge_candidate_id(self.candidate.model_dump(mode="json"))

    def to_json(self) -> dict:
        return {
            "kind": "relation",
            "candidate": self.candidate.model_dump(mode="json"),
            "reason": self.reason,
            "pinned_proposal": _relation_proposal_to_json(self.pinned_proposal),
        }

    @classmethod
    def from_json(cls, data: dict) -> "_RelationEntry":
        _require_record_fields(
            data,
            required={"kind", "candidate", "reason", "pinned_proposal"},
            optional=set(),
            name="relation review entry",
        )
        if data["kind"] != "relation":
            raise ValueError("relation review entry kind must be 'relation'")
        return cls(
            candidate=EdgeCandidate.model_validate(data["candidate"]),
            reason=data["reason"],
            pinned_proposal=_relation_proposal_from_json(data["pinned_proposal"]),
        )

    def to_item(self) -> RelationReviewItem:
        return RelationReviewItem(
            candidate_id=self.candidate_id,
            reason=self.reason,
            candidate=self.candidate,
            pinned_proposal=self.pinned_proposal,
        )


_Entry: TypeAlias = _NodeEntry | _RelationEntry
ReviewQueueItem: TypeAlias = ReviewItem | RelationReviewItem


def _entry_id(entry: _Entry) -> str:
    if isinstance(entry, _NodeEntry):
        return entry.candidate.candidate_id
    return entry.candidate_id


def _same_literal(left: object, right: object) -> bool:
    """Keep JSON scalar identity exact (Python otherwise equates bool and int)."""

    return type(left) is type(right) and left == right


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"review queue contains non-finite JSON number: {value}")


def _require_record_fields(
    data: object,
    *,
    required: set[str],
    optional: set[str],
    name: str,
) -> None:
    if not isinstance(data, dict):
        raise ValueError(f"{name} must be an object")
    fields = set(data)
    missing = required - fields
    unknown = fields - required - optional
    if missing:
        raise ValueError(f"{name} missing fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"{name} has unknown fields: {', '.join(sorted(unknown))}")


def _endpoint_to_json(value: EndpointDecision) -> dict:
    return {
        "entity_id": value.entity_id,
        "type_status": value.type_status,
        "primitive_type": value.primitive_type,
        "live": value.live,
    }


def _endpoint_from_json(data: object) -> EndpointDecision:
    _require_record_fields(
        data,
        required={"entity_id", "type_status", "primitive_type", "live"},
        optional=set(),
        name="relation endpoint decision",
    )
    assert isinstance(data, dict)
    return EndpointDecision(
        entity_id=data["entity_id"],
        type_status=data["type_status"],
        primitive_type=data["primitive_type"],
        live=data["live"],
    )


def _gate_input_to_json(value: RelationGateInput) -> dict:
    relation_object: dict
    if isinstance(value.object, TopologyObject):
        relation_object = {
            "kind": "topology",
            "endpoint": _endpoint_to_json(value.object.endpoint),
        }
    else:
        relation_object = {"kind": "literal", "value": value.object.value}
    return {
        "subject": _endpoint_to_json(value.subject),
        "predicate": {
            "predicate": value.predicate.predicate,
            "status": value.predicate.status,
        },
        "object": relation_object,
        "grounding": {
            "block_id": value.grounding.block_id,
            "subject_supported": value.grounding.subject_supported,
            "predicate_supported": value.grounding.predicate_supported,
            "object_supported": value.grounding.object_supported,
            "direction_supported": value.grounding.direction_supported,
            "unsupported_inference": value.grounding.unsupported_inference,
        },
        "structural_noise": value.structural_noise,
        "redundant": value.redundant,
    }


def _gate_input_from_json(data: object) -> RelationGateInput:
    _require_record_fields(
        data,
        required={"subject", "predicate", "object", "grounding", "structural_noise", "redundant"},
        optional=set(),
        name="pinned relation proposal",
    )
    assert isinstance(data, dict)
    predicate = data["predicate"]
    _require_record_fields(
        predicate,
        required={"predicate", "status"},
        optional=set(),
        name="pinned predicate admission",
    )
    assert isinstance(predicate, dict)
    relation_object = data["object"]
    if not isinstance(relation_object, dict):
        raise ValueError("pinned relation object must be an object")
    object_kind = relation_object.get("kind")
    if object_kind == "topology":
        _require_record_fields(
            relation_object,
            required={"kind", "endpoint"},
            optional=set(),
            name="pinned topology object",
        )
        pinned_object = TopologyObject(endpoint=_endpoint_from_json(relation_object["endpoint"]))
    elif object_kind == "literal":
        _require_record_fields(
            relation_object,
            required={"kind", "value"},
            optional=set(),
            name="pinned literal object",
        )
        pinned_object = LiteralObject(value=relation_object["value"])
    else:
        raise ValueError(f"unknown pinned relation object kind: {object_kind!r}")
    grounding = data["grounding"]
    _require_record_fields(
        grounding,
        required={
            "block_id",
            "subject_supported",
            "predicate_supported",
            "object_supported",
            "direction_supported",
            "unsupported_inference",
        },
        optional=set(),
        name="pinned source grounding",
    )
    assert isinstance(grounding, dict)
    return RelationGateInput(
        subject=_endpoint_from_json(data["subject"]),
        predicate=PinnedPredicateAdmission(
            predicate=predicate["predicate"],
            status=predicate["status"],
        ),
        object=pinned_object,
        grounding=SourceGrounding(
            block_id=grounding["block_id"],
            subject_supported=grounding["subject_supported"],
            predicate_supported=grounding["predicate_supported"],
            object_supported=grounding["object_supported"],
            direction_supported=grounding["direction_supported"],
            unsupported_inference=grounding["unsupported_inference"],
        ),
        structural_noise=data["structural_noise"],
        redundant=data["redundant"],
    )


def _relation_proposal_to_json(value: PinnedRelationProposal) -> dict:
    admission = value.admission
    return {
        "admission": {
            "reason": admission.reason,
            "state": admission.state,
            "raw_predicate": admission.raw_predicate,
            "predicate": admission.predicate,
            "subject_id": admission.subject_id,
            "object_id": admission.object_id,
            "object_literal": admission.object_literal,
            "swapped": admission.swapped,
        },
        "gate_input": _gate_input_to_json(value.gate_input),
        "predicate_definition": value.predicate_definition,
        "predicate_direction": value.predicate_direction,
        "inverse_direction_required": value.inverse_direction_required,
        "useful": value.useful,
    }


def _relation_proposal_from_json(data: object) -> PinnedRelationProposal:
    _require_record_fields(
        data,
        required={
            "admission",
            "gate_input",
            "predicate_definition",
            "predicate_direction",
            "inverse_direction_required",
            "useful",
        },
        optional=set(),
        name="pinned relation proposal",
    )
    assert isinstance(data, dict)
    admission = data["admission"]
    _require_record_fields(
        admission,
        required={
            "reason",
            "state",
            "raw_predicate",
            "predicate",
            "subject_id",
            "object_id",
            "object_literal",
            "swapped",
        },
        optional=set(),
        name="pinned predicate admission decision",
    )
    assert isinstance(admission, dict)
    return PinnedRelationProposal(
        admission=PredicateAdmissionDecision(
            reason=admission["reason"],
            state=admission["state"],
            raw_predicate=admission["raw_predicate"],
            predicate=admission["predicate"],
            subject_id=admission["subject_id"],
            object_id=admission["object_id"],
            object_literal=admission["object_literal"],
            swapped=admission["swapped"],
        ),
        gate_input=_gate_input_from_json(data["gate_input"]),
        predicate_definition=data["predicate_definition"],
        predicate_direction=data["predicate_direction"],
        inverse_direction_required=data["inverse_direction_required"],
        useful=data["useful"],
    )


def _confidence_of(correlations: tuple[Correlation, ...]) -> float:
    """A queued item carries no standalone score; surface the strongest
    correlation as a hint (0.0 when novel-but-low)."""
    if not correlations:
        return 0.0
    return max(c.score for c in correlations)


def entry_digest(record: dict) -> str:
    """The ``entry_sha256`` of one entry record: the digest a sealed plan pins."""

    encoded = json.dumps(
        record,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def parse_legacy_entry(record: object) -> _Entry:
    """Validate one ``review_queue.json`` record exactly as the JSON-era loader did."""

    if not isinstance(record, dict):
        raise ValueError("review queue entry must be an object")
    kind = record.get("kind", "node")
    if kind == "node":
        return _NodeEntry.from_json(record)
    if kind == "relation":
        return _RelationEntry.from_json(record)
    raise ValueError(f"unknown review queue entry kind: {kind!r}")


def load_legacy_records(path: Path) -> list[dict]:
    """Parse ``review_queue.json`` into its raw record list (migration input)."""

    data = json.loads(
        Path(path).read_text(encoding="utf-8"),
        parse_constant=_reject_nonfinite_json,
    )
    _require_record_fields(
        data,
        required={"entries"},
        optional=set(),
        name="review queue",
    )
    assert isinstance(data, dict)
    records = data["entries"]
    if not isinstance(records, list):
        raise ValueError("review queue entries must be a list")
    return records


def load_legacy_entries(path: Path) -> dict[str, _Entry]:
    """Validated entries of ``review_queue.json`` keyed by id (dup id is an error)."""

    loaded: dict[str, _Entry] = {}
    for index, record in enumerate(load_legacy_records(path)):
        try:
            entry = parse_legacy_entry(record)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"review queue entry #{index} refused: {exc}") from exc
        candidate_id = _entry_id(entry)
        if candidate_id in loaded:
            raise ValueError(f"duplicate review queue candidate id: {candidate_id}")
        loaded[candidate_id] = entry
    return loaded


def split_entry(entry: _Entry) -> tuple[dict, tuple[float, ...] | None, str]:
    """``(payload record without embedding, embedding, entry_sha256)`` for storage.

    The digest is over the FULL record (embedding included), identical to the
    JSON era's ``resolution_scope`` digest.
    """

    record = entry.to_json()
    digest = entry_digest(record)
    candidate = record["candidate"]
    embedding = candidate.get("embedding") if record["kind"] == "node" else None
    if embedding is None:
        return record, None, digest
    payload = dict(record)
    payload["candidate"] = {**candidate, "embedding": None}
    return payload, tuple(embedding), digest


def join_record(row: StoredRow) -> dict:
    """Rebuild the full entry record (embedding restored) from a stored row."""

    record = json.loads(row.payload, parse_constant=_reject_nonfinite_json)
    if row.embedding is not None:
        record["candidate"]["embedding"] = list(row.embedding)
    return record


def layout_refusal(vault_root: Path | str) -> dict[str, str] | None:
    """Why a daemon must not serve ``vault_root`` yet, or ``None`` when it may.

    A vault still at ``marginalia_yaml_version: 1`` keeps ``review_queue.json``
    and is older-binary territory until ``kg review-queue migrate`` runs. The
    check only reads the yaml; it never writes anything.
    """

    root = Path(vault_root)
    try:
        version = vault_yaml_version(root)
    except Exception:  # noqa: BLE001 - a broken yaml is the open path's error to report
        return None
    if version != 1:
        return None
    remedy = f"okto-neuron kg review-queue migrate --vault {root.name}"
    return {
        "code": "review_queue_migration_required",
        "path": str(root),
        "detail": (
            "this vault still stores its review queue as review_queue.json "
            "(marginalia_yaml_version 1); it is not served until it is migrated"
        ),
        "remedy": remedy,
    }


_REFUSAL_LOGGED: set[Path] = set()


def log_layout_refusal_once(vault_root: Path, refusal: dict[str, str]) -> None:
    """One WARNING per vault and process for a refused v1 vault (status polls repeat)."""

    if vault_root in _REFUSAL_LOGGED:
        return
    _REFUSAL_LOGGED.add(vault_root)
    logging.getLogger(__name__).warning(
        "refusing vault %s: %s; remedy: %s",
        vault_root.name,
        refusal["detail"],
        refusal["remedy"],
    )


def clear_layout_refusal_log(vault_root: Path) -> None:
    _REFUSAL_LOGGED.discard(vault_root)


def _vault_root_of(directory: Path) -> Path | None:
    return directory.parent if directory.name == ".marginalia" else None


@dataclass
class ReviewQueue:
    """Persistent queue of parked candidates, keyed by ``candidate_id``.

    ``dir`` holds ``review_queue.sqlite`` (see ``review_queue_sqlite``); ``store``
    is the graph the resolve actions act on. Construction reads nothing. A vault
    that still uses ``review_queue.json`` (config version 1) is refused with
    ``ReviewQueueMigrationRequired``: the queue never migrates implicitly, the
    explicit ``kg review-queue migrate`` does.
    """

    dir: Path
    store: GraphStore
    _rows: SqliteQueueStore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.dir = Path(self.dir)
        self._rows = SqliteQueueStore(self.dir)
        self._require_current_layout()

    # ── layout gate ──────────────────────────────────────────────────────────
    @property
    def path(self) -> Path:
        """The SQLite file backing this queue."""
        return self._rows.path

    @property
    def legacy_path(self) -> Path:
        return self.dir / QUEUE_FILENAME

    def _require_current_layout(self) -> None:
        root = _vault_root_of(self.dir)
        remedy = (
            "run `okto-neuron kg review-queue migrate --vault "
            f"{root.name if root is not None else '<name>'}`"
        )
        if root is not None and vault_yaml_version(root) == 1:
            raise ReviewQueueMigrationRequired(
                f"review queue uses the legacy JSON layout (config version 1); {remedy}",
                vault_path=root,
            )
        if not self._rows.exists() and self.legacy_path.exists():
            raise ReviewQueueMigrationRequired(
                f"{QUEUE_FILENAME} exists but no SQLite queue does; {remedy}",
                file_path=self.legacy_path,
            )

    # ── row <-> entry ────────────────────────────────────────────────────────
    @staticmethod
    def _entry_from_row(row: StoredRow, *, verify: bool) -> _Entry:
        """Rebuild an entry. ``verify`` recomputes the digest over the full record."""

        record = join_record(row)
        if verify and entry_digest(record) != row.entry_sha256:
            raise ReviewQueueCorruption(
                "review queue row failed its entry_sha256 check "
                f"(seq {row.seq}, kind {row.kind})"
            )
        return parse_legacy_entry(record)

    def _put(self, entry: _Entry) -> None:
        payload, embedding, digest = split_entry(entry)
        self._rows.put(
            candidate_id=_entry_id(entry),
            kind=payload["kind"],
            reason=entry.reason,
            payload=json.dumps(payload, sort_keys=True, allow_nan=False),
            entry_sha256=digest,
            embedding=embedding,
        )

    # ── surface ──────────────────────────────────────────────────────────────
    def enqueue(
        self,
        candidate: NodeCandidate,
        reason: ReviewReason,
        correlations: tuple[Correlation, ...] = (),
    ) -> ReviewItem:
        """Park ``candidate``. Upserts by ``candidate_id`` (the deterministic
        content hash), so re-enqueuing the same content replaces, not duplicates."""
        entry = _NodeEntry(
            candidate=candidate,
            reason=reason,
            correlations=tuple(correlations),
        )
        self._put(entry)
        return entry.to_item()

    def enqueue_relation(
        self,
        candidate: EdgeCandidate,
        reason: RelationReviewReason,
        pinned_proposal: PinnedRelationProposal,
    ) -> RelationReviewItem:
        """Persist a relation review without performing or exposing a graph write."""

        entry = _RelationEntry(
            candidate=candidate,
            reason=reason,
            pinned_proposal=pinned_proposal,
        )
        self._put(entry)
        return entry.to_item()

    def list(self) -> list[ReviewItem]:
        """Legacy node-only list; relation entries use :meth:`list_relations`."""

        return [
            self._entry_from_row(row, verify=False).to_item()
            for row in self._rows.rows(kind="node")
        ]

    def list_relations(self) -> list[RelationReviewItem]:
        """All parked relations with their exact reason and pinned proposal."""

        return [
            self._entry_from_row(row, verify=False).to_item()
            for row in self._rows.rows(kind="relation")
        ]

    def page(
        self, limit: int, cursor: str | None = None
    ) -> tuple[list[ReviewQueueItem], str | None, int]:
        """One page ``(items, next_cursor, total)``: nodes first, then relations.

        ``cursor`` is the opaque value a previous page returned; ``ValueError`` on
        a malformed one. ``limit=0`` returns no items, only the total.
        """

        after = decode_cursor(cursor) if cursor else None
        total = self._rows.count()
        if limit <= 0:
            return [], None, total
        rows = self._rows.rows(after=after, limit=limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        items = [self._entry_from_row(row, verify=False).to_item() for row in rows]
        next_cursor = encode_cursor(rows[-1].kind, rows[-1].seq) if more else None
        return items, next_cursor, total

    def read(self, candidate_id: str) -> ReviewQueueItem:
        """Return one full kind-appropriate projection without changing it."""

        row = self._rows.get(candidate_id, with_embedding=False)
        if row is None:
            raise ReviewItemNotFoundError(f"no review item with id {candidate_id!r}")
        return self._entry_from_row(row, verify=False).to_item()

    def candidates(self) -> list[NodeCandidate]:
        """Full parked node candidates for legacy judge-driven curation jobs."""

        return [
            self._entry_from_row(row, verify=True).candidate  # type: ignore[union-attr]
            for row in self._rows.rows(kind="node", with_embedding=True)
        ]

    def get_candidate(self, candidate_id: str) -> NodeCandidate | None:
        """The full (embedding included, digest-verified) node candidate, or None."""

        row = self._rows.get(candidate_id, with_embedding=True)
        if row is None or row.kind != "node":
            return None
        return self._entry_from_row(row, verify=True).candidate  # type: ignore[union-attr]

    def resolution_scope(self, candidate_id: str) -> dict[str, str]:
        """Bind a manual resolution plan to the exact persisted queue entry."""

        row = self._rows.get(candidate_id, with_embedding=True)
        if row is None:
            raise ReviewItemNotFoundError(f"no review item with id {candidate_id!r}")
        if row.kind == "relation":
            raise ValueError(
                "relation review items are read/acknowledge only; "
                "graph resolution belongs to the orchestrator"
            )
        self._entry_from_row(row, verify=True)
        return {
            # The legacy name is kept on purpose: in-flight sealed plans compare
            # this scope verbatim, and the digest below is unchanged.
            "queue_file": QUEUE_FILENAME,
            "candidate_id": candidate_id,
            "entry_sha256": row.entry_sha256,
        }

    def __len__(self) -> int:
        return self._rows.count()

    def acknowledge(self, candidate_id: str) -> None:
        """Remove a resolved item only after its owning durability boundary passes."""

        if not self._rows.delete(candidate_id):
            raise ReviewItemNotFoundError(f"no review item with id {candidate_id!r}")


__all__ = [
    "layout_refusal",
    "log_layout_refusal_once",
    "clear_layout_refusal_log",
    "ReviewQueueCorruption",
    "ReviewQueueMigrationRequired",
    "entry_digest",
    "join_record",
    "load_legacy_entries",
    "load_legacy_records",
    "parse_legacy_entry",
    "split_entry",
    "PinnedRelationDirection",
    "PinnedRelationProposal",
    "QUEUE_FILENAME",
    "RelationReviewItem",
    "RelationReviewReason",
    "ReviewQueue",
    "ReviewQueueItem",
]
