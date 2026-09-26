#!/usr/bin/env python3
"""JUDGE PANEL — N-judge, disjoint-family, 2/3-vote grading for the Okto Neuron eval.

Zero marginalia-source imports (black-box contract, same rule as
judge.py / scorecard.py / manifest.py). `selftest` re-pins the deterministic math
(6-class collapse, majority vote, Fleiss kappa, ASK×RECALL) with NO network.

WHY THIS EXISTS
---------------
Today a SINGLE local Qwen judge (judge.py) grades every answer. One model grading
alone carries that model's blind spots straight into the verdict — and a model
cannot be trusted to grade its own family (self-enhancement bias). The eval goal
asks for a 3-judge panel drawn from DISJOINT model families, a 2-of-3 majority
vote, and an inter-judge agreement number (Fleiss kappa) that exposes *shared*
bias (when judges all err together, kappa stays high while the panel is wrong —
so kappa is read alongside, never instead of, the human kappa gate).

Each judge emits the eval's structured rubric:
  * correctness  ∈ {0,1,2}   (does it convey the expected facts?)
  * groundedness ∈ {0,1,2}   (is every claim supported by the notes, no fabrication?)
  * completeness ∈ {0,1,2}   (are all key facts present?)
  * hallucination: bool      (did it invent a specific false fact / answer an absent
                              question as if present?)
which is DETERMINISTICALLY collapsed (same code for every judge) to the 6-class
taxonomy C/P/W/M/H/A with precedence H>A>M>W>P>C — the classes the human-kappa
methodology scores against. Doing the collapse in code (not asking the model for a
letter) keeps the mapping identical across families and auditable.

DISJOINT FAMILIES (probed live; see the panel-reachability note in the report)
  * qwen        — explicitly configured OpenAI-compatible Qwen server.
  * claude_cli  — `claude -p "<prompt>" --output-format json` (Anthropic). The
                  envelope is a JSON LIST; the verdict is the element type==result.
  * gemini      — Google Generative Language OpenAI-compatible endpoint, gemini-2.x.
  * glm         — z.ai / Zhipu OpenAI-compatible (glm-4.x). Wired but may be
                  billing-gated; the panel records each judge's reachability and
                  runs on whatever ≥2 disjoint families ARE reachable, flagging the
                  constraint rather than pretending a 2nd same-family judge is
                  disjoint.

BIAS CONTROLS
  (a) no-self-grading — assert every judge's family != the answerer's family; a
      violation is a HARD FAILURE (raises), never a silent demotion. A panel that
      includes the answerer's own family is not a valid panel.
  (b) verbosity       — per item we log answer char/word length next to each
      verdict so a post-hoc regression can test the length→leniency bias.
  (c) position-swap   — implemented as a hook for any PAIRWISE mode (grade A-vs-B
      and B-vs-A, keep only order-invariant verdicts). Reference-based grading
      (what this panel does) has no candidate ordering, so swap is N/A here — but
      the code path exists and is selftested so a future pairwise arm is covered.

WIRING
  panel.py panel  --responses R.jsonl --questions Q.yaml --out panel.json
                  [--judges qwen,claude_cli,gemini] [--answerer-family <fam>]
                  [--limit N] [--qwen-base-url U] [--qwen-model M] ...
  panel.py selftest

`--responses` accepts BOTH the golden responses.jsonl shape (rec["recall"]["hits"])
AND the calib shape (hits under rec["ask"]["hits"]); the loader normalizes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

# ARCHIVED apparatus: this module now lives under bin/archive/, so the harness
# package dir (bin/) must be importable for its shared YAML loader.
_HARNESS_DIR = str(Path(__file__).resolve().parent.parent)
if _HARNESS_DIR not in sys.path:
    sys.path.insert(0, _HARNESS_DIR)

from golden_yaml import GoldenYamlError, load_yaml  # noqa: E402

# ════════════════════════════ 6-class taxonomy ════════════════════════════════
# Mirrors kappa/validation-corpus.yaml and run_judge.map_to_taxonomy. The panel is
# the producer the human kappa scores against, so the classes + precedence MUST be
# byte-identical to that methodology.
CLASSES = ["C", "P", "W", "M", "H", "A"]
PRECEDENCE = "H > A > M > W > P > C"
# Lower rank wins when two classes could apply (the precedence order above).
_PRECEDENCE_RANK = {"H": 0, "A": 1, "M": 2, "W": 3, "P": 4, "C": 5}

# Abstain markers — identical set to kappa/run_judge.py, so "did the system decline"
# is decided the same way the human-label corpus was built.
ABSTAIN_MARKERS = (
    "no information",
    "not in the",
    "no mention",
    "not mentioned",
    "cannot find",
    "no record",
    "do not specify",
    "does not specify",
    "not specify",
    "not contain",
    "unable to",
    "no data",
    "not provided",
    "not available",
    "there is no",
    "not found",
    "do not state",
    "does not state",
    "do not mention",
    "does not mention",
    "no such",
    "not present in",
    "not appear",
    "no relevant",
)


def looks_abstained(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in ABSTAIN_MARKERS)


def collapse_to_class(axes: dict[str, Any], *, negative_control: bool, abstained: bool) -> str:
    """Deterministically map a judge's structured axes -> one 6-class letter.

    axes = {correctness, groundedness, completeness in {0,1,2}, hallucination bool}.
    The SAME function runs for every judge so the collapse is family-independent
    and auditable. Rules, applied in precedence order H>A>M>W>P>C:

      H  hallucinated        — the judge flagged hallucination=true, OR it answered
                               an ABSENT (negative_control) question as if present
                               (did NOT abstain) with non-zero correctness. (A
                               confident made-up answer to an absent question is H,
                               never A.) GUARD: an unambiguous decline (abstained
                               AND correctness==0) asserts no fact, so a stray
                               hallucination=true flag on it is dropped — the rubric
                               itself says an honest "the notes don't say" is NOT a
                               hallucination. Such a decline routes to A or M, not H.
      A  correctly-abstained — negative_control AND the system abstained AND the
                               judge did not flag a fabrication (groundedness>=1).
      M  missed              — not a negative control, the answer is empty/abstains,
                               and correctness==0 (an answer existed but none given).
      W  wrong               — correctness==0 (contradicts / no key fact) and not
                               already H/M.
      P  partial             — correctness==1 (some but not all key facts), or full
                               correctness with a real completeness gap.
      C  correct             — correctness==2 and completeness>=1 and not hallucinated.
    """
    corr = _clip2(axes.get("correctness"))
    ground = _clip2(axes.get("groundedness"))
    comp = _clip2(axes.get("completeness"))
    hallu = bool(axes.get("hallucination"))

    # H — fabrication (explicit flag) or a confident answer to an absent question.
    # GUARD: an unambiguous decline (abstained AND correctness==0) asserts no fact, so
    # by the rubric's own definition ("an honest 'the notes don't say' is NOT a
    # hallucination") it cannot be H even if a judge stray-flagged hallucination=true.
    # Such a decline routes to A (neg-control) or M (answerable) below, never H. Drops
    # ONLY the zero-correctness decline; a mixed answer that abstains AND asserts a
    # fact (corr>=1) still collapses to H.
    if hallu and not (abstained and corr == 0):
        return "H"
    if negative_control and not abstained and corr >= 1:
        # answered an absent question as if it had a real answer
        return "H"
    # A — correct decline on a genuinely absent question.
    if negative_control and abstained and ground >= 1:
        return "A"
    # M — an answer existed (not a negative control) but the system gave none.
    if (not negative_control) and abstained and corr == 0:
        return "M"
    # W — wrong / no key fact conveyed.
    if corr == 0:
        return "W"
    # P — partly right, or fully right but materially incomplete.
    if corr == 1:
        return "P"
    if corr == 2 and comp == 0:
        return "P"
    # C — correct and reasonably complete.
    return "C"


def _clip2(v: Any) -> int:
    """Coerce a judge-emitted axis to an int in {0,1,2}; junk -> 0 (conservative)."""
    try:
        i = int(v)
    except (TypeError, ValueError):
        return 0
    return 0 if i < 0 else 2 if i > 2 else i


# ════════════════════════════ trust wiring (the kappa gate) ════════════════════
# "Wire the judge once it passes." The judge becomes a usable signal ONLY when a
# HUMAN-validated Cohen's-kappa artifact certifies it. This derives that wiring from
# the artifact instead of a hand-edited flag — and is DEFAULT-OFF and HUMAN-ONLY by
# construction, so it cannot silently self-promote:
#   * No artifact (env OKTO_NEURON_JUDGE_TRUST_ARTIFACT unset / missing / malformed)
#     -> deterministic-only. This is the committed + CI state, so behavior is
#     unchanged today and the CI gate stays judge-free.
#   * source != "human" (AI-proxy, synthetic, anything) -> HARD-refused to
#     deterministic-only, regardless of kappa. Enforces "AI-proxy kappa is capped"
#     IN CODE, not just prose — a Codex-proxy artifact can NEVER wire the judge.
#   * Bands match compute_kappa.gate_verdict on the CI LOWER bound (conservative):
#     >=0.80 authoritative (may adjudicate) | >=0.60 advisory/trusted (soft signal,
#     deterministic floor still rules) | <0.60 deterministic-only.
TRUST_ENV = "OKTO_NEURON_JUDGE_TRUST_ARTIFACT"
_AUTHORITATIVE_CI = 0.80
_TRUST_CI = 0.60
# grounded-consensus (LLM, adversarially-verified, NOT human): a STRICTER bar than
# human's 0.60 — it may lift the judge to ADVISORY only at kappa CI-lower >= 0.80,
# and NEVER to authoritative (that remains human-only).
_GROUNDED_TRUST_CI = 0.80


def derive_trust(artifact_path: str | None = None) -> dict[str, Any]:
    """Derive the judge's trust level from a signed HUMAN-validation kappa artifact.

    Returns {trust_level, authoritative, trusted, kappa_ci_lower, source,
    artifact_path, note}. Pure + side-effect-free; reads at most one JSON file.
    Defaults to deterministic-only on ANY of: no path, absent file, parse error,
    non-human source, or a missing/non-numeric kappa_ci_lower.
    """

    def _det_only(
        note: str, *, source: Any = None, ci: float | None = None, path: str | None = None
    ) -> dict[str, Any]:
        return {
            "trust_level": "deterministic-only",
            "authoritative": False,
            "trusted": False,
            "kappa_ci_lower": ci,
            "source": source,
            "artifact_path": path,
            "note": note,
        }

    path = artifact_path or os.environ.get(TRUST_ENV)
    if not path:
        return _det_only(
            "No human-validation trust artifact present (set "
            f"{TRUST_ENV} to a signed HUMAN kappa artifact to wire the judge); "
            "verdicts stay diagnostic-only."
        )
    p = Path(path).expanduser()
    if not p.is_file():
        return _det_only(
            f"Trust artifact path {path!r} does not exist; deterministic-only.", path=str(p)
        )
    try:
        art = json.loads(p.read_text())
    except (OSError, ValueError) as e:
        return _det_only(
            f"Trust artifact {path!r} unreadable/invalid JSON ({e}); deterministic-only.",
            path=str(p),
        )
    source = art.get("source")
    if source not in ("human", "grounded-consensus"):
        # HARD refusal — only blind HUMAN labels or an adversarially-verified
        # GROUNDED-CONSENSUS artifact can wire the judge. A single AI-proxy pass
        # (e.g. raw Codex), synthetic, or unlabeled source never promotes.
        return _det_only(
            f"Trust artifact source is {source!r}, not 'human'/'grounded-consensus' "
            "— capped; deterministic-only.",
            source=source,
            path=str(p),
        )
    ci = art.get("kappa_ci_lower")
    if not isinstance(ci, (int, float)) or isinstance(ci, bool):
        return _det_only(
            f"Trust artifact has no numeric kappa_ci_lower ({ci!r}); deterministic-only.",
            source=source,
            path=str(p),
        )
    ci = float(ci)
    if source == "human":
        if ci >= _AUTHORITATIVE_CI:
            level, auth, trusted = "authoritative", True, True
            note = (
                f"HUMAN-cleared: kappa CI-lower {ci:.4f} >= {_AUTHORITATIVE_CI} "
                "-> AUTHORITATIVE (judge may adjudicate eval runs)."
            )
        elif ci >= _TRUST_CI:
            level, auth, trusted = "advisory", False, True
            note = (
                f"HUMAN-cleared: kappa CI-lower {ci:.4f} in "
                f"[{_TRUST_CI}, {_AUTHORITATIVE_CI}) -> ADVISORY (soft signal; the "
                "deterministic floor still rules the gate)."
            )
        else:
            level, auth, trusted = "deterministic-only", False, False
            note = (
                f"HUMAN artifact but kappa CI-lower {ci:.4f} < {_TRUST_CI} "
                "-> DETERMINISTIC-ONLY (judge not yet trusted)."
            )
    else:  # grounded-consensus: stricter bar, ADVISORY ceiling (never authoritative)
        if ci >= _GROUNDED_TRUST_CI:
            level, auth, trusted = "advisory", False, True
            note = (
                f"GROUNDED-CONSENSUS (LLM, not human): kappa CI-lower {ci:.4f} >= "
                f"{_GROUNDED_TRUST_CI} -> ADVISORY (soft signal only; NEVER "
                "authoritative — that needs human labels; floor still rules)."
            )
        else:
            level, auth, trusted = "deterministic-only", False, False
            note = (
                f"GROUNDED-CONSENSUS artifact but kappa CI-lower {ci:.4f} < "
                f"{_GROUNDED_TRUST_CI} -> DETERMINISTIC-ONLY (judge not trusted)."
            )
    return {
        "trust_level": level,
        "authoritative": auth,
        "trusted": trusted,
        "kappa_ci_lower": ci,
        "source": source,
        "artifact_path": str(p),
        "note": note,
    }


# ════════════════════════════ majority vote ═══════════════════════════════════


def majority_vote(labels: list[str]) -> dict[str, Any]:
    """2/3 (strict-majority) consensus over per-judge 6-class labels.

    consensus = the class held by > half the judges; None when no class has a
    strict majority (a 3-way split, or a 1-1 tie on a 2-judge panel). On an exact
    tie among the top classes the precedence order H>A>M>W>P>C breaks it ONLY for
    reporting `precedence_pick`; `consensus` stays None so the caller can route
    no-majority items to human review rather than silently accept a tie-break."""
    valid = [x for x in labels if x in CLASSES]
    counts: dict[str, int] = {}
    for x in valid:
        counts[x] = counts.get(x, 0) + 1
    n = len(valid)
    consensus: str | None = None
    if n:
        top = max(counts.values())
        leaders = [c for c, k in counts.items() if k == top]
        if top * 2 > n:  # strict majority
            consensus = leaders[0]
        precedence_pick = min(leaders, key=lambda c: _PRECEDENCE_RANK[c])
    else:
        precedence_pick = None
    return {
        "labels": labels,
        "counts": counts,
        "n_valid": n,
        "consensus": consensus,
        "unanimous": bool(n) and len(counts) == 1,
        "majority": consensus is not None,
        "precedence_pick": precedence_pick,
    }


# ════════════════════════════ Fleiss kappa ════════════════════════════════════
# Inter-rater agreement for N raters over a fixed nominal class set. This is the
# SHARED-BIAS signal: high Fleiss kappa = the judges agree with each other (but
# agreement among judges is NOT correctness — three judges of correlated families
# can agree and all be wrong, which is exactly why the human kappa, not this one,
# is the trust gate). Reported as a diagnostic alongside the panel verdicts.


def fleiss_kappa(rows: list[list[int]]) -> float | None:
    """Fleiss' kappa over `rows`, one row per item = per-class rater counts.

    Each row must sum to the SAME number of raters n (items graded by fewer judges
    are dropped by the caller). Returns None if <1 usable item or n<2. Formula
    (Fleiss 1971): P_bar = mean over items of agreement P_i; P_e = sum_j p_j^2 with
    p_j the overall proportion in class j; kappa = (P_bar - P_e)/(1 - P_e)."""
    rows = [r for r in rows if sum(r) > 0]
    if not rows:
        return None
    n = sum(rows[0])
    if n < 2 or any(sum(r) != n for r in rows):
        return None
    N = len(rows)
    k = len(rows[0])
    # overall class proportions
    col_tot = [0] * k
    for r in rows:
        for j in range(k):
            col_tot[j] += r[j]
    denom = N * n
    p = [c / denom for c in col_tot]
    P_e = sum(pj * pj for pj in p)
    # per-item agreement
    P_i = [(sum(c * c for c in r) - n) / (n * (n - 1)) for r in rows]
    P_bar = sum(P_i) / N
    if P_e >= 1.0:  # all raters in one class on every item
        return 1.0 if P_bar >= 1.0 else 0.0
    return (P_bar - P_e) / (1.0 - P_e)


def fleiss_rows(per_item_labels: list[list[str]], classes: list[str]) -> list[list[int]]:
    """Build Fleiss count rows from per-item lists of judge labels. Only items with
    >=2 valid labels AND a uniform rater count contribute (Fleiss needs a constant
    n); rows with a different count than the modal n are dropped by fleiss_kappa."""
    idx = {c: i for i, c in enumerate(classes)}
    rows: list[list[int]] = []
    for labels in per_item_labels:
        valid = [x for x in labels if x in idx]
        if len(valid) < 2:
            continue
        row = [0] * len(classes)
        for x in valid:
            row[idx[x]] += 1
        rows.append(row)
    if not rows:
        return []
    # keep only the modal rater-count so every contributing row has equal n
    from collections import Counter

    modal_n = Counter(sum(r) for r in rows).most_common(1)[0][0]
    return [r for r in rows if sum(r) == modal_n]


# ════════════════════════════ judge backends ══════════════════════════════════
# Each backend is a callable (system, user) -> raw_text plus a `.family` tag. The
# family tag is what the no-self-grading control compares against the answerer.

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def parse_axes(text: str) -> dict[str, Any]:
    """Extract {correctness, groundedness, completeness, hallucination, rationale}
    from a judge reply, robust to reasoning models / fences / stray prose. Returns
    the parsed axes; on total failure returns a sentinel with parse_error set (the
    item is then dropped from that judge's votes, never silently scored 0)."""
    raw = (text or "").strip()
    if not raw:
        return {"parse_error": "empty reply"}
    cleaned = _THINK_RE.sub(" ", raw).strip()
    candidates: list[str] = []
    candidates.extend(f for f in _FENCE_RE.findall(cleaned) if "{" in f)
    candidates.append(cleaned)
    last_good: dict[str, Any] | None = None
    for blob in candidates:
        for m in _JSON_OBJ_RE.finditer(blob):
            frag = m.group(0)
            # _JSON_OBJ_RE is greedy; if the whole-match fails, retry the minimal
            # leading object so trailing prose after a valid object is tolerated.
            for cand in (frag, _first_balanced_object(frag)):
                if not cand:
                    continue
                try:
                    obj = json.loads(cand)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(obj, dict) and _has_axis(obj):
                    last_good = obj
    if last_good is None:
        return {"parse_error": "no axis object", "raw": raw[:200]}
    return {
        "correctness": _clip2(last_good.get("correctness")),
        "groundedness": _clip2(last_good.get("groundedness")),
        "completeness": _clip2(last_good.get("completeness")),
        "hallucination": _coerce_bool(last_good.get("hallucination")),
        "rationale": str(last_good.get("rationale", ""))[:200],
    }


def _has_axis(obj: dict[str, Any]) -> bool:
    return any(k in obj for k in ("correctness", "groundedness", "completeness", "hallucination"))


def _first_balanced_object(s: str) -> str | None:
    """Return the first balanced {...} substring (depth-tracked), or None."""
    depth = 0
    start = -1
    for i, ch in enumerate(s):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                return s[start : i + 1]
    return None


def _coerce_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "y")
    return False


