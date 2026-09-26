"""Semantic identity helpers for relationship Claims."""

from __future__ import annotations

import json

from okto_neuron.ingest.markdown import sha256_hex


def literal_identity(value: object) -> str:
    """Return the byte-stable object term for literal-object Claims."""

    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return f"literal:{type(value).__name__}:{encoded}"


def claim_object_identity(
    *,
    object_id: object | None = None,
    literal: object | None = None,
) -> str:
    """Return the Claim hash object term for either an entity id or literal."""

    if object_id is not None:
        return str(object_id)
    if literal is not None:
        return literal_identity(literal)
    raise ValueError("semantic Claim identity requires object_id or literal")


def semantic_claim_id(
    subject_id: object,
    predicate: object,
    *,
    object_id: object | None = None,
    literal: object | None = None,
) -> str:
    """Return the semantic Claim id for a resolved S-P-O assertion."""

    return sha256_hex(
        "claim",
        subject_id,
        predicate,
        claim_object_identity(object_id=object_id, literal=literal),
    )


__all__ = ["claim_object_identity", "literal_identity", "semantic_claim_id"]
