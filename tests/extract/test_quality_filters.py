"""Tests for extraction quality gates: structural-noise filter + node-cap.

These pin the two in-prompt-independent defenses added alongside the redesigned
extraction prompt — a deterministic structural-noise title drop and a per-block
runaway circuit breaker — so they keep working regardless of model behavior.
"""

from __future__ import annotations

import json

from okto_neuron.extract import (
    ALLOWED_NODE_TYPES,
    MAX_NODES_PER_BLOCK,
    ExtractionResult,
    LLMExtractor,
    _canonical_title,
    _is_filename_noise,
    _is_generic_token_noise,
    _is_low_value_title,
    _is_structural_noise,
    parse_extraction,
)


class _StubProvider:
    """Returns a canned JSON payload; records calls."""

    model = "stub"

    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.calls: list = []

    def complete(self, messages, *, temperature=0.0, max_tokens=1024, **kwargs) -> str:
        self.calls.append((messages, temperature, max_tokens))
        return self._payload


class TestStructuralNoiseFilter:
    """``_is_structural_noise`` drops scaffolding, keeps real entities."""

    def test_drops_document_structure_titles(self) -> None:
        for title in [
            "Open Questions",
            "Risks",
            "Assumptions",
            "Deliverables",
            "Scope",
            "Design Rationale",
            "Key Findings",
            "Next Steps",
            "Background",
        ]:
            assert _is_structural_noise(title), title

    def test_drops_priority_and_section_codes(self) -> None:
        for title in [
            "P1",
            "P11",
            "A7",
            "A13",
            "M5",
            "Subsection 4.2",
            "Section 3.1",
            "§6",
            "1.3.g",
        ]:
            assert _is_structural_noise(title), title

    def test_drops_generated_parser_block_titles(self) -> None:
        for title in [
            "paragraph 0",
            "Paragraph 1",
            "code-block 0",
            "Code Block 2",
            "code_block 3",
            "blockquote 4",
            "list item 5",
            "Table 1",
        ]:
            assert _is_structural_noise(title), title

    def test_drops_bare_type_names(self) -> None:
        for title in ["Agent", "Activity", "Concept", "InformationObject", "Node", "Entity"]:
            assert _is_structural_noise(title), title

    def test_keeps_real_entities(self) -> None:
        for title in [
            "NX",
            "NX Lab",
            "Casey Buck",
            "Alex Rivera",
            "Azure OpenAI",
            "COPPA",
            "SOW DRAFT v0.1",
            "Tovrin Kalia",
            "OCR routing",
            "Statement of Work No. 3",
        ]:
            assert not _is_structural_noise(title), title

    def test_parse_extraction_drops_structural_nodes(self) -> None:
        payload = json.dumps(
            {
                "nodes": [
                    {"type": "Agent", "title": "NX", "content": "NX is a company."},
                    {"type": "Concept", "title": "Open Questions", "content": "noise"},
                    {"type": "Concept", "title": "code-block 0", "content": "noise"},
                    {"type": "Concept", "title": "paragraph 1", "content": "noise"},
                    {"type": "Concept", "title": "P1", "content": "noise"},
                    {"type": "Concept", "title": "OCR routing", "content": "real."},
                ],
                "edges": [],
            }
        )
        result = parse_extraction(payload)
        titles = {n.title for n in result.node_candidates}
        assert titles == {"NX", "OCR routing"}

    def test_edges_to_dropped_node_are_pruned(self) -> None:
        payload = json.dumps(
            {
                "nodes": [
                    {"type": "Agent", "title": "NX", "content": "c"},
                    {"type": "Concept", "title": "Risks", "content": "noise"},
                ],
                "edges": [{"type": "rel", "src": "NX", "dst": "Risks"}],
            }
        )
        result = parse_extraction(payload)
        # Risks dropped → the edge referencing it cannot resolve → pruned.
        assert [n.title for n in result.node_candidates] == ["NX"]
        assert result.edge_candidates == []


