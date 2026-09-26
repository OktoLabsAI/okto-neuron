"""Fix B (task-12) — seed-quality diversification for the ask/explore seed cut.

Model-free, CI-safe: InMemoryStore + StubEmbedder only (no LLM, no network).

Covers the per-plan unit matrix (§6.1):
  (a) quota composition on a synthetic fused list (deep-rank entities admitted,
      REL floor rescues a rank-41 relationship Claim, scalar cap at 10),
  (b) fact dedup — same ``(S_id, predicate, object)`` collapses to one slot,
      while the same ``(S_id, object)`` under a DIFFERENT predicate is a
      distinct fact and must not collapse (3.9 regression),
  (c) subject cap 3 with backfill,
  (d) soft floors backfill in fused order, output length == limit,
  (e) determinism,
  (f) ``seed_diversity=False`` → ``search_claims`` byte-identical to the shipped
      behaviour (deterministic-floor regression pin) on a flooded-seed store
      where the answering REL claim sits past rank 40,
plus T2c (``_source_context_for_hits`` anchor-dedup + path round-robin, gated)
and T2b (``Vault.expand_hit_context_spans`` neighbor-span parity).
"""

from __future__ import annotations

from pathlib import Path

from okto_neuron.companion import _diversified_hit_order, _source_context_for_hits
from okto_neuron.core.schema import Node
from okto_neuron.embed import StubEmbedder
from okto_neuron.models import ContextSpan, Node as PublicNode, Provenance, QueryHit
from okto_neuron.query import (
    _SEED_ENTITY_MIN,
    _SEED_REL_MIN,
    _SEED_SCALAR_MAX,
    _SEED_SUBJECT_CAP,
    _diversify_seeds,
    _seed_class,
    expand_block_context,
    search_claims,
)
from okto_neuron.store.memory import InMemoryStore
from okto_neuron.vault import Vault

_HASH = "sha256:" + "a" * 64


# ── synthetic node builders ───────────────────────────────────────────────────
def _scalar(i: int, *, subject: str | None = None, pred: str = "use_case") -> Node:
    return Node(
        id=f"scalar-{i:02d}",
        type="Claim",
        title=f"config {i} scalar",
        content=f"scalar literal claim {i}",
        facets={"S_id": subject or f"subj-{i:02d}", "P": pred, "O_literal": f"value {i}"},
    )


def _rel(i: int, *, subject: str = "cloudbox", obj: str = "postgresql") -> Node:
    return Node(
        id=f"rel-{i:02d}",
        type="Claim",
        title=f"rel {i}",
        content=f"relationship claim {i}",
        facets={"S_id": subject, "P": "uses", "O_id": obj},
    )


def _entity(i: int) -> Node:
    return Node(
        id=f"entity-{i:02d}",
        type="Concept",
        title=f"Entity {i}",
        content=f"entity {i}",
        facets={},
    )


def _fused(*nodes: Node) -> list[tuple[Node, float]]:
    n = len(nodes)
    return [(node, float(n - i)) for i, node in enumerate(nodes)]


# ── (a) quota composition on a synthetic fused list ───────────────────────────
def test_quota_composition_rescues_deep_entities_and_rank41_rel() -> None:
    # ranks 0-29: scalar flood; ranks 30-45: entities; ranks 46-50: REL claims.
    nodes = (
        [_scalar(i) for i in range(30)]
        + [_entity(i) for i in range(16)]
        + [_rel(i, subject=f"rs-{i}", obj=f"ro-{i}") for i in range(5)]
    )
    results = _fused(*nodes)
    picked = _diversify_seeds(results, 20)

    assert len(picked) == 20
    classes = [_seed_class(node) for node, _ in picked]
    assert classes.count("entity") == _SEED_ENTITY_MIN  # deep-rank entities admitted
    assert classes.count("rel") == _SEED_REL_MIN  # REL floor filled from rank 46+
    assert classes.count("scalar") == _SEED_SCALAR_MAX  # flood capped at 10
    # fused ordering preserved among the selected seeds
    ids = [node.id for node, _ in picked]
    assert ids == sorted(ids, key=lambda nid: [n.id for n in nodes].index(nid))
    # scores travel untouched
    by_id = dict((node.id, score) for node, score in results)
    assert all(score == by_id[node.id] for node, score in picked)


