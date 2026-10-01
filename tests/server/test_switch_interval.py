"""GIL switch interval at startup (refs #38).

The interpreter value and the module flag are restored by the autouse
``_restore_gc_state`` fixture in ``tests/conftest.py``.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import signal
import socket
import sys
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
    monkeypatch.delenv("OKTO_NEURON_SWITCH_INTERVAL", raising=False)
    monkeypatch.delenv("MARGINALIA_SWITCH_INTERVAL", raising=False)
    sys.setswitchinterval(0.005)  # a known starting point; conftest restores the real one


def _logs(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == level]


def test_applied_sets_the_default_and_logs_old_and_new(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        report = gct.apply_switch_interval(_NO_CONFIG)
    assert sys.getswitchinterval() == 0.001
    assert report["enabled"] is True and report["skipped"] is None
    assert report["previous_switch_interval_s"] == 0.005
    (info,) = _logs(caplog, logging.INFO)
    assert info.getMessage() == "switch interval set to 0.001 (was 0.005)"
    assert gct.snapshot()["switch_interval_tuned"] is True


@pytest.mark.parametrize("raw", ["off", "OFF", "false", "no"])
def test_env_off_leaves_the_interpreter_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, raw: str
) -> None:
    monkeypatch.setenv("OKTO_NEURON_SWITCH_INTERVAL", raw)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        report = gct.apply_switch_interval(_NO_CONFIG)
    assert sys.getswitchinterval() == 0.005
    assert report["enabled"] is False and report["skipped"] == "disabled"
    assert _logs(caplog, logging.INFO) == [] and _logs(caplog, logging.WARNING) == []
    snap = gct.snapshot()
    assert snap["switch_interval_tuned"] is False and snap["switch_interval_s"] == 0.005


def test_config_off_and_value() -> None:
    gct.apply_switch_interval(SimpleNamespace(switch_interval=False))
    assert sys.getswitchinterval() == 0.005
    gct.apply_switch_interval(SimpleNamespace(switch_interval=0.02))
    assert sys.getswitchinterval() == 0.02


@pytest.mark.parametrize("bad", ["abc", "0", "-1", "5", "nan", "", 0, -1, 5, True, [1], "1.5"])
def test_invalid_env_warns_and_uses_the_default(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, bad: object
) -> None:
    monkeypatch.setenv("OKTO_NEURON_SWITCH_INTERVAL", str(bad))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        gct.apply_switch_interval(_NO_CONFIG)
    assert sys.getswitchinterval() == 0.001
    assert len(_logs(caplog, logging.WARNING)) == 1


@pytest.mark.parametrize("bad", ["abc", 0, -1, 5, 1.5, True, [1], {"x": 1}, float("nan")])
def test_invalid_config_warns_and_uses_the_default(
    caplog: pytest.LogCaptureFixture, bad: object
) -> None:
    with caplog.at_level(logging.INFO, logger=LOGGER):
        gct.apply_switch_interval(SimpleNamespace(switch_interval=bad))
    assert sys.getswitchinterval() == 0.001
    assert len(_logs(caplog, logging.WARNING)) == 1


def test_boundary_values_are_accepted() -> None:
    gct.apply_switch_interval(SimpleNamespace(switch_interval=1.0))
    assert sys.getswitchinterval() == 1.0


def test_env_beats_config_beats_default(monkeypatch: pytest.MonkeyPatch) -> None:
    gct.apply_switch_interval(SimpleNamespace(switch_interval=0.02))
    assert sys.getswitchinterval() == 0.02
    gct.reset_for_tests()
    monkeypatch.setenv("OKTO_NEURON_SWITCH_INTERVAL", "0.03")
    gct.apply_switch_interval(SimpleNamespace(switch_interval=0.02))
    assert sys.getswitchinterval() == 0.03
    gct.reset_for_tests()
    gct.apply_switch_interval(_NO_CONFIG)
    assert sys.getswitchinterval() == 0.03  # env still set, config absent
    monkeypatch.delenv("OKTO_NEURON_SWITCH_INTERVAL")
    gct.reset_for_tests()
    gct.apply_switch_interval(_NO_CONFIG)
    assert sys.getswitchinterval() == 0.001


def test_invalid_env_falls_through_to_valid_config(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OKTO_NEURON_SWITCH_INTERVAL", "abc")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        gct.apply_switch_interval(SimpleNamespace(switch_interval=0.02))
    assert sys.getswitchinterval() == 0.02
    assert len(_logs(caplog, logging.WARNING)) == 1


def test_app_config_accepts_a_garbage_key_without_failing_the_file() -> None:
    config = OktoNeuronConfig.model_validate(
        {"server": {"switch_interval": {"x": 1}, "store_workers": 3}}
    )
    assert config.server.store_workers == 3
    gct.apply_switch_interval(config.server)
    assert sys.getswitchinterval() == 0.001
    assert ServerSettings().switch_interval is None


def test_unreadable_app_config_never_crashes_apply(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("broken toml")

    monkeypatch.setattr(OktoNeuronConfig, "load", classmethod(boom))
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        gct.apply_switch_interval()
    assert sys.getswitchinterval() == 0.001
    assert len(_logs(caplog, logging.WARNING)) == 1


def test_applies_at_most_once_per_process(caplog: pytest.LogCaptureFixture) -> None:
    gct.apply_switch_interval(_NO_CONFIG)
    sys.setswitchinterval(0.02)  # a second apply must not touch the interpreter
    with caplog.at_level(logging.INFO, logger=LOGGER):
        second = gct.apply_switch_interval(_NO_CONFIG)
    assert second["skipped"] == "already_applied" and sys.getswitchinterval() == 0.02
    assert any("already applied" in r.getMessage() for r in caplog.records)
    gct.reset_for_tests()  # re-arms the flag
    gct.apply_switch_interval(_NO_CONFIG)
    assert sys.getswitchinterval() == 0.001


class _StubVault:
    recovered_from_corruption = False

    def close(self) -> None:
        pass


def test_status_payload_carries_the_switch_interval(tmp_path: Path) -> None:
    reset_state_for_tests()
    try:
        state = init_state(_StubVault(), tmp_path)
        gct.apply_switch_interval(_NO_CONFIG)
        with TestClient(build_rest_app(state), base_url="http://127.0.0.1") as client:
            body = client.get("/api/v1/status").json()
        payload = http_mod._status_payload(state)
    finally:
        reset_state_for_tests()
    for summary in (payload["gc"], body["gc"]):
        assert summary["switch_interval_s"] == 0.001 and type(summary["switch_interval_s"]) is float
        assert summary["switch_interval_tuned"] is True
        assert "thresholds" in summary  # existing fields stay
    json.dumps(body["gc"])


def test_serve_startup_applies_after_the_opens_and_the_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    real_open = runtime._open_startup_vault
    real_freeze = gc.freeze
    real_set = sys.setswitchinterval

    def spy_open(path: Path | None):  # noqa: ANN202
        result = real_open(path)
        order.append("open")
        return result

    def spy_freeze() -> None:
        order.append("freeze")
        real_freeze()

    def spy_set(value: float) -> None:
        order.append("switch")
        real_set(value)

    monkeypatch.setattr(runtime, "_open_startup_vault", spy_open)
    monkeypatch.setattr(gc, "freeze", spy_freeze)
    monkeypatch.setattr(sys, "setswitchinterval", spy_set)
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
            assert sys.getswitchinterval() == 0.001
            assert gct.snapshot()["switch_interval_tuned"] is True
        finally:
            signal.raise_signal(signal.SIGTERM)
            await asyncio.wait_for(task, 60)

    asyncio.run(scenario())
    assert order == ["open", "freeze", "switch"]
