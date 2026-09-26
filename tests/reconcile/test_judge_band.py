"""ADR 0010 Tier B — real-judge band + precision gate (laptop-only).

This is the **Tier B** harness from ADR 0010 §Axis 5 / Phased rollout P5: a K-run
real-judge stability + precision gate that validates the whole recall→judge→
corroboration arc end to end against the **live** LLM backend. Where Tier A
(``test_recall_gold.py``) pins the candidate layer with no LLM, Tier B drives the
full ``apply_reconciliation`` pipeline (cluster → judge → corroboration partition →
off-graph apply) K times and scores RATES, not a single verdict.

What it asserts (ADR 0010 §Axis 5 Tier B, §Precision invariants):

* **Hard precision guard (== 0 across ALL K, BOTH judge paths).** ``NX Lab`` and
  ``Tamola`` NEVER land in an auto-merged authority record's member set, and
  ``Aler``/``Mualer`` are never auto-merged together. A single violation FAILS —
  this is the irreversible-precision invariant (a false auto-merge collapses two
  real entities and is only reversible by an explicit unmerge).
* **Must-merge band.** ``Tovrin``/``Tovrin Kalia`` exercises out-of-example
  generalization of M3's bare-name/full-name rule; shared distinctive token
  ``tovrin`` supplies identity-grade corroboration. It must auto-merge at a
  same-rate ≥ a justified floor.
* **Must-distinct band.** Distinct pairs auto-merge at a rate ≤ a small band.
* **Alex H/F is measure-first** (ADR 0010 Q1): reported, never hard-failed. With
  no shared-neighbour recall lane in candidates today (that lane is P6, deferred),
  the pair does not even cluster, so it is neither merged nor queued — measured and
  reported as such.

Marker: ``acceptance_judge`` (laptop-only). The default ``uv run pytest`` selection
auto-skips it (see ``tests/conftest.py::pytest_collection_modifyitems``). Set
``OKTO_NEURON_JUDGE_BAND_API_BASE`` and run it explicitly with
``uv run pytest -m acceptance_judge -q``.

Reachability: the backend idle-unloads after 300s and cold-loads on first request
(seconds to minutes). Setup probes ``GET /v1/models`` + a generous warmup judge call
and ``pytest.skip``s cleanly when the endpoint is offline/unloadable — this is a laptop
validation gate, not a CI gate, so it must never hard-fail when the endpoint is off.

Off-graph invariant (ADR 0008): ``apply_reconciliation`` writes ONLY the two JSON
side-files; the ``SpyStore`` re-asserts zero graph writes across the whole K-run.
Each K iteration uses FRESH ``authority``/``queue`` side-files in their own temp dir
— an ``upsert`` persists, and shared side-files would corrupt the rate denominator.
"""

from __future__ import annotations

import os
from collections import Counter

import httpx
import pytest

from okto_neuron.companion import embedding_text_for
from okto_neuron.config._vault import ResolvedLLM
from okto_neuron.core.schema import Edge, Node
from okto_neuron.embed import get_provider as embed_provider
from okto_neuron.llm import get_provider as llm_provider
from okto_neuron.reconcile.apply import apply_reconciliation
from okto_neuron.reconcile.authority import AuthorityIndex
from okto_neuron.reconcile.candidates import (
    RECONCILE_RECALL_FLOOR,
    _cosine,
    generate_candidate_clusters,
)
from okto_neuron.reconcile.queue import ReconcileQueue
from okto_neuron.resolve import LLMMergeJudge
from okto_neuron.store.memory import InMemoryStore

pytestmark = pytest.mark.acceptance_judge

# ── backend wiring — an explicit OpenAI-compatible server ────────────────────────
# Replicates production's real judge (server/_curation.py::_build_judge builds an
# LLMMergeJudge from VaultConfig.llm.resolved("judge")). Constructing ResolvedLLM
# directly keeps this opt-in gate independent of the user's vault configuration.
# The endpoint is always explicit so a live run cannot silently select a retired
# machine. The shipping judge knobs remain temperature 0.2 and thinking OFF.
_API_BASE = os.environ.get("OKTO_NEURON_JUDGE_BAND_API_BASE", "").strip()
_MODEL = os.environ.get("OKTO_NEURON_JUDGE_BAND_MODEL", "unsloth/Qwen3.6-27B-NVFP4").strip()
# Generous cold-load absorption: the endpoint can take seconds-to-minutes to load.
_WARMUP_TIMEOUT = float(os.environ.get("OKTO_NEURON_JUDGE_BAND_WARMUP_TIMEOUT", "180"))
_K = int(os.environ.get("OKTO_NEURON_JUDGE_BAND_K", "10"))

