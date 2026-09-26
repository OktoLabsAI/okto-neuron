"""Regression for review 3.3/companion-ingest "cross-call-guard": inside one
``Companion.remember()`` batch, Tier 0 (``reconcile_against_store``) and Tier
1/2 (``judge_against_store``) each used to build their OWN ``EdgeEndpointGuard``
scoped to a single call. A candidate edge-connected to a batch sibling already
folded by Tier 0 into an existing store node could then still be independently
merged by Tier 1/2 into that SAME store node -- fusing the relationship's two
ends into a self-loop that gets silently dropped, and destroying the second
candidate as a distinct entity (see ``resolve.judge_against_store``'s
docstring: "It does NOT reach across the call boundary from a PRIOR
reconcile_against_store pass").

Model-free: a fixed constant embedder and a stub judge that always answers
"same" stand in for a real LLM, so the whole dedup pipeline is deterministic
and offline.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from okto_neuron import Vault
from okto_neuron.companion import Companion
from okto_neuron.consolidate._candidates import EdgeCandidate, NodeCandidate
from okto_neuron.core.schema import Node, Provenance
from okto_neuron.extract import ExtractionResult
from okto_neuron.llm import StubLLM
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection

_EMBED_DIM = 384


@pytest.fixture(autouse=True)
def close_vault_handles() -> Iterator[None]:
    yield
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


class _FixedEmbedder:
    """Deterministic, model-free embedder: every text maps to the SAME vector,
    so every same-type pair clears the Tier 1 similarity band regardless of
    title -- exactly the worst case for the cross-tier guard."""

    dim = _EMBED_DIM

    def embed(self, text: str) -> list[float]:  # noqa: ARG002
        return [0.1] * _EMBED_DIM


class _AlwaysSameMergeJudgeLLM(StubLLM):
    """StubLLM variant whose merge-verdict answers are always 'same' with high
    confidence. Every other schema-gated call (extraction curator, relation
    curator, ...) keeps StubLLM's default behavior -- only the merge-judge
    schema is overridden."""

    def complete(self, messages, *, response_format=None, **kwargs):  # noqa: ANN001
        text = super().complete(messages, response_format=response_format, **kwargs)
        schema = response_format.get("json_schema") if isinstance(response_format, dict) else None
        if isinstance(schema, dict) and schema.get("name") == "marginalia_merge_verdict":
            return '{"same":true,"confidence":0.99,"reason":"stub same"}'
        return text


class _EdgeConnectedPairExtractor:
    """Parses one ``FACT2 subj=<A> pred=<P> obj=<B>`` line into exactly two
    node candidates connected by one real node-to-node edge (A -> B), and
    keeps the exact ``NodeCandidate`` objects it minted so a test can key
    dedup events/outcomes by their deterministic ``candidate_id``."""

    _RE = re.compile(r"FACT2\s+subj=(\S+)\s+pred=(\S+)\s+obj=(\S+)")

    def __init__(self) -> None:
        self.node_a: NodeCandidate | None = None
        self.node_b: NodeCandidate | None = None

    def extract(
        self,
        text: str,
        *,
        provenance: Provenance | None = None,
    ) -> ExtractionResult:
        prov = provenance or Provenance()
        match = self._RE.search(text)
        if match is None:
            return ExtractionResult(node_candidates=[], edge_candidates=[])
        subj, pred, obj = match.group(1), match.group(2), match.group(3)
        node_a = NodeCandidate(
            type="Concept", title=subj, content=f"subject {subj}", provenance=prov
        )
        node_b = NodeCandidate(type="Concept", title=obj, content=f"object {obj}", provenance=prov)
        self.node_a, self.node_b = node_a, node_b
        edge = EdgeCandidate(
            type=pred,
            src_ref=node_a.candidate_id,
            dst_ref=node_b.candidate_id,
            provenance=prov,
        )
        return ExtractionResult(node_candidates=[node_a, node_b], edge_candidates=[edge])


def test_tier1_does_not_reabsorb_a_tier0_survivors_edge_partner(tmp_path: Path) -> None:
    """A (title 'NX') exact-title-matches a pre-existing store node ('n-nx')
    and is folded into it by Tier 0. B (title 'NX-B') does not exact-match, so
    it survives Tier 0 -- but the fixed embedder makes it "similar" to n-nx,
    and the stub judge always says "same", so an un-threaded Tier 1 guard
    would merge B into n-nx too, even though B is edge-connected to A (which
    is exactly finding 3.3's shape, one tier later). The fix must veto that
    second merge: B must survive as a distinct candidate, not disappear into
    n-nx alongside A.
    """
    vault = Vault.init(tmp_path / "v")
    try:
        vault.store.add_node(Node(id="n-nx", type="Concept", title="NX"))

        note = Path(vault.path) / "note.md"
        note.write_text("FACT2 subj=NX pred=has_info obj=NX-B\n", encoding="utf-8")

        extractor = _EdgeConnectedPairExtractor()
        companion = Companion(
            vault,
            provider=_AlwaysSameMergeJudgeLLM(),
            extractor=extractor,
            embedder=_FixedEmbedder(),
        )

        events: list[dict] = []
        result = companion.remember(note, on_event=events.append)

        assert extractor.node_a is not None
        assert extractor.node_b is not None

        # Precondition: Tier 0 actually folded A into the pre-existing node --
        # otherwise this test isn't exercising the scenario it claims to.
        exact_events = [e for e in events if e.get("kind") == "dedup_store_exact"]
        assert exact_events, "expected a dedup_store_exact event"
        assert exact_events[-1]["payload"]["merged_into"] == {extractor.node_a.candidate_id: "n-nx"}

        # The actual regression: Tier 1/2 must NOT also fold B into n-nx.
        judge_events = [e for e in events if e.get("kind") == "dedup_store_judge"]
        assert judge_events, "expected a dedup_store_judge event"
        judge_merged_into = judge_events[-1]["payload"]["merged_into"]
        assert extractor.node_b.candidate_id not in judge_merged_into, (
            f"B was merged into {judge_merged_into.get(extractor.node_b.candidate_id)!r} "
            "by Tier 1 despite being edge-connected to A, which Tier 0 already "
            "folded into n-nx -- the self-loop-and-dropped-relationship failure "
            "from finding 3.3, one tier later"
        )

        # B must have flowed on to resolve/gate as its own candidate (a
        # merged-away candidate never gets an outcome), not vanished.
        outcome_ids = {o.candidate_id for o in result.outcomes}
        assert extractor.node_b.candidate_id in outcome_ids

        # And the A-B relationship must still be a real edge onto the
        # surviving pair (n-nx, B), not remapped into a dropped self-loop.
        assert judge_events[-1]["payload"]["edges"], "the A-B relationship was dropped"
    finally:
        vault.close()
