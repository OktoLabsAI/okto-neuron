"""Vault opening primitive for Ladybug-backed Okto Neuron stores."""

from __future__ import annotations

import inspect
import logging
import os
import shutil
import threading
import time
from pathlib import Path

import yaml

from okto_neuron._compat import vault_config_path
from okto_neuron.config._vault import IndexConfig, VaultConfig
from okto_neuron.errors import (
    OktoNeuronError,
    VaultNotFoundError,
    VaultPathNotADirectory,
    VaultPathNotWritable,
)
from okto_neuron.store._bootstrap import (
    bootstrap_vault_graph,
    reset_bootstrap_cache_for_tests,
)
from okto_neuron.store.index import compute_graph_generation, reindex_all
from okto_neuron.store.index.indexed import IndexedStore
from okto_neuron.store.index.registry import resolve_index_backend
from okto_neuron.store.ladybug import LadybugStore, VaultConnection
from okto_neuron.store.registry import resolve_graph_backend

_LOG = logging.getLogger(__name__)

_DIR_MODE = 0o755
_STORE_CACHE: dict[Path, IndexedStore] = {}
_CACHE_LOCK = threading.Lock()


def _open_vault(path: Path | str) -> IndexedStore:
    """Open a vault, scaffolding it on first use.

    Registry-driven since M3 (spec S2.2/S2.5): reads the graph/index backend
    names pinned in ``okto-neuron.yaml`` (a vault with no ``storage``/``index``
    block pins ``ladybug``/the default index engine — the pin's "absence
    means ladybug" rule), resolves each through its registry, and constructs.
    Ladybug's own construction (scaffold, ``bootstrap_vault_graph``,
    ``LadybugStore(...)``) is unchanged byte-for-byte, reached via
    ``resolve_graph_backend("ladybug")`` — zero behavior change for the
    existing default.
    """
    vault_path = Path(path).expanduser().resolve(strict=False)
    cached = _STORE_CACHE.get(vault_path)
    if cached is not None and not cached.is_closed:
        return cached

    with _CACHE_LOCK:
        cached = _STORE_CACHE.get(vault_path)
        if cached is not None and not cached.is_closed:
            return cached
        if cached is not None:
            _STORE_CACHE.pop(vault_path, None)

        _scaffold_vault(vault_path)
        _write_default_config_if_absent(vault_path)
        graph_backend, index_backend, storage_config = _read_pinned_backends(vault_path)
        store = _open_graph_store(vault_path, graph_backend, storage_config)
        indexed = IndexedStore(store, _open_index(vault_path, store, index_backend))
        _STORE_CACHE[vault_path] = indexed
        return indexed


def _read_pinned_backends(vault_path: Path) -> tuple[str, str, object | None]:
    """Read the graph/index backend names pinned in ``okto-neuron.yaml``.

    Only called after :func:`_write_default_config_if_absent` has run, so the
    file is always present here. A vault with no ``storage``/``index`` block
    (the M3 omit-when-unset default, or any pre-M3 vault) pins
    ``"ladybug"``/the default index engine — matching pre-M3 behavior exactly
    (M3 spec S2.1, S2.5).
    """
    config = VaultConfig.load(vault_path)
    storage = config.storage
    graph_backend = storage.backend if storage is not None else "ladybug"
    index_backend = config.index.backend if config.index is not None else IndexConfig().backend
    return graph_backend, index_backend, storage


def _open_graph_store(vault_path: Path, backend_name: str, storage_config: object | None) -> object:
    """Resolve and construct the pinned ``GraphStore`` backend."""
    graph_cls = resolve_graph_backend(backend_name)
    if backend_name == "ladybug":
        # Pre-M3 behavior, unchanged: bootstrap first so the handle is cached
        # under store/_bootstrap.py's own key before the store wraps it.
        graph_handle = bootstrap_vault_graph(vault_path)
        return LadybugStore(vault_path, graph_handle=graph_handle)
    return _construct_backend(graph_cls, vault_path, storage_config)


