"""Regression tests for two REST security-gate inconsistencies (deep review
3.12, 3.13):

  * 3.12 — the legacy bare ``/query``, ``/recall``, ``/ask`` routes must
    enforce the same ``MAX_QUERY_K`` upper bound their ``/api/v1`` siblings
    enforce, via the shared ``_k_cap_error`` helper.
  * 3.13 — ``resolve-review`` / ``resolve-review/batch`` (both the bare and
    ``/api/v1`` aliases) and ``GET /review-queue`` must enforce the same
    ``remote_config_allowed`` loopback gate every other write/curation-read
    route enforces.

Model-free: no LLM and no real Ladybug graph (``InMemoryStore``).
"""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from okto_neuron.server.http import MAX_QUERY_K, build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests
from okto_neuron.store.memory import InMemoryStore
from okto_neuron.vault import Vault


class _Embedder:
    def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


def _vault(tmp_path: Path) -> Vault:
    root = tmp_path / "vault"
    root.mkdir()
    return Vault(root, InMemoryStore(), embedder=_Embedder())


def _app(tmp_path: Path):
    reset_state_for_tests()
    vault = _vault(tmp_path)
    state = init_state(vault, vault.path)
    return build_rest_app(state), state


# ── 3.12: legacy /query, /recall, /ask must enforce MAX_QUERY_K ────────────


def test_legacy_query_route_rejects_oversized_k(tmp_path: Path) -> None:
    app, _state = _app(tmp_path)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            over = client.post("/query", json={"query": "x", "k": MAX_QUERY_K + 1})
            ok = client.post("/query", json={"query": "x", "k": MAX_QUERY_K})
    finally:
        reset_state_for_tests()

    assert over.status_code == 400, over.text
    assert f"k must be <= {MAX_QUERY_K}" in over.json()["detail"]
    assert ok.status_code == 200, ok.text


def test_legacy_recall_route_rejects_oversized_k(tmp_path: Path) -> None:
    app, _state = _app(tmp_path)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            over = client.post("/recall", json={"query": "x", "k": MAX_QUERY_K + 1})
            ok = client.post("/recall", json={"query": "x", "k": MAX_QUERY_K})
    finally:
        reset_state_for_tests()

    assert over.status_code == 400, over.text
    assert f"k must be <= {MAX_QUERY_K}" in over.json()["detail"]
    assert ok.status_code == 200, ok.text


def test_legacy_ask_route_rejects_oversized_k_before_touching_llm(tmp_path: Path) -> None:
    """The cap must be enforced before any companion/LLM work — a stub vault
    with no companion/LLM wiring proves the request never falls through."""
    app, _state = _app(tmp_path)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            over = client.post("/ask", json={"question": "x", "k": MAX_QUERY_K + 1})
    finally:
        reset_state_for_tests()

    assert over.status_code == 400, over.text
    assert f"k must be <= {MAX_QUERY_K}" in over.json()["detail"]


def test_legacy_ask_route_seed_k_override_still_capped(tmp_path: Path) -> None:
    """retrieval_policy.seed_k overrides the plain ``k`` field in ``ask`` — the
    cap must apply to the effective (post-override) value, matching api_ask."""
    app, _state = _app(tmp_path)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            over = client.post(
                "/ask",
                json={
                    "question": "x",
                    "k": 5,
                    "retrieval_policy": {"seed_k": MAX_QUERY_K + 1},
                },
            )
    finally:
        reset_state_for_tests()

    assert over.status_code == 400, over.text
    assert f"k must be <= {MAX_QUERY_K}" in over.json()["detail"]


# ── 3.13: resolve-review / resolve-review-batch / review-queue loopback gate ─


def test_review_queue_forbidden_from_remote_peer(tmp_path: Path) -> None:
    app, _state = _app(tmp_path)
    try:
        with TestClient(app, base_url="http://127.0.0.1", client=("203.0.113.7", 5555)) as client:
            bare = client.get("/review-queue")
            versioned = client.get("/api/v1/review-queue")
    finally:
        reset_state_for_tests()

    for resp in (bare, versioned):
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "forbidden"


def test_resolve_review_forbidden_from_remote_peer(tmp_path: Path) -> None:
    app, _state = _app(tmp_path)
    try:
        with TestClient(app, base_url="http://127.0.0.1", client=("203.0.113.7", 5555)) as client:
            bare = client.post(
                "/resolve-review",
                json={"candidate_id": "does-not-exist", "action": "commit"},
            )
            versioned = client.post(
                "/api/v1/resolve-review",
                json={"candidate_id": "does-not-exist", "action": "commit"},
            )
    finally:
        reset_state_for_tests()

    for resp in (bare, versioned):
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "forbidden"


def test_resolve_review_batch_forbidden_from_remote_peer(tmp_path: Path) -> None:
    app, _state = _app(tmp_path)
    try:
        with TestClient(app, base_url="http://127.0.0.1", client=("203.0.113.7", 5555)) as client:
            bare = client.post(
                "/review-queue/batch",
                json={"candidate_ids": ["does-not-exist"], "action": "discard"},
            )
            versioned = client.post(
                "/api/v1/review-queue/batch",
                json={"candidate_ids": ["does-not-exist"], "action": "discard"},
            )
    finally:
        reset_state_for_tests()

    for resp in (bare, versioned):
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "forbidden"


def test_review_queue_and_resolve_review_still_permitted_from_loopback(
    tmp_path: Path,
) -> None:
    """The new gate must not regress the loopback allow path (matches the
    acceptance harness's own '(3) M1: loopback caller may write' contract)."""
    app, _state = _app(tmp_path)
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            listing = client.get("/review-queue")
            # A missing candidate reaches the companion (404), proving the
            # request passed the loopback gate rather than being 403'd.
            resolve = client.post(
                "/resolve-review",
                json={"candidate_id": "does-not-exist", "action": "commit"},
            )
    finally:
        reset_state_for_tests()

    assert listing.status_code == 200, listing.text
    assert listing.json() == {"status": "ok", "items": []}
    assert resolve.status_code == 404, resolve.text
    assert resolve.json()["error"] == "review_item_not_found"
