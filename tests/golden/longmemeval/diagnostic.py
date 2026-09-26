#!/usr/bin/env python3
"""Run the frozen LongMemEval procurement diagnostic without duplicate ingestion.

The adapter materializes one isolated Golden dataset per question.  This runner keeps that
boundary, records the exact ingest item ids before waiting, resumes from durable state, and
executes the existing Golden harness only after the selected vault exactly matches its case.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import queue
import re
import shlex
import subprocess
import sys
import threading
import time
from typing import Any
from urllib import error, request

import numpy as np

from okto_neuron.construction_cost import (
    CONSTRUCTION_COST_SCHEMA,
    combine_construction_costs,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
EVAL_RUNNER = REPO_ROOT / "tests" / "golden" / "bin" / "eval-run.sh"
JUDGE_TOOL = REPO_ROOT / "tests" / "golden" / "bin" / "judge.py"
STATE_SCHEMA = "longmemeval-diagnostic-state.v1"
RETRIEVAL_SCHEMA = "longmemeval-marginalia-retrieval.v1"
RAG_SCHEMA = "longmemeval-direct-rag.v1"
BM25_SCHEMA = "longmemeval-flat-bm25.v1"
UPSTREAM_REVISION = "9e0b455f4ef0e2ab8f2e582289761153549043fc"
UPSTREAM_RETRIEVAL_FILE = Path("src/retrieval/run_retrieval.py")
UPSTREAM_EVAL_FILE = Path("src/retrieval/eval_utils.py")
K_VALUES = (1, 3, 5, 10, 30)
TERMINAL_INGEST_STATES = frozenset({"done", "error", "cancelled"})
TERMINAL_CURATION_STATES = frozenset({"done", "error"})
CONSTRUCTION_ARTIFACT_SCHEMA = "longmemeval-construction-cost.v1"


class DiagnosticError(RuntimeError):
    """Raised when diagnostic evidence is incomplete or violates the frozen contract."""


class TransientApiError(DiagnosticError):
    """Raised when a diagnostic status read can be retried within its hard deadline."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DiagnosticError(f"could not read JSON {path}: {exc}") from exc


def _write_json(path: Path, value: object) -> str:
    rendered = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(rendered)
    temporary.replace(path)
    return _sha256_bytes(rendered)


def _require_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise DiagnosticError(f"{name} must be an object")
    return value


def _materialization(root: Path) -> dict[str, Any]:
    manifest_path = root / "reproducibility-manifest.json"
    manifest = _require_mapping(_load_json(manifest_path), "materialization manifest")
    if manifest.get("schema_version") != "longmemeval-materialization.v3":
        raise DiagnosticError("unsupported LongMemEval materialization schema")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        raise DiagnosticError("materialization must contain cases")
    if manifest.get("selected_count") != len(cases):
        raise DiagnosticError("materialization selected_count does not match cases")
    for case in cases:
        row = _require_mapping(case, "materialization case")
        case_id = row.get("case_id")
        files = row.get("files")
        if not isinstance(case_id, str) or not case_id:
            raise DiagnosticError("materialization case_id must be a non-empty string")
        if not isinstance(files, dict) or not files:
            raise DiagnosticError(f"materialization case {case_id} has no file pins")
        case_root = root / "cases" / case_id
        for relative, expected in files.items():
            path = case_root / relative
            if not path.is_file() or _sha256_file(path) != expected:
                raise DiagnosticError(f"materialized case pin mismatch: {case_id}/{relative}")
    return manifest


