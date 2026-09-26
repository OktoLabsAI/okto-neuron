"""ts_da4568ca — Canonical RFC §4.2/§4.3 snapshot for all 6 support models."""

from __future__ import annotations

from okto_neuron.schema.support import (
    Annotation,
    Block,
    Claim,
    Document,
    Finding,
    Identifier,
)


def test_canonical_snapshot_all_six_models(
    canonical_document_payload,
    canonical_identifier_payload,
    canonical_annotation_payload,
    canonical_claim_payload,
    canonical_block_payload,
    canonical_finding_payload,
):
    """RFC §4.2 + §4.3 — every support type instantiates from its canonical payload."""
    Document(**canonical_document_payload)
    Identifier(**canonical_identifier_payload)
    Block(**canonical_block_payload)
    Annotation(**canonical_annotation_payload)
    Claim(**canonical_claim_payload)
    Finding(**canonical_finding_payload)
