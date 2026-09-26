#!/usr/bin/env python3
"""Light unit test for the reference-guided semantic judge.

STATIC triples prove both directions of the owner's binding decision — judge
MEANING not wording:

  (a) gold "34" vs candidates "34" / "thirty-four" / "34.0" / "about 34"
        -> CORRECT   (paraphrase / format / unit tolerance)
  (b) gold "34" vs "the value is 43" / "unknown"
        -> INCORRECT (strict on substance: wrong value, or missing when present)
  (c) a fully paraphrased SENTENCE answer
        -> CORRECT

These make a handful of REAL judge LLM calls against an explicitly configured
OpenAI-compatible endpoint with a capable model. The test SKIPS cleanly when no
endpoint is configured or reachable so it never breaks offline / CI, but it is
meant to be run live to earn the verdicts.

Pure-logic checks (verdict parser, deterministic backstop) run with NO LLM and
always execute.

Run:
  uv run pytest tests/golden/bin/test_semantic_judge.py -xvs
  # or standalone, to print the real verdicts:
  uv run python tests/golden/bin/test_semantic_judge.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import semantic_judge as sj  # noqa: E402

BASE_URL = os.environ.get("OKTO_NEURON_LLM_BASE_URL", "").strip()
MODEL = os.environ.get("OKTO_NEURON_JUDGE_MODEL", "unsloth/Qwen3.6-27B-NVFP4").strip()


def _model_available(base_url: str, model: str) -> bool:
    """The live tests pin a specific judge model for a deterministic verdict. Skip
    (don't ERROR) when the endpoint is up but hosts a different lineup."""
    try:
        import json
        import urllib.request

        with urllib.request.urlopen(base_url.rstrip("/") + "/models", timeout=3) as r:
            ids = {m["id"] for m in json.load(r).get("data", [])}
        return model in ids
    except Exception:
        return False


_LLM_UP = bool(BASE_URL) and sj._llm_models_reachable(BASE_URL)
_MODEL_OK = _LLM_UP and _model_available(BASE_URL, MODEL)
# The live groups make ~7 real LLM calls (~100s). Tagged `slow` so the default
# `uv run pytest` (testpaths=["tests"]) deselects them under `-m "not slow"`,
# per the project's no-reflexive-LLM-in-default-suite rule. The pure-logic
# parser/backstop checks stay unmarked and always run.
requires_llm = pytest.mark.skipif(
    not _MODEL_OK, reason=f"judge model {MODEL} not available at {BASE_URL}/models"
)
live = pytest.mark.slow


def _judge(question: str, gold: str, quotes: list[str], candidate: str) -> dict:
    return sj.judge_item(
        question,
        gold,
        quotes,
        candidate,
        base_url=BASE_URL,
        model=MODEL,
        must_contain=[gold],
    )


# ── pure-logic checks (no LLM) ───────────────────────────────────────────────────


def test_parse_handles_incorrect_verdict():
    # Regression guard: judge.py's parser rejects "INCORRECT" and would coerce it
    # to unparseable. Ours must keep it AND not settle on the CORRECT substring.
    out = sj.parse_semantic_verdict(
        '{"verdict":"INCORRECT","reason":"wrong value","covered_gold_facts":[]}'
    )
    assert out["verdict"] == "INCORRECT"
    out2 = sj.parse_semantic_verdict(
        '{"verdict":"CORRECT","reason":"ok","covered_gold_facts":["34"]}'
    )
    assert out2["verdict"] == "CORRECT"
    assert out2["covered_gold_facts"] == ["34"]


def test_parse_prefers_last_valid_object():
    reply = (
        '<think>maybe {"verdict":"PARTIAL"} ... no</think> here is my answer: '
        '{"verdict":"CORRECT","reason":"conveys the value","covered_gold_facts":["34"]}'
    )
    out = sj.parse_semantic_verdict(reply)
    assert out["verdict"] == "CORRECT"


def test_backstop_is_non_gating_signal():
    # "thirty-four" does NOT contain "34" -> backstop flags it absent even though
    # the semantic verdict would be CORRECT. Proves the backstop never gates.
    bs = sj.deterministic_backstop("thirty-four engineers", ["34"])
    assert bs["all_present"] is False
    assert bs["missing"] == ["34"]
    # And a wrong answer can still carry the token.
    bs2 = sj.deterministic_backstop("it is definitely not 34", ["34"])
    assert bs2["any_present"] is True


# ── live semantic-judge checks (real LLM calls) ──────────────────────────────────

Q_COUNT = "How many DS & ML engineers are on the roster?"
GOLD_COUNT = "34"
QUOTES_COUNT = ["The roster lists 34 DS & ML engineers."]


@live
@requires_llm
@pytest.mark.parametrize("candidate", ["34", "thirty-four", "34.0", "about 34"])
def test_a_paraphrase_and_format_tolerant_correct(candidate):
    res = _judge(Q_COUNT, GOLD_COUNT, QUOTES_COUNT, candidate)
    assert res["verdict"] == "CORRECT", res


@live
@requires_llm
@pytest.mark.parametrize("candidate", ["the value is 43", "unknown"])
def test_b_wrong_or_missing_is_incorrect(candidate):
    res = _judge(Q_COUNT, GOLD_COUNT, QUOTES_COUNT, candidate)
    assert res["verdict"] == "INCORRECT", res


@live
@requires_llm
def test_c_paraphrased_sentence_is_correct():
    question = "What did the summary conclude about the size of the team?"
    gold = "The team has 34 engineers."
    quotes = ["In total the team comprises 34 engineers."]
    candidate = "According to the write-up, the group is made up of thirty-four engineers in all."
    res = _judge(question, gold, quotes, candidate)
    assert res["verdict"] == "CORRECT", res


# ── standalone runner: print the REAL verdicts ───────────────────────────────────


def _run_live() -> int:
    if not BASE_URL:
        print("SKIP: OKTO_NEURON_LLM_BASE_URL is required for live judge tests")
        return 0
    if not _LLM_UP:
        print(f"SKIP: no LLM at {BASE_URL}/models")
        return 0
    cases = [
        ("a", Q_COUNT, GOLD_COUNT, QUOTES_COUNT, "34", "CORRECT"),
        ("a", Q_COUNT, GOLD_COUNT, QUOTES_COUNT, "thirty-four", "CORRECT"),
        ("a", Q_COUNT, GOLD_COUNT, QUOTES_COUNT, "34.0", "CORRECT"),
        ("a", Q_COUNT, GOLD_COUNT, QUOTES_COUNT, "about 34", "CORRECT"),
        ("b", Q_COUNT, GOLD_COUNT, QUOTES_COUNT, "the value is 43", "INCORRECT"),
        ("b", Q_COUNT, GOLD_COUNT, QUOTES_COUNT, "unknown", "INCORRECT"),
        (
            "c",
            "What did the summary conclude about the size of the team?",
            "The team has 34 engineers.",
            ["In total the team comprises 34 engineers."],
            "According to the write-up, the group is made up of thirty-four engineers in all.",
            "CORRECT",
        ),
    ]
    ok = 0
    for label, q, gold, quotes, cand, want in cases:
        res = _judge(q, gold, quotes, cand)
        got = res["verdict"]
        passed = got == want
        ok += passed
        print(
            f"[{label}] cand={cand!r:55} want={want:9} got={got:11} "
            f"{'PASS' if passed else 'FAIL'}  covered={res.get('covered_gold_facts')} "
            f"backstop_all_present={res['backstop']['all_present']}"
        )
        print(f"      reason: {res.get('reason')}")
    print(f"\n{ok}/{len(cases)} cases passed (model={MODEL})")
    return 0 if ok == len(cases) else 1


if __name__ == "__main__":
    sys.exit(_run_live())
