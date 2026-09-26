#!/usr/bin/env bash
# Scenario 00: uv provisioning smoke — `uv sync` the locked env, then real
# `kg --help`, real `import okto_neuron`, real `okto-neuron serve` smoke via
# start_server. NO mocks. uv is the canonical entry point (no pip).
set -uo pipefail
SCENARIO_NAME="00_cold_install"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"

log "uv sync (provision the locked env: serve+dev groups = ladybug + fastmcp + fastembed)"
source "$SCRIPT_DIR/_preamble.sh"

log "kg --help"
kg --help >"$work_dir/kg-help.stdout" 2>"$work_dir/kg-help.stderr"
assert_exit_code 0 $?
assert_contains "$work_dir/kg-help.stdout" "^[[:space:]]*Commands:"
assert_contains "$work_dir/kg-help.stdout" "init"
assert_contains "$work_dir/kg-help.stdout" "add"
assert_contains "$work_dir/kg-help.stdout" "query"

log "python -c 'import okto_neuron; from okto_neuron.models import embed; from okto_neuron.vault import Vault'"
python3 -c "import okto_neuron; from okto_neuron.models import embed; from okto_neuron.vault import Vault" \
  >"$work_dir/import.stdout" 2>"$work_dir/import.stderr"
assert_exit_code 0 $?

# ---------------------------------------------------------------------------
# Client/server smoke: with this freshly-installed venv, init a vault,
# launch okto-neuron serve via start_server (real /health probe), and tear
# it down cleanly. No mocks: real binary, real REST surface.
# ---------------------------------------------------------------------------
VAULT="$work_dir/vault"
log "kg init $VAULT (cold-install client/server smoke)"
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.stdout" 2>"$work_dir/init.stderr"
assert_exit_code 0 $?
assert_file_exists "$VAULT/okto-neuron.yaml"

log "start_server $VAULT"
if start_server "$VAULT"; then
  assert_exit_code 0 0
  # Real /version probe to prove the REST surface actually answers.
  python3 - <<PY >"$work_dir/version.stdout" 2>"$work_dir/version.stderr"
import json, urllib.request
request = urllib.request.Request("${OKTO_NEURON_ENDPOINT}/version")
with urllib.request.urlopen(request, timeout=5) as r:
    body = json.loads(r.read())
print(json.dumps(body))
PY
  assert_exit_code 0 $?
  assert_contains "$work_dir/version.stdout" "marginalia_version"
  assert_contains "$work_dir/version.stdout" "api_version"
  stop_server
else
  _failures+=("start_server failed; see $work_dir/server.log")
  log "FAIL start_server"
fi

finish
