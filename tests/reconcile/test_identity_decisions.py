"""ADR 0040 typed identity decisions remain deterministic and off-graph."""

from __future__ import annotations

import json

import pytest

from okto_neuron.reconcile import (
    AmbiguousReview,
    AuthorityIndex,
    DistinctDecision,
    IdentityDecisionIndex,
    IdentityDecisionStoreError,
    TypeCorrection,
)
from okto_neuron.reconcile.decisions import (
    IDENTITY_DECISIONS,
    IDENTITY_DECISIONS_SCHEMA_VERSION,
)


def _decisions():
    return (
        TypeCorrection(
            decision_id="type-1",
            candidate_id="candidate-1",
            previous_type="Concept",
            corrected_type="Agent",
            reason="source says this referent acts",
            evidence={"claim_ids": ["claim-1"]},
            judge_model="fixture",
            prompt_version="primitive-guide.v1",
            semantic_policy_fingerprint="policy-1",
            created_at="2026-07-16T00:00:00Z",
        ),
        DistinctDecision(
            decision_id="distinct-1",
            left_id="entity-z",
            right_id="entity-a",
            reason="same surface, contradictory senses",
            evidence={"source_ids": ["source-2", "source-1"]},
            semantic_policy_fingerprint="policy-1",
        ),
        AmbiguousReview(
            decision_id="review-1",
            candidate_ids=("candidate-z", "candidate-a", "candidate-z"),
            possible_types=("Place", "Concept", "Place"),
            reason="insufficient evidence",
            evidence={"anchors": []},
            semantic_policy_fingerprint="policy-1",
        ),
    )


