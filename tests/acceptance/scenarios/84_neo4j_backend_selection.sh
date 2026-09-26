#!/usr/bin/env bash
# Scenario 84: the Neo4j backend (M5) exercised through the SAME lifecycle
# 83_grafx_backend_selection.sh already proves for Grafx -- onboarding
# consent (neo4j is registered and non-experimental, and since D-94 retired
# the D-12 gate outright, neither neo4j nor grafx requires
# --accept-experimental any more; no official backend does), kg add /
# kg query, kg rebuild (x2) / kg reconcile heal / kg reembed, and the REST
# rollback job -- pinned to `--backend neo4j` against a REAL, already-running
# local `neo4j:5-community` container (no mocks).
#
# Requires OKTO_NEURON_ACCEPTANCE_NEO4J_URI (e.g. bolt://127.0.0.1:7687) and
# OKTO_NEURON_ACCEPTANCE_NEO4J_CREDENTIAL_ENV (the NAME of an env var already
# holding the password, e.g. OKTO_NEURON_ACCEPTANCE_NEO4J_PASSWORD) to be set
# before this scenario runs; it skips cleanly (not a failure) when either is
# unset, mirroring how the contract suite's own `graph_store` fixture skips
# without a live server (M5 spec section 4). `_lib.sh`'s
# `kg_init_backend_args` already splices `--storage-uri`/
# `--storage-credential-env` from these same two env vars into every
# `kg init`/`okto-neuron init` call site when `OKTO_NEURON_ACCEPTANCE_BACKEND`
# is pinned to neo4j; this scenario passes them explicitly on its own
# `okto-neuron init` calls too since it intentionally always targets neo4j
# regardless of the harness-level `--backend` flag (same pattern as 83).
#
# Rollback expectation note (mirrors 83's own D-84 note): historically,
# `server/http.py`'s `api_curation_rollback` gate hardcoded the Ladybug-only
# `previous-graph.lbug` checkpoint filename, so it could never see Neo4j's
# real rollback evidence -- a `backup_tag` property on the metadata
# singleton row, not a filesystem path at all. D-84 (backend-neutral
# rollback availability) replaced that gate with
# `marginalia.curation.orchestrate.rollback_candidate`, which reads each
# backend's OWN evidence -- for Neo4j, the metadata singleton's
# `backup_tag`, verified to still tag at least one live node. Since reembed
# (the verb immediately before this rollback attempt) commits through
# `Neo4jStaging.commit` and stashes the pre-reembed generation as
# `backup_tag`, this scenario now expects and asserts a REAL 202 + completed
# rollback + reverted generation here, not a 409 -- see the case statement
# below.
set -uo pipefail
SCENARIO_NAME="84_neo4j_backend_selection"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

if [[ -z "${OKTO_NEURON_ACCEPTANCE_NEO4J_URI:-}" || -z "${OKTO_NEURON_ACCEPTANCE_NEO4J_CREDENTIAL_ENV:-}" ]]; then
  log "SKIP: OKTO_NEURON_ACCEPTANCE_NEO4J_URI / OKTO_NEURON_ACCEPTANCE_NEO4J_CREDENTIAL_ENV not set -- no live Neo4j server to target"
  finish
fi
if [[ -z "${!OKTO_NEURON_ACCEPTANCE_NEO4J_CREDENTIAL_ENV:-}" ]]; then
  log "SKIP: \$${OKTO_NEURON_ACCEPTANCE_NEO4J_CREDENTIAL_ENV} is empty -- no credential to authenticate with"
  finish
fi

# This scenario always needs the [neo4j] extra, regardless of whether the
# harness itself was invoked with `--backend neo4j` (see 83's own comment
# for why `uv sync` being exact-by-default means every additive sync here
# must repeat every extra it needs).
if ! (
  cd "$REPO_ROOT" || exit 1
  uv sync --quiet --python 3.12 --extra neo4j --group litellm
) >"$work_dir/uv-sync-neo4j.log" 2>&1; then
  log "uv sync --extra neo4j --group litellm failed; see $work_dir/uv-sync-neo4j.log"
  _failures+=("uv_sync_neo4j_failed")
  finish
