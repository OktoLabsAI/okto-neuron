"""First-import race on ``okto_neuron.reconcile`` (refs #40).

A reconcile-propose job died with ``ImportError: cannot import name
'ReconcileQueue' from partially initialized module 'okto_neuron.reconcile.queue'``
when the job thread first-imported ``reconcile.candidates`` while a request thread
first-imported ``reconcile.queue``. Each run is a NEW interpreter so the import
state is genuinely cold; the two threads are released together by a barrier.
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


def _queue():
    barrier.wait()
    try:
        import okto_neuron.reconcile.queue  # noqa: F401
    except BaseException:
        errors.append(traceback.format_exc())


def _candidates():
    barrier.wait()
    try:
        from okto_neuron.reconcile.candidates import generate_candidate_clusters  # noqa: F401
    except BaseException:
        errors.append(traceback.format_exc())


threads = [threading.Thread(target=_queue), threading.Thread(target=_candidates)]
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
def test_reconcile_first_import_race_is_clean() -> None:
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(_one_run, range(ITERATIONS)))
    failures = [r for r in results if r is not None]
    assert not failures, (
        f"{len(failures)}/{ITERATIONS} cold-interpreter iterations failed; first error:\n{failures[0]}"
    )
