"""GET /review-queue pagination and the per-vault refusal of a v1 vault (#14)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from okto_neuron.consolidate import NodeCandidate
from okto_neuron.consolidate.review_queue import ReviewQueue
from okto_neuron.server import http as http_mod
from okto_neuron.server._vault_pool import VaultPoolError
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import ServerState, init_state, reset_state_for_tests
from okto_neuron.vault import Vault

REAL_QUEUE_GATE = True


def _listing(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def client(tmp_path: Path):
    reset_state_for_tests()
    vault = Vault.init(tmp_path / "v", packs=["core"])
    queue = ReviewQueue(Path(vault.path) / ".marginalia", vault.store)
    for index in range(25):
        queue.enqueue(
            NodeCandidate(type="Concept", title=f"item {index}", facets={"block_id": f"blk-{index:02d}"}),
            "low_confidence" if index % 2 else "contradiction",
        )
    state = init_state(vault, vault.path)
    with TestClient(build_rest_app(state), base_url="http://127.0.0.1") as test_client:
        yield test_client
    reset_state_for_tests()


@pytest.mark.parametrize("path", ["/review-queue", "/api/v1/review-queue"])
def test_no_limit_keeps_the_full_list_and_is_marked_deprecated(client: TestClient, path: str) -> None:
    response = client.get(path)

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 25
    assert body["total"] == 25 and body["next_cursor"] is None and body["status"] == "ok"
    assert response.headers["Deprecation"] == "true"


def test_limit_pages_the_queue_with_an_opaque_cursor(client: TestClient) -> None:
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": "10", **({"cursor": cursor} if cursor else {})}
        response = client.get("/api/v1/review-queue", params=params)
        assert response.status_code == 200, response.text
        assert "Deprecation" not in response.headers
        body = response.json()
        seen.extend(item["candidate_id"] for item in body["items"])
        assert body["total"] == 25
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    assert len(seen) == len(set(seen)) == 25
    full = [item["candidate_id"] for item in client.get("/api/v1/review-queue").json()["items"]]
    assert seen == full


def test_limit_zero_returns_only_the_total(client: TestClient) -> None:
    response = client.get("/api/v1/review-queue", params={"limit": "0"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "items": [], "next_cursor": None, "total": 25}


def test_only_the_pages_evidence_blocks_are_fetched(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = http_mod.get_state().vault.store
    fetched: list[list[str]] = []
    real = store.get_nodes

    def recording(ids):
        fetched.append(list(ids))
        return real(ids)

    monkeypatch.setattr(store, "get_nodes", recording)

    page = client.get("/api/v1/review-queue", params={"limit": "3"}).json()
    assert len(page["items"]) == 3
    assert fetched == [[item["block_id"] for item in page["items"]]]
    assert len(fetched[0]) == 3

    fetched.clear()
    assert client.get("/api/v1/review-queue", params={"limit": "0"}).json()["items"] == []
    assert fetched == []  # limit=0 is the cheap count: no evidence batch at all

    client.get("/api/v1/review-queue")  # legacy full list: one batch for all 25
    assert len(fetched) == 1 and len(fetched[0]) == 25


def test_limit_is_capped_and_bad_input_is_a_400(client: TestClient) -> None:
    assert client.get("/review-queue", params={"limit": "5000"}).status_code == 200
    for params in ({"limit": "-1"}, {"limit": "abc"}, {"cursor": "x"}, {"limit": "5", "cursor": "bad"}):
        response = client.get("/review-queue", params=params)
        assert response.status_code == 400, (params, response.text)
        assert response.json()["error"] == "bad_request"


def _make_v1_vault(path: Path) -> Path:
    vault = Vault.init(path, packs=["core"])
    root = Path(vault.path)
    vault.close()
    config = root / "okto-neuron.yaml"
    config.write_text(
        config.read_text().replace("marginalia_yaml_version: 2", "marginalia_yaml_version: 1"),
        encoding="utf-8",
    )
    (root / ".marginalia").mkdir(exist_ok=True)
    (root / ".marginalia" / "review_queue.json").write_text('{"entries": []}\n', encoding="utf-8")
    return root.resolve(strict=False)


def test_a_v1_vault_is_refused_alone_and_nothing_of_it_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    reset_state_for_tests()
    root = tmp_path / "home" / "roots"
    served = Vault.init(root / "served", packs=["core"])
    old_path = _make_v1_vault(root / "old")
    served_path = Path(served.path).resolve(strict=False)
    config_path = tmp_path / "home" / ".okto-neuron" / "okto-neuron.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        f'marginalia_toml_version = 1\nvault_roots = [{json.dumps(str(root))}]\n',
        encoding="utf-8",
    )
    state = init_state(served, served_path)
    before = _listing(old_path)
    try:
        with TestClient(build_rest_app(state), base_url="http://127.0.0.1") as http:
            with caplog.at_level("WARNING"):
                status = http.get("/api/v1/status").json()
                http.get("/api/v1/status")  # polled again: logged once
            by_name = {Path(v["path"]).name: v for v in status["vaults"]}
            assert by_name["old"]["review_queue"]["state"] == "migration_required"
            assert by_name["old"]["review_queue"]["code"] == "review_queue_migration_required"
            assert by_name["old"]["review_queue"]["remedy"] == (
                "okto-neuron kg review-queue migrate --vault old"
            )
            assert by_name["served"]["review_queue"]["state"] == "ok"
            assert status["status"] == "degraded"
            assert any(
                "review_queue_migration_required: old" in r for r in status["degraded_reasons"]
            ), status["degraded_reasons"]
            assert caplog.text.count("refusing vault old") == 1
            assert "kg review-queue migrate --vault old" in caplog.text

            # The refused vault's routes answer 409 with the remedy, not a 500.
            refused = http.get("/api/v1/review-queue", headers={"X-Okto-Neuron-Vault": "old"})
            assert refused.status_code == 409, refused.text
            body = refused.json()
            assert body["error"] == "review_queue_migration_required"
            assert "kg review-queue migrate --vault old" in body["detail"]
            # ... while the other vault keeps serving.
            ok = http.get("/api/v1/review-queue", headers={"X-Okto-Neuron-Vault": "served"})
            assert ok.status_code == 200, ok.text
            assert ok.json()["total"] == 0
    finally:
        reset_state_for_tests()

    # No yaml touch, no sqlite, no writer lease file: the vault is byte-identical.
    assert _listing(old_path) == before
    assert not list(old_path.rglob("*.sqlite*"))
    assert not (old_path / ".okto-neuron-writer.lock").exists()
    assert json.loads((old_path / ".marginalia" / "review_queue.json").read_text()) == {"entries": []}
