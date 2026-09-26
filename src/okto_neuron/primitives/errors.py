"""Frozen catalog of public error codes raised by primitive validators.

This is a public contract: topic 02 (persistence), topic 07 (Python API),
and topic 09 (MCP) all surface these codes verbatim. Snapshot tests in
``tests/primitives/test_errors.py`` pin the loc paths.

The catalog is wrapped in ``types.MappingProxyType`` so mutation attempts
raise ``TypeError`` at runtime. To add or rename a code, edit this file
and bump ``__schema_version__`` per DEC-008 amendment policy.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Mapping

__all__ = ["ERROR_CODES"]

_RAW_ERROR_CODES: dict[str, str] = {
    # --- COMMON (every primitive) ---
    "id_invalid_chars": "id must have no leading/trailing whitespace or control chars",
    "id_invalid_format": (
        "id must be non-empty, <=512 chars, and contain no whitespace or control chars"
    ),
    "id_too_long": "id exceeds 512 characters",
    "type_missing": "type field is required",
    "curie_invalid": "invalid CURIE format",
    "type_invalid_curie": "invalid CURIE format",
    "created_at_missing": "created_at field is required",
    "datetime_not_tz_aware": "datetime must be timezone-aware",
    "datetime_naive": "datetime must be timezone-aware",
    "extra_forbidden": "extra fields are not permitted",
    "frozen_instance": "primitive instances are immutable",
    "name_invalid": "name must be non-empty and stripped when provided",
    "base_abstract_instantiation": (
        "Primitive is abstract; instantiate Agent/Activity/InformationObject/Concept/Place"
    ),
    "primitive_abstract": (
        "Primitive is abstract; instantiate Agent/Activity/InformationObject/Concept/Place"
    ),
    # --- PER-PRIMITIVE ---
    "temporal_order_violation": "ended_at must be >= started_at",
    "activity_ordering": "ended_at_time must be >= started_at_time",
    "lang_invalid_bcp47": "invalid BCP47 tag",
    "label_empty": "label value must be non-empty",
    "label_duplicate_in_lang": "label list cannot contain duplicates per language",
    "label_pref_alt_overlap": ("alt_label entry collides with pref_label for the same language"),
    "concept_bcp47_invalid": "invalid BCP47 tag",
    "concept_pref_label_empty": "pref_label value must be non-empty",
    "concept_alt_label_collision": (
        "alt_labels entry collides with pref_label for the same language"
    ),
    # --- CLASS-DEFINITION-TIME (raised at import) ---
    "standards_missing": "subclass must define non-empty __standards__",
    "standards_bad_type": "__standards__ must be tuple[str, ...]",
    "standards_bad_curie": "__standards__ entry is not a valid CURIE",
}

ERROR_CODES: Final[Mapping[str, str]] = MappingProxyType(_RAW_ERROR_CODES)
