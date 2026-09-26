#!/usr/bin/env bash
# Scenario 92: model-free recall quality eval against the REAL web-UI API surface.
#
# Builds a fresh vault from the fixed synthetic-vault fixture, starts the real
# `okto-neuron serve`, ingests every note via the real REST /add (real bge-small
# embeddings — no mocks), then drives the NEW web-UI endpoint /api/v1/recall for
# each golden query and scores three metrics via tests/eval/golden.py:
#
#   - expected-entity-present@k   (gate: >= 5/6)
#   - provenance-valid            (Claim/Block byte ranges sha256 back to source)
#   - partner-recall              (handoff-owner query MUST surface its source)
#
# Scenario 56 exclusively owns live extraction/answer acceptance. Scenario 92
# never probes an ambient LLM endpoint, so its outcome is deterministic regardless
# of what model services happen to be running. No mocks.
set -uo pipefail
SCENARIO_NAME="92_eval_recall_provenance"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_preamble.sh"

export PYTHONPATH="$REPO_ROOT${PYTHONPATH:+:$PYTHONPATH}"
VAULT="$work_dir/vault"
FIXTURE="$REPO_ROOT/tests/fixtures/synthetic-vault"

log "kg init $VAULT"
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.out" 2>"$work_dir/init.err"
assert_exit_code 0 $?
assert_file_exists "$VAULT/okto-neuron.yaml"

log "copying fixed synthetic-vault notes into vault"
# Copy markdown fixture tree under the vault's notes-bearing root.
cp -R "$FIXTURE/." "$VAULT/" 2>/dev/null || true

log "start_server $VAULT"
if ! start_server "$VAULT"; then
  _failures+=("start_server failed; see $work_dir/server.log")
  finish
fi

log "ingesting every fixture note via real REST /add (bge-small)"
add_failures=0
while IFS= read -r f; do
  if ! kg add "$f" --endpoint "$OKTO_NEURON_ENDPOINT" \
       >>"$work_dir/add.out" 2>>"$work_dir/add.err"; then
    add_failures=$((add_failures+1))
    log "add failed: $f"
  fi
done < <(find "$VAULT" -name '*.md' -not -name 'README.md')
assert_exit_code 0 "$add_failures"
# NOTE: this scenario's `cp -R "$FIXTURE/." "$VAULT/"` step above unconditionally
# overwrites okto-neuron.yaml with the fixed synthetic-vault fixture's own copy,
# which hardcodes `storage: backend: ladybug`. That happens regardless of
# `kg_init_backend_args()` above, so this vault is ALWAYS ladybug on disk no
# matter which backend the harness pinned — unlike every other scenario's
# graph.lbug check, this one is not backend-conditional and must not be
# skipped under --backend grafx.
#
# Root-caused empirically (not a marginalia/Grafx content defect): when this
# scenario's earlier `kg init $VAULT $(kg_init_backend_args)` line pins the
# vault to a NON-ladybug backend first (e.g. grafx, under --backend grafx),
# the fixture `cp -R` above still discards that init entirely -- it always
# overwrites okto-neuron.yaml back to `storage: backend: ladybug` -- but the
# real Ladybug graph that then gets written from a fresh "cp overwrote an
# already-initialized-for-a-different-backend vault" history lands at a
# genuinely different, smaller on-disk page/checkpoint footprint than one
# written from a plain `kg init` (86016 vs 126976 bytes, reproduced
# deterministically both ways). Querying the live node count during both
# proved this is disk-layout variance only, not missing data: identical 87
# nodes either way, with every recall/provenance/partner-recall gate below
# scoring 100% regardless. `_lib.sh` itself documents exactly this: "file
# size tracks page allocation and checkpoint timing, counts track content" --
# use its `assert_min_count` against the real node count instead of the
# fragile `assert_min_bytes` proxy.
node_count="$(python3 - "$OKTO_NEURON_ENDPOINT" <<'PY'
import json
import sys
import urllib.request

endpoint = sys.argv[1]
req = urllib.request.Request(
    endpoint.rstrip("/") + "/api/v1/nodes?limit=1",
    headers={},
)
with urllib.request.urlopen(req, timeout=10) as r:
    payload = json.load(r)
print(int(payload.get("total") or 0))
PY
)"
assert_min_count "$node_count" 14 "graph_node_count"

REPORT_JSON="$work_dir/eval_report.json"

log "scoring /api/v1/recall against golden set"
python3 - "$VAULT" "$OKTO_NEURON_ENDPOINT" "$REPORT_JSON" <<'PY'
import json, sys, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))  # noop; PYTHONPATH set
from tests.eval import golden

vault, endpoint, report_path = sys.argv[1], sys.argv[2], sys.argv[3]

def query_fn(q, k):
    req = urllib.request.Request(
        endpoint.rstrip("/") + "/api/v1/recall",
        data=json.dumps({"query": q, "k": k}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.load(r)
    return body.get("hits", [])

report = golden.score(Path(vault), query_fn, k=golden.K)
Path(report_path).write_text(json.dumps(report, indent=2))
print(json.dumps({
    "entity_at_k": report["entity_at_k"],
    "entity_hits": report["entity_hits"],
    "total": report["total_queries"],
    "entity_gate_pass": report["entity_gate_pass"],
    "provenance_gate_pass": report["provenance_gate_pass"],
    "partner_recall_gate_pass": report["partner_recall_gate_pass"],
    "overall_pass": report["overall_pass"],
    "provenance_failures": report["provenance_failures"],
    "partner_recall_failures": report["partner_recall_failures"],
}, indent=2))
sys.exit(0 if report["overall_pass"] else 1)
PY
score_rc=$?
assert_file_exists "$REPORT_JSON"
assert_exit_code 0 "$score_rc"
if [[ "$score_rc" -ne 0 ]]; then
  log "eval gates failed; see $REPORT_JSON"
fi

stop_server
finish
