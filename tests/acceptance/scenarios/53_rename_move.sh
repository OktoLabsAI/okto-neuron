#!/usr/bin/env bash
# Scenario 53 (client/server): move/rename a remembered source and remember the
# new path. Document identity is path-derived, so the new source gets a distinct
# id while the prior memory remains available (memory accretes; it does not
# pretend a filesystem move is a delete transaction).
set -uo pipefail
SCENARIO_NAME="53_rename_move"
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

cat > "$VAULT/notes/movable.md" <<'EOF'
# Movable
Unique sentinel string: blueparrot_chartreuse_xyz.
EOF

if ! start_server "$VAULT"; then
  _failures+=("start_server failed; see $work_dir/server.log")
  finish
fi

python3 - "$OKTO_NEURON_MCP_ENDPOINT" "$VAULT" >"$work_dir/client.out" 2>"$work_dir/client.err" <<'PY'
import asyncio
import os
from pathlib import Path
import shutil
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
    old_path = VAULT / "notes" / "movable.md"
    new_path = VAULT / "notes" / "projects" / "moved.md"
    async with Client(URL, auth=TOKEN) as client:
        first = data(await client.call_tool("remember", {"source": str(old_path)}))
        new_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(old_path, new_path)
        second = data(await client.call_tool("remember", {"source": str(new_path)}))
        explored = data(
            await client.call_tool("explore", {"topic": "blueparrot_chartreuse_xyz", "k": 20})
        )

    first_id = first.get("document_id")
    second_id = second.get("document_id")
    seeds = explored.get("seeds", [])
    distinct_documents = bool(first_id and second_id and first_id != second_id)
    old_memory_retained = first_id in seeds
    new_memory_reachable = second_id in seeds
    print(f"distinct_documents={distinct_documents}")
    print(f"old_memory_retained={old_memory_retained}")
    print(f"new_memory_reachable={new_memory_reachable}")
    if not all((distinct_documents, old_memory_retained, new_memory_reachable)):
        print("FAIL move_path_identity_contract")


asyncio.run(main())
PY
client_rc=$?
cat "$work_dir/client.out" >&2
assert_exit_code 0 "$client_rc"

stop_server

if grep -q "FAIL " "$work_dir/client.out"; then
  _failures+=("move_path_identity_contract")
fi

finish