# ── band thresholds (justified below) ───────────────────────────────────────────
# MUST-MERGE floor. The pair is deliberately absent from _VERDICT_SYSTEM and tests
# whether M3's bare-name/full-name principle generalizes beyond its Aurelin example.
# The judge runs at temperature 0.2 (near-deterministic for a binary verdict), so a
# healthy judge should merge this identity-corroborated pair on essentially every
# run. The floor is below 1.0 only to absorb K-sample sampling flake at temp>0 and an
# occasional unparseable reply (which defaults DISTINCT), not to tolerate genuine
# rule disengagement. A rate under 0.7 is a real generalization regression, not noise.
_MUST_MERGE_FLOOR = float(os.environ.get("OKTO_NEURON_JUDGE_BAND_MERGE_FLOOR", "0.7"))
# MUST-DISTINCT ceiling. A truly-distinct pair should auto-merge at ~0. A small band
# (≤ 0.1) absorbs at most a single stray sampled 'same' across K=10 without flipping
# the gate red; anything above it signals the conservative-distinct exemplars eroding.
# NOTE: this ceiling applies to pairs that CAN reach auto-merge (i.e. that corroborate
# if merged); the NX-Lab/Tamola/Aler-Mualer guards below are the HARD == 0 invariant.
_MUST_DISTINCT_CEILING = float(os.environ.get("OKTO_NEURON_JUDGE_BAND_DISTINCT_CEILING", "0.1"))


# ── gold fixture (titles + role-distinct content + honest corroboration edges) ───
# Per-family content is deliberately role-distinct so cross-family cosine stays below
# RECONCILE_RECALL_FLOOR (0.65) and the embedding lane does NOT weld unrelated
# families into one blob — a polluted Tovrin cluster whose canonical is a foreign
# higher-degree node would silently steal the Tovrin merge (corroborated_ids is built
# against the cluster canonical). The setup precondition below fails loud if that
# happens, rather than letting the must-merge rate quietly tank.
#
# Content is assigned a DISTINCT semantic domain per family (database/storage,
# research, sales, procurement, marketing, legal, finance, mobile-UI) so the only
# cross-pair cosines clearing RECONCILE_RECALL_FLOOR (0.65) are the two intended
# same-pairs. Measured (uv run python ... _cosine): tovrin/tovrin_kalia 0.887,
# luke_gray/luke_gray_lab 0.888; every other cross-family pair < 0.65. A shared-domain
# wording (the obvious "engineer"/"delivery" content) over-clusters at 0.68–0.75 and
# welds e.g. tamola/mualer into the Tovrin blob — measured, and the reason the content
# below is domain-separated rather than generic.
_GOLD: dict[str, tuple[str, str]] = {
    # MUST-MERGE: Tovrin / Tovrin Kalia (shared distinctive token 'tovrin' =
    # identity-grade; out-of-example M3 generalization). Same domain
    # (database/storage) so they read as one person and corroborate honestly.
    "tovrin": (
        "Tovrin",
        "Tovrin Kalia maintains the database replication cluster and storage backups.",
    ),
    "tovrin_kalia": (
        "Tovrin Kalia",
        "Owns database replication, storage backups, and the disk array.",
    ),
    # MUST-MERGE (open, measure-first): two different surnames, same first name.
    # No shared surname, neither subsets the other, cosine < floor (distinct domains),
    # and no shared-neighbour recall lane exists today (P6) → does not cluster.
    "alex_h": (
        "Alex Rivera",
        "Researches transformer architectures and writes the Okto Neuron knowledge graph.",
    ),
    "alex_f": ("Alex Morgan", "Closes enterprise sales deals across the LATAM retail market."),
    # MUST-STAY-DISTINCT: a team/org-unit and a person bearing a related name, plus
    # two more unrelated people. NX Lab and Tamola must NEVER fold into the Luke Gray
    # record — the irreversible-precision invariant. Luke Gray / Luke Gray Lab DO share
    # tokens (identity-grade) and MAY legitimately merge; that is allowed.
    "nx_lab": ("NX Lab", "Internal hardware procurement and printer-fleet logistics unit."),
    "tamola": ("Tamola", "Runs the marketing campaign analytics dashboards and ad spend."),
    "luke_gray": (
        "Luke Gray",
        "Negotiates the legal contract terms and signs the master agreement.",
    ),
    "luke_gray_lab": (
        "Luke Gray Lab",
        "Handles legal contract review and the signed master agreement.",
    ),
    # MUST-STAY-DISTINCT: two different people sharing a first name (distinct domains).
    "aler": ("Aler Dalvic", "Audits financial compliance and quarterly tax filings."),
    "mualer": ("Mualer Disija", "Designs the mobile app user interface and onboarding screens."),
}

