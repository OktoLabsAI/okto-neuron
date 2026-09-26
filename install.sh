#!/usr/bin/env bash
#
# Okto Neuron one-shot installer.
#
#   curl -fsSL https://raw.githubusercontent.com/OktoLabsAI/okto-neuron/main/install.sh | bash
#
# Takes a fresh machine from zero to a running Okto Neuron application wired into
# Claude Code: prereqs → install tool → serve/open the app → register MCP.
# Vault creation and provider setup are application-first by default. Automation
# may explicitly preseed one vault and run CLI onboarding with OKTO_NEURON_VAULT.
#
# Upgrading from Marginalia (the pre-0.3.0 name of this product): the installer
# finds the `marginalia` uv tool, stops its daemon with that tool's own command,
# installs okto-neuron in its place, copies app-level files from ~/.marginalia to
# ~/.okto-neuron (vaults are never moved, D5), and re-registers a user-scope
# `marginalia` Claude MCP entry as `okto-neuron`. Any failure before the new
# version is verified restores the exact previous `marginalia` tool.
# Every OKTO_NEURON_* variable below can still be given under its pre-0.3.0
# MARGINALIA_* name (read with a warning) when the OKTO_NEURON_* one is unset.
#
# Everything is overridable by environment variable so the SAME script works
# piped to bash (non-interactive) and run from a clone (interactive):
#
#   OKTO_NEURON_SRC          path to an existing checkout (skips clone)
#   OKTO_NEURON_WHEEL        path/URL to a built wheel (skips clone+source build)
#   OKTO_NEURON_EXPECTED_VERSION required version when overriding the wheel
#   OKTO_NEURON_MANIFEST     release-manifest.json path/URL for wheel verification
#   OKTO_NEURON_WHEEL_SHA256 required SHA-256 for a custom wheel without a manifest
#   OKTO_NEURON_REPO         git URL to clone   (default: SSH source repo)
#   OKTO_NEURON_REF          git ref to check out (default: repo default branch)
#   OKTO_NEURON_VAULT        optional vault name to preseed before opening the app
#   OKTO_NEURON_PACKS        preseed type packs (requires OKTO_NEURON_VAULT)
#   OKTO_NEURON_LLM_PROVIDER preseed provider passed to `okto-neuron onboard`
#   OKTO_NEURON_LLM_API_BASE provider base URL
#   OKTO_NEURON_LLM_MODEL    model name (also skips model discovery)
#   OKTO_NEURON_LLM_API_KEY_ENV OKTO_NEURON_* environment variable holding the key
#   OKTO_NEURON_LLM_ALLOW_REMOTE=1 explicit opt-in for a non-loopback LLM endpoint
#   OKTO_NEURON_ONBOARD_NONINTERACTIVE=1 never prompt during onboarding
#   OKTO_NEURON_NO_SERVE=1   install + configure only; don't start the daemon
#   OKTO_NEURON_NO_OPEN=1    don't open the verified local UI in a browser
#   OKTO_NEURON_NO_MCP=1     don't run `claude mcp add`
#
# Flags:
#   --no-onboard            skip the one-shot greenfield first-run onboarding prompt
#
set -euo pipefail

# Honour pre-0.3.0 MARGINALIA_* inputs (until 0.5): the new OKTO_NEURON_* name
# wins, the old one is read with one warning. Runs before any input is read.
import_legacy_env() {
  local legacy name
  for legacy in $(compgen -e | grep '^MARGINALIA_' || true); do
    name="OKTO_NEURON_${legacy#MARGINALIA_}"
    if [ -z "${!name+x}" ]; then
      export "${name}=${!legacy}"
      printf ' !! %s is deprecated; using it as %s\n' "${legacy}" "${name}" >&2
    elif [ "${!name}" != "${!legacy}" ]; then
      printf ' !! both %s and %s are set; using %s\n' "${name}" "${legacy}" "${name}" >&2
    fi
  done
}
import_legacy_env

# ── config ────────────────────────────────────────────────────────────────
# The release bakes the immutable GitHub Release wheel URL and version here
# (scripts/release_manifest.py prints the values), so `curl … | bash` needs no env.
# The wheel is always checked against release-manifest.json's SHA-256.
DEFAULT_WHEEL_URL="${OKTO_NEURON_DEFAULT_WHEEL_URL:-https://github.com/OktoLabsAI/okto-neuron/releases/download/v0.3.0/okto_neuron-0.3.0-py3-none-any.whl}"
DEFAULT_MANIFEST_URL="${OKTO_NEURON_DEFAULT_MANIFEST_URL:-https://raw.githubusercontent.com/OktoLabsAI/okto-neuron/main/release-manifest.json}"
EXPECTED_VERSION="${OKTO_NEURON_EXPECTED_VERSION:-0.3.0}"
EXTRAS="serve,litellm"
PY_VERSION="3.12"
REPO="${OKTO_NEURON_REPO:-https://github.com/OktoLabsAI/okto-neuron.git}"
REF="${OKTO_NEURON_REF:-}"
VAULT="${OKTO_NEURON_VAULT:-}"
PACKS="${OKTO_NEURON_PACKS:-core,research,personal}"
HOME_ROOT="${HOME}/.okto-neuron"
# Pre-0.3.0 app home. Its vaults stay where they are (document ids hash absolute
# paths), so an upgraded machine keeps creating named vaults there too.
LEGACY_HOME_ROOT="${HOME}/.marginalia"
VAULT_ROOT="${HOME_ROOT}/vaults"
[ -d "${LEGACY_HOME_ROOT}/vaults" ] && VAULT_ROOT="${LEGACY_HOME_ROOT}/vaults"
VAULT_DIR=""
if [ -n "${VAULT}" ]; then
  VAULT_DIR="${VAULT_ROOT}/${VAULT}"
fi
TOOL_NAME="okto-neuron"
CLI="okto-neuron"
LEGACY_TOOL_NAME="marginalia"
LEGACY_CLI="marginalia"
# Launchers the okto-neuron tool installs (marginalia is the warning alias).
LAUNCHERS="okto-neuron kg marginalia"
REST_URL="http://127.0.0.1:7777"
MCP_URL="http://127.0.0.1:8201/mcp"
DAEMON_TOKEN_FILE="${HOME_ROOT}/daemon-7777.token"
LEGACY_DAEMON_TOKEN_FILE="${LEGACY_HOME_ROOT}/daemon-7777.token"
DAEMON_RUNTIME_ROOT="${HOME_ROOT}/runtime"
# `.marginalia/` inside a lifecycle root is the kept state-directory name.
DAEMON_PID_FILE="${DAEMON_RUNTIME_ROOT}/.marginalia/server.pid"
LEGACY_DAEMON_RUNTIME_ROOT="${LEGACY_HOME_ROOT}/runtime"
LEGACY_DAEMON_PID_FILE="${LEGACY_DAEMON_RUNTIME_ROOT}/.marginalia/server.pid"

# Transaction state. The EXIT trap restores the exact prior uv tool if anything
# fails after activation begins; vault/provider configuration always remains
# outside the tool directory and is never replaced by the transaction.
WORK_TMP=""
CLONE_TMP=""
TOOL_ROOT=""
TOOL_BIN=""
BACKUP_ROOT=""
PREVIOUS_VERSION=""
PREVIOUS_COMMAND=""
PREVIOUS_DAEMON_VAULT=""
LEGACY_DAEMON=""
ACTIVATION_STARTED=""
ACTIVATION_COMMITTED=""
CANDIDATE_DAEMON_STARTED=""
WAS_RUNNING=""
SHUTDOWN_REQUESTED=""
# Set when the prior install is the pre-0.3.0 `marginalia` tool.
PRODUCT_UPGRADE=""
# Product name of the install being replaced, for rollback messages.
PREVIOUS_PRODUCT="Okto Neuron"
PREVIOUS_TOOL_NAME=""
HOME_MIGRATION_JSON=""

# ── pretty logging ────────────────────────────────────────────────────────
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; X=$'\033[0m'
else B=""; G=""; Y=""; R=""; X=""; fi
step() { printf "\n%s==>%s %s%s%s\n" "$B$G" "$X" "$B" "$1" "$X"; }
info() { printf "    %s\n" "$1"; }
warn() { printf "%s !! %s%s\n" "$Y" "$1" "$X"; }
die()  { printf "%serror:%s %s\n" "$R" "$X" "$1" >&2; exit 1; }

validate_preseed_inputs() {
  [ -n "${VAULT}" ] && return 0

  local name value
  for name in \
    OKTO_NEURON_PACKS \
    OKTO_NEURON_LLM_PROVIDER \
    OKTO_NEURON_LLM_API_BASE \
    OKTO_NEURON_LLM_MODEL \
    OKTO_NEURON_LLM_API_KEY_ENV \
    OKTO_NEURON_LLM_SKIP_DISCOVERY \
    OKTO_NEURON_LLM_ALLOW_REMOTE \
    OKTO_NEURON_ALLOW_REMOTE_LLM \
    OKTO_NEURON_ONBOARD_NONINTERACTIVE
  do
    value="${!name:-}"
    if [ -n "${value}" ]; then
      die "${name} requires OKTO_NEURON_VAULT; omit preseed settings and configure vaults in the Web UI, or set OKTO_NEURON_VAULT explicitly"
    fi
  done
}

validate_preseed_inputs

