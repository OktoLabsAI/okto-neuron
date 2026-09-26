"""CLI dispatch for `kg reconcile review` (LLM-free — review never builds a judge).

Guards the Click positional-vault-vs-subcommand collision: a group-level optional
VAULT argument would swallow the subcommand name. These commands open the vault
read-only and touch only the off-graph queue, so no LLM loads."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from okto_neuron.cli import app


def _init_vault(tmp_path, monkeypatch):
    """Minimal vault so `Vault.open` succeeds (okto-neuron.yaml + graph).

    Isolates HOME first: the `app` group callback unconditionally calls
    load_user_env_file(), which would otherwise leak this developer
    machine's real ~/.marginalia/env secrets into the pytest process (see
    tests/cli/conftest.py's docstring for the reproduction history).
    """
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    runner = CliRunner()
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return tmp_path


def test_review_confirm_unknown_id_dispatches(tmp_path, monkeypatch: pytest.MonkeyPatch):
    vault = _init_vault(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(app, ["kg", "reconcile", "review", "confirm", "bogus-id", str(vault)])
    # The bug signature was "No such command 'bogus-id'". Correct dispatch reaches
    # the handler, which reports the missing queued cluster and exits 1.
    assert "No such command" not in result.output
    assert "no queued cluster" in result.output
    assert result.exit_code == 1


def test_review_reject_unknown_id_dispatches(tmp_path, monkeypatch: pytest.MonkeyPatch):
    vault = _init_vault(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(app, ["kg", "reconcile", "review", "reject", "bogus-id", str(vault)])
    assert "No such command" not in result.output
    assert result.exit_code == 0  # reject is idempotent (no-op on unknown id)


def test_review_list_empty_dispatches(tmp_path, monkeypatch: pytest.MonkeyPatch):
    vault = _init_vault(tmp_path, monkeypatch)
    runner = CliRunner()
    result = runner.invoke(app, ["kg", "reconcile", "review", "list", str(vault)])
    assert "No such command" not in result.output
    assert result.exit_code == 0
    assert "review queue empty" in result.output
