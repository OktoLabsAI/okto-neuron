"""SMOKE GUARD: the real ``okto-neuron serve`` daemon survives concurrent first imports (refs #40).

This does NOT reproduce the race: it also passes 20/20 on the unfixed 4e564fc build.
The proof of the fix is the strict-xfail barrier tests (tests/reconcile and
tests/predicates ``test_import_race.py``) plus the preload ordering assertions in
``test_preload.py``. This test only checks the real daemon end to end.

Each iteration is a FRESH daemon process (an in-process server would find every
module already in ``sys.modules`` and could never reproduce a cold-import race).
At the first ``/health`` 200 we fire, at once, the requests that first-import the
reconcile and predicate packages on worker threads (the two propose jobs, the
queue summary behind the status path) plus ``/api/v1/status``, then require that
no ImportError appears in any job record or in the daemon's output.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from okto_neuron.vault import Vault

ITERATIONS = 20
HEALTH_DEADLINE_S = 60
JOBS_DEADLINE_S = 30
_SERVE = "from okto_neuron.cli import app; app()"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _call(method: str, url: str, body: dict | None = None) -> tuple[int, str]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _one_daemon(home: Path) -> list[str]:
    """Start one daemon, race the first imports, return the problems found."""
    rest, mcp = _free_port(), _free_port()
    vault = Vault.init(home / "vault", packs=["core"])
    vault_path = str(Path(vault.path).resolve(strict=False))
    vault.close()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    if "PYTHONPATH" in os.environ:
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _SERVE,
            "serve",
            "--no-open",
            "--vault",
            vault_path,
            "--port",
            str(rest),
            "--mcp-port",
            str(mcp),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{rest}"
    problems: list[str] = []
    try:
        deadline = time.monotonic() + HEALTH_DEADLINE_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return [f"daemon exited early with {proc.returncode}"]
            try:
                if _call("GET", f"{base}/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.005)
        else:
            return ["no /health 200 within the deadline"]
        calls = [
            ("POST", "/api/v1/reconcile/propose", {}),
            ("POST", "/api/v1/upkeep/predicates/propose", {}),
            ("GET", "/api/v1/status", None),
            ("GET", "/api/v1/reconcile/status", None),
            ("GET", "/api/v1/reconcile/queue", None),
        ]
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            results = list(pool.map(lambda c: _call(c[0], base + c[1], c[2]), calls))
        for (method, path, _), (status, text) in zip(calls, results):
            if "ImportError" in text or status >= 500:
                problems.append(f"{method} {path} -> {status}: {text[:300]}")
        jobs_text = ""
        deadline = time.monotonic() + JOBS_DEADLINE_S
        while time.monotonic() < deadline:
            _, jobs_text = _call("GET", f"{base}/api/v1/curation/jobs")
            jobs = json.loads(jobs_text).get("jobs", [])
            if jobs and all(j["status"] not in ("queued", "running") for j in jobs):
                break
            time.sleep(0.05)
        else:
            problems.append(f"jobs did not settle: {jobs_text[:300]}")
        if "ImportError" in jobs_text or "partially initialized" in jobs_text:
            problems.append(f"import failure in a job record: {jobs_text[:600]}")
    finally:
        proc.terminate()
        try:
            output, _ = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            output, _ = proc.communicate()
    if "ImportError" in output or "partially initialized" in output:
        problems.append(f"import failure in daemon output: {output[-600:]}")
    return problems


def test_concurrent_first_imports_never_fail_in_a_fresh_daemon(tmp_path: Path) -> None:
    failures: dict[int, list[str]] = {}
    for i in range(ITERATIONS):
        home = tmp_path / f"home{i}"
        home.mkdir()
        problems = _one_daemon(home)
        if problems:
            failures[i] = problems
    assert not failures, f"{len(failures)}/{ITERATIONS} daemon starts failed: {failures}"
