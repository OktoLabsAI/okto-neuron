#!/usr/bin/env bash
# ============================================================================
# run-golden.sh — THE golden-dataset harness. Deterministic, dataset-agnostic.
#
# Black-box only: drives Okto Neuron through its public CLI + HTTP surface,
# exactly like a human. Imports ZERO project source.
#
# Okto Neuron IS a normal web server; the CLI/MCP/UI are all just clients of it.
# This eval is one more client. Three modes:
#
#   # sealed self-hosted live run (no flag): ephemeral server, then teardown
#   ./tests/golden/bin/run-golden.sh <dataset-name-or-path> [--no-judge] [--keep-vault]
#   # sealed mode against an AUTHENTICATING gateway: name the env var that holds
#   # the key (name must match ^OKTO_NEURON_[A-Z0-9_]+$). Only the NAME reaches the
#   # vault config; the value is read by the app from the environment at call time
#   # and is never written to YAML, logs, traces, or evidence.
#   #   export OKTO_NEURON_PROVIDER_OPENAI_API_KEY=...   # the secret, your shell only
#   #   OKTO_NEURON_GOLDEN_API_KEY_ENV=OKTO_NEURON_PROVIDER_OPENAI_API_KEY \
#   #     ./tests/golden/bin/run-golden.sh <dataset>
#
#   # pure client: hit an already-running server (you started it, or docker/remote)
#   ./tests/golden/bin/run-golden.sh <dataset-name-or-path> --endpoint http://127.0.0.1:7777 [--no-judge]
#
#   # launcher: start a persistent named server on :7777, run as its client, leave it up
#   ./tests/golden/bin/run-golden.sh <dataset> --serve [--vault <name>] [--wipe] [--no-judge]
#
# A vault is persistent and named, like a document: opening NEVER overrides it;
# wiping to start fresh is an explicit --wipe opt-in.
#
# Exit codes:
#   0  = harness ran clean end-to-end (answer QUALITY lives in the report, not here)
#   non-zero = harness/infra failure (serve died, ingest errored, queue stuck, etc.)
#
# Outputs (gitignored): tests/golden/results/<dataset>/<timestamp>/
#   server.log  responses.jsonl  semantic-quality.json  deterministic.json
#   judge.json  report.json
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GOLDEN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$GOLDEN_DIR/../.." && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_serve.sh"

USAGE="usage: run-golden.sh <dataset-name-or-path> [--no-judge] [--keep-vault] [--endpoint <url>] [--vault-path <path>] [--never-ingest|--force-reingest] [--serve [--vault <name>] [--wipe]]
                     [--retrieval-policy '<json>'] [--questions <file>]"

DATASET="${1:-}"
if [[ -z "$DATASET" || "$DATASET" == --* ]]; then
  echo "$USAGE" >&2
  exit 64
fi
shift || true

RUN_JUDGE=1
KEEP_VAULT=0
ENDPOINT=""        # Mode A: pure client against an already-running server
ENDPOINT_VAULT_PATH="" # Explicit multi-vault selector for Mode A
SERVE_MODE=0       # Mode B: launch a persistent named server, then run as its client
VAULT_NAME=""      # Mode B: named vault under tests/golden/vaults/<name>
WIPE=0             # Mode B: explicit fresh-baseline opt-in
NEVER_INGEST=0     # Pure-client audit mode: require a populated matching graph
FORCE_REINGEST=0   # ADR 0040 arm: explicitly re-ingest one matching populated vault
RETRIEVAL_POLICY="" # A/B arm knob: JSON merged into every /ask body as retrieval_policy
QUESTIONS_OVERRIDE="" # drive an optional out-of-dataset questions.yaml
LIVE_LLM_BASE="${OKTO_NEURON_LLM_BASE_URL:-${OPENAI_BASE_URL:-}}"
LIVE_MODEL="${OKTO_NEURON_REALMODEL_MODEL:-unsloth/Qwen3.6-27B-NVFP4}"
GOLDEN_HTTP_CONNECT_TIMEOUT_S="${OKTO_NEURON_GOLDEN_HTTP_CONNECT_TIMEOUT_S:-5}"
GOLDEN_HTTP_TIMEOUT_S="${OKTO_NEURON_GOLDEN_HTTP_TIMEOUT_S:-30}"
GOLDEN_RECALL_TIMEOUT_S="${OKTO_NEURON_GOLDEN_RECALL_TIMEOUT_S:-60}"
GOLDEN_ASK_TIMEOUT_S="${OKTO_NEURON_GOLDEN_ASK_TIMEOUT_S:-180}"
GOLDEN_INGEST_TIMEOUT_S="${OKTO_NEURON_GOLDEN_INGEST_TIMEOUT_S:-5400}"
for timeout_name in \
  GOLDEN_HTTP_CONNECT_TIMEOUT_S \
  GOLDEN_HTTP_TIMEOUT_S \
  GOLDEN_RECALL_TIMEOUT_S \
  GOLDEN_ASK_TIMEOUT_S \
  GOLDEN_INGEST_TIMEOUT_S; do
  timeout_value="${!timeout_name}"
  [[ "$timeout_value" =~ ^[1-9][0-9]*$ ]] || {
    echo "$timeout_name must be a positive integer" >&2
    exit 64
  }
