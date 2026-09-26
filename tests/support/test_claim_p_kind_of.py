"""ts_8da6deb9 — Claim rejects P=='kind_of'.

RFC §4.2 composition rule: `kind_of` binds a pack type to a primitive — it is
not a Claim predicate (br_eb954a2d).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from okto_neuron.schema.support import Claim


def test_claim_rejects_kind_of_predicate():
    with pytest.raises(ValidationError) as exc:
        Claim(
            id="a" * 64,
            S_id="pack:Decision",
            P="kind_of",
            O_id="primitive:InformationObject",
            confidence=1.0,
            block_id="b" * 64,
            extraction_activity_id="act-1",
            agent_id="agent:marginalia",
        )
    assert "kind_of" in str(exc.value)


def test_claim_accepts_real_predicate():
    Claim(
        id="a" * 64,
        S_id="pack:Decision",
        P="cito:cites",
        O_id="info:RFC",
        confidence=1.0,
        block_id="b" * 64,
        extraction_activity_id="act-1",
        agent_id="agent:marginalia",
    )
