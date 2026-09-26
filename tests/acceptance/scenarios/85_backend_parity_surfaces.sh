#!/usr/bin/env bash
# Scenario 85: Definition-of-done parity (D-89). A user picking Ladybug,
# Grafx, or Neo4j at vault creation gets identical behaviour from CLI, MCP,
# REST/UI, and SDK. Per backend: kg init + 3 notes + kg init(graph) + query
# via all four surfaces + kg snapshot dump counts + curation rebuild/rollback
# 202 path. This scenario itself always targets ONE backend — the one the
# harness pinned via `bin/acceptance.sh --backend NAME` (grafx when unpinned,
# matching DEFAULT_NEW_VAULT_BACKEND since D-94; it was ladybug pre-D-94) —
# matching every other *_backend_selection.sh scenario's convention. Run it
# three times (default/grafx, --backend ladybug, --backend neo4j) to cover
# all three backends; cross-backend id/count equality is asserted by
# comparing this scenario's own report.jsonl-adjacent parity_<backend>.json
# artifacts across the three runs (see bin/ wrapper).
set -uo pipefail
SCENARIO_NAME="85_backend_parity_surfaces"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

BACKEND="${OKTO_NEURON_ACCEPTANCE_BACKEND:-grafx}"
log "parity scenario targeting backend=$BACKEND"

VAULT="$work_dir/vault"

# ---------------------------------------------------------------------------
# 1. CLI: init + 3 notes + kg init (graph) + query + snapshot dump counts.
# `kg_init_backend_args()` now emits `--backend $OKTO_NEURON_ACCEPTANCE_BACKEND`
# for ANY pinned backend, ladybug included (see _lib.sh) -- an unpinned run
# gets no flag at all and falls through to the CLI's own grafx default,
# which is exactly $BACKEND's own fallback above.
# ---------------------------------------------------------------------------
INIT_ARGS="$(kg_init_backend_args)"
log "okto-neuron init $VAULT $INIT_ARGS"
# shellcheck disable=SC2086
okto-neuron init "$VAULT" $INIT_ARGS \
  >"$work_dir/cli_init.stdout" 2>"$work_dir/cli_init.stderr"
assert_exit_code 0 $?
assert_file_exists "$VAULT/okto-neuron.yaml"
assert_contains "$VAULT/okto-neuron.yaml" "^  backend: ${BACKEND}\$"

NOTES_DIR="$VAULT/notes"
mkdir -p "$NOTES_DIR"
cat > "$NOTES_DIR/parity-one.md" <<'EOF'
# Parity Note One
PARITY_SENTINEL_ALPHA describes volcanic ash dispersal over mountain valleys.
EOF
cat > "$NOTES_DIR/parity-two.md" <<'EOF'
# Parity Note Two
PARITY_SENTINEL_BETA describes a second, unrelated hydrology survey.
EOF
cat > "$NOTES_DIR/parity-three.md" <<'EOF'
# Parity Note Three
PARITY_SENTINEL_ALPHA reappears here alongside glacial melt records.
EOF

log "okto-neuron onboard --vault $VAULT --disable-llm (model-free)"
okto-neuron onboard --vault "$VAULT" --disable-llm --non-interactive $(kg_init_backend_args) \
  >"$work_dir/cli_onboard.stdout" 2>"$work_dir/cli_onboard.stderr"
assert_exit_code 0 $?

if ! start_server "$VAULT"; then
  _failures+=("start_server failed backend=$BACKEND; see $work_dir/server.log")
  finish
fi

log "kg add x3 (CLI ingest surface)"
for f in parity-one parity-two parity-three; do
  kg add "$NOTES_DIR/$f.md" --endpoint "$OKTO_NEURON_ENDPOINT" \
    >"$work_dir/cli_add_${f}.stdout" 2>"$work_dir/cli_add_${f}.stderr"
  assert_exit_code 0 $?
done

QUERY_TEXT="PARITY_SENTINEL_ALPHA"

log "kg query --format json (CLI query surface)"
kg query "$QUERY_TEXT" --endpoint "$OKTO_NEURON_ENDPOINT" --format json \
  >"$work_dir/cli_query.json" 2>"$work_dir/cli_query.stderr"
