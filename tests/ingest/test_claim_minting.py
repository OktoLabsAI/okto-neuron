"""Track A: byte-anchored Claim minting from LLM relationships.

Drives the real ``Companion.remember`` path (per-block extraction → resolve →
gate → commit → mint) with a deterministic fake extractor, the stub embedder,
and :class:`StubLLM`. NO network.

A committed relationship must mint exactly one ``Node(type="Claim")`` that:
  * carries ``block_id`` / ``confidence`` / ``model_id`` / ``prompt_hash``;
  * has its three PROV edges (wasDerivedFrom→Block, wasGeneratedBy→Activity,
    wasAttributedTo→Agent);
  * anchors to a Block whose byte-range hashes back to the source bytes.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
from okto_neuron.core.schema import Provenance
from okto_neuron.extract import _SYSTEM, ExtractionResult
from okto_neuron.llm import StubLLM
from okto_neuron.semantic_quality import evaluate_rebuild_gate, evaluate_store
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


class _RelExtractor:
    """Emits one ``subj -> obj`` relationship per paragraph in the text it is
    handed (blank-line separated), both novel Concepts, so each paragraph yields
    exactly one committed relationship (=> one Claim). With fixed-window chunking
    a whole small doc arrives as ONE window, so this proves minting tracks
    committed relationships, not block count. Records the texts it saw."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        prov = provenance or Provenance()
        self.seen.append(text)
        nodes: list[NodeCandidate] = []
        edges: list[EdgeCandidate] = []
        for para in (p for p in text.split("\n\n") if p.strip()):
            key = para.strip()[:24]
            subj = NodeCandidate(type="Concept", title=f"subj {key}", content=para, provenance=prov)
            obj = NodeCandidate(type="Concept", title=f"obj {key}", content=para, provenance=prov)
            nodes += [subj, obj]
            edges.append(
                EdgeCandidate(
                    type="relates_to",
                    src_ref=subj.candidate_id,
                    dst_ref=obj.candidate_id,
                    provenance=prov,
                )
            )
        return ExtractionResult(node_candidates=nodes, edge_candidates=edges)


class _FixedRelExtractor:
    def __init__(self) -> None:
        self.subject = NodeCandidate(
            type="Concept",
            title="The Shire",
            content="A place in Middle-earth.",
        )
        self.object = NodeCandidate(
            type="Concept",
            title="Middle-earth",
            content="The setting of the story.",
        )

    def extract(self, text: str, *, provenance: Provenance | None = None) -> ExtractionResult:
        prov = provenance or Provenance()
        return ExtractionResult(
            node_candidates=[
                self.subject.model_copy(update={"provenance": prov}),
                self.object.model_copy(update={"provenance": prov}),
            ],
            edge_candidates=[
                EdgeCandidate(
                    type="part_of",
                    src_ref=self.subject.candidate_id,
                    dst_ref=self.object.candidate_id,
                    provenance=prov,
                )
            ],
        )


_DOC = "para one about alpha and beta.\n\npara two about gamma and delta.\n"


def _write_doc(vault: Vault) -> Path:
    p = Path(vault.path) / "note.md"
    p.write_text(_DOC, encoding="utf-8")
    return p


def _companion(vault: Vault, extractor: _RelExtractor):
    from okto_neuron.companion import Companion

    return Companion(vault, provider=StubLLM(), extractor=extractor)


