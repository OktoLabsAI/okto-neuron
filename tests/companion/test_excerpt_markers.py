"""EXCERPT markers on assembled ask context (model-free).

Live incident (2026-09-17): a tax question was answered "R$ 470,58" when the
true value, R$ 502,51, sat at byte ~25443 of a 32111-byte source. Retrieval had
served only bytes 0-5959 and 14643-15070 of that file, so the answer was
outside every slice the model saw. Because nothing marked those slices as
FRAGMENTS, the model read them as the whole record and extrapolated from an
adjacent year — ``synthesis_status="ok"``, five citations, indistinguishable
from a correct answer.

These tests pin the marker format and the one place the model is TOLD it is
reading excerpts. They assert on the ASSEMBLED CONTEXT and the built prompt,
never on model output.
"""

from __future__ import annotations

from pathlib import Path

from okto_neuron.companion import (
    _EXCERPT_MARKER_TOKEN,
    _format_excerpt_marker,
    _hit_excerpt_marker,
    _source_context_for_hits,
)
from okto_neuron.models import Node as PublicNode, Provenance, QueryHit

_HASH = "sha256:" + "a" * 64


def _hit(node_id: str, path: str, start: int, end: int) -> QueryHit:
    return QueryHit(
        node=PublicNode(id=node_id, type="Claim", name=node_id),
        score=0.5,
        provenance=Provenance(
            path=path,
            byte_start=start,
            byte_end=end,
            content_hash=_HASH,
            extraction_activity_id="act",
            agent_id="agent",
            document_id="doc",
            block_id=f"blk-{node_id}",
        ),
    )


def test_marker_format_is_stable() -> None:
    assert (
        _format_excerpt_marker("cnpj/guias/catalogo.md", [(0, 5959), (14643, 15070)], 32111)
        == "[EXCERPT source=cnpj/guias/catalogo.md bytes=0-5959,14643-15070 of 32111]"
    )
    # Each component degrades independently; no absolute path ever leaks.
    assert _format_excerpt_marker(None, [(0, 10)], 100) == "[EXCERPT bytes=0-10 of 100]"
    assert _format_excerpt_marker("a.md", [(0, 10)], None) == "[EXCERPT source=a.md bytes=0-10]"
    assert "\n" not in _format_excerpt_marker("a.md", [(0, 10)], 100)


def test_assembled_context_marks_each_excerpt_with_path_range_and_total(
    tmp_path: Path,
) -> None:
    doc = tmp_path / "notes" / "catalogo.md"
    doc.parent.mkdir()
    doc.write_bytes(b"X" * 40 + b"THE-ANSWER-502.51" + b"Y" * 43)
    total = doc.stat().st_size

    context = _source_context_for_hits(
        [_hit("c1", str(doc), 0, 10), _hit("c2", str(doc), 20, 30)],
        vault_root=tmp_path,
    )
    assert f"[EXCERPT source=notes/catalogo.md bytes=0-10 of {total}]" in context
    assert f"[EXCERPT source=notes/catalogo.md bytes=20-30 of {total}]" in context
    # The marker is what makes the gap visible: the value is NOT in the context,
    # and the marker says the excerpts cover 20 of 100 bytes.
    assert "THE-ANSWER-502.51" not in context


def test_anchorless_hit_gets_no_marker(tmp_path: Path) -> None:
    """A node-NAME fallback is not an excerpt; labelling it would fabricate a
    byte range."""
    nameless = QueryHit(
        node=PublicNode(id="n1", type="Concept", name="a concept"),
        score=0.5,
        provenance=Provenance(
            path="",
            byte_start=0,
            byte_end=0,
            content_hash=_HASH,
            extraction_activity_id="act",
            agent_id="agent",
            document_id="doc",
            block_id="blk",
        ),
    )
    marker, text, from_source = _hit_excerpt_marker(nameless, vault_root=tmp_path)
    assert marker == ""
    assert (text, from_source) == ("a concept", False)
    assert _EXCERPT_MARKER_TOKEN not in _source_context_for_hits([nameless], vault_root=tmp_path)


def test_marker_never_emits_an_absolute_path(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"Z" * 50)
    inside_root = tmp_path / "vault"
    inside_root.mkdir()
    context = _source_context_for_hits([_hit("c1", str(outside), 0, 10)], vault_root=inside_root)
    assert str(tmp_path) not in context


def test_ranking_input_never_contains_markers(tmp_path: Path, monkeypatch) -> None:
    """Markers attach AFTER ranking. If they ever entered the IDF input, their
    tokens (path segments, byte digits) would reorder source blocks — i.e.
    change retrieval behaviour. Pinned on the ranker's actual input, not on an
    outcome that a robust ranking would mask."""
    import okto_neuron.companion as comp

    seen: list[list[str]] = []
    real = comp._query_term_ranked_snippets
    monkeypatch.setattr(
        comp,
        "_query_term_ranked_snippets",
        lambda snippets, terms: (seen.append(list(snippets)), real(snippets, terms))[1],
    )
    doc = tmp_path / "d.md"
    doc.write_bytes(b"alpha beta gamma  delta epsilon zeta")
    context = comp._source_context_for_hits(
        [_hit("a", str(doc), 0, 16), _hit("b", str(doc), 18, 36)],
        vault_root=tmp_path,
        query_terms={"gamma"},
    )
    assert seen, "the ranker was never called"
    assert all(_EXCERPT_MARKER_TOKEN not in snippet for snippet in seen[0])
    # ...and the markers DID land on the output.
    assert _EXCERPT_MARKER_TOKEN in context


def test_ask_prompt_tells_the_model_it_is_reading_excerpts(tmp_path: Path) -> None:
    """The model must be TOLD that absence from an excerpt is not absence from
    the record. Asserts on the PROMPT actually handed to the provider, never on
    model output (no live LLM is involved)."""
    from okto_neuron import Vault
    from okto_neuron.companion import Companion

    doc = tmp_path / "v" / "catalogo.md"
    doc.parent.mkdir()
    doc.write_bytes(b"X" * 200)
    context = _source_context_for_hits([_hit("c1", str(doc), 0, 10)], vault_root=doc.parent)
    assert _EXCERPT_MARKER_TOKEN in context

    sent: list[str] = []

    class _Recorder:
        def complete(self, messages, **_kwargs):
            sent.append(messages[-1].content)
            return ""

    vault = Vault.init(tmp_path / "vault", packs=["core"])
    try:
        companion = Companion(vault)
        companion._get_provider = lambda _role: _Recorder()  # type: ignore[assignment]
        cfg = companion._vault_config()

        companion._complete_ask("q", context, cfg)
        prompt = sent[-1]
        assert "FRAGMENT" in prompt
        assert "not thereby absent from the record" in prompt
        # The full causal chain of the incident: the marker, with its coverage
        # data, reaches the string actually handed to the provider.
        assert f"bytes=0-10 of {doc.stat().st_size}" in prompt

        # No markers -> the scaffold is byte-identical to the pre-change prompt.
        companion._complete_ask("q", "- plain text with no markers", cfg)
        assert sent[-1] == (
            "Answer the question using only the retrieved notes below.\n\n"
            "Question: q\n\nNotes:\n- plain text with no markers"
        )
    finally:
        vault.close()
