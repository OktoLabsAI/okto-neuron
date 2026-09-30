"""Set-based predicate scan (#11): equivalence with the old per-node scan, and a
bounded store-call count independent of graph size.

``_reference_scan`` below is the pre-#11 ``_scan_store`` kept verbatim as a
test-only oracle (one ``get_node`` per edge endpoint, per Claim argument and per
sample Block). The production scan must produce byte-identical snapshots (same
values AND same dict/Counter insertion order, checked through ``repr``) on every
backend available in the venv.
"""

from __future__ import annotations

import math
import os
from collections import Counter
from itertools import combinations
from pathlib import Path

import pytest

from okto_neuron.core.schema import Edge, Node
from okto_neuron.predicates import (
    collect_predicate_vocabulary,
    generate_predicate_candidates,
    shared_argument_evidence,
)
from okto_neuron.predicates import candidates as candidates_mod
from okto_neuron.predicates.candidates import PredicateSample, _StoreSnapshot
from okto_neuron.store.memory import InMemoryStore

# ── test-only reference: the pre-#11 implementation, unchanged ──────────────────


def _reference_scan(store) -> _StoreSnapshot:
    vocabulary: Counter[str] = Counter()
    argument_pairs: dict[str, Counter[tuple[str, str]]] = {}
    signatures: dict[str, Counter[tuple[str, str]]] = {}
    samples: dict[str, list[PredicateSample]] = {}

    for edge in store.list_edges():
        predicate = str(getattr(edge, "type", "") or "").strip()
        if not predicate:
            continue
        vocabulary[predicate] += 1
        argument_pairs.setdefault(predicate, Counter())[(str(edge.src), str(edge.dst))] += 1
        src_type = _ref_node_type(store.get_node(str(edge.src)))
        dst_type = _ref_node_type(store.get_node(str(edge.dst)))
        signatures.setdefault(predicate, Counter())[(src_type, dst_type)] += 1

    for claim in store.list_nodes(type="Claim"):
        facets = dict(getattr(claim, "facets", {}) or {})
        predicate = str(facets.get("P") or "").strip()
        if not predicate:
            continue
        vocabulary[predicate] += 1
        pair = _ref_claim_argument_pair(facets)
        if pair is not None:
            argument_pairs.setdefault(predicate, Counter())[pair] += 1
            signatures.setdefault(predicate, Counter())[_ref_claim_signature(store, facets)] += 1
        if len(samples.setdefault(predicate, [])) < 3:
            samples[predicate].append(_ref_claim_sample(store, claim, facets))

    return _StoreSnapshot(
        vocabulary=vocabulary,
        argument_pairs=argument_pairs,
        signatures=signatures,
        samples={pred: tuple(items) for pred, items in samples.items()},
    )


def _ref_claim_argument_pair(facets: dict) -> tuple[str, str] | None:
    subject = facets.get("S_id") or facets.get("S") or facets.get("subject")
    obj = facets.get("O_id")
    if obj is None:
        obj = facets.get("O_literal")
    if obj is None:
        obj = facets.get("O") or facets.get("object")
    if subject is None or obj is None:
        return None
    return str(subject), str(obj)


def _ref_claim_signature(store, facets: dict) -> tuple[str, str]:
    subject_id = facets.get("S_id")
    object_id = facets.get("O_id")
    subject_type = _ref_node_type(store.get_node(str(subject_id))) if subject_id else "literal"
    if object_id:
        object_type = _ref_node_type(store.get_node(str(object_id)))
    else:
        object_type = "literal"
    return subject_type, object_type


def _ref_claim_sample(store, claim: Node, facets: dict) -> PredicateSample:
    excerpt = (
        facets.get("source_excerpt") or facets.get("excerpt") or facets.get("source_text") or ""
    )
    if not excerpt and facets.get("block_id"):
        block = store.get_node(str(facets["block_id"]))
        excerpt = getattr(block, "content", "") if block else ""
    return PredicateSample(
        claim_id=str(claim.id),
        title=str(claim.title or ""),
        source_excerpt=str(excerpt or "")[:500],
    )


def _ref_node_type(node) -> str:
    return str(getattr(node, "type", "") or "unknown")


# ── fixture graphs ──────────────────────────────────────────────────────────────