fi

VAULT="$work_dir/vault"
QUERY_TEXT="Okto Neuron neo4j backend selection acceptance fixture"
BASELINE_PATHS_FILE="$work_dir/baseline_paths.json"

# _assert_neo4j_graph_populated <stage-label>: no filesystem graph to stat
# (M5 spec section 2 -- Neo4j has no bytes on disk at all), so assert
# directly against the live server instead: open a real Neo4jStore for this
# vault and confirm it reports more than the lone schema-metadata node.
_assert_neo4j_graph_populated() {
  local stage="$1"
  python3 - "$VAULT" "$stage" <<'PY'
import sys
from pathlib import Path

from okto_neuron.config import VaultConfig
from okto_neuron.store.neo4j import Neo4jStore

vault_path, stage = Path(sys.argv[1]), sys.argv[2]
config = VaultConfig.load(vault_path)
store = Neo4jStore(vault_path, config=config.storage)
try:
    nodes = list(store.list_nodes())
finally:
    store.close()
if len(nodes) < 2:
    print(f"[{stage}] expected > 1 node (metadata + real content), got {len(nodes)}", file=sys.stderr)
    sys.exit(1)
print(f"[{stage}] neo4j node count: {len(nodes)}")
PY
  local rc=$?
  if [[ "$rc" -ne 0 ]]; then
    _failures+=("neo4j_graph_not_populated stage=$stage")
    log "FAIL neo4j graph population check failed at stage=$stage"
  fi
}

# _curation_generation <stage-label>: same helper as 83, GET /api/v1/status.
_curation_generation() {
  local stage="$1"
  local dump="$work_dir/status_${stage}.json"
  local value
  value="$(python3 - "$OKTO_NEURON_ENDPOINT" "$dump" <<'PY'
import json
import sys
import urllib.request

endpoint, dump_path = sys.argv[1:]
with urllib.request.urlopen(endpoint.rstrip("/") + "/api/v1/status", timeout=10) as resp:
    payload = json.load(resp)
with open(dump_path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2)

vaults = payload.get("vaults")
if isinstance(vaults, list) and vaults:
    generation = (vaults[0].get("integrity") or {}).get("graph_generation") or ""
else:
    generation = (payload.get("integrity") or {}).get("graph_generation") or ""
print(generation)
PY
)"
  if [[ -z "$value" ]]; then
    _failures+=("curation_generation_unreadable stage=$stage see=$dump")
    log "FAIL could not read a non-empty graph_generation at stage=$stage; see $dump"
  fi
  printf '%s' "$value"
}

# _verify_notes_still_present <stage-label>: mirrors 83's own helper.
_verify_notes_still_present() {
  local stage="$1"
  local out="$work_dir/query_${stage}.json"
  log "kg query --format json ($stage)"
  kg query "$QUERY_TEXT" --endpoint "$OKTO_NEURON_ENDPOINT" --format json \
    >"$out" 2>"$work_dir/query_${stage}.stderr"
  local rc=$?
  assert_exit_code 0 "$rc"
  if [[ "$rc" -ne 0 ]]; then tail -40 "$work_dir/query_${stage}.stderr" >&2; fi

  python3 - "$BASELINE_PATHS_FILE" "$out" <<'PY'
import json
import sys

baseline_path, candidate_path = sys.argv[1], sys.argv[2]

with open(baseline_path, encoding="utf-8") as fh:
    baseline = set(json.load(fh))

with open(candidate_path, encoding="utf-8") as fh:
    hits = json.load(fh)
candidate = {hit.get("path") for hit in hits if hit.get("path")}

if not baseline:
    print("baseline note-path set is empty", file=sys.stderr)
    sys.exit(1)
if baseline != candidate:
    print(
        f"note path set changed: baseline={sorted(baseline)} now={sorted(candidate)}",
        file=sys.stderr,
    )
    sys.exit(1)
print(f"note paths stable ({sorted(baseline)})")
PY
  local content_rc=$?
  assert_exit_code 0 "$content_rc"
}

