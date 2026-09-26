#!/usr/bin/env bash
# Scenario 98: the curation verbs (kg rebuild / kg reconcile heal / kg reembed)
# plus the REST rollback job, exercised end-to-end model-free against a
# two-note vault.
#
# kg rebuild / kg reconcile heal / kg reembed are offline CLI verbs that
# refuse to run while a server owns the vault (they take the vault's offline
# rebuild lock), while kg add / kg query / the rollback job are thin clients
# that only work through a running server. So this scenario cycles the
# server up for every client call and down for every offline verb — the same
# pattern 94_snapshot_roundtrip.sh uses for its dump/verify/load step.
#
# Verb order (read before "fixing" a failure here): kg rebuild runs TWICE in
# a row before rollback is ever attempted. This is deliberate, not padding.
# POST /api/v1/curation/rollback derives its target generation from the
# vault's CURRENT live generation and requires a matching
# <vault>/.marginalia/rebuild-artifacts/<generation>/previous-{graph.lbug,
# semantic-materialization.json,semantic-policy.json} checkpoint (all three).
# cli/kg.py's _write_previous_semantic_materialization /
# _write_semantic_policy_checkpoint (the functions that populate the two
# semantic files) are gated on a REAL previous generation existing
# (`if not graph_generation: return None`, kg.py:1772-1797) — read from the
# persisted <vault>/.marginalia/graph-integrity.json, which nothing but a
# prior rebuild/heal/reembed swap ever populates. A vault's FIRST-EVER
# rebuild therefore has no "previous" generation to snapshot and can NEVER
# satisfy rollback's checkpoint contract, no matter when rollback is called
# afterward — empirically confirmed (rebuild-artifacts/<gen1>/ came back
# with only previous-graph.lbug and previous-graph-integrity.json, missing
# both semantic files, a genuine, permanent gap for gen1). A SECOND rebuild
# immediately after the first gives rollback a real previous generation
# (gen1) to snapshot, and its own rebuild-artifacts/<gen2>/ then carries all
# three required files — also empirically confirmed, followed by a real
# POST rollback that returned 202, polled to "done", and reverted the live
# generation to gen1 exactly. Calling rollback any later (e.g. after heal or
# reembed, neither of which ever writes rebuild-artifacts/ — see the M2b
# facts sheet's own critical finding) targets a generation that structurally
# can never have a complete checkpoint, so it would 409 forever regardless of
# M2b's own correctness. Placing rollback right after the second rebuild is
# the one placement in this four-verb chain where the REST surface can
# genuinely succeed without faking anything or touching the explicitly
# out-of-scope server/http.py 409 gate.
set -uo pipefail
SCENARIO_NAME="98_curation_verbs"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

# ---------------------------------------------------------------------------
# The vault under test EXPLICITLY disables the LLM (written into
# okto-neuron.yaml right after kg init, below): since 0.0.48 the discovery-
# first built-in default carries an EMPTY model, so an ambient live endpoint
# at the default loopback base URL (127.0.0.1:8123) rejects it with a 500 --
# and the rebuild audit (RebuildAuditFailed) fails on provider-failed files.
# Whether the host happens to run such an endpoint is environmental noise
# this scenario must not depend on: with llm.enabled: false the offline verbs
# take the deterministic structural-ingest + extraction-skip path in EVERY
# environment. litellm is still synced for IMPORTABILITY: the pipeline's
# provider plumbing is litellm-backed unconditionally, and without the
# package the offline verbs crash on ModuleNotFoundError. _preamble.sh only
# syncs the litellm uv group for scenario 56; sync it here too, additively.
# `uv sync` is EXACT by default (it prunes any package outside the requested
# extras/groups, extras included -- confirmed empirically; it is NOT limited
# to pruning packages outside pyproject's default-groups). grafx and ladybug
# are BASE package dependencies (D-94 step 1), so this sync needs no
# `--extra grafx` branch -- only `neo4j` is still extra-only and must be
# repeated here or a bare `--group litellm` sync would prune the driver
# _preamble.sh already installed for a harness-pinned neo4j run.
uv_sync_litellm_args=(--quiet --python 3.12 --group litellm)
if [[ "${OKTO_NEURON_ACCEPTANCE_BACKEND:-}" == "neo4j" ]]; then
  uv_sync_litellm_args+=(--extra neo4j)
fi
if ! (
  cd "$REPO_ROOT" || exit 1
  uv sync "${uv_sync_litellm_args[@]}"
) >"$work_dir/uv-sync-litellm.log" 2>&1; then
  log "uv sync --group litellm failed; see $work_dir/uv-sync-litellm.log"
  _failures+=("uv_sync_litellm_failed")
  finish
fi
unset uv_sync_litellm_args

