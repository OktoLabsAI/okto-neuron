#!/usr/bin/env bash
# Okto Neuron acceptance harness — runs real scenarios end-to-end.
#
# HARD RULE: selected scenarios use no mocks and no reduced corpora. Required
# prerequisites fail; any explicitly optional sub-gate must say it was unavailable.
# Private-corpus and live-model scenarios are opt-in, not part of the default suite.
#
# Exit codes:
#   0 = all selected scenarios green
#   1 = at least one regression
#   2 = at least one known_bug, no regressions
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SCENARIOS_DIR="$REPO_ROOT/tests/acceptance/scenarios"
REPORT_DIR_RAW="${OKTO_NEURON_ACCEPTANCE_DIR:-/tmp/okto-neuron-acceptance}"

# Validate the caller-controlled report root before mkdir or report truncation.
# Scenarios recursively replace their own work directories, so the root must be
# an absolute, dedicated location that cannot alias a protected tree.
if ! REPORT_DIR=$(python3 - "$REPORT_DIR_RAW" "$REPO_ROOT" "${HOME:-}" \
  "${OKTO_NEURON_PRIVATE_CORPUS:-${HOME:-}/.marginalia-private-corpus}" <<'PY'
from pathlib import Path
import sys
import tempfile


def fail(message: str) -> None:
    print(f"unsafe OKTO_NEURON_ACCEPTANCE_DIR: {message}", file=sys.stderr)
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


raw, repo_raw, home_raw, corpus_raw = sys.argv[1:]
root = resolve(raw, "report root")
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

print(root)
PY
); then
  exit 1
fi

mkdir -p -- "$REPORT_DIR" || exit 1
if [[ "$(cd "$REPORT_DIR" && pwd -P)" != "$REPORT_DIR" ]]; then
  echo "acceptance report root changed while being prepared: $REPORT_DIR" >&2
  exit 1
fi
export OKTO_NEURON_ACCEPTANCE_DIR="$REPORT_DIR"
export ACCEPTANCE_REPORT="$REPORT_DIR/report.jsonl"
if [[ -L "$ACCEPTANCE_REPORT" || ( -e "$ACCEPTANCE_REPORT" && ! -f "$ACCEPTANCE_REPORT" ) ]]; then
  echo "refusing unsafe acceptance report target: $ACCEPTANCE_REPORT" >&2
  exit 1
fi
# Unlink a pre-existing regular file before recreation so even an unexpected
# hard link cannot cause truncation of another pathname's content. Noclobber
# makes recreation fail if anything races into this name.
rm -f -- "$ACCEPTANCE_REPORT"
if ! ( set -o noclobber; umask 077; : > "$ACCEPTANCE_REPORT" ); then
  echo "could not create a fresh acceptance report: $ACCEPTANCE_REPORT" >&2
  exit 1
fi

GREEN=$'\033[32m'; RED=$'\033[31m'; YEL=$'\033[33m'; NC=$'\033[0m'

usage() {
  cat <<'EOF'
Usage: ./bin/acceptance.sh [--json] [--private] [--realmodel] [--backend NAME] [filter]

Runs the deterministic, model-free, non-private acceptance scenarios by default.

  filter        Run matching scenario names. Explicit 56/90/91 filters opt into
                the live-model or private scenario they select.
  --realmodel   Include live-model scenario 56 in an unfiltered run. Requires a
                real OpenAI-compatible endpoint in OKTO_NEURON_LLM_BASE_URL. The
                model defaults to unsloth/Qwen3.6-27B-NVFP4 and can be overridden
                with OKTO_NEURON_REALMODEL_MODEL.
  --private     Include external private-corpus scenarios 90 and 91 in an unfiltered run.
  --backend NAME Pin every scenario's `kg init`/`okto-neuron init` to backend NAME
                (exported as OKTO_NEURON_ACCEPTANCE_BACKEND). Defaults to grafx
                when omitted, matching marginalia's own DEFAULT_NEW_VAULT_BACKEND
                (D-94: Grafx is the default, non-experimental graph backend;
                Ladybug and Neo4j remain selectable). --accept-experimental is
                added to init calls only for a backend whose own
                capabilities_for(...).experimental flag is set -- true for no
                official backend today. grafx and ladybug are base package
                dependencies and need no extra uv sync step; NAME=neo4j syncs
                the [neo4j] extra.
  --json        Print the aggregated JSON report after the summary.
  -h, --help    Show this help.

Live-model and private scenarios require their real prerequisites. Missing
prerequisites fail the selected scenario; they are never reported as passes.
EOF
}