# ---------------------------------------------------------------------------
# Part 1: `okto-neuron init --backend neo4j` -- neo4j is registered and
# non-experimental (M5 spec section 2, NEO4J_CAPABILITIES.experimental =
# False), so -- like grafx since D-94 -- it needs NO --accept-experimental
# flag at all.
# ---------------------------------------------------------------------------
log "okto-neuron init $VAULT --backend neo4j --storage-uri ... --storage-credential-env ..."
okto-neuron init "$VAULT" --backend neo4j \
  --storage-uri "$OKTO_NEURON_ACCEPTANCE_NEO4J_URI" \
  --storage-credential-env "$OKTO_NEURON_ACCEPTANCE_NEO4J_CREDENTIAL_ENV" \
  >"$work_dir/init.stdout" 2>"$work_dir/init.stderr"
init_rc=$?
assert_exit_code 0 "$init_rc"
if [[ "$init_rc" -ne 0 ]]; then cat "$work_dir/init.stderr" >&2; fi
assert_file_exists "$VAULT/okto-neuron.yaml"
assert_contains "$VAULT/okto-neuron.yaml" '^storage:$'
assert_contains "$VAULT/okto-neuron.yaml" '^  backend: neo4j$'

# Explicit LLM disable -- same 0.0.48 rationale as 83's header: the
# discovery-first default carries an empty model, so the vault must not
# depend on any ambient endpoint at the default loopback base URL.
python3 - "$VAULT/okto-neuron.yaml" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
if "\nllm:" not in text:
    path.write_text(text.rstrip("\n") + "\nllm:\n  enabled: false\n", encoding="utf-8")
PY
assert_exit_code 0 $?

# ---------------------------------------------------------------------------
# Cycle 1 (server up): seed two notes via kg add, capture the baseline
# two-note query result.
# ---------------------------------------------------------------------------
if ! start_server "$VAULT"; then
  _failures+=("server_start_failed phase=seed")
  finish
fi

SEED_DIR="$work_dir/seed_notes"
mkdir -p "$SEED_DIR"
SEED1="$SEED_DIR/neo4j-alpha.md"
SEED2="$SEED_DIR/neo4j-beta.md"

cat > "$SEED1" <<EOF
# Neo4j Alpha
$QUERY_TEXT note Alpha covers desert canyon survey logs.
EOF
cat > "$SEED2" <<EOF
# Neo4j Beta
$QUERY_TEXT note Beta covers coral reef bleaching studies.
EOF

log "kg add $SEED1"
kg add "$SEED1" --endpoint "$OKTO_NEURON_ENDPOINT" \
  >"$work_dir/add1.stdout" 2>"$work_dir/add1.stderr"
assert_exit_code 0 $?

log "kg add $SEED2"
kg add "$SEED2" --endpoint "$OKTO_NEURON_ENDPOINT" \
  >"$work_dir/add2.stdout" 2>"$work_dir/add2.stderr"
assert_exit_code 0 $?

log "kg query --format json (baseline)"
kg query "$QUERY_TEXT" --endpoint "$OKTO_NEURON_ENDPOINT" --format json \
  >"$work_dir/query_baseline.json" 2>"$work_dir/query_baseline.stderr"
baseline_query_rc=$?
assert_exit_code 0 "$baseline_query_rc"
if [[ "$baseline_query_rc" -ne 0 ]]; then tail -40 "$work_dir/query_baseline.stderr" >&2; fi

python3 - "$work_dir/query_baseline.json" "$BASELINE_PATHS_FILE" <<'PY'
import json
import sys

src, dst = sys.argv[1], sys.argv[2]
with open(src, encoding="utf-8") as fh:
    hits = json.load(fh)
paths = sorted({hit.get("path") for hit in hits if hit.get("path")})
if len(paths) != 2:
    print(f"expected exactly 2 distinct note paths at baseline, got {paths}", file=sys.stderr)
    sys.exit(1)
with open(dst, "w", encoding="utf-8") as fh:
    json.dump(paths, fh)
print(f"baseline note paths: {paths}")
PY
baseline_paths_rc=$?
assert_exit_code 0 "$baseline_paths_rc"

