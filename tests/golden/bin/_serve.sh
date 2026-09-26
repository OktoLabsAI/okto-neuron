#!/usr/bin/env bash
# Serve/stop/health helpers for the golden harness.
#
# Adapted from tests/acceptance/scenarios/_preamble.sh, but standalone: the golden
# harness drives the `marginalia` binary from uv's managed env. The env is DERIVED
# from uv (`golden_resolve_env`, correct under UV_PROJECT_ENVIRONMENT) rather than
# assumed to be `$REPO_ROOT/.venv`, and the resolved binary's provenance is then
# asserted (`golden_assert_repo_binary`). It does NOT import okto_neuron source
# — pure black-box CLI + HTTP.
#
# Contract:
#   golden_resolve_env <repo-root>
#     - prints the uv-managed environment prefix (sys.prefix) on stdout
#   golden_assert_repo_binary <venv-dir> <repo-root>
#     - asserts `marginalia` on PATH belongs to that env and imports repo code;
#       returns non-zero with a loud diagnostic otherwise
#   golden_start_server <vault-path> <rest-port> <mcp-port> <log-path>
#     - launches `okto-neuron serve --vault <vault> --foreground` in background
#     - waits up to 60s for GET /health to return 200
#     - exports OKTO_NEURON_ENDPOINT (REST), OKTO_NEURON_MCP_ENDPOINT (MCP), and
#       OKTO_NEURON_AUTH_TOKEN without printing the credential
#     - sets _GOLDEN_SERVER_PID
#   golden_stop_server
#     - SIGTERM, wait up to 30s, escalate to SIGKILL
#
# HARD RULE: no mocks. Launches the real binary against a real vault on loopback.

_GOLDEN_SERVER_PID=""

# ── venv provenance ─────────────────────────────────────────────────────────
# The harness must never start a daemon from unknown code. `command -v marginalia`
# proves only that SOME binary of that name is on $PATH — historically the user's
# GLOBAL uv tool, whose source is a different, older SHA. These two helpers derive
# the environment uv actually manages and then prove the resolved binary and the
# imported `marginalia` module both come from it / from this checkout.

golden_resolve_env() {  # golden_resolve_env <repo-root> -> prints sys.prefix
  local repo_root="$1"
  ( cd "$repo_root" && uv run --frozen --no-sync --group litellm \
      python -c 'import sys; print(sys.prefix)' )
}

