"""ADR 0010 P3 — graded corroboration + distinguishing-edge veto (no LLM).

These tests exercise the unified ``_variant_evidence`` signal directly and the
``corroborated_ids`` partition it drives, over an ``InMemoryStore``. No LLM judge
is loaded: the evidence helpers are pure string/graph functions, and the
partition-through-``adjudicate_cluster`` cells use a trivial stub judge returning
same=True so the corroboration subset is the only moving part.

The gold cases (ADR 0010 §Gold-set walkthrough):

- ``taylor.nguyen`` ↔ ``Taylor Nguyen`` → discovery-only lexical recall → queued.
- ``Tovereign`` ↔ ``Tovrin`` → fuzzy only (JW 0.922, no shared token) → NOT corroborated.
- ``Tovrin`` ↔ ``Tovrin Kalia`` → identity (shared token ``tovrin``) → corroborated
  (the MUST-MERGE preserved).
- ``NX Lab`` / ``Tamola`` ↔ ``Luke Gray (Lab)`` → no shared token / no neighbour
  overlap → no identity/relational → never in ``corroborated_ids`` (over-merge guard).
- Veto — a distinguishing edge subtracts a member even when (the stub) judge says same.
- Relational-only — shared neighbours ≥ Jaccard floor, no name overlap → corroborated.
"""

from __future__ import annotations

from okto_neuron.core.schema import Edge, Node
from okto_neuron.reconcile.apply import is_high_confidence
from okto_neuron.reconcile.candidates import CandidateCluster
from okto_neuron.reconcile.propose import (
    REL_JACCARD,
    _variant_evidence,
    adjudicate_cluster,
)
from okto_neuron.resolve import MergeVerdict
from okto_neuron.store.memory import InMemoryStore


class StubJudge:
    """Fixed same=True@0.95 for every pair — isolates the corroboration partition
    from any judge behaviour (the judge engagement is P4, out of scope here)."""

    def __init__(self, same: bool = True, confidence: float = 0.95):
        self._v = MergeVerdict(same=same, confidence=confidence, reason="stub")

    def judge(self, candidate, existing, *, candidate_context="", existing_context=""):
        return self._v


def _node(node_id: str, title: str, *, type: str = "Agent") -> Node:
    return Node(id=node_id, type=type, title=title, content="")


def _cluster(member_ids) -> CandidateCluster:
    return CandidateCluster(
        cluster_id="c",
        type="Agent",
        member_ids=tuple(member_ids),
        lane_evidence={"lexical": ["x <-> y"]},
    )


def _corroborated_via_apply(store, member_ids) -> set[str]:
    """Run the real partition (adjudicate_cluster with a same@0.95 stub) and return
    the ``corroborated_ids`` set the auto-merge path would mint over."""
    verdict = adjudicate_cluster(_cluster(member_ids), store, judge=StubJudge())
    return set(verdict.corroborated_ids)


# ── discovery must not manufacture identity evidence ────────────────────────
def test_taylor_nguyen_handle_is_discovery_only_not_corroborated():
    """Handle decomposition recalls the pair, but punctuation folding is a broad
    discovery rule. It must not become identity-grade evidence for auto-merge."""
    store = InMemoryStore()
    canon = _node("full", "Taylor Nguyen")
    var = _node("handle", "taylor.nguyen")
    store.add_node(canon)
    store.add_node(var)
    ev = _variant_evidence(store, canon, var)
    assert ev == {"fuzzy"}
    assert "negative" not in ev
    corroborated = _corroborated_via_apply(store, ["full", "handle"])
    assert corroborated != {"full", "handle"}
    assert len(corroborated) == 1


def test_underscore_discovery_match_is_not_identity_or_auto_merge():
    """Underscore-to-space folding belongs only to candidate discovery."""
    store = InMemoryStore()
    canonical = _node("canonical", "Data Pipeline Team")
    variant = _node("variant", "data_pipeline")
    store.add_node(canonical)
    store.add_node(variant)
    evidence = _variant_evidence(store, canonical, variant)
    assert "identity" not in evidence
    assert len(_corroborated_via_apply(store, ["canonical", "variant"])) == 1


