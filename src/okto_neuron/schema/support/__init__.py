"""okto_neuron.schema.support — Cluster 1B support types (Pydantic v2).

Public surface locked by br_6bd97b97. Downstream Topics import from this
module by name; removal or rename of any public name fails CI.
"""

from __future__ import annotations

from ._common import HEX64, PATH_MAX_LENGTH, SupportBase, validate_safe_path
from .annotation import Annotation, validate_annotation_within_block
from .block import Block, BlockKind
from .claim import CLAIM_PROV_EDGES, Claim, ClaimLiteral
from .document import SAME_WORK_AS_EDGE, Document
from .finding import Finding, FindingSeverity, FindingStatus
from .identifier import (
    IDENTIFIER_REGISTRY,
    Identifier,
    IdentifierSchemeValidator,
    register_identifier_scheme,
)
from .source_span import SourceSpan

__all__ = [
    # Models
    "Document",
    "Identifier",
    "Annotation",
    "Claim",
    "Block",
    "Finding",
    # Value-objects (not graph nodes)
    "SourceSpan",
    # Primitives / config
    "HEX64",
    "SupportBase",
    "PATH_MAX_LENGTH",
    # Enums
    "BlockKind",
    "FindingSeverity",
    "FindingStatus",
    "ClaimLiteral",
    # Registries / constants
    "CLAIM_PROV_EDGES",
    "SAME_WORK_AS_EDGE",
    "IDENTIFIER_REGISTRY",
    "IdentifierSchemeValidator",
    # Helpers / hooks
    "register_identifier_scheme",
    "validate_annotation_within_block",
    "validate_safe_path",
]
