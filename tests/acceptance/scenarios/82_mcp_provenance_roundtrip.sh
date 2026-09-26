#!/usr/bin/env bash
# Scenario 82: authenticated MCP remember/explore must land on the same shared
# graph whose public REST Block records carry honest byte provenance. For every
# sentinel, explore must reach the remembered document and a current Block's
# stored byte range/hash must round-trip to the source bytes.
set -uo pipefail
SCENARIO_NAME="82_mcp_provenance_roundtrip"
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

cat > "$VAULT/notes/prov.md" <<'EOF'
# Provenance Roundtrip Probe

This is paragraph alpha. It contains the unique sentinel ALPHA_SENTINEL_QWE.

This is paragraph beta. It contains the unique sentinel BETA_SENTINEL_ZXC.

This is paragraph gamma. It contains the unique sentinel GAMMA_SENTINEL_RTY.
EOF

if ! start_server "$VAULT"; then
  _failures+=("start_server failed; see $work_dir/server.log")
  finish
fi

python3 - "$OKTO_NEURON_MCP_ENDPOINT" "$OKTO_NEURON_ENDPOINT" "$VAULT/notes/prov.md" \
  >"$work_dir/client.out" 2>"$work_dir/client.err" <<'PY'
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.parse
import urllib.request

from fastmcp import Client

MCP_URL = sys.argv[1]
REST_URL = sys.argv[2]
SOURCE = Path(sys.argv[3]).resolve()
TOKEN = os.environ["OKTO_NEURON_AUTH_TOKEN"]
SENTINELS = ["ALPHA_SENTINEL_QWE", "BETA_SENTINEL_ZXC", "GAMMA_SENTINEL_RTY"]


def data(result):
    value = result.structured_content if hasattr(result, "structured_content") else result.data
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    return value if isinstance(value, dict) else {}


def get_json(path):
    request = urllib.request.Request(
        f"{REST_URL}{path}", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def current_blocks():
    listing = get_json("/api/v1/nodes?type=Block&limit=500")
    blocks = []
    raw = SOURCE.read_bytes()
    for row in listing.get("nodes", []):
        node_id = urllib.parse.quote(row["id"], safe="")
        detail = get_json(f"/api/v1/nodes/{node_id}")
        facets = detail.get("node", {}).get("facets", {})
        if Path(str(facets.get("source_path", ""))).resolve() != SOURCE:
            continue
        start = int(facets.get("byte_start", 0))
        end = int(facets.get("byte_end", 0))
        expected = str(facets.get("content_hash", "")).removeprefix("sha256:")
        chunk = raw[start:end]
        if end > start and hashlib.sha256(chunk).hexdigest() == expected:
            blocks.append((start, end, chunk))
    return blocks


async def main():
    reached = 0
    async with Client(MCP_URL, auth=TOKEN) as client:
        tools = sorted(tool.name for tool in await client.list_tools())
        print(f"tools={tools}")
        if tools != ["ask", "explore", "init_vault", "list_vaults", "remember"]:
            print("FAIL unexpected_tool_surface")
        remembered = data(await client.call_tool("remember", {"source": str(SOURCE)}))
        document_id = remembered.get("document_id")
        for sentinel in SENTINELS:
            graph = data(await client.call_tool("explore", {"topic": sentinel, "k": 12}))
            if document_id in graph.get("seeds", []):
                reached += 1
            else:
                print(f"{sentinel}: NO_DOCUMENT_SEED")

    blocks = current_blocks()
    verified = 0
    for sentinel in SENTINELS:
        encoded = sentinel.encode("utf-8")
        if any(encoded in chunk for _, _, chunk in blocks):
            verified += 1
            print(f"{sentinel}: OK")
        else:
            print(f"{sentinel}: NO_HASH_VALID_BLOCK")
    print(f"explore_reached={reached}/{len(SENTINELS)}")
    print(f"verified={verified}/{len(SENTINELS)}")
    print(f"hash_valid_blocks={len(blocks)}")
    if reached != len(SENTINELS) or verified != len(SENTINELS) or not blocks:
        print("FAIL provenance_roundtrip_contract")


asyncio.run(main())
PY
client_rc=$?
cat "$work_dir/client.out" >&2
assert_exit_code 0 "$client_rc"

stop_server

assert_contains "$work_dir/client.out" "explore_reached=3/3"
assert_contains "$work_dir/client.out" "verified=3/3"
assert_contains "$work_dir/client.out" "hash_valid_blocks=[1-9]"
if grep -q "FAIL " "$work_dir/client.out"; then
  _failures+=("provenance_roundtrip_contract")
fi

finish
