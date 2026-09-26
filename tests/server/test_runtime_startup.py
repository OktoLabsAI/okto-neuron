from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from okto_neuron.config._vault import ResolvedLLM
from okto_neuron.errors import EmbeddingDimMismatch
from okto_neuron.server import runtime
from okto_neuron.server.state import ServerState


def _bedrock_resolved() -> ResolvedLLM:
    return ResolvedLLM(
        provider="bedrock",
        api_base="http://127.0.0.1:8123/v1",
        model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        api_key_env=None,
        max_tokens=1024,
        temperature=0.0,
        top_p=1.0,
        top_k=0,
        min_p=0.0,
        presence_penalty=0.0,
        enable_thinking=False,
    )


def test_startup_vault_open_degrades_on_embedding_dim_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_path = tmp_path / "vault"
    graph_path = vault_path / "graph.lbug"
    exc = EmbeddingDimMismatch(
        graph_path,
        stored_dim=384,
        configured_dim=1024,
        vault_path=vault_path,
    )

    def _raise(path: Path) -> object:
        assert path == vault_path.resolve(strict=False)
        raise exc

    monkeypatch.setattr(runtime.Vault, "open", staticmethod(_raise))

    vault, active_path, warning = runtime._open_startup_vault(vault_path)

    assert vault is None
    assert active_path is None
    assert warning is not None
    assert warning["code"] == "embedding_dim_mismatch"
    assert warning["path"] == str(vault_path.resolve(strict=False))
    assert "okto-neuron kg reembed" in str(warning["remedy"])


def test_dependency_warmup_discovers_vaults_without_opening_graphs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from okto_neuron.llm import LLMProviderError

    first = tmp_path / "first"
    second = tmp_path / "second"
    broken = tmp_path / "broken"
    for path in (first, second, broken):
        path.mkdir()
    first.joinpath("okto-neuron.yaml").write_text(
        """marginalia_yaml_version: 1
packs: [core]
llm:
  defaults:
    provider: openai
""",
        encoding="utf-8",
    )
    second.joinpath("okto-neuron.yaml").write_text(
        """marginalia_yaml_version: 1
packs: [core]
llm:
  defaults:
    provider: openai
  judge:
    provider: bedrock
""",
        encoding="utf-8",
    )
    broken.joinpath("okto-neuron.yaml").write_text("llm: [not valid", encoding="utf-8")

    monkeypatch.setattr(
        "okto_neuron.vault_registry.list_vaults",
        lambda: [
            SimpleNamespace(path=broken),
            SimpleNamespace(path=first),
            SimpleNamespace(path=second),
        ],
    )
    monkeypatch.setattr(
        runtime.Vault,
        "open",
        staticmethod(lambda path: pytest.fail(f"warmup opened graph for {path}")),
    )
    warmed: list[str] = []

    def _warm(provider: str) -> None:
        warmed.append(provider)
        if provider == "bedrock":
            raise LLMProviderError("missing optional dependency")

    monkeypatch.setattr("okto_neuron.llm.warm_provider_dependencies", _warm)

    runtime._warm_llm_provider_dependencies(ServerState(vault=None, vault_path=None))

    assert warmed == ["bedrock", "openai"]


def test_legacy_mcp_entrypoints_fail_closed(tmp_path: Path) -> None:
    from okto_neuron import mcp_server
    from okto_neuron.mcp import server as compatibility_server

    callbacks = (
        lambda: mcp_server.build_app(tmp_path),
        lambda: mcp_server.run(tmp_path, host="0.0.0.0"),
        lambda: compatibility_server.build_app(tmp_path),
        lambda: compatibility_server.run(tmp_path, host="0.0.0.0"),
    )
    for callback in callbacks:
        with pytest.raises(RuntimeError, match="authentication and loopback-only contract"):
            callback()


def test_legacy_pure_query_serializer_remains_import_compatible() -> None:
    from types import SimpleNamespace

    from okto_neuron.mcp_server import kg_query_natural, marginalia_kg_query

    hit = SimpleNamespace(
        claim_id="claim:1",
        score=0.75,
        path="notes/a.md",
        byte_start=1,
        byte_end=4,
        content_hash="abc",
        node=SimpleNamespace(title="A", name="a"),
    )
    vault = SimpleNamespace(query=lambda text, k: [hit] if (text, k) == ("topic", 3) else [])
    expected = [
        {
            "claim_id": "claim:1",
            "score": 0.75,
            "path": "notes/a.md",
            "byte_start": 1,
            "byte_end": 4,
            "content_hash": "abc",
            "title": "A",
            "name": "a",
        }
    ]
    assert kg_query_natural(vault, "topic", k=3) == expected
    assert marginalia_kg_query(vault, "topic", k=3) == expected


