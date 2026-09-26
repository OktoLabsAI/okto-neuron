#!/usr/bin/env bash
# Scenario 93: task #9 security-hardening gates, exercised against the REAL
# `okto-neuron serve` binary end-to-end (no mocks). Regression-locks the four
# controls security #7 flagged and #9/#6 implemented:
#
#   (1) H1  api_key_env allowlist: PATCH /api/v1/config with a non-namespaced env
#           name (AWS_SECRET_ACCESS_KEY, PATH, OPENAI_API_KEY) -> 400; only
#           ^OKTO_NEURON_[A-Z0-9_]+$ accepted.
#   (2) L2  non-loopback bind refuses to start (exit 2).
#   (2b)    the compatibility --allow-remote flag also refuses until a tested
#           TLS/trusted-proxy contract exists.
#   (4) L1  spoofed Host header on a loopback-bound server -> 403 forbidden_host;
#           loopback Host -> 200.
#
# The remote-PEER deny for config-PATCH + MCP remember/init_vault (M1, item 3) cannot be
# triggered over a loopback socket (the peer IP is always 127.0.0.1 locally);
# it is regression-locked in pytest (test_security_hardening_qa.py +
# test_mcp_loopback_gate.py). Here we assert the loopback caller is permitted,
# which is the same gate's allow path.
set -uo pipefail
SCENARIO_NAME="93_security_hardening"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
source "$SCRIPT_DIR/_lib.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/_preamble.sh"

# helper: direct credential-free REST status
_status() {
  local output="$1"
  shift
  curl -s -o "$output" -w "%{http_code}" "$@"
}

# ── (1) + (4) + (3-allow) against a loopback-bound real server ───────────────
VAULT="$work_dir/vault"
kg init "$VAULT" $(kg_init_backend_args) >"$work_dir/init.out" 2>"$work_dir/init.err"
assert_exit_code 0 $?
export OKTO_NEURON_ENV_FILE="$work_dir/user-env"

if ! start_server "$VAULT"; then
  _failures+=("start_server failed; see $work_dir/server.log")
  finish
fi
EP="$OKTO_NEURON_ENDPOINT"

log "(0) direct UI/REST needs no browser credential and emits no auth cookie"
ui_code=$(curl -sS -D "$work_dir/ui.headers" -o "$work_dir/ui.html" -w "%{http_code}" "$EP/")
assert_exit_code 200 "$ui_code"
assert_contains "$work_dir/ui.html" '<div id="root">'
if grep -qi '^set-cookie:' "$work_dir/ui.headers"; then
  _failures+=("direct UI emitted a Set-Cookie header")
else
  _assertions=$((_assertions+1))
fi

log "(0b) cross-site browser writes are rejected"
code=$(_status "$work_dir/cross-origin.json" -X POST "$EP/api/v1/ingest-cancel" \
  -H 'Origin: https://attacker.example' -H 'Content-Type: application/json' -d '{}')
assert_exit_code 403 "$code"
assert_contains "$work_dir/cross-origin.json" "forbidden_origin"

log "(0c) non-JSON writes are rejected before route dispatch"
code=$(_status "$work_dir/non-json.json" -X POST "$EP/api/v1/ingest-cancel" \
  -H 'Content-Type: application/x-www-form-urlencoded' -d 'submit=1')
assert_exit_code 415 "$code"
assert_contains "$work_dir/non-json.json" "unsupported_media_type"

log "(1) H1: api_key_env allowlist rejects non-namespaced env names"
for bad in AWS_SECRET_ACCESS_KEY PATH OPENAI_API_KEY marginalia_lower; do
  code=$(_status "$work_dir/k.json" -X PATCH "$EP/api/v1/config" \
    -H 'Content-Type: application/json' -d "{\"llm\":{\"defaults\":{\"api_key_env\":\"$bad\"}}}")
  log "    api_key_env=$bad -> HTTP $code"
  assert_exit_code 400 "$code"
done
log "(1) H1: a namespaced env name is accepted"
code=$(_status "$work_dir/k.json" -X PATCH "$EP/api/v1/config" \
  -H 'Content-Type: application/json' -d '{"llm":{"defaults":{"api_key_env":"OKTO_NEURON_LLM_KEY"}}}')
