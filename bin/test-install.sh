#!/usr/bin/env bash
#
# Canonical public-installer tester for Okto Neuron.
#
# Default mode runs the raw GitHub installer with an isolated HOME/XDG/uv state.
# `--tmux` and `--docker-tmux` run the same raw installer through a real TTY and
# drive prompts with tmux send-keys, producing capture-pane evidence.

set -euo pipefail

DEFAULT_URL="https://raw.githubusercontent.com/OktoLabsAI/okto-neuron/main/install.sh"
INSTALL_URL="${OKTO_NEURON_INSTALL_URL:-$DEFAULT_URL}"
TEST_HOME="${OKTO_NEURON_TEST_HOME:-}"
ORIGINAL_HOME="${HOME:-}"
VAULT="${OKTO_NEURON_VAULT:-mynotes}"
EXPECTED_VERSION="${OKTO_NEURON_EXPECTED_VERSION:-0.3.0}"
PROVIDER="${OKTO_NEURON_LLM_PROVIDER:-}"
API_BASE="${OKTO_NEURON_LLM_API_BASE:-}"
MODEL="${OKTO_NEURON_LLM_MODEL:-}"
PROFILE="interactive"
MODE="direct"
CLEANUP=0
NO_SERVE=0
SESSION="${OKTO_NEURON_TEST_SESSION:-}"
CONTAINER="${OKTO_NEURON_TEST_CONTAINER:-}"
EVIDENCE=""
SANDBOX_MARKER_NAME=".okto-neuron-test-sandbox-owner"
SANDBOX_MARKER_VALUE="okto-neuron-test-sandbox-v1"
RESOURCE_OWNER_KEY="@okto_neuron_test_owner"
DOCKER_OWNER_LABEL="com.oktolabs.okto-neuron.test-owner"
DRIVER_COMMIT="${OKTO_NEURON_TEST_DRIVER_COMMIT:-}"
DRIVER_URL=""
DRIVER_SHA256=""
INSTALL_SHA256=""
MANIFEST_URL=""
MANIFEST_SHA256=""

usage() {
  cat <<'EOF'
Usage: ./test-install.sh [options]

Runs the public Okto Neuron installer in an isolated sandbox.

Modes:
  default          Run in this terminal with isolated HOME/XDG/uv state.
  --tmux          Run on this host inside tmux and drive prompts.
  --docker-tmux   Run in a fresh Ubuntu container inside tmux and drive prompts.

Profiles for tmux modes:
  --profile skip       Choose "Skip LLM setup"; verifies no explicit llm block.
  --profile auto-lm-studio  Auto-detect a mocked LM Studio endpoint.
  --profile lm-studio  Start a mock LM Studio /v1/models endpoint and choose it.
  --profile ollama     Start a mock Ollama OpenAI-compatible endpoint and choose it.
  --profile litellm    Start a mock LiteLLM Proxy model-info endpoint and choose it.
  --profile hosted-openai  Choose OpenAI with a fake exported key and manual model.
  --profile hosted-openrouter  Choose OpenRouter with a fake exported key/model.
  --profile hosted-gemini  Choose Gemini with a fake exported key/model.
  --profile hosted-anthropic  Choose Anthropic with a fake exported key/model.
  --profile custom     Choose custom endpoint; requires --api-base and --model.
  --profile custom-rootform  Hermetic /v1-only fake OpenAI endpoint (oMLX shape,
                               non-chat model listed first): type the base in ROOT
                               form (no /v1); proves discovery + pre-save verify +
                               canonical /v1 save, then the dead-endpoint and
                               no-silent-model failure paths.
  --profile existing-keep  Preseed LLM config and choose keep existing.
  --profile existing-inspect  Preseed LLM config and test it without writing.
  --profile existing-reconfigure  Preseed LLM config and reconfigure to LM Studio.
  --profile disable-llm  Preseed LLM config and choose disable.
  --profile release-lifecycle  Run the complete Linux release lifecycle in Docker+tmux.
  --profile interactive  Do not auto-drive prompts; print tmux attach command.
  --profile onboard-prompt-yes  Greenfield + TTY: accept the first-run prompt
                               (Enter), drive `okto-neuron onboard` to a
                               user-named vault with LLM setup skipped.
  --profile onboard-prompt-no   Greenfield + TTY: answer n; application-first
                               path plus the `okto-neuron onboard` hint line.
  --profile onboard-no-open     Greenfield + TTY + OKTO_NEURON_NO_OPEN=1: no prompt.
  --profile onboard-no-flag     Greenfield + TTY + --no-onboard flag: no prompt.
  --profile onboard-upgrade-tty  Preseeded vault config + TTY: update path never
                               prompts.
  --profile onboard-non-tty     Direct mode, no controlling terminal: greenfield
                               run skips silently, then an upgrade run in the same
                               home never prompts. Implies OKTO_NEURON_NO_SERVE=1.

Options:
  --home PATH       Test HOME/evidence directory (default: per-mode/profile path)
  --url URL         Installer URL (default: public raw GitHub install.sh)
  --vault NAME      Test vault name (default: mynotes)
  --provider ID     Pre-fill OKTO_NEURON_LLM_PROVIDER for direct mode
  --api-base URL    Pre-fill OKTO_NEURON_LLM_API_BASE
  --model NAME      Pre-fill OKTO_NEURON_LLM_MODEL
  --session NAME    tmux session name
  --container NAME  Docker container name for --docker-tmux
  --driver-commit SHA  Exact dist commit for a public release-lifecycle run
  --no-serve        Install/configure only; do not start the daemon
  --cleanup         Stop daemon/container and delete the test home after the run
  -h, --help        Show this help

All modes install from the raw URL by default, set OKTO_NEURON_NO_MCP=1, delete
the previous sandbox for the selected mode/profile before running, and refuse to
use your real HOME or ~/.okto-neuron as the sandbox.
EOF
}

die() {
  printf 'error: %s\n' "$1" >&2
  exit 1
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --home)
      [ "$#" -ge 2 ] || die "--home requires a path"
      TEST_HOME="$2"
      shift 2
      ;;
    --url)
      [ "$#" -ge 2 ] || die "--url requires a URL"
      INSTALL_URL="$2"
      shift 2
      ;;
    --vault)
      [ "$#" -ge 2 ] || die "--vault requires a name"
      VAULT="$2"
      shift 2
      ;;
    --provider)
      [ "$#" -ge 2 ] || die "--provider requires an id"
      PROVIDER="$2"
      shift 2
      ;;
    --api-base)
      [ "$#" -ge 2 ] || die "--api-base requires a URL"
      API_BASE="$2"
      shift 2
      ;;
    --model)
      [ "$#" -ge 2 ] || die "--model requires a name"
      MODEL="$2"
      shift 2
      ;;
    --profile)
      [ "$#" -ge 2 ] || die "--profile requires a name"
      PROFILE="$2"
      shift 2
      ;;
    --session)
      [ "$#" -ge 2 ] || die "--session requires a name"
      SESSION="$2"
      shift 2
      ;;
    --container)
      [ "$#" -ge 2 ] || die "--container requires a name"
      CONTAINER="$2"
      shift 2
      ;;
    --driver-commit)
      [ "$#" -ge 2 ] || die "--driver-commit requires a SHA"
      DRIVER_COMMIT="$2"
      shift 2
      ;;
    --tmux)
      MODE="tmux"
      shift
      ;;
    --docker-tmux)
      MODE="docker-tmux"
      shift
      ;;
    --no-serve)
      NO_SERVE=1
      shift
      ;;
    --cleanup)
      CLEANUP=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

case "$PROFILE" in
  interactive|skip|auto-lm-studio|lm-studio|ollama|litellm|hosted-openai|hosted-openrouter|hosted-gemini|hosted-anthropic|custom|custom-rootform|existing-keep|existing-inspect|existing-reconfigure|disable-llm|release-lifecycle|onboard-prompt-yes|onboard-prompt-no|onboard-no-open|onboard-no-flag|onboard-upgrade-tty|onboard-non-tty) ;;
  *) die "unknown profile: $PROFILE" ;;
esac
case "$MODE:$PROFILE" in
  direct:onboard-prompt-yes|direct:onboard-prompt-no|direct:onboard-no-open|direct:onboard-no-flag|direct:onboard-upgrade-tty)
    die "--profile $PROFILE needs a real TTY; run it with --tmux or --docker-tmux"
    ;;
esac
if [ "$PROFILE" = "onboard-non-tty" ] && [ "$MODE" != "direct" ]; then
  die "--profile onboard-non-tty is a direct-mode scenario; it detaches the controlling terminal itself"
fi
if [ "$PROFILE" = "custom" ] && { [ -z "$API_BASE" ] || [ -z "$MODEL" ]; }; then
  die "--profile custom requires --api-base and --model"
fi
if [ "$PROFILE" = "release-lifecycle" ] && [ "$MODE" != "docker-tmux" ]; then
  die "--profile release-lifecycle requires --docker-tmux"
fi
if [ "$PROFILE" = "release-lifecycle" ] && [ "$NO_SERVE" -eq 1 ]; then
  die "--profile release-lifecycle cannot be combined with --no-serve"
fi
case "$PROFILE" in
  hosted-openai) [ -z "$MODEL" ] && MODEL="hosted-human-model" ;;
  hosted-openrouter) [ -z "$MODEL" ] && MODEL="openrouter-human-model" ;;
  hosted-gemini) [ -z "$MODEL" ] && MODEL="gemini-human-model" ;;
  hosted-anthropic) [ -z "$MODEL" ] && MODEL="anthropic-human-model" ;;
esac

profile_uses_fake_secret() {
  case "$PROFILE" in
    hosted-openai|hosted-openrouter|hosted-gemini|hosted-anthropic) return 0 ;;
    *) return 1 ;;
  esac
}

# custom-rootform: the hermetic endpoint serves ONLY under /v1/, so the pane
# evidence must show discovery and verify probing the canonical /v1 URLs —
# never the root-level paths the pre-0.0.50 root-form bug probed.
check_custom_rootform_evidence() {
  grep -Fq "Available models:" "$EVIDENCE" \
    || die "custom-rootform discovery did not list models: $EVIDENCE"
  grep -Fq "MOCK-REQ GET /v1/models" "$EVIDENCE" \
    || die "custom-rootform discovery did not probe the canonical /v1/models: $EVIDENCE"
  grep -Fq "MOCK-REQ POST /v1/chat/completions" "$EVIDENCE" \
    || die "custom-rootform pre-save verify did not run a completion: $EVIDENCE"
  grep -Fq "CUSTOM_ROOTFORM_DEAD_ENDPOINT_OK" "$EVIDENCE" \
    || die "custom-rootform dead-endpoint follow-up did not pass: $EVIDENCE"
  grep -Fq "CUSTOM_ROOTFORM_NO_SILENT_MODEL_OK" "$EVIDENCE" \
    || die "custom-rootform no-silent-model follow-up did not pass: $EVIDENCE"
  grep -Fq "MOCK-REQ GET /models" "$EVIDENCE" \
    && die "custom-rootform probed a root-level /models (pre-0.0.50 bug): $EVIDENCE"
  grep -Fq "MOCK-REQ POST /chat/completions" "$EVIDENCE" \
    && die "custom-rootform probed a root-level /chat/completions (pre-0.0.50 bug): $EVIDENCE"
  return 0
}

init_paths() {
  if [ -z "$TEST_HOME" ]; then
    TEST_HOME="${TMPDIR:-/tmp}/okto-neuron-install-test-${MODE}-${PROFILE}"
  fi
  if [ -z "$SESSION" ]; then
    SESSION="okto-neuron-install-${MODE}-${PROFILE}-$$"
  fi
  if [ -z "$CONTAINER" ]; then
    CONTAINER="okto-neuron-install-human-$$"
  fi
  case "$SESSION" in okto-neuron-install-*) ;; *) die "tmux session must start with okto-neuron-install-" ;; esac
  case "$CONTAINER" in okto-neuron-install-*) ;; *) die "Docker container must start with okto-neuron-install-" ;; esac
}

command -v curl >/dev/null 2>&1 || die "curl is required"
command -v bash >/dev/null 2>&1 || die "bash is required"
if [ "$MODE" = "tmux" ] || [ "$MODE" = "docker-tmux" ]; then
  command -v tmux >/dev/null 2>&1 || die "tmux is required"
fi
if [ "$MODE" = "docker-tmux" ]; then
  command -v docker >/dev/null 2>&1 || die "docker is required"
fi

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$1"
  fi
}

