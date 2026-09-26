#!/usr/bin/env python3
"""SCORECARD — paired A/B significance + power statistics for the Okto Neuron eval
framework. Zero marginalia-source imports (black-box contract, same
rule as judge.py / manifest.py / floor).

WHY THIS EXISTS
---------------
A golden / calibration run measures two arms (configs that differ by exactly one
knob — block-neighbours, k, model, prompt). Given each arm's per-question binary
verdict (correct / incorrect) on the SAME questions, this module answers the only
question that matters before anyone ships a knob change:

    "Is arm B actually better than arm A, or is the delta just noise?"

It emits, over the paired questions:
  * the paired contingency (both-right / both-wrong / the two discordant cells),
  * observed accuracy delta (B - A),
  * McNemar EXACT two-sided p (binomial on the discordant pairs),
  * a BOOTSTRAP 95% CI for the delta (resampling questions with replacement),
  * MDE@0.80 — the smallest |delta| this N could detect at 80% power,
  * required-N to detect a 5pt / 10pt delta at the observed discordance,
  * a 5-condition REAL-vs-DIRECTIONAL classification of the delta.

The power-analysis math (Connor 1987 / Miettinen normal approximation for
McNemar) and the McNemar exact test are adapted from a previously validated
calibration runner; the bootstrap CI and the 5-condition rule are the production
hardening added here.
`selftest` re-pins the ported anchors so any drift is caught with no network.

THE 5-CONDITION "REAL IMPROVEMENT" RULE
---------------------------------------
A delta is classified REAL only if ALL FIVE hold; otherwise it is "directional,
not proven" (or "no improvement" when the sign is wrong / zero):

  1. directional      — observed delta has the expected sign (B beats A: delta>0).
  2. significant      — McNemar exact two-sided p < ALPHA (default 0.05).
  3. powered          — |delta| >= MDE@power (the sample can resolve a delta this
                        size; if MDE is undefined the run is under-powered).
  4. ci_excludes_zero — the bootstrap 95% CI for the delta does not straddle 0.
  5. material         — |delta| >= a practical-effect floor (default 5pt), so a
                        statistically-real-but-tiny delta is not oversold.

Any failing condition is named in the verdict so the reader knows WHICH gate the
delta missed (e.g. "directional, not proven — fails: significant, powered").

SUBCOMMANDS / WIRING
--------------------
  scorecard.py scorecard --a A.jsonl --b B.jsonl [--grader proxy|judge] ...
  scorecard.py selftest

Also wired into judge.py as `judge.py scorecard ...` (mirrors `floor` / `manifest`).

ARM FILE FORMAT
---------------
Each arm is a JSONL file of per-question records (the calibration runner's output
shape). A record needs an "id" and a correctness column:
  * proxy grader -> bool field "proxy_correct"
  * judge grader -> bool field "judge_correct"
If the chosen column is absent but a stored "ask" response and a --questions
manifest are supplied, the proxy verdict is graded on the fly from must_contain
(the deterministic all-tokens-present rule), so an arm captured without a baked
verdict can still be scored.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path
from typing import Any

from golden_yaml import GoldenYamlError, load_yaml

# ───────────────────────────── power constants ───────────────────────────────
# Two-sided alpha=0.05, power=0.80 — identical to calibrate.py.
Z_ALPHA = 1.95996
Z_BETA = 0.84162

DEFAULT_ALPHA = 0.05
DEFAULT_MATERIAL_PT = 0.05  # 5pt practical-effect floor for condition (5)
DEFAULT_BOOTSTRAP = 10000
DEFAULT_BOOTSTRAP_SEED = 1234  # fixed -> deterministic CI / reproducible selftest


# ════════════════════════════ ported power math ══════════════════════════════
# Adapted from the validated calibration runner; selftest re-pins these anchors.


def solve_psi(n_d: int) -> float | None:
    """Solve sqrt(n_d)*(psi-0.5) = z_alpha*0.5 + z_beta*sqrt(psi*(1-psi)) for psi
    in (0.5, 1). psi is the probability a discordant pair favours B (vs A) needed
    to reach 80% power at the given number of discordant pairs n_d. Bisection."""
    if n_d <= 0:
        return None

    def f(psi: float) -> float:
        return math.sqrt(n_d) * (psi - 0.5) - (Z_ALPHA * 0.5 + Z_BETA * math.sqrt(psi * (1 - psi)))

    lo, hi = 0.5, 1.0 - 1e-12
    if f(lo) > 0:  # already solvable at the boundary (tiny n_d edge)
        return 0.5
    if f(hi) < 0:  # not enough discordant pairs to ever reach power -> no MDE
        return None
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if f(mid) <= 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def mde_delta(n_d: int, N: int) -> float | None:
    """Minimum detectable |delta| (in proportion of ALL N pairs) at observed n_d."""
    psi = solve_psi(n_d)
    if psi is None or N <= 0:
        return None
    p_disc = n_d / N
    return p_disc * (2.0 * psi - 1.0)


def required_N(delta: float, p_disc: float) -> float | None:
    """Required total N to detect a target |delta| at discordance rate p_disc.

    delta = p_disc*(2*psi-1)  =>  psi = 0.5 + delta/(2*p_disc). Plug into the
    Connor equation, solve closed-form for n_d, then N = n_d / p_disc."""
    if p_disc <= 0 or delta <= 0:
        return None
    psi_t = 0.5 + delta / (2.0 * p_disc)
    if psi_t >= 1.0:
        return None  # delta exceeds what this discordance can ever express
    rhs = Z_ALPHA * 0.5 + Z_BETA * math.sqrt(psi_t * (1 - psi_t))
    n_d = (rhs / (psi_t - 0.5)) ** 2
    return n_d / p_disc


def mcnemar_exact_p(b: int, c: int) -> float:
    """Exact two-sided McNemar via the binomial: condition on n_d=b+c discordant
    pairs, test k=min(b,c) successes against Binom(n_d, 0.5). Two-sided = 2x the
    one-tail tail prob, capped at 1.0. Exact and appropriate for small n_d."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    # cumulative P(X <= k) under Binom(n, 0.5)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2.0**n)
    return min(1.0, 2.0 * tail)


def mcnemar_cc_chi2(b: int, c: int) -> tuple[float | None, float | None]:
    """Continuity-corrected chi-square McNemar: chi2 = (|b-c|-1)^2/(b+c), 1 dof.
    Returns (chi2, p). Cross-check for the exact test on larger n_d."""
    n = b + c
    if n == 0:
        return None, None
    chi2 = (abs(b - c) - 1) ** 2 / n
    # survival of chi-square with 1 dof = erfc(sqrt(chi2/2))
    p = math.erfc(math.sqrt(chi2 / 2.0))
    return chi2, p


