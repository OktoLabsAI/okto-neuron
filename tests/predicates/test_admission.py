from __future__ import annotations

from dataclasses import replace

import pytest

from okto_neuron.predicates import (
    PredicateAdmissionDecision,
    PredicateDecisionProvenance,
    PredicateEvidenceSample,
    PredicateProposal,
    PredicateRecord,
    PredicateTypeSignature,
    admit_predicate,
)
from okto_neuron.curator import normalize_predicate


def _record(label: str, *, lifecycle: str = "canonical") -> PredicateRecord:
    return PredicateRecord(
        label=label,
        lifecycle=lifecycle,  # type: ignore[arg-type]
        definition=f"Governed definition for {label}.",
        direction="subject_to_object",
        symmetric=False,
        signatures=(PredicateTypeSignature("Agent", "Place", 1),),
        support_count=1,
        samples=(PredicateEvidenceSample("claim-1", "source-1"),),
        confidence=0.9,
        provenance=PredicateDecisionProvenance(
            source="human",
            decision_id=f"decision-{label}",
            judge_model="",
            prompt_version="",
            semantic_policy_fingerprint="policy-v1",
            created_at="2026-07-16T12:00:00Z",
        ),
    )


def _registry() -> dict[str, PredicateRecord]:
    return {
        label: _record(label, lifecycle="provisional" if label == "protects" else "canonical")
        for label in ("born_in", "lives_in", "protects", "has_role")
    }


def test_canonical_topology_is_pinned_without_swap() -> None:
    result = admit_predicate(
        PredicateProposal("lives_in", "person", object_id="place"),
        registry=_registry(),
    )

    assert result.reason == "canonical"
    assert result.action == "commit_topology"
    assert result.state == "canonical"
    assert (result.subject_id, result.predicate, result.object_id) == (
        "person",
        "lives_in",
        "place",
    )
    assert result.swapped is False


def test_exact_mapping_pins_registered_label_without_direction_change() -> None:
    result = admit_predicate(
        PredicateProposal("resides_in", "person", object_id="place"),
        registry=_registry(),
        exact_mappings={"resides_in": "lives_in"},
    )

    assert result.reason == "exact_mapping"
    assert result.predicate == "lives_in"
    assert result.subject_id == "person"
    assert result.object_id == "place"
    assert result.swapped is False


def test_core_mapping_is_observable_as_exact_mapping() -> None:
    registry = {**_registry(), "wrote_to": _record("wrote_to")}

    result = admit_predicate(
        PredicateProposal("letter_to", "person", object_id="person-2"),
        registry=registry,
    )

    assert result.reason == "exact_mapping"
    assert result.predicate == "wrote_to"


def test_core_mapping_cannot_be_overridden_by_vault_mapping() -> None:
    registry = {
        **_registry(),
        "wrote_to": _record("wrote_to"),
        "corresponds_with": _record("corresponds_with"),
    }

    result = admit_predicate(
        PredicateProposal("letter_to", "person", object_id="person-2"),
        registry=registry,
        exact_mappings={"letter_to": "corresponds_with"},
    )

    assert result.reason == "queue_mapping_conflict"
    assert result.predicate == "wrote_to"

    target_override = admit_predicate(
        PredicateProposal("letter_to", "person", object_id="person-2"),
        registry=registry,
        exact_mappings={"wrote_to": "corresponds_with"},
    )
    assert target_override.reason == "queue_mapping_conflict"
    assert target_override.predicate == "wrote_to"


def test_exact_and_inverse_decisions_for_same_label_queue_as_conflict() -> None:
    result = admit_predicate(
        PredicateProposal("birthplace_of", "place", object_id="person"),
        registry=_registry(),
        exact_mappings={"birthplace_of": "born_in"},
        inverse_mappings={"birthplace_of": "born_in"},
    )

    assert result.reason == "queue_mapping_conflict"
    assert result.action == "queue_review"
    assert result.swapped is False


def test_confirmed_inverse_is_explicit_and_pins_swapped_endpoints() -> None:
    proposal = PredicateProposal(
        "birthplace_of",
        "place",
        object_id="person",
        apply_confirmed_inverse=True,
    )
    result = admit_predicate(
        proposal,
        registry=_registry(),
        inverse_mappings={"birthplace_of": "born_in"},
    )

    assert result.reason == "confirmed_inverse"
    assert result.action == "commit_topology"
    assert result.predicate == "born_in"
    assert result.subject_id == "person"
    assert result.object_id == "place"
    assert result.swapped is True


def test_known_inverse_is_not_applied_without_explicit_adjudicated_signal() -> None:
    result = admit_predicate(
        PredicateProposal("born_in", "person", object_id="place"),
        registry=_registry(),
        inverse_mappings={"born_in": "birthplace_of"},
    )

    assert result.reason == "canonical"
    assert result.predicate == "born_in"
    assert result.swapped is False