class TestClaimTypeNotExtractable:
    """Workstream #3: ``Claim`` is not an extractable node type, and a node titled
    ``"Claim"`` is still dropped as type-name noise."""

    def test_claim_absent_from_allowlist(self) -> None:
        assert "Claim" not in ALLOWED_NODE_TYPES

    def test_claim_typed_node_dropped(self) -> None:
        payload = json.dumps(
            {
                "nodes": [
                    {"type": "Agent", "title": "NX", "content": "a company"},
                    {"type": "Claim", "title": "NX slowed 19%", "content": "a fact"},
                ],
                "edges": [],
            }
        )
        result = parse_extraction(payload)
        titles = {n.title for n in result.node_candidates}
        assert titles == {"NX"}  # the Claim-typed node never survives the allowlist
        assert all(n.type in ALLOWED_NODE_TYPES for n in result.node_candidates)

    def test_node_titled_claim_still_dropped_as_noise(self) -> None:
        # "Claim" left ALLOWED_NODE_TYPES; the title-name guard must still drop it.
        assert _is_structural_noise("Claim")
        payload = json.dumps(
            {
                "nodes": [{"type": "Concept", "title": "Claim", "content": "noise"}],
                "edges": [],
            }
        )
        assert parse_extraction(payload).node_candidates == []


class TestGenericTokenDenylist:
    """Workstream #2a: a tiny curated denylist of bare file-type/format tokens is
    dropped, while domain acronyms always survive (no length filter)."""

    def test_drops_denylisted_tokens(self) -> None:
        for title in ["pdf", "PDF", "doc", "image", "file", "link", "url", " URL "]:
            assert _is_generic_token_noise(title), title

    def test_keeps_domain_acronyms_and_phrases(self) -> None:
        for title in ["NX", "AI", "ML", "ZDR", "NX access", "PDF export pipeline"]:
            assert not _is_generic_token_noise(title), title

    def test_parse_extraction_drops_denylisted_keeps_acronyms(self) -> None:
        payload = json.dumps(
            {
                "nodes": [
                    {"type": "Concept", "title": "pdf", "content": "format noise"},
                    {"type": "Concept", "title": "link", "content": "format noise"},
                    {"type": "Agent", "title": "NX", "content": "a company"},
                    {"type": "Concept", "title": "ZDR", "content": "zero data retention"},
                ],
                "edges": [],
            }
        )
        titles = {n.title for n in parse_extraction(payload).node_candidates}
        assert titles == {"NX", "ZDR"}


class TestFilesystemMetadataPredicates:
    """File-provenance / filesystem-metadata claims describe the SOURCE FILE,
    not domain knowledge — they are dropped at parse time (same seam as
    tag/version metadata predicates)."""

    def test_file_size_claim_not_minted(self) -> None:
        # "file size" normalizes to "file_size" and is dropped; a real domain
        # claim from the same payload survives.
        payload = json.dumps(
            {
                "nodes": [
                    {"type": "InformationObject", "title": "Report", "content": "c"},
                ],
                "edges": [],
                "claims": [
                    {"subject": "Report", "predicate": "file size", "object": "4kb"},
                    {"subject": "Report", "predicate": "concludes", "object": "growth"},
                ],
            }
        )
        result = parse_extraction(payload)
        preds = {e.type for e in result.edge_candidates}
        assert "file size" not in preds and "file_size" not in preds
        assert "concludes" in preds

    def test_filesystem_predicate_variants_dropped(self) -> None:
        for pred in [
            "file size",
            "file_name",
            "last modified",
            "mime type",
            "checksum",
            "file path",
        ]:
            payload = json.dumps(
                {
                    "nodes": [{"type": "Concept", "title": "Doc", "content": "c"}],
                    "edges": [],
                    "claims": [{"subject": "Doc", "predicate": pred, "object": "x"}],
                }
            )
            result = parse_extraction(payload)
            assert result.edge_candidates == [], pred

    def test_company_size_survives(self) -> None:
        # "company size" must NOT be caught by the file-size drop (bare "size"
        # is deliberately not denylisted).
        payload = json.dumps(
            {
                "nodes": [{"type": "Agent", "title": "NX", "content": "c"}],
                "edges": [],
                "claims": [{"subject": "NX", "predicate": "company size", "object": "large"}],
            }
        )
        result = parse_extraction(payload)
        assert [e.type for e in result.edge_candidates] == ["company size"]


