"""Candidate-generation lanes — recall for retroactive reconciliation.

ADR 0008. Where ``resolve.find_similar`` recalls duplicates of a NEW candidate
against the committed store at the 0.82 'similar' threshold, this module recalls
ALREADY-COMMITTED look-alikes within the live graph at a WIDENED floor. The 0.82
floor demonstrably missed a known alias pair (cosine 0.7177), so reconciliation
recall drops the floor to ``RECONCILE_RECALL_FLOOR`` and adds two string lanes
that need no embedding at all.

Every lane only PROPOSES pairs. None auto-merges on a string match — adjudication
is the conservative LLM judge in ``propose.py``. Jaro-Winkler is used purely as an
ORDERING / precision signal (Nora/Liam score low → never paired), NEVER as a
substring test (which would wrongly pair any shared prefix).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from okto_neuron._internal.infra import is_infra
from okto_neuron.semantic_surface import discovery_surface_key, exact_surface_key

if TYPE_CHECKING:
    from okto_neuron.embed import EmbeddingProvider
    from okto_neuron.store.protocol import GraphStore

# ── tunables ──────────────────────────────────────────────────────────────────
RECONCILE_RECALL_FLOOR = 0.65
"""Widened cosine floor for retroactive recall. Below ``SIMILAR_THRESHOLD`` (0.82)
which missed a known alias pair at 0.7177; calibrated from that data point. The
recall lane is deliberately permissive — precision is enforced downstream by the
conservative judge, not here."""

ENTITY_TYPES: frozenset[str] = frozenset(
    {"Agent", "Activity", "InformationObject", "Concept", "Place"}
)
"""The 5 closed primitives — the ONLY reconciliation targets. Support types
(Claim, Block, Document, Identifier, Annotation, Finding, Authority) are NEVER
reconciled: Claims are atomic provenance units with distinct source anchors, so
deduping them would collapse legitimately-separate assertions. ``type=None`` scans
these 5 only; an explicit support ``type`` yields no clusters."""

JW_STRONG = 0.90
"""Jaro-Winkler floor for the lexical lane. An ORDERING/precision score: 'Nora'
vs 'Liam' scores well below this (distinct first names), so they are never
paired by the string lane."""

JW_FUZZY_MIN_LEN = 7
"""Minimum (shorter-title) length for a NON-EXACT fuzzy lexical match to form a
cluster edge (ADR 0010 Axis 1 de-pollution). Jaro-Winkler's 0.1 prefix bonus
over-rewards a shared prefix on short tokens, so a fuzzy match under ~7 chars
cannot distinguish a true variant from a coincidental near-spelling:
``jaro_winkler("riley","ridley")=0.956`` would otherwise weld the unrelated
``Ridley`` (shorter title ``riley`` = 5 chars < 7) into the ``Riley`` cluster.
Gates ONLY the fuzzy ``jw ≥ JW_STRONG`` branch — the exact shared-surname
path and the token-subset paths are unaffected, so ``Riley``/``Riley Chen``
still pair via the shared exact token ``riley``."""

IDENTITY_EDGE_SCORE = 1.0
"""Pair score for a DETERMINISTIC token-containment edge (token-subset, order-
insensitive token-subset, email-handle decomposition). Set ABOVE the embedding
band (cosine ≤ 1.0, recall floor 0.65) so the ``_split_oversize`` cap, which packs
strongest-score edges FIRST, keeps an identity-linked variant pair together and
sheds weaker embedding-pollutant edges instead. Without this, a containment edge
scored by Jaro-Winkler collapses to ~0 for word-order-different variants
(``jaro_winkler("Lee Casey","Dr Casey Lee")≈0``) — the kind of pair the lane exists to
recover — so the cap split severs the bridge and drops them into separate clusters
on a graph where the 0.65 embedding floor has fused most entities into one
oversize component (measured: 80 of 95 Agents in a validation vault). A token-identity
match is stronger evidence than a 0.65 cosine; the score reflects that. This only
reorders cap packing — it creates NO new edges, so it cannot manufacture a merge
the lanes did not already propose; the judge + corroboration still decide."""

RECONCILE_CLASS_CAP = 8
"""Maximum cluster size before a deterministic split (descending pair-score), so
a transitive blob never explodes the judge budget."""

_WORD = re.compile(r"[a-z0-9]+")
_HANDLE_RE = re.compile(r"^[a-z0-9]+(?:[._][a-z0-9]+)+$")


# ── string similarity ──────────────────────────────────────────────────────────
def _jaro(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    match_distance = max(len(a), len(b)) // 2 - 1
    if match_distance < 0:
        match_distance = 0
    a_matches = [False] * len(a)
    b_matches = [False] * len(b)
    matches = 0
    for i, ca in enumerate(a):
        lo = max(0, i - match_distance)
        hi = min(i + match_distance + 1, len(b))
        for j in range(lo, hi):
            if b_matches[j] or b[j] != ca:
                continue
            a_matches[i] = True
            b_matches[j] = True
            matches += 1
            break
    if matches == 0:
        return 0.0
    transpositions = 0
    k = 0
    for i in range(len(a)):
        if not a_matches[i]:
            continue
        while not b_matches[k]:
            k += 1
        if a[i] != b[k]:
            transpositions += 1
        k += 1
    transpositions //= 2
    return (matches / len(a) + matches / len(b) + (matches - transpositions) / matches) / 3.0


def jaro_winkler(a: str, b: str) -> float:
    """Jaro-Winkler similarity in [0, 1]. ORDERING/precision signal only — never a
    substring test. Case-insensitive; uses the standard 0.1 prefix scaling over a
    common prefix of up to 4 chars."""
    a = discovery_surface_key(a)
    b = discovery_surface_key(b)
    jaro = _jaro(a, b)
    prefix = 0
    for ca, cb in zip(a, b):
        if ca != cb or prefix >= 4:
            break
        prefix += 1
    return jaro + prefix * 0.1 * (1.0 - jaro)


def email_handle_tokens(s: str) -> str | None:
    """Decompose an email-handle-shaped title into space-separated tokens, e.g.
    ``"casey.lee"`` → ``"casey lee"``. Returns ``None`` when the string
    is not a dotted/underscored handle (so a normal title is left alone). This
    lane recalls thin handle entities that have ZERO edges."""
    # Preserve separators until this specialized lane recognizes the handle;
    # its returned token string is then a discovery key.
    s = exact_surface_key(s)
    # Drop an email domain if present, keep the local part.
    if "@" in s:
        s = s.split("@", 1)[0]
    if not _HANDLE_RE.match(s):
        return None
    tokens = [t for t in re.split(r"[._]", s) if t]
    if len(tokens) < 2:
        return None
    return " ".join(tokens)


def _surname(title: str) -> str | None:
    """The trailing WORD token of a multi-token title, or ``None``.

    A surname is a word. A trailing token made only of digits is an identifier
    or a date fragment — ``TASK-01``, ``Sprint 01``, ``ECD 2026``,
    ``DARF Unificado jul/2026`` — and reading one as a surname welds every
    numbered or dated item in a corpus into a single blocking family: measured
    on a live 386-node graph, ``DARF Unificado jul/2026`` drew 15 candidates
    tied at ``IDENTITY_EDGE_SCORE``, nearly all of them pure numeric-token
    collisions with unrelated documents that merely shared the year ``2026`` or
    the sequence number ``01``. A caller that ranks and caps its matches (see
    ``resolve.LEXICAL_ALIAS_CAP``) then breaks that tie on node id, so the TRUE
    pair is crowded out by coincidences and never reaches the judge at all.

    This is a PRECISION fix to a blocking SIGNAL, not a blocking-time veto —
    ADR 0042 is explicit that blocking optimizes recall and precision belongs to
    the judge, so nothing here may reject a pair some lane would propose. Nothing
    does: a genuine short-name/full-name or identifier-variant pair still rides
    ``token_subset`` / ``token_subset_unordered`` on its shared WORD tokens
    (``TASK-01`` ⊂ ``TASK-01: Add is_recurrent Property to Expense Model``,
    ``darf-unificado`` ⊂ ``DARF Unificado jul/2026``). The only pairs this drops
    are ones no other lane would ever have proposed — their Jaro-Winkler scores
    measure 0.39 to 0.68, far below ``JW_STRONG``. Do not "restore" the digit case
    by mistaking this for a recall veto that should be reverted.
    """
    toks = _WORD.findall(discovery_surface_key(title))
    if len(toks) < 2:
        return None
    last = toks[-1]
    return last if any(ch.isalpha() for ch in last) else None


def _token_set(title: str) -> frozenset[str]:
    return frozenset(_WORD.findall(discovery_surface_key(title)))


# Generic org/role/structural words that, even when shared, carry no identity
# signal — sharing only one of these is NOT distinctive enough to co-cluster.
# (Words < 4 chars — ``nx``, ``lab``, ``of``, ``the``, ``inc`` — are already
# excluded by the ≥4-char distinctiveness floor; this set earns its keep for the
# ≥4-char generics like ``team``/``group``/``dept``.)
_GENERIC_TOKENS: frozenset[str] = frozenset(
    {"team", "group", "inc", "llc", "ltd", "dept", "the", "of", "and"}
)


def _is_distinctive(token: str) -> bool:
    """A token carries identity signal when it is long enough (≥4 chars) to be
    more than a structural fragment AND is not a known generic org/role word."""
    return len(token) >= 4 and token not in _GENERIC_TOKENS


def token_subset(a: str, b: str) -> bool:
    """True when one title's token set is a PROPER subset of the other's and they
    share the FIRST token — the first-name/full-name containment chain
    (``{casey} ⊂ {casey, lee} ⊂ {casey, morgan, lee, reed}``).

    The shared-first-token guard is what keeps this off the precision traps it
    would otherwise hit: ``{nora} ⊄ {liam, reed}`` (neither subsets the other
    AND first tokens differ), and a bare ``{nx} ⊂ {nx, legal}`` still PROPOSES
    only — the conservative judge + degree-based canonical reject the org variants.
    NEVER a substring test; operates on whole tokens, so a shared prefix alone
    (``nx`` vs ``nxx``) never qualifies."""
    sa, sb = _token_set(a), _token_set(b)
    if not sa or not sb or sa == sb:
        return False
    shorter, longer = (sa, sb) if len(sa) < len(sb) else (sb, sa)
    if not shorter < longer:  # proper subset only
        return False
    a_toks = _WORD.findall(discovery_surface_key(a))
    b_toks = _WORD.findall(discovery_surface_key(b))
    if not a_toks or not b_toks:
        return False
    return a_toks[0] == b_toks[0]


def token_subset_unordered(a: str, b: str) -> bool:
    """True when one title's token SET is a PROPER subset of the other's AND they
    share at least one DISTINCTIVE token (≥4 chars, not a generic org/role word).

    Order-insensitive sibling of ``token_subset`` (ADR 0010 Axis 1): it DROPS the
    shared-first-token requirement, so ``{casey, lee} ⊂ {dr, casey, lee}`` qualifies
    (shared distinctive ``casey``) → ``Casey Lee``/``Dr Casey Lee`` co-cluster even though
    their first tokens differ and ``jaro_winkler`` is ~0 (word-order). Additive —
    ``token_subset`` is untouched and still owns the ordered name chain.

    The distinctive-token requirement is the precision guard: ``{nora}`` is not a
    token-element of ``{liam, reed}``, so no proper subset exists and the names never
    pair. ``NX Lab`` vs ``Morgan Lee Lab`` share only ``lab`` (3 chars / generic),
    so this lane adds no pair. Whole-
    token only, never a substring test."""
    sa, sb = _token_set(a), _token_set(b)
    if not sa or not sb or sa == sb:
        return False
    shorter, longer = (sa, sb) if len(sa) < len(sb) else (sb, sa)
    if not shorter < longer:  # proper subset only
        return False
    return any(_is_distinctive(tok) for tok in (sa & sb))


# ── cluster value object ────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CandidateCluster:
    """A connected component of recalled pairs, scoped to one node type."""

    cluster_id: str
    type: str
    member_ids: tuple[str, ...]
    lane_evidence: dict[str, list[str]] = field(default_factory=dict)


def cluster_id_for(member_ids: tuple[str, ...]) -> str:
    """Deterministic id = sha256 of the sorted member ids (stable across runs)."""
    h = hashlib.sha256()
    h.update("\x1f".join(sorted(member_ids)).encode("utf-8"))
    return h.hexdigest()[:16]


# ── cosine (mirrors resolve._cosine; vectors are node.embedding) ──────────────--
def _cosine(a, b) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = sum(a[i] * a[i] for i in range(n)) ** 0.5
    nb = sum(b[i] * b[i] for i in range(n)) ** 0.5
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


# ── connected components ────────────────────────────────────────────────────────
def connected_components(
    pairs: list[tuple[str, str]],
) -> list[set[str]]:
    """Union-find over the recalled pairs → list of connected member-id sets.
    Singletons (no pair) are intentionally NOT returned: a node with no recalled
    look-alike is not a reconciliation candidate."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for a, b in pairs:
        union(a, b)

    groups: dict[str, set[str]] = {}
    for node in parent:
        groups.setdefault(find(node), set()).add(node)
    return [g for g in groups.values() if len(g) >= 2]


