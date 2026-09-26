#!/usr/bin/env bash
# Scenario 21: three concurrent thin-client `kg add` processes against one
# isolated server. Real user pattern: `find … | xargs -P3 kg add`.
set -uo pipefail
SCENARIO_NAME="21_parallel_add"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

VAULT="$work_dir/vault"
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.stdout" 2>"$work_dir/init.stderr"
assert_exit_code 0 $?

if ! start_server "$VAULT"; then
  _failures+=("server_start_failed")
  finish
fi

for i in 1 2 3; do
  cat > "$VAULT/notes/n${i}.md" <<EOF
# Note ${i}
Concurrent lock probe note ${i} with enough content for a real embedding pass.
EOF
done

log "launching 3 parallel kg add processes"
kg add "$VAULT/notes/n1.md" --endpoint "$OKTO_NEURON_ENDPOINT" >"$work_dir/a1.out" 2>"$work_dir/a1.err" &
P1=$!
kg add "$VAULT/notes/n2.md" --endpoint "$OKTO_NEURON_ENDPOINT" >"$work_dir/a2.out" 2>"$work_dir/a2.err" &
P2=$!
kg add "$VAULT/notes/n3.md" --endpoint "$OKTO_NEURON_ENDPOINT" >"$work_dir/a3.out" 2>"$work_dir/a3.err" &
P3=$!
wait $P1; R1=$?
wait $P2; R2=$?
wait $P3; R3=$?
log "exit codes: $R1 $R2 $R3"

# Real user expectation: all three succeed (lock should queue or retry).
assert_exit_code 0 "$R1"
assert_exit_code 0 "$R2"
assert_exit_code 0 "$R3"
assert_not_contains "$work_dir/a1.err" "VaultLockedError"
assert_not_contains "$work_dir/a2.err" "VaultLockedError"
assert_not_contains "$work_dir/a3.err" "VaultLockedError"
# Backend-neutral "the live graph actually grew" check (see _lib.sh's
# assert_graph_populated).
assert_graph_populated "$VAULT" "after_parallel_add"

stop_server
finish