@pytest.mark.asyncio
async def test_serve_mcp_surface_is_the_five_memory_tools() -> None:
    """The live `serve` MCP surface is exactly ask / explore / remember / init_vault / list_vaults.

    Deliberately small graph-native surface: subgraph-grounded ask, ego-graph
    drill-down, write, vault creation, and name-only vault discovery (ADR 0014).
    The legacy
    kg_add/kg_query_natural/kg_get_provenance plus flat recall and the review pair
    were retired to avoid tool-selection ambiguity for agents.
    """
    state = ServerState(vault=None, vault_path=None)
    server = runtime._build_mcp_server(state)
    names = {tool.name for tool in await server.list_tools()}  # type: ignore[attr-defined]

    assert names == {"ask", "explore", "remember", "init_vault", "list_vaults"}


@pytest.mark.asyncio
async def test_init_vault_creates_runtime_without_switching_active(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """init_vault (ADR 0014) creates a named vault, pools it, leaves active alone.

    Driven through the FastMCP in-memory client so there is no HTTP context — the
    loopback gate's "no request = local = allowed" branch is exercised.
    """
    from fastmcp import Client

    from okto_neuron import Vault

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.delenv("OKTO_NEURON_VAULT", raising=False)

    active = Vault.init(tmp_path / "active", packs=["core"])
    state = ServerState(vault=active, vault_path=(tmp_path / "active").resolve())
    state.vault_pool.adopt(active, state.vault_path)
    server = runtime._build_mcp_server(state)
    try:
        async with Client(server) as client:
            result = await client.call_tool("init_vault", {"name": "scratch"})
            data = result.structured_content or {}
            assert data.get("name") == "scratch"
            created = Path(data["path"])
            assert (created / "okto-neuron.yaml").is_file()
            from okto_neuron.vault_registry import list_vaults

            entry = next(item for item in list_vaults() if item.path == created)
            assert entry.managed is True
            assert entry.deletable is True
            # Runtime is registered and opens lazily; the active vault is unchanged.
            assert state.runtime_for(created).vault_path == created.resolve(strict=False)
            assert state.vault_pool.peek(created) is None
            assert state.vault is active

            # Duplicate name → error.
            with pytest.raises(Exception) as excinfo:
                await client.call_tool("init_vault", {"name": "scratch"})
            assert "vault_exists" in str(excinfo.value)
    finally:
        state.close()


@pytest.mark.asyncio
async def test_init_vault_hints_when_the_new_vault_has_no_llm_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fix B2: a vault created with the application defaults inherits an EMPTY
    ``llm.defaults.model`` (discovery-first), so it provably cannot ingest.
    Creation genuinely succeeds, so the warning rides the RESULT (additively,
    alongside name/path) and names the key to set."""
    from fastmcp import Client

    from okto_neuron import Vault

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.delenv("OKTO_NEURON_VAULT", raising=False)

    active = Vault.init(tmp_path / "active", packs=["core"])
    state = ServerState(vault=active, vault_path=(tmp_path / "active").resolve())
    state.vault_pool.adopt(active, state.vault_path)
    server = runtime._build_mcp_server(state)
    try:
        async with Client(server) as client:
            result = await client.call_tool("init_vault", {"name": "nomodel"})
            data = result.structured_content or {}
            assert data.get("name") == "nomodel"
            assert data.get("path")
            hint = str(data.get("hint") or "")
            assert "llm.defaults.model" in hint, data
    finally:
        state.close()


@pytest.mark.asyncio
async def test_init_vault_has_no_hint_when_a_model_is_configured(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2 negative: a vault whose resolved config names a model is reported
    clean — no hint key."""
    from fastmcp import Client

    from okto_neuron import Vault
    from okto_neuron.server import http as http_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.delenv("OKTO_NEURON_VAULT", raising=False)

    real_create = http_mod._create_inheriting_vault

    def _create_with_model(path, **kwargs):
        created = real_create(path, **kwargs)
        from okto_neuron.config import VaultConfig

        VaultConfig.apply_patch(path, {"llm": {"defaults": {"model": "some-real-model"}}})
        return created

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _create_with_model)

    active = Vault.init(tmp_path / "active", packs=["core"])
    state = ServerState(vault=active, vault_path=(tmp_path / "active").resolve())
    state.vault_pool.adopt(active, state.vault_path)
    server = runtime._build_mcp_server(state)
    try:
        async with Client(server) as client:
            result = await client.call_tool("init_vault", {"name": "withmodel"})
            data = result.structured_content or {}
            assert "hint" not in data, data
    finally:
        state.close()


