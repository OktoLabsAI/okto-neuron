"""Tests for the LLM extractor (Phase B)."""

from __future__ import annotations

from collections.abc import Sequence
import os

import httpx
import pytest

from okto_neuron.consolidate import NodeCandidate
from okto_neuron.extract import (
    ALLOWED_NODE_TYPES,
    ExtractionResult,
    LLMExtractor,
    iter_json_objects,
    parse_extraction,
    validate_extraction_payload,
)
from okto_neuron.llm import Message
from okto_neuron.semantic_surface import build_surface_record, exact_surface_key

_LIVE_API_BASE = os.environ.get("OKTO_NEURON_LLM_BASE_URL", "").strip()
_LIVE_MODEL = os.environ.get("OKTO_NEURON_REALMODEL_MODEL", "unsloth/Qwen3.6-27B-NVFP4").strip()

_GOOD = (
    '{"nodes":['
    '{"type":"Agent","title":"Evan","content":"CDTO at ExampleCorp"},'
    '{"type":"Decision","title":"pivot","content":"ignored, not allowed type"},'
    '{"type":"Concept","title":"ROI","content":"return on investment"}'
    '],"edges":[{"type":"discusses","src":"Evan","dst":"ROI"}]}'
)


class _FakeLLM:
    model = "fake"

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.last_kwargs: dict[str, object] = {}
        self.last_messages: Sequence[Message] = ()

    def complete(
        self,
        messages: Sequence[Message],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        **kwargs,
    ) -> str:
        self.last_messages = messages
        self.last_kwargs = kwargs
        return self._reply


class _SequenceLLM:
    model = "fake-sequence"

    def __init__(self, replies: Sequence[str]) -> None:
        self._replies = iter(replies)

    def complete(self, messages: Sequence[Message], **kwargs: object) -> str:
        return next(self._replies)


def test_parse_maps_nodes_and_resolves_edge_titles() -> None:
    result = parse_extraction(_GOOD)
    titles = {n.title for n in result.node_candidates}
    # "Decision" is not an allowed spine type → dropped
    assert titles == {"Evan", "ROI"}
    assert len(result.edge_candidates) == 1
    edge = result.edge_candidates[0]
    refs = {n.candidate_id: n.title for n in result.node_candidates}
    assert refs[edge.src_ref] == "Evan"
    assert refs[edge.dst_ref] == "ROI"


def test_parse_preserves_source_surface_without_changing_candidate_identity() -> None:
    payload = (
        '{"nodes":[{"type":"Agent","title":"  Galadriel_Of_Lórien  ",'
        '"content":"A named concept."}],"edges":[]}'
    )

    result = parse_extraction(payload)

    candidate = result.node_candidates[0]
    assert candidate.title == "Galadriel_Of_Lórien"
    assert candidate.surface is not None
    assert candidate.surface.source_surface == "  Galadriel_Of_Lórien  "
    assert candidate.surface.exact_key == "galadriel_of_lórien"
    # discovery_surface_key folds diacritics (ADR 0042): "lórien" -> "lorien".
    assert candidate.surface.discovery_key == "galadriel of lorien"
    assert candidate.surface.canonical_title == candidate.title
    assert candidate.surface.aliases == ()
    assert candidate.surface.normalizer_version == "surface.v1"
    assert candidate.surface.normalization_flags == (
        "whitespace_normalized",
        "underscore_separator",
    )

    without_surface = NodeCandidate(
        type=candidate.type,
        title=candidate.title,
        content=candidate.content,
        provenance=candidate.provenance,
    )
    assert candidate.candidate_id == without_surface.candidate_id


def test_surface_record_normalizes_comparison_keys_without_rewriting_source() -> None:
    source = "  Cafe\u0301_—_D’Art  "

    record = build_surface_record(source, "Café d’Art")

    assert record.source_surface == source
    assert record.exact_key == "café_—_d’art"
    # discovery_surface_key folds diacritics (ADR 0042): "café" -> "cafe".
    assert record.discovery_key == "cafe - d'art"
    assert record.canonical_title == "Café d’Art"
    assert record.aliases == ("Cafe\u0301_—_D’Art",)
    assert record.normalization_flags == (
        "non_nfc",
        "whitespace_normalized",
        "underscore_separator",
        "apostrophe_variant",
        "dash_variant",
    )
    assert exact_surface_key(0) == "0"


