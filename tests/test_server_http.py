"""Unit tests for the marginalia REST server (card fe117e9f).

Exercises every endpoint via the Starlette TestClient against a stubbed
in-memory ServerState — no real Ladybug vault is required, which keeps
the gate CI-safe. Acceptance scenarios live in sibling Test cards and
drive the full client/server stack end-to-end.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from starlette.requests import Request
from starlette.testclient import TestClient

from okto_neuron import __version__
from okto_neuron.server import http as http_mod
from okto_neuron.server import state as state_mod
from okto_neuron.server import _folder_watch as folder_watch_mod
from okto_neuron.server._ingest_queue import IngestItem
from okto_neuron.consolidate.ledger import CandidateLedger
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests


@pytest.fixture(autouse=True)
def _isolate_folder_watch_status():
    """Keep module-global watcher telemetry from leaking between HTTP tests."""
    folder_watch_mod._WATCH_STATUS.clear()
    yield
    folder_watch_mod._WATCH_STATUS.clear()


class _StubDoc:
    id = "doc_test_1"
    title = "stub"
    path = "stub.md"


class _StubNode:
    id = "doc_test_1"
    title = "stub-title"
    name = "stub"
    type = "Document"


class _StubHit:
    node = _StubNode()
    score = 0.42

    @property
    def claim_id(self):
        return None

    @property
    def path(self):
        return "/tmp/x.md"

    @property
    def byte_start(self):
        return 0

    @property
    def byte_end(self):
        return 1

    @property
    def content_hash(self):
        return "0" * 64


class _StubVault:
    def __init__(self) -> None:
        self.added: list[Path] = []
        self.closed = False

    def add(self, source):
        self.added.append(Path(source))
        return _StubDoc()

    def query(self, text, *, k=5, type=None, with_drift=False):
        return [_StubHit()]

    def close(self):
        self.closed = True


@pytest.fixture
def client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    reset_state_for_tests()
    vault = _StubVault()
    state = init_state(vault, tmp_path)
    app = build_rest_app(state)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c, state
    reset_state_for_tests()


@pytest.fixture
def no_vault_client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    reset_state_for_tests()
    state = init_state(None, None)
    app = build_rest_app(state)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c, state
    reset_state_for_tests()


def test_health_ok(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_operational_status_is_separate_from_public_liveness(client):
    c, state = client
    r = c.get("/api/v1/status")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["vault_path"] == str(state.vault_path)
    assert body["active_vault"] is True
    assert body["pid"] == os.getpid()
    assert isinstance(body["uptime_s"], int)
    assert body["ingest"] == {
        "total": 0,
        "queued": 0,
        "processing": 0,
        "done": 0,
        "error": 0,
        "cancelled": 0,
        "active": False,
        "cancel_requested": False,
        "inline": {"processing": 0, "done": 0, "error": 0},
    }


def test_health_ok_without_active_vault(no_vault_client):
    c, _ = no_vault_client

    r = c.get("/health")

    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    body = c.get("/api/v1/status").json()
    assert body["vault_path"] is None
    assert body["active_vault"] is False


def test_health_503_only_when_process_is_shutting_down(client):
    c, state = client
    state.mark_shutting_down()
    r = c.get("/health")
    assert r.status_code == 503
    assert r.json() == {"status": "shutting_down"}


def test_version(client):
    c, _ = client
    r = c.get("/version")
    assert r.status_code == 200
    body = r.json()
    assert body["api_version"] == "v1"
    assert body["okto_neuron_version"] == __version__
    assert body["marginalia_version"] == __version__
    assert set(body) == {"okto_neuron_version", "marginalia_version", "api_version"}

    diagnostics = c.get("/api/v1/status").json()
    assert diagnostics["embedding_model"] == "bge-small-en-v1.5"
    assert "ladybug_version" in diagnostics


def test_vaults_list_includes_current_server_vault(client):
    c, state = client

    r = c.get("/api/v1/vaults")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["current"]["path"] == str(state.vault_path)
    assert body["vaults"][0]["current"] is True


def test_vaults_list_surfaces_pinned_backend_end_to_end(no_vault_client, tmp_path):
    """D-94 follow-up: ``GET /api/v1/vaults`` surfaces each vault's pinned
    graph backend for a real (non-stubbed) vault, not just via the
    ``VaultEntry``/registry unit tests -- created through the real
    ``api_vault_create`` route (no ``_create_inheriting_vault`` monkeypatch),
    exercising ``vault_registry.py``'s ``resolve_vault_backend`` against an
    actual on-disk ``okto-neuron.yaml``."""
    c, _state = no_vault_client

    r = c.post("/api/v1/vaults", json={"name": "grafx-e2e"})
    assert r.status_code == 200, r.text

    listed = c.get("/api/v1/vaults")
    assert listed.status_code == 200, listed.text
    entry = next(item for item in listed.json()["vaults"] if item["name"] == "grafx-e2e")
    assert entry["backend"] == "grafx"


def test_vaults_list_allows_no_active_vault(no_vault_client):
    c, _ = no_vault_client

    r = c.get("/api/v1/vaults")

    assert r.status_code == 200
    assert r.json() == {"status": "ok", "current": None, "vaults": []}


def test_vaults_list_surfaces_startup_vault_warning(no_vault_client, tmp_path):
    c, state = no_vault_client
    broken = tmp_path / "home" / ".okto-neuron" / "vaults" / "broken"
    broken.mkdir(parents=True)
    (broken / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    state.vault_open_error = {
        "code": "embedding_dim_mismatch",
        "path": str(broken.resolve(strict=False)),
        "detail": "embedding dimension 1024 does not match width 384",
        "remedy": "run reembed",
    }

    r = c.get("/api/v1/vaults")

    assert r.status_code == 200
    body = r.json()
    assert body["current"] is None
    assert body["warning"]["code"] == "embedding_dim_mismatch"
    assert body["vaults"][0]["name"] == "broken"
    assert body["vaults"][0]["issue"]["path"] == str(broken.resolve(strict=False))


def test_vault_reembed_repairs_warning_without_changing_selection(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    c, state = no_vault_client
    broken = tmp_path / "home" / ".okto-neuron" / "vaults" / "broken"
    broken.mkdir(parents=True)
    (broken / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    state.vault_open_error = {
        "code": "embedding_dim_mismatch",
        "path": str(broken.resolve(strict=False)),
        "detail": "embedding dimension 1024 does not match width 384",
        "remedy": "run reembed",
    }
    calls: list[Path] = []

    import okto_neuron.cli.kg as kg_mod

    monkeypatch.setattr(kg_mod, "kg_reembed", lambda path: calls.append(Path(path)))
    repaired = _StubVault()
    monkeypatch.setattr(http_mod.Vault, "open", staticmethod(lambda path: repaired))
    workers = []
    monkeypatch.setattr(
        http_mod,
        "_start_owned_maintenance",
        lambda state, worker, name: workers.append(worker),
    )

    r = c.post("/api/v1/vaults/reembed", json={"vault": str(broken)})
    manager_status = c.get(f"/api/v1/vaults/reembed/status?vault={broken}")
    runtime_status = c.get(
        "/api/v1/embedding/reembed/status",
        headers={"X-Okto-Neuron-Vault": str(broken)},
    )
    import asyncio

    assert manager_status.status_code == 200, manager_status.text
    assert manager_status.json()["running"] is True
    assert runtime_status.status_code == 200, runtime_status.text
    assert runtime_status.json()["running"] is True
    asyncio.run(workers[0]())

    assert r.status_code == 202, r.text
    assert calls == [broken.resolve(strict=False)]
    assert state.vault is None
    assert state.vault_path is None
    assert state.vault_pool.peek(broken) is repaired
    assert state.vault_open_error is None
    runtime = state.runtime_for(broken)
    assert runtime.vault_reembed_active is False
    assert runtime.draining is False


def test_vault_reembed_status_reads_target_sidecar(no_vault_client, tmp_path):
    c, state = no_vault_client
    target = tmp_path / "home" / ".okto-neuron" / "vaults" / "target"
    sidecar = target / ".marginalia"
    sidecar.mkdir(parents=True)
    (target / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    (sidecar / "reembed.state.json").write_text(
        '{"phase":"embedding","nodes_done":2,"nodes_total":5}',
        encoding="utf-8",
    )
    runtime = state.runtime_for(target)
    runtime.vault_reembed_active = True
    runtime.vault_reembed_path = str(target.resolve(strict=False))

    r = c.get(f"/api/v1/vaults/reembed/status?vault={target}")

    assert r.status_code == 200
    body = r.json()
    assert body["phase"] == "embedding"
    assert body["nodes_done"] == 2
    assert body["nodes_total"] == 5
    assert body["running"] is True


def test_content_routes_reject_without_active_vault(no_vault_client):
    c, _ = no_vault_client

    r = c.post("/query", json={"query": "what"})

    assert r.status_code == 409
    assert r.json()["error"] == "no_active_vault"


def test_bare_review_batch_rejects_without_active_vault(no_vault_client):
    c, _ = no_vault_client

    r = c.post(
        "/review-queue/batch",
        json={"candidate_ids": ["candidate"], "action": "discard"},
    )

    assert r.status_code == 409
    assert r.json()["error"] == "no_active_vault"


def test_bare_review_batch_uses_immutable_request_vault(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    c, state = no_vault_client
    root = tmp_path / "home" / ".okto-neuron" / "vaults"
    path_a, path_b = root / "alpha", root / "beta"
    for path in (path_a, path_b):
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
        state.runtime_for(path, vault=_StubVault())

    resolved: list[tuple[Path, str, str]] = []

    class _Companion:
        def __init__(self, vault_path: Path) -> None:
            self.vault_path = vault_path

        def resolve_review(self, candidate_id: str, action: str, **_bounds: object) -> None:
            resolved.append((self.vault_path, candidate_id, action))

    def _selected_companion(runtime):
        return _Companion(runtime.vault_path)

    monkeypatch.setattr(http_mod, "_companion", _selected_companion)

    response = c.post(
        "/review-queue/batch",
        headers={"X-Okto-Neuron-Vault": str(path_b)},
        json={"candidate_ids": ["candidate"], "action": "discard"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "ok",
        "resolved": 1,
        "skipped": 0,
        "errors": [],
    }
    assert response.headers["x-okto-neuron-vault"] == str(path_b.resolve(strict=False))
    assert resolved == [(path_b.resolve(strict=False), "candidate", "discard")]
    assert state.vault is None
    assert state.vault_path is None


def test_duplicate_vault_name_is_409_for_scoped_and_manager_rest(
    no_vault_client,
    tmp_path,
):
    c, _ = no_vault_client
    home = tmp_path / "home"
    roots = [home / "primary-vaults", home / "secondary-vaults"]
    for root in roots:
        target = root / "duplicate"
        target.mkdir(parents=True)
        (target / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    config_path = home / ".okto-neuron" / "okto-neuron.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        "marginalia_toml_version = 1\n"
        + f"vault_roots = {json.dumps([str(root) for root in roots])}\n",
        encoding="utf-8",
    )

    scoped = c.post(
        "/query",
        headers={"X-Okto-Neuron-Vault": "duplicate"},
        json={"query": "where"},
    )
    manager = c.post("/api/v1/vaults/switch", json={"vault": "duplicate"})

    for response in (scoped, manager):
        assert response.status_code == 409, response.text
        assert response.json()["error"] == "ambiguous_vault"
        assert "absolute path" in response.json()["detail"]


def test_vault_create_initializes_without_mutating_application_selection(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    c, state = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    new_vault = _StubVault()

    def _init(path, *, packs, embedding_provider):
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
        assert packs is None
        assert embedding_provider == "stub"
        return new_vault

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _init)

    r = c.post("/api/v1/vaults", json={"name": "alpha", "embedder": "stub"})

    vault_path = tmp_path / "home" / ".okto-neuron" / "vaults" / "alpha"
    assert r.status_code == 200, r.text
    assert state.vault is None
    assert state.vault_path is None
    body = r.json()
    assert body["current"] is None
    assert body["created"]["name"] == "alpha"
    assert body["created"]["path"] == str(vault_path.resolve(strict=False))
    assert body["created"]["managed"] is True
    assert body["created"]["deletable"] is True
    assert new_vault.closed is True
    assert state.runtime_for(vault_path).vault_path == vault_path.resolve(strict=False)
    assert state.vault_pool.peek(vault_path) is None


def test_vault_create_with_explicit_ladybug_backend_pins_it(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """M3 spec section 2.6: an explicit ``"backend": "ladybug"`` in the create
    payload is validated via the registry (``resolve_graph_backend``) and
    threaded through to vault creation. This stub's write mirrors what a real
    ``Vault._write_config`` pins for the (legacy, no-longer-default) ladybug
    backend (spec section 2.5). Since D-94 flipped the product default to
    grafx, an explicit ``"ladybug"`` is a non-default request and widens the
    ``_create_inheriting_vault`` call (``server/http.py``'s ``_initialize_managed_vault``
    compares against ``DEFAULT_NEW_VAULT_BACKEND``, not a literal ladybug),
    so this stub takes the full keyword set rather than the narrow 2-keyword
    shape ``test_vault_create_default_backend_is_grafx`` uses.
    """
    c, state = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    new_vault = _StubVault()

    def _init(
        path,
        *,
        packs,
        embedding_provider,
        backend,
        storage_uri,
        storage_credential_env,
        storage_database,
        storage_allow_remote,
    ):
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text(
            "marginalia_yaml_version: 1\n"
            "storage:\n"
            "  backend: ladybug\n"
            "  reason: null\n",
            encoding="utf-8",
        )
        assert packs is None
        assert embedding_provider == "stub"
        assert backend == "ladybug"
        assert storage_uri is None
        assert storage_credential_env is None
        assert storage_database is None
        assert storage_allow_remote is False
        return new_vault

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _init)

    r = c.post(
        "/api/v1/vaults",
        json={"name": "backend-ladybug", "embedder": "stub", "backend": "ladybug"},
    )

    assert r.status_code == 200, r.text
    assert state.vault is None
    vault_path = tmp_path / "home" / ".okto-neuron" / "vaults" / "backend-ladybug"
    written = yaml.safe_load((vault_path / "okto-neuron.yaml").read_text(encoding="utf-8"))
    assert written["storage"]["backend"] == "ladybug"


def test_vault_create_default_backend_is_grafx(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """D-94: when the create payload omits ``backend`` entirely, the resolved
    default is ``config._vault.DEFAULT_NEW_VAULT_BACKEND`` ("grafx"), not the
    historical "ladybug" literal. Grafx now *is* the product default, so this
    still exercises the narrow 2-keyword ``_create_inheriting_vault`` stub
    shape (the same DI seam ``test_vault_create_initializes_without_mutating_application_selection``
    uses) — a non-default backend request is the one that would widen the call."""
    c, state = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    new_vault = _StubVault()

    def _init(path, *, packs, embedding_provider):
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text(
            "marginalia_yaml_version: 1\n"
            "storage:\n"
            "  backend: grafx\n"
            "  reason: null\n",
            encoding="utf-8",
        )
        assert packs is None
        assert embedding_provider == "stub"
        return new_vault

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _init)

    r = c.post("/api/v1/vaults", json={"name": "backend-default", "embedder": "stub"})

    assert r.status_code == 200, r.text
    assert state.vault is None
    vault_path = tmp_path / "home" / ".okto-neuron" / "vaults" / "backend-default"
    written = yaml.safe_load((vault_path / "okto-neuron.yaml").read_text(encoding="utf-8"))
    assert written["storage"]["backend"] == "grafx"
    assert new_vault.closed is True


def test_vault_create_neo4j_without_consent_rejects_non_loopback_storage_uri(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A non-loopback ``storage_uri`` without ``allow_remote_db`` is a clear
    400 naming that exact field -- server/http.py's ``api_vault_create``
    egress gate, mirroring the CLI's ``--allow-remote-db`` consent and the
    LLM ``allow_remote`` gate's ``_check_api_base`` shape (the project rule is that
    non-loopback database (graph backend) endpoints require the same egress consent
    as remote LLM endpoints). No ``_create_inheriting_vault`` monkeypatch is
    needed since this fails during payload validation, before any filesystem
    work happens."""
    c, _state = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    r = c.post(
        "/api/v1/vaults",
        json={
            "name": "neo4j-no-consent",
            "backend": "neo4j",
            "storage_uri": "bolt://db.example.com:7687",
            "storage_credential_env": "OKTO_NEURON_NEO4J_PASSWORD",
        },
    )
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["error"] == "bad_request"
    assert "allow_remote_db" in body["detail"]


