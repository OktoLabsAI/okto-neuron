#!/usr/bin/env bash
# Scenario 91: retrieval dogfooding gate against an external private corpus.
#
# Private opt-in. Assumes scenario 90 has already built the isolated acceptance
# vault under the same acceptance report directory. An override may name only
# that exact acceptance-local vault; external/private user vaults are rejected.
# This scenario asks caller-supplied questions through `kg query`
# against that vault, captures top-k paths/titles into a JSON report, and
# writes a markdown worksheet for optional later human review. The automated
# scenario does not grade answer quality.
#
# PASS gate (automated portion):
#   - vault must exist and be queryable
#   - at least 80% of questions must return >=1 hit
#   - the machine report and review worksheet must exist
#
# Non-destructive: read-only against the vault.
set -uo pipefail
SCENARIO_NAME="91_private_corpus_qa_dogfooding"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"

EXPECTED_VAULT="$OKTO_NEURON_ACCEPTANCE_DIR/private/corpus"
VAULT_RAW="${OKTO_NEURON_PRIVATE_CORPUS_VAULT:-$EXPECTED_VAULT}"
QUESTIONS_FILE="${OKTO_NEURON_PRIVATE_CORPUS_QUESTIONS:-}"
if ! VAULT=$(python3 - "$VAULT_RAW" "$EXPECTED_VAULT" <<'PY'
from pathlib import Path
import sys

raw = Path(sys.argv[1])
expected = Path(sys.argv[2]).resolve(strict=False)
if not raw.is_absolute() or ".." in raw.parts or raw.is_symlink():
    raise SystemExit("private-corpus QA vault must be an absolute non-symlink path without '..'")
resolved = raw.resolve(strict=False)
if resolved != expected:
    raise SystemExit(f"private-corpus QA vault must be the acceptance-local vault: {expected}")
print(resolved)
PY
); then
  _failures+=("unsafe_or_external_private_corpus_qa_vault path=$VAULT_RAW")
  finish
fi
export OKTO_NEURON_VAULT="$VAULT"
REPORT_JSON="$work_dir/report.json"
REPORT_MD="$work_dir/report.md"

if [[ ! -d "$VAULT" || ! -f "$VAULT/okto-neuron.yaml" || ! -f "$VAULT/graph.lbug" ]]; then
  log "required private-corpus vault not present at $VAULT (run scenario 90 first)"
  _failures+=("vault_missing path=$VAULT")
  finish
fi
log "vault: $VAULT"

source "$SCRIPT_DIR/_preamble.sh"

# Use the shared foreground lifecycle: unique free REST/MCP ports, exported
# bearer token, and teardown limited to the exact PID this scenario owns.
if ! start_server "$VAULT"; then
  _failures+=("server_start_failed")
  finish
fi
ENDPOINT="$OKTO_NEURON_ENDPOINT"
log "server ready REST=$ENDPOINT MCP=$OKTO_NEURON_MCP_ENDPOINT"

if [[ -z "$QUESTIONS_FILE" || ! -f "$QUESTIONS_FILE" ]]; then
  log "required question manifest not found; set OKTO_NEURON_PRIVATE_CORPUS_QUESTIONS"
  _failures+=("private_corpus_questions_missing")
  finish
fi

declare -a QUESTIONS=()
while IFS= read -r question; do
  [[ -z "$question" || "$question" == \#* ]] && continue
  QUESTIONS+=("$question")
done < "$QUESTIONS_FILE"
if [[ "${#QUESTIONS[@]}" -eq 0 ]]; then
  _failures+=("private_corpus_questions_empty")
  finish
fi

K=10
results_dir="$work_dir/results"
mkdir -p "$results_dir"

# Build the JSON report incrementally as a Python list of dicts.
python3 - "$REPORT_JSON" <<'PY'
import json, sys
open(sys.argv[1], "w").write("[]")
PY

hit_count=0
total=${#QUESTIONS[@]}

# Start the optional review worksheet.
{
  echo "# 91 Private-Corpus Q&A Dogfooding — Retrieval Review Worksheet"
  echo
  echo "**Vault**: \`$VAULT\`"
  echo "**Generated**: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "**Total questions**: $total  |  **top-k**: $K"
  echo
  echo "Optional reviewer: inspect each question's retrieval quality and record"
  echo "a short assessment. This worksheet is ungraded by the scenario and is"
  echo "not answer-quality evidence."
  echo
} > "$REPORT_MD"

for i in "${!QUESTIONS[@]}"; do
  q="${QUESTIONS[$i]}"
  idx=$((i+1))
  safe=$(printf '%02d' "$idx")
  out="$results_dir/q${safe}.json"
  err="$results_dir/q${safe}.err"
  t0=$(date +%s)
  kg query "$q" --endpoint "$ENDPOINT" --format json --k "$K" >"$out" 2>"$err" || true
  dt=$(( $(date +%s) - t0 ))

  hits=$(python3 -c "
import json,sys
try:
    d=json.load(open(sys.argv[1]))
    print(len(d) if isinstance(d, list) else 0)
except Exception:
    print(0)
" "$out" 2>/dev/null || echo 0)

  if [[ "$hits" -ge 1 ]]; then
    hit_count=$((hit_count+1))
  fi
  log "Q${safe} hits=$hits dt=${dt}s :: $q"

  # Append structured row to JSON report.
  python3 - "$REPORT_JSON" "$out" "$q" "$idx" <<'PY'
import json, sys
report_path, result_path, question, idx = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
try:
    data = json.load(open(result_path))
    if not isinstance(data, list):
        data = []
except Exception:
    data = []
paths, titles = [], []
for h in data:
    if not isinstance(h, dict):
        continue
    paths.append(h.get("path") or "")
    titles.append(h.get("title") or h.get("name") or "")
report = json.load(open(report_path))
report.append({
    "index": idx,
    "question": question,
    "top_k_paths": paths,
    "top_k_titles": titles,
    "hit_count": len(data),
})
json.dump(report, open(report_path, "w"), indent=2)
PY

  # Append markdown section.
  {
    echo "## Q${safe}. $q"
    echo
    echo "- hits: $hits"
    if [[ "$hits" -ge 1 ]]; then
      echo "- top results:"
      python3 -c "
import json,sys
try:
    d=json.load(open(sys.argv[1]))
except Exception:
    d=[]
for i,h in enumerate(d[:5],1):
    if not isinstance(h,dict): continue
    title=h.get('title') or h.get('name') or ''
    path=h.get('path') or ''
    print(f'  {i}. {title} — \`{path}\`')
" "$out"
    else
      echo "- (no results)"
      if [[ -s "$err" ]]; then
        echo "- stderr:"
        echo '  ```'
        sed 's/^/  /' "$err" | head -20
        echo '  ```'
      fi
    fi
    echo
    echo "**Optional reviewer assessment**: [ ] PASS  [ ] FAIL"
    echo "**Notes**:"
    echo
  } >> "$REPORT_MD"
done

log "automated tally: $hit_count/$total questions returned >=1 hit"
log "machine report:  $REPORT_JSON"
log "review worksheet: $work_dir/report.md"

# Gate: at least 80% must have hits.
min_hits=$(( (total * 4 + 4) / 5 ))
if [[ "$hit_count" -lt "$min_hits" ]]; then
  _failures+=("insufficient_hit_rate hits=$hit_count total=$total threshold=$min_hits")
fi

# Gate: both retrieval report artifacts must exist.
assert_file_exists "$work_dir/report.md"
assert_min_bytes "$work_dir/report.md" 500
assert_file_exists "$REPORT_JSON"

stop_server
finish