def test_typed_decisions_round_trip_in_deterministic_order(tmp_path) -> None:
    index = IdentityDecisionIndex(tmp_path / "authority")
    for decision in _decisions():
        index.append(decision)

    payload = json.loads(index.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == IDENTITY_DECISIONS_SCHEMA_VERSION
    assert [record["kind"] for record in payload["records"]] == [
        "type_correction",
        "distinct",
        "ambiguous_review",
    ]
    assert index.records() == IdentityDecisionIndex(index.dir).records()
    assert index.records()[1].pair == ("entity-a", "entity-z")
    assert index.records()[2].candidate_ids == ("candidate-a", "candidate-z")
    assert index.records()[2].possible_types == ("Concept", "Place")

    second = IdentityDecisionIndex(tmp_path / "second-authority")
    for decision in _decisions():
        second.append(decision)
    assert second.path.read_bytes() == index.path.read_bytes()


def test_missing_file_is_safe_empty_legacy_state(tmp_path) -> None:
    index = IdentityDecisionIndex(tmp_path / "authority")

    assert index.records() == ()
    assert index.distinct_pairs() == frozenset()
    assert index.is_distinct("a", "b") is False
    assert not (index.dir / IDENTITY_DECISIONS).exists()


def test_distinct_decision_is_direction_independent_negative_cache(tmp_path) -> None:
    index = IdentityDecisionIndex(tmp_path / "authority")
    index.append(_decisions()[1])

    assert index.distinct_pairs() == frozenset({("entity-a", "entity-z")})
    assert index.is_distinct("entity-a", "entity-z") is True
    assert index.is_distinct("entity-z", "entity-a") is True
    assert index.is_distinct("entity-a", "other") is False


def test_type_corrections_fold_in_append_order_and_reject_stale_chain(tmp_path) -> None:
    index = IdentityDecisionIndex(tmp_path / "authority")
    index.append(
        TypeCorrection(
            decision_id="type-1",
            candidate_id="candidate",
            previous_type="Concept",
            corrected_type="InformationObject",
            reason="source describes a work",
        )
    )
    index.append(
        TypeCorrection(
            decision_id="type-2",
            candidate_id="candidate",
            previous_type="InformationObject",
            corrected_type="Activity",
            reason="source describes an event rather than the work",
        )
    )

    assert index.corrected_type("candidate", "Concept") == "Activity"
    assert index.corrected_type("other", "Place") == "Place"
    with pytest.raises(IdentityDecisionStoreError, match="chain"):
        index.corrected_type("candidate", "Agent")

    before = index.path.read_bytes()
    with pytest.raises(IdentityDecisionStoreError, match="chain"):
        index.append(
            TypeCorrection(
                decision_id="type-poison",
                candidate_id="candidate",
                previous_type="Place",
                corrected_type="Concept",
                reason="stale writer",
            )
        )
    assert index.path.read_bytes() == before


def test_later_explicit_decisions_resolve_append_only_ambiguity(tmp_path) -> None:
    index = IdentityDecisionIndex(tmp_path / "authority")
    index.append(
        AmbiguousReview(
            decision_id="type-review",
            candidate_ids=("left",),
            possible_types=("Agent", "Place"),
            reason="same surface has conflicting primitive evidence",
        )
    )
    index.append(
        AmbiguousReview(
            decision_id="identity-review",
            candidate_ids=("right", "third"),
            reason="identity unclear",
        )
    )
    assert index.unresolved_ambiguous_candidate_ids() == frozenset({"left", "right", "third"})

    index.append(
        TypeCorrection(
            decision_id="type",
            candidate_id="left",
            previous_type="Place",
            corrected_type="Agent",
            reason="source identifies a person",
        )
    )
    index.append(
        DistinctDecision(
            decision_id="distinct",
            left_id="right",
            right_id="third",
            reason="two separately grounded senses",
        )
    )

    assert index.unresolved_ambiguous_candidate_ids() == frozenset()


def test_unrelated_distinct_decision_does_not_clear_type_ambiguity(tmp_path) -> None:
    index = IdentityDecisionIndex(tmp_path / "authority")
    index.append(
        AmbiguousReview(
            decision_id="type-review",
            candidate_ids=("candidate",),
            possible_types=("Agent", "Concept"),
            reason="type unclear",
        )
    )
    index.append(
        DistinctDecision(
            decision_id="unrelated-distinct",
            left_id="candidate",
            right_id="other",
            reason="different entities",
        )
    )

    assert index.unresolved_ambiguous_candidate_ids() == frozenset({"candidate"})


def test_identity_ambiguity_resolves_only_for_the_exact_distinct_pair(tmp_path) -> None:
    index = IdentityDecisionIndex(tmp_path / "authority")
    index.append(
        AmbiguousReview(
            decision_id="identity-review",
            candidate_ids=("left", "right"),
            reason="identity unclear",
        )
    )
    index.append(
        DistinctDecision(
            decision_id="unrelated",
            left_id="left",
            right_id="third",
            reason="different entities",
        )
    )
    assert index.unresolved_ambiguous_candidate_ids() == frozenset({"left", "right"})

    index.append(
        DistinctDecision(
            decision_id="answer",
            left_id="left",
            right_id="right",
            reason="two grounded senses",
        )
    )
    assert index.unresolved_ambiguous_candidate_ids() == frozenset()


def test_decision_evidence_is_detached_from_caller_mutation() -> None:
    raw = {"claim_ids": ["claim-1"]}
    decision = TypeCorrection(
        decision_id="type",
        candidate_id="candidate",
        previous_type="Concept",
        corrected_type="Agent",
        reason="source evidence",
        evidence=raw,
    )

    raw["claim_ids"].append("injected")
    assert decision.evidence == {"claim_ids": ["claim-1"]}


def test_stale_instances_re_read_under_lock_without_lost_append(tmp_path) -> None:
    first = IdentityDecisionIndex(tmp_path / "authority")
    stale = IdentityDecisionIndex(tmp_path / "authority")

    first.append(_decisions()[0])
    stale.append(_decisions()[1])

    assert IdentityDecisionIndex(first.dir).records() == _decisions()[:2]


def test_post_replace_failure_keeps_memory_and_next_append_aligned(tmp_path, monkeypatch) -> None:
    import okto_neuron.reconcile.decisions as decisions_module

    index = IdentityDecisionIndex(tmp_path / "authority")
    real_fsync = decisions_module._fsync_directory
    calls = 0

    def fail_once(path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("directory fsync failed")
        real_fsync(path)

    monkeypatch.setattr(decisions_module, "_fsync_directory", fail_once)

    with pytest.raises(OSError, match="directory fsync failed"):
        index.append(_decisions()[0])

    # The rename happened, but durability was not proven, so the in-memory
    # projection is intentionally not published. The next locked append
    # re-reads the installed file before adding another decision.
    assert index.records() == ()
    assert IdentityDecisionIndex(index.dir).records() == (_decisions()[0],)
    index.append(_decisions()[1])
    assert IdentityDecisionIndex(index.dir).records() == _decisions()[:2]


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        '{"schema_version":"identity_decisions.v999","records":[]}',
        '{"schema_version":"identity_decisions.v1","records":{}}',
        '{"schema_version":"identity_decisions.v1","records":[{"kind":"future"}]}',
        '{"schema_version":"identity_decisions.v1","records":[],"future":true}',
        (
            '{"schema_version":"identity_decisions.v1","records":['
            '{"kind":"distinct","decision_id":"d","left_id":"a",'
            '"right_id":"b","reason":"different","future":true}]}'
        ),
        (
            '{"schema_version":"identity_decisions.v1","records":['
            '{"kind":"distinct","decision_id":"d","left_id":"same",'
            '"right_id":"same","reason":"bad"}]}'
        ),
    ],
)
def test_corrupt_or_unsupported_file_fails_closed(tmp_path, payload: str) -> None:
    path = tmp_path / "authority" / IDENTITY_DECISIONS
    path.parent.mkdir()
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(IdentityDecisionStoreError):
        IdentityDecisionIndex(path.parent)


