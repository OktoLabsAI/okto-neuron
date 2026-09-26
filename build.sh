#!/usr/bin/env bash
# Build the Okto Neuron web UI (SPA) into repo-root frontend_dist/.
#
# The frontend lives in frontend/ (React + Vite + TS + Tailwind + Zustand).
# Vite's outDir is the repo-root frontend_dist/ (see frontend/vite.config.ts);
# the Starlette server (server/http.py::_webui_dir) serves that directory and
# pyproject's force-include ships it into the wheel as okto_neuron/_webui.
#
# Usage:
#   ./build.sh            # clean install + production build
#
# Requires: node + npm on PATH. After this runs, `okto-neuron serve` exposes the
# UI on http://127.0.0.1:7777 (REST/UI port). With no build present the server
# still boots API-only — CLI and MCP are unaffected.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FRONTEND_DIR="${REPO_ROOT}/frontend"
DIST_DIR="${REPO_ROOT}/frontend_dist"

if ! command -v npm >/dev/null 2>&1; then
  echo "error: npm not found on PATH. Install Node.js (https://nodejs.org) first." >&2
  exit 1
fi

echo "==> Installing frontend dependencies (npm ci) in ${FRONTEND_DIR}"
cd "${FRONTEND_DIR}"
npm ci

echo "==> Building SPA (npm run build → ${DIST_DIR})"
npm run build

echo "==> Done. Built UI is at ${DIST_DIR}"
echo "    Start the server with:  okto-neuron serve"
echo "    Dev mode:               okto-neuron dev"
echo "    UI:   http://127.0.0.1:7777"
echo "    MCP:  http://127.0.0.1:8201"