class _OpenAIChatJudge:
    """OpenAI-compatible /chat/completions judge (Qwen, Gemini, GLM all speak this)."""

    def __init__(
        self,
        family: str,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        max_tokens: int = 1024,
        model_path: str = "/chat/completions",
        disable_thinking: bool = True,
    ):
        self.family = family
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.model_path = model_path
        self.disable_thinking = disable_thinking

    def __call__(self, system: str, user: str) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self.disable_thinking:
            # Qwen3 thinking variants burn the whole budget on CoT prose unless this
            # is passed under chat_template_kwargs (verified in judge.py). Harmless
            # to servers that ignore it (Gemini/GLM).
            body["chat_template_kwargs"] = {"enable_thinking": False}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.base_url + self.model_path,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.load(r)
        return data["choices"][0]["message"]["content"]

    def reachable(self) -> tuple[bool, str]:
        url = self.base_url + "/models"
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                if r.status != 200:
                    return False, f"/models HTTP {r.status}"
        except Exception as exc:  # noqa: BLE001 — any failure = unreachable
            return False, f"/models error: {exc}"
        # A live /models can still be billing-gated (GLM returns 200 on /models but
        # 1113 'insufficient balance' on complet/chat). Probe a 1-token chat.
        try:
            self.__call__("Reply with the word OK.", "OK")
            return True, "ok"
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:160]
            except Exception:  # noqa: BLE001
                pass
            return False, f"chat HTTP {exc.code}: {detail}"
        except Exception as exc:  # noqa: BLE001
            return False, f"chat error: {exc}"