assert_exit_code 0 $?

# ---------------------------------------------------------------------------
# 2. SDK: same query via the public Vault API (in-process, same running
#    vault path re-opened read-only style is not possible while the server
#    holds the write lock, so the SDK check opens a SEPARATE, freshly synced
#    snapshot copy is unnecessary -- Vault.query is safe to call concurrently
#    with the server via the same on-disk store for ladybug/grafx; for a
#    fair, surface-equivalent check we instead call the SDK against the
#    running vault's OWN store through a short-lived read query helper that
#    talks to the server process directly is what CLI/REST already do, so the
#    SDK surface here demonstrates the *library* entry point: Vault.open on
#    the same path, used the way docs/examples show, after stopping the
#    server to avoid a lock conflict.
# ---------------------------------------------------------------------------
stop_server

log "kg snapshot dump (CLI counts surface; server stopped so the vault is free)"
SNAP_DIR="$work_dir/snapshot"
kg snapshot dump "$VAULT" "$SNAP_DIR" \
  >"$work_dir/cli_snapshot.stdout" 2>"$work_dir/cli_snapshot.stderr"
assert_exit_code 0 $?
assert_file_exists "$SNAP_DIR/manifest.json"

python3 - "$VAULT" "$QUERY_TEXT" "$work_dir/sdk_query.json" <<'PY'
import json
import sys
from pathlib import Path

from okto_neuron.vault import Vault

vault_path, text, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
v = Vault.open(Path(vault_path))
try:
    hits = v.query(text, k=10)
    rows = [
        {
            "document_id": h.node.id,
            "score": float(h.score),
            "path": getattr(h, "path", None),
        }
        for h in hits
    ]
finally:
    v.close()
Path(out_path).write_text(json.dumps(rows, indent=2), encoding="utf-8")
print(json.dumps(rows, indent=2))
PY
sdk_rc=$?
assert_exit_code 0 "$sdk_rc"

if ! start_server "$VAULT"; then
  _failures+=("start_server failed backend=$BACKEND phase=restart; see $work_dir/server.log")
  finish
fi

# ---------------------------------------------------------------------------
# 3. REST/UI: GET /api/v1/backends (lists all three), GET vault info/backend
#    pin, POST /recall (the route the Web UI's query-api.ts calls), GET
#    /api/v1/graph/stats, then the curation rebuild + rollback 202 path.
# ---------------------------------------------------------------------------
python3 - "$OKTO_NEURON_ENDPOINT" "$QUERY_TEXT" \
  "$work_dir/rest_backends.json" "$work_dir/rest_current.json" \
  "$work_dir/rest_recall.json" "$work_dir/rest_stats.json" \
  "$work_dir/rest_rebuild.json" "$work_dir/rest_rebuild_status.json" \
  "$work_dir/rest_rollback.json" \
  >"$work_dir/rest_check.stdout" 2>"$work_dir/rest_check.stderr" <<'PY'
import json
import sys
import time
import urllib.request

(endpoint, text, backends_out, current_out, recall_out, stats_out,
 rebuild_out, rebuild_status_out, rollback_out) = sys.argv[1:]


def get_json(path):
    with urllib.request.urlopen(f"{endpoint}{path}", timeout=15) as r:
        return r.status, json.load(r)


