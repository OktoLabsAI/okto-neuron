"""Guards for the public one-shot installer (install.sh at the repository root).

The public installer starts the application with no vault by default. An explicitly
requested preseed may route LLM setup through `okto-neuron onboard`, rather than
hand-writing YAML or prompting for a raw api_base (a distribution rule).
These are content assertions on the script text: the real end-to-end proof is the
raw-URL Docker+tmux install test (bin/test-install.sh), which is a surfaced manual step.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
RELEASE_ARTIFACT_GATE = REPO_ROOT / ".github" / "workflows" / "release-artifact-gate.yml"


def _script() -> str:
    return INSTALL_SH.read_text(encoding="utf-8")


def test_public_installer_is_application_first_without_an_implicit_vault() -> None:
    text = _script()

    assert 'VAULT="${OKTO_NEURON_VAULT:-}"' in text
    assert 'VAULT="${OKTO_NEURON_VAULT:-mynotes}"' not in text
    assert 'elif [ -z "${VAULT}" ]; then' in text
    assert "no vault preseed requested" in text
    assert "create and manage them in the Web UI" in text


def test_public_installer_only_onboards_an_explicit_preseed() -> None:
    text = _script()

    preseed_guard = text.index('elif [ -z "${VAULT}" ]; then')
    onboard = text.index('ONBOARD=(okto-neuron onboard --vault "${VAULT}")')
    assert preseed_guard < onboard
    assert "OKTO_NEURON_PACKS requires OKTO_NEURON_VAULT" not in text
    assert "${name} requires OKTO_NEURON_VAULT" in text


def test_public_installer_rejects_orphaned_preseed_overrides() -> None:
    text = _script()

    required = {
        "OKTO_NEURON_PACKS",
        "OKTO_NEURON_LLM_PROVIDER",
        "OKTO_NEURON_LLM_API_BASE",
        "OKTO_NEURON_LLM_MODEL",
        "OKTO_NEURON_LLM_API_KEY_ENV",
        "OKTO_NEURON_LLM_SKIP_DISCOVERY",
        "OKTO_NEURON_LLM_ALLOW_REMOTE",
        "OKTO_NEURON_ALLOW_REMOTE_LLM",
        "OKTO_NEURON_ONBOARD_NONINTERACTIVE",
    }
    validation = text[text.index("validate_preseed_inputs()") : text.index("open_application_ui()")]
    assert required <= set(validation.split())
    assert "validate_preseed_inputs\n" in text


def test_release_artifact_pid_guard_has_no_orphaned_onboarding_preseed() -> None:
    workflow = RELEASE_ARTIFACT_GATE.read_text(encoding="utf-8")
    guard = workflow.split(
        "      - name: Refuse unverified live PID records before activation\n", 1
    )[1].split("      - name:", 1)[0]

    assert "OKTO_NEURON_NO_SERVE=1" in guard
    assert "OKTO_NEURON_NO_MCP=1" in guard
    assert "OKTO_NEURON_ONBOARD_NONINTERACTIVE" not in guard
    assert "OKTO_NEURON_LLM_" not in guard
    for required in (
        "okto-neuron-extra-venvs/serve/bin/python",
        "from okto_neuron.server.lifecycle import PidFile",
        'payload["version"]',
        'payload["pid"]',
        'payload["start_token"]',
        'payload["owner_id"]',
        "validate_guard_record",
    ):
        assert required in guard
    assert "sleep 300" not in guard
    assert 'printf \'%s\\n\' "$GUARD_PID" > "$PID_FILE"' not in guard


def test_public_installer_reads_legacy_and_versioned_pid_records(tmp_path: Path) -> None:
    helpers = _script().split("trap installer_exit EXIT", 1)[0]
    helper_path = tmp_path / "installer-helpers.sh"
    helper_path.write_text(helpers, encoding="utf-8")
    record = tmp_path / "server.pid"
    env = {name: value for name, value in os.environ.items() if not name.startswith("OKTO_NEURON_")}

    for payload in (
        "4242\n",
        '{"version":1,"pid":4242,"start_token":"token","owner_id":"owner"}\n',
    ):
        record.write_text(payload, encoding="utf-8")
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; VERIFY_PYTHON="$2"; read_server_pid "$3"',
                "bash",
                str(helper_path),
                sys.executable,
                str(record),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip() == "4242"


def test_public_installer_uses_complete_serve_extra() -> None:
    text = _script()
    assert 'EXTRAS="serve,litellm"' in text
    assert 'EXTRAS="embeddings,ladybug,mcp,litellm"' not in text


def test_public_installer_does_not_hand_write_llm_yaml() -> None:
    text = _script()
    # The old flow appended an `allow_remote: true` llm: block directly to
    # okto-neuron.yaml. onboard owns that now and defaults allow_remote off.
    assert "allow_remote: true" not in text
    assert "llm:\n  allow_remote" not in text


def test_public_installer_does_not_prompt_for_raw_api_base() -> None:
    text = _script()
    # No free-text api_base / model prompts — provider-first onboard replaces them.
    assert "LLM api_base URL" not in text
    assert "pick_model" not in text


def test_public_installer_does_not_export_placeholder_llm_key() -> None:
    text = _script()
    # The app injects a keyless placeholder itself; the installer must not export a
    # process-wide placeholder LLM key (a distribution rule).
    assert "sk-no-key-required" not in text
    assert "export OPENAI_API_KEY" not in text


def test_public_installer_gates_remote_llm_behind_explicit_optin() -> None:
    text = _script()
    # Non-loopback endpoints require explicit opt-in; never inferred from the URL.
    assert "OKTO_NEURON_ALLOW_REMOTE_LLM" in text
    assert "--allow-remote-llm" in text


def test_public_installer_stops_the_verified_lock_owner_without_hiding_failure() -> None:
    text = _script()

    assert 'OLD_LOCK_ROOT="$(find_daemon_lock_root "${OLD_PID}"' in text
    assert 'STOP_COMMAND=("${STAGE_CLI}" stop --timeout 30)' in text
    assert 'if ! "${STOP_COMMAND[@]}"; then' in text
    assert "still draining after 120s" not in text


def test_public_installer_migrates_only_a_verified_v0040_vault_lifecycle() -> None:
    text = _script()

    assert 'STATUS_VAULT="$(json_value "${STATUS_JSON}" vault_path)"' in text
    assert '[ "${PREVIOUS_VERSION}" != "0.0.40" ]' in text
    assert '[ "${STATUS_VERSION}" != "0.0.40" ]' in text
    assert 'find_verified_legacy_lock_root "${OLD_PID}" "${STATUS_VAULT}"' in text
    assert 'PREVIOUS_DAEMON_VAULT="${STATUS_VAULT}"' in text
    assert (
        'STOP_COMMAND=("${STAGE_CLI}" stop --vault "${PREVIOUS_DAEMON_VAULT}" --timeout 30)' in text
    )
    assert "SERVE_ARGS=(serve --daemon --no-open)" in text
    assert '[ -n "${LEGACY_DAEMON}" ] && [ -n "${PREVIOUS_DAEMON_VAULT}" ]' in text
    assert 'SERVE_ARGS+=(--vault "${PREVIOUS_DAEMON_VAULT}")' in text
    assert 'okto-neuron "${SERVE_ARGS[@]}"' in text


def test_public_installer_refuses_an_unidentified_live_v0040_vault_daemon() -> None:
    text = _script()

    assert 'UNVERIFIED_LEGACY_DAEMON="$(find_unverified_live_legacy_daemon)"' in text
    assert "vault-scoped PID record" in text
    # A 0.0.40 daemon belongs to the pre-0.3.0 tool, so the hint names its command.
    assert 'marginalia stop --vault \\"${UNVERIFIED_VAULT}\\"' in text
    assert "update aborted before replacing the installed tool" in text


def test_public_installer_rolls_back_with_predecessor_capabilities() -> None:
    text = _script()

    rollback = text[text.index("restart_previous_daemon()") : text.index("stop_candidate_daemon()")]
    assert "serve --help 2>/dev/null | grep -q -- '--no-open'" in rollback
    assert "restart_args+=(--no-open)" in rollback
    assert 'restart_args+=(--vault "${PREVIOUS_DAEMON_VAULT}")' in rollback
    assert 'daemon_version "${restart_command}" "${PREVIOUS_DAEMON_VAULT}"' in rollback


def test_public_installer_preserves_a_stopped_zero_vault_install() -> None:
    text = _script()

    detection = text[text.index("elif port_in_use; then") : text.index("# ── 3. install")]
    assert 'elif [ -n "${PREVIOUS_TOOL_NAME}" ] || ! is_greenfield_home; then' in detection
    assert 'UPGRADE="1"' in detection
    assert "Existing Okto Neuron install detected (daemon not running)" in detection
    assert 'elif [ -n "${UPGRADE}" ] && [ -z "${WAS_RUNNING}" ]; then' in text
    assert "Preserving stopped daemon state" in text


def test_public_installer_uses_application_scoped_daemon_lifecycle() -> None:
    text = _script()

    assert 'DAEMON_PID_FILE="${DAEMON_RUNTIME_ROOT}/.marginalia/server.pid"' in text
    assert 'DAEMON_TOKEN_FILE="${HOME_ROOT}/daemon-7777.token"' in text
    assert "SERVE_ARGS=(serve --daemon --no-open)" in text
    assert 'okto-neuron "${SERVE_ARGS[@]}"' in text
    assert 'SERVE_ARGS+=(--vault "${PREVIOUS_DAEMON_VAULT}")' in text
    assert "/vaults/*/.marginalia/server.pid" not in text
    assert "/vaults/*/.marginalia/daemon.token" not in text


def test_public_installer_opens_only_the_committed_verified_application() -> None:
    text = _script()

    serve = text.index('okto-neuron "${SERVE_ARGS[@]}"')
    version_ready = text.index('SERVER_STARTED="1"', serve)
    committed = text.index('ACTIVATION_COMMITTED="1"', version_ready)
    open_call = text.index('open_application_ui "${REST_URL}/"', committed)

    assert serve < version_ready < committed < open_call
    assert "OKTO_NEURON_NO_OPEN=1    don't open the verified local UI" in text
    assert '[ "${OKTO_NEURON_NO_OPEN:-}" != "1" ]' in text
    assert "?token=" not in text
    workflow = RELEASE_ARTIFACT_GATE.read_text(encoding="utf-8")
    assert 'OKTO_NEURON_NO_OPEN: "1"' in workflow


def test_public_installer_browser_opener_uses_plain_url(
    tmp_path: Path,
) -> None:
    helpers = _script().split("trap installer_exit EXIT", 1)[0]
    helper_path = tmp_path / "installer-helpers.sh"
    helper_path.write_text(helpers, encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "opened"
    opener = fake_bin / "xdg-open"
    opener.write_text(
        '#!/bin/sh\nprintf "%s" "$1" > "$OPEN_MARKER"\n',
        encoding="utf-8",
    )
    opener.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["OPEN_MARKER"] = str(marker)

    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; uname() { printf Linux; }; '
            'open_application_ui "http://127.0.0.1:7777/"; wait',
            "bash",
            str(helper_path),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text(encoding="utf-8") == "http://127.0.0.1:7777/"


def test_public_installer_requires_an_independent_expected_wheel_version(
    tmp_path: Path,
) -> None:
    text = _script()
    wheel_mode = text.index('if [ "${CANDIDATE_KIND}" = "wheel" ]; then')
    requirement_call = text.index(
        'require_expected_wheel_version "${EXPECTED_VERSION}"', wheel_mode
    )
    candidate_fetch = text.index(
        'fetch_file "${WHEEL_SOURCE}" "${CANDIDATE_WHEEL}"', requirement_call
    )
    assert wheel_mode < requirement_call < candidate_fetch

    helpers = text.split("trap installer_exit EXIT", 1)[0]
    helper_path = tmp_path / "installer-helpers.sh"
    helper_path.write_text(helpers, encoding="utf-8")

    missing = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; require_expected_wheel_version ""',
            "bash",
            str(helper_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert missing.returncode != 0
    assert (
        "wheel verification requires a manifest version or OKTO_NEURON_EXPECTED_VERSION"
    ) in missing.stderr

    pinned = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; require_expected_wheel_version "0.0.41"',
            "bash",
            str(helper_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert pinned.returncode == 0, pinned.stdout + pinned.stderr


def test_public_installer_rejects_successful_but_local_failed_mcp_get(
    tmp_path: Path,
) -> None:
    helpers = _script().split("trap installer_exit EXIT", 1)[0]
    helper_path = tmp_path / "installer-helpers.sh"
    helper_path.write_text(helpers, encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_claude = fake_bin / "claude"
    fake_claude.write_text(
        """#!/usr/bin/env bash
