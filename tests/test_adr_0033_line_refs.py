"""ADR 0033 must not cite eval-run.sh line numbers that drift out from under it.

The deep-review flagged ADR 0033 for citing `eval-run.sh:70` and
`eval-run.sh:202-211` for the judge-model default and the resolve-once block; by
the time of the review the real lines were 73 and ~360-369. ADRs are never
regenerated (unlike docs/*.html), so a line-number citation goes stale on the
next unrelated edit to the script. This test pins the fix: the ADR must name the
resolve-once mechanism by variable/function identifiers instead of line numbers,
and those identifiers must actually exist in the script it describes.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
ADR = ROOT / "docs" / "adr" / "0033-eval-harness-pins-one-judge-model.md"
EVAL_RUN_SH = ROOT / "tests" / "golden" / "bin" / "eval-run.sh"

# Matches the stale-citation shape the review flagged, e.g. "eval-run.sh:70" or
# "eval-run.sh:202-211" — a bare script-name-colon-line(s) reference.
_STALE_LINE_CITATION = re.compile(r"eval-run\.sh:\d+(?:-\d+)?")


def test_adr_file_exists():
    assert ADR.is_file()


def test_adr_does_not_cite_eval_run_sh_line_numbers():
    text = ADR.read_text(encoding="utf-8")
    matches = _STALE_LINE_CITATION.findall(text)
    assert not matches, (
        "ADR 0033 cites eval-run.sh by line number, which drifts on the next "
        f"unrelated script edit — found {matches!r}; reference the "
        "variable/function name instead (e.g. `JUDGE_MODEL`, "
        "`semantic_judge._select_chat_model`)."
    )


def test_adr_referenced_identifiers_exist_in_eval_run_sh():
    adr_text = ADR.read_text(encoding="utf-8")
    script_text = EVAL_RUN_SH.read_text(encoding="utf-8")

    assert "JUDGE_MODEL" in adr_text
    assert 'JUDGE_MODEL="${OKTO_NEURON_JUDGE_MODEL:-' in script_text, (
        "ADR 0033 documents a JUDGE_MODEL default that no longer exists in eval-run.sh"
    )

    assert "_select_chat_model" in adr_text
    assert "_select_chat_model(" in script_text, (
        "ADR 0033 documents a semantic_judge._select_chat_model() resolve call "
        "that no longer exists in eval-run.sh"
    )

    assert 'if [[ "$JUDGE_MODEL" == "auto" ]]' in script_text, (
        "ADR 0033 documents the auto-resolve-once guard by its condition; that "
        "exact guard no longer exists in eval-run.sh"
    )
