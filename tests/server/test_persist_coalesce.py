"""Coalesced sidecar persistence (#37-2): one background flush, durable transitions."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import okto_neuron
from okto_neuron.server import _ingest_queue as iq
from okto_neuron.server._ingest_queue import IngestItem
from okto_neuron.server._persist_coalesce import PersistCoalescer
from okto_neuron.server._store_io import job_io


def _state(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        vault_path=root,
        ingest_queue=[],
        ingest_worker_active=False,
        ingest_cancel_requested=False,
        last_ingest_at=None,
        last_ingest_at_by_vault={},
        runtimes=lambda: (),
    )


def test_a_burst_of_marks_makes_one_flush_on_a_worker_thread() -> None:
    flushes: list[str] = []
    main = threading.current_thread().name

    async def scenario() -> None:
        c = PersistCoalescer(
            lambda: flushes.append(threading.current_thread().name), name="t", interval=0.1
        )
        for _ in range(1000):
            c.mark_dirty()
        await asyncio.sleep(0.25)
        assert len(flushes) == 1, flushes
        assert flushes[0] != main, "the flush must not run on the event loop thread"
        c.mark_dirty()
        await asyncio.sleep(0.25)
        assert len(flushes) == 2

    asyncio.run(scenario())


def test_marks_from_a_dispatched_worker_thread_reach_the_loop() -> None:
    flushes: list[int] = []

    async def scenario() -> None:
        c = PersistCoalescer(lambda: flushes.append(1), name="t", interval=0.1)

        def producer() -> None:
            for _ in range(500):
                c.mark_dirty()

        await job_io(producer)
        await asyncio.sleep(0.3)
        assert flushes == [1]

    asyncio.run(scenario())


def test_without_an_event_loop_a_mark_writes_at_once() -> None:
    flushes: list[int] = []
    c = PersistCoalescer(lambda: flushes.append(1), name="t", interval=0.1)
    c.mark_dirty()
    c.mark_dirty()
    assert flushes == [1, 1]


def test_a_real_persist_clears_the_dirty_flag_so_no_flush_follows() -> None:
    flushes: list[int] = []

    async def scenario() -> None:
        c = PersistCoalescer(lambda: flushes.append(1), name="t", interval=0.1)
        c.mark_dirty()
        c.flushed()  # a durable transition wrote everything
        await asyncio.sleep(0.25)
        assert flushes == []
        assert c.dirty is False

    asyncio.run(scenario())


def test_close_writes_what_is_pending_and_later_marks_write_at_once() -> None:
    flushes: list[int] = []

    async def scenario() -> None:
        c = PersistCoalescer(lambda: flushes.append(1), name="t", interval=5.0)
        c.mark_dirty()
        assert c.close() is True
        assert flushes == [1]
        assert c.close() is False
        c.mark_dirty()
        assert flushes == [1, 1]

    asyncio.run(scenario())


def test_a_slow_persist_does_not_stretch_on_event_latency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path / "vault")
    item = IngestItem(id="1", name="a.md", path="/x/a.md", status="processing")
    state.ingest_queue = [item]
    writes: list[float] = []
    real = iq._persist_locked

    def slow(s):  # type: ignore[no-untyped-def]
        time.sleep(0.4)
        writes.append(time.monotonic())
        real(s)

    monkeypatch.setattr(iq, "_persist_locked", slow)
    on_event = iq._make_on_event(state, item)
    latencies: list[float] = []

    def emit() -> None:
        for n in range(200):
            start = time.perf_counter()
            on_event({"kind": "stage", "summary": f"s{n}", "payload": {"n": n}})
            latencies.append(time.perf_counter() - start)

    async def scenario() -> None:
        monkeypatch.setattr(iq._coalescer(state), "interval", 0.05)
        await job_io(emit)
        await asyncio.sleep(1.0)

    asyncio.run(scenario())
    assert max(latencies) < 0.05, f"slowest on_event {max(latencies) * 1000:.0f} ms"
    assert 1 <= len(writes) <= 4, "the burst coalesced into a few writes"


def test_persists_per_item_follow_the_interval_not_the_event_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path / "vault")
    item = IngestItem(id="1", name="a.md", path="/x/a.md", status="processing")
    state.ingest_queue = [item]
    writes: list[int] = []
    real = iq._persist_locked
    monkeypatch.setattr(iq, "_persist_locked", lambda s: (writes.append(1), real(s)))
    on_event = iq._make_on_event(state, item)
    duration = 1.5

    def emit() -> None:
        end = time.monotonic() + duration
        n = 0
        while time.monotonic() < end:
            on_event({"kind": "llm_request", "summary": f"s{n}", "payload": {"n": n}})
            n += 1
            time.sleep(0.002)

    async def scenario() -> None:
        monkeypatch.setattr(iq._coalescer(state), "interval", 0.5)
        await job_io(emit)
        await asyncio.sleep(0.7)

    asyncio.run(scenario())
    assert len(writes) <= duration / 0.5 + 3, writes


def test_shutdown_flush_writes_the_pending_queue(tmp_path: Path) -> None:
    from okto_neuron.server import runtime as rt

    state = _state(tmp_path / "vault")
    item = IngestItem(id="1", name="a.md", path="/x/a.md", status="processing")
    state.ingest_queue = [item]

    async def scenario() -> int:
        iq.record_event(state, item, "stage", "s", {}, persist_now=False)
        iq.request_persist(state)  # a coalesced mark, flush still 2 s away
        assert not iq.history_path(state).exists()
        return rt._flush_sidecars(state)

    assert asyncio.run(scenario()) == 1
    data = json.loads(iq.history_path(state).read_text(encoding="utf-8"))
    assert data["items"][0]["events"][0]["summary"] == "s"


_CHILD = textwrap.dedent(
    """
    import asyncio, os, signal, sys
    from pathlib import Path
    from types import SimpleNamespace
    from okto_neuron.server import _ingest_queue as iq
    from okto_neuron.server._store_io import job_io

    root = Path(sys.argv[1])
    state = SimpleNamespace(vault_path=root, ingest_queue=[], ingest_worker_active=False,
        ingest_cancel_requested=False, last_ingest_at=None, last_ingest_at_by_vault={})
    src = root / "a.md"
    root.mkdir(parents=True, exist_ok=True)
    src.write_text("# a\\n")

    async def main():
        iq.enqueue_paths(state, [src], root / ".marginalia" / "sources")  # durable transition
        item = state.ingest_queue[0]
        item.status = "done"
        iq.persist(state)                                                  # durable transition
        on_event = iq._make_on_event(state, item)
        def chatter():
            for n in range(300):
                on_event({"kind": "stage", "summary": f"e{n}", "payload": {}})
        await job_io(chatter)
        print("ready", flush=True)
        os.kill(os.getpid(), signal.SIGKILL)                               # within the 2 s window

    asyncio.run(main())
    """
)


def test_kill_9_inside_the_window_loses_no_state_transition(tmp_path: Path) -> None:
    root = str(Path(okto_neuron.__file__).resolve().parent.parent)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([root, str(Path(__file__).parents[2])])}
    vault = tmp_path / "vault"
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(vault)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == -signal.SIGKILL, (proc.returncode, proc.stderr[-500:])
    state = _state(vault)
    iq.rehydrate_queue(state)
    assert [i.status for i in state.ingest_queue] == ["done"], "the durable transition survived"
    assert len(state.ingest_queue[0].events) <= 300, (
        "events in the window may be lost, never states"
    )