class _ClaudeCliJudge:
    """`claude -p "<prompt>" --output-format json` judge (Anthropic family).

    The envelope is a JSON LIST of events; the verdict text is the element with
    type=="result" (its `result` field). `advisorModel` in the user's settings can
    pin an unavailable sub-model (the 'tools.N.model: Fable' 400); we override it to
    a known model so the headless call is clean. We also pass an empty MCP config so
    no project MCP servers are loaded into the judge call."""

    def __init__(
        self,
        family: str = "claude",
        model: str = "sonnet",
        advisor_model: str = "opus",
        timeout: int = 180,
    ):
        self.family = family
        self.model = model
        self.advisor_model = advisor_model
        self.timeout = timeout

    def _run(self, prompt: str) -> str:
        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            self.model,
            "--settings",
            json.dumps({"advisorModel": self.advisor_model}),
            "--strict-mcp-config",
            "--mcp-config",
            json.dumps({"mcpServers": {}}),
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"claude -p exit {proc.returncode}: {proc.stderr[:200]}")
        data = json.loads(proc.stdout)
        if isinstance(data, list):
            results = [e for e in data if isinstance(e, dict) and e.get("type") == "result"]
            if not results:
                raise RuntimeError("claude -p: no type==result element in envelope")
            res = results[-1]
            if res.get("is_error"):
                raise RuntimeError(f"claude -p API error: {str(res.get('result'))[:200]}")
            return str(res.get("result", ""))
        # tolerate a non-list envelope (older CLI) — take `result` if present
        if isinstance(data, dict):
            return str(data.get("result", proc.stdout))
        return proc.stdout

    def __call__(self, system: str, user: str) -> str:
        # claude -p has no separate system slot in print mode; prepend it.
        return self._run(f"{system}\n\n{user}")

    def reachable(self) -> tuple[bool, str]:
        try:
            out = self._run("Reply with the single word OK and nothing else.")
            return ("ok" in out.lower(), out.strip()[:80] or "empty")
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:160]


class _CodexCliJudge:
    """`codex exec -m <model> -o <file>` judge (OpenAI gpt-5.x family).

    codex exec runs non-interactively; the final agent message is written to the
    `--output-last-message` file — cleaner than parsing the JSONL event stream
    (where the verdict is the `item.completed` element with item.type=="agent_message").
    GOTCHA: stdin MUST be DEVNULL or codex blocks forever 'Reading additional input
    from stdin...'. Read-only sandbox + --skip-git-repo-check so the grade call never
    mutates the repo and never trips the git-root check. reasoning_effort=high is
    passed via `-c model_reasoning_effort=<eff>` (parsed as a TOML literal)."""

    def __init__(
        self,
        family: str = "codex",
        model: str = "gpt-5.5",
        reasoning_effort: str = "high",
        timeout: int = 300,
    ):
        self.family = family
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout

    def _run(self, prompt: str) -> str:
        import tempfile

        fh = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        out_path = fh.name
        fh.close()
        try:
            cmd = [
                "codex",
                "exec",
                "-m",
                self.model,
                "-c",
                f"model_reasoning_effort={self.reasoning_effort}",
                "-s",
                "read-only",
                "--skip-git-repo-check",
                "-o",
                out_path,
                prompt,
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
            )
            if proc.returncode != 0:
                raise RuntimeError(f"codex exec exit {proc.returncode}: {proc.stderr[:200]}")
            try:
                with open(out_path, encoding="utf-8") as f:
                    text = f.read()
            except OSError as e:
                raise RuntimeError(f"codex exec: no last-message file ({e})")
            if not text.strip():
                raise RuntimeError("codex exec: empty last-message file")
            return text
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass

    def __call__(self, system: str, user: str) -> str:
        # codex exec has one prompt slot; prepend the rubric like claude -p.
        return self._run(f"{system}\n\n{user}")

    def reachable(self) -> tuple[bool, str]:
        try:
            out = self._run("Reply with the single word OK and nothing else.")
            return ("ok" in out.lower(), out.strip()[:80] or "empty")
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:160]


