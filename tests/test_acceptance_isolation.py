from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS_DIR = REPO_ROOT / "tests" / "acceptance" / "scenarios"
SCENARIOS = tuple(sorted(SCENARIOS_DIR.glob("[0-9][0-9]_*.sh")))
LIB = SCENARIOS_DIR / "_lib.sh"
PREAMBLE = SCENARIOS_DIR / "_preamble.sh"

_PRODUCT_COMMAND = re.compile(
    r"^\s*(?:(?:if|elif|while|until)\s+)?(?:!\s*)?(?:\(\s*)?"
    r"(?:kg|marginalia)\s+"
    r"(?P<command>add|query|serve|start|stop|onboard|init|detect-drift)\b"
)


def _logical_lines(text: str) -> list[str]:
    return re.sub(r"\\\n[ \t]*", " ", text).splitlines()


def _product_commands(path: Path) -> list[tuple[str, str]]:
    commands: list[tuple[str, str]] = []
    for line in _logical_lines(path.read_text(encoding="utf-8")):
        if line.lstrip().startswith("#"):
            continue
        match = _PRODUCT_COMMAND.match(line)
        if match is not None:
            commands.append((match.group("command"), line.strip()))
    return commands


def test_shared_lib_isolates_user_config_and_implicit_daemon_state(tmp_path: Path) -> None:
    caller_home = tmp_path / "caller-home"
    caller_home.mkdir()
    caller_config = caller_home / "okto-neuron.toml"
    original_config = (
        "marginalia_toml_version = 1\n"
        f'vault_roots = ["{caller_home / "vaults"}"]\n'
        f'default_vault = "{caller_home / "vaults" / "original"}"\n'
    )
    caller_config.write_text(original_config, encoding="utf-8")
    caller_env_file = caller_home / "env"
    caller_env_file.write_text("OKTO_NEURON_PROVIDER_TEST_API_KEY=keep-me\n", encoding="utf-8")

    acceptance_root = tmp_path / "acceptance"
    work_dir = acceptance_root / "isolation_probe"
    report = acceptance_root / "report.jsonl"
    corpus = tmp_path / "private-corpus"
    corpus.mkdir()

    script = r"""
set -euo pipefail
source "$1"
"$2" - "$SCENARIO_WORK_DIR/vault" <<'PY'
import sys
from okto_neuron.vault_registry import set_default_vault

set_default_vault(sys.argv[1])
PY
"$2" - <<'PY'
import json
import os

keys = (
    "HOME",
    "OKTO_NEURON_ACCEPTANCE_CALLER_HOME",
    "OKTO_NEURON_CONFIG",
    "OKTO_NEURON_ENV_FILE",
    "OKTO_NEURON_VAULT",
    "OKTO_NEURON_ENDPOINT",
    "OKTO_NEURON_MCP_ENDPOINT",
    "OKTO_NEURON_AUTH_TOKEN",
    "UV_PROJECT_ENVIRONMENT",
    "VIRTUAL_ENV",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)
print(json.dumps({key: os.environ.get(key) for key in keys}, sort_keys=True))
PY
"""
    inherited = os.environ.copy()
    inherited.update(
        {
            "HOME": str(caller_home),
            "OKTO_NEURON_ACCEPTANCE_DIR": str(acceptance_root),
            "SCENARIO_WORK_DIR": str(work_dir),
            "ACCEPTANCE_REPORT": str(report),
            "OKTO_NEURON_PRIVATE_CORPUS": str(corpus),
            "OKTO_NEURON_CONFIG": str(caller_config),
            "OKTO_NEURON_ENV_FILE": str(caller_env_file),
            "OKTO_NEURON_VAULT": str(caller_home / "vaults" / "real"),
            "OKTO_NEURON_ENDPOINT": "http://127.0.0.1:7777",
            "OKTO_NEURON_MCP_ENDPOINT": "http://127.0.0.1:8201/mcp",
            "OKTO_NEURON_AUTH_TOKEN": "do-not-inherit",
            "UV_PROJECT_ENVIRONMENT": str(REPO_ROOT / ".venv"),
            "VIRTUAL_ENV": str(REPO_ROOT / ".venv"),
            "REPO_ROOT": str(REPO_ROOT),
            "SCENARIO_NAME": "isolation_probe",
        }
    )
    result = subprocess.run(
        ["bash", "-c", script, "acceptance-isolation", str(LIB), sys.executable],
        cwd=REPO_ROOT,
        env=inherited,
        check=True,
        capture_output=True,
        text=True,
    )
    state = json.loads(result.stdout.strip().splitlines()[-1])

    isolated_home = work_dir / "home"
    assert state["HOME"] == str(isolated_home)
    assert state["OKTO_NEURON_ACCEPTANCE_CALLER_HOME"] == str(caller_home)
    assert state["OKTO_NEURON_CONFIG"] == str(isolated_home / ".okto-neuron/okto-neuron.toml")
    assert state["OKTO_NEURON_ENV_FILE"] == str(isolated_home / ".okto-neuron/env")
    assert state["OKTO_NEURON_VAULT"] == str(work_dir / "vault")
    assert state["OKTO_NEURON_AUTH_TOKEN"] is None
    assert state["OKTO_NEURON_ENDPOINT"] == "http://127.0.0.1:0"
    assert state["OKTO_NEURON_MCP_ENDPOINT"] == "http://127.0.0.1:0/mcp"
    assert state["UV_PROJECT_ENVIRONMENT"] == str(acceptance_root / ".venv")
    assert state["VIRTUAL_ENV"] is None
    assert state["XDG_CONFIG_HOME"] == str(isolated_home / ".config")
    assert state["XDG_DATA_HOME"] == str(isolated_home / ".local/share")
    assert state["XDG_STATE_HOME"] == str(isolated_home / ".local/state")

    assert caller_config.read_text(encoding="utf-8") == original_config
    assert caller_env_file.read_text(encoding="utf-8") == (
        "OKTO_NEURON_PROVIDER_TEST_API_KEY=keep-me\n"
    )
    sandbox_config = Path(state["OKTO_NEURON_CONFIG"])
    assert sandbox_config.is_file()
    assert str(work_dir / "vault") in sandbox_config.read_text(encoding="utf-8")


