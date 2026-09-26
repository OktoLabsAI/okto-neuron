#!/usr/bin/env bash
# ============================================================================
# quality-gate.sh — THE single laptop quality gate. Three tiers, one command.
#
#   TIER 0  CI, zero-tolerance, UNCHANGED. Exactly the four steps of
#           .github/workflows/eval-gate.yml, run in-process so the laptop can
#           reproduce what CI gates. This script NEVER edits that workflow and
#           never relaxes recall_floor_baseline.json. Tier 0 failing aborts
#           everything below it.
#
#   TIER 1  Laptop, threshold-gated, the new gate. A suite-owned daemon over a
#           suite-owned vault LIVE-INGESTS datasets/semantic-adversarial/inputs/
#           (6 docs) — that live ingest is the extraction signal; a frozen vault
#           cannot see an extraction regression. Then: citation byte-verify,
#           extraction-completeness, hard-recall@k, and deterministic
#           must_contain + negative-control abstention over the 13 asks. Every
#           number is gated on COUNTS against quality_gate_baseline.json.
#
#   TIER 2  Advisory, same run, NEVER fails the gate. The reference-guided
#           semantic judge reports a band into the report. It stays advisory
#           until a measured kappa >= 0.60 against human labels is committed.
#
# ISOLATION: suite-owned vault, isolated HOME/XDG, free (non-advertised) ports,
# server-identity assertion. Never a user vault, never ~/.marginalia state,
# never the daemon on :7777.
#
# USAGE
#   ./tests/golden/bin/quality-gate.sh                  # full three-tier gate
#   ./tests/golden/bin/quality-gate.sh --tier0-only     # CI-parity, model-free
#   ./tests/golden/bin/quality-gate.sh --no-judge       # skip Tier 2 advisory
#   ./tests/golden/bin/quality-gate.sh --mint-baseline  # bless this run as the
#                                                       # PROVISIONAL baseline
#
# REQUIRED ENV (Tier 1/2 only; --tier0-only needs none):
#   OKTO_NEURON_QUALITY_GATE_API_BASE  base URL of the private-LAN LiteLLM
#     gateway, e.g. http://<private-lan-gateway>:4000. No default — the address
#     is private and must never be committed. Reports and minted baselines
#     record it as the literal "<private-lan-gateway>", never the raw value.
#
# Exit codes: 0 pass · 1 Tier 0 or Tier 1 regression · 64 usage · 70 infra.
# ============================================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GOLDEN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$GOLDEN_DIR/../.." && pwd)"
# shellcheck source=/dev/null
source "$SCRIPT_DIR/_serve.sh"

TIER0_DATASET="$GOLDEN_DIR/datasets/synthetic-ci"
TIER1_DATASET="$GOLDEN_DIR/datasets/semantic-adversarial"
BASELINE="$TIER1_DATASET/quality_gate_baseline.json"

# Pinned gate lineup. A drifting model turns the gate into a model-comparison
# instrument, so both aliases are pinned here and recorded in the report.
# OKTO_NEURON_QUALITY_GATE_API_BASE is REQUIRED for Tier 1/2 and has deliberately
# no default: the endpoint is a private-LAN address and must never live in a
# tracked file. It is checked after flag parsing so --tier0-only stays model-free.
GATE_API_BASE_ENV="OKTO_NEURON_QUALITY_GATE_API_BASE"
GATE_API_BASE="${OKTO_NEURON_QUALITY_GATE_API_BASE:-}"
# Recorded into every report/baseline in place of the raw address, so
# --mint-baseline can never re-emit a private endpoint into version control.
GATE_API_BASE_REDACTED="<private-lan-gateway>"
GATE_MODEL="${OKTO_NEURON_QUALITY_GATE_MODEL:-desktop/qwen3.6-35b-10-parallel}"
GATE_PROVIDER_REF="${OKTO_NEURON_QUALITY_GATE_PROVIDER_REF:-litellm-local-proxy}"
# EMBEDDING IS DELIBERATELY NOT OVERRIDDEN. The gate keeps the vault's own
# default local fastembed embedder: it is the same embedder Tier 0 gates against,
# it keeps the retrieval leg deterministic and offline, and re-pointing it at a
# wider remote alias would mean replacing the graph file that `okto-neuron init`
# just minted — which trips the ADR 0039 generation fence and disables semantic
# writes. Only the LLM (extraction / ask / advisory judge) is pinned to the LAN
# gateway alias below.
WARN_SECONDS="${OKTO_NEURON_QUALITY_GATE_WARN_S:-540}"   # 9 min

