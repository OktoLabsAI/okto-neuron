"""ts_66d8f063 — Frozen mutation rejection across all 6 models.

TR2: every support model is frozen+extra='forbid' (dec_cd7a06be).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from okto_neuron.schema.support import (
    Annotation,
    Block,
    Claim,
    Document,
    Finding,
    Identifier,
)


def _build_all(
    canonical_document_payload,
    canonical_identifier_payload,
    canonical_block_payload,
    canonical_annotation_payload,
    canonical_claim_payload,
    canonical_finding_payload,
):
    return [
        (Document(**canonical_document_payload), "uri"),
        (Identifier(**canonical_identifier_payload), "value"),
        (Block(**canonical_block_payload), "block_index"),
        (Annotation(**canonical_annotation_payload), "byte_start"),
        (Claim(**canonical_claim_payload), "confidence"),
        (Finding(**canonical_finding_payload), "message"),
    ]


def test_frozen_mutation_rejected_for_all_six(
    canonical_document_payload,
    canonical_identifier_payload,
    canonical_block_payload,
    canonical_annotation_payload,
    canonical_claim_payload,
    canonical_finding_payload,
):
    instances = _build_all(
        canonical_document_payload,
        canonical_identifier_payload,
        canonical_block_payload,
        canonical_annotation_payload,
        canonical_claim_payload,
        canonical_finding_payload,
    )
    for instance, field in instances:
        with pytest.raises(ValidationError):
            setattr(instance, field, getattr(instance, field))


def test_extra_forbid_rejects_unknown_keys(canonical_document_payload):
    bad = dict(canonical_document_payload, mystery="x")
    with pytest.raises(ValidationError):
        Document(**bad)
