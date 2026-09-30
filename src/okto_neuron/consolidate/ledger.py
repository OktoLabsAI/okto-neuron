"""Durable candidate ledger for the pre-commit curation boundary.

The ledger is vault-local process state under ``<vault>/.marginalia``. It is
not a graph primitive and it does not drive graph writes; it records what the
existing ``remember`` pipeline already decided so the hidden middle becomes
inspectable and replayable enough for future tooling.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import threading
import uuid
from collections import Counter, OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AnyStr, Callable, Iterable, Iterator, Literal

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX
    msvcrt = None  # type: ignore[assignment]

LEDGER_FILENAME = "candidate-ledger.jsonl"
_LOG = logging.getLogger(__name__)
# A crash-torn tail is copied under the lock so scan() can report it exactly; a tail
# bigger than this (one absurd row) is reported but not read.
_SCAN_TAIL_CAP = 16 * 1024 * 1024
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


_STREAM_CHUNK_BYTES = 1 << 20


def _stream_lines(chunks: Iterable[AnyStr]) -> Iterator[AnyStr]:
    """Yield the lines of a chunked ``bytes`` or ``str`` stream without holding it.

    The result equals ``"".join(chunks).splitlines()`` for any chunking (``bytes``
    break on ``\\n``/``\\r``/``\\r\\n`` only; ``str`` on the wider set). A terminator
    that could still be extended by the next chunk (a trailing ``\\r`` that may pair
    with a leading ``\\n``) and an unterminated tail are held back, so peak memory
    is one chunk plus the longest line.
    """

    pending: list[AnyStr] = []
    for chunk in chunks:
        if not chunk:
            continue
        cr: AnyStr = b"\r" if isinstance(chunk, bytes) else "\r"  # type: ignore[assignment]
        pieces = chunk.splitlines(keepends=True)
        if pending:
            if pending[-1].endswith(cr):
                pieces = (chunk[:0].join(pending) + chunk).splitlines(keepends=True)
                pending = []
            elif len(pieces) == 1 and pieces[0].splitlines()[0] == pieces[0]:
                pending.append(chunk)  # still inside one long line; do not re-join
                continue
            else:
                pieces[0] = chunk[:0].join(pending) + pieces[0]
                pending = []
        last = pieces[-1]
        if last.splitlines()[0] == last or last.endswith(cr):
            pending.append(pieces.pop())
        for piece in pieces:
            yield piece.splitlines()[0]
    if pending:
        yield from pending[0][:0].join(pending).splitlines()


def _read_chunks(handle: Any, size: int) -> Iterator[Any]:
    while True:
        chunk = handle.read(size)
        if not chunk:
            return
        yield chunk


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
    """Validated, memory-bounded routing metadata for the ledger prefix ``[0, cut)``.

    ``signature`` is ``(device, inode, cut, anchor)`` where ``anchor`` is the last
    ``_INDEX_ANCHOR_BYTES`` bytes below ``cut``. The index stays valid while the
    same file still holds those bytes, however much has been appended beyond
    ``cut``; rows beyond it are folded in by a short tail merge.
    """

    signature: tuple[int, int, int, bytes]
    offsets_by_kind: dict[str, list[tuple[int, int]]]
    offsets_by_run_kind: dict[tuple[str, str], list[tuple[int, int]]]
    node_identity_by_id: dict[str, tuple[str, str]]
    ambiguous_node_ids: set[str]
    latest_candidate_run: dict[str, str]
    completeness_status: LedgerCompleteness
    completeness_reason: str
    malformed: int = 0
    unrecognized_versions: int = 0


@dataclass
class _Snapshot:
    """A pinned ledger prefix: ``[0, cut)`` is read from ``handle``; ``tail`` is ``[cut, size)``."""

    handle: Any
    size: int
    cut: int
    tail: bytes
    tail_skipped: bool = False  # the torn tail exceeded _SCAN_TAIL_CAP and was not copied


class _PrefixReader(io.RawIOBase):
    """A read-only raw stream over ``handle[0:cut]`` followed by ``tail``."""

    def __init__(self, snapshot: _Snapshot) -> None:
        self._handle = snapshot.handle
        self._handle.seek(0)
        self._left = snapshot.cut
        self._tail = memoryview(snapshot.tail)

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        if self._left > 0:
            chunk = self._handle.read(min(len(buffer), self._left))
            if chunk:
                buffer[: len(chunk)] = chunk
                self._left -= len(chunk)
                return len(chunk)
            self._left = 0
        count = min(len(buffer), len(self._tail))
        buffer[:count] = self._tail[:count]
        self._tail = self._tail[count:]
        return count


def _last_line_end(handle: Any, size: int) -> int:
    """Byte offset just past the last newline at or below ``size`` (0 if none)."""
    position = size
    while position > 0:
        start = max(0, position - 65536)
        handle.seek(start)
        block = handle.read(position - start)
        found = block.rfind(b"\n")
        if found >= 0:
            return start + found + 1
        position = start
    return 0


_INDEX_ANCHOR_BYTES = 4096


def _read_anchor(fd: int, cut: int) -> bytes:
    """The last bytes below ``cut``, read unbuffered so they are never a stale copy."""
    start = max(0, cut - _INDEX_ANCHOR_BYTES)
    return os.pread(fd, cut - start, start)


def _prefix_signature(handle: Any, cut: int) -> tuple[int, int, int, bytes]:
    stat = os.fstat(handle.fileno())
    return (stat.st_dev, stat.st_ino, cut, _read_anchor(handle.fileno(), cut))


def _prefix_holds(signature: tuple[int, int, int, bytes], handle: Any) -> bool:
    """True when the open file still starts with the prefix ``signature`` describes."""
    dev, ino, cut, anchor = signature
    stat = os.fstat(handle.fileno())
    return (
        stat.st_dev == dev
        and stat.st_ino == ino
        and stat.st_size >= cut
        and _read_anchor(handle.fileno(), cut) == anchor
    )


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


def _ingest_index_row(index: _LedgerOffsetIndex, offset: int, raw: bytes) -> None:
    if not raw.strip():
        return
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        index.malformed += 1
        return
    if not isinstance(record, dict):
        index.malformed += 1
        return
    version = record.get("ledger_version")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version not in _ACCEPTED_LEDGER_VERSIONS
    ):
        index.unrecognized_versions += 1
    kind = str(record.get("kind") or "")
    if not kind:
        return
    location = (offset, len(raw))
    index.offsets_by_kind.setdefault(kind, []).append(location)
    run_id = str(record.get("run_id") or "")
    if run_id:
        index.offsets_by_run_kind.setdefault((run_id, kind), []).append(location)
    if kind == "candidate":
        _index_candidate_identity(index, record)


def _refresh_index_completeness(index: _LedgerOffsetIndex) -> None:
    reasons: list[str] = []
    if index.malformed:
        reasons.append("malformed_ledger_lines")
    if index.unrecognized_versions:
        reasons.append("unrecognized_ledger_versions")
    if reasons:
        index.completeness_status = "incomplete"
        index.completeness_reason = "+".join(reasons)
    else:
        index.completeness_status = "complete"
        index.completeness_reason = (
            "all_nonempty_lines_parsed" if any(index.offsets_by_kind.values()) else "empty_ledger"
        )


def _merge_index_tail(index: _LedgerOffsetIndex, path: Path) -> None:
    """Fold every row from ``index``'s cut to the end of the file into ``index``.

    The ledger lock must be held (the tail then cannot change underneath). A fresh
    handle is used on purpose: a handle opened earlier may hold read-ahead bytes
    from above the cut (a crash-torn tail that an append can since have replaced).
    """
    cut = index.signature[2]
    with path.open("rb") as handle:
        handle.seek(cut)
        while True:
            offset = handle.tell()
            raw = handle.readline()
            if not raw:
                break
            _ingest_index_row(index, offset, raw)
        index.signature = _prefix_signature(handle, handle.tell())
    _refresh_index_completeness(index)


_CANCEL_CHECK_BYTES = 4 * 1024 * 1024  # how often a cancellable build looks at its flag


class _BuildCancelled(Exception):
    """A background index build was asked to stop; its partial state is discarded."""


_BUILD_CANCEL = threading.local()  # set only by CandidateLedger.prewarm, per thread


def _raise_if_cancelled() -> None:
    cancelled = getattr(_BUILD_CANCEL, "fn", None)
    if cancelled is not None and cancelled():
        raise _BuildCancelled


def _build_ledger_offset_index(directory: Path, path: Path) -> _LedgerOffsetIndex:
    index = _LedgerOffsetIndex(
        signature=(0, 0, 0, b""),
        offsets_by_kind={},
        offsets_by_run_kind={},
        node_identity_by_id={},
        ambiguous_node_ids=set(),
        latest_candidate_run={},
        completeness_status="complete",
        completeness_reason="empty_ledger",
    )

    # Snapshot under the lock, stream the bulk without it, then take the lock only
    # to fold in what was appended meanwhile. The file is append-only, so bytes
    # below the snapshot never change under the open handle.
    lock = directory / ".candidate-ledger.lock"
    with _exclusive_lock(lock):
        handle = path.open("rb")
        cut = _last_line_end(handle, os.fstat(handle.fileno()).st_size)
    try:
        handle.seek(0)
        next_check = _CANCEL_CHECK_BYTES
        while handle.tell() < cut:
            offset = handle.tell()
            if offset >= next_check:
                next_check = offset + _CANCEL_CHECK_BYTES
                _raise_if_cancelled()
            raw = handle.readline()
            if not raw:
                break
            _ingest_index_row(index, offset, raw)
        # The index so far covers exactly [0, cut): its signature says so even while
        # appends keep landing; the tail merge below advances it.
        index.signature = _prefix_signature(handle, cut)
    except BaseException:
        handle.close()
        raise
    handle.close()
    with _exclusive_lock(lock):
        _merge_index_tail(index, path)
    return index


def _invalidate_ledger_offset_index(path: Path) -> None:
    with _LEDGER_INDEXES_GUARD:
        _LEDGER_INDEXES.pop(str(path.resolve()), None)


def _pin_ledger_offset_index(
    directory: Path, path: Path
) -> tuple[_LedgerOffsetIndex, int, Any] | None:
    """Return ``(index, cut, handle)``: the offset index and an open handle on the ledger.

    Every indexed row below ``cut`` lies in an immutable prefix of the ledger that
    ``handle`` reads without any lock. The index is brought up to date with the
    file by a short tail merge under the ledger lock (or rebuilt when the file no
    longer starts with the prefix it was built from), so an append landing while a
    reader works can only add rows beyond ``cut``; it never invalidates the
    snapshot. ``None`` when the ledger does not exist. The caller closes ``handle``.
    """
    key = str(path.resolve())
    lock = directory / ".candidate-ledger.lock"
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return None
    try:
        with _LEDGER_INDEXES_GUARD:
            cached = _LEDGER_INDEXES.get(key)
            build_lock = _LEDGER_INDEX_BUILD_LOCKS.setdefault(key, threading.Lock())
        if cached is not None:
            signature = cached.signature  # one atomic read: cut and anchor agree
            if (
                _prefix_holds(signature, handle)
                and os.fstat(handle.fileno()).st_size == signature[2]
            ):
                with _LEDGER_INDEXES_GUARD:
                    if _LEDGER_INDEXES.get(key) is cached:
                        _LEDGER_INDEXES.move_to_end(key)
                return cached, signature[2], handle
        with build_lock:
            with _LEDGER_INDEXES_GUARD:
                cached = _LEDGER_INDEXES.get(key)
            if cached is not None:
                with _exclusive_lock(lock):
                    size = os.fstat(handle.fileno()).st_size
                    if (
                        _prefix_holds(cached.signature, handle)
                        and size - cached.signature[2] <= _SIDECAR_INLINE_TAIL
                    ):
                        try:
                            # Ledger lock held: no append (hence no in-place extension
                            # of this index) can run concurrently.
                            _merge_index_tail(cached, path)
                        except BaseException:
                            _invalidate_ledger_offset_index(path)  # half-merged: never reuse
                            raise
                        with _LEDGER_INDEXES_GUARD:
                            _LEDGER_INDEXES.move_to_end(key)
                        return cached, cached.signature[2], handle
            built = _build_ledger_offset_index(directory, path)
            cut = built.signature[2]  # the snapshot this reader holds
            with _LEDGER_INDEXES_GUARD:
                _LEDGER_INDEXES[key] = built
                _LEDGER_INDEXES.move_to_end(key)
                while len(_LEDGER_INDEXES) > _LEDGER_INDEX_CACHE_SIZE:
                    _LEDGER_INDEXES.popitem(last=False)
            return built, cut, handle
    except BaseException:
        handle.close()
        raise


def _get_ledger_offset_index(directory: Path, path: Path) -> _LedgerOffsetIndex | None:
    pinned = _pin_ledger_offset_index(directory, path)
    if pinned is None:
        return None
    index, _cut, handle = pinned
    handle.close()
    return index


def _extend_ledger_offset_index(
    path: Path,
    *,
    offset: int,
    encoded: bytes,
    record: dict[str, Any],
) -> None:
    """Fold a row this process just appended (ledger lock held) into a cached index.

    Only an index that ends exactly where the row starts is extended; one that lags
    (another process appended) is left alone: readers catch it up from the file.
    """
    key = str(path.resolve())
    with _LEDGER_INDEXES_GUARD:
        index = _LEDGER_INDEXES.get(key)
        if index is None:
            return
        dev, ino, cut, anchor = index.signature
        if cut != offset:
            return
        kind = str(record.get("kind") or "")
        location = (offset, len(encoded))
        index.offsets_by_kind.setdefault(kind, []).append(location)
        run_id = str(record.get("run_id") or "")
        if run_id:
            index.offsets_by_run_kind.setdefault((run_id, kind), []).append(location)
        if kind == "candidate":
            _index_candidate_identity(index, record)
        index.signature = (
            dev,
            ino,
            cut + len(encoded),
            (anchor + encoded)[-_INDEX_ANCHOR_BYTES:],
        )
        _LEDGER_INDEXES.move_to_end(key)


# ---------------------------------------------------------------------------
# Ledger index sidecar (issue #14b)
#
# ``candidate-ledger.jsonl.index`` is a rebuildable cache next to the ledger: the
# per-run byte spans and sort keys the run APIs need, plus the open-plan set
# (plans with a sealed ``commit_plan`` and no terminal row). It is NEVER a source
# of truth: every fact in it is derivable from the ledger by one streaming scan,
# deleting it is always safe, and any doubt (missing, corrupt, other version,
# shorter ledger, tail bytes that no longer match) rebuilds it from the ledger.
# The ledger format is untouched.
# ---------------------------------------------------------------------------

LEDGER_INDEX_FILENAME = LEDGER_FILENAME + ".index"
_SIDECAR_VERSION = 1
_SIDECAR_ANCHOR_BYTES = 4096
# Persist at least this often (bytes of ledger covered since the last write).
# Appends update the in-memory state under the ledger lock; a crash loses only
# what the next open re-reads from the ledger tail.
_SIDECAR_CHECKPOINT_BYTES = 4 * 1024 * 1024
_SIDECAR_CACHE_SIZE = 8
_SIDECAR_INLINE_TAIL = 8 * 1024 * 1024  # catch up under the lock only up to this much
_SIDECARS_GUARD = threading.Lock()
_SNAPSHOT_FAILURES: OrderedDict[str, tuple[tuple[int, str], str]] = OrderedDict()
_SNAPSHOT_FAILURES_GUARD = threading.Lock()
_SNAPSHOT_FAILURES_MAX = 64


def _snapshot_signature(handle: Any, cut: int) -> tuple[int, str]:
    """``(cut, sha256 of the last anchor-sized block below cut)``: names a ledger prefix."""
    start = max(0, cut - _SIDECAR_ANCHOR_BYTES)
    handle.seek(start)
    return cut, hashlib.sha256(handle.read(cut - start)).hexdigest()


def _snapshot_failure_record(key: str, signature: tuple[int, str], reason: str) -> None:
    with _SNAPSHOT_FAILURES_GUARD:
        _SNAPSHOT_FAILURES[key] = (signature, reason)
        _SNAPSHOT_FAILURES.move_to_end(key)
        while len(_SNAPSHOT_FAILURES) > _SNAPSHOT_FAILURES_MAX:
            _SNAPSHOT_FAILURES.popitem(last=False)


def _snapshot_failure_reason(key: str, signature: tuple[int, str]) -> str | None:
    """The remembered reason if this exact prefix already failed, else ``None``."""
    with _SNAPSHOT_FAILURES_GUARD:
        entry = _SNAPSHOT_FAILURES.get(key)
    return entry[1] if entry is not None and entry[0] == signature else None


def _snapshot_failure_current(key: str) -> str | None:
    with _SNAPSHOT_FAILURES_GUARD:
        entry = _SNAPSHOT_FAILURES.get(key)
    return entry[1] if entry is not None else None


def _snapshot_failure_clear(key: str) -> None:
    with _SNAPSHOT_FAILURES_GUARD:
        _SNAPSHOT_FAILURES.pop(key, None)


_SIDECARS: OrderedDict[str, "_Sidecar"] = OrderedDict()
_PLAN_ROW_KINDS = frozenset({"commit_plan", "operation_receipt", "commit_record", "plan_abandoned"})
_RECEIPT_STATUSES = frozenset({"applied", "already_present", "dead_lettered", "failed", "aborted"})


def _accepted_version(record: dict[str, Any]) -> bool:
    version = record.get("ledger_version")
    return (
        isinstance(version, int)
        and not isinstance(version, bool)
        and version in _ACCEPTED_LEDGER_VERSIONS
    )


def _is_current_plan_row(record: dict[str, Any]) -> bool:
    operations = record.get("operations")
    return (
        isinstance(operations, list)
        and all(isinstance(operation, dict) for operation in operations)
        and all(
            isinstance(operation.get("operation_id"), str) and bool(operation.get("operation_id"))
            for operation in operations
        )
    )


def _validate_plan_group(
    plan_id: str,
    plan: dict[str, Any],
    plan_position: int,
    receipt_rows: list[tuple[int, dict[str, Any]]],
    commit: tuple[int, dict[str, Any]] | None,
    abandoned: tuple[int, dict[str, Any]] | None,
) -> CommitPlanSnapshot | None:
    """Validate one sealed plan with its receipts and terminal row.

    ``None`` means a completed historical (pre-operation-id) plan, which stays
    readable but is never executable. Positions are ledger byte offsets: only
    their order matters. Raises the ``ValueError`` the whole-ledger validation
    has always raised for the same damage.
    """

    run_id = str(plan.get("run_id") or "")
    operations = plan.get("operations")
    context = plan.get("context") or {}
    plan_hash = str(plan.get("plan_hash") or "")
    if not _is_current_plan_row(plan):
        if commit is not None:
            # Completed historical plans remain readable. An open legacy
            # plan cannot be reinterpreted as an executable current plan.
            return None
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
    expected = {
        str(operation.get("operation_id") or ""): str(operation.get("operation") or "")
        for operation in operations
        if isinstance(operation, dict)
    }
    plan_receipts: dict[str, tuple[int, dict[str, Any]]] = {}
    for receipt_position, receipt in receipt_rows:
        operation_id = str(receipt.get("operation_id") or "")
        if operation_id not in expected:
            raise ValueError(f"invalid operation receipt: {plan_id}/{operation_id}")
        if str(receipt.get("operation") or "") != expected[operation_id]:
            raise ValueError(f"invalid operation receipt: {plan_id}/{operation_id}")
        if str(receipt.get("run_id") or "") != run_id:
            raise ValueError(f"invalid operation receipt: {plan_id}/{operation_id}")
        if str(receipt.get("plan_hash") or "") != plan_hash:
            raise ValueError(f"invalid operation receipt: {plan_id}/{operation_id}")
        if receipt.get("status") not in _RECEIPT_STATUSES:
            raise ValueError(f"invalid operation receipt status: {plan_id}/{operation_id}")
        if not isinstance(receipt.get("result"), dict):
            raise ValueError(f"invalid operation receipt: {plan_id}/{operation_id}")
        if operation_id in plan_receipts:
            raise ValueError(f"duplicate operation receipt: {plan_id}/{operation_id}")
        plan_receipts[operation_id] = (receipt_position, receipt)

    if commit is not None:
        commit_position, commit_row = commit
        if str(commit_row.get("plan_hash") or "") != plan_hash:
            raise ValueError(f"commit receipt hash mismatch: {plan_id}")
        if set(plan_receipts) != set(expected):
            raise ValueError(f"commit receipt closes an incomplete plan: {plan_id}")
        if any(position >= commit_position for position, _ in plan_receipts.values()):
            raise ValueError(f"commit receipt precedes an operation receipt: {plan_id}")
        if any(
            receipt.get("status") in {"failed", "aborted"} for _, receipt in plan_receipts.values()
        ):
            raise ValueError(f"commit receipt closes a failed plan: {plan_id}")
        result = commit_row.get("result")
        if (
            not isinstance(result, dict)
            or result.get("operation_receipts_complete") is not True
            or result.get("operation_receipts") != len(expected)
        ):
            raise ValueError(f"commit receipt lacks closure evidence: {plan_id}")
    if abandoned is not None:
        abandoned_position, abandoned_row = abandoned
        if str(abandoned_row.get("run_id") or "") != run_id:
            raise ValueError(f"abandoned plan run mismatch: {plan_id}")
        if str(abandoned_row.get("plan_hash") or "") != plan_hash:
            raise ValueError(f"abandoned plan hash mismatch: {plan_id}")
        if abandoned_position <= plan_position:
            raise ValueError(f"abandoned plan record precedes its plan: {plan_id}")
        if plan_receipts:
            raise ValueError(f"abandoned plan has operation receipts: {plan_id}")
        if not str(abandoned_row.get("reason") or "").strip():
            raise ValueError(f"abandoned plan lacks a reason: {plan_id}")
        if not isinstance(abandoned_row.get("evidence"), dict):
            raise ValueError(f"abandoned plan evidence is invalid: {plan_id}")
    return snapshot


class _RowReader:
    """Read single ledger rows by ``(offset, length)``; opens the file on first use."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: Any = None

    def __call__(self, offset: int, length: int) -> dict[str, Any]:
        if self._handle is None:
            self._handle = self._path.open("rb")
        self._handle.seek(offset)
        raw = self._handle.read(length)
        try:
            row = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"unreadable ledger row at byte {offset}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"unreadable ledger row at byte {offset}")
        return row

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


