"""Regression coverage for the MCP tool surface in ``server/runtime.py``.

Targets deep-review findings 3.11, 3.21, 3.24, 3.29, 3.30, and 3.31 — all
scoped to ``src/okto_neuron/server/runtime.py``'s ``ask``/``explore``/
``remember`` MCP tools and the ``_mcp_kg_add_allowed``/``_mcp_request_selector``
sensitive-write gate helpers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import okto_neuron.server.runtime as runtime
from okto_neuron.server.state import ServerState
from okto_neuron.vault import Vault


def _new_vault(tmp_path: Path, name: str) -> tuple[Vault, Path]:
    vault = Vault.init(tmp_path / name, packs=["core"])
    return vault, Path(vault.path).resolve(strict=False)


# --------------------------------------------------------------------------
# 3.31 — the MCP sensitive-write gate must not fail OPEN on an unexpected
# exception type; only the intended "no HTTP context" RuntimeError should be
# swallowed.
# --------------------------------------------------------------------------


def test_kg_add_allowed_propagates_unexpected_exception(monkeypatch):
    def _raise():
        raise ValueError("unexpected boom")

    monkeypatch.setattr("fastmcp.server.dependencies.get_http_request", _raise)
    with pytest.raises(ValueError, match="unexpected boom"):
        runtime._mcp_kg_add_allowed()


def test_kg_add_allowed_still_treats_no_context_as_local(monkeypatch):
    def _raise():
        raise RuntimeError("No active HTTP request found.")

    monkeypatch.setattr("fastmcp.server.dependencies.get_http_request", _raise)
    assert runtime._mcp_kg_add_allowed() is True


def test_request_selector_propagates_unexpected_exception(monkeypatch):
    def _raise():
        raise ValueError("unexpected boom")

    monkeypatch.setattr("fastmcp.server.dependencies.get_http_request", _raise)
    with pytest.raises(ValueError, match="unexpected boom"):
        runtime._mcp_request_selector()


# --------------------------------------------------------------------------
# 3.29 — a pool-level fence (reembed/maintenance) surfaced through ask/explore
# must read as friendly "maintenance: ..." text, not the raw internal
# "vault_fenced: ..." pool message.
# --------------------------------------------------------------------------


def test_ask_reports_friendly_maintenance_message_when_pool_fenced(
    tmp_path: Path,
):
    from fastmcp import Client

    vault, path = _new_vault(tmp_path, "fenced")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)
    state.runtime_for(path)
    # Fence the pool WITHOUT marking the runtime draining — this is the actual
    # mechanism a reembed job uses for its "vault_fenced" pool error, and reads
    # deliberately must not short-circuit on ``runtime.draining`` alone (a
    # sibling pinned test asserts reads survive plain maintenance-draining).
    state.vault_pool.fence(path)
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            with pytest.raises(Exception) as exc_info:
                await client.call_tool("ask", {"question": "what?"})
            return exc_info

    import asyncio

    try:
        exc_info = asyncio.run(exercise())
        message = str(exc_info.value)
        assert "maintenance:" in message
        assert "vault_fenced" not in message
    finally:
        state.vault_pool.unfence(path)
        state.close()


# --------------------------------------------------------------------------
# 3.30 — ``ask``/``explore``'s ``k`` must be capped at the same MAX_QUERY_K
# REST enforces, not left unbounded.
# --------------------------------------------------------------------------


def test_ask_caps_k_at_max_query_k(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastmcp import Client

    from okto_neuron.companion import Answer
    from okto_neuron.server import http as http_module

    vault, path = _new_vault(tmp_path, "ask-k-cap")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)
    observed: dict[str, object] = {}

    class FakeCompanion:
        def ask(self, question, *, k, retrieval_policy=None):
            observed["k"] = k
            return Answer(
                text="ok", citations=("claim:1",), retrieval={"mode": "block", "seed_k": k}
            )

    monkeypatch.setattr(http_module, "companion_for", lambda _vault: FakeCompanion())
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            await client.call_tool("ask", {"question": "what?", "k": 10_000})

    try:
        import asyncio

        asyncio.run(exercise())
        assert observed["k"] == http_module.MAX_QUERY_K
    finally:
        state.close()


def test_explore_caps_k_at_max_query_k(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastmcp import Client

    from okto_neuron.server import http as http_module

    vault, path = _new_vault(tmp_path, "explore-k-cap")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)
    observed: dict[str, object] = {}

    class FakeCompanion:
        def explore(self, _topic, *, node_id=None, hops=1, k=12, **_kwargs):
            observed["k"] = k
            return {"nodes": [], "relationships": [], "claims": []}

    monkeypatch.setattr(http_module, "companion_for", lambda _vault: FakeCompanion())
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            await client.call_tool("explore", {"topic": "x", "k": 10_000})

    try:
        import asyncio

        asyncio.run(exercise())
        assert observed["k"] == http_module.MAX_QUERY_K
    finally:
        state.close()


# --------------------------------------------------------------------------
# 3.11 — ``ask`` must surface the effective ``enable_subgraph``/``hops`` in
# its ``retrieval`` response block instead of silently no-op'ing ``hops``.
# --------------------------------------------------------------------------


def test_ask_retrieval_block_reports_hops_noop_in_block_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from fastmcp import Client

    from okto_neuron.companion import Answer
    from okto_neuron.server import http as http_module

    vault, path = _new_vault(tmp_path, "ask-hops-block")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)

    class FakeCompanion:
        def ask(self, _question, *, k, retrieval_policy=None):
            # Default (non-subgraph) retrieval never puts "hops" in the trace.
            return Answer(text="ok", citations=(), retrieval={"mode": "block", "seed_k": k})

    monkeypatch.setattr(http_module, "companion_for", lambda _vault: FakeCompanion())
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            result = await client.call_tool("ask", {"question": "what?", "hops": 3})
            return result.structured_content or {}

    try:
        import asyncio

        payload = asyncio.run(exercise())
        retrieval = payload["retrieval"]
        assert retrieval["enable_subgraph"] is False
        # hops=3 was requested but never had any effect under block-mode
        # retrieval — the response must say so rather than silently agreeing.
        assert retrieval["hops"] is None
    finally:
        state.close()


def test_ask_retrieval_block_reports_effective_hops_in_subgraph_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from fastmcp import Client

    from okto_neuron.companion import Answer
    from okto_neuron.server import http as http_module

    vault, path = _new_vault(tmp_path, "ask-hops-subgraph")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)

    class FakeCompanion:
        def ask(self, _question, *, k, retrieval_policy=None):
            return Answer(
                text="ok",
                citations=(),
                retrieval={"mode": "subgraph", "seed_k": k, "hops": 3},
            )

    monkeypatch.setattr(http_module, "companion_for", lambda _vault: FakeCompanion())
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            result = await client.call_tool("ask", {"question": "what?", "hops": 3})
            return result.structured_content or {}

    try:
        import asyncio

        payload = asyncio.run(exercise())
        retrieval = payload["retrieval"]
        assert retrieval["enable_subgraph"] is True
        assert retrieval["hops"] == 3
    finally:
        state.close()


# --------------------------------------------------------------------------
# 3.19 — subgraph-mode answers ground ``text`` in a wider ego-graph than
# ``citations`` (the retrieval seeds) alone represents. ``Answer`` carries
# the full grounding pool as ``subgraph_evidence_ids``; the MCP ``ask``
# response must surface it too, not just ``citations``.
# --------------------------------------------------------------------------


def test_ask_surfaces_subgraph_evidence_ids_in_mcp_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from fastmcp import Client

    from okto_neuron.companion import Answer
    from okto_neuron.server import http as http_module

    vault, path = _new_vault(tmp_path, "ask-subgraph-evidence")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)

    ids = ("claim:seed-1", "claim:neighbour-2")

    class FakeCompanion:
        def ask(self, _question, *, k, retrieval_policy=None):
            return Answer(
                text="ok",
                # citations are only the retrieval seeds ...
                citations=("claim:seed-1",),
                # ... while subgraph_evidence_ids is the full ego-graph pool the
                # model was actually instructed to cite from, including a
                # 1-hop neighbour that never made it into citations. Mirror
                # Companion.ask's real trace, which already nests this same
                # pool inside retrieval (companion/__init__.py ~7314) — the
                # gap this test targets is that, pre-fix, it was reachable
                # only as an undocumented nested `retrieval` key, not as a
                # documented top-level response field.
                subgraph_evidence_ids=ids,
                retrieval={"mode": "subgraph", "seed_k": k, "subgraph_evidence_ids": ids},
            )

    monkeypatch.setattr(http_module, "companion_for", lambda _vault: FakeCompanion())
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            result = await client.call_tool("ask", {"question": "what?"})
            return result.structured_content or {}

    try:
        import asyncio

        payload = asyncio.run(exercise())
        assert payload["citations"] == ["claim:seed-1"]
        # Now a documented top-level field, not just a nested one buried in
        # `retrieval` a caller would have to know to look for.
        assert "subgraph_evidence_ids" in payload
        assert payload["subgraph_evidence_ids"] == list(ids)
        assert "claim:neighbour-2" not in payload["citations"]
    finally:
        state.close()


def test_ask_block_mode_reports_empty_subgraph_evidence_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from fastmcp import Client

    from okto_neuron.companion import Answer
    from okto_neuron.server import http as http_module

    vault, path = _new_vault(tmp_path, "ask-subgraph-evidence-block")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)

    class FakeCompanion:
        def ask(self, _question, *, k, retrieval_policy=None):
            return Answer(
                text="ok",
                citations=("claim:seed-1",),
                retrieval={"mode": "block", "seed_k": k},
            )

    monkeypatch.setattr(http_module, "companion_for", lambda _vault: FakeCompanion())
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            result = await client.call_tool("ask", {"question": "what?"})
            return result.structured_content or {}

    try:
        import asyncio

        payload = asyncio.run(exercise())
        assert payload["subgraph_evidence_ids"] == []
    finally:
        state.close()


# --------------------------------------------------------------------------
# 3.21 — ``remember``'s ``sensitivity`` must be a real enum in the tool
# schema; a typo must be rejected, not silently coerced to "default".
# --------------------------------------------------------------------------


def test_remember_rejects_invalid_sensitivity_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from fastmcp import Client

    from okto_neuron.server import http as http_module

    vault, path = _new_vault(tmp_path, "sensitivity")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)

    class FakeCompanion:
        def remember(self, _source, *, sensitivity, on_progress=None, **_kw):
            pytest.fail("remember must not run when sensitivity fails schema validation")

    monkeypatch.setattr(http_module, "companion_for", lambda _vault: FakeCompanion())
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            with pytest.raises(Exception):
                await client.call_tool(
                    "remember",
                    {"source": "raw note", "sensitivity": "local-only"},  # typo: hyphen
                )

    try:
        import asyncio

        asyncio.run(exercise())
    finally:
        state.close()


def test_remember_accepts_exact_local_only_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastmcp import Client

    from okto_neuron.companion import RememberResult
    from okto_neuron.server import http as http_module

    vault, path = _new_vault(tmp_path, "sensitivity-ok")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)
    observed: dict[str, object] = {}

    class FakeCompanion:
        def remember(self, _source, *, sensitivity, on_progress=None, **_kw):
            observed["sensitivity"] = sensitivity
            return RememberResult(document_id="d1", committed=1, blocks_total=1)

    monkeypatch.setattr(http_module, "companion_for", lambda _vault: FakeCompanion())
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            await client.call_tool("remember", {"source": "raw note", "sensitivity": "local_only"})

    try:
        import asyncio

        asyncio.run(exercise())
        assert observed["sensitivity"] == "local_only"
    finally:
        state.close()


# --------------------------------------------------------------------------
# 3.24 — MCP ``remember`` must normalize ``SourceOutsideVaultError``/
# ``IngestError`` the way REST's ``/remember`` does, instead of leaking the
# raw exception type/message.
# --------------------------------------------------------------------------


def test_remember_normalizes_source_outside_vault_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from fastmcp import Client

    from okto_neuron.companion import SourceOutsideVaultError
    from okto_neuron.server import http as http_module

    vault, path = _new_vault(tmp_path, "outside-vault")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)

    class FakeCompanion:
        def remember(self, _source, *, sensitivity, on_progress=None, **_kw):
            raise SourceOutsideVaultError("escaped the vault root")

    monkeypatch.setattr(http_module, "companion_for", lambda _vault: FakeCompanion())
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            with pytest.raises(Exception) as exc_info:
                await client.call_tool("remember", {"source": "raw note"})
            return exc_info

    try:
        import asyncio

        exc_info = asyncio.run(exercise())
        assert "forbidden:" in str(exc_info.value)
    finally:
        state.close()


def test_remember_normalizes_ingest_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from fastmcp import Client

    from okto_neuron.errors import IngestError
    from okto_neuron.server import http as http_module

    vault, path = _new_vault(tmp_path, "ingest-error")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)

    class FakeCompanion:
        def remember(self, _source, *, sensitivity, on_progress=None, **_kw):
            raise IngestError("ladybug write blew up")

    monkeypatch.setattr(http_module, "companion_for", lambda _vault: FakeCompanion())
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            with pytest.raises(Exception) as exc_info:
                await client.call_tool("remember", {"source": "raw note"})
            return exc_info

    try:
        import asyncio

        exc_info = asyncio.run(exercise())
        assert "ladybug_write_failed:" in str(exc_info.value)
    finally:
        state.close()


# --------------------------------------------------------------------------
# serverInfo must report marginalia's version, not FastMCP's. Omitting
# ``version=`` on the FastMCP constructor makes the framework substitute its
# own package version into the initialize handshake.
# --------------------------------------------------------------------------


def test_mcp_server_info_reports_marginalia_version(tmp_path):
    import asyncio

    from fastmcp import Client

    from okto_neuron import __version__ as marginalia_version

    vault, path = _new_vault(tmp_path, "versioned")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            return client.initialize_result.serverInfo

    info = asyncio.run(exercise())
    assert info.name == "okto-neuron"
    assert info.version == marginalia_version


# --------------------------------------------------------------------------
# Per-call vault selection: ``list_vaults`` (names only, never paths) plus the
# optional ``vault=`` argument on ask/explore/remember.
# --------------------------------------------------------------------------


@pytest.fixture
def registry_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the global vault root to a tmp HOME so named lookups are isolated."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.delenv("OKTO_NEURON_VAULT", raising=False)
    return home / ".okto-neuron" / "vaults"


def test_list_vaults_returns_names_and_never_paths(registry_home: Path):
    import asyncio

    from fastmcp import Client

    alpha = Vault.init(registry_home / "alpha", packs=["core"])
    Vault.init(registry_home / "beta", packs=["core"]).close()
    path = Path(alpha.path).resolve(strict=False)
    state = ServerState(vault=alpha, vault_path=path, multi_vault_runtime_enabled=True)
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            result = await client.call_tool("list_vaults", {})
            return result.data

    try:
        data = asyncio.run(exercise())
        vaults = data["vaults"]
        assert [entry["name"] for entry in vaults] == ["alpha", "beta"]
        for entry in vaults:
            assert set(entry) == {"name", "current", "backend"}
            assert "path" not in entry
            assert "id" not in entry
        assert [entry["current"] for entry in vaults] == [True, False]
    finally:
        state.close()


def test_per_call_vault_argument_selects_another_vault_for_reads(
    registry_home: Path, monkeypatch: pytest.MonkeyPatch
):
    import asyncio

    from fastmcp import Client

    from okto_neuron.companion import Answer
    from okto_neuron.server import http as http_module

    alpha = Vault.init(registry_home / "alpha", packs=["core"])
    Vault.init(registry_home / "beta", packs=["core"]).close()
    path = Path(alpha.path).resolve(strict=False)
    state = ServerState(vault=alpha, vault_path=path, multi_vault_runtime_enabled=True)
    seen: dict[str, object] = {}

    class FakeCompanion:
        def __init__(self, vault):
            self._vault = vault

        def ask(self, _question, *, k, retrieval_policy=None):
            seen["ask"] = Path(self._vault.path).resolve(strict=False)
            return Answer(text="ok", citations=(), retrieval={"mode": "block", "seed_k": k})

        def explore(self, _topic, *, node_id=None, hops=1, k=12, **_kwargs):
            seen["explore"] = Path(self._vault.path).resolve(strict=False)
            return {"nodes": [], "relationships": [], "claims": []}

    monkeypatch.setattr(http_module, "companion_for", FakeCompanion)
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            await client.call_tool("ask", {"question": "what?", "vault": "beta"})
            await client.call_tool("explore", {"topic": "x", "vault": "beta"})

    try:
        asyncio.run(exercise())
        expected = (registry_home / "beta").resolve(strict=False)
        assert seen["ask"] == expected
        assert seen["explore"] == expected
    finally:
        state.close()


def test_per_call_vault_argument_selects_another_vault_for_remember(
    registry_home: Path, monkeypatch: pytest.MonkeyPatch
):
    import asyncio

    from fastmcp import Client

    from okto_neuron.server import http as http_module

    alpha = Vault.init(registry_home / "alpha", packs=["core"])
    Vault.init(registry_home / "beta", packs=["core"]).close()
    path = Path(alpha.path).resolve(strict=False)
    state = ServerState(vault=alpha, vault_path=path, multi_vault_runtime_enabled=True)
    seen: dict[str, object] = {}

    class FakeResult:
        document_id = "doc:1"
        committed = True
        queued = False
        blocks_total = 1
        nodes_extracted = 0
        edges_extracted = 0
        claims_minted = 0
        provider_error = None
        provider_failures = 0
        empty_after_retry_blocks = 0
        llm_disabled = False
        outcomes: list = []
        outcome: dict = {}

    class FakeCompanion:
        def __init__(self, vault):
            self._vault = vault

        def remember(self, _source, *, sensitivity, on_progress=None, **_kw):
            seen["remember"] = Path(self._vault.path).resolve(strict=False)
            return FakeResult()

    monkeypatch.setattr(http_module, "companion_for", FakeCompanion)
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            await client.call_tool("remember", {"source": "a note\nacross lines", "vault": "beta"})

    try:
        asyncio.run(exercise())
        assert seen["remember"] == (registry_home / "beta").resolve(strict=False)
    finally:
        state.close()


def test_unknown_per_call_vault_fails_loudly_and_connection_still_works(
    registry_home: Path, monkeypatch: pytest.MonkeyPatch
):
    import asyncio

    from fastmcp import Client

    from okto_neuron.companion import Answer
    from okto_neuron.server import http as http_module

    alpha = Vault.init(registry_home / "alpha", packs=["core"])
    path = Path(alpha.path).resolve(strict=False)
    state = ServerState(vault=alpha, vault_path=path, multi_vault_runtime_enabled=True)

    class FakeCompanion:
        def __init__(self, vault):
            self._vault = vault

        def ask(self, _question, *, k, retrieval_policy=None):
            return Answer(text="ok", citations=(), retrieval={"mode": "block", "seed_k": k})

    monkeypatch.setattr(http_module, "companion_for", FakeCompanion)
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            with pytest.raises(Exception) as exc_info:
                await client.call_tool("ask", {"question": "q", "vault": "nope"})
            message = str(exc_info.value)
            # The bad selector must not poison the connection or the runtime map.
            good = await client.call_tool("ask", {"question": "q"})
            return message, good.data

    try:
        message, good = asyncio.run(exercise())
        assert "unknown_vault:" in message
        assert good["text"] == "ok"
    finally:
        state.close()


def test_path_shaped_per_call_vault_is_forbidden(
    registry_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import asyncio

    from fastmcp import Client

    alpha = Vault.init(registry_home / "alpha", packs=["core"])
    outside = tmp_path / "outside"
    Vault.init(outside, packs=["core"]).close()
    path = Path(alpha.path).resolve(strict=False)
    state = ServerState(vault=alpha, vault_path=path, multi_vault_runtime_enabled=True)
    server = runtime._build_mcp_server(state)

    async def exercise(selector: str):
        async with Client(server) as client:
            with pytest.raises(Exception) as exc_info:
                await client.call_tool("ask", {"question": "q", "vault": selector})
            return str(exc_info.value)

    try:
        for selector in (str(outside), "~/vaults/alpha", "sub/alpha"):
            assert "forbidden_path_selector:" in asyncio.run(exercise(selector))
    finally:
        state.close()


# --------------------------------------------------------------------------
# Flattened retrieval-policy params on MCP ``ask`` — parity with the web UI's
# query controls. Every knob defaults to None = inherit, and the default
# behaviour (subgraph OFF) must be byte-identical to before.
# --------------------------------------------------------------------------


def _policy_capture_state(tmp_path: Path, name: str, monkeypatch, *, mode: str = "block"):
    from okto_neuron.companion import Answer
    from okto_neuron.server import http as http_module

    vault, path = _new_vault(tmp_path, name)
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)
    seen: dict[str, object] = {}

    class FakeCompanion:
        def ask(self, _question, *, k, retrieval_policy=None):
            seen["policy"] = retrieval_policy
            return Answer(text="ok", citations=(), retrieval={"mode": mode, "seed_k": k})

    monkeypatch.setattr(http_module, "companion_for", lambda _vault: FakeCompanion())
    return state, seen


def _call_ask(state, args: dict) -> dict:
    import asyncio

    from fastmcp import Client

    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            result = await client.call_tool("ask", args)
            return result.structured_content or {}

    return asyncio.run(exercise())


def test_ask_policy_params_reach_the_retrieval_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state, seen = _policy_capture_state(tmp_path, "ask-policy", monkeypatch)
    try:
        _call_ask(
            state,
            {
                "question": "q",
                "enable_subgraph": True,
                "source_block_policy": "always",
                "seed_k": 7,
                "max_degree_per_seed": 3,
                "neighbour_budget_tokens": 1234,
                "source_block_budget_tokens": 4321,
                "coverage_threshold": 0.25,
                "min_claim_confidence": 0.5,
                "max_nodes": 11,
                "max_relationships": 12,
                "max_claims": 13,
                "relationship_types": ["founded", "visited"],
            },
        )
        policy = seen["policy"]
        assert policy.enable_subgraph is True
        assert policy.source_block_policy == "always"
        assert policy.seed_k == 7
        assert policy.max_degree_per_seed == 3
        assert policy.neighbour_budget_tokens == 1234
        assert policy.source_block_budget_tokens == 4321
        assert policy.coverage_threshold == 0.25
        assert policy.min_claim_confidence == 0.5
        assert policy.max_nodes == 11
        assert policy.max_relationships == 12
        assert policy.max_claims == 13
        assert policy.relationship_types == ("founded", "visited")
    finally:
        state.close()


def test_ask_unset_policy_params_mean_inherit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The assembled policy with no new knobs passed must be field-for-field
    identical to the pre-existing ``AskRetrievalPolicy(hops=...)`` — a None
    never clobbers a vault/config default with a hardcoded value."""
    from okto_neuron.companion import AskRetrievalPolicy

    state, seen = _policy_capture_state(tmp_path, "ask-inherit", monkeypatch)
    try:
        _call_ask(state, {"question": "q", "hops": 2})
        assert seen["policy"] == AskRetrievalPolicy(hops=2)
    finally:
        state.close()


