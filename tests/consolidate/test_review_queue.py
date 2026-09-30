"""Tests for the persistent review queue (Phase E)."""

from __future__ import annotations

REAL_QUEUE_GATE = True  # these tests exercise the version-1 layout gate itself

import json
import sqlite3

import pytest

import okto_neuron.consolidate.review_queue_sqlite as review_queue_sqlite_module
from okto_neuron.companion import Correlation, ReviewItemNotFoundError
from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.consolidate.relation_gate import (
    EndpointDecision,
    LiteralObject,
    PinnedPredicateAdmission,
    RelationGateInput,
    SourceGrounding,
    TopologyObject,
)
from okto_neuron.config._vault import vault_yaml_version
from okto_neuron.consolidate.review_queue import (
    PinnedRelationProposal,
    RelationReviewItem,
    ReviewQueue,
    _NodeEntry,
    _relation_proposal_to_json,
    entry_digest,
)
from okto_neuron.consolidate.review_queue_migration import (
    ReviewQueueMigrationError,
    migrate,
)
from okto_neuron.consolidate.review_queue_sqlite import (
    ReviewQueueMigrationRequired,
    SqliteQueueStore,
)
from okto_neuron.predicates.admission import PredicateAdmissionDecision
from okto_neuron.store import InMemoryStore


def _cand(title: str = "c", content: str = "x", type_: str = "Concept") -> NodeCandidate:
    return NodeCandidate(type=type_, title=title, content=content)


def _relation_candidate(*, literal: bool = False) -> EdgeCandidate:
    return EdgeCandidate(
        type="raw_relation",
        src_ref="subject",
        dst_ref="" if literal else "object",
        dst_literal="ring-bearer" if literal else None,
        block_id="block-1",
        byte_start=12,
        byte_end=40,
        content_hash="sha256:source",
        confidence=0.72,
        model_id="test-model",
        prompt_hash="sha256:prompt",
    )


def _pinned_relation_proposal(
    gate_reason: str,
    *,
    literal: bool = False,
    admission_reason: str | None = None,
) -> PinnedRelationProposal:
    type_status = "conflict" if gate_reason == "queue_type_conflict" else "accepted"
    d6_queue_reasons = {
        "queue_unregistered",
        "queue_mapping_conflict",
        "queue_verbose_label",
    }
    predicate_queued = gate_reason == "queue_predicate" or admission_reason in d6_queue_reasons
    predicate_status = "queued" if predicate_queued else "canonical"
    if admission_reason is None:
        admission_reason = (
            "queue_unregistered"
            if predicate_status == "queued"
            else ("literal" if literal else "canonical")
        )
    admission_state = "queued" if predicate_status == "queued" else "canonical"
    gate_input = RelationGateInput(
        subject=EndpointDecision(
            entity_id="subject",
            type_status=type_status,
            primitive_type=None if type_status == "conflict" else "Agent",
            live=True,
        ),
        predicate=PinnedPredicateAdmission(
            predicate="holds_role",
            status=predicate_status,
        ),
        object=(
            LiteralObject(value="ring-bearer")
            if literal
            else TopologyObject(
                EndpointDecision(
                    entity_id="object",
                    type_status="accepted",
                    primitive_type="Concept",
                    live=True,
                )
            )
        ),
        grounding=SourceGrounding(
            block_id="block-1",
            subject_supported=gate_reason != "queue_grounding",
            predicate_supported=True,
            object_supported=True,
            direction_supported=True,
        ),
    )
    return PinnedRelationProposal(
        admission=PredicateAdmissionDecision(
            reason=admission_reason,
            state=admission_state,
            raw_predicate="raw_relation",
            predicate="holds_role",
            subject_id="subject",
            object_id=None if literal else "object",
            object_literal="ring-bearer" if literal else None,
            swapped=False,
        ),
        gate_input=gate_input,
        predicate_definition="The subject holds the object role.",
        predicate_direction="subject_to_object",
        inverse_direction_required=False,
        useful=True,
    )


