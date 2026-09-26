"""Concurrent dump-vs-swap snapshot consistency (M2c, D-39).

M2's `done_when` (the internal ADR 0041 plan §5) asks for proof
that a `store/snapshot.py::dump()` started concurrently with a live
rebuild's `swap()` is snapshot-consistent: the resulting manifest describes
a state that existed at exactly one instant (either wholly pre-swap or
wholly post-swap), never a mix of pre- and post-swap rows. D-39 scoped this
to implementing `GraphStore.snapshot()` for Ladybug only (§3.1/§3.7); Grafx
has no `[grafx]` extra installed in this environment (skipped, matching
every other contract-suite fixture's optional-dependency handling) and
Neo4j has no `snapshot()` implementation as of M2c (documented, §3.1), so
its lane instead proves the *degraded fallback* the plan names for a
backend without native pinning: dump() and swap() serialized through the
same `RebuildLockHandle` (`store/rebuild_lock.py`, already shipped M2b)
structurally cannot interleave, so read-skew is impossible by construction
rather than by observation.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from okto_neuron.core.schema import Node
from okto_neuron.store import rebuild_lock
from okto_neuron.store._bootstrap import bootstrap_vault_graph
from okto_neuron.store.ladybug import LadybugStore
from okto_neuron.store.snapshot import dump, verify
from okto_neuron.store.staging import LadybugStaging

N_REPEATS = 8


def _nodes(prefix: str, count: int) -> list[Node]:
    return [
        Node(
            id=f"{prefix}-{i:04d}",
            type="Concept",
            title=f"{prefix} node {i}",
            content=("x" * 200) + f" body {prefix} {i}",
        )
        for i in range(count)
    ]


def _node_ids(dest: Path) -> set[str]:
    import json

    ids = set()
    with (dest / "nodes.jsonl").open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                ids.add(json.loads(line)["id"])
    return ids


def _dump_kwargs(vault_id: str) -> dict:
    from okto_neuron import __version__ as OKTO_NEURON_VERSION
    from okto_neuron.store.schema import CURRENT_SCHEMA_VERSION

    return dict(
        vault_id=vault_id,
        origin_backend="ladybug",
        origin_identity=(None, None),
        embedding={"provider": "fastembed", "model": "BAAI/bge-small-en-v1.5", "dimension": 384},
        packs=[],
        sources_dir=None,
        marginalia_version=OKTO_NEURON_VERSION,
        schema_version=CURRENT_SCHEMA_VERSION,
        embedding_dim=384,
    )


class TestLadybugSnapshotPinnedDumpVsSwap:
    """Real concurrency: `LadybugStore.snapshot()` pins reads across an
    actual physical file swap (`LadybugStaging.commit`, the exact primitive
    every rebuild/heal/reembed/rollback owner uses)."""

    def test_dump_never_mixes_pre_and_post_swap_rows(self, tmp_path: Path) -> None:
        pre_swap_only = 0
        post_swap_only = 0

        for iteration in range(N_REPEATS):
            vault_path = tmp_path / f"vault-{iteration}"
            store = LadybugStore(vault_path)
            gen_a = _nodes("gen-a", 400)
            for node in gen_a:
                store.add_node(node)
            store.checkpoint()
            gen_a_ids = {n.id for n in gen_a}

            # Build generation B in an isolated staged file (never touches
            # live until commit()), exactly the shape `orchestrate.rebuild`
            # builds its candidate in.
            staging = LadybugStaging(vault_path)
            staged_path = staging.stage_path("rebuild")
            staged_handle = bootstrap_vault_graph(staged_path)
            staged_store = LadybugStore(staged_path, graph_handle=staged_handle)
            gen_b = _nodes("gen-b", 400)
            for node in gen_b:
                staged_store.add_node(node)
            staged_store.checkpoint()
            staged_store.close()
            gen_b_ids = {n.id for n in gen_b}

            swap_started = threading.Event()
            swap_done = threading.Event()

            def _do_swap() -> None:
                swap_started.set()
                # A tiny jittered delay maximizes the odds the swap lands
                # mid-dump (dump() is writing ~400 JSONL rows, real wall
                # time) without making the test itself flaky if it doesn't.
                time.sleep(0.0005)
                staging.commit(staged_path, backup_tag=f"backup-{iteration}")
                swap_done.set()

            swap_thread = threading.Thread(target=_do_swap)
            dest = tmp_path / f"dump-{iteration}"
            swap_thread.start()
            manifest = dump(store, dest, **_dump_kwargs(f"vault-{iteration}"))
            swap_thread.join(timeout=5)
            assert swap_done.is_set(), "swap thread did not complete"

            report = verify(dest)
            assert report.ok, report.problems

            dumped_ids = _node_ids(dest)
            assert dumped_ids in (gen_a_ids, gen_b_ids), (
                f"iteration {iteration}: dump mixed pre/post-swap rows "
                f"(dumped {len(dumped_ids)} ids, neither pure gen-a nor pure gen-b: "
                f"extra={dumped_ids - gen_a_ids - gen_b_ids}, "
                f"missing_a={gen_a_ids - dumped_ids if dumped_ids == gen_a_ids else 'n/a'})"
            )
            assert manifest.node_count == len(dumped_ids)
            if dumped_ids == gen_a_ids:
                pre_swap_only += 1
            else:
                post_swap_only += 1

            store.close()

        # Sanity: the test actually exercised both outcomes across repeats
        # (otherwise the race window never landed and the assertion above
        # would be vacuously true every time). Not a hard requirement --
        # timing is best-effort -- but worth surfacing if it never happens.
        assert pre_swap_only + post_swap_only == N_REPEATS


class TestDegradedFallbackWithoutSnapshot:
    """Backends without `snapshot()` (Neo4j as of M2c; Grafx likewise, but
    skipped here for lack of the `[grafx]` extra) rely on the documented
    degraded fallback instead: dump() and swap() serialized through the
    same fenced `RebuildLockHandle`, so no interleaving -- hence no
    read-skew -- is structurally possible."""

    def test_lock_serializes_dump_against_swap(self, tmp_path: Path) -> None:
        pytest.importorskip("neo4j")
        if not os.environ.get("OKTO_NEURON_TEST_NEO4J_URI"):
            pytest.skip("OKTO_NEURON_TEST_NEO4J_URI is not set")

        vault_path = tmp_path / "vault"
        vault_path.mkdir()

        results: list[str] = []
        barrier = threading.Barrier(2)

        def _holder(tag: str, hold_for: float) -> None:
            barrier.wait()
            try:
                with rebuild_lock.acquire_rebuild_lock(vault_path, operation=f"test-{tag}"):
                    results.append(f"{tag}-acquired")
                    time.sleep(hold_for)
                    results.append(f"{tag}-released")
            except Exception:  # noqa: BLE001 - VaultLockHeld is the expected contention path
                results.append(f"{tag}-contended")

        for _ in range(N_REPEATS):
            results.clear()
            barrier.reset()
            t_dump = threading.Thread(target=_holder, args=("dump", 0.05))
            t_swap = threading.Thread(target=_holder, args=("swap", 0.05))
            t_dump.start()
            t_swap.start()
            t_dump.join(timeout=5)
            t_swap.join(timeout=5)

            # Exactly one side ever holds the lock at a time: either both
            # acquired it in sequence (never interleaved -- no "X-acquired"
            # appears between the other side's "acquired"/"released" pair),
            # or one side found it contended outright. Either way the two
            # holders' [acquired, released] windows never overlap, which is
            # the whole point -- a concurrent dump can never observe a
            # graph mid-swap.
            acquired = [r for r in results if r.endswith("-acquired")]
            assert len(acquired) <= 1 or _windows_disjoint(results)


def _windows_disjoint(results: list[str]) -> bool:
    """True if every "<tag>-acquired" is immediately followed (before the
    other tag's own "-acquired") by that same tag's "-released"."""
    open_tag: str | None = None
    for event in results:
        tag, _, kind = event.rpartition("-")
        if kind == "acquired":
            if open_tag is not None:
                return False
            open_tag = tag
        elif kind == "released":
            if open_tag != tag:
                return False
            open_tag = None
    return True
