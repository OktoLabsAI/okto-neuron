#!/usr/bin/env bash
# Scenario 20: two sequential thin-client `kg add` calls from separate
# subshells against one isolated server must both succeed.
set -uo pipefail
SCENARIO_NAME="20_concurrent_add"
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

cat > "$VAULT/notes/a.md" <<'EOF'
# Note A
Okto Neuron provenance test note A.
EOF
cat > "$VAULT/notes/b.md" <<'EOF'
# Note B
Okto Neuron provenance test note B.
EOF

log "first kg add (subshell 1)"
( kg add "$VAULT/notes/a.md" --endpoint "$OKTO_NEURON_ENDPOINT" ) \
  >"$work_dir/add1.stdout" 2>"$work_dir/add1.stderr"
rc1=$?
log "second kg add (subshell 2, expects clean lock)"
( kg add "$VAULT/notes/b.md" --endpoint "$OKTO_NEURON_ENDPOINT" ) \
  >"$work_dir/add2.stdout" 2>"$work_dir/add2.stderr"
rc2=$?

assert_exit_code 0 "$rc1"
assert_exit_code 0 "$rc2"
assert_not_contains "$work_dir/add2.stderr" "VaultLockedError"
# Backend-neutral "the live graph actually grew" check (see _lib.sh's
# assert_graph_populated).
assert_graph_populated "$VAULT" "after_concurrent_add"

stop_server
finish