def test_ask_enable_subgraph_default_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state, seen = _policy_capture_state(tmp_path, "ask-subgraph-default", monkeypatch)
    try:
        payload = _call_ask(state, {"question": "q", "hops": 3})
        # Not forced on: the vault config still decides.
        assert seen["policy"].enable_subgraph is None
        # Honest reporting: hops is null while the block path ignores it.
        assert payload["retrieval"]["enable_subgraph"] is False
        assert payload["retrieval"]["hops"] is None
    finally:
        state.close()


def test_ask_invalid_policy_value_is_a_clean_tool_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state, _seen = _policy_capture_state(tmp_path, "ask-policy-invalid", monkeypatch)
    try:
        # ``coverage_threshold`` is le=1.0 — a range violation clears the FastMCP
        # schema layer and reaches our AskRetrievalPolicy construction.
        with pytest.raises(Exception) as exc_info:
            _call_ask(state, {"question": "q", "coverage_threshold": 1.5})
        message = str(exc_info.value)
        assert "invalid retrieval policy: coverage_threshold" in message
        assert "Traceback" not in message
    finally:
        state.close()


# --------------------------------------------------------------------------
# ``include_sources`` — per-hit provenance, VAULT-RELATIVE path only. ask has
# no loopback gate, so an absolute path here would leak the filesystem layout
# to a remote caller under --allow-remote.
# --------------------------------------------------------------------------


