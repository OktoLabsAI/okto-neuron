from __future__ import annotations

import importlib.util
import json
import sys
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_neuron._internal.infra import INFRA_FACET
from okto_neuron.core.schema import Edge, Node
from okto_neuron.semantic_quality import (
    compare_semantic_snapshots,
    discovery_surface_key,
    evaluate_store,
    exact_surface_key,
)
from okto_neuron.store import InMemoryStore


SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ingest_quality_check.py"
_SNAPSHOT_FINGERPRINTS = {
    "config_fingerprint": f"sha256:{'1' * 64}",
    "extraction_fingerprint": f"sha256:{'2' * 64}",
    "semantic_policy_fingerprint": f"sha256:{'3' * 64}",
}
spec = importlib.util.spec_from_file_location("ingest_quality_check", SCRIPT)
assert spec is not None and spec.loader is not None
ingest_quality_check = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ingest_quality_check
spec.loader.exec_module(ingest_quality_check)


def _fresh_integrity(
    graph_generation: str = "generation-1",
    *,
    adjacency_complete: bool | None = True,
) -> dict[str, object]:
    return {
        "status": "verified",
        "graph_generation": graph_generation,
        "fresh_for_semantic_scan": True,
        "last_audit": {
            "status": "verified",
            "graph_generation": graph_generation,
            "nodes_complete": True,
            "edges_complete": True,
            "adjacency_complete": adjacency_complete,
            "manifest_complete": True,
        },
    }


def _recall_sample(**overrides: object) -> dict[str, object]:
    sample: dict[str, object] = {
        "schema_version": "recall_cost.v1",
        "completion_calls": 0,
        "generated_tokens": 0,
        "query_embedding_calls": 1,
        "query_embedding_latency_ms": 4.0,
        "query_embedding_provider": "LocalEmbedder",
        "query_embedding_model": "test-embedder",
        "query_embedding_execution": "local",
        "deterministic_retrieval_latency_ms": 6.0,
        "deterministic_projection_latency_ms": 2.0,
        "total_latency_ms": 10.0,
        "retrieved_results": 2,
        "retrieved_bytes": 120,
        "completion_free": True,
    }
    sample.update(overrides)
    return sample


def _verified_semantic_topology(
    *,
    total_edges: int = 3,
    semantic_edges: int = 2,
    edge_types: dict[str, int] | None = None,
) -> dict[str, object]:
    counts = edge_types or {"carries": 1, "member_of": 1}
    return {
        "semantic_quality": {
            "evidence": {
                "complete": True,
                "technical_integrity_verified": True,
                "topology_evidence_status": "measured",
            },
            "population": {"edges": total_edges},
            "layers": {
                "relation": {
                    "materialized_topology_edges": semantic_edges,
                    "materialized_topology_edge_types": {
                        "status": "measured",
                        "counts": counts,
                    },
                }
            },
        }
    }


def _semantic_snapshot_store(*, expanded: bool = False) -> InMemoryStore:
    store = InMemoryStore()
    store.add_node(Node(id="alice", type="Agent", title="Alice"))
    store.add_node(Node(id="atlas", type="Concept", title="Atlas"))
    store.add_node(
        Node(
            id="claim-leads",
            type="Claim",
            title="Alice leads Atlas",
            facets={"S_id": "alice", "P": "leads", "O_id": "atlas"},
        )
    )
    if expanded:
        store.add_node(Node(id="bob", type="Agent", title="Bob"))
        store.add_node(
            Node(
                id="claim-supports",
                type="Claim",
                title="Bob supports Atlas",
                facets={"S_id": "bob", "P": "supports", "O_id": "atlas"},
            )
        )
    return store


def _semantic_snapshot_equivalent_store(
    prefix: str,
    *,
    object_title: str = "Atlas",
) -> InMemoryStore:
    store = InMemoryStore()
    subject_id = f"{prefix}-alice"
    object_id = f"{prefix}-atlas"
    store.add_node(
        Node(
            id=subject_id,
            type="Agent",
            title="Alice",
            content=f"extractor-authored description {prefix}",
        )
    )
    store.add_node(
        Node(
            id=object_id,
            type="Concept",
            title=object_title,
            content=f"different extractor prose {prefix}",
        )
    )
    store.add_node(
        Node(
            id=f"{prefix}-claim",
            type="Claim",
            title=f"physical claim title {prefix}",
            facets={"S_id": subject_id, "P": "leads", "O_id": object_id},
        )
    )
    return store


def _args(**overrides):
    base = dict(
        min_nodes=3,
        min_edges=2,
        min_claims=1,
        min_knowledge_nodes=None,
        min_semantic_edges=None,
        min_commit_plans=None,
        min_commit_records=None,
        min_pending_nodes=None,
        min_pending_relations=None,
        min_extracted_node_mentions=None,
        min_extracted_relation_candidates=None,
        min_extracted_claim_candidates=None,
        max_queue_items=0,
        expect_title=["Frodo Baggins"],
        forbid_title=["Definition of Done"],
        expect_edge_type=["carries"],
        forbid_edge_type=["requires"],
        domain_profile=None,
        min_domain_profile_coverage=None,
        min_extracted_domain_profile_coverage=None,
        min_pending_domain_profile_coverage=None,
        forbid_structural_title_noise=False,
        max_isolated_knowledge_nodes=None,
        require_semantic_quality=False,
    )
    base.update(overrides)
    return Namespace(**base)


def test_check_report_passes_expected_structural_invariants() -> None:
    report = {
        "graph": {"truncated": False},
        "stats": {
            "total_nodes": 5,
            "total_edges": 3,
            "node_types": [{"type": "Claim", "count": 2}],
        },
        "queue": {"summary": {"queued": 0, "processing": 0}},
        "node_names": ["Frodo Baggins", "The One Ring"],
        "edge_types": ["carries", "member_of"],
        **_verified_semantic_topology(),
    }

    checks = ingest_quality_check.check_report(_args(), report)

    assert checks
    assert all(check.ok for check in checks)


def test_check_report_fails_for_missing_expected_and_forbidden_present() -> None:
    report = {
        "graph": {"truncated": False},
        "stats": {
            "total_nodes": 2,
            "total_edges": 1,
            "node_types": [{"type": "Claim", "count": 0}],
        },
        "queue": {"summary": {"queued": 1, "processing": 0}},
        "node_names": ["Definition of Done"],
        "edge_types": ["requires"],
        **_verified_semantic_topology(
            total_edges=1,
            semantic_edges=1,
            edge_types={"requires": 1},
        ),
    }

    checks = ingest_quality_check.check_report(_args(), report)
    failed = [check.detail for check in checks if not check.ok]

    assert any("total_nodes" in detail for detail in failed)
    assert any("Claim nodes" in detail for detail in failed)
    assert any("active queue items" in detail for detail in failed)
    assert any("expected title present: Frodo Baggins" in detail for detail in failed)
    assert any("forbidden title absent: Definition of Done" in detail for detail in failed)
    assert any("expected audited edge type present: carries" in detail for detail in failed)
    assert any("forbidden audited edge type absent: requires" in detail for detail in failed)


def test_check_report_fails_for_structural_title_noise() -> None:
    report = {
        "graph": {"truncated": False},
        "stats": {
            "total_nodes": 5,
            "total_edges": 3,
            "node_types": [{"type": "Claim", "count": 2}],
        },
        "queue": {"summary": {"queued": 0, "processing": 0}},
        "node_names": ["Frodo Baggins", "code-block 0", "paragraph 1", "Table 2"],
        "edge_types": ["carries", "member_of"],
        **_verified_semantic_topology(),
    }

    checks = ingest_quality_check.check_report(
        _args(forbid_structural_title_noise=True),
        report,
    )
    failed = [check.detail for check in checks if not check.ok]

    assert failed == ["structural title noise absent: code-block 0, paragraph 1, Table 2"]


def test_check_report_ignores_structural_source_anchor_titles() -> None:
    report = {
        "graph": {"truncated": False},
        "stats": {
            "total_nodes": 5,
            "total_edges": 3,
            "node_types": [{"type": "Claim", "count": 2}],
        },
        "queue": {"summary": {"queued": 0, "processing": 0}},
        "node_names": ["Frodo Baggins", "code-block 0", "paragraph 1"],
        "knowledge_node_names": ["Frodo Baggins", "The One Ring"],
        "edge_types": ["carries", "member_of"],
        **_verified_semantic_topology(),
    }

    checks = ingest_quality_check.check_report(
        _args(forbid_structural_title_noise=True),
        report,
    )

    assert all(check.ok for check in checks)


def test_isolated_knowledge_nodes_ignore_provenance_and_claim_nodes() -> None:
    nodes = [
        {"id": "topic", "name": "Search Quality", "type": "Concept"},
        {"id": "row", "name": "Query Expansion", "type": "Concept"},
        {"id": "orphan", "name": "Prompt Precision", "type": "Concept"},
        {"id": "doc", "name": "Source Doc", "type": "Document"},
        {"id": "claim", "name": "Claim 1", "type": "Claim"},
    ]
    edges = [
        {"type": "includes", "src": "topic", "dst": "row"},
        {"type": "rdf:subject", "src": "claim", "dst": "row"},
        {"type": "schema:mentions", "src": "doc", "dst": "orphan"},
        {"type": "prov:wasDerivedFrom", "src": "orphan", "dst": "doc"},
    ]

    isolated = ingest_quality_check._isolated_knowledge_nodes(nodes, edges)

    assert isolated == [{"id": "orphan", "name": "Prompt Precision", "type": "Concept"}]


def test_check_report_does_not_fallback_to_a_different_isolation_definition() -> None:
    report = {
        "graph": {"truncated": False},
        "stats": {
            "total_nodes": 5,
            "total_edges": 3,
            "node_types": [{"type": "Claim", "count": 2}],
        },
        "queue": {"summary": {"queued": 0, "processing": 0}},
        "node_names": ["Frodo Baggins", "The One Ring"],
        "edge_types": ["carries", "member_of"],
        "isolated_knowledge_nodes": [
            {"name": "Prompt Precision", "type": "Concept"},
            {"name": "Checklist Verification", "type": "Concept"},
        ],
        **_verified_semantic_topology(),
    }

    checks = ingest_quality_check.check_report(
        _args(max_isolated_knowledge_nodes=1),
        report,
    )
    failed = [check.detail for check in checks if not check.ok]

    assert failed == [
        "isolated knowledge nodes not measured: complete, technically verified "
        "semantic evidence is required"
    ]