def test_exact_surface_key_is_closed_under_unicode_canonical_equivalence() -> None:
    assert exact_surface_key("\u0390") == exact_surface_key("\u03aa\u0301")


def test_surface_record_does_not_accept_suspected_mojibake_as_alias() -> None:
    record = build_surface_record("FranÃ§ois", "François")

    assert record.source_surface == "FranÃ§ois"
    assert record.aliases == ()
    assert "suspected_mojibake" in record.normalization_flags


def test_enumerate_exact_dedup_preserves_same_surface_cross_type_conflict() -> None:
    provider = _SequenceLLM(
        [
            "- Mordor as an organization\n- Mordor as a place",
            "",
            '{"nodes":[{"type":"Agent","title":"Mordor","content":"An organization."}],"edges":[]}',
            '{"nodes":[{"type":"Place","title":"Mordor","content":"A region."}],"edges":[]}',
        ]
    )
    extractor = LLMExtractor(
        provider,
        mode="enumerate",
        enumerate_describe_batch=1,
    )

    result = extractor.extract("Mordor the organization operates in Mordor the region.")

    assert [(candidate.type, candidate.title) for candidate in result.node_candidates] == [
        ("Agent", "Mordor"),
        ("Place", "Mordor"),
    ]


def test_enumerate_exact_dedup_collapses_unicode_equivalent_same_type() -> None:
    provider = _SequenceLLM(
        [
            "- first spelling\n- second spelling",
            "",
            '{"nodes":[{"type":"Concept","title":"Café","content":"First."}],"edges":[]}',
            '{"nodes":[{"type":"Concept","title":"Cafe\u0301","content":"Second."}],"edges":[]}',
        ]
    )
    extractor = LLMExtractor(provider, mode="enumerate", enumerate_describe_batch=1)

    result = extractor.extract("Two canonically equivalent spellings refer to one concept.")

    assert len(result.node_candidates) == 1
    survivor = result.node_candidates[0]
    assert survivor.surface is not None
    assert survivor.surface.source_surface == "Café"
    assert survivor.surface.exact_key == exact_surface_key("Café")
    assert "Cafe\u0301" in survivor.surface.aliases
    assert "non_nfc" in survivor.surface.normalization_flags


def test_enumerate_exact_dedup_does_not_promote_discovery_separator_match() -> None:
    provider = _SequenceLLM(
        [
            "- underscored\n- spaced",
            "",
            '{"nodes":[{"type":"Concept","title":"Graph_Store","content":"First."}],"edges":[]}',
            '{"nodes":[{"type":"Concept","title":"Graph Store","content":"Second."}],"edges":[]}',
        ]
    )
    extractor = LLMExtractor(provider, mode="enumerate", enumerate_describe_batch=1)

    result = extractor.extract("The spellings require adjudication, not automatic collapse.")

    assert [candidate.title for candidate in result.node_candidates] == [
        "Graph_Store",
        "Graph Store",
    ]


def test_parse_tolerates_prose_wrapped_json() -> None:
    wrapped = "Sure! Here is the graph:\n```json\n" + _GOOD + "\n```\nDone."
    result = parse_extraction(wrapped)
    assert {n.title for n in result.node_candidates} == {"Evan", "ROI"}


def test_parse_handles_raw_prose_json() -> None:
    # No fence, JSON sits inline after reasoning prose — the common reasoning-model
    # shape that hand-rolled brace counting handled but was never tested.
    text = "Let me think. The answer is " + _GOOD + " and that's final."
    result = parse_extraction(text)
    assert {n.title for n in result.node_candidates} == {"Evan", "ROI"}


