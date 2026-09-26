"""ADR 0009 P3 heal — deterministic canonicalizing graph→fresh-graph copy (NO LLM).

Model-free unit tests for :func:`copy_graph_canonicalizing` on an InMemoryStore.
They prove the topology collapse the heal materializes:

  * NODE DROP — a variant whose canonical survives is dropped; the canonical and all
    unmerged nodes are kept.
  * EDGE REMAP + DEDUP + SELF-LOOP — edges are remapped through the equivalence map,
    self-loops the collapse creates are dropped, content-addressed ids are recomputed
    and deduped.
  * CLAIM FACET REMAP — a Claim's S_id/O_id facets are remapped to the canonical so
    they stay consistent with the remapped rdf:subject/rdf:object edges (O_id=None
    for literal-object claims is left untouched).
  * CLAIM DEDUP — duplicate Claim nodes with the same semantic triple collapse to
    the semantic Claim id, all old-id edge references re-point, and corroborations
    merge.
  * STALE-CANONICAL — a variant whose canonical is ABSENT from the input keeps
    itself (no dangling edge), mirroring fold_graph's repr_in_view guard.
  * IDEMPOTENT — a re-heal on an already-collapsed graph is a verbatim copy.

These run against the in-memory store only; no Ladybug, no LLM, no embedder.
"""

from __future__ import annotations

from dataclasses import replace
import subprocess
import sys

import pytest

from okto_neuron.consolidate._claim_identity import semantic_claim_id
from okto_neuron.core.schema import Edge, Node
from okto_neuron.ingest.markdown import sha256_hex
from okto_neuron.reconcile.authority import AuthorityIndex, AuthorityRecord
from okto_neuron.store.memory import InMemoryStore
from okto_neuron.store.reembed import copy_graph_canonicalizing


def _edge(type_: str, src: str, dst: str) -> Edge:
    return Edge(id=sha256_hex("edge", src, type_, dst), type=type_, src=src, dst=dst)


def test_node_drop_keeps_canonical_and_unmerged():
    nodes = [
        Node(id="canon", type="Place", title="United States"),
        Node(id="var", type="Place", title="USA"),
        Node(id="other", type="Concept", title="Trade"),
    ]
    equivalence = {"var": "canon", "canon": "canon"}
    dst = InMemoryStore()
    stats = copy_graph_canonicalizing(nodes, [], equivalence, dst)

    ids = {n.id for n in dst.list_nodes()}
    assert ids == {"canon", "other"}  # variant merged away
    assert stats["nodes_kept"] == 2
    assert stats["nodes_dropped"] == 1


def test_edge_remap_dedup_and_self_loop_drop():
    nodes = [
        Node(id="canon", type="Place", title="United States"),
        Node(id="var", type="Place", title="USA"),
        Node(id="city", type="Place", title="Austin"),
    ]
    equivalence = {"var": "canon", "canon": "canon"}
    edges = [
        # city -> var and city -> canon both collapse to city -> canon (dedup to one)
        _edge("located_in", "city", "var"),
        _edge("located_in", "city", "canon"),
        # var -> canon collapses to canon -> canon: a self-loop, dropped
        _edge("same_as", "var", "canon"),
    ]
    dst = InMemoryStore()
    stats = copy_graph_canonicalizing(nodes, edges, equivalence, dst)

    kept = list(dst.list_edges())
    assert len(kept) == 1
    e = kept[0]
    assert (e.src, e.dst, e.type) == ("city", "canon", "located_in")
    # id is content-addressed on the remapped endpoints
    assert e.id == sha256_hex("edge", "city", "located_in", "canon")
    assert stats["edges_kept"] == 1
    assert stats["self_loops"] == 1
    assert stats["dedup_collisions"] == 1


