"""Tests for the five concrete primitives (Cluster 1A — card aa323337).

Verifies default `type` CURIE, `__standards__`, `__schema_version__`,
optional `name` field, Activity temporal ordering, and Concept SKOS
invariants.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
from pydantic import ValidationError

from okto_neuron.primitives import (
    Agent,
    Activity,
    InformationObject,
    Concept,
    Place,
    Primitive,
    PRIMITIVES,
    PRIMITIVE_NAMES,
    PRIMITIVES_BY_NAME,
)


NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


# ---- defaults / standards / schema_version ------------------------------


def test_agent_defaults():
    a = Agent(id="a1", created_at=NOW)
    assert a.type == "core:Agent"
    assert Agent.__standards__ == ("prov:Agent", "crm:E39", "foaf:Agent")
    assert isinstance(Agent.__schema_version__, str) and Agent.__schema_version__


def test_activity_defaults():
    a = Activity(id="ev1", created_at=NOW)
    assert a.type == "core:Activity"
    assert Activity.__standards__ == ("prov:Activity", "crm:E7", "schema:Event")


def test_informationobject_defaults():
    i = InformationObject(id="io1", created_at=NOW)
    assert i.type == "core:InformationObject"
    assert InformationObject.__standards__ == (
        "crm:E73",
        "frbr-lrm:Work",
        "schema:CreativeWork",
    )


def test_concept_defaults():
    c = Concept(id="c1", created_at=NOW, pref_label={"en": ["thing"]})
    assert c.type == "core:Concept"
    assert Concept.__standards__ == ("skos:Concept",)


def test_place_defaults():
    p = Place(id="p1", created_at=NOW)
    assert p.type == "core:Place"
    assert Place.__standards__ == ("crm:E53", "schema:Place")


# ---- registry surface ---------------------------------------------------


def test_registry_contains_exactly_five():
    assert len(PRIMITIVES) == 5
    assert PRIMITIVE_NAMES == frozenset(
        {"Agent", "Activity", "InformationObject", "Concept", "Place"}
    )
    assert set(PRIMITIVES_BY_NAME.keys()) == PRIMITIVE_NAMES
    for name in PRIMITIVE_NAMES:
        assert issubclass(PRIMITIVES_BY_NAME[name], Primitive)


def test_primitives_by_name_is_readonly():
    with pytest.raises(TypeError):
        PRIMITIVES_BY_NAME["X"] = Agent  # type: ignore[index]


# ---- optional name field ------------------------------------------------


def test_name_optional_on_all_primitives():
    Agent(id="a", created_at=NOW, name="Alice")
    Activity(id="b", created_at=NOW, name="Conference")
    Place(id="c", created_at=NOW, name="Berlin")
    assert Agent(id="a2", created_at=NOW).name is None


# ---- Activity temporal ordering -----------------------------------------


def test_activity_started_before_ended_ok():
    s = NOW
    e = s + timedelta(hours=1)
    a = Activity(id="ev", created_at=NOW, started_at_time=s, ended_at_time=e)
    assert a.started_at_time <= a.ended_at_time


def test_activity_started_after_ended_rejected():
    s = datetime(2025, 1, 1, 5, tzinfo=timezone.utc)
    e = datetime(2025, 1, 1, 4, tzinfo=timezone.utc)
    with pytest.raises(ValidationError):
        Activity(id="ev", created_at=NOW, started_at_time=s, ended_at_time=e)


def test_activity_equal_timestamps_ok():
    Activity(id="ev", created_at=NOW, started_at_time=NOW, ended_at_time=NOW)


def test_activity_only_one_timestamp_ok():
    Activity(id="ev", created_at=NOW, started_at_time=NOW)
    Activity(id="ev2", created_at=NOW, ended_at_time=NOW)


def test_activity_naive_datetime_rejected():
    naive = datetime(2025, 1, 1)
    with pytest.raises(ValidationError):
        Activity(id="ev", created_at=NOW, started_at_time=naive)


# ---- Concept SKOS invariants --------------------------------------------


def test_concept_pref_alt_basic_ok():
    Concept(
        id="c",
        created_at=NOW,
        pref_label={"en": ["cat"], "pt": ["gato"]},
        alt_label={"en": ["feline"]},
    )


def test_concept_per_lang_dedup_rejected():
    with pytest.raises(ValidationError):
        Concept(id="c", created_at=NOW, pref_label={"en": ["cat", "cat"]})


def test_concept_pref_alt_disjoint_per_lang_rejected():
    with pytest.raises(ValidationError):
        Concept(
            id="c",
            created_at=NOW,
            pref_label={"en": ["cat"]},
            alt_label={"en": ["cat"]},
        )


def test_concept_empty_label_rejected():
    with pytest.raises(ValidationError):
        Concept(id="c", created_at=NOW, pref_label={"en": [""]})
    with pytest.raises(ValidationError):
        Concept(id="c", created_at=NOW, pref_label={"en": ["   "]})


def test_concept_bcp47_invalid_rejected():
    with pytest.raises(ValidationError):
        Concept(id="c", created_at=NOW, pref_label={"english!": ["cat"]})


def test_concept_other_language_disjointness_independent():
    Concept(
        id="c",
        created_at=NOW,
        pref_label={"en": ["cat"]},
        alt_label={"pt": ["cat"]},
    )


# ---- unknown fields rejected -------------------------------------------


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        Agent(id="a", created_at=NOW, bogus=True)
    with pytest.raises(ValidationError):
        Place(id="p", created_at=NOW, latitude=1.0)


# ---- abstract base guard ------------------------------------------------


def test_primitive_base_not_instantiable():
    with pytest.raises((TypeError, ValidationError)):
        Primitive(id="x", type="core:X", created_at=NOW)


# ---- standards in JSON schema ------------------------------------------


def test_standards_in_json_schema():
    for cls in (Agent, Activity, InformationObject, Concept, Place):
        schema = cls.model_json_schema()
        assert schema.get("x-marginalia-standards"), (
            f"{cls.__name__} missing x-marginalia-standards in schema"
        )
        assert all(isinstance(s, str) for s in schema["x-marginalia-standards"])


# ---- round-trip serialization ------------------------------------------


def test_roundtrip_each_primitive():
    import json

    instances = [
        Agent(id="a", created_at=NOW, name="Alice"),
        Activity(id="b", created_at=NOW, started_at_time=NOW),
        InformationObject(id="c", created_at=NOW),
        Concept(id="d", created_at=NOW, pref_label={"en": ["x"]}),
        Place(id="e", created_at=NOW, name="Earth"),
    ]
    for inst in instances:
        cls = type(inst)
        rt = cls.model_validate(json.loads(inst.model_dump_json()))
        assert rt == inst
