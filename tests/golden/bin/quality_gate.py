#!/usr/bin/env python3
"""quality_gate.py — Tier 1 scoring, merge, and baseline gate for the 3-tier gate.

The shell orchestrator (`quality-gate.sh`) owns isolation, the suite-owned daemon,
live ingest, and the HTTP legs. This module owns only the deterministic parts:

  must-contain  deterministic must_contain scoring + negative-control abstention
  report        merge every Tier 1/Tier 2 sidecar into quality_gate_report.json
  gate          count-drop regression gate vs a committed baseline (exit 0/1/2)
  mint          write a baseline from a blessed report (always stamped provisional)
  selftest      pure-logic validation (no vault, no daemon, no network)

TIER DISCIPLINE (binding):
  * Tier 0 (CI, .github/workflows/eval-gate.yml) is untouched by this file.
  * Tier 1 metrics gate on COUNTS, exactly like recall_floor_baseline.json.
  * Tier 2 (semantic judge) is ADVISORY. Its tally is recorded and NEVER gated,
    until a measured Fleiss/Cohen kappa >= 0.60 against human labels is committed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from golden_yaml import load_yaml  # noqa: E402
from semantic_judge import deterministic_backstop  # noqa: E402

# A negative control passes only when the answer explicitly declines. These are
# deterministic surface markers, not a judge: the point is a model-free abstention
# signal that cannot be argued with. Two shapes cover real declines — a bare "no
# X" phrase, and a "<subject> do/does not <assert-verb>" construction.
ABSTENTION_MARKERS: tuple[str, ...] = (
    "no source",
    "no sources",
    "no evidence",
    "no record",
    "no basis",
    "no mention",
    "no statement",
    "not supported",
    "unsupported",
    "not established",
    "not stated",
    "nothing in the",
    "cannot be supported",
)

# "do not state", "does not say", "don't establish", "doesn't mention", ...
ABSTENTION_PATTERN = re.compile(
    r"\b(?:do(?:es)?\s+not|don't|doesn't|did\s+not|didn't)\s+"
    r"(?:\w+\s+){0,2}?"
    r"(?:say|state|establish|mention|indicate|support|show|record|link|claim|assert|contain)",
    re.IGNORECASE,
)


# Bump this whenever scoring semantics change (e.g. the abstention rule). Reports
# scored under different semantics must never be mixed into one baseline floor.
MUST_CONTAIN_SCHEMA = "quality_gate_must_contain.v2"


def _answer_text(record: dict[str, Any]) -> str:
    ask = record.get("ask")
    if not isinstance(ask, dict):
        return ""
    for key in ("answer", "text", "response"):
        value = ask.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def read_responses(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def abstains(answer: str) -> bool:
    lowered = answer.lower()
    if any(marker in lowered for marker in ABSTENTION_MARKERS):
        return True
    return bool(ABSTENTION_PATTERN.search(answer))


def score_must_contain(
    questions: dict[str, Any], responses: list[dict[str, Any]]
) -> dict[str, Any]:
    """Deterministic must_contain + negative-control abstention scoring.

    Denominators are explicit and separate: a question with an EMPTY must_contain
    would score vacuously true, so it is never counted in the positive band.
    """
    by_id = {str(q.get("id")): q for q in (questions.get("questions") or [])}
    per_question: list[dict[str, Any]] = []
    empty_answers: list[str] = []
    positive_total = positive_pass = 0
    negative_total = negative_pass = 0

    for record in responses:
        qid = str(record.get("id"))
        question = by_id.get(qid, {})
        answer = _answer_text(record)
        if not answer.strip():
            empty_answers.append(qid)
        must_contain = [s for s in (question.get("must_contain") or []) if str(s).strip()]
        negative = bool(question.get("negative_control"))
        row: dict[str, Any] = {"id": qid, "answer_present": bool(answer.strip())}
        if negative:
            negative_total += 1
            ok = bool(answer.strip()) and abstains(answer)
            negative_pass += int(ok)
            row.update({"band": "negative_control", "abstained": ok})
        elif must_contain:
            positive_total += 1
            backstop = deterministic_backstop(answer, must_contain)
            ok = bool(backstop.get("all_present"))
            positive_pass += int(ok)
            row.update(
                {
                    "band": "must_contain",
                    "all_present": ok,
                    "missing": backstop.get("missing") or [],
                }
            )
        else:
            row.update({"band": "unscored", "reason": "no must_contain, not a negative control"})
        per_question.append(row)

    return {
        "schema_version": MUST_CONTAIN_SCHEMA,
        "must_contain": {
            "questions": positive_total,
            "all_present": positive_pass,
            "rate": round(positive_pass / positive_total, 4) if positive_total else 0.0,
        },
        "negative_control": {
            "questions": negative_total,
            "abstained": negative_pass,
            "rate": round(negative_pass / negative_total, 4) if negative_total else 0.0,
        },
        "empty_answers": sorted(empty_answers),
        "per_question": per_question,
    }


# ════════════════════════════════════════════════════════════════════════════
# report merge
# ════════════════════════════════════════════════════════════════════════════

GATED_METRICS: tuple[tuple[str, str, str], ...] = (
    # (metric, count key, total key)
    ("citation_byte_verification", "citations_pass", "citations_verifiable"),
    ("hard_recall_at_k", "gold_targets_hit", "gold_targets_total"),
    ("extraction_completeness", "gold_targets_present", "gold_targets_total"),
    ("claim_coverage", "claim_present", "claim_total"),
    ("must_contain", "all_present", "questions"),
    ("negative_control", "abstained", "questions"),
)


def _load(path: str | None) -> Any:
    if not path:
        return None
    return json.loads(Path(path).expanduser().read_text(encoding="utf-8"))


def build_report(
    *,
    responses: list[dict[str, Any]],
    floor_laptop: Any,
    recall_floor: Any,
    must_contain: Any,
    judge: Any,
    meta: dict[str, Any],
) -> dict[str, Any]:
    citations = (floor_laptop or {}).get("citation_byte_verification") or {}
    metrics = {
        "citation_byte_verification": {
            "citations_pass": int(citations.get("citations_pass") or 0),
            "citations_verifiable": int(citations.get("citations_verifiable") or 0),
            "citation_floor_pass": bool(citations.get("citation_floor_pass")),
        },
        "hard_recall_at_k": dict((recall_floor or {}).get("hard_recall_at_k") or {}),
        "extraction_completeness": dict((recall_floor or {}).get("extraction_completeness") or {}),
        "claim_coverage": dict((recall_floor or {}).get("claim_coverage") or {}),
        "must_contain": dict((must_contain or {}).get("must_contain") or {}),
        "negative_control": dict((must_contain or {}).get("negative_control") or {}),
    }
    payload = json.dumps(metrics, sort_keys=True, separators=(",", ":")).encode("utf-8")
    report: dict[str, Any] = {
        "report": "quality_gate",
        "schema_version": "quality_gate_report.v1",
        "tier0": {"source": ".github/workflows/eval-gate.yml", "edited_by_this_gate": False},
        "meta": meta,
        "scoring_schema": (must_contain or {}).get("schema_version") or "unknown",
        "questions_answered": len(responses),
        "empty_answers": (must_contain or {}).get("empty_answers") or [],
        "metrics": metrics,
        "metric_payload_sha256": hashlib.sha256(payload).hexdigest(),
        "tier2_advisory": {
            "gating": False,
            "reason": "semantic judge kappa vs human labels is unmeasured; "
            "advisory until a measured kappa >= 0.60 is committed",
            "tally": (judge or {}).get("tally"),
            "model": (judge or {}).get("model"),
        },
        "per_question_must_contain": (must_contain or {}).get("per_question") or [],
    }
    return report


def compare_to_baseline(
    report: dict[str, Any], baseline: dict[str, Any], tolerance: int = 0
) -> dict[str, Any]:
    """Count-drop regression check — the recall_floor_baseline.json discipline."""
    regressions: list[str] = []
    total_changed: list[str] = []
    current = report.get("metrics") or {}
    base = baseline.get("metrics") or {}
    for metric, count_key, total_key in GATED_METRICS:
        cur = current.get(metric) or {}
        old = base.get(metric) or {}
        if not old:
            continue
        cur_count = int(cur.get(count_key) or 0)
        old_count = int(old.get(count_key) or 0)
        if cur_count < old_count - tolerance:
            regressions.append(f"{metric} {count_key} {old_count} -> {cur_count}")
        cur_total = int(cur.get(total_key) or 0)
        old_total = int(old.get(total_key) or 0)
        if cur_total != old_total:
            total_changed.append(f"{metric} {total_key} {old_total} -> {cur_total}")
    if current.get("citation_byte_verification", {}).get("citation_floor_pass") is not True:
        regressions.append("citation_floor_pass is not true")
    if report.get("empty_answers"):
        regressions.append(f"empty answers: {sorted(report['empty_answers'])}")
    return {
        "tolerance": tolerance,
        "baseline_provisional": bool(baseline.get("provisional")),
        "regressions": sorted(regressions),
        "total_changed": sorted(total_changed),
    }


def _write_stable(payload: dict[str, Any], out: Path) -> None:
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ════════════════════════════════════════════════════════════════════════════
# subcommands
# ════════════════════════════════════════════════════════════════════════════


def cmd_must_contain(args: argparse.Namespace) -> int:
    questions = load_yaml(Path(args.questions).expanduser())
    responses = read_responses(Path(args.responses).expanduser())
    result = score_must_contain(questions, responses)
    _write_stable(result, Path(args.out))
    print(
        json.dumps(
            {
                "must_contain": result["must_contain"],
                "negative_control": result["negative_control"],
                "empty_answers": result["empty_answers"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    responses = read_responses(Path(args.responses).expanduser())
    meta = json.loads(args.meta) if args.meta else {}
    report = build_report(
        responses=responses,
        floor_laptop=_load(args.floor_laptop),
        recall_floor=_load(args.recall_floor),
        must_contain=_load(args.must_contain),
        judge=_load(args.judge),
        meta=meta,
    )
    _write_stable(report, Path(args.out))
    print(
        json.dumps(
            {
                "metrics": report["metrics"],
                "metric_payload_sha256": report["metric_payload_sha256"],
                "tier2_advisory_tally": report["tier2_advisory"]["tally"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_gate(args: argparse.Namespace) -> int:
    report = _load(args.report)
    baseline_path = Path(args.baseline).expanduser()
    if not baseline_path.exists():
        print(json.dumps({"error": f"baseline not found: {baseline_path}"}), file=sys.stderr)
        return 2
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    comparison = compare_to_baseline(report, baseline, args.tolerance)
    report["baseline_comparison"] = comparison
    _write_stable(report, Path(args.report))
    print(json.dumps(comparison, indent=2, sort_keys=True))
    return 1 if comparison["regressions"] else 0


def mint_floor(reports: list[dict[str, Any]]) -> dict[str, Any]:
    """Element-wise MINIMUM across observed runs.

    Extraction is stochastic: on this corpus the same tree produced 11/11 and
    7/11 gold targets on consecutive runs. A baseline minted from one lucky run
    fails the next unchanged run and teaches everyone to ignore the gate. The
    floor across N observed runs is the honest "no run has ever been worse than
    this" line, which is exactly what a count-drop gate should defend.
    """
    floor: dict[str, Any] = {}
    for metric, count_key, total_key in GATED_METRICS:
        rows = [r.get("metrics", {}).get(metric) or {} for r in reports]
        rows = [row for row in rows if row]
        if not rows:
            continue
        merged = dict(rows[0])
        merged[count_key] = min(int(row.get(count_key) or 0) for row in rows)
        totals = {int(row.get(total_key) or 0) for row in rows}
        if len(totals) != 1:
            raise SystemExit(
                f"{metric} {total_key} disagrees across runs ({sorted(totals)}); "
                "the runs did not measure the same thing"
            )
        merged[total_key] = totals.pop()
        if merged[total_key]:
            merged["rate"] = round(merged[count_key] / merged[total_key], 4)
        if "citation_floor_pass" in merged:
            merged["citation_floor_pass"] = all(
                bool(row.get("citation_floor_pass")) for row in rows
            )
        floor[metric] = merged
    return floor


def cmd_mint(args: argparse.Namespace) -> int:
    reports = [_load(path) for path in args.report]
    schemas = {str(r.get("scoring_schema")) for r in reports}
    if len(schemas) > 1:
        print(
            json.dumps(
                {
                    "error": "refusing to mint across mixed scoring schemas",
                    "schemas": sorted(schemas),
                }
            ),
            file=sys.stderr,
        )
        return 2
    metrics = mint_floor(reports)
    payload = json.dumps(metrics, sort_keys=True, separators=(",", ":")).encode("utf-8")
    baseline = {
        "baseline": "quality_gate",
        "schema_version": "quality_gate_baseline.v1",
        "provisional": True,
        "provisional_note": (
            "PROVISIONAL. Minted before ADR 0040 adjudication closed. Re-mint from blessed "
            "runs once that adjudication lands; until then a pass means 'no regression vs a "
            "tree with known open findings', not 'quality proven'."
        ),
        "minting_rule": (
            "element-wise MINIMUM across the observed runs below — extraction is stochastic, "
            "so a single-run ceiling baseline would fail the next unchanged run"
        ),
        "runs": len(reports),
        "scoring_schema": sorted(schemas)[0] if schemas else "unknown",
        "run_metric_payloads": sorted(str(r.get("metric_payload_sha256")) for r in reports),
        "source_sha": args.source_sha,
        "minted_at": args.minted_at,
        "meta": (reports[-1].get("meta") if reports else {}) or {},
        "metrics": metrics,
        "metric_payload_sha256": hashlib.sha256(payload).hexdigest(),
        "tier2_advisory_only": True,
    }
    _write_stable(baseline, Path(args.out))
    print(
        json.dumps(
            {
                "minted": str(args.out),
                "provisional": True,
                "runs": len(reports),
                "metric_payload_sha256": baseline["metric_payload_sha256"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_selftest(_args: argparse.Namespace) -> int:
    print("SELFTEST — quality_gate (no vault, no daemon, no network)")
    ok = True

    questions = {
        "questions": [
            {"id": "pos", "must_contain": ["Dr. Maren Vale"]},
            {"id": "vacuous", "must_contain": []},
            {"id": "neg", "must_contain": [], "negative_control": True},
        ]
    }
    responses = [
        {"id": "pos", "ask": {"answer": "That is Dr. Maren Vale."}},
        {"id": "vacuous", "ask": {"answer": "anything"}},
        {"id": "neg", "ask": {"answer": "No source establishes that."}},
    ]
    scored = score_must_contain(questions, responses)
    d_ok = (
        scored["must_contain"] == {"questions": 1, "all_present": 1, "rate": 1.0}
        and scored["negative_control"] == {"questions": 1, "abstained": 1, "rate": 1.0}
        and scored["per_question"][1]["band"] == "unscored"
    )
    print(
        f"  {'ok  ' if d_ok else 'FAIL'} denominators: empty must_contain is UNSCORED, "
        "negative control scored as abstention"
    )
    ok &= d_ok

    e_ok = score_must_contain(questions, [{"id": "pos", "ask": {}}])["empty_answers"] == ["pos"]
    print(f"  {'ok  ' if e_ok else 'FAIL'} empty ask answer is recorded, not silently passed")
    ok &= e_ok

    n_ok = (
        not abstains("Dr. Maren Vale authored it.")
        and abstains("No evidence supports that.")
        # the two shapes the live model actually produced
        and abstains("The provided notes do not state who authored the manual.")
        and abstains("No. The provided notes do not establish that Maren knows the doctrine.")
    )
    print(f"  {'ok  ' if n_ok else 'FAIL'} abstention markers discriminate")
    ok &= n_ok

    base = {
        "metrics": {
            "must_contain": {"all_present": 9, "questions": 11},
            "hard_recall_at_k": {"gold_targets_hit": 8, "gold_targets_total": 11},
        },
        "provisional": True,
    }
    same = {
        "metrics": {
            "must_contain": {"all_present": 9, "questions": 11},
            "hard_recall_at_k": {"gold_targets_hit": 8, "gold_targets_total": 11},
            "citation_byte_verification": {"citation_floor_pass": True},
        }
    }
    worse = json.loads(json.dumps(same))
    worse["metrics"]["must_contain"]["all_present"] = 8
    g_ok = (
        not compare_to_baseline(same, base)["regressions"]
        and compare_to_baseline(worse, base)["regressions"]
        and compare_to_baseline(same, base)["baseline_provisional"] is True
    )
    print(f"  {'ok  ' if g_ok else 'FAIL'} count-drop gate: equal passes, one-count drop fails")
    ok &= g_ok

    empty_flagged = compare_to_baseline({**same, "empty_answers": ["q1"]}, base)["regressions"]
    f_ok = any("empty answers" in r for r in empty_flagged)
    print(f"  {'ok  ' if f_ok else 'FAIL'} an all-empty/transport-degraded run cannot pass")
    ok &= f_ok

    lucky = {"metrics": {"must_contain": {"all_present": 11, "questions": 11}}}
    unlucky = {"metrics": {"must_contain": {"all_present": 7, "questions": 11}}}
    floor = mint_floor([lucky, unlucky])["must_contain"]
    m_ok = floor["all_present"] == 7 and floor["questions"] == 11 and floor["rate"] == 0.6364
    print(
        f"  {'ok  ' if m_ok else 'FAIL'} baseline mint takes the FLOOR across runs, not a lucky ceiling"
    )
    ok &= m_ok

    print("-" * 60)
    print("SELFTEST: PASS" if ok else "SELFTEST: FAIL")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    pm = sub.add_parser("must-contain", help="deterministic must_contain + abstention scoring")
    pm.add_argument("--questions", required=True)
    pm.add_argument("--responses", required=True)
    pm.add_argument("--out", required=True)
    pm.set_defaults(func=cmd_must_contain)

    pr = sub.add_parser("report", help="merge Tier 1/Tier 2 sidecars into one report")
    pr.add_argument("--responses", required=True)
    pr.add_argument("--floor-laptop", dest="floor_laptop", default=None)
    pr.add_argument("--recall-floor", dest="recall_floor", default=None)
    pr.add_argument("--must-contain", dest="must_contain", default=None)
    pr.add_argument("--judge", default=None, help="Tier 2 advisory semantic-judge report")
    pr.add_argument("--meta", default=None, help="JSON object pinned into the report")
    pr.add_argument("--out", required=True)
    pr.set_defaults(func=cmd_report)

    pg = sub.add_parser("gate", help="count-drop regression gate vs a committed baseline")
    pg.add_argument("--report", required=True)
    pg.add_argument("--baseline", required=True)
    pg.add_argument("--tolerance", type=int, default=0)
    pg.set_defaults(func=cmd_gate)

    pb = sub.add_parser("mint", help="write a PROVISIONAL baseline from a blessed report")
    pb.add_argument(
        "--report",
        required=True,
        action="append",
        help="a blessed run report; repeat to mint the floor across runs",
    )
    pb.add_argument("--source-sha", dest="source_sha", required=True)
    pb.add_argument("--minted-at", dest="minted_at", required=True)
    pb.add_argument("--out", required=True)
    pb.set_defaults(func=cmd_mint)

    pt = sub.add_parser("selftest", help="pure-logic validation (no vault/daemon/network)")
    pt.set_defaults(func=cmd_selftest)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
