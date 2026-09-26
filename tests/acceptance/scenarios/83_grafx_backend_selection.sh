#!/usr/bin/env bash
# Scenario 83: the Grafx backend (M4, now the default per D-94) exercised
# through the SAME lifecycle 98_curation_verbs.sh already proves for
# Ladybug — deprecated-but-still-accepted `--accept-experimental` coverage
# (D-94 retired the D-12 onboarding-consent gate: Grafx init succeeds with
# or without the flag now, and this scenario proves both), kg add /
# kg query, kg rebuild (x2) / kg reconcile heal / kg reembed, and the REST
# rollback job — plus the M3-style backend-selection init assertions from
# 99_backend_selection.sh, all pinned to `--backend grafx` instead of the
# default. This scenario intentionally runs regardless of the harness
# `--backend` flag (bin/acceptance.sh --backend NAME): it is the one place
# Grafx parity is proven end to end, so it always targets grafx.
#
# Verb order mirrors 98 (rebuild x2, then heal, then reembed, then REST
# rollback with the server up), placing the REST rollback attempt AFTER
# heal + reembed (matching the M4 spec's requested verb order) rather than
# 98's placement immediately after the 2nd rebuild.
#
# Historically (pre-D-84) this position 409'd unconditionally: the REST gate
# (`server/http.py`'s `api_curation_rollback`) checked ONLY for Ladybug's
# generation-keyed `rebuild-artifacts/<generation>/previous-graph.lbug`,
# which neither heal nor reembed ever writes on any backend, AND it 409'd
# even at 98's own placement for Grafx specifically -- `cli/kg.py`'s
# `_prepare_rebuild_backup` always computed the checkpoint's graph-backup
# path as the LITERAL, Ladybug-only `previous-graph.lbug`, while Grafx's
# REAL rebuild backup lands at `<vault>/graph.grafx.rebuild`, a sibling of
# the live graph directory the gate's file-existence check could never see.
#
# D-84 (backend-neutral rollback availability) closed both gaps: the REST
# gate now defers to `marginalia.curation.orchestrate.rollback_candidate`,
# which asks each backend for ITS OWN durable "previous generation" evidence
# -- for Grafx, the newest sibling backup directory (`graph.grafx.rebuild`
# or `graph.grafx.bak`) the last swap produced, exactly what the daemon
# rollback runner (`_run_rollback_non_ladybug`) already restored from. Since
# reembed (the verb immediately before this rollback attempt) leaves
# `graph.grafx.bak` behind, this scenario now expects and asserts a REAL
# 202 + completed rollback + reverted generation here, not a 409 -- see the
# case statement below.
set -uo pipefail
SCENARIO_NAME="83_grafx_backend_selection"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

# ---------------------------------------------------------------------------
# grafx and ladybug are BASE package dependencies now (D-94 step 1), so
# _preamble.sh's own default sync already installs okto-grafx regardless of
# the harness's --backend flag -- no additive [grafx] extra sync is needed
# here any more. This scenario still drives `kg rebuild` (offline,
# model-free -- see the "kg rebuild, TWICE" section below for the full
# rationale). The vault under test EXPLICITLY disables the LLM (written
# into okto-neuron.yaml right after init, below): since 0.0.48 the
# discovery-first built-in default carries an EMPTY model, so an ambient
# live endpoint at the default loopback base URL (127.0.0.1:8123) rejects
# it with a 500 -- and the rebuild audit (RebuildAuditFailed) fails on
# provider-failed files. Whether the host runs such an endpoint is
# environmental noise this scenario must not depend on: with llm.enabled: false
# the offline verbs take the deterministic structural-ingest +
# extraction-skip path in EVERY environment. `litellm` still needs to be
# IMPORTABLE (the pipeline's provider plumbing is litellm-backed
# unconditionally), so without the package the offline verbs crash on
# ModuleNotFoundError. `uv sync` is EXACT by
# default (it prunes any package outside the exact extras/groups requested
# on THIS invocation -- confirmed empirically), so this additive sync must
# still repeat the `--extra neo4j` _preamble.sh may have already installed
# for the harness-pinned backend, exactly like scenario 98's own additive
# `--group litellm` sync does, or a bare `--group litellm` sync here would
# prune it.
uv_sync_litellm_args=(--quiet --python 3.12 --group litellm)
if [[ "${OKTO_NEURON_ACCEPTANCE_BACKEND:-}" == "neo4j" ]]; then
  uv_sync_litellm_args+=(--extra neo4j)
