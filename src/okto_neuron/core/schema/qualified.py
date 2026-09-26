"""Qualified type names and standard vocabulary prefixes."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

PRIMITIVE_NAMES: Final[tuple[str, ...]] = ("Agent", "Asset", "Event", "Place", "Concept")

STANDARDS_PREFIXES: Final = MappingProxyType(
    {
        "schema": "https://schema.org/",
        "prov": "http://www.w3.org/ns/prov#",
        "skos": "http://www.w3.org/2004/02/skos/core#",
        "foaf": "http://xmlns.com/foaf/0.1/",
        "cito": "http://purl.org/spar/cito/",
        "oa": "http://www.w3.org/ns/oa#",
        "bibo": "http://purl.org/ontology/bibo/",
        "crm": "http://www.cidoc-crm.org/cidoc-crm/",
        "bf": "http://id.loc.gov/ontologies/bibframe/",
        "frbr-lrm": "http://iflastandards.info/ns/lrm/lrmer/",
        "dcterms": "http://purl.org/dc/terms/",
    }
)


@dataclass(frozen=True, slots=True)
class QualifiedTypeName:
    pack_id: str
    name: str

    def __str__(self) -> str:
        return f"{self.pack_id}:{self.name}"