# ── flags ───────────────────────────────────────────────────────────────
# The plain-path first-run prompt (0.0.48) is opt-out only; unknown flags die
# so a typo can never be mistaken for an opt-out. `bash -s -- --no-onboard`
# and `bash install.sh --no-onboard` both land here.
# Guarded on direct execution: the source-repo helper tests and the CI wheel
# gate `source` the prefix of this file above the EXIT trap, passing their own
# positional arguments. Unguarded, those paths hit the unknown-flag die.
# The `:-$0` default is load-bearing: this script runs under `set -u` and the
# public install path is `curl ... | bash`, where BASH_SOURCE is unset and a
# bare ${BASH_SOURCE[0]} is a fatal unbound-variable error. Defaulting to $0
# keeps the piped and executed paths parsing flags while sourced callers skip.
NO_ONBOARD=""
if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
  for arg in "$@"; do
    case "${arg}" in
      --no-onboard) NO_ONBOARD="1" ;;
      *) die "unknown install.sh flag: ${arg} (supported: --no-onboard)" ;;
    esac
  done
fi

open_application_ui() {
  local url="$1"
  case "$(uname -s 2>/dev/null || true)" in
    Darwin)
      command -v open >/dev/null 2>&1 || return 1
      open "${url}" >/dev/null 2>&1
      ;;
    MINGW*|MSYS*|CYGWIN*)
      command -v cmd.exe >/dev/null 2>&1 || return 1
      cmd.exe /c start "" "${url}" >/dev/null 2>&1
      ;;
    *)
      command -v xdg-open >/dev/null 2>&1 || return 1
      xdg-open "${url}" >/dev/null 2>&1
      ;;
  esac
}

require_expected_wheel_version() {
  [ -n "${1:-}" ] \
    || die "wheel verification requires a manifest version or OKTO_NEURON_EXPECTED_VERSION"
}

# Greenfield: no vault config and no defaults config under the Okto Neuron
# home. The update-path detector (running daemon or prior tool environment)
# takes precedence over this check, so an upgrade can never be prompted.
is_greenfield_home() {
  local root name
  for root in "${HOME_ROOT}" "${LEGACY_HOME_ROOT}"; do
    for name in okto-neuron.yaml marginalia.yaml; do
      ls "${root}"/vaults/*/"${name}" >/dev/null 2>&1 && return 1
    done
    [ -f "${root}/defaults.yaml" ] && return 1
  done
  return 0
}

# A vault directory holds okto-neuron.yaml, or marginalia.yaml when it was
# created before 0.3.0 (that file is read in place and never renamed).
vault_has_config() {
  [ -f "$1/okto-neuron.yaml" ] || [ -f "$1/marginalia.yaml" ]
}

# Print the version a status or /version payload reports. 0.3.0+ sends
# okto_neuron_version (and marginalia_version); older daemons only the latter.
payload_version() {
  printf '%s' "$1" | "${VERIFY_PYTHON}" -c '
import json, sys
try:
    payload = json.load(sys.stdin)
except (TypeError, ValueError):
    raise SystemExit(0)
if isinstance(payload, dict):
    print(payload.get("okto_neuron_version") or payload.get("marginalia_version") or "")
' 2>/dev/null || true
}

# One-shot greenfield first-run prompt (0.0.48): offered only from the plain
# (non-preseed, non-update) path, and only when a real terminal is available.
# Non-TTY, OKTO_NEURON_NO_OPEN=1, and --no-onboard skip silently; Y/Enter runs
# `okto-neuron onboard` (the user names their own vault in-flow), n or EOF
# falls through to the application-first path with a discoverability hint.
run_greenfield_first_run_prompt() {
  [ "${NO_ONBOARD}" = "1" ] && return 0
  [ "${OKTO_NEURON_NO_OPEN:-}" = "1" ] && return 0
  (exec < /dev/tty) 2>/dev/null || return 0
  is_greenfield_home || return 0
  local answer=""
  printf '%s\n' "Okto Neuron first run: no vault configured." > /dev/tty
  printf '%s' "Set up your vault and LLM provider now? [Y/n] (default Y) " > /dev/tty
  if ! IFS= read -r answer < /dev/tty; then
    info "run 'okto-neuron onboard' from any shell to do this setup in the terminal"
    return 0
  fi
  case "${answer//[[:space:]]/}" in
    [nN]|[nN][oO])
      info "run 'okto-neuron onboard' from any shell to do this setup in the terminal"
      return 0
      ;;
    *)
      step "Running 'okto-neuron onboard' (you name your own vault in-flow)"
      okto-neuron onboard < /dev/tty
      info "onboarding complete — manage your vaults and provider in the Web UI"
      return 0
      ;;
  esac
}

read_server_pid() {
  local pid_file="$1"
  [ -f "${pid_file}" ] || return 0
  "${VERIFY_PYTHON}" -c '
import json, sys
with open(sys.argv[1], "rb") as handle:
    raw = handle.read(16385)
if len(raw) > 16384:
    raise SystemExit(0)
text = raw.decode("utf-8").strip()
try:
    payload = json.loads(text)
except (TypeError, ValueError):
    payload = None
value = payload.get("pid", "") if isinstance(payload, dict) else (text.splitlines()[0] if text else "")
try:
    pid = int(value)
except (TypeError, ValueError):
    pid = 0
if pid > 0:
    print(pid)
' "${pid_file}" 2>/dev/null || true
}

find_daemon_lock_root() {
  local target_pid="$1" recorded_pid="" pid_file root
  # A pre-0.3.0 daemon keeps its application PID record under ~/.marginalia.
  for root in "${DAEMON_RUNTIME_ROOT}" "${LEGACY_DAEMON_RUNTIME_ROOT}"; do
    pid_file="${root}/.marginalia/server.pid"
    [ -f "${pid_file}" ] || continue
    recorded_pid="$(read_server_pid "${pid_file}")"
    if [ "${recorded_pid}" = "${target_pid}" ]; then
      printf '%s' "${root}"
      return 0
    fi
  done
  return 1
}

claude_mcp_registration_matches() {
  local output="$1" expected_url="$2" scope_label="${3:-User}"
  printf '%s\n' "${output}" | awk '
    $1 == "Scope:" { scope += 1 }
    $1 == "Status:" { status += 1 }
    $1 == "Type:" { type += 1 }
    $1 == "URL:" { url += 1 }
    END { exit(scope == 1 && status == 1 && type == 1 && url == 1 ? 0 : 1) }
  ' || return 1
  printf '%s\n' "${output}" | grep -Eq \
    "^[[:space:]]*Scope:[[:space:]]*${scope_label} config([[:space:]]+\\([^()]*\\))?[[:space:]]*\$" \
    || return 1
  printf '%s\n' "${output}" | grep -Eq \
    '^[[:space:]]*Status:[[:space:]]*([^[:alnum:]][[:space:]]*)?Connected[[:space:]]*$' \
    || return 1
  printf '%s\n' "${output}" | grep -Eq \
    '^[[:space:]]*Type:[[:space:]]*http[[:space:]]*$' || return 1
  printf '%s\n' "${output}" | awk -v expected="${expected_url}" '
    $1 == "URL:" && $2 == expected && NF == 2 { found = 1 }
    END { exit(found ? 0 : 1) }
  '
}

claude_mcp_registration_scope() {
  local output="$1"
  if printf '%s\n' "${output}" | grep -Eq \
      '^[[:space:]]*Scope:[[:space:]]*Local config([[:space:]]+\([^()]*\))?[[:space:]]*$'; then
    printf 'local'
  elif printf '%s\n' "${output}" | grep -Eq \
      '^[[:space:]]*Scope:[[:space:]]*Project config([[:space:]]+\([^()]*\))?[[:space:]]*$'; then
    printf 'project'
  elif printf '%s\n' "${output}" | grep -Eq \
      '^[[:space:]]*Scope:[[:space:]]*User config([[:space:]]+\([^()]*\))?[[:space:]]*$'; then
    printf 'user'
  else
    printf 'unknown'
  fi
}

daemon_version() {
  local cli="$1" vault="${2:-}" payload="" version=""
  local status_command=("${cli}" status --json --timeout 2)
  [ -n "${vault}" ] && status_command+=(--vault "${vault}")
  payload="$("${status_command[@]}" 2>/dev/null || true)"
  version="$(payload_version "${payload}")"
  if [ -z "${version}" ]; then
    payload="$(curl -fsS --max-time 2 "${REST_URL}/version" 2>/dev/null || true)"
    version="$(payload_version "${payload}")"
  fi
  [ -n "${version}" ] || return 1
  printf '%s' "${version}"
}

restart_previous_daemon() {
  [ -n "${WAS_RUNNING}" ] || return 0

  # Before activation the old PID may still own the daemon. Never launch a
  # duplicate; the unchanged prior process already preserves running state.
  if [ -z "${ACTIVATION_STARTED}" ] && [ -n "${OLD_PID:-}" ] \
     && kill -0 "${OLD_PID}" 2>/dev/null; then
    info "previous ${PREVIOUS_PRODUCT} daemon remains running (pid ${OLD_PID})"
    SHUTDOWN_REQUESTED=""
    return 0
  fi

  # After a rollback the restored launcher is the previous tool's own command
  # (marginalia when the previous install predates the rename).
  local restart_command="${TOOL_BIN}/${PREVIOUS_CLI:-${CLI}}"
  [ -x "${restart_command}" ] || restart_command="${PREVIOUS_COMMAND}"
  if [ -z "${restart_command}" ] || [ ! -x "${restart_command}" ]; then
    warn "previous daemon was running but its command could not be restored"
    return 1
  fi
  local restart_args=(serve --daemon)
  # ADR-0034 daemons open the browser by default and support --no-open. The
  # immutable 0.0.40 predecessor lacks that option and never auto-opened.
  if "${restart_command}" serve --help 2>/dev/null | grep -q -- '--no-open'; then
    restart_args+=(--no-open)
  fi
  if [ -n "${PREVIOUS_DAEMON_VAULT}" ]; then
    restart_args+=(--vault "${PREVIOUS_DAEMON_VAULT}")
  fi
  if ! "${restart_command}" "${restart_args[@]}" >/dev/null 2>&1; then
    warn "previous tool is available but its daemon could not be restarted"
    return 1
  fi

  local running_version=""
  for _ in $(seq 1 30); do
    running_version="$(daemon_version "${restart_command}" "${PREVIOUS_DAEMON_VAULT}" || true)"
    [ -n "${running_version}" ] && break
    sleep 1
  done
  if [ -z "${running_version}" ] \
     || { [ -n "${PREVIOUS_VERSION}" ] && [ "${running_version}" != "${PREVIOUS_VERSION}" ]; }; then
    warn "previous daemon restart could not be verified"
    return 1
  fi
  SHUTDOWN_REQUESTED=""
  info "restored and restarted ${PREVIOUS_PRODUCT} ${PREVIOUS_VERSION:-previous version}"
}

stop_candidate_daemon() {
  [ -n "${CANDIDATE_DAEMON_STARTED}" ] || return 0
  local candidate_pid=""
  candidate_pid="$(read_server_pid "${DAEMON_PID_FILE}")"
  if [ -x "${TOOL_BIN}/${CLI}" ]; then
    "${TOOL_BIN}/${CLI}" stop --timeout 10 \
      >/dev/null 2>&1 || true
  fi
  if { [ -n "${candidate_pid}" ] && kill -0 "${candidate_pid}" 2>/dev/null; } \
     || port_in_use || mcp_port_in_use; then
    warn "candidate daemon is still live; refusing to remove its environment"
    return 1
  fi
}

restore_previous_tool() {
  [ -n "${ACTIVATION_STARTED}" ] || return 0
  warn "activation failed; restoring the previous ${PREVIOUS_PRODUCT} tool"

  stop_candidate_daemon || return 1
  undo_home_migration || return 1

  if [ -n "${TOOL_ROOT}" ] && [ "${TOOL_ROOT}" != "/" ]; then
    rm -rf "${TOOL_ROOT:?}/${TOOL_NAME}" || return 1
  fi
  if [ -n "${TOOL_BIN}" ] && [ "${TOOL_BIN}" != "/" ]; then
    for launcher in ${LAUNCHERS}; do
      rm -f "${TOOL_BIN:?}/${launcher}" || return 1
    done
  fi
  # The previous tool keeps its own name: `marginalia` for a pre-0.3.0 install.
  local previous_tool="${PREVIOUS_TOOL_NAME:-${TOOL_NAME}}"
  if [ -n "${BACKUP_ROOT}" ] && [ -d "${BACKUP_ROOT}/tool" ]; then
    mv "${BACKUP_ROOT}/tool" "${TOOL_ROOT}/${previous_tool}" || return 1
  elif [ -n "${PREVIOUS_VERSION}" ]; then
    warn "previous tool backup is missing at ${BACKUP_ROOT}/tool"
    return 1
  fi
  if [ -n "${BACKUP_ROOT}" ] && [ -d "${BACKUP_ROOT}/bin" ]; then
    for launcher in ${LAUNCHERS}; do
      if [ -e "${BACKUP_ROOT}/bin/${launcher}" ] || [ -L "${BACKUP_ROOT}/bin/${launcher}" ]; then
        mv "${BACKUP_ROOT}/bin/${launcher}" "${TOOL_BIN}/${launcher}" || return 1
      fi
    done
  fi

  local restored=""
  if [ -x "${TOOL_ROOT}/${previous_tool}/bin/python" ]; then
    restored="$("${TOOL_ROOT}/${previous_tool}/bin/python" -c \
      'import importlib.metadata, sys; print(importlib.metadata.version(sys.argv[1]))' \
      "${previous_tool}" 2>/dev/null || true)"
  fi
  if [ -n "${PREVIOUS_VERSION}" ] && [ "${restored}" != "${PREVIOUS_VERSION}" ]; then
    warn "previous tool restoration could not be verified (expected ${PREVIOUS_VERSION}, got ${restored:-missing})"
    return 1
  fi

  if [ -n "${WAS_RUNNING}" ]; then
    restart_previous_daemon || return 1
  else
    info "restored ${PREVIOUS_PRODUCT} ${PREVIOUS_VERSION:-previous version}; daemon remains stopped"
  fi
  ACTIVATION_STARTED=""
}

# Copy app-level files from ~/.marginalia into ~/.okto-neuron (D5: vaults are
# never moved). Records exactly what this run created so a rollback can remove
# it again and leave the previous install as it was.
MOVED_TO_EXISTED=""
migrate_app_home() {
  [ -d "${LEGACY_HOME_ROOT}" ] || return 0
  [ -e "${LEGACY_HOME_ROOT}/MOVED_TO" ] && MOVED_TO_EXISTED="1"
  HOME_MIGRATION_JSON="$("${TOOL_BIN}/${CLI}" migrate-home --json)" \
    || die "could not copy app-level files from ${LEGACY_HOME_ROOT} to ${HOME_ROOT}"
  local copied=""
  copied="$(printf '%s' "${HOME_MIGRATION_JSON}" | "${VERIFY_PYTHON}" -c \
    'import json,sys; print(", ".join(json.load(sys.stdin).get("copied", [])) or "none")' \
    2>/dev/null || true)"
  info "copied app files from ${LEGACY_HOME_ROOT} to ${HOME_ROOT}: ${copied}"
  if [ -d "${LEGACY_HOME_ROOT}/vaults" ]; then
    info "vaults stay in ${LEGACY_HOME_ROOT}/vaults (document ids depend on their paths)"
  fi
}

undo_home_migration() {
  [ -n "${HOME_MIGRATION_JSON}" ] || return 0
  local name
  while IFS= read -r name; do
    case "${name}" in ''|*/*|.|..) continue ;; esac
    rm -f "${HOME_ROOT:?}/${name}" || return 1
  done < <(printf '%s' "${HOME_MIGRATION_JSON}" | "${VERIFY_PYTHON}" -c \
    'import json,sys; [print(n) for n in json.load(sys.stdin).get("copied", [])]' 2>/dev/null)
  if [ -z "${MOVED_TO_EXISTED}" ]; then
    rm -f "${LEGACY_HOME_ROOT}/MOVED_TO" || return 1
  fi
  HOME_MIGRATION_JSON=""
}

