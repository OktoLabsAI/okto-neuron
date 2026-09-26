"""Deterministic ``has_heading`` Claims are structural anchors (a document's
table of contents), not propositional knowledge. They are tagged
``_salience:low`` at materialization so the recall/ask/subgraph filters
(``is_low_salience``) drop them from retrieval, while the Claim stays a
first-class, provenance-anchored node in the graph.

Model-free: drives the deterministic ``ingest_document`` path against an
InMemoryStore. No LLM, no network.
"""

from __future__ import annotations

from pathlib import Path

from okto_neuron._internal.infra import is_low_salience
from okto_neuron.ingest import ingest_document
from okto_neuron.store.memory import InMemoryStore

_DOC = "---\ntitle: Note\n---\n\n# Overview\n\nAlpha relates to beta here.\n"


def _ingest(tmp_path: Path) -> InMemoryStore:
    p = tmp_path / "note.md"
    p.write_text(_DOC, encoding="utf-8")
    store = InMemoryStore()
    ingest_document(store, p, vault_root=tmp_path)
    return store


def _claims_by_predicate(store: InMemoryStore, predicate: str) -> list:
    return [n for n in store.list_nodes(type="Claim") if (n.facets or {}).get("P") == predicate]


def test_has_heading_claim_is_low_salience(tmp_path: Path) -> None:
    store = _ingest(tmp_path)
    headings = _claims_by_predicate(store, "has_heading")
    assert headings, "expected a has_heading Claim from the '# Overview' heading"
    for claim in headings:
        assert claim.facets.get("_salience") == "low"
        # The retrieval filter must actually fire on the materialized node.
        assert is_low_salience(claim) is True


def test_non_heading_claims_are_not_low_salience(tmp_path: Path) -> None:
    # Frontmatter carries no tags here, so the only structural claims are
    # headings; assert nothing else got mislabeled low-salience.
    store = _ingest(tmp_path)
    for claim in store.list_nodes(type="Claim"):
        if (claim.facets or {}).get("P") == "has_heading":
            continue
        assert not is_low_salience(claim)
