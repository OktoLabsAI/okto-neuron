"""Batched store reads at the former N+1 sites (#11).

Behaviour of each site is covered by its own suite (tests/companion,
tests/migrate, tests/reconcile, tests/resolve, tests/test_subgraph.py); these
tests pin the read SHAPE: no per-node ``get_node`` in the converted loops, and
the same result as the unbatched code path.
"""

from __future__ import annotations

from collections import Counter

from okto_neuron.companion import _incremental
from okto_neuron.consolidate import NodeCandidate
from okto_neuron.core.schema import Edge, Node
from okto_neuron.ingest.markdown import sha256_hex
from okto_neuron.migrate.bridge_edges import ensure_source_mentions
from okto_neuron.resolve import find_contradictions
from okto_neuron.store.memory import InMemoryStore
from okto_neuron.subgraph import _ranked_bridge_claims


class _CountingStore:
    """Forwards to a real store and counts every method call by name."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: Counter[str] = Counter()
        self.get_nodes_sizes: list[int] = []

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def wrapped(*args, **kwargs):
            self.calls[name] += 1
            if name == "get_nodes":
                args = (list(args[0]), *args[1:])
                self.get_nodes_sizes.append(len(args[0]))
            return attr(*args, **kwargs)

        return wrapped


def _derived(store: InMemoryStore, claim_id: str, block_id: str) -> None:
    store.add_edge(
        Edge(
            id=f"d-{claim_id}-{block_id}",
            type="prov:wasDerivedFrom",
            src=claim_id,
            dst=block_id,
        )
    )


def _llm_claim(claim_id: str, obj: str, **extra) -> Node:
    return Node(
        id=claim_id,
        type="Claim",
        title=claim_id,
        facets={"model_id": "fake-model", "P": "has", "O_literal": obj, **extra},
    )


# ── companion/_incremental ──────────────────────────────────────────────────────


def test_llm_claims_for_block_reads_claims_in_one_batch() -> None:
    store = InMemoryStore()
    store.add_node(Node(id="block-a", type="Block", content="x"))
    store.add_node(Node(id="det", type="Claim", title="det", facets={"P": "has_tag"}))
    store.add_node(Node(id="not-claim", type="Concept", title="c"))
    for i in range(5):
        store.add_node(_llm_claim(f"c{i}", "x"))
        _derived(store, f"c{i}", "block-a")
    _derived(store, "det", "block-a")
    _derived(store, "not-claim", "block-a")

    counting = _CountingStore(store)
    got = _incremental._llm_claims_for_block(counting, "block-a")

    assert sorted(got) == [f"c{i}" for i in range(5)]
    assert counting.calls["get_node"] == 0
    assert counting.calls["get_nodes"] == 1


def _removal_fixture() -> InMemoryStore:
    store = InMemoryStore()
    store.add_node(
        Node(id="old-block", type="Block", content="alpha fact.\n\nbeta fact.\n\ngamma fact.")
    )
    store.add_node(Node(id="new-block", type="Block", content="alpha fact."))
    store.add_node(Node(id="obj", type="Concept", title="beta fact."))
    store.add_node(_llm_claim("keep", "alpha fact."))
    store.add_node(_llm_claim("drop-literal", "beta fact."))
    store.add_node(
        Node(
            id="drop-object",
            type="Claim",
            title="drop-object",
            facets={"model_id": "fake-model", "P": "has", "O_id": "obj"},
        )
    )
    for claim_id in ("keep", "drop-literal", "drop-object"):
        _derived(store, claim_id, "old-block")
    return store


def test_detach_and_resurrect_use_batched_reads() -> None:
    store = _removal_fixture()
    counting = _CountingStore(store)

    detached = _incremental.detach_orphan_removals(
        counting,
        frozenset({"old-block"}),
        frozenset({"new-block"}),
        valid_as_of="2026-01-01",
    )

    assert sorted(detached) == ["drop-literal", "drop-object"]
    assert counting.calls["get_node"] == 0

    counting = _CountingStore(store)
    revived = _incremental.resurrect_reverted_claims(
        counting,
        frozenset({"old-block"}),
        asserted_at="",
    )

    assert sorted(r["claim_id"] for r in revived) == ["drop-literal", "drop-object"]
    # Only the (untouched) superseder check may still read one node at a time,
    # and none of these claims is superseded.
    assert counting.calls["get_node"] == 0


# ── migrate/bridge_edges ────────────────────────────────────────────────────────


def test_source_mentions_batches_node_and_document_reads() -> None:
    store = InMemoryStore()
    doc_path = "/tmp/example/doc.md"
    doc_id = sha256_hex("document", doc_path)
    store.add_node(Node(id=doc_id, type="Document", title="doc"))
    ids = []
    for i in range(20):
        node_id = f"agent-{i}"
        path = doc_path if i % 2 == 0 else "/tmp/example/no-such-doc.md"
        store.add_node(Node(id=node_id, type="Agent", title=node_id, facets={"source_path": path}))
        ids.append(node_id)
    ids += ["missing-id", "agent-0"]  # a missing id and a duplicate, as before

    counting = _CountingStore(store)
    minted = ensure_source_mentions(counting, ids)

    assert minted == 10
    assert counting.calls["get_node"] == 0
    assert counting.calls["get_nodes"] == 2
    assert {e.dst for e in store.list_edges(src=doc_id, type="schema:mentions")} == {
        f"agent-{i}" for i in range(0, 20, 2)
    }


# ── resolve ─────────────────────────────────────────────────────────────────────


def test_find_contradictions_reuses_a_claim_snapshot() -> None:
    store = InMemoryStore()
    for i in range(4):
        store.add_node(
            Node(
                id=f"claim-{i}",
                type="Claim",
                title=f"claim {i}",
                facets={"S": "subj", "P": "lives_in", "O": f"place {i}"},
            )
        )
    candidate = NodeCandidate(
        type="Claim",
        title="new",
        facets={"S": "subj", "P": "lives_in", "O": "place 0"},
    )
    expected = find_contradictions(candidate, store)
    assert len(expected) == 3

    snapshot = list(store.list_nodes(type="Claim"))
    counting = _CountingStore(store)
    got = find_contradictions(candidate, counting, claims=snapshot)

    assert got == expected
    assert counting.calls["list_nodes"] == 0


# ── subgraph ────────────────────────────────────────────────────────────────────


def test_ranked_bridge_claims_one_batched_read_within_scan_cap() -> None:
    store = InMemoryStore()
    store.add_node(Node(id="hub", type="Agent", title="hub"))
    for i in range(200):
        store.add_node(
            Node(
                id=f"claim-{i:03d}",
                type="Claim",
                title=f"claim {i}",
                facets={"S_id": "hub", "P": "knows", "confidence": 0.5 + (i % 5) / 10},
            )
        )
        store.add_edge(Edge(id=f"s-{i:03d}", type="rdf:subject", src=f"claim-{i:03d}", dst="hub"))

    expected = _ranked_bridge_claims(store, "hub", degree_cap=4)
    counting = _CountingStore(store)
    got = _ranked_bridge_claims(counting, "hub", degree_cap=4)

    assert [(n.id, s) for n, s in got] == [(n.id, s) for n, s in expected]
    assert counting.calls["get_node"] == 0
    assert counting.calls["get_nodes"] == 1
    assert counting.get_nodes_sizes == [64]  # max(4 * 8, 64): the scan cap still bounds reads
