"""Loop guard (issue #13): no REST route or MCP tool may block the event loop.

REST (:7777) and MCP (:8201) share ONE asyncio loop with ``/health``. These
tests run the real Starlette app and the real FastMCP tool handlers on the test's
own loop against a real vault whose store and vault-file entry points are made
slow (every call sleeps ``SLOW_S``). A heartbeat task measures how long the loop
goes without running it; any store, sidecar or YAML call made ON the loop shows
up as a gap of at least ``SLOW_S`` and fails the test. Work correctly routed
through ``store_io`` (or another executor) sleeps on a worker thread and leaves
the heartbeat untouched.

The REST cases are parametrized from ``http._routes()`` itself, so a new route
is covered automatically. ``_ROUTE_EXEMPT`` lists the (documented) exceptions.
"""

from __future__ import annotations

import asyncio
import functools
import re
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from starlette.routing import Route

from okto_neuron import Vault
from okto_neuron.companion import Companion
from okto_neuron.core.schema import Edge, Node
from okto_neuron.llm import StubLLM
from okto_neuron.server import _ingest_queue as iq
from okto_neuron.server import _jobs
from okto_neuron.server import http as http_mod
from okto_neuron.server import runtime as runtime_mod
from okto_neuron.server._vault_pool import VaultPool
from okto_neuron.server.state import get_server_state, init_state, reset_state_for_tests

SLOW_S = 0.2
"""Every patched store / vault-file call sleeps this long on whatever thread runs it."""
MAX_LOOP_BLOCK_S = 0.05
"""The loop may never go longer than this without running the heartbeat."""
_HEARTBEAT_S = 0.005

# Routes deliberately not exercised here, with the reason.
_ROUTE_EXEMPT: dict[tuple[str, str], str] = {}

# Routes that close and reopen the Ladybug handle. Ladybug's native open/close
# holds the GIL for ~60-80 ms on a WORKER thread, which the heartbeat sees even
# though no loop callback runs long (asyncio debug, slow_callback_duration=20 ms,
# reports none). They get a looser budget that still catches any patched store
# call made on the loop (each one sleeps SLOW_S = 200 ms).
_GIL_BOUND_BUDGET_S = 0.15
_GIL_BOUND_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/reset"): "wipe + reopen of the Ladybug graph on a store worker",
    ("POST", "/api/v1/embedding/reembed"): "releases (closes) the Ladybug handle on a worker",
    ("POST", "/api/v1/vaults/reembed"): "same re-embed coordinator as /embedding/reembed",
}

