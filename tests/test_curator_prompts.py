from okto_neuron.predicates import builtin_predicate_records, render_registry_block

from okto_neuron.curator import (
    CURATOR_SYSTEM,
    RELATION_CURATOR_RESPONSE_FORMAT,
    RELATION_CURATOR_SYSTEM,
    candidate_curator_system,
    effective_relation_curator_system,
    normalize_predicate,
    parse_relation_curator_verdict,
    relation_curator_system,
)


def _relation_reply(**updates: object) -> str:
    import json

    payload: dict[str, object] = {
        "action": "commit",
        "confidence": 0.91,
        "canonical_predicate": "lives_in",
        "predicate_definition": "The subject lives in the object place.",
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
        "reason": "directly stated",
    }
    payload.update(updates)
    return json.dumps(payload)


def test_relation_curator_schema_requires_structured_evidence() -> None:
    schema = RELATION_CURATOR_RESPONSE_FORMAT["json_schema"]["schema"]

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["properties"]["predicate_direction"]["enum"] == [
        "subject_to_object",
        "symmetric",
        "unknown",
    ]


def test_custom_relation_prompt_cannot_downgrade_structured_response_contract() -> None:
    prompt = effective_relation_curator_system(
        'Judge carefully. Reply with {"action":"commit","reason":"ok"}.'
    )

    assert prompt.startswith("Judge carefully.")
    assert "ignore any earlier output-shape instruction" in prompt
    assert "predicate_definition" in prompt
    assert "unsupported_inference" in prompt
    assert "Every field is required" in prompt


def test_relation_curator_parser_preserves_complete_structured_evidence() -> None:
    verdict = parse_relation_curator_verdict(_relation_reply())

    assert verdict.action == "commit"
    assert verdict.canonical_predicate == "lives_in"
    assert verdict.predicate_definition.startswith("The subject")
    assert verdict.predicate_direction == "subject_to_object"
    assert verdict.subject_supported is True
    assert verdict.direction_supported is True
    assert verdict.unsupported_inference is False
    assert verdict.useful is True


def test_relation_curator_parser_accepts_json_markdown_fence() -> None:
    raw = _relation_reply()

    verdict = parse_relation_curator_verdict(f"```json\n{raw}\n```")

    assert verdict.action == "commit"
    assert verdict.canonical_predicate == "lives_in"
    assert verdict.direction_supported is True


def test_relation_curator_parser_fails_closed_on_missing_or_malformed_evidence() -> None:
    missing = _relation_reply(predicate_supported=None)
    multiline_definition = _relation_reply(
        predicate_definition="The subject lives in\nthe object place."
    )
    invalid_inverse = _relation_reply(
        predicate_direction="symmetric",
        inverse_direction_required=True,
    )

    assert parse_relation_curator_verdict(missing).action == "abstain"
    assert parse_relation_curator_verdict(multiline_definition).action == "abstain"
    assert parse_relation_curator_verdict(invalid_inverse).action == "abstain"


def test_relation_curator_parser_rejects_non_schema_scalar_types() -> None:
    assert parse_relation_curator_verdict(_relation_reply(confidence=True)).action == ("abstain")
    assert parse_relation_curator_verdict(_relation_reply(confidence=float("nan"))).action == (
        "abstain"
    )
    assert (
        parse_relation_curator_verdict(_relation_reply(predicate_definition=7)).action == "abstain"
    )
    assert parse_relation_curator_verdict(_relation_reply(reason={"text": "ok"})).action == (
        "abstain"
    )
    assert parse_relation_curator_verdict(_relation_reply(extra="not in schema")).action == (
        "abstain"
    )


def test_relation_curator_parser_requires_commit_definition_and_predicate() -> None:
    assert parse_relation_curator_verdict(_relation_reply(canonical_predicate="")).action == (
        "abstain"
    )
    assert parse_relation_curator_verdict(_relation_reply(predicate_definition="")).action == (
        "abstain"
    )


def test_relation_curator_parser_allows_empty_queue_semantics() -> None:
    verdict = parse_relation_curator_verdict(
        _relation_reply(
            action="queue",
            canonical_predicate="",
            predicate_definition="",
            predicate_direction="unknown",
            subject_supported=False,
            predicate_supported=False,
            object_supported=False,
            direction_supported=False,
            useful=False,
        )
    )

    assert verdict.action == "queue"
    assert verdict.canonical_predicate == ""
    assert verdict.predicate_direction == "unknown"


def test_default_candidate_curator_does_not_include_sdlc_specific_guidance() -> None:
    assert "Alistair Cockburn User Story Format" not in CURATOR_SYSTEM
    assert "Definition of Ready" not in CURATOR_SYSTEM
    assert "Definition of Done" not in CURATOR_SYSTEM