def test_every_scenario_enters_isolation_before_product_commands() -> None:
    assert SCENARIOS
    source = 'source "$SCRIPT_DIR/_lib.sh"'
    for path in SCENARIOS:
        lines = _logical_lines(path.read_text(encoding="utf-8"))
        source_lines = [index for index, line in enumerate(lines) if source in line]
        assert source_lines, f"{path.name} does not source _lib.sh"
        product_lines = [
            index
            for index, line in enumerate(lines)
            if not line.lstrip().startswith("#") and _PRODUCT_COMMAND.match(line)
        ]
        assert not product_lines or source_lines[0] < product_lines[0], (
            f"{path.name} invokes Okto Neuron before _lib.sh establishes isolation"
        )

    for name in ("90_private_corpus_ingest.sh", "91_private_corpus_qa_dogfooding.sh"):
        text = (SCENARIOS_DIR / name).read_text(encoding="utf-8")
        assert 'export OKTO_NEURON_VAULT="$VAULT"' in text


def test_acceptance_clients_never_use_an_implicit_endpoint() -> None:
    for path in SCENARIOS:
        for command, line in _product_commands(path):
            if command in {"add", "query", "detect-drift"}:
                assert "--endpoint" in line, f"implicit endpoint in {path.name}: {line}"


def test_acceptance_never_activates_or_syncs_the_repository_venv() -> None:
    shell_files = (*SCENARIOS_DIR.glob("*.sh"), REPO_ROOT / "bin" / "acceptance.sh")
    for path in shell_files:
        text = path.read_text(encoding="utf-8")
        assert "$REPO_ROOT/.venv" not in text, f"repository venv referenced in {path}"

    lib_text = LIB.read_text(encoding="utf-8")
    assert 'export UV_PROJECT_ENVIRONMENT="$acceptance_root/.venv"' in lib_text
    assert "unset VIRTUAL_ENV" in lib_text
    activation = 'source "$UV_PROJECT_ENVIRONMENT/bin/activate"'
    assert activation in PREAMBLE.read_text(encoding="utf-8")
    scenario_zero = (SCENARIOS_DIR / "00_cold_install.sh").read_text(encoding="utf-8")
    assert 'source "$SCRIPT_DIR/_preamble.sh"' in scenario_zero


def test_acceptance_provisions_and_enforces_cpython_312() -> None:
    text = PREAMBLE.read_text(encoding="utf-8")
    assert "uv_sync_args=(--quiet --python 3.12)" in text
    assert "sys.version_info[:2] == (3, 12)" in text
    assert '"$work_dir/python-version.log"' in text