class TestConceptTitleCanonicalization:
    """Concept titles get normalized before ids/edge refs are minted."""

    def test_parse_extraction_canonicalizes_concept_titles_and_refs(self) -> None:
        payload = json.dumps(
            {
                "nodes": [
                    {
                        "type": "Concept",
                        "title": "The Confidence Scale",
                        "content": "a self-assessment scale",
                    },
                    {
                        "type": "Concept",
                        "title": "domain expertise multiplier",
                        "content": "AI multiplies existing expertise",
                    },
                    {
                        "type": "Concept",
                        "title": "LLM mechanics",
                        "content": "how LLMs behave",
                    },
                    {"type": "Concept", "title": "DDD", "content": "domain driven design"},
                    {"type": "Agent", "title": "The Research Team", "content": "named team"},
                ],
                "edges": [
                    {
                        "type": "supports",
                        "src": "The Confidence Scale",
                        "dst": "domain expertise multiplier",
                    }
                ],
                "claims": [
                    {
                        "subject": "The Confidence Scale",
                        "predicate": "used_for",
                        "object": "surfacing blind spots",
                    }
                ],
            }
        )

        result = parse_extraction(payload)

        titles = {n.title for n in result.node_candidates}
        # Shape is normalized: a leading generic "The" is dropped from a Concept
        # and the edge/claim refs rebind to the stripped title.
        assert "Confidence Scale" in titles
        assert "The Confidence Scale" not in titles
        # Casing is NOT normalized — the source spelling is provenance and there
        # is no language signal to recase against (ADR 0040). Matching still
        # collapses case via semantic_surface.exact_surface_key.
        assert "domain expertise multiplier" in titles
        assert "LLM mechanics" in titles
        assert "DDD" in titles
        # A non-Concept keeps its leading article: it can be part of the name.
        assert "The Research Team" in titles
        assert len(result.edge_candidates) == 2
        assert {edge.type for edge in result.edge_candidates} == {"supports", "used_for"}

    def test_parse_extraction_keeps_plural_local_taxonomy_titles_without_sdlc_pack(
        self,
    ) -> None:
        payload = json.dumps(
            {
                "nodes": [
                    {"type": "Concept", "title": "Epics", "content": "big goals"},
                    {"type": "Concept", "title": "Stories", "content": "features"},
                    {"type": "Concept", "title": "Tasks", "content": "atomic work"},
                ],
                "edges": [],
            }
        )

        result = parse_extraction(payload)

        assert {candidate.title for candidate in result.node_candidates} == {
            "Epics",
            "Stories",
            "Tasks",
        }

    def test_parse_extraction_singularizes_sdlc_taxonomy_titles_and_refs(self) -> None:
        payload = json.dumps(
            {
                "nodes": [
                    {"type": "Concept", "title": "Epics", "content": "big goals"},
                    {"type": "Concept", "title": "Stories", "content": "features"},
                    {"type": "Concept", "title": "Tasks", "content": "atomic work"},
                ],
                "edges": [
                    {"type": "breaks_into", "src": "Epics", "dst": "Stories"},
                    {"type": "breaks_into", "src": "Stories", "dst": "Tasks"},
                ],
                "claims": [
                    {
                        "subject": "Tasks",
                        "predicate": "requires",
                        "object": "Definition of Done",
                    }
                ],
            }
        )

        result = parse_extraction(payload, packs=["sdlc"])

        by_title = {candidate.title: candidate for candidate in result.node_candidates}
        assert set(by_title) == {"Epic", "Story", "Task"}
        edge_pairs = {(edge.src_ref, edge.dst_ref, edge.type) for edge in result.edge_candidates}
        assert (
            by_title["Epic"].candidate_id,
            by_title["Story"].candidate_id,
            "breaks_into",
        ) in edge_pairs
        assert (
            by_title["Story"].candidate_id,
            by_title["Task"].candidate_id,
            "breaks_into",
        ) in edge_pairs
        assert any(
            edge.src_ref == by_title["Task"].candidate_id
            and edge.type == "requires"
            and edge.dst_literal == "Definition of Done"
            for edge in result.edge_candidates
        )

    def test_parse_extraction_turns_pillar_document_label_into_topic_concept(self) -> None:
        payload = json.dumps(
            {
                "nodes": [
                    {
                        "type": "InformationObject",
                        "title": "Pillar 7: domain expertise & blind spot management",
                        "content": "a document label for the pillar",
                    },
                    {
                        "type": "Concept",
                        "title": "The Confidence Scale",
                        "content": "a scale for surfacing blind spots",
                    },
                ],
                "edges": [
                    {
                        "type": "defines",
                        "src": "Pillar 7: domain expertise & blind spot management",
                        "dst": "The Confidence Scale",
                    }
                ],
                "claims": [
                    {
                        "subject": "Pillar 7: domain expertise & blind spot management",
                        "predicate": "includes",
                        "object": "blind spot management",
                    }
                ],
            }
        )

        result = parse_extraction(payload, packs=["sdlc"])

        by_title = {node.title: node for node in result.node_candidates}
        assert by_title["domain expertise & blind spot management"].type == "Concept"
        assert "Pillar 7: domain expertise & blind spot management" not in by_title
        assert len(result.edge_candidates) == 2
        assert {edge.type for edge in result.edge_candidates} == {"defines", "includes"}

    def test_parse_extraction_normalizes_physical_artifact_mistypes(self) -> None:
        payload = json.dumps(
            {
                "nodes": [
                    {
                        "type": "InformationObject",
                        "title": "Narsil",
                        "content": "Narsil is the Sword of Elendil.",
                    },
                    {
                        "type": "Agent",
                        "title": "Sting",
                        "content": "Sting is a named sword carried by Bilbo.",
                    },
                    {
                        "type": "InformationObject",
                        "title": "The Hobbit",
                        "content": "A book written by J. R. R. Tolkien.",
                    },
                ],
                "edges": [
                    {"type": "also_known_as", "src": "Narsil", "dst": "Sting"},
                    {"type": "mentions", "src": "The Hobbit", "dst": "Sting"},
                ],
            }
        )

        result = parse_extraction(payload)

        by_title = {node.title: node for node in result.node_candidates}
        assert by_title["Narsil"].type == "Concept"
        assert by_title["Sting"].type == "Concept"
        assert by_title["The Hobbit"].type == "InformationObject"
        assert len(result.edge_candidates) == 2