# Parse args: optional flags + one optional filter (order-agnostic). --backend
# is the one flag that also consumes a value, so this uses a stateful while/shift
# loop rather than the simpler `for arg in "$@"` scan.
emit_json=0
include_private=0
include_realmodel=0
filter=""
backend=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --json) emit_json=1; shift ;;
    --private) include_private=1; shift ;;
    --realmodel) include_realmodel=1; shift ;;
    --backend)
      if [[ $# -lt 2 || -z "$2" || "$2" == -* ]]; then
        echo "--backend requires a NAME argument" >&2
        usage >&2
        exit 1
      fi
      backend="$2"
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    --*) echo "unknown option: $1" >&2; usage >&2; exit 1 ;;
    *)
      if [[ -n "$filter" ]]; then
        echo "only one scenario filter is supported" >&2
        usage >&2
        exit 1
      fi
      filter="$1"
      shift
      ;;
  esac
done
if [[ -n "$backend" ]]; then
  export OKTO_NEURON_ACCEPTANCE_BACKEND="$backend"
fi

is_private_scenario() {
  case "$1" in
    90_private_corpus_ingest|91_private_corpus_qa_dogfooding) return 0 ;;
    *) return 1 ;;
  esac
}

is_realmodel_scenario() {
  case "$1" in
    56_remember_anchored_claims) return 0 ;;
    *) return 1 ;;
  esac
}

echo "=== okto-neuron acceptance harness ==="
echo "scenarios dir: $SCENARIOS_DIR"
echo "report:        $ACCEPTANCE_REPORT"
if [[ -n "${OKTO_NEURON_ACCEPTANCE_BACKEND:-}" ]]; then
  echo "backend:       $OKTO_NEURON_ACCEPTANCE_BACKEND"
else
  echo "backend:       grafx (default)"
fi
if [[ -z "$filter" && "$include_private" -eq 0 ]]; then
  echo "private:       excluded (use --private or an explicit 90/91 filter)"
fi
if [[ -z "$filter" && "$include_realmodel" -eq 0 ]]; then
  echo "live model:    excluded (use --realmodel or an explicit 56 filter)"
fi
echo

regressions=0
known_bugs=0
passes=0
selected=0

for s in "$SCENARIOS_DIR"/[0-9][0-9]_*.sh; do
  name="$(basename "$s" .sh)"
  if [[ -n "$filter" && "$name" != *"$filter"* ]]; then
    continue
  fi
  if [[ -z "$filter" && "$include_private" -eq 0 ]] && is_private_scenario "$name"; then
    continue
  fi
  if [[ -z "$filter" && "$include_realmodel" -eq 0 ]] && is_realmodel_scenario "$name"; then
    continue
  fi
  selected=$((selected+1))
  export SCENARIO_WORK_DIR="$REPORT_DIR/$name"
  echo "--- $name ---"
  bash "$s"
  rc=$?
  case "$rc" in
    0) passes=$((passes+1)) ;;
    2) known_bugs=$((known_bugs+1)) ;;
    *) regressions=$((regressions+1)) ;;
  esac
  echo
done

if [[ "$selected" -eq 0 ]]; then
  echo "No acceptance scenarios matched filter: ${filter:-<none>}" >&2
  exit 1
fi

echo "=== summary ==="
echo "  pass:        $passes"
echo "  known_bug:   $known_bugs"
echo "  regression:  $regressions"
echo "  report:      $ACCEPTANCE_REPORT"
echo

if [[ "$emit_json" -eq 1 ]]; then
  if command -v jq >/dev/null 2>&1; then
    jq -s . "$ACCEPTANCE_REPORT"
  else
    python3 -c "import json,sys; print(json.dumps([json.loads(l) for l in open('$ACCEPTANCE_REPORT') if l.strip()], indent=2))"
  fi
fi

if [[ "$regressions" -gt 0 ]]; then
  echo "${RED}OVERALL: REGRESSION${NC}"
  exit 1
elif [[ "$known_bugs" -gt 0 ]]; then
  echo "${YEL}OVERALL: KNOWN_BUG (no regressions)${NC}"
  exit 2
else
  echo "${GREEN}OVERALL: GREEN${NC}"
  exit 0
fi
