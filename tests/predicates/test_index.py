from __future__ import annotations

import json
import threading

import pytest

from okto_neuron.predicates import (
    PREDICATE_ALIASES,
    PREDICATE_SCHEMA_VERSION,
    PredicateAliasIndex,
    PredicateAliasRecord,
)


def _record(
    record_id: str,
    subject: str,
    obj: str,
    *,
    mapping: str = "exact_match",
    status: str = "auto",
    confidence: float = 0.9,
    counts: dict[str, int] | None = None,
) -> PredicateAliasRecord:
    return PredicateAliasRecord(
        id=record_id,
        subject_predicate=subject,
        mapping=mapping,  # type: ignore[arg-type]
        object_predicate=obj,
        confidence=confidence,
        justification="test",
        evidence={"counts": counts or {subject: 1, obj: 1}},
        judge_model="fake-judge",
        votes={"forward": {"verdict": "same"}},
        status=status,  # type: ignore[arg-type]
        created_at="2026-06-12T00:00:00+00:00",
    )


def test_index_round_trip_persists_at_vault_marginalia_path(tmp_path) -> None:
    idx = PredicateAliasIndex(tmp_path)
    idx.upsert(_record("r1", "located_at", "located_in"))

    path = tmp_path / ".marginalia" / "predicates" / PREDICATE_ALIASES
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == PREDICATE_SCHEMA_VERSION
    assert len(data["records"]) == 1
    assert path.stat().st_mode & 0o077 == 0

    reloaded = PredicateAliasIndex(tmp_path)
    assert reloaded.records() == idx.records()
    assert reloaded.judged_pairs() == {("located_at", "located_in")}


def test_union_find_resolution_uses_highest_evidence_root(tmp_path) -> None:
    idx = PredicateAliasIndex(tmp_path)
    idx.upsert(_record("ab", "a", "b", counts={"a": 3, "b": 5}))
    idx.upsert(_record("bc", "b", "c", counts={"b": 5, "c": 9}))

    aliases = idx.alias_map()
    assert aliases["a"] == "c"
    assert aliases["b"] == "c"
    assert "c" not in aliases


def test_core_alias_precedence_is_not_overridden(tmp_path) -> None:
    idx = PredicateAliasIndex(tmp_path)
    idx.upsert(
        _record(
            "core",
            "letter_to",
            "communicates_with",
            counts={"letter_to": 1, "communicates_with": 99},
        )
    )

    assert idx.alias_map()["letter_to"] == "wrote_to"


def test_cycle_breaks_by_highest_evidence_count(tmp_path) -> None:
    idx = PredicateAliasIndex(tmp_path)
    idx.upsert(_record("ab", "a", "b", counts={"a": 2, "b": 10}))
    idx.upsert(_record("bc", "b", "c", counts={"b": 10, "c": 4}))
    idx.upsert(_record("ca", "c", "a", counts={"c": 4, "a": 2}))

    aliases = idx.alias_map()
    assert aliases["a"] == "b"
    assert aliases["c"] == "b"
    assert "b" not in aliases


def test_alias_map_ignores_rejected_and_queued_exact_matches(tmp_path) -> None:
    idx = PredicateAliasIndex(tmp_path)
    idx.upsert(_record("queued", "x", "y", status="queued"))
    idx.upsert(_record("rejected", "a", "b", status="rejected"))

    assert idx.alias_map() == {}
    assert idx.judged_pairs() == {("x", "y"), ("a", "b")}


def test_inverse_map_is_confirmed_inverse_only(tmp_path) -> None:
    idx = PredicateAliasIndex(tmp_path)
    idx.upsert(
        _record("confirmed", "shared_by", "shared_with", mapping="inverse_of", status="confirmed")
    )
    idx.upsert(_record("auto", "sent_by", "sent_to", mapping="inverse_of", status="auto"))

    assert idx.inverse_map() == {
        "shared_by": "shared_with",
        "shared_with": "shared_by",
    }


def test_stale_instances_reread_under_lock_without_lost_update(tmp_path) -> None:
    left = PredicateAliasIndex(tmp_path)
    right = PredicateAliasIndex(tmp_path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def write(index: PredicateAliasIndex, record: PredicateAliasRecord) -> None:
        try:
            barrier.wait()
            index.upsert(record)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        threading.Thread(target=write, args=(left, _record("ab", "a", "b"))),
        threading.Thread(target=write, args=(right, _record("cd", "c", "d"))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert {record.id for record in PredicateAliasIndex(tmp_path).records()} == {"ab", "cd"}


def test_failed_atomic_replace_preserves_previous_file_and_memory(tmp_path, monkeypatch) -> None:
    import okto_neuron.predicates.index as index_module

    index = PredicateAliasIndex(tmp_path)
    first = _record("ab", "a", "b")
    index.upsert(first)
    before = index.path.read_bytes()

    def fail_replace(_source, _target):
        raise OSError("replace failed")

    monkeypatch.setattr(index_module, "_replace_with_windows_retry", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        index.upsert(_record("cd", "c", "d"))

    assert index.path.read_bytes() == before
    assert index.records() == [first]


def test_record_detaches_mutable_evidence_from_caller() -> None:
    evidence = {"support": {"claim_ids": ["claim-1"]}}
    votes = {"judges": ["model-a"]}
    record = PredicateAliasRecord(
        id="alias-1",
        subject_predicate="lives_in",
        mapping="exact_match",
        object_predicate="resides_in",
        confidence=0.9,
        justification="same relation",
        evidence=evidence,
        votes=votes,
    )

    evidence["support"]["claim_ids"].append("claim-2")
    votes["judges"].append("model-b")

    assert record.evidence == {"support": {"claim_ids": ["claim-1"]}}
    assert record.votes == {"judges": ["model-a"]}


def test_fold_record_elects_incumbent_not_novel_label(tmp_path) -> None:
    """ADR 0040 D6a.4 — a fold must not be silently reversible on the next read.

    ``alias_map``'s root election is ``max`` over (evidence count, -first_seen,
    label), and the record's SUBJECT is inserted into the union-find first. So a
    fold record written with no evidence counts (or with counts that let the
    novel label win) elects the NOVEL label as root and inverts the fold —
    presenting as an intermittent, document-ordering-dependent bug.
    """

    index = PredicateAliasIndex(tmp_path)
    index.upsert(
        _record(
            "predicate-fold",
            "estado_atual",
            "has_status",
            counts={"estado_atual": 1, "has_status": 62},
        )
    )

    mapping = PredicateAliasIndex(tmp_path).alias_map()

    assert mapping["estado_atual"] == "has_status"
    assert "has_status" not in mapping


def test_fold_record_without_evidence_counts_inverts_the_fold(tmp_path) -> None:
    """The negative half of the rule above, pinned so it cannot regress quietly."""

    index = PredicateAliasIndex(tmp_path)
    index.upsert(_record("predicate-fold", "estado_atual", "has_status", counts={}))

    mapping = PredicateAliasIndex(tmp_path).alias_map()

    assert mapping == {"has_status": "estado_atual"}
