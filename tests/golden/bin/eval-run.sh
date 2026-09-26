#!/usr/bin/env bash
# ============================================================================
# eval-run.sh — THE single resumable eval orchestrator.
#
# One command runs the whole chain and is RESUMABLE: each step writes a named
# artifact into a STABLE run dir (keyed by dataset + --id, NOT a timestamp), and
# on re-run a step whose artifact already exists is SKIPPED. Delete an artifact
# (or pass --force) to re-do just that step.
#
# THE CHAIN (in order):
#   1. manifest      reproducibility pin (corpus-hash + embedder + git + knobs)   [deterministic]
#   2. floor         CI provenance byte-hash gate over the dataset's quotes        [deterministic]
#   3. recall+ask    drive the live daemon over HTTP -> responses.jsonl            [LLM/daemon]
#   4. semantic      server-owned semantic report + measured recall-cost evidence  [daemon]
#   5. judge         scripted single-LLM verdicts (or --panel for the N-judge panel)[LLM]
#   6. floor-laptop  citation byte-verify + extraction-completeness vs live vault  [LLM/daemon]
#   7. scorecard     paired A/B significance vs a baseline arm (optional)          [stats]
#   8. report        merge everything into report.json                            [deterministic]
#
# Steps 1-2 (+8 if responses exist) are DETERMINISTIC — no daemon, no LLM — and
# are the CI-provable legs. Steps 3-7 need the live daemon + its LLM and are
# LAPTOP-ONLY. The judge/panel verdicts feed the scorecard but the JUDGE REMAINS
# NON-AUTHORITATIVE pending the human kappa; the deterministic legs do not.
#
# MODES:
#   eval-run.sh <dataset-name-or-path> --ci
#       deterministic legs only (manifest + floor). No daemon, no LLM. The path CI
#       runs and the one to prove from scratch + show resumability.
#
#   eval-run.sh <dataset-name-or-path> --endpoint http://127.0.0.1:7777 [--bound F] [--panel] \
#               [--baseline ARM.jsonl] [--limit N]
#       full pipeline as a pure client of an already-running daemon.
#
# Exit codes: 0 = chain completed (answer QUALITY lives in the artifacts, not the
# exit code); non-zero = a hard gate failed (floor provenance) or infra error.
#
# Artifacts (gitignored), under tests/golden/results/<dataset>/<run-id>-<dataset-fingerprint>/:
#   run-manifest.json  floor_report.json  responses.jsonl  node-types.json
#   semantic-quality.json  judge.json | panel.json  floor_laptop.json
#   scorecard.txt  report.json
#   eval-run.log  STATUS
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GOLDEN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$GOLDEN_DIR/../.." && pwd)"
JUDGE_PY="$SCRIPT_DIR/judge.py"

USAGE="usage: eval-run.sh <dataset-name-or-path> [--id NAME] [--ci] [--endpoint URL] [--vault-path PATH] [--never-ingest|--force-reingest] [--bound FILE]
                  [--panel] [--no-judge] [--baseline ARM.jsonl] [--grader proxy|judge]
                  [--limit N] [--force] [--free-variable NAME] [--questions FILE]
                  [--ab-subgraph [--judge-model NAME] [--partial-as-correct]]"

DATASET="${1:-}"
if [[ -z "$DATASET" || "$DATASET" == --* ]]; then echo "$USAGE" >&2; exit 64; fi
shift || true

RUN_ID="eval-run"
CI_ONLY=0
ENDPOINT=""
ENDPOINT_VAULT_PATH=""
BOUND=""
USE_PANEL=0
RUN_JUDGE=1
BASELINE=""
GRADER="proxy"
LIMIT=""
FORCE=0
FREE_VAR=""
AB_SUBGRAPH=0        # A/B: subgraph vs block arms on ONE daemon+graph
QUESTIONS_OVERRIDE="" # optional out-of-dataset questions.yaml
JUDGE_MODEL="${OKTO_NEURON_JUDGE_MODEL:-unsloth/Qwen3.6-27B-NVFP4}"
PARTIAL_AS_CORRECT=0
NEVER_INGEST=0
FORCE_REINGEST=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --id) shift; RUN_ID="${1:-eval-run}" ;;
    --ci) CI_ONLY=1 ;;
    --endpoint) shift; ENDPOINT="${1:-}" ;;
    --vault-path) shift; ENDPOINT_VAULT_PATH="${1:-}"; [[ -n "$ENDPOINT_VAULT_PATH" ]] || { echo "--vault-path requires a path" >&2; exit 64; } ;;
    --never-ingest) NEVER_INGEST=1 ;;
    --force-reingest) FORCE_REINGEST=1 ;;
    --bound) shift; BOUND="${1:-}" ;;
    --panel) USE_PANEL=1 ;;
    --no-judge) RUN_JUDGE=0 ;;
    --baseline) shift; BASELINE="${1:-}" ;;
    --grader) shift; GRADER="${1:-proxy}" ;;
    --limit) shift; LIMIT="${1:-}" ;;
    --force) FORCE=1 ;;
    --free-variable) shift; FREE_VAR="${1:-}" ;;
    --ab-subgraph) AB_SUBGRAPH=1 ;;
    --questions) shift; QUESTIONS_OVERRIDE="${1:-}" ;;
    --judge-model) shift; JUDGE_MODEL="${1:-auto}" ;;
    --partial-as-correct) PARTIAL_AS_CORRECT=1 ;;
    *) echo "unknown flag: $1" >&2; echo "$USAGE" >&2; exit 64 ;;
  esac
  shift
