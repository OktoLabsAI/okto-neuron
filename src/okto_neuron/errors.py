"""Typed failure taxonomy for vault and config operations."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, Final, NamedTuple

from pydantic import ValidationError

PathInput = Path | str


def _absolute_path(path: PathInput | None) -> Path | None:
    if path is None:
        return None
    return Path(path).expanduser().resolve(strict=False)


def _one_line(value: object) -> str:
    return " ".join(str(value).split())


def _looks_like_message(
    value: PathInput | None,
    *,
    vault_path: PathInput | None,
    cause: Exception | None,
) -> bool:
    if not isinstance(value, str) or vault_path is not None or cause is not None:
        return False
    return not any(marker in value for marker in ("/", "\\", "."))


class OktoNeuronError(Exception):
    """Base exception for expected Okto Neuron failures surfaced by the CLI."""

    EXIT_CODE: ClassVar[int] = 1
    default_message: ClassVar[str] = "Okto Neuron operation failed"

    def __init__(
        self,
        message: str | None = None,
        *,
        vault_path: PathInput | None = None,
        file_path: PathInput | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.vault_path = _absolute_path(vault_path)
        self.file_path = _absolute_path(file_path)
        self.cause = cause
        super().__init__(self.message)
        if cause is not None:
            self.__cause__ = cause

    def user_message(self) -> str:
        """Return a stderr-ready message with the relevant absolute path."""
        details = [_one_line(self.message)]
        if self.file_path is not None:
            location = f"file: {self.file_path}"
            line = getattr(self, "line", None)
            if line is not None:
                location = f"{location}: line {line}"
            details.append(location)
        if self.vault_path is not None:
            details.append(f"vault: {self.vault_path}")
        if self.cause is not None:
            details.append(f"cause: {_one_line(self.cause)}")
        return f"{self.__class__.__name__}: {'; '.join(details)}"


class OptionalDependencyError(OktoNeuronError):
    """An explicitly optional public feature is missing its install extra."""

    EXIT_CODE: ClassVar[int] = 2
    default_message: ClassVar[str] = "optional feature dependencies are not installed"


class VaultPathNotADirectory(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 2
    default_message: ClassVar[str] = "vault path is a file, not a directory"

    def __init__(
        self,
        vault_path: PathInput,
        *,
        message: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, vault_path=vault_path, cause=cause)


class VaultPathNotWritable(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 2
    default_message: ClassVar[str] = "vault parent directory is not writable"

    def __init__(
        self,
        vault_path: PathInput,
        *,
        message: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, vault_path=vault_path, cause=cause)


class NoVaultSpecified(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 2
    default_message: ClassVar[str] = "no vault was specified and no vault_roots are configured"

    def __init__(
        self,
        *,
        vault_path: PathInput | None = None,
        file_path: PathInput | None = None,
        message: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, vault_path=vault_path, file_path=file_path, cause=cause)


class ConfigNotFound(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 2
    default_message: ClassVar[str] = "configuration file was not found"

    def __init__(
        self,
        file_path: PathInput,
        *,
        message: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, file_path=file_path, cause=cause)


class ConfigParseError(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 3
    default_message: ClassVar[str] = "configuration file is malformed"

    def __init__(
        self,
        file_path: PathInput,
        line: int | None = None,
        cause: Exception | None = None,
        *,
        message: str | None = None,
    ) -> None:
        self.line = line
        super().__init__(message, file_path=file_path, cause=cause)


class ConfigVersionUnsupported(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 4
    default_message: ClassVar[str] = "configuration version is unsupported"

    def __init__(
        self,
        file_path: PathInput,
        found_version: int | str | None,
        supported_versions: tuple[int, ...] | list[int] = (1,),
        *,
        message: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.found_version = found_version
        self.supported_versions = tuple(supported_versions)
        if message is None:
            supported = ", ".join(str(version) for version in self.supported_versions)
            message = (
                f"configuration version {found_version!r} is unsupported; supported: {supported}"
            )
        super().__init__(message, file_path=file_path, cause=cause)


class VaultLockHeld(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 5
    default_message: ClassVar[str] = "ladybug file lock is held"

    def __init__(
        self,
        vault_path: PathInput,
        *,
        holding_pid: int | None = None,
        message: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.holding_pid = holding_pid
        if message is None and holding_pid is not None:
            message = f"ladybug file lock is held by pid {holding_pid}"
        super().__init__(message, vault_path=vault_path, cause=cause)


class VaultError(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 11
    exit_code: ClassVar[int] = 11
    default_message: ClassVar[str] = "vault operation failed"

    def __init__(
        self,
        vault_path: PathInput | None = None,
        *,
        file_path: PathInput | None = None,
        message: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, vault_path=vault_path, file_path=file_path, cause=cause)


class VaultNotFoundError(VaultError):
    default_message: ClassVar[str] = "vault path was not found"


class VaultLockedError(VaultError):
    """Public open-time lock error, distinct from internal bootstrap VaultLockHeld."""

    default_message: ClassVar[str] = "vault lock is held"


class VaultAlreadyExistsError(VaultError):
    default_message: ClassVar[str] = "vault already exists"


class VaultClosedError(VaultError):
    default_message: ClassVar[str] = "vault is closed"


class InvalidVaultConfigError(VaultError):
    default_message: ClassVar[str] = "vault configuration is malformed"

    def __init__(
        self,
        file_path: PathInput,
        *,
        vault_path: PathInput | None = None,
        message: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(
            vault_path,
            file_path=file_path,
            message=message,
            cause=cause,
        )


class VaultCorrupted(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 6
    default_message: ClassVar[str] = "ladybug graph is corrupted"

    def __init__(
        self,
        file_path: PathInput | None = None,
        *,
        vault_path: PathInput | None = None,
        message: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, vault_path=vault_path, file_path=file_path, cause=cause)


class BootstrapPartial(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 7
    default_message: ClassVar[str] = "vault bootstrap only partially completed"

    def __init__(
        self,
        vault_path: PathInput,
        *,
        file_path: PathInput | None = None,
        message: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, vault_path=vault_path, file_path=file_path, cause=cause)


class IngestError(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 8
    exit_code: ClassVar[int] = 8
    default_message: ClassVar[str] = "ingest callable raised an error"

    def __init__(
        self,
        file_path: PathInput | None = None,
        *,
        vault_path: PathInput | None = None,
        message: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        if message is None and _looks_like_message(file_path, vault_path=vault_path, cause=cause):
            message = str(file_path)
            file_path = None
        super().__init__(message, vault_path=vault_path, file_path=file_path, cause=cause)


class FileNotUnderVaultError(IngestError):
    default_message: ClassVar[str] = "file is not under the vault root"


class QueryError(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 12
    exit_code: ClassVar[int] = 12
    default_message: ClassVar[str] = "query operation failed"


class ExportError(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 13
    exit_code: ClassVar[int] = 13
    default_message: ClassVar[str] = "export operation failed"


class RebuildSwapFailed(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 9
    default_message: ClassVar[str] = "atomic graph swap failed"

    def __init__(
        self,
        vault_path: PathInput,
        *,
        source_path: PathInput | None = None,
        target_path: PathInput | None = None,
        message: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.source_path = _absolute_path(source_path)
        self.target_path = _absolute_path(target_path)
        if message is None and self.source_path is not None and self.target_path is not None:
            message = f"atomic graph swap failed from {self.source_path} to {self.target_path}"
        super().__init__(message, vault_path=vault_path, cause=cause)


class RebuildAuditFailed(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 15
    default_message: ClassVar[str] = "rebuilt graph failed integrity verification"

    def __init__(
        self,
        vault_path: PathInput,
        *,
        staging_path: PathInput | None = None,
        failing_file: str | None = None,
        audit_status: str | None = None,
        message: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.staging_path = _absolute_path(staging_path)
        self.failing_file = failing_file
        self.audit_status = audit_status
        if message is None:
            details = []
            if audit_status is not None:
                details.append(f"status={audit_status}")
            if failing_file is not None:
                details.append(f"file={failing_file}")
            message = self.default_message
            if details:
                message = f"{message} ({', '.join(details)})"
            if self.staging_path is not None:
                message = f"{message}; staging retained at {self.staging_path}"
        super().__init__(message, vault_path=vault_path, cause=cause)


class SchemaVersionMismatch(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 10
    default_message: ClassVar[str] = "database schema version does not match this code"

    def __init__(
        self,
        file_path: PathInput,
        found_version: int | str | None,
        expected_version: int,
        *,
        vault_path: PathInput | None = None,
        message: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.found_version = found_version
        self.expected_version = expected_version
        if message is None:
            message = (
                f"database schema version {found_version!r} does not match expected "
                f"{expected_version}"
            )
        super().__init__(message, vault_path=vault_path, file_path=file_path, cause=cause)


class EmbeddingDimMismatch(OktoNeuronError):
    """The stored vector width disagrees with the configured embedding dimension.

    The embedding column is fixed-width, so a model/``dimension`` change cannot be
    applied in place. Raised on open/ingest/query against a graph whose recorded
    width differs from config; the remedy is ``kg reembed`` (recompute vectors at
    the configured width). Reembed's own re-width bootstrap bypasses this guard.
    """

    EXIT_CODE: ClassVar[int] = 14
    default_message: ClassVar[str] = "embedding dimension does not match the stored graph"

    def __init__(
        self,
        file_path: PathInput,
        *,
        stored_dim: int | None = None,
        configured_dim: int | None = None,
        vault_path: PathInput | None = None,
        message: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.stored_dim = stored_dim
        self.configured_dim = configured_dim
        if message is None and stored_dim is not None and configured_dim is not None:
            message = (
                f"embedding dimension {configured_dim} does not match the stored graph "
                f"width {stored_dim}; run `kg reembed` to rebuild vectors at the "
                "configured width"
            )
        super().__init__(message, vault_path=vault_path, file_path=file_path, cause=cause)


class GraphBackendError(OktoNeuronError):
    """A graph backend's driver failed in a way the caller could not anticipate.

    The backend adapters (`store/grafx.py`, `store/neo4j.py`) translate their own
    driver's exception hierarchy into this type at the store boundary, so that a
    driver exception never escapes the store abstraction. Without it, a driver
    failure is an ordinary `Exception` to every caller: `server/http.py`'s
    `api_ask`/`api_recall` fall past their `except OktoNeuronError` branch into
    the bare catch-all and answer `internal server error`, with the real cause
    visible only in the daemon's stderr. That is exactly how Grafx's
    "A query list may hold at most 1024 elements" limit presented in 0.0.50.

    This class exists under D-41's own reversal trigger in
    the internal ADR 0041 plan — "route non-Ladybug failures through
    a dedicated exception class once M4/M5 backends exist and produce genuinely
    different failure shapes". M4 (Grafx) and M5 (Neo4j) both ship and both
    already mint their own `*WriteExhausted` types, so that condition has fired.
    The backend-neutral REST constants D-41 chose instead (`GRAPH_WRITE_FAILED_CODE`)
    are unaffected and stay the wire contract.

    Carries `backend` (the registry name) so a message can say WHICH backend
    failed without the caller importing that backend's driver.
    """

    EXIT_CODE: ClassVar[int] = 16
    default_message: ClassVar[str] = "graph backend operation failed"

    def __init__(
        self,
        message: str | None = None,
        *,
        backend: str | None = None,
        vault_path: PathInput | None = None,
        file_path: PathInput | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.backend = backend
        if message is not None and backend:
            message = f"{backend} backend: {message}"
        super().__init__(message, vault_path=vault_path, file_path=file_path, cause=cause)


class GraphWriteExhausted(OktoNeuronError):
    """A graph write did not land after exhausting the D-10 retry budget.

    The shared base of each backend's own exhausted-retry type
    (`store/grafx.py`'s `GrafxWriteExhausted`, `store/neo4j.py`'s
    `Neo4jWriteExhausted`), so a caller such as `server/http.py` can recognize
    a failed graph write without importing an optional backend driver.
    """

    default_message: ClassVar[str] = (
        "graph write failed: retry budget exhausted under sustained write conflict"
    )


class RebuildInterrupted(OktoNeuronError):
    EXIT_CODE: ClassVar[int] = 130
    default_message: ClassVar[str] = "kg rebuild was interrupted"

    def __init__(
        self,
        vault_path: PathInput,
        *,
        signal_name: str | None = None,
        message: str | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.signal_name = signal_name
        if message is None and signal_name is not None:
            message = f"kg rebuild was interrupted by {signal_name}"
        super().__init__(message, vault_path=vault_path, cause=cause)


class FailureTaxonomyRow(NamedTuple):
    failure: str
    exception: type[OktoNeuronError]
    exit_code: int


FAILURE_TAXONOMY: tuple[FailureTaxonomyRow, ...] = (
    FailureTaxonomyRow("vault path is a file", VaultPathNotADirectory, 2),
    FailureTaxonomyRow("parent dir not writable", VaultPathNotWritable, 2),
    FailureTaxonomyRow("no vault and no vault_roots", NoVaultSpecified, 2),
    FailureTaxonomyRow("okto-neuron.toml malformed", ConfigParseError, 3),
    FailureTaxonomyRow("okto-neuron.yaml malformed", ConfigParseError, 3),
    FailureTaxonomyRow("schema version unknown", ConfigVersionUnsupported, 4),
    FailureTaxonomyRow("ladybug file lock held", VaultLockHeld, 5),
    FailureTaxonomyRow("ladybug corruption", VaultCorrupted, 6),
    FailureTaxonomyRow("partial bootstrap", BootstrapPartial, 7),
    FailureTaxonomyRow("ingest callable raised", IngestError, 8),
    FailureTaxonomyRow("atomic swap failed (EXDEV)", RebuildSwapFailed, 9),
    FailureTaxonomyRow("db schema_version vs code mismatch", SchemaVersionMismatch, 10),
    FailureTaxonomyRow("rebuild integrity audit failed", RebuildAuditFailed, 15),
    FailureTaxonomyRow("SIGINT/SIGTERM during rebuild", RebuildInterrupted, 130),
)


# Backend-neutral REST error vocabulary (M3 spec section 2.6/§7 D-41). Additive:
# these sit alongside the Ladybug-specific ``VaultLockHeld``/``VaultCorrupted``
# ``default_message``s above and the existing ``"ladybug_write_failed"`` wire
# code in ``server/http.py`` — neither is touched or replaced. Once a second
# graph backend ships (M4/M5), its REST failures can route through these
# instead of minting another backend-specific string; until then Ladybug's own
# code/message stay the primary values a client sees, per D-41's reversal note.
GRAPH_LOCK_HELD_MESSAGE: Final[str] = "graph store lock is held"
GRAPH_WRITE_FAILED_CODE: Final[str] = "graph_write_failed"


__all__ = [
    "BootstrapPartial",
    "ConfigNotFound",
    "ConfigParseError",
    "ConfigVersionUnsupported",
    "EmbeddingDimMismatch",
    "ExportError",
    "FAILURE_TAXONOMY",
    "FileNotUnderVaultError",
    "FailureTaxonomyRow",
    "GRAPH_LOCK_HELD_MESSAGE",
    "GRAPH_WRITE_FAILED_CODE",
    "InvalidVaultConfigError",
    "GraphBackendError",
    "GraphWriteExhausted",
    "IngestError",
    "OktoNeuronError",
    "NoVaultSpecified",
    "QueryError",
    "RebuildInterrupted",
    "RebuildSwapFailed",
    "SchemaVersionMismatch",
    "ValidationError",
    "VaultAlreadyExistsError",
    "VaultClosedError",
    "VaultCorrupted",
    "VaultError",
    "VaultLockedError",
    "VaultLockHeld",
    "VaultNotFoundError",
    "VaultPathNotADirectory",
    "VaultPathNotWritable",
]
