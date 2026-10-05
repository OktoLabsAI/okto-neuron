"""The single-threaded import phase (issue #40): content, and where it runs.

``preload_server_modules`` must (a) import every ``okto_neuron`` module and (b) run
in the real ``serve`` path before any thread that can run ``okto_neuron`` code
exists (store/job executors, the asyncio default executor, the stop-signal
watcher). Removing the call from ``serve`` or moving it after thread creation
fails here.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from okto_neuron.cli import app
from okto_neuron.server import _preload, runtime
from okto_neuron.server import lifecycle

_OWN_THREAD_PREFIXES = ("okto-neuron", "asyncio_")

_FRESH = """
import importlib.util, importlib, sys
from pathlib import Path
from okto_neuron.server._preload import preload_server_modules

n = preload_server_modules()
spec = importlib.util.find_spec("okto_neuron")
root = Path(next(iter(spec.submodule_search_locations)))
missing = []
for path in sorted(root.rglob("*.py")):
    parts = ["okto_neuron", *path.relative_to(root).with_suffix("").parts]
    if parts[-1] == "__main__":
        continue
    if parts[-1] == "__init__":
        parts.pop()
    name = ".".join(parts)
    if name not in sys.modules:
        missing.append(name)
print(n)
if missing:
    print("\\n".join(missing))
"""


def _own_threads() -> set[str]:
    return {t.name for t in threading.enumerate() if t.name.startswith(_OWN_THREAD_PREFIXES)}


def test_preload_puts_every_module_in_sys_modules() -> None:
    proc = subprocess.run(
        [sys.executable, "-c", _FRESH], capture_output=True, text=True, timeout=120, check=True
    )
    count, *missing = [line for line in proc.stdout.splitlines() if line]
    assert int(count) > 100
    # Only modules that need an optional third-party extra that is not installed may be absent.
    for name in missing:
        probe = subprocess.run(
            [sys.executable, "-c", f"import {name}"], capture_output=True, text=True, timeout=120
        )
        assert probe.returncode != 0 and "No module named" in probe.stderr, (
            f"{name} was not preloaded but imports fine: {probe.stderr}"
        )
        assert "No module named 'okto_neuron" not in probe.stderr


def test_serve_preloads_before_any_own_thread_and_before_the_lock_and_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for var in ("OKTO_NEURON_MLFLOW_TRACKING_URI", "MARGINALIA_MLFLOW_TRACKING_URI"):
        monkeypatch.delenv(var, raising=False)
    baseline = _own_threads()
    events: list[str] = []
    new_threads_at_preload: list[set[str]] = []

    def spy_preload() -> int:
        events.append("preload")
        new_threads_at_preload.append(_own_threads() - baseline)
        return 0

    real_enter = lifecycle.PidFile.__enter__

    def spy_enter(self: lifecycle.PidFile) -> lifecycle.PidFile:
        events.append("pidfile")
        return real_enter(self)

    def spy_run(**_: object) -> None:
        events.append("run")

    monkeypatch.setattr(_preload, "preload_server_modules", spy_preload)
    monkeypatch.setattr(lifecycle.PidFile, "__enter__", spy_enter)
    monkeypatch.setattr(runtime, "run", spy_run)

    result = CliRunner().invoke(app, ["serve", "--no-open", "--port", "0", "--mcp-port", "0"])
    assert result.exit_code == 0, result.output
    assert events == ["preload", "pidfile", "run"]
    assert new_threads_at_preload == [set()], "own threads existed before the import phase"


def test_run_preloads_before_the_event_loop_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    def spy_asyncio_run(coro: object) -> None:
        events.append("asyncio.run")
        getattr(coro, "close", lambda: None)()

    monkeypatch.setattr(runtime, "preload_server_modules", lambda: events.append("preload") or 0)
    monkeypatch.setattr(runtime.asyncio, "run", spy_asyncio_run)
    runtime.run(None)
    assert events == ["preload", "asyncio.run"]
