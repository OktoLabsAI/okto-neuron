#!/usr/bin/env bash
# Scenario 51 (client/server): edit one of two real 12k ingest windows and
# re-remember through MCP. The document id and unchanged window stay stable;
# the edited window gets a new content-addressed Block id and is discoverable
# through the current explore tool.
set -uo pipefail
SCENARIO_NAME="51_edit_reanchor"
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

python3 - "$VAULT/notes/edit.md" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
alpha = "alpha paragraph discusses self-healing kiosks " + ("a" * 12400)
beta = "beta paragraph covers transcript ingestion pipelines " + ("b" * 12400)
path.write_text(f"{alpha}\n{beta}\n", encoding="utf-8")
PY

if ! start_server "$VAULT"; then
  _failures+=("start_server failed; see $work_dir/server.log")
  finish
fi

python3 - "$OKTO_NEURON_MCP_ENDPOINT" "$OKTO_NEURON_ENDPOINT" "$VAULT/notes/edit.md" "$VAULT" \
  >"$work_dir/client.out" 2>"$work_dir/client.err" <<'PY'
import asyncio
import os
from pathlib import Path
import sys
import urllib.request
import json

from fastmcp import Client
from okto_neuron.ingest.markdown import parse_markdown

URL = sys.argv[1]
REST_URL = sys.argv[2]
SOURCE = Path(sys.argv[3])
VAULT_ROOT = Path(sys.argv[4])
TOKEN = os.environ["OKTO_NEURON_AUTH_TOKEN"]


def data(result):
    value = result.structured_content if hasattr(result, "structured_content") else result.data
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    return value if isinstance(value, dict) else {}


def block_ids():
    # Must mirror ingest_document's own parse_markdown call (finding 3.4's
    # vault-relative Block-id fix): passing vault_root here is not optional
    # dressing, it changes the minted id. Without it this oracle predicts the
    # bare-filename fallback id while the server (which always has a vault
    # root, per vault.py's Vault.add) stores the vault-relative one, and the
    # two id sets never match.
    parsed = parse_markdown(
        SOURCE, extraction_activity_id="acceptance", agent_id="acceptance", vault_root=VAULT_ROOT
    )
    return [block.block.id for block in parsed.blocks]


def stored_block_ids():
    request = urllib.request.Request(
        f"{REST_URL}/api/v1/nodes?type=Block&limit=500",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return {row["id"] for row in json.load(response).get("nodes", [])}


async def main():
    before_blocks = block_ids()
    if len(before_blocks) != 2:
        print(f"FAIL expected_two_blocks actual={len(before_blocks)}")

    async with Client(URL, auth=TOKEN) as client:
        first = data(await client.call_tool("remember", {"source": str(SOURCE)}))
        alpha_before = data(await client.call_tool("explore", {"topic": "alpha paragraph"}))
        stored_before = stored_block_ids()

        text = SOURCE.read_text(encoding="utf-8")
        SOURCE.write_text(
            text.replace("transcript ingestion pipelines", "RAG retrieval pipelines"),
            encoding="utf-8",
        )
        after_blocks = block_ids()
        second = data(await client.call_tool("remember", {"source": str(SOURCE)}))
        alpha_after = data(await client.call_tool("explore", {"topic": "alpha paragraph"}))
        rag_after = data(await client.call_tool("explore", {"topic": "RAG retrieval pipelines"}))
        stored_after = stored_block_ids()

    stable_document = first.get("document_id") == second.get("document_id")
    stable_alpha = before_blocks[0] == after_blocks[0]
    changed_beta = before_blocks[1] != after_blocks[1]
    blocks_stored = set(before_blocks) <= stored_before and set(after_blocks) <= stored_after
    document_id = second.get("document_id")
    alpha_reachable = document_id in alpha_before.get("seeds", []) and document_id in alpha_after.get("seeds", [])
    rag_reachable = document_id in rag_after.get("seeds", [])
    print(f"stable_document={stable_document}")
    print(f"stable_alpha_block={stable_alpha}")
    print(f"changed_beta_block={changed_beta}")
    print(f"blocks_stored={blocks_stored}")
    print(f"alpha_reachable={alpha_reachable}")
    print(f"rag_reachable={rag_reachable}")
    if not all((stable_document, stable_alpha, changed_beta, blocks_stored, alpha_reachable, rag_reachable)):
        print("FAIL edit_reanchor_contract")


asyncio.run(main())
PY
client_rc=$?
cat "$work_dir/client.out" >&2
assert_exit_code 0 "$client_rc"

stop_server

if grep -q "FAIL " "$work_dir/client.out"; then
  _failures+=("edit_reanchor_contract")
fi

finish