def test_check_report_enforces_isolation_limit_from_complete_semantic_scan() -> None:
    report = {
        "graph": {"truncated": False},
        "stats": {"total_nodes": 5, "total_edges": 3, "node_types": []},
        "queue": {"summary": {"queued": 0, "processing": 0}},
        "semantic_quality": {
            "evidence": {"complete": True, "technical_integrity_verified": True},
            "layers": {
                "relation": {
                    "isolated_entities": {
                        "count": 2,
                        "samples": [
                            {"title": "Prompt Precision", "type": "Concept"},
                            {"title": "Checklist Verification", "type": "Concept"},
                        ],
                    }
                }
            },
        },
    }

    checks = ingest_quality_check.check_report(
        _args(
            min_nodes=None,
            min_edges=None,
            min_claims=None,
            expect_title=[],
            forbid_title=[],
            expect_edge_type=[],
            forbid_edge_type=[],
            max_isolated_knowledge_nodes=1,
        ),
        report,
    )

    assert [(check.ok, check.detail) for check in checks if not check.ok] == [
        (
            False,
            "isolated knowledge nodes 2 <= 1: Prompt Precision (Concept), "
            "Checklist Verification (Concept)",
        )
    ]


def test_check_report_does_not_pass_checks_derived_from_truncated_overview() -> None:
    report = {
        "stats": {"total_nodes": 100, "total_edges": 100, "node_types": []},
        "graph": {"truncated": True},
        "graph_profile": {"knowledge_node_count": 10, "semantic_edge_count": 10},
        "queue": {"summary": {"queued": 0, "processing": 0}},
        "knowledge_node_names": ["Frodo Baggins"],
        "edge_types": ["carries"],
    }

    checks = ingest_quality_check.check_report(
        _args(min_knowledge_nodes=1, expect_title=["Frodo Baggins"]),
        report,
    )

    details = [check.detail for check in checks]
    assert (
        False,
        "overview-derived checks not measured: /api/v1/graph completeness was not attested",
    ) in [(check.ok, check.detail) for check in checks]
    assert not any(detail.startswith("knowledge nodes ") for detail in details)
    assert not any(detail.startswith("expected title present: ") for detail in details)


def test_check_report_does_not_pass_edge_checks_without_verified_topology() -> None:
    report = {
        "graph": {"truncated": False},
        "stats": {"total_nodes": 10, "total_edges": 999, "node_types": []},
        "graph_profile": {"semantic_edge_count": 999},
        "queue": {"summary": {"queued": 0, "processing": 0}},
        "edge_types": ["carries"],
        "semantic_quality": {
            "evidence": {
                "complete": False,
                "technical_integrity_verified": False,
                "topology_evidence_status": "not_measured",
            }
        },
    }

    checks = ingest_quality_check.check_report(
        _args(
            min_nodes=None,
            min_edges=1,
            min_claims=None,
            min_semantic_edges=1,
            max_queue_items=None,
            expect_title=[],
            forbid_title=[],
            expect_edge_type=["carries"],
            forbid_edge_type=[],
        ),
        report,
    )

    assert [(check.ok, check.detail) for check in checks] == [
        (
            False,
            "edge-derived checks not measured: complete, technically verified "
            "semantic topology evidence is required",
        )
    ]


def test_check_report_uses_only_the_audited_semantic_snapshot_for_edge_gates() -> None:
    report = {
        "graph": {"truncated": False},
        "stats": {"total_nodes": 10, "total_edges": 999, "node_types": []},
        "graph_profile": {"semantic_edge_count": 999},
        "queue": {"summary": {"queued": 0, "processing": 0}},
        "edge_types": ["stale_overview_type"],
        **_verified_semantic_topology(
            total_edges=2,
            semantic_edges=1,
            edge_types={"audited_type": 1},
        ),
    }

    checks = ingest_quality_check.check_report(
        _args(
            min_nodes=None,
            min_edges=3,
            min_claims=None,
            min_semantic_edges=2,
            max_queue_items=None,
            expect_title=[],
            forbid_title=[],
            expect_edge_type=["audited_type"],
            forbid_edge_type=["stale_overview_type"],
        ),
        report,
    )

    assert [(check.ok, check.detail) for check in checks] == [
        (False, "audited edges 2 >= 3"),
        (False, "audited semantic edges 1 >= 2"),
        (True, "expected audited edge type present: audited_type"),
        (True, "forbidden audited edge type absent: stale_overview_type"),
    ]


def test_check_report_distinguishes_structural_graph_from_committed_semantic_kg() -> None:
    report = {
        "graph": {"truncated": False},
        "stats": {"total_nodes": 93, "total_edges": 4, "node_types": []},
        "graph_profile": {
            "knowledge_node_count": 1,
            "semantic_edge_count": 0,
        },
        "ledger_detail": {
            "counts": {
                "commit_plans": 0,
                "commit_records": 0,
            }
        },
        "queue": {"summary": {"queued": 3, "processing": 1}},
        "knowledge_node_names": ["heading: Attached file: The Fellowship of the Ring.pdf"],
        "edge_types": ["prov:wasDerivedFrom", "rdf:subject"],
    }

    checks = ingest_quality_check.check_report(
        _args(
            min_nodes=None,
            min_edges=None,
            min_claims=None,
            min_knowledge_nodes=2,
            min_semantic_edges=1,
            min_commit_plans=1,
            min_commit_records=1,
            max_queue_items=4,
            expect_title=[],
            forbid_title=[],
            expect_edge_type=[],
            forbid_edge_type=[],
        ),
        report,
    )

    assert [check.ok for check in checks] == [False, False, False, False, True]
    assert [check.detail for check in checks] == [
        "edge-derived checks not measured: complete, technically verified "
        "semantic topology evidence is required",
        "knowledge nodes 1 >= 2",
        "commit plans 0 >= 1",
        "commit records 0 >= 1",
        "active queue items 4 <= 4",
    ]


def test_domain_profile_summary_tracks_lotr_coverage_and_forbidden_titles() -> None:
    node_records = [
        {"name": "Frodo", "type": "Agent"},
        {"name": "Samwise Gamgee", "type": "Agent"},
        {"name": "Pippin Took", "type": "Agent"},
        {"name": "Rivendell", "type": "Place"},
        {"name": "The One Ring", "type": "Concept"},
        {"name": "J. R. R. Tolkien", "type": "Agent"},
        {"name": "Definition of Done", "type": "Concept"},
        {"name": "paragraph 1", "type": "Block"},
    ]

    summary = ingest_quality_check._domain_profile_summary("lotr", node_records)

    assert summary["coverage"]["present"] == 6
    assert summary["coverage"]["total"] == 21
    assert summary["groups"]["characters"]["present"] == 3
    assert summary["groups"]["places"]["present"] == 1
    assert summary["groups"]["artifacts"]["present"] == 1
    assert summary["groups"]["works_and_contributors"]["present"] == 1
    assert summary["forbidden_present"] == [{"title": "Definition of Done", "type": "Concept"}]


def test_check_report_enforces_domain_profile_forbidden_and_optional_coverage() -> None:
    report = {
        "graph": {"truncated": False},
        "stats": {
            "total_nodes": 5,
            "total_edges": 3,
            "node_types": [{"type": "Claim", "count": 1}],
        },
        "queue": {"summary": {"queued": 0, "processing": 0}},
        "node_names": ["Frodo", "Definition of Done"],
        "edge_types": ["carries"],
        "domain_profile": {
            "name": "lotr",
            "coverage": {"present": 1, "total": 21, "fraction": 1 / 21},
            "groups": {},
            "forbidden_present": [{"title": "Definition of Done", "type": "Concept"}],
        },
    }

    checks = ingest_quality_check.check_report(
        _args(
            min_nodes=None,
            min_edges=None,
            min_claims=None,
            expect_title=[],
            forbid_title=[],
            expect_edge_type=[],
            forbid_edge_type=[],
            min_domain_profile_coverage=0.5,
        ),
        report,
    )
    failed = [check.detail for check in checks if not check.ok]

    assert failed == [
        "domain forbidden titles absent: Definition of Done (Concept)",
        "domain profile coverage 1/21 >= 0.50",
    ]


def test_summarize_ingest_extraction_counts_retained_event_candidates(monkeypatch) -> None:
    queue = {
        "items": [
            {
                "id": "item-1",
                "name": "The Fellowship.md",
                "status": "processing",
                "stage": "extracting",
                "event_count": 4,
            }
        ]
    }

    def fake_detail(endpoint: str, vault: str, item_id: str, **kwargs) -> dict:
        assert vault == "LOTR"
        assert item_id == "item-1"
        return {
            "item": {
                "id": item_id,
                "name": "The Fellowship.md",
                "status": "processing",
                "stage": "extracting",
                "blocks_done": 2,
                "blocks_total": 91,
                "events": [
                    {"kind": "llm_request", "payload": {}},
                    {
                        "kind": "extraction_result",
                        "payload": {
                            "block": {"index": 1},
                            "nodes": [
                                {"type": "Agent", "title": "Frodo"},
                                {"type": "Agent", "title": "Samwise Gamgee"},
                                {"type": "Place", "title": "Shire"},
                            ],
                            "edges": [
                                {
                                    "type": "travels_with",
                                    "src_ref": "frodo",
                                    "dst_ref": "sam",
                                },
                                {
                                    "type": "age",
                                    "src_ref": "frodo",
                                    "dst_literal": "50",
                                },
                            ],
                        },
                    },
                ],
            }
        }

    monkeypatch.setattr(ingest_quality_check, "ingest_queue_item", fake_detail)

    summary = ingest_quality_check.summarize_ingest_extraction(
        "http://127.0.0.1:7777",
        "LOTR",
        queue,
        domain_profile="lotr",
    )

    aggregate = summary["aggregate"]
    assert aggregate["counts"] == {
        "node_mentions": 3,
        "relation_candidates": 1,
        "claim_candidates": 1,
        "unique_node_titles": 3,
    }
    assert aggregate["node_types"] == {"Agent": 2, "Place": 1}
    assert aggregate["relation_types"] == {"travels_with": 1}
    assert aggregate["claim_predicates"] == {"age": 1}
    assert aggregate["domain_profile"]["coverage"]["present"] == 3
    assert summary["items"][0]["retained_block_range"] == {"first": 1, "last": 1}


