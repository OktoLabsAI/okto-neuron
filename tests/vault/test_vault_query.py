from __future__ import annotations

import hashlib
import sys
import types
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron._internal.completion_guard import CompletionForbiddenError
from okto_neuron.core.schema import Node
from okto_neuron.llm import Message, StubLLM
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture(autouse=True)
def close_vault_handles() -> None:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def test_ts_4ae238ef_query_empty_vault_returns_empty_list(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v", embedder="stub")

    try:
        assert vault.query("text", k=5) == []
    finally:
        vault.close()


def test_ts_4ae238ef_query_rejects_zero_k(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")

    try:
        with pytest.raises(AssertionError, match="k must be >= 1"):
            vault.query("text", k=0)
    finally:
        vault.close()


def test_ts_4ae238ef_query_returns_at_most_k_hits_sorted_by_score_desc(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v")
    _add_query_nodes(vault)

    try:
        hits = vault.query("alpha beta gamma", k=2)

        assert len(hits) <= 2
        assert [hit.node.id for hit in hits] == ["claim-exact", "claim-partial"]
        assert [hit.score for hit in hits] == sorted(
            (hit.score for hit in hits),
            reverse=True,
        )
    finally:
        vault.close()


def test_query_with_metrics_preserves_results_and_measures_completion_free_recall(
    tmp_path: Path,
) -> None:
    vault = Vault.init(tmp_path / "v", embedder="stub")
    _add_query_nodes(vault)

    try:
        expected = vault.query("alpha beta gamma", k=2)
        actual, metrics = vault.query_with_metrics("alpha beta gamma", k=2)

        assert [hit.node.id for hit in actual] == [hit.node.id for hit in expected]
        assert metrics["schema_version"] == "recall_cost.v1"
        assert metrics["measurement_status"] == "measured"
        assert metrics["completion_calls"] == 0
        assert metrics["generated_tokens"] == 0
        assert metrics["query_embedding_calls"] == 1
        assert metrics["query_embedding_provider"] == "stub"
        assert metrics["query_embedding_model"] == "sha256-deterministic"
        assert metrics["query_embedding_execution"] == "local"
        assert metrics["completion_free"] is True
        assert metrics["retrieved_results"] == len(actual)
        assert metrics["retrieved_bytes"] > 0
        assert metrics["query_embedding_latency_ms"] >= 0
        assert metrics["deterministic_retrieval_latency_ms"] >= 0
        assert metrics["deterministic_projection_latency_ms"] >= 0
        assert metrics["total_latency_ms"] >= 0
    finally:
        vault.close()


def test_query_with_metrics_rejects_an_llm_completion_inside_recall(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault.init(tmp_path / "v", embedder="stub")

    def _illegal_search(*_args: object, **_kwargs: object) -> list[tuple[object, float]]:
        StubLLM().complete([Message(role="user", content="illegal recall completion")])
        return []

    monkeypatch.setattr(vault, "_search", _illegal_search)
    try:
        with pytest.raises(CompletionForbiddenError, match="ordinary recall"):
            vault.query_with_metrics("test", k=1)
    finally:
        vault.close()


def test_document_provenance_covers_the_full_source_bytes(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v", embedder="stub")
    source = vault.path / "document.md"
    raw = b"The whole document is the cited support.\n"
    source.write_bytes(raw)
    document = Node(
        id="document-full-source",
        type="Document",
        title="Full source",
        content="",
        facets={
            "path": str(source),
            "byte_length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    )
    claim = Node(
        id="claim-with-unrelated-byte-length",
        type="Claim",
        title="Claim",
        content="",
        facets={
            "path": str(source),
            "byte_length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        },
    )

    try:
        document_provenance = vault._provenance_for_node(document)
        claim_provenance = vault._provenance_for_node(claim)

        assert document_provenance.path == str(source)
        assert document_provenance.byte_start == 0
        assert document_provenance.byte_end == len(raw)
        assert document_provenance.content_hash == f"sha256:{hashlib.sha256(raw).hexdigest()}"
        assert claim_provenance.byte_end == 0
    finally:
        vault.close()


def test_ts_76b2f40b_query_with_drift_suppresses_detector_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = Vault.init(tmp_path / "v")
    _add_query_nodes(vault)
    drift_module = types.ModuleType("marginalia.drift")

    def detect_drift(store: object, hits: object) -> object:
        raise RuntimeError("detector unavailable")

    drift_module.detect_drift = detect_drift
    monkeypatch.setitem(sys.modules, "marginalia.drift", drift_module)

    try:
        hits = vault.query("alpha beta gamma", k=1, with_drift=True)

        assert hits
        assert hits[0].drift is None
    finally:
        vault.close()


def _add_query_nodes(vault: Vault) -> None:
    vault.store.add_node(
        Node(
            id="claim-exact",
            type="Claim",
            title="Exact",
            content="alpha beta gamma alpha beta gamma",
            facets={"path": "exact.md", "byte_start": 0, "byte_end": 35},
        )
    )
    vault.store.add_node(
        Node(
            id="claim-partial",
            type="Claim",
            title="Partial",
            content="alpha beta",
            facets={"path": "partial.md", "byte_start": 0, "byte_end": 10},
        )
    )
    vault.store.add_node(
        Node(
            id="claim-third",
            type="Claim",
            title="Third",
            content="alpha",
            facets={"path": "third.md", "byte_start": 0, "byte_end": 5},
        )
    )
