#!/usr/bin/env bash
# Scenario 99: the M3 backend-selection surface (spec section 4, last bullet)
# — default vs. explicit `--backend ladybug`, a real third-party
# `--backend stub` vault exercised end to end across a server restart, the
# `POST /api/v1/vaults` loopback gate, and `onboard`'s remote-storage consent
# + registry-validation ordering for a typed-but-unregistered backend
# (`neo4j`).
#
# `kg init` here means the flattened TOP-LEVEL `kg init` command
# (`_install_kg_compat_commands` copies every `marginalia` top-level command
# — including `init`, the full vault-scaffold one — onto `kg_cli` FIRST, then
# only adds `kg_group` commands whose name isn't already taken; `kg_group`'s
# OWN "init" ["Initialize a vault graph."], the lighter D-46 target reachable
# as `okto-neuron kg init`, is shadowed and never reachable as bare `kg init`).
# So `kg init PATH` and `okto-neuron init PATH` are the SAME command
# (`cli/__init__.py`'s `init()`), which is why items 1 and 2 below produce
# the same `storage:` shape when the harness pins ladybug — `--backend`
# defaults to `DEFAULT_NEW_VAULT_BACKEND`, `"grafx"` since D-94 (it was
# `"ladybug"` pre-D-94), not a literal always-ladybug default.
#
# Part 4 (the spoofed non-loopback 403) cannot be exercised through a real
# `okto-neuron serve` subprocess: the harness binds it to 127.0.0.1 only, and
# `remote_config_allowed`/`request_is_loopback` (server/http.py) read the
# ASGI `request.client.host` populated by the real TCP peer address — a
# genuine loopback socket connection can never present a non-loopback peer,
# and forging one would need real routable network access this sandbox does
# not have (and must not — never write a LAN IP into a repo file). The
# established, non-mocked way this codebase already tests this exact gate is
# `starlette.testclient.TestClient`'s `client=(host, port)` override
# (`tests/server/test_http_security_gates.py`): the REAL production ASGI app
# (`build_rest_app`), REAL registry, REAL filesystem calls — only the
# simulated transport's reported peer address differs from a real socket's.
# Do not "fix" this into a plain curl call; it would silently stop testing
# the gate it exists to prove.
set -uo pipefail
SCENARIO_NAME="99_backend_selection"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
source "$SCRIPT_DIR/_preamble.sh"

# Part 5/6 below always run `okto-neuron onboard --backend neo4j`, regardless
# of whatever backend the harness itself was invoked with
# (`bin/acceptance.sh --backend NAME`) -- _preamble.sh's own `--extra neo4j`
# sync is conditional on that harness-level flag, so this scenario needs its
# own additive sync here too: since M5 registers "neo4j"
# (`store/registry.py`'s `_OFFICIAL["neo4j"]`), `_resolve_and_pin_backend`
# now actually imports `marginalia.store.neo4j`, which needs the real
# `neo4j` driver installed -- without this sync, Part 6 (the
# consent-granted, now-succeeds case) fails with a missing-extra
# `click.BadParameter` instead of exercising the real onboarding path.
# grafx and ladybug are BASE package dependencies (D-94 step 1), so unlike
# pre-D-94, this additive `--extra neo4j` sync needs no `--extra grafx`
# companion -- a bare `--group litellm`-free `uv sync --extra neo4j` can
# never prune okto-grafx, since it isn't extras-gated any more.
uv_sync_neo4j_args=(--quiet --python 3.12 --extra neo4j)
if ! (
  cd "$REPO_ROOT" || exit 1
  uv sync "${uv_sync_neo4j_args[@]}"
) >"$work_dir/uv-sync-neo4j.log" 2>&1; then
  log "uv sync --extra neo4j failed; see $work_dir/uv-sync-neo4j.log"
  _failures+=("uv_sync_neo4j_failed")
  finish
fi
unset uv_sync_neo4j_args

