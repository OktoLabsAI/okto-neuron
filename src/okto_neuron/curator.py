"""LLM-backed candidate curation before the confidence gate."""

from __future__ import annotations

import json
import math
import re
import time
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal, Mapping

from okto_neuron.llm import (
    LLMProviderError,
    Message,
    ResponseFormat,
    complete_with_retry,
    last_call_stats,
)
from okto_neuron.llm._structured_output import extract_json_object
from okto_neuron.resolve import ResolveOutcome

if TYPE_CHECKING:
    from okto_neuron.consolidate import EdgeCandidate, NodeCandidate
    from okto_neuron.llm import LLMProvider
    from okto_neuron.store.protocol import GraphStore

CuratorAction = Literal["commit", "queue", "abstain"]
RelationDirection = Literal["subject_to_object", "symmetric", "unknown"]


def _pack_set(packs: Iterable[str] | None = None) -> set[str]:
    return {str(pack).strip().casefold() for pack in packs or () if str(pack).strip()}


_BASE_CURATOR_SYSTEM = (
    "You are Marginalia's candidate curator. You review ONE extracted knowledge-graph "
    "node candidate before it may be committed.\n"
    "Commit only when the candidate is grounded in the source excerpt, has a stable "
    "canonical title, is useful as graph knowledge, and is distinct from any similar "
    "existing target shown to you.\n"
    "Local taxonomy terms are useful Concept nodes when the excerpt defines them "
    "as named levels, roles, or units in a hierarchy or sequence; do not queue "
    "them merely because the title is generic outside that local taxonomy.\n"
    "Do not collapse a named level or method into a parenthetical transition "
    "state. If an excerpt defines `Level 1: Checklist Verification (Ad-Hoc -> "
    "Structured)`, the candidate `Checklist Verification` is distinct from the "
    "state labels `Ad-Hoc` and `Structured`; shared words in the transition are "
    "not enough to mark it as a duplicate.\n"
    "Queue when the candidate is probably a duplicate, only topically related but "
    "ambiguous, too generic, merely document structure, a path/list/slide label with "
    "no durable knowledge value, wrongly typed, or insufficiently grounded.\n"
    "Queue cross-reference-only targets: a node mentioned only in a "
    "Cross-References/Related/See also section is navigation metadata, not grounded "
    "knowledge about that target. Titles like 'Pillar N: X' extracted from another "
    "pillar's cross-reference must queue unless the excerpt substantively defines "
    "that pillar as the current topic.\n"
    "A similar correlation is not automatically a duplicate: commit distinct useful "
    "subtopics, queue true duplicates or unclear variants. You do not write to the "
    "graph; you only return a gate recommendation.\n"
    "Concrete named works are useful InformationObject nodes when the excerpt "
    "cites or names a stable title and also grounds authorship, editorship, "
    "compilation, contribution, publication, or edition facts. Do not queue a "
    "book, article, bibliography, report, dataset, or edition solely because it "
    "appears in bibliographic or acknowledgement context, especially when "
    "proposed Agent relations depend on that work being live.\n"
    "Reply with ONLY JSON: "
    '{"action":"commit|queue","confidence":<0..1>,"reason":"<short grounded reason>"}.'
)

_SDLC_CURATOR_GUIDANCE = (
    "\n"
    "Software-delivery / SDLC pack guidance:\n"
    "- Named methods, formats, frameworks, standards, and techniques can be useful "
    "nodes even when the excerpt gives only their role in the current topic. If a "
    "table or sentence says a current concept uses a named methodology such as "
    "'Alistair Cockburn User Story Format', commit that methodology Concept when "
    "the title is stable and the role is grounded; do not require a full external "
    "definition in the excerpt.\n"
    "- Stable practice terms, quality gates, and named criteria are also useful "
    "Concept nodes when the excerpt grounds their operational role in the current "
    "workflow. Commit 'Definition of Ready' and 'Definition of Done' when the "
    "source describes them as start/end conditions for a task or shows their "
    "checklist shape; do not queue them merely because they are established "
    "Agile/Scrum terms with broader external definitions.\n"
    "- Commit grounded terms such as 'Epic', 'Story', and 'Task' when the source "
    "defines their role in a decomposition chain and relationships depend on them."
)


def candidate_curator_system(packs: Iterable[str] | None = None) -> str:
    prompt = _BASE_CURATOR_SYSTEM
    if "sdlc" in _pack_set(packs):
        prompt += _SDLC_CURATOR_GUIDANCE
    return prompt


CURATOR_SYSTEM = candidate_curator_system()

CURATOR_RESPONSE_FORMAT: ResponseFormat = {
    "type": "json_schema",
    "json_schema": {
        "name": "marginalia_candidate_curator",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["commit", "queue"]},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "reason": {"type": "string"},
            },
            "required": ["action", "confidence", "reason"],
            "additionalProperties": False,
        },
    },
}

