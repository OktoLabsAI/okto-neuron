#!/usr/bin/env bash
# Scenario 54 (client/server): edit exactly one of ten real ingest windows via
# MCP remember. Nine content-addressed Block ids must remain stable, one must
# change. Explore must still reach the document for every sentinel, while the
# shared REST graph must expose every current Block id.
set -uo pipefail
SCENARIO_NAME="54_partial_edit_anchors"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

# KNOWN, DOCUMENTED GAP (not a fixable marginalia code defect): this scenario
# writes a single ~124KB markdown source file that ingests into one Block
# whose content is bound as a single write parameter. Grafx's own query
# planner (okto_grafx, not marginalia code -- out of scope to modify) hard-caps
# a query string, parameter values included, at 65536 characters
# ("GrafxConfigurationError: A query string may carry at most 65536
# characters"), so a Block content payload this large cannot be written
# through GrafxStore at all. Ladybug has no such cap. Skip cleanly on any
# non-ladybug backend rather than mask this as a marginalia regression.
#
# Effective backend defaults to grafx when OKTO_NEURON_ACCEPTANCE_BACKEND is
# unset (D-94: DEFAULT_NEW_VAULT_BACKEND flipped from ladybug to grafx), so
# the skip must fire on an unset/empty value too, not only an explicitly
# pinned non-ladybug name -- an unpinned default run now creates a grafx
# vault via kg_init_backend_args()'s own no-flag fallthrough below.
_effective_backend_54="${OKTO_NEURON_ACCEPTANCE_BACKEND:-grafx}"
if [[ "$_effective_backend_54" != "ladybug" ]]; then
  log "SKIPPED -- backend='${_effective_backend_54}' cannot write this scenario's ~124KB single-Block payload (Grafx query string is hard-capped at 65536 chars; see file header)"
  assert_exit_code 0 0
  finish
fi

VAULT="$work_dir/vault"
kg init "$VAULT" $(kg_init_backend_args) >/dev/null 2>&1
assert_exit_code 0 $?
okto-neuron onboard --vault "$VAULT" --disable-llm --non-interactive $(kg_init_backend_args) \
  >"$work_dir/onboard.stdout" 2>"$work_dir/onboard.stderr"
assert_exit_code 0 $?

python3 - "$VAULT/notes/anchors.md" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = [
    f"sentinel_{index:02d}_zzz ORIGINAL " + (chr(96 + index) * 12400)
    for index in range(1, 11)
]
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

if ! start_server "$VAULT"; then
  _failures+=("start_server failed; see $work_dir/server.log")
  finish
fi

python3 - "$OKTO_NEURON_MCP_ENDPOINT" "$OKTO_NEURON_ENDPOINT" "$VAULT/notes/anchors.md" "$VAULT" \
  >"$work_dir/client.out" 2>"$work_dir/client.err" <<'PY'
import asyncio
import json
import os
from pathlib import Path
import sys
import urllib.request

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


def current_block_ids():
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


async def unreachable_topics(client, document_id):
    missing = []
    for index in range(1, 11):
        graph = data(
            await client.call_tool(
                "explore", {"topic": f"sentinel_{index:02d}_zzz", "k": 20}
            )
        )
        if document_id not in graph.get("seeds", []):
            missing.append(index)
    return missing


async def main():
    before = current_block_ids()
    async with Client(URL, auth=TOKEN) as client:
        first = data(await client.call_tool("remember", {"source": str(SOURCE)}))
        missing_before = await unreachable_topics(client, first.get("document_id"))
        stored_before = stored_block_ids()

        SOURCE.write_text(
            SOURCE.read_text(encoding="utf-8").replace(
                "sentinel_07_zzz ORIGINAL", "sentinel_07_zzz EDITED"
            ),
            encoding="utf-8",
        )
        after = current_block_ids()
        second = data(await client.call_tool("remember", {"source": str(SOURCE)}))
        missing_after = await unreachable_topics(client, second.get("document_id"))
        stored_after = stored_block_ids()

    changed = [index for index, pair in enumerate(zip(before, after), start=1) if pair[0] != pair[1]]
    ten_blocks = len(before) == len(after) == 10
    stable_document = first.get("document_id") == second.get("document_id")
    exact_change = changed == [7]
    blocks_stored = set(before) <= stored_before and set(after) <= stored_after
    all_reachable = not missing_before and not missing_after
    print(f"ten_blocks={ten_blocks}")
    print(f"stable_document={stable_document}")
    print(f"changed_indices={changed}")
    print(f"blocks_stored={blocks_stored}")
    print(f"missing_before={missing_before}")
    print(f"missing_after={missing_after}")
    if not all((ten_blocks, stable_document, exact_change, blocks_stored, all_reachable)):
        print("FAIL partial_edit_anchor_contract")


asyncio.run(main())
PY
client_rc=$?
cat "$work_dir/client.out" >&2
assert_exit_code 0 "$client_rc"

stop_server

if grep -q "FAIL " "$work_dir/client.out"; then
  _failures+=("partial_edit_anchor_contract")
fi

finish