def test_realmodel_acceptance_requires_explicit_endpoint_and_defaults_exact_model(
    tmp_path: Path,
) -> None:
    scenario = SCENARIOS_DIR / "56_remember_anchored_claims.sh"
    text = scenario.read_text(encoding="utf-8")
    assert 'LLM_BASE="${OKTO_NEURON_LLM_BASE_URL:-}"' in text
    assert 'LLM_MODEL="${OKTO_NEURON_REALMODEL_MODEL:-unsloth/Qwen3.6-27B-NVFP4}"' in text
    assert "--allow-remote-llm --yes" in text
    assert "127.0.0.1:8123" not in text

    env = os.environ.copy()
    env.pop("OKTO_NEURON_LLM_BASE_URL", None)
    env["OKTO_NEURON_ACCEPTANCE_DIR"] = str(tmp_path / "acceptance")
    result = subprocess.run(
        ["bash", str(scenario)],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "OKTO_NEURON_LLM_BASE_URL is required" in result.stderr
    report = json.loads((tmp_path / "acceptance" / "report.jsonl").read_text(encoding="utf-8"))
    assert report["status"] == "regression"
    assert report["failures"] == ["missing_prerequisite env=OKTO_NEURON_LLM_BASE_URL"]


def test_private_corpus_probes_are_caller_supplied_not_embedded() -> None:
    text = (SCENARIOS_DIR / "90_private_corpus_ingest.sh").read_text(encoding="utf-8")
    assert "OKTO_NEURON_PRIVATE_CORPUS_PROBES" in text
    assert "while IFS=$'\\t' read -r query expected extra" in text
    assert 'PROBES+=("$query::$expected")' in text
    assert "cohort comparison" not in text.lower()


def test_private_corpus_questions_are_caller_supplied_not_embedded() -> None:
    text = (SCENARIOS_DIR / "91_private_corpus_qa_dogfooding.sh").read_text(encoding="utf-8")
    assert "OKTO_NEURON_PRIVATE_CORPUS_QUESTIONS" in text
    assert "while IFS= read -r question" in text
    assert 'QUESTIONS+=("$question")' in text
    assert "what did" not in text.lower()


def test_acceptance_servers_never_use_default_ports() -> None:
    default_port = re.compile(r"(?<![0-9])(?:7777|8201)(?![0-9])")
    for path in SCENARIOS:
        executable_lines = [
            line
            for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ]
        assert default_port.search("\n".join(executable_lines)) is None, path.name
        for command, line in _product_commands(path):
            if command == "serve":
                assert "--vault" in line, f"implicit vault in {path.name}: {line}"
                assert "--port" in line, f"implicit REST port in {path.name}: {line}"
                assert "--mcp-port" in line, f"implicit MCP port in {path.name}: {line}"
                assert path.name == "93_security_hardening.sh", (
                    f"successful server lifecycle must use start_server in {path.name}: {line}"
                )
                assert "--host 0.0.0.0" in line and "--port 0" in line

    preamble = PREAMBLE.read_text(encoding="utf-8")
    assert "reserved = {7777, 8201}" in preamble
    assert "if port not in reserved:" in preamble


def test_server_readiness_is_credential_free_and_bound_to_spawned_identity() -> None:
    text = PREAMBLE.read_text(encoding="utf-8")
    assert 'endpoint.rstrip("/") + "/api/v1/status"' in text
    assert 'payload.get("pid") != int(expected_pid_raw)' in text
    assert "actual_vault != expected_vault" in text

    rest_identity_check = text.split(
        "endpoint, expected_pid_raw, expected_vault_raw = sys.argv[1:]", 1
    )[1].split("expected_vault =", 1)[0]
    assert "Authorization" not in rest_identity_check
    assert "OKTO_NEURON_AUTH_TOKEN" not in rest_identity_check

    assert "daemon-${rest_port}.token" in text
    assert "FastMCP remains bearer-protected" in text
    assert "export OKTO_NEURON_AUTH_TOKEN" in text


def test_rest_acceptance_probes_never_send_the_mcp_bearer() -> None:
    for name in (
        "00_cold_install.sh",
        "90_private_corpus_ingest.sh",
        "92_eval_recall_provenance.sh",
        "93_security_hardening.sh",
    ):
        text = (SCENARIOS_DIR / name).read_text(encoding="utf-8")
        assert "Authorization: Bearer" not in text, name


def test_security_acceptance_proves_direct_ui_and_browser_write_defenses() -> None:
    text = (SCENARIOS_DIR / "93_security_hardening.sh").read_text(encoding="utf-8")

    assert "direct UI/REST needs no browser credential" in text
    assert "set-cookie:" in text.lower()
    assert "forbidden_origin" in text
    assert "unsupported_media_type" in text


def test_mcp_acceptance_proves_denial_before_bearer_success() -> None:
    text = (SCENARIOS_DIR / "40_mcp_serve.sh").read_text(encoding="utf-8")

    unauthenticated = text.index("MCP refuses a request without")
    bearer = text.index("MCP accepts the application bearer")
    assert unauthenticated < bearer
    assert 'assert_exit_code 401 "$unauth_code"' in text
    assert "async with Client(URL, auth=TOKEN)" in text


def test_restart_acceptance_uses_application_scoped_lifecycle() -> None:
    text = (SCENARIOS_DIR / "96_server_restart_mid_session.sh").read_text(encoding="utf-8")

    assert 'PID_FILE="$HOME/.okto-neuron/runtime/.marginalia/server.pid"' in text
    assert text.count('validate_pid_record "$PID_FILE"') == 2
    for field in ("version", "pid", "start_token", "owner_id"):
        assert f'payload["{field}"]' in text
    assert "lifecycle._process_start_token(expected_pid)" in text
    assert "pid_owner_id_reused_across_restart" in text
    assert 'pre_stop_pid=$(cat "$PID_FILE"' not in text
    assert "okto-neuron stop --vault" not in text
    assert text.count("okto-neuron stop") >= 2