def test_registered_provisional_label_commits_without_generic_replacement() -> None:
    result = admit_predicate(
        PredicateProposal("protects", "person", object_id="person-2"),
        registry=_registry(),
    )

    assert result.reason == "provisional"
    assert result.state == "provisional"
    assert result.predicate == "protects"


def test_literal_representation_is_explicit_and_keeps_scalar() -> None:
    result = admit_predicate(
        PredicateProposal("has_role", "person", object_literal="ring-bearer"),
        registry=_registry(),
    )

    assert result.reason == "literal"
    assert result.action == "commit_literal"
    assert result.object_kind == "literal"
    assert result.object_literal == "ring-bearer"
    assert result.object_id is None


@pytest.mark.parametrize("raw", ["unknown", "NONE", "n/a", "None."])
def test_placeholder_is_rejected_before_registry(raw: str) -> None:
    result = admit_predicate(
        PredicateProposal(raw, "person", object_id="place"),
        registry=_registry(),
    )

    assert result.reason == "reject_placeholder"
    assert result.action == "reject"
    assert result.state == "placeholder"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("...", "reject_malformed"),
        ("écrit_par", "reject_malformed"),
        ("and", "reject_structural_noise"),
        ("this predicate is really a complete explanatory sentence", "reject_structural_noise"),
    ],
)
def test_malformed_and_structural_labels_never_reach_commit(raw: str, reason: str) -> None:
    result = admit_predicate(
        PredicateProposal(raw, "person", object_id="place"),
        registry=_registry(),
    )

    assert result.reason == reason
    assert result.action == "reject"


def test_uncommon_grounded_label_is_queued_until_registered_not_genericized() -> None:
    result = admit_predicate(
        PredicateProposal("rare_but_precise", "person", object_id="place"),
        registry=_registry(),
    )

    assert result.reason == "queue_unregistered"
    assert result.action == "queue_review"
    assert result.predicate == "rare_but_precise"


def test_verbose_but_well_formed_label_routes_to_review_instead_of_rejection() -> None:
    result = admit_predicate(
        PredicateProposal(
            "reduces_time_to_first_byte_by_percent",
            "person",
            object_id="place",
        ),
        registry=_registry(),
    )

    assert result.reason == "queue_verbose_label"
    assert result.action == "queue_review"


def test_mapping_or_registered_provisional_unblocks_a_verbose_label() -> None:
    raw = "reduces_time_to_first_byte_by_percent"
    mapped = admit_predicate(
        PredicateProposal(raw, "person", object_id="place"),
        registry={**_registry(), "impact": _record("impact")},
        exact_mappings={raw: "impact"},
    )
    provisional_record = _record(raw, lifecycle="provisional")
    provisional = admit_predicate(
        PredicateProposal(raw, "person", object_id="place"),
        registry={**_registry(), raw: provisional_record},
    )

    assert (mapped.reason, mapped.predicate) == ("exact_mapping", "impact")
    assert (provisional.reason, provisional.predicate) == ("provisional", raw)


@pytest.mark.parametrize(
    "proposal",
    [
        lambda: PredicateProposal("uses", "subject"),
        lambda: PredicateProposal("uses", "subject", object_id="object", object_literal="literal"),
        lambda: PredicateProposal("uses", "subject", object_literal=float("nan")),
        lambda: PredicateProposal(
            "uses", "subject", object_literal="literal", apply_confirmed_inverse=True
        ),
    ],
)
def test_proposal_rejects_ambiguous_or_unstable_object_shape(proposal) -> None:
    with pytest.raises(ValueError):
        proposal()


def test_requested_inverse_without_mapping_queues_instead_of_guessing() -> None:
    result = admit_predicate(
        PredicateProposal(
            "birthplace_of",
            "place",
            object_id="person",
            apply_confirmed_inverse=True,
        ),
        registry=_registry(),
    )

    assert result.reason == "queue_mapping_conflict"
    assert result.swapped is False


def test_inverse_target_core_mapping_conflict_queues_instead_of_silent_fold() -> None:
    result = admit_predicate(
        PredicateProposal(
            "received_letter_from",
            "recipient",
            object_id="author",
            apply_confirmed_inverse=True,
        ),
        registry=_registry(),
        exact_mappings={"wrote_to": "communicates_with"},
        inverse_mappings={"received_letter_from": "letter_to"},
    )

    assert result.reason == "queue_mapping_conflict"
    assert result.predicate == "wrote_to"
    assert result.swapped is False


