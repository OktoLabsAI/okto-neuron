#!/usr/bin/env bash
# Scenario 56: remember() mints byte-anchored Claims, recall surfaces them.
#
# Definition of done for the marginalia-claims work. NO MOCKS: drives the REAL
# Companion.remember pipeline with an explicitly selected REAL OpenAI-compatible
# provider for LLM extraction and REAL fastembed for vectors. Asserts:
#   1. remember() of the partner note mints at least one Claim naming
#      Jordan Lee Carter, anchored to a Block whose byte range hashes back to the
#      exact source bytes (sha256(raw[bs:be]) == content_hash).
#   2. recall("who is the partner on Okto Neuron?") surfaces Jordan Lee Carter
#      WITH byte-range provenance (path + byte_start/byte_end into the note).
#
# Synthetic corpus (single partner note) — the synthetic sibling of the
# laptop-only private-corpus scenarios. Real models, no reduced corpora, no skips
# except OS-incompat (an unreachable configured server is a hard FAIL, not a skip).
# The gate explicitly enables ADR 0030's two-draw union: a single non-zero-
# temperature draw is intentionally lossy and cannot be a reliable release
# verdict for one required relationship.
set -uo pipefail
SCENARIO_NAME="56_remember_anchored_claims"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"

LLM_BASE="${OKTO_NEURON_LLM_BASE_URL:-}"
LLM_MODEL="${OKTO_NEURON_REALMODEL_MODEL:-unsloth/Qwen3.6-27B-NVFP4}"
if [[ -z "${LLM_BASE//[[:space:]]/}" ]]; then
  log "OKTO_NEURON_LLM_BASE_URL is required for live-model scenario 56"
  _failures+=("missing_prerequisite env=OKTO_NEURON_LLM_BASE_URL")
  finish
fi

# shellcheck disable=SC1091
source "$SCRIPT_DIR/_preamble.sh"

VAULT="$work_dir/vault"

log "kg init $VAULT"
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.stdout" 2>"$work_dir/init.stderr"
assert_exit_code 0 $?
assert_file_exists "$VAULT/okto-neuron.yaml"

log "onboarding real model $LLM_MODEL at $LLM_BASE"
okto-neuron onboard --vault "$VAULT" --provider custom --api-base "$LLM_BASE" \
  --model "$LLM_MODEL" --skip-model-discovery --non-interactive \
  --allow-remote-llm --yes \
  >"$work_dir/onboard.stdout" 2>"$work_dir/onboard.stderr"
assert_exit_code 0 $?

log "enabling ADR 0030 two-draw extraction union for the live release gate"
VAULT_PATH="$VAULT" python3 - <<'PY' \
  >"$work_dir/extraction-config.stdout" 2>"$work_dir/extraction-config.stderr"
import os
from pathlib import Path

from okto_neuron.config import VaultConfig

vault_path = Path(os.environ["VAULT_PATH"])
config, _changed = VaultConfig.apply_patch(
    vault_path,
    {"llm": {"extraction": {"samples": 2}}},
)
assert config.llm.extraction.samples == 2
print("llm.extraction.samples=2")
PY
assert_exit_code 0 $?
assert_contains "$work_dir/extraction-config.stdout" "llm.extraction.samples=2"

log "writing real partner note into notes/"
mkdir -p "$VAULT/notes"
cat > "$VAULT/notes/partner.md" <<'EOF'
# Okto Neuron project note

Okto Neuron is a local-first knowledge graph spun out of okto-pulse-core.
Alex is the lead author of Okto Neuron. Jordan Lee Carter is the project
partner. The schema is locked to five primitives and uses PROV-O for
provenance.
EOF
assert_file_exists "$VAULT/notes/partner.md"

log "driving REAL Companion.remember ($LLM_MODEL at $LLM_BASE + fastembed) + recall"
DRIVER_OUT="$work_dir/driver.out"
VAULT="$VAULT" python3 "$SCRIPT_DIR/_remember_anchored_claims_driver.py" \
  >"$DRIVER_OUT" 2>"$work_dir/driver.err"
driver_rc=$?
assert_exit_code 0 "$driver_rc"
if [[ "$driver_rc" -ne 0 ]]; then tail -40 "$work_dir/driver.err" >&2; fi

# Real-work assertions on the driver's machine-readable output.
assert_contains "$DRIVER_OUT" "PARTNER_CLAIM_ANCHOR_OK"
assert_contains "$DRIVER_OUT" "RECALL_PARTNER_OK"
assert_contains "$DRIVER_OUT" "Jordan Lee Carter"
# Full quality bar (team definition of done): top-1 partner Claim, no infra,
# no raw Block, single-entity dedup.
assert_contains "$DRIVER_OUT" "QUALITY_BAR_TOP1_OK"
assert_contains "$DRIVER_OUT" "QUALITY_BAR_NO_INFRA_OK"
assert_contains "$DRIVER_OUT" "QUALITY_BAR_NO_BLOCK_OK"
assert_contains "$DRIVER_OUT" "QUALITY_BAR_NO_DUPES_OK"
assert_contains "$DRIVER_OUT" "QUALITY_BAR_OK"
assert_contains "$DRIVER_OUT" "ACCEPTANCE_OK"
# Graph must have grown — real Claims/Blocks/embeddings committed.
assert_min_bytes "$VAULT/graph.lbug" 100000

finish