done
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-judge) RUN_JUDGE=0 ;;
    --keep-vault) KEEP_VAULT=1 ;;
    --endpoint) shift; ENDPOINT="${1:-}"; [[ -n "$ENDPOINT" ]] || { echo "--endpoint requires a url" >&2; exit 64; } ;;
    --vault-path) shift; ENDPOINT_VAULT_PATH="${1:-}"; [[ -n "$ENDPOINT_VAULT_PATH" ]] || { echo "--vault-path requires a path" >&2; exit 64; } ;;
    --serve) SERVE_MODE=1 ;;
    --vault) shift; VAULT_NAME="${1:-}"; [[ -n "$VAULT_NAME" ]] || { echo "--vault requires a name" >&2; exit 64; } ;;
    --wipe) WIPE=1 ;;
    --never-ingest) NEVER_INGEST=1 ;;
    --force-reingest) FORCE_REINGEST=1 ;;
    # ── A/B arm knob ── the ONLY per-arm difference: this JSON object is spliced
    # into every /api/v1/ask body as `retrieval_policy` (AskRetrievalPolicy).
    # Block arm passes '{"enable_subgraph": false}', subgraph arm '{"enable_subgraph": true}'.
    # Recall is unaffected (subgraph only changes the ASK answering path).
    --retrieval-policy)
      shift; RETRIEVAL_POLICY="${1:-}"
      [[ -n "$RETRIEVAL_POLICY" ]] || { echo "--retrieval-policy requires a JSON object" >&2; exit 64; }
      python3 -c "import json,sys;o=json.loads(sys.argv[1]);sys.exit(0 if isinstance(o,dict) else 1)" "$RETRIEVAL_POLICY" \
        || { echo "--retrieval-policy must be a JSON object, got: $RETRIEVAL_POLICY" >&2; exit 64; } ;;
    --questions)
      shift; QUESTIONS_OVERRIDE="${1:-}"
      [[ -f "$QUESTIONS_OVERRIDE" ]] || { echo "--questions file not found: $QUESTIONS_OVERRIDE" >&2; exit 64; } ;;
    *) echo "unknown flag: $1" >&2; echo "$USAGE" >&2; exit 64 ;;
  esac
  shift
done

# Mode validation: --endpoint and --serve are mutually exclusive; --vault/--wipe
# only make sense when launching a server (--serve).
if [[ -n "$ENDPOINT" && "$SERVE_MODE" == "1" ]]; then
  echo "--endpoint and --serve are mutually exclusive" >&2; exit 64
fi
if [[ "$SERVE_MODE" != "1" && ( -n "$VAULT_NAME" || "$WIPE" == "1" ) ]]; then
  echo "--vault/--wipe require --serve" >&2; exit 64
fi
if [[ "$NEVER_INGEST" == "1" && -z "$ENDPOINT" ]]; then
  echo "--never-ingest requires --endpoint" >&2; exit 64
fi
if [[ "$FORCE_REINGEST" == "1" && -z "$ENDPOINT" ]]; then
  echo "--force-reingest requires --endpoint" >&2; exit 64
fi
if [[ "$FORCE_REINGEST" == "1" && -z "$ENDPOINT_VAULT_PATH" ]]; then
  echo "--force-reingest requires --vault-path" >&2; exit 64
fi
if [[ "$FORCE_REINGEST" == "1" && "$NEVER_INGEST" == "1" ]]; then
  echo "--force-reingest and --never-ingest are mutually exclusive" >&2; exit 64
fi
if [[ -n "$ENDPOINT_VAULT_PATH" && -z "$ENDPOINT" ]]; then
  echo "--vault-path requires --endpoint" >&2; exit 64
fi
if [[ -n "$ENDPOINT_VAULT_PATH" ]]; then
  export OKTO_NEURON_VAULT_PATH="$ENDPOINT_VAULT_PATH"
else
  unset OKTO_NEURON_VAULT_PATH
fi
# Both client modes leave the server + vault standing (never tear down).
LEAVE_UP=0
if [[ -n "$ENDPOINT" || "$SERVE_MODE" == "1" ]]; then
  LEAVE_UP=1
fi

# shellcheck source=tests/golden/bin/_dataset_dir.sh
source "$SCRIPT_DIR/_dataset_dir.sh"
DATASET_DIR="$(resolve_dataset_dir "$DATASET" "$GOLDEN_DIR")" || exit 64
DATASET="$(basename "$DATASET_DIR")"
INPUTS_DIR="$DATASET_DIR/inputs"
QUESTIONS="$DATASET_DIR/questions.yaml"
# --questions overrides the dataset's questions.yaml so a caller can drive an
# out-of-dataset set against the SAME graph.
[[ -n "$QUESTIONS_OVERRIDE" ]] && QUESTIONS="$QUESTIONS_OVERRIDE"
MANIFEST="$DATASET_DIR/dataset.yaml"
JUDGE_PY="$SCRIPT_DIR/judge.py"

[[ -d "$DATASET_DIR" ]] || { echo "no dataset: $DATASET_DIR" >&2; exit 64; }
[[ -d "$INPUTS_DIR" ]]  || { echo "no inputs/: $INPUTS_DIR" >&2; exit 64; }
[[ -f "$QUESTIONS" ]]   || { echo "no questions.yaml: $QUESTIONS" >&2; exit 64; }

log() { printf '[golden:%s] %s\n' "$DATASET" "$*" >&2; }

# uv is the canonical entry point: sync the locked env, then drive the uv-managed
# binary directly. The harness needs the real server PID for signal-based stop, so
# it runs the resolved uv-managed `marginalia` rather than wrapping
# every call in `uv run`. No pip anywhere.
command -v uv >/dev/null 2>&1 || { echo "uv not found; install it: https://docs.astral.sh/uv/" >&2; exit 70; }
if ! ( cd "$REPO_ROOT" && uv sync --quiet --group litellm ); then
  echo "uv sync --group litellm failed in $REPO_ROOT; run it directly to diagnose" >&2
  exit 70
fi
# Derive the env uv actually manages (honours UV_PROJECT_ENVIRONMENT) instead of
# assuming $REPO_ROOT/.venv, then PROVE the resolved binary belongs to it. A bare
# `command -v marginalia` would silently accept the operator's global uv tool.
if ! VENV_DIR="$(golden_resolve_env "$REPO_ROOT")" || [[ -z "$VENV_DIR" ]]; then
  echo "could not derive the uv-managed environment" >&2; exit 70
fi
export PATH="$VENV_DIR/bin:$PATH"
golden_assert_repo_binary "$VENV_DIR" "$REPO_ROOT" || exit 70
log "venv provenance OK: bin=$GOLDEN_MARGINALIA_BIN prefix=$GOLDEN_MARGINALIA_PREFIX module=$GOLDEN_MARGINALIA_MODULE"
# (artifact written once RESULTS_DIR exists — see below)

PY() { "$VENV_DIR/bin/python" "$JUDGE_PY" "$@"; }
if ! QUESTION_VALIDATION="$(PY validate-questions --questions "$QUESTIONS" 2>&1)"; then
  printf '%s\n' "$QUESTION_VALIDATION" >&2
  echo "invalid questions: every non-negative question needs at least one gold_target" >&2
  exit 64