@pytest.mark.asyncio
async def test_init_vault_default_backend_is_grafx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-94: the MCP ``init_vault`` tool defaults to ``DEFAULT_NEW_VAULT_BACKEND``
    ("grafx") when ``backend`` is omitted, matching REST's ``api_vault_create``
    (``tests/test_server_http.py::test_vault_create_default_backend_is_grafx``).
    Stubbing ``_create_inheriting_vault`` with the narrow 2-keyword signature
    proves the call is not widened for the default backend, the same DI seam
    the REST tests use."""
    import yaml

    from fastmcp import Client

    from okto_neuron import Vault
    from okto_neuron.server import http as http_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.delenv("OKTO_NEURON_VAULT", raising=False)

    def _create(path, *, packs, embedding_provider):
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text(
            "marginalia_yaml_version: 1\nstorage:\n  backend: grafx\n  reason: null\n",
            encoding="utf-8",
        )
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _create)

    active = Vault.init(tmp_path / "active", packs=["core"])
    state = ServerState(vault=active, vault_path=(tmp_path / "active").resolve())
    state.vault_pool.adopt(active, state.vault_path)
    server = runtime._build_mcp_server(state)
    try:
        async with Client(server) as client:
            result = await client.call_tool("init_vault", {"name": "grafx-default"})
            data = result.structured_content or {}
            created = Path(data["path"])
            written = yaml.safe_load((created / "okto-neuron.yaml").read_text(encoding="utf-8"))
            assert written["storage"]["backend"] == "grafx"
    finally:
        state.close()


@pytest.mark.asyncio
async def test_init_vault_neo4j_without_consent_rejects_non_loopback_storage_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-loopback ``storage_uri`` without ``allow_remote_db`` is a clear
    error naming that field -- mirrors REST's
    ``test_vault_create_neo4j_without_consent_rejects_non_loopback_storage_uri``.
    No ``_create_inheriting_vault`` stub is needed: the egress gate rejects
    the call before any filesystem work happens."""
    pytest.importorskip("neo4j")
    from fastmcp import Client

    from okto_neuron import Vault

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.delenv("OKTO_NEURON_VAULT", raising=False)

    active = Vault.init(tmp_path / "active", packs=["core"])
    state = ServerState(vault=active, vault_path=(tmp_path / "active").resolve())
    state.vault_pool.adopt(active, state.vault_path)
    server = runtime._build_mcp_server(state)
    try:
        async with Client(server) as client:
            with pytest.raises(Exception) as excinfo:
                await client.call_tool(
                    "init_vault",
                    {
                        "name": "neo4j-no-consent",
                        "backend": "neo4j",
                        "storage_uri": "bolt://db.example.com:7687",
                        "storage_credential_env": "OKTO_NEURON_NEO4J_PASSWORD",
                    },
                )
            assert "allow_remote_db" in str(excinfo.value)
    finally:
        state.close()


