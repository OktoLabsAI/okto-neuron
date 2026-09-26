from __future__ import annotations

from collections.abc import Iterable

from okto_neuron.core.schema import Edge, Node
from okto_neuron.ingest.markdown import sha256_hex
from okto_neuron.store.integrity import (
    AuditStatus,
    EdgeAdjacencyObservation,
    ExpectedArtifact,
    ExpectedArtifactManifest,
    ScanCompleteness,
    audit_graph,
)
from okto_neuron.store.memory import InMemoryStore
from okto_neuron.store.schema import (
    CURRENT_IDENTITY_CONTRACT_VERSION,
    DETERMINISTIC_EDGE_IDENTITY_CONTRACTS,
    LEGACY_IDENTITY_CONTRACT_VERSION,
)


def _node(node_id: str, type_: str = "Concept", **facets: object) -> Node:
    return Node(id=node_id, type=type_, facets=dict(facets))


def _clean_store() -> InMemoryStore:
    store = InMemoryStore()
    for node in (
        _node("subject", "Agent"),
        _node("object"),
        _node("block", "Block"),
        _node("activity", "Activity"),
        _node("agent", "Agent"),
        _node(
            "claim",
            "Claim",
            S_id="subject",
            O_id="object",
            block_id="block",
            extraction_activity_id="activity",
            agent_id="agent",
        ),
    ):
        store.add_node(node)
    store.add_edge(Edge(id="edge", type="relates_to", src="subject", dst="object"))
    return store


def test_clean_complete_graph_is_verified() -> None:
    result = audit_graph(
        _clean_store(),
        graph_generation="generation-1",
        identity_contract_version="legacy",
    )

    assert result.status is AuditStatus.VERIFIED
    assert result.verified
    assert result.complete
    assert result.nodes_scanned == 6
    assert result.edges_scanned == 1
    assert result.issue_count == 0
    assert result.duration_ms >= 0
    assert result.graph_generation == "generation-1"


def test_dangling_edge_endpoint_and_claim_references_fail_deterministically() -> None:
    store = _clean_store()
    store._edges["dangling"] = Edge(  # noqa: SLF001 - intentional corrupt fixture
        id="dangling", type="broken", src="missing-source", dst="subject"
    )
    store.add_node(
        _node(
            "broken-claim",
            "Claim",
            S_id="missing-subject",
            O_id="missing-object",
            block_id="block",
        )
    )

    result = audit_graph(store)

    assert result.status is AuditStatus.FAILED
    assert [(issue.code, issue.artifact_id, issue.reference) for issue in result.issues] == [
        ("missing_edge_endpoint", "dangling", "src"),
        ("missing_facet_reference", "broken-claim", "O_id"),
        ("missing_facet_reference", "broken-claim", "S_id"),
    ]


def test_expected_artifact_manifest_checks_presence_type_and_endpoints() -> None:
    store = _clean_store()
    expected = ExpectedArtifactManifest(
        artifacts=(
            ExpectedArtifact(kind="node", id="claim", type="Claim"),
            ExpectedArtifact(kind="node", id="missing-node", type="Concept"),
            ExpectedArtifact(
                kind="edge",
                id="edge",
                type="wrong_type",
                src="object",
                dst="subject",
            ),
        )
    )

    result = audit_graph(store, expected=expected)

    assert result.status is AuditStatus.FAILED
    assert result.expected_artifacts_checked == 3
    assert {(issue.code, issue.artifact_id, issue.reference) for issue in result.issues} == {
        ("expected_artifact_endpoint_mismatch", "edge", "dst"),
        ("expected_artifact_endpoint_mismatch", "edge", "src"),
        ("expected_artifact_type_mismatch", "edge", "type"),
        ("missing_expected_artifact", "missing-node", ""),
    }