# ── _variant_evidence: fuzzy-only is NOT corroborating ───────────────────────────
def test_tovereign_tovrin_is_fuzzy_only_not_corroborated():
    """``Tovereign`` ↔ ``Tovrin``: ``jaro_winkler``=0.922 ≥ JW_STRONG but they share
    NO token → fuzzy-supporting only, never sufficient alone (invariant 6). Excluded
    from ``corroborated_ids``."""
    store = InMemoryStore()
    canon = _node("tovrin", "Tovrin")
    var = _node("tovereign", "Tovereign")
    store.add_node(canon)
    store.add_node(var)
    ev = _variant_evidence(store, canon, var)
    assert ev == {"fuzzy"}, ev
    assert not (ev & {"identity", "relational"})
    # The pair never co-corroborates: whichever ``select_canonical`` picks is the
    # lone survivor, the fuzzy-only variant is dropped (never both folded together).
    corroborated = _corroborated_via_apply(store, ["tovrin", "tovereign"])
    assert corroborated != {"tovrin", "tovereign"}
    assert len(corroborated) == 1


# ── _variant_evidence: shared distinctive token is identity (MUST-MERGE preserved) ─
def test_tovrin_kalia_shared_token_is_identity():
    """``Tovrin`` ↔ ``Tovrin Kalia`` share the distinctive token ``tovrin`` →
    identity-grade → corroborated. The MUST-MERGE that passed today via the lexical
    lane is preserved (a blanket 'lexical never sufficient' would wrongly regress it)."""
    store = InMemoryStore()
    canon = _node("tovrin_kalia", "Tovrin Kalia")
    var = _node("tovrin", "Tovrin")
    store.add_node(canon)
    store.add_node(var)
    ev = _variant_evidence(store, canon, var)
    assert "identity" in ev
    assert "fuzzy" not in ev  # a shared distinctive token denies the fuzzy tier
    assert _corroborated_via_apply(store, ["tovrin_kalia", "tovrin"]) == {
        "tovrin_kalia",
        "tovrin",
    }


def test_explicit_distinct_negative_cache_blocks_retroactive_judge_merge():
    store = InMemoryStore()
    canonical = _node("canonical", "Taylor Nguyen")
    variant = _node("variant", "Taylor")
    store.add_node(canonical)
    store.add_node(variant)
    judge = StubJudge()

    verdict = adjudicate_cluster(
        _cluster(["canonical", "variant"]),
        store,
        judge=judge,
        merge_blocked=lambda left, right: {left, right} == {"canonical", "variant"},
    )

    assert verdict.same is False
    assert verdict.corroborated_ids == ()


def test_distinct_pair_between_variants_blocks_transitive_retroactive_merge():
    store = InMemoryStore()
    canonical = _node("canonical", "Taylor Alexandra Nguyen")
    first = _node("first", "Taylor Nguyen")
    second = _node("second", "Taylor A Nguyen")
    for node in (canonical, first, second):
        store.add_node(node)

    verdict = adjudicate_cluster(
        _cluster(["canonical", "first", "second"]),
        store,
        judge=StubJudge(),
        merge_blocked=lambda left, right: {left, right} == {"first", "second"},
    )

    assert verdict.same is True
    assert verdict.member_ids == ("canonical", "first")
    assert "second" not in verdict.corroborated_ids


def test_tovereign_dropped_but_tovrin_kalia_kept_in_one_cluster():
    """The whole polluted cluster: ``Tovrin Kalia`` folds (shared token), ``Tovereign``
    is dropped (fuzzy-only) — the corroborated subset is exactly the true merge."""
    store = InMemoryStore()
    store.add_node(_node("tovrin_kalia", "Tovrin Kalia"))
    store.add_node(_node("tovrin", "Tovrin"))
    store.add_node(_node("tovereign", "Tovereign"))
    corroborated = _corroborated_via_apply(store, ["tovrin_kalia", "tovrin", "tovereign"])
    assert "tovereign" not in corroborated
    assert {"tovrin", "tovrin_kalia"} <= corroborated


# ── over-merge guard: NX Lab / Tamola never corroborate against Luke Gray ────────
def test_nx_lab_no_evidence_against_luke_gray():
    """``NX Lab`` vs canonical ``Luke Gray Lab``: share only ``lab`` (3 chars /
    generic → not distinctive), no neighbour overlap → no identity, no relational →
    never in ``corroborated_ids``."""
    store = InMemoryStore()
    canon = _node("luke_gray_lab", "Luke Gray Lab")
    nx = _node("nx_lab", "NX Lab")
    store.add_node(canon)
    store.add_node(nx)
    ev = _variant_evidence(store, canon, nx)
    assert not (ev & {"identity", "relational"}), ev