fi
if ! (
  cd "$REPO_ROOT" || exit 1
  uv sync "${uv_sync_litellm_args[@]}"
) >"$work_dir/uv-sync-grafx.log" 2>&1; then
  log "uv sync --group litellm failed; see $work_dir/uv-sync-grafx.log"
  _failures+=("uv_sync_grafx_failed")
  finish
fi
unset uv_sync_litellm_args

VAULT="$work_dir/vault"
QUERY_TEXT="Okto Neuron grafx backend selection acceptance fixture"
BASELINE_PATHS_FILE="$work_dir/baseline_paths.json"

# ---------------------------------------------------------------------------
# Helpers local to this scenario (adapted from 98_curation_verbs.sh).
# ---------------------------------------------------------------------------

# _curation_generation <stage-label>: print the vault's current
# graph_generation via GET /api/v1/status (per-vault detail lives at
# vaults[0].integrity, not a top-level `integrity` key -- see 98's own
# comment; verified again here against a grafx-pinned vault).
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

# _verify_notes_still_present <stage-label>: kg query --format json (the
# same REST query route 98 drives through the CLI), then assert the set of
# hit "path" values equals the baseline two-note set captured right after
# kg add.
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

# _assert_grafx_graph_populated <stage-label>: Grafx's live graph is a
# directory (`graph.grafx/`), not a single file, so 98's
# `assert_min_bytes "$VAULT/graph.lbug" 100000` (a plain-file byte count)
# does not translate directly. Assert the directory exists AND holds a
# non-trivial number of real files instead -- content, not allocation.
_assert_grafx_graph_populated() {
  local stage="$1"
  assert_file_exists "$VAULT/graph.grafx"
  local count
  count="$(find "$VAULT/graph.grafx" -type f 2>/dev/null | wc -l | tr -d ' ')"
  assert_min_count "$count" 5 "grafx_graph_file_count[$stage]"
}

# ---------------------------------------------------------------------------
# Part 1: `okto-neuron init --backend grafx` WITHOUT --accept-experimental ->
# succeeds; okto-neuron.yaml pins grafx. D-94 retired the D-12 onboarding
# consent gate outright (Grafx is the default, non-experimental backend now,
# per the owner decision "Make grafx not experimental anymore" /
# "Okto Grafx becomes the DEFAULT graph backend"), so this is no longer the
# "no consent -> exit 1" case the M4 spec's OQ6 originally asked for.
# ---------------------------------------------------------------------------
log "okto-neuron init $VAULT --backend grafx (no --accept-experimental)"
okto-neuron init "$VAULT" --backend grafx \
  >"$work_dir/init.stdout" 2>"$work_dir/init.stderr"
assert_exit_code 0 $?
assert_file_exists "$VAULT/okto-neuron.yaml"
assert_contains "$VAULT/okto-neuron.yaml" '^storage:$'
assert_contains "$VAULT/okto-neuron.yaml" '^  backend: grafx$'
assert_file_exists "$VAULT/graph.grafx"

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
# Part 2: the deprecated `--accept-experimental` flag is still accepted (CLI
# help text: "Deprecated, no longer required; kept for compatibility") --
# an old script that still passes it must keep working unmodified. Proven
# against a throwaway second vault so it doesn't disturb the one the rest of
# this scenario's lifecycle (seed/rebuild/heal/reembed/rollback) uses.
# ---------------------------------------------------------------------------
VAULT_DEPRECATED_FLAG="$work_dir/vault_deprecated_flag"
log "okto-neuron init $VAULT_DEPRECATED_FLAG --backend grafx --accept-experimental (deprecated flag, still accepted)"
okto-neuron init "$VAULT_DEPRECATED_FLAG" --backend grafx --accept-experimental \
  >"$work_dir/init_deprecated_flag.stdout" 2>"$work_dir/init_deprecated_flag.stderr"