stop_server
_assert_neo4j_graph_populated "after_seed"

# ---------------------------------------------------------------------------
# kg rebuild, TWICE (offline, model-free -- same rationale as 83).
# ---------------------------------------------------------------------------
log "kg rebuild $VAULT (1st)"
kg rebuild "$VAULT" >"$work_dir/rebuild1.stdout" 2>"$work_dir/rebuild1.stderr"
rebuild1_rc=$?
assert_exit_code 0 "$rebuild1_rc"
if [[ "$rebuild1_rc" -ne 0 ]]; then tail -40 "$work_dir/rebuild1.stderr" >&2; fi

log "kg rebuild $VAULT (2nd)"
kg rebuild "$VAULT" >"$work_dir/rebuild2.stdout" 2>"$work_dir/rebuild2.stderr"
rebuild2_rc=$?
assert_exit_code 0 "$rebuild2_rc"
if [[ "$rebuild2_rc" -ne 0 ]]; then tail -40 "$work_dir/rebuild2.stderr" >&2; fi
_assert_neo4j_graph_populated "after_rebuild2"

if ! start_server "$VAULT"; then
  _failures+=("server_start_failed phase=after_rebuild")
  finish
fi
_verify_notes_still_present "after_rebuild"
stop_server

# ---------------------------------------------------------------------------
# kg reconcile heal (offline, model-free).
# ---------------------------------------------------------------------------
log "kg reconcile heal $VAULT (1st)"
kg reconcile heal "$VAULT" >"$work_dir/heal.stdout" 2>"$work_dir/heal.stderr"
heal_rc=$?
assert_exit_code 0 "$heal_rc"
if [[ "$heal_rc" -ne 0 ]]; then tail -40 "$work_dir/heal.stderr" >&2; fi
_assert_neo4j_graph_populated "after_heal"

if ! start_server "$VAULT"; then
  _failures+=("server_start_failed phase=after_heal")
  finish
fi
_verify_notes_still_present "after_heal"
stop_server

# Second heal, same literal tag as the first (`Neo4jStaging.stage_path`
# always receives the fixed literal "heal" -- see server/_curation.py:2536,
# cli/kg.py:1675) -- regression coverage for the live-graph-deletion defect
# where the SECOND run of the same verb's unconditional pre-build
# `staging.discard(tmp_graph_path)` deleted the live graph the first run
# had just committed under that same literal tag.
log "kg reconcile heal $VAULT (2nd, same literal tag)"
kg reconcile heal "$VAULT" >"$work_dir/heal2.stdout" 2>"$work_dir/heal2.stderr"
heal2_rc=$?
assert_exit_code 0 "$heal2_rc"
if [[ "$heal2_rc" -ne 0 ]]; then tail -40 "$work_dir/heal2.stderr" >&2; fi
_assert_neo4j_graph_populated "after_heal2"

if ! start_server "$VAULT"; then
  _failures+=("server_start_failed phase=after_heal2")
  finish
fi
_verify_notes_still_present "after_heal2"
stop_server

# ---------------------------------------------------------------------------
# kg reembed (offline, model-free -- vectors-only).
# ---------------------------------------------------------------------------
log "kg reembed $VAULT (1st)"
kg reembed "$VAULT" >"$work_dir/reembed.stdout" 2>"$work_dir/reembed.stderr"
reembed_rc=$?
assert_exit_code 0 "$reembed_rc"
if [[ "$reembed_rc" -ne 0 ]]; then tail -40 "$work_dir/reembed.stderr" >&2; fi
_assert_neo4j_graph_populated "after_reembed"

# Second reembed, same literal tag as the first (`Neo4jStaging.stage_path`
# always receives the fixed literal "reembed" -- see server/_curation.py:2797,
# cli/kg.py:1675) -- same regression rationale as the second heal run above.
log "kg reembed $VAULT (2nd, same literal tag)"
kg reembed "$VAULT" >"$work_dir/reembed2.stdout" 2>"$work_dir/reembed2.stderr"
reembed2_rc=$?
assert_exit_code 0 "$reembed2_rc"
if [[ "$reembed2_rc" -ne 0 ]]; then tail -40 "$work_dir/reembed2.stderr" >&2; fi
_assert_neo4j_graph_populated "after_reembed2"