def _case_rows(root: Path, manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for raw in manifest["cases"]:
        row = dict(_require_mapping(raw, "materialization case"))
        row["path"] = str((root / "cases" / row["case_id"]).resolve())
        rows.append(row)
    return sorted(rows, key=lambda row: row["case_id"])


def _api_json(
    endpoint: str,
    path: str,
    *,
    vault_path: str | None = None,
    method: str = "GET",
    payload: object | None = None,
    timeout_s: float = 30,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if vault_path:
        headers["X-Okto-Neuron-Vault"] = vault_path
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    target = endpoint.rstrip("/") + path
    try:
        with request.urlopen(
            request.Request(target, data=body, headers=headers, method=method), timeout=timeout_s
        ) as response:
            value = json.load(response)
    except error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1000]
        raise DiagnosticError(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
    except (error.URLError, TimeoutError) as exc:
        raise TransientApiError(f"{method} {path} failed: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DiagnosticError(f"{method} {path} failed: {exc}") from exc
    return _require_mapping(value, f"{method} {path} response")


def _existing_vault(endpoint: str, name: str) -> str | None:
    payload = _api_json(endpoint, "/api/v1/vaults")
    for raw in payload.get("vaults", []):
        if isinstance(raw, dict) and raw.get("name") == name and isinstance(raw.get("path"), str):
            return raw["path"]
    return None


def _ensure_vault(endpoint: str, name: str) -> str:
    existing = _existing_vault(endpoint, name)
    if existing is not None:
        return existing
    payload = _api_json(
        endpoint,
        "/api/v1/vaults",
        method="POST",
        payload={"name": name},
        timeout_s=60,
    )
    created = _require_mapping(payload.get("created"), "vault creation response.created")
    path = created.get("path")
    if not isinstance(path, str) or not path:
        raise DiagnosticError("vault creation response did not contain a path")
    return path


def _effective_runtime(endpoint: str, vault_path: str) -> dict[str, Any]:
    config = _api_json(endpoint, "/api/v1/config", vault_path=vault_path)
    llm = _require_mapping(config.get("llm"), "config.llm")
    defaults = _require_mapping(llm.get("defaults"), "config.llm.defaults")
    embedding = _require_mapping(config.get("embedding"), "config.embedding")
    if embedding.get("batch_size") != 32 or embedding.get("max_concurrent_batches") != 1:
        raise DiagnosticError(
            "public diagnostic requires embedding batch_size=32 and max_concurrent_batches=1"
        )
    required = {
        "llm_provider": defaults.get("provider"),
        "llm_model": defaults.get("model"),
        "llm_api_base": defaults.get("api_base"),
        "llm_api_key_env": defaults.get("api_key_env"),
        "embedder_provider": embedding.get("provider"),
        "embedder_model": embedding.get("model"),
        "embedding_dimension": embedding.get("dimension"),
        "embedding_batch_size": embedding.get("batch_size"),
        "embedding_max_concurrent_batches": embedding.get("max_concurrent_batches"),
    }
    for field in (
        "llm_provider",
        "llm_model",
        "llm_api_base",
        "embedder_provider",
        "embedder_model",
    ):
        if not isinstance(required[field], str) or not required[field]:
            raise DiagnosticError(f"effective runtime is missing {field}")
    return required


def _initial_state(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    vault_prefix: str = "adr0040-lme",
) -> dict[str, Any]:
    cases = {}
    for row in _case_rows(root, manifest):
        case_id = row["case_id"]
        cases[case_id] = {
            "question_id": row["question_id"],
            "selection_bucket": row["selection_bucket"],
            "status": "pending",
            "vault_name": f"{vault_prefix}-{case_id}",
            "vault_path": None,
            "ingest_item_ids": [],
            "reconciliation_job_ids": [],
            "construction_artifact": None,
            "identity_artifact": None,
            "eval_run_dir": None,
            "error": None,
            "updated_at": _now(),
        }
    return {
        "schema_version": STATE_SCHEMA,
        "materialization_sha256": _sha256_file(root / "reproducibility-manifest.json"),
        "materialization_path": str(root.resolve()),
        "vault_prefix": vault_prefix,
        "started_at": _now(),
        "updated_at": _now(),
        "runtime": None,
        "cases": cases,
    }


def _load_state(
    path: Path,
    root: Path,
    manifest: Mapping[str, Any],
    *,
    vault_prefix: str | None = None,
) -> dict[str, Any]:
    if not path.exists():
        return _initial_state(
            root,
            manifest,
            vault_prefix=vault_prefix or "adr0040-lme",
        )
    state = _require_mapping(_load_json(path), "diagnostic state")
    if state.get("schema_version") != STATE_SCHEMA:
        raise DiagnosticError("unsupported diagnostic state schema")
    expected = _sha256_file(root / "reproducibility-manifest.json")
    if state.get("materialization_sha256") != expected:
        raise DiagnosticError("diagnostic state belongs to a different materialization")
    state_prefix = str(state.get("vault_prefix") or "adr0040-lme")
    if vault_prefix is not None and state_prefix != vault_prefix:
        raise DiagnosticError(
            f"diagnostic state vault prefix is {state_prefix!r}, not {vault_prefix!r}"
        )
    state["vault_prefix"] = state_prefix
    expected_cases = {row["case_id"] for row in _case_rows(root, manifest)}
    actual_cases = set(_require_mapping(state.get("cases"), "state.cases"))
    if actual_cases != expected_cases:
        raise DiagnosticError("diagnostic state case set does not match materialization")
    for case_id in expected_cases:
        row = _require_mapping(state["cases"][case_id], f"state case {case_id}")
        row.setdefault("reconciliation_job_ids", [])
        row.setdefault("construction_artifact", None)
    return state


def _select_case_rows(
    cases: Sequence[dict[str, Any]],
    case_ids: Sequence[str] | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    selected = list(cases)
    if case_ids:
        requested = set(case_ids)
        available = {case["case_id"] for case in selected}
        missing = sorted(requested - available)
        if missing:
            raise DiagnosticError(f"requested case ids are absent: {missing}")
        selected = [case for case in selected if case["case_id"] in requested]
    if limit is not None:
        selected = selected[:limit]
    return selected


def _save_state(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = _now()
    _write_json(path, state)


def _document_count(endpoint: str, vault_path: str) -> int:
    payload = _api_json(
        endpoint,
        "/api/v1/nodes?type=Document&limit=1&offset=0",
        vault_path=vault_path,
    )
    value = payload.get("total")
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise DiagnosticError("Document census did not return a valid total")
    return value


def _enqueue_case(endpoint: str, vault_path: str, case_root: Path) -> list[str]:
    files = []
    for path in sorted((case_root / "inputs").glob("*.md")):
        files.append(
            {
                "filename": path.name,
                "content": path.read_text(encoding="utf-8"),
            }
        )
    if not files:
        raise DiagnosticError(f"case has no Markdown inputs: {case_root}")
    response = _api_json(
        endpoint,
        "/api/v1/ingest-batch",
        vault_path=vault_path,
        method="POST",
        payload={"files": files},
        timeout_s=120,
    )
    item_ids = response.get("enqueued_item_ids")
    if (
        not isinstance(item_ids, list)
        or len(item_ids) != len(files)
        or len(set(item_ids)) != len(item_ids)
        or any(not isinstance(item_id, str) or not item_id for item_id in item_ids)
    ):
        raise DiagnosticError("ingest-batch did not return every exact item id")
    return item_ids


def _wait_for_ingest(
    endpoint: str,
    vault_path: str,
    item_ids: Sequence[str],
    *,
    timeout_s: float,
    heartbeat_s: float,
) -> None:
    started = time.monotonic()
    next_heartbeat = started
    wanted = set(item_ids)
    transient_poll_failures = 0
    while True:
        try:
            payload = _api_json(endpoint, "/api/v1/ingest-queue", vault_path=vault_path)
        except TransientApiError as exc:
            transient_poll_failures += 1
            now = time.monotonic()
            if now >= next_heartbeat:
                print(
                    "ingest poll transient failure: "
                    f"count={transient_poll_failures} elapsed_s={now - started:.0f} "
                    f"last_error={exc}",
                    flush=True,
                )
                next_heartbeat = now + heartbeat_s
            if now - started >= timeout_s:
                raise DiagnosticError(
                    f"owned ingest exceeded {timeout_s:g}s after "
                    f"{transient_poll_failures} transient poll failures; "
                    "server work was left running for resume"
                ) from exc
            time.sleep(5)
            continue
        items = {
            row.get("id"): row
            for row in payload.get("items", [])
            if isinstance(row, dict) and row.get("id") in wanted
        }
        states = {item_id: items.get(item_id, {}).get("status") for item_id in item_ids}
        terminal = sum(status in TERMINAL_INGEST_STATES for status in states.values())
        done = sum(status == "done" for status in states.values())
        failed = [
            (item_id, items[item_id].get("error"))
            for item_id, status in states.items()
            if status in {"error", "cancelled"} and item_id in items
        ]
        if failed:
            raise DiagnosticError(f"owned ingest item failed: {failed[0][0]}: {failed[0][1]}")
        if len(items) == len(item_ids) and terminal == len(item_ids):
            if done != len(item_ids):
                raise DiagnosticError(f"owned ingest terminal but only {done}/{len(item_ids)} done")
            print(f"ingest complete: {done}/{len(item_ids)} items", flush=True)
            return
        now = time.monotonic()
        if now >= next_heartbeat:
            elapsed = now - started
            print(
                f"ingest progress: registered={len(items)}/{len(item_ids)} "
                f"done={done}/{len(item_ids)} elapsed_s={elapsed:.0f} "
                f"transient_poll_failures={transient_poll_failures}",
                flush=True,
            )
            next_heartbeat = now + heartbeat_s
        if now - started >= timeout_s:
            raise DiagnosticError(
                f"owned ingest exceeded {timeout_s:g}s; server work was left running for resume"
            )
        time.sleep(5)


def _owned_ingest_rows(
    endpoint: str,
    vault_path: str,
    item_ids: Sequence[str],
) -> list[dict[str, Any]]:
    payload = _api_json(endpoint, "/api/v1/ingest-queue", vault_path=vault_path)
    wanted = set(item_ids)
    by_id = {
        row.get("id"): row
        for row in payload.get("items", [])
        if isinstance(row, dict) and row.get("id") in wanted
    }
    missing = [item_id for item_id in item_ids if item_id not in by_id]
    if missing:
        raise DiagnosticError(f"owned ingest items disappeared from the queue: {missing[:3]}")
    return [by_id[item_id] for item_id in item_ids]


def _reconciliation_job_ids(
    endpoint: str,
    vault_path: str,
    item_ids: Sequence[str],
) -> list[str]:
    job_ids: set[str] = set()
    for item in _owned_ingest_rows(endpoint, vault_path, item_ids):
        item_id = str(item.get("id") or "")
        outcome = _require_mapping(item.get("outcome"), f"ingest item {item_id}.outcome")
        reconciliation = _require_mapping(
            outcome.get("cross_document_reconciliation"),
            f"ingest item {item_id}.cross_document_reconciliation",
        )
        state = reconciliation.get("state")
        if state == "skipped":
            continue
        if state == "failed":
            raise DiagnosticError(
                f"cross-document reconciliation failed for {item_id}: "
                f"{reconciliation.get('error') or reconciliation.get('reason')}"
            )
        if state not in {"scheduled", "complete"}:
            raise DiagnosticError(
                f"cross-document reconciliation has invalid state for {item_id}: {state!r}"
            )
        job_id = reconciliation.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise DiagnosticError(f"cross-document reconciliation has no job id for {item_id}")
        job_ids.add(job_id)
    return sorted(job_ids)


def _curation_job_rows(
    endpoint: str,
    vault_path: str,
    job_ids: Sequence[str],
) -> list[dict[str, Any]]:
    if not job_ids:
        return []
    payload = _api_json(
        endpoint,
        "/api/v1/curation/jobs?kind=reconcile-propose",
        vault_path=vault_path,
    )
    wanted = set(job_ids)
    by_id = {
        row.get("id"): row
        for row in payload.get("jobs", [])
        if isinstance(row, dict) and row.get("id") in wanted
    }
    missing = [job_id for job_id in job_ids if job_id not in by_id]
    if missing:
        raise DiagnosticError(f"owned reconciliation jobs disappeared: {missing[:3]}")
    return [by_id[job_id] for job_id in job_ids]


def _wait_for_reconciliation(
    endpoint: str,
    vault_path: str,
    job_ids: Sequence[str],
    *,
    timeout_s: float,
    heartbeat_s: float,
) -> None:
    if not job_ids:
        print("reconciliation complete: no job required", flush=True)
        return
    started = time.monotonic()
    next_heartbeat = started
    transient_poll_failures = 0
    while True:
        try:
            rows = _curation_job_rows(endpoint, vault_path, job_ids)
        except TransientApiError as exc:
            transient_poll_failures += 1
            now = time.monotonic()
            if now >= next_heartbeat:
                print(
                    "reconciliation poll transient failure: "
                    f"count={transient_poll_failures} elapsed_s={now - started:.0f} "
                    f"last_error={exc}",
                    flush=True,
                )
                next_heartbeat = now + heartbeat_s
            if now - started >= timeout_s:
                raise DiagnosticError(
                    f"owned reconciliation exceeded {timeout_s:g}s after "
                    f"{transient_poll_failures} transient poll failures; "
                    "job was left running for resume"
                ) from exc
            time.sleep(5)
            continue
        errors = [row for row in rows if row.get("status") == "error"]
        if errors:
            first = errors[0]
            raise DiagnosticError(
                f"owned reconciliation job failed: {first.get('id')}: {first.get('error')}"
            )
        done = sum(row.get("status") == "done" for row in rows)
        if len(rows) == len(job_ids) and done == len(job_ids):
            for row in rows:
                result = _require_mapping(row.get("result"), f"job {row.get('id')}.result")
                outcome = _require_mapping(
                    result.get("outcome"), f"job {row.get('id')}.result.outcome"
                )
                if outcome.get("state") != "complete":
                    raise DiagnosticError(
                        f"owned reconciliation job has non-complete outcome: {row.get('id')}"
                    )
            print(f"reconciliation complete: {done}/{len(job_ids)} jobs", flush=True)
            return
        now = time.monotonic()
        if now >= next_heartbeat:
            progress = [
                {
                    "id": row.get("id"),
                    "status": row.get("status"),
                    "progress": row.get("progress"),
                }
                for row in rows
            ]
            print(
                f"reconciliation progress: done={done}/{len(job_ids)} "
                f"elapsed_s={now - started:.0f} "
                f"transient_poll_failures={transient_poll_failures} "
                f"jobs={json.dumps(progress)}",
                flush=True,
            )
            next_heartbeat = now + heartbeat_s
        if now - started >= timeout_s:
            raise DiagnosticError(
                f"owned reconciliation exceeded {timeout_s:g}s; job was left running for resume"
            )
        time.sleep(5)


def _validated_cost(value: object, name: str) -> dict[str, Any]:
    row = _require_mapping(value, name)
    try:
        normalized = combine_construction_costs([row])
    except ValueError as exc:
        raise DiagnosticError(f"{name} is invalid: {exc}") from exc
    if normalized["status"] != "measured":
        raise DiagnosticError(f"{name} is partial; provider usage is required for this diagnostic")
    return {"schema_version": CONSTRUCTION_COST_SCHEMA, **normalized}


def _capture_construction_cost(
    endpoint: str,
    vault_path: str,
    item_ids: Sequence[str],
    job_ids: Sequence[str],
    out: Path,
) -> None:
    item_rows = []
    costs: list[Mapping[str, Any]] = []
    for item in _owned_ingest_rows(endpoint, vault_path, item_ids):
        item_id = str(item.get("id") or "")
        outcome = _require_mapping(item.get("outcome"), f"ingest item {item_id}.outcome")
        cost = _validated_cost(
            outcome.get("construction_cost"),
            f"ingest item {item_id}.construction_cost",
        )
        costs.append(cost)
        item_rows.append({"item_id": item_id, "construction_cost": cost})

    job_rows = []
    for job in _curation_job_rows(endpoint, vault_path, job_ids):
        job_id = str(job.get("id") or "")
        if job.get("status") != "done":
            raise DiagnosticError(f"reconciliation job is not done: {job_id}")
        result = _require_mapping(job.get("result"), f"job {job_id}.result")
        cost = _validated_cost(
            result.get("construction_cost"),
            f"job {job_id}.construction_cost",
        )
        costs.append(cost)
        job_rows.append({"job_id": job_id, "construction_cost": cost})

    try:
        totals = combine_construction_costs(costs)
    except ValueError as exc:  # pragma: no cover - rows were validated above
        raise DiagnosticError(f"could not combine construction costs: {exc}") from exc
    if totals["status"] != "measured":  # pragma: no cover - rows fail closed above
        raise DiagnosticError("combined construction cost is partial")
    _write_json(
        out,
        {
            "schema_version": CONSTRUCTION_ARTIFACT_SCHEMA,
            "status": "measured",
            "measured_at": _now(),
            "vault_path": vault_path,
            "owned_ingest_item_ids": list(item_ids),
            "owned_reconciliation_job_ids": list(job_ids),
            "ingest_items": item_rows,
            "reconciliation_jobs": job_rows,
            "totals": totals,
        },
    )


def _run_process(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    log_path: Path,
    timeout_s: float,
    heartbeat_s: float,
) -> list[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    process = subprocess.Popen(
        list(command),
        cwd=REPO_ROOT,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=os.name == "posix",
    )
    print(f"process started: pid={process.pid} timeout_s={timeout_s:g}", flush=True)
    output: list[str] = []
    next_heartbeat = started + heartbeat_s
    assert process.stdout is not None

    messages: queue.SimpleQueue[str | None] = queue.SimpleQueue()

    def _read_output() -> None:
        try:
            for line in process.stdout:
                messages.put(line)
        finally:
            messages.put(None)

    reader = threading.Thread(target=_read_output, daemon=True)
    reader.start()
    reader_done = False
    with log_path.open("a", encoding="utf-8") as log:
        while True:
            while True:
                try:
                    line = messages.get_nowait()
                except queue.Empty:
                    break
                if line is None:
                    reader_done = True
                    continue
                output.append(line.rstrip("\n"))
                log.write(line)
                log.flush()
                print(line, end="", flush=True)
            return_code = process.poll()
            if return_code is not None and reader_done:
                if return_code != 0:
                    raise DiagnosticError(f"process exited with status {return_code}: {command[0]}")
                return output
            now = time.monotonic()
            if now - started >= timeout_s:
                if os.name == "posix":
                    os.killpg(process.pid, 15)
                else:  # pragma: no cover - Windows runner path
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    if os.name == "posix":
                        os.killpg(process.pid, 9)
                    else:  # pragma: no cover - Windows runner path
                        process.kill()
                    process.wait()
                raise DiagnosticError(f"process exceeded {timeout_s:g}s: {command[0]}")
            if now >= next_heartbeat:
                print(
                    f"process running: pid={process.pid} elapsed_s={now - started:.0f}",
                    flush=True,
                )
                next_heartbeat = now + heartbeat_s
            time.sleep(0.1)


def _identity_check(
    endpoint: str,
    vault_path: str,
    case_root: Path,
    out: Path,
    log_path: Path,
    *,
    heartbeat_s: float,
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["OKTO_NEURON_VAULT_PATH"] = vault_path
    _run_process(
        [
            "uv",
            "run",
            "--group",
            "litellm",
            "python3",
            str(JUDGE_TOOL),
            "assert-endpoint-dataset",
            str(case_root),
            "--endpoint",
            endpoint,
            "--out",
            str(out),
        ],
        env=env,
        log_path=log_path,
        timeout_s=180,
        heartbeat_s=heartbeat_s,
    )


def _load_env_value(path: Path, name: str) -> str | None:
    if not path.is_file():
        return None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != name:
            continue
        parsed = shlex.split(value.strip(), comments=False, posix=True)
        return parsed[0] if len(parsed) == 1 else value.strip()
    return None


def _judge_environment(runtime: Mapping[str, Any], env_file: Path) -> dict[str, str]:
    env = os.environ.copy()
    api_key_env = runtime.get("llm_api_key_env")
    if isinstance(api_key_env, str) and api_key_env:
        key = env.get(api_key_env) or _load_env_value(env_file, api_key_env)
        if key:
            env["OPENAI_API_KEY"] = key
    base = str(runtime["llm_api_base"]).rstrip("/")
    env["OKTO_NEURON_LLM_BASE_URL"] = base if base.endswith("/v1") else base + "/v1"
    env["OKTO_NEURON_JUDGE_MODEL"] = str(runtime["llm_model"])
    env["OKTO_NEURON_REALMODEL_MODEL"] = str(runtime["llm_model"])
    return env


def _run_case_eval(
    endpoint: str,
    vault_path: str,
    case_root: Path,
    runtime: Mapping[str, Any],
    *,
    env_file: Path,
    log_path: Path,
    timeout_s: float,
    heartbeat_s: float,
    no_judge: bool,
) -> Path:
    command = [
        str(EVAL_RUNNER),
        str(case_root),
        "--id",
        "longmemeval-public-v1",
        "--endpoint",
        endpoint,
        "--vault-path",
        vault_path,
        "--never-ingest",
    ]
    if no_judge:
        command.append("--no-judge")
    output = _run_process(
        command,
        env=_judge_environment(runtime, env_file),
        log_path=log_path,
        timeout_s=timeout_s,
        heartbeat_s=heartbeat_s,
    )
    candidates = [Path(line) for line in output if line.startswith(str(REPO_ROOT))]
    run_dirs = [path for path in candidates if path.is_dir() and (path / "report.json").is_file()]
    if not run_dirs:
        raise DiagnosticError("eval runner completed without a verifiable report directory")
    return run_dirs[-1]


def run_marginalia(args: argparse.Namespace) -> int:
    materialized = args.materialized.resolve()
    manifest = _materialization(materialized)
    cases = _select_case_rows(
        _case_rows(materialized, manifest),
        args.case_id,
        args.limit,
    )
    state_path = args.run_root.resolve() / "state.json"
    state = _load_state(
        state_path,
        materialized,
        manifest,
        vault_prefix=args.vault_prefix,
    )
    _api_json(args.endpoint, "/api/v1/status", timeout_s=10)
    args.run_root.mkdir(parents=True, exist_ok=True)

    for index, case in enumerate(cases, start=1):
        case_id = case["case_id"]
        row = _require_mapping(state["cases"][case_id], f"state case {case_id}")
        if row.get("status") == "done":
            print(f"[{index}/{len(cases)}] SKIP {case_id}: already done", flush=True)
            continue
        print(f"[{index}/{len(cases)}] START {case_id}", flush=True)
        row["status"] = "running"
        row["error"] = None
        row["updated_at"] = _now()
        _save_state(state_path, state)
        case_root = Path(case["path"])
        case_log = args.run_root / "logs" / f"{case_id}.log"
        try:
            vault_path = row.get("vault_path")
            if not isinstance(vault_path, str) or not vault_path:
                vault_path = _ensure_vault(args.endpoint, row["vault_name"])
                row["vault_path"] = vault_path
                _save_state(state_path, state)
            runtime = _effective_runtime(args.endpoint, vault_path)
            if state.get("runtime") is None:
                state["runtime"] = runtime
            elif state["runtime"] != runtime:
                raise DiagnosticError(
                    "effective provider/model runtime changed during the diagnostic"
                )

            item_ids = row.get("ingest_item_ids")
            if not isinstance(item_ids, list):
                raise DiagnosticError("case ingest_item_ids is malformed")
            identity_path = args.run_root / "identity" / f"{case_id}.json"
            construction_path = args.run_root / "construction" / f"{case_id}.json"
            if not item_ids:
                documents = _document_count(args.endpoint, vault_path)
                if documents != 0:
                    raise DiagnosticError(
                        "vault has Documents but state has no owned ingest ids; "
                        "construction cost cannot be attributed safely"
                    )
                item_ids = _enqueue_case(args.endpoint, vault_path, case_root)
                row["ingest_item_ids"] = item_ids
                _save_state(state_path, state)
                print(f"enqueued {len(item_ids)} exact ingest items", flush=True)

            _wait_for_ingest(
                args.endpoint,
                vault_path,
                item_ids,
                timeout_s=args.ingest_timeout_s,
                heartbeat_s=args.heartbeat_s,
            )
            discovered_job_ids = _reconciliation_job_ids(
                args.endpoint,
                vault_path,
                item_ids,
            )
            recorded_job_ids = row.get("reconciliation_job_ids")
            if not isinstance(recorded_job_ids, list):
                raise DiagnosticError("case reconciliation_job_ids is malformed")
            if recorded_job_ids and set(recorded_job_ids) != set(discovered_job_ids):
                raise DiagnosticError("owned reconciliation job ids changed during resume")
            row["reconciliation_job_ids"] = discovered_job_ids
            _save_state(state_path, state)
            _wait_for_reconciliation(
                args.endpoint,
                vault_path,
                discovered_job_ids,
                timeout_s=args.reconciliation_timeout_s,
                heartbeat_s=args.heartbeat_s,
            )
            if not construction_path.is_file():
                _capture_construction_cost(
                    args.endpoint,
                    vault_path,
                    item_ids,
                    discovered_job_ids,
                    construction_path,
                )
            row["construction_artifact"] = str(construction_path)
            _save_state(state_path, state)

            if not identity_path.is_file():
                _identity_check(
                    args.endpoint,
                    vault_path,
                    case_root,
                    identity_path,
                    case_log,
                    heartbeat_s=args.heartbeat_s,
                )
            row["identity_artifact"] = str(identity_path)
            _save_state(state_path, state)

            run_dir = row.get("eval_run_dir")
            if not isinstance(run_dir, str) or not (Path(run_dir) / "report.json").is_file():
                run_path = _run_case_eval(
                    args.endpoint,
                    vault_path,
                    case_root,
                    runtime,
                    env_file=args.env_file.expanduser(),
                    log_path=case_log,
                    timeout_s=args.eval_timeout_s,
                    heartbeat_s=args.heartbeat_s,
                    no_judge=args.no_judge,
                )
                row["eval_run_dir"] = str(run_path)
            row["status"] = "done"
            row["updated_at"] = _now()
            _save_state(state_path, state)
            print(f"[{index}/{len(cases)}] DONE {case_id}", flush=True)
        except Exception as exc:
            row["status"] = "error"
            row["error"] = str(exc)
            row["updated_at"] = _now()
            _save_state(state_path, state)
            print(f"[{index}/{len(cases)}] ERROR {case_id}: {exc}", file=sys.stderr, flush=True)
            return 1
    return 0


def _upstream_pin(upstream: Path) -> dict[str, str]:
    try:
        revision = subprocess.run(
            ["git", "-C", str(upstream), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise DiagnosticError(f"could not inspect upstream checkout: {exc}") from exc
    if revision != UPSTREAM_REVISION:
        raise DiagnosticError(
            f"upstream checkout must be pinned to {UPSTREAM_REVISION}, got {revision}"
        )
    return {
        "revision": revision,
        "retrieval_source_sha256": _sha256_file(upstream / UPSTREAM_RETRIEVAL_FILE),
        "evaluation_source_sha256": _sha256_file(upstream / UPSTREAM_EVAL_FILE),
    }


def _upstream_session_corpus(record: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    corpus = []
    ids = []
    for session_id, session in zip(record["haystack_session_ids"], record["haystack_sessions"]):
        user_turns = [turn for turn in session if turn.get("role") == "user"]
        corpus.append(" ".join(str(turn.get("content", "")) for turn in user_turns))
        corpus_id = session_id
        if "answer" in corpus_id and all(not turn.get("has_answer", False) for turn in user_turns):
            corpus_id = corpus_id.replace("answer", "noans")
        ids.append(corpus_id)
    return corpus, ids


def _ranking_metrics(
    rankings: Sequence[int], correct_docs: set[str], corpus_ids: Sequence[str]
) -> dict[str, float]:
    metrics = {}
    for k in K_VALUES:
        retrieved = [corpus_ids[index] for index in rankings[:k]]
        hits = [index for index, item in enumerate(retrieved) if item in correct_docs]
        recall_any = float(bool(hits))
        recall_all = float(all(item in set(retrieved) for item in correct_docs))
        if not hits:
            ndcg = 0.0
        else:
            # Preserve LongMemEval's pinned eval_utils.py exactly: rank zero has weight 1,
            # and later ranks use 1/log2(rank + 1), so rank one also has weight 1.
            def discount(index: int) -> float:
                return 1.0 if index == 0 else 1.0 / math.log2(index + 1)

            dcg = sum(discount(index) for index in hits)
            ideal = sum(discount(index) for index in range(min(len(correct_docs), k)))
            ndcg = dcg / ideal if ideal else 0.0
        metrics[f"recall_any@{k}"] = recall_any
        metrics[f"recall_all@{k}"] = recall_all
        metrics[f"ndcg_any@{k}"] = ndcg
    return metrics


def run_flat_bm25(
    *,
    source: Path,
    materialized: Path,
    upstream: Path,
    out: Path,
    bm25_factory: Callable[[list[list[str]]], Any] | None = None,
) -> dict[str, Any]:
    manifest = _materialization(materialized)
    upstream_pin = _upstream_pin(upstream)
    source_bytes = source.read_bytes()
    if _sha256_bytes(source_bytes) != manifest["source"]["sha256"]:
        raise DiagnosticError("LongMemEval source SHA-256 does not match materialization")
    records = json.loads(source_bytes)
    if not isinstance(records, list):
        raise DiagnosticError("LongMemEval source must be a JSON array")
    by_id = {row.get("question_id"): row for row in records if isinstance(row, dict)}
    selected_ids = list(manifest["selected_question_ids"])
    if set(selected_ids) - set(by_id):
        raise DiagnosticError("selected question id is absent from pinned source")
    if bm25_factory is None:
        try:
            from rank_bm25 import BM25Okapi
        except ImportError as exc:
            raise DiagnosticError(
                "rank-bm25 0.2.2 is required; run with `uv run --with rank-bm25==0.2.2`"
            ) from exc
        if importlib.metadata.version("rank-bm25") != "0.2.2":
            raise DiagnosticError("flat BM25 diagnostic requires rank-bm25==0.2.2")
        bm25_factory = BM25Okapi

    started = time.perf_counter()
    rows = []
    for question_id in selected_ids:
        record = _require_mapping(by_id[question_id], f"source record {question_id}")
        corpus, corpus_ids = _upstream_session_corpus(record)
        model = bm25_factory([document.split(" ") for document in corpus])
        scores = model.get_scores(str(record["question"]).split(" "))
        rankings = np.argsort(scores)[::-1].tolist()
        correct_docs = {item for item in corpus_ids if "answer" in item}
        excluded = question_id.endswith("_abs") or not correct_docs
        rows.append(
            {
                "question_id": question_id,
                "question_type": record["question_type"],
                "excluded_from_retrieval_average": excluded,
                "correct_session_ids": sorted(correct_docs),
                "ranked_session_ids": [corpus_ids[index] for index in rankings[:30]],
                "metrics": _ranking_metrics(rankings, correct_docs, corpus_ids),
            }
        )
    eligible = [row for row in rows if not row["excluded_from_retrieval_average"]]
    averages = {
        metric: sum(row["metrics"][metric] for row in eligible) / len(eligible)
        for metric in rows[0]["metrics"]
    }
    result = {
        "schema_version": BM25_SCHEMA,
        "status": "measured",
        "measured_at": _now(),
        "case_count": len(rows),
        "eligible_case_count": len(eligible),
        "excluded_case_count": len(rows) - len(eligible),
        "elapsed_ms": (time.perf_counter() - started) * 1000,
        "configuration": {
            "implementation": "upstream-compatible-flat-bm25",
            "granularity": "session",
            "indexed_roles": ["user"],
            "tokenization": "str.split(' ')",
            "rank_bm25_version": "0.2.2",
            "k_values": list(K_VALUES),
        },
        "upstream": upstream_pin,
        "materialization_sha256": _sha256_file(materialized / "reproducibility-manifest.json"),
        "source_sha256": _sha256_bytes(source_bytes),
        "selected_question_ids_sha256": manifest["selected_question_ids_sha256"],
        "averages": averages,
        "cases": rows,
        "disclosure": (
            "Runs the pinned upstream session/user-only transformation, rank-bm25 0.2.2, "
            "space splitting, ordering, and retrieval metrics without importing the upstream "
            "entrypoint's unused CUDA/dense-model dependencies."
        ),
    }
    _write_json(out, result)
    return result


def flat_bm25_command(args: argparse.Namespace) -> int:
    result = run_flat_bm25(
        source=args.source.resolve(),
        materialized=args.materialized.resolve(),
        upstream=args.upstream.resolve(),
        out=args.out.resolve(),
    )
    print(json.dumps({key: result[key] for key in ("status", "case_count", "averages")}, indent=2))
    return 0


def _artifact_pin(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise DiagnosticError(f"artifact is missing: {path}")
    return {"path": str(path.resolve()), "sha256": f"sha256:{_sha256_file(path)}"}


def _latency_ms(response: Mapping[str, Any]) -> float | None:
    timing = response.get("timing")
    if (
        isinstance(timing, dict)
        and timing.get("schema_version") == "golden_http_timing.v1"
        and isinstance(timing.get("recall_elapsed_ms"), (int, float))
        and not isinstance(timing.get("recall_elapsed_ms"), bool)
    ):
        return float(timing["recall_elapsed_ms"])
    recall = response.get("recall")
    if not isinstance(recall, dict):
        return None
    cost = recall.get("recall_cost")
    if not isinstance(cost, dict):
        return None
    for key in ("latency_ms", "elapsed_ms", "total_ms"):
        value = cost.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _ask_latency_ms(response: Mapping[str, Any]) -> float | None:
    timing = response.get("timing")
    if (
        isinstance(timing, dict)
        and timing.get("schema_version") == "golden_http_timing.v1"
        and isinstance(timing.get("ask_elapsed_ms"), (int, float))
        and not isinstance(timing.get("ask_elapsed_ms"), bool)
    ):
        return float(timing["ask_elapsed_ms"])
    return None


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    return float(np.percentile(np.asarray(values, dtype=float), percentile))


def aggregate_marginalia(args: argparse.Namespace) -> int:
    materialized = args.materialized.resolve()
    manifest = _materialization(materialized)
    state = _load_state(args.run_root / "state.json", materialized, manifest)
    cases = _case_rows(materialized, manifest)
    if any(state["cases"][row["case_id"]].get("status") != "done" for row in cases):
        raise DiagnosticError("all 35 Okto Neuron cases must be done before aggregation")

    retrieval_cases = []
    rag_cases = []
    recall_latencies = []
    ask_latencies = []
    construction_costs: list[Mapping[str, Any]] = []
    for case in cases:
        case_id = case["case_id"]
        case_state = state["cases"][case_id]
        run_dir = Path(case_state["eval_run_dir"])
        responses_path = run_dir / "responses.jsonl"
        lines = [json.loads(line) for line in responses_path.read_text().splitlines() if line]
        if len(lines) != 1:
            raise DiagnosticError(f"case {case_id} must have exactly one response")
        response = _require_mapping(lines[0], f"case {case_id} response")
        deterministic = _require_mapping(
            _load_json(run_dir / "deterministic.json"), f"case {case_id} deterministic"
        )
        latency = _latency_ms(response)
        if latency is not None:
            recall_latencies.append(latency)
        ask_latency = _ask_latency_ms(response)
        if ask_latency is not None:
            ask_latencies.append(ask_latency)
        judge_path = run_dir / "judge.json"
        construction_path = Path(str(case_state.get("construction_artifact") or ""))
        construction_artifact = _require_mapping(
            _load_json(construction_path), f"case {case_id} construction artifact"
        )
        if (
            construction_artifact.get("schema_version") != CONSTRUCTION_ARTIFACT_SCHEMA
            or construction_artifact.get("status") != "measured"
        ):
            raise DiagnosticError(f"case {case_id} construction artifact is not measured")
        construction_costs.append(
            _validated_cost(
                construction_artifact.get("totals"),
                f"case {case_id} construction totals",
            )
        )
        rag_cases.append(
            {
                "case_id": case_id,
                "question_id": case["question_id"],
                "selection_bucket": case["selection_bucket"],
                "response_artifact": _artifact_pin(responses_path),
                "judge_artifact": _artifact_pin(judge_path) if judge_path.is_file() else None,
                "ask_transport_ok": bool(response.get("ask")),
                "ask_latency_ms": ask_latency,
            }
        )
        retrieval_cases.append(
            {
                "case_id": case_id,
                "question_id": case["question_id"],
                "selection_bucket": case["selection_bucket"],
                "response_artifact": _artifact_pin(responses_path),
                "deterministic_artifact": _artifact_pin(run_dir / "deterministic.json"),
                "semantic_quality_artifact": _artifact_pin(run_dir / "semantic-quality.json"),
                "identity_artifact": _artifact_pin(Path(case_state["identity_artifact"])),
                "construction_artifact": _artifact_pin(construction_path),
                "provenance_gate_pass": deterministic.get("provenance_gate_pass"),
                "recall_latency_ms": latency,
            }
        )

    runtime = _require_mapping(state.get("runtime"), "state.runtime")
    construction_cost = combine_construction_costs(construction_costs)
    if construction_cost["status"] != "measured":
        raise DiagnosticError("combined Okto Neuron construction cost is partial")
    common = {
        "measured_at": _now(),
        "case_count": len(cases),
        "materialization_sha256": _sha256_file(materialized / "reproducibility-manifest.json"),
        "selected_question_ids_sha256": manifest["selected_question_ids_sha256"],
        "runtime": runtime,
        "construction_cost": construction_cost,
        "marginalia_revision": subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip(),
    }
    retrieval = {
        "schema_version": RETRIEVAL_SCHEMA,
        "status": "measured",
        **common,
        "retrieval_p50_ms": _percentile(recall_latencies, 50),
        "retrieval_p95_ms": _percentile(recall_latencies, 95),
        "cases": retrieval_cases,
    }
    rag = {
        "schema_version": RAG_SCHEMA,
        "status": "measured",
        **common,
        "judge_case_count": sum(row["judge_artifact"] is not None for row in rag_cases),
        "qa_p50_ms": _percentile(ask_latencies, 50),
        "qa_p95_ms": _percentile(ask_latencies, 95),
        "cases": rag_cases,
    }
    if rag["judge_case_count"] != len(cases):
        raise DiagnosticError("every direct-RAG case requires a judge artifact")
    if len(recall_latencies) != len(cases) or len(ask_latencies) != len(cases):
        raise DiagnosticError("every case requires measured recall and ask latency")
    retrieval_path = args.out_dir / "marginalia-retrieval.json"
    rag_path = args.out_dir / "direct-rag.json"
    _write_json(retrieval_path, retrieval)
    _write_json(rag_path, rag)
    print(retrieval_path)
    print(rag_path)
    return 0


def receipt_command(args: argparse.Namespace) -> int:
    manifest = args.materialized / "reproducibility-manifest.json"
    arms = {
        "marginalia_retrieval": _artifact_pin(args.marginalia_retrieval),
        "direct_rag": _artifact_pin(args.direct_rag),
        "flat_bm25": _artifact_pin(args.flat_bm25),
    }
    counts = {}
    for name, artifact in arms.items():
        value = _require_mapping(_load_json(Path(artifact["path"])), name)
        if value.get("status") != "measured" or not isinstance(value.get("case_count"), int):
            raise DiagnosticError(f"{name} is not a measured diagnostic artifact")
        counts[name] = value["case_count"]
    receipt = {
        "status": "measured",
        "manifest_sha256": f"sha256:{_sha256_file(manifest)}",
        **{
            name: {
                "status": "measured",
                "artifact_sha256": artifact["sha256"],
                "case_count": counts[name],
            }
            for name, artifact in arms.items()
        },
    }
    _write_json(args.out, receipt)
    print(args.out)
    return 0


def status_command(args: argparse.Namespace) -> int:
    state = _require_mapping(_load_json(args.run_root / "state.json"), "diagnostic state")
    counts: dict[str, int] = {}
    errors = []
    for case_id, raw in _require_mapping(state.get("cases"), "state.cases").items():
        row = _require_mapping(raw, f"state case {case_id}")
        status = str(row.get("status"))
        counts[status] = counts.get(status, 0) + 1
        if status == "error":
            errors.append({"case_id": case_id, "error": row.get("error")})
    print(
        json.dumps(
            {"counts": counts, "errors": errors, "updated_at": state.get("updated_at")}, indent=2
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    marginalia = subparsers.add_parser("run-marginalia")
    marginalia.add_argument("--materialized", type=Path, required=True)
    marginalia.add_argument("--endpoint", default="http://127.0.0.1:7777")
    marginalia.add_argument("--run-root", type=Path, required=True)
    marginalia.add_argument("--env-file", type=Path, default=Path("~/.marginalia/env"))
    marginalia.add_argument("--ingest-timeout-s", type=float, default=21600)
    marginalia.add_argument("--reconciliation-timeout-s", type=float, default=21600)
    marginalia.add_argument("--eval-timeout-s", type=float, default=1800)
    marginalia.add_argument("--heartbeat-s", type=float, default=30)
    marginalia.add_argument("--vault-prefix", default="adr0040-lme")
    marginalia.add_argument("--case-id", action="append")
    marginalia.add_argument("--limit", type=int)
    marginalia.add_argument("--no-judge", action="store_true")
    marginalia.set_defaults(handler=run_marginalia)

    bm25 = subparsers.add_parser("flat-bm25")
    bm25.add_argument("--source", type=Path, required=True)
    bm25.add_argument("--materialized", type=Path, required=True)
    bm25.add_argument("--upstream", type=Path, required=True)
    bm25.add_argument("--out", type=Path, required=True)
    bm25.set_defaults(handler=flat_bm25_command)

    aggregate = subparsers.add_parser("aggregate")
    aggregate.add_argument("--materialized", type=Path, required=True)
    aggregate.add_argument("--run-root", type=Path, required=True)
    aggregate.add_argument("--out-dir", type=Path, required=True)
    aggregate.set_defaults(handler=aggregate_marginalia)

    receipt = subparsers.add_parser("receipt")
    receipt.add_argument("--materialized", type=Path, required=True)
    receipt.add_argument("--marginalia-retrieval", type=Path, required=True)
    receipt.add_argument("--direct-rag", type=Path, required=True)
    receipt.add_argument("--flat-bm25", type=Path, required=True)
    receipt.add_argument("--out", type=Path, required=True)
    receipt.set_defaults(handler=receipt_command)

    status = subparsers.add_parser("status")
    status.add_argument("--run-root", type=Path, required=True)
    status.set_defaults(handler=status_command)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if hasattr(args, "heartbeat_s") and not 1 <= args.heartbeat_s <= 30:
        raise DiagnosticError("heartbeat-s must be between 1 and 30 seconds")
    if getattr(args, "limit", None) is not None and args.limit <= 0:
        raise DiagnosticError("limit must be greater than zero")
    vault_prefix = getattr(args, "vault_prefix", None)
    if vault_prefix is not None and not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,50}", vault_prefix):
        raise DiagnosticError("vault-prefix must use 1-51 lowercase letters, numbers, or hyphens")
    try:
        return args.handler(args)
    except DiagnosticError as exc:
        print(f"diagnostic error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
