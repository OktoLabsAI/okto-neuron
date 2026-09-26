"""Continuous curation loop (ADR 0009 P4) — endpoint + integrated E2E (model-free).

Uses a REAL vault on a tmp path (never the live vault) and the REST TestClient,
but stubs ``_jobs.submit`` so the auto sweep does not spin up the real LLM judge
(propose) — the loop's POLICY and the surfacing endpoint are what we verify, not
the propose/detect domain logic (covered elsewhere). No model is loaded.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from okto_neuron.server import _jobs, _scheduler
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests
from okto_neuron.vault import Vault
from okto_neuron.store import vault as vault_module


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    reset_state_for_tests()
    vault = Vault.init(tmp_path / "v")
    state = init_state(vault, vault.path)
    app = build_rest_app(state)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c
    reset_state_for_tests()
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()


def test_scheduler_endpoint_shape_default(client: TestClient) -> None:
    resp = client.get("/api/v1/curation/scheduler")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["enabled"] is True
    assert body["quiet_debounce_s"] == 60
    assert body["min_interval_s"] == 3600
    # no ingest yet → no sweep, waiting for ingest
    assert body["last_sweep_at"] is None
    assert body["next_eligible"] == "waiting for ingest"
    assert body["recent"] == []


def test_scheduler_endpoint_disabled_via_config(client: TestClient, tmp_path: Path) -> None:
    import yaml

    cfg_path = tmp_path / "v" / "okto-neuron.yaml"
    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    raw["curation"] = {"enabled": False}
    cfg_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    resp = client.get("/api/v1/curation/scheduler")
    body = resp.json()
    # live re-read: toggle takes effect without restart
    assert body["enabled"] is False
    assert body["next_eligible"] == "disabled"


def test_loop_fires_after_ingest_then_surfaces(client: TestClient) -> None:
    """End-to-end: ingest activity → eligible → a tick submits sweeps tagged
    trigger=scheduler → the endpoint surfaces them in ``recent``."""
    from okto_neuron.server.state import get_state

    state = get_state()

    # Stub submit so no real judge/LLM runs; build real CurationJob objects so the
    # endpoint's filtering (kind in SWEEP_KINDS AND trigger=scheduler) is exercised.
    def fake_submit(s, kind, *, label="", params=None):
        job = _jobs.new_job(kind, label=label, params=params)
        job.status = "done"
        job.finished_at = time.time()
        if kind == "reconcile-propose":
            job.result = {"clusters": [], "count": 0}
        elif kind == "predicate-propose":
            job.result = {"pairs_considered": 0, "judged": 0, "outcomes": []}
        else:
            job.result = {"counts": {}, "total": 0}
        s.curation_jobs.append(job)
        return job

    # Simulate ingest that happened well past the debounce window, no prior sweep.
    now = time.time()
    state.last_ingest_at = now - 120  # quiet > 60s

    _scheduler._submit_sweeps(state, now, "e2e probe", submit=fake_submit)

    # The loop recorded its outcome.
    assert state.last_sweep_at == now
    assert len(state.last_sweep_outcome["submitted"]) == len(_scheduler.SWEEP_KINDS)

    # The endpoint surfaces the auto sweeps in recent (trigger=scheduler) with
    # their outcomes from the result payload.
    body = client.get("/api/v1/curation/scheduler").json()
    kinds = {j["kind"] for j in body["recent"]}
    assert kinds == set(_scheduler.SWEEP_KINDS)
    for j in body["recent"]:
        assert j["params"]["trigger"] == "scheduler"


def test_endpoint_excludes_manual_runs_from_recent(client: TestClient) -> None:
    """A MANUAL propose (no trigger) must NOT appear in the loop's ``recent``
    history — that view is the loop's own auto sweeps only."""
    from okto_neuron.server.state import get_state

    state = get_state()
    manual = _jobs.new_job("reconcile-propose", label="manual")  # no trigger param
    manual.status = "done"
    state.curation_jobs.append(manual)

    auto = _jobs.new_job("detect-drift", label="auto", params={"trigger": "scheduler"})
    auto.status = "done"
    state.curation_jobs.append(auto)

    body = client.get("/api/v1/curation/scheduler").json()
    recent_ids = {j["id"] for j in body["recent"]}
    assert auto.id in recent_ids
    assert manual.id not in recent_ids


def test_scheduler_endpoint_loopback_gated(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-loopback caller is 403 (same gate as the other curation endpoints)."""
    import okto_neuron.server.http as http_mod

    monkeypatch.setattr(http_mod, "remote_config_allowed", lambda request: False)
    resp = client.get("/api/v1/curation/scheduler")
    assert resp.status_code == 403