prepare_release_provenance() {
  local script_path tmp_driver tmp_install tmp_manifest pinned_install manifest_version
  script_path="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
  DRIVER_SHA256="$(sha256_file "$script_path")"
  if [ -n "$DRIVER_COMMIT" ]; then
    case "$DRIVER_COMMIT" in
      *[!0-9a-f]*|'') die "--driver-commit must be exactly 40 lowercase hexadecimal characters" ;;
    esac
    [ "${#DRIVER_COMMIT}" -eq 40 ] \
      || die "--driver-commit must be exactly 40 lowercase hexadecimal characters"
    DRIVER_URL="https://raw.githubusercontent.com/OktoLabsAI/okto-neuron/${DRIVER_COMMIT}/bin/test-install.sh"
    pinned_install="https://raw.githubusercontent.com/OktoLabsAI/okto-neuron/${DRIVER_COMMIT}/install.sh"
    MANIFEST_URL="https://raw.githubusercontent.com/OktoLabsAI/okto-neuron/${DRIVER_COMMIT}/release-manifest.json"
    tmp_driver="$(mktemp)"
    tmp_install="$(mktemp)"
    tmp_manifest="$(mktemp)"
    trap 'rm -f "$tmp_driver" "$tmp_install" "$tmp_manifest"' RETURN
    curl -fsSL "$DRIVER_URL" -o "$tmp_driver"
    cmp -s "$script_path" "$tmp_driver" \
      || die "running test-install.sh does not match exact public driver commit $DRIVER_COMMIT"
    INSTALL_URL="$pinned_install"
  else
    DRIVER_COMMIT="LOCAL_UNCOMMITTED"
    DRIVER_URL="LOCAL_UNCOMMITTED"
    MANIFEST_URL="https://raw.githubusercontent.com/OktoLabsAI/okto-neuron/main/release-manifest.json"
    tmp_install="$(mktemp)"
    tmp_manifest="$(mktemp)"
    trap 'rm -f "$tmp_install" "$tmp_manifest"' RETURN
  fi
  curl -fsSL "$INSTALL_URL" -o "$tmp_install"
  curl -fsSL "$MANIFEST_URL" -o "$tmp_manifest"
  INSTALL_SHA256="$(sha256_file "$tmp_install")"
  MANIFEST_SHA256="$(sha256_file "$tmp_manifest")"
  manifest_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "$tmp_manifest")"
  if [ -n "$EXPECTED_VERSION" ] && [ "$EXPECTED_VERSION" != "$manifest_version" ]; then
    die "pinned release manifest version $manifest_version does not match $EXPECTED_VERSION"
  fi
  EXPECTED_VERSION="$manifest_version"
  rm -f "${tmp_driver:-}" "$tmp_install" "$tmp_manifest"
  trap - RETURN
}

port_open() {
  local port="$1"
  (echo >"/dev/tcp/127.0.0.1/${port}") >/dev/null 2>&1
}

canonical_tmp_root() {
  mkdir -p "${TMPDIR:-/tmp}"
  (cd "${TMPDIR:-/tmp}" && pwd)
}

test_home_is_owned() {
  local home="$1" tmp_root parent marker
  [ -d "$home" ] || return 1
  tmp_root="$(canonical_tmp_root)"
  parent="$(cd "$(dirname "$home")" && pwd)"
  [ "$parent" = "$tmp_root" ] || return 1
  case "$(basename "$home")" in okto-neuron-install-test-*) ;; *) return 1 ;; esac
  marker="$home/$SANDBOX_MARKER_NAME"
  [ -f "$marker" ] || return 1
  [ "$(cat "$marker")" = "$SANDBOX_MARKER_VALUE" ]
}

assert_test_home_location() {
  local home="$1" tmp_root parent
  tmp_root="$(canonical_tmp_root)"
  parent="$(cd "$(dirname "$home")" && pwd)"
  [ "$parent" = "$tmp_root" ] \
    || die "test home must be a okto-neuron-install-test-* directory directly under $tmp_root"
  case "$(basename "$home")" in
    okto-neuron-install-test-*) ;;
    *) die "test home must be a okto-neuron-install-test-* directory directly under $tmp_root" ;;
  esac
}

remove_owned_tmux_session() {
  command -v tmux >/dev/null 2>&1 || return 0
  tmux has-session -t "$SESSION" 2>/dev/null || return 0
  [ "$(tmux show-options -t "$SESSION" -v "$RESOURCE_OWNER_KEY" 2>/dev/null || true)" = \
      "$SANDBOX_MARKER_VALUE" ] \
    || die "refusing to remove unowned tmux session: $SESSION"
  tmux kill-session -t "$SESSION"
}