def test_rel_floor_admits_rank_41_claim() -> None:
    # 40 scalars, the answering REL claim at rank 41 (index 40), entities after.
    nodes = [_scalar(i) for i in range(40)] + [_rel(0)] + [_entity(i) for i in range(8)]
    picked = _diversify_seeds(_fused(*nodes), 20)
    assert any(node.id == "rel-00" for node, _ in picked)
    # without diversification the plain cut locks it out
    plain = _fused(*nodes)[:20]
    assert all(node.id != "rel-00" for node, _ in plain)


# ── (b) fact dedup ────────────────────────────────────────────────────────────
def test_fact_dedup_collapses_true_duplicates_to_one_slot() -> None:
    """Claims re-minted verbatim — same (S_id, predicate, normalized object) —
    collapse to the best-ranked representative."""
    same_fact = [
        Node(
            id=f"dup-{i}",
            type="Claim",
            title="cloudbox has_config PostgreSQL",
            content="dup",
            facets={"S_id": "cloudbox", "P": "has_config", "O_literal": "PostgreSQL 16!"},
        )
        for i in range(3)
    ]
    fillers = [_scalar(i) for i in range(30)]
    picked = _diversify_seeds(_fused(*same_fact, *fillers), 20)
    dups = [node.id for node, _ in picked if node.id.startswith("dup-")]
    assert dups == ["dup-0"]  # best-ranked representative kept


def test_fact_dedup_keeps_distinct_facts_under_different_predicates() -> None:
    """3.9 regression: facts sharing (S_id, normalized object) under DIFFERENT
    predicates are distinct facts and must both survive — dedup must not
    collapse them just because the literal value matches under a different
    predicate (e.g. status="active" vs priority="Active")."""
    predicate_variants = [
        Node(
            id=f"variant-{pred}",
            type="Claim",
            title=f"cloudbox {pred} PostgreSQL",
            content="variant",
            facets={"S_id": "cloudbox", "P": pred, "O_literal": "PostgreSQL 16!"},
        )
        for pred in ("has_config", "setting_effect", "use_case")
    ]
    fillers = [_scalar(i) for i in range(30)]
    picked = _diversify_seeds(_fused(*predicate_variants, *fillers), 20)
    variants = {node.id for node, _ in picked if node.id.startswith("variant-")}
    assert variants == {
        "variant-has_config",
        "variant-setting_effect",
        "variant-use_case",
    }  # all three distinct-predicate facts survive


def test_fact_dedup_key_includes_predicate() -> None:
    """3.9: minimal repro from the finding — (proj-q3, status, "active") and
    (proj-q3, priority, "Active") are two different facts and must not
    collapse to one seed even though they normalize to the same object."""
    status = Node(
        id="status-claim",
        type="Claim",
        title="proj-q3 status active",
        content="status",
        facets={"S_id": "proj-q3", "P": "status", "O_literal": "active"},
    )
    priority = Node(
        id="priority-claim",
        type="Claim",
        title="proj-q3 priority Active",
        content="priority",
        facets={"S_id": "proj-q3", "P": "priority", "O_literal": "Active"},
    )
    picked = _diversify_seeds(_fused(status, priority), 10)
    ids = {node.id for node, _ in picked}
    assert ids == {"status-claim", "priority-claim"}


def test_fact_dedup_requires_subject_and_normalizable_object() -> None:
    no_sid = [
        Node(
            id=f"nosid-{i}",
            type="Claim",
            title="x",
            content="x",
            facets={"P": "use_case", "O_literal": "same value"},
        )
        for i in range(2)
    ]
    no_obj = [
        Node(
            id=f"noobj-{i}",
            type="Claim",
            title="x",
            content="x",
            facets={"S_id": "s", "P": "use_case"},
        )
        for i in range(2)
    ]
    picked = _diversify_seeds(_fused(*no_sid, *no_obj), 4)
    assert len(picked) == 4  # nothing merged without S_id + object


# ── (c) subject cap with backfill ─────────────────────────────────────────────
def test_subject_cap_three_with_backfill() -> None:
    # 8 distinct facts on ONE subject + enough other material to fill 20.
    one_subject = [_scalar(i, subject="vit") for i in range(8)]
    others = [_scalar(100 + i) for i in range(8)] + [_entity(i) for i in range(12)]
    picked = _diversify_seeds(_fused(*one_subject, *others), 20)
    vit_claims = [node for node, _ in picked if node.facets.get("S_id") == "vit"]
    assert len(vit_claims) == _SEED_SUBJECT_CAP  # capped at 3, no backfill needed
    assert len(picked) == 20

    # When ONLY the capped subject can fill the remaining slots, backfill
    # re-admits the overflow rather than under-returning.
    picked_small = _diversify_seeds(_fused(*one_subject), 5)
    assert len(picked_small) == 5  # 3 under the cap + 2 via backfill


