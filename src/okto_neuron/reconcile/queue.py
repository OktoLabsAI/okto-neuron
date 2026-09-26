"""ReconcileQueue — parked clusters awaiting curation (ZERO graph writes).

ADR 0008. Like ``consolidate.review_queue.ReviewQueue``, this queue is an
off-graph evidence boundary. ``ReconcileQueue.confirm`` appends the cluster's
:class:`~okto_neuron.reconcile.authority.AuthorityRecord` to the off-graph
:class:`~okto_neuron.reconcile.authority.AuthorityIndex` and dequeues. There is no
``store`` reference here at all, so a graph write is impossible by construction.

Persisted as JSON at ``<vault>/.marginalia/reconcile/queue.json``, keyed by
``cluster_id``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from okto_neuron.reconcile.authority import AuthorityIndex, AuthorityRecord
from okto_neuron.reconcile.candidates import CandidateCluster
from okto_neuron.reconcile.propose import ClusterVerdict

RECONCILE_DIRNAME = "reconcile"
QUEUE_FILENAME = "queue.json"


@dataclass(frozen=True)
class QueuedCluster:
    cluster: CandidateCluster
    verdict: ClusterVerdict
    correlations: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "cluster": {
                "cluster_id": self.cluster.cluster_id,
                "type": self.cluster.type,
                "member_ids": list(self.cluster.member_ids),
                "lane_evidence": dict(self.cluster.lane_evidence),
            },
            "verdict": {
                "cluster_id": self.verdict.cluster_id,
                "same": self.verdict.same,
                "confidence": self.verdict.confidence,
                "canonical_id": self.verdict.canonical_id,
                "member_ids": list(self.verdict.member_ids),
                "corroboration": self.verdict.corroboration,
                "reason": self.verdict.reason,
                "corroborated_ids": list(self.verdict.corroborated_ids),
            },
            "correlations": dict(self.correlations),
        }

    @classmethod
    def from_json(cls, data: dict) -> "QueuedCluster":
        c = data["cluster"]
        v = data["verdict"]
        return cls(
            cluster=CandidateCluster(
                cluster_id=str(c["cluster_id"]),
                type=str(c["type"]),
                member_ids=tuple(str(m) for m in c.get("member_ids", ())),
                lane_evidence=dict(c.get("lane_evidence", {})),
            ),
            verdict=ClusterVerdict(
                cluster_id=str(v["cluster_id"]),
                same=bool(v["same"]),
                confidence=float(v.get("confidence", 0.0)),
                canonical_id=str(v.get("canonical_id", "")),
                member_ids=tuple(str(m) for m in v.get("member_ids", ())),
                corroboration=str(v.get("corroboration", "none")),
                reason=str(v.get("reason", "")),
                corroborated_ids=tuple(str(m) for m in v.get("corroborated_ids", ())),
            ),
            correlations=dict(data.get("correlations", {})),
        )


def _record_from_queued(
    qc: QueuedCluster, *, judge_model: str = "", agent: str = "marginalia-reconcile"
) -> AuthorityRecord:
    """Build the off-graph AuthorityRecord for a confirmed cluster. Canonical is
    the verdict's canonical_id; variants/exact_match_pairs derive from the
    confirmed member set. Titles are carried in correlations where available."""
    v = qc.verdict
    titles: dict[str, str] = dict(qc.correlations.get("titles", {}))
    canonical_id = v.canonical_id or (v.member_ids[0] if v.member_ids else "")
    member_ids = tuple(v.member_ids)
    variant_ids = [m for m in member_ids if m != canonical_id]
    variants = tuple(titles.get(m, m) for m in variant_ids)
    pairs = tuple((canonical_id, m) for m in variant_ids)
    return AuthorityRecord(
        cluster_id=v.cluster_id,
        canonical_id=canonical_id,
        canonical_name=titles.get(canonical_id, canonical_id),
        member_ids=member_ids,
        variants=variants,
        exact_match_pairs=pairs,
        verdict="same",
        confidence=v.confidence,
        provenance={
            "agent": agent,
            "activity": "entity-reconciliation",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "lanes": sorted(qc.cluster.lane_evidence.keys()),
            "judge_model": judge_model,
            "source": "review-confirm",
        },
    )


@dataclass
class ReconcileQueue:
    """Persistent queue of parked clusters, keyed by ``cluster_id``. NO ``store``
    reference — a graph write is impossible by construction."""

    dir: Path
    authority: AuthorityIndex
    _entries: dict[str, QueuedCluster] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.dir = Path(self.dir)
        self._load()

    @property
    def path(self) -> Path:
        return self.dir / QUEUE_FILENAME

    def _load(self) -> None:
        self._entries = {}
        if not self.path.exists():
            return
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for record in data.get("entries", []):
            qc = QueuedCluster.from_json(record)
            self._entries[qc.cluster.cluster_id] = qc

    def _save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = {"entries": [qc.to_json() for qc in self._entries.values()]}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ── surface ──────────────────────────────────────────────────────────────--
    def enqueue(
        self,
        cluster: CandidateCluster,
        verdict: ClusterVerdict,
        correlations: dict | None = None,
    ) -> None:
        self._entries[cluster.cluster_id] = QueuedCluster(
            cluster=cluster, verdict=verdict, correlations=dict(correlations or {})
        )
        self._save()

    def list(self) -> list[QueuedCluster]:
        return list(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, cluster_id: str) -> QueuedCluster | None:
        return self._entries.get(cluster_id)

    def confirm(self, cluster_id: str, *, judge_model: str = "") -> AuthorityRecord:
        """Confirm a parked cluster → append its AuthorityRecord OFF-GRAPH and
        dequeue. Writes only ``index.json`` (via authority) and ``queue.json``."""
        qc = self._entries.get(cluster_id)
        if qc is None:
            raise KeyError(f"no queued cluster with id {cluster_id!r}")
        rec = _record_from_queued(qc, judge_model=judge_model)
        self.authority.upsert(rec)
        del self._entries[cluster_id]
        self._save()
        return rec

    def reject(self, cluster_id: str) -> None:
        if cluster_id in self._entries:
            del self._entries[cluster_id]
            self._save()


__all__ = [
    "ReconcileQueue",
    "QueuedCluster",
    "RECONCILE_DIRNAME",
    "QUEUE_FILENAME",
]