def test_parse_ignores_unrelated_braces_then_takes_payload() -> None:
    # A config-shaped object in the reasoning must not be mistaken for the result;
    # validate_extraction_payload requires a `nodes` list.
    text = '{"temperature": 0.0, "note": "warming up"}\n' + _GOOD
    result = parse_extraction(text)
    assert {n.title for n in result.node_candidates} == {"Evan", "ROI"}


def test_parse_takes_last_valid_payload() -> None:
    # Two payloads; the LAST schema-valid one is the final answer.
    first = '{"nodes":[{"type":"Concept","title":"FIRST","content":"early"}],"edges":[]}'
    text = "draft: " + first + "\nfinal: " + _GOOD
    result = parse_extraction(text)
    assert {n.title for n in result.node_candidates} == {"Evan", "ROI"}


def test_parse_rejects_payload_with_non_list_nodes() -> None:
    # nodes is not a list → not a valid extraction payload → empty result.
    assert parse_extraction('{"nodes": "oops", "edges": []}') == ExtractionResult(parse_failed=True)


def test_validate_extraction_payload_gate() -> None:
    assert validate_extraction_payload({"nodes": [], "edges": []})
    assert validate_extraction_payload({"nodes": []})  # edges optional
    assert not validate_extraction_payload({"nodes": {}})
    assert not validate_extraction_payload({"edges": []})
    assert not validate_extraction_payload({"nodes": [], "edges": "x"})
    assert not validate_extraction_payload("not a dict")


def test_iter_json_objects_skips_brace_inside_string() -> None:
    # A `}` inside a string literal must not desync the scan.
    text = 'prefix {"a": "has } brace", "b": 1} suffix'
    objs = iter_json_objects(text)
    assert objs == [{"a": "has } brace", "b": 1}]


def test_parse_returns_empty_on_junk() -> None:
    assert parse_extraction("no json here at all") == ExtractionResult(parse_failed=True)
    assert parse_extraction("") == ExtractionResult(parse_failed=True)


def test_parse_keeps_nodes_when_edges_reference_unknown_titles() -> None:
    data = (
        '{"nodes":[{"type":"Concept","title":"X","content":"c"}],'
        '"edges":[{"type":"rel","src":"X","dst":"GHOST"}]}'
    )
    result = parse_extraction(data)
    assert [node.title for node in result.node_candidates] == ["X"]
    assert result.edge_candidates == []


def test_parse_keeps_unreferenced_nodes_for_curator_review() -> None:
    data = (
        '{"nodes":[{"type":"InformationObject","title":"Task Definition Template",'
        '"content":"A template with criteria."}],"edges":[],"claims":[]}'
    )
    result = parse_extraction(data)
    assert [node.title for node in result.node_candidates] == ["Task Definition Template"]
    assert result.edge_candidates == []


def test_parse_keeps_node_referenced_by_literal_claim() -> None:
    data = (
        '{"nodes":[{"type":"InformationObject","title":"Task Definition Template",'
        '"content":"A template with criteria."}],"edges":[],"claims":[{"subject":'
        '"Task Definition Template","predicate":"describes","object":"objective, '
        'inputs, outputs, criteria, and questions"}]}'
    )
    result = parse_extraction(data)

    assert [node.title for node in result.node_candidates] == ["Task Definition Template"]
    assert len(result.edge_candidates) == 1
    assert result.edge_candidates[0].dst_literal == (
        "objective, inputs, outputs, criteria, and questions"
    )


def test_llm_extractor_uses_provider_reply() -> None:
    extractor = LLMExtractor(_FakeLLM(_GOOD))
    result = extractor.extract("Evan discussed ROI.")
    assert {n.title for n in result.node_candidates} == {"Evan", "ROI"}


def test_llm_extractor_requests_structured_output() -> None:
    provider = _FakeLLM(_GOOD)
    extractor = LLMExtractor(provider)

    extractor.extract("Evan discussed ROI.")

    response_format = provider.last_kwargs["response_format"]
    assert response_format["type"] == "json_schema"
    schema = response_format["json_schema"]
    assert schema["name"] == "marginalia_extraction"
    assert schema["schema"]["required"] == ["nodes", "edges", "claims"]


