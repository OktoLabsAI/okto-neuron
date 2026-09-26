#!/usr/bin/env bash
# Shared helpers for marginalia acceptance scenarios.
# HARD RULE: no mocks and no reduced corpora. Required prerequisites fail;
# explicitly optional sub-gates must report when they are unavailable.
set -uo pipefail

_AL_GREEN=$'\033[32m'; _AL_RED=$'\033[31m'; _AL_YEL=$'\033[33m'; _AL_NC=$'\033[0m'

# Capture the invoking user's HOME once, before acceptance switches to its
# private process environment below.  Private-corpus defaults and destructive
# path guards must compare against the real caller HOME, never the sandbox HOME.
export OKTO_NEURON_ACCEPTANCE_CALLER_HOME="${HOME:-}"

scenario_name="${SCENARIO_NAME:-$(basename "${BASH_SOURCE[1]:-unknown}" .sh)}"
acceptance_root_raw="${OKTO_NEURON_ACCEPTANCE_DIR:-/tmp/okto-neuron-acceptance}"
work_dir_raw="${SCENARIO_WORK_DIR:-$acceptance_root_raw/$scenario_name}"
report_jsonl_raw="${ACCEPTANCE_REPORT:-$acceptance_root_raw/report.jsonl}"
acceptance_repo_root="${REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd -P)}"