TIER0_ONLY=0
RUN_JUDGE=1
MINT=0
KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --tier0-only) TIER0_ONLY=1 ;;
    --no-judge) RUN_JUDGE=0 ;;
    --mint-baseline) MINT=1 ;;
    --keep) KEEP=1 ;;
    -h|--help) sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 64 ;;
  esac
  shift
done

if [[ "$TIER0_ONLY" == "0" && -z "$GATE_API_BASE" ]]; then
  echo "$GATE_API_BASE_ENV is not set." >&2
  echo "Tier 1/2 need a private-LAN LiteLLM gateway base URL; there is no default" >&2
  echo "because the address must not be committed. Export it, e.g.:" >&2
  echo "  export $GATE_API_BASE_ENV=http://<private-lan-gateway>:4000" >&2
  echo "Or run the model-free CI-parity tier: $0 --tier0-only" >&2
  exit 64
fi

START_S="$(date +%s)"
OUT_DIR="$GOLDEN_DIR/results/quality-gate/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT_DIR"
LOG="$OUT_DIR/quality-gate.log"
log() { printf '[quality-gate] %s\n' "$*" | tee -a "$LOG" >&2; }
tier() { printf '\n[quality-gate] ===== %s =====\n' "$*" | tee -a "$LOG" >&2; }

log "artifacts: $OUT_DIR"
command -v uv >/dev/null 2>&1 || { log "uv not found"; exit 70; }

# ONE explicit dependency sync, up front, from the committed lockfile. Every
# nested harness re-runs the identical `uv sync --group litellm`, so this is a
# no-op afterwards — the gate never lets a later sync prune the venv out from
# under its own running daemon (the 2026-07-07 all-500 gotcha). The closure is
# EXACTLY run-golden.sh's (`--group litellm`); ladybug arrives with the default
# `serve` group, so no nested sync can prune the store out from under the daemon.
if ! ( cd "$REPO_ROOT" && uv sync --frozen --quiet --group litellm ); then
  log "uv sync failed"; exit 70
fi
UV=(uv run --frozen --no-sync --group litellm)

# Derive the environment uv actually manages (correct under UV_PROJECT_ENVIRONMENT)
# instead of assuming $REPO_ROOT/.venv, then PROVE the resolved `marginalia` comes
# from it and imports this checkout. `command -v marginalia` alone would happily
# resolve the operator's global uv tool and this gate would report another SHA's
# behaviour as evidence for this one. Done before Tier 0 (which also runs project
# code) and before HOME is isolated, so uv resolves against the real environment.
if ! VENV_DIR="$(golden_resolve_env "$REPO_ROOT")" || [[ -z "$VENV_DIR" ]]; then
  log "could not derive the uv-managed environment"; exit 70
fi
export PATH="$VENV_DIR/bin:$PATH"
if ! golden_assert_repo_binary "$VENV_DIR" "$REPO_ROOT" 2>>"$LOG"; then
  tail -20 "$LOG" >&2
  log "venv provenance check FAILED — refusing to produce evidence from unknown code"
  exit 70
fi
log "venv provenance OK: bin=$GOLDEN_MARGINALIA_BIN prefix=$GOLDEN_MARGINALIA_PREFIX module=$GOLDEN_MARGINALIA_MODULE"
golden_write_provenance "$OUT_DIR/venv-provenance.json"