def test_tamola_no_evidence_against_luke_gray():
    """``Tamola`` shares no token and no neighbour with ``Luke Gray`` → no
    corroborating evidence → never folded."""
    store = InMemoryStore()
    canon = _node("luke_gray", "Luke Gray")
    tamola = _node("tamola", "Tamola")
    store.add_node(canon)
    store.add_node(tamola)
    ev = _variant_evidence(store, canon, tamola)
    assert not (ev & {"identity", "relational"}), ev


def test_luke_gray_cluster_collapses_to_corroborated_subset():
    """The whole NX cluster: ``Luke Gray Lab`` shares tokens ``luke``+``gray`` with
    ``Luke Gray`` → identity → folded; ``NX Lab`` and ``Tamola`` carry no evidence →
    queued, never folded. Auto-merge subset ≤ {Luke Gray, Luke Gray Lab}."""
    store = InMemoryStore()
    store.add_node(_node("luke_gray", "Luke Gray"))
    store.add_node(_node("luke_gray_lab", "Luke Gray Lab"))
    store.add_node(_node("nx_lab", "NX Lab"))
    store.add_node(_node("tamola", "Tamola"))
    corroborated = _corroborated_via_apply(
        store, ["luke_gray", "luke_gray_lab", "nx_lab", "tamola"]
    )
    assert "nx_lab" not in corroborated
    assert "tamola" not in corroborated
    assert corroborated <= {"luke_gray", "luke_gray_lab"}


# ── the veto: a distinguishing edge SUBTRACTS even an identity-grade member ───────
def test_distinguishing_edge_vetoes_identity_member():
    """The new power: a variant that carries BOTH identity-grade evidence (shared
    token) AND a ``distinguished_from`` edge to the canonical is REMOVED from
    ``corroborated_ids`` even though (the stub) judge said same@0.95. Negative
    overrides identity — corroboration subtracts a wrongly-merged member."""
    store = InMemoryStore()
    canon = _node("tovrin_kalia", "Tovrin Kalia")
    var = _node("tovrin", "Tovrin")  # shares distinctive token `tovrin` → identity
    store.add_node(canon)
    store.add_node(var)
    store.add_edge(Edge(type="distinguished_from", src="tovrin", dst="tovrin_kalia"))
    ev = _variant_evidence(store, canon, var)
    assert "identity" in ev  # the identity evidence is still present...
    assert "negative" in ev  # ...but the veto fires
    # judge said same, but the vetoed member is absent from the corroborated subset
    verdict = adjudicate_cluster(_cluster(["tovrin_kalia", "tovrin"]), store, judge=StubJudge())
    assert verdict.same is True
    assert "tovrin" in verdict.member_ids  # the judge merged it (survivor)
    assert "tovrin" not in verdict.corroborated_ids  # but corroboration subtracted it
    assert set(verdict.corroborated_ids) == {"tovrin_kalia"}
    # a single-member corroborated subset cannot auto-merge (gate needs ≥2)
    assert is_high_confidence(verdict) is False


def test_veto_in_reverse_edge_direction():
    """The veto reads node-anchored from BOTH ends, so a distinguishing edge from
    canonical → variant (the reverse direction) also fires."""
    store = InMemoryStore()
    canon = _node("tovrin_kalia", "Tovrin Kalia")
    var = _node("tovrin", "Tovrin")
    store.add_node(canon)
    store.add_node(var)
    store.add_edge(Edge(type="not_same_as", src="tovrin_kalia", dst="tovrin"))
    ev = _variant_evidence(store, canon, var)
    assert "negative" in ev
    assert _corroborated_via_apply(store, ["tovrin_kalia", "tovrin"]) == {"tovrin_kalia"}


def test_alias_of_is_NOT_a_veto():
    """``alias_of`` denotes SAMENESS (``bob alias_of robert`` = the two ARE the same),
    so it is deliberately NOT in ``_VETO_PREDICATES`` — an ``alias_of`` edge must
    NOT remove the variant. (It is the one member of resolve's
    ``_DISTINGUISHING_PREDICATES`` the veto correctly omits.)"""
    store = InMemoryStore()
    canon = _node("tovrin_kalia", "Tovrin Kalia")
    var = _node("tovrin", "Tovrin")
    store.add_node(canon)
    store.add_node(var)
    store.add_edge(Edge(type="alias_of", src="tovrin", dst="tovrin_kalia"))
    ev = _variant_evidence(store, canon, var)
    assert "negative" not in ev, "alias_of means SAME, must not veto"
    assert _corroborated_via_apply(store, ["tovrin_kalia", "tovrin"]) == {
        "tovrin_kalia",
        "tovrin",
    }