def _open_index(vault_path: Path, store: object, backend_name: str) -> object:
    """Open the pinned index backend for a vault, rebuilding it when it lags the graph.

    The stamp covers every score-bearing field of every node (M1 spec part 2
    section 7); a mismatch means a write bypassed the index (kg rebuild, reembed,
    heal, a swap) and the cache is rebuilt from the graph with no LLM or embedding
    calls.
    """
    index_cls = resolve_index_backend(backend_name)
    index = _construct_backend(index_cls, vault_path, None)
    expected = compute_graph_generation(store)
    if index.generation() != expected:
        started = time.perf_counter()
        reindex_all(store, index)
        _LOG.info(
            "index rebuilt for %s: %d docs in %.2fs",
            vault_path,
            index.stats().doc_count,
            time.perf_counter() - started,
        )
    return index


def _construct_backend(cls: type, vault_path: Path, config: object | None) -> object:
    """Construct a registry-resolved backend class.

    Prefers a documented ``from_vault(vault_path, config)`` classmethod when
    the class exposes one (M3 spec S2.6/S2.7 — a backend whose constructor
    needs more than a bare path: credentials, endpoint, etc.); otherwise
    introspects the constructor's own arity so a backend with no
    vault-path parameter (e.g. the M3 registry contract-suite's stub
    package, whose constructor takes none) is still constructed correctly
    instead of raising a spurious ``TypeError`` (D-47).
    """
    from_vault = getattr(cls, "from_vault", None)
    if callable(from_vault):
        return from_vault(vault_path, config)
    try:
        params = inspect.signature(cls).parameters.values()
    except (TypeError, ValueError):
        params = ()
    accepts_positional = any(
        param.kind in (param.POSITIONAL_ONLY, param.POSITIONAL_OR_KEYWORD) and param.default is param.empty
        for param in params
    )
    return cls(vault_path) if accepts_positional else cls()


def _scaffold_vault(vault_path: Path) -> None:
    if vault_path.exists() and not vault_path.is_dir():
        raise VaultPathNotADirectory(vault_path)

    _ensure_parent_writable(vault_path)
    try:
        vault_path.mkdir(parents=True, exist_ok=True)
    except NotADirectoryError as exc:
        raise VaultPathNotADirectory(vault_path, cause=exc) from exc
    except PermissionError as exc:
        raise VaultPathNotWritable(vault_path, cause=exc) from exc
    except OSError as exc:
        raise VaultPathNotWritable(vault_path, cause=exc) from exc

    for directory in (
        vault_path,
        vault_path / "notes",
        vault_path / "refs",
        vault_path / ".marginalia",
    ):
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, _DIR_MODE)


def _ensure_parent_writable(vault_path: Path) -> None:
    candidate = vault_path if vault_path.exists() else vault_path.parent
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    if not candidate.exists() or not candidate.is_dir():
        raise VaultPathNotWritable(vault_path)
    if not os.access(candidate, os.W_OK | os.X_OK):
        raise VaultPathNotWritable(vault_path)


def _write_default_config_if_absent(vault_path: Path) -> None:
    config_path = vault_config_path(vault_path)
    if config_path.exists():
        return
    config = VaultConfig.default().model_dump(mode="json", exclude_none=True)
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    os.chmod(config_path, 0o644)


