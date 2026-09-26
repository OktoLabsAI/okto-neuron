#!/usr/bin/env bash
# Scenario 61: three-state probe for supersedence_stale_head detector.
# A: lone dec-001 → 0 findings.
# B: add dec-002 (head:false, supersedes dec-001) AND dec-003 (supersedes
#    dec-002, head:true) — dec-002 is now a stale head.
# C: archive dec-002 by promoting its head:true OR removing the chain.
set -uo pipefail
SCENARIO_NAME="61_induce_supersedence_drift"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

VAULT="$work_dir/vault"
kg init "$VAULT" $(kg_init_backend_args) >/dev/null 2>&1
assert_exit_code 0 $?

start_server "$VAULT"

# A: only dec-001 exists.
cat > "$VAULT/notes/dec_001.md" <<'EOF'
---
type: decision
created_at: 2025-01-15T10:00:00Z
agent: agent:platform-team
decision_id: dec-001
head: true
---

# Decision dec-001 — Base image

We adopt python:3.12-slim-bookworm as the base image.
EOF
kg add "$VAULT/notes/dec_001.md" --endpoint "$OKTO_NEURON_ENDPOINT" >/dev/null 2>&1

kg detect-drift --vault "$VAULT" --endpoint "$OKTO_NEURON_ENDPOINT" \
  --mode on-query --algos supersedence_stale_head --json \
  >"$work_dir/sup_a.json" 2>"$work_dir/sup_a.err"
count_a=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(sum(1 for a in d.get('actions',[]) if a.get('path')=='supersedence_stale_head'))" "$work_dir/sup_a.json")
log "state A (lone decision): count=${count_a}"
if [[ "$count_a" -ne 0 ]]; then
  _failures+=("baseline_not_clean state_a=$count_a")
fi

# B: dec-002 is a stale middle entry. dec-003 supersedes dec-002.
cat > "$VAULT/notes/dec_002.md" <<'EOF'
---
type: decision
created_at: 2025-03-01T10:00:00Z
agent: agent:platform-team
decision_id: dec-002
supersedes: dec-001
head: false
---

# Decision dec-002 — Switch to chainguard

This middle decision proposed cgr.dev/chainguard/python:3.12. It supersedes dec-001 but is itself superseded by dec-003.
EOF
cat > "$VAULT/notes/dec_003.md" <<'EOF'
---
type: decision
created_at: 2025-04-20T10:00:00Z
agent: agent:platform-team
decision_id: dec-003
supersedes: dec-002
head: true
---

# Decision dec-003 — Pinned chainguard digest

Current decision: use the pinned chainguard digest for ARM64 builds.
EOF
kg add "$VAULT/notes/dec_002.md" --endpoint "$OKTO_NEURON_ENDPOINT" >/dev/null 2>&1
kg add "$VAULT/notes/dec_003.md" --endpoint "$OKTO_NEURON_ENDPOINT" >/dev/null 2>&1

kg detect-drift --vault "$VAULT" --endpoint "$OKTO_NEURON_ENDPOINT" \
  --mode on-query --algos supersedence_stale_head --json \
  >"$work_dir/sup_b.json" 2>"$work_dir/sup_b.err"
count_b=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(sum(1 for a in d.get('actions',[]) if a.get('path')=='supersedence_stale_head'))" "$work_dir/sup_b.json")
log "state B (dec-002 stale head): count=${count_b}"
if [[ "$count_b" -lt 1 ]]; then
  _failures+=("supersedence_not_detected count_b=$count_b")
fi

# C: promote dec-002 to archived (remove it from the chain by deleting head:false marker
# — flip head to true OR mark as archived; detector requires head:false to fire).
cat > "$VAULT/notes/dec_002.md" <<'EOF'
---
type: decision
created_at: 2025-03-01T10:00:00Z
agent: agent:platform-team
decision_id: dec-002
supersedes: dec-001
head: true
archived_at: 2025-04-20T10:00:00Z
---

# Decision dec-002 — Switch to chainguard [HISTORICAL]

This decision is preserved for history; head flag flipped to true to remove it from the stale-head population.
EOF
kg add "$VAULT/notes/dec_002.md" --endpoint "$OKTO_NEURON_ENDPOINT" >/dev/null 2>&1

kg detect-drift --vault "$VAULT" --endpoint "$OKTO_NEURON_ENDPOINT" \
  --mode on-query --algos supersedence_stale_head --json \
  >"$work_dir/sup_c.json" 2>"$work_dir/sup_c.err"
count_c=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(sum(1 for a in d.get('actions',[]) if a.get('path')=='supersedence_stale_head'))" "$work_dir/sup_c.json")
log "state C (head flag flipped): count=${count_c}"
if [[ "$count_c" -gt "$count_b" ]]; then
  _failures+=("remediation_increased_findings c=$count_c b=$count_b")
fi

if [[ ${#_failures[@]} -gt 0 ]]; then
  finish "supersedence-drift-detection-broken"
else
  finish
fi
