"""Invariants for the five Okto Neuron primitives.

Covers test scenarios 1-10 from spec 70bdbfab:

- ts_9b7d0365 abstract-base rejection
- ts_46c624e2 default CURIE types per subclass
- ts_fdb3bb72 id format invariants
- ts_06c2d415 CURIE rejection at construction
- ts_419e5356 CURIE rejection at __init_subclass__
- ts_567d629d BCP47 validate
- ts_9e3d374c BCP47 normalize
- ts_0c211513 naive-datetime rejection on Activity
- ts_15164680 Activity temporal ordering
- ts_1bab3955 Concept SKOS label invariants

Implementations live in `okto_neuron.primitives`. Run directly with::

    pytest tests/primitives/test_invariants.py -q
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError


# --------------------------------------------------------------------------- #
# Imports under test                                                          #
# --------------------------------------------------------------------------- #

from okto_neuron.primitives import (  # noqa: E402
    Primitive,
    Agent,
    Activity,
    InformationObject,
    Concept,
    Place,
)
from okto_neuron.primitives._lang import normalize_bcp47 as normalize  # noqa: E402

# Use the production validator here. A local bool-returning regex helper cannot
# satisfy the rejection contract, which requires Pydantic ValidationError codes.
from okto_neuron.primitives._lang import validate  # noqa: E402


SUBCLASSES = [Agent, Activity, InformationObject, Concept, Place]
DEFAULT_TYPES = {
    Agent: "core:Agent",
    Activity: "core:Activity",
    InformationObject: "core:InformationObject",
    Concept: "core:Concept",
    Place: "core:Place",
}


def _first_error_code(exc: ValidationError) -> str:
    errors = exc.errors()
    assert errors, "ValidationError carried no errors"
    err = errors[0]
    # pydantic-v2 surfaces custom code under "ctx" or as the error type;
    # be permissive about which slot the impl uses.
    ctx = err.get("ctx") or {}
    return ctx.get("code") or err.get("type", "")


def _has_error_code(exc: ValidationError, code: str) -> bool:
    for err in exc.errors():
        ctx = err.get("ctx") or {}
        if ctx.get("code") == code or err.get("type") == code:
            return True
        # Also accept code embedded in the message — some impls put it there.
        if code in (err.get("msg") or ""):
            return True
    return False


# --------------------------------------------------------------------------- #
# ts_9b7d0365 — abstract-base rejection                                       #
# --------------------------------------------------------------------------- #


def test_ts_9b7d0365_primitive_base_rejects_direct_instantiation():
    with pytest.raises(ValidationError) as excinfo:
        Primitive(id="x", type="ex:Thing")
    assert _has_error_code(excinfo.value, "base_abstract_instantiation")
    assert "abstract" in str(excinfo.value).lower()


# --------------------------------------------------------------------------- #
# ts_46c624e2 — default CURIE types per subclass                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cls", SUBCLASSES, ids=[c.__name__ for c in SUBCLASSES])
def test_ts_46c624e2_default_curie_type(cls):
    # Activity needs tz-aware temporals if started_at is required; try minimum
    # construction first and fall back to a minimal-valid kwargs map.
    kwargs = {"id": "x"}
    if cls is Activity:
        # Activity may require started_at; supply tz-aware minimum if so.
        try:
            inst = cls(**kwargs)
        except ValidationError:
            now = datetime.now(timezone.utc)
            inst = cls(id="x", started_at=now)
    elif cls is Concept:
        try:
            inst = cls(**kwargs)
        except ValidationError:
            inst = cls(id="x", pref_label={"en": ["Thing"]})
    else:
        inst = cls(**kwargs)
    assert inst.model_dump()["type"] == DEFAULT_TYPES[cls]


# --------------------------------------------------------------------------- #
# ts_fdb3bb72 — id format invariants                                          #
# --------------------------------------------------------------------------- #

INVALID_IDS = ["", "   ", "a\x07c", "a b", "x" * 513]


@pytest.mark.parametrize("cls", SUBCLASSES, ids=[c.__name__ for c in SUBCLASSES])
@pytest.mark.parametrize(
    "bad_id", INVALID_IDS, ids=["empty", "whitespace", "control", "space", "too_long"]
)
def test_ts_fdb3bb72_id_format_invariants(cls, bad_id):
    kwargs = {"id": bad_id}
    if cls is Activity:
        kwargs["started_at"] = datetime.now(timezone.utc)
    elif cls is Concept:
        kwargs["pref_label"] = {"en": ["Thing"]}
    with pytest.raises(ValidationError) as excinfo:
        cls(**kwargs)
    assert _has_error_code(excinfo.value, "id_invalid_format")


# --------------------------------------------------------------------------- #
# ts_06c2d415 — CURIE rejection at construction                               #
# --------------------------------------------------------------------------- #

INVALID_CURIES = ["NotACurie", "foo:", ":bar", "1bad:value"]


@pytest.mark.parametrize("cls", SUBCLASSES, ids=[c.__name__ for c in SUBCLASSES])
@pytest.mark.parametrize("bad_type", INVALID_CURIES)
def test_ts_06c2d415_curie_rejection_at_construction(cls, bad_type):
    kwargs = {"id": "x", "type": bad_type}
    if cls is Activity:
        kwargs["started_at"] = datetime.now(timezone.utc)
    elif cls is Concept:
        kwargs["pref_label"] = {"en": ["Thing"]}
    with pytest.raises(ValidationError) as excinfo:
        cls(**kwargs)
    assert _has_error_code(excinfo.value, "curie_invalid")


# --------------------------------------------------------------------------- #
# ts_419e5356 — CURIE rejection at __init_subclass__                          #
# --------------------------------------------------------------------------- #


def test_ts_419e5356_curie_rejection_at_init_subclass():
    with pytest.raises(TypeError) as excinfo:

        class Bogus(Primitive):  # noqa: F841
            __standards__ = ("not a curie",)

    assert "not a curie" in str(excinfo.value) or "curie" in str(excinfo.value).lower()


# --------------------------------------------------------------------------- #
# ts_567d629d — BCP47 validate                                                #
# --------------------------------------------------------------------------- #

VALID_TAGS = ["en-US", "pt-BR", "zh-Hant-TW"]
INVALID_TAGS = ["english", "en_US", ""]


@pytest.mark.parametrize("tag", VALID_TAGS)
def test_ts_567d629d_bcp47_validate_accepts(tag):
    # validate returns normally for valid tags (return value unspecified).
    validate(tag)


@pytest.mark.parametrize("tag", INVALID_TAGS)
def test_ts_567d629d_bcp47_validate_rejects(tag):
    with pytest.raises(ValidationError) as excinfo:
        validate(tag)
    assert _has_error_code(excinfo.value, "lang_invalid_bcp47")


# --------------------------------------------------------------------------- #
# ts_9e3d374c — BCP47 normalize                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [("EN-us", "en-US"), ("zh-hant-tw", "zh-Hant-TW")],
)
def test_ts_9e3d374c_bcp47_normalize(raw, expected):
    assert normalize(raw) == expected


# --------------------------------------------------------------------------- #
# ts_0c211513 — naive-datetime rejection on Activity                          #
# --------------------------------------------------------------------------- #


def test_ts_0c211513_naive_datetime_rejected():
    with pytest.raises(ValidationError) as excinfo:
        Activity(id="a", started_at=datetime(2025, 1, 1))
    assert _has_error_code(excinfo.value, "datetime_not_tz_aware")


# --------------------------------------------------------------------------- #
# ts_15164680 — Activity temporal ordering                                    #
# --------------------------------------------------------------------------- #


def test_ts_15164680_activity_temporal_ordering():
    t1 = datetime(2025, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2025, 1, 2, 0, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValidationError) as excinfo:
        Activity(id="a", started_at=t2, ended_at=t1)
    assert _has_error_code(excinfo.value, "temporal_order_violation")


# --------------------------------------------------------------------------- #
# ts_1bab3955 — Concept SKOS label invariants                                 #
# --------------------------------------------------------------------------- #


def test_ts_1bab3955_label_duplicate_in_lang():
    with pytest.raises(ValidationError) as excinfo:
        Concept(id="c", pref_label={"en": ["Cat", "Cat"]})
    assert _has_error_code(excinfo.value, "label_duplicate_in_lang")


def test_ts_1bab3955_label_pref_alt_overlap():
    with pytest.raises(ValidationError) as excinfo:
        Concept(id="c", pref_label={"en": ["Cat"]}, alt_label={"en": ["Cat"]})
    assert _has_error_code(excinfo.value, "label_pref_alt_overlap")


def test_ts_1bab3955_label_empty():
    with pytest.raises(ValidationError) as excinfo:
        Concept(id="c", pref_label={"en": [""]})
    assert _has_error_code(excinfo.value, "label_empty")
