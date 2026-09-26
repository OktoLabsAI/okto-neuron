"""Core schema — the ultra-generic substrate.

Grounded in FRBR (Work/Item), authority control (Authority/Mention),
and SKOS-style relations (Edge.type vocabulary in packs).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def _uid() -> str:
    return uuid4().hex[:16]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Provenance(BaseModel):
    """How and why this object came to exist."""

    source: str = "user"  # user | ingest | llm | rule
    rule_id: Optional[str] = None
    layer: str = "deterministic"  # deterministic | cognitive | fallback
    confidence: float = 1.0


class Node(BaseModel):
    id: str = Field(default_factory=_uid)
    type: str  # e.g. "Note", "Person", "Decision"
    title: str = ""
    content: str = ""
    tags: list[str] = Field(default_factory=list)
    facets: dict[str, Any] = Field(default_factory=dict)  # Ranganathan PMEST
    embedding: Optional[list[float]] = None
    created_at: datetime = Field(default_factory=_now)
    provenance: Provenance = Field(default_factory=Provenance)


class Edge(BaseModel):
    id: str = Field(default_factory=_uid)
    type: str  # e.g. "mentions", "broader", "supersedes"
    src: str  # node id
    dst: str
    weight: float = 1.0
    provenance: Provenance = Field(default_factory=Provenance)


class Authority(Node):
    """Canonical entity record. All name variants resolve here."""

    type: str = "Authority"
    canonical_name: str = ""
    variants: list[str] = Field(default_factory=list)


class Mention(Edge):
    """X is mentioned in Y at offset Z."""

    type: str = "mentions"
    offset: Optional[int] = None
    span: Optional[int] = None


class Reference(Edge):
    """Citation: src cites dst."""

    type: str = "references"


class Work(Node):
    """FRBR: the abstract knowledge unit."""

    type: str = "Work"


class Item(Node):
    """FRBR: the file/URL/email carrying a Work."""

    type: str = "Item"
    path: Optional[str] = None
    url: Optional[str] = None
    mimetype: Optional[str] = None
    work_id: Optional[str] = None