# Request bodies for routes whose empty-body path would stop at validation before
# reaching any store work. Everything else gets ``{}`` (or no body for GET).
_BODIES: dict[tuple[str, str], Any] = {
    ("POST", "/add"): {"path": "notes/guard.md", "content": "# Guard\n\nloop guard note\n"},
    ("POST", "/query"): {"query": "knowledge graphs", "k": 3},
    ("POST", "/recall"): {"query": "knowledge graphs", "k": 3},
    ("POST", "/api/v1/recall"): {"query": "knowledge graphs", "k": 3},
    ("POST", "/ask"): {"question": "what is this?", "k": 3},
    ("POST", "/api/v1/ask"): {"question": "what is this?", "k": 3},
    ("POST", "/remember"): {"source": "note.md"},
    ("POST", "/api/v1/ingest"): {"content": "# Pasted\n\nloop guard paste\n", "filename": "p.md"},
    ("POST", "/detect-drift"): {"corpus_root": "__VAULT__"},
    ("POST", "/api/v1/detect-drift"): {"corpus_root": "__VAULT__"},
    ("POST", "/resolve-review"): {"candidate_id": "missing", "action": "discard"},
    ("POST", "/api/v1/resolve-review"): {"candidate_id": "missing", "action": "discard"},
    ("POST", "/review-queue/batch"): {"candidate_ids": ["missing"], "action": "discard"},
    ("POST", "/api/v1/review-queue/batch"): {"candidate_ids": ["missing"], "action": "discard"},
    ("POST", "/api/v1/vaults"): {"name": "guard-created"},
    ("POST", "/api/v1/vaults/switch"): {"vault": "__VAULT__"},
    ("POST", "/api/v1/vaults/reembed"): {"vault": "__VAULT__"},
    ("DELETE", "/api/v1/vaults/{vault_id}"): {"confirm_name": "nope"},
    ("PATCH", "/api/v1/config"): {"folder_watch": {"poll_interval_s": 30}},
    ("PATCH", "/api/v1/config/defaults"): {"folder_watch": {"poll_interval_s": 30}},
    ("POST", "/api/v1/ingest-folder"): {"path": "__VAULT__/notes"},
    ("POST", "/api/v1/folder-watch/roots"): {"path": "__VAULT__/notes"},
    ("DELETE", "/api/v1/folder-watch/roots"): {"path": "__VAULT__/notes"},
    ("POST", "/api/v1/ingest-batch"): {"files": [{"filename": "b.md", "content": "# B\n\nb\n"}]},
    ("POST", "/api/v1/reconcile/review/confirm"): {"cluster_id": "missing"},
    ("POST", "/api/v1/reconcile/review/reject"): {"cluster_id": "missing"},
    ("POST", "/api/v1/authority/unmerge"): {"cluster_id": "missing"},
    ("POST", "/api/v1/quality/semantic"): {},
    ("POST", "/api/v1/llm/test"): {"provider": "stub"},
    ("POST", "/api/v1/config/defaults/llm/test"): {"provider": "stub"},
    ("POST", "/api/v1/llm/test-completion"): {"provider": "stub", "model": "stub"},
    ("POST", "/api/v1/config/defaults/llm/test-completion"): {"provider": "stub", "model": "stub"},
    ("POST", "/api/v1/embedding/models"): {"provider": "stub"},
    ("POST", "/api/v1/config/defaults/embedding/models"): {"provider": "stub"},
    ("POST", "/api/v1/embedding/test"): {"provider": "stub", "model": "stub", "dimension": 8},
    ("POST", "/api/v1/config/defaults/embedding/test"): {
        "provider": "stub",
        "model": "stub",
        "dimension": 8,
    },
    ("POST", "/api/v1/credentials"): {"name": "guard", "api_key": "guard-secret"},
    ("PUT", "/api/v1/credentials/provider"): {
        "kind": "llm",
        "provider": "openai",
        "api_key": "guard-secret",
    },
    ("PUT", "/api/v1/llm/credential"): {"provider": "openai", "api_key": "guard-secret"},
    ("PUT", "/api/v1/credentials/{credential_id}"): {"name": "renamed"},
    ("POST", "/api/v1/providers"): {"name": "guard", "driver": "stub"},
    ("PATCH", "/api/v1/providers/{provider_id}"): {"name": "renamed"},
}


def _route_cases() -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for route in http_mod._routes():
        if not isinstance(route, Route):
            continue
        for method in sorted(route.methods or ()):
            if method == "HEAD":
                continue
            cases.append((method, route.path))
    return cases


class _SlowStore:
    """Delegate every store method, sleeping ``SLOW_S`` first."""

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> Any:
        value = getattr(self._inner, name)
        if not callable(value) or name.startswith("__"):
            return value

        @functools.wraps(value)
        def _slow(*args: Any, **kwargs: Any) -> Any:
            time.sleep(SLOW_S)
            return value(*args, **kwargs)

        return _slow

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)


def _slow_fn(fn: Callable[..., Any]) -> Callable[..., Any]:
    @functools.wraps(fn)
    def _slow(*args: Any, **kwargs: Any) -> Any:
        time.sleep(SLOW_S)
        return fn(*args, **kwargs)

    return _slow


def _make_everything_slow(monkeypatch: pytest.MonkeyPatch, vault: Vault) -> None:
    """Slow down the store and the vault-file/YAML entry points a handler uses."""
    import okto_neuron.vault_registry as registry
    from okto_neuron.config import VaultConfig

    monkeypatch.setattr(vault, "store", _SlowStore(vault.store))
    slow_list_vaults = _slow_fn(registry.list_vaults)
    monkeypatch.setattr(registry, "list_vaults", slow_list_vaults)
    monkeypatch.setattr(http_mod, "list_vaults", slow_list_vaults)
    monkeypatch.setattr(VaultConfig, "load", classmethod(_slow_fn(VaultConfig.load.__func__)))
    monkeypatch.setattr(
        VaultConfig,
        "load_application_defaults",
        classmethod(_slow_fn(VaultConfig.load_application_defaults.__func__)),
    )
    monkeypatch.setattr(_jobs, "persist", _slow_fn(_jobs.persist))
    monkeypatch.setattr(iq, "persist", _slow_fn(iq.persist))
    monkeypatch.setattr(VaultPool, "lease", _slow_fn(VaultPool.lease))


