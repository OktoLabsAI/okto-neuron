from __future__ import annotations

from dataclasses import replace
import json
import threading

import pytest

from okto_neuron.predicates import (
    PREDICATE_REGISTRY,
    PREDICATE_REGISTRY_SCHEMA_VERSION,
    PredicateDecisionProvenance,
    PredicateEvidenceSample,
    PredicateRecord,
    PredicateRegistry,
    PredicateRegistryError,
    PredicateTypeSignature,
    builtin_predicate_records,
    render_registry_block,
)


def _provenance(*, decision_id: str = "decision-1") -> PredicateDecisionProvenance:
    return PredicateDecisionProvenance(
        source="human",
        decision_id=decision_id,
        judge_model="",
        prompt_version="",
        semantic_policy_fingerprint="policy-v1",
        created_at="2026-07-16T12:00:00Z",
    )


def _record(label: str = "lives_in", *, support: int = 3) -> PredicateRecord:
    return PredicateRecord(
        label=label,
        lifecycle="canonical",
        definition="The subject lives in the object place.",
        direction="subject_to_object",
        symmetric=False,
        signatures=(PredicateTypeSignature("Agent", "Place", support),),
        support_count=support,
        samples=(PredicateEvidenceSample("claim-1", "source-1"),),
        confidence=0.95,
        provenance=_provenance(decision_id=f"decision-{label}"),
    )


def test_registry_missing_is_safe_empty_and_round_trips_atomically(tmp_path) -> None:
    registry = PredicateRegistry(tmp_path)
    assert registry.records() == ()
    assert registry.labels() == frozenset()

    registry.upsert(_record())

    path = tmp_path / ".marginalia" / "predicates" / PREDICATE_REGISTRY
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == PREDICATE_REGISTRY_SCHEMA_VERSION
    assert path.stat().st_mode & 0o077 == 0
    assert PredicateRegistry(tmp_path).records() == registry.records()


def test_builtin_records_cover_core_alias_targets_and_pack_vocabulary() -> None:
    from okto_neuron.curator import _CORE_PREDICATE_ALIASES, _SDLC_PREDICATE_ALIASES

    core = {record.label for record in builtin_predicate_records()}
    sdlc = {record.label for record in builtin_predicate_records(("sdlc",))}

    assert set(_CORE_PREDICATE_ALIASES.values()) <= core
    assert "has_value" in core
    assert set(_SDLC_PREDICATE_ALIASES.values()) <= sdlc
    assert "validated_by" not in core


def test_seed_builtins_is_atomic_idempotent_and_preserves_existing_decision(
    tmp_path,
) -> None:
    registry = PredicateRegistry(tmp_path)
    existing = replace(_record("uses"), definition="Vault-owned definition.")
    registry.upsert(existing)

    added = registry.seed_builtins(("sdlc",))
    first_bytes = registry.path.read_bytes()
    second_added = registry.seed_builtins(("sdlc",))

    assert "uses" not in added
    assert "risk" in added
    assert second_added == ()
    assert registry.path.read_bytes() == first_bytes
    assert PredicateRegistry(tmp_path).get("uses") == existing


def test_apply_planned_absolute_record_is_idempotent_after_lost_receipt(tmp_path) -> None:
    registry = PredicateRegistry(tmp_path)
    planned = _record("lives_in")

    assert registry.apply_planned(planned, expected_before=None) == planned
    first_bytes = registry.path.read_bytes()
    assert registry.apply_planned(planned, expected_before=None) == planned

    assert registry.path.read_bytes() == first_bytes


def test_apply_planned_support_update_uses_exact_precondition(tmp_path) -> None:
    registry = PredicateRegistry(tmp_path)
    before = _record("uses", support=2)
    after = _record("uses", support=3)
    registry.upsert(before)

    registry.apply_planned(after, expected_before=before)
    registry.apply_planned(after, expected_before=before)

    assert PredicateRegistry(tmp_path).get("uses") == after


def test_apply_planned_refuses_concurrent_drift_and_canonical_downgrade(tmp_path) -> None:
    registry = PredicateRegistry(tmp_path)
    before = _record("uses", support=2)
    concurrent = _record("uses", support=4)
    registry.upsert(concurrent)

    with pytest.raises(PredicateRegistryError, match="precondition changed"):
        registry.apply_planned(_record("uses", support=3), expected_before=before)

    with pytest.raises(PredicateRegistryError, match="cannot downgrade canonical"):
        registry.apply_planned(
            replace(before, lifecycle="provisional"),
            expected_before=before,
        )


