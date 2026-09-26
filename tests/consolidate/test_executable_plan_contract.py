"""Focused ADR 0039/0040 executable-plan regression contract.

These tests stay model-free.  They pin the durable boundary between a sealed
semantic plan, its per-operation receipts, and apply-resume; they deliberately
avoid testing curation policy that belongs upstream of the sealed plan.
"""

from __future__ import annotations

from collections.abc import Iterator
import hashlib
import json
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.companion import Companion, _source_binding
from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.consolidate.ledger import (
    CandidateLedger,
    CommitPlanSnapshot,
    edge_candidate_id,
)
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection
from okto_neuron.extract import ExtractionResult
from okto_neuron.llm import StubLLM


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _single_operation_plan(ledger: CandidateLedger) -> CommitPlanSnapshot:
    candidate = EdgeCandidate(type="mentions", src_ref="node-1", dst_ref="node-2")
    candidate_payload = candidate.model_dump(mode="json")
    plan_id = ledger.record_commit_plan(
        "run-1",
        operations=[
            {
                "operation": "dead_letter",
                "candidate_kind": "edge",
                "candidate_id": edge_candidate_id(candidate_payload),
                "candidate": candidate_payload,
                "reason": "reject_structural_noise",
            }
        ],
        context={"document_id": "doc-1"},
    )
    return next(
        plan
        for plan in ledger.unreceipted_commit_plans(document_id="doc-1")
        if plan.plan_id == plan_id
    )


def _record_valid_receipt(
    ledger: CandidateLedger,
    plan: CommitPlanSnapshot,
) -> None:
    operation = plan.operations[0]
    ledger.record_operation_receipt(
        plan.run_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        operation_id=str(operation["operation_id"]),
        operation=str(operation["operation"]),
        status="dead_lettered",
        result={"candidate_id": operation["candidate_id"]},
    )


def _supersede_operation() -> dict[str, object]:
    return {
        "operation": "supersede",
        "old_claim_id": "claim-old",
        "new_claim_id": "claim-new",
        "expected_edge_id": "edge-new-supersedes-old",
        "edge": {
            "id": "edge-new-supersedes-old",
            "type": "supersedes",
            "src": "claim-new",
            "dst": "claim-old",
        },
        "reason": "corrected_fact",
    }


def _detachment_annotation_operation() -> dict[str, object]:
    return {
        "operation": "append_detachment_annotation",
        "annotation_id": "annotation-claim-old",
        "artifact": ".marginalia/detached/source.jsonl",
        "record": {
            "claim_id": "claim-old",
            "valid_as_of": "2026-07-17",
        },
        "reason": "source_line_removed",
    }


def test_reconciliation_operations_are_strict_sealed_plan_intents(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    plan_id = ledger.record_commit_plan(
        "run-1",
        operations=[_supersede_operation(), _detachment_annotation_operation()],
        context={"document_id": "doc-1"},
    )

    plan = next(plan for plan in ledger.unreceipted_commit_plans() if plan.plan_id == plan_id)

    assert [operation["operation"] for operation in plan.operations] == [
        "supersede",
        "append_detachment_annotation",
    ]
    assert all(
        str(operation["operation_id"]).startswith("sha256:") for operation in plan.operations
    )


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        ({**_supersede_operation(), "old_claim_id": ""}, "old_claim_id"),
        ({**_supersede_operation(), "new_claim_id": 7}, "new_claim_id"),
        (
            {**_supersede_operation(), "new_claim_id": "claim-old"},
            "distinct old and new claim ids",
        ),
        ({**_supersede_operation(), "expected_edge_id": ""}, "expected_edge_id"),
        ({**_supersede_operation(), "edge": []}, "edge must be an object"),
        (
            {
                **_supersede_operation(),
                "edge": {**_supersede_operation()["edge"], "id": "wrong"},
            },
            "edge id does not match",
        ),
        (
            {
                **_supersede_operation(),
                "edge": {**_supersede_operation()["edge"], "type": "related_to"},
            },
            "edge type",
        ),
        (
            {
                **_supersede_operation(),
                "edge": {**_supersede_operation()["edge"], "src": "claim-old"},
            },
            "edge source",
        ),
        (
            {
                **_supersede_operation(),
                "edge": {**_supersede_operation()["edge"], "dst": "claim-new"},
            },
            "edge destination",
        ),
        ({**_supersede_operation(), "reason": None}, "reason"),
        (
            {**_detachment_annotation_operation(), "annotation_id": ""},
            "annotation_id",
        ),
        ({**_detachment_annotation_operation(), "artifact": 42}, "artifact"),
        ({**_detachment_annotation_operation(), "record": []}, "record must be an object"),
        ({**_detachment_annotation_operation(), "reason": None}, "reason"),
    ],
)
def test_reconciliation_operation_schema_rejects_invalid_payloads(
    tmp_path: Path,
    operation: dict[str, object],
    message: str,
) -> None:
    ledger = CandidateLedger(tmp_path)

    with pytest.raises(ValueError, match=message):
        ledger.record_commit_plan("run-1", operations=[operation])

    assert not ledger.path.exists()