VAULT="$work_dir/vault"
QUERY_TEXT="Okto Neuron curation verbs acceptance fixture"
BASELINE_PATHS_FILE="$work_dir/baseline_paths.json"

# ---------------------------------------------------------------------------
# Helpers local to this scenario.
# ---------------------------------------------------------------------------

# _curation_generation <stage-label>: print the vault's current
# graph_generation via the REST status route (GET /api/v1/status). Empirically
# (dumped from inside this harness, not assumed): `okto-neuron serve --vault
# <path>` reports scope=="application" with vault_count==1 and a `vaults`
# array -- the per-vault detail (including `integrity`) lives at
# vaults[0].integrity, NOT at a top-level `integrity` key (that key is only a
# rolled-up {vaults, writer_fenced, failed_or_incomplete} COUNT). This
# disagrees with the M2b facts sheet's assumption of a single-vault-scoped
# top-level `integrity`, so prefer the array form and fall back to the
# top-level shape for forward/backward compatibility. Fails the scenario
# (rather than silently printing an empty string) when neither shape yields a
# non-empty generation -- an empty comparison target would make every
# generation-equality assertion vacuously true.
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

# _verify_notes_still_present <stage-label>: kg query --format json, then
# assert the set of hit "path" values equals the baseline two-note set
# captured right after kg add. This is the "content check" the spec asks
# for at every "kg query still answers" checkpoint.
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
# kg init
# ---------------------------------------------------------------------------
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.stdout" 2>"$work_dir/init.stderr"
assert_exit_code 0 $?

# Explicit LLM disable (see the file header for the 0.0.48 rationale):
# deterministic model-free behavior regardless of any ambient endpoint.
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

# Seed files live OUTSIDE the vault, not under "$VAULT/notes/". kg add reads
# the given file and stages its OWN durable copy under
# <vault>/.marginalia/sources/<hash>/ (cli/__init__.py's `add` command posts
# {path, content} to the daemon; the daemon owns where it lands). kg rebuild's
# file discovery (_deterministic_rebuild_files, cli/kg.py) deliberately walks
# BOTH notes/refs AND .marginalia/sources/** -- by design, "real vaults keep
# notes/refs empty and their whole corpus" in the staged sources tree. Writing
# the same content directly into "$VAULT/notes/" as well would give rebuild
# TWO distinct on-disk copies of the same logical note and it would (correctly,
# by its own documented contract) re-extract both -- an artifact of a naive
# seeding choice, not a curation-verb bug. Seeding from outside the vault keeps
# kg add's staged copy the ONE source rebuild ever sees, matching how a real
# vault is actually populated.
SEED_DIR="$work_dir/seed_notes"
mkdir -p "$SEED_DIR"
SEED1="$SEED_DIR/curation-alpha.md"
SEED2="$SEED_DIR/curation-beta.md"

cat > "$SEED1" <<EOF
# Curation Alpha
$QUERY_TEXT note Alpha covers desert canyon survey logs.
EOF
cat > "$SEED2" <<EOF
# Curation Beta
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

# ---------------------------------------------------------------------------
# kg rebuild, TWICE (offline, model-free — the vault's LLM is explicitly
# disabled, so the verbs re-ingest from the markdown trust root on the
# deterministic structural path in every environment, LLM or no LLM).
# The second run is what gives rollback (below) a real checkpoint to target —
# see the file header comment for why one rebuild is structurally never
# enough.
# ---------------------------------------------------------------------------
log "kg rebuild $VAULT (1st)"
kg rebuild "$VAULT" >"$work_dir/rebuild1.stdout" 2>"$work_dir/rebuild1.stderr"
rebuild1_rc=$?
assert_exit_code 0 "$rebuild1_rc"
if [[ "$rebuild1_rc" -ne 0 ]]; then tail -40 "$work_dir/rebuild1.stderr" >&2; fi
assert_graph_populated "$VAULT" "after_rebuild1"

log "kg rebuild $VAULT (2nd)"
kg rebuild "$VAULT" >"$work_dir/rebuild2.stdout" 2>"$work_dir/rebuild2.stderr"
rebuild2_rc=$?
assert_exit_code 0 "$rebuild2_rc"
if [[ "$rebuild2_rc" -ne 0 ]]; then tail -40 "$work_dir/rebuild2.stderr" >&2; fi
assert_graph_populated "$VAULT" "after_rebuild2"

# Cycle 2 (server up): "kg query still answers" checkpoint #1, right after
# rebuild; capture the pre-rollback generation; then the REST rollback job,
# which should revert to the FIRST rebuild's generation.
if ! start_server "$VAULT"; then
  _failures+=("server_start_failed phase=after_rebuild")
  finish
