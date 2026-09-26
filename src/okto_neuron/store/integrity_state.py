"""Durable per-vault graph-integrity and writer-fence state."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final
from uuid import uuid4

from okto_neuron.store.integrity import AuditStatus, IntegrityAuditResult, audit_graph

# Version 2 requires the physical adjacency audit introduced by ADR 0039.
# Older green sidecars cannot attest to that stronger contract and fail closed.
_STATE_VERSION: Final = 2
_STATE_RELATIVE_PATH: Final = Path(".marginalia") / "graph-integrity.json"
_UNSET: Final = object()
INTEGRITY_FENCED_CODE: Final = "integrity_fenced"
RECOVERY_GUIDANCE: Final = (
    "semantic writes are disabled for this graph generation; reads remain available. "
    "Run a fresh-graph rebuild to recover and mint a new generation, then audit it"
)
INCOMPLETE_GUIDANCE: Final = (
    "retry a complete integrity audit; semantic writes stay disabled until one completes. "
    "If verification cannot complete, run a fresh-graph rebuild"
)


@dataclass(frozen=True, slots=True)
class GraphIntegrityState:
    """Integrity verdict and independent semantic-writer fence for one generation."""

    status: AuditStatus
    graph_generation: str | None
    writer_fenced: bool
    reason: str | None = None
    audit_id: str | None = None

    def __post_init__(self) -> None:
        terminal_failures = {AuditStatus.FAILED, AuditStatus.INCOMPLETE}
        if self.status in terminal_failures and not self.writer_fenced:
            raise ValueError(f"{self.status.value} integrity state must fence writers")


class IntegrityFenceError(RuntimeError):
    """Stable failure raised when the current generation cannot accept writes.

    Deliberately a plain exception class, NOT a frozen/slots dataclass: a slotted
    frozen dataclass exception cannot have ``__traceback__`` assigned, which makes
    ``contextlib.contextmanager.__exit__`` raise ``TypeError`` while unwinding and
    loses the real fence error.
    """

    def __init__(self, state: GraphIntegrityState) -> None:
        super().__init__(state)
        self.state = state

    def __str__(self) -> str:
        reason = self.state.reason or f"graph integrity is {self.state.status.value}"
        guidance = (
            INCOMPLETE_GUIDANCE
            if self.state.status is AuditStatus.INCOMPLETE
            else RECOVERY_GUIDANCE
        )
        return f"{INTEGRITY_FENCED_CODE}: {reason}. {guidance}"


def integrity_state_path(vault_path: Path | str) -> Path:
    """Return the sidecar path without opening the graph database."""
    return Path(vault_path).expanduser().resolve(strict=False) / _STATE_RELATIVE_PATH


def load_integrity_state(
    vault_path: Path | str,
    *,
    expected_graph_generation: str | None | object = _UNSET,
) -> GraphIntegrityState:
    """Load integrity state without touching Ladybug.

    Missing, unreadable, malformed, or generation-stale state is conservatively
    returned as ``unverified`` with the writer fenced.  The persisted file is left
    untouched so a stale generation remains available as incident evidence.
    """
    path = integrity_state_path(vault_path)
    expected = None
    if expected_graph_generation is not _UNSET:
        expected = _optional_string(expected_graph_generation)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = _state_from_payload(payload)
    except FileNotFoundError:
        return _unverified(expected, "integrity state is missing")
    except (OSError, TypeError, ValueError) as exc:
        return _unverified(expected, f"integrity state is unreadable: {type(exc).__name__}")

    if expected_graph_generation is not _UNSET and state.graph_generation != expected:
        return _unverified(expected, "integrity state belongs to a different graph generation")
    return state


def write_integrity_state(
    vault_path: Path | str,
    state: GraphIntegrityState,
) -> Path:
    """Atomically persist ``state`` and fsync both the file and its directory."""
    path = integrity_state_path(vault_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    payload = {"version": _STATE_VERSION, **asdict(state)}
    payload["status"] = state.status.value
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()

    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return path


def initialize_integrity_state(
    vault_path: Path | str,
    *,
    graph_generation: str | None,
) -> GraphIntegrityState:
    """Create the conservative initial sidecar once, preserving existing evidence."""
    path = integrity_state_path(vault_path)
    if not path.exists():
        write_integrity_state(
            vault_path,
            _unverified(graph_generation, "graph generation has not been audited"),
        )
    return load_integrity_state(
        vault_path,
        expected_graph_generation=graph_generation,
    )


def invalidate_integrity_state(
    vault_path: Path | str,
    *,
    graph_generation: str | None,
    reason: str,
) -> GraphIntegrityState:
    """Fence a generation after recovery or another out-of-band graph change."""

    state = _unverified(graph_generation, reason)
    write_integrity_state(vault_path, state)
    return state


def require_store_write_allowed(
    vault_path: Path | str,
    store: object,
) -> GraphIntegrityState | None:
    """Enforce the durable fence for direct SDK/CLI writers.

    Non-Ladybug test stores have no graph handle and remain unaffected. A live
    Ladybug graph is audited synchronously when its generation is not yet verified;
    a proven failure is never retried into green on the same generation.
    """

    handle = getattr(store, "_graph_handle", None)
    if handle is None:
        return None
    generation = store.generation() or None  # type: ignore[attr-defined]
    contract = getattr(handle, "identity_contract_version", None)
    _require_no_drift(vault_path, store)
    state = load_integrity_state(
        vault_path,
        expected_graph_generation=generation,
    )
    if state.status is AuditStatus.VERIFIED and not state.writer_fenced:
        return state
    if state.status is AuditStatus.FAILED:
        raise IntegrityFenceError(state)

    audit_id = uuid4().hex
    write_integrity_state(
        vault_path,
        GraphIntegrityState(
            status=AuditStatus.VERIFYING,
            graph_generation=generation,
            writer_fenced=True,
            reason="graph integrity audit is running",
            audit_id=audit_id,
        ),
    )
    result = audit_graph(
        store,  # type: ignore[arg-type] - runtime GraphStore protocol
        graph_generation=generation,
        identity_contract_version=contract,
    )
    _require_no_drift(vault_path, store)
    state = GraphIntegrityState(
        status=result.status,
        graph_generation=generation,
        writer_fenced=not result.verified,
        reason=_audit_reason(result),
        audit_id=audit_id,
    )
    write_integrity_state(vault_path, state)
    _require_no_drift(vault_path, store)
    if state.status is not AuditStatus.VERIFIED or state.writer_fenced:
        raise IntegrityFenceError(state)
    return state


@contextmanager
def guarded_store_write(
    vault_path: Path | str,
    store: object,
) -> Iterator[None]:
    """Fence one direct semantic write and audit its generation on every exit."""

    current = require_store_write_allowed(vault_path, store)
    if current is None:
        yield
        return
    from okto_neuron.semantic_fingerprint import (
        invalidate_semantic_materialization,
        load_semantic_materialization,
        semantic_materialization_path,
        write_semantic_materialization,
    )

    epoch_before = getattr(store, "semantic_write_epoch", None)
    receipt_path = semantic_materialization_path(vault_path)
    try:
        previous_receipt = load_semantic_materialization(
            receipt_path,
            expected_graph_generation=current.graph_generation,
        )
    except (OSError, TypeError, ValueError):
        previous_receipt = None

    def _restore_receipt_after_noop() -> None:
        if (
            previous_receipt is None
            or not isinstance(epoch_before, int)
            or getattr(store, "semantic_write_epoch", None) != epoch_before
        ):
            return
        write_semantic_materialization(
            receipt_path,
            graph_generation=previous_receipt["graph_generation"],
            fingerprints=previous_receipt["fingerprints"],
            source=previous_receipt["source"],
        )

    invalidate_semantic_materialization(vault_path)
    write_integrity_state(
        vault_path,
        GraphIntegrityState(
            status=AuditStatus.UNVERIFIED,
            graph_generation=current.graph_generation,
            writer_fenced=True,
            reason="semantic write is in progress and has not been audited",
        ),
    )
    try:
        yield
    except BaseException as operation_error:
        try:
            require_store_write_allowed(vault_path, store)
        except BaseException as audit_error:
            raise audit_error from operation_error
        _restore_receipt_after_noop()
        raise
    else:
        require_store_write_allowed(vault_path, store)
        _restore_receipt_after_noop()


def _state_from_payload(payload: object) -> GraphIntegrityState:
    if not isinstance(payload, dict) or payload.get("version") != _STATE_VERSION:
        raise ValueError("unsupported integrity-state payload")
    status = AuditStatus(payload.get("status"))
    generation = _optional_string(payload.get("graph_generation"))
    writer_fenced = payload.get("writer_fenced")
    if not isinstance(writer_fenced, bool):
        raise TypeError("writer_fenced must be boolean")
    reason = _optional_string(payload.get("reason"))
    audit_id = _optional_string(payload.get("audit_id"))
    return GraphIntegrityState(
        status=status,
        graph_generation=generation,
        writer_fenced=writer_fenced,
        reason=reason,
        audit_id=audit_id,
    )


def _require_no_drift(vault_path: Path | str, store: object) -> None:
    """Fence writers when the open store's on-disk identity drifted from the durable record.

    Replaces the old hardcoded-``graph.lbug`` duplicate of this check (M3 spec
    section 2.4): delegates to the store's own :meth:`GraphStore.detect_drift`,
    which reads whatever path it actually opened instead of assuming a fixed
    file name, and compares it against the durable sidecar's last-recorded
    generation exactly as the retired check did.
    """
    durable = load_integrity_state(vault_path)
    drift = store.detect_drift(durable.graph_generation)  # type: ignore[attr-defined]
    if drift is None:
        return
    state = GraphIntegrityState(
        status=AuditStatus.UNVERIFIED,
        graph_generation=durable.graph_generation,
        writer_fenced=True,
        reason=drift.reason,
    )
    write_integrity_state(vault_path, state)
    raise IntegrityFenceError(state)


def require_unfenced_generation(vault_path: Path | str) -> None:
    """Refuse a graph→fresh-graph copy whose source generation is proven bad.

    Shared by BOTH copy sites (``kg reconcile heal`` and the daemon ``heal`` job)
    so one fence rule governs them. ``failed`` means the live topology is known
    damaged; ``incomplete`` means the audit could not attest it at all. Copying
    either would canonicalize damage into a fresh generation that then LOOKS
    clean. A freshly bootstrapped vault is legitimately ``unverified`` and a
    stale ``verifying`` marker is superseded by the caller's swap lease, so
    neither blocks. The recovery path is a fresh-graph rebuild from the markdown
    trust root, which the raised fence error names.
    """
    state = load_integrity_state(vault_path)
    if state.status in (AuditStatus.FAILED, AuditStatus.INCOMPLETE):
        raise IntegrityFenceError(state)


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise TypeError("value must be a non-empty string or null")
    return value


def _unverified(graph_generation: str | None, reason: str) -> GraphIntegrityState:
    return GraphIntegrityState(
        status=AuditStatus.UNVERIFIED,
        graph_generation=graph_generation,
        writer_fenced=True,
        reason=reason,
    )


def _audit_reason(result: IntegrityAuditResult) -> str | None:
    if result.verified:
        return None
    if result.issues:
        first = result.issues[0]
        return f"{result.issue_count} issue(s); first={first.code}:{first.artifact_id or 'graph'}"
    if result.incomplete_reasons:
        return "; ".join(result.incomplete_reasons)
    return f"graph integrity audit finished {result.status.value}"


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        if os.name == "nt":  # Windows cannot open directory handles this way.
            return
        raise
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


__all__ = [
    "GraphIntegrityState",
    "INCOMPLETE_GUIDANCE",
    "INTEGRITY_FENCED_CODE",
    "IntegrityFenceError",
    "RECOVERY_GUIDANCE",
    "guarded_store_write",
    "initialize_integrity_state",
    "invalidate_integrity_state",
    "integrity_state_path",
    "load_integrity_state",
    "require_store_write_allowed",
    "require_unfenced_generation",
    "write_integrity_state",
]
