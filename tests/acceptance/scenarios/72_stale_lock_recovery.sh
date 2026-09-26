#!/usr/bin/env bash
# Scenario 72 (client/server): plant a stale lock file with a dead pid before
# `okto-neuron serve` boots. Acceptable outcomes:
#   (a) server reclaims the stale lock and comes up clean (ideal). Then a
#       normal POST /add via `kg add` must succeed (201 -> exit 0).
#   (b) server refuses to boot, but the operator-facing message names the
#       stale pid OR the lock file path (so they can remove it).
# Never acceptable: silent hang, raw Python traceback as the only signal.
set -uo pipefail
SCENARIO_NAME="72_stale_lock_recovery"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_preamble.sh"

VAULT="$work_dir/vault"
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.out" 2>"$work_dir/init.err"
assert_exit_code 0 $?

# Inventory existing lock-like artifacts post-init for diagnostic clarity.
find "$VAULT" -maxdepth 4 \( -name '*.lock' -o -name '*.lbug.lock' -o -name 'lock*' \) \
  2>/dev/null >"$work_dir/lock_candidates.txt" || true
log "lock candidates after init:"
cat "$work_dir/lock_candidates.txt" >&2

# Plant a stale lock at the conventional location with a pid that cannot
# possibly be a live process on a fresh CI box.
fake_lock="$VAULT/graph.lbug.lock"
echo "99999999" > "$fake_lock"

cat > "$VAULT/notes/probe.md" <<'EOF'
# Probe
Sentinel: stalelockprobe_xyz.
EOF

# Try to bring the server up.
log "attempting start_server with stale lock present"
if start_server "$VAULT"; then
  log "outcome (a): server recovered from stale lock"
  # Confirm we can actually write: POST /add via kg add must succeed.
  kg add "$VAULT/notes/probe.md" --endpoint "$OKTO_NEURON_ENDPOINT" \
    >"$work_dir/add.out" 2>"$work_dir/add.err"
  rc=$?
  log "kg add rc=${rc}"
  if [[ "$rc" -ne 0 ]]; then
    head -20 "$work_dir/add.err" >&2
    _failures+=("server_up_but_add_failed rc=${rc}")
  fi
  stop_server || true
else
  log "outcome (b): server refused to boot — check actionability"
  if [[ -f "$work_dir/server.log" ]]; then
    if grep -qiE 'stale.*lock|pid 99999999|graph\.lbug\.lock|remove.*lock|reclaim' "$work_dir/server.log"; then
      log "server log gave actionable stale-lock guidance — acceptable"
    else
      _failures+=("no_stale_lock_guidance_in_server_log")
    fi
    if grep -q "Traceback (most recent call last)" "$work_dir/server.log" \
       && ! grep -qiE 'stale.*lock|pid 99999999|graph\.lbug\.lock' "$work_dir/server.log"; then
      _failures+=("server_log_is_raw_traceback_only")
    fi
  else
    _failures+=("no_server_log_emitted")
  fi
fi

if [[ ${#_failures[@]} -gt 0 ]]; then
  finish "stale-lock-not-handled"
else
  finish
fi
