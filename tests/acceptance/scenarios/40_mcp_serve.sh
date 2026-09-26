#!/usr/bin/env bash
# Scenario 40: real MCP server smoke under client/server mode.
# `start_server` brings up `okto-neuron serve` (REST + MCP same process).
# FastMCP client connects to OKTO_NEURON_MCP_ENDPOINT, lists tools, queries.
# Then SIGTERM via stop_server, then restart + query to prove vault re-opens
# clean (no orphan ladybug lock).
set -uo pipefail
SCENARIO_NAME="40_mcp_serve"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

VAULT="$work_dir/vault"
log "kg init"
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.stdout" 2>"$work_dir/init.stderr"
assert_exit_code 0 $?

cat > "$VAULT/notes/mcp_smoke.md" <<'EOF'
# MCP Smoke Note

Marginalia exposes a local-first knowledge graph through an MCP server.
Provenance hits include byte spans and content hashes.

Author: acceptance-harness.
EOF

if ! start_server "$VAULT"; then
  _failures+=("start_server failed; see $work_dir/server.log")
  finish
fi

# Seed via REST (thin client → server).
kg add "$VAULT/notes/mcp_smoke.md" \
  --endpoint "$OKTO_NEURON_ENDPOINT" \
  >"$work_dir/add.stdout" 2>"$work_dir/add.stderr"
assert_exit_code 0 $?

# Drive the live MCP endpoint with the real FastMCP client.
MCP_URL="${OKTO_NEURON_MCP_ENDPOINT}/"
log "MCP refuses a request without the application bearer capability"
unauth_code=$(curl -sS -o "$work_dir/mcp-unauth.json" -w "%{http_code}" \
  -X POST "$OKTO_NEURON_MCP_ENDPOINT")
assert_exit_code 401 "$unauth_code"

log "MCP accepts the application bearer through the real FastMCP client"
python3 - "$MCP_URL" >"$work_dir/client.stdout" 2>"$work_dir/client.stderr" <<'PY'
import asyncio, json, os, sys
from fastmcp import Client

URL = sys.argv[1]
TOKEN = os.environ["OKTO_NEURON_AUTH_TOKEN"]

async def main():
    async with Client(URL, auth=TOKEN) as c:
        tools = await c.list_tools()
        tool_names = sorted(t.name for t in tools)
        print(f"tools={tool_names}")
        result = await c.call_tool("explore", {"topic": "marginalia", "k": 5})
        payload = result.structured_content if hasattr(result, "structured_content") else result.data
        if isinstance(payload, dict) and "result" in payload:
            payload = payload["result"]
        nodes = payload.get("nodes", []) if isinstance(payload, dict) else []
        print(f"nodes_count={len(nodes)}")
        print("--- explored graph ---")
        print(json.dumps(payload, indent=2))

asyncio.run(main())
PY
client_rc=$?
cat "$work_dir/client.stdout" >&2
assert_exit_code 0 "$client_rc"
if [[ "$client_rc" -ne 0 ]]; then tail -30 "$work_dir/client.stderr" >&2; fi

assert_contains "$work_dir/client.stdout" "tools=.*ask.*explore.*init_vault.*remember"
assert_contains "$work_dir/client.stdout" "nodes_count=[1-9]"
assert_contains "$work_dir/client.stdout" '"name": "mcp_smoke"'

log "SIGTERM via stop_server"
stop_server

# Restart the server and prove the vault is re-openable (no orphan lock).
if ! start_server "$VAULT"; then
  _failures+=("start_server (restart) failed; see $work_dir/server.log")
  finish
fi
kg query "marginalia" \
  --endpoint "$OKTO_NEURON_ENDPOINT" \
  >"$work_dir/post_query.stdout" 2>"$work_dir/post_query.stderr"
post_rc=$?
assert_exit_code 0 "$post_rc"
assert_not_contains "$work_dir/post_query.stderr" "VaultLockedError"
stop_server

finish
