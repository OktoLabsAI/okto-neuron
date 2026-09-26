from okto_neuron.core.schema import __all__


def test_ts16_all_is_frozen_snapshot():
    expected = (
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

    assert __all__ == expected
