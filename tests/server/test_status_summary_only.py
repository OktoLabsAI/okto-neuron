"""Status builds summary-only ingest/jobs snapshots (refs #14).

py-spy showed /api/v1/status spending ~86% of its time in the full queue snapshot
(``dataclasses.asdict`` over every history item, twice per call) while keeping only
``["summary"]``. These tests pin that the summary-only path returns the identical
dict, never deep-copies items, and is far cheaper than the full snapshot.
"""

from __future__ import annotations

import dataclasses
import time

import pytest
from starlette.testclient import TestClient

from okto_neuron.server import _ingest_queue as iq
from okto_neuron.server import _jobs
from okto_neuron.server import http as http_mod
from okto_neuron.server._ingest_queue import IngestItem
from okto_neuron.server._jobs import CurationJob
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests


class _StubVault:
    recovered_from_corruption = False

    def close(self) -> None:
        pass


def _events(n: int) -> list[dict]:
    return [
        {
            "kind": "extraction_result" if i % 2 == 0 else "stage",
            "summary": f"event {i}",
            "payload": {"nodes": [{"id": j} for j in range(3)], "edges": [{}], "claims": [{}, {}]},
        }
        for i in range(n)
    ]


def _populate_queue(state, per_status: int = 3, events: int = 6) -> None:
    for status in ("queued", "processing", "done", "error", "cancelled"):
        for k in range(per_status):
            state.ingest_queue.append(
                IngestItem(
                    id=f"{status}-{k}",
                    name=f"{status}{k}.md",
                    path=f"/tmp/{status}{k}.md",
                    status=status,
                    error="boom" if status == "error" else None,
                    provider_error="429" if status == "done" and k == 0 else None,
                    events=_events(events),
                )
            )


@pytest.fixture
def state(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    reset_state_for_tests()
    yield init_state(_StubVault(), tmp_path)
    reset_state_for_tests()


def _old_ingest_summary(state) -> dict:
    """The pre-change inline computation, kept here as an independent oracle."""
    items = state.ingest_queue
    return {
        "total": len(items),
        "queued": sum(1 for i in items if i.status == "queued"),
        "processing": sum(1 for i in items if i.status == "processing"),
        "done": sum(1 for i in items if i.status == "done"),
        "error": sum(1 for i in items if i.status == "error"),
        "cancelled": sum(1 for i in items if i.status == "cancelled"),
        "active": state.ingest_worker_active,
        "cancel_requested": bool(
            state.ingest_worker_active and getattr(state, "ingest_cancel_requested", False)
        ),
    }


def _old_jobs_summary(state, kind=None) -> dict:
    jobs = [j for j in state.curation_jobs if kind is None or j.kind == kind]
    return {
        "total": len(jobs),
        "queued": sum(1 for j in jobs if j.status == "queued"),
        "running": sum(1 for j in jobs if j.status == "running"),
        "done": sum(1 for j in jobs if j.status == "done"),
        "error": sum(1 for j in jobs if j.status == "error"),
        "active": state.curation_worker_active,
    }


# ── 1. parity ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("active,cancel", [(False, False), (True, False), (True, True), (False, True)])
def test_ingest_summary_matches_full_snapshot_in_every_state(state, active, cancel) -> None:
    _populate_queue(state)
    state.ingest_worker_active = active
    state.ingest_cancel_requested = cancel

    new = iq.summary(state)

    assert new == iq.snapshot(state)["summary"]
    assert new == _old_ingest_summary(state)
    assert new["total"] == 15 and new["queued"] == 3 and new["cancelled"] == 3
    assert new["cancel_requested"] is (active and cancel)


def test_ingest_summary_empty_queue(state) -> None:
    assert iq.summary(state) == iq.snapshot(state)["summary"] == _old_ingest_summary(state)
    assert iq.summary(state)["total"] == 0


@pytest.mark.parametrize("kind", [None, "reconcile-propose", "reconcile-apply", "absent"])
def test_jobs_summary_matches_full_snapshot_in_every_status(state, kind) -> None:
    for job_kind in ("reconcile-propose", "reconcile-apply"):
        for status in ("queued", "running", "done", "error"):
            for k in range(2):
                state.curation_jobs.append(
                    CurationJob(
                        id=f"{job_kind}-{status}-{k}",
                        kind=job_kind,
                        status=status,
                        result={"big": list(range(50))} if status == "done" else None,
                    )
                )
    state.curation_worker_active = True

    new = _jobs.summary(state, kind=kind)

    assert new == _jobs.snapshot(state, kind=kind)["summary"]
    assert new == _old_jobs_summary(state, kind)


# ── 2. counted: no asdict on the status path ───────────────────────────────


def test_status_endpoint_never_deep_copies_queue_items(state, monkeypatch) -> None:
    _populate_queue(state)
    state.curation_jobs.append(CurationJob(id="j1", kind="reconcile-propose", status="done"))
    calls: list[object] = []
    real_asdict = dataclasses.asdict

    def counting_asdict(obj, *args, **kwargs):
        calls.append(obj)
        return real_asdict(obj, *args, **kwargs)

    monkeypatch.setattr(dataclasses, "asdict", counting_asdict)
    monkeypatch.setattr(iq, "asdict", counting_asdict)
    monkeypatch.setattr(_jobs, "asdict", counting_asdict, raising=False)
    monkeypatch.setattr(http_mod, "asdict", counting_asdict, raising=False)

    with TestClient(build_rest_app(state), base_url="http://127.0.0.1") as client:
        body = client.get("/api/v1/status").json()

    assert [c for c in calls if isinstance(c, IngestItem)] == []
    assert calls == []
    assert body["ingest"]["total"] == 15
    # The counter is live: the full-snapshot route does deep-copy items.
    iq.snapshot(state)
    assert len([c for c in calls if isinstance(c, IngestItem)]) == 15


def test_status_payload_values_unchanged(state) -> None:
    _populate_queue(state)
    state.curation_jobs.append(CurationJob(id="j1", kind="reconcile-propose", status="running"))

    payload = http_mod._status_payload(state)

    zero_inline = {"inline": {"processing": 0, "done": 0, "error": 0}}
    assert payload["ingest"] == _old_ingest_summary(state) | zero_inline
    (vault,) = payload["vaults"]
    assert vault["ingest"] == _old_ingest_summary(state) | zero_inline
    assert vault["curation"] == _old_jobs_summary(state)


# ── 3. performance ─────────────────────────────────────────────────────────


def _best_of(fn, n: int = 5) -> float:
    best = float("inf")
    for _ in range(n):
        start = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - start)
    return best


def test_summary_is_cheap_on_a_long_history_queue(state) -> None:
    for i in range(2000):
        state.ingest_queue.append(
            IngestItem(
                id=f"h-{i}",
                name=f"h{i}.md",
                path=f"/tmp/h{i}.md",
                status="done" if i % 7 else "error",
                events=_events(5),
            )
        )

    new = _best_of(lambda: iq.summary(state))
    old = _best_of(lambda: iq.snapshot(state))

    assert new < 0.020, f"summary {new * 1000:.2f} ms (full snapshot {old * 1000:.2f} ms)"
    assert new < old / 5, f"summary {new * 1000:.2f} ms vs full snapshot {old * 1000:.2f} ms"