def test_vault_create_neo4j_with_consent_pins_it(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """``"allow_remote_db": true`` unblocks a non-loopback ``neo4j``
    ``storage_uri`` and threads ``storage_allow_remote`` through to
    ``_create_inheriting_vault`` (which forwards it to ``Vault.scaffold``) --
    same stub-based pin-writing shape the ladybug/grafx tests above use."""
    pytest.importorskip("neo4j")
    c, state = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    new_vault = _StubVault()

    def _init(
        path,
        *,
        packs,
        embedding_provider,
        backend,
        storage_uri,
        storage_credential_env,
        storage_database,
        storage_allow_remote,
    ):
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text(
            "marginalia_yaml_version: 1\n"
            "storage:\n"
            "  backend: neo4j\n"
            "  reason: null\n",
            encoding="utf-8",
        )
        assert packs is None
        assert embedding_provider == "stub"
        assert backend == "neo4j"
        assert storage_uri == "bolt://db.example.com:7687"
        assert storage_credential_env == "OKTO_NEURON_NEO4J_PASSWORD"
        assert storage_database == "neo4j"
        assert storage_allow_remote is True
        return new_vault

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _init)

    r = c.post(
        "/api/v1/vaults",
        json={
            "name": "neo4j-consented",
            "embedder": "stub",
            "backend": "neo4j",
            "storage_uri": "bolt://db.example.com:7687",
            "storage_credential_env": "OKTO_NEURON_NEO4J_PASSWORD",
            "storage_database": "neo4j",
            "allow_remote_db": True,
        },
    )

    assert r.status_code == 200, r.text
    assert state.vault is None
    vault_path = tmp_path / "home" / ".okto-neuron" / "vaults" / "neo4j-consented"
    written = yaml.safe_load((vault_path / "okto-neuron.yaml").read_text(encoding="utf-8"))
    assert written["storage"]["backend"] == "neo4j"
    assert new_vault.closed is True


def test_vault_create_neo4j_loopback_storage_uri_needs_no_consent(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A loopback ``storage_uri`` (the local Neo4j default) never needs
    ``allow_remote_db`` -- the egress gate only fires for a non-loopback
    host, matching the LLM ``allow_remote`` gate's loopback carve-out."""
    pytest.importorskip("neo4j")
    c, state = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    new_vault = _StubVault()

    def _init(
        path,
        *,
        packs,
        embedding_provider,
        backend,
        storage_uri,
        storage_credential_env,
        storage_database,
        storage_allow_remote,
    ):
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text(
            "marginalia_yaml_version: 1\nstorage:\n  backend: neo4j\n  reason: null\n",
            encoding="utf-8",
        )
        assert storage_allow_remote is False
        return new_vault

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _init)

    r = c.post(
        "/api/v1/vaults",
        json={
            "name": "neo4j-loopback",
            "embedder": "stub",
            "backend": "neo4j",
            "storage_uri": "bolt://127.0.0.1:7687",
            "storage_credential_env": "OKTO_NEURON_NEO4J_PASSWORD",
        },
    )

    assert r.status_code == 200, r.text
    assert state.vault is None


def test_vault_create_rejects_unknown_backend(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """An unresolvable backend name fails registry validation before any
    filesystem work happens — no ``_create_inheriting_vault`` monkeypatch
    needed, since ``resolve_graph_backend`` raises inside the request's
    initial payload-parsing block (server/http.py's ``api_vault_create``)."""
    c, _state = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    r = c.post("/api/v1/vaults", json={"name": "backend-nope", "backend": "nope"})

    assert r.status_code == 400, r.text
    body = r.json()
    assert body["error"] == "bad_request"
    assert "nope" in body["detail"]


def test_vault_create_rejects_experimental_backend_without_consent(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The Web UI's own D-12 gate (M4 spec section 2, ``server/http.py``'s
    ``api_vault_create``): creating against a backend whose registered
    capabilities mark it ``experimental`` is rejected before any filesystem
    work happens unless the payload sets ``"accept_experimental": true`` —
    mirroring the CLI gate's own onboarding confirmation, but enforced
    server-side so the frontend checkbox is not merely cosmetic.

    D-94 retired ``grafx``'s own ``experimental`` flag (it's the default,
    non-experimental graph backend now), so no OFFICIAL backend is
    experimental any more to exercise this against directly. The gate itself
    (``capabilities_for(backend).experimental``) is unchanged and still the
    live path a future experimental backend (in-tree or third-party) would
    hit, so this proves it by patching a fake ``experimental=True``
    capability onto the registered "grafx" name rather than deleting the
    coverage."""
    c, _state = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    from dataclasses import replace

    real_capabilities_for = http_mod.capabilities_for

    def fake_capabilities_for(name: str):
        caps = real_capabilities_for(name)
        if caps is not None and name == "grafx":
            caps = replace(caps, experimental=True)
        return caps

    monkeypatch.setattr(http_mod, "capabilities_for", fake_capabilities_for)

    r = c.post("/api/v1/vaults", json={"name": "grafx-no-consent", "backend": "grafx"})
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["error"] == "bad_request"
    assert "accept_experimental" in body["detail"]
    assert "grafx" in body["detail"]

    r = c.post(
        "/api/v1/vaults",
        json={"name": "grafx-explicit-false", "backend": "grafx", "accept_experimental": False},
    )
    assert r.status_code == 400, r.text


def test_vault_create_grafx_with_consent_pins_it(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """``"accept_experimental": true`` against ``grafx`` (the default,
    non-experimental graph backend since D-94; still deprecated-but-accepted
    on this payload field for compatibility) lets vault creation proceed
    (200, not 400) and the written ``okto-neuron.yaml`` pins ``grafx`` -- same
    narrow ``_create_inheriting_vault`` stub-based shape
    ``test_vault_create_default_backend_is_grafx`` uses, since an explicit
    ``"backend": "grafx"`` now equals ``DEFAULT_NEW_VAULT_BACKEND`` and no
    longer widens the call the way ``test_vault_create_with_explicit_ladybug_backend_pins_it``'s
    now-non-default ``"ladybug"`` does. This locks in that the deprecated
    field is a pure no-op, never a blocker, for how the pin itself gets
    written."""
    c, state = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    new_vault = _StubVault()

    def _init(path, *, packs, embedding_provider):
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text(
            "marginalia_yaml_version: 1\n"
            "storage:\n"
            "  backend: grafx\n"
            "  reason: null\n",
            encoding="utf-8",
        )
        assert packs is None
        assert embedding_provider == "stub"
        return new_vault

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _init)

    r = c.post(
        "/api/v1/vaults",
        json={
            "name": "grafx-consented",
            "embedder": "stub",
            "backend": "grafx",
            "accept_experimental": True,
        },
    )

    assert r.status_code == 200, r.text
    assert state.vault is None
    vault_path = tmp_path / "home" / ".okto-neuron" / "vaults" / "grafx-consented"
    written = yaml.safe_load((vault_path / "okto-neuron.yaml").read_text(encoding="utf-8"))
    assert written["storage"]["backend"] == "grafx"
    assert new_vault.closed is True


def test_vault_create_rejects_non_boolean_accept_experimental(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A non-boolean ``accept_experimental`` is a clear 400, never a 500 from
    an unguarded truthiness check."""
    c, _state = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    r = c.post(
        "/api/v1/vaults",
        json={"name": "grafx-bad-type", "backend": "grafx", "accept_experimental": "yes"},
    )
    assert r.status_code == 400, r.text
    assert "boolean" in r.json()["detail"]