def test_claim_facet_remap_s_and_o_id():
    # A Claim node points S_id->var (entity) and O_id->var2 (entity); both must remap.
    nodes = [
        Node(id="canon", type="Place", title="United States"),
        Node(id="var", type="Place", title="USA"),
        Node(id="canon2", type="Concept", title="Trade"),
        Node(id="var2", type="Concept", title="commerce"),
        Node(
            id="claim1",
            type="Claim",
            title="USA relates_to commerce",
            facets={"S_id": "var", "P": "relates_to", "O_id": "var2"},
        ),
        # a literal-object claim: O_id is None and must stay None
        Node(
            id="claim2",
            type="Claim",
            title="USA has_name 'America'",
            facets={"S_id": "var", "P": "has_name", "O_id": None, "O_literal": "America"},
        ),
    ]
    equivalence = {
        "var": "canon",
        "canon": "canon",
        "var2": "canon2",
        "canon2": "canon2",
    }
    dst = InMemoryStore()
    copy_graph_canonicalizing(nodes, [], equivalence, dst)

    claim1_id = semantic_claim_id("canon", "relates_to", object_id="canon2")
    claim2_id = semantic_claim_id("canon", "has_name", literal="America")
    assert dst.get_node("claim1") is None
    assert dst.get_node("claim2") is None
    c1 = dst.get_node(claim1_id)
    assert c1 is not None
    assert c1.id == claim1_id
    assert c1.facets["id"] == claim1_id
    assert c1.facets["S_id"] == "canon"
    assert c1.facets["O_id"] == "canon2"
    c2 = dst.get_node(claim2_id)
    assert c2 is not None
    assert c2.id == claim2_id
    assert c2.facets["id"] == claim2_id
    assert c2.facets["S_id"] == "canon"
    assert c2.facets["O_id"] is None  # literal-object claim untouched
    assert c2.facets["O_literal"] == "America"


def test_claim_semantic_triple_dedup_repoints_edges_and_merges_corroborations():
    other_facets = {
        "S_id": "alice",
        "P": "founded",
        "O_id": "beta",
        "source_path": "doc.md",
    }
    nodes = [
        Node(id="alice", type="Agent", title="Alice"),
        Node(id="acme", type="Agent", title="Acme"),
        Node(id="beta", type="Agent", title="Beta"),
        Node(id="block:1", type="Block", title="Block 1"),
        Node(id="block:2", type="Block", title="Block 2"),
        Node(id="activity:1", type="Activity", title="Run 1"),
        Node(id="activity:2", type="Activity", title="Run 2"),
        Node(id="agent:1", type="Agent", title="Extractor"),
        Node(
            id="claim:first",
            type="Claim",
            title="Alice founded Acme",
            facets={
                "id": "claim:first",
                "S_id": "alice",
                "P": "founded",
                "O_id": "acme",
                "corroborations": 2,
            },
        ),
        Node(
            id="claim:second",
            type="Claim",
            title="Alice founded Acme",
            facets={
                "id": "claim:second",
                "S_id": "alice",
                "P": "founded",
                "O_id": "acme",
                "corroborations": 3,
            },
        ),
        Node(
            id="claim:other",
            type="Claim",
            title="Alice founded Beta",
            facets=other_facets,
        ),
    ]
    edges = [
        _edge("prov:wasDerivedFrom", "claim:first", "block:1"),
        _edge("prov:wasGeneratedBy", "claim:first", "activity:1"),
        _edge("prov:wasAttributedTo", "claim:first", "agent:1"),
        _edge("rdf:subject", "claim:first", "alice"),
        _edge("rdf:object", "claim:first", "acme"),
        _edge("prov:wasDerivedFrom", "claim:second", "block:2"),
        _edge("prov:wasGeneratedBy", "claim:second", "activity:2"),
        _edge("prov:wasAttributedTo", "claim:second", "agent:1"),
        _edge("rdf:subject", "claim:second", "alice"),
        _edge("rdf:object", "claim:second", "acme"),
        _edge("rdf:subject", "claim:other", "alice"),
        _edge("rdf:object", "claim:other", "beta"),
    ]

    dst = InMemoryStore()
    stats = copy_graph_canonicalizing(nodes, edges, None, dst)

    acme_claim_id = semantic_claim_id("alice", "founded", object_id="acme")
    beta_claim_id = semantic_claim_id("alice", "founded", object_id="beta")
    claims = {node.id: node for node in dst.list_nodes(type="Claim")}
    assert set(claims) == {acme_claim_id, beta_claim_id}
    assert claims[acme_claim_id].facets == {
        "id": acme_claim_id,
        "S_id": "alice",
        "P": "founded",
        "O_id": "acme",
        "corroborations": 5,
    }
    assert claims[beta_claim_id].facets == {**other_facets, "id": beta_claim_id}

    kept_edges = list(dst.list_edges())
    assert dst.get_node("claim:first") is None
    assert dst.get_node("claim:second") is None
    for edge in kept_edges:
        assert "claim:first" not in (edge.src, edge.dst)
        assert "claim:second" not in (edge.src, edge.dst)
    edge_triples = {(edge.src, edge.type, edge.dst) for edge in kept_edges}
    assert {
        (acme_claim_id, "prov:wasDerivedFrom", "block:1"),
        (acme_claim_id, "prov:wasDerivedFrom", "block:2"),
        (acme_claim_id, "prov:wasGeneratedBy", "activity:1"),
        (acme_claim_id, "prov:wasGeneratedBy", "activity:2"),
        (acme_claim_id, "prov:wasAttributedTo", "agent:1"),
        (acme_claim_id, "rdf:subject", "alice"),
        (acme_claim_id, "rdf:object", "acme"),
        (beta_claim_id, "rdf:subject", "alice"),
        (beta_claim_id, "rdf:object", "beta"),
    } == edge_triples
    assert len(kept_edges) == len(edge_triples)
    for edge in kept_edges:
        assert edge.id == sha256_hex("edge", edge.src, edge.type, edge.dst)

    assert stats["claims_in"] == 3
    assert stats["claims_kept"] == 2
    assert stats["claims_dropped"] == 1
    assert stats["nodes_dropped"] == 1
    assert stats["dedup_collisions"] == 3
    assert stats["edges_kept"] == 9


