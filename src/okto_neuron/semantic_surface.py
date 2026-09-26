"""Lossless, versioned title-surface evidence for semantic adjudication.

Comparison keys are deliberately separate from display text.  The conservative
exact key may support deterministic comparison; the broader discovery key may
only find candidates for later adjudication.  Neither function repairs or
rewrites the preserved source surface.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

from pydantic import BaseModel, ConfigDict

NORMALIZER_VERSION: Final = "surface.v1"

_WS = re.compile(r"\s+")
_DISCOVERY_TRANSLATION = str.maketrans(
    {
        "’": "'",
        "‘": "'",
        "‛": "'",
        "–": "-",
        "—": "-",
        "_": " ",
    }
)
_MOJIBAKE_MARKERS = (
    "\ufffd",
    "Ã¡",
    "Ã£",
    "Ã©",
    "Ãª",
    "Ã­",
    "Ã³",
    "Ãº",
    "Ã§",
    "Â ",
    "Â ",
    "â€",
    "ðŸ",
)


class SurfaceRecord(BaseModel):
    """Preserved source spelling plus non-destructive comparison evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_surface: str
    exact_key: str
    discovery_key: str
    canonical_title: str
    aliases: tuple[str, ...] = ()
    normalization_flags: tuple[str, ...] = ()
    normalizer_version: str = NORMALIZER_VERSION


def exact_surface_key(value: object) -> str:
    """Return the conservative ADR 0040 exact-comparison key."""

    normalized = unicodedata.normalize("NFC", "" if value is None else str(value))
    folded = _WS.sub(" ", normalized).strip().casefold()
    # Case folding is not closed over NFC for every Unicode codepoint.  Normalize
    # again so canonically equivalent spellings cannot split inside one version.
    return unicodedata.normalize("NFC", folded)


def _strip_diacritics(value: str) -> str:
    """Drop combining marks: "andré" -> "andre", "são" -> "sao".

    Discovery-only. The same person or place is routinely written both with and
    without accents in a Portuguese or Spanish corpus, and ``exact_surface_key``
    deliberately keeps them apart (NFC + casefold is closed over diacritics), so
    without this the two spellings never even reach the merge judge.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    return unicodedata.normalize(
        "NFC", "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    )


def discovery_surface_key(value: object) -> str:
    """Return a broader discovery-only key, never an automatic merge signal."""

    translated = exact_surface_key(value).translate(_DISCOVERY_TRANSLATION)
    return _WS.sub(" ", _strip_diacritics(translated)).strip()


def surface_normalization_flags(value: object) -> tuple[str, ...]:
    """Describe comparison-affecting source features without repairing them."""

    source = "" if value is None else str(value)
    flags: list[str] = []
    if unicodedata.normalize("NFC", source) != source:
        flags.append("non_nfc")
    if _WS.sub(" ", source).strip() != source:
        flags.append("whitespace_normalized")
    if "_" in source:
        flags.append("underscore_separator")
    if any(char in source for char in ("’", "‘", "‛")):
        flags.append("apostrophe_variant")
    if any(char in source for char in ("–", "—")):
        flags.append("dash_variant")
    if any(marker in source for marker in _MOJIBAKE_MARKERS):
        flags.append("suspected_mojibake")
    return tuple(flags)


def build_surface_record(source_surface: str, canonical_title: str) -> SurfaceRecord:
    """Build lossless evidence without changing the selected canonical title."""

    source = str(source_surface)
    canonical = str(canonical_title)
    stripped_source = source.strip()
    flags = surface_normalization_flags(source)

    aliases = (
        (stripped_source,)
        if (stripped_source and stripped_source != canonical and "suspected_mojibake" not in flags)
        else ()
    )
    return SurfaceRecord(
        source_surface=source,
        exact_key=exact_surface_key(source),
        discovery_key=discovery_surface_key(source),
        canonical_title=canonical,
        aliases=aliases,
        normalization_flags=flags,
    )


def merge_surface_records(
    survivor: SurfaceRecord | None,
    variant: SurfaceRecord | None,
    *,
    canonical_title: str,
) -> SurfaceRecord | None:
    """Retain variant spellings and flags without changing survivor identity."""

    if variant is None:
        return survivor
    if survivor is None:
        return variant.model_copy(update={"canonical_title": canonical_title})

    aliases = list(survivor.aliases)
    variant_aliases = list(variant.aliases)
    variant_source = variant.source_surface.strip()
    if (
        variant_source
        and variant_source != canonical_title
        and "suspected_mojibake" not in variant.normalization_flags
    ):
        variant_aliases.insert(0, variant_source)
    for alias in variant_aliases:
        if alias and alias != canonical_title and alias not in aliases:
            aliases.append(alias)
    flags = tuple(dict.fromkeys((*survivor.normalization_flags, *variant.normalization_flags)))
    return survivor.model_copy(
        update={
            "canonical_title": canonical_title,
            "aliases": tuple(aliases),
            "normalization_flags": flags,
        }
    )


__all__ = [
    "NORMALIZER_VERSION",
    "SurfaceRecord",
    "build_surface_record",
    "discovery_surface_key",
    "exact_surface_key",
    "merge_surface_records",
    "surface_normalization_flags",
]