assert_exit_code 0 $?
assert_file_exists "$VAULT_DEPRECATED_FLAG/okto-neuron.yaml"
assert_contains "$VAULT_DEPRECATED_FLAG/okto-neuron.yaml" '^storage:$'
assert_contains "$VAULT_DEPRECATED_FLAG/okto-neuron.yaml" '^  backend: grafx$'
assert_file_exists "$VAULT_DEPRECATED_FLAG/graph.grafx"

# ---------------------------------------------------------------------------
# Cycle 1 (server up): seed two notes via kg add, capture the baseline
# two-note query result, then note that this scenario has no shared MCP
# helper to reuse.
# ---------------------------------------------------------------------------
if ! start_server "$VAULT"; then
  _failures+=("server_start_failed phase=seed")
  finish
fi

# Seed files live OUTSIDE the vault, same rationale as 98: kg add stages its
# own durable copy under <vault>/.marginalia/sources/<hash>/, which is the
# ONE source kg rebuild's file discovery should ever see for these notes.
SEED_DIR="$work_dir/seed_notes"
mkdir -p "$SEED_DIR"
SEED1="$SEED_DIR/grafx-alpha.md"
SEED2="$SEED_DIR/grafx-beta.md"

cat > "$SEED1" <<EOF
# Grafx Alpha
$QUERY_TEXT note Alpha covers desert canyon survey logs.
EOF
cat > "$SEED2" <<EOF
# Grafx Beta
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

log "kg query --format json (baseline) -- the REST query route 98 uses"
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

# MCP round trip: there is no shared _lib.sh/_preamble.sh MCP client helper
# to reuse (every existing MCP scenario -- e.g. 82_mcp_provenance_roundtrip.sh
# -- embeds its own bespoke fastmcp.Client python heredoc rather than calling
# a shared function), so per this scenario's own scope this step is a no-op
# skip rather than a freshly-authored MCP client. The REST/CLI query path
# above (and again after every curation verb below) already exercises the
# same shared graph an MCP client would read.
log "MCP round trip skipped: no shared MCP helper exists in _lib.sh/_preamble.sh to reuse (see header note); REST/CLI query coverage below stands in for it"

stop_server

# ---------------------------------------------------------------------------
# kg rebuild, TWICE (offline, model-free -- the vault's LLM is explicitly
# disabled, so the verbs re-ingest from the markdown trust root on the
# deterministic structural path in every environment, same as 98).
# ---------------------------------------------------------------------------
log "kg rebuild $VAULT (1st)"
kg rebuild "$VAULT" >"$work_dir/rebuild1.stdout" 2>"$work_dir/rebuild1.stderr"
rebuild1_rc=$?
assert_exit_code 0 "$rebuild1_rc"
if [[ "$rebuild1_rc" -ne 0 ]]; then tail -40 "$work_dir/rebuild1.stderr" >&2; fi
_assert_grafx_graph_populated "after_rebuild1"

log "kg rebuild $VAULT (2nd)"
kg rebuild "$VAULT" >"$work_dir/rebuild2.stdout" 2>"$work_dir/rebuild2.stderr"
rebuild2_rc=$?
assert_exit_code 0 "$rebuild2_rc"
if [[ "$rebuild2_rc" -ne 0 ]]; then tail -40 "$work_dir/rebuild2.stderr" >&2; fi
_assert_grafx_graph_populated "after_rebuild2"

# Cycle 2 (server up): "kg query still answers" checkpoint right after
# rebuild.
if ! start_server "$VAULT"; then
  _failures+=("server_start_failed phase=after_rebuild")
  finish
fi
_verify_notes_still_present "after_rebuild"
stop_server