def test_check_report_enforces_extracted_candidate_thresholds() -> None:
    report = {
        "stats": {"total_nodes": 0, "total_edges": 0, "node_types": []},
        "queue": {"summary": {"queued": 1, "processing": 1}},
        "node_names": [],
        "edge_types": [],
        "ingest_extraction": {
            "aggregate": {
                "counts": {
                    "node_mentions": 10,
                    "relation_candidates": 5,
                    "claim_candidates": 2,
                },
                "domain_profile": {
                    "coverage": {"present": 2, "total": 21, "fraction": 2 / 21},
                },
            }
        },
    }

    checks = ingest_quality_check.check_report(
        _args(
            min_nodes=None,
            min_edges=None,
            min_claims=None,
            min_extracted_node_mentions=11,
            min_extracted_relation_candidates=5,
            min_extracted_claim_candidates=3,
            min_extracted_domain_profile_coverage=0.10,
            max_queue_items=2,
            expect_title=[],
            forbid_title=[],
            expect_edge_type=[],
            forbid_edge_type=[],
        ),
        report,
    )

    failed = [check.detail for check in checks if not check.ok]
    assert "extracted node mentions 10 >= 11" in failed
    assert "extracted claim candidates 2 >= 3" in failed
    assert "extracted domain profile coverage 2/21 >= 0.10" in failed
    assert all("extracted relation candidates 5 >= 5" != detail for detail in failed)


def test_cognitive_scorer_receives_report_on_stdin() -> None:
    report = {"status": "ok", "node_names": ["Frodo Baggins"]}

    result = ingest_quality_check.run_cognitive_scorer(
        "python -c \"import sys; print('Frodo' in sys.stdin.read())\"",
        report,
    )

    assert result["returncode"] == 0
    assert result["stdout"].strip() == "True"


def test_summarize_ledger_detail_counts_candidates_and_comparisons() -> None:
    detail = {
        "run": {"run_id": "run-1", "state": "started"},
        "candidates": [
            {
                "candidate_id": "node-1",
                "candidate_kind": "node",
                "state": "proposed",
                "payload": {"type": "Agent", "title": "Frodo"},
            },
            {
                "candidate_id": "node-2",
                "candidate_kind": "node",
                "state": "proposed",
                "type": "Place",
                "title": "Rivendell",
            },
            {
                "candidate_id": "edge-1",
                "candidate_kind": "edge",
                "state": "proposed",
                "payload": {"type": "located_in"},
            },
            {
                "candidate_id": "edge-2",
                "candidate_kind": "edge",
                "state": "proposed",
                "payload": {
                    "type": "located_in",
                    "derived": True,
                    "derivation_reason": "deterministic endpoint remap before relation review",
                },
            },
            {
                "candidate_id": "node-2",
                "candidate_kind": "node",
                "state": "queued",
                "type": "Place",
                "title": "Rivendell",
            },
        ],
        "comparisons": [
            {
                "candidate_id": "node-1",
                "ts": "2026-06-09T00:00:00+00:00",
                "method": "resolver",
                "verdict": "novel",
                "score": 0.9,
                "reason": "new entity",
            },
            {
                "candidate_id": "edge-1",
                "ts": "2026-06-09T00:00:01+00:00",
                "method": "curator",
                "verdict": "queue",
                "score": 0.95,
                "reason": "not grounded",
            },
        ],
        "commit_plans": [{"plan_id": "plan-1"}],
        "commit_records": [],
    }

    summary = ingest_quality_check.summarize_ledger_detail(detail, recent_limit=1)

    assert summary["run"] == {"run_id": "run-1", "state": "started"}
    assert summary["counts"] == {
        "candidates": 4,
        "candidate_rows": 5,
        "comparisons": 2,
        "commit_plans": 1,
        "commit_records": 0,
    }
    assert summary["candidate_kinds"] == {"edge": 2, "node": 2}
    assert summary["candidate_origins_by_kind"] == {
        "edge": {
            "deterministic endpoint remap before relation review": 1,
            "raw": 1,
        },
        "node": {"raw": 2},
    }
    assert summary["candidate_row_states_by_kind"] == {
        "edge": {"proposed": 2},
        "node": {"proposed": 2, "queued": 1},
    }
    assert summary["candidate_node_types"] == {"Agent": 1, "Place": 1}
    assert summary["comparison_methods"] == {"curator": 1, "resolver": 1}
    assert summary["comparison_verdicts"] == {"novel": 1, "queue": 1}
    assert summary["unique_compared_candidates_by_method"] == {
        "curator": 1,
        "resolver": 1,
    }
    assert summary["unique_compared_candidates_by_verdict"] == {
        "novel": 1,
        "queue": 1,
    }
    assert summary["compared_candidate_kinds_by_method"] == {
        "curator": {"edge": 1},
        "resolver": {"node": 1},
    }
    assert summary["progress"] == {
        "node_curator": {
            "done": 0,
            "total": 2,
            "remaining": 2,
            "fraction": 0.0,
        },
        "relation_curator": {
            "done": 0,
            "total": 2,
            "remaining": 2,
            "fraction": 0.0,
        },
    }
    assert summary["recent_comparisons"] == [
        {
            "candidate_id": "edge-1",
            "ts": "2026-06-09T00:00:01+00:00",
            "method": "curator",
            "verdict": "queue",
            "score": 0.95,
            "reason": "not grounded",
        }
    ]


def test_summarize_ledger_detail_progress_uses_active_post_dedup_totals() -> None:
    detail = {
        "run": {"run_id": "run-1", "state": "started"},
        "candidates": [
            {"candidate_id": "node-1", "candidate_kind": "node", "state": "proposed"},
            {"candidate_id": "node-2", "candidate_kind": "node", "state": "proposed"},
            {"candidate_id": "node-3", "candidate_kind": "node", "state": "proposed"},
            {"candidate_id": "edge-1", "candidate_kind": "edge", "state": "proposed"},
        ],
        "comparisons": [
            {
                "candidate_id": "",
                "method": "exact_batch",
                "verdict": "dedup_pass",
                "payload": {
                    "before": {"nodes": 3, "edges": 1},
                    "after": {"nodes": 2, "edges": 1},
                },
            },
            {
                "candidate_id": "node-1",
                "method": "curator",
                "verdict": "commit",
                "payload": {"candidate": {"type": "Agent", "title": "Frodo"}},
            },
            {
                "candidate_id": "node-3",
                "method": "curator",
                "verdict": "commit",
                "payload": {"audit_only": True, "llm_skipped": True},
            },
            {
                "candidate_id": "edge-1",
                "method": "relation_curator",
                "verdict": "commit",
                "payload": {
                    "type": "carries",
                    "proposed_terminal_action": "create_edge_or_claim",
                },
            },
        ],
        "commit_plans": [],
        "commit_records": [],
    }

    summary = ingest_quality_check.summarize_ledger_detail(detail)

    assert summary["candidate_kinds"] == {"edge": 1, "node": 3}
    assert summary["active_candidate_kinds"] == {"edge": 1, "node": 2}
    assert summary["progress"]["node_curator"] == {
        "done": 1,
        "total": 2,
        "remaining": 1,
        "fraction": 0.5,
    }
    assert summary["progress"]["relation_curator"] == {
        "done": 1,
        "total": 1,
        "remaining": 0,
        "fraction": 1.0,
    }
    assert summary["comparison_methods"]["curator"] == 2


def test_local_candidate_origins_reads_unsanitized_ledger(tmp_path: Path) -> None:
    ledger_dir = tmp_path / ".marginalia"
    ledger_dir.mkdir()
    ledger_path = ledger_dir / "candidate-ledger.jsonl"
    rows = [
        {
            "kind": "candidate",
            "run_id": "run-1",
            "candidate_id": "edge-1",
            "candidate_kind": "edge",
            "payload": {"type": "uses"},
        },
        {
            "kind": "candidate",
            "run_id": "run-1",
            "candidate_id": "edge-2",
            "candidate_kind": "edge",
            "payload": {
                "type": "uses",
                "derived": True,
                "derivation_reason": "relation curator canonicalized predicate",
            },
        },
        {
            "kind": "candidate",
            "run_id": "other-run",
            "candidate_id": "edge-3",
            "candidate_kind": "edge",
            "payload": {
                "type": "uses",
                "derived": True,
                "derivation_reason": "ignored",
            },
        },
    ]
    ledger_path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    origins = ingest_quality_check.local_candidate_origins(str(tmp_path), "run-1")

    assert origins == {
        "edge": {
            "raw": 1,
            "relation curator canonicalized predicate": 1,
        }
    }


def test_local_node_review_summary_counts_verdicts_states_and_types(tmp_path: Path) -> None:
    ledger_dir = tmp_path / ".marginalia"
    ledger_dir.mkdir()
    rows = [
        {
            "kind": "candidate",
            "run_id": "run-1",
            "candidate_id": "node-1",
            "candidate_kind": "node",
            "state": "proposed",
            "payload": {"type": "Agent", "title": "Frodo"},
        },
        {
            "kind": "candidate",
            "run_id": "run-1",
            "candidate_id": "node-2",
            "candidate_kind": "node",
            "state": "queued",
            "payload": {
                "candidate": {"type": "Place", "title": "Amon Lhaw"},
                "reason": "not grounded",
            },
        },
        {
            "kind": "candidate",
            "run_id": "run-1",
            "candidate_id": "node-3",
            "candidate_kind": "node",
            "state": "proposed",
            "payload": {"type": "Place", "title": "Amon Lhaw"},
        },
        {
            "kind": "candidate",
            "run_id": "run-1",
            "candidate_id": "node-4",
            "candidate_kind": "node",
            "state": "queued",
            "payload": {"type": "Concept", "title": "Frodo"},
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "node-1",
            "method": "curator",
            "verdict": "commit",
            "reason": "grounded",
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "node-2",
            "method": "curator",
            "verdict": "queue",
            "reason": "not grounded",
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "node-3",
            "method": "curator",
            "verdict": "commit",
            "reason": "grounded elsewhere",
        },
        {
            "kind": "comparison",
            "run_id": "other-run",
            "candidate_id": "node-3",
            "method": "curator",
            "verdict": "commit",
        },
    ]
    (ledger_dir / "candidate-ledger.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows),
        encoding="utf-8",
    )

    summary = ingest_quality_check.local_node_review_summary(
        str(tmp_path),
        "run-1",
        limit=3,
    )

    assert summary["candidate_states"] == {"proposed": 2, "queued": 2}
    assert summary["candidate_states_by_type"] == {
        "proposed": {"Agent": 1, "Place": 1},
        "queued": {"Concept": 1, "Place": 1},
    }
    assert summary["node_curator_verdicts"] == {"commit": 2, "queue": 1}
    assert summary["node_types_by_verdict"] == {
        "commit": {"Agent": 1, "Place": 1},
        "queue": {"Place": 1},
    }
    assert summary["sample_titles_by_verdict"] == {
        "commit": ["Amon Lhaw", "Frodo"],
        "queue": ["Amon Lhaw"],
    }
    assert summary["titles_with_multiple_verdicts"] == [
        {"title": "Amon Lhaw", "types": ["Place"], "verdicts": ["commit", "queue"]}
    ]
    assert summary["titles_with_multiple_types"] == [
        {"title": "Frodo", "types": ["Agent", "Concept"]}
    ]
    assert summary["recent_node_reviews"][-1] == {
        "candidate_id": "node-3",
        "verdict": "commit",
        "type": "Place",
        "title": "Amon Lhaw",
        "reason": "grounded elsewhere",
    }


