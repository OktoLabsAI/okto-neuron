"""Shared fixtures for ``tests/cli/``.

Isolates ``HOME`` for every test in this directory so the CLI group callback
(``app``/``kg_cli`` in ``cli/__init__.py``) can never read a real developer
machine's ``~/.marginalia/env`` through its unconditional
``load_user_env_file()`` call.

Without this, a test that invokes ``CliRunner().invoke(app, ...)`` (or
``kg_cli``) without first isolating ``HOME`` -- ``tests/cli/test_kg_init.py``
did exactly this -- loads whatever ``OKTO_NEURON_*`` secrets happen to sit in
*this developer's own* ``~/.marginalia/env`` (``load_user_env_file`` only
sets a name that is not already in ``os.environ``, so this is a one-way,
first-writer-wins injection). That value is a raw ``os.environ[...] =``
mutation, not a ``monkeypatch`` one, so it survives for the rest of the
pytest process and is invisible to any single test's own cleanup --
including tests that run much later, in unrelated files, that only fail when
the full suite runs in this exact order. The reproduction that surfaced this:
``tests/cli/test_kg_init.py`` leaked this machine's real
``OKTO_NEURON_LOCAL_LLM_KEY``, which later made
``tests/cli/test_onboard.py::
test_onboard_noninteractive_creates_vault_and_persists_visible_llm_config``
see a non-``None`` ``api_key_env`` it never asked for.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_cli_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``HOME`` at a per-test scratch directory before any CLI invoke.

    A test that needs a specific ``HOME`` subdirectory (e.g. to assert on a
    printed path) may still call ``monkeypatch.setenv("HOME", ...)`` itself
    afterward -- that later call simply wins, same as any other monkeypatch
    stacking. This fixture exists only to guarantee *some* isolated ``HOME``
    is always in place first, so a test that forgets to set one of its own
    still can't reach the real machine's ``~/.marginalia/env``.
    """

    monkeypatch.setenv("HOME", str(tmp_path / "_cli_home"))
    monkeypatch.delenv("OKTO_NEURON_ENV_FILE", raising=False)