def test_enqueue_and_list(tmp_path) -> None:
    queue = ReviewQueue(tmp_path / ".marginalia", InMemoryStore())
    cand = _cand()
    item = queue.enqueue(cand, "low_confidence")

    assert item.candidate_id == cand.candidate_id
    assert item.reason == "low_confidence"
    items = queue.list()
    assert len(items) == 1
    assert items[0].candidate_id == cand.candidate_id


def test_node_review_read_model_preserves_source_anchor(tmp_path) -> None:
    candidate = NodeCandidate(
        type="Concept",
        title="anchored",
        facets={
            "source_path": "/vault/source.md",
            "block_id": "block-1",
            "byte_start": 12,
            "byte_end": 40,
            "content_hash": "a" * 64,
        },
    )
    queue = ReviewQueue(tmp_path / ".marginalia", InMemoryStore())

    item = queue.enqueue(candidate, "low_confidence")

    assert item.kind == "node"
    assert item.source_path == "/vault/source.md"
    assert item.block_id == "block-1"
    assert item.byte_start == 12
    assert item.byte_end == 40
    assert item.content_hash == "a" * 64


def test_enqueue_upserts_by_candidate_id(tmp_path) -> None:
    queue = ReviewQueue(tmp_path / ".marginalia", InMemoryStore())
    cand = _cand()
    queue.enqueue(cand, "low_confidence")
    queue.enqueue(cand, "contradiction")

    assert len(queue) == 1
    assert queue.list()[0].reason == "contradiction"


def test_persists_across_reopen(tmp_path) -> None:
    store = InMemoryStore()
    qdir = tmp_path / ".marginalia"
    cand = _cand(title="durable")
    corr = Correlation(kind="similar", target_id="n1", score=0.6)

    ReviewQueue(qdir, store).enqueue(cand, "low_confidence", (corr,))

    reopened = ReviewQueue(qdir, InMemoryStore())
    items = reopened.list()
    assert len(items) == 1
    assert items[0].title == "durable"
    assert items[0].correlations[0].target_id == "n1"


def _v1_vault(tmp_path, *, version: int = 1):
    root = tmp_path / "vault"
    (root / ".marginalia").mkdir(parents=True)
    (root / "okto-neuron.yaml").write_text(
        f"marginalia_yaml_version: {version}\nvault_id: t\n", encoding="utf-8"
    )
    return root, root / ".marginalia"