# Backend registry: name -> factory(args) -> judge instance. Adding a family is one
# entry here. Default panel = qwen + claude_cli + gemini (three disjoint families).
def build_judge(name: str, args: argparse.Namespace):
    if name == "qwen":
        return _OpenAIChatJudge(
            "qwen",
            args.qwen_base_url,
            args.qwen_model,
            max_tokens=args.max_tokens,
        )
    if name == "gemini":
        key = os.environ.get(args.gemini_key_env)
        return _OpenAIChatJudge(
            "gemini",
            args.gemini_base_url,
            args.gemini_model,
            api_key=key,
            max_tokens=args.max_tokens,
            disable_thinking=False,
        )
    if name == "glm":
        key = os.environ.get(args.glm_key_env)
        return _OpenAIChatJudge(
            "glm",
            args.glm_base_url,
            args.glm_model,
            api_key=key,
            max_tokens=args.max_tokens,
            disable_thinking=False,
        )
    if name == "claude_cli":
        return _ClaudeCliJudge(
            "claude",
            model=args.claude_model,
            advisor_model=args.claude_advisor_model,
        )
    if name == "codex":
        return _CodexCliJudge(
            "codex",
            model=args.codex_model,
            reasoning_effort=args.codex_reasoning,
        )
    raise SystemExit(
        f"unknown judge backend: {name!r} (known: qwen, claude_cli, gemini, glm, codex)"
    )


# ════════════════════════════ the rubric prompt ═══════════════════════════════

_PANEL_RUBRIC = """You are a strict, independent grader for a knowledge-graph QA system.
Compare the SYSTEM ANSWER against the EXPECTED ANSWER for the QUESTION, using the
RETRIEVED NOTES as the only admissible evidence.

Score THREE axes, each an integer 0, 1, or 2, and one boolean:

correctness  — does the system answer convey the key facts of the expected answer?
   2 = all key facts correct;  1 = partly right / a key fact garbled;  0 = wrong,
   contradicts the expected answer, or no usable answer.
groundedness — is every claim in the system answer supported by the retrieved notes?
   2 = fully grounded, nothing invented;  1 = mostly grounded, minor unsupported
   detail;  0 = makes claims absent from / unsupported by the notes.
completeness — are ALL key facts from the expected answer present?
   2 = complete;  1 = partial;  0 = missing the answer.
hallucination (true/false) — true if the answer invents a SPECIFIC false fact, cites
   something not in the notes, or answers a question whose information is ABSENT as
   if it were present. An honest "the notes don't say" is NOT a hallucination.

ABSENT/NEGATIVE questions: if the expected answer states the information is NOT in the
notes, then correctness=2 ONLY when the system also declines / says it's absent.
A confident made-up answer to an absent question is correctness=0, hallucination=true.

Respond with ONLY a compact JSON object, no prose, no markdown:
{"correctness":0|1|2,"groundedness":0|1|2,"completeness":0|1|2,"hallucination":true|false,"rationale":"<=20 words"}"""


# ════════════════════════════ responses loading ═══════════════════════════════


def load_responses(path: Path) -> list[dict[str, Any]]:
    """Normalize both the golden responses.jsonl and the calib JSONL shapes.

    Golden:  rec["recall"]["hits"], rec["ask"]["text"].
    Calib:   hits under rec["ask"]["hits"], no top-level recall.
    We surface a uniform rec with ["ask_text"], ["hits"], ["negative_control"]."""
    out: list[dict[str, Any]] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        rec = json.loads(ln)
        ask = rec.get("ask") or {}
        hits = (rec.get("recall") or {}).get("hits")
        if hits is None:
            hits = ask.get("hits") or []
        out.append(
            {
                "id": rec.get("id", "?"),
                "tier": rec.get("tier"),
                "category": rec.get("category"),
                "negative_control": bool(rec.get("negative_control")),
                "question": rec.get("question", ""),
                "ask_text": ask.get("text", "") or "",
                "ask_status": ask.get("status"),
                "hits": hits,
            }
        )
    return out


def load_questions(path: Path) -> dict[str, dict[str, Any]]:
    """{id: question-record} with expected_answer / must_contain / gold_targets.
    Uses the Golden harness's authoritative PyYAML boundary."""
    doc = load_yaml(path)
    return {str(q.get("id")): q for q in (doc.get("questions") or [])}


# ════════════════════════════ ASK×RECALL matrix ═══════════════════════════════
# Per-arm diagnostic: cross ask-correctness (did the answer convey the expected
# facts) with recall-hit (did retrieval surface the gold source at all). Four cells:
#   grounded        — ask correct  AND recall hit  (the system worked end-to-end).
#   synthesis_fail  — ask wrong    BUT recall hit  (info was retrieved, answer blew
#                     it — a generation/synthesis failure, not a retrieval one).
#   ungrounded      — ask correct  BUT recall miss (answered right with no gold
#                     evidence retrieved — lucky/parametric, or a recall-scoring gap).
#   honest_gap      — ask wrong    AND recall miss (nothing retrieved, no answer —
#                     the honest failure mode; for negative controls this is the
#                     CORRECT outcome).
# ask-correct is taken from the panel consensus (C or A == correct); recall-hit is
# whether any retrieved hit resolves to a gold target for the question.


def _gold_paths(q: dict[str, Any]) -> set[str]:
    """Basenames of the question's gold-target sources (recall-hit is matched on
    basename, since hits carry absolute vault paths and gold uses repo-relative)."""
    out: set[str] = set()
    for gt in q.get("gold_targets") or []:
        sp = str(gt.get("source_path") or "")
        if sp:
            out.add(Path(sp).name)
    return out


def recall_hit(hits: list[dict[str, Any]], gold_basenames: set[str]) -> bool:
    """True if any retrieved hit's provenance path basename is a gold source."""
    if not gold_basenames:
        return False
    for h in hits or []:
        prov = h.get("provenance") or {}
        p = str(prov.get("path") or "")
        if p and Path(p).name in gold_basenames:
            return True
        for cs in h.get("context_spans") or []:
            cp = str((cs or {}).get("path") or "")
            if cp and Path(cp).name in gold_basenames:
                return True
    return False