# ---------------------------------------------------------------------------
# Part 1: default `kg init` (no explicit --backend on THIS command's own
# argv) pins whatever the harness effectively defaults to -- unless the
# harness itself was invoked with `bin/acceptance.sh --backend NAME`, in
# which case `kg_init_backend_args()` splices that name in for every init
# call site in the default suite (bin/acceptance.sh's own contract:
# --backend NAME pins the effective default backend for the whole run).
# When nothing was pinned, the CLI's own DEFAULT_NEW_VAULT_BACKEND governs
# unchanged -- "grafx" since D-94 (it was "ladybug" pre-D-94). So the
# "default" this part proves is whatever the harness pinned, grafx when
# nothing was pinned -- the assertion below tracks that, it does not
# hardcode either name.
# ---------------------------------------------------------------------------
_effective_default_backend="${OKTO_NEURON_ACCEPTANCE_BACKEND:-grafx}"
VAULT_DEFAULT="$work_dir/vault_default"
log "kg init $VAULT_DEFAULT (no --backend; effective default=$_effective_default_backend)"
kg init "$VAULT_DEFAULT" $(kg_init_backend_args) >"$work_dir/init_default.stdout" 2>"$work_dir/init_default.stderr"
assert_exit_code 0 $?
assert_file_exists "$VAULT_DEFAULT/okto-neuron.yaml"
assert_contains "$VAULT_DEFAULT/okto-neuron.yaml" '^storage:$'
assert_contains "$VAULT_DEFAULT/okto-neuron.yaml" "^  backend: ${_effective_default_backend}\$"

# ---------------------------------------------------------------------------
# Part 2: explicit `okto-neuron init --backend ladybug` — same pin.
# ---------------------------------------------------------------------------
VAULT_LADYBUG="$work_dir/vault_ladybug"
log "okto-neuron init $VAULT_LADYBUG --backend ladybug"
okto-neuron init "$VAULT_LADYBUG" --backend ladybug \
  >"$work_dir/init_ladybug.stdout" 2>"$work_dir/init_ladybug.stderr"
assert_exit_code 0 $?
assert_file_exists "$VAULT_LADYBUG/okto-neuron.yaml"
assert_contains "$VAULT_LADYBUG/okto-neuron.yaml" '^storage:$'
assert_contains "$VAULT_LADYBUG/okto-neuron.yaml" '^  backend: ladybug$'
# `vault_id` differs (derived from the directory name), so compare only the
# storage block itself — that is the "same" the spec's bullet means.
# This equivalence (default init's storage block == explicit `--backend
# ladybug`'s storage block) only holds when the effective default really is
# ladybug. Under `bin/acceptance.sh --backend NAME` for a non-ladybug NAME,
# VAULT_DEFAULT is legitimately pinned to NAME (see Part 1 above) while
# VAULT_LADYBUG stays pinned to ladybug by its own explicit, untouched
# `--backend ladybug` flag -- the two are then supposed to differ, so skip
# the comparison cleanly instead of asserting a false equivalence.
if [[ "$_effective_default_backend" == "ladybug" ]]; then
  default_storage="$(awk '/^storage:$/{f=1} f{print; if(/^  reason:/) exit}' "$VAULT_DEFAULT/okto-neuron.yaml")"
  ladybug_storage="$(awk '/^storage:$/{f=1} f{print; if(/^  reason:/) exit}' "$VAULT_LADYBUG/okto-neuron.yaml")"
  if [[ "$default_storage" != "$ladybug_storage" ]]; then
    _failures+=("storage_block_mismatch default='$default_storage' explicit='$ladybug_storage'")
    log "FAIL default and explicit --backend ladybug storage blocks differ"
  fi
else
  log "default-vs-explicit-ladybug storage comparison SKIPPED -- effective default backend is '${_effective_default_backend}', not ladybug"
fi

# ---------------------------------------------------------------------------
# Part 3: `--backend stub` — a real, out-of-tree GraphStore
# (tests/fixtures/stub_backend_pkg, installed via the dev group's
# [tool.uv.sources] entry, M3 spec section 2.11) — opens, kg add + kg query
# work, and the graph survives a full server restart (a fresh OS process,
# not an in-process reopen).
# ---------------------------------------------------------------------------
VAULT_STUB="$work_dir/vault_stub"
log "okto-neuron init $VAULT_STUB --backend stub"
okto-neuron init "$VAULT_STUB" --backend stub \
  >"$work_dir/init_stub.stdout" 2>"$work_dir/init_stub.stderr"
assert_exit_code 0 $?
assert_file_exists "$VAULT_STUB/okto-neuron.yaml"
assert_contains "$VAULT_STUB/okto-neuron.yaml" '^  backend: stub$'

STUB_QUERY_TEXT="Okto Neuron stub backend selection fixture"
STUB_BASELINE_PATHS="$work_dir/stub_baseline_paths.json"

