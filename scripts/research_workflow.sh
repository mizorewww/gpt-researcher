#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV_BIN="${UV_BIN:-$(command -v uv || true)}"

if [[ -z "$UV_BIN" ]]; then
  echo "ERROR: uv is required but was not found on PATH." >&2
  exit 2
fi

exec "$UV_BIN" run --directory "$PROJECT_ROOT" gptr-workflow \
  --project-root "$PROJECT_ROOT" "$@"
