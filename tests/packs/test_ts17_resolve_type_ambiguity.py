import pytest

from okto_neuron.core.schema import AmbiguousTypeError, PackLoader, PackRegistry, QualifiedTypeName


def test_ts17_resolve_type_ambiguity_and_disambiguation():
    registry = PackRegistry()
    loader = PackLoader(registry=registry)
    loader.load(
        {
            "id": "foo",
            "version": "0.1.0",
            "compatRange": ">=0.0.1",
            "types": [{"name": "Thing", "kind_of": "Concept"}],
        }
    )
    loader.load(
        {
            "id": "bar",
            "version": "0.1.0",
            "compatRange": ">=0.0.1",
            "types": [{"name": "Thing", "kind_of": "Concept"}],
        }
    )

    with pytest.raises(AmbiguousTypeError):
        registry.resolve_type("Thing")

    assert registry.resolve_type("Thing", pack_id="foo") == QualifiedTypeName("foo", "Thing")
