"""Layered, secret-free semantic policy fingerprints for ingest runs.

The ledger persists only the resulting SHA-256 identifiers.  Structured payloads
remain available here for tests and diagnostics, so each inclusion/exclusion rule
has one implementation rather than being rediscovered by every caller.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from okto_neuron.curator import (
    RELATION_CURATOR_EVIDENCE_VERSION,
    candidate_curator_system,
    effective_relation_curator_system,
)
from okto_neuron.extract import (
    _DESCRIBE_TMPL,
    _ENUM_CONT,
    _ENUM_DESCRIBE_BATCH,
    _ENUM_MAX_DESCRIBE_BATCHES,
    _ENUM_MAX_HANDLES,
    _ENUM_MAX_ROUNDS,
    _ENUM_SYS,
    ALLOWED_NODE_TYPES,
    EXTRACTION_RESPONSE_FORMAT,
    MAX_NODES_PER_BLOCK,
    RUNAWAY_CHARS_PER_NODE,
    extraction_system_prompt,
)
from okto_neuron.packs import BUILTIN
from okto_neuron.predicates.index import (
    PREDICATE_ALIASES,
    PREDICATE_DIRNAME,
    PREDICATE_SCHEMA_VERSION,
)
from okto_neuron.predicates.registry import (
    PREDICATE_REGISTRY_SCHEMA_VERSION,
    PredicateRegistry,
)
from okto_neuron.reconcile.authority import (
    AUTHORITY_DIRNAME,
    AUTHORITY_INDEX,
)
from okto_neuron.reconcile.authority import (
    SCHEMA_VERSION as AUTHORITY_SCHEMA_VERSION,
)
from okto_neuron.reconcile.decisions import (
    IDENTITY_DECISIONS,
    IDENTITY_DECISIONS_SCHEMA_VERSION,
)
from okto_neuron.reconcile.type_adjudication import (
    TYPE_ADJUDICATION_PROMPT_VERSION,
    TYPE_ADJUDICATION_RESPONSE_FORMAT,
    TYPE_ADJUDICATION_SYSTEM_PROMPT,
)
from okto_neuron.resolve import _VERDICT_SYSTEM
from okto_neuron.semantic_surface import NORMALIZER_VERSION

if TYPE_CHECKING:
    from okto_neuron.config import IngestConfig, VaultConfig


FINGERPRINT_SCHEMA_VERSION = "semantic_fingerprint.v1"
SEMANTIC_MATERIALIZATION_SCHEMA_VERSION = "semantic_materialization.v1"
_SEMANTIC_MATERIALIZATION_RELATIVE_PATH = Path(".marginalia") / "semantic-materialization.json"
_SHA256_IDENTIFIER = re.compile(r"^sha256:[0-9a-f]{64}$")

# These are behavior contracts, not package versions. Bump the owning entry when
# a compatible code release changes the corresponding semantic decision.
CONTRACT_VERSIONS: Mapping[str, str] = {
    "claim_evidence": "claim_evidence.v2",
    "claim_identity": "claim_identity.v1",
    "entity_resolution": "entity_resolution.v2",
    "extraction_primitives": "extraction_primitives.v2",
    "identity_type_adjudication": TYPE_ADJUDICATION_PROMPT_VERSION,
    "node_identity": "node_identity.v1",
    "predicate_normalization": "predicate_normalization.v1",
    "predicate_registry": PREDICATE_REGISTRY_SCHEMA_VERSION,
    "predicate_registry_fingerprint": "predicate_registry_policy_projection.v1",
    # v3 (ADR 0040 D6a.7): `admit_predicate` itself is unchanged and still
    # pure, but the admission DECISION PROCEDURE its ingest caller applies now
    # resolves a novel label against the live registry before minting it.
    # Without the bump, full-policy replay of a v2 run would reuse a mint that
    # v3 policy would have folded.
    "predicate_admission": "predicate_admission.v3",
    "predicate_lexical_normalizer": "predicate_lexical_normalizer.v1",
    "relation_curator_evidence": RELATION_CURATOR_EVIDENCE_VERSION,
    "relation_gate": "relation_gate.v1",
    "commit_plan": "commit_plan.v2",
    "surface_normalization": NORMALIZER_VERSION,
}

_LLM_STEPS = ("extraction", "judge", "curator", "relation_curator")
_GENERATION_FIELDS = (
    "max_tokens",
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "enable_thinking",
)
_SECRET_KEY_PARTS = frozenset(
    {
        "api_key",
        "apikey",
        "auth_token",
        "authorization",
        "cookie",
        "credential",
        "password",
        "secret",
    }
)
_EXECUTION_KEY_PARTS = frozenset(
    {
        "batch_size",
        "concurrency",
        "concurrent",
        "max_retries",
        "num_retries",
        "request_timeout",
        "retry",
        "retries",
        "timeout",
    }
)


@dataclass(frozen=True)
class SemanticFingerprints:
    """The three immutable identifiers written on an ingest-run start row."""

    semantic_policy_fingerprint: str
    config_fingerprint: str
    extraction_fingerprint: str


@dataclass(frozen=True)
class SemanticFingerprintPayloads:
    """Structured inputs used to derive :class:`SemanticFingerprints`."""

    config: dict[str, Any]
    extraction: dict[str, Any]
    semantic_policy: dict[str, Any]


def semantic_materialization_path(vault_path: Path | str) -> Path:
    """Return the generation-bound materialization receipt path."""

    return (
        Path(vault_path).expanduser().resolve(strict=False)
        / _SEMANTIC_MATERIALIZATION_RELATIVE_PATH
    )


def write_semantic_materialization(
    path: Path | str,
    *,
    graph_generation: str,
    fingerprints: Mapping[str, object],
    source: str,
) -> dict[str, Any]:
    """Atomically bind one fingerprint triplet to one graph generation."""

    generation = str(graph_generation).strip()
    source_name = str(source).strip()
    if not generation:
        raise ValueError("semantic materialization requires graph_generation")
    if not source_name:
        raise ValueError("semantic materialization requires source")
    normalized = _validated_materialization_fingerprints(fingerprints)
    payload = {
        "schema_version": SEMANTIC_MATERIALIZATION_SCHEMA_VERSION,
        "graph_generation": generation,
        "fingerprints": normalized,
        "source": source_name,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        _fsync_directory(target.parent)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return payload


def publish_semantic_materialization(
    vault_path: Path | str,
    *,
    graph_generation: str,
    fingerprints: Mapping[str, object],
    source: str,
) -> dict[str, Any]:
    """Publish the active generation's semantic materialization receipt."""

    return write_semantic_materialization(
        semantic_materialization_path(vault_path),
        graph_generation=graph_generation,
        fingerprints=fingerprints,
        source=source,
    )


