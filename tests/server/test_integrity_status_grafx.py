"""A grafx vault must report its integrity status truthfully (field report: integrity status of a new grafx vault).

Only the Ladybug bootstrap writes ``graph-integrity.json`` and only a store with a graph handle is
write-fenced (``server/_integrity.py`` ``require_write_allowed``). A new grafx vault therefore used
to read "unverified, writer_fenced=true, integrity state is missing" forever: a fence nothing
enforces and nothing lifts. These tests pin the truthful status and the behaviour that must NOT change.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from okto_neuron.server import _integrity as graph_integrity
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests
from okto_neuron.store.capabilities import capabilities_for
from okto_neuron.store.integrity import AuditStatus
from okto_neuron.store.integrity_state import (
    GraphIntegrityState,
    integrity_state_path,
    write_integrity_state,
)


def _runtime(vault_path: Path) -> SimpleNamespace:
    return SimpleNamespace(vault_path=vault_path, integrity_last_audit=None)


def _vault(*, fenced_backend: bool, generation: str = "generation-a") -> SimpleNamespace:
    """A vault double: a store WITH a graph handle is Ladybug-like (fenced), WITHOUT is grafx-like."""
    store = SimpleNamespace(generation=lambda: generation, detect_drift=lambda _generation: None)
    if fenced_backend:
        store._graph_handle = SimpleNamespace(identity_contract_version="v1")
    return SimpleNamespace(store=store, path=None)


def test_grafx_never_audited_vault_is_unverified_but_not_fenced(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "vault")
    result = graph_integrity.summary(runtime, _vault(fenced_backend=False))

    assert result["status"] == "unverified"
    assert result["writer_fenced"] is False
    reason = str(result["reason"])
    assert "no integrity audit has run" in reason
    assert "does not fence writes" in reason
    assert "optional" in reason
    assert "POST /api/v1/graph/integrity" in reason
    assert "missing" not in reason
    assert result["recovery_guidance"] is None


def test_ladybug_never_audited_vault_keeps_its_fence_and_reason(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path / "vault")
    result = graph_integrity.summary(runtime, _vault(fenced_backend=True))

    assert result["status"] == "unverified"
    assert result["writer_fenced"] is True
    assert result["reason"] == "integrity state is missing"


def test_grafx_unreadable_record_is_not_fenced_and_keeps_the_read_error(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    path = integrity_state_path(vault_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")

    result = graph_integrity.summary(_runtime(vault_path), _vault(fenced_backend=False))

    assert result["writer_fenced"] is False
    assert str(result["reason"]).startswith("integrity state is unreadable: ")
    assert "does not fence writes" in str(result["reason"])


def test_grafx_stale_generation_record_is_not_fenced(tmp_path: Path) -> None:
    vault_path = tmp_path / "vault"
    write_integrity_state(
        vault_path,
        GraphIntegrityState(
            status=AuditStatus.VERIFIED,
            graph_generation="old-generation",
            writer_fenced=False,
            audit_id="audit-old",
        ),
    )

    result = graph_integrity.summary(
        _runtime(vault_path), _vault(fenced_backend=False, generation="new-generation")
    )

    assert result["status"] == "unverified"
    assert result["writer_fenced"] is False
    assert "no integrity audit has run" in str(result["reason"])


@pytest.mark.parametrize("status", [AuditStatus.FAILED, AuditStatus.INCOMPLETE])
def test_grafx_recorded_failed_or_incomplete_audit_stays_fenced(
    tmp_path: Path, status: AuditStatus
) -> None:
    vault_path = tmp_path / "vault"
    write_integrity_state(
        vault_path,
        GraphIntegrityState(
            status=status,
            graph_generation="generation-a",
            writer_fenced=True,
            reason="1 issue(s); first=adjacency_property_mismatch:e1",
            audit_id="audit-x",
        ),
    )

    result = graph_integrity.summary(_runtime(vault_path), _vault(fenced_backend=False))

    assert result["status"] == status.value
    assert result["writer_fenced"] is True
    assert result["recovery_guidance"] is not None


def test_write_fence_enforced_follows_the_store_then_the_declared_capability(
    tmp_path: Path,
) -> None:
    assert graph_integrity.write_fence_enforced(_runtime(tmp_path), _vault(fenced_backend=True))
    assert not graph_integrity.write_fence_enforced(
        _runtime(tmp_path), _vault(fenced_backend=False)
    )
    assert capabilities_for("ladybug").write_fence is True
    assert capabilities_for("grafx").write_fence is False
    assert capabilities_for("grafx").audit_supported is True


@pytest.fixture
def no_vault_client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    reset_state_for_tests()
    state = init_state(None, None)
    app = build_rest_app(state)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, state
    reset_state_for_tests()


def _vault_status(client: TestClient, name: str) -> dict:
    body = client.get("/api/v1/status").json()
    entry = next(v for v in body["vaults"] if Path(v["path"]).name == name)
    return {"payload": body, "vault": entry}


def test_new_grafx_vault_status_through_the_real_create_route(no_vault_client) -> None:
    client, _state = no_vault_client
    created = client.post("/api/v1/vaults", json={"name": "fresh-grafx"})
    assert created.status_code == 200, created.text

    seen = _vault_status(client, "fresh-grafx")
    integrity = seen["vault"]["integrity"]
    assert seen["vault"]["backend"] == "grafx"
    assert integrity["status"] == "unverified"
    assert integrity["writer_fenced"] is False
    assert "optional" in integrity["reason"]
    reasons = seen["payload"].get("degraded_reasons") or []
    assert not any(r.startswith("integrity_fenced") for r in reasons), reasons

    # The audit is real and optional: running it records `verified`.
    audited = client.post(
        "/api/v1/graph/integrity",
        json={},
        headers={"X-Okto-Neuron-Vault": str(Path(seen["vault"]["path"]))},
    )
    assert audited.status_code == 200, audited.text
    assert audited.json()["integrity"]["status"] == "verified"
    assert audited.json()["integrity"]["writer_fenced"] is False
    assert _vault_status(client, "fresh-grafx")["vault"]["integrity"]["status"] == "verified"


def test_failed_audit_on_grafx_vault_still_degrades_the_status(no_vault_client) -> None:
    client, _state = no_vault_client
    assert client.post("/api/v1/vaults", json={"name": "bad-grafx"}).status_code == 200
    path = Path(_vault_status(client, "bad-grafx")["vault"]["path"])
    audited = client.post(
        "/api/v1/graph/integrity", json={}, headers={"X-Okto-Neuron-Vault": str(path)}
    )
    generation = audited.json()["integrity"]["graph_generation"]
    write_integrity_state(
        path,
        GraphIntegrityState(
            status=AuditStatus.FAILED,
            graph_generation=generation,
            writer_fenced=True,
            reason="1 issue(s); first=adjacency_property_mismatch:e1",
            audit_id="audit-failed",
        ),
    )

    seen = _vault_status(client, "bad-grafx")
    assert seen["vault"]["integrity"]["status"] == "failed"
    assert seen["vault"]["integrity"]["writer_fenced"] is True
    assert any(r.startswith("integrity_fenced") for r in seen["payload"]["degraded_reasons"])
