#!/usr/bin/env bash
# Scenario 62: three-state probe for authority_alias_collision detector.
# Detector contract: two type=authority docs naming the SAME agent CURIE, one
# with alias_of (alias record), one without (primary). That's the collision.
# A: only primary exists → 0. B: add alias for same agent → ≥1. C: remove alias.
set -uo pipefail
SCENARIO_NAME="62_induce_alias_collision"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

VAULT="$work_dir/vault"
kg init "$VAULT" $(kg_init_backend_args) >/dev/null 2>&1
assert_exit_code 0 $?

start_server "$VAULT"

# A: primary authority record only.
cat > "$VAULT/notes/agent_bob.md" <<'EOF'
---
type: authority
created_at: 2026-04-01T12:00:00Z
agent: agent:bob-engineer
---

# Authority: agent:bob-engineer

Primary canonical record for agent:bob-engineer.
EOF
kg add "$VAULT/notes/agent_bob.md" --endpoint "$OKTO_NEURON_ENDPOINT" >/dev/null 2>&1

kg detect-drift --vault "$VAULT" --endpoint "$OKTO_NEURON_ENDPOINT" \
  --mode on-query --algos authority_alias_collision --json \
  >"$work_dir/al_a.json" 2>"$work_dir/al_a.err"
count_a=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(sum(1 for a in d.get('actions',[]) if a.get('path')=='authority_alias_collision'))" "$work_dir/al_a.json")
log "state A (primary only): count=${count_a}"
if [[ "$count_a" -ne 0 ]]; then
  _failures+=("baseline_not_clean state_a=$count_a")
fi

# B: introduce an alias record for the SAME agent — the collision.
cat > "$VAULT/notes/agent_bob_alias.md" <<'EOF'
---
type: authority
created_at: 2026-04-15T12:00:00Z
agent: agent:bob-engineer
alias_of: agent:robert-engineer
---

# Authority: agent:bob-engineer (alias)

This record declares agent:bob-engineer as an alias of agent:robert-engineer — but a primary record for agent:bob-engineer already exists in the vault, which is the collision condition.
EOF
kg add "$VAULT/notes/agent_bob_alias.md" --endpoint "$OKTO_NEURON_ENDPOINT" >/dev/null 2>&1

kg detect-drift --vault "$VAULT" --endpoint "$OKTO_NEURON_ENDPOINT" \
  --mode on-query --algos authority_alias_collision --json \
  >"$work_dir/al_b.json" 2>"$work_dir/al_b.err"
count_b=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(sum(1 for a in d.get('actions',[]) if a.get('path')=='authority_alias_collision'))" "$work_dir/al_b.json")
log "state B (alias collides with primary): count=${count_b}"
if [[ "$count_b" -lt 1 ]]; then
  _failures+=("alias_collision_not_detected count_b=$count_b")
fi

# C: fix the alias to point to a different agent CURIE — collision clears.
cat > "$VAULT/notes/agent_bob_alias.md" <<'EOF'
---
type: authority
created_at: 2026-04-15T12:00:00Z
agent: agent:bobby-engineer
alias_of: agent:robert-engineer
---

# Authority: agent:bobby-engineer (alias)

Corrected alias for a different agent CURIE that does not collide with any primary record.
EOF
kg add "$VAULT/notes/agent_bob_alias.md" --endpoint "$OKTO_NEURON_ENDPOINT" >/dev/null 2>&1

kg detect-drift --vault "$VAULT" --endpoint "$OKTO_NEURON_ENDPOINT" \
  --mode on-query --algos authority_alias_collision --json \
  >"$work_dir/al_c.json" 2>"$work_dir/al_c.err"
count_c=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(sum(1 for a in d.get('actions',[]) if a.get('path')=='authority_alias_collision'))" "$work_dir/al_c.json")
log "state C (alias corrected): count=${count_c}"
if [[ "$count_c" -gt "$count_b" ]]; then
  _failures+=("correction_increased_findings c=$count_c b=$count_b")
fi

if [[ ${#_failures[@]} -gt 0 ]]; then
  finish "alias-collision-detection-broken"
else
  finish
fi