# This check is intentionally independent of bin/acceptance.sh: every scenario
# can be invoked directly, and no caller-controlled path may reach rm -rf first.
if ! acceptance_paths=$(python3 - "$acceptance_root_raw" "$work_dir_raw" \
  "$report_jsonl_raw" "$scenario_name" "$acceptance_repo_root" "${HOME:-}" \
  "${OKTO_NEURON_PRIVATE_CORPUS:-${OKTO_NEURON_ACCEPTANCE_CALLER_HOME:-}/.marginalia-private-corpus}" <<'PY'
from pathlib import Path
import re
import sys
import tempfile


def fail(message: str) -> None:
    print(f"unsafe acceptance sandbox: {message}", file=sys.stderr)
    raise SystemExit(1)


def resolve(raw: str, label: str) -> Path:
    if not raw or any(ord(char) < 32 for char in raw):
        fail(f"{label} is empty or contains control characters")
    path = Path(raw)
    if not path.is_absolute():
        fail(f"{label} must be absolute: {raw}")
    if ".." in path.parts:
        fail(f"{label} must not contain '..': {raw}")
    if path.is_symlink():
        fail(f"{label} must not be a symlink: {raw}")
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        fail(f"cannot resolve {label} {raw!r}: {exc}")


def ancestor_or_same(candidate: Path, protected: Path) -> bool:
    return candidate == protected or candidate in protected.parents


def related(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


root_raw, work_raw, report_raw, scenario, repo_raw, home_raw, corpus_raw = sys.argv[1:]
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", scenario) or scenario in {".", ".."}:
    fail(f"invalid scenario name: {scenario!r}")
root = resolve(root_raw, "report root")
work = resolve(work_raw, "scenario work directory")
report = resolve(report_raw, "report file")
repo = resolve(repo_raw, "repository")
corpus = resolve(corpus_raw, "private corpus")
home = resolve(home_raw, "home") if home_raw else None
temp_root = Path(tempfile.gettempdir()).resolve(strict=False)

if root == Path(root.anchor):
    fail(f"report root resolves to filesystem root: {root}")
if ancestor_or_same(root, temp_root):
    fail(f"report root must be below, not equal to/above, the system temp root: {root}")
if home is not None and ancestor_or_same(root, home):
    fail(f"report root is HOME or its ancestor: {root}")
if related(root, repo):
    fail(f"report root overlaps the repository: {root}")
if related(root, corpus):
    fail(f"report root overlaps the private corpus: {root}")

expected_work = (root / scenario).resolve(strict=False)
if work != expected_work:
    fail(f"scenario work directory must equal {expected_work}: {work}")
if report.parent != root:
    fail(f"report file must be a direct child of {root}: {report}")

print(f"{root}\t{work}\t{report}")
PY
); then
  exit 1
fi

IFS=$'\t' read -r acceptance_root work_dir report_jsonl <<<"$acceptance_paths"
unset acceptance_paths
export OKTO_NEURON_ACCEPTANCE_DIR="$acceptance_root"
export SCENARIO_WORK_DIR="$work_dir"
export ACCEPTANCE_REPORT="$report_jsonl"

mkdir -p -- "$acceptance_root" || exit 1
if [[ -L "$report_jsonl" || ( -e "$report_jsonl" && ! -f "$report_jsonl" ) ]]; then
  echo "refusing unsafe acceptance report target: $report_jsonl" >&2
  exit 1
fi
if [[ ! -e "$report_jsonl" ]]; then
  if ! ( set -o noclobber; umask 077; : > "$report_jsonl" ); then
    echo "could not create acceptance report: $report_jsonl" >&2
    exit 1
  fi
fi
if ! python3 - "$report_jsonl" <<'PY'
import os
import stat
import sys

metadata = os.lstat(sys.argv[1])
raise SystemExit(0 if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1 else 1)
PY
then
  echo "refusing linked or non-regular acceptance report: $report_jsonl" >&2
  exit 1
fi
rm -rf -- "$work_dir"
mkdir -p -- "$work_dir" || exit 1
if [[ "$(cd "$work_dir" && pwd -P)" != "$work_dir" ]]; then
  echo "acceptance work directory changed while being prepared: $work_dir" >&2
  exit 1
fi

# Every scenario gets an isolated user/config namespace before it can invoke a
# Okto Neuron CLI.  This prevents `kg init`, onboarding, server vault discovery,
# and any implicit config write from reading or mutating ~/.okto-neuron (or a
# pre-0.3.0 ~/.marginalia).
acceptance_home="$work_dir/home"
mkdir -p -- \
  "$acceptance_home/.okto-neuron" \
  "$acceptance_home/.config" \
  "$acceptance_home/.local/share" \
  "$acceptance_home/.local/state" || exit 1
export HOME="$acceptance_home"
export OKTO_NEURON_CONFIG="$HOME/.okto-neuron/okto-neuron.toml"
export OKTO_NEURON_ENV_FILE="$HOME/.okto-neuron/env"
export XDG_CONFIG_HOME="$HOME/.config"
export XDG_DATA_HOME="$HOME/.local/share"
export XDG_STATE_HOME="$HOME/.local/state"
export OKTO_NEURON_VAULT="$work_dir/vault"
export UV_PROJECT_ENVIRONMENT="$acceptance_root/.venv"
unset VIRTUAL_ENV
unset OKTO_NEURON_AUTH_TOKEN

# Never inherit a developer/user daemon endpoint into acceptance. Scenarios
# that own a server replace these fail-closed port-0 sentinels via start_server
# (or set an explicit isolated endpoint themselves) before any client call.
export OKTO_NEURON_ENDPOINT="http://127.0.0.1:0"
export OKTO_NEURON_MCP_ENDPOINT="http://127.0.0.1:0/mcp"

_t0="$(date +%s)"
_assertions=0
_failures=()

log() { printf '[%s] %s\n' "$scenario_name" "$*" >&2; }

# kg_init_backend_args — extra flags for a `kg init`/`okto-neuron init` call
# site: `--backend "$OKTO_NEURON_ACCEPTANCE_BACKEND" [--accept-experimental]`
# whenever the harness pinned ANY backend (bin/acceptance.sh --backend NAME,
# ladybug included), otherwise nothing. `--accept-experimental` is spliced in
# only when the backend's own `capabilities_for(...).experimental` flag says
# so -- true for no official backend today (D-94 retired Grafx's D-12 gate),
# but this stays dynamic as the documented hook for a future experimental
# backend (in-tree or third-party). Scenarios invoked directly (not through
# bin/acceptance.sh) never set OKTO_NEURON_ACCEPTANCE_BACKEND, so this is a
# silent no-op there and the CLI's own default (grafx, DEFAULT_NEW_VAULT_BACKEND)
# governs unchanged. Callers splice the (possibly empty) result unquoted:
# `kg init "$VAULT" $(kg_init_backend_args)`.
#
# The empty-only early return matters: an explicit `--backend ladybug` pin
# (bin/acceptance.sh --backend ladybug) must still emit `--backend ladybug`
# on every init call site -- since the CLI's own unflagged default is grafx
# now, silently no-op'ing on "ladybug" would leave a "ladybug run" actually
# creating grafx vaults.
kg_init_backend_args() {
  local backend="${OKTO_NEURON_ACCEPTANCE_BACKEND:-}"
  if [[ -z "$backend" ]]; then
    return
  fi
  local args
  args="--backend $backend"
  if python3 -c "
import sys
from okto_neuron.store.capabilities import capabilities_for
caps = capabilities_for(sys.argv[1])
sys.exit(0 if caps is not None and caps.experimental else 1)
" "$backend"; then
    args="$args --accept-experimental"
  fi
  if [[ "$backend" == "neo4j" ]]; then
    if [[ -n "${OKTO_NEURON_ACCEPTANCE_NEO4J_URI:-}" ]]; then
      args="$args --storage-uri ${OKTO_NEURON_ACCEPTANCE_NEO4J_URI}"
    fi
    if [[ -n "${OKTO_NEURON_ACCEPTANCE_NEO4J_CREDENTIAL_ENV:-}" ]]; then
      args="$args --storage-credential-env ${OKTO_NEURON_ACCEPTANCE_NEO4J_CREDENTIAL_ENV}"
    fi
  fi
  printf -- '%s' "$args"
}

# kg_snapshot_load_backend_args — extra flags for a `kg snapshot load` call
# site restoring a snapshot whose origin backend needs live connection info to
# even open a store (currently only neo4j -- see Neo4jStorageConfig). A
# snapshot dump never carries connection secrets, so a scenario restoring one
# under a pinned non-ladybug/-grafx backend must re-supply the SAME server's
# URI/credential-env it dumped from, mirroring kg_init_backend_args above.
# Empty (no-op) for ladybug/grafx or when no backend is pinned.
kg_snapshot_load_backend_args() {
  local backend="${OKTO_NEURON_ACCEPTANCE_BACKEND:-}"
  if [[ "$backend" != "neo4j" ]]; then
    return
  fi
  local args=""
  if [[ -n "${OKTO_NEURON_ACCEPTANCE_NEO4J_URI:-}" ]]; then
    args="--storage-uri ${OKTO_NEURON_ACCEPTANCE_NEO4J_URI}"
  fi
  if [[ -n "${OKTO_NEURON_ACCEPTANCE_NEO4J_CREDENTIAL_ENV:-}" ]]; then
    args="$args --storage-credential-env ${OKTO_NEURON_ACCEPTANCE_NEO4J_CREDENTIAL_ENV}"
  fi
  printf -- '%s' "$args"
}

# assert_exit_code <expected> <actual>
assert_exit_code() {
  _assertions=$((_assertions+1))
  if [[ "$1" -ne "$2" ]]; then
    _failures+=("exit_code expected=$1 actual=$2")
    log "${_AL_RED}FAIL${_AL_NC} exit_code expected=$1 actual=$2"
    return 1
  fi
}

# assert_contains <haystack-file> <regex>
assert_contains() {
  _assertions=$((_assertions+1))
  if ! grep -E -q "$2" "$1"; then
    _failures+=("missing_pattern regex=$2 file=$1")
    log "${_AL_RED}FAIL${_AL_NC} pattern not found: $2 (in $1)"
    return 1
  fi
}

# assert_not_contains <file> <regex>
assert_not_contains() {
  _assertions=$((_assertions+1))
  if grep -E -q "$2" "$1"; then
    _failures+=("unwanted_pattern regex=$2 file=$1")
    log "${_AL_RED}FAIL${_AL_NC} unwanted pattern found: $2 (in $1)"
    return 1
  fi
}

# assert_file_exists <path>
assert_file_exists() {
  _assertions=$((_assertions+1))
  if [[ ! -e "$1" ]]; then
    _failures+=("file_missing path=$1")
    log "${_AL_RED}FAIL${_AL_NC} expected file: $1"
    return 1
  fi
}

# assert_min_count <actual> <min> <label>
# Prefer this over assert_min_bytes for "did the graph get populated" checks:
# file size tracks page allocation and checkpoint timing, counts track content.
assert_min_count() {
  _assertions=$((_assertions+1))
  local actual="${1:-0}"; local min="$2"; local label="$3"
  if [[ ! "$actual" =~ ^[0-9]+$ ]] || [[ "$actual" -lt "$min" ]]; then
    _failures+=("too_few $label=$actual min=$min")
    log "${_AL_RED}FAIL${_AL_NC} $label=$actual < min=$min"
    return 1
  fi
}

# assert_min_bytes <path> <min_bytes>
assert_min_bytes() {
  _assertions=$((_assertions+1))
  local sz; sz=$(stat -f%z "$1" 2>/dev/null || stat -c%s "$1" 2>/dev/null || echo 0)
  if [[ "$sz" -lt "$2" ]]; then
    _failures+=("too_small path=$1 size=$sz min=$2")
    log "${_AL_RED}FAIL${_AL_NC} $1 size=$sz < min=$2"
    return 1
  fi
}

# assert_graph_populated <vault> <label> [min_bytes] [min_files]
# Backend-neutral "the live graph actually has content" check, shared by
# every scenario that used to hardcode a `graph.lbug` min-bytes assertion.
# Effective backend defaults to grafx when OKTO_NEURON_ACCEPTANCE_BACKEND is
# unset, matching the CLI's own DEFAULT_NEW_VAULT_BACKEND (D-94) -- NOT
# ladybug, which was this repo's pre-D-94 default.
#   - ladybug: single-file graph.lbug, assert_min_bytes (default 100000).
#   - grafx: graph.grafx/ is a directory of segment files; assert it exists
#     and holds at least min_files real files (default 5) -- a byte count on
#     a directory doesn't mean anything, so this mirrors 83's own
#     _assert_grafx_graph_populated helper instead of reusing assert_min_bytes.
#   - anything else (e.g. neo4j): server-side storage, no local graph
#     artifact to inspect -- logs a skip rather than asserting a path that
#     was never supposed to exist for that backend.
assert_graph_populated() {
  local vault="$1" label="$2" min_bytes="${3:-100000}" min_files="${4:-5}"
  local backend="${OKTO_NEURON_ACCEPTANCE_BACKEND:-grafx}"
  case "$backend" in
    ladybug)
      assert_min_bytes "$vault/graph.lbug" "$min_bytes"
      ;;
    grafx)
      assert_file_exists "$vault/graph.grafx"
      local count
      count="$(find "$vault/graph.grafx" -type f 2>/dev/null | wc -l | tr -d ' ')"
      assert_min_count "$count" "$min_files" "grafx_graph_file_count[$label]"
      ;;
    *)
      log "graph populated check ($label) SKIPPED -- backend='$backend' is server-side (no local graph artifact to inspect)"
      ;;
  esac
}

