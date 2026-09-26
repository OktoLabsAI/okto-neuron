"""ADR 0009 P3 heal — DETERMINISTIC topology collapse (graph→fresh-graph copy, NO LLM).

Where Option A (ADR 0008) is a query-time view over an UNCHANGED graph, the heal
actually folds the confirmed equivalence classes into the graph topology — but it
does so the ONE corruption-free way AND without re-running the LLM: a deterministic
graph→fresh-graph COPY + atomic swap.

The entities are already extracted. There is nothing for an LLM to do; the heal only
needs to materialize the off-graph fold (``member_id -> canonical_id``, the SAME map
recall/Browse/Graph already fold through) into a new graph file. So it:

1. reads the live graph's nodes + edges,
2. hands them to :func:`okto_neuron.curation.orchestrate.heal`, which runs
   :func:`okto_neuron.store.reembed.copy_graph_canonicalizing` — drops variant
   nodes that merge onto a canonical, remaps edge/claim refs through the equivalence
   map, drops self-loops the collapse creates, dedups by content-addressed edge id —
   into a FRESH graph (append-only and fully auditable before it becomes live),
3. atomic-swaps the fresh graph onto live (the same shared swap tail every
   rebuild/heal/reembed owner now drives, M2b spec §2.3).

Reversibility: the off-graph authority records remain the alias source and still
describe the now-applied merges. The markdown trust root is unchanged, so a full
re-derivation is always available via ``kg rebuild`` (markdown + LLM). The heal is
itself re-runnable and idempotent — a second heal on an already-collapsed graph is a
verbatim copy.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from okto_neuron.reconcile.authority import AuthorityIndex

if TYPE_CHECKING:
    from okto_neuron.store.rebuild_lock import RebuildLockHandle

_LOG = logging.getLogger(__name__)


def heal_via_copy(
    vault: Path | None,
    *,
    authority: AuthorityIndex | None = None,
    return_stats: bool = False,
) -> int | tuple[int, dict[str, int]]:
    """Run standalone heal under the same cross-process lease as other swaps."""
    from okto_neuron.cli.kg import _ensure_vault_directory, _resolve_rebuild_vault
    from okto_neuron.store.rebuild_lock import acquire_rebuild_lock

    vault_path = _resolve_rebuild_vault(vault)
    _ensure_vault_directory(vault_path)
    with acquire_rebuild_lock(vault_path, operation="reconcile heal") as lock:
        return _heal_via_copy_owned(
            vault_path,
            lock,
            authority=authority,
            return_stats=return_stats,
        )


def _heal_via_copy_owned(
    vault_path: Path,
    lock: "RebuildLockHandle",
    *,
    authority: AuthorityIndex | None = None,
    return_stats: bool = False,
) -> int | tuple[int, dict[str, int]]:
    """Run the deterministic heal: read the live graph, then delegate the fold +
    atomic swap to :func:`okto_neuron.curation.orchestrate.heal`.

    This CLI owner keeps only the two things that are genuinely specific to it
    (M2b spec §2.3): the bootstrap lock + close-live-handles-first-then-key-the-
    staged-store-on-``vault_path`` sequencing, and reading the live graph off a RAW
    closed handle rather than a still-serving one — distinct from the daemon's
    ``run_heal`` runner, which keeps the live handle serving and marshals the swap
    onto the event loop. Everything from equivalence/predicate-alias resolution
    through the staged build and the swap tail (pre-swap audit -> commit ->
    fence-generation -> post-swap audit -> publish) now lives in ``orchestrate.heal``
    so it is no longer hand-copied here.
    """
    from okto_neuron.cli.kg import (
        _audit_rebuild_graph_path,
        _audit_reopened_staged_store,
        _bootstrap_graph_at_path,
        _close_live_graph_handles,
        _ensure_live_graph_exists,
        _live_graph_path,
        _load_storage_config,
        _mark_rebuild_generation_verifying,
        _open_live_store,
        _publish_integrity_result,
        _resolve_pinned_backend,
        _swap_construction_for,
    )
    from okto_neuron.curation.orchestrate import heal as _orchestrate_heal
    from okto_neuron.store._bootstrap import _BOOTSTRAP_LOCK, _MARGINALIA_DIR, _bootstrap_lock

    marginalia_dir = vault_path / _MARGINALIA_DIR
    marginalia_dir.mkdir(parents=True, exist_ok=True)
    lock_path = marginalia_dir / _BOOTSTRAP_LOCK

    with _bootstrap_lock(vault_path, lock_path):
        _close_live_graph_handles(vault_path)

        # M4 spec §2 ("one construction seam generalized"): resolved once,
        # right after lock acquisition (NOT before — a lock-contention
        # caller must see the lock error before any config read, mirroring
        # ``cli/kg.py``'s own placement in ``_kg_rebuild_owned``/
        # ``_kg_reembed_owned``) and defensively: this function never read
        # ``okto-neuron.yaml`` before M4, so an absent/unreadable config
        # falls back to ``"ladybug"`` rather than newly crashing a heal
        # against a bare/never-initialized vault.
        try:
            backend_name = _resolve_pinned_backend(vault_path)
        except Exception:  # noqa: BLE001 — missing/partial config → default backend
            backend_name = "ladybug"
        graph_path = _live_graph_path(vault_path, backend_name)

        # A never-bootstrapped vault has nothing to heal AND no swap target; create a
        # fresh empty graph so the swap below has a target (mirrors kg_reembed). The
        # extra close only runs when a bootstrap actually happened — byte-identical
        # to the pre-M4 Ladybug-only ``if not graph_path.exists(): ...; _close_live_
        # graph_handles(vault_path)`` shape, now backend-neutral.
        needed_bootstrap = not graph_path.exists()
        _ensure_live_graph_exists(
            vault_path,
            backend_name,
            graph_path,
            storage_config=_load_storage_config(vault_path),
        )
        if needed_bootstrap:
            _close_live_graph_handles(vault_path)

        # Read the live graph. Ladybug uses a RAW handle (bypasses the
        # bootstrap lock we already hold + the dim-guard); a non-Ladybug
        # backend has no such guard to bypass (M4 spec §2 item 1). Either
        # way, close it so the swap is clean.
        nodes: list = []
        edges: list = []
        # Neo4j's live "path" is a synthetic marker (``_live_graph_path``)
        # never written to disk -- ``graph_path.exists()`` is always False
        # for it, even once ``_ensure_live_graph_exists`` above has
        # self-bootstrapped (or a prior swap has populated) the actual
        # metadata singleton in the database. Reading unconditionally for
        # neo4j is safe: an empty database just reads back as empty nodes.
        if backend_name == "neo4j" or graph_path.exists():
            live_store = _open_live_store(
                vault_path, backend_name, _load_storage_config(vault_path)
            )
            try:
                nodes = list(live_store.list_nodes())
                edges = list(live_store.list_edges())
            finally:
                live_store.close()
            _close_live_graph_handles(vault_path)

        # M4 spec §2 ("one construction seam generalized"): registry-driven
        # staging port + staged-store opener — a ladybug-pinned (or
        # unpinned, pre-M4) vault gets byte-identical behaviour (opener
        # ``None`` lets orchestrate.heal fall back to its own default).
        staging, open_staged_store = _swap_construction_for(vault_path, backend_name)

        def _audit_graph_path_for_backend(
            audited_path, *, dim, expected_identity, stage
        ):
            if open_staged_store is None:
                return _audit_rebuild_graph_path(
                    audited_path, dim=dim, expected_identity=expected_identity, stage=stage
                )
            return _audit_reopened_staged_store(
                vault_path,
                audited_path,
                dim=dim,
                expected_identity=expected_identity,
                stage=stage,
                open_staged_store=open_staged_store,
            )

        _, stats = _orchestrate_heal(
            vault_path,
            lock,
            authority=authority,
            live_nodes=nodes,
            live_edges=edges,
            staging=staging,
            bootstrap_graph_at_path=_bootstrap_graph_at_path,
            open_staged_store=open_staged_store,
            audit_graph_path=_audit_graph_path_for_backend,
            mark_generation_verifying=_mark_rebuild_generation_verifying,
            publish_integrity_result=_publish_integrity_result,
            close_live_handles=_close_live_graph_handles,
            live_graph_path=graph_path,
        )

    _LOG.info("reconcile heal copy completed: %s", stats)
    if return_stats:
        return 0, stats
    return 0


__all__ = ["heal_via_copy"]
