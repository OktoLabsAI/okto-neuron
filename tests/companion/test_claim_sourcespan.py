"""Model-free test for ADR 0003 Road B brick R1: a minted Claim carries its
provenance as a SourceSpan derived from the SAME anchor data already used for
block_id/byte fields. Drives the sealed Claim planner/applier with an
InMemoryStore and a fixed-vector embedder — no LLM, no provider, no model load."""

from __future__ import annotations

from pathlib import Path

from okto_neuron.companion import (
    _LLMNodes,
    _apply_sealed_semantic_plan,
    _plan_relationship_claims,
)
from okto_neuron.consolidate._candidates import EdgeCandidate
from okto_neuron.consolidate.ledger import CandidateLedger, edge_candidate_id
from okto_neuron.consolidate.review_queue import ReviewQueue
from okto_neuron.core.schema import Node, Provenance
from okto_neuron.predicates import PredicateRegistry
from okto_neuron.schema.support import SourceSpan
from okto_neuron.store.memory import InMemoryStore

_HASH = "a" * 64
_HASH_2 = "d" * 64
_BLOCK_ID = "b" * 64
_BLOCK_ID_2 = "c" * 64
_BYTE_START = 12
_BYTE_END = 48


class _FixedEmbedder:
    """A deterministic, model-free embedder (no network, no model load)."""

    dim = 3

    def embed(self, text: str) -> list[float]:  # noqa: ARG002
        return [0.1, 0.2, 0.3]


def _llm() -> _LLMNodes:
    return _LLMNodes(
        activity_id="act-1",
        agent_id="agent-1",
        model_id="model-x",
        prompt_hash="prompt-1",
    )


def _seed_endpoints(store: InMemoryStore) -> None:
    # Relationship endpoints plus the extractor Activity/Agent the provenance
    # edges link the minted Claim to (ids must match the _LLMNodes fixture).
    for ref, typ in (
        ("subj", "Concept"),
        ("obj", "Concept"),
        ("act-1", "Activity"),
        ("agent-1", "Agent"),
    ):
        store.add_node(
            Node(
                id=ref,
                type=typ,
                title=ref,
                content=ref,
                facets={},
                provenance=Provenance(source="test", rule_id="t"),
            ),
        )


def _block(block_id: str, abs_source: str) -> Node:
    return Node(
        id=block_id,
        type="Block",
        title="block",
        content="some block text",
        facets={
            "source_path": abs_source,
            "byte_start": _BYTE_START,
            "byte_end": _BYTE_END,
            "content_hash": _HASH,
        },
        provenance=Provenance(source="ingest", rule_id="deterministic-v1"),
    )


def _edge(block_id: str) -> EdgeCandidate:
    return EdgeCandidate(
        type="relates_to",
        src_ref="subj",
        dst_ref="obj",
        block_id=block_id,
        byte_start=_BYTE_START,
        byte_end=_BYTE_END,
        content_hash=_HASH,
    )


def _edge_with_hash(block_id: str, content_hash: str) -> EdgeCandidate:
    return _edge(block_id).model_copy(update={"content_hash": content_hash})


def _pinned_relation_traces(edges: list[EdgeCandidate]) -> dict[str, dict[str, object]]:
    traces: dict[str, dict[str, object]] = {}
    for edge in edges:
        candidate_id = edge_candidate_id(edge.model_dump(mode="json"))
        object_id = edge.dst_ref or None
        object_literal = edge.dst_literal
        traces[candidate_id] = {
            "d6": {
                "reason": "canonical",
                "state": "canonical",
                "action": "commit_literal" if object_literal is not None else "commit_topology",
                "raw_predicate": edge.type,
                "predicate": edge.type,
                "subject_id": edge.src_ref,
                "object_id": object_id,
                "object_literal": object_literal,
                "swapped": False,
            },
            "d7": {
                "reason": "commit",
                "action": "commit",
                "relation_kind": "literal" if object_literal is not None else "topology",
                "subject_id": edge.src_ref,
                "predicate": edge.type,
                "object_id": object_id,
                "object_literal": object_literal,
                "liveness_support_ids": sorted(
                    {edge.src_ref, *({object_id} if object_id is not None else set())}
                ),
            },
        }
    return traces


def _plan_and_apply(
    tmp_path: Path,
    store: InMemoryStore,
    edges: list[EdgeCandidate],
    *,
    asserted_at: str | None = None,
) -> tuple[object, dict[str, object]]:
    claim_plan = _plan_relationship_claims(
        store,
        edges,
        planned_node_ids={"subj", "obj"},
        titles={"subj": "Subj", "obj": "Obj"},
        confidences={"subj": 0.9},
        pinned_predicates={
            edge_candidate_id(edge.model_dump(mode="json")): edge.type for edge in edges
        },
        pinned_relation_traces=_pinned_relation_traces(edges),
        llm=_llm(),
        embedder=_FixedEmbedder(),
        vault_root=tmp_path,
        min_claim_confidence=0.0,
        extra_mention_anchors={},
        asserted_at=asserted_at,
        embedding_settings=None,
    )
    ledger = CandidateLedger(tmp_path / ".marginalia")
    plan_id = ledger.record_commit_plan(
        "claim-source-span-test",
        operations=list(claim_plan.operations),
        context={"document_id": "doc-1"},
    )
    sealed = next(
        plan
        for plan in ledger.unreceipted_commit_plans(document_id="doc-1")
        if plan.plan_id == plan_id
    )
    result = _apply_sealed_semantic_plan(
        sealed,
        store=store,
        ledger=ledger,
        registry=PredicateRegistry(tmp_path),
        review_queue=ReviewQueue(tmp_path / ".marginalia", store),
    )
    return claim_plan, result


