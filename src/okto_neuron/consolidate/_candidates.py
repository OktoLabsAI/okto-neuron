"""Candidate value objects — proposed nodes/edges before they are committed.

Candidates are what an extractor *proposes*. They are staged, resolved, gated,
and only then committed as real graph nodes/edges. A node candidate's
``candidate_id`` is a deterministic content hash and becomes the committed
node id, so edges can reference candidates before commit and re-proposing the
same content is idempotent.

Phase C of docs/autonomous-architecture-plan.md.
"""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from okto_neuron.core.schema import Edge, Node, Provenance
from okto_neuron.semantic_surface import SurfaceRecord, exact_surface_key

_SEP = "\x1f"

# Literal object payload for a propositional Claim (mirrors schema.support.claim
# .ClaimLiteral). Kept inline to avoid importing the support schema into the
# candidate layer.
ClaimLiteral = str | int | float | bool


def _candidate_id(type_: str, title: str, content: str) -> str:
    """Identity from the FOLDED title, never the display spelling.

    Every dedup layer already matches on ``exact_surface_key`` (casefold +
    whitespace collapse), but identity used to hash the raw title, so the
    casing an entity happened to arrive with on its first extraction was baked
    into its permanent node id — the gap ADR 0040 tracks as Phase 2 work. Two
    spellings of one name now land on one id. ``content`` stays in the hash, so
    two mentions of the same title in different context remain distinct.
    """
    h = hashlib.sha256()
    h.update(_SEP.join((type_, exact_surface_key(title), content)).encode("utf-8"))
    return h.hexdigest()


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NodeCandidate(_Frozen):
    """A proposed node. ``candidate_id`` doubles as the committed node id."""

    type: str
    title: str = ""
    content: str = ""
    tags: tuple[str, ...] = ()
    facets: dict[str, Any] = Field(default_factory=dict)
    embedding: tuple[float, ...] | None = None
    provenance: Provenance = Field(default_factory=Provenance)
    surface: SurfaceRecord | None = None

    @property
    def candidate_id(self) -> str:
        return _candidate_id(self.type, self.title, self.content)

    def to_node(self) -> Node:
        return Node(
            id=self.candidate_id,
            type=self.type,
            title=self.title,
            content=self.content,
            tags=list(self.tags),
            facets=dict(self.facets),
            embedding=list(self.embedding) if self.embedding is not None else None,
            provenance=self.provenance,
        )


class EdgeCandidate(_Frozen):
    """A proposed edge. ``src_ref``/``dst_ref`` resolve to a candidate_id staged
    this session or to an id already in the store.

    The optional ``block_*`` / ``content_hash`` / ``confidence`` / ``model_id`` /
    ``prompt_hash`` fields carry the byte-anchored provenance of the source Block
    the relationship was extracted from. They default to ``None`` so existing
    callers (deterministic edges, untagged proposals) stay valid; a committed
    relationship that carries a ``block_id`` is minted into a provenance-bearing
    Claim (see ``companion._plan_relationship_claims``).

    ``dst_literal`` makes the candidate a **propositional Claim** rather than a
    topology edge: the object is a literal value (e.g. ``"19% slower"``), not a
    node. When set, ``dst_ref`` is empty, NO topology Edge is planned, and
    ``_plan_relationship_claims`` mints a Claim with ``O_literal`` instead of
    ``O_id``. This is the RFC ``O_value_or_id``
    object slot — the path for facts whose object has no canonical entity."""

    type: str
    src_ref: str
    dst_ref: str = ""
    dst_literal: ClaimLiteral | None = None
    weight: float = 1.0
    block_id: str | None = None
    byte_start: int | None = None
    byte_end: int | None = None
    content_hash: str | None = None
    confidence: float | None = None
    model_id: str | None = None
    prompt_hash: str | None = None
    provenance: Provenance = Field(default_factory=Provenance)

    def to_edge(self, src_id: str, dst_id: str) -> Edge:
        return Edge(
            type=self.type,
            src=src_id,
            dst=dst_id,
            weight=self.weight,
            provenance=self.provenance,
        )


__all__ = ["NodeCandidate", "EdgeCandidate", "ClaimLiteral"]
