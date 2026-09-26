"""Claim support type — S-P-O assertion with PROV anchors.

Locked decisions:
- dec_bd2a1754: two-field O_id / O_literal split with model_validator
  enforcing exactly-one (over a Pydantic tagged union which forces a
  discriminator key and string-routes typed literals).
- br_eb954a2d: predicate `kind_of` is reserved for primitives (Cluster 1A)
  and rejected at field validation.
- br_4abc8c56: required PROV-anchor triple (block_id, extraction_activity_id,
  agent_id) so Cluster 1C can attach the 3 CLAIM_PROV_EDGES.
- br_bf5139e2: CLAIM_PROV_EDGES is exposed as a module-level constant.

`id` is opaque at the schema layer; Topic 06 derives it as
`sha256(block.content_hash || S || P || O || optional(model_id || prompt_hash))`
with the 0x1F separator.
"""

from __future__ import annotations

from typing import Final, Optional, Union

from pydantic import Field, field_validator, model_validator

from ._common import HEX64, SupportBase
from .source_span import SourceSpan

__all__ = ["Claim", "CLAIM_PROV_EDGES", "ClaimLiteral"]


# Typed JSON-LD-star literal payload. Locked by dec_bd2a1754.
ClaimLiteral = Union[str, int, float, bool]


class Claim(SupportBase):
    """An S-P-O assertion with content-hashed id and PROV anchors."""

    id: HEX64
    S_id: str
    P: str
    O_id: Optional[str] = None
    O_literal: Optional[ClaimLiteral] = None
    confidence: float = Field(ge=0.0, le=1.0)
    block_id: HEX64
    extraction_activity_id: str
    agent_id: str
    model_id: Optional[str] = None
    prompt_hash: Optional[str] = None
    # ADR 0003 Phase A (additive): optional vault-relative byte-span anchor.
    # block_id remains the canonical anchor; this is supplemental.
    source_span: Optional[SourceSpan] = None

    @field_validator("P")
    @classmethod
    def _reject_kind_of(cls, v: str) -> str:
        # br_eb954a2d: `kind_of` is the Cluster 1A composition operator, not
        # a Claim predicate.
        if v == "kind_of":
            raise ValueError(
                "Claim.P must not be 'kind_of' — that predicate is reserved for "
                "Cluster 1A primitive composition, not provenance assertions"
            )
        return v

    @model_validator(mode="after")
    def _validate_o_exactly_one(self) -> "Claim":
        # br_a0d586f2: exactly one of O_id / O_literal must be set.
        has_id = self.O_id is not None
        has_literal = self.O_literal is not None
        if has_id and has_literal:
            raise ValueError("Claim must set exactly one of O_id or O_literal, not both")
        if not has_id and not has_literal:
            raise ValueError("Claim must set exactly one of O_id or O_literal, not neither")
        return self


# Three PROV edges every Claim anchors. Locked by br_bf5139e2 + KB c5ada413.
# Target classes are referenced by string name because ExtractionActivity and
# Agent are not yet defined inside this package (they live in Cluster 1A /
# Topic 06); Cluster 1C's pack loader resolves them through the standards
# registry.
CLAIM_PROV_EDGES: Final[dict[str, dict[str, str]]] = {
    "wasDerivedFrom": {"curie": "prov:wasDerivedFrom", "target": "Block"},
    "wasGeneratedBy": {"curie": "prov:wasGeneratedBy", "target": "ExtractionActivity"},
    "wasAttributedTo": {"curie": "prov:wasAttributedTo", "target": "Agent"},
}
