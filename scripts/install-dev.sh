#!/usr/bin/env bash
# POSIX wrapper for the cross-platform Python installer.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -n "${PYTHON:-}" ]]; then
  exec "${PYTHON}" "${REPO_ROOT}/scripts/install-dev.py" "$@"
fi

for candidate in python3 python; do
  if command -v "${candidate}" >/dev/null 2>&1; then
    exec "${candidate}" "${REPO_ROOT}/scripts/install-dev.py" "$@"
  fi
done

echo "error: Python 3 not found on PATH." >&2
exit 1
