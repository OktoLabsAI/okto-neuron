"""HealthPort M3 slice contract (M3 spec §2.4/§4): ``recovery_status()`` /
``detect_drift()`` on every ``GraphStore``.

The cross-backend assertions run against the shared ``graph_store`` fixture
(ladybug, memory, stub) and only pin the shape every backend must honor: a
fresh store reports itself unrecovered, and a caller-observed generation that
still matches reports no drift. Real corruption-recovery and real on-disk
drift are Ladybug-only concepts -- the in-memory and stub backends have no
on-disk state to recover from or drift against (see their own
``recovery_status``/``detect_drift`` docstrings) -- so those are exercised
directly against ``LadybugStore``, mirroring ``tests/test_bootstrap.py``'s
proven corruption fixture rather than re-deriving it.
"""

from __future__ import annotations

import os
from pathlib import Path

from okto_neuron.store.ladybug import LadybugStore
from okto_neuron.store.protocol import DriftReport, RecoveryStatus

# --- cross-backend: ladybug, memory, stub (tests/store/contract/conftest.py) --
#
# The stub fixture package (tests/fixtures/stub_backend_pkg) predates
# RecoveryStatus landing on GraphStore and returns a locally-defined,
# same-shaped stand-in rather than the real dataclass -- GraphStore is a
# structural Protocol, so a nominal isinstance check is not the actual
# contract. Assert on the shape (.recovered) here; the nominal type is
# pinned separately for Ladybug, which does use the real dataclass.


def test_recovery_status_on_a_fresh_store_reports_unrecovered(graph_store) -> None:
    status = graph_store.recovery_status()
    assert hasattr(status, "recovered")
    assert status.recovered is False


def test_detect_drift_with_the_store_s_own_current_generation_reports_none(graph_store) -> None:
    assert graph_store.detect_drift(graph_store.generation()) is None


def test_is_closed_is_false_before_close_and_true_after(graph_store) -> None:
    """``is_closed`` (D-49) is part of the ``GraphStore`` Protocol itself, not
    a ``LadybugStore``-only reflection target — the vault open cache
    (``store/vault.py``'s ``_open_vault``) reads it on every cached backend
    to tell a still-live handle apart from one a caller already closed."""
    assert graph_store.is_closed is False
    graph_store.close()
    assert graph_store.is_closed is True


# --- Ladybug-only: real corruption recovery and real on-disk drift ---------


def test_ladybug_recovery_status_reflects_real_corruption_recovery(tmp_path: Path) -> None:
    """A clobbered main graph file must warm-recover (tests/test_bootstrap.py's
    ``test_bootstrap_recovers_from_corrupt_graph_file`` fixture, one layer up
    through ``LadybugStore.recovery_status()`` instead of the raw handle)."""
    vault_path = tmp_path / "vault"
    graph_path = vault_path / "graph.lbug"

    LadybugStore(vault_path).close()  # bootstrap once, then fully release

    # Simulate a kill -9 that left the main graph file torn.
    graph_path.write_bytes(b"\x00not-a-graph\xff" * 100)

    store = LadybugStore(vault_path)
    try:
        status = store.recovery_status()
        assert isinstance(status, RecoveryStatus)
        assert status.recovered is True
        # A torn MAIN file has no recoverable checkpoint -> empty fallback,
        # matching VaultGraphHandle.recovered_mode.
        assert status.mode == "empty"
    finally:
        store.close()


def test_ladybug_detect_drift_flags_a_generation_mismatch(tmp_path: Path) -> None:
    store = LadybugStore(tmp_path / "vault")
    try:
        assert store.detect_drift(store.generation()) is None

        report = store.detect_drift("not-a-real-generation")

        assert isinstance(report, DriftReport)
        assert report.reason
    finally:
        store.close()


def test_ladybug_detect_drift_flags_an_on_disk_file_swap_under_an_open_handle(
    tmp_path: Path,
) -> None:
    """An external process replacing graph.lbug's inode out from under an
    already-open handle must be caught even when the caller's *expected*
    generation still matches what the handle believes -- the on-disk file
    identity itself has moved."""
    live_path = tmp_path / "live"
    replacement_path = tmp_path / "replacement"
    LadybugStore(replacement_path).close()  # a second, distinct graph.lbug

    store = LadybugStore(live_path)
    try:
        expected_generation = store.generation()
        assert store.detect_drift(expected_generation) is None

        # Deliberately bypass the lease API to model a legacy/external
        # rename while this handle stays open (same pattern as
        # tests/server/test_vault_pool_leases.py's raw-swap test).
        os.replace(replacement_path / "graph.lbug", live_path / "graph.lbug")

        report = store.detect_drift(expected_generation)

        assert report is not None
        assert report.reason
    finally:
        store.close()
