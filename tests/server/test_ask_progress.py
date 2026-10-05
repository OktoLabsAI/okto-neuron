"""P2: MCP ask progress — heartbeat, stage notifications, token counts.

A real fastmcp Client (progressToken included) calls ask against a stub
companion whose synthesis is deliberately slow and emits tokens slowly; the
client must receive >=2 progress notifications (at minimum the stage changes
retrieving -> synthesizing -> done, and token-count progress during
synthesis) and the final answer payload unchanged. No notification ever
carries answer TEXT — only stage names and "synthesizing: N tokens" counts.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from okto_neuron.server import http as http_module
from okto_neuron.server import runtime
from okto_neuron.server.state import ServerState

pytest.importorskip("fastmcp")


def _new_vault(tmp_path: Path, name: str):  # type: ignore[no-untyped-def]
    from okto_neuron import Vault

    root = tmp_path / name
    root.mkdir(parents=True)
    (root / "okto-neuron.yaml").write_text(
        "marginalia_yaml_version: 2\n"
        f"vault_id: {name}\n"
        "federation_opt_in: false\n"
        "packs: [core]\n"
        "embedding: {provider: stub, dimension: 16}\n"
        "storage: {backend: grafx}\n",
        encoding="utf-8",
    )
    import yaml  # noqa: F401

    return Vault.open(root), root


class _SlowAskCompanion:
    """ask() that takes ~1.5s and reports stages + tokens like the real one."""

    def __init__(self) -> None:
        self.seen_stages: list[str] = []
        self.saw_token_hook = False

    def ask(self, question, *, k=20, retrieval_policy=None, on_stage=None, on_token=None):  # type: ignore[no-untyped-def]
        if on_stage:
            on_stage("retrieving")
        time.sleep(0.2)
        if on_stage:
            on_stage("synthesizing")
        for i in range(30):
            time.sleep(0.05)
            if on_token:
                self.saw_token_hook = True
                on_token(f"tok{i} ")
        if on_stage:
            on_stage("done")
        return SimpleNamespace(
            text="the slow answer",
            citations=(),
            subgraph_evidence_ids=(),
            retrieval={"synthesis_status": "ok"},
        )


@pytest.mark.asyncio
async def test_ask_emits_stage_and_token_progress_notifications(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp import Client
    vault, root = _new_vault(tmp_path, "askprogress")
    state = ServerState(vault=vault, vault_path=root, multi_vault_runtime_enabled=True)
    slow = _SlowAskCompanion()
    monkeypatch.setattr(http_module, "companion_for", lambda _vault: slow)

    notifications: list[dict[str, Any]] = []

    async def on_progress(progress: float | None, total: float | None, message: str | None) -> None:
        notifications.append(
            {"progress": progress, "total": total, "message": message or ""}
        )

    server = runtime._build_mcp_server(state)
    client = Client(server, progress_handler=on_progress)
    try:
        async with client:
            result = await client.call_tool("ask", {"question": "anything", "k": 3})
            payload = result.data if isinstance(result.data, dict) else result.data[0]
    finally:
        state.close()

    assert payload["text"] == "the slow answer"
    assert slow.saw_token_hook, "the token hook must reach companion.ask"
    messages = [n["message"] for n in notifications]
    # Stages arrive as notifications. The stage NAME may ride a notification
    # whose message is exactly the stage (or carry the count prefix for
    # synthesizing); assert on the exact stage strings first.
    assert "retrieving" in messages, messages
    assert "done" in messages, messages
    assert any(
        m == "synthesizing" or m.startswith("synthesizing: ") for m in messages
    ), messages
    # Token-count progress arrived during synthesis, throttled (30 slow tokens
    # over ~1.5s at >=250ms must produce several but not 30).
    token_notes = [m for m in messages if m.startswith("synthesizing: ")]
    assert 2 <= len(token_notes) <= 15, (len(token_notes), messages)
    for note in token_notes:
        assert note.count("tokens") == 1
    # NEVER the text itself: only the three stage names and token COUNTS.
    allowed = {"retrieving", "synthesizing", "done"}
    for note in messages:
        assert note in allowed or note.startswith("synthesizing: "), note
        assert "the slow answer" not in note, note
        for i in range(30):
            assert f"tok{i} " not in note, note


@pytest.mark.asyncio
async def test_ask_progress_never_breaks_the_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A notification transport that explodes must not fail the ask."""

    class _BoomCompanion:
        def ask(self, question, *, k=20, retrieval_policy=None, on_stage=None, on_token=None):  # type: ignore[no-untyped-def]
            if on_stage:
                on_stage("retrieving")
                on_stage("synthesizing")
            if on_token:
                for i in range(5):
                    on_token(f"t{i} ")
            if on_stage:
                on_stage("done")
            return SimpleNamespace(
                text="still answered",
                citations=(),
                subgraph_evidence_ids=(),
                retrieval={"synthesis_status": "ok"},
            )

    import fastmcp.server.context as fastmcp_context

    async def _boom(*_a: object, **_kw: object) -> None:
        raise RuntimeError("transport gone")

    monkeypatch.setattr(fastmcp_context.Context, "report_progress", _boom)
    vault, root = _new_vault(tmp_path, "askboom")
    state = ServerState(vault=vault, vault_path=root, multi_vault_runtime_enabled=True)
    monkeypatch.setattr(http_module, "companion_for", lambda _vault: _BoomCompanion())

    server = runtime._build_mcp_server(state)
    from fastmcp import Client

    try:
        async with Client(server) as client:
            result = await client.call_tool("ask", {"question": "q", "k": 3})
            payload = result.data if isinstance(result.data, dict) else result.data[0]
    finally:
        state.close()
    assert payload["text"] == "still answered"