def _populate(store) -> None:
    """Several predicates; Claims with id/literal S-O pairs, dangling S_id/O_id,
    no-pair Claims, >3 Claims per predicate (sample cap), samples with an
    excerpt, with a live Block, with a missing Block, and with neither."""
    for i in range(6):
        store.add_node(Node(id=f"agent-{i}", type="Agent", title=f"Agent {i}"))
    for i in range(3):
        store.add_node(Node(id=f"concept-{i}", type="Concept", title=f"Concept {i}"))
    store.add_node(Node(id="block-1", type="Block", title="b1", content="first block text " * 40))
    store.add_node(Node(id="block-2", type="Block", title="b2", content="second block"))

    def edge(predicate: str, src: str, dst: str) -> None:
        store.add_edge(Edge(id=f"e-{predicate}-{src}-{dst}", type=predicate, src=src, dst=dst))

    edge("knows", "agent-0", "agent-1")
    edge("knows", "agent-1", "agent-0")
    edge("knows", "agent-2", "concept-0")
    edge("acquainted_with", "agent-1", "agent-0")
    edge("acquainted_with", "agent-0", "agent-1")
    edge("about", "block-1", "concept-1")
    edge("about", "block-2", "concept-1")
    edge("mentions", "block-1", "agent-3")

    claims = [
        # predicate, facets extras, title
        ("founded", {"S_id": "agent-0", "O_id": "concept-0", "source_excerpt": "x founded y"}),
        ("founded", {"S_id": "agent-1", "O_id": "concept-1", "block_id": "block-1"}),
        ("founded", {"S_id": "agent-0", "O_literal": "a literal", "block_id": "missing-block"}),
        ("founded", {"S_id": "agent-2", "O_id": "concept-2"}),  # 4th: past sample cap
        ("founded", {"S_id": "ghost-subject", "O_id": "ghost-object"}),
        ("created", {"S_id": "agent-0", "O_id": "concept-0", "excerpt": "made it"}),
        ("created", {"S_id": "concept-0", "O_id": "agent-0", "block_id": "block-2"}),
        ("created", {"S": "a plain subject", "O": "a plain object", "source_text": "t"}),
        ("created", {"subject": "subj", "object": "obj"}),
        ("created", {"S_id": "agent-4"}),  # no object: no pair, still counted + sampled
        ("works_for", {"S_id": "agent-3", "O_id": "agent-4"}),
        ("works_for", {"S_id": "agent-4", "O_id": "agent-3", "block_id": "block-1"}),
        ("employed_by", {"S_id": "agent-3", "O_id": "agent-4"}),
        ("employed_by", {"S_id": "ghost-subject", "O_literal": 42}),
        ("  ", {"S_id": "agent-0", "O_id": "agent-1"}),  # blank predicate: ignored
        ("", {"S_id": "agent-0", "O_id": "agent-1"}),
    ]
    for i, (predicate, extra) in enumerate(claims):
        facets = {"P": predicate, **extra}
        store.add_node(Node(id=f"claim-{i:03d}", type="Claim", title=f"claim {i}", facets=facets))
    store.add_node(Node(id="claim-nop", type="Claim", title="no predicate", facets={}))


@pytest.fixture(params=["memory", "grafx", "ladybug", "neo4j"])
def backend(request, tmp_path: Path):
    if request.param == "memory":
        store = InMemoryStore()
    elif request.param == "grafx":
        pytest.importorskip("okto_grafx")
        from okto_neuron.store.grafx import GrafxStore

        store = GrafxStore(tmp_path / "vault")
    elif request.param == "ladybug":
        pytest.importorskip("ladybug")
        from okto_neuron.store.ladybug import LadybugStore, VaultConnection

        request.addfinalizer(VaultConnection.close_all)
        store = LadybugStore(tmp_path / "vault")
    else:
        # Same gate as tests/store/contract/conftest.py: a real server or skip.
        pytest.importorskip("neo4j")
        uri = os.environ.get("OKTO_NEURON_TEST_NEO4J_URI")
        if not uri:
            pytest.skip("OKTO_NEURON_TEST_NEO4J_URI is not set")

        class _Neo4jTestConfig:
            backend = "neo4j"
            database = "neo4j"
            uri = os.environ["OKTO_NEURON_TEST_NEO4J_URI"]
            credential_env = os.environ.get("OKTO_NEURON_TEST_NEO4J_CREDENTIAL_ENV")
            allow_remote = False

        from okto_neuron.store.neo4j import Neo4jStore

        store = Neo4jStore(tmp_path / "vault", config=_Neo4jTestConfig())
    yield store
    store.close()


class _PredicateEmbedder:
    """Deterministic embeddings that cluster the synonym pairs together."""

    _AXES = {"knows": 0, "acquainted_with": 0, "founded": 1, "created": 1, "works_for": 2}

    def embed(self, text: str) -> list[float]:
        vec = [0.0, 0.0, 0.0, 0.0]
        vec[self._AXES.get(text, 3)] = 1.0
        return vec


