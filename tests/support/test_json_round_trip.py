"""ts_0e15bcb8 — JSON round-trip for all 6 models.

TR8: every support model round-trips losslessly via
`model_dump(mode='json')` → `model_validate(...)` (MCP boundary requirement).
"""

from __future__ import annotations

import json

import pytest

from okto_neuron.schema.support import (
    Annotation,
    Block,
    Claim,
    Document,
    Finding,
    Identifier,
)


@pytest.mark.parametrize(
    "cls,payload_fixture",
    [
        (Document, "canonical_document_payload"),
        (Identifier, "canonical_identifier_payload"),
        (Block, "canonical_block_payload"),
        (Annotation, "canonical_annotation_payload"),
        (Claim, "canonical_claim_payload"),
        (Finding, "canonical_finding_payload"),
    ],
)
def test_round_trip(cls, payload_fixture, request):
    payload = request.getfixturevalue(payload_fixture)
    original = cls(**payload)
    dumped = original.model_dump(mode="json")
    # JSON-serializable.
    encoded = json.dumps(dumped)
    decoded = json.loads(encoded)
    restored = cls.model_validate(decoded)
    assert restored == original
