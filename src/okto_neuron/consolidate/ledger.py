"""Durable candidate ledger for the pre-commit curation boundary.

The ledger is vault-local process state under ``<vault>/.marginalia``. It is
not a graph primitive and it does not drive graph writes; it records what the
existing ``remember`` pipeline already decided so the hidden middle becomes
inspectable and replayable enough for future tooling.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from collections import Counter, OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None  # type: ignore[assignment]

LEDGER_FILENAME = "candidate-ledger.jsonl"
FRESH_REBUILD_MATERIALIZATION_SCOPE = "fresh_rebuild.v1"
# v2 (ADR 0015 D3.1): candidate rows no longer inline embedding vectors —
# they store ``embedding_dim`` instead. Vectors live only in the graph store.
# Readers accept v1 and v2 rows alike (the read path already collapsed
# embeddings to embedding_dim for display, so nothing downstream changes).
# Candidate payloads are intentionally extensible: ADR 0040 adds an optional
# ``surface.v1`` evidence object without changing row framing or write meaning.
LEDGER_VERSION = 2
# Grow this set deliberately when a new reader-compatible framing version ships.
# Do not derive it from LEDGER_VERSION: doing so would silently reject v2 on a
# future writer bump even though this module explicitly supports historical rows.
_ACCEPTED_LEDGER_VERSIONS = frozenset({1, 2})

RecordKind = Literal[
    "ingest_run",
    "extraction_unit",
    "candidate",
    "comparison",
    "commit_plan",
    "operation_receipt",
    "commit_record",
    "plan_abandoned",
    "integrity_outcome",
]
CandidateKind = Literal["node", "edge"]
ExtractionUnitStatus = Literal[
    "succeeded",
    "provider_failed",
    "invalid_output",
    "empty_after_retry",
    "source_changed",
    "cancelled",
    "intentionally_skipped",
]
LedgerCompleteness = Literal["absent", "complete", "incomplete"]

_MALFORMED_SAMPLE_LIMIT = 8
_MALFORMED_SAMPLE_CHARS = 200
_SAFE_TORN_RECORD_KINDS = frozenset({"ingest_run", "extraction_unit", "candidate", "comparison"})
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, Any] = {}
_LEDGER_INDEXES_GUARD = threading.Lock()
_LEDGER_INDEX_CACHE_SIZE = 8
_LEDGER_INDEXES: OrderedDict[str, "_LedgerOffsetIndex"] = OrderedDict()
_LEDGER_INDEX_BUILD_LOCKS: dict[str, threading.Lock] = {}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _partial_top_level_kind(fragment: bytes) -> str | None:
    """Return a complete top-level ``kind`` value from a partial JSON object.

    This deliberately does not search for a byte substring: source/model payloads
    may themselves contain a nested ``kind`` field.  The small scanner only needs
    enough JSON framing awareness to identify a completed string value attached to
    the root object's ``kind`` key.
    """

    object_depth = 0
    array_depth = 0
    in_string = False
    escaped = False
    token = bytearray()
    token_is_top_level = False
    pending_string: bytes | None = None
    expecting_kind_value = False

    for byte in fragment:
        if in_string:
            if escaped:
                escaped = False
                token.append(byte)
                continue
            if byte == ord("\\"):
                escaped = True
                token.append(byte)
                continue
            if byte != ord('"'):
                token.append(byte)
                continue

            in_string = False
            if token_is_top_level:
                if expecting_kind_value:
                    try:
                        return json.loads(b'"' + bytes(token) + b'"')
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        return None
                pending_string = bytes(token)
            continue

        if byte == ord('"'):
            in_string = True
            escaped = False
            token.clear()
            token_is_top_level = object_depth == 1 and array_depth == 0
            continue
        if byte == ord("{"):
            if expecting_kind_value:
                return None
            object_depth += 1
            continue
        if byte == ord("}"):
            object_depth = max(0, object_depth - 1)
            continue
        if byte == ord("["):
            if expecting_kind_value:
                return None
            array_depth += 1
            continue
        if byte == ord("]"):
            array_depth = max(0, array_depth - 1)
            continue
        if object_depth != 1 or array_depth != 0:
            continue
        if byte == ord(":") and pending_string is not None:
            expecting_kind_value = pending_string == b"kind"
            pending_string = None
            continue
        if byte == ord(","):
            pending_string = None
            expecting_kind_value = False
            continue
        if expecting_kind_value and byte not in b" \t\r\n":
            return None


@contextmanager
def _exclusive_lock(path: Path):
    # POSIX flock locks alone do not serialize independent threads reliably:
    # they are process-scoped on the supported platforms. Pair the file lock
    # with one process-local lock keyed by the canonical lock-file path.
    key = str(path.resolve())
    with _PROCESS_LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.setdefault(key, threading.Lock())
    with process_lock, path.open("a+b") as lock_file:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - exercised on Windows
            lock_file.seek(0)
            if not lock_file.read(1):
                lock_file.write(b"0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - all supported platforms provide one
            raise OSError("no candidate-ledger file locking backend available")
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - exercised on Windows
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _without_heavy_values(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if key == "embedding":
                if isinstance(item, (list, tuple)):
                    out["embedding_dim"] = len(item)
                continue
            out[str(key)] = _without_heavy_values(item)
        return out
    if isinstance(value, list):
        return [_without_heavy_values(item) for item in value]
    if isinstance(value, tuple):
        return [_without_heavy_values(item) for item in value]
    return value


def _canonical_plan_hash(
    run_id: str,
    plan_id: str,
    operations: list[dict[str, Any]],
    context: dict[str, Any],
) -> str:
    encoded = json.dumps(
        {
            "run_id": run_id,
            "plan_id": plan_id,
            "operations": operations,
            "context": context,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _operation_id(operation: dict[str, Any]) -> str:
    payload = {key: value for key, value in operation.items() if key != "operation_id"}
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


_PLAN_OPERATION_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "register_predicate": (
        frozenset({"operation", "predicate", "expected_before", "record"}),
        frozenset(),
    ),
    # ADR 0040 D6a. No `expected_before`: the alias ledger is keyed by a
    # deterministic record id and `PredicateAliasIndex.upsert` is idempotent
    # under its own lock, so this needs no compare-and-set precondition.
    "register_predicate_alias": (
        frozenset({"operation", "predicate", "record"}),
        frozenset(),
    ),
    "create_node": (
        frozenset(
            {
                "operation",
                "candidate_kind",
                "candidate_id",
                "candidate",
                "confidence",
                "correlations",
                "reason",
            }
        ),
        frozenset({"node"}),
    ),
    "queue_review": (
        frozenset({"operation", "candidate_kind", "candidate_id", "candidate", "reason"}),
        frozenset({"confidence", "correlations", "target_ref", "review_item"}),
    ),
    "supersede_candidate": (
        frozenset({"operation", "candidate_kind", "candidate_id", "reason"}),
        frozenset(
            {
                "candidate",
                "correlations",
                "target_ref",
                "type",
                "src_ref",
                "dst_ref",
                "dst_literal",
            }
        ),
    ),
    "dead_letter": (
        frozenset({"operation", "candidate_kind", "candidate_id", "candidate", "reason"}),
        frozenset({"target_ref"}),
    ),
    "mint_claim": (
        frozenset(
            {
                "operation",
                "candidate_kind",
                "candidate_id",
                "candidate",
                "expected_claim_id",
                "min_claim_confidence",
                "mention_candidates",
                "reason",
                "pinned_semantics",
                "claim",
                "expected_before",
            }
        ),
        frozenset({"decision_trace"}),
    ),
    "attach_claim_provenance": (
        frozenset({"operation", "candidate_id", "claim_id", "edge"}),
        frozenset({"reason"}),
    ),
    "create_topology_edge": (
        frozenset(
            {
                "operation",
                "candidate_kind",
                "candidate_id",
                "candidate",
                "edge",
                "expected_edge_id",
                "required_claim_id",
                "reason",
                "pinned_semantics",
            }
        ),
        frozenset({"decision_trace"}),
    ),
    "ensure_node": (
        frozenset({"operation", "node"}),
        frozenset({"reason"}),
    ),
    "update_node_state": (
        frozenset({"operation", "node_id", "expected_before", "node"}),
        frozenset({"reason"}),
    ),
    "ensure_source_mention": (
        frozenset({"operation", "edge"}),
        frozenset({"reason"}),
    ),
    "supersede": (
        frozenset(
            {
                "operation",
                "old_claim_id",
                "new_claim_id",
                "expected_edge_id",
                "edge",
                "reason",
            }
        ),
        frozenset(),
    ),
    "append_detachment_annotation": (
        frozenset({"operation", "annotation_id", "artifact", "record", "reason"}),
        frozenset(),
    ),
    "source_removed": (
        frozenset({"operation", "source_id", "derived_artifact_ids", "reason"}),
        frozenset(),
    ),
    "review_commit": (
        frozenset(
            {
                "operation",
                "candidate_kind",
                "candidate_id",
                "type",
                "title",
                "confidence",
                "target_ref",
                "reason",
                "review_item",
                "node",
                "edge",
            }
        ),
        frozenset(),
    ),
    "review_link": (
        frozenset(
            {
                "operation",
                "candidate_kind",
                "candidate_id",
                "type",
                "title",
                "confidence",
                "target_ref",
                "reason",
                "review_item",
                "node",
                "edge",
            }
        ),
        frozenset(),
    ),
    "review_discard": (
        frozenset(
            {
                "operation",
                "candidate_kind",
                "candidate_id",
                "type",
                "title",
                "confidence",
                "target_ref",
                "reason",
                "review_item",
                "node",
                "edge",
            }
        ),
        frozenset(),
    ),
    "review_merge": (
        frozenset(
            {
                "operation",
                "candidate_kind",
                "candidate_id",
                "type",
                "title",
                "confidence",
                "target_ref",
                "reason",
                "review_item",
                "node",
                "edge",
            }
        ),
        frozenset(),
    ),
}


def _validate_plan_operation(
    operation: dict[str, Any],
    *,
    operation_id_required: bool,
) -> None:
    discriminator = operation.get("operation")
    if not isinstance(discriminator, str) or not discriminator:
        raise ValueError("commit plan operation requires a non-empty discriminator")
    schema = _PLAN_OPERATION_FIELDS.get(discriminator)
    if schema is None:
        raise ValueError(f"unsupported commit plan operation: {discriminator!r}")
    required, optional = schema
    expected = required | optional | ({"operation_id"} if operation_id_required else set())
    fields = set(operation)
    missing = required - fields
    if operation_id_required and "operation_id" not in fields:
        missing = missing | {"operation_id"}
    unknown = fields - expected
    if missing or unknown:
        raise ValueError(
            f"invalid {discriminator} operation fields: "
            f"missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    if discriminator in {"mint_claim", "create_topology_edge"}:
        trace = operation.get("decision_trace")
        if trace is not None:
            if not isinstance(trace, dict) or set(trace) != {"d6", "d7"}:
                raise ValueError(f"{discriminator} operation decision_trace must contain d6 and d7")
            d6 = trace["d6"]
            d7 = trace["d7"]
            d6_fields = {
                "reason",
                "state",
                "action",
                "raw_predicate",
                "predicate",
                "subject_id",
                "object_id",
                "object_literal",
                "swapped",
            }
            d7_fields = {
                "reason",
                "action",
                "relation_kind",
                "subject_id",
                "predicate",
                "object_id",
                "object_literal",
                "liveness_support_ids",
            }
            if not isinstance(d6, dict) or set(d6) != d6_fields:
                raise ValueError(f"{discriminator} operation has an invalid D6 trace")
            if not isinstance(d7, dict) or set(d7) != d7_fields:
                raise ValueError(f"{discriminator} operation has an invalid D7 trace")
            semantics = operation.get("pinned_semantics")
            if not isinstance(semantics, dict):
                raise ValueError(f"{discriminator} operation lacks pinned semantics")
            expected_semantics = {
                "predicate": d7.get("predicate"),
                "subject_id": d7.get("subject_id"),
                "object_id": d7.get("object_id"),
                "object_literal": d7.get("object_literal"),
            }
            if (
                d7.get("reason") != "commit"
                or d7.get("action") != "commit"
                or operation.get("reason") != d7.get("reason")
                or semantics != expected_semantics
                or d6.get("predicate") != d7.get("predicate")
                or d6.get("subject_id") != d7.get("subject_id")
                or d6.get("object_id") != d7.get("object_id")
                or d6.get("object_literal") != d7.get("object_literal")
            ):
                raise ValueError(
                    f"{discriminator} operation decision trace differs from pinned semantics"
                )
    if discriminator == "supersede":
        for field in ("old_claim_id", "new_claim_id", "expected_edge_id"):
            if not isinstance(operation[field], str) or not operation[field]:
                raise ValueError(f"supersede operation {field} must be non-empty text")
        if not isinstance(operation["reason"], str):
            raise ValueError("supersede operation reason must be text")
        if operation["old_claim_id"] == operation["new_claim_id"]:
            raise ValueError("supersede operation requires distinct old and new claim ids")
        edge = operation["edge"]
        if not isinstance(edge, dict):
            raise ValueError("supersede operation edge must be an object")
        if edge.get("id") != operation["expected_edge_id"]:
            raise ValueError("supersede operation edge id does not match expected_edge_id")
        if edge.get("type") != "supersedes":
            raise ValueError("supersede operation edge type must be 'supersedes'")
        if edge.get("src") != operation["new_claim_id"]:
            raise ValueError("supersede operation edge source must be new_claim_id")
        if edge.get("dst") != operation["old_claim_id"]:
            raise ValueError("supersede operation edge destination must be old_claim_id")
    elif discriminator == "append_detachment_annotation":
        for field in ("annotation_id", "artifact"):
            if not isinstance(operation[field], str) or not operation[field]:
                raise ValueError(
                    f"append_detachment_annotation operation {field} must be non-empty text"
                )
        if not isinstance(operation["reason"], str):
            raise ValueError("append_detachment_annotation operation reason must be text")
        if not isinstance(operation["record"], dict):
            raise ValueError("append_detachment_annotation operation record must be an object")
    elif discriminator == "source_removed":
        # ADR 0039 D2: source removal is a planned, receipted group closing with
        # this verification operation — never an escape hatch around the model.
        source_id = operation["source_id"]
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("source_removed operation source_id must be non-empty text")
        if not isinstance(operation["reason"], str) or not operation["reason"]:
            raise ValueError("source_removed operation reason must be non-empty text")
        artifact_ids = operation["derived_artifact_ids"]
        if not isinstance(artifact_ids, list) or not artifact_ids:
            raise ValueError(
                "source_removed operation derived_artifact_ids must be a non-empty list"
            )
        if any(not isinstance(artifact_id, str) or not artifact_id for artifact_id in artifact_ids):
            raise ValueError("source_removed operation derived_artifact_ids must be non-empty text")
        # The operation id hashes this payload verbatim, so a non-canonical order
        # would give the same intent two identities across a resume.
        if artifact_ids != sorted(set(artifact_ids)):
            raise ValueError(
                "source_removed operation derived_artifact_ids must be sorted and unique"
            )
    elif discriminator.startswith("review_"):
        review_item = operation["review_item"]
        if not isinstance(review_item, dict):
            raise ValueError("manual review operation review_item must be an object")
        if set(review_item) != {"candidate", "reason", "correlations"}:
            raise ValueError("manual review operation has an invalid review_item shape")
        if not isinstance(review_item["candidate"], dict):
            raise ValueError("manual review operation candidate must be an object")
        if review_item["reason"] not in {"low_confidence", "contradiction"}:
            raise ValueError("manual review operation has an invalid review reason")
        if not isinstance(review_item["correlations"], list):
            raise ValueError("manual review operation correlations must be a list")
        node = operation["node"]
        if not isinstance(node, dict):
            raise ValueError("manual review operation node must be an object")
        if (
            node.get("id") != operation["candidate_id"]
            or node.get("type") != operation["type"]
            or node.get("title") != operation["title"]
        ):
            raise ValueError("manual review operation node differs from the candidate")
        target_ref = operation["target_ref"]
        if target_ref is not None and not isinstance(target_ref, str):
            raise ValueError("manual review operation target_ref must be text or null")
        edge = operation["edge"]
        if discriminator == "review_link" and target_ref is not None:
            if not isinstance(edge, dict):
                raise ValueError("review_link operation with a target requires an edge")
            if (
                edge.get("type") != "relates_to"
                or edge.get("src") != operation["candidate_id"]
                or edge.get("dst") != target_ref
            ):
                raise ValueError("review_link operation edge differs from its target")
        elif edge is not None:
            raise ValueError("manual review operation has an unexpected edge")
    if operation_id_required:
        operation_id = operation.get("operation_id")
        if not isinstance(operation_id, str) or operation_id != _operation_id(operation):
            raise ValueError("commit plan operation id does not match its intent")


def _validate_plan_operation_set(operations: list[dict[str, Any]]) -> None:
    """Validate invariants that span otherwise-valid individual operations."""

    manual_operations = [
        operation
        for operation in operations
        if str(operation.get("operation") or "").startswith("review_")
    ]
    if manual_operations and len(operations) != 1:
        raise ValueError("manual review plans must contain exactly one operation")

    unique_targets = {
        "update_node_state": "node_id",
        "append_detachment_annotation": "annotation_id",
        "supersede": "old_claim_id",
        "source_removed": "source_id",
    }
    # ADR 0039 D2: source removal is a GROUP, not an escape hatch — the closing
    # source_removed receipt may only ride along with the retirement operations
    # that actually did the work. (Per-artifact-id coverage is deferred: an
    # artifact retired by an earlier run is legitimately absent from this plan.)
    if any(operation.get("operation") == "source_removed" for operation in operations):
        if not any(
            operation.get("operation") in {"update_node_state", "supersede"}
            for operation in operations
        ):
            raise ValueError("source_removed requires the retirement operations of its group")

    for operation_kind, target_field in unique_targets.items():
        targets = [
            str(operation[target_field])
            for operation in operations
            if operation.get("operation") == operation_kind
        ]
        if len(targets) != len(set(targets)):
            raise ValueError(
                f"commit plan contains multiple {operation_kind} intents for one {target_field}"
            )


def _candidate_summary(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    candidate = payload.get("candidate") if isinstance(payload.get("candidate"), dict) else payload
    outcome = payload.get("outcome") if isinstance(payload.get("outcome"), dict) else {}
    return {
        "candidate_id": record.get("candidate_id"),
        "candidate_kind": record.get("candidate_kind"),
        "state": record.get("state"),
        "type": candidate.get("type") or outcome.get("type") or "",
        "title": candidate.get("title") or outcome.get("title") or "",
        "confidence": outcome.get("confidence"),
        "source_path": (candidate.get("facets") or {}).get("source_path")
        if isinstance(candidate.get("facets"), dict)
        else None,
        "block_id": (candidate.get("facets") or {}).get("block_id")
        if isinstance(candidate.get("facets"), dict)
        else None,
    }


def _node_candidate_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    if isinstance(payload, dict):
        nested = payload.get("candidate")
        if isinstance(nested, dict):
            return nested
        return payload
    return record


def _timing_stats(values: list[float], *, tokens: dict[str, int] | None = None) -> dict[str, Any]:
    """Aggregate per-method LLM call timings (ADR 0015 D3.3)."""
    ordered = sorted(values)
    n = len(ordered)
    stats: dict[str, Any] = {
        "calls": n,
        "total_s": round(sum(ordered), 3),
        "p50_s": round(ordered[n // 2], 3),
        "p90_s": round(ordered[min(n - 1, int(n * 0.9))], 3),
        "max_s": round(ordered[-1], 3),
    }
    if tokens:
        stats["tokens"] = dict(sorted(tokens.items()))
    return stats


def _top_counts(counts: Counter[str], *, limit: int) -> dict[str, int]:
    return {
        key: count
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    }


def _population_revision(
    *, previous_total: int, total: int, reason: str, source: str | None = None
) -> dict[str, Any]:
    """One declared change of a progress denominator (ADR 0039 T9).

    A dynamic population (prefilter/dedup shrinking the active candidate set)
    must publish its revised total together with the reason it changed, so a
    denominator move is visible instead of being absorbed into the percentage.
    """
    revision: dict[str, Any] = {
        "previous_total": previous_total,
        "total": total,
        "reason": reason,
    }
    if source:
        revision["source"] = source
    return revision


def _progress(
    done: int,
    total: int,
    *,
    population: str,
    revision: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One phase's progress against a named population (ADR 0039 T9).

    ``done <= total`` is the invariant. A violation is a telemetry error, not a
    percentage to clamp and hide: ``remaining`` reports the true (negative)
    arithmetic, ``fraction`` stays unbounded, and an additive
    ``progress_integrity_error`` record names the violation. This is a pure
    read-path summary helper — it never raises and never writes a ledger row.
    """
    record: dict[str, Any] = {
        "done": done,
        "total": total,
        "remaining": total - done,
        "fraction": (done / total) if total else None,
        "population": population,
    }
    if revision is not None:
        record["population_revision"] = revision
    if done > total:
        record["progress_integrity_error"] = {
            "code": "progress_done_exceeds_total",
            "population": population,
            "done": done,
            "total": total,
            "overflow": done - total,
        }
    return record