def _hit_state(tmp_path: Path, name: str, monkeypatch, *, source: Path | None = None):
    from okto_neuron.companion import Answer
    from okto_neuron.models import Node, Provenance, QueryHit
    from okto_neuron.server import http as http_module

    vault, path = _new_vault(tmp_path, name)
    source = source if source is not None else path / "notes" / "deep.md"
    hit = QueryHit(
        node=Node(id="a" * 64, type="Claim", name="t"),
        score=0.5,
        provenance=Provenance(
            path=str(source),
            byte_start=10,
            byte_end=42,
            content_hash="sha256:" + "0" * 64,
            extraction_activity_id="act:1",
            agent_id="agent:1",
            document_id="doc:1",
            block_id="b" * 64,
        ),
    )
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)

    class FakeCompanion:
        def ask(self, _question, *, k, retrieval_policy=None):
            return Answer(
                text="ok",
                citations=("a" * 64,),
                hits=(hit,),
                retrieval={"mode": "block", "seed_k": k},
            )

    monkeypatch.setattr(http_module, "companion_for", lambda _vault: FakeCompanion())
    return state, path


def test_ask_include_sources_false_leaves_payload_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state, _path = _hit_state(tmp_path, "sources-off", monkeypatch)
    try:
        payload = _call_ask(state, {"question": "q"})
        assert "sources" not in payload
        assert set(payload) == {"status", "text", "citations", "subgraph_evidence_ids", "retrieval"}
    finally:
        state.close()


