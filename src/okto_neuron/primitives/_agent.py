"""Agent primitive (prov:Agent / crm:E39 / foaf:Agent).

Humans, organizations, AI systems, roles. Subtype discrimination
(Person/Organization/Software) is delegated to packs via the
``extends`` mechanism — the base Agent carries common fields only.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import ConfigDict, Field

from ._base import Primitive


def _agent_schema_extra(schema: dict[str, Any]) -> None:
    schema["x-marginalia-standards"] = list(Agent.__standards__)


class Agent(Primitive):
    """Agent — entity capable of performing activities."""

    __standards__: ClassVar[tuple[str, ...]] = (
        "prov:Agent",
        "crm:E39",
        "foaf:Agent",
    )
    __schema_version__: ClassVar[str] = "0.1.0"

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_assignment=True,
        json_schema_extra=_agent_schema_extra,
    )

    type: str = Field(default="core:Agent")