@pytest.mark.asyncio
async def test_init_vault_neo4j_with_consent_pins_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``allow_remote_db=True`` unblocks a non-loopback ``neo4j`` ``storage_uri``
    and threads it (plus the other storage fields) through to
    ``_create_inheriting_vault`` -- mirrors REST's
    ``test_vault_create_neo4j_with_consent_pins_it``."""
    pytest.importorskip("neo4j")
    import yaml

    from fastmcp import Client

    from okto_neuron import Vault
    from okto_neuron.server import http as http_mod

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.delenv("OKTO_NEURON_VAULT", raising=False)

    def _create(
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
        assert backend == "neo4j"
        assert storage_uri == "bolt://db.example.com:7687"
        assert storage_credential_env == "OKTO_NEURON_NEO4J_PASSWORD"
        assert storage_database == "neo4j"
        assert storage_allow_remote is True
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text(
            "marginalia_yaml_version: 1\nstorage:\n  backend: neo4j\n  reason: null\n",
            encoding="utf-8",
        )
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _create)

    active = Vault.init(tmp_path / "active", packs=["core"])
    state = ServerState(vault=active, vault_path=(tmp_path / "active").resolve())
    state.vault_pool.adopt(active, state.vault_path)
    server = runtime._build_mcp_server(state)
    try:
        async with Client(server) as client:
            result = await client.call_tool(
                "init_vault",
                {
                    "name": "neo4j-consented",
                    "backend": "neo4j",
                    "storage_uri": "bolt://db.example.com:7687",
                    "storage_credential_env": "OKTO_NEURON_NEO4J_PASSWORD",
                    "storage_database": "neo4j",
                    "allow_remote_db": True,
                },
            )
            data = result.structured_content or {}
            created = Path(data["path"])
            written = yaml.safe_load((created / "okto-neuron.yaml").read_text(encoding="utf-8"))
            assert written["storage"]["backend"] == "neo4j"
    finally:
        state.close()


@pytest.mark.asyncio
async def test_rest_and_mcp_vault_create_share_registry_mutation_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading

    from fastmcp import Client
    import httpx

    from okto_neuron.server import http as http_mod
    from okto_neuron.server.http import build_rest_app
    from okto_neuron.server.state import init_state, reset_state_for_tests
    from okto_neuron.vault_registry import read_managed_vault_marker
    import okto_neuron.vault_registry as registry

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.delenv("OKTO_NEURON_VAULT", raising=False)
    target = home / ".okto-neuron" / "vaults" / "shared"
    init_started = threading.Event()
    mcp_precheck = threading.Event()
    init_calls: list[Path] = []
    original_is_vault = registry.is_vault

    def _tracked_is_vault(path) -> bool:
        exists = original_is_vault(path)
        if init_started.is_set() and not exists:
            mcp_precheck.set()
        return exists

    def _create(path, *, packs, embedding_provider):
        # An omitted REST choice inherits both values from application defaults.
        assert packs is None
        assert embedding_provider is None
        init_calls.append(Path(path))
        init_started.set()
        assert mcp_precheck.wait(timeout=5)
        path.mkdir(parents=True)
        (path / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
        return SimpleNamespace(close=lambda: None)

    monkeypatch.setattr(http_mod, "is_vault", _tracked_is_vault)
    monkeypatch.setattr(registry, "is_vault", _tracked_is_vault)
    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _create)

    reset_state_for_tests()
    state = init_state(None, None)
    rest_app = build_rest_app(state)
    mcp_server = runtime._build_mcp_server(state)
    transport = httpx.ASGITransport(app=rest_app, client=("127.0.0.1", 53100))
    try:
        async with (
            httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as rest_client,
            Client(mcp_server) as mcp_client,
        ):
            rest_task = asyncio.create_task(
                rest_client.post("/api/v1/vaults", json={"name": "shared"})
            )
            assert await asyncio.to_thread(init_started.wait, 2)
            mcp_task = asyncio.create_task(mcp_client.call_tool("init_vault", {"name": "shared"}))
            response = await asyncio.wait_for(rest_task, timeout=10)
            with pytest.raises(Exception) as excinfo:
                await asyncio.wait_for(mcp_task, timeout=10)

        assert response.status_code == 200, response.text
        assert "vault_exists" in str(excinfo.value)
        assert init_calls == [target]
        marker = read_managed_vault_marker(target)
        assert marker is not None
        assert response.json()["created"]["id"] == marker.id
    finally:
        reset_state_for_tests()


@pytest.mark.asyncio
async def test_cancelled_create_keeps_worker_lock_and_closes_init_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading

    from fastmcp import Client
    import httpx

    from okto_neuron.server import http as http_mod
    from okto_neuron.server.http import build_rest_app
    from okto_neuron.server.state import init_state, reset_state_for_tests
    from okto_neuron.vault_registry import read_managed_vault_marker
    import okto_neuron.vault_registry as registry

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.delenv("OKTO_NEURON_VAULT", raising=False)
    target = home / ".okto-neuron" / "vaults" / "cancelled"
    init_started = threading.Event()
    second_precheck = threading.Event()
    release_init = threading.Event()
    init_calls: list[Path] = []
    closed: list[Path] = []

    def _create(path, *, packs, embedding_provider):
        del packs, embedding_provider
        resolved = Path(path)
        init_calls.append(resolved)
        init_started.set()
        assert release_init.wait(timeout=5)
        resolved.mkdir(parents=True)
        (resolved / "okto-neuron.yaml").write_text("marginalia_yaml_version: 1\n", encoding="utf-8")
        return SimpleNamespace(close=lambda: closed.append(resolved))

    original_is_vault = registry.is_vault

    def _tracked_is_vault(path) -> bool:
        exists = original_is_vault(path)
        if init_started.is_set() and not release_init.is_set() and not exists:
            second_precheck.set()
        return exists

    monkeypatch.setattr(http_mod, "_create_inheriting_vault", _create)
    monkeypatch.setattr(registry, "is_vault", _tracked_is_vault)

    reset_state_for_tests()
    state = init_state(None, None)
    rest_app = build_rest_app(state)
    mcp_server = runtime._build_mcp_server(state)
    transport = httpx.ASGITransport(app=rest_app, client=("127.0.0.1", 53101))
    try:
        async with (
            httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as rest_client,
            Client(mcp_server) as mcp_client,
        ):
            first = asyncio.create_task(
                rest_client.post("/api/v1/vaults", json={"name": "cancelled"})
            )
            assert await asyncio.to_thread(init_started.wait, 2)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first

            second = asyncio.create_task(mcp_client.call_tool("init_vault", {"name": "cancelled"}))
            assert await asyncio.to_thread(second_precheck.wait, 2)
            assert not second.done()
            assert init_calls == [target]

            release_init.set()
            with pytest.raises(Exception) as excinfo:
                await asyncio.wait_for(second, timeout=10)

        assert "vault_exists" in str(excinfo.value)
        assert init_calls == [target]
        assert closed == [target]
        assert read_managed_vault_marker(target) is not None
        assert state.vault_pool.peek(target) is None
    finally:
        release_init.set()
        reset_state_for_tests()


@pytest.mark.asyncio
async def test_mcp_remember_relays_bedrock_missing_dependency_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastmcp import Client

    from okto_neuron import Vault
    from okto_neuron.companion import Companion
    from okto_neuron.llm import LiteLLMProvider

    original_find_spec = importlib.util.find_spec

    def _missing_boto3(name: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "boto3":
            return None
        return original_find_spec(name, *args, **kwargs)

    def _bedrock_companion(vault):  # type: ignore[no-untyped-def]
        return Companion(vault, provider=LiteLLMProvider(_bedrock_resolved()))

    monkeypatch.setattr(importlib.util, "find_spec", _missing_boto3)
    monkeypatch.setattr("okto_neuron.server.http.companion_for", _bedrock_companion)

    vault = Vault.init(tmp_path / "active", packs=["core"])
    note = Path(vault.path) / "note.md"
    note.write_text(
        "# Bedrock\n\nMCP remember should relay provider setup errors.\n", encoding="utf-8"
    )
    state = ServerState(vault=vault, vault_path=Path(vault.path).resolve())
    state.vault_pool.adopt(vault, state.vault_path)
    server = runtime._build_mcp_server(state)
    try:
        async with Client(server) as client:
            # Fix 1 (issue #4 — loud total failure): this note is a single
            # Block, so the one attempted extraction failing with a provider
            # error means EVERY attempted block failed. remember() now raises
            # LLMUnavailableError instead of returning a success-shaped
            # payload with provider_error quietly set — the MCP tool call
            # surfaces that as a ToolError, not a structured "ok" result.
            with pytest.raises(Exception) as excinfo:
                await client.call_tool("remember", {"source": str(note)})

        error = str(excinfo.value)
        assert "bedrock" in error.lower()
        assert "boto3" in error
        assert "okto-neuron[litellm,bedrock]" in error
    finally:
        state.close()


@pytest.mark.asyncio
async def test_mcp_remember_materializes_raw_text_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Raw multi-line text passed to MCP ``remember`` (issue #2) is durably
    materialized under ``.marginalia/sources/`` before ingest, instead of the
    docstring's claimed-but-unimplemented raw-text mode silently doing
    nothing. ``companion_for`` is faked so this stays LLM-free; the fake
    asserts it received a real, existing file path — not the raw string."""
    from fastmcp import Client

    from okto_neuron import Vault
    from okto_neuron.companion import RememberResult

    captured: dict[str, object] = {}

    class _FakeCompanion:
        def remember(self, source, *, sensitivity="default", on_progress=None, **_kw):
            path = Path(source)
            captured["source"] = source
            captured["is_file"] = path.is_file()
            captured["content"] = path.read_text(encoding="utf-8") if path.is_file() else None
            return RememberResult(
                document_id="doc-raw-text",
                committed=1,
                blocks_total=1,
                outcome={"quality": "partial"},
            )

    monkeypatch.setattr("okto_neuron.server.http.companion_for", lambda vault: _FakeCompanion())

    vault = Vault.init(tmp_path / "active", packs=["core"])
    state = ServerState(vault=vault, vault_path=Path(vault.path).resolve())
    state.vault_pool.adopt(vault, state.vault_path)
    server = runtime._build_mcp_server(state)
    raw_text = "Line one of a note.\nLine two, not path-shaped at all.\n"
    try:
        async with Client(server) as client:
            result = await client.call_tool("remember", {"source": raw_text})

        data = result.structured_content or {}
        assert data.get("document_id") == "doc-raw-text"
        assert data.get("outcome") == {"quality": "partial"}
        assert captured["is_file"] is True
        assert captured["content"] == raw_text
        sources_dir = Path(vault.path) / ".marginalia" / "sources"
        md_files = list(sources_dir.glob("*.md"))
        assert md_files, "expected a materialized .md file under .marginalia/sources/"
    finally:
        state.close()


