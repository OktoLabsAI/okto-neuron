"""The public Windows installer (install.ps1) refuses a preseed without a vault
with its own message, in both documented invocation shapes.

install.ps1 has a script-wide `trap` whose cleanup functions are defined further
down. A refusal that runs before those definitions surfaced as
"The term 'Remove-InstallerTemps' is not recognized" instead of the refusal. This
runs the real script in PowerShell 7 (pwsh); the distribution gate runs the same
path under Windows PowerShell 5.1.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_PS1 = REPO_ROOT / "install.ps1"
PWSH = shutil.which("pwsh")

pytestmark = pytest.mark.skipif(PWSH is None, reason="pwsh (PowerShell 7) is not installed")

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# pwsh's error view wraps long messages onto "     | " gutter lines.
_GUTTER = re.compile(r"\r?\n[ \t]*\|")


def _flat(text: str) -> str:
    return re.sub(r"\s+", "", _GUTTER.sub("", _ANSI.sub("", text)))


def _run(
    tmp_path: Path, command: list[str], preseed: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OKTO_NEURON_", "MARGINALIA_"))
    }
    env.update(HOME=str(tmp_path), LANG="en_US.UTF-8", LC_ALL="en_US.UTF-8", NO_COLOR="1")
    env.update(preseed)
    return subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", *command],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(["-File", str(INSTALL_PS1)], id="file"),
        pytest.param(
            ["-Command", f"Get-Content -Raw -LiteralPath '{INSTALL_PS1}' | Invoke-Expression"],
            id="irm-iex",
        ),
    ],
)
@pytest.mark.parametrize(
    ("preseed", "name"),
    [
        ({"OKTO_NEURON_LLM_PROVIDER": "skip"}, "OKTO_NEURON_LLM_PROVIDER"),
        ({"MARGINALIA_LLM_PROVIDER": "skip"}, "OKTO_NEURON_LLM_PROVIDER"),
        ({"OKTO_NEURON_PACKS": "core"}, "OKTO_NEURON_PACKS"),
    ],
)
def test_preseed_without_vault_is_refused_with_the_real_message(
    tmp_path: Path, command: list[str], preseed: dict[str, str], name: str
) -> None:
    result = _run(tmp_path, command, preseed)
    output = result.stdout + result.stderr

    assert result.returncode != 0, output
    assert _flat(
        f"error: {name} requires OKTO_NEURON_VAULT; omit preseed settings and configure "
        "vaults in the Web UI, or set OKTO_NEURON_VAULT explicitly"
    ) in _flat(output), output
    assert "is not recognized" not in output, output
    for helper in ("Remove-InstallerTemps", "Restore-LegacyMcp", "Restore-PreviousTool"):
        assert helper not in output, output
    # Refused before touching anything: no installer state under the sandbox home.
    assert not (tmp_path / ".okto-neuron").exists()


_MATCHER_SCRIPT = r"""
$ast = [System.Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$null, [ref]$null)
$fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $n.Name -eq "Test-ClaudeMcpRegistrationMatches" }, $true)
. ([scriptblock]::Create($fn.Extent.Text))
$url = "http://127.0.0.1:8201/mcp"
# U+2714 is what Claude Code 2.1.283 printed (bytes E2 9C 94), 2026-09-26.
$glyphBytes = [System.Text.Encoding]::UTF8.GetBytes([string][char]0x2714)
foreach ($codePage in @(65001, 437, 850, 1252)) {
    $glyph = [System.Text.Encoding]::GetEncoding($codePage).GetString($glyphBytes)
    foreach ($status in @("$glyph Connected", "$glyph Not Connected", "Not Connected")) {
        $output = @(
            "okto-neuron:",
            "  Scope: User config (available in all your projects)",
            "  Status: $status",
            "  Type: http",
            "  URL: $url"
        ) -join "`n"
        "{0}|{1}|{2}" -f $codePage, $status.EndsWith("Not Connected"), (Test-ClaudeMcpRegistrationMatches $output $url)
    }
}
"""


def test_mcp_status_glyph_matches_under_any_console_code_page(tmp_path: Path) -> None:
    """Windows PowerShell 5.1 decodes native output with the OEM code page, so
    the check mark's three UTF-8 bytes arrive as three characters, some of them
    letters (cp437: U+0393 U+00A3 U+00F6). A connected registration must still
    verify, and "Not Connected" must still fail, whatever the code page."""
    script = tmp_path / "matcher.ps1"
    script.write_text(_MATCHER_SCRIPT, encoding="utf-8")
    result = _run(tmp_path, ["-File", str(script), str(INSTALL_PS1)], {})
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [line.split("|") for line in result.stdout.split() if "|" in line]
    assert len(rows) == 12, result.stdout + result.stderr
    for code_page, negated, matched in rows:
        assert matched == str(negated != "True"), (code_page, negated, matched, result.stdout)


FAKE_CLAUDE = REPO_ROOT / "tests" / "fixtures" / "fake-claude-mcp.sh"
_MCP_URL = "http://127.0.0.1:8201/mcp"
_TOKEN = "daemon-credential-0123456789"

# Loads the installer's real helper functions (by AST, so nothing else in the
# script runs) and then its real Claude Code wiring block, with the daemon
# either verified running or intentionally left stopped.
_WIRING_SCRIPT = r"""
param([string]$Installer, [string]$Started)
$ErrorActionPreference = "Stop"
$text = Get-Content -Raw -LiteralPath $Installer
$ast = [System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$null, [ref]$null)
$names = @("Step", "Info", "Warn", "Die", "Test-ClaudeMcpRegistrationMatches",
    "Test-ClaudeMcpRegistrationWritten", "Get-ClaudeMcpRegistrationScope", "Get-ScopeLabel",
    "Test-McpRegistrationVerified", "Register-Mcp", "Remove-LegacyMcp")
