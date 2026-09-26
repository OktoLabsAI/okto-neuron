"""ts_74ffecf5 — Hypothesis property tests P1-P9.

P1 round-trip serialization; P2 Claim O-exactly-one; P3 Finding dedup
order-preserving; P4 dedup_key stability; P5 byte-order monotonicity;
P6 path NFC idempotence; P7 Identifier scheme matrix; P8 frozen mutation
rejection; P9 ID-determinism fixture stub (deferred to Topic 06).
"""

from __future__ import annotations

import hashlib
import string
import unicodedata

import pytest
from hypothesis import HealthCheck, given, settings, strategies as st
from pydantic import ValidationError

from okto_neuron.schema.support import (
    Annotation,
    Block,
    Claim,
    Document,
    Finding,
    Identifier,
)

HEX = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)
SAFE_PATH = st.text(
    alphabet=string.ascii_letters + string.digits + "_-/.",
    min_size=1,
    max_size=200,
).filter(
    lambda s: (
        "\x00" not in s and not s.startswith("/") and "\\" not in s and ".." not in s.split("/")
    )
)
BYTE_RANGE = st.tuples(st.integers(0, 10_000), st.integers(0, 10_000)).map(sorted)


# ---- P1: model_dump(mode='json') -> model_validate round-trip ----------------


@given(content_hash=HEX, block_index=st.integers(0, 1000))
@settings(max_examples=50, suppress_health_check=[HealthCheck.too_slow])
def test_p1_block_round_trip(content_hash, block_index):
    b = Block(
        id=content_hash,
        path="x.md",
        block_index=block_index,
        byte_start=0,
        byte_end=1,
        block_kind="paragraph",
        content_hash=content_hash,
    )
    dumped = b.model_dump(mode="json")
    assert Block.model_validate(dumped) == b


# ---- P2: Claim exactly one of O_id/O_literal ---------------------------------


def _claim(**o):
    payload = dict(
        id="a" * 64,
        S_id="s",
        P="p",
        confidence=1.0,
        block_id="b" * 64,
        extraction_activity_id="act",
        agent_id="ag",
    )
    payload.update(o)
    return Claim(**payload)


@given(
    o_id=st.one_of(st.none(), st.text(min_size=1, max_size=20)),
    o_lit=st.one_of(st.none(), st.text(min_size=1, max_size=20)),
)
def test_p2_claim_o_exactly_one(o_id, o_lit):
    n = (o_id is not None) + (o_lit is not None)
    if n == 1:
        _claim(O_id=o_id, O_literal=o_lit)
    else:
        with pytest.raises(ValidationError):
            _claim(O_id=o_id, O_literal=o_lit)


# ---- P3: Finding dedup is order-preserving -----------------------------------


@given(st.lists(HEX, min_size=1, max_size=20))
def test_p3_finding_dedup_first_seen(ids):
    f = Finding(
        id="f",
        kind="k",
        severity="info",
        evidence_claim_ids=ids,
        message="m",
        detected_at="2026-05-19T00:00:00Z",
    )
    expected = list(dict.fromkeys(ids))
    assert f.evidence_claim_ids == expected


# ---- P4: Annotation.dedup_key is stable across construction ------------------


@given(
    block_id=HEX,
    target=st.text(min_size=1, max_size=20),
    rng=BYTE_RANGE,
)
def test_p4_annotation_dedup_key_stability(block_id, target, rng):
    a = Annotation(id="x", block_id=block_id, target_id=target, byte_start=rng[0], byte_end=rng[1])
    assert a.dedup_key == (block_id, target, rng[0], rng[1])
    a2 = Annotation(id="x", block_id=block_id, target_id=target, byte_start=rng[0], byte_end=rng[1])
    assert a.dedup_key == a2.dedup_key


# ---- P5: byte_end >= byte_start monotonicity ---------------------------------


@given(st.integers(0, 10_000), st.integers(0, 10_000))
def test_p5_block_byte_monotonicity(s, e):
    if e >= s:
        Block(
            id="a" * 64,
            path="x.md",
            block_index=0,
            byte_start=s,
            byte_end=e,
            block_kind="paragraph",
            content_hash="a" * 64,
        )
    else:
        with pytest.raises(ValidationError):
            Block(
                id="a" * 64,
                path="x.md",
                block_index=0,
                byte_start=s,
                byte_end=e,
                block_kind="paragraph",
                content_hash="a" * 64,
            )


# ---- P6: NFC normalization is idempotent --------------------------------------


@given(SAFE_PATH)
def test_p6_path_nfc_idempotent(p):
    b = Block(
        id="a" * 64,
        path=p,
        block_index=0,
        byte_start=0,
        byte_end=1,
        block_kind="paragraph",
        content_hash="a" * 64,
    )
    assert b.path == unicodedata.normalize("NFC", b.path)
    # Second pass through is unchanged.
    b2 = Block(
        id="a" * 64,
        path=b.path,
        block_index=0,
        byte_start=0,
        byte_end=1,
        block_kind="paragraph",
        content_hash="a" * 64,
    )
    assert b2.path == b.path


# ---- P7: Identifier per-scheme accept matrix ---------------------------------

_VALID_BY_SCHEME = {
    "QID": ["Q1", "Q42", "Q12345"],
    "DOI": ["10.1038/x", "10.5555/abc.def"],
    "ORCID": ["0000-0002-1825-0097"],
    "ISBN": ["9780306406157", "0306406152"],
    "EMAIL": ["a@b.co", "alex@oktolabs.ai"],
    "DOMAIN": ["oktolabs.ai", "sub.example.com"],
}


@pytest.mark.parametrize(
    "scheme,value",
    [(s, v) for s, vs in _VALID_BY_SCHEME.items() for v in vs],
)
def test_p7_identifier_scheme_matrix(scheme, value):
    Identifier(scheme=scheme, value=value, owner_id="agent:x")


# ---- P8: Frozen mutation rejection -------------------------------------------


def test_p8_frozen(canonical_document_payload):
    d = Document(**canonical_document_payload)
    with pytest.raises(ValidationError):
        d.uri = "file:///other"


# ---- P9: Claim.id determinism fixture stub (Topic 06 owns full impl) ---------


def test_p9_claim_id_determinism_stub():
    # The schema layer is opaque to id derivation; this fixture documents the
    # contract Topic 06 must satisfy: same (block.content_hash,S,P,O) -> same
    # sha256, byte-stable separator 0x1F.
    sep = b"\x1f"
    ch = "a" * 64
    components = [ch.encode(), b"S", b"P", b"O"]
    expected = hashlib.sha256(sep.join(components)).hexdigest()
    assert len(expected) == 64
    # Stub: the model accepts the hash as opaque.
    Claim(
        id=expected,
        S_id="S",
        P="P",
        O_id="O",
        confidence=1.0,
        block_id=ch,
        extraction_activity_id="act",
        agent_id="ag",
    )