# ════════════════════════════ bootstrap 95% CI ═══════════════════════════════
# The production hardening on top of the ported math: a non-parametric CI for the
# paired accuracy delta. Resample the N questions WITH REPLACEMENT, recompute
# delta = mean(B_i - A_i) on each resample, take the 2.5 / 97.5 percentiles.
# Operates on the per-pair difference d_i in {-1, 0, +1} so it is the paired
# bootstrap (preserves the within-question correlation), the correct analogue of
# McNemar's conditioning on discordant pairs.


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolation percentile (q in [0,100]) over a pre-sorted list."""
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (q / 100.0) * (len(sorted_vals) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_vals[int(rank)]
    frac = rank - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def bootstrap_delta_ci(
    diffs: list[int],
    *,
    iters: int = DEFAULT_BOOTSTRAP,
    seed: int = DEFAULT_BOOTSTRAP_SEED,
    conf: float = 0.95,
) -> tuple[float, float]:
    """Paired bootstrap 95% CI for delta = mean(diffs), where each diff is
    (B_i - A_i) in {-1, 0, +1}. Deterministic given (seed, iters). Returns
    (ci_lo, ci_hi) as proportion-of-N deltas."""
    n = len(diffs)
    if n == 0:
        return (0.0, 0.0)
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(iters):
        s = 0
        for _ in range(n):
            s += diffs[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    alpha = (1.0 - conf) / 2.0 * 100.0  # e.g. 2.5
    return (_percentile(means, alpha), _percentile(means, 100.0 - alpha))


# ════════════════════════════ arm loading / grading ══════════════════════════


def proxy_correct_from_ask(must_contain: list[Any], ask_resp: dict[str, Any]) -> bool:
    """Deterministic must_contain proxy (ported from calibrate.proxy_correct):
    correct iff EVERY token appears (case-insensitively) in the answer text.
    Holds uniformly including negative controls (their must_contain encodes the
    decline word + the correcting fact)."""
    text = (ask_resp or {}).get("text", "") or ""
    low = text.lower()
    tokens = [str(t) for t in (must_contain or [])]
    if not tokens:
        return False
    return all(tok.lower() in low for tok in tokens)


def _load_questions_must_contain(path: Path) -> dict[str, list[Any]]:
    """Return the deterministic proxy tokens from authoritative YAML."""

    doc = load_yaml(path)
    return {
        str(question.get("id")): list(question.get("must_contain") or [])
        for question in (doc.get("questions") or [])
    }


def load_arm(path: Path, *, grader: str, questions: Path | None = None) -> dict[str, bool]:
    """Return {qid: correct_bool} for an arm using the chosen grader column.

    Falls back to grading the stored `ask` response against must_contain (from
    --questions) when the baked column is missing and grader == 'proxy'."""
    if not path.exists():
        raise SystemExit(f"no verdict file for arm: {path}")
    col = "judge_correct" if grader == "judge" else "proxy_correct"
    mc_map: dict[str, list[Any]] | None = None
    out: dict[str, bool] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        qid = str(rec.get("id"))
        if col in rec:
            out[qid] = bool(rec[col])
            continue
        if grader == "proxy" and questions is not None:
            if mc_map is None:
                mc_map = _load_questions_must_contain(questions)
            if qid in mc_map and "ask" in rec:
                out[qid] = proxy_correct_from_ask(mc_map[qid], rec["ask"])
    if not out:
        raise SystemExit(
            f"arm {path.name!r} yielded no '{col}' verdicts; "
            f"run it with the matching grader or pass --questions for on-the-fly proxy grading"
        )
    return out


# ════════════════════════════ the comparison core ════════════════════════════
# Returns a plain dict so both the CLI renderer AND the sweep (Deliverable 2)
# consume one source of truth.


def compare_arms(
    a: dict[str, bool],
    b: dict[str, bool],
    *,
    alpha: float = DEFAULT_ALPHA,
    material_pt: float = DEFAULT_MATERIAL_PT,
    bootstrap_iters: int = DEFAULT_BOOTSTRAP,
    bootstrap_seed: int = DEFAULT_BOOTSTRAP_SEED,
) -> dict[str, Any]:
    paired_ids = sorted(set(a) & set(b))
    if not paired_ids:
        raise SystemExit("no overlapping question ids between the two arms")

    n00 = n01 = n10 = n11 = 0  # (A,B): ww / wr / rw / rr
    discord_b: list[str] = []  # A wrong, B right
    discord_c: list[str] = []  # A right, B wrong
    diffs: list[int] = []  # (B_i - A_i) in {-1,0,+1} for the paired bootstrap
    for qid in paired_ids:
        ra, rb = a[qid], b[qid]
        diffs.append(int(rb) - int(ra))
        if ra and rb:
            n11 += 1
        elif (not ra) and (not rb):
            n00 += 1
        elif (not ra) and rb:
            n01 += 1
            discord_b.append(qid)
        else:
            n10 += 1
            discord_c.append(qid)

    N = len(paired_ids)
    bb, cc = n01, n10
    n_d = bb + cc
    concordant = n00 + n11
    p_disc = n_d / N if N else 0.0
    delta = (bb - cc) / N if N else 0.0  # observed (B - A) accuracy delta

    exact_p = mcnemar_exact_p(bb, cc)
    chi2, chi2_p = mcnemar_cc_chi2(bb, cc)
    mde = mde_delta(n_d, N)
    need_5 = required_N(0.05, p_disc) if p_disc > 0 else None
    need_10 = required_N(0.10, p_disc) if p_disc > 0 else None
    ci_lo, ci_hi = bootstrap_delta_ci(diffs, iters=bootstrap_iters, seed=bootstrap_seed)

    acc_a = sum(a[q] for q in paired_ids) / N
    acc_b = sum(b[q] for q in paired_ids) / N

    # ── the 5-condition REAL rule ──
    c1_directional = delta > 0.0
    c2_significant = exact_p < alpha
    c3_powered = (mde is not None) and (abs(delta) >= mde)
    c4_ci_excludes_zero = (ci_lo > 0.0) or (ci_hi < 0.0)
    c5_material = abs(delta) >= material_pt
    conditions = {
        "directional": c1_directional,
        "significant": c2_significant,
        "powered": c3_powered,
        "ci_excludes_zero": c4_ci_excludes_zero,
        "material": c5_material,
    }
    real = all(conditions.values())
    failed = [name for name, ok in conditions.items() if not ok]
    if real:
        classification = "REAL"
    elif delta <= 0.0:
        classification = "no improvement"
    else:
        classification = "directional, not proven"

    # ── AUTO-ESCALATE recommendation ──
    # The screening set can RESOLVE the observed delta only when it is powered
    # (|delta| >= MDE at this discordance). When a directional delta is under-
    # powered (|delta| < MDE, the common "looks promising but the screen is too
    # small" case), recommend re-running on a larger CONFIRMATION set sized to
    # detect a material effect. Target N = required_N for the observed |delta| if
    # it is itself material; otherwise size for the canonical material floor (5pt
    # default) — both via the same Connor math the rest of the scorecard uses, so
    # e.g. a 10pt target at 20% discordance reproduces the familiar N≈145.
    escalate = bool(c1_directional and not c3_powered)
    confirm_target_delta = abs(delta) if abs(delta) >= material_pt else material_pt
    confirm_n = required_N(confirm_target_delta, p_disc) if p_disc > 0 else None
    if escalate:
        if confirm_n is not None:
            escalation_reason = (
                f"delta {delta * 100:+.1f}pt is directional but UNDER-POWERED at "
                f"this screen (|delta| < MDE {_pct(mde).strip()}); "
                f"ESCALATE to a confirmation set of N≈{round(confirm_n)} "
                f"to detect a {confirm_target_delta * 100:.0f}pt effect at the "
                f"observed {p_disc * 100:.0f}% discordance"
            )
        else:
            escalation_reason = (
                f"delta {delta * 100:+.1f}pt is directional but the screen has too "
                f"few discordant pairs (n_d={n_d}) to ever resolve it; ESCALATE to a "
                f"larger confirmation set before trusting the sign"
            )
    elif c3_powered and real:
        escalation_reason = "no escalation: delta is REAL and powered on this screen"
    elif delta <= 0.0:
        escalation_reason = "no escalation: B does not beat A (wrong sign / zero delta)"
    else:
        escalation_reason = "no escalation: delta is powered on this screen"

    return {
        "N": N,
        "acc_a": acc_a,
        "acc_b": acc_b,
        "correct_a": sum(a[q] for q in paired_ids),
        "correct_b": sum(b[q] for q in paired_ids),
        "n00": n00,
        "n01": n01,
        "n10": n10,
        "n11": n11,
        "discord_b": discord_b,
        "discord_c": discord_c,
        "concordant": concordant,
        "n_d": n_d,
        "p_disc": p_disc,
        "delta": delta,
        "exact_p": exact_p,
        "chi2": chi2,
        "chi2_p": chi2_p,
        "mde": mde,
        "need_5": need_5,
        "need_10": need_10,
        "ci_lo": ci_lo,
        "ci_hi": ci_hi,
        "alpha": alpha,
        "material_pt": material_pt,
        "conditions": conditions,
        "failed": failed,
        "real": real,
        "classification": classification,
        "escalate": escalate,
        "confirm_n": confirm_n,
        "confirm_target_delta": confirm_target_delta,
        "escalation_reason": escalation_reason,
    }


# ════════════════════════════ rendering ══════════════════════════════════════


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:5.1f}pt"


def _num(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1f}"


def render_scorecard(res: dict[str, Any], *, label_a: str, label_b: str, grader: str) -> str:
    N = res["N"]
    lines: list[str] = []
    lines.append("=" * 66)
    lines.append(f"  A/B SCORECARD   grader={grader}")
    lines.append(f"  arm A = {label_a!r}   arm B = {label_b!r}")
    lines.append("=" * 66)
    lines.append(f"  paired questions N ............... {N}")
    lines.append(
        f"  accuracy A ...................... {res['acc_a'] * 100:5.1f}%  ({res['correct_a']}/{N})"
    )
    lines.append(
        f"  accuracy B ...................... {res['acc_b'] * 100:5.1f}%  ({res['correct_b']}/{N})"
    )
    lines.append("  ---- paired contingency (A x B) ----")
    lines.append(f"    both right (n11) .............. {res['n11']}")
    lines.append(f"    both wrong (n00) .............. {res['n00']}")
    lines.append(
        f"    A wrong / B right  b (n01) .... {res['n01']}   {res['discord_b'] if res['discord_b'] else ''}"
    )
    lines.append(
        f"    A right / B wrong  c (n10) .... {res['n10']}   {res['discord_c'] if res['discord_c'] else ''}"
    )
    lines.append("  ---- discordance ----")
    lines.append(f"    concordant ..................... {res['concordant']}")
    lines.append(f"    discordant n_d = b+c ........... {res['n_d']}")
    lines.append(f"    p_disc = n_d/N ................. {res['p_disc'] * 100:5.1f}%")
    lines.append(f"    observed delta = (b-c)/N ....... {res['delta'] * 100:+5.1f}pt  (B - A)")
    lines.append("  ---- McNemar (is the delta real?) ----")
    lines.append(f"    exact binomial two-sided p ..... {res['exact_p']:.4f}")
    chi2_p_txt = "n/a" if res["chi2_p"] is None else f"{res['chi2_p']:.4f}"
    lines.append(f"    cc chi-square (1 dof) .......... chi2={_num(res['chi2'])}  p={chi2_p_txt}")
    lines.append("  ---- bootstrap 95% CI for delta (paired, 10k resamples) ----")
    lines.append(
        f"    delta 95% CI ................... [{res['ci_lo'] * 100:+5.1f}pt, {res['ci_hi'] * 100:+5.1f}pt]"
    )
    lines.append("  ---- power @ observed discordance (a=0.05, power=0.80) ----")
    lines.append(f"    MDE at this n_d ................ {_pct(res['mde'])}")
    lines.append(f"    N needed to detect  5pt ........ {_num(res['need_5'])}")
    lines.append(f"    N needed to detect 10pt ........ {_num(res['need_10'])}")
    lines.append("  ---- 5-condition REAL-improvement rule ----")
    order = ["directional", "significant", "powered", "ci_excludes_zero", "material"]
    for name in order:
        ok = res["conditions"][name]
        lines.append(f"    [{'x' if ok else ' '}] {name}")
    lines.append("=" * 66)
    if res["classification"] == "REAL":
        lines.append(
            f"  VERDICT: REAL improvement  (delta {res['delta'] * 100:+.1f}pt, "
            f"McNemar p={res['exact_p']:.4f}, all 5 conditions met)"
        )
    elif res["classification"] == "no improvement":
        lines.append(
            f"  VERDICT: NO improvement  (delta {res['delta'] * 100:+.1f}pt; B does not beat A)"
        )
        lines.append(f"           unmet: {', '.join(res['failed'])}")
    else:
        lines.append(
            f"  VERDICT: DIRECTIONAL, NOT PROVEN  (delta {res['delta'] * 100:+.1f}pt "
            f"< MDE {_pct(res['mde']).strip()})"
        )
        lines.append(f"           unmet conditions: {', '.join(res['failed'])}")
    # AUTO-ESCALATE — when a directional delta is under-powered on the screen.
    if res.get("escalate"):
        confirm_n = res.get("confirm_n")
        n_txt = f"N≈{round(confirm_n)}" if confirm_n is not None else "a larger N"
        lines.append("  ---- escalation ----")
        lines.append(f"  >> ESCALATE to confirmation set ({n_txt})")
        lines.append(f"     {res.get('escalation_reason', '')}")
    lines.append("=" * 66)
    return "\n".join(lines)


# ════════════════════════════ MULTI-SEED variance band ═══════════════════════
# Run the comparison N times and report the per-metric variance band as
# median[IQR] (the robust, distribution-free band — median plus the 25th/75th
# percentiles, which is what to quote when an eval metric is stochastic and you
# want "where does it land and how wide is the spread" without assuming
# normality).
#
# TWO ways to get N samples, both supported (a run is one paired comparison):
#   * --runs runs.json  : N independent grader/judge runs, each a {"a","b"} pair
#     of verdict files (the real multi-seed: re-grading the SAME questions N times
#     under a stochastic judge yields N verdict sets -> N scorecards). This is the
#     authoritative mode for "how much does the JUDGE wobble run-to-run".
#   * --a/--b + --seeds N : one verdict pair re-scored across N bootstrap-CI seeds.
#     The verdicts are fixed, so delta / accuracy / McNemar p / MDE are CONSTANT
#     (band width 0) — only the bootstrap CI bounds move. This isolates and
#     reports the Monte-Carlo CI variance, and proves the band machinery on real
#     arms without inventing verdicts.


def _quartiles(vals: list[float]) -> dict[str, float]:
    """median + IQR (q1, q3) over a list, via the same linear-interp percentile
    used for the bootstrap CI. NaNs/Nones must be filtered by the caller."""
    s = sorted(vals)
    return {
        "median": _percentile(s, 50.0),
        "q1": _percentile(s, 25.0),
        "q3": _percentile(s, 75.0),
        "min": s[0] if s else float("nan"),
        "max": s[-1] if s else float("nan"),
        "n": len(s),
    }


# The scalar scorecard metrics worth a variance band (skip list/None-valued ones).
_BAND_METRICS = (
    "delta",
    "acc_a",
    "acc_b",
    "exact_p",
    "mde",
    "ci_lo",
    "ci_hi",
    "confirm_n",
    "p_disc",
)


def multiseed_bands(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate N scorecard result-dicts into per-metric median[IQR] bands."""
    bands: dict[str, Any] = {}
    for m in _BAND_METRICS:
        vals = [
            r[m]
            for r in results
            if r.get(m) is not None and not (isinstance(r[m], float) and math.isnan(r[m]))
        ]
        bands[m] = _quartiles([float(v) for v in vals]) if vals else None
    # escalation is a vote across runs (how often the screen says "escalate")
    esc_votes = sum(1 for r in results if r.get("escalate"))
    real_votes = sum(1 for r in results if r.get("real"))
    return {
        "runs": len(results),
        "bands": bands,
        "escalate_votes": esc_votes,
        "escalate_fraction": round(esc_votes / len(results), 4) if results else None,
        "real_votes": real_votes,
        "classifications": sorted({r["classification"] for r in results}),
    }


