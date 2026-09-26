from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from okto_neuron.errors import SchemaVersionMismatch
from okto_neuron.store import schema


EXPECTED_TABLES = (
    "Node",
    "Edge",
    "Authority",
    "Annotation",
    "Reference",
    "Work",
    "Document",
    "Block",
)
EXPECTED_VECTOR_INDEXES = {
    "Document": "document_embedding_idx",
    "Block": "block_embedding_idx",
    "Annotation": "annotation_embedding_idx",
    "Authority": "authority_embedding_idx",
}


class FakeConnection:
    def __init__(self, stored_version: object | None) -> None:
        self.stored_version = stored_version
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def execute(self, sql: str, params: dict[str, object] | None = None) -> list[list[Any]]:
        self.calls.append((sql, params))
        if self.stored_version is None:
            return []
        return [[self.stored_version]]


def test_current_schema_version_is_v1() -> None:
    assert schema.CURRENT_SCHEMA_VERSION == 1


def test_new_graph_identity_has_uuid_generation_and_current_contract() -> None:
    identity = schema.new_graph_identity()

    assert UUID(identity.graph_generation or "")
    assert identity.identity_contract_version == schema.CURRENT_IDENTITY_CONTRACT_VERSION
    assert identity.identity_contract_version == "semantic_edges.v1"
    assert identity.is_unset is False
    # A freshly minted graph declares the deterministic edge-identity contract, so
    # it is no longer legacy and the auditor's identity checks apply to it.
    assert identity.is_legacy is False
    assert schema.GraphIdentity("g", schema.LEGACY_IDENTITY_CONTRACT_VERSION).is_legacy is True
    assert schema.GraphIdentity("g", None).is_legacy is True


def test_keep_list_tables_are_exact_golden_source_of_truth() -> None:
    assert tuple(schema.KEEP_LIST) == EXPECTED_TABLES
    assert schema.table_names() == EXPECTED_TABLES
    assert set(schema.PULSE_ONLY_TABLES).isdisjoint(schema.table_names())


def test_keep_list_vector_indexes_are_exact() -> None:
    actual = {
        table_name: config["vector_index"]
        for table_name, config in schema.KEEP_LIST.items()
        if "vector_index" in config
    }
    assert actual == EXPECTED_VECTOR_INDEXES
    assert set(schema.vector_index_names()) == set(EXPECTED_VECTOR_INDEXES.values())


def test_ddl_statements_are_derived_from_keep_list_without_pulse_tables() -> None:
    ddl = schema.ddl_statements()
    ddl_text = "\n".join(ddl)

    for table_name in EXPECTED_TABLES:
        assert table_name in ddl_text
    for table_name in schema.PULSE_ONLY_TABLES:
        assert table_name not in ddl_text
    for table_name, index_name in EXPECTED_VECTOR_INDEXES.items():
        assert f"'{table_name}', '{index_name}', 'embedding'" in ddl_text

    assert schema.SCHEMA_METADATA_NODE_ID in ddl[-1]
    assert "MERGE (m:Node" in ddl[-1]


def test_schema_metadata_identity_is_only_assigned_when_explicit() -> None:
    legacy_ddl = schema.ddl_statements()[-1]
    fresh_ddl = schema.ddl_statements(
        graph_generation="1e2e916d-59a6-4bf5-93e9-51324cf697eb",
        identity_contract_version=schema.CURRENT_IDENTITY_CONTRACT_VERSION,
    )[-1]

    assert "m.graph_generation =" not in legacy_ddl
    assert "m.identity_contract_version =" not in legacy_ddl
    assert "m.graph_generation = '1e2e916d-59a6-4bf5-93e9-51324cf697eb'" in fresh_ddl
    assert "m.identity_contract_version = 'semantic_edges.v1'" in fresh_ddl


@pytest.mark.parametrize("stored_version", [None, 0, 1, "1"])
def test_verify_schema_version_accepts_missing_current_or_older_versions(
    stored_version: object | None,
) -> None:
    schema.verify_schema_version(FakeConnection(stored_version), file_path=Path("graph.lbug"))


def test_verify_schema_version_raises_for_future_schema() -> None:
    with pytest.raises(SchemaVersionMismatch) as exc_info:
        schema.verify_schema_version(FakeConnection(2), file_path=Path("graph.lbug"))

    error = exc_info.value
    assert error.EXIT_CODE == 10
    assert error.found_version == 2
    assert error.expected_version == schema.CURRENT_SCHEMA_VERSION