def test_policy_projection_excludes_observational_evidence(tmp_path) -> None:
    registry = PredicateRegistry(tmp_path)
    original = _record()
    registry.upsert(original)
    baseline = registry.policy_projection()

    registry.upsert(
        replace(
            original,
            signatures=(PredicateTypeSignature("Concept", "Place", 7),),
            support_count=7,
            samples=(PredicateEvidenceSample("claim-2", "source-2"),),
            confidence=0.51,
            provenance=_provenance(decision_id="new-evidence"),
        )
    )

    assert registry.policy_projection() == baseline


@pytest.mark.parametrize(
    "change",
    [
        {"lifecycle": "provisional"},
        {"definition": "A materially different relation definition."},
        {"direction": "symmetric", "symmetric": True},
    ],
)
def test_policy_projection_changes_for_admission_semantics(tmp_path, change) -> None:
    registry = PredicateRegistry(tmp_path)
    original = _record()
    registry.upsert(original)
    baseline = registry.policy_projection()

    registry.upsert(replace(original, **change))

    assert registry.policy_projection() != baseline


def test_registry_upsert_replaces_one_label_and_sorts_records(tmp_path) -> None:
    registry = PredicateRegistry(tmp_path)
    registry.upsert(_record("uses", support=2))
    registry.upsert(_record("lives_in", support=3))
    registry.upsert(replace(_record("uses", support=4), lifecycle="provisional"))

    assert [record.label for record in registry.records()] == ["lives_in", "uses"]
    assert registry.get("uses").support_count == 4
    assert registry.get("uses").lifecycle == "provisional"


