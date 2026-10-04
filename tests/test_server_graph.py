"""HTTP tests for the graph-visualization surface: /api/v1/graph,
/api/v1/nodes/{id}/neighbors, /api/v1/graph/stats.

All tests run against an InMemory-backed vault populated directly with a
closed-schema fixture graph (no LLM, fully offline + deterministic). The fixture
is shaped to exercise degree ranking, the cap + ``truncated`` flag, and the
edges-only-between-returned-nodes invariant.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from okto_neuron import Vault
from okto_neuron.core.schema import Edge, Node
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests
from tests._settled import get_settled
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    reset_state_for_tests()
    vault = Vault.init(tmp_path / "v")
    store = vault.store

    # Star topology so degree ordering is unambiguous:
    #   hub (Concept) connects to leaf0..leaf4 (Concept) via "mentions"  → degree 5
    #   leafN connects to its own tag (Agent) via "skos:related"          → degree 2
    #   tags have degree 1; the hub is the clear top node by degree.
    # Plus an isolated Place (degree 0) to exercise min_degree filtering,
    # and a hidden SchemaMetadata node that must never leak.
    hub = Node(id="hub", type="Concept", title="hub concept")
    store.add_node(hub)
    store.add_node(Node(id="island", type="Place", title="isolated place"))
    store.add_node(Node(id="__meta__", type="SchemaMetadata", title="internal"))

    leaves = []
    for i in range(5):
        leaf = Node(id=f"leaf{i}", type="Concept", title=f"leaf {i}")
        tag = Node(id=f"tag{i}", type="Agent", title=f"tag {i}")
        store.add_node(leaf)
        store.add_node(tag)
        leaves.append((leaf, tag))
        store.add_edge(Edge(id=f"hub-leaf{i}", type="mentions", src="hub", dst=f"leaf{i}"))
        store.add_edge(
            Edge(id=f"leaf{i}-tag{i}", type="skos:related", src=f"leaf{i}", dst=f"tag{i}")
        )
    # A same-ring edge between two 1-hop neighbors of the hub. This is the case
    # the neighbors BFS would otherwise drop at the outermost ring (most visible
    # at hops=1): it must surface in the seed's 1-hop neighborhood.
    store.add_edge(Edge(id="leaf0-leaf1", type="skos:related", src="leaf0", dst="leaf1"))

    state = init_state(vault, vault.path)
    app = build_rest_app(state)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c
    reset_state_for_tests()
    for s in list(vault_module._STORE_CACHE.values()):
        s.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


# ── /api/v1/graph ─────────────────────────────────────────────────────────────


def test_graph_full_shape(client: TestClient) -> None:
    r = client.get("/api/v1/graph")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert {
        "truncated",
        "total_nodes",
        "total_edges",
        "returned_nodes",
        "returned_edges",
        "nodes",
        "edges",
    } <= set(body)
    # 1 hub + 5 leaves + 5 tags + 1 island = 12 visible nodes; meta hidden.
    assert body["total_nodes"] == 12
    assert body["total_edges"] == 11  # 5 mentions + 5 leaf→tag + 1 leaf0→leaf1
    assert body["truncated"] is False
    ids = {n["id"] for n in body["nodes"]}
    assert "__meta__" not in ids  # internal node never leaks
    # hub has the highest degree and carries it.
    hub = next(n for n in body["nodes"] if n["id"] == "hub")
    assert hub["degree"] == 5


def test_graph_capping_and_truncated(client: TestClient) -> None:
    # limit=3 → top-3 by degree: hub(5), then two leaves(2) by id tiebreak.
    r = client.get("/api/v1/graph?limit=3")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_nodes"] == 12
    assert body["returned_nodes"] == 3
    assert body["truncated"] is True
    ids = [n["id"] for n in body["nodes"]]
    assert ids[0] == "hub"  # highest degree first
    assert set(ids) == {"hub", "leaf0", "leaf1"}  # degree-then-id tiebreak


def test_graph_edges_only_between_returned_nodes(client: TestClient) -> None:
    # With limit=3 the returned set is {hub, leaf0, leaf1}. Edges with BOTH
    # endpoints surviving are hub→leaf0, hub→leaf1, and leaf0→leaf1 (the leaf→tag
    # edges drop because the tags are cut). Critically, hub.degree (5, full
    # subgraph) exceeds its shown-edge count (2) — degree is computed before the
    # cap, so a high-degree node can show fewer edges than its degree.
    r = client.get("/api/v1/graph?limit=3")
    body = r.json()
    returned_ids = {n["id"] for n in body["nodes"]}
    for e in body["edges"]:
        assert e["src"] in returned_ids and e["dst"] in returned_ids
    edge_pairs = {(e["src"], e["dst"]) for e in body["edges"]}
    assert edge_pairs == {("hub", "leaf0"), ("hub", "leaf1"), ("leaf0", "leaf1")}
    hub = next(n for n in body["nodes"] if n["id"] == "hub")
    shown_for_hub = sum(1 for e in body["edges"] if "hub" in (e["src"], e["dst"]))
    assert hub["degree"] == 5  # full-subgraph degree
    assert shown_for_hub == 2  # but only 2 edges shown (high-degree neighbors cut)
    assert hub["degree"] > shown_for_hub


def test_graph_type_filter(client: TestClient) -> None:
    # Only Concept nodes → hub + 5 leaves; tags (Agent) excluded, so leaf→tag
    # edges vanish (one endpoint gone). The 5 hub→leaf mentions survive plus the
    # leaf0→leaf1 same-ring edge (both Concept).
    r = client.get("/api/v1/graph?types=Concept")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {n["type"] for n in body["nodes"]} == {"Concept"}
    assert body["total_nodes"] == 6
    assert body["total_edges"] == 6  # 5 hub→leaf mentions + leaf0→leaf1
    edge_pairs = {(e["src"], e["dst"]) for e in body["edges"]}
    assert ("leaf0", "leaf1") in edge_pairs


def test_graph_relation_filter(client: TestClient) -> None:
    r = client.get("/api/v1/graph?relations=skos:related")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_edges"] == 6  # 5 leaf→tag + leaf0→leaf1
    assert all(e["type"] == "skos:related" for e in body["edges"])
    # hub now has degree 0 within this relation-filtered subgraph.
    hub = next(n for n in body["nodes"] if n["id"] == "hub")
    assert hub["degree"] == 0


def test_graph_min_degree(client: TestClient) -> None:
    # min_degree=2 keeps hub(5) + 5 leaves(2 each) = 6; drops tags(1) + island(0).
    r = client.get("/api/v1/graph?min_degree=2")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_nodes"] == 6
    ids = {n["id"] for n in body["nodes"]}
    assert "island" not in ids
    assert all(n["degree"] >= 2 for n in body["nodes"])


def test_graph_unknown_type_400(client: TestClient) -> None:
    r = client.get("/api/v1/graph?types=Bogus")
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_graph_limit_clamps_not_400(client: TestClient) -> None:
    # Oversize limit clamps to the hard cap rather than erroring.
    r = client.get("/api/v1/graph?limit=999999")
    assert r.status_code == 200, r.text
    assert r.json()["returned_nodes"] == 12  # all visible nodes, no error


def test_graph_bad_limit_400(client: TestClient) -> None:
    assert client.get("/api/v1/graph?limit=abc").status_code == 400
    assert client.get("/api/v1/graph?limit=0").status_code == 400
    assert client.get("/api/v1/graph?min_degree=-1").status_code == 400


# ── /api/v1/nodes/{id}/neighbors ──────────────────────────────────────────────


def test_neighbors_one_hop(client: TestClient) -> None:
    r = client.get("/api/v1/nodes/hub/neighbors")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["seed"] == "hub"
    ids = {n["id"] for n in body["nodes"]}
    # 1 hop from hub: hub + 5 leaves.
    assert ids == {"hub", "leaf0", "leaf1", "leaf2", "leaf3", "leaf4"}
    # All edges between returned nodes; hub degree within the returned set is 5.
    hub = next(n for n in body["nodes"] if n["id"] == "hub")
    assert hub["degree"] == 5
    for e in body["edges"]:
        assert e["src"] in ids and e["dst"] in ids
    # The same-ring edge between two 1-hop neighbors MUST surface (the closing
    # pass over the outermost frontier) — without it, hops=1 would show the
    # neighbors with no edges among them.
    edge_pairs = {(e["src"], e["dst"]) for e in body["edges"]}
    assert ("leaf0", "leaf1") in edge_pairs
    # leaf0 within the returned set: hub + leaf1 = 2 (its tag0 is NOT in the
    # 1-hop set, so the leaf0→tag0 edge is absent here).
    leaf0 = next(n for n in body["nodes"] if n["id"] == "leaf0")
    assert leaf0["degree"] == 2


def test_neighbors_two_hops(client: TestClient) -> None:
    r = client.get("/api/v1/nodes/hub/neighbors?hops=2")
    assert r.status_code == 200, r.text
    body = r.json()
    ids = {n["id"] for n in body["nodes"]}
    # 2 hops: hub + 5 leaves + 5 tags = 11 (the island stays unreachable).
    assert ids == {
        "hub",
        "leaf0",
        "leaf1",
        "leaf2",
        "leaf3",
        "leaf4",
        "tag0",
        "tag1",
        "tag2",
        "tag3",
        "tag4",
    }
    assert "island" not in ids
    # Degree within the returned set: leaf0 touches hub + tag0 + leaf1 = 3
    # (the leaf0→leaf1 same-ring edge is included), while leaf2 touches just
    # hub + tag2 = 2.
    leaf0 = next(n for n in body["nodes"] if n["id"] == "leaf0")
    assert leaf0["degree"] == 3
    leaf2 = next(n for n in body["nodes"] if n["id"] == "leaf2")
    assert leaf2["degree"] == 2


def test_neighbors_relation_filter(client: TestClient) -> None:
    # Restrict to mentions only → from hub we reach leaves but never the tags.
    r = client.get("/api/v1/nodes/hub/neighbors?hops=2&relations=mentions")
    assert r.status_code == 200, r.text
    ids = {n["id"] for n in r.json()["nodes"]}
    assert ids == {"hub", "leaf0", "leaf1", "leaf2", "leaf3", "leaf4"}


def test_neighbors_404_missing_seed(client: TestClient) -> None:
    r = client.get("/api/v1/nodes/nope:99/neighbors")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_neighbors_404_hidden_seed(client: TestClient) -> None:
    # Internal nodes are hidden — expanding from one mirrors api_node_detail's 404.
    r = client.get("/api/v1/nodes/__meta__/neighbors")
    assert r.status_code == 404


def test_neighbors_hops_clamp_not_400(client: TestClient) -> None:
    r = client.get("/api/v1/nodes/hub/neighbors?hops=99")
    assert r.status_code == 200, r.text  # clamps to NEIGHBORS_MAX_HOPS


def test_neighbors_bad_hops_400(client: TestClient) -> None:
    assert client.get("/api/v1/nodes/hub/neighbors?hops=abc").status_code == 400
    assert client.get("/api/v1/nodes/hub/neighbors?hops=0").status_code == 400


# ── /api/v1/graph/stats ───────────────────────────────────────────────────────


def test_graph_stats_counts(client: TestClient) -> None:
    r = get_settled(client, "/api/v1/graph/stats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    node_counts = {t["type"]: t["count"] for t in body["node_types"]}
    assert node_counts["Concept"] == 6  # hub + 5 leaves
    assert node_counts["Agent"] == 5  # 5 tags
    assert node_counts["Place"] == 1  # island
    assert "SchemaMetadata" not in node_counts  # internal excluded
    edge_counts = {t["type"]: t["count"] for t in body["edge_types"]}
    assert edge_counts["mentions"] == 5
    assert edge_counts["skos:related"] == 6  # 5 leaf→tag + leaf0→leaf1
    assert body["total_nodes"] == 12
    assert body["total_edges"] == 11