done

if [[ "$NEVER_INGEST" == "1" && -z "$ENDPOINT" ]]; then
  echo "--never-ingest requires --endpoint" >&2
  exit 64
fi
if [[ "$FORCE_REINGEST" == "1" && -z "$ENDPOINT" ]]; then
  echo "--force-reingest requires --endpoint" >&2
  exit 64
fi
if [[ "$FORCE_REINGEST" == "1" && -z "$ENDPOINT_VAULT_PATH" ]]; then
  echo "--force-reingest requires --vault-path" >&2
  exit 64
fi
if [[ "$FORCE_REINGEST" == "1" && "$NEVER_INGEST" == "1" ]]; then
  echo "--force-reingest and --never-ingest are mutually exclusive" >&2
  exit 64
fi
if [[ -n "$ENDPOINT_VAULT_PATH" && -z "$ENDPOINT" ]]; then
  echo "--vault-path requires --endpoint" >&2
  exit 64
fi
if [[ -n "$ENDPOINT_VAULT_PATH" ]]; then
  export OKTO_NEURON_VAULT_PATH="$ENDPOINT_VAULT_PATH"
else
  unset OKTO_NEURON_VAULT_PATH
fi

# shellcheck source=tests/golden/bin/_dataset_dir.sh
source "$SCRIPT_DIR/_dataset_dir.sh"
DATASET_DIR="$(resolve_dataset_dir "$DATASET" "$GOLDEN_DIR")" || exit 64
DATASET="$(basename "$DATASET_DIR")"
INPUTS_DIR="$DATASET_DIR/inputs"
QUESTIONS="$DATASET_DIR/questions.yaml"
# --questions drives an out-of-dataset set through
# the SAME graph; both the ask arms and the semantic judge read it.
[[ -n "$QUESTIONS_OVERRIDE" ]] && QUESTIONS="$QUESTIONS_OVERRIDE"
[[ -d "$DATASET_DIR" ]] || { echo "no dataset: $DATASET_DIR" >&2; exit 64; }
[[ -d "$INPUTS_DIR" ]]  || { echo "no inputs/: $INPUTS_DIR" >&2; exit 64; }
[[ -f "$QUESTIONS" ]]   || { echo "no questions.yaml: $QUESTIONS" >&2; exit 64; }

DATASET_FINGERPRINT="$(python3 - "$INPUTS_DIR" "$QUESTIONS" <<'PY'
import hashlib, pathlib, sys
root, questions = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
h = hashlib.sha256()
for path in sorted((p for p in root.rglob('*') if p.is_file()),
                   key=lambda p: p.relative_to(root).as_posix()):
    h.update(path.relative_to(root).as_posix().encode())
    h.update(b'\0')
    h.update(path.read_bytes())
    h.update(b'\0')
h.update(b'questions\0')
h.update(questions.read_bytes())
print(h.hexdigest())
PY
)"
[[ "$DATASET_FINGERPRINT" =~ ^[0-9a-f]{64}$ ]] \
  || { echo "could not fingerprint dataset" >&2; exit 70; }
RUN_DIR="$GOLDEN_DIR/results/$DATASET/${RUN_ID}-${DATASET_FINGERPRINT:0:12}"
mkdir -p "$RUN_DIR"
LOG="$RUN_DIR/eval-run.log"
STATUS="$RUN_DIR/STATUS"
: > "$STATUS"

log() { printf '[eval-run:%s/%s] %s\n' "$DATASET" "$RUN_ID" "$*" | tee -a "$LOG" >&2; }
mark() { printf '%s\t%s\n' "$1" "$2" >> "$STATUS"; }   # step<TAB>done|skipped|failed
# GN-8: explicit Definition-of-Done gate lines (see docs/eval-gates.md). The three
# gates of the graph-native arc: (1) deterministic floor [the only CI gate],
# (2) multi-run A/B significance [laptop-only], (3) cost budget [tier tag].
gate() { printf '[gate] %s\n' "$*" | tee -a "$LOG" >&2; }

command -v uv >/dev/null 2>&1 || { echo "uv not found" >&2; exit 70; }
( cd "$REPO_ROOT" && uv sync --quiet --group litellm ) \
  || { echo "uv sync --group litellm failed" >&2; exit 70; }