def test_vault_create_never_gates_ladybug_on_accept_experimental(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """``ladybug`` carries no ``experimental`` capability flag, so the D-12
    gate never fires for it regardless of ``accept_experimental`` -- this is
    the same (now non-default, widened) ``_create_inheriting_vault`` stub-based
    path ``test_vault_create_with_explicit_ladybug_backend_pins_it`` already
    exercises, reused here to prove the new gate leaves it untouched."""
    c, state = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    new_vault = _StubVault()

    def _init(path, *, packs, embedding_provider, **_storage_kwargs):
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text(
            "marginalia_yaml_version: 1\nstorage:\n  backend: ladybug\n  reason: null\n",
            encoding="utf-8",
        )
        return new_vault

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _init)

    r = c.post("/api/v1/vaults", json={"name": "ladybug-ungated", "backend": "ladybug"})

    assert r.status_code == 200, r.text
    assert state.vault is None


def test_vault_create_forbidden_from_remote_peer_even_with_backend(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The pre-existing loopback gate (``remote_config_allowed``) still applies
    unchanged when a ``backend`` is present in the payload — the field adds a
    new validation branch, not a new gate, so a spoofed non-loopback peer
    (matching ``tests/server/test_http_security_gates.py``'s established
    ``client=(\"203.0.113.7\", 5555)`` pattern) must still get the existing 403
    before backend validation is even reached."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    reset_state_for_tests()
    state = init_state(None, None)
    app = build_rest_app(state)
    try:
        with TestClient(
            app, base_url="http://127.0.0.1", client=("203.0.113.7", 5555)
        ) as c:
            r = c.post(
                "/api/v1/vaults",
                json={"name": "backend-remote", "backend": "ladybug"},
            )
    finally:
        reset_state_for_tests()

    assert r.status_code == 403, r.text
    assert r.json()["error"] == "forbidden"


def test_api_backends_lists_ladybug_grafx_and_neo4j_never_neptune(no_vault_client):
    """M4 spec section 3 (site-by-site table, ``store/registry.py:34
    _OFFICIAL``): ``grafx`` is officially registered alongside ``ladybug``,
    ``GET /api/v1/backends`` lists both. ``grafx`` was flagged
    ``experimental`` under D-12; D-94 retired that flag outright (Grafx is
    the default, non-experimental graph backend now) --
    ``GRAFX_CAPABILITIES.experimental`` is ``False``. M5 additionally
    registers ``neo4j`` (non-experimental, ``NEO4J_CAPABILITIES.experimental
    = False``) -- updated from the M4-era name/assertion (which predates M5
    registering neo4j) to reflect that. ``neptune`` remains unregistered --
    it exists only in the config type system today (spec section 2.1, out of
    M5 scope) -- so it is still never listed."""
    c, _state = no_vault_client

    r = c.get("/api/v1/backends")

    assert r.status_code == 200, r.text
    body = r.json()
    names = {entry["name"] for entry in body}
    assert "ladybug" in names
    assert "grafx" in names
    assert "neo4j" in names
    assert "neptune" not in names

    ladybug_entry = next(entry for entry in body if entry["name"] == "ladybug")
    assert ladybug_entry["capabilities"]["name"] == "ladybug"
    assert ladybug_entry["capabilities"]["concurrency_model"] == "single_writer"
    assert ladybug_entry["capabilities"]["requires_network"] is False

    grafx_entry = next(entry for entry in body if entry["name"] == "grafx")
    assert grafx_entry["capabilities"]["name"] == "grafx"
    assert grafx_entry["capabilities"]["experimental"] is False

    neo4j_entry = next(entry for entry in body if entry["name"] == "neo4j")
    assert neo4j_entry["capabilities"]["name"] == "neo4j"
    assert neo4j_entry["capabilities"]["experimental"] is False
    assert neo4j_entry["capabilities"]["requires_network"] is True


def test_api_backends_survives_grafx_extra_missing(no_vault_client, monkeypatch):
    """``GET /api/v1/backends`` must not 500 when ``okto-grafx`` (the
    ``[grafx]`` extra) isn't installed.

    ``list_graph_backends()`` enumerates ``_OFFICIAL`` names only and never
    imports a backend module (``store/registry.py``'s own module docstring),
    so the route can't actually observe a missing extra today -- this test
    locks in *why* that's true (a thin passthrough of an import-free
    enumeration) rather than asserting a tautology. It does so by reproducing
    ``store/registry.py:_import_target``'s real missing-extra classification
    (a ``ModuleNotFoundError`` whose failing module is *not* the target
    itself gets converted to a ``NoSuchBackendError`` naming
    ``okto-neuron[grafx]`` -- module docstring, "Two distinct failures") against
    a faked-absent ``okto_grafx``, confirming ``resolve_graph_backend``
    itself degrades correctly, and then swaps the route's enumeration for one
    that actually validates each name the same way, proving the route
    answers 200 with ladybug only rather than raising.
    """
    import importlib as real_importlib

    from okto_neuron.store import registry as registry_mod

    class _FakeImportlib:
        """Delegates to the real ``importlib`` for everything except the
        one target this test fakes as missing its optional dependency --
        rebinding just ``registry_mod``'s own ``importlib`` name (not the
        real global module) so nothing else importing anything else during
        this test is affected.
        """

        @staticmethod
        def import_module(name: str, *args: object, **kwargs: object) -> object:
            if name == "okto_neuron.store.grafx":
                raise ModuleNotFoundError("No module named 'okto_grafx'", name="okto_grafx")
            return real_importlib.import_module(name, *args, **kwargs)

    monkeypatch.setattr(registry_mod, "importlib", _FakeImportlib)

    with pytest.raises(registry_mod.NoSuchBackendError, match=r"okto-neuron\[grafx\]"):
        registry_mod.resolve_graph_backend("grafx")

    def _validated_names() -> list[str]:
        names = []
        for name in registry_mod.list_graph_backends():
            try:
                registry_mod.resolve_graph_backend(name)
            except registry_mod.NoSuchBackendError:
                continue
            names.append(name)
        return names

    monkeypatch.setattr(http_mod, "list_graph_backends", _validated_names)

    c, _state = no_vault_client
    r = c.get("/api/v1/backends")

    assert r.status_code == 200, r.text
    # "ladybug only" among the two M4 official backends -- ``grafx`` is the
    # one whose import was faked absent, so it alone must fall out of the
    # validated listing; a real, independently-installed entry-point backend
    # (the ``stub`` test fixture, see the docstring above) is unaffected and
    # may legitimately still be present.
    names = {entry["name"] for entry in r.json()}
    assert "ladybug" in names
    assert "grafx" not in names


def test_application_defaults_create_sparse_inheriting_vault(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    from okto_neuron.config import VaultConfig

    c, state = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    defaults = c.get("/api/v1/config/defaults")
    assert defaults.status_code == 200, defaults.text
    assert defaults.json()["scope"] == "application"
    assert defaults.json()["embedding"]["batch_size"] == 32
    assert defaults.json()["embedding"]["max_concurrent_batches"] == 1
    assert defaults.json()["ingest"]["chunk_size_bytes"] == 6_000
    assert defaults.json()["ingest"]["chunk_overlap_bytes"] == 0

    configured = c.patch(
        "/api/v1/config/defaults",
        json={
            "embedding": {
                "provider": "stub",
                "model": "application-model",
                "dimension": 64,
            }
        },
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["affected_vaults"] == []

    opened = _StubVault()
    monkeypatch.setattr(http_mod.Vault, "open", staticmethod(lambda path: opened))
    created = c.post("/api/v1/vaults", json={"name": "inheriting"})

    path = tmp_path / "home" / ".okto-neuron" / "vaults" / "inheriting"
    assert created.status_code == 200, created.text
    raw = VaultConfig.load_raw(path)
    assert raw["inherits_application_defaults"] is True
    assert "embedding" not in raw
    assert "packs" not in raw
    effective = VaultConfig.load(path)
    assert effective.embedding.provider == "stub"
    assert effective.embedding.model == "application-model"
    assert effective.embedding.dimension == 64
    assert opened.closed is True
    assert state.vault_path is None

    execution_change = c.patch(
        "/api/v1/config/defaults",
        json={
            "embedding": {
                "batch_size": 64,
                "max_concurrent_batches": 16,
            }
        },
    )
    assert execution_change.status_code == 200, execution_change.text
    assert execution_change.json()["applied"] == "live"
    assert execution_change.json()["affected_vaults"] == []
    effective = VaultConfig.load(path)
    assert effective.embedding.batch_size == 64
    assert effective.embedding.max_concurrent_batches == 16

    chunk_change = c.patch(
        "/api/v1/config/defaults",
        json={"ingest": {"chunk_size_bytes": 20_000, "chunk_overlap_bytes": 2_000}},
    )
    assert chunk_change.status_code == 200, chunk_change.text
    assert chunk_change.json()["applied"] == "live"
    assert any("next ingest" in note for note in chunk_change.json()["notes"])
    effective = VaultConfig.load(path)
    assert effective.ingest.chunk_size_bytes == 20_000
    assert effective.ingest.chunk_overlap_bytes == 2_000

    semantic_change = c.patch(
        "/api/v1/config/defaults",
        json={"consolidation": {"relation_curator_enabled": False}},
    )
    assert semantic_change.status_code == 200, semantic_change.text
    assert semantic_change.json()["applied"] == "rebuild"
    assert semantic_change.json()["rebuild_required_vaults"] == [str(path.resolve(strict=False))]
    assert VaultConfig.load(path).consolidation.relation_curator_enabled is False

    changed = c.patch(
        "/api/v1/config/defaults",
        json={"embedding": {"model": "application-model-v2"}},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["applied"] == "reembed"
    assert changed.json()["affected_vaults"] == [str(path.resolve(strict=False))]
    assert VaultConfig.load(path).embedding.model == "application-model-v2"


def test_config_defaults_does_not_claim_a_concrete_model(no_vault_client) -> None:
    """0.0.48 companion item: on a fresh HOME (no ~/.marginalia/defaults.yaml)
    the defaults endpoint is discovery-first — it returns an empty model and
    never claims a concrete model name the endpoint may not serve (the stale
    hardcoded default is removed, not just renamed)."""
    c, _ = no_vault_client

    defaults = c.get("/api/v1/config/defaults")

    assert defaults.status_code == 200, defaults.text
    llm_defaults = defaults.json()["llm"]["defaults"]
    assert llm_defaults["model"] == ""
    assert "Qwen3.6-35B-A3B-oQ4-fp16-mtp" not in defaults.text


def test_named_credentials_and_provider_profiles_are_global_and_reference_safe(
    no_vault_client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, _ = no_vault_client
    home = tmp_path / "provider-home"
    env_path = home / ".okto-neuron" / "env"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OKTO_NEURON_ENV_FILE", str(env_path))
    first_secret = "first-gemini-secret"
    second_secret = "rotated-gemini-secret"

    created_credential = c.post(
        "/api/v1/credentials",
        json={"name": "Gemini pessoal", "api_key": first_secret},
    )
    assert created_credential.status_code == 201, created_credential.text
    credential = created_credential.json()["credential"]
    assert credential["id"] == "gemini-pessoal"
    assert credential["configured"] is True
    assert first_secret not in created_credential.text

    created_provider = c.post(
        "/api/v1/providers",
        json={
            "name": "Gemini via LiteLLM",
            "driver": "litellm_proxy",
            "api_base": "http://192.0.2.10:4000",
            "credential_id": credential["id"],
            "parameter_mode": "safe",
            "request_timeout_s": 900,
        },
    )
    assert created_provider.status_code == 201, created_provider.text
    provider = created_provider.json()["provider"]
    assert provider["uses"] == ["llm", "embedding"]
    assert provider["allow_remote"] is True
    assert provider["credential_name"] == "Gemini pessoal"
    assert provider["request_timeout_s"] == 900
    assert first_secret not in created_provider.text

    unbounded = c.patch(
        f"/api/v1/providers/{provider['id']}",
        json={"request_timeout_s": None},
    )
    assert unbounded.status_code == 200, unbounded.text
    assert unbounded.json()["provider"]["request_timeout_s"] is None

    invalid_timeout = c.patch(
        f"/api/v1/providers/{provider['id']}",
        json={"request_timeout_s": 0},
    )
    assert invalid_timeout.status_code == 400, invalid_timeout.text
    assert "positive number or null" in invalid_timeout.json()["detail"]

    attached = c.patch(
        "/api/v1/config/defaults",
        json={
            "llm": {
                "defaults": {
                    "provider_ref": provider["id"],
                    "model": "gemini-3.1-flash-lite",
                }
            }
        },
    )
    assert attached.status_code == 200, attached.text
    defaults = attached.json()["config"]["llm"]["defaults"]
    assert defaults["provider_ref"] == provider["id"]
    assert defaults["provider"] == "litellm_proxy"

    json_headers = {"Content-Type": "application/json"}
    blocked = c.delete(f"/api/v1/providers/{provider['id']}", headers=json_headers)
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "provider_in_use"

    rotated = c.put(
        f"/api/v1/credentials/{credential['id']}",
        json={"api_key": second_secret},
    )
    assert rotated.status_code == 200, rotated.text
    assert second_secret not in rotated.text
    assert first_secret not in env_path.read_text(encoding="utf-8")

    detached = c.patch(
        "/api/v1/config/defaults",
        json={"llm": {"defaults": {"provider_ref": None}}},
    )
    assert detached.status_code == 200, detached.text
    assert c.delete(f"/api/v1/providers/{provider['id']}", headers=json_headers).status_code == 200
    assert (
        c.delete(f"/api/v1/credentials/{credential['id']}", headers=json_headers).status_code == 200
    )
    assert second_secret not in env_path.read_text(encoding="utf-8")


def test_litellm_proxy_profile_rejects_local_extended_mode(
    no_vault_client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, _ = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "provider-home"))

    response = c.post(
        "/api/v1/providers",
        json={
            "name": "Unsafe proxy",
            "driver": "litellm_proxy",
            "api_base": "http://127.0.0.1:4000/v1",
            "parameter_mode": "local_extended",
        },
    )

    assert response.status_code == 400
    assert "direct local inference" in response.json()["detail"]


def test_embedding_provider_connection_cannot_change_under_a_live_reference(
    no_vault_client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron.config import VaultConfig

    c, _ = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "provider-home"))
    created = c.post(
        "/api/v1/providers",
        json={
            "name": "Local OpenAI embeddings",
            "driver": "openai",
            "api_base": "http://127.0.0.1:8123/v1",
            "parameter_mode": "safe",
        },
    )
    assert created.status_code == 201, created.text
    provider_id = created.json()["provider"]["id"]
    attached = c.patch(
        "/api/v1/config/defaults",
        json={"embedding": {"provider_ref": provider_id, "model": "embed-v1"}},
    )
    assert attached.status_code == 200, attached.text
    assert "embedding.provider_ref" in VaultConfig.REEMBED_FIELDS

    blocked = c.patch(
        f"/api/v1/providers/{provider_id}",
        json={"api_base": "http://127.0.0.1:9000/v1"},
    )
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["error"] == "embedding_provider_in_use"
    assert "Config > Embedding" in blocked.json()["detail"]


def test_embedding_model_discovery_filters_litellm_gateway_catalog(
    no_vault_client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron.providers import LiteLLMProxyModel

    c, _ = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "provider-home"))
    created = c.post(
        "/api/v1/providers",
        json={
            "name": "LiteLLM gateway",
            "driver": "litellm_proxy",
            "api_base": "http://127.0.0.1:4000",
            "parameter_mode": "safe",
        },
    )
    provider_id = created.json()["provider"]["id"]
    monkeypatch.setattr(
        "okto_neuron.providers.litellm_proxy_models",
        lambda **kwargs: (
            LiteLLMProxyModel("chat-alias", "chat", frozenset({"temperature"})),
            LiteLLMProxyModel("embed-z", "embedding", None),
            LiteLLMProxyModel("embed-a", "embedding", None),
        ),
    )

    response = c.post(
        "/api/v1/config/defaults/embedding/models",
        json={"provider_ref": provider_id, "provider": "ignored"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "known": True,
        "source": "litellm_gateway",
        "models": ["embed-a", "embed-z"],
        "error": None,
    }


def test_llm_probe_resolves_named_provider_reference(
    no_vault_client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c, _ = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "provider-home"))
    created = c.post(
        "/api/v1/providers",
        json={"name": "Test stub", "driver": "stub", "parameter_mode": "safe"},
    )
    provider_id = created.json()["provider"]["id"]

    response = c.post(
        "/api/v1/config/defaults/llm/test",
        json={"provider_ref": provider_id, "provider": "ignored"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "models": ["stub"], "error": None}


def test_concurrent_rest_vault_create_has_one_owner_and_stable_marker(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    from okto_neuron.vault_registry import read_managed_vault_marker

    c, _ = no_vault_client
    target = tmp_path / "home" / ".okto-neuron" / "vaults" / "shared"
    init_started = threading.Event()
    concurrent_precheck = threading.Event()
    init_calls: list[Path] = []
    original_is_vault = http_mod.is_vault

    def _tracked_is_vault(path) -> bool:
        exists = original_is_vault(path)
        if init_started.is_set() and not exists:
            concurrent_precheck.set()
        return exists

    def _init(path, *, packs, embedding_provider):
        assert packs is None
        assert embedding_provider is None
        init_calls.append(Path(path))
        init_started.set()
        assert concurrent_precheck.wait(timeout=5)
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
        return _StubVault()

    monkeypatch.setattr(http_mod, "is_vault", _tracked_is_vault)
    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _init)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(c.post, "/api/v1/vaults", json={"name": "shared"})
        assert init_started.wait(timeout=2)
        second = executor.submit(c.post, "/api/v1/vaults", json={"name": "shared"})
        responses = [first.result(timeout=10), second.result(timeout=10)]

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert init_calls == [target]
    success = next(response for response in responses if response.status_code == 200)
    conflict = next(response for response in responses if response.status_code == 409)
    marker = read_managed_vault_marker(target)
    assert marker is not None
    assert success.json()["created"]["id"] == marker.id
    assert conflict.json()["error"] == "vault_exists"


def test_vault_switch_replaces_active_vault(
    client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    c, state = client
    old_vault = state.vault
    target = tmp_path / "target"
    target.mkdir()
    (target / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    new_vault = _StubVault()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(http_mod.Vault, "open", staticmethod(lambda path: new_vault))

    r = c.post("/api/v1/vaults/switch", json={"vault": str(target)})

    assert r.status_code == 200, r.text
    # ADR 0014: switch no longer closes the old vault (close is path-wide; an MCP
    # connection may still hold it). It stays pooled until shutdown.
    assert old_vault.closed is False
    assert state.vault is new_vault
    assert state.vault_path == target.resolve(strict=False)
    assert r.json()["current"]["path"] == str(target.resolve(strict=False))
    # The old vault is still owned by the pool and closes at shutdown.
    state.close()
    assert old_vault.closed is True
    assert new_vault.closed is True


def test_compatibility_vault_switch_does_not_retarget_active_ingest(
    client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    c, state = client
    target = tmp_path / "target"
    target.mkdir()
    (target / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    target_vault = _StubVault()
    monkeypatch.setattr(http_mod.Vault, "open", staticmethod(lambda path: target_vault))
    state.ingest_queue.append(IngestItem(id="1", name="n.md", path="/tmp/n.md"))

    r = c.post("/api/v1/vaults/switch", json={"vault": str(target)})

    assert r.status_code == 200, r.text
    old_runtime = state.runtime_for(tmp_path)
    assert old_runtime.ingest_queue[0].path == "/tmp/n.md"
    assert state.vault is target_vault
    assert state.vault_path == target.resolve(strict=False)


def test_compatibility_vault_switch_leaves_curation_in_its_runtime(
    client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    from okto_neuron.server import _jobs

    c, state = client
    target = tmp_path / "target"
    target.mkdir()
    (target / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    target_vault = _StubVault()
    monkeypatch.setattr(http_mod.Vault, "open", staticmethod(lambda path: target_vault))

    job = _jobs.new_job("predicate-apply", label="canonicalize")
    job.status = "running"
    job.progress = "judging predicate pair 12/40"
    state.curation_jobs.append(job)

    r = c.post("/api/v1/vaults/switch", json={"vault": str(target)})

    assert r.status_code == 200, r.text
    old_runtime = state.runtime_for(tmp_path)
    assert old_runtime.curation_jobs == [job]
    assert old_runtime.curation_jobs[0].progress == "judging predicate pair 12/40"
    assert state.vault is target_vault
    assert state.vault_path == target.resolve(strict=False)


def test_request_vault_header_is_client_scoped_and_does_not_switch_fallback(
    client,
    tmp_path,
):
    c, state = client
    fallback_path = state.vault_path
    target = tmp_path / "home" / ".okto-neuron" / "vaults" / "second"
    target.mkdir(parents=True)
    (target / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    second = _StubVault()
    second.query = lambda text, k=5: [  # type: ignore[method-assign]
        SimpleNamespace(
            node=SimpleNamespace(id="doc_second", title="second vault"),
            score=0.9,
            claim_id=None,
            path="second.md",
            byte_start=0,
            byte_end=1,
            content_hash="b" * 64,
        )
    ]
    state.runtime_for(target, vault=second)

    selected = c.post(
        "/query",
        headers={"X-Okto-Neuron-Vault": str(target)},
        json={"query": "where"},
    )
    fallback = c.post("/query", json={"query": "where"})

    assert selected.status_code == 200, selected.text
    assert selected.json()["results"][0]["document_id"] == "doc_second"
    assert selected.headers["x-okto-neuron-vault"] == str(target.resolve(strict=False))
    assert fallback.json()["results"][0]["document_id"] == "doc_test_1"
    assert state.vault_path == fallback_path

    unknown = c.post(
        "/query",
        headers={"X-Okto-Neuron-Vault": str(target.parent / "missing")},
        json={"query": "where"},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"] == "vault_not_found"


def test_two_rest_clients_query_distinct_vaults_while_one_query_is_blocked(
    no_vault_client,
    tmp_path,
):
    from concurrent.futures import ThreadPoolExecutor
    import threading

    c_b, state = no_vault_client
    root = tmp_path / "home" / ".okto-neuron" / "vaults"
    path_a, path_b = root / "alpha", root / "beta"
    for path in (path_a, path_b):
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")

    started = threading.Event()
    release = threading.Event()
    vault_a = _StubVault()
    vault_b = _StubVault()

    def _slow_query(text, *, k=5):
        del text, k
        started.set()
        assert release.wait(timeout=5)
        return [_StubHit()]

    vault_a.query = _slow_query  # type: ignore[method-assign]
    vault_b.query = lambda text, k=5: [  # type: ignore[method-assign]
        SimpleNamespace(
            node=SimpleNamespace(id="doc_beta", title="beta"),
            score=0.8,
            claim_id="claim_beta",
            path="beta.md",
            byte_start=7,
            byte_end=19,
            content_hash="d" * 64,
        )
    ]
    runtime_a = state.runtime_for(path_a, vault=vault_a)
    runtime_b = state.runtime_for(path_b, vault=vault_b)

    app_a = build_rest_app(state)
    with TestClient(app_a, base_url="http://127.0.0.1") as c_a:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future_a = executor.submit(
                c_a.post,
                "/query",
                headers={"X-Okto-Neuron-Vault": str(path_a)},
                json={"query": "slow"},
            )
            assert started.wait(timeout=2)
            assert state.vault_pool.lease_count(path_a) == 1

            response_b = c_b.post(
                "/query",
                headers={"X-Okto-Neuron-Vault": str(path_b)},
                json={"query": "fast"},
            )
            assert response_b.status_code == 200, response_b.text
            assert future_a.done() is False
            release.set()
            response_a = future_a.result(timeout=5)

    assert response_a.status_code == 200, response_a.text
    assert response_a.headers["x-okto-neuron-vault"] == str(path_a.resolve(strict=False))
    assert response_b.headers["x-okto-neuron-vault"] == str(path_b.resolve(strict=False))
    beta_hit = response_b.json()["results"][0]
    assert beta_hit["document_id"] == "doc_beta"
    assert beta_hit["claim_id"] == "claim_beta"
    assert beta_hit["path"] == "beta.md"
    assert beta_hit["byte_start"] == 7
    assert beta_hit["byte_end"] == 19
    assert runtime_a.vault_path == path_a.resolve(strict=False)
    assert runtime_b.vault_path == path_b.resolve(strict=False)
    assert runtime_a.ingest_queue == []
    assert runtime_b.ingest_queue == []
    assert state.vault is None
    assert state.vault_path is None


def test_unscoped_content_request_uses_sole_vault_without_setting_fallback(
    no_vault_client,
    tmp_path,
):
    c, state = no_vault_client
    target = tmp_path / "home" / ".okto-neuron" / "vaults" / "only"
    target.mkdir(parents=True)
    (target / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    vault = _StubVault()
    state.runtime_for(target, vault=vault)

    response = c.post("/query", json={"query": "where"})

    assert response.status_code == 200, response.text
    assert response.headers["x-okto-neuron-vault"] == str(target.resolve(strict=False))
    assert state.vault is None
    assert state.vault_path is None


def test_unscoped_content_request_uses_configured_default_immutably(
    no_vault_client,
    tmp_path,
):
    from okto_neuron.vault_registry import set_default_vault

    c, state = no_vault_client
    root = tmp_path / "home" / ".okto-neuron" / "vaults"
    first = root / "first"
    selected = root / "selected"
    for path in (first, selected):
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    first_vault = _StubVault()
    selected_vault = _StubVault()
    selected_vault.query = lambda text, k=5: [  # type: ignore[method-assign]
        SimpleNamespace(
            node=SimpleNamespace(id="doc_default", title="default vault"),
            score=0.9,
            claim_id=None,
            path="default.md",
            byte_start=0,
            byte_end=1,
            content_hash="c" * 64,
        )
    ]
    state.runtime_for(first, vault=first_vault)
    state.runtime_for(selected, vault=selected_vault)
    set_default_vault(selected)

    response = c.post("/query", json={"query": "where"})

    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["document_id"] == "doc_default"
    assert state.vault is None
    assert state.vault_path is None


def test_unscoped_status_aggregates_runtimes_and_header_status_is_exact(
    no_vault_client,
    tmp_path,
):
    from okto_neuron.server import _jobs

    c, state = no_vault_client
    root = tmp_path / "home" / ".okto-neuron" / "vaults"
    paths = [root / "alpha", root / "beta"]
    runtimes = []
    for path in paths:
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
        runtimes.append(state.runtime_for(path, vault=_StubVault()))
    runtimes[0].ingest_queue.append(
        IngestItem(id="a", name="a.md", path="/tmp/a.md", status="queued")
    )
    job = _jobs.new_job("reconcile-propose")
    job.status = "running"
    runtimes[1].curation_jobs.append(job)

    application = c.get("/api/v1/status")
    selected = c.get(
        "/api/v1/status",
        headers={"X-Okto-Neuron-Vault": str(paths[1])},
    )

    assert application.status_code == 200, application.text
    app_body = application.json()
    assert app_body["scope"] == "application"
    assert app_body["vault_count"] == 2
    assert app_body["ingest"]["queued"] == 1
    by_path = {item["path"]: item for item in app_body["vaults"]}
    assert by_path[str(paths[1].resolve(strict=False))]["curation"]["running"] == 1
    # Both fixture vaults have no `storage` key (legacy absent-key rule).
    assert by_path[str(paths[1].resolve(strict=False))]["backend"] == "ladybug"
    assert app_body["backend"] is None  # application scope has no single vault_path

    assert selected.status_code == 200, selected.text
    selected_body = selected.json()
    assert selected_body["scope"] == "vault"
    assert selected_body["vault_path"] == str(paths[1].resolve(strict=False))
    assert selected_body["backend"] == "ladybug"
    assert selected_body["ingest"]["queued"] == 0
    assert len(selected_body["vaults"]) == 1


def test_reconcile_proposal_separates_candidates_from_adjudicated_members():
    from okto_neuron.server._curation import cluster_verdict_row

    cluster = SimpleNamespace(
        cluster_id="bilbo-family",
        type="Agent",
        member_ids=("bilbo", "bilbo-baggins", "bungo"),
        lane_evidence={"embedding": [], "lexical": []},
    )
    verdict = SimpleNamespace(
        same=True,
        confidence=0.99,
        canonical_id="bilbo-baggins",
        member_ids=("bilbo", "bilbo-baggins"),
        corroborated_ids=("bilbo", "bilbo-baggins"),
        corroboration="lexical",
        reason="Bilbo and Bilbo Baggins match; Bungo is Bilbo's father.",
    )

    row = cluster_verdict_row(cluster, verdict)

    assert row["candidate_member_ids"] == ["bilbo", "bilbo-baggins", "bungo"]
    assert row["member_ids"] == ["bilbo", "bilbo-baggins"]
    assert row["corroborated_ids"] == ["bilbo", "bilbo-baggins"]
    assert "bungo" not in row["member_ids"]


def test_semantic_quality_fails_fast_when_stable_snapshot_is_busy(
    client,
    monkeypatch: pytest.MonkeyPatch,
):
    c, _state = client

    class BusyWriterLock:
        async def __aenter__(self):
            raise http_mod._LockBusy

        async def __aexit__(self, _exc_type, _exc, _tb):
            return False

    monkeypatch.setattr(
        http_mod,
        "_writer_lock_fast",
        lambda *_args, **_kwargs: BusyWriterLock(),
    )

    response = c.post("/api/v1/quality/semantic", json={})

    assert response.status_code == 409
    assert response.json()["error"] == "audit_busy"
    assert response.json()["retryable"] is True


def test_reset_fences_only_target_and_preserves_other_vault_lease(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    c, state = no_vault_client
    root = tmp_path / "home" / ".okto-neuron" / "vaults"
    path_a, path_b = root / "alpha", root / "beta"
    for path in (path_a, path_b):
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    vault_a, vault_b = _StubVault(), _StubVault()
    runtime_a = state.runtime_for(path_a, vault=vault_a)
    runtime_b = state.runtime_for(path_b, vault=vault_b)
    replacement_a = _StubVault()
    wiped: list[Path] = []
    monkeypatch.setattr(
        http_mod,
        "wipe_vault",
        lambda path, keep_config: wiped.append(Path(path)),
    )
    monkeypatch.setattr(
        http_mod.Vault,
        "open",
        staticmethod(lambda path: replacement_a),
    )

    lease_b = runtime_b.lease_vault()
    try:
        response = c.post(
            "/api/v1/reset",
            headers={"X-Okto-Neuron-Vault": str(path_a)},
            json={},
        )

        assert response.status_code == 200, response.text
        assert wiped == [path_a.resolve(strict=False)]
        assert state.vault_pool.peek(path_a) is replacement_a
        assert state.vault_pool.peek(path_b) is vault_b
        assert state.vault_pool.lease_count(path_b) == 1
        assert lease_b.vault.query("still live")[0].node.id == "doc_test_1"
        assert vault_b.closed is False
        assert runtime_a.draining is False
    finally:
        lease_b.release()


def test_reset_refuses_same_vault_concurrent_lease_without_closing_handle(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    c, state = no_vault_client
    path = tmp_path / "home" / ".okto-neuron" / "vaults" / "alpha"
    path.mkdir(parents=True)
    (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    vault = _StubVault()
    runtime = state.runtime_for(path, vault=vault)
    lease = runtime.lease_vault()
    monkeypatch.setattr(http_mod, "_VAULT_DELETE_LEASE_WAIT_S", 0.0)
    try:
        response = c.post(
            "/api/v1/reset",
            headers={"X-Okto-Neuron-Vault": str(path)},
            json={},
        )

        assert response.status_code == 409
        assert response.json()["error"] == "vault_in_use"
        assert vault.closed is False
        assert state.vault_pool.peek(path) is vault
        assert runtime.draining is False
        assert state.vault_pool.is_fenced(path) is False
    finally:
        lease.release()


def test_vault_create_while_curating_does_not_switch(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Creation is independent of work in every existing vault runtime."""
    c, state = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    # An active vault with a running curation job (the global-busy condition).
    active = tmp_path / "home" / ".okto-neuron" / "vaults" / "active"
    active.mkdir(parents=True)
    (active / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    state.switch_vault(_StubVault(), active)

    from okto_neuron.server import _jobs

    running = _jobs.new_job("reconcile-apply", label="apply")
    running.status = "running"
    running.progress = "applying cluster 3/9"
    state.curation_jobs.append(running)

    new_vault = _StubVault()

    def _init(path, *, packs, embedding_provider):
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
        return new_vault

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _init)

    r = c.post("/api/v1/vaults", json={"name": "beta", "embedder": "stub"})

    assert r.status_code == 200, r.text
    body = r.json()
    # current stays on the active vault; the new vault was NOT switched to.
    assert state.vault_path == active.resolve(strict=False)
    assert body["current"]["path"] == str(active.resolve(strict=False))
    # The new vault is initialized on disk and appears in the list.
    new_path = tmp_path / "home" / ".okto-neuron" / "vaults" / "beta"
    listed = {v["path"] for v in body["vaults"]}
    assert str(new_path.resolve(strict=False)) in listed
    assert body["created"]["name"] == "beta"
    assert "switch_deferred" not in body
    # The worker closes its init handle; the immutable runtime opens lazily on
    # first use without becoming the process fallback or disrupting the worker.
    assert new_vault.closed is True
    assert state.runtime_for(new_path).vault_path == new_path.resolve(strict=False)
    assert state.vault_pool.peek(new_path) is None
    # The running curation job on the active vault is untouched.
    assert running.status == "running"


def test_managed_vault_delete_requires_exact_name_and_preserves_siblings(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    c, state = no_vault_client
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    created_vault = _StubVault()

    def _init(path, *, packs, embedding_provider):
        del packs, embedding_provider
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
        return created_vault

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _init)
    created = c.post("/api/v1/vaults", json={"name": "delete-me", "embedder": "stub"})
    assert created.status_code == 200, created.text
    entry = created.json()["created"]
    vault_path = Path(entry["path"])
    sibling = vault_path.parent / "keep-me"
    sibling.mkdir()
    sentinel = sibling / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")

    mismatch = c.request(
        "DELETE",
        f"/api/v1/vaults/{entry['id']}",
        json={"confirm_name": "wrong-name"},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"] == "confirmation_mismatch"
    assert vault_path.exists()

    deleted = c.request(
        "DELETE",
        f"/api/v1/vaults/{entry['id']}",
        json={"confirm_name": "delete-me"},
    )

    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["deleted"] == {
        "id": entry["id"],
        "name": "delete-me",
        "path": str(vault_path),
    }
    assert not vault_path.exists()
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert created_vault.closed is True
    assert all(runtime.vault_path != vault_path for runtime in state.runtimes())


def test_legacy_configured_root_vault_is_deletable(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    c, _ = no_vault_client
    created_vault = _StubVault()

    def _init(path, *, packs, embedding_provider):
        del packs, embedding_provider
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
        return created_vault

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _init)
    created = c.post("/api/v1/vaults", json={"name": "legacy", "embedder": "stub"})
    assert created.status_code == 200, created.text
    vault_path = Path(created.json()["created"]["path"])
    (vault_path / ".marginalia" / "managed-vault.json").unlink()

    entry = next(
        vault for vault in c.get("/api/v1/vaults").json()["vaults"] if vault["name"] == "legacy"
    )
    assert entry["managed"] is True
    assert entry["deletable"] is True

    response = c.request(
        "DELETE",
        f"/api/v1/vaults/{entry['id']}",
        json={"confirm_name": "legacy"},
    )

    assert response.status_code == 200, response.text
    assert not vault_path.exists()


def test_vault_outside_configured_roots_is_listed_but_never_deletable(
    no_vault_client,
    tmp_path,
):
    c, _ = no_vault_client
    home = tmp_path / "home"
    root = home / ".okto-neuron" / "vaults"
    root.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (external / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    config_path = home / ".okto-neuron" / "okto-neuron.toml"
    config_path.write_text(
        f'marginalia_toml_version = 1\nvault_roots = ["{root}"]\ndefault_vault = "{external}"\n',
        encoding="utf-8",
    )

    entry = next(
        vault for vault in c.get("/api/v1/vaults").json()["vaults"] if vault["name"] == "external"
    )
    assert entry["managed"] is False
    assert entry["deletable"] is False

    response = c.request(
        "DELETE",
        f"/api/v1/vaults/{entry['id']}",
        json={"confirm_name": "external"},
    )

    assert response.status_code == 409
    assert response.json()["error"] == "vault_protected"
    assert external.exists()


def test_managed_vault_delete_rejects_work_and_active_leases(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    c, state = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    created_vault = _StubVault()

    def _init(path, *, packs, embedding_provider):
        del packs, embedding_provider
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
        return created_vault

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _init)
    entry = c.post("/api/v1/vaults", json={"name": "busy", "embedder": "stub"}).json()["created"]
    runtime = state.runtime_for(Path(entry["path"]))
    runtime.ingest_queue.append(IngestItem(id="1", name="n.md", path="/tmp/n.md"))

    busy = c.request(
        "DELETE",
        f"/api/v1/vaults/{entry['id']}",
        json={"confirm_name": "busy"},
    )
    assert busy.status_code == 409
    assert busy.json()["error"] == "vault_busy"
    runtime.ingest_queue.clear()

    monkeypatch.setattr(http_mod.Vault, "open", staticmethod(lambda path: _StubVault()))
    lease = runtime.lease_vault()
    monkeypatch.setattr(http_mod, "_VAULT_DELETE_LEASE_WAIT_S", 0.0)
    leased = c.request(
        "DELETE",
        f"/api/v1/vaults/{entry['id']}",
        json={"confirm_name": "busy"},
    )
    assert leased.status_code == 409
    assert leased.json()["error"] == "vault_busy"
    assert runtime.draining is False
    assert state.vault_pool.is_fenced(runtime.vault_path) is False
    lease.release()


def test_failed_nonactive_delete_restores_default_and_pool_handle(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    from okto_neuron.vault_registry import resolve_vault_reference, set_default_vault

    c, state = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    created_vault = _StubVault()

    def _init(path, *, packs, embedding_provider):
        del packs, embedding_provider
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
        return created_vault

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _init)
    entry = c.post("/api/v1/vaults", json={"name": "restore-me", "embedder": "stub"}).json()[
        "created"
    ]
    path = Path(entry["path"])
    set_default_vault(path)

    restored = _StubVault()
    monkeypatch.setattr(http_mod.Vault, "open", staticmethod(lambda target: restored))
    from okto_neuron.store.writer_lease import acquire_writer_lease, held_writer_lease

    acquire_writer_lease(path, role="daemon", operation="serve")
    lease_held_at_rmtree: list[bool] = []

    def _failing_rmtree(target):
        lease_held_at_rmtree.append(held_writer_lease(path) is not None)
        raise OSError("simulated rmtree failure")

    monkeypatch.setattr(http_mod.shutil, "rmtree", _failing_rmtree)

    response = c.request(
        "DELETE",
        f"/api/v1/vaults/{entry['id']}",
        json={"confirm_name": "restore-me"},
    )

    assert response.status_code == 500
    assert path.exists()
    # The writer lease is released before the rmtree and re-acquired on rollback.
    assert lease_held_at_rmtree == [False]
    assert held_writer_lease(path) is not None
    assert resolve_vault_reference(None) == path.resolve(strict=False)
    runtime = state.runtime_for(path)
    assert state.vault_pool.peek(path) is restored
    assert runtime.draining is False
    assert state.vault_pool.is_fenced(path) is False
    with runtime.lease_vault() as usable:
        assert usable.query("still usable")[0].node.id == "doc_test_1"


def test_partial_delete_failure_quarantines_path_without_restoring_default(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    from okto_neuron.vault_registry import (
        ensure_global_layout,
        list_vaults,
        mark_managed_vault,
        set_default_vault,
    )
    from okto_neuron.config import OktoNeuronConfig

    c, state = no_vault_client
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    ensure_global_layout()
    path = home / ".okto-neuron" / "vaults" / "partial-delete"
    path.mkdir()
    (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    partially_removed = path / "graph-part"
    partially_removed.write_text("must survive or fail closed", encoding="utf-8")
    marker = mark_managed_vault(path)
    set_default_vault(path)
    original_handle = _StubVault()
    state.switch_vault(original_handle, path)
    runtime = state.active_runtime
    assert runtime is not None
    reopen_calls: list[Path] = []

    def _partial_rmtree(target: Path) -> None:
        assert Path(target) == path
        partially_removed.unlink()
        raise OSError("simulated partial rmtree failure")

    def _failed_reopen(target: Path) -> _StubVault:
        reopen_calls.append(Path(target))
        raise OSError("simulated partial vault open failure")

    monkeypatch.setattr(http_mod.shutil, "rmtree", _partial_rmtree)
    monkeypatch.setattr(http_mod.Vault, "open", staticmethod(_failed_reopen))

    response = c.request(
        "DELETE",
        f"/api/v1/vaults/{marker.id}",
        json={"confirm_name": "partial-delete"},
    )

    assert response.status_code == 500
    assert path.exists()
    assert (path / "okto-neuron.yaml").is_file()
    assert partially_removed.exists() is False
    assert OktoNeuronConfig.load().default_vault is None
    assert any(entry.path == path.resolve(strict=False) for entry in list_vaults())
    assert state.vault is None
    assert state.vault_path is None
    assert state.active_runtime is None
    assert all(item.vault_path != path for item in state.runtimes())
    assert state.vault_pool.peek(path) is None
    assert state.vault_pool.is_fenced(path) is True
    assert runtime.draining is True
    assert original_handle.closed is True
    assert reopen_calls == [path]

    selected = c.post(
        "/query",
        headers={"X-Okto-Neuron-Vault": str(path)},
        json={"query": "must fail closed"},
    )
    assert selected.status_code == 409
    assert selected.json()["error"] == "vault_fenced"
    assert all(item.vault_path != path for item in state.runtimes(discover=True))
    assert state.vault_pool.peek(path) is None
    assert state.vault_pool.is_fenced(path) is True


def test_managed_vault_delete_cancellation_waits_for_filesystem_outcome(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    from okto_neuron.vault_registry import (
        ensure_global_layout,
        mark_managed_vault,
        resolve_vault_reference,
        set_default_vault,
    )

    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    reset_state_for_tests()
    ensure_global_layout()
    path = home / ".okto-neuron" / "vaults" / "cancel-delete"
    path.mkdir()
    (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    marker = mark_managed_vault(path)
    set_default_vault(path)

    original_handle = _StubVault()
    state = init_state(original_handle, path)
    runtime = state.runtime_for(path)
    original_rmtree = http_mod.shutil.rmtree
    removal_started = threading.Event()
    allow_removal = threading.Event()
    competing_attempted = threading.Event()
    competing_entered = threading.Event()
    reopen_calls: list[Path] = []

    def _blocked_rmtree(target: Path) -> None:
        assert Path(target) == path
        # Model a real mid-rmtree cancellation window: the directory still
        # exists, but it is no longer a valid vault and must never be restored.
        (path / "okto-neuron.yaml").unlink()
        removal_started.set()
        if not allow_removal.wait(timeout=5):
            raise TimeoutError("test did not release managed vault removal")
        original_rmtree(target)

    def _unexpected_reopen(target: Path) -> _StubVault:
        reopen_calls.append(Path(target))
        return _StubVault()

    monkeypatch.setattr(http_mod.shutil, "rmtree", _blocked_rmtree)
    monkeypatch.setattr(http_mod.Vault, "open", staticmethod(_unexpected_reopen))

    body = json.dumps({"confirm_name": "cancel-delete"}).encode("utf-8")
    body_sent = False

    async def _receive() -> dict[str, object]:
        nonlocal body_sent
        if body_sent:
            return {"type": "http.disconnect"}
        body_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "DELETE",
            "scheme": "http",
            "path": f"/api/v1/vaults/{marker.id}",
            "raw_path": f"/api/v1/vaults/{marker.id}".encode("ascii"),
            "query_string": b"",
            "headers": [
                (b"host", b"127.0.0.1"),
                (b"content-type", b"application/json"),
            ],
            "client": ("127.0.0.1", 43123),
            "server": ("127.0.0.1", 8765),
            "path_params": {"vault_id": marker.id},
        },
        _receive,
    )

    def _competing_mutation() -> None:
        competing_attempted.set()
        state.run_application_mutation(competing_entered.set)

    async def _exercise() -> None:
        delete_task = asyncio.create_task(http_mod.api_vault_delete(request))
        competing_task: asyncio.Task[None] | None = None
        try:
            assert await asyncio.to_thread(removal_started.wait, 2)
            assert path.exists()
            assert (path / "okto-neuron.yaml").exists() is False
            assert resolve_vault_reference(None) != path.resolve(strict=False)
            assert state.vault_pool.is_fenced(path) is True
            assert runtime.draining is True
            assert state.vault_pool.peek(path) is None

            delete_task.cancel()
            await asyncio.sleep(0)
            delete_task.cancel()
            await asyncio.sleep(0)
            assert delete_task.done() is False

            competing_task = asyncio.create_task(asyncio.to_thread(_competing_mutation))
            assert await asyncio.to_thread(competing_attempted.wait, 2)
            assert await asyncio.to_thread(competing_entered.wait, 0.1) is False
            assert path.exists()
            assert (path / "okto-neuron.yaml").exists() is False
            assert resolve_vault_reference(None) != path.resolve(strict=False)
            assert state.vault_pool.is_fenced(path) is True
            assert runtime.draining is True
            assert reopen_calls == []

            allow_removal.set()
            with pytest.raises(asyncio.CancelledError):
                await delete_task
            await asyncio.wait_for(competing_task, timeout=2)
        finally:
            allow_removal.set()
            if not delete_task.done():
                with pytest.raises(asyncio.CancelledError):
                    await delete_task
            if competing_task is not None and not competing_task.done():
                await asyncio.wait_for(competing_task, timeout=2)

    try:
        asyncio.run(_exercise())

        assert path.exists() is False
        assert resolve_vault_reference(None) != path.resolve(strict=False)
        assert state.vault is None
        assert state.vault_path is None
        assert state.active_runtime is None
        assert all(item.vault_path != path for item in state.runtimes())
        assert state.vault_pool.peek(path) is None
        assert state.vault_pool.is_fenced(path) is False
        assert runtime.draining is False
        assert original_handle.closed is True
        assert reopen_calls == []
        assert competing_entered.is_set() is True
    finally:
        allow_removal.set()
        reset_state_for_tests()


def test_vault_create_concurrent_reads_stay_ok_while_curating(
    no_vault_client,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Creating a vault mid-curation must not 503/409 reads — /health and /api/v1/vaults
    keep returning 200."""
    c, state = no_vault_client
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    active = tmp_path / "home" / ".okto-neuron" / "vaults" / "active"
    active.mkdir(parents=True)
    (active / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
    state.switch_vault(_StubVault(), active)

    from okto_neuron.server import _jobs

    running = _jobs.new_job("reconcile-apply", label="apply")
    running.status = "running"
    state.curation_jobs.append(running)

    def _init(path, *, packs, embedding_provider):
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
        return _StubVault()

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _init)

    assert c.post("/api/v1/vaults", json={"name": "gamma", "embedder": "stub"}).status_code == 200
    assert c.get("/health").status_code == 200
    assert c.get("/api/v1/vaults").status_code == 200


def test_ingest_queue_item_returns_rich_events(client):
    c, state = client
    item = IngestItem(id="1", name="n.md", path="/tmp/n.md")
    item.events.append(
        {
            "ts": 1.0,
            "kind": "llm_request",
            "summary": "Extraction LLM request",
            "payload": {"model": "stub"},
        }
    )
    state.ingest_queue.append(item)

    r = c.get("/api/v1/ingest-queue/1")

    assert r.status_code == 200
    body = r.json()
    assert body["item"]["event_count"] == 1
    assert body["item"]["events"][0]["kind"] == "llm_request"


def test_ledger_runs_and_detail_return_sanitized_records(client):
    c, state = client
    ledger = CandidateLedger(Path(state.vault_path) / ".marginalia")
    run_id = ledger.start_run(
        document_id="doc-1",
        source="/tmp/note.md",
        blocks_total=1,
        model="stub",
    )
    ledger.record_candidate(
        run_id,
        candidate_id="cand-1",
        candidate_kind="node",
        state="proposed",
        payload={
            "type": "Concept",
            "title": "Ledger",
            "content": "demo",
            "embedding": [0.1, 0.2, 0.3],
            "facets": {"source_path": "/tmp/note.md", "block_id": "block-1"},
        },
    )
    ledger.record_commit_plan(
        run_id,
        operations=[
            {
                "operation": "create_node",
                "candidate_kind": "node",
                "candidate_id": "cand-1",
                "candidate": {
                    "type": "Concept",
                    "title": "Ledger",
                    "content": "demo",
                },
                "confidence": 0.9,
                "correlations": [],
                "reason": None,
            }
        ],
    )
    ledger.finish_run(run_id, state="completed", summary={"committed": 1})

    runs = c.get("/api/v1/ledger/runs")
    assert runs.status_code == 200
    body = runs.json()
    assert body["runs"][0]["run_id"] == run_id
    assert body["runs"][0]["name"] == "note.md"
    assert body["runs"][0]["summary"]["committed"] == 1

    detail = c.get(f"/api/v1/ledger/runs/{run_id}")
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["candidates"][0]["title"] == "Ledger"
    assert payload["candidates"][0]["block_id"] == "block-1"
    candidate_record = next(r for r in payload["records"] if r["kind"] == "candidate")
    assert "embedding" not in candidate_record["payload"]
    assert candidate_record["payload"]["embedding_dim"] == 3


def test_ledger_summary_reports_pending_commit_preview(client):
    c, state = client
    ledger = CandidateLedger(Path(state.vault_path) / ".marginalia")
    run_id = ledger.start_run(
        document_id="doc-1",
        source="/tmp/fellowship.md",
        blocks_total=91,
        model="stub",
    )
    ledger.record_candidate(
        run_id,
        candidate_id="node-1",
        candidate_kind="node",
        state="proposed",
        payload={"type": "Agent", "title": "Frodo", "content": "hobbit"},
    )
    ledger.record_candidate(
        run_id,
        candidate_id="node-2",
        candidate_kind="node",
        state="superseded",
        payload={"type": "Agent", "title": "Frodo", "content": "duplicate"},
    )
    ledger.record_candidate(
        run_id,
        candidate_id="edge-1",
        candidate_kind="edge",
        state="proposed",
        payload={"type": "located_in", "src_ref": "node-1", "dst_ref": "place-1"},
    )
    ledger.record_candidate(
        run_id,
        candidate_id="edge-2",
        candidate_kind="edge",
        state="proposed",
        payload={"type": "said", "src_ref": "node-1", "dst_literal": "hello"},
    )
    ledger.record_comparison(
        run_id,
        candidate_id="node-1",
        method="curator",
        verdict="commit",
        score=0.95,
        payload={"candidate": {"type": "Agent", "title": "Frodo"}},
    )
    ledger.record_comparison(
        run_id,
        candidate_id="node-2",
        method="curator",
        verdict="commit",
        score=0.1,
        payload={"audit_only": True, "llm_skipped": True},
    )
    ledger.record_comparison(
        run_id,
        candidate_id="edge-1",
        method="relation_curator",
        verdict="commit",
        score=0.9,
        payload={
            "type": "located_in",
            "src_ref": "node-1",
            "dst_ref": "place-1",
            "canonical_predicate": "located_in",
            "proposed_terminal_action": "create_edge_or_claim",
        },
    )
    ledger.record_comparison(
        run_id,
        candidate_id="edge-2",
        method="relation_curator",
        verdict="queue",
        score=0.2,
        payload={
            "type": "said",
            "src_ref": "node-1",
            "dst_literal": "hello",
            "proposed_terminal_action": "create_edge_or_claim",
        },
    )

    r = c.get("/api/v1/ledger/summary")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"]["run_id"] == run_id
    assert body["candidate_kinds"] == {"edge": 2, "node": 2}
    assert body["progress"]["node_curator"]["done"] == 2
    # ADR 0039 T9: every phase declares the population its denominator represents.
    assert body["progress"]["relation_curator"] == {
        "done": 2,
        "total": 2,
        "remaining": 0,
        "fraction": 1.0,
        "population": "edge_candidates",
    }
    assert body["audit_modes"] == {"llm_skipped": 1}
    assert body["pending_commit_preview"]["nodes"]["accepted_for_write"] == 1
    assert body["pending_commit_preview"]["nodes"]["types_by_verdict"]["commit"] == {"Agent": 1}
    assert body["pending_commit_preview"]["relations"]["accepted_for_write"] == 1
    assert body["pending_commit_preview"]["relations"]["queued_or_abstained"] == 1
    assert body["pending_commit_preview"]["relations"]["accepted_predicates"] == {"located_in": 1}
    assert body["pending_commit_preview"]["nodes"]["sample_candidates_by_verdict"]["commit"] == [
        {"candidate_id": "node-1", "type": "Agent", "title": "Frodo"}
    ]
    relation_sample = body["pending_commit_preview"]["relations"]["sample_relations_by_verdict"][
        "commit"
    ][0]
    assert relation_sample["predicate"] == "located_in"
    assert relation_sample["subject"] == {
        "ref": "node-1",
        "type": "Agent",
        "title": "Frodo",
    }
    assert relation_sample["object"] == {
        "ref": "place-1",
        "type": "",
        "title": "place-1",
    }


def test_ledger_summary_progress_uses_active_post_dedup_totals(client):
    c, state = client
    ledger = CandidateLedger(Path(state.vault_path) / ".marginalia")
    run_id = ledger.start_run(
        document_id="doc-1",
        source="/tmp/fellowship.md",
        blocks_total=91,
        model="stub",
    )
    for index in range(3):
        ledger.record_candidate(
            run_id,
            candidate_id=f"node-{index}",
            candidate_kind="node",
            state="proposed",
            payload={"type": "Agent", "title": f"Candidate {index}"},
        )
    ledger.record_candidate(
        run_id,
        candidate_id="edge-1",
        candidate_kind="edge",
        state="proposed",
        payload={"type": "located_in", "src_ref": "node-1", "dst_ref": "place-1"},
    )
    ledger.record_comparison(
        run_id,
        candidate_id="",
        method="exact_batch",
        verdict="dedup_pass",
        score=1.0,
        payload={"before": {"nodes": 3, "edges": 1}, "after": {"nodes": 2, "edges": 1}},
    )
    ledger.record_comparison(
        run_id,
        candidate_id="node-0",
        method="curator",
        verdict="commit",
        score=0.95,
        payload={"candidate": {"type": "Agent", "title": "Candidate 0"}},
    )
    ledger.record_comparison(
        run_id,
        candidate_id="node-1",
        method="curator",
        verdict="commit",
        score=0.0,
        payload={"audit_only": True, "llm_skipped": True},
    )

    r = c.get("/api/v1/ledger/summary")

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["candidate_kinds"] == {"edge": 1, "node": 3}
    assert body["active_candidate_kinds"] == {"edge": 1, "node": 2}
    assert body["comparison_methods"]["curator"] == 2
    # ADR 0039 T9: the post-dedup denominator move is published as an explicit
    # revision with its reason and source, not absorbed into the percentage.
    assert body["progress"]["node_curator"] == {
        "done": 1,
        "total": 2,
        "remaining": 1,
        "fraction": 0.5,
        "population": "node_candidates",
        "population_revision": {
            "previous_total": 3,
            "total": 2,
            "reason": "active_population_recomputed",
            "source": "exact_batch",
        },
    }


def test_ingest_cancel_marks_queued_items_cancelled(client):
    c, state = client
    runtime = state.active_runtime
    assert runtime is not None
    runtime.ingest_worker_active = True
    runtime.ingest_queue.extend(
        [
            IngestItem(id="p", name="p.md", path="/tmp/p.md", status="processing"),
            IngestItem(id="q", name="q.md", path="/tmp/q.md", status="queued"),
        ]
    )

    r = c.post("/api/v1/ingest-cancel", json={})

    assert r.status_code == 200
    assert runtime.ingest_cancel_requested is True
    assert runtime.ingest_queue[1].status == "cancelled"
    assert r.json()["summary"]["cancelled"] == 1
    assert r.json()["summary"]["cancel_requested"] is True

    event_count = len(runtime.ingest_queue[0].events)
    repeated = c.post("/api/v1/ingest-cancel", json={})
    assert repeated.status_code == 200
    assert repeated.json()["cancelled"] == 0
    assert len(runtime.ingest_queue[0].events) == event_count


def test_add_ok(client):
    c, state = client
    r = c.post("/add", json={"path": "note.md", "content": "hello"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["document_id"] == "doc_test_1"
    assert body["embedding_model"] == "bge-small-en-v1.5"


def test_add_same_basename_different_directories_does_not_collide(client, tmp_path):
    """Regression for review 3.4-e2e: ``okto-neuron add notes/a/README.md`` then
    ``okto-neuron add notes/b/README.md`` (distinct content, same basename) used
    to both write to ``.marginalia/sources/README.md`` — the second POST
    silently overwrote the first file's bytes on disk, and (in the real,
    non-stubbed ``Vault.add`` path) both documents then hashed the same
    resolved target path into an identical ``document_id``
    (``sha256_hex("document", str(p.resolve()))`` in
    ``ingest/markdown.py``), so the first document's provenance permanently
    pointed at the second document's content.

    ``_StubVault.add`` here always returns a fixed id, so this asserts the
    real underlying mechanism instead: the two POSTs must materialize to two
    distinct files under ``.marginalia/sources/`` that each still hold their
    own posted content, and ``Vault.add`` (recorded on the stub) must have
    been called with two distinct paths — collapsing to one path is exactly
    what used to mint one shared document id for two different documents.
    """
    c, state = client

    r_a = c.post("/add", json={"path": "notes/a/README.md", "content": "# A\ncontent A\n"})
    r_b = c.post("/add", json={"path": "notes/b/README.md", "content": "# B\ncontent B\n"})
    assert r_a.status_code == 200, r_a.text
    assert r_b.status_code == 200, r_b.text

    vault = state.vault
    assert len(vault.added) == 2
    target_a, target_b = vault.added
    assert target_a != target_b, (
        f"both /add calls materialized to the same file ({target_a}); "
        "same-basename files from different directories must not collide"
    )
    assert target_a.exists() and target_b.exists()
    assert target_a.read_text(encoding="utf-8") == "# A\ncontent A\n"
    assert target_b.read_text(encoding="utf-8") == "# B\ncontent B\n"


def test_add_missing_field_400(client):
    c, _ = client
    r = c.post("/add", json={"path": "x"})
    assert r.status_code == 400
    body = r.json()
    assert body["error"] == "bad_request"
    assert "content" in body["detail"]


def test_add_malformed_json_400(client):
    c, _ = client
    r = c.post("/add", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_add_503_when_draining(client):
    c, state = client
    runtime = state.active_runtime
    assert runtime is not None
    runtime.mark_draining()
    r = c.post("/add", json={"path": "n.md", "content": "x"})
    assert r.status_code == 503
    assert r.json()["error"] == "maintenance"
    assert "shutdown" not in r.json()["detail"]


def test_maintenance_drain_keeps_old_generation_reads_available(client):
    c, state = client
    runtime = state.active_runtime
    assert runtime is not None
    runtime.mark_draining()

    query_response = c.post("/query", json={"query": "what", "k": 3})
    queue_response = c.get("/api/v1/ingest-queue")
    config_response = c.get("/api/v1/config")

    assert query_response.status_code == 200
    assert queue_response.status_code == 200
    assert config_response.status_code == 200
    assert runtime.maintenance_draining is True
    assert runtime.draining is True

    blocker = http_mod._maintenance_blocker(runtime)
    assert blocker is not None
    assert blocker["kind"] == "maintenance"
    assert "shutdown" not in str(blocker["reason"])


def test_process_shutdown_still_stops_reads(client):
    c, state = client
    state.mark_shutting_down()

    response = c.post("/query", json={"query": "what", "k": 3})

    assert response.status_code == 503
    assert response.json()["error"] == "shutting_down"


def test_query_ok(client):
    c, _ = client
    r = c.post("/query", json={"query": "what", "k": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert len(body["results"]) == 1
    hit = body["results"][0]
    assert hit["document_id"] == "doc_test_1"
    assert hit["score"] == pytest.approx(0.42)
    assert body["recall_cost"]["schema_version"] == "recall_cost.v1"
    assert body["recall_cost"]["measurement_status"] == "not_measured"
    assert body["recall_cost"]["completion_free"] is None


def test_query_missing_field_400(client):
    c, _ = client
    r = c.post("/query", json={"k": 5})
    assert r.status_code == 400
    assert "query" in r.json()["detail"]


def test_query_invalid_k_400(client):
    c, _ = client
    r = c.post("/query", json={"query": "x", "k": 0})
    assert r.status_code == 400


@pytest.mark.parametrize("path", ["/detect-drift", "/api/v1/detect-drift"])
def test_detect_drift_bad_corpus_400(client, path):
    c, _ = client
    r = c.post(path, json={"corpus_root": "relative/path"})
    assert r.status_code == 400


def test_detect_drift_missing_corpus_404(client, tmp_path):
    c, _ = client
    missing = tmp_path / "nope"
    r = c.post("/detect-drift", json={"corpus_root": str(missing)})
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_detect_drift_does_not_hold_writer_lock(tmp_path):
    """Regression: every registered detector (detectors.py) only reads
    ``vault.store`` and returns Finding values — nothing there ever writes, so
    detect_drift must not depend on writer_lock at all (previously it did, so
    drift detection hung behind any in-flight ingest/rebuild/heal/reembed for no
    reason; state.py's own docstring says read handlers MUST NOT acquire it).

    Proven by holding writer_lock for the WHOLE handler call in the SAME task:
    if detect_drift still needed writer_lock this would deadlock (a lock is not
    reentrant), and the bounded ``asyncio.wait_for`` turns that hang into a clean
    failure instead of hanging the suite.
    """
    import asyncio

    class _Store:
        def list_nodes(self, type=None):  # noqa: A002 - matches GraphStore signature
            return []

    class _DriftVault:
        def __init__(self, root: Path) -> None:
            self.store = _Store()
            self.root = root

        def close(self) -> None:
            pass

    class _Req:
        class _C:
            host = "127.0.0.1"

        client = _C()
        headers: dict[str, str] = {}

        async def body(self) -> bytes:
            import json

            return json.dumps({"corpus_root": str(tmp_path)}).encode("utf-8")

    reset_state_for_tests()
    root = tmp_path / "v"
    root.mkdir()
    state = init_state(_DriftVault(root), root)

    async def _run():
        async with state.writer_lock:
            return await asyncio.wait_for(http_mod.detect_drift(_Req()), timeout=2.0)

    resp = asyncio.run(_run())
    assert resp.status_code == 200, resp.body
    reset_state_for_tests()


def test_state_singleton_enforced(tmp_path):
    reset_state_for_tests()
    init_state(_StubVault(), tmp_path)
    with pytest.raises(RuntimeError):
        init_state(_StubVault(), tmp_path)
    reset_state_for_tests()


def test_active_server_state_uses_runtime_writer_lock(tmp_path):
    reset_state_for_tests()
    state = init_state(_StubVault(), tmp_path)
    runtime = state.active_runtime
    assert runtime is not None
    assert state.writer_lock is state.writer_lock
    assert state.writer_lock is runtime.writer_lock
    assert state.writer_lock is not state_mod.get_vault_write_lock()
    reset_state_for_tests()


# ── Backend-neutral rollback gate (D-84) ────────────────────────────────
#
# ``api_curation_rollback`` now defers to
# ``marginalia.curation.orchestrate.rollback_candidate`` for every backend
# instead of only checking Ladybug's rebuild-artifact tuple. These tests
# drive it against a real grafx store and a real (live-container) neo4j
# store — a real backup on disk / a real backup_tag in the graph, not a
# stub — mirroring the existing Ladybug 202 coverage in
# ``tests/test_server_api_v1.py::test_rollback_api_requires_current_generation_evidence_and_pins_submission``.
# The job itself is monkeypatched away (same technique that test uses): this
# is a REST-gate test, not a full swap-execution test — those live in
# ``tests/server/test_curation_rebuild.py`` and
# ``tests/curation/test_rollback_candidate.py``.


def test_rollback_api_grafx_returns_202_when_a_backup_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("okto_grafx")
    from okto_neuron.cli.kg import kg_init
    from okto_neuron.server import _jobs
    from okto_neuron.store.grafx import _GRAPH_DIR_NAME
    from okto_neuron.vault import Vault

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    reset_state_for_tests()
    vault_path = tmp_path / "v"
    assert kg_init(vault_path, backend="grafx") == 0
    vault = Vault.open(vault_path)
    state = init_state(vault, vault_path)
    app = build_rest_app(state)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as c:
            unavailable = c.post("/api/v1/curation/rollback", json={})
            assert unavailable.status_code == 409
            assert unavailable.json()["error"] == "rollback_unavailable"

            graph_path = vault_path / _GRAPH_DIR_NAME
            bak_dir = graph_path.with_name(f"{graph_path.name}.bak")
            bak_dir.mkdir()
            (bak_dir / "marker").write_text("bak", encoding="utf-8")

            submitted: dict[str, object] = {}

            def fake_submit(_state, kind, *, label="", params=None):
                submitted.update({"kind": kind, "label": label, "params": params})
                return _jobs.CurationJob(
                    id="rollback-grafx-test", kind=kind, label=label, params=params or {}
                )

            monkeypatch.setattr(_jobs, "submit", fake_submit)
            accepted = c.post("/api/v1/curation/rollback", json={})
            assert accepted.status_code == 202, accepted.text
            assert submitted["kind"] == "rollback"
    finally:
        reset_state_for_tests()


def test_rollback_api_neo4j_returns_202_when_a_backup_tag_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os as _os

    pytest.importorskip("neo4j")
    uri = _os.environ.get("OKTO_NEURON_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("OKTO_NEURON_TEST_NEO4J_URI is not set")
    credential_env = _os.environ.get("OKTO_NEURON_TEST_NEO4J_CREDENTIAL_ENV")

    from okto_neuron.cli.kg import kg_init
    from okto_neuron.config import VaultConfig
    from okto_neuron.server import _jobs
    from okto_neuron.store import schema
    from okto_neuron.store.neo4j import Neo4jStore
    from okto_neuron.vault import Vault

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    reset_state_for_tests()
    vault_path = tmp_path / "v"
    assert (
        kg_init(
            vault_path,
            backend="neo4j",
            storage_uri=uri,
            storage_credential_env=credential_env,
            storage_database="neo4j",
        )
        == 0
    )
    vault = Vault.open(vault_path)
    state = init_state(vault, vault_path)
    app = build_rest_app(state)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as c:
            unavailable = c.post("/api/v1/curation/rollback", json={})
            assert unavailable.status_code == 409
            assert unavailable.json()["error"] == "rollback_unavailable"

            # Stash a real backup_tag on the metadata singleton, and tag one
            # real node at that generation -- the same evidence a real swap
            # (``Neo4jStaging.commit``) leaves behind, without running a
            # full rebuild through the daemon job queue.
            storage_config = VaultConfig.load(vault_path).storage
            backup_generation = "backup-gen-test"
            store = Neo4jStore.from_vault(vault_path, storage_config)
            try:
                with store._driver.session(database=store._database) as session:  # noqa: SLF001
                    session.run(
                        "MATCH (m:Node {id: $id, vault_id: $vault_id, "
                        "_generation: $meta_generation}) SET m.backup_tag = $backup_tag",
                        {
                            "id": schema.SCHEMA_METADATA_NODE_ID,
                            "vault_id": store.vault_id,
                            "meta_generation": schema.NEO4J_METADATA_GENERATION,
                            "backup_tag": backup_generation,
                        },
                    )
                    session.run(
                        "CREATE (n:Node {id: $id, vault_id: $vault_id, _generation: $generation})",
                        {
                            "id": "backup-node",
                            "vault_id": store.vault_id,
                            "generation": backup_generation,
                        },
                    )
            finally:
                store.close()

            submitted: dict[str, object] = {}

            def fake_submit(_state, kind, *, label="", params=None):
                submitted.update({"kind": kind, "label": label, "params": params})
                return _jobs.CurationJob(
                    id="rollback-neo4j-test", kind=kind, label=label, params=params or {}
                )

            monkeypatch.setattr(_jobs, "submit", fake_submit)
            accepted = c.post("/api/v1/curation/rollback", json={})
            assert accepted.status_code == 202, accepted.text
            assert submitted["kind"] == "rollback"
    finally:
        reset_state_for_tests()