def test_llm_extractor_prompts_for_eponymous_method_concepts() -> None:
    provider = _FakeLLM(_GOOD)
    extractor = LLMExtractor(provider)

    extractor.extract("The note discusses Dijkstra's algorithm.")

    system = provider.last_messages[0].content
    assert "Dijkstra's algorithm" in system
    assert "full technique title as a Concept" in system
    assert "only if the excerpt also says something substantive about the person" in system
    assert "Alistair Cockburn User Story Format" not in system


def test_llm_extractor_adds_sdlc_method_guidance_for_sdlc_pack() -> None:
    provider = _FakeLLM(_GOOD)
    extractor = LLMExtractor(provider, packs=["sdlc"])

    extractor.extract("Requirements Engineering uses Alistair Cockburn user story format.")

    system = provider.last_messages[0].content
    assert "Software-delivery / SDLC pack guidance" in system
    assert "Alistair Cockburn User Story Format" in system
    assert "as a Concept" in system


def test_llm_extractor_prompts_for_ordered_model_completeness() -> None:
    provider = _FakeLLM(_GOOD)
    extractor = LLMExtractor(provider)

    extractor.extract(
        "Stage 1: Ad-Hoc\nStage 2: Structured\nStage 3: Systematic\nStage 4: Optimized"
    )

    system = provider.last_messages[0].content
    assert "Completeness for ordered models" in system
    assert "EVERY named step/stage/level" in system
    assert "inside a fenced code block" in system
    assert "Stage 4: Optimized" in system
    assert "parenthetical transition" in system
    assert "Checklist Verification" in system
    assert "transition states" in system
    assert "Ordered level/spectrum relationships are progression" in system
    assert "`progresses_to` between adjacent named levels" in system
    assert "Use `breaks_into` only when the source says" in system


def test_llm_extractor_prompts_for_physical_artifact_concepts() -> None:
    provider = _FakeLLM(_GOOD)
    extractor = LLMExtractor(provider)

    extractor.extract("Narsil is the Sword of Elendil.")

    system = provider.last_messages[0].content
    assert "Named physical artifacts or objects" in system
    assert "should be typed as Concept" in system
    assert "Do NOT type them as Agent or InformationObject" in system


def test_llm_extractor_prompts_for_named_method_acronym_cell_liveness() -> None:
    provider = _FakeLLM(_GOOD)
    extractor = LLMExtractor(provider)

    extractor.extract(
        "Verification Infrastructure table: Mindset | "
        "TDD — write the test first, then generate the code."
    )

    system = provider.last_messages[0].content
    assert "Named methods/acronyms inside row cells need graph liveness" in system
    assert "TDD, SOLID, DDD, or Feynman Technique" in system
    assert "emit a grounded topology edge from the current topic or row label" in system
    assert "`uses`, `underpins`, or `maps_to`" in system
    assert '"title":"TDD"' in system
    assert '"type":"uses","src":"Verification Infrastructure","dst":"TDD"' in system
    assert '"subject":"TDD","predicate":"defines"' in system


def test_llm_extractor_prompts_for_local_taxonomy_chains() -> None:
    provider = _FakeLLM(_GOOD)
    extractor = LLMExtractor(provider)

    extractor.extract("The setting hierarchy is Kingdoms -> Regions -> Settlements.")

    system = provider.last_messages[0].content
    assert "Completeness for local taxonomy chains" in system
    assert "Prefer singular canonical titles" in system
    assert "Kingdoms -> Regions -> Settlements" in system
    assert '"title":"Kingdom"' in system
    assert '"title":"Region"' in system
    assert '"title":"Settlement"' in system
    assert '"type":"breaks_into","src":"Kingdom","dst":"Region"' in system
    assert "`Epics -> Stories -> Tasks`" not in system