# ---------------------------------------------------------------------------
# Cycle 4 (server up, final): final query check, then the REST rollback
# attempt -- see file header (D-84) for why 202 + a completed,
# reverted-generation rollback is expected here.
# ---------------------------------------------------------------------------
if ! start_server "$VAULT"; then
  _failures+=("server_start_failed phase=after_reembed2")
  finish
fi
_verify_notes_still_present "after_reembed2"

pre_rollback_generation="$(_curation_generation "before_rollback")"
log "graph_generation before the rollback attempt: $pre_rollback_generation"

log "POST /api/v1/curation/rollback"
python3 - "$OKTO_NEURON_ENDPOINT" "$work_dir/rollback_post.json" \
  >"$work_dir/rollback_post.status" 2>"$work_dir/rollback_post.error" <<'PY'
import json
import sys
import urllib.error
import urllib.request

endpoint, out_path = sys.argv[1:]
url = endpoint.rstrip("/") + "/api/v1/curation/rollback"
req = urllib.request.Request(
    url, data=b"{}", method="POST", headers={"Content-Type": "application/json"}
)
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        status = resp.status
        body = json.load(resp)
except urllib.error.HTTPError as exc:
    status = exc.code
    raw = exc.read()
    try:
        body = json.loads(raw)
    except ValueError:
        body = {"raw": raw.decode("utf-8", "replace")}
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump({"status_code": status, "body": body}, fh, indent=2)
print(status)
print(body.get("error", "") if isinstance(body, dict) else "")
PY
rollback_status="$(sed -n '1p' "$work_dir/rollback_post.status")"
rollback_error="$(sed -n '2p' "$work_dir/rollback_post.status")"
log "rollback POST status: $rollback_status error=$rollback_error (body: $(cat "$work_dir/rollback_post.json"))"

case "$rollback_status/$rollback_error" in
  202/*)
    log "rollback was ACCEPTED (202) -- backend-neutral rollback availability (D-84) closed the documented Ladybug-only checkpoint gap this scenario used to route around; polling for completion."
    job_final_status=""
    deadline=$((SECONDS + 60))
    while (( SECONDS < deadline )); do
      poll_status="$(python3 - "$OKTO_NEURON_ENDPOINT" "$work_dir/rollback_poll.json" <<'PY'
import json
import sys
import urllib.request

endpoint, out_path = sys.argv[1:]
url = endpoint.rstrip("/") + "/api/v1/curation/rebuild/status?kind=rollback"
with urllib.request.urlopen(url, timeout=10) as resp:
    payload = json.load(resp)
with open(out_path, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, indent=2)
last = payload.get("last_rollback") or {}
print(last.get("status", ""))
PY
)"
      if [[ "$poll_status" == "done" || "$poll_status" == "error" ]]; then
        job_final_status="$poll_status"
        break
      fi
      sleep 1
    done
    post_rollback_generation="$(_curation_generation "after_rollback")"
    if [[ "$job_final_status" == "done" && "$post_rollback_generation" != "$pre_rollback_generation" ]]; then
      log "rollback actually completed and reverted the generation ($pre_rollback_generation -> $post_rollback_generation)"
    else
      _failures+=("rollback_accepted_but_did_not_complete job_status=${job_final_status:-timeout} before=$pre_rollback_generation after=$post_rollback_generation")
      log "FAIL rollback was accepted (202) but did not cleanly complete: job_status=${job_final_status:-timeout}"
    fi
    ;;
  *)
    _failures+=("rollback_response_unexpected status=$rollback_status error=$rollback_error")
    log "FAIL unexpected rollback response: status=$rollback_status error=$rollback_error (expected 202 -- backend-neutral rollback availability (D-84) means neo4j must now be accepted just like ladybug)"
    ;;
esac

_verify_notes_still_present "after_rollback_attempt"
stop_server

finish
