"""Finding 3.5 (marginalia-deep-review §3.5): ``ingest_document`` used to
unconditionally write the freshly parsed deterministic
(has_tag/has_heading/links_to) Claims with no removal of stale ones from a
prior ingest of the same document. An edited tag/heading/wikilink left its
OLD Claim id stranded, live, and directly searchable via BM25 (no staleness
filter there) forever, since Block/Claim ids are content-hash-derived and an
edited chunk mints a NEW id alongside the old one.

Model-free: drives ``ingest_document`` (called by every ``vault.add()``)
directly against an InMemoryStore.
"""

from __future__ import annotations

from pathlib import Path

from okto_neuron._internal.infra import is_superseded
from okto_neuron.ingest import ingest_document
from okto_neuron.store.memory import InMemoryStore


def _tags(store: InMemoryStore) -> list:
    return [n for n in store.list_nodes(type="Claim") if (n.facets or {}).get("P") == "has_tag"]


def test_reingest_supersedes_claim_for_removed_tag(tmp_path: Path) -> None:
    p = tmp_path / "note.md"
    p.write_text("---\ntags: [alpha]\n---\n\nbody text.\n", encoding="utf-8")
    store = InMemoryStore()

    ingest_document(store, p, vault_root=tmp_path)
    alpha = [c for c in _tags(store) if c.facets.get("O_literal") == "alpha"]
    assert len(alpha) == 1
    assert not is_superseded(alpha[0])
    alpha_id = alpha[0].id

    p.write_text("---\ntags: [beta]\n---\n\nbody text.\n", encoding="utf-8")
    ingest_document(store, p, vault_root=tmp_path)

    refreshed = store.get_node(alpha_id)
    assert refreshed is not None
    assert is_superseded(refreshed), (
        "the 'alpha' has_tag Claim is no longer asserted by the file and "
        "must be superseded (filtered from recall), not accumulate forever"
    )

    live_beta = [
        c for c in _tags(store) if c.facets.get("O_literal") == "beta" and not is_superseded(c)
    ]
    assert len(live_beta) == 1


def test_reingest_does_not_touch_still_current_tags(tmp_path: Path) -> None:
    p = tmp_path / "note.md"
    p.write_text("---\ntags: [alpha]\n---\n\nbody v1.\n", encoding="utf-8")
    store = InMemoryStore()

    ingest_document(store, p, vault_root=tmp_path)
    alpha_id = [c for c in _tags(store) if c.facets.get("O_literal") == "alpha"][0].id

    # Edit the BODY only — the frontmatter block (which anchors the tag
    # Claim) is byte-identical, so the tag Claim's content-hash-derived id is
    # unchanged and must stay live.
    p.write_text("---\ntags: [alpha]\n---\n\nbody v2 - unrelated edit.\n", encoding="utf-8")
    ingest_document(store, p, vault_root=tmp_path)

    refreshed = store.get_node(alpha_id)
    assert refreshed is not None
    assert not is_superseded(refreshed), (
        "a tag still present after re-ingest must stay live, not be superseded"
    )


def test_reingest_after_removal_then_restore_resurrects_claim(tmp_path: Path) -> None:
    """The revert-for-free property this fix relies on: ``add_node`` fully
    overwrites facets, so re-minting the byte-identical fresh Claim (same
    content-hash-derived id) in the main claims loop clears any earlier
    ``_superseded`` stamp, PROVIDED the fresh-claims loop runs before the
    retirement pass. Pins that ordering."""
    p = tmp_path / "note.md"
    original = "---\ntags: [alpha]\n---\n\nbody text.\n"
    p.write_text(original, encoding="utf-8")
    store = InMemoryStore()

    ingest_document(store, p, vault_root=tmp_path)
    alpha_id = [c for c in _tags(store) if c.facets.get("O_literal") == "alpha"][0].id

    p.write_text("---\ntags: [beta]\n---\n\nbody text.\n", encoding="utf-8")
    ingest_document(store, p, vault_root=tmp_path)
    assert is_superseded(store.get_node(alpha_id))

    p.write_text(original, encoding="utf-8")
    ingest_document(store, p, vault_root=tmp_path)

    assert not is_superseded(store.get_node(alpha_id)), (
        "reverting to content that reproduces the exact same content-hash-"
        "derived Claim id must clear the stale _superseded stamp"
    )


def test_reingest_does_not_touch_llm_minted_claims(tmp_path: Path) -> None:
    """The retirement pass is scoped to deterministic
    (has_tag/has_heading/links_to, rule_id='deterministic-v1') Claims only —
    an LLM-minted Claim subject-anchored to the same document must survive
    untouched even if it isn't in ingest_document's fresh deterministic set."""
    from okto_neuron.core.schema import Edge, Node, Provenance
    from okto_neuron.ingest.markdown import sha256_hex

    p = tmp_path / "note.md"
    p.write_text("---\ntags: [alpha]\n---\n\nbody text.\n", encoding="utf-8")
    store = InMemoryStore()
    ingest_document(store, p, vault_root=tmp_path)

    document_id = sha256_hex("document", str(p.resolve()))
    llm_claim_id = "a" * 64
    store.add_node(
        Node(
            id=llm_claim_id,
            type="Claim",
            title="llm claim",
            content="subject relates_to object",
            facets={"P": "relates_to", "O_literal": "object", "model_id": "test-model"},
            provenance=Provenance(source="companion-remember", rule_id="companion-remember"),
        )
    )
    store.add_edge(
        Edge(
            id=sha256_hex("edge", llm_claim_id, "rdf:subject", document_id),
            type="rdf:subject",
            src=llm_claim_id,
            dst=document_id,
            provenance=Provenance(source="test", rule_id="t"),
        )
    )

    # Re-ingest with the SAME tags — nothing about the deterministic set
    # changed, but the retirement pass still walks every rdf:subject edge
    # into the document.
    ingest_document(store, p, vault_root=tmp_path)

    refreshed = store.get_node(llm_claim_id)
    assert refreshed is not None
    assert not is_superseded(refreshed), (
        "an LLM-minted Claim must never be touched by the deterministic-claim retirement pass"
    )
