"""Model-free contract tests for the ADR 0040 D7 semantic relation gate."""

from __future__ import annotations

import pytest

from okto_neuron.consolidate.relation_gate import (
    EndpointDecision,
    LiteralObject,
    PinnedPredicateAdmission,
    RelationGateDecision,
    RelationGateInput,
    SourceGrounding,
    TopologyObject,
    decide_relation,
)


def _endpoint(
    entity_id: str,
    *,
    type_status: str = "accepted",
    primitive_type: str | None = "Agent",
    live: bool = True,
) -> EndpointDecision:
    return EndpointDecision(
        entity_id=entity_id,
        type_status=type_status,  # type: ignore[arg-type]
        primitive_type=primitive_type,  # type: ignore[arg-type]
        live=live,
    )


def _input(
    *,
    predicate_status: str = "canonical",
    subject: EndpointDecision | None = None,
    object_endpoint: EndpointDecision | None = None,
    literal: object | None = None,
    block_id: str | None = "block-1",
    subject_supported: bool = True,
    predicate_supported: bool = True,
    object_supported: bool = True,
    direction_supported: bool = True,
    unsupported_inference: bool = False,
    structural_noise: bool = False,
    redundant: bool = False,
) -> RelationGateInput:
    if literal is not None:
        relation_object = LiteralObject(literal)  # type: ignore[arg-type]
    else:
        relation_object = TopologyObject(object_endpoint or _endpoint("object"))
    return RelationGateInput(
        subject=subject or _endpoint("subject"),
        predicate=PinnedPredicateAdmission(
            predicate="knows",
            status=predicate_status,  # type: ignore[arg-type]
        ),
        object=relation_object,
        grounding=SourceGrounding(
            block_id=block_id,
            subject_supported=subject_supported,
            predicate_supported=predicate_supported,
            object_supported=object_supported,
            direction_supported=direction_supported,
            unsupported_inference=unsupported_inference,
        ),
        structural_noise=structural_noise,
        redundant=redundant,
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (_input(), "commit"),
        (_input(predicate_status="placeholder"), "reject_placeholder"),
        (_input(predicate_status="structural_noise"), "reject_structural_noise"),
        (_input(structural_noise=True), "reject_structural_noise"),
        (_input(unsupported_inference=True), "reject_unsupported_inference"),
        (_input(subject=_endpoint("subject", live=False)), "reject_dead_endpoint"),
        (_input(object_endpoint=_endpoint("object", live=False)), "reject_dead_endpoint"),
        (_input(redundant=True), "reject_redundant"),
        (
            _input(subject=_endpoint("subject", type_status="conflict")),
            "queue_type_conflict",
        ),
        (
            _input(object_endpoint=_endpoint("object", type_status="ambiguous")),
            "queue_type_conflict",
        ),
        (_input(predicate_status="queued"), "queue_predicate"),
        (_input(block_id=None), "queue_grounding"),
        (_input(subject_supported=False), "queue_grounding"),
        (_input(predicate_supported=False), "queue_grounding"),
        (_input(object_supported=False), "queue_grounding"),
        (_input(direction_supported=False), "queue_grounding"),
    ],
)
def test_each_closed_reason(value: RelationGateInput, expected: str) -> None:
    assert decide_relation(value).reason == expected


def test_hard_reject_precedence_before_all_queue_uncertainty() -> None:
    value = _input(
        predicate_status="placeholder",
        subject=_endpoint("subject", type_status="conflict", live=False),
        object_endpoint=_endpoint("object", type_status="ambiguous", live=False),
        block_id=None,
        unsupported_inference=True,
        structural_noise=True,
        redundant=True,
    )

    assert decide_relation(value).reason == "reject_placeholder"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            _input(predicate_status="structural_noise", unsupported_inference=True),
            "reject_structural_noise",
        ),
        (
            _input(unsupported_inference=True, subject=_endpoint("subject", live=False)),
            "reject_unsupported_inference",
        ),
        (_input(subject=_endpoint("subject", live=False), redundant=True), "reject_dead_endpoint"),
        (_input(redundant=True, predicate_status="queued"), "reject_redundant"),
        (
            _input(
                subject=_endpoint("subject", type_status="conflict"),
                predicate_status="queued",
                block_id=None,
            ),
            "queue_type_conflict",
        ),
        (_input(predicate_status="queued", block_id=None), "queue_predicate"),
    ],
)
def test_precedence_is_deterministic(value: RelationGateInput, expected: str) -> None:
    assert decide_relation(value).reason == expected


def test_topology_commit_preserves_pinned_semantics_and_supports_both_endpoints() -> None:
    value = _input(predicate_status="provisional")

    decision = decide_relation(value)

    assert decision.reason == "commit"
    assert decision.action == "commit"
    assert decision.relation_kind == "topology"
    assert decision.subject_id == "subject"
    assert decision.predicate == "knows"
    assert decision.liveness_support_ids == frozenset({"subject", "object"})