def test_ask_include_sources_emits_relative_path_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state, path = _hit_state(tmp_path, "sources-on", monkeypatch)
    try:
        payload = _call_ask(state, {"question": "q", "include_sources": True})
        sources = payload["sources"]
        assert len(sources) == 1
        entry = sources[0]
        assert entry["path"] == "notes/deep.md"
        assert entry["block_id"] == "b" * 64
        assert entry["byte_start"] == 10
        assert entry["byte_end"] == 42
        assert entry["content_hash"] == "sha256:" + "0" * 64
        # No absolute path and no vault-root string anywhere in the payload.
        blob = repr(payload)
        assert str(path) not in blob
        assert str(path.resolve(strict=False)) not in blob
        assert all(not str(value).startswith("/") for value in entry.values())
    finally:
        state.close()


def test_ask_include_sources_omits_path_when_outside_the_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A hit can legitimately anchor outside the vault root (watch_roots), where
    ``_vault_relative`` returns None. The path key must then be OMITTED — never
    fall back to the absolute path, which is the whole point of the gate."""
    outside = tmp_path / "elsewhere" / "note.md"
    state, path = _hit_state(tmp_path, "sources-outside", monkeypatch, source=outside)
    try:
        payload = _call_ask(state, {"question": "q", "include_sources": True})
        entry = payload["sources"][0]
        assert "path" not in entry
        assert entry["block_id"] == "b" * 64
        assert entry["byte_start"] == 10
        assert entry["content_hash"] == "sha256:" + "0" * 64
        blob = repr(payload)
        assert str(outside) not in blob
        assert str(path) not in blob
        assert all(not str(value).startswith("/") for value in entry.values())
    finally:
        state.close()


def test_ask_falsy_policy_values_are_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``is not None`` filtering, not truthiness: ``enable_subgraph=False`` is a
    caller forcing block mode on a subgraph-enabled vault, and ``0`` caps are
    meaningful values, so neither may be silently dropped into "inherit"."""
    state, seen = _policy_capture_state(tmp_path, "ask-falsy", monkeypatch)
    try:
        _call_ask(
            state,
            {
                "question": "q",
                "enable_subgraph": False,
                "max_relationships": 0,
                "max_claims": 0,
                "min_claim_confidence": 0.0,
            },
        )
        policy = seen["policy"]
        assert policy.enable_subgraph is False
        assert policy.max_relationships == 0
        assert policy.max_claims == 0
        assert policy.min_claim_confidence == 0.0
    finally:
        state.close()


def test_validity_subset_surfaces_superseded_and_valid_until() -> None:
    """The ADR 0024 validity subset shared by REST ``_serialize_hit`` and the MCP
    ``include_sources`` entries. NOTE: ``Vault.query`` builds hits via
    ``_public_node``, and ``okto_neuron.models.Node`` has no ``facets`` field, so
    today NEITHER surface can reach a facet-bearing hit node — this pins the
    shared helper's contract so both stay in step if that changes."""
    from types import SimpleNamespace

    from okto_neuron.server.http import validity_subset

    assert validity_subset(SimpleNamespace(facets={})) == {}
    assert validity_subset(
        SimpleNamespace(facets={"_superseded": True, "valid_until": "2025-01-01"})
    ) == {"superseded": True, "valid_until": "2025-01-01"}
    assert validity_subset(
        SimpleNamespace(facets={"_detached": True, "valid_as_of": "2024-06-01"})
    ) == {"detached": True, "valid_as_of": "2024-06-01"}


