#!/usr/bin/env python3
"""Reference-guided SEMANTIC judge — the PRIMARY answer-quality verdict.

The owner's binding decision: answer-quality grading must be SEMANTIC and
paraphrase-tolerant, not deterministic token matching. Knowledge answers have
many valid surface forms — a correct "34" can appear as "34", "thirty-four",
"34.0", "$34", or "about 34" — so must_contain both false-negatives correct
paraphrases and false-positives wrong answers that merely echo the token.

This module grades one (question, reference answer, gold quote(s), candidate)
item with a REFERENCE-GUIDED rubric: the judge SEES the gold answer and the
byte-anchored gold quote, then decides whether the candidate conveys the same
fact. That is the κ-recovery lever over the old open-ended panel (κ=0.14): a
reference-guided judge is scored against a known target, not asked to invent one.

Contract:
  judge_item(question, gold_answer, gold_quotes, candidate, ...) -> dict with
    verdict            : "CORRECT" | "PARTIAL" | "INCORRECT" (or "SKIPPED"/"ERROR")
    reason             : short judge rationale
    covered_gold_facts : list[str] — which reference facts the candidate conveys
    backstop           : deterministic must_contain hook (NON-authoritative)

Deterministic backstop (`deterministic_backstop`) is a cheap CI sanity signal
ONLY — it reuses the must_contain tokens against the CANDIDATE answer and never
overrides the semantic verdict.

Transport (models discovery + thinking-off chat) is copied verbatim from
judge.py so this module stays standalone (importing judge.py would drag in its
panel/scorecard/sweep sibling graph). Thinking is suppressed via
chat_template_kwargs.enable_thinking=false — mandatory for a parseable verdict
on the Qwen3 thinking variants (see judge.py for the live-verified rationale).

Usage:
  # library
  from semantic_judge import judge_item, deterministic_backstop

  # batch (LAPTOP-ONLY; running the full 142-Q/51-Q A/B is a DEFERRED step)
  semantic_judge.py judge-file --questions Q.yaml --responses R.jsonl --out J.json
                    [--base-url URL] [--model auto|NAME] [--limit N]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from golden_yaml import GoldenYamlError, load_yaml, question_validation_errors

DEFAULT_BASE_URL = os.environ.get("OKTO_NEURON_LLM_BASE_URL", "").strip()
DEFAULT_MODEL = os.environ.get("OKTO_NEURON_JUDGE_MODEL", "unsloth/Qwen3.6-27B-NVFP4").strip()


def _openai_headers(*, json_body: bool = False) -> dict[str, str]:
    """Build standard OpenAI-compatible headers without logging the secret."""

    headers = {"Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


# ── transport (copied from judge.py; keeps this module import-standalone) ────────

# Substrings that mark a model id as NOT chat-capable, so "auto" never routes a
# judge call to a TTS/embedding/rerank model (which 400s every request).
_NON_CHAT_MODEL_MARKERS = (
    "tts",
    "voice",
    "embed",
    "whisper",
    "parakeet",
    "stt",
    "rerank",
    "clip",
    "asr",
    "locate",
)


def _is_chat_model(model_id: str) -> bool:
    mid = model_id.lower()
    return not any(marker in mid for marker in _NON_CHAT_MODEL_MARKERS)


def _model_params_billions(model_id: str) -> float:
    """Parse the parameter count (billions) from a model id: ``Qwen3.6-27B`` -> 27.0."""
    best = 0.0
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*[bB]\b", model_id):
        best = max(best, float(m.group(1)))
    return best


def _llm_models_reachable(base_url: str) -> bool:
    if not base_url:
        return False
    url = base_url.rstrip("/") + "/models"
    try:
        request = urllib.request.Request(url, headers=_openai_headers())
        with urllib.request.urlopen(request, timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _select_chat_model(base_url: str) -> str:
    """Pick the largest chat-capable model the server advertises.

    A tiny chat model (0.8B) cannot reliably emit verdict JSON (memory: it goes
    all-unparseable), so prefer the most-parameter chat id — the most capable
    judge available. Filtering excludes TTS/embedding/rerank ids first."""
    request = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers=_openai_headers(),
    )
    with urllib.request.urlopen(request, timeout=5) as r:
        ids = [m["id"] for m in json.load(r).get("data", [])]
    chat = [mid for mid in ids if _is_chat_model(mid)]
    if not chat:
        raise RuntimeError(f"no chat-capable model at {base_url}/models; advertised: {ids}")
    return max(chat, key=_model_params_billions)


def _llm_chat(base_url: str, model: str, system: str, user: str, *, max_tokens: int) -> str:
    """Thinking-OFF OpenAI-compatible chat call. temperature 0 for stable verdicts.

    enable_thinking=false MUST live under chat_template_kwargs — a top-level flag
    is ignored by compatible Qwen3 endpoints (verified live in judge.py). Without
    it the model burns the whole token budget on chain-of-thought prose and the
    verdict JSON never appears."""
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers=_openai_headers(json_body=True),
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = json.load(r)
    return data["choices"][0]["message"]["content"]


# ── canonical-quote normalization (shared by the deterministic backstop) ─────────
def _canonical_quote(quote: str) -> str:
    """Return already-parsed content without treating source backslashes as YAML."""

    return quote


# ── reference-guided semantic rubric ─────────────────────────────────────────────

_SYSTEM = """You are a strict, REFERENCE-GUIDED grader for a knowledge-graph QA system.

