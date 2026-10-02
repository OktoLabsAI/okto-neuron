"""Generation-scoped graph integrity audit and semantic-writer fence."""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING
from uuid import uuid4

from okto_neuron.store.integrity import AuditStatus, IntegrityAuditResult, audit_graph
from okto_neuron.store.integrity_state import (
    INCOMPLETE_GUIDANCE,
    INTEGRITY_FENCED_CODE,
    RECOVERY_GUIDANCE,
    GraphIntegrityState,
    REASON_UNREADABLE_PREFIX,
    IntegrityFenceError,
    is_unrecorded_state,
    load_integrity_state,
    write_integrity_state,
)
from okto_neuron.store.capabilities import capabilities_for

if TYPE_CHECKING:
    from okto_neuron.server.state import VaultRuntime
    from okto_neuron.vault import Vault


def graph_identity(vault: "Vault") -> tuple[str | None, str | None]:
    """Read immutable generation metadata from the already-open graph handle."""
    store = getattr(vault, "store", None)
    handle = getattr(store, "_graph_handle", None)
    generation = store.generation() or None if store is not None else None
    return (
        generation,
        # identity_contract_version stays a raw reflection: SchemaPort's shared
        # interface is out of M3's scope (D-42), so there is no store method to
        # retire this one into yet.
        getattr(handle, "identity_contract_version", None),
    )


def read_state(runtime: "VaultRuntime", vault: "Vault | None" = None) -> GraphIntegrityState:
    """Read the sidecar without opening a graph when no handle is already leased."""
    if vault is None:
        return load_integrity_state(runtime.vault_path)
    drift = _handle_disk_drift_state(runtime, vault)
    if drift is not None:
        return drift
    generation, _contract = graph_identity(vault)
    return load_integrity_state(
        runtime.vault_path,
        expected_graph_generation=generation,
    )


def run_audit(
    runtime: "VaultRuntime",
    vault: "Vault",
    *,
    preserve_terminal_fence: bool = True,
) -> tuple[GraphIntegrityState, IntegrityAuditResult | None]:
    """Audit the current generation; caller must hold ``runtime.writer_lock``.

    A proven failure is incident evidence and ordinary retries never turn that
    same generation green. ``incomplete`` proves no mismatch, so a later complete
    scan may verify and release its fence.
    """
    generation, contract = graph_identity(vault)
    drift = _fence_handle_disk_drift(runtime, vault)
    if drift is not None:
        return drift, None
    current = read_state(runtime, vault)
    if preserve_terminal_fence and current.status is AuditStatus.FAILED:
        return current, None

    audit_id = uuid4().hex
    write_integrity_state(
        runtime.vault_path,
        GraphIntegrityState(
            status=AuditStatus.VERIFYING,
            graph_generation=generation,
            writer_fenced=True,
            reason="graph integrity audit is running",
            audit_id=audit_id,
        ),
    )
    result = audit_graph(
        vault.store,
        graph_generation=generation,
        identity_contract_version=contract,
    )
    drift = _fence_handle_disk_drift(runtime, vault)
    if drift is not None:
        return drift, None
    state = GraphIntegrityState(
        status=result.status,
        graph_generation=generation,
        writer_fenced=not result.verified,
        reason=_audit_reason(result),
        audit_id=audit_id,
    )
    write_integrity_state(runtime.vault_path, state)
    drift = _fence_handle_disk_drift(runtime, vault)
    if drift is not None:
        return drift, None
    runtime.integrity_last_audit = result
    return state, result


def require_write_allowed(
    runtime: "VaultRuntime",
    vault: "Vault",
) -> GraphIntegrityState:
    """Verify the current generation once, then enforce its durable fence."""
    store = getattr(vault, "store", None)
    if store is None or getattr(store, "_graph_handle", None) is None:
        return GraphIntegrityState(
            status=AuditStatus.VERIFIED,
            graph_generation=None,
            writer_fenced=False,
            reason="integrity audit is not applicable to this non-Ladybug runtime",
        )
    state = read_state(runtime, vault)
    if state.status is AuditStatus.VERIFIED and not state.writer_fenced:
        return state
    if state.status in {
        AuditStatus.UNVERIFIED,
        AuditStatus.VERIFYING,
        AuditStatus.INCOMPLETE,
    }:
        state, _result = run_audit(runtime, vault)
    if state.status is not AuditStatus.VERIFIED or state.writer_fenced:
        raise IntegrityFenceError(state)
    return state


# What a grafx vault says when nothing was ever audited. Plain on purpose: nothing is wrong, no write
# is blocked, an audit is optional. (Ladybug keeps its fenced "unverified" until the first write.)
NO_FENCE_UNAUDITED_REASON = (
    "no integrity audit has run for this graph; the {backend} backend does not fence writes, "
    "so an audit is optional (POST /api/v1/graph/integrity runs one)"
)