class _LoopMonitor:
    """Heartbeat on the running loop; records the longest gap between beats."""

    def __init__(self) -> None:
        self.max_gap = 0.0
        self._task: asyncio.Task[None] | None = None

    async def _beat(self) -> None:
        last = time.perf_counter()
        while True:
            await asyncio.sleep(_HEARTBEAT_S)
            now = time.perf_counter()
            self.max_gap = max(self.max_gap, now - last - _HEARTBEAT_S)
            last = now

    async def __aenter__(self) -> "_LoopMonitor":
        self._task = asyncio.create_task(self._beat())
        await asyncio.sleep(_HEARTBEAT_S * 2)
        self.max_gap = 0.0
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._task is not None
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass


def _seed_graph(vault: Vault) -> None:
    note = Path(vault.path) / "note.md"
    note.write_text("# Title\n\nbody about knowledge graphs.\n", encoding="utf-8")
    (Path(vault.path) / "notes" / "watched.md").write_text("# W\n\nwatched\n", encoding="utf-8")
    store = vault.store
    store.add_node(
        Node(
            id="block:1",
            type="Block",
            title="block one",
            facets={"source_path": str(note), "byte_start": 0, "byte_end": 10},
        )
    )
    store.add_node(Node(id="concept:1", type="Concept", title="knowledge graph"))
    store.add_edge(Edge(id="e1", type="skos:related", src="concept:1", dst="block:1"))


@pytest.fixture
def guarded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OKTO_NEURON_CONFIG", raising=False)
    monkeypatch.delenv("OKTO_NEURON_VAULT", raising=False)
    reset_state_for_tests()
    vault = Vault.init(tmp_path / "v", embedder="stub")
    _seed_graph(vault)
    monkeypatch.setattr(
        http_mod, "_companion", lambda state: Companion(state.vault, provider=StubLLM())
    )
    state = init_state(vault, Path(vault.path))
    _make_everything_slow(monkeypatch, vault)
    try:
        yield state, Path(vault.path)
    finally:
        reset_state_for_tests()


async def _settle_background(state: Any) -> None:
    """Let workers a route started finish before the vault closes."""
    tasks = [task for task in get_server_state().runtime_tasks() if not task.done()]
    if tasks:
        await asyncio.wait(tasks, timeout=60)


def _fill(value: Any, vault_path: Path) -> Any:
    if isinstance(value, str):
        return value.replace("__VAULT__", str(vault_path))
    if isinstance(value, dict):
        return {key: _fill(item, vault_path) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, vault_path) for item in value]
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), _route_cases())
async def test_rest_route_never_blocks_event_loop(guarded, method: str, path: str) -> None:
    if (method, path) in _ROUTE_EXEMPT:
        pytest.skip(_ROUTE_EXEMPT[(method, path)])
    state, vault_path = guarded
    url = re.sub(r"\{(\w+)\}", lambda match: f"guard-{match.group(1)}", path)
    body = _fill(_BODIES.get((method, path), {}), vault_path)
    kwargs: dict[str, Any] = {} if method == "GET" else {"json": body}

    app = http_mod.build_rest_app(state)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50123))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        # Warm-up: first-call lazy imports are a one-off cost, not a store call;
        # every patched store/vault-file call stays slow on every request.
        await client.request(method, url, timeout=120, **kwargs)
        await _settle_background(state)
        async with _LoopMonitor() as monitor:
            response = await client.request(method, url, timeout=120, **kwargs)
        await _settle_background(state)

    assert response.status_code < 600
    budget = _GIL_BOUND_BUDGET_S if (method, path) in _GIL_BOUND_ROUTES else MAX_LOOP_BLOCK_S
    assert monitor.max_gap < budget, (
        f"{method} {path} blocked the event loop for {monitor.max_gap * 1000:.0f} ms "
        f"(status {response.status_code}); route its store/vault-file work through store_io"
    )


# Arguments for every MCP tool; the test fails if the server grows a tool that
# is missing here, so new tools are covered too.
# Each entry is (warm-up args, measured args); the measured call must succeed so
# the test times the tool itself, not FastMCP's error-report rendering.
_MCP_TOOL_ARGS: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "ask": ({"question": "what is this?", "k": 3},) * 2,
    "explore": ({"topic": "knowledge graph", "k": 3},) * 2,
    "remember": (
        {"source": "a pasted note\nabout the loop guard"},
        {"source": "a second pasted note\nabout the loop guard"},
    ),
    "list_vaults": ({}, {}),
    "init_vault": ({"name": "guard-mcp-warm"}, {"name": "guard-mcp"}),
}


