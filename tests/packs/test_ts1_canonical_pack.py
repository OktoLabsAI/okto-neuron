from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

import pytest

from okto_neuron.core.schema import PackLoader


FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "pack-manifests"
    / "valid"
    / "research-pack.yaml"
)


def test_ts1_loads_canonical_pack_end_to_end():
    loader = PackLoader()

    pack = loader.load(FIXTURE)
    second = loader.load(FIXTURE)

    assert second is pack
    assert pack.id == "research"
    assert pack.version == "0.1.0"
    assert pack.content_hash == second.content_hash
    assert isinstance(pack.types, MappingProxyType)
    assert isinstance(pack.edge_types, MappingProxyType)
    assert isinstance(pack.prefixes, MappingProxyType)
    assert set(pack.types) == {"Paper", "Researcher"}
    assert pack.types["Paper"].kind_of == "Asset"
    assert set(pack.edge_types) == {"cites"}
    assert pack.prefixes["ex"] == "https://example.test/vocab/"
    with pytest.raises(FrozenInstanceError):
        pack.id = "changed"
