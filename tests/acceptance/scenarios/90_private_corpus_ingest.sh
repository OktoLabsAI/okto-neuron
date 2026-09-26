#!/usr/bin/env bash
# Scenario 90 (private opt-in): build an isolated acceptance vault from an
# external private corpus
# under client/server mode. One `kg serve` process; per-file ingest happens
# over HTTP POST /add via the thin `kg add` client; caller-supplied semantic
# probes run over HTTP POST /query; the server stops at the end.
#
# Acceptance:
#   * Server logs MUST contain zero "Corrupted wal file" lines (the whole
#     point of pivoting to single-writer client/server).
#   * ≤5% legit `kg add` failures.
#   * All caller-supplied probes return non-empty hits and match their expected
#     file pattern.
#
# Isolation: the default vault lives under the acceptance report directory and
# is rebuilt from scratch. A caller-supplied OKTO_NEURON_PRIVATE_CORPUS_VAULT is never
# reset when it already exists unless OKTO_NEURON_PRIVATE_CORPUS_ALLOW_RESET=1 is also
# explicit. This scenario is excluded from the default harness run.
# HARD RULE: real bge-small embeddings; no mocks; no stub embedder.
set -uo pipefail
SCENARIO_NAME="90_private_corpus_ingest"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"

CORPUS_RAW="${OKTO_NEURON_PRIVATE_CORPUS:-${OKTO_NEURON_ACCEPTANCE_CALLER_HOME:-}/.marginalia-private-corpus}"
DEFAULT_VAULT="$(dirname "$work_dir")/private/corpus"
VAULT_RAW="${OKTO_NEURON_PRIVATE_CORPUS_VAULT:-$DEFAULT_VAULT}"
PROBES_FILE="${OKTO_NEURON_PRIVATE_CORPUS_PROBES:-}"