def test_ask_include_sources_merges_the_validity_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``include_sources`` entries must carry the same staleness keys REST emits
    — asserted by pinning the shared helper, since no real hit node can carry
    facets today (see the note above)."""
    from okto_neuron.server import http as http_module

    state, _path = _hit_state(tmp_path, "sources-stale", monkeypatch)
    monkeypatch.setattr(
        http_module,
        "validity_subset",
        lambda _node: {"superseded": True, "valid_until": "2025-01-01"},
    )
    try:
        entry = _call_ask(state, {"question": "q", "include_sources": True})["sources"][0]
        assert entry["superseded"] is True
        assert entry["valid_until"] == "2025-01-01"
    finally:
        state.close()


# --------------------------------------------------------------------------
# ``explore`` graph-control pass-throughs reach the companion.
# --------------------------------------------------------------------------


def test_explore_passes_graph_controls_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import asyncio

    from fastmcp import Client

    from okto_neuron.server import http as http_module

    vault, path = _new_vault(tmp_path, "explore-controls")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)
    seen: dict[str, object] = {}

    class FakeCompanion:
        def explore(self, _topic, **kwargs):
            seen.update(kwargs)
            return {"seeds": [], "hops": 1, "nodes": [], "relationships": [], "claims": []}

    monkeypatch.setattr(http_module, "companion_for", lambda _vault: FakeCompanion())
    server = runtime._build_mcp_server(state)

    async def exercise(args):
        async with Client(server) as client:
            await client.call_tool("explore", args)

    try:
        asyncio.run(
            exercise(
                {
                    "topic": "x",
                    "relationship_types": ["founded"],
                    "min_claim_confidence": 0.4,
                    "max_degree_per_seed": 2,
                }
            )
        )
        assert seen["relationship_types"] == ("founded",)
        assert seen["min_claim_confidence"] == 0.4
        assert seen["max_degree_per_seed"] == 2

        seen.clear()
        asyncio.run(exercise({"topic": "x"}))
        assert seen["relationship_types"] is None
        assert seen["min_claim_confidence"] is None
        assert seen["max_degree_per_seed"] is None
    finally:
        state.close()


# --------------------------------------------------------------------------
# ask synthesis honesty — ``retrieval["synthesis_status"]`` must survive to the
# MCP payload. An empty ``text`` with ``synthesis_status == "provider_error"``
# means the model was unreachable, NOT that the graph lacks the answer; the
# agent can only act on that if the field actually reaches it.
# --------------------------------------------------------------------------


def test_ask_tool_payload_carries_synthesis_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from fastmcp import Client

    from okto_neuron.companion import Answer
    from okto_neuron.server import http as http_module

    vault, path = _new_vault(tmp_path, "ask-synthesis-status")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)

    class FakeCompanion:
        def ask(self, question, *, k, retrieval_policy=None):
            return Answer(
                text="",
                citations=("claim:1",),
                retrieval={
                    "mode": "block",
                    "seed_k": k,
                    "synthesis_status": "provider_error",
                    "provider_error": "litellm completion failed for openai/llama-3: HTTP 500",
                },
            )

    monkeypatch.setattr(http_module, "companion_for", lambda _vault: FakeCompanion())
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            result = await client.call_tool("ask", {"question": "what?"})
            return result.data

    try:
        import asyncio

        payload = asyncio.run(exercise())
        assert payload["text"] == ""
        retrieval = payload["retrieval"]
        assert retrieval["synthesis_status"] == "provider_error"
        assert "HTTP 500" in retrieval["provider_error"]
        assert payload["status"] == "degraded"
    finally:
        state.close()


def test_ask_tool_without_llm_is_degraded_and_calls_no_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Real companion, vault on the built-in defaults (empty model): the MCP
    ask answers ``status: degraded`` / ``no_llm`` and never builds a provider."""
    from fastmcp import Client

    import okto_neuron.llm as llm_mod

    def _no_call(*_args, **_kwargs):
        raise AssertionError("ask built an LLM provider with no model configured")

    monkeypatch.setattr(llm_mod, "get_provider", _no_call)
    vault, path = _new_vault(tmp_path, "ask-no-llm")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            result = await client.call_tool("ask", {"question": "what?"})
            return result.data

    try:
        import asyncio

        payload = asyncio.run(exercise())
        assert payload["status"] == "degraded"
        assert payload["text"] == ""
        assert payload["retrieval"]["synthesis_status"] == "no_llm"
        assert payload["retrieval"]["no_llm_reason"]
    finally:
        state.close()


