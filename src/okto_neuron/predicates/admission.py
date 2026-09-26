"""Pure ADR 0040 D6 predicate admission.

Admission consumes an immutable snapshot of D5 registry and mapping decisions.
It pins the final predicate, representation, and endpoint direction for a later
ADR 0039 plan.  It never writes the registry or graph, calls an LLM, or lets the
applier re-interpret an inverse mapping during replay.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypeAlias

from okto_neuron.curator import normalize_predicate, predicate_label_key

from .registry import DENIED_PREDICATE_LABELS, PredicateRecord

PredicateAdmissionAction: TypeAlias = Literal[
    "commit_topology", "commit_literal", "queue_review", "reject"
]
PredicateAdmissionReason: TypeAlias = Literal[
    "canonical",
    "exact_mapping",
    "confirmed_inverse",
    "provisional",
    "literal",
    "queue_unregistered",
    "queue_mapping_conflict",
    "queue_direction_conflict",
    "queue_verbose_label",
    "reject_placeholder",
    "reject_malformed",
    "reject_structural_noise",
]
PredicateAdmissionState: TypeAlias = Literal[
    "canonical", "provisional", "queued", "placeholder", "malformed", "structural_noise"
]
PredicateObjectKind: TypeAlias = Literal["topology", "literal"]
PredicateLiteral: TypeAlias = str | int | float | bool

PLACEHOLDER_PREDICATES = frozenset({"", "unknown", "none", "null", "n/a", "na", "undefined"})
_GRAMMATICAL_GLUE = frozenset(
    {"and", "or", "the", "of", "to", "is", "was", "were", "with", "from", "by"}
)
_REASONS = frozenset(
    {
        "canonical",
        "exact_mapping",
        "confirmed_inverse",
        "provisional",
        "literal",
        "queue_unregistered",
        "queue_mapping_conflict",
        "queue_direction_conflict",
        "queue_verbose_label",
        "reject_placeholder",
        "reject_malformed",
        "reject_structural_noise",
    }
)
_ACTION_BY_REASON: dict[str, PredicateAdmissionAction] = {
    "canonical": "commit_topology",
    "exact_mapping": "commit_topology",
    "confirmed_inverse": "commit_topology",
    "provisional": "commit_topology",
    "literal": "commit_literal",
    "queue_unregistered": "queue_review",
    "queue_mapping_conflict": "queue_review",
    "queue_direction_conflict": "queue_review",
    "queue_verbose_label": "queue_review",
    "reject_placeholder": "reject",
    "reject_malformed": "reject",
    "reject_structural_noise": "reject",
}
_STATES_BY_REASON: dict[str, frozenset[str]] = {
    "canonical": frozenset({"canonical"}),
    "exact_mapping": frozenset({"canonical", "provisional"}),
    "confirmed_inverse": frozenset({"canonical", "provisional"}),
    "provisional": frozenset({"provisional"}),
    "literal": frozenset({"canonical", "provisional"}),
    "queue_unregistered": frozenset({"queued"}),
    "queue_mapping_conflict": frozenset({"queued"}),
    "queue_direction_conflict": frozenset({"queued"}),
    "queue_verbose_label": frozenset({"queued"}),
    "reject_placeholder": frozenset({"placeholder"}),
    "reject_malformed": frozenset({"malformed"}),
    "reject_structural_noise": frozenset({"structural_noise"}),
}


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    if value != value.strip():
        raise ValueError(f"{field_name} must be trimmed")
    return value


@dataclass(frozen=True)
class PredicateProposal:
    raw_predicate: str
    subject_id: str
    object_id: str | None = None
    object_literal: PredicateLiteral | None = None
    apply_confirmed_inverse: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.raw_predicate, str):
            raise ValueError("raw_predicate must be text")
        if self.raw_predicate != self.raw_predicate.strip():
            raise ValueError("raw_predicate must be trimmed")
        _text(self.subject_id, "subject_id")
        if (self.object_id is None) == (self.object_literal is None):
            raise ValueError("proposal must define exactly one object_id or object_literal")
        if self.object_id is not None:
            _text(self.object_id, "object_id")
        elif (
            not isinstance(self.object_literal, (str, int, float, bool))
            or (isinstance(self.object_literal, str) and not self.object_literal.strip())
            or (isinstance(self.object_literal, float) and not math.isfinite(self.object_literal))
        ):
            raise ValueError("object_literal must be a finite non-empty scalar")
        if not isinstance(self.apply_confirmed_inverse, bool):
            raise ValueError("apply_confirmed_inverse must be boolean")
        if self.apply_confirmed_inverse and self.object_id is None:
            raise ValueError("literal predicates cannot swap entity endpoints")

    @property
    def object_kind(self) -> PredicateObjectKind:
        return "topology" if self.object_id is not None else "literal"


@dataclass(frozen=True)
class PredicateAdmissionDecision:
    reason: PredicateAdmissionReason
    state: PredicateAdmissionState
    raw_predicate: str
    predicate: str
    subject_id: str
    object_id: str | None
    object_literal: PredicateLiteral | None
    swapped: bool

    def __post_init__(self) -> None:
        if self.reason not in _REASONS:
            raise ValueError(f"unknown predicate admission reason: {self.reason!r}")
        if self.state not in _STATES_BY_REASON[self.reason]:
            raise ValueError("predicate admission reason and state are inconsistent")
        if not isinstance(self.raw_predicate, str):
            raise ValueError("raw_predicate must be text")
        if self.raw_predicate != self.raw_predicate.strip():
            raise ValueError("raw_predicate must be trimmed")
        _text(self.subject_id, "subject_id")
        if self.action.startswith("commit"):
            _text(self.predicate, "predicate")
        elif self.predicate:
            _text(self.predicate, "predicate")
        if (self.object_id is None) == (self.object_literal is None):
            raise ValueError("decision must retain exactly one object representation")
        if self.object_id is not None:
            _text(self.object_id, "object_id")
        elif (
            not isinstance(self.object_literal, (str, int, float, bool))
            or (isinstance(self.object_literal, str) and not self.object_literal.strip())
            or (isinstance(self.object_literal, float) and not math.isfinite(self.object_literal))
        ):
            raise ValueError("decision object_literal must be a finite non-empty scalar")
        if self.action == "commit_topology" and self.object_id is None:
            raise ValueError("commit_topology requires an entity object")
        if self.action == "commit_literal" and self.object_literal is None:
            raise ValueError("commit_literal requires a literal object")
        if not isinstance(self.swapped, bool):
            raise ValueError("swapped must be boolean")
        if self.swapped and (self.object_id is None or self.action != "commit_topology"):
            raise ValueError("only committed topology can carry an endpoint swap")
        if self.swapped != (self.reason == "confirmed_inverse"):
            raise ValueError("confirmed_inverse reason and swapped state must agree")

    @property
    def action(self) -> PredicateAdmissionAction:
        return _ACTION_BY_REASON[self.reason]

    @property
    def object_kind(self) -> PredicateObjectKind:
        return "topology" if self.object_id is not None else "literal"


def admit_predicate(
    proposal: PredicateProposal,
    *,
    registry: Mapping[str, PredicateRecord],
    exact_mappings: Mapping[str, str] | None = None,
    inverse_mappings: Mapping[str, str] | None = None,
) -> PredicateAdmissionDecision:
    """Return one replay-pinned admission decision from immutable snapshots.

    ``exact_mappings`` is the caller-assembled effective snapshot: it must
    already contain enabled pack aliases and omit legacy mapping keys that do
    not satisfy :func:`predicate_label_key`. Core aliases are applied here and
    retain precedence over that contextual snapshot.
    """

    if not isinstance(proposal, PredicateProposal):
        raise TypeError("proposal must be a PredicateProposal")
    records = _registry_snapshot(registry)
    exact = _mapping_snapshot(exact_mappings or {}, "exact_mappings")
    inverse = _mapping_snapshot(inverse_mappings or {}, "inverse_mappings")
    raw_folded = proposal.raw_predicate.casefold()
    lexical = predicate_label_key(proposal.raw_predicate)

    if raw_folded in PLACEHOLDER_PREDICATES:
        return _decision(proposal, "reject_placeholder", "placeholder", predicate="")
    if not lexical:
        return _decision(proposal, "reject_malformed", "malformed", predicate="")
    if lexical in {"unknown", "none", "null", "n_a", "na", "undefined"}:
        return _decision(proposal, "reject_placeholder", "placeholder", predicate="")
    if _is_structural_noise(proposal.raw_predicate, lexical):
        return _decision(
            proposal,
            "reject_structural_noise",
            "structural_noise",
            predicate=lexical,
        )
    core_mapped = normalize_predicate(lexical)
    core_changed = core_mapped != lexical
    conflicting_core_override = core_changed and (
        (lexical in exact and exact[lexical] != core_mapped)
        or (core_mapped in exact and exact[core_mapped] != core_mapped)
    )
    dual_mapping_keys = {key for key in (lexical, core_mapped) if key in exact and key in inverse}
    if conflicting_core_override or dual_mapping_keys:
        return _decision(
            proposal,
            "queue_mapping_conflict",
            "queued",
            predicate=core_mapped,
        )
    mapped = core_mapped if core_changed else exact.get(lexical, lexical)
    inverse_targets = {inverse[key] for key in (lexical, core_mapped, mapped) if key in inverse}
    if len(inverse_targets) > 1:
        return _decision(
            proposal,
            "queue_mapping_conflict",
            "queued",
            predicate=mapped,
        )
    inverse_target = next(iter(inverse_targets), None)
    if proposal.apply_confirmed_inverse and inverse_target is None:
        return _decision(
            proposal,
            "queue_mapping_conflict",
            "queued",
            predicate=mapped,
        )
    if proposal.apply_confirmed_inverse:
        assert inverse_target is not None  # narrowed by the fail-closed branch above
        inverse_core = normalize_predicate(inverse_target)
        conflicting_inverse_core_override = inverse_core != inverse_target and (
            (inverse_target in exact and exact[inverse_target] != inverse_core)
            or (inverse_core in exact and exact[inverse_core] != inverse_core)
        )
        inverse_dual_mapping_keys = {
            key for key in (inverse_target, inverse_core) if key in exact and key in inverse
        }
        if conflicting_inverse_core_override or inverse_dual_mapping_keys:
            return _decision(
                proposal,
                "queue_mapping_conflict",
                "queued",
                predicate=inverse_core,
            )
        inverse_target = (
            inverse_core
            if inverse_core != inverse_target
            else exact.get(inverse_target, inverse_target)
        )
    final_candidate = inverse_target if proposal.apply_confirmed_inverse else mapped
    assert final_candidate is not None
    if final_candidate is not None and final_candidate in DENIED_PREDICATE_LABELS:
        return _decision(
            proposal,
            "reject_structural_noise",
            "structural_noise",
            predicate=final_candidate,
        )
    record = records.get(final_candidate)
    if record is None and _is_verbose_label(final_candidate):
        return _decision(
            proposal,
            "queue_verbose_label",
            "queued",
            predicate=final_candidate,
        )
    if proposal.apply_confirmed_inverse:
        if record is None:
            return _decision(
                proposal,
                "queue_unregistered",
                "queued",
                predicate=final_candidate,
            )
        return _decision(
            proposal,
            "confirmed_inverse",
            record.lifecycle,
            predicate=record.label,
            swap=True,
        )

    if record is None:
        return _decision(
            proposal,
            "queue_unregistered",
            "queued",
            predicate=mapped,
        )
    if proposal.object_kind == "literal":
        return _decision(
            proposal,
            "literal",
            record.lifecycle,
            predicate=record.label,
        )
    reason: PredicateAdmissionReason
    if mapped != lexical:
        reason = "exact_mapping"
    elif record.lifecycle == "provisional":
        reason = "provisional"
    else:
        reason = "canonical"
    return _decision(proposal, reason, record.lifecycle, predicate=record.label)


def _decision(
    proposal: PredicateProposal,
    reason: PredicateAdmissionReason,
    state: PredicateAdmissionState,
    *,
    predicate: str,
    swap: bool = False,
) -> PredicateAdmissionDecision:
    subject_id = proposal.subject_id
    object_id = proposal.object_id
    if swap:
        if object_id is None:  # guarded by PredicateProposal, retained fail closed
            raise ValueError("cannot swap a literal predicate")
        subject_id, object_id = object_id, subject_id
    return PredicateAdmissionDecision(
        reason=reason,
        state=state,
        raw_predicate=proposal.raw_predicate,
        predicate=predicate,
        subject_id=subject_id,
        object_id=object_id,
        object_literal=proposal.object_literal,
        swapped=swap,
    )


def _registry_snapshot(value: Mapping[str, PredicateRecord]) -> dict[str, PredicateRecord]:
    if not isinstance(value, Mapping):
        raise TypeError("registry must be a mapping")
    result: dict[str, PredicateRecord] = {}
    for label, record in value.items():
        if not isinstance(label, str) or not isinstance(record, PredicateRecord):
            raise ValueError("registry must map predicate labels to PredicateRecord values")
        if label != record.label:
            raise ValueError("registry key must equal PredicateRecord.label")
        result[label] = record
    return result


def _mapping_snapshot(value: Mapping[str, str], field_name: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    result: dict[str, str] = {}
    for source, target in value.items():
        if not isinstance(source, str) or not source or predicate_label_key(source) != source:
            raise ValueError(f"{field_name} source must be normalized")
        if not isinstance(target, str) or not target or predicate_label_key(target) != target:
            raise ValueError(f"{field_name} target must be normalized")
        result[source] = target
    return result


def _is_structural_noise(raw: str, normalized: str) -> bool:
    words = raw.replace("_", " ").replace("-", " ").split()
    sentence_shaped = len(words) > 6 and any(character.isspace() for character in raw)
    return sentence_shaped or normalized in _GRAMMATICAL_GLUE


def _is_verbose_label(normalized: str) -> bool:
    return len(normalized.split("_")) > 6


__all__ = [
    "PLACEHOLDER_PREDICATES",
    "PredicateAdmissionAction",
    "PredicateAdmissionDecision",
    "PredicateAdmissionReason",
    "PredicateAdmissionState",
    "PredicateLiteral",
    "PredicateObjectKind",
    "PredicateProposal",
    "admit_predicate",
]