class TestNodeCap:
    """A block yielding more than the cap is rejected as runaway extraction."""

    def _payload(self, n: int) -> str:
        nodes = [{"type": "Agent", "title": f"Person {i}", "content": f"c{i}"} for i in range(n)]
        return json.dumps({"nodes": nodes, "edges": []})

    def test_under_cap_passes(self) -> None:
        provider = _StubProvider(self._payload(MAX_NODES_PER_BLOCK))
        result = LLMExtractor(provider).extract("text")
        assert len(result.node_candidates) == MAX_NODES_PER_BLOCK

    def test_over_cap_rejects_whole_block(self) -> None:
        provider = _StubProvider(self._payload(MAX_NODES_PER_BLOCK + 1))
        result = LLMExtractor(provider).extract("text")
        assert result == ExtractionResult()

    def test_custom_cap_respected(self) -> None:
        provider = _StubProvider(self._payload(4))
        assert LLMExtractor(provider, max_nodes_per_block=3).extract("t") == ExtractionResult()
        provider2 = _StubProvider(self._payload(3))
        assert len(LLMExtractor(provider2, max_nodes_per_block=3).extract("t").node_candidates) == 3

    def _payload_titles(self, titles: list[str]) -> str:
        nodes = [{"type": "Agent", "title": t, "content": "c"} for t in titles]
        return json.dumps({"nodes": nodes, "edges": []})

    def test_legit_dense_block_passes(self) -> None:
        # Regression: the dense-fact prompt legitimately yields ~29 DISTINCT
        # entities in ~1639 chars. With RUNAWAY_CHARS_PER_NODE=50 the cap is
        # max(12, 1639//50)=32, so 29 distinct nodes must NOT be rejected.
        text = "x" * 1639
        provider = _StubProvider(self._payload_titles([f"App {i}" for i in range(29)]))
        result = LLMExtractor(provider).extract(text)
        assert len(result.node_candidates) == 29

    def test_exact_duplicate_explosion_does_not_trip(self) -> None:
        # An exact-duplicate loop collapses to one id downstream; the breaker
        # counts DISTINCT titles, so it must NOT reject on raw duplicate count.
        provider = _StubProvider(self._payload_titles(["Dup"] * 60))
        result = LLMExtractor(provider).extract("short text")
        assert result != ExtractionResult()

    def test_distinct_runaway_still_rejects(self) -> None:
        # A near-/distinct-node explosion well over the char-scaled cap still trips.
        text = "x" * 1639  # cap = max(12, 1639//50) = 32
        provider = _StubProvider(self._payload_titles([f"Hallucination {i}" for i in range(100)]))
        result = LLMExtractor(provider).extract(text)
        assert result == ExtractionResult()