def test_local_relation_review_summary_counts_predicates_and_endpoint_gates(
    tmp_path: Path,
) -> None:
    ledger_dir = tmp_path / ".marginalia"
    ledger_dir.mkdir()
    rows = [
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "edge-1",
            "method": "relation_curator",
            "verdict": "commit",
            "reason": "grounded",
            "payload": {
                "relation_kind": "edge",
                "type": "located_in",
                "canonical_predicate": "located_in",
                "proposed_terminal_action": "create_edge_or_claim",
            },
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "claim-1",
            "method": "relation_curator",
            "verdict": "queue",
            "reason": "too loose",
            "payload": {
                "relation_kind": "claim",
                "type": "action",
                "canonical_predicate": "",
                "proposed_terminal_action": "create_edge_or_claim",
            },
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "claim-1",
            "method": "endpoint_gate",
            "verdict": "skipped_endpoint",
            "reason": "relationship endpoint not accepted by node gate: src_ref",
            "payload": {"relation_kind": "claim"},
        },
        {
            "kind": "comparison",
            "run_id": "other-run",
            "candidate_id": "edge-2",
            "method": "relation_curator",
            "verdict": "commit",
            "payload": {"type": "ignored"},
        },
    ]
    (ledger_dir / "candidate-ledger.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows),
        encoding="utf-8",
    )

    summary = ingest_quality_check.local_relation_review_summary(
        str(tmp_path),
        "run-1",
        limit=3,
    )

    assert summary["relation_curator_verdicts"] == {"commit": 1, "queue": 1}
    assert summary["relation_kinds"] == {"claim": 1, "edge": 1}
    assert summary["terminal_actions"] == {"create_edge_or_claim": 2}
    assert summary["canonical_predicates"] == {"located_in": 1}
    assert summary["predicates_by_verdict"] == {
        "commit": {"located_in": 1},
        "queue": {"action": 1},
    }
    assert summary["endpoint_gate_reasons"] == {
        "relationship endpoint not accepted by node gate: src_ref": 1
    }
    assert summary["recent_relation_reviews"][-1] == {
        "candidate_id": "claim-1",
        "verdict": "queue",
        "relation_kind": "claim",
        "predicate": "action",
        "canonical_predicate": None,
        "terminal_action": "create_edge_or_claim",
        "reason": "too loose",
    }


def test_local_endpoint_shadow_summary_counts_same_title_accepted_sibling(
    tmp_path: Path,
) -> None:
    ledger_dir = tmp_path / ".marginalia"
    ledger_dir.mkdir()
    rows = [
        {
            "kind": "candidate",
            "run_id": "run-1",
            "candidate_kind": "node",
            "candidate_id": "accepted-galadriel",
            "state": "proposed",
            "payload": {
                "type": "Agent",
                "title": "Galadriel",
                "content": "accepted",
            },
        },
        {
            "kind": "candidate",
            "run_id": "run-1",
            "candidate_kind": "node",
            "candidate_id": "queued-galadriel",
            "state": "proposed",
            "payload": {
                "type": "Agent",
                "title": "Galadriel ",
                "content": "queued",
            },
        },
        {
            "kind": "candidate",
            "run_id": "run-1",
            "candidate_kind": "node",
            "candidate_id": "superseded-galadriel",
            "state": "superseded",
            "payload": {
                "type": "Agent",
                "title": "Galadriel",
                "content": "post-curator duplicate",
            },
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "accepted-galadriel",
            "method": "curator",
            "verdict": "commit",
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "queued-galadriel",
            "method": "curator",
            "verdict": "queue",
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "superseded-galadriel",
            "method": "curator",
            "verdict": "commit",
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "edge-1",
            "method": "endpoint_gate",
            "verdict": "skipped_endpoint",
            "reason": "relationship endpoint not accepted by node gate: src_ref",
            "payload": {
                "relation_kind": "claim",
                "src_ref": "queued-galadriel",
                "dst_literal": "the skill of the Dwarves is in their hands",
            },
        },
    ]
    (ledger_dir / "candidate-ledger.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows),
        encoding="utf-8",
    )

    summary = ingest_quality_check.local_endpoint_shadow_summary(
        str(tmp_path),
        "run-1",
        limit=3,
    )

    assert summary["endpoint_gate_total"] == 1
    assert summary["shadowed_by_accepted_same_title"] == 1
    assert summary["remappable_exact_title_endpoint_gates"] == 1
    assert summary["ambiguous_exact_title_endpoint_gates"] == 0
    assert summary["missing_endpoint_verdicts"] == {"queue": 1}
    assert summary["missing_endpoint_types"] == {"Agent": 1}
    assert summary["shadowed_by_field_type_verdict"] == {"src_ref:Agent:queue": 1}
    assert summary["samples"] == [
        {
            "candidate_id": "edge-1",
            "missing_field": "src_ref",
            "missing_ref": "queued-galadriel",
            "missing_type": "Agent",
            "missing_title": "Galadriel ",
            "missing_verdict": "queue",
            "accepted_same_title_refs": ["accepted-galadriel"],
            "reason": "relationship endpoint not accepted by node gate: src_ref",
        }
    ]


def test_local_pending_commit_preview_ignores_audit_only_rows(tmp_path: Path) -> None:
    ledger_dir = tmp_path / ".marginalia"
    ledger_dir.mkdir()
    rows = [
        {
            "kind": "candidate",
            "run_id": "run-1",
            "candidate_kind": "node",
            "candidate_id": "frodo",
            "state": "proposed",
            "payload": {"type": "Agent", "title": "Frodo"},
        },
        {
            "kind": "candidate",
            "run_id": "run-1",
            "candidate_kind": "node",
            "candidate_id": "ring",
            "state": "proposed",
            "payload": {"type": "InformationObject", "title": "One Ring"},
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "frodo",
            "method": "curator",
            "verdict": "commit",
            "payload": {},
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "ring",
            "method": "curator",
            "verdict": "queue",
            "payload": {},
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "audit-node",
            "method": "curator",
            "verdict": "commit",
            "payload": {"audit_only": True, "llm_skipped": True},
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "edge-1",
            "method": "relation_curator",
            "verdict": "commit",
            "payload": {
                "type": "carries",
                "canonical_predicate": "carries",
                "src_ref": "frodo",
                "dst_ref": "ring",
                "relation_kind": "edge",
                "proposed_terminal_action": "create_edge_or_claim",
            },
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "edge-original",
            "method": "relation_curator",
            "verdict": "commit",
            "payload": {
                "type": "has",
                "canonical_predicate": "carries",
                "proposed_terminal_action": "canonicalize_predicate",
            },
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "edge-dead",
            "method": "relation_curator",
            "verdict": "commit",
            "payload": {
                "type": "mentions",
                "proposed_terminal_action": "dead_letter",
            },
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "edge-queued",
            "method": "relation_curator",
            "verdict": "queue",
            "payload": {
                "type": "near",
                "proposed_terminal_action": "create_edge_or_claim",
            },
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "audit-edge",
            "method": "relation_curator",
            "verdict": "commit",
            "payload": {
                "audit_only": True,
                "llm_skipped": True,
                "type": "ignored",
                "proposed_terminal_action": "supersede_candidate",
            },
        },
    ]
    (ledger_dir / "candidate-ledger.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows),
        encoding="utf-8",
    )

    preview = ingest_quality_check.local_pending_commit_preview(
        str(tmp_path),
        "run-1",
        limit=3,
    )

    assert preview["nodes"]["accepted_for_write"] == 1
    assert preview["nodes"]["queued_or_abstained"] == 1
    assert preview["nodes"]["types_by_verdict"] == {
        "commit": {"Agent": 1},
        "queue": {"InformationObject": 1},
    }
    assert preview["nodes"]["sample_candidates_by_verdict"] == {
        "commit": [{"candidate_id": "frodo", "type": "Agent", "title": "Frodo"}],
        "queue": [{"candidate_id": "ring", "type": "InformationObject", "title": "One Ring"}],
    }
    assert preview["relations"]["accepted_for_write"] == 1
    assert preview["relations"]["canonicalized_originals"] == 1
    assert preview["relations"]["endpoint_dead_letters"] == 1
    assert preview["relations"]["queued_or_abstained"] == 1
    assert preview["relations"]["accepted_predicates"] == {"carries": 1}
    assert preview["relations"]["queued_predicates"] == {"near": 1}
    assert preview["relations"]["sample_relations_by_verdict"]["commit"][0] == {
        "candidate_id": "edge-1",
        "predicate": "carries",
        "raw_predicate": "carries",
        "terminal_action": "create_edge_or_claim",
        "relation_kind": "edge",
        "subject": {"ref": "frodo", "type": "Agent", "title": "Frodo"},
        "object": {"ref": "ring", "type": "InformationObject", "title": "One Ring"},
    }


def test_check_report_enforces_pending_commit_minimums() -> None:
    report = {
        "stats": {"total_nodes": 0, "total_edges": 0, "node_types": []},
        "queue": {"summary": {"queued": 3, "processing": 1}},
        "ledger_detail": {
            "pending_commit_preview": {
                "nodes": {"accepted_for_write": 621},
                "relations": {"accepted_for_write": 428},
            }
        },
        "node_names": [],
        "edge_types": [],
    }

    checks = ingest_quality_check.check_report(
        _args(
            min_nodes=None,
            min_edges=None,
            min_claims=None,
            min_pending_nodes=500,
            min_pending_relations=400,
            max_queue_items=4,
            expect_title=[],
            forbid_title=[],
            expect_edge_type=[],
            forbid_edge_type=[],
        ),
        report,
    )

    assert [check.detail for check in checks] == [
        "pending accepted nodes 621 >= 500",
        "pending accepted relations 428 >= 400",
        "active queue items 4 <= 4",
    ]
    assert all(check.ok for check in checks)


