from __future__ import annotations

import json
from pathlib import Path

import okto_neuron.store.integrity_state as state_module
import pytest
from okto_neuron.store.integrity import AuditStatus
from okto_neuron.store.integrity_state import (
    GraphIntegrityState,
    IntegrityFenceError,
    guarded_store_write,
    initialize_integrity_state,
    integrity_state_path,
    is_unrecorded_state,
    load_integrity_state,
    require_store_write_allowed,
    write_integrity_state,
)
from okto_neuron.core.schema import Node
from okto_neuron.semantic_fingerprint import (
    load_semantic_materialization,
    publish_semantic_materialization,
    semantic_materialization_path,
)
from okto_neuron.store.ladybug import LadybugStore


def test_integrity_state_round_trips_without_opening_graph(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    expected = GraphIntegrityState(
        status=AuditStatus.VERIFIED,
        graph_generation="generation-a",
        writer_fenced=False,
        reason=None,
        audit_id="audit-a",
    )

    path = write_integrity_state(vault_path, expected)

    assert path == vault_path / ".marginalia" / "graph-integrity.json"
    assert not (vault_path / "graph.lbug").exists()
    assert load_integrity_state(vault_path) == expected
    assert (
        load_integrity_state(
            vault_path,
            expected_graph_generation="generation-a",
        )
        == expected
    )


def test_generation_mismatch_is_unverified_and_preserves_old_evidence(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    previous = GraphIntegrityState(
        status=AuditStatus.VERIFIED,
        graph_generation="generation-a",
        writer_fenced=False,
    )
    write_integrity_state(vault_path, previous)

    current = load_integrity_state(
        vault_path,
        expected_graph_generation="generation-b",
    )

    assert current.status is AuditStatus.UNVERIFIED
    assert current.graph_generation == "generation-b"
    assert current.writer_fenced is True
    assert current.reason == "integrity state belongs to a different graph generation"
    assert load_integrity_state(vault_path) == previous


def test_missing_or_malformed_state_fails_closed(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    missing = load_integrity_state(
        vault_path,
        expected_graph_generation="generation-a",
    )
    assert missing.status is AuditStatus.UNVERIFIED
    assert missing.writer_fenced is True

    path = integrity_state_path(vault_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")

    malformed = load_integrity_state(
        vault_path,
        expected_graph_generation="generation-a",
    )
    assert malformed.status is AuditStatus.UNVERIFIED
    assert malformed.writer_fenced is True


def test_legacy_verified_sidecar_fails_closed_under_current_audit_contract(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    path = integrity_state_path(vault_path)
    path.parent.mkdir(parents=True)
    legacy = {
        "version": 1,
        "status": "verified",
        "graph_generation": "generation-a",
        "writer_fenced": False,
        "reason": None,
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")

    state = load_integrity_state(
        vault_path,
        expected_graph_generation="generation-a",
    )

    assert state.status is AuditStatus.UNVERIFIED
    assert state.graph_generation == "generation-a"
    assert state.writer_fenced is True
    assert state.reason == "integrity state is unreadable: ValueError"
    assert json.loads(path.read_text(encoding="utf-8")) == legacy


def test_failed_and_incomplete_states_cannot_clear_writer_fence() -> None:
    for status in (AuditStatus.FAILED, AuditStatus.INCOMPLETE):
        with pytest.raises(ValueError, match="must fence writers"):
            GraphIntegrityState(
                status=status,
                graph_generation="generation-a",
                writer_fenced=False,
            )


def test_initialize_creates_once_and_does_not_overwrite_existing_state(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    initial = initialize_integrity_state(vault_path, graph_generation="generation-a")
    assert initial.status is AuditStatus.UNVERIFIED
    assert initial.writer_fenced is True

    verified = GraphIntegrityState(
        status=AuditStatus.VERIFIED,
        graph_generation="generation-a",
        writer_fenced=False,
    )
    write_integrity_state(vault_path, verified)

    assert (
        initialize_integrity_state(
            vault_path,
            graph_generation="generation-a",
        )
        == verified
    )


def test_atomic_write_fsyncs_file_and_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fsync_calls: list[int] = []
    monkeypatch.setattr(state_module.os, "fsync", fsync_calls.append)
    vault_path = tmp_path / "vault"

    path = write_integrity_state(
        vault_path,
        GraphIntegrityState(
            status=AuditStatus.FAILED,
            graph_generation="generation-a",
            writer_fenced=True,
            reason="endpoint mismatch",
        ),
    )

    assert path.exists()
    assert len(fsync_calls) == 2
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_direct_store_writer_audits_once_then_honors_failed_fence(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    store = LadybugStore(vault_path)
    try:
        generation = store._graph_handle.graph_generation  # noqa: SLF001
        verified = require_store_write_allowed(vault_path, store)
        assert verified is not None
        assert verified.status is AuditStatus.VERIFIED
        assert verified.graph_generation == generation
        assert verified.writer_fenced is False
        assert verified.audit_id

        failed = GraphIntegrityState(
            status=AuditStatus.FAILED,
            graph_generation=generation,
            writer_fenced=True,
            reason="adjacency mismatch",
        )
        write_integrity_state(vault_path, failed)

        with pytest.raises(IntegrityFenceError, match="integrity_fenced"):
            require_store_write_allowed(vault_path, store)
        assert load_integrity_state(vault_path) == failed
    finally:
        store.close()


def test_guarded_noop_preserves_generation_materialization_receipt(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    store = LadybugStore(vault_path)
    try:
        generation = store._graph_handle.graph_generation  # noqa: SLF001
        assert generation
        require_store_write_allowed(vault_path, store)
        expected = publish_semantic_materialization(
            vault_path,
            graph_generation=generation,
            fingerprints={
                "config": "sha256:" + "a" * 64,
                "extraction": "sha256:" + "b" * 64,
                "semantic_policy": "sha256:" + "c" * 64,
            },
            source="test",
        )

        with guarded_store_write(vault_path, store):
            pass

        assert (
            load_semantic_materialization(
                semantic_materialization_path(vault_path),
                expected_graph_generation=generation,
            )
            == expected
        )
    finally:
        store.close()


def test_guarded_mutation_invalidates_generation_materialization_receipt(
    tmp_path: Path,
) -> None:
    vault_path = tmp_path / "vault"
    store = LadybugStore(vault_path)
    try:
        generation = store._graph_handle.graph_generation  # noqa: SLF001
        assert generation
        require_store_write_allowed(vault_path, store)
        publish_semantic_materialization(
            vault_path,
            graph_generation=generation,
            fingerprints={
                "config": "sha256:" + "a" * 64,
                "extraction": "sha256:" + "b" * 64,
                "semantic_policy": "sha256:" + "c" * 64,
            },
            source="test",
        )

        with guarded_store_write(vault_path, store):
            store.add_node(Node(type="Concept", title="Changed", content="changed"))

        assert not semantic_materialization_path(vault_path).exists()
    finally:
        store.close()


def test_fence_error_propagates_out_of_a_contextmanager() -> None:
    """The fence error must be a plain exception, not a frozen/slots dataclass.

    A slotted frozen dataclass exception cannot have ``__traceback__`` assigned, so
    ``contextlib.contextmanager.__exit__`` raises ``TypeError`` while unwinding and
    the real fence error is lost. Reproduce that unwind path here.
    """
    from contextlib import contextmanager

    fenced = GraphIntegrityState(
        status=AuditStatus.FAILED,
        graph_generation="gen-1",
        writer_fenced=True,
        reason="adjacency mismatch",
    )

    @contextmanager
    def _swap_lease():
        yield

    with pytest.raises(IntegrityFenceError, match="integrity_fenced") as excinfo:
        with _swap_lease():
            raise IntegrityFenceError(fenced)

    assert excinfo.value.state is fenced
    assert excinfo.value.args == (fenced,)


def test_is_unrecorded_state_matches_only_the_synthesized_no_record_verdicts(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    missing = load_integrity_state(vault_path, expected_graph_generation="generation-a")
    assert is_unrecorded_state(missing)

    path = integrity_state_path(vault_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    assert is_unrecorded_state(
        load_integrity_state(vault_path, expected_graph_generation="generation-a")
    )

    write_integrity_state(
        vault_path,
        GraphIntegrityState(
            status=AuditStatus.VERIFIED,
            graph_generation="generation-a",
            writer_fenced=False,
            audit_id="audit-a",
        ),
    )
    stale = load_integrity_state(vault_path, expected_graph_generation="generation-b")
    assert is_unrecorded_state(stale)

    # Recorded verdicts are never "unrecorded": a verifying marker (audit id), a failed audit and
    # a drift/recovery fence (unverified with another reason and no audit id).
    verifying = GraphIntegrityState(
        status=AuditStatus.VERIFYING,
        graph_generation="g",
        writer_fenced=True,
        reason="graph integrity audit is running",
        audit_id="audit-b",
    )
    failed = GraphIntegrityState(
        status=AuditStatus.FAILED, graph_generation="g", writer_fenced=True, reason="1 issue(s)"
    )
    recovery = GraphIntegrityState(
        status=AuditStatus.UNVERIFIED,
        graph_generation="g",
        writer_fenced=True,
        reason="graph recovered from a corrupt WAL/checkpoint; the recovered generation has not been re-audited",
    )
    unaudited_with_id = GraphIntegrityState(
        status=AuditStatus.UNVERIFIED,
        graph_generation="g",
        writer_fenced=True,
        reason="integrity state is missing",
        audit_id="audit-c",
    )
    for state in (verifying, failed, recovery, unaudited_with_id):
        assert not is_unrecorded_state(state)