# Rollback half of remove_legacy_mcp: put the pre-0.3.0 `marginalia` entry
# back exactly as it was (same scope, endpoint and daemon credential).
LEGACY_MCP_REMOVED_SCOPE=""
restore_legacy_mcp() {
  [ -n "${LEGACY_MCP_REMOVED_SCOPE}" ] || return 0
  local scope="${LEGACY_MCP_REMOVED_SCOPE}"
  LEGACY_MCP_REMOVED_SCOPE=""
  if claude mcp add --scope "${scope}" --transport http \
       "${LEGACY_CLI}" "${GLOBAL_URL}" \
       --header "Authorization: Bearer ${AUTH_TOKEN}" >/dev/null 2>&1; then
    info "re-added the old '${LEGACY_CLI}' ${scope}-scope Claude MCP entry"
    return 0
  fi
  warn "could not re-add the old '${LEGACY_CLI}' ${scope}-scope Claude MCP entry; add it back with: claude mcp add --scope ${scope} --transport http ${LEGACY_CLI} ${GLOBAL_URL} --header \"Authorization: Bearer <token from ${TOKEN_FILE}>\""
  return 1
}

installer_exit() {
  local rc=$?
  local retain_backup=""
  trap - EXIT
  if [ -n "${ACTIVATION_STARTED}" ] && [ -z "${ACTIVATION_COMMITTED}" ]; then
    if ! restore_previous_tool; then
      rc=1
      retain_backup="1"
    fi
  elif [ -n "${SHUTDOWN_REQUESTED}" ]; then
    restart_previous_daemon || rc=1
  fi
  if [ "${rc}" -ne 0 ]; then
    restore_legacy_mcp || rc=1
  fi
  if [ -n "${BACKUP_ROOT}" ]; then
    if [ -n "${retain_backup}" ]; then
      warn "rollback is incomplete; recovery backup retained at ${BACKUP_ROOT}"
    else
      rm -rf "${BACKUP_ROOT}"
    fi
  fi
  [ -n "${WORK_TMP}" ] && rm -rf "${WORK_TMP}"
  [ -n "${CLONE_TMP}" ] && rm -rf "${CLONE_TMP}"
  exit "${rc}"
}
trap installer_exit EXIT

printf "%s\n" "${B}Okto Neuron installer${X} — local-first knowledge graph for Claude Code"