def _fmt_band_pct(b: dict[str, Any] | None) -> str:
    if not b:
        return "n/a"
    return (
        f"{b['median'] * 100:+6.2f}pt  [IQR {b['q1'] * 100:+6.2f}, "
        f"{b['q3'] * 100:+6.2f}]  (min {b['min'] * 100:+.2f}, max {b['max'] * 100:+.2f})"
    )


def _fmt_band_raw(b: dict[str, Any] | None, scale: float = 1.0) -> str:
    if not b:
        return "n/a"
    return (
        f"{b['median'] * scale:8.4f}  [IQR {b['q1'] * scale:.4f}, "
        f"{b['q3'] * scale:.4f}]  (min {b['min'] * scale:.4f}, max {b['max'] * scale:.4f})"
    )


def render_multiseed(
    agg: dict[str, Any], *, label_a: str, label_b: str, grader: str, mode: str
) -> str:
    b = agg["bands"]
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"  MULTI-SEED SCORECARD   grader={grader}  runs={agg['runs']}  mode={mode}")
    lines.append(f"  arm A = {label_a!r}   arm B = {label_b!r}")
    lines.append("  per-metric variance band = median [IQR q1, q3]")
    lines.append("=" * 72)
    lines.append(f"  accuracy A ............ {_fmt_band_pct(b.get('acc_a'))}")
    lines.append(f"  accuracy B ............ {_fmt_band_pct(b.get('acc_b'))}")
    lines.append(f"  delta (B - A) ........ {_fmt_band_pct(b.get('delta'))}")
    lines.append(f"  discordance p_disc ... {_fmt_band_pct(b.get('p_disc'))}")
    lines.append(f"  McNemar exact p ...... {_fmt_band_raw(b.get('exact_p'))}")
    lines.append(f"  MDE @ n_d ............ {_fmt_band_pct(b.get('mde'))}")
    lines.append(f"  bootstrap CI lo ...... {_fmt_band_pct(b.get('ci_lo'))}")
    lines.append(f"  bootstrap CI hi ...... {_fmt_band_pct(b.get('ci_hi'))}")
    confirm = b.get("confirm_n")
    if confirm:
        lines.append(f"  confirm-set N ........ {_fmt_band_raw(confirm)}")
    lines.append("-" * 72)
    lines.append(f"  REAL votes ........... {agg['real_votes']}/{agg['runs']}")
    lines.append(
        f"  ESCALATE votes ....... {agg['escalate_votes']}/{agg['runs']}  "
        f"({(agg['escalate_fraction'] or 0) * 100:.0f}% of runs say escalate)"
    )
    lines.append(f"  classifications seen . {', '.join(agg['classifications'])}")
    lines.append("=" * 72)
    return "\n".join(lines)