def test_ask_tool_payload_carries_truncated_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A completion cut off by the token budget must reach the agent as
    ``truncated`` with the finish reason attached — otherwise an incomplete
    answer reads as a complete one."""
    from fastmcp import Client

    from okto_neuron.companion import Answer
    from okto_neuron.server import http as http_module

    vault, path = _new_vault(tmp_path, "ask-truncated-status")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)

    class FakeCompanion:
        def ask(self, question, *, k, retrieval_policy=None):
            return Answer(
                text="Alice founded Acme in",
                citations=("claim:1",),
                retrieval={
                    "mode": "block",
                    "seed_k": k,
                    "synthesis_status": "truncated",
                    "finish_reason": "length",
                },
            )

    monkeypatch.setattr(http_module, "companion_for", lambda _vault: FakeCompanion())
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            result = await client.call_tool("ask", {"question": "what?"})
            return result.data

    try:
        import asyncio

        payload = asyncio.run(exercise())
        retrieval = payload["retrieval"]
        assert retrieval["synthesis_status"] == "truncated"
        assert retrieval["finish_reason"] == "length"
    finally:
        state.close()


# --------------------------------------------------------------------------
# Live findings (2026-09-17): no payload told a caller which vault answered,
# ``list_vaults`` reported current=false for the connection's own pinned vault,
# and ``explore`` had no auditable ``retrieval`` block at all.
# --------------------------------------------------------------------------


def _pin_connection(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """Simulate a connection pinned via ``?vault=<name>``.

    The in-memory FastMCP client has no HTTP context, so ``_mcp_request_selector``
    would otherwise always report ``(None, True)``.
    """
    monkeypatch.setattr(runtime, "_mcp_request_selector", lambda: (name, True))


class _FakeCompanion:
    """Minimal companion double: records the vault it was built for."""

    seen: dict[str, object] = {}

    def __init__(self, vault):
        self._vault = vault

    def ask(self, _question, *, k, retrieval_policy=None):
        from okto_neuron.companion import Answer

        _FakeCompanion.seen["ask"] = Path(self._vault.path).resolve(strict=False)
        return Answer(text="ok", citations=(), retrieval={"mode": "block", "seed_k": k})


def test_list_vaults_current_reflects_the_connection_vault(
    registry_home: Path, monkeypatch: pytest.MonkeyPatch
):
    import asyncio

    from fastmcp import Client

    Vault.init(registry_home / "alpha", packs=["core"]).close()
    Vault.init(registry_home / "beta", packs=["core"]).close()
    # The daemon started with NO vault — exactly the live incident's shape.
    state = ServerState(vault=None, vault_path=None, multi_vault_runtime_enabled=True)
    _pin_connection(monkeypatch, "beta")
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            return (await client.call_tool("list_vaults", {})).data

    try:
        vaults = asyncio.run(exercise())["vaults"]
        current = {entry["name"]: entry["current"] for entry in vaults}
        assert current == {"alpha": False, "beta": True}
        for entry in vaults:
            assert set(entry) == {"name", "current", "backend"}
    finally:
        state.close()


def test_ask_retrieval_names_the_serving_vault_and_echoes_ignored_override(
    registry_home: Path, monkeypatch: pytest.MonkeyPatch
):
    import asyncio

    from fastmcp import Client

    from okto_neuron.server import http as http_module

    Vault.init(registry_home / "alpha", packs=["core"]).close()
    Vault.init(registry_home / "beta", packs=["core"]).close()
    state = ServerState(vault=None, vault_path=None, multi_vault_runtime_enabled=True)
    _FakeCompanion.seen = {}
    monkeypatch.setattr(http_module, "companion_for", _FakeCompanion)
    _pin_connection(monkeypatch, "alpha")
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            return (await client.call_tool("ask", {"question": "q?", "vault": "beta"})).data

    try:
        data = asyncio.run(exercise())
        retrieval = data["retrieval"]
        # Routed to the CONNECTION's vault, and says so.
        assert _FakeCompanion.seen["ask"] == (registry_home / "alpha").resolve(strict=False)
        assert retrieval["vault"] == "alpha"
        assert retrieval["vault_override_ignored"] == "beta"
    finally:
        state.close()


def test_ask_reports_no_ignored_override_when_none_was_supplied(
    registry_home: Path, monkeypatch: pytest.MonkeyPatch
):
    import asyncio

    from fastmcp import Client

    from okto_neuron.server import http as http_module

    Vault.init(registry_home / "alpha", packs=["core"]).close()
    state = ServerState(vault=None, vault_path=None, multi_vault_runtime_enabled=True)
    _FakeCompanion.seen = {}
    monkeypatch.setattr(http_module, "companion_for", _FakeCompanion)
    _pin_connection(monkeypatch, "alpha")
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            return (await client.call_tool("ask", {"question": "q?"})).data

    try:
        retrieval = asyncio.run(exercise())["retrieval"]
        assert retrieval["vault"] == "alpha"
        assert "vault_override_ignored" not in retrieval
    finally:
        state.close()


def test_explore_returns_a_retrieval_block_with_documented_keys(
    registry_home: Path, monkeypatch: pytest.MonkeyPatch
):
    import asyncio

    from fastmcp import Client

    Vault.init(registry_home / "alpha", packs=["core"]).close()
    state = ServerState(vault=None, vault_path=None, multi_vault_runtime_enabled=True)
    _pin_connection(monkeypatch, "alpha")
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            return (await client.call_tool("explore", {"topic": "anything"})).data

    try:
        data = asyncio.run(exercise())
        retrieval = data["retrieval"]
        assert set(retrieval) == {
            "mode",
            "seed_k",
            "hops",
            "max_degree_per_seed",
            "min_claim_confidence",
            "relationship_types",
            "vault",
        }
        # explore makes NO LLM call — an invented synthesis_status would be a lie.
        assert "synthesis_status" not in retrieval
        assert retrieval["mode"] == "topic"
        assert retrieval["vault"] == "alpha"
        # The pre-existing top-level contract is untouched.
        assert "seeds" in data and "hops" in data and "nodes" in data
    finally:
        state.close()


def test_explore_retrieval_reflects_caller_filters_over_config(
    registry_home: Path, monkeypatch: pytest.MonkeyPatch
):
    import asyncio

    from fastmcp import Client

    Vault.init(registry_home / "alpha", packs=["core"]).close()
    state = ServerState(vault=None, vault_path=None, multi_vault_runtime_enabled=True)
    _pin_connection(monkeypatch, "alpha")
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            return (
                await client.call_tool(
                    "explore",
                    {
                        "topic": "anything",
                        "hops": 3,
                        "k": 7,
                        "relationship_types": ["mentions"],
                        "min_claim_confidence": 0.75,
                        "max_degree_per_seed": 4,
                    },
                )
            ).data

    try:
        retrieval = asyncio.run(exercise())["retrieval"]
        assert retrieval["hops"] == 3
        assert retrieval["seed_k"] == 7
        assert retrieval["relationship_types"] == ["mentions"]
        assert retrieval["min_claim_confidence"] == 0.75
        assert retrieval["max_degree_per_seed"] == 4
    finally:
        state.close()


def test_explore_node_mode_reports_null_seed_k(
    registry_home: Path, monkeypatch: pytest.MonkeyPatch
):
    import asyncio

    from fastmcp import Client

    Vault.init(registry_home / "alpha", packs=["core"]).close()
    state = ServerState(vault=None, vault_path=None, multi_vault_runtime_enabled=True)
    _pin_connection(monkeypatch, "alpha")
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            return (await client.call_tool("explore", {"node_id": "concept:nope"})).data

    try:
        retrieval = asyncio.run(exercise())["retrieval"]
        assert retrieval["mode"] == "node"
        # ``k`` is unused in node mode; reporting it would misrepresent the call.
        assert retrieval["seed_k"] is None
    finally:
        state.close()


# --------------------------------------------------------------------------
# Path non-disclosure at the LEASE seam: a non-fenced ``VaultPoolError``
# carries the RESOLVED ABSOLUTE vault path in its message (pool messages are
# built as ``"...: {key}"`` — see ``server/_vault_pool.py``). ``_lease`` used
# to interpolate that verbatim as ``f"{exc.code}: {exc}"``, handing an MCP
# client the user's home directory and vault layout. It must now route through
# the same ``_pool_error`` sanitiser the resolution seam uses.
# --------------------------------------------------------------------------


def _pool_failure_message(state, path: Path, monkeypatch) -> str:
    """Drive ``ask`` with ``lease_vault`` raising a path-carrying pool error."""
    import asyncio

    from fastmcp import Client

    from okto_neuron.server._vault_pool import VaultPoolError
    from okto_neuron.server.state import VaultRuntime

    def _boom(self):
        raise VaultPoolError("open_failed", f"could not open vault at {path}: [Errno 13] denied")

    monkeypatch.setattr(VaultRuntime, "lease_vault", _boom)
    server = runtime._build_mcp_server(state)

    async def exercise():
        async with Client(server) as client:
            with pytest.raises(Exception) as exc_info:
                await client.call_tool("ask", {"question": "what?"})
            return str(exc_info.value)

    return asyncio.run(exercise())


def test_lease_pool_error_does_not_leak_vault_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
):
    import logging

    vault, path = _new_vault(tmp_path, "lease-leak")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)
    try:
        with caplog.at_level(logging.WARNING, logger=runtime._LOG.name):
            message = _pool_failure_message(state, path, monkeypatch)
    finally:
        state.close()

    # The stable machine code still prefixes the message, so MCP clients that
    # parse ``code: message`` keep working.
    assert message.startswith("open_failed: ") or "open_failed: " in message

    # ...but nothing filesystem-shaped survives into the client-facing text.
    assert str(path) not in message
    assert "/Users" not in message
    assert "/home/" not in message
    assert ".marginalia" not in message
    assert "vaults" not in message
    assert "Errno" not in message

    # The operator still gets the whole thing, path included, in the log.
    operator_log = "\n".join(record.getMessage() for record in caplog.records)
    assert str(path) in operator_log
    assert "open_failed" in operator_log


def test_lease_fenced_messages_are_unchanged(tmp_path: Path):
    """Pin the fenced wording so a future refactor cannot quietly alter it."""
    import asyncio

    from fastmcp import Client

    vault, path = _new_vault(tmp_path, "lease-fenced-pin")
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)
    state.runtime_for(path)
    state.vault_pool.fence(path)
    server = runtime._build_mcp_server(state)

    async def exercise() -> str:
        async with Client(server) as client:
            with pytest.raises(Exception) as exc_info:
                await client.call_tool("ask", {"question": "what?"})
            return str(exc_info.value)

    try:
        message = asyncio.run(exercise())
        assert "maintenance: vault maintenance is in progress; try again shortly" in message
        assert "vault_fenced" not in message
        assert str(path) not in message

        state.shutting_down = True
        shutdown_message = asyncio.run(exercise())
        assert "shutting_down: server is shutting down" in shutdown_message
        assert str(path) not in shutdown_message
    finally:
        state.shutting_down = False
        state.vault_pool.unfence(path)
        state.close()


# --------------------------------------------------------------------------
# MCP progress notifications for ``remember``.
#
# A long ``remember`` used to return NOTHING for minutes and clients abort an
# idle tool call ("sent no response or progress for 300s"), discarding the only
# trustworthy success signal (the payload). ``remember`` now bridges
# ``Companion.remember(on_progress=...)`` — which fires on the ``to_thread``
# worker — onto ``Context.report_progress`` on the event loop.
# --------------------------------------------------------------------------


def _remember_payload_fields(result):
    return {
        "document_id": result.document_id,
        "committed": result.committed,
        "queued": result.queued,
        "blocks_total": result.blocks_total,
        "nodes_extracted": result.nodes_extracted,
        "edges_extracted": result.edges_extracted,
        "claims_minted": result.claims_minted,
        "provider_error": result.provider_error,
        "provider_failures": result.provider_failures,
        "empty_after_retry_blocks": result.empty_after_retry_blocks,
        "llm_disabled": result.llm_disabled,
    }


def _progress_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, emit):
    """Wire a FakeCompanion whose ``remember`` drives ``on_progress`` via ``emit``."""
    from okto_neuron.companion import RememberResult
    from okto_neuron.server import http as http_module

    vault, path = _new_vault(tmp_path, name)
    state = ServerState(vault=vault, vault_path=path, multi_vault_runtime_enabled=True)
    result = RememberResult(
        document_id="doc-1", committed=2, queued=1, blocks_total=3, nodes_extracted=4
    )

    class FakeCompanion:
        def remember(self, _source, *, sensitivity, on_progress=None, **_kw):
            emit(on_progress)
            return result

    monkeypatch.setattr(http_module, "companion_for", lambda _vault: FakeCompanion())
    return state, runtime._build_mcp_server(state), result


def _three_block_ingest(on_progress):
    """Mimic the real ``_emit`` firing pattern: stage boundaries + per block.

    The extraction loop has alternate paths that emit the SAME
    ``(stage, blocks_done)`` pair more than once for one block — duplicated
    here deliberately so the coalescing rule is under test.
    """
    assert on_progress is not None
    on_progress("parsing", 0, 0)
    on_progress("extracting", 0, 3)
    for block in (1, 2, 3):
        on_progress("extracting", block, 3)
        on_progress("extracting", block, 3)  # duplicate alternate path
    on_progress("embedding", 3, 3)
    on_progress("committing", 3, 3)


def _run_remember_collecting_progress(server, seen):
    import asyncio

    from fastmcp import Client

    async def handler(progress: float, total: float | None, message: str | None) -> None:
        seen.append((progress, total, message))

    async def exercise():
        async with Client(server) as client:
            result = await client.call_tool(
                "remember", {"source": "a note\nacross lines"}, progress_handler=handler
            )
            # Notifications are fire-and-forget; let the client's receive loop
            # drain them before asserting on the strict per-block sequence.
            await asyncio.sleep(0.05)
            return result

    return asyncio.run(exercise())


def test_remember_emits_progress_notification_per_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    state, server, expected = _progress_fixture(
        tmp_path, monkeypatch, "progress-blocks", _three_block_ingest
    )
    seen: list[tuple[float, float | None, str | None]] = []
    try:
        call = _run_remember_collecting_progress(server, seen)
    finally:
        state.close()

    # One notification per block completion...
    blocks = [s for s in seen if s[2] and s[2].startswith("extracting ") and s[0] > 0]
    assert [s[0] for s in blocks] == [1.0, 2.0, 3.0]
    assert all(s[1] == 3.0 for s in blocks)
    # ...duplicated (stage, blocks_done) pairs coalesced away.
    assert len(seen) == len({(s[0], s[2]) for s in seen})
    # parsing has no block count yet: total must be None, never 0.
    parsing = [s for s in seen if s[2] == "parsing"]
    assert parsing and parsing[0][1] is None
    # ...and every stage boundary is represented.
    assert {s[2].split()[0] for s in seen if s[2]} == {
        "parsing",
        "extracting",
        "embedding",
        "committing",
    }

    # The payload is the whole point: it must be UNCHANGED.
    payload = call.data
    assert payload["document_id"] == expected.document_id
    for field, value in _remember_payload_fields(expected).items():
        assert payload[field] == value
    assert payload["outcomes"] == []
    assert "outcome" in payload


def _curation_ingest(on_progress):
    """A 3-block ingest whose dedup/curation phases tick sub-stage ordinals.

    Mirrors the measured live run: the blocks finish quickly, then dedup and
    curation dominate the runtime. The keep-alive reports an item ordinal with
    an explicitly undeclared total (0), which the bridge renders as the bare
    stage name and sends with total=None.
    """
    assert on_progress is not None
    on_progress("parsing", 0, 0)
    on_progress("extracting", 0, 3)
    for block in (1, 2, 3):
        on_progress("extracting", block, 3)
    on_progress("embedding", 3, 3)
    on_progress("dedup", 3, 3)
    for ordinal in (1, 6, 11, 16):
        on_progress("dedup", ordinal, 0)
    on_progress("committing", 3, 3)
    for ordinal in (1, 6, 11):
        on_progress("committing", ordinal, 0)


def test_remember_emits_progress_through_dedup_and_curation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The phase that dominates runtime must not be a single notification.

    Pins that the sub-stage ordinals survive the bridge's (stage, done)
    coalescing as SEPARATE notifications, and that the payload is unchanged.
    """
    state, server, expected = _progress_fixture(
        tmp_path, monkeypatch, "progress-curation", _curation_ingest
    )
    seen: list[tuple[float, float | None, str | None]] = []
    try:
        call = _run_remember_collecting_progress(server, seen)
    finally:
        state.close()

    dedup = [s for s in seen if s[2] == "dedup"]
    committing = [s for s in seen if s[2] == "committing"]
    # Advancing ordinals, each its own notification (the old behaviour was one).
    assert [s[0] for s in dedup] == [1.0, 6.0, 11.0, 16.0]
    assert [s[0] for s in committing] == [1.0, 6.0, 11.0]
    # An undeclared total is sent as None, never 0 (no divide-by-zero, no
    # bogus 100%).
    assert all(s[1] is None for s in dedup + committing)
    # Nothing was coalesced away.
    assert len(seen) == len({(s[0], s[2]) for s in seen})

    payload = call.data
    assert payload["document_id"] == expected.document_id
    for field, value in _remember_payload_fields(expected).items():
        assert payload[field] == value