def test_mint_populates_sourcespan_from_anchor(tmp_path: Path) -> None:
    vault_root = tmp_path
    rel = "notes/topic.md"
    abs_source = str((vault_root / rel).resolve())

    store = InMemoryStore()
    _seed_endpoints(store)
    block_id = _BLOCK_ID
    store.add_node(_block(block_id, abs_source))

    _, result = _plan_and_apply(vault_root, store, [_edge(block_id)])
    assert result["claims_minted"] == 1

    claims = [n for n in store.list_nodes(type="Claim")]
    assert len(claims) == 1
    facets = claims[0].facets

    # block_id is still set and unchanged.
    assert facets["block_id"] == block_id

    # source_span is a SourceSpan equal to the candidate's provenance, with the
    # path in vault-relative form.
    span = SourceSpan.model_validate(facets["source_span"])
    assert span == SourceSpan(
        source_path=rel,
        byte_start=_BYTE_START,
        byte_end=_BYTE_END,
        content_hash=_HASH,
    )


def test_committed_relation_plan_and_receipts_preserve_d6_d7_trace(
    tmp_path: Path,
) -> None:
    store = InMemoryStore()
    _seed_endpoints(store)
    store.add_node(_block(_BLOCK_ID, str((tmp_path / "notes/topic.md").resolve())))
    edge = _edge(_BLOCK_ID)
    expected_trace = next(iter(_pinned_relation_traces([edge]).values()))

    claim_plan, _ = _plan_and_apply(tmp_path, store, [edge])

    semantic_operations = [
        operation
        for operation in claim_plan.operations
        if operation["operation"] in {"mint_claim", "create_topology_edge"}
    ]
    assert {operation["operation"] for operation in semantic_operations} == {
        "mint_claim",
        "create_topology_edge",
    }
    assert all(operation["reason"] == "commit" for operation in semantic_operations)
    assert all(operation["decision_trace"] == expected_trace for operation in semantic_operations)

    receipts = [
        record
        for record in CandidateLedger(tmp_path / ".marginalia").records()
        if record["kind"] == "operation_receipt"
        and record["operation"] in {"mint_claim", "create_topology_edge"}
    ]
    assert {receipt["operation"] for receipt in receipts} == {
        "mint_claim",
        "create_topology_edge",
    }
    assert all(receipt["result"]["reason"] == "commit" for receipt in receipts)
    assert all(receipt["result"]["decision_trace"] == expected_trace for receipt in receipts)


def test_mint_no_anchor_path_yields_none(tmp_path: Path) -> None:
    """When the block's source_path can't be relativized against the vault root
    (here: no vault_root passed), source_span is None but minting still works and
    block_id stays set — minting never starts failing for missing provenance."""
    store = InMemoryStore()
    _seed_endpoints(store)
    block_id = _BLOCK_ID_2
    # Absolute, outside-any-vault path; with vault_root=None it can't relativize.
    store.add_node(_block(block_id, "/somewhere/abs/topic.md"))

    _, result = _plan_and_apply(tmp_path, store, [_edge(block_id)])
    assert result["claims_minted"] == 1

    claims = [n for n in store.list_nodes(type="Claim")]
    assert len(claims) == 1
    facets = claims[0].facets
    assert facets["block_id"] == block_id
    assert facets["source_span"] is None


def test_remention_existing_claim_corroborates_and_records_store_merge(
    tmp_path: Path,
) -> None:
    vault_root = tmp_path
    abs_source = str((vault_root / "notes/topic.md").resolve())

    store = InMemoryStore()
    _seed_endpoints(store)
    store.add_node(_block(_BLOCK_ID, abs_source))
    store.add_node(_block(_BLOCK_ID_2, abs_source))

    first = _edge(_BLOCK_ID)
    second = _edge_with_hash(_BLOCK_ID_2, _HASH_2)
    _, first_result = _plan_and_apply(vault_root, store, [first])
    assert first_result["claims_minted"] == 1

    second_plan, second_result = _plan_and_apply(vault_root, store, [second])

    assert second_result["claims_minted"] == 0
    assert second_plan.relations_corroborated == 1
    claims = [n for n in store.list_nodes(type="Claim")]
    assert len(claims) == 1
    claim = claims[0]
    assert claim.facets["corroborations"] == 2
    derived_blocks = {
        edge.dst for edge in store.list_edges(src=claim.id, type="prov:wasDerivedFrom")
    }
    assert derived_blocks == {_BLOCK_ID, _BLOCK_ID_2}

    second_id = edge_candidate_id(second.model_dump(mode="json"))
    assert second_plan.edge_results[second_id] == {
        "state": "merged",
        "reason": "relationship claim already exists",
        "claim_id": claim.id,
    }
