"""Capture every public ledger reader's output as plain JSON (replay-parity harness)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

from okto_neuron.consolidate.ledger import FRESH_REBUILD_MATERIALIZATION_SCOPE, CandidateLedger


def dump(obj: Any) -> Any:
    return json.loads(
        json.dumps(
            obj,
            default=lambda v: (
                str(v)
                if isinstance(v, Path)
                else dataclasses.asdict(v)
                if dataclasses.is_dataclass(v)
                else sorted(v)
                if isinstance(v, set)
                else repr(v)
            ),
        )
    )


def snap(plan: Any) -> dict[str, Any]:
    return dict(
        run_id=plan.run_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        operations=[dict(op) for op in plan.operations],
        context=plan.context,
    )


def capture(ledger: CandidateLedger, runs: list[str]) -> dict[str, Any]:
    """Every public reader, in the order the fixtures were captured."""
    res: dict[str, Any] = {}
    res["records"] = ledger.records()
    scan = dataclasses.asdict(ledger.scan())
    scan["path"] = "<path>"
    res["scan"] = dump(scan)
    res["run_summaries"] = {str(n): ledger.run_summaries(limit=n) for n in (1, 2, 500)}
    res["run_detail"] = {r: ledger.run_detail(r) for r in runs}
    res["run_detail"]["missing"] = ledger.run_detail("missing")
    res["progress"] = {r: ledger.run_progress_summary(r, limit=5) for r in runs}
    res["progress"]["latest"] = ledger.run_progress_summary(None, limit=5)
    res["progress"]["missing"] = ledger.run_progress_summary("missing", limit=5)
    try:
        res["unreceipted"] = {
            "all": [snap(p) for p in ledger.unreceipted_commit_plans()],
            "doc-b": [snap(p) for p in ledger.unreceipted_commit_plans(document_id="doc-b")],
            "doc-a": [snap(p) for p in ledger.unreceipted_commit_plans(document_id="doc-a")],
        }
        res["receipts"] = {
            p.plan_id: ledger.operation_receipts(p) for p in ledger.unreceipted_commit_plans()
        }
        res["decision_runs"] = list(
            ledger.find_completed_decision_runs(
                document_id="doc-a",
                blocks_total=2,
                model="m1",
                config_fingerprint="cf1",
                extraction_fingerprint="ef1",
                semantic_policy_fingerprint="sp1",
                materialization_scope=FRESH_REBUILD_MATERIALIZATION_SCOPE,
            )
        )
    except ValueError as exc:
        res["unreceipted_error"] = str(exc)
    res["open_runs"] = {k: v.get("document_id") for k, v in ledger._open_runs().items()}
    res["size_after"] = ledger.path.stat().st_size
    return dump(res)