@dataclass(frozen=True)
class ResumeSnapshot:
    """One-scan snapshot of a prior (interrupted) run, for ADR 0015 D5b resume.

    ``verdicts_by_method`` maps each requested comparison method to a
    ``candidate_id -> comparison record`` dict (last record per id wins;
    ``audit_only`` comparisons are excluded — they belong to the audit pass,
    not the live curation gate). ``candidate_ids`` is every candidate-row id
    already present in the run, used to suppress duplicate candidate rows on
    a resumed ingest (the observed v0.0.9 duplicate-rows bug).
    ``candidate_records`` also keeps the last row per id, so an exact-policy
    rebuild can distinguish a raw proposal from its sealed terminal outcome.
    """

    run_id: str
    verdicts_by_method: dict[str, dict[str, dict[str, Any]]]
    candidate_ids: set[str]
    candidate_records: dict[str, dict[str, Any]]
    node_identity_by_id: dict[str, tuple[str, str]]


@dataclass(frozen=True)
class CommitPlanSnapshot:
    """One sealed, unreceipted write plan that can drive apply-resume."""

    run_id: str
    plan_id: str
    plan_hash: str
    operations: tuple[dict[str, Any], ...]
    context: dict[str, Any]


@dataclass(frozen=True)
class MalformedLedgerLine:
    """Bounded private diagnostic for one non-empty non-record line.

    ``sample`` can contain source-derived text and must not be exposed through
    a public API without an explicit redaction policy.
    """

    line_number: int
    reason: str
    sample: str
    sample_truncated: bool