def test_persisted_reconciliation_operation_is_revalidated_on_read(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.record_commit_plan(
        "run-1",
        operations=[_supersede_operation()],
        context={"document_id": "doc-1"},
    )
    plan_record = ledger.records()[0]
    operation = plan_record["operations"][0]
    operation["edge"]["dst"] = "different-claim"
    raw_operation = {key: value for key, value in operation.items() if key != "operation_id"}
    operation["operation_id"] = _canonical_hash(raw_operation)
    plan_record["plan_hash"] = _canonical_hash(
        {
            "run_id": plan_record["run_id"],
            "plan_id": plan_record["plan_id"],
            "operations": plan_record["operations"],
            "context": plan_record["context"],
        }
    )
    ledger.path.write_text(json.dumps(plan_record) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="edge destination"):
        ledger.unreceipted_commit_plans()


def test_sealed_plan_has_stable_operation_ids_and_canonical_hash(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    candidate = EdgeCandidate(type="mentions", src_ref="node-1", dst_ref="node-2")
    candidate_payload = candidate.model_dump(mode="json")
    candidate_id = edge_candidate_id(candidate_payload)
    first_plan_id = ledger.record_commit_plan(
        "run-1",
        operations=[
            {
                "operation": "dead_letter",
                "candidate_kind": "edge",
                "candidate_id": candidate_id,
                "candidate": candidate_payload,
                "reason": "reject_structural_noise",
            },
            {
                "operation": "supersede_candidate",
                "candidate_kind": "edge",
                "candidate_id": "edge-2",
                "target_ref": "edge-1",
                "reason": "deduplicated",
            },
        ],
        context={"semantic_policy_fingerprint": "sha256:policy", "document_id": "doc-1"},
    )
    # Reordering object keys must not alter operation identity.  The second
    # sealed plan is distinct, however, so its plan hash must bind its own id.
    second_plan_id = ledger.record_commit_plan(
        "run-1",
        operations=[
            {
                "reason": "reject_structural_noise",
                "candidate": candidate_payload,
                "candidate_id": candidate_id,
                "candidate_kind": "edge",
                "operation": "dead_letter",
            },
            {
                "reason": "deduplicated",
                "target_ref": "edge-1",
                "candidate_id": "edge-2",
                "candidate_kind": "edge",
                "operation": "supersede_candidate",
            },
        ],
        context={"document_id": "doc-1", "semantic_policy_fingerprint": "sha256:policy"},
    )

    plans = {plan.plan_id: plan for plan in ledger.unreceipted_commit_plans()}
    first = plans[first_plan_id]
    second = plans[second_plan_id]

    assert [op["operation_id"] for op in first.operations] == [
        op["operation_id"] for op in second.operations
    ]
    for operation in first.operations:
        raw_operation = {key: value for key, value in operation.items() if key != "operation_id"}
        assert operation["operation_id"] == _canonical_hash(raw_operation)
    assert first.plan_hash == _canonical_hash(
        {
            "run_id": first.run_id,
            "plan_id": first.plan_id,
            "operations": list(first.operations),
            "context": first.context,
        }
    )
    assert second.plan_hash == _canonical_hash(
        {
            "run_id": second.run_id,
            "plan_id": second.plan_id,
            "operations": list(second.operations),
            "context": second.context,
        }
    )
    assert first.plan_hash != second.plan_hash

    # Reopening the ledger cannot change either durable identity.
    reopened = {plan.plan_id: plan for plan in CandidateLedger(tmp_path).unreceipted_commit_plans()}
    assert reopened[first_plan_id] == first
    assert reopened[second_plan_id] == second


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("run_id", "other-run"),
        ("plan_hash", "sha256:not-the-plan"),
        ("operation_id", "sha256:not-an-operation"),
        ("operation", "mint_claim"),
    ],
)
def test_invalid_or_unknown_operation_receipt_fails_closed(
    tmp_path: Path,
    field: str,
    invalid_value: str,
) -> None:
    ledger = CandidateLedger(tmp_path)
    plan = _single_operation_plan(ledger)
    operation = plan.operations[0]
    receipt = {
        "run_id": plan.run_id,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "operation_id": str(operation["operation_id"]),
        "operation": str(operation["operation"]),
        "status": "dead_lettered",
        "result": {},
    }
    receipt[field] = invalid_value

    ledger.record_operation_receipt(
        receipt.pop("run_id"),
        **receipt,
    )

    with pytest.raises(ValueError, match="invalid operation receipt"):
        ledger.unreceipted_commit_plans(document_id="doc-1")