@pytest.mark.asyncio
async def test_mcp_remember_nonexistent_path_shaped_source_fails_as_path(
    tmp_path: Path,
) -> None:
    """A single-line, ``.md``-suffixed source that names a nonexistent file must
    still be treated as a path (not silently ingested as raw text): it fails as
    a path/ingest error, and no file is materialized under
    ``.marginalia/sources/`` for it."""
    from fastmcp import Client

    from okto_neuron import Vault

    vault = Vault.init(tmp_path / "active", packs=["core"])
    state = ServerState(vault=vault, vault_path=Path(vault.path).resolve())
    state.vault_pool.adopt(vault, state.vault_path)
    server = runtime._build_mcp_server(state)
    missing_path = str(Path(vault.path) / "notes" / "does-not-exist.md")
    try:
        async with Client(server) as client:
            with pytest.raises(Exception):
                await client.call_tool("remember", {"source": missing_path})

        sources_dir = Path(vault.path) / ".marginalia" / "sources"
        assert not sources_dir.exists() or not list(sources_dir.glob("*.md"))
    finally:
        state.close()


@pytest.mark.asyncio
async def test_mcp_remember_existing_directory_fails_as_path_not_junk_note(
    tmp_path: Path,
) -> None:
    """Defect B: an EXISTING DIRECTORY is path-shaped (``Path.exists()`` is
    True even though ``is_file()`` is False) — it must fail as a path/ingest
    error downstream, never get silently ingested as a raw-text "document"
    whose content is the literal directory path string."""
    from fastmcp import Client

    from okto_neuron import Vault

    vault = Vault.init(tmp_path / "active", packs=["core"])
    a_directory = Path(vault.path) / "some-existing-dir"
    a_directory.mkdir()
    state = ServerState(vault=vault, vault_path=Path(vault.path).resolve())
    state.vault_pool.adopt(vault, state.vault_path)
    server = runtime._build_mcp_server(state)
    try:
        async with Client(server) as client:
            with pytest.raises(Exception):
                await client.call_tool("remember", {"source": str(a_directory)})

        sources_dir = Path(vault.path) / ".marginalia" / "sources"
        assert not sources_dir.exists() or not list(sources_dir.glob("*.md"))
    finally:
        state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relative_source",
    [
        "notes/report.pdf",  # nonexistent, has a slash, non-ingestible suffix
        "notes/q3-review",  # nonexistent, has a slash, no suffix at all
        "note.pdf",  # nonexistent, no slash — bare-suffix-shaped
    ],
)
async def test_mcp_remember_path_shaped_junk_sources_fail_as_path(
    tmp_path: Path,
    relative_source: str,
) -> None:
    """Defect B: a typo'd/nonexistent path (with a wrong suffix, no suffix at
    all, or a bare filename with no slash) must still be classified as a
    PATH and fail loudly downstream — never silently ingested as a junk
    "document" whose content is the literal path string."""
    from fastmcp import Client

    from okto_neuron import Vault

    vault = Vault.init(tmp_path / "active", packs=["core"])
    source = str(Path(vault.path) / relative_source) if "/" in relative_source else relative_source
    state = ServerState(vault=vault, vault_path=Path(vault.path).resolve())
    state.vault_pool.adopt(vault, state.vault_path)
    server = runtime._build_mcp_server(state)
    try:
        async with Client(server) as client:
            with pytest.raises(Exception):
                await client.call_tool("remember", {"source": source})

        sources_dir = Path(vault.path) / ".marginalia" / "sources"
        assert not sources_dir.exists() or not list(sources_dir.glob("*.md"))
    finally:
        state.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_text",
    [
        "remember to buy milk tomorrow",
        "~5 minutes to set up",
        "~this is a note, not a home directory\nwith a second line too",
    ],
)
async def test_mcp_remember_spaced_and_tilde_prefixed_text_materializes_raw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_text: str,
) -> None:
    """Regression guard for Defect B (single-line spaced sentences stay raw
    text) and Defect 16 (a source starting with ``~`` must never crash with
    ``RuntimeError`` from ``Path.expanduser()`` — it materializes as raw text
    instead, whether single- or multi-line)."""
    from fastmcp import Client

    from okto_neuron import Vault
    from okto_neuron.companion import RememberResult

    captured: dict[str, object] = {}

    class _FakeCompanion:
        def remember(self, source, *, sensitivity="default", on_progress=None, **_kw):
            path = Path(source)
            captured["source"] = source
            captured["is_file"] = path.is_file()
            captured["content"] = path.read_text(encoding="utf-8") if path.is_file() else None
            return RememberResult(document_id="doc-raw-text", committed=1, blocks_total=1)

    monkeypatch.setattr("okto_neuron.server.http.companion_for", lambda vault: _FakeCompanion())

    vault = Vault.init(tmp_path / "active", packs=["core"])
    state = ServerState(vault=vault, vault_path=Path(vault.path).resolve())
    state.vault_pool.adopt(vault, state.vault_path)
    server = runtime._build_mcp_server(state)
    try:
        async with Client(server) as client:
            result = await client.call_tool("remember", {"source": raw_text})

        data = result.structured_content or {}
        assert data.get("document_id") == "doc-raw-text"
        assert captured["is_file"] is True
        assert captured["content"] == raw_text
    finally:
        state.close()