# ── TIER 0 — the CI floor, verbatim, unchanged ──────────────────────────────
tier "TIER 0 — deterministic floor (CI parity; eval-gate.yml is NOT edited)"
T0_DIR="$OUT_DIR/tier0"; mkdir -p "$T0_DIR"
t0_fail=0
( cd "$REPO_ROOT" && "${UV[@]}" python tests/golden/bin/judge.py floor \
    "$TIER0_DATASET" --out "$T0_DIR/floor_report.json" ) >>"$LOG" 2>&1 \
  || { log "TIER 0 FAIL: provenance byte-hash floor"; t0_fail=1; }
( cd "$REPO_ROOT" && "${UV[@]}" python tests/golden/bin/recall_floor.py selftest ) >>"$LOG" 2>&1 \
  || { log "TIER 0 FAIL: recall_floor selftest"; t0_fail=1; }
( cd "$REPO_ROOT" && "${UV[@]}" python tests/golden/bin/recall_floor.py gate \
    --vault "$TIER0_DATASET/frozen-vault" \
    --questions "$TIER0_DATASET/questions.yaml" \
    --baseline "$TIER0_DATASET/recall_floor_baseline.json" \
    --k 10 --out "$T0_DIR/recall_floor.json" ) >>"$LOG" 2>&1 \
  || { log "TIER 0 FAIL: hard-recall@k + extraction-completeness vs frozen vault"; t0_fail=1; }
( cd "$REPO_ROOT" && "${UV[@]}" python tests/golden/bin/judge.py manifest \
    "$TIER0_DATASET" --out "$T0_DIR/run-manifest.json" ) >>"$LOG" 2>&1 \
  || { log "TIER 0 FAIL: run manifest"; t0_fail=1; }
if [[ "$t0_fail" != "0" ]]; then
  log "TIER 0 FAILED — aborting. Tier 1 never excuses a Tier 0 failure. See $LOG"
  exit 1
fi
log "TIER 0 PASS (provenance floor · selftest · frozen-vault floor · manifest)"

# quality_gate.py's own pure-logic selftest guards the gate's scoring/denominators.
( cd "$REPO_ROOT" && "${UV[@]}" python tests/golden/bin/quality_gate.py selftest ) >>"$LOG" 2>&1 \
  || { log "quality_gate selftest FAILED"; exit 1; }

if [[ "$TIER0_ONLY" == "1" ]]; then
  log "--tier0-only: stopping after the CI-parity legs (model-free, daemon-free)"
  log "elapsed $(( $(date +%s) - START_S ))s"
  exit 0
fi

# ── TIER 1 — suite-owned isolation ──────────────────────────────────────────
tier "TIER 1 — suite-owned daemon, live ingest, gated metrics"
REAL_HOME="$HOME"
WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/marginalia-quality-gate.XXXXXX")"
VAULT="$WORK_DIR/vault"
export HOME="$WORK_DIR/home"
export XDG_CONFIG_HOME="$WORK_DIR/xdg-config"
export XDG_DATA_HOME="$WORK_DIR/xdg-data"
export XDG_STATE_HOME="$WORK_DIR/xdg-state"
export XDG_CACHE_HOME="$WORK_DIR/xdg-cache"
mkdir -p "$HOME/.okto-neuron" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$XDG_CACHE_HOME"
# UV_CACHE_DIR is deliberately NOT isolated: it is a download cache, holds no
# vault/daemon/config state, and isolating it would re-download the whole
# closure on every gate run.
#
# Provider credentials are application-scoped, not vault state. Copy ONLY the
# provider registry + secret env into the isolated HOME so the pinned gateway
# resolves; the credential value is never read, printed, or logged here.
# The app home is ~/.okto-neuron since 0.3.0; an upgraded machine may still
# keep these files in the pre-0.3.0 ~/.marginalia.
for f in providers.yaml env; do
  for src_home in "$REAL_HOME/.okto-neuron" "$REAL_HOME/.marginalia"; do
    if [[ -r "$src_home/$f" ]]; then
      cp "$src_home/$f" "$HOME/.okto-neuron/$f"
      break
    fi
  done
