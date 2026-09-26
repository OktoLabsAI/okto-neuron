#!/usr/bin/env bash
# Scenario 71 (client/server): point `okto-neuron serve` at a directory that
# is NOT a vault. The server MUST refuse to come up with a typed, actionable
# error mentioning "not a vault" / "okto-neuron.yaml" / "init first". Then the
# CLI client MUST exit 2 (unreachable) — never a raw traceback.
set -uo pipefail
SCENARIO_NAME="71_non_vault_dir"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_preamble.sh"

NOTAVAULT="$work_dir/random"
mkdir -p "$NOTAVAULT"
echo "hello" > "$NOTAVAULT/hello.txt"

log "expecting start_server to FAIL against a non-vault directory"
if start_server "$NOTAVAULT"; then
  _failures+=("server_started_against_non_vault")
  stop_server || true
else
  log "start_server failed as required"
fi

if [[ -f "$work_dir/server.log" ]]; then
  if ! grep -qiE 'vault not found|not a vault|not.*initialized|missing.*marginalia\.yaml|init.*first|marginalia\.yaml' "$work_dir/server.log"; then
    _failures+=("server_log_message_unhelpful")
  fi
else
  _failures+=("no_server_log_emitted")
fi

ENDPOINT="$OKTO_NEURON_ENDPOINT"
kg query "anything" --endpoint "$ENDPOINT" >"$work_dir/q.out" 2>"$work_dir/q.err"
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
  finish "non-vault-error-surface-poor"
else
  finish
fi
