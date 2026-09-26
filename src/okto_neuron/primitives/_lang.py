"""BCP47 language-tag validation and normalization (v0 pragmatic subset).

Public surface: ``normalize_bcp47(tag) -> str`` and ``validate(tag) -> str``.
``normalize_bcp47`` raises ``ValueError`` on malformed input. ``validate``
raises Pydantic ``ValidationError`` with Okto Neuron's public error code.
v0 scope = 2-3 letter language subtag, optional 4-letter script subtag,
optional 2-letter alpha or 3-digit region subtag. Extlangs, variants,
extensions, private-use tags are deferred (see refinement Q2).

Normalization rules per RFC 5646 §2.1.1:
- language: lowercase
- script:   title-case (4 letters)
- region:   uppercase (alpha) or kept as-is (numeric)
"""

from __future__ import annotations

import re
from typing import Final

from pydantic import BaseModel, field_validator
from pydantic_core import PydanticCustomError

__all__ = ["normalize_bcp47", "validate", "BCP47_RE"]

BCP47_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z]{2,3}(-[A-Za-z]{4})?(-([A-Za-z]{2}|[0-9]{3}))?$"
)


def normalize_bcp47(tag: str) -> str:
    """Validate ``tag`` and return its canonical RFC 5646 form.

    Raises ``ValueError`` if ``tag`` does not match the v0 BCP47 subset.
    """
    if not isinstance(tag, str) or not BCP47_RE.match(tag):
        raise ValueError(f"invalid BCP47 tag: {tag!r}")
    parts = tag.split("-")
    out = [parts[0].lower()]
    for p in parts[1:]:
        if len(p) == 4 and p.isalpha():
            out.append(p.title())  # script
        elif p.isdigit():
            out.append(p)  # numeric region
        else:
            out.append(p.upper())  # alpha region
    return "-".join(out)


class _LangTagCheck(BaseModel):
    tag: str

    @field_validator("tag", mode="before")
    @classmethod
    def _valid_bcp47(cls, v: object) -> str:
        if not isinstance(v, str) or not BCP47_RE.match(v):
            raise PydanticCustomError(
                "lang_invalid_bcp47",
                "invalid BCP47 tag",
                {"code": "lang_invalid_bcp47"},
            )
        return normalize_bcp47(v)


def validate(tag: str) -> str:
    """Validate and normalize a BCP47 language tag via Pydantic errors."""
    return _LangTagCheck(tag=tag).tag