@dataclass(frozen=True)
class LedgerScanResult:
    """Lossless evidence summary for one immutable view of a JSONL ledger file.

    ``parsed_records`` remains useful even when ``completeness_status`` is
    ``incomplete``. Callers that publish quality evidence must carry the status
    and reason with those records instead of treating the readable subset as
    the complete ledger.
    """

    path: Path
    parsed_records: tuple[dict[str, Any], ...]
    total_lines: int
    nonempty_lines: int
    malformed_line_count: int
    malformed_lines: tuple[MalformedLedgerLine, ...]
    malformed_samples_truncated: bool
    unrecognized_version_record_count: int
    unterminated_final_line: bool
    trailing_partial: bool
    trailing_partial_line_number: int | None
    ledger_versions: tuple[int, ...]
    file_size_bytes: int
    file_sha256: str | None
    completeness_status: LedgerCompleteness
    completeness_reason: str

    @property
    def parsed_record_count(self) -> int:
        return len(self.parsed_records)


@dataclass
class _LedgerOffsetIndex:
    """Validated, memory-bounded routing metadata for one immutable file size."""

    signature: tuple[int, int, int, int]
    offsets_by_kind: dict[str, list[tuple[int, int]]]
    offsets_by_run_kind: dict[tuple[str, str], list[tuple[int, int]]]
    node_identity_by_id: dict[str, tuple[str, str]]
    ambiguous_node_ids: set[str]
    latest_candidate_run: dict[str, str]
    completeness_status: LedgerCompleteness
    completeness_reason: str


def _ledger_file_signature(path: Path) -> tuple[int, int, int, int]:
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def _index_candidate_identity(
    index: _LedgerOffsetIndex,
    record: dict[str, Any],
) -> None:
    candidate_id = str(record.get("candidate_id") or "")
    run_id = str(record.get("run_id") or "")
    if candidate_id and run_id:
        index.latest_candidate_run[candidate_id] = run_id
    if not candidate_id or record.get("candidate_kind") != "node":
        return
    payload = _node_candidate_payload(record)
    type_ = str(payload.get("type") or "").strip()
    title = str(payload.get("title") or "").strip()
    if not type_ or not title or candidate_id in index.ambiguous_node_ids:
        return
    identity = (type_, title)
    prior = index.node_identity_by_id.get(candidate_id)
    if prior is None:
        index.node_identity_by_id[candidate_id] = identity
    elif prior != identity:
        index.node_identity_by_id.pop(candidate_id, None)
        index.ambiguous_node_ids.add(candidate_id)


def _build_ledger_offset_index(directory: Path, path: Path) -> _LedgerOffsetIndex:
    offsets_by_kind: dict[str, list[tuple[int, int]]] = {}
    offsets_by_run_kind: dict[tuple[str, str], list[tuple[int, int]]] = {}
    malformed = 0
    unrecognized_versions = 0
    index = _LedgerOffsetIndex(
        signature=(0, 0, 0, 0),
        offsets_by_kind=offsets_by_kind,
        offsets_by_run_kind=offsets_by_run_kind,
        node_identity_by_id={},
        ambiguous_node_ids=set(),
        latest_candidate_run={},
        completeness_status="complete",
        completeness_reason="empty_ledger",
    )
    with _exclusive_lock(directory / ".candidate-ledger.lock"):
        with path.open("rb") as handle:
            while True:
                offset = handle.tell()
                raw = handle.readline()
                if not raw:
                    break
                if not raw.strip():
                    continue
                try:
                    record = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    malformed += 1
                    continue
                if not isinstance(record, dict):
                    malformed += 1
                    continue
                version = record.get("ledger_version")
                if (
                    not isinstance(version, int)
                    or isinstance(version, bool)
                    or version not in _ACCEPTED_LEDGER_VERSIONS
                ):
                    unrecognized_versions += 1
                kind = str(record.get("kind") or "")
                if not kind:
                    continue
                location = (offset, len(raw))
                offsets_by_kind.setdefault(kind, []).append(location)
                run_id = str(record.get("run_id") or "")
                if run_id:
                    offsets_by_run_kind.setdefault((run_id, kind), []).append(location)
                if kind == "candidate":
                    _index_candidate_identity(index, record)
            index.signature = _ledger_file_signature(path)
    reasons: list[str] = []
    if malformed:
        reasons.append("malformed_ledger_lines")
    if unrecognized_versions:
        reasons.append("unrecognized_ledger_versions")
    if reasons:
        index.completeness_status = "incomplete"
        index.completeness_reason = "+".join(reasons)
    elif any(offsets_by_kind.values()):
        index.completeness_reason = "all_nonempty_lines_parsed"
    return index


def _invalidate_ledger_offset_index(path: Path) -> None:
    with _LEDGER_INDEXES_GUARD:
        _LEDGER_INDEXES.pop(str(path.resolve()), None)


def _get_ledger_offset_index(directory: Path, path: Path) -> _LedgerOffsetIndex:
    key = str(path.resolve())
    signature = _ledger_file_signature(path)
    with _LEDGER_INDEXES_GUARD:
        cached = _LEDGER_INDEXES.get(key)
        if cached is not None and cached.signature == signature:
            _LEDGER_INDEXES.move_to_end(key)
            return cached
        build_lock = _LEDGER_INDEX_BUILD_LOCKS.setdefault(key, threading.Lock())
    with build_lock:
        signature = _ledger_file_signature(path)
        with _LEDGER_INDEXES_GUARD:
            cached = _LEDGER_INDEXES.get(key)
            if cached is not None and cached.signature == signature:
                _LEDGER_INDEXES.move_to_end(key)
                return cached
        built = _build_ledger_offset_index(directory, path)
        with _LEDGER_INDEXES_GUARD:
            _LEDGER_INDEXES[key] = built
            _LEDGER_INDEXES.move_to_end(key)
            while len(_LEDGER_INDEXES) > _LEDGER_INDEX_CACHE_SIZE:
                _LEDGER_INDEXES.popitem(last=False)
        return built


def _extend_ledger_offset_index(
    path: Path,
    *,
    previous_signature: tuple[int, int, int, int] | None,
    current_signature: tuple[int, int, int, int],
    offset: int,
    length: int,
    record: dict[str, Any],
) -> None:
    key = str(path.resolve())
    with _LEDGER_INDEXES_GUARD:
        index = _LEDGER_INDEXES.get(key)
        if index is None:
            return
        if previous_signature is None or index.signature != previous_signature:
            _LEDGER_INDEXES.pop(key, None)
            return
        kind = str(record.get("kind") or "")
        location = (offset, length)
        index.offsets_by_kind.setdefault(kind, []).append(location)
        run_id = str(record.get("run_id") or "")
        if run_id:
            index.offsets_by_run_kind.setdefault((run_id, kind), []).append(location)
        if kind == "candidate":
            _index_candidate_identity(index, record)
        index.signature = current_signature
        _LEDGER_INDEXES.move_to_end(key)