def cmd_multiseed(args: argparse.Namespace) -> int:
    qpath = Path(args.questions).expanduser() if args.questions else None
    results: list[dict[str, Any]] = []
    mode: str
    label_a = args.label_a or "A"
    label_b = args.label_b or "B"

    if args.runs:
        # N independent runs, each a {"a": fileA, "b": fileB} verdict pair.
        runs = json.loads(Path(args.runs).expanduser().read_text(encoding="utf-8"))
        if not isinstance(runs, list) or not runs:
            raise SystemExit("--runs must be a non-empty JSON list of {a,b} pairs")
        mode = f"N-runs ({len(runs)} verdict pairs)"
        for i, r in enumerate(runs):
            fa = Path(str(r["a"])).expanduser()
            fb = Path(str(r["b"])).expanduser()
            a = load_arm(fa, grader=args.grader, questions=qpath)
            b = load_arm(fb, grader=args.grader, questions=qpath)
            results.append(
                compare_arms(
                    a,
                    b,
                    alpha=args.alpha,
                    material_pt=args.material_pt,
                    bootstrap_iters=args.bootstrap,
                    bootstrap_seed=args.bootstrap_seed + i,
                )
            )
        label_a = args.label_a or Path(str(runs[0]["a"])).stem
        label_b = args.label_b or Path(str(runs[0]["b"])).stem
    else:
        # One pair re-scored across N bootstrap seeds (CI Monte-Carlo variance).
        if not (args.a and args.b):
            raise SystemExit("provide either --runs, or both --a and --b with --seeds N")
        mode = f"{args.seeds} bootstrap seeds (fixed verdicts)"
        a = load_arm(Path(args.a).expanduser(), grader=args.grader, questions=qpath)
        b = load_arm(Path(args.b).expanduser(), grader=args.grader, questions=qpath)
        for i in range(args.seeds):
            results.append(
                compare_arms(
                    a,
                    b,
                    alpha=args.alpha,
                    material_pt=args.material_pt,
                    bootstrap_iters=args.bootstrap,
                    bootstrap_seed=args.bootstrap_seed + i,
                )
            )
        label_a = args.label_a or Path(args.a).stem
        label_b = args.label_b or Path(args.b).stem

    agg = multiseed_bands(results)
    if args.json:
        print(
            json.dumps(
                {
                    "label_a": label_a,
                    "label_b": label_b,
                    "grader": args.grader,
                    "mode": mode,
                    **agg,
                },
                ensure_ascii=False,
            )
        )
    else:
        print(
            render_multiseed(agg, label_a=label_a, label_b=label_b, grader=args.grader, mode=mode)
        )
    return 0


