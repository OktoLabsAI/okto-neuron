#!/usr/bin/env python3
"""Live semantic-judge wrong-answer rejection over fictional examples.

Every candidate deliberately overlaps its reference while changing the meaning:
wrong entity or number, negation, swapped quantities, off-by-one counts, or a
fabrication against an abstention reference. The live judge must never label a
wholly wrong candidate ``CORRECT``.

Run with either::

    uv run python tests/golden/bin/test_semantic_judge_adversarial.py
    uv run pytest tests/golden/bin/test_semantic_judge_adversarial.py -m slow -xvs
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import semantic_judge as sj  # noqa: E402

BASE_URL = os.environ.get("OKTO_NEURON_LLM_BASE_URL", "").strip()
MODEL = os.environ.get("OKTO_NEURON_JUDGE_MODEL", "unsloth/Qwen3.6-27B-NVFP4").strip()

# id, question, reference answer, grounding quote, required facts, wrong candidate, failure mode
CASES: list[tuple[str, str, str, str, list[str], str, str]] = [
    (
        "wrong-number",
        "Which port does SearchRelay listen on?",
        "SearchRelay listens on port 8989.",
        "SearchRelay service port: 8989.",
        ["8989"],
        "SearchRelay listens on port 7878.",
        "right schema, wrong number",
    ),
    (
        "wrong-subdomain",
        "Where is ArchiveBox served?",
        "ArchiveBox is served on docs.example.test.",
        "Public URL: https://docs.example.test",
        ["docs.example.test"],
        "ArchiveBox is served on media.example.test.",
        "right domain root, wrong subdomain",
    ),
    (
        "negated-token",
        "Which database image does LedgerDB use?",
        "LedgerDB uses postgres:latest.",
        "image: postgres:latest",
        ["postgres:latest"],
        "LedgerDB does not use postgres:latest; it pins postgres:16.",
        "negation carries the exact required token",
    ),
    (
        "swapped-counts",
        "What did the PhotoShelf import report?",
        "It imported 2,400 files into 80 albums with 0 errors.",
        "Import complete: 2,400 files, 80 albums, 0 errors.",
        ["2,400", "80", "0 errors"],
        "It imported 80 files into 2,400 albums with 0 errors.",
        "all facts present but files and albums swapped",
    ),
    (
        "meaning-flipped",
        "What did post-validation find?",
        "It verified 1,054 of 1,062 files; 8 were missing.",
        "Verified: 1,054 / 1,062. Missing: 8.",
        ["1,054", "1,062", "8"],
        "It found 1,054 of 1,062 files missing; only 8 were verified.",
        "tokens present but verified and missing are reversed",
    ),
    (
        "off-by-one",
        "What did post-validation find?",
        "It verified 1,054 of 1,062 files; 8 were missing.",
        "Verified: 1,054 / 1,062. Missing: 8.",
        ["1,054", "1,062", "8"],
        "It verified 1,055 of 1,062 files; 7 were missing.",
        "plausible off-by-one counts",
    ),
    (
        "wrong-role",
        "What is Morgan's role?",
        "Morgan is the client-services vice president.",
        "Morgan — Vice President, Client Services.",
        ["Client Services"],
        "Morgan is the vice president of engineering.",
        "right person, wrong role",
    ),
    (
        "fabricated-service",
        "Does the material identify a self-hosted source-control service?",
        "No. The material provides no evidence of one.",
        "No source-control service is listed.",
        [],
        "Yes, a self-hosted Forge service runs on port 3000.",
        "fabrication against an abstention reference",
    ),
]


def _judge_case(case: tuple[str, str, str, str, list[str], str, str]) -> dict:
    _, question, answer, quote, must_contain, candidate, _ = case
    reference = answer
    if must_contain:
        reference += "\n(Required facts: " + "; ".join(must_contain) + ")"
    return sj.judge_item(
        question,
        reference,
        [quote],
        candidate,
        base_url=BASE_URL,
        model=MODEL,
        must_contain=must_contain,
    )


_LLM_UP = bool(BASE_URL) and sj._llm_models_reachable(BASE_URL)


def _model_available() -> bool:
    try:
        import urllib.request

        with urllib.request.urlopen(BASE_URL.rstrip("/") + "/models", timeout=3) as response:
            ids = {row["id"] for row in json.load(response).get("data", [])}
        return MODEL in ids
    except Exception:
        return False


requires_llm = pytest.mark.skipif(
    not (_LLM_UP and _model_available()),
    reason=f"judge model {MODEL} not available at {BASE_URL}/models",
)


@pytest.mark.slow
@requires_llm
@pytest.mark.parametrize("case", CASES, ids=[case[0] for case in CASES])
def test_wrong_answer_not_marked_correct(case) -> None:
    result = _judge_case(case)
    assert result["verdict"] != "CORRECT", (case[0], case[-1], result)


def _run_live() -> int:
    if not BASE_URL:
        print("SKIP: OKTO_NEURON_LLM_BASE_URL is required for live judge tests")
        return 0
    if not (_LLM_UP and _model_available()):
        print(f"SKIP: judge model {MODEL} not available at {BASE_URL}/models")
        return 0
    defects: list[tuple[str, str, dict]] = []
    print(f"Adversarial wrong-answer rejection — model={MODEL}\n")
    for case in CASES:
        result = _judge_case(case)
        stability = [result["verdict"]]
        if result["verdict"] in {"CORRECT", "unparseable"}:
            stability.extend(_judge_case(case)["verdict"] for _ in range(2))
        if "CORRECT" in stability:
            defects.append((case[0], case[-1], result))
        print(f"[{case[0]}] verdict={result['verdict']} reruns={stability}")
    print(f"\n{len(CASES)} cases; hard defects: {len(defects)}")
    for case_id, label, result in defects:
        print(f"DEFECT {case_id}: {label}; reason={result.get('reason')}")
    return 1 if defects else 0


if __name__ == "__main__":
    raise SystemExit(_run_live())
