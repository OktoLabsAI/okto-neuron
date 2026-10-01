"""The ingest history sidecar carries previews, not full event bodies (#37-1).

Body-carrying events keep the full body only while the item is in flight; the
persisted form (and a finished item) holds a preview, the original length and a
sha256, and one item's persisted events stay under a byte budget.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from okto_neuron.server import _ingest_queue as iq
from okto_neuron.server._ingest_queue import IngestItem


def _state(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        vault_path=root,
        ingest_queue=[],
        ingest_worker_active=False,
        ingest_cancel_requested=False,
        last_ingest_at=None,
        last_ingest_at_by_vault={},
    )


def _persisted(state: SimpleNamespace) -> dict:
    iq.persist(state)
    return json.loads(iq.history_path(state).read_text(encoding="utf-8"))


def _big_request(n: int = 40_000) -> dict:
    return {"messages": [{"role": "user", "content": "q" * n}], "params": {"temperature": 0}}


def test_an_over_budget_body_is_stored_as_preview_length_and_sha256() -> None:
    event = iq._event("llm_request", "request", _big_request(10_000))
    full = json.dumps(event["payload"], separators=(",", ":"), ensure_ascii=False).encode()
    preview = event["body_preview"]
    assert preview["truncated"] is True
    assert preview["original_bytes"] == len(full)
    assert preview["sha256"] == hashlib.sha256(full).hexdigest()
    assert len(preview["preview"]) == iq.EVENT_PREVIEW_CHARS
    assert event["payload"]["messages"][0]["content"].startswith("q")  # live body untouched


def test_small_and_non_body_events_are_left_alone() -> None:
    small = iq._event("llm_request", "request", {"messages": [{"role": "user", "content": "hi"}]})
    assert "body_preview" not in small
    other = iq._event("extraction_result", "x", {"nodes": [{"title": "t" * 5_000}]})
    assert "body_preview" not in other


def test_the_sidecar_holds_previews_while_an_in_flight_item_keeps_the_full_body(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path / "vault")
    item = IngestItem(id="1", name="a.md", path="/x/a.md", status="processing")
    state.ingest_queue = [item]
    iq.record_event(state, item, "llm_request", "request", _big_request(), persist_now=False)
    data = _persisted(state)
    persisted = data["items"][0]["events"][0]
    assert persisted["payload"]["truncated"] is True and "body_preview" not in persisted
    assert persisted["payload"]["original_bytes"] > 12_000
    assert len(json.dumps(persisted)) < 4_000
    live = item.events[0]
    assert (
        live["payload"]["messages"][0]["content"] == "q" * 12_000 + "\n...[truncated 28000 chars]"
    )
    assert "body_preview" in live, "an in-flight item keeps its full body in memory"


def test_a_terminal_transition_cuts_the_bodies_in_memory_too(tmp_path: Path) -> None:
    state = _state(tmp_path / "vault")
    item = IngestItem(id="1", name="a.md", path="/x/a.md", status="processing")
    state.ingest_queue = [item]
    iq.record_event(state, item, "llm_request", "request", _big_request(), persist_now=False)
    iq.record_event(
        state, item, "llm_response", "response", {"response": "r" * 9_000}, persist_now=False
    )
    item.status = "done"
    _persisted(state)
    for event in item.events:
        assert "body_preview" not in event
        assert event["payload"]["truncated"] is True
    assert len(json.dumps(item.events)) < 8_000


def test_one_items_persisted_events_stay_within_the_byte_budget(tmp_path: Path) -> None:
    state = _state(tmp_path / "vault")
    item = IngestItem(id="1", name="a.md", path="/x/a.md", status="done")
    state.ingest_queue = [item]
    for n in range(iq.MAX_EVENTS_PER_ITEM):
        kind = "extraction_result" if n % 10 == 0 else "llm_request"
        payload = {"nodes": [{"title": f"n{n}"}]} if kind == "extraction_result" else _big_request()
        iq.record_event(state, item, kind, f"e{n}", payload, persist_now=False)
    first_summary, last_five = item.events[0]["summary"], [e["summary"] for e in item.events[-5:]]
    data = _persisted(state)
    events = data["items"][0]["events"]
    assert len(json.dumps(events, separators=(",", ":"))) <= iq.MAX_PERSISTED_EVENT_BYTES_PER_ITEM
    assert events[0]["summary"] == first_summary
    assert [e["summary"] for e in events[-5:]] == last_five
    kept_structural = [e for e in events if e["kind"] == "extraction_result"]
    assert len(kept_structural) == 8, "structural events go last: all of them survive here"


def test_the_persisted_form_round_trips_through_rehydrate(tmp_path: Path) -> None:
    state = _state(tmp_path / "vault")
    item = IngestItem(id="1", name="a.md", path="/x/a.md", status="done")
    state.ingest_queue = [item]
    iq.record_event(state, item, "llm_request", "request", _big_request(), persist_now=False)
    iq.record_event(
        state,
        item,
        "extraction_result",
        "x",
        {"nodes": [{"title": "a"}], "edges": []},
        persist_now=False,
    )
    _persisted(state)
    fresh = _state(tmp_path / "vault")
    iq.rehydrate_queue(fresh)
    restored = fresh.ingest_queue[0]
    assert restored.events[0]["payload"]["truncated"] is True
    assert iq._retained_extraction_counts(restored.events) == (1, 0, 0)


def test_the_sidecar_is_a_small_fraction_of_the_live_bodies(tmp_path: Path) -> None:
    state = _state(tmp_path / "vault")
    live_bytes = 0
    for i in range(20):
        item = IngestItem(id=str(i), name=f"{i}.md", path=f"/x/{i}.md", status="done")
        state.ingest_queue.append(item)
        for _ in range(20):
            iq.record_event(state, item, "llm_request", "r", _big_request(), persist_now=False)
            iq.record_event(
                state, item, "llm_response", "s", {"response": "x" * 9_000}, persist_now=False
            )
            live_bytes += 40_000 + 9_000
    iq.persist(state)
    size = iq.history_path(state).stat().st_size
    assert size < live_bytes / 4, (size, live_bytes)
    assert size < 20 * (iq.MAX_PERSISTED_EVENT_BYTES_PER_ITEM * 2 + 4_000)


def test_a_sidecar_written_before_the_budget_is_migrated_on_rehydrate(tmp_path: Path) -> None:
    state = _state(tmp_path / "vault")
    legacy = IngestItem(id="1", name="a.md", path="/x/a.md", status="done")
    legacy.events = [
        {"ts": 1.0, "kind": "llm_request", "summary": "r", "payload": _big_request(11_000)}
        for _ in range(30)
    ]
    live = IngestItem(id="2", name="b.md", path="/x/b.md", status="queued")
    live.events = [
        {"ts": 2.0, "kind": "llm_request", "summary": "r", "payload": _big_request(11_000)}
    ]
    path = iq.history_path(state)
    path.parent.mkdir(parents=True)
    from dataclasses import asdict

    path.write_text(json.dumps({"version": 1, "items": [asdict(legacy), asdict(live)]}, indent=2))
    before = path.stat().st_size
    iq.rehydrate_queue(state)
    done, queued = state.ingest_queue
    assert all(e["payload"].get("truncated") is True for e in done.events)
    assert (
        len(json.dumps(done.events, separators=(",", ":"))) <= iq.MAX_PERSISTED_EVENT_BYTES_PER_ITEM
    )
    assert "body_preview" in queued.events[0], "an unfinished item keeps its full body in memory"
    iq.persist(state)
    assert path.stat().st_size < before / 3
