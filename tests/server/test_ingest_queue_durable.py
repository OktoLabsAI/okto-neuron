"""Restart-durable ingest queue (Feature 1) + progress fields in snapshot.

The persistence helpers only touch ``state.vault_path`` and
``state.ingest_queue``, so a lightweight stub state keeps these unit tests off
the real Ladybug vault (per tests/support/conftest.py stub style). One
integration test proves ``init_state`` rehydrates against a real vault.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_neuron.companion import RememberCancelled
from okto_neuron.server import _ingest_queue as iq
from okto_neuron.server._ingest_queue import IngestItem


def _state(root: Path) -> SimpleNamespace:
    """Minimal stand-in exposing the attributes the helpers read."""
    return SimpleNamespace(
        vault_path=root,
        ingest_queue=[],
        ingest_worker_active=False,
        ingest_cancel_requested=False,
        last_ingest_at=None,
        # issue #5: multi-vault scheduler signal, bumped alongside the scalar.
        last_ingest_at_by_vault={},
    )


def test_enqueue_persists_sidecar(tmp_path: Path) -> None:
    ext = tmp_path / "ext"
    ext.mkdir()
    src = ext / "note.md"
    src.write_text("# Note\n\nbody.\n", encoding="utf-8")
    state = _state(tmp_path / "vault")

    iq.enqueue_paths(state, [src], state.vault_path / ".marginalia" / "sources")

    history = iq.history_path(state)
    assert history.exists()
    data = json.loads(history.read_text(encoding="utf-8"))
    assert data["version"] == iq.HISTORY_VERSION
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert item["status"] == "queued"
    # New progress fields are serialized with their defaults.
    assert item["stage"] == "queued"
    assert item["blocks_total"] == 0
    assert item["blocks_done"] == 0
    assert item["outcome"] == {}


def test_persist_writes_atomically_no_tmp_left(tmp_path: Path) -> None:
    state = _state(tmp_path / "vault")
    state.ingest_queue = [IngestItem(id="1", name="a.md", path="/x/a.md")]
    iq.persist(state)
    history = iq.history_path(state)
    assert history.exists()
    # No leftover temp files in the sidecar directory.
    leftovers = [p for p in history.parent.iterdir() if p.name != history.name]
    assert leftovers == []


def test_rehydrate_restores_queue(tmp_path: Path) -> None:
    state = _state(tmp_path / "vault")
    state.ingest_queue = [
        IngestItem(
            id="1",
            name="a.md",
            path="/x/a.md",
            status="done",
            committed=3,
            outcome={"quality": "partial", "units": {"failed": 1}},
        ),
        IngestItem(
            id="2",
            name="b.md",
            path="/x/b.md",
            status="queued",
            stage="queued",
            blocks_total=5,
            blocks_done=0,
        ),
    ]
    iq.persist(state)

    fresh = _state(tmp_path / "vault")
    iq.rehydrate_queue(fresh)
    assert [i.id for i in fresh.ingest_queue] == ["1", "2"]
    assert fresh.ingest_queue[0].status == "done"
    assert fresh.ingest_queue[0].committed == 3
    assert fresh.ingest_queue[0].outcome == {
        "quality": "partial",
        "units": {"failed": 1},
    }
    assert fresh.ingest_queue[1].blocks_total == 5


def test_processing_resets_to_queued_on_rehydrate(tmp_path: Path) -> None:
    state = _state(tmp_path / "vault")
    state.ingest_queue = [
        IngestItem(
            id="1",
            name="crash.md",
            path="/x/crash.md",
            status="processing",
            stage="extracting",
            blocks_total=10,
            blocks_done=4,
            outcome={"quality": "partial"},
        ),
    ]
    iq.persist(state)

    fresh = _state(tmp_path / "vault")
    iq.rehydrate_queue(fresh)
    # remember() is content-hash idempotent, so a crash-interrupted item is
    # re-queued for the drain worker rather than failed.
    item = fresh.ingest_queue[0]
    assert item.status == "queued"
    assert item.stage == "queued"
    assert item.outcome == {}


def test_retention_caps_terminal_keeps_active(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(iq, "RETENTION_CAP", 2)
    state = _state(tmp_path / "vault")
    # Interleave terminal + active items in append order.
    state.ingest_queue = [
        IngestItem(id="d1", name="d1", path="/d1", status="done"),
        IngestItem(id="q1", name="q1", path="/q1", status="queued"),
        IngestItem(id="e1", name="e1", path="/e1", status="error"),
        IngestItem(id="p1", name="p1", path="/p1", status="processing"),
        IngestItem(id="d2", name="d2", path="/d2", status="done"),
        IngestItem(id="d3", name="d3", path="/d3", status="done"),
    ]
    iq.persist(state)

    data = json.loads(iq.history_path(state).read_text(encoding="utf-8"))
    ids = [i["id"] for i in data["items"]]
    # All queued/processing kept; only the most recent 2 terminal retained
    # (d1 is the oldest terminal and is dropped).
    assert "q1" in ids and "p1" in ids
    terminal_ids = [i["id"] for i in data["items"] if i["status"] in ("done", "error")]
    assert len(terminal_ids) == 2
    assert terminal_ids == ["e1", "d2", "d3"][-2:]
    assert "d1" not in ids


def test_snapshot_includes_progress_fields(tmp_path: Path) -> None:
    state = _state(tmp_path / "vault")
    state.ingest_worker_active = False
    state.ingest_queue = [
        IngestItem(
            id="1",
            name="a.md",
            path="/x/a.md",
            status="processing",
            stage="extracting",
            blocks_total=8,
            blocks_done=3,
        ),
    ]
    snap = iq.snapshot(state)
    item = snap["items"][0]
    assert {
        "stage",
        "blocks_total",
        "blocks_done",
        "stage_progress_label",
        "stage_progress_done",
        "stage_progress_total",
    } <= set(item)
    assert item["stage"] == "extracting"
    assert item["blocks_total"] == 8
    assert item["blocks_done"] == 3
    assert "events" not in item
    assert item["event_count"] == 0


def test_stage_events_project_fine_grained_progress_and_transition_resets_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path / "vault")
    item = IngestItem(
        id="1",
        name="a.md",
        path="/x/a.md",
        status="processing",
        stage="committing",
        blocks_total=44,
        blocks_done=44,
    )
    state.ingest_queue = [item]
    monkeypatch.setattr(iq, "persist", lambda _state: None)

    iq._make_on_event(state, item)(
        {
            "kind": "relation_curator_progress",
            "summary": "Relationship curator progress",
            "payload": {"reviewed": 419, "total": 2070, "remaining": 1651},
        }
    )

    assert item.stage_progress_label == "Curating relationships"
    assert item.stage_progress_done == 419
    assert item.stage_progress_total == 2070
    snapshot = iq.snapshot(state)["items"][0]
    assert snapshot["stage_progress_done"] == 419
    assert snapshot["stage_progress_total"] == 2070

    iq._make_on_progress(state, item)("done", 44, 44)

    assert item.stage_progress_label is None
    assert item.stage_progress_done == 0
    assert item.stage_progress_total == 0


def test_snapshot_includes_live_extraction_candidate_counts(tmp_path: Path) -> None:
    state = _state(tmp_path / "vault")
    item = IngestItem(
        id="1",
        name="a.md",
        path="/x/a.md",
        status="processing",
        stage="extracting",
    )
    state.ingest_queue = [item]

    iq.record_event(
        state,
        item,
        "extraction_result",
        "Extracted block 1/2",
        {
            "nodes": [{"type": "Agent", "title": "Frodo"}],
            "edges": [
                {"type": "travels_with", "src_ref": "frodo", "dst_ref": "sam"},
                {"type": "age", "src_ref": "frodo", "dst_literal": "50"},
            ],
        },
    )

    snap = iq.snapshot(state)
    payload = snap["items"][0]
    assert payload["extracted_nodes"] == 1
    assert payload["extracted_edges"] == 1
    assert payload["extracted_claims"] == 1
    assert "events" not in payload


def test_snapshot_identifies_active_vault(tmp_path: Path) -> None:
    state = _state(tmp_path / "vault")

    snap = iq.snapshot(state)

    assert snap["vault"] == {
        "name": "vault",
        "path": str((tmp_path / "vault").resolve(strict=False)),
        "current": True,
    }


def test_item_detail_includes_events(tmp_path: Path) -> None:
    state = _state(tmp_path / "vault")
    item = IngestItem(id="1", name="a.md", path="/x/a.md")
    state.ingest_queue = [item]

    iq.record_event(state, item, "chunks", "Parsed chunks", {"chunks": [{"text": "body"}]})

    detail = iq.item_detail(state, "1")

    assert detail is not None
    payload = detail["item"]
    assert detail["vault"]["name"] == "vault"
    assert payload["event_count"] == 1
    assert payload["events"][0]["kind"] == "chunks"
    assert payload["events"][0]["payload"]["chunks"][0]["text"] == "body"


def test_on_event_only_appends_and_marks_dirty(tmp_path: Path, monkeypatch) -> None:
    """The sidecar write is not on the event path (it runs under the companion's event lock)."""
    state = _state(tmp_path / "vault")
    item = IngestItem(id="1", name="a.md", path="/x/a.md")
    state.ingest_queue = [item]
    persists: list[int] = []
    marks: list[int] = []
    monkeypatch.setattr(iq, "persist", lambda _state: persists.append(len(item.events)))
    monkeypatch.setattr(iq, "request_persist", lambda _state: marks.append(len(item.events)))
    on_event = iq._make_on_event(state, item)

    for idx in range(30):
        on_event({"kind": "llm_request", "summary": f"request {idx}", "payload": {}})
    on_event({"kind": "chunks", "summary": "Parsed chunks", "payload": {}})

    assert len(item.events) == 31
    assert persists == [], "on_event must never write the sidecar itself"
    assert marks == list(range(1, 32)), "every event marks the queue dirty"


def test_cancel_marks_queued_terminal_and_requests_processing_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from okto_neuron.llm import _litellm_process

    provider_cancels: list[bool] = []
    monkeypatch.setattr(
        _litellm_process,
        "cancel_requested_litellm_calls",
        lambda: provider_cancels.append(True) or 1,
    )
    state = _state(tmp_path / "vault")
    state.ingest_worker_active = True
    state.ingest_queue = [
        IngestItem(id="p", name="p.md", path="/p.md", status="processing"),
        IngestItem(id="q", name="q.md", path="/q.md", status="queued"),
    ]

    snap = iq.cancel(state)

    assert state.ingest_cancel_requested is True
    assert state.ingest_queue[0].status == "processing"
    assert state.ingest_queue[1].status == "cancelled"
    assert snap["summary"]["cancelled"] == 1
    assert snap["summary"]["cancel_requested"] is True
    assert snap["cancelled"] == 1

    event_count = len(state.ingest_queue[0].events)
    repeated = iq.cancel(state)
    assert repeated["cancelled"] == 0
    assert len(state.ingest_queue[0].events) == event_count
    assert provider_cancels == [True, True]


def test_worker_stops_active_file_and_cancels_late_queue_items(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    remembered: list[str] = []

    class _StubCompanion:
        def remember(
            self,
            path,
            *,
            on_progress=None,
            on_event=None,
            should_cancel=None,
        ):  # type: ignore[no-untyped-def]
            remembered.append(path)
            started.set()
            assert release.wait(timeout=2)
            assert should_cancel is not None
            if should_cancel():
                raise RememberCancelled()
            raise AssertionError("should_cancel should have stopped remember")

    async def _run() -> tuple[SimpleNamespace, dict]:
        state = _state(tmp_path / "vault")
        state.draining = False
        state.writer_lock = asyncio.Lock()
        state.ingest_queue = [
            IngestItem(id="active", name="active.md", path="/active.md"),
            IngestItem(id="queued", name="queued.md", path="/queued.md"),
        ]
        iq.ensure_worker(state, lambda _state: _StubCompanion())
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()

        snap = iq.cancel(state)
        state.ingest_queue.append(IngestItem(id="late", name="late.md", path="/late.md"))
        release.set()
        for _ in range(200):
            if not state.ingest_worker_active:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0)
        return state, snap

    state, snap = asyncio.run(_run())

    assert snap["summary"]["cancel_requested"] is True
    assert remembered == ["/active.md"]
    assert [item.status for item in state.ingest_queue] == [
        "cancelled",
        "cancelled",
        "cancelled",
    ]
    assert state.ingest_worker_active is False
    assert state.ingest_cancel_requested is False
    assert state.ingest_queue[0].events[-1]["kind"] == "cancelled"


def test_server_shutdown_pauses_active_file_without_discarding_queue(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()

    class _StubCompanion:
        def remember(
            self,
            path,
            *,
            on_progress=None,
            on_event=None,
            should_cancel=None,
        ):  # type: ignore[no-untyped-def]
            started.set()
            assert release.wait(timeout=2)
            assert should_cancel is not None
            if should_cancel():
                raise RememberCancelled()
            raise AssertionError("shutdown should have stopped remember")

    async def _run() -> SimpleNamespace:
        state = _state(tmp_path / "vault")
        state.draining = False
        state.writer_lock = asyncio.Lock()
        state.ingest_queue = [
            IngestItem(id="active", name="active.md", path="/active.md"),
            IngestItem(id="queued", name="queued.md", path="/queued.md"),
        ]
        iq.ensure_worker(state, lambda _state: _StubCompanion())
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.01)
        assert started.is_set()
        state.draining = True
        release.set()
        task = state.ingest_worker_task
        assert task is not None
        await task
        return state

    state = asyncio.run(_run())

    assert [item.status for item in state.ingest_queue] == ["queued", "queued"]
    assert state.ingest_queue[0].events[-1]["kind"] == "paused"
    assert state.ingest_worker_active is False


def test_record_completed_appends_done_stored_item(tmp_path: Path) -> None:
    """A deterministic /add store is recorded as a terminal done/stored item
    so it shows up in the queue snapshot and summary."""
    state = _state(tmp_path / "vault")
    state.ingest_worker_active = False

    item = iq.record_completed(state, name="a.md", path="/x/a.md", committed=1, stage="stored")

    assert item.status == "done"
    assert item.stage == "stored"
    assert item.committed == 1
    assert item.outcome == {"quality": "not_applicable"}
    assert state.ingest_queue == [item]
    # Persisted to the durable sidecar.
    assert iq.history_path(state).exists()

    snap = iq.snapshot(state)
    assert snap["summary"]["done"] == 1
    assert len(snap["items"]) == 1
    assert snap["items"][0]["stage"] == "stored"
    assert snap["items"][0]["status"] == "done"


def test_ensure_worker_drains_rehydrated_queued_item(tmp_path: Path) -> None:
    """Crash-recovery: a rehydrated ``queued`` item must drain when the worker
    is kicked on startup (runtime wires ``ensure_worker`` after ``init_state``),
    not sit idle until the next manual enqueue."""

    class _StubCompanion:
        def remember(self, path, *, on_progress=None, on_event=None, should_cancel=None):  # type: ignore[no-untyped-def]
            return SimpleNamespace(committed=1, queued=0)

    async def _run() -> str:
        state = _state(tmp_path / "vault")
        state.draining = False
        state.ingest_worker_active = False
        state.writer_lock = asyncio.Lock()
        state.ingest_queue = [
            IngestItem(id="1", name="x.md", path=str(tmp_path / "x.md"), status="queued"),
        ]
        iq.ensure_worker(state, lambda _s: _StubCompanion())
        for _ in range(200):
            if state.ingest_queue[0].status in ("done", "error"):
                break
            await asyncio.sleep(0.01)
        return state.ingest_queue[0].status

    assert asyncio.run(_run()) == "done"


def test_drain_checkpoints_the_store_after_a_document_completes(tmp_path: Path) -> None:
    """Task #12 plumbing: once a document reaches a terminal ingest state, the
    drain worker must checkpoint the vault's store before moving on — still
    inside ``writer_lock``, so no second writer can interleave."""

    class _StubCompanion:
        def remember(self, path, *, on_progress=None, on_event=None, should_cancel=None):  # type: ignore[no-untyped-def]
            return SimpleNamespace(committed=1, queued=0)

    checkpoint_calls: list[bool] = []

    class _FakeStore:
        def checkpoint(self) -> None:
            checkpoint_calls.append(True)

    async def _run() -> str:
        state = _state(tmp_path / "vault")
        state.draining = False
        state.ingest_worker_active = False
        state.writer_lock = asyncio.Lock()
        state.vault = SimpleNamespace(store=_FakeStore())
        state.ingest_queue = [
            IngestItem(id="1", name="x.md", path=str(tmp_path / "x.md"), status="queued"),
        ]
        iq.ensure_worker(state, lambda _s: _StubCompanion())
        for _ in range(200):
            if state.ingest_queue[0].status in ("done", "error"):
                break
            await asyncio.sleep(0.01)
        return state.ingest_queue[0].status

    assert asyncio.run(_run()) == "done"
    assert checkpoint_calls == [True]


def test_drain_survives_a_checkpoint_failure(tmp_path: Path) -> None:
    """A checkpoint failure is best-effort: it must not flip an otherwise
    successful ingest to ``error`` — the semantic write already committed."""

    class _StubCompanion:
        def remember(self, path, *, on_progress=None, on_event=None, should_cancel=None):  # type: ignore[no-untyped-def]
            return SimpleNamespace(committed=1, queued=0)

    class _BrokenStore:
        def checkpoint(self) -> None:
            raise RuntimeError("disk full")

    async def _run() -> str:
        state = _state(tmp_path / "vault")
        state.draining = False
        state.ingest_worker_active = False
        state.writer_lock = asyncio.Lock()
        state.vault = SimpleNamespace(store=_BrokenStore())
        state.ingest_queue = [
            IngestItem(id="1", name="x.md", path=str(tmp_path / "x.md"), status="queued"),
        ]
        iq.ensure_worker(state, lambda _s: _StubCompanion())
        for _ in range(200):
            if state.ingest_queue[0].status in ("done", "error"):
                break
            await asyncio.sleep(0.01)
        return state.ingest_queue[0].status

    assert asyncio.run(_run()) == "done"


def test_worker_records_provider_error_from_remember_result(tmp_path: Path) -> None:
    provider_error = (
        "LLM provider 'bedrock' requires optional dependency boto3. "
        "Install Okto Neuron with the bedrock extra, for example: uv tool install --force "
        '"okto-neuron[litellm,bedrock]"'
    )

    class _StubCompanion:
        def remember(self, path, *, on_progress=None, on_event=None, should_cancel=None):  # type: ignore[no-untyped-def]
            return SimpleNamespace(
                committed=0,
                queued=0,
                blocks_total=1,
                nodes_extracted=0,
                edges_extracted=0,
                claims_minted=0,
                provider_error=provider_error,
                outcomes=(),
            )

    async def _run() -> dict:
        state = _state(tmp_path / "vault")
        state.draining = False
        state.ingest_worker_active = False
        state.writer_lock = asyncio.Lock()
        state.ingest_queue = [
            IngestItem(id="1", name="x.md", path=str(tmp_path / "x.md"), status="queued"),
        ]
        iq.ensure_worker(state, lambda _s: _StubCompanion())
        for _ in range(200):
            if state.ingest_queue[0].status in ("done", "error"):
                break
            await asyncio.sleep(0.01)
        return iq.snapshot(state)["items"][0]

    item = asyncio.run(_run())
    # F4 (2026-07-02 remediation): a provider error with ZERO yield (no
    # commits, no claims, no gate-parked candidates) is a failed ingest —
    # terminal status is "error" (visible + retryable), not a quiet "done".
    assert item["status"] == "error"
    assert item["provider_error"] == provider_error
    assert "zero yield" in item["error"]
    assert item["last_event"]["payload"]["provider_error"] == provider_error


def test_init_state_rehydrates_real_vault(tmp_path: Path) -> None:
    from okto_neuron.server.state import init_state, reset_state_for_tests
    from okto_neuron.vault import Vault

    vault = Vault.init(tmp_path / "v")
    try:
        # Seed a durable sidecar as if a prior process had left it.
        seed = _state(Path(vault.path))
        seed.ingest_queue = [
            IngestItem(id="1", name="x.md", path="/x.md", status="processing"),
            IngestItem(id="2", name="y.md", path="/y.md", status="done"),
        ]
        iq.persist(seed)

        state = init_state(vault, Path(vault.path))
        assert [i.id for i in state.ingest_queue] == ["1", "2"]
        # processing -> queued on load.
        assert state.ingest_queue[0].status == "queued"
        assert state.ingest_queue[1].status == "done"
    finally:
        reset_state_for_tests()