done
chmod 700 "$HOME/.okto-neuron"

# shellcheck disable=SC2329  # invoked by the EXIT trap below
cleanup() {
  golden_stop_server || true
  if [[ "$KEEP" == "1" ]]; then
    log "keeping suite-owned vault at $WORK_DIR (--keep)"
  else
    rm -rf "$WORK_DIR"
  fi
}
trap cleanup EXIT

# PATH + provenance were established up front (before Tier 0), deliberately
# outside the isolated HOME. Re-assert here: HOME/XDG changed since.
if ! golden_assert_repo_binary "$VENV_DIR" "$REPO_ROOT" 2>>"$LOG"; then
  tail -20 "$LOG" >&2
  log "venv provenance check FAILED after HOME isolation"; exit 70
fi

log "init suite-owned vault: $VAULT"
if ! okto-neuron init "$VAULT" >"$OUT_DIR/init.log" 2>&1; then
  log "vault init failed; see $OUT_DIR/init.log"; exit 70
fi
# Pin the gate's lineup into the vault config. The companion reads
# llm.defaults.api_base from okto-neuron.yaml (env is NOT consulted), and the
# pinned alias is the one approved parallel-capable model, so extraction is not
# clamped to a single in-flight completion.
if ! "$VENV_DIR/bin/python" - "$VAULT/okto-neuron.yaml" \
    "$GATE_API_BASE" "$GATE_MODEL" "$GATE_PROVIDER_REF" <<'PY' >>"$LOG" 2>&1
import sys, yaml
path, api_base, model, provider_ref = sys.argv[1:]
with open(path) as fh:
    cfg = yaml.safe_load(fh) or {}
cfg["llm"] = {
    "enabled": True,
    "allow_remote": True,
    "parallel_capable_models": [model],
    "defaults": {
        "provider_ref": provider_ref,
        "provider": "openai",
        "api_base": api_base,
        "model": model,
    },
    "extraction": {"temperature": 0.6, "enable_thinking": False},
}
with open(path, "w") as fh:
    yaml.safe_dump(cfg, fh, sort_keys=False)
PY
then
  log "failed to pin the gate lineup into the vault config"; exit 70
fi

REST_PORT="$(golden_free_port)"; MCP_PORT="$(golden_free_port)"
[[ "$REST_PORT" == "$MCP_PORT" ]] && MCP_PORT="$(golden_free_port)"
log "serve REST=$REST_PORT MCP=$MCP_PORT (non-advertised, suite-owned)"
if ! golden_start_server "$VAULT" "$REST_PORT" "$MCP_PORT" "$OUT_DIR/server.log"; then
  log "suite-owned daemon failed to start; see $OUT_DIR/server.log"; exit 70
fi
ENDPOINT="$OKTO_NEURON_ENDPOINT"

# Server-identity assertion: the daemon we measure must be OUR pid on OUR vault.
# Read it from the credential-free /api/v1/status, NOT /health. /health is
# deliberately "unauthenticated process liveness with no operational metadata"
# and returns only {"status":"ok"} — asserting against it means every field is
# absent, so the check passes for ANY daemon that answers the port, including a
# user daemon that grabbed it. /api/v1/status carries the real `pid` and
# `vault_path`. The check below is FAIL-CLOSED: a missing field is a failure,
# never a pass, so the assertion can never silently go vacuous again.
IDENTITY="$(timeout 20 curl -fsS "$ENDPOINT/api/v1/status" 2>/dev/null || echo '{}')"
printf '%s\n' "$IDENTITY" > "$OUT_DIR/identity.json"
if ! python3 - "$OUT_DIR/identity.json" "$VAULT" "$_GOLDEN_SERVER_PID" <<'PY'
import json, os, sys
status = json.load(open(sys.argv[1]))
vault, pid = os.path.realpath(sys.argv[2]), int(sys.argv[3])
seen = status.get("vault_path") or status.get("vault")
seen_pid = status.get("pid")
if not seen or seen_pid is None:
    print(
        "identity assertion: /api/v1/status returned no pid/vault_path "
        f"(keys={sorted(status)}) — refusing to measure an unidentified daemon",
        file=sys.stderr,
    )
    raise SystemExit(1)
