#!/usr/bin/env bash
# Shared preamble: ensure the uv-managed env is synced + active, and provide
# start_server / stop_server helpers for client/server scenarios.
# Sourced after _lib.sh, with $work_dir and $REPO_ROOT already set.
#
# uv is the canonical entry point. Model-free scenarios sync the default
# serve+dev groups; explicit scenario 56 additionally syncs the optional
# litellm group required by its real provider. The server lifecycle below needs
# the real PID for signal-based stop, so it runs the binary directly, not `uv run`.
#
# grafx and ladybug are BASE `dependencies` in pyproject.toml (D-94 step 1,
# commit "deps: okto-grafx and ladybug become base dependencies") -- every
# `uv sync`, with or without any `--extra`/`--group` flag, installs both
# unconditionally, and `uv sync`'s EXACT-by-default pruning can never remove
# an unconditional base dependency. So there is no `--extra grafx` branch
# here any more: it would be a no-op. `neo4j`, by contrast, is still an
# extra-only dependency (not a base one), so it still needs its own
# conditional sync below or a bare/litellm-only sync would prune it.
uv_sync_args=(--quiet --python 3.12)
uv_sync_contract="default groups (serve+dev)"
if [[ "${SCENARIO_NAME:-}" == "56_remember_anchored_claims" ]]; then
  uv_sync_args+=(--group litellm)
  uv_sync_contract="default groups + litellm"
fi
if [[ "${OKTO_NEURON_ACCEPTANCE_BACKEND:-}" == "neo4j" ]]; then
  uv_sync_args+=(--extra neo4j)
  uv_sync_contract="$uv_sync_contract + neo4j extra"
fi
if ! (
  cd "$REPO_ROOT" || exit 1
  printf 'acceptance uv contract: %s\n' "$uv_sync_contract"
  uv sync "${uv_sync_args[@]}"
) >"$work_dir/uv-sync.log" 2>&1; then
  log "uv sync failed; see $work_dir/uv-sync.log"
  _failures+=("uv_sync_failed contract=$uv_sync_contract")
  finish
fi
unset uv_sync_args uv_sync_contract
# The suite-owned environment is outside the repository, so acceptance syncs
# cannot prune or add packages beneath a developer or running daemon.
# shellcheck disable=SC1091
source "$UV_PROJECT_ENVIRONMENT/bin/activate"
if ! python3 -c '
import pathlib
import sys

pathlib.Path(sys.argv[1]).write_text(
    f"python={sys.version.split()[0]} executable={sys.executable}\n",
    encoding="utf-8",
)
raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)
' "$work_dir/python-version.log"; then
  log "acceptance requires CPython 3.12; see $work_dir/python-version.log"
  _failures+=("python_version_mismatch required=3.12")
  finish
fi

# ---------------------------------------------------------------------------
# start_server / stop_server — real `okto-neuron serve` lifecycle helpers.
#
# Contract:
#   start_server <vault-path>
#     - picks a free REST port and a free MCP port
#     - launches `okto-neuron serve --vault <vault> --foreground` in background
#     - waits up to 60s for GET /health to return 200
#     - verifies credential-free REST process/vault identity
#     - exports OKTO_NEURON_ENDPOINT (REST), OKTO_NEURON_MCP_ENDPOINT (MCP),
#       and OKTO_NEURON_AUTH_TOKEN for MCP only (read from the 0600 daemon
#       credential file)
#     - registers an EXIT trap so the server is torn down on scenario exit
#
#   stop_server
#     - sends SIGTERM, waits up to 30s, escalates to SIGKILL on timeout
#     - removes the EXIT trap so re-entry doesn't double-kill
#
# HARD RULE: no mocks. This launches the real binary against the real vault.
# ---------------------------------------------------------------------------

_MARG_SERVER_PID=""
_MARG_SERVER_VAULT=""
_MARG_SERVER_LOG=""

_marg_free_port() {
  python3 - <<'PY'
import socket

reserved = {7777, 8201}
while True:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    if port not in reserved:
        print(port)
        break
PY
}

_marg_teardown_trap() {
  # shellcheck disable=SC2317
  stop_server || true
}