fi
if ! QJSON="$(PY yaml2json --file "$QUESTIONS")"; then
  log "questions YAML conversion FAILED"
  exit 64
fi
if ! INPUT_VALIDATION="$(PY validate-inputs --inputs "$INPUTS_DIR" 2>&1)"; then
  printf '%s\n' "$INPUT_VALIDATION" >&2
  echo "invalid inputs: source basenames must remain unique after HTTP ingest" >&2
  exit 64
fi

# ── run-scoped working dirs (NOT durable /tmp) ──────────────────────────────
TS="$(date +%Y%m%d-%H%M%S)"
RESULTS_DIR="$GOLDEN_DIR/results/$DATASET/$TS"
mkdir -p "$RESULTS_DIR"
log "artifacts: $RESULTS_DIR"
golden_write_provenance "$RESULTS_DIR/venv-provenance.json"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/marginalia-golden.XXXXXX")"
VAULT="$WORK_DIR/vault"
SERVER_LOG="$RESULTS_DIR/server.log"
RESPONSES="$RESULTS_DIR/responses.jsonl"
: > "$RESPONSES"

# Sealed mode must not observe or overwrite the operator's application config,
# daemon tokens, or user-level caches.  The vault and its lifecycle locks are
# already under WORK_DIR; HOME/XDG isolate the remaining global state.
if [[ "$LEAVE_UP" != "1" ]]; then
  export HOME="$WORK_DIR/home"
  export XDG_CONFIG_HOME="$WORK_DIR/xdg-config"
  export XDG_DATA_HOME="$WORK_DIR/xdg-data"
  export XDG_STATE_HOME="$WORK_DIR/xdg-state"
  export XDG_CACHE_HOME="$WORK_DIR/xdg-cache"
  mkdir -p "$HOME" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$XDG_CACHE_HOME"
fi

cleanup() {
  # Client modes (--endpoint / --serve): leave the server up and the vault on
  # disk. Only the run-scoped scratch dir is removed — never the persistent vault.
  if [[ "$LEAVE_UP" == "1" ]]; then
    local url="${OKTO_NEURON_ENDPOINT:-${ENDPOINT:-http://127.0.0.1:7777}}"
    log "server left running at $url (vault persists)"
    if [[ -n "${_GOLDEN_SERVER_PID:-}" ]]; then
      log "  stop it with: kill $_GOLDEN_SERVER_PID"
    fi
    rm -rf "$WORK_DIR"
    return
  fi
  if [[ -n "${PROXY_PID:-}" ]] && kill -0 "$PROXY_PID" 2>/dev/null; then
    kill "$PROXY_PID" 2>/dev/null || true
    wait "$PROXY_PID" 2>/dev/null || true
  fi
  golden_stop_server || true
  if [[ "$KEEP_VAULT" == "1" ]]; then
    log "keeping vault at $WORK_DIR (--keep-vault)"
  else
    rm -rf "$WORK_DIR"
  fi
}
trap cleanup EXIT

# Pre-flight port guard (Mode B): refuse to start a second server on a busy port,
# which would violate the single-writer-per-vault invariant.
port_in_use() {  # port_in_use <port> -> 0 if LISTENing
  python3 -c "
import socket,sys
s=socket.socket(); s.settimeout(0.5)
try:
    s.connect(('127.0.0.1', int(sys.argv[1]))); sys.exit(0)
except Exception:
    sys.exit(1)
finally:
    s.close()
" "$1" 2>/dev/null
}

# ── 1. settings from manifest ───────────────────────────────────────────────
SETTINGS_JSON="{}"
if [[ -f "$MANIFEST" ]]; then
  if ! SETTINGS_JSON="$(PY yaml2json --file "$MANIFEST")"; then
    log "invalid dataset manifest: $MANIFEST"
    exit 64
  fi
fi
get_setting() {  # get_setting <jqlike-path> <default>
  python3 -c "
import json,sys
d=json.loads(sys.argv[1] or '{}')
cur=d.get('settings',{}) if isinstance(d,dict) else {}
for key in sys.argv[2].split('.'):
    cur=cur.get(key,{}) if isinstance(cur,dict) else {}
print(cur if (cur or cur==0) else sys.argv[3])
" "$SETTINGS_JSON" "$1" "$2"
}
DEFAULT_K="$(python3 -c "import json,sys;print((json.loads(sys.argv[1]).get('settings') or {}).get('k',10))" "$QJSON")"
[[ "$DEFAULT_K" =~ ^[0-9]+$ ]] || DEFAULT_K=10
# Queue-drain budget. Ingest runs REAL LLM extraction synchronously and can take
# minutes per file on the explicitly configured model, so a 17-file corpus can
# take 30-60+ min. Default generous;
# override per-dataset via settings.ingest_timeout_s in dataset.yaml.
INGEST_TIMEOUT_S="$(get_setting 'ingest_timeout_s' 5400)"   # default 90 min
[[ "$INGEST_TIMEOUT_S" =~ ^[0-9]+$ ]] || INGEST_TIMEOUT_S=5400

# ── 2+3. vault + server bring-up (mode-gated) ───────────────────────────────
if [[ -n "$ENDPOINT" ]]; then
  # ── Mode A: pure client — server already running; skip init/proxy/serve ──
  export OKTO_NEURON_ENDPOINT="$ENDPOINT"
  export OKTO_NEURON_QUERY_NEIGHBORS="${OKTO_NEURON_QUERY_NEIGHBORS:-0}"
  log "pure-client mode: server at $OKTO_NEURON_ENDPOINT (no init, no serve)"
  [[ -n "$ENDPOINT_VAULT_PATH" ]] && log "explicit vault selector enabled"
  if ! curl -fsS "$OKTO_NEURON_ENDPOINT/health" >/dev/null 2>&1; then
    log "server unreachable at $OKTO_NEURON_ENDPOINT/health — start it first"; exit 70
  fi