# Honest corroboration edges (ADR 0010 invariant 4: node-anchored; here we simply
# seed shared neighbours). The Tovrin pair shares a neighbour (relational evidence,
# on top of its identity-grade shared token) so corroboration is exercised for real.
# NX Lab / Tamola are given a neighbour that is NOT shared with Luke Gray, so they
# have NEITHER identity NOR relational evidence toward Luke Gray — they cannot
# corroborate into the Luke Gray record even if the judge wrongly says 'same'. We do
# NOT fabricate any edge that would make NX Lab / Tamola falsely corroborate.
_NEIGHBOURS: dict[str, str] = {
    "n_partner_org": "Partner Org",
    "n_alpha": "Project Alpha",
    "n_beta": "Project Beta",
    "n_gamma": "Project Gamma",
}
_EDGES: list[tuple[str, str, str]] = [
    # Tovrin pair → shared neighbour (relational corroboration, honest).
    ("tovrin", "works_on", "n_alpha"),
    ("tovrin_kalia", "works_on", "n_alpha"),
    # NX Lab and Tamola → their OWN neighbours, NOT shared with Luke Gray.
    ("nx_lab", "works_on", "n_beta"),
    ("tamola", "works_on", "n_gamma"),
    # Luke Gray → partner org (distinct from NX Lab / Tamola neighbours).
    ("luke_gray", "works_on", "n_partner_org"),
]


class SpyStore(InMemoryStore):
    """InMemoryStore that counts every add_node / add_edge — the ADR 0008 zero-write
    spy, re-asserted across the WHOLE K-run."""

    def __init__(self) -> None:
        super().__init__()
        self.add_node_calls = 0
        self.add_edge_calls = 0

    def add_node(self, node):  # noqa: ANN001
        self.add_node_calls += 1
        super().add_node(node)

    def add_edge(self, edge):  # noqa: ANN001
        self.add_edge_calls += 1
        super().add_edge(edge)


def _build_store() -> SpyStore:
    """Embedded gold store with the REAL fastembed embedder + honest neighbour edges.
    Counters are zeroed after seeding so the spy measures only writes DURING apply."""
    embedder = embed_provider("default")
    store = SpyStore()
    for nid, (title, content) in _GOLD.items():
        node = Node(id=nid, type="Agent", title=title, content=content)
        vector = list(embedder.embed(embedding_text_for(node)))
        store.add_node(node.model_copy(update={"embedding": vector}))
    for nid, title in _NEIGHBOURS.items():
        store.add_node(Node(id=nid, type="Concept", title=title, content=""))
    for src, etype, dst in _EDGES:
        store.add_edge(Edge(type=etype, src=src, dst=dst))
    store.add_node_calls = 0
    store.add_edge_calls = 0
    return store


def _build_judge() -> LLMMergeJudge:
    """The real judge, wired exactly as production (server/_curation.py::_build_judge)
    but pointed at the endpoint via a directly-constructed ResolvedLLM. system_prompt=None
    so the judge uses _VERDICT_SYSTEM — validating M3's exemplars is the whole point;
    a custom prompt would bypass them. Thinking OFF (mandatory), temperature 0.2
    (shipping judge config)."""
    resolved = ResolvedLLM(
        provider="openai",
        api_base=_API_BASE,
        model=_MODEL,
        api_key_env=None,
        max_tokens=2000,
        temperature=0.2,
        top_p=0.95,
        top_k=20,
        min_p=0.0,
        presence_penalty=0.0,
        enable_thinking=False,
    )
    return LLMMergeJudge(
        llm_provider(resolved),
        temperature=resolved.temperature,
        max_tokens=resolved.max_tokens,
        top_p=resolved.top_p,
        top_k=resolved.top_k,
        min_p=resolved.min_p,
        presence_penalty=resolved.presence_penalty,
        enable_thinking=resolved.enable_thinking,
        system_prompt=None,
    )