def post_json(path, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{endpoint}{path}", data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


ok = True

status, backends = get_json("/api/v1/backends")
Path_write = lambda p, o: __import__("pathlib").Path(p).write_text(json.dumps(o, indent=2), encoding="utf-8")
Path_write(backends_out, backends)
names = {b.get("name") for b in backends} if isinstance(backends, list) else set()
if status != 200 or not {"ladybug", "grafx", "neo4j"}.issubset(names):
    print(f"FAIL backends listing status={status} names={names}")
    ok = False

status, current = get_json("/api/v1/vaults/current")
Path_write(current_out, current)
if status != 200:
    print(f"FAIL vaults/current status={status}")
    ok = False

status, recall = post_json("/recall", {"query": text, "k": 10})
Path_write(recall_out, recall)
if status != 200 or "results" not in recall:
    print(f"FAIL /recall status={status} body={recall}")
    ok = False

status, stats = get_json("/api/v1/graph/stats")
Path_write(stats_out, stats)
if status != 200:
    print(f"FAIL graph/stats status={status}")
    ok = False

# curation rebuild -> 202 + job, poll status, then rollback -> 202 + job.
status, rebuild = post_json("/api/v1/curation/rebuild", {})
Path_write(rebuild_out, rebuild)
if status != 202:
    print(f"FAIL curation/rebuild status={status} body={rebuild}")
    ok = False
else:
    deadline = time.time() + 60
    phase = None
    last_status_payload = {}
    while time.time() < deadline:
        st, payload = get_json("/api/v1/curation/rebuild/status?kind=rebuild")
        last_status_payload = payload
        job = payload.get("last_rebuild") or {}
        phase = job.get("status")
        if phase in ("succeeded", "failed", "completed", "done", "error"):
            break
        time.sleep(1)
    Path_write(rebuild_status_out, last_status_payload)
    if phase not in ("succeeded", "completed", "done"):
        print(f"FAIL curation rebuild did not complete: phase={phase}")
        ok = False

# Give the job worker a moment to fully persist rollback evidence
# (checkpoint files written after the job's terminal status flips).
time.sleep(2)
status, rollback = post_json("/api/v1/curation/rollback", {})
Path_write(rollback_out, rollback)
if status != 202:
    # D-92 (reversal of D-89's ladybug caveat): the rebuild path now
    # publishes a generation-bound semantic-materialization receipt from the
    # vault's live config fingerprints whenever the candidate ledger has no
    # completed-run history to project (semantic_fingerprint.py's
    # ``_ledger_materialized_semantic_fingerprints``), so a model-free
    # rebuild is no longer receipt-less on Ladybug — 202 is required on all
    # three backends, matching Grafx's and Neo4j's own rollback contracts.
    print(f"FAIL curation/rollback status={status} body={rollback}")
    ok = False

print("REST_OK" if ok else "REST_FAIL")
sys.exit(0 if ok else 1)
PY
rest_rc=$?
cat "$work_dir/rest_check.stdout" >&2
if [[ "$rest_rc" -ne 0 ]]; then
  cat "$work_dir/rest_check.stderr" >&2
  _failures+=("rest_ui_surface_failed backend=$BACKEND")
fi
_assertions=$((_assertions+1))

# Give the rollback job a moment to settle before shutting the server down.
sleep 2

# ---------------------------------------------------------------------------
# 4. MCP: same query via the MCP `explore` tool (the retrieval surface the
#    existing MCP acceptance scenarios use — 82_mcp_provenance_roundtrip.sh),
#    compared to the REST/CLI/SDK hit ids.
# ---------------------------------------------------------------------------
python3 - "$OKTO_NEURON_MCP_ENDPOINT" "$QUERY_TEXT" "$work_dir/mcp_explore.json" \
  >"$work_dir/mcp_check.stdout" 2>"$work_dir/mcp_check.stderr" <<'PY'
import asyncio
import json
import os
import sys
from pathlib import Path

from fastmcp import Client

MCP_URL, TEXT, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
TOKEN = os.environ["OKTO_NEURON_AUTH_TOKEN"]


def data(result):
    value = result.structured_content if hasattr(result, "structured_content") else result.data
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    return value if isinstance(value, dict) else {}


async def main():
    async with Client(MCP_URL, auth=TOKEN) as client:
        graph = data(await client.call_tool("explore", {"topic": TEXT, "k": 10}))
    nodes = graph.get("nodes", [])
    ids = [n.get("id") for n in nodes if isinstance(n, dict) and n.get("id")]
    Path(OUT).write_text(json.dumps(ids, indent=2), encoding="utf-8")
    print(json.dumps(ids, indent=2))
    if not ids:
        print("FAIL_MCP_EMPTY")
        return 1
    return 0


sys.exit(asyncio.run(main()))
PY
mcp_rc=$?
cat "$work_dir/mcp_check.stdout" >&2
assert_exit_code 0 "$mcp_rc"

stop_server

# ---------------------------------------------------------------------------
# Cross-surface assertions for THIS backend.
# ---------------------------------------------------------------------------
python3 - "$work_dir/cli_query.json" "$work_dir/sdk_query.json" \
  "$work_dir/rest_recall.json" "$work_dir/mcp_explore.json" "$BACKEND" \
  "$acceptance_root/parity_${BACKEND}.json" "$SNAP_DIR/manifest.json" \
  >"$work_dir/cross_surface.stdout" 2>"$work_dir/cross_surface.stderr" <<'PY'
import json
import sys
from pathlib import Path

cli_p, sdk_p, rest_p, mcp_p, backend, out_p, manifest_p = sys.argv[1:]

cli = json.loads(Path(cli_p).read_text())
sdk = json.loads(Path(sdk_p).read_text())
rest = json.loads(Path(rest_p).read_text())
mcp_ids = json.loads(Path(mcp_p).read_text())
manifest = json.loads(Path(manifest_p).read_text())

# CLI json format: list of hit dicts with document_id (see cli/__init__.py query rendering).
cli_ids = [h.get("document_id") or h.get("claim_id") for h in cli] if isinstance(cli, list) else []
sdk_ids = [h.get("document_id") for h in sdk]
# Legacy `/recall` (the exact route frontend/src/services/query-api.ts calls)
# echoes the same back-compat "results" shape as `/query` — NOT `/api/v1/recall`'s
# "hits" key (verified empirically: this route's handler is server/http.py's
# module-level `recall()`, which wraps the identical `_rich_hit`/`_query_with_recall_cost`
# call `query()` and `api_recall()` also use, just under the CLI-era key name).
rest_ids = [h.get("document_id") for h in rest.get("results", [])]
mcp_id_set = set(mcp_ids)

ok = True
if not cli_ids:
    print("FAIL cli_ids empty"); ok = False
if cli_ids != sdk_ids:
    print(f"FAIL cli vs sdk id order differs: {cli_ids} != {sdk_ids}"); ok = False
if cli_ids != rest_ids:
    print(f"FAIL cli vs rest id order differs: {cli_ids} != {rest_ids}"); ok = False
# MCP has no raw top-k `query` tool (production 4-tool surface is
# ask/explore/init_vault/remember — see mcp_server.py's retirement note and
# runtime.py). `explore` is the closest read surface, but it seeds via
# `query_seeds(..., seed_diversity=...)` (companion/__init__.py's `explore`,
# diversity ON by default for ego-graph seeding) rather than plain
# `vault.query` — a different, diversity-reranked call, not the same
# operation CLI/SDK/REST make. So this only asserts the single top CLI/SDK/
# REST hit (the highest-scored document) is reachable from MCP's explore,
# not full order/set equality — recorded as a known architecture gap below,
# not silently strengthened into a false equivalence.
top_id = cli_ids[0] if cli_ids else None
mcp_reaches_top_hit = top_id in mcp_id_set if top_id else False
if not mcp_reaches_top_hit:
    print(f"FAIL top cli/sdk/rest hit {top_id!r} not reachable via mcp explore seeds {sorted(mcp_id_set)}")
    ok = False

result = {
    "backend": backend,
    "cli_ids": cli_ids,
    "sdk_ids": sdk_ids,
    "rest_ids": rest_ids,
    "mcp_ids": sorted(mcp_id_set),
    "node_count": manifest.get("node_count"),
    "edge_count": manifest.get("edge_count"),
    "ok": ok,
}
Path(out_p).write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result, indent=2))
sys.exit(0 if ok else 1)
PY
cross_rc=$?
cat "$work_dir/cross_surface.stdout" >&2
if [[ "$cross_rc" -ne 0 ]]; then
  cat "$work_dir/cross_surface.stderr" >&2
  _failures+=("cross_surface_parity_failed backend=$BACKEND")
fi
_assertions=$((_assertions+1))

log "wrote $acceptance_root/parity_${BACKEND}.json for cross-backend comparison"

finish
