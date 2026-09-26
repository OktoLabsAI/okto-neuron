import pytest

from okto_neuron.core.schema import MissingKindOfError, parse_manifest


def test_ts9_missing_kind_of_has_field_path():
    text = """
id: missing-kind
version: "0.1.0"
compatRange: ">=0.0.1"
types:
  - name: Paper
"""

    with pytest.raises(MissingKindOfError) as exc_info:
        parse_manifest(text, "inline")

    assert exc_info.value.code == "MARG-PACK-003"
    assert exc_info.value.field_path == "types.0.kind_of"
