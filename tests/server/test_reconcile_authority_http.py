"""HTTP tests for the reconcile / authority surface + the read-time equivalence
fold (ADR 0009 P2). No LLM: reconcile job RUNNERS are stubbed (the propose/apply
domain logic is tested elsewhere); these tests pin the endpoint contracts, the
loopback gate, the off-graph authority side-store, and the Browse/Graph fold.
"""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
from pathlib import Path
import time

import pytest
from starlette.testclient import TestClient

from okto_neuron import Vault
from okto_neuron.core.schema import Edge, Node
from okto_neuron.predicates import (
    ParsedPredicateVerdict,
    PredicateJudgeResult,
    PredicateJudgeVote,
)
from okto_neuron.reconcile.authority import AuthorityRecord
from okto_neuron.server import _curation
from okto_neuron.server import http as http_mod
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests
from okto_neuron.store.memory import InMemoryStore
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    reset_state_for_tests()
    vault = Vault.init(tmp_path / "v")
    # Two Concepts that are equivalence variants ('alpha' canonical, 'alpha2' variant)
    # plus a third distinct one, and a couple edges to exercise the graph fold.
    store = vault.store
    for n in (
        Node(id="c:alpha", type="Concept", title="Alpha"),
        Node(id="c:alpha2", type="Concept", title="Alpha (dup)"),
        Node(id="c:beta", type="Concept", title="Beta"),
        Node(id="__meta__", type="SchemaMetadata", title="internal"),
    ):
        store.add_node(n)
    store.add_edge(Edge(id="e1", type="skos:related", src="c:alpha2", dst="c:beta"))
    store.add_edge(Edge(id="e2", type="skos:related", src="c:alpha", dst="c:beta"))

    state = init_state(vault, vault.path)
    app = build_rest_app(state)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.vault_path = str(vault.path)  # type: ignore[attr-defined]
        yield c
    reset_state_for_tests()
    for s in list(vault_module._STORE_CACHE.values()):
        s.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


class _ClusterEmbedder:
    def embed(self, text: str) -> list[float]:  # noqa: ARG002
        return [1.0, 0.0]


class _FakePredicateJudge:
    def judge(self, candidate, *, auto_fold_threshold: float = 0.85):  # noqa: ANN001
        parsed = ParsedPredicateVerdict(
            verdict="same",
            canonical=candidate.predicate_b,
            confidence=0.8,
            reason="same relation, needs review",
        )
        votes = (
            PredicateJudgeVote(
                predicate_a=candidate.predicate_a,
                predicate_b=candidate.predicate_b,
                parsed=parsed,
                duration_s=0.01,
                usage={"total_tokens": 3},
            ),
            PredicateJudgeVote(
                predicate_a=candidate.predicate_b,
                predicate_b=candidate.predicate_a,
                parsed=parsed,
                duration_s=0.02,
                usage={"total_tokens": 4},
            ),
        )
        return PredicateJudgeResult(
            predicate_a=candidate.predicate_a,
            predicate_b=candidate.predicate_b,
            mapping="exact_match",
            status="queued",
            outcome="queued",
            canonical=candidate.predicate_b,
            confidence=min(parsed.confidence, auto_fold_threshold - 0.01),
            reason=parsed.reason,
            votes=votes,
            duration_s=0.03,
            usage={"total_tokens": 7},
            evidence={
                "counts": {
                    candidate.predicate_a: candidate.count_a,
                    candidate.predicate_b: candidate.count_b,
                },
                "shared_pairs": [],
                "sample_claim_ids": {},
            },
            judge_model="fake/predicate",
        )


@pytest.fixture
def predicate_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    reset_state_for_tests()
    root = tmp_path / "v"
    Vault.scaffold(root)
    store = InMemoryStore()
    for node_id in ("alice", "bob", "carol"):
        store.add_node(Node(id=node_id, type="Agent", title=node_id))
    store.add_edge(Edge(type="wrote_to", src="alice", dst="bob"))
    store.add_edge(Edge(type="wrote_to", src="carol", dst="bob"))
    store.add_edge(Edge(type="sent_to", src="alice", dst="bob"))
    store.add_edge(Edge(type="sent_to", src="carol", dst="bob"))

    monkeypatch.setattr(
        _curation,
        "_build_predicate_judge",
        lambda state, **kwargs: _FakePredicateJudge(),
    )

    vault = Vault(root, store, embedder=_ClusterEmbedder())
    state = init_state(vault, vault.path)
    app = build_rest_app(state)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c
    reset_state_for_tests()


