"""L1 dense-fact prompt directive tests (ADR 0019 Phase 2).

Validates that:
1. _BASE_SYSTEM contains the mandatory dense-fact imperative directive.
2. _BASE_SYSTEM contains the ADR-0020 predicate vocabulary names.
3. _BASE_SYSTEM contains the two dense-fact worked examples (Example 10/11).
4. Prefix stability: the directive is part of the constant string, not an
   interpolated per-call variable (verified by checking it survives a second
   import — the string is module-level, not generated at call time).
5. _is_metadata_predicate does NOT drop has_config / has_status / has_version /
   has_measurement / has_value (the dense-fact predicates must survive curation).
"""

from __future__ import annotations

import importlib

import pytest

from okto_neuron.extract import _BASE_SYSTEM, _is_metadata_predicate


# ── 1. Imperative directive present ──────────────────────────────────────────


def test_dense_fact_mandatory_directive_present() -> None:
    """The word MANDATORY and the dense-fact instruction must be in _BASE_SYSTEM."""
    assert "MANDATORY dense-fact rule" in _BASE_SYSTEM, (
        "_BASE_SYSTEM is missing the mandatory dense-fact directive (L1)"
    )


def test_dense_fact_directive_references_key_surfaces() -> None:
    """The directive must call out the key dense surfaces by name."""
    for phrase in (
        "version strings",
        "config key=value",
        "status labels",
        "measurements with units",
        "quantitative values",
    ):
        assert phrase in _BASE_SYSTEM, f"Dense-fact directive missing surface mention: {phrase!r}"


# ── 2. ADR-0020 predicate vocabulary present ─────────────────────────────────


@pytest.mark.parametrize(
    "predicate",
    [
        "has_version",
        "has_config",
        "has_status",
        "has_measurement",
        "has_value",
    ],
)
def test_adr0020_predicate_in_base_system(predicate: str) -> None:
    """Every ADR-0020 dense-fact predicate must appear in _BASE_SYSTEM."""
    assert predicate in _BASE_SYSTEM, f"ADR-0020 predicate {predicate!r} not found in _BASE_SYSTEM"


# ── 3. Dense-fact worked examples present ────────────────────────────────────


def test_dense_fact_example_10_present() -> None:
    """Example 10 (version + config) must be in _BASE_SYSTEM."""
    assert "Example 10 (dense-fact: version + config)" in _BASE_SYSTEM
    assert "WidgetService" in _BASE_SYSTEM
    assert '"has_version"' in _BASE_SYSTEM
    assert '"has_config"' in _BASE_SYSTEM
    assert '"has_status"' in _BASE_SYSTEM


def test_dense_fact_example_11_present() -> None:
    """Example 11 (measurement + value) must be in _BASE_SYSTEM."""
    assert "Example 11 (dense-fact: measurement + value)" in _BASE_SYSTEM
    assert "ModelX" in _BASE_SYSTEM
    assert '"has_measurement"' in _BASE_SYSTEM
    assert '"has_value"' in _BASE_SYSTEM


# ── 3b. Prose-embedded dense-fact directive + examples (L1 extension) ─────────


def test_prose_embedded_directive_present() -> None:
    """The directive must explicitly cover prose-embedded scalars + relational prose.

    Diagnosed failure mode: structured facts mint exhaustively but scalars inside
    flowing sentences / capability bullets and relational prose were silently dropped.
    """
    assert "PROSE-EMBEDDED facts count too" in _BASE_SYSTEM
    assert "RELATIONAL" in _BASE_SYSTEM
    # subject-from-context instruction (don't drop when subject isn't in-line)
    assert "named only in a nearby heading or an earlier sentence" in _BASE_SYSTEM


def test_dense_fact_example_12_prose_scalar_present() -> None:
    """Example 12 (prose-embedded scalar, subject from heading) must be present."""
    assert "Example 12 (dense-fact: prose-embedded scalar" in _BASE_SYSTEM
    assert "24 subtitle providers" in _BASE_SYSTEM


def test_dense_fact_example_13_relational_prose_present() -> None:
    """Example 13 (relational prose between named entities) must be present."""
    assert "Example 13 (dense-fact: relational prose" in _BASE_SYSTEM
    assert "one-way sync mirrors" in _BASE_SYSTEM
    assert '"mirrors"' in _BASE_SYSTEM  # relational edge, not just bare nodes


# ── 4. Prefix stability ───────────────────────────────────────────────────────


def test_base_system_is_module_level_constant() -> None:
    """_BASE_SYSTEM must be a plain str constant (not a function or lazy value).

    Prefix-cache stability requires the system prompt to be identical across
    calls. If it were generated per-call, cache reuse would break.
    """
    import okto_neuron.extract as ext_mod

    assert isinstance(ext_mod._BASE_SYSTEM, str), (
        "_BASE_SYSTEM must be a str, not a callable or property"
    )
    # Re-importing the module must yield the identical object (module cache).
    reimported = importlib.import_module("okto_neuron.extract")
    assert reimported._BASE_SYSTEM is ext_mod._BASE_SYSTEM, (
        "_BASE_SYSTEM is not the same object on re-import — indicates per-call generation"
    )


def test_dense_fact_directive_in_stable_prefix() -> None:
    """The dense-fact directive must precede the per-example section.

    Examples can vary in future; the directive is in the immutable prefix
    (before Example 1) so the cache-able prefix stays stable.
    """
    directive_pos = _BASE_SYSTEM.find("MANDATORY dense-fact rule")
    example1_pos = _BASE_SYSTEM.find("Example 1 (topology")
    assert directive_pos != -1, "MANDATORY dense-fact directive not found"
    assert example1_pos != -1, "Example 1 not found"
    assert directive_pos < example1_pos, (
        "Dense-fact directive must appear BEFORE the examples section "
        f"(directive at {directive_pos}, example1 at {example1_pos})"
    )


# ── 5. _is_metadata_predicate does NOT drop dense-fact predicates ─────────────


@pytest.mark.parametrize(
    "predicate",
    [
        "has_version",
        "has_config",
        "has_status",
        "has_measurement",
        "has_value",
    ],
)
def test_metadata_predicate_does_not_drop_dense_fact_predicates(predicate: str) -> None:
    """Dense-fact predicates must NOT be classified as metadata — they carry content.

    _is_metadata_predicate returning True would cause the curator to silently
    drop the Claims minted via the L1 dense-fact directive, masking any coverage
    lift. All five ADR-0020 predicates must return False.
    """
    assert not _is_metadata_predicate(predicate), (
        f"_is_metadata_predicate({predicate!r}) returned True — "
        f"this would silently drop dense-fact Claims at curation time"
    )