foreach ($fn in $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -in $names }, $true)) {
    . ([scriptblock]::Create($fn.Extent.Text))
}
$CliName = "okto-neuron"
$LegacyCliName = "marginalia"
$globalUrl = $env:TEST_MCP_URL
$authToken = $env:TEST_TOKEN
$serverStarted = ($Started -eq "1")
$mcpWired = $false
$mcpScopeWired = ""
$script:LegacyMcpRemovedScope = ""
$start = $text.IndexOf('if ($env:OKTO_NEURON_NO_MCP -eq "1") {')
$end = $text.IndexOf("# The daemon itself stays headless")
. ([scriptblock]::Create($text.Substring($start, $end - $start)))
"LEGACY_REMOVED_SCOPE=$($script:LegacyMcpRemovedScope)"
"""


def _seed_mcp_entry(state: Path, name: str, token: str, scope: str = "user") -> None:
    (state / name).write_text(
        f"{scope}\n{_MCP_URL}\nAuthorization: Bearer {token}\n", encoding="utf-8"
    )


def _run_wiring(
    tmp_path: Path, *, server_started: bool, status: str
) -> subprocess.CompletedProcess[str]:
    script = tmp_path / "wiring.ps1"
    script.write_text(_WIRING_SCRIPT, encoding="utf-8")
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir(exist_ok=True)
    if not (fake_bin / "claude").exists():
        (fake_bin / "claude").symlink_to(FAKE_CLAUDE)
    return _run(
        tmp_path,
        ["-File", str(script), str(INSTALL_PS1), "1" if server_started else "0"],
        {
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "FAKE_CLAUDE_STATE": str(tmp_path / "state"),
            "FAKE_CLAUDE_STATUS": status,
            "TEST_MCP_URL": _MCP_URL,
            "TEST_TOKEN": _TOKEN,
        },
    )


def test_stopped_daemon_update_registers_okto_neuron_and_removes_marginalia(
    tmp_path: Path,
) -> None:
    """The install.ps1 twin of the install.sh fix: with the daemon left stopped
    the new entry is verified as written, and the old `marginalia` entry is
    still replaced (before, the update died with "did not verify as a
    connected user-scope endpoint")."""
    state = tmp_path / "state"
    state.mkdir()
    _seed_mcp_entry(state, "marginalia", _TOKEN)

    result = _run_wiring(tmp_path, server_started=False, status="failed")

    output = _flat(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in state.iterdir()) == ["okto-neuron"], output
    assert (state / "okto-neuron").read_text(encoding="utf-8").splitlines() == [
        "user",
        _MCP_URL,
        f"Authorization: Bearer {_TOKEN}",
    ]
    assert _flat("removed the old 'marginalia' user-scope entry") in output
    assert _flat("it connects on the next 'okto-neuron serve'") in output
    assert _flat("LEGACY_REMOVED_SCOPE=user") in output
    assert _TOKEN not in result.stdout + result.stderr


def test_stopped_daemon_rerun_preserves_the_written_entry(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _seed_mcp_entry(state, "marginalia", _TOKEN)
    _seed_mcp_entry(state, "okto-neuron", _TOKEN)

    result = _run_wiring(tmp_path, server_started=False, status="failed")

    output = _flat(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    assert sorted(p.name for p in state.iterdir()) == ["okto-neuron"], output
    assert _flat("preserved configured 'okto-neuron' user-scope registration") in output


def test_stopped_daemon_refuses_an_entry_with_another_credential(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _seed_mcp_entry(state, "marginalia", _TOKEN)
    _seed_mcp_entry(state, "okto-neuron", "some-other-credential")

    result = _run_wiring(tmp_path, server_started=False, status="failed")

    assert result.returncode != 0, result.stdout + result.stderr
    assert _flat("Claude MCP registration conflict") in _flat(result.stdout + result.stderr)
    assert sorted(p.name for p in state.iterdir()) == ["marginalia", "okto-neuron"]


def test_running_daemon_still_requires_a_connected_entry(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    _seed_mcp_entry(state, "marginalia", _TOKEN)

    failed = _run_wiring(tmp_path, server_started=True, status="failed")
    assert failed.returncode != 0, failed.stdout + failed.stderr
    assert _flat("did not verify as a connected user-scope endpoint") in _flat(
        failed.stdout + failed.stderr
    )
    assert (state / "marginalia").exists()

    (state / "okto-neuron").unlink()
    connected = _run_wiring(tmp_path, server_started=True, status="connected")
    assert connected.returncode == 0, connected.stdout + connected.stderr
    assert sorted(p.name for p in state.iterdir()) == ["okto-neuron"]
    assert "connects on the next" not in connected.stdout