def _split_oversize(
    members: set[str],
    pair_score: dict[frozenset[str], float],
    cap: int,
) -> list[tuple[str, ...]]:
    """Deterministically bound a cluster to ``cap`` members. A cluster at/under
    the cap passes through whole. Otherwise it is greedily packed by descending
    intra-cluster pair score so the highest-confidence pairs stay together; the
    pack order (and tie-break by sorted id) is deterministic."""
    if len(members) <= cap:
        return [tuple(sorted(members))]
    member_list = sorted(members)
    # Edges within this cluster, strongest first; ties broken by sorted endpoints.
    internal: list[tuple[float, str, str]] = []
    for i in range(len(member_list)):
        for j in range(i + 1, len(member_list)):
            key = frozenset((member_list[i], member_list[j]))
            score = pair_score.get(key, 0.0)
            internal.append((score, member_list[i], member_list[j]))
    internal.sort(key=lambda t: (-t[0], t[1], t[2]))

    buckets: list[set[str]] = []

    def bucket_of(node: str) -> int | None:
        for idx, b in enumerate(buckets):
            if node in b:
                return idx
        return None

    for _score, a, b in internal:
        ba, bb = bucket_of(a), bucket_of(b)
        if ba is None and bb is None:
            buckets.append({a, b})
        elif ba is not None and bb is None:
            if len(buckets[ba]) < cap:
                buckets[ba].add(b)
            else:
                buckets.append({b})
        elif ba is None and bb is not None:
            if len(buckets[bb]) < cap:
                buckets[bb].add(a)
            else:
                buckets.append({a})
        # both already bucketed → leave as-is (merging buckets could exceed cap)
    # MEMBER CONSERVATION: every input member must survive into some returned
    # bucket — a cap split bounds size, it must never silently DROP a member. Two
    # leftover cases are reattached rather than discarded:
    #   1. a node never placed (e.g. its only edges hit full buckets), and
    #   2. a node alone in its own bucket (the prior code's len>=2 filter dropped it).
    placed = {n for b in buckets for n in b}
    for node in member_list:
        if node not in placed:
            buckets.append({node})

    def _best_host(node: str) -> int | None:
        # Strongest under-cap bucket by max pair score to any member there; ties →
        # the bucket with the lexicographically smallest member (deterministic).
        best_idx: int | None = None
        best_key: tuple[float, str] | None = None
        for idx, b in enumerate(buckets):
            if node in b or len(b) >= cap:
                continue
            top = max((pair_score.get(frozenset((node, m)), 0.0) for m in b), default=0.0)
            key = (top, _neg_id(min(b)))
            if best_key is None or key > best_key:
                best_key, best_idx = key, idx
        return best_idx

    # Reattach singletons to a host so no member is lost. Process small buckets
    # first so a chain of singletons coalesces deterministically.
    changed = True
    while changed:
        changed = False
        for idx in sorted(range(len(buckets)), key=lambda i: (len(buckets[i]), sorted(buckets[i]))):
            if idx >= len(buckets) or len(buckets[idx]) != 1:
                continue
            (lone,) = tuple(buckets[idx])
            host = _best_host(lone)
            if host is not None:
                buckets[host].add(lone)
                buckets.pop(idx)
                changed = True
                break
    return [tuple(sorted(b)) for b in buckets if b]


