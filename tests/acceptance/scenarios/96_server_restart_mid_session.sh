#!/usr/bin/env bash
# Scenario 96: server restart mid-session.
#
# Start the marginalia server, ingest N notes (real bge-small via the server),
# record sentinel ids/contents, stop the server via `okto-neuron stop`, restart
# on the same vault, then query for the sentinels. Vault state and provenance
# must survive a clean restart.
#
# Assertions:
#   - `okto-neuron stop` exits 0 (first stop succeeds).
#   - Each boot owns a canonical, birth-bound application PID record.
#   - The lifecycle owner identity changes across the clean restart.
#   - Application PID file is removed cleanly after stop.
#   - Server process exits cleanly (no SIGKILL fallback needed).
#   - Server restarts on the same vault without StaleLockError.
#   - All sentinel queries return >=1 hit naming the originating file.
#   - No graph corruption: verify_ladybug_db_health returns ok post-restart.
set -uo pipefail
SCENARIO_NAME="96_server_restart_mid_session"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

validate_pid_record() {
  python3 - "$1" "$2" <<'PY'
import json
import string
import sys
from pathlib import Path

from okto_neuron.server import lifecycle

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
expected_pid = int(sys.argv[2])


def fail(message: str) -> None:
    raise SystemExit(f"invalid canonical PID record: {message}")


if not isinstance(payload, dict):
    fail("payload is not an object")
if set(payload) != {"version", "pid", "start_token", "owner_id"}:
    fail("payload keys do not match the canonical schema")
if type(payload["version"]) is not int:
    fail("version is not an integer")
if payload["version"] != lifecycle.PID_RECORD_VERSION:
    fail("version does not match the lifecycle contract")
if type(payload["pid"]) is not int or payload["pid"] != expected_pid:
    fail("pid does not match the live owner")
if not isinstance(payload["start_token"], str) or not payload["start_token"]:
    fail("start_token is missing")
current_token = lifecycle._process_start_token(expected_pid)
if current_token is None or payload["start_token"] != current_token:
    fail("start_token does not match the live process birth identity")
owner_id = payload["owner_id"]
if not isinstance(owner_id, str) or len(owner_id) != 32:
    fail("owner_id is not a 32-character string")
if not all(char in string.hexdigits for char in owner_id):
    fail("owner_id is not hexadecimal")
print(owner_id)
PY
}

VAULT="$work_dir/vault"
log "kg init"
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.stdout" 2>"$work_dir/init.stderr"
assert_exit_code 0 $?

# ---- Phase 1: first server boot, ingest sentinels ---------------------------
log "phase1: start_server"
if ! start_server "$VAULT"; then
  _failures+=("phase1_start_server_failed")
  finish
fi
PHASE1_PID="$_MARG_SERVER_PID"
log "phase1: PID=${PHASE1_PID} endpoint=${OKTO_NEURON_ENDPOINT}"

# Author N notes with unique sentinels.
N=6
declare -a SENTINELS=()
for i in $(seq 1 $N); do
  sentinel="restartprobe_$(printf '%02d' "$i")_$(python3 -c 'import secrets;print(secrets.token_hex(4))')"
  SENTINELS+=("$sentinel")
  cat > "$VAULT/notes/sn${i}.md" <<EOF
# Restart probe ${i}

Unique sentinel token: ${sentinel}.

This note exercises persistence across a clean SIGTERM stop and a fresh
server start on the same vault. Content body: alpha beta gamma delta
epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma
tau upsilon phi chi psi omega.
EOF
  log "phase1: okto-neuron add sn${i}.md sentinel=${sentinel}"
  okto-neuron add "$VAULT/notes/sn${i}.md" --endpoint "$OKTO_NEURON_ENDPOINT" \
    >"$work_dir/add_${i}.out" 2>"$work_dir/add_${i}.err"
  rc=$?
  if [[ "$rc" -ne 0 ]]; then
    _failures+=("phase1_add_failed i=$i rc=$rc")
    log "FAIL phase1 add sn${i} rc=$rc"
    tail -10 "$work_dir/add_${i}.err" >&2
  fi
done
printf '%s\n' "${SENTINELS[@]}" > "$work_dir/sentinels.txt"