def test_remember_survives_failing_progress_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A notification that cannot be delivered must never fail the ingest."""
    state, server, expected = _progress_fixture(
        tmp_path, monkeypatch, "progress-raises", _three_block_ingest
    )

    async def _boom(*_a, **_kw):
        raise RuntimeError("transport gone")

    import fastmcp.server.context as fastmcp_context

    monkeypatch.setattr(fastmcp_context.Context, "report_progress", _boom)
    seen: list[tuple[float, float | None, str | None]] = []
    try:
        call = _run_remember_collecting_progress(server, seen)
    finally:
        state.close()

    assert seen == []
    assert call.data["document_id"] == expected.document_id
    assert call.data["blocks_total"] == expected.blocks_total


def test_remember_bridge_returns_none_outside_mcp_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No MCP context (direct in-process call) => no callback, no failure."""
    import asyncio

    async def _check():
        assert runtime._mcp_progress_bridge() is None

    asyncio.run(_check())

    calls: list[object] = []
    state, server, expected = _progress_fixture(
        tmp_path, monkeypatch, "progress-nocontext", calls.append
    )

    async def exercise():
        from fastmcp import Client

        async with Client(server) as client:
            return await client.call_tool("remember", {"source": "a note\nacross lines"})

    try:
        call = asyncio.run(exercise())
    finally:
        state.close()

    assert call.data["document_id"] == expected.document_id


