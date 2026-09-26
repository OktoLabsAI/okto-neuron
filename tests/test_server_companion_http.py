"""HTTP tests for the companion surface (Phase F): /remember /recall /ask /review-queue.

Runs against a real InMemory-backed vault (stub embedder, no network). The
companion is pinned to StubLLM by monkeypatching the handler factory, so the
endpoints are deterministic and offline. Assertions check response shape and
status, not exact LLM-derived counts.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from okto_neuron import Vault
from okto_neuron.companion import Companion, ReviewItem
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


def test_remember_ok(client: TestClient) -> None:
    r = client.post("/remember", json={"source": client.note_path})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["document_id"]
    assert isinstance(body["committed"], int)
    assert isinstance(body["queued"], int)
    assert isinstance(body["outcomes"], list)
    assert isinstance(body["outcome"], dict)


def test_verified_remember_surfaces_reconciliation_schedule(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron.companion import RememberResult

    class _VerifiedCompanion:
        @staticmethod
        def remember(source, *, sensitivity="default"):  # type: ignore[no-untyped-def]
            return RememberResult(
                document_id="verified-doc",
                committed=1,
                blocks_total=1,
                outcome={
                    "quality": "complete",
                    "integrity": {
                        "status": "verified",
                        "graph_generation": "generation-a",
                    },
                },
            )

    monkeypatch.setattr(http_mod, "_companion", lambda _state: _VerifiedCompanion())

    response = client.post("/remember", json={"source": client.note_path})

    assert response.status_code == 200, response.text
    semantic = response.json()["outcome"]["cross_document_reconciliation"]
    assert semantic["state"] == "scheduled"


def test_remember_missing_field_400(client: TestClient) -> None:
    r = client.post("/remember", json={})
    assert r.status_code == 400
    assert r.json()["error"] == "bad_request"


def test_recall_ok(client: TestClient) -> None:
    r = client.post("/recall", json={"query": "knowledge graphs", "k": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert isinstance(body["results"], list)


def test_ask_ok(client: TestClient) -> None:
    r = client.post("/ask", json={"question": "what is this?", "k": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    # "ok" only for a clean synthesis; anything else is "degraded" (S1).
    expected = "ok" if body["retrieval"]["synthesis_status"] == "ok" else "degraded"
    assert body["status"] == expected
    assert "text" in body
    assert isinstance(body["citations"], list)


def test_ask_without_llm_is_degraded_and_never_calls_a_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vault on the built-in defaults (empty model) must not dial anything."""
    import okto_neuron.llm as llm_mod

    def _no_call(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("ask built an LLM provider with no model configured")

    monkeypatch.setattr(llm_mod, "get_provider", _no_call)
    monkeypatch.setattr(http_mod, "_companion", lambda state: Companion(state.vault))
    state_vault = http_mod.get_state().vault
    state_vault.add(Path(client.note_path))  # type: ignore[attr-defined]

    r = client.post("/ask", json={"question": "knowledge graphs", "k": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "degraded"
    assert body["text"] == ""
    assert body["retrieval"]["synthesis_status"] == "no_llm"
    assert "llm.defaults.model" in body["retrieval"]["no_llm_reason"]
    assert "provider_error" not in body["retrieval"]
    assert body["citations"]  # retrieval still answered


@pytest.fixture
def offload_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every function offloaded via ``asyncio.to_thread`` so tests can
    prove a handler runs its blocking work off the event loop rather than inline.
    Delegates to the real implementation so behaviour is unchanged."""
    seen: list[str] = []
    real = asyncio.to_thread

    async def _spy(func, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        seen.append(getattr(func, "__name__", repr(func)))
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _spy)
    return seen


def test_remember_offloads_blocking_call(client: TestClient, offload_spy: list[str]) -> None:
    r = client.post("/remember", json={"source": client.note_path})
    assert r.status_code == 200, r.text
    # The blocking companion.remember must be awaited off the loop, not run inline.
    assert "remember" in offload_spy


def test_ask_offloads_blocking_call(client: TestClient, offload_spy: list[str]) -> None:
    r = client.post("/ask", json={"question": "what is this?", "k": 5})
    assert r.status_code == 200, r.text
    assert "ask" in offload_spy


def test_recall_offloads_blocking_call(client: TestClient, offload_spy: list[str]) -> None:
    r = client.post("/recall", json={"query": "knowledge graphs", "k": 5})
    assert r.status_code == 200, r.text
    # The full measured query path is offloaded as one blocking unit.
    assert "_query_with_recall_cost" in offload_spy


@pytest.mark.parametrize("path", ["/review-queue", "/api/v1/review-queue"])
def test_review_queue_ok(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert isinstance(body["items"], list)


@pytest.mark.parametrize("path", ["/resolve-review", "/api/v1/resolve-review"])
def test_manual_link_action_is_not_a_public_review_operation(
    client: TestClient,
    path: str,
) -> None:
    response = client.post(
        path,
        json={"candidate_id": "candidate", "action": "link"},
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"] == "bad_request"
    assert "commit" in response.json()["detail"]
    assert "link" not in response.json()["detail"]


def test_review_queue_exposes_discriminated_node_and_read_only_relation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Candidate:
        type = "raw_relation"
        src_ref = "bilbo"
        dst_ref = "shire"
        dst_literal = None
        confidence = 0.72
        block_id = None
        byte_start = 10
        byte_end = 30
        content_hash = "a" * 64

        @staticmethod
        def model_dump(*, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"type": "raw_relation", "src_ref": "bilbo", "dst_ref": "shire"}

    class _Proposal:
        admitted_predicate = "lives_in"

        @staticmethod
        def to_json() -> dict[str, object]:
            return {
                "admission": {
                    "state": "provisional",
                    "reason": "queue_unregistered",
                    "predicate": "lives_in",
                },
                "gate_input": {"grounding": {"subject_supported": True}},
            }

    class _ReviewCompanion:
        @staticmethod
        def review_queue_all() -> list[object]:
            return [
                ReviewItem(
                    candidate_id="node-1",
                    type="Concept",
                    title="Bilbo",
                    confidence=0.5,
                    reason="low_confidence",
                    source_path="/vault/hobbit.md",
                ),
                SimpleNamespace(
                    kind="relation",
                    candidate_id="relation-1",
                    reason="queue_predicate",
                    candidate=_Candidate(),
                    pinned_proposal=_Proposal(),
                ),
            ]

    monkeypatch.setattr(http_mod, "_companion", lambda _state: _ReviewCompanion())

    response = client.get("/api/v1/review-queue")

    assert response.status_code == 200, response.text
    node, relation = response.json()["items"]
    assert node["kind"] == "node"
    assert node["source_evidence"]["source_path"] == "/vault/hobbit.md"
    assert relation["kind"] == "relation"
    assert relation["reason"] == "queue_predicate"
    assert relation["pinned_proposal"]["admission"]["predicate"] == "lives_in"
