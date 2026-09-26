import pytest

from okto_neuron.core.schema import DuplicateTypeNameError, parse_manifest


def test_ts7_duplicate_type_name_is_error_013():
    text = """
id: duplicate-type
version: "0.1.0"
compatRange: ">=0.0.1"
types:
  - name: Paper
    kind_of: Asset
  - name: Paper
    kind_of: Asset
"""

    with pytest.raises(DuplicateTypeNameError) as exc_info:
        parse_manifest(text, "inline")

    assert exc_info.value.code == "MARG-PACK-013"
    assert exc_info.value.kind == "type"


def test_ts7_type_edge_name_collision_is_edge_type_error_013():
    text = """
id: duplicate-edge
version: "0.1.0"
compatRange: ">=0.0.1"
types:
  - name: Paper
    kind_of: Asset
edge_types:
  - name: Paper
    domain: Paper
    range: Asset
"""

    with pytest.raises(DuplicateTypeNameError) as exc_info:
        parse_manifest(text, "inline")

    assert exc_info.value.code == "MARG-PACK-013"
    assert exc_info.value.kind == "edge_type"
