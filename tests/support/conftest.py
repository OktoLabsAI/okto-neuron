"""Shared fixtures for Cluster 1B support-type tests.

Canonical payloads per RFC v1.0 §4.2 (six support types) and §4.3 (atomic
provenance unit). Hex digests are sha256 of stable inputs so the fixtures
remain trivially reproducible.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest


def _h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# Deterministic HEX64 fixtures.
BLOCK_CONTENT_HASH = _h("paragraph-body-bytes")
BLOCK_ID_HASH = _h("notes/idea.md::0::" + BLOCK_CONTENT_HASH)
CLAIM_ID_HASH = _h("claim-1")
EVIDENCE_HASH_A = _h("evidence-a")
EVIDENCE_HASH_B = _h("evidence-b")
DOC_SHA = _h("doc-bytes")


@pytest.fixture
def block_content_hash() -> str:
    return BLOCK_CONTENT_HASH


@pytest.fixture
def block_id() -> str:
    return BLOCK_ID_HASH


@pytest.fixture
def claim_id() -> str:
    return CLAIM_ID_HASH


@pytest.fixture
def doc_sha() -> str:
    return DOC_SHA


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def canonical_document_payload(doc_sha, now):
    # RFC §4.2 — Document carries file/URL/email; DCMI-aligned.
    return {
        "id": "doc-1",
        "uri": "file:///vault/notes/idea.md",
        "media_type": "text/markdown",
        "byte_length": 4096,
        "sha256": doc_sha,
        "discovered_at": now,
    }


@pytest.fixture
def canonical_block_payload(block_id, block_content_hash):
    # RFC §4.3 — Block(path,byte_start,byte_end,content_hash,block_index,block_kind).
    return {
        "id": block_id,
        "path": "notes/idea.md",
        "block_index": 0,
        "byte_start": 0,
        "byte_end": 64,
        "block_kind": "paragraph",
        "content_hash": block_content_hash,
    }


@pytest.fixture
def canonical_annotation_payload(block_id):
    # RFC §4.2 — Annotation: "X appears in Y at offset Z" (oa:Annotation).
    return {
        "id": "ann-1",
        "block_id": block_id,
        "target_id": "agent:jp",
        "byte_start": 4,
        "byte_end": 18,
        "surface_form": "Jordan Lee",
    }


@pytest.fixture
def canonical_claim_payload(claim_id, block_id):
    # RFC §4.3 — Claim S-P-O with three PROV anchors.
    return {
        "id": claim_id,
        "S_id": "agent:jp",
        "P": "prov:wasInformedBy",
        "O_id": "agent:alex",
        "O_literal": None,
        "confidence": 0.92,
        "block_id": block_id,
        "extraction_activity_id": "act-deterministic-1",
        "agent_id": "agent:marginalia-extractor",
        "model_id": None,
        "prompt_hash": None,
    }


@pytest.fixture
def canonical_finding_payload(claim_id, now):
    # RFC §4.2 — Finding: detector output with evidence Claim ids.
    return {
        "id": "find-1",
        "kind": "drift",
        "severity": "warn",
        "status": "open",
        "evidence_claim_ids": [claim_id],
        "message": "content_hash drift detected on re-ingest",
        "detected_at": now,
    }


@pytest.fixture
def canonical_identifier_payload():
    # RFC §4.2 — Identifier(QID/ORCID/DOI/ISBN/email/…), here QID for Wikidata.
    return {"scheme": "QID", "value": "Q42", "owner_id": "agent:dna"}
