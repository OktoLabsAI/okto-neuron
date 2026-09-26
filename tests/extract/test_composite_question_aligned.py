"""Composite-literal + question-aligned subject prompt tests.

Validates the claim-representation change (Alex-approved 2026-06-26):
1. BLOCK 1 — question-aligned subject selection + preserved topology edge.
2. BLOCK 2 — co-located scalars form ONE composite literal (composite-only),
   using only the ADR-0020 locked predicates (never `has_result`).
3. The independent-attributes carve-out (over-merge guard).
4. Examples 14 (composite) and 15 (question-aligned subject) are present in the
   exact worked-example style, with single composite claims.
5. Prefix stability: all of the above live in the module-level constant.
"""

from __future__ import annotations

import importlib

from okto_neuron.extract import _BASE_SYSTEM


def test_question_aligned_subject_block_present() -> None:
    assert "QUESTION-ALIGNED subject" in _BASE_SYSTEM
    # the topology-edge preservation is the load-bearing half (no info loss)
    assert "topology edge" in _BASE_SYSTEM
    assert "never invent a subject" in _BASE_SYSTEM


def test_composite_literal_block_present() -> None:
    assert "CO-LOCATED scalars form ONE composite Claim" in _BASE_SYSTEM
    # composite-only: keep a related figure-set in one literal, do not split
    assert "ONE literal rather" in _BASE_SYSTEM
    # capped literal (ADR-0016 hashes O into the claim id)
    assert "hashed into the claim" in _BASE_SYSTEM


def test_has_result_predicate_is_banned() -> None:
    """The composite rule must NOT introduce has_result (not an ADR-0020 predicate)."""
    assert "never coin a new predicate such as has_result" in _BASE_SYSTEM
    # and no example actually emits it
    assert '"predicate":"has_result"' not in _BASE_SYSTEM
    assert "has_result" not in _BASE_SYSTEM.replace(
        "never coin a new predicate such as has_result", ""
    )


def test_independent_attributes_carveout_present() -> None:
    assert "INDEPENDENT attributes stay ATOMIZED" in _BASE_SYSTEM
    assert "MUST NOT be merged into a" in _BASE_SYSTEM


def test_example_14_composite_only() -> None:
    assert "Example 14" in _BASE_SYSTEM
    # one composite literal carries the whole figure-set
    assert "2,400 files, 80 albums, 0 errors imported" in _BASE_SYSTEM


def test_example_15_question_aligned_subject() -> None:
    assert "Example 15" in _BASE_SYSTEM
    # subject re-pointed to the queryable entity, framing preserved as an edge
    assert "28% of committed code is AI-generated" in _BASE_SYSTEM
    assert "generates_code_for" in _BASE_SYSTEM


def test_stable_subject_over_structural_label_block_present() -> None:
    """Fix B (Alex-approved 2026-06-28): dense facts bind to a durable entity,
    not a document-structure label (folder/phase/file/heading)."""
    assert "STABLE subject over STRUCTURAL label" in _BASE_SYSTEM
    # the durable-entity examples and the structural-label examples both named
    assert "phase/step heading" in _BASE_SYSTEM or "phase/step" in _BASE_SYSTEM
    # Fix A's job is preserved: keep a structural label when nothing better exists
    assert "no durable entity is in scope" in _BASE_SYSTEM
    # still grounded in the question-aligned principle
    assert "the node a reader would name" in _BASE_SYSTEM


def test_example_16_stable_subject_migration_count() -> None:
    assert "Example 16" in _BASE_SYSTEM
    # the migration count binds to PhotoShelf (durable), not the batch label
    assert "3,200 files migrated from Rowan/Photos" in _BASE_SYSTEM
    assert '"subject":"PhotoShelf"' in _BASE_SYSTEM
    # and never to the phase label as a claim subject
    assert '"subject":"Batch 3' not in _BASE_SYSTEM


def test_dense_fact_examples_use_only_locked_predicates() -> None:
    """Every has_* predicate in the worked examples is an ADR-0020 locked one.

    Relational claims legitimately use free-form predicates (impact, example,
    generates_code_for, ...); only the dense-fact `has_*` family is locked.
    """
    locked = {"has_version", "has_config", "has_status", "has_measurement", "has_value"}
    import re

    for pred in re.findall(r'"predicate":"(has_[a-z_]+)"', _BASE_SYSTEM):
        assert pred in locked, f"example uses non-locked dense predicate {pred!r}"


def test_blocks_survive_reimport() -> None:
    """Prefix stability: the additions are module-level constant text, not per-call.

    Uses a cached re-import (NOT importlib.reload, which pollutes sibling test
    modules that imported from okto_neuron.extract).
    """
    reimported = importlib.import_module("okto_neuron.extract")
    assert "QUESTION-ALIGNED subject" in reimported._BASE_SYSTEM
    assert "CO-LOCATED scalars form ONE composite Claim" in reimported._BASE_SYSTEM