assert_exit_code 200 "$code"
# never persists a literal secret — only the env-NAME
grep -q "OKTO_NEURON_LLM_KEY" "$VAULT/okto-neuron.yaml" \
  || _failures+=("api_key_env not persisted to okto-neuron.yaml")

log "(1b) H1: a LITERAL api_key (not env-name) is rejected -> 400"
code=$(_status "$work_dir/lit.json" -X PATCH "$EP/api/v1/config" \
  -H 'Content-Type: application/json' -d '{"llm":{"defaults":{"api_key":"sk-secret"}}}')
assert_exit_code 400 "$code"

log "(1c) managed LLM credential: key is stored out-of-YAML and response is metadata-only"
managed_secret="sk-""acceptance-managed-secret"
managed_env="OKTO_NEURON_PROVIDER_OPENAI_API_KEY"
code=$(_status "$work_dir/credential.json" -X PUT "$EP/api/v1/llm/credential" \
  -H 'Content-Type: application/json' \
  -d "{\"provider\":\"openai\",\"api_base\":\"https://api.openai.com/v1\",\"api_key\":\"$managed_secret\"}")
assert_exit_code 200 "$code"
assert_contains "$work_dir/credential.json" "$managed_env"
if grep -q "$managed_secret" "$work_dir/credential.json"; then
  _failures+=("managed credential response reflected the API key")
fi
assert_contains "$OKTO_NEURON_ENV_FILE" "$managed_secret"
code=$(_status "$work_dir/credential-config.json" -X PATCH "$EP/api/v1/config" \
  -H 'Content-Type: application/json' \
  -d "{\"llm\":{\"defaults\":{\"api_key_env\":\"$managed_env\"}}}")
assert_exit_code 200 "$code"
assert_contains "$work_dir/credential-config.json" "credential_status"
if grep -q "$managed_secret" "$VAULT/okto-neuron.yaml"; then
  _failures+=("managed API key leaked into okto-neuron.yaml")
fi

log "(1d) managed LLM credential rejects newline injection"
code=$(_status "$work_dir/credential-injection.json" -X PUT "$EP/api/v1/llm/credential" \
  -H 'Content-Type: application/json' \
  -d '{"provider":"openai","api_key":"safe\nMARGINALIA_ENV_FILE=evil"}')
assert_exit_code 400 "$code"
if grep -q "OKTO_NEURON_ENV_FILE=evil" "$OKTO_NEURON_ENV_FILE"; then
  _failures+=("managed credential accepted an injected env assignment")
fi

log "(L2) packs allowlist: unknown pack -> 400, known packs -> 200"
code=$(_status "$work_dir/pk.json" -X PATCH "$EP/api/v1/config" \
  -H 'Content-Type: application/json' -d '{"packs":["core","totally-fake-pack"]}')
assert_exit_code 400 "$code"
assert_contains "$work_dir/pk.json" "unknown pack"
code=$(_status "$work_dir/pk2.json" -X PATCH "$EP/api/v1/config" \
  -H 'Content-Type: application/json' -d '{"packs":["core","research"]}')
assert_exit_code 200 "$code"

log "(L3) recall/ask k cap: k=9999 -> 400, k=100 boundary -> 200"
code=$(_status "$work_dir/kr.json" -X POST "$EP/api/v1/recall" \
  -H 'Content-Type: application/json' -d '{"query":"x","k":9999}')
assert_exit_code 400 "$code"
assert_contains "$work_dir/kr.json" "k must be <= 100"
code=$(_status "$work_dir/ka.json" -X POST "$EP/api/v1/ask" \
  -H 'Content-Type: application/json' -d '{"question":"x","k":9999}')
assert_exit_code 400 "$code"
code=$(_status "$work_dir/kb.json" -X POST "$EP/api/v1/recall" \
  -H 'Content-Type: application/json' -d '{"query":"x","k":100}')
assert_exit_code 200 "$code"

log "(L3) legacy /query /recall /ask k cap: k=9999 -> 400, k=100 boundary -> 200"
code=$(_status "$work_dir/klq.json" -X POST "$EP/query" \
  -H 'Content-Type: application/json' -d '{"query":"x","k":9999}')
