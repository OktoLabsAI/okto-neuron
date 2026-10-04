"""GC tuning at startup and the slow-pause watch (refs #38).

Process-wide gc state (thresholds, frozen objects, the callback, the module flag) is
restored by the autouse ``_restore_gc_state`` fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import signal
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from okto_neuron.config import OktoNeuronConfig
from okto_neuron.config._app_config import ServerSettings
from okto_neuron.server import _gc_tuning as gct
from okto_neuron.server import http as http_mod
from okto_neuron.server import runtime
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests
from okto_neuron.vault import Vault

LOGGER = "okto_neuron.server._gc_tuning"
_NO_CONFIG = SimpleNamespace()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GC_TUNING", "GC_THRESHOLDS", "GC_WATCH"):
        monkeypatch.delenv(f"OKTO_NEURON_{name}", raising=False)
        monkeypatch.delenv(f"MARGINALIA_{name}", raising=False)


@pytest.fixture
def freeze_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real = gc.freeze

    def spy() -> None:
        calls.append("freeze")
        real()

    monkeypatch.setattr(gc, "freeze", spy)
    return calls


def _clock(monkeypatch: pytest.MonkeyPatch, *values: float) -> None:
    it = iter(values)
    monkeypatch.setattr(gct, "_clock", lambda: next(it))


def _pause(monkeypatch: pytest.MonkeyPatch, seconds: float, generation: int = 2) -> None:
    _clock(monkeypatch, 0.0, seconds)
    gct._on_gc("start", {"generation": generation})
    gct._on_gc("stop", {"generation": generation, "collected": 0, "uncollectable": 0})


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.WARNING and r.name == LOGGER]


# -- configuration ------------------------------------------------------------------


def test_defaults_are_on_with_the_measured_thresholds() -> None:
    assert gct._resolve(_NO_CONFIG) == (True, (50000, 20, 100))


def test_config_off_and_values() -> None:
    assert gct._resolve(SimpleNamespace(gc_tuning=False)) == (False, (50000, 20, 100))
    assert gct._resolve(SimpleNamespace(gc_thresholds=[1000, 5, 7])) == (True, (1000, 5, 7))
    assert gct._resolve(SimpleNamespace(gc_thresholds="1000, 5,7"))[1] == (1000, 5, 7)


@pytest.mark.parametrize("raw", ["off", "OFF", "false", "0", "no"])
def test_env_off_switch(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("OKTO_NEURON_GC_TUNING", raw)
    assert gct._resolve(_NO_CONFIG)[0] is False


def test_env_overrides_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKTO_NEURON_GC_THRESHOLDS", "2000,3,4")
    monkeypatch.setenv("OKTO_NEURON_GC_TUNING", "on")
    cfg = SimpleNamespace(gc_tuning=False, gc_thresholds=[9, 9, 9])
    assert gct._resolve(cfg) == (True, (2000, 3, 4))


@pytest.mark.parametrize(
    "bad", ["1,2", "a,b,c", "0,1,2", "1,2,3,4", "", [1, 2], [1, 2, -3], [True, 2, 3], [1.5, 2, 3], 7]
)
def test_invalid_thresholds_warn_and_fall_back(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, bad: object
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert gct._resolve(SimpleNamespace(gc_thresholds=bad)) == (True, (50000, 20, 100))
    assert len(_warnings(caplog)) == 1
    caplog.clear()
    monkeypatch.setenv("OKTO_NEURON_GC_THRESHOLDS", str(bad))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert gct._resolve(_NO_CONFIG)[1] == (50000, 20, 100)
    assert len(_warnings(caplog)) == 1


def test_invalid_env_falls_through_to_valid_config(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OKTO_NEURON_GC_TUNING", "maybe")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert gct._resolve(SimpleNamespace(gc_tuning=False))[0] is False
    assert len(_warnings(caplog)) == 1


def test_app_config_accepts_garbage_gc_keys_without_failing_the_file() -> None:
    config = OktoNeuronConfig.model_validate(
        {"server": {"gc_tuning": "banana", "gc_thresholds": {"x": 1}, "store_workers": 3}}
    )
    assert config.server.store_workers == 3
    assert gct._resolve(config.server) == (True, (50000, 20, 100))
    assert ServerSettings().gc_tuning is True and ServerSettings().gc_thresholds is None


def test_unreadable_app_config_never_crashes_apply(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("broken toml")

    monkeypatch.setattr(OktoNeuronConfig, "load", classmethod(boom))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        report = gct.apply_gc_tuning()
    assert report["enabled"] and report["thresholds"] == [50000, 20, 100]
    assert len(_warnings(caplog)) == 1


# -- apply --------------------------------------------------------------------------


def test_apply_freezes_then_sets_thresholds_and_logs_once(
    freeze_spy: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    before = gc.get_threshold()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        report = gct.apply_gc_tuning(SimpleNamespace(gc_thresholds=[40000, 11, 12]))
    assert freeze_spy == ["freeze"]
    assert gc.get_threshold() == (40000, 11, 12)
    assert report["frozen"] is True and report["skipped"] is None
    assert report["previous_thresholds"] == list(before)
    assert report["frozen_objects"] > 0
    (info,) = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.INFO]
    message = info.getMessage()
    assert "(40000, 11, 12)" in message and str(before) in message
    assert f"froze {report['frozen_objects']} objects" in message


def test_apply_when_disabled_changes_nothing(freeze_spy: list[str]) -> None:
    before = gc.get_threshold()
    report = gct.apply_gc_tuning(SimpleNamespace(gc_tuning=False))
    assert freeze_spy == [] and gc.get_threshold() == before
    assert report["frozen"] is False and report["skipped"] == "disabled"
    assert gct.snapshot()["tuning_applied"] is False


def test_freeze_runs_at_most_once_per_process(
    freeze_spy: list[str], caplog: pytest.LogCaptureFixture
) -> None:
    gct.apply_gc_tuning(_NO_CONFIG)
    gc.set_threshold(1, 2, 3)  # a second apply must not touch the state either
    with caplog.at_level(logging.INFO, logger=LOGGER):
        second = gct.apply_gc_tuning(_NO_CONFIG)
    assert freeze_spy == ["freeze"]
    assert second["frozen"] is False and second["skipped"] == "already_applied"
    assert gc.get_threshold() == (1, 2, 3)
    assert any("already applied" in r.getMessage() for r in caplog.records)
    gct.reset_for_tests()  # the fixture's reset re-arms the flag
    gct.apply_gc_tuning(_NO_CONFIG)
    assert freeze_spy == ["freeze", "freeze"]


def test_serve_startup_freezes_after_the_startup_vault_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, freeze_spy: list[str]
) -> None:
    order = freeze_spy  # shared list: "open" then "freeze"
    real_open = runtime._open_startup_vault

    def spy_open(path: Path | None):  # noqa: ANN202
        result = real_open(path)
        order.append("open")
        return result

    monkeypatch.setattr(runtime, "_open_startup_vault", spy_open)
    vault = Vault.init(tmp_path / "v", packs=["core"])
    root = Path(vault.path).resolve(strict=False)
    vault.close()

    def free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    async def scenario() -> None:
        ready = asyncio.Event()
        task = asyncio.create_task(
            runtime._run_async(
                root, rest_port=free_port(), mcp_port=free_port(), ready_event=ready
            )
        )
        await asyncio.wait_for(ready.wait(), 120)
        try:
            assert gc.get_threshold() == (50000, 20, 100)
            assert gct.snapshot()["watch"] is True
        finally:
            signal.raise_signal(signal.SIGTERM)
            await asyncio.wait_for(task, 60)

    asyncio.run(scenario())
    assert order == ["open", "freeze"]


# -- watch --------------------------------------------------------------------------


def test_slow_pause_updates_counters_and_logs_one_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _pause(monkeypatch, 0.060, generation=2)
    snap = gct.snapshot()
    assert snap["collections"] == {"gen0": 0, "gen1": 0, "gen2": 1}
    assert snap["max_pause_ms"] == 60 and snap["total_pause_ms"] == 60
    assert snap["slow_pauses"] == 1 and snap["last_slow_pause_at"] > 0
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert gct.flush_pending(now=100.0) is True
        assert gct.flush_pending(now=100.1) is False  # nothing pending any more
    (record,) = _warnings(caplog)
    message = record.getMessage()
    assert "gen=2" in message and "60.0 ms" in message and "1 slow" in message
    assert "on thread MainThread" in message


def test_fast_pause_counts_but_never_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _pause(monkeypatch, 0.010, generation=1)
    snap = gct.snapshot()
    assert snap["collections"]["gen1"] == 1 and snap["slow_pauses"] == 0
    assert snap["max_pause_ms"] == 10 and snap["last_slow_pause_at"] is None
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert gct.flush_pending(now=100.0) is False
    assert _warnings(caplog) == []


def test_warning_is_rate_limited_and_folds_the_suppressed_pauses(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        _pause(monkeypatch, 0.070)
        assert gct.flush_pending(now=100.0) is True
        _pause(monkeypatch, 0.080)
        _pause(monkeypatch, 0.090)
        assert gct.flush_pending(now=102.0) is False  # inside the 5 s window
        assert gct.flush_pending(now=105.5) is True
    first, second = _warnings(caplog)
    assert "1 slow" in first.getMessage() and "70.0 ms" in first.getMessage()
    assert "2 slow" in second.getMessage() and "90.0 ms" in second.getMessage()
    assert gct.snapshot()["slow_pauses"] == 3


def test_callback_swallows_malformed_input() -> None:
    gct._on_gc("stop", {})  # no generation: must not raise inside the collector


def test_watch_off_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKTO_NEURON_GC_WATCH", "off")
    assert gct.install_gc_watch() is False
    assert gct._on_gc not in gc.callbacks and gct.snapshot()["watch"] is False


def test_install_is_idempotent_and_reset_removes_the_hook() -> None:
    assert gct.install_gc_watch() is True and gct.install_gc_watch() is True
    assert gc.callbacks.count(gct._on_gc) == 1
    gct.reset_for_tests()
    assert gct._on_gc not in gc.callbacks and gct.snapshot()["watch"] is False


def test_real_collection_moves_the_counters() -> None:
    gct.install_gc_watch()
    before = gct.snapshot()
    gc.collect(2)
    after = gct.snapshot()
    assert after["collections"]["gen2"] >= before["collections"]["gen2"] + 1
    assert after["total_pause_ms"] >= before["total_pause_ms"]
    assert after["max_pause_ms"] >= 0


# -- status payload -----------------------------------------------------------------


class _StubVault:
    recovered_from_corruption = False

    def close(self) -> None:
        pass


def test_status_payload_carries_plain_int_gc_summary(tmp_path: Path) -> None:
    reset_state_for_tests()
    try:
        state = init_state(_StubVault(), tmp_path)
        gct.install_gc_watch()
        gct.apply_gc_tuning(_NO_CONFIG)
        gc.collect(2)
        payload = http_mod._status_payload(state)
        with TestClient(build_rest_app(state), base_url="http://127.0.0.1") as client:
            body = client.get("/api/v1/status").json()
    finally:
        reset_state_for_tests()
    for gc_summary in (payload["gc"], body["gc"]):
        assert gc_summary["watch"] is True and gc_summary["tuning_applied"] is True
        assert gc_summary["thresholds"] == [50000, 20, 100]
        assert gc_summary["collections"]["gen2"] >= 1
        for key in ("max_pause_ms", "total_pause_ms", "slow_pauses", "frozen_objects"):
            assert type(gc_summary[key]) is int
        assert gc_summary["last_slow_pause_at"] is None or type(gc_summary["last_slow_pause_at"]) is int
    json.dumps(payload["gc"])
    assert {"status", "vaults", "uptime_s", "pid", "ingest", "integrity"} <= set(payload)
