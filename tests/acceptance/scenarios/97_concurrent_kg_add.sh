#!/usr/bin/env bash
# Scenario 97: ONE running marginalia server + N=16 parallel `kg add` CLI
# processes hammering it concurrently with distinct markdown files.
#
# This is the production-shape concurrency contract for the spec
# 9030718e (marginalia v0.1 server): the in-process asyncio.Lock on
# ServerState.writer_lock must serialise vault writes so that all 16
# clients return 200 OK, no VaultLockedError surfaces (the lock lives
# in-process, not at the ladybug file level any more), and the final
# node count equals initial + 16 with zero graph corruption.
#
# Real bge-small embeddings — no mocks, no skips.
set -uo pipefail
SCENARIO_NAME="97_concurrent_kg_add"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

N=16
VAULT="$work_dir/vault"

log "kg init on fresh vault"
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.stdout" 2>"$work_dir/init.stderr"
assert_exit_code 0 $?

# Author N distinct markdown files BEFORE starting the server. Each note
# carries a unique sentinel so we can prove every write landed.
NOTES_DIR="$work_dir/inbox"
mkdir -p "$NOTES_DIR"
for i in $(seq 1 "$N"); do
  cat > "$NOTES_DIR/n_${i}.md" <<EOF
# Concurrent Note ${i}

Sentinel: concurrent_kg_add_sentinel_${i}_zzz.
This note has enough natural-language content to drive a real bge-small
embedding pass: marginalia, knowledge graph, provenance, byte-span hits,
ladybug store, asyncio writer lock, scenario 97 concurrency probe.
EOF
done

# Use the shared owned lifecycle. It chooses distinct non-advertised ports,
# confirms child liveness, and requires authenticated PID + canonical-vault
# identity before this scenario can send any writes.
if ! start_server "$VAULT"; then
  _failures+=("server_start_failed")
  finish
fi

# The running server holds the ladybug write handle, so we cannot open a
# second Vault for the initial-count snapshot without contending on the
# lock. Instead we baseline after server shutdown along with the final
# count, and assert the post-condition: every one of the N sentinel
# titles must appear in the final node listing AND the headings-per-note
# count == N (one "Concurrent Note <i>" heading per ingested doc).

# Fan out N parallel `kg add` processes against the single server.
log "launching ${N} parallel kg add clients"
pids=()
for i in $(seq 1 "$N"); do
  ( kg add "$NOTES_DIR/n_${i}.md" \
      --endpoint "$OKTO_NEURON_ENDPOINT" \
      >"$work_dir/add_${i}.out" 2>"$work_dir/add_${i}.err" ) &
  pids+=("$!")
done

# Reap; collect per-client exit codes.
declare -a rcs
ok=0
for idx in "${!pids[@]}"; do
  wait "${pids[$idx]}"
  rc=$?
  rcs[idx]=$rc
  if [[ "$rc" -eq 0 ]]; then ok=$((ok+1)); fi
done
log "client exit codes: ${rcs[*]}"
log "successful clients: $ok / $N"

# Aggregate stderr for VaultLockedError / 5xx surfaces.
cat "$work_dir"/add_*.err >"$work_dir/all_add.err"
cat "$work_dir"/add_*.out >"$work_dir/all_add.out"

# Per-client assertion: every one of the N adds returned 0.
for idx in "${!rcs[@]}"; do
  assert_exit_code 0 "${rcs[$idx]}"
done

# No VaultLockedError must surface from any client.
assert_not_contains "$work_dir/all_add.err" "VaultLockedError"
# No 5xx responses — the thin client surfaces those as "server error 5\\d\\d".
assert_not_contains "$work_dir/all_add.err" "server error 5[0-9][0-9]"

# Drain the exact owned server cleanly so we can re-open the vault.
stop_server

# Final node count probe — open the vault fresh and list nodes.
# Expectation: initial + N. If less, some writes were lost (lock not
# serialising) or graph corruption truncated the table.
python3 - "$VAULT" >"$work_dir/final_count.out" 2>"$work_dir/final_count.err" <<'PY'
import sys
from pathlib import Path
from okto_neuron.vault import Vault

v = Vault.open(Path(sys.argv[1]))
try:
    nodes = list(v.store.list_nodes())
    titles = sorted(n.title or "" for n in nodes)
finally:
    v.close()
print(f"final_node_count={len(nodes)}")
for t in titles:
    print(f"title={t}")
PY
final_rc=$?
cat "$work_dir/final_count.out" >&2
assert_exit_code 0 "$final_rc"

final=$(grep -oE 'final_node_count=[0-9]+' "$work_dir/final_count.out" | head -1 | cut -d= -f2)
final=${final:-0}
log "final node count: $final  (expected ≥ $N)"

# Concurrency contract — every one of the N sentinel headings must
# appear EXACTLY once in the final node listing. Any miss = lost write
# (lock not serialising). Any duplicate = double-write or graph corruption.
missing=0
duplicate=0
for i in $(seq 1 "$N"); do
  c=$(grep -c "^title=heading: Concurrent Note ${i}$" "$work_dir/final_count.out" || true)
  c=${c:-0}
  if [[ "$c" -lt 1 ]]; then missing=$((missing+1)); fi
  if [[ "$c" -gt 1 ]]; then duplicate=$((duplicate+1)); fi
done
if [[ "$missing" -gt 0 ]]; then
  _failures+=("missing_sentinel_titles count=$missing of $N")
fi
if [[ "$duplicate" -gt 0 ]]; then
  _failures+=("duplicate_sentinel_titles count=$duplicate of $N")
fi
log "sentinel titles: $((N - missing)) / $N present, $duplicate duplicates"

# Per-document filename anchors n_1..n_N must each appear exactly once
# (these are top-level document nodes ingested by Vault.add).
missing_docs=0
for i in $(seq 1 "$N"); do
  c=$(grep -c "^title=n_${i}$" "$work_dir/final_count.out" || true)
  c=${c:-0}
  if [[ "$c" -ne 1 ]]; then missing_docs=$((missing_docs+1)); fi
done
if [[ "$missing_docs" -gt 0 ]]; then
  _failures+=("missing_or_dup_doc_nodes count=$missing_docs of $N")
fi

finish
