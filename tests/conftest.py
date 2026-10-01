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


# --- real-home guard --------------------------------------------------------
# A pytest run once wrote writer-lease lock files into the developer's LIVE
# vaults (~/.marginalia/vaults) because tests resolved the real home and opened
# the real vault registry. Every test now runs with HOME pointing at a scratch
# directory, the Neuron path variables unset, and a hard assertion that nothing
# resolves under the real home. Tests may still monkeypatch.setenv their own
# HOME / variables afterward: the per-test fixture runs first, theirs wins.

# Read by src/okto_neuron/vault_registry.py:152/442, config/_app_config.py:157,
# onboarding.py:326, core/schema/loader.py:159 (via _compat.getenv, which falls
# back to the MARGINALIA_* spelling built by _compat.legacy_env_name, _compat.py:98)
# and llm/_chatgpt.py:121/201 (CHATGPT_TOKEN_DIR, read straight from os.environ).
_NEURON_PATH_VARS = (
    "OKTO_NEURON_CONFIG",
    "OKTO_NEURON_VAULT",
    "OKTO_NEURON_ENV_FILE",
    "OKTO_NEURON_PACK_PATH",
    "CHATGPT_TOKEN_DIR",
    "MARGINALIA_CONFIG",
    "MARGINALIA_VAULT",
    "MARGINALIA_ENV_FILE",
    "MARGINALIA_PACK_PATH",
)
_APP_HOME_DIRNAMES = (".marginalia", ".okto-neuron")


def _real_home() -> Path:
    import os
    import pwd

    return Path(os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir))


def _is_under(path: Path, root: Path) -> bool:
    import os

    resolved = Path(os.path.realpath(path))
    return resolved == root or root in resolved.parents


def assert_home_is_isolated() -> None:
    """Fail loudly if ``Path.home()`` or the app homes resolve into the real home."""
    real = _real_home()
    home = Path.home()
    problems = []
    if _is_under(home, real):
        problems.append(f"Path.home() = {home} is the real user home {real} (or under it)")
    for dirname in _APP_HOME_DIRNAMES:
        if _is_under(home / dirname, real):
            problems.append(f"{home / dirname} resolves under the real home {real}")
    if problems:
        message = "REAL-HOME GUARD: refusing to run tests against live data: " + "; ".join(problems)
        raise pytest.UsageError(message)


_REAL_APP_DIRS: tuple[str, ...] = ()
_WRITE_FLAGS = 0


def _write_guard_audit(event: str, args: tuple) -> None:
    """Audit hook: refuse file creation/writes under the real ~/.marginalia|~/.okto-neuron.

    Covers Python-level open()/os.open()/os.mkdir(). Native writers (sqlite, ladybug)
    bypass audit events; HOME isolation above is the primary defence.
    """
    if not _REAL_APP_DIRS:
        return
    if event == "open":
        path, _mode, flags = args
        if not flags & _WRITE_FLAGS:
            return
    elif event == "os.mkdir":
        path = args[0]
    else:
        return
    if isinstance(path, bytes):
        path = path.decode(errors="replace")
    if not isinstance(path, str):
        return
    import os

    resolved = os.path.realpath(path)
    for root in _REAL_APP_DIRS:
        if resolved == root or resolved.startswith(root + os.sep):
            raise PermissionError(f"REAL-HOME GUARD: test attempted to write {resolved}")


def pytest_configure(config: pytest.Config) -> None:
    import os
    import sys

    global _REAL_APP_DIRS, _WRITE_FLAGS
    _WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
    real = _real_home()
    _REAL_APP_DIRS = tuple(str(real / d) for d in _APP_HOME_DIRNAMES)
    sys.addaudithook(_write_guard_audit)


@pytest.fixture(autouse=True, scope="session")
def _session_isolated_home(tmp_path_factory: pytest.TempPathFactory):
    """Session-wide scratch HOME, then assert it is not the real one."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(tmp_path_factory.mktemp("session_home")))
        for name in _NEURON_PATH_VARS:
            mp.delenv(name, raising=False)
        assert_home_is_isolated()
        yield


@pytest.fixture(autouse=True)
def _isolated_home_and_neuron_env(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-test scratch HOME and no path variables; re-assert before the test body."""
    monkeypatch.setenv("HOME", str(tmp_path_factory.mktemp("home")))
    for name in _NEURON_PATH_VARS:
        monkeypatch.delenv(name, raising=False)
    assert_home_is_isolated()