def test_local_pending_domain_profile_uses_accepted_pending_nodes(tmp_path: Path) -> None:
    ledger_dir = tmp_path / ".marginalia"
    ledger_dir.mkdir()
    rows = [
        {
            "kind": "candidate",
            "run_id": "run-1",
            "candidate_kind": "node",
            "candidate_id": "tolkien",
            "state": "proposed",
            "payload": {"type": "Agent", "title": "J.R.R. Tolkien"},
        },
        {
            "kind": "candidate",
            "run_id": "run-1",
            "candidate_kind": "node",
            "candidate_id": "lotr",
            "state": "proposed",
            "payload": {"type": "InformationObject", "title": "The Lord of the Rings"},
        },
        {
            "kind": "candidate",
            "run_id": "run-1",
            "candidate_kind": "node",
            "candidate_id": "hobbit",
            "state": "proposed",
            "payload": {"type": "InformationObject", "title": "The Hobbit"},
        },
        {
            "kind": "candidate",
            "run_id": "run-1",
            "candidate_kind": "node",
            "candidate_id": "frodo",
            "state": "proposed",
            "payload": {"type": "Agent", "title": "Frodo Baggins"},
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "tolkien",
            "method": "curator",
            "verdict": "commit",
            "payload": {},
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "lotr",
            "method": "curator",
            "verdict": "commit",
            "payload": {},
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "hobbit",
            "method": "curator",
            "verdict": "commit",
            "payload": {},
        },
        {
            "kind": "comparison",
            "run_id": "run-1",
            "candidate_id": "frodo",
            "method": "curator",
            "verdict": "queue",
            "payload": {},
        },
    ]
    (ledger_dir / "candidate-ledger.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows),
        encoding="utf-8",
    )

    profile = ingest_quality_check.local_pending_domain_profile(
        str(tmp_path),
        "run-1",
        "lotr",
    )

    assert profile["source"] == "pending_commit_preview"
    assert profile["node_count"] == 3
    assert profile["coverage"] == {"present": 3, "total": 21, "fraction": 3 / 21}
    assert profile["groups"]["works_and_contributors"]["present"] == 3
    assert profile["groups"]["characters"]["present"] == 0
    assert profile["missing_diagnostics"][0] == {
        "label": "Frodo",
        "group": "characters",
        "candidate_count": 1,
        "states": {"proposed": 1},
        "verdicts": {"queue": 1},
        "types": {"Agent": 1},
        "samples": [
            {
                "candidate_id": "frodo",
                "title": "Frodo Baggins",
                "type": "Agent",
                "state": "proposed",
                "verdict": "queue",
                "reason": "",
            }
        ],
    }


def test_check_report_enforces_pending_domain_profile_coverage() -> None:
    report = {
        "stats": {"total_nodes": 0, "total_edges": 0, "node_types": []},
        "queue": {"summary": {"queued": 3, "processing": 1}},
        "ledger_detail": {
            "pending_domain_profile": {
                "name": "lotr",
                "coverage": {"present": 3, "total": 21, "fraction": 3 / 21},
                "forbidden_present": [],
            }
        },
        "node_names": [],
        "edge_types": [],
    }

    checks = ingest_quality_check.check_report(
        _args(
            min_nodes=None,
            min_edges=None,
            min_claims=None,
            min_pending_domain_profile_coverage=0.10,
            max_queue_items=4,
            expect_title=[],
            forbid_title=[],
            expect_edge_type=[],
            forbid_edge_type=[],
        ),
        report,
    )

    assert [check.detail for check in checks] == [
        "active queue items 4 <= 4",
        "pending domain forbidden titles absent",
        "pending domain profile coverage 3/21 >= 0.10",
    ]
    assert all(check.ok for check in checks)


def test_graph_profile_summarizes_semantic_shape() -> None:
    nodes = [
        {"id": "frodo", "name": "Frodo", "type": "Agent"},
        {"id": "shire", "name": "Shire", "type": "Place"},
        {"id": "doc", "name": "The Fellowship", "type": "Document"},
    ]
    edges = [
        {"src": "doc", "dst": "frodo", "type": "schema:mentions"},
        {"src": "frodo", "dst": "doc", "type": "prov:wasDerivedFrom"},
        {"src": "frodo", "dst": "shire", "type": "rdf:subject"},
        {"src": "frodo", "dst": "shire", "type": "located_in"},
        {"src": "shire", "dst": "frodo", "type": "associated_with"},
    ]

    profile = ingest_quality_check.graph_profile(nodes, edges, sample_limit=2)

    assert profile["node_types"] == {"Agent": 1, "Document": 1, "Place": 1}
    assert profile["edge_types"] == {
        "associated_with": 1,
        "located_in": 1,
        "prov:wasDerivedFrom": 1,
        "rdf:subject": 1,
        "schema:mentions": 1,
    }
    assert profile["semantic_edge_types"] == {
        "associated_with": 1,
        "located_in": 1,
    }
    assert profile["knowledge_node_count"] == 2
    assert profile["semantic_edge_count"] == 2
    assert profile["top_degree_nodes"] == [
        {"id": "frodo", "name": "Frodo", "type": "Agent", "semantic_degree": 2},
        {"id": "shire", "name": "Shire", "type": "Place", "semantic_degree": 2},
    ]
    assert profile["sample_titles_by_type"] == {
        "Agent": ["Frodo"],
        "Document": ["The Fellowship"],
        "Place": ["Shire"],
    }


def test_semantic_quality_uses_complete_store_and_keeps_layers_separate() -> None:
    store = InMemoryStore()
    nodes = [
        Node(id="alice", type="Agent", title="Alice"),
        Node(id="project", type="Concept", title="Project Atlas"),
        Node(id="acme-agent", type="Agent", title="Acme"),
        Node(id="acme-concept", type="Concept", title=" acme "),
        Node(id="activity", type="Activity", title="extraction", facets=dict(INFRA_FACET)),
        Node(id="extractor", type="Agent", title="extractor", facets=dict(INFRA_FACET)),
        Node(
            id="block",
            type="Block",
            title="paragraph 0",
            facets={
                "source_path": "/vault/notes/source.md",
                "byte_start": 0,
                "byte_end": 42,
                "content_hash": "a" * 64,
            },
        ),
        Node(
            id="claim",
            type="Claim",
            title="Alice leads Project Atlas",
            facets={
                "S_id": "alice",
                "P": "leads",
                "O_id": "project",
                "block_id": "block",
                "extraction_activity_id": "activity",
                "agent_id": "extractor",
            },
        ),
    ]
    for node in nodes:
        store.add_node(node)
    for edge in (
        Edge(id="topology", type="leads", src="alice", dst="project"),
        Edge(id="prov", type="prov:wasDerivedFrom", src="claim", dst="block"),
        Edge(id="subject", type="rdf:subject", src="claim", dst="alice"),
        Edge(id="object", type="rdf:object", src="claim", dst="project"),
        Edge(id="generated", type="prov:wasGeneratedBy", src="claim", dst="activity"),
        Edge(id="attributed", type="prov:wasAttributedTo", src="claim", dst="extractor"),
    ):
        store.add_edge(edge)

    report = evaluate_store(
        store,
        integrity=_fresh_integrity(),
        registered_predicates={"leads"},
    )

    evidence = dict(report["evidence"])
    external_inputs = evidence.pop("external_inputs")
    assert evidence == {
        "scope": "stored_graph",
        "complete": True,
        "graph_generation": "generation-1",
        "integrity_status": "verified",
        "integrity_freshness": "fresh",
        "technical_integrity_verified": True,
        "topology_source": "stored_edge_properties",
        "topology_evidence_status": "measured",
        "integrity_audit": {
            "status": "verified",
            "issue_count": None,
            "adjacency_complete": True,
            "first_issue": None,
        },
        "authoritative": False,
        "reason": "ADR 0039 has not published an authoritative semantic-baseline generation",
    }
    assert external_inputs["registered_predicates"]["status"] == "supplied"
    assert len(external_inputs["registered_predicates"]["sha256"]) == 64
    assert external_inputs["recall_samples"] == {"status": "not_supplied"}
    assert external_inputs["adjudication"] == {"status": "not_supplied"}
    assert report["layers"]["surface"]["cross_type_conflict_groups"]["count"] == 1
    assert report["layers"]["identity"]["cluster_quality"]["status"] == "not_measured"
    assert report["layers"]["predicate"]["raw_vocabulary_size"] == 1
    assert report["layers"]["relation"]["missing_topology_edges"]["count"] == 0
    assert report["layers"]["relation"]["unanchored_claims"]["count"] == 0
    assert report["layers"]["relation"]["materialized_topology_edge_types"] == {
        "status": "measured",
        "counts": {"leads": 1},
    }
    assert report["hard_invariants"]["status"] == "incomplete"
    assert report["hard_invariants"]["measured_pass"] is True
    assert report["verdict"] == {
        "scope": "registered_semantic_hard_invariants",
        "status": "incomplete",
        "authoritative_pass": False,
        "reason": "evidence or required semantic measurements are incomplete",
    }


def test_semantic_snapshot_measures_opaque_population_and_exact_churn() -> None:
    before = evaluate_store(
        _semantic_snapshot_store(),
        integrity=_fresh_integrity("generation-before"),
        registered_predicates={"leads"},
        **_SNAPSHOT_FINGERPRINTS,
    )["semantic_snapshot"]
    after = evaluate_store(
        _semantic_snapshot_store(expanded=True),
        integrity=_fresh_integrity("generation-after"),
        registered_predicates={"leads", "supports"},
        **_SNAPSHOT_FINGERPRINTS,
    )["semantic_snapshot"]

    assert before["schema_version"] == "semantic_snapshot.v1"
    assert before["status"] == "measured"
    assert before["dimensions"]["identities"]["count"] == 2
    assert before["dimensions"]["predicates"]["count"] == 1
    assert before["dimensions"]["relations"]["count"] == 1
    assert all(
        member.startswith("sha256:")
        for dimension in before["dimensions"].values()
        for member in dimension["members"]
    )

    churn = compare_semantic_snapshots(before, after)
    assert churn["schema_version"] == "semantic_churn.v1"
    assert churn["stable"] is False
    assert churn["dimensions"]["identities"] | {
        "added_samples": [],
        "removed_samples": [],
    } == {
        "before_count": 2,
        "after_count": 3,
        "added_count": 1,
        "removed_count": 0,
        "symmetric_difference_count": 1,
        "union_count": 3,
        "churn_share": 0.333333,
        "stable": False,
        "added_samples": [],
        "removed_samples": [],
        "samples_truncated": False,
    }
    assert churn["dimensions"]["predicates"]["churn_share"] == 0.5
    assert churn["dimensions"]["relations"]["churn_share"] == 0.5
    assert compare_semantic_snapshots(before, before)["stable"] is True

    changed_policy = evaluate_store(
        _semantic_snapshot_store(),
        integrity=_fresh_integrity("generation-policy-change"),
        registered_predicates={"leads"},
        **{
            **_SNAPSHOT_FINGERPRINTS,
            "semantic_policy_fingerprint": f"sha256:{'4' * 64}",
        },
    )["semantic_snapshot"]
    policy_churn = compare_semantic_snapshots(before, changed_policy)
    assert policy_churn["population_stable"] is True
    assert policy_churn["fingerprints"]["all_equal"] is False
    assert policy_churn["stable"] is False