def test_sdlc_candidate_curator_accepts_grounded_named_methodology_roles() -> None:
    system = candidate_curator_system(["sdlc"])

    assert "Alistair Cockburn User Story Format" in system
    assert "commit that methodology Concept" in system
    assert "do not require a full external definition" in system


def test_sdlc_candidate_curator_accepts_grounded_practice_gate_terms() -> None:
    system = candidate_curator_system(["sdlc"])

    assert "Definition of Ready" in system
    assert "Definition of Done" in system
    assert "start/end conditions for a task" in system
    assert "do not queue them merely because they are established Agile/Scrum terms" in (system)


def test_candidate_curator_accepts_grounded_local_taxonomy_terms_generically() -> None:
    assert "Local taxonomy terms" in CURATOR_SYSTEM
    assert "do not queue them merely because the title is generic" in CURATOR_SYSTEM
    assert "Do not collapse a named level or method into a parenthetical transition" in (
        CURATOR_SYSTEM
    )
    assert "Checklist Verification" in CURATOR_SYSTEM


def test_candidate_curator_accepts_concrete_cited_works_generically() -> None:
    assert "Concrete named works are useful InformationObject nodes" in CURATOR_SYSTEM
    assert "authorship, editorship, compilation, contribution" in CURATOR_SYSTEM
    assert "bibliographic or acknowledgement context" in CURATOR_SYSTEM
    assert "proposed Agent relations depend on that work being live" in CURATOR_SYSTEM


def test_sdlc_candidate_curator_names_sdlc_taxonomy_terms() -> None:
    system = candidate_curator_system(["sdlc"])

    assert "Epic" in system
    assert "Story" in system
    assert "Task" in system
    assert "decomposition chain" in system


def test_relation_curator_prompt_names_canonical_predicate_aliases() -> None:
    """The eight collapse rules and every behavioural rule stay in the SYSTEM prompt.

    ADR 0040 D6a moved the vocabulary itself out: it is vault state, rendered
    into the user prompt from the live registry. The collapse rules stay because
    all eight resolve in ``_CORE_PREDICATE_ALIASES`` and are applied by
    ``normalize_predicate`` regardless of what the model returns, so they are
    stable policy and belong in the fingerprinted prompt.
    """

    assert "uses_analogy -> example" in RELATION_CURATOR_SYSTEM
    assert "advances_to/next_level/followed_by -> progresses_to" in (RELATION_CURATOR_SYSTEM)
    assert "failure_mode/red_flag -> risk" not in RELATION_CURATOR_SYSTEM
    assert "has -> includes" not in RELATION_CURATOR_SYSTEM
    assert "has_condition/has_criterion -> requires" in RELATION_CURATOR_SYSTEM
    assert "includes_mechanism/includes_strategy/includes_phase -> includes" in (
        RELATION_CURATOR_SYSTEM
    )
    assert "requires_competency -> requires" in RELATION_CURATOR_SYSTEM
    assert "defined_as -> defines" in RELATION_CURATOR_SYSTEM
    assert "described_as/is_described_as -> describes" in RELATION_CURATOR_SYSTEM
    assert "contrasted_with -> contrasts_with" in RELATION_CURATOR_SYSTEM
    # Behavioural rules — unchanged.
    assert "wrote_to" in RELATION_CURATOR_SYSTEM
    assert "addressed_to" in RELATION_CURATOR_SYSTEM
    assert "author_of" in RELATION_CURATOR_SYSTEM
    assert "authored_by" in RELATION_CURATOR_SYSTEM
    assert "contributor_to" in RELATION_CURATOR_SYSTEM
    assert "assisted_with" in RELATION_CURATOR_SYSTEM
    assert "never `wrote_to`" in RELATION_CURATOR_SYSTEM
    assert "explicitly says A wrote/sent/addressed" in RELATION_CURATOR_SYSTEM
    assert "named participant relations" in RELATION_CURATOR_SYSTEM
    assert "partnership, romance, alliance" in RELATION_CURATOR_SYSTEM
    # The frozen enumerated vocabulary is GONE: 26 of its 49 labels were seeded
    # in no registry, so the prompt itself manufactured the mint loop.
    for advertised_only_label in (
        "constrains, maps_to",
        "editor_of, edited_by",
        "compiler_of, compiled_by",
        "also_known_as",
        "considered_relationship_with",
        "recommended_rule",
        "setting_effect",
    ):
        assert advertised_only_label not in RELATION_CURATOR_SYSTEM
    assert "REUSE the label of a listed predicate whose DEFINITION matches" in (
        RELATION_CURATOR_SYSTEM
    )


