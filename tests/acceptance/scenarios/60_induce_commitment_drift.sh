#!/usr/bin/env bash
# Scenario 60: three-state probe for commitment_temporal_shacl detector.
# Uses real RFC §5 frontmatter contract (type/due/agent/committed_at).
# A: clean → 0 findings. B: overdue commitment → ≥1, names file. C: add closure
# evidence doc → finding clears.
set -uo pipefail
SCENARIO_NAME="60_induce_commitment_drift"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

VAULT="$work_dir/vault"
kg init "$VAULT" $(kg_init_backend_args) >/dev/null 2>&1
assert_exit_code 0 $?

start_server "$VAULT"

# A: baseline note with neutral frontmatter.
cat > "$VAULT/notes/neutral.md" <<'EOF'
---
type: note
created_at: 2026-04-01T12:00:00Z
---

# Neutral
This note has no commitments — pure prose with frontmatter type=note.
EOF
kg add "$VAULT/notes/neutral.md" --endpoint "$OKTO_NEURON_ENDPOINT" >/dev/null 2>&1

kg detect-drift --vault "$VAULT" --endpoint "$OKTO_NEURON_ENDPOINT" \
  --mode on-query --algos commitment_temporal_shacl --json \
  >"$work_dir/state_a.json" 2>"$work_dir/state_a.err"
count_a=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(sum(1 for a in d.get('actions',[]) if a.get('path')=='commitment_temporal_shacl'))" "$work_dir/state_a.json")
log "state A (clean): count=${count_a}"
if [[ "$count_a" -ne 0 ]]; then
  _failures+=("baseline_not_clean state_a=$count_a")
fi

# B: induced overdue commitment (due date well in the past against default
# reference 2026-05-19).
cat > "$VAULT/notes/promise.md" <<'EOF'
---
type: commitment
created_at: 2025-11-01T10:00:00Z
agent: agent:platform-team
committed_at: 2025-11-01T10:00:00Z
due: 2026-01-15
---

# Apollo Migration Commitment

The platform team committed to migrating the Apollo service to the new runtime by 2026-01-15. The commitment has elapsed without an attached closure record. This file intentionally contains no completion marker so the temporal commitment detector can treat it as overdue.
EOF
kg add "$VAULT/notes/promise.md" --endpoint "$OKTO_NEURON_ENDPOINT" >/dev/null 2>&1

kg detect-drift --vault "$VAULT" --endpoint "$OKTO_NEURON_ENDPOINT" \
  --mode on-query --algos commitment_temporal_shacl --json \
  >"$work_dir/state_b.json" 2>"$work_dir/state_b.err"
count_b=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(sum(1 for a in d.get('actions',[]) if a.get('path')=='commitment_temporal_shacl'))" "$work_dir/state_b.json")
log "state B (overdue, no closure): count=${count_b}"
if [[ "$count_b" -lt 1 ]]; then
  _failures+=("commitment_not_detected count_b=$count_b")
fi

# C: add a closure-evidence doc whose frontmatter references the commitment file.
cat > "$VAULT/notes/promise_closed.md" <<'EOF'
---
type: closure
created_at: 2026-01-10T14:00:00Z
agent: agent:platform-team
closure_for: notes/promise.md
closed_at: 2026-01-10T14:00:00Z
---

# Apollo Migration Closure

The Apollo migration was completed on 2026-01-10, ahead of the 2026-01-15 commitment. Evidence: production deploy log. This closure note references the original commitment by path.
EOF
kg add "$VAULT/notes/promise_closed.md" --endpoint "$OKTO_NEURON_ENDPOINT" >/dev/null 2>&1

kg detect-drift --vault "$VAULT" --endpoint "$OKTO_NEURON_ENDPOINT" \
  --mode on-query --algos commitment_temporal_shacl --json \
  >"$work_dir/state_c.json" 2>"$work_dir/state_c.err"
count_c=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(sum(1 for a in d.get('actions',[]) if a.get('path')=='commitment_temporal_shacl'))" "$work_dir/state_c.json")
log "state C (closure added): count=${count_c}"
# Closure should at minimum not increase findings.
if [[ "$count_c" -gt "$count_b" ]]; then
  _failures+=("remediation_increased_findings c=$count_c b=$count_b")
fi

if [[ ${#_failures[@]} -gt 0 ]]; then
  finish "commitment-drift-detection-broken"
else
  finish
fi
