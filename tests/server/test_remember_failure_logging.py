"""A failed ``remember`` leaves the same classified record on REST and MCP.

A REST client only sees the JSON body and an MCP client only sees the queued
job, so the daemon log (REST) and the job sidecar (MCP) are the operator's
records of why a write failed. REST classifies the failure through
``http.log_remember_failure``:

- a caller's mistake (a source path that is missing or outside the vault) is a
  WARNING naming the mistake, without a traceback;
- a failed graph write (``IngestError`` around a store failure, a store
  ``GraphBackendError``, a backend's exhausted-retry error) is an ERROR
  ``graph write failed: ...`` line with its cause and traceback;
- any other exception is an ERROR ``unexpected remember failure`` with its
  traceback.

MCP (async, P1) classifies the SAME failure as the job's terminal state: the
queue worker runs the extraction, the item ends ``status=error`` and
``ingest_status`` reports ``ok=false`` with the exception text in
``error``. (Missing-path sources are a separate, call-time contract: MCP
``remember`` now refuses them loudly as ``bad_path`` before enqueueing.)

Each case runs through both surfaces and asserts the matching record.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import okto_neuron.server.runtime as runtime
from okto_neuron import errors
from okto_neuron.companion import SourceOutsideVaultError
from okto_neuron.server import http as http_mod
from okto_neuron.server.state import ServerState, init_state, reset_state_for_tests
from okto_neuron.store import vault as vault_module
from okto_neuron.store.ladybug import VaultConnection
from okto_neuron.vault import Vault

_LOGGERS = ("okto_neuron.server.http", "okto_neuron.server.runtime")
_HEADS = ("graph write failed", "unexpected remember failure", "remember rejected")


@dataclass(frozen=True)
class Case:
    """``make(source, vault_path)`` builds the exception the companion raises."""

    make: Callable[[str, Path], Exception]
    level: str
    head: str
    traceback: bool
    rest_status: int
    rest_error: str


def _grafx_exhausted(source: str, vault_path: Path) -> Exception:
    from okto_neuron.store.grafx import GrafxWriteExhausted

    return GrafxWriteExhausted(vault_path=vault_path)


def _neo4j_exhausted(source: str, vault_path: Path) -> Exception:
    pytest.importorskip("neo4j")
    from okto_neuron.store.neo4j import Neo4jWriteExhausted

    return Neo4jWriteExhausted(vault_path=vault_path)


_DENIED = PermissionError(13, "Permission denied")

CASES: dict[str, Case] = {
    "ingest_store_failure": Case(
        lambda s, v: errors.IngestError(s, vault_path=v, message="graph write failed", cause=_DENIED),
        "ERROR",
        "graph write failed: IngestError: graph write failed",
        True,
        500,
        "ladybug_write_failed",
    ),
    "graph_backend_error": Case(
        lambda s, v: errors.GraphBackendError(
            "lock file could not be opened", backend="grafx", vault_path=v, cause=_DENIED
        ),
        "ERROR",
        "graph write failed: GraphBackendError: grafx backend: lock file could not be opened",
        True,
        500,
        "remember_failed",
    ),
    "grafx_write_exhausted": Case(
        _grafx_exhausted,
        "ERROR",
        "graph write failed: GrafxWriteExhausted: graph write failed: retry budget exhausted",
        True,
        500,
        "remember_failed",
    ),
    "neo4j_write_exhausted": Case(
        _neo4j_exhausted,
        "ERROR",
        "graph write failed: Neo4jWriteExhausted: graph write failed: retry budget exhausted",
        True,
        500,
        "remember_failed",
    ),
    "unexpected": Case(
        lambda s, v: PermissionError(13, "Permission denied", str(v / ".marginalia" / "ledger")),
        "ERROR",
        "unexpected remember failure",
        True,
        500,
        "internal",
    ),
    # A FileNotFoundError about some OTHER file is not the caller's mistake.
    "unrelated_missing_file": Case(
        lambda s, v: FileNotFoundError(2, "No such file or directory", str(v / "graph.db")),
        "ERROR",
        "unexpected remember failure",
        True,
        500,
        "internal",
    ),
    # How Vault.add reports a missing source: an IngestError around the OSError.
    "missing_source": Case(
        lambda s, v: errors.IngestError(
            s,
            vault_path=v,
            message=f"FileNotFoundError: [Errno 2] No such file or directory: '{s}'",
            cause=FileNotFoundError(2, "No such file or directory", s),
        ),
        "WARNING",
        "remember rejected: source path not found: IngestError: FileNotFoundError",
        False,
        500,
        "ladybug_write_failed",
    ),
    "missing_source_bare": Case(
        lambda s, v: FileNotFoundError(2, "No such file or directory", s),
        "WARNING",
        "remember rejected: source path not found: [Errno 2]",
        False,
        500,
        "internal",
    ),
    "source_is_directory": Case(
        lambda s, v: errors.IngestError(
            s, vault_path=v, cause=IsADirectoryError(21, "Is a directory", s)
        ),
        "WARNING",
        "remember rejected: source path is not a file: IngestError",
        False,
        500,
        "ladybug_write_failed",
    ),
    "file_not_under_vault": Case(
        lambda s, v: errors.FileNotUnderVaultError(s, vault_path=v),
        "WARNING",
        "remember rejected: source path is outside the vault: FileNotUnderVaultError",
        False,
        500,
        "ladybug_write_failed",
    ),
    "source_outside_vault": Case(
        lambda s, v: SourceOutsideVaultError(f"refusing to remember source outside: {s!r}"),
        "WARNING",
        "remember rejected: source path is outside the vault: SourceOutsideVaultError",
        False,
        403,
        "forbidden",
    ),
}


def _records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.name in _LOGGERS and r.getMessage().startswith(_HEADS)
    ]


def _assert_record(records: list[logging.LogRecord], case: Case, exc: Exception) -> None:
    assert [r.levelname for r in records] == [case.level], [r.getMessage() for r in records]
    assert records[0].getMessage().startswith(case.head), records[0].getMessage()
    if case.traceback:
        assert records[0].exc_info is not None and records[0].exc_info[1] is exc
    else:
        assert not records[0].exc_info


def _fake_companion(exc: Exception):
    class FakeCompanion:
        def remember(self, _source, *, sensitivity, on_progress=None, **_kw):
            raise exc

    return FakeCompanion()


def _close_stores() -> None:
    for store in list(vault_module._STORE_CACHE.values()):
        store.close()
    vault_module._STORE_CACHE.clear()
    VaultConnection.close_all()


def _mcp_remember_enqueued(state: ServerState, source: str) -> dict[str, object]:
    """Call async MCP ``remember`` with an EXISTING source (so it enqueues),
    drain the queue worker inside the client loop, and return the terminal
    ``ingest_status`` detail for the job."""
    from fastmcp import Client

    server = runtime._build_mcp_server(state)

    async def exercise() -> dict[str, object]:
        async with Client(server) as client:
            result = await client.call_tool("remember", {"source": source})
            queued = result.structured_content or {}
            assert queued.get("status") == "queued", queued
            assert queued.get("poll") == "ingest_status"
            job_id = queued["job_id"]
            # Let the drain worker finish INSIDE this loop (bounded).
            for _ in range(500):
                item = next((i for i in state.ingest_queue if i.id == job_id), None)
                if item is not None and item.status in {"done", "error", "cancelled"}:
                    break
                await asyncio.sleep(0.01)
            return (
                await client.call_tool("ingest_status", {"job_id": job_id})
            ).structured_content or {}

    return asyncio.run(exercise())


@pytest.fixture
def rest_client(tmp_path: Path) -> Iterator[tuple[TestClient, Vault]]:
    reset_state_for_tests()
    vault = Vault.init(tmp_path / "rest", packs=["core"])
    state = init_state(vault, vault.path)
    with TestClient(http_mod.build_rest_app(state), base_url="http://127.0.0.1") as client:
        yield client, vault
    reset_state_for_tests()
    _close_stores()


@pytest.mark.parametrize("kind", sorted(CASES))
def test_mcp_remember_records_failure_on_the_job_by_the_shared_rule(
    kind: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async contract (P1): the companion failure happens in the queue
    worker, not at call time, so the operator's MCP-side record is the
    terminal job (``ingest_status``: status=error, ok=false, the exception
    text in ``error``) — the same failure the REST surface logs."""
    case = CASES[kind]
    vault = Vault.init(tmp_path / "mcp", packs=["core"])
    vault_path = Path(vault.path).resolve()
    # An EXISTING in-vault file: remember enqueues it and the worker is what
    # raises the case's exception.
    (vault_path / "notes").mkdir(parents=True, exist_ok=True)
    source_path = vault_path / "notes" / "real.md"
    source_path.write_text(f"# {kind}\n", encoding="utf-8")
    source = str(source_path)
    exc = case.make(source, vault_path)
    monkeypatch.setattr(http_mod, "companion_for", lambda _vault: _fake_companion(exc))
    state = ServerState(vault=vault, vault_path=vault_path, multi_vault_runtime_enabled=True)
    try:
        detail = _mcp_remember_enqueued(state, source)
        assert detail.get("status") == "error", detail
        assert detail.get("ok") is False, detail
        assert detail.get("error") == str(exc), detail
    finally:
        state.close()


