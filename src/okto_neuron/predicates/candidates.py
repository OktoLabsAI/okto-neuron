"""Predicate vocabulary scans and deterministic candidate generation."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from difflib import SequenceMatcher
from itertools import combinations
from typing import Any, Iterable, Protocol

from okto_neuron.core.schema import Edge, Node
from okto_neuron.store.protocol import GraphStore

from .index import PredicateAliasIndex


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class PredicateSample:
    claim_id: str
    title: str
    source_excerpt: str = ""


@dataclass(frozen=True)
class SharedArgumentPair:
    subject: str
    object: str
    same_order: int = 0
    swapped_order: int = 0


@dataclass(frozen=True)
class SharedArgumentEvidence:
    same_order: int = 0
    swapped_order: int = 0
    pairs: tuple[SharedArgumentPair, ...] = ()


@dataclass(frozen=True)
class ArgumentSignature:
    subject_type: str
    object_type: str
    count: int


@dataclass(frozen=True)
class PredicateCandidate:
    predicate_a: str
    predicate_b: str
    count_a: int
    count_b: int
    string_affinity: float
    shared_evidence: SharedArgumentEvidence
    samples_a: tuple[PredicateSample, ...] = ()
    samples_b: tuple[PredicateSample, ...] = ()
    signatures_a: tuple[ArgumentSignature, ...] = ()
    signatures_b: tuple[ArgumentSignature, ...] = ()

    @property
    def pair(self) -> tuple[str, str]:
        return (self.predicate_a, self.predicate_b)


@dataclass(frozen=True)
class PredicateStats:
    """Everything candidate generation needs from the graph, as plain counters.

    Built by one set-based scan (:func:`build_predicate_stats`) and maintained per vault by
    ``server/_projection.py``; :func:`generate_predicate_candidates` and
    :func:`shared_argument_evidence` accept it through ``stats=`` so a caller that holds a
    current projection never rescans the store.
    """

    vocabulary: Counter[str]
    argument_pairs: dict[str, Counter[tuple[str, str]]]
    signatures: dict[str, Counter[tuple[str, str]]]
    samples: dict[str, tuple[PredicateSample, ...]]

    def to_payload(self) -> dict[str, Any]:
        """JSON-safe form (sidecar persistence)."""

        def pairs(table: dict[str, Counter[tuple[str, str]]]) -> dict[str, list[list[Any]]]:
            return {
                predicate: [[a, b, count] for (a, b), count in sorted(counter.items())]
                for predicate, counter in sorted(table.items())
            }

        return {
            "vocabulary": dict(sorted(self.vocabulary.items())),
            "argument_pairs": pairs(self.argument_pairs),
            "signatures": pairs(self.signatures),
            "samples": {
                predicate: [[s.claim_id, s.title, s.source_excerpt] for s in items]
                for predicate, items in sorted(self.samples.items())
            },
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "PredicateStats":
        """Inverse of :meth:`to_payload`; raises ``ValueError``/``TypeError``/``KeyError`` on a
        malformed payload (the caller treats that as a corrupt sidecar)."""

        def pairs(raw: dict[str, list[list[Any]]]) -> dict[str, Counter[tuple[str, str]]]:
            return {
                str(predicate): Counter({(str(a), str(b)): int(n) for a, b, n in rows})
                for predicate, rows in raw.items()
            }

        return cls(
            vocabulary=Counter({str(k): int(v) for k, v in payload["vocabulary"].items()}),
            argument_pairs=pairs(payload["argument_pairs"]),
            signatures=pairs(payload["signatures"]),
            samples={
                str(predicate): tuple(PredicateSample(str(c), str(t), str(e)) for c, t, e in rows)
                for predicate, rows in payload["samples"].items()
            },
        )


_StoreSnapshot = PredicateStats  # pre-projection name, kept for importers


def collect_predicate_vocabulary(store: GraphStore) -> Counter[str]:
    """Count edge types and Claim ``P`` facets.

    Two store reads (one ``list_edges``, one ``list_nodes("Claim")``) regardless of
    graph size: the vocabulary needs no endpoint, signature or sample lookups.
    """
    edge_rows, claim_rows = _predicate_rows(store)
    return _vocabulary(edge_rows, claim_rows)


def shared_argument_evidence(
    store: GraphStore | None,
    predicate_a: str,
    predicate_b: str,
    *,
    stats: PredicateStats | None = None,
) -> SharedArgumentEvidence:
    """Count same-order and swapped-order shared S/O pairs for two predicates.

    With ``stats`` the answer comes from the maintained projection and ``store`` is unused.
    """
    if stats is not None:
        return _shared_argument_evidence_from_counts(
            predicate_a, predicate_b, stats.argument_pairs
        )
    assert store is not None, "shared_argument_evidence needs a store or stats"
    edge_rows, claim_rows = _predicate_rows(store)
    return _shared_argument_evidence_from_counts(
        predicate_a,
        predicate_b,
        _argument_pairs(edge_rows, claim_rows),
    )


def generate_predicate_candidates(
    store: GraphStore | None,
    embedder: EmbeddingProvider,
    *,
    stats: PredicateStats | None = None,
    alias_index: PredicateAliasIndex | None = None,
    judged_pairs: Iterable[tuple[str, str]] | None = None,
    cluster_threshold: float = 0.80,
    min_support: int = 2,
    max_pairs: int = 30,
) -> list[PredicateCandidate]:
    """Return ranked predicate-pair candidates for one bounded judge run.

    ``stats`` is the maintained per-vault projection; without it the store is scanned.
    """
    if stats is None:
        assert store is not None, "generate_predicate_candidates needs a store or stats"
    snapshot = stats if stats is not None else _scan_store(store)
    vocabulary = snapshot.vocabulary
    if len(vocabulary) < 2 or max_pairs <= 0:
        return []

    skipped = set(judged_pairs or ())
    if alias_index is not None:
        skipped |= alias_index.judged_pairs()
    skipped = {_pair_key(a, b) for a, b in skipped}

    embeddings = {pred: embedder.embed(pred) for pred in vocabulary}
    clusters = _greedy_clusters(vocabulary, embeddings, threshold=cluster_threshold)
    candidates: list[PredicateCandidate] = []

    for cluster in clusters:
        if len(cluster) < 2:
            continue
        for predicate_a, predicate_b in combinations(sorted(cluster), 2):
            if _pair_key(predicate_a, predicate_b) in skipped:
                continue
            count_a = vocabulary[predicate_a]
            count_b = vocabulary[predicate_b]
            if count_a < min_support and count_b < min_support:
                continue
            shared = _shared_argument_evidence_from_counts(
                predicate_a,
                predicate_b,
                snapshot.argument_pairs,
            )
            candidates.append(
                PredicateCandidate(
                    predicate_a=predicate_a,
                    predicate_b=predicate_b,
                    count_a=count_a,
                    count_b=count_b,
                    string_affinity=_string_affinity(predicate_a, predicate_b),
                    shared_evidence=shared,
                    samples_a=snapshot.samples.get(predicate_a, ()),
                    samples_b=snapshot.samples.get(predicate_b, ()),
                    signatures_a=_signatures(snapshot.signatures.get(predicate_a)),
                    signatures_b=_signatures(snapshot.signatures.get(predicate_b)),
                )
            )

    candidates.sort(
        key=lambda cand: (
            -cand.string_affinity,
            -(cand.shared_evidence.same_order + cand.shared_evidence.swapped_order),
            -max(cand.count_a, cand.count_b),
            -min(cand.count_a, cand.count_b),
            cand.predicate_a,
            cand.predicate_b,
        )
    )
    return candidates[:max_pairs]


_EdgeRow = tuple[str, Edge]
_ClaimRow = tuple[str, Node, dict]


def _predicate_rows(store: GraphStore) -> tuple[list[_EdgeRow], list[_ClaimRow]]:
    """One ``list_edges`` and one ``list_nodes("Claim")``, keeping only rows that
    carry a predicate, in store order."""
    edge_rows: list[_EdgeRow] = []
    for edge in store.list_edges():
        predicate = str(getattr(edge, "type", "") or "").strip()
        if predicate:
            edge_rows.append((predicate, edge))
    claim_rows: list[_ClaimRow] = []
    for claim in store.list_nodes(type="Claim"):
        facets = dict(getattr(claim, "facets", {}) or {})
        predicate = str(facets.get("P") or "").strip()
        if predicate:
            claim_rows.append((predicate, claim, facets))
    return edge_rows, claim_rows


def _vocabulary(edge_rows: list[_EdgeRow], claim_rows: list[_ClaimRow]) -> Counter[str]:
    vocabulary: Counter[str] = Counter()
    for predicate, _edge in edge_rows:
        vocabulary[predicate] += 1
    for predicate, _claim, _facets in claim_rows:
        vocabulary[predicate] += 1
    return vocabulary


def _argument_pairs(
    edge_rows: list[_EdgeRow], claim_rows: list[_ClaimRow]
) -> dict[str, Counter[tuple[str, str]]]:
    argument_pairs: dict[str, Counter[tuple[str, str]]] = {}
    for predicate, edge in edge_rows:
        argument_pairs.setdefault(predicate, Counter())[(str(edge.src), str(edge.dst))] += 1
    for predicate, _claim, facets in claim_rows:
        pair = _claim_argument_pair(facets)
        if pair is not None:
            argument_pairs.setdefault(predicate, Counter())[pair] += 1
    return argument_pairs


def build_predicate_stats(store: GraphStore) -> PredicateStats:
    """One set-based scan of ``store`` (no vectors are read) into :class:`PredicateStats`."""
    return _scan_store(store)


def _scan_store(store: GraphStore) -> PredicateStats:
    """Full snapshot for candidate generation, set-based: the two row scans plus
    ONE ``get_nodes`` over every endpoint, Claim argument and sample Block id."""
    edge_rows, claim_rows = _predicate_rows(store)

    sample_rows: list[_ClaimRow] = []
    sampled_per_predicate: Counter[str] = Counter()
    for row in claim_rows:
        if sampled_per_predicate[row[0]] < 3:
            sampled_per_predicate[row[0]] += 1
            sample_rows.append(row)

    wanted: list[str] = []
    for _predicate, edge in edge_rows:
        wanted.append(str(edge.src))
        wanted.append(str(edge.dst))
    for _predicate, _claim, facets in claim_rows:
        if _claim_argument_pair(facets) is None:
            continue
        if facets.get("S_id"):
            wanted.append(str(facets["S_id"]))
        if facets.get("O_id"):
            wanted.append(str(facets["O_id"]))
    for _predicate, _claim, facets in sample_rows:
        if not _claim_excerpt(facets) and facets.get("block_id"):
            wanted.append(str(facets["block_id"]))
    nodes = {node.id: node for node in store.get_nodes(wanted)} if wanted else {}

    signatures: dict[str, Counter[tuple[str, str]]] = {}
    for predicate, edge in edge_rows:
        src_type = _node_type(nodes.get(str(edge.src)))
        dst_type = _node_type(nodes.get(str(edge.dst)))
        signatures.setdefault(predicate, Counter())[(src_type, dst_type)] += 1
    for predicate, _claim, facets in claim_rows:
        if _claim_argument_pair(facets) is not None:
            signatures.setdefault(predicate, Counter())[_claim_signature(nodes, facets)] += 1

    samples: dict[str, list[PredicateSample]] = {}
    for predicate, claim, facets in sample_rows:
        samples.setdefault(predicate, []).append(_claim_sample(nodes, claim, facets))

    return PredicateStats(
        vocabulary=_vocabulary(edge_rows, claim_rows),
        argument_pairs=_argument_pairs(edge_rows, claim_rows),
        signatures=signatures,
        samples={pred: tuple(items) for pred, items in samples.items()},
    )


def _claim_argument_pair(facets: dict) -> tuple[str, str] | None:
    subject = facets.get("S_id") or facets.get("S") or facets.get("subject")
    obj = facets.get("O_id")
    if obj is None:
        obj = facets.get("O_literal")
    if obj is None:
        obj = facets.get("O") or facets.get("object")
    if subject is None or obj is None:
        return None
    return str(subject), str(obj)


def _claim_signature(nodes: dict[str, Node], facets: dict) -> tuple[str, str]:
    subject_id = facets.get("S_id")
    object_id = facets.get("O_id")
    subject_type = _node_type(nodes.get(str(subject_id))) if subject_id else "literal"
    if object_id:
        object_type = _node_type(nodes.get(str(object_id)))
    else:
        object_type = "literal"
    return subject_type, object_type


def _claim_excerpt(facets: dict) -> object:
    return facets.get("source_excerpt") or facets.get("excerpt") or facets.get("source_text") or ""


def _claim_sample(nodes: dict[str, Node], claim: Node, facets: dict) -> PredicateSample:
    excerpt = _claim_excerpt(facets)
    if not excerpt and facets.get("block_id"):
        block = nodes.get(str(facets["block_id"]))
        excerpt = getattr(block, "content", "") if block else ""
    return PredicateSample(
        claim_id=str(claim.id),
        title=str(claim.title or ""),
        source_excerpt=str(excerpt or "")[:500],
    )


def _node_type(node: Node | None) -> str:
    return str(getattr(node, "type", "") or "unknown")


def _greedy_clusters(
    vocabulary: Counter[str],
    embeddings: dict[str, list[float]],
    *,
    threshold: float,
) -> list[list[str]]:
    clusters: list[list[str]] = []
    for predicate, _count in sorted(vocabulary.items(), key=lambda item: (-item[1], item[0])):
        best_index = -1
        best_score = -1.0
        for index, cluster in enumerate(clusters):
            score = max(_cosine(embeddings[predicate], embeddings[member]) for member in cluster)
            if score > best_score:
                best_index = index
                best_score = score
        if best_index >= 0 and best_score >= threshold:
            clusters[best_index].append(predicate)
        else:
            clusters.append([predicate])
    return clusters


def _shared_argument_evidence_from_counts(
    predicate_a: str,
    predicate_b: str,
    argument_pairs: dict[str, Counter[tuple[str, str]]],
) -> SharedArgumentEvidence:
    left = argument_pairs.get(predicate_a, Counter())
    right = argument_pairs.get(predicate_b, Counter())
    rows: dict[tuple[str, str], list[int]] = {}

    for pair in set(left) & set(right):
        rows.setdefault(pair, [0, 0])[0] += min(left[pair], right[pair])

    for subject, obj in left:
        swapped = (obj, subject)
        if swapped not in right:
            continue
        rows.setdefault((subject, obj), [0, 0])[1] += min(left[(subject, obj)], right[swapped])

    shared_pairs = tuple(
        SharedArgumentPair(subject, obj, same, swapped)
        for (subject, obj), (same, swapped) in sorted(
            rows.items(),
            key=lambda item: (-(item[1][0] + item[1][1]), item[0][0], item[0][1]),
        )
    )
    return SharedArgumentEvidence(
        same_order=sum(pair.same_order for pair in shared_pairs),
        swapped_order=sum(pair.swapped_order for pair in shared_pairs),
        pairs=shared_pairs,
    )


def _signatures(counter: Counter[tuple[str, str]] | None) -> tuple[ArgumentSignature, ...]:
    if not counter:
        return ()
    return tuple(
        ArgumentSignature(subject, obj, count)
        for (subject, obj), count in sorted(
            counter.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1]),
        )
    )


def _cosine(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return numerator / (left_norm * right_norm)


def _string_affinity(left: str, right: str) -> float:
    return SequenceMatcher(None, left.replace("_", " "), right.replace("_", " ")).ratio()


def _pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


__all__ = [
    "ArgumentSignature",
    "EmbeddingProvider",
    "PredicateCandidate",
    "PredicateSample",
    "SharedArgumentEvidence",
    "SharedArgumentPair",
    "collect_predicate_vocabulary",
    "generate_predicate_candidates",
    "shared_argument_evidence",
]
