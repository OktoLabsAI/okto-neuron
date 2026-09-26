from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import ladybug

from okto_neuron.store.schema import KEEP_LIST
from okto_neuron.store._bootstrap import bootstrap_vault_graph, reset_bootstrap_cache_for_tests


GOLDEN_PATH = Path(__file__).with_name("golden") / "schema_tables_v1.txt"
REGEN_ENV_VAR = "OKTO_NEURON_REGEN_GOLDEN"
SHOW_TABLES_EQUIVALENT = "CALL show_tables() RETURN *"
CATALOG_TABLE_TYPES = {"BASE TABLE", "NODE", "REL"}


def test_bootstrapped_catalog_tables_match_keep_list_golden(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    handle = bootstrap_vault_graph(vault_path)

    try:
        actual_table_names = _catalog_table_names(handle.database)
    finally:
        reset_bootstrap_cache_for_tests(vault_path)

    expected_table_names = set(KEEP_LIST.keys())
    expected_sorted_names = sorted(KEEP_LIST.keys())
    actual_sorted_names = sorted(actual_table_names)

    assert actual_table_names == expected_table_names
    assert actual_sorted_names == expected_sorted_names

    expected_golden_bytes = _golden_bytes(expected_sorted_names)
    if os.environ.get(REGEN_ENV_VAR) == "1":
        GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN_PATH.write_bytes(expected_golden_bytes)

    assert GOLDEN_PATH.read_bytes() == expected_golden_bytes
    assert _golden_bytes(actual_sorted_names) == GOLDEN_PATH.read_bytes()


def _catalog_table_names(database: ladybug.Database) -> set[str]:
    connection = ladybug.Connection(database)
    try:
        result = connection.execute(SHOW_TABLES_EQUIVALENT)
        try:
            columns = _column_names(result)
            rows = [_row_mapping(row, columns) for row in _result_rows(result)]
        finally:
            close = getattr(result, "close", None)
            if callable(close):
                close()
    finally:
        connection.close()

    return {
        str(row["name"])
        for row in rows
        if str(row.get("type", "")).strip().upper() in CATALOG_TABLE_TYPES
    }


def _column_names(result: Any) -> tuple[str, ...]:
    get_column_names = getattr(result, "get_column_names", None)
    if not callable(get_column_names):
        return ()
    return tuple(str(name) for name in get_column_names())


def _result_rows(result: Any) -> list[Any]:
    fetch_all = getattr(result, "fetch_all", None)
    if callable(fetch_all):
        return list(fetch_all())

    rows: list[Any] = []
    has_next = getattr(result, "has_next", None)
    get_next = getattr(result, "get_next", None)
    if callable(has_next) and callable(get_next):
        while has_next():
            rows.append(get_next())
    return rows


def _row_mapping(row: Any, columns: tuple[str, ...]) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    if isinstance(row, Sequence) and not isinstance(row, (str, bytes, bytearray)):
        return dict(zip(columns, row))
    raise TypeError(f"unsupported SHOW TABLES row: {row!r}")


def _golden_bytes(table_names: Sequence[str]) -> bytes:
    return ("\n".join(table_names) + "\n").encode("utf-8")