# ── (d) soft floors backfill in fused order, output length == limit ───────────
def test_soft_floors_backfill_in_fused_order() -> None:
    # Only 2 entities and 1 REL exist — floors are underpopulated; scalars
    # exceed the scalar cap only via final backfill; output is exactly limit.
    nodes = [_scalar(i) for i in range(25)] + [_entity(0), _entity(1), _rel(0)]
    picked = _diversify_seeds(_fused(*nodes), 20)
    assert len(picked) == 20
    classes = [_seed_class(node) for node, _ in picked]
    assert classes.count("entity") == 2
    assert classes.count("rel") == 1
    assert classes.count("scalar") == 17  # 10 under the cap + 7 backfilled
    # backfill follows fused order: scalars 00..16 selected, 17+ not
    scalar_ids = [n.id for n, _ in picked if _seed_class(n) == "scalar"]
    assert scalar_ids == [f"scalar-{i:02d}" for i in range(17)]


def test_short_lists_pass_through() -> None:
    nodes = [_scalar(0), _entity(0)]
    assert _diversify_seeds(_fused(*nodes), 20) == _fused(*nodes)
    assert _diversify_seeds([], 20) == []


# ── (e) determinism ───────────────────────────────────────────────────────────
def test_diversify_is_deterministic() -> None:
    nodes = (
        [_scalar(i) for i in range(30)]
        + [_entity(i) for i in range(10)]
        + [_rel(i, subject=f"s{i}", obj=f"o{i}") for i in range(5)]
    )
    a = _diversify_seeds(_fused(*nodes), 20)
    b = _diversify_seeds(_fused(*nodes), 20)
    assert [(n.id, s) for n, s in a] == [(n.id, s) for n, s in b]


# ── (f) flooded-seed store: gate off = shipped behaviour, gate on = rescue ────
def _flooded_store(embedder: StubEmbedder, qvec: list[float]) -> InMemoryStore:
    """45 lexically-echoing scalar claims covering every question term; the
    answering REL claim covers only the common topic term (no stemming: 'uses'
    != 'use') so it sits past rank 40; topic entities rank below all covered
    claims (the measured nc-103 flood signature)."""
    store = InMemoryStore()
    for i in range(45):
        title = f"setting {i:02d} cloudbox use database"
        store.add_node(
            Node(
                id=f"scalar-{i:02d}",
                type="Claim",
                title=title,
                content=f"{title} tuning literal {i}",
                embedding=embedder.embed(title),
                facets={"S_id": f"cfg-{i:02d}", "P": "use_case", "O_literal": f"value {i}"},
            )
        )
    # qvec embeddings guarantee pool membership via the cosine leg (StubEmbedder
    # is hash-seeded, so an arbitrary vector may have non-positive cosine and
    # silently drop a node from the candidate pool). Final ranks are still
    # decided by the answer partition: the REL claim covers one common term, so
    # it sits BELOW all 45 three-term scalars regardless of its vector rank.
    store.add_node(
        Node(
            id="rel-answer",
            type="Claim",
            title="CloudBox uses PostgreSQL",
            content="CloudBox uses PostgreSQL",
            embedding=qvec,
            facets={"S_id": "cloudbox", "P": "uses", "O_id": "postgresql"},
        )
    )
    store.add_node(
        Node(
            id="cloudbox",
            type="Concept",
            title="CloudBox",
            content="CloudBox file platform",
            embedding=qvec,
            facets={},
        )
    )
    store.add_node(
        Node(
            id="postgresql",
            type="Concept",
            title="PostgreSQL",
            content="PostgreSQL database engine",
            embedding=qvec,
            facets={},
        )
    )
    store.add_node(
        Node(
            id="doc-infra",
            type="InformationObject",
            title="Infra notes",
            content="infrastructure notes",
            embedding=qvec,
            facets={},
        )
    )
    for name in ("mariadb", "redis", "php", "docker", "nginx"):
        store.add_node(
            Node(
                id=name,
                type="Concept",
                title=name.capitalize(),
                content=f"{name} component",
                embedding=qvec,
                facets={},
            )
        )
    return store


