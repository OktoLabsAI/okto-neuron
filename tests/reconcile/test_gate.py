"""adjudicate_cluster gate logic with a STUB judge (no LLM)."""

from __future__ import annotations

from okto_neuron.core.schema import Edge, Node
from okto_neuron.reconcile.candidates import CandidateCluster
from okto_neuron.reconcile.propose import (
    RECONCILE_AUTO_CONFIDENCE,
    adjudicate_cluster,
    parse_cluster_verdict,
    select_canonical,
)
from okto_neuron.resolve import MergeVerdict
from okto_neuron.store.memory import InMemoryStore
from okto_neuron.reconcile.apply import is_high_confidence


class StubJudge:
    """Returns a fixed verdict for every pair; records call count."""

    def __init__(self, same: bool, confidence: float, reason: str = "stub"):
        self._v = MergeVerdict(same=same, confidence=confidence, reason=reason)
        self.calls = 0

    def judge(self, candidate, existing, *, candidate_context="", existing_context=""):
        self.calls += 1
        return self._v


def _node(node_id, title, *, type="Agent") -> Node:
    return Node(id=node_id, type=type, title=title, content="")


def _cluster(member_ids, type="Agent") -> CandidateCluster:
    return CandidateCluster(
        cluster_id="c",
        type=type,
        member_ids=tuple(member_ids),
        lane_evidence={"lexical": ["x <-> y"]},
    )


def test_same_high_conf_discovery_only_handle_is_queued():
    store = InMemoryStore()
    store.add_node(_node("canon", "Taylor Nguyen"))
    store.add_node(_node("var", "taylor.nguyen"))  # recalled lexically, not exact identity
    judge = StubJudge(same=True, confidence=0.95)
    verdict = adjudicate_cluster(_cluster(["canon", "var"]), store, judge=judge)
    assert verdict.same is True
    assert verdict.corroboration in {"lexical", "both"}
    assert is_high_confidence(verdict) is False
    assert len(verdict.corroborated_ids) == 1


def test_same_low_conf_goes_to_queue_not_automerge():
    store = InMemoryStore()
    store.add_node(_node("canon", "Taylor Nguyen"))
    store.add_node(_node("var", "taylor.nguyen"))
    judge = StubJudge(same=True, confidence=0.82)  # below 0.9 auto floor
    verdict = adjudicate_cluster(_cluster(["canon", "var"]), store, judge=judge)
    assert verdict.same is True
    assert verdict.confidence < RECONCILE_AUTO_CONFIDENCE
    assert is_high_confidence(verdict) is False  # → queue


def test_distinct_verdict_is_skipped():
    store = InMemoryStore()
    store.add_node(_node("a", "Aler Dalvic"))
    store.add_node(_node("b", "Mualer Disija"))
    judge = StubJudge(same=False, confidence=0.0)
    verdict = adjudicate_cluster(_cluster(["a", "b"]), store, judge=judge)
    assert verdict.same is False
    assert is_high_confidence(verdict) is False


def test_thin_discovery_only_cluster_needs_more_than_lexical_recall():
    # taylor.nguyen has ZERO edges. The lexical lane recalls it, but broad
    # punctuation folding alone is insufficient for an automatic merge.
    store = InMemoryStore()
    store.add_node(_node("canon", "Taylor Nguyen"))
    store.add_node(_node("var", "taylor.nguyen"))
    judge = StubJudge(same=True, confidence=0.95)
    verdict = adjudicate_cluster(_cluster(["canon", "var"]), store, judge=judge)
    assert verdict.corroboration == "lexical"  # NOT "none"
    assert is_high_confidence(verdict) is False


def test_canonical_is_highest_degree():
    store = InMemoryStore()
    rich = _node("rich", "Alex Rivera")
    thin = _node("thin", "alex")
    other = _node("other", "Project X", type="Concept")
    store.add_node(rich)
    store.add_node(thin)
    store.add_node(other)
    # give 'rich' an edge → degree 1; 'thin' degree 0
    store.add_edge(Edge(type="works_on", src="rich", dst="other"))
    canonical = select_canonical(store, [rich, thin])
    assert canonical.id == "rich"


def test_canonical_tiebreak_longest_title_then_smallest_id():
    store = InMemoryStore()
    # equal degree (0) → longest title wins
    short = _node("zzz", "Acme")
    long = _node("aaa", "Acme Corporation")
    store.add_node(short)
    store.add_node(long)
    assert select_canonical(store, [short, long]).id == "aaa"
    # equal degree AND equal title length → lexicographically smallest id wins
    a = _node("aaa", "Acme Corp")
    b = _node("bbb", "Acme Corp")
    store2 = InMemoryStore()
    store2.add_node(a)
    store2.add_node(b)
    assert select_canonical(store2, [a, b]).id == "aaa"


# ── compare/select parser (DISTINCT-on-unparseable, model-free) ─────────────────
def test_parse_cluster_verdict_valid():
    text = '{"same_indices": [0, 2], "confidence": 0.93, "reason": "same person"}'
    parsed = parse_cluster_verdict(text, n_others=3)
    assert parsed is not None
    idxs, conf, reason = parsed
    assert idxs == {0, 2}
    assert conf == 0.93
    assert "same person" in reason


def test_parse_cluster_verdict_empty_list_is_none_match():
    # A VALID parse with no matches → empty survivor set (authoritative 'none match',
    # NOT a fall-back-to-pairwise None).
    parsed = parse_cluster_verdict('{"same_indices": [], "confidence": 0.4}', n_others=3)
    assert parsed is not None
    idxs, conf, _ = parsed
    assert idxs == set()
    assert conf == 0.4


def test_parse_cluster_verdict_malformed_returns_none_safe_default():
    # Unparseable / missing key → None so the caller falls back to pairwise.
    assert parse_cluster_verdict("not json at all", n_others=3) is None
    assert parse_cluster_verdict('{"confidence": 0.9}', n_others=3) is None
    assert parse_cluster_verdict('{"same_indices": "nope"', n_others=3) is None


def test_parse_cluster_verdict_drops_out_of_range_indices():
    # Indices beyond the candidate count are filtered, not crashed on.
    parsed = parse_cluster_verdict('{"same_indices": [0, 9, -1], "confidence": 1.5}', n_others=2)
    assert parsed is not None
    idxs, conf, _ = parsed
    assert idxs == {0}  # 9 and -1 dropped
    assert conf == 1.0  # clamped into [0, 1]


def test_same_confidence_is_min_of_survivors():
    store = InMemoryStore()
    store.add_node(_node("canon", "Acme Corp"))
    store.add_node(_node("v1", "Acme Corporation"))
    store.add_node(_node("v2", "ACME corp"))
    judge = StubJudge(same=True, confidence=0.91)
    verdict = adjudicate_cluster(_cluster(["canon", "v1", "v2"]), store, judge=judge)
    assert verdict.same is True
    assert verdict.confidence == 0.91