def test_semantic_snapshot_compares_semantics_not_physical_ids_or_model_prose() -> None:
    before = evaluate_store(
        _semantic_snapshot_equivalent_store("first"),
        integrity=_fresh_integrity("generation-first"),
        registered_predicates={"leads"},
        **_SNAPSHOT_FINGERPRINTS,
    )["semantic_snapshot"]
    after = evaluate_store(
        _semantic_snapshot_equivalent_store("second"),
        integrity=_fresh_integrity("generation-second"),
        registered_predicates={"leads"},
        **_SNAPSHOT_FINGERPRINTS,
    )["semantic_snapshot"]

    churn = compare_semantic_snapshots(before, after)

    assert churn["stable"] is True
    assert churn["population_stable"] is True
    assert all(dimension["churn_share"] == 0.0 for dimension in churn["dimensions"].values())

    changed = evaluate_store(
        _semantic_snapshot_equivalent_store("third", object_title="Atlas Project"),
        integrity=_fresh_integrity("generation-third"),
        registered_predicates={"leads"},
        **_SNAPSHOT_FINGERPRINTS,
    )["semantic_snapshot"]
    changed_churn = compare_semantic_snapshots(before, changed)
    assert changed_churn["stable"] is False
    assert changed_churn["dimensions"]["identities"]["churn_share"] > 0
    assert changed_churn["dimensions"]["relations"]["churn_share"] > 0


def test_semantic_snapshot_fails_closed_without_verified_complete_store() -> None:
    snapshot = evaluate_store(
        _semantic_snapshot_store(),
        registered_predicates={"leads"},
    )["semantic_snapshot"]

    assert snapshot == {
        "schema_version": "semantic_snapshot.v1",
        "status": "not_measured",
        "graph_generation": None,
        "reason": "ADR 0039 integrity is unverified for this semantic scan",
        "dimensions": {},
    }
    with pytest.raises(ValueError, match="must be measured"):
        compare_semantic_snapshots(snapshot, snapshot)


def test_semantic_snapshot_comparison_rejects_tampered_member_digest() -> None:
    snapshot = evaluate_store(
        _semantic_snapshot_store(),
        integrity=_fresh_integrity(),
        registered_predicates={"leads"},
        **_SNAPSHOT_FINGERPRINTS,
    )["semantic_snapshot"]
    tampered = json.loads(json.dumps(snapshot))
    tampered["dimensions"]["identities"]["members"].append(f"sha256:{'f' * 64}")
    tampered["dimensions"]["identities"]["members"].sort()
    tampered["dimensions"]["identities"]["count"] += 1

    with pytest.raises(ValueError, match="sha256 does not match members"):
        compare_semantic_snapshots(snapshot, tampered)

    legacy_encoding = json.loads(json.dumps(snapshot))
    legacy_encoding["member_encoding"] = "sha256(kind + NUL + value)"
    with pytest.raises(ValueError, match="member_encoding"):
        compare_semantic_snapshots(snapshot, legacy_encoding)


def test_semantic_quality_keeps_recall_explicitly_unmeasured_without_samples() -> None:
    report = evaluate_store(InMemoryStore(), registered_predicates=set())

    recall = report["layers"]["recall"]
    assert recall == {
        "schema_version": "recall_cost.aggregate.v1",
        "sample_schema_version": "recall_cost.v1",
        "percentile_method": "nearest_rank",
        "status": "not_measured",
        "reason": "no recall_cost.v1 samples were supplied",
        "provided_samples": 0,
        "measured_samples": 0,
        "rejected_samples": {"count": 0, "samples": []},
    }
    checks = {row["code"]: row for row in report["hard_invariants"]["checks"]}
    assert checks["completion_free_recall"]["status"] == "not_measured"


def test_semantic_registry_gate_excludes_deterministic_support_predicates() -> None:
    store = InMemoryStore()
    store.add_node(
        Node(
            id="claim",
            type="Claim",
            title="document has tag alpha",
            facets={
                "S_id": "document",
                "P": "has_tag",
                "O_literal": "alpha",
                "block_id": "block",
            },
        )
    )

    report = evaluate_store(store, registered_predicates=set())

    coverage = report["layers"]["predicate"]["registry_coverage"]
    assert coverage["unregistered_count"] == 0
    assert coverage["excluded_structural_predicates"] == ["has_tag"]
    checks = {row["code"]: row for row in report["hard_invariants"]["checks"]}
    assert checks["registered_predicates"]["status"] == "passed"


def test_semantic_quality_aggregates_complete_recall_cost_samples() -> None:
    report = evaluate_store(
        InMemoryStore(),
        registered_predicates=set(),
        recall_samples=(
            _recall_sample(),
            _recall_sample(
                query_embedding_latency_ms=8.0,
                deterministic_retrieval_latency_ms=12.0,
                deterministic_projection_latency_ms=4.0,
                total_latency_ms=20.0,
                retrieved_results=4,
                retrieved_bytes=240,
                query_embedding_provider="RemoteEmbedder",
                query_embedding_model="remote-model",
                query_embedding_execution="remote",
            ),
        ),
    )

    recall = report["layers"]["recall"]
    assert recall["status"] == "measured"
    assert recall["provided_samples"] == 2
    assert recall["completion"] == {
        "calls_total": 0,
        "calls_max_per_recall": 0,
        "generated_tokens_total": 0,
        "generated_tokens_max_per_recall": 0,
        "violations": [],
    }
    assert recall["query_embedding"]["calls_total"] == 2
    assert recall["query_embedding"]["latency_ms"] == {
        "total": 12.0,
        "p50": 4.0,
        "p95": 8.0,
    }
    assert recall["query_embedding"]["execution"] == {"local": 1, "remote": 1}
    assert recall["deterministic_retrieval"]["latency_ms"] == {
        "total": 18.0,
        "p50": 6.0,
        "p95": 12.0,
    }
    assert recall["deterministic_retrieval"]["results"] == {
        "total": 6,
        "p50": 2,
        "p95": 4,
    }
    assert recall["deterministic_retrieval"]["bytes"] == {
        "total": 360,
        "p50": 120,
        "p95": 240,
    }
    assert recall["deterministic_projection"]["latency_ms"] == {
        "total": 6.0,
        "p50": 2.0,
        "p95": 4.0,
    }
    assert recall["total_latency_ms"] == {"total": 30.0, "p50": 10.0, "p95": 20.0}
    checks = {row["code"]: row for row in report["hard_invariants"]["checks"]}
    assert checks["completion_free_recall"]["status"] == "passed"


def test_semantic_quality_fails_recall_that_uses_a_completion() -> None:
    report = evaluate_store(
        InMemoryStore(),
        registered_predicates=set(),
        recall_samples=[
            _recall_sample(completion_calls=1, generated_tokens=12, completion_free=False)
        ],
    )

    recall = report["layers"]["recall"]
    assert recall["status"] == "measured"
    assert recall["completion"]["violations"] == [
        {"index": 0, "completion_calls": 1, "generated_tokens": 12}
    ]
    checks = {row["code"]: row for row in report["hard_invariants"]["checks"]}
    assert checks["completion_free_recall"]["status"] == "failed"
    assert report["verdict"]["status"] == "failed"


def test_semantic_quality_does_not_pass_incomplete_recall_accounting() -> None:
    report = evaluate_store(
        InMemoryStore(),
        registered_predicates=set(),
        recall_samples=[
            _recall_sample(),
            {
                "schema_version": "recall_cost.v1",
                "measurement_status": "not_measured",
            },
        ],
    )

    recall = report["layers"]["recall"]
    assert recall["status"] == "not_measured"
    assert recall["provided_samples"] == 2
    assert recall["measured_samples"] == 1
    assert recall["rejected_samples"] == {
        "count": 1,
        "samples": [{"index": 1, "reason": "sample is explicitly not_measured"}],
    }
    checks = {row["code"]: row for row in report["hard_invariants"]["checks"]}
    assert checks["completion_free_recall"]["status"] == "not_measured"


def test_semantic_quality_requires_projection_timing_in_every_recall_sample() -> None:
    incomplete = _recall_sample()
    incomplete.pop("deterministic_projection_latency_ms")

    report = evaluate_store(
        InMemoryStore(),
        registered_predicates=set(),
        recall_samples=[incomplete],
    )

    recall = report["layers"]["recall"]
    assert recall["status"] == "not_measured"
    assert recall["rejected_samples"] == {
        "count": 1,
        "samples": [
            {
                "index": 0,
                "reason": "deterministic_projection_latency_ms must be a number >= 0",
            }
        ],
    }
    checks = {row["code"]: row for row in report["hard_invariants"]["checks"]}
    assert checks["completion_free_recall"]["status"] == "not_measured"


def test_semantic_quality_does_not_trust_cached_integrity_for_topology() -> None:
    store = InMemoryStore()
    store.add_node(Node(id="alice", type="Agent", title="Alice"))
    store.add_node(Node(id="project", type="Concept", title="Project"))
    store.add_edge(Edge(id="topology", type="leads", src="alice", dst="project"))

    report = evaluate_store(
        store,
        integrity={"status": "verified", "graph_generation": "generation-1"},
        registered_predicates={"leads"},
    )

    assert report["evidence"]["complete"] is False
    assert report["evidence"]["integrity_freshness"] == "cached"
    assert report["evidence"]["technical_integrity_verified"] is False
    relation = report["layers"]["relation"]
    assert relation["topology_evidence_status"] == "not_measured"
    assert relation["materialized_topology_edges"] is None
    assert relation["topology_edges_without_claims"]["status"] == "not_measured"
    checks = {row["code"]: row for row in report["hard_invariants"]["checks"]}
    assert checks["topology_claim_coverage"]["status"] == "not_measured"
    assert report["verdict"]["status"] == "incomplete"


def test_semantic_quality_rejects_audit_for_different_store_generation() -> None:
    store = InMemoryStore()
    store._graph_handle = SimpleNamespace(graph_generation="store-generation")  # type: ignore[attr-defined]  # noqa: SLF001
    store.add_node(Node(id="alice", type="Agent", title="Alice"))
    store.add_node(Node(id="project", type="Concept", title="Project"))
    store.add_edge(Edge(id="topology", type="leads", src="alice", dst="project"))

    report = evaluate_store(
        store,
        integrity=_fresh_integrity("proof-generation"),
        registered_predicates={"leads"},
    )

    assert report["evidence"]["complete"] is False
    assert report["evidence"]["technical_integrity_verified"] is False
    assert report["evidence"]["integrity_freshness"] == "rejected"
    assert report["population"]["edges"] is None
    relation = report["layers"]["relation"]
    assert relation["topology_evidence_status"] == "not_measured"
    assert relation["materialized_topology_edges"] is None
    assert relation["topology_edges_without_claims"]["status"] == "not_measured"