golden_assert_repo_binary() {  # golden_assert_repo_binary <venv-dir> <repo-root>
  local venv_dir="$1" repo_root="$2"
  local bin interp probe prefix modfile
  _golden_prov_fail() {
    {
      echo "=============================================================="
      echo "GOLDEN HARNESS ABORT: okto-neuron binary provenance check FAILED"
      echo "  reason:                  $1"
      echo "  resolved okto-neuron:    ${bin:-<unresolved>}"
      echo "  shebang interpreter:     ${interp:-<unread>}"
      echo "  reported sys.prefix:     ${prefix:-<unknown>}"
      echo "  reported __file__:       ${modfile:-<unknown>}"
      echo "  expected env (VENV_DIR): $venv_dir"
      echo "  repo root:               $repo_root"
      echo "  UV_PROJECT_ENVIRONMENT:  ${UV_PROJECT_ENVIRONMENT:-<unset>}"
      echo "  VIRTUAL_ENV:             ${VIRTUAL_ENV:-<unset>}"
      echo "  PATH:                    $PATH"
      echo "This run would have evaluated code that is NOT this checkout."
      echo "=============================================================="
    } >&2
    return 1
  }

  [[ -n "$venv_dir" && -d "$venv_dir" ]] || { _golden_prov_fail "derived env dir missing"; return 1; }
  # A derived prefix with no pyvenv.cfg is a base interpreter, not a managed
  # virtualenv — e.g. UV_PROJECT_ENVIRONMENT pointing at a directory that is not
  # a real venv, where `sys.prefix` silently falls through to the system/pyenv
  # install and its globally-installed marginalia.
  [[ -f "$venv_dir/pyvenv.cfg" ]] \
    || { _golden_prov_fail "derived prefix is not a virtualenv (no pyvenv.cfg)"; return 1; }

  if ! bin="$(command -v okto-neuron 2>/dev/null)"; then
    _golden_prov_fail "no okto-neuron on PATH"; return 1
  fi
  bin="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$bin")"
  case "$bin" in
    "$venv_dir"/*) ;;
    *) _golden_prov_fail "resolved binary is outside the uv-managed env"; return 1 ;;
  esac

  # Advisory: a console script is a text file whose shebang names the interpreter.
  interp="$(sed -n '1s/^#!//p' "$bin" 2>/dev/null | awk '{print $1}')"

  # Authoritative: ask the env's own interpreter where it and the module live.
  if ! probe="$("$venv_dir/bin/python" -c \
      'import sys, okto_neuron; print(sys.prefix); print(okto_neuron.__file__)' 2>&1)"; then
    _golden_prov_fail "could not import okto_neuron from the derived env: $probe"; return 1
  fi
  prefix="$(printf '%s\n' "$probe" | sed -n '1p')"
  modfile="$(printf '%s\n' "$probe" | sed -n '2p')"

  if [[ "$prefix" != "$venv_dir" ]]; then
    _golden_prov_fail "sys.prefix does not match the derived env"; return 1
  fi
  # Editable install -> repo src/; non-editable -> site-packages inside the env.
  case "$modfile" in
    "$repo_root"/src/*|"$venv_dir"/*) ;;
    *) _golden_prov_fail "okto_neuron module is neither this checkout nor the derived env"; return 1 ;;
  esac

  GOLDEN_MARGINALIA_VERSION="$("$bin" --version 2>&1 | head -1)"
  GOLDEN_MARGINALIA_BIN="$bin"
  GOLDEN_MARGINALIA_INTERP="$interp"
  GOLDEN_MARGINALIA_PREFIX="$prefix"
  GOLDEN_MARGINALIA_MODULE="$modfile"
  export GOLDEN_MARGINALIA_BIN GOLDEN_MARGINALIA_INTERP GOLDEN_MARGINALIA_PREFIX \
    GOLDEN_MARGINALIA_MODULE GOLDEN_MARGINALIA_VERSION
  return 0
}

# Write the accepted tuple into the run's evidence, alongside the existing
# PID-plus-vault identity discipline: what this run actually executed.
golden_write_provenance() {  # golden_write_provenance <out-file>
  python3 -c '
import json, os, sys
json.dump({
    "marginalia_bin": os.environ.get("GOLDEN_MARGINALIA_BIN"),
    "interpreter": os.environ.get("GOLDEN_MARGINALIA_INTERP"),
    "sys_prefix": os.environ.get("GOLDEN_MARGINALIA_PREFIX"),
    "module_file": os.environ.get("GOLDEN_MARGINALIA_MODULE"),
    "version": os.environ.get("GOLDEN_MARGINALIA_VERSION"),
    "uv_project_environment": os.environ.get("UV_PROJECT_ENVIRONMENT"),
    "virtual_env": os.environ.get("VIRTUAL_ENV"),
}, open(sys.argv[1], "w"), indent=2)
' "$1"
}

golden_free_port() {
  python3 - <<'PY'
import socket
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.bind(("127.0.0.1", 0))
    print(s.getsockname()[1])
PY
}

golden_start_server() {
  local vault="$1" rest_port="$2" mcp_port="$3" log="$4"
  if [[ -z "$vault" || -z "$rest_port" || -z "$mcp_port" || -z "$log" ]]; then
    echo "golden_start_server: vault rest_port mcp_port log all required" >&2
    return 1
  fi

  okto-neuron serve \
    --vault "$vault" \
    --host 127.0.0.1 \
    --port "$rest_port" \
    --mcp-port "$mcp_port" \
    --foreground \
    --no-open \
    >"$log" 2>&1 &
  _GOLDEN_SERVER_PID=$!

  export OKTO_NEURON_ENDPOINT="http://127.0.0.1:${rest_port}"
  export OKTO_NEURON_MCP_ENDPOINT="http://127.0.0.1:${mcp_port}/mcp"

  local i=0
  while (( i < 120 )); do
    if ! kill -0 "$_GOLDEN_SERVER_PID" 2>/dev/null; then
      echo "golden_start_server: server exited before ready; see $log" >&2
      tail -40 "$log" >&2 || true
      _GOLDEN_SERVER_PID=""
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
      local token_file="$HOME/.okto-neuron/daemon-${rest_port}.token"
      if [[ ! -r "$token_file" ]]; then
        echo "golden_start_server: auth token unreadable: $token_file" >&2
        golden_stop_server || true
        return 1
      fi
      IFS= read -r OKTO_NEURON_AUTH_TOKEN <"$token_file"
      if [[ -z "$OKTO_NEURON_AUTH_TOKEN" ]]; then
        echo "golden_start_server: auth token empty: $token_file" >&2
        golden_stop_server || true
        return 1
      fi
      export OKTO_NEURON_AUTH_TOKEN
      return 0
    fi
    sleep 0.5
    i=$((i+1))
  done

  echo "golden_start_server: /health did not return 200 within 60s; see $log" >&2
  tail -40 "$log" >&2 || true
  golden_stop_server || true
  return 1
}

golden_stop_server() {
  local pid="$_GOLDEN_SERVER_PID"
  [[ -z "$pid" ]] && return 0
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    local i=0
    while (( i < 60 )); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.5; i=$((i+1))
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
    else
      wait "$pid" 2>/dev/null || true
    fi
  fi
  _GOLDEN_SERVER_PID=""
  unset OKTO_NEURON_ENDPOINT OKTO_NEURON_MCP_ENDPOINT OKTO_NEURON_AUTH_TOKEN
}