def test_identical_expected_artifacts_are_one_check_but_conflicts_fail() -> None:
    result = audit_graph(
        _clean_store(),
        expected=(
            ExpectedArtifact(kind="edge", id="edge", type="relates_to"),
            ExpectedArtifact(kind="edge", id="edge", type="relates_to"),
            ExpectedArtifact(kind="edge", id="edge", type="contradicts"),
        ),
    )

    assert result.status is AuditStatus.FAILED
    assert result.expected_artifacts_checked == 1
    assert {issue.code for issue in result.issues} == {
        "conflicting_expected_artifact",
        "expected_artifact_type_mismatch",
    }


def test_declared_partial_population_cannot_verify() -> None:
    result = audit_graph(
        _clean_store(),
        scan=ScanCompleteness(nodes=True, edges=False, incomplete_reason="next page unavailable"),
    )

    assert result.status is AuditStatus.INCOMPLETE
    assert not result.complete
    assert result.nodes_complete
    assert not result.edges_complete
    assert result.incomplete_reasons == ("next page unavailable",)
    assert result.issues[0].code == "population_incomplete"


def test_incomplete_manifest_cannot_verify() -> None:
    result = audit_graph(
        _clean_store(),
        expected=ExpectedArtifactManifest(
            artifacts=(ExpectedArtifact(kind="node", id="claim", type="Claim"),),
            complete=False,
            incomplete_reason="receipt page missing",
        ),
    )

    assert result.status is AuditStatus.INCOMPLETE
    assert result.manifest_complete is False
    assert result.incomplete_reasons == ("receipt page missing",)


def test_manifest_iteration_error_is_incomplete_not_an_exception() -> None:
    def expected_artifacts() -> Iterable[ExpectedArtifact]:
        yield ExpectedArtifact(kind="node", id="claim", type="Claim")
        raise RuntimeError("receipt page failed")

    result = audit_graph(_clean_store(), expected=expected_artifacts())

    assert result.status is AuditStatus.INCOMPLETE
    assert result.expected_artifacts_checked == 1
    assert result.incomplete_reasons == (
        "expected-artifact scan failed: RuntimeError: receipt page failed",
    )


class _FailingEdgeScanStore(InMemoryStore):
    def list_edges(
        self,
        src: str | None = None,
        dst: str | None = None,
        type: str | None = None,
    ) -> Iterable[Edge]:
        del src, dst, type
        yield Edge(id="prefix", type="relates_to", src="subject", dst="object")
        raise RuntimeError("page two failed")


class _FailingNodeScanStore(_FailingEdgeScanStore):
    def list_nodes(self, type: str | None = None) -> Iterable[Node]:
        del type
        yield _node("claim", "Claim", S_id="later-subject")
        raise RuntimeError("node page two failed")


class _FailingAdjacencyScanStore(InMemoryStore):
    def list_edge_adjacency(self) -> Iterable[EdgeAdjacencyObservation]:
        yield EdgeAdjacencyObservation(
            edge_id="edge",
            edge_type="relates_to",
            actual_src="subject",
            actual_dst="object",
            stored_src="subject",
            stored_dst="object",
        )
        raise RuntimeError("adjacency page two failed")


def test_iteration_error_preserves_counts_but_reports_incomplete() -> None:
    base = _clean_store()
    store = _FailingEdgeScanStore()
    for node in base.list_nodes():
        store.add_node(node)

    result = audit_graph(store)

    assert result.status is AuditStatus.INCOMPLETE
    assert result.nodes_scanned == 6
    assert result.edges_scanned == 1
    assert not result.edges_complete
    assert result.issue_count == 1
    assert result.issues[0].code == "edge_scan_error"
    assert result.issues[0].observed == "RuntimeError: page two failed"


def test_observed_mismatch_remains_failed_when_later_scan_is_incomplete() -> None:
    store = _FailingEdgeScanStore()
    store.add_node(_node("subject"))

    result = audit_graph(store)

    assert result.status is AuditStatus.FAILED
    assert not result.complete
    assert {issue.code for issue in result.issues} == {
        "edge_scan_error",
        "missing_edge_endpoint",
    }