elif [[ "$SERVE_MODE" == "1" ]]; then
  # ── Mode B: launch a persistent named server, then run as its client ──
  # A vault is persistent and named, like a document. Opening NEVER overrides it;
  # --wipe is the explicit fresh-baseline opt-in.
  VAULT="$REPO_ROOT/tests/golden/vaults/${VAULT_NAME:-$DATASET}"
  if [[ "$WIPE" == "1" ]]; then
    log "--wipe: fresh baseline, removing $VAULT"
    rm -rf "$VAULT"
  fi
  if [[ -f "$VAULT/okto-neuron.yaml" ]]; then
    log "opening existing vault as-is (non-destructive): $VAULT"
  else
    log "kg init $VAULT"
    if [[ -z "${LIVE_LLM_BASE//[[:space:]]/}" ]]; then
      log "--serve requires OKTO_NEURON_LLM_BASE_URL for a new live-model vault"
      exit 64
    fi
    mkdir -p "$(dirname "$VAULT")"
    if ! okto-neuron init "$VAULT" >"$RESULTS_DIR/init.log" 2>&1; then
      log "kg init failed; see $RESULTS_DIR/init.log"; exit 70
    fi
    [[ -f "$VAULT/okto-neuron.yaml" ]] || { log "vault config missing after init"; exit 70; }
    if ! okto-neuron onboard --vault "$VAULT" --provider custom \
      --api-base "$LIVE_LLM_BASE" --model "$LIVE_MODEL" --skip-model-discovery \
      --non-interactive --allow-remote-llm --yes \
      >"$RESULTS_DIR/onboard.log" 2>&1; then
      log "live-model onboarding failed; see $RESULTS_DIR/onboard.log"; exit 70
    fi
  fi
  # Pre-flight :7777 guard — protect single-writer-per-vault (no two servers, one vault).
  if port_in_use 7777; then
    log ":7777 is already LISTENing — refusing to start a second server."
    log "  stop the running server, or run as a client: --endpoint http://127.0.0.1:7777"
    exit 75
  fi
  # Use the vault's own explicitly configured LLM directly — no proxy, exactly
  # like an admin's box. The UI auto-serves at :7777 via the StaticFiles mount.
  export OKTO_NEURON_QUERY_NEIGHBORS="${OKTO_NEURON_QUERY_NEIGHBORS:-0}"
  log "block-neighbor expansion: OKTO_NEURON_QUERY_NEIGHBORS=$OKTO_NEURON_QUERY_NEIGHBORS"
  log "serve REST=7777 MCP=8201 (no proxy)"
  if ! golden_start_server "$VAULT" 7777 8201 "$SERVER_LOG"; then
    log "server failed to come up; see $SERVER_LOG"; exit 70
  fi
  log "server ready at $OKTO_NEURON_ENDPOINT — leaving it up after the run"
else
# ── Sealed CI mode (unchanged): ephemeral vault + LLM proxy + random ports ──
# ── 2. fresh vault ──────────────────────────────────────────────────────────
log "kg init $VAULT"
if ! okto-neuron init "$VAULT" >"$RESULTS_DIR/init.log" 2>&1; then
  log "kg init failed; see $RESULTS_DIR/init.log"; exit 70
fi
[[ -f "$VAULT/okto-neuron.yaml" ]] || { log "vault config missing after init"; exit 70; }

# ── 3. serve ────────────────────────────────────────────────────────────────
# --- LLM logging proxy: black-box visibility into every inference call -------
# The proxy sits between marginalia and the real LLM endpoint — it just connects
# to the upstream IP like any other client — and writes the full request/response
# chain per block into the per-run trace dir. Touches NO app code.
# ── sealed-mode credential passthrough (name only, never the value) ─────────
# OKTO_NEURON_GOLDEN_API_KEY_ENV names an env var (e.g.
# OKTO_NEURON_PROVIDER_OPENAI_API_KEY) whose value this harness NEVER reads,
# echoes, or persists. Without it the sealed vault carries no credential and any
# authenticating gateway answers 401 — the failure two sessions worked around
# with untracked auth shims. The pattern mirrors _check_api_key_env in
# src/marginalia/config/_vault.py so a bad name fails in one second instead of
# forty minutes into a run.
SEALED_API_KEY_ENV="${OKTO_NEURON_GOLDEN_API_KEY_ENV:-}"
if [[ -n "$SEALED_API_KEY_ENV" ]]; then
  if [[ ! "$SEALED_API_KEY_ENV" =~ ^OKTO_NEURON_[A-Z0-9_]+$ ]]; then
    log "OKTO_NEURON_GOLDEN_API_KEY_ENV must NAME an env var matching ^OKTO_NEURON_[A-Z0-9_]+\$"
    log "  got: $SEALED_API_KEY_ENV (did you pass the key itself instead of its name?)"
    exit 64
  fi
  if [[ -z "${!SEALED_API_KEY_ENV:-}" ]]; then
    log "$SEALED_API_KEY_ENV is named but unset/empty; export it in your shell."
    log "  the harness never stores or reads the value — the daemon inherits it."
    exit 64
  fi
  log "sealed LLM auth: api_key_env=$SEALED_API_KEY_ENV (value not read by the harness)"
else
  log "sealed LLM auth: none (no OKTO_NEURON_GOLDEN_API_KEY_ENV); an authenticating gateway will 401"
fi

LLM_TRACE_DIR="$RESULTS_DIR/llm_trace"
mkdir -p "$LLM_TRACE_DIR"
PROXY_UPSTREAM="$LIVE_LLM_BASE"
if [[ -z "${PROXY_UPSTREAM//[[:space:]]/}" ]]; then
  log "sealed live-model runs require OKTO_NEURON_LLM_BASE_URL"
  exit 64
fi
PROXY_PORT="$(golden_free_port)"
PROXY_CURRENT_FILE="$WORK_DIR/current_source.txt"
: >"$PROXY_CURRENT_FILE"
PROXY_LOG="$RESULTS_DIR/llm-proxy.log"
OKTO_NEURON_PROXY_PORT="$PROXY_PORT" \
OKTO_NEURON_PROXY_UPSTREAM="$PROXY_UPSTREAM" \
OKTO_NEURON_PROXY_TRACE_DIR="$LLM_TRACE_DIR" \
OKTO_NEURON_PROXY_CURRENT_FILE="$PROXY_CURRENT_FILE" \
  python3 "$SCRIPT_DIR/llm-proxy.py" >"$PROXY_LOG" 2>&1 &