You are given a QUESTION, a REFERENCE ANSWER (the gold answer), one or more GOLD
QUOTES (verbatim source passages that ground the reference), and a CANDIDATE
ANSWER produced by the system. Decide whether the candidate is correct GIVEN THE
REFERENCE.

JUDGE MEANING, NOT WORDING. A candidate conveys a reference fact when it means
the same thing, in ANY phrasing, format, or unit. Treat all of these as the same
value: "34" = "thirty-four" = "34.0" = "34.00" = "$34" = "about 34" = "~34".
Numbers, dates, names, ports, paths, and identifiers match whenever they denote
the same real-world value regardless of surface form. Reordering, extra correct
context, and different sentence structure never lower the verdict.

BE STRICT ON SUBSTANCE. Overlapping tokens do NOT make a candidate correct.
Mark a reference fact wrong or missing when the candidate states a DIFFERENT
value (e.g. "43" when the reference is "34"), says the information is
unknown/absent while the reference supplies it, or simply omits the required
fact.

NEGATIVE / ABSENT references: when the reference says the information is NOT in
the notes, the candidate is CORRECT only if it also declines or says it is
absent; fabricating an answer is INCORRECT.

VERDICTS:
- CORRECT   : the candidate conveys EVERY key fact of the reference, with no
              contradiction and no fabrication.
- PARTIAL   : the candidate is on-topic and conveys SOME but not all key
              reference facts, or hedges a key fact.
- INCORRECT : the candidate contradicts the reference, fabricates, or misses the
              key reference fact(s).

