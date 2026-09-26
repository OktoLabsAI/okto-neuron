"""Candidate-generation lanes — recall correctness, precision traps, cap."""

from __future__ import annotations

from okto_neuron.core.schema import Node
from okto_neuron.reconcile.candidates import (
    RECONCILE_CLASS_CAP,
    _split_oversize,
    _surname,
    connected_components,
    email_handle_tokens,
    generate_candidate_clusters,
    jaro_winkler,
    token_subset,
    token_subset_unordered,
)
from okto_neuron.store.memory import InMemoryStore


def _node(node_id: str, title: str, *, type="Agent", embedding=None) -> Node:
    return Node(id=node_id, type=type, title=title, content="", embedding=embedding)


# ── string helpers ──────────────────────────────────────────────────────────────
def test_jaro_winkler_aler_vs_mualer_low():
    # The precision trap: Aler (Gasevic) vs Mualer (Disija) must score LOW so the
    # lexical lane never pairs them.
    assert jaro_winkler("Aler", "Mualer") < 0.90


def test_jaro_winkler_identical_is_one():
    assert jaro_winkler("Alex", "Alex") == 1.0


def test_discovery_surface_recalls_unicode_and_separator_variants_only_as_candidates():
    assert jaro_winkler("\u0390", "\u03aa\u0301") == 1.0
    assert jaro_winkler("Graph_Store", "Graph Store") == 1.0

    store = InMemoryStore()
    store.add_node(_node("underscore", "Graph_Store", type="Concept"))
    store.add_node(_node("spaced", "Graph Store", type="Concept"))
    clusters = generate_candidate_clusters(store, embedder=None)

    assert len(clusters) == 1
    assert set(clusters[0].member_ids) == {"underscore", "spaced"}


def test_email_handle_tokens():
    assert email_handle_tokens("taylor.nguyen") == "taylor nguyen"
    assert email_handle_tokens("first_last") == "first last"
    assert email_handle_tokens("taylor.nguyen@univ.edu") == "taylor nguyen"
    # not a handle
    assert email_handle_tokens("Taylor Nguyen") is None
    assert email_handle_tokens("Alex") is None


def test_surname_is_a_word_not_a_trailing_number():
    """A surname is a WORD. A trailing all-digit token is an identifier or a date
    fragment, so it must not be read as one: `_surname("TASK-01") == "01"` made
    every numbered or dated title in a corpus share one blocking family, and the
    surname lane is used as an identity-grade signal by
    `resolve._structural_alias_score`. Measured on a live 386-node graph, that
    handed `DARF Unificado jul/2026` 15 candidates tied at 1.0, nearly all pure
    numeric collisions. Single-token titles still have no surname at all."""
    assert _surname("Casey Lee") == "lee"
    assert _surname("Waverly Chen") == "chen"
    assert _surname("TASK-01") is None
    assert _surname("Sprint 01") is None
    assert _surname("ECD 2026") is None
    assert _surname("DARF Unificado jul/2026") is None
    # unchanged: a single token has no surname either way
    assert _surname("Karine") is None


# ── connected components ────────────────────────────────────────────────────────
def test_connected_components_merges_transitive():
    comps = connected_components([("a", "b"), ("b", "c"), ("x", "y")])
    sets = sorted([frozenset(c) for c in comps], key=lambda s: sorted(s)[0])
    assert frozenset({"a", "b", "c"}) in sets
    assert frozenset({"x", "y"}) in sets


def test_connected_components_drops_singletons():
    assert connected_components([]) == []


# ── lanes ──────────────────────────────────────────────────────────────────────
def test_aler_mualer_not_paired():
    store = InMemoryStore()
    store.add_node(_node("aler", "Aler Dalvic"))
    store.add_node(_node("mualer", "Mualer Disija"))
    clusters = generate_candidate_clusters(store, embedder=None)
    # distinct surnames, distinct first names, no embedding → no cluster
    assert clusters == []


def test_taylor_nguyen_recalled_via_handle_with_zero_edges():
    store = InMemoryStore()
    store.add_node(_node("handle", "taylor.nguyen"))
    store.add_node(_node("full", "Taylor Nguyen"))
    clusters = generate_candidate_clusters(store, embedder=None)
    assert len(clusters) == 1
    assert set(clusters[0].member_ids) == {"handle", "full"}
    assert "handle" in clusters[0].lane_evidence


