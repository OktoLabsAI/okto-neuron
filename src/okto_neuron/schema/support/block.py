"""Block support type — CommonMark / transcript anchor.

A Block pins an extracted range of bytes within a source path. Path safety
pipeline locked by br_33ab833d; content_hash semantics locked by KB
1d643f38 (raw bytes [byte_start:byte_end], no normalization).
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, field_validator, model_validator

from ._common import HEX64, PATH_MAX_LENGTH, SupportBase, validate_safe_path

__all__ = ["Block", "BlockKind", "PATH_MAX_LENGTH"]


class BlockKind(str, Enum):
    """Locked enum per KB 1d643f38."""

    paragraph = "paragraph"
    heading = "heading"
    list_item = "list-item"
    code_block = "code-block"
    transcript_utterance = "transcript-utterance"


class Block(SupportBase):
    """A CommonMark / transcript byte-range anchor.

    `id` is opaque at the schema layer; Topic 06 derives it as
    `sha256(path || content_hash || block_index)`.

    Path safety pipeline (br_33ab833d), in order:
      1. reject NUL byte
      2. reject absolute path
      3. reject backslash
      4. reject `..` traversal segment
      5. enforce length <= 4096
      6. NFC-normalize the returned path (dec_ee679ddd)
    """

    id: HEX64
    path: str
    block_index: int = Field(ge=0)
    byte_start: int = Field(ge=0)
    byte_end: int = Field(ge=0)
    block_kind: BlockKind
    content_hash: HEX64

    @field_validator("path", mode="before")
    @classmethod
    def _validate_path(cls, v: object) -> str:
        return validate_safe_path(v, label="Block.path")

    @model_validator(mode="after")
    def _validate_byte_range(self) -> "Block":
        if self.byte_end < self.byte_start:
            raise ValueError(
                f"Block.byte_end ({self.byte_end}) must be >= byte_start ({self.byte_start})"
            )
        return self