assert_exit_code 400 "$code"
assert_contains "$work_dir/klq.json" "k must be <= 100"
code=$(_status "$work_dir/klq2.json" -X POST "$EP/query" \
  -H 'Content-Type: application/json' -d '{"query":"x","k":100}')
assert_exit_code 200 "$code"
code=$(_status "$work_dir/klr.json" -X POST "$EP/recall" \
  -H 'Content-Type: application/json' -d '{"query":"x","k":9999}')
assert_exit_code 400 "$code"
assert_contains "$work_dir/klr.json" "k must be <= 100"
code=$(_status "$work_dir/klr2.json" -X POST "$EP/recall" \
  -H 'Content-Type: application/json' -d '{"query":"x","k":100}')
assert_exit_code 200 "$code"
code=$(_status "$work_dir/kla.json" -X POST "$EP/ask" \
  -H 'Content-Type: application/json' -d '{"question":"x","k":9999}')
assert_exit_code 400 "$code"
assert_contains "$work_dir/kla.json" "k must be <= 100"

log "(4) L1: spoofed (non-loopback) Host header on /api/v1 -> 403 forbidden_host"
code=$(_status "$work_dir/h.json" -H "Host: attacker.example.com" "$EP/api/v1/node-types")
log "    spoofed Host -> HTTP $code"
assert_exit_code 403 "$code"
assert_contains "$work_dir/h.json" "forbidden_host"

log "(4) L1: loopback Host header -> 200"
code=$(_status "$work_dir/h2.json" -H "Host: 127.0.0.1" "$EP/api/v1/node-types")
assert_exit_code 200 "$code"

log "(4) L1: spoofed Host on POST /api/v1/reset -> 403 forbidden_host"
code=$(_status "$work_dir/rh.json" -X POST "$EP/api/v1/reset" \
  -H "Host: attacker.example.com" -H 'Content-Type: application/json' -d '{}')
log "    spoofed Host reset -> HTTP $code"
assert_exit_code 403 "$code"
assert_contains "$work_dir/rh.json" "forbidden_host"

log "(3) M1: loopback caller may write config (gate allow path)"
code=$(_status "$work_dir/c.json" -X PATCH "$EP/api/v1/config" \
  -H 'Content-Type: application/json' -d '{"llm":{"defaults":{"model":"loopback-ok"}}}')
assert_exit_code 200 "$code"

# DESTRUCTIVE — keep last in this live-server block so earlier assertions still
# have state. A loopback caller may reset the vault (gate allow path); the
# response shape is locked to {"status":"ok","wiped":true}.
log "(reset) loopback POST /api/v1/reset -> 200 wiped:true"
code=$(_status "$work_dir/rok.json" -X POST "$EP/api/v1/reset" \
  -H 'Content-Type: application/json' -d '{}')
log "    loopback reset -> HTTP $code"
assert_exit_code 200 "$code"
assert_contains "$work_dir/rok.json" '"wiped": *true'

log "(E) L1: spoofed Host on POST /api/v1/llm/test -> 403 forbidden_host"
code=$(_status "$work_dir/lt_host.json" -X POST "$EP/api/v1/llm/test" \
  -H "Host: evil.example.com" -H 'Content-Type: application/json' \
  -d '{"provider":"stub","model":"stub"}')
log "    spoofed Host llm/test -> HTTP $code"
assert_exit_code 403 "$code"
assert_contains "$work_dir/lt_host.json" "forbidden_host"

log "(E) loopback stub provider -> 200 ok:true (no network call)"
code=$(_status "$work_dir/lt_stub.json" -X POST "$EP/api/v1/llm/test" \
  -H 'Content-Type: application/json' -d '{"provider":"stub","model":"stub"}')
log "    loopback llm/test stub -> HTTP $code"
assert_exit_code 200 "$code"
assert_contains "$work_dir/lt_stub.json" '"ok"'

log "(E) H1 boundary: api_key_env with non-namespaced var -> 200 ok:false (no secret leak)"
code=$(_status "$work_dir/lt_keyenv.json" -X POST "$EP/api/v1/llm/test" \
  -H 'Content-Type: application/json' \
  -d '{"provider":"openai","model":"test","api_key_env":"AWS_SECRET_ACCESS_KEY"}')