def test_each_committed_relationship_mints_a_claim(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        path = _write_doc(vault)
        companion = _companion(vault, _RelExtractor())
        companion.remember(path)

        claims = [n for n in vault.store.list_nodes(type="Claim", include_embedding=True)]
        # One ~12k window holds both paragraphs; two relationships => two Claims
        # (minting tracks committed relationships, not block count).
        assert len(claims) == 2

        prompt_hash = hashlib.sha256(_SYSTEM.encode("utf-8")).hexdigest()
        for claim in claims:
            facets = claim.facets
            assert facets["block_id"]
            assert facets["model_id"] == "stub"
            assert facets["prompt_hash"] == prompt_hash
            # Subject is a novel Concept (0.7 + 0.2 novelty bonus).
            assert facets["confidence"] == pytest.approx(0.9)
            # Relationship Claim: O_id set, O_literal None (XOR arm).
            assert facets["O_id"]
            assert facets["O_literal"] is None
            # The Claim node also carries an embedding for the vector leg.
            assert claim.embedding is not None and len(claim.embedding) > 0
    finally:
        vault.close()


def test_claim_has_three_prov_edges(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        path = _write_doc(vault)
        companion = _companion(vault, _RelExtractor())
        companion.remember(path)

        claims = list(vault.store.list_nodes(type="Claim"))
        assert claims
        for claim in claims:
            edges = list(vault.store.list_edges(src=claim.id))
            by_type = {e.type: e.dst for e in edges}
            # PROV-O triple (F3) plus the ADR 0005 bridge edges: these are
            # entity-object relationship Claims, so both rdf:subject and rdf:object
            # are minted.
            assert set(by_type) == {
                "prov:wasDerivedFrom",
                "prov:wasGeneratedBy",
                "prov:wasAttributedTo",
                "rdf:subject",
                "rdf:object",
            }
            # wasDerivedFrom must point at the Claim's anchoring Block.
            assert by_type["prov:wasDerivedFrom"] == claim.facets["block_id"]
            # wasGeneratedBy/wasAttributedTo target the LLM Activity/Agent nodes.
            activity = vault.store.get_node(by_type["prov:wasGeneratedBy"])
            agent = vault.store.get_node(by_type["prov:wasAttributedTo"])
            assert activity is not None and activity.type == "Activity"
            assert agent is not None and agent.type == "Agent"
            # ADR 0005 F1/F2: the bridge edges point at the subject/object entities
            # recorded in the Claim's facets.
            assert by_type["rdf:subject"] == claim.facets["S_id"]
            assert by_type["rdf:object"] == claim.facets["O_id"]
    finally:
        vault.close()


def test_semantic_quality_matches_full_remember_relation_materialization(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        path = _write_doc(vault)
        companion = _companion(vault, _RelExtractor())
        companion.remember(path)
        generation = vault.store._graph_handle.graph_generation

        report = evaluate_store(
            vault.store,
            integrity={
                "status": "verified",
                "graph_generation": generation,
                "fresh_for_semantic_scan": True,
                "last_audit": {
                    "status": "verified",
                    "graph_generation": generation,
                    "nodes_complete": True,
                    "edges_complete": True,
                    "adjacency_complete": True,
                    "manifest_complete": True,
                },
            },
            registered_predicates={"relates_to"},
        )
        relation = report["layers"]["relation"]
        assert relation["relation_claims"] == 2
        assert relation["materialized_topology_edges"] == 2
        assert relation["missing_topology_edges"]["count"] == 0
        assert relation["topology_edges_without_claims"]["count"] == 0
        assert relation["unanchored_claims"]["count"] == 0
        assert report["hard_invariants"]["measured_pass"] is True
        assert report["verdict"]["status"] == "incomplete"
        assert report["rebuild_gate"]["status"] == "passed"
        assert report["rebuild_gate"]["swap_allowed"] is True
    finally:
        vault.close()


def test_semantic_rebuild_gate_fails_closed_on_missing_or_unmeasured_check() -> None:
    report = {
        "hard_invariants": {
            "checks": [
                {
                    "code": "complete_store_scan",
                    "status": "not_measured",
                    "count": None,
                    "reason": "scan was incomplete",
                    "samples": [],
                }
            ]
        }
    }

    gate = evaluate_rebuild_gate(report)

    assert gate["status"] == "failed"
    assert gate["swap_allowed"] is False
    assert "complete_store_scan" in gate["failed_codes"]
    assert "registered_predicates" in gate["failed_codes"]


def test_claim_block_byte_range_hashes_to_source(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        path = _write_doc(vault)
        companion = _companion(vault, _RelExtractor())
        companion.remember(path)

        raw = path.read_bytes()
        claims = list(vault.store.list_nodes(type="Claim"))
        assert claims
        for claim in claims:
            block = vault.store.get_node(claim.facets["block_id"])
            assert block is not None and block.type == "Block"
            bs = int(block.facets["byte_start"])
            be = int(block.facets["byte_end"])
            content_hash = str(block.facets["content_hash"])
            # The anchoring contract: the Block's content_hash is sha256 of the
            # exact source byte-slice it pins (no normalization).
            assert hashlib.sha256(raw[bs:be]).hexdigest() == content_hash
            assert len(content_hash) == 64
    finally:
        vault.close()


def test_same_semantic_claim_from_another_block_adds_corroboration(tmp_path: Path) -> None:
    vault = Vault.init(tmp_path / "v")
    try:
        first = Path(vault.path) / "first.md"
        second = Path(vault.path) / "second.md"
        first.write_text("The Shire is part of Middle-earth.\n", encoding="utf-8")
        second.write_text("Again: the Shire is part of Middle-earth.\n", encoding="utf-8")
        companion = _companion(vault, _FixedRelExtractor())

        companion.remember(first)
        companion.remember(second)

        claims = list(vault.store.list_nodes(type="Claim"))
        semantic_claims = [claim for claim in claims if (claim.facets or {}).get("P") == "part_of"]
        assert len(semantic_claims) == 1
        claim = semantic_claims[0]
        derived_blocks = {
            edge.dst
            for edge in vault.store.list_edges(
                src=claim.id,
                type="prov:wasDerivedFrom",
            )
        }
        assert len(derived_blocks) == 2
        assert claim.facets["corroborations"] == 2
    finally:
        vault.close()


def test_missing_claim_resumes_without_provider_or_embedder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import okto_neuron.companion as companion_module

    vault = Vault.init(tmp_path / "v")
    try:
        path = _write_doc(vault)
        companion = _companion(vault, _RelExtractor())
        with monkeypatch.context() as crash:
            crash.setattr(
                companion_module,
                "_apply_sealed_semantic_plan",
                lambda *args, **kwargs: (_ for _ in ()).throw(
                    RuntimeError("crash after plan seal")
                ),
            )
            with pytest.raises(RuntimeError, match="crash after plan seal"):
                companion.remember(path)

        class _ForbiddenProvider:
            @property
            def model(self) -> str:
                raise AssertionError("sealed replay must not resolve the provider")

        class _ForbiddenExtractor:
            def extract(self, *args: object, **kwargs: object) -> ExtractionResult:
                raise AssertionError("sealed replay must not run extraction")

        result = companion_module.Companion(
            vault,
            provider=_ForbiddenProvider(),  # type: ignore[arg-type]
            extractor=_ForbiddenExtractor(),  # type: ignore[arg-type]
            embedder=object(),  # type: ignore[arg-type]
        ).remember(path)

        assert result.claims_minted == 2
        assert len(list(vault.store.list_nodes(type="Claim"))) == 2
    finally:
        vault.close()
