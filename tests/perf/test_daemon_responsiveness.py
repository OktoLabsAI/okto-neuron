"""Real-server responsiveness under the web UI's poll mix (#13 / #18).

Starts the real ``okto-neuron serve`` process on free ports (never 7777/8201)
with an isolated HOME, against a synthetic real-scale vault (see
``_synthetic_vault.py``: 4k nodes / 13k edges / 2.5k Claims / 2k review items),
replays the pages' poll mix, and measures what a user and an agent feel:

* ``/health`` latency p50 / p99 / max, probed every 100 ms;
* MCP connect + ``list_vaults`` latency p99, one fresh connection every 2 s.

Fails when ``/health`` p99 >= 100 ms or MCP connect p99 >= 1 s.

Skipped unless ``OKTO_NEURON_PERF=1`` (and deselected by ``-m 'not perf'``).
CI runs it nightly and on demand in ``.github/workflows/daemon-perf.yml`` (never
on push/PR; the grafx fixture is cached between runs), equivalent to::

    OKTO_NEURON_PERF=1 OKTO_NEURON_PERF_CACHE=$RUNNER_TEMP/okto-neuron-perf \\
      uv run --extra ladybug --extra grafx python -m pytest -m perf -s \\
      tests/perf/test_daemon_responsiveness.py

Knobs: ``OKTO_NEURON_PERF_DURATION_S`` (default 60), ``OKTO_NEURON_PERF_BACKEND``
(``grafx`` default, or ``ladybug``), ``OKTO_NEURON_PERF_REPORT`` (write the JSON
summary to this path), ``OKTO_NEURON_PERF_SCALE`` (fixture fraction, default 1.0;
below 1 only smoke-tests the harness and is never a valid result).
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from tests.perf._synthetic_vault import cached_synthetic_vault, scaled

pytestmark = [pytest.mark.perf]

HEALTH_P99_MAX_S = 0.100
MCP_CONNECT_P99_MAX_S = 1.0

# (path, period seconds, concurrent pollers) — the pages' own poll cadence.
_POLL_MIX: tuple[tuple[str, float, int], ...] = (
    ("/api/v1/upkeep/predicates", 5.0, 2),
    ("/api/v1/upkeep/predicates", 15.0, 1),
    ("/api/v1/review-queue", 5.0, 1),
    ("/api/v1/graph/stats", 10.0, 1),
    ("/api/v1/curation/jobs", 5.0, 1),
    ("/api/v1/reconcile/status", 5.0, 1),
    ("/api/v1/curation/scheduler", 5.0, 1),
    ("/api/v1/ledger/summary", 5.0, 1),
    ("/api/v1/ingest-queue", 5.0, 1),
    ("/api/v1/status", 10.0, 1),
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def _summary(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "p50_ms": round(statistics.median(values) * 1000, 1),
        "p99_ms": round(_pct(values, 0.99) * 1000, 1),
        "max_ms": round(max(values) * 1000, 1),
    }


async def _probe_health(client: httpx.AsyncClient, stop: float, out: list[float]) -> None:
    while time.monotonic() < stop:
        started = time.perf_counter()
        response = await client.get("/health", timeout=30)
        out.append(time.perf_counter() - started)
        assert response.status_code == 200
        await asyncio.sleep(0.1)


async def _poll(
    client: httpx.AsyncClient,
    path: str,
    period: float,
    stop: float,
    out: dict[str, list[float]],
    statuses: dict[str, list[int]],
) -> None:
    while time.monotonic() < stop:
        started = time.perf_counter()
        response = await client.get(path, timeout=300)
        out.setdefault(path, []).append(time.perf_counter() - started)
        statuses.setdefault(path, []).append(response.status_code)
        await asyncio.sleep(max(0.0, period - (time.perf_counter() - started)))


async def _probe_mcp(url: str, token: str, stop: float, out: list[float]) -> None:
    from fastmcp import Client

    while time.monotonic() < stop:
        started = time.perf_counter()
        async with Client(url, auth=token) as client:
            await client.call_tool("list_vaults", {})
        out.append(time.perf_counter() - started)
        await asyncio.sleep(2.0)


def test_daemon_stays_responsive_under_ui_poll_mix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.environ.get("OKTO_NEURON_PERF") != "1":
        pytest.skip("set OKTO_NEURON_PERF=1 to run the daemon responsiveness load test")

    backend = os.environ.get("OKTO_NEURON_PERF_BACKEND", "grafx")
    duration = float(os.environ.get("OKTO_NEURON_PERF_DURATION_S", "60"))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    shape = scaled(float(os.environ.get("OKTO_NEURON_PERF_SCALE", "1.0")))
    vault_path, build_s = cached_synthetic_vault(tmp_path / "vault", backend=backend, shape=shape)

    rest_port, mcp_port = _free_port(), _free_port()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OKTO_NEURON_MLFLOW", "MARGINALIA_"))
    }
    env["HOME"] = str(home)
    log_path = tmp_path / "serve.log"
    server = subprocess.Popen(
        [
            str(Path(sys.executable).parent / "okto-neuron"),
            "serve",
            "--vault",
            str(vault_path),
            "--port",
            str(rest_port),
            "--mcp-port",
            str(mcp_port),
            "--no-open",
            "--log-file",
            str(log_path),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        from okto_neuron.server.runtime import auth_token_path

        base = f"http://127.0.0.1:{rest_port}"
        deadline = time.monotonic() + 180
        while True:
            assert server.poll() is None, log_path.read_text(encoding="utf-8", errors="replace")
            try:
                if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            assert time.monotonic() < deadline, "server did not become healthy"
            time.sleep(0.25)
        token = auth_token_path(vault_path, rest_port).read_text(encoding="utf-8").strip()

        async def _run() -> dict[str, object]:
            health: list[float] = []
            mcp: list[float] = []
            polls: dict[str, list[float]] = {}
            statuses: dict[str, list[int]] = {}
            stop = time.monotonic() + duration
            async with httpx.AsyncClient(base_url=base) as client:
                await asyncio.gather(
                    _probe_health(client, stop, health),
                    _probe_mcp(f"http://127.0.0.1:{mcp_port}/mcp", token, stop, mcp),
                    *(
                        _poll(client, path, period, stop, polls, statuses)
                        for path, period, count in _POLL_MIX
                        for _ in range(count)
                    ),
                )
            return {
                "backend": backend,
                "fixture": {"nodes": shape.nodes, "edges": shape.edges, "build_s": build_s},
                "duration_s": duration,
                "health": _summary(health),
                "mcp_connect_list_vaults": _summary(mcp),
                "polls": {
                    path: {**_summary(values), "statuses": sorted(set(statuses[path]))}
                    for path, values in polls.items()
                },
                "_raw": {"health": health, "mcp": mcp},
            }

        result = asyncio.run(_run())
    finally:
        server.terminate()
        try:
            server.wait(timeout=60)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=10)

    raw = result.pop("_raw")
    report = json.dumps(result, indent=2)
    print(report)
    if os.environ.get("OKTO_NEURON_PERF_REPORT"):
        Path(os.environ["OKTO_NEURON_PERF_REPORT"]).write_text(report + "\n", encoding="utf-8")
    assert raw["health"] and raw["mcp"]
    assert _pct(raw["health"], 0.99) < HEALTH_P99_MAX_S, report
    assert _pct(raw["mcp"], 0.99) < MCP_CONNECT_P99_MAX_S, report