def test_predicate_aliases_fold_claims_before_semantic_collapse():
    nodes = [
        Node(id="alice", type="Agent", title="Alice"),
        Node(id="acme", type="Agent", title="Acme"),
        Node(
            id="claim:first",
            type="Claim",
            title="Alice founded Acme",
            facets={
                "id": "claim:first",
                "S_id": "alice",
                "P": "founded",
                "O_id": "acme",
                "corroborations": 2,
            },
        ),
        Node(
            id="claim:second",
            type="Claim",
            title="Alice created Acme",
            facets={
                "id": "claim:second",
                "S_id": "alice",
                "P": "created",
                "O_id": "acme",
                "corroborations": 3,
            },
        ),
    ]
    edges = [
        _edge("rdf:subject", "claim:first", "alice"),
        _edge("rdf:object", "claim:first", "acme"),
        _edge("rdf:subject", "claim:second", "alice"),
        _edge("rdf:object", "claim:second", "acme"),
    ]

    dst = InMemoryStore()
    stats = copy_graph_canonicalizing(
        nodes,
        edges,
        None,
        dst,
        predicate_aliases={"created": "founded"},
    )

    claim_id = semantic_claim_id("alice", "founded", object_id="acme")
    claims = list(dst.list_nodes(type="Claim"))
    assert [claim.id for claim in claims] == [claim_id]
    assert claims[0].facets["P"] == "founded"
    assert claims[0].facets["corroborations"] == 5
    assert stats["claims_dropped"] == 1
    assert stats["predicates_folded"] == 1
    assert stats["claims_inverse_rewritten"] == 0