# ── (a) equivalence ─────────────────────────────────────────────────────────────


def test_set_based_scan_is_byte_identical_to_reference(backend) -> None:
    _populate(backend)

    expected = _reference_scan(backend)
    actual = candidates_mod._scan_store(backend)

    assert actual == expected
    assert repr(actual) == repr(expected)  # also pins dict / Counter insertion order
    # Sanity: the fixture really exercises every branch.
    assert expected.samples["founded"][1].source_excerpt.startswith("first block text")
    assert expected.samples["founded"][2].source_excerpt == ""  # missing block
    assert len(expected.samples["founded"]) == 3
    assert ("unknown", "unknown") in expected.signatures["founded"]
    assert ("Agent", "literal") in expected.signatures["founded"]


def test_public_api_matches_reference(backend) -> None:
    _populate(backend)
    expected = _reference_scan(backend)

    vocabulary = collect_predicate_vocabulary(backend)
    assert vocabulary == expected.vocabulary
    assert list(vocabulary.items()) == list(expected.vocabulary.items())

    for a, b in combinations(sorted(expected.vocabulary), 2):
        got = shared_argument_evidence(backend, a, b)
        want = candidates_mod._shared_argument_evidence_from_counts(a, b, expected.argument_pairs)
        assert got == want, (a, b)

    candidates = generate_predicate_candidates(
        backend, _PredicateEmbedder(), min_support=1, max_pairs=30
    )
    assert {c.pair for c in candidates} >= {("acquainted_with", "knows"), ("created", "founded")}
    for cand in candidates:
        assert cand.samples_a == expected.samples.get(cand.predicate_a, ())
        assert cand.samples_b == expected.samples.get(cand.predicate_b, ())
        assert cand.signatures_a == candidates_mod._signatures(
            expected.signatures.get(cand.predicate_a)
        )
        assert cand.signatures_b == candidates_mod._signatures(
            expected.signatures.get(cand.predicate_b)
        )


def test_empty_store_matches_reference(backend) -> None:
    assert repr(candidates_mod._scan_store(backend)) == repr(_reference_scan(backend))
    assert collect_predicate_vocabulary(backend) == Counter()


# ── (b) store-call count ────────────────────────────────────────────────────────


class _CountingStore:
    """Forwards to a real store and counts every protocol read."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: Counter[str] = Counter()

    def __getattr__(self, name: str):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def wrapped(*args, **kwargs):
            self.calls[name] += 1
            return attr(*args, **kwargs)

        return wrapped


def _synthetic(size: int) -> InMemoryStore:
    store = InMemoryStore()
    for i in range(size):
        store.add_node(Node(id=f"n{i:05d}", type="Concept", title=f"node {i}"))
    for i in range(size):
        store.add_edge(
            Edge(id=f"e{i:05d}", type=f"rel_{i % 7}", src=f"n{i:05d}", dst=f"n{(i + 1) % size:05d}")
        )
        store.add_node(
            Node(
                id=f"c{i:05d}",
                type="Claim",
                title=f"claim {i}",
                facets={
                    "P": f"pred_{i % 5}",
                    "S_id": f"n{i:05d}",
                    "O_id": f"n{(i * 3) % size:05d}",
                    "block_id": f"n{(i * 7) % size:05d}",
                },
            )
        )
    return store


@pytest.mark.parametrize("size", [40, 2500])
def test_vocabulary_is_constant_store_calls_and_no_get_node(size: int) -> None:
    counting = _CountingStore(_synthetic(size))
    vocabulary = collect_predicate_vocabulary(counting)

    assert sum(vocabulary.values()) == 2 * size
    assert counting.calls["get_node"] == 0
    assert counting.calls["get_nodes"] == 0
    assert sum(counting.calls.values()) <= 3 + math.ceil(size / 1000)
    assert counting.calls == Counter({"list_edges": 1, "list_nodes": 1})


@pytest.mark.parametrize("size", [40, 2500])
def test_shared_evidence_and_full_scan_store_calls_are_bounded(size: int) -> None:
    counting = _CountingStore(_synthetic(size))
    shared_argument_evidence(counting, "pred_0", "pred_1")
    assert counting.calls == Counter({"list_edges": 1, "list_nodes": 1})

    counting = _CountingStore(_synthetic(size))
    candidates_mod._scan_store(counting)
    assert counting.calls["get_node"] == 0
    assert counting.calls == Counter({"list_edges": 1, "list_nodes": 1, "get_nodes": 1})
