#!/usr/bin/env bash
# A stand-in for `claude mcp add|get|remove`, for the installer wiring tests.
# One file per entry under $FAKE_CLAUDE_STATE (scope, URL, header lines).
# `get` prints what Claude Code 2.1.283 prints; the Status line is
# $FAKE_CLAUDE_STATUS ("connected" or anything else for a stopped daemon).
set -eu
state="${FAKE_CLAUDE_STATE:?}"
[ "${1:-}" = "mcp" ] || exit 2
case "${2:-}" in
  add)
    shift 2
    scope="" header="" name="" url=""
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --scope) scope="$2"; shift 2 ;;
        --transport) shift 2 ;;
        --header) header="$2"; shift 2 ;;
        *) if [ -z "${name}" ]; then name="$1"; else url="$1"; fi; shift ;;
      esac
    done
    [ ! -e "${state}/${name}" ] || { echo "MCP server ${name} already exists" >&2; exit 1; }
    printf '%s\n%s\n%s\n' "${scope}" "${url}" "${header}" > "${state}/${name}"
    ;;
  get)
    name="$3"
    [ -f "${state}/${name}" ] || { echo "No MCP server named \"${name}\"." >&2; exit 1; }
    { IFS= read -r scope; IFS= read -r url; IFS= read -r header; } < "${state}/${name}"
    case "${scope}" in
      local) label="Local config (private to you in this project)" ;;
      project) label="Project config (shared via .mcp.json)" ;;
      *) label="User config (available in all your projects)" ;;
    esac
    printf '%s:\n  Scope: %s\n' "${name}" "${label}"
    if [ "${FAKE_CLAUDE_STATUS:-}" = "connected" ]; then
      printf '  Status: \342\234\224 Connected\n'
    else
      printf '  Status: \342\234\230 Failed to connect\n'
      printf '  Issue: ECONNREFUSED: Unable to connect.\n'
    fi
    printf '  Type: http\n  URL: %s\n  Headers:\n    %s\n\n' "${url}" "${header}"
    printf 'To remove this server, run: claude mcp remove %s -s %s\n' "${name}" "${scope}"
    ;;
  remove)
    name="$3"
    [ -f "${state}/${name}" ] || exit 1
    rm -f "${state}/${name}"
    ;;
  *) exit 2 ;;
esac
