"""ADR 0010 Tier A — gold-set candidate-recall regression gate (CI-safe, no LLM).

This is the **Tier A** harness from ADR 0010 §Axis 5: a gate on the candidate-
recall layer (``generate_candidate_clusters``) over a gold fixture drawn from the
live 2026-06-05 NX-vault propose run
(``.scratchpad/reconcile-findings/propose-agents-2026-06-05.json``). It pulls in
NO LLM — the judge never runs here; this gate covers recall/precision
*co-consideration*, not adjudication.

Two-store design, because the two cell classes depend on different things and
must not contaminate each other:

* **String-deterministic membership cells** run on a **no-embedding store**, so
  only the string lanes (token-subset, surname, handle, fuzzy lexical) fire and
  the result is independent of embedding cosines. This mirrors
  ``tests/reconcile/test_candidates.py`` and is robust to fastembed cross-platform
  drift. (A single embedded store with short generic content over-clusters
  everything NX/engineer-ish at cosine 0.6–0.8 and pollutes every string cell —
  measured, not assumed; see the P2–P5 note in the P1 handoff.)
* **Embedding-dependent cells** use the **real fastembed embedder** (BAAI/bge-
  small-en-v1.5, the vault default) and are asserted as direct ``_cosine``
  measurements against the live ``RECONCILE_RECALL_FLOOR`` — production fidelity is
  what matters here, so the real model is load-bearing.

Assertion tiers (ADR 0010 §Axis 5, Phased rollout P1):

* **HARD asserts — string-deterministic cells.** GREEN today; pinned as hard
  gates. Includes the NX-Lab *surname collision*: ``NX Lab`` shares surname
  ``lab`` with ``Luke Gray Lab`` and chains to ``Luke Gray`` — so at the CANDIDATE
  layer they co-cluster. That is expected and asserted as a hard gate. Excluding
  NX Lab / Tamola from the Luke Gray merge is owned by the apply-time corroboration
  partition (ADR 0010 Axis 3), NOT by clustering — it belongs to Tier B / P3+,
  not P1, so it is deliberately not asserted here.
* **Tolerance-band / measure-first — embedding-dependent cells.** Avery Q./Avery
  B. and Aler/Mualer cosines are MEASURED on this fixture and asserted below the
  live floor (below floor → the embedding lane does not fire → queued-not-merged
  is reality). Per the ADR, the alias pair is MEASURE-FIRST, not pre-pinned RED.
* **RED-today gaps — ``xfail(strict=True)``.** Cells a future ADR 0010 phase
  closes; ``strict=True`` makes them XPASS-FAIL loudly the instant the sibling
  phase lands, forcing the flip from xfail to a hard gate:
    - ``Nivod``/``Ari Nivod`` co-cluster — blocked by the shared-first-token guard
      in ``token_subset`` today; closed by P2 ``token_subset_unordered``.
    - ``Tovereign`` exclusion from the ``Tovrin`` cluster — welded in today via the
      fuzzy lexical lane (JW=0.922); closed by P2's lexical length-gate.

Precondition: before any embedding-dependent assertion we assert embeddings are
non-null and full-width on the gold nodes — otherwise the cell silently degrades.
"""

from __future__ import annotations

import pytest

from okto_neuron.companion import embedding_text_for
from okto_neuron.core.schema import Node
from okto_neuron.embed import get_provider
from okto_neuron.reconcile.candidates import (
    RECONCILE_RECALL_FLOOR,
    _cosine,
    generate_candidate_clusters,
)
from okto_neuron.store.memory import InMemoryStore

# ── gold fixture (string cells: titles only; embedding lane held off) ────────────
# (node_id, title) covering every gold case from ADR 0010 §Gold-set walkthrough.
_GOLD_TITLES: list[tuple[str, str]] = [
    # MUST-MERGE pairs
    ("nivod", "Nivod"),
    ("ari_nivod", "Ari Nivod"),
    ("tovrin", "Tovrin"),
    ("tovrin_kalia", "Tovrin Kalia"),
    ("avery_q", "Avery Quinn"),
    ("avery_b", "Avery Blake"),
    # MUST-STAY-DISTINCT guards
    ("aler", "Aler Dalvic"),
    ("mualer", "Mualer Disija"),
    ("nx_lab", "NX Lab"),
    ("tamola", "Tamola"),
    ("luke_gray", "Luke Gray"),
    ("luke_gray_lab", "Luke Gray Lab"),
    # Pollution case
    ("tovereign", "Tovereign"),
    # Handle-decomposition cell
    ("nguyen_handle", "taylor.nguyen"),
    ("nguyen_full", "Taylor Nguyen"),
]