def test_literal_commit_needs_no_object_endpoint_or_direction_support() -> None:
    value = _input(literal="19% slower", direction_supported=False)

    decision = decide_relation(value)

    assert decision.reason == "commit"
    assert decision.relation_kind == "literal"
    assert decision.liveness_support_ids == frozenset({"subject"})
    assert decision.object is value.object


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            _input(literal="fact", subject=_endpoint("subject", live=False)),
            "reject_dead_endpoint",
        ),
        (
            _input(literal="fact", subject=_endpoint("subject", type_status="conflict")),
            "queue_type_conflict",
        ),
        (_input(literal="fact", predicate_status="placeholder"), "reject_placeholder"),
    ],
)
def test_literal_path_obeys_subject_and_hard_reject_gates(
    value: RelationGateInput, expected: str
) -> None:
    assert decide_relation(value).reason == expected


@pytest.mark.parametrize(
    "value",
    [
        _input(predicate_status="placeholder"),
        _input(subject=_endpoint("subject", live=False)),
        _input(predicate_status="queued"),
        _input(block_id=None),
    ],
)
def test_non_commit_relation_never_claims_liveness_support(value: RelationGateInput) -> None:
    decision = decide_relation(value)

    assert decision.reason != "commit"
    assert decision.liveness_support_ids == frozenset()


def test_queue_and_reject_actions_are_derived_from_closed_reason() -> None:
    assert decide_relation(_input(predicate_status="queued")).action == "queue"
    assert decide_relation(_input(redundant=True)).action == "reject"


@pytest.mark.parametrize("invalid_object", [None, [], float("nan"), "raw", {}])
def test_input_rejects_object_that_bypasses_literal_topology_wrappers(
    invalid_object: object,
) -> None:
    valid = _input()
    with pytest.raises(ValueError, match="TopologyObject or LiteralObject"):
        RelationGateInput(
            subject=valid.subject,
            predicate=valid.predicate,
            object=invalid_object,  # type: ignore[arg-type]
            grounding=valid.grounding,
        )


@pytest.mark.parametrize("reason", ["queue_bogus", "banana", ""])
def test_decision_rejects_reason_outside_closed_set(reason: str) -> None:
    valid = decide_relation(_input())
    with pytest.raises(ValueError, match="unknown relation gate reason"):
        RelationGateDecision(
            reason=reason,  # type: ignore[arg-type]
            subject_id=valid.subject_id,
            predicate=valid.predicate,
            object=valid.object,
        )


@pytest.mark.parametrize("literal", ["", "   ", float("nan"), float("inf"), []])
def test_literal_object_rejects_invalid_non_scalar_or_unstable_values(literal: object) -> None:
    with pytest.raises(ValueError):
        LiteralObject(literal)  # type: ignore[arg-type]


def test_accepted_endpoint_requires_closed_primitive_type() -> None:
    with pytest.raises(ValueError, match="closed primitive"):
        _endpoint("subject", primitive_type=None)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"type_status": "future"},
        {"primitive_type": "Future"},
        {"live": "yes"},
    ],
)
def test_endpoint_decision_rejects_invalid_runtime_state(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        _endpoint("subject", **kwargs)


def test_pinned_predicate_must_be_nonempty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        PinnedPredicateAdmission(predicate=" ", status="canonical")


def test_pinned_predicate_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="unknown predicate"):
        PinnedPredicateAdmission(predicate="knows", status="future")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"block_id": " "},
        {"block_id": 123},
        {"subject_supported": "false"},
        {"predicate_supported": 1},
        {"object_supported": None},
        {"direction_supported": "yes"},
        {"unsupported_inference": 0},
    ],
)
def test_source_grounding_rejects_malformed_runtime_evidence(kwargs: dict) -> None:
    values = {
        "block_id": "block-1",
        "subject_supported": True,
        "predicate_supported": True,
        "object_supported": True,
        "direction_supported": True,
        "unsupported_inference": False,
    }
    values.update(kwargs)

    with pytest.raises(ValueError):
        SourceGrounding(**values)


@pytest.mark.parametrize("field_name", ["structural_noise", "redundant"])
def test_input_rejects_non_boolean_policy_flags(field_name: str) -> None:
    valid = _input()
    values = {
        "subject": valid.subject,
        "predicate": valid.predicate,
        "object": valid.object,
        "grounding": valid.grounding,
        "structural_noise": False,
        "redundant": False,
    }
    values[field_name] = "false"

    with pytest.raises(ValueError, match="must be boolean"):
        RelationGateInput(**values)
