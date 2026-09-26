"""``POST /remember`` must validate ``sensitivity`` rather than silently
coercing an unrecognized value to ``"default"``.

Mirrors the fixture shape used by ``tests/test_server_companion_http.py``:
a real InMemory-backed vault with a stubbed companion provider so the
handler runs its full validation path deterministically and offline.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from okto_neuron import Vault
from okto_neuron.companion import Companion
from okto_neuron.llm import StubLLM
from okto_neuron.server import http as http_mod
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    reset_state_for_tests()
    vault = Vault.init(tmp_path / "v")
    note = Path(vault.path) / "note.md"
    note.write_text("# Title\n\nbody about knowledge graphs.\n", encoding="utf-8")

    monkeypatch.setattr(
        http_mod, "_companion", lambda state: Companion(state.vault, provider=StubLLM())
    )

    state = init_state(vault, vault.path)
    app = build_rest_app(state)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.note_path = str(note)  # type: ignore[attr-defined]
        yield c
    reset_state_for_tests()
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def test_remember_rejects_hyphenated_sensitivity_typo(client: TestClient) -> None:
    """The exact typo from the review: ``"local-only"`` (hyphen) must not be
    silently downgraded to ``"default"`` — it must fail loudly."""
    r = client.post(
        "/remember",
        json={"source": client.note_path, "sensitivity": "local-only"},
    )
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["error"] == "bad_request"
    assert "sensitivity" in body["detail"]


@pytest.mark.parametrize("bad_value", ["Local_Only", "LOCAL_ONLY", "private", "", "none", 1, True])
def test_remember_rejects_other_invalid_sensitivity_values(
    client: TestClient, bad_value: object
) -> None:
    r = client.post(
        "/remember",
        json={"source": client.note_path, "sensitivity": bad_value},
    )
    assert r.status_code == 400, r.text
    assert r.json()["error"] == "bad_request"


def test_remember_accepts_local_only_sensitivity(client: TestClient) -> None:
    r = client.post(
        "/remember",
        json={"source": client.note_path, "sensitivity": "local_only"},
    )
    assert r.status_code == 200, r.text


def test_remember_accepts_default_sensitivity(client: TestClient) -> None:
    r = client.post(
        "/remember",
        json={"source": client.note_path, "sensitivity": "default"},
    )
    assert r.status_code == 200, r.text


def test_remember_defaults_sensitivity_when_field_missing(client: TestClient) -> None:
    r = client.post("/remember", json={"source": client.note_path})
    assert r.status_code == 200, r.text


def test_remember_defaults_sensitivity_when_field_is_null(client: TestClient) -> None:
    r = client.post(
        "/remember",
        json={"source": client.note_path, "sensitivity": None},
    )
    assert r.status_code == 200, r.text