def ask_recall_matrix(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the 4-cell ASK×RECALL matrix over scored items.

    Each item needs {ask_correct: bool, recall_hit: bool, negative_control: bool}.
    Negative-control questions have no gold target, so recall_hit is False by
    construction; a correct decline (ask_correct, no recall) therefore lands in the
    `ungrounded` cell. That is expected for negative controls, so we ALSO report
    `nc_correct` (count of negative-control questions the system correctly declined,
    cell-independent) so an evaluator can net them out when reading the cells."""
    cells = {"grounded": 0, "ungrounded": 0, "synthesis_fail": 0, "honest_gap": 0}
    cell_ids: dict[str, list[str]] = {k: [] for k in cells}
    nc_correct = 0
    nc_total = 0
    for it in items:
        ac, rh, nc = it["ask_correct"], it["recall_hit"], it["negative_control"]
        if ac and rh:
            cell = "grounded"
        elif ac and not rh:
            cell = "ungrounded"
        elif (not ac) and rh:
            cell = "synthesis_fail"
        else:
            cell = "honest_gap"
        cells[cell] += 1
        cell_ids[cell].append(it["id"])
        if nc:
            nc_total += 1
            if ac:
                nc_correct += 1
    total = sum(cells.values())
    return {
        "total": total,
        "cells": cells,
        "cell_ids": cell_ids,
        "nc_total": nc_total,
        "nc_correct": nc_correct,
        "legend": {
            "grounded": "ask correct AND recall hit (end-to-end success)",
            "ungrounded": "ask correct BUT recall miss (incl. correct neg-control declines)",
            "synthesis_fail": "ask wrong BUT recall hit (retrieved but mis-answered)",
            "honest_gap": "ask wrong AND recall miss (the honest failure mode)",
        },
    }


# ════════════════════════════ no-self-grading control ═════════════════════════


class SelfGradingViolation(RuntimeError):
    """A judge shares the answerer's family — the panel is invalid (hard fail)."""


def assert_no_self_grading(judge_families: list[str], answerer_family: str | None) -> None:
    """HARD FAIL if any judge family == the answerer family. No silent demotion: a
    self-graded panel is not a panel. answerer_family=None disables the check (the
    caller did not declare who produced the answers)."""
    if not answerer_family:
        return
    af = answerer_family.strip().lower()
    clash = [f for f in judge_families if f.strip().lower() == af]
    if clash:
        raise SelfGradingViolation(
            f"no-self-grading violated: answerer family {answerer_family!r} also "
            f"appears as judge(s) {clash}. Remove that judge or change the answerer."
        )


# ════════════════════════════ position-swap hook (pairwise) ═══════════════════
# Reference-based grading (this panel) has no candidate ordering, so swap is N/A.
# The hook exists so a future PAIRWISE arm (grade 'is A better than B?') can be made
# order-invariant: grade (A,B) and (B,A); keep the verdict only when the two orders
# agree after relabel, else mark order_sensitive (a positional-bias tell).


def position_swap_consistent(verdict_ab: str, verdict_ba: str) -> dict[str, Any]:
    """Given a pairwise judge's verdict with A-first and with B-first, decide if the
    judgment is order-invariant. Verdicts are one of 'A','B','tie'. The B-first
    verdict is relabeled (its 'A' meant the second candidate). Consistent iff the
    two name the SAME winning candidate (or both 'tie')."""
    relabel = {"A": "B", "B": "A", "tie": "tie"}
    ba_relabeled = relabel.get(verdict_ba, verdict_ba)
    consistent = verdict_ab == ba_relabeled
    return {
        "verdict_ab": verdict_ab,
        "verdict_ba": verdict_ba,
        "ba_relabeled": ba_relabeled,
        "order_invariant": consistent,
        "final": verdict_ab if consistent else "order_sensitive",
    }


# ════════════════════════════ the panel runner ════════════════════════════════


def grade_item(judge, system: str, user: str) -> dict[str, Any]:
    """Call one judge on one item; return parsed axes (or a parse/transport error)."""
    try:
        raw = judge(system, user)
    except Exception as exc:  # noqa: BLE001 — transport/timeout/HTTP all -> skip vote
        return {"parse_error": f"transport: {exc}"[:200], "family": judge.family}
    axes = parse_axes(raw)
    axes["family"] = judge.family
    return axes


# Per-note evidence cap. Retrieved blocks can be large (~12 KB in the reference-eval
# vault); we feed the judges the ACTUAL note text — not just the node name — but
# bound each note so the panel prompt stays in budget. 1000 chars/note × 8 hits
# ≈ 8 KB of evidence, enough to ground a verdict without blowing the context.
_HIT_TEXT_CAP = 1000
_HITS_FOR_EVIDENCE = 8


def _hit_note_text(h: dict[str, Any], *, cap: int = _HIT_TEXT_CAP) -> str:
    """Best-effort REAL retrieved note text for one hit, byte-range-resolved.

    Hits carry no inline text — only node {id,type,name} + provenance
    {path, byte_start, byte_end}. The grounded evidence the system answered from
    is the byte slice of that source file. We read it (the same provenance paths
    recall_hit already trusts), decode tolerantly, collapse whitespace, and cap.
    On any read failure we fall back to '' so build_user_prompt can degrade to the
    node name rather than crash a grade."""
    prov = h.get("provenance") or {}
    path = str(prov.get("path") or "")
    bs = prov.get("byte_start")
    be = prov.get("byte_end")
    if not path or not isinstance(bs, int) or not isinstance(be, int) or be <= bs:
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(bs)
            raw = fh.read(be - bs)
    except OSError:
        return ""
    text = raw.decode("utf-8", "replace")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > cap:
        text = text[:cap].rstrip() + " …[truncated]"
    return text


def build_user_prompt(item: dict[str, Any], q: dict[str, Any]) -> str:
    expected = q.get("expected_answer", "")
    snippets = []
    for h in (item.get("hits") or [])[:_HITS_FOR_EVIDENCE]:
        node = h.get("node") or {}
        header = f"- [{node.get('type', '')}] {node.get('name', '')}".rstrip()
        body = _hit_note_text(h)
        snippets.append(f"{header}\n  {body}" if body else header)
    snip = "\n".join(snippets) if snippets else "(none)"
    return (
        f"QUESTION:\n{item['question']}\n\n"
        f"EXPECTED ANSWER:\n{expected}\n\n"
        f"SYSTEM ANSWER:\n{item['ask_text'] or '(empty)'}\n\n"
        f"RETRIEVED NOTES (top {_HITS_FOR_EVIDENCE} hits, each with its actual "
        f"source text — this is the admissible evidence):\n{snip}\n"
    )


def run_panel(
    responses: list[dict[str, Any]],
    questions: dict[str, dict[str, Any]],
    judges: list[Any],
    *,
    answerer_family: str | None,
    limit: int | None = None,
    probe: bool = True,
    log: Callable[[str], None] = lambda s: None,
) -> dict[str, Any]:
    """Run the full panel and assemble the report dict (no I/O)."""
    families = [j.family for j in judges]
    # BIAS CONTROL (a): hard-fail before spending a single token.
    assert_no_self_grading(families, answerer_family)

    # Reachability probe per judge (records the disjoint-family finding).
    reachable: dict[str, dict[str, Any]] = {}
    active: list[Any] = []
    for j in judges:
        if not probe:
            reachable[j.family] = {"reachable": True, "detail": "probe skipped"}
            active.append(j)
            continue
        ok, detail = j.reachable()
        reachable[j.family] = {"reachable": ok, "detail": detail}
        log(f"  judge {j.family:12s} reachable={ok}  ({detail})")
        if ok:
            active.append(j)
    active_families = [j.family for j in active]
    distinct_families = sorted(set(active_families))

    if len(active) < 2:
        return {
            "error": "fewer than 2 reachable judges; a panel needs >=2 disjoint families",
            "judges_configured": families,
            "judges_reachable": reachable,
        }

    rows = responses if limit is None else responses[:limit]
    per_item: list[dict[str, Any]] = []
    per_item_labels: list[list[str]] = []
    matrix_items: list[dict[str, Any]] = []
    verbosity_log: list[dict[str, Any]] = []

    for item in rows:
        qid = item["id"]
        q = questions.get(qid, {})
        user = build_user_prompt(item, q)
        abstained = looks_abstained(item["ask_text"])
        nc = item["negative_control"]

        judge_views: list[dict[str, Any]] = []
        labels: list[str] = []
        for j in active:
            axes = grade_item(j, _PANEL_RUBRIC, user)
            if axes.get("parse_error"):
                judge_views.append(
                    {
                        "family": j.family,
                        "error": axes["parse_error"],
                        "label": None,
                    }
                )
                continue
            label = collapse_to_class(axes, negative_control=nc, abstained=abstained)
            labels.append(label)
            judge_views.append(
                {
                    "family": j.family,
                    "correctness": axes["correctness"],
                    "groundedness": axes["groundedness"],
                    "completeness": axes["completeness"],
                    "hallucination": axes["hallucination"],
                    "rationale": axes.get("rationale", ""),
                    "label": label,
                }
            )

        gold = _gold_paths(q)
        rh = recall_hit(item["hits"], gold)
        if len(labels) < 2:
            per_item.append(
                {
                    "id": qid,
                    "tier": item.get("tier"),
                    "category": item.get("category"),
                    "negative_control": nc,
                    "abstained": abstained,
                    "recall_hit": rh,
                    "gold_sources": sorted(gold),
                    "judges": judge_views,
                    "complete": False,
                    "incomplete_reason": f"only {len(labels)} valid judge vote(s); need at least 2",
                    "valid_votes": len(labels),
                    "consensus": None,
                    "majority": False,
                    "unanimous": False,
                    "precedence_pick": None,
                    "vote_counts": {},
                }
            )
            continue

        vote = majority_vote(labels)
        consensus = vote["consensus"]
        # ask_correct for the matrix: consensus is C (correct) or A (correct abstain).
        # When there's no majority, fall back to the precedence pick for the matrix
        # only (the per-item record still flags majority=False for routing).
        decided = consensus or vote["precedence_pick"]
        ask_correct = decided in ("C", "A")
        per_item_labels.append(labels)
        # verbosity bias log (control b): length vs the decided class.
        alen = len(item["ask_text"])
        awords = len(re.findall(r"\w+", item["ask_text"]))
        verbosity_log.append(
            {
                "id": qid,
                "answer_chars": alen,
                "answer_words": awords,
                "consensus": consensus,
                "decided": decided,
                "ask_correct": ask_correct,
            }
        )
        matrix_items.append(
            {
                "id": qid,
                "ask_correct": ask_correct,
                "recall_hit": rh,
                "negative_control": nc,
            }
        )
        per_item.append(
            {
                "id": qid,
                "tier": item.get("tier"),
                "category": item.get("category"),
                "negative_control": nc,
                "abstained": abstained,
                "recall_hit": rh,
                "gold_sources": sorted(gold),
                "judges": judge_views,
                "complete": True,
                "incomplete_reason": None,
                "valid_votes": len(labels),
                "consensus": consensus,
                "majority": vote["majority"],
                "unanimous": vote["unanimous"],
                "precedence_pick": vote["precedence_pick"],
                "vote_counts": vote["counts"],
            }
        )

    # Fleiss kappa over the per-item judge labels (shared-bias signal).
    frows = fleiss_rows(per_item_labels, CLASSES)
    fkappa = fleiss_kappa(frows)
    n_full_panel = len(frows)

    matrix = ask_recall_matrix(matrix_items)

    # consensus tally + no-majority routing list
    tally: dict[str, int] = {c: 0 for c in CLASSES}
    no_majority: list[str] = []
    incomplete_items: list[str] = []
    for rec in per_item:
        if not rec["complete"]:
            incomplete_items.append(rec["id"])
            continue
        if rec["consensus"]:
            tally[rec["consensus"]] += 1
        else:
            no_majority.append(rec["id"])

    # Derive trust from the human-validation kappa artifact (default-off, human-only).
    trust = derive_trust()
    return {
        "panel": {
            "judges_configured": families,
            "judges_active": active_families,
            "distinct_families": distinct_families,
            "n_distinct_families": len(distinct_families),
            "judges_reachable": reachable,
            "answerer_family": answerer_family,
            "no_self_grading_ok": True,  # we got here, so the assert passed
            "vote_rule": "2/3 strict majority (>half active judges)",
            "items_requested": len(rows),
            "items_graded": len(per_item) - len(incomplete_items),
        },
        "complete": not incomplete_items,
        "incomplete_items": incomplete_items,
        "inter_judge": {
            "fleiss_kappa": fkappa,
            "items_with_full_panel": n_full_panel,
            "note": (
                "Fleiss kappa is inter-JUDGE agreement (shared-bias signal), "
                "NOT correctness. High agreement among correlated families can "
                "coincide with a wrong panel — the HUMAN kappa (kappa/) is the "
                "trust gate, not this number."
            ),
        },
        "consensus_tally": tally,
        "no_majority_items": no_majority,
        "ask_recall_matrix": matrix,
        "verbosity_log": verbosity_log,
        # Trust is DERIVED from the human-validation kappa artifact (derive_trust),
        # never a hand-edited flag. Default-off + human-only: with no artifact this is
        # deterministic-only (today's committed/CI state). Re-validated 2026-06-17
        # against the Codex-proxy 6-class labels (51 items, qwen+gemini disjoint panel,
        # no-self-grading vs the Bedrock-Opus answerer): primary Cohen kappa = 0.20,
        # 10k-bootstrap 95% CI [0.05, 0.36] -> DETERMINISTIC-ONLY (CI lower < 0.60).
        # Feeding judges the real retrieved note text lifted kappa from 0.11 and (with
        # the abstention guard) drops the evidence-starvation false-H, but Codex is an
        # AI proxy, not a human — only a HUMAN artifact can wire this judge.
        "trust": trust,
        "authoritative": trust["authoritative"],
        "authoritative_note": trust["note"],
        "per_item": per_item,
    }


# ════════════════════════════ rendering ═══════════════════════════════════════


def render_panel(report: dict[str, Any]) -> str:
    if "error" in report:
        return f"PANEL ERROR: {report['error']}\n" + json.dumps(
            report.get("judges_reachable", {}), indent=2
        )
    p = report["panel"]
    ij = report["inter_judge"]
    m = report["ask_recall_matrix"]
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("  JUDGE PANEL  —  N-judge, disjoint-family, 2/3 vote")
    lines.append("=" * 70)
    lines.append(f"  judges configured ... {', '.join(p['judges_configured'])}")
    lines.append(
        f"  judges active ....... {', '.join(p['judges_active'])}  "
        f"({p['n_distinct_families']} distinct families)"
    )
    for fam, st in p["judges_reachable"].items():
        mark = "ok " if st["reachable"] else "DOWN"
        lines.append(f"     [{mark}] {fam:12s} {st['detail']}")
    lines.append(
        f"  answerer family ..... {p['answerer_family']}  "
        f"(no-self-grading: {'PASS' if p['no_self_grading_ok'] else 'FAIL'})"
    )
    lines.append(f"  vote rule ........... {p['vote_rule']}")
    lines.append(f"  items graded ........ {p['items_graded']}")
    if not report.get("complete", True):
        lines.append(f"  INCOMPLETE items .... {report.get('incomplete_items', [])}")
    lines.append("  ---- consensus tally (6-class) ----")
    tally = report["consensus_tally"]
    lines.append(
        "    "
        + "  ".join(f"{c}={tally[c]}" for c in CLASSES)
        + f"   no-majority={len(report['no_majority_items'])}"
    )
    if report["no_majority_items"]:
        lines.append(f"    no-majority ids: {report['no_majority_items']}")
    lines.append("  ---- inter-judge agreement (SHARED-BIAS signal) ----")
    fk = ij["fleiss_kappa"]
    lines.append(
        f"    Fleiss kappa ...... {('n/a' if fk is None else f'{fk:.4f}')}  "
        f"(over {ij['items_with_full_panel']} full-panel items)"
    )
    lines.append("  ---- ASK x RECALL matrix ----")
    c = m["cells"]
    lines.append(f"    grounded ......... {c['grounded']:3d}   (ask correct + recall hit)")
    lines.append(f"    synthesis_fail ... {c['synthesis_fail']:3d}   (ask wrong + recall hit)")
    lines.append(
        f"    ungrounded ....... {c['ungrounded']:3d}   (ask correct + recall miss; "
        f"includes {m['nc_correct']}/{m['nc_total']} correct neg-control declines)"
    )
    lines.append(f"    honest_gap ....... {c['honest_gap']:3d}   (ask wrong + recall miss)")
    lines.append("=" * 70)
    tr = report.get("trust", {"trust_level": "deterministic-only", "authoritative": False})
    if tr.get("authoritative"):
        lines.append(
            f"  AUTHORITATIVE — human kappa-cleared (CI-lower "
            f"{tr.get('kappa_ci_lower')}); judge may adjudicate."
        )
    elif tr.get("trusted"):
        lines.append(
            f"  ADVISORY — human kappa-cleared (CI-lower "
            f"{tr.get('kappa_ci_lower')}); soft signal, floor still rules."
        )
    else:
        lines.append(
            "  NOT AUTHORITATIVE — gated on the human kappa (kappa/), "
            f"trust_level={tr.get('trust_level')}."
        )
    lines.append("=" * 70)
    return "\n".join(lines)


# ════════════════════════════ panel subcommand ════════════════════════════════


def cmd_panel(args: argparse.Namespace) -> int:
    responses = load_responses(Path(args.responses).expanduser())
    questions = load_questions(Path(args.questions).expanduser())
    names = [n.strip() for n in args.judges.split(",") if n.strip()]
    judges = [build_judge(n, args) for n in names]
    try:
        report = run_panel(
            responses,
            questions,
            judges,
            answerer_family=args.answerer_family,
            limit=args.limit,
            probe=not args.no_probe,
            log=(lambda s: print(s, file=sys.stderr)) if args.verbose else (lambda s: None),
        )
    except SelfGradingViolation as exc:
        print(json.dumps({"error": "self_grading_violation", "detail": str(exc)}, indent=2))
        return 2
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render_panel(report))
    if "error" in report or not report.get("complete", True):
        return 1
    return 0