if [ "$1 $2 $3" = "mcp get marginalia" ]; then
  cat <<'EOF'
marginalia:
  Scope: Local config (private to you in this project)
  Status: ✘ Failed to connect
  Type: http
  URL: http://127.0.0.1:8201/mcp
EOF
  exit 0
fi
exit 1
""",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"

    result = subprocess.run(
        [
            "bash",
            "-c",
            """
source "$1"
output="$(claude mcp get marginalia 2>&1)"
! claude_mcp_registration_matches "$output" "http://127.0.0.1:8201/mcp"
test "$(claude_mcp_registration_scope "$output")" = local
""",
            "bash",
            str(helper_path),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_public_installer_accepts_only_connected_matching_user_mcp_get(
    tmp_path: Path,
) -> None:
    helpers = _script().split("trap installer_exit EXIT", 1)[0]
    helper_path = tmp_path / "installer-helpers.sh"
    helper_path.write_text(helpers, encoding="utf-8")
    connected_user = """marginalia:
  Scope: User config (available in all your projects)
  Status: ✓ Connected
  Type: http
  URL: http://127.0.0.1:8201/mcp
"""
    invalid_outputs = (
        connected_user.replace("Status: ✓ Connected", "Status: Not Connected"),
        f"{connected_user}  Scope: Local config\n",
        f"{connected_user}  Status: Not Connected\n",
        f"{connected_user}  Type: stdio\n",
        f"{connected_user}  URL: http://127.0.0.1:9999/mcp\n",
        connected_user.replace(
            "Scope: User config (available in all your projects)",
            "Scope: User config unexpected",
        ),
        connected_user.replace("Type: http", "Type: http unexpected"),
        connected_user.replace(
            "URL: http://127.0.0.1:8201/mcp",
            "URL: http://127.0.0.1:8201/mcp unexpected",
        ),
    )

    for output, should_match in (
        (connected_user, True),
        *[(invalid, False) for invalid in invalid_outputs],
    ):
        result = subprocess.run(
            [
                "bash",
                "-c",
                'source "$1"; claude_mcp_registration_matches "$2" "$3"',
                "bash",
                str(helper_path),
                output,
                "http://127.0.0.1:8201/mcp",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert (result.returncode == 0) is should_match, result.stdout + result.stderr


# -- Marginalia -> Okto Neuron upgrade path (0.3.0) ---------------------------


def test_public_installer_bakes_the_okto_neuron_release_manifest() -> None:
    text = _script()

    assert (
        "https://github.com/OktoLabsAI/okto-neuron/releases/download/v0.3.0/"
        "okto_neuron-0.3.0-py3-none-any.whl"
    ) in text
    assert "https://raw.githubusercontent.com/OktoLabsAI/okto-neuron/main/release-manifest.json" in text
    assert 'EXPECTED_VERSION="${OKTO_NEURON_EXPECTED_VERSION:-0.3.0}"' in text
    assert "verified wheel SHA-256" in text
    assert "marginalia-dist" not in text


def test_public_installer_replaces_the_marginalia_tool_and_can_restore_it_exactly() -> None:
    text = _script()

    detect = text[text.index("detect_previous_tool() {") : text.index("UPGRADE=\"\"\nOLD_PID")]
    assert 'PREVIOUS_TOOL_NAME="${LEGACY_TOOL_NAME}"' in detect
    assert "import okto_neuron._compat" in detect  # the MVP okto-neuron tool is never replaced
    # The old daemon is stopped by the old command, which owns ~/.marginalia.
    assert 'STOP_COMMAND=("${PREVIOUS_COMMAND}" stop --timeout 30)' in text
    # Activation moves the previous tool aside under its own name; rollback restores it.
    assert 'mv "${TOOL_ROOT}/${PREVIOUS_TOOL_NAME}" "${BACKUP_ROOT}/tool"' in text
    assert 'mv "${BACKUP_ROOT}/tool" "${TOOL_ROOT}/${previous_tool}"' in text
    assert 'LAUNCHERS="okto-neuron kg marginalia"' in text
    assert "undo_home_migration || return 1" in text


def test_public_installer_migrates_app_files_but_never_vaults() -> None:
    text = _script()

    assert '"${TOOL_BIN}/${CLI}" migrate-home --json' in text
    assert 'LEGACY_HOME_ROOT="${HOME}/.marginalia"' in text
    assert '[ -d "${LEGACY_HOME_ROOT}/vaults" ] && VAULT_ROOT="${LEGACY_HOME_ROOT}/vaults"' in text
    migrate = text[text.index("migrate_app_home() {") : text.index("undo_home_migration() {")]
    assert "mv " not in migrate and "rm " not in migrate


def test_public_installer_honours_legacy_env_names_with_a_warning() -> None:
    text = _script()

    shim = text[text.index("import_legacy_env() {") : text.index("# ── config")]
    assert "grep '^MARGINALIA_'" in shim
    assert 'name="OKTO_NEURON_${legacy#MARGINALIA_}"' in shim
    assert "is deprecated; using it as" in shim
    assert text.index("import_legacy_env\n") < text.index("# ── config")


def test_public_installer_replaces_legacy_mcp_only_after_the_new_entry_verifies() -> None:
    import re

    text = _script()

    # The only registration the installer ever removes is the pre-0.3.0
    # `marginalia` entry, and only inside remove_legacy_mcp. It never removes
    # an `okto-neuron` entry.
    removals = re.findall(r"(?m)^\s*(?:if\s+(?:!\s+)?|elif\s+|!\s+)?claude\s+mcp\s+remove\b.*$", text)
    assert removals == [
        '  if ! claude mcp remove "${LEGACY_CLI}" --scope "${scope}" >/dev/null 2>&1; then'
    ], removals
    remover = text[text.index("remove_legacy_mcp() {") : text.index('if [ "${OKTO_NEURON_NO_MCP:-}" = "1" ]; then')]
    # The old entry is only removed when it pointed at this same endpoint, and
    # the removal is recorded for the rollback before anything else can fail.
    assert remover.index('"${GLOBAL_URL}"') < remover.index("claude mcp remove")
    assert remover.index("claude mcp remove") < remover.index('LEGACY_MCP_REMOVED_SCOPE="${scope}"')

    wiring = text[text.index("LEGACY_MCP_SCOPE=") : text.index("# The daemon itself stays headless")]
    # Remove only after register_mcp verified the new entry as connected, in the same scope.
    assert 'register_mcp local && remove_legacy_mcp local "${LEGACY_MCP_OUTPUT}"' in wiring
    assert 'if register_mcp user && [ "${LEGACY_MCP_SCOPE}" = "user" ]; then' in wiring
    assert 'remove_legacy_mcp user "${LEGACY_MCP_OUTPUT}"' in wiring
    assert "a project-scope '${LEGACY_CLI}' entry (.mcp.json) was left unchanged" in wiring

    # Rollback: any failing exit after the removal re-adds the old entry as it was.
    restore = text[text.index("restore_legacy_mcp() {") : text.index("installer_exit() {")]
    assert 'claude mcp add --scope "${scope}" --transport http' in restore
    assert '"${LEGACY_CLI}" "${GLOBAL_URL}"' in restore
    exit_handler = text[text.index("installer_exit() {") : text.index("trap installer_exit EXIT")]
    assert 'if [ "${rc}" -ne 0 ]; then\n    restore_legacy_mcp || rc=1' in exit_handler


def test_legacy_mcp_rollback_readds_the_removed_entry(tmp_path: Path) -> None:
    """Run installer_exit with a fake `claude` after a recorded removal."""
    helpers = tmp_path / "helpers.sh"
    text = _script()
    helpers.write_text(text[: text.index("trap installer_exit EXIT")], encoding="utf-8")
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    calls = tmp_path / "claude.calls"
    (fake_bin / "claude").write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{calls}"\n', encoding="utf-8"
    )
    (fake_bin / "claude").chmod(0o755)
    script = f"""
set -euo pipefail
source "{helpers}"
GLOBAL_URL=http://127.0.0.1:8201/mcp
AUTH_TOKEN=secret-token
TOKEN_FILE=/nonexistent
LEGACY_MCP_REMOVED_SCOPE=user
(exit 3) || installer_exit
"""
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"},
    )
    assert result.returncode == 3, result.stderr
    assert calls.read_text(encoding="utf-8").splitlines() == [
        "mcp add --scope user --transport http marginalia http://127.0.0.1:8201/mcp "
        "--header Authorization: Bearer secret-token"
    ]
    assert "re-added the old 'marginalia' user-scope Claude MCP entry" in result.stdout


def test_public_installer_shell_scripts_pass_bash_syntax_check() -> None:
    for path in (REPO_ROOT / "install.sh", REPO_ROOT / "bin" / "test-install.sh"):
        result = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
