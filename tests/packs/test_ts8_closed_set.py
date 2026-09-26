import pytest

from okto_neuron.core.schema import ClosedSetViolationError, PRIMITIVE_NAMES, parse_manifest


def test_ts8_kind_of_closed_set_violation_preserves_value_and_set():
    text = """
id: closed-set
version: "0.1.0"
compatRange: ">=0.0.1"
types:
  - name: PersonRecord
    kind_of: Person
"""

    with pytest.raises(ClosedSetViolationError) as exc_info:
        parse_manifest(text, "inline")

    assert exc_info.value.code == "MARG-PACK-005"
    assert exc_info.value.offending_value == "Person"
    assert exc_info.value.closed_set == PRIMITIVE_NAMES
