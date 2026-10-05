"""Loop guard (issue #13): no REST route or MCP tool may block the event loop.

REST (:7777) and MCP (:8201) share ONE asyncio loop with ``/health``. The guard is
structural, not a stopwatch: the real Starlette app and the real FastMCP tool
handlers run on the test's own loop against a real vault, while the blocking
entry points are wrapped with recorders that note whether each call ran on the
event-loop thread. The entry points are the ``IndexedStore`` facade (every
public method), the sidecar/YAML/JSON loaders and writers (``_jobs``/
``_ingest_queue`` persist, ``VaultConfig``/``OktoNeuronConfig`` loaders, the vault
registry and its markers, ``ReviewQueue``, ``CandidateLedger``), ``VaultPool``
lease/open, and the raw file primitives (``Path.read_text``/``write_text``/
``open``/..., ``os.replace``, ``builtins.open``) for any path under the test's
scratch directory, which catches helpers nobody listed. Every call recorded on
the loop thread fails the test, unless it is listed in ``_ALLOWED_ON_LOOP`` with
a reason.

The REST cases are parametrized from ``http._routes()`` itself and the MCP test
enumerates the server's own tool list, so a new route or tool is covered
automatically. A heartbeat task stays only as a coarse 1 s smoke test for the
unknown unknowns; no per-route millisecond budget remains, so load on the host
cannot flake it.
"""

from __future__ import annotations

import asyncio
import builtins
import functools
import inspect
import os
import re
import sys
import threading
import time
import traceback
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

SMOKE_LOOP_BLOCK_S = 1.0
"""Coarse heartbeat smoke limit: catches a stall the recorders do not know about."""
_HEARTBEAT_S = 0.005

# Routes deliberately not exercised here, with the reason.
_ROUTE_EXEMPT: dict[tuple[str, str], str] = {}

# Recorded labels that are allowed to run on the loop thread, with the reason.
# A label is matched as a prefix. Keep this empty unless the call is deliberate.
_ALLOWED_ON_LOOP: dict[str, str] = {}

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


_VAULT_POOL_BLOCKING = frozenset(
    {
        "lease",
        "get_or_open",
        "adopt",
        "claim_fenced_ownership",
        "install_fenced",
        "release_path",
        "close_all",
    }
)


class _LoopRecorder:
    """Records every wrapped call made on the event-loop thread while ``active``."""

    def __init__(self, scratch_root: Path) -> None:
        self.loop_thread = threading.get_ident()
        self.active = False
        self.calls: list[str] = []
        self.root = str(scratch_root)

    def hit(self, label: str) -> None:
        if not self.active or threading.get_ident() != self.loop_thread:
            return
        where = "?"
        for frame in reversed(traceback.extract_stack()[:-2]):
            if "okto_neuron" in frame.filename and "test_event_loop_guard" not in frame.filename:
                where = f"{Path(frame.filename).name}:{frame.lineno} {frame.name}"
                break
        self.calls.append(f"{label} <- {where}")

    def under_root(self, target: Any) -> bool:
        try:
            return os.fspath(target).startswith(self.root)
        except TypeError:
            return False

    def violations(self) -> list[str]:
        return [
            call
            for call in self.calls
            if not any(call.startswith(prefix) for prefix in _ALLOWED_ON_LOOP)
        ]


def _recording(fn: Callable[..., Any], label: str, rec: _LoopRecorder) -> Callable[..., Any]:
    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        rec.hit(label)
        return fn(*args, **kwargs)

    return _wrapped


def _record_class(
    monkeypatch: pytest.MonkeyPatch,
    cls: type,
    rec: _LoopRecorder,
    only: frozenset[str] | None = None,
) -> None:
    """Wrap every public function, classmethod and staticmethod defined on ``cls``
    (or only the names in ``only``)."""
    for name, attr in list(vars(cls).items()):
        if name.startswith("_") or (only is not None and name not in only):
            continue
        label = f"{cls.__name__}.{name}"
        if isinstance(attr, classmethod):
            wrapped: Any = classmethod(_recording(attr.__func__, label, rec))
        elif isinstance(attr, staticmethod):
            wrapped = staticmethod(_recording(attr.__func__, label, rec))
        elif inspect.isfunction(attr):
            wrapped = _recording(attr, label, rec)
        else:
            continue
        monkeypatch.setattr(cls, name, wrapped)


def _record_function_everywhere(
    monkeypatch: pytest.MonkeyPatch, fn: Callable[..., Any], label: str, rec: _LoopRecorder
) -> None:
    """Replace ``fn`` with a recorder in every okto_neuron module that imported it."""
    wrapper = _recording(fn, label, rec)
    for module in list(sys.modules.values()):
        if not getattr(module, "__name__", "").startswith("okto_neuron"):
            continue
        for attr, value in list(vars(module).items()):
            if value is fn:
                monkeypatch.setattr(module, attr, wrapper)