Respond with ONLY a compact JSON object — no prose, no markdown fences:
{"verdict":"CORRECT|PARTIAL|INCORRECT","reason":"<=25 words","covered_gold_facts":["<reference fact the candidate conveys>"]}
covered_gold_facts is a list of plain strings (empty [] if the candidate conveys
none of the reference facts)."""


def build_user_prompt(
    question: str, gold_answer: str, gold_quotes: list[str] | None, candidate: str
) -> str:
    quotes = [q for q in (gold_quotes or []) if str(q).strip()]
    if quotes:
        quote_block = "\n".join(f"- {q}" for q in quotes)
    else:
        quote_block = "(none provided)"
    return (
        f"QUESTION:\n{question}\n\n"
        f"REFERENCE ANSWER (gold):\n{gold_answer or '(none)'}\n\n"
        f"GOLD QUOTE(S) — verbatim source grounding the reference:\n{quote_block}\n\n"
        f"CANDIDATE ANSWER:\n{candidate.strip() if candidate and candidate.strip() else '(empty)'}\n"
    )


# ── verdict parsing (three-value; robust to reasoning-model noise) ───────────────

_VALID_VERDICTS = ("CORRECT", "PARTIAL", "INCORRECT")
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
_JSON_OBJ_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)
# INCORRECT listed FIRST so the alternation never settles on the CORRECT
# substring inside "inCORRECT"; \b guards also block that match (no word
# boundary inside a single token), but ordering makes the intent explicit.
_VERDICT_KV_RE = re.compile(
    r"""["']?verdict["']?\s*[:=]\s*["']?\b(INCORRECT|CORRECT|PARTIAL)\b""",
    re.IGNORECASE,
)


def _coerce_facts(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x) for x in value if str(x).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def parse_semantic_verdict(text: str) -> dict[str, Any]:
    """Extract {verdict, reason, covered_gold_facts} from a judge reply.

    Most-precise-first: strip <think>, prefer JSON in a ```json fence, then scan
    every balanced {...} object and take the LAST one carrying a valid verdict
    (the model's final answer, not a schema it echoed mid-thought). Falls back to
    a tolerant `verdict: <LABEL>` regex. Only genuinely label-free replies become
    "unparseable"."""
    raw = (text or "").strip()
    if not raw:
        return {"verdict": "unparseable", "reason": "", "covered_gold_facts": []}
    cleaned = _THINK_RE.sub(" ", raw).strip()

    candidates: list[str] = []
    candidates.extend(f for f in _FENCE_RE.findall(cleaned) if "{" in f)
    candidates.append(cleaned)

    last_good: dict[str, Any] | None = None
    for blob in candidates:
        for m in _JSON_OBJ_RE.finditer(blob):
            try:
                obj = json.loads(m.group(0))
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(obj, dict):
                continue
            v = str(obj.get("verdict", "")).upper().strip()
            if v in _VALID_VERDICTS:
                last_good = {
                    "verdict": v,
                    "reason": str(obj.get("reason", ""))[:300],
                    "covered_gold_facts": _coerce_facts(obj.get("covered_gold_facts")),
                }
    if last_good is not None:
        return last_good

    m = _VERDICT_KV_RE.findall(cleaned)
    if m:
        return {"verdict": m[-1].upper(), "reason": cleaned[:300], "covered_gold_facts": []}
    return {"verdict": "unparseable", "reason": raw[:300], "covered_gold_facts": []}


# ── deterministic backstop (NON-authoritative CI sanity hook) ────────────────────


def deterministic_backstop(candidate: str, must_contain: list[str] | None) -> dict[str, Any]:
    """Cheap must_contain presence check against the CANDIDATE answer.

    Reuses the must_contain tokens and _canonical_quote normalization purely as a
    CI sanity signal. It is NEVER the correctness verdict — the reference-guided
    semantic verdict is primary. A token can be absent from a correct paraphrase
    ("thirty-four" has no "34") and present in a wrong answer ("...not 34..."),
    which is exactly why it does not gate."""
    tokens = [str(t) for t in (must_contain or [])]
    cand = _canonical_quote(candidate or "").lower()
    present, missing = [], []
    for tok in tokens:
        norm = _canonical_quote(tok).lower().strip()
        (present if norm and norm in cand else missing).append(tok)
    return {
        "must_contain_total": len(tokens),
        "present": present,
        "missing": missing,
        "all_present": bool(tokens) and not missing,
        "any_present": bool(present),
        "note": "non-authoritative sanity signal; semantic verdict is primary",
    }


# ── the core reference-guided judge call ─────────────────────────────────────────


def judge_item(
    question: str,
    gold_answer: str,
    gold_quotes: list[str] | None,
    candidate: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    model: str = DEFAULT_MODEL,
    must_contain: list[str] | None = None,
    max_tokens: int = 512,
) -> dict[str, Any]:
    """Grade one item. Returns verdict + reason + covered_gold_facts + backstop.

    verdict is "SKIPPED" when no LLM is reachable and "ERROR" on a transport
    fault, so callers can distinguish "not graded" from a real INCORRECT."""
    backstop = deterministic_backstop(candidate, must_contain)
    if not base_url:
        return {
            "verdict": "SKIPPED",
            "reason": "OKTO_NEURON_LLM_BASE_URL or an explicit base_url is required",
            "covered_gold_facts": [],
            "backstop": backstop,
            "model": None,
        }
    if not _llm_models_reachable(base_url):
        return {
            "verdict": "SKIPPED",
            "reason": f"no OpenAI-compatible LLM at {base_url}/models",
            "covered_gold_facts": [],
            "backstop": backstop,
            "model": None,
        }
    resolved = _select_chat_model(base_url) if model == "auto" else model
    user = build_user_prompt(question, gold_answer, gold_quotes, candidate)
    try:
        raw = _llm_chat(base_url, resolved, _SYSTEM, user, max_tokens=max_tokens)
        parsed = parse_semantic_verdict(raw)
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        KeyError,
        TimeoutError,
        ValueError,
    ) as exc:
        return {
            "verdict": "ERROR",
            "reason": f"llm error: {exc}",
            "covered_gold_facts": [],
            "backstop": backstop,
            "model": resolved,
        }
    parsed["backstop"] = backstop
    parsed["model"] = resolved
    return parsed


# ── batch entry point (LAPTOP-ONLY; full 142/51 A/B is a DEFERRED step) ──────────


def _load_yaml(path: Path) -> dict[str, Any]:
    return load_yaml(path)


def _read_responses(path: Path) -> list[dict[str, Any]]:
    out = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln:
            out.append(json.loads(ln))
    return out


def _batch_input_errors(
    q_doc: Any, responses: list[dict[str, Any]], *, require_all_questions: bool
) -> list[str]:
    errors = question_validation_errors(q_doc)
    questions = q_doc.get("questions") if isinstance(q_doc, dict) else []
    question_ids: list[str] = []
    if isinstance(questions, list):
        for index, question in enumerate(questions):
            if not isinstance(question, dict):
                continue
            qid = str(question.get("id") or "").strip()
            if qid:
                question_ids.append(qid)
            if not isinstance(question.get("question"), str) or not question["question"].strip():
                errors.append(f"{qid or f'index-{index}'}: question text is required")

    response_ids: list[str] = []
    for index, response in enumerate(responses):
        if not isinstance(response, dict):
            errors.append(f"response at index {index} must be an object")
            continue
        qid = str(response.get("id") or "").strip()
        if not qid:
            errors.append(f"response at index {index} requires a non-empty id")
        else:
            response_ids.append(qid)
    if not responses:
        errors.append("responses must contain at least one item")
    if len(set(response_ids)) != len(response_ids):
        errors.append("response ids must be unique")
    unknown = sorted(set(response_ids) - set(question_ids))
    if unknown:
        errors.append("response ids missing from questions: " + ", ".join(unknown))
    if require_all_questions:
        missing = sorted(set(question_ids) - set(response_ids))
        if missing:
            errors.append("question ids missing from responses: " + ", ".join(missing))
    return errors


def semantic_report_errors(
    report: Any, *, expected_response_ids: list[str] | None = None
) -> list[str]:
    if not isinstance(report, dict):
        return ["semantic judge report must be an object"]
    errors: list[str] = []
    if report.get("skipped") is not False:
        errors.append("semantic judge report is skipped")
    verdicts = report.get("verdicts")
    if not isinstance(verdicts, list) or not verdicts:
        errors.append("semantic judge report requires verdicts")
        return errors
    verdict_ids: list[str] = []
    allowed = {"CORRECT", "PARTIAL", "INCORRECT", "UNSUPPORTED"}
    for index, verdict in enumerate(verdicts):
        if not isinstance(verdict, dict):
            errors.append(f"verdict at index {index} must be an object")
            continue
        qid = str(verdict.get("id") or "").strip()
        if not qid:
            errors.append(f"verdict at index {index} requires a non-empty id")
        else:
            verdict_ids.append(qid)
        value = str(verdict.get("verdict") or "").upper()
        if value == "UNSUPPORTED":
            if (
                verdict.get("unsupported_capability") != "valid_time_queries"
                or verdict.get("score_eligible") is not False
            ):
                errors.append(
                    f"{qid or f'index-{index}'}: unsupported verdict lacks its "
                    "valid_time_queries exclusion contract"
                )
        elif value not in allowed:
            errors.append(f"{qid or f'index-{index}'}: incomplete verdict {value or '<empty>'}")
    if len(set(verdict_ids)) != len(verdict_ids):
        errors.append("verdict ids must be unique")
    if expected_response_ids is not None and verdict_ids != expected_response_ids:
        errors.append("verdict ids/order do not match responses")
    return errors


def _gold_answer_for(q: dict[str, Any]) -> str:
    """Compose the reference answer shown to the judge: the expected answer, and
    any must_contain facts appended so a terse expected_answer still exposes every
    required token as a reference fact."""
    expected = str(q.get("expected_answer") or "").strip()
    must = [str(t) for t in (q.get("must_contain") or [])]
    if must:
        joined = "; ".join(must)
        return (
            f"{expected}\n(Required facts: {joined})" if expected else f"Required facts: {joined}"
        )
    return expected


def cmd_judge_file(args: argparse.Namespace) -> int:
    q_doc = _load_yaml(Path(args.questions))
    responses = _read_responses(Path(args.responses))
    if args.limit:
        responses = responses[: args.limit]
    input_errors = _batch_input_errors(q_doc, responses, require_all_questions=not bool(args.limit))
    if input_errors:
        print(json.dumps({"error": "invalid batch", "details": input_errors}), file=sys.stderr)
        return 2
    questions = {str(q["id"]): q for q in q_doc["questions"]}

    requires_judge = any(
        not questions.get(str(rec.get("id") or ""), {}).get("unsupported_capability")
        for rec in responses
    )
    if requires_judge and not _llm_models_reachable(args.base_url):
        report = {"skipped": True, "reason": f"no LLM at {args.base_url}/models", "verdicts": []}
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps({"skipped": True, "reason": report["reason"]}, indent=2))
        return 75

    model = (
        _select_chat_model(args.base_url) if requires_judge and args.model == "auto" else args.model
    )
    tally = {
        "CORRECT": 0,
        "PARTIAL": 0,
        "INCORRECT": 0,
        "UNSUPPORTED": 0,
        "SKIPPED": 0,
        "ERROR": 0,
        "unparseable": 0,
    }
    verdicts = []
    for rec in responses:
        qid = rec.get("id", "?")
        q = questions.get(qid, {})
        unsupported_capability = q.get("unsupported_capability")
        if unsupported_capability:
            tally["UNSUPPORTED"] += 1
            verdicts.append(
                {
                    "id": qid,
                    "tier": rec.get("tier") or q.get("tier"),
                    "verdict": "UNSUPPORTED",
                    "reason": (
                        "ADR 0040 preserves temporal evidence but does not support "
                        "valid-time query semantics"
                    ),
                    "covered_gold_facts": [],
                    "backstop": None,
                    "score_eligible": False,
                    "unsupported_capability": unsupported_capability,
                }
            )
            continue
        question = q.get("question", rec.get("question", ""))
        gold_answer = _gold_answer_for(q)
        gold_quotes = [str(g.get("quote", "")) for g in (q.get("gold_targets") or [])]
        candidate = (rec.get("ask") or {}).get("text", "") or ""
        res = judge_item(
            question,
            gold_answer,
            gold_quotes,
            candidate,
            base_url=args.base_url,
            model=model,
            must_contain=q.get("must_contain"),
            max_tokens=args.max_tokens,
        )
        tally[res["verdict"]] = tally.get(res["verdict"], 0) + 1
        verdicts.append(
            {
                "id": qid,
                "tier": rec.get("tier") or q.get("tier"),
                "verdict": res["verdict"],
                "reason": res.get("reason", ""),
                "covered_gold_facts": res.get("covered_gold_facts", []),
                "backstop": res.get("backstop"),
                "score_eligible": True,
            }
        )

    report = {
        "skipped": False,
        "base_url": args.base_url,
        "model": model,
        "tally": tally,
        "verdicts": verdicts,
    }
    report_errors = semantic_report_errors(
        report, expected_response_ids=[str(rec["id"]) for rec in responses]
    )
    report["complete"] = not report_errors
    report["errors"] = report_errors
    Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({"skipped": False, "model": model, "tally": tally}, indent=2))
    return 0 if not report_errors else 2


# ── verdict → scorecard arm JSONL (A/B wiring) ────────────────────────────────
# Maps this module's three-value semantic verdict to the binary correctness column
# scorecard.py consumes, so the same judge output drives per-arm tallies AND the
# paired McNemar A/B. CORRECT→True; PARTIAL/INCORRECT/SKIPPED/ERROR/unparseable→
# False (strict "fully correct"). UNSUPPORTED capability rows never enter either
# arm. --partial-as-correct flips PARTIAL→True for a sensitivity read. The mapping
# MUST be identical across both arms or the A/B is confounded (the runner calls
# this with the same flags for block + subgraph).


def _verdict_to_correct(verdict: str, *, partial_as_correct: bool) -> bool:
    v = str(verdict or "").upper().strip()
    if v == "CORRECT":
        return True
    if v == "PARTIAL":
        return partial_as_correct
    return False  # INCORRECT / SKIPPED / ERROR / unparseable are never "correct"


def cmd_to_arm(args: argparse.Namespace) -> int:
    """Emit a scorecard arm JSONL from a semantic judge-file report.

    Each line: {id, judge_correct, proxy_correct, verdict, tier}. `judge_correct`
    is the PRIMARY semantic verdict (scorecard --grader judge). `proxy_correct` is
    the deterministic must_contain backstop (`backstop.all_present`) for
    scorecard --grader proxy — a NON-authoritative CI sanity signal, never the
    correctness verdict."""
    report = json.loads(Path(args.judge).read_text(encoding="utf-8"))
    report_errors = semantic_report_errors(report)
    if report_errors:
        print(
            json.dumps({"error": "incomplete semantic judge", "details": report_errors}),
            file=sys.stderr,
        )
        return 2
    verdicts = report.get("verdicts") or []
    rows = 0
    with open(args.out, "w", encoding="utf-8") as fh:
        for v in verdicts:
            if v.get("score_eligible") is False:
                continue
            backstop = v.get("backstop") or {}
            rec = {
                "id": v.get("id"),
                "judge_correct": _verdict_to_correct(
                    v.get("verdict", ""), partial_as_correct=args.partial_as_correct
                ),
                "proxy_correct": bool(backstop.get("all_present", False)),
                "verdict": v.get("verdict"),
                "tier": v.get("tier"),
            }
            fh.write(json.dumps(rec) + "\n")
            rows += 1
    print(
        json.dumps(
            {
                "arm": args.out,
                "rows": rows,
                "judge_correct": sum(
                    _verdict_to_correct(
                        v.get("verdict", ""), partial_as_correct=args.partial_as_correct
                    )
                    for v in verdicts
                    if v.get("score_eligible") is not False
                ),
                "excluded_unsupported": sum(
                    1 for v in verdicts if v.get("score_eligible") is False
                ),
                "partial_as_correct": args.partial_as_correct,
            }
        )
    )
    return 0


def cmd_validate_report(args: argparse.Namespace) -> int:
    report = json.loads(Path(args.judge).read_text(encoding="utf-8"))
    expected_ids = None
    if args.responses:
        responses = _read_responses(Path(args.responses))
        if args.limit:
            responses = responses[: args.limit]
        expected_ids = [str(rec.get("id") or "") for rec in responses]
    errors = semantic_report_errors(report, expected_response_ids=expected_ids)
    if errors:
        print(json.dumps({"valid": False, "errors": errors}), file=sys.stderr)
        return 2
    print(json.dumps({"valid": True, "verdicts": len(report["verdicts"])}))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Reference-guided semantic judge (stdlib only).")
    sub = p.add_subparsers(dest="cmd", required=True)

    pj = sub.add_parser(
        "judge-file",
        help="grade responses.jsonl vs questions.yaml with the reference-guided "
        "semantic verdict (LAPTOP-ONLY; the full 142/51 A/B is DEFERRED)",
    )
    pj.add_argument("--questions", required=True)
    pj.add_argument("--responses", required=True)
    pj.add_argument("--out", required=True)
    pj.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        required=not bool(DEFAULT_BASE_URL),
        help="OpenAI-compatible endpoint (or set OKTO_NEURON_LLM_BASE_URL)",
    )
    pj.add_argument("--model", default=DEFAULT_MODEL)
    pj.add_argument("--limit", type=int, default=0, help="grade only the first N responses")
    pj.add_argument("--max-tokens", dest="max_tokens", type=int, default=512)
    pj.set_defaults(func=cmd_judge_file)

    pa = sub.add_parser(
        "to-arm",
        help="convert a semantic judge-file report to a scorecard arm JSONL "
        "(judge_correct + proxy_correct columns) for the paired A/B",
    )
    pa.add_argument("--judge", required=True, help="semantic judge-file JSON")
    pa.add_argument("--out", required=True, help="arm JSONL for scorecard.py")
    pa.add_argument(
        "--partial-as-correct",
        action="store_true",
        help="count PARTIAL verdicts as correct (default: only CORRECT)",
    )
    pa.set_defaults(func=cmd_to_arm)

    pv = sub.add_parser("validate-report", help="fail unless a semantic judge report is complete")
    pv.add_argument("--judge", required=True)
    pv.add_argument("--responses", default=None)
    pv.add_argument("--limit", type=int, default=0)
    pv.set_defaults(func=cmd_validate_report)

    args = p.parse_args()
    try:
        return args.func(args)
    except GoldenYamlError as exc:
        print(json.dumps({"error": "golden_yaml_error", "detail": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