# Resolve aliases before any reset. A custom target must be absolute and may
# not overlap HOME, the repository, corpus, report root, or scenario workdir.
if ! private_corpus_paths=$(python3 - "$CORPUS_RAW" "$VAULT_RAW" "$REPO_ROOT" \
  "${OKTO_NEURON_ACCEPTANCE_CALLER_HOME:-}" "$OKTO_NEURON_ACCEPTANCE_DIR" "$work_dir" <<'PY'
from pathlib import Path
import sys


def fail(message: str) -> None:
    print(f"unsafe private-corpus acceptance path: {message}", file=sys.stderr)
    raise SystemExit(1)


def resolve(raw: str, label: str, *, reject_symlink: bool = False) -> Path:
    if not raw or any(ord(char) < 32 for char in raw):
        fail(f"{label} is empty or contains control characters")
    path = Path(raw)
    if not path.is_absolute():
        fail(f"{label} must be absolute: {raw}")
    if ".." in path.parts:
        fail(f"{label} must not contain '..': {raw}")
    if reject_symlink and path.is_symlink():
        fail(f"{label} must not be a symlink: {raw}")
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        fail(f"cannot resolve {label} {raw!r}: {exc}")


def ancestor_or_same(candidate: Path, protected: Path) -> bool:
    return candidate == protected or candidate in protected.parents


def related(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


corpus_raw, vault_raw, repo_raw, home_raw, root_raw, work_raw = sys.argv[1:]
corpus = resolve(corpus_raw, "private corpus")
vault = resolve(vault_raw, "vault reset target", reject_symlink=True)
repo = resolve(repo_raw, "repository")
home = resolve(home_raw, "home") if home_raw else None
root = resolve(root_raw, "acceptance report root")
work = resolve(work_raw, "scenario work directory")

if vault == Path(vault.anchor):
    fail(f"vault reset target resolves to filesystem root: {vault}")
if home is not None and ancestor_or_same(vault, home):
    fail(f"vault reset target is HOME or its ancestor: {vault}")
if related(vault, repo):
    fail(f"vault reset target overlaps the repository: {vault}")
if related(vault, corpus):
    fail(f"vault reset target overlaps the private corpus: {vault}")
if ancestor_or_same(vault, root):
    fail(f"vault reset target is the report root or its ancestor: {vault}")
if related(vault, work):
    fail(f"vault reset target overlaps the scenario work directory: {vault}")

print(f"{corpus}\t{vault}")
PY
); then
  _failures+=("unsafe_private_corpus_path")
  finish
fi
IFS=$'\t' read -r CORPUS VAULT <<<"$private_corpus_paths"
unset private_corpus_paths
export OKTO_NEURON_VAULT="$VAULT"

if [[ ! -d "$CORPUS" ]]; then
  log "required private corpus not found at $CORPUS"
  _failures+=("private_corpus_missing path=$CORPUS")
  finish
fi
if [[ -n "${OKTO_NEURON_PRIVATE_CORPUS_VAULT:-}" && -e "$VAULT" && \
      "${OKTO_NEURON_PRIVATE_CORPUS_ALLOW_RESET:-0}" != "1" ]]; then
  log "refusing to reset caller-supplied existing vault without OKTO_NEURON_PRIVATE_CORPUS_ALLOW_RESET=1"
  _failures+=("existing_custom_vault_reset_not_confirmed path=$VAULT")
  finish
fi
log "corpus: $CORPUS"
log "vault:  $VAULT"

if [[ -z "$PROBES_FILE" || ! -f "$PROBES_FILE" ]]; then
  log "required tab-separated probe manifest not found; set OKTO_NEURON_PRIVATE_CORPUS_PROBES"
  _failures+=("private_corpus_probes_missing")
  finish
fi

declare -a PROBES=()
while IFS=$'\t' read -r query expected extra; do
  [[ -z "$query" || "$query" == \#* ]] && continue
  if [[ -z "$expected" || -n "$extra" ]]; then
    _failures+=("invalid_private_corpus_probe_manifest")
    finish
  fi
  PROBES+=("$query::$expected")
done < "$PROBES_FILE"
if [[ "${#PROBES[@]}" -eq 0 ]]; then
  _failures+=("private_corpus_probes_empty")
  finish
fi

# Provision only after all destructive inputs have passed the fail-closed gate.
source "$SCRIPT_DIR/_preamble.sh"

# Fresh vault every run — the flagship test exercises the ingest path.
log "rebuilding isolated private-corpus vault from scratch"
mkdir -p -- "$(dirname "$VAULT")"
rm -rf -- "$VAULT"
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.out" 2>"$work_dir/init.err"
assert_exit_code 0 $?

# Mirror corpus md files (excluding caches and repos/) into vault/notes/ preserving structure.
log "mirroring corpus markdown into vault/notes/"
count=0
while IFS= read -r -d '' f; do
  rel="${f#"$CORPUS"/}"
  dest="$VAULT/notes/$rel"
  mkdir -p "$(dirname "$dest")"
  cp "$f" "$dest"
  count=$((count+1))
done < <(find "$CORPUS" \
  \( -path '*/.*' -o -path '*/repos/*' -o -path '*/node_modules/*' \) -prune -o \
  -type f -name '*.md' -print0 2>/dev/null)
log "copied $count markdown files"
if [[ "$count" -lt 100 ]]; then
  _failures+=("corpus_too_small count=$count")
fi

# Start one isolated server with distinct free REST/MCP ports. The shared
# lifecycle helper also reads and exports the MCP-only daemon bearer token.
ARCHIVE="$work_dir/archive"
mkdir -p "$ARCHIVE"
if ! start_server "$VAULT"; then
  _failures+=("server_start_failed")
  finish
fi
SERVE_LOG="$work_dir/server.log"
curl -fsS "$OKTO_NEURON_ENDPOINT/health" \
  >"$ARCHIVE/health.json" 2>"$ARCHIVE/health.err"
assert_exit_code 0 $?
log "OKTO_NEURON_ENDPOINT=$OKTO_NEURON_ENDPOINT OKTO_NEURON_MCP_ENDPOINT=$OKTO_NEURON_MCP_ENDPOINT"

log "client/server ingest: $count files via POST /add (real bge-small)"
t0=$(date +%s)
add_fail=0
add_ok=0
: > "$work_dir/add.out"; : > "$work_dir/add.err"
while IFS= read -r -d '' f; do
  if kg add "$f" --endpoint "$OKTO_NEURON_ENDPOINT" --timeout 60 \
      >>"$work_dir/add.out" 2>>"$work_dir/add.err"; then
    add_ok=$((add_ok+1))
  else
    add_fail=$((add_fail+1))
  fi
done < <(find "$VAULT/notes" -type f -name '*.md' -print0)
add_dt=$(( $(date +%s) - t0 ))
log "ingest: ${add_dt}s ok=${add_ok} failures=${add_fail}"
max_fail=$(( count / 20 ))
if [[ "$add_fail" -gt "$max_fail" ]]; then
  _failures+=("too_many_ingest_failures fail=$add_fail max=$max_fail")
fi

# Probes — assert caller-declared topics surface from the expected files. HTTP /query.
probe_dir="$work_dir/probes"; mkdir -p "$probe_dir"
probe_t0=$(date +%s)
probe_index=0
for probe in "${PROBES[@]}"; do
  probe_index=$((probe_index+1))
  q="${probe%%::*}"; expect="${probe##*::}"
  safe=$(printf '%03d' "$probe_index")
  pt0=$(date +%s)
  kg query "$q" --endpoint "$OKTO_NEURON_ENDPOINT" --format json --k 10 \
    >"$probe_dir/${safe}.json" 2>"$probe_dir/${safe}.err"
  dt=$(( $(date +%s) - pt0 ))
  hits=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print(len(d))" "$probe_dir/${safe}.json" 2>/dev/null || echo 0)
  matched=$(python3 -c "
import json,sys,re
d=json.load(open(sys.argv[1]))
pat=re.compile(sys.argv[2], re.I)
print(sum(1 for h in d if pat.search(h.get('path','')+' '+(h.get('title','') or h.get('name','') or ''))))
" "$probe_dir/${safe}.json" "$expect" 2>/dev/null || echo 0)
  log "probe '$q' → hits=$hits matching '$expect'=$matched dt=${dt}s"
  if [[ "$hits" -lt 1 ]]; then
    _failures+=("probe_zero_hits q=$q")
  fi
  if [[ "$matched" -lt 1 ]]; then
    _failures+=("probe_no_expected_match q=$q expected=$expect")
  fi
done
probe_dt=$(( $(date +%s) - probe_t0 ))
log "probes: ${probe_dt}s"

# Sanity: graph size.
size=$(stat -f%z "$VAULT/graph.lbug" 2>/dev/null || stat -c%s "$VAULT/graph.lbug" 2>/dev/null || echo 0)
log "graph.lbug=${size} bytes"
if [[ "$size" -lt 1000000 ]]; then
  _failures+=("graph_too_small size=$size")
fi

# Stop the owned server BEFORE inspecting logs so shutdown-time WAL output is
# included, without touching unrelated daemons.
stop_server
# Also copy add.out/err into archive for post-mortem.
cp "$work_dir/add.out" "$ARCHIVE/add.out" 2>/dev/null || true
cp "$work_dir/add.err" "$ARCHIVE/add.err" 2>/dev/null || true
cp -r "$work_dir/probes" "$ARCHIVE/probes" 2>/dev/null || true
cp "$SERVE_LOG" "$ARCHIVE/serve.log" 2>/dev/null || true

# THE flagship assertion. The entire reason for the client/server pivot is
# eliminating "Corrupted wal file" — if it appears here, the run is a regression.
log "scanning server logs for 'Corrupted wal file'"
wal_hits=0
if [[ -f "$SERVE_LOG" ]]; then
  wal_hits=$(grep -c -i "Corrupted wal file" "$SERVE_LOG" 2>/dev/null || true)
  wal_hits=${wal_hits:-0}
fi
log "wal_corruption_hits=${wal_hits}"
if [[ "$wal_hits" -gt 0 ]]; then
  _failures+=("wal_corruption_detected hits=$wal_hits")
fi

finish