emit_report() {
  local status="$1"; local known_bug="${2:-}"
  local dt=$(( $(date +%s) - _t0 ))
  local failures_json="[]"
  if [[ ${#_failures[@]} -gt 0 ]]; then
    failures_json=$(printf '%s\n' "${_failures[@]}" | python3 -c 'import sys,json;print(json.dumps([l.rstrip() for l in sys.stdin if l.strip()]))')
  fi
  python3 -c "
import json
print(json.dumps({
  'scenario': '$scenario_name',
  'status': '$status',
  'known_bug': '${known_bug}',
  'duration_s': $dt,
  'assertions': $_assertions,
  'failures': $failures_json
}))" >> "$report_jsonl"
  if [[ "$status" == "pass" ]]; then
    log "${_AL_GREEN}PASS${_AL_NC} assertions=$_assertions duration=${dt}s"
  elif [[ -n "$known_bug" ]]; then
    log "${_AL_YEL}KNOWN_BUG${_AL_NC} ($known_bug) assertions=$_assertions duration=${dt}s"
  else
    log "${_AL_RED}REGRESSION${_AL_NC} assertions=$_assertions duration=${dt}s"
  fi
}

finish() {
  local known_bug="${1:-}"
  # A caller that passes a known_bug/failure reason (e.g. the common
  # `start_server ... || finish "reason"` pattern) is asserting a failure
  # already happened. That assertion must never be downgraded to
  # status=pass just because nothing else has appended to `_failures` yet
  # -- otherwise "the thing I was about to test never started" silently
  # reports green. Only a bare `finish` (no reason) with zero recorded
  # failures is an actual pass.
  if [[ ${#_failures[@]} -eq 0 && -z "$known_bug" ]]; then
    emit_report "pass"
    exit 0
  elif [[ -n "$known_bug" ]]; then
    emit_report "known_bug" "$known_bug"
    exit 2
  else
    emit_report "regression"
    exit 1
  fi
}

# start_server / stop_server are provided by _preamble.sh (sourced after _lib.sh).
# They export OKTO_NEURON_ENDPOINT (REST) and OKTO_NEURON_MCP_ENDPOINT (MCP).
