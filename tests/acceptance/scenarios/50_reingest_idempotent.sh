#!/usr/bin/env bash
# Scenario 50 (client/server): remember the same file twice via the current MCP
# surface. The second call must preserve the document identity and explore seed
# set. LLM extraction is explicitly disabled because this scenario owns the
# deterministic ingest/idempotency contract, not model quality.
set -uo pipefail
SCENARIO_NAME="50_reingest_idempotent"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

VAULT="$work_dir/vault"
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.stdout" 2>"$work_dir/init.stderr"
assert_exit_code 0 $?
okto-neuron onboard --vault "$VAULT" --disable-llm --non-interactive $(kg_init_backend_args) \
  >"$work_dir/onboard.stdout" 2>"$work_dir/onboard.stderr"
assert_exit_code 0 $?

cat > "$VAULT/notes/idem.md" <<'EOF'
# Idempotency Probe

The same content ingested twice must not duplicate Claims or Blocks.
Provenance content_hash is the deduplication key per RFC §4.2.
EOF

if ! start_server "$VAULT"; then
  _failures+=("start_server failed; see $work_dir/server.log")
  finish
fi

python3 - "$OKTO_NEURON_MCP_ENDPOINT" >"$work_dir/client.out" 2>"$work_dir/client.err" <<'PY'
import asyncio
import os
import sys

from fastmcp import Client

URL = sys.argv[1]
TOKEN = os.environ["OKTO_NEURON_AUTH_TOKEN"]
SOURCE = os.path.join(os.environ["SCENARIO_WORK_DIR"], "vault", "notes", "idem.md")


def payload(result):
    data = result.structured_content if hasattr(result, "structured_content") else result.data
    if isinstance(data, dict) and set(data) == {"result"}:
        data = data["result"]
    return data if isinstance(data, dict) else {}


async def await_job(client, queued):
    """P1: remember is ASYNC — poll ingest_status to done/ok (bounded, loud)."""
    status = {}
    for _ in range(600):
        status = payload(await client.call_tool("ingest_status", {"job_id": queued.get("job_id")}))
        if status.get("status") in {"done", "error", "cancelled"}:
            break
        await asyncio.sleep(0.1)
    else:
        raise SystemExit(f"FAIL ingest_status timeout: {status}")
    if status.get("status") != "done" or status.get("ok") is not True:
        raise SystemExit(f"FAIL ingest not ok: {status}")
    return status


async def main():
    async with Client(URL, auth=TOKEN) as client:
        tools = sorted(tool.name for tool in await client.list_tools())
        print(f"tools={tools}")
        if tools != ["ask", "explore", "ingest_status", "init_vault", "list_vaults", "remember"]:
            print("FAIL unexpected_tool_surface")

        first = await await_job(client, payload(await client.call_tool("remember", {"source": SOURCE})))
        first_graph = payload(
            await client.call_tool("explore", {"topic": "idempotency", "k": 12})
        )
        second = await await_job(client, payload(await client.call_tool("remember", {"source": SOURCE})))
        second_graph = payload(
            await client.call_tool("explore", {"topic": "idempotency", "k": 12})
        )

        same_document = first.get("document_id") == second.get("document_id")
        same_seeds = first_graph.get("seeds") == second_graph.get("seeds")
        # P1: remember returns the queued job; llm_disabled lives on the
        # finished job's status payload (the worker's remember result).
        disabled = first.get("llm_disabled") is True and second.get("llm_disabled") is True
        print(f"same_document={same_document}")
        print(f"same_seeds={same_seeds}")
        print(f"llm_disabled={disabled}")
        print(f"seed_count={len(second_graph.get('seeds', []))}")
        if not same_document:
            print("FAIL document_identity_drift")
        if not same_seeds:
            print("FAIL explore_seed_drift")
        if not disabled:
            print("FAIL model_free_contract_missing")


asyncio.run(main())
PY
client_rc=$?
cat "$work_dir/client.out" >&2
assert_exit_code 0 "$client_rc"

stop_server

if grep -q "FAIL " "$work_dir/client.out"; then
  _failures+=("mcp_remember_not_idempotent")
fi
assert_contains "$work_dir/client.out" "seed_count=[1-9]"

finish
