from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_installer():
    path = Path(__file__).resolve().parents[1] / "scripts" / "install-dev.py"
    spec = importlib.util.spec_from_file_location("install_dev_script", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_install_dev_noninteractive_installs_without_onboarding(monkeypatch) -> None:
    installer = _load_installer()
    calls: list[list[str]] = []

    monkeypatch.setattr(installer.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(installer, "_is_interactive", lambda: False)

    def fake_run(cmd: list[str], *, quiet: bool = False) -> int:
        del quiet
        calls.append(cmd)
        return 0

    monkeypatch.setattr(installer, "_run", fake_run)

    assert installer.main(["--no-onboard"]) == 0
    assert calls[0][:4] == ["/bin/uv", "tool", "install", "--editable"]
    assert calls[1] == ["/bin/marginalia", "--help"]
    assert all("onboard" not in call for call in calls)


def test_install_dev_forwards_onboard_options(monkeypatch) -> None:
    installer = _load_installer()
    calls: list[list[str]] = []

    monkeypatch.setattr(installer.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(installer, "_is_interactive", lambda: False)

    def fake_run(cmd: list[str], *, quiet: bool = False) -> int:
        del quiet
        calls.append(cmd)
        return 0

    monkeypatch.setattr(installer, "_run", fake_run)

    assert (
        installer.main(
            [
                "--onboard",
                "--non-interactive",
                "--provider",
                "local",
                "--vault",
                "alpha",
                "--model",
                "qwen",
                "--skip-model-discovery",
                "--allow-remote-llm",
                "--yes",
                "--reconfigure",
            ]
        )
        == 0
    )

    assert calls[-1] == [
        "/bin/marginalia",
        "onboard",
        "--vault",
        "alpha",
        "--provider",
        "local",
        "--model",
        "qwen",
        "--skip-model-discovery",
        "--reconfigure",
        "--yes",
        "--allow-remote-llm",
        "--non-interactive",
    ]


def test_install_dev_finds_windows_command_names_on_path(monkeypatch) -> None:
    installer = _load_installer()
    seen: list[str] = []

    monkeypatch.setattr(installer, "_is_windows", lambda: True)

    def fake_which(name: str) -> str | None:
        seen.append(name)
        return "C:/Users/me/.local/bin/marginalia.exe" if name == "marginalia.exe" else None

    monkeypatch.setattr(installer.shutil, "which", fake_which)

    assert str(installer._find_marginalia("uv")).endswith("marginalia.exe")
    assert seen[0] == "marginalia.exe"


def test_install_dev_finds_windows_command_in_uv_tool_bin(tmp_path, monkeypatch) -> None:
    installer = _load_installer()
    bin_dir = tmp_path / "uv-bin"
    bin_dir.mkdir()
    command = bin_dir / "marginalia.cmd"
    command.write_text("@echo off\n", encoding="utf-8")

    monkeypatch.setattr(installer, "_is_windows", lambda: True)
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)

    class _Proc:
        returncode = 0
        stdout = str(bin_dir)

    monkeypatch.setattr(installer.subprocess, "run", lambda *args, **kwargs: _Proc())

    assert installer._find_marginalia("uv") == command