def test_duplicate_decision_id_is_rejected_without_changing_file(tmp_path) -> None:
    index = IdentityDecisionIndex(tmp_path / "authority")
    first = _decisions()[0]
    index.append(first)
    before = index.path.read_bytes()

    with pytest.raises(IdentityDecisionStoreError, match="duplicate"):
        index.append(first)

    assert index.path.read_bytes() == before
    assert index.records() == (first,)


def test_duplicate_decision_id_in_file_fails_closed(tmp_path) -> None:
    record = _decisions()[1].to_json()
    path = tmp_path / "authority" / IDENTITY_DECISIONS
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "schema_version": IDENTITY_DECISIONS_SCHEMA_VERSION,
                "records": [record, record],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(IdentityDecisionStoreError, match="duplicate"):
        IdentityDecisionIndex(path.parent)


def test_non_utf8_file_fails_closed(tmp_path) -> None:
    path = tmp_path / "authority" / IDENTITY_DECISIONS
    path.parent.mkdir()
    path.write_bytes(b"\xff")

    with pytest.raises(IdentityDecisionStoreError, match="invalid identity decisions"):
        IdentityDecisionIndex(path.parent)


def test_failed_refresh_retains_last_known_good_negative_cache(tmp_path) -> None:
    index = IdentityDecisionIndex(tmp_path / "authority")
    index.append(_decisions()[1])
    index.path.write_text("not-json", encoding="utf-8")

    with pytest.raises(IdentityDecisionStoreError, match="invalid identity decisions"):
        index.load()

    assert index.is_distinct("entity-a", "entity-z") is True


def test_append_over_corrupt_file_raises_typed_store_error(tmp_path) -> None:
    index = IdentityDecisionIndex(tmp_path / "authority")
    index.path.parent.mkdir(parents=True, exist_ok=True)
    index.path.write_text("not-json", encoding="utf-8")

    with pytest.raises(IdentityDecisionStoreError, match="invalid identity decisions"):
        index.append(_decisions()[0])


def test_corrupt_decisions_file_does_not_affect_positive_authority_index(tmp_path) -> None:
    authority_dir = tmp_path / "authority"
    authority_dir.mkdir()
    (authority_dir / IDENTITY_DECISIONS).write_text("not-json", encoding="utf-8")

    assert AuthorityIndex(authority_dir).equivalence_map() == {}


def test_ambiguous_review_direct_input_uses_typed_validation() -> None:
    with pytest.raises(IdentityDecisionStoreError, match="closed primitive"):
        AmbiguousReview(
            decision_id="review",
            candidate_ids=("candidate",),
            possible_types=("Agent", "not-a-primitive"),  # type: ignore[arg-type]
            reason="invalid fixture",
        )


def test_invalid_type_correction_fails_before_persistence(tmp_path) -> None:
    index = IdentityDecisionIndex(tmp_path / "authority")

    with pytest.raises(IdentityDecisionStoreError, match="must change"):
        index.append(
            TypeCorrection(
                decision_id="bad",
                candidate_id="candidate",
                previous_type="Agent",
                corrected_type="Agent",
                reason="not a correction",
            )
        )

    assert not index.path.exists()
