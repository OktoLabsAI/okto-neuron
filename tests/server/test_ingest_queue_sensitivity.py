"""P1: the ingest queue carries and honors ``sensitivity``.

The queue used to drop the caller's sensitivity ask entirely —
``_ingest_queue``'s drain worker called ``companion.remember`` without it, so
a ``local_only`` source enqueued through MCP/REST was silently ingested
through whatever LLM the vault had configured. These tests pin the fix:
the ``IngestItem`` field exists (persisted + rehydrated),
``enqueue_materialized`` sets it, the worker passes it to
``companion.remember`` verbatim, and the dedup path lets a newer non-default
ask govern a still-pending item.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from okto_neuron.server import _ingest_queue as iq
from okto_neuron.server._ingest_queue import IngestItem


def _state(root: Path) -> SimpleNamespace:
    root.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        vault_path=root,
        ingest_queue=[],
        ingest_worker_active=False,
        ingest_worker_task=None,
        ingest_cancel_requested=False,
        draining=False,
        shutting_down=False,
        writer_lock=asyncio.Lock(),
        note_ingest=None,
    )


class _RecordingCompanion:
    """Captures every remember() call's path AND sensitivity."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.done = threading.Event()

    def remember(
        self,
        path,
        *,
        sensitivity="default",
        on_progress=None,
        on_event=None,
        should_cancel=None,
    ):  # type: ignore[no-untyped-def]
        self.calls.append({"path": path, "sensitivity": sensitivity})
        self.done.set()
        return SimpleNamespace(
            committed=1,
            queued=0,
            blocks_total=1,
            nodes_extracted=0,
            edges_extracted=0,
            claims_minted=0,
            provider_error=None,
            outcome={"quality": "complete"},
            outcomes=[],
        )


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_local_only_reaches_companion_remember(tmp_path: Path) -> None:
    companion = _RecordingCompanion()
    source = tmp_path / "materialized.md"
    source.write_text("local-only bytes\n", encoding="utf-8")

    async def _run_case() -> SimpleNamespace:
        state = _state(tmp_path / "vault")
        item = iq.enqueue_materialized(state, source, sensitivity="local_only")
        assert item.sensitivity == "local_only"
        iq.ensure_worker(state, lambda _state: companion)
        for _ in range(500):
            if companion.done.is_set():
                break
            await asyncio.sleep(0.01)
        return state

    state = _run(_run_case())
    assert companion.calls == [{"path": str(source.resolve()), "sensitivity": "local_only"}]
    assert state.ingest_queue[0].status == "done"
    assert state.ingest_queue[0].sensitivity == "local_only"


def test_default_sensitivity_is_the_field_default_and_persists(tmp_path: Path) -> None:
    """The field must round-trip through the sidecar so a restarted worker
    still honors the caller's ask (rehydrate keeps unknown keys out, known
    fields in)."""
    state = _state(tmp_path / "vault")
    item = iq.enqueue_materialized(state, tmp_path / "a.md", sensitivity="local_only")
    plain = IngestItem(id="plain", name="plain.md", path="/plain.md")
    assert plain.sensitivity == "default", "field default must stay 'default'"
    state.ingest_queue.append(plain)
    iq.persist(state)

    sidecar = tmp_path / "vault" / ".marginalia" / iq.HISTORY_FILENAME
    raw = json.loads(sidecar.read_text())
    by_id = {entry["id"]: entry for entry in raw["items"]}
    assert by_id[item.id]["sensitivity"] == "local_only"
    assert by_id["plain"]["sensitivity"] == "default"

    rehydrated_state = _state(tmp_path / "vault")
    assert iq.rehydrate_queue(rehydrated_state) is None  # mutates in place
    fields = {i.id: i.sensitivity for i in rehydrated_state.ingest_queue}
    assert fields[item.id] == "local_only"
    assert fields["plain"] == "default"


def test_dedup_returns_the_pending_item_and_honors_the_newest_ask(tmp_path: Path) -> None:
    state = _state(tmp_path / "vault")
    source = tmp_path / "same.md"
    source.write_text("same bytes\n", encoding="utf-8")
    first = iq.enqueue_materialized(state, source)
    assert first.sensitivity == "default"
    again = iq.enqueue_materialized(state, source, sensitivity="local_only")
    assert again is first, "identical pending source dedups to one item"
    assert first.sensitivity == "local_only"
    assert len(state.ingest_queue) == 1
