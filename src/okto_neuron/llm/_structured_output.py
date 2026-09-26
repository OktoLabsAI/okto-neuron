"""Structured-output strategies for CLI-shell LLM providers.

Two ways a CLI can honor a JSON schema:

- Native (``NativeSchemaStrategy``): the CLI has its own schema-enforcement
  flag (``codex exec --output-schema``, ``claude --json-schema``). The
  provider trusts it and returns the raw text unchanged.
- Best-effort (``BestEffortJsonStrategy``): the CLI has no such flag (``pi``).
  The schema is embedded as a prompt instruction and the response is repaired
  (fence-stripping, balanced-brace extraction) and schema-validated before
  being handed back — proven live against real curator/extraction schemas
  (28/28, ``.scratchpad/pi_cli_schema_spike.py``): every call required
  fence-stripping despite explicit "no fences" instructions.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

import jsonschema

_FENCED = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


class _JsonValidationFailure(Exception):
    """Raised by a strategy's ``finalize`` when the response can't be repaired
    into schema-valid JSON. Caught by :class:`CliShellProvider`'s reask loop —
    never surfaced directly to callers."""


class StructuredOutputStrategy(Protocol):
    max_attempts: int

    def embed_prompt_suffix(self, schema: dict | None) -> str: ...

    def finalize(self, raw_text: str, schema: dict) -> str: ...


class NativeSchemaStrategy:
    """The CLI enforces the schema itself via a native flag/output file."""

    max_attempts = 1

    def embed_prompt_suffix(self, schema: dict | None) -> str:
        return ""

    def finalize(self, raw_text: str, schema: dict) -> str:
        return raw_text


class BestEffortJsonStrategy:
    """No native schema slot — embed instructions in-prompt, repair + validate."""

    max_attempts = 3

    def embed_prompt_suffix(self, schema: dict | None) -> str:
        if schema is None:
            return ""
        return (
            "\n\nRespond with ONLY valid JSON matching this schema, "
            "no prose, no markdown code fences:\n"
            f"{json.dumps(schema, indent=2)}"
        )

    def finalize(self, raw_text: str, schema: dict) -> str:
        parsed, note = extract_json_object(raw_text)
        if parsed is None:
            raise _JsonValidationFailure(f"could not parse JSON from response: {note}")
        try:
            jsonschema.validate(parsed, schema)
        except jsonschema.ValidationError as exc:
            raise _JsonValidationFailure(f"schema validation failed: {exc.message}") from exc
        return json.dumps(parsed)


def extract_json_object(raw: str) -> tuple[dict | None, str]:
    """Extract one JSON object from a structured-output response.

    Providers do not always honor the request to omit Markdown fences, even
    when they accept a native JSON schema. Keep that transport normalization
    in one place while leaving schema validation to each caller.
    """
    raw = raw.strip()
    try:
        return json.loads(raw), ""
    except json.JSONDecodeError:
        pass
    m = _FENCED.search(raw)
    if m:
        try:
            return json.loads(m.group(1)), "required fence-stripping"
        except json.JSONDecodeError:
            pass
    start = raw.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i, ch in enumerate(raw[start:], start=start):
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                # Any other character inside a string — including a literal
                # ``{``/``}`` — must not perturb brace depth.
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = raw[start : i + 1]
                    try:
                        return json.loads(candidate), "required balanced-brace extraction"
                    except json.JSONDecodeError:
                        break
    return None, "unparseable"


# Compatibility for the original internal helper name. New structured-output
# consumers should use the descriptive public name above.
_extract_json = extract_json_object


__all__ = [
    "BestEffortJsonStrategy",
    "NativeSchemaStrategy",
    "StructuredOutputStrategy",
    "_JsonValidationFailure",
    "extract_json_object",
]
