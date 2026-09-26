"""Model-free unit tests for ``Companion.explore`` (graph drill-down surface).

No LLM, no embedder, no rebuild: a ``Vault`` wrapping an ``InMemoryStore`` wired
with the same Claim -[rdf:subject/object]-> entity topology the ingest path mints.
The ego-graph internals are covered by ``tests/test_subgraph.py``; this pins the
``explore`` contract (entry modes + serialized shape).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_neuron.companion import Companion, CompanionError
from okto_neuron.core.schema import Edge, Node
from okto_neuron.store.memory import InMemoryStore
from okto_neuron.vault import Vault


def _topology_vault(tmp_path: Path) -> Vault:
    store = InMemoryStore()
    alice = Node(id="a" * 64, type="Agent", title="Alice", content="Alice")
    acme = Node(id="c" * 64, type="Agent", title="Acme", content="Acme")
    claim = Node(
        id="d" * 64,
        type="Claim",
        title="alice founded",
        content="alice founded",
        facets={
            "S_id": alice.id,
            "P": "founded",
            "O_id": acme.id,
            "confidence": 0.9,
            "block_id": "b" * 64,
        },
    )
    for n in (alice, acme, claim):
        store.add_node(n)
    store.add_edge(Edge(id="e1", type="rdf:subject", src=claim.id, dst=alice.id))
    store.add_edge(Edge(id="e2", type="rdf:object", src=claim.id, dst=acme.id))
    return Vault(tmp_path, store)


def test_explore_by_node_id_returns_structured_ego_graph(tmp_path: Path) -> None:
    companion = Companion(_topology_vault(tmp_path))
    alice_id = "a" * 64

    out = companion.explore(node_id=alice_id)

    assert out["seeds"] == [alice_id]
    assert out["hops"] == 1
    assert {"nodes", "relationships", "claims"} <= out.keys()
    node_ids = {n["id"] for n in out["nodes"]}
    # Seed entity is present; its 1-hop topology neighbour (acme) is reached.
    assert alice_id in node_ids
    assert ("c" * 64) in node_ids
    rels = out["relationships"]
    assert any(r["predicate"] == "founded" for r in rels)


def test_explore_requires_topic_or_node_id(tmp_path: Path) -> None:
    companion = Companion(_topology_vault(tmp_path))
    with pytest.raises(CompanionError):
        companion.explore()


# --------------------------------------------------------------------------
# Per-call graph controls: ``relationship_types`` / ``min_claim_confidence`` /
# ``max_degree_per_seed`` used to be config-only, unreachable from a caller.
# --------------------------------------------------------------------------


def _two_predicate_vault(tmp_path: Path) -> Vault:
    """alice -[founded]-> acme (0.9) and alice -[visited]-> beta (0.2)."""
    store = InMemoryStore()
    alice = Node(id="a" * 64, type="Agent", title="Alice", content="Alice")
    acme = Node(id="c" * 64, type="Agent", title="Acme", content="Acme")
    beta = Node(id="b" * 64, type="Agent", title="Beta", content="Beta")
    founded = Node(
        id="d" * 64,
        type="Claim",
        title="alice founded",
        content="alice founded",
        facets={
            "S_id": alice.id,
            "P": "founded",
            "O_id": acme.id,
            "confidence": 0.9,
            "block_id": "1" * 64,
        },
    )
    visited = Node(
        id="e" * 64,
        type="Claim",
        title="alice visited",
        content="alice visited",
        facets={
            "S_id": alice.id,
            "P": "visited",
            "O_id": beta.id,
            "confidence": 0.2,
            "block_id": "2" * 64,
        },
    )
    for n in (alice, acme, beta, founded, visited):
        store.add_node(n)
    for claim, obj in ((founded, acme), (visited, beta)):
        store.add_edge(Edge(id=f"s-{claim.id}", type="rdf:subject", src=claim.id, dst=alice.id))
        store.add_edge(Edge(id=f"o-{claim.id}", type="rdf:object", src=claim.id, dst=obj.id))
    return Vault(tmp_path, store)


def test_explore_relationship_types_filters_edge_types(tmp_path: Path) -> None:
    companion = Companion(_two_predicate_vault(tmp_path))
    alice_id = "a" * 64

    both = companion.explore(node_id=alice_id)
    assert {r["predicate"] for r in both["relationships"]} == {"founded", "visited"}

    filtered = companion.explore(node_id=alice_id, relationship_types=("founded",))
    assert {r["predicate"] for r in filtered["relationships"]} == {"founded"}


def test_explore_min_claim_confidence_filters(tmp_path: Path) -> None:
    companion = Companion(_two_predicate_vault(tmp_path))
    alice_id = "a" * 64

    filtered = companion.explore(node_id=alice_id, min_claim_confidence=0.5)
    assert {r["predicate"] for r in filtered["relationships"]} == {"founded"}


def test_explore_max_degree_per_seed_caps_neighbours(tmp_path: Path) -> None:
    companion = Companion(_two_predicate_vault(tmp_path))
    alice_id = "a" * 64

    capped = companion.explore(node_id=alice_id, max_degree_per_seed=1)
    assert len(capped["relationships"]) == 1


def test_explore_none_overrides_keep_config_behaviour(tmp_path: Path) -> None:
    """Explicit ``None`` must be indistinguishable from not passing the knob."""
    companion = Companion(_two_predicate_vault(tmp_path))
    alice_id = "a" * 64

    baseline = companion.explore(node_id=alice_id)
    explicit_none = companion.explore(
        node_id=alice_id,
        relationship_types=None,
        min_claim_confidence=None,
        max_degree_per_seed=None,
    )
    assert baseline == explicit_none


def test_explore_emits_block_id_on_relationships_and_claims(tmp_path: Path) -> None:
    store = InMemoryStore()
    alice = Node(id="a" * 64, type="Agent", title="Alice", content="Alice")
    acme = Node(id="c" * 64, type="Agent", title="Acme", content="Acme")
    rel_claim = Node(
        id="d" * 64,
        type="Claim",
        title="alice founded",
        content="alice founded",
        facets={
            "S_id": alice.id,
            "P": "founded",
            "O_id": acme.id,
            "confidence": 0.9,
            "block_id": "1" * 64,
        },
    )
    lit_claim = Node(
        id="e" * 64,
        type="Claim",
        title="alice role",
        content="alice role",
        facets={
            "S_id": alice.id,
            "P": "role",
            "O_literal": "founder",
            "confidence": 0.9,
            "block_id": "2" * 64,
        },
    )
    for n in (alice, acme, rel_claim, lit_claim):
        store.add_node(n)
    store.add_edge(Edge(id="s1", type="rdf:subject", src=rel_claim.id, dst=alice.id))
    store.add_edge(Edge(id="o1", type="rdf:object", src=rel_claim.id, dst=acme.id))
    store.add_edge(Edge(id="s2", type="rdf:subject", src=lit_claim.id, dst=alice.id))

    out = Companion(Vault(tmp_path, store)).explore(node_id="a" * 64)

    assert [r["block_id"] for r in out["relationships"]] == ["1" * 64]
    assert [c["block_id"] for c in out["claims"]] == ["2" * 64]


def test_explore_caller_value_beats_vault_config(tmp_path: Path) -> None:
    """Precedence, not just override-vs-code-default: with the vault config
    setting a 0.9 confidence floor, a caller-supplied 0.0 must win (and an
    explicit 0.0 must not be treated as "unset")."""
    (tmp_path / "okto-neuron.yaml").write_text(
        "marginalia_yaml_version: 1\nllm:\n  ask:\n    min_claim_confidence: 0.9\n",
        encoding="utf-8",
    )
    companion = Companion(_two_predicate_vault(tmp_path))
    alice_id = "a" * 64

    # Config alone gates out the 0.2-confidence ``visited`` claim.
    assert {r["predicate"] for r in companion.explore(node_id=alice_id)["relationships"]} == {
        "founded"
    }
    # Caller override wins.
    relaxed = companion.explore(node_id=alice_id, min_claim_confidence=0.0)
    assert {r["predicate"] for r in relaxed["relationships"]} == {"founded", "visited"}
