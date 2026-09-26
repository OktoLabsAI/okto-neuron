"""Regression test for deep-review finding 3.28.

``ddl_statements()`` hand-interpolates ``graph_generation`` and
``identity_contract_version`` into a Cypher literal via a hand-rolled
escaper (``_cypher_string``) instead of a bound parameter — a real design
smell next to an otherwise fully parameterized module. Both values are
sourced only from ``uuid4()`` or fixed constants today, but nothing
previously stopped a value containing a stray quote from producing a
malformed (or, with a buggy escaper, injectable) DDL statement. Defense in
depth: reject any value outside a strict allow-list before it is
interpolated.
"""

from __future__ import annotations

import pytest

from okto_neuron.store import schema


def test_ddl_statements_accepts_well_formed_graph_generation_and_contract() -> None:
    statements = schema.ddl_statements(
        graph_generation="8f14e45f-ceea-4b3b-a3d4-cd2f0d0f2f9e",
        identity_contract_version=schema.CURRENT_IDENTITY_CONTRACT_VERSION,
    )
    metadata_statement = statements[-1]
    assert "8f14e45f-ceea-4b3b-a3d4-cd2f0d0f2f9e" in metadata_statement
    assert schema.CURRENT_IDENTITY_CONTRACT_VERSION in metadata_statement


def test_ddl_statements_accepts_legacy_identity_contract_version() -> None:
    # bootstrap_vault_graph re-feeds whatever identity_contract_version was
    # read back off an existing vault's schema-metadata node straight into
    # ddl_statements() on every open (see _bootstrap.py). An older vault's
    # stored value is this legacy constant, not the current one — the
    # allow-list must accept it too, or every existing legacy vault would
    # fail to open after this fix.
    statements = schema.ddl_statements(
        identity_contract_version=schema.LEGACY_IDENTITY_CONTRACT_VERSION,
    )
    assert schema.LEGACY_IDENTITY_CONTRACT_VERSION in statements[-1]


def test_ddl_statements_rejects_graph_generation_with_quote_injection_attempt() -> None:
    malicious = "x'}) DETACH DELETE (m) //"
    with pytest.raises(ValueError, match="unsafe schema metadata value"):
        schema.ddl_statements(graph_generation=malicious)


def test_ddl_statements_rejects_identity_contract_version_with_quote() -> None:
    malicious = "legacy.v0' RETURN 1 //"
    with pytest.raises(ValueError, match="unsafe schema metadata value"):
        schema.ddl_statements(identity_contract_version=malicious)


def test_ddl_statements_none_values_skip_validation() -> None:
    # None values are the default path (no graph identity yet) and must not
    # be rejected — only non-None values are validated.
    statements = schema.ddl_statements()
    assert statements[-1].startswith("MERGE (m:Node")