# Keep the same dependency closure on every uv invocation.  A plain `uv run`
# exact-syncs only the default groups and would prune LiteLLM again after the
# explicit sync above, potentially breaking the shared endpoint daemon on its
# next lazy provider import.
PY() { ( cd "$REPO_ROOT" && uv run --group litellm python3 "$JUDGE_PY" "$@" ); }
semantic_quality_valid() {  # semantic_quality_valid <sidecar> <responses>
  [[ -s "$1" ]] && PY validate-semantic-quality \
    --semantic-quality "$1" --responses "$2" >/dev/null 2>&1
}
semantic_judge_valid() {  # semantic_judge_valid <sidecar> <responses> [limit]
  local -a args=(validate-report --judge "$1" --responses "$2")
  [[ -n "${3:-}" ]] && args+=(--limit "$3")
  [[ -s "$1" ]] && ( cd "$REPO_ROOT" && uv run --group litellm python3 \
    "$SCRIPT_DIR/semantic_judge.py" "${args[@]}" >/dev/null 2>&1 )
}
scripted_judge_valid() {  # scripted_judge_valid <sidecar> <responses>
  [[ -s "$1" ]] && PY validate-judge --judge "$1" --responses "$2" >/dev/null 2>&1
}
panel_valid() {  # panel_valid <sidecar> <responses> [limit]
  local -a args=(validate-report --panel "$1" --responses "$2")
  [[ -n "${3:-}" ]] && args+=(--limit "$3")
  [[ -s "$1" ]] && ( cd "$REPO_ROOT" && uv run --group litellm python3 \
    "$SCRIPT_DIR/archive/panel.py" "${args[@]}" >/dev/null 2>&1 )
}
floor_laptop_valid() {  # floor_laptop_valid <sidecar> <responses>
  [[ -s "$1" ]] && ( cd "$REPO_ROOT" && uv run --group litellm python3 \
    "$SCRIPT_DIR/floor_metrics.py" validate-floor-laptop \
    --report "$1" --responses "$2" >/dev/null 2>&1 )
}
golden_artifact_dir() {  # latest run-golden artifact dir recorded in this run's log
  awk -v marker="[golden:$DATASET] artifacts: " \
    'index($0, marker) == 1 { print substr($0, length(marker) + 1) }' "$LOG" | tail -1
}
golden_responses_complete() {  # golden_responses_complete <dir>
  [[ -s "$1/responses.jsonl" && -s "$1/responses.complete" ]] || return 1
  local expected actual
  expected="$(tr -d '[:space:]' < "$1/responses.complete")"
  actual="$(wc -l < "$1/responses.jsonl" | tr -d '[:space:]')"
  [[ "$expected" =~ ^[1-9][0-9]*$ && "$actual" == "$expected" ]]
}
dataset_floor_valid() {  # dataset_floor_valid <sidecar> <allow-failed-bool:0|1>
  [[ -s "$1" ]] && python3 - "$1" "$2" <<'PY'
import json, sys
try:
    value = json.load(open(sys.argv[1])).get('provenance_gate_pass')
except Exception:
    raise SystemExit(1)
ok = isinstance(value, bool) if sys.argv[2] == '1' else value is True
raise SystemExit(0 if ok else 1)
PY
}

# resumable guard: returns 0 (skip) if the artifact exists and --force not set.
done_already() {  # done_already <artifact> <step-name>
  if [[ "$FORCE" != "1" && -s "$1" ]]; then
    log "SKIP $2 (artifact exists: $(basename "$1"))"
    mark "$2" skipped
    return 0
  fi
  return 1
}

log "run dir: $RUN_DIR  (mode: $([[ "$CI_ONLY" == 1 ]] && echo CI-deterministic || echo full))"

# ── 1. MANIFEST (deterministic) ─────────────────────────────────────────────
MANIFEST_OUT="$RUN_DIR/run-manifest.json"
if ! done_already "$MANIFEST_OUT" manifest; then
  log "STEP manifest -> run-manifest.json"
  MF_ARGS=("manifest" "$DATASET_DIR" "--out" "$MANIFEST_OUT" "--repo" "$REPO_ROOT")
  [[ -n "$QUESTIONS_OVERRIDE" ]] && MF_ARGS+=("--questions" "$QUESTIONS_OVERRIDE")
  [[ -n "$ENDPOINT" ]] && MF_ARGS+=("--endpoint" "$ENDPOINT")
  [[ -n "$FREE_VAR" ]] && MF_ARGS+=("--free-variable" "$FREE_VAR")
  if PY "${MF_ARGS[@]}" >>"$LOG" 2>&1; then mark manifest "done"; else
    log "manifest FAILED (see $LOG)"; mark manifest failed; exit 73
  fi
fi

# Resumability must not bypass corpus identity.  When the shared endpoint is
# already populated, verify it now even if responses.jsonl would otherwise make
# the recall/ask step skip.  Empty endpoints are allowed here because the nested
# run-golden invocation will ingest and then verify them before asking questions.
ENDPOINT_DATASET_VERIFIED=0
if [[ -n "$ENDPOINT" ]]; then
  ENDPOINT_DOC_TOTAL="$(python3 - "$ENDPOINT" <<'PY'
import json, os, sys, urllib.request
url = sys.argv[1].rstrip('/') + '/api/v1/nodes?type=Document&limit=1&offset=0'
try:
    headers = {}
    token = os.environ.get('OKTO_NEURON_AUTH_TOKEN', '').strip()
    if token:
        headers['Authorization'] = f'Bearer {token}'
    vault_path = os.environ.get('OKTO_NEURON_VAULT_PATH', '').strip()
    if vault_path:
        headers['X-Okto-Neuron-Vault'] = vault_path
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=10) as response:
        print(int(json.load(response).get('total', 0)))
except Exception:
    print(-1)
PY
)"
  if [[ "$ENDPOINT_DOC_TOTAL" == "-1" ]]; then
    log "endpoint Document identity census failed"
    exit 70
  fi
  if (( ENDPOINT_DOC_TOTAL > 0 )); then
    ENDPOINT_IDENTITY_OUT="$RUN_DIR/dataset-identity.json"
    log "STEP endpoint identity -> $(basename "$ENDPOINT_IDENTITY_OUT")"
    if ! PY assert-endpoint-dataset "$DATASET_DIR" --endpoint "$ENDPOINT" \
        --out "$ENDPOINT_IDENTITY_OUT" >>"$LOG" 2>&1; then
      log "endpoint dataset identity MISMATCH or unverifiable; refusing to reuse populated graph"
      exit 74
    fi
    mark endpoint_identity "done"
    ENDPOINT_DATASET_VERIFIED=1
  fi
fi