# ── 1. uv ─────────────────────────────────────────────────────────────────
step "Checking uv (the package manager Okto Neuron runs through)"
if ! command -v uv >/dev/null 2>&1; then
  info "uv not found — installing from astral.sh ..."
  curl -fsSL https://astral.sh/uv/install.sh | sh
  # Make uv visible for the rest of this run.
  export PATH="${HOME}/.local/bin:${PATH}"
  command -v uv >/dev/null 2>&1 || die "uv installed but not on PATH; restart your shell and re-run."
fi
info "uv: $(command -v uv)"

# A prior uv tool can exist even when this shell has not loaded uv's PATH
# update yet. Make it reachable before update detection needs to stop it.
PREINSTALL_TOOL_BIN="$(uv tool dir --bin 2>/dev/null || true)"
[ -n "${PREINSTALL_TOOL_BIN}" ] && export PATH="${PREINSTALL_TOOL_BIN}:${PATH}"
TOOL_ROOT="$(uv tool dir 2>/dev/null || true)"
TOOL_BIN="${PREINSTALL_TOOL_BIN:-${HOME}/.local/bin}"
WORK_TMP="$(mktemp -d)"

step "Ensuring Python ${PY_VERSION} (uv-managed; no system Python touched)"
uv python install "${PY_VERSION}" >/dev/null 2>&1 || true
VERIFY_PYTHON="$(uv python find "${PY_VERSION}" 2>/dev/null || true)"
[ -x "${VERIFY_PYTHON}" ] || die "uv could not resolve Python ${PY_VERSION} for release verification"

# ── 2. obtain the source / wheel ──────────────────────────────────────────
fetch_file() {
  local source="$1" destination="$2"
  case "${source}" in
    http://*|https://*) curl -fsSL "${source}" -o "${destination}" ;;
    file://*) cp "${source#file://}" "${destination}" ;;
    *) cp "${source}" "${destination}" ;;
  esac
}

manifest_value() {
  "${VERIFY_PYTHON}" -c \
    'import json,sys; value=json.load(open(sys.argv[1], encoding="utf-8")).get(sys.argv[2], ""); print(value)' \
    "$1" "$2"
}

step "Resolving and staging the Okto Neuron candidate"
SPEC=""
CANDIDATE_KIND=""
WHEEL_SOURCE=""
SOURCE_PATH=""
if [ -n "${OKTO_NEURON_WHEEL:-}" ]; then
  CANDIDATE_KIND="wheel"
  WHEEL_SOURCE="${OKTO_NEURON_WHEEL}"
  info "using wheel: ${WHEEL_SOURCE}"
elif [ -n "${OKTO_NEURON_SRC:-}" ]; then
  [ -f "${OKTO_NEURON_SRC}/pyproject.toml" ] \
    || die "OKTO_NEURON_SRC has no pyproject.toml: ${OKTO_NEURON_SRC}"
  CANDIDATE_KIND="source"
  SOURCE_PATH="${OKTO_NEURON_SRC}"
  info "using checkout: ${SOURCE_PATH}"
else
  if ! SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd)"; then
    SELF_DIR=""
  fi
  if [ -n "${SELF_DIR}" ] && [ -f "${SELF_DIR}/pyproject.toml" ] \
     && grep -q '^name = "okto-neuron"' "${SELF_DIR}/pyproject.toml" 2>/dev/null; then
    CANDIDATE_KIND="source"
    SOURCE_PATH="${SELF_DIR}"
    info "running from a clone: ${SOURCE_PATH}"
  elif [ -n "${DEFAULT_WHEEL_URL}" ]; then
    CANDIDATE_KIND="wheel"
    WHEEL_SOURCE="${DEFAULT_WHEEL_URL}"
    info "using release wheel: ${WHEEL_SOURCE}"
  else
    command -v git >/dev/null 2>&1 \
      || die "git not found; install git or set OKTO_NEURON_SRC / OKTO_NEURON_WHEEL."
    CLONE_TMP="${WORK_TMP}/source"
    info "cloning ${REPO} ..."
    git clone --depth 1 ${REF:+--branch "$REF"} "${REPO}" "${CLONE_TMP}" \
      || die "clone failed. Check OKTO_NEURON_REPO and your network, or pass OKTO_NEURON_SRC=<path> / OKTO_NEURON_WHEEL=<url>."
    CANDIDATE_KIND="source"
    SOURCE_PATH="${CLONE_TMP}"
  fi
fi

if [ "${CANDIDATE_KIND}" = "wheel" ]; then
  MANIFEST_SOURCE="${OKTO_NEURON_MANIFEST:-}"
  if [ -z "${MANIFEST_SOURCE}" ] && [ "${WHEEL_SOURCE}" = "${DEFAULT_WHEEL_URL}" ]; then
    MANIFEST_SOURCE="${DEFAULT_MANIFEST_URL}"
  fi
  MANIFEST_SHA=""
  MANIFEST_WHEEL=""
  if [ -n "${MANIFEST_SOURCE}" ]; then
    MANIFEST_FILE="${WORK_TMP}/release-manifest.json"
    fetch_file "${MANIFEST_SOURCE}" "${MANIFEST_FILE}" \
      || die "could not load release manifest: ${MANIFEST_SOURCE}"
    MANIFEST_VERSION="$(manifest_value "${MANIFEST_FILE}" version)"
    MANIFEST_URL="$(manifest_value "${MANIFEST_FILE}" wheel_url)"
    MANIFEST_SHA="$(manifest_value "${MANIFEST_FILE}" sha256)"
    MANIFEST_WHEEL="$(manifest_value "${MANIFEST_FILE}" wheel)"
    if [ -z "${MANIFEST_VERSION}" ] || [ -z "${MANIFEST_URL}" ] \
      || [ -z "${MANIFEST_SHA}" ] || [ -z "${MANIFEST_WHEEL}" ]; then
      die "release manifest is missing version, wheel_url, wheel, or sha256"
    fi
    [ "$(basename "${MANIFEST_WHEEL}")" = "${MANIFEST_WHEEL}" ] \
      || die "release manifest wheel must be a filename, not a path"
    if [ -n "${EXPECTED_VERSION}" ] && [ "${EXPECTED_VERSION}" != "${MANIFEST_VERSION}" ]; then
      die "release manifest version ${MANIFEST_VERSION} does not match expected ${EXPECTED_VERSION}"
    fi
    EXPECTED_VERSION="${MANIFEST_VERSION}"
    case "${WHEEL_SOURCE}" in
      http://*|https://*)
        [ "${WHEEL_SOURCE}" = "${MANIFEST_URL}" ] \
          || die "wheel URL does not match release manifest"
        ;;
      *)
        [ "$(basename "${WHEEL_SOURCE}")" = "${MANIFEST_WHEEL}" ] \
          || die "wheel filename does not match release manifest"
        ;;
    esac
  fi

  require_expected_wheel_version "${EXPECTED_VERSION}"

  EXPECTED_SHA="${OKTO_NEURON_WHEEL_SHA256:-${MANIFEST_SHA}}"
  [ -n "${EXPECTED_SHA}" ] \
    || die "wheel verification requires OKTO_NEURON_MANIFEST or OKTO_NEURON_WHEEL_SHA256"
  if [ -n "${MANIFEST_SHA}" ] && [ -n "${OKTO_NEURON_WHEEL_SHA256:-}" ] \
     && [ "${OKTO_NEURON_WHEEL_SHA256}" != "${MANIFEST_SHA}" ]; then
    die "OKTO_NEURON_WHEEL_SHA256 does not match the release manifest"
  fi
  case "${EXPECTED_SHA}" in
    *[!0-9a-fA-F]*|'') die "wheel SHA-256 must be 64 hexadecimal characters" ;;
  esac
  [ "${#EXPECTED_SHA}" -eq 64 ] || die "wheel SHA-256 must be 64 hexadecimal characters"

  WHEEL_NAME="${MANIFEST_WHEEL:-$(basename "${WHEEL_SOURCE%%\?*}")}"
  case "${WHEEL_NAME}" in *.whl) ;; *) die "wheel filename must end in .whl" ;; esac
  CANDIDATE_WHEEL="${WORK_TMP}/${WHEEL_NAME}"
  fetch_file "${WHEEL_SOURCE}" "${CANDIDATE_WHEEL}" \
    || die "could not download wheel: ${WHEEL_SOURCE}"
  ACTUAL_SHA="$("${VERIFY_PYTHON}" -c \
    'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' \
    "${CANDIDATE_WHEEL}")"
  [ "$(printf '%s' "${ACTUAL_SHA}" | tr '[:upper:]' '[:lower:]')" = \
    "$(printf '%s' "${EXPECTED_SHA}" | tr '[:upper:]' '[:lower:]')" ] \
    || die "wheel SHA-256 mismatch; expected ${EXPECTED_SHA}, got ${ACTUAL_SHA}"
  info "verified wheel SHA-256: ${ACTUAL_SHA}"
  SPEC="${CANDIDATE_WHEEL}[${EXTRAS}]"
else
  SPEC="${SOURCE_PATH}[${EXTRAS}]"
fi