def test_inverse_predicate_rewrite_swaps_claim_facets_and_folds():
    nodes = [
        Node(id="alice", type="Agent", title="Alice"),
        Node(id="bob", type="Agent", title="Bob"),
        Node(
            id="claim:canonical",
            type="Claim",
            title="Alice employs Bob",
            facets={
                "id": "claim:canonical",
                "S_id": "alice",
                "P": "employs",
                "O_id": "bob",
                "corroborations": 2,
            },
        ),
        Node(
            id="claim:inverse",
            type="Claim",
            title="Bob employed by Alice",
            facets={
                "id": "claim:inverse",
                "S_id": "bob",
                "P": "employed_by",
                "O_id": "alice",
                "corroborations": 3,
            },
        ),
    ]
    edges = [
        _edge("employs", "alice", "bob"),
        _edge("employed_by", "bob", "alice"),
        _edge("rdf:subject", "claim:canonical", "alice"),
        _edge("rdf:object", "claim:canonical", "bob"),
        _edge("rdf:subject", "claim:inverse", "bob"),
        _edge("rdf:object", "claim:inverse", "alice"),
    ]

    dst = InMemoryStore()
    stats = copy_graph_canonicalizing(
        nodes,
        edges,
        None,
        dst,
        inverse_aliases={"employed_by": "employs"},
    )

    claim_id = semantic_claim_id("alice", "employs", object_id="bob")
    claims = list(dst.list_nodes(type="Claim"))
    assert [claim.id for claim in claims] == [claim_id]
    assert claims[0].facets["S_id"] == "alice"
    assert claims[0].facets["P"] == "employs"
    assert claims[0].facets["O_id"] == "bob"
    assert claims[0].facets["corroborations"] == 5

    edge_triples = {(edge.src, edge.type, edge.dst) for edge in dst.list_edges()}
    assert edge_triples == {
        ("alice", "employs", "bob"),
        (claim_id, "rdf:subject", "alice"),
        (claim_id, "rdf:object", "bob"),
    }
    assert stats["claims_dropped"] == 1
    assert stats["predicates_folded"] == 2
    assert stats["claims_inverse_rewritten"] == 1


def test_legacy_and_semantic_claim_ids_heal_to_single_semantic_claim():
    semantic_id = semantic_claim_id("alice", "founded", object_id="acme")
    legacy_id = "legacy-claim"
    nodes = [
        Node(id="alice", type="Agent", title="Alice"),
        Node(id="acme", type="Agent", title="Acme"),
        Node(id="block:1", type="Block", title="Block 1"),
        Node(id="block:2", type="Block", title="Block 2"),
        Node(id="finding:1", type="Finding", title="Finding 1"),
        Node(
            id=legacy_id,
            type="Claim",
            title="Alice founded Acme",
            facets={
                "id": legacy_id,
                "S_id": "alice",
                "P": "founded",
                "O_id": "acme",
                "corroborations": 24,
            },
        ),
        Node(
            id=semantic_id,
            type="Claim",
            title="Alice founded Acme",
            facets={
                "id": semantic_id,
                "S_id": "alice",
                "P": "founded",
                "O_id": "acme",
                "corroborations": 1,
            },
        ),
    ]
    edges = [
        _edge("prov:wasDerivedFrom", legacy_id, "block:1"),
        _edge("prov:wasDerivedFrom", semantic_id, "block:2"),
        _edge("rdf:subject", legacy_id, "alice"),
        _edge("rdf:subject", semantic_id, "alice"),
        _edge("rdf:object", legacy_id, "acme"),
        _edge("rdf:object", semantic_id, "acme"),
        _edge("supports", "finding:1", legacy_id),
    ]

    dst = InMemoryStore()
    stats = copy_graph_canonicalizing(nodes, edges, None, dst)

    claims = list(dst.list_nodes(type="Claim"))
    assert [claim.id for claim in claims] == [semantic_id]
    assert claims[0].facets["id"] == semantic_id
    assert claims[0].facets["corroborations"] == 25
    assert dst.get_node(legacy_id) is None

    kept_edges = list(dst.list_edges())
    for edge in kept_edges:
        assert legacy_id not in (edge.src, edge.dst)
        assert edge.id == sha256_hex("edge", edge.src, edge.type, edge.dst)
    assert (semantic_id, "prov:wasDerivedFrom", "block:1") in {
        (edge.src, edge.type, edge.dst) for edge in kept_edges
    }
    assert ("finding:1", "supports", semantic_id) in {
        (edge.src, edge.type, edge.dst) for edge in kept_edges
    }
    assert stats["claims_in"] == 2
    assert stats["claims_kept"] == 1
    assert stats["claims_dropped"] == 1


