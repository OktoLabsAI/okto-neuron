"""Finding support type — detector output with narrative evidence chain.

Per dec_496fbb2d + br_9219bcbb: evidence_claim_ids is an ordered list with
first-seen dedup; min_length=1 enforces at-least-one citation; elements are
HEX64. A Python set would lose causal-chain order conveyed by the list.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated

from pydantic import Field, StringConstraints, field_validator

from ._common import SupportBase

__all__ = ["Finding", "FindingSeverity", "FindingStatus"]


# Repeat the HEX64 constraint here so we can apply it element-wise inside the
# list. (Pydantic v2 applies StringConstraints per item when nested under
# Annotated[list[...]].)
_HEX64_ITEM = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$", min_length=64, max_length=64),
]


class FindingSeverity(str, Enum):
    info = "info"
    warn = "warn"
    error = "error"


class FindingStatus(str, Enum):
    open = "open"
    acknowledged = "acknowledged"
    resolved = "resolved"


class Finding(SupportBase):
    """A detector output citing one or more Claim IDs as evidence."""

    id: str
    kind: str
    severity: FindingSeverity
    status: FindingStatus = FindingStatus.open
    evidence_claim_ids: list[_HEX64_ITEM] = Field(min_length=1)
    message: str
    detected_at: datetime

    @field_validator("evidence_claim_ids", mode="after")
    @classmethod
    def _dedup_order_preserving(cls, v: list[str]) -> list[str]:
        # dec_496fbb2d: list(dict.fromkeys(v)) preserves first-seen order
        # while removing duplicates.
        deduped = list(dict.fromkeys(v))
        if not deduped:
            # Defensive: min_length=1 should have caught this, but keep the
            # post-dedup invariant explicit.
            raise ValueError("Finding.evidence_claim_ids must be non-empty")
        return deduped