def _backend_reachable() -> bool:
    if not _API_BASE:
        return False
    try:
        httpx.get(_API_BASE.rstrip("/") + "/models", timeout=10.0).raise_for_status()
        return True
    except Exception:  # noqa: BLE001 — any failure → unreachable → skip
        return False


def _member_sets(authority: AuthorityIndex) -> list[set[str]]:
    """Auto-merged member sets from the authority records. The NX-Lab/Tamola guard and
    the Tovrin merge check both ride on ``record.member_ids`` — NOT on
    ``ApplyReport.auto_merged`` (which is a list of cluster_id STRINGS, not memberships,
    so asserting on it would pass green while checking nothing)."""
    return [set(rec.member_ids) for rec in authority.records()]


@pytest.fixture(scope="module")
def store() -> SpyStore:
    return _build_store()


@pytest.fixture(scope="module")
def judge() -> LLMMergeJudge:
    """The warm, validated real judge — or a clean skip if the endpoint is offline/broken.

    Two-stage reachability (the box idle-unloads + cold-loads): a fast /v1/models
    probe, then a REAL warmup judge call on the Tovrin pair under a generous timeout to
    absorb cold-load. The warmup ALSO catches box-up-but-misrouted: LLMMergeJudge.judge
    swallows LLMProviderError → same=False, so a reachable-but-broken path (auth,
    thinking misconfig) would silently read EVERY pair as distinct and FALSELY fail the
    must-merge band. If the warmup does not return a confident 'same' on Tovrin, we
    skip (not fail) — the gate has nothing meaningful to measure."""
    if not _API_BASE:
        pytest.skip("OKTO_NEURON_JUDGE_BAND_API_BASE is required for acceptance_judge")
    if not _backend_reachable():
        pytest.skip(f"judge backend offline (no /v1/models at {_API_BASE})")

    # litellm may demand an api key for the openai adapter even on a local server;
    # a dummy value satisfies it without leaking anything (the local box ignores it).
    os.environ.setdefault("OPENAI_API_KEY", "sk-local-judge-band")

    j = _build_judge()
    # Cold-load warmup with a generous timeout — first call can take minutes.
    embedder = embed_provider("default")

    def _node(nid: str) -> Node:
        title, content = _GOLD[nid]
        node = Node(id=nid, type="Agent", title=title, content=content)
        vec = list(embedder.embed(embedding_text_for(node)))
        return node.model_copy(update={"embedding": vec})

    from okto_neuron.reconcile.propose import _node_to_candidate

    tovrin = _node("tovrin")
    tovrin_kalia = _node("tovrin_kalia")
    try:
        # httpx/litellm timeout is per-call; raise it generously for the cold load.
        os.environ.setdefault("OPENAI_TIMEOUT", str(int(_WARMUP_TIMEOUT)))
        verdict = j.judge(_node_to_candidate(tovrin), tovrin_kalia)
    except Exception as exc:  # noqa: BLE001 — unreachable/unloadable at warmup → skip
        pytest.skip(f"judge backend unloadable at warmup: {exc}")
    if not verdict.same or verdict.reason in {"llm-unavailable", "unparseable"}:
        pytest.skip(
            "judge backend reachable but warmup verdict is not a confident 'same' on "
            f"the Tovrin pair (same={verdict.same!r} reason={verdict.reason!r}); the "
            "path is misrouted/misconfigured — nothing meaningful to measure, skipping"
        )
    return j


def _clusters_for(store: SpyStore):
    return generate_candidate_clusters(store, embedder=embed_provider("default"))