def _instrument(monkeypatch: pytest.MonkeyPatch, rec: _LoopRecorder) -> None:
    """Install recorders on the blocking entry points (see the module docstring)."""
    import okto_neuron.vault_registry as registry
    from okto_neuron.config import VaultConfig
    from okto_neuron.config._app_config import OktoNeuronConfig
    from okto_neuron.consolidate.ledger import CandidateLedger
    from okto_neuron.consolidate.review_queue import ReviewQueue
    from okto_neuron.store.index.indexed import IndexedStore

    for cls in (
        IndexedStore,
        VaultConfig,
        OktoNeuronConfig,
        ReviewQueue,
        CandidateLedger,
    ):
        _record_class(monkeypatch, cls, rec)
    # VaultPool: only the calls that open, lease or close a vault. peek, is_fenced,
    # lease_count and paths are in-memory bookkeeping under a lock, not blocking I/O.
    _record_class(monkeypatch, VaultPool, rec, only=_VAULT_POOL_BLOCKING)
    for fn in (_jobs.persist, iq.persist):
        _record_function_everywhere(monkeypatch, fn, f"{fn.__module__}.{fn.__name__}", rec)
    for name, fn in list(vars(registry).items()):
        if not name.startswith("_") and inspect.isfunction(fn) and fn.__module__ == registry.__name__:
            _record_function_everywhere(monkeypatch, fn, f"vault_registry.{name}", rec)

    # Raw file primitives, recorded only for paths under the scratch directory, so a
    # helper that reads or writes a sidecar without going through a listed entry
    # point is still caught.
    for name in ("read_text", "read_bytes", "write_text", "write_bytes", "open"):
        original = getattr(Path, name)

        def _path_wrapper(self: Path, *args: Any, _o: Any = original, _n: str = name, **kw: Any):
            if rec.under_root(self):
                rec.hit(f"Path.{_n}({self.name})")
            return _o(self, *args, **kw)

        monkeypatch.setattr(Path, name, _path_wrapper)

    real_open = builtins.open
    real_replace = os.replace

    def _open(file: Any, *args: Any, **kwargs: Any) -> Any:
        if not isinstance(file, int) and rec.under_root(file):
            rec.hit(f"open({Path(os.fspath(file)).name})")
        return real_open(file, *args, **kwargs)

    def _replace(src: Any, dst: Any, *args: Any, **kwargs: Any) -> Any:
        if rec.under_root(dst):
            rec.hit(f"os.replace({Path(os.fspath(dst)).name})")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", _open)
    monkeypatch.setattr(os, "replace", _replace)


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
    recorder = _LoopRecorder(tmp_path)
    _instrument(monkeypatch, recorder)
    try:
        yield state, Path(vault.path), recorder
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
    state, vault_path, rec = guarded
    url = re.sub(r"\{(\w+)\}", lambda match: f"guard-{match.group(1)}", path)
    body = _fill(_BODIES.get((method, path), {}), vault_path)
    kwargs: dict[str, Any] = {} if method == "GET" else {"json": body}

    app = http_mod.build_rest_app(state)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50123))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        # Warm-up: first-call lazy imports and lazily created state are a one-off
        # cost; the measured request is the second one.
        await client.request(method, url, timeout=120, **kwargs)
        await _settle_background(state)
        rec.calls.clear()
        rec.active = True
        try:
            async with _LoopMonitor() as monitor:
                response = await client.request(method, url, timeout=120, **kwargs)
        finally:
            rec.active = False
        await _settle_background(state)

    assert response.status_code < 600
    assert not rec.violations(), (
        f"{method} {path} ran blocking calls on the event loop thread "
        f"(status {response.status_code}); route them through store_io:\n  "
        + "\n  ".join(rec.violations())
    )
    assert monitor.max_gap < SMOKE_LOOP_BLOCK_S, (
        f"{method} {path} starved the event loop heartbeat for {monitor.max_gap * 1000:.0f} ms"
    )