class TestEmptyAndOptionalFields:
    """The empty-result case parses cleanly; aliases are never required."""

    def test_empty_result_parses_empty(self) -> None:
        assert parse_extraction('{"nodes":[],"edges":[]}') == ExtractionResult()

    def test_nodes_without_aliases_key_parse(self) -> None:
        payload = '{"nodes":[{"type":"Agent","title":"Sam","content":"c"}],"edges":[]}'
        result = parse_extraction(payload)
        assert [n.title for n in result.node_candidates] == ["Sam"]


class TestLayer1SourceDrop:
    """Layer 1: metadata-predicate claims and low-value Concept titles are dropped
    at parse time, while legit short entities survive."""

    def test_metadata_predicate_claim_dropped(self) -> None:
        payload = json.dumps(
            {
                "nodes": [
                    {"type": "Concept", "title": "QZX", "content": "a method"},
                    {"type": "Concept", "title": "v1", "content": "version one"},
                ],
                "edges": [],
                "claims": [
                    {"subject": "QZX", "predicate": "tag", "object": "9"},
                    {"subject": "QZX", "predicate": "found", "object": "a real finding"},
                ],
            }
        )
        result = parse_extraction(payload)
        titles = [n.title for n in result.node_candidates]
        assert "QZX" in titles  # legit short entity kept
        assert "v1" not in titles  # version token dropped
        preds = [e.type for e in result.edge_candidates]
        assert "tag" not in preds  # metadata-predicate claim dropped at source
        assert "found" in preds  # real propositional claim kept

    def test_noisy_placeholder_predicates_dropped(self) -> None:
        payload = json.dumps(
            {
                "nodes": [
                    {"type": "Agent", "title": "Frodo", "content": "a hobbit"},
                    {"type": "Place", "title": "Shire", "content": "a homeland"},
                ],
                "edges": [
                    {"type": "unknown", "src": "Frodo", "dst": "Shire"},
                    {"type": "lives_in", "src": "Frodo", "dst": "Shire"},
                ],
                "claims": [
                    {"subject": "Frodo", "predicate": "n/a", "object": "placeholder"},
                    {"subject": "Frodo", "predicate": "status", "object": "travelling"},
                ],
            }
        )

        result = parse_extraction(payload)

        preds = [edge.type for edge in result.edge_candidates]
        assert preds == ["lives_in", "status"]

    def test_metadata_predicate_variants_dropped(self) -> None:
        payload = json.dumps(
            {
                "nodes": [
                    {
                        "type": "Concept",
                        "title": "Competency Pillars",
                        "content": "a competency model",
                    }
                ],
                "edges": [],
                "claims": [
                    {
                        "subject": "Competency Pillars",
                        "predicate": "built_date",
                        "object": "2026-05-05",
                    },
                    {
                        "subject": "Competency Pillars",
                        "predicate": "source",
                        "object": "Interview extraction",
                    },
                    {
                        "subject": "Competency Pillars",
                        "predicate": "is-version",
                        "object": "1.0",
                    },
                    {
                        "subject": "Competency Pillars",
                        "predicate": "includes",
                        "object": "Problem Decomposition",
                    },
                ],
            }
        )

        result = parse_extraction(payload)

        preds = [e.type for e in result.edge_candidates]
        assert "built_date" not in preds
        assert "source" not in preds
        assert "is-version" not in preds
        assert "includes" in preds

    def test_low_value_titles_dropped_short_entities_kept(self) -> None:
        for title in ["v1", "v2.3", "9", "42", "#", "-"]:
            assert _is_low_value_title(title), title
        for title in ["QZX", "NX", "AI", "RJ", "Derek"]:
            assert not _is_low_value_title(title), title