# ════════════════════════════ scorecard subcommand ═══════════════════════════


def cmd_scorecard(args: argparse.Namespace) -> int:
    qpath = Path(args.questions).expanduser() if args.questions else None
    a = load_arm(Path(args.a).expanduser(), grader=args.grader, questions=qpath)
    b = load_arm(Path(args.b).expanduser(), grader=args.grader, questions=qpath)
    res = compare_arms(
        a,
        b,
        alpha=args.alpha,
        material_pt=args.material_pt,
        bootstrap_iters=args.bootstrap,
        bootstrap_seed=args.bootstrap_seed,
    )
    label_a = args.label_a or Path(args.a).stem
    label_b = args.label_b or Path(args.b).stem
    if args.json:
        print(
            json.dumps(
                {"label_a": label_a, "label_b": label_b, "grader": args.grader, **res},
                ensure_ascii=False,
            )
        )
    else:
        print(render_scorecard(res, label_a=label_a, label_b=label_b, grader=args.grader))
    return 0


# ════════════════════════════ selftest subcommand ════════════════════════════


def _approx(got: float | None, want: float, tol: float, label: str) -> bool:
    if got is None:
        print(f"  FAIL {label}: got None, want ~{want}")
        return False
    ok = abs(got - want) <= tol
    print(f"  {'ok  ' if ok else 'FAIL'} {label}: got {got:.4f}  want ~{want}  (tol {tol})")
    return ok


def _synth_arms(n11: int, n00: int, n01: int, n10: int) -> tuple[dict[str, bool], dict[str, bool]]:
    """Build two {qid: bool} verdict maps that realise a target paired contingency
    (both-right / both-wrong / A-wrong-B-right / A-right-B-wrong). Used by selftest
    to drive compare_arms from known cell counts."""
    a: dict[str, bool] = {}
    b: dict[str, bool] = {}
    i = 0
    for ra, rb, count in (
        (True, True, n11),
        (False, False, n00),
        (False, True, n01),
        (True, False, n10),
    ):
        for _ in range(count):
            a[f"q{i}"] = ra
            b[f"q{i}"] = rb
            i += 1
    return a, b


