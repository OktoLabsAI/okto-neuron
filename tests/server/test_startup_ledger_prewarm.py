"""The daemon warms the ledger indexes of opened startup vaults in the background (#14)."""

from __future__ import annotations

import asyncio
import json
import signal
import socket
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from okto_neuron.consolidate import ledger as ledger_mod
from okto_neuron.consolidate.ledger import LEDGER_FILENAME, CandidateLedger
from okto_neuron.server import runtime
from okto_neuron.vault import Vault
from tests.consolidate.test_ledger_run_view_cache import _Loads
from tests.support._ledger_synth import build_synthetic_ledger

REAL_QUEUE_GATE = True  # keep the real v1 layout refusal (see conftest)
_BIG = 10 * 1024 * 1024  # past the inline-tail threshold so the snapshot build runs


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _vault_with_cold_ledger(path: Path, **synth: int) -> tuple[Path, str]:
    vault = Vault.init(path, packs=["core"])
    root = Path(vault.path).resolve(strict=False)
    vault.close()
    marg = root / ".marginalia"
    marg.mkdir(exist_ok=True)
    build_synthetic_ledger(marg, target_bytes=_BIG, **synth)
    ledger_mod._sidecar_cache_drop()
    with ledger_mod._LEDGER_INDEXES_GUARD:
        ledger_mod._LEDGER_INDEXES.clear()
    CandidateLedger(marg).index_path.unlink(missing_ok=True)
    return root, str((marg / LEDGER_FILENAME).resolve())


def _health(port: int) -> int:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as response:
        return response.status


def _isolate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OKTO_NEURON_MLFLOW_TRACKING_URI", raising=False)
    monkeypatch.delenv("MARGINALIA_MLFLOW_TRACKING_URI", raising=False)


async def _serve(root: Path, ready: asyncio.Event) -> asyncio.Task:
    task = asyncio.create_task(
        runtime._run_async(root, rest_port=_free_port(), mcp_port=_free_port(), ready_event=ready)
    )
    await asyncio.wait_for(ready.wait(), 120)
    return task


async def _stop(task: asyncio.Task) -> None:
    signal.raise_signal(signal.SIGTERM)
    await asyncio.wait_for(task, 60)


def test_warm_runs_after_readiness_and_populates_both_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    root, key = _vault_with_cold_ledger(tmp_path / "v")
    gate, entered = threading.Event(), threading.Event()
    real = CandidateLedger.prewarm

    def gated(self: CandidateLedger, cancelled=None) -> None:
        entered.set()
        assert gate.wait(60)
        real(self, cancelled)

    monkeypatch.setattr(CandidateLedger, "prewarm", gated)

    async def scenario() -> None:
        ready = asyncio.Event()
        rest_port = _free_port()
        task = asyncio.create_task(
            runtime._run_async(root, rest_port=rest_port, mcp_port=_free_port(), ready_event=ready)
        )
        await asyncio.wait_for(ready.wait(), 120)
        try:
            assert await asyncio.to_thread(entered.wait, 30)
            # The warm is parked mid-flight: readiness and /health are unaffected.
            assert key not in ledger_mod._SIDECARS and key not in ledger_mod._LEDGER_INDEXES
            deadline = time.monotonic() + 30
            while True:
                try:
                    assert await asyncio.to_thread(_health, rest_port) == 200
                    break
                except OSError:
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.2)
            assert key not in ledger_mod._SIDECARS
            gate.set()
            deadline = time.monotonic() + 120
            while key not in ledger_mod._LEDGER_INDEXES or key not in ledger_mod._SIDECARS:
                assert time.monotonic() < deadline, "warm never populated the indexes"
                await asyncio.sleep(0.2)
        finally:
            gate.set()
            await _stop(task)

    asyncio.run(scenario())


def test_refused_v1_vault_ledger_is_never_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate(monkeypatch, tmp_path)
    from tests.server.test_startup_v1_vault_refused import _make_v1_vault

    old = _make_v1_vault(tmp_path / "old")
    build_synthetic_ledger(old / ".marginalia", target_bytes=64 * 1024)
    touched: list[str] = []

    def record(self: CandidateLedger, *args: object) -> bool:
        import traceback

        touched.append("".join(traceback.format_stack(limit=12)))
        return True

    monkeypatch.setattr(CandidateLedger, "prewarm", record)
    monkeypatch.setattr(CandidateLedger, "_prepare_sidecar", record)

    async def scenario() -> None:
        ready = asyncio.Event()
        task = await _serve(old, ready)
        await asyncio.sleep(1.0)
        await _stop(task)

    asyncio.run(scenario())
    assert touched == []