def panel_report_errors(
    report: Any, *, expected_response_ids: list[str] | None = None
) -> list[str]:
    if not isinstance(report, dict):
        return ["panel report must be an object"]
    errors: list[str] = []
    if report.get("error"):
        errors.append(f"panel error: {report['error']}")
    if report.get("complete") is not True:
        errors.append("panel report is incomplete")
    rows = report.get("per_item")
    if not isinstance(rows, list) or not rows:
        errors.append("panel report requires per_item rows")
        return errors
    row_ids = [str(row.get("id") or "") for row in rows if isinstance(row, dict)]
    if any(not qid for qid in row_ids) or len(row_ids) != len(rows):
        errors.append("every panel item requires a non-empty id")
    if any(row.get("complete") is not True for row in rows if isinstance(row, dict)):
        errors.append("one or more panel items are incomplete")
    if expected_response_ids is not None and row_ids != expected_response_ids:
        errors.append("panel item ids/order do not match responses")
    return errors


def cmd_validate_panel(args: argparse.Namespace) -> int:
    report = json.loads(Path(args.panel).read_text(encoding="utf-8"))
    responses = load_responses(Path(args.responses).expanduser())
    if args.limit is not None:
        responses = responses[: args.limit]
    errors = panel_report_errors(
        report, expected_response_ids=[str(rec.get("id") or "") for rec in responses]
    )
    if errors:
        print(json.dumps({"valid": False, "errors": errors}), file=sys.stderr)
        return 2
    print(json.dumps({"valid": True, "items": len(report["per_item"])}))
    return 0


# ════════════════════════════ selftest ════════════════════════════════════════