# Pre-restart query sanity check. The server returns hits keyed by the
# originating file path; query for the sentinel and assert that the
# matching note's path comes back with a perfect score.
log "phase1: pre-restart query for ${SENTINELS[0]}"
okto-neuron query "${SENTINELS[0]}" --endpoint "$OKTO_NEURON_ENDPOINT" --k 5 --format json \
  >"$work_dir/pre_restart_q.json" 2>"$work_dir/pre_restart_q.err"
pre_rc=$?
assert_exit_code 0 "$pre_rc"
assert_contains "$work_dir/pre_restart_q.json" "sn1\\.md"
assert_contains "$work_dir/pre_restart_q.json" "\"score\": 1.0"

# PID file must exist while the server runs.
PID_FILE="$HOME/.okto-neuron/runtime/.marginalia/server.pid"
assert_file_exists "$PID_FILE"
phase1_owner_id="$(validate_pid_record "$PID_FILE" "$PHASE1_PID")"
phase1_record_rc=$?
assert_exit_code 0 "$phase1_record_rc"
if [[ "$phase1_record_rc" -eq 0 ]]; then
  log "phase1: canonical PID record verified"
fi

# ---- Phase 2: clean stop via the CLI ----------------------------------------
log "phase2: okto-neuron stop"
okto-neuron stop >"$work_dir/stop.stdout" 2>"$work_dir/stop.stderr"
stop_rc=$?
log "phase2: okto-neuron stop rc=${stop_rc}"
assert_exit_code 0 "$stop_rc"

# Reap the server process started by start_server (it was launched in this
# shell via &). okto-neuron stop signalled it; just wait.
exit_code=0
if [[ -n "${_MARG_SERVER_PID:-}" ]]; then
  for i in $(seq 1 40); do
    if ! kill -0 "$_MARG_SERVER_PID" 2>/dev/null; then
      log "phase2: server pid ${_MARG_SERVER_PID} exited after ${i} polls"
      break
    fi
    sleep 0.5
  done
  if kill -0 "$_MARG_SERVER_PID" 2>/dev/null; then
    _failures+=("server_did_not_exit_after_marginalia_stop pid=${_MARG_SERVER_PID}")
    log "FAIL server still alive after okto-neuron stop — forcing SIGKILL"
    kill -KILL "$_MARG_SERVER_PID" 2>/dev/null || true
  fi
  wait "$_MARG_SERVER_PID" 2>/dev/null
  exit_code=$?
  log "phase2: server exit_code=${exit_code}"
  if [[ "$exit_code" -ne 0 ]]; then
    _failures+=("server_exit_nonzero_on_first_stop exit_code=${exit_code}")
  else
    _assertions=$((_assertions+1))
  fi
fi

# Disarm the preamble's EXIT trap; we no longer have a running server.
_MARG_SERVER_PID=""
_MARG_SERVER_VAULT=""
unset OKTO_NEURON_ENDPOINT OKTO_NEURON_MCP_ENDPOINT
trap - EXIT

# PID file must be gone after a clean stop.
if [[ -e "$PID_FILE" ]]; then
  _failures+=("pid_file_not_removed path=${PID_FILE}")
  log "FAIL PID file still present after okto-neuron stop"
  ls -la "$HOME/.okto-neuron/runtime/.marginalia/" >&2
else
  log "phase2: PID file removed cleanly"
  _assertions=$((_assertions+1))
fi

# ---- Phase 3: restart on the same vault -------------------------------------
log "phase3: restart server on same vault"
if ! start_server "$VAULT"; then
  _failures+=("phase3_restart_failed")
  finish
fi
log "phase3: PID=${_MARG_SERVER_PID} endpoint=${OKTO_NEURON_ENDPOINT}"
phase3_owner_id="$(validate_pid_record "$PID_FILE" "$_MARG_SERVER_PID")"
phase3_record_rc=$?
assert_exit_code 0 "$phase3_record_rc"
if [[ "$phase3_record_rc" -eq 0 ]]; then
  log "phase3: canonical PID record verified"
fi
if [[ "$phase1_record_rc" -eq 0 && "$phase3_record_rc" -eq 0 ]]; then
  if [[ "$phase3_owner_id" == "$phase1_owner_id" ]]; then
    _failures+=("pid_owner_id_reused_across_restart")
    log "FAIL lifecycle owner identity was reused across restart"
  else
    _assertions=$((_assertions+1))
  fi
fi