start_server() {
  local vault="$1"
  if [[ -z "$vault" ]]; then
    log "start_server: vault path required"
    return 1
  fi
  if [[ -n "$_MARG_SERVER_PID" ]]; then
    log "start_server: server already running pid=$_MARG_SERVER_PID"
    return 1
  fi

  local rest_port mcp_port
  rest_port="$(_marg_free_port)"
  mcp_port="$(_marg_free_port)"
  while [[ "$rest_port" == "$mcp_port" ]]; do
    mcp_port="$(_marg_free_port)"
  done

  _MARG_SERVER_VAULT="$vault"
  _MARG_SERVER_LOG="$work_dir/server.log"

  log "starting okto-neuron serve --vault $vault --port $rest_port --mcp-port $mcp_port"
  okto-neuron serve \
    --vault "$vault" \
    --host 127.0.0.1 \
    --port "$rest_port" \
    --mcp-port "$mcp_port" \
    --foreground \
    --no-open \
    >"$_MARG_SERVER_LOG" 2>&1 &
  _MARG_SERVER_PID=$!

  export OKTO_NEURON_ENDPOINT="http://127.0.0.1:${rest_port}"
  export OKTO_NEURON_MCP_ENDPOINT="http://127.0.0.1:${mcp_port}/mcp"

  # Register tear-down on EXIT (preserve any prior trap).
  trap '_marg_teardown_trap' EXIT

  # Wait up to 60s for /health to come up.
  local i=0
  while (( i < 120 )); do
    if ! kill -0 "$_MARG_SERVER_PID" 2>/dev/null; then
      log "start_server: server process exited before becoming ready; see $_MARG_SERVER_LOG"
      tail -40 "$_MARG_SERVER_LOG" >&2 || true
      _MARG_SERVER_PID=""
      return 1
    fi
    if python3 -c "
import sys, urllib.request
try:
    with urllib.request.urlopen('${OKTO_NEURON_ENDPOINT}/health', timeout=1) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
      # A public /health response proves only that something owns the chosen
      # port. Require credential-free process and canonical-vault identity
      # before any scenario is allowed to send data to the REST server.
      if ! python3 - "$OKTO_NEURON_ENDPOINT" "$_MARG_SERVER_PID" "$vault" <<'PY'
import json
from pathlib import Path
import sys
import urllib.request

endpoint, expected_pid_raw, expected_vault_raw = sys.argv[1:]
with urllib.request.urlopen(endpoint.rstrip("/") + "/api/v1/status", timeout=3) as response:
    if response.status != 200:
        raise SystemExit(1)
    payload = json.load(response)

expected_vault = Path(expected_vault_raw).resolve(strict=False)
actual_vault_raw = payload.get("vault_path")
if not isinstance(actual_vault_raw, str):
    raise SystemExit(1)
actual_vault = Path(actual_vault_raw).resolve(strict=False)
if payload.get("pid") != int(expected_pid_raw) or actual_vault != expected_vault:
    raise SystemExit(1)
PY
      then
        log "start_server: REST server identity did not match pid=$_MARG_SERVER_PID vault=$vault"
        stop_server || true
        return 1
      fi

      # FastMCP remains bearer-protected. Its application-scoped credential is
      # named by the REST port for compatibility, but is never sent to REST.
      local token_file="$HOME/.okto-neuron/daemon-${rest_port}.token"
      if [[ ! -r "$token_file" ]]; then
        log "start_server: MCP auth token is unreadable: $token_file"
        stop_server || true
        return 1
      fi
      IFS= read -r OKTO_NEURON_AUTH_TOKEN <"$token_file"
      if [[ -z "$OKTO_NEURON_AUTH_TOKEN" ]]; then
        log "start_server: MCP auth token is empty: $token_file"
        stop_server || true
        return 1
      fi
      export OKTO_NEURON_AUTH_TOKEN

      log "server ready endpoint=$OKTO_NEURON_ENDPOINT pid=$_MARG_SERVER_PID"
      return 0
    fi
    sleep 0.5
    i=$((i+1))
  done

  log "start_server: /health did not return 200 within 60s; see $_MARG_SERVER_LOG"
  tail -40 "$_MARG_SERVER_LOG" >&2 || true
  stop_server || true
  return 1
}

stop_server() {
  local pid="$_MARG_SERVER_PID"
  if [[ -z "$pid" ]]; then
    return 0
  fi
  if kill -0 "$pid" 2>/dev/null; then
    log "stopping okto-neuron server pid=$pid"
    kill -TERM "$pid" 2>/dev/null || true
    local i=0
    while (( i < 60 )); do
      if ! kill -0 "$pid" 2>/dev/null; then
        break
      fi
      sleep 0.5
      i=$((i+1))
    done
    if kill -0 "$pid" 2>/dev/null; then
      log "stop_server: SIGTERM timeout; sending SIGKILL"
      kill -KILL "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    else
      wait "$pid" 2>/dev/null || true
    fi
  fi
  _MARG_SERVER_PID=""
  _MARG_SERVER_VAULT=""
  unset OKTO_NEURON_ENDPOINT OKTO_NEURON_MCP_ENDPOINT OKTO_NEURON_AUTH_TOKEN
  trap - EXIT
}