_BASE_RELATION_CURATOR_SYSTEM = (
    "You are Marginalia's relationship curator. You review ONE proposed "
    "knowledge-graph relationship before it may be written.\n"
    "Commit only when the source excerpt directly supports the subject, predicate, "
    "and object/literal, and the predicate accurately describes the relation.\n"
    "Queue when the relation is unsupported, over-inferred, uses a generic or "
    "misleading predicate, connects the wrong endpoints, smuggles in sibling "
    "document context, or turns document structure into graph knowledge.\n"
    "Queue cross-reference-only relations: links in Cross-References/Related/See "
    "also sections are navigation metadata, not semantic graph facts, unless the "
    "same excerpt also provides substantive explanatory content for the subject, "
    "predicate, and object beyond the link label.\n"
    "For literal claims, the literal object must be stated or tightly paraphrased "
    "by the excerpt. For topology edges, both endpoint nodes must be grounded and "
    "the excerpt must relate them. You do not write to the graph; you only return "
    "a gate recommendation.\n"
    "For comparative literal claims, require the complete comparison in the "
    "literal. If the source says something drops from hours to minutes, commit "
    "only a single literal like `loop time drops from hours to minutes`; queue "
    "fragmentary one-sided literals such as only `hours` or only `minutes`.\n"
    "For ordered levels, maturity ladders, capability spectra, and sequential "
    "stage lists, commit adjacent level-to-level relations as `progresses_to` "
    "when the excerpt directly shows the order. If the proposed predicate is "
    "`breaks_into` but the excerpt shows progression rather than decomposition, "
    "commit with canonical_predicate `progresses_to` instead of queueing solely "
    "because the raw predicate was imprecise. Keep `breaks_into` only for true "
    "part/whole decomposition.\n"
    "For table rows that map a current topic or row label to a named method, "
    "standard, framework, or acronym, commit a grounded topology relation such "
    "as `uses`, `underpins`, or `maps_to` when the excerpt directly states the "
    "mapping. Queue only when the method/acronym is merely co-mentioned without "
    "a row-local role.\n"
    "For cited works, commit grounded authorship, editorship, compilation, or "
    "contributor relations when the excerpt says by, written by, edited by, "
    "compiled by, with assistance of, or with contributions from. If the raw "
    "predicate overstates the role, commit with a precise canonical_predicate "
    "such as `contributor_to` or `assisted_with` rather than queueing solely "
    "because `author_of` was too strong.\n"
    # No enumerated vocabulary lives here any more. The frozen ~50-label list
    # this replaced advertised 26 labels that were seeded in no registry, so the
    # prompt itself manufactured the `queue_unregistered` mint loop. The live
    # registry is injected into the USER prompt instead (see
    # `_build_relation_prompt`'s `registry_block`): it is vault state, and the
    # system prompt is hashed into `config_fingerprint`. The collapse rules
    # below stay here because all eight are code-enforced by
    # `normalize_predicate` regardless of model output — stable policy belongs
    # in the fingerprinted prompt.
    "For commit decisions, also return `canonical_predicate`: a concise lower_snake_case "
    "predicate for graph storage. The user message lists the predicates this vault "
    "already uses, each with its one-line definition. REUSE the label of a listed "
    "predicate whose DEFINITION matches the relation you are committing, even when "
    "the source words it differently or in another language; the definition is the "
    "identity, the label is not. Coin a new precise lower_snake_case predicate from "
    "the source text ONLY when no listed definition fits. Collapse local variants "
    "into canonical "
    "forms: includes_mechanism/includes_strategy/includes_phase -> includes; "
    "has_condition/has_criterion -> requires; "
    "requires_competency -> requires; defined_as -> defines; described_as/"
    "is_described_as -> describes; contrasted_with -> contrasts_with; "
    "advances_to/next_level/followed_by -> progresses_to; "
    "uses_analogy -> example. For queue decisions, use an empty string.\n"
    "Use `wrote_to`/`addressed_to` only for letters, emails, messages, or other "
    "addressed communications with named recipients. Title-page or byline "
    "authorship of a book/article/report is `author_of` or `authored_by`, never "
    "`wrote_to`. For direct correspondence relations, commit when the excerpt "
    "explicitly says A wrote/sent/addressed a letter or message to B and the "
    "endpoints match, even if the letter is contextual background; queue only "
    "when the sender, recipient, or addressed-message predicate is unsupported.\n"
    "For named participant relations embedded inside literal ideas, commit when "
    "the excerpt directly states a proposed/considered partnership, romance, "
    "alliance, alias, or other relationship between the endpoints. Hypothetical "
    "or considered relationships can still be grounded; queue only when the text "
    "merely co-mentions the endpoints without a relationship phrase.\n"
    "Return a one-line `predicate_definition` and `predicate_direction` for the "
    "canonical label. Set `inverse_direction_required` only when the source supports "
    "the opposite endpoint direction; Marginalia will still require a confirmed inverse "
    "mapping before applying it. Independently report whether the excerpt supports the "
    "subject, predicate, object, and direction; whether the relation relies on unsupported "
    "inference, is structural noise, is redundant, and is useful. These are evidence fields, "
    "not permission to bypass application policy.\n"
    "Reply with ONLY JSON: "
    '{"action":"commit|queue","confidence":<0..1>,'
    '"canonical_predicate":"<lower_snake_case or empty>",'
    '"predicate_definition":"<one line or empty>",'
    '"predicate_direction":"subject_to_object|symmetric|unknown",'
    '"inverse_direction_required":<true|false>,'
    '"subject_supported":<true|false>,"predicate_supported":<true|false>,'
    '"object_supported":<true|false>,"direction_supported":<true|false>,'
    '"unsupported_inference":<true|false>,"structural_noise":<true|false>,'
    '"redundant":<true|false>,"useful":<true|false>,'
    '"reason":"<short grounded reason>"}.'
)

