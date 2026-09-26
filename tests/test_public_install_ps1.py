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
