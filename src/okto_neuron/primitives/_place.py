"""Place primitive (crm:E53 / schema:Place).

Locations. v0 carries common fields only — geometry, coordinates,
country codes etc. are delegated to packs (proving the strict minimum
of the base surface).
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import ConfigDict, Field

from ._base import Primitive


def _place_schema_extra(schema: dict[str, Any]) -> None:
    schema["x-marginalia-standards"] = list(Place.__standards__)


class Place(Primitive):
    """Place — a location (spatial entity)."""

    __standards__: ClassVar[tuple[str, ...]] = ("crm:E53", "schema:Place")
    __schema_version__: ClassVar[str] = "0.1.0"

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_assignment=True,
        json_schema_extra=_place_schema_extra,
    )

    type: str = Field(default="core:Place")
