"""Endpoint gate BEFORE relation curation.

A relation candidate whose endpoint is dead and NOT promotable under
Fix A/E3 (a dangling ref that was never extracted, a contradicted candidate,
or an edge with both endpoints dead) can only ever be dead-lettered, so it
must never reach the relation curator LLM. The pre-curation gate records the
SAME ``endpoint_gate`` / ``skipped_endpoint`` comparison the post-curation
safety-net gate uses, so ledger semantics and totals are unchanged — only the
wasted LLM call is gone. (Queued extracted endpoints are promoted instead —
see ``test_endpoint_promote.py``.)

Harness pattern from ``test_prefilter.py`` / ``test_resume.py``: real
``Companion.remember()`` with a fake extractor and a counting curator provider.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from collections.abc import Sequence as _Sequence
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.companion import Companion
from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.consolidate.ledger import LEDGER_FILENAME
from okto_neuron.core.schema import Provenance
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
    everything else. Records every relation-curator prompt it sees."""

    model = "endpoint-pregate-test"
    api_base = "http://127.0.0.1:8123/v1"

    def __init__(self, *, queue_marker: str) -> None:
        self._queue_marker = queue_marker
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
                    "action": "commit",
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
                    "reason": "test commit",
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


def test_queued_endpoint_relation_never_reaches_relation_curator(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        src = NodeCandidate(
            type="Agent", title="Alex", content="a chat participant who builds things"
        )
        dst = NodeCandidate(
            type="Concept", title="Quarantine", content="a concept the curator rejects"
        )
        # Dangling object ref: never extracted, not in the store — dead and
        # NOT promotable under Fix A/E3, so the pre-gate must skip the LLM.
        gated = EdgeCandidate(type="works_on", src_ref=src.candidate_id, dst_ref="ref-to-nothing")
        kept = EdgeCandidate(type="defines", src_ref=src.candidate_id, dst_literal="a person")
        provider = _QueueingCuratorProvider(queue_marker="Quarantine")
        companion = Companion(
            vault,
            provider=provider,
            extractor=_FakeExtractor([src, dst], [gated, kept]),
        )
        doc = Path(vault.path) / "note.md"
        doc.write_text("# Note\n\nsome body text.\n", encoding="utf-8")
        companion.remember(doc)

        # Both nodes were judged; only the relation with LIVE endpoints reached
        # the relation curator — the gated one cost zero LLM calls.
        assert provider.node_curator_calls == 2
        assert provider.relation_curator_calls == 1
        assert all("works_on" not in prompt for prompt in provider.relation_prompts)

        records = _ledger_records(vault)
        gated_ids = {
            r["candidate_id"]
            for r in records
            if r["kind"] == "candidate"
            and r["candidate_kind"] == "edge"
            and r["payload"].get("type") == "works_on"
        }
        assert len(gated_ids) == 1
        gated_id = next(iter(gated_ids))
        # Same ledger semantics as the post-curation safety-net gate.
        gate_rows = [
            r for r in records if r["kind"] == "comparison" and r["candidate_id"] == gated_id
        ]
        assert ("endpoint_gate", "skipped_endpoint") in [
            (row["method"], row["verdict"]) for row in gate_rows
        ]
        endpoint_row = next(row for row in gate_rows if row["method"] == "endpoint_gate")
        assert "endpoint not accepted" in endpoint_row["reason"]
        # And it never produced a relation_curator comparison row.
        assert not [
            r
            for r in records
            if r["kind"] == "comparison"
            and r["method"] == "relation_curator"
            and r["candidate_id"] == gated_id
        ]
    finally:
        vault.close()
