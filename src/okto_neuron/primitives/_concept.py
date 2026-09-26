"""Concept primitive (skos:Concept).

Multilingual SKOS-style concept with ``pref_label`` and ``alt_label``
maps keyed by BCP47 language tags. Enforces SKOS invariants:

* every label string is non-empty after strip
* within a language, the label list has no duplicates
* for each language, pref_label and alt_label sets are disjoint
* language keys are valid BCP47 (v0 subset) and normalized
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from ._base import Primitive
from ._lang import normalize_bcp47


def _concept_schema_extra(schema: dict[str, Any]) -> None:
    schema["x-marginalia-standards"] = list(Concept.__standards__)


def _raise(code: str, msg: str) -> None:
    raise PydanticCustomError(code, msg, {"code": code})


def _normalize_label_map(raw: dict[str, list[str]], field_name: str) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        raise ValueError(f"{field_name} must be a mapping[str, list[str]]")
    out: dict[str, list[str]] = {}
    for lang, values in raw.items():
        try:
            norm_lang = normalize_bcp47(lang)
        except ValueError as exc:
            _raise("lang_invalid_bcp47", str(exc))
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError(f"{field_name}[{lang!r}] must be a list[str]")
        cleaned: list[str] = []
        seen: set[str] = set()
        for v in values:
            if not v or not v.strip():
                _raise("label_empty", f"{field_name}[{norm_lang!r}] has empty label")
            if v in seen:
                _raise(
                    "label_duplicate_in_lang",
                    f"{field_name}[{norm_lang!r}] has duplicate label {v!r}",
                )
            seen.add(v)
            cleaned.append(v)
        out[norm_lang] = cleaned
    return out


class Concept(Primitive):
    """Concept — multilingual SKOS concept."""

    __standards__: ClassVar[tuple[str, ...]] = ("skos:Concept",)
    __schema_version__: ClassVar[str] = "0.1.0"

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_assignment=True,
        json_schema_extra=_concept_schema_extra,
    )

    type: str = Field(default="core:Concept")
    pref_label: dict[str, list[str]] = Field(default_factory=dict)
    alt_label: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_labels(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        if "pref_label" in data and data["pref_label"] is not None:
            data["pref_label"] = _normalize_label_map(data["pref_label"], "pref_label")
        if "alt_label" in data and data["alt_label"] is not None:
            data["alt_label"] = _normalize_label_map(data["alt_label"], "alt_label")
        return data

    @model_validator(mode="after")
    def _pref_alt_disjoint(self) -> "Concept":
        for lang, alts in self.alt_label.items():
            prefs = set(self.pref_label.get(lang, ()))
            if not prefs:
                continue
            collisions = prefs.intersection(alts)
            if collisions:
                _raise(
                    "label_pref_alt_overlap",
                    f"alt_label[{lang!r}] collides with pref_label: {sorted(collisions)!r}",
                )
        return self