# Resolve/build/install the candidate in a throwaway venv while the current
# daemon and uv tool remain untouched. Activation only begins after this passes.
STAGE_VENV="${WORK_TMP}/stage"
uv venv --python "${PY_VERSION}" "${STAGE_VENV}" >/dev/null
STAGE_PYTHON="${STAGE_VENV}/bin/python"
STAGE_CLI="${STAGE_VENV}/bin/okto-neuron"
uv pip install --python "${STAGE_PYTHON}" "${SPEC}"
CANDIDATE_VERSION="$("${STAGE_PYTHON}" -c \
  'import importlib.metadata; print(importlib.metadata.version("okto-neuron"))')"
if [ -n "${EXPECTED_VERSION}" ] && [ "${CANDIDATE_VERSION}" != "${EXPECTED_VERSION}" ]; then
  die "staged Okto Neuron ${CANDIDATE_VERSION}, expected ${EXPECTED_VERSION}"
fi
EXPECTED_VERSION="${CANDIDATE_VERSION}"
[ -x "${STAGE_CLI}" ] || die "staged wheel did not install the okto-neuron command"
"${STAGE_CLI}" --help >/dev/null
# --version prints "okto-neuron <version>" and then the Okto Labs attribution.
STAGED_CLI_VERSION="$("${STAGE_CLI}" --version 2>/dev/null | head -n 1 || true)"
if [ -n "${STAGED_CLI_VERSION}" ] \
   && [ "${STAGED_CLI_VERSION}" != "okto-neuron ${CANDIDATE_VERSION}" ]; then
  die "staged CLI does not match package version ${CANDIDATE_VERSION}"
fi
info "staged Okto Neuron ${CANDIDATE_VERSION}; active installation is still untouched"

# ── 2b. app-scoped update discovery ──────────────────────────────────
# Replacing the installed package under a LIVE daemon leaves it serving stale
# file handles. Lifecycle ownership and status are application-scoped; selecting
# a vault is independent of starting, stopping, or updating the daemon.
json_value() {
  printf '%s' "$1" | "${VERIFY_PYTHON}" -c \
    'import json,sys; value=json.load(sys.stdin).get(sys.argv[1], ""); print(value if value is not None else "")' \
    "$2" 2>/dev/null || true
}

port_in_use() {
  (: >/dev/tcp/127.0.0.1/7777) >/dev/null 2>&1
}

mcp_port_in_use() {
  (: >/dev/tcp/127.0.0.1/8201) >/dev/null 2>&1
}

discover_daemon_status() {
  local payload=""
  if [ -n "${PREVIOUS_COMMAND}" ] && [ -x "${PREVIOUS_COMMAND}" ]; then
    payload="$("${PREVIOUS_COMMAND}" status --json --timeout 2 2>/dev/null || true)"
    if [ -n "${payload}" ] && [ -n "$(json_value "${payload}" pid)" ]; then
      printf '%s' "${payload}"
      return 0
    fi
  fi
  payload="$(curl -fsS --max-time 2 "${REST_URL}/api/v1/status" 2>/dev/null || true)"
  if [ -n "${payload}" ] && [ -n "$(json_value "${payload}" pid)" ]; then
    printf '%s' "${payload}"
  fi
  return 0
}