# Server boot log must not surface a stale-lock complaint.
if [[ -n "${_MARG_SERVER_LOG:-}" && -f "$_MARG_SERVER_LOG" ]]; then
  assert_not_contains "$_MARG_SERVER_LOG" "StaleLockError"
fi

# Query every sentinel and confirm retrieval after restart.
miss_count=0
for i in $(seq 1 $N); do
  sentinel="${SENTINELS[$((i-1))]}"
  log "phase3: query sentinel=${sentinel}"
  okto-neuron query "$sentinel" --endpoint "$OKTO_NEURON_ENDPOINT" --k 5 --format json \
    >"$work_dir/post_q_${i}.json" 2>"$work_dir/post_q_${i}.err"
  qrc=$?
  if [[ "$qrc" -ne 0 ]]; then
    miss_count=$((miss_count+1))
    _failures+=("post_restart_query_rc i=$i rc=$qrc sentinel=$sentinel")
    continue
  fi
  # Hits are keyed by path; the matching note must come back with score 1.0.
  if ! grep -qE "sn${i}\.md" "$work_dir/post_q_${i}.json"; then
    miss_count=$((miss_count+1))
    _failures+=("post_restart_path_missing i=$i sentinel=$sentinel")
    log "FAIL path sn${i}.md not in query results for ${sentinel}"
    head -40 "$work_dir/post_q_${i}.json" >&2
    continue
  fi
  if ! grep -q '"score": 1.0' "$work_dir/post_q_${i}.json"; then
    miss_count=$((miss_count+1))
    _failures+=("post_restart_low_score i=$i sentinel=$sentinel")
    log "FAIL no score=1.0 hit for sentinel ${sentinel} on sn${i}.md"
  fi
done

if [[ "$miss_count" -eq 0 ]]; then
  log "phase3: all ${N} sentinels retrieved after restart"
  _assertions=$((_assertions+1))
fi

# ---- Phase 4: stop the restarted server, then verify graph health ----------
log "phase4: stop restarted server"
okto-neuron stop >"$work_dir/stop2.stdout" 2>"$work_dir/stop2.stderr" || true
if [[ -n "${_MARG_SERVER_PID:-}" ]]; then
  wait "$_MARG_SERVER_PID" 2>/dev/null || true
fi
_MARG_SERVER_PID=""
unset OKTO_NEURON_ENDPOINT OKTO_NEURON_MCP_ENDPOINT
trap - EXIT

# verify_ladybug_db_health opens the DB read-only — only safe once the server
# has fully released its handle. Returns True on healthy schema; raises
# VaultCorrupted on damage. This check is inherently Ladybug-specific (it
# imports marginalia.store.ladybug and opens the literal graph.lbug file), so
# it only runs when the vault is actually, explicitly pinned to ladybug; a
# non-ladybug backend (e.g. the CLI's own grafx default, DEFAULT_NEW_VAULT_BACKEND
# since D-94 -- or --backend grafx/neo4j) has no graph.lbug to open and skips
# cleanly with a logged reason instead of failing on a file that was never
# supposed to exist for that backend. Unlike the graph-populated checks
# elsewhere in this suite, there is no grafx counterpart to
# verify_ladybug_db_health to substitute here -- an unset/empty
# OKTO_NEURON_ACCEPTANCE_BACKEND must NOT be treated as ladybug any more, so
# this only fires on an explicit "ladybug" pin.
if [[ "${OKTO_NEURON_ACCEPTANCE_BACKEND:-}" == "ladybug" ]]; then
  log "phase4: verify_ladybug_db_health"
  python3 - "$VAULT" >"$work_dir/health.stdout" 2>"$work_dir/health.stderr" <<'PY'
import sys
from pathlib import Path
from okto_neuron.store.ladybug import verify_ladybug_db_health
vault = Path(sys.argv[1])
graph = vault / "graph.lbug"
try:
    ok = verify_ladybug_db_health(graph)
    print(f"ok={bool(ok)}")
    sys.exit(0 if ok else 2)
except Exception as exc:
    print(f"ok=False detail={type(exc).__name__}: {exc}")
    sys.exit(3)
PY
  health_rc=$?
  assert_exit_code 0 "$health_rc"
  assert_contains "$work_dir/health.stdout" "ok=True"
else
  log "phase4: verify_ladybug_db_health SKIPPED -- backend='${OKTO_NEURON_ACCEPTANCE_BACKEND:-grafx}' has no graph.lbug (Ladybug-only on-disk format)"
fi

finish