# ── 2. FLOOR (deterministic CI gate) ────────────────────────────────────────
FLOOR_OUT="$RUN_DIR/floor_report.json"
if ! dataset_floor_valid "$FLOOR_OUT" "$AB_SUBGRAPH" || ! done_already "$FLOOR_OUT" floor; then
  log "STEP floor -> floor_report.json (provenance byte-hash gate)"
  FLOOR_ARGS=("floor" "$DATASET_DIR" "--out" "$FLOOR_OUT")
  [[ -n "$QUESTIONS_OVERRIDE" ]] && FLOOR_ARGS+=("--questions" "$QUESTIONS_OVERRIDE")
  if PY "${FLOOR_ARGS[@]}" >>"$LOG" 2>&1; then
    mark floor "done"
  else
    # In A/B mode the AUTHORITATIVE signal is the semantic scorecard, not the
    # dataset floor. The floor is dataset-hygiene: a single intrinsic ambiguous
    # gold quote in the owner's (unchanged) corpus must not abort the answer-quality
    # A/B. Elsewhere the floor stays a HARD gate (unchanged).
    if [[ "$AB_SUBGRAPH" == "1" ]]; then
      log "floor gate NOT fully clean (informational in --ab-subgraph; semantic scorecard is authoritative — see $LOG)"
      mark floor "informational"
    else
      log "floor GATE FAILED — a gold quote did not resolve uniquely (see $LOG)"
      mark floor failed; exit 1
    fi
  fi
fi
# surface the floor verdict regardless of skip/run
FLOOR_PASS="$(python3 -c "import json;print(json.load(open('$FLOOR_OUT'))['provenance_gate_pass'])" 2>/dev/null || echo "?")"
log "floor provenance_gate_pass=$FLOOR_PASS"
# GATE 1 — DETERMINISTIC FLOOR (the only CI gate; judge-free). This eval-run only
# runs the provenance leg; recall_floor.py gate (hard-recall + extraction +
# claim-coverage) is the rest of the same gate and is run by .github/workflows/eval-gate.yml.
if [[ "$FLOOR_PASS" == "True" ]]; then gate "deterministic-floor PASS"; else gate "deterministic-floor FAIL"; fi
if [[ "$AB_SUBGRAPH" != "1" && "$FLOOR_PASS" != "True" ]]; then
  log "floor GATE FAILED — retained floor evidence is not a pass"
  exit 1
fi

# GATE 3 — COST BUDGET (static tier tag pinned into the run manifest by manifest.py).
TIER_TAG="$(python3 -c "import json;m=json.load(open('$MANIFEST_OUT'));t=m.get('run_meta',{}).get('tier_cost',{});print('change-tier=%s change-requires-reingest=%s'%(t.get('tier','tier-1'),t.get('needs_reingest',False)))" 2>/dev/null || echo "change-tier=tier-1 change-requires-reingest=False")"
gate "cost-budget $TIER_TAG"

if [[ "$CI_ONLY" == "1" ]]; then
  log "CI mode: deterministic legs done (manifest + floor). Stopping before LLM legs."
  gate "A/B N/A (CI mode: no LLM answerer)"
  log "STATUS:"; cat "$STATUS" | sed 's/^/  /' >&2
  echo "$RUN_DIR"
  [[ "$FLOOR_PASS" == "True" ]] && exit 0 || exit 1
fi

# ── A/B: SUBGRAPH vs BLOCK arms on ONE daemon + ONE graph ────────────────────
# Both arms drive the SAME populated vault over --endpoint; they differ in EXACTLY
# one field — retrieval_policy.enable_subgraph (block=false, subgraph=true). Recall
# is identical across arms (subgraph only changes the ASK answering path); we grade
# ASK. Each arm is graded by the reference-guided SEMANTIC judge (primary) with the
# deterministic must_contain backstop (proxy) as a CI sanity column. The paired
# arm JSONLs feed scorecard.py for the per-arm + McNemar-ready paired scorecard.
if [[ "$AB_SUBGRAPH" == "1" ]]; then
  SEMJUDGE_PY="$SCRIPT_DIR/semantic_judge.py"
  LLM_BASE="${OKTO_NEURON_LLM_BASE_URL:-}"
  if [[ -z "${LLM_BASE//[[:space:]]/}" ]]; then
    log "--ab-subgraph requires OKTO_NEURON_LLM_BASE_URL for its live judge"
    exit 64
  fi
  if [[ -z "$ENDPOINT" ]]; then
    log "--ab-subgraph requires --endpoint (a running reference-eval daemon on its own port)"; exit 64
  fi
  if ! curl -fsS "$ENDPOINT/health" >/dev/null 2>&1; then
    log "daemon unreachable at $ENDPOINT/health — bring up the reference-eval daemon first"; exit 70
  fi
  # Pin ONE judge model for BOTH arms so the verdict isn't confounded by a lineup
  # shift mid-run (advisor). "auto" → resolve the largest chat model once, now.
  if [[ "$JUDGE_MODEL" == "auto" ]]; then
    RESOLVED="$(cd "$REPO_ROOT" && uv run --group litellm python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR')
import semantic_judge as s
try:
    print(s._select_chat_model('$LLM_BASE'))
except Exception:
    print('auto')