def load_semantic_materialization(
    path: Path | str,
    *,
    expected_graph_generation: str | None = None,
) -> dict[str, Any] | None:
    """Load and strictly validate one materialization receipt."""

    target = Path(path)
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict):
        raise ValueError("semantic materialization must be an object")
    if set(payload) != {
        "schema_version",
        "graph_generation",
        "fingerprints",
        "source",
    }:
        raise ValueError("semantic materialization has unexpected fields")
    if payload.get("schema_version") != SEMANTIC_MATERIALIZATION_SCHEMA_VERSION:
        raise ValueError("unsupported semantic materialization schema")
    generation = str(payload.get("graph_generation") or "").strip()
    source = str(payload.get("source") or "").strip()
    if not generation or not source:
        raise ValueError("semantic materialization identity is incomplete")
    if expected_graph_generation is not None and generation != expected_graph_generation:
        return None
    return {
        "schema_version": SEMANTIC_MATERIALIZATION_SCHEMA_VERSION,
        "graph_generation": generation,
        "fingerprints": _validated_materialization_fingerprints(payload.get("fingerprints")),
        "source": source,
    }


def materialized_semantic_fingerprints(
    vault_path: Path | str,
    *,
    graph_generation: str | None,
) -> dict[str, str | None]:
    """Resolve the fingerprints materialized in the selected graph generation.

    A generation-bound receipt wins. Legacy/live generations without one retain
    the fail-closed latest-per-document ledger projection.
    """

    if graph_generation:
        receipt = load_semantic_materialization(
            semantic_materialization_path(vault_path),
            expected_graph_generation=graph_generation,
        )
        if receipt is not None:
            return dict(receipt["fingerprints"])
    return _ledger_materialized_semantic_fingerprints(vault_path)