def test_shutdown_during_a_warm_finishes_within_budget_and_ends_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _isolate(monkeypatch, tmp_path)
    root, _ = _vault_with_cold_ledger(tmp_path / "v")
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(runtime, "SHUTDOWN_DRAIN_TIMEOUT", 2.0)

    def stuck(self: CandidateLedger, cancelled=None) -> None:
        entered.set()
        release.wait(8)  # an uninterruptible build step outliving the drain budget

    monkeypatch.setattr(CandidateLedger, "prewarm", stuck)
    caplog.set_level("INFO")
    seen: list[asyncio.Task] = []
    task_state: list[bool] = []

    async def scenario() -> None:
        ready = asyncio.Event()
        task = await _serve(root, ready)
        try:
            assert await asyncio.to_thread(entered.wait, 30)
            seen.extend(
                t for t in asyncio.all_tasks() if t.get_name() == "okto-neuron-ledger-prewarm"
            )
            assert seen and not seen[0].done()
            signal.raise_signal(signal.SIGTERM)
            await asyncio.wait({seen[0]}, timeout=5)
            task_state.append(seen[0].done())
            await asyncio.wait_for(task, 60)
        finally:
            release.set()

    asyncio.run(scenario())
    # The warm task ends as soon as the daemon stops, long before the thread does.
    assert task_state == [True] and not seen[0].cancelled()
    summary = [r.getMessage() for r in caplog.records if "shutdown.summary" in r.getMessage()]
    assert len(summary) == 1, summary
    total_ms = int(summary[0].split("total_ms=")[1].split()[0])
    # drain 2s + close budget 5s: the graceful path never waits on the warm beyond it.
    assert total_ms < 7_000, summary[0]


@pytest.mark.parametrize(
    ("warm", "expect_big_parse"), [(True, False), (False, True)], ids=["warmed", "warm-disabled"]
)
def test_first_ui_summary_after_the_startup_warm_parses_nothing_big(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, warm: bool, expect_big_parse: bool
) -> None:
    """GET /api/v1/ledger/summary right after the warm: zero big json parses.

    The ``warm-disabled`` case is the negative control: with the warm a no-op the
    same request must cold-parse the multi-MB plan rows, so the assertion has teeth.
    """
    _isolate(monkeypatch, tmp_path)
    # plan rows of ~1.2 MB (> the counter's threshold), inside a ~10 MB ledger
    root, _ = _vault_with_cold_ledger(tmp_path / "v", plan_ops=6, op_chars=200_000)
    if not warm:
        monkeypatch.setattr(CandidateLedger, "prewarm", lambda self, cancelled=None: None)
    loads = _Loads(monkeypatch)
    body: list[dict] = []

    def summary(port: int) -> dict:
        url = f"http://127.0.0.1:{port}/api/v1/ledger/summary?limit=12"
        with urllib.request.urlopen(url, timeout=120) as response:
            return json.loads(response.read())

    async def scenario() -> None:
        ready = asyncio.Event()
        rest_port = _free_port()
        task = asyncio.create_task(
            runtime._run_async(root, rest_port=rest_port, mcp_port=_free_port(), ready_event=ready)
        )
        await asyncio.wait_for(ready.wait(), 120)
        try:
            deadline = time.monotonic() + 30
            while not (
                warmers := [
                    t for t in asyncio.all_tasks() if t.get_name() == "okto-neuron-ledger-prewarm"
                ]
            ):
                assert time.monotonic() < deadline, "no warm task was started"
                await asyncio.sleep(0.05)
            await asyncio.wait_for(asyncio.gather(*warmers), 120)
            while True:
                try:
                    await asyncio.to_thread(_health, rest_port)
                    break
                except OSError:
                    assert time.monotonic() < deadline
                    await asyncio.sleep(0.2)
            loads.reset()  # the warm itself may parse; only the UI request is measured
            body.append(await asyncio.to_thread(summary, rest_port))
        finally:
            await _stop(task)

    asyncio.run(scenario())
    assert body[0]["status"] == "ok" and body[0]["run"] is not None
    assert (loads.big > 0) is expect_big_parse, loads.sizes
