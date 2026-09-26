from __future__ import annotations

from pathlib import Path

import pytest

from okto_neuron import dev as dev_module
from okto_neuron.dev import (
    DevOptions,
    classify_changed_paths,
    iter_watched_files,
    should_restart_after_exit,
    should_restart_server,
    start_server,
)


def test_implicit_dev_start_does_not_resolve_a_vault(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        dev_module,
        "resolve_vault_reference",
        lambda _ref: pytest.fail("implicit dev startup must remain vault-neutral"),
    )

    assert dev_module._resolve_dev_vault(None) is None


def test_explicit_dev_vault_is_still_resolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    monkeypatch.setattr(dev_module, "resolve_vault_reference", lambda _ref: vault)
    monkeypatch.setattr(dev_module, "is_vault", lambda candidate: candidate == vault)

    assert dev_module._resolve_dev_vault("vault") == vault


def test_classify_changed_paths_separates_python_and_frontend(tmp_path: Path) -> None:
    repo = tmp_path
    python_file = repo / "src" / "okto_neuron" / "dev.py"
    frontend_file = repo / "frontend" / "src" / "App.tsx"

    assert classify_changed_paths([python_file], repo) == {"python"}
    assert classify_changed_paths([frontend_file], repo) == {"frontend"}
    assert classify_changed_paths([python_file, frontend_file], repo) == {
        "frontend",
        "python",
    }


def test_iter_watched_files_ignores_generated_and_dependency_dirs(tmp_path: Path) -> None:
    repo = tmp_path
    watched_python = repo / "src" / "okto_neuron" / "cli.py"
    watched_frontend = repo / "frontend" / "src" / "App.tsx"
    ignored_dist = repo / "frontend_dist" / "assets" / "index.js"
    ignored_node_modules = repo / "frontend" / "node_modules" / "pkg" / "index.js"
    for path in (watched_python, watched_frontend, ignored_dist, ignored_node_modules):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    files = {path.relative_to(repo) for path in iter_watched_files(repo)}

    assert Path("src/okto_neuron/cli.py") in files
    assert Path("frontend/src/App.tsx") in files
    assert Path("frontend_dist/assets/index.js") not in files
    assert Path("frontend/node_modules/pkg/index.js") not in files


def test_frontend_only_change_keeps_running_server() -> None:
    assert should_restart_server({"frontend"}, server_running=True) is False
    assert should_restart_server({"python"}, server_running=True) is True
    assert should_restart_server({"frontend", "python"}, server_running=True) is True
    assert should_restart_server({"frontend"}, server_running=False) is True


def test_dev_restarts_clean_or_signal_exit_without_crash_looping() -> None:
    assert should_restart_after_exit(0) is True
    assert should_restart_after_exit(-15) is True
    assert should_restart_after_exit(2) is False


def test_dev_server_never_opens_browser_on_start_or_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], Path, dict[str, str]]] = []

    def fake_popen(cmd, *, cwd, env):  # noqa: ANN001
        calls.append((cmd, cwd, env))
        return object()

    monkeypatch.setattr(dev_module, "find_repo_root", lambda: tmp_path)
    monkeypatch.setattr(dev_module.subprocess, "Popen", fake_popen)

    start_server(DevOptions(), None)

    assert len(calls) == 1
    command, cwd, _env = calls[0]
    assert cwd == tmp_path
    assert "--no-open" in command
