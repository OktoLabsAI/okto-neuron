from __future__ import annotations


def test_ts_0e148fa5_public_import_surface() -> None:
    namespace: dict[str, object] = {}

    exec(
        """
from okto_neuron import (
    Vault,
    QueryHit,
    Provenance,
    IngestResult,
    ExportScope,
    OktoNeuronError,
    VaultError,
    VaultNotFoundError,
    VaultLockedError,
    VaultAlreadyExistsError,
    VaultClosedError,
    InvalidVaultConfigError,
    IngestError,
    FileNotUnderVaultError,
    QueryError,
    ExportError,
    ValidationError,
)
""",
        namespace,
    )

    expected = {
        "Vault",
        "QueryHit",
        "Provenance",
        "IngestResult",
        "ExportScope",
        "OktoNeuronError",
        "VaultError",
        "VaultNotFoundError",
        "VaultLockedError",
        "VaultAlreadyExistsError",
        "VaultClosedError",
        "InvalidVaultConfigError",
        "IngestError",
        "FileNotUnderVaultError",
        "QueryError",
        "ExportError",
        "ValidationError",
    }
    assert expected <= namespace.keys()