def test_widened_band_recalls_alex_pair_missed_by_082():
    # Two vectors whose cosine is ~0.7177 — below 0.82 (missed Alex) but above
    # the widened 0.65 floor. Distinct titles so ONLY the embedding lane fires.
    import math

    theta = math.acos(0.7177)
    a = [1.0, 0.0]
    b = [math.cos(theta), math.sin(theta)]
    store = InMemoryStore()
    store.add_node(_node("alex", "Alex Rivera", embedding=a))
    store.add_node(_node("mh", "M.H.", embedding=b))
    clusters = generate_candidate_clusters(store, embedder=None)
    assert len(clusters) == 1
    assert set(clusters[0].member_ids) == {"alex", "mh"}
    assert "embedding" in clusters[0].lane_evidence


def test_082_floor_would_have_missed_it():
    # Same pair at the OLD floor recalls nothing — proves the widening is load-bearing.
    import math

    theta = math.acos(0.7177)
    a = [1.0, 0.0]
    b = [math.cos(theta), math.sin(theta)]
    store = InMemoryStore()
    store.add_node(_node("alex", "Alex Rivera", embedding=a))
    store.add_node(_node("mh", "M.H.", embedding=b))
    clusters = generate_candidate_clusters(store, embedder=None, recall_floor=0.82)
    assert clusters == []


def test_class_size_cap_splits_oversize_cluster():
    store = InMemoryStore()
    # cap+3 near-identical titles all pair lexically → one giant component → split.
    n = RECONCILE_CLASS_CAP + 3
    for i in range(n):
        store.add_node(_node(f"node{i}", "Acme Corporation"))
    clusters = generate_candidate_clusters(store, embedder=None)
    assert clusters, "expected at least one cluster"
    assert all(len(c.member_ids) <= RECONCILE_CLASS_CAP for c in clusters)


def test_cross_type_never_paired():
    store = InMemoryStore()
    store.add_node(_node("a", "Apollo", type="Agent"))
    store.add_node(_node("c", "Apollo", type="Concept"))
    clusters = generate_candidate_clusters(store, embedder=None)
    # identical titles but different types → no cluster
    assert clusters == []


# ── token-subset lane (first-name/full-name containment chain) ──────────────────
def test_token_subset_first_name_full_name_chain():
    # {alex} ⊂ {alex, rivera} ⊂ {alex, jordan, rivera, blake}
    assert token_subset("Alex", "Alex Rivera")
    assert token_subset("Alex Rivera", "Alex Jordan Rivera Blake")
    assert token_subset("Alex", "Alex Jordan Rivera Blake")


def test_token_subset_rejects_aler_mualer_precision_trap():
    # {aler} ⊄ {mualer, disija}: neither subsets the other AND first tokens differ.
    assert not token_subset("Aler", "Mualer Disija")
    assert not token_subset("Aler Dalvic", "Mualer Disija")


def test_token_subset_not_a_substring_test():
    # Shared char-prefix but distinct whole tokens must NOT qualify (no substring).
    assert not token_subset("NX", "HPX")
    assert not token_subset("NX", "HPX app")  # {nx} vs {hpx, app}: nx ∉ longer set
    # equal token sets are not a proper subset
    assert not token_subset("Acme Corp", "Corp Acme")


def test_token_subset_requires_shared_first_token():
    # proper subset by tokens but DIFFERENT first token → not the name-chain shape
    assert not token_subset("Smith", "John Smith")


# ── order-insensitive token-subset lane (ADR 0010 Axis 1) ───────────────────────
def test_token_subset_unordered_pairs_nivod_ari_nivod():
    # {nivod} ⊂ {ari, nivod}, shared distinctive token `nivod` (5 chars). The
    # ordered token_subset misses this (first tokens differ); the unordered one
    # recovers it.
    assert token_subset_unordered("Nivod", "Ari Nivod")
    assert token_subset_unordered("Ari Nivod", "Nivod")
    assert not token_subset("Nivod", "Ari Nivod")  # ordered lane still rejects


def test_token_subset_unordered_rejects_aler_mualer():
    # {aler} ⊄ {mualer, disija}: `aler` is not a token-element of the other set
    # (aler ≠ mualer), so no proper subset → never pairs. Precision guard.
    assert not token_subset_unordered("Aler", "Mualer Disija")
    assert not token_subset_unordered("Aler Dalvic", "Mualer Disija")


def test_token_subset_unordered_requires_distinctive_shared_token():
    # {lab} ⊂ {luke, gray, lab} as sets, but the only shared token `lab` is 3 chars
    # / generic → not distinctive → this lane adds no pair (NX-Lab precision).
    assert not token_subset_unordered("NX Lab", "Luke Gray Lab")
    # Sharing only a generic ≥4-char word (`team`) is also not distinctive.
    assert not token_subset_unordered("Team", "Alpha Team")