def write_fence_enforced(runtime: "VaultRuntime", vault: "Vault | None" = None) -> bool:
    """Whether writes to this vault are gated by the integrity fence.

    The same predicate ``require_write_allowed`` uses when a store is open (only a store with a
    graph handle is fenced); without an open store it falls back to the backend's declared
    capability, resolved from the vault's pin without opening the graph. An unknown backend is
    treated as fenced: that is the conservative reading and the previous behaviour.
    """
    store = getattr(vault, "store", None) if vault is not None else None
    if store is not None:
        return getattr(store, "_graph_handle", None) is not None
    from okto_neuron.vault_registry import resolve_vault_backend

    capabilities = capabilities_for(resolve_vault_backend(runtime.vault_path))
    return True if capabilities is None else capabilities.write_fence


def _backend_label(runtime: "VaultRuntime") -> str:
    from okto_neuron.vault_registry import resolve_vault_backend

    return resolve_vault_backend(runtime.vault_path)


def _truthful_state(
    runtime: "VaultRuntime", vault: "Vault | None", state: GraphIntegrityState
) -> GraphIntegrityState:
    """Report "never audited" truthfully for a backend that has no write fence.

    Only the synthesized no-record verdicts are rewritten (``is_unrecorded_state``): on such a
    backend ``writer_fenced=True`` would claim a fence nothing enforces and nothing ever lifts.
    A recorded verifying/failed/incomplete result, a drift fence and every Ladybug state keep
    their stored fence, so a real failed audit still raises the degraded alarm.
    """
    if not is_unrecorded_state(state) or write_fence_enforced(runtime, vault):
        return state
    reason = NO_FENCE_UNAUDITED_REASON.format(backend=_backend_label(runtime))
    if (state.reason or "").startswith(REASON_UNREADABLE_PREFIX):
        reason = f"{state.reason}; {reason}"
    return GraphIntegrityState(
        status=state.status,
        graph_generation=state.graph_generation,
        writer_fenced=False,
        reason=reason,
        audit_id=None,
    )


def summary(runtime: "VaultRuntime", vault: "Vault | None" = None) -> dict[str, object]:
    """Return compact diagnostics, optionally enriched by this process's last run."""
    state = _truthful_state(runtime, vault, read_state(runtime, vault))
    payload: dict[str, object] = {
        "status": state.status.value,
        "graph_generation": state.graph_generation,
        "writer_fenced": state.writer_fenced,
        "reason": state.reason,
        "audit_id": state.audit_id,
        "recovery_guidance": (
            RECOVERY_GUIDANCE
            if state.status is AuditStatus.FAILED
            else INCOMPLETE_GUIDANCE
            if state.status is AuditStatus.INCOMPLETE
            else None
        ),
    }
    result = getattr(runtime, "integrity_last_audit", None)
    if result is not None and result.graph_generation == state.graph_generation:
        payload["last_audit"] = _audit_payload(result)
    return payload


def _audit_reason(result: IntegrityAuditResult) -> str | None:
    if result.verified:
        return None
    if result.issues:
        first = result.issues[0]
        return f"{result.issue_count} issue(s); first={first.code}:{first.artifact_id or 'graph'}"
    if result.incomplete_reasons:
        return "; ".join(result.incomplete_reasons)
    return f"graph integrity audit finished {result.status.value}"


def _audit_payload(result: IntegrityAuditResult) -> dict[str, object]:
    payload = asdict(result)
    payload["status"] = result.status.value
    return payload


def _handle_disk_drift_state(
    runtime: "VaultRuntime",
    vault: "Vault",
) -> GraphIntegrityState | None:
    """Compare the open store's on-disk identity against the durable sidecar.

    Delegates to :meth:`GraphStore.detect_drift` (M3 spec section 2.4) instead
    of this module's own hardcoded ``graph.lbug`` stat/compare — the same
    duplicate ``store/integrity_state.py``'s ``_require_no_drift`` retired.
    """
    store = getattr(vault, "store", None)
    if store is None:
        return None
    durable = load_integrity_state(runtime.vault_path)
    drift = store.detect_drift(durable.graph_generation)
    if drift is None:
        return None
    return GraphIntegrityState(
        status=AuditStatus.UNVERIFIED,
        graph_generation=durable.graph_generation,
        writer_fenced=True,
        reason=drift.reason,
    )


def _fence_handle_disk_drift(
    runtime: "VaultRuntime",
    vault: "Vault",
) -> GraphIntegrityState | None:
    state = _handle_disk_drift_state(runtime, vault)
    if state is None:
        return None
    write_integrity_state(runtime.vault_path, state)
    runtime.integrity_last_audit = None
    return state


__all__ = [
    "INTEGRITY_FENCED_CODE",
    "INCOMPLETE_GUIDANCE",
    "RECOVERY_GUIDANCE",
    "IntegrityFenceError",
    "read_state",
    "require_write_allowed",
    "run_audit",
    "summary",
]