def cmd_selftest(_args: argparse.Namespace) -> int:
    print("SELFTEST — panel deterministic logic (no network)")
    ok = True

    def check(name: str, got: Any, want: Any) -> None:
        nonlocal ok
        good = got == want
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'} {name}: got {got!r}  want {want!r}")

    def approx(name: str, got: float | None, want: float, tol: float = 1e-9) -> None:
        nonlocal ok
        good = got is not None and abs(got - want) <= tol
        ok = ok and good
        print(f"  {'ok  ' if good else 'FAIL'} {name}: got {got!r}  want ~{want} (tol {tol})")

    # ── 6-class collapse (precedence H>A>M>W>P>C) ──
    check(
        "collapse correct",
        collapse_to_class(
            {"correctness": 2, "groundedness": 2, "completeness": 2, "hallucination": False},
            negative_control=False,
            abstained=False,
        ),
        "C",
    )
    check(
        "collapse partial (corr=1)",
        collapse_to_class(
            {"correctness": 1, "groundedness": 2, "completeness": 1, "hallucination": False},
            negative_control=False,
            abstained=False,
        ),
        "P",
    )
    check(
        "collapse partial (corr=2,comp=0)",
        collapse_to_class(
            {"correctness": 2, "groundedness": 2, "completeness": 0, "hallucination": False},
            negative_control=False,
            abstained=False,
        ),
        "P",
    )
    check(
        "collapse wrong (corr=0)",
        collapse_to_class(
            {"correctness": 0, "groundedness": 1, "completeness": 0, "hallucination": False},
            negative_control=False,
            abstained=False,
        ),
        "W",
    )
    check(
        "collapse hallucinated (flag)",
        collapse_to_class(
            {"correctness": 2, "groundedness": 0, "completeness": 2, "hallucination": True},
            negative_control=False,
            abstained=False,
        ),
        "H",
    )
    check(
        "collapse missed (answer existed, abstained)",
        collapse_to_class(
            {"correctness": 0, "groundedness": 2, "completeness": 0, "hallucination": False},
            negative_control=False,
            abstained=True,
        ),
        "M",
    )
    check(
        "collapse abstained (neg-control + declined)",
        collapse_to_class(
            {"correctness": 2, "groundedness": 2, "completeness": 2, "hallucination": False},
            negative_control=True,
            abstained=True,
        ),
        "A",
    )
    check(
        "collapse H on confident absent answer (neg-control, NOT abstained)",
        collapse_to_class(
            {"correctness": 2, "groundedness": 1, "completeness": 2, "hallucination": False},
            negative_control=True,
            abstained=False,
        ),
        "H",
    )
    # GUARD: a stray hallucination=true flag on an UNAMBIGUOUS decline (abstained +
    # corr==0) must NOT collapse to H — the rubric says an honest decline is not a
    # hallucination. It routes to A (neg-control) or M (answerable) instead. Justified
    # by the rubric definition alone; independent of any label set.
    check(
        "collapse decline+stray-H on neg-control -> A (not H)",
        collapse_to_class(
            {"correctness": 0, "groundedness": 2, "completeness": 0, "hallucination": True},
            negative_control=True,
            abstained=True,
        ),
        "A",
    )
    check(
        "collapse decline+stray-H on answerable -> M (not H)",
        collapse_to_class(
            {"correctness": 0, "groundedness": 2, "completeness": 0, "hallucination": True},
            negative_control=False,
            abstained=True,
        ),
        "M",
    )
    # A mixed answer that abstains AND asserts a fact (corr>=1) still collapses to H.
    check(
        "collapse abstain+assert+H still -> H",
        collapse_to_class(
            {"correctness": 1, "groundedness": 1, "completeness": 1, "hallucination": True},
            negative_control=False,
            abstained=True,
        ),
        "H",
    )

    # ── trust wiring (derive_trust): default-off, human-only, banded ──
    import tempfile as _tf

    check(
        "trust default (no artifact) -> deterministic-only",
        derive_trust(None)["trust_level"],
        "deterministic-only",
    )
    check("trust default not authoritative", derive_trust(None)["authoritative"], False)
    check(
        "trust missing-file -> deterministic-only",
        derive_trust("/nonexistent/never/here.json")["trust_level"],
        "deterministic-only",
    )

    def _trust_of(obj: dict) -> dict:
        fh = _tf.NamedTemporaryFile("w", suffix=".json", delete=False)
        try:
            json.dump(obj, fh)
            fh.close()  # flush to disk BEFORE derive_trust reads it
            return derive_trust(fh.name)
        finally:
            try:
                os.unlink(fh.name)
            except OSError:
                pass

    # HARD refusal: a non-human (AI-proxy) source can NEVER wire, even at kappa 0.95.
    t_proxy = _trust_of({"source": "codex-proxy", "kappa_ci_lower": 0.95})
    check(
        "trust proxy-source HARD-refused -> deterministic-only",
        t_proxy["trust_level"],
        "deterministic-only",
    )
    check("trust proxy-source not authoritative", t_proxy["authoritative"], False)
    # HUMAN bands mirror compute_kappa.gate_verdict on the CI lower bound.
    check(
        "trust human CI-lower 0.85 -> authoritative",
        _trust_of({"source": "human", "kappa_ci_lower": 0.85})["trust_level"],
        "authoritative",
    )
    check(
        "trust human CI-lower 0.85 authoritative=True",
        _trust_of({"source": "human", "kappa_ci_lower": 0.85})["authoritative"],
        True,
    )
    check(
        "trust human CI-lower 0.70 -> advisory (trusted, not authoritative)",
        _trust_of({"source": "human", "kappa_ci_lower": 0.70})["trust_level"],
        "advisory",
    )
    check(
        "trust human CI-lower 0.70 authoritative=False",
        _trust_of({"source": "human", "kappa_ci_lower": 0.70})["authoritative"],
        False,
    )
    check(
        "trust human CI-lower 0.70 trusted=True",
        _trust_of({"source": "human", "kappa_ci_lower": 0.70})["trusted"],
        True,
    )
    check(
        "trust human CI-lower 0.59 -> deterministic-only",
        _trust_of({"source": "human", "kappa_ci_lower": 0.59})["trust_level"],
        "deterministic-only",
    )
    # The actual 2026-06-17 proxy result (kappa~0.20) would stay deterministic-only
    # even if it were (it is NOT) a human artifact.
    check(
        "trust human CI-lower 0.20 -> deterministic-only",
        _trust_of({"source": "human", "kappa_ci_lower": 0.20})["trust_level"],
        "deterministic-only",
    )
    check(
        "trust human malformed kappa -> deterministic-only",
        _trust_of({"source": "human", "kappa_ci_lower": "n/a"})["trust_level"],
        "deterministic-only",
    )
    # GROUNDED-CONSENSUS tier: stricter bar (advisory only at >=0.80), NEVER authoritative.
    gc85 = _trust_of({"source": "grounded-consensus", "kappa_ci_lower": 0.85})
    check("trust grounded CI 0.85 -> advisory (not authoritative)", gc85["trust_level"], "advisory")
    check("trust grounded CI 0.85 authoritative=False (human-only)", gc85["authoritative"], False)
    check("trust grounded CI 0.85 trusted=True", gc85["trusted"], True)
    check(
        "trust grounded CI 0.79 -> deterministic-only (stricter than human's 0.60)",
        _trust_of({"source": "grounded-consensus", "kappa_ci_lower": 0.79})["trust_level"],
        "deterministic-only",
    )
    # The ACTUAL 2026-06-20 3-judge panel result (kappa CI-lower ~ -0.05) stays det-only.
    check(
        "trust grounded ACTUAL CI-lower -0.05 -> deterministic-only",
        _trust_of({"source": "grounded-consensus", "kappa_ci_lower": -0.05})["trust_level"],
        "deterministic-only",
    )

    # ── majority vote ──
    v = majority_vote(["C", "C", "W"])
    check("vote 2/3 -> C", v["consensus"], "C")
    check("vote 2/3 majority flag", v["majority"], True)
    check("vote 2/3 not unanimous", v["unanimous"], False)
    v2 = majority_vote(["C", "C", "C"])
    check("vote 3/3 unanimous", v2["unanimous"], True)
    v3 = majority_vote(["C", "W", "A"])
    check("vote 3-way split -> no majority", v3["consensus"], None)
    check("vote 3-way split precedence pick = A (H>A>M>W>P>C)", v3["precedence_pick"], "A")
    v4 = majority_vote(["C", "W"])  # 1-1 on a 2-judge panel
    check("vote 1-1 tie -> no majority", v4["consensus"], None)

    # ── Fleiss kappa ──
    # Perfect agreement: every item all-3 raters in one class -> kappa 1.0.
    perfect = fleiss_rows([["C", "C", "C"], ["W", "W", "W"], ["A", "A", "A"]], CLASSES)
    approx("fleiss perfect -> 1.0", fleiss_kappa(perfect), 1.0)
    # Fleiss textbook anchor (Fleiss 1971, 10 raters / classic worked example gives
    # kappa≈0.21). We use a smaller hand-computed case: 3 raters, 2 items, both items
    # split 2-1 the same way. Hand: N=2,n=3,k=6. col_tot C=4,W=2 -> p_C=4/6,p_W=2/6.
    #   P_e = (2/3)^2 + (1/3)^2 = 4/9+1/9 = 5/9.
    #   P_i each = (2^2+1^2 - 3)/(3*2) = (4+1-3)/6 = 2/6 = 1/3.  P_bar=1/3.
    #   kappa = (1/3 - 5/9)/(1 - 5/9) = (3/9-5/9)/(4/9) = (-2/9)/(4/9) = -0.5.
    split = fleiss_rows([["C", "C", "W"], ["C", "C", "W"]], CLASSES)
    approx("fleiss 2-1 split anchor -> -0.5", fleiss_kappa(split), -0.5, tol=1e-9)
    # Chance-level mixed -> near 0 (not pinned tightly; just present + finite).
    mixed = fleiss_rows([["C", "W", "A"], ["W", "A", "C"], ["A", "C", "W"]], CLASSES)
    km = fleiss_kappa(mixed)
    check("fleiss mixed is finite", km is not None and math.isfinite(km), True)

    # ── ASK×RECALL matrix ──
    items = [
        {"id": "a", "ask_correct": True, "recall_hit": True, "negative_control": False},
        {"id": "b", "ask_correct": False, "recall_hit": True, "negative_control": False},
        {"id": "c", "ask_correct": True, "recall_hit": False, "negative_control": False},
        {"id": "d", "ask_correct": False, "recall_hit": False, "negative_control": False},
        {"id": "e", "ask_correct": True, "recall_hit": False, "negative_control": True},
    ]
    mtx = ask_recall_matrix(items)
    check("matrix grounded", mtx["cells"]["grounded"], 1)
    check("matrix synthesis_fail", mtx["cells"]["synthesis_fail"], 1)
    check("matrix ungrounded (incl neg-control correct decline e)", mtx["cells"]["ungrounded"], 2)
    check("matrix honest_gap", mtx["cells"]["honest_gap"], 1)
    check("matrix nc_correct (e is a correct neg-control decline)", mtx["nc_correct"], 1)
    check("matrix nc_total", mtx["nc_total"], 1)
    check("matrix total", mtx["total"], 5)

    # ── no-self-grading control ──
    raised = False
    try:
        assert_no_self_grading(["qwen", "claude", "gemini"], "qwen")
    except SelfGradingViolation:
        raised = True
    check("no-self-grading fires when answerer family in panel", raised, True)
    raised2 = False
    try:
        assert_no_self_grading(["claude", "gemini"], "qwen")
    except SelfGradingViolation:
        raised2 = True
    check("no-self-grading passes for disjoint panel", raised2, False)

    # ── position-swap hook (pairwise) ──
    swap_ok = position_swap_consistent("A", "B")  # both say first-given wins -> consistent
    check("position-swap consistent (A wins both orders)", swap_ok["order_invariant"], True)
    check("position-swap final = A", swap_ok["final"], "A")
    swap_bad = position_swap_consistent("A", "A")  # position bias: 'A' wins regardless
    check("position-swap detects order sensitivity", swap_bad["order_invariant"], False)
    check("position-swap flags order_sensitive", swap_bad["final"], "order_sensitive")
    swap_tie = position_swap_consistent("tie", "tie")
    check("position-swap tie stays tie", swap_tie["final"], "tie")

    # ── axis parsing robustness ──
    p1 = parse_axes(
        '{"correctness":2,"groundedness":1,"completeness":2,"hallucination":false,"rationale":"ok"}'
    )
    check(
        "parse clean axes",
        (p1["correctness"], p1["groundedness"], p1["hallucination"]),
        (2, 1, False),
    )
    p2 = parse_axes(
        'thinking... ```json\n{"correctness":0,"groundedness":0,"completeness":0,"hallucination":true}\n``` done'
    )
    check("parse fenced + prose", (p2["correctness"], p2["hallucination"]), (0, True))
    p3 = parse_axes(
        '<think>let me reason</think>{"correctness":1,"groundedness":2,"completeness":1,"hallucination":false}'
    )
    check("parse strips <think>", p3["correctness"], 1)
    p4 = parse_axes("no json here at all")
    check("parse failure -> parse_error", "parse_error" in p4, True)
    p5 = parse_axes('{"correctness":"2","hallucination":"yes"}')  # stringy values coerced
    check("parse coerces stringy axis/bool", (p5["correctness"], p5["hallucination"]), (2, True))

    print("-" * 60)
    print("SELFTEST: PASS" if ok else "SELFTEST: FAIL")
    return 0 if ok else 1


