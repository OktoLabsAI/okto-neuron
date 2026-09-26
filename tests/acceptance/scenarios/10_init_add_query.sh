#!/usr/bin/env bash
# Scenario 10: happy path — kg init, write real markdown into notes/, start
# the real marginalia server, kg add + kg query against OKTO_NEURON_ENDPOINT,
# assert provenance hit. Real bge-small inference (cache reuse OK). No mocks.
set -uo pipefail
SCENARIO_NAME="10_init_add_query"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_preamble.sh"

VAULT="$work_dir/vault"

log "kg init $VAULT"
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.stdout" 2>"$work_dir/init.stderr"
assert_exit_code 0 $?
assert_file_exists "$VAULT/okto-neuron.yaml"
assert_file_exists "$VAULT/notes"
assert_graph_populated "$VAULT" "after_init"

log "writing real markdown note into notes/"
cat > "$VAULT/notes/hello.md" <<'EOF'
# Hello Okto Neuron

This note states that Okto Neuron is a local-first knowledge graph
with provenance-bearing query results.

Author: example.
EOF
assert_file_exists "$VAULT/notes/hello.md"

log "start_server $VAULT"
if ! start_server "$VAULT"; then
  _failures+=("start_server failed; see $work_dir/server.log")
  finish
fi

log "kg add (real bge-small embedding via REST /add)"
kg add "$VAULT/notes/hello.md" --endpoint "$OKTO_NEURON_ENDPOINT" \
  >"$work_dir/add.stdout" 2>"$work_dir/add.stderr"
add_rc=$?
assert_exit_code 0 "$add_rc"
if [[ "$add_rc" -ne 0 ]]; then tail -20 "$work_dir/add.stderr" >&2; fi

# Real work assertion: the live graph must have grown beyond an empty/fresh
# header (backend-neutral -- see _lib.sh's assert_graph_populated).
assert_graph_populated "$VAULT" "after_add"

log "kg query 'marginalia' (real embedding + similarity search)"
kg query "marginalia" --endpoint "$OKTO_NEURON_ENDPOINT" \
  >"$work_dir/query.stdout" 2>"$work_dir/query.stderr"
query_rc=$?
assert_exit_code 0 "$query_rc"
# Must surface the file path + some provenance shape (path, byte span, or content_hash)
assert_contains "$work_dir/query.stdout" "hello\.md"
# Should NOT be the no-results message
assert_not_contains "$work_dir/query.stdout" "^\(no hits\)$"

stop_server

finish