def test_unseen_pages_do_not_turn_unproven_absence_into_failure() -> None:
    result = audit_graph(
        _FailingNodeScanStore(),
        expected=ExpectedArtifactManifest(
            artifacts=(
                ExpectedArtifact(kind="node", id="later-node"),
                ExpectedArtifact(kind="edge", id="later-edge"),
            )
        ),
    )

    assert result.status is AuditStatus.INCOMPLETE
    assert result.nodes_scanned == 1
    assert result.edges_scanned == 1
    assert {issue.code for issue in result.issues} == {
        "edge_scan_error",
        "node_scan_error",
    }


def test_optional_adjacency_iteration_error_is_incomplete() -> None:
    base = _clean_store()
    store = _FailingAdjacencyScanStore()
    for node in base.list_nodes():
        store.add_node(node)
    for edge in base.list_edges():
        store.add_edge(edge)

    result = audit_graph(store)

    assert result.status is AuditStatus.INCOMPLETE
    assert result.adjacency_complete is False
    assert result.adjacency_scanned == 1
    assert result.issues[0].code == "adjacency_scan_error"


def _deterministic_edge(src: str, type_: str, dst: str) -> Edge:
    return Edge(id=sha256_hex("edge", src, type_, dst), type=type_, src=src, dst=dst)


def test_deterministic_identity_checks_are_gated_on_the_declared_contract() -> None:
    store = _clean_store()
    store.add_edge(_deterministic_edge("subject", "prov:wasDerivedFrom", "block"))
    corrupt = _deterministic_edge("subject", "rdf:subject", "object")
    store._edges[corrupt.id] = corrupt.model_copy(  # noqa: SLF001 - corrupt fixture
        update={"dst": "activity"}
    )

    legacy = audit_graph(store, identity_contract_version=LEGACY_IDENTITY_CONTRACT_VERSION)
    unset = audit_graph(store)
    current = audit_graph(store, identity_contract_version=CURRENT_IDENTITY_CONTRACT_VERSION)

    assert CURRENT_IDENTITY_CONTRACT_VERSION == "semantic_edges.v1"
    assert CURRENT_IDENTITY_CONTRACT_VERSION in DETERMINISTIC_EDGE_IDENTITY_CONTRACTS
    assert legacy.status is AuditStatus.VERIFIED
    assert unset.status is AuditStatus.VERIFIED
    assert current.status is AuditStatus.FAILED
    assert [(issue.code, issue.artifact_id, issue.expected) for issue in current.issues] == [
        (
            "content_addressed_id_mismatch",
            corrupt.id,
            sha256_hex("edge", "subject", "rdf:subject", "activity"),
        )
    ]


def test_deterministic_identity_ignores_random_ids_and_verifies_clean_edges() -> None:
    store = _clean_store()
    for edge in (
        _deterministic_edge("claim", "prov:wasAttributedTo", "agent"),
        _deterministic_edge("subject", "relates_to", "object"),
        Edge(type="relates_to", src="subject", dst="block"),
        # A hand-assigned id was never content-addressed, so the contract makes
        # no promise about it even for a deterministic-family edge type.
        Edge(id="e1", type="prov:wasDerivedFrom", src="claim", dst="block"),
    ):
        store.add_edge(edge)

    result = audit_graph(store, identity_contract_version=CURRENT_IDENTITY_CONTRACT_VERSION)

    assert result.status is AuditStatus.VERIFIED
    assert result.edges_scanned == 5


def test_two_ids_claiming_one_deterministic_identity_fail() -> None:
    store = _clean_store()
    honest = _deterministic_edge("subject", "supersedes", "object")
    store.add_edge(honest)
    impostor = sha256_hex("edge", "subject", "supersedes", "block")
    store._edges[impostor] = honest.model_copy(  # noqa: SLF001 - corrupt fixture
        update={"id": impostor}
    )

    result = audit_graph(store, identity_contract_version=CURRENT_IDENTITY_CONTRACT_VERSION)

    assert result.status is AuditStatus.FAILED
    assert sorted((issue.code, issue.artifact_id) for issue in result.issues) == sorted(
        [
            ("conflicting_deterministic_identity", honest.id),
            ("conflicting_deterministic_identity", impostor),
            ("content_addressed_id_mismatch", impostor),
        ]
    )