def test_token_subset_unordered_recalls_nivod_with_no_embedding():
    store = InMemoryStore()
    store.add_node(_node("nivod", "Nivod"))
    store.add_node(_node("ari_nivod", "Ari Nivod"))
    clusters = generate_candidate_clusters(store, embedder=None)
    assert len(clusters) == 1
    assert set(clusters[0].member_ids) == {"nivod", "ari_nivod"}
    assert "subset_unordered" in clusters[0].lane_evidence


def test_tovereign_not_welded_into_tovrin_by_short_fuzzy():
    # jaro_winkler("Tovrin","Tovereign")=0.922 ≥ JW_STRONG, but the shorter title
    # `Tovrin` is 6 chars < JW_FUZZY_MIN_LEN → the fuzzy edge is gated. The true
    # merge Tovrin/Tovrin Kalia still pairs via the exact shared token `tovrin`.
    store = InMemoryStore()
    store.add_node(_node("tovrin", "Tovrin"))
    store.add_node(_node("tovrin_kalia", "Tovrin Kalia"))
    store.add_node(_node("tovereign", "Tovereign"))
    clusters = generate_candidate_clusters(store, embedder=None)
    tovrin_members: set[str] = set()
    for c in clusters:
        if "tovrin" in c.member_ids:
            tovrin_members = set(c.member_ids)
    assert tovrin_members == {"tovrin", "tovrin_kalia"}
    assert "tovereign" not in tovrin_members


def test_identical_short_single_token_titles_still_co_cluster():
    # The length-gate only de-pollutes NON-equal short fuzzy matches. Two identical
    # short single-token Agents (jw=1.0, no surname, no subset) must STILL pair —
    # an exact duplicate is not a coincidental near-spelling.
    store = InMemoryStore()
    store.add_node(_node("d1", "Tamola"))
    store.add_node(_node("d2", "Tamola"))
    clusters = generate_candidate_clusters(store, embedder=None)
    assert len(clusters) == 1
    assert set(clusters[0].member_ids) == {"d1", "d2"}


def test_subset_lane_recalls_first_name_full_name_with_no_embedding():
    store = InMemoryStore()
    store.add_node(_node("short", "Alex"))
    store.add_node(_node("long", "Alex Jordan Rivera Blake"))
    clusters = generate_candidate_clusters(store, embedder=None)
    assert len(clusters) == 1
    assert set(clusters[0].member_ids) == {"short", "long"}
    assert "subset" in clusters[0].lane_evidence


def test_numbered_titles_are_not_welded_by_a_shared_trailing_number():
    """REGRESSION (live 386-node graph): `_surname` returned the trailing token of
    a title, so `TASK-01`, `Sprint 01` and `CNAE 6201-5/01` all "shared the
    surname" `01`, and everything dated 2026 shared `2026`. The surname lane was
    then the ONLY lane admitting those pairs — their Jaro-Winkler scores are
    0.39-0.59, nowhere near JW_STRONG — so a whole corpus of numbered items
    collapsed into one blocking family.

    Recall is NOT reduced (ADR 0042 — blocking is recall): the genuine
    `TASK-01` / `TASK-01: ...` variant pair still co-clusters through the
    token-subset lane on its shared WORD token `task`."""
    store = InMemoryStore()
    store.add_node(_node("task_01", "TASK-01", type="Concept"))
    store.add_node(
        _node("task_01_full", "TASK-01: Add is_recurrent Property to Expense Model", type="Concept")
    )
    store.add_node(_node("sprint_01", "Sprint 01", type="Concept"))
    store.add_node(_node("cnae", "CNAE 6201-5/01", type="Concept"))
    store.add_node(_node("darf", "DARF Unificado jul/2026", type="Concept"))
    store.add_node(_node("ecd", "ECD 2026", type="Concept"))

    clusters = generate_candidate_clusters(store, embedder=None)
    members = {mid: set(c.member_ids) for c in clusters for mid in c.member_ids}

    # the TRUE variant pair survives — token_subset, not the surname lane
    assert members.get("task_01") == {"task_01", "task_01_full"}
    # the numeric-token collisions are gone
    assert "sprint_01" not in members
    assert "cnae" not in members
    assert "darf" not in members
    assert "ecd" not in members


def test_split_oversize_conserves_every_member():
    # Member conservation: a cap split BOUNDS size but must never DROP a member.
    members = {f"n{i}" for i in range(RECONCILE_CLASS_CAP + 3)}
    member_list = sorted(members)
    pair_score = {
        frozenset((member_list[i], member_list[j])): 0.5
        for i in range(len(member_list))
        for j in range(i + 1, len(member_list))
    }
    buckets = _split_oversize(members, pair_score, RECONCILE_CLASS_CAP)
    out = [m for b in buckets for m in b]
    assert set(out) == members, "every input member must survive the cap split"
    assert len(out) == len(members), "no member duplicated across buckets"
    assert all(len(b) <= RECONCILE_CLASS_CAP for b in buckets), "cap respected"