_stub_query_paths() {
  local stage="$1"
  local out="$work_dir/stub_query_${stage}.json"
  log "kg query --format json ($stage)"
  kg query "$STUB_QUERY_TEXT" --endpoint "$OKTO_NEURON_ENDPOINT" --format json \
    >"$out" 2>"$work_dir/stub_query_${stage}.stderr"
  local rc=$?
  assert_exit_code 0 "$rc"
  if [[ "$rc" -ne 0 ]]; then tail -40 "$work_dir/stub_query_${stage}.stderr" >&2; fi
  python3 - "$out" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    hits = json.load(fh)
paths = sorted({hit.get("path") for hit in hits if hit.get("path")})
if not paths:
    print("zero hits", file=sys.stderr)
    sys.exit(1)
print("\n".join(paths))
PY
}

if ! start_server "$VAULT_STUB"; then
  _failures+=("server_start_failed vault=stub phase=seed")
  finish
fi

SEED_DIR="$work_dir/stub_seed"
mkdir -p "$SEED_DIR"
SEED_NOTE="$SEED_DIR/stub-backend.md"
cat > "$SEED_NOTE" <<EOF
# Stub Backend Selection
$STUB_QUERY_TEXT note describing volcanic ash dispersal over mountain valleys.
EOF

log "kg add $SEED_NOTE"
kg add "$SEED_NOTE" --endpoint "$OKTO_NEURON_ENDPOINT" \
  >"$work_dir/stub_add.stdout" 2>"$work_dir/stub_add.stderr"
assert_exit_code 0 $?

if ! _stub_query_paths "before_restart" >"$STUB_BASELINE_PATHS.txt"; then
  _failures+=("stub_query_before_restart_empty_or_failed")
fi

stop_server

if ! start_server "$VAULT_STUB"; then
  _failures+=("server_start_failed vault=stub phase=restart")
  finish
fi

if ! _stub_query_paths "after_restart" >"$work_dir/stub_after_restart_paths.txt"; then
  _failures+=("stub_query_after_restart_empty_or_failed")
fi

if ! diff -q "$STUB_BASELINE_PATHS.txt" "$work_dir/stub_after_restart_paths.txt" >/dev/null 2>&1; then
  _failures+=("stub_note_path_set_changed_after_restart")
  log "FAIL note path set changed across the stub-backend server restart"
  diff "$STUB_BASELINE_PATHS.txt" "$work_dir/stub_after_restart_paths.txt" >&2 || true
fi

stop_server

# ---------------------------------------------------------------------------
# Part 4: a spoofed non-loopback POST /api/v1/vaults with a backend payload
# -> 403, and no vault is created (see header comment for why this is an
# in-process ASGI TestClient call against the real app, not a curl).
# ---------------------------------------------------------------------------
log "spoofed non-loopback POST /api/v1/vaults (in-process ASGI, real app + registry)"
python3 - "$work_dir/spoofed_vault_create.json" \
  >"$work_dir/spoofed_check.stdout" 2>"$work_dir/spoofed_check.stderr" <<'PY'
import json
import sys
from pathlib import Path

from starlette.testclient import TestClient

from okto_neuron.server.http import build_rest_app
from okto_neuron.server.state import init_state, reset_state_for_tests
from okto_neuron.vault_registry import ensure_global_layout, vault_path_for_name

out_path = Path(sys.argv[1])
vault_name = "spoofed-backend-selection-vault"

reset_state_for_tests()
state = init_state(None, None)
app = build_rest_app(state)
try:
    # A real remote peer, simulated only in the transport's reported address
    # (203.0.113.7 is TEST-NET-3, RFC 5737 — never a routable real host).
    with TestClient(
        app, base_url="http://127.0.0.1", client=("203.0.113.7", 5555)
    ) as client:
        response = client.post(
            "/api/v1/vaults", json={"name": vault_name, "backend": "ladybug"}
        )
finally:
    reset_state_for_tests()

ensure_global_layout()
target_path = vault_path_for_name(vault_name)
body = response.json()
result = {
    "status_code": response.status_code,
    "body": body,
    "vault_path": str(target_path),
    "vault_created": target_path.exists(),
}
out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
print(json.dumps(result, indent=2))

ok = (
    result["status_code"] == 403
    and body.get("error") == "forbidden"
    and result["vault_created"] is False
)
sys.exit(0 if ok else 1)
PY
spoof_rc=$?
assert_exit_code 0 "$spoof_rc"
if [[ "$spoof_rc" -ne 0 ]]; then
  log "FAIL spoofed non-loopback vault-create check; see spoofed_check.stdout/.stderr"
  cat "$work_dir/spoofed_check.stdout" "$work_dir/spoofed_check.stderr" >&2
fi

