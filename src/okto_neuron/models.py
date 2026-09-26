"""Public value models for the Okto Neuron Vault API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Node(_FrozenModel):
    id: str
    type: str
    name: str | None = None

    @property
    def title(self) -> str:
        """Compatibility alias for older call sites that rendered node titles."""
        return self.name or ""


class Document(Node):
    type: str = "Document"
    path: str | None = None
    tags: tuple[str, ...] = ()
    media_type: str | None = None
    content_hash: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )


class Provenance(_FrozenModel):
    path: str
    byte_start: int
    byte_end: int
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    extraction_activity_id: str
    agent_id: str
    document_id: str
    block_id: str


class ContextSpan(_FrozenModel):
    """A same-document neighbor block of a hit, recovered at query time.

    Storage is overlap-free (D5); context is rebuilt at read time by expanding a
    retrieved block to its adjacent siblings on the SAME ``path`` (selected by
    identical ``source_path`` + neighbouring ``block_index``). Carries only byte
    coordinates already implied by the primary provenance's path — it discloses
    no path the hit didn't already anchor to."""

    path: str
    byte_start: int
    byte_end: int
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    block_id: str
    block_index: int


class QueryHit(_FrozenModel):
    node: Node
    score: float = Field(ge=0.0, le=1.0)
    provenance: Provenance
    context_spans: tuple[ContextSpan, ...] = ()
    drift: Any | None = None

    @property
    def path(self) -> str:
        return self.provenance.path

    @property
    def byte_start(self) -> int:
        return self.provenance.byte_start

    @property
    def byte_end(self) -> int:
        return self.provenance.byte_end

    @property
    def content_hash(self) -> str:
        return self.provenance.content_hash.removeprefix("sha256:")

    @property
    def claim_id(self) -> str | None:
        if self.node.type == "Claim":
            return self.node.id
        return None


class IngestResult(_FrozenModel):
    document_id: str
    blocks_added: int
    claims_added: int
    annotations_added: int
    references_added: int
    skipped_existing: bool


class ExportScope(_FrozenModel):
    node_types: tuple[str, ...] | None = None
    document_ids: tuple[str, ...] | None = None
    since: datetime | None = None
    include_provenance: bool = True
    include_embeddings: bool = False


__all__ = [
    "ContextSpan",
    "Document",
    "ExportScope",
    "IngestResult",
    "Node",
    "Provenance",
    "QueryHit",
]
