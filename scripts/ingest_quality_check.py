#!/usr/bin/env python3
"""Repeatable ingest quality guardrail for live Okto Neuron vaults.

This script intentionally talks to the running HTTP server instead of importing
server internals. Product-owned semantic metrics come from
``/api/v1/quality/semantic`` so this orchestration layer cannot redefine them
from the capped graph-overview response. It targets a registered vault on every
scoped request, can optionally reset and ingest a source folder, waits for the
queue to drain, then asserts simple graph/ledger invariants. For subjective review,
``--cognitive-score-cmd`` receives the JSON report on stdin and may call Codex,
Claude, or any local scorer.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_ENDPOINT = "http://127.0.0.1:7777"
PROVENANCE_NODE_TYPES = frozenset({"Block", "Document"})
ISOLATION_EXCLUDED_NODE_TYPES = PROVENANCE_NODE_TYPES | frozenset({"Claim"})
PLUMBING_EDGE_TYPES = frozenset(
    {
        "prov:wasDerivedFrom",
        "prov:wasGeneratedBy",
        "prov:wasAttributedTo",
        "rdf:subject",
        "rdf:object",
        "schema:mentions",
    }
)
STRUCTURAL_TITLE_NOISE_RE = re.compile(
    r"^(?:"
    r"(?:sub)?section\s+\d[\w.]*"
    r"|appendix(?:\s+[\w.]+)?"
    r"|figure\s+\d[\w.]*"
    r"|table\s+\d[\w.]*"
    r"|(?:paragraph|code[-_ ]?block|blockquote|list item)\s+\d+"
    r"|§\s*\d+"
    r"|\d+(?:\.\w+)*"
    r")$",
    re.IGNORECASE,
)

DOMAIN_PROFILES: dict[str, dict[str, Any]] = {
    "lotr": {
        "groups": {
            "characters": [
                {"label": "Frodo", "aliases": ["Frodo", "Frodo Baggins"]},
                {"label": "Samwise Gamgee", "aliases": ["Sam", "Samwise Gamgee"]},
                {"label": "Gandalf", "aliases": ["Gandalf", "Gandalf the Grey"]},
                {"label": "Aragorn", "aliases": ["Aragorn", "Strider"]},
                {"label": "Bilbo Baggins", "aliases": ["Bilbo", "Bilbo Baggins"]},
                {"label": "Merry Brandybuck", "aliases": ["Merry", "Meriadoc Brandybuck"]},
                {"label": "Pippin Took", "aliases": ["Pippin", "Peregrin Took"]},
            ],
            "places": [
                {"label": "Shire", "aliases": ["The Shire", "Shire"]},
                {"label": "Hobbiton", "aliases": ["Hobbiton"]},
                {"label": "Rivendell", "aliases": ["Rivendell"]},
                {"label": "Moria", "aliases": ["Moria", "Mines of Moria"]},
                {"label": "Lothlorien", "aliases": ["Lothlorien", "Lothlórien", "Lorien", "Lórien"]},
                {"label": "Mordor", "aliases": ["Mordor"]},
                {"label": "Minas Tirith", "aliases": ["Minas Tirith"]},
            ],
            "artifacts": [
                {"label": "One Ring", "aliases": ["One Ring", "The One Ring", "Ring"]},
                {"label": "Red Book", "aliases": ["Red Book", "Red Book of Westmarch"]},
                {"label": "Mithril", "aliases": ["Mithril"]},
            ],
            "works_and_contributors": [
                {"label": "The Lord of the Rings", "aliases": ["The Lord of the Rings"]},
                {"label": "The Hobbit", "aliases": ["The Hobbit"]},
                {
                    "label": "J. R. R. Tolkien",
                    "aliases": ["J. R. R. Tolkien", "J.R.R. Tolkien", "Tolkien"],
                },
                {"label": "Christopher Tolkien", "aliases": ["Christopher Tolkien"]},
            ],
        },
        "forbidden_titles": [
            "Definition of Done",
            "Definition of Ready",
            "Epic",
            "Story",
            "Task",
            "Requirements Engineering",
            "Domain Modeling",
        ],
    },
}


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    detail: str


def _json_request(
    endpoint: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    vault: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if vault is not None:
        headers["X-Okto-Neuron-Vault"] = vault
    request = Request(
        endpoint.rstrip("/") + path,
        data=body,
        method=method,
        headers=headers,
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - local dev tool
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8")
        except Exception:  # noqa: BLE001
            detail = str(exc)
        raise RuntimeError(f"{method} {path} failed: HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"{method} {path} failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"{method} {path} timed out after {timeout:.1f}s") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{method} {path} returned non-object JSON")
    return data


def vaults(endpoint: str) -> dict[str, Any]:
    return _json_request(endpoint, "GET", "/api/v1/vaults")


def _vault_matches(vault: str, row: dict[str, Any] | None) -> bool:
    if not isinstance(row, dict):
        return False
    wanted = vault.casefold()
    name = str(row.get("name") or "").casefold()
    path = str(row.get("path") or "").casefold()
    return wanted == name or wanted == path


def ensure_vault(endpoint: str, vault: str) -> dict[str, Any]:
    rows = vaults(endpoint).get("vaults") or []
    selected = next(
        (row for row in rows if isinstance(row, dict) and _vault_matches(vault, row)),
        None,
    )
    if selected is None:
        raise RuntimeError(
            f"vault {vault!r} is not registered with the running Okto Neuron application"
        )
    return {"status": "ok", "selected": selected}


def reset_vault(endpoint: str, vault: str) -> dict[str, Any]:
    return _json_request(endpoint, "POST", "/api/v1/reset", {}, vault=vault)


def ingest_folder(endpoint: str, vault: str, source: Path, *, recursive: bool) -> dict[str, Any]:
    return _json_request(
        endpoint,
        "POST",
        "/api/v1/ingest-folder",
        {"path": str(source.expanduser().resolve(strict=False)), "recursive": recursive},
        vault=vault,
    )


def _source_files(source: Path, *, recursive: bool) -> list[Path]:
    patterns = ("*.md", "*.markdown", "*.txt")
    files: list[Path] = []
    for pattern in patterns:
        files.extend(source.rglob(pattern) if recursive else source.glob(pattern))
    return sorted({path.resolve(strict=False) for path in files}, key=lambda path: path.name.lower())


def ingest_batch(
    endpoint: str,
    vault: str,
    source: Path,
    *,
    recursive: bool,
    max_files: int | None,
    max_chars_per_file: int | None,
) -> dict[str, Any]:
    files = _source_files(source, recursive=recursive)
    if max_files is not None:
        files = files[:max_files]
    if not files:
        raise RuntimeError(f"no source files found under {source}")
    payload_files: list[dict[str, str]] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        if max_chars_per_file is not None and len(text) > max_chars_per_file:
            text = text[:max_chars_per_file]
        if text.strip():
            payload_files.append({"filename": path.name, "content": text})
    if not payload_files:
        raise RuntimeError(f"no non-empty source files found under {source}")
    result = _json_request(
        endpoint,
        "POST",
        "/api/v1/ingest-batch",
        {"files": payload_files},
        vault=vault,
    )
    result["selected_files"] = [entry["filename"] for entry in payload_files]
    result["max_chars_per_file"] = max_chars_per_file
    return result


def wait_for_ingest(
    endpoint: str,
    vault: str,
    *,
    timeout_s: float,
    poll_s: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last = {}
    while time.monotonic() < deadline:
        last = _json_request(endpoint, "GET", "/api/v1/ingest-queue", vault=vault)
        summary = last.get("summary") or {}
        if (
            not summary.get("active")
            and summary.get("queued", 0) == 0
            and summary.get("processing", 0) == 0
        ):
            return last
        time.sleep(poll_s)
    raise TimeoutError(f"ingest did not finish within {timeout_s:.1f}s: {last}")


def graph_stats(
    endpoint: str,
    vault: str,
    *,
    request_timeout_s: float = 30.0,
) -> dict[str, Any]:
    return _json_request(
        endpoint,
        "GET",
        "/api/v1/graph/stats",
        vault=vault,
        timeout=request_timeout_s,
    )


def semantic_quality(
    endpoint: str,
    vault: str,
    *,
    request_timeout_s: float = 30.0,
) -> dict[str, Any]:
    """Run the product-owned complete-store semantic evaluation."""

    payload = _json_request(
        endpoint,
        "POST",
        "/api/v1/quality/semantic",
        {},
        vault=vault,
        timeout=request_timeout_s,
    )
    report = payload.get("semantic_quality")
    if not isinstance(report, dict):
        raise RuntimeError("semantic-quality endpoint returned no report")
    return report


def graph_overview(
    endpoint: str,
    vault: str,
    *,
    limit: int = 5000,
    request_timeout_s: float = 30.0,
) -> dict[str, Any]:
    return _json_request(
        endpoint,
        "GET",
        f"/api/v1/graph?{urlencode({'limit': limit})}",
        vault=vault,
        timeout=request_timeout_s,
    )


def ledger_runs(
    endpoint: str,
    vault: str,
    *,
    limit: int = 50,
    request_timeout_s: float = 30.0,
) -> dict[str, Any]:
    return _json_request(
        endpoint,
        "GET",
        f"/api/v1/ledger/runs?{urlencode({'limit': limit})}",
        vault=vault,
        timeout=request_timeout_s,
    )


def ledger_run_detail(
    endpoint: str,
    vault: str,
    run_id: str,
    *,
    request_timeout_s: float = 30.0,
) -> dict[str, Any]:
    return _json_request(
        endpoint,
        "GET",
        f"/api/v1/ledger/runs/{run_id}",
        vault=vault,
        timeout=request_timeout_s,
    )


def ingest_queue_item(
    endpoint: str,
    vault: str,
    item_id: str,
    *,
    request_timeout_s: float = 30.0,
) -> dict[str, Any]:
    return _json_request(
        endpoint,
        "GET",
        f"/api/v1/ingest-queue/{item_id}",
        vault=vault,
        timeout=request_timeout_s,
    )


def summarize_ledger_detail(detail: dict[str, Any], *, recent_limit: int = 20) -> dict[str, Any]:
    candidates = detail.get("candidates") or []
    comparisons = detail.get("comparisons") or []
    commit_plans = detail.get("commit_plans") or []
    commit_records = detail.get("commit_records") or []
    unique_candidates: dict[str, dict[str, Any]] = {}
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            continue
        key = str(candidate.get("candidate_id") or f"row:{index}")
        existing = unique_candidates.get(key)
        if existing is None:
            unique_candidates[key] = candidate
            continue
        existing_type = str(existing.get("type") or "")
        candidate_type = str(candidate.get("type") or "")
        if not existing_type and candidate_type:
            unique_candidates[key] = candidate
    candidate_kinds: dict[str, int] = {}
    candidate_origins_by_kind: dict[str, dict[str, int]] = {}
    candidate_row_states_by_kind: dict[str, dict[str, int]] = {}
    candidate_node_types: dict[str, int] = {}
    candidate_kind_by_id: dict[str, str] = {}
    comparison_methods: dict[str, int] = {}
    comparison_verdicts: dict[str, int] = {}
    compared_ids_by_method: dict[str, set[str]] = {}
    compared_ids_by_verdict: dict[str, set[str]] = {}
    final_node_verdict_by_id: dict[str, str] = {}
    terminal_relation_review_count = 0
    active_node_total: int | None = None
    active_edge_total: int | None = None
    for candidate in unique_candidates.values():
        kind = str(candidate.get("candidate_kind") or "unknown")
        candidate_id = str(candidate.get("candidate_id") or "")
        if candidate_id:
            candidate_kind_by_id[candidate_id] = kind
        candidate_kinds[kind] = candidate_kinds.get(kind, 0) + 1
        origin = _candidate_origin(candidate)
        origin_counts = candidate_origins_by_kind.setdefault(kind, {})
        origin_counts[origin] = origin_counts.get(origin, 0) + 1
        payload = candidate.get("payload") or {}
        if kind == "node" and isinstance(payload, dict):
            node_type = str(candidate.get("type") or payload.get("type") or "unknown")
            candidate_node_types[node_type] = candidate_node_types.get(node_type, 0) + 1
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        kind = str(candidate.get("candidate_kind") or "unknown")
        state = str(candidate.get("state") or "unknown")
        state_counts = candidate_row_states_by_kind.setdefault(kind, {})
        state_counts[state] = state_counts.get(state, 0) + 1
    recent_comparisons: list[dict[str, Any]] = []
    for comparison in comparisons:
        if not isinstance(comparison, dict):
            continue
        method = str(comparison.get("method") or "unknown")
        verdict = str(comparison.get("verdict") or "unknown")
        candidate_id = str(comparison.get("candidate_id") or "")
        payload = comparison.get("payload") or {}
        is_audit = isinstance(payload, dict) and payload.get("audit_only") is True
        comparison_methods[method] = comparison_methods.get(method, 0) + 1
        comparison_verdicts[verdict] = comparison_verdicts.get(verdict, 0) + 1
        if isinstance(payload, dict):
            after = payload.get("after")
            if isinstance(after, dict):
                if isinstance(after.get("nodes"), int):
                    active_node_total = int(after["nodes"])
                if isinstance(after.get("edges"), int):
                    active_edge_total = int(after["edges"])
            survivors = payload.get("survivors")
            if isinstance(survivors, list):
                active_node_total = len(survivors)
            nodes = payload.get("nodes")
            if isinstance(nodes, list):
                active_node_total = len(nodes)
            edges = payload.get("edges")
            if isinstance(edges, list):
                active_edge_total = len(edges)
        if candidate_id and not is_audit:
            if method == "curator":
                final_node_verdict_by_id[candidate_id] = verdict
            elif method == "relation_curator":
                terminal_relation_review_count += 1
        if candidate_id:
            compared_ids_by_method.setdefault(method, set()).add(candidate_id)
            compared_ids_by_verdict.setdefault(verdict, set()).add(candidate_id)
        recent_comparisons.append(
            {
                "candidate_id": candidate_id or None,
                "ts": comparison.get("ts"),
                "method": method,
                "verdict": verdict,
                "score": comparison.get("score"),
                "reason": str(comparison.get("reason") or "")[:240],
            }
        )
    compared_candidate_kinds_by_method = {
        key: _candidate_kind_counts(value, candidate_kind_by_id)
        for key, value in sorted(compared_ids_by_method.items())
    }
    active_candidate_kinds = dict(candidate_kinds)
    if active_node_total is not None:
        active_candidate_kinds["node"] = active_node_total
    if active_edge_total is not None:
        active_candidate_kinds["edge"] = active_edge_total
    fallback_progress = _ledger_progress(
        active_candidate_kinds,
        compared_candidate_kinds_by_method,
    )
    progress = {
        "node_curator": _progress_row(
            len(final_node_verdict_by_id),
            int(active_candidate_kinds.get("node") or 0),
        )
        if active_node_total is not None
        else fallback_progress["node_curator"],
        "relation_curator": _progress_row(
            terminal_relation_review_count,
            int(active_candidate_kinds.get("edge") or 0),
        )
        if active_edge_total is not None
        else fallback_progress["relation_curator"],
    }
    return {
        "run": detail.get("run"),
        "counts": {
            "candidates": len(unique_candidates),
            "candidate_rows": len(candidates),
            "comparisons": len(comparisons),
            "commit_plans": len(commit_plans),
            "commit_records": len(commit_records),
        },
        "candidate_kinds": dict(sorted(candidate_kinds.items())),
        "active_candidate_kinds": dict(sorted(active_candidate_kinds.items())),
        "candidate_origins_by_kind": {
            key: dict(sorted(value.items()))
            for key, value in sorted(candidate_origins_by_kind.items())
        },
        "candidate_row_states_by_kind": {
            key: dict(sorted(value.items()))
            for key, value in sorted(candidate_row_states_by_kind.items())
        },
        "candidate_node_types": dict(sorted(candidate_node_types.items())),
        "comparison_methods": dict(sorted(comparison_methods.items())),
        "comparison_verdicts": dict(sorted(comparison_verdicts.items())),
        "unique_compared_candidates_by_method": {
            key: len(value) for key, value in sorted(compared_ids_by_method.items())
        },
        "unique_compared_candidates_by_verdict": {
            key: len(value) for key, value in sorted(compared_ids_by_verdict.items())
        },
        "compared_candidate_kinds_by_method": compared_candidate_kinds_by_method,
        "progress": progress,
        "recent_comparisons": recent_comparisons[-recent_limit:],
    }


def _candidate_origin(candidate: dict[str, Any]) -> str:
    if candidate.get("derived"):
        return str(candidate.get("derivation_reason") or "derived")
    payload = candidate.get("payload") or {}
    if not isinstance(payload, dict):
        return "raw"
    if payload.get("derived"):
        return str(payload.get("derivation_reason") or "derived")
    nested = payload.get("candidate")
    if isinstance(nested, dict) and nested.get("derived"):
        return str(nested.get("derivation_reason") or "derived")
    return "raw"


def local_candidate_origins(vault_path: str | None, run_id: str) -> dict[str, dict[str, int]]:
    """Return candidate origin counts from the local ledger, when available.

    The HTTP ledger detail deliberately exposes sanitized candidate summaries.
    Local quality checks can use the append-only ledger file for derived/audit
    markers without changing the server API contract.
    """
    if not vault_path:
        return {}
    ledger_path = Path(vault_path) / ".marginalia" / "candidate-ledger.jsonl"
    if not ledger_path.exists():
        return {}
    unique_candidates: dict[str, dict[str, Any]] = {}
    for index, line in enumerate(ledger_path.read_text(errors="replace").splitlines()):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("run_id") != run_id or record.get("kind") != "candidate":
            continue
        key = str(record.get("candidate_id") or f"row:{index}")
        existing = unique_candidates.get(key)
        if existing is None:
            unique_candidates[key] = record
            continue
        if _candidate_origin(existing) == "raw" and _candidate_origin(record) != "raw":
            unique_candidates[key] = record

    origins_by_kind: dict[str, dict[str, int]] = {}
    for candidate in unique_candidates.values():
        kind = str(candidate.get("candidate_kind") or "unknown")
        origin = _candidate_origin(candidate)
        counts = origins_by_kind.setdefault(kind, {})
        counts[origin] = counts.get(origin, 0) + 1
    return {
        key: dict(sorted(value.items()))
        for key, value in sorted(origins_by_kind.items())
    }


def local_relation_review_summary(
    vault_path: str | None,
    run_id: str,
    *,
    limit: int = 12,
) -> dict[str, Any]:
    if not vault_path:
        return {}
    ledger_path = Path(vault_path) / ".marginalia" / "candidate-ledger.jsonl"
    if not ledger_path.exists():
        return {}

    verdicts: Counter[str] = Counter()
    audit_modes: Counter[str] = Counter()
    relation_kinds: Counter[str] = Counter()
    terminal_actions: Counter[str] = Counter()
    canonical_predicates: Counter[str] = Counter()
    endpoint_gate_reasons: Counter[str] = Counter()
    predicates_by_verdict: dict[str, Counter[str]] = {}
    recent: list[dict[str, Any]] = []

    for line in ledger_path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("run_id") != run_id or record.get("kind") != "comparison":
            continue
        method = str(record.get("method") or "unknown")
        if method == "endpoint_gate":
            endpoint_gate_reasons[str(record.get("reason") or "unknown")] += 1
            continue
        if method != "relation_curator":
            continue
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        verdict = str(record.get("verdict") or "unknown")
        predicate = str(payload.get("type") or "unknown")
        canonical = str(payload.get("canonical_predicate") or "")
        relation_kind = str(payload.get("relation_kind") or "unknown")
        terminal_action = str(payload.get("proposed_terminal_action") or "unknown")
        if payload.get("audit_only") is True:
            mode = "llm_skipped" if payload.get("llm_skipped") is True else "llm_reviewed"
            audit_modes[mode] += 1

        verdicts[verdict] += 1
        relation_kinds[relation_kind] += 1
        terminal_actions[terminal_action] += 1
        predicates_by_verdict.setdefault(verdict, Counter())[predicate] += 1
        if canonical:
            canonical_predicates[canonical] += 1
        recent.append(
            {
                "candidate_id": record.get("candidate_id"),
                "verdict": verdict,
                "relation_kind": relation_kind,
                "predicate": predicate,
                "canonical_predicate": canonical or None,
                "terminal_action": terminal_action,
                "reason": str(record.get("reason") or "")[:240],
            }
        )

    if not verdicts and not endpoint_gate_reasons:
        return {}
    return {
        "relation_curator_verdicts": _top_counts(verdicts, limit=limit),
        "audit_modes": _top_counts(audit_modes, limit=limit),
        "relation_kinds": _top_counts(relation_kinds, limit=limit),
        "terminal_actions": _top_counts(terminal_actions, limit=limit),
        "canonical_predicates": _top_counts(canonical_predicates, limit=limit),
        "predicates_by_verdict": {
            verdict: _top_counts(counts, limit=limit)
            for verdict, counts in sorted(predicates_by_verdict.items())
        },
        "endpoint_gate_reasons": _top_counts(endpoint_gate_reasons, limit=limit),
        "recent_relation_reviews": recent[-limit:],
    }


def local_node_review_summary(
    vault_path: str | None,
    run_id: str,
    *,
    limit: int = 12,
) -> dict[str, Any]:
    if not vault_path:
        return {}
    ledger_path = Path(vault_path) / ".marginalia" / "candidate-ledger.jsonl"
    if not ledger_path.exists():
        return {}

    node_by_id: dict[str, dict[str, Any]] = {}
    candidate_states: Counter[str] = Counter()
    states_by_type: dict[str, Counter[str]] = {}
    title_candidates: dict[str, dict[str, Any]] = {}
    comparisons: list[dict[str, Any]] = []

    for line in ledger_path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("run_id") != run_id:
            continue
        if record.get("kind") == "candidate" and record.get("candidate_kind") == "node":
            candidate_id = str(record.get("candidate_id") or "")
            node_payload = _node_candidate_payload(record)
            if candidate_id and candidate_id not in node_by_id:
                node_by_id[candidate_id] = node_payload
            state = str(record.get("state") or "unknown")
            node_type = str(node_payload.get("type") or "unknown")
            title = str(node_payload.get("title") or candidate_id)
            title_key = title.casefold()
            title_row = title_candidates.setdefault(
                title_key,
                {"title": title, "types": set()},
            )
            title_row["types"].add(node_type)
            candidate_states[state] += 1
            states_by_type.setdefault(state, Counter())[node_type] += 1
        elif record.get("kind") == "comparison" and record.get("method") == "curator":
            comparisons.append(record)

    if not candidate_states and not comparisons:
        return {}

    verdicts: Counter[str] = Counter()
    audit_modes: Counter[str] = Counter()
    types_by_verdict: dict[str, Counter[str]] = {}
    titles_by_verdict: dict[str, set[str]] = {}
    title_verdicts: dict[str, dict[str, Any]] = {}
    recent: list[dict[str, Any]] = []
    for record in comparisons:
        candidate_id = str(record.get("candidate_id") or "")
        node_payload = node_by_id.get(candidate_id) or {}
        verdict = str(record.get("verdict") or "unknown")
        payload = record.get("payload") or {}
        if isinstance(payload, dict) and payload.get("audit_only") is True:
            mode = "llm_skipped" if payload.get("llm_skipped") is True else "llm_reviewed"
            audit_modes[mode] += 1
        node_type = str(node_payload.get("type") or "unknown")
        title = str(node_payload.get("title") or candidate_id)
        title_key = title.casefold()
        verdicts[verdict] += 1
        types_by_verdict.setdefault(verdict, Counter())[node_type] += 1
        titles_by_verdict.setdefault(verdict, set()).add(title)
        title_row = title_verdicts.setdefault(
            title_key,
            {"title": title, "types": set(), "verdicts": set()},
        )
        title_row["types"].add(node_type)
        title_row["verdicts"].add(verdict)
        recent.append(
            {
                "candidate_id": candidate_id or None,
                "verdict": verdict,
                "type": node_type,
                "title": title,
                "reason": str(record.get("reason") or "")[:240],
            }
        )

    return {
        "candidate_states": _top_counts(candidate_states, limit=limit),
        "candidate_states_by_type": {
            state: _top_counts(counts, limit=limit)
            for state, counts in sorted(states_by_type.items())
        },
        "node_curator_verdicts": _top_counts(verdicts, limit=limit),
        "audit_modes": _top_counts(audit_modes, limit=limit),
        "node_types_by_verdict": {
            verdict: _top_counts(counts, limit=limit)
            for verdict, counts in sorted(types_by_verdict.items())
        },
        "sample_titles_by_verdict": {
            verdict: sorted(titles, key=str.casefold)[:limit]
            for verdict, titles in sorted(titles_by_verdict.items())
        },
        "titles_with_multiple_types": _title_multi_type_rows(title_candidates, limit=limit),
        "titles_with_multiple_verdicts": _title_overlap_rows(title_verdicts, limit=limit),
        "recent_node_reviews": recent[-limit:],
    }


def local_endpoint_shadow_summary(
    vault_path: str | None,
    run_id: str,
    *,
    limit: int = 12,
) -> dict[str, Any]:
    if not vault_path:
        return {}
    ledger_path = Path(vault_path) / ".marginalia" / "candidate-ledger.jsonl"
    if not ledger_path.exists():
        return {}

    node_by_id: dict[str, dict[str, Any]] = {}
    node_verdict_by_id: dict[str, str] = {}
    node_state_by_id: dict[str, str] = {}
    endpoint_gates: list[dict[str, Any]] = []
    for line in ledger_path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("run_id") != run_id:
            continue
        if record.get("kind") == "candidate" and record.get("candidate_kind") == "node":
            candidate_id = str(record.get("candidate_id") or "")
            if candidate_id and candidate_id not in node_by_id:
                node_by_id[candidate_id] = _node_candidate_payload(record)
            if candidate_id:
                node_state_by_id[candidate_id] = str(record.get("state") or "unknown")
        elif record.get("kind") == "comparison" and record.get("method") == "curator":
            candidate_id = str(record.get("candidate_id") or "")
            if candidate_id:
                node_verdict_by_id[candidate_id] = str(record.get("verdict") or "unknown")
        elif record.get("kind") == "comparison" and record.get("method") == "endpoint_gate":
            endpoint_gates.append(record)

    if not endpoint_gates:
        return {}

    accepted_by_key: dict[tuple[str, str], list[str]] = {}
    for candidate_id, node_payload in node_by_id.items():
        if node_verdict_by_id.get(candidate_id) != "commit":
            continue
        if node_state_by_id.get(candidate_id) == "superseded":
            continue
        key = _entity_key(node_payload)
        if key is not None:
            accepted_by_key.setdefault(key, []).append(candidate_id)

    missing_endpoint_verdicts: Counter[str] = Counter()
    missing_endpoint_types: Counter[str] = Counter()
    shadowed_by_field_type_verdict: Counter[str] = Counter()
    shadowed_total = 0
    remappable_total = 0
    ambiguous_total = 0
    samples: list[dict[str, Any]] = []

    for record in endpoint_gates:
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        reason = str(record.get("reason") or "")
        missing_field = "dst_ref" if "dst_ref" in reason else "src_ref"
        missing_ref = str(payload.get(missing_field) or "")
        node_payload = node_by_id.get(missing_ref)
        if not node_payload:
            continue
        node_type = str(node_payload.get("type") or "unknown")
        title = str(node_payload.get("title") or "")
        verdict = node_verdict_by_id.get(missing_ref, "unknown")
        missing_endpoint_verdicts[verdict] += 1
        missing_endpoint_types[node_type] += 1

        accepted_ids = accepted_by_key.get(_entity_key(node_payload) or ("", ""), [])
        if not accepted_ids:
            continue
        shadowed_total += 1
        if len(set(accepted_ids)) == 1:
            remappable_total += 1
        else:
            ambiguous_total += 1
        shadowed_by_field_type_verdict[f"{missing_field}:{node_type}:{verdict}"] += 1
        if len(samples) < limit:
            samples.append(
                {
                    "candidate_id": record.get("candidate_id"),
                    "missing_field": missing_field,
                    "missing_ref": missing_ref,
                    "missing_type": node_type,
                    "missing_title": title,
                    "missing_verdict": verdict,
                    "accepted_same_title_refs": sorted(set(accepted_ids))[:limit],
                    "reason": reason[:240],
                }
            )

    return {
        "endpoint_gate_total": len(endpoint_gates),
        "shadowed_by_accepted_same_title": shadowed_total,
        "remappable_exact_title_endpoint_gates": remappable_total,
        "ambiguous_exact_title_endpoint_gates": ambiguous_total,
        "missing_endpoint_verdicts": _top_counts(missing_endpoint_verdicts, limit=limit),
        "missing_endpoint_types": _top_counts(missing_endpoint_types, limit=limit),
        "shadowed_by_field_type_verdict": _top_counts(
            shadowed_by_field_type_verdict,
            limit=limit,
        ),
        "samples": samples,
    }


def local_pending_commit_preview(
    vault_path: str | None,
    run_id: str,
    *,
    limit: int = 12,
) -> dict[str, Any]:
    if not vault_path:
        return {}
    ledger_path = Path(vault_path) / ".marginalia" / "candidate-ledger.jsonl"
    if not ledger_path.exists():
        return {}

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

    for line in ledger_path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("run_id") != run_id:
            continue
        candidate_id = str(record.get("candidate_id") or "")
        if record.get("kind") == "candidate" and record.get("candidate_kind") == "node":
            if candidate_id and candidate_id not in node_by_id:
                node_by_id[candidate_id] = _node_candidate_payload(record)
            if candidate_id:
                node_state_by_id[candidate_id] = str(record.get("state") or "unknown")
            continue
        if record.get("kind") != "comparison":
            continue
        payload = record.get("payload") or {}
        if not isinstance(payload, dict) or payload.get("audit_only") is True:
            continue
        method = str(record.get("method") or "")
        verdict = str(record.get("verdict") or "unknown")
        if method == "curator":
            final_node_verdict_by_id[candidate_id] = verdict
            node_payload = node_by_id.get(candidate_id) or {}
            node_type = str(node_payload.get("type") or "unknown")
            title = str(node_payload.get("title") or candidate_id)
            node_types_by_verdict.setdefault(verdict, Counter())[node_type] += 1
            node_titles_by_verdict.setdefault(verdict, []).append(title)
            samples = node_samples_by_verdict.setdefault(verdict, [])
            if len(samples) < limit:
                samples.append(
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
            samples = relation_samples_by_verdict.setdefault(verdict, [])
            if len(samples) < limit:
                samples.append(
                    _relation_sample(
                        candidate_id,
                        payload,
                        predicate=predicate,
                        terminal=terminal,
                        node_by_id=node_by_id,
                    )
                )

    if not final_node_verdict_by_id and not relation_terminal_by_verdict:
        return {}

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
    relation_queue_terminals = Counter()
    for verdict, counts in relation_terminal_by_verdict.items():
        if verdict != "commit":
            relation_queue_terminals.update(counts)

    return {
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
            "accepted_for_write": int(relation_commit_terminals.get("create_edge_or_claim") or 0),
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


def local_pending_domain_profile(
    vault_path: str | None,
    run_id: str,
    profile_name: str | None,
) -> dict[str, Any]:
    """Domain profile coverage over accepted pre-commit node candidates."""
    if not vault_path or not profile_name:
        return {}
    ledger_path = Path(vault_path) / ".marginalia" / "candidate-ledger.jsonl"
    if not ledger_path.exists():
        return {}

    node_by_id: dict[str, dict[str, Any]] = {}
    node_state_by_id: dict[str, str] = {}
    final_node_verdict_by_id: dict[str, str] = {}
    node_reason_by_id: dict[str, str] = {}
    for line in ledger_path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("run_id") != run_id:
            continue
        candidate_id = str(record.get("candidate_id") or "")
        if record.get("kind") == "candidate" and record.get("candidate_kind") == "node":
            if candidate_id and candidate_id not in node_by_id:
                node_by_id[candidate_id] = _node_candidate_payload(record)
            if candidate_id:
                node_state_by_id[candidate_id] = str(record.get("state") or "unknown")
            continue
        if record.get("kind") != "comparison" or record.get("method") != "curator":
            continue
        payload = record.get("payload") or {}
        if isinstance(payload, dict) and payload.get("audit_only") is True:
            continue
        if candidate_id:
            final_node_verdict_by_id[candidate_id] = str(record.get("verdict") or "unknown")
            node_reason_by_id[candidate_id] = str(record.get("reason") or "")

    node_records = sorted(
        (
            {
                "name": str(payload.get("title") or candidate_id),
                "type": str(payload.get("type") or "unknown"),
            }
            for candidate_id, payload in node_by_id.items()
            if final_node_verdict_by_id.get(candidate_id) == "commit"
            and node_state_by_id.get(candidate_id) != "superseded"
        ),
        key=lambda row: (row["type"], row["name"].casefold()),
    )
    if not node_records:
        return {}
    summary = _domain_profile_summary(profile_name, node_records)
    summary["source"] = "pending_commit_preview"
    summary["node_count"] = len(node_records)
    missing_diagnostics = _pending_domain_missing_diagnostics(
        profile_name,
        summary,
        node_by_id=node_by_id,
        node_state_by_id=node_state_by_id,
        final_node_verdict_by_id=final_node_verdict_by_id,
        node_reason_by_id=node_reason_by_id,
    )
    if missing_diagnostics:
        summary["missing_diagnostics"] = missing_diagnostics
    return summary


def _profile_aliases_by_label(profile_name: str) -> dict[str, set[str]]:
    profile = DOMAIN_PROFILES.get(profile_name) or {}
    aliases_by_label: dict[str, set[str]] = {}
    for entries in (profile.get("groups") or {}).values():
        for entry in entries:
            label = str(entry.get("label") or "")
            aliases_by_label[label] = {
                label.casefold(),
                *(str(alias).casefold() for alias in entry.get("aliases") or []),
            }
    return aliases_by_label


def _pending_domain_missing_diagnostics(
    profile_name: str,
    summary: dict[str, Any],
    *,
    node_by_id: dict[str, dict[str, Any]],
    node_state_by_id: dict[str, str],
    final_node_verdict_by_id: dict[str, str],
    node_reason_by_id: dict[str, str],
    limit: int = 8,
) -> list[dict[str, Any]]:
    aliases_by_label = _profile_aliases_by_label(profile_name)
    diagnostics: list[dict[str, Any]] = []
    for group_name, group in (summary.get("groups") or {}).items():
        if not isinstance(group, dict):
            continue
        for label in group.get("missing") or []:
            label_text = str(label)
            aliases = aliases_by_label.get(label_text) or {label_text.casefold()}
            matches: list[dict[str, Any]] = []
            states: Counter[str] = Counter()
            verdicts: Counter[str] = Counter()
            types: Counter[str] = Counter()
            for candidate_id, payload in node_by_id.items():
                title = str(payload.get("title") or "")
                if title.casefold() not in aliases:
                    continue
                state = str(node_state_by_id.get(candidate_id) or "unknown")
                verdict = str(final_node_verdict_by_id.get(candidate_id) or "no_final_verdict")
                node_type = str(payload.get("type") or "unknown")
                states[state] += 1
                verdicts[verdict] += 1
                types[node_type] += 1
                if len(matches) < limit:
                    matches.append(
                        {
                            "candidate_id": candidate_id,
                            "title": title,
                            "type": node_type,
                            "state": state,
                            "verdict": verdict,
                            "reason": node_reason_by_id.get(candidate_id, "")[:240],
                        }
                    )
            if matches:
                diagnostics.append(
                    {
                        "label": label_text,
                        "group": str(group_name),
                        "candidate_count": sum(states.values()),
                        "states": _top_counts(states, limit=limit),
                        "verdicts": _top_counts(verdicts, limit=limit),
                        "types": _top_counts(types, limit=limit),
                        "samples": matches,
                    }
                )
    return diagnostics


def summarize_ingest_extraction(
    endpoint: str,
    vault: str,
    queue: dict[str, Any] | None,
    *,
    domain_profile: str | None = None,
    item_limit: int = 8,
    sample_limit: int = 12,
    request_timeout_s: float = 30.0,
) -> dict[str, Any]:
    if not isinstance(queue, dict):
        return {}
    items = [
        item
        for item in queue.get("items") or []
        if isinstance(item, dict) and item.get("id") and int(item.get("event_count") or 0) > 0
    ]
    if not items:
        return {}
    priority = {"processing": 0, "done": 1, "error": 2, "cancelled": 3, "queued": 4}
    items = sorted(
        items,
        key=lambda item: (
            priority.get(str(item.get("status") or ""), 9),
            str(item.get("name") or "").casefold(),
        ),
    )[:item_limit]

    reports: list[dict[str, Any]] = []
    aggregate_node_mentions = 0
    aggregate_relation_candidates = 0
    aggregate_claim_candidates = 0
    aggregate_node_records: list[dict[str, str]] = []
    aggregate_node_types: Counter[str] = Counter()
    aggregate_titles: Counter[str] = Counter()
    aggregate_relation_types: Counter[str] = Counter()
    aggregate_claim_predicates: Counter[str] = Counter()

    for item in items:
        detail = ingest_queue_item(
            endpoint,
            vault,
            str(item["id"]),
            request_timeout_s=request_timeout_s,
        )
        payload = detail.get("item") or {}
        item_report = _summarize_extraction_item(
            payload if isinstance(payload, dict) else item,
            domain_profile=domain_profile,
            sample_limit=sample_limit,
        )
        reports.append(item_report)
        counts = item_report.get("counts") or {}
        aggregate_node_mentions += int(counts.get("node_mentions") or 0)
        aggregate_relation_candidates += int(counts.get("relation_candidates") or 0)
        aggregate_claim_candidates += int(counts.get("claim_candidates") or 0)
        for record in item_report.get("node_records") or []:
            if not isinstance(record, dict):
                continue
            title = str(record.get("name") or "")
            node_type = str(record.get("type") or "unknown")
            if not title:
                continue
            aggregate_node_records.append({"name": title, "type": node_type})
            aggregate_node_types[node_type] += 1
            aggregate_titles[title] += 1
        aggregate_relation_types.update(
            Counter({
                str(key): int(value)
                for key, value in (item_report.get("relation_types") or {}).items()
            })
        )
        aggregate_claim_predicates.update(
            Counter({
                str(key): int(value)
                for key, value in (item_report.get("claim_predicates") or {}).items()
            })
        )

    aggregate = {
        "items_inspected": len(reports),
        "counts": {
            "node_mentions": aggregate_node_mentions,
            "relation_candidates": aggregate_relation_candidates,
            "claim_candidates": aggregate_claim_candidates,
            "unique_node_titles": len({row["name"].casefold() for row in aggregate_node_records}),
        },
        "node_types": _top_counts(aggregate_node_types, limit=sample_limit),
        "top_titles": _top_counts(aggregate_titles, limit=sample_limit),
        "relation_types": _top_counts(aggregate_relation_types, limit=sample_limit),
        "claim_predicates": _top_counts(aggregate_claim_predicates, limit=sample_limit),
    }
    if domain_profile:
        aggregate["domain_profile"] = _domain_profile_summary(domain_profile, aggregate_node_records)

    return {
        "scope": "retained_ingest_events",
        "note": (
            "Counts come from bounded ingest event payloads. On older running daemons "
            "this is a recent window, not the full file total."
        ),
        "aggregate": aggregate,
        "items": reports,
    }


def _summarize_extraction_item(
    item: dict[str, Any],
    *,
    domain_profile: str | None,
    sample_limit: int,
) -> dict[str, Any]:
    node_records: list[dict[str, str]] = []
    node_types: Counter[str] = Counter()
    titles: Counter[str] = Counter()
    relation_types: Counter[str] = Counter()
    claim_predicates: Counter[str] = Counter()
    extraction_events = 0
    node_mentions = 0
    relation_candidates = 0
    claim_candidates = 0
    block_indices: list[int] = []

    for event in item.get("events") or []:
        if not isinstance(event, dict) or event.get("kind") != "extraction_result":
            continue
        extraction_events += 1
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        block = payload.get("block") or {}
        if isinstance(block, dict) and isinstance(block.get("index"), int):
            block_indices.append(int(block["index"]))
        for node in payload.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            title = str(node.get("title") or "").strip()
            if not title:
                continue
            node_type = str(node.get("type") or "unknown")
            node_records.append({"name": title, "type": node_type})
            node_mentions += 1
            node_types[node_type] += 1
            titles[title] += 1
        for edge in payload.get("edges") or []:
            if not isinstance(edge, dict):
                continue
            if edge.get("dst_literal") is not None:
                claim_candidates += 1
                claim_predicates[str(edge.get("type") or "unknown")] += 1
            else:
                relation_candidates += 1
                relation_types[str(edge.get("type") or "unknown")] += 1
        for claim in payload.get("claims") or []:
            if not isinstance(claim, dict):
                continue
            claim_candidates += 1
            claim_predicates[str(claim.get("predicate") or "unknown")] += 1

    report: dict[str, Any] = {
        "id": item.get("id"),
        "name": item.get("name"),
        "status": item.get("status"),
        "stage": item.get("stage"),
        "blocks_done": item.get("blocks_done"),
        "blocks_total": item.get("blocks_total"),
        "retained_events": len(item.get("events") or []),
        "extraction_result_events": extraction_events,
        "retained_block_range": (
            {"first": min(block_indices), "last": max(block_indices)}
            if block_indices
            else None
        ),
        "counts": {
            "node_mentions": node_mentions,
            "relation_candidates": relation_candidates,
            "claim_candidates": claim_candidates,
            "unique_node_titles": len({row["name"].casefold() for row in node_records}),
        },
        "cumulative_queue_counts": {
            "extracted_nodes": item.get("extracted_nodes"),
            "extracted_edges": item.get("extracted_edges"),
            "extracted_claims": item.get("extracted_claims"),
        },
        "node_types": _top_counts(node_types, limit=sample_limit),
        "top_titles": _top_counts(titles, limit=sample_limit),
        "relation_types": _top_counts(relation_types, limit=sample_limit),
        "claim_predicates": _top_counts(claim_predicates, limit=sample_limit),
        "node_records": sorted(
            node_records,
            key=lambda row: (row["type"], row["name"].casefold()),
        ),
    }
    if domain_profile:
        report["domain_profile"] = _domain_profile_summary(domain_profile, node_records)
    return report


def _title_overlap_rows(
    title_rows: dict[str, dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in title_rows.values():
        verdicts = row.get("verdicts") or set()
        if len(verdicts) < 2:
            continue
        rows.append(
            {
                "title": str(row.get("title") or ""),
                "types": sorted(str(value) for value in row.get("types") or []),
                "verdicts": sorted(str(value) for value in verdicts),
            }
        )
    return sorted(rows, key=lambda row: row["title"].casefold())[:limit]


def _title_multi_type_rows(
    title_rows: dict[str, dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in title_rows.values():
        types = row.get("types") or set()
        if len(types) < 2:
            continue
        rows.append(
            {
                "title": str(row.get("title") or ""),
                "types": sorted(str(value) for value in types),
            }
        )
    return sorted(rows, key=lambda row: row["title"].casefold())[:limit]


def _node_candidate_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    if isinstance(payload, dict):
        nested = payload.get("candidate")
        if isinstance(nested, dict):
            return nested
        return payload
    return record


def _entity_key(candidate: dict[str, Any]) -> tuple[str, str] | None:
    title = " ".join(str(candidate.get("title") or "").split()).casefold()
    type_ = str(candidate.get("type") or "").strip()
    if not title or not type_:
        return None
    return (type_, title)


def _top_counts(counts: Counter[str], *, limit: int) -> dict[str, int]:
    return {
        key: count
        for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    }


def _candidate_kind_counts(
    candidate_ids: set[str],
    candidate_kind_by_id: dict[str, str],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for candidate_id in candidate_ids:
        kind = candidate_kind_by_id.get(candidate_id, "unknown")
        counts[kind] = counts.get(kind, 0) + 1
    return dict(sorted(counts.items()))


def _progress_row(done: int, total: int) -> dict[str, Any]:
    remaining = max(total - done, 0)
    fraction = (done / total) if total else None
    return {
        "done": done,
        "total": total,
        "remaining": remaining,
        "fraction": fraction,
    }


def _ledger_progress(
    candidate_kinds: dict[str, int],
    compared_candidate_kinds_by_method: dict[str, dict[str, int]],
) -> dict[str, Any]:
    curator = compared_candidate_kinds_by_method.get("curator") or {}
    relation_curator = compared_candidate_kinds_by_method.get("relation_curator") or {}
    return {
        "node_curator": _progress_row(
            int(curator.get("node") or 0),
            int(candidate_kinds.get("node") or 0),
        ),
        "relation_curator": _progress_row(
            int(relation_curator.get("edge") or 0),
            int(candidate_kinds.get("edge") or 0),
        ),
    }


def build_report(
    endpoint: str,
    vault: str,
    *,
    queue: dict[str, Any] | None = None,
    include_ledger_detail: bool = False,
    domain_profile: str | None = None,
    request_timeout_s: float = 30.0,
) -> dict[str, Any]:
    stats = graph_stats(endpoint, vault, request_timeout_s=request_timeout_s)
    semantic_report = semantic_quality(endpoint, vault, request_timeout_s=request_timeout_s)
    graph = graph_overview(endpoint, vault, request_timeout_s=request_timeout_s)
    ledgers = ledger_runs(endpoint, vault, request_timeout_s=request_timeout_s)
    latest_run = (ledgers.get("runs") or [None])[0]
    nodes = graph.get("nodes") or []
    edges = graph.get("edges") or []
    node_names = sorted(
        str(node.get("name"))
        for node in nodes
        if isinstance(node, dict) and node.get("name")
    )
    knowledge_node_names = sorted(
        str(node.get("name"))
        for node in nodes
        if (
            isinstance(node, dict)
            and node.get("name")
            and str(node.get("type")) not in PROVENANCE_NODE_TYPES
        )
    )
    node_records = sorted(
        (
            {"name": str(node.get("name")), "type": str(node.get("type"))}
            for node in nodes
            if isinstance(node, dict) and node.get("name")
        ),
        key=lambda row: (row["type"], row["name"].casefold()),
    )
    edge_types = sorted(
        {
            str(edge.get("type"))
            for edge in edges
            if isinstance(edge, dict) and edge.get("type")
        }
    )
    report = {
        "status": "ok",
        "queue": compact_ingest_queue(queue),
        "stats": stats,
        "ledger": ledgers,
        "graph": {
            "total_nodes": graph.get("total_nodes"),
            "total_edges": graph.get("total_edges"),
            "returned_nodes": graph.get("returned_nodes"),
            "returned_edges": graph.get("returned_edges"),
            "truncated": graph.get("truncated"),
        },
        "semantic_quality": semantic_report,
        "snapshot_contract": {
            "semantic_quality": "atomic_writer_and_store_scan",
            "stats_overview_ledger": "diagnostic_non_atomic",
        },
        "node_names": node_names,
        "knowledge_node_names": knowledge_node_names,
        "node_records": node_records,
        "graph_profile": graph_profile(nodes, edges),
        "isolated_knowledge_nodes": _isolated_knowledge_nodes(nodes, edges),
        "edge_types": edge_types,
    }
    if domain_profile:
        report["domain_profile"] = _domain_profile_summary(domain_profile, node_records)
    ingest_extraction = summarize_ingest_extraction(
        endpoint,
        vault,
        queue,
        domain_profile=domain_profile,
        request_timeout_s=request_timeout_s,
    )
    if ingest_extraction:
        report["ingest_extraction"] = ingest_extraction
    if include_ledger_detail and isinstance(latest_run, dict) and latest_run.get("run_id"):
        detail = ledger_run_detail(
            endpoint,
            vault,
            str(latest_run["run_id"]),
            request_timeout_s=request_timeout_s,
        )
        ledger_summary = summarize_ledger_detail(detail)
        vault_path = None
        if isinstance(queue, dict):
            vault = queue.get("vault") or {}
            if isinstance(vault, dict):
                vault_path = str(vault.get("path") or "") or None
        local_origins = local_candidate_origins(vault_path, str(latest_run["run_id"]))
        if local_origins:
            ledger_summary["candidate_origins_by_kind"] = local_origins
            ledger_summary["candidate_origins_source"] = "local_ledger"
        else:
            ledger_summary["candidate_origins_source"] = "api_summary"
        node_summary = local_node_review_summary(
            vault_path,
            str(latest_run["run_id"]),
        )
        if node_summary:
            ledger_summary["node_review"] = node_summary
        relation_summary = local_relation_review_summary(
            vault_path,
            str(latest_run["run_id"]),
        )
        if relation_summary:
            ledger_summary["relation_review"] = relation_summary
        endpoint_shadow = local_endpoint_shadow_summary(
            vault_path,
            str(latest_run["run_id"]),
        )
        if endpoint_shadow:
            ledger_summary["endpoint_shadow"] = endpoint_shadow
        pending_preview = local_pending_commit_preview(
            vault_path,
            str(latest_run["run_id"]),
        )
        if pending_preview:
            ledger_summary["pending_commit_preview"] = pending_preview
        pending_domain = local_pending_domain_profile(
            vault_path,
            str(latest_run["run_id"]),
            domain_profile,
        )
        if pending_domain:
            ledger_summary["pending_domain_profile"] = pending_domain
        report["ledger_detail"] = ledger_summary
    return report


def compact_ingest_queue(queue: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(queue, dict):
        return queue
    return {
        "status": queue.get("status"),
        "summary": queue.get("summary"),
        "vault": queue.get("vault"),
        "items": [
            _compact_ingest_item(item)
            for item in queue.get("items") or []
            if isinstance(item, dict)
        ],
    }


def _compact_ingest_item(item: dict[str, Any]) -> dict[str, Any]:
    compact = {
        "id": item.get("id"),
        "name": item.get("name"),
        "path": item.get("path"),
        "status": item.get("status"),
        "stage": item.get("stage"),
        "blocks_done": item.get("blocks_done"),
        "blocks_total": item.get("blocks_total"),
        "nodes": item.get("nodes"),
        "edges": item.get("edges"),
        "claims": item.get("claims"),
        "queued": item.get("queued"),
        "committed": item.get("committed"),
        "event_count": item.get("event_count"),
        "error": item.get("error"),
    }
    last_event = item.get("last_event")
    if isinstance(last_event, dict):
        compact["last_event"] = {
            "kind": last_event.get("kind"),
            "summary": last_event.get("summary"),
            "ts": last_event.get("ts"),
        }
    else:
        compact["last_event"] = None
    return compact


def _node_type_count(report: dict[str, Any], node_type: str) -> int:
    stats = report.get("stats") or {}
    for row in stats.get("node_types") or []:
        if isinstance(row, dict) and row.get("type") == node_type:
            return int(row.get("count") or 0)
    return 0


def _casefold_set(values: list[str]) -> set[str]:
    return {value.casefold() for value in values}


def _domain_profile_summary(
    profile_name: str,
    node_records: list[dict[str, str]],
) -> dict[str, Any]:
    profile = DOMAIN_PROFILES.get(profile_name)
    if profile is None:
        raise ValueError(f"unknown domain profile: {profile_name}")

    title_to_records: dict[str, list[dict[str, str]]] = {}
    for record in node_records:
        node_type = str(record.get("type") or "")
        if node_type in PROVENANCE_NODE_TYPES:
            continue
        title = str(record.get("name") or "")
        if not title:
            continue
        title_to_records.setdefault(title.casefold(), []).append(record)

    groups: dict[str, Any] = {}
    total_expected = 0
    total_present = 0
    for group_name, entries in (profile.get("groups") or {}).items():
        present: list[dict[str, Any]] = []
        missing: list[str] = []
        for entry in entries:
            label = str(entry.get("label") or "")
            aliases = sorted(
                {label, *(str(alias) for alias in entry.get("aliases") or [])},
                key=str.casefold,
            )
            matched: list[dict[str, str]] = []
            for alias in aliases:
                matched.extend(title_to_records.get(alias.casefold(), []))
            total_expected += 1
            if matched:
                total_present += 1
                present.append(
                    {
                        "label": label,
                        "matched_titles": sorted(
                            {str(row.get("name") or "") for row in matched},
                            key=str.casefold,
                        ),
                        "matched_types": sorted(
                            {str(row.get("type") or "") for row in matched},
                            key=str.casefold,
                        ),
                    }
                )
            else:
                missing.append(label)
        total = len(entries)
        groups[group_name] = {
            "present": len(present),
            "total": total,
            "fraction": (len(present) / total) if total else None,
            "matches": present,
            "missing": missing,
        }

    forbidden_present: list[dict[str, str]] = []
    for title in profile.get("forbidden_titles") or []:
        for record in title_to_records.get(str(title).casefold(), []):
            forbidden_present.append(
                {
                    "title": str(record.get("name") or title),
                    "type": str(record.get("type") or ""),
                }
            )

    return {
        "name": profile_name,
        "coverage": {
            "present": total_present,
            "total": total_expected,
            "fraction": (total_present / total_expected) if total_expected else None,
        },
        "groups": groups,
        "forbidden_present": sorted(
            forbidden_present,
            key=lambda row: (row["title"].casefold(), row["type"]),
        ),
    }


def _structural_title_noise(values: list[str]) -> list[str]:
    return sorted(
        {value for value in values if STRUCTURAL_TITLE_NOISE_RE.match(value.strip())},
        key=str.casefold,
    )


def _semantic_degree_by_node(edges: list[Any]) -> dict[str, int]:
    degree: dict[str, int] = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        edge_type = str(edge.get("type") or "")
        if edge_type in PLUMBING_EDGE_TYPES:
            continue
        for key in (edge.get("src"), edge.get("dst")):
            if key:
                node_key = str(key)
                degree[node_key] = degree.get(node_key, 0) + 1
    return degree


def graph_profile(nodes: list[Any], edges: list[Any], *, sample_limit: int = 12) -> dict[str, Any]:
    node_by_id = {
        str(node.get("id")): node
        for node in nodes
        if isinstance(node, dict) and node.get("id")
    }
    node_types: dict[str, int] = {}
    edge_types: dict[str, int] = {}
    semantic_edge_types: dict[str, int] = {}
    titles_by_type: dict[str, list[str]] = {}
    semantic_degree = _semantic_degree_by_node(edges)

    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_type = str(node.get("type") or "unknown")
        node_types[node_type] = node_types.get(node_type, 0) + 1
        name = str(node.get("name") or "")
        if name:
            titles_by_type.setdefault(node_type, []).append(name)
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        edge_type = str(edge.get("type") or "unknown")
        edge_types[edge_type] = edge_types.get(edge_type, 0) + 1
        if edge_type not in PLUMBING_EDGE_TYPES:
            semantic_edge_types[edge_type] = semantic_edge_types.get(edge_type, 0) + 1

    top_degree_nodes: list[dict[str, Any]] = []
    for node_id, degree in sorted(
        semantic_degree.items(),
        key=lambda item: (-item[1], item[0]),
    )[:sample_limit]:
        node = node_by_id.get(node_id) or {}
        top_degree_nodes.append(
            {
                "id": node_id,
                "name": str(node.get("name") or node_id),
                "type": str(node.get("type") or "unknown"),
                "semantic_degree": degree,
            }
        )

    return {
        "node_types": dict(sorted(node_types.items())),
        "edge_types": dict(sorted(edge_types.items())),
        "semantic_edge_types": dict(sorted(semantic_edge_types.items())),
        "knowledge_node_count": sum(
            count
            for node_type, count in node_types.items()
            if node_type not in PROVENANCE_NODE_TYPES
        ),
        "semantic_edge_count": sum(semantic_edge_types.values()),
        "top_degree_nodes": top_degree_nodes,
        "sample_titles_by_type": {
            node_type: sorted(titles, key=str.casefold)[:sample_limit]
            for node_type, titles in sorted(titles_by_type.items())
        },
    }


def _isolated_knowledge_nodes(nodes: list[Any], edges: list[Any]) -> list[dict[str, str]]:
    degree = _semantic_degree_by_node(edges)
    isolated: list[dict[str, str]] = []
    for node in nodes:
        if not isinstance(node, dict) or not node.get("name"):
            continue
        node_type = str(node.get("type") or "")
        if node_type in ISOLATION_EXCLUDED_NODE_TYPES:
            continue
        node_key = str(node.get("id") or node.get("name"))
        if degree.get(node_key, 0) > 0:
            continue
        record = {"name": str(node["name"]), "type": node_type}
        if node.get("id"):
            record["id"] = str(node["id"])
        isolated.append(record)
    return sorted(isolated, key=lambda row: (row["type"], row["name"].casefold()))


def check_report(args: argparse.Namespace, report: dict[str, Any]) -> list[CheckResult]:
    node_names = [
        str(name)
        for name in report.get("knowledge_node_names") or report.get("node_names") or []
    ]
    node_names_cf = _casefold_set(node_names)
    stats = report.get("stats") or {}
    total_nodes = int(stats.get("total_nodes") or 0)
    queue_summary = ((report.get("queue") or {}).get("summary") or {})

    checks: list[CheckResult] = []
    overview_complete = (report.get("graph") or {}).get("truncated") is False
    semantic_evidence = (report.get("semantic_quality") or {}).get("evidence") or {}
    semantic_population = (report.get("semantic_quality") or {}).get("population") or {}
    semantic_relation = (
        (((report.get("semantic_quality") or {}).get("layers") or {}).get("relation") or {})
    )
    audited_total_edges = semantic_population.get("edges")
    audited_semantic_edges = semantic_relation.get("materialized_topology_edges")
    audited_edge_types_metric = (
        semantic_relation.get("materialized_topology_edge_types") or {}
    )
    audited_edge_type_counts_value = audited_edge_types_metric.get("counts")
    audited_edge_type_counts_are_valid = isinstance(audited_edge_type_counts_value, dict)
    audited_edge_type_counts = (
        audited_edge_type_counts_value if audited_edge_type_counts_are_valid else {}
    )
    has_verified_topology = (
        semantic_evidence.get("complete") is True
        and semantic_evidence.get("technical_integrity_verified") is True
        and semantic_evidence.get("topology_evidence_status") == "measured"
        and isinstance(audited_total_edges, int)
        and isinstance(audited_semantic_edges, int)
        and audited_edge_types_metric.get("status") == "measured"
        and audited_edge_type_counts_are_valid
    )
    audited_edge_types = {str(edge_type) for edge_type in audited_edge_type_counts}
    semantic_isolated = semantic_relation.get("isolated_entities") or {}
    has_complete_isolated_count = (
        semantic_isolated.get("count") is not None
        and semantic_evidence.get("complete") is True
        and semantic_evidence.get("technical_integrity_verified") is True
    )
    if not has_complete_isolated_count:
        semantic_isolated = {}
    overview_checks_requested = any(
        (
            args.min_knowledge_nodes is not None,
            bool(args.expect_title),
            bool(args.forbid_title),
            args.forbid_structural_title_noise,
            bool(report.get("domain_profile")),
            args.min_domain_profile_coverage is not None,
        )
    )
    if not overview_complete and overview_checks_requested:
        checks.append(
            CheckResult(
                False,
                "overview-derived checks not measured: /api/v1/graph completeness was not attested",
            )
        )
    topology_checks_requested = any(
        (
            args.min_edges is not None,
            args.min_semantic_edges is not None,
            bool(args.expect_edge_type),
            bool(args.forbid_edge_type),
        )
    )
    if topology_checks_requested and not has_verified_topology:
        checks.append(
            CheckResult(
                False,
                "edge-derived checks not measured: complete, technically verified "
                "semantic topology evidence is required",
            )
        )
    if args.min_nodes is not None:
        checks.append(CheckResult(
            total_nodes >= args.min_nodes,
            f"total_nodes {total_nodes} >= {args.min_nodes}",
        ))
    if has_verified_topology and args.min_edges is not None:
        checks.append(CheckResult(
            audited_total_edges >= args.min_edges,
            f"audited edges {audited_total_edges} >= {args.min_edges}",
        ))
    if args.min_claims is not None:
        claims = _node_type_count(report, "Claim")
        checks.append(CheckResult(
            claims >= args.min_claims,
            f"Claim nodes {claims} >= {args.min_claims}",
        ))
    graph_summary = report.get("graph_profile") or {}
    if overview_complete and args.min_knowledge_nodes is not None:
        knowledge_nodes = int(graph_summary.get("knowledge_node_count") or 0)
        checks.append(CheckResult(
            knowledge_nodes >= args.min_knowledge_nodes,
            f"knowledge nodes {knowledge_nodes} >= {args.min_knowledge_nodes}",
        ))
    if has_verified_topology and args.min_semantic_edges is not None:
        checks.append(CheckResult(
            audited_semantic_edges >= args.min_semantic_edges,
            f"audited semantic edges {audited_semantic_edges} >= {args.min_semantic_edges}",
        ))
    ledger_counts = ((report.get("ledger_detail") or {}).get("counts") or {})
    if args.min_commit_plans is not None:
        commit_plans = int(ledger_counts.get("commit_plans") or 0)
        checks.append(CheckResult(
            commit_plans >= args.min_commit_plans,
            f"commit plans {commit_plans} >= {args.min_commit_plans}",
        ))
    if args.min_commit_records is not None:
        commit_records = int(ledger_counts.get("commit_records") or 0)
        checks.append(CheckResult(
            commit_records >= args.min_commit_records,
            f"commit records {commit_records} >= {args.min_commit_records}",
        ))
    pending_preview = ((report.get("ledger_detail") or {}).get("pending_commit_preview") or {})
    pending_nodes = int(((pending_preview.get("nodes") or {}).get("accepted_for_write")) or 0)
    pending_relations = int(
        ((pending_preview.get("relations") or {}).get("accepted_for_write")) or 0
    )
    if args.min_pending_nodes is not None:
        checks.append(CheckResult(
            pending_nodes >= args.min_pending_nodes,
            f"pending accepted nodes {pending_nodes} >= {args.min_pending_nodes}",
        ))
    if args.min_pending_relations is not None:
        checks.append(CheckResult(
            pending_relations >= args.min_pending_relations,
            f"pending accepted relations {pending_relations} >= {args.min_pending_relations}",
        ))
    ingest_extraction = report.get("ingest_extraction") or {}
    extraction_aggregate = (
        (ingest_extraction.get("aggregate") or {}) if isinstance(ingest_extraction, dict) else {}
    )
    extraction_counts = extraction_aggregate.get("counts") or {}
    if args.min_extracted_node_mentions is not None:
        extracted_nodes = int(extraction_counts.get("node_mentions") or 0)
        checks.append(CheckResult(
            extracted_nodes >= args.min_extracted_node_mentions,
            f"extracted node mentions {extracted_nodes} >= {args.min_extracted_node_mentions}",
        ))
    if args.min_extracted_relation_candidates is not None:
        extracted_relations = int(extraction_counts.get("relation_candidates") or 0)
        checks.append(CheckResult(
            extracted_relations >= args.min_extracted_relation_candidates,
            (
                "extracted relation candidates "
                f"{extracted_relations} >= {args.min_extracted_relation_candidates}"
            ),
        ))
    if args.min_extracted_claim_candidates is not None:
        extracted_claims = int(extraction_counts.get("claim_candidates") or 0)
        checks.append(CheckResult(
            extracted_claims >= args.min_extracted_claim_candidates,
            f"extracted claim candidates {extracted_claims} >= {args.min_extracted_claim_candidates}",
        ))
    if args.max_queue_items is not None:
        queued_total = int(queue_summary.get("queued") or 0)
        processing = int(queue_summary.get("processing") or 0)
        active_items = queued_total + processing
        checks.append(
            CheckResult(
                active_items <= args.max_queue_items,
                f"active queue items {active_items} <= {args.max_queue_items}",
            )
        )
    if overview_complete:
        for title in args.expect_title:
            checks.append(
                CheckResult(title.casefold() in node_names_cf, f"expected title present: {title}")
            )
        for title in args.forbid_title:
            checks.append(
                CheckResult(title.casefold() not in node_names_cf, f"forbidden title absent: {title}")
            )
    if has_verified_topology:
        for edge_type in args.expect_edge_type:
            checks.append(
                CheckResult(
                    edge_type in audited_edge_types,
                    f"expected audited edge type present: {edge_type}",
                )
            )
        for edge_type in args.forbid_edge_type:
            checks.append(
                CheckResult(
                    edge_type not in audited_edge_types,
                    f"forbidden audited edge type absent: {edge_type}",
                )
            )
    if overview_complete and args.forbid_structural_title_noise:
        structural_noise = _structural_title_noise(node_names)
        detail = "structural title noise absent"
        if structural_noise:
            detail = f"{detail}: {', '.join(structural_noise)}"
        checks.append(CheckResult(not structural_noise, detail))
    if args.max_isolated_knowledge_nodes is not None:
        if not has_complete_isolated_count:
            checks.append(
                CheckResult(
                    False,
                    "isolated knowledge nodes not measured: complete, "
                    "technically verified semantic evidence is required",
                )
            )
            isolated_count = None
        else:
            isolated_count = int(semantic_isolated["count"])
        isolated = [row for row in semantic_isolated.get("samples") or [] if isinstance(row, dict)]
        if isolated_count is None:
            pass
        else:
            detail = (
                f"isolated knowledge nodes {isolated_count} "
                f"<= {args.max_isolated_knowledge_nodes}"
            )
            if isolated_count > args.max_isolated_knowledge_nodes:
                sample = ", ".join(
                    f"{row.get('title') or row.get('name')} ({row.get('type')})"
                    for row in isolated[:10]
                )
                if sample:
                    detail = f"{detail}: {sample}"
            checks.append(
                CheckResult(
                    isolated_count <= args.max_isolated_knowledge_nodes,
                    detail,
                )
            )
    if getattr(args, "require_semantic_quality", False):
        semantic_report = report.get("semantic_quality") or {}
        semantic_verdict = semantic_report.get("verdict") or {}
        status = str(semantic_verdict.get("status") or "missing")
        checks.append(
            CheckResult(
                status == "passed",
                f"authoritative semantic quality verdict is passed (actual: {status})",
            )
        )
    domain_profile = report.get("domain_profile") or {}
    if overview_complete and domain_profile:
        forbidden_present = [
            row for row in domain_profile.get("forbidden_present") or [] if isinstance(row, dict)
        ]
        detail = "domain forbidden titles absent"
        if forbidden_present:
            sample = ", ".join(
                f"{row.get('title')} ({row.get('type')})" for row in forbidden_present[:10]
            )
            detail = f"{detail}: {sample}"
        checks.append(CheckResult(not forbidden_present, detail))
    pending_domain_profile = (
        ((report.get("ledger_detail") or {}).get("pending_domain_profile") or {})
        if isinstance(report.get("ledger_detail"), dict)
        else {}
    )
    if pending_domain_profile:
        forbidden_present = [
            row for row in pending_domain_profile.get("forbidden_present") or [] if isinstance(row, dict)
        ]
        detail = "pending domain forbidden titles absent"
        if forbidden_present:
            sample = ", ".join(
                f"{row.get('title')} ({row.get('type')})" for row in forbidden_present[:10]
            )
            detail = f"{detail}: {sample}"
        checks.append(CheckResult(not forbidden_present, detail))
    if overview_complete and args.min_domain_profile_coverage is not None:
        coverage = (domain_profile.get("coverage") or {}) if isinstance(domain_profile, dict) else {}
        fraction = coverage.get("fraction")
        present = int(coverage.get("present") or 0)
        total = int(coverage.get("total") or 0)
        ok = fraction is not None and float(fraction) >= args.min_domain_profile_coverage
        checks.append(
            CheckResult(
                ok,
                (
                    f"domain profile coverage {present}/{total} "
                    f">= {args.min_domain_profile_coverage:.2f}"
                ),
            )
        )
    if args.min_extracted_domain_profile_coverage is not None:
        extracted_profile = (
            extraction_aggregate.get("domain_profile") or {}
            if isinstance(extraction_aggregate, dict)
            else {}
        )
        coverage = (
            extracted_profile.get("coverage") or {}
            if isinstance(extracted_profile, dict)
            else {}
        )
        fraction = coverage.get("fraction")
        present = int(coverage.get("present") or 0)
        total = int(coverage.get("total") or 0)
        ok = (
            fraction is not None
            and float(fraction) >= args.min_extracted_domain_profile_coverage
        )
        checks.append(
            CheckResult(
                ok,
                (
                    f"extracted domain profile coverage {present}/{total} "
                    f">= {args.min_extracted_domain_profile_coverage:.2f}"
                ),
            )
        )
    if args.min_pending_domain_profile_coverage is not None:
        coverage = (
            pending_domain_profile.get("coverage") or {}
            if isinstance(pending_domain_profile, dict)
            else {}
        )
        fraction = coverage.get("fraction")
        present = int(coverage.get("present") or 0)
        total = int(coverage.get("total") or 0)
        ok = (
            fraction is not None
            and float(fraction) >= args.min_pending_domain_profile_coverage
        )
        checks.append(
            CheckResult(
                ok,
                (
                    f"pending domain profile coverage {present}/{total} "
                    f">= {args.min_pending_domain_profile_coverage:.2f}"
                ),
            )
        )
    return checks


def run_cognitive_scorer(command: str, report: dict[str, Any]) -> dict[str, Any]:
    argv = shlex.split(command)
    if not argv:
        raise ValueError("cognitive scorer command is empty")
    proc = subprocess.run(
        argv,
        input=json.dumps(report, indent=2, sort_keys=True),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return {
        "command": command,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--vault", required=True, help="Vault name or path known to the server.")
    parser.add_argument("--source", type=Path, help="Folder to ingest before checking.")
    parser.add_argument("--recursive", action="store_true", help="Recurse when ingesting --source.")
    parser.add_argument(
        "--max-files",
        type=int,
        help="Ingest only the first N sorted source files via /ingest-batch.",
    )
    parser.add_argument(
        "--max-chars-per-file",
        type=int,
        help="Ingest only the first N characters of each selected source file.",
    )
    parser.add_argument("--reset", action="store_true", help="Reset the selected vault before ingest.")
    parser.add_argument("--timeout-s", type=float, default=3600.0)
    parser.add_argument(
        "--request-timeout-s",
        type=float,
        default=300.0,
        help="Timeout for each HTTP audit request; independent from the ingest wait timeout.",
    )
    parser.add_argument("--poll-s", type=float, default=2.0)
    parser.add_argument("--min-nodes", type=int)
    parser.add_argument(
        "--min-edges",
        type=int,
        help="Minimum non-infrastructure edges in the audited semantic snapshot.",
    )
    parser.add_argument("--min-claims", type=int)
    parser.add_argument(
        "--min-knowledge-nodes",
        type=int,
        help="Minimum non-provenance graph nodes returned by /api/v1/graph.",
    )
    parser.add_argument(
        "--min-semantic-edges",
        type=int,
        help="Minimum claim-backed topology edges in the audited semantic snapshot.",
    )
    parser.add_argument(
        "--min-commit-plans",
        type=int,
        help="Minimum commit-plan records in the latest ledger run.",
    )
    parser.add_argument(
        "--min-commit-records",
        type=int,
        help="Minimum commit result records in the latest ledger run.",
    )
    parser.add_argument(
        "--min-pending-nodes",
        type=int,
        help="Minimum accepted pre-commit node candidates in the latest ledger run.",
    )
    parser.add_argument(
        "--min-pending-relations",
        type=int,
        help="Minimum accepted pre-commit relation candidates in the latest ledger run.",
    )
    parser.add_argument(
        "--min-extracted-node-mentions",
        type=int,
        help="Minimum node mentions in retained active-ingest extraction events.",
    )
    parser.add_argument(
        "--min-extracted-relation-candidates",
        type=int,
        help="Minimum topology relation candidates in retained active-ingest extraction events.",
    )
    parser.add_argument(
        "--min-extracted-claim-candidates",
        type=int,
        help="Minimum literal claim candidates in retained active-ingest extraction events.",
    )
    parser.add_argument("--max-queue-items", type=int, default=0)
    parser.add_argument("--expect-title", action="append", default=[])
    parser.add_argument("--forbid-title", action="append", default=[])
    parser.add_argument("--expect-edge-type", action="append", default=[])
    parser.add_argument("--forbid-edge-type", action="append", default=[])
    parser.add_argument(
        "--domain-profile",
        choices=sorted(DOMAIN_PROFILES),
        help="Add a named corpus/domain coverage summary to the report.",
    )
    parser.add_argument(
        "--min-domain-profile-coverage",
        type=float,
        help=(
            "Fail unless the selected --domain-profile has at least this total "
            "coverage fraction across expected concepts."
        ),
    )
    parser.add_argument(
        "--min-extracted-domain-profile-coverage",
        type=float,
        help=(
            "Fail unless retained extraction events for the selected --domain-profile "
            "have at least this total coverage fraction."
        ),
    )
    parser.add_argument(
        "--min-pending-domain-profile-coverage",
        type=float,
        help=(
            "Fail unless accepted pending node candidates for the selected --domain-profile "
            "have at least this total coverage fraction."
        ),
    )
    parser.add_argument(
        "--forbid-structural-title-noise",
        action="store_true",
        help="Fail when parser-generated titles such as paragraph 1 or code-block 0 exist.",
    )
    parser.add_argument(
        "--max-isolated-knowledge-nodes",
        type=int,
        help=(
            "Fail when more than N primitive entities have neither an active Claim endpoint "
            "nor schema:mentions evidence. Requires a complete, technically verified "
            "semantic-quality scan."
        ),
    )
    parser.add_argument(
        "--require-semantic-quality",
        action="store_true",
        help=(
            "Fail unless the product-owned semantic evaluator has complete, "
            "ADR-0039-verified evidence and every registered hard invariant passes."
        ),
    )
    parser.add_argument(
        "--cognitive-score-cmd",
        help="Optional shell command that receives the JSON report on stdin.",
    )
    parser.add_argument(
        "--include-ledger-detail",
        action="store_true",
        help="Include compact candidate/comparison counts for the latest ledger run.",
    )
    parser.add_argument("--report", type=Path, help="Write the full JSON report to this path.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    ensure_vault(args.endpoint, args.vault)
    if args.reset:
        reset_vault(args.endpoint, args.vault)
    ingest_result = None
    queue = None
    if args.source is not None:
        if args.max_files is not None or args.max_chars_per_file is not None:
            ingest_result = ingest_batch(
                args.endpoint,
                args.vault,
                args.source,
                recursive=args.recursive,
                max_files=args.max_files,
                max_chars_per_file=args.max_chars_per_file,
            )
        else:
            ingest_result = ingest_folder(
                args.endpoint,
                args.vault,
                args.source,
                recursive=args.recursive,
            )
    # A read-only audit of an existing vault needs the same stable boundary as
    # an audit that enqueued its own source.  Otherwise the semantic snapshot
    # can race the active writer and either block or describe a partial graph.
    queue = wait_for_ingest(
        args.endpoint,
        args.vault,
        timeout_s=args.timeout_s,
        poll_s=args.poll_s,
    )
    report = build_report(
        args.endpoint,
        args.vault,
        queue=queue,
        include_ledger_detail=args.include_ledger_detail,
        domain_profile=args.domain_profile,
        request_timeout_s=args.request_timeout_s,
    )
    if ingest_result is not None:
        report["ingest"] = ingest_result
    checks = check_report(args, report)
    report["checks"] = [{"ok": check.ok, "detail": check.detail} for check in checks]
    if args.cognitive_score_cmd:
        report["cognitive_score"] = run_cognitive_scorer(args.cognitive_score_cmd, report)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))
    failed = [check for check in checks if not check.ok]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
