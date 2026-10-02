"""Task 6 security boundaries for loopback REST/UI and privileged MCP.

Model-free: no LLM and no real Ladybug graph. The tests prove that direct local
browser profiles need no credential while browser cross-site writes, remote
writes, and unauthenticated MCP calls remain closed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import okto_neuron.companion as companion_mod
from okto_neuron.companion import (
    Companion,
    SourceOutsideVaultError,
    _source_context_for_hits,
    _source_is_ingestable_path,
    _source_outside_roots_message,
)
from okto_neuron.server import runtime as runtime_mod
from okto_neuron.server.http import AuthTokenMiddleware, build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests


class _StubDoc:
    id = "doc_1"
    title = "stub"
    path = "stub.md"


class _StubVault:
    """Minimal vault stub. Carries ``path`` so Companion._vault_config() loads a
    default (config-less) VaultConfig without opening a real graph."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def add(self, source):  # pragma: no cover - write gate short-circuits first
        return _StubDoc()

    def query(self, text, *, k=5, expand_context=True):
        return []

    def close(self):
        pass


@pytest.fixture
def _clean_state():
    reset_state_for_tests()
    yield
    reset_state_for_tests()


def _make_app(tmp_path: Path, *, allow_remote: bool = False):
    vault = _StubVault(tmp_path)
    state = init_state(vault, tmp_path, allow_remote=allow_remote)
    return build_rest_app(state), state


# ── Fix 1: writes are loopback-only EVEN under --allow-remote ──────────────────


def test_remember_and_add_forbidden_from_remote_peer_under_allow_remote(tmp_path, _clean_state):
    """A server bound --allow-remote still refuses writes from a non-loopback peer
    (with a valid token) — the write surface never widens."""
    app, state = _make_app(tmp_path, allow_remote=True)
    hdr = {"Authorization": f"Bearer {state.auth_token}"}
    with TestClient(app, base_url="http://127.0.0.1", client=("203.0.113.7", 5555)) as c:
        r = c.post("/remember", json={"source": "some text"}, headers=hdr)
        assert r.status_code == 403
        assert r.json()["error"] == "forbidden"
        r2 = c.post("/add", json={"path": "x.md", "content": "hi"}, headers=hdr)
        assert r2.status_code == 403
        assert r2.json()["error"] == "forbidden"


def test_mcp_remember_uses_loopback_gate(tmp_path, _clean_state):
    """The MCP remember tool gates on the same loopback helper init_vault uses."""
    # A non-loopback fastmcp request must be denied; assert the shared helper the
    # tool calls returns False for such a peer (the tool raises 'forbidden' on it).
    import types

    class _Req:
        def __init__(self, host):
            self.client = types.SimpleNamespace(host=host)
            self.query_params = {}

    from okto_neuron.server.http import request_is_loopback

    assert request_is_loopback(_Req("203.0.113.7")) is False
    assert request_is_loopback(_Req("127.0.0.1")) is True


# ── Fix 2: remember(path) is confined to the vault root + configured watch roots ─