PROXY_PID=$!
export OKTO_NEURON_LLM_BASE_URL="http://127.0.0.1:$PROXY_PORT/v1"
for _ in $(seq 1 50); do
  curl -fsS "http://127.0.0.1:$PROXY_PORT/healthz" >/dev/null 2>&1 && break
  sleep 0.2
done
# The companion reads llm.defaults.api_base from the vault's okto-neuron.yaml (env
# is NOT consulted), so point that config at the proxy — exactly like an admin
# would. LLMConfig is strict (extra="forbid"): write ONLY known fields, else
# validation fails silently and the server falls back to its product default instead
# of the explicitly selected live endpoint.
# Sanctioned credential passthrough: the vault config carries only the NAME of
# an env var (validated above); the value is read by the app from the process
# environment at call time and is never written to YAML, logs, or evidence.
if ! "$VENV_DIR/bin/python" - \
  "$VAULT/okto-neuron.yaml" "http://127.0.0.1:$PROXY_PORT/v1" "$LIVE_MODEL" \
  "$SEALED_API_KEY_ENV" <<'PY'
import sys, yaml
path, url, model, api_key_env = sys.argv[1:]
with open(path) as f:
    cfg = yaml.safe_load(f) or {}
defaults = {"provider": "openai", "api_base": url, "model": model}
if api_key_env:
    defaults["api_key_env"] = api_key_env
cfg["llm"] = {
    "allow_remote": False,
    "defaults": defaults,
}
with open(path, "w") as f:
    yaml.safe_dump(cfg, f, sort_keys=False)
PY
then
  log "failed to patch vault config for the pinned LLM proxy"
  exit 70
fi
log "LLM proxy :$PROXY_PORT -> $PROXY_UPSTREAM (vault config patched; trace -> $LLM_TRACE_DIR)"
# Evidence: which credential slot was in play. The NAME only, never the value.
if [[ -n "$SEALED_API_KEY_ENV" ]]; then
  printf '{"api_key_env": "%s"}\n' "$SEALED_API_KEY_ENV" >"$RESULTS_DIR/sealed-llm-auth.json"
else
  printf '{"api_key_env": null}\n' >"$RESULTS_DIR/sealed-llm-auth.json"
fi
# Block-neighbor expansion (D5) pin — paired baseline knob, mirrors the judge
# model pin. Default OFF (0) so a bare run reproduces stock recall/ask; sweep the
# ON arm with `OKTO_NEURON_QUERY_NEIGHBORS=1 run-golden.sh ...`. The server (and
# its vault) inherits this exported env at query time.
export OKTO_NEURON_QUERY_NEIGHBORS="${OKTO_NEURON_QUERY_NEIGHBORS:-0}"
log "block-neighbor expansion: OKTO_NEURON_QUERY_NEIGHBORS=$OKTO_NEURON_QUERY_NEIGHBORS"
REST_PORT="$(golden_free_port)"; MCP_PORT="$(golden_free_port)"
[[ "$REST_PORT" == "$MCP_PORT" ]] && MCP_PORT="$(golden_free_port)"
log "serve REST=$REST_PORT MCP=$MCP_PORT"
if ! golden_start_server "$VAULT" "$REST_PORT" "$MCP_PORT" "$SERVER_LOG"; then
  log "server failed to come up; see $SERVER_LOG"; exit 70
fi
log "server ready at $OKTO_NEURON_ENDPOINT"
fi  # end vault + server bring-up

VAULT_HEADER_ARGS=()
if [[ -n "${OKTO_NEURON_VAULT_PATH:-}" ]]; then
  VAULT_HEADER_ARGS=(-H "X-Okto-Neuron-Vault: $OKTO_NEURON_VAULT_PATH")
fi

post_json() {  # post_json <path> <json-body> [timeout-seconds] -> stdout body
  # The REST port is credential-free on loopback. Keep sending the bearer when
  # present for compatibility with older daemons; current REST ignores it while
  # the separate MCP port still requires it.
  local timeout_s="${3:-$GOLDEN_HTTP_TIMEOUT_S}"
  if [[ -n "${OKTO_NEURON_AUTH_TOKEN:-}" ]]; then
    curl -fsS --connect-timeout "$GOLDEN_HTTP_CONNECT_TIMEOUT_S" \
      --max-time "$timeout_s" -X POST "$OKTO_NEURON_ENDPOINT$1" \
      ${VAULT_HEADER_ARGS[@]+"${VAULT_HEADER_ARGS[@]}"} \
      -H "Authorization: Bearer $OKTO_NEURON_AUTH_TOKEN" \
      -H 'Content-Type: application/json' -d "$2" 2>/dev/null
  else
    curl -fsS --connect-timeout "$GOLDEN_HTTP_CONNECT_TIMEOUT_S" \
      --max-time "$timeout_s" -X POST "$OKTO_NEURON_ENDPOINT$1" \
      ${VAULT_HEADER_ARGS[@]+"${VAULT_HEADER_ARGS[@]}"} \
      -H 'Content-Type: application/json' -d "$2" 2>/dev/null
  fi
}
get_json() {
  if [[ -n "${OKTO_NEURON_AUTH_TOKEN:-}" ]]; then
    curl -fsS --connect-timeout "$GOLDEN_HTTP_CONNECT_TIMEOUT_S" \
      --max-time "$GOLDEN_HTTP_TIMEOUT_S" "$OKTO_NEURON_ENDPOINT$1" \
      ${VAULT_HEADER_ARGS[@]+"${VAULT_HEADER_ARGS[@]}"} \
      -H "Authorization: Bearer $OKTO_NEURON_AUTH_TOKEN" 2>/dev/null
  else
    curl -fsS --connect-timeout "$GOLDEN_HTTP_CONNECT_TIMEOUT_S" \
      --max-time "$GOLDEN_HTTP_TIMEOUT_S" \
      "$OKTO_NEURON_ENDPOINT$1" ${VAULT_HEADER_ARGS[@]+"${VAULT_HEADER_ARGS[@]}"} 2>/dev/null
  fi
}
monotonic_ns() {
  python3 -c 'import time; print(time.monotonic_ns())'
}