def test_flooded_store_old_gate_reproduces_shipped_ordering() -> None:
    embedder = StubEmbedder()
    query = "what database does cloudbox use?"
    store = _flooded_store(embedder, embedder.embed(query))

    default_hits = search_claims(query, k=5, store=store, embedder=embedder)
    gated_off = search_claims(query, k=5, store=store, embedder=embedder, seed_diversity=False)
    # regression pin: the gate default and explicit False are byte-identical
    assert [(n.id, s) for n, s in default_hits] == [(n.id, s) for n, s in gated_off]
    # and reproduce the flood: 20/20 Claim seeds, answering REL claim locked out
    assert len(default_hits) == 20
    assert all(node.type == "Claim" for node, _ in default_hits)
    assert all(node.id != "rel-answer" for node, _ in default_hits)


def test_flooded_store_diversified_seeds_rescue_rel_claim_and_entities() -> None:
    embedder = StubEmbedder()
    query = "what database does cloudbox use?"
    store = _flooded_store(embedder, embedder.embed(query))

    hits = search_claims(query, k=5, store=store, embedder=embedder, seed_diversity=True)
    ids = [node.id for node, _ in hits]
    assert len(hits) == 20
    assert "rel-answer" in ids  # the rank-41-class answering REL claim enters
    assert "cloudbox" in ids and "postgresql" in ids  # topic entities seed the ego-graph
    entities = [n for n, _ in hits if n.type != "Claim"]
    scalars = [n for n, _ in hits if n.type == "Claim" and not n.facets.get("O_id")]
    assert len(entities) >= 6  # entity floor met (8 entities exist)
    # the flood can no longer take every slot: scalar_max holds while any other
    # admissible material exists (soft backfill refills only past exhaustion —
    # 9 non-scalar nodes exist, so scalars get at most 20 - 9 = 11 slots).
    assert len(scalars) <= _SEED_SCALAR_MAX + 1
    # determinism end-to-end
    again = search_claims(query, k=5, store=store, embedder=embedder, seed_diversity=True)
    assert [(n.id, s) for n, s in hits] == [(n.id, s) for n, s in again]


def test_quota_overrides_thread_through_search_claims() -> None:
    embedder = StubEmbedder()
    query = "what database does cloudbox use?"
    store = _flooded_store(embedder, embedder.embed(query))
    hits = search_claims(
        query,
        k=5,
        store=store,
        embedder=embedder,
        seed_diversity=True,
        seed_scalar_max=3,
        seed_entity_min=2,
        seed_rel_min=1,
    )
    scalars = [n for n, _ in hits if n.type == "Claim" and not n.facets.get("O_id")]
    entities = [n for n, _ in hits if n.type != "Claim"]
    # The tightened scalar cap (3) holds until every other admissible node is
    # in: 8 entities + 1 REL exist, so ALL of them are selected before backfill
    # refills the remaining 11 slots with scalars — proof the override reached
    # _diversify_seeds (default cap 10 would have admitted 10 scalars pre-fill).
    assert len(hits) == 20
    assert any(n.id == "rel-answer" for n, _ in hits)
    assert len(entities) == 8
    assert len(scalars) == 20 - 8 - 1


# ── T2c: _source_context_for_hits anchor-dedup + path round-robin (gated) ─────
def _hit(node_id: str, path: str, start: int, end: int, score: float = 0.5) -> QueryHit:
    return QueryHit(
        node=PublicNode(id=node_id, type="Claim", name=node_id),
        score=score,
        provenance=Provenance(
            path=path,
            byte_start=start,
            byte_end=end,
            content_hash=_HASH,
            extraction_activity_id="act",
            agent_id="agent",
            document_id="doc",
            block_id=f"blk-{node_id}",
        ),
    )


def test_source_context_diversify_false_is_byte_identical(tmp_path: Path) -> None:
    doc = tmp_path / "a.md"
    doc.write_bytes(b"aaaaabbbbbccccc")
    hits = [
        _hit("h1", str(doc), 0, 5),
        _hit("h2", str(doc), 0, 5),  # duplicate anchor
        _hit("h3", str(doc), 5, 10),
    ]
    legacy = _source_context_for_hits(hits)
    explicit = _source_context_for_hits(hits, diversify=False)
    assert legacy == explicit
    assert legacy.count("aaaaa") == 2  # duplicate anchor kept when gated off
    deduped = _source_context_for_hits(hits, diversify=True)
    assert deduped.count("aaaaa") == 1  # and collapsed when gated on