def test_fixture_preconditions(store: SpyStore) -> None:
    """Fail LOUD at setup if the fixture itself is unsound, instead of letting a band
    quietly measure noise (ADR 0010 §Axis 5: assert embeddings non-null; the Tovrin
    cluster must be clean and self-canonical, else corroborated_ids is built against a
    foreign canonical and the Tovrin merge is silently stolen)."""
    nodes = {n.id: n for n in store.list_nodes(type="Agent")}
    assert len(nodes) == len(_GOLD)
    for nid, node in nodes.items():
        assert node.embedding, f"{nid} has no embedding — fastembed not live?"
        assert len(node.embedding) == 384, f"{nid} wrong dim {len(node.embedding)}"

    # Alex pair: measured below the recall floor (no embedding lane fires; the only
    # recall path is the P6 shared-neighbour lane, which does not exist yet).
    cos = _cosine(nodes["alex_h"].embedding, nodes["alex_f"].embedding)
    assert cos < RECONCILE_RECALL_FLOOR, (
        f"Alex H/F cosine {cos:.4f} >= floor {RECONCILE_RECALL_FLOOR}: fixture content "
        "drifted; the below-floor reality this gate assumes has shifted"
    )

    clusters = _clusters_for(store)
    members_of: dict[str, set[str]] = {}
    for c in clusters:
        for m in c.member_ids:
            members_of[m] = set(c.member_ids)

    # The Tovrin pair MUST co-cluster, and that cluster must NOT have swept in a foreign
    # higher-degree node that would become the canonical and steal the merge.
    assert "tovrin" in members_of and "tovrin_kalia" in members_of, (
        "Tovrin pair did not cluster — recall regressed"
    )
    tovrin_cluster = members_of["tovrin"]
    assert "tovrin_kalia" in tovrin_cluster, "Tovrin / Tovrin Kalia not co-clustered"
    foreign = tovrin_cluster - {"tovrin", "tovrin_kalia"}
    assert not foreign, (
        f"Tovrin cluster polluted by foreign members {foreign}: corroborated_ids would "
        "be built against a foreign canonical and the Tovrin merge would be stolen"
    )

    # NX Lab / Tamola must NOT share a neighbour with Luke Gray (honest fixture: they
    # cannot corroborate into the Luke Gray record even on a wrong 'same').
    def neighbours(nid: str) -> set[str]:
        out = {e.dst for e in store.list_edges(src=nid)}
        out |= {e.src for e in store.list_edges(dst=nid)}
        return out

    assert not (neighbours("nx_lab") & neighbours("luke_gray")), (
        "NX Lab shares a Luke Gray neighbour — dishonest fixture"
    )
    assert not (neighbours("tamola") & neighbours("luke_gray")), (
        "Tamola shares a Luke Gray neighbour — dishonest fixture"
    )


def _run_k(store: SpyStore, judge: LLMMergeJudge, *, use_cluster_judge: bool, tmp_path_factory):
    """Run apply_reconciliation K times with FRESH side-files per iteration. Returns
    (per-pair merge-rate Counter over K, list of all auto-merged member sets)."""
    tovrin_merges = 0
    nx_lab_auto = 0
    tamola_auto = 0
    aler_mualer_auto = 0
    luke_gray_merges = 0  # informational: the allowed Luke Gray / Luke Gray Lab merge
    alex_auto = 0
    all_sets: list[set[str]] = []

    for k in range(_K):
        # Fresh temp side-files EACH iteration — an upsert persists; shared side-files
        # would corrupt the rate denominator (every later run would see the earlier
        # merge already recorded).
        side = tmp_path_factory.mktemp(f"jb-{'cluster' if use_cluster_judge else 'pair'}-{k}")
        authority = AuthorityIndex(side / "authority")
        queue = ReconcileQueue(side / "reconcile", authority)

        report = apply_reconciliation(
            store,
            embedder=embed_provider("default"),
            judge=judge,
            authority=authority,
            queue=queue,
            type="Agent",
            use_cluster_judge=use_cluster_judge,
            judge_model=_MODEL,
        )
        assert report is not None
        sets = _member_sets(authority)
        all_sets.extend(sets)

        for s in sets:
            if {"tovrin", "tovrin_kalia"} <= s:
                tovrin_merges += 1
            if "nx_lab" in s:
                nx_lab_auto += 1
            if "tamola" in s:
                tamola_auto += 1
            if {"aler", "mualer"} <= s:
                aler_mualer_auto += 1
            if {"luke_gray", "luke_gray_lab"} <= s:
                luke_gray_merges += 1
            if "alex_h" in s or "alex_f" in s:
                alex_auto += 1

    rates = Counter(
        {
            "tovrin": tovrin_merges,
            "nx_lab_auto": nx_lab_auto,
            "tamola_auto": tamola_auto,
            "aler_mualer_auto": aler_mualer_auto,
            "luke_gray_merge": luke_gray_merges,
            "alex_auto": alex_auto,
        }
    )
    return rates, all_sets


