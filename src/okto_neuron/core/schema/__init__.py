"""Core schema compatibility models and pack manifest machinery."""

from __future__ import annotations

from .errors import (
    AmbiguousTypeError,
    CircularImportError,
    ClosedSetViolationError,
    DuplicateTypeNameError,
    IncompatibleCoreVersionError,
    MalformedCURIEError,
    MissingKindOfError,
    NamespaceShadowingFinding,
    PackManifestError,
    PackVersionConflictError,
    RegistryError,
    ReservedPackManifestError,  # noqa: F401 - reserved code remains importable outside __all__.
    SchemaValidationError,
    SelfImportError,
    SemverError,
    UnknownCURIEPrefixError,
    UnknownPrimitiveError,
    UnresolvedImportError,
    YamlParseError,
)
from .legacy import Authority, Edge, Item, Mention, Node, Provenance, Reference, Work
from .loader import Pack, PackLoader
from .manifest import EdgeTypeDecl, PackManifestModel, TypeDecl, parse_manifest
from .qualified import PRIMITIVE_NAMES, STANDARDS_PREFIXES, QualifiedTypeName
from .registry import PackRegistry, default_registry

__all__ = (
    # legacy compat
    "Provenance",
    "Node",
    "Edge",
    "Authority",
    "Mention",
    "Reference",
    "Work",
    "Item",
    # pack manifest surface
    "PackManifestModel",
    "TypeDecl",
    "EdgeTypeDecl",
    "parse_manifest",
    "QualifiedTypeName",
    "NamespaceShadowingFinding",
    "STANDARDS_PREFIXES",
    "PRIMITIVE_NAMES",
    "PackManifestError",
    "YamlParseError",
    "SchemaValidationError",
    "MissingKindOfError",
    "UnknownPrimitiveError",
    "ClosedSetViolationError",
    "UnknownCURIEPrefixError",
    "MalformedCURIEError",
    "CircularImportError",
    "SelfImportError",
    "UnresolvedImportError",
    "SemverError",
    "IncompatibleCoreVersionError",
    "DuplicateTypeNameError",
    "PackVersionConflictError",
    "PackLoader",
    "Pack",
    "PackRegistry",
    "default_registry",
    "AmbiguousTypeError",
    "RegistryError",
)