def test_stale_canonical_keeps_variant():
    # The variant's canonical is NOT present in the input set → the variant must keep
    # itself (else an edge to it would dangle), mirroring fold_graph.repr_in_view.
    nodes = [
        Node(id="var", type="Place", title="USA"),
        Node(id="city", type="Place", title="Austin"),
    ]
    equivalence = {"var": "canon-absent", "canon-absent": "canon-absent"}
    edges = [_edge("located_in", "city", "var")]
    dst = InMemoryStore()
    stats = copy_graph_canonicalizing(nodes, edges, equivalence, dst)

    ids = {n.id for n in dst.list_nodes()}
    assert ids == {"var", "city"}  # variant survives as itself
    assert stats["nodes_dropped"] == 0
    kept = list(dst.list_edges())
    assert len(kept) == 1
    assert (kept[0].src, kept[0].dst) == ("city", "var")  # no remap, no dangle


def test_empty_equivalence_is_verbatim_copy():
    nodes = [
        Node(id="a", type="Place", title="A"),
        Node(id="b", type="Place", title="B"),
    ]
    edges = [_edge("near", "a", "b")]
    dst = InMemoryStore()
    stats = copy_graph_canonicalizing(nodes, edges, None, dst)
    assert {n.id for n in dst.list_nodes()} == {"a", "b"}
    assert [(e.src, e.dst) for e in dst.list_edges()] == [("a", "b")]
    assert stats["nodes_dropped"] == 0 and stats["edges_dropped"] == 0


def test_idempotent_reheal_on_collapsed_graph():
    # First heal collapses var->canon; a second heal on the OUTPUT (which no longer
    # contains var) is a verbatim copy — idempotent.
    nodes = [
        Node(id="canon", type="Place", title="United States"),
        Node(id="var", type="Place", title="USA"),
        Node(id="city", type="Place", title="Austin"),
    ]
    equivalence = {"var": "canon", "canon": "canon"}
    edges = [_edge("located_in", "city", "var")]

    first = InMemoryStore()
    copy_graph_canonicalizing(nodes, edges, equivalence, first)
    first_nodes = list(first.list_nodes())
    first_edges = list(first.list_edges())

    # Re-heal the OUTPUT with the same equivalence. ``var`` is gone, so repr_for(var)
    # would be a no-op anyway; nothing changes.
    second = InMemoryStore()
    stats = copy_graph_canonicalizing(first_nodes, first_edges, equivalence, second)
    assert {n.id for n in second.list_nodes()} == {n.id for n in first_nodes}
    assert [(e.src, e.dst, e.id) for e in second.list_edges()] == [
        (e.src, e.dst, e.id) for e in first_edges
    ]
    assert stats["nodes_dropped"] == 0


def test_authority_equivalence_map_drives_the_fold(tmp_path):
    # End-to-end at the map level: the AuthorityIndex.equivalence_map() (member->
    # canonical) is exactly what the heal copy consumes.
    auth = AuthorityIndex(tmp_path / "authority")
    auth.upsert(
        AuthorityRecord(
            cluster_id="c1",
            canonical_id="canon",
            canonical_name="United States",
            member_ids=("canon", "var"),
            variants=("USA",),
            exact_match_pairs=(("canon", "var"),),
        )
    )
    equivalence = auth.equivalence_map()
    assert equivalence == {"canon": "canon", "var": "canon"}

    nodes = [
        Node(id="canon", type="Place", title="United States"),
        Node(id="var", type="Place", title="USA"),
    ]
    dst = InMemoryStore()
    copy_graph_canonicalizing(nodes, [], equivalence, dst)
    assert {n.id for n in dst.list_nodes()} == {"canon"}


