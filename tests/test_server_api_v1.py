"""HTTP tests for the web-UI surface: /api/v1/* (KG browser, config, query).

KG-browser tests populate a real InMemory-backed vault directly (no LLM) so node
listing, type filtering, and byte-range provenance are deterministic and offline.
Config tests round-trip okto-neuron.yaml. Query tests pin the companion to StubLLM.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest
import yaml
from starlette.testclient import TestClient

from okto_neuron import Vault
from okto_neuron._internal.infra import INFRA_FACET
from okto_neuron.companion import Companion
from okto_neuron.config._vault import DEFAULT_CURATION_CALL_TIMEOUT_S
from okto_neuron.consolidate import NodeCandidate
from okto_neuron.consolidate.ledger import CandidateLedger
from okto_neuron.core.schema import Edge, Node
from okto_neuron.llm import StubLLM
from okto_neuron.predicates import (
    PredicateDecisionProvenance,
    PredicateRecord,
    PredicateRegistry,
)
from okto_neuron.server import http as http_mod
from okto_neuron.server.http import CLOSED_NODE_TYPES, build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests
from okto_neuron.server.state import get_server_state
from okto_neuron.store import vault as vault_module
from okto_neuron.store.integrity import AuditStatus
from okto_neuron.store.integrity_state import (
    GraphIntegrityState,
    integrity_state_path,
    write_integrity_state,
)
from okto_neuron.store.ladybug import VaultConnection


def _add_literal_claim(store, *, claim_id: str, predicate: str) -> None:
    store.add_node(
        Node(
            id=claim_id,
            type="Claim",
            title=f"claim using {predicate}",
            facets={
                "S_id": "concept:1",
                "P": predicate,
                "O_literal": f"literal for {predicate}",
                "block_id": "block:1",
                "document_id": "doc:1",
                "extraction_activity_id": "act:1",
                "agent_id": "agent:1",
            },
        )
    )
    edge_prefix = claim_id.replace(":", "-")
    store.add_edge(
        Edge(
            id=f"{edge_prefix}-derived",
            type="prov:wasDerivedFrom",
            src=claim_id,
            dst="block:1",
        )
    )
    store.add_edge(
        Edge(
            id=f"{edge_prefix}-broader",
            type="skos:broader",
            src="concept:1",
            dst=claim_id,
        )
    )
    store.add_edge(
        Edge(
            id=f"{edge_prefix}-subject",
            type="rdf:subject",
            src=claim_id,
            dst="concept:1",
        )
    )
    store.add_edge(
        Edge(
            id=f"{edge_prefix}-generated",
            type="prov:wasGeneratedBy",
            src=claim_id,
            dst="act:1",
        )
    )
    store.add_edge(
        Edge(
            id=f"{edge_prefix}-attributed",
            type="prov:wasAttributedTo",
            src=claim_id,
            dst="agent:1",
        )
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # The registry, managed credentials, and daemon state must never discover or
    # mutate the developer's real HOME during an API test.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.delenv("OKTO_NEURON_VAULT", raising=False)
    reset_state_for_tests()
    vault = Vault.init(tmp_path / "v")
    note = Path(vault.path) / "note.md"
    note.write_text("# Title\n\nbody about knowledge graphs.\n", encoding="utf-8")

    # Populate the store directly with a closed-schema fixture graph.
    store = vault.store
    block = Node(
        id="block:1",
        type="Block",
        title="block one",
        facets={
            "source_path": str(note),
            "byte_start": 10,
            "byte_end": 42,
            "content_hash": "a" * 64,
            "document_id": "doc:1",
        },
    )
    claim = Node(
        id="claim:1",
        type="Claim",
        title="turing worked at bletchley",
        facets={
            "S_id": "concept:1",
            "P": "describes",
            "O_literal": "Turing worked at Bletchley Park",
            "block_id": "block:1",
            "document_id": "doc:1",
            "extraction_activity_id": "act:1",
            "agent_id": "agent:1",
        },
    )
    concept = Node(id="concept:1", type="Concept", title="knowledge graph")
    agent = Node(id="agent:1", type="Agent", title="extractor", facets=dict(INFRA_FACET))
    activity = Node(id="act:1", type="Activity", title="extraction", facets=dict(INFRA_FACET))
    schema_meta = Node(id="__meta__", type="SchemaMetadata", title="internal")
    for n in (block, claim, concept, agent, activity, schema_meta):
        store.add_node(n)
    store.add_edge(Edge(id="e1", type="prov:wasDerivedFrom", src="claim:1", dst="block:1"))
    store.add_edge(Edge(id="e2", type="skos:broader", src="concept:1", dst="claim:1"))
    store.add_edge(Edge(id="e3", type="rdf:subject", src="claim:1", dst="concept:1"))
    store.add_edge(Edge(id="e4", type="prov:wasGeneratedBy", src="claim:1", dst="act:1"))
    store.add_edge(Edge(id="e5", type="prov:wasAttributedTo", src="claim:1", dst="agent:1"))
    PredicateRegistry(vault.path).seed_builtins()

    monkeypatch.setattr(
        http_mod, "_companion", lambda state: Companion(state.vault, provider=StubLLM())
    )

    state = init_state(vault, vault.path)
    app = build_rest_app(state)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.vault_path = str(vault.path)  # type: ignore[attr-defined]
        yield c
    reset_state_for_tests()
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


# ── KG browser ───────────────────────────────────────────────────────────────


def test_nodes_list_ok(client: TestClient) -> None:
    r = client.get("/api/v1/nodes")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert {"total", "limit", "offset", "nodes"} <= set(body)
    types = {n["type"] for n in body["nodes"]}
    assert "SchemaMetadata" not in types  # internal nodes hidden
    assert {"Block", "Claim", "Concept"} <= types


def test_structural_claims_hidden_from_browse_unless_requested(
    client: TestClient,
) -> None:
    """has_heading/has_tag/links_to Claims are deterministic document structure —
    a third of every Claim page — so browse hides them unless asked. They stay in
    the graph and a direct link to one still resolves."""
    state = get_server_state()
    for cid, pred in (
        ("claim:heading", "has_heading"),
        ("claim:tag", "has_tag"),
        ("claim:link", "links_to"),
    ):
        _add_literal_claim(state.vault.store, claim_id=cid, predicate=pred)

    hidden_ids = {"claim:heading", "claim:tag", "claim:link"}

    default = client.get("/api/v1/nodes?type=Claim&limit=500")
    assert default.status_code == 200, default.text
    default_ids = {n["id"] for n in default.json()["nodes"]}
    assert not (default_ids & hidden_ids)
    assert "claim:1" in default_ids  # a real Claim is untouched

    opted_in = client.get("/api/v1/nodes?type=Claim&limit=500&include_structural=1")
    assert opted_in.status_code == 200, opted_in.text
    opted_ids = {n["id"] for n in opted_in.json()["nodes"]}
    assert hidden_ids <= opted_ids
    assert opted_in.json()["total"] == default.json()["total"] + len(hidden_ids)

    # Counts follow the same rule, so the sidebar total matches the list.
    counts = {t["name"]: t["count"] for t in client.get("/api/v1/node-types").json()["types"]}
    counts_in = {
        t["name"]: t["count"]
        for t in client.get("/api/v1/node-types?include_structural=1").json()["types"]
    }
    assert counts_in["Claim"] == counts["Claim"] + len(hidden_ids)
    # Only Claims are affected — entity types must not move.
    for t in ("Concept", "Block", "Document", "InformationObject", "Agent", "Place"):
        assert counts.get(t, 0) == counts_in.get(t, 0), t

    # Hidden from the list, but still a real node behind a direct link.
    detail = client.get("/api/v1/nodes/claim:heading")
    assert detail.status_code == 200, detail.text


def test_add_same_basename_different_directories_mints_distinct_document_ids(
    client: TestClient,
) -> None:
    """CLI-level regression for review 3.4-e2e: ``okto-neuron add
    notes/a/README.md`` then ``okto-neuron add notes/b/README.md`` (different
    content, same basename) used to both materialize to
    ``.marginalia/sources/README.md`` — the second POST silently overwrote the
    first file's bytes, and both documents then hashed that one shared,
    resolved path into the same ``document_id`` in
    ``ingest/markdown.py::parse_markdown``. Runs the exact route the CLI
    ``okto-neuron add`` command uses, against the fixture's real (non-stub)
    Ladybug-backed vault, so this proves the fix end to end: distinct target
    files on disk, distinct document ids, and both documents independently
    retrievable afterward.
    """
    r_a = client.post(
        "/add",
        json={"path": "notes/a/README.md", "content": "# A\n\ncontent about apples.\n"},
    )
    r_b = client.post(
        "/add",
        json={"path": "notes/b/README.md", "content": "# B\n\ncontent about bananas.\n"},
    )
    assert r_a.status_code == 200, r_a.text
    assert r_b.status_code == 200, r_b.text
    doc_id_a = r_a.json()["document_id"]
    doc_id_b = r_b.json()["document_id"]
    assert doc_id_a != doc_id_b

    vault_path = Path(client.vault_path)  # type: ignore[attr-defined]
    target_a = vault_path / ".marginalia" / "sources" / "notes" / "a" / "README.md"
    target_b = vault_path / ".marginalia" / "sources" / "notes" / "b" / "README.md"
    assert target_a.read_text(encoding="utf-8") == "# A\n\ncontent about apples.\n"
    assert target_b.read_text(encoding="utf-8") == "# B\n\ncontent about bananas.\n"

    runtime = get_server_state().active_runtime
    assert runtime is not None
    assert runtime.vault.store.get_node(doc_id_a) is not None
    assert runtime.vault.store.get_node(doc_id_b) is not None


def test_remember_outside_root_is_a_403_that_names_the_roots(client: TestClient, tmp_path: Path) -> None:
    """Field report, REST side: same wording as the MCP tool."""
    outside = tmp_path / "elsewhere.md"
    outside.write_text("# elsewhere\n", encoding="utf-8")

    response = client.post("/remember", json={"source": str(outside)})

    assert response.status_code == 403, response.text
    body = response.json()
    assert body["error"] == "forbidden"
    detail = body.get("detail") or body.get("message") or ""
    assert "refusing to remember source outside the vault and watch roots" in detail
    assert "Allowed roots" in detail and "folder_watch.roots" in detail


def test_graph_integrity_audit_verifies_current_generation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = get_server_state().active_runtime
    assert runtime is not None
    before = client.get("/api/v1/graph/integrity")
    assert before.status_code == 200, before.text
    assert before.json()["integrity"]["status"] == "unverified"

    original_guard = runtime.vault._integrity_scan_guard  # noqa: SLF001
    original_audit = http_mod.graph_integrity.run_audit
    original_summary = http_mod.graph_integrity.summary
    scan_active = False

    @contextmanager
    def tracked_guard():
        nonlocal scan_active
        with original_guard():
            scan_active = True
            try:
                yield
            finally:
                scan_active = False

    def tracked_audit(*args, **kwargs):
        assert scan_active
        assert runtime.writer_lock.locked()
        return original_audit(*args, **kwargs)

    def tracked_summary(*args, **kwargs):
        assert scan_active
        assert runtime.writer_lock.locked()
        return original_summary(*args, **kwargs)

    monkeypatch.setattr(runtime.vault, "_integrity_scan_guard", tracked_guard)
    monkeypatch.setattr(http_mod.graph_integrity, "run_audit", tracked_audit)
    monkeypatch.setattr(http_mod.graph_integrity, "summary", tracked_summary)

    audited = client.post("/api/v1/graph/integrity", json={})
    assert audited.status_code == 200, audited.text
    integrity = audited.json()["integrity"]
    assert integrity["status"] == "verified"
    assert integrity["writer_fenced"] is False
    assert integrity["last_audit"]["adjacency_complete"] is True


@pytest.mark.asyncio
async def test_cancelled_integrity_request_holds_writer_lock_until_audit_finishes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading

    runtime = get_server_state().active_runtime
    assert runtime is not None
    started = threading.Event()
    release = threading.Event()

    def blocking_audit(_runtime) -> dict[str, object]:
        assert runtime.writer_lock.locked()
        started.set()
        assert release.wait(timeout=5)
        assert runtime.writer_lock.locked()
        return {"status": "verified"}

    monkeypatch.setattr(http_mod, "get_state", lambda: runtime)
    monkeypatch.setattr(http_mod, "remote_config_allowed", lambda _request: True)
    monkeypatch.setattr(http_mod, "_fresh_graph_integrity_report", blocking_audit)

    request_task = asyncio.create_task(http_mod.api_graph_integrity_run(SimpleNamespace()))
    assert await asyncio.to_thread(started.wait, 2)
    request_task.cancel()
    await asyncio.sleep(0)
    assert runtime.writer_lock.locked()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await request_task
    assert not runtime.writer_lock.locked()


def test_semantic_quality_scans_complete_store_and_uses_integrity_generation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http_mod, "GRAPH_MAX_LIMIT", 1)
    runtime = get_server_state().active_runtime
    assert runtime is not None
    fingerprints = http_mod._effective_semantic_fingerprints(runtime)
    ledger = CandidateLedger(Path(client.vault_path) / ".marginalia")  # type: ignore[attr-defined]
    run_id = ledger.start_run(
        document_id="semantic-snapshot-document",
        source="semantic-snapshot.md",
        blocks_total=1,
        model="fixture",
        config_fingerprint=fingerprints.config_fingerprint,
        extraction_fingerprint=fingerprints.extraction_fingerprint,
        semantic_policy_fingerprint=fingerprints.semantic_policy_fingerprint,
    )
    ledger.finish_run(
        run_id,
        state="completed",
        summary={},
        post_semantic_policy_fingerprint=fingerprints.semantic_policy_fingerprint,
    )
    overview = client.get("/api/v1/graph?limit=100")
    assert overview.status_code == 200, overview.text
    assert overview.json()["truncated"] is True

    response = client.post("/api/v1/quality/semantic", json={})
    assert response.status_code == 200, response.text
    report = response.json()["semantic_quality"]
    assert report["schema_version"] == "semantic_quality.v1"
    assert report["population"]["nodes"] == 3
    assert report["evidence"]["complete"] is True
    assert report["evidence"]["integrity_status"] == "verified"
    assert report["evidence"]["integrity_freshness"] == "fresh"
    assert report["evidence"]["technical_integrity_verified"] is True
    assert report["evidence"]["topology_evidence_status"] == "measured"
    assert report["evidence"]["authoritative"] is False
    assert report["verdict"]["status"] == "incomplete"
    snapshot = report["semantic_snapshot"]
    assert snapshot["status"] == "measured"
    assert snapshot["graph_generation"] == report["evidence"]["graph_generation"]
    assert snapshot["fingerprints"]["status"] == "measured"
    assert snapshot["fingerprints"]["config"].startswith("sha256:")
    assert snapshot["fingerprints"]["extraction"].startswith("sha256:")
    governance = client.get("/api/v1/quality/semantic/governance")
    assert governance.status_code == 200, governance.text
    assert (
        snapshot["fingerprints"]["semantic_policy"]
        == governance.json()["current_semantic_policy_fingerprint"]
    )

    assert report["evidence"]["authoritative"] is False
    assert (
        report["evidence"]["reason"]
        == "ADR 0039 has not published an authoritative semantic-baseline generation"
    )
    registered = next(
        row for row in report["hard_invariants"]["checks"] if row["code"] == "registered_predicates"
    )
    assert registered["status"] == "passed"
    assert report["layers"]["predicate"]["registry_coverage"] == {
        "status": "measured",
        "reason": None,
        "unregistered_count": 0,
        "unregistered_samples": [],
        "excluded_structural_predicates": [],
    }
    assert report["verdict"]["status"] == "incomplete"


def test_semantic_governance_has_no_rebuild_without_observed_run(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/quality/semantic/governance")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["current_semantic_policy_fingerprint"].startswith("sha256:")
    assert body["observed_semantic_policy_fingerprints"] == []
    assert body["observed_run_count"] == 0
    assert body["runs_without_fingerprint"] == 0
    assert body["latest_applied_run_count"] == 0
    assert body["latest_applied_runs_without_fingerprint"] == 0
    assert body["superseded_completed_run_count"] == 0
    assert body["rebuild_required"] is False
    assert body["predicate_registry"]["counts"]["total"] == len(
        body["predicate_registry"]["records"]
    )
    assert body["identity_decisions"] == {
        "counts": {
            "type_correction": 0,
            "distinct": 0,
            "ambiguous_review": 0,
            "total": 0,
        },
        "records": [],
    }


def test_semantic_governance_reports_policy_drift_and_governed_records(
    client: TestClient,
) -> None:
    from okto_neuron.reconcile import TypeCorrection

    vault_path = Path(client.vault_path)  # type: ignore[attr-defined]
    registry = PredicateRegistry(vault_path)
    provisional = PredicateRecord(
        label="observed_near",
        lifecycle="provisional",
        definition="The subject was observed near the stated object.",
        direction="subject_to_object",
        symmetric=False,
        signatures=(),
        support_count=0,
        samples=(),
        confidence=0.8,
        provenance=PredicateDecisionProvenance(
            source="human",
            decision_id="test-observed-near-governance",
            judge_model="",
            prompt_version="",
            semantic_policy_fingerprint="test-policy-v1",
            created_at="2026-07-17T00:00:00Z",
        ),
    )
    registry.upsert(provisional)

    state = get_server_state().active_runtime
    assert state is not None
    _curation = http_mod._curation
    _curation.identity_decision_index(state).append(
        TypeCorrection(
            decision_id="type-correction-1",
            candidate_id="candidate:1",
            previous_type="Concept",
            corrected_type="Agent",
            reason="The candidate names an actor.",
            evidence={
                "excerpt": "hidden source excerpt",
                "api_key": "hidden-secret",
                "source_id": "block:1",
            },
            judge_model="fixture-judge",
            prompt_version="identity.v1",
            semantic_policy_fingerprint="test-policy-v1",
            created_at="2026-07-17T00:00:00Z",
        )
    )

    initial = client.get("/api/v1/quality/semantic/governance")
    assert initial.status_code == 200, initial.text
    current = initial.json()["current_semantic_policy_fingerprint"]

    ledger = CandidateLedger(vault_path / ".marginalia")
    matching_run = ledger.start_run(
        document_id="doc:shared",
        source="matching.md",
        blocks_total=1,
        model="fixture",
        semantic_policy_fingerprint=current,
    )
    ledger.finish_run(matching_run, state="completed", summary={})
    drifted_fingerprint = f"sha256:{'f' * 64}"
    drifted_run = ledger.start_run(
        document_id="doc:shared",
        source="drifted.md",
        blocks_total=1,
        model="fixture",
        semantic_policy_fingerprint=drifted_fingerprint,
    )
    ledger.finish_run(drifted_run, state="completed", summary={})
    legacy_run = ledger.start_run(
        document_id="doc:legacy",
        source="legacy.md",
        blocks_total=1,
        model="fixture",
    )
    ledger.finish_run(legacy_run, state="completed", summary={})
    failed_run = ledger.start_run(
        document_id="doc:shared",
        source="failed.md",
        blocks_total=1,
        model="fixture",
        semantic_policy_fingerprint=f"sha256:{'d' * 64}",
    )
    ledger.finish_run(failed_run, state="failed", summary={})
    ledger.start_run(
        document_id="doc:shared",
        source="still-running.md",
        blocks_total=1,
        model="fixture",
        semantic_policy_fingerprint=f"sha256:{'c' * 64}",
    )
    with ledger.path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "ledger_version": 999,
                    "kind": "ingest_run",
                    "state": "started",
                    "run_id": "future-run",
                    "semantic_policy_fingerprint": f"sha256:{'e' * 64}",
                }
            )
            + "\n"
        )

    response = client.get("/api/v1/quality/semantic/governance")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_semantic_policy_fingerprint"] == current
    assert body["observed_run_count"] == 2
    assert body["runs_without_fingerprint"] == 1
    assert body["latest_applied_run_count"] == 2
    assert body["latest_applied_runs_without_fingerprint"] == 1
    assert body["superseded_completed_run_count"] == 1
    assert body["rebuild_required"] is True
    observed = {row["fingerprint"]: row for row in body["observed_semantic_policy_fingerprints"]}
    assert observed[current] == {
        "fingerprint": current,
        "run_count": 1,
        "run_ids": [matching_run],
        "latest_applied_run_count": 0,
    }
    assert observed[drifted_fingerprint] == {
        "fingerprint": drifted_fingerprint,
        "run_count": 1,
        "run_ids": [drifted_run],
        "latest_applied_run_count": 1,
    }

    registry_payload = body["predicate_registry"]
    assert registry_payload["counts"]["provisional"] == 1
    assert registry_payload["counts"]["total"] == (registry_payload["counts"]["canonical"] + 1)
    assert provisional.to_json() in registry_payload["records"]

    decisions = body["identity_decisions"]
    assert decisions["counts"] == {
        "type_correction": 1,
        "distinct": 0,
        "ambiguous_review": 0,
        "total": 1,
    }
    assert decisions["records"] == [
        {
            "kind": "type_correction",
            "decision_id": "type-correction-1",
            "candidate_id": "candidate:1",
            "previous_type": "Concept",
            "corrected_type": "Agent",
            "reason": "The candidate names an actor.",
            "judge_model": "fixture-judge",
            "prompt_version": "identity.v1",
            "semantic_policy_fingerprint": "test-policy-v1",
            "created_at": "2026-07-17T00:00:00Z",
        }
    ]
    assert "hidden source excerpt" not in response.text
    assert "hidden-secret" not in response.text


def test_semantic_governance_uses_latest_completed_run_per_document(
    client: TestClient,
) -> None:
    vault_path = Path(client.vault_path)  # type: ignore[attr-defined]
    initial = client.get("/api/v1/quality/semantic/governance")
    assert initial.status_code == 200, initial.text
    current = initial.json()["current_semantic_policy_fingerprint"]

    ledger = CandidateLedger(vault_path / ".marginalia")
    drifted_fingerprint = f"sha256:{'f' * 64}"
    old_run = ledger.start_run(
        document_id="doc:shared",
        source="old.md",
        blocks_total=1,
        model="fixture",
        semantic_policy_fingerprint=drifted_fingerprint,
    )
    ledger.finish_run(old_run, state="completed", summary={})
    current_run = ledger.start_run(
        document_id="doc:shared",
        source="current.md",
        blocks_total=1,
        model="fixture",
        semantic_policy_fingerprint=current,
    )
    ledger.finish_run(current_run, state="completed", summary={})
    failed_run = ledger.start_run(
        document_id="doc:shared",
        source="failed.md",
        blocks_total=1,
        model="fixture",
        semantic_policy_fingerprint=drifted_fingerprint,
    )
    ledger.finish_run(failed_run, state="failed", summary={})
    ledger.start_run(
        document_id="doc:shared",
        source="running.md",
        blocks_total=1,
        model="fixture",
        semantic_policy_fingerprint=drifted_fingerprint,
    )

    response = client.get("/api/v1/quality/semantic/governance")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["rebuild_required"] is False
    assert body["observed_run_count"] == 2
    assert body["latest_applied_run_count"] == 1
    assert body["latest_applied_runs_without_fingerprint"] == 0
    assert body["superseded_completed_run_count"] == 1
    observed = {row["fingerprint"]: row for row in body["observed_semantic_policy_fingerprints"]}
    assert observed[drifted_fingerprint]["latest_applied_run_count"] == 0
    assert observed[current]["latest_applied_run_count"] == 1


def test_semantic_governance_prefers_active_generation_receipt_over_ledger_history(
    client: TestClient,
) -> None:
    from okto_neuron.semantic_fingerprint import publish_semantic_materialization
    from okto_neuron.server.state import get_state

    vault_path = Path(client.vault_path)  # type: ignore[attr-defined]
    initial = client.get("/api/v1/quality/semantic/governance")
    assert initial.status_code == 200, initial.text
    current = initial.json()["current_semantic_policy_fingerprint"]
    runtime = get_state()
    generation = str(runtime.vault.store._graph_handle.graph_generation or "")
    measured = http_mod._effective_semantic_fingerprints(runtime)
    publish_semantic_materialization(
        vault_path,
        graph_generation=generation,
        fingerprints={
            "config": measured.config_fingerprint,
            "extraction": measured.extraction_fingerprint,
            "semantic_policy": current,
        },
        source="test_rebuild",
    )

    ledger = CandidateLedger(vault_path / ".marginalia")
    drifted = f"sha256:{'f' * 64}"
    run_id = ledger.start_run(
        document_id="doc:historical",
        source="historical.md",
        blocks_total=1,
        model="fixture",
        semantic_policy_fingerprint=drifted,
    )
    ledger.finish_run(run_id, state="completed", summary={})

    response = client.get("/api/v1/quality/semantic/governance")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["materialized_graph_generation"] == generation
    assert body["materialized_semantic_policy_fingerprint"] == current
    assert body["materialization_source"] == "test_rebuild"
    assert body["rebuild_required"] is False
    assert body["observed_semantic_policy_fingerprints"][0]["fingerprint"] == drifted


def test_semantic_quality_accepts_provisional_registry_labels(client: TestClient) -> None:
    vault_path = Path(client.vault_path)  # type: ignore[attr-defined]
    registry = PredicateRegistry(vault_path)
    registry.upsert(
        PredicateRecord(
            label="observed_near",
            lifecycle="provisional",
            definition="The subject was observed near the stated object.",
            direction="subject_to_object",
            symmetric=False,
            signatures=(),
            support_count=0,
            samples=(),
            confidence=0.8,
            provenance=PredicateDecisionProvenance(
                source="human",
                decision_id="test-observed-near",
                judge_model="",
                prompt_version="",
                semantic_policy_fingerprint="test-policy-v1",
                created_at="2026-07-17T00:00:00Z",
            ),
        )
    )
    runtime = get_server_state().active_runtime
    assert runtime is not None
    _add_literal_claim(runtime.vault.store, claim_id="claim:provisional", predicate="observed_near")

    response = client.post("/api/v1/quality/semantic", json={})

    assert response.status_code == 200, response.text
    report = response.json()["semantic_quality"]
    coverage = report["layers"]["predicate"]["registry_coverage"]
    assert coverage["status"] == "measured"
    assert coverage["unregistered_count"] == 0
    assert report["evidence"]["external_inputs"]["registered_predicates"]["count"] == len(
        registry.labels()
    )


def test_semantic_quality_still_rejects_placeholder_predicates(client: TestClient) -> None:
    runtime = get_server_state().active_runtime
    assert runtime is not None
    _add_literal_claim(runtime.vault.store, claim_id="claim:placeholder", predicate="unknown")

    response = client.post("/api/v1/quality/semantic", json={})

    assert response.status_code == 200, response.text
    report = response.json()["semantic_quality"]
    predicate_layer = report["layers"]["predicate"]
    assert predicate_layer["placeholder_predicates"] == {
        "count": 1,
        "samples": ["unknown"],
    }
    assert predicate_layer["registry_coverage"]["unregistered_samples"] == ["unknown"]
    placeholder_check = next(
        row
        for row in report["hard_invariants"]["checks"]
        if row["code"] == "placeholder_predicates"
    )
    assert placeholder_check["status"] == "failed"
    assert report["verdict"]["status"] == "failed"


def test_semantic_quality_fails_closed_for_invalid_predicate_registry(
    client: TestClient,
) -> None:
    registry = PredicateRegistry(Path(client.vault_path))  # type: ignore[attr-defined]
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.path.write_text("{not-json", encoding="utf-8")

    response = client.post("/api/v1/quality/semantic", json={})

    assert response.status_code == 500, response.text
    assert response.json() == {
        "status": 500,
        "error": "internal",
        "detail": "internal server error",
    }


def test_semantic_quality_aggregates_supplied_live_recall_cost(
    client: TestClient,
) -> None:
    recall = client.post(
        "/api/v1/recall",
        json={"query": "knowledge graph", "k": 3},
    )
    assert recall.status_code == 200, recall.text

    response = client.post(
        "/api/v1/quality/semantic",
        json={"recall_samples": [recall.json()["recall_cost"]]},
    )

    assert response.status_code == 200, response.text
    report = response.json()["semantic_quality"]
    assert report["layers"]["recall"]["status"] == "measured"
    assert report["layers"]["recall"]["measured_samples"] == 1
    check = next(
        row
        for row in report["hard_invariants"]["checks"]
        if row["code"] == "completion_free_recall"
    )
    assert check["status"] == "passed"


def test_semantic_quality_exposes_complete_precommit_ledger_variant(
    client: TestClient,
) -> None:
    ledger = CandidateLedger(Path(client.vault_path) / ".marginalia")  # type: ignore[attr-defined]
    candidate = NodeCandidate(type="Agent", title="Ari")
    run_id = ledger.start_run(
        document_id="doc",
        source="note.md",
        blocks_total=1,
        model="fixture",
    )
    ledger.record_candidate(
        run_id,
        candidate_id=candidate.candidate_id,
        candidate_kind="node",
        state="proposed",
        payload=candidate.model_dump(mode="json"),
    )
    plan_id = ledger.record_commit_plan(
        run_id,
        operations=[
            {
                "operation": "create_node",
                "candidate_kind": "node",
                "candidate_id": candidate.candidate_id,
                "candidate": candidate.model_dump(mode="json"),
                "confidence": 0.99,
                "correlations": [],
                "reason": None,
            }
        ],
    )
    ledger.record_candidate(
        run_id,
        candidate_id=candidate.candidate_id,
        candidate_kind="node",
        state="committed",
        payload={},
    )
    plan = next(plan for plan in ledger.unreceipted_commit_plans() if plan.plan_id == plan_id)
    operation = plan.operations[0]
    ledger.record_operation_receipt(
        run_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        operation_id=str(operation["operation_id"]),
        operation=str(operation["operation"]),
        status="applied",
        result={"candidate_id": candidate.candidate_id},
    )
    ledger.record_commit(run_id, plan_id=plan_id, result={})
    ledger.finish_run(run_id, state="completed", summary={})

    before = client.get("/api/v1/graph/integrity").json()["integrity"]["status"]
    response = client.post(
        "/api/v1/quality/semantic",
        json={"variant": "candidate_ledger", "run_ids": [run_id]},
    )

    assert response.status_code == 200, response.text
    report = response.json()["semantic_quality"]
    assert report["report_variant"] == "candidate_ledger_plan.v1"
    assert report["evidence"]["complete"] is True
    assert report["evidence"]["authoritative"] is False
    assert report["evidence"]["selected_run_ids"] == [run_id]
    assert report["evidence"]["source_framing"]["status"] == "measured"
    assert client.get("/api/v1/graph/integrity").json()["integrity"]["status"] == before


@pytest.mark.parametrize(
    "payload",
    [
        {"variant": "candidate_ledger"},
        {"variant": "candidate_ledger", "run_ids": []},
        {"variant": "candidate_ledger", "run_ids": [""]},
        {"variant": "unknown"},
        {"variant": "stored_graph", "registered_predicates": ["uses"]},
        {"variant": "stored_graph", "adjudication": {}},
        {"variant": "candidate_ledger", "run_ids": ["run-1"], "adjudication": []},
        {
            "variant": "candidate_ledger",
            "run_ids": ["run-1"],
            "registered_predicates": [""],
        },
    ],
)
def test_semantic_quality_rejects_invalid_variant_selection(
    client: TestClient,
    payload: dict[str, object],
) -> None:
    response = client.post("/api/v1/quality/semantic", json=payload)

    assert response.status_code == 400
    assert response.json()["error"] == "bad_request"


def test_semantic_quality_audit_is_loopback_only(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = client.get("/api/v1/graph/integrity").json()["integrity"]
    assert before["status"] == "unverified"
    assert client.get("/api/v1/quality/semantic").status_code == 404
    monkeypatch.setattr(http_mod, "remote_config_allowed", lambda request: False)

    response = client.post("/api/v1/quality/semantic", json={})

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"
    after = client.get("/api/v1/graph/integrity").json()["integrity"]
    assert after["status"] == "unverified"


def test_semantic_quality_does_not_start_while_server_is_draining(
    client: TestClient,
) -> None:
    runtime = get_server_state().active_runtime
    assert runtime is not None
    sidecar = integrity_state_path(runtime.vault_path)
    before = sidecar.read_bytes()
    runtime.mark_draining()
    try:
        response = client.post("/api/v1/quality/semantic", json={})
    finally:
        runtime.draining = False

    assert response.status_code == 503
    assert sidecar.read_bytes() == before


def test_semantic_quality_rechecks_draining_after_waiting_for_writer_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    state = SimpleNamespace(draining=False)

    class DrainOnAcquire:
        async def acquire(self):
            state.draining = True
            return True

        def release(self):
            return None

    state.writer_lock = DrainOnAcquire()
    evaluated = False

    def evaluate(_state) -> dict:
        nonlocal evaluated
        evaluated = True
        return {}

    monkeypatch.setattr(http_mod, "get_state", lambda: state)
    monkeypatch.setattr(http_mod, "remote_config_allowed", lambda request: True)
    monkeypatch.setattr(http_mod, "_fresh_semantic_quality_report", evaluate)

    class Request:
        async def body(self) -> bytes:
            return b"{}"

    response = asyncio.run(http_mod.api_semantic_quality(Request()))

    assert response.status_code == 503
    assert evaluated is False


def test_semantic_quality_fails_closed_when_fresh_adjacency_audit_fails(
    client: TestClient,
) -> None:
    clean = client.post("/api/v1/quality/semantic", json={})
    assert clean.status_code == 200, clean.text
    assert clean.json()["semantic_quality"]["evidence"]["technical_integrity_verified"]

    state = get_server_state()
    state.vault.store._execute(  # noqa: SLF001 - intentional corruption fixture
        "MATCH (:Node)-[e:Edge {id: $id}]->(:Node) SET e.src = $src",
        {"id": "e1", "src": "concept:1"},
    )

    response = client.post("/api/v1/quality/semantic", json={})
    assert response.status_code == 200, response.text
    report = response.json()["semantic_quality"]
    evidence = report["evidence"]
    assert evidence["complete"] is False
    assert evidence["integrity_status"] == "failed"
    assert evidence["technical_integrity_verified"] is False
    assert evidence["topology_evidence_status"] == "not_measured"
    assert evidence["integrity_audit"]["issue_count"] == 1
    assert evidence["integrity_audit"]["first_issue"]["code"] == "adjacency_property_mismatch"
    assert report["layers"]["relation"]["materialized_topology_edges"] is None
    assert report["layers"]["relation"]["missing_topology_edges"]["status"] == "not_measured"
    checks = {row["code"]: row for row in report["hard_invariants"]["checks"]}
    assert checks["relation_materialization"]["status"] == "not_measured"
    assert report["verdict"]["status"] == "incomplete"
    assert client.get("/api/v1/graph/integrity").json()["integrity"]["writer_fenced"]


def test_semantic_quality_omits_stale_cached_audit_after_terminal_fence(
    client: TestClient,
) -> None:
    clean = client.post("/api/v1/quality/semantic", json={})
    assert clean.status_code == 200, clean.text
    assert clean.json()["semantic_quality"]["evidence"]["integrity_audit"]["status"] == ("verified")

    state = get_server_state()
    handle = state.vault.store._graph_handle
    write_integrity_state(
        state.vault_path,
        GraphIntegrityState(
            status=AuditStatus.FAILED,
            graph_generation=handle.graph_generation,
            writer_fenced=True,
            reason="terminal test fence",
        ),
    )

    fenced = client.post("/api/v1/quality/semantic", json={})
    assert fenced.status_code == 200, fenced.text
    evidence = fenced.json()["semantic_quality"]["evidence"]
    assert evidence["integrity_status"] == "failed"
    assert evidence["technical_integrity_verified"] is False
    assert "integrity_audit" not in evidence


def test_failed_integrity_fence_blocks_writes_but_not_reads(client: TestClient) -> None:
    state = get_server_state()
    handle = state.vault.store._graph_handle
    write_integrity_state(
        state.vault_path,
        GraphIntegrityState(
            status=AuditStatus.FAILED,
            graph_generation=handle.graph_generation,
            writer_fenced=True,
            reason="test adjacency mismatch",
        ),
    )

    read = client.get("/api/v1/nodes")
    assert read.status_code == 200, read.text

    write = client.post("/api/v1/ingest", json={"content": "# blocked"})
    assert write.status_code == 409, write.text
    assert write.json()["error"] == "integrity_fenced"

    batch = client.post(
        "/api/v1/review-queue/batch",
        json={"candidate_ids": [], "action": "commit"},
    )
    assert batch.status_code == 409, batch.text
    assert batch.json()["error"] == "integrity_fenced"

    status = client.get("/api/v1/status")
    assert status.status_code == 200, status.text
    assert status.json()["status"] == "degraded"
    assert status.json()["integrity"]["failed_or_incomplete"] == 1

    refresh = client.post("/api/v1/graph/integrity", json={})
    assert refresh.status_code == 200, refresh.text
    assert refresh.json()["integrity"]["status"] == "failed"


def test_incomplete_integrity_audit_can_retry_to_verified(client: TestClient) -> None:
    state = get_server_state()
    handle = state.vault.store._graph_handle
    write_integrity_state(
        state.vault_path,
        GraphIntegrityState(
            status=AuditStatus.INCOMPLETE,
            graph_generation=handle.graph_generation,
            writer_fenced=True,
            reason="scan was interrupted",
        ),
    )

    retried = client.post("/api/v1/graph/integrity", json={})
    assert retried.status_code == 200, retried.text
    assert retried.json()["integrity"]["status"] == "verified"
    assert retried.json()["integrity"]["writer_fenced"] is False


def test_nodes_list_type_filter(client: TestClient) -> None:
    r = client.get("/api/v1/nodes?type=Claim")
    assert r.status_code == 200, r.text
    nodes = r.json()["nodes"]
    assert nodes and all(n["type"] == "Claim" for n in nodes)


def test_nodes_list_unknown_type_400(client: TestClient) -> None:
    r = client.get("/api/v1/nodes?type=Bogus")
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_nodes_list_query_substring(client: TestClient) -> None:
    r = client.get("/api/v1/nodes?q=bletchley")
    assert r.status_code == 200
    nodes = r.json()["nodes"]
    assert [n["id"] for n in nodes] == ["claim:1"]


def test_nodes_list_pagination(client: TestClient) -> None:
    r = client.get("/api/v1/nodes?limit=1&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert len(body["nodes"]) == 1
    assert body["total"] >= 3


def test_nodes_list_bad_limit_400(client: TestClient) -> None:
    assert client.get("/api/v1/nodes?limit=0").status_code == 400
    assert client.get("/api/v1/nodes?limit=abc").status_code == 400
    assert client.get("/api/v1/nodes?offset=-1").status_code == 400


def test_node_detail_claim_with_provenance(client: TestClient) -> None:
    r = client.get("/api/v1/nodes/claim:1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["node"]["id"] == "claim:1"
    prov = body["provenance"]
    assert prov is not None
    assert prov["path"] == str(Path(client.vault_path) / "note.md")
    assert prov["byte_start"] == 10
    assert prov["byte_end"] == 42
    assert prov["content_hash"] == "sha256:" + "a" * 64
    assert prov["block_id"] == "block:1"
    assert body["block"]["id"] == "block:1"
    out_types = {e["type"] for e in body["edges"]["out"]}
    in_types = {e["type"] for e in body["edges"]["in"]}
    assert "prov:wasDerivedFrom" in out_types
    assert "skos:broader" in in_types


def test_node_detail_non_claim_null_provenance(client: TestClient) -> None:
    r = client.get("/api/v1/nodes/concept:1")
    assert r.status_code == 200
    body = r.json()
    assert body["provenance"] is None
    assert body["block"] is None


def test_node_detail_404(client: TestClient) -> None:
    r = client.get("/api/v1/nodes/nope:99")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_node_detail_internal_node_hidden_404(client: TestClient) -> None:
    r = client.get("/api/v1/nodes/__meta__")
    assert r.status_code == 404


def test_node_types_facets(client: TestClient) -> None:
    r = client.get("/api/v1/node-types")
    assert r.status_code == 200, r.text
    types = {t["name"]: t for t in r.json()["types"]}
    assert set(types) == CLOSED_NODE_TYPES
    assert types["Claim"]["count"] == 1
    assert types["Claim"]["kind"] == "support"
    assert types["Concept"]["kind"] == "primitive"
    assert types["Finding"]["count"] == 0


def test_node_types_flag_never_minted_support_types(client: TestClient) -> None:
    """A zero for a type no ingest path writes must not read as a regression."""
    r = client.get("/api/v1/node-types")
    assert r.status_code == 200, r.text
    types = {t["name"]: t for t in r.json()["types"]}
    for name in ("Identifier", "Annotation", "Finding"):
        assert types[name]["minted_by_ingest"] is False, name
        assert types[name]["count"] == 0, name
    for name in (
        "Agent",
        "Activity",
        "Concept",
        "InformationObject",
        "Place",
        "Document",
        "Claim",
        "Block",
    ):
        assert types[name]["minted_by_ingest"] is True, name


# ── config ───────────────────────────────────────────────────────────────────


def test_config_get(client: TestClient) -> None:
    r = client.get("/api/v1/config")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["llm"]["defaults"]["api_base"] == "http://127.0.0.1:8123/v1"
    assert body["consolidation"]["auto_commit_threshold"] == 0.75
    assert body["consolidation"]["type_adjudication_enabled"] is True
    assert body["consolidation"]["relation_curator_enabled"] is True
    assert body["consolidation"]["audit_superseded_nodes_with_llm"] is False
    assert body["consolidation"]["audit_superseded_relations_with_llm"] is False
    assert body["upkeep"] == {
        "enabled": True,
        "max_pairs_per_run": 30,
        "min_support": 2,
        "cluster_threshold": 0.8,
        "auto_fold_threshold": 0.85,
    }
    assert body["llm"]["defaults"]["max_tokens"] is None
    assert body["llm"]["defaults"]["sampling_payload"] == {}
    assert body["embedding"]["batch_size"] == 32
    assert body["embedding"]["max_concurrent_batches"] == 1
    assert body["ingest"] == {
        "incremental": True,
        "subchunk": True,
        "chunk_size_bytes": 6_000,
        "chunk_overlap_bytes": 0,
    }
    assert body["reembed_required_fields"] == [
        "embedding.provider_ref",
        "embedding.provider",
        "embedding.model",
        "embedding.dimension",
    ]
    assert body["semantic_rebuild_required_fields"] == [
        "consolidation.type_adjudication_enabled",
        "consolidation.relation_curator_enabled",
    ]
    # per-step override blocks are present in the payload
    assert "extraction" in body["llm"]
    assert "judge" in body["llm"]
    assert "curator" in body["llm"]
    assert "relation_curator" in body["llm"]
    assert "ask" in body["llm"]
    assert "step_default_prompts" in body["llm"]
    assert "curator" in body["llm"]["step_default_prompts"]
    assert "relation_curator" in body["llm"]["step_default_prompts"]
    assert "Kingdoms -> Regions -> Settlements" in body["llm"]["step_default_prompts"]["extraction"]
    assert "Epics -> Stories -> Tasks" not in body["llm"]["step_default_prompts"]["extraction"]
    assert "Definition of Done" not in body["llm"]["step_default_prompts"]["curator"]


def test_onboarding_llm_patch_is_visible_in_config_api(client: TestClient) -> None:
    from okto_neuron.config import VaultConfig
    from okto_neuron.onboarding import get_provider_preset, llm_config_patch

    patch = llm_config_patch(
        get_provider_preset("local"),
        model="qwen-local",
        api_base="http://127.0.0.1:9999/v1",
        api_key_env=None,
    )
    VaultConfig.apply_patch(client.vault_path, patch)  # type: ignore[attr-defined]

    r = client.get("/api/v1/config")

    assert r.status_code == 200, r.text
    defaults = r.json()["llm"]["defaults"]
    assert defaults["provider"] == "openai"
    assert defaults["api_base"] == "http://127.0.0.1:9999/v1"
    assert defaults["model"] == "qwen-local"
    assert defaults["api_key_env"] is None


def test_config_patch_llm_live(client: TestClient) -> None:
    r = client.patch(
        "/api/v1/config",
        json={"llm": {"defaults": {"model": "llama3.1", "max_tokens": 32000}}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == "live"
    assert body["config"]["llm"]["defaults"]["model"] == "llama3.1"
    assert body["config"]["llm"]["defaults"]["max_tokens"] == 32000
    # persisted
    raw = yaml.safe_load((Path(client.vault_path) / "okto-neuron.yaml").read_text())  # type: ignore[attr-defined]
    assert raw["llm"]["defaults"]["model"] == "llama3.1"
    assert raw["llm"]["defaults"]["max_tokens"] == 32000


def test_config_patch_step_preserves_and_replaces_sampling_payload(
    client: TestClient,
) -> None:
    """Unrelated edits preserve explicit overrides; a sampling_payload edit
    replaces the map wholesale (FROZEN, no per-key merge) so removing a value
    through the raw editor is durable."""
    from okto_neuron.config import VaultConfig

    r = client.patch(
        "/api/v1/config",
        json={"llm": {"extraction": {"sampling_payload": {"typical_p": 0.9, "top_k": 20}}}},
    )
    assert r.status_code == 200, r.text

    r = client.patch("/api/v1/config", json={"llm": {"extraction": {"model": "some-other-model"}}})
    assert r.status_code == 200, r.text

    cfg = VaultConfig.load(client.vault_path)  # type: ignore[attr-defined]
    extraction = cfg.llm.resolved("extraction")
    assert extraction.model == "some-other-model"
    assert extraction.sampling_payload == {"typical_p": 0.9, "top_k": 20}

    r = client.patch(
        "/api/v1/config",
        json={"llm": {"extraction": {"sampling_payload": {"top_k": 5}}}},
    )
    assert r.status_code == 200, r.text

    cfg = VaultConfig.load(client.vault_path)  # type: ignore[attr-defined]
    assert cfg.llm.resolved("extraction").sampling_payload == {"top_k": 5}


def test_config_patch_embedding_reembed(client: TestClient) -> None:
    r = client.patch("/api/v1/config", json={"embedding": {"model": "BAAI/other"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == "reembed"
    assert any("re-embed" in n for n in body["notes"])
    assert any("no restart is required" in n for n in body["notes"])


def test_config_patch_embedding_execution_is_live_and_does_not_reembed(
    client: TestClient,
) -> None:
    response = client.patch(
        "/api/v1/config",
        json={
            "embedding": {
                "batch_size": 64,
                "max_concurrent_batches": 12,
            }
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == "live"
    assert any("active embedding work" in note for note in body["notes"])
    assert body["affected_vaults"] == []
    assert body["config"]["embedding"]["batch_size"] == 64
    assert body["config"]["embedding"]["max_concurrent_batches"] == 12

    raw = yaml.safe_load((Path(client.vault_path) / "okto-neuron.yaml").read_text())  # type: ignore[attr-defined]
    assert raw["embedding"]["batch_size"] == 64
    assert raw["embedding"]["max_concurrent_batches"] == 12

    invalid = client.patch(
        "/api/v1/config",
        json={"embedding": {"max_concurrent_batches": 33}},
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"] == "bad_request"


def test_config_patch_ingest_chunking_is_live_and_requires_reingest(
    client: TestClient,
) -> None:
    response = client.patch(
        "/api/v1/config",
        json={"ingest": {"chunk_size_bytes": 24_000, "chunk_overlap_bytes": 2_000}},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == "live"
    assert body["config"]["ingest"]["chunk_size_bytes"] == 24_000
    assert body["config"]["ingest"]["chunk_overlap_bytes"] == 2_000
    assert any("reingest existing sources" in note for note in body["notes"])
    assert body["affected_vaults"] == []

    invalid = client.patch(
        "/api/v1/config",
        json={"ingest": {"chunk_size_bytes": 1024, "chunk_overlap_bytes": 1024}},
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"] == "bad_request"


def test_config_patch_superseded_audit_flags(client: TestClient) -> None:
    r = client.patch(
        "/api/v1/config",
        json={
            "consolidation": {
                "audit_superseded_nodes_with_llm": True,
                "audit_superseded_relations_with_llm": True,
            }
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == "live"
    assert body["config"]["consolidation"]["audit_superseded_nodes_with_llm"] is True
    assert body["config"]["consolidation"]["audit_superseded_relations_with_llm"] is True
    raw = yaml.safe_load((Path(client.vault_path) / "okto-neuron.yaml").read_text())  # type: ignore[attr-defined]
    assert raw["consolidation"]["audit_superseded_nodes_with_llm"] is True
    assert raw["consolidation"]["audit_superseded_relations_with_llm"] is True


def test_config_patch_semantic_stage_requires_rebuild(client: TestClient) -> None:
    response = client.patch(
        "/api/v1/config",
        json={
            "consolidation": {
                "type_adjudication_enabled": False,
                "relation_curator_enabled": False,
            }
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["applied"] == "rebuild"
    assert body["rebuild_required_vaults"] == [str(client.vault_path)]
    assert any("semantic model stage changed" in note for note in body["notes"])
    assert body["config"]["consolidation"]["type_adjudication_enabled"] is False
    assert body["config"]["consolidation"]["relation_curator_enabled"] is False
    raw = yaml.safe_load((Path(client.vault_path) / "okto-neuron.yaml").read_text())
    assert raw["consolidation"]["type_adjudication_enabled"] is False
    assert raw["consolidation"]["relation_curator_enabled"] is False


def test_config_get_exposes_adr0015_knobs(client: TestClient) -> None:
    r = client.get("/api/v1/config")
    assert r.status_code == 200, r.text
    cons = r.json()["consolidation"]
    assert cons["curation_max_concurrent"] == 1
    assert cons["curation_call_timeout_s"] == DEFAULT_CURATION_CALL_TIMEOUT_S  # issue #24
    assert cons["curation_batch_size"] == 1
    assert cons["prefilter"]["enabled"] is False
    assert cons["prefilter"]["established_entity_fastpath"] is True


def test_config_patch_adr0015_knobs_nested(client: TestClient) -> None:
    r = client.patch(
        "/api/v1/config",
        json={
            "consolidation": {
                "curation_max_concurrent": 4,
                "curation_batch_size": 10,
                "curation_call_timeout_s": 90,
                "prefilter": {"enabled": True, "established_entity_fastpath": False},
            }
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == "live"
    cons = body["config"]["consolidation"]
    assert cons["curation_max_concurrent"] == 4
    assert cons["curation_batch_size"] == 10
    assert cons["curation_call_timeout_s"] == 90.0
    assert cons["prefilter"]["enabled"] is True
    assert cons["prefilter"]["established_entity_fastpath"] is False
    # The nested prefilter merge must not clobber sibling defaults.
    assert cons["prefilter"]["min_mentions"] == 1
    raw = yaml.safe_load((Path(client.vault_path) / "okto-neuron.yaml").read_text())  # type: ignore[attr-defined]
    assert raw["consolidation"]["curation_max_concurrent"] == 4
    assert raw["consolidation"]["prefilter"]["enabled"] is True


def test_config_patch_adr0015_bounds_400(client: TestClient) -> None:
    r = client.patch("/api/v1/config", json={"consolidation": {"curation_max_concurrent": 33}})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"
    r = client.patch("/api/v1/config", json={"consolidation": {"curation_batch_size": 0}})
    assert r.status_code == 400


def test_config_patch_extraction_concurrency_roundtrip_and_bounds(
    client: TestClient,
) -> None:
    response = client.patch(
        "/api/v1/config",
        json={"llm": {"extraction": {"max_concurrent": 16}}},
    )
    assert response.status_code == 200, response.text
    assert response.json()["applied"] == "live"
    assert any("active extraction" in note for note in response.json()["notes"])
    assert response.json()["config"]["llm"]["extraction"]["max_concurrent"] == 16
    raw = yaml.safe_load((Path(client.vault_path) / "okto-neuron.yaml").read_text())  # type: ignore[attr-defined]
    assert raw["llm"]["extraction"]["max_concurrent"] == 16

    response = client.patch(
        "/api/v1/config",
        json={"llm": {"extraction": {"max_concurrent": 33}}},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "bad_request"


def test_config_patch_upkeep_roundtrip(client: TestClient) -> None:
    r = client.patch(
        "/api/v1/config",
        json={
            "upkeep": {
                "enabled": False,
                "max_pairs_per_run": 12,
                "min_support": 3,
                "cluster_threshold": 0.77,
                "auto_fold_threshold": 0.91,
            }
        },
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["config"]["upkeep"] == {
        "enabled": False,
        "max_pairs_per_run": 12,
        "min_support": 3,
        "cluster_threshold": 0.77,
        "auto_fold_threshold": 0.91,
    }

    raw = yaml.safe_load((Path(client.vault_path) / "okto-neuron.yaml").read_text())  # type: ignore[attr-defined]
    assert raw["upkeep"]["max_pairs_per_run"] == 12


def test_config_patch_upkeep_bounds_400(client: TestClient) -> None:
    bad_patches = [
        {"upkeep": {"max_pairs_per_run": 0}},
        {"upkeep": {"max_pairs_per_run": 201}},
        {"upkeep": {"min_support": 0}},
        {"upkeep": {"min_support": 101}},
        {"upkeep": {"cluster_threshold": 0.49}},
        {"upkeep": {"cluster_threshold": 1.0}},
        {"upkeep": {"auto_fold_threshold": 0.49}},
        {"upkeep": {"auto_fold_threshold": 1.01}},
    ]
    for patch in bad_patches:
        r = client.patch("/api/v1/config", json=patch)
        assert r.status_code == 400, patch
        assert r.json()["error"] == "bad_request"


def test_config_patch_out_of_range_400(client: TestClient) -> None:
    r = client.patch("/api/v1/config", json={"consolidation": {"auto_commit_threshold": 1.5}})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_config_patch_remote_base_url_blocked(client: TestClient) -> None:
    r = client.patch(
        "/api/v1/config", json={"llm": {"defaults": {"api_base": "http://10.0.0.9:9/v1"}}}
    )
    assert r.status_code == 400
    assert "loopback" in r.json()["detail"] or "allow_remote" in r.json()["detail"]


def test_config_patch_remote_base_url_optin_ok(client: TestClient) -> None:
    r = client.patch(
        "/api/v1/config",
        json={"llm": {"allow_remote": True, "defaults": {"api_base": "http://10.0.0.9:9/v1"}}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["config"]["llm"]["defaults"]["api_base"] == "http://10.0.0.9:9/v1"


def test_config_patch_bad_scheme_400(client: TestClient) -> None:
    r = client.patch(
        "/api/v1/config", json={"llm": {"defaults": {"api_base": "file:///etc/passwd"}}}
    )
    assert r.status_code == 400


def test_config_patch_literal_api_key_rejected(client: TestClient) -> None:
    # api_key is not a field in LLMDefaults (only api_key_env is) — extra_forbidden → 400.
    secret = "sk-" + "must-not-be-reflected"
    r = client.patch("/api/v1/config", json={"llm": {"defaults": {"api_key": secret}}})
    assert r.status_code == 400
    assert secret not in r.text


def test_credential_put_stores_and_rotates_without_exposing_secret(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / "home" / ".okto-neuron" / "env"
    monkeypatch.setenv("OKTO_NEURON_ENV_FILE", str(env_path))
    first_secret = "sk-" + "first-provider-secret"
    second_secret = "sk-" + "second-provider-secret"
    body = {
        "provider": "openai",
        "api_base": "https://api.z.ai/api/coding/paas/v4/",
        "api_key": first_secret,
    }

    response = client.put("/api/v1/llm/credential", json=body)
    assert response.status_code == 200, response.text
    payload = response.json()
    env_name = payload["api_key_env"]
    try:
        assert payload == {
            "status": "ok",
            "api_key_env": env_name,
            "configured": True,
        }
        assert env_name.startswith("OKTO_NEURON_PROVIDER_OPENAI_API_Z_AI_")
        assert env_name.endswith("_API_KEY")
        assert first_secret not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert os.environ[env_name] == first_secret
        if os.name != "nt":
            assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
            assert stat.S_IMODE(env_path.parent.stat().st_mode) == 0o700

        response = client.put("/api/v1/llm/credential", json={**body, "api_key": second_secret})
        assert response.status_code == 200, response.text
        assert response.json()["api_key_env"] == env_name
        assert first_secret not in env_path.read_text(encoding="utf-8")
        assert env_path.read_text(encoding="utf-8").count(f"{env_name}=") == 1
        assert os.environ[env_name] == second_secret

        response = client.patch(
            "/api/v1/config",
            json={"llm": {"defaults": {"api_key_env": env_name}}},
        )
        assert response.status_code == 200, response.text
        assert response.json()["config"]["credential_status"] == {env_name: True}
        raw_yaml = (Path(client.vault_path) / "okto-neuron.yaml").read_text(encoding="utf-8")  # type: ignore[attr-defined]
        assert second_secret not in raw_yaml
        assert env_name in raw_yaml
    finally:
        os.environ.pop(env_name, None)


def test_concurrent_managed_credentials_both_persist_and_reload(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    import threading

    from okto_neuron import onboarding

    env_path = tmp_path / "managed-env"
    monkeypatch.setenv("OKTO_NEURON_ENV_FILE", str(env_path))
    first_entered = threading.Event()
    second_prelock = threading.Event()
    second_entered = threading.Event()
    release_writes = threading.Event()
    active_lock = threading.Lock()
    active = 0
    max_active = 0
    original_default = onboarding.default_api_key_env
    original_write = onboarding.write_user_env_secret

    def _tracked_default(provider: str, api_base: str | None = None) -> str:
        if provider == "voyage":
            second_prelock.set()
        return original_default(provider, api_base)

    def _tracked_write(env_name: str, secret: str, path: Path | None = None) -> Path:
        nonlocal active, max_active
        with active_lock:
            active += 1
            max_active = max(max_active, active)
        try:
            if "OPENAI" in env_name:
                first_entered.set()
                assert second_prelock.wait(timeout=5)
                second_entered.wait(timeout=0.2)
                release_writes.set()
            else:
                second_entered.set()
                assert release_writes.wait(timeout=5)
            return original_write(env_name, secret, path=path)
        finally:
            with active_lock:
                active -= 1

    monkeypatch.setattr(onboarding, "default_api_key_env", _tracked_default)
    monkeypatch.setattr(onboarding, "write_user_env_secret", _tracked_write)
    requests = [
        {
            "kind": "llm",
            "provider": "openai",
            "api_key": "first-managed-secret",
        },
        {
            "kind": "embedding",
            "provider": "voyage",
            "api_key": "second-managed-secret",
        },
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(client.put, "/api/v1/credentials/provider", json=requests[0])
        assert first_entered.wait(timeout=2)
        second = executor.submit(client.put, "/api/v1/credentials/provider", json=requests[1])
        responses = [first.result(timeout=10), second.result(timeout=10)]

    assert [response.status_code for response in responses] == [200, 200]
    assert max_active == 1
    env_names = [response.json()["api_key_env"] for response in responses]
    stored = env_path.read_text(encoding="utf-8")
    assert all(stored.count(f"{env_name}=") == 1 for env_name in env_names)

    for env_name in env_names:
        os.environ.pop(env_name, None)
    onboarding.load_user_env_file(env_path)
    try:
        assert os.environ[env_names[0]] == requests[0]["api_key"]
        assert os.environ[env_names[1]] == requests[1]["api_key"]
    finally:
        for env_name in env_names:
            os.environ.pop(env_name, None)


@pytest.mark.asyncio
async def test_cancelled_credential_request_keeps_worker_serialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading

    import httpx

    from okto_neuron import onboarding

    home = tmp_path / "home"
    env_path = home / ".okto-neuron" / "env"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OKTO_NEURON_ENV_FILE", str(env_path))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.delenv("OKTO_NEURON_VAULT", raising=False)
    first_entered = threading.Event()
    second_prelock = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    original_default = onboarding.default_api_key_env
    original_write = onboarding.write_user_env_secret

    def _tracked_default(provider: str, api_base: str | None = None) -> str:
        if provider == "voyage":
            second_prelock.set()
        return original_default(provider, api_base)

    def _tracked_write(env_name: str, secret: str, path: Path | None = None) -> Path:
        if "OPENAI" in env_name:
            first_entered.set()
            assert release_first.wait(timeout=5)
        else:
            second_entered.set()
        return original_write(env_name, secret, path=path)

    monkeypatch.setattr(onboarding, "default_api_key_env", _tracked_default)
    monkeypatch.setattr(onboarding, "write_user_env_secret", _tracked_write)
    reset_state_for_tests()
    state = init_state(None, None)
    app = build_rest_app(state)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 53102))
    env_names = [
        original_default("openai"),
        original_default("voyage"),
    ]
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
        ) as async_client:
            first = asyncio.create_task(
                async_client.put(
                    "/api/v1/credentials/provider",
                    json={
                        "kind": "llm",
                        "provider": "openai",
                        "api_key": "first-cancelled-secret",
                    },
                )
            )
            assert await asyncio.to_thread(first_entered.wait, 2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            second = asyncio.create_task(
                async_client.put(
                    "/api/v1/credentials/provider",
                    json={
                        "kind": "embedding",
                        "provider": "voyage",
                        "api_key": "second-managed-secret",
                    },
                )
            )
            assert await asyncio.to_thread(second_prelock.wait, 2)
            assert not second.done()
            assert not second_entered.is_set()

            release_first.set()
            response = await asyncio.wait_for(second, timeout=10)

        assert response.status_code == 200, response.text
        stored = env_path.read_text(encoding="utf-8")
        assert all(stored.count(f"{env_name}=") == 1 for env_name in env_names)
        for env_name in env_names:
            os.environ.pop(env_name, None)
        onboarding.load_user_env_file(env_path)
        assert os.environ[env_names[0]] == "first-cancelled-secret"
        assert os.environ[env_names[1]] == "second-managed-secret"
    finally:
        release_first.set()
        for env_name in env_names:
            os.environ.pop(env_name, None)
        reset_state_for_tests()


@pytest.mark.parametrize(
    "body",
    [
        {"provider": "openai", "api_key": ""},
        {"provider": "openai", "api_key": "safe\nMARGINALIA_ENV_FILE=evil"},
        {"provider": "openai", "api_key": "x" * 16_385},
        {"provider": "bedrock", "api_key": "not-an-api-key-provider"},
        {
            "provider": "openai",
            "api_key": "sk-test",
            "api_key_env": "OKTO_NEURON_ENV_FILE",
        },
    ],
)
def test_credential_put_rejects_unsafe_or_unsupported_requests(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, str],
) -> None:
    env_path = tmp_path / "env"
    monkeypatch.setenv("OKTO_NEURON_ENV_FILE", str(env_path))

    response = client.put("/api/v1/llm/credential", json=body)

    assert response.status_code == 400, response.text
    assert not env_path.exists()
    secret = body.get("api_key", "")
    if secret:
        assert secret not in response.text


def test_credential_put_is_loopback_only(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(http_mod, "remote_config_allowed", lambda request: False)

    response = client.put(
        "/api/v1/llm/credential",
        json={"provider": "openai", "api_key": "sk-test"},
    )

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden"


def test_credential_storage_failure_never_exposes_the_secret(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "credential-storage-failure-secret"

    def fail_safely(*_args: object, **_kwargs: object) -> None:
        raise OSError("Windows credential protection failed")

    monkeypatch.setattr(
        "okto_neuron.onboarding.write_user_env_secret",
        fail_safely,
    )

    response = client.put(
        "/api/v1/credentials/provider",
        json={"kind": "embedding", "provider": "voyage", "api_key": secret},
    )

    assert response.status_code == 500
    assert response.json()["error"] == "internal"
    assert secret not in response.text
    assert secret not in caplog.text


def test_embedding_credential_uses_the_managed_provider_contract(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / "env"
    monkeypatch.setenv("OKTO_NEURON_ENV_FILE", str(env_path))
    secret = "voyage-managed-secret"

    response = client.put(
        "/api/v1/credentials/provider",
        json={"kind": "embedding", "provider": "voyage", "api_key": secret},
    )

    assert response.status_code == 200, response.text
    assert client.get("/api/v1/config").json()["managed_credentials_supported"] is True
    payload = response.json()
    env_name = payload["api_key_env"]
    try:
        assert payload == {
            "status": "ok",
            "api_key_env": env_name,
            "configured": True,
        }
        assert env_name == "OKTO_NEURON_PROVIDER_VOYAGE_API_KEY"
        assert secret not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert os.environ[env_name] == secret

        saved = client.patch(
            "/api/v1/config",
            json={
                "embedding": {
                    "provider": "voyage",
                    "model": "voyage-3",
                    "dimension": 1024,
                    "api_key_env": env_name,
                    "allow_remote": True,
                }
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["config"]["embedding"]["api_key_env"] == env_name
        assert saved.json()["config"]["credential_status"] == {env_name: True}
        raw_yaml = (Path(client.vault_path) / "okto-neuron.yaml").read_text(encoding="utf-8")  # type: ignore[attr-defined]
        assert secret not in raw_yaml
        assert env_name in raw_yaml
    finally:
        os.environ.pop(env_name, None)


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "embedding", "provider": "bedrock", "api_key": "not-single-key"},
        {"kind": "unknown", "provider": "voyage", "api_key": "not-used"},
        {"provider": "voyage", "api_key": "missing-kind"},
    ],
)
def test_generic_credential_route_rejects_unsupported_embedding_requests(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, str],
) -> None:
    env_path = tmp_path / "env"
    monkeypatch.setenv("OKTO_NEURON_ENV_FILE", str(env_path))

    response = client.put("/api/v1/credentials/provider", json=body)

    assert response.status_code == 400, response.text
    assert body["api_key"] not in response.text
    assert not env_path.exists()


def test_embedding_provider_or_endpoint_change_clears_stale_credential(
    client: TestClient,
) -> None:
    env_name = "OKTO_NEURON_PROVIDER_VOYAGE_API_KEY"
    configured = client.patch(
        "/api/v1/config",
        json={
            "embedding": {
                "provider": "voyage",
                "model": "voyage-3",
                "dimension": 1024,
                "api_key_env": env_name,
                "allow_remote": True,
            }
        },
    )
    assert configured.status_code == 200, configured.text

    changed = client.patch(
        "/api/v1/config",
        json={"embedding": {"provider": "openai", "model": "text-embedding-3-small"}},
    )

    assert changed.status_code == 200, changed.text
    assert changed.json()["config"]["embedding"]["api_key_env"] is None


def test_embedding_test_runs_the_real_provider_without_returning_vector_or_secret(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    secret = "embedding-test-secret"

    def embedding(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            data=[
                {"index": 0, "embedding": [0.25, 0.75]},
                {"index": 1, "embedding": [0.5, 1.0]},
            ]
        )

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(embedding=embedding))
    monkeypatch.setenv("OKTO_NEURON_TEST_EMBEDDING_KEY", secret)

    response = client.post(
        "/api/v1/embedding/test",
        json={
            "provider": "voyage",
            "model": "voyage-3",
            "dimension": 2,
            "api_key_env": "OKTO_NEURON_TEST_EMBEDDING_KEY",
            "allow_remote": True,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "models": ["voyage-3"],
        "dimension": 2,
        "vectors": 2,
        "error": None,
    }
    assert response.headers["cache-control"] == "no-store"
    assert calls[0]["model"] == "voyage/voyage-3"
    assert len(calls[0]["input"]) == 2
    assert calls[0]["api_key"] == secret
    assert "embedding" not in response.text
    assert secret not in response.text


def test_embedding_test_redacts_provider_exception_secret(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "embedding-exception-secret"

    def embedding(**kwargs):
        raise RuntimeError(f"provider reflected {kwargs['api_key']}")

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(embedding=embedding))
    monkeypatch.setenv("OKTO_NEURON_TEST_EMBEDDING_KEY", secret)

    response = client.post(
        "/api/v1/embedding/test",
        json={
            "provider": "voyage",
            "model": "voyage-3",
            "dimension": 2,
            "api_key_env": "OKTO_NEURON_TEST_EMBEDDING_KEY",
            "allow_remote": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    # The surfaced error names the provider/model boundary and the normalized
    # failure category, but never the provider's own exception text.
    assert response.json()["error"] == (
        "litellm embedding failed for voyage/voyage-3 (category=unknown)"
    )
    assert "provider reflected" not in response.text
    assert secret not in response.text


def test_config_patch_unknown_block_400(client: TestClient) -> None:
    r = client.patch("/api/v1/config", json={"bogus": {"x": 1}})
    assert r.status_code == 400


def test_config_patch_api_key_env_accepted_and_persisted(client: TestClient) -> None:
    r = client.patch(
        "/api/v1/config",
        json={"llm": {"defaults": {"api_key_env": "OKTO_NEURON_OPENAI_KEY"}}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["config"]["llm"]["defaults"]["api_key_env"] == "OKTO_NEURON_OPENAI_KEY"
    raw = yaml.safe_load((Path(client.vault_path) / "okto-neuron.yaml").read_text())  # type: ignore[attr-defined]
    assert raw["llm"]["defaults"]["api_key_env"] == "OKTO_NEURON_OPENAI_KEY"
    assert "api_key" not in raw.get("llm", {}).get("defaults", {})  # env-name only, never a secret


# ── H1: api_key_env must be namespace-scoped (no arbitrary env exfil) ──────────


def test_config_patch_api_key_env_arbitrary_var_rejected(client: TestClient) -> None:
    # The exfil vector: point api_key_env at a server secret + allow_remote +
    # attacker api_base → key leaks on next ask. The allowlist blocks it at write.
    for bad in ("AWS_SECRET_ACCESS_KEY", "PATH", "OPENAI_API_KEY", "marginalia_lower"):
        r = client.patch("/api/v1/config", json={"llm": {"defaults": {"api_key_env": bad}}})
        assert r.status_code == 400, (bad, r.text)
        assert r.json()["error"] == "bad_request"
    # Unchanged on disk.
    raw = yaml.safe_load((Path(client.vault_path) / "okto-neuron.yaml").read_text()) or {}  # type: ignore[attr-defined]
    defaults = raw.get("llm", {}).get("defaults", {})
    assert "api_key_env" not in defaults or defaults["api_key_env"] is None


# ── L2: packs validated against the built-in registry ─────────────────────────


def test_config_patch_unknown_pack_rejected(client: TestClient) -> None:
    r = client.patch("/api/v1/config", json={"packs": ["core", "totally-fake-pack"]})
    assert r.status_code == 400
    assert "totally-fake-pack" in r.json()["detail"] or "unknown pack" in r.json()["detail"]


def test_config_patch_known_packs_ok(client: TestClient) -> None:
    r = client.patch("/api/v1/config", json={"packs": ["core", "research"]})
    assert r.status_code == 200, r.text
    assert r.json()["config"]["packs"] == ["core", "research"]


def test_config_prompts_follow_sdlc_pack(client: TestClient) -> None:
    r = client.patch("/api/v1/config", json={"packs": ["core", "research", "sdlc"]})
    assert r.status_code == 200, r.text
    prompts = r.json()["config"]["llm"]["step_default_prompts"]

    assert "Software-delivery / SDLC pack guidance" in prompts["extraction"]
    assert "Epics -> Stories -> Tasks" in prompts["extraction"]
    assert "Definition of Done" in prompts["curator"]
    assert "has -> includes" in prompts["relation_curator"]


# ── L3: recall/ask k is capped ────────────────────────────────────────────────


def test_recall_k_cap_400(client: TestClient) -> None:
    r = client.post("/api/v1/recall", json={"query": "x", "k": 9999})
    assert r.status_code == 400
    assert "k must be" in r.json()["detail"]


def test_ask_k_cap_400(client: TestClient) -> None:
    r = client.post("/api/v1/ask", json={"question": "x", "k": 9999})
    assert r.status_code == 400


def test_ask_policy_seed_k_cap_400(client: TestClient) -> None:
    r = client.post(
        "/api/v1/ask",
        json={"question": "x", "retrieval_policy": {"seed_k": 9999}},
    )
    assert r.status_code == 400
    assert "k must be" in r.json()["detail"]


def test_ask_policy_rejects_unknown_field(client: TestClient) -> None:
    r = client.post(
        "/api/v1/ask",
        json={"question": "x", "retrieval_policy": {"bogus": 1}},
    )
    assert r.status_code == 400
    assert "retrieval_policy" in r.json()["detail"]


# ── L1: loopback Host-header guard (DNS-rebind) ───────────────────────────────


def test_non_loopback_host_header_403(client: TestClient) -> None:
    r = client.get("/api/v1/config", headers={"host": "evil.attacker.example"})
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden_host"


def test_loopback_host_variants_ok(client: TestClient) -> None:
    for host in ("127.0.0.1", "localhost", "127.0.0.1:7777", "[::1]:7777"):
        r = client.get("/api/v1/config", headers={"host": host})
        assert r.status_code == 200, (host, r.text)


def test_non_loopback_host_allowed_when_allow_remote(client: TestClient) -> None:
    from okto_neuron.server.state import get_state

    get_state().allow_remote = True
    try:
        r = client.get("/api/v1/config", headers={"host": "marginalia.lan"})
        assert r.status_code == 200, r.text
    finally:
        get_state().allow_remote = False


# ── POST /api/v1/llm/test ─────────────────────────────────────────────────────


def test_llm_test_stub_happy_path(client: TestClient) -> None:
    """Stub provider short-circuits without any network call → 200 ok:true."""
    r = client.post("/api/v1/llm/test", json={"provider": "stub", "model": "stub"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["models"] == ["stub"]
    assert body["error"] is None


def test_llm_validate_does_not_require_a_model_before_discovery(client: TestClient) -> None:
    response = client.post("/api/v1/llm/test", json={"provider": "stub"})

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "models": ["stub"], "error": None}


def test_llm_validate_uses_provider_specific_discovery_for_exact_preset(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron.onboarding import ModelDiscoveryResult

    env_name = "OKTO_NEURON_PROVIDER_ANTHROPIC_API_KEY"
    secret = "anthropic-test-secret"
    seen: dict[str, object] = {}

    def fake_discover(preset, **kwargs):  # type: ignore[no-untyped-def]
        seen["preset"] = preset.key
        seen.update(kwargs)
        return ModelDiscoveryResult(["claude-test"])

    monkeypatch.setenv(env_name, secret)
    monkeypatch.setattr("okto_neuron.onboarding.discover_models", fake_discover)
    config_response = client.patch("/api/v1/config", json={"llm": {"allow_remote": True}})
    assert config_response.status_code == 200, config_response.text

    response = client.post(
        "/api/v1/llm/test",
        json={
            "provider": "anthropic",
            "api_base": "https://api.anthropic.com/v1",
            "api_key_env": env_name,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "models": ["claude-test"], "error": None}
    assert seen["preset"] == "anthropic"
    assert seen["api_key"] == secret
    assert secret not in response.text


def test_llm_test_spoofed_host_403(client: TestClient) -> None:
    """L1: spoofed non-loopback Host on /llm/test is rejected before the handler runs."""
    r = client.post(
        "/api/v1/llm/test",
        json={"provider": "stub", "model": "stub"},
        headers={"host": "evil.attacker.example"},
    )
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden_host"


def test_llm_test_bedrock_missing_boto3_short_circuits_models_probe(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    def find_spec(module_name: str):
        if module_name == "boto3":
            return None
        return object()

    def fail_async_client(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("Bedrock dependency failure must not issue /models probe")

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(httpx, "AsyncClient", fail_async_client)

    r = client.post(
        "/api/v1/llm/test",
        json={
            "provider": "bedrock",
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "api_base": "http://127.0.0.1:8123/v1",
        },
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["models"] == []
    assert "bedrock" in body["error"].lower()
    assert "boto3" in body["error"].lower()
    assert "bedrock extra" in body["error"].lower()


def test_llm_test_bedrock_rejects_invalid_api_key_env_before_preflight(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspected: list[str] = []

    def find_spec(module_name: str):
        inspected.append(module_name)
        return None

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-be-read")

    r = client.post(
        "/api/v1/llm/test",
        json={
            "provider": "bedrock",
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "api_key_env": "AWS_SECRET_ACCESS_KEY",
        },
    )

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["models"] == []
    assert "api_key_env" in body["error"]
    assert inspected == []


# ── POST /api/v1/llm/test-completion ──────────────────────────────────────────


def test_llm_test_completion_stub_happy_path(client: TestClient) -> None:
    """Stub provider round-trips a real (deterministic) completion → 200 ok:true."""
    r = client.post("/api/v1/llm/test-completion", json={"provider": "stub", "model": "stub"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["reply"] is not None
    assert "pong" in body["reply"]
    assert body["error"] is None
    assert isinstance(body["duration_s"], float)
    assert body["parameter_plan"] == {
        "sent": [],
        "extra_body": [],
        "omitted": {},
    }


def test_llm_test_completion_applies_its_disclosed_probe_timeout(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class ProbeProvider:
        def complete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return "pong"

    def provider_for(resolved):  # type: ignore[no-untyped-def]
        seen["request_timeout_s"] = resolved.request_timeout_s
        return ProbeProvider()

    monkeypatch.setattr("okto_neuron.llm.get_provider", provider_for)

    response = client.post(
        "/api/v1/llm/test-completion",
        json={"provider": "stub", "model": "stub"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert seen["request_timeout_s"] == 60.0


def test_llm_test_completion_spoofed_host_403(client: TestClient) -> None:
    """L1: spoofed non-loopback Host on /llm/test-completion is rejected before the handler runs."""
    r = client.post(
        "/api/v1/llm/test-completion",
        json={"provider": "stub", "model": "stub"},
        headers={"host": "evil.attacker.example"},
    )
    assert r.status_code == 403
    assert r.json()["error"] == "forbidden_host"


def test_llm_test_completion_missing_provider_400(client: TestClient) -> None:
    r = client.post("/api/v1/llm/test-completion", json={"model": "stub"})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_llm_test_completion_missing_model_400(client: TestClient) -> None:
    r = client.post("/api/v1/llm/test-completion", json={"provider": "stub"})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_llm_test_completion_rejects_invalid_api_key_env(client: TestClient) -> None:
    r = client.post(
        "/api/v1/llm/test-completion",
        json={"provider": "stub", "model": "stub", "api_key_env": "AWS_SECRET_ACCESS_KEY"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["reply"] is None
    assert "api_key_env" in body["error"]


def test_llm_test_completion_unknown_provider_reports_error(client: TestClient) -> None:
    r = client.post(
        "/api/v1/llm/test-completion",
        json={"provider": "not-a-real-provider", "model": "whatever"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["reply"] is None
    assert body["error"]


def test_llm_test_completion_redacts_configured_key_from_response_and_logs(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env_name = "OKTO_NEURON_PROVIDER_OPENAI_API_KEY"
    secret = "sk-" + "SENTINEL-http-secret"

    class BrokenProvider:
        def complete(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError(
                f"provider echoed api_key={secret} at "
                "http://private-user:private-pass@127.0.0.1:8123/v1"
            )

    monkeypatch.setenv(env_name, secret)
    monkeypatch.setattr("okto_neuron.llm.get_provider", lambda resolved: BrokenProvider())

    response = client.post(
        "/api/v1/llm/test-completion",
        json={
            "provider": "openai",
            "model": "test",
            "api_base": "http://127.0.0.1:8123/v1",
            "api_key_env": env_name,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is False
    assert "[redacted]" in response.json()["error"]
    assert secret not in response.text
    assert "private-user" not in response.text
    assert "private-pass" not in response.text
    assert secret not in caplog.text


# ── M1: locality helpers exist for devops to gate sensitive routes ────────────


def test_locality_helpers_exposed() -> None:
    from okto_neuron.server.http import request_is_loopback

    class _Req:
        class _C:
            host = "203.0.113.7"

        client = _C()
        headers: dict[str, str] = {}

    assert request_is_loopback(_Req()) is False  # real remote peer
    _Req.client.host = "127.0.0.1"
    assert request_is_loopback(_Req()) is True


def test_sensitive_write_gate_loopback_only_even_with_allow_remote(client: TestClient) -> None:
    # R1: config PATCH and MCP remember/init_vault stay loopback-only regardless
    # of compatibility state. Production startup rejects direct remote serving.
    from okto_neuron.server.http import remote_config_allowed
    from okto_neuron.server.state import get_state

    class _Req:
        class _C:
            host = "203.0.113.7"  # real remote peer

        client = _C()
        headers: dict[str, str] = {}

    get_state().allow_remote = True  # the flag must NOT relax the write gate
    try:
        assert remote_config_allowed(_Req()) is False  # remote write still refused
    finally:
        get_state().allow_remote = False
    _Req.client.host = "127.0.0.1"
    assert remote_config_allowed(_Req()) is True


def test_api_key_env_rejects_trailing_newline() -> None:
    # H1 nit: fullmatch, so a trailing newline can't smuggle past the `$`.
    # api_key_env lives in LLMDefaults (and StepLLM), not on LLMConfig directly.
    from okto_neuron.config._vault import LLMDefaults

    with pytest.raises(Exception):
        LLMDefaults(api_key_env="OKTO_NEURON_X\n")


# ── query ──────────────────────────────────────────────────────────────────--


def test_recall_rich_provenance(client: TestClient) -> None:
    r = client.post("/api/v1/recall", json={"query": "knowledge graph", "k": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert isinstance(body["hits"], list)
    assert body["recall_cost"]["schema_version"] == "recall_cost.v1"
    assert body["recall_cost"]["completion_calls"] == 0
    assert body["recall_cost"]["generated_tokens"] == 0
    assert body["recall_cost"]["query_embedding_calls"] == 1
    assert body["recall_cost"]["completion_free"] is True
    for hit in body["hits"]:
        assert {"node", "score", "provenance"} <= set(hit)
        assert {"path", "byte_start", "byte_end", "content_hash"} <= set(hit["provenance"])


def test_recall_missing_field_400(client: TestClient) -> None:
    r = client.post("/api/v1/recall", json={"k": 5})
    assert r.status_code == 400


def test_ask_shape(client: TestClient) -> None:
    r = client.post("/api/v1/ask", json={"question": "what about knowledge graphs?", "k": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"text", "citations", "hits", "retrieval"} <= set(body)
    assert isinstance(body["citations"], list)
    assert isinstance(body["hits"], list)
    assert body["retrieval"]["seed_k"] == 5


def test_ask_without_llm_is_degraded_and_never_calls_a_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import okto_neuron.llm as llm_mod

    def _no_call(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("ask built an LLM provider with no model configured")

    monkeypatch.setattr(llm_mod, "get_provider", _no_call)
    monkeypatch.setattr(http_mod, "_companion", lambda state: Companion(state.vault))
    r = client.post("/api/v1/ask", json={"question": "what about knowledge graphs?", "k": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "degraded"
    assert body["text"] == ""
    assert body["retrieval"]["synthesis_status"] == "no_llm"
    assert body["retrieval"]["no_llm_reason"]


def test_ask_policy_shape(client: TestClient) -> None:
    r = client.post(
        "/api/v1/ask",
        json={
            "question": "what about knowledge graphs?",
            "retrieval_policy": {
                "enable_subgraph": False,
                "seed_k": 6,
                "source_block_policy": "never",
                "source_block_budget_tokens": 500,
            },
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["retrieval"]["seed_k"] == 6
    assert body["retrieval"]["source_block_policy"] == "never"
    assert body["retrieval"]["source_blocks_used"] is False


def test_ask_missing_field_400(client: TestClient) -> None:
    r = client.post("/api/v1/ask", json={})
    assert r.status_code == 400


def test_add_records_in_ingest_queue(client: TestClient) -> None:
    """A deterministic POST /add (no LLM) must show up in the durable ingest
    queue as a done/stored item so server-initiated stores are visible in the UI."""
    r = client.post("/add", json={"path": "memo.md", "content": "# Memo\n\nbody about graphs.\n"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"

    q = client.get("/api/v1/ingest-queue")
    assert q.status_code == 200, q.text
    items = q.json()["items"]
    memo = [i for i in items if i["name"] == "memo.md"]
    assert len(memo) == 1, items
    assert memo[0]["status"] == "done"
    assert memo[0]["stage"] == "stored"
    assert memo[0]["outcome"] == {"quality": "not_applicable"}


def test_api_v1_routes_precede_bare_routes(client: TestClient) -> None:
    # The bare /recall must still work for CLI back-compat (different shape).
    r = client.post("/recall", json={"query": "knowledge graph", "k": 3})
    assert r.status_code == 200
    assert "results" in r.json()  # bare shape, not hits


# ── ingest (paste/write/attach -> companion) ──────────────────────────────────


def test_ingest_ok_materializes_source_and_runs_companion(client: TestClient) -> None:
    r = client.post(
        "/api/v1/ingest",
        json={"content": "# Note\n\nAda Lovelace wrote the first algorithm.\n", "title": "ada"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert {"document_id", "filename", "committed", "queued", "outcomes", "outcome"} <= set(body)
    assert isinstance(body["committed"], int) and isinstance(body["queued"], int)
    assert isinstance(body["outcomes"], list)
    assert isinstance(body["outcome"], dict)
    # The posted text persists as a durable markdown source (trust root).
    src = Path(client.vault_path) / ".marginalia" / "sources" / body["filename"]
    assert src.exists()
    assert "Ada Lovelace" in src.read_text(encoding="utf-8")


def test_ingest_empty_content_400(client: TestClient) -> None:
    assert client.post("/api/v1/ingest", json={"content": "   "}).status_code == 400


def test_ingest_missing_content_400(client: TestClient) -> None:
    assert client.post("/api/v1/ingest", json={"title": "x"}).status_code == 400


def test_ingest_filename_sanitized_no_traversal(client: TestClient) -> None:
    r = client.post(
        "/api/v1/ingest",
        json={"content": "body", "filename": "../../../etc/passwd"},
    )
    assert r.status_code == 200, r.text
    filename = r.json()["filename"]
    assert "/" not in filename and ".." not in filename
    assert filename.endswith(".md")
    # File lands inside sources, nowhere else.
    src = Path(client.vault_path) / ".marginalia" / "sources" / filename
    assert src.exists()


def test_ingest_unnamed_gets_content_hashed_filename(client: TestClient) -> None:
    r = client.post("/api/v1/ingest", json={"content": "some free text with no title"})
    assert r.status_code == 200, r.text
    assert r.json()["filename"].startswith("note-")


def test_ingest_remote_peer_refused(client: TestClient) -> None:
    # Ingest is a sensitive write: loopback-only even under --allow-remote.
    from okto_neuron.server.http import api_ingest

    class _Req:
        class _C:
            host = "203.0.113.7"  # real remote peer

        client = _C()
        headers: dict[str, str] = {}

        async def json(self) -> dict[str, str]:
            return {"content": "x"}

    import asyncio

    from okto_neuron.server.state import get_state

    get_state().allow_remote = True  # must NOT relax the write gate
    try:
        resp = asyncio.run(api_ingest(_Req()))
    finally:
        get_state().allow_remote = False
    assert resp.status_code == 403


# ── bulk ingest: folder / batch / queue ──────────────────────────────────────


def test_ingest_folder_enqueues_text_files(client: TestClient, tmp_path: Path) -> None:
    folder = tmp_path / "corpus"
    folder.mkdir()
    (folder / "a.md").write_text("# A\n\nAlan Turing worked at Bletchley.\n", encoding="utf-8")
    (folder / "b.txt").write_text("Ada Lovelace wrote the first algorithm.\n", encoding="utf-8")
    (folder / "skip.png").write_bytes(b"\x89PNG")  # non-text ignored

    r = client.post("/api/v1/ingest-folder", json={"path": str(folder)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert {"summary", "items", "enqueued", "truncated"} <= set(body)
    assert body["enqueued"] == 2  # the png is filtered out
    assert body["truncated"] is False
    assert {"total", "queued", "processing", "done", "error", "active"} <= set(body["summary"])
    names = {it["name"] for it in body["items"]}
    assert {"a.md", "b.txt"} <= names


def test_ingest_folder_missing_path_400(client: TestClient) -> None:
    assert client.post("/api/v1/ingest-folder", json={}).status_code == 400


def test_ingest_folder_relative_path_400(client: TestClient) -> None:
    assert client.post("/api/v1/ingest-folder", json={"path": "rel/dir"}).status_code == 400


def test_ingest_folder_nonexistent_404(client: TestClient, tmp_path: Path) -> None:
    missing = tmp_path / "nope"
    assert client.post("/api/v1/ingest-folder", json={"path": str(missing)}).status_code == 404


def test_ingest_folder_no_text_files_400(client: TestClient, tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    (folder / "x.png").write_bytes(b"\x89PNG")
    assert client.post("/api/v1/ingest-folder", json={"path": str(folder)}).status_code == 400


def test_ingest_batch_enqueues_uploads(client: TestClient) -> None:
    files = [
        {"filename": "one.md", "content": "# One\n\nGrace Hopper coined the term bug.\n"},
        {"filename": "two.md", "content": "Edsger Dijkstra wrote about structured programming.\n"},
    ]
    r = client.post("/api/v1/ingest-batch", json={"files": files})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enqueued"] == 2
    assert len(body["enqueued_item_ids"]) == 2
    assert len(set(body["enqueued_item_ids"])) == 2
    assert set(body["enqueued_item_ids"]) <= {item["id"] for item in body["items"]}
    assert body["summary"]["total"] >= 2
    # Uploaded content materialized to the durable sources dir.
    src = Path(client.vault_path) / ".marginalia" / "sources"
    assert src.exists()


def test_ingest_batch_empty_list_400(client: TestClient) -> None:
    assert client.post("/api/v1/ingest-batch", json={"files": []}).status_code == 400


def test_ingest_batch_all_empty_content_400(client: TestClient) -> None:
    r = client.post("/api/v1/ingest-batch", json={"files": [{"filename": "x.md", "content": "  "}]})
    assert r.status_code == 400


def test_ingest_batch_oversize_file_400(client: TestClient) -> None:
    huge = "x" * 2_000_001
    r = client.post(
        "/api/v1/ingest-batch", json={"files": [{"filename": "big.md", "content": huge}]}
    )
    assert r.status_code == 400


def test_ingest_batch_applies_folder_source_selection_policy(client: TestClient) -> None:
    """The Web UI's "Choose a folder" button POSTs here, not to /ingest-folder,
    and used to be filtered by nothing but a browser regex: a folder
    /ingest-folder reduced to 76 files was queued here as 168, 51% of it
    dot-directory scaffolding and agent tooling notes. The batch endpoint now
    applies the SAME policy the folder walk applies (ADR 0025 F1 / ADR 0026)."""
    files = [
        {"filename": "vault/real.md", "content": "Alan Turing worked at Bletchley.\n"},
        {"filename": "vault/sub/nested.md", "content": "Ada Lovelace wrote an algorithm.\n"},
        {"filename": "vault/.scratchpad/plan.md", "content": "scratch state\n"},
        {"filename": "vault/.remember/now.md", "content": "agent memory\n"},
        {"filename": "vault/.claude/settings.md", "content": "agent config\n"},
        {"filename": "vault/.pytest_cache/last.md", "content": "cache\n"},
        {"filename": "vault/CLAUDE.md", "content": "agent tooling notes\n"},
        {"filename": "vault/docs/CLAUDE.md", "content": "more tooling notes\n"},
        {"filename": "vault/.documentation/.hidden.md", "content": "dot file\n"},
    ]
    r = client.post("/api/v1/ingest-batch", json={"files": files})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enqueued"] == 2, body["skipped"]
    queued = {it["name"] for it in body["items"]}
    assert "real.md" in queued and "nested.md" in queued
    assert not any(n.startswith("CLAUDE") for n in queued), queued
    # Reported, never silent: exact counts plus a per-file reason.
    assert body["skipped_excluded"] == 7
    reasons = {s["filename"]: s["reason"] for s in body["skipped"]}
    assert reasons["vault/.scratchpad/plan.md"] == "ignored_dir"
    assert reasons["vault/.remember/now.md"] == "ignored_dir"
    assert reasons["vault/.claude/settings.md"] == "ignored_dir"
    assert reasons["vault/.pytest_cache/last.md"] == "ignored_dir"
    assert reasons["vault/CLAUDE.md"] == "denylisted"
    assert reasons["vault/docs/CLAUDE.md"] == "denylisted"


def test_ingest_batch_rejects_non_markdown_suffix(client: TestClient) -> None:
    """Markdown is the trust root, and that must not depend on a browser regex.
    safe_source_filename force-appends .md to any other stem, so a direct POST
    of x.pdf used to land as x.pdf.md and sail past the downstream suffix gate;
    any non-browser caller bypassed the only filter there was."""
    r = client.post(
        "/api/v1/ingest-batch",
        json={
            "files": [
                {"filename": "keep.md", "content": "a real note\n"},
                {"filename": "scan.pdf", "content": "%PDF-1.7 not markdown\n"},
                {"filename": "sheet.xlsx", "content": "binary-ish\n"},
                {"filename": "notes/deck.pptx", "content": "slides\n"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enqueued"] == 1
    assert body["skipped_non_text"] == 3
    assert {s["reason"] for s in body["skipped"]} == {"non_text_suffix"}
    sources = Path(client.vault_path) / ".marginalia" / "sources"  # type: ignore[attr-defined]
    assert not list(sources.rglob("*.pdf.md")), "a .pdf must never land as .pdf.md"


def test_ingest_batch_accepts_legitimate_nested_markdown(client: TestClient) -> None:
    """The policy must not over-reject: a plain nested note, a .txt, and a bare
    index.md outside tracking/ are all legitimate knowledge."""
    files = [
        {"filename": "vault/notes/2026/q3/review.md", "content": "quarterly review\n"},
        {"filename": "vault/docs/index.md", "content": "a real index note\n"},
        {"filename": "vault/plain.txt", "content": "a text note\n"},
        {"filename": "vault/readme.markdown", "content": "markdown variant\n"},
    ]
    r = client.post("/api/v1/ingest-batch", json={"files": files})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enqueued"] == 4, body["skipped"]
    assert body["skipped"] == []
    assert body["skipped_excluded"] == 0 and body["skipped_non_text"] == 0


def test_ingest_batch_all_rejected_400_names_the_reasons(client: TestClient) -> None:
    """When the policy rejects everything the failure must SAY what it rejected.
    The owner just lost a run to over-inclusion nobody could see; a generic
    "nothing to ingest" would repeat that in the other direction."""
    r = client.post(
        "/api/v1/ingest-batch",
        json={
            "files": [
                {"filename": ".scratchpad/a.md", "content": "x\n"},
                {"filename": "CLAUDE.md", "content": "x\n"},
                {"filename": "b.pdf", "content": "x\n"},
                {"filename": "empty.md", "content": "   "},
            ]
        },
    )
    assert r.status_code == 400
    body = r.json()
    assert body["skipped_excluded"] == 2
    assert body["skipped_non_text"] == 1
    assert body["skipped_empty"] == 1
    assert "no ingestible files to queue" in body["detail"]
    for fragment in ("excluded directory", "tooling/scaffolding", "not markdown/text", "empty"):
        assert fragment in body["detail"], body["detail"]
    assert {s["filename"] for s in body["skipped"]} == {
        ".scratchpad/a.md",
        "CLAUDE.md",
        "b.pdf",
        "empty.md",
    }


def test_ingest_batch_honors_vault_configured_globs(client: TestClient) -> None:
    """A customized folder_watch config applies to BOTH ingest surfaces — the
    batch endpoint reads the same vault config /ingest-folder does."""
    cfg_path = Path(client.vault_path) / "okto-neuron.yaml"  # type: ignore[attr-defined]
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    cfg["folder_watch"] = {
        "ignore_globs": ["scratch-*.md"],
        "ignore_dir_globs": [".*", "__pycache__", "node_modules", "archive"],
    }
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    r = client.post(
        "/api/v1/ingest-batch",
        json={
            "files": [
                {"filename": "vault/keep.md", "content": "kept\n"},
                {"filename": "vault/scratch-draft.md", "content": "glob-excluded\n"},
                {"filename": "vault/archive/old.md", "content": "dir-excluded\n"},
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enqueued"] == 1
    assert {s["filename"]: s["reason"] for s in body["skipped"]} == {
        "vault/scratch-draft.md": "ignored_glob",
        "vault/archive/old.md": "ignored_dir",
    }


def test_ingest_batch_same_basename_different_folders_all_survive(
    client: TestClient,
) -> None:
    """Drag-dropping a folder sends webkitRelativePath, so four catalogo.md from
    four sibling directories must land as four files. They used to collapse onto
    sources/catalogo.md, each overwriting the last — a real vault lost 23 of 166
    documents that way."""
    folders = ["guias", "husky", "invoices", "notas-fiscais"]
    r = client.post(
        "/api/v1/ingest-batch",
        json={
            "files": [
                {"filename": f"cnpj/{d}/catalogo.md", "content": f"# catalogo\n\nbody for {d}.\n"}
                for d in folders
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["enqueued"] == len(folders)

    sources = Path(client.vault_path) / ".marginalia" / "sources"  # type: ignore[attr-defined]
    written = sorted(p for p in sources.rglob("catalogo.md"))
    assert len(written) == len(folders), [str(p) for p in written]
    assert len({p.read_text(encoding="utf-8") for p in written}) == len(folders)
    # The relative tree is mirrored, so provenance paths stay readable.
    for d in folders:
        assert any(p.parent.name == d for p in written), d


def test_ingest_batch_flat_name_collision_does_not_clobber(client: TestClient) -> None:
    """Two uploads that share a bare basename but differ in content keep both
    sets of bytes — a Block anchors byte ranges to the source path."""
    for body in ("first document\n", "second, different document\n"):
        r = client.post(
            "/api/v1/ingest-batch",
            json={"files": [{"filename": "notes.md", "content": body}]},
        )
        assert r.status_code == 200, r.text

    sources = Path(client.vault_path) / ".marginalia" / "sources"  # type: ignore[attr-defined]
    notes = sorted(p for p in sources.rglob("notes*.md"))
    assert len(notes) == 2, [str(p) for p in notes]
    assert {p.read_text(encoding="utf-8") for p in notes} == {
        "first document\n",
        "second, different document\n",
    }

    # Re-posting identical content is idempotent: no third file.
    r = client.post(
        "/api/v1/ingest-batch",
        json={"files": [{"filename": "notes.md", "content": "first document\n"}]},
    )
    assert r.status_code == 200, r.text
    assert len(sorted(sources.rglob("notes*.md"))) == 2


def test_upload_target_path_cannot_escape_sources(tmp_path: Path) -> None:
    """A traversal-shaped upload name stays inside sources/."""
    from okto_neuron.server import _ingest_queue as iq

    sources = tmp_path / "sources"
    sources.mkdir()
    for evil in ("../../etc/passwd.md", "/etc/passwd.md", "a/../../b.md"):
        target = iq.upload_target_path(sources, evil, "x")
        assert sources.resolve() in target.resolve().parents, (evil, target)


def test_ingest_queue_read_only_shape(client: TestClient) -> None:
    r = client.get("/api/v1/ingest-queue")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert {"vault", "summary", "items"} <= set(body)
    assert body["vault"]["path"] == str(Path(client.vault_path).resolve(strict=False))  # type: ignore[arg-type, attr-defined]
    assert {"total", "queued", "processing", "done", "error", "active"} <= set(body["summary"])


def test_ingest_folder_remote_peer_refused(client: TestClient) -> None:
    from okto_neuron.server.http import api_ingest_folder

    class _Req:
        class _C:
            host = "203.0.113.7"

        client = _C()
        headers: dict[str, str] = {}

        async def json(self) -> dict[str, str]:
            return {"path": "/tmp"}

    import asyncio

    from okto_neuron.server.state import get_state

    get_state().allow_remote = True
    try:
        resp = asyncio.run(api_ingest_folder(_Req()))
    finally:
        get_state().allow_remote = False
    assert resp.status_code == 403


def test_ingest_batch_remote_peer_refused(client: TestClient) -> None:
    from okto_neuron.server.http import api_ingest_batch

    class _Req:
        class _C:
            host = "203.0.113.7"

        client = _C()
        headers: dict[str, str] = {}

        async def json(self) -> dict[str, object]:
            return {"files": [{"filename": "x.md", "content": "body"}]}

    import asyncio

    from okto_neuron.server.state import get_state

    get_state().allow_remote = True
    try:
        resp = asyncio.run(api_ingest_batch(_Req()))
    finally:
        get_state().allow_remote = False
    assert resp.status_code == 403


def test_drain_worker_processes_queue_to_honest_terminal(
    client: TestClient, tmp_path: Path
) -> None:
    # Directly exercise the background drain (TestClient's loop closes before the
    # fire-and-forget worker runs, so the HTTP contract tests never hit this path).
    import asyncio

    from okto_neuron.server import _ingest_queue as iq
    from okto_neuron.server import http as http_mod
    from okto_neuron.server.state import get_state

    ext = tmp_path / "external"
    ext.mkdir()
    src = ext / "note.md"
    src.write_text("# Note\n\nAlan Turing worked at Bletchley Park.\n", encoding="utf-8")
    state = get_state()
    # Folder ingest copies into the vault sources dir (remember re-validates that
    # every source path lives under the vault root), so this also proves an
    # external file gets relocated under the trust root before extraction.
    sources = state.vault_path / ".marginalia" / "sources"
    iq.enqueue_paths(state, [src], sources)
    assert state.ingest_queue and state.ingest_queue[-1].status == "queued"
    assert str(state.vault_path) in state.ingest_queue[-1].path

    asyncio.run(iq._drain(state, http_mod._companion))

    item = state.ingest_queue[-1]
    # The offline stub cannot produce extraction JSON, so every unit ends
    # `empty_after_retry` with zero provider failures -- a legitimate `empty`
    # technical outcome (2026-09-14 fix), not a `failed` one: nothing broke,
    # StubLLM just never yields parseable candidates. D6 still requires this
    # be a visible, honest terminal state rather than a success-shaped `done`
    # with no trace of what happened -- it just isn't an *error*.
    assert item.status == "done"
    assert item.outcome.get("quality") == "empty"
    assert "has no attribute 'vault'" not in (item.error or "")
    assert state.ingest_worker_active is False


# ── ingest queue hygiene (retry / delete) ─────────────────────────────────────


def _seed_queue_item(status: str, *, error: str | None = None):
    from okto_neuron.server import _ingest_queue as iq
    from okto_neuron.server.state import get_state

    state = get_state()
    item = iq.IngestItem(
        id=f"test-{status}-{len(state.ingest_queue)}",
        name="seed.md",
        path=str(Path(state.vault_path) / ".marginalia" / "sources" / "seed.md"),
        status=status,
        stage=status,
        error=error,
    )
    state.ingest_queue.append(item)
    return state, item


def test_ingest_queue_retry_errored_item(client: TestClient) -> None:
    state, item = _seed_queue_item("error", error="boom")
    # Keep the drain worker from racing the assertions: ensure_worker no-ops
    # while a worker is (claimed to be) active.
    state.ingest_worker_active = True
    try:
        r = client.post(f"/api/v1/ingest-queue/{item.id}/retry", json={})
        assert r.status_code == 200, r.text
        assert item.status == "queued"
        assert item.stage == "queued"
        assert item.error is None
        listed = [i for i in r.json()["items"] if i["id"] == item.id]
        assert listed and listed[0]["status"] == "queued"
    finally:
        state.ingest_worker_active = False


def test_ingest_queue_retry_non_error_409(client: TestClient) -> None:
    _, item = _seed_queue_item("done")
    r = client.post(f"/api/v1/ingest-queue/{item.id}/retry", json={})
    assert r.status_code == 409, r.text
    assert item.status == "done"  # untouched


def test_ingest_queue_retry_unknown_404(client: TestClient) -> None:
    assert client.post("/api/v1/ingest-queue/nope/retry", json={}).status_code == 404


def test_ingest_queue_delete_terminal_item(client: TestClient) -> None:
    state, item = _seed_queue_item("error", error="boom")
    r = client.request("DELETE", f"/api/v1/ingest-queue/{item.id}", json={})
    assert r.status_code == 200, r.text
    assert all(i.id != item.id for i in state.ingest_queue)
    assert all(i["id"] != item.id for i in r.json()["items"])


def test_ingest_queue_delete_processing_409(client: TestClient) -> None:
    state, item = _seed_queue_item("processing")
    r = client.request("DELETE", f"/api/v1/ingest-queue/{item.id}", json={})
    assert r.status_code == 409, r.text
    assert any(i.id == item.id for i in state.ingest_queue)  # still there


def test_ingest_queue_delete_queued_409(client: TestClient) -> None:
    _, item = _seed_queue_item("queued")
    assert client.request("DELETE", f"/api/v1/ingest-queue/{item.id}", json={}).status_code == 409


def test_ingest_queue_delete_unknown_404(client: TestClient) -> None:
    assert client.request("DELETE", "/api/v1/ingest-queue/nope", json={}).status_code == 404


def test_ingest_queue_retry_persists_transition(client: TestClient) -> None:
    import json as _json

    from okto_neuron.server import _ingest_queue as iq
    from okto_neuron.server.state import get_state

    state, item = _seed_queue_item("error", error="boom")
    state.ingest_worker_active = True
    try:
        assert client.post(f"/api/v1/ingest-queue/{item.id}/retry", json={}).status_code == 200
    finally:
        state.ingest_worker_active = False
    sidecar = _json.loads(iq.history_path(get_state()).read_text(encoding="utf-8"))
    persisted = [e for e in sidecar["items"] if e["id"] == item.id]
    assert persisted and persisted[0]["status"] == "queued"
    assert persisted[0]["error"] is None


def test_ingest_queue_retry_remote_peer_refused(client: TestClient) -> None:
    # Queue mutation is a sensitive write: loopback-only, same gate as config.
    import asyncio

    from okto_neuron.server.http import api_ingest_queue_retry
    from okto_neuron.server.state import get_state

    class _Req:
        class _C:
            host = "203.0.113.7"

        client = _C()
        headers: dict[str, str] = {}
        path_params = {"item_id": "x"}

    get_state().allow_remote = True  # must NOT relax the write gate
    try:
        resp = asyncio.run(api_ingest_queue_retry(_Req()))
    finally:
        get_state().allow_remote = False
    assert resp.status_code == 403


# ── config PATCH invalidates the vault's cached embedder ─────────────────────


def test_config_patch_invalidates_cached_embedder(client: TestClient) -> None:
    """A successful embedding-config PATCH must drop the vault's cached
    embedder so the NEXT use constructs from the fresh config (stale-embedder
    bug: previously only a daemon restart picked up a new embedding model)."""
    from okto_neuron.embed import StubEmbedder
    from okto_neuron.server.state import get_state

    state = get_state()
    sentinel = object()  # stands in for the long-lived cached embedder
    state.vault._embedder = sentinel

    r = client.patch("/api/v1/config", json={"embedding": {"model": "bge-m3-mlx-fp16"}})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] == "reembed"  # dim-guard messaging intact
    assert state.vault._embedder is None  # cache dropped, no eager rebuild

    # The next resolution reads the FRESH config: switch provider to stub and
    # verify the lazily rebuilt embedder reflects it.
    r = client.patch("/api/v1/config", json={"embedding": {"provider": "stub"}})
    assert r.status_code == 200, r.text
    assert isinstance(state.vault.embedder, StubEmbedder)


def test_config_patch_dimension_change_fails_closed_on_open_handle(
    client: TestClient,
) -> None:
    """A live config PATCH cannot leave the old-width graph writable."""
    from okto_neuron.errors import EmbeddingDimMismatch
    from okto_neuron.server.state import get_state

    state = get_state()
    r = client.patch(
        "/api/v1/config",
        json={
            "embedding": {
                "provider": "stub",
                "model": "stub-2560",
                "dimension": 2560,
            }
        },
    )

    assert r.status_code == 200, r.text
    assert r.json()["applied"] == "reembed"
    with pytest.raises(EmbeddingDimMismatch, match=r"re-embed the vault at the configured width \(`kg reembed`"):
        _ = state.vault.embedder


def test_config_patch_noop_keeps_cached_embedder(client: TestClient) -> None:
    from okto_neuron.config import VaultConfig
    from okto_neuron.server.state import get_state

    state = get_state()
    current = VaultConfig.load(state.vault_path)
    sentinel = object()
    state.vault._embedder = sentinel
    r = client.patch("/api/v1/config", json={"embedding": {"model": current.embedding.model}})
    assert r.status_code == 200, r.text
    assert state.vault._embedder is sentinel  # nothing changed → no invalidation


# ── writer_lock hang regression (live bug: Save spun forever behind an ingest) ──
# An ingest item (or rebuild/heal/reembed) can hold ``state.writer_lock`` for
# minutes to over an hour. Config/folder-watch writes only touch okto-neuron.yaml
# and must use ``state.config_lock`` instead — proven here by holding writer_lock
# for the ENTIRE handler call: if the handler needed writer_lock too, this would
# deadlock (a lock is not reentrant) and the ``asyncio.wait_for`` bounds would
# turn that hang into a clean test failure instead of hanging the suite.


class _FakeRequest:
    """Minimal duck-typed stand-in for ``starlette.requests.Request`` — mirrors
    the ``_Req`` fakes already used elsewhere in this file (e.g.
    ``test_ingest_remote_peer_refused``), extended with an async ``body()`` so
    handlers that go through ``_read_json`` (not just ``.json()``) work too."""

    class _Client:
        host = "127.0.0.1"  # loopback: passes remote_config_allowed/request_is_loopback

    def __init__(self, body: bytes = b"{}", path_params: dict | None = None) -> None:
        self.client = self._Client()
        self.headers: dict[str, str] = {}
        self.path_params = path_params or {}
        self._body = body

    async def body(self) -> bytes:
        return self._body


def test_config_patch_responds_while_writer_lock_held(client: TestClient) -> None:
    """Regression: PATCH /api/v1/config must use config_lock, not writer_lock (see
    ServerState.config_lock docstring — live bug: PATCH {} timed out mid-ingest)."""
    import asyncio

    from okto_neuron.server.state import get_state

    state = get_state()

    async def _run():
        async with state.writer_lock:
            return await asyncio.wait_for(
                http_mod.api_config_patch(_FakeRequest(b"{}")), timeout=2.0
            )

    resp = asyncio.run(_run())
    assert resp.status_code == 200, resp.body
    assert resp.body and b'"no fields changed"' in resp.body


def test_folder_watch_roots_add_responds_while_writer_lock_held(
    client: TestClient, tmp_path: Path
) -> None:
    """Regression: folder-watch root add/remove PATCH okto-neuron.yaml just like
    api_config_patch — must also use config_lock, not writer_lock."""
    import asyncio
    import json as json_mod

    from okto_neuron.server.state import get_state

    state = get_state()
    root = tmp_path / "watched"
    root.mkdir()
    body = json_mod.dumps({"path": str(root)}).encode("utf-8")

    async def _run():
        async with state.writer_lock:
            return await asyncio.wait_for(
                http_mod.api_folder_watch_roots_add(_FakeRequest(body)), timeout=2.0
            )

    resp = asyncio.run(_run())
    assert resp.status_code == 200, resp.body
    assert str(root) in resp.body.decode("utf-8")


def test_rollback_api_requires_current_generation_evidence_and_pins_submission(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron.server import _jobs
    from okto_neuron.server.state import get_state

    state = get_state()
    generation = str(state.vault.store._graph_handle.graph_generation or "")

    unavailable = client.post("/api/v1/curation/rollback", json={})
    assert unavailable.status_code == 409
    assert unavailable.json()["error"] == "rollback_unavailable"

    artifact_dir = state.vault_path / ".marginalia" / "rebuild-artifacts" / generation
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "previous-graph.lbug").write_bytes(b"checkpoint")
    (artifact_dir / "previous-semantic-materialization.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    (artifact_dir / "previous-semantic-policy.json").write_text(
        "{}\n",
        encoding="utf-8",
    )
    submitted: dict[str, object] = {}

    def fake_submit(_state, kind, *, label="", params=None):
        submitted.update({"kind": kind, "label": label, "params": params})
        return _jobs.CurationJob(id="rollback-test", kind=kind, label=label, params=params or {})

    monkeypatch.setattr(_jobs, "submit", fake_submit)
    accepted = client.post("/api/v1/curation/rollback", json={})
    assert accepted.status_code == 202, accepted.text
    assert submitted == {
        "kind": "rollback",
        "label": "rollback",
        "params": {"from_generation": generation},
    }
