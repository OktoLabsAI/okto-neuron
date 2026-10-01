"""First-import race on ``okto_neuron.predicates`` (refs #40).

Same hazard as ``tests/reconcile/test_import_race.py``: two threads cold-importing
different submodules of a package whose ``__init__`` imports them eagerly. Each run
is a NEW interpreter; the threads are released together by a barrier. The daemon
avoids this with its single-threaded import phase, so this documents the hazard
and must stay a strict xfail (if it ever passes, the marker must go).
"""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

ITERATIONS = 50
PER_RUN_TIMEOUT_S = 60

_RACE = """
import threading, traceback

barrier = threading.Barrier(2)
errors = []


def _first():
    barrier.wait()
    try:
        import okto_neuron.predicates.candidates  # noqa: F401
    except BaseException:
        errors.append(traceback.format_exc())


def _second():
    barrier.wait()
    try:
        import okto_neuron.predicates.index  # noqa: F401
    except BaseException:
        errors.append(traceback.format_exc())


threads = [threading.Thread(target=_first), threading.Thread(target=_second)]
for t in threads:
    t.start()
for t in threads:
    t.join()
if errors:
    print("\\n".join(errors))
    raise SystemExit(1)
"""


def _one_run(_: int) -> str | None:
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _RACE],
            capture_output=True,
            text=True,
            timeout=PER_RUN_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return f"timed out after {PER_RUN_TIMEOUT_S}s (import deadlock?)"
    if proc.returncode != 0:
        return (proc.stdout + proc.stderr).strip()
    return None


@pytest.mark.xfail(
    strict=True,
    reason="eager-__init__ import inversion; daemon prevents it by a single-threaded "
    "import phase (issue #40)",
)
def test_predicates_first_import_race_is_clean() -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_one_run, range(ITERATIONS)))
    failures = [r for r in results if r is not None]
    assert not failures, (
        f"{len(failures)}/{ITERATIONS} cold-interpreter iterations failed; first error:\n{failures[0]}"
    )