def test_legacy_untagged_node_row_migrates_and_keeps_its_digest(tmp_path) -> None:
    root, qdir = _v1_vault(tmp_path)
    candidate = _cand(title="legacy")
    (qdir / "review_queue.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "candidate": candidate.model_dump(mode="json"),
                        "reason": "low_confidence",
                        "correlations": [],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    json_era_digest = entry_digest(
        _NodeEntry(candidate=candidate, reason="low_confidence", correlations=()).to_json()
    )

    with pytest.raises(ReviewQueueMigrationRequired):
        ReviewQueue(qdir, InMemoryStore())
    migrate(root)
    queue = ReviewQueue(qdir, InMemoryStore())

    assert queue.list()[0].candidate_id == candidate.candidate_id
    assert queue.candidates() == [candidate]
    first = queue.resolution_scope(candidate.candidate_id)
    assert first["entry_sha256"] == json_era_digest
    assert first["queue_file"] == "review_queue.json"
    assert ReviewQueue(qdir, InMemoryStore()).resolution_scope(candidate.candidate_id) == first

    changed = candidate.model_copy(update={"facets": {"predicate": "second"}})
    assert changed.candidate_id == candidate.candidate_id
    queue.enqueue(changed, "low_confidence")

    second = queue.resolution_scope(candidate.candidate_id)
    assert first["candidate_id"] == second["candidate_id"]
    assert first["entry_sha256"] != second["entry_sha256"]


@pytest.mark.parametrize(
    ("reason", "gate_reason"),
    [
        ("queue_type_conflict", "queue_type_conflict"),
        ("queue_grounding", "queue_grounding"),
        ("queue_predicate", "queue_predicate"),
        ("queue_unregistered", "queue_predicate"),
        ("queue_mapping_conflict", "queue_predicate"),
        ("queue_direction_conflict", "queue_predicate"),
        ("queue_verbose_label", "queue_predicate"),
    ],
)
def test_relation_roundtrip_preserves_exact_reason_candidate_and_pinned_proposal(
    tmp_path,
    reason: str,
    gate_reason: str,
) -> None:
    qdir = tmp_path / ".marginalia"
    candidate = _relation_candidate()
    proposal = _pinned_relation_proposal(
        gate_reason,
        admission_reason=(
            reason
            if reason
            in {
                "queue_unregistered",
                "queue_mapping_conflict",
                "queue_direction_conflict",
                "queue_verbose_label",
            }
            else None
        ),
    )

    queued = ReviewQueue(qdir, InMemoryStore()).enqueue_relation(
        candidate,
        reason,
        proposal,
    )
    reopened = ReviewQueue(qdir, InMemoryStore())

    assert reopened.list() == []
    assert reopened.candidates() == []
    assert reopened.list_relations() == [queued]
    assert reopened.read(queued.candidate_id) == RelationReviewItem(
        candidate_id=queued.candidate_id,
        reason=reason,
        candidate=candidate,
        pinned_proposal=proposal,
    )


def test_literal_relation_roundtrip_preserves_literal_semantics(tmp_path) -> None:
    qdir = tmp_path / ".marginalia"
    candidate = _relation_candidate(literal=True)
    proposal = _pinned_relation_proposal("queue_grounding", literal=True)

    queued = ReviewQueue(qdir, InMemoryStore()).enqueue_relation(
        candidate,
        "queue_grounding",
        proposal,
    )
    reopened = ReviewQueue(qdir, InMemoryStore()).read(queued.candidate_id)

    assert isinstance(reopened, RelationReviewItem)
    assert reopened.candidate.dst_literal == "ring-bearer"
    assert isinstance(reopened.pinned_proposal.gate_input.object, LiteralObject)
    assert reopened.pinned_proposal.gate_input.object.value == "ring-bearer"


def test_type_conflict_precedence_retains_nested_d6_queue_reason(tmp_path) -> None:
    qdir = tmp_path / ".marginalia"
    proposal = _pinned_relation_proposal(
        "queue_type_conflict",
        admission_reason="queue_unregistered",
    )

    queued = ReviewQueue(qdir, InMemoryStore()).enqueue_relation(
        _relation_candidate(),
        "queue_type_conflict",
        proposal,
    )
    reopened = ReviewQueue(qdir, InMemoryStore()).read(queued.candidate_id)

    assert isinstance(reopened, RelationReviewItem)
    assert reopened.reason == "queue_type_conflict"
    assert reopened.pinned_proposal.admission_reason == "queue_unregistered"
    assert reopened.pinned_proposal.admission_state == "queued"


def test_relation_roundtrip_preserves_d6_and_curator_semantics(tmp_path) -> None:
    qdir = tmp_path / ".marginalia"
    candidate = _relation_candidate()
    proposal = PinnedRelationProposal(
        admission=PredicateAdmissionDecision(
            reason="confirmed_inverse",
            state="canonical",
            raw_predicate="raw_relation",
            predicate="is_held_by",
            subject_id="object",
            object_id="subject",
            object_literal=None,
            swapped=True,
        ),
        gate_input=RelationGateInput(
            subject=EndpointDecision("object", "accepted", "Concept", True),
            predicate=PinnedPredicateAdmission("is_held_by", "canonical"),
            object=TopologyObject(EndpointDecision("subject", "accepted", "Agent", True)),
            grounding=SourceGrounding(
                block_id="block-1",
                subject_supported=False,
                predicate_supported=True,
                object_supported=True,
                direction_supported=True,
            ),
        ),
        predicate_definition="The subject is held by the object.",
        predicate_direction="subject_to_object",
        inverse_direction_required=True,
        useful=False,
    )

    queued = ReviewQueue(qdir, InMemoryStore()).enqueue_relation(
        candidate,
        "queue_grounding",
        proposal,
    )
    reopened = ReviewQueue(qdir, InMemoryStore()).read(queued.candidate_id)

    assert isinstance(reopened, RelationReviewItem)
    assert reopened.pinned_proposal.raw_predicate == "raw_relation"
    assert reopened.pinned_proposal.admitted_predicate == "is_held_by"
    assert reopened.pinned_proposal.admission_reason == "confirmed_inverse"
    assert reopened.pinned_proposal.admission_state == "canonical"
    assert reopened.pinned_proposal.swapped is True
    assert reopened.pinned_proposal.predicate_definition.endswith("object.")
    assert reopened.pinned_proposal.predicate_direction == "subject_to_object"
    assert reopened.pinned_proposal.inverse_direction_required is True
    assert reopened.pinned_proposal.useful is False


def test_relation_acknowledge_is_explicit_and_writes_no_graph(tmp_path) -> None:
    store = InMemoryStore()
    qdir = tmp_path / ".marginalia"
    queue = ReviewQueue(qdir, store)
    queued = queue.enqueue_relation(
        _relation_candidate(),
        "queue_grounding",
        _pinned_relation_proposal("queue_grounding"),
    )

    assert list(store.list_edges()) == []
    assert queue.list_relations() == [queued]
    queue.acknowledge(queued.candidate_id)
    assert ReviewQueue(qdir, store).list_relations() == []
    assert list(store.list_edges()) == []


def test_relation_enqueue_rejects_reason_that_disagrees_with_pinned_proposal(tmp_path) -> None:
    queue = ReviewQueue(tmp_path / ".marginalia", InMemoryStore())

    with pytest.raises(ValueError, match="does not match"):
        queue.enqueue_relation(
            _relation_candidate(),
            "queue_unregistered",
            _pinned_relation_proposal("queue_grounding"),
        )

    assert len(queue) == 0
    assert not queue.path.exists()


def test_relation_enqueue_rejects_mismatched_source_block(tmp_path) -> None:
    queue = ReviewQueue(tmp_path / ".marginalia", InMemoryStore())
    candidate = _relation_candidate().model_copy(update={"block_id": "other-block"})

    with pytest.raises(ValueError, match="block ids differ"):
        queue.enqueue_relation(
            candidate,
            "queue_grounding",
            _pinned_relation_proposal("queue_grounding"),
        )


def test_relation_enqueue_rejects_literal_with_topology_dst_ref(tmp_path) -> None:
    queue = ReviewQueue(tmp_path / ".marginalia", InMemoryStore())
    candidate = _relation_candidate(literal=True).model_copy(update={"dst_ref": "ambiguous-object"})

    with pytest.raises(ValueError, match="empty dst_ref"):
        queue.enqueue_relation(
            candidate,
            "queue_grounding",
            _pinned_relation_proposal("queue_grounding", literal=True),
        )


@pytest.mark.parametrize("corruption", ["unknown_kind", "unknown_field", "duplicate"])
def test_relation_rows_fail_closed_on_corrupt_framing(tmp_path, corruption: str) -> None:
    root, qdir = _v1_vault(tmp_path)
    record = {
        "kind": "relation",
        "candidate": _relation_candidate().model_dump(mode="json"),
        "reason": "queue_grounding",
        "pinned_proposal": _relation_proposal_to_json(_pinned_relation_proposal("queue_grounding")),
    }
    entries = [record]
    if corruption == "unknown_kind":
        entries = [{**record, "kind": "edge"}]
    elif corruption == "unknown_field":
        entries = [{**record, "untrusted": True}]
    else:
        entries = [record, dict(record)]
    source = qdir / "review_queue.json"
    source.write_text(json.dumps({"entries": entries}), encoding="utf-8")
    before = source.read_bytes()

    with pytest.raises(ReviewQueueMigrationError):
        migrate(root)

    # refused loudly: the source is intact and nothing was written
    assert source.read_bytes() == before
    assert not (qdir / "review_queue.sqlite").exists()
    assert vault_yaml_version(root) == 1


def test_relation_rows_fail_closed_on_corrupt_semantic_type(tmp_path) -> None:
    qdir = tmp_path / ".marginalia"
    queue = ReviewQueue(qdir, InMemoryStore())
    queue.enqueue_relation(
        _relation_candidate(),
        "queue_grounding",
        _pinned_relation_proposal("queue_grounding"),
    )
    (row,) = SqliteQueueStore(qdir).rows()
    payload = json.loads(row.payload)
    payload["pinned_proposal"]["useful"] = "yes"
    with sqlite3.connect(queue.path) as connection:
        connection.execute(
            "UPDATE entries SET payload = ? WHERE seq = ?", (json.dumps(payload), row.seq)
        )

    with pytest.raises(ValueError, match="useful must be boolean"):
        ReviewQueue(qdir, InMemoryStore()).list_relations()


def test_claim_facets_and_embedding_survive_reopen(tmp_path) -> None:
    """The persisted queue preserves the complete candidate for a sealed plan."""
    qdir = tmp_path / ".marginalia"
    claim = NodeCandidate(
        type="Claim",
        title="hot water is harmful",
        content="drinking hot water causes harm",
        facets={"subject": "hot water", "predicate": "causes", "object": "harm"},
        embedding=tuple(0.01 * i for i in range(384)),
    )

    ReviewQueue(qdir, InMemoryStore()).enqueue(claim, "contradiction")

    # Reopen with a fresh store and inspect the candidate without writing it.
    store = InMemoryStore()
    reopened = ReviewQueue(qdir, store)
    restored = reopened.candidates()[0]
    assert restored.facets == {
        "subject": "hot water",
        "predicate": "causes",
        "object": "harm",
    }
    assert restored.embedding is not None
    assert len(restored.embedding) == 384
    assert abs(restored.embedding[1] - 0.01) < 1e-9
    assert store.get_node(claim.candidate_id) is None


def test_failed_write_rolls_back_and_keeps_the_previous_queue(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qdir = tmp_path / ".marginalia"
    queue = ReviewQueue(qdir, InMemoryStore())
    first = _cand(title="first")
    queue.enqueue(first, "low_confidence")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(review_queue_sqlite_module, "pack_embedding", fail)
    with pytest.raises(OSError, match="disk full"):
        queue.enqueue(
            NodeCandidate(type="Concept", title="second", embedding=(0.5, 0.25)),
            "low_confidence",
        )
    monkeypatch.undo()

    assert [item.candidate_id for item in ReviewQueue(qdir, InMemoryStore()).list()] == [
        first.candidate_id
    ]


def test_sqlite_pragmas_are_wal_and_full_sync(tmp_path) -> None:
    qdir = tmp_path / ".marginalia"
    ReviewQueue(qdir, InMemoryStore()).enqueue(_cand(), "low_confidence")
    connection = review_queue_sqlite_module.connect(qdir / "review_queue.sqlite", create=False)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    finally:
        connection.close()


def test_concurrent_enqueue_from_threads_loses_nothing(tmp_path) -> None:
    import threading

    qdir = tmp_path / ".marginalia"
    errors: list[BaseException] = []

    def work(start: int) -> None:
        try:
            queue = ReviewQueue(qdir, InMemoryStore())
            for index in range(start, start + 25):
                queue.enqueue(_cand(title=f"t{index}"), "low_confidence")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n * 25,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(ReviewQueue(qdir, InMemoryStore())) == 100


def test_acknowledge_unknown_id_raises(tmp_path) -> None:
    queue = ReviewQueue(tmp_path / ".marginalia", InMemoryStore())
    with pytest.raises(ReviewItemNotFoundError):
        queue.acknowledge("nope")