_SDLC_RELATION_CURATOR_GUIDANCE = (
    "\n"
    "Software-delivery / SDLC pack predicate aliases: has -> includes; "
    "tested_via -> validated_by; implements/provides -> includes; "
    "failure_mode/fails_because/"
    "red_flag/identifies_danger -> risk; reduces_loop_time_from/"
    "reduces_loop_time -> impact; fix/counteracts/compensates_for -> mitigates. "
    "When a task has/needs Definition of Ready or Definition of Done, commit "
    "the relation with canonical_predicate `requires` even if the raw candidate "
    "predicate is `has` or `includes`; do not queue solely because `includes` is "
    "imprecise for a task condition."
)


def relation_curator_system(packs: Iterable[str] | None = None) -> str:
    prompt = _BASE_RELATION_CURATOR_SYSTEM
    if "sdlc" in _pack_set(packs):
        prompt += _SDLC_RELATION_CURATOR_GUIDANCE
    return prompt


_CUSTOM_RELATION_RESPONSE_CONTRACT = (
    "\n\nMarginalia structured response contract (authoritative): ignore any earlier "
    "output-shape instruction in custom guidance. Return ONLY one JSON object with "
    "action, confidence, canonical_predicate, predicate_definition, "
    "predicate_direction, inverse_direction_required, subject_supported, "
    "predicate_supported, object_supported, direction_supported, "
    "unsupported_inference, structural_noise, redundant, useful, and reason. "
    "Every field is required and must match the response schema supplied with the request."
)


def effective_relation_curator_system(
    custom_prompt: str | None,
    packs: Iterable[str] | None = None,
) -> str:
    """Return the effective prompt with an application-owned response contract.

    Custom guidance may change semantic instructions, but it cannot silently
    downgrade the structured D5-D7 response expected by the parser and replay.
    """

    if not custom_prompt:
        return relation_curator_system(packs)
    return custom_prompt.rstrip() + _CUSTOM_RELATION_RESPONSE_CONTRACT


RELATION_CURATOR_SYSTEM = relation_curator_system()
RELATION_CURATOR_EVIDENCE_VERSION = "relation_curator_evidence.v2"

RELATION_CURATOR_RESPONSE_FORMAT: ResponseFormat = {
    "type": "json_schema",
    "json_schema": {
        "name": "marginalia_relation_curator",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["commit", "queue"]},
                "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "canonical_predicate": {"type": "string"},
                "predicate_definition": {"type": "string"},
                "predicate_direction": {
                    "type": "string",
                    "enum": ["subject_to_object", "symmetric", "unknown"],
                },
                "inverse_direction_required": {"type": "boolean"},
                "subject_supported": {"type": "boolean"},
                "predicate_supported": {"type": "boolean"},
                "object_supported": {"type": "boolean"},
                "direction_supported": {"type": "boolean"},
                "unsupported_inference": {"type": "boolean"},
                "structural_noise": {"type": "boolean"},
                "redundant": {"type": "boolean"},
                "useful": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": [
                "action",
                "confidence",
                "canonical_predicate",
                "predicate_definition",
                "predicate_direction",
                "inverse_direction_required",
                "subject_supported",
                "predicate_supported",
                "object_supported",
                "direction_supported",
                "unsupported_inference",
                "structural_noise",
                "redundant",
                "useful",
                "reason",
            ],
            "additionalProperties": False,
        },
    },
}

