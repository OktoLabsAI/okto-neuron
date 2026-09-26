#!/usr/bin/env bash
# Scenario 55 (client/server): SIGKILL the okto-neuron serve process mid-ingest,
# then restart and retry. In client/server mode the writer lives in the server,
# so preserving the test's intent (crash the writer) means SIGKILLing the
# server, not the client. Vault must either auto-recover or surface clear
# stale-lock guidance. Related to vault-lock-no-wait-no-retry (card 7f165cfe).
set -uo pipefail
SCENARIO_NAME="55_crash_mid_ingest_recover"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

VAULT="$work_dir/vault"
kg init "$VAULT" $(kg_init_backend_args) >/dev/null 2>&1
assert_exit_code 0 $?
okto-neuron onboard --vault "$VAULT" --disable-llm --non-interactive $(kg_init_backend_args) \
  >"$work_dir/onboard.stdout" 2>"$work_dir/onboard.stderr"
assert_exit_code 0 $?

# Corpus large enough that we land mid-ingest when we kill the server.
for i in $(seq 1 20); do
  cat > "$VAULT/notes/c${i}.md" <<EOF
# Crash probe ${i}
Lots of unique content here block ${i} alpha beta gamma delta epsilon
zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau
upsilon phi chi psi omega — random sentinel crashprobe_${i}_qwe.
EOF
done

if ! start_server "$VAULT"; then
  _failures+=("start_server failed; see $work_dir/server.log")
  finish
fi
SERVER_PID_TO_KILL="$_MARG_SERVER_PID"

# Async client fires many remember calls; we SIGKILL the server mid-flight.
cat > "$work_dir/crasher.py" <<'PY'
import asyncio, sys
import os

from fastmcp import Client

URL=sys.argv[1]; VAULT=sys.argv[2]
TOKEN=os.environ["OKTO_NEURON_AUTH_TOKEN"]

async def add_one(c, path):
  try:
    await c.call_tool("remember", {"source": path})
    return ("ok", path)
  except Exception as e:
    return ("err", path, type(e).__name__+":"+str(e)[:120])

async def main():
  paths=[f"{VAULT}/notes/c{i}.md" for i in range(1,11)]
  try:
    async with Client(URL, auth=TOKEN) as c:
      results = await asyncio.gather(*[add_one(c,p) for p in paths], return_exceptions=True)
      for r in results: print(r)
  except Exception as e:
    print(f"client_outer_err={type(e).__name__}:{e}")

asyncio.run(main())
PY

python3 "$work_dir/crasher.py" "$OKTO_NEURON_MCP_ENDPOINT" "$VAULT" \
  >"$work_dir/client.out" 2>"$work_dir/client.err" &
CLIENT_PID=$!
sleep 0.5
log "SIGKILL serve pid ${SERVER_PID_TO_KILL} (mid-ingest)"
kill -KILL "$SERVER_PID_TO_KILL" 2>/dev/null || true
wait "$SERVER_PID_TO_KILL" 2>/dev/null || true
# A streamable-HTTP call that lost its server may wait for the transport's own
# long timeout. Bound the crash phase: disconnect/timeout is expected here; the
# restarted server below owns the actual recovery assertion.
for _ in $(seq 1 20); do
  if ! kill -0 "$CLIENT_PID" 2>/dev/null; then break; fi
  sleep 0.5
done
if kill -0 "$CLIENT_PID" 2>/dev/null; then
  log "terminating crash client after bounded disconnect wait"
  kill -TERM "$CLIENT_PID" 2>/dev/null || true
  for _ in $(seq 1 10); do
    if ! kill -0 "$CLIENT_PID" 2>/dev/null; then break; fi
    sleep 0.2
  done
fi
if kill -0 "$CLIENT_PID" 2>/dev/null; then
  kill -KILL "$CLIENT_PID" 2>/dev/null || true
fi
wait "$CLIENT_PID" 2>/dev/null || true
# Server is dead — clear preamble bookkeeping so trap doesn't redouble-kill.
_MARG_SERVER_PID=""
trap - EXIT

log "post-crash vault state:"
ls -la "$VAULT" >"$work_dir/post_crash_ls.txt" 2>&1
cat "$work_dir/post_crash_ls.txt" >&2

# Recovery path: restart via start_server. If it binds we have auto-recovery.
log "restart okto-neuron serve after crash"
if start_server "$VAULT"; then
  python3 - "$OKTO_NEURON_MCP_ENDPOINT" "$VAULT" >"$work_dir/retry.out" 2>"$work_dir/retry.err" <<'PY'
import asyncio, os, sys
from fastmcp import Client
URL=sys.argv[1]; VAULT=sys.argv[2]
TOKEN=os.environ["OKTO_NEURON_AUTH_TOKEN"]

async def main():
  async with Client(URL, auth=TOKEN) as c:
    try:
      await c.call_tool("remember", {"source": f"{VAULT}/notes/c11.md"})
      print("retry_remember=ok")
    except Exception as e:
      print(f"retry_remember_err={type(e).__name__}:{e}")
    r = await c.call_tool("explore", {"topic": "crashprobe_11_qwe", "k": 12})
    p = r.structured_content if hasattr(r,"structured_content") else r.data
    if isinstance(p, dict) and "result" in p: p = p["result"]
    seeds = p.get("seeds", []) if isinstance(p, dict) else []
    print(f"explore_seeds={len(seeds)}")

asyncio.run(main())
PY
  retry_rc=$?
  cat "$work_dir/retry.out" >&2
  if [[ "$retry_rc" -ne 0 ]] || ! grep -q "retry_remember=ok" "$work_dir/retry.out"; then
    _failures+=("retry_remember_failed_after_restart rc=$retry_rc")
  fi
  if ! grep -qE "explore_seeds=[1-9]" "$work_dir/retry.out"; then
    _failures+=("explore_failed_after_restart")
  fi
  stop_server
else
  # Server failed to restart. Honest stale-lock guidance is acceptable.
  if grep -qiE 'stale lock|recover|delete .* lock|pid .* not running|StaleLockError' "$work_dir/server.log" 2>/dev/null; then
    log "got actionable stale-lock guidance on restart — acceptable"
  else
    _failures+=("restart_failed_no_recovery_guidance")
    tail -40 "$work_dir/server.log" >&2 || true
  fi
fi

if [[ ${#_failures[@]} -gt 0 ]]; then
  finish "no-stale-lock-recovery"
else
  finish
fi
