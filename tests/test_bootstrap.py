from __future__ import annotations

import errno
import multiprocessing as mp
import os
from pathlib import Path
from typing import Any
from uuid import UUID

import ladybug
import pytest

import okto_neuron.store._bootstrap as bootstrap_module
from okto_neuron.errors import BootstrapPartial, VaultLockHeld
from okto_neuron.store import schema
from okto_neuron.store._bootstrap import (
    _bootstrap_cache,
    bootstrap_vault_graph,
    reset_bootstrap_cache_for_tests,
)
from okto_neuron.store.integrity import AuditStatus
from okto_neuron.store.integrity_state import (
    GraphIntegrityState,
    load_integrity_state,
    write_integrity_state,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]


@pytest.fixture(autouse=True)
def clear_bootstrap_cache() -> None:
    yield
    for handle in list(_bootstrap_cache.values()):
        handle.close()
    _bootstrap_cache.clear()


def test_bootstrap_vault_graph_is_idempotent_across_reopens(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    graph_path = vault_path / "graph.lbug"

    first = bootstrap_vault_graph(vault_path)

    assert first.vault_path == vault_path.resolve()
    assert first.schema_version == schema.CURRENT_SCHEMA_VERSION
    assert Path(first.database.database_path) == graph_path.resolve()
    assert graph_path.exists()
    assert (vault_path / ".marginalia" / ".bootstrap.lock").exists()
    assert UUID(first.graph_generation or "")
    assert first.identity_contract_version == schema.CURRENT_IDENTITY_CONTRACT_VERSION
    integrity_state = load_integrity_state(
        vault_path,
        expected_graph_generation=first.graph_generation,
    )
    assert integrity_state.status is AuditStatus.UNVERIFIED
    assert integrity_state.writer_fenced is True
    _verify_live_schema(first.database, graph_path)

    reset_bootstrap_cache_for_tests(vault_path)
    second = bootstrap_vault_graph(vault_path)

    assert second is not first
    assert second.schema_version == schema.CURRENT_SCHEMA_VERSION
    assert second.graph_generation == first.graph_generation
    assert second.identity_contract_version == first.identity_contract_version
    _verify_live_schema(second.database, graph_path)


def test_bootstrap_cache_hits_on_second_call(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"

    first = bootstrap_vault_graph(vault_path)
    second = bootstrap_vault_graph(vault_path)

    assert second is first
    assert _bootstrap_cache[vault_path.resolve()] is first


def test_reset_bootstrap_cache_for_tests_closes_and_removes_handle(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    first = bootstrap_vault_graph(vault_path)

    reset_bootstrap_cache_for_tests(vault_path)

    assert vault_path.resolve() not in _bootstrap_cache
    assert first.database.is_closed

    second = bootstrap_vault_graph(vault_path)
    assert second is not first


def test_bootstrap_lock_contention_raises_vault_lock_held(tmp_path: Path) -> None:
    if fcntl is None:
        pytest.skip("POSIX fcntl contention helper is not available on Windows")

    vault_path = tmp_path / "vault"
    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    process = ctx.Process(
        target=_hold_bootstrap_lock,
        args=(str(vault_path), ready, release),
    )

    process.start()
    try:
        assert ready.wait(5), "lock holder did not acquire bootstrap lock"
        with pytest.raises(VaultLockHeld) as exc_info:
            bootstrap_vault_graph(vault_path)

        error = exc_info.value
        assert error.vault_path == vault_path.resolve()
        assert error.holding_pid == process.pid
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert process.exitcode == 0


def test_bootstrap_partial_wraps_ddl_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_path = tmp_path / "vault"

    monkeypatch.setattr(
        bootstrap_module.schema,
        "ddl_statements",
        lambda *_args, **_kwargs: (
            "CREATE NODE TABLE IF NOT EXISTS Node (id STRING PRIMARY KEY)",
            "NOT CYPHER",
        ),
    )

    with pytest.raises(BootstrapPartial) as exc_info:
        bootstrap_vault_graph(vault_path)

    error = exc_info.value
    assert error.vault_path == vault_path.resolve()
    assert error.file_path == (vault_path / "graph.lbug").resolve()
    assert error.cause is not None
    assert vault_path.resolve() not in _bootstrap_cache


@pytest.mark.parametrize(
    "message",
    (
        "IO exception: Could not set lock on file /tmp/wal-vault/graph.lbug",
        "Conflicting lock is held in /tmp/wal-vault/graph.lbug by another process",
        "Database is locked: /tmp/wal-vault/graph.lbug",
    ),
)
def test_ladybug_lock_contention_is_never_classified_as_wal_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    """A lock error may contain ``wal`` in its path without authorizing quarantine."""

    vault_path = tmp_path / "wal-vault"
    graph_path = vault_path / "graph.lbug"
    contention = BootstrapPartial(
        vault_path,
        file_path=graph_path,
        cause=RuntimeError(message),
    )
    recovery_called = False

    def fail_open(*_args: object, **_kwargs: object) -> None:
        raise contention

    def refuse_recovery(*_args: object, **_kwargs: object) -> None:
        nonlocal recovery_called
        recovery_called = True
        raise AssertionError("lock contention must not enter corruption recovery")

    monkeypatch.setattr(bootstrap_module, "_open_and_bootstrap", fail_open)
    monkeypatch.setattr(bootstrap_module, "_recover_from_corruption", refuse_recovery)

    with pytest.raises(BootstrapPartial) as exc_info:
        bootstrap_vault_graph(vault_path)

    assert exc_info.value is contention
    assert recovery_called is False
    assert not (vault_path / ".marginalia" / "corrupt-graph").exists()


def test_real_ladybug_lock_contention_never_quarantines_wal_named_vault(
    tmp_path: Path,
) -> None:
    """Exercise Ladybug's real cross-process lock exception through bootstrap."""

    vault_path = tmp_path / "wal-vault"
    graph_path = vault_path / "graph.lbug"
    bootstrap_vault_graph(vault_path)
    reset_bootstrap_cache_for_tests(vault_path)

    ctx = mp.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    process = ctx.Process(
        target=_hold_ladybug_database,
        args=(str(graph_path), ready, release),
    )
    process.start()
    try:
        assert ready.wait(5), "Ladybug lock holder did not open the graph"
        with pytest.raises(BootstrapPartial) as exc_info:
            bootstrap_vault_graph(vault_path)

        cause = exc_info.value.cause
        assert cause is not None
        assert "could not set lock" in str(cause).lower()
        assert not (vault_path / ".marginalia" / "corrupt-graph").exists()
        assert graph_path.exists()
    finally:
        release.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert process.exitcode == 0


def test_wal_in_path_does_not_turn_unrelated_open_error_into_corruption(tmp_path: Path) -> None:
    vault_path = tmp_path / "wal-vault"
    graph_path = vault_path / "graph.lbug"
    failure = BootstrapPartial(
        vault_path,
        file_path=graph_path,
        cause=OSError(errno.ENOSPC, f"no space left on device: {graph_path}"),
    )

    assert bootstrap_module._is_corruption(failure) is False


def test_ladybug_wal_record_assertion_is_classified_as_corruption(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    graph_path = vault_path / "graph.lbug"
    failure = BootstrapPartial(
        vault_path,
        file_path=graph_path,
        cause=RuntimeError(
            'Assertion failed in file "/ladybug/src/storage/wal/wal_record.cpp" '
            "on line 76: UNREACHABLE_CODE"
        ),
    )

    assert bootstrap_module._is_corruption(failure) is True


def test_fresh_bootstrap_not_marked_recovered(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    handle = bootstrap_vault_graph(vault_path)
    assert handle.recovered_from_corruption is False
    assert handle.quarantine_path is None


def test_bootstrap_recovers_from_corrupt_graph_file(tmp_path: Path) -> None:
    """A clobbered main graph file must warm-recover, not crash the server."""
    vault_path = tmp_path / "vault"
    graph_path = vault_path / "graph.lbug"

    original = bootstrap_vault_graph(vault_path)
    reset_bootstrap_cache_for_tests(vault_path)

    # Simulate a kill -9 that left the main graph file torn.
    garbage = b"\x00not-a-graph\xff" * 100
    graph_path.write_bytes(garbage)

    handle = bootstrap_vault_graph(vault_path)

    assert handle.recovered_from_corruption is True
    # A torn MAIN file has no recoverable checkpoint → empty fallback.
    assert handle.recovered_mode == "empty"
    assert UUID(handle.graph_generation or "")
    assert handle.graph_generation != original.graph_generation
    assert handle.identity_contract_version == schema.CURRENT_IDENTITY_CONTRACT_VERSION
    recovered_state = load_integrity_state(
        vault_path,
        expected_graph_generation=handle.graph_generation,
    )
    assert recovered_state.status is AuditStatus.UNVERIFIED
    assert recovered_state.writer_fenced is True
    assert handle.quarantine_path is not None
    assert handle.quarantine_path.exists()
    # The torn file was moved aside, garbage preserved.
    quarantined = handle.quarantine_path / "graph.lbug"
    assert quarantined.exists()
    assert quarantined.read_bytes() == garbage
    # A fresh, working graph is back in place with a usable schema.
    assert graph_path.exists()
    _verify_live_schema(handle.database, graph_path)


def test_bootstrap_recovers_from_corrupt_wal(tmp_path: Path) -> None:
    """A torn WAL (kill -9 mid-write) over an intact checkpoint quarantines ONLY the
    WAL and recovers the checkpoint — it must NOT wipe the main graph file."""
    vault_path = tmp_path / "vault"
    graph_path = vault_path / "graph.lbug"
    wal_path = vault_path / "graph.lbug.wal"

    original = bootstrap_vault_graph(vault_path)
    reset_bootstrap_cache_for_tests(vault_path)
    write_integrity_state(
        vault_path,
        GraphIntegrityState(
            status=AuditStatus.VERIFIED,
            graph_generation=original.graph_generation,
            writer_fenced=False,
            audit_id="pre-recovery-audit",
        ),
    )

    # A clean close checkpoints + removes the WAL; simulate a kill -9 that left a
    # torn WAL behind. ladybug fails checksum verification on the next open.
    wal_path.write_bytes(b"\xde\xad\xbe\xef" * 200)

    handle = bootstrap_vault_graph(vault_path)

    assert handle.recovered_from_corruption is True
    assert handle.recovered_mode == "checkpoint"
    assert handle.quarantine_path is not None
    # ONLY the torn WAL was quarantined; the main checkpoint stayed in place.
    assert (handle.quarantine_path / "graph.lbug.wal").exists()
    assert not (handle.quarantine_path / "graph.lbug").exists()
    assert graph_path.exists()
    recovered_state = load_integrity_state(
        vault_path,
        expected_graph_generation=handle.graph_generation,
    )
    assert recovered_state.status is AuditStatus.UNVERIFIED
    assert recovered_state.writer_fenced is True
    assert "recovered" in (recovered_state.reason or "")
    _verify_live_schema(handle.database, graph_path)


def test_torn_wal_recovers_checkpointed_claims(tmp_path: Path) -> None:
    """Durability guarantee (WAL-durability P0): a torn WAL over an intact checkpoint
    recovers the CLAIMS in that checkpoint, not an empty store.

    Regression for the replay that dropped 5389 checkpointed claims to 0 because the
    quarantine wiped the good ``graph.lbug`` alongside the torn ``graph.lbug.wal``.
    """
    vault_path = tmp_path / "vault"
    graph_path = vault_path / "graph.lbug"
    wal_path = vault_path / "graph.lbug.wal"

    handle = bootstrap_vault_graph(vault_path)
    conn = ladybug.Connection(handle.database)
    try:
        for i in range(25):
            conn.execute("CREATE (:Node {id: $id, type: 'Claim'})", {"id": f"claim-{i}"})
    finally:
        conn.close()
    # A clean close checkpoints the WAL into the main .lbug and removes the WAL.
    reset_bootstrap_cache_for_tests(vault_path)
    assert not wal_path.exists()

    # kill -9 mid-write leaves a torn WAL next to the intact checkpoint.
    wal_path.write_bytes(b"\xde\xad\xbe\xef" * 200)

    handle = bootstrap_vault_graph(vault_path)

    assert handle.recovered_from_corruption is True
    assert handle.recovered_mode == "checkpoint"
    assert handle.quarantine_path is not None
    assert (handle.quarantine_path / "graph.lbug.wal").exists()
    assert not (handle.quarantine_path / "graph.lbug").exists()
    assert graph_path.exists()
    # The checkpointed claims are RECOVERED — not 0.
    probe = ladybug.Connection(handle.database)
    try:
        result = probe.execute("MATCH (n:Node) WHERE n.type = 'Claim' RETURN count(n)")
        assert result.get_next()[0] == 25
    finally:
        probe.close()


def test_store_recovers_checkpointed_claims_through_public_api(tmp_path: Path) -> None:
    """The named durability deliverable, asserted through the REAL store surface:
    reopen a vault whose WAL was torn and confirm ``LadybugStore.list_nodes`` returns
    the checkpointed claims (not 0) and ``Vault.recovery_mode`` reports ``checkpoint``.
    Exercises the handle-wiring past ``bootstrap_vault_graph`` that the primitive-level
    tests skip."""
    from okto_neuron.core.schema import Node
    from okto_neuron.store.ladybug import LadybugStore
    from okto_neuron.vault import Vault

    vault_path = tmp_path / "vault"
    wal_path = vault_path / "graph.lbug.wal"

    store = LadybugStore(vault_path)
    for i in range(20):
        store.add_node(Node(id=f"claim-{i}", type="Claim", title=f"claim {i}"))
    store.close()
    # A clean close checkpoints the WAL into the main .lbug and removes the WAL.
    reset_bootstrap_cache_for_tests(vault_path)
    assert not wal_path.exists()

    # kill -9 mid-write leaves a torn WAL next to the intact checkpoint.
    wal_path.write_bytes(b"\xde\xad\xbe\xef" * 200)

    store = LadybugStore(vault_path)
    vault = Vault(vault_path, store)
    try:
        assert vault.recovered_from_corruption is True
        assert vault.recovery_mode == "checkpoint"
        claims = [n for n in store.list_nodes("Claim")]
        assert len(claims) == 20
    finally:
        store.close()


def test_wal_quarantine_preserves_bak_backup(tmp_path: Path) -> None:
    """marginalia's own ``graph.lbug.bak`` safety backup (heal/rebuild/reconcile) must
    NOT be swept into quarantine on a torn WAL — it stays as a manual fallback."""
    vault_path = tmp_path / "vault"
    graph_path = vault_path / "graph.lbug"
    wal_path = vault_path / "graph.lbug.wal"
    bak_path = vault_path / "graph.lbug.bak"

    bootstrap_vault_graph(vault_path)
    reset_bootstrap_cache_for_tests(vault_path)

    bak_bytes = b"prior-good-graph-backup"
    bak_path.write_bytes(bak_bytes)
    wal_path.write_bytes(b"\xde\xad\xbe\xef" * 200)

    handle = bootstrap_vault_graph(vault_path)

    assert handle.recovered_mode == "checkpoint"
    assert handle.quarantine_path is not None
    # The .bak stayed in place; only the WAL was quarantined.
    assert bak_path.exists()
    assert bak_path.read_bytes() == bak_bytes
    assert not (handle.quarantine_path / "graph.lbug.bak").exists()
    assert (handle.quarantine_path / "graph.lbug.wal").exists()
    assert graph_path.exists()


def test_quarantine_dir_increments_on_repeat_recovery(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    graph_path = vault_path / "graph.lbug"

    bootstrap_vault_graph(vault_path)
    reset_bootstrap_cache_for_tests(vault_path)
    graph_path.write_bytes(b"\x00not-a-graph\xff" * 100)
    first = bootstrap_vault_graph(vault_path)
    reset_bootstrap_cache_for_tests(vault_path)

    graph_path.write_bytes(b"\x00not-a-graph\xff" * 100)
    second = bootstrap_vault_graph(vault_path)

    assert first.quarantine_path is not None and second.quarantine_path is not None
    assert first.quarantine_path != second.quarantine_path
    assert first.quarantine_path.name == "corrupt-graph"
    assert second.quarantine_path.name == "corrupt-graph-2"


def test_bootstrap_migrates_pre_embedding_dim_graph(tmp_path: Path) -> None:
    """A graph built before the ``embedding_dim`` column existed must auto-migrate
    and open, not crash. ``CREATE TABLE IF NOT EXISTS`` never adds the column to an
    existing table, so the schema-metadata MERGE would reference a missing property
    and the binder would fail — the idempotent ALTER migration prevents that.
    """
    vault_path = tmp_path / "vault"
    (vault_path / ".marginalia").mkdir(parents=True)
    graph_path = vault_path / "graph.lbug"

    # Build an OLD-schema Node table: every common column EXCEPT embedding_dim.
    migration_columns = {name for name, _column_type in schema._MIGRATION_NODE_COLUMNS}
    old_columns = [
        (name, "DOUBLE[384]" if ctype == schema._EMBEDDING_COLUMN_PLACEHOLDER else ctype)
        for name, ctype in schema._COMMON_NODE_COLUMNS
        if name not in migration_columns
    ]
    col_sql = ", ".join(f"{name} {ctype}" for name, ctype in old_columns)
    database = ladybug.Database(str(graph_path))
    connection = ladybug.Connection(database)
    connection.execute(f"CREATE NODE TABLE Node ({col_sql})")
    connection.execute(
        "CREATE (m:Node {id: $id, type: 'SchemaMetadata', schema_version: $v})",
        {"id": schema.SCHEMA_METADATA_NODE_ID, "v": schema.CURRENT_SCHEMA_VERSION},
    )
    # Confirm the pre-migration table genuinely lacks the column.
    with pytest.raises(Exception):
        connection.execute("MATCH (m:Node) RETURN m.embedding_dim")
    connection.close()
    database.close()

    reset_bootstrap_cache_for_tests(vault_path)
    handle = bootstrap_vault_graph(vault_path)  # must NOT raise BootstrapPartial

    assert handle is not None
    probe = ladybug.Connection(handle.database)
    try:
        # The column now exists and reads cleanly (NULL → None, accepted by the guard).
        assert schema._read_embedding_dim(probe) is None
        identity = schema.read_graph_identity(probe)
        assert identity.is_unset
        assert identity.is_legacy
        assert identity.graph_generation is None
        assert identity.identity_contract_version is None
    finally:
        probe.close()


def _verify_live_schema(database: ladybug.Database, graph_path: Path) -> None:
    connection = ladybug.Connection(database)
    try:
        schema.verify_schema_version(connection, file_path=graph_path)
    finally:
        connection.close()


def _hold_bootstrap_lock(vault_path: str, ready: Any, release: Any) -> None:
    if fcntl is None:
        raise RuntimeError("fcntl is required by this POSIX-only test helper")

    lock_path = Path(vault_path) / ".marginalia" / ".bootstrap.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        os.fsync(lock_file.fileno())
        ready.set()
        release.wait(10)
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.flush()
        os.fsync(lock_file.fileno())
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _hold_ladybug_database(graph_path: str, ready: Any, release: Any) -> None:
    database = ladybug.Database(graph_path)
    try:
        ready.set()
        release.wait(10)
    finally:
        database.close()
