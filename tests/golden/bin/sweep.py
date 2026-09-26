#!/usr/bin/env python3
"""SWEEP — model x knob comparative grid for the Okto Neuron eval framework (P3).

Zero marginalia-source imports (black-box contract, same rule as
judge.py / manifest.py / scorecard.py). LAPTOP-ONLY when it executes cells: a
`run` cell hits the live /api/v1/ask endpoint and (optionally) the :8123 judge,
so it must not run in CI. Cells that only *reuse* captured arm JSONL are pure
file reads and run anywhere.

WHAT IT DOES
------------
Generalizes the two-arm calibration runner into an N-cell matrix. Each cell is
one config (a {model, k, query_neighbors, ...} point). The sweep:

  1. for each cell, OBTAINS per-question verdicts — either by reusing an existing
     arm JSONL (`source: arm:<path>`) or by running ask+grade against a live
     endpoint (`source: run`, the laptop path), writing the arm JSONL,
  2. picks a BASELINE cell (first, or --baseline LABEL),
  3. emits a comparative table: each cell's accuracy + N, then the full
     scorecard delta-vs-baseline (observed delta, McNemar exact p, bootstrap
     95%% CI, MDE@0.80, and the 5-condition REAL classification) — reusing
     scorecard.compare_arms verbatim, so the grid and the pairwise scorecard
     agree by construction.

KNOB SEMANTICS (mirrors run-golden.sh)
--------------------------------------
  * k                — PER-REQUEST: sent in the /api/v1/ask body, so two k cells
    can run back-to-back against ONE daemon.
  * query_neighbors  — SERVE-TIME env (OKTO_NEURON_QUERY_NEIGHBORS, read in
    Vault._query_neighbors at query time). Changing it needs a daemon restart,
    which the sweep does NOT do for you: declare it per cell only for the
    manifest/record, and point each neighbours cell at a daemon already serving
    that value (or reuse pre-captured arm JSONL, as the proof below does).
  * model            — recorded per cell; the answerer model is a serve-time
    choice in this product, so the same restart caveat applies as neighbours.

MATRIX FORMAT (JSON)
--------------------
  {
    "baseline": "neighbors0",            # optional; default = first cell
    "grader": "proxy",                   # proxy | judge
    "questions": "~/.../questions.yaml", # for on-the-fly proxy grading
    "cells": [
      {"label": "neighbors0", "source": "arm:~/.../calib/neighbors0.jsonl",
       "knobs": {"query_neighbors": 0, "k": 8}},
      {"label": "neighbors1", "source": "arm:~/.../calib/neighbors1.jsonl",
       "knobs": {"query_neighbors": 1, "k": 8}},
      {"label": "k20", "source": "run", "endpoint": "http://127.0.0.1:7777",
       "knobs": {"k": 20}, "out": "~/.../calib/k20.jsonl"}
    ]
  }

SUBCOMMAND / WIRING
-------------------
  sweep.py run --matrix matrix.json [--baseline LABEL] [--json]
Also wired into judge.py as `judge.py sweep ...` (mirrors scorecard / manifest).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# Sibling modules — same dir, same black-box contract.
import scorecard as _sc  # noqa: E402
from golden_yaml import GoldenYamlError, load_yaml

DEFAULT_K = 8
DEFAULT_TIMEOUT = 180.0


# ───────────────────────────── ask (run cells only) ──────────────────────────


def _ask_endpoint(endpoint: str, question: str, k: int, *, timeout: float) -> dict[str, Any]:
    """POST /api/v1/ask (calibrate.ask_endpoint's contract). run-cells only."""
    url = endpoint.rstrip("/") + "/api/v1/ask"
    data = json.dumps({"question": question, "k": k}).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _load_questions(path: Path) -> list[dict[str, Any]]:
    """Load complete gold questions through the authoritative YAML boundary."""

    doc = load_yaml(path)
    return doc.get("questions") or []


def _run_cell(cell: dict[str, Any], grader: str, questions_path: Path | None) -> Path:
    """Execute a `run` cell against its endpoint, grade with the proxy, write its
    arm JSONL, and return that path. (LLM-judge grading for run cells is left to
    the calibration runner; the sweep's run path grades with the deterministic
    must_contain proxy so it needs no judge.)"""
    if questions_path is None:
        raise SystemExit(
            f"cell {cell.get('label')!r}: 'run' source needs a top-level 'questions' path"
        )
    out = Path(cell["out"]).expanduser() if cell.get("out") else None
    if out is None:
        raise SystemExit(f"cell {cell.get('label')!r}: 'run' source needs an 'out' arm path")
    endpoint = cell.get("endpoint") or "http://127.0.0.1:7777"
    k = int((cell.get("knobs") or {}).get("k", DEFAULT_K))
    timeout = float(cell.get("timeout", DEFAULT_TIMEOUT))
    questions = _load_questions(questions_path)
    mc_map = _sc._load_questions_must_contain(questions_path)  # noqa: SLF001
    out.parent.mkdir(parents=True, exist_ok=True)
    print(
        f"[sweep] run cell {cell.get('label')!r}: endpoint={endpoint} k={k} "
        f"-> {out} ({len(questions)} questions)",
        file=sys.stderr,
    )
    written = 0
    with out.open("w", encoding="utf-8") as fh:
        for q in questions:
            qid = str(q.get("id"))
            question = str(q.get("question", ""))
            t0 = time.time()
            try:
                ask_resp = _ask_endpoint(endpoint, question, k, timeout=timeout)
            except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
                ask_resp = {"status": "error", "error": str(exc), "text": "", "hits": []}
            rec = {
                "id": qid,
                "question": question,
                "ask": ask_resp,
                "ask_ms": int((time.time() - t0) * 1000),
                "proxy_correct": _sc.proxy_correct_from_ask(mc_map.get(qid, []), ask_resp),
                "cell": cell.get("label"),
                "knobs": cell.get("knobs") or {},
            }
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            written += 1
    print(f"[sweep] wrote {written} records for cell {cell.get('label')!r}", file=sys.stderr)
    return out


# ───────────────────────────── matrix execution ──────────────────────────────


def _cell_arm_path(cell: dict[str, Any], grader: str, questions_path: Path | None) -> Path:
    """Resolve a cell to its arm JSONL: reuse `arm:<path>`, or execute a `run`."""
    source = str(cell.get("source", ""))
    if source.startswith("arm:"):
        return Path(source[len("arm:") :]).expanduser()
    if source == "run":
        return _run_cell(cell, grader, questions_path)
    raise SystemExit(
        f"cell {cell.get('label')!r}: unknown source {source!r} (expected 'arm:<path>' or 'run')"
    )


def run_sweep(matrix: dict[str, Any]) -> dict[str, Any]:
    """Execute/resolve every cell, grade, and build the comparative result vs the
    baseline cell. Returns a dict the renderer + --json share."""
    cells = matrix.get("cells") or []
    if len(cells) < 2:
        raise SystemExit("sweep needs at least 2 cells")
    grader = str(matrix.get("grader", "proxy"))
    questions_path = Path(matrix["questions"]).expanduser() if matrix.get("questions") else None
    labels = [str(c.get("label")) for c in cells]
    if len(set(labels)) != len(labels):
        raise SystemExit(f"duplicate cell labels in matrix: {labels}")

    baseline = str(matrix.get("baseline") or labels[0])
    if baseline not in labels:
        raise SystemExit(f"baseline {baseline!r} is not a cell label ({labels})")

    # Resolve each cell to {qid: correct_bool}.
    verdicts: dict[str, dict[str, bool]] = {}
    knobs_by_label: dict[str, dict[str, Any]] = {}
    for cell in cells:
        label = str(cell.get("label"))
        path = _cell_arm_path(cell, grader, questions_path)
        verdicts[label] = _sc.load_arm(path, grader=grader, questions=questions_path)
        knobs_by_label[label] = cell.get("knobs") or {}

    base_v = verdicts[baseline]

    # Per-cell accuracy on each cell's OWN paired-with-baseline question set, plus
    # the full pairwise scorecard vs the baseline for every non-baseline cell.
    rows: list[dict[str, Any]] = []
    for label in labels:
        v = verdicts[label]
        own_n = len(v)
        own_correct = sum(1 for ok in v.values() if ok)
        row: dict[str, Any] = {
            "label": label,
            "knobs": knobs_by_label[label],
            "own_n": own_n,
            "own_correct": own_correct,
            "own_acc": (own_correct / own_n) if own_n else 0.0,
            "is_baseline": (label == baseline),
        }
        if label != baseline:
            res = _sc.compare_arms(base_v, v)  # baseline=A, cell=B  -> delta = cell - baseline
            row["scorecard"] = res
        rows.append(row)

    return {
        "baseline": baseline,
        "grader": grader,
        "rows": rows,
    }


# ───────────────────────────── rendering ─────────────────────────────────────


def _fmt_knobs(knobs: dict[str, Any]) -> str:
    if not knobs:
        return "-"
    return " ".join(f"{k}={v}" for k, v in knobs.items())


def render_sweep(result: dict[str, Any]) -> str:
    rows = result["rows"]
    baseline = result["baseline"]
    grader = result["grader"]
    lines: list[str] = []
    lines.append("=" * 88)
    lines.append(f"  MODEL x KNOB SWEEP   grader={grader}   baseline={baseline!r}")
    lines.append("=" * 88)
    # cell summary table
    lines.append(
        f"  {'cell':<14} {'knobs':<22} {'N':>4} {'acc':>7} "
        f"{'delta':>8} {'McNemar p':>10} {'verdict':>26}"
    )
    lines.append("  " + "-" * 84)
    for row in rows:
        label = row["label"]
        knobs = _fmt_knobs(row["knobs"])
        n = row["own_n"]
        acc = f"{row['own_acc'] * 100:5.1f}%"
        if row["is_baseline"]:
            lines.append(
                f"  {label:<14} {knobs:<22} {n:>4} {acc:>7} "
                f"{'(base)':>8} {'-':>10} {'baseline':>26}"
            )
        else:
            sc = row["scorecard"]
            delta = f"{sc['delta'] * 100:+5.1f}pt"
            p = f"{sc['exact_p']:.4f}"
            verdict = sc["classification"]
            lines.append(
                f"  {label:<14} {knobs:<22} {n:>4} {acc:>7} {delta:>8} {p:>10} {verdict:>26}"
            )
    lines.append("=" * 88)
    # per-comparison detail
    for row in rows:
        if row["is_baseline"]:
            continue
        sc = row["scorecard"]
        lines.append("")
        lines.append(
            _sc.render_scorecard(sc, label_a=baseline, label_b=row["label"], grader=grader)
        )
    return "\n".join(lines)


# ───────────────────────────── subcommand / wiring ───────────────────────────


def cmd_run(args: argparse.Namespace) -> int:
    matrix = json.loads(Path(args.matrix).expanduser().read_text(encoding="utf-8"))
    if args.baseline:
        matrix["baseline"] = args.baseline
    result = run_sweep(matrix)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(render_sweep(result))
    return 0


def build_sweep_parser(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Attach `sweep` arguments (shared with judge.py's wiring)."""
    p.add_argument(
        "--matrix", required=True, help="matrix JSON describing >=2 cells (see module docstring)"
    )
    p.add_argument(
        "--baseline",
        default=None,
        help="override the baseline cell label (default: matrix baseline / first cell)",
    )
    p.add_argument("--json", action="store_true", help="emit the sweep result as one JSON line")
    return p


def main() -> int:
    p = argparse.ArgumentParser(
        description="SWEEP — model x knob comparative grid for the eval framework (laptop-only)."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser(
        "run", help="run/grade a model x knob matrix and emit a comparative scorecard table"
    )
    build_sweep_parser(pr)
    pr.set_defaults(func=cmd_run)
    args = p.parse_args()
    try:
        return args.func(args)
    except GoldenYamlError as exc:
        print(json.dumps({"error": "golden_yaml_error", "detail": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
