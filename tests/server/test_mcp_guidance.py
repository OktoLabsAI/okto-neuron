"""P3: MCP guidance — instructions, the recall tool, prompts, status resource.

Pins the new agent-facing surface: a real fastmcp Client sees the prompts and
resources; ``recall`` returns provenance (vault-RELATIVE paths only, never
absolute) with optional capped span text; the status resource carries counts,
the embedding dimension, pending sealed plans, a queue summary, and the LLM
locality verdict.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

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
    return Vault.open(root), root


class _FakeProvenance:
    def __init__(self, path: str, start: int, end: int) -> None:
        self.path = path
        self.byte_start = start
        self.byte_end = end
        self.content_hash = "sha256:" + "a" * 64
        self.document_id = "doc-1"
        self.block_id = "blk-1"


class _FakeNode:
    def __init__(self) -> None:
        self.id = "node-1"
        self.type = "Document"
        self.title = "Ash dispersal notes"


class _FakeHit:
    def __init__(self, path: str) -> None:
        self.node = _FakeNode()
        self.score = 0.87
        self.provenance = _FakeProvenance(path, 0, 40)


class _FakeLease:
    def __init__(self, vault: "_FakeVault") -> None:
        self.vault = vault

    def __enter__(self):
        return self.vault

    def __exit__(self, *_a: object) -> None:
        return None

    def release(self) -> None:
        return None


class _FakeVault:
    def __init__(self, root: Path) -> None:
        self.path = root
        self.store = SimpleNamespace(embedding_dim=16)
        self._root = root

    def query(self, query: str, k: int = 10):  # type: ignore[no-untyped-def]
        note = self._root / "notes" / "ash.md"
        note.parent.mkdir(exist_ok=True)
        note.write_text("Volcanic ash over mountain valleys x 1234567890", encoding="utf-8")
        return [_FakeHit(str(note))]

    def close(self) -> None:
        return None


def _register_vault(monkeypatch: pytest.MonkeyPatch, name: str, root: Path) -> Path:
    """Create the vault UNDER the throwaway HOME's default vaults root, so the
    registry (which only scans cfg.vault_roots) actually lists it."""
    import shutil

    home = root.parent / f"{name}-home"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    for var in ("OKTO_NEURON_HOME", "OKTO_NEURON_CONFIG", "OKTO_NEURON_VAULT"):
        monkeypatch.delenv(var, raising=False)
    from okto_neuron.vault_registry import ensure_global_layout

    ensure_global_layout()
    registered = home / ".okto-neuron" / "vaults" / name
    if not registered.exists():
        shutil.copytree(root, registered)
    return registered


def _patch_lease(monkeypatch: pytest.MonkeyPatch, fake: "_FakeVault") -> None:
    from okto_neuron.server.state import VaultRuntime

    monkeypatch.setattr(
        VaultRuntime, "lease_vault", lambda self: _FakeLease(fake)
    )


@pytest.mark.asyncio
async def test_client_lists_prompts_and_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp import Client

    vault, root = _new_vault(tmp_path, "guidance")
    state = ServerState(vault=vault, vault_path=root, multi_vault_runtime_enabled=True)
    server = runtime._build_mcp_server(state)
    try:
        async with Client(server) as client:
            prompts = sorted(p.name for p in await client.list_prompts())
            templates = sorted(
                t.name for t in await client.list_resource_templates()
            )
        assert prompts == ["record_decision", "research"]
        assert "vault status" in templates
    finally:
        state.close()


@pytest.mark.asyncio
async def test_recall_returns_provenance_never_absolute_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp import Client

    vault, root = _new_vault(tmp_path, "recall-prov")
    state = ServerState(vault=vault, vault_path=root, multi_vault_runtime_enabled=True)
    fake = _FakeVault(root)
    _patch_lease(monkeypatch, fake)
    server = runtime._build_mcp_server(state)
    try:
        async with Client(server) as client:
            result = await client.call_tool("recall", {"query": "ash", "k": 5})
            payload = result.data if isinstance(result.data, dict) else result.data[0]
    finally:
        state.close()
    hits = payload["hits"]
    assert len(hits) == 1
    hit = hits[0]
    assert hit["id"] == "node-1" and hit["type"] == "Document"
    assert hit["title"] == "Ash dispersal notes"
    assert 0.0 < hit["score"] <= 1.0
    source = hit["source"]
    assert source["path"] == "notes/ash.md", source
    assert not source["path"].startswith("/")
    assert "byte_start" in source and "byte_end" in source
    assert source["document_id"] == "doc-1"
    assert payload["retrieval"]["vault"]


@pytest.mark.asyncio
async def test_recall_include_text_returns_capped_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp import Client

    vault, root = _new_vault(tmp_path, "recall-text")
    state = ServerState(vault=vault, vault_path=root, multi_vault_runtime_enabled=True)
    fake = _FakeVault(root)
    _patch_lease(monkeypatch, fake)
    server = runtime._build_mcp_server(state)
    try:
        async with Client(server) as client:
            result = await client.call_tool(
                "recall", {"query": "ash", "include_text": True}
            )
            payload = result.data if isinstance(result.data, dict) else result.data[0]
    finally:
        state.close()
    assert "text" in payload["hits"][0]
    assert "Volcanic ash" in payload["hits"][0]["text"]


@pytest.mark.asyncio
async def test_vault_status_resource_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp import Client

    vault, root = _new_vault(tmp_path, "statusres")
    registered = _register_vault(monkeypatch, "statusres", root)
    state = ServerState(vault=vault, vault_path=registered, multi_vault_runtime_enabled=True)
    server = runtime._build_mcp_server(state)
    try:
        async with Client(server) as client:
            import json

            # The status resource is a TEMPLATE resource: read the
            # instantiated URI for this vault's name.
            content = await client.read_resource(
                "neuron://vaults/statusres/status"
            )
            payload = json.loads(
                next(iter(c.text for c in content if hasattr(c, "text")))
            )
    finally:
        state.close()
    for key in (
        "vault",
        "pending_sealed_plans",
        "queue",
        "embedding_dim",
        "total_nodes",
        "total_edges",
        "llm_is_local",
    ):
        assert key in payload, key
    assert payload["embedding_dim"] == 16
    assert payload["pending_sealed_plans"] == 0