@pytest.mark.parametrize("kind", sorted(CASES))
def test_rest_remember_logs_failure_by_the_shared_rule(
    kind: str,
    rest_client: tuple[TestClient, Vault],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    case = CASES[kind]
    client, vault = rest_client
    vault_path = Path(vault.path).resolve()
    source = str(vault_path / "notes" / "missing.md")
    exc = case.make(source, vault_path)
    monkeypatch.setattr(http_mod, "_companion", lambda _state: _fake_companion(exc))
    with caplog.at_level("WARNING"):
        response = client.post("/remember", json={"source": source})
    # The response body is unchanged by the logging rule.
    assert response.status_code == case.rest_status, response.text
    assert response.json()["error"] == case.rest_error
    _assert_record(_records(caplog), case, exc)


# The two cases below use the real Companion (no fake): the missing-source
# classification must hold for the exception the ingest path actually raises.


def test_mcp_remember_real_missing_source_fails_loudly_at_call_time(
    tmp_path: Path,
) -> None:
    """Async contract (P1): a path-shaped source that does not exist is a
    caller's mistake that MCP ``remember`` refuses LOUDLY AT CALL TIME as a
    ``bad_path`` error — no job is enqueued (there is nothing for the worker
    to fail on later), and no source is materialized."""
    from fastmcp import Client

    vault = Vault.init(tmp_path / "mcp-real", packs=["core"])
    vault_path = Path(vault.path).resolve()
    state = ServerState(vault=vault, vault_path=vault_path, multi_vault_runtime_enabled=True)
    server = runtime._build_mcp_server(state)

    async def exercise() -> None:
        async with Client(server) as client:
            with pytest.raises(Exception) as excinfo:
                await client.call_tool(
                    "remember", {"source": str(vault_path / "notes" / "does-not-exist.md")}
                )
        assert "bad_path" in str(excinfo.value)
        assert "does not exist" in str(excinfo.value)

    try:
        asyncio.run(exercise())
        assert not list(state.ingest_queue), "missing path must not enqueue a job"
        sources_dir = vault_path / ".marginalia" / "sources"
        assert not sources_dir.exists() or not list(sources_dir.glob("*.md"))
    finally:
        state.close()
        _close_stores()


def test_rest_remember_real_missing_source_is_a_caller_warning(
    rest_client: tuple[TestClient, Vault], caplog: pytest.LogCaptureFixture
) -> None:
    client, vault = rest_client
    source = str(Path(vault.path).resolve() / "notes" / "does-not-exist.md")
    with caplog.at_level("WARNING"):
        response = client.post("/remember", json={"source": source})
    assert response.status_code == 500
    assert response.json()["error"] == "ladybug_write_failed"
    records = _records(caplog)
    assert [r.levelname for r in records] == ["WARNING"]
    assert records[0].getMessage().startswith("remember rejected: source path not found:")
    assert not records[0].exc_info
