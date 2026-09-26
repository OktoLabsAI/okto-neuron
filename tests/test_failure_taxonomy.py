from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
from typing import Callable

import click
from click.testing import CliRunner
import pytest

from okto_neuron.cli import OktoNeuronGroup
from okto_neuron.config import OktoNeuronConfig, VaultConfig
import okto_neuron.errors as errors
import okto_neuron.store._bootstrap as bootstrap_module
from okto_neuron.store import schema
from okto_neuron.store import vault as store_vault_module
from okto_neuron.store.ladybug import VaultConnection
from okto_neuron.store.vault import _open_vault

Inducer = Callable[[Path, pytest.MonkeyPatch], None]


@pytest.fixture(autouse=True)
def close_graph_handles() -> None:
    yield
    for store in list(store_vault_module._STORE_CACHE.values()):
        store.close()
    store_vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()
    for handle in list(bootstrap_module._bootstrap_cache.values()):
        handle.close()
    bootstrap_module._bootstrap_cache.clear()


def test_inducer_table_covers_failure_taxonomy() -> None:
    assert len(errors.FAILURE_TAXONOMY) == 14
    assert set(_INDUCERS) == {row.failure for row in errors.FAILURE_TAXONOMY}


@pytest.mark.parametrize("row", errors.FAILURE_TAXONOMY, ids=lambda row: row.failure)
def test_failure_taxonomy_exit_code_matrix(
    row: errors.FailureTaxonomyRow,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(row.exception) as raised:
        _INDUCERS[row.failure](tmp_path, monkeypatch)

    error = raised.value
    assert type(error) is row.exception
    assert error.EXIT_CODE == row.exit_code

    user_message = error.user_message()
    assert user_message.startswith(f"{row.exception.__name__}: ")
    assert "\n" not in user_message
    _assert_path_attrs_are_absolute_and_rendered(error)

    result = _dispatch_through_cli_handler(error)
    stderr = result.stderr or result.output
    assert result.exit_code == row.exit_code
    assert stderr == f"{user_message}\n"


def _dispatch_through_cli_handler(error: errors.OktoNeuronError) -> click.testing.Result:
    @click.group(cls=OktoNeuronGroup)
    def cli() -> None:
        pass

    @cli.command()
    def fail() -> None:
        raise error

    return CliRunner().invoke(cli, ["fail"])


def _assert_path_attrs_are_absolute_and_rendered(error: errors.OktoNeuronError) -> None:
    message = error.user_message()
    for attr in ("file_path", "vault_path", "source_path", "target_path"):
        path = getattr(error, attr, None)
        if path is None:
            continue
        assert isinstance(path, Path)
        assert path.is_absolute()
        assert str(path) in message


def _induce_vault_path_is_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch
    vault_path = tmp_path / "vault-file"
    vault_path.write_text("not a directory", encoding="utf-8")

    _open_vault(vault_path)


def _induce_parent_dir_not_writable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = tmp_path / "blocked"
    parent.mkdir()
    monkeypatch.setattr(store_vault_module.os, "access", lambda path, mode: False)

    _open_vault(parent / "vault")


def _induce_no_vault_and_no_vault_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del tmp_path, monkeypatch
    raise errors.NoVaultSpecified()


def _induce_marginalia_toml_malformed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del monkeypatch
    config_path = tmp_path / "okto-neuron.toml"
    config_path.write_text("marginalia_toml_version = 1\nbad = \n", encoding="utf-8")

    OktoNeuronConfig.load(config_path)


def _induce_marginalia_yaml_malformed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del monkeypatch
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    (vault_path / "okto-neuron.yaml").write_text(
        "marginalia_yaml_version: 1\nbad:\n\tchild: value\n",
        encoding="utf-8",
    )

    VaultConfig.load(vault_path)


def _induce_schema_version_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch
    vault_path = tmp_path / "vault"
    vault_path.mkdir()
    (vault_path / "okto-neuron.yaml").write_text(
        "marginalia_yaml_version: 99\npacks:\n  - core\n",
        encoding="utf-8",
    )

    VaultConfig.load(vault_path)


def _induce_ladybug_file_lock_held(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch
    vault_path = tmp_path / "vault"
    lock_path = vault_path / ".marginalia" / ".bootstrap.lock"
    lock_path.parent.mkdir(parents=True)

    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        os.fsync(lock_file.fileno())
        try:
            bootstrap_module.bootstrap_vault_graph(vault_path)
        finally:
            lock_file.seek(0)
            lock_file.truncate()
            lock_file.flush()
            os.fsync(lock_file.fileno())
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _induce_ladybug_corruption(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch
    vault_path = tmp_path / "vault"
    graph_path = vault_path / "graph.lbug"
    graph_path.parent.mkdir()
    graph_path.write_text("not a ladybug graph", encoding="utf-8")

    raise errors.VaultCorrupted(
        graph_path,
        vault_path=vault_path,
        cause=RuntimeError("invalid ladybug graph header"),
    )


def _induce_partial_bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    vault_path = tmp_path / "vault"
    monkeypatch.setattr(
        bootstrap_module.schema,
        "ddl_statements",
        lambda: (
            "CREATE NODE TABLE IF NOT EXISTS Node (id STRING PRIMARY KEY)",
            "NOT CYPHER",
        ),
    )

    bootstrap_module.bootstrap_vault_graph(vault_path)


def _induce_ingest_callable_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch
    vault_path = tmp_path / "vault"
    note_path = tmp_path / "note.md"

    raise errors.IngestError(
        note_path,
        vault_path=vault_path,
        cause=RuntimeError("ingest callable raised"),
    )


def _induce_atomic_swap_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch
    vault_path = tmp_path / "vault"
    raise errors.RebuildSwapFailed(
        vault_path,
        source_path=vault_path / "graph.lbug.tmp",
        target_path=vault_path / "graph.lbug",
        cause=OSError(errno.EXDEV, "Invalid cross-device link"),
    )


class _FutureSchemaConnection:
    def execute(self, sql: str, params: dict[str, object] | None = None) -> list[list[int]]:
        del sql, params
        return [[schema.CURRENT_SCHEMA_VERSION + 1]]


def _induce_schema_version_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch
    schema.verify_schema_version(
        _FutureSchemaConnection(),
        file_path=tmp_path / "vault" / "graph.lbug",
    )


def _induce_rebuild_audit_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch
    vault_path = tmp_path / "vault"
    raise errors.RebuildAuditFailed(
        vault_path,
        staging_path=vault_path / "staging.failed.lbug",
        audit_status="failed",
    )


def _induce_rebuild_interrupted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    del monkeypatch
    raise errors.RebuildInterrupted(tmp_path / "vault", signal_name="SIGINT")


_INDUCERS: dict[str, Inducer] = {
    "vault path is a file": _induce_vault_path_is_a_file,
    "parent dir not writable": _induce_parent_dir_not_writable,
    "no vault and no vault_roots": _induce_no_vault_and_no_vault_roots,
    "okto-neuron.toml malformed": _induce_marginalia_toml_malformed,
    "okto-neuron.yaml malformed": _induce_marginalia_yaml_malformed,
    "schema version unknown": _induce_schema_version_unknown,
    "ladybug file lock held": _induce_ladybug_file_lock_held,
    "ladybug corruption": _induce_ladybug_corruption,
    "partial bootstrap": _induce_partial_bootstrap,
    "ingest callable raised": _induce_ingest_callable_raised,
    "atomic swap failed (EXDEV)": _induce_atomic_swap_failed,
    "db schema_version vs code mismatch": _induce_schema_version_mismatch,
    "rebuild integrity audit failed": _induce_rebuild_audit_failed,
    "SIGINT/SIGTERM during rebuild": _induce_rebuild_interrupted,
}