def invalidate_semantic_materialization(vault_path: Path | str) -> None:
    """Invalidate a generation receipt before any in-place semantic write."""

    path = semantic_materialization_path(vault_path)
    path.unlink(missing_ok=True)
    if path.parent.exists():
        _fsync_directory(path.parent)


def _validated_materialization_fingerprints(
    value: object,
) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != {
        "config",
        "extraction",
        "semantic_policy",
    }:
        raise ValueError("semantic materialization requires the complete fingerprint triplet")
    result: dict[str, str] = {}
    for key in ("config", "extraction", "semantic_policy"):
        item = value.get(key)
        if not isinstance(item, str) or _SHA256_IDENTIFIER.fullmatch(item) is None:
            raise ValueError(f"semantic materialization {key} fingerprint is invalid")
        result[key] = item
    return result


def _ledger_materialized_semantic_fingerprints(
    vault_path: Path | str,
) -> dict[str, str | None]:
    from okto_neuron.consolidate.ledger import (
        _ACCEPTED_LEDGER_VERSIONS,
        CandidateLedger,
    )

    starts: dict[str, dict[str, Any]] = {}
    latest_by_document: dict[str, dict[str, str]] = {}
    # Only run rows are read below; kinds= keeps every other row out of memory.
    scan = CandidateLedger(Path(vault_path) / ".marginalia").scan(kinds=frozenset({"ingest_run"}))
    for record in scan.parsed_records:
        if record.get("ledger_version") not in _ACCEPTED_LEDGER_VERSIONS:
            continue
        if record.get("kind") != "ingest_run":
            continue
        run_id = str(record.get("run_id") or "").strip()
        if not run_id:
            continue
        run_state = str(record.get("state") or "").strip()
        if run_state == "started":
            starts[run_id] = record
            continue
        if run_state != "completed":
            continue
        started = starts.get(run_id)
        if started is None:
            continue
        document_id = str(started.get("document_id") or "").strip()
        if not document_id:
            continue
        latest_by_document[document_id] = {
            "config": str(started.get("config_fingerprint") or "").strip(),
            "extraction": str(started.get("extraction_fingerprint") or "").strip(),
            "semantic_policy": str(
                record.get("post_semantic_policy_fingerprint")
                or started.get("semantic_policy_fingerprint")
                or ""
            ).strip(),
        }
    if not latest_by_document:
        # No ``remember()`` run was ever ledgered for this vault — either
        # nothing has been ingested through the full companion pipeline yet,
        # or every document arrived through the deterministic-only ``Vault.add``
        # path (server ``/add``, the CLI's ``kg add``), which never touches
        # the candidate ledger regardless of whether the LLM is enabled (it
        # mints Document/Block/Claim structure straight from markdown, with
        # no extraction pass to record). Both fingerprints and the graph
        # content they describe in that case are a pure function of the
        # vault's CURRENT semantic configuration — there is no history to be
        # ambiguous about, unlike the ``len(values) != 1`` branch below where
        # completed runs actually disagree and staying fail-closed matters.
        # Compute that live triplet instead of failing closed, so a
        # model-free vault still gets a real, generation-bound materialization
        # receipt (see ``kg.py::_write_previous_semantic_materialization``).
        return _effective_semantic_fingerprint_triplet(vault_path)
    result: dict[str, str | None] = {}
    for key in ("config", "extraction", "semantic_policy"):
        values = {row[key] for row in latest_by_document.values() if row[key]}
        result[key] = next(iter(values)) if len(values) == 1 else None
    return result