def test_remember_progress_dispatch_does_not_block_worker_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The worker must hand the coroutine off and walk away — never join it."""
    import asyncio as _asyncio

    real = _asyncio.run_coroutine_threadsafe
    futures: list[object] = []

    class _Unjoinable:
        def __init__(self, fut):
            self._fut = fut

        def result(self, timeout=None):  # pragma: no cover - must never run
            raise AssertionError("worker thread joined the notification future")

        def __getattr__(self, item):
            return getattr(self._fut, item)

    def _spy(coro, loop):
        fut = _Unjoinable(real(coro, loop))
        futures.append(fut)
        return fut

    monkeypatch.setattr(runtime.asyncio, "run_coroutine_threadsafe", _spy)
    state, server, expected = _progress_fixture(
        tmp_path, monkeypatch, "progress-nonblocking", _three_block_ingest
    )
    seen: list[tuple[float, float | None, str | None]] = []
    try:
        call = _run_remember_collecting_progress(server, seen)
    finally:
        state.close()

    assert futures, "no notification was dispatched"
    assert call.data["document_id"] == expected.document_id


# --------------------------------------------------------------------------
# Timer heartbeat + inline counters for MCP ``remember`` (client idle timeout).
# --------------------------------------------------------------------------


def _silent_ingest(seconds: float):
    import time

    def emit(on_progress):
        time.sleep(seconds)  # one long LLM call: no block events at all

    return emit


def test_remember_heartbeat_flows_while_block_call_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(runtime, "_MCP_HEARTBEAT_INTERVAL_S", 0.1)
    state, server, expected = _progress_fixture(
        tmp_path, monkeypatch, "hb-silent", _silent_ingest(0.65)
    )
    seen: list[tuple[float, float | None, str | None]] = []
    try:
        call = _run_remember_collecting_progress(server, seen)
    finally:
        state.close()

    beats = [s for s in seen if s[2] and s[2].startswith("remember in progress")]
    assert 4 <= len(beats) <= 7  # ~0.65 s / 0.1 s, interval honoured
    assert all(b[1] is None for b in beats)
    assert [b[0] for b in beats] == sorted(b[0] for b in beats)
    assert call.data["document_id"] == expected.document_id


def test_remember_heartbeat_stops_after_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import asyncio

    monkeypatch.setattr(runtime, "_MCP_HEARTBEAT_INTERVAL_S", 0.05)
    state, server, _ = _progress_fixture(tmp_path, monkeypatch, "hb-stop", _silent_ingest(0.2))
    seen: list[tuple[float, float | None, str | None]] = []
    try:
        _run_remember_collecting_progress(server, seen)
        n = len(seen)
        asyncio.run(asyncio.sleep(0.3))
    finally:
        state.close()
    assert n >= 2 and len(seen) == n


def test_remember_heartbeat_cancelled_on_exception_and_no_token_ok():
    import asyncio

    async def exercise():
        # No MCP context: a no-op that neither raises nor leaks a task.
        before = len(asyncio.all_tasks())
        with pytest.raises(ValueError):
            async with runtime._mcp_heartbeat():
                raise ValueError("boom")
        assert len(asyncio.all_tasks()) == before

    asyncio.run(exercise())


def test_remember_heartbeat_task_cancelled_when_body_raises(monkeypatch: pytest.MonkeyPatch):
    import asyncio

    import fastmcp.server.dependencies as deps

    sent: list[float] = []

    class _Ctx:
        async def report_progress(self, progress, total, message):
            sent.append(progress)

    monkeypatch.setattr(deps, "get_context", lambda: _Ctx())
    monkeypatch.setattr(runtime, "_MCP_HEARTBEAT_INTERVAL_S", 0.02)

    async def exercise():
        before = len(asyncio.all_tasks())
        with pytest.raises(ValueError):
            async with runtime._mcp_heartbeat():
                await asyncio.sleep(0.1)
                raise ValueError("boom")
        assert len(asyncio.all_tasks()) == before
        n = len(sent)
        await asyncio.sleep(0.1)
        assert len(sent) == n >= 2

    asyncio.run(exercise())


def test_remember_inline_counters_visible_while_running_and_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from okto_neuron.server import _ingest_queue as iq

    holder: dict = {}

    def emit(on_progress):
        holder["during"] = iq.inline_summary(holder["runtime"])
        holder["queue_during"] = iq.summary(holder["runtime"])

    state, server, _ = _progress_fixture(tmp_path, monkeypatch, "inline-ok", emit)
    holder["runtime"] = state.runtime_for(state.vault_path)
    try:
        _run_remember_collecting_progress(server, [])
        after = iq.inline_summary(holder["runtime"])
        queue_after = iq.summary(holder["runtime"])
    finally:
        state.close()
    assert holder["during"] == {"processing": 1, "done": 0, "error": 0}
    assert after == {"processing": 0, "done": 1, "error": 0}
    # Normal queue counters are untouched: an inline call is not a queue item.
    assert holder["queue_during"] == queue_after
    assert queue_after["total"] == 0 and queue_after["processing"] == 0


def test_remember_inline_counters_record_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from okto_neuron.server import _ingest_queue as iq

    def emit(on_progress):
        raise RuntimeError("provider exploded")

    state, server, _ = _progress_fixture(tmp_path, monkeypatch, "inline-err", emit)
    rt = state.runtime_for(state.vault_path)
    try:
        with pytest.raises(Exception):
            _run_remember_collecting_progress(server, [])
        after = iq.inline_summary(rt)
    finally:
        state.close()
    assert after == {"processing": 0, "done": 0, "error": 1}


def test_inline_summary_defaults_to_zero_for_legacy_state():
    from okto_neuron.server import _ingest_queue as iq

    assert iq.inline_summary(object()) == {"processing": 0, "done": 0, "error": 0}