def test_registry_rereads_under_cross_instance_lock_without_lost_update(tmp_path) -> None:
    left = PredicateRegistry(tmp_path)
    right = PredicateRegistry(tmp_path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def write(registry: PredicateRegistry, record: PredicateRecord) -> None:
        try:
            barrier.wait()
            registry.upsert(record)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=write, args=(left, _record("lives_in"))),
        threading.Thread(target=write, args=(right, _record("protects"))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert PredicateRegistry(tmp_path).labels() == frozenset({"lives_in", "protects"})


def test_registry_atomic_update_prevents_same_label_lost_update(tmp_path) -> None:
    registry = PredicateRegistry(tmp_path)
    registry.upsert(_record("uses", support=1))
    left = PredicateRegistry(tmp_path)
    right = PredicateRegistry(tmp_path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def increment(store: PredicateRegistry) -> None:
        try:
            barrier.wait()
            store.update(
                "uses",
                lambda current: replace(
                    current,
                    support_count=current.support_count + 1,
                    signatures=(
                        replace(
                            current.signatures[0],
                            count=current.signatures[0].count + 1,
                        ),
                    ),
                ),
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [threading.Thread(target=increment, args=(store,)) for store in (left, right)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert PredicateRegistry(tmp_path).get("uses").support_count == 3


def test_registry_upsert_fails_with_typed_error_when_existing_file_is_corrupt(tmp_path) -> None:
    registry = PredicateRegistry(tmp_path)
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(PredicateRegistryError, match="invalid predicate registry"):
        registry.upsert(_record())


def test_registry_update_validates_target_and_transform(tmp_path) -> None:
    registry = PredicateRegistry(tmp_path)
    registry.upsert(_record())

    with pytest.raises(PredicateRegistryError, match="not registered"):
        registry.update("missing", lambda current: current)
    with pytest.raises(TypeError, match="return a PredicateRecord"):
        registry.update("lives_in", lambda _current: None)  # type: ignore[arg-type,return-value]
    with pytest.raises(PredicateRegistryError, match="cannot rename"):
        registry.update("lives_in", lambda current: replace(current, label="protects"))


def test_registry_get_requires_an_exact_trimmed_label(tmp_path) -> None:
    registry = PredicateRegistry(tmp_path)
    registry.upsert(_record())

    assert registry.get("lives_in") is not None
    assert registry.get(" lives_in") is None
    assert registry.get("") is None


def test_registry_save_sweeps_only_its_stale_temp_files(tmp_path) -> None:
    registry = PredicateRegistry(tmp_path)
    registry.dir.mkdir(parents=True, exist_ok=True)
    stale = registry.dir / f".{PREDICATE_REGISTRY}.stale.tmp"
    unrelated = registry.dir / ".unrelated.tmp"
    stale.write_text("stale", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")

    registry.upsert(_record())

    assert not stale.exists()
    assert unrelated.exists()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"schema_version": "future", "records": []},
        {"schema_version": PREDICATE_REGISTRY_SCHEMA_VERSION},
        {
            "schema_version": PREDICATE_REGISTRY_SCHEMA_VERSION,
            "records": [],
            "extra": True,
        },
        {
            "schema_version": PREDICATE_REGISTRY_SCHEMA_VERSION,
            "records": [_record().to_json(), _record().to_json()],
        },
        {
            "schema_version": PREDICATE_REGISTRY_SCHEMA_VERSION,
            "records": [{**_record().to_json(), "unknown": True}],
        },
    ],
)
def test_registry_fails_closed_on_corrupt_or_future_whole_file(tmp_path, payload) -> None:
    path = tmp_path / ".marginalia" / "predicates" / PREDICATE_REGISTRY
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PredicateRegistryError):
        PredicateRegistry(tmp_path)


@pytest.mark.parametrize(
    "record",
    [
        lambda: replace(_record(), label="Lives In"),
        lambda: replace(_record(), label="letter_to"),
        lambda: replace(_record(), direction="symmetric", symmetric=False),
        lambda: replace(_record(), symmetric=True),
        lambda: replace(_record(), direction="unknown", symmetric=False),
        lambda: replace(_record(), direction="subject_to_object", symmetric=None),
        lambda: replace(_record(), definition="two\nlines"),
        lambda: replace(_record(), definition="x" * 501),
        lambda: replace(_record(), support_count=0),
        lambda: replace(_record(), confidence=float("nan")),
        lambda: replace(_record(), confidence=1.1),
        lambda: replace(_record(), signatures=(PredicateTypeSignature("Agent", "Place", 4),)),
        lambda: replace(
            _record(),
            signatures=(
                PredicateTypeSignature("Agent", "Place", 1),
                PredicateTypeSignature("Agent", "Place", 2),
            ),
        ),
        lambda: replace(_record(), samples=(_record().samples[0], _record().samples[0])),
    ],
)
def test_record_rejects_unnormalized_or_incoherent_state(record) -> None:
    with pytest.raises(PredicateRegistryError):
        record()


def test_model_provenance_requires_model_and_prompt() -> None:
    with pytest.raises(PredicateRegistryError, match="model decisions"):
        PredicateDecisionProvenance(
            source="model",
            decision_id="decision-1",
            judge_model="",
            prompt_version="",
            semantic_policy_fingerprint="policy-v1",
            created_at="2026-07-16T12:00:00Z",
        )


@pytest.mark.parametrize(
    "created_at",
    ["not-a-time", "2026-07-16T12:00:00", "2026-07-16"],
)
def test_provenance_requires_timezone_aware_iso_timestamp(created_at: str) -> None:
    with pytest.raises(PredicateRegistryError, match="created_at"):
        replace(_provenance(), created_at=created_at)


def test_registry_record_contains_only_ids_not_source_excerpts() -> None:
    payload = _record().to_json()
    rendered = json.dumps(payload)

    assert payload["samples"] == [{"claim_id": "claim-1", "source_id": "source-1"}]
    assert "excerpt" not in rendered
    assert "api_key" not in rendered


# ── render_registry_block ─────────────────────────────────────────────────────


def _model_provisional(
    label: str,
    *,
    definition: str,
    direction: str = "subject_to_object",
    support: int = 62,
) -> PredicateRecord:
    """A record shaped exactly like one the relation curator mints at ingest."""

    return PredicateRecord(
        label=label,
        lifecycle="provisional",
        definition=definition,
        direction=direction,  # type: ignore[arg-type]
        symmetric=(direction == "symmetric"),
        signatures=(),
        support_count=support,
        samples=(),
        confidence=0.9,
        provenance=PredicateDecisionProvenance(
            source="model",
            decision_id=f"relation-curator:candidate:{label}",
            judge_model="test/model",
            prompt_version="relation_curator_evidence.v2",
            semantic_policy_fingerprint="policy-v1",
            created_at="2026-07-20T00:00:00+00:00",
        ),
    )


def test_registry_block_is_byte_stable_under_dict_reordering() -> None:
    records = [
        _record("uses", support=3),
        _model_provisional("states", definition="The subject states the object.", support=42),
        _model_provisional("impact", definition="The subject impacts the object.", support=8),
    ]
    forward = render_registry_block({record.label: record for record in records})
    backward = render_registry_block({record.label: record for record in reversed(records)})

    assert forward == backward
    # Canonical first, then support descending.
    assert forward.splitlines()[0].startswith("uses | ")
    assert forward.splitlines()[1].startswith("states | ")
    assert forward.splitlines()[2].startswith("impact | ")


def test_registry_block_cap_keeps_all_canonicals() -> None:
    canonicals = [_record(f"canon_{index}", support=1) for index in range(5)]
    provisionals = [
        _model_provisional(f"prov_{index}", definition="A provisional thing.", support=index)
        for index in range(20)
    ]
    block = render_registry_block(
        {record.label: record for record in canonicals + provisionals},
        cap=8,
    )

    lines = block.splitlines()
    for record in canonicals:
        assert any(line.startswith(f"{record.label} | ") for line in lines)
    assert lines[-1] == "... 17 further low-support predicates omitted"
    # Highest-support provisionals fill the remaining room, lowest are dropped.
    assert any(line.startswith("prov_19 | ") for line in lines)
    assert not any(line.startswith("prov_0 | ") for line in lines)