def _effective_semantic_fingerprint_triplet(vault_path: Path | str) -> dict[str, str]:
    """Return the vault's current, live-config semantic fingerprint triplet.

    Every fingerprint here is a deterministic hash of *configuration*
    (ingest settings, extraction/curation prompts and parameters, the
    registered semantic policy sidefiles), never of an actual LLM call's
    output — so it is always computable, LLM-enabled or not. This mirrors
    ``cli/kg.py``'s ``_effective_semantic_fingerprint_triplet`` (which callers
    outside this module should keep using; this copy exists so
    ``semantic_fingerprint.py`` has no import-time dependency on ``cli.kg``).
    """

    from okto_neuron.companion import _incremental
    from okto_neuron.config import VaultConfig

    path = Path(vault_path)
    cfg = VaultConfig.load(path)
    computed = semantic_fingerprints(
        cfg,
        path,
        ingest_config=cfg.ingest,
        effective_incremental=_incremental.incremental_enabled(cfg.ingest),
        effective_subchunk=_incremental.subchunk_enabled(cfg.ingest),
    )
    return {
        "config": computed.config_fingerprint,
        "extraction": computed.extraction_fingerprint,
        "semantic_policy": computed.semantic_policy_fingerprint,
    }


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _text_hash(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _normalized_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")


def _contains_key_part(name: str, parts: frozenset[str]) -> bool:
    compact = name.replace("_", "")
    return any(part in name or part.replace("_", "") in compact for part in parts)


def _is_secret_key(value: object) -> bool:
    name = _normalized_key(value)
    if (
        name in {"code", "key", "sig", "signature", "token"}
        or name.endswith("_key")
        or name.endswith("_sig")
        or name.endswith("_signature")
        or name.endswith("_token")
    ):
        return True
    return _contains_key_part(name, _SECRET_KEY_PARTS)


def _is_execution_only_key(value: object) -> bool:
    return _contains_key_part(_normalized_key(value), _EXECUTION_KEY_PARTS)


def _semantic_parameters(value: object) -> object:
    """Recursively remove credentials and request-scheduling controls."""

    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
            if _is_secret_key(key) or _is_execution_only_key(key):
                continue
            clean = _semantic_parameters(item)
            if clean in ({}, []):
                continue
            sanitized[str(key)] = clean
        return sanitized
    if isinstance(value, (list, tuple)):
        return [_semantic_parameters(item) for item in value]
    return value


def _safe_api_base(value: object) -> str | None:
    """Keep endpoint identity while dropping userinfo, secret query parts and fragments."""

    if value is None:
        return None
    parsed = urlsplit(str(value))
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        port = ""
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if not _is_secret_key(key)
        )
    )
    return urlunsplit(
        (parsed.scheme.casefold(), f"{host.casefold()}{port}", parsed.path, query, "")
    )


def _runtime_identity(component: object, *, kind: str) -> dict[str, Any]:
    cls = type(component)
    identity: dict[str, Any] = {"implementation": f"{cls.__module__}.{cls.__qualname__}"}
    for name in ("model", "model_name", "dimension", "parameter_mode"):
        value = getattr(component, name, None)
        if value is not None:
            identity[name] = value
    api_base = _safe_api_base(getattr(component, "api_base", None))
    if api_base is not None:
        identity["api_base"] = api_base
    return {"runtime_override": kind, **identity}