# ════════════════════════════ argparse / wiring ═══════════════════════════════


def build_panel_parser(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    p.add_argument(
        "--responses",
        required=True,
        help="responses.jsonl (golden) or calib JSONL (ask.hits) — both accepted",
    )
    p.add_argument("--questions", required=True, help="questions.yaml with expected_answer")
    p.add_argument("--out", required=True, help="panel report JSON output path")
    p.add_argument(
        "--judges",
        default="qwen,claude_cli,gemini",
        help="comma list of judge backends (default 3 disjoint families)",
    )
    p.add_argument(
        "--answerer-family",
        dest="answerer_family",
        default=None,
        help="family that PRODUCED the answers (no-self-grading hard check)",
    )
    p.add_argument("--limit", type=int, default=None, help="grade only the first N items")
    p.add_argument(
        "--no-probe",
        dest="no_probe",
        action="store_true",
        help="skip the per-judge reachability probe (assume all up)",
    )
    p.add_argument("--max-tokens", dest="max_tokens", type=int, default=1024)
    p.add_argument("--json", action="store_true", help="also print the full report JSON")
    p.add_argument("--verbose", action="store_true", help="log probe lines to stderr")
    # qwen
    p.add_argument(
        "--qwen-base-url",
        dest="qwen_base_url",
        default=os.environ.get("OKTO_NEURON_LLM_BASE_URL", "").strip(),
        help="OpenAI-compatible Qwen endpoint (or set OKTO_NEURON_LLM_BASE_URL)",
    )
    p.add_argument(
        "--qwen-model",
        dest="qwen_model",
        default=os.environ.get("OKTO_NEURON_JUDGE_MODEL", "unsloth/Qwen3.6-27B-NVFP4").strip(),
    )
    # claude
    p.add_argument("--claude-model", dest="claude_model", default="sonnet")
    p.add_argument("--claude-advisor-model", dest="claude_advisor_model", default="opus")
    # codex (OpenAI gpt-5.x via codex-cli `codex exec`)
    p.add_argument("--codex-model", dest="codex_model", default="gpt-5.5")
    p.add_argument("--codex-reasoning", dest="codex_reasoning", default="high")
    # gemini
    p.add_argument(
        "--gemini-base-url",
        dest="gemini_base_url",
        default="https://generativelanguage.googleapis.com/v1beta/openai",
    )
    p.add_argument("--gemini-model", dest="gemini_model", default="gemini-2.5-flash")
    p.add_argument("--gemini-key-env", dest="gemini_key_env", default="GEMINI_API_KEY")
    # glm (z.ai / zhipu) — wired; billing-gated in practice
    p.add_argument("--glm-base-url", dest="glm_base_url", default="https://api.z.ai/api/paas/v4")
    p.add_argument("--glm-model", dest="glm_model", default="glm-4.6")
    p.add_argument("--glm-key-env", dest="glm_key_env", default="ZAI_API_KEY")
    return p


def _resolve_qwen_auto(args: argparse.Namespace) -> None:
    """If --qwen-model auto, discover the largest chat model via judge.py's picker
    (kept identical to judge.py so the panel's Qwen judge == the standalone judge)."""
    if args.qwen_model != "auto":
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import judge as _judge  # type: ignore

    try:
        args.qwen_model = _judge._select_chat_model(args.qwen_base_url)
    except Exception:  # noqa: BLE001 — leave 'auto'; reachability probe will fail it
        pass


def main() -> int:
    p = argparse.ArgumentParser(
        description="JUDGE PANEL — N-judge disjoint-family 2/3 vote + Fleiss kappa "
        "+ bias controls + 0-2 axes + ASK×RECALL (black-box)."
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("panel", help="run the panel over responses + questions")
    build_panel_parser(pp)

    def _panel_entry(a: argparse.Namespace) -> int:
        selected = {name.strip() for name in a.judges.split(",") if name.strip()}
        if "qwen" in selected and not a.qwen_base_url:
            pp.error("qwen requires --qwen-base-url or OKTO_NEURON_LLM_BASE_URL")
        _resolve_qwen_auto(a)
        return cmd_panel(a)

    pp.set_defaults(func=_panel_entry)

    pv = sub.add_parser("validate-report", help="fail unless a panel report is complete")
    pv.add_argument("--panel", required=True)
    pv.add_argument("--responses", required=True)
    pv.add_argument("--limit", type=int, default=None)
    pv.set_defaults(func=cmd_validate_panel)

    pst = sub.add_parser("selftest", help="validate the deterministic logic (no network)")
    pst.set_defaults(func=cmd_selftest)

    args = p.parse_args()
    try:
        return args.func(args)
    except GoldenYamlError as exc:
        print(json.dumps({"error": "golden_yaml_error", "detail": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