ok_vault = os.path.realpath(str(seen)) == vault
ok_pid = int(seen_pid) == pid
if not (ok_vault and ok_pid):
    print(
        f"identity assertion: expected pid={pid} vault={vault}, "
        f"got pid={seen_pid} vault={seen}",
        file=sys.stderr,
    )
raise SystemExit(0 if (ok_vault and ok_pid) else 1)
PY
then
  log "server identity assertion FAILED (endpoint is not this run's daemon/vault)"; exit 70
fi
log "server identity OK: pid=$_GOLDEN_SERVER_PID vault=$VAULT endpoint=$ENDPOINT"

# ── live ingest + 13 recall/ask, via the existing black-box harness ─────────
log "live-ingesting $TIER1_DATASET/inputs (6 docs) and running 13 recall+ask"
RG_STDOUT="$("$SCRIPT_DIR/run-golden.sh" "$TIER1_DATASET" --endpoint "$ENDPOINT" --no-judge 2>>"$LOG")"
RG_STATUS=$?
RG_REPORT="$(printf '%s\n' "$RG_STDOUT" | grep -E '/report\.json$' | tail -1)"
if [[ -z "$RG_REPORT" || ! -s "$RG_REPORT" ]]; then
  # Fail LOUD, with the queue's own error text: a silent ingest failure is the
  # difference between "the gate found a regression" and "the gate measured nothing".
  timeout 20 curl -fsS "$ENDPOINT/api/v1/ingest-queue" > "$OUT_DIR/ingest-queue.json" 2>/dev/null || true
  if [[ -s "$OUT_DIR/ingest-queue.json" ]]; then
    python3 -c 'import json,sys; q=json.load(open(sys.argv[1])); print("ingest queue summary:", json.dumps(q.get("summary",{}))); [print("  %s: %s" % (i.get("name"), (i.get("error") or "")[:600])) for i in q.get("items",[]) if i.get("error")]' \
      "$OUT_DIR/ingest-queue.json" | tee -a "$LOG" >&2
  fi
  log "live ingest/ask leg failed (status=$RG_STATUS); see $LOG"; exit 70
fi
RG_DIR="$(dirname "$RG_REPORT")"
RESPONSES="$RG_DIR/responses.jsonl"
cp "$RESPONSES" "$OUT_DIR/responses.jsonl"
log "captured $(wc -l <"$RESPONSES" | tr -d ' ') responses"

# ── citation byte-verify against the LIVE vault (needs the daemon up) ───────
log "citation byte-verification (model-free) over the live vault"
( cd "$REPO_ROOT" && "${UV[@]}" python tests/golden/bin/floor_metrics.py floor-laptop \
    --responses "$OUT_DIR/responses.jsonl" --endpoint "$ENDPOINT" \
    --out "$OUT_DIR/floor_laptop.json" ) >>"$LOG" 2>&1
CITATION_STATUS=$?
[[ -s "$OUT_DIR/floor_laptop.json" ]] || { log "floor-laptop produced no report"; exit 70; }
[[ "$CITATION_STATUS" == "0" ]] || log "citation floor did NOT pass (gated below)"

# ── deterministic must_contain + negative-control abstention ───────────────
( cd "$REPO_ROOT" && "${UV[@]}" python tests/golden/bin/quality_gate.py must-contain \
    --questions "$TIER1_DATASET/questions.yaml" --responses "$OUT_DIR/responses.jsonl" \
    --out "$OUT_DIR/must_contain.json" ) 2>&1 | tee -a "$LOG" >&2