" 2>/dev/null || echo auto)"
    [[ -n "$RESOLVED" ]] && JUDGE_MODEL="$RESOLVED"
  fi
  log "A/B subgraph-vs-block: endpoint=$ENDPOINT questions=$QUESTIONS judge-model=$JUDGE_MODEL"
  gate "A/B mode: subgraph vs block on one daemon+graph (semantic judge primary)"

  PARTIAL_ARG=(); [[ "$PARTIAL_AS_CORRECT" == "1" ]] && PARTIAL_ARG=(--partial-as-correct)

  # run one arm: recall+ask via run-golden (with the arm's retrieval_policy) →
  # responses-<arm>.jsonl, then semantic judge → semjudge-<arm>.json → arm-<arm>.jsonl.
  run_arm() {  # run_arm <arm-label> <retrieval-policy-json>
    local arm="$1" policy="$2"
    local resp="$RUN_DIR/responses-$arm.jsonl"
    local quality="$RUN_DIR/semantic-quality-$arm.json"
    local sem="$RUN_DIR/semjudge-$arm.json"
    local armfile="$RUN_DIR/arm-$arm.jsonl"

    # -- ask leg (resumable) --
    if ! done_already "$resp" "recall_ask_$arm"; then
      log "STEP recall+ask [$arm] policy=$policy via run-golden.sh --endpoint $ENDPOINT"
      local rg_stdout rg_report rg_dir rg_status
      local -a rg_args
      rg_args=("$DATASET_DIR" --endpoint "$ENDPOINT" --no-judge --retrieval-policy "$policy")
      [[ "$NEVER_INGEST" == "1" ]] && rg_args+=(--never-ingest)
      [[ -n "$ENDPOINT_VAULT_PATH" ]] && rg_args+=(--vault-path "$ENDPOINT_VAULT_PATH")
      [[ -n "$QUESTIONS_OVERRIDE" ]] && rg_args+=(--questions "$QUESTIONS_OVERRIDE")
      rg_stdout="$("$SCRIPT_DIR/run-golden.sh" "${rg_args[@]}" 2>>"$LOG")"
      rg_status=$?
      rg_report="$(printf '%s\n' "$rg_stdout" | grep -E '/report\.json$' | tail -1)"
      if [[ -n "$rg_report" ]]; then
        rg_dir="$(dirname "$rg_report")"
      else
        rg_dir="$(golden_artifact_dir)"
      fi
      if [[ -n "$rg_dir" ]] && golden_responses_complete "$rg_dir"; then
        cp "$rg_dir/responses.jsonl" "$resp"
        [[ -s "$rg_dir/semantic-quality.json" ]] && \
          cp "$rg_dir/semantic-quality.json" "$quality"
        log "  [$arm] captured $(wc -l <"$resp" | tr -d ' ') responses -> $(basename "$resp")"
        mark "recall_ask_$arm" "done"
        if [[ "$rg_status" != "0" ]]; then
          log "run-golden [$arm] exited $rg_status after the complete response capture; preserving it and resuming from the first missing sidecar"
        fi
      else
        log "recall+ask [$arm] failed before a complete response capture (status=$rg_status; see $LOG)"
        mark "recall_ask_$arm" failed
        return 71
      fi
    fi

    # An older resumable arm may already have responses without the new sidecar.
    # Recompute only the server-owned aggregation from those immutable samples.
    if ! semantic_quality_valid "$quality" "$resp"; then
      if [[ "$ENDPOINT_DATASET_VERIFIED" != "1" ]]; then
        log "cannot regenerate semantic-quality [$arm]: endpoint corpus identity is not verified"
        mark "semantic_quality_$arm" failed
        return 74
      fi
      log "STEP semantic-quality [$arm] -> $(basename "$quality")"
      if PY semantic-quality --endpoint "$ENDPOINT" --responses "$resp" \
          --out "$quality" >>"$LOG" 2>&1; then
        mark "semantic_quality_$arm" "done"
      else
        log "semantic-quality [$arm] failed or incomplete (see $LOG)"
        mark "semantic_quality_$arm" failed
        return 73
      fi
    fi

    # -- semantic judge (primary) + backstop; resumable --
    if ! semantic_judge_valid "$sem" "$resp" "$LIMIT" \
        || ! done_already "$sem" "judge_$arm"; then
      log "STEP semantic-judge [$arm] (reference-guided; model=$JUDGE_MODEL) -> $(basename "$sem")"
      local -a sj_args
      sj_args=(judge-file --questions "$QUESTIONS" --responses "$resp" --out "$sem"
               --base-url "$LLM_BASE" --model "$JUDGE_MODEL")
      [[ -n "$LIMIT" ]] && sj_args+=(--limit "$LIMIT")
      if ( cd "$REPO_ROOT" && uv run --group litellm python3 \
          "$SEMJUDGE_PY" "${sj_args[@]}" ) >>"$LOG" 2>&1; then
        mark "judge_$arm" "done"
      else
        log "semantic-judge [$arm] failed or incomplete (see $LOG)"
        mark "judge_$arm" failed
        return 73
      fi
    fi

    # -- verdict → scorecard arm JSONL (always re-derive; cheap, no LLM) --
    if ( cd "$REPO_ROOT" && uv run --group litellm python3 "$SEMJUDGE_PY" to-arm \
          --judge "$sem" --out "$armfile" ${PARTIAL_ARG[@]+"${PARTIAL_ARG[@]}"} ) >>"$LOG" 2>&1; then
      mark "arm_$arm" "done"
      # per-arm tally line straight from the semantic judge report
      python3 -c "
import json,sys
r=json.load(open(sys.argv[1]))
t=r.get('tally',{})
print('[eval-run] arm=%s semantic tally: %s' % (sys.argv[2], json.dumps(t)))
" "$sem" "$arm" | tee -a "$LOG" >&2
    else
      log "to-arm [$arm] failed (see $LOG)"; mark "arm_$arm" failed; return 73
    fi
  }

  run_arm block '{"enable_subgraph": false}'
  ARM_RC=$?
  if [[ "$ARM_RC" -ne 0 ]]; then
    log "block arm failed before complete recall-cost evidence was retained"
    exit "$ARM_RC"
  fi
  run_arm subgraph '{"enable_subgraph": true}'
  ARM_RC=$?
  if [[ "$ARM_RC" -ne 0 ]]; then
    log "subgraph arm failed before complete recall-cost evidence was retained"
    exit "$ARM_RC"
  fi

  # ── DIVERGENCE SELF-VERIFY ── the two arms must actually take different answering
  # paths. The /ask response carries retrieval.mode ("block"|"subgraph"); if the
  # policy was silently dropped (e.g. a STALE global build that predates the
  # retrieval_policy field → 400 → run-golden's `|| echo '{}'` swallows it), BOTH
  # arms run the block path and the scorecard shows a fake delta≈0 "no improvement".
  # Catch that loudly instead of shipping a plausible-but-wrong null.
  RESP_BLOCK="$RUN_DIR/responses-block.jsonl"
  RESP_SUBGRAPH="$RUN_DIR/responses-subgraph.jsonl"
  if [[ -s "$RESP_BLOCK" && -s "$RESP_SUBGRAPH" ]]; then
    DIVERGE="$(python3 -c "
