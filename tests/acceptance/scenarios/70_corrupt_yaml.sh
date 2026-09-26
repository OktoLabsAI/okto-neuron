#!/usr/bin/env bash
# Scenario 70 (client/server): corrupt okto-neuron.yaml before the server boots.
# `okto-neuron serve` MUST fail to come up — the server is the single source of
# truth about vault validity, so a broken config must surface at boot, not
# silently degrade. The client follow-up MUST then exit 2 (unreachable) with
# the canonical actionable message — never a raw Python traceback.
set -uo pipefail
SCENARIO_NAME="70_corrupt_yaml"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_preamble.sh"

VAULT="$work_dir/vault"
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.out" 2>"$work_dir/init.err"
assert_exit_code 0 $?

# Seed a real note so the vault is non-trivial before corruption.
cat > "$VAULT/notes/x.md" <<'EOF'
# X
Content for scenario 70.
EOF

# Corrupt the yaml — destructive, irrecoverable without operator action.
echo "{ this is :: not yaml >>>>>" > "$VAULT/okto-neuron.yaml"

# start_server must fail. It logs to $work_dir/server.log and returns non-zero.
log "expecting start_server to FAIL against corrupted yaml"
if start_server "$VAULT"; then
  _failures+=("server_started_against_corrupt_yaml")
  # If it somehow came up, tear it down so the trap doesn't leak.
  stop_server || true
else
  log "start_server failed as required"
fi

# Server log must name yaml/config/vault and must NOT be a raw traceback at top.
if [[ -f "$work_dir/server.log" ]]; then
  if grep -q "Traceback (most recent call last)" "$work_dir/server.log" \
     && ! grep -qiE 'yaml|marginalia\.yaml|config|invalid.*vault|parse' "$work_dir/server.log"; then
    _failures+=("server_log_is_raw_traceback_only")
  fi
  if ! grep -qiE 'yaml|marginalia\.yaml|config|invalid.*vault|parse' "$work_dir/server.log"; then
    _failures+=("server_log_does_not_mention_yaml_or_config")
  fi
else
  _failures+=("no_server_log_emitted")
fi

# With no server up, the client must exit 2 (unreachable) and print the
# canonical "no okto-neuron server reachable" message.
ENDPOINT="$OKTO_NEURON_ENDPOINT"
kg query "X" --endpoint "$ENDPOINT" >"$work_dir/q.out" 2>"$work_dir/q.err"
rc=$?
log "client rc=${rc}"
head -5 "$work_dir/q.err" >&2

if [[ "$rc" -ne 2 ]]; then
  _failures+=("client_exit_not_2 rc=${rc}")
fi
if grep -q "Traceback (most recent call last)" "$work_dir/q.err"; then
  _failures+=("client_emitted_python_traceback")
fi
if ! grep -qiE 'no okto-neuron server reachable|okto-neuron serve --vault' "$work_dir/q.err"; then
  _failures+=("client_message_not_actionable")
fi

if [[ ${#_failures[@]} -gt 0 ]]; then
  finish "corrupt-yaml-error-surface-poor"
else
  finish
fi