remove_owned_container() {
  command -v docker >/dev/null 2>&1 || return 0
  docker inspect "$CONTAINER" >/dev/null 2>&1 || return 0
  [ "$(docker inspect -f "{{ index .Config.Labels \"$DOCKER_OWNER_LABEL\" }}" "$CONTAINER" 2>/dev/null || true)" = \
      "$SANDBOX_MARKER_VALUE" ] \
    || die "refusing to remove unowned Docker container: $CONTAINER"
  docker rm -f "$CONTAINER" >/dev/null
}

prepare_home() {
  local parent
  parent="$(mkdir -p "$(dirname "$TEST_HOME")" && cd "$(dirname "$TEST_HOME")" && pwd)"
  TEST_HOME="${parent}/$(basename "$TEST_HOME")"
  assert_test_home_location "$TEST_HOME"
  if [ -n "$ORIGINAL_HOME" ]; then
    ORIGINAL_HOME="$(cd "$ORIGINAL_HOME" && pwd)"
    case "$TEST_HOME" in
      "$ORIGINAL_HOME"|"$ORIGINAL_HOME/.okto-neuron"|"$ORIGINAL_HOME/.okto-neuron"/*|"$ORIGINAL_HOME/.marginalia"|"$ORIGINAL_HOME/.marginalia"/*)
        die "refusing to use your real HOME, ~/.okto-neuron or ~/.marginalia as the test home"
        ;;
    esac
  fi
  cleanup_previous_test_runs
  if [ -e "$TEST_HOME" ] && ! test_home_is_owned "$TEST_HOME"; then
    die "refusing to reuse unowned test directory: $TEST_HOME"
  fi
  cleanup_test_home_path "$TEST_HOME"
  if command -v tmux >/dev/null 2>&1; then
    remove_owned_tmux_session
  fi
  if [ "$MODE" = "docker-tmux" ]; then
    remove_owned_container
  fi
  # Docker has its own network namespace. Host port checks protect direct/tmux
  # runs, but must not require stopping a real host daemon for a container test.
  if [ "$MODE" != "docker-tmux" ]; then
    if curl -fsS http://127.0.0.1:7777/health >/dev/null 2>&1 || port_open 7777; then
      die "127.0.0.1:7777 is already in use. Stop the real Okto Neuron daemon first."
    fi
    if [ "$NO_SERVE" -ne 1 ] && port_open 8201; then
      die "127.0.0.1:8201 is already in use. Stop the process using it first."
    fi
  fi

  mkdir -p "$TEST_HOME"
  printf '%s\n' "$SANDBOX_MARKER_VALUE" > "$TEST_HOME/$SANDBOX_MARKER_NAME"
  EVIDENCE="$TEST_HOME/evidence-${SESSION}.txt"
}

stop_sandbox_daemon_in_home() {
  local home="$1" pid_file raw pid parser size cli
  local tool_bin="$home/.local/bin"
  # Only the sandbox HOME's own tool, never a daemon of the real user.
  for cli in okto-neuron marginalia; do
    [ -x "$tool_bin/$cli" ] || continue
    HOME="$home" \
    XDG_DATA_HOME="$home/.local/share" \
    XDG_CACHE_HOME="$home/.cache" \
    XDG_CONFIG_HOME="$home/.config" \
    UV_CACHE_DIR="$home/.cache/uv" \
    PATH="$tool_bin:$PATH" \
      "$tool_bin/$cli" stop >/dev/null 2>&1 || true
  done
  while IFS= read -r pid_file; do
    [ -r "$pid_file" ] \
      || die "refusing to delete test sandbox with unreadable PID record: $pid_file"
    size="$(wc -c < "$pid_file" | tr -d '[:space:]')"
    case "$size" in *[!0-9]*|'') die "could not size test sandbox PID record: $pid_file" ;; esac
    [ "$size" -gt 0 ] \
      || die "refusing to delete test sandbox with empty PID record: $pid_file"
    [ "$size" -le 16384 ] \
      || die "refusing to delete test sandbox with oversized PID record: $pid_file"
    raw="$(cat "$pid_file")"
    case "$raw" in
      *[!0-9]*|'')
        if command -v python3 >/dev/null 2>&1; then
          parser="$(command -v python3)"
        elif [ -x "$home/.local/share/uv/tools/okto-neuron/bin/python" ]; then
          parser="$home/.local/share/uv/tools/okto-neuron/bin/python"
        elif [ -x "$home/.local/share/uv/tools/marginalia/bin/python" ]; then
          parser="$home/.local/share/uv/tools/marginalia/bin/python"
        else
          die "refusing to delete test sandbox with unverifiable PID record: $pid_file"
        fi
        pid="$("$parser" -c '
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
pid = payload.get("pid") if isinstance(payload, dict) else None
if type(pid) is not int or pid <= 0:
    raise SystemExit(1)
print(pid)
' "$pid_file" 2>/dev/null)" \
          || die "refusing to delete test sandbox with invalid PID record: $pid_file"
        ;;
      *) pid="$raw" ;;
    esac
    if [ -n "$pid" ] && kill -0 "$pid" >/dev/null 2>&1; then
      die "refusing to delete test sandbox with live daemon pid $pid: $home"
    fi
  done < <(find "$home/.okto-neuron" -type f -name server.pid -print 2>/dev/null || true)
}

cleanup_test_home_path() {
  local home="$1"
  [ -e "$home" ] || return 0
  assert_test_home_location "$home"
  test_home_is_owned "$home" || die "refusing to delete unowned test directory: $home"
  if [ -n "$ORIGINAL_HOME" ]; then
    case "$home" in
      "$ORIGINAL_HOME"|"$ORIGINAL_HOME/.okto-neuron"|"$ORIGINAL_HOME/.okto-neuron"/*|"$ORIGINAL_HOME/.marginalia"|"$ORIGINAL_HOME/.marginalia"/*)
        die "refusing to delete your real HOME or ~/.okto-neuron: $home"
        ;;
    esac
  fi
  stop_sandbox_daemon_in_home "$home"
  rm -rf "$home"
}

cleanup_previous_test_runs() {
  local tmp_root candidate parent home session_name
  tmp_root="$(mkdir -p "${TMPDIR:-/tmp}" && cd "${TMPDIR:-/tmp}" && pwd)"
  for candidate in "$tmp_root"/okto-neuron-install-test*; do
    [ -e "$candidate" ] || continue
    parent="$(cd "$(dirname "$candidate")" && pwd)"
    home="${parent}/$(basename "$candidate")"
    if test_home_is_owned "$home"; then
      cleanup_test_home_path "$home"
    fi
  done
  if command -v tmux >/dev/null 2>&1; then
    while IFS= read -r session_name; do
      case "$session_name" in
        okto-neuron-install-*)
          if [ "$(tmux show-options -t "$session_name" -v "$RESOURCE_OWNER_KEY" 2>/dev/null || true)" = \
              "$SANDBOX_MARKER_VALUE" ]; then
            tmux kill-session -t "$session_name" >/dev/null 2>&1 || true
          fi
          ;;
      esac
    done < <(tmux list-sessions -F '#S' 2>/dev/null || true)
  fi
}

cleanup_sandbox() {
  if [ "$MODE" = "docker-tmux" ]; then
    remove_owned_container
  fi
  if command -v tmux >/dev/null 2>&1; then
    remove_owned_tmux_session
  fi
  cleanup_test_home_path "$TEST_HOME"
}

write_host_runner() {
  local runner="$TEST_HOME/host-runner.sh"
  cat > "$runner" <<'RUNNER'
#!/usr/bin/env bash
set -euo pipefail

start_mock_lm_studio() {
  python3 - <<'PY' &
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "mac-human-model"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"MOCK-REQ GET {self.path}", flush=True)
        if self.path.rstrip("/") == "/v1/models":
            body = json.dumps({"data": [{"id": MODEL}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
    def do_POST(self):
        length = int(self.headers.get("content-length", 0) or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        print(f"MOCK-REQ POST {self.path}", flush=True)
        if self.path.rstrip("/") == "/v1/chat/completions":
            body = json.dumps({
                "id": "cmpl-mock",
                "object": "chat.completion",
                "model": payload.get("model", MODEL),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
    def log_message(self, *_args):
        return

ThreadingHTTPServer(("127.0.0.1", 1234), Handler).serve_forever()
PY
  MOCK_PID="$!"
  trap 'kill "$MOCK_PID" >/dev/null 2>&1 || true' EXIT
  for _ in $(seq 1 30); do
    curl -fsS http://127.0.0.1:1234/v1/models >/dev/null 2>&1 && return
    sleep 0.2
  done
  echo "mock LM Studio server did not start" >&2
  exit 80
}

start_mock_ollama() {
  python3 - <<'PY' &
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "mac-ollama-human-model"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"MOCK-REQ GET {self.path}", flush=True)
        if self.path.rstrip("/") == "/v1/models":
            body = json.dumps({"data": [{"id": MODEL}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
    def do_POST(self):
        length = int(self.headers.get("content-length", 0) or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        print(f"MOCK-REQ POST {self.path}", flush=True)
        if self.path.rstrip("/") == "/v1/chat/completions":
            body = json.dumps({
                "id": "cmpl-mock",
                "object": "chat.completion",
                "model": payload.get("model", MODEL),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
    def log_message(self, *_args):
        return

ThreadingHTTPServer(("127.0.0.1", 11434), Handler).serve_forever()
PY
  MOCK_PID="$!"
  trap 'kill "$MOCK_PID" >/dev/null 2>&1 || true' EXIT
  for _ in $(seq 1 30); do
    curl -fsS http://127.0.0.1:11434/v1/models >/dev/null 2>&1 && return
    sleep 0.2
  done
  echo "mock Ollama server did not start" >&2
  exit 80
}

start_mock_litellm() {
  python3 - <<'PY' &
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "mac-litellm-human-model"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"MOCK-REQ GET {self.path}", flush=True)
        if self.path.rstrip("/") == "/v1/model/info":
            body = json.dumps({
                "data": [
                    {
                        "model_name": MODEL,
                        "litellm_params": {
                            "model": "openai/hidden-upstream",
                            "api_key": "should-not-surface"
                        }
                    }
                ]
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
    def do_POST(self):
        length = int(self.headers.get("content-length", 0) or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        print(f"MOCK-REQ POST {self.path}", flush=True)
        if self.path.rstrip("/") == "/v1/chat/completions":
            body = json.dumps({
                "id": "cmpl-mock",
                "object": "chat.completion",
                "model": payload.get("model", MODEL),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
    def log_message(self, *_args):
        return

ThreadingHTTPServer(("127.0.0.1", 4000), Handler).serve_forever()
PY
  MOCK_PID="$!"
  trap 'kill "$MOCK_PID" >/dev/null 2>&1 || true' EXIT
  for _ in $(seq 1 30); do
    curl -fsS http://127.0.0.1:4000/v1/model/info >/dev/null 2>&1 && return
    sleep 0.2
  done
  echo "mock LiteLLM server did not start" >&2
  exit 80
}

# Hermetic OpenAI-compatible endpoint for the custom-rootform profile:
# serves ONLY under /v1/ (the oMLX shape that exposed the root-form
# onboarding bug). The models list is deliberately ordered with a
# non-chat model first, so a silent models[0] default would be visible
# in the saved config. Every request is echoed as MOCK-REQ so the tmux
# evidence shows exactly which endpoints discovery and verify probed.
start_mock_custom_openai() {
  python3 - <<'PY' &
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = ["fake-nonchat-first", "fake-chat-model"]

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"MOCK-REQ GET {self.path}", flush=True)
        if self.path.rstrip("/") == "/v1/models":
            body = json.dumps({"data": [{"id": m} for m in MODELS]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
    def do_POST(self):
        length = int(self.headers.get("content-length", 0) or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        print(f"MOCK-REQ POST {self.path}", flush=True)
        if self.path.rstrip("/") == "/v1/chat/completions":
            body = json.dumps({
                "id": "cmpl-mock",
                "object": "chat.completion",
                "model": payload.get("model", MODELS[1]),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
    def log_message(self, *_args):
        return

ThreadingHTTPServer(("127.0.0.1", 18123), Handler).serve_forever()
PY
  MOCK_PID="$!"
  trap 'kill "$MOCK_PID" >/dev/null 2>&1 || true' EXIT
  for _ in $(seq 1 30); do
    curl -fsS http://127.0.0.1:18123/v1/models >/dev/null 2>&1 && return
    sleep 0.2
  done
  echo "mock custom OpenAI server did not start" >&2
  exit 80
}

# custom-rootform follow-ups, run in the sandbox after the install:
# (f) dead endpoint -> pre-save verify fails, NOTHING is saved, and the
#     exact attempted URL is the canonical /v1 completion URL derived
#     from the root-form base; (g) a non-interactive run without --model
#     never silently defaults to models[0] (here: the non-chat first).
run_custom_rootform_followups() {
  local dead_out nomodel_out
  dead_out="$HOME/.okto-neuron/custom-rootform-dead-endpoint.out"
  printf 'custom-rootform follow-up (f): dead endpoint -> verify fails, nothing saved\n'
  if env -u OKTO_NEURON_VAULT okto-neuron onboard --vault dead-endpoint-vault --non-interactive \
      --provider custom --api-base http://127.0.0.1:18124 --model fake-any-model \
      >"$dead_out" 2>&1; then
    echo "dead-endpoint onboard unexpectedly succeeded" >&2
    return 1
  fi
  cat "$dead_out"
  grep -Fq 'verify failed — nothing was saved:' "$dead_out" \
    || { echo "dead-endpoint run did not report verify failure: $dead_out" >&2; return 1; }
  grep -Fq 'attempted: POST http://127.0.0.1:18124/v1/chat/completions' "$dead_out" \
    || { echo "dead-endpoint run did not show the exact attempted URL: $dead_out" >&2; return 1; }
  ! grep -q '^llm:' "$HOME/.okto-neuron/vaults/dead-endpoint-vault/okto-neuron.yaml" \
    || { echo "dead-endpoint run saved an llm block" >&2; return 1; }
  echo CUSTOM_ROOTFORM_DEAD_ENDPOINT_OK

  nomodel_out="$HOME/.okto-neuron/custom-rootform-no-model.out"
  printf 'custom-rootform follow-up (g): non-interactive without --model refuses models[0]\n'
  if env -u OKTO_NEURON_VAULT okto-neuron onboard --vault no-model-vault --non-interactive \
      --provider custom --api-base http://127.0.0.1:18123 \
      >"$nomodel_out" 2>&1; then
    echo "no-model onboard unexpectedly succeeded" >&2
    return 1
  fi
  cat "$nomodel_out"
  grep -Fq 'no --model given and no preset default among the discovered models' "$nomodel_out" \
    || { echo "no-model run did not demand an explicit --model: $nomodel_out" >&2; return 1; }
  grep -Fq 'fake-nonchat-first' "$nomodel_out" \
    || { echo "no-model run did not list the discovered models: $nomodel_out" >&2; return 1; }
  ! grep -q '^llm:' "$HOME/.okto-neuron/vaults/no-model-vault/okto-neuron.yaml" \
    || { echo "no-model run saved an llm block" >&2; return 1; }
  echo CUSTOM_ROOTFORM_NO_SILENT_MODEL_OK
}

seed_existing_config() {
  mkdir -p "$HOME/.okto-neuron/vaults/$VAULT"
  cat > "$HOME/.okto-neuron/vaults/$VAULT/okto-neuron.yaml" <<YAML
marginalia_yaml_version: 1
vault_id: $VAULT
packs:
  - core
embedding:
  provider: fastembed
  model: BAAI/bge-small-en-v1.5
llm:
  enabled: true
  allow_remote: false
  defaults:
    provider: openai
    api_base: http://127.0.0.1:9999/v1
    model: preexisting-model
    api_key_env: OKTO_NEURON_EXISTING_KEY
YAML
}

export HOME="$TEST_HOME"
export XDG_DATA_HOME="$HOME/.local/share"
export XDG_CACHE_HOME="$HOME/.cache"
export XDG_CONFIG_HOME="$HOME/.config"
export UV_CACHE_DIR="$HOME/.cache/uv"
export OKTO_NEURON_NO_MCP=1
# The first-run-prompt profiles run with OKTO_NEURON_NO_OPEN unset (it
# suppresses the prompt by contract); their `open` is shimmed below so the
# verified UI never launches a real browser.
case "$PROFILE" in
  onboard-prompt-yes|onboard-prompt-no|onboard-no-flag|onboard-upgrade-tty) ;;
  *) export OKTO_NEURON_NO_OPEN=1 ;;
esac
# HOME is redirected here but never let a sandbox run persist PATH via
# uv tool update-shell regardless.
export OKTO_NEURON_NO_UPDATE_SHELL=1
# The greenfield (plain-path) profiles must not preseed OKTO_NEURON_VAULT.
case "$PROFILE" in
  onboard-prompt-yes|onboard-prompt-no|onboard-no-open|onboard-no-flag) ;;
  *) export OKTO_NEURON_VAULT="$VAULT" ;;
esac
export OKTO_NEURON_EXPECTED_VERSION="$EXPECTED_VERSION"
[ "$NO_SERVE" = "1" ] && export OKTO_NEURON_NO_SERVE=1

case "$PROFILE" in
  existing-keep|existing-inspect|existing-reconfigure|disable-llm|onboard-upgrade-tty)
    seed_existing_config
    ;;
esac
case "$PROFILE" in
  onboard-prompt-yes|onboard-prompt-no|onboard-no-flag|onboard-upgrade-tty)
    mkdir -p "$HOME/.sandbox-bin"
    printf '#!/bin/sh\nexit 0\n' > "$HOME/.sandbox-bin/open"
    printf '#!/bin/sh\nexit 0\n' > "$HOME/.sandbox-bin/xdg-open"
    chmod +x "$HOME/.sandbox-bin/open" "$HOME/.sandbox-bin/xdg-open"
    export PATH="$HOME/.sandbox-bin:$PATH"
    ;;
esac

if [ "$PROFILE" = "lm-studio" ] || [ "$PROFILE" = "auto-lm-studio" ] || [ "$PROFILE" = "existing-reconfigure" ]; then
  start_mock_lm_studio
elif [ "$PROFILE" = "ollama" ]; then
  start_mock_ollama
elif [ "$PROFILE" = "litellm" ]; then
  start_mock_litellm
elif [ "$PROFILE" = "hosted-openai" ] || [ "$PROFILE" = "hosted-openrouter" ] || [ "$PROFILE" = "hosted-gemini" ] || [ "$PROFILE" = "hosted-anthropic" ]; then
  export OKTO_NEURON_LLM_MODEL="$MODEL"
  export OKTO_NEURON_LLM_API_KEY_ENV=OKTO_NEURON_HOSTED_TEST_KEY
  export OKTO_NEURON_HOSTED_TEST_KEY=fake-public-installer-test-key
elif [ "$PROFILE" = "custom-rootform" ]; then
  start_mock_custom_openai
fi

printf 'TEST_HOME=%s\nINSTALL_URL=%s\nPROFILE=%s\n' "$HOME" "$INSTALL_URL" "$PROFILE"
case "$PROFILE" in
  onboard-no-flag) curl -fsSL "$INSTALL_URL" | bash -s -- --no-onboard ;;
  *) curl -fsSL "$INSTALL_URL" | bash ;;
esac

export PATH="$HOME/.local/bin:$PATH"
okto-neuron --help | sed -n '1,12p'
CLI_VERSION="$(okto-neuron --version | head -n 1)"
[ "$CLI_VERSION" = "okto-neuron $EXPECTED_VERSION" ]
case "$PROFILE" in
  onboard-prompt-yes)
    CURRENT_VAULT="$(okto-neuron vault current)"
    [ "$(cd "$CURRENT_VAULT" && pwd)" = \
      "$(cd "$HOME/.okto-neuron/vaults/onboarded-vault" && pwd)" ]
    printf '%s\n' "$CURRENT_VAULT"
    YAML="$HOME/.okto-neuron/vaults/onboarded-vault/okto-neuron.yaml"
    [ -f "$YAML" ]
    ! grep -q '^llm:' "$YAML"
    ;;
  onboard-prompt-no|onboard-no-open|onboard-no-flag)
    [ -z "$(find "$HOME/.okto-neuron/vaults" -name okto-neuron.yaml -print -quit 2>/dev/null || true)" ]
    ;;
  onboard-upgrade-tty)
    [ -f "$HOME/.okto-neuron/vaults/$VAULT/okto-neuron.yaml" ]
    grep -q 'preexisting-model' "$HOME/.okto-neuron/vaults/$VAULT/okto-neuron.yaml"
    ;;
  *)
    CURRENT_VAULT="$(okto-neuron vault current)"
    [ "$(cd "$CURRENT_VAULT" && pwd)" = "$(cd "$HOME/.okto-neuron/vaults/$VAULT" && pwd)" ]
    printf '%s\n' "$CURRENT_VAULT"
    ;;
esac
# onboard-upgrade-tty intentionally keeps a stopped daemon stopped.
if [ "$NO_SERVE" != "1" ] && [ "$PROFILE" != "onboard-upgrade-tty" ]; then
  curl -fsS http://127.0.0.1:7777/health
  printf '\n'
  TOOL_PYTHON="$(uv tool dir)/okto-neuron/bin/python"
  SERVER_VERSION="$(curl -fsS http://127.0.0.1:7777/version | \
    "$TOOL_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["okto_neuron_version"])')"
  [ "$SERVER_VERSION" = "$EXPECTED_VERSION" ]
fi
YAML="$HOME/.okto-neuron/vaults/$VAULT/okto-neuron.yaml"
case "$PROFILE" in
  skip)
    ! grep -q '^llm:' "$YAML"
    ;;
  lm-studio)
    grep -q 'provider: lm_studio' "$YAML"
    grep -q 'mac-human-model' "$YAML"
    ;;
  auto-lm-studio)
    grep -q 'provider: lm_studio' "$YAML"
    grep -q 'mac-human-model' "$YAML"
    ;;
  ollama)
    grep -q 'provider: ollama' "$YAML"
    grep -q 'mac-ollama-human-model' "$YAML"
    ;;
  litellm)
    grep -q 'provider: litellm_proxy' "$YAML"
    grep -q 'api_base: http://127.0.0.1:4000' "$YAML"
    grep -q 'mac-litellm-human-model' "$YAML"
    ! grep -q 'should-not-surface' "$YAML"
    ;;
  hosted-openai)
    grep -q 'provider: openai' "$YAML"
    grep -q "$MODEL" "$YAML"
    grep -q 'api_key_env: OKTO_NEURON_HOSTED_TEST_KEY' "$YAML"
    ! grep -q 'fake-public-installer-test-key' "$YAML"
    ;;
  hosted-openrouter)
    grep -q 'provider: openrouter' "$YAML"
    grep -q "$MODEL" "$YAML"
    grep -q 'api_key_env: OKTO_NEURON_HOSTED_TEST_KEY' "$YAML"
    ! grep -q 'fake-public-installer-test-key' "$YAML"
    ;;
  hosted-gemini)
    grep -q 'provider: gemini' "$YAML"
    grep -q 'api_base: https://generativelanguage.googleapis.com/v1beta/openai/' "$YAML"
    grep -q "$MODEL" "$YAML"
    grep -q 'api_key_env: OKTO_NEURON_HOSTED_TEST_KEY' "$YAML"
    ! grep -q 'fake-public-installer-test-key' "$YAML"
    ;;
  hosted-anthropic)
    grep -q 'provider: anthropic' "$YAML"
    grep -q "$MODEL" "$YAML"
    grep -q 'api_key_env: OKTO_NEURON_HOSTED_TEST_KEY' "$YAML"
    ! grep -q 'fake-public-installer-test-key' "$YAML"
    ;;
  custom)
    grep -q "$MODEL" "$YAML"
    ;;
  existing-keep)
    grep -q 'preexisting-model' "$YAML"
    grep -q 'api_base: http://127.0.0.1:9999/v1' "$YAML"
    ;;
  existing-inspect)
    grep -q 'preexisting-model' "$YAML"
    grep -q 'api_base: http://127.0.0.1:9999/v1' "$YAML"
    ;;
  existing-reconfigure)
    grep -q 'provider: lm_studio' "$YAML"
    grep -q 'mac-human-model' "$YAML"
    ! grep -q 'preexisting-model' "$YAML"
    ;;
  custom-rootform)
    grep -q 'provider: openai' "$YAML"
    grep -q 'api_base: http://127.0.0.1:18123/v1' "$YAML"
    grep -q 'fake-chat-model' "$YAML"
    ! grep -q 'fake-nonchat-first' "$YAML"
    ;;
  disable-llm)
    grep -q 'enabled: false' "$YAML"
    ;;
esac
if [ "$PROFILE" = "custom-rootform" ]; then
  run_custom_rootform_followups
fi
okto-neuron stop >/dev/null 2>&1 || true
echo MAC_TMUX_HUMAN_INSTALL_OK
RUNNER
  chmod +x "$runner"
  printf '%s\n' "$runner"
}

write_docker_runner() {
  local runner="$TEST_HOME/docker-runner.sh"
  cat > "$runner" <<'RUNNER'
#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update >/dev/null
apt-get install -y --no-install-recommends ca-certificates curl bash python3 >/dev/null

start_mock_lm_studio() {
  python3 - <<'PY' &
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "docker-human-model"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"MOCK-REQ GET {self.path}", flush=True)
        if self.path.rstrip("/") == "/v1/models":
            body = json.dumps({"data": [{"id": MODEL}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
    def do_POST(self):
        length = int(self.headers.get("content-length", 0) or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        print(f"MOCK-REQ POST {self.path}", flush=True)
        if self.path.rstrip("/") == "/v1/chat/completions":
            body = json.dumps({
                "id": "cmpl-mock",
                "object": "chat.completion",
                "model": payload.get("model", MODEL),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
    def log_message(self, *_args):
        return

ThreadingHTTPServer(("127.0.0.1", 1234), Handler).serve_forever()
PY
  MOCK_PID="$!"
  trap 'kill "$MOCK_PID" >/dev/null 2>&1 || true' EXIT
  for _ in $(seq 1 30); do
    curl -fsS http://127.0.0.1:1234/v1/models >/dev/null 2>&1 && return
    sleep 0.2
  done
  echo "mock LM Studio server did not start" >&2
  exit 80
}

start_mock_ollama() {
  python3 - <<'PY' &
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "docker-ollama-human-model"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"MOCK-REQ GET {self.path}", flush=True)
        if self.path.rstrip("/") == "/v1/models":
            body = json.dumps({"data": [{"id": MODEL}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
    def do_POST(self):
        length = int(self.headers.get("content-length", 0) or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        print(f"MOCK-REQ POST {self.path}", flush=True)
        if self.path.rstrip("/") == "/v1/chat/completions":
            body = json.dumps({
                "id": "cmpl-mock",
                "object": "chat.completion",
                "model": payload.get("model", MODEL),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
    def log_message(self, *_args):
        return

ThreadingHTTPServer(("127.0.0.1", 11434), Handler).serve_forever()
PY
  MOCK_PID="$!"
  trap 'kill "$MOCK_PID" >/dev/null 2>&1 || true' EXIT
  for _ in $(seq 1 30); do
    curl -fsS http://127.0.0.1:11434/v1/models >/dev/null 2>&1 && return
    sleep 0.2
  done
  echo "mock Ollama server did not start" >&2
  exit 80
}

start_mock_litellm() {
  python3 - <<'PY' &
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "docker-litellm-human-model"

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"MOCK-REQ GET {self.path}", flush=True)
        if self.path.rstrip("/") == "/v1/model/info":
            body = json.dumps({
                "data": [
                    {
                        "model_name": MODEL,
                        "litellm_params": {
                            "model": "openai/hidden-upstream",
                            "api_key": "should-not-surface"
                        }
                    }
                ]
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
    def do_POST(self):
        length = int(self.headers.get("content-length", 0) or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        print(f"MOCK-REQ POST {self.path}", flush=True)
        if self.path.rstrip("/") == "/v1/chat/completions":
            body = json.dumps({
                "id": "cmpl-mock",
                "object": "chat.completion",
                "model": payload.get("model", MODEL),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
    def log_message(self, *_args):
        return

ThreadingHTTPServer(("127.0.0.1", 4000), Handler).serve_forever()
PY
  MOCK_PID="$!"
  trap 'kill "$MOCK_PID" >/dev/null 2>&1 || true' EXIT
  for _ in $(seq 1 30); do
    curl -fsS http://127.0.0.1:4000/v1/model/info >/dev/null 2>&1 && return
    sleep 0.2
  done
  echo "mock LiteLLM server did not start" >&2
  exit 80
}

# Hermetic OpenAI-compatible endpoint for the custom-rootform profile:
# serves ONLY under /v1/ (the oMLX shape that exposed the root-form
# onboarding bug). The models list is deliberately ordered with a
# non-chat model first, so a silent models[0] default would be visible
# in the saved config. Every request is echoed as MOCK-REQ so the tmux
# evidence shows exactly which endpoints discovery and verify probed.
start_mock_custom_openai() {
  python3 - <<'PY' &
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = ["fake-nonchat-first", "fake-chat-model"]

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"MOCK-REQ GET {self.path}", flush=True)
        if self.path.rstrip("/") == "/v1/models":
            body = json.dumps({"data": [{"id": m} for m in MODELS]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
    def do_POST(self):
        length = int(self.headers.get("content-length", 0) or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        print(f"MOCK-REQ POST {self.path}", flush=True)
        if self.path.rstrip("/") == "/v1/chat/completions":
            body = json.dumps({
                "id": "cmpl-mock",
                "object": "chat.completion",
                "model": payload.get("model", MODELS[1]),
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": "ok"},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
    def log_message(self, *_args):
        return

ThreadingHTTPServer(("127.0.0.1", 18123), Handler).serve_forever()
PY
  MOCK_PID="$!"
  trap 'kill "$MOCK_PID" >/dev/null 2>&1 || true' EXIT
  for _ in $(seq 1 30); do
    curl -fsS http://127.0.0.1:18123/v1/models >/dev/null 2>&1 && return
    sleep 0.2
  done
  echo "mock custom OpenAI server did not start" >&2
  exit 80
}

# custom-rootform follow-ups, run in the sandbox after the install:
# (f) dead endpoint -> pre-save verify fails, NOTHING is saved, and the
#     exact attempted URL is the canonical /v1 completion URL derived
#     from the root-form base; (g) a non-interactive run without --model
#     never silently defaults to models[0] (here: the non-chat first).
run_custom_rootform_followups() {
  local dead_out nomodel_out
  dead_out="$HOME/.okto-neuron/custom-rootform-dead-endpoint.out"
  printf 'custom-rootform follow-up (f): dead endpoint -> verify fails, nothing saved\n'
  if env -u OKTO_NEURON_VAULT okto-neuron onboard --vault dead-endpoint-vault --non-interactive \
      --provider custom --api-base http://127.0.0.1:18124 --model fake-any-model \
      >"$dead_out" 2>&1; then
    echo "dead-endpoint onboard unexpectedly succeeded" >&2
    return 1
  fi
  cat "$dead_out"
  grep -Fq 'verify failed — nothing was saved:' "$dead_out" \
    || { echo "dead-endpoint run did not report verify failure: $dead_out" >&2; return 1; }
  grep -Fq 'attempted: POST http://127.0.0.1:18124/v1/chat/completions' "$dead_out" \
    || { echo "dead-endpoint run did not show the exact attempted URL: $dead_out" >&2; return 1; }
  ! grep -q '^llm:' "$HOME/.okto-neuron/vaults/dead-endpoint-vault/okto-neuron.yaml" \
    || { echo "dead-endpoint run saved an llm block" >&2; return 1; }
  echo CUSTOM_ROOTFORM_DEAD_ENDPOINT_OK

  nomodel_out="$HOME/.okto-neuron/custom-rootform-no-model.out"
  printf 'custom-rootform follow-up (g): non-interactive without --model refuses models[0]\n'
  if env -u OKTO_NEURON_VAULT okto-neuron onboard --vault no-model-vault --non-interactive \
      --provider custom --api-base http://127.0.0.1:18123 \
      >"$nomodel_out" 2>&1; then
    echo "no-model onboard unexpectedly succeeded" >&2
    return 1
  fi
  cat "$nomodel_out"
  grep -Fq 'no --model given and no preset default among the discovered models' "$nomodel_out" \
    || { echo "no-model run did not demand an explicit --model: $nomodel_out" >&2; return 1; }
  grep -Fq 'fake-nonchat-first' "$nomodel_out" \
    || { echo "no-model run did not list the discovered models: $nomodel_out" >&2; return 1; }
  ! grep -q '^llm:' "$HOME/.okto-neuron/vaults/no-model-vault/okto-neuron.yaml" \
    || { echo "no-model run saved an llm block" >&2; return 1; }
  echo CUSTOM_ROOTFORM_NO_SILENT_MODEL_OK
}

seed_existing_config() {
  mkdir -p "$HOME/.okto-neuron/vaults/$VAULT"
  cat > "$HOME/.okto-neuron/vaults/$VAULT/okto-neuron.yaml" <<YAML
marginalia_yaml_version: 1
vault_id: $VAULT
packs:
  - core
embedding:
  provider: fastembed
  model: BAAI/bge-small-en-v1.5
llm:
  enabled: true
  allow_remote: false
  defaults:
    provider: openai
    api_base: http://127.0.0.1:9999/v1
    model: preexisting-model
    api_key_env: OKTO_NEURON_EXISTING_KEY
YAML
}

json_field() {
  local payload="$1" field="$2"
  printf '%s' "$payload" | python3 -c \
    'import json,sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$field"
}

wait_for_health() {
  local endpoint="$1"
  for _ in $(seq 1 90); do
    curl -fsS "${endpoint}/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "daemon did not become healthy at ${endpoint}" >&2
  return 1
}

runner_port_open() {
  local port="$1"
  (echo >"/dev/tcp/127.0.0.1/${port}") >/dev/null 2>&1
}

wait_for_daemon_stopped() {
  local pid="$1" port port_open_now
  shift
  for _ in $(seq 1 60); do
    port_open_now=0
    for port in "$@"; do
      if runner_port_open "$port"; then
        port_open_now=1
        break
      fi
    done
    if ! kill -0 "$pid" >/dev/null 2>&1 && [ "$port_open_now" -eq 0 ]; then
      return 0
    fi
    sleep 0.5
  done
  echo "daemon process or ports remained live after stop (pid $pid; ports $*)" >&2
  return 1
}

verify_default_daemon() {
  local status html ui_output
  wait_for_health "http://127.0.0.1:7777"
  status="$(okto-neuron status --json --timeout 5)"
  [ "$(json_field "$status" okto_neuron_version)" = "$EXPECTED_VERSION" ]
  [ "$(json_field "$status" endpoint)" = "http://127.0.0.1:7777" ]
  html="$(curl -fsS http://127.0.0.1:7777/)"
  printf '%s' "$html" | grep -Eiq '<!doctype html|<html'
  ui_output="$(okto-neuron ui --no-open)"
  [ "$ui_output" = \
    "Okto Neuron UI is ready at http://127.0.0.1:7777/; browser launch skipped" ]
  printf '%s\n' "$ui_output" >&2
  printf '%s' "$status"
}

run_raw_installer() {
  curl -fsSL "$INSTALL_URL" | bash
}

run_predecessor_migration() (
  # Predecessor: Marginalia 0.2.0, the last release under the pre-0.3.0 name,
  # installed with the exact marginalia-dist one-liner of that release. The
  # successor must stop it with its own command, roll back to exactly that tool
  # on a forced activation failure, and on a clean run replace it while leaving
  # its vault in ~/.marginalia/vaults and keeping the daemon credential.
  set -euo pipefail
  local predecessor_commit predecessor_install_url predecessor_manifest_url
  local predecessor_install_sha predecessor_manifest_sha migration_home legacy_vault
  local predecessor_install predecessor_manifest status old_pid rollback_pid migrated_pid
  local legacy_pid_file legacy_token_file app_pid_file app_token_file token_sha sentinel
  local sentinel_sha real_uv rollback_output migration_output successor_version successor_manifest
  local legacy_config_sha alias_err

  predecessor_commit="29683d2156840eadaff43efd6133750cd7f24465"
  predecessor_install_url="https://raw.githubusercontent.com/OktoLabsAI/marginalia-dist/${predecessor_commit}/install.sh"
  predecessor_manifest_url="https://raw.githubusercontent.com/OktoLabsAI/marginalia-dist/${predecessor_commit}/release-manifest.json"
  predecessor_install_sha="a4c0f72b757bbfe54c17ee766885c84d8b89b2126fe1027df4d562f8824b15a8"
  predecessor_manifest_sha="22eb2f07ee453a708052469d71382a2dc47b0491ff8534d7899325f3bea8068f"
  migration_home="/tmp/okto-neuron-predecessor-migration"
  legacy_vault="${migration_home}/.marginalia/vaults/legacy-vault"
  predecessor_install="${migration_home}/predecessor-install.sh"
  predecessor_manifest="${migration_home}/predecessor-manifest.json"
  successor_version="$EXPECTED_VERSION"
  successor_manifest="$MANIFEST_URL"

  rm -rf "$migration_home"
  mkdir -p "$migration_home/tmp"
  export HOME="$migration_home"
  export XDG_DATA_HOME="$HOME/.local/share"
  export XDG_CACHE_HOME="$HOME/.cache"
  export XDG_CONFIG_HOME="$HOME/.config"
  export UV_TOOL_DIR="$HOME/.local/share/uv/tools"
  export UV_TOOL_BIN_DIR="$HOME/.local/bin"
  export UV_PYTHON_INSTALL_DIR="$HOME/.local/share/uv/python"
  export UV_CACHE_DIR="$HOME/.cache/uv"
  export TMPDIR="$HOME/tmp"
  export PATH="$HOME/.local/bin:$PATH"
  # The 0.2.0 installer only knows the old names. The successor reads them too
  # (with a warning), which this phase also exercises.
  export MARGINALIA_NO_MCP=1
  export MARGINALIA_NO_OPEN=1
  export MARGINALIA_NO_UPDATE_SHELL=1
  export MARGINALIA_VAULT=legacy-vault
  export MARGINALIA_ONBOARD_NONINTERACTIVE=1
  export MARGINALIA_LLM_PROVIDER=skip
  export MARGINALIA_EXPECTED_VERSION=0.2.0
  export MARGINALIA_MANIFEST="$predecessor_manifest_url"
  export MARGINALIA_DEFAULT_MANIFEST_URL="$predecessor_manifest_url"
  unset OKTO_NEURON_NO_MCP OKTO_NEURON_NO_OPEN OKTO_NEURON_NO_UPDATE_SHELL OKTO_NEURON_VAULT \
    OKTO_NEURON_ONBOARD_NONINTERACTIVE OKTO_NEURON_LLM_PROVIDER OKTO_NEURON_EXPECTED_VERSION \
    OKTO_NEURON_MANIFEST OKTO_NEURON_DEFAULT_MANIFEST_URL
  cleanup_predecessor_migration() {
    local cli
    for cli in "$HOME/.local/bin/okto-neuron" "$HOME/.local/bin/marginalia"; do
      [ -x "$cli" ] || continue
      "$cli" stop --timeout 10 >/dev/null 2>&1 || true
    done
  }
  trap cleanup_predecessor_migration EXIT

  curl -fsSL "$predecessor_install_url" -o "$predecessor_install"
  curl -fsSL "$predecessor_manifest_url" -o "$predecessor_manifest"
  [ "$(sha256sum "$predecessor_install" | awk '{print $1}')" = "$predecessor_install_sha" ]
  [ "$(sha256sum "$predecessor_manifest" | awk '{print $1}')" = "$predecessor_manifest_sha" ]
  printf 'PREDECESSOR_COMMIT=%s\nPREDECESSOR_INSTALL_URL=%s\nPREDECESSOR_INSTALL_SHA256=%s\n' \
    "$predecessor_commit" "$predecessor_install_url" "$predecessor_install_sha"
  printf 'PREDECESSOR_MANIFEST_URL=%s\nPREDECESSOR_MANIFEST_SHA256=%s\n' \
    "$predecessor_manifest_url" "$predecessor_manifest_sha"
  bash "$predecessor_install"
  if [ "${OKTO_NEURON_TEST_FAIL_PREDECESSOR_BEFORE_STATUS:-}" = 1 ]; then
    echo "forced predecessor failure before status" >&2
    return 88
  fi

  export PATH="$HOME/.local/bin:$PATH"
  legacy_pid_file="$HOME/.marginalia/runtime/.marginalia/server.pid"
  legacy_token_file="$HOME/.marginalia/daemon-7777.token"
  for _ in $(seq 1 90); do
    status="$(marginalia status --json --timeout 5 2>/dev/null || true)"
    [ "$(json_field "$status" marginalia_version 2>/dev/null || true)" = "0.2.0" ] && break
    sleep 1
  done
  [ "$(json_field "$status" marginalia_version)" = "0.2.0" ]
  old_pid="$(json_field "$status" pid)"
  [ -n "$old_pid" ] && kill -0 "$old_pid"
  [ -f "$legacy_pid_file" ]
  [ -s "$legacy_token_file" ]
  [ -f "$legacy_vault/marginalia.yaml" ]
  legacy_config_sha="$(sha256sum "$legacy_vault/marginalia.yaml" | awk '{print $1}')"
  token_sha="$(sha256sum "$legacy_token_file" | awk '{print $1}')"
  sentinel="$(uv tool dir)/marginalia/.predecessor-v020-sentinel"
  printf 'immutable-predecessor:0.2.0\n' > "$sentinel"
  sentinel_sha="$(sha256sum "$sentinel" | awk '{print $1}')"
  echo RELEASE_LIFECYCLE_PREDECESSOR_RUNNING_OK

  export MARGINALIA_EXPECTED_VERSION="$successor_version"
  export MARGINALIA_MANIFEST="$successor_manifest"
  export MARGINALIA_DEFAULT_MANIFEST_URL="$successor_manifest"
  real_uv="$(command -v uv)"
  uv() {
    if [ "${OKTO_NEURON_FAIL_ACTIVATION:-}" = "1" ] \
       && [ "${1:-}" = "tool" ] && [ "${2:-}" = "install" ]; then
      return 77
    fi
    "$REAL_UV" "$@"
  }
  export -f uv
  export REAL_UV="$real_uv"
  export OKTO_NEURON_FAIL_ACTIVATION=1
  rollback_output="$HOME/predecessor-rollback.out"
  if run_raw_installer >"$rollback_output" 2>&1; then
    echo "forced successor activation failure unexpectedly succeeded" >&2
    return 87
  fi
  unset OKTO_NEURON_FAIL_ACTIVATION
  unset -f uv
  cat "$rollback_output"
  grep -Fq 'candidate activation failed' "$rollback_output"
  grep -Fq 'MARGINALIA_NO_MCP is deprecated; using it as OKTO_NEURON_NO_MCP' "$rollback_output"
  grep -Fq 'restored and restarted Marginalia 0.2.0' "$rollback_output"
  [ "$(marginalia --version | head -n 1)" = "marginalia 0.2.0" ]
  [ ! -e "$HOME/.local/bin/okto-neuron" ]
  [ -f "$sentinel" ]
  [ "$(sha256sum "$sentinel" | awk '{print $1}')" = "$sentinel_sha" ]
  [ "$(sha256sum "$legacy_token_file" | awk '{print $1}')" = "$token_sha" ]
  [ ! -e "$HOME/.marginalia/MOVED_TO" ]
  status="$(marginalia status --json --timeout 5)"
  rollback_pid="$(json_field "$status" pid)"
  [ "$(json_field "$status" marginalia_version)" = "0.2.0" ]
  [ -n "$rollback_pid" ] && [ "$rollback_pid" != "$old_pid" ] && kill -0 "$rollback_pid"
  [ -f "$legacy_pid_file" ]
  echo RELEASE_LIFECYCLE_PREDECESSOR_ROLLBACK_OK

  migration_output="$HOME/predecessor-migration.out"
  run_raw_installer >"$migration_output" 2>&1
  cat "$migration_output"
  status="$(verify_default_daemon)"
  migrated_pid="$(json_field "$status" pid)"
  [ -n "$migrated_pid" ] && [ "$migrated_pid" != "$rollback_pid" ] && kill -0 "$migrated_pid"
  [ "$(okto-neuron --version | head -n 1)" = "okto-neuron $successor_version" ]
  alias_err="$(marginalia --version 2>&1 >/dev/null || true)"
  printf '%s\n' "$alias_err" | grep -Fq "the 'marginalia' command is now 'okto-neuron'"
  app_pid_file="$HOME/.okto-neuron/runtime/.marginalia/server.pid"
  app_token_file="$HOME/.okto-neuron/daemon-7777.token"
  [ -f "$app_pid_file" ] || {
    echo "successor migration did not create the application PID record" >&2
    return 1
  }
  [ -s "$app_token_file" ] || {
    echo "successor migration did not adopt the application capability token" >&2
    return 1
  }
  [ "$(sha256sum "$app_token_file" | awk '{print $1}')" = "$token_sha" ] || {
    echo "successor migration rotated the application capability token" >&2
    return 1
  }
  [ "$(sha256sum "$legacy_token_file" | awk '{print $1}')" = "$token_sha" ] || {
    echo "successor migration modified the predecessor capability token" >&2
    return 1
  }
  [ -f "$legacy_vault/marginalia.yaml" ] \
    && [ "$(sha256sum "$legacy_vault/marginalia.yaml" | awk '{print $1}')" = "$legacy_config_sha" ] || {
    echo "successor migration moved or rewrote the legacy vault config" >&2
    return 1
  }
  [ ! -e "$HOME/.okto-neuron/vaults/legacy-vault" ] || {
    echo "successor migration copied the vault into the new home" >&2
    return 1
  }
  [ -f "$HOME/.marginalia/MOVED_TO" ] || {
    echo "successor migration did not leave a MOVED_TO pointer" >&2
    return 1
  }
  [ ! -e "$sentinel" ] || {
    echo "successor migration retained the predecessor tool environment" >&2
    return 1
  }
  okto-neuron stop
  wait_for_daemon_stopped "$migrated_pid" 7777 8201
  echo RELEASE_LIFECYCLE_PREDECESSOR_MIGRATION_OK
)

run_release_lifecycle() {
  local status fresh_pid stopped_output running_before running_after custom_status custom_pid
  local custom_output guard_runtime guard_pid guard_output rollback_before rollback_after
  local rollback_output config_sha tool_root real_uv rollback_sentinel
  local rollback_sentinel_sha token_file initial_token

  status="$(verify_default_daemon)"
  fresh_pid="$(json_field "$status" pid)"
  [ -n "$fresh_pid" ]
  [ "$(json_field "$status" vault_count)" = "0" ]
  [ ! -d "$HOME/.okto-neuron/vaults/$VAULT" ]
  echo RELEASE_LIFECYCLE_APP_FIRST_OK
  okto-neuron vault create "$VAULT" --use
  status="$(verify_default_daemon)"
  [ "$(json_field "$status" vault_count)" = "1" ]
  token_file="$HOME/.okto-neuron/daemon-7777.token"
  initial_token="$(cat "$token_file")"
  [ -n "$initial_token" ]
  echo RELEASE_LIFECYCLE_FRESH_INSTALL_OK
  echo RELEASE_LIFECYCLE_STATUS_UI_OK

  config_sha="$(sha256sum "$HOME/.okto-neuron/vaults/$VAULT/okto-neuron.yaml" | awk '{print $1}')"
  okto-neuron stop
  wait_for_daemon_stopped "$fresh_pid" 7777 8201 || return 81
  stopped_output="$HOME/stopped-update.out"
  run_raw_installer >"$stopped_output" 2>&1
  cat "$stopped_output"
  grep -Fq 'daemon remains stopped' "$stopped_output"
  wait_for_daemon_stopped "$fresh_pid" 7777 8201 || return 82
  [ "$(sha256sum "$HOME/.okto-neuron/vaults/$VAULT/okto-neuron.yaml" | awk '{print $1}')" = "$config_sha" ]
  [ "$(cat "$token_file")" = "$initial_token" ]
  echo RELEASE_LIFECYCLE_STOPPED_UPDATE_OK

  okto-neuron serve --daemon --no-open
  status="$(verify_default_daemon)"
  running_before="$(json_field "$status" pid)"
  run_raw_installer
  status="$(verify_default_daemon)"
  running_after="$(json_field "$status" pid)"
  [ -n "$running_before" ] && [ -n "$running_after" ]
  [ "$running_before" != "$running_after" ]
  [ "$(sha256sum "$HOME/.okto-neuron/vaults/$VAULT/okto-neuron.yaml" | awk '{print $1}')" = "$config_sha" ]
  [ "$(cat "$token_file")" = "$initial_token" ]
  tool_root="$(uv tool dir)"
  rollback_sentinel="${tool_root}/okto-neuron/.release-lifecycle-previous-tool-sentinel"
  [ ! -e "$rollback_sentinel" ]
  printf 'previous-tool-only:%s\n' "$EXPECTED_VERSION" > "$rollback_sentinel"
  rollback_sentinel_sha="$(sha256sum "$rollback_sentinel" | awk '{print $1}')"
  echo RELEASE_LIFECYCLE_RUNNING_UPDATE_OK

  okto-neuron stop
  wait_for_daemon_stopped "$running_after" 7777 8201
  okto-neuron serve --daemon --no-open --port 7788 --mcp-port 8202
  wait_for_health "http://127.0.0.1:7788"
  custom_status="$(OKTO_NEURON_ENDPOINT=http://127.0.0.1:7788 \
    okto-neuron status --json --timeout 5)"
  custom_pid="$(json_field "$custom_status" pid)"
  [ -n "$custom_pid" ]
  custom_output="$HOME/custom-port-refusal.out"
  export OKTO_NEURON_ENDPOINT=http://127.0.0.1:7788
  if run_raw_installer >"$custom_output" 2>&1; then
    echo "installer accepted a running custom-port daemon" >&2
    return 83
  fi
  unset OKTO_NEURON_ENDPOINT
  cat "$custom_output"
  grep -Fq 'uses custom endpoint http://127.0.0.1:7788' "$custom_output"
  kill -0 "$custom_pid"
  [ "$(okto-neuron --version | head -n 1)" = "okto-neuron $EXPECTED_VERSION" ]
  [ "$(sha256sum "$HOME/.okto-neuron/vaults/$VAULT/okto-neuron.yaml" | awk '{print $1}')" = "$config_sha" ]
  [ "$(cat "$token_file")" = "$initial_token" ]
  [ -f "$rollback_sentinel" ]
  [ "$(sha256sum "$rollback_sentinel" | awk '{print $1}')" = "$rollback_sentinel_sha" ]
  OKTO_NEURON_ENDPOINT=http://127.0.0.1:7788 okto-neuron stop
  wait_for_daemon_stopped "$custom_pid" 7788 8202
  echo RELEASE_LIFECYCLE_CUSTOM_PORT_REFUSAL_OK

  guard_runtime="$HOME/.okto-neuron/runtime"
  guard_output="$HOME/live-pid-refusal.out"
  (
    set -euo pipefail
    guard_python="${tool_root}/okto-neuron/bin/python"
    guard_pid_file="$guard_runtime/.marginalia/server.pid"
    guard_ready="$HOME/live-pid-refusal.ready"
    guard_stop="$HOME/live-pid-refusal.stop"
    guard_owner_log="$HOME/live-pid-refusal-owner.log"
    guard_job_pid=""
    guard_pid=""
    cleanup_guard_owner() {
      [ -n "${guard_job_pid:-}" ] || return 0
      : > "$guard_stop"
      for _ in $(seq 1 100); do
        kill -0 "$guard_job_pid" >/dev/null 2>&1 || break
        sleep 0.05
      done
      if kill -0 "$guard_job_pid" >/dev/null 2>&1; then
        kill -KILL "$guard_job_pid" >/dev/null 2>&1 || true
      fi
      wait "$guard_job_pid" 2>/dev/null || true
    }
    trap cleanup_guard_owner EXIT
    [ -x "$guard_python" ]
    rm -f "$guard_ready" "$guard_stop"
    "$guard_python" - "$guard_runtime" "$guard_ready" "$guard_stop" \
      >"$guard_owner_log" 2>&1 <<'PY' &
import os
from pathlib import Path
import sys
import time

from okto_neuron.server.lifecycle import PidFile

runtime_root, ready_path, stop_path = map(Path, sys.argv[1:])
with PidFile(runtime_root):
    ready_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
    while not stop_path.exists():
        time.sleep(0.05)
PY
    guard_job_pid=$!
    for _ in $(seq 1 200); do
      [ -s "$guard_ready" ] && break
      if ! kill -0 "$guard_job_pid" >/dev/null 2>&1; then
        wait "$guard_job_pid" 2>/dev/null || true
        cat "$guard_owner_log" >&2
        exit 84
      fi
      sleep 0.05
    done
    [ -s "$guard_ready" ]
    IFS= read -r guard_pid < "$guard_ready"
    [ "$guard_pid" = "$guard_job_pid" ]
    validate_guard_record() {
      "$guard_python" - "$guard_pid_file" "$guard_pid" <<'PY'
import json
from pathlib import Path
import sys

from okto_neuron.server import lifecycle

path = Path(sys.argv[1])
expected_pid = int(sys.argv[2])
payload = json.loads(path.read_text(encoding="utf-8"))


def fail(message: str) -> None:
    raise SystemExit(f"invalid canonical PID record: {message}")


if not isinstance(payload, dict):
    fail("payload is not an object")
if set(payload) != {"version", "pid", "start_token", "owner_id"}:
    fail("payload keys do not match the canonical schema")
if type(payload.get("version")) is not int:
    fail("version is not an integer")
if payload["version"] != lifecycle.PID_RECORD_VERSION:
    fail("version does not match the installed lifecycle contract")
if type(payload.get("pid")) is not int or payload["pid"] != expected_pid:
    fail("pid does not match the live owner")
if not isinstance(payload.get("start_token"), str) or not payload["start_token"]:
    fail("start_token is missing")
if not isinstance(payload.get("owner_id"), str) or not payload["owner_id"]:
    fail("owner_id is missing")
current_start = lifecycle._process_start_token(expected_pid)
if current_start is None or payload["start_token"] != current_start:
    fail("start_token does not match the live process birth identity")
print(
    "canonical application PID owner verified: "
    f"version={payload['version']} pid={expected_pid}"
)
PY
    }
    validate_guard_record
    guard_record_sha="$(sha256sum "$guard_pid_file" | awk '{print $1}')"
    if run_raw_installer >"$guard_output" 2>&1; then
      echo "installer accepted an unverified live PID record" >&2
      exit 84
    fi
    cat "$guard_output"
    grep -Fq "live Okto Neuron daemon (pid $guard_pid)" "$guard_output"
    grep -Fq "okto-neuron stop" "$guard_output"
    kill -0 "$guard_pid"
    validate_guard_record
    [ "$(sha256sum "$guard_pid_file" | awk '{print $1}')" = "$guard_record_sha" ]
    [ "$(okto-neuron --version | head -n 1)" = "okto-neuron $EXPECTED_VERSION" ]
    [ "$(sha256sum "$HOME/.okto-neuron/vaults/$VAULT/okto-neuron.yaml" | awk '{print $1}')" = "$config_sha" ]
    [ "$(cat "$token_file")" = "$initial_token" ]
    [ -f "$rollback_sentinel" ]
    [ "$(sha256sum "$rollback_sentinel" | awk '{print $1}')" = "$rollback_sentinel_sha" ]
    : > "$guard_stop"
    for _ in $(seq 1 100); do
      kill -0 "$guard_job_pid" >/dev/null 2>&1 || break
      sleep 0.05
    done
    if kill -0 "$guard_job_pid" >/dev/null 2>&1; then
      echo "canonical PID owner did not exit cleanly" >&2
      cat "$guard_owner_log" >&2
      exit 84
    fi
    wait "$guard_job_pid"
    guard_job_pid=""
    guard_pid=""
    [ ! -e "$guard_pid_file" ]
    rm -f "$guard_ready" "$guard_stop" "$guard_owner_log"
    trap - EXIT
  )
  echo RELEASE_LIFECYCLE_LIVE_PID_REFUSAL_OK

  okto-neuron serve --daemon --no-open
  status="$(verify_default_daemon)"
  rollback_before="$(json_field "$status" pid)"
  rollback_output="$HOME/activation-rollback.out"
  real_uv="$(command -v uv)"
  uv() {
    if [ "${OKTO_NEURON_FAIL_ACTIVATION:-}" = "1" ] \
       && [ "${1:-}" = "tool" ] && [ "${2:-}" = "install" ]; then
      return 77
    fi
    "$REAL_UV" "$@"
  }
  export -f uv
  export REAL_UV="$real_uv"
  export OKTO_NEURON_FAIL_ACTIVATION=1
  if run_raw_installer >"$rollback_output" 2>&1; then
    echo "forced activation failure unexpectedly succeeded" >&2
    return 85
  fi
  unset OKTO_NEURON_FAIL_ACTIVATION
  unset -f uv
  cat "$rollback_output"
  grep -Fq 'candidate activation failed' "$rollback_output"
  grep -Fq "restored and restarted Okto Neuron $EXPECTED_VERSION" "$rollback_output"
  status="$(verify_default_daemon)"
  rollback_after="$(json_field "$status" pid)"
  [ -n "$rollback_before" ] && [ -n "$rollback_after" ]
  [ "$rollback_before" != "$rollback_after" ]
  [ "$(okto-neuron --version | head -n 1)" = "okto-neuron $EXPECTED_VERSION" ]
  [ "$(sha256sum "$HOME/.okto-neuron/vaults/$VAULT/okto-neuron.yaml" | awk '{print $1}')" = "$config_sha" ]
  [ "$(cat "$token_file")" = "$initial_token" ]
  [ -f "$rollback_sentinel" ]
  [ "$(sha256sum "$rollback_sentinel" | awk '{print $1}')" = "$rollback_sentinel_sha" ]
  [ -z "$(find "$tool_root" -maxdepth 1 -name '.okto-neuron-installer-backup-*' -print -quit)" ]
  printf 'RELEASE_LIFECYCLE_PREVIOUS_TOOL_SENTINEL_SHA256=%s\n' "$rollback_sentinel_sha"
  echo RELEASE_LIFECYCLE_PREVIOUS_TOOL_SENTINEL_OK
  echo RELEASE_LIFECYCLE_ACTIVATION_ROLLBACK_OK

  okto-neuron stop
  wait_for_daemon_stopped "$rollback_after" 7777 8201 || return 86
  [ "$(cat "$token_file")" = "$initial_token" ]
  echo RELEASE_LIFECYCLE_FINAL_STOP_OK
  echo DOCKER_TMUX_RELEASE_LIFECYCLE_OK
}

export OKTO_NEURON_NO_MCP=1
# The first-run-prompt profiles run with OKTO_NEURON_NO_OPEN unset (it
# suppresses the prompt by contract); their `open` is shimmed below so the
# verified UI never launches a real browser.
case "$PROFILE" in
  onboard-prompt-yes|onboard-prompt-no|onboard-no-flag|onboard-upgrade-tty) ;;
  *) export OKTO_NEURON_NO_OPEN=1 ;;
esac
# Ephemeral container, but stay consistent - never persist PATH via
# uv tool update-shell in a sandbox run.
export OKTO_NEURON_NO_UPDATE_SHELL=1
# The greenfield (plain-path) profiles must not preseed OKTO_NEURON_VAULT.
case "$PROFILE" in
  release-lifecycle|onboard-prompt-yes|onboard-prompt-no|onboard-no-open|onboard-no-flag) ;;
  *) export OKTO_NEURON_VAULT="$VAULT" ;;
esac
export OKTO_NEURON_EXPECTED_VERSION="$EXPECTED_VERSION"
[ "$PROFILE" = "release-lifecycle" ] && export OKTO_NEURON_MANIFEST="$MANIFEST_URL"
[ "$NO_SERVE" = "1" ] && export OKTO_NEURON_NO_SERVE=1

case "$PROFILE" in
  existing-keep|existing-inspect|existing-reconfigure|disable-llm|onboard-upgrade-tty)
    seed_existing_config
    ;;
esac
case "$PROFILE" in
  onboard-prompt-yes|onboard-prompt-no|onboard-no-flag|onboard-upgrade-tty)
    mkdir -p "$HOME/.sandbox-bin"
    printf '#!/bin/sh\nexit 0\n' > "$HOME/.sandbox-bin/open"
    printf '#!/bin/sh\nexit 0\n' > "$HOME/.sandbox-bin/xdg-open"
    chmod +x "$HOME/.sandbox-bin/open" "$HOME/.sandbox-bin/xdg-open"
    export PATH="$HOME/.sandbox-bin:$PATH"
    ;;
esac

if [ "$PROFILE" = "lm-studio" ] || [ "$PROFILE" = "auto-lm-studio" ] || [ "$PROFILE" = "existing-reconfigure" ]; then
  start_mock_lm_studio
elif [ "$PROFILE" = "ollama" ]; then
  start_mock_ollama
elif [ "$PROFILE" = "litellm" ]; then
  start_mock_litellm
elif [ "$PROFILE" = "hosted-openai" ] || [ "$PROFILE" = "hosted-openrouter" ] || [ "$PROFILE" = "hosted-gemini" ] || [ "$PROFILE" = "hosted-anthropic" ]; then
  export OKTO_NEURON_LLM_MODEL="$MODEL"
  export OKTO_NEURON_LLM_API_KEY_ENV=OKTO_NEURON_HOSTED_TEST_KEY
  export OKTO_NEURON_HOSTED_TEST_KEY=fake-public-installer-test-key
elif [ "$PROFILE" = "custom-rootform" ]; then
  start_mock_custom_openai
fi

if [ "$PROFILE" = "release-lifecycle" ]; then
  printf 'DRIVER_COMMIT=%s\nDRIVER_URL=%s\nDRIVER_SHA256=%s\n' \
    "$DRIVER_COMMIT" "$DRIVER_URL" "$DRIVER_SHA256"
  printf 'INSTALL_URL=%s\nINSTALL_SHA256=%s\nMANIFEST_URL=%s\nMANIFEST_SHA256=%s\n' \
    "$INSTALL_URL" "$INSTALL_SHA256" "$MANIFEST_URL" "$MANIFEST_SHA256"
  run_predecessor_migration
fi
printf 'PROFILE=%s\n' "$PROFILE"
case "$PROFILE" in
  onboard-no-flag) curl -fsSL "$INSTALL_URL" | bash -s -- --no-onboard ;;
  *) curl -fsSL "$INSTALL_URL" | bash ;;
esac

export PATH="$HOME/.local/bin:$PATH"
okto-neuron --help | sed -n '1,12p'
CLI_VERSION="$(okto-neuron --version | head -n 1)"
[ "$CLI_VERSION" = "okto-neuron $EXPECTED_VERSION" ]
case "$PROFILE" in
  release-lifecycle) ;;
  onboard-prompt-yes)
    CURRENT_VAULT="$(okto-neuron vault current)"
    [ "$(cd "$CURRENT_VAULT" && pwd)" = \
      "$(cd "$HOME/.okto-neuron/vaults/onboarded-vault" && pwd)" ]
    printf '%s\n' "$CURRENT_VAULT"
    [ -f "$HOME/.okto-neuron/vaults/onboarded-vault/okto-neuron.yaml" ]
    ! grep -q '^llm:' "$HOME/.okto-neuron/vaults/onboarded-vault/okto-neuron.yaml"
    ;;
  onboard-prompt-no|onboard-no-open|onboard-no-flag)
    [ -z "$(find "$HOME/.okto-neuron/vaults" -name okto-neuron.yaml -print -quit 2>/dev/null || true)" ]
    ;;
  onboard-upgrade-tty)
    [ -f "$HOME/.okto-neuron/vaults/$VAULT/okto-neuron.yaml" ]
    grep -q 'preexisting-model' "$HOME/.okto-neuron/vaults/$VAULT/okto-neuron.yaml"
    ;;
  *)
    okto-neuron vault current
    ;;
esac
# onboard-upgrade-tty intentionally keeps a stopped daemon stopped.
if [ "$NO_SERVE" != "1" ] && [ "$PROFILE" != "onboard-upgrade-tty" ]; then
  curl -fsS http://127.0.0.1:7777/health
  printf '\n'
  TOOL_PYTHON="$(uv tool dir)/okto-neuron/bin/python"
  SERVER_VERSION="$(curl -fsS http://127.0.0.1:7777/version | \
    "$TOOL_PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["okto_neuron_version"])')"
  [ "$SERVER_VERSION" = "$EXPECTED_VERSION" ]
fi
YAML="$HOME/.okto-neuron/vaults/$VAULT/okto-neuron.yaml"
case "$PROFILE" in
  skip)
    ! grep -q '^llm:' "$YAML"
    ;;
  release-lifecycle)
    [ ! -e "$YAML" ]
    ;;
  lm-studio)
    grep -q 'provider: lm_studio' "$YAML"
    grep -q 'docker-human-model' "$YAML"
    ;;
  auto-lm-studio)
    grep -q 'provider: lm_studio' "$YAML"
    grep -q 'docker-human-model' "$YAML"
    ;;
  ollama)
    grep -q 'provider: ollama' "$YAML"
    grep -q 'docker-ollama-human-model' "$YAML"
    ;;
  litellm)
    grep -q 'provider: litellm_proxy' "$YAML"
    grep -q 'api_base: http://127.0.0.1:4000' "$YAML"
    grep -q 'docker-litellm-human-model' "$YAML"
    ! grep -q 'should-not-surface' "$YAML"
    ;;
  hosted-openai)
    grep -q 'provider: openai' "$YAML"
    grep -q "$MODEL" "$YAML"
    grep -q 'api_key_env: OKTO_NEURON_HOSTED_TEST_KEY' "$YAML"
    ! grep -q 'fake-public-installer-test-key' "$YAML"
    ;;
  hosted-openrouter)
    grep -q 'provider: openrouter' "$YAML"
    grep -q "$MODEL" "$YAML"
    grep -q 'api_key_env: OKTO_NEURON_HOSTED_TEST_KEY' "$YAML"
    ! grep -q 'fake-public-installer-test-key' "$YAML"
    ;;
  hosted-gemini)
    grep -q 'provider: gemini' "$YAML"
    grep -q 'api_base: https://generativelanguage.googleapis.com/v1beta/openai/' "$YAML"
    grep -q "$MODEL" "$YAML"
    grep -q 'api_key_env: OKTO_NEURON_HOSTED_TEST_KEY' "$YAML"
    ! grep -q 'fake-public-installer-test-key' "$YAML"
    ;;
  hosted-anthropic)
    grep -q 'provider: anthropic' "$YAML"
    grep -q "$MODEL" "$YAML"
    grep -q 'api_key_env: OKTO_NEURON_HOSTED_TEST_KEY' "$YAML"
    ! grep -q 'fake-public-installer-test-key' "$YAML"
    ;;
  custom)
    grep -q "$MODEL" "$YAML"
    ;;
  existing-keep)
    grep -q 'preexisting-model' "$YAML"
    grep -q 'api_base: http://127.0.0.1:9999/v1' "$YAML"
    ;;
  existing-inspect)
    grep -q 'preexisting-model' "$YAML"
    grep -q 'api_base: http://127.0.0.1:9999/v1' "$YAML"
    ;;
  existing-reconfigure)
    grep -q 'provider: lm_studio' "$YAML"
    grep -q 'docker-human-model' "$YAML"
    ! grep -q 'preexisting-model' "$YAML"
    ;;
  custom-rootform)
    grep -q 'provider: openai' "$YAML"
    grep -q 'api_base: http://127.0.0.1:18123/v1' "$YAML"
    grep -q 'fake-chat-model' "$YAML"
    ! grep -q 'fake-nonchat-first' "$YAML"
    ;;
  disable-llm)
    grep -q 'enabled: false' "$YAML"
    ;;
esac
if [ "$PROFILE" = "custom-rootform" ]; then
  run_custom_rootform_followups
fi
if [ "$PROFILE" = "release-lifecycle" ]; then
  run_release_lifecycle
else
  okto-neuron stop >/dev/null 2>&1 || true
  echo DOCKER_TMUX_HUMAN_INSTALL_OK
fi
RUNNER
  chmod +x "$runner"
  printf '%s\n' "$runner"
}

capture() {
  tmux capture-pane -pt "$SESSION" -S -4000 > "$EVIDENCE" || true
}

wait_for_text() {
  local needle="$1" timeout="$2" start now pane_dead pane_status
  start="$(date +%s)"
  while true; do
    capture
    if grep -Fq "$needle" "$EVIDENCE"; then
      return 0
    fi
    pane_dead="$(tmux display-message -p -t "$SESSION" '#{pane_dead}' 2>/dev/null || true)"
    if [ "$pane_dead" = "1" ]; then
      pane_status="$(tmux display-message -p -t "$SESSION" \
        '#{pane_dead_status}' 2>/dev/null || true)"
      printf 'tmux pane exited with status %s before %s; evidence: %s\n' \
        "${pane_status:-unknown}" "$needle" "$EVIDENCE" >&2
      return 1
    fi
    now="$(date +%s)"
    if [ $((now - start)) -ge "$timeout" ]; then
      printf 'timed out waiting for %s; evidence: %s\n' "$needle" "$EVIDENCE" >&2
      return 1
    fi
    sleep 1
  done
}

wait_for_pane_success() {
  local timeout="$1" start now pane_dead pane_status
  start="$(date +%s)"
  while true; do
    pane_dead="$(tmux display-message -p -t "$SESSION" '#{pane_dead}' 2>/dev/null || true)"
    if [ "$pane_dead" = "1" ]; then
      pane_status="$(tmux display-message -p -t "$SESSION" \
        '#{pane_dead_status}' 2>/dev/null || true)"
      capture
      if [ "$pane_status" != "0" ]; then
        printf 'tmux pane exited with status %s after success marker; evidence: %s\n' \
          "${pane_status:-unknown}" "$EVIDENCE" >&2
        return 1
      fi
      return 0
    fi
    now="$(date +%s)"
    if [ $((now - start)) -ge "$timeout" ]; then
      capture
      printf 'timed out waiting for tmux pane to exit after success marker; evidence: %s\n' \
        "$EVIDENCE" >&2
      return 1
    fi
    sleep 1
  done
}

drive_profile() {
  case "$PROFILE" in
    interactive)
      printf 'tmux session started. Attach and drive prompts:\n  tmux attach -t %s\n' "$SESSION"
      return
      ;;
    release-lifecycle)
      wait_for_text "RELEASE_LIFECYCLE_PREDECESSOR_RUNNING_OK" 900
      ;;
    skip)
      # 0.0.47+ interactive onboard asks the graph-backend question before the
      # provider menu; accept the default (grafx) and continue.
      wait_for_text "Graph backend" 900
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "Provider" 120
      tmux send-keys -t "$SESSION" "0" C-m
      ;;
    auto-lm-studio)
      wait_for_text "Provider" 900
      tmux send-keys -t "$SESSION" "1" C-m
      wait_for_text "Base URL" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "API key" 120
      tmux send-keys -t "$SESSION" C-m
      ;;
    lm-studio)
      wait_for_text "Provider" 900
      tmux send-keys -t "$SESSION" "2" C-m
      wait_for_text "Base URL" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "API key" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "Model" 120
      tmux send-keys -t "$SESSION" C-m
      ;;
    ollama)
      wait_for_text "Provider" 900
      tmux send-keys -t "$SESSION" "3" C-m
      wait_for_text "Base URL" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "API key" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "Model" 120
      tmux send-keys -t "$SESSION" C-m
      ;;
    litellm)
      wait_for_text "Provider" 900
      tmux send-keys -t "$SESSION" "4" C-m
      wait_for_text "Base URL" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "API key" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "Model" 120
      tmux send-keys -t "$SESSION" C-m
      ;;
    hosted-openai)
      wait_for_text "Provider" 900
      tmux send-keys -t "$SESSION" "6" C-m
      wait_for_text "Base URL" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "Allow this endpoint?" 120
      tmux send-keys -t "$SESSION" "y" C-m
      wait_for_text "API key" 120
      tmux send-keys -t "$SESSION" C-m
      ;;
    hosted-openrouter)
      wait_for_text "Provider" 900
      tmux send-keys -t "$SESSION" "5" C-m
      wait_for_text "Base URL" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "Allow this endpoint?" 120
      tmux send-keys -t "$SESSION" "y" C-m
      wait_for_text "API key" 120
      tmux send-keys -t "$SESSION" C-m
      ;;
    hosted-gemini)
      wait_for_text "Provider" 900
      tmux send-keys -t "$SESSION" "7" C-m
      wait_for_text "Base URL" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "Allow this endpoint?" 120
      tmux send-keys -t "$SESSION" "y" C-m
      wait_for_text "API key" 120
      tmux send-keys -t "$SESSION" C-m
      ;;
    hosted-anthropic)
      wait_for_text "Provider" 900
      tmux send-keys -t "$SESSION" "8" C-m
      wait_for_text "Base URL" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "Allow this endpoint?" 120
      tmux send-keys -t "$SESSION" "y" C-m
      wait_for_text "API key" 120
      tmux send-keys -t "$SESSION" C-m
      ;;
    custom)
      # Provider 11 since pi_cli (9) and codex_cli (10) joined the menu.
      wait_for_text "Provider" 900
      tmux send-keys -t "$SESSION" "11" C-m
      wait_for_text "Base URL" 120
      tmux send-keys -t "$SESSION" "$API_BASE" C-m
      wait_for_text "API key" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "Model" 120
      tmux send-keys -t "$SESSION" "$MODEL" C-m
      ;;
    custom-rootform)
      # 0.0.47+ interactive onboard asks the graph-backend question before the
      # provider menu on a fresh preseed vault; accept the default (grafx).
      wait_for_text "Graph backend" 900
      tmux send-keys -t "$SESSION" C-m
      # The base is typed in ROOT form (no /v1): the onboarding resolver must
      # derive {root}/v1 for both discovery and the pre-save verify.
      # Provider 11 since pi_cli (9) and codex_cli (10) joined the menu.
      wait_for_text "Provider" 120
      tmux send-keys -t "$SESSION" "11" C-m
      wait_for_text "Base URL" 120
      tmux send-keys -t "$SESSION" "http://127.0.0.1:18123" C-m
      wait_for_text "API key" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "Model" 120
      tmux send-keys -t "$SESSION" "fake-chat-model" C-m
      ;;
    existing-keep)
      wait_for_text "Action" 900
      tmux send-keys -t "$SESSION" "1" C-m
      ;;
    existing-inspect)
      wait_for_text "Action" 900
      tmux send-keys -t "$SESSION" "2" C-m
      ;;
    existing-reconfigure)
      wait_for_text "Action" 900
      tmux send-keys -t "$SESSION" "3" C-m
      wait_for_text "Provider" 120
      tmux send-keys -t "$SESSION" "2" C-m
      wait_for_text "Base URL" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "API key" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "Model" 120
      tmux send-keys -t "$SESSION" C-m
      ;;
    disable-llm)
      wait_for_text "Action" 900
      tmux send-keys -t "$SESSION" "4" C-m
      ;;
    onboard-prompt-yes)
      wait_for_text "Set up your vault and LLM provider now?" 900
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "Graph backend" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "Vault name" 120
      tmux send-keys -t "$SESSION" "onboarded-vault" C-m
      wait_for_text "Create vault at" 120
      tmux send-keys -t "$SESSION" C-m
      wait_for_text "Provider" 120
      tmux send-keys -t "$SESSION" "0" C-m
      ;;
    onboard-prompt-no)
      wait_for_text "Set up your vault and LLM provider now?" 900
      tmux send-keys -t "$SESSION" "n" C-m
      ;;
  esac
}

# Greenfield first-run prompt matrix row b (+ the upgrade row without a TTY):
# run the raw installer with NO controlling terminal, exactly like CI. The
# python setsid detaches the session; the fake `open` keeps the app-first
# browser launch side-effect-free. NO_SERVE is forced: the prompt gate lands
# before the serve step, and this scenario must not touch host ports.
run_onboard_non_tty_scenario() {
  local tool_bin="$TEST_HOME/.local/bin"
  local sandbox_bin="$TEST_HOME/.sandbox-bin"
  local out1="$TEST_HOME/evidence-non-tty-greenfield.txt"
  local out2="$TEST_HOME/evidence-non-tty-upgrade.txt"
  local runner_py="$TEST_HOME/run-detached-installer.py"

  command -v python3 >/dev/null 2>&1 || die "python3 is required for --profile onboard-non-tty"

  mkdir -p "$sandbox_bin"
  printf '#!/bin/sh\nexit 0\n' > "$sandbox_bin/open"
  printf '#!/bin/sh\nexit 0\n' > "$sandbox_bin/xdg-open"
  chmod +x "$sandbox_bin/open" "$sandbox_bin/xdg-open"

  cat > "$runner_py" <<'PY'
import os
import subprocess
import sys

# A new session with no controlling terminal: /dev/tty is unavailable, which
# is exactly what a CI or headless piped install sees.
os.setsid()
url = sys.argv[1]
proc = subprocess.run(
    ["bash", "-c", "curl -fsSL '%s' | bash" % url],
    stdin=subprocess.DEVNULL,
    capture_output=True,
    text=True,
    env=dict(os.environ),
)
sys.stdout.write(proc.stdout)
sys.stderr.write(proc.stderr)
sys.exit(proc.returncode)
PY

  export HOME="$TEST_HOME"
  export XDG_DATA_HOME="$TEST_HOME/.local/share"
  export XDG_CACHE_HOME="$TEST_HOME/.cache"
  export XDG_CONFIG_HOME="$TEST_HOME/.config"
  export UV_CACHE_DIR="$TEST_HOME/.cache/uv"
  export OKTO_NEURON_NO_MCP=1
  export OKTO_NEURON_NO_UPDATE_SHELL=1
  export OKTO_NEURON_NO_SERVE=1
  export OKTO_NEURON_EXPECTED_VERSION="$EXPECTED_VERSION"
  # Never leak preseed settings from the caller's environment into the
  # greenfield plain-path run.
  unset OKTO_NEURON_VAULT OKTO_NEURON_NO_OPEN OKTO_NEURON_ONBOARD_NONINTERACTIVE \
    OKTO_NEURON_PACKS OKTO_NEURON_LLM_PROVIDER OKTO_NEURON_LLM_API_BASE \
    OKTO_NEURON_LLM_MODEL OKTO_NEURON_LLM_API_KEY_ENV OKTO_NEURON_LLM_SKIP_DISCOVERY \
    OKTO_NEURON_LLM_ALLOW_REMOTE OKTO_NEURON_ALLOW_REMOTE_LLM

  printf 'ONBOARD_NON_TTY_SCENARIO\n'
  printf 'run 1: greenfield, no controlling terminal (CI-like)\n'
  PATH="$sandbox_bin:$PATH" python3 "$runner_py" "$INSTALL_URL" > "$out1" 2>&1 \
    || die "greenfield non-TTY run failed: $out1"
  cat "$out1"
  grep -Fq "Set up your vault and LLM provider now?" "$out1" \
    && die "greenfield non-TTY run printed the first-run prompt: $out1"
  grep -Fq "Okto Neuron first run: no vault configured." "$out1" \
    && die "greenfield non-TTY run printed the first-run prompt: $out1"
  grep -Fq "no vault preseed requested; create, select, and configure vaults in the Web UI" "$out1" \
    || die "greenfield non-TTY run did not take the application-first path: $out1"
  grep -Fq "Okto Neuron ${EXPECTED_VERSION} installed; daemon was not started." "$out1" \
    || die "greenfield non-TTY run did not finish: $out1"
  echo ONBOARD_NON_TTY_GREENFIELD_SKIP_OK

  PATH="$tool_bin:$PATH" okto-neuron vault create non-tty-upgrade-vault --use \
    || die "could not create the upgrade fixture vault"

  printf 'run 2: upgrade (prior tool + existing vault), still no controlling terminal\n'
  PATH="$tool_bin:$sandbox_bin:$PATH" python3 "$runner_py" "$INSTALL_URL" > "$out2" 2>&1 \
    || die "upgrade non-TTY run failed: $out2"
  cat "$out2"
  grep -Fq "Set up your vault and LLM provider now?" "$out2" \
    && die "upgrade non-TTY run printed the first-run prompt: $out2"
  grep -Fq "Okto Neuron first run: no vault configured." "$out2" \
    && die "upgrade non-TTY run printed the first-run prompt: $out2"
  grep -Fq "Update mode" "$out2" \
    || die "upgrade non-TTY run did not take the update path: $out2"
  echo ONBOARD_NON_TTY_UPGRADE_NO_PROMPT_OK
  echo ONBOARD_NON_TTY_OK
}

run_direct() {
  local xdg_data="$TEST_HOME/.local/share"
  local xdg_cache="$TEST_HOME/.cache"
  local xdg_config="$TEST_HOME/.config"
  local uv_cache="$TEST_HOME/.cache/uv"
  local tool_bin="$TEST_HOME/.local/bin"

  if [ "$PROFILE" = "onboard-non-tty" ]; then
    run_onboard_non_tty_scenario
    return
  fi

  printf 'Okto Neuron public installer test\n'
  printf '  installer: %s\n' "$INSTALL_URL"
  printf '  test HOME: %s\n' "$TEST_HOME"
  printf '  vault:     %s\n' "$VAULT"
  printf '  MCP:       disabled (OKTO_NEURON_NO_MCP=1)\n\n'

  (
    export HOME="$TEST_HOME"
    export XDG_DATA_HOME="$xdg_data"
    export XDG_CACHE_HOME="$xdg_cache"
    export XDG_CONFIG_HOME="$xdg_config"
    export UV_CACHE_DIR="$uv_cache"
    export OKTO_NEURON_NO_MCP=1
    export OKTO_NEURON_NO_OPEN=1
    # HOME is redirected here but never let a sandbox run persist PATH via
    # uv tool update-shell regardless.
    export OKTO_NEURON_NO_UPDATE_SHELL=1
    export OKTO_NEURON_VAULT="$VAULT"
    export OKTO_NEURON_EXPECTED_VERSION="$EXPECTED_VERSION"
    [ "$NO_SERVE" -eq 1 ] && export OKTO_NEURON_NO_SERVE=1
    [ -n "$PROVIDER" ] && export OKTO_NEURON_LLM_PROVIDER="$PROVIDER"
    [ -n "$API_BASE" ] && export OKTO_NEURON_LLM_API_BASE="$API_BASE"
    [ -n "$MODEL" ] && export OKTO_NEURON_LLM_MODEL="$MODEL"
    if [ -n "$API_BASE" ] || [ -n "$MODEL" ] || [ -n "$PROVIDER" ]; then
      export OKTO_NEURON_ONBOARD_NONINTERACTIVE=1
    fi
    curl -fsSL "$INSTALL_URL" | bash
  )

  if [ -x "$tool_bin/okto-neuron" ]; then
    printf '\nInstalled CLI:\n'
    PATH="$tool_bin:$PATH" "$tool_bin/okto-neuron" --help | sed -n '1,12p'
    CLI_VERSION="$(PATH="$tool_bin:$PATH" "$tool_bin/okto-neuron" --version | head -n 1)"
    [ "$CLI_VERSION" = "okto-neuron $EXPECTED_VERSION" ] \
      || die "CLI version '$CLI_VERSION' does not match $EXPECTED_VERSION"
    if [ "$NO_SERVE" -ne 1 ]; then
      tool_python="$(HOME="$TEST_HOME" XDG_DATA_HOME="$TEST_HOME/.local/share" uv tool dir)/okto-neuron/bin/python"
      SERVER_VERSION="$(curl -fsS http://127.0.0.1:7777/version | \
        "$tool_python" -c 'import json,sys; print(json.load(sys.stdin)["okto_neuron_version"])')"
      [ "$SERVER_VERSION" = "$EXPECTED_VERSION" ] \
        || die "server version '$SERVER_VERSION' does not match $EXPECTED_VERSION"
    fi
    printf 'Verified CLI/server version: %s\n' "$EXPECTED_VERSION"
  fi
}

run_host_tmux() {
  local runner
  runner="$(write_host_runner)"
  remove_owned_tmux_session
  tmux new-session -d -s "$SESSION" -x 200 -y 90 \
    "INSTALL_URL='$INSTALL_URL' TEST_HOME='$TEST_HOME' VAULT='$VAULT' PROFILE='$PROFILE' NO_SERVE='$NO_SERVE' MODEL='$MODEL' EXPECTED_VERSION='$EXPECTED_VERSION' bash '$runner'"
  tmux set-option -t "$SESSION" remain-on-exit on
  tmux set-option -t "$SESSION" "$RESOURCE_OWNER_KEY" "$SANDBOX_MARKER_VALUE"
  drive_profile
  if [ "$PROFILE" != "interactive" ]; then
    wait_for_text "MAC_TMUX_HUMAN_INSTALL_OK" 1800
    wait_for_pane_success 30
    if profile_uses_fake_secret; then
      if grep -q 'fake-public-installer-test-key' "$EVIDENCE"; then
        die "fake hosted secret leaked into tmux evidence: $EVIDENCE"
      fi
    fi
    if [ "$PROFILE" = "existing-inspect" ]; then
      grep -Fq "Testing existing LLM config" "$EVIDENCE" || die "inspection branch did not run: $EVIDENCE"
      grep -Fq "config unchanged" "$EVIDENCE" || die "inspection branch did not preserve config: $EVIDENCE"
    fi
    if [ "$PROFILE" = "custom-rootform" ]; then
      check_custom_rootform_evidence
    fi
    printf 'tmux host install passed. Evidence: %s\n' "$EVIDENCE"
  fi
}

run_docker_tmux() {
  local runner install_url="$INSTALL_URL" install_mount="" install_file=""
  runner="$(write_docker_runner)"
  # A local file:// installer cannot be read inside the container; mount it
  # read-only and hand the container the in-container path.
  case "$install_url" in
    file://*)
      install_file="${install_url#file://}"
      [ -f "$install_file" ] || die "local installer file not found: $install_file"
      install_mount="-v $install_file:/local-install.sh:ro"
      install_url="file:///local-install.sh"
      ;;
  esac
  remove_owned_tmux_session
  remove_owned_container
  tmux new-session -d -s "$SESSION" -x 200 -y 90 \
    "docker run --rm -it --name '$CONTAINER' --label '$DOCKER_OWNER_LABEL=$SANDBOX_MARKER_VALUE' $install_mount -e INSTALL_URL='$install_url' -e VAULT='$VAULT' -e PROFILE='$PROFILE' -e NO_SERVE='$NO_SERVE' -e MODEL='$MODEL' -e EXPECTED_VERSION='$EXPECTED_VERSION' -e DRIVER_COMMIT='$DRIVER_COMMIT' -e DRIVER_URL='$DRIVER_URL' -e DRIVER_SHA256='$DRIVER_SHA256' -e INSTALL_SHA256='$INSTALL_SHA256' -e MANIFEST_URL='$MANIFEST_URL' -e MANIFEST_SHA256='$MANIFEST_SHA256' -v '$runner:/runner.sh:ro' ubuntu:24.04 bash /runner.sh"
  tmux set-option -t "$SESSION" remain-on-exit on
  tmux set-option -t "$SESSION" "$RESOURCE_OWNER_KEY" "$SANDBOX_MARKER_VALUE"
  drive_profile
  if [ "$PROFILE" != "interactive" ]; then
    if [ "$PROFILE" = "release-lifecycle" ]; then
      wait_for_text "DOCKER_TMUX_RELEASE_LIFECYCLE_OK" 3600
    else
      wait_for_text "DOCKER_TMUX_HUMAN_INSTALL_OK" 2400
    fi
    wait_for_pane_success 30
    if profile_uses_fake_secret; then
      if grep -q 'fake-public-installer-test-key' "$EVIDENCE"; then
        die "fake hosted secret leaked into tmux evidence: $EVIDENCE"
      fi
    fi
    if [ "$PROFILE" = "existing-inspect" ]; then
      grep -Fq "Testing existing LLM config" "$EVIDENCE" || die "inspection branch did not run: $EVIDENCE"
      grep -Fq "config unchanged" "$EVIDENCE" || die "inspection branch did not preserve config: $EVIDENCE"
    fi
    if [ "$PROFILE" = "custom-rootform" ]; then
      check_custom_rootform_evidence
    fi
    printf 'Docker tmux install passed. Evidence: %s\n' "$EVIDENCE"
  fi
}

init_paths
if [ "$PROFILE" = "release-lifecycle" ]; then
  prepare_release_provenance
elif [ -z "$EXPECTED_VERSION" ]; then
  die "OKTO_NEURON_EXPECTED_VERSION is required by this unbaked tester"
fi
prepare_home
if [ "$CLEANUP" -eq 1 ]; then
  trap cleanup_sandbox EXIT
fi

case "$MODE" in
  direct) run_direct ;;
  tmux) run_host_tmux ;;
  docker-tmux) run_docker_tmux ;;
esac

printf '\nTest install finished.\n'
if [ "$CLEANUP" -eq 1 ]; then
  printf 'Test home cleaned: %s\n' "$TEST_HOME"
else
  printf 'Sandbox/evidence kept at:\n  %s\n' "$TEST_HOME"
  [ -n "$EVIDENCE" ] && [ -f "$EVIDENCE" ] && printf 'Evidence:\n  %s\n' "$EVIDENCE"
fi
