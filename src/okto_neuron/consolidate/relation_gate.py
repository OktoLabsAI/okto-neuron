"""Pure ADR 0040 D7 semantic relation gate.

The gate consumes decisions already pinned by type/identity resolution and
predicate admission.  It does not inspect a graph, consult a registry, call an
LLM, or write a plan.  Its precedence is deliberately closed and deterministic:

1. hard rejects: placeholder, structural noise, unsupported inference, dead
   endpoint, redundancy;
2. review queues: type conflict, predicate uncertainty, grounding uncertainty;
3. commit.

Topology objects carry a typed/live endpoint. Literal objects carry a scalar
value and therefore need no artificial object endpoint; both forms still require
a live, accepted subject. Direction grounding is required only for topology.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, TypeAlias

PrimitiveType: TypeAlias = Literal["Agent", "Activity", "InformationObject", "Concept", "Place"]
EndpointTypeStatus: TypeAlias = Literal["accepted", "conflict", "ambiguous"]
PredicateAdmissionStatus: TypeAlias = Literal[
    "canonical", "provisional", "queued", "placeholder", "structural_noise"
]
RelationGateReason: TypeAlias = Literal[
    "commit",
    "queue_type_conflict",
    "queue_predicate",
    "queue_grounding",
    "reject_placeholder",
    "reject_structural_noise",
    "reject_unsupported_inference",
    "reject_dead_endpoint",
    "reject_redundant",
]
RelationGateAction: TypeAlias = Literal["commit", "queue", "reject"]
RelationLiteral: TypeAlias = str | int | float | bool

_PRIMITIVE_TYPES = frozenset({"Agent", "Activity", "InformationObject", "Concept", "Place"})
_TYPE_STATUSES = frozenset({"accepted", "conflict", "ambiguous"})
_PREDICATE_STATUSES = frozenset(
    {"canonical", "provisional", "queued", "placeholder", "structural_noise"}
)
_REASONS = frozenset(
    {
        "commit",
        "queue_type_conflict",
        "queue_predicate",
        "queue_grounding",
        "reject_placeholder",
        "reject_structural_noise",
        "reject_unsupported_inference",
        "reject_dead_endpoint",
        "reject_redundant",
    }
)
_ACTION_BY_REASON: dict[str, RelationGateAction] = {
    "commit": "commit",
    "queue_type_conflict": "queue",
    "queue_predicate": "queue",
    "queue_grounding": "queue",
    "reject_placeholder": "reject",
    "reject_structural_noise": "reject",
    "reject_unsupported_inference": "reject",
    "reject_dead_endpoint": "reject",
    "reject_redundant": "reject",
}


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


@dataclass(frozen=True)
class EndpointDecision:
    """One identity already resolved enough for D7 to test type and liveness."""

    entity_id: str
    type_status: EndpointTypeStatus
    primitive_type: PrimitiveType | None
    live: bool

    def __post_init__(self) -> None:
        _required_text(self.entity_id, "entity_id")
        if self.type_status not in _TYPE_STATUSES:
            raise ValueError(f"unknown endpoint type status: {self.type_status!r}")
        if self.primitive_type is not None and self.primitive_type not in _PRIMITIVE_TYPES:
            raise ValueError(f"unknown primitive type: {self.primitive_type!r}")
        if self.type_status == "accepted" and self.primitive_type is None:
            raise ValueError("an accepted endpoint requires a closed primitive type")
        if not isinstance(self.live, bool):
            raise ValueError("endpoint live decision must be boolean")


@dataclass(frozen=True)
class PinnedPredicateAdmission:
    """The final D6 predicate decision; inverse swaps are already reflected upstream."""

    predicate: str
    status: PredicateAdmissionStatus

    def __post_init__(self) -> None:
        _required_text(self.predicate, "predicate")
        if self.status not in _PREDICATE_STATUSES:
            raise ValueError(f"unknown predicate admission status: {self.status!r}")

    @property
    def accepted(self) -> bool:
        return self.status in {"canonical", "provisional"}


@dataclass(frozen=True)
class SourceGrounding:
    """Pinned evidence that the cited source supports each semantic term.

    ``block_id=None`` is a valid incomplete evidence state and queues grounding,
    even if individual support flags are true. A present id must be non-empty.
    """

    block_id: str | None
    subject_supported: bool
    predicate_supported: bool
    object_supported: bool
    direction_supported: bool
    unsupported_inference: bool = False

    def __post_init__(self) -> None:
        if self.block_id is not None:
            _required_text(self.block_id, "block_id")
        for field_name in (
            "subject_supported",
            "predicate_supported",
            "object_supported",
            "direction_supported",
            "unsupported_inference",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"{field_name} must be boolean")

    @property
    def has_block(self) -> bool:
        return isinstance(self.block_id, str) and bool(self.block_id.strip())


@dataclass(frozen=True)
class TopologyObject:
    endpoint: EndpointDecision


@dataclass(frozen=True)
class LiteralObject:
    value: RelationLiteral

    def __post_init__(self) -> None:
        if not isinstance(self.value, (str, int, float, bool)):
            raise ValueError("literal object must be a scalar string, number, or boolean")
        if isinstance(self.value, str) and not self.value.strip():
            raise ValueError("literal object must not be empty")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("literal object must be finite")


RelationObject: TypeAlias = TopologyObject | LiteralObject


@dataclass(frozen=True)
class RelationGateInput:
    """All pinned inputs required for one D7 decision."""

    subject: EndpointDecision
    predicate: PinnedPredicateAdmission
    object: RelationObject
    grounding: SourceGrounding
    structural_noise: bool = False
    redundant: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.subject, EndpointDecision):
            raise ValueError("subject must be an EndpointDecision")
        if not isinstance(self.predicate, PinnedPredicateAdmission):
            raise ValueError("predicate must be a PinnedPredicateAdmission")
        if not isinstance(self.object, (TopologyObject, LiteralObject)):
            raise ValueError("object must be a TopologyObject or LiteralObject")
        if not isinstance(self.grounding, SourceGrounding):
            raise ValueError("grounding must be SourceGrounding")
        if not isinstance(self.structural_noise, bool):
            raise ValueError("structural_noise must be boolean")
        if not isinstance(self.redundant, bool):
            raise ValueError("redundant must be boolean")

    @property
    def relation_kind(self) -> Literal["topology", "literal"]:
        return "topology" if isinstance(self.object, TopologyObject) else "literal"


@dataclass(frozen=True)
class RelationGateDecision:
    """One immutable reason plus the exact pinned semantics it governs."""

    reason: RelationGateReason
    subject_id: str
    predicate: str
    object: RelationObject

    def __post_init__(self) -> None:
        if self.reason not in _REASONS:
            raise ValueError(f"unknown relation gate reason: {self.reason!r}")
        _required_text(self.subject_id, "subject_id")
        _required_text(self.predicate, "predicate")
        if not isinstance(self.object, (TopologyObject, LiteralObject)):
            raise ValueError("object must be a TopologyObject or LiteralObject")

    @property
    def action(self) -> RelationGateAction:
        return _ACTION_BY_REASON[self.reason]

    @property
    def relation_kind(self) -> Literal["topology", "literal"]:
        return "topology" if isinstance(self.object, TopologyObject) else "literal"

    @property
    def liveness_support_ids(self) -> frozenset[str]:
        """Entities this accepted relation may support at a later policy boundary.

        Queue and reject outcomes always return an empty set, so a dead-lettered
        relation can never be reused as evidence that one of its endpoints lives.
        """

        if self.reason != "commit":
            return frozenset()
        if isinstance(self.object, TopologyObject):
            return frozenset({self.subject_id, self.object.endpoint.entity_id})
        return frozenset({self.subject_id})


def decide_relation(value: RelationGateInput) -> RelationGateDecision:
    """Return the single D7 decision for ``value`` using the module precedence."""

    reason: RelationGateReason
    predicate_status = value.predicate.status
    object_endpoint = value.object.endpoint if isinstance(value.object, TopologyObject) else None

    # Hard reject safety precedes every uncertain queue outcome.
    if predicate_status == "placeholder":
        reason = "reject_placeholder"
    elif predicate_status == "structural_noise" or value.structural_noise:
        reason = "reject_structural_noise"
    elif value.grounding.unsupported_inference:
        reason = "reject_unsupported_inference"
    elif not value.subject.live or (object_endpoint is not None and not object_endpoint.live):
        reason = "reject_dead_endpoint"
    elif value.redundant:
        reason = "reject_redundant"
    # Queues are also ordered: identity/type, predicate, then source evidence.
    elif value.subject.type_status != "accepted" or (
        object_endpoint is not None and object_endpoint.type_status != "accepted"
    ):
        reason = "queue_type_conflict"
    elif not value.predicate.accepted:
        reason = "queue_predicate"
    elif not _fully_grounded(value):
        reason = "queue_grounding"
    else:
        reason = "commit"

    return RelationGateDecision(
        reason=reason,
        subject_id=value.subject.entity_id,
        predicate=value.predicate.predicate,
        object=value.object,
    )


def _fully_grounded(value: RelationGateInput) -> bool:
    grounding = value.grounding
    if not (
        grounding.has_block
        and grounding.subject_supported
        and grounding.predicate_supported
        and grounding.object_supported
    ):
        return False
    if isinstance(value.object, TopologyObject):
        return grounding.direction_supported
    return True


__all__ = [
    "EndpointDecision",
    "EndpointTypeStatus",
    "LiteralObject",
    "PinnedPredicateAdmission",
    "PredicateAdmissionStatus",
    "PrimitiveType",
    "RelationGateAction",
    "RelationGateDecision",
    "RelationGateInput",
    "RelationGateReason",
    "RelationLiteral",
    "RelationObject",
    "SourceGrounding",
    "TopologyObject",
    "decide_relation",
]