def test_split_oversize_under_cap_passes_through_whole():
    members = {"a", "b", "c"}
    assert _split_oversize(members, {}, RECONCILE_CLASS_CAP) == [("a", "b", "c")]


# ── support types are never reconciled (regression: live apply deduped Claims) ──
def test_type_none_excludes_support_types():
    """type=None must scope to the 5 entity primitives only. Claim/Block/Document
    (support types) must NEVER form candidate clusters — a Claim is an atomic
    provenance unit, and deduping identical claims collapses distinct assertions."""
    store = InMemoryStore()
    # two genuine entity variants (should cluster)
    store.add_node(_node("a1", "Alex", type="Agent"))
    store.add_node(_node("a2", "Alex Rivera", type="Agent"))
    # identical Claim sentences from different blocks (must NOT cluster)
    store.add_node(_node("c1", "Riley Chen member_of NX", type="Claim"))
    store.add_node(_node("c2", "Riley Chen member_of NX", type="Claim"))
    store.add_node(_node("c3", "Riley Chen member_of NX", type="Claim"))
    # duplicate Document/Block titles (support — must NOT cluster)
    store.add_node(_node("d1", "action-items", type="Document"))
    store.add_node(_node("d2", "action-items", type="Document"))

    clusters = generate_candidate_clusters(store, embedder=None, type=None)
    clustered_types = set()
    for c in clusters:
        for mid in c.member_ids:
            node = store.get_node(mid)
            assert node is not None
            clustered_types.add(node.type)
    assert "Claim" not in clustered_types
    assert "Document" not in clustered_types
    assert "Block" not in clustered_types
    assert clustered_types <= {"Agent", "Activity", "InformationObject", "Concept", "Place"}


def test_explicit_support_type_yields_nothing():
    store = InMemoryStore()
    store.add_node(_node("c1", "X owns Y", type="Claim"))
    store.add_node(_node("c2", "X owns Y", type="Claim"))
    assert generate_candidate_clusters(store, embedder=None, type="Claim") == []


# ── cap must not sever a deterministic identity bridge (ADR 0010 real-vault fix) ──
def test_cap_keeps_identity_bridge_over_embedding_pollutants():
    """REGRESSION (ADR 0010, real-vault 2026-06-05): on the live vault the 0.65
    embedding floor fused 80/95 Agents into ONE oversize component; the cap's
    score-ordered split dropped the ``subset_unordered`` edge ``Nivod``<->``Ari
    Nivod`` (scored ``jaro_winkler=0.0`` for word-order variants) and severed the
    pair into different clusters. ``IDENTITY_EDGE_SCORE`` (deterministic
    containment edges score ABOVE the embedding band) makes the cap keep the
    identity bridge and shed the embedding pollutants instead.

    Geometry: ``Nivod`` is embedding-near three pollutants (cosine ~1.0) so they
    fuse into a >cap component; ``Ari Nivod`` is embedding-ORTHOGONAL to all of
    them (cosine 0), reachable ONLY by the ``subset_unordered`` identity edge to
    ``Nivod`` (jw=0). With cap=2 the score-ordered packer must process the
    identity edge (1.0) before any embedding edge (<1.0) and keep Nivod+Ari Nivod
    together. Before the fix (identity scored jw=0) the embedding edges packed
    first and Ari Nivod was orphaned."""
    near = [1.0, 0.0]
    orth = [0.0, 1.0]
    store = InMemoryStore()
    store.add_node(_node("nivod", "Nivod", embedding=near))
    store.add_node(_node("ari_nivod", "Ari Nivod", embedding=orth))  # identity-only link
    pollutant_vectors = ([1.0, 0.04], [1.0, -0.05], [1.0, 0.07])
    for k, vector in enumerate(pollutant_vectors):
        # Strong enough to fuse the component, but strictly below the 1.0
        # deterministic identity edge the test is intended to prioritize.
        store.add_node(_node(f"poll{k}", f"Pollutant Person {k}", embedding=vector))

    clusters = generate_candidate_clusters(store, embedder=None, type="Agent", class_cap=2)
    members = next((set(c.member_ids) for c in clusters if "nivod" in c.member_ids), set())
    assert "ari_nivod" in members, (
        "cap split severed the identity bridge — IDENTITY_EDGE_SCORE regressed"
    )