def test_heal_via_copy_cli_path_collapses_variant(tmp_path):
    """CLI heal path end-to-end against a REAL Ladybug vault (model-free): seed a
    variant + canonical + an edge, confirm an authority record, then call
    ``heal_via_copy`` and assert the swapped graph dropped the variant and remapped
    the edge. Proves the CLI orchestration (read live → copy-fold → atomic swap)
    runs — its function-local ``from okto_neuron.cli.kg import (...)`` block and the
    swap sequencing only execute on a real call."""
    import okto_neuron.store._bootstrap as bootstrap_module
    from okto_neuron.cli.kg import kg_init
    from okto_neuron.reconcile.authority import AUTHORITY_DIRNAME
    from okto_neuron.reconcile.heal import heal_via_copy
    from okto_neuron.store import vault as vault_module
    from okto_neuron.store.ladybug import VaultConnection
    from okto_neuron.vault import Vault

    def _drop_cached_handles() -> None:
        for store in list(vault_module._STORE_CACHE.values()):
            try:
                store.close()
            except Exception:
                pass
        vault_module._STORE_CACHE.clear()
        VaultConnection.close_all()
        for handle in list(bootstrap_module._bootstrap_cache.values()):
            try:
                handle.close()
            except Exception:
                pass
        bootstrap_module._bootstrap_cache.clear()

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _drop_cached_handles()

    # Seed the live graph through a normal Vault handle.
    vault = Vault.open(vault_path)
    try:
        vault.store.add_node(Node(id="canon", type="Place", title="United States"))
        vault.store.add_node(Node(id="var", type="Place", title="USA"))
        vault.store.add_node(Node(id="city", type="Place", title="Austin"))
        vault.store.add_edge(_edge("located_in", "city", "var"))
    finally:
        vault.close()
    _drop_cached_handles()

    authority = AuthorityIndex(vault_path / ".marginalia" / AUTHORITY_DIRNAME)
    authority.upsert(
        AuthorityRecord(
            cluster_id="c1",
            canonical_id="canon",
            canonical_name="United States",
            member_ids=("canon", "var"),
            variants=("USA",),
            exact_match_pairs=(("canon", "var"),),
        )
    )

    code, stats = heal_via_copy(vault_path, authority=authority, return_stats=True)
    assert code == 0
    assert stats["nodes_dropped"] == 1
    assert stats["claims_in"] == 0
    _drop_cached_handles()

    vault = Vault.open(vault_path)
    try:
        ids = {n.id for n in vault.store.list_nodes()}
        edges = list(vault.store.list_edges())
    finally:
        vault.close()
    _drop_cached_handles()

    assert "var" not in ids  # variant merged away
    assert {"canon", "city"} <= ids
    assert any(e.src == "city" and e.dst == "canon" for e in edges)
    assert not any(e.dst == "var" for e in edges)


