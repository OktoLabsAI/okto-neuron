#!/usr/bin/env python3
"""Rubric v3 fabrication-detection regression fixture (plan-5-judge-fabrication §3D).

Grades the scripted golden ``judge.py`` rubric (rubric v3, `_RUBRIC` /
`compose_verdict` / `_parse_verdict`), NOT `semantic_judge.py` — that judge has
its own adversarial fixture next door
(`test_semantic_judge_adversarial.py`) and is a separate gate (owner Q4).

Every fixture row here is a SYNTHESIZED STRUCTURAL ANALOGUE, fictional entities
only. None of the CoP private corpus's item ids, question text, expected
answers, or embellished-answer text appear here or anywhere in this repo —
those were never retained (plan §1.5) and policy forbids committing them even
if they had been (ADR 0040 calibration-content exclusion). The shapes mirror
the ADR's 5-row embellishment table one-for-one (owner Q5): this is a NEW,
separately-labelled baseline, not a reproduction of that historical table.

Layer 1 (this file's pure-Python tests, always run): `compose_verdict` truth
table including both `negative` branches, and `_parse_verdict` structured /
legacy / noisy-reasoning-model parsing. No LLM, no daemon, no vault.

Layer 2 (`-m slow`, live only, skips cleanly offline): the fixture rows judged
end-to-end against the configured model host at temperature 0, thinking OFF,
mirroring `test_semantic_judge_adversarial.py`'s live-skip pattern.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import judge as j  # noqa: E402

BASE_URL = os.environ.get("OKTO_NEURON_LLM_BASE_URL", "").strip()
MODEL = os.environ.get("OKTO_NEURON_JUDGE_MODEL", "unsloth/Qwen3.6-27B-NVFP4").strip()

# ── Layer 1: compose_verdict truth table (owner ruling Q1 applied) ─────────────
#
# key_facts, fabrication, negative -> expected verdict
_COMPOSE_CASES: list[tuple[str, str, bool, bool, str]] = [
    ("conveyed-no-fab", "conveyed", False, False, "correct"),
    ("conveyed-fab-capped-partial", "conveyed", True, False, "partial"),  # Q1 cap, not "wrong"
    ("hedged-no-fab", "hedged", False, False, "partial"),
    ("hedged-fab-still-partial", "hedged", True, False, "partial"),
    ("partial-no-fab", "partial", False, False, "partial"),
    ("partial-fab-still-partial", "partial", True, False, "partial"),
    ("missing-no-fab-non-negative", "missing", False, False, "missed"),
    ("missing-fab-non-negative", "missing", True, False, "partial"),
    ("contradicted-no-fab", "contradicted", False, False, "wrong"),
    ("contradicted-fab", "contradicted", True, False, "wrong"),
    # negative/absent-question branches — the row draft-4 must not re-break.
    ("negative-decline-correct", "missing", False, True, "correct"),
    ("negative-decline-with-fab-flag-capped", "missing", True, True, "partial"),
    # a substantive fabricated answer to a negative question is routed through
    # key_facts="contradicted" by the rubric, not through the fabrication flag
    # (that path is unaffected by the Q1 cap — see judge.py compose_verdict doc).
    ("negative-substantive-fabricated-answer", "contradicted", True, True, "wrong"),
]


@pytest.mark.parametrize("case", _COMPOSE_CASES, ids=[c[0] for c in _COMPOSE_CASES])
def test_compose_verdict_truth_table(case) -> None:
    _, key_facts, fabrication, negative, expected = case
    assert j.compose_verdict(key_facts, fabrication, negative=negative) == expected


def test_compose_verdict_rejects_unknown_key_facts() -> None:
    with pytest.raises(ValueError):
        j.compose_verdict("maybe", False)


# ── Layer 1: _parse_verdict shapes ──────────────────────────────────────────────


def test_parse_verdict_structured_shape() -> None:
    raw = json.dumps(
        {
            "key_facts": "conveyed",
            "fabrication": True,
            "fabricated_items": ["Dr. Fictional Reviewer"],
            "rationale": "score right, reviewer invented",
        }
    )
    parsed = j._parse_verdict(raw)
    assert parsed["key_facts"] == "conveyed"
    assert parsed["fabrication"] is True
    assert parsed["fabricated_items"] == ["Dr. Fictional Reviewer"]


def test_parse_verdict_legacy_shape_back_compat() -> None:
    raw = json.dumps({"verdict": "partial", "rationale": "old shape"})
    parsed = j._parse_verdict(raw)
    assert "key_facts" not in parsed
    assert parsed["verdict"] == "partial"


def test_parse_verdict_strips_think_blocks_and_fences() -> None:
    raw = (
        "<think>let me reason about this at length</think>\n"
        "```json\n"
        '{"key_facts":"hedged","fabrication":false,"fabricated_items":[],'
        '"rationale":"present in code block"}\n'
        "```"
    )
    parsed = j._parse_verdict(raw)
    assert parsed["key_facts"] == "hedged"
    assert parsed["fabrication"] is False


def test_parse_verdict_prefers_structured_over_legacy_when_both_present() -> None:
    # a reasoning model that echoes a schema example, then answers for real.
    raw = (
        '{"verdict":"correct","rationale":"echoed schema example"}\n'
        '{"key_facts":"partial","fabrication":false,"fabricated_items":[],'
        '"rationale":"actual answer"}'
    )
    parsed = j._parse_verdict(raw)
    assert parsed["key_facts"] == "partial"


def test_parse_verdict_unparseable_on_label_free_text() -> None:
    parsed = j._parse_verdict("I cannot help with that request.")
    assert parsed["verdict"] == "unparseable"


# ── Layer 1: excerpt builder ─────────────────────────────────────────────────────


def test_build_excerpt_block_empty_without_inputs_root() -> None:
    assert (
        j.build_excerpt_block(
            None, [{"provenance": {"path": "a.md", "byte_start": 0, "byte_end": 5}}]
        )
        == ""
    )


def test_build_excerpt_block_dedups_by_block_id_and_caps_bytes(tmp_path: Path) -> None:
    src = tmp_path / "note.md"
    body = ("x" * 5000).encode()
    src.write_bytes(body)
    hits = [
        {"provenance": {"path": "note.md", "block_id": "b1", "byte_start": 0, "byte_end": 4000}},
        {"provenance": {"path": "note.md", "block_id": "b1", "byte_start": 0, "byte_end": 4000}},
    ]
    block = j.build_excerpt_block(tmp_path, hits, cap_bytes=1200)
    assert block.count("[note.md]") == 1  # deduped by block_id
    assert len(block) <= 1200 + 20  # capped (plus small prefix/ellipsis overhead)


def test_build_excerpt_block_uses_strict_source_and_tolerates_missing_range(tmp_path: Path) -> None:
    src = tmp_path / "note.md"
    src.write_bytes(b"hello world")
    # no byte range -> skipped, not raised
    assert j.build_excerpt_block(tmp_path, [{"provenance": {"path": "note.md"}}]) == ""


# ── §3D fixture: 5 fabrication analogues + 4 hold-the-line rows ─────────────────
#
# id, question, expected_answer, negative, system_answer, source_excerpt,
# expect_wrong_or_partial (fabrication analogues never score "correct" per Q1;
# the negative analogue reaches "wrong" via contradiction, not the fab flag)
FabricationCase = tuple[str, str, str, bool, str, str, str]

FABRICATION_CASES: list[FabricationCase] = [
    (
        "neg-fabricated-answer",
        "Does the runbook name a fallback region for the ingest queue?",
        "The runbook states no fallback region is configured for the ingest queue.",
        True,
        "Yes — the fallback region is eu-west-9, configured by the platform team in Q2.",
        "Ingest queue: primary region only. No fallback region is configured at this time.",
        "wrong",
    ),
    (
        "invented-awarding-body",
        "What score did the pilot review give the migration plan?",
        "The pilot review scored the migration plan 8.4 out of 10.",
        False,
        "The migration plan scored 8.4/10, awarded by the Continental Migration Standards "
        "Board, reviewed by Dr. Helena Voss.",
        "Pilot review score: 8.4/10.",
        "partial",
    ),
    (
        "invented-governance-body",
        "Did the team complete the transition to the new ticketing system?",
        "Yes, the team completed the transition to the new ticketing system.",
        False,
        "Yes, the transition was completed, approved by the Cross-Regional Steering Board "
        "under officer Priya Nandakumar.",
        "Transition to the new ticketing system: complete.",
        "partial",
    ),
    (
        "invented-audit-venue",
        "What percentage of tickets passed the quarterly audit?",
        "92% of tickets passed the quarterly audit.",
        False,
        "92% passed, per the audit conducted by the Meridian Audit Collective at their "
        "Lisbon summit.",
        "Quarterly audit result: 92% pass rate.",
        "partial",
    ),
    (
        "invented-lab-cohort-funder",
        "What effect size did the study report for the intervention?",
        "The study reported an effect size of 0.42.",
        False,
        "Effect size 0.42, measured by the Aurora Behavioral Lab across a cohort of 340, "
        "funded by the Whitfield Foundation.",
        "Reported effect size: 0.42.",
        "partial",
    ),
]

# Hold-the-line rows: the fix must not re-break what v2 got right.
HOLD_THE_LINE_CASES: list[FabricationCase] = [
    (
        "hold-hedged-in-code-block",
        "What is the exact CLI flag to disable retries?",
        "The exact flag is --no-retry.",
        False,
        "The docs don't spell out the exact flag text, but here's the relevant snippet:\n"
        "```\n--no-retry\n```",
        "Retry behavior: pass --no-retry to disable.",
        "partial",
    ),
    (
        "hold-wrong-slot-fill",
        "Who chairs the release review?",
        "Maria Alvarez chairs the release review.",
        False,
        "Maria Alvarez is the release manager, not the chair.",
        "Release review chair: Maria Alvarez. Release manager: Jon Petit.",
        "partial",
    ),
    (
        "hold-secondary-omission-still-correct",
        "What version shipped the new indexer?",
        "Version 4.2 shipped the new indexer, sponsored by the platform guild.",
        False,
        "Version 4.2 shipped the new indexer.",
        "v4.2 release notes: new indexer, sponsored by the platform guild.",
        "correct",
    ),
    (
        "hold-plain-correct",
        "How many regions does the deploy cover?",
        "The deploy covers 3 regions.",
        False,
        "The deploy covers 3 regions.",
        "Deploy footprint: 3 regions.",
        "correct",
    ),
]

ALL_CASES = FABRICATION_CASES + HOLD_THE_LINE_CASES


def _judge_user_prompt(case: FabricationCase) -> str:
    _id, question, expected, _negative, system_answer, excerpt, _expect = case
    return (
        f"QUESTION:\n{question}\n\n"
        f"EXPECTED ANSWER:\n{expected}\n\n"
        f"SYSTEM ANSWER:\n{system_answer}\n\n"
        f"TOP RETRIEVED NODES:\n(none)\n\n"
        f"SOURCE EXCERPTS:\n- [fixture.md] …{excerpt}…\n"
    )


def _model_available() -> bool:
    if not BASE_URL:
        return False
    try:
        with urllib.request.urlopen(BASE_URL.rstrip("/") + "/models", timeout=3) as response:
            ids = {row["id"] for row in json.load(response).get("data", [])}
        return MODEL in ids
    except Exception:
        return False


requires_llm = pytest.mark.skipif(
    not _model_available(),
    reason=f"judge model {MODEL} not available at {BASE_URL}/models",
)


@pytest.mark.slow
@requires_llm
@pytest.mark.parametrize("case", ALL_CASES, ids=[c[0] for c in ALL_CASES])
def test_fabrication_fixture_live(case: FabricationCase) -> None:
    case_id, _q, _expected, negative, _sys, _excerpt, expect = case
    user = _judge_user_prompt(case)
    raw = j._llm_chat(BASE_URL, MODEL, j._RUBRIC, user, max_tokens=400)
    parsed = j._parse_verdict(raw)
    if "key_facts" in parsed:
        verdict = j.compose_verdict(parsed["key_facts"], parsed["fabrication"], negative=negative)
    else:
        verdict = parsed.get("verdict", "unparseable")
    if expect == "correct":
        assert verdict == "correct", (case_id, verdict, parsed)
    else:
        # fabrication analogues: never "correct" (Q1); hold-the-line partial
        # rows: exactly "partial".
        assert verdict != "correct", (case_id, verdict, parsed)
        assert verdict == expect, (case_id, verdict, parsed)
    if case_id.startswith("invented-") or case_id == "neg-fabricated-answer":
        if "key_facts" in parsed:
            assert parsed["fabrication"] is True or verdict == "wrong", (case_id, parsed)


if __name__ == "__main__":
    if not _model_available():
        print(f"SKIP: judge model {MODEL} not available at {BASE_URL}/models")
        raise SystemExit(0)
    failures = 0
    for case in ALL_CASES:
        case_id, _q, _expected, negative, _sys, _excerpt, expect = case
        raw = j._llm_chat(BASE_URL, MODEL, j._RUBRIC, _judge_user_prompt(case), max_tokens=400)
        parsed = j._parse_verdict(raw)
        verdict = (
            j.compose_verdict(parsed["key_facts"], parsed["fabrication"], negative=negative)
            if "key_facts" in parsed
            else parsed.get("verdict", "unparseable")
        )
        ok = (
            verdict == expect
            if expect == "correct"
            else (verdict != "correct" and verdict == expect)
        )
        print(f"[{case_id}] verdict={verdict} expect={expect} {'OK' if ok else 'DEFECT'}")
        failures += 0 if ok else 1
    raise SystemExit(1 if failures else 0)