legacy_vault_paths() {
  local payload="" config=""
  if [ -n "${PREVIOUS_COMMAND}" ] && [ -x "${PREVIOUS_COMMAND}" ]; then
    payload="$("${PREVIOUS_COMMAND}" vault list --json 2>/dev/null || true)"
  fi
  if [ -n "${payload}" ]; then
    printf '%s' "${payload}" | "${VERIFY_PYTHON}" -c '
import json, sys
try:
    payload = json.load(sys.stdin)
except (TypeError, ValueError):
    raise SystemExit(0)
for row in payload.get("vaults", []):
    path = row.get("path") if isinstance(row, dict) else None
    if isinstance(path, str) and path and "\n" not in path and "\r" not in path:
        print(path)
' 2>/dev/null || true
  fi
  for config in "${LEGACY_HOME_ROOT}"/vaults/*/marginalia.yaml; do
    [ -f "${config}" ] || continue
    dirname "${config}"
  done
}

find_unverified_live_legacy_daemon() {
  local vault="" pid=""
  while IFS= read -r vault; do
    [ -n "${vault}" ] || continue
    pid="$(read_server_pid "${vault}/.marginalia/server.pid")"
    case "${pid}" in ''|*[!0-9]*) continue ;; esac
    if kill -0 "${pid}" 2>/dev/null; then
      "${VERIFY_PYTHON}" -c \
        'import json,sys; print(json.dumps({"pid": int(sys.argv[1]), "vault": sys.argv[2]}))' \
        "${pid}" "${vault}"
      return 0
    fi
  done < <(legacy_vault_paths | sort -u)
}

find_verified_legacy_lock_root() {
  local target_pid="$1" vault="$2" recorded_pid=""
  [ -n "${vault}" ] && [ -f "${vault}/marginalia.yaml" ] || return 1
  recorded_pid="$(read_server_pid "${vault}/.marginalia/server.pid")"
  [ "${recorded_pid}" = "${target_pid}" ] || return 1
  printf '%s' "${vault}"
}

find_unverified_live_daemon() {
  local pid="" pid_file
  for pid_file in "${DAEMON_PID_FILE}" "${LEGACY_DAEMON_PID_FILE}"; do
    [ -f "${pid_file}" ] || continue
    pid="$(read_server_pid "${pid_file}")"
    case "${pid}" in ''|*[!0-9]*) continue ;; esac
    if kill -0 "${pid}" 2>/dev/null; then
      "${VERIFY_PYTHON}" -c \
        'import json,sys; print(json.dumps({"pid": int(sys.argv[1]), "record": sys.argv[2]}))' \
        "${pid}" "${pid_file}"
      return 0
    fi
  done
}

tool_package_version() {
  local tool="$1" package="$2"
  [ -x "${TOOL_ROOT}/${tool}/bin/python" ] || return 0
  "${TOOL_ROOT}/${tool}/bin/python" -c \
    'import importlib.metadata, sys; print(importlib.metadata.version(sys.argv[1]))' \
    "${package}" 2>/dev/null || true
}

# Which installed uv tool this run replaces. Sets PREVIOUS_TOOL_NAME,
# PREVIOUS_CLI, PREVIOUS_VERSION and, for a pre-0.3.0 install, PRODUCT_UPGRADE.
detect_previous_tool() {
  if [ -x "${TOOL_ROOT}/${TOOL_NAME}/bin/python" ]; then
    # João's 2026-09 Okto Neuron MVP was also published as a uv tool named
    # okto-neuron (command `neuron`). Never replace or modify it.
    if ! "${TOOL_ROOT}/${TOOL_NAME}/bin/python" -c 'import okto_neuron._compat' >/dev/null 2>&1; then
      die "a different 'okto-neuron' uv tool is installed (the Okto Neuron MVP, command 'neuron'). This installer will not replace it. Keep it by leaving it installed and stopping here, or remove it yourself with 'uv tool uninstall okto-neuron' and re-run."
    fi
    PREVIOUS_TOOL_NAME="${TOOL_NAME}"
    PREVIOUS_CLI="${CLI}"
    PREVIOUS_VERSION="$(tool_package_version "${TOOL_NAME}" okto-neuron)"
    if [ -x "${TOOL_ROOT}/${LEGACY_TOOL_NAME}/bin/python" ]; then
      die "both the okto-neuron and the pre-0.3.0 marginalia uv tools are installed. Remove the old one with 'uv tool uninstall marginalia' and re-run."
    fi
  elif [ -x "${TOOL_ROOT}/${LEGACY_TOOL_NAME}/bin/python" ]; then
    PRODUCT_UPGRADE="1"
    PREVIOUS_PRODUCT="Marginalia"
    PREVIOUS_TOOL_NAME="${LEGACY_TOOL_NAME}"
    PREVIOUS_CLI="${LEGACY_CLI}"
    PREVIOUS_VERSION="$(tool_package_version "${LEGACY_TOOL_NAME}" marginalia)"
    info "found Marginalia ${PREVIOUS_VERSION:-unknown} (the pre-0.3.0 name of Okto Neuron); it will be replaced"
  fi
  if [ -n "${PREVIOUS_CLI:-}" ] && [ -x "${TOOL_BIN}/${PREVIOUS_CLI}" ]; then
    PREVIOUS_COMMAND="${TOOL_BIN}/${PREVIOUS_CLI}"
  else
    PREVIOUS_COMMAND="$(command -v "${PREVIOUS_CLI:-${CLI}}" 2>/dev/null || true)"
  fi
  if [ -d "${HOME_ROOT}/grafx" ]; then
    info "found Okto Neuron MVP memory at ${HOME_ROOT}/grafx; it is left untouched"
  fi
}

UPGRADE=""
OLD_PID=""
PREVIOUS_CLI=""
PREVIOUS_COMMAND=""
detect_previous_tool
STATUS_JSON="$(discover_daemon_status)"
if [ -z "${STATUS_JSON}" ]; then
  UNVERIFIED_DAEMON="$(find_unverified_live_daemon)"
  if [ -n "${UNVERIFIED_DAEMON}" ]; then
    UNVERIFIED_PID="$(json_value "${UNVERIFIED_DAEMON}" pid)"
    UNVERIFIED_RECORD="$(json_value "${UNVERIFIED_DAEMON}" record)"
      die "live Okto Neuron daemon (pid ${UNVERIFIED_PID}) has an application PID record at ${UNVERIFIED_RECORD}, but status at ${REST_URL} is unavailable; update aborted before replacing the installed tool. Stop it first: ${PREVIOUS_CLI:-${CLI}} stop. If it uses custom ports, stop it manually and rerun."
  fi
  if [ "${PREVIOUS_VERSION}" = "0.0.40" ]; then
    UNVERIFIED_LEGACY_DAEMON="$(find_unverified_live_legacy_daemon)"
    if [ -n "${UNVERIFIED_LEGACY_DAEMON}" ]; then
      UNVERIFIED_PID="$(json_value "${UNVERIFIED_LEGACY_DAEMON}" pid)"
      UNVERIFIED_VAULT="$(json_value "${UNVERIFIED_LEGACY_DAEMON}" vault)"
      die "live Marginalia 0.0.40 daemon (pid ${UNVERIFIED_PID}) has a vault-scoped PID record at ${UNVERIFIED_VAULT}/.marginalia/server.pid, but verified status at ${REST_URL} is unavailable; update aborted before replacing the installed tool. Stop it first: marginalia stop --vault \"${UNVERIFIED_VAULT}\", then rerun the installer."
    fi
  fi
fi
if [ -n "${STATUS_JSON}" ]; then
  UPGRADE="1"
  WAS_RUNNING="1"
  OLD_PID="$(json_value "${STATUS_JSON}" pid)"
  STATUS_ENDPOINT="$(json_value "${STATUS_JSON}" endpoint)"
  STATUS_VERSION="$(payload_version "${STATUS_JSON}")"
  OLD_LOCK_ROOT="$(find_daemon_lock_root "${OLD_PID}" || true)"
  if [ -z "${OLD_LOCK_ROOT}" ]; then
    STATUS_VAULT="$(json_value "${STATUS_JSON}" vault_path)"
    if [ "${PREVIOUS_VERSION}" != "0.0.40" ] \
      || [ "${STATUS_VERSION}" != "0.0.40" ]; then
      die "Okto Neuron status reported pid ${OLD_PID}, but its application lifecycle lock could not be verified; update aborted before shutdown"
    fi
    OLD_LOCK_ROOT="$(find_verified_legacy_lock_root "${OLD_PID}" "${STATUS_VAULT}" || true)"
    [ -n "${OLD_LOCK_ROOT}" ] \
      || die "Marginalia 0.0.40 status reported pid ${OLD_PID} and vault ${STATUS_VAULT:-unknown}, but the matching vault-scoped lifecycle lock could not be verified; update aborted before shutdown"
    LEGACY_DAEMON="1"
    PREVIOUS_DAEMON_VAULT="${STATUS_VAULT}"
  fi
  case "${STATUS_ENDPOINT%/}" in
    ""|"${REST_URL}"|"http://localhost:7777") ;;
    *)
      if [ -n "${LEGACY_DAEMON}" ]; then
        die "live Marginalia 0.0.40 daemon uses custom endpoint ${STATUS_ENDPOINT}; update aborted before shutdown because the installer cannot preserve custom ports automatically. Stop it first: marginalia stop --vault \"${PREVIOUS_DAEMON_VAULT}\""
      fi
      die "live Okto Neuron daemon uses custom endpoint ${STATUS_ENDPOINT}; update aborted before shutdown because the installer cannot preserve custom ports automatically. Stop it first: ${PREVIOUS_CLI:-${CLI}} stop"
      ;;
  esac
  step "Existing ${PREVIOUS_PRODUCT} daemon detected — updating in place"
  [ -n "${PREVIOUS_COMMAND}" ] \
    || die "daemon is running but its installed ${PREVIOUS_CLI:-${CLI}} command was not found"
  # From this point every exit path must leave the unchanged prior daemon
  # running until activation has a restorable tool backup.
  SHUTDOWN_REQUESTED="1"
  if [ -n "${LEGACY_DAEMON}" ]; then
    STOP_COMMAND=("${STAGE_CLI}" stop --vault "${PREVIOUS_DAEMON_VAULT}" --timeout 30)
  elif [ -n "${PRODUCT_UPGRADE}" ]; then
    # The pre-0.3.0 daemon's lifecycle record lives under ~/.marginalia, which
    # only that version's own command owns. Stop it with that command.
    STOP_COMMAND=("${PREVIOUS_COMMAND}" stop --timeout 30)
  else
    STOP_COMMAND=("${STAGE_CLI}" stop --timeout 30)
  fi
  if ! "${STOP_COMMAND[@]}"; then
    die "could not stop the verified daemon (pid ${OLD_PID}); update aborted before replacing the installed tool"
  fi

  # Never replace files under a process that is still draining.
  for _ in $(seq 1 10); do
    if [ -n "${OLD_PID}" ] && kill -0 "${OLD_PID}" 2>/dev/null; then sleep 1; continue; fi
    port_in_use && { sleep 1; continue; }
    break
  done
  if { [ -n "${OLD_PID}" ] && kill -0 "${OLD_PID}" 2>/dev/null; } \
     || port_in_use; then
    die "old daemon${OLD_PID:+ (pid ${OLD_PID})} still owns its process or port after a successful stop; update aborted before replacing the installed tool"
  fi
  info "stopped the running daemon — it will restart on the new version below"
elif port_in_use; then
  die "port 7777 is in use but verified Okto Neuron status is unavailable; update aborted"
elif [ -n "${PREVIOUS_TOOL_NAME}" ] || ! is_greenfield_home; then
  # Daemon isn't up (crashed, machine rebooted, whatever) but this machine was
  # already set up before — a re-run should update in place, not treat this
  # as a fresh install and re-run vault-create/onboard against existing state.
  UPGRADE="1"
  step "Existing Okto Neuron install detected (daemon not running) — updating in place"
fi

# ── 3. install the global tool ────────────────────────────────────────────
step "Activating staged Okto Neuron ${CANDIDATE_VERSION}"

# Rename the prior environment and launchers on the same filesystem. This
# preserves the exact dependency set and uv receipt for a lossless rollback.
# A pre-0.3.0 `marginalia` tool is moved aside the same way, so a failure
# restores exactly that tool under its own name.
BACKUP_ROOT="${TOOL_ROOT}/.okto-neuron-installer-backup-$$"
mkdir -p "${BACKUP_ROOT}/bin"
ACTIVATION_STARTED="1"
if [ -n "${PREVIOUS_TOOL_NAME}" ] && [ -d "${TOOL_ROOT}/${PREVIOUS_TOOL_NAME}" ]; then
  mv "${TOOL_ROOT}/${PREVIOUS_TOOL_NAME}" "${BACKUP_ROOT}/tool"
fi
for launcher in ${LAUNCHERS}; do
  if [ -e "${TOOL_BIN}/${launcher}" ] || [ -L "${TOOL_BIN}/${launcher}" ]; then
    mv "${TOOL_BIN}/${launcher}" "${BACKUP_ROOT}/bin/${launcher}"
  fi
done

if ! uv tool install --python "${PY_VERSION}" "${SPEC}"; then
  die "candidate activation failed"
fi
export PATH="${TOOL_BIN}:${PATH}"
# Best-effort: persist PATH into the user's shell rc so a NEW shell (next
# terminal, next `curl | bash` re-run) finds okto-neuron without manual setup.
# Opt out for sandboxed/test runs that must not touch real shell rc files.
if [ "${OKTO_NEURON_NO_UPDATE_SHELL:-}" != "1" ]; then
  uv tool update-shell >/dev/null 2>&1 || warn "run 'uv tool update-shell' to persist PATH"
fi
command -v okto-neuron >/dev/null 2>&1 \
  || die "okto-neuron installed but not found in ${TOOL_BIN}. Run 'uv tool update-shell', restart your shell, re-run."
info "okto-neuron: $(command -v okto-neuron)"
TOOL_PYTHON="${TOOL_ROOT}/${TOOL_NAME}/bin/python"
[ -x "${TOOL_PYTHON}" ] || die "could not locate Okto Neuron's uv-managed Python at ${TOOL_PYTHON}"
INSTALLED_VERSION="$("${TOOL_PYTHON}" -c 'import importlib.metadata; print(importlib.metadata.version("okto-neuron"))')"
if [ "${INSTALLED_VERSION}" != "${CANDIDATE_VERSION}" ]; then
  die "installed Okto Neuron ${INSTALLED_VERSION}, expected staged ${CANDIDATE_VERSION}"
fi
CLI_VERSION="$(okto-neuron --version 2>/dev/null | head -n 1 || true)"
if [ -n "${CLI_VERSION}" ] && [ "${CLI_VERSION}" != "okto-neuron ${INSTALLED_VERSION}" ]; then
  die "the installed okto-neuron command does not match package version ${INSTALLED_VERSION}"
fi
info "version: ${INSTALLED_VERSION}"
migrate_app_home

if [ -n "${UPGRADE}" ]; then
  # Update path: the machine is already set up — don't create vaults, don't
  # touch the default, and don't re-prompt for an LLM.
  step "Update mode — leaving your vaults, default, and LLM config untouched"
elif [ -z "${VAULT}" ]; then
  step "Application-first setup"
  info "no vault preseed requested; create, select, and configure vaults in the Web UI"
  run_greenfield_first_run_prompt
else

# ── 4. vault ──────────────────────────────────────────────────────────────
step "Creating vault '${VAULT}' (packs: ${PACKS})"
if vault_has_config "${VAULT_DIR}"; then
  info "vault already exists at ${VAULT_DIR} — leaving it as-is"
  okto-neuron vault use "${VAULT}" >/dev/null 2>&1 || true
else
  okto-neuron vault create "${VAULT}" --packs "${PACKS}" --use
  info "created ${VAULT_DIR}"
fi

# ── 5. LLM provider (okto-neuron onboard; ask + remember need one) ──────────
# Provider-first setup is delegated to `okto-neuron onboard`: it selects the vault
# we just created, walks the provider menu (auto-detect · skip · LM Studio ·
# Ollama · LiteLLM Proxy · OpenRouter · OpenAI · Gemini · Anthropic · custom),
# stores any API key in ~/.okto-neuron/env (never in okto-neuron.yaml), and writes
# the llm: block. Keys stay out of config and non-loopback endpoints are opt-in.
step "Configuring the LLM provider via 'okto-neuron onboard'"
ONBOARD=(okto-neuron onboard --vault "${VAULT}")
NONINTERACTIVE=""
if [ "${OKTO_NEURON_ONBOARD_NONINTERACTIVE:-}" = "1" ]; then
  NONINTERACTIVE="1"
elif ! (exec < /dev/tty) 2>/dev/null; then
  NONINTERACTIVE="1"
fi
if [ -n "${OKTO_NEURON_LLM_PROVIDER:-}" ]; then
  ONBOARD+=(--provider "${OKTO_NEURON_LLM_PROVIDER}")
elif [ -n "${OKTO_NEURON_LLM_API_BASE:-}" ] || [ -n "${OKTO_NEURON_LLM_MODEL:-}" ]; then
  ONBOARD+=(--provider custom)
elif [ -n "${NONINTERACTIVE}" ]; then
  ONBOARD+=(--provider skip)
fi
[ -n "${OKTO_NEURON_LLM_API_BASE:-}" ] \
  && ONBOARD+=(--api-base "${OKTO_NEURON_LLM_API_BASE}")
[ -n "${OKTO_NEURON_LLM_MODEL:-}" ] \
  && ONBOARD+=(--model "${OKTO_NEURON_LLM_MODEL}" --skip-model-discovery)
[ "${OKTO_NEURON_LLM_SKIP_DISCOVERY:-}" = "1" ] \
  && [ -z "${OKTO_NEURON_LLM_MODEL:-}" ] && ONBOARD+=(--skip-model-discovery)
[ -n "${OKTO_NEURON_LLM_API_KEY_ENV:-}" ] \
  && ONBOARD+=(--api-key-env "${OKTO_NEURON_LLM_API_KEY_ENV}")
if [ "${OKTO_NEURON_LLM_ALLOW_REMOTE:-${OKTO_NEURON_ALLOW_REMOTE_LLM:-}}" = "1" ]; then
  ONBOARD+=(--allow-remote-llm --yes)
fi
if [ -n "${NONINTERACTIVE}" ]; then
  ONBOARD+=(--non-interactive)
  info "using noninteractive onboarding"
  "${ONBOARD[@]}"
else
  # Interactive, including `curl … | bash` where stdin is the piped script:
  # drive onboard's provider-first prompts from the real terminal.
  info "choose a provider (or pick Skip — explore() works without an LLM)."
  "${ONBOARD[@]}" < /dev/tty
fi
fi  # end update / application-first / explicit-preseed setup

server_version() {
  local cli="$1" payload="" version=""
  payload="$("${cli}" status --json --timeout 2 2>/dev/null || true)"
  version="$(payload_version "${payload}")"
  if [ -z "${version}" ]; then
    payload="$(curl -fsS --max-time 2 "${REST_URL}/version" 2>/dev/null || true)"
    version="$(payload_version "${payload}")"
  fi
  [ -n "${version}" ] || return 1
  printf '%s' "${version}"
}

# ── 6. serve ──────────────────────────────────────────────────────────────
# Keep install-only and intentionally stopped updates successful, while treating
# a requested daemon start that does not serve the installed version as failure.
SERVE_OK="1"
SERVER_STARTED=""
DAEMON_LOG="${HOME_ROOT}/logs/okto-neuron-serve.log"
if [ "${OKTO_NEURON_NO_SERVE:-}" = "1" ]; then
  step "Skipping daemon start (OKTO_NEURON_NO_SERVE=1)"
elif [ -n "${UPGRADE}" ] && [ -z "${WAS_RUNNING}" ]; then
  step "Preserving stopped daemon state"
  info "the daemon was stopped before this update, so it remains stopped"
else
  if [ -n "${UPGRADE}" ]; then
    step "Restarting the application daemon on the new version"
  else
    step "Starting the Okto Neuron daemon (UI/REST :7777 + MCP :8201)"
  fi
  # The installer no longer exports a process-wide placeholder LLM key (forbidden
  # by the distribution rules). The LLM client injects a placeholder api_key for a
  # keyless custom api_base on its own (okto_neuron/llm __init__), so keyless local
  # LLM serve works. Real keys stay in ~/.okto-neuron/env under the OKTO_NEURON_*
  # name onboard recorded. (A keyless *remote* embedding endpoint is not covered
  # here; the default fastembed embedder is local and needs no key.)
  CANDIDATE_DAEMON_STARTED="1"
  SERVE_ARGS=(serve --daemon --no-open)
  # The 0.0.40 daemon credential lives under its verified vault. Give the
  # successor that vault exactly once so its runtime can adopt the credential
  # into application scope without rotating connected MCP clients. Fresh and
  # already application-scoped starts remain vaultless.
  if [ -n "${LEGACY_DAEMON}" ] && [ -n "${PREVIOUS_DAEMON_VAULT}" ]; then
    SERVE_ARGS+=(--vault "${PREVIOUS_DAEMON_VAULT}")
  fi
  if ! okto-neuron "${SERVE_ARGS[@]}"; then
    SERVE_OK=""
    warn "daemon start command failed"
  else
    info "waiting for server version ${INSTALLED_VERSION} ..."
    SERVER_VERSION=""
    for _ in $(seq 1 60); do
      SERVER_VERSION="$(server_version "${TOOL_BIN}/${CLI}" || true)"
      [ "${SERVER_VERSION}" = "${INSTALLED_VERSION}" ] && break
      sleep 1
    done
    if [ "${SERVER_VERSION}" = "${INSTALLED_VERSION}" ]; then
      SERVER_STARTED="1"
      info "server: ${G}ready${X} (${REST_URL}, version ${SERVER_VERSION})"
    else
      SERVE_OK=""
      if [ -n "${SERVER_VERSION}" ]; then
        warn "server reported version ${SERVER_VERSION}; installed version is ${INSTALLED_VERSION}"
      else
        warn "server did not become ready within 60s"
      fi
      warn "try 'okto-neuron serve --foreground --no-open'"
      warn "daemon log: ${DAEMON_LOG}"
    fi
  fi
  if [ -z "${SERVE_OK}" ] && [ -f "${DAEMON_LOG}" ]; then
    info "last 20 lines of ${DAEMON_LOG}:"
    tail -n 20 "${DAEMON_LOG}" || true
  fi
fi

if [ -z "${SERVE_OK}" ]; then
  die "candidate daemon verification failed; the previous installation will be restored"
fi

# Version and requested daemon-state verification passed. From this point the
# candidate is committed and the backup can be discarded.
ACTIVATION_COMMITTED="1"
SHUTDOWN_REQUESTED=""
rm -rf "${BACKUP_ROOT}"
BACKUP_ROOT=""

# ── 6b. launchd (macOS, only if the opt-in LaunchAgent template was used) ─
# The packaging/macos template is opt-in; the installer never creates a job.
# If a pre-0.3.0 com.oktolabs.marginalia job exists, write the renamed plist
# next to it with the okto-neuron command. A loaded job is not switched here,
# because loading the new one while this installer's daemon holds the ports
# would make launchd restart it in a loop; the exact commands are printed.
migrate_launch_agent() {
  [ "$(uname -s 2>/dev/null || true)" = "Darwin" ] || return 0
  local agents="${HOME}/Library/LaunchAgents"
  local old_plist="${agents}/com.oktolabs.marginalia.plist"
  local new_plist="${agents}/com.oktolabs.okto-neuron.plist"
  [ -f "${old_plist}" ] || return 0
  if [ ! -e "${new_plist}" ]; then
    sed -e 's#<string>com\.oktolabs\.marginalia</string>#<string>com.oktolabs.okto-neuron</string>#' \
        -e "s#<string>[^<]*/marginalia</string>#<string>${TOOL_BIN}/${CLI}</string>#" \
        -e 's#marginalia\.out\.log#okto-neuron.out.log#; s#marginalia\.err\.log#okto-neuron.err.log#' \
        "${old_plist}" > "${new_plist}" || { warn "could not write ${new_plist}"; return 0; }
    info "wrote ${new_plist} from your Marginalia LaunchAgent"
  fi
  if launchctl list com.oktolabs.marginalia >/dev/null 2>&1; then
    warn "the old LaunchAgent com.oktolabs.marginalia is still loaded and runs the 'marginalia' alias"
    info "to switch: okto-neuron stop; launchctl unload ${old_plist}; launchctl load ${new_plist}"
  else
    mv "${old_plist}" "${old_plist}.pre-okto-neuron" \
      && info "moved the unloaded old LaunchAgent aside: ${old_plist}.pre-okto-neuron"
  fi
}
migrate_launch_agent