import json,sys
def modes(p):
    c={}
    for ln in open(p):
        ln=ln.strip()
        if not ln: continue
        m=((json.loads(ln).get('ask') or {}).get('retrieval') or {}).get('mode')
        c[m]=c.get(m,0)+1
    return c
b=modes(sys.argv[1]); s=modes(sys.argv[2])
# subgraph arm must show subgraph mode; block arm must NOT. Absent mode = old build.
ok = s.get('subgraph',0)>0 and b.get('subgraph',0)==0
print('%s|block_modes=%s|subgraph_modes=%s' % ('OK' if ok else 'DIVERGENCE_FAIL', json.dumps(b), json.dumps(s)))
" "$RESP_BLOCK" "$RESP_SUBGRAPH" 2>/dev/null || echo "DIVERGENCE_FAIL|error|error")"
    log "arm divergence: $DIVERGE"
    if [[ "$DIVERGE" == OK\|* ]]; then
      gate "A/B arms diverged (block path vs subgraph path confirmed via ask.retrieval.mode)"
    else
      log "!! A/B arms did NOT diverge — retrieval_policy likely IGNORED (stale daemon build?). Scorecard delta is NOT trustworthy."
      gate "A/B DIVERGENCE FAIL — arms ran the SAME path; fix the daemon (uv run okto-neuron serve from the worktree) before trusting any delta"
    fi
  fi

  ARM_BLOCK="$RUN_DIR/arm-block.jsonl"
  ARM_SUBGRAPH="$RUN_DIR/arm-subgraph.jsonl"
  SCORECARD_JUDGE="$RUN_DIR/scorecard-ab.txt"
  SCORECARD_PROXY="$RUN_DIR/scorecard-ab-backstop.txt"

  if [[ -s "$ARM_BLOCK" && -s "$ARM_SUBGRAPH" ]]; then
    # PRIMARY: paired A/B on the SEMANTIC verdict (McNemar-ready). A=block, B=subgraph.
    log "STEP scorecard (PRIMARY, semantic): block(A) vs subgraph(B) grader=judge"
    if PY scorecard --a "$ARM_BLOCK" --b "$ARM_SUBGRAPH" --grader judge \
          --label-a block --label-b subgraph >"$SCORECARD_JUDGE" 2>>"$LOG"; then
      mark scorecard_ab_judge "done"; sed 's/^/  /' "$SCORECARD_JUDGE" >&2
      if grep -q '^  VERDICT: REAL improvement' "$SCORECARD_JUDGE"; then
        gate "A/B subgraph-vs-block REAL (semantic; single-pair — confirm with ab-runset N>=5)"
      else
        gate "A/B subgraph-vs-block NOT_REAL/directional (semantic; single-pair)"
      fi
    else
      log "scorecard (semantic) failed (see $LOG)"; mark scorecard_ab_judge failed
      gate "A/B N/A (semantic scorecard failed)"
    fi
    # BACKSTOP: same pairing on the deterministic must_contain column (NON-authoritative).
    log "STEP scorecard (backstop, deterministic must_contain): grader=proxy"
    if PY scorecard --a "$ARM_BLOCK" --b "$ARM_SUBGRAPH" --grader proxy \
          --label-a block --label-b subgraph >"$SCORECARD_PROXY" 2>>"$LOG"; then
      mark scorecard_ab_proxy "done"; sed 's/^/  /' "$SCORECARD_PROXY" >&2
    else
      log "scorecard (backstop) failed/absent (non-fatal; see $LOG)"; mark scorecard_ab_proxy failed
    fi
  else
    log "one or both arm files missing/empty — scorecard skipped (LLM likely unreachable; arms SKIPPED)"
    mark scorecard_ab_judge skipped
    gate "A/B N/A (arms not graded — is the LLM endpoint up at $LLM_BASE?)"
  fi

  log "A/B DONE. artifacts in $RUN_DIR (responses-*.jsonl, semjudge-*.json, arm-*.jsonl, scorecard-ab*.txt)"
  log "STATUS:"; sed 's/^/  /' "$STATUS" >&2
  echo "$RUN_DIR"
  exit 0
fi

