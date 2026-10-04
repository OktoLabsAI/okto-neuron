"""Synthetic candidate ledgers built through the ledger's own public writers.

Every row goes through ``CandidateLedger.record_*`` so the synthetic file is
byte-for-byte what a real ingest would write (valid framing, valid sealed
plans), only with deterministic, bulky payloads. Used by the ledger index and
streaming-reader tests and by the out-of-tree memory probes.
"""

from __future__ import annotations

import zlib
from pathlib import Path
from typing import Any

from okto_neuron.consolidate.ledger import LEDGER_FILENAME, CandidateLedger

_WORDS = (
    "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho "
    "sigma tau upsilon phi chi psi omega"
).split()


def _text(seed: int, size: int) -> str:
    out: list[str] = []
    total = 0
    i = seed
    while total < size:
        word = _WORDS[i % len(_WORDS)] + str(i % 97)
        out.append(word)
        total += len(word) + 1
        i = i * 1103515245 + 12345 & 0x7FFFFFFF
    return " ".join(out)[:size]


def dead_letter_operation(candidate_id: str, *, note_chars: int = 0) -> dict[str, Any]:
    candidate: dict[str, Any] = {"type": "mentions", "src_ref": "node-1", "dst_ref": "node-2"}
    if note_chars:
        candidate["note"] = _text(zlib.crc32(candidate_id.encode()) & 0xFFFF, note_chars)
    return {
        "operation": "dead_letter",
        "candidate_kind": "edge",
        "candidate_id": candidate_id,
        "candidate": candidate,
        "reason": "synthetic",
    }


def close_plan(ledger: CandidateLedger, run_id: str, plan_id: str) -> None:
    """Receipt every operation of ``plan_id`` and write its commit record."""
    plan = next(p for p in ledger.unreceipted_commit_plans() if p.plan_id == plan_id)
    for operation in plan.operations:
        ledger.record_operation_receipt(
            run_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            operation_id=str(operation["operation_id"]),
            operation=str(operation["operation"]),
            status="dead_lettered",
            result={"candidate_id": operation["candidate_id"]},
        )
    ledger.record_commit(run_id, plan_id=plan_id, result={"ok": True})


def build_synthetic_ledger(
    directory: Path,
    *,
    target_bytes: int,
    open_plans: int = 3,
    row_chars: int = 8_000,
    plan_ops: int = 6,
    op_chars: int = 1_500,
) -> dict[str, Any]:
    """Append documents until the ledger file reaches ``target_bytes``.

    Each document is one run: started, candidate and comparison rows, a sealed
    plan, per-operation receipts, a commit record and a completed row. The last
    ``open_plans`` documents keep their plan unreceipted (crash-interrupted
    apply). Returns the run ids, open plan ids and run count.
    """

    ledger = CandidateLedger(directory)
    path = directory / LEDGER_FILENAME
    runs: list[str] = []
    open_ids: list[str] = []

    def add_document(doc: int, *, keep_open: bool) -> None:
        run_id = ledger.start_run(
            document_id=f"doc-{doc}", source=f"/synthetic/doc-{doc}.md", blocks_total=3, model="m"
        )
        runs.append(run_id)
        for c in range(3):
            ledger.record_candidate(
                run_id,
                candidate_id=f"c-{doc}-{c}",
                candidate_kind="node",
                state="proposed",
                payload={
                    "type": "Agent",
                    "title": f"Doc {doc} node {c}",
                    "summary": _text(doc * 31 + c, row_chars),
                },
            )
            ledger.record_comparison(
                run_id,
                candidate_id=f"c-{doc}-{c}",
                method="curator",
                verdict="commit",
                reason=_text(doc * 17 + c, row_chars // 2),
                payload={"duration_s": 0.5, "after": {"nodes": c + 1, "edges": c}},
            )
        plan_id = ledger.record_commit_plan(
            run_id,
            operations=[
                dead_letter_operation(f"e-{doc}-{k}", note_chars=op_chars) for k in range(plan_ops)
            ],
            context={"document_id": f"doc-{doc}"},
        )
        if keep_open:
            open_ids.append(plan_id)
            return
        close_plan(ledger, run_id, plan_id)
        ledger.finish_run(run_id, state="completed", summary={"outcome": {"quality": "complete"}})

    doc = 0
    while not path.exists() or path.stat().st_size < target_bytes:
        add_document(doc, keep_open=False)
        doc += 1
    for _ in range(open_plans):
        add_document(doc, keep_open=True)
        doc += 1
    return {"runs": runs, "open_plans": open_ids, "documents": doc}