def test_veto_in_larger_cluster_excludes_from_corroborated_subset():
    """Multi-member veto: a cluster with a clean identity variant AND a vetoed
    identity variant. The vetoed member is EXCLUDED from ``corroborated_ids`` (so the
    auto-merge mints only over the canonical + clean variant), while the clean variant
    still folds.

    KNOWN P3 LIMITATION (documented, not a bug to fix here): ADR 0010 invariant 7
    distinguishes "uncorroborated → queued" from "vetoed → removed". At P3 the vetoed
    member is excluded from the merge (correct) but, because it is a survivor the judge
    called 'same', it lands in apply.py's leftover queue alongside merely-uncorroborated
    members. Fully suppressing its queue entry needs a `vetoed_ids` signal on
    ``ClusterVerdict`` + an apply.py change, deliberately out of P3 scope (the task
    scopes the change to how ``corroborated_ids`` is built). This test pins the
    corroboration-subset behavior that IS in scope."""
    store = InMemoryStore()
    store.add_node(_node("canon", "Luke Gray Kalia"))
    store.add_node(_node("good", "Luke Gray"))  # shares luke+gray → identity, no veto
    store.add_node(_node("bad", "Casey Kalia"))  # shares kalia → identity...
    store.add_edge(Edge(type="distinguished_from", src="bad", dst="canon"))  # ...but vetoed
    verdict = adjudicate_cluster(_cluster(["canon", "good", "bad"]), store, judge=StubJudge())
    assert verdict.same is True
    assert "bad" in verdict.member_ids  # the judge merged it (survivor)
    assert "bad" not in verdict.corroborated_ids  # but the veto subtracted it
    assert {"canon", "good"} <= set(verdict.corroborated_ids)
    assert is_high_confidence(verdict) is True  # canonical + clean variant → auto-merge


# ── relational-only: shared neighbours, no name overlap → corroborated ───────────
def test_relational_only_pair_corroborated():
    """Two names with NO lexical overlap but a shared neighbour set above the Jaccard
    floor → ``relational`` evidence → corroborated. Relational is sufficient alone."""
    store = InMemoryStore()
    canon = _node("canon", "Robert Smith")
    var = _node("var", "Bobby Jones")  # no shared token, JW low → no identity/fuzzy
    store.add_node(canon)
    store.add_node(var)
    # three shared neighbours → Jaccard 3/3 = 1.0 ≥ REL_JACCARD
    for nid in ("proj_a", "proj_b", "proj_c"):
        store.add_node(_node(nid, nid, type="Activity"))
        store.add_edge(Edge(type="works_on", src="canon", dst=nid))
        store.add_edge(Edge(type="works_on", src="var", dst=nid))
    ev = _variant_evidence(store, canon, var)
    assert "relational" in ev
    assert "identity" not in ev  # purely relational
    assert _corroborated_via_apply(store, ["canon", "var"]) == {"canon", "var"}


def test_relational_below_floor_not_corroborated():
    """Sanity floor: a single shared neighbour out of many disjoint ones falls below
    ``REL_JACCARD`` → no relational evidence (and no name overlap) → not corroborated."""
    store = InMemoryStore()
    canon = _node("canon", "Robert Smith")
    var = _node("var", "Bobby Jones")
    store.add_node(canon)
    store.add_node(var)
    # 1 shared neighbour, 4 disjoint each → Jaccard 1/9 ≈ 0.11 < REL_JACCARD (0.25)
    store.add_node(_node("shared", "shared", type="Activity"))
    store.add_edge(Edge(type="works_on", src="canon", dst="shared"))
    store.add_edge(Edge(type="works_on", src="var", dst="shared"))
    for i in range(4):
        a, b = f"ca{i}", f"vb{i}"
        store.add_node(_node(a, a, type="Activity"))
        store.add_node(_node(b, b, type="Activity"))
        store.add_edge(Edge(type="works_on", src="canon", dst=a))
        store.add_edge(Edge(type="works_on", src="var", dst=b))
    ev = _variant_evidence(store, canon, var)
    assert "relational" not in ev, ev
    assert _corroborated_via_apply(store, ["canon", "var"]) == {"canon"}


def test_rel_jaccard_floor_is_unchanged():
    """Guard: the relational floor used here is the shipped ``REL_JACCARD`` (0.25),
    not a re-tuned value — ADR 0010 Q2 is deferred."""
    assert REL_JACCARD == 0.25