def _report(path: str, rates: Counter, store: SpyStore) -> None:
    print(f"\n=== ADR 0010 Tier B band — judge path: {path} (K={_K}) ===")
    print(
        f"  Tovrin / Tovrin Kalia auto-merge:  {rates['tovrin']}/{_K}  (must-merge, floor {_MUST_MERGE_FLOOR})"
    )
    print(
        f"  Luke Gray / Luke Gray Lab merge:   {rates['luke_gray_merge']}/{_K}  (allowed; informational)"
    )
    print(f"  NX Lab in any auto-merge set:      {rates['nx_lab_auto']}/{_K}  (HARD GUARD == 0)")
    print(f"  Tamola in any auto-merge set:      {rates['tamola_auto']}/{_K}  (HARD GUARD == 0)")
    print(
        f"  Aler + Mualer auto-merged:         {rates['aler_mualer_auto']}/{_K}  (HARD GUARD == 0)"
    )
    print(
        f"  Alex H/F in any auto-merge set:  {rates['alex_auto']}/{_K}  (measure-first; open, Q1)"
    )
    print(
        f"  store add_node/add_edge during apply: {store.add_node_calls}/{store.add_edge_calls} (must be 0/0)"
    )


@pytest.mark.parametrize("use_cluster_judge", [False, True], ids=["pairwise", "cluster"])
def test_judge_band(
    use_cluster_judge: bool, store: SpyStore, judge: LLMMergeJudge, tmp_path_factory
):
    """The Tier B gate on ONE judge path. Both paths are exercised via parametrize."""
    path = "cluster" if use_cluster_judge else "pairwise"
    rates, _sets = _run_k(
        store, judge, use_cluster_judge=use_cluster_judge, tmp_path_factory=tmp_path_factory
    )
    _report(path, rates, store)

    # Off-graph invariant (ADR 0008): zero graph writes across the WHOLE K-run.
    assert store.add_node_calls == 0, (
        f"[{path}] apply wrote graph nodes — off-graph invariant broken"
    )
    assert store.add_edge_calls == 0, (
        f"[{path}] apply wrote graph edges — off-graph invariant broken"
    )

    # HARD precision guards — irreversible-merge invariant (== 0 across all K).
    assert rates["nx_lab_auto"] == 0, (
        f"[{path}] NX Lab auto-merged {rates['nx_lab_auto']}/{_K} times — a team/org-unit "
        "was folded into a person record (irreversible-precision invariant violated)"
    )
    assert rates["tamola_auto"] == 0, (
        f"[{path}] Tamola auto-merged {rates['tamola_auto']}/{_K} times — "
        "irreversible-precision invariant violated"
    )
    assert rates["aler_mualer_auto"] == 0, (
        f"[{path}] Aler + Mualer auto-merged {rates['aler_mualer_auto']}/{_K} times — "
        "two distinct people sharing a first name were fused"
    )

    # MUST-MERGE band — out-of-example M3 generalization + identity-grade corroboration.
    merge_rate = rates["tovrin"] / _K
    assert merge_rate >= _MUST_MERGE_FLOOR, (
        f"[{path}] Tovrin / Tovrin Kalia auto-merged {rates['tovrin']}/{_K} "
        f"(rate {merge_rate:.2f} < floor {_MUST_MERGE_FLOOR}) — the M3 identity rule "
        "is not generalizing on this path (a real judge regression, not K-sample flake)"
    )

    # MUST-DISTINCT band — Alex H/F is measure-first (NOT a hard fail). It should not
    # auto-merge (no recall lane reaches it today), and if it ever does it must stay
    # within the small distinct ceiling.
    alex_rate = rates["alex_auto"] / _K
    assert alex_rate <= _MUST_DISTINCT_CEILING, (
        f"[{path}] Alex H/F appeared in an auto-merge {rates['alex_auto']}/{_K} "
        f"(rate {alex_rate:.2f} > ceiling {_MUST_DISTINCT_CEILING}) — unexpected; the "
        "pair has no sound auto-merge path under the current design (ADR 0010 Q1)"
    )
