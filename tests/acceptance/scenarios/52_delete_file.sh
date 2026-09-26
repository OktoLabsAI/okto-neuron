#!/usr/bin/env bash
# Scenario 52 (client/server): source deletion follows the current
# memory-accretes contract. MCP has exactly five tools (no delete/forget tool),
# remembering a now-missing path fails loudly, and already remembered knowledge
# remains explorable instead of being silently erased.
set -uo pipefail
SCENARIO_NAME="52_delete_file"
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

cat > "$VAULT/notes/keep.md" <<'EOF'
# Keep
Discusses the kiosk aberration triage agent.
EOF
cat > "$VAULT/notes/drop.md" <<'EOF'
# Drop
Discusses self-healing pipelines that remain remembered after source removal.
EOF

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


async def main():
    keep_path = VAULT / "notes" / "keep.md"
    drop_path = VAULT / "notes" / "drop.md"
    async with Client(URL, auth=TOKEN) as client:
        tools = sorted(tool.name for tool in await client.list_tools())
        print(f"tools={tools}")
        if tools != ["ask", "explore", "init_vault", "list_vaults", "remember"]:
            print("FAIL unexpected_tool_surface")

        keep = data(await client.call_tool("remember", {"source": str(keep_path)}))
        drop = data(await client.call_tool("remember", {"source": str(drop_path)}))
        drop_path.unlink()

        missing_path_error = False
        try:
            await client.call_tool("remember", {"source": str(drop_path)})
        except Exception as exc:
            missing_path_error = True
            print(f"missing_path_error_type={type(exc).__name__}")

        dropped_memory = data(
            await client.call_tool("explore", {"topic": "self-healing pipelines", "k": 12})
        )
        kept_memory = data(
            await client.call_tool("explore", {"topic": "kiosk aberration", "k": 12})
        )

    remembered_after_delete = drop.get("document_id") in dropped_memory.get("seeds", [])
    keep_remembered = keep.get("document_id") in kept_memory.get("seeds", [])
    print(f"missing_path_error={missing_path_error}")
    print(f"remembered_after_delete={remembered_after_delete}")
    print(f"keep_remembered={keep_remembered}")
    if not all((missing_path_error, remembered_after_delete, keep_remembered)):
        print("FAIL source_deletion_contract")


asyncio.run(main())
PY
client_rc=$?
cat "$work_dir/client.out" >&2
assert_exit_code 0 "$client_rc"

stop_server

if grep -q "FAIL " "$work_dir/client.out"; then
  _failures+=("source_deletion_contract")
fi

finish
