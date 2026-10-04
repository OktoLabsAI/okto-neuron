"""Background warm-up of the candidate ledger indexes for opened startup vaults (#14).

The first ledger reader after a daemon start pays a cold index build that grows
with the ledger (hundreds of MB on a real vault). This module builds both
indexes once, on the job executor, right after startup so the first UI poll or
ingest finds them warm. It never delays readiness, never touches a vault that
was refused or not opened, and ends promptly when the daemon is stopping. The
index is a cache, so a failure here is logged and ignored.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from okto_neuron.server._store_io import get_job_executor

if TYPE_CHECKING:
    from okto_neuron.server.state import ServerState, VaultRuntime

_LOG = logging.getLogger(__name__)
_POLL_S = 0.1


def _warm_blocking(vault_path: Path, stop: threading.Event) -> None:
    from okto_neuron.consolidate.ledger import CandidateLedger

    if stop.is_set():
        return
    CandidateLedger(Path(vault_path) / ".marginalia").prewarm(stop.is_set)


async def _warm_one(state: ServerState, runtime: VaultRuntime) -> None:
    stop = threading.Event()
    key = ("ledger_prewarm", str(runtime.vault_path))
    # Identical keys coalesce on the executor; the ledger's own build locks
    # serialize a reader's build against this one, so nothing is built twice.
    work = asyncio.ensure_future(
        get_job_executor().single_flight(key, _warm_blocking, runtime.vault_path, stop)
    )
    try:
        while not work.done():
            if state.shutting_down:
                stop.set()
                return  # the thread finishes its current step on its own
            await asyncio.wait({work}, timeout=_POLL_S)
        work.result()
        _LOG.info("ledger indexes warmed for %s", runtime.vault_path.name)
    except asyncio.CancelledError:
        stop.set()
        raise
    except Exception as exc:  # noqa: BLE001 - a cache warm-up never fails the daemon
        _LOG.info("ledger index warm-up skipped for %s: %s", runtime.vault_path.name, exc)


def _opened_runtimes(state: ServerState) -> list[VaultRuntime]:
    refused = set(state.queue_refusals())
    opened = []
    for runtime in state.runtimes():
        key = runtime.vault_path.resolve(strict=False)
        if key in refused or state.vault_pool.peek(key) is None:
            continue
        opened.append(runtime)
    return opened


def start_ledger_prewarm(state: ServerState) -> None:
    """Start one warm task per vault that is open right now (call after startup work)."""
    for runtime in _opened_runtimes(state):
        task = asyncio.create_task(_warm_one(state, runtime), name="okto-neuron-ledger-prewarm")
        runtime.prewarm_task = task