@pytest.mark.asyncio
async def test_every_mcp_tool_never_blocks_event_loop(
    guarded, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp import Client

    state, _vault_path = guarded
    monkeypatch.setattr(
        http_mod, "companion_for", lambda vault: Companion(vault, provider=StubLLM())
    )
    server = runtime_mod._build_mcp_server(state)
    async with Client(server) as client:
        tools = sorted(tool.name for tool in await client.list_tools())
        assert tools == sorted(_MCP_TOOL_ARGS), "add the new MCP tool to _MCP_TOOL_ARGS"
        for name in tools:
            warm_args, args = _MCP_TOOL_ARGS[name]
            # Warm-up for first-call imports, then the measured call.
            await client.call_tool(name, warm_args, raise_on_error=False)
            await _settle_background(state)
            async with _LoopMonitor() as monitor:
                result = await client.call_tool(name, args, raise_on_error=False)
            await _settle_background(state)
            assert not result.is_error, f"MCP tool {name} failed: {result.content}"
            assert monitor.max_gap < MAX_LOOP_BLOCK_S, (
                f"MCP tool {name} blocked the event loop for {monitor.max_gap * 1000:.0f} ms"
            )


@pytest.mark.asyncio
async def test_concurrent_predicate_polls_share_one_scan_and_health_stays_fast(
    guarded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """30 concurrent upkeep polls collapse into ONE vocabulary scan while /health
    keeps answering quickly on the same loop."""
    import okto_neuron.predicates as predicates

    state, _vault_path = guarded
    http_mod._PREDICATE_VOCAB_CACHE.clear()
    scans: list[float] = []
    real_collect = predicates.collect_predicate_vocabulary

    def _counting_collect(store: Any) -> Any:
        scans.append(time.perf_counter())
        time.sleep(0.5)
        return real_collect(store)

    monkeypatch.setattr(predicates, "collect_predicate_vocabulary", _counting_collect)

    app = http_mod.build_rest_app(state)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50124))
    health_latencies: list[float] = []
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        polls = [
            asyncio.create_task(client.get("/api/v1/upkeep/predicates", timeout=120))
            for _ in range(30)
        ]
        while not all(poll.done() for poll in polls):
            started = time.perf_counter()
            health = await client.get("/health", timeout=10)
            health_latencies.append(time.perf_counter() - started)
            assert health.status_code == 200
            await asyncio.sleep(0.02)
        responses = await asyncio.gather(*polls)

    assert all(response.status_code == 200 for response in responses)
    assert len({response.json()["vocabulary_size"] for response in responses}) == 1
    assert len(scans) == 1, f"expected one shared scan, saw {len(scans)}"
    assert len(health_latencies) >= 5
    assert max(health_latencies) < 0.1, f"/health max {max(health_latencies) * 1000:.0f} ms"


# ---------------------------------------------------------------------------
# Big payloads: serialising a large response is loop work too
# ---------------------------------------------------------------------------

_BIG_ITEMS = 12000

_ROWS_CACHE: list[dict[str, Any]] = []


def _big_rows(count: int = _BIG_ITEMS) -> list[dict[str, Any]]:
    """Built once: rebuilding ~200k objects per request would measure the garbage
    collector, not serialisation."""
    if count != _BIG_ITEMS:
        return _make_rows(count)
    if not _ROWS_CACHE:
        _ROWS_CACHE.extend(_make_rows(count))
    return _ROWS_CACHE


def _make_rows(count: int) -> list[dict[str, Any]]:
    excerpt = "excerpt text " * 40  # ~500 characters
    return [
        {
            "id": f"node:{index}",
            "type": "Claim",
            "title": f"synthetic title {index}",
            "excerpt": excerpt,
            "facets": {f"k{n}": f"value-{index}-{n}" for n in range(40)},
            "edges": [{"type": "rel", "dst": f"node:{index + n}"} for n in range(10)],
        }
        for index in range(count)
    ]


_REVIEW_ITEMS: list[Any] = []


class _BigReviewCompanion:
    @staticmethod
    def review_queue_all() -> list[Any]:
        from types import SimpleNamespace

        if not _REVIEW_ITEMS:
            _REVIEW_ITEMS.extend(
                SimpleNamespace(
                    kind="node",
                    model_dump=lambda mode="json", row=row: row,
                    block_id=None,
                    source_path="/synthetic/source.md",
                    byte_start=0,
                    byte_end=10,
                    content_hash="h",
                )
                for row in _big_rows()
            )
        return list(_REVIEW_ITEMS)