def test_receipt_without_a_known_plan_fails_closed(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.record_operation_receipt(
        "run-1",
        plan_id="missing-plan",
        plan_hash="sha256:missing",
        operation_id="sha256:missing",
        operation="dead_letter",
        status="dead_lettered",
    )

    with pytest.raises(ValueError, match="operation receipt precedes or lacks plan"):
        ledger.unreceipted_commit_plans()


def test_duplicate_operation_receipt_fails_closed(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    plan = _single_operation_plan(ledger)
    _record_valid_receipt(ledger, plan)
    _record_valid_receipt(ledger, plan)

    with pytest.raises(ValueError, match="duplicate operation receipt"):
        ledger.unreceipted_commit_plans(document_id="doc-1")
    with pytest.raises(ValueError, match="duplicate operation receipt"):
        ledger.operation_receipts(plan)


def test_invalid_receipt_status_is_rejected_before_append(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    plan = _single_operation_plan(ledger)
    operation = plan.operations[0]

    with pytest.raises(ValueError, match="invalid operation receipt status"):
        ledger.record_operation_receipt(
            plan.run_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            operation_id=str(operation["operation_id"]),
            operation=str(operation["operation"]),
            status="success",
        )

    assert ledger.operation_receipts(plan) == {}


def test_invalid_persisted_receipt_status_fails_closed(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    plan = _single_operation_plan(ledger)
    operation = plan.operations[0]
    # Simulate a tampered/corrupt row that bypassed the typed receipt writer.
    ledger.append(
        "operation_receipt",
        run_id=plan.run_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        operation_id=operation["operation_id"],
        operation=operation["operation"],
        status="not-terminal",
        result={},
    )

    with pytest.raises(ValueError, match="invalid operation receipt status"):
        ledger.unreceipted_commit_plans(document_id="doc-1")
    with pytest.raises(ValueError, match="invalid operation receipt status"):
        ledger.operation_receipts(plan)


def test_commit_record_cannot_hide_missing_operation_receipts(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)
    plan = _single_operation_plan(ledger)

    # The aggregate close is only a projection of per-operation proof.  Even a
    # lying summary must not make an unreceipted operation disappear from the
    # apply-resume/verifier boundary.
    with pytest.raises(ValueError, match="missing operation receipts"):
        ledger.record_commit(
            plan.run_id,
            plan_id=plan.plan_id,
            result={"operation_receipts_complete": True, "operation_receipts": 1},
        )
    assert ledger.unreceipted_commit_plans(document_id="doc-1") == (plan,)


def test_commit_record_closes_plan_after_every_operation_is_receipted(
    tmp_path: Path,
) -> None:
    ledger = CandidateLedger(tmp_path)
    plan = _single_operation_plan(ledger)
    _record_valid_receipt(ledger, plan)
    ledger.record_commit(
        plan.run_id,
        plan_id=plan.plan_id,
        result={"operation_receipts_complete": True, "operation_receipts": 1},
    )

    assert ledger.unreceipted_commit_plans(document_id="doc-1") == ()


class _NeverCompleteProvider:
    model = "sealed-plan-resume-test"
    api_base = "http://127.0.0.1:8123/v1"

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, *args: object, **kwargs: object) -> str:
        self.calls += 1
        raise AssertionError("a sealed plan must not call the relation LLM")


class _NeverExtract:
    def extract(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("a sealed plan must not rerun extraction")


class _EmptyExtract:
    def extract(self, *args: object, **kwargs: object) -> ExtractionResult:
        return ExtractionResult()


def test_crash_after_seal_resumes_without_semantic_recomputation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault.init(tmp_path / "vault")
    source = Path(vault.path) / "note.md"
    source.write_text("# Note\n\nA durable semantic fact.\n", encoding="utf-8")
    document = vault.add(source)
    candidate = NodeCandidate(
        type="Concept",
        title="Durable plan",
        content="A plan sealed before semantic graph application.",
    )
    ledger = CandidateLedger(Path(vault.path) / ".marginalia")
    plan_id = ledger.record_commit_plan(
        "crashed-after-seal",
        operations=[
            {
                "operation": "create_node",
                "candidate_kind": "node",
                "candidate_id": candidate.candidate_id,
                "candidate": candidate.model_dump(mode="json"),
                "confidence": 0.98,
                "correlations": [],
                "reason": None,
            }
        ],
        context={
            "document_id": document.id,
            "source": str(source),
            "source_binding": _source_binding(vault.store, source, document.id),
            "blocks_total": 1,
            "nodes_extracted": 1,
            "edges_extracted": 0,
            "semantic_policy_fingerprint": "sha256:sealed-policy",
        },
    )

    calls = {"fingerprint": 0, "admission": 0, "relation_gate": 0}

    def unexpected_fingerprint(*args: object, **kwargs: object) -> object:
        calls["fingerprint"] += 1
        raise AssertionError("a sealed plan must not recompute policy fingerprints")

    def unexpected_admission(*args: object, **kwargs: object) -> object:
        calls["admission"] += 1
        raise AssertionError("a sealed plan must not rerun predicate admission")

    def unexpected_relation_gate(*args: object, **kwargs: object) -> object:
        calls["relation_gate"] += 1
        raise AssertionError("a sealed plan must not rerun the relation gate")

    import okto_neuron.consolidate.relation_gate as relation_gate_module
    import okto_neuron.consolidate.review_queue  # noqa: F401 - freeze import before patch
    import okto_neuron.predicates as predicates_module

    provider = _NeverCompleteProvider()
    companion = Companion(
        vault,
        provider=provider,
        extractor=_NeverExtract(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(companion, "_semantic_fingerprints", unexpected_fingerprint)
    monkeypatch.setattr(predicates_module, "admit_predicate", unexpected_admission)
    monkeypatch.setattr(relation_gate_module, "decide_relation", unexpected_relation_gate)

    try:
        result = companion.remember(source)

        assert result.document_id == document.id
        assert result.committed == 1
        assert provider.calls == 0
        assert calls == {"fingerprint": 0, "admission": 0, "relation_gate": 0}
        assert vault.store.get_node(candidate.candidate_id) is not None
        assert ledger.unreceipted_commit_plans(document_id=document.id) == ()
        records = ledger.records()
        plan_receipts = [
            record
            for record in records
            if record.get("kind") == "operation_receipt" and record.get("plan_id") == plan_id
        ]
        assert len(plan_receipts) == 1
        assert plan_receipts[0]["status"] == "applied"
    finally:
        vault.close()


def test_changed_source_abandons_untouched_plan_and_ingests_fresh_bytes(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "vault")
    source = Path(vault.path) / "note.md"
    source.write_text("# Before\n\nOriginal bytes.\n", encoding="utf-8")
    document = vault.add(source)
    stale_candidate = NodeCandidate(
        type="Concept",
        title="Stale semantic plan",
        content="This must never be applied after the source changes.",
    )
    ledger = CandidateLedger(Path(vault.path) / ".marginalia")
    plan_id = ledger.record_commit_plan(
        "stale-plan",
        operations=[
            {
                "operation": "create_node",
                "candidate_kind": "node",
                "candidate_id": stale_candidate.candidate_id,
                "candidate": stale_candidate.model_dump(mode="json"),
                "node": stale_candidate.to_node().model_dump(mode="json"),
                "confidence": 0.98,
                "correlations": [],
                "reason": None,
            }
        ],
        context={
            "document_id": document.id,
            "source": str(source),
            "source_binding": _source_binding(vault.store, source, document.id),
        },
    )
    source.write_text("# After\n\nCurrent bytes win.\n", encoding="utf-8")

    try:
        result = Companion(
            vault,
            provider=StubLLM(),
            extractor=_EmptyExtract(),  # type: ignore[arg-type]
        ).remember(source)

        assert result.document_id == document.id
        assert vault.store.get_node(stale_candidate.candidate_id) is None
        abandoned = [
            record
            for record in ledger.records()
            if record.get("kind") == "plan_abandoned" and record.get("plan_id") == plan_id
        ]
        assert len(abandoned) == 1
        assert abandoned[0]["reason"] == "source_generation_changed"
        assert ledger.unreceipted_commit_plans(document_id=document.id) == ()
    finally:
        vault.close()


# ── ADR 0039 D2: source removal is a planned, receipted, verified group ───────


def _source_removed_operation(
    *,
    source_id: str = "doc-removed",
    artifact_ids: tuple[str, ...] = ("claim-a", "claim-b"),
) -> dict[str, object]:
    return {
        "operation": "source_removed",
        "source_id": source_id,
        "derived_artifact_ids": sorted(artifact_ids),
        "reason": "source_deleted",
    }


def _seed_live_claim(vault: Vault, claim_id: str) -> None:
    from okto_neuron.core.schema import Node

    vault.store.add_node(Node(id=claim_id, type="Claim", title=claim_id))


def _retire_claim_operation(vault: Vault, claim_id: str) -> dict[str, object]:
    """The retirement half of the group: the state update that detaches a Claim.

    ``expected_before`` is the exact stored node, because the applier's
    compare-and-swap compares full serialized state.
    """

    before = vault.store.get_node(claim_id)
    assert before is not None
    after = before.model_copy(update={"facets": {"_detached": True, "valid_as_of": "2026-07-28"}})
    return {
        "operation": "update_node_state",
        "node_id": claim_id,
        "expected_before": before.model_dump(mode="json"),
        "node": after.model_dump(mode="json"),
        "reason": "claim_detached",
    }


def _seal_source_removed_plan(
    vault: Vault,
    operation: dict[str, object],
    *,
    retirements: tuple[dict[str, object], ...] = (),
) -> tuple[CandidateLedger, CommitPlanSnapshot]:
    ledger = CandidateLedger(Path(vault.path) / ".marginalia")
    plan_id = ledger.record_commit_plan(
        "run-source-removed",
        operations=[*retirements, operation],
        context={"document_id": "doc-removed"},
    )
    plan = next(plan for plan in ledger.unreceipted_commit_plans() if plan.plan_id == plan_id)
    return ledger, plan


def _apply(
    vault: Vault,
    ledger: CandidateLedger,
    plan: CommitPlanSnapshot,
    store=None,
    *,
    close_plan: bool = True,
):
    from okto_neuron.companion import _apply_sealed_semantic_plan
    from okto_neuron.consolidate.review_queue import ReviewQueue
    from okto_neuron.predicates import PredicateRegistry

    return _apply_sealed_semantic_plan(
        plan,
        store=vault.store if store is None else store,
        ledger=ledger,
        registry=PredicateRegistry(vault.path),
        review_queue=ReviewQueue(Path(vault.path) / ".marginalia", vault.store),
        close_plan=close_plan,
    )


class _WriteCountingStore:
    """Proxy that proves a replay re-verifies without rewriting."""

    def __init__(self, inner: object) -> None:
        self._inner = inner
        self.writes = 0

    def add_node(self, node: object) -> object:
        self.writes += 1
        return self._inner.add_node(node)  # type: ignore[attr-defined]

    def add_edge(self, edge: object) -> object:
        self.writes += 1
        return self._inner.add_edge(edge)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def _retired_claim(claim_id: str, *, facet: str) -> object:
    from okto_neuron.core.schema import Node

    return Node(
        id=claim_id,
        type="Claim",
        title=claim_id,
        facets={facet: True, "valid_as_of": "2026-07-28"},
    )


def test_source_removed_plan_applies_and_receipts_the_survivor_count(
    tmp_path: Path,
) -> None:
    from okto_neuron.core.schema import Node

    vault = Vault.init(tmp_path / "source-removed-applies")
    try:
        # Live at the start: the plan's own retirement operations must retire them.
        vault.store.add_node(Node(id="claim-a", type="Claim", title="claim-a"))
        vault.store.add_node(Node(id="claim-b", type="Claim", title="claim-b"))
        ledger, plan = _seal_source_removed_plan(
            vault,
            _source_removed_operation(),
            retirements=(
                _retire_claim_operation(vault, "claim-a"),
                _retire_claim_operation(vault, "claim-b"),
            ),
        )

        _apply(vault, ledger, plan)

        for claim_id in ("claim-a", "claim-b"):
            observed = vault.store.get_node(claim_id)
            assert observed is not None
            assert (observed.facets or {}).get("_detached") is True
        receipts = ledger.operation_receipts(plan)
        assert len(receipts) == 3
        receipt = next(
            value
            for key, value in receipts.items()
            if value["result"].get("source_id") == "doc-removed"
        )
        assert receipt["status"] == "applied"
        assert receipt["result"] == {
            "source_id": "doc-removed",
            "derived_artifact_ids": ["claim-a", "claim-b"],
            "survivor_count": 0,
            "reason": "source_deleted",
        }
    finally:
        vault.close()


def test_source_removed_plan_replays_without_rewriting(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "source-removed-replay")
    try:
        _seed_live_claim(vault, "claim-a")
        vault.store.add_node(_retired_claim("claim-b", facet="_detached"))
        ledger, plan = _seal_source_removed_plan(
            vault,
            _source_removed_operation(),
            retirements=(_retire_claim_operation(vault, "claim-a"),),
        )
        # Crash after the receipt but before the plan closed: the resume path
        # re-reads the same still-open plan.
        _apply(vault, ledger, plan, close_plan=False)
        first = dict(ledger.operation_receipts(plan))

        counting = _WriteCountingStore(vault.store)
        _apply(vault, ledger, plan, store=counting)

        assert counting.writes == 0
        assert ledger.operation_receipts(plan) == first
    finally:
        vault.close()


def test_source_removed_fails_closed_while_a_derived_artifact_survives(
    tmp_path: Path,
) -> None:
    from okto_neuron.core.schema import Node

    vault = Vault.init(tmp_path / "source-removed-survivor")
    try:
        _seed_live_claim(vault, "claim-a")
        vault.store.add_node(Node(id="claim-b", type="Claim", title="still live"))
        ledger, plan = _seal_source_removed_plan(
            vault,
            _source_removed_operation(),
            retirements=(_retire_claim_operation(vault, "claim-a"),),
        )

        with pytest.raises(ValueError, match="left live artifacts"):
            _apply(vault, ledger, plan)

        # The retirement half receipted; the closing verification refused, so the
        # plan stays open and can never report terminal success.
        receipted = {row["operation"] for row in ledger.operation_receipts(plan).values()}
        assert receipted == {"update_node_state"}
        assert ledger.unreceipted_commit_plans() == (plan,)
    finally:
        vault.close()


@pytest.mark.parametrize(
    ("operation", "message"),
    [
        (
            {
                "operation": "source_removed",
                "source_id": "",
                "derived_artifact_ids": ["claim-a"],
                "reason": "source_deleted",
            },
            "source_id must be non-empty text",
        ),
        (
            {
                "operation": "source_removed",
                "source_id": "doc-removed",
                "derived_artifact_ids": [],
                "reason": "source_deleted",
            },
            "must be a non-empty list",
        ),
        (
            {
                "operation": "source_removed",
                "source_id": "doc-removed",
                "derived_artifact_ids": ["claim-b", "claim-a"],
                "reason": "source_deleted",
            },
            "must be sorted and unique",
        ),
        (
            {
                "operation": "source_removed",
                "source_id": "doc-removed",
                "derived_artifact_ids": ["claim-a", "claim-a"],
                "reason": "source_deleted",
            },
            "must be sorted and unique",
        ),
        (
            {
                "operation": "source_removed",
                "source_id": "doc-removed",
                "derived_artifact_ids": ["claim-a"],
                "reason": "source_deleted",
                "survivor_count": 0,
            },
            "unknown=\\['survivor_count'\\]",
        ),
    ],
)
def test_source_removed_operation_schema_rejects_invalid_payloads(
    tmp_path: Path,
    operation: dict[str, object],
    message: str,
) -> None:
    ledger = CandidateLedger(tmp_path)

    with pytest.raises(ValueError, match=message):
        ledger.record_commit_plan("run-1", operations=[operation])

    assert not ledger.path.exists()


def test_torn_source_removed_operation_stays_fail_closed_on_read(
    tmp_path: Path,
) -> None:
    ledger = CandidateLedger(tmp_path)
    ledger.record_commit_plan(
        "run-1",
        operations=[_supersede_operation(), _source_removed_operation()],
        context={"document_id": "doc-removed"},
    )
    plan_record = ledger.records()[0]
    operation = plan_record["operations"][-1]
    operation["derived_artifact_ids"] = ["claim-b", "claim-a"]
    operation["operation_id"] = _canonical_hash(
        {key: value for key, value in operation.items() if key != "operation_id"}
    )
    plan_record["plan_hash"] = _canonical_hash(
        {
            "run_id": plan_record["run_id"],
            "plan_id": plan_record["plan_id"],
            "operations": plan_record["operations"],
            "context": plan_record["context"],
        }
    )
    ledger.path.write_text(json.dumps(plan_record) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="must be sorted and unique"):
        ledger.unreceipted_commit_plans()


def test_artifact_without_a_durable_receipt_is_reverified_not_rewritten(
    tmp_path: Path,
) -> None:
    """Crash matrix: the artifact landed but its receipt never became durable."""

    from okto_neuron.core.schema import Node

    vault = Vault.init(tmp_path / "receipt-lost-after-artifact")
    try:
        candidate = NodeCandidate(type="Concept", title="artifact landed first")
        pinned = candidate.to_node()
        ledger = CandidateLedger(Path(vault.path) / ".marginalia")
        plan_id = ledger.record_commit_plan(
            "run-crash-before-receipt",
            operations=[
                {
                    "operation": "create_node",
                    "candidate_kind": "node",
                    "candidate_id": candidate.candidate_id,
                    "candidate": candidate.model_dump(mode="json"),
                    "node": pinned.model_dump(mode="json"),
                    "confidence": 0.99,
                    "correlations": [],
                    "reason": None,
                }
            ],
            context={"document_id": "doc-crash"},
        )
        plan = next(plan for plan in ledger.unreceipted_commit_plans() if plan.plan_id == plan_id)
        # The write landed; the process died before the receipt was durable.
        vault.store.add_node(pinned)
        assert ledger.operation_receipts(plan) == {}

        counting = _WriteCountingStore(vault.store)
        _apply(vault, ledger, plan, store=counting)

        assert counting.writes == 0
        receipt = next(iter(ledger.operation_receipts(plan).values()))
        assert receipt["status"] == "already_present"
        observed = vault.store.get_node(candidate.candidate_id)
        assert observed is not None
        assert isinstance(observed, Node)
    finally:
        vault.close()


def test_bare_source_removed_plan_is_not_an_escape_hatch(tmp_path: Path) -> None:
    ledger = CandidateLedger(tmp_path)

    with pytest.raises(ValueError, match="requires the retirement operations"):
        ledger.record_commit_plan(
            "run-1",
            operations=[_source_removed_operation()],
            context={"document_id": "doc-removed"},
        )

    assert not ledger.path.exists()
