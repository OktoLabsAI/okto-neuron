"""Annotation support type — oa:Annotation-aligned byte anchor.

Schema-layer validators enforce intra-Annotation invariants only
(byte_end>=byte_start, HEX64 block_id). The cross-model
"Annotation within parent Block" invariant (br_c5e7498a) lives in
`validate_annotation_within_block` and is enforced at the producer boundary
per dec_c63abf26.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from pydantic import Field, model_validator

from ._common import HEX64, SupportBase
from .source_span import SourceSpan

if TYPE_CHECKING:
    from .block import Block

__all__ = ["Annotation", "validate_annotation_within_block"]


class Annotation(SupportBase):
    """Byte-anchored annotation linking a Block region to a target primitive.

    Dedup composite key `(block_id, target_id, byte_start, byte_end)` is
    per-Block scoped (br_c5e7498a notes correction vs. the RFC story text).

    Zero-length anchors (byte_start == byte_end) are valid point anchors
    (br_ec5a750c); only byte_end < byte_start is rejected.
    """

    id: str
    block_id: HEX64
    target_id: str
    byte_start: int = Field(ge=0)
    byte_end: int = Field(ge=0)
    surface_form: Optional[str] = None
    # ADR 0003 Phase A (additive): optional vault-relative byte-span anchor.
    source_span: Optional[SourceSpan] = None

    @model_validator(mode="after")
    def _validate_byte_range(self) -> "Annotation":
        if self.byte_end < self.byte_start:
            raise ValueError(
                f"Annotation.byte_end ({self.byte_end}) must be >= byte_start ({self.byte_start})"
            )
        return self

    @property
    def dedup_key(self) -> tuple[str, str, int, int]:
        """Per-Block-scoped dedup key per br_c5e7498a.

        Plain ``@property`` (not ``@computed_field``) so the value is
        derivable on demand but is NOT emitted by ``model_dump`` — keeps
        JSON round-trip lossless under ``extra='forbid'`` (TR8 / AC14).
        """
        return (self.block_id, self.target_id, self.byte_start, self.byte_end)


def validate_annotation_within_block(annotation: Annotation, block: "Block") -> None:
    """Cross-model invariant per br_c5e7498a / br_48cf59ef.

    Returns None when the annotation lies wholly within the given Block and
    references it by id. Raises ValueError otherwise.
    """
    if annotation.block_id != block.id:
        raise ValueError(
            f"Annotation.block_id ({annotation.block_id}) does not match Block.id ({block.id})"
        )
    if annotation.byte_start < block.byte_start:
        raise ValueError(
            f"Annotation.byte_start ({annotation.byte_start}) is before "
            f"Block.byte_start ({block.byte_start})"
        )
    if annotation.byte_end > block.byte_end:
        raise ValueError(
            f"Annotation.byte_end ({annotation.byte_end}) is past Block.byte_end ({block.byte_end})"
        )
    return None