_VERDICT_RE = re.compile(r"\{[^{}]*?\"action\"[^{}]*?\}", re.DOTALL | re.IGNORECASE)
# Must cover a FULL production extraction block (fixed ~12k-char windows,
# okto_neuron.ingest.markdown._WINDOW_BYTES). The previous 9000-char cap
# truncated the final ~3k chars of every 12k block, so any candidate anchored
# there (markdown table cells especially) drew a structurally-guaranteed
# "the source excerpt does not contain the literal" queue verdict — a false
# negative of the excerpt window, not of the model (task-13 E2/R5). 16000
# keeps a bound for pathological single-line oversized blocks while never
# truncating a standard window. The excerpt stays byte-identical across every
# candidate of the same block, so the prompt-prefix cache reuse is unchanged.
CURATION_SOURCE_EXCERPT_LIMIT = 16000


@dataclass(frozen=True)
class CuratorVerdict:
    action: CuratorAction
    confidence: float = 0.0
    reason: str = ""
    canonical_predicate: str = ""
    predicate_definition: str = ""
    predicate_direction: RelationDirection = "unknown"
    inverse_direction_required: bool = False
    subject_supported: bool = False
    predicate_supported: bool = False
    object_supported: bool = False
    direction_supported: bool = False
    unsupported_inference: bool = False
    structural_noise: bool = False
    redundant: bool = False
    useful: bool = False
    # Telemetry (ADR 0015 D3.3): wall-clock of the LLM call and provider-reported
    # token usage, persisted into the ledger comparison record by the caller.
    duration_s: float | None = None
    usage: dict[str, int] | None = None
    # ADR 0039 D5: each transient provider failure this call retried (see
    # ``okto_neuron.llm.complete_with_retry``), recorded with the verdict so the
    # ledger row shows a second attempt, including when it also failed.
    provider_retries: tuple[dict[str, object], ...] = ()


_PREDICATE_TOKEN_RE = re.compile(r"[^a-z0-9_]+")
_PREDICATE_UNDERSCORE_RE = re.compile(r"_+")
_PREDICATE_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
_CORE_PREDICATE_ALIASES: dict[str, str] = {
    "includes_mechanism": "includes",
    "includes_strategy": "includes",
    "includes_phase": "includes",
    "includes_stage": "includes",
    "includes_competency": "includes",
    "includes_principle": "includes",
    "has_phase": "includes",
    "has_condition": "requires",
    "has_criterion": "requires",
    "requires_competency": "requires",
    "defined_as": "defines",
    "is_defined_as": "defines",
    "defines_score": "defines",
    "defines_rule": "defines",
    "described_as": "describes",
    "describes_as": "describes",
    "is_described_as": "describes",
    "characterized_by": "describes",
    "characteristic": "describes",
    "recommended_view": "recommended_approach",
    "recommendation": "recommended_approach",
    "contrasted_with": "contrasts_with",
    "contrasts": "contrasts_with",
    "uses_methodology": "uses",
    "uses_framework": "uses",
    "uses_tool": "uses",
    "uses_workflow": "uses",
    "uses_mindset": "uses",
    "uses_analogy": "example",
    "is_analogy": "example",
    "analogy": "example",
    "advances_to": "progresses_to",
    "next_level": "progresses_to",
    "followed_by": "progresses_to",
    "letter_to": "wrote_to",
    "wrote_letter_to": "wrote_to",
    "sent_letter_to": "wrote_to",
    "sent_to": "wrote_to",
    "written_by": "authored_by",
    "authored": "author_of",
    "written": "author_of",
    "edited": "editor_of",
    "edited_by": "edited_by",
    "compiled": "compiler_of",
    "compiled_by": "compiled_by",
    "with_assistance_of": "contributor_to",
    "assisted_by": "contributor_to",
    "contributed_to": "contributor_to",
    "constraint": "constrains",
    "is_constraint": "constrains",
    "rates": "describes",
}
_SDLC_PREDICATE_ALIASES: dict[str, str] = {
    "has": "includes",
    "tested_via": "validated_by",
    "verified_by": "validated_by",
    "validated_via": "validated_by",
    "implements": "includes",
    "provides": "includes",
    "benefit": "enables",
    "failure_mode": "risk",
    "fails_because": "risk",
    "red_flag": "risk",
    "identifies_danger": "risk",
    "reduces_loop_time_from": "impact",
    "reduces_loop_time": "impact",
    "fix": "mitigates",
    "counteracts": "mitigates",
    "compensates_for": "mitigates",
}