def test_inverse_target_must_already_have_registry_record() -> None:
    result = admit_predicate(
        PredicateProposal(
            "birthplace_of",
            "place",
            object_id="person",
            apply_confirmed_inverse=True,
        ),
        registry=_registry(),
        inverse_mappings={"birthplace_of": "was_born_in"},
    )

    assert result.reason == "queue_unregistered"
    assert result.predicate == "was_born_in"
    assert result.swapped is False


def test_registry_and_mapping_snapshots_are_strictly_bound() -> None:
    proposal = PredicateProposal("lives_in", "person", object_id="place")
    with pytest.raises(ValueError, match="registry key"):
        admit_predicate(proposal, registry={"wrong": _record("lives_in")})
    with pytest.raises(ValueError, match="source must be normalized"):
        admit_predicate(
            proposal,
            registry=_registry(),
            exact_mappings={"Lives In": "lives_in"},
        )
    with pytest.raises(ValueError, match="source must be normalized"):
        admit_predicate(proposal, registry=_registry(), exact_mappings={"": "lives_in"})
    with pytest.raises(ValueError, match="target must be normalized"):
        admit_predicate(proposal, registry=_registry(), exact_mappings={"lives_in": ""})
    result = admit_predicate(
        PredicateProposal("letter_to", "person", object_id="person-2"),
        registry={**_registry(), "wrote_to": _record("wrote_to")},
        exact_mappings={"letter_to": "wrote_to"},
    )
    assert result.predicate == "wrote_to"


def test_decision_rejects_reason_state_and_representation_inconsistency() -> None:
    valid = admit_predicate(
        PredicateProposal("lives_in", "person", object_id="place"),
        registry=_registry(),
    )
    with pytest.raises(ValueError, match="reason and state"):
        replace(valid, state="queued")
    with pytest.raises(ValueError, match="commit_topology"):
        PredicateAdmissionDecision(
            reason="canonical",
            state="canonical",
            raw_predicate="lives_in",
            predicate="lives_in",
            subject_id="person",
            object_id=None,
            object_literal="place",
            swapped=False,
        )
    with pytest.raises(ValueError, match="finite"):
        PredicateAdmissionDecision(
            reason="literal",
            state="canonical",
            raw_predicate="has_role",
            predicate="has_role",
            subject_id="person",
            object_id=None,
            object_literal=float("nan"),
            swapped=False,
        )
    with pytest.raises(ValueError, match="confirmed_inverse"):
        replace(valid, reason="confirmed_inverse")
    with pytest.raises(ValueError, match="confirmed_inverse"):
        replace(valid, swapped=True)


def test_inverse_lookup_can_follow_an_exact_mapping_chain() -> None:
    result = admit_predicate(
        PredicateProposal(
            "place_of_birth",
            "place",
            object_id="person",
            apply_confirmed_inverse=True,
        ),
        registry=_registry(),
        exact_mappings={"place_of_birth": "birthplace_of"},
        inverse_mappings={"birthplace_of": "born_in"},
    )

    assert result.reason == "confirmed_inverse"
    assert result.predicate == "born_in"
    assert (result.subject_id, result.object_id) == ("person", "place")


def test_inverse_target_can_follow_a_core_mapping_before_swap() -> None:
    registry = {**_registry(), "wrote_to": _record("wrote_to")}
    result = admit_predicate(
        PredicateProposal(
            "received_letter_from",
            "recipient",
            object_id="sender",
            apply_confirmed_inverse=True,
        ),
        registry=registry,
        inverse_mappings={"received_letter_from": "letter_to"},
    )

    assert result.reason == "confirmed_inverse"
    assert result.predicate == "wrote_to"
    assert (result.subject_id, result.object_id) == ("sender", "recipient")


def test_learned_alias_can_repair_a_lexically_invalid_raw_key_before_regex_gate() -> None:
    assert (
        normalize_predicate(
            "24x7 support",
            predicate_aliases={"24x7_support": "supports"},
        )
        == "supports"
    )


def test_final_mapped_denylist_label_never_commits() -> None:
    with pytest.raises(ValueError, match="denied"):
        _record("unknown")

    result = admit_predicate(
        PredicateProposal("part_of_org", "person", object_id="place"),
        registry=_registry(),
        exact_mappings={"part_of_org": "of"},
    )
    assert result.reason == "reject_structural_noise"
    assert result.predicate == "of"


def test_inverse_chain_denylist_is_checked_after_exact_remap() -> None:
    result = admit_predicate(
        PredicateProposal(
            "birthplace_of",
            "place",
            object_id="person",
            apply_confirmed_inverse=True,
        ),
        registry=_registry(),
        inverse_mappings={"birthplace_of": "origin_of"},
        exact_mappings={"origin_of": "of"},
    )

    assert result.reason == "reject_structural_noise"
    assert result.predicate == "of"
