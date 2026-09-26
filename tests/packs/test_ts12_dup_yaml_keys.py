import pytest

from okto_neuron.core.schema import YamlParseError, parse_manifest


def test_ts12_duplicate_yaml_keys_are_rejected():
    text = """
id: duplicate-a
id: duplicate-b
version: "0.1.0"
compatRange: ">=0.0.1"
"""

    with pytest.raises(YamlParseError) as exc_info:
        parse_manifest(text, "inline")

    assert exc_info.value.code == "MARG-PACK-001"
