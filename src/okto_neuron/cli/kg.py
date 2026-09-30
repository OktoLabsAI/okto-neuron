"""Storage KG command entry points."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import shutil
import signal
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING
from uuid import uuid4

import click
import ladybug
import yaml

from okto_neuron._compat import secret_env as _secret_env
from okto_neuron._compat import vault_config_path
from okto_neuron import __version__
from okto_neuron.curation import orchestrate
from okto_neuron.errors import (
    BootstrapPartial,
    IngestError,
    RebuildAuditFailed,
    RebuildInterrupted,
    RebuildSwapFailed,
    VaultLockHeld,
    VaultNotFoundError,
    VaultPathNotADirectory,
)
from okto_neuron.store import _open_vault, schema, snapshot
from okto_neuron.store._bootstrap import (
    VaultGraphHandle,
    _bootstrap_lock,
    _graph_file_identity,
    _resolve_configured_dim,
    reset_bootstrap_cache_for_tests,
)
from okto_neuron.store.integrity import (
    AuditStatus,
    IntegrityAuditResult,
    IntegrityIssue,
    audit_graph,
)
from okto_neuron.store.integrity_state import (
    GraphIntegrityState,
    integrity_state_path,
    load_integrity_state,
    write_integrity_state,
)
from okto_neuron.store.ladybug import LadybugStore, VaultConnection, verify_ladybug_db_health
from okto_neuron.store.rebuild_lock import RebuildLockHandle, acquire_rebuild_lock
from okto_neuron.store.vault_writer import vault_writer

# M2b (spec §2.1/§3): _active_graph_sidecars/_copy_closed_graph_checkpoint have
# no remaining bare-name caller in this module (their only prior callers were
# the swap primitives above, now relocated alongside them); re-exported
# "as"-style so external module-qualified access (server/_curation.py's and
# tests' kg_cli.<name>(...)) keeps working without ruff flagging them unused.
from okto_neuron.store.staging import _active_graph_sidecars as _active_graph_sidecars
from okto_neuron.store.staging import (
    _copy_closed_graph_checkpoint as _copy_closed_graph_checkpoint,
)
from okto_neuron.store.staging import (
    _discard_graph_family,
    _fsync_parent_dir,
    _move_graph_family,
    _swap_rebuilt_graph,
)
from okto_neuron.vault_registry import resolve_vault_reference

if TYPE_CHECKING:
    from okto_neuron.store.ladybug import LadybugStore as LadybugStoreType
    from okto_neuron.store.staging import StagingPort

_LOG = logging.getLogger("okto_neuron.cli.kg")

_SCAFFOLD_LAYOUT = ("notes/", "refs/", ".marginalia/", "okto-neuron.yaml")
_MARGINALIA_DIR = ".marginalia"
_BOOTSTRAP_LOCK = ".bootstrap.lock"
_GRAPH_FILE = "graph.lbug"
_REBUILD_STATE = "rebuild.state.json"
_REEMBED_STATE = "reembed.state.json"
_ROLLBACK_STATE = "rollback.state.json"
# Write reembed progress at most every N nodes — a state write fsyncs, so a write
# per node would dominate the (cheap) embed work on a large graph.
_REEMBED_PROGRESS_EVERY = 25
_HASH_CHUNK_SIZE = 1024 * 1024
_REBUILD_INTERRUPT = threading.local()
_REBUILD_SIGNALS = (signal.SIGINT, signal.SIGTERM)
_REBUILD_AUDIT_SAMPLE_LIMIT = 10
_REBUILD_ARTIFACTS_DIR = "rebuild-artifacts"
_PREVIOUS_SEMANTIC_MATERIALIZATION = "previous-semantic-materialization.json"
_PREVIOUS_SEMANTIC_POLICY = "previous-semantic-policy.json"
_SEMANTIC_POLICY_CHECKPOINT_SCHEMA = "semantic_policy_checkpoint.v1"
_PENDING_SEMANTIC_POLICY = "pending-semantic-policy.json"
_DISCOVERED_SEMANTIC_POLICY = "discovered-semantic-policy.json"
_SEMANTIC_DISCOVERY_CHECKPOINT_SCHEMA = "semantic_discovery_checkpoint.v1"
# Policy-changing passes discover durable semantic decisions; none of them can
# prove that the resulting policy is stable. Keep that mutation budget separate
# from the one final pass that must materialize the whole corpus without drift.
_MAX_SEMANTIC_DISCOVERY_PASSES = 3
_MAX_SEMANTIC_STABILIZATION_PASSES = _MAX_SEMANTIC_DISCOVERY_PASSES + 1
_REBUILD_ERROR_LIMIT = 2_000


def kg_init(
    vault: Path,
    *,
    backend: str | None = None,
    # Deprecated, no longer required: no backend is D-12-gated any more
    # (Okto Grafx is now the default, non-experimental graph backend).
    # Accepted-and-ignored so an old script/test that passes it does not
    # break; kept default ``True`` for the same reason it always was.
    accept_experimental: bool = True,
    storage_uri: str | None = None,
    storage_credential_env: str | None = None,
    storage_database: str | None = None,
    storage_allow_remote: bool = False,
) -> int:
    """Initialize a vault graph and print the scaffolded layout.

    ``backend`` is only ever a caller-supplied ``--backend`` value (``None``
    when the flag was omitted, per the CLI wrapper's own default-vs-explicit
    check) — an explicit value is validated and, when this vault has no
    ``okto-neuron.yaml`` yet, pinned before the graph opens; against an
    already-initialized vault it only re-validates the existing pin, matching
    the "no --backend on re-init" contract the other create paths share (M3
    spec section 2.5). ``backend=None`` (no ``--backend`` flag at all) is
    unaffected by the M4/D-12 retirement below: it still leaves
    ``okto-neuron.yaml`` untouched and defers entirely to
    ``store.vault._open_vault``'s own scaffolding, matching every prior
    release's behavior (fresh vault -> unpinned config -> the ``"ladybug"``
    absent-``storage``-key fallback) rather than newly opting a bare
    ``kg init`` into ``DEFAULT_NEW_VAULT_BACKEND``.
    """
    vault_path = Path(vault)
    # An existing vault is refused while the daemon writes it; a new path is taken.
    with vault_writer(vault_path, "kg init"):
        if backend is not None:
            from okto_neuron.cli import _resolve_and_pin_backend

            _resolve_and_pin_backend(vault_path, backend)
            if not (vault_config_path(vault_path)).exists():
                _write_kg_init_vault_config(
                    vault_path,
                    backend,
                    storage_uri=storage_uri,
                    storage_credential_env=storage_credential_env,
                    storage_database=storage_database,
                    storage_allow_remote=storage_allow_remote,
                )
        store = _open_vault(vault_path)
        try:
            click.echo(_format_scaffold_layout(vault_path))
        finally:
            close = getattr(store, "close", None)
            if callable(close):
                close()
        return 0


def _write_kg_init_vault_config(
    vault_path: Path,
    backend: str,
    *,
    storage_uri: str | None = None,
    storage_credential_env: str | None = None,
    storage_database: str | None = None,
    storage_allow_remote: bool = False,
) -> None:
    """Pre-write ``okto-neuron.yaml`` with a pinned storage block before ``_open_vault``.

    Mirrors ``kg_snapshot_load``'s ``_write_snapshot_vault_config`` pattern:
    ``kg init`` bypasses ``Vault.scaffold`` entirely (M3 spec D-46), so this
    is the only place an explicit ``--backend`` can reach the config before
    ``store.vault._open_vault``'s own ``_write_default_config_if_absent``
    would otherwise write an unpinned default. Every other field comes from
    ``VaultConfig.default()``, unchanged from today's default-config
    behavior — only the ``storage`` block is added, in the same shape
    ``Vault._write_config`` emits (``vault.py:611-637``).
    """
    from okto_neuron.config import VaultConfig

    config = VaultConfig.default().model_dump(mode="json", exclude_none=True)
    storage: dict[str, object] = {"backend": backend}
    if backend in ("ladybug", "grafx"):
        storage["reason"] = None
    if storage_uri is not None:
        storage["uri"] = storage_uri
    if storage_credential_env is not None:
        storage["credential_env"] = storage_credential_env
    if storage_database is not None:
        storage["database"] = storage_database
    if storage_allow_remote:
        storage["allow_remote"] = True
    if backend == "neo4j":
        import uuid

        storage["vault_id"] = uuid.uuid4().hex
    config["storage"] = storage
    try:
        vault_path.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError) as exc:
        raise VaultPathNotADirectory(vault_path, cause=exc) from exc
    config_path = vault_config_path(vault_path)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    os.chmod(config_path, 0o644)


def _format_scaffold_layout(vault: Path) -> str:
    root = Path(vault).expanduser().resolve(strict=False)
    lines = [f"{root}/"]
    lines.extend(_SCAFFOLD_LAYOUT)
    return "\n".join(lines)


def _resolve_pinned_backend(vault_path: Path) -> str:
    """The vault's pinned graph backend name (``storage.backend`` in
    ``okto-neuron.yaml``), or ``"ladybug"`` for a vault with no ``storage``
    block — the same default ``store/vault.py``'s own
    ``_read_pinned_backends`` uses (M3 spec §2.1)."""
    from okto_neuron.config import VaultConfig

    storage = VaultConfig.load(vault_path).storage
    return storage.backend if storage is not None else "ladybug"


def _swap_construction_for(
    vault_path: Path,
    backend_name: str,
) -> tuple["StagingPort", "orchestrate.OpenStagedStoreFn | None"]:
    """Registry-driven staging port + staged-store opener (M4 spec §2, "one
    construction seam generalized").

    Ladybug — the default, and every pre-M4 vault with no ``storage`` pin —
    returns Ladybug's own staging port and ``None`` for the opener, which
    lets ``orchestrate.heal``/``orchestrate.reembed`` fall back to their
    byte-identical default closure (unchanged behaviour). A ``grafx``-pinned
    vault gets a staging port from the sibling ``staging_for`` factory
    (``store/staging.py``, imported lazily: added alongside this seam, not
    yet importable at module load for every environment) plus a closure that
    bootstraps a fresh ``GrafxStore`` at the staged path and reports its
    identity from the store itself (``store.generation()`` — Grafx has no
    ``VaultGraphHandle`` to read ``graph_generation``/
    ``identity_contract_version`` off of).
    """
    from okto_neuron.store.staging import staging_for

    if backend_name == "neo4j":
        from okto_neuron.config import VaultConfig
        from okto_neuron.store.neo4j import Neo4jStore

        storage_config = VaultConfig.load(vault_path).storage
        # Neo4jStaging needs a live handle to run its Cypher (no path-only
        # way to reach the database) -- opened once here and reused by the
        # staged-store opener closure below, so both share one connection
        # for the whole swap.
        live_store = Neo4jStore.from_vault(vault_path, storage_config)
        staging = staging_for(vault_path, backend_name, store=live_store)

        def _open_neo4j_staged_store(
            vp: Path, staged_path: Path, dim: int
        ) -> tuple[object, schema.GraphIdentity]:
            # ``staged_path`` is the synthetic marker path (see
            # ``Neo4jStaging.stage_path``); ``Neo4jStore.__init__`` detects
            # it and opens in build mode against that generation tag, kept
            # isolated from the live graph until ``staging.commit`` flips
            # the metadata singleton's pointer.
            del vp, dim  # Neo4j has no fixed embedding-dim schema to bootstrap.
            store = Neo4jStore(staged_path, config=storage_config)
            return store, schema.GraphIdentity(
                store.generation(), schema.CURRENT_IDENTITY_CONTRACT_VERSION
            )

        return staging, _open_neo4j_staged_store

    staging = staging_for(vault_path, backend_name)
    if backend_name != "grafx":
        return staging, None

    def _open_grafx_staged_store(
        vp: Path, staged_path: Path, dim: int
    ) -> tuple[object, schema.GraphIdentity]:
        # ``vp`` unused: the staged path already carries the real vault root
        # as its own parent (`GrafxStore._is_graph_directory_path`), which is
        # all the store needs to resolve config-file fallbacks. ``dim`` is
        # NOT unused — the caller (`curation/orchestrate.py`'s `heal`/
        # `reembed`) already resolved it authoritatively (the live graph's
        # actual stored width for `heal`, or the `reembed` target width,
        # which may not be written to ``okto-neuron.yaml`` yet) and it must
        # win over whatever `GrafxStore` would otherwise read from the vault
        # config, so it is passed straight through as the fresh-bootstrap
        # override instead of being discarded.
        del vp
        from okto_neuron.store.grafx import GrafxStore

        store = GrafxStore(staged_path, embedding_dim=dim)
        return store, schema.GraphIdentity(
            store.generation(), schema.CURRENT_IDENTITY_CONTRACT_VERSION
        )

    return staging, _open_grafx_staged_store


def _live_graph_path(vault_path: Path, backend_name: str) -> Path:
    """The pinned backend's on-disk LIVE graph path (M4 spec §2, "one
    construction seam generalized" — items 1 and 3).

    Ladybug: a single file, ``vault_path / _GRAPH_FILE`` — the same literal
    every pre-M4 call site hardcoded. Grafx: a directory,
    ``vault_path / GrafxStore``'s own live-directory name (imported lazily —
    see ``_swap_construction_for``'s own lazy Grafx import for why). Every
    other/unregistered backend falls back to the Ladybug literal too, since
    no third backend is registered yet to disagree with it.
    """
    if backend_name == "grafx":
        from okto_neuron.store.grafx import _GRAPH_DIR_NAME

        return vault_path / _GRAPH_DIR_NAME
    if backend_name == "neo4j":
        from okto_neuron.store.neo4j import LIVE_MARKER_TAG
        from okto_neuron.store.staging import _NEO4J_MARKER_DIR

        return vault_path / _NEO4J_MARKER_DIR / LIVE_MARKER_TAG
    return vault_path / _GRAPH_FILE


def _load_storage_config(vault_path: Path) -> object | None:
    """Best-effort ``storage:`` config load, for backends whose store
    construction needs it (e.g. Neo4j's ``config.uri``). Returns ``None`` for
    a vault with no ``storage`` block (Ladybug/Grafx never read it)."""
    from okto_neuron.config import VaultConfig

    return VaultConfig.load(vault_path).storage


def _open_live_store(
    vault_path: Path,
    backend_name: str,
    storage_config: object | None,
) -> object:
    """Open the LIVE graph for a bare read (heal/reembed's pre-swap source
    read) or a plain existence-bootstrap (M4 spec §2 item 1).

    Ladybug keeps today's exact raw-handle path (``_open_live_handle``
    bypasses the bootstrap-lock re-entry + the dim-guard — safe only because
    every caller already holds the bootstrap lock for the whole read). Grafx
    and Neo4j DO have the same dim-guard concept as Ladybug — each backend's
    own ``__init__`` raises ``EmbeddingDimMismatch`` on every non-fresh open
    whose stored width disagrees with the vault's configured width, exactly
    like Ladybug's — so calling their guarded ``from_vault`` here would trip
    that guard on every ``kg reembed`` (whose entire job is to read a graph
    at its OLD width right after the configured width has changed) and
    refuse the very read that exists to fix the mismatch. Both backends
    therefore expose their own raw-open counterpart, ``open_for_live_read``,
    used here instead of ``from_vault`` — mirroring Ladybug's
    ``_open_live_handle`` bypass, just without a separate handle object
    (neither backend's constructor has a bootstrap-lock-reentry concept to
    bypass; only the dim-guard needs bypassing here, and each already
    tolerates being reopened by whichever process gets there first — D-10's
    retry-based metadata adoption). Any other/unregistered backend falls
    back to the registry's own construction (``from_vault`` when the class
    exposes one, matching ``store/vault.py``'s ``_construct_backend``).

    Caller closes the returned store exactly once (``store.close()``) — for
    Ladybug this fully releases the handle, mirroring ``VaultConnection.
    close_vault`` already popping and closing the same handle object the
    caller passed in.
    """
    if backend_name == "ladybug":
        graph_path = vault_path / _GRAPH_FILE
        live_handle = _open_live_handle(vault_path, graph_path)
        return LadybugStore(vault_path, graph_handle=live_handle)

    if backend_name == "grafx":
        from okto_neuron.store.grafx import GrafxStore

        return GrafxStore.open_for_live_read(vault_path, storage_config)

    if backend_name == "neo4j":
        from okto_neuron.store.neo4j import Neo4jStore

        return Neo4jStore.open_for_live_read(vault_path, storage_config)

    from okto_neuron.store.registry import resolve_graph_backend

    graph_cls = resolve_graph_backend(backend_name)
    from_vault = getattr(graph_cls, "from_vault", None)
    if callable(from_vault):
        return from_vault(vault_path, storage_config)
    return graph_cls(vault_path)


def _ensure_live_graph_exists(
    vault_path: Path,
    backend_name: str,
    graph_path: Path,
    *,
    dim: int | None = None,
    storage_config: object | None = None,
) -> None:
    """Bootstrap an empty live graph when nothing has been written yet, so a
    later ``StagingPort.commit`` always has a swap target to back up (mirrors
    every pre-M4 "if not graph_path.exists(): bootstrap fresh" call in
    ``kg_rebuild``/``kg_reembed``/``reconcile heal``).
    """
    if graph_path.exists():
        return
    if backend_name == "ladybug":
        live = _bootstrap_graph_at_path(vault_path, graph_path, dim)
        live.close()
        _fsync_parent_dir(graph_path)
        return
    # Any other backend's store self-bootstraps its schema on first open
    # (e.g. ``GrafxStore.__init__`` runs its DDL when the catalog is empty) —
    # opening and closing once is enough to materialize the live directory.
    store = _open_live_store(vault_path, backend_name, storage_config)
    close = getattr(store, "close", None)
    if callable(close):
        close()


def kg_rebuild(
    vault: Path | None = None,
    *,
    ingest: Callable[[Path, "LadybugStoreType"], object | None] | None = None,
    source_files: Sequence[Path] | None = None,
) -> int:
    """Run a standalone rebuild while owning the vault's cross-process handle lease."""
    vault_path = _resolve_rebuild_vault(vault)
    _ensure_vault_directory(vault_path)
    with acquire_rebuild_lock(vault_path, operation="rebuild") as ownership:
        return _kg_rebuild_owned(
            vault_path,
            ownership,
            ingest=ingest,
            source_files=source_files,
        )


def _kg_rebuild_owned(
    vault_path: Path,
    ownership: RebuildLockHandle,
    *,
    ingest: Callable[[Path, "LadybugStoreType"], object | None] | None = None,
    source_files: Sequence[Path] | None = None,
) -> int:
    """Rebuild a vault graph from the markdown trust root into a FRESH graph.

    The vault's markdown (notes/, refs/, and ``.marginalia/sources/``) is the
    canonical source; the graph is derived. Rebuild re-extracts every source file
    through the SAME full pipeline live ingest uses (``Companion.remember`` —
    deterministic Document/Block/Claim PLUS LLM entity/relationship extraction)
    into a brand-new empty graph, then atomic-swaps it onto LIVE.

    Why fresh-graph-only: rebuild is an all-or-nothing derivation from the trust
    root. A fresh generation prevents partial topology and mixed semantic policy
    from becoming visible, then the verified result is swapped atomically. The
    Ladybug boundary also treats edge topology identity as immutable; rebuild never
    has to repurpose an existing edge id.

    ``ingest`` is an injectable per-file ``(path, store)`` seam for tests
    (deterministic, model-free). When ``None`` (production), the default drives the
    full companion pipeline against the fresh temp store. ``source_files`` is the
    private acceptance-only exact-permutation seam; the public CLI never supplies
    it, so normal rebuilds remain canonically ordered.
    """
    marginalia_dir = vault_path / _MARGINALIA_DIR
    marginalia_dir.mkdir(parents=True, exist_ok=True)
    lock_path = marginalia_dir / _BOOTSTRAP_LOCK
    state_path = marginalia_dir / _REBUILD_STATE
    with _bootstrap_lock(vault_path, lock_path):
        _close_live_graph_handles(vault_path)
        # M4 spec §2 ("one construction seam generalized"): resolved once,
        # right after lock acquisition (NOT before — ``_resolve_pinned_backend``
        # reads ``okto-neuron.yaml``, which a lock-contention caller may not
        # even have written yet; the lock-contention error must win, matching
        # every pre-M4 vault's behavior), so both the build head (staged
        # store construction, below) and the swap tail (further down) share
        # one registry-driven pin instead of re-resolving it twice. Ladybug —
        # the default, and every pre-M4 vault with no ``storage`` pin — gets
        # byte-identical values: ``graph_path``/``tmp_graph_path`` match
        # today's own literal computation exactly (``LadybugStaging.
        # stage_path`` is a pure computation, no side effect), and
        # ``open_staged_store`` stays ``None`` so the build head below keeps
        # its unmodified Ladybug path.
        backend_name = _resolve_pinned_backend(vault_path)
        staging, open_staged_store = _swap_construction_for(vault_path, backend_name)
        graph_path = _live_graph_path(vault_path, backend_name)
        # NOT ``graph.lbug.tmp``: ladybug owns ``<db>.tmp`` as its OWN scratch file for
        # any LIVE ``graph.lbug`` handle (e.g. a concurrent ``okto-neuron serve`` daemon),
        # and closing/checkpointing that handle deletes ``graph.lbug.tmp`` — taking our
        # freshly-built graph with it. Use a distinct base name like the server does
        # (see server/_curation.py run_rebuild → graph.rebuild.lbug).
        tmp_graph_path = staging.stage_path("rebuild")
        _ensure_live_graph_exists(
            vault_path,
            backend_name,
            graph_path,
            storage_config=_load_storage_config(vault_path),
        )

        previous_integrity = load_integrity_state(vault_path)
        previous_generation = previous_integrity.graph_generation
        from okto_neuron.semantic_fingerprint import materialized_semantic_fingerprints

        previous_fingerprints = materialized_semantic_fingerprints(
            vault_path,
            graph_generation=previous_generation,
        )
        previous_policy_sidefiles = _capture_semantic_policy_sidefiles(vault_path)
        ordered_source_files = _validated_rebuild_source_files(vault_path, source_files)
        semantic_seed_base = _effective_semantic_fingerprint_triplet(vault_path)
        source_manifest = _semantic_source_manifest(vault_path, ordered_source_files)

        staging.discard(tmp_graph_path)
        if backend_name != "neo4j":
            # Neo4j's staged "path" is a synthetic marker never written to
            # disk (Neo4jStaging docstring) -- nothing to fsync.
            _fsync_parent_dir(tmp_graph_path)

        started_at = _utc_now()
        _write_rebuild_state(
            state_path,
            {
                "phase": "in_progress",
                "files_done": [],
                "current_file": None,
                "started_at": started_at,
            },
        )

        previous_handlers = _install_rebuild_signal_handlers()
        try:
            try:
                semantic_seed = _apply_pending_semantic_policy(
                    vault_path,
                    expected_base=semantic_seed_base,
                    expected_source_manifest=source_manifest,
                )
                built = _build_fresh_graph(
                    vault_path,
                    tmp_graph_path,
                    state_path,
                    started_at,
                    ingest=ingest,
                    source_files=ordered_source_files,
                    interrupt_check=_cli_interrupt_check,
                    staging=staging,
                    open_staged_store=open_staged_store,
                )
                if semantic_seed is not None:
                    built["semantic_policy_seed"] = str(semantic_seed)
            except RebuildInterrupted:
                _restore_semantic_policy_sidefiles(
                    vault_path,
                    previous_policy_sidefiles,
                )
                _write_interrupted_state(state_path, started_at)
                raise
            except Exception:
                _restore_semantic_policy_sidefiles(
                    vault_path,
                    previous_policy_sidefiles,
                )
                if _rebuild_interrupted():
                    _write_interrupted_state(state_path, started_at)
                    raise _make_rebuild_interrupted(vault_path, step=4)
                raise
            _close_live_graph_handles(vault_path)
        finally:
            _restore_rebuild_signal_handlers(previous_handlers)
            _clear_rebuild_interrupted()

        try:
            checkpoint = _checkpoint_semantic_discovery(
                vault_path,
                built,
                base_fingerprints=semantic_seed_base,
                source_manifest=source_manifest,
            )
            if checkpoint is not None:
                built["semantic_discovery_checkpoint"] = str(checkpoint)
            _require_rebuild_candidate(
                vault_path, tmp_graph_path, state_path, built, backend_name=backend_name
            )
            graph_generation = str(built["graph_generation"])
            backup_graph_path, artifact_dir = _prepare_rebuild_backup(
                vault_path,
                graph_generation,
            )
            _write_previous_semantic_materialization(
                artifact_dir,
                graph_generation=previous_generation,
                fingerprints=previous_fingerprints,
            )
            if previous_generation and all(previous_fingerprints.values()):
                _write_semantic_policy_checkpoint(
                    artifact_dir,
                    graph_generation=previous_generation,
                    semantic_policy_fingerprint=str(previous_fingerprints["semantic_policy"]),
                    files=previous_policy_sidefiles,
                )
        except Exception:
            _restore_semantic_policy_sidefiles(
                vault_path,
                previous_policy_sidefiles,
            )
            raise
        # M2b (spec §2.3/§3): the swap tail (recheck-lock -> commit -> fence
        # generation -> post-swap audit -> publish) is the sequence hand-copied
        # across rebuild/heal/reembed/rollback; it now lives once in
        # curation.orchestrate.finish_staged_swap. Called directly here rather
        # than through orchestrate.rebuild()'s wrapper because that wrapper's
        # default staging.commit(..., backup_tag) always writes the backup to a
        # fixed sibling path (graph.lbug.<tag>) — kg_rebuild instead needs the
        # generation-keyed rebuild-artifacts/<generation>/previous-graph.lbug
        # backup that server/http.py's rollback endpoint depends on
        # (test_kg_rebuild_happy_path_swaps_and_finalizes_checkpoint asserts
        # this exact layout), so a custom ``commit`` closure performs the same
        # swap onto the path _prepare_rebuild_backup already chose.
        # M4 spec §2 ("one construction seam generalized"): the staging port
        # itself is still registry-driven even though this call site's own
        # ``commit=`` closure (not ``staging.commit``) does the actual swap —
        # a ladybug-pinned (or unpinned, pre-M4) vault gets byte-identical
        # ``LadybugStaging(vault_path)``. Both ``staging``/``open_staged_store``
        # were already resolved once, up front, alongside
        # ``graph_path``/``tmp_graph_path``.
        expected_identity = schema.GraphIdentity(
            graph_generation,
            str(built["identity_contract_version"]),
        )
        dim = int(built["embedding_dim"])
        captured_post_audit: dict[str, object] = {}
        # Populated by ``_commit_rebuild`` once the swap actually lands. A
        # non-Ladybug backend's real backup location (``StagingPort.commit``'s
        # own sibling-tag naming, e.g. ``graph.grafx.rebuild``) never equals
        # ``backup_graph_path`` (the Ladybug-only generation-keyed guess
        # ``_prepare_rebuild_backup`` computed above for the rollback
        # endpoint's ``rebuild-artifacts/<generation>/previous-graph.lbug``
        # layout) — every later report below reads THIS instead of the
        # precomputed path directly; ``backup_graph_path`` stays the fallback
        # default for the (never-reached in practice) case commit() raises
        # before it can record its own result.
        committed_backup: dict[str, Path] = {}

        def _audit_after_swap(
            audited_path: Path,
            *,
            dim: int,
            expected_identity: schema.GraphIdentity,
            stage: str,
        ) -> tuple[IntegrityAuditResult, dict[str, object]]:
            # finish_staged_swap labels this f"{stage_prefix}_after_swap_reopen"
            # ("rebuild_after_swap_reopen"); kg_rebuild's state.json/audit
            # contract has always used the bare "after_swap_reopen" label, so
            # this wrapper restores it before calling the real (possibly
            # test-monkeypatched) _audit_rebuild_graph_path.
            del stage
            if open_staged_store is None:
                result, payload = _audit_rebuild_graph_path(
                    audited_path,
                    dim=dim,
                    expected_identity=expected_identity,
                    stage="after_swap_reopen",
                )
            else:
                result, payload = _audit_reopened_staged_store(
                    vault_path,
                    audited_path,
                    dim=dim,
                    expected_identity=expected_identity,
                    stage="after_swap_reopen",
                    open_staged_store=open_staged_store,
                )
            captured_post_audit.clear()
            captured_post_audit.update(payload)
            return result, payload

        def _commit_rebuild() -> Path:
            try:
                if backend_name == "ladybug":
                    _swap_rebuilt_graph(vault_path, graph_path, tmp_graph_path, backup_graph_path)
                    result_path = backup_graph_path
                else:
                    result_path = staging.commit(tmp_graph_path, "rebuild")
            except Exception:
                _restore_semantic_policy_sidefiles(
                    vault_path,
                    previous_policy_sidefiles,
                )
                raise
            committed_backup["path"] = result_path
            return result_path

        try:
            orchestrate.finish_staged_swap(
                vault_path,
                staging,
                tmp_graph_path,
                lock=ownership,
                backup_tag="rebuild",
                expected_identity=expected_identity,
                dim=dim,
                stage_prefix="rebuild",
                audit_graph_path=_audit_after_swap,
                mark_generation_verifying=_mark_rebuild_generation_verifying,
                publish_integrity_result=_publish_integrity_result,
                commit=_commit_rebuild,
                # Live handles are already closed, above, once the build
                # completes; nothing reopens them before this point, so there
                # is nothing new to close here (today's code never did).
                close_live_handles=None,
                live_graph_path=graph_path,
            )
        except RebuildAuditFailed:
            post_audit = captured_post_audit
            _write_rebuild_state(artifact_dir / "post-swap-audit.json", post_audit)
            failed_payload = {
                "phase": "post_swap_validation_failed",
                "failed_at": _utc_now(),
                "backup_path": str(committed_backup.get("path", backup_graph_path)),
                "post_swap_audit": post_audit,
                **built,
            }
            _write_rebuild_state(state_path, failed_payload)
            raise

        post_audit = captured_post_audit
        _write_rebuild_state(artifact_dir / "post-swap-audit.json", post_audit)

        _publish_built_semantic_materialization(vault_path, built)
        _clear_pending_semantic_policy(vault_path)

        final_sha256 = _sha256_of_graph(graph_path, backend_name)
        _write_rebuild_state(
            state_path,
            {
                "phase": "complete",
                "completed_at": _utc_now(),
                "sha256": final_sha256,
                "backup_path": str(committed_backup.get("path", backup_graph_path)),
                "post_swap_audit": post_audit,
                **built,
            },
        )

    return 0


def _cli_interrupt_check(vault_path: Path, *, step: int) -> None:
    """Signal-based interrupt predicate for the CLI rebuild path. The in-process
    (daemon) path passes ``None`` — it never installs signal handlers (a non-main
    thread cannot, and the daemon owns process signals)."""
    _raise_if_rebuild_interrupted(vault_path, step=step)


def _audit_rebuild_store(
    store: LadybugStore,
    identity: schema.GraphIdentity,
    *,
    stage: str,
    file: str | None = None,
) -> tuple[IntegrityAuditResult, dict[str, object]]:
    result = audit_graph(
        store,
        graph_generation=identity.graph_generation,
        identity_contract_version=identity.identity_contract_version,
        max_issue_samples=_REBUILD_AUDIT_SAMPLE_LIMIT,
    )
    payload = _rebuild_audit_payload(result, stage=stage, file=file)
    return result, payload


def _audit_rebuild_graph_path(
    graph_path: Path,
    *,
    dim: int,
    expected_identity: schema.GraphIdentity,
    stage: str,
) -> tuple[IntegrityAuditResult, dict[str, object]]:
    """Reopen durable staging bytes and audit them through an isolated handle."""

    database: ladybug.Database | None = None
    connection: ladybug.Connection | None = None
    store: LadybugStore | None = None
    try:
        database = ladybug.Database(graph_path, read_only=True)
        connection = ladybug.Connection(database)
        actual_identity = schema.read_graph_identity(connection)
        connection.close()
        connection = None
        if actual_identity != expected_identity:
            return _identity_mismatch_audit(actual_identity, expected_identity, stage=stage)
        handle = VaultGraphHandle(
            vault_path=graph_path,
            schema_version=schema.CURRENT_SCHEMA_VERSION,
            embedding_dim=dim,
            database=database,
            graph_generation=actual_identity.graph_generation,
            identity_contract_version=actual_identity.identity_contract_version,
            graph_file_identity=_graph_file_identity(graph_path),
        )
        store = LadybugStore(graph_path, graph_handle=handle)
        database = None  # store now owns the handle
        return _audit_rebuild_store(store, actual_identity, stage=stage)
    finally:
        if connection is not None:
            connection.close()
        if store is not None:
            store.close()
        elif database is not None:
            database.close()


def _audit_reopened_staged_store(
    vault_path: Path,
    staged_path: Path,
    *,
    dim: int,
    expected_identity: schema.GraphIdentity,
    stage: str,
    open_staged_store: "orchestrate.OpenStagedStoreFn",
) -> tuple[IntegrityAuditResult, dict[str, object]]:
    """Non-Ladybug counterpart to :func:`_audit_rebuild_graph_path`.

    Reopens a just-closed staged graph through the SAME backend-specific
    opener that built it in the first place (M4 spec §2's ``open_staged_store``
    seam) — this adopts the already-committed identity rather than rebuilding
    schema (see ``GrafxStore.__init__``'s ``fresh`` branch), mirroring
    ``_audit_rebuild_graph_path``'s raw ``ladybug.Database(graph_path,
    read_only=True)`` reopen, the one piece of that function with no
    cross-backend equivalent. Once reopened, the audit itself
    (:func:`_audit_rebuild_store`) is the identical protocol-generic call
    both backends share.
    """
    store, actual_identity = open_staged_store(vault_path, staged_path, dim)
    try:
        if actual_identity != expected_identity:
            return _identity_mismatch_audit(actual_identity, expected_identity, stage=stage)
        return _audit_rebuild_store(store, actual_identity, stage=stage)
    finally:
        store.close()


def _graph_identity_at_path(graph_path: Path) -> schema.GraphIdentity:
    """Read one closed graph checkpoint's durable generation identity."""

    database = ladybug.Database(graph_path, read_only=True)
    connection = ladybug.Connection(database)
    try:
        return schema.read_graph_identity(connection)
    finally:
        connection.close()
        database.close()


def _semantic_rebuild_report(
    vault_path: Path,
    graph_path: Path,
    *,
    dim: int,
    expected_identity: schema.GraphIdentity,
    integrity_audit: dict[str, object],
    materialized_fingerprints: dict[str, str] | None = None,
    open_staged_store: "orchestrate.OpenStagedStoreFn | None" = None,
) -> dict[str, object]:
    """Evaluate registered semantic swap gates on isolated staging bytes.

    ``open_staged_store`` is the M4 spec §2 seam (default ``None`` keeps this
    function's Ladybug path byte-identical — a raw
    ``ladybug.Database(graph_path, read_only=True)`` reopen); a non-Ladybug
    backend reopens the just-closed staged graph through the SAME opener
    that built it instead (mirrors :func:`_audit_reopened_staged_store`).
    """

    from okto_neuron.predicates import PredicateRegistry
    from okto_neuron.semantic_quality import evaluate_store

    if open_staged_store is not None:
        store, actual_identity = open_staged_store(vault_path, graph_path, dim)
        try:
            if actual_identity != expected_identity:
                raise ValueError("semantic rebuild scan opened an unexpected graph generation")
            report_fingerprints = (
                _effective_semantic_fingerprint_triplet(vault_path)
                if materialized_fingerprints is None
                else dict(materialized_fingerprints)
            )
            return evaluate_store(
                store,
                integrity={
                    "status": "verified",
                    "graph_generation": actual_identity.graph_generation,
                    "fresh_for_semantic_scan": True,
                    "semantic_baseline_authoritative": True,
                    "last_audit": integrity_audit,
                },
                registered_predicates=PredicateRegistry(vault_path).labels(),
                config_fingerprint=report_fingerprints["config"],
                extraction_fingerprint=report_fingerprints["extraction"],
                semantic_policy_fingerprint=report_fingerprints["semantic_policy"],
            )
        finally:
            store.close()

    database: ladybug.Database | None = None
    connection: ladybug.Connection | None = None
    store = None
    try:
        database = ladybug.Database(graph_path, read_only=True)
        connection = ladybug.Connection(database)
        actual_identity = schema.read_graph_identity(connection)
        connection.close()
        connection = None
        if actual_identity != expected_identity:
            raise ValueError("semantic rebuild scan opened an unexpected graph generation")
        handle = VaultGraphHandle(
            vault_path=graph_path,
            schema_version=schema.CURRENT_SCHEMA_VERSION,
            embedding_dim=dim,
            database=database,
            graph_generation=actual_identity.graph_generation,
            identity_contract_version=actual_identity.identity_contract_version,
            graph_file_identity=_graph_file_identity(graph_path),
        )
        store = LadybugStore(graph_path, graph_handle=handle)
        database = None
        if materialized_fingerprints is None:
            report_fingerprints = _effective_semantic_fingerprint_triplet(vault_path)
        else:
            report_fingerprints = dict(materialized_fingerprints)
        return evaluate_store(
            store,
            integrity={
                "status": "verified",
                "graph_generation": actual_identity.graph_generation,
                "fresh_for_semantic_scan": True,
                "semantic_baseline_authoritative": True,
                "last_audit": integrity_audit,
            },
            registered_predicates=PredicateRegistry(vault_path).labels(),
            config_fingerprint=report_fingerprints["config"],
            extraction_fingerprint=report_fingerprints["extraction"],
            semantic_policy_fingerprint=report_fingerprints["semantic_policy"],
        )
    finally:
        if connection is not None:
            connection.close()
        if store is not None:
            store.close()
        elif database is not None:
            database.close()


def _effective_semantic_fingerprint_triplet(vault_path: Path) -> dict[str, str]:
    """Return the effective decision triplet used by one rebuild source.

    Rebuilds may discover durable predicate or identity decisions while walking
    sources.  Capturing the same triplet before and after every source lets the
    rebuild prove that one complete pass used a single policy instead of
    labelling a mixed-policy graph with whichever fingerprint happened to exist
    at the end.
    """

    from okto_neuron.semantic_fingerprint import (
        _effective_semantic_fingerprint_triplet as _shared_effective_triplet,
    )

    return _shared_effective_triplet(vault_path)


def _identity_mismatch_audit(
    actual: schema.GraphIdentity,
    expected: schema.GraphIdentity,
    *,
    stage: str,
) -> tuple[IntegrityAuditResult, dict[str, object]]:
    reason = (
        "rebuilt graph identity changed across close/reopen: "
        f"expected={expected}, observed={actual}"
    )
    result = IntegrityAuditResult(
        status=AuditStatus.FAILED,
        nodes_scanned=0,
        edges_scanned=0,
        adjacency_scanned=0,
        expected_artifacts_checked=0,
        issue_count=1,
        issues=(
            IntegrityIssue(
                code="graph_identity_mismatch",
                artifact_kind="graph",
                artifact_id=actual.graph_generation or "legacy",
                expected=str(expected),
                observed=str(actual),
            ),
        ),
        nodes_complete=False,
        edges_complete=False,
        adjacency_complete=False,
        manifest_complete=True,
        duration_ms=0.0,
        incomplete_reasons=(reason,),
        graph_generation=actual.graph_generation,
        identity_contract_version=actual.identity_contract_version,
    )
    return result, _rebuild_audit_payload(result, stage=stage, file=None)


def _rebuild_audit_payload(
    result: IntegrityAuditResult,
    *,
    stage: str,
    file: str | None,
) -> dict[str, object]:
    return {
        "audit_id": uuid4().hex,
        "stage": stage,
        "file": file,
        "status": result.status.value,
        "nodes_scanned": result.nodes_scanned,
        "edges_scanned": result.edges_scanned,
        "adjacency_scanned": result.adjacency_scanned,
        "expected_artifacts_checked": result.expected_artifacts_checked,
        "issue_count": result.issue_count,
        "issues": [asdict(issue) for issue in result.issues],
        "nodes_complete": result.nodes_complete,
        "edges_complete": result.edges_complete,
        "adjacency_complete": result.adjacency_complete,
        "manifest_complete": result.manifest_complete,
        "duration_ms": result.duration_ms,
        "incomplete_reasons": list(result.incomplete_reasons),
        "graph_generation": result.graph_generation,
        "identity_contract_version": result.identity_contract_version,
    }


def _build_fresh_graph(
    vault_path: Path,
    tmp_graph_path: Path,
    state_path: Path,
    started_at: str,
    *,
    ingest: Callable[[Path, "LadybugStoreType"], object | None] | None = None,
    interrupt_check: Callable[..., None] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    source_files: Sequence[Path] | None = None,
    staging: "StagingPort | None" = None,
    open_staged_store: "orchestrate.OpenStagedStoreFn | None" = None,
) -> dict[str, object]:
    """Build until one complete source pass uses one immutable policy triplet.

    Predicate and identity decisions are legitimate outputs of the semantic
    pipeline.  If a pass discovers one, that pass is a discovery pass: its graph
    was built under mixed policy and must never be swapped or receive a
    generation receipt.  The next pass reuses compatible extraction evidence but
    rematerializes the graph from empty under the newly settled policy.  A bounded
    loop fails closed if decisions do not converge.

    ``staging``/``open_staged_store`` are the M4 spec §2 seam (both default
    ``None``, preserving today's Ladybug-only path byte-for-byte — including
    every existing test/daemon call site that supplies neither): per-pass
    discard uses ``staging.discard`` when a staging port is given (a
    non-Ladybug graph is a directory, which the Ladybug-only
    ``_discard_graph_family`` cannot remove), and ``open_staged_store`` is
    threaded straight through to :func:`_build_fresh_graph_pass`.
    """

    ordered_source_files = _validated_rebuild_source_files(vault_path, source_files)
    fingerprint_passes: list[dict[str, object]] = []
    for pass_index in range(1, _MAX_SEMANTIC_STABILIZATION_PASSES + 1):
        if staging is not None:
            staging.discard(tmp_graph_path)
        else:
            _discard_graph_family(tmp_graph_path)
        result = _build_fresh_graph_pass(
            vault_path,
            tmp_graph_path,
            state_path,
            started_at,
            ingest=ingest,
            interrupt_check=interrupt_check,
            progress=progress,
            pass_index=pass_index,
            source_files=ordered_source_files,
            open_staged_store=open_staged_store,
        )
        raw_pass = result.get("fingerprint_pass")
        if not isinstance(raw_pass, dict):
            raise RuntimeError("rebuild pass did not report semantic fingerprint evidence")
        fingerprint_passes.append(dict(raw_pass))
        result["fingerprint_passes"] = list(fingerprint_passes)

        state_payload = json.loads(state_path.read_text(encoding="utf-8"))
        state_payload["fingerprint_passes"] = list(fingerprint_passes)
        state_payload["semantic_stabilization_pass"] = pass_index

        stable = raw_pass.get("stable") is True
        retryable = (
            not stable
            and not result.get("failed_files")
            and result.get("failing_file") is None
            and isinstance(result.get("final_audit"), dict)
            and result["final_audit"].get("status") == "verified"  # type: ignore[index]
        )
        if stable or not retryable:
            _write_rebuild_state(state_path, state_payload)
            return result

        if pass_index < _MAX_SEMANTIC_STABILIZATION_PASSES:
            state_payload["phase"] = "stabilizing_semantic_policy"
            state_payload["current_file"] = None
            _write_rebuild_state(state_path, state_payload)
            if progress is not None:
                progress(
                    f"semantic policy changed; rebuilding pass {pass_index + 1}",
                    0,
                    len(ordered_source_files),
                )
            continue

        state_payload["phase"] = "semantic_policy_unstable"
        _write_rebuild_state(state_path, state_payload)
        return result

    raise AssertionError("semantic stabilization loop exhausted without a result")


def _build_fresh_graph_pass(
    vault_path: Path,
    tmp_graph_path: Path,
    state_path: Path,
    started_at: str,
    *,
    ingest: Callable[[Path, "LadybugStoreType"], object | None] | None = None,
    interrupt_check: Callable[..., None] | None = None,
    progress: Callable[[str, int, int], None] | None = None,
    pass_index: int = 1,
    source_files: Sequence[Path] | None = None,
    open_staged_store: "orchestrate.OpenStagedStoreFn | None" = None,
) -> dict[str, object]:
    """Build a FRESH graph at ``tmp_graph_path`` from the markdown trust root.

    The handle-/signal-/swap-free build CORE shared by CLI ``kg rebuild`` and the
    ADR 0009 P3 in-process rebuild curation runner. It ONLY: bootstraps the tmp
    graph, ingests every deterministic source file through the bound per-file
    ingest, health-checks, and closes the tmp store. It does NOT touch the live
    handle, install signal handlers, or swap — those are the CALLER's policy (the
    CLI closes the live handle at start + installs signals; the in-process runner
    keeps the live handle serving and marshals the swap onto the event loop).

    ``ingest`` is the injectable per-file ``(path, store)`` seam (model-free tests).
    ``source_files`` is a fail-closed acceptance seam: when supplied it must be an
    exact permutation of the canonical source set, so source-order tests can vary
    only order while production rebuilds keep their deterministic order.
    ``interrupt_check(vault_path, step=...)`` is an optional injected predicate (CLI
    passes the signal-based one; in-process passes ``None``). ``progress(stage, done,
    total)`` is an optional per-file UI callback. Every completed source is followed
    by a whole-staging-graph audit; a final audit reopens the durable bytes after a
    clean close. The returned ``swap_allowed`` is true only when every audit verified
    and no provider-failed file exists.

    ``open_staged_store`` is the M4 spec §2 seam (default ``None`` keeps this
    function's Ladybug path byte-for-byte identical — every existing
    test/daemon call site supplies neither): the staged store is built via
    ``_bootstrap_graph_at_path`` + ``LadybugStore`` as today, and the final
    close+reopen durability check (health + :func:`_audit_rebuild_graph_path`)
    stays a raw Ladybug reopen. A non-Ladybug backend instead builds the
    staged store through the given opener (e.g. a fresh ``GrafxStore`` at
    ``tmp_graph_path``) and reopens through that SAME opener for the
    close+reopen check (:func:`_audit_reopened_staged_store`) and the
    semantic report (:func:`_semantic_rebuild_report`'s own seam) — the
    per-file ingest loop, ledger recording, and per-file audit below are
    unchanged either way (already protocol-generic: they only ever call
    ``store.add_node``/``add_edge`` through the companion pipeline and
    :func:`_audit_rebuild_store`).
    """

    def _check(step: int) -> None:
        if interrupt_check is not None:
            interrupt_check(vault_path, step=step)

    files_done: list[str] = []
    failed_files: list[dict[str, str]] = []
    audits: list[dict[str, object]] = []
    failing_file: str | None = None
    store: LadybugStore | None = None
    tmp_handle: VaultGraphHandle | None = None
    identity = schema.GraphIdentity(None, None)
    dim = _resolve_configured_dim(vault_path)
    current_file: str | None = None
    staging_started = False
    pass_start_fingerprints = _effective_semantic_fingerprint_triplet(vault_path)
    fingerprint_observations: list[dict[str, object]] = []
    stopped_early_for_policy_drift = False
    try:
        _check(3)
        if open_staged_store is None:
            tmp_handle = _bootstrap_graph_at_path(vault_path, tmp_graph_path)
            identity = schema.GraphIdentity(
                tmp_handle.graph_generation,
                tmp_handle.identity_contract_version,
            )
            staging_started = True
            dim = tmp_handle.embedding_dim or dim
            _check(3)
            # CRITICAL (ADR 0009 P3): key the tmp store on ``tmp_graph_path``, NOT
            # ``vault_path``. ``VaultConnection`` caches exactly one handle per resolved
            # path and ``_get_handle`` returns the cached LIVE handle (ignoring the one
            # passed) when that path is already open. The CLI rebuild closed the live
            # handle at start, so it never collided; the in-process daemon keeps the live
            # handle serving, so re-using ``vault_path`` here would silently bind the
            # build to the LIVE graph (writing into it, leaving tmp empty). Keying on the
            # tmp file (a distinct path, same directory) gives the build its OWN handle —
            # ADR-0007-safe (one handle per db FILE; live=graph.lbug, build=graph.rebuild.lbug)
            # — while ``os.replace`` stays same-filesystem (the tmp sits next to live).
            store = LadybugStore(tmp_graph_path, graph_handle=tmp_handle)
        else:
            # M4 spec §2 ("one construction seam generalized"): the same
            # opener the swap tail later reuses to reopen this exact staged
            # path (`_audit_reopened_staged_store`) builds it here first.
            # ``tmp_handle`` stays ``None`` — there is no Ladybug-style raw
            # handle for a non-Ladybug backend to fall back to on failure
            # (`_close_tmp_rebuild_handles` already degrades gracefully).
            store, identity = open_staged_store(vault_path, tmp_graph_path, dim)
            staging_started = True
        _check(3)

        # Bind the per-file ingest. Tests inject a deterministic, model-free
        # callable; production (ingest is None) drives the FULL companion pipeline
        # against this fresh temp store. The companion is built ONCE here (after the
        # store exists) so providers resolve a single time; this build core — not the
        # companion — still owns and closes ``store``.
        ingest_file = ingest or _make_full_pipeline_ingest(vault_path, store)

        # Defect C: a per-file total LLM failure must not abort the WHOLE
        # rebuild (see the ``except LLMUnavailableError`` below) — lazy import
        # mirrors ``_make_full_pipeline_ingest``'s own lazy ``Companion`` import
        # just above, keeping this CLI module's import-time cost unchanged for
        # callers that never touch the companion pipeline.
        from okto_neuron.companion import LLMUnavailableError, RememberResult
        from okto_neuron.consolidate.ledger import CandidateLedger

        ledger = CandidateLedger(vault_path / _MARGINALIA_DIR)

        ordered_source_files = _validated_rebuild_source_files(vault_path, source_files)
        total = len(ordered_source_files)
        for index, file_path in enumerate(ordered_source_files):
            _check(4)
            relative_file = _relative_posix(vault_path, file_path)
            current_file = relative_file
            _write_rebuild_state(
                state_path,
                {
                    "phase": "in_progress",
                    "files_done": files_done,
                    "failed_files": failed_files,
                    "current_file": relative_file,
                    "started_at": started_at,
                    "graph_generation": identity.graph_generation,
                    "identity_contract_version": identity.identity_contract_version,
                    "audits": audits,
                },
            )
            if progress is not None:
                progress(relative_file, index, total)
            _check(4)
            provider_failed = False
            ledger_run_id: str | None = None
            document_id: str | None = None
            try:
                ingest_result = ingest_file(file_path, store)
                if isinstance(ingest_result, RememberResult):
                    ledger_run_id = ingest_result.ledger_run_id
                    document_id = ingest_result.document_id
            except RebuildInterrupted:
                raise
            except LLMUnavailableError as exc:
                # Defect C fix: every attempted block in ONE file failing (a
                # transient timeout is enough for a single-block file) must not
                # blast-radius the WHOLE rebuild — the bare ``except Exception``
                # below re-raises out of this function, and the CALLER's
                # ``except Exception`` discards the entire tmp graph being
                # built. Record the file as failed (still keeps its structural,
                # LLM-free claims already committed by ``remember()`` before it
                # raised) and continue rebuilding the rest of the vault.
                _LOG.warning(
                    "LLM unavailable ingesting %s during rebuild of vault %s — "
                    "keeping structural-only content for this file, rebuild "
                    "continues: %s",
                    file_path,
                    vault_path,
                    exc,
                )
                failed_files.append({"file": relative_file, "error": str(exc)})
                provider_failed = True
                ledger_run_id = exc.ledger_run_id
                document_id = exc.document_id
            except Exception as exc:
                # Capture the REAL cause (traceback + chain) at the throw site.
                # IngestError already wraps it via ``from exc``; this log ensures
                # the underlying failure is diagnosable even when a caller only
                # surfaces the terse IngestError message.
                _LOG.exception(
                    "ingest of %s failed during rebuild of vault %s",
                    file_path,
                    vault_path,
                )
                raise IngestError(
                    file_path,
                    vault_path=vault_path,
                    message=_rebuild_error_summary(exc, vault_path=vault_path),
                    cause=exc,
                ) from exc
            _check(4)
            after_source_fingerprints = _effective_semantic_fingerprint_triplet(vault_path)
            fingerprint_observations.append(
                {
                    "file": relative_file,
                    "after": after_source_fingerprints,
                    "matches_pass_start": (after_source_fingerprints == pass_start_fingerprints),
                }
            )
            if not provider_failed:
                files_done = [*files_done, relative_file]
            audit_result, audit_payload = _audit_rebuild_store(
                store,
                identity,
                stage="after_source",
                file=relative_file,
            )
            audits.append(audit_payload)
            if ledger_run_id and document_id:
                integrity = {
                    "status": audit_payload["status"],
                    "audit_id": audit_payload["audit_id"],
                    "graph_generation": audit_payload["graph_generation"],
                }
                ledger.record_integrity_outcome(
                    ledger_run_id,
                    document_id=document_id,
                    integrity=integrity,
                    quality=(None if audit_result.verified else "integrity_failed"),
                )
            _write_rebuild_state(
                state_path,
                {
                    "phase": "in_progress",
                    "files_done": files_done,
                    "failed_files": failed_files,
                    "current_file": relative_file,
                    "started_at": started_at,
                    "graph_generation": identity.graph_generation,
                    "identity_contract_version": identity.identity_contract_version,
                    "audits": audits,
                },
            )
            _check(4)
            if progress is not None:
                progress(relative_file, index + 1, total)
            if not audit_result.verified:
                failing_file = relative_file
                break
            if (
                pass_index == _MAX_SEMANTIC_STABILIZATION_PASSES
                and after_source_fingerprints != pass_start_fingerprints
            ):
                # The final bounded pass can no longer satisfy its pass invariant.
                # Audit and retain the completed prefix, then stop before spending
                # model calls on sources that cannot make this staging swappable.
                stopped_early_for_policy_drift = True
                break

        _check(5)
        store.close()
        store = None
        if open_staged_store is None:
            verify_ladybug_db_health(tmp_graph_path)
            final_result, final_payload = _audit_rebuild_graph_path(
                tmp_graph_path,
                dim=dim,
                expected_identity=identity,
                stage="after_close_reopen",
            )
        else:
            final_result, final_payload = _audit_reopened_staged_store(
                vault_path,
                tmp_graph_path,
                dim=dim,
                expected_identity=identity,
                stage="after_close_reopen",
                open_staged_store=open_staged_store,
            )
        audits.append(final_payload)
        pass_end_fingerprints = _effective_semantic_fingerprint_triplet(vault_path)
        fingerprints_stable = pass_end_fingerprints == pass_start_fingerprints and all(
            observation.get("matches_pass_start") is True
            for observation in fingerprint_observations
        )
        fingerprint_pass: dict[str, object] = {
            "schema_version": "semantic_rebuild_fingerprint_pass.v1",
            "pass": pass_index,
            "start": pass_start_fingerprints,
            "end": pass_end_fingerprints,
            "stable": fingerprints_stable,
            "stopped_early_for_policy_drift": stopped_early_for_policy_drift,
            "files_materialized": len(files_done),
            "files_total": total,
            "changed_after_files": [
                str(observation["file"])
                for observation in fingerprint_observations
                if observation.get("matches_pass_start") is not True
            ],
            "observations": fingerprint_observations,
        }
        semantic_report: dict[str, object] | None = None
        semantic_gate: dict[str, object] = {
            "schema_version": "semantic_rebuild_gate.v1",
            "status": "not_run",
            "swap_allowed": False,
            "reason": "technical integrity did not verify",
        }
        if final_result.verified and fingerprints_stable:
            semantic_report = _semantic_rebuild_report(
                vault_path,
                tmp_graph_path,
                dim=dim,
                expected_identity=identity,
                integrity_audit=final_payload,
                materialized_fingerprints=pass_start_fingerprints,
                open_staged_store=open_staged_store,
            )
            raw_gate = semantic_report.get("rebuild_gate")
            if isinstance(raw_gate, dict):
                semantic_gate = dict(raw_gate)
        elif final_result.verified:
            semantic_gate = {
                "schema_version": "semantic_rebuild_gate.v1",
                "status": "failed",
                "swap_allowed": False,
                "reason": "semantic fingerprints changed during rebuild pass",
                "fingerprint_pass": fingerprint_pass,
            }
        _check(5)
    except Exception as exc:
        _close_tmp_rebuild_handles(store, tmp_handle)
        if staging_started and tmp_graph_path.exists():
            _preserve_failed_staging(
                vault_path,
                tmp_graph_path,
                {
                    "files_done": files_done,
                    "count": len(files_done),
                    "failed_files": failed_files,
                    "audits": audits,
                    "final_audit": None,
                    "graph_generation": identity.graph_generation,
                    "identity_contract_version": identity.identity_contract_version,
                    "embedding_dim": dim,
                    "failing_file": current_file,
                    "swap_allowed": False,
                    "build_error": f"{type(exc).__name__}: {exc}",
                },
                state_path,
            )
        elif open_staged_store is None:
            _discard_graph_family(tmp_graph_path)
        else:
            # A non-Ladybug staged graph is a directory (e.g. Grafx's
            # ``graph.<tag>.grafx``) — ``_discard_graph_family`` is
            # Ladybug-only (it ``unlink()``s a file); this reaches that
            # branch only when the exception landed before/during store
            # construction (``staging_started`` never flipped True), so
            # there is nothing more than a bare directory removal to do.
            shutil.rmtree(tmp_graph_path, ignore_errors=True)
        raise
    if failed_files:
        _LOG.warning(
            "rebuild of vault %s completed with %d file(s) skipped for LLM "
            "unavailability (structural-only content kept): %s",
            vault_path,
            len(failed_files),
            [f["file"] for f in failed_files],
        )
    swap_allowed = (
        failing_file is None
        and not failed_files
        and final_result.verified
        and fingerprints_stable
        and semantic_gate.get("swap_allowed") is True
    )
    result: dict[str, object] = {
        "files_done": files_done,
        "count": len(files_done),
        "failed_files": failed_files,
        "audits": audits,
        "final_audit": final_payload,
        "semantic_gate": semantic_gate,
        "semantic_quality": semantic_report,
        "graph_generation": identity.graph_generation,
        "identity_contract_version": identity.identity_contract_version,
        "embedding_dim": dim,
        "failing_file": failing_file,
        "fingerprint_pass": fingerprint_pass,
        "materialized_fingerprints": (pass_start_fingerprints if fingerprints_stable else None),
        "swap_allowed": swap_allowed,
    }
    _write_rebuild_state(
        state_path,
        {
            "phase": "staged" if swap_allowed else "validation_failed",
            "files_done": files_done,
            "failed_files": failed_files,
            "current_file": failing_file,
            "started_at": started_at,
            "graph_generation": identity.graph_generation,
            "identity_contract_version": identity.identity_contract_version,
            "embedding_dim": dim,
            "audits": audits,
            "final_audit": final_payload,
            "semantic_gate": semantic_gate,
            "semantic_quality": semantic_report,
            "fingerprint_pass": fingerprint_pass,
            "materialized_fingerprints": (pass_start_fingerprints if fingerprints_stable else None),
        },
    )
    return result


def kg_reembed(vault: Path | None = None) -> int:
    """Run standalone re-embedding under cross-process graph ownership."""
    vault_path = _resolve_rebuild_vault(vault)
    _ensure_vault_directory(vault_path)
    with acquire_rebuild_lock(vault_path, operation="reembed") as ownership:
        return _kg_reembed_owned(vault_path, ownership)


def kg_reindex(vault: Path | None = None, *, force: bool = False) -> int:
    """Rebuild the search index from the graph, or confirm it is already current.

    Reads each node's stored text and embedding straight off the graph — no LLM
    calls and no re-embedding. Without ``force`` this is a no-op when the
    index's generation stamp already matches the graph's current content
    (the same staleness check ``_open_index`` runs on every vault open).
    """
    from okto_neuron.store.index import DefaultIndexStore, compute_graph_generation, reindex_all

    vault_path = _resolve_rebuild_vault(vault)
    _ensure_vault_directory(vault_path)
    with acquire_rebuild_lock(vault_path, operation="reindex"):
        _close_live_graph_handles(vault_path)
        # M4 spec §2 item 3 (registry-driven open): a plain
        # ``LadybugStore(vault_path)`` degrades to exactly
        # ``store/vault.py``'s own ``_open_graph_store`` ladybug branch
        # (``bootstrap_vault_graph`` + ``LadybugStore(vault_path,
        # graph_handle=...)``) — reusing it directly keeps Ladybug
        # byte-identical while making a ``grafx``-pinned vault open through
        # its own registered class instead of a hardcoded Ladybug literal.
        from okto_neuron.store.vault import _open_graph_store, _read_pinned_backends

        graph_backend, _index_backend, storage_config = _read_pinned_backends(vault_path)
        store = _open_graph_store(vault_path, graph_backend, storage_config)
        try:
            index = DefaultIndexStore(vault_path)
            expected = compute_graph_generation(store)
            if not force and index.generation() == expected:
                click.echo(f"index up to date (generation {expected[:12]})")
                return 0

            started = time.perf_counter()
            reindex_all(store, index)
            elapsed = time.perf_counter() - started
            stats = index.stats()
            new_stamp = index.generation()[:12]
            click.echo(
                f"doc_count={stats.doc_count} embedded_count={stats.embedded_count} "
                f"elapsed={elapsed:.2f}s generation={new_stamp}"
            )
            return 0
        except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a store error
            click.echo(f"error: {exc}", err=True)
            return 1
        finally:
            store.close()
            VaultConnection.close_vault(vault_path)


def _kg_reembed_owned(vault_path: Path, ownership: RebuildLockHandle) -> int:
    """Recompute every vector in a vault graph at the configured embedding width.

    Vectors-only: the existing graph's stored text is replayed through the
    configured embedder — NO LLM re-extraction (that is ``kg rebuild``). Because
    the Ladybug vector column is fixed-width, a model/width change cannot be applied
    in place, so this reads the live graph, bootstraps a fresh graph at the new
    width, copies nodes (recomputing vectors) then edges, health-checks, and
    atomic-swaps — reusing the same lock/state/swap machinery as ``kg rebuild`` but
    sourcing from the live graph instead of markdown.

    The live read uses a RAW handle (no bootstrap lock re-entry, no dim-guard), so
    an intentional re-width never trips the guard that protects normal opens.
    """
    from okto_neuron.config import VaultConfig
    from okto_neuron.embed import get_provider

    marginalia_dir = vault_path / _MARGINALIA_DIR
    marginalia_dir.mkdir(parents=True, exist_ok=True)
    lock_path = marginalia_dir / _BOOTSTRAP_LOCK
    state_path = marginalia_dir / _REEMBED_STATE

    try:
        cfg = VaultConfig.load(vault_path).embedding
    except Exception:  # noqa: BLE001 — missing/partial config → defaults
        from okto_neuron.config import EmbeddingConfig

        cfg = EmbeddingConfig()
    embedder = get_provider(cfg)
    new_dim = int(cfg.dimension)

    with _bootstrap_lock(vault_path, lock_path):
        _close_live_graph_handles(vault_path)
        # M4 spec §2 ("one construction seam generalized"): resolved once,
        # right after lock acquisition (NOT before — mirrors
        # ``_kg_rebuild_owned``'s own placement: a lock-contention caller
        # must see ``VaultLockHeld`` before any config read) and
        # defensively, matching this function's own pre-existing
        # "missing/partial config -> defaults" tolerance just above —
        # ``kg reembed``, unlike ``kg rebuild``, has always supported
        # running against a bare/never-initialized vault (no
        # ``okto-neuron.yaml`` at all), so an absent/unreadable config falls
        # back to ``"ladybug"``, the same default ``_resolve_pinned_backend``
        # documents for a vault with no ``storage`` block. ``tmp_graph_path``
        # via ``staging.stage_path("reembed")`` matches today's own literal
        # computation byte-for-byte for Ladybug (``LadybugStaging.
        # stage_path`` is a pure computation, no side effect).
        try:
            backend_name = _resolve_pinned_backend(vault_path)
        except Exception:  # noqa: BLE001 — missing/partial config → default backend
            backend_name = "ladybug"
        staging, open_staged_store = _swap_construction_for(vault_path, backend_name)
        graph_path = _live_graph_path(vault_path, backend_name)
        # NOT ``graph.lbug.tmp`` — see kg_rebuild above; ladybug owns ``<db>.tmp`` as
        # scratch for any live handle, so use a distinct base name (server reembed uses
        # graph.reembed.lbug).
        tmp_graph_path = staging.stage_path("reembed")
        started_at = _utc_now()
        _write_rebuild_state(
            state_path,
            {"phase": "reading", "nodes_done": 0, "nodes_total": 0, "started_at": started_at},
        )

        # A never-bootstrapped vault has no graph to read OR to swap onto; create a
        # fresh empty one at the configured width so the swap below has a target
        # (mirrors kg_rebuild's live-bootstrap-if-missing). The extra close only
        # runs when a bootstrap actually happened — byte-identical to the
        # pre-M4 Ladybug-only ``if not graph_path.exists(): ...; _close_live_
        # graph_handles(vault_path)`` shape, now backend-neutral.
        needed_bootstrap = not graph_path.exists()
        _ensure_live_graph_exists(
            vault_path,
            backend_name,
            graph_path,
            dim=new_dim,
            storage_config=_load_storage_config(vault_path),
        )
        if needed_bootstrap:
            _close_live_graph_handles(vault_path)

        # Read the live graph (if any) into memory. Ladybug uses a RAW handle
        # that bypasses the bootstrap lock (we already hold it) AND the
        # dim-guard (this is the intentional re-width). Grafx and Neo4j have
        # no bootstrap-lock-reentry concept (only Ladybug's construction
        # takes that lock), but DO have their own dim-guard, so
        # ``_open_live_store`` opens them via their own raw
        # ``open_for_live_read`` counterpart instead of the guarded
        # ``from_vault`` — see ``_open_live_store``'s docstring. An absent
        # graph means nothing to reembed — a fresh empty graph at the new
        # width is bootstrapped by the swap below.
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
                nodes = list(live_store.list_nodes(include_embedding=True))
                edges = list(live_store.list_edges())
            finally:
                live_store.close()
            _close_live_graph_handles(vault_path)

        if backend_name == "ladybug":
            _remove_if_exists(tmp_graph_path)
        else:
            # Grafx's own ``stage_path`` already vacated any stale staged
            # directory as a side effect (unlike Ladybug's pure-computation
            # ``stage_path``); ``discard`` here is a cheap, safe no-op that
            # keeps this line backend-symmetric rather than a silent skip.
            staging.discard(tmp_graph_path)
        if backend_name != "neo4j":
            _fsync_parent_dir(tmp_graph_path)
        _write_rebuild_state(
            state_path,
            {
                "phase": "embedding",
                "nodes_done": 0,
                "nodes_total": len(nodes),
                "started_at": started_at,
            },
        )

        def _progress(done: int, total: int) -> None:
            if done % _REEMBED_PROGRESS_EVERY != 0 and done != total:
                return
            _write_rebuild_state(
                state_path,
                {
                    "phase": "embedding",
                    "nodes_done": done,
                    "nodes_total": total,
                    "started_at": started_at,
                },
            )

        def _embedding_settings() -> tuple[int, int]:
            latest = VaultConfig.load(vault_path).embedding
            return latest.batch_size, latest.max_concurrent_batches

        # M2b (spec §2.3/§3): builds the staged graph, copies nodes/edges
        # through the reembedding path, health-checks the closed staged graph
        # via store.health() (retiring this site's by-name
        # verify_ladybug_db_health import, spec §2.4), then runs the shared
        # swap tail with publish_integrity=False — reembed's documented
        # asymmetry preserved exactly (vectors-only; no post-swap audit, no
        # generation fence/publish). orchestrate.reembed() discards the
        # staged graph and re-raises on any build/health failure; kg_reembed
        # still owns translating that into today's "failed" state.json (no
        # CLI state file is orchestrate.py's concern) — but NOT for a failure
        # at or after the lock recheck/commit, matching today's exact
        # boundary (VaultLockHeld/RebuildSwapFailed there write no state).
        # M4 spec §2 ("one construction seam generalized"): ``staging``/
        # ``open_staged_store`` were already resolved once, up front,
        # alongside ``backend_name``/``graph_path``/``tmp_graph_path`` — a
        # ladybug-pinned (or unpinned, pre-M4) vault gets byte-identical
        # behaviour (opener ``None`` lets orchestrate.reembed fall back to
        # its own default).
        try:
            _result, stats = orchestrate.reembed(
                vault_path,
                ownership,
                live_nodes=nodes,
                live_edges=edges,
                embedder=embedder,
                dim=new_dim,
                progress=_progress,
                staging=staging,
                bootstrap_graph_at_path=_bootstrap_graph_at_path,
                open_staged_store=open_staged_store,
                close_live_handles=_close_live_graph_handles,
                batch_size=cfg.batch_size,
                max_concurrent_batches=cfg.max_concurrent_batches,
                embedding_settings=_embedding_settings,
                live_graph_path=graph_path,
            )
        except (VaultLockHeld, RebuildSwapFailed):
            raise
        except Exception:
            _write_rebuild_state(
                state_path,
                {"phase": "failed", "started_at": started_at, "failed_at": _utc_now()},
            )
            raise

        final_sha256 = _sha256_of_graph(graph_path, backend_name)
        _write_rebuild_state(
            state_path,
            {
                "phase": "complete",
                "completed_at": _utc_now(),
                "sha256": final_sha256,
                "embedding_dim": new_dim,
                **stats,
            },
        )

    return 0


def _open_live_handle(vault_path: Path, graph_path: Path) -> VaultGraphHandle:
    """Open an EXISTING graph for reading, bypassing the dim-guard.

    Unlike :func:`_bootstrap_graph_at_path` (which builds a fresh graph) and the
    bootstrap path in ``_bootstrap.py`` (which runs DDL + the dim-guard), this just
    opens the on-disk graph so its nodes/edges can be read at whatever width they
    were written. Used only by ``kg reembed``, whose whole purpose is to read an
    old-width graph and re-embed it at the new width.
    """
    database = ladybug.Database(graph_path)
    return VaultGraphHandle(
        vault_path=vault_path,
        schema_version=schema.CURRENT_SCHEMA_VERSION,
        # This raw handle intentionally bypasses width validation so re-embed can
        # read an old-width graph. The width is not consumed on this path.
        embedding_dim=None,
        database=database,
        graph_file_identity=_graph_file_identity(graph_path),
    )


def _resolve_rebuild_vault(vault: Path | None) -> Path:
    return resolve_vault_reference(vault)


def _ensure_vault_directory(vault_path: Path) -> None:
    if vault_path.exists() and not vault_path.is_dir():
        raise VaultPathNotADirectory(vault_path)
    vault_path.mkdir(parents=True, exist_ok=True)


def _make_full_pipeline_ingest(
    vault_path: Path,
    store: "LadybugStoreType",
) -> Callable[[Path, "LadybugStoreType"], object]:
    """Build the production per-file rebuild ingest: the FULL companion pipeline
    bound to the fresh temp ``store``.

    The wrapping :class:`~okto_neuron.vault.Vault` points at the REAL vault path (so
    ``okto-neuron.yaml`` → the configured LLM/embedder and source-path validation
    resolve correctly) but is backed by the temp graph ``store``. Every read/write
    ``remember`` does flows through that one store, so single-writer holds and
    nothing else opens the temp graph.

    Critical: the returned closure NEVER closes the Vault/Companion/store — the
    rebuild owns the temp store and closes it itself before the health-check and
    swap. Closing here would pull the handle out from under the swap.
    ``allow_external_sources=True`` because the canonical sources live under
    ``.marginalia/sources/`` (inside the vault root, but the validator's default
    rejects paths it can't relativize cleanly for non-notes layouts)."""
    from okto_neuron.companion import Companion
    from okto_neuron.consolidate.ledger import FRESH_REBUILD_MATERIALIZATION_SCOPE
    from okto_neuron.vault import Vault

    vault = Vault(vault_path, store, allow_external_sources=True)
    companion = Companion(
        vault,
        materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
    )

    def _ingest(path: Path, _store: "LadybugStoreType") -> object:
        # ``_store`` is the same object ``companion`` already holds; ignored.
        return companion.remember(path)

    return _ingest


def _install_rebuild_signal_handlers() -> dict[signal.Signals, object]:
    _clear_rebuild_interrupted()
    previous_handlers: dict[signal.Signals, object] = {}
    for rebuild_signal in _REBUILD_SIGNALS:
        previous_handlers[rebuild_signal] = signal.getsignal(rebuild_signal)
        signal.signal(rebuild_signal, _handle_rebuild_signal)
    return previous_handlers


def _restore_rebuild_signal_handlers(previous_handlers: dict[signal.Signals, object]) -> None:
    for rebuild_signal, handler in previous_handlers.items():
        signal.signal(rebuild_signal, handler)


def _handle_rebuild_signal(signum: int, frame: FrameType | None) -> None:
    del frame
    _REBUILD_INTERRUPT.interrupted = True
    _REBUILD_INTERRUPT.signal_name = _signal_name(signum)


def _raise_if_rebuild_interrupted(vault_path: Path, *, step: int) -> None:
    if _rebuild_interrupted():
        raise _make_rebuild_interrupted(vault_path, step=step)


def _make_rebuild_interrupted(vault_path: Path, *, step: int) -> RebuildInterrupted:
    signal_name = getattr(_REBUILD_INTERRUPT, "signal_name", None)
    prefix = signal_name if signal_name is not None else "signal"
    return RebuildInterrupted(
        vault_path,
        signal_name=signal_name,
        message=f"{prefix} during rebuild step {step}",
    )


def _rebuild_interrupted() -> bool:
    return bool(getattr(_REBUILD_INTERRUPT, "interrupted", False))


def _clear_rebuild_interrupted() -> None:
    _REBUILD_INTERRUPT.interrupted = False
    _REBUILD_INTERRUPT.signal_name = None


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"signal {signum}"


def _close_tmp_rebuild_handles(
    store: LadybugStore | None,
    tmp_handle: VaultGraphHandle | None,
) -> None:
    try:
        if store is not None:
            store.close()
        elif tmp_handle is not None:
            tmp_handle.close()
    except Exception:
        return


def _bootstrap_graph_at_path(
    vault_path: Path,
    graph_path: Path,
    dim: int | None = None,
) -> VaultGraphHandle:
    """Create a fresh graph at ``graph_path`` built for vector width ``dim``.

    ``dim`` defaults to the vault's configured embedding width. The rebuild/reembed
    callers pass the width they intend the new graph to carry. No dim-guard runs
    here — this executor only ever builds a *fresh* graph, so there is nothing to
    mismatch against.
    """
    if dim is None:
        dim = _resolve_configured_dim(vault_path)
    identity = schema.new_graph_identity()
    database: ladybug.Database | None = None
    connection: object | None = None
    try:
        _remove_if_exists(graph_path)
        database = ladybug.Database(graph_path)
        connection = ladybug.Connection(database)
        schema.verify_schema_version(connection, file_path=graph_path)
        _execute_bootstrap_ddl(
            connection,
            dim,
            graph_generation=identity.graph_generation,
            identity_contract_version=identity.identity_contract_version,
        )
        schema.verify_schema_version(connection, file_path=graph_path)
    except Exception as exc:
        _close_connection(connection)
        if database is not None:
            database.close()
        _discard_path(graph_path)
        raise BootstrapPartial(vault_path, file_path=graph_path, cause=exc) from exc
    _close_connection(connection)
    return VaultGraphHandle(
        vault_path=vault_path,
        schema_version=schema.CURRENT_SCHEMA_VERSION,
        embedding_dim=dim,
        database=database,
        graph_generation=identity.graph_generation,
        identity_contract_version=identity.identity_contract_version,
        graph_file_identity=_graph_file_identity(graph_path),
    )


def _execute_bootstrap_ddl(
    connection: object,
    dim: int,
    *,
    graph_generation: str | None = None,
    identity_contract_version: str | None = None,
) -> None:
    for statement in schema.ddl_statements(
        dim,
        graph_generation=graph_generation,
        identity_contract_version=identity_contract_version,
    ):
        try:
            result = connection.execute(statement)
        except Exception as exc:
            if _is_idempotent_skip(statement, exc):
                continue
            raise
        _close_result(result)


def _is_idempotent_skip(statement: str, exc: Exception) -> bool:
    """True when re-issued DDL on an already-current graph reports the object is
    already present — CREATE_VECTOR_INDEX ('already exists') or a migration ALTER
    ('already has property') — safe to skip."""
    message = str(exc).lower()
    if statement.startswith("CALL CREATE_VECTOR_INDEX("):
        return "already exists" in message
    if statement.startswith("ALTER TABLE "):
        return "already has property" in message or "already exists" in message
    return False


# Generated manifest under .marginalia/sources/ — a derived tracking index that
# links the real source files, never an ingestable source itself. Re-ingesting it
# would mint spurious nodes from index boilerplate, so it is excluded.
_SOURCES_MANIFEST = "index.md"


def _deterministic_rebuild_files(vault_path: Path) -> list[Path]:
    """The canonical source set rebuild re-extracts, in deterministic posix order.

    Covers all three places a vault stores markdown sources: ``notes/`` and
    ``refs/`` (hand-authored layouts) AND ``.marginalia/sources/**`` (where the
    companion saves ingested external sources — real vaults keep notes/refs empty
    and their whole corpus here). All ingestible text suffixes are included (the
    F11 tree scheme preserves original names, so ``.txt``/``.markdown`` copies
    and NESTED ``index.md`` files are real sources); the TOP-LEVEL generated
    ``sources/index.md`` manifest is excluded (it is a derived index), AND the
    SAME basename-level denylist the folder watch applies at ingest is applied
    here so rebuild does not re-mint the junk the live path already excludes
    (tooling/scaffolding files, dot-files, tracking index pages, and the
    ``tracking/tracking`` double-descent duplicates). The denylist is evaluated
    RELATIVE TO each source-key root — never the vault root — because every
    durable source lives under ``.marginalia/sources/`` and a vault-relative
    check would trip the watch's ``.marginalia`` internal-dir rule and reject the
    entire durable corpus. Sharing ``_is_denylisted_relpath`` keeps the rebuild
    and watch source-selection from ever drifting apart."""
    candidates: list[Path] = []
    notes_path = vault_path / "notes"
    refs_path = vault_path / "refs"
    sources_path = vault_path / _MARGINALIA_DIR / "sources"
    if notes_path.exists():
        candidates.extend(path for path in notes_path.rglob("*.md") if path.is_file())
    if refs_path.exists():
        candidates.extend(path for path in refs_path.rglob("*") if path.is_file())
    if sources_path.exists():
        from okto_neuron.server._folder_watch import _is_denylisted_relpath
        from okto_neuron.server._ingest_queue import TEXT_SUFFIXES

        manifest_path = sources_path / _SOURCES_MANIFEST
        for path in sources_path.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if path == manifest_path:
                continue
            # Strip the leading source-key hash segment so the denylist sees the
            # mirrored tree (``tracking/…``) as it was at ingest, not the hash dir.
            rel_parts = path.relative_to(sources_path).parts
            rel = Path(*rel_parts[1:]) if len(rel_parts) > 1 else Path(*rel_parts)
            if _is_denylisted_relpath(rel):
                continue
            candidates.append(path)
    return sorted(candidates, key=lambda path: _relative_posix(vault_path, path))


def _validated_rebuild_source_files(
    vault_path: Path,
    source_files: Sequence[Path] | None,
) -> list[Path]:
    """Return canonical sources or a verified permutation for an acceptance arm.

    A source-order comparison is only meaningful when coverage is identical.  The
    private override therefore cannot add, omit, or duplicate a source; it may
    change order only.  Normal production callers never supply it.
    """

    canonical = _deterministic_rebuild_files(vault_path)
    if source_files is None:
        return canonical
    ordered = list(source_files)
    if len(ordered) != len(canonical) or set(ordered) != set(canonical):
        raise ValueError(
            "source_files must be an exact permutation of the canonical rebuild source set"
        )
    return ordered


def _relative_posix(vault_path: Path, file_path: Path) -> str:
    return file_path.relative_to(vault_path).as_posix()


def _write_rebuild_state(state_path: Path, payload: dict[str, object]) -> None:
    tmp_path = state_path.with_name(f"{state_path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, state_path)
    _fsync_parent_dir(state_path)


def _rebuild_error_summary(exc: BaseException, *, vault_path: Path) -> str:
    """Return one actionable, secret-redacted rebuild failure boundary.

    Provider adapters already redact their own errors, but internal rebuild
    failures may still carry a configured credential value in an exception.
    Remove every credential referenced by the effective vault configuration
    before persisting the boundary in job/state artifacts.
    """

    text = " ".join(str(exc).split()) or type(exc).__name__
    try:
        from okto_neuron.config import VaultConfig

        cfg = VaultConfig.load(vault_path)
        key_envs = {cfg.embedding.resolved_provider().api_key_env}
        key_envs.update(
            cfg.llm.resolved(step).api_key_env
            for step in ("extraction", "judge", "curator", "relation_curator", "ask")
        )
        for key_env in key_envs:
            secret = _secret_env(key_env) if key_env else None
            if secret:
                text = text.replace(secret, "[redacted]")
    except Exception:  # noqa: BLE001 - diagnostics must not hide the original failure
        pass
    return f"{type(exc).__name__}: {text}"[:_REBUILD_ERROR_LIMIT]


def _write_failed_rebuild_state(
    state_path: Path,
    *,
    vault_path: Path,
    error: BaseException,
) -> None:
    """Mark a rebuild failed without discarding its last durable evidence."""

    payload: dict[str, object] = {}
    try:
        loaded = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            payload = loaded
    except (OSError, TypeError, ValueError):
        pass
    payload.update(
        phase="failed",
        failed_at=_utc_now(),
        error=_rebuild_error_summary(error, vault_path=vault_path),
    )
    _write_rebuild_state(state_path, payload)


def _write_interrupted_state(state_path: Path, started_at: str) -> None:
    """Write the ``phase: interrupted`` rebuild state, reconstructing
    ``current_file``/``last_file_done`` from the last ``in_progress`` state the
    build core wrote per-file. Keeps ``kg rebuild``'s observable interrupt
    behavior unchanged after the build core was extracted (the core re-raises and
    the CLI owns interrupt-state, since the in-process path has no signal interrupt)."""
    files_done: list[str] = []
    current_file: str | None = None
    try:
        last = json.loads(state_path.read_text(encoding="utf-8"))
        if isinstance(last, dict):
            raw_done = last.get("files_done")
            if isinstance(raw_done, list):
                files_done = [str(f) for f in raw_done]
            cf = last.get("current_file")
            current_file = str(cf) if cf is not None else None
    except (OSError, ValueError, TypeError):
        pass
    last_file_done = files_done[-1] if files_done else None
    _write_rebuild_state(
        state_path,
        {
            "phase": "interrupted",
            "files_done": files_done,
            "current_file": current_file,
            "last_file_done": last_file_done,
            "started_at": started_at,
            "interrupted_at": _utc_now(),
        },
    )


def _rebuild_artifact_dir(vault_path: Path, graph_generation: str) -> Path:
    target = vault_path / _MARGINALIA_DIR / _REBUILD_ARTIFACTS_DIR / graph_generation
    target.mkdir(parents=True, exist_ok=True)
    _fsync_parent_dir(target)
    return target


def _preserve_failed_staging(
    vault_path: Path,
    tmp_graph_path: Path,
    built: dict[str, object],
    state_path: Path,
    *,
    backend_name: str = "ladybug",
) -> Path:
    generation = str(built.get("graph_generation") or "legacy-unset")
    artifact_dir = _rebuild_artifact_dir(vault_path, generation)
    if backend_name == "neo4j":
        # Neo4j's staged "path" is a synthetic marker never written to disk
        # (Neo4jStaging docstring) -- there are no bytes for
        # ``_move_graph_family`` to relocate. The failed candidate's own
        # nodes/edges are already durably tagged with their build generation
        # in the database (nothing to move OR delete for postmortem), so
        # this just records where they live for the failure report instead
        # of a moved sibling path.
        retained = artifact_dir / f"staging.failed.{tmp_graph_path.name}"
    else:
        retained = artifact_dir / "staging.failed.lbug"
        _move_graph_family(tmp_graph_path, retained)
    payload = {
        "phase": "validation_failed",
        "failed_at": _utc_now(),
        "staging_graph": str(retained),
        "current_file": built.get("failing_file"),
        **built,
    }
    _write_rebuild_state(state_path, payload)
    _write_rebuild_state(artifact_dir / "validation.json", payload)
    return retained


def _require_rebuild_candidate(
    vault_path: Path,
    tmp_graph_path: Path,
    state_path: Path,
    built: dict[str, object],
    *,
    backend_name: str = "ladybug",
) -> None:
    if built.get("swap_allowed") is True:
        return
    retained = _preserve_failed_staging(
        vault_path, tmp_graph_path, built, state_path, backend_name=backend_name
    )
    failed_files = built.get("failed_files")
    failing_file = built.get("failing_file")
    provider_failed = isinstance(failed_files, list) and bool(failed_files)
    if failing_file is None and isinstance(failed_files, list) and failed_files:
        first = failed_files[0]
        if isinstance(first, dict):
            failing_file = first.get("file")
    final_audit = built.get("final_audit")
    audit_status = "provider_failed" if provider_failed else None
    if audit_status is None and isinstance(final_audit, dict):
        technical_status = str(final_audit.get("status") or "unverified")
        if technical_status != "verified":
            audit_status = technical_status
    semantic_gate = built.get("semantic_gate")
    if (
        audit_status is None
        and isinstance(semantic_gate, dict)
        and semantic_gate.get("swap_allowed") is not True
    ):
        audit_status = "semantic_failed"
    raise RebuildAuditFailed(
        vault_path,
        staging_path=retained,
        failing_file=str(failing_file) if failing_file is not None else None,
        audit_status=str(audit_status) if audit_status is not None else "unverified",
    )


def _prepare_rebuild_backup(
    vault_path: Path,
    graph_generation: str,
) -> tuple[Path, Path]:
    artifact_dir = _rebuild_artifact_dir(vault_path, graph_generation)
    backup_path = artifact_dir / "previous-graph.lbug"
    sidecar = integrity_state_path(vault_path)
    if sidecar.exists():
        snapshot = artifact_dir / "previous-graph-integrity.json"
        shutil.copy2(sidecar, snapshot)
        with snapshot.open("rb") as handle:
            os.fsync(handle.fileno())
        _fsync_parent_dir(snapshot)
    return backup_path, artifact_dir


def _write_previous_semantic_materialization(
    artifact_dir: Path,
    *,
    graph_generation: str | None,
    fingerprints: dict[str, str | None],
) -> Path | None:
    """Pin the previous graph's policy identity beside its rollback bytes."""

    if not graph_generation or any(
        not fingerprints.get(key)
        for key in (
            "config",
            "extraction",
            "semantic_policy",
        )
    ):
        return None
    from okto_neuron.semantic_fingerprint import write_semantic_materialization

    target = artifact_dir / _PREVIOUS_SEMANTIC_MATERIALIZATION
    write_semantic_materialization(
        target,
        graph_generation=graph_generation,
        fingerprints=fingerprints,
        source="pre_rebuild",
    )
    return target


def _semantic_policy_sidefiles(vault_path: Path) -> dict[str, Path]:
    """Files whose exact bytes participate in the semantic-policy fingerprint."""

    from okto_neuron.predicates import (
        PREDICATE_ALIASES,
        PREDICATE_DIRNAME,
        PREDICATE_REGISTRY,
    )
    from okto_neuron.reconcile.authority import AUTHORITY_DIRNAME, AUTHORITY_INDEX
    from okto_neuron.reconcile.decisions import IDENTITY_DECISIONS

    root = vault_path / _MARGINALIA_DIR
    return {
        "authority/decisions.json": root / AUTHORITY_DIRNAME / IDENTITY_DECISIONS,
        "authority/index.json": root / AUTHORITY_DIRNAME / AUTHORITY_INDEX,
        "predicates/aliases.json": root / PREDICATE_DIRNAME / PREDICATE_ALIASES,
        "predicates/registry.json": root / PREDICATE_DIRNAME / PREDICATE_REGISTRY,
    }


def _capture_semantic_policy_sidefiles(vault_path: Path) -> dict[str, bytes | None]:
    """Capture the exact decision-sidefile state before a rebuild can evolve it."""

    return {
        name: path.read_bytes() if path.is_file() else None
        for name, path in _semantic_policy_sidefiles(vault_path).items()
    }


def _write_semantic_policy_checkpoint(
    artifact_dir: Path,
    *,
    graph_generation: str,
    semantic_policy_fingerprint: str,
    files: dict[str, bytes | None],
) -> Path:
    """Persist a secret-free, exact-byte decision checkpoint beside graph bytes."""

    expected = set(_semantic_policy_sidefiles(artifact_dir).keys())
    if set(files) != expected:
        raise ValueError("semantic policy checkpoint has an unexpected file set")
    encoded: dict[str, dict[str, object]] = {}
    for name, content in sorted(files.items()):
        if content is None:
            encoded[name] = {"status": "absent"}
        else:
            encoded[name] = {
                "status": "present",
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
    target = artifact_dir / _PREVIOUS_SEMANTIC_POLICY
    _write_rebuild_state(
        target,
        {
            "schema_version": _SEMANTIC_POLICY_CHECKPOINT_SCHEMA,
            "graph_generation": graph_generation,
            "semantic_policy_fingerprint": semantic_policy_fingerprint,
            "files": encoded,
        },
    )
    return target


def _load_semantic_policy_checkpoint(
    path: Path,
    *,
    expected_graph_generation: str,
    expected_semantic_policy_fingerprint: str,
) -> dict[str, bytes | None]:
    """Load and verify one decision checkpoint without applying it."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "graph_generation",
        "semantic_policy_fingerprint",
        "files",
    }:
        raise ValueError("semantic policy checkpoint has unexpected fields")
    if payload["schema_version"] != _SEMANTIC_POLICY_CHECKPOINT_SCHEMA:
        raise ValueError("unsupported semantic policy checkpoint schema")
    if payload["graph_generation"] != expected_graph_generation:
        raise ValueError("semantic policy checkpoint graph generation mismatch")
    if payload["semantic_policy_fingerprint"] != expected_semantic_policy_fingerprint:
        raise ValueError("semantic policy checkpoint fingerprint mismatch")
    raw_files = payload["files"]
    expected_names = set(_semantic_policy_sidefiles(path).keys())
    if not isinstance(raw_files, dict) or set(raw_files) != expected_names:
        raise ValueError("semantic policy checkpoint file set mismatch")
    decoded: dict[str, bytes | None] = {}
    for name, raw in raw_files.items():
        if not isinstance(raw, dict):
            raise ValueError(f"semantic policy checkpoint {name} entry is invalid")
        if raw == {"status": "absent"}:
            decoded[name] = None
            continue
        if set(raw) != {"status", "sha256", "content_base64"} or raw.get("status") != "present":
            raise ValueError(f"semantic policy checkpoint {name} entry is invalid")
        try:
            content = base64.b64decode(str(raw["content_base64"]), validate=True)
        except ValueError as exc:
            raise ValueError(f"semantic policy checkpoint {name} content is invalid") from exc
        if hashlib.sha256(content).hexdigest() != raw["sha256"]:
            raise ValueError(f"semantic policy checkpoint {name} hash mismatch")
        decoded[name] = content
    return decoded


def _restore_semantic_policy_sidefiles(
    vault_path: Path,
    files: dict[str, bytes | None],
) -> None:
    """Restore exact decision bytes while the caller holds the vault write fence."""

    targets = _semantic_policy_sidefiles(vault_path)
    if set(files) != set(targets):
        raise ValueError("semantic policy restore has an unexpected file set")
    for name, path in targets.items():
        content = files[name]
        if content is None:
            path.unlink(missing_ok=True)
            if path.parent.exists():
                _fsync_parent_dir(path)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temp.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
            _fsync_parent_dir(path)
        finally:
            temp.unlink(missing_ok=True)


def _semantic_source_manifest(
    vault_path: Path,
    source_files: Sequence[Path],
) -> str:
    """Bind a discovery checkpoint to the exact ordered source bytes."""

    digest = hashlib.sha256()
    for path in source_files:
        relative = _relative_posix(vault_path, path)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(_sha256_file(path)))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _semantic_discovery_checkpoint_payload(
    *,
    graph_generation: str,
    base_fingerprints: dict[str, str],
    discovered_fingerprints: dict[str, str],
    source_manifest: str,
    files: dict[str, bytes | None],
) -> dict[str, object]:
    expected_names = set(_semantic_policy_sidefiles(Path(".")).keys())
    if set(files) != expected_names:
        raise ValueError("semantic discovery checkpoint has an unexpected file set")
    encoded: dict[str, dict[str, object]] = {}
    for name, content in sorted(files.items()):
        if content is None:
            encoded[name] = {"status": "absent"}
        else:
            encoded[name] = {
                "status": "present",
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
    return {
        "schema_version": _SEMANTIC_DISCOVERY_CHECKPOINT_SCHEMA,
        "graph_generation": graph_generation,
        "base_fingerprints": base_fingerprints,
        "discovered_fingerprints": discovered_fingerprints,
        "source_manifest": source_manifest,
        "files": encoded,
    }


def _decode_semantic_discovery_checkpoint(
    payload: object,
) -> tuple[dict[str, object], dict[str, bytes | None]]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "graph_generation",
        "base_fingerprints",
        "discovered_fingerprints",
        "source_manifest",
        "files",
    }:
        raise ValueError("semantic discovery checkpoint has unexpected fields")
    if payload["schema_version"] != _SEMANTIC_DISCOVERY_CHECKPOINT_SCHEMA:
        raise ValueError("unsupported semantic discovery checkpoint schema")
    for key in ("base_fingerprints", "discovered_fingerprints"):
        value = payload[key]
        if (
            not isinstance(value, dict)
            or set(value)
            != {
                "config",
                "extraction",
                "semantic_policy",
            }
            or any(not isinstance(item, str) for item in value.values())
        ):
            raise ValueError(f"semantic discovery checkpoint {key} is invalid")
    if not isinstance(payload["source_manifest"], str):
        raise ValueError("semantic discovery checkpoint source manifest is invalid")
    raw_files = payload["files"]
    expected_names = set(_semantic_policy_sidefiles(Path(".")).keys())
    if not isinstance(raw_files, dict) or set(raw_files) != expected_names:
        raise ValueError("semantic discovery checkpoint file set mismatch")
    decoded: dict[str, bytes | None] = {}
    for name, raw in raw_files.items():
        if raw == {"status": "absent"}:
            decoded[name] = None
            continue
        if (
            not isinstance(raw, dict)
            or set(raw)
            != {
                "status",
                "sha256",
                "content_base64",
            }
            or raw.get("status") != "present"
        ):
            raise ValueError(f"semantic discovery checkpoint {name} entry is invalid")
        try:
            content = base64.b64decode(str(raw["content_base64"]), validate=True)
        except ValueError as exc:
            raise ValueError(f"semantic discovery checkpoint {name} content is invalid") from exc
        if hashlib.sha256(content).hexdigest() != raw["sha256"]:
            raise ValueError(f"semantic discovery checkpoint {name} hash mismatch")
        decoded[name] = content
    return payload, decoded


def _pending_semantic_policy_path(vault_path: Path) -> Path:
    return vault_path / _MARGINALIA_DIR / _PENDING_SEMANTIC_POLICY


def _checkpoint_semantic_discovery(
    vault_path: Path,
    built: dict[str, object],
    *,
    base_fingerprints: dict[str, str],
    source_manifest: str,
) -> Path | None:
    """Retain a technically verified policy discovery without publishing it live."""

    final_audit = built.get("final_audit")
    fingerprint_pass = built.get("fingerprint_pass")
    if (
        built.get("swap_allowed") is True
        or not isinstance(final_audit, dict)
        or final_audit.get("status") != "verified"
        or not isinstance(fingerprint_pass, dict)
        or fingerprint_pass.get("stable") is True
    ):
        return None
    discovered = _effective_semantic_fingerprint_triplet(vault_path)
    if (
        discovered["config"] != base_fingerprints["config"]
        or discovered["extraction"] != base_fingerprints["extraction"]
        or discovered["semantic_policy"] == base_fingerprints["semantic_policy"]
    ):
        return None
    graph_generation = str(built.get("graph_generation") or "")
    if not graph_generation:
        return None
    payload = _semantic_discovery_checkpoint_payload(
        graph_generation=graph_generation,
        base_fingerprints=base_fingerprints,
        discovered_fingerprints=discovered,
        source_manifest=source_manifest,
        files=_capture_semantic_policy_sidefiles(vault_path),
    )
    artifact_dir = _rebuild_artifact_dir(vault_path, graph_generation)
    artifact_path = artifact_dir / _DISCOVERED_SEMANTIC_POLICY
    _write_rebuild_state(artifact_path, payload)
    _write_rebuild_state(_pending_semantic_policy_path(vault_path), payload)
    return artifact_path


def _apply_pending_semantic_policy(
    vault_path: Path,
    *,
    expected_base: dict[str, str],
    expected_source_manifest: str,
) -> Path | None:
    """Seed an explicit rebuild from compatible prior discovery evidence."""

    path = _pending_semantic_policy_path(vault_path)
    if not path.is_file():
        return None
    original = _capture_semantic_policy_sidefiles(vault_path)
    try:
        payload, files = _decode_semantic_discovery_checkpoint(
            json.loads(path.read_text(encoding="utf-8"))
        )
        if (
            payload["base_fingerprints"] != expected_base
            or payload["source_manifest"] != expected_source_manifest
        ):
            _clear_pending_semantic_policy(vault_path)
            return None
        _restore_semantic_policy_sidefiles(vault_path, files)
        measured = _effective_semantic_fingerprint_triplet(vault_path)
        if measured != payload["discovered_fingerprints"]:
            raise ValueError("semantic discovery checkpoint does not reproduce its fingerprint")
        return path
    except (OSError, TypeError, ValueError) as exc:
        _restore_semantic_policy_sidefiles(vault_path, original)
        _LOG.warning("ignoring invalid pending semantic policy checkpoint %s: %s", path, exc)
        _clear_pending_semantic_policy(vault_path)
        return None


def _clear_pending_semantic_policy(vault_path: Path) -> None:
    path = _pending_semantic_policy_path(vault_path)
    try:
        path.unlink()
        _fsync_parent_dir(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        _LOG.warning("could not remove stale semantic policy checkpoint %s: %s", path, exc)


def _publish_built_semantic_materialization(
    vault_path: Path,
    built: dict[str, object],
) -> dict[str, object]:
    """Publish the staged report's fingerprints for the installed generation."""

    report = built.get("semantic_quality")
    snapshot = report.get("semantic_snapshot") if isinstance(report, dict) else None
    fingerprints = snapshot.get("fingerprints") if isinstance(snapshot, dict) else None
    if not isinstance(fingerprints, dict) or fingerprints.get("status") != "measured":
        raise RebuildAuditFailed(
            vault_path,
            audit_status="semantic_materialization_unmeasured",
            message="rebuilt graph has no complete semantic materialization fingerprints",
        )
    from okto_neuron.semantic_fingerprint import publish_semantic_materialization

    return publish_semantic_materialization(
        vault_path,
        graph_generation=str(built.get("graph_generation") or ""),
        fingerprints={
            "config": fingerprints.get("config"),
            "extraction": fingerprints.get("extraction"),
            "semantic_policy": fingerprints.get("semantic_policy"),
        },
        source="rebuild",
    )


def _publish_integrity_result(
    vault_path: Path,
    result: IntegrityAuditResult,
    *,
    audit_id: str | None = None,
) -> None:
    if result.verified:
        reason = None
    elif result.issues:
        first = result.issues[0]
        reason = f"{result.issue_count} issue(s); first={first.code}:{first.artifact_id or 'graph'}"
    elif result.incomplete_reasons:
        reason = "; ".join(result.incomplete_reasons)
    else:
        reason = f"graph integrity audit finished {result.status.value}"
    write_integrity_state(
        vault_path,
        GraphIntegrityState(
            status=result.status,
            graph_generation=result.graph_generation,
            writer_fenced=not result.verified,
            reason=reason,
            audit_id=audit_id or uuid4().hex,
        ),
    )


def _mark_rebuild_generation_verifying(
    vault_path: Path,
    graph_generation: str,
    *,
    audit_id: str | None = None,
) -> None:
    """Fence newly installed bytes before the post-swap audit starts."""

    write_integrity_state(
        vault_path,
        GraphIntegrityState(
            status=AuditStatus.VERIFYING,
            graph_generation=graph_generation,
            writer_fenced=True,
            reason="post-swap graph integrity audit is running",
            audit_id=audit_id or uuid4().hex,
        ),
    )


# M2b (spec §2.1/§3): _active_graph_sidecars/_move_graph_family/
# _discard_graph_family/_copy_closed_graph_checkpoint/_swap_rebuilt_graph/
# _fsync_parent_dir moved verbatim to store/staging.py; re-exported at the
# top of this module (see the okto_neuron.store.staging import above) so
# heal.py's by-name import and server/_curation.py's kg_cli.<name>(...)
# module-qualified calls keep working unmodified until re-routed directly
# onto store.staging/LadybugStaging.


def _close_live_graph_handles(vault_path: Path) -> None:
    from okto_neuron.store import vault as vault_module

    cached = vault_module._STORE_CACHE.pop(vault_path, None)
    if cached is not None:
        cached.close()
    VaultConnection.close_vault(vault_path)
    reset_bootstrap_cache_for_tests(vault_path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_of_graph(graph_path: Path, backend_name: str) -> str:
    """A durable content fingerprint for the rebuild/reembed completion report.

    Ladybug's live graph is one file — byte-identical to a bare
    :func:`_sha256_file` call. A non-Ladybug backend's live graph is a
    directory (e.g. Grafx's ``graph.grafx``, which ``_sha256_file`` cannot
    open), so this hashes every regular file under it in deterministic
    relative-path order instead.
    """
    if backend_name == "ladybug":
        return _sha256_file(graph_path)
    if backend_name == "neo4j":
        # No filesystem tree to hash for a server-side backend: fingerprint
        # the store's own generation tag instead (a logical content marker,
        # not a byte hash -- graph_path is a synthetic, never-written marker
        # path for Neo4j).
        digest = hashlib.sha256()
        digest.update(graph_path.name.encode("utf-8"))
        return digest.hexdigest()
    digest = hashlib.sha256()
    for file_path in sorted(
        (candidate for candidate in graph_path.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(graph_path).as_posix(),
    ):
        digest.update(file_path.relative_to(graph_path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256_file(file_path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _remove_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _discard_path(path: Path) -> None:
    try:
        _remove_if_exists(path)
    except OSError:
        return


def _close_connection(connection: object | None) -> None:
    if connection is None:
        return
    close = getattr(connection, "close", None)
    if callable(close):
        close()


def _close_result(result: object) -> None:
    if isinstance(result, list):
        for item in result:
            _close_result(item)
        return
    close = getattr(result, "close", None)
    if callable(close):
        close()


# ── retroactive entity reconciliation (v0.0.5, ADR 0008) ──────────────────────--
def _open_reconcile_context(vault_path: Path, *, open_vault: bool = True):
    """Open the vault and build the conservative judge + embedder + the two
    off-graph side-stores. Mirrors the companion's judge/embedder construction
    (CRITICAL: the embedder must be the SAME provider the read side uses).

    ``open_vault=False`` (``review list``, a pure reader) never opens the graph
    store: it returns ``None`` for the vault and touches only the two JSON
    side-stores, so it needs no writer lease.
    """
    from okto_neuron.config import VaultConfig
    from okto_neuron.reconcile.authority import AUTHORITY_DIRNAME, AuthorityIndex
    from okto_neuron.reconcile.queue import RECONCILE_DIRNAME, ReconcileQueue
    from okto_neuron.resolve import LLMMergeJudge
    from okto_neuron.vault import Vault

    if open_vault:
        vault = Vault.open(vault_path)
    else:
        if not vault_path.exists():
            raise VaultNotFoundError(vault_path)
        vault = None
    try:
        cfg = VaultConfig.load(vault_path)
    except Exception:  # noqa: BLE001 — missing/partial config → defaults
        cfg = VaultConfig()
    judge_resolved = cfg.llm.resolved("judge")
    judge_model = getattr(judge_resolved, "model", "") or ""

    def build_judge():
        from okto_neuron.llm import get_provider, sampler_overrides

        return LLMMergeJudge(
            get_provider(judge_resolved),
            **sampler_overrides(judge_resolved),
            top_p=judge_resolved.top_p,
            top_k=judge_resolved.top_k,
            min_p=judge_resolved.min_p,
            presence_penalty=judge_resolved.presence_penalty,
            enable_thinking=judge_resolved.enable_thinking,
            system_prompt=cfg.llm.judge.system_prompt,
        )

    marginalia_dir = vault_path / _MARGINALIA_DIR
    authority = AuthorityIndex(marginalia_dir / AUTHORITY_DIRNAME)
    queue = ReconcileQueue(marginalia_dir / RECONCILE_DIRNAME, authority)
    return vault, build_judge, authority, queue, judge_model


def kg_reconcile_propose(
    vault: Path | None,
    *,
    type: str | None = None,
    use_cluster_judge: bool = False,
    as_json: bool = False,
) -> int:
    """Read-only: emit candidate clusters + per-cluster verdicts. Writes NOTHING."""
    from okto_neuron.reconcile.candidates import generate_candidate_clusters
    from okto_neuron.reconcile.propose import adjudicate_cluster

    vault_path = _resolve_rebuild_vault(vault)
    with vault_writer(vault_path, "reconcile propose"):
        vault, build_judge, authority, _queue, _model = _open_reconcile_context(vault_path)
        try:
            from okto_neuron.reconcile.decisions import IdentityDecisionIndex

            decisions = IdentityDecisionIndex(authority.dir)
            judge = build_judge()
            clusters = generate_candidate_clusters(vault.store, embedder=vault.embedder, type=type)
            rows = []
            for cluster in clusters:
                verdict = adjudicate_cluster(
                    cluster,
                    vault.store,
                    judge=judge,
                    embedder=vault.embedder,
                    use_cluster_judge=use_cluster_judge,
                    merge_blocked=decisions.is_distinct,
                )
                rows.append((cluster, verdict))
        finally:
            vault.close()

        if as_json:
            payload = [
                {
                    "cluster_id": c.cluster_id,
                    "type": c.type,
                    "member_ids": list(c.member_ids),
                    "lanes": sorted(c.lane_evidence.keys()),
                    "same": v.same,
                    "confidence": v.confidence,
                    "canonical_id": v.canonical_id,
                    "corroboration": v.corroboration,
                    "reason": v.reason,
                }
                for c, v in rows
            ]
            click.echo(json.dumps(payload, indent=2))
            return 0

        if not rows:
            click.echo("(no candidate clusters)")
            return 0
        for cluster, verdict in rows:
            mark = "SAME" if verdict.same else "distinct"
            click.echo(
                f"{cluster.cluster_id} [{cluster.type}] {mark} "
                f"conf={verdict.confidence:.2f} corr={verdict.corroboration} "
                f"members={len(cluster.member_ids)} lanes={','.join(sorted(cluster.lane_evidence))}"
            )
        return 0


def kg_reconcile_apply(
    vault: Path | None,
    *,
    type: str | None = None,
    use_cluster_judge: bool = False,
    as_json: bool = False,
) -> int:
    """Auto-merge high-confidence clusters to the off-graph AuthorityIndex; queue
    the rest. Writes ONLY the two JSON side-files — never the graph."""
    from okto_neuron.reconcile.apply import apply_reconciliation

    vault_path = _resolve_rebuild_vault(vault)
    with vault_writer(vault_path, "reconcile apply"):
        vault, build_judge, authority, queue, judge_model = _open_reconcile_context(vault_path)
        try:
            from okto_neuron.reconcile.decisions import IdentityDecisionIndex

            decisions = IdentityDecisionIndex(authority.dir)
            judge = build_judge()
            report = apply_reconciliation(
                vault.store,
                embedder=vault.embedder,
                judge=judge,
                authority=authority,
                queue=queue,
                type=type,
                use_cluster_judge=use_cluster_judge,
                judge_model=judge_model,
                merge_blocked=decisions.is_distinct,
            )
        finally:
            vault.close()

        if as_json:
            click.echo(
                json.dumps(
                    {
                        "auto_merged": report.auto_merged,
                        "queued": report.queued,
                        "skipped": report.skipped,
                    },
                    indent=2,
                )
            )
            return 0
        click.echo(
            f"reconcile apply: {len(report.auto_merged)} auto-merged (off-graph), "
            f"{len(report.queued)} queued, {len(report.skipped)} skipped"
        )
        return 0


def kg_reconcile_review_list(vault: Path | None, *, as_json: bool = False) -> int:
    """List queued clusters awaiting confirmation."""
    # Pure reader: no graph store opened, no writer lease taken.
    vault_path = _resolve_rebuild_vault(vault)
    _vault, _build_judge, _authority, queue, _model = _open_reconcile_context(
        vault_path, open_vault=False
    )
    entries = queue.list()
    if as_json:
        click.echo(
            json.dumps(
                [
                    {
                        "cluster_id": qc.cluster.cluster_id,
                        "type": qc.cluster.type,
                        "confidence": qc.verdict.confidence,
                        "corroboration": qc.verdict.corroboration,
                        "member_ids": list(qc.cluster.member_ids),
                    }
                    for qc in entries
                ],
                indent=2,
            )
        )
        return 0
    if not entries:
        click.echo("(review queue empty)")
        return 0
    for qc in entries:
        click.echo(
            f"{qc.cluster.cluster_id} [{qc.cluster.type}] "
            f"conf={qc.verdict.confidence:.2f} corr={qc.verdict.corroboration} "
            f"members={len(qc.cluster.member_ids)}"
        )
    return 0


def kg_reconcile_review_confirm(cluster_id: str, vault: Path | None) -> int:
    """Confirm a queued cluster → off-graph AuthorityIndex; dequeue."""
    vault_path = _resolve_rebuild_vault(vault)
    with vault_writer(vault_path, "reconcile review confirm"):
        vault, _build_judge, _authority, queue, judge_model = _open_reconcile_context(vault_path)
        try:
            try:
                rec = queue.confirm(cluster_id, judge_model=judge_model)
            except KeyError:
                click.echo(f"no queued cluster {cluster_id!r}", err=True)
                return 1
        finally:
            vault.close()
        click.echo(f"confirmed {rec.cluster_id} → canonical {rec.canonical_name!r} (off-graph)")
        return 0


def kg_reconcile_review_reject(cluster_id: str, vault: Path | None) -> int:
    """Reject (drop) a queued cluster."""
    vault_path = _resolve_rebuild_vault(vault)
    with vault_writer(vault_path, "reconcile review reject"):
        vault, _build_judge, _authority, queue, _model = _open_reconcile_context(vault_path)
        try:
            queue.reject(cluster_id)
        finally:
            vault.close()
        click.echo(f"rejected {cluster_id}")
        return 0


def kg_reconcile_heal(vault: Path | None) -> int:
    """ADR 0009 P3: materialize the confirmed off-graph equivalences into the graph
    via a deterministic no-LLM graph→fresh-graph copy + atomic swap."""
    from okto_neuron.reconcile.heal import heal_via_copy

    vault_path = _resolve_rebuild_vault(vault)
    code = heal_via_copy(vault_path)
    if code == 0:
        click.echo("reconcile heal: collapsed equivalences into a fresh graph + swap")
    return code


def kg_snapshot_dump(vault: Path | None, dest: Path) -> int:
    """Dump VAULT's graph into a fresh logical snapshot directory at DEST.

    Runs offline under the same single-writer lease guard as ``kg reindex``
    (D-30): a held lease (the daemon's or another command's) exits 5 with the
    holder's pid and what to do instead. Reads the raw graph (bootstrapping it exactly as ``kg reindex``
    does), the graph identity, and ``okto-neuron.yaml``'s embedding/packs — the
    manifest never records the vault's absolute path (see
    ``store/snapshot.py``'s module docstring and ``ORIGIN_SOURCES_PREFIX``).
    """
    from okto_neuron.config import VaultConfig

    vault_path = _resolve_rebuild_vault(vault)
    _ensure_vault_directory(vault_path)
    with acquire_rebuild_lock(vault_path, operation="snapshot dump"):
        _close_live_graph_handles(vault_path)
        # M4 spec §2 item 3 (registry-driven open, supersedes M3's OQ7
        # note): a plain ``LadybugStore(vault_path)`` degrades to exactly
        # ``store/vault.py``'s own ``_open_graph_store`` ladybug branch —
        # reusing it directly keeps Ladybug byte-identical while making a
        # ``grafx``-pinned vault open through its own registered class
        # instead of a hardcoded Ladybug literal.
        from okto_neuron.store.vault import _open_graph_store, _read_pinned_backends

        graph_backend, _index_backend, storage_config = _read_pinned_backends(vault_path)
        store = _open_graph_store(vault_path, graph_backend, storage_config)
        try:
            if graph_backend == "ladybug":
                identity = schema.read_graph_identity_path(vault_path / _GRAPH_FILE)
            else:
                # No raw on-disk identity reader for a non-Ladybug backend;
                # ``store.generation()`` (protocol-generic) already gives the
                # same value ``_open_grafx_staged_store`` trusts elsewhere —
                # the contract version is always the current constant a
                # store bootstraps against (never independently observable
                # off the store itself, matching that same call site).
                identity = schema.GraphIdentity(
                    store.generation(), schema.CURRENT_IDENTITY_CONTRACT_VERSION
                )
            cfg = VaultConfig.load(vault_path)
            embedding_dim = int(cfg.embedding.dimension)
            embedding = {
                "provider": cfg.embedding.provider,
                "model": cfg.embedding.model,
                "dimension": embedding_dim,
            }
            sources_dir = vault_path / _MARGINALIA_DIR / "sources"
            manifest = snapshot.dump(
                store,
                Path(dest),
                vault_id=cfg.vault_id or vault_path.name,
                origin_backend=graph_backend,
                origin_identity=(identity.graph_generation, identity.identity_contract_version),
                embedding=embedding,
                packs=list(cfg.packs),
                sources_dir=sources_dir if sources_dir.is_dir() else None,
                marginalia_version=__version__,
                schema_version=schema.CURRENT_SCHEMA_VERSION,
                embedding_dim=embedding_dim,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a store error
            click.echo(f"error: {exc}", err=True)
            return 1
        finally:
            store.close()
            VaultConnection.close_vault(vault_path)

    click.echo(
        f"nodes={manifest.node_count} edges={manifest.edge_count} "
        f"embedded={manifest.embedded_count} sources={manifest.source_file_count} "
        f"manifest={Path(dest) / 'manifest.json'}"
    )
    return 0


def kg_snapshot_verify(src: Path) -> int:
    """Recompute every checksum/count in the snapshot at SRC and report problems."""
    report = snapshot.verify(Path(src))
    if report.ok:
        manifest = report.manifest
        assert manifest is not None  # guaranteed by verify() when ok
        click.echo(
            f"ok nodes={report.node_count} edges={report.edge_count} "
            f"embedded={report.embedded_count} vault_id={manifest.vault_id}"
        )
        return 0
    for problem in report.problems:
        click.echo(problem, err=True)
    click.echo("verify failed", err=True)
    return 1


def _write_snapshot_vault_config(
    vault_path: Path,
    manifest: "snapshot.SnapshotManifest",
    *,
    storage_uri: str | None = None,
    storage_credential_env: str | None = None,
    storage_database: str | None = None,
    storage_allow_remote: bool = False,
) -> None:
    """Write ``okto-neuron.yaml`` for a vault restored from a snapshot.

    Mirrors ``Vault._write_config``'s shape (``vault.py:611-637``), but that
    classmethod hardcodes ``model="BAAI/bge-small-en-v1.5"`` and has no
    ``dimension`` key — a restored vault must instead bootstrap at the
    snapshot's own recorded embedding width and model, so the shape is
    written out directly rather than reused. ``storage.backend`` comes from
    the manifest's own ``origin_backend`` (M3 spec section 2.6/site table),
    not a hardcoded literal — a snapshot dumped from a non-Ladybug backend
    restores pinned to that same backend, not silently to Ladybug.

    A snapshot never carries live connection secrets (a portable dump has no
    ``uri``/credentials to replay), so a backend that requires one to even
    open a store (currently only ``neo4j`` — see ``Neo4jStorageConfig``) needs
    it re-supplied by the caller at load time, the same optional
    ``storage_uri``/``storage_credential_env``/``storage_database`` triple
    ``kg_init`` already accepts for a fresh ``--backend neo4j`` vault
    (``_write_kg_init_vault_config`` above) — restoring into the SAME server
    the snapshot was dumped from is the expected use, not a new server.
    """
    storage: dict[str, object] = {"backend": manifest.origin_backend}
    if manifest.origin_backend in ("ladybug", "grafx"):
        storage["reason"] = None
    if storage_uri is not None:
        storage["uri"] = storage_uri
    if storage_credential_env is not None:
        storage["credential_env"] = storage_credential_env
    if storage_database is not None:
        storage["database"] = storage_database
    if storage_allow_remote:
        storage["allow_remote"] = True
    if manifest.origin_backend == "neo4j":
        import uuid

        storage["vault_id"] = uuid.uuid4().hex
    config = {
        "marginalia_yaml_version": 1,
        "vault_id": vault_path.name,
        "federation_opt_in": False,
        "packs": list(manifest.packs),
        "embedding": {
            "provider": manifest.embedding.get("provider"),
            "model": manifest.embedding.get("model"),
            "dimension": manifest.embedding.get("dimension"),
        },
        "storage": storage,
    }
    config_path = vault_config_path(vault_path)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    os.chmod(config_path, 0o644)


def kg_snapshot_load(
    src: Path,
    vault: Path,
    *,
    skip_embeddings: bool = False,
    storage_uri: str | None = None,
    storage_credential_env: str | None = None,
    storage_database: str | None = None,
    storage_allow_remote: bool = False,
) -> int:
    """Load the snapshot at SRC into a fresh vault at VAULT.

    VAULT must not exist or must be an empty directory. Writes
    ``okto-neuron.yaml`` first (so ``_open_vault`` bootstraps the graph at the
    snapshot's own embedding width instead of a default), opens through
    ``store.vault._open_vault`` (scaffolds ``notes/``/``refs/``/``.marginalia/``
    and returns the ``IndexedStore``, so every node also upserts the index),
    then replays the snapshot via :func:`okto_neuron.store.snapshot.load`.
    Leaves nothing behind on failure: VAULT is removed only if this command
    is the one that created it.
    """
    src_path = Path(src)
    vault_path = Path(vault)

    report = snapshot.verify(src_path)
    if not report.ok:
        for problem in report.problems:
            click.echo(problem, err=True)
        click.echo("verify failed", err=True)
        return 1
    manifest = report.manifest
    assert manifest is not None  # guaranteed by verify() when ok

    if vault_path.exists():
        if not vault_path.is_dir():
            click.echo(f"error: {vault_path} exists and is not a directory", err=True)
            return 1
        if any(vault_path.iterdir()):
            click.echo(f"error: {vault_path} exists and is not empty", err=True)
            return 1
        created_vault = False
    else:
        created_vault = True

    # A new path: nobody else holds it, so this takes the lease instead of refusing.
    with vault_writer(vault_path, "snapshot load"):
        try:
            vault_path.mkdir(parents=True, exist_ok=True)
            _write_snapshot_vault_config(
                vault_path,
                manifest,
                storage_uri=storage_uri,
                storage_credential_env=storage_credential_env,
                storage_database=storage_database,
                storage_allow_remote=storage_allow_remote,
            )
            store = _open_vault(vault_path)
            try:
                load_report = snapshot.load(
                    store,
                    src_path,
                    skip_embeddings=skip_embeddings,
                    sources_dest=vault_path / _MARGINALIA_DIR / "sources",
                )
                from okto_neuron.store.index import compute_graph_generation

                generation = compute_graph_generation(store.graph)
                in_sync = store.index.generation() == generation
            finally:
                store.close()
                VaultConnection.close_vault(vault_path)
        except Exception as exc:  # noqa: BLE001 — surfaced to the caller as a store error
            click.echo(f"error: {exc}", err=True)
            if created_vault:
                shutil.rmtree(vault_path, ignore_errors=True)
            return 1

        click.echo(
            f"nodes={load_report.nodes_written} edges={load_report.edges_written} "
            f"embeddings={load_report.embeddings_applied} sources={load_report.sources_copied} "
            f"skipped_embeddings={load_report.skipped_embeddings}"
        )
        stamp = generation[:12]
        click.echo(
            f"index up to date (generation {stamp})"
            if in_sync
            else f"index generation mismatch (generation {stamp})"
        )
        return 0


__all__ = [
    "kg_init",
    "kg_rebuild",
    "kg_reembed",
    "kg_reconcile_propose",
    "kg_reconcile_apply",
    "kg_reconcile_review_list",
    "kg_reconcile_review_confirm",
    "kg_reconcile_review_reject",
    "kg_reconcile_heal",
    "kg_snapshot_dump",
    "kg_snapshot_verify",
    "kg_snapshot_load",
]
