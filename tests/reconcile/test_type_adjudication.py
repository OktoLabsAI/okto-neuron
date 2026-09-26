from __future__ import annotations

import json

from okto_neuron.llm import LLMProviderError
from okto_neuron.reconcile.type_adjudication import (
    LLMTypeAdjudicator,
    TYPE_ADJUDICATION_PROMPT_VERSION,
    TYPE_ADJUDICATION_RESPONSE_FORMAT,
    TypeAdjudicationCase,
    parse_type_adjudication,
)


def _case(candidate_id: str, reported_type: str) -> TypeAdjudicationCase:
    return TypeAdjudicationCase(
        candidate_id=candidate_id,
        reported_type=reported_type,  # type: ignore[arg-type]
        title="Bilbo Baggins",
        content="A hobbit who acts as a burglar.",
        source_excerpt="Bilbo put the Arkenstone in his deepest pocket.",
    )


class _Provider:
    model = "test/type-judge"

    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls: list[dict[str, object]] = []

    def complete(self, messages, **kwargs) -> str:
        self.calls.append({"messages": messages, **kwargs})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_type_adjudicator_returns_source_grounded_decisions() -> None:
    provider = _Provider(
        json.dumps(
            {
                "decisions": [
                    {
                        "candidate_id": "agent",
                        "primitive_type": "Agent",
                        "confidence": 0.99,
                        "reason": "The excerpt identifies an acting hobbit.",
                    },
                    {
                        "candidate_id": "concept",
                        "primitive_type": "Agent",
                        "confidence": 0.98,
                        "reason": "The referent acts and bears responsibility.",
                    },
                ]
            }
        )
    )

    result = LLMTypeAdjudicator(provider).adjudicate(
        "bilbo baggins",
        (_case("agent", "Agent"), _case("concept", "Concept")),
    )

    assert not result.error
    assert [item.primitive_type for item in result.decisions] == ["Agent", "Agent"]
    assert provider.calls[0]["response_format"] == TYPE_ADJUDICATION_RESPONSE_FORMAT
    system_prompt = provider.calls[0]["messages"][0].content
    assert '"decisions"' in system_prompt
    assert '"primitive_type"' in system_prompt
    assert "Do not return a bare array" in system_prompt
    assert TYPE_ADJUDICATION_PROMPT_VERSION == "type_adjudication.v2"
    prompt = provider.calls[0]["messages"][1].content
    assert "deepest pocket" in prompt


def test_type_adjudicator_fails_closed_on_provider_or_shape_error() -> None:
    unavailable = _Provider(LLMProviderError("offline", category="unavailable", retryable=True))
    assert (
        LLMTypeAdjudicator(unavailable).adjudicate("bilbo", (_case("one", "Concept"),)).error
        == "llm-unavailable:unavailable"
    )

    malformed = _Provider('{"decisions":[{"candidate_id":"other"}]}')
    result = LLMTypeAdjudicator(malformed).adjudicate("bilbo", (_case("one", "Concept"),))
    assert result.decisions == ()
    assert result.error.startswith("malformed-output:")


def test_parse_type_adjudication_allows_partial_but_rejects_unknown_ids() -> None:
    partial = parse_type_adjudication(
        json.dumps(
            {
                "decisions": [
                    {
                        "candidate_id": "one",
                        "primitive_type": "Agent",
                        "confidence": 0.8,
                        "reason": "Some evidence, but not enough to auto-correct.",
                    }
                ]
            }
        ),
        expected_ids={"one", "two"},
    )
    assert [item.candidate_id for item in partial] == ["one"]

    try:
        parse_type_adjudication(
            json.dumps(
                {
                    "decisions": [
                        {
                            "candidate_id": "other",
                            "primitive_type": "Agent",
                            "confidence": 1.0,
                            "reason": "Unexpected.",
                        }
                    ]
                }
            ),
            expected_ids={"one"},
        )
    except ValueError as exc:
        assert "unexpected candidate id" in str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("unknown ids must fail closed")
