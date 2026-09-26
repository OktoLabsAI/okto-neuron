from __future__ import annotations

from pathlib import Path

import pytest

from okto_neuron import errors


EXPECTED_EXIT_CODES = {
    errors.VaultPathNotADirectory: 2,
    errors.VaultPathNotWritable: 2,
    errors.NoVaultSpecified: 2,
    errors.ConfigNotFound: 2,
    errors.ConfigParseError: 3,
    errors.ConfigVersionUnsupported: 4,
    errors.VaultLockHeld: 5,
    errors.VaultCorrupted: 6,
    errors.BootstrapPartial: 7,
    errors.IngestError: 8,
    errors.RebuildSwapFailed: 9,
    errors.SchemaVersionMismatch: 10,
    errors.RebuildAuditFailed: 15,
    errors.RebuildInterrupted: 130,
}


@pytest.mark.parametrize(("error_cls", "exit_code"), EXPECTED_EXIT_CODES.items())
def test_error_subclasses_declare_documented_exit_codes(
    error_cls: type[errors.OktoNeuronError],
    exit_code: int,
) -> None:
    assert issubclass(error_cls, errors.OktoNeuronError)
    assert error_cls.EXIT_CODE == exit_code


def test_failure_taxonomy_matches_documented_table() -> None:
    actual = [
        (row.failure, row.exception.__name__, row.exit_code) for row in errors.FAILURE_TAXONOMY
    ]
    assert actual == [
        ("vault path is a file", "VaultPathNotADirectory", 2),
        ("parent dir not writable", "VaultPathNotWritable", 2),
        ("no vault and no vault_roots", "NoVaultSpecified", 2),
        ("okto-neuron.toml malformed", "ConfigParseError", 3),
        ("okto-neuron.yaml malformed", "ConfigParseError", 3),
        ("schema version unknown", "ConfigVersionUnsupported", 4),
        ("ladybug file lock held", "VaultLockHeld", 5),
        ("ladybug corruption", "VaultCorrupted", 6),
        ("partial bootstrap", "BootstrapPartial", 7),
        ("ingest callable raised", "IngestError", 8),
        ("atomic swap failed (EXDEV)", "RebuildSwapFailed", 9),
        ("db schema_version vs code mismatch", "SchemaVersionMismatch", 10),
        ("rebuild integrity audit failed", "RebuildAuditFailed", 15),
        ("SIGINT/SIGTERM during rebuild", "RebuildInterrupted", 130),
    ]


def test_error_paths_are_absolute_in_attrs_and_user_message(tmp_path: Path) -> None:
    config_path = tmp_path / "vault" / "okto-neuron.yaml"
    cause = ValueError("bad yaml")

    error = errors.ConfigParseError(config_path, line=7, cause=cause)

    assert error.file_path == config_path.resolve()
    assert error.vault_path is None
    assert error.cause is cause
    assert error.__cause__ is cause

    message = error.user_message()
    assert str(config_path.resolve()) in message
    assert "line 7" in message


def test_config_errors_internal_path_uses_same_classes() -> None:
    from okto_neuron.config.errors import (
        ConfigNotFound,
        ConfigParseError,
        ConfigVersionUnsupported,
    )

    assert ConfigNotFound is errors.ConfigNotFound
    assert ConfigParseError is errors.ConfigParseError
    assert ConfigVersionUnsupported is errors.ConfigVersionUnsupported