# ── 4. ingest inputs/ (mode-gated) ──────────────────────────────────────────
CURRENT_BATCH_IDS_JSON="[]"
CURRENT_BATCH_ENQUEUED=0
if [[ "$LEAVE_UP" == "1" ]]; then
  # ── Client modes: ingest-if-empty, honoring "open never overrides" ──
  # Census the graph; if it's already populated, run questions against what's
  # there rather than re-ingesting on top of it.
  if ! census="$(get_json /api/v1/node-types)"; then
    log "endpoint census FAILED"; exit 70
  fi
  total_nodes="$(python3 -c "
import json,sys
d=json.loads(sys.argv[1] or '{}')
print(sum(int(t.get('count',0)) for t in d.get('types',[])))
" "$census")"
  [[ "$total_nodes" =~ ^[0-9]+$ ]] || total_nodes=0
  if [[ "$total_nodes" -gt 0 && "$FORCE_REINGEST" != "1" ]]; then
    log "vault populated ($total_nodes nodes) — opening, not re-ingesting"
  else
    if [[ "$NEVER_INGEST" == "1" ]]; then
      log "endpoint is empty and --never-ingest forbids populating it"
      exit 74
    fi
    if [[ "$total_nodes" -gt 0 ]]; then
      # This is the deliberately mutating ADR 0040 identical-reingest arm.  It
      # may run only against an explicitly selected vault whose complete
      # Document byte-hash multiset matches the dataset.  Verify before upload;
      # the ordinary post-ingest identity check is too late for this boundary.
      IDENTITY_BEFORE_REINGEST="$RESULTS_DIR/dataset-identity-before-reingest.json"
      log "--force-reingest: verifying populated endpoint identity before enqueue"
      if ! PY assert-endpoint-dataset "$DATASET_DIR" \
          --endpoint "$OKTO_NEURON_ENDPOINT" --out "$IDENTITY_BEFORE_REINGEST"; then
        log "endpoint dataset identity MISMATCH or unverifiable; refusing forced re-ingest"
        exit 74
      fi
      log "endpoint dataset identity MATCH; starting explicit identical re-ingest arm"
    fi
    # Async batch upload through the UI's BulkImport path, so the live queue and
    # progress bar actually move. One POST /api/v1/ingest-batch.
    if [[ "$total_nodes" -gt 0 ]]; then
      log "vault populated and identity-verified — explicitly re-ingesting inputs/"
    else
      log "vault empty — ingesting inputs/ via POST /api/v1/ingest-batch (async queue)"
    fi
    batch_body="$(python3 -c "
import json,os,sys
inputs=sys.argv[1]
exts=('.md','.markdown','.txt')
files=[]
for root,_,names in os.walk(inputs):
    for n in names:
        if not n.lower().endswith(exts): continue
        p=os.path.join(root,n)
        files.append({'filename':os.path.relpath(p,inputs),
                      'content':open(p,'rb').read().decode('utf-8')})
files.sort(key=lambda f: f['filename'])
print(json.dumps({'files':files}))
" "$INPUTS_DIR")"
    if ! resp="$(post_json /api/v1/ingest-batch "$batch_body")"; then
      log "ingest-batch FAILED"; exit 71
    fi
    enq="$(python3 -c "import json,sys;print(json.loads(sys.argv[1] or '{}').get('enqueued',0))" "$resp")"
    CURRENT_BATCH_IDS_JSON="$(python3 -c "
import json,sys
d=json.loads(sys.argv[1] or '{}')
print(json.dumps(d.get('enqueued_item_ids',[]),separators=(',',':')))
" "$resp")"
    log "ingest-batch enqueued=$enq"
    if [[ "${enq:-0}" -eq 0 ]]; then
      log "no files enqueued; aborting"; exit 71
    fi
    if ! python3 -c "
import json,sys
expected=int(sys.argv[1]); ids=json.loads(sys.argv[2])
ok=(isinstance(ids,list) and len(ids)==expected and len(set(ids))==expected
    and all(isinstance(item_id,str) and item_id for item_id in ids))
raise SystemExit(0 if ok else 1)
" "$enq" "$CURRENT_BATCH_IDS_JSON"; then
      log "ingest-batch did not return the exact enqueued item identities"
      exit 71
    fi
    CURRENT_BATCH_ENQUEUED=1
  fi
else
  # ── Sealed mode (unchanged): deterministic sync per-file POST /api/v1/ingest ──
  log "ingesting inputs/ via POST /api/v1/ingest"
  ingest_errors=0; ingested=0
  while IFS= read -r f; do
    rel="${f#"$INPUTS_DIR"/}"
    # tag every LLM trace from this document with its source filename
    printf '%s' "$rel" >"$PROXY_CURRENT_FILE"
    body="$(python3 -c "
import json,sys
content=open(sys.argv[1],'rb').read().decode('utf-8')
print(json.dumps({'content':content,'filename':sys.argv[2]}))
" "$f" "$rel")"
    if resp="$(post_json /api/v1/ingest "$body" "$GOLDEN_INGEST_TIMEOUT_S")"; then
      ingested=$((ingested+1))
    else
      ingest_errors=$((ingest_errors+1))
      log "ingest FAILED: $rel"
    fi
  done < <(find "$INPUTS_DIR" -type f \( -name '*.md' -o -name '*.markdown' -o -name '*.txt' \) | sort)
  log "ingested=$ingested errors=$ingest_errors"
  if [[ "$ingest_errors" -ne 0 ]]; then
    log "ingest had errors; aborting"; exit 71
  fi
  if [[ "$ingested" -eq 0 ]]; then
    log "no input files ingested; aborting"; exit 71
  fi
fi

# ── 5. watch only work owned by this invocation ─────────────────────────────
# A reused endpoint can retain historical errors or unrelated active work. The
# batch API returns exact item ids, so client mode waits only for its own batch.
# A populated/--never-ingest run owns no queue work and must not drain globally.
if [[ "$LEAVE_UP" == "1" && "$CURRENT_BATCH_ENQUEUED" != "1" ]]; then
  log "no ingest owned by this run — skipping queue wait"
