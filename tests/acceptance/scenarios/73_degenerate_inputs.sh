#!/usr/bin/env bash
# Scenario 73 (client/server): degenerate inputs against a healthy server.
#   (1) empty query string         -> 400 {"error":"bad_request",...} -> client exit 1
#   (2) whitespace-only query      -> 400 bad_request -> client exit 1 (server rejects empty-after-trim semantics; if accepted, must return JSON results envelope with no traceback)
#   (3) `kg add` on a nonexistent local file -> client exit 1 with "cannot read", server never contacted
#   (4) extremely long query       -> server must respond (4xx or 2xx) without 500/traceback; client returns 0 or 1, never crashes
# Hard rule: zero raw Python tracebacks in any client stderr or server log.
set -uo pipefail
SCENARIO_NAME="73_degenerate_inputs"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_preamble.sh"

VAULT="$work_dir/vault"
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.out" 2>"$work_dir/init.err"
assert_exit_code 0 $?

cat > "$VAULT/notes/seed.md" <<'EOF'
# Seed
Some content for scenario 73 to make the vault non-empty.
EOF

if ! start_server "$VAULT"; then
  _failures+=("start_server failed; see $work_dir/server.log")
  finish "degenerate-inputs-server-down"
fi

# Ingest the seed via REST so query has something to score against.
kg add "$VAULT/notes/seed.md" --endpoint "$OKTO_NEURON_ENDPOINT" \
  >"$work_dir/seed_add.out" 2>"$work_dir/seed_add.err" || true

# ----- (1) empty query --------------------------------------------------
kg query "" --endpoint "$OKTO_NEURON_ENDPOINT" >"$work_dir/empty.out" 2>"$work_dir/empty.err"
rc=$?
log "empty query rc=${rc}"
if grep -q "Traceback (most recent call last)" "$work_dir/empty.err"; then
  _failures+=("traceback_on_empty_query")
fi
# Spec: server returns 400 bad_request for missing/empty required string.
# CLI surfaces this as "server error 400: missing or invalid field: query" + exit 1.
if [[ "$rc" -eq 0 ]]; then
  _failures+=("empty_query_silently_accepted")
fi
if ! grep -qE 'server error 400|bad_request|missing or invalid field' "$work_dir/empty.err"; then
  _failures+=("empty_query_error_unhelpful")
fi

# ----- (2) whitespace-only query ---------------------------------------
kg query "   " --endpoint "$OKTO_NEURON_ENDPOINT" >"$work_dir/ws.out" 2>"$work_dir/ws.err"
rc=$?
log "whitespace query rc=${rc}"
if grep -q "Traceback (most recent call last)" "$work_dir/ws.err"; then
  _failures+=("traceback_on_whitespace_query")
fi
# Acceptable: either 400 (server treats trimmed-empty as invalid) OR 200 with
# an empty/sensible results envelope. Forbidden: 5xx, crash, traceback.
if grep -qE 'server error 5[0-9]{2}' "$work_dir/ws.err"; then
  _failures+=("whitespace_query_5xx rc=${rc}")
fi

# ----- (3) `kg add` on nonexistent local file --------------------------
kg add "$VAULT/notes/does_not_exist.md" --endpoint "$OKTO_NEURON_ENDPOINT" \
  >"$work_dir/ne.out" 2>"$work_dir/ne.err"
rc=$?
log "nonexistent file rc=${rc}"
if [[ "$rc" -eq 0 ]]; then
  _failures+=("silent_accept_of_nonexistent_file")
fi
if grep -q "Traceback (most recent call last)" "$work_dir/ne.err"; then
  _failures+=("traceback_on_nonexistent_file")
fi
# Client-side: file is read before the POST. Expect a "cannot read" or
# typical FS error in stderr — never a contactless 500 from the server.
if ! grep -qiE 'cannot read|no such file|does not exist|not found' "$work_dir/ne.err"; then
  _failures+=("nonexistent_file_error_unhelpful")
fi

# ----- (4) extremely long query string (DOS resilience, soft) ----------
big_q="$(python3 -c 'print("a "*5000)')"
kg query "$big_q" --endpoint "$OKTO_NEURON_ENDPOINT" \
  >"$work_dir/big.out" 2>"$work_dir/big.err"
rc=$?
log "huge query rc=${rc}"
if grep -q "Traceback (most recent call last)" "$work_dir/big.err"; then
  _failures+=("traceback_on_huge_query")
fi
if grep -qE 'server error 5[0-9]{2}' "$work_dir/big.err"; then
  _failures+=("huge_query_5xx rc=${rc}")
fi

# Server log must be traceback-free across all four sub-cases.
if [[ -f "$work_dir/server.log" ]] && grep -q "Traceback (most recent call last)" "$work_dir/server.log"; then
  _failures+=("server_log_has_traceback")
fi

stop_server || true

if [[ ${#_failures[@]} -gt 0 ]]; then
  finish "degenerate-inputs-not-handled"
else
  finish
fi
