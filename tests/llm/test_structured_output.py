"""Tests for the structured-output strategies (native passthrough vs.
best-effort JSON repair + validation)."""

from __future__ import annotations

import json

import pytest

from okto_neuron.llm._structured_output import (
    BestEffortJsonStrategy,
    NativeSchemaStrategy,
    _JsonValidationFailure,
    _extract_json,
)

SCHEMA = {
    "type": "object",
    "properties": {"x": {"type": "integer"}},
    "required": ["x"],
}


# ── NativeSchemaStrategy ─────────────────────────────────────────────────


def test_native_max_attempts_is_one() -> None:
    assert NativeSchemaStrategy().max_attempts == 1


def test_native_embed_prompt_suffix_is_empty() -> None:
    assert NativeSchemaStrategy().embed_prompt_suffix(SCHEMA) == ""
    assert NativeSchemaStrategy().embed_prompt_suffix(None) == ""


def test_native_finalize_trusts_raw_text_unchanged() -> None:
    # Trusts the CLI's own enforcement — no parsing, no validation, no repair.
    raw = "```json\n{not even valid json\n```"
    assert NativeSchemaStrategy().finalize(raw, SCHEMA) == raw


# ── BestEffortJsonStrategy ────────────────────────────────────────────────


def test_best_effort_max_attempts_is_three() -> None:
    assert BestEffortJsonStrategy().max_attempts == 3


def test_best_effort_embed_prompt_suffix_none_schema() -> None:
    assert BestEffortJsonStrategy().embed_prompt_suffix(None) == ""


def test_best_effort_embed_prompt_suffix_includes_schema() -> None:
    suffix = BestEffortJsonStrategy().embed_prompt_suffix(SCHEMA)
    assert "Respond with ONLY valid JSON" in suffix
    assert "no markdown code fences" in suffix
    assert '"type": "integer"' in suffix


def test_best_effort_finalize_plain_json() -> None:
    out = BestEffortJsonStrategy().finalize('{"x": 1}', SCHEMA)
    assert json.loads(out) == {"x": 1}


def test_best_effort_finalize_strips_fences() -> None:
    raw = '```json\n{"x": 1}\n```'
    out = BestEffortJsonStrategy().finalize(raw, SCHEMA)
    assert json.loads(out) == {"x": 1}


def test_best_effort_finalize_balanced_brace_extraction() -> None:
    raw = 'Sure, here you go: {"x": 1} — hope that helps!'
    out = BestEffortJsonStrategy().finalize(raw, SCHEMA)
    assert json.loads(out) == {"x": 1}


def test_best_effort_finalize_unparseable_raises() -> None:
    with pytest.raises(_JsonValidationFailure, match="could not parse JSON"):
        BestEffortJsonStrategy().finalize("not json at all", SCHEMA)


def test_best_effort_finalize_schema_violation_raises() -> None:
    with pytest.raises(_JsonValidationFailure, match="schema validation failed"):
        BestEffortJsonStrategy().finalize('{"x": "not an integer"}', SCHEMA)


def test_best_effort_finalize_missing_required_field_raises() -> None:
    with pytest.raises(_JsonValidationFailure, match="schema validation failed"):
        BestEffortJsonStrategy().finalize("{}", SCHEMA)


# ── _extract_json helper ──────────────────────────────────────────────────


def test_extract_json_raw_parse() -> None:
    parsed, note = _extract_json('{"a": 1}')
    assert parsed == {"a": 1}
    assert note == ""


def test_extract_json_fenced() -> None:
    parsed, note = _extract_json('```json\n{"a": 1}\n```')
    assert parsed == {"a": 1}
    assert "fence-stripping" in note


def test_extract_json_balanced_brace() -> None:
    parsed, note = _extract_json('prose before {"a": 1} prose after')
    assert parsed == {"a": 1}
    assert "balanced-brace" in note


def test_extract_json_unparseable() -> None:
    parsed, note = _extract_json("no json here")
    assert parsed is None
    assert note == "unparseable"


def test_extract_json_balanced_brace_with_literal_brace_in_string_value() -> None:
    """3.10: a literal '}' inside a JSON string value must not perturb the
    scanner's brace-depth count. Before the string/escape-aware fix, the
    naive depth counter closed the object at the '}' inside the string
    value (depth 1 -> 0), producing the truncated, invalid candidate
    '{"note": "call foo() }' and returning (None, "unparseable") even
    though the full payload is valid JSON."""
    raw = 'Sure, here is the JSON:\n{"note": "call foo() } to close", "value": 42}'
    parsed, note = _extract_json(raw)
    assert parsed == {"note": "call foo() } to close", "value": 42}
    assert "balanced-brace" in note


def test_extract_json_balanced_brace_with_escaped_quote_in_string_value() -> None:
    """A '\\"' inside a string must not be read as the string's closing
    quote — otherwise the scanner would treat subsequent text as bare
    (non-string) content and misparse any braces within it."""
    raw = 'prose {"note": "she said \\"hi } there\\" today", "n": 1} trailing'
    parsed, note = _extract_json(raw)
    assert parsed == {"note": 'she said "hi } there" today', "n": 1}
    assert "balanced-brace" in note


def test_best_effort_finalize_handles_literal_brace_in_string_value() -> None:
    """End-to-end: BestEffortJsonStrategy.finalize (the sole PiCliProvider
    consumer) must not raise _JsonValidationFailure for schema-valid JSON
    whose string values happen to contain a literal '}'."""
    schema = {
        "type": "object",
        "properties": {"note": {"type": "string"}},
        "required": ["note"],
    }
    raw = 'Here: {"note": "call foo() } to close"}'
    out = BestEffortJsonStrategy().finalize(raw, schema)
    assert json.loads(out) == {"note": "call foo() } to close"}