def test_materialize_raw_text_source_collision_reminted_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defect 17: the 8-hex ``safe_source_filename`` hash is short enough to
    collide across unrelated pastes. On a collision with DIFFERENT prior
    content, the second write must land in a distinct file (full-hash
    suffix) rather than silently overwriting the first source's bytes — that
    would corrupt its byte-anchored provenance. An identical-content re-paste
    stays idempotent and reuses the same file, no duplicate."""
    import okto_neuron.server._ingest_queue as iq_mod
    from okto_neuron import Vault

    monkeypatch.setattr(iq_mod, "safe_source_filename", lambda raw, content: "collide.md")

    vault = Vault.init(tmp_path / "active", packs=["core"])
    sources_dir = Path(vault.path) / ".marginalia" / "sources"
    try:
        first_text = "first distinct raw text body, not path shaped"
        second_text = "second, different raw text body, not path shaped"

        first = runtime._materialize_raw_text_source(vault, first_text)
        assert Path(first).name == "collide.md"
        assert Path(first).read_text(encoding="utf-8") == first_text

        second = runtime._materialize_raw_text_source(vault, second_text)
        assert second != first, "differing content must not overwrite the collision target"
        assert Path(second).read_text(encoding="utf-8") == second_text
        # First file is untouched by the second, differing-content write.
        assert Path(first).read_text(encoding="utf-8") == first_text

        # Identical re-paste of the second content reuses the same file.
        third = runtime._materialize_raw_text_source(vault, second_text)
        assert third == second
        assert len(list(sources_dir.glob("*.md"))) == 2
    finally:
        vault.close()