def normalize_predicate(
    value: object,
    *,
    packs: Iterable[str] | None = None,
    predicate_aliases: dict[str, str] | None = None,
) -> str:
    """Return a conservative lower_snake_case predicate suggestion.

    Empty or malformed suggestions return ``""`` so callers can keep the
    original predicate instead of silently fabricating a new relation label.
    """

    raw = _predicate_label_candidate(value)
    if not raw:
        return ""
    aliases = dict(_CORE_PREDICATE_ALIASES)
    if "sdlc" in _pack_set(packs):
        aliases.update(_SDLC_PREDICATE_ALIASES)
    raw_key = raw
    raw = aliases.get(raw, raw)
    if predicate_aliases and raw_key not in aliases:
        raw = predicate_aliases.get(raw, raw)
    if not _PREDICATE_RE.fullmatch(raw):
        return ""
    return raw


def predicate_label_key(value: object) -> str:
    """Return only the lexical lower-snake label, without applying mappings."""

    raw = _predicate_label_candidate(value)
    return raw if _PREDICATE_RE.fullmatch(raw) else ""


def _predicate_label_candidate(value: object) -> str:
    """Lexicalize before mapping while preserving legacy learned-alias lookup."""

    raw = unicodedata.normalize("NFKC", str(value or ""))
    # Predicate storage is intentionally ASCII. Reject unsupported Unicode
    # instead of deleting characters and risking two distinct labels collapsing
    # onto the same key (for example ``ecrit`` and ``écrit``).
    if any(ord(character) > 127 for character in raw):
        return ""
    raw = raw.strip().lower().replace("-", "_").replace(" ", "_")
    if not raw:
        return ""
    raw = _PREDICATE_TOKEN_RE.sub("_", raw)
    raw = _PREDICATE_UNDERSCORE_RE.sub("_", raw).strip("_")
    return raw


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def parse_curator_verdict(text: str) -> CuratorVerdict:
    """Parse a curator response. Any malformed output abstains, preserving the
    existing deterministic gate behavior."""
    for blob in reversed(_VERDICT_RE.findall(text or "")):
        try:
            data = json.loads(blob)
        except (TypeError, ValueError):
            continue
        action = data.get("action")
        if action not in {"commit", "queue"}:
            continue
        try:
            confidence = float(data.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        return CuratorVerdict(
            action=action,
            confidence=_clamp(confidence),
            reason=str(data.get("reason", ""))[:300],
            canonical_predicate=normalize_predicate(data.get("canonical_predicate")),
        )
    return CuratorVerdict(action="abstain", confidence=0.0, reason="unparseable")


_RELATION_VERDICT_FIELDS = frozenset(
    {
        "action",
        "confidence",
        "canonical_predicate",
        "predicate_definition",
        "predicate_direction",
        "inverse_direction_required",
        "subject_supported",
        "predicate_supported",
        "object_supported",
        "direction_supported",
        "unsupported_inference",
        "structural_noise",
        "redundant",
        "useful",
        "reason",
    }
)


def _relation_curator_verdict_from_mapping(
    data: Mapping[str, object],
    *,
    allow_candidate_id: bool = False,
) -> CuratorVerdict:
    expected_fields = (
        _RELATION_VERDICT_FIELDS | {"candidate_id"}
        if allow_candidate_id
        else _RELATION_VERDICT_FIELDS
    )
    if set(data) != expected_fields:
        return CuratorVerdict(action="abstain", reason="unparseable")
    action = data.get("action")
    if action not in {"commit", "queue"}:
        return CuratorVerdict(action="abstain", reason="unparseable")
    raw_confidence = data.get("confidence")
    if (
        not isinstance(raw_confidence, (int, float))
        or isinstance(raw_confidence, bool)
        or not math.isfinite(float(raw_confidence))
        or not 0.0 <= float(raw_confidence) <= 1.0
    ):
        return CuratorVerdict(action="abstain", reason="unparseable")
    confidence = float(raw_confidence)
    boolean_fields = (
        "inverse_direction_required",
        "subject_supported",
        "predicate_supported",
        "object_supported",
        "direction_supported",
        "unsupported_inference",
        "structural_noise",
        "redundant",
        "useful",
    )
    if any(not isinstance(data.get(field_name), bool) for field_name in boolean_fields):
        return CuratorVerdict(action="abstain", reason="unparseable")
    direction = data.get("predicate_direction")
    if direction not in {"subject_to_object", "symmetric", "unknown"}:
        return CuratorVerdict(action="abstain", reason="unparseable")
    raw_predicate = data.get("canonical_predicate")
    raw_definition = data.get("predicate_definition")
    reason = data.get("reason")
    if not isinstance(raw_predicate, str) or not isinstance(raw_definition, str):
        return CuratorVerdict(action="abstain", reason="unparseable")
    if not isinstance(reason, str):
        return CuratorVerdict(action="abstain", reason="unparseable")
    predicate = normalize_predicate(raw_predicate)
    definition = raw_definition.strip()
    if "\n" in definition or "\r" in definition or len(definition) > 500:
        return CuratorVerdict(action="abstain", reason="unparseable")
    if action == "commit" and (not predicate or not definition):
        return CuratorVerdict(action="abstain", reason="unparseable")
    if data["inverse_direction_required"] and direction != "subject_to_object":
        return CuratorVerdict(action="abstain", reason="unparseable")
    return CuratorVerdict(
        action=action,
        confidence=_clamp(confidence),
        reason=reason[:300],
        canonical_predicate=predicate,
        predicate_definition=definition[:500],
        predicate_direction=direction,  # type: ignore[arg-type]
        **{field_name: data[field_name] for field_name in boolean_fields},
    )


def parse_relation_curator_verdict(text: str) -> CuratorVerdict:
    """Parse the strict D5-D7 relation-evidence response or abstain."""

    data, _note = extract_json_object(text or "")
    if not isinstance(data, dict):
        return CuratorVerdict(action="abstain", confidence=0.0, reason="unparseable")
    return _relation_curator_verdict_from_mapping(data)


class LLMCandidateCurator:
    """Reviews every surviving node candidate before the confidence gate.

    The curator is the semantic gate: it can approve a candidate for the
    confidence gate or force it to queue. Provider failures abstain, and callers
    treat abstention as queue so deterministic resolver evidence never writes a
    candidate by itself.
    """

    def __init__(
        self,
        provider: "LLMProvider",
        *,
        temperature: float = 0.2,
        max_tokens: int = 2000,
        top_p: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        presence_penalty: float | None = None,
        enable_thinking: bool | None = None,
        system_prompt: str | None = None,
        packs: Iterable[str] | None = None,
    ) -> None:
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._top_p = top_p
        self._top_k = top_k
        self._min_p = min_p
        self._presence_penalty = presence_penalty
        self._enable_thinking = enable_thinking
        self._system_prompt = system_prompt or candidate_curator_system(packs)

    def build_prompt(
        self,
        candidate: "NodeCandidate",
        outcome: ResolveOutcome,
        *,
        store: "GraphStore",
        edges: list["EdgeCandidate"],
        proposed_action: str = "",
    ) -> str:
        """Build the user prompt without calling the LLM.

        ADR 0015 D1: prompt building reads the graph store; callers that fan
        curation out across threads prebuild prompts serially via this method
        and pass them to :meth:`curate` so no store access happens off the
        main thread.
        """
        return _build_curator_prompt(
            candidate,
            outcome,
            store=store,
            edges=edges,
            proposed_action=proposed_action,
        )

    def curate(
        self,
        candidate: "NodeCandidate",
        outcome: ResolveOutcome,
        *,
        store: "GraphStore",
        edges: list["EdgeCandidate"],
        proposed_action: str = "",
        user_prompt: str | None = None,
    ) -> CuratorVerdict:
        user = (
            user_prompt
            if user_prompt is not None
            else self.build_prompt(
                candidate,
                outcome,
                store=store,
                edges=edges,
                proposed_action=proposed_action,
            )
        )
        started = time.perf_counter()
        retries: list[dict[str, object]] = []
        try:
            reply = complete_with_retry(
                self._provider,
                [Message("system", self._system_prompt), Message("user", user)],
                step="curator",
                retries=retries,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                top_p=self._top_p,
                top_k=self._top_k,
                min_p=self._min_p,
                presence_penalty=self._presence_penalty,
                enable_thinking=self._enable_thinking,
                response_format=CURATOR_RESPONSE_FORMAT,
            )
        except LLMProviderError:
            return CuratorVerdict(
                action="abstain",
                confidence=0.0,
                reason="llm-unavailable",
                duration_s=round(time.perf_counter() - started, 3),
                provider_retries=tuple(retries),
            )
        return replace(
            parse_curator_verdict(reply),
            duration_s=round(time.perf_counter() - started, 3),
            usage=last_call_stats(),
            provider_retries=tuple(retries),
        )


class LLMRelationCurator:
    """Reviews every relationship candidate that could still be written.

    This second pass covers both topology edges and literal Claim candidates.
    Provider failures abstain, and callers fail closed by queueing the relation
    instead of writing from resolver evidence alone.
    """

    def __init__(
        self,
        provider: "LLMProvider",
        *,
        temperature: float = 0.2,
        max_tokens: int = 2000,
        top_p: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        presence_penalty: float | None = None,
        enable_thinking: bool | None = None,
        system_prompt: str | None = None,
        packs: Iterable[str] | None = None,
        registry_block: str = "",
    ) -> None:
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._top_p = top_p
        self._top_k = top_k
        self._min_p = min_p
        self._presence_penalty = presence_penalty
        self._enable_thinking = enable_thinking
        self._system_prompt = effective_relation_curator_system(system_prompt, packs)
        # Rendered ONCE per run by the caller from the start-of-run registry
        # snapshot. Deliberately not refreshed as the run mints predicates: a
        # mutating block would break the provider prefix cache on every mint.
        self._registry_block = registry_block

    @property
    def system_prompt(self) -> str:
        """The exact prompt used by single and batch calls."""

        return self._system_prompt

    @property
    def registry_block(self) -> str:
        """The run's rendered known-predicate block (may be empty)."""

        return self._registry_block

    def build_prompt(
        self,
        candidate: "EdgeCandidate",
        *,
        store: "GraphStore",
        node_candidates: Mapping[str, "NodeCandidate"],
        proposed_action: str = "",
    ) -> str:
        """Build the user prompt without calling the LLM (see ADR 0015 D1)."""
        return _build_relation_prompt(
            candidate,
            store=store,
            node_candidates=node_candidates,
            proposed_action=proposed_action,
            registry_block=self._registry_block,
        )

    def curate(
        self,
        candidate: "EdgeCandidate",
        *,
        store: "GraphStore",
        node_candidates: Mapping[str, "NodeCandidate"],
        proposed_action: str = "",
        user_prompt: str | None = None,
    ) -> CuratorVerdict:
        user = (
            user_prompt
            if user_prompt is not None
            else self.build_prompt(
                candidate,
                store=store,
                node_candidates=node_candidates,
                proposed_action=proposed_action,
            )
        )
        started = time.perf_counter()
        retries: list[dict[str, object]] = []
        try:
            reply = complete_with_retry(
                self._provider,
                [
                    Message("system", self._system_prompt),
                    Message("user", user),
                ],
                step="relation_curator",
                retries=retries,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                top_p=self._top_p,
                top_k=self._top_k,
                min_p=self._min_p,
                presence_penalty=self._presence_penalty,
                enable_thinking=self._enable_thinking,
                response_format=RELATION_CURATOR_RESPONSE_FORMAT,
            )
        except LLMProviderError:
            return CuratorVerdict(
                action="abstain",
                confidence=0.0,
                reason="llm-unavailable",
                duration_s=round(time.perf_counter() - started, 3),
                provider_retries=tuple(retries),
            )
        return replace(
            parse_relation_curator_verdict(reply),
            duration_s=round(time.perf_counter() - started, 3),
            usage=last_call_stats(),
            provider_retries=tuple(retries),
        )


def _build_curator_prompt(
    candidate: "NodeCandidate",
    outcome: ResolveOutcome,
    *,
    store: "GraphStore",
    edges: list["EdgeCandidate"],
    proposed_action: str = "",
) -> str:
    block_excerpt = _source_excerpt(candidate, store)
    relationships = _candidate_relationships(candidate, edges)
    correlations = _correlation_context(outcome, store)
    proposal = proposed_action.strip() or "  (none)"
    # Excerpt FIRST so [system + excerpt] is a byte-identical prefix across every
    # candidate of the same block. The provider's block-aware prefix cache reuses
    # that prefix over the within-block fan-out (oMLX caches in ~2048/4096-token
    # blocks; the ~12KB excerpt clears the floor, candidate-specific text doesn't).
    # Keep this ordering in lockstep with _build_relation_prompt.
    return (
        f"Source excerpt:\n{block_excerpt or '  (not available)'}\n\n"
        "Candidate:\n"
        f"  id: {candidate.candidate_id}\n"
        f"  type: {candidate.type}\n"
        f"  title: {candidate.title}\n"
        f"  content: {candidate.content[:900].strip()}\n"
        f"  current_resolver_confidence: {outcome.confidence:.3f}\n\n"
        f"Deterministic resolver proposal:\n{proposal}\n\n"
        f"Candidate relationships/claims:\n{relationships or '  (none)'}\n\n"
        f"Resolver correlations:\n{correlations or '  (none)'}\n\n"
        "Return commit if this is distinct, grounded, useful graph knowledge. "
        "Return queue if it needs human review before entering the graph."
    )


def relation_registry_section(registry_block: str) -> str:
    """The known-predicate section, or nothing when the registry is empty.

    Kept next to the excerpt section so ``curator_batch.relation_shared_prefix``
    can compose a byte-identical stripped prefix for the batch path.
    """

    if not registry_block:
        return ""
    return f"Known predicates (reuse by definition):\n{registry_block}\n\n"


def _build_relation_prompt(
    candidate: "EdgeCandidate",
    *,
    store: "GraphStore",
    node_candidates: Mapping[str, "NodeCandidate"],
    proposed_action: str = "",
    registry_block: str = "",
) -> str:
    src = _node_ref_context(candidate.src_ref, store=store, node_candidates=node_candidates)
    if candidate.dst_literal is not None:
        relation_kind = "literal claim"
        obj = f"literal object: {candidate.dst_literal!r}"
    else:
        relation_kind = "topology edge"
        obj = _node_ref_context(candidate.dst_ref, store=store, node_candidates=node_candidates)
    excerpt = _edge_source_excerpt(candidate, store)
    proposal = proposed_action.strip() or "  (none)"
    # Excerpt FIRST — same cacheable-prefix rationale as _build_curator_prompt.
    # The excerpt is byte-identical across every edge of the same block (keyed by
    # block_id), so [system + excerpt] is reused across the within-block fan-out.
    # The registry block joins that shared prefix: it is rendered once per run
    # from the start-of-run snapshot, so it is byte-stable across the whole run
    # and extends the cacheable prefix rather than breaking it. It must stay a
    # prefix, not a tail, or `curator_batch.build_batch_user_prompt` pays K
    # copies of it per batch (the strip at curator_batch.py fails silently).
    return (
        f"Source excerpt:\n{excerpt or '  (not available)'}\n\n"
        f"{relation_registry_section(registry_block)}"
        f"Relationship kind: {relation_kind}\n"
        f"Predicate/type: {candidate.type}\n\n"
        f"Deterministic resolver proposal:\n{proposal}\n\n"
        f"Subject:\n{src}\n\n"
        f"Object:\n{obj}\n\n"
        "Return commit if the excerpt directly supports this relationship. "
        "Return queue if the relationship needs human review before entering the graph."
    )


def _source_excerpt(
    candidate: "NodeCandidate",
    store: "GraphStore",
    limit: int = CURATION_SOURCE_EXCERPT_LIMIT,
) -> str:
    block_id = candidate.facets.get("block_id")
    if not block_id:
        return ""
    block = store.get_node(str(block_id))
    if block is None:
        return ""
    text = str(getattr(block, "content", "") or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n..."


def _edge_source_excerpt(
    candidate: "EdgeCandidate",
    store: "GraphStore",
    limit: int = CURATION_SOURCE_EXCERPT_LIMIT,
) -> str:
    block_id = candidate.block_id
    if not block_id:
        return ""
    block = store.get_node(str(block_id))
    if block is None:
        return ""
    text = str(getattr(block, "content", "") or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n..."


def _node_ref_context(
    ref: str,
    *,
    store: "GraphStore",
    node_candidates: Mapping[str, "NodeCandidate"],
) -> str:
    # Relation grounding belongs to the source excerpt. Endpoint descriptions
    # are mutable summaries and may describe an older state of the same stable
    # identity, so feeding them to the relation curator can incorrectly reject
    # a later correction. Type and title are the identity context required to
    # verify that the excerpt connects the intended endpoints.
    candidate = node_candidates.get(ref)
    if candidate is not None:
        return (
            f"  candidate_id: {candidate.candidate_id}\n"
            f"  type: {candidate.type}\n"
            f"  title: {candidate.title}"
        )
    node = store.get_node(ref)
    if node is not None:
        return f"  node_id: {node.id}\n  type: {node.type}\n  title: {node.title}"
    return f"  unresolved_ref: {ref}"


def _candidate_relationships(candidate: "NodeCandidate", edges: list["EdgeCandidate"]) -> str:
    lines: list[str] = []
    for edge in edges:
        if edge.src_ref == candidate.candidate_id:
            dst = edge.dst_ref or repr(edge.dst_literal)
            lines.append(f"  {candidate.title} --{edge.type}--> {dst}")
        elif edge.dst_ref == candidate.candidate_id:
            lines.append(f"  {edge.src_ref} --{edge.type}--> {candidate.title}")
    return "\n".join(lines[:12])


def _correlation_context(outcome: ResolveOutcome, store: "GraphStore") -> str:
    lines: list[str] = []
    for corr in outcome.correlations[:8]:
        target = store.get_node(corr.target_id)
        if target is None:
            target_text = corr.target_id
        else:
            target_text = (
                f"{target.id} | {target.type} | {target.title} | "
                f"{str(target.content or '')[:300].strip()}"
            )
        lines.append(
            f"  {corr.kind} score={corr.score:.3f}: {corr.summary}\n    target: {target_text}"
        )
    return "\n".join(lines)


__all__ = [
    "CURATOR_RESPONSE_FORMAT",
    "CURATOR_SYSTEM",
    "CURATION_SOURCE_EXCERPT_LIMIT",
    "CuratorAction",
    "CuratorVerdict",
    "LLMCandidateCurator",
    "LLMRelationCurator",
    "RELATION_CURATOR_EVIDENCE_VERSION",
    "RELATION_CURATOR_RESPONSE_FORMAT",
    "RELATION_CURATOR_SYSTEM",
    "RelationDirection",
    "candidate_curator_system",
    "effective_relation_curator_system",
    "normalize_predicate",
    "predicate_label_key",
    "parse_curator_verdict",
    "parse_relation_curator_verdict",
    "relation_curator_system",
]
