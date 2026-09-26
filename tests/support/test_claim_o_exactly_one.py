"""ts_c0527fee — Claim.O exactly-one invariant.

RFC §4.3: Claim is one S-P-O assertion. O may be a node-ref OR a literal,
never both, never neither (dec_bd2a1754 / br_a0d586f2).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from okto_neuron.schema.support import Claim


def _claim(**overrides):
    payload = {
        "id": "a" * 64,
        "S_id": "agent:1",
        "P": "schema:knows",
        "O_id": "agent:2",
        "O_literal": None,
        "confidence": 1.0,
        "block_id": "b" * 64,
        "extraction_activity_id": "act-1",
        "agent_id": "agent:marginalia",
    }
    payload.update(overrides)
    return Claim(**payload)


def test_claim_with_only_o_id_ok():
    c = _claim()
    assert c.O_id == "agent:2"
    assert c.O_literal is None


@pytest.mark.parametrize("literal", ["text", 1, 3.14, True])
def test_claim_with_only_o_literal_ok(literal):
    c = _claim(O_id=None, O_literal=literal)
    assert c.O_literal == literal


def test_claim_neither_o_rejected():
    with pytest.raises(ValidationError) as exc:
        _claim(O_id=None, O_literal=None)
    assert "neither" in str(exc.value)


def test_claim_both_o_rejected():
    with pytest.raises(ValidationError) as exc:
        _claim(O_id="agent:2", O_literal="text")
    assert "both" in str(exc.value)
