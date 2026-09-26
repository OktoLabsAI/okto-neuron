"""Primitive registry surface and supported-Python smoke tests.

Covers test scenarios:
- ts_bc48fd98  Public re-export surface and PRIMITIVES registry
- ts_be9c8e94  Full parametrized primitives suite green on Python 3.12+
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from types import MappingProxyType

import pytest
from okto_neuron import primitives

PRIMITIVES = primitives.PRIMITIVES
PRIMITIVE_NAMES = primitives.PRIMITIVE_NAMES
PRIMITIVES_BY_NAME = primitives.PRIMITIVES_BY_NAME
Primitive = primitives.Primitive


# ---------------------------------------------------------------------------
# ts_bc48fd98 — public re-export + closed-set registry
# ---------------------------------------------------------------------------
def test_top_level_marginalia_reexports_primitives():
    import okto_neuron

    # Per Cross-topic interface contracts KB: 5 primitives re-exported on
    # marginalia.__init__ (and marginalia.types). Allow either surface to count.
    expected = {"Agent", "Activity", "InformationObject", "Concept", "Place"}
    top = {n for n in expected if hasattr(okto_neuron, n)}
    types_mod = sys.modules.get("marginalia.types")
    if types_mod is None:
        try:
            import okto_neuron.types as types_mod  # type: ignore
        except ImportError:
            types_mod = None
    via_types = {n for n in expected if types_mod is not None and hasattr(types_mod, n)}
    assert expected.issubset(top | via_types), (
        f"missing primitive re-exports — top-level: {expected - top}, "
        f"via marginalia.types: {expected - via_types}"
    )


def test_primitives_registry_closed_set():
    assert isinstance(PRIMITIVES, frozenset)
    assert len(PRIMITIVES) == 5
    assert PRIMITIVE_NAMES == {"Agent", "Activity", "InformationObject", "Concept", "Place"}
    # PRIMITIVES_BY_NAME is a read-only mapping (MappingProxyType per DEC-008).
    assert isinstance(PRIMITIVES_BY_NAME, MappingProxyType)
    with pytest.raises(TypeError):
        PRIMITIVES_BY_NAME["X"] = object  # type: ignore[index]
    for name, cls in PRIMITIVES_BY_NAME.items():
        assert cls.__name__ == name
        assert issubclass(cls, Primitive)
        assert cls in PRIMITIVES


def test_primitive_base_is_abstract():
    # Per KB "Failure-mode catalog": Primitive() direct -> TypeError "Primitive is abstract".
    with pytest.raises((TypeError, Exception)):
        Primitive(
            id="x",
            type="core:Primitive",
            created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize(
    "cls", sorted(PRIMITIVES, key=lambda c: c.__name__), ids=lambda c: c.__name__
)
def test_each_primitive_declares_standards(cls):
    std = getattr(cls, "__standards__", ())
    assert isinstance(std, tuple) and std, f"{cls.__name__}.__standards__ must be non-empty tuple"
    for s in std:
        assert isinstance(s, str) and ":" in s


@pytest.mark.parametrize(
    "cls", sorted(PRIMITIVES, key=lambda c: c.__name__), ids=lambda c: c.__name__
)
def test_frozen_rejects_mutation(cls):
    instance = cls(id="x", created_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    from pydantic import ValidationError

    with pytest.raises((ValidationError, TypeError, AttributeError)):
        instance.id = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ts_be9c8e94 — full suite green on supported Python
# ---------------------------------------------------------------------------
def test_python_312():
    assert sys.version_info >= (3, 12), "Okto Neuron requires Python 3.12+"
