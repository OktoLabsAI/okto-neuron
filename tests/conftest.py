"""Shared test fixtures.

`cli_inprocess_server` makes the thin-client CLI (`add` / `query` /
`detect-drift`) hermetic: it routes the CLI's HTTP POSTs at an in-process
Starlette app bound to *the test's own vault*, instead of whatever real
server happens to be listening on :7777. Without this, those CLI commands
hit a foreign server whose vault != the test vault, so `add` writes
elsewhere and the test reads an empty local vault — the long-standing
non-hermetic failure.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import pytest
from starlette.testclient import TestClient

from okto_neuron.cli._client import ServerError
from okto_neuron.server import state as state_mod
from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import init_state
from okto_neuron.vault import Vault


@pytest.fixture(autouse=True)
def _no_inherited_legacy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every test without pre-0.3.0 ``MARGINALIA_*`` variables.

    The product reads them as a fallback for ``OKTO_NEURON_*`` (``_compat.getenv``),
    so one leaked from the developer's shell or from an earlier test that wrote
    ``os.environ`` directly (for example ``MARGINALIA_MLFLOW_TRACKING_URI``) would
    silently switch features on. Tests that exercise the fallback set the legacy
    name themselves with ``monkeypatch``.
    """
    import os

    for name in [key for key in os.environ if key.startswith("MARGINALIA_")]:
        monkeypatch.delenv(name, raising=False)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Auto-skip the ADR 0010 Tier B real-judge band gate (``acceptance_judge``)
    unless it is explicitly selected with ``-m acceptance_judge``.

    The gate drives a live LLM backend, so it must NEVER execute in the default
    ``uv run pytest`` selection (or in CI). pyproject has no ``addopts`` marker
    filter, and an ``addopts = ["-m", "not acceptance_judge"]`` would be ANDed with
    a CLI ``-m acceptance_judge`` (yielding ``not X and X`` = nothing selected), so
    the gate could never be run at all. Instead we collect the marked items and skip
    them at collection time unless the run's marker expression names the marker —
    bare runs SKIP it (collected, never executed); ``-m acceptance_judge`` runs it.
    """
    markexpr = str(config.getoption("-m") or "")
    if "acceptance_judge" in markexpr:
        return
    skip = pytest.mark.skip(
        reason="acceptance_judge: laptop-only real-judge gate; run with -m acceptance_judge"
    )
    for item in items:
        if item.get_closest_marker("acceptance_judge") is not None:
            item.add_marker(skip)


@pytest.fixture
def cli_inprocess_server(monkeypatch: pytest.MonkeyPatch):
    """Return ``bind(vault_path)``; after calling it, every CLI thin-client POST
    goes to an in-process server whose vault is opened at ``vault_path``.

    The vault is opened lazily on the first request, so callers may ``bind``
    before the CLI ``init`` command has actually created the vault on disk.
    """
    state_mod.reset_state_for_tests()
    bound: dict[str, Any] = {"path": None, "client": None}

    def _client() -> TestClient:
        if bound["client"] is None:
            vault_path = Path(bound["path"])
            vault = Vault.open(vault_path)
            init_state(vault, vault_path)
            # Host must be loopback for LoopbackHostMiddleware to allow the request.
            bound["client"] = TestClient(build_rest_app(), base_url="http://127.0.0.1")
        return bound["client"]

    def fake_post(
        endpoint: str,
        path: str,
        payload: Mapping[str, Any],
        *,
        timeout: float = 30.0,
        transport: Any = None,
        vault: str | Path | None = None,
    ) -> dict[str, Any]:
        headers = {"X-Okto-Neuron-Vault": str(vault)} if vault is not None else {}
        response = _client().post(path, json=dict(payload), headers=headers)
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                detail = body.get("detail", str(body)) if isinstance(body, dict) else str(body)
            except ValueError:
                detail = response.text
            raise ServerError(response.status_code, detail)
        return response.json()

    monkeypatch.setattr("okto_neuron.cli._client_post", fake_post)

    def bind(vault_path: Path | str) -> None:
        bound["path"] = str(vault_path)

    yield bind

    if bound["client"] is not None:
        bound["client"].close()
    state_mod.reset_state_for_tests()