def edge_candidate_id(payload: dict[str, Any]) -> str:
    """Stable id for an edge/propositional-claim candidate.

    ``EdgeCandidate`` intentionally has no public ``candidate_id`` because it is
    resolved through endpoint refs at commit time. The ledger still needs a
    durable handle, so derive one from the edge's semantic payload plus source
    anchor/model fields.
    """
    h = hashlib.sha256()
    h.update(
        json.dumps(
            {
                "type": payload.get("type"),
                "src_ref": payload.get("src_ref"),
                "dst_ref": payload.get("dst_ref"),
                "dst_literal": payload.get("dst_literal"),
                "block_id": payload.get("block_id"),
                "content_hash": payload.get("content_hash"),
                "model_id": payload.get("model_id"),
                "prompt_hash": payload.get("prompt_hash"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    return h.hexdigest()


def extraction_unit_id(
    *,
    block_id: str,
    byte_start: int,
    byte_end: int,
    content_hash: str,
    extraction_fingerprint: str,
) -> str:
    """Stable identity for one exact extraction span and extraction policy."""

    if not block_id or not content_hash or not extraction_fingerprint:
        raise ValueError("extraction unit identity requires block, content, and fingerprint")
    if byte_start < 0 or byte_end < byte_start:
        raise ValueError("extraction unit byte range is invalid")
    encoded = json.dumps(
        {
            "block_id": block_id,
            "byte_start": byte_start,
            "byte_end": byte_end,
            "content_hash": content_hash,
            "extraction_fingerprint": extraction_fingerprint,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class CandidateLedger:
    """Append-only JSONL ledger rooted in a vault's ``.marginalia`` directory."""

    dir: Path

    @property
    def path(self) -> Path:
        return Path(self.dir) / LEDGER_FILENAME

    def _offset_index(self) -> _LedgerOffsetIndex | None:
        if not self.path.exists():
            return None
        return _get_ledger_offset_index(Path(self.dir), self.path)

    def _indexed_records(
        self,
        *,
        kinds: set[str],
        run_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Parse only indexed rows needed by one operational query."""

        for _attempt in range(2):
            index = self._offset_index()
            if index is None:
                return []
            if run_ids is None:
                locations = [
                    location for kind in kinds for location in index.offsets_by_kind.get(kind, ())
                ]
            else:
                locations = [
                    location
                    for run_id in run_ids
                    for kind in kinds
                    for location in index.offsets_by_run_kind.get((run_id, kind), ())
                ]
            locations.sort()
            with _exclusive_lock(Path(self.dir) / ".candidate-ledger.lock"):
                if _ledger_file_signature(self.path) != index.signature:
                    _invalidate_ledger_offset_index(self.path)
                    continue
                records: list[dict[str, Any]] = []
                with self.path.open("rb") as handle:
                    for offset, length in locations:
                        handle.seek(offset)
                        try:
                            record = json.loads(handle.read(length).decode("utf-8"))
                        except (UnicodeDecodeError, ValueError):
                            continue
                        if not isinstance(record, dict) or record.get("kind") not in kinds:
                            continue
                        if run_ids is not None and str(record.get("run_id") or "") not in run_ids:
                            continue
                        records.append(record)
                return records
        raise RuntimeError("candidate ledger changed continuously while reading its index")

    def _require_complete_index(self, *, purpose: str) -> _LedgerOffsetIndex | None:
        index = self._offset_index()
        if index is not None and index.completeness_status != "complete":
            raise ValueError(
                f"cannot {purpose} from an incomplete candidate ledger: {index.completeness_reason}"
            )
        return index

    def start_run(
        self,
        *,
        document_id: str,
        source: str,
        blocks_total: int,
        model: str,
        semantic_policy_fingerprint: str | None = None,
        config_fingerprint: str | None = None,
        extraction_fingerprint: str | None = None,
        materialization_scope: str | None = None,
    ) -> str:
        if materialization_scope is not None and not materialization_scope.strip():
            raise ValueError("materialization scope cannot be empty")
        run_id = uuid.uuid4().hex
        self.append(
            "ingest_run",
            run_id=run_id,
            state="started",
            document_id=document_id,
            source=source,
            blocks_total=blocks_total,
            model=model,
            semantic_policy_fingerprint=semantic_policy_fingerprint,
            config_fingerprint=config_fingerprint,
            extraction_fingerprint=extraction_fingerprint,
            materialization_scope=materialization_scope,
        )
        return run_id

    def finish_run(
        self,
        run_id: str,
        *,
        state: str,
        summary: dict[str, Any],
        post_semantic_policy_fingerprint: str | None = None,
    ) -> None:
        self.append(
            "ingest_run",
            run_id=run_id,
            state=state,
            summary=summary,
            **(
                {"post_semantic_policy_fingerprint": (post_semantic_policy_fingerprint)}
                if post_semantic_policy_fingerprint
                else {}
            ),
        )

    def record_integrity_outcome(
        self,
        run_id: str,
        *,
        document_id: str,
        integrity: dict[str, Any],
        quality: str | None = None,
    ) -> None:
        """Append the post-write audit that closes one run's integrity evidence."""

        self.append(
            "integrity_outcome",
            run_id=run_id,
            document_id=document_id,
            integrity=integrity,
            **({"quality": quality} if quality else {}),
        )

    def record_extraction_unit(
        self,
        run_id: str,
        *,
        document_id: str,
        unit_id: str,
        block_id: str,
        byte_start: int,
        byte_end: int,
        content_hash: str,
        source_path: str,
        extraction_fingerprint: str,
        attempt: int,
        status: ExtractionUnitStatus,
        reason: str | None = None,
        error_class: str | None = None,
        retry_disposition: str | None = None,
        duration_ms: float = 0.0,
        anomalies: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Persist one terminal extraction attempt without source text or vectors."""

        if not all((run_id, document_id, unit_id, block_id, content_hash)):
            raise ValueError("extraction unit record is missing required identity")
        if attempt < 0 or byte_start < 0 or byte_end < byte_start:
            raise ValueError("extraction unit attempt or byte range is invalid")
        expected_unit_id = extraction_unit_id(
            block_id=block_id,
            byte_start=byte_start,
            byte_end=byte_end,
            content_hash=content_hash,
            extraction_fingerprint=extraction_fingerprint,
        )
        if unit_id != expected_unit_id:
            raise ValueError("extraction unit id does not match its identity")
        if status == "succeeded":
            if not isinstance(result, dict):
                raise ValueError("successful extraction unit requires a result")
            if error_class is not None:
                raise ValueError("successful extraction unit cannot carry an error class")
        elif result is not None:
            raise ValueError("unsuccessful extraction unit cannot carry a replay result")
        if status == "provider_failed" and not error_class:
            raise ValueError("provider failure requires an error class")
        if status != "provider_failed" and retry_disposition is not None:
            raise ValueError("retry disposition is valid only for provider failures")
        self.append(
            "extraction_unit",
            run_id=run_id,
            document_id=document_id,
            unit_id=unit_id,
            block_id=block_id,
            byte_start=byte_start,
            byte_end=byte_end,
            content_hash=content_hash,
            source_path=source_path,
            extraction_fingerprint=extraction_fingerprint,
            attempt=attempt,
            status=status,
            reason=reason,
            error_class=error_class,
            retry_disposition=retry_disposition,
            duration_ms=max(0.0, float(duration_ms)),
            anomalies=anomalies or {},
            result=_without_heavy_values(result) if result is not None else None,
        )

    def successful_extraction_units(
        self,
        *,
        document_id: str,
        extraction_fingerprint: str,
    ) -> dict[str, dict[str, Any]]:
        """Replayable successful units across runs for one exact extraction policy.

        Malformed trailing bytes are not replay evidence: the readable prefix is
        inspected and any missing unit is simply re-extracted. If an explicit
        rerun produced another valid payload for the same stochastic model/unit,
        the newest durable success is the replay value; downstream semantic
        decisions and plans are always recomputed from that one value.
        """

        successes: dict[str, dict[str, Any]] = {}
        for record in self._indexed_records(kinds={"extraction_unit"}):
            if record.get("kind") != "extraction_unit":
                continue
            if str(record.get("document_id") or "") != str(document_id):
                continue
            if str(record.get("extraction_fingerprint") or "") != extraction_fingerprint:
                continue
            unit_id = str(record.get("unit_id") or "")
            if not unit_id:
                continue
            if (
                record.get("status") == "invalid_output"
                and record.get("reason") == "stored_replay_incompatible"
            ):
                # A newer reader proved the cached payload cannot be rehydrated.
                # This explicit invalidation lets the next successful extraction
                # replace it without permanently wedging the content-addressed id.
                successes.pop(unit_id, None)
                continue
            if record.get("status") != "succeeded":
                continue
            result = record.get("result")
            if not isinstance(result, dict):
                continue
            successes[unit_id] = record
        return successes

    def record_candidate(
        self,
        run_id: str,
        *,
        candidate_id: str,
        candidate_kind: CandidateKind,
        state: str,
        payload: dict[str, Any],
    ) -> None:
        self.append(
            "candidate",
            run_id=run_id,
            candidate_id=candidate_id,
            candidate_kind=candidate_kind,
            state=state,
            # Strip embedding vectors at write time (ledger v2): a 10MB corpus
            # was producing a 48MB ledger dominated by inlined float lists.
            payload=_without_heavy_values(payload),
        )

    def record_comparison(
        self,
        run_id: str,
        *,
        candidate_id: str,
        method: str,
        verdict: str,
        score: float | None = None,
        target_ref: str | None = None,
        reason: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.append(
            "comparison",
            run_id=run_id,
            candidate_id=candidate_id,
            method=method,
            target_ref=target_ref,
            verdict=verdict,
            score=score,
            reason=reason,
            payload=payload or {},
        )

    def record_commit_plan(
        self,
        run_id: str,
        *,
        operations: list[dict[str, Any]],
        context: dict[str, Any] | None = None,
    ) -> str:
        plan_id = uuid.uuid4().hex
        raw_operations = _jsonable(operations)
        if not isinstance(raw_operations, list) or any(
            not isinstance(operation, dict) for operation in raw_operations
        ):
            raise ValueError("commit plan operations must be objects")
        normalized_operations: list[dict[str, Any]] = []
        for operation in raw_operations:
            _validate_plan_operation(operation, operation_id_required=False)
            if "operation_id" in operation:
                raise ValueError("operation_id is assigned by the candidate ledger")
            normalized_operations.append({**operation, "operation_id": _operation_id(operation)})
        operation_ids = [operation["operation_id"] for operation in normalized_operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("commit plan contains duplicate operation intents")
        _validate_plan_operation_set(normalized_operations)
        normalized_context = _jsonable(context or {})
        plan_hash = _canonical_plan_hash(
            run_id,
            plan_id,
            normalized_operations,
            normalized_context,
        )
        self.append(
            "commit_plan",
            run_id=run_id,
            plan_id=plan_id,
            plan_hash=plan_hash,
            operations=normalized_operations,
            context=normalized_context,
        )
        return plan_id

    def record_operation_receipt(
        self,
        run_id: str,
        *,
        plan_id: str,
        plan_hash: str,
        operation_id: str,
        operation: str,
        status: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        if status not in {
            "applied",
            "already_present",
            "dead_lettered",
            "failed",
            "aborted",
        }:
            raise ValueError(f"invalid operation receipt status: {status!r}")
        self.append(
            "operation_receipt",
            run_id=run_id,
            plan_id=plan_id,
            plan_hash=plan_hash,
            operation_id=operation_id,
            operation=operation,
            status=status,
            result=result or {},
        )

    def record_commit(
        self,
        run_id: str,
        *,
        plan_id: str,
        result: dict[str, Any],
    ) -> None:
        plans = [plan for plan in self.unreceipted_commit_plans() if plan.plan_id == plan_id]
        if len(plans) != 1:
            raise ValueError(f"commit plan is missing or already closed: {plan_id}")
        plan = plans[0]
        if plan.run_id != run_id:
            raise ValueError("commit record run does not match its plan")
        receipts = self.operation_receipts(plan)
        expected_ids = {str(operation["operation_id"]) for operation in plan.operations}
        if set(receipts) != expected_ids:
            missing = sorted(expected_ids - set(receipts))
            raise ValueError(f"commit plan cannot close with missing operation receipts: {missing}")
        failed = sorted(
            operation_id
            for operation_id, receipt in receipts.items()
            if receipt.get("status") in {"failed", "aborted"}
        )
        if failed:
            raise ValueError(f"commit plan cannot close with failed operation receipts: {failed}")
        normalized_result = {
            **_jsonable(result),
            "operation_receipts_complete": True,
            "operation_receipts": len(receipts),
        }
        self.append(
            "commit_record",
            run_id=run_id,
            plan_id=plan_id,
            plan_hash=plan.plan_hash,
            result=normalized_result,
        )

    def record_plan_abandoned(
        self,
        run_id: str,
        *,
        plan_id: str,
        plan_hash: str,
        reason: str,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        """Durably close an untouched plan whose pinned source no longer matches."""

        plans = [plan for plan in self.unreceipted_commit_plans() if plan.plan_id == plan_id]
        if len(plans) != 1:
            raise ValueError(f"commit plan is missing or already closed: {plan_id}")
        plan = plans[0]
        if plan.run_id != run_id or plan.plan_hash != plan_hash:
            raise ValueError("abandoned plan identity does not match its sealed plan")
        if self.operation_receipts(plan):
            raise ValueError("a partially applied plan cannot be abandoned")
        if not reason.strip():
            raise ValueError("abandoned plan requires a reason")
        self.append(
            "plan_abandoned",
            run_id=run_id,
            plan_id=plan_id,
            plan_hash=plan_hash,
            reason=reason.strip(),
            evidence=evidence or {},
        )

    @contextmanager
    def semantic_writer_lease(self):
        """Hold the cross-process vault writer lease for one semantic transaction."""

        self.dir.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self.dir / ".semantic-writer.lock"):
            yield

    def append(self, kind: RecordKind, **payload: Any) -> None:
        record = {
            "kind": kind,
            "ts": _now(),
            **_jsonable(payload),
            # The caller cannot override the format contract through payload.
            "ledger_version": LEDGER_VERSION,
        }
        # Keep the discriminator first.  A power loss can leave the final write
        # incomplete, so recovery must be able to distinguish a replayable
        # extraction result from a write-ahead plan or receipt without parsing
        # the whole row.  Re-assigning preserves the first insertion position.
        record["kind"] = kind
        encoded = (json.dumps(record, sort_keys=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        self.dir.mkdir(parents=True, exist_ok=True)
        durable = kind in {
            "extraction_unit",
            "commit_plan",
            "operation_receipt",
            "commit_record",
            "plan_abandoned",
            "integrity_outcome",
        } or (kind == "ingest_run" and str(record.get("state") or "") != "started")
        previous_signature = _ledger_file_signature(self.path) if self.path.exists() else None
        record_offset = 0
        with _exclusive_lock(self.dir / ".candidate-ledger.lock"):
            with self.path.open("a+b") as fh:
                repaired_tail = self._prepare_append_target(fh)
                fh.seek(0, os.SEEK_END)
                record_offset = fh.tell()
                fh.write(encoded)
                # Plans are the write-ahead boundary and commit records are their
                # receipts. Neither may remain only in Python's append buffer while
                # graph mutations advance.
                if durable or repaired_tail:
                    fh.flush()
                    os.fsync(fh.fileno())
            current_signature = _ledger_file_signature(self.path)
            if repaired_tail:
                _invalidate_ledger_offset_index(self.path)
            else:
                _extend_ledger_offset_index(
                    self.path,
                    previous_signature=previous_signature,
                    current_signature=current_signature,
                    offset=record_offset,
                    length=len(encoded),
                    record=record,
                )
        if durable or repaired_tail:
            _fsync_directory(self.dir)

    def _repair_replayable_tail(self) -> bool:
        """Discard only a torn tail that cannot authorize graph mutation.

        Apply-resume reads the ledger before it appends anything, so it must use
        the same framing-aware recovery boundary as :meth:`append`. Plans and
        receipts remain fail-closed; run metadata and extraction evidence can be
        reproduced from the source document.
        """

        if not self.path.exists():
            return False
        repaired = False
        with _exclusive_lock(self.dir / ".candidate-ledger.lock"):
            with self.path.open("r+b") as fh:
                repaired = self._prepare_append_target(fh)
                if repaired:
                    fh.flush()
                    os.fsync(fh.fileno())
        if repaired:
            _invalidate_ledger_offset_index(self.path)
            _fsync_directory(self.dir)
        return repaired

    @staticmethod
    def _prepare_append_target(fh: Any) -> bool:
        """Make a prior interrupted append safe before writing another row.

        A complete JSON object that merely lacks its newline is preserved.  A
        malformed final run-metadata, extraction-unit, candidate, or comparison
        row is safe to discard because graph writes never depend on it and that
        work can be reproduced. Every other malformed tail remains fail-closed:
        it may be a plan or receipt at a write-ahead boundary and must not be
        guessed away.
        """

        fh.seek(0, os.SEEK_END)
        end = fh.tell()
        if end == 0:
            return False
        fh.seek(end - 1)
        if fh.read(1) == b"\n":
            return False

        cursor = end
        chunks: list[bytes] = []
        line_start = 0
        while cursor > 0:
            read_size = min(8192, cursor)
            cursor -= read_size
            fh.seek(cursor)
            chunk = fh.read(read_size)
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                line_start = cursor + newline + 1
                chunks.append(chunk[newline + 1 :])
                break
            chunks.append(chunk)
        fragment = b"".join(reversed(chunks))

        try:
            parsed = json.loads(fragment.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            fh.seek(0, os.SEEK_END)
            fh.write(b"\n")
            return True

        if _partial_top_level_kind(fragment) not in _SAFE_TORN_RECORD_KINDS:
            raise ValueError("candidate ledger ends with an unsafe torn record; refusing to append")

        fh.truncate(line_start)
        fh.seek(0, os.SEEK_END)
        return True

    def scan(
        self,
        *,
        max_malformed_samples: int = _MALFORMED_SAMPLE_LIMIT,
    ) -> LedgerScanResult:
        """Read all ledger bytes and report any evidence that could not be parsed.

        The regular :meth:`records` method intentionally preserves its historic
        compatibility behavior: it skips invalid JSON and non-object rows but
        can still fail on invalid UTF-8. Semantic audits use this method so such
        rows and interrupted final appends cannot disappear without an explicit
        incomplete result.
        """
        if max_malformed_samples < 0:
            raise ValueError("max_malformed_samples must be >= 0")
        if not self.path.exists():
            return LedgerScanResult(
                path=self.path,
                parsed_records=(),
                total_lines=0,
                nonempty_lines=0,
                malformed_line_count=0,
                malformed_lines=(),
                malformed_samples_truncated=False,
                unrecognized_version_record_count=0,
                unterminated_final_line=False,
                trailing_partial=False,
                trailing_partial_line_number=None,
                ledger_versions=(),
                file_size_bytes=0,
                file_sha256=None,
                completeness_status="absent",
                completeness_reason="ledger_file_absent",
            )

        with _exclusive_lock(Path(self.dir) / ".candidate-ledger.lock"):
            data = self.path.read_bytes()
        raw_lines = data.splitlines()
        unterminated_final_line = bool(data) and not data.endswith((b"\n", b"\r"))
        parsed_records: list[dict[str, Any]] = []
        malformed: list[MalformedLedgerLine] = []
        malformed_line_count = 0
        malformed_line_numbers: set[int] = set()
        nonempty_lines = 0
        ledger_versions: set[int] = set()
        unrecognized_version_record_count = 0
        final_nonempty_line_number: int | None = None

        for line_number, raw_line in enumerate(raw_lines, start=1):
            if not raw_line.strip():
                continue
            nonempty_lines += 1
            final_nonempty_line_number = line_number
            reason: str | None = None
            try:
                line = raw_line.decode("utf-8")
            except UnicodeDecodeError:
                line = raw_line.decode("utf-8", errors="replace")
                reason = "invalid_utf8"

            record: Any = None
            if reason is None:
                try:
                    record = json.loads(line)
                except ValueError:
                    reason = "invalid_json"
                else:
                    if not isinstance(record, dict):
                        reason = "record_not_object"

            if reason is not None:
                malformed_line_count += 1
                malformed_line_numbers.add(line_number)
                if len(malformed) < max_malformed_samples:
                    sample = json.dumps(line.strip(), ensure_ascii=True)[1:-1]
                    sample_truncated = len(sample) > _MALFORMED_SAMPLE_CHARS
                    if sample_truncated:
                        sample = sample[: _MALFORMED_SAMPLE_CHARS - 3] + "..."
                    malformed.append(
                        MalformedLedgerLine(
                            line_number=line_number,
                            reason=reason,
                            sample=sample,
                            sample_truncated=sample_truncated,
                        )
                    )
                continue

            parsed_records.append(record)
            version = record.get("ledger_version")
            if isinstance(version, int) and not isinstance(version, bool):
                ledger_versions.add(version)
                if version not in _ACCEPTED_LEDGER_VERSIONS:
                    unrecognized_version_record_count += 1
            else:
                unrecognized_version_record_count += 1

        trailing_partial = bool(
            unterminated_final_line
            and final_nonempty_line_number is not None
            and final_nonempty_line_number == len(raw_lines)
            and final_nonempty_line_number in malformed_line_numbers
        )
        incomplete_reasons: list[str] = []
        if malformed_line_count:
            incomplete_reasons.append("malformed_ledger_lines")
        if trailing_partial:
            incomplete_reasons.append("trailing_partial_record")
        if unrecognized_version_record_count:
            incomplete_reasons.append("unrecognized_ledger_versions")
        if incomplete_reasons:
            completeness_status: LedgerCompleteness = "incomplete"
            completeness_reason = "+".join(incomplete_reasons)
        elif nonempty_lines:
            completeness_status = "complete"
            completeness_reason = "all_nonempty_lines_parsed"
        else:
            completeness_status = "complete"
            completeness_reason = "empty_ledger"

        return LedgerScanResult(
            path=self.path,
            parsed_records=tuple(parsed_records),
            total_lines=len(raw_lines),
            nonempty_lines=nonempty_lines,
            malformed_line_count=malformed_line_count,
            malformed_lines=tuple(malformed),
            malformed_samples_truncated=malformed_line_count > len(malformed),
            unrecognized_version_record_count=unrecognized_version_record_count,
            unterminated_final_line=unterminated_final_line,
            trailing_partial=trailing_partial,
            trailing_partial_line_number=(final_nonempty_line_number if trailing_partial else None),
            ledger_versions=tuple(sorted(ledger_versions)),
            file_size_bytes=len(data),
            file_sha256=hashlib.sha256(data).hexdigest(),
            completeness_status=completeness_status,
            completeness_reason=completeness_reason,
        )

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def _open_runs(self) -> dict[str, dict[str, Any]]:
        """All runs whose LAST ``ingest_run`` record is state ``started``
        (crash-interrupted — a later completed/failed record closes a run)."""
        open_runs: dict[str, dict[str, Any]] = {}
        for record in self._indexed_records(
            kinds={"ingest_run", "commit_record", "plan_abandoned"}
        ):
            run_id = str(record.get("run_id") or "")
            if not run_id:
                continue
            if record.get("kind") in {"commit_record", "plan_abandoned"}:
                # The graph write tail has a durable receipt. A crash before the
                # later terminal run row must not replay curation over artifacts
                # that already committed.
                open_runs.pop(run_id, None)
                continue
            if record.get("kind") != "ingest_run":
                continue
            if str(record.get("state") or "") == "started":
                open_runs[run_id] = record
            else:
                open_runs.pop(run_id, None)
        return open_runs

    def has_open_run(self, *, document_id: str) -> bool:
        """True when a crash-interrupted (``started``) run exists for this
        document — REGARDLESS of ``blocks_total`` or ``model``.

        Used by the resume-aware narrowing bypass: sub-chunk narrowing (ADR
        0024) diffs stored Blocks against identical re-ingested bytes and can
        collapse the unit list (a crashed run leaves Blocks committed but no
        Claims), changing ``blocks_total`` and defeating
        :meth:`find_resumable_run`'s exact-match guard — the crashed run's
        streamed verdicts would be stranded forever. The caller disables
        narrowing for that ingest so the fingerprints line up.

        Deliberately NOT model-scoped (adversarial-verify finding): the bypass
        is cost-only-safe (extract full units instead of narrowing), so it is
        maximally permissive for recovery — a model switch between crash and
        re-ingest must still recover the document. Replay CORRECTNESS stays
        guarded by :meth:`find_resumable_run`'s exact model match, so a new
        model never replays another model's verdicts; it re-extracts fresh."""
        for record in self._open_runs().values():
            if str(record.get("document_id") or "") == str(document_id):
                return True
        return False

    def close_stale_runs(self, *, document_id: str, keep_run_id: str) -> int:
        """Close (state ``abandoned``) every crash-orphaned ``started`` run for
        ``document_id`` except ``keep_run_id``. Called after a run COMPLETES:
        the document's knowledge is now committed, so lingering rows must not
        keep :meth:`has_open_run`'s resume-narrowing bypass permanently
        engaged (review finding: a fingerprint-mismatched crashed run was
        never closed, disabling narrowing for that document forever)."""
        closed = 0
        for run_id, record in self._open_runs().items():
            if run_id == keep_run_id:
                continue
            if str(record.get("document_id") or "") != str(document_id):
                continue
            self.finish_run(
                run_id,
                state="abandoned",
                summary={"reason": "superseded_by_completed_run", "closed_by": keep_run_id},
            )
            closed += 1
        return closed

    def find_resumable_run(
        self,
        *,
        document_id: str,
        blocks_total: int,
        model: str,
        extraction_fingerprint: str,
        semantic_policy_fingerprint: str,
    ) -> str | None:
        """The most recent run still in state ``started`` for this document.

        ADR 0015 D5b guard rails: the run must match ``document_id`` AND
        ``blocks_total``, ``model``, extraction fingerprint, AND enclosing
        semantic-policy fingerprint exactly. The full policy is required because
        resume replays curator/relation-curator verdicts, not only raw extraction.
        Legacy rows without both fingerprints fail closed. A run with any later
        ``ingest_run`` record (e.g. ``completed``/``failed``) is never resumed.
        """
        if not extraction_fingerprint or not semantic_policy_fingerprint:
            return None
        match: str | None = None
        for run_id, record in self._open_runs().items():  # file order — last match wins
            if (
                str(record.get("document_id") or "") == str(document_id)
                and int(record.get("blocks_total") or 0) == int(blocks_total)
                and str(record.get("model") or "") == str(model)
                and str(record.get("extraction_fingerprint") or "") == extraction_fingerprint
                and str(record.get("semantic_policy_fingerprint") or "")
                == semantic_policy_fingerprint
            ):
                match = run_id
        return match

    def find_completed_decision_run(
        self,
        *,
        document_id: str,
        blocks_total: int,
        model: str,
        config_fingerprint: str,
        extraction_fingerprint: str,
        semantic_policy_fingerprint: str,
        materialization_scope: str,
    ) -> str | None:
        """Return the first canonical closed run whose verdicts are replay-safe.

        Kept as the single-run compatibility projection. Fresh materialization
        uses :meth:`find_completed_decision_runs` so later compatible runs can
        fill candidate-id gaps without replacing an earlier authoritative
        verdict for the same candidate.
        """

        matches = self.find_completed_decision_runs(
            document_id=document_id,
            blocks_total=blocks_total,
            model=model,
            config_fingerprint=config_fingerprint,
            extraction_fingerprint=extraction_fingerprint,
            semantic_policy_fingerprint=semantic_policy_fingerprint,
            materialization_scope=materialization_scope,
        )
        return matches[0] if matches else None

    def find_completed_decision_runs(
        self,
        *,
        document_id: str,
        blocks_total: int,
        model: str,
        config_fingerprint: str,
        extraction_fingerprint: str,
        semantic_policy_fingerprint: str,
        materialization_scope: str,
    ) -> tuple[str, ...]:
        """Return every compatible closed decision run in authority order.

        Unlike crash resume, this never reuses the old ``run_id`` or its write
        receipts.  It only selects the immutable comparison evidence that can be
        projected into a new run.  Exact source identity plus the complete
        fingerprint triplet and materialization scope are required, and the
        selected run must have a complete sealed-plan receipt and a terminal
        complete outcome.  The scope prevents a decision made against an
        already-populated incremental graph from being projected into an empty
        rebuild staging graph. Runs remain in ledger order: the first verdict for
        one candidate is authoritative for that exact policy identity, while a
        later compatible run may contribute verdicts for candidate ids that the
        earlier run never observed. This matters when deterministic source bytes
        produce a different candidate subset in a different graph context; one
        latest-run pointer would otherwise discard valid sealed authority.
        Legitimate policy changes produce a new fingerprint. Invalid ledger
        framing fails closed instead of turning a readable prefix into a decision
        cache.
        """

        if not all(
            (
                config_fingerprint,
                extraction_fingerprint,
                semantic_policy_fingerprint,
                materialization_scope,
            )
        ):
            return ()
        self._repair_replayable_tail()
        # Reuse the sealed-plan validator even though this path selects closed
        # plans.  It verifies plan hashes, operation ids, receipts, terminal
        # ordering, and commit closure for every current-format plan.
        self.unreceipted_commit_plans()
        index = self._require_complete_index(purpose="replay completed semantic decisions")
        if index is None:
            return ()
        replay_records = self._indexed_records(
            kinds={"ingest_run", "commit_plan", "commit_record", "plan_abandoned"}
        )

        starts: dict[str, dict[str, Any]] = {}
        terminal: dict[str, tuple[int, dict[str, Any]]] = {}
        committed: set[str] = set()
        abandoned: set[str] = set()
        current_plan_runs: set[str] = set()
        for position, record in enumerate(replay_records):
            if record.get("ledger_version") not in _ACCEPTED_LEDGER_VERSIONS:
                continue
            run_id = str(record.get("run_id") or "")
            if not run_id:
                continue
            kind = record.get("kind")
            if kind == "ingest_run":
                if record.get("state") == "started":
                    starts[run_id] = record
                else:
                    terminal[run_id] = (position, record)
            elif kind == "commit_plan":
                operations = record.get("operations")
                if (
                    isinstance(operations, list)
                    and all(isinstance(operation, dict) for operation in operations)
                    and all(
                        isinstance(operation.get("operation_id"), str)
                        and bool(operation.get("operation_id"))
                        for operation in operations
                    )
                ):
                    current_plan_runs.add(run_id)
            elif kind == "commit_record":
                result = record.get("result")
                if (
                    isinstance(result, dict)
                    and result.get("operation_receipts_complete") is True
                    and isinstance(result.get("operation_receipts"), int)
                ):
                    committed.add(run_id)
            elif kind == "plan_abandoned":
                abandoned.add(run_id)

        matches: list[str] = []
        for run_id, (_position, completed) in terminal.items():
            started = starts.get(run_id)
            summary = completed.get("summary")
            outcome = summary.get("outcome") if isinstance(summary, dict) else None
            if (
                run_id in committed
                and run_id in current_plan_runs
                and run_id not in abandoned
                and completed.get("state") == "completed"
                and isinstance(outcome, dict)
                and outcome.get("quality") == "complete"
                and outcome.get("receipts_complete") is True
                and str(started.get("document_id") if started else "") == str(document_id)
                and int(started.get("blocks_total") if started else 0) == int(blocks_total)
                and str(started.get("model") if started else "") == str(model)
                and str(started.get("config_fingerprint") if started else "") == config_fingerprint
                and str(started.get("extraction_fingerprint") if started else "")
                == extraction_fingerprint
                and str(started.get("semantic_policy_fingerprint") if started else "")
                == semantic_policy_fingerprint
                and str(started.get("materialization_scope") if started else "")
                == materialization_scope
                and str(completed.get("post_semantic_policy_fingerprint") or "")
                == semantic_policy_fingerprint
            ):
                matches.append(run_id)
        return tuple(matches)

    def unreceipted_commit_plans(
        self,
        *,
        document_id: str | None = None,
    ) -> tuple[CommitPlanSnapshot, ...]:
        """Return sealed plans without a commit receipt, independent of run rows.

        This is the apply-resume lane. A fsynced plan remains authoritative even
        if the earlier buffered ``started`` row was lost or a planned sidefile
        mutation changed the run-start policy fingerprint.
        """

        try:
            self._repair_replayable_tail()
        except ValueError as exc:
            raise ValueError(
                "cannot resume apply from an invalid candidate ledger: " + str(exc)
            ) from exc
        index = self._offset_index()
        if index is not None and index.completeness_status != "complete":
            raise ValueError(
                f"cannot resume apply from an invalid candidate ledger: {index.completeness_reason}"
            )
        plan_records = self._indexed_records(
            kinds={
                "commit_plan",
                "operation_receipt",
                "commit_record",
                "plan_abandoned",
            }
        )

        plans: dict[str, dict[str, Any]] = {}
        plan_positions: dict[str, int] = {}
        order: list[str] = []
        operation_receipt_rows: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        commit_rows: dict[str, tuple[int, dict[str, Any]]] = {}
        abandoned_rows: dict[str, tuple[int, dict[str, Any]]] = {}
        for position, record in enumerate(plan_records):
            plan_id = str(record.get("plan_id") or "")
            if not plan_id:
                continue
            if record.get("kind") == "commit_plan":
                if plan_id in plans:
                    raise ValueError(f"duplicate commit plan id: {plan_id}")
                order.append(plan_id)
                plans[plan_id] = record
                plan_positions[plan_id] = position
            elif record.get("kind") == "operation_receipt":
                if plan_id not in plans:
                    raise ValueError(f"operation receipt precedes or lacks plan: {plan_id}")
                operation_receipt_rows.setdefault(plan_id, []).append((position, record))
            elif record.get("kind") == "commit_record":
                if plan_id not in plans:
                    raise ValueError(f"commit receipt precedes or lacks plan: {plan_id}")
                if plan_id in commit_rows:
                    raise ValueError(f"duplicate commit receipt: {plan_id}")
                if str(record.get("run_id") or "") != str(plans[plan_id].get("run_id") or ""):
                    raise ValueError(f"commit receipt run mismatch: {plan_id}")
                commit_rows[plan_id] = (position, record)
            elif record.get("kind") == "plan_abandoned":
                if plan_id not in plans:
                    raise ValueError(f"abandoned plan precedes or lacks plan: {plan_id}")
                if plan_id in abandoned_rows or plan_id in commit_rows:
                    raise ValueError(f"duplicate terminal plan record: {plan_id}")
                abandoned_rows[plan_id] = (position, record)

        validated: dict[str, CommitPlanSnapshot] = {}
        receipts_by_plan: dict[str, dict[str, tuple[int, dict[str, Any]]]] = {}
        for plan_id in order:
            plan = plans[plan_id]
            run_id = str(plan.get("run_id") or "")
            operations = plan.get("operations")
            context = plan.get("context") or {}
            plan_hash = str(plan.get("plan_hash") or "")
            current_format = (
                isinstance(operations, list)
                and all(isinstance(operation, dict) for operation in operations)
                and all(
                    isinstance(operation.get("operation_id"), str)
                    and bool(operation.get("operation_id"))
                    for operation in operations
                )
            )
            if not current_format:
                if plan_id in commit_rows:
                    # Completed historical plans remain readable. An open legacy
                    # plan cannot be reinterpreted as an executable current plan.
                    continue
                raise ValueError(f"legacy unreceipted commit plan: {plan_id}")
            assert isinstance(operations, list)
            if not run_id or not isinstance(context, dict) or not plan_hash:
                raise ValueError(f"invalid commit plan structure: {plan_id}")
            for operation in operations:
                assert isinstance(operation, dict)
                _validate_plan_operation(operation, operation_id_required=True)
            operation_ids = [str(operation["operation_id"]) for operation in operations]
            if len(operation_ids) != len(set(operation_ids)):
                raise ValueError(f"invalid commit plan operation ids: {plan_id}")
            _validate_plan_operation_set(operations)
            expected_hash = _canonical_plan_hash(run_id, plan_id, operations, context)
            if plan_hash != expected_hash:
                raise ValueError(f"commit plan digest mismatch: {plan_id}")
            snapshot = CommitPlanSnapshot(
                run_id=run_id,
                plan_id=plan_id,
                plan_hash=plan_hash,
                operations=tuple(dict(operation) for operation in operations),
                context=dict(context),
            )
            validated[plan_id] = snapshot
            expected = {
                str(operation.get("operation_id") or ""): str(operation.get("operation") or "")
                for operation in operations
                if isinstance(operation, dict)
            }
            plan_receipts: dict[str, tuple[int, dict[str, Any]]] = {}
            for receipt_position, receipt in operation_receipt_rows.get(plan_id, []):
                operation_id = str(receipt.get("operation_id") or "")
                if operation_id not in expected:
                    raise ValueError(f"invalid operation receipt: {plan_id}/{operation_id}")
                if str(receipt.get("operation") or "") != expected[operation_id]:
                    raise ValueError(f"invalid operation receipt: {plan_id}/{operation_id}")
                if str(receipt.get("run_id") or "") != run_id:
                    raise ValueError(f"invalid operation receipt: {plan_id}/{operation_id}")
                if str(receipt.get("plan_hash") or "") != plan_hash:
                    raise ValueError(f"invalid operation receipt: {plan_id}/{operation_id}")
                if receipt.get("status") not in {
                    "applied",
                    "already_present",
                    "dead_lettered",
                    "failed",
                    "aborted",
                }:
                    raise ValueError(f"invalid operation receipt status: {plan_id}/{operation_id}")
                if not isinstance(receipt.get("result"), dict):
                    raise ValueError(f"invalid operation receipt: {plan_id}/{operation_id}")
                if operation_id in plan_receipts:
                    raise ValueError(f"duplicate operation receipt: {plan_id}/{operation_id}")
                plan_receipts[operation_id] = (receipt_position, receipt)
            receipts_by_plan[plan_id] = plan_receipts

            if plan_id in commit_rows:
                commit_position, commit = commit_rows[plan_id]
                if str(commit.get("plan_hash") or "") != plan_hash:
                    raise ValueError(f"commit receipt hash mismatch: {plan_id}")
                if set(plan_receipts) != set(expected):
                    raise ValueError(f"commit receipt closes an incomplete plan: {plan_id}")
                if any(position >= commit_position for position, _ in plan_receipts.values()):
                    raise ValueError(f"commit receipt precedes an operation receipt: {plan_id}")
                if any(
                    receipt.get("status") in {"failed", "aborted"}
                    for _, receipt in plan_receipts.values()
                ):
                    raise ValueError(f"commit receipt closes a failed plan: {plan_id}")
                result = commit.get("result")
                if (
                    not isinstance(result, dict)
                    or result.get("operation_receipts_complete") is not True
                    or result.get("operation_receipts") != len(expected)
                ):
                    raise ValueError(f"commit receipt lacks closure evidence: {plan_id}")
            if plan_id in abandoned_rows:
                abandoned_position, abandoned = abandoned_rows[plan_id]
                if str(abandoned.get("run_id") or "") != run_id:
                    raise ValueError(f"abandoned plan run mismatch: {plan_id}")
                if str(abandoned.get("plan_hash") or "") != plan_hash:
                    raise ValueError(f"abandoned plan hash mismatch: {plan_id}")
                if abandoned_position <= plan_positions[plan_id]:
                    raise ValueError(f"abandoned plan record precedes its plan: {plan_id}")
                if plan_receipts:
                    raise ValueError(f"abandoned plan has operation receipts: {plan_id}")
                if not str(abandoned.get("reason") or "").strip():
                    raise ValueError(f"abandoned plan lacks a reason: {plan_id}")
                if not isinstance(abandoned.get("evidence"), dict):
                    raise ValueError(f"abandoned plan evidence is invalid: {plan_id}")

        snapshots: list[CommitPlanSnapshot] = []
        for plan_id in order:
            if plan_id in commit_rows or plan_id in abandoned_rows:
                continue
            snapshot = validated[plan_id]
            if document_id is not None and str(snapshot.context.get("document_id") or "") != str(
                document_id
            ):
                continue
            snapshots.append(snapshot)
        return tuple(snapshots)

    def operation_receipts(self, plan: CommitPlanSnapshot) -> dict[str, dict[str, Any]]:
        """Return validated, unique durable receipts for one sealed plan."""

        expected = {
            str(operation["operation_id"]): str(operation["operation"])
            for operation in plan.operations
        }
        receipts: dict[str, dict[str, Any]] = {}
        for record in self._indexed_records(
            kinds={"operation_receipt"},
            run_ids={plan.run_id},
        ):
            if str(record.get("plan_id") or "") != plan.plan_id:
                continue
            if str(record.get("run_id") or "") != plan.run_id:
                raise ValueError("operation receipt run does not match its plan")
            if str(record.get("plan_hash") or "") != plan.plan_hash:
                raise ValueError("operation receipt hash does not match its plan")
            operation_id = str(record.get("operation_id") or "")
            if operation_id not in expected:
                raise ValueError("operation receipt references an unknown operation")
            if str(record.get("operation") or "") != expected[operation_id]:
                raise ValueError("operation receipt discriminator does not match its plan")
            if record.get("status") not in {
                "applied",
                "already_present",
                "dead_lettered",
                "failed",
                "aborted",
            }:
                raise ValueError("invalid operation receipt status")
            if not isinstance(record.get("result"), dict):
                raise ValueError("operation receipt result must be an object")
            if operation_id in receipts:
                raise ValueError("duplicate operation receipt")
            receipts[operation_id] = record
        return receipts

    def resume_snapshot(
        self,
        run_id: str,
        *,
        methods: tuple[str, ...] = (
            "curator",
            "relation_curator",
            # ADR 0040 D6a.6: ingest-time predicate resolution rows. Collected
            # here so a resumed or replayed run reuses the decision instead of
            # re-asking the judge, which is the instability being fixed. These
            # rows are keyed by `predicate:<label>`, not by candidate id, and
            # are read straight off `verdicts_by_method` — `_replayable_verdicts`
            # would drop them (its filter is {"commit", "queue"}).
            "predicate_resolution",
            "resume_replay",
            "policy_replay",
        ),
    ) -> ResumeSnapshot:
        """Resume state for one ``run_id`` (ADR 0015 D5b).

        Collects, in one pass over the ledger file, both the prior curation
        verdicts (per requested method, keyed by candidate_id) and the set of
        candidate-row ids already recorded in the run. Replay methods are part
        of the default because a replayed run is itself immutable decision
        evidence for the next exact-policy materialization.
        """
        return self.resume_snapshots((run_id,), methods=methods)[0]

    def resume_snapshots(
        self,
        run_ids: tuple[str, ...],
        *,
        methods: tuple[str, ...] = (
            "curator",
            "relation_curator",
            # ADR 0040 D6a.6: ingest-time predicate resolution rows. Collected
            # here so a resumed or replayed run reuses the decision instead of
            # re-asking the judge, which is the instability being fixed. These
            # rows are keyed by `predicate:<label>`, not by candidate id, and
            # are read straight off `verdicts_by_method` — `_replayable_verdicts`
            # would drop them (its filter is {"commit", "queue"}).
            "predicate_resolution",
            "resume_replay",
            "policy_replay",
        ),
    ) -> tuple[ResumeSnapshot, ...]:
        """Collect several replay authorities with one ledger scan.

        Returned snapshots preserve ``run_ids`` order. Callers can therefore
        keep the first verdict per candidate authoritative while allowing later
        compatible runs to fill candidate-id gaps.
        """

        if len(set(run_ids)) != len(run_ids):
            raise ValueError("resume snapshot run_ids must be unique")
        wanted_runs = set(run_ids)
        wanted_methods = set(methods)
        verdicts = {run_id: {method: {} for method in methods} for run_id in run_ids}
        candidate_ids = {run_id: set() for run_id in run_ids}
        candidate_records = {run_id: {} for run_id in run_ids}
        index = self._offset_index()
        node_identity_by_id = dict(index.node_identity_by_id) if index is not None else {}
        for record in self._indexed_records(
            kinds={"candidate", "comparison"},
            run_ids=wanted_runs,
        ):
            record_run_id = str(record.get("run_id") or "")
            kind = record.get("kind")
            candidate_id = str(record.get("candidate_id") or "")
            if not candidate_id:
                continue
            if kind == "candidate":
                candidate_ids[record_run_id].add(candidate_id)
                candidate_records[record_run_id][candidate_id] = record
                continue
            if kind != "comparison":
                continue
            method = str(record.get("method") or "")
            if method not in wanted_methods:
                continue
            payload = record.get("payload")
            if isinstance(payload, dict) and payload.get("audit_only") is True:
                continue
            verdicts[record_run_id][method][candidate_id] = record
        return tuple(
            ResumeSnapshot(
                run_id=run_id,
                verdicts_by_method=verdicts[run_id],
                candidate_ids=candidate_ids[run_id],
                candidate_records=candidate_records[run_id],
                node_identity_by_id=dict(node_identity_by_id),
            )
            for run_id in run_ids
        )

    def latest_run_for_candidate(self, candidate_id: str) -> str | None:
        index = self._offset_index()
        return index.latest_candidate_run.get(candidate_id) if index is not None else None

    def run_summaries(self, *, limit: int = 50) -> list[dict[str, Any]]:
        runs: dict[str, dict[str, Any]] = {}
        for record in self.records():
            run_id = str(record.get("run_id") or "")
            if not run_id:
                continue
            row = runs.setdefault(
                run_id,
                {
                    "run_id": run_id,
                    "state": "unknown",
                    "started_at": None,
                    "completed_at": None,
                    "document_id": None,
                    "source": None,
                    "name": None,
                    "blocks_total": 0,
                    "model": None,
                    "summary": {},
                    "counts": {
                        "candidates": 0,
                        "comparisons": 0,
                        "commit_plans": 0,
                        "commit_records": 0,
                    },
                    "_candidate_ids": set(),
                },
            )
            kind = record.get("kind")
            if kind == "ingest_run":
                state = str(record.get("state") or row["state"])
                row["state"] = state
                if state == "started":
                    row["started_at"] = record.get("ts")
                    row["document_id"] = record.get("document_id")
                    row["source"] = record.get("source")
                    row["name"] = os.path.basename(str(record.get("source") or "")) or None
                    row["blocks_total"] = int(record.get("blocks_total") or 0)
                    row["model"] = record.get("model")
                elif state:
                    row["completed_at"] = record.get("ts")
                    row["summary"] = record.get("summary") or {}
                    row["post_semantic_policy_fingerprint"] = record.get(
                        "post_semantic_policy_fingerprint"
                    )
            elif kind == "candidate":
                if record.get("candidate_id"):
                    row["_candidate_ids"].add(str(record.get("candidate_id")))
            elif kind == "comparison":
                row["counts"]["comparisons"] += 1
            elif kind == "commit_plan":
                row["counts"]["commit_plans"] += 1
            elif kind == "commit_record":
                row["counts"]["commit_records"] += 1
            elif kind == "integrity_outcome":
                integrity = record.get("integrity")
                if isinstance(integrity, dict):
                    summary = dict(row.get("summary") or {})
                    outcome = dict(summary.get("outcome") or {})
                    outcome["integrity"] = dict(integrity)
                    if record.get("quality"):
                        outcome["quality"] = str(record["quality"])
                    summary["outcome"] = outcome
                    row["summary"] = summary
                    row["integrity"] = dict(integrity)
        values = []
        for row in runs.values():
            row["counts"]["candidates"] = len(row.pop("_candidate_ids", set()))
            values.append(row)
        ordered = sorted(
            values,
            key=lambda item: str(item.get("started_at") or item.get("completed_at") or ""),
            reverse=True,
        )
        return ordered[:limit]

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        records = [record for record in self.records() if record.get("run_id") == run_id]
        if not records:
            return None
        compact = [_without_heavy_values(record) for record in records]
        candidates = [
            _candidate_summary(record) for record in compact if record.get("kind") == "candidate"
        ]
        return {
            "run": next(
                (row for row in self.run_summaries(limit=500) if row["run_id"] == run_id), None
            ),
            "records": compact,
            "candidates": candidates,
            "comparisons": [r for r in compact if r.get("kind") == "comparison"],
            "commit_plans": [r for r in compact if r.get("kind") == "commit_plan"],
            "commit_records": [r for r in compact if r.get("kind") == "commit_record"],
            "integrity_outcomes": [r for r in compact if r.get("kind") == "integrity_outcome"],
        }

    def run_progress_summary(
        self,
        run_id: str | None = None,
        *,
        limit: int = 12,
    ) -> dict[str, Any] | None:
        """Compact pre-commit progress for UI surfaces.

        The graph only shows committed store contents. During an ADR 0013 ingest,
        semantic candidates live in this ledger until the file-level commit lands.
        This summary exposes that pending layer without returning the heavy LLM
        request/response records that the detail endpoint keeps for audit.
        """
        records = self.records()
        if not records:
            return None

        run: dict[str, Any] | None = None
        if run_id:
            run = next(
                (row for row in self.run_summaries(limit=500) if row["run_id"] == run_id), None
            )
        else:
            run = next(iter(self.run_summaries(limit=1)), None)
        if not run:
            return None

        selected_run_id = str(run.get("run_id") or "")
        run_records = [
            record for record in records if str(record.get("run_id") or "") == selected_run_id
        ]
        if not run_records:
            return None

        unique_candidate_ids_by_kind: dict[str, set[str]] = {}
        node_by_id: dict[str, dict[str, Any]] = {}
        node_state_by_id: dict[str, str] = {}
        final_node_verdict_by_id: dict[str, str] = {}
        node_types_by_verdict: dict[str, Counter[str]] = {}
        node_titles_by_verdict: dict[str, list[str]] = {}
        node_samples_by_verdict: dict[str, list[dict[str, Any]]] = {}
        relation_terminal_by_verdict: dict[str, Counter[str]] = {}
        accepted_relation_predicates: Counter[str] = Counter()
        queued_relation_predicates: Counter[str] = Counter()
        relation_samples_by_verdict: dict[str, list[dict[str, Any]]] = {}
        comparison_methods: Counter[str] = Counter()
        comparison_verdicts: Counter[str] = Counter()
        audit_modes: Counter[str] = Counter()
        durations_by_method: dict[str, list[float]] = {}
        tokens_by_method: dict[str, dict[str, int]] = {}
        active_node_total: int | None = None
        active_edge_total: int | None = None
        # ADR 0039 T9: which comparison method last revised each denominator.
        active_node_total_source: str | None = None
        active_edge_total_source: str | None = None

        candidate_rows = 0
        comparison_rows = 0
        commit_plans = 0
        commit_records = 0
        for record in run_records:
            kind = str(record.get("kind") or "")
            candidate_id = str(record.get("candidate_id") or "")
            if kind == "candidate":
                candidate_rows += 1
                candidate_kind = str(record.get("candidate_kind") or "unknown")
                if candidate_id:
                    unique_candidate_ids_by_kind.setdefault(candidate_kind, set()).add(candidate_id)
                if candidate_kind == "node":
                    if candidate_id and candidate_id not in node_by_id:
                        node_by_id[candidate_id] = _node_candidate_payload(record)
                    if candidate_id:
                        node_state_by_id[candidate_id] = str(record.get("state") or "unknown")
                continue
            if kind == "commit_plan":
                commit_plans += 1
                continue
            if kind == "commit_record":
                commit_records += 1
                continue
            if kind != "comparison":
                continue

            comparison_rows += 1
            method = str(record.get("method") or "unknown")
            verdict = str(record.get("verdict") or "unknown")
            comparison_methods[method] += 1
            comparison_verdicts[verdict] += 1
            payload = record.get("payload") or {}
            if isinstance(payload, dict):
                duration_s = payload.get("duration_s")
                if isinstance(duration_s, (int, float)):
                    durations_by_method.setdefault(method, []).append(float(duration_s))
                usage = payload.get("usage")
                if isinstance(usage, dict):
                    totals = tokens_by_method.setdefault(method, {})
                    for key, value in usage.items():
                        if isinstance(value, int):
                            totals[key] = totals.get(key, 0) + value
            if isinstance(payload, dict) and payload.get("audit_only") is True:
                mode = "llm_skipped" if payload.get("llm_skipped") is True else "llm_reviewed"
                audit_modes[mode] += 1
                continue
            if not isinstance(payload, dict):
                payload = {}
            after = payload.get("after")
            if isinstance(after, dict):
                if isinstance(after.get("nodes"), int):
                    active_node_total = int(after["nodes"])
                    active_node_total_source = method
                if isinstance(after.get("edges"), int):
                    active_edge_total = int(after["edges"])
                    active_edge_total_source = method
            survivors = payload.get("survivors")
            if isinstance(survivors, list):
                active_node_total = len(survivors)
                active_node_total_source = method
            nodes = payload.get("nodes")
            if isinstance(nodes, list):
                active_node_total = len(nodes)
                active_node_total_source = method
            edges = payload.get("edges")
            if isinstance(edges, list):
                active_edge_total = len(edges)
                active_edge_total_source = method

            if method == "curator":
                final_node_verdict_by_id[candidate_id] = verdict
                node_payload = node_by_id.get(candidate_id) or {}
                node_type = str(node_payload.get("type") or "unknown")
                title = str(node_payload.get("title") or candidate_id)
                node_types_by_verdict.setdefault(verdict, Counter())[node_type] += 1
                node_titles_by_verdict.setdefault(verdict, []).append(title)
                node_samples = node_samples_by_verdict.setdefault(verdict, [])
                if len(node_samples) < limit:
                    node_samples.append(
                        {
                            "candidate_id": candidate_id,
                            "type": node_type,
                            "title": title,
                        }
                    )
            elif method == "relation_curator":
                terminal = str(payload.get("proposed_terminal_action") or "unknown")
                relation_terminal_by_verdict.setdefault(verdict, Counter())[terminal] += 1
                predicate = str(
                    payload.get("canonical_predicate") or payload.get("type") or "unknown"
                )
                if verdict == "commit" and terminal == "create_edge_or_claim":
                    accepted_relation_predicates[predicate] += 1
                elif verdict != "commit":
                    queued_relation_predicates[predicate] += 1
                relation_samples = relation_samples_by_verdict.setdefault(verdict, [])
                if len(relation_samples) < limit:
                    relation_samples.append(
                        _relation_sample(
                            candidate_id,
                            payload,
                            predicate=predicate,
                            terminal=terminal,
                            node_by_id=node_by_id,
                        )
                    )

        committed_node_ids = {
            candidate_id
            for candidate_id, verdict in final_node_verdict_by_id.items()
            if verdict == "commit" and node_state_by_id.get(candidate_id) != "superseded"
        }
        queued_node_ids = {
            candidate_id
            for candidate_id, verdict in final_node_verdict_by_id.items()
            if verdict != "commit"
        }
        relation_commit_terminals = relation_terminal_by_verdict.get("commit") or Counter()
        relation_queue_terminals: Counter[str] = Counter()
        for verdict, counts in relation_terminal_by_verdict.items():
            if verdict != "commit":
                relation_queue_terminals.update(counts)

        candidate_counts = {
            kind: len(ids) for kind, ids in sorted(unique_candidate_ids_by_kind.items())
        }
        active_candidate_counts = dict(candidate_counts)
        if active_node_total is not None:
            active_candidate_counts["node"] = active_node_total
        if active_edge_total is not None:
            active_candidate_counts["edge"] = active_edge_total
        node_total = active_candidate_counts.get("node", 0)
        edge_total = active_candidate_counts.get("edge", 0)
        # ADR 0039 T9: prefilter/dedup legitimately shrink the live population.
        # Publish that as a declared revision instead of letting the
        # denominator silently move under the same percentage.
        node_population_revision = (
            _population_revision(
                previous_total=candidate_counts.get("node", 0),
                total=node_total,
                reason="active_population_recomputed",
                source=active_node_total_source,
            )
            if active_node_total is not None and node_total != candidate_counts.get("node", 0)
            else None
        )
        edge_population_revision = (
            _population_revision(
                previous_total=candidate_counts.get("edge", 0),
                total=edge_total,
                reason="active_population_recomputed",
                source=active_edge_total_source,
            )
            if active_edge_total is not None and edge_total != candidate_counts.get("edge", 0)
            else None
        )
        node_curator_done = (
            len(final_node_verdict_by_id)
            if active_node_total is not None
            else int(comparison_methods.get("curator") or 0)
        )
        relation_curator_done = (
            sum(sum(counts.values()) for counts in relation_terminal_by_verdict.values())
            if active_edge_total is not None
            else int(comparison_methods.get("relation_curator") or 0)
        )
        return {
            "run": run,
            "counts": {
                "candidates": sum(candidate_counts.values()),
                "candidate_rows": candidate_rows,
                "comparisons": comparison_rows,
                "commit_plans": commit_plans,
                "commit_records": commit_records,
            },
            "candidate_kinds": candidate_counts,
            "active_candidate_kinds": active_candidate_counts,
            "comparison_methods": _top_counts(comparison_methods, limit=limit),
            "comparison_verdicts": _top_counts(comparison_verdicts, limit=limit),
            "audit_modes": _top_counts(audit_modes, limit=limit),
            "llm_timing": {
                method: _timing_stats(values, tokens=tokens_by_method.get(method))
                for method, values in sorted(durations_by_method.items())
            },
            "progress": {
                "node_curator": _progress(
                    node_curator_done,
                    node_total,
                    population="node_candidates",
                    revision=node_population_revision,
                ),
                "relation_curator": _progress(
                    relation_curator_done,
                    edge_total,
                    population="edge_candidates",
                    revision=edge_population_revision,
                ),
            },
            "pending_commit_preview": {
                "nodes": {
                    "accepted_for_write": len(committed_node_ids),
                    "queued_or_abstained": len(queued_node_ids),
                    "types_by_verdict": {
                        verdict: _top_counts(counts, limit=limit)
                        for verdict, counts in sorted(node_types_by_verdict.items())
                    },
                    "sample_titles_by_verdict": {
                        verdict: sorted(titles, key=str.casefold)[:limit]
                        for verdict, titles in sorted(node_titles_by_verdict.items())
                    },
                    "sample_candidates_by_verdict": {
                        verdict: rows for verdict, rows in sorted(node_samples_by_verdict.items())
                    },
                },
                "relations": {
                    "accepted_for_write": int(
                        relation_commit_terminals.get("create_edge_or_claim") or 0
                    ),
                    "canonicalized_originals": int(
                        relation_commit_terminals.get("canonicalize_predicate") or 0
                    ),
                    "endpoint_dead_letters": int(relation_commit_terminals.get("dead_letter") or 0),
                    "queued_or_abstained": sum(relation_queue_terminals.values()),
                    "terminals_by_verdict": {
                        verdict: _top_counts(counts, limit=limit)
                        for verdict, counts in sorted(relation_terminal_by_verdict.items())
                    },
                    "accepted_predicates": _top_counts(accepted_relation_predicates, limit=limit),
                    "queued_predicates": _top_counts(queued_relation_predicates, limit=limit),
                    "sample_relations_by_verdict": {
                        verdict: rows
                        for verdict, rows in sorted(relation_samples_by_verdict.items())
                    },
                },
            },
        }


def _node_ref_summary(ref: Any, node_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ref_text = str(ref or "")
    node = node_by_id.get(ref_text) or {}
    return {
        "ref": ref_text,
        "type": str(node.get("type") or ""),
        "title": str(node.get("title") or ref_text),
    }


def _relation_sample(
    candidate_id: str,
    payload: dict[str, Any],
    *,
    predicate: str,
    terminal: str,
    node_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    sample: dict[str, Any] = {
        "candidate_id": candidate_id,
        "predicate": predicate,
        "raw_predicate": str(payload.get("type") or ""),
        "terminal_action": terminal,
        "relation_kind": str(payload.get("relation_kind") or ""),
        "subject": _node_ref_summary(payload.get("src_ref"), node_by_id),
        "object": None,
    }
    if payload.get("dst_literal") is not None:
        sample["object"] = {"literal": payload.get("dst_literal")}
    else:
        sample["object"] = _node_ref_summary(payload.get("dst_ref"), node_by_id)
    return sample


__all__ = [
    "CommitPlanSnapshot",
    "CandidateLedger",
    "LEDGER_FILENAME",
    "LedgerCompleteness",
    "LedgerScanResult",
    "MalformedLedgerLine",
    "ResumeSnapshot",
    "edge_candidate_id",
]
