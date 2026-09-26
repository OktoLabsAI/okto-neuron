#!/usr/bin/env bash
# Scenario 80: four authenticated FastMCP clients call the current remember
# tool concurrently against one running server. The shared writer lock must
# serialize every write: zero client errors, no VaultLockedError, and every
# returned document must be discoverable through explore.
set -uo pipefail
SCENARIO_NAME="80_mcp_parallel_clients"
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

for i in 1 2 3 4; do
  cat > "$VAULT/notes/m${i}.md" <<EOF
# MCP parallel ${i}
Sentinel mcpparallel_${i}_zzz with enough text to embed properly.
EOF
done

if ! start_server "$VAULT"; then
  _failures+=("start_server failed; see $work_dir/server.log")
  finish
fi

python3 - "$OKTO_NEURON_MCP_ENDPOINT" "$VAULT" >"$work_dir/client.out" 2>"$work_dir/client.err" <<'PY'
import asyncio
import os
from pathlib import Path
import sys

from fastmcp import Client

URL = sys.argv[1]
VAULT = Path(sys.argv[2])
TOKEN = os.environ["OKTO_NEURON_AUTH_TOKEN"]


def data(result):
    value = result.structured_content if hasattr(result, "structured_content") else result.data
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    return value if isinstance(value, dict) else {}


async def remember_one(index):
    async with Client(URL, auth=TOKEN) as client:
        try:
            result = data(
                await client.call_tool(
                    "remember", {"source": str(VAULT / "notes" / f"m{index}.md")}
                )
            )
            return ("ok", index, result.get("document_id"))
        except Exception as exc:
            return ("err", index, f"{type(exc).__name__}: {str(exc)[:200]}")


async def main():
    results = await asyncio.gather(*(remember_one(index) for index in range(1, 5)))
    for result in results:
        print(result)
    errors = sum(1 for result in results if result[0] == "err")

    verified = 0
    async with Client(URL, auth=TOKEN) as client:
        tools = sorted(tool.name for tool in await client.list_tools())
        print(f"tools={tools}")
        if tools != ["ask", "explore", "init_vault", "list_vaults", "remember"]:
            print("FAIL unexpected_tool_surface")
        for status, index, document_id in results:
            if status != "ok":
                continue
            graph = data(
                await client.call_tool(
                    "explore", {"topic": f"mcpparallel_{index}_zzz", "k": 12}
                )
            )
            if document_id in graph.get("seeds", []):
                verified += 1

    print(f"error_count={errors}")
    print(f"verified_documents={verified}/4")
    if errors or verified != 4:
        print("FAIL parallel_remember_contract")


asyncio.run(main())
PY
client_rc=$?
cat "$work_dir/client.out" >&2
assert_exit_code 0 "$client_rc"

stop_server

assert_contains "$work_dir/client.out" "error_count=0"
assert_contains "$work_dir/client.out" "verified_documents=4/4"
assert_not_contains "$work_dir/server.log" "VaultLockedError|lock is held"
if grep -q "FAIL " "$work_dir/client.out"; then
  _failures+=("parallel_remember_contract")
fi

finish
