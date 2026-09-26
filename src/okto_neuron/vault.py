"""Synchronous public Vault API."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import logging
import os
from os import PathLike
from pathlib import Path
import re
import threading
import time
from typing import ClassVar, Literal
from urllib.parse import urlparse
from urllib.request import url2pathname

import yaml

from okto_neuron._compat import getenv as _compat_getenv
from okto_neuron._compat import (
    JSONLD_VOCABULARY_IRI,
    JSONLD_VOCABULARY_PREFIX,
    vault_config_path,
)
from okto_neuron.errors import (
    ExportError,
    FileNotUnderVaultError,
    IngestError,
    InvalidVaultConfigError,
    OktoNeuronError,
    QueryError,
    VaultAlreadyExistsError,
    VaultClosedError,
    VaultError,
    VaultLockedError,
    VaultLockHeld,
    VaultNotFoundError,
)
from okto_neuron.models import Document, ExportScope, IngestResult, Node, Provenance, QueryHit

_LOGGER = logging.getLogger(__name__)
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_SCHEMES = {"file", "http", "https"}
_INTEGRITY_LOCK_INIT = threading.Lock()
_MISSING = object()


@dataclass(frozen=True)
class StorageInfo:
    backend: str
    reason: str | None = None


class VaultBackendMismatch(VaultError):
    """A create/re-init call requested a different backend than the one
    already pinned in this vault's ``okto-neuron.yaml`` (M3 spec S2.5 — the
    pin holds at open time via the registry-driven
    ``store/vault.py::_open_vault``; this is the create-time refusal,
    replacing ``Vault.init``'s previous embedding_provider-only silent-reopen
    quirk for the backend dimension)."""

    default_message: ClassVar[str] = "vault is pinned to a different backend"


def _check_backend_pin(root: Path, backend: str) -> None:
    """Refuse creating/reopening ``root`` under a different backend than the
    one already pinned in its ``okto-neuron.yaml``, when one exists.

    A config that can't be read or parsed here is left to the caller's own
    unconditional path (``VaultAlreadyExistsError``, or ``init``'s existing
    embedding_provider reopen) — diagnosing a malformed config is not this
    guard's job; it only judges a well-formed pin conflict.
    """
    from okto_neuron.config import VaultConfig

    try:
        pinned = VaultConfig.load(root).storage
    except OktoNeuronError:
        return
    # Deliberately still the literal "ladybug", not
    # ``config._vault.DEFAULT_NEW_VAULT_BACKEND`` -- an existing
    # ``marginalia.yaml`` with no ``storage`` key is a legacy vault that
    # predates any backend being pinnable, and legacy always means ladybug,
    # never today's new-vault default.
    pinned_backend = pinned.backend if pinned is not None else "ladybug"
    if pinned_backend != backend:
        raise VaultBackendMismatch(
            root,
            message=(
                f"vault at {root} is pinned to backend {pinned_backend!r}; "
                f"cannot create or reopen it as backend {backend!r}"
            ),
        )


class Vault:
    def __init__(
        self,
        path: Path,
        store: object,
        *,
        allow_external_sources: bool = False,
        embedder: object | None = None,
    ) -> None:
        self.path = path
        self.root = path
        self.store = store
        self.storage_info = StorageInfo(
            backend="json-fallback",
            reason="compatibility metadata; public API is backed by Ladybug store",
        )
        self._allow_external_sources = allow_external_sources
        self._embedder = embedder
        self._closed = False
        with _INTEGRITY_LOCK_INIT:
            integrity_lock = getattr(store, "_integrity_sync_lock", None)
            integrity_local = getattr(store, "_integrity_guard_local", None)
            try:
                if integrity_lock is None:
                    integrity_lock = threading.RLock()
                    setattr(store, "_integrity_sync_lock", integrity_lock)
                if integrity_local is None:
                    integrity_local = threading.local()
                    setattr(store, "_integrity_guard_local", integrity_local)
            except (AttributeError, TypeError) as exc:
                is_live_store = (
                    getattr(store, "vault_path", _MISSING) is not _MISSING
                    or getattr(store, "_graph_handle", _MISSING) is not _MISSING
                )
                if is_live_store:
                    raise TypeError(
                        "live graph store must support shared integrity synchronization"
                    ) from exc
                # Lightweight compatibility objects used only for configuration
                # validation do not write a live graph. A per-Vault boundary is
                # sufficient for those objects.
                integrity_lock = threading.RLock()
                integrity_local = threading.local()
        self._integrity_sync_lock = integrity_lock
        self._integrity_guard_local = integrity_local

    @property
    def recovered_from_corruption(self) -> bool:
        """True when this vault's graph was quarantined + recovered on the most
        recent open because the on-disk Ladybug files were corrupt (a ``kill -9``
        mid-write leaves a torn graph/WAL — see ``store/_bootstrap.py``).

        Recovery has two modes (see ``recovery_mode``): the common torn-WAL case
        recovers the last good checkpoint (claims kept); a torn main file falls back
        to an EMPTY graph. Either way this stays ``True`` — a hard kill happened and
        the operator should know.

        Surfaced on ``/health`` (Task 4) so a daemon that recovered after a hard kill
        reports ``status: degraded`` instead of ``ok``; the operator re-ingests recent
        sources or reconstructs with ``kg rebuild``. Backed by the store's own
        ``recovery_status()`` (M3 spec S2.4) instead of reflecting into
        ``_graph_handle`` directly; defensive ``getattr`` so a store without the
        method (test doubles) reads ``False``."""
        recovery_status = getattr(self.store, "recovery_status", None)
        if not callable(recovery_status):
            return False
        return bool(recovery_status().recovered)

    @property
    def recovery_mode(self) -> str | None:
        """How a corruption recovery resolved: ``"checkpoint"`` (last good checkpoint
        recovered, claims kept — only writes since that checkpoint were lost),
        ``"empty"`` (the checkpoint was itself torn, started blank), or ``None`` (no
        recovery on the most recent open)."""
        handle = getattr(self.store, "_graph_handle", None)
        return getattr(handle, "recovered_mode", None)

    @property
    def embedder(self) -> object:
        """The vault's configured embedding provider (lazily resolved).

        Resolved from ``okto-neuron.yaml`` (``embedding.provider`` / ``model``) so
        the query-side embedding and any stored embeddings come from the SAME
        provider — a model/dim mismatch scores silently wrong otherwise.
        """
        if self._embedder is None:
            embedder = self._resolve_embedder()
            self._ensure_embedding_compatible(embedder)
            self._embedder = embedder
        else:
            self._ensure_embedding_compatible(self._embedder)
        return self._embedder

    @property
    def index(self):
        """The IndexStore paired with this vault store, or None when the store carries none."""
        return getattr(self.store, "index", None)

    def _ensure_embedding_compatible(self, embedder: object) -> None:
        """Fail before query/ingest when hot config no longer matches the graph."""
        handle = getattr(self.store, "_graph_handle", None)
        stored_dim = getattr(handle, "embedding_dim", None)
        configured_dim = getattr(embedder, "dim", None)
        if not isinstance(stored_dim, int) or not isinstance(configured_dim, int):
            return
        if stored_dim == configured_dim:
            return

        from okto_neuron.errors import EmbeddingDimMismatch

        raise EmbeddingDimMismatch(
            self.path / "graph.lbug",
            stored_dim=stored_dim,
            configured_dim=configured_dim,
            vault_path=self.path,
        )

    def invalidate_runtime_caches(self) -> None:
        """Drop runtime caches derived from ``okto-neuron.yaml``.

        Called after a successful config write so the NEXT use re-resolves the
        embedder from the fresh config instead of the instance cached at first
        access. Deliberately narrow: it does not touch the store handle, and an
        in-flight ``remember``/query holding a reference to the old embedder
        simply finishes on the old instance — only the next construction sees
        the new config. It also does NOT bypass the embedding-dimension guard:
        the next resolution checks the fresh configured width against the open
        graph handle, so a re-embed-required change fails before query/ingest
        without needing a daemon restart.
        """
        self._embedder = None

    def _resolve_embedder(self) -> object:
        from okto_neuron.config import VaultConfig
        from okto_neuron.embed import get_provider

        cfg = VaultConfig.load(self.path).embedding.resolved_provider()
        # An explicit provider is part of the vault's vector-space identity.
        # Falling back to FastEmbed on a typo, missing dependency, bad key, or
        # outage silently mixes embedding spaces. Fail loud; inherited vaults
        # resolve through the same typed config path used by graph bootstrap.
        return get_provider(cfg)

    @classmethod
    def open(cls, path: str | PathLike[str]) -> "Vault":
        root = Path(path).expanduser().resolve(strict=False)
        if not root.exists():
            raise VaultNotFoundError(root)

        config = cls._read_config_if_present(root)
        store = cls._open_store(root)
        if config is None:
            config = cls._read_config_if_present(root) or {}
        return cls(
            root,
            store,
            allow_external_sources=bool(config.get("compat_allow_external_sources", False)),
        )

    @classmethod
    def init(
        cls,
        path: str | PathLike[str],
        *,
        packs: tuple[str, ...] | list[str] = ("core",),
        embedder: str = "default",
        embedding_provider: str | None = None,
        # Python-API-level default -- deliberately left "ladybug", NOT
        # ``config._vault.DEFAULT_NEW_VAULT_BACKEND`` ("grafx"). Every CLI
        # vault-creation entry point (``okto-neuron init``, ``vault create``,
        # ``onboard``) now passes ``backend=DEFAULT_NEW_VAULT_BACKEND``
        # explicitly, so this bare default only matters for a direct
        # library/test caller that omits ``backend`` -- and the existing
        # test suite (companion/, consolidate/, server/, vault/, ...) calls
        # ``Vault.init``/``Vault.scaffold`` this way expecting Ladybug.
        backend: str = "ladybug",
        storage_uri: str | None = None,
        storage_credential_env: str | None = None,
        storage_database: str | None = None,
        storage_allow_remote: bool = False,
    ) -> "Vault":
        root = Path(path).expanduser().resolve(strict=False)
        config_path = vault_config_path(root)
        if config_path.exists():
            _check_backend_pin(root, backend)
            if embedding_provider is not None:
                return cls.open(root)
            raise VaultAlreadyExistsError(root)

        legacy_external_sources = embedding_provider is not None
        if embedding_provider is not None:
            embedder = embedding_provider

        cls.scaffold(
            root,
            packs=packs,
            embedder=embedder,
            allow_external_sources=legacy_external_sources,
            backend=backend,
            storage_uri=storage_uri,
            storage_credential_env=storage_credential_env,
            storage_database=storage_database,
            storage_allow_remote=storage_allow_remote,
        )
        store = cls._open_store(root)
        os.chmod(root, 0o700)
        return cls(
            root,
            store,
            allow_external_sources=legacy_external_sources,
        )

    @classmethod
    def scaffold(
        cls,
        path: str | PathLike[str],
        *,
        packs: tuple[str, ...] | list[str] = ("core",),
        embedder: str = "default",
        allow_external_sources: bool = False,
        backend: str = "ladybug",  # see the matching comment on Vault.init above
        storage_uri: str | None = None,
        storage_credential_env: str | None = None,
        storage_database: str | None = None,
        storage_allow_remote: bool = False,
    ) -> Path:
        """Create the vault directory and config without opening the graph."""
        root = Path(path).expanduser().resolve(strict=False)
        if (vault_config_path(root)).exists():
            _check_backend_pin(root, backend)
            raise VaultAlreadyExistsError(root)
        for directory in (root, root / "notes", root / "refs", root / ".marginalia"):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
        cls._write_config(
            root,
            packs=tuple(packs),
            embedder=embedder,
            allow_external_sources=allow_external_sources,
            backend=backend,
            storage_uri=storage_uri,
            storage_credential_env=storage_credential_env,
            storage_database=storage_database,
            storage_allow_remote=storage_allow_remote,
        )
        return root

    def add(self, source: str | PathLike[str]) -> Document:
        self._ensure_open()
        local_path, delegated_source = self._validate_source(source)
        try:
            from okto_neuron.ingest import ingest_document
        except ImportError as exc:
            raise IngestError(
                message="ingest module not available in this build",
                vault_path=self.path,
                cause=exc,
            ) from exc

        with self._integrity_write_guard():
            try:
                result = ingest_document(self.store, delegated_source, vault_root=self.path)
            except OktoNeuronError:
                raise
            except Exception as exc:
                raise IngestError(
                    local_path,
                    vault_path=self.path,
                    message=f"{type(exc).__name__}: {exc}",
                    cause=exc,
                ) from exc
        return _document_from_ingest_result(result, local_path)

    @contextmanager
    def _integrity_write_guard(self):
        """Guard a live-graph write; nested calls and rebuild staging are no-ops."""

        store_path = getattr(self.store, "vault_path", None)
        if store_path is None:
            yield
            return
        if Path(store_path).resolve(strict=False) != self.path.resolve(strict=False):
            yield
            return
        from okto_neuron.store.integrity_state import guarded_store_write

        with self._integrity_sync_lock:
            depth = int(getattr(self._integrity_guard_local, "depth", 0))
            if depth:
                yield
                return
            self._integrity_guard_local.depth = depth + 1
            try:
                with guarded_store_write(self.path, self.store):
                    yield
            finally:
                self._integrity_guard_local.depth = depth

    @contextmanager
    def _integrity_scan_guard(self):
        """Serialize one audit+scan snapshot with every in-process live write."""

        with self._integrity_sync_lock:
            yield

    def _integrity_write_guard_active(self) -> bool:
        """Return whether this thread already owns a live-write audit boundary."""

        return bool(getattr(self._integrity_guard_local, "depth", 0))

    def query(
        self,
        text: str,
        *,
        k: int = 5,
        type: str | None = None,
        with_drift: bool = False,
        expand_context: bool = True,
        seed_diversity: bool = False,
        seed_subject_cap: int | None = None,
        seed_entity_min: int | None = None,
        seed_rel_min: int | None = None,
        seed_scalar_max: int | None = None,
    ) -> list[QueryHit]:
        """``seed_diversity`` (Fix B, task-12) is passed ``True`` by the
        subgraph ask path only — the default recall/query output stays
        byte-identical (deterministic-floor CI gate)."""
        return self._query_impl(
            text,
            k=k,
            type=type,
            with_drift=with_drift,
            expand_context=expand_context,
            seed_diversity=seed_diversity,
            seed_subject_cap=seed_subject_cap,
            seed_entity_min=seed_entity_min,
            seed_rel_min=seed_rel_min,
            seed_scalar_max=seed_scalar_max,
        )

    def query_with_metrics(
        self,
        text: str,
        *,
        k: int = 5,
        type: str | None = None,
        with_drift: bool = False,
        expand_context: bool = True,
        seed_diversity: bool = False,
        seed_subject_cap: int | None = None,
        seed_entity_min: int | None = None,
        seed_rel_min: int | None = None,
        seed_scalar_max: int | None = None,
    ) -> tuple[list[QueryHit], dict[str, object]]:
        """Return ordinary recall hits plus ADR 0040 cost evidence.

        This path performs exactly the same query as :meth:`query`. The metrics
        make the completion-free contract falsifiable without moving any model
        work into recall: one query embedding is allowed; completion/judge calls
        and generated tokens are pinned to zero.
        """

        from okto_neuron._internal.completion_guard import prohibit_completions

        metrics: dict[str, object] = {"schema_version": "recall_cost.v1"}
        started = time.perf_counter()
        with prohibit_completions("ordinary recall") as completion_probe:
            hits = self._query_impl(
                text,
                k=k,
                type=type,
                with_drift=with_drift,
                expand_context=expand_context,
                seed_diversity=seed_diversity,
                seed_subject_cap=seed_subject_cap,
                seed_entity_min=seed_entity_min,
                seed_rel_min=seed_rel_min,
                seed_scalar_max=seed_scalar_max,
                metrics=metrics,
            )
        metrics.update(
            {
                "measurement_status": "measured",
                "completion_calls": completion_probe.attempted_calls,
                "generated_tokens": 0,
                "total_latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "retrieved_results": len(hits),
                "retrieved_bytes": sum(
                    max(0, hit.provenance.byte_end - hit.provenance.byte_start)
                    + sum(max(0, span.byte_end - span.byte_start) for span in hit.context_spans)
                    for hit in hits
                ),
                "completion_free": completion_probe.attempted_calls == 0,
            }
        )
        return hits, metrics

    def _query_impl(
        self,
        text: str,
        *,
        k: int,
        type: str | None,
        with_drift: bool,
        expand_context: bool,
        seed_diversity: bool,
        seed_subject_cap: int | None,
        seed_entity_min: int | None,
        seed_rel_min: int | None,
        seed_scalar_max: int | None,
        metrics: dict[str, object] | None = None,
    ) -> list[QueryHit]:
        self._ensure_open()
        assert k >= 1, "k must be >= 1"
        self._validate_type_filter(type)

        raw_hits = self._search(
            text,
            k=k,
            type=type,
            seed_diversity=seed_diversity,
            seed_subject_cap=seed_subject_cap,
            seed_entity_min=seed_entity_min,
            seed_rel_min=seed_rel_min,
            seed_scalar_max=seed_scalar_max,
            metrics=metrics,
        )
        projection_started = time.perf_counter()
        hits = self._to_query_hits(raw_hits, k=k, expand_context=expand_context)
        if metrics is not None:
            metrics["deterministic_projection_latency_ms"] = round(
                (time.perf_counter() - projection_started) * 1000,
                3,
            )
        if with_drift:
            hits = self._attach_drift_best_effort(hits)
        return hits

    def query_seeds(
        self,
        text: str,
        *,
        k: int = 5,
        seed_diversity: bool = True,
        seed_subject_cap: int | None = None,
        seed_entity_min: int | None = None,
        seed_rel_min: int | None = None,
        seed_scalar_max: int | None = None,
    ) -> list[tuple[str, float]]:
        """Raw ``(node_id, score)`` seeds from ``search_claims`` BEFORE
        ``_to_query_hits`` (ADR 0011). ``search_claims`` returns up to
        ``max(k, 20)`` fused hits, so the subgraph builder seeds off the FULL
        recall, not the truncated top-``k`` QueryHits. Used by the subgraph-first
        ask/explore paths to build the ego-graph from raw store nodes + scores.

        ``seed_diversity`` defaults ``True`` here (Fix B, task-12): the ego-graph
        seed cut is diversified (fact dedup + subject cap + class quotas) so
        scalar-literal Claims cannot flood every slot. Pass ``False`` to restore
        the pre-Fix-B seed ordering (A/B attribution switch)."""
        self._ensure_open()
        assert k >= 1, "k must be >= 1"
        raw_hits = self._search(
            text,
            k=k,
            type=None,
            seed_diversity=seed_diversity,
            seed_subject_cap=seed_subject_cap,
            seed_entity_min=seed_entity_min,
            seed_rel_min=seed_rel_min,
            seed_scalar_max=seed_scalar_max,
        )
        return [(getattr(node, "id", ""), float(score)) for node, score in raw_hits]

    def get(self, node_id: str) -> Node | None:
        self._ensure_open()
        node = self.store.get_node(node_id)
        if node is None:
            return None
        return _public_node(node)

    def export(
        self,
        *,
        format: Literal["jsonld", "jsonld-star"] = "jsonld",
        stream=None,
        scope: ExportScope | None = None,
    ) -> str | None:
        self._ensure_open()
        if format not in {"jsonld", "jsonld-star"}:
            raise ExportError(message=f"unsupported export format: {format}", vault_path=self.path)
        try:
            import rdflib  # noqa: F401
        except ImportError as exc:
            raise ExportError(
                message=("rdflib is required for JSON-LD export; install okto-neuron[jsonld]"),
                vault_path=self.path,
                cause=exc,
            ) from exc

        export_scope = scope or ExportScope()
        payload = {
            "@context": dict(
                sorted(
                    {
                        "@vocab": JSONLD_VOCABULARY_IRI,
                        JSONLD_VOCABULARY_PREFIX: JSONLD_VOCABULARY_IRI,
                        "prov": "http://www.w3.org/ns/prov#",
                    }.items()
                )
            ),
            "@graph": [
                self._export_node(node, scope=export_scope)
                for node in self._scoped_nodes(export_scope)
            ],
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        if stream is not None:
            stream.write(serialized)
            return None
        return serialized

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        close = getattr(self.store, "close", None)
        if callable(close):
            close()

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, *exc):
        self.close()

    def get_provenance(self, claim_id: str) -> dict[str, object] | None:
        self._ensure_open()
        claim = self.store.get_node(claim_id)
        if not claim or getattr(claim, "type", None) != "Claim":
            return None
        block_id = str(getattr(claim, "facets", {}).get("block_id") or "")
        block = self.store.get_node(block_id) if block_id else None
        return {
            "claim": claim.model_dump(mode="json"),
            "block": block.model_dump(mode="json") if block else None,
            "edges": [
                edge.model_dump(mode="json")
                for edge in self.store.list_edges(src=claim_id)
                if edge.type.startswith("prov:")
            ],
        }

    @classmethod
    def _open_store(cls, root: Path) -> object:
        try:
            from okto_neuron._internal._open_vault import _open_vault

            return _open_vault(root)
        except ModuleNotFoundError as exc:
            if exc.name != "ladybug":
                raise
            raise VaultError(
                root,
                message=(
                    "Ladybug storage is not installed; install okto-neuron[ladybug] "
                    "to open graph-backed vaults"
                ),
                cause=exc,
            ) from exc
        except VaultLockHeld as exc:
            raise VaultLockedError(root, cause=exc) from exc

    @classmethod
    def _read_config_if_present(cls, root: Path) -> dict[str, object] | None:
        config_path = vault_config_path(root)
        if not config_path.exists():
            return None
        try:
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise InvalidVaultConfigError(config_path, vault_path=root, cause=exc) from exc
        except OSError as exc:
            raise InvalidVaultConfigError(config_path, vault_path=root, cause=exc) from exc
        if not isinstance(data, dict):
            raise InvalidVaultConfigError(
                config_path,
                vault_path=root,
                cause=TypeError("okto-neuron.yaml must contain a mapping"),
            )
        return data

    @classmethod
    def _write_config(
        cls,
        root: Path,
        *,
        packs: tuple[str, ...],
        embedder: str,
        allow_external_sources: bool,
        backend: str = "ladybug",  # see the matching comment on Vault.init above
        storage_uri: str | None = None,
        storage_credential_env: str | None = None,
        storage_database: str | None = None,
        storage_allow_remote: bool = False,
    ) -> None:
        storage: dict[str, object] = {"backend": backend}
        # "reason" is accepted only by backends whose StorageConfig variant
        # declares it (Ladybug, Grafx) -- Neo4j's is extra="forbid" with no
        # such field, so it must not be written there.
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
        config = {
            "marginalia_yaml_version": 1,
            "vault_id": root.name,
            "federation_opt_in": False,
            "packs": list(packs),
            "embedding": {
                "provider": embedder,
                "model": "BAAI/bge-small-en-v1.5",
            },
            "storage": storage,
        }
        if allow_external_sources:
            config["compat_allow_external_sources"] = True
        config_path = vault_config_path(root)
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        os.chmod(config_path, 0o644)

    def _validate_source(self, source: str | PathLike[str]) -> tuple[Path | None, str | Path]:
        if isinstance(source, PathLike):
            path = Path(source).expanduser().resolve(strict=False)
            self._ensure_under_vault(path)
            return path, path

        raw = str(source)
        parsed = urlparse(raw)
        # A Windows drive-letter path ("C:\Work\notes\demo.md") is misparsed by
        # urlparse as a URI with a single-letter scheme ("c"). Detect that shape
        # and treat it as a plain path instead of running it through the scheme
        # allowlist below — genuine unsupported schemes (ftp, etc.) are always
        # multi-letter and still rejected exactly as before.
        is_drive_letter_path = (
            len(parsed.scheme) == 1
            and parsed.scheme.isalpha()
            and re.match(r"^[A-Za-z]:[\\/]", raw) is not None
        )
        if parsed.scheme and not is_drive_letter_path:
            if parsed.scheme not in _ALLOWED_SCHEMES:
                raise IngestError(
                    message=f"unsupported source scheme: {parsed.scheme}",
                    vault_path=self.path,
                )
            if parsed.scheme == "file":
                path = Path(url2pathname(parsed.path)).expanduser().resolve(strict=False)
                self._ensure_under_vault(path)
                return path, path
            return None, raw

        path = Path(raw).expanduser().resolve(strict=False)
        self._ensure_under_vault(path)
        return path, path

    def _ensure_under_vault(self, path: Path) -> None:
        if self._allow_external_sources:
            return
        try:
            path.relative_to(self.path)
        except ValueError as exc:
            raise FileNotUnderVaultError(path, vault_path=self.path) from exc

    def _search(
        self,
        text: str,
        *,
        k: int,
        type: str | None,
        seed_diversity: bool = False,
        seed_subject_cap: int | None = None,
        seed_entity_min: int | None = None,
        seed_rel_min: int | None = None,
        seed_scalar_max: int | None = None,
        metrics: dict[str, object] | None = None,
    ) -> list[tuple[object, float]]:
        from okto_neuron.query import search_claims

        return search_claims(
            text,
            k=k,
            store=self.store,
            index=self.index,
            embedder=self.embedder,
            type=type,
            equivalence=self._equivalence_map(),
            predicate_aliases=self._predicate_alias_map(),
            seed_diversity=seed_diversity,
            seed_subject_cap=seed_subject_cap,
            seed_entity_min=seed_entity_min,
            seed_rel_min=seed_rel_min,
            seed_scalar_max=seed_scalar_max,
            metrics=metrics,
        )

    def _equivalence_map(self) -> dict[str, str] | None:
        """Option-A query-time consolidation fold (ADR 0008).

        Loads ``<vault>/.marginalia/authority/index.json`` → ``member_id ->
        canonical_id``. Loaded fresh per query with a fast missing-file path
        (returns ``None`` → the fold is a no-op → recall behavior is unchanged
        unless ``kg reconcile apply`` has written the index). Reloading per query
        avoids a stale cache masking a just-applied fold (an in-process apply →
        query must see its own write)."""
        from okto_neuron.reconcile.authority import AUTHORITY_DIRNAME, AuthorityIndex

        index_dir = self.path / ".marginalia" / AUTHORITY_DIRNAME
        if not (index_dir / "index.json").exists():
            return None
        try:
            mapping = AuthorityIndex(index_dir).equivalence_map()
        except Exception:  # noqa: BLE001 — a malformed index must not break recall
            return None
        return mapping or None

    def _predicate_alias_map(self) -> dict[str, str] | None:
        """ADR 0017 query-time predicate fold for exact-match aliases."""
        path = self.path / ".marginalia" / "predicates" / "aliases.json"
        if not path.exists():
            return None
        try:
            from okto_neuron.predicates import PredicateAliasIndex

            mapping = PredicateAliasIndex(self.path).alias_map()
        except Exception:  # noqa: BLE001 — a malformed index must not break recall
            return None
        return mapping or None

    def _to_query_hits(
        self, raw_hits: list[tuple[object, float]], *, k: int, expand_context: bool = True
    ) -> list[QueryHit]:
        if not raw_hits:
            return []
        from okto_neuron.query import build_block_index, expand_block_context

        max_score = max(score for _, score in raw_hits) or 1.0
        # ADR 0011 3.3: the same-file text-neighbour expansion is a Tier-2 depth
        # tool. The subgraph-first ask path supplies its own graph context and
        # passes expand_context=False to suppress it; the default path leaves the
        # legacy query_neighbors expansion exactly as-is.
        neighbors = self._query_neighbors() if expand_context else 0
        block_index = build_block_index(self.store) if neighbors > 0 else None
        hits: list[QueryHit] = []
        for core_node, raw_score in raw_hits[:k]:
            score = max(0.0, min(float(raw_score) / float(max_score), 1.0))
            prov = self._provenance_for_node(core_node)
            context_spans: tuple = ()
            if neighbors > 0 and prov.block_id:
                context_spans = tuple(
                    expand_block_context(
                        self.store,
                        prov.block_id,
                        k_neighbors=neighbors,
                        index=block_index,
                    )
                )
            hits.append(
                QueryHit(
                    node=_public_node(core_node),
                    score=score,
                    provenance=prov,
                    context_spans=context_spans,
                )
            )
        return hits

    def expand_hit_context_spans(self, hits: list[QueryHit]) -> list[QueryHit]:
        """T2b (Fix B, task-12): enrich already-built QueryHits with same-document
        neighbor spans (D5) at Tier-2 fallback time.

        The subgraph ask path builds its hits with ``expand_context=False`` (the
        ego-graph render supplies Tier-1 context), which also strips the
        ``context_spans`` the block-dump arm gets from IDENTICAL hits — so its
        Tier-2 fallback read a strictly poorer context. This restores parity
        without a re-query: same ``_query_neighbors()`` width resolution, same
        ``expand_block_context`` primitive, no new source paths (neighbors share
        the seed block's exact path). Hits that already carry spans, or have no
        block anchor, pass through unchanged. Pure reads; returns new QueryHit
        copies (QueryHit is frozen)."""
        self._ensure_open()
        neighbors = self._query_neighbors()
        if neighbors <= 0 or not hits:
            return list(hits)
        from okto_neuron.query import build_block_index, expand_block_context

        block_index = build_block_index(self.store)
        enriched: list[QueryHit] = []
        for hit in hits:
            block_id = hit.provenance.block_id if hit.provenance else ""
            if hit.context_spans or not block_id:
                enriched.append(hit)
                continue
            spans = tuple(
                expand_block_context(self.store, block_id, k_neighbors=neighbors, index=block_index)
            )
            enriched.append(hit.model_copy(update={"context_spans": spans}) if spans else hit)
        return enriched

    def _query_neighbors(self) -> int:
        """Block-neighbor expansion width (D5). Resolution: env
        ``OKTO_NEURON_QUERY_NEIGHBORS`` (test/run pin) -> ``query_neighbors`` in
        ``okto-neuron.yaml`` -> 0 (off). Default-off keeps recall/ask behavior
        unchanged unless explicitly enabled; negatives/garbage clamp to 0."""
        raw = _compat_getenv("OKTO_NEURON_QUERY_NEIGHBORS")
        if raw is None:
            config = self._read_config_if_present(self.path) or {}
            raw = config.get("query_neighbors") if isinstance(config, dict) else None
        try:
            return max(0, int(raw)) if raw is not None else 0
        except (TypeError, ValueError):
            return 0

    def _anchor_from_span(self, facets: dict) -> tuple[str, int, int, str] | None:
        """ADR 0003 C2: derive the byte anchor from a Claim's ``source_span``
        facet (the span survives Block deletion; the Block-node lookup does not).

        The span's ``source_path`` is vault-RELATIVE (ADR 0003 E2); resolve it
        back to an absolute path under the vault root and re-validate containment
        before exposing it for byte reads (security: re-validate joined paths).
        Returns ``(abs_path, byte_start, byte_end, content_hash)`` or ``None`` to
        fall through to the legacy block-id path (entities/legacy data)."""
        raw = facets.get("source_span")
        if not isinstance(raw, dict):
            return None
        rel = str(raw.get("source_path") or "")
        content_hash = str(raw.get("content_hash") or "")
        if not rel or not content_hash:
            return None
        try:
            root = self.path.resolve(strict=False)
            abs_path = (root / rel).resolve(strict=False)
            abs_path.relative_to(root)  # re-validate under vault root
        except (ValueError, OSError):
            return None
        return (
            str(abs_path),
            int(raw.get("byte_start") or 0),
            int(raw.get("byte_end") or 0),
            content_hash,
        )

    def _provenance_for_node(self, node: object) -> Provenance:
        facets = dict(getattr(node, "facets", {}) or {})
        node_type = str(getattr(node, "type", "") or "")
        block_id = str(facets.get("block_id") or "")
        anchor = self._anchor_from_span(facets)
        if anchor is not None:
            path, byte_start, byte_end, content_hash = anchor
        else:
            block = self.store.get_node(block_id) if block_id else None
            block_facets = dict(getattr(block, "facets", {}) or {}) if block else facets
            content_hash = (
                block_facets.get("content_hash")
                or block_facets.get("sha256")
                or facets.get("content_hash")
                or facets.get("sha256")
                or getattr(node, "id", "")
            )
            path = str(
                block_facets.get("source_path")
                or block_facets.get("path")
                or facets.get("source_path")
                or facets.get("path")
                or ""
            )
            byte_start = int(block_facets.get("byte_start") or facets.get("byte_start") or 0)
            byte_end = int(
                block_facets.get("byte_end")
                or facets.get("byte_end")
                or (facets.get("byte_length") if node_type == "Document" else 0)
                or 0
            )
            # ADR 0011 Phase 3 (security_byte_read_path_revalidation): the span
            # branch above re-validates containment; the legacy block_id branch did
            # NOT. Re-validate the (absolute) source path is under the vault root
            # before it can flow to a byte read; blank it on failure so out-of-vault
            # provenance never resolves to file bytes. Defense-in-depth — the Tier-2
            # reader (_read_slice with vault_root) is the load-bearing guard; this
            # closes the gap at the provenance source for every consumer.
            if path:
                try:
                    root = self.path.resolve(strict=False)
                    Path(path).resolve(strict=False).relative_to(root)
                except (ValueError, OSError):
                    path = ""
        return Provenance(
            path=path,
            byte_start=byte_start,
            byte_end=byte_end,
            content_hash=_sha256_uri(content_hash),
            extraction_activity_id=str(facets.get("extraction_activity_id") or ""),
            agent_id=str(facets.get("agent_id") or ""),
            document_id=str(
                facets.get("document_id")
                or facets.get("S_id")
                or (getattr(node, "id", "") if node_type == "Document" else "")
            ),
            block_id=block_id or (getattr(node, "id", "") if node_type == "Block" else ""),
        )

    def _attach_drift_best_effort(self, hits: list[QueryHit]) -> list[QueryHit]:
        try:
            from okto_neuron.drift import detect_drift

            findings = detect_drift(self.store, hits)
        except (ImportError, Exception):
            _LOGGER.warning(
                "drift detection unavailable; returning hits without drift", exc_info=True
            )
            return [hit.model_copy(update={"drift": None}) for hit in hits]

        if isinstance(findings, dict):
            return [
                hit.model_copy(
                    update={"drift": findings.get(hit.node.id) or findings.get(hit.claim_id)}
                )
                for hit in hits
            ]
        if isinstance(findings, list) and len(findings) == len(hits):
            return [
                hit.model_copy(update={"drift": finding})
                for hit, finding in zip(hits, findings, strict=True)
            ]
        return hits

    def _scoped_nodes(self, scope: ExportScope) -> list[object]:
        nodes = list(self.store.list_nodes())
        if scope.node_types is not None:
            allowed_types = set(scope.node_types)
            nodes = [node for node in nodes if getattr(node, "type", None) in allowed_types]
        if scope.document_ids is not None:
            allowed_docs = set(scope.document_ids)
            nodes = [node for node in nodes if _document_id_for_export(node) in allowed_docs]
        if scope.since is not None:
            nodes = [
                node
                for node in nodes
                if isinstance(getattr(node, "created_at", None), datetime)
                and getattr(node, "created_at") >= scope.since
            ]
        return nodes

    def _export_node(self, node: object, *, scope: ExportScope) -> dict[str, object]:
        facets = dict(getattr(node, "facets", {}) or {})
        payload: dict[str, object] = {
            "@id": str(getattr(node, "id")),
            "@type": str(getattr(node, "type")),
            "name": str(getattr(node, "title", "") or facets.get("name") or ""),
            "facets": facets,
        }
        created_at = getattr(node, "created_at", None)
        if isinstance(created_at, datetime):
            payload["created_at"] = created_at.isoformat()
        if scope.include_provenance:
            provenance = getattr(node, "provenance", None)
            if provenance is not None and hasattr(provenance, "model_dump"):
                payload["provenance"] = provenance.model_dump(mode="json")
        if scope.include_embeddings and getattr(node, "embedding", None) is not None:
            payload["embedding"] = getattr(node, "embedding")
        return payload

    def _validate_type_filter(self, type: str | None) -> None:
        # KNOWN LATENT BUG (pre-existing, tracked by QA/security): the
        # ``okto_neuron.standards`` module below does not exist, so the ImportError
        # branch always fires and this validation is a silent no-op. The HTTP
        # surface does NOT rely on it — ``server/http.py`` validates ``?type=``
        # against its own closed-set constant (CLOSED_NODE_TYPES) before calling
        # the vault, so unknown types are rejected at the API layer regardless.
        if type is None:
            return
        try:
            from okto_neuron.standards import PRIMITIVES
        except ImportError:
            return
        allowed = {str(getattr(primitive, "__name__", primitive)) for primitive in PRIMITIVES}
        if type not in allowed:
            raise QueryError(message=f"unknown node type filter: {type}")

    def _ensure_open(self) -> None:
        if self._closed:
            raise VaultClosedError(self.path)


def _public_node(node: object) -> Node:
    facets = dict(getattr(node, "facets", {}) or {})
    title = getattr(node, "title", None) or facets.get("name")
    return Node(
        id=str(getattr(node, "id")),
        type=str(getattr(node, "type")),
        name=str(title) if title else None,
    )


def _document_from_ingest_result(result: object, source_path: Path | None) -> Document:
    if isinstance(result, Document):
        return result
    if isinstance(result, IngestResult):
        return Document(id=result.document_id, type="Document")
    if isinstance(result, dict):
        document_id = str(
            result.get("document_id") or result.get("id") or _fallback_document_id(source_path)
        )
        name = result.get("name") or result.get("title")
        return Document(
            id=document_id,
            type=str(result.get("type") or "Document"),
            name=str(name) if name else None,
            path=str(result.get("path") or source_path or "") or None,
        )

    document_id = str(getattr(result, "id", "") or _fallback_document_id(source_path))
    path = getattr(result, "path", None) or getattr(result, "uri", None) or source_path
    title = getattr(result, "title", None) or getattr(result, "name", None)
    tags = tuple(str(tag) for tag in (getattr(result, "tags", None) or ()))
    media_type = getattr(result, "mimetype", None) or getattr(result, "media_type", None)
    raw_hash = getattr(result, "sha256", None) or _hash_file(path)
    return Document(
        id=document_id,
        type="Document",
        name=str(title) if title else None,
        path=str(path) if path else None,
        tags=tags,
        media_type=str(media_type) if media_type else None,
        content_hash=_sha256_uri(raw_hash) if raw_hash else None,
    )


def _document_id_for_export(node: object) -> str:
    facets = dict(getattr(node, "facets", {}) or {})
    if getattr(node, "type", None) == "Document":
        return str(getattr(node, "id"))
    return str(facets.get("document_id") or facets.get("S_id") or "")


def _fallback_document_id(source_path: Path | None) -> str:
    value = str(source_path or "document")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_file(path: object) -> str | None:
    if path is None:
        return None
    candidate = Path(path)
    if not candidate.exists() or not candidate.is_file():
        return None
    return hashlib.sha256(candidate.read_bytes()).hexdigest()


def _sha256_uri(value: object) -> str:
    raw = str(value or "")
    if raw.startswith("sha256:"):
        digest = raw.removeprefix("sha256:")
        if _SHA256_HEX.fullmatch(digest):
            return raw
    if _SHA256_HEX.fullmatch(raw):
        return f"sha256:{raw}"
    return f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


__all__ = ["StorageInfo", "Vault", "VaultBackendMismatch"]