def wipe_vault(vault_path: Path | str, *, keep_config: bool = True) -> None:
    """Reset a vault to empty, then re-scaffold a fresh empty graph.

    The markdown vault is the trust root, so "start fresh" deletes user-facing
    content (``notes/``, ``refs/``), the graph, and all vault-local derived app
    state under ``.marginalia/`` (ingested sources, queue histories, curation
    sidecars, re-embed/rebuild progress, reconcile/authority metadata), leaving
    a clean empty graph. Shared single source of truth for the CLI
    ``kg init --wipe`` and the API ``POST /api/v1/reset``.

    Path-safety: refuses unless ``okto-neuron.yaml`` is present — a missing config
    means this is not a vault, and we never delete a directory we did not create.
    Callers wanting "wipe-or-init" must check for the vault before calling.

    ``keep_config`` gates config *writing*, not just deletion. When True
    (default) an existing ``okto-neuron.yaml`` is preserved untouched, so the
    embedder/LLM/packs survive a reset (the UI button reuses the same config).
    When False the config is deleted and NOT rewritten — on return
    ``okto-neuron.yaml`` is absent, so the caller can reinitialize it (the CLI
    ``--wipe`` with explicit ``--packs``/``--embedder`` reapplies via
    ``Vault.init``, whose guard requires no config to be present).
    """
    resolved = Path(vault_path).expanduser().resolve(strict=False)
    config_path = vault_config_path(resolved)
    if not config_path.exists():
        raise VaultNotFoundError(
            resolved,
            message="not a vault (okto-neuron.yaml absent); refusing to wipe",
        )

    # Read the pinned backend before anything is deleted: keep_config=False
    # removes okto-neuron.yaml below, and re-scaffolding must still target the
    # backend this vault was pinned to, not silently fall back to ladybug.
    graph_backend, _index_backend, storage_config = _read_pinned_backends(resolved)

    # Release any live graph handles before deleting the on-disk files, so the
    # fresh open rebuilds from scratch. Mirrors the CLI's close sequence.
    with _CACHE_LOCK:
        cached = _STORE_CACHE.pop(resolved, None)
    if cached is not None:
        cached.close()
    VaultConnection.close_vault(resolved)
    reset_bootstrap_cache_for_tests(resolved)

    # Delete derived graph (+ WAL/shadow siblings, backend-dispatched), all
    # vault-local app state, and user content. Best-effort: a reset must not
    # half-fail on a stray locked file.
    _erase_backend_storage(resolved, graph_backend, storage_config)
    _remove_path(resolved / ".marginalia")
    _remove_path(resolved / "notes")
    _remove_path(resolved / "refs")
    if not keep_config:
        _remove_path(config_path)

    # Re-scaffold an empty vault with a fresh empty graph, through the same
    # registry-driven construction every other open uses.
    _scaffold_vault(resolved)
    if keep_config:
        _write_default_config_if_absent(resolved)
    _open_graph_store(resolved, graph_backend, storage_config).close()


def _erase_backend_storage(
    vault_path: Path, backend_name: str, storage_config: object | None = None
) -> None:
    """Delete one backend's on-disk graph storage and its swap sidecars.

    Backend-dispatched (M4): pre-M4 ``wipe_vault`` hardcoded Ladybug's
    ``graph.lbug*`` glob, so wiping a non-Ladybug-pinned vault silently left
    its graph storage on disk untouched. Ladybug's storage is a single file
    with WAL/backup siblings; Grafx's is a directory (``graph.grafx``) with
    ``.bak``/``.discard`` siblings from an interrupted
    :class:`~okto_neuron.store.staging.GrafxStaging` swap (`store/staging.py`)
    — both glob patterns below match every sibling that starts with the live
    graph's own name. A backend with no registered erase path fails closed
    rather than silently leaving storage behind.
    """
    if backend_name == "ladybug":
        for graph_sibling in sorted(vault_path.glob("graph.lbug*")):
            _remove_path(graph_sibling)
        return
    if backend_name == "grafx":
        for graph_sibling in sorted(vault_path.glob("graph.grafx*")):
            _remove_path(graph_sibling)
        return
    if backend_name == "neo4j":
        from okto_neuron.store.neo4j import Neo4jStore

        store = Neo4jStore(vault_path, config=storage_config)
        try:
            store.wipe()
        finally:
            store.close()
        _remove_path(vault_path / ".neo4j-generation")
        return
    raise OktoNeuronError(
        f"no storage-erase path registered for graph backend {backend_name!r}; "
        "wipe_vault cannot safely delete this vault's graph storage",
        vault_path=vault_path,
    )


def _remove_path(target: Path) -> None:
    """Best-effort delete of a file, symlink, or directory tree."""
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        else:
            target.unlink(missing_ok=True)
    except OSError:
        pass


__all__ = ["_open_vault", "wipe_vault"]
