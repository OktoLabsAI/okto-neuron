"""Fix A (+ task-13 E3) — endpoint-gate auto-promotion of queued endpoints.

A literal-fact Claim whose SUBJECT was merely *queued* by the node curator (a
folder/phase/file label that resolves to a weak Concept) used to be
dead-lettered by the relation endpoint gate, taking a valid quantity fact with
it. Fix A auto-promotes such a subject — LOW-SALIENCE, so it anchors the Claim
but never surfaces as an entity in recall/ask — instead of dead-lettering it.
Task-13 E3 extends the same mechanism to topology edges with exactly ONE dead
endpoint (src or dst).

Guards verified here:
  1. queued subject + literal claim  -> subject promoted (low-salience) + claim
     survives the gate;
  2. contradicted subject + literal claim -> still dead-lettered (no promote);
  3. dangling subject (never extracted) -> still dead-lettered (no promote);
  4. entity-object edge with a queued OBJECT -> object promoted (low-salience)
     + edge survives the gate (E3);
  5. edge with BOTH endpoints queued -> still dead-lettered (no promote).

Harness mirrors ``test_endpoint_pregate.py``: real ``Companion.remember()`` with
a fake extractor and a counting curator provider.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from collections.abc import Sequence as _Sequence
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron._internal.infra import is_low_salience
from okto_neuron.companion import Companion
from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.consolidate.ledger import LEDGER_FILENAME
from okto_neuron.core.schema import Node, Provenance
from okto_neuron.extract import ExtractionResult
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


class _QueueingCuratorProvider:
    """Queues node candidates whose prompt mentions a marker title; commits
    everything else. Commits every relationship-curator candidate and counts the
    calls so we can tell whether a claim reached the curator at all."""

    model = "endpoint-promote-test"
    api_base = "http://127.0.0.1:8123/v1"

    def __init__(self, *, queue_marker: str, relation_action: str = "commit") -> None:
        self._queue_marker = queue_marker
        self._relation_action = relation_action
        self.node_curator_calls = 0
        self.relation_curator_calls = 0
        self.relation_prompts: list[str] = []

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        system = getattr(messages[0], "content", "") if messages else ""
        user = getattr(messages[-1], "content", "")
        if "candidate curator" in system:
            self.node_curator_calls += 1
            if self._queue_marker in user:
                return '{"action":"queue","confidence":0.1,"reason":"test queue"}'
            return '{"action":"commit","confidence":0.95,"reason":"test commit"}'
        if "relationship curator" in system:
            self.relation_curator_calls += 1
            self.relation_prompts.append(user)
            predicate = next(
                (
                    line.partition(":")[2].strip()
                    for line in user.splitlines()
                    if line.startswith("Predicate/type:")
                ),
                "related_to",
            )
            return json.dumps(
                {
                    "action": self._relation_action,
                    "confidence": 0.95,
                    "canonical_predicate": predicate,
                    "predicate_definition": "The subject has the relation to the object.",
                    "predicate_direction": "subject_to_object",
                    "inverse_direction_required": False,
                    "subject_supported": True,
                    "predicate_supported": True,
                    "object_supported": True,
                    "direction_supported": True,
                    "unsupported_inference": False,
                    "structural_noise": False,
                    "redundant": False,
                    "useful": True,
                    "reason": "test relation decision",
                }
            )
        return '{"same":false,"confidence":0.99,"reason":"test distinct"}'


class _FakeExtractor:
    def __init__(
        self,
        nodes: list[NodeCandidate],
        edges: list[EdgeCandidate] | None = None,
    ) -> None:
        self._nodes = nodes
        self._edges = edges or []

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        prov = provenance or Provenance()
        return ExtractionResult(
            node_candidates=[n.model_copy(update={"provenance": prov}) for n in self._nodes],
            edge_candidates=[e.model_copy(update={"provenance": prov}) for e in self._edges],
        )


def _ledger_records(vault: Vault) -> list[dict]:
    path = Path(vault.path) / ".marginalia" / LEDGER_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _promote_rows(records: list[dict], subject_id: str) -> list[dict]:
    return [
        r
        for r in records
        if r["kind"] == "comparison"
        and r["candidate_id"] == subject_id
        and r["method"] == "endpoint_gate_promote"
    ]


def _claim_edge_id(records: list[dict]) -> str:
    """The candidate_id of the proposed literal-fact edge (dst_literal set)."""
    ids = {
        r["candidate_id"]
        for r in records
        if r["kind"] == "candidate"
        and r["candidate_kind"] == "edge"
        and r["payload"].get("dst_literal")
    }
    assert len(ids) == 1, ids
    return next(iter(ids))


def test_queued_subject_literal_claim_is_promoted_low_salience(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        # A folder/phase label the curator queues, and the dense quantity fact
        # whose only subject is that label.
        subject = NodeCandidate(
            type="Concept",
            title="Phase1FolderLabel",
            content="a folder/phase label, a weak structural Concept",
        )
        claim = EdgeCandidate(
            type="has_size",
            src_ref=subject.candidate_id,
            dst_literal="268 GB, 19,243 files",
        )
        provider = _QueueingCuratorProvider(queue_marker="Phase1FolderLabel")
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject], [claim]),
        )
        doc = Path(vault.path) / "note.md"
        doc.write_text("# Note\n\nsome body text.\n", encoding="utf-8")
        companion.remember(doc)

        records = _ledger_records(vault)

        # The subject was promoted: a dedicated ledger row records it, and the
        # claim DID reach the relation curator (promotion cleared the endpoint).
        promote = _promote_rows(records, subject.candidate_id)
        assert len(promote) == 1
        assert promote[0]["verdict"] == "promoted_subject"
        assert provider.relation_curator_calls == 1

        # The subject is now a live node, stamped LOW-SALIENCE so recall/ask
        # filter it (mechanism: is_low_salience / the _salience facet).
        node = vault.store.get_node(subject.candidate_id)
        assert node is not None
        assert is_low_salience(node)
        assert node.facets.get("_salience") == "low"

        # The claim was NOT dead-lettered by the endpoint gate.
        claim_id = _claim_edge_id(records)
        assert not [
            r
            for r in records
            if r["kind"] == "comparison"
            and r["candidate_id"] == claim_id
            and r["method"] == "endpoint_gate"
        ]
        # ...and it committed (a Claim was minted against the live subject).
        committed = [
            r
            for r in records
            if r["kind"] == "candidate"
            and r["candidate_kind"] == "edge"
            and r["candidate_id"] == claim_id
            and r["state"] == "committed"
        ]
        assert committed, "literal claim should have committed under Fix A"
        assert committed[-1]["payload"].get("claim_id")
    finally:
        vault.close()


def test_promoted_subject_not_orphaned_when_claim_is_queued(tmp_path: Path) -> None:
    """No-orphan guarantee: a subject is promoted at the endpoint gate, but if
    the relation curator QUEUES its claim (does not commit), the subject has no
    supporting relationship and the relationship-liveness gate queues it back —
    so promotion never mints an orphan node."""
    vault = Vault.init(tmp_path / "v")
    try:
        subject = NodeCandidate(
            type="Concept",
            title="Phase1FolderLabel",
            content="a folder/phase label, a weak structural Concept",
        )
        claim = EdgeCandidate(
            type="has_size",
            src_ref=subject.candidate_id,
            dst_literal="268 GB, 19,243 files",
        )
        provider = _QueueingCuratorProvider(
            queue_marker="Phase1FolderLabel", relation_action="queue"
        )
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject], [claim]),
        )
        doc = Path(vault.path) / "note.md"
        doc.write_text("# Note\n\nsome body text.\n", encoding="utf-8")
        result = companion.remember(doc)

        records = _ledger_records(vault)
        # Promotion DID fire (endpoint cleared, claim reached the curator)...
        assert len(_promote_rows(records, subject.candidate_id)) == 1
        assert provider.relation_curator_calls == 1
        # ...but the queued claim left the subject unsupported, so it was queued
        # back, not committed — no orphan node in the store.
        assert vault.store.get_node(subject.candidate_id) is None
        assert result.committed == 0
        assert result.claims_minted == 0
    finally:
        vault.close()


def test_contradicted_subject_literal_claim_still_dead_lettered(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        # Pre-seed a Claim the new subject will contradict (same S+P, diff O).
        vault.store.add_node(
            Node(
                id="existing-claim",
                type="Claim",
                title="x p A",
                facets={"subject": "x", "predicate": "p", "object": "A"},
            )
        )
        # The queued subject is itself a Claim asserting x p B — contradicted,
        # which the gate must treat as an ACTIVE rejection (never promote).
        subject = NodeCandidate(
            type="Claim",
            title="ContradictedSubject",
            content="x p B",
            facets={"subject": "x", "predicate": "p", "object": "B"},
        )
        claim = EdgeCandidate(
            type="has_size",
            src_ref=subject.candidate_id,
            dst_literal="999 GB",
        )
        provider = _QueueingCuratorProvider(queue_marker="ContradictedSubject")
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([subject], [claim]),
        )
        doc = Path(vault.path) / "note.md"
        doc.write_text("# Note\n\nsome body text.\n", encoding="utf-8")
        companion.remember(doc)

        records = _ledger_records(vault)

        # No promotion, no live subject node.
        assert _promote_rows(records, subject.candidate_id) == []
        assert vault.store.get_node(subject.candidate_id) is None

        # The claim was dead-lettered at the endpoint gate, never curated.
        claim_id = _claim_edge_id(records)
        gate_rows = [
            r
            for r in records
            if r["kind"] == "comparison"
            and r["candidate_id"] == claim_id
            and r["method"] == "endpoint_gate"
        ]
        assert [(r["method"], r["verdict"]) for r in gate_rows] == [
            ("endpoint_gate", "skipped_endpoint")
        ]
        assert provider.relation_curator_calls == 0
    finally:
        vault.close()


def test_dangling_subject_literal_claim_still_dead_lettered(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        # The claim's subject ref points at a node that was never extracted and
        # is not in the store — Fix A must not conjure it.
        claim = EdgeCandidate(
            type="has_size",
            src_ref="ref-to-nothing",
            dst_literal="42 GB",
        )
        provider = _QueueingCuratorProvider(queue_marker="__never__")
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([], [claim]),
        )
        doc = Path(vault.path) / "note.md"
        doc.write_text("# Note\n\nsome body text.\n", encoding="utf-8")
        companion.remember(doc)

        records = _ledger_records(vault)
        assert _promote_rows(records, "ref-to-nothing") == []
        assert vault.store.get_node("ref-to-nothing") is None

        claim_id = _claim_edge_id(records)
        gate_rows = [
            r
            for r in records
            if r["kind"] == "comparison"
            and r["candidate_id"] == claim_id
            and r["method"] == "endpoint_gate"
        ]
        assert [(r["method"], r["verdict"]) for r in gate_rows] == [
            ("endpoint_gate", "skipped_endpoint")
        ]
        assert provider.relation_curator_calls == 0
    finally:
        vault.close()


def _edge_ids_of_type(records: list[dict], edge_type: str) -> set[str]:
    return {
        r["candidate_id"]
        for r in records
        if r["kind"] == "candidate"
        and r["candidate_kind"] == "edge"
        and r["payload"].get("type") == edge_type
    }


def test_entity_object_edge_with_queued_object_is_promoted(tmp_path: Path) -> None:
    """Task-13 E3: a topology edge whose only dead endpoint is a QUEUED object
    candidate gets that object promoted (low-salience) instead of the edge
    being dead-lettered — the exact reference-eval failure family where e.g.
    ``Eng --created--> group-email`` died because the group-email node was
    queued by the node curator."""
    vault = Vault.init(tmp_path / "v")
    try:
        src = NodeCandidate(type="Agent", title="Alex", content="a builder")
        obj = NodeCandidate(type="Concept", title="QueuedObject", content="a queued concept")
        edge = EdgeCandidate(type="works_on", src_ref=src.candidate_id, dst_ref=obj.candidate_id)
        provider = _QueueingCuratorProvider(queue_marker="QueuedObject")
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([src, obj], [edge]),
        )
        doc = Path(vault.path) / "note.md"
        doc.write_text("# Note\n\nsome body text.\n", encoding="utf-8")
        companion.remember(doc)

        records = _ledger_records(vault)

        # The object was promoted: dedicated ledger row, and the edge DID
        # reach the relation curator (promotion cleared the endpoint).
        promote = _promote_rows(records, obj.candidate_id)
        assert len(promote) == 1
        assert promote[0]["verdict"] == "promoted_object"
        assert provider.relation_curator_calls == 1

        # The object is now a live node, stamped LOW-SALIENCE.
        node = vault.store.get_node(obj.candidate_id)
        assert node is not None
        assert is_low_salience(node)

        # The edge was NOT dead-lettered at the endpoint gate and committed.
        edge_ids = _edge_ids_of_type(records, "works_on")
        assert len(edge_ids) == 1
        edge_id = next(iter(edge_ids))
        assert not [
            r
            for r in records
            if r["kind"] == "comparison"
            and r["candidate_id"] == edge_id
            and r["method"] == "endpoint_gate"
        ]
        committed = [
            r
            for r in records
            if r["kind"] == "candidate"
            and r["candidate_kind"] == "edge"
            and r["candidate_id"] == edge_id
            and r["state"] == "committed"
        ]
        assert committed, "topology edge should have committed under Fix A/E3"
    finally:
        vault.close()


def test_edge_with_both_endpoints_queued_still_dead_lettered(tmp_path: Path) -> None:
    """E3 stays scoped to exactly ONE dead endpoint: an edge between two
    queued structural nodes must still dead-letter with no promotion."""
    vault = Vault.init(tmp_path / "v")
    try:
        src = NodeCandidate(type="Concept", title="QueuedSubjectLabel", content="a queued label")
        obj = NodeCandidate(type="Concept", title="QueuedObjectLabel", content="a queued label")
        edge = EdgeCandidate(type="relates_to", src_ref=src.candidate_id, dst_ref=obj.candidate_id)
        provider = _QueueingCuratorProvider(queue_marker="Label")
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([src, obj], [edge]),
        )
        doc = Path(vault.path) / "note.md"
        doc.write_text("# Note\n\nsome body text.\n", encoding="utf-8")
        companion.remember(doc)

        records = _ledger_records(vault)
        assert not [
            r
            for r in records
            if r["kind"] == "comparison" and r["method"] == "endpoint_gate_promote"
        ]
        assert vault.store.get_node(src.candidate_id) is None
        assert vault.store.get_node(obj.candidate_id) is None
        edge_ids = _edge_ids_of_type(records, "relates_to")
        assert len(edge_ids) == 1
        edge_id = next(iter(edge_ids))
        gate_rows = [
            r
            for r in records
            if r["kind"] == "comparison"
            and r["candidate_id"] == edge_id
            and r["method"] == "endpoint_gate"
        ]
        assert [(r["method"], r["verdict"]) for r in gate_rows] == [
            ("endpoint_gate", "skipped_endpoint")
        ]
        assert provider.relation_curator_calls == 0
    finally:
        vault.close()


def test_promoted_object_not_orphaned_when_edge_is_queued(tmp_path: Path) -> None:
    """No-orphan guarantee holds for the E3 extension too: a promoted object
    whose edge the relation curator queues is queued back by the
    relationship-liveness gate."""
    vault = Vault.init(tmp_path / "v")
    try:
        src = NodeCandidate(type="Agent", title="Alex", content="a builder")
        obj = NodeCandidate(type="Concept", title="QueuedObject", content="a queued concept")
        edge = EdgeCandidate(type="works_on", src_ref=src.candidate_id, dst_ref=obj.candidate_id)
        provider = _QueueingCuratorProvider(queue_marker="QueuedObject", relation_action="queue")
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([src, obj], [edge]),
        )
        doc = Path(vault.path) / "note.md"
        doc.write_text("# Note\n\nsome body text.\n", encoding="utf-8")
        companion.remember(doc)

        records = _ledger_records(vault)
        assert len(_promote_rows(records, obj.candidate_id)) == 1
        assert provider.relation_curator_calls == 1
        # Edge queued -> object unsupported -> queued back, no orphan node.
        assert vault.store.get_node(obj.candidate_id) is None
    finally:
        vault.close()