# Embedding-dependent cells: only the pairs whose decision rides on the cosine.
# Minimal, role-distinct NX-vault content (two different people who share a first
# name read as distinct under the embedder, as on the live vault).
_GOLD_EMBEDDED: dict[str, tuple[str, str]] = {
    "avery_q": (
        "Avery Quinn",
        "Principal AI/ML engineer building neural inference systems.",
    ),
    "avery_b": (
        "Avery Blake",
        "Regional sales director negotiating customer contracts.",
    ),
    "aler": ("Aler Dalvic", "NX contact."),
    "mualer": ("Mualer Disija", "Delivery engineer."),
}


@pytest.fixture(scope="module")
def string_clusters():
    """Candidate clusters from string lanes ONLY (no embeddings) — deterministic
    and embedding-drift-proof, mirroring ``test_candidates.py``."""
    store = InMemoryStore()
    for node_id, title in _GOLD_TITLES:
        store.add_node(Node(id=node_id, type="Agent", title=title, content=""))
    return generate_candidate_clusters(store, embedder=None)


@pytest.fixture(scope="module")
def embedded_store() -> InMemoryStore:
    """The cosine-cell nodes, embedded with the REAL fastembed model exactly as
    production does (``embedding_text_for`` = title+content)."""
    embedder = get_provider("default")
    store = InMemoryStore()
    for node_id, (title, content) in _GOLD_EMBEDDED.items():
        node = Node(id=node_id, type="Agent", title=title, content=content)
        vector = list(embedder.embed(embedding_text_for(node)))
        store.add_node(node.model_copy(update={"embedding": vector}))
    return store


def _members_for(clusters, node_id: str) -> set[str]:
    for c in clusters:
        if node_id in c.member_ids:
            return set(c.member_ids)
    return {node_id}


def _co_clustered(clusters, a: str, b: str) -> bool:
    return b in _members_for(clusters, a)


# ── precondition: embeddings are real & non-null ────────────────────────────────
def test_gold_embeddings_non_null(embedded_store: InMemoryStore):
    """Guard: every embedding-dependent assertion is meaningless if fastembed
    silently fell back to a stub / empty vector. The real model must be live and
    every cosine-cell node must carry a full-width (384-dim) vector."""
    nodes = list(embedded_store.list_nodes(type="Agent", include_embedding=True))
    assert len(nodes) == len(_GOLD_EMBEDDED)
    for n in nodes:
        assert n.embedding, f"{n.title!r} has no embedding — fastembed not live?"
        assert len(n.embedding) == 384, f"{n.title!r} wrong dim {len(n.embedding)}"


# ── HARD asserts — string-deterministic cells (GREEN today) ─────────────────────
def test_tovrin_subset_co_clusters(string_clusters):
    """``Tovrin`` ⊂ ``Tovrin Kalia`` (shared first token) → subset lane. GREEN."""
    assert _co_clustered(string_clusters, "tovrin", "tovrin_kalia")


def test_aler_mualer_not_co_clustered(string_clusters):
    """``Aler Dalvic`` ≠ ``Mualer Disija``: no string lane fires (``aler`` not a
    token of ``{mualer, disija}``; JW low; subset false). Precision guard. GREEN."""
    assert not _co_clustered(string_clusters, "aler", "mualer")


def test_taylor_nguyen_handle_co_clusters(string_clusters):
    """``taylor.nguyen`` ↔ ``Taylor Nguyen`` via handle decomposition. The
    thin-handle invariant; GREEN; a hard gate."""
    assert _co_clustered(string_clusters, "nguyen_handle", "nguyen_full")


def test_nx_lab_surname_collision_co_clusters(string_clusters):
    """The NX-Lab surname collision (ADR 0010 §Axis 5, a string-deterministic
    cell): ``NX Lab`` and ``Luke Gray Lab`` share surname ``lab`` (surname lane),
    and ``Luke Gray`` ⊂ ``Luke Gray Lab`` (subset lane), so at the CANDIDATE layer
    all three co-cluster. This is expected — excluding NX Lab from the Luke Gray
    *merge* is owned by the apply-time corroboration partition (Axis 3 / Tier B),
    NOT by clustering. We pin the candidate-layer reality as a hard gate."""
    assert _co_clustered(string_clusters, "nx_lab", "luke_gray_lab")
    assert _co_clustered(string_clusters, "nx_lab", "luke_gray")


