"""HTTP tests for ``POST /api/v1/reset`` (the web-UI "Start fresh" surface).

Two gates and one happy path, all model-free (StubLLM companion, real
InMemory-backed vault populated directly — the proven offline path from
test_server_api_v1.py):

  * Peer gate — scenario 93's spoofed-Host test only exercises the Host
    middleware (``forbidden_host``). Reset must ALSO be loopback-only at the
    ``remote_config_allowed`` peer layer (``forbidden_remote``); we prove the
    route consults that gate by monkeypatching the helper to deny.
  * Happy path — a loopback reset returns the LOCKED shape
    ``{"status":"ok","wiped":true}`` and empties the graph (every node-type
    count drops to 0).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from okto_neuron import Vault
from okto_neuron.companion import Companion
from okto_neuron.core.schema import Edge, Node
from okto_neuron.llm import StubLLM
from okto_neuron.server import _ingest_queue as iq
from okto_neuron.server import _jobs
from okto_neuron.server import http as http_mod
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    reset_state_for_tests()
    vault = Vault.init(tmp_path / "v")
    note = Path(vault.path) / "note.md"
    note.write_text("# Title\n\nbody about knowledge graphs.\n", encoding="utf-8")

    # Populate the store directly with a closed-schema fixture graph so the
    # reset has real nodes to clear.
    store = vault.store
    block = Node(
        id="block:1",
        type="Block",
        title="block one",
        facets={
            "path": "note.md",
            "byte_start": 10,
            "byte_end": 42,
            "content_hash": "sha256:" + "a" * 64,
            "document_id": "doc:1",
        },
    )
    claim = Node(
        id="claim:1",
        type="Claim",
        title="turing worked at bletchley",
        facets={
            "block_id": "block:1",
            "document_id": "doc:1",
            "extraction_activity_id": "act:1",
            "agent_id": "agent:1",
        },
    )
    concept = Node(id="concept:1", type="Concept", title="knowledge graph")
    schema_meta = Node(id="__meta__", type="SchemaMetadata", title="internal")
    for n in (block, claim, concept, schema_meta):
        store.add_node(n)
    store.add_edge(Edge(id="e1", type="prov:wasDerivedFrom", src="claim:1", dst="block:1"))
    store.add_edge(Edge(id="e2", type="skos:broader", src="concept:1", dst="claim:1"))

    monkeypatch.setattr(
        http_mod, "_companion", lambda state: Companion(state.vault, provider=StubLLM())
    )

    state = init_state(vault, vault.path)
    app = build_rest_app(state)
    # base_url loopback so the Host guard (L1) lets requests through.
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.vault_path = str(vault.path)  # type: ignore[attr-defined]
        yield c
    reset_state_for_tests()
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


# ── peer gate: reset is loopback-ONLY at the remote_config_allowed layer ──────


def test_reset_denied_when_remote_peer(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A remote peer is refused reset even when the Host gate would pass.

    This is the gate scenario 93's spoofed-Host test can't reach (the loopback
    socket peer is always 127.0.0.1). We force the peer helper to deny and prove
    the route honors it — a different layer (forbidden_remote) than the Host
    middleware (forbidden_host).
    """
    monkeypatch.setattr(http_mod, "remote_config_allowed", lambda request: False)
    r = client.post("/api/v1/reset", json={})
    assert r.status_code == 403, r.text
    # Mirror the config-PATCH peer-deny code (_err(403, "forbidden", ...)).
    # Asserted exactly on the JSON `error` field to disambiguate from the Host
    # middleware's "forbidden_host" (a different, earlier-firing gate).
    assert r.json()["error"] == "forbidden", r.text


def test_reset_allowed_for_loopback_peer(client: TestClient) -> None:
    """The default loopback caller passes the peer gate (allow path)."""
    r = client.post("/api/v1/reset", json={})
    assert r.status_code == 200, r.text


def test_reset_refuses_active_ingest(client: TestClient) -> None:
    state = http_mod.get_state()
    state.ingest_queue = [
        iq.IngestItem(
            id="queued",
            name="queued.md",
            path=str(Path(client.vault_path) / "queued.md"),  # type: ignore[attr-defined]
            status="queued",
        )
    ]

    r = client.post("/api/v1/reset", json={})

    assert r.status_code == 409, r.text
    assert r.json()["error"] == "busy"
    assert "ingest is active" in r.json()["detail"]


# ── happy path: locked response shape + empties the graph ─────────────────────


def test_reset_returns_locked_shape_and_empties_graph(client: TestClient) -> None:
    # Pre-condition: there are nodes to clear.
    pre = client.get("/api/v1/node-types")
    assert pre.status_code == 200, pre.text
    pre_counts = {t["name"]: t["count"] for t in pre.json()["types"]}
    assert pre_counts["Claim"] == 1
    assert pre_counts["Concept"] == 1

    r = client.post("/api/v1/reset", json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "wiped": True}

    post = client.get("/api/v1/node-types")
    assert post.status_code == 200, post.text
    post_counts = {t["name"]: t["count"] for t in post.json()["types"]}
    # Every closed node-type count is now zero — the graph is empty.
    assert all(count == 0 for count in post_counts.values()), post_counts


def test_reset_removes_derived_sidecars_and_clears_live_state(client: TestClient) -> None:
    vault_path = Path(client.vault_path)  # type: ignore[attr-defined]
    marginalia_dir = vault_path / ".marginalia"
    stale_files = [
        marginalia_dir / "sources" / "imported.md",
        marginalia_dir / "ingest-history.json",
        marginalia_dir / "review_queue.json",
        marginalia_dir / "candidate-ledger.jsonl",
        marginalia_dir / "curation-jobs.json",
        marginalia_dir / "rebuild.state.json",
        marginalia_dir / "reembed.state.json",
        marginalia_dir / "authority" / "index.json",
        marginalia_dir / "reconcile" / "queue.json",
        marginalia_dir / "unknown-derived.bin",
    ]
    for path in stale_files:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stale", encoding="utf-8")

    state = http_mod.get_state()
    state.ingest_queue = [
        iq.IngestItem(
            id="done",
            name="done.md",
            path=str(vault_path / "done.md"),
            status="done",
        )
    ]
    state.curation_jobs = [_jobs.CurationJob(id="done", kind="reconcile-propose", status="done")]
    state.last_ingest_at = 1.0
    state.last_sweep_at = 2.0
    state.last_sweep_outcome = {"reason": "stale"}
    state.vault_open_error = {"error": "stale"}

    r = client.post("/api/v1/reset", json={})

    assert r.status_code == 200, r.text
    for path in stale_files:
        assert not path.exists(), path
    assert state.ingest_queue == []
    assert state.curation_jobs == []
    assert state.last_ingest_at is None
    assert state.last_sweep_at is None
    assert state.last_sweep_outcome is None
    assert state.vault_open_error is None