def _wait_job(client: TestClient, kind: str, job_id: str) -> dict:
    for _ in range(200):
        body = client.get(f"/api/v1/curation/jobs?kind={kind}").json()
        job = next((item for item in body["jobs"] if item["id"] == job_id), None)
        if job and job["status"] in ("done", "error"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"{kind} job did not finish: {job_id}")


def _seed_authority(client: TestClient) -> None:
    """Write an off-graph equivalence record folding c:alpha2 -> c:alpha."""
    from okto_neuron.server.state import get_state

    index = _curation.authority_index(get_state())
    index.upsert(
        AuthorityRecord(
            cluster_id="cl:1",
            canonical_id="c:alpha",
            canonical_name="Alpha",
            member_ids=("c:alpha", "c:alpha2"),
            variants=("Alpha (dup)",),
            exact_match_pairs=(("c:alpha", "c:alpha2"),),
            confidence=0.95,
        )
    )


def _seed_reconcile_queue(client: TestClient, cluster_id: str = "cl:q1") -> None:
    """Park a QueuedCluster in the off-graph reconcile review queue (no LLM).

    Mirrors how the apply runner would queue a sub-threshold cluster, but seeded
    directly so the confirm/reject HTTP round-trips have something to act on —
    same construction shape as tests/reconcile/test_queue.py."""
    from okto_neuron.reconcile.candidates import CandidateCluster
    from okto_neuron.reconcile.propose import ClusterVerdict
    from okto_neuron.server.state import get_state

    queue = _curation.reconcile_queue(get_state())
    cluster = CandidateCluster(
        cluster_id=cluster_id,
        type="Concept",
        member_ids=("c:alpha", "c:alpha2"),
        lane_evidence={"lexical": ["Alpha <-> Alpha (dup)"]},
    )
    verdict = ClusterVerdict(
        cluster_id=cluster_id,
        same=True,
        confidence=0.85,  # below auto floor → parked for review
        canonical_id="c:alpha",
        member_ids=("c:alpha", "c:alpha2"),
        corroboration="lexical",
        reason="probable duplicate",
    )
    queue.enqueue(
        cluster,
        verdict,
        correlations={"titles": {"c:alpha": "Alpha", "c:alpha2": "Alpha (dup)"}},
    )


# ── authority surface ──────────────────────────────────────────────────────────


def test_authority_list_empty(client: TestClient) -> None:
    r = client.get("/api/v1/authority")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "records": []}