assert_exit_code 200 "$code"
assert_contains "$work_dir/lt_keyenv.json" '"ok"'

# ── (F) embedding endpoints: same loopback/SSRF/secret gates as llm/test ──────
log "(F) L1: spoofed Host on POST /api/v1/embedding/test -> 403 forbidden_host"
code=$(_status "$work_dir/et_host.json" -X POST "$EP/api/v1/embedding/test" \
  -H "Host: evil.example.com" -H 'Content-Type: application/json' \
  -d '{"provider":"stub","model":"stub","dimension":8}')
log "    spoofed Host embedding/test -> HTTP $code"
assert_exit_code 403 "$code"
assert_contains "$work_dir/et_host.json" "forbidden_host"

log "(F) loopback stub provider -> 200 ok:true (no network call)"
code=$(_status "$work_dir/et_stub.json" -X POST "$EP/api/v1/embedding/test" \
  -H 'Content-Type: application/json' \
  -d '{"provider":"stub","model":"stub","dimension":8}')
log "    loopback embedding/test stub -> HTTP $code"
assert_exit_code 200 "$code"
assert_contains "$work_dir/et_stub.json" '"ok"'

log "(F) H1 boundary: api_key_env with non-namespaced var -> 200 ok:false (no secret leak)"
code=$(_status "$work_dir/et_keyenv.json" -X POST "$EP/api/v1/embedding/test" \
  -H 'Content-Type: application/json' \
  -d '{"provider":"openai","model":"text-embedding-test","dimension":8,"api_key_env":"AWS_SECRET_ACCESS_KEY"}')
assert_exit_code 200 "$code"
assert_contains "$work_dir/et_keyenv.json" '"ok"'

log "(F) L1: spoofed Host on POST /api/v1/embedding/reembed -> 403 forbidden_host"
code=$(_status "$work_dir/rb_host.json" -X POST "$EP/api/v1/embedding/reembed" \
  -H "Host: evil.example.com" -H 'Content-Type: application/json' -d '{}')
log "    spoofed Host embedding/reembed -> HTTP $code"
assert_exit_code 403 "$code"
assert_contains "$work_dir/rb_host.json" "forbidden_host"

log "(F) L1: spoofed Host on GET /api/v1/embedding/reembed/status -> 403 forbidden_host"
code=$(_status "$work_dir/rs_host.json" -X GET "$EP/api/v1/embedding/reembed/status" \
  -H "Host: evil.example.com")
log "    spoofed Host embedding/reembed/status -> HTTP $code"
assert_exit_code 403 "$code"
assert_contains "$work_dir/rs_host.json" "forbidden_host"

stop_server

# ── (2) L2: non-loopback bind without --allow-remote refuses to start ────────
log "(2) L2: serve --host 0.0.0.0 WITHOUT --allow-remote refuses (exit 2)"
REFUSE_VAULT="$work_dir/refuse_vault"
kg init "$REFUSE_VAULT" $(kg_init_backend_args) >/dev/null 2>&1
okto-neuron serve --vault "$REFUSE_VAULT" --host 0.0.0.0 \
  --port 0 --mcp-port 0 --foreground >"$work_dir/refuse.out" 2>&1
refuse_rc=$?
log "    exit code=$refuse_rc"
assert_exit_code 2 "$refuse_rc"
assert_contains "$work_dir/refuse.out" "refusing to bind to non-loopback host"
assert_contains "$work_dir/refuse.out" "allow-remote"

# ── (2b) --allow-remote remains fail-closed ─────────────────────────────────
log "(2b) L2: --host 0.0.0.0 WITH --allow-remote also refuses (exit 2)"
AR_VAULT="$work_dir/allowremote_vault"
kg init "$AR_VAULT" $(kg_init_backend_args) >/dev/null 2>&1
okto-neuron serve --vault "$AR_VAULT" --host 0.0.0.0 \
  --port 0 --mcp-port 0 --allow-remote --foreground >"$work_dir/allowremote.out" 2>&1
allow_remote_rc=$?
assert_exit_code 2 "$allow_remote_rc"
assert_contains "$work_dir/allowremote.out" "Direct remote serving"
assert_contains "$work_dir/allowremote.out" "SSH tunnel"

finish