def test_relation_curator_vocabulary_now_comes_from_the_rendered_registry() -> None:
    """The label list the system prompt used to freeze is now live vault state."""

    records = {
        record.label: record
        for record in builtin_predicate_records()
        if record.label in {"constrains", "editor_of", "edited_by", "compiler_of", "has_value"}
    }
    block = render_registry_block(records)

    for label in ("constrains", "editor_of", "edited_by", "compiler_of", "has_value"):
        assert f"{label} | subject_to_object | support=0 | " in block
        assert label not in RELATION_CURATOR_SYSTEM.split("Collapse local variants")[0]


def test_relation_curator_rewrites_ordered_level_edges_to_progression() -> None:
    assert "ordered levels, maturity ladders, capability spectra" in (RELATION_CURATOR_SYSTEM)
    assert "commit adjacent level-to-level relations as `progresses_to`" in (
        RELATION_CURATOR_SYSTEM
    )
    assert "proposed predicate is `breaks_into`" in RELATION_CURATOR_SYSTEM
    assert "canonical_predicate `progresses_to`" in RELATION_CURATOR_SYSTEM
    assert "true part/whole decomposition" in RELATION_CURATOR_SYSTEM


def test_relation_curator_accepts_named_method_table_mappings() -> None:
    assert "table rows that map a current topic or row label to a named method" in (
        RELATION_CURATOR_SYSTEM
    )
    assert "`uses`, `underpins`, or `maps_to`" in RELATION_CURATOR_SYSTEM
    assert "row-local role" in RELATION_CURATOR_SYSTEM


def test_relation_curator_accepts_cited_work_contributor_relations() -> None:
    assert "For cited works" in RELATION_CURATOR_SYSTEM
    assert "authorship, editorship, compilation, or contributor relations" in (
        RELATION_CURATOR_SYSTEM
    )
    assert "with assistance of" in RELATION_CURATOR_SYSTEM
    assert "commit with a precise canonical_predicate" in RELATION_CURATOR_SYSTEM
    assert "`contributor_to` or `assisted_with`" in RELATION_CURATOR_SYSTEM


def test_sdlc_relation_curator_prompt_names_sdlc_predicate_aliases() -> None:
    system = relation_curator_system(["sdlc"])

    assert "has -> includes" in system
    assert "tested_via -> validated_by" in system
    assert "failure_mode/fails_because/" in system
    assert "reduces_loop_time_from/" in system
    assert "canonical_predicate `requires`" in system
    assert "Definition of Ready or Definition of Done" in system
    assert "do not queue solely because `includes` is imprecise" in system


def test_relation_curator_prompt_queues_fragmentary_comparative_literals() -> None:
    assert "complete comparison" in RELATION_CURATOR_SYSTEM
    assert "drops from hours to minutes" in RELATION_CURATOR_SYSTEM
    assert "queue fragmentary one-sided literals" in RELATION_CURATOR_SYSTEM
    assert "only `hours` or only `minutes`" in RELATION_CURATOR_SYSTEM


def test_relation_predicate_aliases_keep_literary_has_generic_by_default() -> None:
    assert normalize_predicate("has") == "has"
    assert normalize_predicate("failure_mode") == "failure_mode"
    assert normalize_predicate("reduces loop time from") == "reduces_loop_time_from"
    assert normalize_predicate("advances to") == "progresses_to"
    assert normalize_predicate("next level") == "progresses_to"
    assert normalize_predicate("followed by") == "progresses_to"
    assert normalize_predicate("wrote letter to") == "wrote_to"
    assert normalize_predicate("sent to") == "wrote_to"
    assert normalize_predicate("written by") == "authored_by"
    assert normalize_predicate("authored") == "author_of"
    assert normalize_predicate("edited") == "editor_of"
    assert normalize_predicate("compiled") == "compiler_of"
    assert normalize_predicate("with assistance of") == "contributor_to"
    assert normalize_predicate("assisted by") == "contributor_to"


def test_predicate_normalization_rejects_non_ascii_without_lossy_collisions() -> None:
    assert normalize_predicate("écrit_par") == ""
    assert normalize_predicate("crit_par") == "crit_par"
    assert normalize_predicate("ｕｓｅｓ") == "uses"


def test_vault_local_predicate_aliases_do_not_override_core_aliases() -> None:
    aliases = {
        "shared_link_to": "shared_link",
        "letter_to": "communicates_with",
        "wrote_to": "communicates_with",
    }

    assert normalize_predicate("shared link to", predicate_aliases=aliases) == "shared_link"
    assert normalize_predicate("letter to", predicate_aliases=aliases) == "wrote_to"
    assert normalize_predicate("wrote to", predicate_aliases=aliases) == "communicates_with"