# ── 3. RECALL + ASK (live daemon) ───────────────────────────────────────────
# Pure-client capture via run-golden.sh --endpoint, redirected into our run dir.
# run-golden writes to its own timestamped dir; we copy responses, node-types,
# and deterministic provenance into the stable dir so the chain is resumable.
RESPONSES="$RUN_DIR/responses.jsonl"
NODE_TYPES="$RUN_DIR/node-types.json"
DETERMINISTIC="$RUN_DIR/deterministic.json"
SEMANTIC_QUALITY="$RUN_DIR/semantic-quality.json"
if [[ -z "$ENDPOINT" ]]; then
  log "no --endpoint and not --ci: nothing more to do (deterministic legs complete)."
  echo "$RUN_DIR"; exit 0
fi
if ! curl -fsS "$ENDPOINT/health" >/dev/null 2>&1; then
  log "daemon unreachable at $ENDPOINT/health — start it or pass a reachable --endpoint"
  mark recall_ask failed; exit 70
fi
if ! done_already "$RESPONSES" recall_ask; then
  log "STEP recall+ask via run-golden.sh --endpoint $ENDPOINT"
  # run-golden prints JSON summaries to stdout and echoes the report path as its
  # FINAL line; take only the last report.json line (the path), not the whole blob.
  RG_ARGS=("$DATASET_DIR" --endpoint "$ENDPOINT" --no-judge)
  [[ "$NEVER_INGEST" == "1" ]] && RG_ARGS+=(--never-ingest)
  [[ "$FORCE_REINGEST" == "1" ]] && RG_ARGS+=(--force-reingest)
  [[ -n "$ENDPOINT_VAULT_PATH" ]] && RG_ARGS+=(--vault-path "$ENDPOINT_VAULT_PATH")
  [[ -n "$QUESTIONS_OVERRIDE" ]] && RG_ARGS+=(--questions "$QUESTIONS_OVERRIDE")
  RG_STDOUT="$("$SCRIPT_DIR/run-golden.sh" "${RG_ARGS[@]}" 2>>"$LOG")"
  RG_STATUS=$?
  RG_REPORT="$(printf '%s\n' "$RG_STDOUT" | grep -E '/report\.json$' | tail -1)"
  if [[ -n "$RG_REPORT" ]]; then
    RG_DIR="$(dirname "$RG_REPORT")"
  else
    RG_DIR="$(golden_artifact_dir)"
  fi
  if [[ -n "$RG_DIR" ]] && golden_responses_complete "$RG_DIR"; then
    cp "$RG_DIR/responses.jsonl" "$RESPONSES"
    [[ -s "$RG_DIR/node-types.json" ]] && cp "$RG_DIR/node-types.json" "$NODE_TYPES"
    [[ -s "$RG_DIR/deterministic.json" ]] && cp "$RG_DIR/deterministic.json" "$DETERMINISTIC"
    [[ -s "$RG_DIR/semantic-quality.json" ]] && \
      cp "$RG_DIR/semantic-quality.json" "$SEMANTIC_QUALITY"
    log "captured $(wc -l <"$RESPONSES" | tr -d ' ') responses -> $(basename "$RESPONSES")"
    mark recall_ask "done"
    if [[ "$RG_STATUS" != "0" ]]; then
      log "run-golden exited $RG_STATUS after the complete response capture; preserving it and resuming from the first missing sidecar"
    fi
  else
    log "recall+ask leg failed before a complete response capture (status=$RG_STATUS; see $LOG)"
    mark recall_ask failed
    exit 71
  fi
fi
[[ -s "$NODE_TYPES" ]] || echo '{}' > "$NODE_TYPES"

# A resumed run may predate semantic-quality.json while retaining immutable
# responses.  Re-submit every captured recall_cost sample to the same endpoint;
# the endpoint, not this harness, owns aggregation and metric definitions.
if ! semantic_quality_valid "$SEMANTIC_QUALITY" "$RESPONSES"; then
  if [[ "$ENDPOINT_DATASET_VERIFIED" != "1" ]]; then
    log "cannot regenerate semantic-quality: endpoint corpus identity is not verified"
    mark semantic_quality failed
    exit 74
  fi
  log "STEP semantic-quality -> semantic-quality.json"
  if PY semantic-quality --endpoint "$ENDPOINT" --responses "$RESPONSES" \
      --out "$SEMANTIC_QUALITY" >>"$LOG" 2>&1; then
    mark semantic_quality "done"
  else
    log "semantic-quality capture failed or incomplete (see $LOG)"
    mark semantic_quality failed
    exit 73
  fi
fi

# run-golden writes deterministic provenance beside its timestamped responses.
# Preserve that evidence in the stable resumable run dir; for older/incomplete
# runs that already have responses but not the sidecar, rebuild it from those
# immutable responses and the dataset inputs without touching the live daemon.
if [[ ! -s "$DETERMINISTIC" ]]; then
  log "STEP provenance -> deterministic.json"
  if PY provenance --inputs "$INPUTS_DIR" --responses "$RESPONSES" \
        --questions "$QUESTIONS" --out "$DETERMINISTIC" >>"$LOG" 2>&1; then
    mark provenance "done"
  else
    log "deterministic provenance failed (see $LOG)"
    mark provenance failed
    exit 73
  fi
fi

