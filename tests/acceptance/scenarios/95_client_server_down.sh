#!/usr/bin/env bash
# Scenario 95: client-when-server-down fails fast.
#
# Contract:
#   - No marginalia server is started.
#   - OKTO_NEURON_ENDPOINT points at an unused TCP port on 127.0.0.1.
#   - `kg add` and `kg query` must both:
#       * exit non-zero
#       * write the exact contract strings to stderr:
#           "no okto-neuron server reachable at"
#         AND
#           "start with `okto-neuron serve`"
#   - No partial vault writes: vault tree hash unchanged before/after.
set -uo pipefail
SCENARIO_NAME="95_client_server_down"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

VAULT="$work_dir/vault"

log "kg init $VAULT (so we have a real vault on disk)"
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.stdout" 2>"$work_dir/init.stderr"
assert_exit_code 0 $?
assert_file_exists "$VAULT/okto-neuron.yaml"

# Pick a non-advertised unused port and immediately release it — nothing
# listens here. The shared allocator can never choose user defaults 7777/8201.
PORT="$(_marg_free_port)"
export OKTO_NEURON_ENDPOINT="http://127.0.0.1:${PORT}"
log "OKTO_NEURON_ENDPOINT=$OKTO_NEURON_ENDPOINT (no server listening)"

# Snapshot vault tree (path + size + mtime + sha256 of every file) BEFORE.
vault_snapshot() {
  # One line per file: <path> <size> <mtime> <sha256>. Handles many files.
  find "$VAULT" -type f | LC_ALL=C sort | while IFS= read -r f; do
    sz=$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f")
    mt=$(stat -f%m "$f" 2>/dev/null || stat -c%Y "$f")
    sh=$(shasum -a 256 "$f" | awk '{print $1}')
    printf '%s %s %s %s\n' "$f" "$sz" "$mt" "$sh"
  done
}
vault_snapshot > "$work_dir/vault.before"

# Make a real file we would try to add (must exist so we are exercising the
# unreachable-server path, not the missing-file path).
cat > "$work_dir/doc.md" <<'EOF'
# Doc

Okto Neuron thin client must fail fast when the server is unreachable.
EOF

log "kg add against unreachable server — expect non-zero + contract strings"
set +e
kg add "$work_dir/doc.md" --endpoint "$OKTO_NEURON_ENDPOINT" \
  >"$work_dir/add.stdout" 2>"$work_dir/add.stderr"
add_rc=$?
set -e
log "kg add rc=$add_rc"
if [[ "$add_rc" -eq 0 ]]; then
  _failures+=("kg_add_unexpectedly_succeeded")
  log "FAIL kg add exited 0 against unreachable endpoint"
fi
# Use fixed strings (-F) so backticks/dashes are not regex-interpreted.
_assertions=$((_assertions+1))
if ! grep -F -q "no okto-neuron server reachable at" "$work_dir/add.stderr"; then
  _failures+=("add_stderr_missing_phrase_no_marginalia_server_reachable_at")
  log "FAIL add stderr missing 'no okto-neuron server reachable at'"
fi
_assertions=$((_assertions+1))
if ! grep -F -q 'start with `okto-neuron serve`' "$work_dir/add.stderr"; then
  _failures+=("add_stderr_missing_phrase_start_with_marginalia_serve_vault")
  log "FAIL add stderr missing 'start with \`okto-neuron serve\`'"
fi

log "kg query against unreachable server — expect non-zero + contract strings"
set +e
kg query "x" --endpoint "$OKTO_NEURON_ENDPOINT" \
  >"$work_dir/query.stdout" 2>"$work_dir/query.stderr"
query_rc=$?
set -e
log "kg query rc=$query_rc"
if [[ "$query_rc" -eq 0 ]]; then
  _failures+=("kg_query_unexpectedly_succeeded")
  log "FAIL kg query exited 0 against unreachable endpoint"
fi
_assertions=$((_assertions+1))
if ! grep -F -q "no okto-neuron server reachable at" "$work_dir/query.stderr"; then
  _failures+=("query_stderr_missing_phrase_no_marginalia_server_reachable_at")
  log "FAIL query stderr missing 'no okto-neuron server reachable at'"
fi
_assertions=$((_assertions+1))
if ! grep -F -q 'start with `okto-neuron serve`' "$work_dir/query.stderr"; then
  _failures+=("query_stderr_missing_phrase_start_with_marginalia_serve_vault")
  log "FAIL query stderr missing 'start with \`okto-neuron serve\`'"
fi

# Vault state must be UNCHANGED (no partial writes).
vault_snapshot > "$work_dir/vault.after"
_assertions=$((_assertions+1))
if ! diff -q "$work_dir/vault.before" "$work_dir/vault.after" >"$work_dir/vault.diff" 2>&1; then
  _failures+=("vault_state_changed_during_unreachable_calls")
  log "FAIL vault state changed:"
  diff -u "$work_dir/vault.before" "$work_dir/vault.after" >&2 || true
fi

finish
