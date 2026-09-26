"""Schema emission + JSON round-trip tests for okto_neuron.primitives.

Covers test scenarios:
- ts_4f75e514  JSON schema carries x-marginalia-standards at root
- ts_4388aeb8  JSON round-trip equality across all five primitives
- ts_f34100c9  __schema_version__ is parseable semver / int on every primitive
- ts_0b6833b3  extra='forbid' rejects unknown fields

These tests exercise the implemented primitive contract directly.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from okto_neuron import primitives

PRIMITIVES = primitives.PRIMITIVES
PRIMITIVE_NAMES = primitives.PRIMITIVE_NAMES
PRIMITIVES_BY_NAME = primitives.PRIMITIVES_BY_NAME


def _minimal_kwargs() -> dict:
    return {
        "id": "urn:test:1",
        "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
    }


# ---------------------------------------------------------------------------
# ts_4f75e514 — x-marginalia-standards emission
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cls", sorted(PRIMITIVES, key=lambda c: c.__name__), ids=lambda c: c.__name__
)
def test_json_schema_carries_x_marginalia_standards(cls):
    schema = cls.model_json_schema()
    assert "x-marginalia-standards" in schema, (
        f"{cls.__name__}.model_json_schema() must expose x-marginalia-standards at root"
    )
    standards = schema["x-marginalia-standards"]
    assert isinstance(standards, list) and standards, (
        "x-marginalia-standards must be non-empty list"
    )
    assert list(standards) == list(cls.__standards__), (
        f"x-marginalia-standards mismatch for {cls.__name__}: "
        f"schema={standards!r} class={list(cls.__standards__)!r}"
    )
    for curie in standards:
        assert isinstance(curie, str) and ":" in curie, f"non-CURIE entry: {curie!r}"


# ---------------------------------------------------------------------------
# ts_4388aeb8 — JSON round-trip equality
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cls", sorted(PRIMITIVES, key=lambda c: c.__name__), ids=lambda c: c.__name__
)
def test_json_round_trip_equality(cls):
    instance = cls(**_minimal_kwargs())
    encoded = instance.model_dump_json()
    decoded = cls.model_validate_json(encoded)
    assert decoded == instance
    assert decoded.model_dump(mode="json") == instance.model_dump(mode="json")


@pytest.mark.parametrize(
    "cls", sorted(PRIMITIVES, key=lambda c: c.__name__), ids=lambda c: c.__name__
)
def test_python_round_trip_equality(cls):
    instance = cls(**_minimal_kwargs())
    dumped = instance.model_dump(mode="python")
    decoded = cls.model_validate(dumped)
    assert decoded == instance


# ---------------------------------------------------------------------------
# ts_f34100c9 — __schema_version__ parseable on every primitive
# ---------------------------------------------------------------------------
# Deep review 3.32: `marginalia.primitives` used to carry a second, dead
# module-level `__schema_version__: Final[int] = 1` alongside each concrete
# primitive class's own `__schema_version__` ClassVar (a semver string). No
# code read the module-level one, and this test — parametrized over `cls`
# but never actually touching `cls.__schema_version__` — was tautological:
# it only ever compared the module constant to itself. The per-primitive
# ClassVar is the single source of truth now; assert against *that*, on
# every primitive.
@pytest.mark.parametrize(
    "cls", sorted(PRIMITIVES, key=lambda c: c.__name__), ids=lambda c: c.__name__
)
def test_schema_version_is_parseable(cls):
    # Contract (KB "Public surface, registry, schema_version"):
    # each primitive publishes its own __schema_version__ ClassVar.
    # Accept int or dotted-semver string for forward-compat.
    sv = cls.__schema_version__
    assert sv is not None
    if isinstance(sv, int):
        assert sv >= 1
    elif isinstance(sv, str):
        parts = sv.split(".")
        assert 1 <= len(parts) <= 3
        for p in parts:
            assert p.isdigit(), f"non-numeric segment in __schema_version__: {p!r}"
    else:
        pytest.fail(f"__schema_version__ must be int or dotted-semver str, got {type(sv).__name__}")


def test_schema_version_has_single_source_of_truth() -> None:
    """Deep review 3.32: the package must not carry a second, unread
    module-level __schema_version__ alongside the per-primitive ClassVar."""
    assert not hasattr(primitives, "__schema_version__")
    assert "__schema_version__" not in primitives.__all__


# ---------------------------------------------------------------------------
# ts_0b6833b3 — extra='forbid' rejects unknown fields
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "cls", sorted(PRIMITIVES, key=lambda c: c.__name__), ids=lambda c: c.__name__
)
def test_extra_forbid_rejects_unknown_fields(cls):
    from pydantic import ValidationError

    bad = _minimal_kwargs() | {"definitely_not_a_field_xyz": 42}
    with pytest.raises(ValidationError) as excinfo:
        cls(**bad)
    # Pydantic v2 emits type='extra_forbidden'.
    assert any(err.get("type") == "extra_forbidden" for err in excinfo.value.errors()), (
        f"expected extra_forbidden error, got: {excinfo.value.errors()!r}"
    )