def cmd_selftest(_args: argparse.Namespace) -> int:
    print("SELFTEST — scorecard stats vs hand-computed anchors")
    ok = True

    # ── ported power-math anchors (identical to calibrate.py selftest) ──
    ok &= _approx(mde_delta(12, 60), 0.146, 0.003, "MDE @ N=60,p_disc=0.20 (n_d=12)")
    ok &= _approx(required_N(0.10, 0.20), 145.0, 1.5, "required N @ 10pt, disc=0.20")
    N10 = required_N(0.10, 0.20)
    if N10:
        n_d10 = round(N10 * 0.20)
        ok &= _approx(mde_delta(n_d10, round(N10)), 0.10, 0.006, "round-trip MDE @ that N")
    ok &= _approx(required_N(0.05, 0.20), 616.0, 3.0, "required N @ 5pt, disc=0.20")

    n_d20 = required_N(0.10, 0.20)
    n_d40 = required_N(0.10, 0.40)
    mono_ok = n_d20 is not None and n_d40 is not None and n_d40 > n_d20
    print(
        f"  {'ok  ' if mono_ok else 'FAIL'} monotonicity: "
        f"N(10pt,disc=.40)={_num(n_d40)} > N(10pt,disc=.20)={_num(n_d20)}"
    )
    ok &= mono_ok

    ok &= _approx(mcnemar_exact_p(5, 5), 1.0, 1e-9, "McNemar exact p (b=c=5)")
    # b=8,c=1: 2*(C(9,0)+C(9,1))/2^9 = 2*10/512 = 0.0390625
    ok &= _approx(mcnemar_exact_p(8, 1), 0.0390625, 1e-6, "McNemar exact p (b=8,c=1)")

    none_ok = mde_delta(1, 60) is None
    print(f"  {'ok  ' if none_ok else 'FAIL'} MDE undefined at n_d=1 (too few discordant pairs)")
    ok &= none_ok

    # ── bootstrap CI anchors (the production hardening) ──
    # All-zero diffs -> degenerate CI exactly [0,0].
    lo0, hi0 = bootstrap_delta_ci([0, 0, 0, 0, 0])
    z_ok = abs(lo0) < 1e-12 and abs(hi0) < 1e-12
    print(
        f"  {'ok  ' if z_ok else 'FAIL'} bootstrap CI all-concordant -> [0,0]  (got [{lo0:.4f},{hi0:.4f}])"
    )
    ok &= z_ok

    # Determinism: same seed -> identical CI on a mixed diff vector.
    mixed = [1, 0, -1, 1, 1, 0, 0, -1, 1, 0]
    ci_x1 = bootstrap_delta_ci(mixed, seed=7, iters=2000)
    ci_x2 = bootstrap_delta_ci(mixed, seed=7, iters=2000)
    det_ok = ci_x1 == ci_x2
    print(
        f"  {'ok  ' if det_ok else 'FAIL'} bootstrap CI deterministic for a fixed seed  (got {ci_x1})"
    )
    ok &= det_ok

    # CI contains the point estimate (mean diff) for an overwhelmingly-positive vector.
    pos = [1] * 18 + [0, -1]  # mean = +0.80
    lo_p, hi_p = bootstrap_delta_ci(pos, seed=99, iters=4000)
    contains_ok = lo_p <= 0.80 <= hi_p and lo_p > 0.0
    print(
        f"  {'ok  ' if contains_ok else 'FAIL'} bootstrap CI brackets the +0.80 mean & excludes 0  "
        f"(got [{lo_p:.3f},{hi_p:.3f}])"
    )
    ok &= contains_ok

    # ── full 5-condition rule, reproducing the REAL calibration verdict ──
    # neighbors0/1 contingency: n11=51, n00=27, b(n01)=4, c(n10)=1, N=83.
    # delta=+3.6pt, McNemar exact p=0.375, MDE=5.8pt -> DIRECTIONAL, NOT PROVEN.
    a_syn, b_syn = _synth_arms(n11=51, n00=27, n01=4, n10=1)
    res = compare_arms(a_syn, b_syn)
    calib_ok = True
    calib_ok &= _approx(res["delta"], 0.0361, 0.001, "calib delta = +3.6pt")
    calib_ok &= _approx(res["exact_p"], 0.375, 0.001, "calib McNemar exact p = 0.375")
    calib_ok &= _approx(res["mde"], 0.058, 0.002, "calib MDE = 5.8pt")
    verdict_ok = res["classification"] == "directional, not proven"
    print(
        f"  {'ok  ' if verdict_ok else 'FAIL'} calib verdict == 'directional, not proven'  "
        f"(got {res['classification']!r}; unmet={res['failed']})"
    )
    calib_ok &= verdict_ok
    ok &= calib_ok

    # A clearly-REAL synthetic case: big lopsided discordance passes all 5.
    a_big, b_big = _synth_arms(n11=120, n00=40, n01=30, n10=5)
    res_big = compare_arms(a_big, b_big)
    real_ok = (res_big["classification"] == "REAL") and res_big["real"]
    print(
        f"  {'ok  ' if real_ok else 'FAIL'} lopsided synthetic -> REAL  "
        f"(delta {res_big['delta'] * 100:+.1f}pt, p={res_big['exact_p']:.4g}, "
        f"CI=[{res_big['ci_lo'] * 100:+.1f},{res_big['ci_hi'] * 100:+.1f}]pt)"
    )
    ok &= real_ok

    # ── AUTO-ESCALATE (Deliverable C-ii) ──
    # The calib case is directional (+3.6pt) but under-powered (< MDE 5.8pt) ->
    # must recommend escalation; target N is sized for the 5pt material floor at
    # the observed discordance (p_disc=5/83). required_N(0.05, 5/83) ~ 192.
    esc_ok = res["escalate"] is True and res["confirm_n"] is not None
    print(
        f"  {'ok  ' if esc_ok else 'FAIL'} calib delta escalates  "
        f"(escalate={res['escalate']}, confirm_n~{_num(res['confirm_n'])}, "
        f"target={res['confirm_target_delta'] * 100:.0f}pt)"
    )
    ok &= esc_ok
    # The REAL, powered case must NOT escalate.
    no_esc_ok = res_big["escalate"] is False
    print(
        f"  {'ok  ' if no_esc_ok else 'FAIL'} REAL+powered case does NOT escalate  "
        f"(escalate={res_big['escalate']})"
    )
    ok &= no_esc_ok
    # Canonical anchor: a 10pt target at 20% discordance reproduces N≈145
    # (the confirmation-set size the deliverable names).
    n145 = required_N(0.10, 0.20)
    ok &= _approx(n145, 145.0, 1.5, "confirm-set N≈145 @ 10pt, disc=0.20")

    # ── MULTI-SEED variance band (Deliverable C-i) ──
    # Re-score the SAME calib arms across 8 bootstrap seeds: verdict-derived
    # metrics (delta, p, MDE) are CONSTANT (band width 0); only the bootstrap CI
    # bounds move a little. This pins both the band shape and the determinism.
    a_ms, b_ms = _synth_arms(n11=51, n00=27, n01=4, n10=1)
    runs = [
        compare_arms(a_ms, b_ms, bootstrap_iters=2000, bootstrap_seed=1000 + i) for i in range(8)
    ]
    agg = multiseed_bands(runs)
    db = agg["bands"]["delta"]
    ci_band = agg["bands"]["ci_lo"]
    # These synthetic runs always populate every band; assert so for type-narrowing
    # (and to fail loudly if the band shape ever regresses).
    assert db is not None and ci_band is not None, "synthetic runs must populate bands"
    delta_const_ok = abs(db["q3"] - db["q1"]) < 1e-12 and abs(db["median"] - 0.0361) < 1e-3
    print(
        f"  {'ok  ' if delta_const_ok else 'FAIL'} multi-seed delta band is a point "
        f"(verdicts fixed): median={db['median'] * 100:+.2f}pt IQR width≈0"
    )
    ok &= delta_const_ok
    ci_moves_ok = ci_band["max"] >= ci_band["min"]
    print(
        f"  {'ok  ' if ci_moves_ok else 'FAIL'} multi-seed bootstrap-CI band well-formed  "
        f"(ci_lo median={ci_band['median'] * 100:+.2f}pt, "
        f"spread {(ci_band['max'] - ci_band['min']) * 100:.2f}pt)"
    )
    ok &= ci_moves_ok
    esc_vote_ok = agg["escalate_votes"] == 8  # all 8 runs agree: escalate
    print(
        f"  {'ok  ' if esc_vote_ok else 'FAIL'} multi-seed escalate vote unanimous  "
        f"({agg['escalate_votes']}/8 say escalate)"
    )
    ok &= esc_vote_ok
    # determinism: same seeds -> identical band
    runs2 = [
        compare_arms(a_ms, b_ms, bootstrap_iters=2000, bootstrap_seed=1000 + i) for i in range(8)
    ]
    det_band_ok = multiseed_bands(runs2)["bands"]["ci_lo"] == ci_band
    print(f"  {'ok  ' if det_band_ok else 'FAIL'} multi-seed band deterministic for fixed seeds")
    ok &= det_band_ok

    # ── AB-RUNSET (GN-3): synthetic 3-run pre-captured mode ──
    # Build 3 synthetic arm pairs with slightly different contingencies so the
    # band has non-trivial width. All are directional (B beats A) but under-
    # powered -> escalate=True for each run.
    import argparse as _ap
    import json as _json
    import tempfile as _tmp
    import os as _os

    arm_configs = [
        (51, 27, 4, 1),  # run 0: calib contingency, delta=+3.6pt
        (50, 28, 5, 1),  # run 1: slightly more discordance, delta=+4.8pt
        (52, 26, 3, 1),  # run 2: slightly less discordance, delta=+2.4pt
    ]
    with _tmp.TemporaryDirectory(prefix="ab_runset_selftest_") as td:
        block_paths: list[str] = []
        subgraph_paths: list[str] = []
        for ri, (n11, n00, n01, n10) in enumerate(arm_configs):
            a_v, b_v = _synth_arms(n11, n00, n01, n10)
            # Write as JSONL with proxy_correct column
            ba_path = _os.path.join(td, f"block_{ri}.jsonl")
            sg_path = _os.path.join(td, f"subgraph_{ri}.jsonl")
            with open(ba_path, "w") as f:
                for qid, val in a_v.items():
                    f.write(_json.dumps({"id": qid, "proxy_correct": val}) + "\n")
            with open(sg_path, "w") as f:
                for qid, val in b_v.items():
                    f.write(_json.dumps({"id": qid, "proxy_correct": val}) + "\n")
            block_paths.append(ba_path)
            subgraph_paths.append(sg_path)

        cfg_json = _json.dumps({"block_arm": block_paths, "subgraph_arm": subgraph_paths})
        ns = _ap.Namespace(
            config=cfg_json,
            grader="proxy",
            questions=None,
            alpha=DEFAULT_ALPHA,
            material_pt=DEFAULT_MATERIAL_PT,
            bootstrap=2000,
            bootstrap_seed=DEFAULT_BOOTSTRAP_SEED,
            json=True,
        )
        import io as _io
        from contextlib import redirect_stdout as _rso

        buf = _io.StringIO()
        with _rso(buf):
            rc = cmd_ab_runset(ns)
        raw_out = buf.getvalue().strip()

    ab_rc_ok = rc == 0
    print(f"  {'ok  ' if ab_rc_ok else 'FAIL'} ab-runset cmd returns 0")
    ok &= ab_rc_ok

    try:
        ab_result = _json.loads(raw_out)
        ab_shape_ok = (
            "runs" in ab_result
            and "bands" in ab_result
            and "escalate_votes" in ab_result
            and "real_votes" in ab_result
            and len(ab_result["runs"]) == 3
        )
    except (_json.JSONDecodeError, KeyError):
        ab_result = {}
        ab_shape_ok = False
    print(
        f"  {'ok  ' if ab_shape_ok else 'FAIL'} ab-runset JSON shape: "
        f"runs/bands/escalate_votes/real_votes present, 3 runs"
    )
    ok &= ab_shape_ok

    if ab_shape_ok:
        ab_delta = ab_result["bands"].get("delta") or {}
        ab_width_ok = (ab_delta.get("q3", 0) - ab_delta.get("q1", 0)) > 0
        print(
            f"  {'ok  ' if ab_width_ok else 'FAIL'} ab-runset delta band has non-trivial IQR "
            f"(width={(ab_delta.get('q3', 0) - ab_delta.get('q1', 0)) * 100:.2f}pt)"
        )
        ok &= ab_width_ok

        # All 3 runs are directional but under-powered -> all should escalate
        ab_esc_ok = ab_result["escalate_votes"] == 3
        print(
            f"  {'ok  ' if ab_esc_ok else 'FAIL'} ab-runset escalate_votes==3/3 "
            f"(got {ab_result.get('escalate_votes')})"
        )
        ok &= ab_esc_ok

    print("-" * 60)
    print("SELFTEST: PASS" if ok else "SELFTEST: FAIL")
    return 0 if ok else 1