@pytest.fixture(autouse=True)
def _review_queue_layout_gate_is_not_under_test(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Treat a hand-written ``marginalia_yaml_version: 1`` fixture as a v2 vault.

    Many tests overwrite a vault's yaml with a minimal version-1 body just to set
    one option, then exercise the review queue. Since #14 a version-1 vault's
    queue is refused until ``kg review-queue migrate``; that gate has its own
    tests, which opt out by defining ``REAL_QUEUE_GATE = True`` at module level.
    """
    if getattr(request.module, "REAL_QUEUE_GATE", False):
        return
    from okto_neuron.consolidate import review_queue as review_queue_module

    real = review_queue_module.vault_yaml_version
    monkeypatch.setattr(
        review_queue_module,
        "vault_yaml_version",
        lambda root: 2 if real(root) == 1 else real(root),
    )


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


def _logger_state(logger: Any) -> dict[str, Any]:
    return {
        "propagate": logger.propagate,
        "level": logger.level,
        "disabled": logger.disabled,
        "filters": list(logger.filters),
        # pytest's own capture handlers are installed and removed by pytest per
        # phase; they are never snapshotted or restored here.
        "handlers": [
            h for h in logger.handlers if not type(h).__module__.startswith("_pytest")
        ],
    }


def _apply_logger_state(logger: Any, state: dict[str, Any]) -> None:
    logger.propagate = state["propagate"]
    logger.setLevel(state["level"])
    logger.disabled = state["disabled"]
    logger.filters[:] = state["filters"]
    for handler in list(logger.handlers):
        if not type(handler).__module__.startswith("_pytest") and handler not in state["handlers"]:
            logger.removeHandler(handler)
    for handler in state["handlers"]:
        if handler not in logger.handlers:
            logger.addHandler(handler)


_PRISTINE_LOGGER_STATE: dict[str, Any] = {
    "propagate": True,
    "level": 0,
    "disabled": False,
    "filters": [],
    "handlers": [],
}


@pytest.fixture(autouse=True)
def _restore_gc_state():
    """Undo what ``apply_gc_tuning`` / ``install_gc_watch`` leave process-wide (#38).

    ``runtime._run_async`` freezes the heap, raises the thresholds and installs a
    ``gc.callbacks`` hook; none of that is covered by the logger restore below.
    """
    import gc
    import sys

    from okto_neuron.server import _gc_tuning

    thresholds = gc.get_threshold()
    switch_interval = sys.getswitchinterval()
    yield
    _gc_tuning.reset_for_tests()
    gc.set_threshold(*thresholds)
    sys.setswitchinterval(switch_interval)
    gc.unfreeze()


@pytest.fixture(autouse=True)
def _reset_model_call_force_cancel():
    """Clear the model-call force-cancel flags before and after every test.

    ``runtime._run_async``'s shutdown calls ``cancel_active_litellm_calls`` /
    ``kill_active_cli_processes``, which raise process-wide flags that nothing
    clears (production exits afterwards). Left set, a later test's model call
    raises ``LLMCallCancelled``.
    """
    from okto_neuron.llm import _cli_provider, _litellm_process

    _litellm_process._reset_force_cancel_for_tests()
    _cli_provider._reset_for_tests()
    yield
    _litellm_process._reset_force_cancel_for_tests()
    _cli_provider._reset_for_tests()


@pytest.fixture(autouse=True)
def _restore_logger_state():
    """Undo logging changes a test (or ``configure_logging``) leaves behind.

    ``configure_logging`` sets ``propagate=False`` and installs handlers on the
    ``okto_neuron`` logger; left in place, a later test's ``caplog`` (a root
    handler) sees nothing (#35). Snapshot the root logger, ``okto_neuron`` and
    its existing children before each test and restore them afterwards; a child
    logger first created during the test returns to the pristine state.
    """
    import logging

    def tracked() -> dict[str, logging.Logger]:
        found = {"": logging.getLogger(), "okto_neuron": logging.getLogger("okto_neuron")}
        for name, obj in list(logging.root.manager.loggerDict.items()):
            if name.startswith("okto_neuron") and isinstance(obj, logging.Logger):
                found[name] = obj
        return found

    before = {name: _logger_state(lg) for name, lg in tracked().items()}
    try:
        yield
    finally:
        for name, lg in tracked().items():
            _apply_logger_state(lg, before.get(name, _PRISTINE_LOGGER_STATE))


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