def test_llm_extractor_prompts_for_named_table_row_parent_relations() -> None:
    provider = _FakeLLM(_GOOD)
    extractor = LLMExtractor(provider)

    extractor.extract("Search Quality capabilities: Query Expansion | improves recall.")

    system = provider.last_messages[0].content
    assert "Completeness for named table/list rows" in system
    assert "current topic" in system
    assert "A topology edge alone does NOT satisfy this row-local fact requirement" in system
    assert "use parent topic -> row label `includes` only" in system
    assert "explicit collection/list topic" in system
    assert "LITERAL claims about the row label" in system
    assert "For EVERY extracted table/list row label" in system
    assert "that row's own non-title cells" in system
    assert "If there is no row-local literal claim" in system
    assert "incidental entity" in system
    assert "linked file title" in system
    assert "Do NOT infer `includes` from a mapping table" in system
    assert "Completeness for labeled diagrams/arrows" in system
    assert "Do NOT rewrite all arrow chains to `breaks_into`" in system
    assert '"type":"includes","src":"Search Quality","dst":"Query Expansion"' in system
    assert '"title":"Context Map"' in system
    assert '"predicate":"describes","object":"agent handoff protocol"' in system
    assert '"title":"One-Time Test Suite"' in system
    assert '"predicate":"recommended_approach","object":"Continuous Integration"' in system
    assert "Definition of Done" not in system


def test_llm_extractor_prompts_for_directional_correspondence() -> None:
    provider = _FakeLLM(_GOOD)
    extractor = LLMExtractor(provider)

    extractor.extract("Ava wrote to Nikhil in a 1963 letter.")

    system = provider.last_messages[0].content
    assert "Completeness for correspondence and addressed messages" in system
    assert "A wrote to B" in system
    assert "`wrote_to`" in system
    assert "`addressed_to`" in system
    assert "`sent_to`" in system
    assert "`correspondent_of`" in system
    assert "Do NOT use `wrote_to` for authorship" in system
    assert "`author_of` or `authored_by`" in system
    assert '"type":"wrote_to","src":"Ava","dst":"Nikhil"' in system


def test_llm_extractor_prompts_for_cited_work_contributor_liveness() -> None:
    provider = _FakeLLM(_GOOD)
    extractor = LLMExtractor(provider)

    extractor.extract(
        "River Atlas: A Descriptive Bibliography, by Mira Chen, "
        "with the assistance of Noel Park (1993)."
    )

    system = provider.last_messages[0].content
    assert "Completeness for cited works and contributors" in system
    assert "extract the work as an InformationObject" in system
    assert "only if you also emit grounded topology relations" in system
    assert "`author_of`, `editor_of`, `compiler_of`" in system
    assert "`contributor_to`, or `assisted_with`" in system
    assert "do not extract contributor Agents solely from that citation" in system
    assert '"title":"River Atlas: A Descriptive Bibliography"' in system
    assert '"type":"author_of","src":"Mira Chen"' in system
    assert '"type":"contributor_to","src":"Noel Park"' in system


def test_llm_extractor_prompts_for_named_participants_inside_literal_facts() -> None:
    provider = _FakeLLM(_GOOD)
    extractor = LLMExtractor(provider)

    extractor.extract("Morgan considered a partnership between Atlas and Beacon.")

    system = provider.last_messages[0].content
    assert "Completeness for named participants inside facts" in system
    assert "participants need graph liveness" in system
    assert "Do NOT extract a named participant as a node only" in system
    assert "`considered_relationship_with`" in system
    assert "`also_known_as`" in system
    assert '"type":"considered_relationship_with","src":"Atlas","dst":"Beacon"' in (system)


def test_llm_extractor_adds_sdlc_local_taxonomy_example_for_sdlc_pack() -> None:
    provider = _FakeLLM(_GOOD)
    extractor = LLMExtractor(provider, packs=["sdlc"])

    extractor.extract("The decomposition chain is Epics -> Stories -> Tasks.")

    system = provider.last_messages[0].content
    assert "Software-delivery / SDLC pack guidance" in system
    assert "`Epics -> Stories -> Tasks`" in system
    assert "`breaks_into` relations" in system