def test_authority_list_then_unmerge(client: TestClient) -> None:
    _seed_authority(client)
    r = client.get("/api/v1/authority")
    assert r.status_code == 200
    recs = r.json()["records"]
    assert len(recs) == 1
    assert recs[0]["cluster_id"] == "cl:1"
    assert recs[0]["canonical_id"] == "c:alpha"

    # Un-merge is reversible: drop the record, the fold vanishes.
    r2 = client.post("/api/v1/authority/unmerge", json={"cluster_id": "cl:1"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["removed"] is True
    assert client.get("/api/v1/authority").json()["records"] == []


def test_authority_unmerge_missing_is_404(client: TestClient) -> None:
    r = client.post("/api/v1/authority/unmerge", json={"cluster_id": "nope"})
    assert r.status_code == 404, r.text


# ── reconcile queue + status ───────────────────────────────────────────────────


def test_reconcile_queue_empty(client: TestClient) -> None:
    r = client.get("/api/v1/reconcile/queue")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "entries": []}


def test_reconcile_status_shape(client: TestClient) -> None:
    _seed_authority(client)
    r = client.get("/api/v1/reconcile/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["authority_count"] == 1
    assert body["queue_count"] == 0
    assert "last_propose" in body and "last_apply" in body


def test_reconcile_status_unknown_job_404(client: TestClient) -> None:
    r = client.get("/api/v1/reconcile/status?job_id=missing")
    assert r.status_code == 404, r.text


# ── propose: submit → poll to terminal ──────────────────────────────────────────


def test_reconcile_propose_submit_polls_to_terminal(client: TestClient, monkeypatch) -> None:
    """POST /reconcile/propose enqueues a READ-ONLY job on the daemon queue and
    returns a job id; polling /reconcile/status?job_id= reaches a terminal state.
    No LLM: clustering is stubbed to empty (the lifecycle is under test here, not
    the candidate logic — that lives in tests/reconcile/test_candidates.py)."""
    import time

    from okto_neuron.reconcile import candidates as cand_mod
    from okto_neuron.server.state import get_state

    # run_propose imports generate_candidate_clusters at call time, so patching the
    # module attribute is enough to keep this model-free and instant.
    monkeypatch.setattr(cand_mod, "generate_candidate_clusters", lambda *a, **k: [])

    store = get_state().vault.store
    nodes_before = len(list(store.list_nodes()))
    edges_before = len(list(store.list_edges()))

    r = client.post("/api/v1/reconcile/propose", json={})
    assert r.status_code == 200, r.text
    job = r.json()["job"]
    assert job["kind"] == "reconcile-propose"
    assert job["status"] in ("queued", "running")
    job_id = job["id"]

    final = None
    for _ in range(200):
        body = client.get(f"/api/v1/reconcile/status?job_id={job_id}").json()
        if body["job"]["status"] in ("done", "error"):
            final = body["job"]
            break
        time.sleep(0.02)
    assert final is not None, "propose job did not finish"
    assert final["status"] == "done", final.get("error")
    # Read-only: zero clusters in, propose writes nothing to the graph.
    assert final["result"]["clusters"] == []
    assert final["result"]["count"] == 0
    outcome = final["result"]["outcome"]
    assert {key: outcome[key] for key in ("state", "stage", "trigger", "clusters")} == {
        "state": "complete",
        "stage": "propose",
        "trigger": "manual",
        "clusters": 0,
    }
    assert outcome["job_id"] == job_id
    assert outcome["graph_generation"]
    assert final["result"]["construction_cost"] == {
        "schema_version": "construction_cost.v1",
        "status": "measured",
        "embedding_calls": 0,
        "embedding_calls_with_usage": 0,
        "embedding_inputs": 0,
        "completion_calls": 0,
        "completion_calls_with_usage": 0,
        "input_tokens": 0,
        "output_tokens": 0,
    }
    assert len(list(store.list_nodes())) == nodes_before
    assert len(list(store.list_edges())) == edges_before


# ── review/confirm + review/reject round-trips ──────────────────────────────────


def test_reconcile_review_confirm_promotes_to_authority(client: TestClient) -> None:
    """A parked cluster confirmed via POST /reconcile/review/confirm moves to the
    off-graph AuthorityIndex and leaves the review queue. Model-free: confirm only
    moves JSON side-files (judge_model is a config string, never a call)."""
    _seed_reconcile_queue(client)
    # Present in the review queue before confirm.
    q = client.get("/api/v1/reconcile/queue").json()["entries"]
    assert any(e["cluster_id"] == "cl:q1" for e in q)
    assert client.get("/api/v1/authority").json()["records"] == []

    r = client.post("/api/v1/reconcile/review/confirm", json={"cluster_id": "cl:q1"})
    assert r.status_code == 200, r.text
    rec = r.json()["record"]
    assert rec["canonical_id"] == "c:alpha"
    assert "c:alpha2" in rec["member_ids"]

    # Dequeued from review, present in authority.
    assert client.get("/api/v1/reconcile/queue").json()["entries"] == []
    recs = client.get("/api/v1/authority").json()["records"]
    assert any(rr["canonical_id"] == "c:alpha" for rr in recs)


def test_reconcile_review_confirm_missing_is_404(client: TestClient) -> None:
    r = client.post("/api/v1/reconcile/review/confirm", json={"cluster_id": "nope"})
    assert r.status_code == 404, r.text


def test_reconcile_review_reject_drops_without_authority(client: TestClient) -> None:
    """POST /reconcile/review/reject dequeues a parked cluster and writes NOTHING
    to the authority index (no equivalence is created)."""
    _seed_reconcile_queue(client)
    assert client.get("/api/v1/reconcile/queue").json()["entries"]

    r = client.post("/api/v1/reconcile/review/reject", json={"cluster_id": "cl:q1"})
    assert r.status_code == 200, r.text
    assert r.json()["cluster_id"] == "cl:q1"

    # Gone from review, and authority stays empty (reject never promotes).
    assert client.get("/api/v1/reconcile/queue").json()["entries"] == []
    assert client.get("/api/v1/authority").json()["records"] == []


def test_reconcile_review_confirm_then_folds_browse(client: TestClient) -> None:
    """End-to-end: confirming a parked cluster makes the read-time fold visible —
    the variant disappears from Browse and the canonical carries the badge count.
    Ties the confirm HTTP path to the equivalence fold the frontend renders."""
    _seed_reconcile_queue(client)
    client.post("/api/v1/reconcile/review/confirm", json={"cluster_id": "cl:q1"})

    body = client.get("/api/v1/nodes?type=Concept").json()
    ids = {n["id"] for n in body["nodes"]}
    assert "c:alpha2" not in ids  # variant folded away
    canonical = next(n for n in body["nodes"] if n["id"] == "c:alpha")
    assert canonical.get("variant_count") == 1


# ── read-time equivalence fold over Browse/Graph ────────────────────────────────


def test_nodes_list_folds_variant(client: TestClient) -> None:
    # Before reconcile: all three concepts visible.
    before = {n["id"] for n in client.get("/api/v1/nodes?type=Concept").json()["nodes"]}
    assert {"c:alpha", "c:alpha2", "c:beta"} <= before

    _seed_authority(client)
    body = client.get("/api/v1/nodes?type=Concept").json()
    ids = {n["id"] for n in body["nodes"]}
    # Variant c:alpha2 folded onto c:alpha → gone from the list; canonical badged.
    assert "c:alpha2" not in ids
    assert {"c:alpha", "c:beta"} <= ids
    canonical = next(n for n in body["nodes"] if n["id"] == "c:alpha")
    assert canonical.get("variant_count") == 1


def test_node_detail_annotates_canonical(client: TestClient) -> None:
    _seed_authority(client)
    r = client.get("/api/v1/nodes/c:alpha2")
    assert r.status_code == 200, r.text
    assert r.json()["canonical_id"] == "c:alpha"
    # The canonical itself is not a variant.
    r2 = client.get("/api/v1/nodes/c:alpha")
    assert r2.json()["canonical_id"] is None


def test_graph_folds_and_remaps_edges(client: TestClient) -> None:
    _seed_authority(client)
    body = client.get("/api/v1/graph").json()
    node_ids = {n["id"] for n in body["nodes"]}
    assert "c:alpha2" not in node_ids  # variant collapsed
    assert {"c:alpha", "c:beta"} <= node_ids
    # Both alpha->beta and alpha2->beta remap to alpha->beta and dedupe to ONE edge.
    edges = [(e["src"], e["dst"], e["type"]) for e in body["edges"]]
    assert edges.count(("c:alpha", "c:beta", "skos:related")) == 1
    assert all(e[0] != "c:alpha2" and e[1] != "c:alpha2" for e in edges)


# ── apply runs on the daemon queue and stays OFF-GRAPH ──────────────────────────


def test_apply_job_is_off_graph(client: TestClient, monkeypatch) -> None:
    """The /reconcile/apply job runs through the in-process curation queue on the
    daemon's handle and writes ZERO graph nodes/edges (off-graph invariant). Judge
    is stubbed so no LLM is needed; we assert the live store's node/edge counts are
    unchanged after the job completes."""
    import time

    from okto_neuron.resolve import MergeVerdict
    from okto_neuron.server import _curation
    from okto_neuron.server.state import get_state

    class _StubJudge:
        def judge(self, candidate, existing, *, candidate_context="", existing_context=""):
            return MergeVerdict(same=True, confidence=0.95)

    monkeypatch.setattr(_curation, "_build_judge", lambda state: _StubJudge())

    store = get_state().vault.store
    nodes_before = len(list(store.list_nodes()))
    edges_before = len(list(store.list_edges()))

    r = client.post("/api/v1/reconcile/apply", json={})
    assert r.status_code == 200, r.text
    job_id = r.json()["job"]["id"]

    # Poll the job to completion via the status endpoint (the daemon never went down).
    final = None
    for _ in range(200):
        body = client.get(f"/api/v1/reconcile/status?job_id={job_id}").json()
        if body["job"]["status"] in ("done", "error"):
            final = body["job"]
            break
        time.sleep(0.02)
    assert final is not None, "apply job did not finish"
    assert final["status"] == "done", final.get("error")

    # OFF-GRAPH: store topology unchanged. (Authority/queue JSON side-files may
    # have changed, but the graph must not.)
    assert len(list(store.list_nodes())) == nodes_before
    assert len(list(store.list_edges())) == edges_before


def test_predicate_upkeep_propose_apply_confirm_reject_flow(
    predicate_client: TestClient,
) -> None:
    snapshot = predicate_client.get("/api/v1/upkeep/predicates")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["vocabulary_size"] == 2

    propose = predicate_client.post("/api/v1/upkeep/predicates/propose", json={})
    assert propose.status_code == 200, propose.text
    propose_job = _wait_job(
        predicate_client,
        "predicate-propose",
        propose.json()["job"]["id"],
    )
    assert propose_job["status"] == "done", propose_job.get("error")
    result = propose_job["result"]
    assert result["pairs_considered"] == 1
    assert result["judged"] == 1
    assert result["queued"] == 1
    assert result["outcomes"][0]["duration_s"] == 0.03
    assert result["outcomes"][0]["usage"] == {"total_tokens": 7}

    # Propose-only: the ledger is still empty until apply runs.
    assert predicate_client.get("/api/v1/upkeep/predicates").json()["counts"]["queued"] == 0

    apply = predicate_client.post(
        "/api/v1/upkeep/predicates/apply",
        json={"job_id": propose_job["id"]},
    )
    assert apply.status_code == 200, apply.text
    apply_job = _wait_job(
        predicate_client,
        "predicate-apply",
        apply.json()["job"]["id"],
    )
    assert apply_job["status"] == "done", apply_job.get("error")
    assert apply_job["result"]["counts"]["queued"] == 1

    after_apply = predicate_client.get("/api/v1/upkeep/predicates").json()
    queued = after_apply["records"]["queued"]
    assert len(queued) == 1
    record_id = queued[0]["id"]

    confirm = predicate_client.post(f"/api/v1/upkeep/predicates/{record_id}/confirm", json={})
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["record"]["status"] == "confirmed"
    after_confirm = predicate_client.get("/api/v1/upkeep/predicates").json()
    assert after_confirm["counts"]["confirmed"] == 1
    assert after_confirm["counts"]["queued"] == 0

    reject = predicate_client.post(f"/api/v1/upkeep/predicates/{record_id}/reject", json={})
    assert reject.status_code == 200, reject.text
    assert reject.json()["record"]["status"] == "rejected"
    after_reject = predicate_client.get("/api/v1/upkeep/predicates").json()
    assert after_reject["counts"]["rejected"] == 1
    assert after_reject["counts"]["confirmed"] == 0


# ── fail-fast writer_lock (live bug: confirm/reject hung behind an in-flight
#    ingest/rebuild/heal/reembed/companion-triage job holding writer_lock for
#    minutes to over an hour) ─────────────────────────────────────────────────
#
# reconcile/predicate confirm-reject and authority-unmerge are single off-graph
# JSON writes (AuthorityIndex / PredicateAliasIndex / ReconcileQueue side-files)
# — they still need writer_lock (the reconcile-apply/predicate-apply curation
# JOBS write the SAME side-files under writer_lock, so a second dedicated lock
# would let a confirm/reject race an in-flight apply's write), but must fail
# fast (503) instead of hanging the UI. Proven the same way as the config_lock
# regressions in test_server_api_v1.py: hold writer_lock for the WHOLE handler
# call — a hang would mean the handler still depends on an unbounded acquire.


@contextlib.asynccontextmanager
async def _always_busy(state, *, timeout: float = 0.0):  # noqa: ANN001, ARG001
    """Drop-in replacement for ``http_mod._writer_lock_fast`` that raises
    ``_LockBusy`` before ever yielding — simulates writer_lock being held by an
    in-flight ingest/rebuild/heal/reembed job without any real contention, on a
    single event loop (the TestClient's own portal loop). No second thread, no
    ``asyncio.run`` on a fresh loop, no ``state.writer_lock`` touched directly."""
    raise http_mod._LockBusy
    yield  # pragma: no cover - unreachable, satisfies asynccontextmanager shape


def test_reconcile_confirm_fails_fast_when_writer_lock_busy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_reconcile_queue(client)

    monkeypatch.setattr(http_mod, "_writer_lock_fast", _always_busy)
    busy = client.post("/api/v1/reconcile/review/confirm", json={"cluster_id": "cl:q1"})
    assert busy.status_code == 503, busy.text
    assert busy.json()["error"] == "busy"

    # Lock free again — the SAME request must now go through for real (no leak).
    monkeypatch.undo()
    free = client.post("/api/v1/reconcile/review/confirm", json={"cluster_id": "cl:q1"})
    assert free.status_code == 200, free.text


def test_reconcile_reject_fails_fast_when_writer_lock_busy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_reconcile_queue(client)

    monkeypatch.setattr(http_mod, "_writer_lock_fast", _always_busy)
    busy = client.post("/api/v1/reconcile/review/reject", json={"cluster_id": "cl:q1"})
    assert busy.status_code == 503, busy.text
    assert busy.json()["error"] == "busy"

    monkeypatch.undo()
    free = client.post("/api/v1/reconcile/review/reject", json={"cluster_id": "cl:q1"})
    assert free.status_code == 200, free.text


def test_predicate_confirm_fails_fast_when_writer_lock_busy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http_mod, "_writer_lock_fast", _always_busy)
    busy = client.post("/api/v1/upkeep/predicates/does-not-exist/confirm", json={})
    assert busy.status_code == 503, busy.text
    assert busy.json()["error"] == "busy"

    # Lock free again → the real lookup runs (404 for an unknown id — proves it
    # is no longer "busy", i.e. no leaked lock from the busy path above).
    monkeypatch.undo()
    free = client.post("/api/v1/upkeep/predicates/does-not-exist/confirm", json={})
    assert free.status_code == 404, free.text


def test_predicate_reject_fails_fast_when_writer_lock_busy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http_mod, "_writer_lock_fast", _always_busy)
    busy = client.post("/api/v1/upkeep/predicates/does-not-exist/reject", json={})
    assert busy.status_code == 503, busy.text
    assert busy.json()["error"] == "busy"

    monkeypatch.undo()
    free = client.post("/api/v1/upkeep/predicates/does-not-exist/reject", json={})
    assert free.status_code == 404, free.text