def _neg_id(node_id: str) -> str:
    """Invert lexicographic order so the smaller id sorts highest under ``max``."""
    return "".join(chr(0x10FFFF - ord(c)) if ord(c) < 0x10FFFF else c for c in node_id)


# ── recall lanes → clusters ─────────────────────────────────────────────────────
def generate_candidate_clusters(
    store: "GraphStore",
    *,
    embedder: "EmbeddingProvider | None" = None,
    type: str | None = None,
    recall_floor: float = RECONCILE_RECALL_FLOOR,
    nickname_gazetteer: dict[str, set[str]] | None = None,
    class_cap: int = RECONCILE_CLASS_CAP,
) -> list[CandidateCluster]:
    """Union the recall lanes into pairs, connect into per-type clusters, cap.

    Lanes (each PROPOSES; none decides):
      1. widened embedding band (cosine ≥ ``recall_floor``)
      2. lexical surname/prefix (Jaro-Winkler ≥ ``JW_STRONG`` length-gated, OR
         exact shared surname)
      3. token-subset (first-name/full-name containment, shared first token)
      4. order-insensitive token-subset (proper subset + shared distinctive token)
      5. email-handle decomposition (``casey.lee`` ↔ ``Casey Lee``)
      6. optional nickname gazetteer (off unless provided)
    """
    # Collect candidate entity nodes, scoped to one type at a time so a cross-type
    # accident (Agent vs Concept sharing a word) never pairs.
    nodes_by_type: dict[str, list] = {}
    for node in store.list_nodes(type=type, include_embedding=True):
        if is_infra(node):
            continue
        # Reconciliation targets the 5 entity primitives ONLY. Support types
        # (esp. Claim — an atomic provenance unit) must never be deduped here.
        if node.type not in ENTITY_TYPES:
            continue
        nodes_by_type.setdefault(node.type, []).append(node)

    clusters: list[CandidateCluster] = []
    for ntype, nodes in nodes_by_type.items():
        pairs: list[tuple[str, str]] = []
        lane_for_pair: dict[frozenset[str], set[str]] = {}
        pair_score: dict[frozenset[str], float] = {}

        def record(a: str, b: str, lane: str, score: float = 0.0) -> None:
            if a == b:
                return
            key = frozenset((a, b))
            pairs.append((a, b))
            lane_for_pair.setdefault(key, set()).add(lane)
            pair_score[key] = max(pair_score.get(key, 0.0), score)

        # Lane 1 — widened embedding band.
        embedded = [n for n in nodes if n.embedding]
        for i in range(len(embedded)):
            for j in range(i + 1, len(embedded)):
                score = _cosine(embedded[i].embedding, embedded[j].embedding)
                if score >= recall_floor:
                    record(embedded[i].id, embedded[j].id, "embedding", score)

        # Lanes 2-4 — string lanes (need no embedding; recall thin entities).
        handles = {n.id: email_handle_tokens(n.title) for n in nodes}
        surnames = {n.id: _surname(n.title) for n in nodes}
        gaz = nickname_gazetteer or {}
        for i in range(len(nodes)):
            for j in range(i + 1, len(nodes)):
                a, b = nodes[i], nodes[j]
                jw = jaro_winkler(a.title, b.title)
                # Length-gate the FUZZY (NON-equal) lexical edge: a short-token
                # near-match (e.g. riley/rarely, jw>0.90) must NOT weld a
                # cluster edge. The gate folds into the `if` so a gated pair FALLS
                # THROUGH to the elifs — Riley/Riley Chen still pairs via the
                # exact shared token `riley` on the surname/subset path. Exact
                # title equality is NOT fuzzy (an identical title is not a near-
                # spelling), so two identical short titles are never gated out.
                a_norm = discovery_surface_key(a.title)
                b_norm = discovery_surface_key(b.title)
                shorter_len = min(len(a_norm), len(b_norm))
                if jw >= JW_STRONG and (a_norm == b_norm or shorter_len >= JW_FUZZY_MIN_LEN):
                    record(a.id, b.id, "lexical", jw)
                elif surnames[a.id] and surnames[a.id] == surnames[b.id]:
                    record(a.id, b.id, "lexical", jw)
                elif token_subset(a.title, b.title):
                    # First-name/full-name containment chain. Identity-grade score
                    # (above the embedding band) so the cap's score-ordered packer
                    # keeps the variant chain together ahead of weaker embedding
                    # pollutant edges — JW alone collapses to ~0 for word-order
                    # variants, the exact case this lane must keep intact.
                    record(a.id, b.id, "subset", IDENTITY_EDGE_SCORE)
                elif token_subset_unordered(a.title, b.title):
                    # Order-insensitive containment sharing a distinctive token
                    # (ADR 0010 Axis 1): recovers Casey Lee/Dr Casey Lee, which the
                    # ordered subset misses (different first tokens, jw~0). Scored
                    # identity-grade so the cap split does not sever the bridge.
                    record(a.id, b.id, "subset_unordered", IDENTITY_EDGE_SCORE)
                # Email-handle decomposition (either direction).
                a_tok, b_tok = handles[a.id], handles[b.id]
                a_title = discovery_surface_key(a.title)
                b_title = discovery_surface_key(b.title)
                if a_tok and a_tok == b_title:
                    record(a.id, b.id, "handle", IDENTITY_EDGE_SCORE)
                elif b_tok and b_tok == a_title:
                    record(a.id, b.id, "handle", IDENTITY_EDGE_SCORE)
                elif a_tok and b_tok and a_tok == b_tok:
                    record(a.id, b.id, "handle", IDENTITY_EDGE_SCORE)
                # Nickname gazetteer (optional).
                if gaz:
                    a_first = a_title.split()[0] if a_title else ""
                    b_first = b_title.split()[0] if b_title else ""
                    if (
                        a_first
                        and b_first
                        and (
                            b_first in gaz.get(a_first, set()) or a_first in gaz.get(b_first, set())
                        )
                    ):
                        record(a.id, b.id, "nickname", jw)

        components = connected_components(pairs)
        title_by_id = {n.id: n.title for n in nodes}
        for component in components:
            for bounded in _split_oversize(component, pair_score, class_cap):
                cid = cluster_id_for(bounded)
                evidence: dict[str, list[str]] = {}
                for x in range(len(bounded)):
                    for y in range(x + 1, len(bounded)):
                        key = frozenset((bounded[x], bounded[y]))
                        for lane in sorted(lane_for_pair.get(key, ())):
                            evidence.setdefault(lane, []).append(
                                f"{title_by_id.get(bounded[x], bounded[x])} <-> "
                                f"{title_by_id.get(bounded[y], bounded[y])}"
                            )
                clusters.append(
                    CandidateCluster(
                        cluster_id=cid,
                        type=ntype,
                        member_ids=bounded,
                        lane_evidence=evidence,
                    )
                )
    clusters.sort(key=lambda c: c.cluster_id)
    return clusters


__all__ = [
    "CandidateCluster",
    "generate_candidate_clusters",
    "jaro_winkler",
    "token_subset",
    "token_subset_unordered",
    "email_handle_tokens",
    "connected_components",
    "cluster_id_for",
    "RECONCILE_RECALL_FLOOR",
    "JW_STRONG",
    "RECONCILE_CLASS_CAP",
]
