"""Source-grounded primitive-type adjudication for exact-surface conflicts.

ADR 0040 requires primitive type correction to happen before identity
resolution.  This module owns the one semantic call needed when extraction has
assigned the same exact surface to different closed primitives.  It never
merges identities or writes graph state: callers may apply only high-confidence
type decisions and must leave every incomplete or ambiguous group in review.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypeAlias

from okto_neuron.llm import LLMProviderError, Message, complete_with_retry, last_call_stats
from okto_neuron.llm._structured_output import extract_json_object

if TYPE_CHECKING:
    from okto_neuron.llm import LLMProvider


PrimitiveType: TypeAlias = Literal["Agent", "Activity", "InformationObject", "Concept", "Place"]
PRIMITIVE_TYPES = frozenset({"Agent", "Activity", "InformationObject", "Concept", "Place"})
TYPE_ADJUDICATION_PROMPT_VERSION = "type_adjudication.v2"

TYPE_ADJUDICATION_SYSTEM_PROMPT = (
    "You adjudicate primitive types for knowledge-graph candidates that share "
    "one exact normalized surface but were extracted under conflicting types. "
    "Classify each candidate independently from its quoted source evidence.\n\n"
    "Use only these five primitives:\n"
    "- Agent: a person, character, animate actor, group, organization, or system "
    "that can act or bear responsibility.\n"
    "- Activity: an event, action, or process with temporal extent.\n"
    "- InformationObject: identifiable symbolic or propositional content, such "
    "as a document, message, dataset, record, or creative work.\n"
    "- Concept: an abstract category, topic, idea, role, or named physical object "
    "that is not better represented by another primitive.\n"
    "- Place: a spatial location, region, site, or jurisdiction.\n\n"
    "A shared title does not prove a shared identity. Do not reconcile, merge, or "
    "invent context. Return the best primitive for every candidate and a "
    "calibrated confidence. Use confidence below 0.95 whenever the excerpt is "
    "insufficient or supports more than one primitive.\n\n"
    "OUTPUT CONTRACT (mandatory): Return only one JSON object, without Markdown "
    "or commentary, in exactly this shape:\n"
    '{"decisions":[{"candidate_id":"<one supplied id>",'
    '"primitive_type":"Agent|Activity|InformationObject|Concept|Place",'
    '"confidence":0.0,"reason":"<brief source-grounded reason>"}]}\n'
    "Include exactly one decision for every supplied candidate. Use the exact "
    "field names shown. Do not return a bare array."
)

TYPE_ADJUDICATION_RESPONSE_FORMAT: dict[str, object] = {
    "type": "json_schema",
    "json_schema": {
        "name": "marginalia_type_adjudication",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "decisions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "candidate_id": {"type": "string"},
                            "primitive_type": {
                                "type": "string",
                                "enum": sorted(PRIMITIVE_TYPES),
                            },
                            "confidence": {
                                "type": "number",
                                "minimum": 0.0,
                                "maximum": 1.0,
                            },
                            "reason": {"type": "string"},
                        },
                        "required": [
                            "candidate_id",
                            "primitive_type",
                            "confidence",
                            "reason",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["decisions"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class TypeAdjudicationCase:
    candidate_id: str
    reported_type: PrimitiveType
    title: str
    content: str
    source_excerpt: str


@dataclass(frozen=True)
class TypeAdjudicationDecision:
    candidate_id: str
    primitive_type: PrimitiveType
    confidence: float
    reason: str


@dataclass(frozen=True)
class TypeAdjudicationResult:
    decisions: tuple[TypeAdjudicationDecision, ...] = ()
    error: str = ""
    duration_s: float | None = None
    usage: dict[str, int] | None = None
    # ADR 0039 D5: each transient provider failure the call retried
    # (``okto_neuron.llm.complete_with_retry``), for the adjudication ledger rows.
    provider_retries: tuple[dict[str, object], ...] = ()


class LLMTypeAdjudicator:
    """Classify one conflicting exact-surface group without mutating state."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        temperature: float | None = 0.0,
        max_tokens: int | None = 4000,
        top_p: float | None = None,
        top_k: int | None = None,
        min_p: float | None = None,
        presence_penalty: float | None = None,
        enable_thinking: bool | None = None,
    ) -> None:
        self._provider = provider
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._top_p = top_p
        self._top_k = top_k
        self._min_p = min_p
        self._presence_penalty = presence_penalty
        self._enable_thinking = enable_thinking

    @property
    def model(self) -> str:
        return str(getattr(self._provider, "model", "unknown"))

    def adjudicate(
        self,
        exact_surface: str,
        cases: tuple[TypeAdjudicationCase, ...],
    ) -> TypeAdjudicationResult:
        if not exact_surface.strip():
            raise ValueError("exact_surface must be non-empty")
        if not cases:
            raise ValueError("type adjudication requires at least one candidate")
        expected_ids = {case.candidate_id for case in cases}
        if len(expected_ids) != len(cases):
            raise ValueError("type adjudication candidate ids must be unique")

        user_prompt = json.dumps(
            {
                "exact_surface": exact_surface,
                "candidates": [
                    {
                        "candidate_id": case.candidate_id,
                        "reported_type": case.reported_type,
                        "title": case.title,
                        "content": case.content,
                        "source_excerpt": case.source_excerpt,
                    }
                    for case in cases
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        started = time.perf_counter()
        retries: list[dict[str, object]] = []
        try:
            reply = complete_with_retry(
                self._provider,
                [
                    Message("system", TYPE_ADJUDICATION_SYSTEM_PROMPT),
                    Message("user", user_prompt),
                ],
                step="type_adjudication",
                retries=retries,
                temperature=self._temperature,
                max_tokens=self._max_tokens,
                top_p=self._top_p,
                top_k=self._top_k,
                min_p=self._min_p,
                presence_penalty=self._presence_penalty,
                enable_thinking=self._enable_thinking,
                response_format=TYPE_ADJUDICATION_RESPONSE_FORMAT,
            )
        except LLMProviderError as exc:
            return TypeAdjudicationResult(
                error=f"llm-unavailable:{exc.category}",
                duration_s=round(time.perf_counter() - started, 3),
                provider_retries=tuple(retries),
            )

        try:
            decisions = parse_type_adjudication(reply, expected_ids=expected_ids)
        except ValueError as exc:
            return TypeAdjudicationResult(
                error=f"malformed-output:{exc}",
                duration_s=round(time.perf_counter() - started, 3),
                usage=last_call_stats(),
                provider_retries=tuple(retries),
            )
        return TypeAdjudicationResult(
            decisions=decisions,
            duration_s=round(time.perf_counter() - started, 3),
            usage=last_call_stats(),
            provider_retries=tuple(retries),
        )


def parse_type_adjudication(
    text: str,
    *,
    expected_ids: set[str],
) -> tuple[TypeAdjudicationDecision, ...]:
    data, note = extract_json_object(text)
    if data is None:
        raise ValueError(note)
    if set(data) != {"decisions"} or not isinstance(data["decisions"], list):
        raise ValueError("response must contain only a decisions array")

    seen: set[str] = set()
    decisions: list[TypeAdjudicationDecision] = []
    for index, raw in enumerate(data["decisions"]):
        if not isinstance(raw, dict) or set(raw) != {
            "candidate_id",
            "primitive_type",
            "confidence",
            "reason",
        }:
            raise ValueError(f"decisions[{index}] has an invalid shape")
        candidate_id = str(raw["candidate_id"]).strip()
        if candidate_id not in expected_ids:
            raise ValueError(f"unexpected candidate id {candidate_id!r}")
        if candidate_id in seen:
            raise ValueError(f"duplicate candidate id {candidate_id!r}")
        seen.add(candidate_id)
        primitive_type = str(raw["primitive_type"]).strip()
        if primitive_type not in PRIMITIVE_TYPES:
            raise ValueError(f"invalid primitive type {primitive_type!r}")
        confidence = raw["confidence"]
        if isinstance(confidence, bool):
            raise ValueError("confidence must be numeric")
        try:
            confidence_value = float(confidence)
        except (TypeError, ValueError) as exc:
            raise ValueError("confidence must be numeric") from exc
        if not 0.0 <= confidence_value <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        reason = str(raw["reason"]).strip()
        if not reason:
            raise ValueError("reason must be non-empty")
        decisions.append(
            TypeAdjudicationDecision(
                candidate_id=candidate_id,
                primitive_type=primitive_type,  # type: ignore[arg-type]
                confidence=confidence_value,
                reason=reason[:500],
            )
        )
    if not decisions:
        raise ValueError("decisions must not be empty")
    return tuple(decisions)


__all__ = [
    "LLMTypeAdjudicator",
    "PRIMITIVE_TYPES",
    "TYPE_ADJUDICATION_PROMPT_VERSION",
    "TYPE_ADJUDICATION_RESPONSE_FORMAT",
    "TYPE_ADJUDICATION_SYSTEM_PROMPT",
    "TypeAdjudicationCase",
    "TypeAdjudicationDecision",
    "TypeAdjudicationResult",
    "parse_type_adjudication",
]