def test_source_context_dedup_and_round_robin(tmp_path: Path) -> None:
    doc_a = tmp_path / "a.md"
    doc_b = tmp_path / "b.md"
    doc_a.write_bytes(b"AAAA0" * 20)
    doc_b.write_bytes(b"BBBB1" * 20)
    hits = [
        _hit("a1", str(doc_a), 0, 5),
        _hit("a1dup", str(doc_a), 0, 5),  # identical anchor → dropped
        _hit("a2", str(doc_a), 5, 10),
        _hit("a3", str(doc_a), 10, 15),
        _hit("b1", str(doc_b), 0, 5),
        _hit("b2", str(doc_b), 5, 10),
    ]
    ordered = _diversified_hit_order(hits)
    assert [h.node.id for h in ordered] == ["a1", "b1", "a2", "b2", "a3"]

    context = _source_context_for_hits(hits, diversify=True)
    # Each block now renders as an EXCERPT marker line followed by its text.
    blocks = [b for b in context.split("- [EXCERPT") if b.strip()]
    assert len(blocks) == 5  # dup collapsed
    # round-robin: sources interleave instead of doc_a exhausting the budget
    assert blocks[0].splitlines()[1].startswith("AAAA0")
    assert blocks[1].splitlines()[1].startswith("BBBB1")


def test_round_robin_protects_budget_across_sources(tmp_path: Path) -> None:
    doc_a = tmp_path / "a.md"
    doc_b = tmp_path / "b.md"
    doc_a.write_bytes(b"A" * 4000)
    doc_b.write_bytes(b"B" * 4000)
    hits = [
        _hit("a1", str(doc_a), 0, 1000),
        _hit("a2", str(doc_a), 1000, 2000),
        _hit("a3", str(doc_a), 2000, 3000),
        _hit("b1", str(doc_b), 0, 1000),
    ]
    context = _source_context_for_hits(hits, max_token_budget=600, diversify=True)
    assert "B" in context  # second source reached within budget
    flooded = _source_context_for_hits(hits, max_token_budget=600, diversify=False)
    assert "B" not in flooded  # legacy order: doc_a consumes the whole budget


# ── T2b: Vault.expand_hit_context_spans neighbor-span parity ──────────────────
def _block(idx: int, path: str) -> Node:
    start = idx * 100
    return Node(
        id=f"blk-{idx}",
        type="Block",
        title=f"block {idx}",
        content=f"block {idx}",
        facets={
            "source_path": path,
            "block_index": idx,
            "byte_start": start,
            "byte_end": start + 50,
            "content_hash": _HASH,
        },
    )


def _block_hit(block_id: str, path: str) -> QueryHit:
    return QueryHit(
        node=PublicNode(id=f"claim-of-{block_id}", type="Claim", name="c"),
        score=0.5,
        provenance=Provenance(
            path=path,
            byte_start=0,
            byte_end=10,
            content_hash=_HASH,
            extraction_activity_id="act",
            agent_id="agent",
            document_id="doc",
            block_id=block_id,
        ),
    )


def test_expand_hit_context_spans_parity(tmp_path: Path, monkeypatch) -> None:
    path = str(tmp_path / "doc.md")
    store = InMemoryStore()
    for i in range(4):
        store.add_node(_block(i, path))
    vault = Vault(tmp_path, store, embedder=StubEmbedder())

    monkeypatch.setenv("OKTO_NEURON_QUERY_NEIGHBORS", "1")
    hit = _block_hit("blk-1", path)
    enriched = vault.expand_hit_context_spans([hit])
    expected = tuple(expand_block_context(store, "blk-1", k_neighbors=1))
    assert enriched[0].context_spans == expected  # exact block-arm parity
    assert expected  # sanity: neighbors actually exist

    # hits that already carry spans pass through untouched
    pre = hit.model_copy(
        update={
            "context_spans": (
                ContextSpan(
                    path=path,
                    byte_start=0,
                    byte_end=5,
                    content_hash=_HASH,
                    block_id="blk-0",
                    block_index=0,
                ),
            )
        }
    )
    assert vault.expand_hit_context_spans([pre])[0] is pre

    # width 0 (the default-config block arm gets no spans either) → unchanged
    monkeypatch.setenv("OKTO_NEURON_QUERY_NEIGHBORS", "0")
    assert vault.expand_hit_context_spans([hit])[0].context_spans == ()
