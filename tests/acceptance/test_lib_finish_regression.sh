#!/usr/bin/env bash
# Regression test for marginalia-deep-review.md §3.1:
# `finish()` in tests/acceptance/scenarios/_lib.sh must never report
# status=pass when start_server() failed, even for the common call-site
# pattern `start_server "$VAULT" || finish "reason"` where nothing else has
# appended to `_failures` yet.
#
# This is a standalone bash-level test, deliberately outside
# tests/acceptance/scenarios/ (its filename does not match the
# `[0-9][0-9]_*.sh` glob bin/acceptance.sh selects) so it never runs as a
# live acceptance scenario itself -- it stubs start_server() and never
# touches a real marginalia server.
#
# Run directly:
#   bash tests/acceptance/test_lib_finish_regression.sh
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/marginalia-lib-finish-test.XXXXXX")"
cleanup() { rm -rf -- "$TMP_ROOT"; }
trap cleanup EXIT

ACCEPTANCE_DIR="$TMP_ROOT/acceptance"
mkdir -p -- "$ACCEPTANCE_DIR"
REPORT="$ACCEPTANCE_DIR/report.jsonl"
FAKE_HOME="$TMP_ROOT/home"
mkdir -p -- "$FAKE_HOME"

# _lib.sh's finish() always exit()s -- run the repro in a subshell so that
# exit only ends the subshell, and this test script can inspect the result.
(
  set -uo pipefail
  export OKTO_NEURON_ACCEPTANCE_DIR="$ACCEPTANCE_DIR"
  export SCENARIO_NAME="99_lib_finish_regression"
  export ACCEPTANCE_REPORT="$REPORT"
  export REPO_ROOT
  export HOME="$FAKE_HOME"
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/scenarios/_lib.sh"

  # Simulate a real startup regression (crash on boot, port bind failure,
  # broken flag): start_server fails on its very first check, exactly like
  # 11 real scenario files' `start_server "$VAULT" || finish "reason"`
  # call sites hit it, with `_failures` still empty at that point.
  start_server() {
    log "stub start_server: simulating a startup crash"
    return 1
  }

  start_server "unused-vault" || finish "server-did-not-start"
  echo "unreachable: finish() must always exit" >&2
  exit 99
)
rc=$?

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

if [[ "$rc" -eq 0 ]]; then
  fail "finish() reported success (subshell exit 0) after start_server() failed"
fi

[[ -f "$REPORT" ]] || fail "no acceptance report was written to $REPORT"

status="$(python3 -c "
import json, sys
with open(sys.argv[1], encoding='utf-8') as fh:
    lines = [line for line in fh if line.strip()]
print(json.loads(lines[-1])['status'])
" "$REPORT")"

if [[ "$status" == "pass" ]]; then
  echo "FAIL: acceptance report recorded status=pass for a server-start failure (subshell exit=$rc)" >&2
  cat "$REPORT" >&2
  exit 1
fi

echo "PASS: start_server() failure correctly reported status=$status (subshell exit=$rc)"
exit 0