# Arguments for every MCP tool; the test fails if the server grows a tool that
# is missing here, so new tools are covered too.
# Each entry is (warm-up args, measured args); the measured call must succeed so
# the test times the tool itself, not FastMCP's error-report rendering.
_MCP_TOOL_ARGS: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "ask": ({"question": "what is this?", "k": 3},) * 2,
    "explore": ({"topic": "knowledge graph", "k": 3},) * 2,
    "ingest_status": (
        # P1 async remember: a cheap, non-blocking status poll (the job id is
        # looked up in the vault's queue; a miss exercises the not_found path).
        {"job_id": "0-nonexistent"},
        {"job_id": "0-nonexistent"},
    ),
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

    state, _vault_path, rec = guarded
    monkeypatch.setattr(
        http_mod, "companion_for", lambda vault: Companion(vault, provider=StubLLM())
    )
    server = runtime_mod._build_mcp_server(state)
    async with Client(server) as client:
        tools = sorted(tool.name for tool in await client.list_tools())
        assert tools == sorted(_MCP_TOOL_ARGS), "add the new MCP tool to _MCP_TOOL_ARGS"
        # P1: ingest_status needs a REAL job id (a miss is a tool error, which
        # this guard must not see); enqueue one now and use its id.
        queued = await client.call_tool(
            "remember", {"source": "loop-guard job\nfor ingest_status"}
        )
        queued_payload = (
            queued.structured_content
            if hasattr(queued, "structured_content")
            else queued.data
        )
        if isinstance(queued_payload, dict) and set(queued_payload) == {"result"}:
            queued_payload = queued_payload["result"]
        args = _MCP_TOOL_ARGS["ingest_status"]
        _MCP_TOOL_ARGS["ingest_status"] = (
            {"job_id": queued_payload["job_id"]},
            {"job_id": queued_payload["job_id"]},
        )
        for name in tools:
            warm_args, args = _MCP_TOOL_ARGS[name]
            # Warm-up for first-call imports, then the measured call.
            await client.call_tool(name, warm_args, raise_on_error=False)
            await _settle_background(state)
            rec.calls.clear()
            rec.active = True
            try:
                async with _LoopMonitor() as monitor:
                    result = await client.call_tool(name, args, raise_on_error=False)
            finally:
                rec.active = False
            await _settle_background(state)
            assert not result.is_error, f"MCP tool {name} failed: {result.content}"
            assert not rec.violations(), (
                f"MCP tool {name} ran blocking calls on the event loop thread:\n  "
                + "\n  ".join(rec.violations())
            )
            assert monitor.max_gap < SMOKE_LOOP_BLOCK_S, (
                f"MCP tool {name} starved the event loop heartbeat "
                f"for {monitor.max_gap * 1000:.0f} ms"
            )


@pytest.mark.asyncio
async def test_concurrent_predicate_polls_share_one_scan_and_health_stays_fast(
    guarded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """30 concurrent upkeep polls on a cold vault collapse into ONE rebuild (they answer 202
    meanwhile, never waiting on it) while /health keeps answering quickly on the same loop;
    once built, every poll is a cheap 200 with no further scan."""
    from okto_neuron.server import _projection

    state, _vault_path, _rec = guarded
    scans: list[float] = []
    real_build = _projection.build_predicate_stats

    def _counting_build(store: Any) -> Any:
        scans.append(time.perf_counter())
        time.sleep(0.5)
        return real_build(store)

    monkeypatch.setattr(_projection, "build_predicate_stats", _counting_build)

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
        first = await asyncio.gather(*polls)
        assert {r.status_code for r in first} == {202}, "a cold vault must answer 202 at once"
        assert all(r.json() == {"status": "building"} for r in first)

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            final = await client.get("/api/v1/upkeep/predicates", timeout=60)
            if final.status_code == 200 and not final.json()["rebuilding"]:
                break
            started = time.perf_counter()  # the rebuild is running: /health must stay quick
            assert (await client.get("/health", timeout=10)).status_code == 200
            health_latencies.append(time.perf_counter() - started)
            await asyncio.sleep(0.02)
        assert final.status_code == 200, final.text[:200]
        later = await asyncio.gather(
            *[client.get("/api/v1/upkeep/predicates", timeout=60) for _ in range(30)]
        )

    assert all(r.status_code == 200 and r.json()["stale"] is False for r in later)
    assert len({r.json()["vocabulary_size"] for r in later}) == 1
    assert len(scans) == 1, f"expected one shared rebuild, saw {len(scans)}"
    assert len(health_latencies) >= 5
    assert max(health_latencies) < SMOKE_LOOP_BLOCK_S, f"/health max {max(health_latencies) * 1000:.0f} ms"


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
    from okto_neuron.server import _projection

    big = {"status": "ok", "node_types": _big_rows(), "edge_types": []}
    monkeypatch.setattr(_projection, "compute_graph_stats", lambda store: {False: big, True: big})


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


_PROJECTION_CASES = frozenset({"graph_stats", "predicate_snapshot"})

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
    state, _vault_path, _rec = guarded

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
        if case in _PROJECTION_CASES:
            # The projection is built on a worker (its big body is encoded there); the
            # request then only splices the flags in.
            from okto_neuron.server import _projection
            from okto_neuron.server._store_io import store_io

            await store_io(_projection.manager_for(state.vault_path).build, state.vault.store)
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
