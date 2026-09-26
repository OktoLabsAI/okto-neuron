"""InformationObject primitive (crm:E73 / frbr-lrm:Work / schema:CreativeWork).

Documents, claims, requirements, notes — any conveyable bundle of
information. Common fields only at v0; payload-specific fields are
delegated to packs.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import ConfigDict, Field

from ._base import Primitive


def _infoobj_schema_extra(schema: dict[str, Any]) -> None:
    schema["x-marginalia-standards"] = list(InformationObject.__standards__)


class InformationObject(Primitive):
    """InformationObject — a Work-level conveyable item."""

    __standards__: ClassVar[tuple[str, ...]] = (
        "crm:E73",
        "frbr-lrm:Work",
        "schema:CreativeWork",
    )
    __schema_version__: ClassVar[str] = "0.1.0"

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_assignment=True,
        json_schema_extra=_infoobj_schema_extra,
    )

    type: str = Field(default="core:InformationObject")
