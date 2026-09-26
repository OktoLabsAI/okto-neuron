#!/usr/bin/env bash
# Scenario 94: `kg snapshot dump` / `verify` / `load` round trip — a graph
# populated in vault A must reappear byte-for-byte (per query hit, modulo the
# vault-relative path) in a fresh vault B rebuilt only from the dumped
# snapshot directory.
set -uo pipefail
SCENARIO_NAME="94_snapshot_roundtrip"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

VAULT_A="$work_dir/vaultA"
VAULT_B="$work_dir/vaultB"
DEST="$work_dir/snapshot"
QUERY_TEXT="Okto Neuron snapshot roundtrip fixture"

kg init "$VAULT_A" $(kg_init_backend_args) >"$work_dir/init.stdout" 2>"$work_dir/init.stderr"
assert_exit_code 0 $?

if ! start_server "$VAULT_A"; then
  _failures+=("server_start_failed vault=A")
  finish
fi

cat > "$VAULT_A/notes/alpha.md" <<'EOF'
# Snapshot Roundtrip Alpha
Okto Neuron snapshot roundtrip fixture note Alpha describes the aurora
borealis over glacier fields.
EOF
cat > "$VAULT_A/notes/beta.md" <<'EOF'
# Snapshot Roundtrip Beta
Okto Neuron snapshot roundtrip fixture note Beta describes bioluminescent
plankton in tidal pools.
EOF

log "kg add alpha.md"
kg add "$VAULT_A/notes/alpha.md" --endpoint "$OKTO_NEURON_ENDPOINT" \
  >"$work_dir/add_alpha.stdout" 2>"$work_dir/add_alpha.stderr"
assert_exit_code 0 $?

log "kg add beta.md"
kg add "$VAULT_A/notes/beta.md" --endpoint "$OKTO_NEURON_ENDPOINT" \
  >"$work_dir/add_beta.stdout" 2>"$work_dir/add_beta.stderr"
assert_exit_code 0 $?

log "kg query on vault A (pre-dump baseline)"
kg query "$QUERY_TEXT" --endpoint "$OKTO_NEURON_ENDPOINT" --format json \
  >"$work_dir/qa.json" 2>"$work_dir/qa.stderr"
assert_exit_code 0 $?

stop_server

log "kg snapshot dump A -> $DEST"
kg snapshot dump "$VAULT_A" "$DEST" \
  >"$work_dir/dump.stdout" 2>"$work_dir/dump.stderr"
dump_rc=$?
assert_exit_code 0 "$dump_rc"
if [[ "$dump_rc" -ne 0 ]]; then tail -40 "$work_dir/dump.stderr" >&2; fi
assert_file_exists "$DEST/manifest.json"
assert_file_exists "$DEST/schema.json"
assert_file_exists "$DEST/nodes.jsonl"
assert_file_exists "$DEST/edges.jsonl"
assert_file_exists "$DEST/CHECKSUMS.sha256"

log "kg snapshot verify $DEST"
kg snapshot verify "$DEST" \
  >"$work_dir/verify.stdout" 2>"$work_dir/verify.stderr"
verify_rc=$?
assert_exit_code 0 "$verify_rc"
if [[ "$verify_rc" -ne 0 ]]; then tail -40 "$work_dir/verify.stderr" >&2; fi

log "kg snapshot load $DEST -> B"
kg snapshot load "$DEST" "$VAULT_B" $(kg_snapshot_load_backend_args) \
  >"$work_dir/load.stdout" 2>"$work_dir/load.stderr"
load_rc=$?
assert_exit_code 0 "$load_rc"
if [[ "$load_rc" -ne 0 ]]; then tail -40 "$work_dir/load.stderr" >&2; fi
assert_file_exists "$VAULT_B/okto-neuron.yaml"

if ! start_server "$VAULT_B"; then
  _failures+=("server_start_failed vault=B")
  finish
fi

log "kg query on vault B (post-load, same text)"
kg query "$QUERY_TEXT" --endpoint "$OKTO_NEURON_ENDPOINT" --format json \
  >"$work_dir/qb.json" 2>"$work_dir/qb.stderr"
assert_exit_code 0 $?

stop_server

log "comparing qa.json and qb.json hit lists (path field dropped)"
python3 - "$work_dir/qa.json" "$work_dir/qb.json" \
  >"$work_dir/compare.stdout" 2>"$work_dir/compare.stderr" <<'PY'
import json
import sys

path_a, path_b = sys.argv[1], sys.argv[2]


def strip_path(hits):
    return [{k: v for k, v in hit.items() if k != "path"} for hit in hits]


with open(path_a, encoding="utf-8") as handle:
    hits_a = json.load(handle)
with open(path_b, encoding="utf-8") as handle:
    hits_b = json.load(handle)

stripped_a = strip_path(hits_a)
stripped_b = strip_path(hits_b)

if not stripped_a:
    print("qa.json produced zero hits", file=sys.stderr)
    sys.exit(1)

if stripped_a != stripped_b:
    print("hit lists differ after dropping path:", file=sys.stderr)
    print(f"A={stripped_a}", file=sys.stderr)
    print(f"B={stripped_b}", file=sys.stderr)
    sys.exit(1)

print(f"hit lists match: {len(stripped_a)} hit(s)")
PY
compare_rc=$?
assert_exit_code 0 "$compare_rc"
if [[ "$compare_rc" -ne 0 ]]; then cat "$work_dir/compare.stderr" >&2; fi

finish