def test_source_is_ingestable_path_confinement(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    inside = vault / "note.md"
    inside.write_text("x", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")

    assert _source_is_ingestable_path(inside, vault, []) is True
    assert _source_is_ingestable_path(outside, vault, []) is False
    # a configured watch root re-admits the external file
    assert _source_is_ingestable_path(outside, vault, [str(tmp_path)]) is True
    # traversal that resolves to a real out-of-vault file is rejected
    trav = vault / ".." / "outside.md"
    assert _source_is_ingestable_path(trav, vault, []) is False
    # a non-file source (raw text / missing path) is left to the ingest reader
    assert _source_is_ingestable_path("just some raw text", vault, []) is True


def test_companion_remember_rejects_out_of_vault_file(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "etc_passwd.md"
    outside.write_text("root:x:0:0", encoding="utf-8")

    companion = Companion(_StubVault(vault))
    with pytest.raises(SourceOutsideVaultError):
        companion.remember(str(outside))


def test_outside_root_message_lists_vault_watch_roots_and_remedies(tmp_path):
    vault = tmp_path / "vault"
    watched = tmp_path / "watched"
    message = _source_outside_roots_message("../notes/a.md", vault, [str(watched), "/srv/drop"])

    assert message.startswith(
        "refusing to remember source outside the vault and watch roots: '../notes/a.md'."
    )
    assert repr(str(vault)) in message
    assert repr(str(watched)) in message and "'/srv/drop'" in message
    assert "none configured" not in message
    for remedy in ("copy the file under", "`folder_watch.roots`", "raw text", ".marginalia/sources/"):
        assert remedy in message


def test_companion_remember_outside_message_says_no_watch_roots_are_configured(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "notes.md"
    outside.write_text("secret", encoding="utf-8")

    with pytest.raises(SourceOutsideVaultError) as exc_info:
        Companion(_StubVault(vault)).remember(str(outside))

    message = str(exc_info.value)
    assert repr(str(outside)) in message
    assert repr(str(vault)) in message
    assert "folder-watch roots: none configured" in message


# ── Fix 3: the DEFAULT ask path threads vault_root into the byte-read guard ─────


def test_default_ask_threads_vault_root(tmp_path, monkeypatch):
    captured: dict = {}

    class _Stop(Exception):
        pass

    def _spy(hits, *, vault_root=None, max_token_budget=None, **_kwargs):
        # ``**_kwargs`` absorbs newer optional kwargs (e.g. ``source_reads``,
        # issue #4 fix 4) this spy doesn't need to observe.
        captured["vault_root"] = vault_root
        raise _Stop  # short-circuit once we've observed the kwarg

    monkeypatch.setattr(companion_mod, "_source_context_for_hits", _spy)

    class _Node:
        id = "n1"
        name = "n"

    class _Hit:
        node = _Node()
        provenance = None
        context_spans = ()

    class _V:
        def __init__(self, path):
            self.path = path

        def query(self, q, *, k=20, expand_context=True, **_seed_kwargs):
            # ``**_seed_kwargs`` absorbs the Fix B seed-diversity/quota kwargs
            # the real Vault.query accepts — this fake only observes routing.
            return [_Hit()]

    # A provider is injected so the block path reaches source assembly; with no
    # usable LLM, ask stops before reading any source bytes (synthesis "no_llm").
    from okto_neuron.llm import StubLLM

    companion = Companion(_V(tmp_path), provider=StubLLM())
    with pytest.raises(_Stop):
        companion.ask("q?")
    assert captured["vault_root"] == tmp_path


def test_source_context_guard_drops_out_of_root_slice(tmp_path):
    """With vault_root threaded, a hit whose stored source_path escapes the vault
    contributes no bytes to the context (the guard rejects the read)."""
    outside = tmp_path / "outside.md"
    outside.write_text("SECRET-EXFIL", encoding="utf-8")
    vault = tmp_path / "vault"
    vault.mkdir()

    class _Prov:
        path = str(outside)
        byte_start = 0
        byte_end = 12

    class _Node:
        id = "n1"
        name = ""

    class _Hit:
        node = _Node()
        provenance = _Prov()
        context_spans = ()

    ctx = _source_context_for_hits([_Hit()], vault_root=vault)
    assert "SECRET-EXFIL" not in ctx


# ── Fix 4: direct loopback REST/UI + browser write defenses ───────────────────


def test_rest_ui_boots_without_any_active_vault(tmp_path, _clean_state):
    state = init_state(None, None)
    app = build_rest_app(state)
    with TestClient(app, base_url="http://127.0.0.1:7777") as client:
        root = client.get("/", headers={"Accept": "text/html"})
        vaults = client.get("/api/v1/vaults")
        status = client.get("/api/v1/status")

    assert root.status_code == 200
    assert vaults.status_code == 200
    assert status.status_code == 200
    assert status.json()["active_vault"] is False
    assert all("set-cookie" not in response.headers for response in (root, vaults, status))


def test_rest_and_ui_are_direct_in_two_independent_browser_profiles(tmp_path, _clean_state):
    app, _ = _make_app(tmp_path)
    responses = []
    for client_port in (5555, 5556):
        with TestClient(
            app,
            base_url="http://127.0.0.1:7777",
            client=("127.0.0.1", client_port),
        ) as browser:
            responses.extend(
                [
                    browser.get("/", headers={"Accept": "text/html"}),
                    browser.get("/api/v1/vaults"),
                    browser.get("/api/v1/status"),
                ]
            )

    assert [response.status_code for response in responses] == [200] * 6
    assert all("set-cookie" not in response.headers for response in responses)
    assert responses[1].json()["status"] == "ok"
    assert responses[2].json()["vault_path"] == str(tmp_path)


def test_rest_security_headers_deny_framing(tmp_path, _clean_state):
    app, _ = _make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:7777") as client:
        response = client.get("/api/v1/vaults")

    assert response.headers["content-security-policy"] == "frame-ancestors 'none'"
    assert response.headers["cross-origin-resource-policy"] == "same-origin"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://attacker.example"},
        {"Sec-Fetch-Site": "cross-site"},
    ],
)
def test_cross_site_browser_writes_are_rejected(tmp_path, _clean_state, headers):
    app, _ = _make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:7777") as client:
        response = client.post("/api/v1/ingest-cancel", json={}, headers=headers)

    assert response.status_code == 403
    assert response.json()["error"] == "forbidden_origin"
    assert response.headers["x-frame-options"] == "DENY"


def test_same_origin_json_write_succeeds(tmp_path, _clean_state):
    app, _ = _make_app(tmp_path)
    headers = {"Origin": "http://127.0.0.1:7777", "Sec-Fetch-Site": "same-origin"}
    with TestClient(app, base_url="http://127.0.0.1:7777") as client:
        response = client.post("/api/v1/ingest-cancel", json={}, headers=headers)

    assert response.status_code == 200


def test_non_json_write_is_rejected_before_route(tmp_path, _clean_state):
    app, _ = _make_app(tmp_path)
    with TestClient(app, base_url="http://127.0.0.1:7777") as client:
        response = client.post(
            "/api/v1/ingest-cancel",
            content="submit=1",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 415
    assert response.json()["error"] == "unsupported_media_type"


# ── Fix 5: MCP alone retains bearer authentication ───────────────────────────


def test_mcp_requires_only_authorization_bearer(tmp_path, _clean_state):
    _, state = _make_app(tmp_path)

    async def mcp_ok(_request):
        return JSONResponse({"status": "ok"})

    app = AuthTokenMiddleware(Starlette(routes=[Route("/mcp", mcp_ok)]))
    with TestClient(app, base_url="http://127.0.0.1:8201") as client:
        assert client.get("/mcp").status_code == 401
        assert client.get(f"/mcp?token={state.auth_token}").status_code == 401
        assert (
            client.get(
                "/mcp", headers={"Cookie": f"marginalia_token={state.auth_token}"}
            ).status_code
            == 401
        )
        assert (
            client.get("/mcp", headers={"X-Okto-Neuron-Token": state.auth_token}).status_code == 401
        )
        accepted = client.get("/mcp", headers={"Authorization": f"Bearer {state.auth_token}"})

    assert accepted.status_code == 200
    assert accepted.json() == {"status": "ok"}


def test_mcp_401_detail_distinguishes_bearer_failures(tmp_path, _clean_state):
    _, state = _make_app(tmp_path)

    async def mcp_ok(_request):
        return JSONResponse({"status": "ok"})

    app = AuthTokenMiddleware(Starlette(routes=[Route("/mcp", mcp_ok)]))
    with TestClient(app, base_url="http://127.0.0.1:8201") as client:
        cases = {
            "missing": client.get("/mcp"),
            # `${OKTO_NEURON_TOKEN}` expanded to nothing.
            "empty": client.get("/mcp", headers={"Authorization": "Bearer "}),
            "scheme": client.get("/mcp", headers={"Authorization": "Basic Zm9vOmJhcg=="}),
            "invalid": client.get("/mcp", headers={"Authorization": "Bearer not-the-token"}),
        }

    details = set()
    for response in cases.values():
        assert response.status_code == 401
        body = response.json()
        assert body["error"] == "unauthorized"
        assert body["status"] == 401
        assert state.auth_token not in body["detail"]
        details.add(body["detail"])

    assert len(details) == len(cases)


# ── Fix 6: application-scoped token file + migration + 0600 perms ────────────


def test_auth_token_path_is_application_scoped_and_per_port(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    p1 = runtime_mod.auth_token_path(tmp_path / "vault-a", 7777)
    p2 = runtime_mod.auth_token_path(tmp_path / "vault-b", 7777)
    p3 = runtime_mod.auth_token_path(None, 7799)

    assert p1 == p2 == tmp_path / ".okto-neuron" / "daemon-7777.token"
    assert p3 == tmp_path / ".okto-neuron" / "daemon-7799.token"


def test_legacy_vault_token_is_migrated_to_application_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    legacy = vault / ".marginalia" / "daemon.token"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy-capability-token", encoding="utf-8")

    token = runtime_mod._read_auth_token(vault, 7777)
    target = runtime_mod.auth_token_path(vault, 7777)

    assert token == "legacy-capability-token"
    assert target.read_text(encoding="utf-8") == token
    assert (target.stat().st_mode & 0o777) == 0o600


def test_write_auth_token_is_0600_and_not_vault_scoped(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runtime_mod._write_auth_token("tok-abc", tmp_path / "vault", 7777)
    target = tmp_path / "home" / ".okto-neuron" / "daemon-7777.token"
    assert target.read_text(encoding="utf-8") == "tok-abc"
    assert (target.stat().st_mode & 0o777) == 0o600


def test_pre_0_3_0_application_token_is_adopted_from_the_legacy_home(tmp_path, monkeypatch):
    """0.2.x kept the daemon credential in ~/.marginalia (D5): MCP clients keep working."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    legacy = tmp_path / "home" / ".marginalia" / "daemon-7777.token"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("legacy-application-token", encoding="utf-8")

    token = runtime_mod._read_auth_token(None, 7777)

    target = tmp_path / "home" / ".okto-neuron" / "daemon-7777.token"
    assert token == "legacy-application-token"
    assert target.read_text(encoding="utf-8") == token
    assert (target.stat().st_mode & 0o777) == 0o600
    assert legacy.read_text(encoding="utf-8") == "legacy-application-token"