[[ "${PIPESTATUS[0]}" == "0" ]] || { log "must_contain scoring failed"; exit 70; }

# ── TIER 2 — advisory semantic judge (never gates) ──────────────────────────
JUDGE_ARG=()
if [[ "$RUN_JUDGE" == "1" ]]; then
  tier "TIER 2 — ADVISORY semantic judge (recorded, never gates)"
  # semantic_judge speaks plain OpenAI-compatible HTTP: it needs the /v1 root and
  # a bearer key in OPENAI_API_KEY. Resolve the gateway credential out of the
  # isolated home's secret file WITHOUT printing it; if it is absent the judge
  # simply reports itself skipped, which is fine because Tier 2 gates nothing.
  JUDGE_KEY="$(python3 -c '
import os, re, sys
host = re.sub(r"[^0-9a-zA-Z]", "_", sys.argv[1].split("//")[-1].rstrip("/")).lower()
path = os.path.join(os.environ["HOME"], ".okto-neuron", "env")
best = ""
try:
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if not name.endswith("_API_KEY") or host not in name.lower():
            continue
        if name.lower().startswith(("okto_neuron_provider_openai", "marginalia_provider_openai")) or not best:
            best = value.strip()
except OSError:
    pass
print(best)
' "$GATE_API_BASE")"
  if [[ -n "$JUDGE_KEY" ]]; then export OPENAI_API_KEY="$JUDGE_KEY"; fi
  unset JUDGE_KEY
  if ( cd "$REPO_ROOT" && "${UV[@]}" python tests/golden/bin/semantic_judge.py judge-file \
        --questions "$TIER1_DATASET/questions.yaml" --responses "$OUT_DIR/responses.jsonl" \
        --out "$OUT_DIR/semantic_judge.json" --base-url "${GATE_API_BASE%/}/v1" \
        --model "$GATE_MODEL" ) >>"$LOG" 2>&1 && [[ -s "$OUT_DIR/semantic_judge.json" ]]; then
    JUDGE_ARG=(--judge "$OUT_DIR/semantic_judge.json")
    log "advisory band recorded (NOT gated; kappa vs human labels is unmeasured)"
  else
    log "advisory judge unavailable — recorded as absent; the gate is unaffected"
  fi
fi

# ── stop the daemon, then read the vault directly for the floor metrics ────
# recall_floor runs the SHIPPED retrieval over the vault's own graph.lbug with
# no daemon and no LLM; the single-writer daemon must be down first.
log "stopping the suite-owned daemon before the direct-vault floor probe"
golden_stop_server || true
GRAPH="$(find "$VAULT" -maxdepth 3 -name 'graph.lbug' -print -quit 2>/dev/null)"
[[ -n "$GRAPH" ]] || { log "no graph.lbug under $VAULT"; exit 70; }
VAULT_GRAPH_DIR="$(dirname "$GRAPH")"
log "hard-recall@k + extraction-completeness over the live-ingested vault ($VAULT_GRAPH_DIR)"
( cd "$REPO_ROOT" && "${UV[@]}" python tests/golden/bin/recall_floor.py probe \
    --vault "$VAULT_GRAPH_DIR" --questions "$TIER1_DATASET/questions.yaml" \
    --k 10 --out "$OUT_DIR/recall_floor_live.json" ) >>"$LOG" 2>&1 \
  || { log "live-vault recall/extraction probe failed"; exit 70; }