# ---------------------------------------------------------------------------
# Part 5: `onboard --backend neo4j --storage-uri <remote>` non-interactively
# without consent -> exit 1, naming the confirming flags in stderr.
#
# Ordering note (verified against live code, not assumed): onboard's remote-
# storage consent check (`_confirm_remote_storage_endpoint`) runs BEFORE the
# backend-name registry check (`_resolve_and_pin_backend`, reached only once
# `_onboarding_vault` is called) — so an unconfirmed remote endpoint fails
# here regardless of whether the backend name would itself be valid.
# ---------------------------------------------------------------------------
log "okto-neuron onboard --backend neo4j --storage-uri ... (no consent)"
okto-neuron onboard --vault "noconsent-backend-vault" --backend neo4j \
  --storage-uri bolt://remote.example:7687 --non-interactive --provider skip \
  >"$work_dir/onboard_noconsent.stdout" 2>"$work_dir/onboard_noconsent.stderr"
noconsent_rc=$?
assert_exit_code 1 "$noconsent_rc"
assert_contains "$work_dir/onboard_noconsent.stderr" 'allow-remote-db'
assert_contains "$work_dir/onboard_noconsent.stderr" '\-\-yes'
if [[ "$noconsent_rc" -ne 1 ]]; then cat "$work_dir/onboard_noconsent.stderr" >&2; fi

# ---------------------------------------------------------------------------
# Part 6: the same, with --allow-remote-db --yes, --storage-credential-env
# naming a OKTO_NEURON_* env var, and a dummy secret value in that env var ->
# the consent gate passes and, since M5 registers "neo4j"
# (`store/registry.py`'s `_OFFICIAL["neo4j"]`), `_onboarding_vault` succeeds:
# a real vault is scaffolded and pinned to the neo4j backend (verified
# empirically: `Vault.scaffold`/`_write_config` only writes `okto-neuron.yaml`
# here -- no live `Neo4jStore` connection is attempted at onboarding time, so
# a real Neo4j endpoint is not required for this assertion). The written
# config carries only the credential env var's NAME
# (`storage.credential_env`), never the dummy secret VALUE -- confirmed by a
# clean grep of the entire scenario HOME tree.
# ---------------------------------------------------------------------------
DUMMY_SECRET="dummy-marginalia-secret-9f3c2b1a-do-not-leak"
log "okto-neuron onboard --backend neo4j --storage-uri ... --allow-remote-db --yes (consent granted, backend registered by M5)"
OKTO_NEURON_NEO4J_PASSWORD="$DUMMY_SECRET" okto-neuron onboard \
  --vault "consent-backend-vault" --backend neo4j \
  --storage-uri bolt://remote.example:7687 \
  --storage-credential-env OKTO_NEURON_NEO4J_PASSWORD \
  --allow-remote-db --yes \
  --non-interactive --provider skip \
  >"$work_dir/onboard_consent.stdout" 2>"$work_dir/onboard_consent.stderr"
consent_rc=$?
assert_exit_code 0 "$consent_rc"
if [[ "$consent_rc" -ne 0 ]]; then cat "$work_dir/onboard_consent.stderr" >&2; fi

assert_file_exists "$HOME/.okto-neuron/vaults"
CONSENT_VAULT_CONFIG="$HOME/.okto-neuron/vaults/consent-backend-vault/okto-neuron.yaml"
if [[ ! -e "$CONSENT_VAULT_CONFIG" ]]; then
  _failures+=("onboard_consent_case_did_not_write_a_vault")
  log "FAIL no okto-neuron.yaml was written for the neo4j onboard attempt: $CONSENT_VAULT_CONFIG"
else
  assert_contains "$CONSENT_VAULT_CONFIG" '^storage:$'
  assert_contains "$CONSENT_VAULT_CONFIG" '^  backend: neo4j$'
  assert_contains "$CONSENT_VAULT_CONFIG" '^  uri: bolt://remote.example:7687$'
  assert_contains "$CONSENT_VAULT_CONFIG" '^  credential_env: OKTO_NEURON_NEO4J_PASSWORD$'
  assert_contains "$CONSENT_VAULT_CONFIG" '^  allow_remote: true$'
fi

if grep -rlF -- "$DUMMY_SECRET" "$HOME" >"$work_dir/secret_leak_grep.out" 2>/dev/null; then
  _failures+=("dummy_secret_leaked_into_home_tree")
  log "FAIL dummy secret value found under HOME:"
  cat "$work_dir/secret_leak_grep.out" >&2
else
  log "confirmed: dummy secret value not found anywhere under HOME"
fi
_assertions=$((_assertions+1))

finish