def test_authority_unmerge_fails_fast_when_writer_lock_busy(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_authority(client)

    monkeypatch.setattr(http_mod, "_writer_lock_fast", _always_busy)
    busy = client.post("/api/v1/authority/unmerge", json={"cluster_id": "cl:1"})
    assert busy.status_code == 503, busy.text
    assert busy.json()["error"] == "busy"

    monkeypatch.undo()
    free = client.post("/api/v1/authority/unmerge", json={"cluster_id": "cl:1"})
    assert free.status_code == 200, free.text


# ── loopback gate ──────────────────────────────────────────────────────────────


def test_reconcile_gate_blocks_non_loopback() -> None:
    """A non-loopback caller is rejected at the reconcile gate. Uses a fresh app
    on a non-loopback base_url; the LoopbackHostMiddleware + caller check apply."""
    import tempfile

    reset_state_for_tests()
    with tempfile.TemporaryDirectory() as d:
        vault = Vault.init(Path(d) / "v")
        state = init_state(vault, vault.path)
        app = build_rest_app(state)
        try:
            with TestClient(app, base_url="http://10.0.0.5") as c:
                r = c.get("/api/v1/authority")
                # Either the Host-header middleware (403) or the caller gate rejects.
                assert r.status_code == 403, r.text
        finally:
            reset_state_for_tests()
            for s in list(vault_module._STORE_CACHE.values()):
                s.close()
            vault_module._STORE_CACHE.clear()
            VaultConnection.close_all()