class TestCanonicalTitleNormalization:
    """``_canonical_title`` normalizes title SHAPE only — whitespace, spacing
    around "/", a trailing "/", a leading generic "The". It must never change
    the letters: source spelling is provenance (ADR 0040 rejects "normalize
    names by overwriting titles"), and this pipeline has no language signal to
    recase against. A per-token recasing pass used to run here with an
    English-only small-word set and rendered every Portuguese title wrong."""

    def test_casing_is_preserved_verbatim(self) -> None:
        for raw in (
            "serviços do contribuinte",  # PT: "do" must stay lowercase
            "Serviços do Contribuinte",
            "convenções de nome",
            "taxa de fiscalização de estabelecimentos",
            "evidências",
            "Solar Petrópolis",
            "são paulo",
            "OpenAI",
            "GraphQL",
            "portainer MCPs",
            "US-01 month initialization",
        ):
            assert _canonical_title("Concept", raw) == raw, raw

    def test_accents_survive_intact(self) -> None:
        """An ASCII-only tokenizer once split words at the accent, so
        "evidências" came back as "EvidÊNcias"."""
        for raw in ("evidências", "Solar Petrópolis", "ação", "São Paulo"):
            assert _canonical_title("Concept", raw) == raw, raw

    def test_trailing_slash_and_padding_are_stripped(self) -> None:
        """A directory label must collapse onto the bare name, not fork off a
        second Concept ("Guias/" vs "Guias")."""
        assert _canonical_title("Concept", "guias/") == "guias"
        assert _canonical_title("Concept", "husky /") == "husky"
        assert _canonical_title("Concept", "notas-fiscais / ") == "notas-fiscais"
        assert _canonical_title("Concept", "data/inbox/") == "data/inbox"

    def test_leading_generic_the_is_still_dropped(self) -> None:
        assert _canonical_title("Concept", "the confidence scale") == "confidence scale"

    def test_non_concept_titles_pass_through(self) -> None:
        raw = "Histórico Enel UC 8495510 — Solar Petrópolis"
        assert _canonical_title("InformationObject", raw) == raw


class TestFilenameNoiseFilter:
    """Catalog and listing documents make the model emit file names as entities.
    A container is not an entity, and these duplicate the thing the file is
    about — a real vault held Concepts "bookkeeping.md" and "orchestrate.py"."""

    def test_filenames_are_dropped(self) -> None:
        for title in (
            "bookkeeping.md",
            "archive/bookkeeping.md",
            "tax-provision-2026-Q3.md",
            "orchestrate.py",
            "fetch_gmail.py",
            "controle.db",
            "sha256.txt",
        ):
            assert _is_filename_noise(title), title

    def test_real_entities_containing_dots_and_slashes_are_kept(self) -> None:
        """The filter keys on a known extension and nothing else, because real
        entities in a Brazilian tax corpus contain "/" and "."."""
        for title in (
            "PIS/COFINS",
            "CNAE 6201-5/01",
            "ACME OPERATIONS BH D.O.O.",
            "Resolução CMN n° 5.112",
            "IRPJ - Lucro Presumido",
            "DARF IRPJ+CSLL",
        ):
            assert not _is_filename_noise(title), title
