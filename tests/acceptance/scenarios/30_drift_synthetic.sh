#!/usr/bin/env bash
# Scenario 30: real RFC §5 drift detection on the full synthetic vault.
# HARD RULE: no mocks, no skips, no reduced corpora. Full fixture set ingested,
# real bge-small embeddings, real Ladybug writes, real detector run.
set -uo pipefail
SCENARIO_NAME="30_drift_synthetic"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

FIXTURE="$REPO_ROOT/tests/fixtures/synthetic-vault"
assert_file_exists "$FIXTURE/drift_alias.md"
assert_file_exists "$FIXTURE/drift_commitment.md"
assert_file_exists "$FIXTURE/drift_supersedence.md"

VAULT="$work_dir/vault"
log "kg init $VAULT"
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.stdout" 2>"$work_dir/init.stderr"
assert_exit_code 0 $?

if ! start_server "$VAULT"; then
  _failures+=("start_server failed; see $work_dir/server.log")
  finish
fi

log "copying full synthetic vault corpus into notes/"
# Real corpus: every md file under the fixture, preserving relative layout.
mkdir -p "$VAULT/notes"
(cd "$FIXTURE" && find . -type f -name '*.md' -print0) | while IFS= read -r -d '' f; do
  rel="${f#./}"
  mkdir -p "$VAULT/notes/$(dirname "$rel")"
  cp "$FIXTURE/$rel" "$VAULT/notes/$rel"
done

md_count=$(find "$VAULT/notes" -type f -name '*.md' | wc -l | tr -d ' ')
log "ingesting $md_count markdown files"
if [[ "$md_count" -lt 10 ]]; then
  _failures+=("corpus_too_small count=$md_count")
fi

log "kg add per-file (real bge-small embedding over full corpus)"
# NOTE: kg add does not currently recurse directories — tracked as Pulse card.
# Loop is the semantically-equivalent workaround (graph state identical).
t0=$(date +%s)
add_fail=0
: > "$work_dir/add.stdout"; : > "$work_dir/add.stderr"
while IFS= read -r -d '' f; do
  if ! kg add "$f" --endpoint "$OKTO_NEURON_ENDPOINT" \
      >>"$work_dir/add.stdout" 2>>"$work_dir/add.stderr"; then
    add_fail=$((add_fail+1))
    log "FAIL add: $f"
  fi
done < <(find "$VAULT/notes" -type f -name '*.md' -print0)
add_dt=$(( $(date +%s) - t0 ))
assert_exit_code 0 "$add_fail"
if [[ "$add_fail" -ne 0 ]]; then tail -40 "$work_dir/add.stderr" >&2; fi
log "ingest took ${add_dt}s for $md_count files"

# Assert the graph got POPULATED, by content rather than by file size. The old
# `assert_min_bytes graph.lbug 200000` proxy measured page-allocated bytes at the
# instant ingest returned, so ADR 0039's deferred-checkpoint work made it read a
# pre-checkpoint 126,976-byte file (exactly 31 x 4096) even though the graph held
# every node — the same vault measures ~1.5 MB once the checkpoint lands. Counts
# are what the scenario actually cares about and cannot drift with storage layout.
log "asserting graph population via /api/v1/graph/stats"
kg_stats="$work_dir/graph_stats.json"
curl -fsS "$OKTO_NEURON_ENDPOINT/api/v1/graph/stats" \
  -H "X-Okto-Neuron-Vault: $VAULT" >"$kg_stats" 2>"$work_dir/graph_stats.stderr"
assert_exit_code 0 "$?"
graph_nodes=$(python3 -c "import json,sys;print(json.load(open('$kg_stats'))['total_nodes'])")
graph_docs=$(python3 -c "
import json
counts={e['type']: e['count'] for e in json.load(open('$kg_stats'))['node_types']}
print(counts.get('Document', 0))
")
log "graph populated: total_nodes=$graph_nodes documents=$graph_docs (corpus=$md_count files)"
assert_min_count "$graph_nodes" 40 "graph_total_nodes"
assert_min_count "$graph_docs" "$md_count" "graph_documents"

log "kg detect-drift --mode=on-query --json (median-of-3 timing)"
times=()
for i in 1 2 3; do
  t0=$(date +%s)
  kg detect-drift --vault "$VAULT" --endpoint "$OKTO_NEURON_ENDPOINT" \
    --mode on-query --json \
    >"$work_dir/drift_${i}.json" 2>"$work_dir/drift_${i}.stderr"
  drift_rc=$?
  dt=$(( $(date +%s) - t0 ))
  times+=("$dt")
  log "run $i: rc=$drift_rc dt=${dt}s"
  if [[ "$i" == "1" ]]; then assert_exit_code 0 "$drift_rc"; fi
done

# Median-of-3 against 30s synthetic budget.
sorted=$(printf '%s\n' "${times[@]}" | sort -n)
median=$(echo "$sorted" | sed -n '2p')
log "median wall-clock: ${median}s (budget: 30s synthetic)"
if [[ "$median" -gt 30 ]]; then
  _failures+=("budget_exceeded median=${median}s budget=30s")
fi

# Real findings expected. Use python to count and inspect.
python3 - "$work_dir/drift_1.json" >"$work_dir/drift_summary.txt" 2>"$work_dir/drift_summary.err" <<'PY'
import json, sys
p = sys.argv[1]
data = json.load(open(p))
findings = data.get("findings") or data.get("Findings") or data.get("actions") or []
if not findings and isinstance(data, list):
    findings = data
print(f"findings_count={len(findings)}")
joined = json.dumps(findings).lower()
for tag in ("alias", "commitment", "supersedence"):
    print(f"mentions_{tag}={tag in joined}")
print("--- raw head ---")
print(json.dumps(data, indent=2)[:2000])
PY
cat "$work_dir/drift_summary.txt" >&2

assert_contains "$work_dir/drift_summary.txt" "findings_count=[1-9]"
# At least one of the three fixture themes must appear in the findings.
if ! grep -E "mentions_(alias|commitment|supersedence)=True" "$work_dir/drift_summary.txt" >/dev/null; then
  _failures+=("no_fixture_theme_in_findings")
  log "FAIL no drift fixture (alias/commitment/supersedence) named in findings"
fi

stop_server

if [[ ${#_failures[@]} -gt 0 ]]; then
  finish
else
  finish
fi