fi
_verify_notes_still_present "after_rebuild"

pre_rollback_generation="$(_curation_generation "before_rollback")"
log "graph_generation after 2nd rebuild (pre-rollback): $pre_rollback_generation"

log "POST /api/v1/curation/rollback"
rollback_status="$(python3 - "$OKTO_NEURON_ENDPOINT" "$work_dir/rollback_post.json" <<'PY'
import json
import sys
import urllib.error
import urllib.request

endpoint, out_path = sys.argv[1:]
url = endpoint.rstrip("/") + "/api/v1/curation/rollback"
# The write-safety middleware (server/http.py ContentTypeGuard) 415s any
# unsafe-method request that does not declare Content-Type: application/json,
# even with an empty body -- api_curation_rollback itself never reads the
# body (the generation is server-derived, not client-supplied), but the
# header is still required to pass the guard. NOTE: no apostrophes in this
# heredoc body -- it is nested inside $(...), and macOS system bash (3.2)
# mis-parses a quoted heredoc closing delimiter when the body contains one.
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
PY
)"
log "rollback POST status: $rollback_status (body: $(cat "$work_dir/rollback_post.json"))"

if [[ "$rollback_status" != "202" ]]; then
  _failures+=("rollback_post_not_accepted status=$rollback_status")
  log "FAIL POST rollback did not return 202: $rollback_status"
else
  log "rollback job accepted; polling GET /api/v1/curation/rebuild/status?kind=rollback"
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

  if [[ "$job_final_status" == "done" ]]; then
    log "rollback job done; verifying generation reverted to the 1st rebuild"
    post_rollback_generation="$(_curation_generation "after_rollback")"
    log "graph_generation after rollback: $post_rollback_generation"
    if [[ "$post_rollback_generation" == "$pre_rollback_generation" ]]; then
      _failures+=("rollback_generation_unchanged generation=$post_rollback_generation")
      log "FAIL rollback did not change the live generation"
    fi
    _verify_notes_still_present "after_rollback"
  elif [[ "$job_final_status" == "error" ]]; then
    job_error="$(python3 -c "
import json, sys
with open(sys.argv[1], encoding=\"utf-8\") as fh:
    d = json.load(fh)
print((d.get(\"last_rollback\") or {}).get(\"error\") or \"\")
" "$work_dir/rollback_poll.json")"
    _failures+=("rollback_job_errored error=$job_error")
    log "FAIL rollback job reported status=error: $job_error"
  else
    _failures+=("rollback_job_poll_timeout")
    log "FAIL rollback job did not reach done/error within 60s"
  fi
fi

stop_server

# ---------------------------------------------------------------------------
# kg reconcile heal (offline, model-free — deterministic canonicalizing copy
# + atomic swap; no confirmed authority equivalences needed to exercise it).
# ---------------------------------------------------------------------------
log "kg reconcile heal $VAULT"
kg reconcile heal "$VAULT" >"$work_dir/heal.stdout" 2>"$work_dir/heal.stderr"
heal_rc=$?
assert_exit_code 0 "$heal_rc"
if [[ "$heal_rc" -ne 0 ]]; then tail -40 "$work_dir/heal.stderr" >&2; fi

# Cycle 3 (server up): capture the generation right before reembed runs, and
# confirm the heal did not disturb query results. No query checkpoint is
# mandated here by the spec (only after rebuild and after reembed), but
# heal changes graph topology (a canonicalizing copy) so checking is cheap
# insurance, not scope creep.
if ! start_server "$VAULT"; then
  _failures+=("server_start_failed phase=after_heal")
  finish
fi
_verify_notes_still_present "after_heal"
pre_reembed_generation="$(_curation_generation "after_heal")"
log "graph_generation after heal (pre-reembed): $pre_reembed_generation"
stop_server

# ---------------------------------------------------------------------------
# kg reembed (offline, model-free — vectors-only, no re-extraction).
# ---------------------------------------------------------------------------
log "kg reembed $VAULT"
kg reembed "$VAULT" >"$work_dir/reembed.stdout" 2>"$work_dir/reembed.stderr"
reembed_rc=$?
assert_exit_code 0 "$reembed_rc"
if [[ "$reembed_rc" -ne 0 ]]; then tail -40 "$work_dir/reembed.stderr" >&2; fi

# ---------------------------------------------------------------------------
# Cycle 4 (server up, final): "kg query still answers" checkpoint #2.
# ---------------------------------------------------------------------------
if ! start_server "$VAULT"; then
  _failures+=("server_start_failed phase=after_reembed")
  finish
fi
_verify_notes_still_present "after_reembed"
final_generation="$(_curation_generation "after_reembed")"
log "graph_generation current (post-reembed): $final_generation"
stop_server

finish