def test_tamola_string_isolated_from_luke_gray(string_clusters):
    """``Tamola`` shares no token or surname with ``Luke Gray`` / ``NX Lab``, so no
    string lane welds it in — at the candidate layer it stays out of the Luke Gray
    component. (On the live vault Tamola is embedding-dependent; here we pin only
    the honest string-deterministic cell.) GREEN."""
    assert not _co_clustered(string_clusters, "tamola", "luke_gray")
    assert not _co_clustered(string_clusters, "tamola", "nx_lab")


# ── measure-first — embedding-dependent cells (NOT pre-pinned) ───────────────────
def test_avery_pair_below_recall_floor(embedded_store: InMemoryStore):
    """MEASURE-FIRST (ADR 0010 §Axis 5): ``Avery Quinn``/``Avery Blake``
    is a MUST-MERGE pair with no sound auto-merge path under the current design —
    no shared surname, neither subsets the other, and the embedding cosine sits
    below ``RECONCILE_RECALL_FLOOR``. We assert the MEASURED relationship (below
    floor → embedding lane does not fire → queued, never auto-merged), not a
    co-cluster the current code cannot produce. Auto-merge is open work gated on
    Identifier ingest (ADR 0010 Q1)."""
    mh = embedded_store.get_node("avery_q")
    mf = embedded_store.get_node("avery_b")
    assert mh is not None and mf is not None
    assert mh.embedding is not None and mf.embedding is not None, (
        "precondition: gold nodes must be embedded or the cell silently degrades"
    )
    cosine = _cosine(mh.embedding, mf.embedding)
    assert cosine < RECONCILE_RECALL_FLOOR, (
        f"measured Avery Q./Avery B. cosine={cosine:.4f} is at/above the recall "
        f"floor {RECONCILE_RECALL_FLOOR} on this fixture — the documented "
        f"below-floor reality has shifted; re-read ADR 0010 §Axis 5 before pinning"
    )


def test_aler_mualer_embedding_below_recall_floor(embedded_store: InMemoryStore):
    """Embedding-cell complement of the string Aler/Mualer guard: the cosine is
    below the recall floor on this fixture, so the embedding lane does not pair
    them either. Measured, not pinned."""
    aler = embedded_store.get_node("aler")
    mualer = embedded_store.get_node("mualer")
    assert aler is not None and mualer is not None
    assert aler.embedding is not None and mualer.embedding is not None, (
        "precondition: gold nodes must be embedded or the cell silently degrades"
    )
    cosine = _cosine(aler.embedding, mualer.embedding)
    assert cosine < RECONCILE_RECALL_FLOOR, (
        f"measured Aler/Mualer cosine={cosine:.4f} is at/above the recall floor "
        f"{RECONCILE_RECALL_FLOOR}; the distinct-guard's embedding cell has shifted"
    )


# ── RED-today gaps — xfail(strict=True), flip to hard gate when the phase lands ──
# ── GREEN as of P2 — flipped from xfail to hard gates (ADR 0010 Axis 1) ─────────
def test_nivod_ari_nivod_co_cluster(string_clusters):
    """MUST-MERGE, GREEN as of P2. ``token_subset_unordered`` shares the distinctive
    token ``nivod`` between ``{nivod} ⊂ {ari, nivod}`` and co-clusters the pair,
    dropping the shared-first-token requirement that previously blocked it (string
    lanes only — the embedding lane is deliberately held off here so the cell tests
    the string defect the ADR names, not an embedding accident). Was xfail(strict)
    under P1; closed by ADR 0010 P2 and now pinned as a hard gate."""
    assert _co_clustered(string_clusters, "nivod", "ari_nivod")


def test_tovereign_excluded_from_tovrin_cluster(string_clusters):
    """De-pollution, GREEN as of P2. ``Tovereign`` shares no token with ``Tovrin``
    but matches it fuzzily (``jaro_winkler=0.922`` on a 6-char token); P2's lexical
    length-gate (``JW_FUZZY_MIN_LEN``) drops the short-token fuzzy edge so
    ``Tovereign`` no longer joins the true ``Tovrin``/``Tovrin Kalia`` merge cluster
    (which still pairs via the exact shared token ``tovrin``). Was xfail(strict)
    under P1; closed by ADR 0010 P2 and now pinned as a hard gate."""
    assert not _co_clustered(string_clusters, "tovereign", "tovrin")