# ── 5. JUDGE  (single-LLM)  OR  --panel (N-judge panel) ─────────────────────
JUDGE_OUT="$RUN_DIR/judge.json"
PANEL_OUT="$RUN_DIR/panel.json"
JUDGE_FOR_REPORT=""
if [[ "$RUN_JUDGE" == "1" ]]; then
  if [[ "$USE_PANEL" == "1" ]]; then
    if ! panel_valid "$PANEL_OUT" "$RESPONSES" "$LIMIT" || ! done_already "$PANEL_OUT" panel; then
      log "STEP panel (N-judge, disjoint-family, 2/3-vote) -> panel.json"
      PN_ARGS=("panel" "--responses" "$RESPONSES" "--questions" "$QUESTIONS" "--out" "$PANEL_OUT")
      [[ -n "$LIMIT" ]] && PN_ARGS+=("--limit" "$LIMIT")
      if PY "${PN_ARGS[@]}" >>"$LOG" 2>&1; then mark panel "done"; else
        log "panel leg failed/partial (see $LOG)"; mark panel failed; exit 73; fi
    fi
  else
    if ! scripted_judge_valid "$JUDGE_OUT" "$RESPONSES" || ! done_already "$JUDGE_OUT" judge; then
      log "STEP judge (scripted single-LLM verdicts) -> judge.json"
      if PY judge --questions "$QUESTIONS" --responses "$RESPONSES" \
            --out "$JUDGE_OUT" >>"$LOG" 2>&1; then mark judge "done"; else
        log "judge leg failed (see $LOG)"; mark judge failed; exit 73; fi
    fi
    scripted_judge_valid "$JUDGE_OUT" "$RESPONSES" && JUDGE_FOR_REPORT="$JUDGE_OUT"
  fi
else
  log "judge/panel grader disabled (--no-judge); live /ask calls were still executed"
fi

# ── 6. FLOOR-LAPTOP (citation byte-verify + extraction-completeness) ────────
FLOOR_LAPTOP_OUT="$RUN_DIR/floor_laptop.json"
if ! floor_laptop_valid "$FLOOR_LAPTOP_OUT" "$RESPONSES" \
    || ! done_already "$FLOOR_LAPTOP_OUT" floor_laptop; then
  log "STEP floor-laptop (citation byte-verify + extraction-completeness vs live vault)"
  FL_ARGS=("floor-laptop" "--responses" "$RESPONSES" "--endpoint" "$ENDPOINT" "--out" "$FLOOR_LAPTOP_OUT")
  [[ -n "$BOUND" ]] && FL_ARGS+=("--bound" "$BOUND")
  if PY "${FL_ARGS[@]}" >>"$LOG" 2>&1; then mark floor_laptop "done"; else
    log "floor-laptop reported a hard citation failure or errored (see $LOG)"
    mark floor_laptop failed
    exit 1
  fi
fi

# ── 7. SCORECARD vs a baseline arm (optional) ───────────────────────────────
SCORECARD_OUT="$RUN_DIR/scorecard.txt"
if [[ -n "$BASELINE" ]]; then
  if ! done_already "$SCORECARD_OUT" scorecard; then
    log "STEP scorecard: this run (B) vs baseline (A=$BASELINE) grader=$GRADER"
    if PY scorecard --a "$BASELINE" --b "$RESPONSES" --grader "$GRADER" \
          --questions "$QUESTIONS" --label-a baseline --label-b "$RUN_ID" \
          >"$SCORECARD_OUT" 2>>"$LOG"; then
      mark scorecard "done"; sed 's/^/  /' "$SCORECARD_OUT" | tail -8 >&2
      # GATE 2 — A/B SIGNIFICANCE. This single-pair scorecard reports the 5-condition
      # REAL classification (neg-guard is the negative_control rows graded here via
      # must_contain). The flip-default decision needs the multi-run band:
      #   uv run python tests/golden/bin/judge.py ab-runset --config '{...}'  (N>=5, laptop-only).
      if grep -q '^  VERDICT: REAL improvement' "$SCORECARD_OUT"; then
        gate "A/B REAL (single-pair; confirm with ab-runset N>=5 before flip-default)"
      else
        gate "A/B NOT_REAL (single-pair; directional only — see ab-runset for the powered band)"
      fi
    else
      log "scorecard leg failed (see $LOG)"; mark scorecard failed
      gate "A/B N/A (scorecard leg failed)"; fi
  fi
else
  log "no --baseline: skipping scorecard (it compares THIS run to a prior arm)"
  gate "A/B N/A (no --baseline; run ab-runset for the multi-run significance gate)"
fi

# ── 8. REPORT (merge everything) ────────────────────────────────────────────
REPORT_OUT="$RUN_DIR/report.json"
# Report merge is cheap and deterministic. Always rebuild it from the currently
# retained sidecars so a later judge/panel completion cannot leave a stale tally.
log "STEP report -> report.json"
RP_ARGS=("report" "--dataset" "$DATASET" "--timestamp" "$RUN_ID"
         "--responses" "$RESPONSES" "--deterministic" "$DETERMINISTIC"
         "--semantic-quality" "$SEMANTIC_QUALITY"
         "--floor-laptop" "$FLOOR_LAPTOP_OUT"
         "--node-types" "$NODE_TYPES" "--out" "$REPORT_OUT")
[[ -n "$JUDGE_FOR_REPORT" ]] && RP_ARGS+=("--judge" "$JUDGE_FOR_REPORT")
if PY "${RP_ARGS[@]}" >>"$LOG" 2>&1; then mark report "done"; else
  log "report merge failed (see $LOG)"; mark report failed; exit 73; fi

log "DONE. artifacts in $RUN_DIR"
log "STATUS:"; sed 's/^/  /' "$STATUS" >&2
echo "$RUN_DIR"
exit 0
