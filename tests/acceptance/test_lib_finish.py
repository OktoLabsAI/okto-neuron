"""Regression test for marginalia-deep-review.md 3.1.

`tests/acceptance/scenarios/_lib.sh`'s `finish()` must never report
status=pass when a caller passes a known_bug/failure reason (the common
`start_server "$VAULT" || finish "reason"` pattern), even though
`_failures` is still empty at that point. See
tests/acceptance/test_lib_finish_regression.sh for the actual bash-level
repro; this file only makes it run as part of the pytest gate
(`uv run pytest` collects `test_*.py`, not the acceptance harness's
`[0-9][0-9]_*.sh` glob or a bare `test_*.sh` file).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).parent / "test_lib_finish_regression.sh"


def test_finish_never_reports_pass_after_start_server_failure() -> None:
    result = subprocess.run(
        ["bash", str(_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        "tests/acceptance/test_lib_finish_regression.sh failed "
        f"(rc={result.returncode}):\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "PASS: start_server() failure correctly reported status=" in result.stdout