def _patch_review(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http_mod, "_companion", lambda state: _BigReviewCompanion())


def _patch_graph_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    body = {"status": "ok", "nodes": _big_rows(), "total": _BIG_ITEMS}
    for name in ("_nodes_list_payload", "_graph_payload", "_neighbors_payload"):
        monkeypatch.setattr(http_mod, name, lambda *args, _b=body, **kwargs: _b)
    monkeypatch.setattr(
        http_mod,
        "_graph_stats_payload",
        lambda *args, _b={"status": "ok", "node_types": _big_rows(), "edge_types": []}: _b,
    )


def _patch_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    from okto_neuron.consolidate.ledger import CandidateLedger

    monkeypatch.setattr(
        CandidateLedger, "run_summaries", lambda self, limit=50: _big_rows(), raising=True
    )
    monkeypatch.setattr(
        CandidateLedger,
        "run_progress_summary",
        lambda self, run_id=None, limit=12: {"runs": _big_rows()},
        raising=True,
    )
    monkeypatch.setattr(
        CandidateLedger, "run_detail", lambda self, run_id: {"rows": _big_rows()}, raising=True
    )


def _patch_predicates(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    rows = [SimpleNamespace(status="queued", row=row) for row in _big_rows()]
    monkeypatch.setattr(
        http_mod._curation,
        "predicate_alias_index",
        lambda state, **kwargs: SimpleNamespace(records=lambda: rows),
    )
    monkeypatch.setattr(
        http_mod._curation, "predicate_record_row", lambda record: dict(record.row)
    )
    monkeypatch.setattr(http_mod, "_predicate_vocabulary_size", lambda state: 1)


_BIG_CASES: dict[str, tuple[Callable[[pytest.MonkeyPatch], None], str]] = {
    "review_queue": (_patch_review, "/api/v1/review-queue"),
    "nodes_list": (_patch_graph_ops, "/api/v1/nodes"),
    "graph": (_patch_graph_ops, "/api/v1/graph"),
    "neighbors": (_patch_graph_ops, "/api/v1/nodes/anything/neighbors"),
    "graph_stats": (_patch_graph_ops, "/api/v1/graph/stats"),
    "ledger_runs": (_patch_ledger, "/api/v1/ledger/runs"),
    "ledger_summary": (_patch_ledger, "/api/v1/ledger/summary"),
    "ledger_detail": (_patch_ledger, "/api/v1/ledger/runs/any-run"),
    "predicate_snapshot": (_patch_predicates, "/api/v1/upkeep/predicates"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(_BIG_CASES))
async def test_big_payload_is_never_serialised_on_the_loop(
    guarded, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Structural, no timing: every ``json.dumps`` the request makes is recorded
    with whether it ran on the event-loop thread; the multi-megabyte encode must
    happen on a worker. (Serialising a big payload on the loop stalls /health,
    REST and MCP for the whole encode.)"""
    import json

    patch, url = _BIG_CASES[case]
    patch(monkeypatch)
    state, _vault_path = guarded

    real_dumps = json.dumps
    calls: list[tuple[bool, int]] = []

    def _recording_dumps(*args: Any, **kwargs: Any) -> str:
        text = real_dumps(*args, **kwargs)
        on_loop = asyncio._get_running_loop() is not None
        calls.append((on_loop, len(text)))
        return text

    app = http_mod.build_rest_app(state)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50125))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        monkeypatch.setattr(json, "dumps", _recording_dumps)
        response = await client.get(url, timeout=300)
        monkeypatch.setattr(json, "dumps", real_dumps)

    assert response.status_code == 200, response.text[:300]
    assert len(response.content) > 1_000_000, "payload too small to be the large-payload path"
    on_loop_chars = sum(size for on_loop, size in calls if on_loop)
    worker_chars = sum(size for on_loop, size in calls if not on_loop)
    assert on_loop_chars < 10_000, (
        f"{case}: {on_loop_chars} characters of JSON were serialised on the event loop "
        f"(largest single call {max((n for on_loop, n in calls if on_loop), default=0)}); "
        "encode the response inside store_io"
    )
    assert worker_chars > 1_000_000, "the large encode did not run on a worker at all"
