from __future__ import annotations

import importlib.metadata
from pathlib import Path
import re

import pytest

from okto_neuron.core.schema import Edge, Node, Provenance
from okto_neuron.ingest.markdown import sha256_hex
from okto_neuron.store.integrity import AuditStatus, audit_graph
from okto_neuron.store.ladybug import LadybugStore
from okto_neuron.store.schema import (
    CURRENT_IDENTITY_CONTRACT_VERSION,
    DETERMINISTIC_EDGE_IDENTITY_CONTRACTS,
)


def test_ladybug_version_includes_relationship_string_checkpoint_fix() -> None:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", importlib.metadata.version("ladybug"))
    assert match is not None
    assert tuple(int(part) for part in match.groups()) >= (0, 18, 2)


def test_ladybug_audit_detects_edge_property_adjacency_divergence(tmp_path: Path) -> None:
    store = LadybugStore(tmp_path / "vault")
    try:
        for node_id in ("actual-source", "property-source", "destination"):
            store.add_node(Node(id=node_id, type="Concept", title=node_id))
        store.add_edge(
            Edge(
                id="edge-1",
                type="relates_to",
                src="actual-source",
                dst="destination",
            )
        )

        clean = audit_graph(store)
        assert clean.status is AuditStatus.VERIFIED
        assert clean.adjacency_complete is True
        assert clean.adjacency_scanned == 1

        store._execute(  # noqa: SLF001 - intentional property-only corruption fixture
            "MATCH (:Node)-[e:Edge {id: $id}]->(:Node) SET e.src = $src",
            {"id": "edge-1", "src": "property-source"},
        )

        observations = list(store.list_edge_adjacency())
        result = audit_graph(store)

        assert len(observations) == 1
        assert observations[0].edge_id == "edge-1"
        assert observations[0].edge_type == "relates_to"
        assert observations[0].actual_src == "actual-source"
        assert observations[0].stored_src == "property-source"
        assert result.status is AuditStatus.FAILED
        assert result.complete
        assert result.adjacency_scanned == 1
        assert [
            (issue.code, issue.artifact_id, issue.reference, issue.expected, issue.observed)
            for issue in result.issues
        ] == [
            (
                "adjacency_property_mismatch",
                "edge-1",
                "src",
                "actual-source",
                "property-source",
            )
        ]
    finally:
        store.close()


def test_ladybug_identical_upserts_are_semantic_noops(tmp_path: Path) -> None:
    store = LadybugStore(tmp_path / "vault")
    try:
        source = Node(id="source", type="Concept", title="Source")
        destination = Node(id="destination", type="Concept", title="Destination")
        store.add_node(source)
        store.add_node(destination)
        edge = Edge(id="edge", type="uses", src=source.id, dst=destination.id)
        store.add_edge(edge)
        epoch = store.semantic_write_epoch

        store.add_node(source.model_copy(update={"created_at": destination.created_at}))
        store.add_edge(edge)

        assert store.semantic_write_epoch == epoch
        assert store.get_node(source.id) == source

        store.add_node(source.model_copy(update={"content": "changed"}))
        assert store.semantic_write_epoch == epoch + 1
        changed = store.get_node(source.id)
        assert changed is not None
        assert changed.content == "changed"
        assert changed.created_at == source.created_at
    finally:
        store.close()


def test_ladybug_edge_payload_updates_in_place_and_survives_reopen(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    store = LadybugStore(vault_path)
    source = Node(id="source", type="Concept", title="Source")
    destination = Node(id="destination", type="Concept", title="Destination")
    store.add_node(source)
    store.add_node(destination)
    edge = Edge(id="edge", type="uses", src=source.id, dst=destination.id)
    store.add_edge(edge)
    epoch = store.semantic_write_epoch

    updated = edge.model_copy(
        update={
            "weight": 0.5,
            "provenance": Provenance(source="llm", rule_id="updated"),
        }
    )
    store.add_edge(updated)

    # One SET statement updates mutable payload. The former DELETE + CREATE
    # path consumed two writes and amplified the relationship-string checkpoint
    # corruption fixed by LadybugDB issue #658 / PR #659 in Ladybug 0.18.2.
    assert store.semantic_write_epoch == epoch + 1
    assert list(store.list_edges()) == [updated]
    store.close()

    reopened = LadybugStore(vault_path)
    try:
        assert list(reopened.list_edges()) == [updated]
        assert audit_graph(reopened).status is AuditStatus.VERIFIED
    finally:
        reopened.close()


def test_ladybug_edge_identity_rejects_endpoint_reuse(tmp_path: Path) -> None:
    store = LadybugStore(tmp_path / "vault")
    try:
        for node_id in ("source", "destination", "other"):
            store.add_node(Node(id=node_id, type="Concept", title=node_id))
        edge = Edge(id="edge", type="uses", src="source", dst="destination")
        store.add_edge(edge)

        with pytest.raises(ValueError, match="edge identity collision"):
            store.add_edge(edge.model_copy(update={"dst": "other"}))

        assert list(store.list_edges()) == [edge]
        assert audit_graph(store).status is AuditStatus.VERIFIED
    finally:
        store.close()


def test_ladybug_fresh_graph_declares_and_enforces_deterministic_identity(
    tmp_path: Path,
) -> None:
    store = LadybugStore(tmp_path / "vault")
    try:
        contract = store._graph_handle.identity_contract_version  # noqa: SLF001 - handle read
        assert contract == CURRENT_IDENTITY_CONTRACT_VERSION
        assert contract in DETERMINISTIC_EDGE_IDENTITY_CONTRACTS

        for node_id in ("claim", "block", "other"):
            store.add_node(Node(id=node_id, type="Concept", title=node_id))
        honest = Edge(
            id=sha256_hex("edge", "claim", "prov:wasDerivedFrom", "block"),
            type="prov:wasDerivedFrom",
            src="claim",
            dst="block",
        )
        store.add_edge(honest)

        clean = audit_graph(store, identity_contract_version=contract)
        assert clean.status is AuditStatus.VERIFIED

        # A content-addressed id that does not match its own (src, type, dst)
        # was previously unobservable: endpoints, adjacency, and the manifest
        # all agree, and only the identity contract proves the id is wrong.
        store.add_edge(
            Edge(
                id=sha256_hex("edge", "claim", "prov:wasDerivedFrom", "elsewhere"),
                type="prov:wasDerivedFrom",
                src="claim",
                dst="other",
            )
        )

        assert audit_graph(store).status is AuditStatus.VERIFIED
        result = audit_graph(store, identity_contract_version=contract)
        assert result.status is AuditStatus.FAILED
        assert result.complete
        assert [issue.code for issue in result.issues] == ["content_addressed_id_mismatch"]
        assert result.issues[0].expected == sha256_hex(
            "edge", "claim", "prov:wasDerivedFrom", "other"
        )
        assert honest.id in {edge.id for edge in store.list_edges()}
    finally:
        store.close()
