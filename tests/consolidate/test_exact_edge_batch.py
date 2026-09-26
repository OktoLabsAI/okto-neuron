"""Tests for ADR 0016 Tier E0 exact relation collapse."""

from __future__ import annotations

from collections.abc import Iterator
from collections.abc import Sequence as _Sequence
import json
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.companion import Companion
from okto_neuron.config import VaultConfig
from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.consolidate.ledger import LEDGER_FILENAME
from okto_neuron.core.schema import Provenance
from okto_neuron.extract import ExtractionResult
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


class _CommitProvider:
    model = "exact-edge-batch-test"
    api_base = "http://127.0.0.1:8123/v1"

    def complete(self, messages: _Sequence, **kwargs: object) -> str:
        system = getattr(messages[0], "content", "") if messages else ""
        user = getattr(messages[-1], "content", "") if messages else ""
        if "candidate curator" in system:
            return '{"action":"commit","confidence":0.95,"reason":"test commit"}'
        if "relationship curator" in system:
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
                    "reason": "test relation commit",
                }
            )
        return '{"same":false,"confidence":0.99,"reason":"test distinct"}'


class _RepeatingExtractor:
    def __init__(self, nodes: list[NodeCandidate], edges: list[EdgeCandidate]) -> None:
        self._nodes = nodes
        self._edges = edges

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        prov = provenance or Provenance()
        return ExtractionResult(
            node_candidates=[node.model_copy(update={"provenance": prov}) for node in self._nodes],
            edge_candidates=[edge.model_copy(update={"provenance": prov}) for edge in self._edges],
        )


class _FixedEmbedder:
    dim = 384

    def embed(self, text: str) -> list[float]:  # noqa: ARG002
        return [0.1] * self.dim


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def _doc(vault: Vault) -> Path:
    # Fixed at the pre-2026-09-12 shipped chunk_size_bytes (12_000, not
    # DEFAULT_CHUNK_SIZE_BYTES/IngestConfig's current default) so this
    # fixture keeps producing exactly the 2 blocks this test asserts on,
    # independent of whatever the shipped ingest default is today.
    VaultConfig.apply_patch(
        vault.path, {"ingest": {"chunk_size_bytes": 12_000, "chunk_overlap_bytes": 0}}
    )
    path = Path(vault.path) / "note.md"
    path.write_text("Alice relates to Bob.\n" * 800, encoding="utf-8")
    return path


def _ledger_records(vault: Vault) -> list[dict]:
    path = Path(vault.path) / ".marginalia" / LEDGER_FILENAME
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_e0_exact_edge_batch_collapse_records_merge_and_corroborates(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        alice = NodeCandidate(type="Agent", title="Alice", content="subject")
        bob = NodeCandidate(type="Agent", title="Bob", content="object")
        edge = EdgeCandidate(
            type="related-to",
            src_ref=alice.candidate_id,
            dst_ref=bob.candidate_id,
        )
        companion = Companion(
            vault,
            provider=_CommitProvider(),
            extractor=_RepeatingExtractor([alice, bob], [edge]),
            embedder=_FixedEmbedder(),
        )

        result = companion.remember(_doc(vault))

        assert result.claims_minted == 1
        claims = list(vault.store.list_nodes(type="Claim"))
        assert len(claims) == 1
        assert claims[0].facets["corroborations"] == 2

        records = _ledger_records(vault)
        exact_batch = [
            record
            for record in records
            if record["kind"] == "comparison"
            and record["method"] == "exact_edge_batch"
            and record["verdict"] == "merge"
        ]
        assert len(exact_batch) == 1
        completed = [
            record
            for record in records
            if record["kind"] == "ingest_run" and record["state"] == "completed"
        ][-1]
        assert completed["summary"]["relations_corroborated"] == 1
    finally:
        vault.close()