def test_semantic_quality_rejects_audit_without_physical_adjacency_proof() -> None:
    store = InMemoryStore()
    store.add_node(Node(id="alice", type="Agent", title="Alice"))
    store.add_node(Node(id="project", type="Concept", title="Project"))
    store.add_edge(Edge(id="topology", type="leads", src="alice", dst="project"))

    report = evaluate_store(
        store,
        integrity=_fresh_integrity(adjacency_complete=None),
        registered_predicates={"leads"},
    )

    assert report["evidence"]["technical_integrity_verified"] is False
    assert report["evidence"]["integrity_freshness"] == "rejected"
    assert report["population"]["edges"] is None
    relation = report["layers"]["relation"]
    assert relation["topology_evidence_status"] == "not_measured"
    assert relation["materialized_topology_edges"] is None
    assert relation["materialized_topology_edge_types"]["status"] == "not_measured"


def test_semantic_quality_never_hides_unmeasured_registry_or_bad_predicate() -> None:
    store = InMemoryStore()
    store.add_node(Node(id="alice", type="Agent", title="Alice"))
    store.add_node(Node(id="project", type="Concept", title="Project"))
    store.add_node(
        Node(
            id="claim",
            type="Claim",
            title="Alice unknown Project",
            facets={
                "S_id": "alice",
                "P": "unknown",
                "O_id": "project",
                "block_id": "missing-block",
            },
        )
    )

    report = evaluate_store(
        store,
        integrity=_fresh_integrity(),
    )

    checks = {row["code"]: row for row in report["hard_invariants"]["checks"]}
    assert checks["placeholder_predicates"]["status"] == "failed"
    assert checks["registered_predicates"]["status"] == "not_measured"
    assert checks["claim_source_anchors"]["status"] == "failed"
    assert checks["relation_materialization"]["status"] == "failed"
    assert report["hard_invariants"]["measured_pass"] is False
    assert report["verdict"]["status"] == "failed"


def test_semantic_quality_checks_predicates_on_literal_claims() -> None:
    store = InMemoryStore()
    store.add_node(Node(id="alice", type="Agent", title="Alice"))
    store.add_node(
        Node(
            id="block",
            type="Block",
            title="paragraph 0",
            facets={
                "source_path": "/vault/notes/source.md",
                "byte_start": 0,
                "byte_end": 42,
                "content_hash": "a" * 64,
            },
        )
    )
    store.add_node(
        Node(
            id="claim",
            type="Claim",
            title="Alice has an unknown status",
            facets={
                "S_id": "alice",
                "P": "unknown",
                "O_literal": "status",
                "block_id": "block",
            },
        )
    )
    store.add_edge(Edge(id="prov", type="prov:wasDerivedFrom", src="claim", dst="block"))
    store.add_edge(Edge(id="subject", type="rdf:subject", src="claim", dst="alice"))

    report = evaluate_store(
        store,
        integrity=_fresh_integrity(),
        registered_predicates={"unknown"},
    )

    predicate = report["layers"]["predicate"]
    assert predicate["predicate_assertions"] == 1
    assert predicate["relation_claims"] == 0
    assert predicate["placeholder_predicates"]["count"] == 1
    relation = report["layers"]["relation"]
    assert relation["topology_isolated_entities"]["count"] == 1
    assert relation["isolated_entities"]["count"] == 0
    assert report["verdict"]["status"] == "failed"


def test_semantic_quality_detects_topology_edge_without_evidence_claim() -> None:
    store = InMemoryStore()
    store.add_node(Node(id="alice", type="Agent", title="Alice"))
    store.add_node(Node(id="project", type="Concept", title="Project"))
    store.add_edge(Edge(id="topology", type="leads", src="alice", dst="project"))

    report = evaluate_store(
        store,
        integrity=_fresh_integrity(),
        registered_predicates={"leads"},
    )

    coverage = report["layers"]["relation"]["topology_edges_without_claims"]
    assert coverage == {
        "count": 1,
        "samples": [{"src": "alice", "predicate": "leads", "dst": "project"}],
    }
    checks = {row["code"]: row for row in report["hard_invariants"]["checks"]}
    assert checks["topology_claim_coverage"]["status"] == "failed"
    assert report["verdict"]["status"] == "failed"


def test_semantic_quality_excludes_superseded_claims_from_live_relation_metrics() -> None:
    store = InMemoryStore()
    store.add_node(Node(id="alice", type="Agent", title="Alice"))
    store.add_node(Node(id="project", type="Concept", title="Old Project"))
    store.add_node(
        Node(
            id="claim",
            type="Claim",
            title="Alice led Old Project",
            facets={
                "S_id": "alice",
                "P": "led",
                "O_id": "project",
                "_superseded": True,
            },
        )
    )
    store.add_edge(Edge(id="topology", type="led", src="alice", dst="project"))

    report = evaluate_store(
        store,
        integrity=_fresh_integrity(),
        registered_predicates={"led"},
    )

    predicate = report["layers"]["predicate"]
    relation = report["layers"]["relation"]
    assert predicate["relation_claims"] == 0
    assert predicate["historical_superseded_claims"] == 1
    assert relation["materialized_topology_edges"] == 0
    assert relation["topology_edges_without_claims"]["count"] == 0
    assert relation["inactive_topology_edges"]["count"] == 1
    assert relation["isolated_entities"]["count"] == 2
    checks = {row["code"]: row for row in report["hard_invariants"]["checks"]}
    assert checks["topology_claim_coverage"]["status"] == "passed"


def test_semantic_quality_rejects_malformed_source_anchor_shape() -> None:
    store = InMemoryStore()
    store.add_node(Node(id="alice", type="Agent", title="Alice"))
    store.add_node(Node(id="project", type="Concept", title="Project"))
    store.add_node(
        Node(
            id="block",
            type="Block",
            title="paragraph 0",
            facets={
                "source_path": "/vault/notes/source.md",
                "byte_start": "wrong",
                "byte_end": -9,
                "content_hash": "bad",
            },
        )
    )
    store.add_node(
        Node(
            id="claim",
            type="Claim",
            title="Alice leads Project",
            facets={
                "S_id": "alice",
                "P": "leads",
                "O_id": "project",
                "block_id": "block",
            },
        )
    )
    store.add_edge(Edge(id="topology", type="leads", src="alice", dst="project"))
    store.add_edge(Edge(id="prov", type="prov:wasDerivedFrom", src="claim", dst="block"))

    report = evaluate_store(
        store,
        integrity=_fresh_integrity(),
        registered_predicates={"leads"},
    )

    checks = {row["code"]: row for row in report["hard_invariants"]["checks"]}
    assert checks["claim_source_anchors"]["status"] == "failed"
    assert checks["claim_bridge_materialization"]["status"] == "failed"
    assert checks["exact_byte_provenance"]["status"] == "not_measured"


@pytest.mark.parametrize("content_hash", ["sha256:" + "a" * 64, "A" * 64])
def test_semantic_quality_rejects_noncanonical_block_hash(content_hash: str) -> None:
    store = InMemoryStore()
    store.add_node(Node(id="alice", type="Agent", title="Alice"))
    store.add_node(
        Node(
            id="block",
            type="Block",
            title="paragraph 0",
            facets={
                "source_path": "/vault/notes/source.md",
                "byte_start": 0,
                "byte_end": 42,
                "content_hash": content_hash,
            },
        )
    )
    store.add_node(
        Node(
            id="claim",
            type="Claim",
            title="Alice has status",
            facets={
                "S_id": "alice",
                "P": "has_status",
                "O_literal": "active",
                "block_id": "block",
            },
        )
    )
    store.add_edge(Edge(id="prov", type="prov:wasDerivedFrom", src="claim", dst="block"))

    report = evaluate_store(
        store,
        integrity=_fresh_integrity(),
        registered_predicates={"has_status"},
    )

    checks = {row["code"]: row for row in report["hard_invariants"]["checks"]}
    assert checks["claim_source_anchors"]["status"] == "failed"


@pytest.mark.parametrize("predicate", ["schema:knows", "prov:wasDerivedFrom"])
def test_semantic_quality_classifies_topology_by_endpoint_types(predicate: str) -> None:
    store = InMemoryStore()
    store.add_node(Node(id="alice", type="Agent", title="Alice"))
    store.add_node(Node(id="bob", type="Agent", title="Bob"))
    store.add_node(
        Node(
            id="claim",
            type="Claim",
            title="Alice knows Bob",
            facets={"S_id": "alice", "P": predicate, "O_id": "bob"},
        )
    )
    store.add_edge(Edge(id="topology", type=predicate, src="alice", dst="bob"))

    report = evaluate_store(
        store,
        integrity=_fresh_integrity(),
        registered_predicates={predicate},
    )

    relation = report["layers"]["relation"]
    assert relation["materialized_topology_edges"] == 1
    assert relation["missing_topology_edges"]["count"] == 0
    assert relation["topology_edges_without_claims"]["count"] == 0
    assert relation["topology_isolated_entities"]["count"] == 0


def test_semantic_quality_rejects_wrong_provenance_target_types() -> None:
    store = InMemoryStore()
    store.add_node(Node(id="alice", type="Agent", title="Alice"))
    store.add_node(Node(id="wrong-activity", type="Concept", title="Not an activity"))
    store.add_node(Node(id="wrong-agent", type="Place", title="Not an agent"))
    store.add_node(
        Node(
            id="claim",
            type="Claim",
            title="Alice has status",
            facets={
                "S_id": "alice",
                "P": "has_status",
                "O_literal": "active",
                "extraction_activity_id": "wrong-activity",
                "agent_id": "wrong-agent",
            },
        )
    )
    store.add_edge(
        Edge(
            id="generated",
            type="prov:wasGeneratedBy",
            src="claim",
            dst="wrong-activity",
        )
    )
    store.add_edge(
        Edge(id="attributed", type="prov:wasAttributedTo", src="claim", dst="wrong-agent")
    )

    report = evaluate_store(
        store,
        integrity=_fresh_integrity(),
        registered_predicates={"has_status"},
    )

    missing = report["layers"]["relation"]["missing_claim_provenance"]
    assert missing["count"] == 2
    checks = {row["code"]: row for row in report["hard_invariants"]["checks"]}
    assert checks["claim_provenance_materialization"]["status"] == "failed"


