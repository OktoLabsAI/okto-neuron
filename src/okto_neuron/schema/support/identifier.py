"""Identifier support type — typed external identifier with closed v0 registry.

Per dec_57ecd6da + br_ebea1368:
  - v0 closed registry seeded with {QID, ORCID, DOI, ISBN, EMAIL, DOMAIN}
  - per-scheme regex + checksum validators
  - public `register_identifier_scheme(scheme, validator, *, override=False)`
    hook lets future packs add schemes without modifying core

Each validator returns the canonical stored value (e.g. DOMAIN is lowercased)
or raises ValueError. The Identifier model_validator invokes the registered
validator after scheme-membership is confirmed.
"""

from __future__ import annotations

import re
from typing import Callable, Final

from pydantic import EmailStr, model_validator

from ._common import SupportBase

__all__ = [
    "Identifier",
    "IDENTIFIER_REGISTRY",
    "register_identifier_scheme",
    "IdentifierSchemeValidator",
]


IdentifierSchemeValidator = Callable[[str], str]


# ---------------------------------------------------------------------------
# Per-scheme validators (br_ebea1368)
# ---------------------------------------------------------------------------

_QID_RE = re.compile(r"^Q[1-9][0-9]*$")
_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[0-9X]$")
_DOI_RE = re.compile(r"^10\.\S+")
_ISBN10_RE = re.compile(r"^[0-9]{9}[0-9X]$")
_ISBN13_RE = re.compile(r"^[0-9]{13}$")
_DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)+$"
)


def _validate_qid(value: str) -> str:
    if not _QID_RE.fullmatch(value):
        raise ValueError(f"Invalid QID '{value}': must match ^Q[1-9][0-9]*$")
    return value


def _orcid_mod_11_2(value: str) -> bool:
    """ISO-7064 mod-11-2 check digit. Last char may be 'X' (=10)."""
    digits = value.replace("-", "")
    if len(digits) != 16:
        return False
    total = 0
    for ch in digits[:-1]:
        if not ch.isdigit():
            return False
        total = (total + int(ch)) * 2
    remainder = total % 11
    check = (12 - remainder) % 11
    last = digits[-1]
    expected = "X" if check == 10 else str(check)
    return last == expected


def _validate_orcid(value: str) -> str:
    if not _ORCID_RE.fullmatch(value):
        raise ValueError(f"Invalid ORCID '{value}': must match {_ORCID_RE.pattern}")
    if not _orcid_mod_11_2(value):
        raise ValueError(f"Invalid ORCID '{value}': ISO-7064 mod-11-2 check digit failed")
    return value


def _validate_doi(value: str) -> str:
    if not _DOI_RE.match(value):
        raise ValueError(f"Invalid DOI '{value}': must start with '10.'")
    return value


def _isbn10_check(value: str) -> bool:
    if not _ISBN10_RE.fullmatch(value):
        return False
    total = 0
    for i, ch in enumerate(value):
        digit = 10 if ch == "X" else int(ch)
        total += (i + 1) * digit
    return total % 11 == 0


def _isbn13_check(value: str) -> bool:
    if not _ISBN13_RE.fullmatch(value):
        return False
    total = 0
    for i, ch in enumerate(value):
        weight = 1 if i % 2 == 0 else 3
        total += weight * int(ch)
    return total % 10 == 0


def _validate_isbn(value: str) -> str:
    # Accept ISBN-10 or ISBN-13; reject anything else.
    cleaned = value.replace("-", "").replace(" ", "")
    if len(cleaned) == 10:
        if not _isbn10_check(cleaned):
            raise ValueError(f"Invalid ISBN-10 '{value}': check digit failed")
        return cleaned
    if len(cleaned) == 13:
        if not _isbn13_check(cleaned):
            raise ValueError(f"Invalid ISBN-13 '{value}': check digit failed")
        return cleaned
    raise ValueError(f"Invalid ISBN '{value}': must be 10 or 13 digits after stripping separators")


def _validate_email(value: str) -> str:
    # Delegate to Pydantic's EmailStr / email-validator. Soft-import so the
    # package still imports if email-validator is unavailable; the validator
    # only fails when EMAIL scheme is actually used.
    try:
        from pydantic import TypeAdapter

        TypeAdapter(EmailStr).validate_python(value)
    except Exception as exc:  # noqa: BLE001 — re-raise as ValueError
        raise ValueError(f"Invalid EMAIL '{value}': {exc}") from exc
    return value


def _validate_domain(value: str) -> str:
    lowered = value.lower()
    if not _DOMAIN_RE.fullmatch(lowered):
        raise ValueError(f"Invalid DOMAIN '{value}': does not match RFC 1035 label form")
    return lowered


# ---------------------------------------------------------------------------
# Registry (mutable at runtime; seeded with v0 closed set)
# ---------------------------------------------------------------------------

IDENTIFIER_REGISTRY: dict[str, IdentifierSchemeValidator] = {
    "QID": _validate_qid,
    "ORCID": _validate_orcid,
    "DOI": _validate_doi,
    "ISBN": _validate_isbn,
    "EMAIL": _validate_email,
    "DOMAIN": _validate_domain,
}

# Frozen v0 reference set kept for introspection / tests.
V0_IDENTIFIER_SCHEMES: Final[frozenset[str]] = frozenset(
    {"QID", "ORCID", "DOI", "ISBN", "EMAIL", "DOMAIN"}
)


def register_identifier_scheme(
    scheme: str,
    validator: IdentifierSchemeValidator,
    *,
    override: bool = False,
) -> None:
    """Register a new Identifier scheme at runtime.

    Pack-extension hook per dec_57ecd6da. Without override=True, attempts to
    redefine an existing scheme raise ValueError.
    """
    if not isinstance(scheme, str) or not scheme:
        raise ValueError("scheme must be a non-empty string")
    if not callable(validator):
        raise ValueError("validator must be callable")
    if scheme in IDENTIFIER_REGISTRY and not override:
        raise ValueError(
            f"Identifier scheme '{scheme}' already registered; pass override=True to replace"
        )
    IDENTIFIER_REGISTRY[scheme] = validator


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class Identifier(SupportBase):
    """A typed external identifier scoped to an owner primitive."""

    scheme: str
    value: str
    owner_id: str

    @model_validator(mode="after")
    def _validate_scheme_and_value(self) -> "Identifier":
        validator = IDENTIFIER_REGISTRY.get(self.scheme)
        if validator is None:
            raise ValueError(
                f"Unknown Identifier.scheme '{self.scheme}'. "
                f"Registered schemes: {sorted(IDENTIFIER_REGISTRY)}"
            )
        canonical = validator(self.value)
        if canonical != self.value:
            # The model is frozen, so we cannot reassign. Use object.__setattr__
            # via the same escape hatch Pydantic uses internally for
            # post-validation rewrites (only safe inside a model_validator).
            object.__setattr__(self, "value", canonical)
        return self
