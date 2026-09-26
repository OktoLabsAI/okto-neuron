#!/usr/bin/env bash
# Scenario 81: send invalid arguments to the current MCP tools. Every bad call
# must fail without killing the authenticated connection; a final explore call
# must still succeed and return the remembered document, and the tool surface
# must still match EXPECTED_TOOLS.
set -uo pipefail
SCENARIO_NAME="81_mcp_bad_args"
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

cat > "$VAULT/notes/seed.md" <<'EOF'
# Seed
Sentinel: badargsprobe.
EOF

if ! start_server "$VAULT"; then
  _failures+=("start_server failed; see $work_dir/server.log")
  finish
fi

python3 - "$OKTO_NEURON_MCP_ENDPOINT" "$VAULT/notes/seed.md" \
  >"$work_dir/client.out" 2>"$work_dir/client.err" <<'PY'
import asyncio
import os
import sys

from fastmcp import Client

URL = sys.argv[1]
SOURCE = sys.argv[2]
TOKEN = os.environ["OKTO_NEURON_AUTH_TOKEN"]
EXPECTED_TOOLS = ["ask", "explore", "init_vault", "list_vaults", "remember"]


def data(result):
    value = result.structured_content if hasattr(result, "structured_content") else result.data
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    return value if isinstance(value, dict) else {}


CASES = [
    ("explore_k_zero", "explore", {"topic": "badargsprobe", "k": 0}),
    ("explore_empty", "explore", {"topic": ""}),
    ("explore_none", "explore", {"topic": None}),
    ("remember_missing", "remember", {}),
    ("remember_bad_path", "remember", {"source": "/nonexistent/asdf.md"}),
    ("ask_missing", "ask", {}),
]


async def main():
    async with Client(URL, auth=TOKEN) as client:
        tools = sorted(tool.name for tool in await client.list_tools())
        print(f"tools={tools}")
        if tools != EXPECTED_TOOLS:
            print("FAIL unexpected_tool_surface")

        remembered = data(await client.call_tool("remember", {"source": SOURCE}))
        errors = 0
        for label, tool, arguments in CASES:
            try:
                await client.call_tool(tool, arguments)
                print(f"{label}: OK_UNEXPECTED")
            except Exception as exc:
                errors += 1
                message = str(exc).replace("\n", " ")[:160]
                print(f"{label}: ERR {type(exc).__name__}: {message}")

        graph = data(await client.call_tool("explore", {"topic": "badargsprobe", "k": 3}))
        good_call = remembered.get("document_id") in graph.get("seeds", [])
        final_tools = sorted(tool.name for tool in await client.list_tools())
        print(f"bad_error_count={errors}/{len(CASES)}")
        print(f"good_call={good_call}")
        tools_intact = final_tools == EXPECTED_TOOLS
        print(f"final_tools_count={len(final_tools)}/{len(EXPECTED_TOOLS)}")
        print(f"final_tools_intact={tools_intact}")
        if errors != len(CASES) or not good_call or not tools_intact:
            print("FAIL bad_args_connection_contract")


asyncio.run(main())
PY
client_rc=$?
cat "$work_dir/client.out" >&2
assert_exit_code 0 "$client_rc"

stop_server

assert_contains "$work_dir/client.out" "bad_error_count=6/6"
assert_contains "$work_dir/client.out" "good_call=True"
assert_contains "$work_dir/client.out" "final_tools_intact=True"
assert_not_contains "$work_dir/server.log" "CRITICAL|Server shutting down unexpectedly"
if grep -q "FAIL " "$work_dir/client.out"; then
  _failures+=("bad_args_connection_contract")
fi

finish