# ── 7. wire Claude Code ───────────────────────────────────────────────────
# MCP alone retains the daemon capability-token gate. Register the private token as a
# Bearer header without printing it to installer output.
#
# Upgrading from Marginalia: the daemon keeps its port and adopts the old
# credential, so an existing `marginalia` entry keeps working until it is
# replaced. `okto-neuron` is registered in the scope the old entry had (user or
# local) and verified as connected (Claude Code's own health check). Only then is
# the old `marginalia` entry in that same scope removed, and only when it pointed
# at this same endpoint. If the installer fails after that removal, the EXIT trap
# adds the old entry back exactly as it was (same scope, URL and credential). A
# project entry (a shared .mcp.json file) is only reported, since writing a
# credential into a shared file is not the installer's call.
GLOBAL_URL="${MCP_URL}"
TOKEN_FILE="${DAEMON_TOKEN_FILE}"
[ -f "${TOKEN_FILE}" ] || TOKEN_FILE="${LEGACY_DAEMON_TOKEN_FILE}"
AUTH_TOKEN=""
if [ -f "${TOKEN_FILE}" ]; then
  IFS= read -r AUTH_TOKEN < "${TOKEN_FILE}" || true
fi
MCP_WIRED=""
MCP_SCOPE_WIRED=""

# Scope label as `claude mcp get` prints it ("User config", "Local config").
scope_label() {
  case "$1" in
    local) printf 'Local' ;;
    project) printf 'Project' ;;
    *) printf 'User' ;;
  esac
}