def test_semantic_quality_mojibake_flag_does_not_match_valid_portuguese() -> None:
    store = InMemoryStore()
    for node in (
        Node(id="angela", type="Agent", title="Ângela"),
        Node(id="joao", type="Agent", title="JOÃO"),
        Node(id="broken", type="Agent", title="JoÃ£o"),
    ):
        store.add_node(node)

    report = evaluate_store(store, registered_predicates=set())

    flags = report["layers"]["surface"]["normalization_flags"]
    assert flags["counts"]["suspected_mojibake"] == 1
    assert flags["samples"]["suspected_mojibake"] == ["JoÃ£o"]


def test_surface_keys_keep_exact_merge_conservative() -> None:
    assert exact_surface_key("  Éowyn\tOf Rohan ") == "éowyn of rohan"
    assert exact_surface_key("Eowyn_of_Rohan") != exact_surface_key("Eowyn of Rohan")
    assert discovery_surface_key("Eowyn_of_Rohan") == discovery_surface_key("Eowyn of Rohan")


def test_vault_matches_name_or_path() -> None:
    current = {
        "name": "LOTR",
        "path": "/Users/example/.marginalia/vaults/LOTR",
    }

    assert ingest_quality_check._vault_matches("lotr", current)
    assert ingest_quality_check._vault_matches(
        "/users/example/.marginalia/vaults/lotr",
        current,
    )
    assert not ingest_quality_check._vault_matches("reference-corpus", current)
    assert not ingest_quality_check._vault_matches("lotr", None)


def test_build_report_can_include_compact_ledger_detail(monkeypatch) -> None:
    monkeypatch.setattr(
        ingest_quality_check,
        "graph_stats",
        lambda endpoint, vault, **kwargs: {
            "total_nodes": 0,
            "total_edges": 0,
            "node_types": [],
        },
    )
    monkeypatch.setattr(
        ingest_quality_check,
        "semantic_quality",
        lambda endpoint, vault, **kwargs: {"verdict": {"status": "incomplete"}},
    )
    monkeypatch.setattr(
        ingest_quality_check,
        "graph_overview",
        lambda endpoint, vault, **kwargs: {
            "total_nodes": 0,
            "total_edges": 0,
            "returned_nodes": 0,
            "returned_edges": 0,
            "truncated": False,
            "nodes": [],
            "edges": [],
        },
    )
    monkeypatch.setattr(
        ingest_quality_check,
        "ledger_runs",
        lambda endpoint, vault, **kwargs: {"runs": [{"run_id": "run-1"}]},
    )
    monkeypatch.setattr(
        ingest_quality_check,
        "ledger_run_detail",
        lambda endpoint, vault, run_id, **kwargs: {
            "run": {"run_id": run_id},
            "candidates": [
                {
                    "candidate_id": "node-1",
                    "candidate_kind": "node",
                    "payload": {"type": "Agent"},
                },
            ],
            "comparisons": [{"method": "curator", "verdict": "commit"}],
            "commit_plans": [],
            "commit_records": [],
        },
    )

    report = ingest_quality_check.build_report(
        "http://127.0.0.1:7777",
        "LOTR",
        include_ledger_detail=True,
    )

    assert report["ledger_detail"]["counts"] == {
        "candidates": 1,
        "candidate_rows": 1,
        "comparisons": 1,
        "commit_plans": 0,
        "commit_records": 0,
    }
    assert report["ledger_detail"]["comparison_verdicts"] == {"commit": 1}


def test_build_report_compacts_ingest_queue_last_event_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        ingest_quality_check,
        "graph_stats",
        lambda endpoint, vault, **kwargs: {
            "total_nodes": 0,
            "total_edges": 0,
            "node_types": [],
        },
    )
    monkeypatch.setattr(
        ingest_quality_check,
        "semantic_quality",
        lambda endpoint, vault, **kwargs: {"verdict": {"status": "incomplete"}},
    )
    monkeypatch.setattr(
        ingest_quality_check,
        "graph_overview",
        lambda endpoint, vault, **kwargs: {
            "total_nodes": 0,
            "total_edges": 0,
            "returned_nodes": 0,
            "returned_edges": 0,
            "truncated": False,
            "nodes": [],
            "edges": [],
        },
    )
    monkeypatch.setattr(
        ingest_quality_check,
        "ledger_runs",
        lambda endpoint, vault, **kwargs: {"runs": []},
    )
    monkeypatch.setattr(
        ingest_quality_check,
        "summarize_ingest_extraction",
        lambda endpoint, vault, queue, **kwargs: {},
    )
    raw_queue = {
        "status": "ok",
        "summary": {"active": True, "queued": 0, "processing": 1},
        "vault": {"name": "LOTR", "path": "/vaults/LOTR"},
        "items": [
            {
                "id": "item-1",
                "name": "The Fellowship.md",
                "path": "/vaults/LOTR/The Fellowship.md",
                "status": "processing",
                "stage": "committing",
                "blocks_done": 91,
                "blocks_total": 91,
                "event_count": 80,
                "last_event": {
                    "kind": "llm_request",
                    "summary": "Relation Curator LLM request",
                    "ts": 123.0,
                    "payload": {
                        "messages": [
                            {"role": "system", "content": "very long prompt"},
                            {"role": "user", "content": "very long source excerpt"},
                        ]
                    },
                },
            }
        ],
    }

    report = ingest_quality_check.build_report(
        "http://127.0.0.1:7777",
        "LOTR",
        queue=raw_queue,
    )

    queue = report["queue"]
    assert queue["summary"] == {"active": True, "queued": 0, "processing": 1}
    assert queue["items"][0]["last_event"] == {
        "kind": "llm_request",
        "summary": "Relation Curator LLM request",
        "ts": 123.0,
    }
    assert "payload" not in json.dumps(queue)
    assert "messages" not in json.dumps(queue)


def test_source_files_are_sorted_and_filter_supported_extensions(tmp_path: Path) -> None:
    (tmp_path / "z.txt").write_text("z", encoding="utf-8")
    (tmp_path / "a.md").write_text("a", encoding="utf-8")
    (tmp_path / "skip.pdf").write_text("skip", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "b.markdown").write_text("b", encoding="utf-8")

    shallow = ingest_quality_check._source_files(tmp_path, recursive=False)
    recursive = ingest_quality_check._source_files(tmp_path, recursive=True)

    assert [path.name for path in shallow] == ["a.md", "z.txt"]
    assert [path.name for path in recursive] == ["a.md", "b.markdown", "z.txt"]


def test_json_request_sends_request_scoped_vault_without_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self) -> bytes:
            return b'{"status":"ok"}'

    def fake_urlopen(request, *, timeout):
        captured["authorization"] = request.get_header("Authorization")
        captured["vault"] = request.get_header("X-okto-neuron-vault")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(ingest_quality_check, "urlopen", fake_urlopen)

    result = ingest_quality_check._json_request(
        "http://127.0.0.1:7777",
        "GET",
        "/api/v1/graph/stats",
        vault="LOTR",
        timeout=7.0,
    )

    assert result == {"status": "ok"}
    assert captured == {
        "authorization": None,
        "vault": "LOTR",
        "timeout": 7.0,
    }


def test_json_request_reports_the_timed_out_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(*unused, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(ingest_quality_check, "urlopen", fake_urlopen)

    with pytest.raises(
        RuntimeError,
        match=r"POST /api/v1/quality/semantic timed out after 90\.0s",
    ):
        ingest_quality_check._json_request(
            "http://127.0.0.1:7777",
            "POST",
            "/api/v1/quality/semantic",
            {},
            vault="LOTR",
            timeout=90.0,
        )


def test_semantic_quality_uses_the_configured_request_timeout(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_request(endpoint, method, path, payload, **kwargs):
        captured.update(endpoint=endpoint, method=method, path=path, payload=payload, **kwargs)
        return {"semantic_quality": {"verdict": {"status": "pass"}}}

    monkeypatch.setattr(ingest_quality_check, "_json_request", fake_request)

    report = ingest_quality_check.semantic_quality(
        "http://127.0.0.1:7777",
        "LOTR",
        request_timeout_s=900.0,
    )

    assert report == {"verdict": {"status": "pass"}}
    assert captured["timeout"] == 900.0


def test_ensure_vault_selects_registered_vault_without_mutating_server_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, str]] = []

    def fake_request(endpoint: str, method: str, path: str, *args, **kwargs) -> dict:
        requests.append((method, path))
        return {
            "vaults": [
                {"name": "notes", "path": "/vaults/notes"},
                {"name": "LOTR", "path": "/vaults/LOTR"},
            ]
        }

    monkeypatch.setattr(ingest_quality_check, "_json_request", fake_request)

    result = ingest_quality_check.ensure_vault("http://127.0.0.1:7777", "lotr")

    assert result == {
        "status": "ok",
        "selected": {"name": "LOTR", "path": "/vaults/LOTR"},
    }
    assert requests == [("GET", "/api/v1/vaults")]


def test_ensure_vault_rejects_unknown_vault_without_switching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        ingest_quality_check,
        "vaults",
        lambda endpoint: {"vaults": [{"name": "notes", "path": "/vaults/notes"}]},
    )

    with pytest.raises(RuntimeError, match="not registered"):
        ingest_quality_check.ensure_vault("http://127.0.0.1:7777", "missing")


def test_main_waits_for_existing_ingest_before_read_only_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    args = SimpleNamespace(
        endpoint="http://127.0.0.1:7777",
        vault="LOTR",
        reset=False,
        source=None,
        recursive=False,
        max_files=None,
        max_chars_per_file=None,
        timeout_s=123.0,
        request_timeout_s=456.0,
        poll_s=4.0,
        include_ledger_detail=False,
        domain_profile=None,
        cognitive_score_cmd=None,
        report=None,
    )
    queue = {"summary": {"active": False, "queued": 0, "processing": 0}, "items": []}

    monkeypatch.setattr(ingest_quality_check, "parse_args", lambda argv: args)
    monkeypatch.setattr(ingest_quality_check, "ensure_vault", lambda *unused: None)

    def fake_wait(endpoint: str, vault: str, *, timeout_s: float, poll_s: float) -> dict:
        calls.append("wait")
        assert (endpoint, vault, timeout_s, poll_s) == (
            "http://127.0.0.1:7777",
            "LOTR",
            123.0,
            4.0,
        )
        return queue

    def fake_build(endpoint: str, vault: str, **kwargs) -> dict:
        calls.append("audit")
        assert kwargs["queue"] is queue
        assert kwargs["request_timeout_s"] == 456.0
        return {}

    monkeypatch.setattr(ingest_quality_check, "wait_for_ingest", fake_wait)
    monkeypatch.setattr(ingest_quality_check, "build_report", fake_build)
    monkeypatch.setattr(ingest_quality_check, "check_report", lambda *unused: [])

    assert ingest_quality_check.main([]) == 0
    assert calls == ["wait", "audit"]
