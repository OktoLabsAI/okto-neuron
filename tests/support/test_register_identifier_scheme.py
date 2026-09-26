"""ts_cd9b6293 — register_identifier_scheme runtime extension.

Pack-extension hook per dec_57ecd6da. Without override, redefining a scheme
fails. With override=True, redefinition succeeds.
"""

from __future__ import annotations

import pytest

from okto_neuron.schema.support import (
    IDENTIFIER_REGISTRY,
    Identifier,
    register_identifier_scheme,
)


@pytest.fixture(autouse=True)
def _registry_isolation():
    # Snapshot before, restore after — keep tests hermetic.
    saved = dict(IDENTIFIER_REGISTRY)
    try:
        yield
    finally:
        IDENTIFIER_REGISTRY.clear()
        IDENTIFIER_REGISTRY.update(saved)


def test_register_new_scheme_then_validate():
    def validator(value: str) -> str:
        if not value.startswith("X-"):
            raise ValueError("must start with X-")
        return value

    register_identifier_scheme("XCODE", validator)
    Identifier(scheme="XCODE", value="X-42", owner_id="agent:x")


def test_register_duplicate_rejected_without_override():
    with pytest.raises(ValueError):
        register_identifier_scheme("QID", lambda v: v)


def test_register_duplicate_accepted_with_override():
    register_identifier_scheme("QID", lambda v: "Q-OVERRIDE", override=True)
    i = Identifier(scheme="QID", value="anything", owner_id="agent:x")
    assert i.value == "Q-OVERRIDE"


def test_register_rejects_invalid_inputs():
    with pytest.raises(ValueError):
        register_identifier_scheme("", lambda v: v)
    with pytest.raises(ValueError):
        register_identifier_scheme("NEW", "not-callable")  # type: ignore[arg-type]