@dataclass
class _Sidecar:
    """In-memory form of the index sidecar (see the block comment above)."""

    size: int = 0  # ledger bytes covered: always ends on a newline
    tail: bytes = b""  # last <= _SIDECAR_ANCHOR_BYTES covered bytes
    records: int = 0  # parseable object rows
    malformed: int = 0  # non-blank rows that are not JSON objects
    unrecognized: int = 0  # object rows with a missing or unknown ledger_version
    exotic: int = 0  # rows the whole-file reader splits or decodes differently
    anomaly: bool = False  # plan bookkeeping needs the whole-ledger validation
    runs: dict[str, dict[str, Any]] | None = None
    plan_runs: set[str] | None = None
    open_plans: dict[str, dict[str, Any]] | None = None
    closed: dict[str, str] | None = None  # plan id -> "c" (committed) | "a" (abandoned)
    next_order: int = 0
    persisted_size: int = -1  # ledger bytes covered by the sidecar file on disk
    uncovered: int = 0  # ledger bytes past ``size`` (an unterminated tail), set by sync
    loaded_anchor: tuple[int, str] | None = None  # from disk; verified once, then dropped

    def __post_init__(self) -> None:
        self.runs = {} if self.runs is None else self.runs
        self.plan_runs = set() if self.plan_runs is None else self.plan_runs
        self.open_plans = {} if self.open_plans is None else self.open_plans
        self.closed = {} if self.closed is None else self.closed

    # -- derived ----------------------------------------------------------
    def anchor(self) -> tuple[int, str]:
        return len(self.tail), hashlib.sha256(self.tail).hexdigest()

    def completeness_reason(self) -> str:
        reasons = []
        if self.malformed:
            reasons.append("malformed_ledger_lines")
        if self.unrecognized:
            reasons.append("unrecognized_ledger_versions")
        return "+".join(reasons)

    def run_order(self) -> list[str]:
        """Run ids newest first, exactly as ``run_summaries`` orders its rows."""
        assert self.runs is not None
        return [
            run_id
            for run_id, _ in sorted(
                self.runs.items(),
                key=lambda item: str(item[1]["started_at"] or item[1]["completed_at"] or ""),
                reverse=True,
            )
        ]

    # -- maintenance ------------------------------------------------------
    def advance(self, raw: bytes) -> None:
        self.size += len(raw)
        self.tail = (self.tail + raw)[-_SIDECAR_ANCHOR_BYTES:]

    def ingest_raw(self, raw: bytes, read_row: _RowReader) -> None:
        """Account for one newline-terminated ledger line read from disk."""
        offset = self.size
        self.advance(raw)
        if not raw.strip():
            return
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            self.malformed += 1
            self.exotic += 1
            return
        if len(text.rstrip("\n").splitlines()) > 1:
            self.exotic += 1  # str.splitlines() would split this row
        try:
            record = json.loads(text)
        except ValueError:
            self.malformed += 1
            return
        if not isinstance(record, dict):
            self.malformed += 1
            return
        self.apply(offset, len(raw), record, read_row)

    def apply(self, offset: int, length: int, record: dict[str, Any], read_row: _RowReader) -> None:
        """Fold one parsed object row (at ``offset``) into the index."""
        assert self.runs is not None and self.plan_runs is not None
        self.records += 1
        if not _accepted_version(record):
            self.unrecognized += 1
        kind = record.get("kind")
        run_id = str(record.get("run_id") or "")
        if run_id:
            entry = self.runs.get(run_id)
            if entry is None:
                entry = self.runs[run_id] = {
                    "first": offset,
                    "end": offset,
                    "state": "unknown",
                    "started_at": None,
                    "completed_at": None,
                }
            entry["end"] = offset + length
            if kind == "ingest_run":
                state = str(record.get("state") or entry["state"])
                entry["state"] = state
                if state == "started":
                    entry["started_at"] = record.get("ts")
                elif state:
                    entry["completed_at"] = record.get("ts")
        if kind == "commit_plan" and run_id and _accepted_version(record):
            if _is_current_plan_row(record):
                self.plan_runs.add(run_id)
        if kind in _PLAN_ROW_KINDS and not self.anomaly:
            try:
                self._track_plan(str(kind), record, offset, length, read_row)
            except ValueError:
                self.anomaly = True

    def _track_plan(
        self,
        kind: str,
        record: dict[str, Any],
        offset: int,
        length: int,
        read_row: _RowReader,
    ) -> None:
        """Keep the open-plan set; raise ``ValueError`` on anything unusual.

        Only the clean lifecycle (plan, its receipts, one terminal row) is handled
        incrementally. Every other ordering flags ``anomaly`` and the validating
        reader re-derives the exact verdict with the whole-ledger validation.
        """
        assert self.open_plans is not None and self.closed is not None
        plan_id = str(record.get("plan_id") or "")
        if not plan_id:
            return
        if kind == "commit_plan":
            if plan_id in self.open_plans or plan_id in self.closed:
                raise ValueError(f"duplicate commit plan id: {plan_id}")
            self.open_plans[plan_id] = {
                "run": str(record.get("run_id") or ""),
                "order": self.next_order,
                "o": offset,
                "l": length,
                "receipts": [],
            }
            self.next_order += 1
            return
        meta = self.open_plans.get(plan_id)
        if meta is None:
            raise ValueError(f"{kind} for a plan that is not open: {plan_id}")
        if kind == "operation_receipt":
            meta["receipts"].append([offset, length])
            return
        if str(record.get("run_id") or "") != meta["run"]:
            raise ValueError(f"terminal row run mismatch: {plan_id}")
        receipts = [(o, read_row(o, n)) for o, n in meta["receipts"]]
        terminal = (offset, record)
        _validate_plan_group(
            plan_id,
            read_row(meta["o"], meta["l"]),
            meta["o"],
            receipts,
            terminal if kind == "commit_record" else None,
            terminal if kind == "plan_abandoned" else None,
        )
        del self.open_plans[plan_id]
        self.closed[plan_id] = "c" if kind == "commit_record" else "a"

    def catch_up(self, handle: Any, read_row: _RowReader, limit: int | None = None) -> None:
        """Index every complete line past ``size`` (stopping at ``limit`` if given).

        An unterminated tail stays uncovered.
        """
        handle.seek(self.size)
        next_check = self.size + _CANCEL_CHECK_BYTES
        while limit is None or self.size < limit:
            if self.size >= next_check:
                next_check = self.size + _CANCEL_CHECK_BYTES
                _raise_if_cancelled()
            raw = handle.readline()
            if not raw or not raw.endswith(b"\n"):
                return
            self.ingest_raw(raw, read_row)

    # -- persistence ------------------------------------------------------
    def to_bytes(self) -> bytes:
        assert self.runs is not None and self.plan_runs is not None
        assert self.open_plans is not None and self.closed is not None
        anchor_len, anchor_sha = self.anchor()
        body = json.dumps(
            {
                "size": self.size,
                "anchor_len": anchor_len,
                "anchor_sha256": anchor_sha,
                "records": self.records,
                "malformed": self.malformed,
                "unrecognized": self.unrecognized,
                "exotic": self.exotic,
                "anomaly": self.anomaly,
                "next_order": self.next_order,
                "runs": self.runs,
                "plan_runs": sorted(self.plan_runs),
                "open_plans": self.open_plans,
                "closed": self.closed,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        header = json.dumps(
            {
                "index_version": _SIDECAR_VERSION,
                "ledger": LEDGER_FILENAME,
                "body_sha256": hashlib.sha256(body).hexdigest(),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return header + b"\n" + body + b"\n"

    @classmethod
    def from_bytes(cls, data: bytes) -> "_Sidecar | None":
        """Parse a sidecar file; ``None`` for anything but a fully valid current one."""
        try:
            header_raw, body_raw, rest = data.split(b"\n")
            if rest:
                return None
            header = json.loads(header_raw)
            if (
                header.get("index_version") != _SIDECAR_VERSION
                or header.get("ledger") != LEDGER_FILENAME
                or header.get("body_sha256") != hashlib.sha256(body_raw).hexdigest()
            ):
                return None
            body = json.loads(body_raw)
            state = cls(
                size=_nonneg_int(body["size"]),
                records=_nonneg_int(body["records"]),
                malformed=_nonneg_int(body["malformed"]),
                unrecognized=_nonneg_int(body["unrecognized"]),
                exotic=_nonneg_int(body["exotic"]),
                anomaly=_bool(body["anomaly"]),
                next_order=_nonneg_int(body["next_order"]),
                runs=_run_table(body["runs"]),
                plan_runs={_text(v) for v in body["plan_runs"]},
                open_plans=_open_plan_table(body["open_plans"]),
                closed={_text(k): _text(v) for k, v in body["closed"].items()},
            )
            anchor_len = _nonneg_int(body["anchor_len"])
            if anchor_len > min(state.size, _SIDECAR_ANCHOR_BYTES):
                return None
            state.loaded_anchor = (anchor_len, _text(body["anchor_sha256"]))
            state.persisted_size = state.size
            return state
        except (ValueError, KeyError, TypeError, AttributeError):
            return None

    def matches(self, handle: Any, file_size: int) -> bool:
        """True when the ledger still has the bytes this state was built from."""
        if self.size > file_size:
            return False
        if self.size == 0:
            return True
        length, sha = self.loaded_anchor or self.anchor()
        handle.seek(self.size - length)
        tail = handle.read(length)
        if len(tail) != length or hashlib.sha256(tail).hexdigest() != sha:
            return False
        self.tail = tail
        self.loaded_anchor = None
        return True


def _nonneg_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected a non-negative integer")
    return value


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("expected a boolean")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("expected text")
    return value


def _run_table(raw: Any) -> dict[str, dict[str, Any]]:
    table: dict[str, dict[str, Any]] = {}
    for run_id, entry in raw.items():
        table[_text(run_id)] = {
            "first": _nonneg_int(entry["first"]),
            "end": _nonneg_int(entry["end"]),
            "state": _text(entry["state"]),
            "started_at": entry["started_at"],
            "completed_at": entry["completed_at"],
        }
    return table


def _open_plan_table(raw: Any) -> dict[str, dict[str, Any]]:
    table: dict[str, dict[str, Any]] = {}
    for plan_id, entry in raw.items():
        table[_text(plan_id)] = {
            "run": _text(entry["run"]),
            "order": _nonneg_int(entry["order"]),
            "o": _nonneg_int(entry["o"]),
            "l": _nonneg_int(entry["l"]),
            "receipts": [[_nonneg_int(o), _nonneg_int(n)] for o, n in entry["receipts"]],
        }
    return table


def _sidecar_cache_get(key: str) -> _Sidecar | None:
    with _SIDECARS_GUARD:
        state = _SIDECARS.get(key)
        if state is not None:
            _SIDECARS.move_to_end(key)
        return state


def _sidecar_cache_put(key: str, state: _Sidecar) -> None:
    with _SIDECARS_GUARD:
        _SIDECARS[key] = state
        _SIDECARS.move_to_end(key)
        while len(_SIDECARS) > _SIDECAR_CACHE_SIZE:
            _SIDECARS.popitem(last=False)


def _sidecar_cache_drop(path: Path | None = None) -> None:
    """Forget the in-process state (all paths, or one): the next use reloads from disk."""
    with _SIDECARS_GUARD:
        if path is None:
            _SIDECARS.clear()
        else:
            _SIDECARS.pop(str(path.resolve()), None)
    with _SNAPSHOT_FAILURES_GUARD:
        if path is None:
            _SNAPSHOT_FAILURES.clear()
        else:
            _SNAPSHOT_FAILURES.pop(str(path.resolve()), None)


def _write_sidecar_file(path: Path, payload: bytes) -> None:
    """Atomic replace: temp file in the same directory, fsync, rename."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


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


class _RunView:
    """Offset-based reads of one ledger run at a time, under the ledger lock."""

    def __init__(self, state: _Sidecar, handle: Any) -> None:
        self.state = state
        self._handle = handle

    def order(self) -> list[str]:
        return self.state.run_order()

    def close(self) -> None:
        self._handle.close()

    def records(self, run_id: str) -> list[dict[str, Any]]:
        """The run's rows in ledger order: only its byte span is read."""
        assert self.state.runs is not None
        entry = self.state.runs.get(run_id)
        if entry is None:
            return []
        self._handle.seek(entry["first"])
        rows: list[dict[str, Any]] = []
        while self._handle.tell() < entry["end"]:
            raw = self._handle.readline()
            if not raw:
                break
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if isinstance(record, dict) and str(record.get("run_id") or "") == run_id:
                rows.append(record)
        return rows


@dataclass(frozen=True)
class CandidateLedger:
    """Append-only JSONL ledger rooted in a vault's ``.marginalia`` directory."""

    dir: Path

    @property
    def path(self) -> Path:
        return Path(self.dir) / LEDGER_FILENAME

    @property
    def index_path(self) -> Path:
        """The rebuildable index sidecar (never a source of truth; safe to delete)."""
        return Path(self.dir) / LEDGER_INDEX_FILENAME

    def _sync_sidecar(self) -> _Sidecar | None:
        """Bring the index in step with the ledger file. The ledger lock must be held.

        Order of trust: the in-process state, else the sidecar file; either is
        used only if the ledger still holds the bytes it was built from (not
        shorter, same tail). Otherwise it is rebuilt by one streaming scan. A
        ledger longer than the state covers (another process appended, or a crash
        fell between the ledger write and the index update) is caught up by
        scanning just the new tail.
        """
        path = self.path
        try:
            file_size = path.stat().st_size
        except FileNotFoundError:
            _sidecar_cache_drop(path)
            return None
        key = str(path.resolve())
        state = _sidecar_cache_get(key)
        reader = _RowReader(path)
        try:
            with path.open("rb") as handle:
                if state is None:
                    state = self._load_sidecar_file()
                if state is not None and not state.matches(handle, file_size):
                    state = None
                if state is None:
                    state = _Sidecar()
                state.catch_up(handle, reader)
        except BaseException:
            _sidecar_cache_drop(path)
            raise
        finally:
            reader.close()
        state.uncovered = file_size - state.size
        _sidecar_cache_put(key, state)
        self._persist_sidecar(state)
        return state

    def _prepare_sidecar(self) -> bool:
        """Bring the in-process index near the ledger's size without a long lock hold.

        A state that is current, or a few MiB behind, is left for the in-lock
        :meth:`_sync_sidecar` to finish. Anything else (no usable state, or far
        behind) is scanned from a snapshot with no lock held and then published;
        the caller's in-lock sync only folds in the rows appended meanwhile.

        Returns ``False`` when that snapshot pass failed, or already failed on this
        exact ledger prefix (a failure is remembered per snapshot signature and not
        retried until the file changes past it). A failed pass is discarded whole:
        its half-applied state is never published.
        """
        path = self.path
        key = str(path.resolve())
        lock = Path(self.dir) / ".candidate-ledger.lock"
        with _exclusive_lock(lock):
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                return True
            cached = _sidecar_cache_get(key)
            candidate = cached if cached is not None else self._load_sidecar_file()
            with path.open("rb") as probe:
                usable = candidate is not None and candidate.matches(probe, size)
            if usable and candidate is not None and size - candidate.size <= _SIDECAR_INLINE_TAIL:
                _snapshot_failure_clear(key)
                return True
            work = candidate if usable and cached is None else None
            handle = path.open("rb")
            cut = _last_line_end(handle, size)
            signature = _snapshot_signature(handle, cut)
            if _snapshot_failure_reason(key, signature) is not None:
                handle.close()
                return False
        reader = _RowReader(path)
        try:
            work = work or _Sidecar()
            work.catch_up(handle, reader, limit=cut)
        except _BuildCancelled:
            return False  # asked to stop: nothing remembered, nothing published
        except Exception as exc:  # noqa: BLE001 - remembered and logged; never published
            reason = f"{type(exc).__name__}: {str(exc)[:200]}"
            _snapshot_failure_record(key, signature, reason)
            _LOG.info(
                "candidate ledger index: snapshot pass failed (%s); not retried until "
                "the ledger changes past %d bytes",
                reason,
                cut,
            )
            return False
        except BaseException:
            return False  # cancelled mid-pass: nothing remembered, nothing published
        finally:
            reader.close()
            handle.close()
        _snapshot_failure_clear(key)
        with _exclusive_lock(lock):
            if _sidecar_cache_get(key) is cached:
                _sidecar_cache_put(key, work)
        return True

    def index_degraded_reason(self) -> str | None:
        """Why the index is being bypassed for this ledger, or ``None`` when it is not."""
        return _snapshot_failure_current(str(self.path.resolve()))

    @contextmanager
    def _run_view(self):
        """Yield an offset-based :class:`_RunView` under the ledger lock.

        Yields ``None`` when the whole-file readers must answer instead: the
        ledger is absent, holds rows that ``str.splitlines`` splits or
        ``utf-8`` rejects (their historic behavior is defined by that reader),
        or ends in an unterminated row the index does not cover.
        """
        if not self._prepare_sidecar():
            # The lock-free pass failed: answer from the streaming readers rather than
            # rebuilding the whole index under the ledger lock.
            yield None
            return
        with _exclusive_lock(Path(self.dir) / ".candidate-ledger.lock"):
            state = self._sync_sidecar()
            if state is None or state.exotic or state.uncovered:
                view = None
            else:
                assert state.runs is not None
                # A copy and an open handle: the spans are read after the lock is
                # released, and bytes below them never change (append-only).
                view = _RunView(
                    _Sidecar(
                        records=state.records,
                        runs={run_id: dict(entry) for run_id, entry in state.runs.items()},
                    ),
                    self.path.open("rb"),
                )
        if view is None:
            yield None
            return
        try:
            yield view
        finally:
            view.close()

    def _load_sidecar_file(self) -> _Sidecar | None:
        try:
            data = self.index_path.read_bytes()
        except OSError:
            return None
        return _Sidecar.from_bytes(data)

    def _persist_sidecar(self, state: _Sidecar, *, force: bool = False) -> None:
        """Best-effort checkpoint of the in-memory index (a cache: failures are ignored)."""
        if (
            not force
            and 0 <= state.persisted_size
            and (state.size - state.persisted_size < _SIDECAR_CHECKPOINT_BYTES)
        ):
            return
        try:
            _write_sidecar_file(self.index_path, state.to_bytes())
        except OSError:
            return
        state.persisted_size = state.size

    def write_index_checkpoint(self) -> None:
        """Persist the index now (it is otherwise checkpointed every few MiB)."""
        if not self.path.exists():
            return
        self._prepare_sidecar()
        with _exclusive_lock(Path(self.dir) / ".candidate-ledger.lock"):
            state = self._sync_sidecar()
            if state is not None:
                self._persist_sidecar(state, force=True)

    def _sidecar_after_append(
        self,
        record_offset: int,
        encoded: bytes,
        record: dict[str, Any],
        *,
        repaired_tail: bool,
    ) -> None:
        """Fold one just-appended row into the index, under the same ledger lock.

        Never raises: the row is already durable, and the index is a cache that
        the next reader repairs by catching up from the ledger.
        """
        try:
            key = str(self.path.resolve())
            state = _sidecar_cache_get(key)
            if state is None:
                if record_offset != 0 or repaired_tail:
                    return  # an existing ledger is indexed lazily by its first reader
                state = _Sidecar()
            elif repaired_tail or state.size > record_offset:
                _sidecar_cache_drop(self.path)  # rewritten below the cut: rebuild on next use
                return
            elif state.size < record_offset:
                # Another process appended: the state is still valid for [0, size) and
                # the next reader folds in the tail (the anchor check catches divergence).
                return
            reader = _RowReader(self.path)
            try:
                state.apply(record_offset, len(encoded), record, reader)
            finally:
                reader.close()
            state.advance(encoded)
            _sidecar_cache_put(key, state)
            self._persist_sidecar(state)
        except Exception:  # noqa: BLE001 - cache maintenance must not fail a durable append
            _sidecar_cache_drop(self.path)

    def prewarm(self, cancelled: Callable[[], bool] | None = None) -> None:
        """Build the in-process sidecar state and the offset index ahead of the first reader.

        Both entry points are the ones a first reader uses, so this only moves
        the cold build earlier; it never changes what a read returns. ``cancelled``
        is checked between the two builds so a daemon that is stopping skips the
        second one. The index is a cache: a failed pass is left to the readers.
        """
        if not self.path.exists():
            return
        # The streaming loops of both builds look at ``cancelled`` every few MiB and
        # discard their partial state when it is set, so a stop never waits for a
        # whole cold pass. Only this thread sees the flag; readers are unaffected.
        _BUILD_CANCEL.fn = cancelled
        try:
            self._prepare_sidecar()
            if cancelled is not None and cancelled():
                return
            self._offset_index()
        except _BuildCancelled:
            return
        finally:
            _BUILD_CANCEL.fn = None

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

        pinned = _pin_ledger_offset_index(Path(self.dir), self.path)
        if pinned is None:
            return []
        index, cut, handle = pinned
        # Every indexed row below ``cut`` lies in a prefix that never changes
        # (append-only), so the rows are read without holding the ledger lock; rows
        # appended after this snapshot are simply not part of it.
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
        locations = sorted(loc for loc in locations if loc[0] + loc[1] <= cut)
        records: list[dict[str, Any]] = []
        with handle:
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
            if repaired_tail:
                _invalidate_ledger_offset_index(self.path)
            else:
                _extend_ledger_offset_index(
                    self.path,
                    offset=record_offset,
                    encoded=encoded,
                    record=record,
                )
            self._sidecar_after_append(record_offset, encoded, record, repaired_tail=repaired_tail)
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

    @contextmanager
    def _snapshot(self):
        """Pin the ledger as it is now, then let appends continue.

        Under the lock, and only for as long as that takes: open the file, read its
        size ``S``, find ``cut`` (just past the last newline at or below ``S``, read
        backwards in small blocks) and copy the bytes ``[cut, S)``, a crash-torn
        tail if there is one. The caller reads ``[0, cut)`` from the open handle
        without the lock. Those bytes never change: ``append()`` only appends, and
        the one in-place change (``_prepare_append_target`` truncating a torn tail)
        removes only bytes above the last newline, so a later append can rewrite
        ``[cut, S)`` but never anything below it. Nothing here replaces the ledger
        file (``os.replace`` is used for the index sidecar only), so the open
        handle cannot end up on a stale inode.
        """
        with _exclusive_lock(Path(self.dir) / ".candidate-ledger.lock"):
            handle = self.path.open("rb")
            size = os.fstat(handle.fileno()).st_size
            cut = _last_line_end(handle, size)
            handle.seek(cut)
            skipped = size - cut > _SCAN_TAIL_CAP
            tail = handle.read(size - cut) if 0 < size - cut and not skipped else b""
        if skipped:
            _LOG.warning(
                "candidate ledger ends in a %d-byte unterminated tail (over the %d-byte read cap); "
                "it is reported as a trailing partial record but not read",
                size - cut,
                _SCAN_TAIL_CAP,
            )
        try:
            yield _Snapshot(handle, size, cut, tail, skipped)
        finally:
            handle.close()

    def scan(
        self,
        *,
        max_malformed_samples: int = _MALFORMED_SAMPLE_LIMIT,
        kinds: frozenset[str] | None = None,
    ) -> LedgerScanResult:
        """Read all ledger bytes and report any evidence that could not be parsed.

        The regular :meth:`records` method intentionally preserves its historic
        compatibility behavior: it skips invalid JSON and non-object rows but
        can still fail on invalid UTF-8. Semantic audits use this method so such
        rows and interrupted final appends cannot disappear without an explicit
        incomplete result.

        The file is streamed one chunk at a time under the ledger lock, so the
        view stays immutable while memory is bounded by one chunk, one row and
        the retained ``parsed_records``. Every line is still parsed and counted
        (completeness, hash, sizes are always whole-file); ``kinds`` only limits
        which parsed rows are *retained* in ``parsed_records`` for callers that
        read one record kind from a large ledger. With ``kinds`` set,
        ``parsed_record_count`` counts the retained rows.
        """
        if max_malformed_samples < 0:
            raise ValueError("max_malformed_samples must be >= 0")
        return self._scan_once(max_malformed_samples, kinds)

    def _scan_once(
        self, max_malformed_samples: int, kinds: frozenset[str] | None
    ) -> LedgerScanResult:
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

        digest = hashlib.sha256()
        file_size = 0
        last_byte = b""

        def hashed_chunks(snap: _Snapshot) -> Iterator[bytes]:
            nonlocal file_size, last_byte
            reader = _PrefixReader(snap)
            while True:
                chunk = reader.read(_STREAM_CHUNK_BYTES)
                if not chunk:
                    return
                digest.update(chunk)
                file_size += len(chunk)
                last_byte = chunk[-1:]
                yield chunk

        parsed_records: list[dict[str, Any]] = []
        malformed: list[MalformedLedgerLine] = []
        malformed_line_count = 0
        malformed_line_numbers: set[int] = set()
        nonempty_lines = 0
        ledger_versions: set[int] = set()
        unrecognized_version_record_count = 0
        final_nonempty_line_number: int | None = None

        line_number = 0
        with self._snapshot() as snap:
            for line_number, raw_line in enumerate(_stream_lines(hashed_chunks(snap)), start=1):
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

                if kinds is None or record.get("kind") in kinds:
                    parsed_records.append(record)
                version = record.get("ledger_version")
                if isinstance(version, int) and not isinstance(version, bool):
                    ledger_versions.add(version)
                    if version not in _ACCEPTED_LEDGER_VERSIONS:
                        unrecognized_version_record_count += 1
                else:
                    unrecognized_version_record_count += 1

        tail_unread = snap.size - snap.cut if snap.tail_skipped else 0
        if tail_unread:
            # Over-cap torn tail: counted as one unread partial row; its bytes are
            # neither parsed nor hashed, so there is no whole-file digest to offer.
            line_number += 1
            nonempty_lines += 1
            final_nonempty_line_number = line_number
            malformed_line_count += 1
            malformed_line_numbers.add(line_number)
            if len(malformed) < max_malformed_samples:
                malformed.append(MalformedLedgerLine(line_number, "tail_over_read_cap", "", False))
            file_size += tail_unread
            last_byte = b"\x00"
        unterminated_final_line = bool(file_size) and last_byte not in (b"\n", b"\r")

        trailing_partial = bool(
            unterminated_final_line
            and final_nonempty_line_number is not None
            and final_nonempty_line_number == line_number
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
            total_lines=line_number,
            nonempty_lines=nonempty_lines,
            malformed_line_count=malformed_line_count,
            malformed_lines=tuple(malformed),
            malformed_samples_truncated=malformed_line_count > len(malformed),
            unrecognized_version_record_count=unrecognized_version_record_count,
            unterminated_final_line=unterminated_final_line,
            trailing_partial=trailing_partial,
            trailing_partial_line_number=(final_nonempty_line_number if trailing_partial else None),
            ledger_versions=tuple(sorted(ledger_versions)),
            file_size_bytes=file_size,
            file_sha256=None if tail_unread else digest.hexdigest(),
            completeness_status=completeness_status,
            completeness_reason=completeness_reason,
        )

    def iter_records(self) -> Iterator[dict[str, Any]]:
        """Stream every parseable record in file order, one row in memory at a time.

        Same filtering and errors as :meth:`records`: blank, non-JSON and
        non-object rows are skipped, invalid UTF-8 raises ``UnicodeDecodeError``
        (here when the reader reaches it, not before the first row). Prefer this
        over :meth:`records` for any consumer that does not need the whole ledger
        at once; the ledger can be far larger than memory.
        """
        if not self.path.exists():
            return
        # ``read_text().splitlines()`` semantics (universal newlines, then the
        # ``str`` line boundaries), streamed instead of slurped.
        # The pinned snapshot (see :meth:`_snapshot`): rows appended after it are
        # not read, and a torn tail that is later repaired cannot change what is read.
        with self._snapshot() as snap:
            text = io.TextIOWrapper(io.BufferedReader(_PrefixReader(snap)), encoding="utf-8")
            for line in _stream_lines(_read_chunks(text, _STREAM_CHUNK_BYTES)):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record

    def ingest_run_records(self) -> list[dict[str, Any]]:
        """Every ``ingest_run`` row in ledger order, read by offset.

        Same rows as ``scan(kinds={"ingest_run"}).parsed_records`` (malformed lines
        are skipped the same way) but the cost follows the number of runs, not the
        size of the ledger.
        """
        if not self.path.exists():
            return []
        return self._indexed_records(kinds={"ingest_run"})

    def records(self) -> list[dict[str, Any]]:
        """Every parseable record as a list (the whole ledger: prefer :meth:`iter_records`)."""
        return list(self.iter_records())

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
            kinds={"ingest_run", "commit_record", "plan_abandoned"}
        )
        # Runs holding a current-format sealed plan come from the index sidecar, so
        # the (large) commit_plan rows are not read back just to be inspected.
        self._prepare_sidecar()
        with _exclusive_lock(Path(self.dir) / ".candidate-ledger.lock"):
            state = self._sync_sidecar()
            if state is None:
                return ()
            assert state.plan_runs is not None
            current_plan_runs = set(state.plan_runs)

        starts: dict[str, dict[str, Any]] = {}
        terminal: dict[str, tuple[int, dict[str, Any]]] = {}
        committed: set[str] = set()
        abandoned: set[str] = set()
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

        The open-plan set comes from the index sidecar, so the cost follows the
        open plans, not the ledger. Every plan is validated once, when it is
        closed (or when the index is rebuilt); open plans are validated here. If
        the ledger has any plan bookkeeping anomaly the verdict is re-derived by
        :meth:`_validate_plans_full`, which raises exactly what the whole-ledger
        validation always raised.
        """

        try:
            self._repair_replayable_tail()
        except ValueError as exc:
            raise ValueError(
                "cannot resume apply from an invalid candidate ledger: " + str(exc)
            ) from exc
        if not self.path.exists():
            return ()
        self._prepare_sidecar()
        loaded: list[tuple[str, dict[str, Any], int, list[tuple[int, dict[str, Any]]]]] | None
        loaded = []
        with _exclusive_lock(Path(self.dir) / ".candidate-ledger.lock"):
            state = self._sync_sidecar()
            if state is None:
                return ()
            reason = state.completeness_reason()
            if reason:
                raise ValueError(f"cannot resume apply from an invalid candidate ledger: {reason}")
            assert state.open_plans is not None
            if state.anomaly:
                loaded = None
            else:
                reader = _RowReader(self.path)
                try:
                    for plan_id, meta in sorted(
                        state.open_plans.items(), key=lambda item: item[1]["order"]
                    ):
                        loaded.append(
                            (
                                plan_id,
                                reader(meta["o"], meta["l"]),
                                meta["o"],
                                [(o, reader(o, n)) for o, n in meta["receipts"]],
                            )
                        )
                except ValueError:
                    loaded = None
                finally:
                    reader.close()
        if loaded is None:
            snapshots = self._validate_plans_full()
        else:
            snapshots = []
            for plan_id, plan_row, plan_position, receipt_rows in loaded:
                snapshot = _validate_plan_group(
                    plan_id, plan_row, plan_position, receipt_rows, None, None
                )
                assert snapshot is not None  # an open plan is never a closed legacy plan
                snapshots.append(snapshot)
        return tuple(
            snapshot
            for snapshot in snapshots
            if document_id is None
            or str(snapshot.context.get("document_id") or "") == str(document_id)
        )

    def _validate_plans_full(self) -> list[CommitPlanSnapshot]:
        """Validate every plan in the ledger; return the unreceipted ones in ledger order.

        The exact-verdict lane for a ledger whose plan bookkeeping is not the clean
        lifecycle. One streaming pass keeps only byte offsets (never the rows), then
        each plan is validated from its own rows, so memory follows the number of
        plans. Raises the first ``ValueError`` in the order the original
        whole-ledger validation did: row-order framing errors first, then per-plan
        errors in plan order.
        """

        order: list[str] = []
        plans: dict[str, dict[str, Any]] = {}
        with self._snapshot() as snap:
            reader = _RowReader(self.path)
            try:
                with self.path.open("rb") as handle:
                    offset = 0
                    for raw in iter(handle.readline, b""):
                        if offset >= snap.cut:
                            break
                        position, offset = offset, offset + len(raw)
                        try:
                            record = json.loads(raw.decode("utf-8"))
                        except (UnicodeDecodeError, ValueError):
                            continue
                        if not isinstance(record, dict):
                            continue
                        kind = record.get("kind")
                        plan_id = str(record.get("plan_id") or "")
                        if kind not in _PLAN_ROW_KINDS or not plan_id:
                            continue
                        span = (position, len(raw))
                        if kind == "commit_plan":
                            if plan_id in plans:
                                raise ValueError(f"duplicate commit plan id: {plan_id}")
                            order.append(plan_id)
                            plans[plan_id] = {
                                "run": str(record.get("run_id") or ""),
                                "plan": span,
                                "receipts": [],
                                "commit": None,
                                "abandoned": None,
                            }
                            continue
                        meta = plans.get(plan_id)
                        if kind == "operation_receipt":
                            if meta is None:
                                raise ValueError(
                                    f"operation receipt precedes or lacks plan: {plan_id}"
                                )
                            meta["receipts"].append(span)
                        elif kind == "commit_record":
                            if meta is None:
                                raise ValueError(
                                    f"commit receipt precedes or lacks plan: {plan_id}"
                                )
                            if meta["commit"] is not None:
                                raise ValueError(f"duplicate commit receipt: {plan_id}")
                            if str(record.get("run_id") or "") != meta["run"]:
                                raise ValueError(f"commit receipt run mismatch: {plan_id}")
                            meta["commit"] = span
                        else:
                            if meta is None:
                                raise ValueError(
                                    f"abandoned plan precedes or lacks plan: {plan_id}"
                                )
                            if meta["abandoned"] is not None or meta["commit"] is not None:
                                raise ValueError(f"duplicate terminal plan record: {plan_id}")
                            meta["abandoned"] = span
                snapshots: list[CommitPlanSnapshot] = []
                for plan_id in order:
                    meta = plans[plan_id]
                    commit = meta["commit"]
                    abandoned = meta["abandoned"]
                    snapshot = _validate_plan_group(
                        plan_id,
                        reader(*meta["plan"]),
                        meta["plan"][0],
                        [(o, reader(o, n)) for o, n in meta["receipts"]],
                        (commit[0], reader(*commit)) if commit is not None else None,
                        (abandoned[0], reader(*abandoned)) if abandoned is not None else None,
                    )
                    if snapshot is not None and commit is None and abandoned is None:
                        snapshots.append(snapshot)
                return snapshots
            finally:
                reader.close()

    def _open_plan_receipt_rows(self, plan: CommitPlanSnapshot) -> list[dict[str, Any]] | None:
        """The receipt rows of an open plan straight from the index, else ``None``."""
        if not self.path.exists():
            return None
        self._prepare_sidecar()
        with _exclusive_lock(Path(self.dir) / ".candidate-ledger.lock"):
            state = self._sync_sidecar()
            if state is None or state.anomaly or state.completeness_reason():
                return None
            assert state.open_plans is not None
            meta = state.open_plans.get(plan.plan_id)
            if meta is None or meta["run"] != plan.run_id:
                return None
            reader = _RowReader(self.path)
            try:
                rows = [reader(o, n) for o, n in meta["receipts"]]
            except ValueError:
                return None
            finally:
                reader.close()
        return [row for row in rows if str(row.get("run_id") or "") == plan.run_id]

    def operation_receipts(self, plan: CommitPlanSnapshot) -> dict[str, dict[str, Any]]:
        """Return validated, unique durable receipts for one sealed plan."""

        expected = {
            str(operation["operation_id"]): str(operation["operation"])
            for operation in plan.operations
        }
        receipts: dict[str, dict[str, Any]] = {}
        rows = self._open_plan_receipt_rows(plan)
        if rows is None:
            rows = self._indexed_records(kinds={"operation_receipt"}, run_ids={plan.run_id})
        for record in rows:
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
        if not self.path.exists():
            return []
        with self._run_view() as view:
            if view is not None:
                ids = view.order()[:limit]
                rows = {
                    row["run_id"]: row
                    for row in _summarize_runs(
                        record for run_id in ids for record in view.records(run_id)
                    )
                }
                return [rows[run_id] for run_id in ids]
        return _sort_run_rows(_summarize_runs(self.iter_records()))[:limit]

    def run_detail(self, run_id: str) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        with self._run_view() as view:
            if view is not None:
                records = (
                    [r for r in view.records(run_id) if r.get("run_id") == run_id]
                    if run_id in view.state.runs  # type: ignore[operator]
                    else []
                )
                row = None
                if records and run_id in view.order()[:500]:
                    row = _summarize_runs(view.records(run_id))[0]
                return _run_detail_payload(records, row)
        records = [record for record in self.iter_records() if record.get("run_id") == run_id]
        if not records:
            return None
        row = next(
            (
                r
                for r in _sort_run_rows(_summarize_runs(self.iter_records()))[:500]
                if r["run_id"] == run_id
            ),
            None,
        )
        return _run_detail_payload(records, row)

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
        if not self.path.exists():
            return None
        with self._run_view() as view:
            if view is not None:
                if not view.state.records:
                    return None
                order = view.order()
                if run_id:
                    selected = run_id if run_id in order[:500] else None
                else:
                    selected = order[0] if order else None
                if selected is None:
                    return None
                run_records = view.records(selected)
                run = _summarize_runs(run_records)[0] if run_records else None
                if not run_records:
                    return None
                return _progress_from_records(run, run_records, limit=limit)
        if next(self.iter_records(), None) is None:
            return None
        rows = _sort_run_rows(_summarize_runs(self.iter_records()))
        if run_id:
            run = next((row for row in rows[:500] if row["run_id"] == run_id), None)
        else:
            run = next(iter(rows[:1]), None)
        if not run:
            return None
        selected_run_id = str(run.get("run_id") or "")
        run_records = [
            record
            for record in self.iter_records()
            if str(record.get("run_id") or "") == selected_run_id
        ]
        if not run_records:
            return None
        progress = _progress_from_records(run, run_records, limit=limit)
        degraded = self.index_degraded_reason()
        if degraded is not None:
            progress["ledger_index_degraded"] = degraded
        return progress


def _summarize_runs(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One summary row per ``run_id`` found in ``records`` (first-appearance order).

    Rows depend only on their own run's rows, in ledger order, so a caller may pass
    one run's rows or the whole ledger and get the same row for that run.
    """
    runs: dict[str, dict[str, Any]] = {}
    for record in records:
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
    return values


def _sort_run_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda item: str(item.get("started_at") or item.get("completed_at") or ""),
        reverse=True,
    )


def _run_detail_payload(
    records: list[dict[str, Any]], row: dict[str, Any] | None
) -> dict[str, Any] | None:
    if not records:
        return None
    compact = [_without_heavy_values(record) for record in records]
    candidates = [
        _candidate_summary(record) for record in compact if record.get("kind") == "candidate"
    ]
    return {
        "run": row,
        "records": compact,
        "candidates": candidates,
        "comparisons": [r for r in compact if r.get("kind") == "comparison"],
        "commit_plans": [r for r in compact if r.get("kind") == "commit_plan"],
        "commit_records": [r for r in compact if r.get("kind") == "commit_record"],
        "integrity_outcomes": [r for r in compact if r.get("kind") == "integrity_outcome"],
    }


def _progress_from_records(
    run: dict[str, Any], run_records: list[dict[str, Any]], *, limit: int
) -> dict[str, Any]:
    """The compact progress payload for one run's rows (see ``run_progress_summary``)."""
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
            predicate = str(payload.get("canonical_predicate") or payload.get("type") or "unknown")
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
                    verdict: rows for verdict, rows in sorted(relation_samples_by_verdict.items())
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
    "LEDGER_INDEX_FILENAME",
    "LedgerCompleteness",
    "LedgerScanResult",
    "MalformedLedgerLine",
    "ResumeSnapshot",
    "edge_candidate_id",
]