def test_relation_predicate_aliases_canonicalize_observed_cop_variants_for_sdlc_pack() -> None:
    cases = {
        "uses_analogy": "example",
        "is_analogy": "example",
        "constraint": "constrains",
        "failure_mode": "risk",
        "fails_because": "risk",
        "red_flag": "risk",
        "recommended_view": "recommended_approach",
        "benefit": "enables",
        "tested_via": "validated_by",
        "verified_by": "validated_by",
        "validated_via": "validated_by",
        "has": "includes",
        "has_condition": "requires",
        "has_criterion": "requires",
        "characterized_by": "describes",
        "reduces loop time from": "impact",
        "identifies_danger": "risk",
    }
    for raw, expected in cases.items():
        assert normalize_predicate(raw, packs=["sdlc"]) == expected


# ── ADR 0040 D6a: the live registry rides in the relation USER prompt ─────────


def _registry_block_fixture() -> str:
    records = {
        record.label: record
        for record in builtin_predicate_records()
        if record.label in {"includes", "has_value", "uses"}
    }
    return render_registry_block(records)


class _StubBlockStore:
    """Minimal GraphStore surface `_build_relation_prompt` actually touches."""

    def __init__(self, block_id: str, text: str) -> None:
        self._block_id = block_id
        self._text = text

    def get_node(self, node_id: str):
        from types import SimpleNamespace

        if node_id != self._block_id:
            return None
        return SimpleNamespace(id=node_id, content=self._text, type="Block", title="")


def _relation_prompt(excerpt: str, registry_block: str) -> str:
    from okto_neuron.consolidate import EdgeCandidate
    from okto_neuron.curator import _build_relation_prompt

    candidate = EdgeCandidate(
        type="engloba",
        src_ref="a" * 64,
        dst_ref="b" * 64,
        block_id="c" * 64,
    )
    return _build_relation_prompt(
        candidate,
        store=_StubBlockStore("c" * 64, excerpt),
        node_candidates={},
        registry_block=registry_block,
    )


def test_relation_prompt_places_registry_block_inside_the_shared_prefix() -> None:
    from okto_neuron.curator_batch import relation_shared_prefix

    excerpt = "Widget service is currently active."
    block = _registry_block_fixture()
    prompt = _relation_prompt(excerpt, block)

    assert prompt.startswith(relation_shared_prefix(excerpt, block))
    assert prompt.index("Source excerpt:") < prompt.index("Known predicates")
    assert prompt.index("Known predicates") < prompt.index("Relationship kind:")


def test_relation_prompt_without_a_registry_block_is_byte_identical_to_before() -> None:
    """An empty registry must not change a single byte of the historic prompt."""

    from okto_neuron.curator_batch import shared_excerpt_section

    excerpt = "Widget service is currently active."
    prompt = _relation_prompt(excerpt, "")

    assert "Known predicates" not in prompt
    assert prompt.startswith(shared_excerpt_section(excerpt))


def test_batch_prompt_emits_exactly_one_excerpt_copy_with_registry_block() -> None:
    """curator_batch's prefix strip fails SILENTLY on a mismatch.

    With the registry block outside the stripped prefix the batch pays K copies
    of both the excerpt and the whole registry; the prompt still "works", which
    is exactly why this needs a pinned test rather than a reviewer's eye.
    """

    from okto_neuron.curator_batch import build_batch_user_prompt, relation_shared_prefix

    # Long enough to dominate the prompt, short enough to survive
    # `_edge_source_excerpt`'s truncation unchanged — the batch caller passes
    # the ALREADY-truncated block excerpt, so the two must agree byte for byte.
    excerpt = ("Widget service is currently active. " * 40).strip()
    block = _registry_block_fixture()
    sentinel = block.splitlines()[0]
    members = [(f"cand-{index}", _relation_prompt(excerpt, block)) for index in range(4)]

    prompt = build_batch_user_prompt(
        excerpt,
        members,
        prefix=relation_shared_prefix(excerpt, block),
    )

    assert prompt.count("Source excerpt:") == 1
    assert prompt.count(sentinel) == 1
    assert prompt.count("Known predicates (reuse by definition):") == 1


def test_batch_prompt_without_the_relation_prefix_repeats_the_registry_block() -> None:
    """The negative half: this is the landmine the parameter exists to defuse."""

    from okto_neuron.curator_batch import build_batch_user_prompt

    excerpt = "Widget service is currently active."
    block = _registry_block_fixture()
    sentinel = block.splitlines()[0]
    members = [(f"cand-{index}", _relation_prompt(excerpt, block)) for index in range(4)]

    prompt = build_batch_user_prompt(excerpt, members)

    assert prompt.count(sentinel) == 4
    # The excerpt itself is still stripped (it is a prefix either way); the
    # registry block is what gets paid K times.
    assert prompt.count("Source excerpt:") == 1