def test_full_audit_catches_corruption_outside_the_current_plan_manifest() -> None:
    store = _clean_store()
    untouched = _deterministic_edge("subject", "schema:mentions", "block")
    store._edges[untouched.id] = untouched.model_copy(  # noqa: SLF001 - corrupt fixture
        update={"dst": "missing-node"}
    )

    result = audit_graph(
        store,
        expected=ExpectedArtifactManifest(
            artifacts=(ExpectedArtifact(kind="edge", id="edge", type="relates_to"),)
        ),
        identity_contract_version=CURRENT_IDENTITY_CONTRACT_VERSION,
    )

    assert result.status is AuditStatus.FAILED
    assert result.expected_artifacts_checked == 1
    assert {(issue.code, issue.artifact_id) for issue in result.issues} == {
        ("content_addressed_id_mismatch", untouched.id),
        ("missing_edge_endpoint", untouched.id),
    }


# ── confidentiality ───────────────────────────────────────────────────────────
_SECRET = "SUPERSECRET-hunter2-canary"


def _secret_bearing_store() -> InMemoryStore:
    """A store whose node/edge *content* carries a marker, and which is corrupt
    enough that every issue-bearing code path is exercised."""
    store = InMemoryStore()
    for node in (
        Node(id="subject", type="Agent", title=f"Alice {_SECRET}", content=f"bio {_SECRET}"),
        Node(id="object", type="Concept", title=_SECRET, content=f"note {_SECRET}"),
        Node(id="block", type="Block", title="Block", content=f"raw source line {_SECRET}"),
        Node(id="activity", type="Activity", title=_SECRET),
        Node(id="agent", type="Agent", title=_SECRET),
        Node(
            id="claim",
            type="Claim",
            title=f"Alice knows {_SECRET}",
            content=f"Alice knows {_SECRET}",
            facets={
                "S_id": "subject",
                "O_id": "missing-object",
                "O_literal": _SECRET,
                "block_id": "block",
                "extraction_activity_id": "activity",
                "agent_id": "agent",
            },
        ),
    ):
        store.add_node(node)
    # dangling endpoint + non-deterministic id, both issue-producing
    store._edges["dangling"] = Edge(  # noqa: SLF001 - intentional corrupt fixture
        id="dangling", type="relates_to", src="missing-source", dst="subject"
    )
    return store


def test_integrity_summary_never_echoes_node_or_edge_content() -> None:
    """Integrity summaries are diagnostics that travel to logs and the HTTP
    surface. They may name ids, types, and facet *keys* — never the secret
    values or source excerpts stored in node/edge content."""
    import dataclasses

    result = audit_graph(
        _secret_bearing_store(),
        expected=ExpectedArtifactManifest(
            artifacts=(
                ExpectedArtifact(kind="node", id="missing-node", type="Concept"),
                ExpectedArtifact(kind="edge", id="dangling", type="other", src="a", dst="b"),
            )
        ),
        graph_generation="generation-secret-test",
        identity_contract_version=CURRENT_IDENTITY_CONTRACT_VERSION,
    )

    # the fixture must actually produce diagnostics, otherwise this is vacuous
    assert result.status is AuditStatus.FAILED
    assert result.issue_count > 0
    assert any(issue.observed or issue.expected for issue in result.issues)

    rendered = repr(dataclasses.asdict(result))
    assert _SECRET not in rendered
    for issue in result.issues:
        for field in (
            issue.code,
            issue.artifact_id,
            issue.reference,
            issue.expected,
            issue.observed,
        ):
            assert _SECRET not in field
