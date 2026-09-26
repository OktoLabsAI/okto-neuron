"""SourceSpan value-object — vault-relative byte-range anchor.

A SourceSpan pins an extracted range of bytes within a vault-relative source
path. It is a frozen *value-object*, NOT a graph node: it deliberately does
NOT inherit `SupportBase` and is never registered as a support/node type. It
reuses the shared path-safety pipeline (br_33ab833d) via `validate_safe_path`.

Introduced by ADR 0003 Phase A (additive): Claim and Annotation gain an
optional `source_span` field while existing anchoring (block_id) is untouched.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._common import HEX64, validate_safe_path

__all__ = ["SourceSpan"]


class SourceSpan(BaseModel):
    """A frozen vault-relative byte-range anchor (value-object, not a node)."""

    model_config = ConfigDict(frozen=True)

    source_path: str
    byte_start: int = Field(ge=0)
    byte_end: int = Field(ge=0)
    content_hash: HEX64

    @field_validator("source_path", mode="before")
    @classmethod
    def _validate_source_path(cls, v: object) -> str:
        return validate_safe_path(v, label="SourceSpan.source_path")

    @model_validator(mode="after")
    def _validate_byte_range(self) -> "SourceSpan":
        if self.byte_end < self.byte_start:
            raise ValueError(
                f"SourceSpan.byte_end ({self.byte_end}) must be >= byte_start ({self.byte_start})"
            )
        return self