def _pack_payload(names: list[str]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for name in sorted({str(item).strip().casefold() for item in names if str(item).strip()}):
        pack = BUILTIN.get(name)
        if pack is None:
            payload.append({"name": name, "definition": "external"})
            continue
        payload.append(
            {
                "name": name,
                "node_types": sorted(pack.node_types),
                "edge_types": sorted(pack.edge_types),
            }
        )
    return payload


def _prompt_hashes(config: "VaultConfig") -> dict[str, str]:
    return {
        "extraction": _text_hash(
            config.llm.extraction.system_prompt or extraction_system_prompt(config.packs)
        ),
        "judge": _text_hash(config.llm.judge.system_prompt or _VERDICT_SYSTEM),
        "curator": _text_hash(
            config.llm.curator.system_prompt or candidate_curator_system(config.packs)
        ),
        "relation_curator": _text_hash(
            effective_relation_curator_system(
                config.llm.relation_curator.system_prompt,
                config.packs,
            )
        ),
        "type_adjudication": _text_hash(TYPE_ADJUDICATION_SYSTEM_PROMPT),
    }


def _llm_step_payload(
    config: "VaultConfig",
    step: str,
    *,
    runtime_provider: object | None,
) -> dict[str, Any]:
    resolved = config.llm.resolved(step)  # type: ignore[arg-type]
    if runtime_provider is not None:
        connection = _runtime_identity(runtime_provider, kind="llm_provider")
    else:
        connection = {
            "provider": resolved.provider,
            "api_base": _safe_api_base(resolved.api_base),
            "model": resolved.model,
            "parameter_mode": resolved.parameter_mode,
        }
    generation = {
        name: getattr(resolved, name)
        for name in _GENERATION_FIELDS
        if getattr(resolved, name) is not None
    }
    dynamic = _semantic_parameters(resolved.parameters)
    if dynamic:
        generation["parameters"] = dynamic
    return {"connection": connection, "generation": generation}


def _embedding_payload(
    config: "VaultConfig",
    *,
    runtime_embedder: object | None,
) -> dict[str, Any]:
    if runtime_embedder is not None:
        return _runtime_identity(runtime_embedder, kind="embedding_provider")
    resolved = config.embedding.resolved_provider()
    return {
        "provider": resolved.provider,
        "api_base": _safe_api_base(resolved.api_base),
        "model": resolved.model,
        "dimension": resolved.dimension,
    }


def _sidefile_hash(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except (FileNotFoundError, IsADirectoryError):
        return "absent"


def build_semantic_fingerprint_payloads(
    config: "VaultConfig",
    vault_path: Path | str,
    *,
    ingest_config: "IngestConfig | None" = None,
    effective_incremental: bool | None = None,
    effective_subchunk: bool | None = None,
    runtime_provider: object | None = None,
    runtime_embedder: object | None = None,
) -> SemanticFingerprintPayloads:
    """Build deterministic semantic payloads without reading secret values."""

    ingest = ingest_config or config.ingest
    incremental = ingest.incremental if effective_incremental is None else effective_incremental
    subchunk = ingest.subchunk if effective_subchunk is None else effective_subchunk
    prompts = _prompt_hashes(config)
    packs = _pack_payload(config.packs)
    llm_steps = {
        step: {
            **_llm_step_payload(config, step, runtime_provider=runtime_provider),
            "system_prompt": prompts[step],
        }
        for step in _LLM_STEPS
    }
    prefilter = config.consolidation.prefilter
    config_payload: dict[str, Any] = {
        "schema": FINGERPRINT_SCHEMA_VERSION,
        "packs": packs,
        "embedding": _embedding_payload(config, runtime_embedder=runtime_embedder),
        "llm": {"enabled": config.llm.enabled, "steps": llm_steps},
        "ingest": {
            "incremental": bool(incremental),
            "subchunk": bool(subchunk),
            "chunk_size_bytes": ingest.chunk_size_bytes,
            "chunk_overlap_bytes": ingest.chunk_overlap_bytes,
        },
        "consolidation": {
            "auto_commit_threshold": config.consolidation.auto_commit_threshold,
            "review_on_contradiction": config.consolidation.review_on_contradiction,
            "type_adjudication_enabled": (config.consolidation.type_adjudication_enabled),
            "relation_curator_enabled": config.consolidation.relation_curator_enabled,
            "curation_batch_size": config.consolidation.curation_batch_size,
            "audit_superseded_nodes_with_llm": (
                config.consolidation.audit_superseded_nodes_with_llm
            ),
            "audit_superseded_relations_with_llm": (
                config.consolidation.audit_superseded_relations_with_llm
            ),
            "prefilter": {
                "enabled": prefilter.enabled,
                "demote_predicates": sorted(set(prefilter.demote_predicates)),
                "min_mentions": prefilter.min_mentions,
                "max_trivial_node_chars": prefilter.max_trivial_node_chars,
                "established_entity_fastpath": prefilter.established_entity_fastpath,
            },
        },
    }

    ext = config.llm.extraction
    extraction_payload: dict[str, Any] = {
        "schema": FINGERPRINT_SCHEMA_VERSION,
        "llm_enabled": config.llm.enabled,
        "provider": llm_steps["extraction"],
        "packs": packs,
        "chunking": config_payload["ingest"],
        "policy": {
            "mode": ext.mode or "auto",
            "samples": ext.samples or 1,
            "enumerate_max_handles": ext.enumerate_max_handles or _ENUM_MAX_HANDLES,
            "enumerate_describe_batch": (ext.enumerate_describe_batch or _ENUM_DESCRIBE_BATCH),
            "enumerate_max_describe_batches": (
                ext.enumerate_max_describe_batches or _ENUM_MAX_DESCRIBE_BATCHES
            ),
            "enumerate_max_rounds": _ENUM_MAX_ROUNDS,
            "max_nodes_per_block": MAX_NODES_PER_BLOCK,
            "runaway_chars_per_node": RUNAWAY_CHARS_PER_NODE,
        },
        "prompt_components": {
            "enumerate": _text_hash(_ENUM_SYS),
            "enumerate_continue": _text_hash(_ENUM_CONT),
            "describe": _text_hash(_DESCRIBE_TMPL),
            "response_schema": _canonical_hash(EXTRACTION_RESPONSE_FORMAT),
        },
        "allowed_node_types": sorted(ALLOWED_NODE_TYPES),
        "contracts": {
            "extraction_primitives": CONTRACT_VERSIONS["extraction_primitives"],
            "node_identity": CONTRACT_VERSIONS["node_identity"],
        },
    }

    config_fingerprint = _canonical_hash(config_payload)
    extraction_fingerprint = _canonical_hash(extraction_payload)
    semantic_policy_payload: dict[str, Any] = {
        "schema": FINGERPRINT_SCHEMA_VERSION,
        "config_fingerprint": config_fingerprint,
        "extraction_fingerprint": extraction_fingerprint,
        "contracts": dict(sorted(CONTRACT_VERSIONS.items())),
        "type_adjudication": {
            "enabled": config.consolidation.type_adjudication_enabled,
            "system_prompt": prompts["type_adjudication"],
            "response_schema": _canonical_hash(TYPE_ADJUDICATION_RESPONSE_FORMAT),
            "provider_step": "curator",
        },
        "relation_curator": {
            "enabled": config.consolidation.relation_curator_enabled,
        },
        "decision_sidefiles": {
            "authority": {
                "schema": AUTHORITY_SCHEMA_VERSION,
                "content_hash": _sidefile_hash(
                    Path(vault_path) / ".marginalia" / AUTHORITY_DIRNAME / AUTHORITY_INDEX
                ),
            },
            "identity_decisions": {
                "schema": IDENTITY_DECISIONS_SCHEMA_VERSION,
                "content_hash": _sidefile_hash(
                    Path(vault_path) / ".marginalia" / AUTHORITY_DIRNAME / IDENTITY_DECISIONS
                ),
            },
            "predicate_aliases": {
                "schema": PREDICATE_SCHEMA_VERSION,
                "content_hash": _sidefile_hash(
                    Path(vault_path) / ".marginalia" / PREDICATE_DIRNAME / PREDICATE_ALIASES
                ),
            },
            "predicate_registry": {
                "schema": PREDICATE_REGISTRY_SCHEMA_VERSION,
                "content_hash": _canonical_hash(
                    PredicateRegistry(vault_path).policy_projection(config.packs)
                ),
            },
        },
    }
    return SemanticFingerprintPayloads(
        config=config_payload,
        extraction=extraction_payload,
        semantic_policy=semantic_policy_payload,
    )


def semantic_fingerprints(
    config: "VaultConfig",
    vault_path: Path | str,
    **kwargs: Any,
) -> SemanticFingerprints:
    """Return the ledger-ready fingerprints for one effective ingest policy."""

    payloads = build_semantic_fingerprint_payloads(config, vault_path, **kwargs)
    return SemanticFingerprints(
        config_fingerprint=_canonical_hash(payloads.config),
        extraction_fingerprint=_canonical_hash(payloads.extraction),
        semantic_policy_fingerprint=_canonical_hash(payloads.semantic_policy),
    )


__all__ = [
    "CONTRACT_VERSIONS",
    "FINGERPRINT_SCHEMA_VERSION",
    "SEMANTIC_MATERIALIZATION_SCHEMA_VERSION",
    "SemanticFingerprintPayloads",
    "SemanticFingerprints",
    "build_semantic_fingerprint_payloads",
    "invalidate_semantic_materialization",
    "load_semantic_materialization",
    "materialized_semantic_fingerprints",
    "publish_semantic_materialization",
    "semantic_materialization_path",
    "semantic_fingerprints",
    "write_semantic_materialization",
]