# ---------------------------------------------------------------------------
# kg reconcile heal (offline, model-free -- deterministic canonicalizing copy
# + atomic swap; no confirmed authority equivalences needed to exercise it).
# ---------------------------------------------------------------------------
log "kg reconcile heal $VAULT"
kg reconcile heal "$VAULT" >"$work_dir/heal.stdout" 2>"$work_dir/heal.stderr"
heal_rc=$?
assert_exit_code 0 "$heal_rc"
if [[ "$heal_rc" -ne 0 ]]; then tail -40 "$work_dir/heal.stderr" >&2; fi
_assert_grafx_graph_populated "after_heal"

# Cycle 3 (server up): confirm heal did not disturb query results.
if ! start_server "$VAULT"; then
  _failures+=("server_start_failed phase=after_heal")
  finish
fi
_verify_notes_still_present "after_heal"
stop_server

# ---------------------------------------------------------------------------
# kg reembed (offline, model-free -- vectors-only, no re-extraction).
# ---------------------------------------------------------------------------
log "kg reembed $VAULT"
kg reembed "$VAULT" >"$work_dir/reembed.stdout" 2>"$work_dir/reembed.stderr"
reembed_rc=$?
assert_exit_code 0 "$reembed_rc"
if [[ "$reembed_rc" -ne 0 ]]; then tail -40 "$work_dir/reembed.stderr" >&2; fi
_assert_grafx_graph_populated "after_reembed"

# ---------------------------------------------------------------------------
# Cycle 4 (server up, final): "kg query still answers" checkpoint #2, then
# the REST rollback attempt. See the file header (D-84) for why 202 + a
# completed, reverted-generation rollback is the correct, expected outcome
# here: reembed's `graph.grafx.bak` backup is real rollback evidence, and
# the backend-neutral REST gate now sees it.
# ---------------------------------------------------------------------------
if ! start_server "$VAULT"; then
  _failures+=("server_start_failed phase=after_reembed")
  finish
fi
_verify_notes_still_present "after_reembed"

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
# ContentTypeGuard (server/http.py) 415s any unsafe-method request without a
# declared Content-Type, even with an empty body -- api_curation_rollback
# itself never reads the body (the generation is server-derived). No
# apostrophes in this heredoc body: it is nested inside a here-doc read by
# `python3 -`, and macOS system bash (3.2) mis-parses a quoted heredoc
# closing delimiter that contains one.
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
# status and error land on two separate stdout lines -- the caller reads
# them with `sed -n`, no fragile in-shell delimiter splitting.
print(status)
print(body.get("error", "") if isinstance(body, dict) else "")
PY
rollback_status="$(sed -n '1p' "$work_dir/rollback_post.status")"
rollback_error="$(sed -n '2p' "$work_dir/rollback_post.status")"
log "rollback POST status: $rollback_status error=$rollback_error (body: $(cat "$work_dir/rollback_post.json"))"

case "$rollback_status/$rollback_error" in
  202/*)
    log "rollback was ACCEPTED (202) -- backend-neutral rollback availability (D-84) closed the documented Ladybug-only checkpoint gap this scenario used to route around. Polling GET /api/v1/curation/rebuild/status?kind=rollback to confirm it actually completed and reverted the generation, rather than assuming success from the accept alone."
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
      log "rollback completed and reverted the generation ($pre_rollback_generation -> $post_rollback_generation)"
    else
      _failures+=("rollback_accepted_but_did_not_complete job_status=${job_final_status:-timeout} before=$pre_rollback_generation after=$post_rollback_generation")
      log "FAIL rollback was accepted (202) but did not cleanly complete: job_status=${job_final_status:-timeout}"
    fi
    ;;
  *)
    _failures+=("rollback_response_unexpected status=$rollback_status error=$rollback_error")
    log "FAIL unexpected rollback response: status=$rollback_status error=$rollback_error (expected 202 -- backend-neutral rollback availability (D-84) means grafx must now be accepted just like ladybug)"
    ;;
esac

_verify_notes_still_present "after_rollback_attempt"
stop_server

finish