elif [[ "$LEAVE_UP" == "1" ]]; then
  log "watching this run's $enq ingest item(s) (budget ${INGEST_TIMEOUT_S}s)"
  start_s="$(date +%s)"
  last_terminal=-1
  while :; do
    now_s="$(date +%s)"; elapsed=$(( now_s - start_s ))
    if ! snap="$(get_json /api/v1/ingest-queue)"; then
      log "ingest queue became unreachable"; exit 70
    fi
    read -r registered terminal failed done_count total < <(python3 -c "
import json,sys
d=json.loads(sys.argv[1]); wanted=json.loads(sys.argv[2])
by_id={item.get('id'):item for item in d.get('items',[]) if isinstance(item,dict)}
found=[by_id[item_id] for item_id in wanted if item_id in by_id]
statuses=[item.get('status') for item in found]
terminal_states={'done','error','cancelled'}
print(len(found), sum(s in terminal_states for s in statuses),
      sum(s in {'error','cancelled'} for s in statuses),
      sum(s == 'done' for s in statuses), len(wanted))
" "$snap" "$CURRENT_BATCH_IDS_JSON")
    if [[ "$registered" -eq "$total" && "$terminal" -eq "$total" ]]; then
      log "owned batch terminal: done=$done_count failed=$failed total=$total in ${elapsed}s"
      if [[ "$failed" -ne 0 ]]; then
        exit 72
      fi
      break
    fi
    if [[ "$terminal" != "$last_terminal" ]]; then
      log "  owned ingest progress: registered=$registered/$total terminal=$terminal/$total elapsed=${elapsed}s"
      last_terminal="$terminal"
    fi
    if [[ "$elapsed" -ge "$INGEST_TIMEOUT_S" ]]; then
      log "owned batch did not finish within ${INGEST_TIMEOUT_S}s (registered=$registered/$total terminal=$terminal/$total)"
      exit 72
    fi
    sleep 2
  done
else
  # Sealed mode owns the entire isolated queue, including its history.
  log "watching isolated ingest queue until active==false (budget ${INGEST_TIMEOUT_S}s)"
  start_s="$(date +%s)"
  while :; do
    now_s="$(date +%s)"; elapsed=$(( now_s - start_s ))
    if ! snap="$(get_json /api/v1/ingest-queue)"; then
      log "isolated ingest queue became unreachable"; exit 70
    fi
    read -r active errcount done_count total < <(python3 -c "
import json,sys
s=json.loads(sys.argv[1]).get('summary',{})
print(s.get('active',False), s.get('error',0), s.get('done',0), s.get('total',0))
" "$snap")
    if [[ "$active" == "False" || "$active" == "false" ]]; then
      log "isolated queue drained: done=$done_count error=$errcount total=$total in ${elapsed}s"
      [[ "$errcount" == "0" ]] || exit 72
      break
    fi
    if [[ "$elapsed" -ge "$INGEST_TIMEOUT_S" ]]; then
      log "isolated queue did not drain within ${INGEST_TIMEOUT_S}s (done=$done_count/$total)"
      exit 72
    fi
    sleep 2
  done
fi

# A populated shared endpoint is reusable only when its complete Document byte
# identity matches the selected dataset.  This fails closed before any question
# is asked, preventing a plausible 0/0 or wrong-corpus result from being scored.
if [[ "$LEAVE_UP" == "1" ]]; then
  IDENTITY_OUT="$RESULTS_DIR/dataset-identity.json"
  log "verifying endpoint dataset identity -> $(basename "$IDENTITY_OUT")"
  if ! PY assert-endpoint-dataset "$DATASET_DIR" \
      --endpoint "$OKTO_NEURON_ENDPOINT" --out "$IDENTITY_OUT" >/dev/null; then
    log "endpoint dataset identity MISMATCH or unverifiable; refusing to score this graph"
    exit 74
  fi
  log "endpoint dataset identity MATCH"
fi

# ── 6. graph shape snapshot ─────────────────────────────────────────────────
if ! get_json /api/v1/node-types > "$RESULTS_DIR/node-types.json"; then
  log "node-types snapshot FAILED"; exit 70
fi
log "node-types snapshot written"

# ── 7. run questions (recall + ask), in tier order ──────────────────────────
log "running questions from $QUESTIONS"
[[ -n "$RETRIEVAL_POLICY" ]] && log "ask retrieval_policy (A/B arm): $RETRIEVAL_POLICY"
# iterate question ids in tier order via python, emitting tab-separated id<TAB>tier<TAB>k<TAB>question
python3 -c "
import json,sys
d=json.loads(sys.argv[1])
defk=int(sys.argv[2])
qs=d.get('questions') or []
def tier_num(t):
    s=str(t).upper().lstrip('T')
    try: return int(s)
    except: return 99
for q in sorted(qs, key=lambda x: (tier_num(x.get('tier','T9')), str(x.get('id')))):
    k=q.get('k') or defk
    print('\t'.join([str(q.get('id')), str(q.get('tier','')), str(k), json.dumps(q.get('question',''))]))
" "$QJSON" "$DEFAULT_K" > "$WORK_DIR/qorder.tsv"

qcount=0
# Transport-failure gate: `post_json ... || echo '{}'` deliberately tolerates a
# TRANSIENT ask failure (one flaky LLM timeout must not kill a 50-min run), but a
# dead ask path (daemon 500ing every request — e.g. litellm pruned from the venv
# under the daemon, 2026-07-07) must abort loudly instead of recording a full run
# of empty `"ask": {}` answers that measure nothing.
ask_fail_streak=0
while IFS=$'\t' read -r qid tier k qjson; do
  [[ -z "$qid" ]] && continue
  question="$(python3 -c "import json,sys;print(json.loads(sys.argv[1]))" "$qjson")"
  recall_body="$(python3 -c "import json,sys;print(json.dumps({'query':sys.argv[1],'k':int(sys.argv[2])}))" "$question" "$k")"
  # Splice the A/B arm's retrieval_policy into the ask body (empty = daemon/config
  # default). This is the SOLE per-arm difference — recall body is untouched.
  ask_body="$(python3 -c "
import json,sys
body={'question':sys.argv[1],'k':int(sys.argv[2])}
pol=sys.argv[3]
if pol.strip():
    body['retrieval_policy']=json.loads(pol)
print(json.dumps(body))
" "$question" "$k" "$RETRIEVAL_POLICY")"
  recall_started_ns="$(monotonic_ns)"
  recall_resp="$(post_json /api/v1/recall "$recall_body" "$GOLDEN_RECALL_TIMEOUT_S" || echo '{}')"
  recall_finished_ns="$(monotonic_ns)"
  recall_elapsed_ms=$(( (recall_finished_ns - recall_started_ns) / 1000000 ))
  if ! python3 -c "
import json,sys
d=json.loads(sys.argv[1] or '{}')
c=d.get('recall_cost') if isinstance(d,dict) else None
ok=(isinstance(c,dict) and c.get('schema_version')=='recall_cost.v1'
    and c.get('measurement_status')=='measured')
raise SystemExit(0 if ok else 1)
" "$recall_resp"; then
    log "FATAL: Q $qid recall returned no measured recall_cost.v1 evidence; aborting before /ask"
    exit 73
  fi
  ask_started_ns="$(monotonic_ns)"
  ask_resp="$(post_json /api/v1/ask "$ask_body" "$GOLDEN_ASK_TIMEOUT_S" || echo '{}')"
  ask_finished_ns="$(monotonic_ns)"
  ask_elapsed_ms=$(( (ask_finished_ns - ask_started_ns) / 1000000 ))
  if [[ "$ask_resp" == "{}" ]]; then
    ask_fail_streak=$((ask_fail_streak + 1))
    log "  Q $qid ask TRANSPORT FAILURE (HTTP error; recorded as {}) — streak $ask_fail_streak"
    if (( ask_fail_streak >= 3 )); then
      log "FATAL: $ask_fail_streak consecutive /api/v1/ask failures — the daemon is erroring on every question; aborting instead of producing an all-empty run. Check the daemon log for the underlying exception."
      exit 1
    fi
  else
    ask_fail_streak=0
  fi
  # append one jsonl record
  python3 -c "
import json,sys
rec={
  'id':sys.argv[1],'tier':sys.argv[2],'k':int(sys.argv[3]),'question':sys.argv[4],
  'recall':json.loads(sys.argv[5] or '{}'),
  'ask':json.loads(sys.argv[6] or '{}'),
  'timing':{
    'schema_version':'golden_http_timing.v1',
    'recall_elapsed_ms':int(sys.argv[7]),
    'ask_elapsed_ms':int(sys.argv[8]),
  },
}
print(json.dumps(rec))
" "$qid" "$tier" "$k" "$question" "$recall_resp" "$ask_resp" \
  "$recall_elapsed_ms" "$ask_elapsed_ms" >> "$RESPONSES"
  qcount=$((qcount+1))
  log "  Q $qid ($tier) done"
done < "$WORK_DIR/qorder.tsv"
log "ran $qcount questions"
printf '%s\n' "$qcount" > "$RESULTS_DIR/responses.complete"

# ── 8. measured recall-cost + semantic-quality capture ──────────────────────
# The server owns all metric definitions.  The harness supplies every captured
# recall_cost.v1 row and requires the endpoint to account for all of them before
# any live run can be reported as complete.
SEMANTIC_QUALITY="$RESULTS_DIR/semantic-quality.json"
log "capturing semantic quality + measured recall cost -> $(basename "$SEMANTIC_QUALITY")"
if ! PY semantic-quality --endpoint "$OKTO_NEURON_ENDPOINT" \
    --responses "$RESPONSES" --out "$SEMANTIC_QUALITY"; then
  log "semantic-quality capture FAILED or incomplete; refusing a partial live report"
  exit 73
fi

# ── 9. deterministic provenance checks ──────────────────────────────────────
log "deterministic provenance byte-hash + gold-span checks"
if ! PY provenance --inputs "$INPUTS_DIR" --responses "$RESPONSES" \
    --questions "$QUESTIONS" --out "$RESULTS_DIR/deterministic.json"; then
  log "deterministic provenance command FAILED"
  exit 73
fi
[[ -s "$RESULTS_DIR/deterministic.json" ]] || {
  log "deterministic provenance produced no sidecar"
  exit 73
}

# ── 10. scripted LLM judge (optional only via explicit --no-judge) ───────────
JUDGE_ARG=""
if [[ "$RUN_JUDGE" == "1" ]]; then
  LLM_BASE="${OKTO_NEURON_LLM_BASE_URL:-}"
  if [[ -z "${LLM_BASE//[[:space:]]/}" ]]; then
    log "scripted judge requires OKTO_NEURON_LLM_BASE_URL"
    exit 64
  fi
  JUDGE_MODEL="${OKTO_NEURON_JUDGE_MODEL:-unsloth/Qwen3.6-27B-NVFP4}"
  log "scripted judge against $LLM_BASE model=$JUDGE_MODEL"
  if ! PY judge --questions "$QUESTIONS" --responses "$RESPONSES" \
      --out "$RESULTS_DIR/judge.json" --base-url "$LLM_BASE" \
      --model "$JUDGE_MODEL" --inputs "$DATASET_DIR/inputs"; then
    log "scripted judge command FAILED"
    exit 73
  fi
  [[ -s "$RESULTS_DIR/judge.json" ]] || {
    log "scripted judge produced no sidecar"
    exit 73
  }
  JUDGE_ARG="--judge $RESULTS_DIR/judge.json"
else
  log "grader disabled (--no-judge); live /ask calls were still executed"
fi

# ── 11. merge into report.json ──────────────────────────────────────────────
# shellcheck disable=SC2086
if ! PY report --dataset "$DATASET" --timestamp "$TS" \
  --responses "$RESPONSES" \
  --deterministic "$RESULTS_DIR/deterministic.json" \
  --semantic-quality "$SEMANTIC_QUALITY" \
  $JUDGE_ARG \
  --node-types "$RESULTS_DIR/node-types.json" \
  --out "$RESULTS_DIR/report.json"; then
  log "report merge FAILED; refusing to announce an incomplete live run"
  exit 73
fi

log "DONE — report at $RESULTS_DIR/report.json"
echo "$RESULTS_DIR/report.json"