def test_llm_extractor_adds_sdlc_requirements_stack_guidance() -> None:
    provider = _FakeLLM(_GOOD)
    extractor = LLMExtractor(provider, packs=["sdlc"])

    extractor.extract("Functional Requirements ↓ constrained by Non-Functional Requirements")

    system = provider.last_messages[0].content
    assert "requirements-stack diagrams" in system
    assert "Functional Requirement breaks_into Non-Functional Requirement" in system
    assert "Non-Functional Requirement constrains Functional Requirement" in system
    assert 'subject="Non-Functional Requirement", predicate="defines"' in system
    assert "tested_via`/`validated_by" in system


def test_llm_extractor_adds_sdlc_task_dor_dod_requires_guidance() -> None:
    provider = _FakeLLM(_GOOD)
    extractor = LLMExtractor(provider, packs=["sdlc"])

    extractor.extract("Tasks each have Definition of Ready / Definition of Done.")

    system = provider.last_messages[0].content
    assert "`Task requires Definition of Ready`" in system
    assert "`Task requires Definition of Done`" in system
    assert "do not use `includes`" in system


def test_llm_extractor_prompts_for_complete_comparative_literals() -> None:
    provider = _FakeLLM(_GOOD)
    extractor = LLMExtractor(provider)

    extractor.extract("The loop time drops from hours to minutes.")

    system = provider.last_messages[0].content
    assert "review time drops from hours to minutes" in system
    assert "Keep the full comparison in ONE literal" in system
    assert "do NOT split it into separate `from=hours` and `to=minutes` claims" in (system)


def test_llm_extractor_adds_sdlc_comparative_example_for_sdlc_pack() -> None:
    provider = _FakeLLM(_GOOD)
    extractor = LLMExtractor(provider, packs=["sdlc"])

    extractor.extract("Tooling Integration drops loop time from hours to minutes.")

    system = provider.last_messages[0].content
    assert "Tooling Integration" in system
    assert "loop time drops from hours to minutes" in system


def test_llm_extractor_empty_text_short_circuits() -> None:
    extractor = LLMExtractor(_FakeLLM(_GOOD))
    assert extractor.extract("   ") == ExtractionResult()


def _live_model_reachable() -> bool:
    if not _LIVE_API_BASE:
        return False
    try:
        httpx.get(f"{_LIVE_API_BASE.rstrip('/')}/models", timeout=2.0).raise_for_status()
        return True
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(
    not _LIVE_API_BASE,
    reason="OKTO_NEURON_LLM_BASE_URL is required for realmodel tests",
)
@pytest.mark.skipif(not _live_model_reachable(), reason="configured live model is unreachable")
@pytest.mark.realmodel
@pytest.mark.slow
def test_live_extraction_against_configured_model() -> None:
    from okto_neuron.config._vault import LLMConfig, LLMDefaults
    from okto_neuron.llm import LLMProviderError, LiteLLMProvider

    # Use the owner-selected capable model. A loaded server can still reject a
    # completion under memory pressure, in which case this opportunistic test skips.
    resolved = LLMConfig(
        allow_remote=True,
        defaults=LLMDefaults(provider="openai", api_base=_LIVE_API_BASE, model=_LIVE_MODEL),
    ).resolved("extraction")
    provider = LiteLLMProvider(resolved)
    extractor = LLMExtractor(provider)  # default token headroom for reasoning models
    try:
        result = extractor.extract(
            "Evan Gaur is the CDTO at ExampleCorp. He approved the POC budget on 2026-03-19."
        )
    except LLMProviderError as exc:  # server memory / load failure — opportunistic test
        pytest.skip(f"configured model could not serve a completion: {exc}")
    assert isinstance(result, ExtractionResult)
    # a capable model should extract at least one node on the spine
    assert len(result.node_candidates) >= 1
    assert all(n.type in ALLOWED_NODE_TYPES for n in result.node_candidates)