# ════════════════════════════ argparse / wiring ══════════════════════════════


def build_scorecard_parser(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Attach `scorecard` arguments (shared with judge.py's wiring)."""
    p.add_argument("--a", required=True, help="arm A JSONL (per-question verdicts)")
    p.add_argument("--b", required=True, help="arm B JSONL (per-question verdicts)")
    p.add_argument(
        "--grader",
        choices=["judge", "proxy"],
        default="proxy",
        help="verdict column to compare (default proxy)",
    )
    p.add_argument(
        "--questions",
        default=None,
        help="questions.yaml — enables on-the-fly proxy grading when an "
        "arm has no baked proxy_correct column",
    )
    p.add_argument(
        "--alpha",
        type=float,
        default=DEFAULT_ALPHA,
        help="significance level for McNemar (default 0.05)",
    )
    p.add_argument(
        "--material-pt",
        dest="material_pt",
        type=float,
        default=DEFAULT_MATERIAL_PT,
        help="practical-effect floor as a proportion (default 0.05 = 5pt)",
    )
    p.add_argument(
        "--bootstrap",
        type=int,
        default=DEFAULT_BOOTSTRAP,
        help="bootstrap resamples for the delta CI (default 10000)",
    )
    p.add_argument(
        "--bootstrap-seed",
        dest="bootstrap_seed",
        type=int,
        default=DEFAULT_BOOTSTRAP_SEED,
        help="bootstrap RNG seed (deterministic CI)",
    )
    p.add_argument("--label-a", dest="label_a", default=None, help="display label for arm A")
    p.add_argument("--label-b", dest="label_b", default=None, help="display label for arm B")
    p.add_argument("--json", action="store_true", help="emit the scorecard as one JSON line")
    return p


def build_ab_runset_parser(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Attach `ab-runset` arguments.

    GN-3 — multi-run A/B significance harness.  LAPTOP-ONLY fleet instrument;
    NOT wired into CI (eval-gate.yml / recall_floor are untouched).

    Two input modes:

    PRE-CAPTURED (recommended, robust):
        --config '{"block_arm": [...], "subgraph_arm": [...], "N_runs": 3}'
        Each element in block_arm / subgraph_arm is a path to a JSONL verdict
        file for that run.  Must be parallel arrays of equal length.

    To maximise reproducibility, pin the LLM seed and model via the server's
    config (OKTO_NEURON_LLM_SEED / model knob) before running; temp>0 sampling
    gives the variance the multi-run band is designed to measure.

    Output JSON shape:
        {runs: [...scorecards], bands: {...median[IQR]}, escalate_votes: N,
         real_votes: N, classifications: [...]}
    """
    p.add_argument(
        "--config",
        required=True,
        help=('JSON config.  Pre-captured: {"block_arm":[f1,...], "subgraph_arm":[f1,...]}'),
    )
    p.add_argument(
        "--grader",
        choices=["judge", "proxy"],
        default="proxy",
        help="verdict column to grade (default proxy)",
    )
    p.add_argument(
        "--questions",
        default=None,
        help="questions.yaml for on-the-fly proxy grading (pre-captured mode)",
    )
    p.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    p.add_argument("--material-pt", dest="material_pt", type=float, default=DEFAULT_MATERIAL_PT)
    p.add_argument("--bootstrap", type=int, default=DEFAULT_BOOTSTRAP)
    p.add_argument(
        "--bootstrap-seed",
        dest="bootstrap_seed",
        type=int,
        default=DEFAULT_BOOTSTRAP_SEED,
        help="base bootstrap seed; run i uses base+i (deterministic)",
    )
    p.add_argument("--json", action="store_true", help="emit result as one JSON line")
    return p


def cmd_ab_runset(args: argparse.Namespace) -> int:
    """GN-3: multi-run A/B significance harness.

    Reuses compare_arms (single-run stats) and multiseed_bands (aggregate) to
    answer: "across N independent re-runs, is block vs subgraph consistently
    directional, and how wide is the variance band?"
    """
    cfg = json.loads(args.config)
    qpath = Path(args.questions).expanduser() if args.questions else None

    if cfg.get("run_both"):
        raise SystemExit(
            "run_both is not supported; capture both arms with eval-run.sh "
            "--ab-subgraph, then pass the resulting arm files to ab-runset"
        )

    # ── pre-captured mode: parallel JSONL arm arrays ──────────────────────────
    block_files = cfg.get("block_arm") or cfg.get("runs", {}) and None
    subgraph_files = cfg.get("subgraph_arm") or None

    # Also accept {"runs": [{"a": fileA, "b": fileB}, ...]} (multiseed-compatible shape)
    if block_files is None and "runs" in cfg:
        run_pairs = cfg["runs"]
        if not isinstance(run_pairs, list) or not run_pairs:
            raise SystemExit("config.runs must be a non-empty list of {a, b} pairs")
        block_files = [str(r["a"]) for r in run_pairs]
        subgraph_files = [str(r["b"]) for r in run_pairs]

    if not block_files or not subgraph_files:
        raise SystemExit(
            "pre-captured config requires parallel 'block_arm' and 'subgraph_arm' arrays "
            "(or a 'runs' list of {a, b} pairs)"
        )
    if len(block_files) != len(subgraph_files):
        raise SystemExit(
            f"block_arm ({len(block_files)}) and subgraph_arm ({len(subgraph_files)}) "
            "must be the same length"
        )
    N = len(block_files)
    if N < 2:
        raise SystemExit("ab-runset requires at least 2 runs (N >= 2 recommended: N >= 5)")

    results: list[dict[str, Any]] = []
    per_run: list[dict[str, Any]] = []
    for i, (bf, sf) in enumerate(zip(block_files, subgraph_files)):
        fa = Path(bf).expanduser()
        fb = Path(sf).expanduser()
        a = load_arm(fa, grader=args.grader, questions=qpath)
        b = load_arm(fb, grader=args.grader, questions=qpath)
        sc = compare_arms(
            a,
            b,
            alpha=args.alpha,
            material_pt=args.material_pt,
            bootstrap_iters=args.bootstrap,
            bootstrap_seed=args.bootstrap_seed + i,
        )
        results.append(sc)
        per_run.append(
            {
                "run": i,
                "block_arm": str(fa),
                "subgraph_arm": str(fb),
                "N": sc["N"],
                "delta": round(sc["delta"], 6),
                "exact_p": round(sc["exact_p"], 6),
                "mde": round(sc["mde"], 6) if sc.get("mde") is not None else None,
                "ci_lo": round(sc["ci_lo"], 6),
                "ci_hi": round(sc["ci_hi"], 6),
                "real": sc["real"],
                "escalate": sc["escalate"],
                "classification": sc["classification"],
            }
        )

    agg = multiseed_bands(results)
    out = {
        "runs": per_run,
        "bands": agg["bands"],
        "escalate_votes": agg["escalate_votes"],
        "escalate_fraction": agg["escalate_fraction"],
        "real_votes": agg["real_votes"],
        "total_runs": N,
        "classifications": agg["classifications"],
        # Convenience summary: median delta and whether it's directional across runs
        "summary": {
            "median_delta_pt": round((agg["bands"]["delta"]["median"] * 100), 2)
            if agg["bands"].get("delta")
            else None,
            "delta_iqr_width_pt": round(
                (agg["bands"]["delta"]["q3"] - agg["bands"]["delta"]["q1"]) * 100, 2
            )
            if agg["bands"].get("delta")
            else None,
        },
    }

    if args.json:
        print(json.dumps(out, ensure_ascii=False))
    else:
        _render_ab_runset(out)
    return 0


def _render_ab_runset(out: dict[str, Any]) -> None:
    """Human-readable ab-runset summary."""
    N = out["total_runs"]
    db = out["bands"].get("delta")
    print("=" * 72)
    print(f"  AB-RUNSET REPORT   N={N} run(s)  (laptop instrument, NOT CI gate)")
    print("=" * 72)
    if db:
        med_pt = db["median"] * 100
        iqr_w = (db["q3"] - db["q1"]) * 100
        print("  delta (subgraph - block):")
        print(f"    median .............. {med_pt:+.2f}pt")
        print(
            f"    IQR width ........... {iqr_w:.2f}pt  (q1={db['q1'] * 100:+.2f}, q3={db['q3'] * 100:+.2f})"
        )
        print(f"    range ............... [{db['min'] * 100:+.2f}, {db['max'] * 100:+.2f}]pt")
    print(f"  REAL votes ............. {out['real_votes']}/{N}")
    print(
        f"  ESCALATE votes ......... {out['escalate_votes']}/{N}  "
        f"({(out['escalate_fraction'] or 0) * 100:.0f}% say escalate)"
    )
    print(f"  classifications ........ {', '.join(out['classifications'])}")
    print("-" * 72)
    for r in out["runs"]:
        print(
            f"  run {r['run']}: delta={r['delta'] * 100:+.2f}pt  p={r['exact_p']:.4f}  "
            f"[{r['ci_lo'] * 100:+.2f},{r['ci_hi'] * 100:+.2f}]pt  "
            f"{'REAL' if r['real'] else r['classification']}"
        )
    print("=" * 72)


def build_multiseed_parser(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Attach `scorecard-multiseed` arguments (shared with judge.py's wiring).

    Two input shapes: --runs (N independent {a,b} verdict-file pairs, the real
    multi-grader-run band) OR --a/--b with --seeds N (one pair re-scored across N
    bootstrap seeds, isolating CI Monte-Carlo variance)."""
    p.add_argument(
        "--runs",
        default=None,
        help='JSON list of N {"a":fileA,"b":fileB} verdict pairs '
        "(N independent grader runs -> per-metric median[IQR] band)",
    )
    p.add_argument("--a", default=None, help="arm A JSONL (with --b + --seeds, single-pair mode)")
    p.add_argument("--b", default=None, help="arm B JSONL (with --a + --seeds, single-pair mode)")
    p.add_argument(
        "--seeds",
        type=int,
        default=8,
        help="single-pair mode: re-score across this many bootstrap seeds (default 8)",
    )
    p.add_argument(
        "--grader",
        choices=["judge", "proxy"],
        default="proxy",
        help="verdict column to compare (default proxy)",
    )
    p.add_argument(
        "--questions", default=None, help="questions.yaml — enables on-the-fly proxy grading"
    )
    p.add_argument("--alpha", type=float, default=DEFAULT_ALPHA)
    p.add_argument("--material-pt", dest="material_pt", type=float, default=DEFAULT_MATERIAL_PT)
    p.add_argument("--bootstrap", type=int, default=DEFAULT_BOOTSTRAP)
    p.add_argument(
        "--bootstrap-seed",
        dest="bootstrap_seed",
        type=int,
        default=DEFAULT_BOOTSTRAP_SEED,
        help="base bootstrap seed; run/seed i uses base+i (deterministic)",
    )
    p.add_argument("--label-a", dest="label_a", default=None)
    p.add_argument("--label-b", dest="label_b", default=None)
    p.add_argument("--json", action="store_true", help="emit the band aggregate as one JSON line")
    return p


def main() -> int:
    p = argparse.ArgumentParser(
        description=(
            "SCORECARD — paired A/B significance + power for the black-box eval framework."
        )
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    psc = sub.add_parser("scorecard", help="paired A/B significance + power scorecard for two arms")
    build_scorecard_parser(psc)
    psc.set_defaults(func=cmd_scorecard)

    pms = sub.add_parser(
        "scorecard-multiseed", help="N-run per-metric median[IQR] variance band (+ escalate vote)"
    )
    build_multiseed_parser(pms)
    pms.set_defaults(func=cmd_multiseed)

    pab = sub.add_parser(
        "ab-runset",
        help="GN-3: multi-run A/B significance harness — N runs, multiseed_bands aggregate "
        "(laptop instrument, NOT a CI gate)",
    )
    build_ab_runset_parser(pab)
    pab.set_defaults(func=cmd_ab_runset)

    pst = sub.add_parser("selftest", help="validate the stats math (no network)")
    pst.set_defaults(func=cmd_selftest)

    args = p.parse_args()
    try:
        return args.func(args)
    except GoldenYamlError as exc:
        print(json.dumps({"error": "golden_yaml_error", "detail": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