register_mcp() {
  local scope="$1" output=""
  if output="$(claude mcp get "${CLI}" 2>&1)"; then
    if claude_mcp_registration_matches "${output}" "${GLOBAL_URL}" "$(scope_label "${scope}")"; then
      MCP_WIRED="1"
      MCP_SCOPE_WIRED="${scope}"
      info "preserved connected '${CLI}' ${scope}-scope registration"
      return 0
    fi
    local existing_scope=""
    existing_scope="$(claude_mcp_registration_scope "${output}")"
    warn "an existing '${CLI}' Claude MCP entry is not the connected ${scope}-scope endpoint ${GLOBAL_URL}"
    if [ "${existing_scope}" = "local" ] || [ "${existing_scope}" = "project" ] || [ "${existing_scope}" = "user" ]; then
      info "resolve it with: claude mcp remove ${CLI} --scope ${existing_scope}"
    else
      info "inspect it with: claude mcp get ${CLI}"
    fi
    die "Claude MCP registration conflict; resolve the existing entry and re-run this installer"
  fi
  if claude mcp add --scope "${scope}" --transport http \
       "${CLI}" "${GLOBAL_URL}" \
       --header "Authorization: Bearer ${AUTH_TOKEN}" >/dev/null 2>&1; then
    output="$(claude mcp get "${CLI}" 2>&1 || true)"
    if ! claude_mcp_registration_matches "${output}" "${GLOBAL_URL}" "$(scope_label "${scope}")"; then
      die "Claude MCP registration was added but did not verify as a connected ${scope}-scope endpoint"
    fi
    MCP_WIRED="1"
    MCP_SCOPE_WIRED="${scope}"
    info "registered and verified the app-scoped '${CLI}' MCP endpoint (${scope} scope)"
    return 0
  fi
  warn "automatic Claude Code registration failed"
  return 1
}

# Remove the pre-0.3.0 `marginalia` entry from the scope `okto-neuron` now
# answers in. Called only after register_mcp verified the new entry as
# connected, and only for an old entry that pointed at this same endpoint.
remove_legacy_mcp() {
  local scope="$1" legacy_output="$2" after=""
  if ! claude_mcp_registration_matches "${legacy_output}" "${GLOBAL_URL}" "$(scope_label "${scope}")" \
     && ! printf '%s\n' "${legacy_output}" | awk -v expected="${GLOBAL_URL}" '
       $1 == "URL:" && $2 == expected && NF == 2 { found = 1 } END { exit(found ? 0 : 1) }'; then
    warn "the old '${LEGACY_CLI}' ${scope}-scope entry points somewhere else; it was left unchanged"
    return 0
  fi
  if ! claude mcp remove "${LEGACY_CLI}" --scope "${scope}" >/dev/null 2>&1; then
    warn "could not remove the old '${LEGACY_CLI}' ${scope}-scope entry; remove it with: claude mcp remove ${LEGACY_CLI} --scope ${scope}"
    return 0
  fi
  LEGACY_MCP_REMOVED_SCOPE="${scope}"
  if after="$(claude mcp get "${LEGACY_CLI}" 2>&1)" \
     && [ "$(claude_mcp_registration_scope "${after}")" = "${scope}" ]; then
    die "the old '${LEGACY_CLI}' ${scope}-scope entry is still registered after removal"
  fi
  info "removed the old '${LEGACY_CLI}' ${scope}-scope entry; '${CLI}' replaces it"
}

if [ "${OKTO_NEURON_NO_MCP:-}" = "1" ]; then
  step "Skipping Claude Code wiring (OKTO_NEURON_NO_MCP=1)"
elif [ -z "${AUTH_TOKEN}" ]; then
  step "Claude Code wiring deferred"
  info "start the daemon, then re-run this installer to register its authenticated MCP endpoint"
elif command -v claude >/dev/null 2>&1; then
  LEGACY_MCP_OUTPUT=""
  LEGACY_MCP_SCOPE=""
  if LEGACY_MCP_OUTPUT="$(claude mcp get "${LEGACY_CLI}" 2>&1)"; then
    LEGACY_MCP_SCOPE="$(claude_mcp_registration_scope "${LEGACY_MCP_OUTPUT}")"
  fi
  if [ "${LEGACY_MCP_SCOPE}" = "local" ]; then
    step "Re-registering the Marginalia MCP entry as '${CLI}' (local scope, as before)"
    register_mcp local && remove_legacy_mcp local "${LEGACY_MCP_OUTPUT}"
  else
    step "Registering the authenticated MCP server with Claude Code (user scope)"
    # Okto Neuron reuses the application credential across daemon restarts. Preserve an
    # existing user registration instead of deleting a working integration before
    # its replacement is proven. A fresh registration still uses Claude's only
    # documented HTTP-header input surface.
    if register_mcp user && [ "${LEGACY_MCP_SCOPE}" = "user" ]; then
      remove_legacy_mcp user "${LEGACY_MCP_OUTPUT}"
    fi
    if [ "${LEGACY_MCP_SCOPE}" = "project" ]; then
      warn "a project-scope '${LEGACY_CLI}' entry (.mcp.json) was left unchanged; it still works, and you can rename it to '${CLI}' in that file"
    fi
  fi
else
  step "Claude Code CLI not found"
  info "install Claude Code, then re-run this installer to register Okto Neuron"
fi

# The daemon itself stays headless while the installer proves the exact version.
# Only the committed, verified application is allowed to launch a browser.
if [ -n "${SERVER_STARTED}" ] && [ "${OKTO_NEURON_NO_OPEN:-}" != "1" ]; then
  step "Opening the verified Okto Neuron application"
  if open_application_ui "${REST_URL}/"; then
    info "opened ${REST_URL}/"
  else
    warn "browser launch is unavailable; open ${REST_URL}/"
  fi
fi

# ── done ──────────────────────────────────────────────────────────────────
if [ -z "${SERVE_OK}" ]; then
  printf "\n%sOkto Neuron %s installed, but the daemon did not start correctly.%s\n" \
    "$Y" "${INSTALLED_VERSION}" "$X"
  info "start manually: okto-neuron serve --foreground"
  info "log      : ${DAEMON_LOG}"
  exit 1
fi

if [ -n "${UPGRADE}" ]; then
  if [ -n "${SERVER_STARTED}" ]; then
    printf "\n%sOkto Neuron %s updated and restarted.%s\n" "$B$G" "${INSTALLED_VERSION}" "$X"
  else
    printf "\n%sOkto Neuron %s updated; daemon remains stopped.%s\n" "$B$G" "${INSTALLED_VERSION}" "$X"
  fi
elif [ -n "${SERVER_STARTED}" ]; then
  printf "\n%sOkto Neuron %s is ready.%s\n" "$B$G" "${INSTALLED_VERSION}" "$X"
else
  printf "\n%sOkto Neuron %s installed; daemon was not started.%s\n" \
    "$B$G" "${INSTALLED_VERSION}" "$X"
fi
if [ -n "${VAULT_DIR}" ]; then
  info "preseed vault: ${VAULT_DIR} (managed independently from the daemon)"
else
  info "vaults   : create and manage them in the Web UI"
fi
info "graph backend: chosen at vault creation, Okto Grafx by default (Ladybug or Neo4j selectable)"
if [ -n "${SERVER_STARTED}" ]; then
  info "web UI   : ${REST_URL}/"
  info "stop     : okto-neuron stop"
  if [ -n "${MCP_WIRED}" ]; then
    info "Claude MCP: authenticated ${MCP_SCOPE_WIRED}-scope connection registered as '${CLI}'"
  fi
else
  info "start    : okto-neuron serve --daemon"
fi
info "update   : re-run this installer"