def test_heal_via_copy_refuses_real_cross_process_pool_handle(tmp_path):
    import okto_neuron.store._bootstrap as bootstrap_module
    from okto_neuron.cli.kg import kg_init
    from okto_neuron.errors import VaultLockHeld
    from okto_neuron.reconcile.heal import heal_via_copy
    from okto_neuron.store import vault as vault_module
    from okto_neuron.store.ladybug import VaultConnection

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()
    for handle in list(bootstrap_module._bootstrap_cache.values()):
        handle.close()
    bootstrap_module._bootstrap_cache.clear()

    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys; from pathlib import Path; "
                "from okto_neuron.server._vault_pool import VaultPool; "
                "pool=VaultPool(); lease=pool.lease(Path(sys.argv[1])); "
                "print('READY', flush=True); sys.stdin.readline(); "
                "lease.release(); pool.close_all()"
            ),
            str(vault_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        with pytest.raises(VaultLockHeld, match="owns a live graph handle") as exc_info:
            heal_via_copy(vault_path)
        assert exc_info.value.holding_pid == child.pid
        assert not (vault_path / "graph.heal.lbug").exists()
    finally:
        if child.stdin is not None:
            child.stdin.write("stop\n")
            child.stdin.flush()
        child.communicate(timeout=10)


def _drop_all_cached_handles() -> None:
    """Close every cached vault/bootstrap handle so a swap path sees no live handle."""
    import okto_neuron.store._bootstrap as bootstrap_module
    from okto_neuron.store import vault as vault_module
    from okto_neuron.store.ladybug import VaultConnection

    for store in list(vault_module._STORE_CACHE.values()):
        try:
            store.close()
        except Exception:
            pass
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()
    for handle in list(bootstrap_module._bootstrap_cache.values()):
        try:
            handle.close()
        except Exception:
            pass
    bootstrap_module._bootstrap_cache.clear()


def _seeded_heal_vault(tmp_path):
    """A real Ladybug vault holding canon/var/city + one edge, plus its authority."""
    from okto_neuron.cli.kg import kg_init
    from okto_neuron.reconcile.authority import AUTHORITY_DIRNAME
    from okto_neuron.vault import Vault

    vault_path = tmp_path / "vault"
    assert kg_init(vault_path) == 0
    _drop_all_cached_handles()

    vault = Vault.open(vault_path)
    try:
        vault.store.add_node(Node(id="canon", type="Place", title="United States"))
        vault.store.add_node(Node(id="var", type="Place", title="USA"))
        vault.store.add_node(Node(id="city", type="Place", title="Austin"))
        vault.store.add_edge(_edge("located_in", "city", "var"))
    finally:
        vault.close()
    _drop_all_cached_handles()

    authority = AuthorityIndex(vault_path / ".marginalia" / AUTHORITY_DIRNAME)
    authority.upsert(
        AuthorityRecord(
            cluster_id="c1",
            canonical_id="canon",
            canonical_name="United States",
            member_ids=("canon", "var"),
            variants=("USA",),
            exact_match_pairs=(("canon", "var"),),
        )
    )
    return vault_path, authority


def _live_node_ids(vault_path):
    from okto_neuron.vault import Vault

    vault = Vault.open(vault_path)
    try:
        return {n.id for n in vault.store.list_nodes()}
    finally:
        vault.close()
        _drop_all_cached_handles()


def test_heal_publishes_verified_state_for_the_generation_it_minted(tmp_path):
    """ADR 0039: a SUCCESSFUL heal must leave the sidecar naming the generation it
    installed, verified and unfenced — otherwise every later writer reads the state
    as belonging to a different generation and stays fenced."""
    from okto_neuron.reconcile.heal import heal_via_copy
    from okto_neuron.store.integrity import AuditStatus
    from okto_neuron.store.integrity_state import load_integrity_state

    vault_path, authority = _seeded_heal_vault(tmp_path)
    before = load_integrity_state(vault_path)

    assert heal_via_copy(vault_path, authority=authority) == 0
    _drop_all_cached_handles()

    after = load_integrity_state(vault_path)
    assert after.status is AuditStatus.VERIFIED
    assert after.writer_fenced is False
    assert after.graph_generation not in (None, before.graph_generation)


def test_heal_refuses_to_run_on_a_fenced_generation(tmp_path):
    """ADR 0039: heal cannot bypass a failed-generation fence — copying a corrupt
    graph would normalize bad stored endpoints and launder the incident."""
    from okto_neuron.reconcile.heal import heal_via_copy
    from okto_neuron.store.integrity import AuditStatus
    from okto_neuron.store.integrity_state import (
        GraphIntegrityState,
        IntegrityFenceError,
        load_integrity_state,
        write_integrity_state,
    )

    vault_path, authority = _seeded_heal_vault(tmp_path)
    fenced = GraphIntegrityState(
        status=AuditStatus.FAILED,
        graph_generation=load_integrity_state(vault_path).graph_generation,
        writer_fenced=True,
        reason="adjacency disagrees with stored edge endpoints",
    )
    write_integrity_state(vault_path, fenced)

    with pytest.raises(IntegrityFenceError, match="integrity_fenced") as exc_info:
        heal_via_copy(vault_path, authority=authority)

    assert "rebuild" in str(exc_info.value)
    assert not (vault_path / "graph.heal.lbug").exists()
    # Live graph untouched and the failed verdict preserved as incident evidence.
    assert "var" in _live_node_ids(vault_path)
    assert load_integrity_state(vault_path).status is AuditStatus.FAILED


def test_heal_refuses_swap_when_the_staging_audit_fails(tmp_path):
    """Phase 6 gate: no swap until the FULL integrity audit passes on the healed
    staging graph. A failed staging audit discards staging and leaves live alone."""
    import okto_neuron.cli.kg as kg_cli
    from okto_neuron.errors import RebuildAuditFailed
    from okto_neuron.reconcile.heal import heal_via_copy
    from okto_neuron.store.integrity import AuditStatus, IntegrityAuditResult
    from okto_neuron.store.integrity_state import load_integrity_state

    vault_path, authority = _seeded_heal_vault(tmp_path)
    before = load_integrity_state(vault_path)

    failed = IntegrityAuditResult(
        status=AuditStatus.FAILED,
        nodes_scanned=2,
        edges_scanned=1,
        adjacency_scanned=1,
        expected_artifacts_checked=0,
        issue_count=1,
        issues=(),
        nodes_complete=True,
        edges_complete=True,
        adjacency_complete=True,
        manifest_complete=True,
        duration_ms=0.0,
    )
    real_audit = kg_cli._audit_rebuild_graph_path
    calls: list[str] = []

    def _fake_audit(graph_path, *, dim, expected_identity, stage):
        calls.append(stage)
        if stage == "heal_after_close_reopen":
            return failed, {"stage": stage, "status": failed.status.value}
        return real_audit(graph_path, dim=dim, expected_identity=expected_identity, stage=stage)

    kg_cli._audit_rebuild_graph_path = _fake_audit
    try:
        with pytest.raises(RebuildAuditFailed, match="staging graph failed integrity"):
            heal_via_copy(vault_path, authority=authority)
    finally:
        kg_cli._audit_rebuild_graph_path = real_audit

    assert calls == ["heal_after_close_reopen"]  # never reached the post-swap audit
    assert not (vault_path / "graph.heal.lbug").exists()
    assert "var" in _live_node_ids(vault_path)  # variant survives => no swap happened
    assert load_integrity_state(vault_path).graph_generation == before.graph_generation


def test_heal_post_swap_audit_failure_fences_the_new_generation(tmp_path):
    """A post-swap failure must leave the INSTALLED generation fenced and the
    previous graph retained, so the operator has a rollback target."""
    import okto_neuron.cli.kg as kg_cli
    from okto_neuron.errors import RebuildAuditFailed
    from okto_neuron.reconcile.heal import heal_via_copy
    from okto_neuron.store.integrity import AuditStatus, IntegrityAuditResult
    from okto_neuron.store.integrity_state import load_integrity_state

    vault_path, authority = _seeded_heal_vault(tmp_path)
    before = load_integrity_state(vault_path)

    failed = IntegrityAuditResult(
        status=AuditStatus.FAILED,
        nodes_scanned=2,
        edges_scanned=1,
        adjacency_scanned=1,
        expected_artifacts_checked=0,
        issue_count=1,
        issues=(),
        nodes_complete=True,
        edges_complete=True,
        adjacency_complete=True,
        manifest_complete=True,
        duration_ms=0.0,
    )
    real_audit = kg_cli._audit_rebuild_graph_path

    def _fake_audit(graph_path, *, dim, expected_identity, stage):
        if stage == "heal_after_swap_reopen":
            # Carry the installed identity exactly as a real audit result would, so
            # the published verdict names the generation the heal just swapped in.
            result = replace(
                failed,
                graph_generation=expected_identity.graph_generation,
                identity_contract_version=expected_identity.identity_contract_version,
            )
            return result, {"stage": stage, "status": result.status.value}
        return real_audit(graph_path, dim=dim, expected_identity=expected_identity, stage=stage)

    kg_cli._audit_rebuild_graph_path = _fake_audit
    try:
        with pytest.raises(RebuildAuditFailed, match="post-swap healed graph"):
            heal_via_copy(vault_path, authority=authority)
    finally:
        kg_cli._audit_rebuild_graph_path = real_audit
    _drop_all_cached_handles()

    state = load_integrity_state(vault_path)
    assert state.status is AuditStatus.FAILED
    assert state.writer_fenced is True
    assert state.graph_generation not in (None, before.graph_generation)
    assert (vault_path / "graph.lbug.bak").exists()  # rollback target retained