# ── merge, gate, (optionally) mint ─────────────────────────────────────────
SOURCE_SHA="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
# The gateway is recorded REDACTED, never as the raw private-LAN address: this
# meta block is pinned into the report and, via --mint-baseline, into a tracked
# baseline file.
META="$(python3 - "$GATE_API_BASE_REDACTED" "$GATE_MODEL" "$SOURCE_SHA" <<'PY'
import json, sys
api_base, model, sha = sys.argv[1:]
print(json.dumps({
    "dataset": "semantic-adversarial",
    "gateway": api_base,
    "model": model,
    "embedding": "vault default (local fastembed)",
    "source_sha": sha,
    "tier0_workflow_edited": False,
}, sort_keys=True))
PY
)"
( cd "$REPO_ROOT" && "${UV[@]}" python tests/golden/bin/quality_gate.py report \
    --responses "$OUT_DIR/responses.jsonl" \
    --floor-laptop "$OUT_DIR/floor_laptop.json" \
    --recall-floor "$OUT_DIR/recall_floor_live.json" \
    --must-contain "$OUT_DIR/must_contain.json" \
    "${JUDGE_ARG[@]+"${JUDGE_ARG[@]}"}" \
    --meta "$META" --out "$OUT_DIR/quality_gate_report.json" ) 2>&1 | tee -a "$LOG" >&2
[[ "${PIPESTATUS[0]}" == "0" ]] || { log "report merge failed"; exit 70; }

RC=0
if [[ "$MINT" == "1" ]]; then
  # Mint the FLOOR across every retained run of this same tree + lineup, not just
  # this one. Extraction is stochastic (11/11 and 7/11 gold targets were both
  # observed on an unchanged tree), so a single-run ceiling baseline would fail
  # the next unchanged run and train everyone to ignore the gate.
  RUN_SCHEMA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("scoring_schema"))' "$OUT_DIR/quality_gate_report.json")"
  MINT_ARGS=()
  while IFS= read -r report; do
    MINT_ARGS+=(--report "$report")
  done < <(python3 -c '
import glob, json, os, sys
root, sha, schema = sys.argv[1], sys.argv[2], sys.argv[3]
for path in sorted(glob.glob(os.path.join(root, "*", "quality_gate_report.json"))):
    try:
        report = json.load(open(path))
    except Exception:
        continue
    # Same tree AND the same scoring semantics — a report scored by an older
    # rule set is not comparable and must not sink the floor.
    if (report.get("meta") or {}).get("source_sha") == sha and \
            str(report.get("scoring_schema")) == schema:
        print(path)
' "$GOLDEN_DIR/results/quality-gate" "$SOURCE_SHA" "$RUN_SCHEMA")
  # each report contributes two array entries (--report <path>)
  log "minting the baseline floor across $(( ${#MINT_ARGS[@]} / 2 )) run report(s) of $SOURCE_SHA"
  [[ ${#MINT_ARGS[@]} -gt 0 ]] || MINT_ARGS=(--report "$OUT_DIR/quality_gate_report.json")
  ( cd "$REPO_ROOT" && "${UV[@]}" python tests/golden/bin/quality_gate.py mint \
      "${MINT_ARGS[@]}" --source-sha "$SOURCE_SHA" \
      --minted-at "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --out "$BASELINE" ) 2>&1 | tee -a "$LOG" >&2
  [[ "${PIPESTATUS[0]}" == "0" ]] || { log "baseline mint failed"; exit 70; }
  log "PROVISIONAL baseline minted: $BASELINE"
elif [[ -f "$BASELINE" ]]; then
  tier "TIER 1 GATE — count-drop vs $BASELINE"
  ( cd "$REPO_ROOT" && "${UV[@]}" python tests/golden/bin/quality_gate.py gate \
      --report "$OUT_DIR/quality_gate_report.json" --baseline "$BASELINE" ) 2>&1 | tee -a "$LOG" >&2
  RC=${PIPESTATUS[0]}
  if [[ "$RC" == "0" ]]; then log "TIER 1 PASS (no count regression)"; else log "TIER 1 FAIL — regression vs baseline"; fi
else
  log "no baseline at $BASELINE — run with --mint-baseline to create one"
fi

ELAPSED=$(( $(date +%s) - START_S ))
log "wall time ${ELAPSED}s"
if (( ELAPSED > WARN_SECONDS )); then
  log "WARNING: the gate took ${ELAPSED}s, over the ${WARN_SECONDS}s budget"
fi
log "report: $OUT_DIR/quality_gate_report.json"
exit "$RC"
