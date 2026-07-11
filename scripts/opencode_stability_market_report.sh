#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  cat <<'EOF'
Usage:
  scripts/opencode_stability_market_report.sh single [harness options]
  scripts/opencode_stability_market_report.sh stress [harness options]
  scripts/opencode_stability_market_report.sh dry-single [harness options]
  scripts/opencode_stability_market_report.sh dry-stress [harness options]
  scripts/opencode_stability_market_report.sh revalidate MANIFEST [OUTPUT]

Commands:
  single       Run one OpenCode session and one three-work-item report.
  stress       Run three attached OpenCode sessions (up to nine Codex calls at once).
  dry-single   Build and validate an isolated single-run config; make no external calls.
  dry-stress   Build and validate an isolated stress config; make no external calls.
  revalidate   Read-only revalidation of a prior immutable manifest and its reports.

Common environment overrides:
  TARGET_DATE=YYYY-MM-DD              Defaults to yesterday in REPORT_TIMEZONE.
  REPORT_TIMEZONE=Asia/Singapore      Freezes relative dates at submission.
  RUN_ID=my-run                       Must be unique; generated automatically otherwise.
  MODEL=deepseek/deepseek-v4-pro      OpenCode coordinator model.
  HARNESS_TIMEOUT_SECONDS=3000        Whole harness deadline.
  OPENCODE_BIN=/path/to/opencode      Override OpenCode executable discovery.
  RUNTIME_BASE_DIR=/path/to/root      Relocate runtime, logs, and output artifacts.

Examples:
  TARGET_DATE=2026-07-10 scripts/opencode_stability_market_report.sh single
  TARGET_DATE=2026-07-10 scripts/opencode_stability_market_report.sh stress
  scripts/opencode_stability_market_report.sh dry-stress
  scripts/opencode_stability_market_report.sh revalidate \
    outputs/stability/<run-id>/manifest.json

This is the market-specific acceptance harness. For generic AGENTS/skills/MCP
workflows, use scripts/research_workflow.sh and docs/GENERIC_RESEARCH_WORKFLOWS.md.
See docs/OPENCODE_MCP_WORKFLOW.md for market acceptance details.
EOF
}

COMMAND="${1:-${HARNESS_MODE:-single}}"
case "$COMMAND" in
  -h|--help|help)
    usage
    exit 0
    ;;
  single|stress)
    MODE="$COMMAND"
    [[ $# -eq 0 ]] || shift
    ;;
  dry-single)
    MODE="single"
    DRY_RUN=1
    export DRY_RUN
    shift
    ;;
  dry-stress)
    MODE="stress"
    DRY_RUN=1
    export DRY_RUN
    shift
    ;;
  revalidate)
    shift
    if [[ $# -lt 1 || $# -gt 2 ]]; then
      echo "ERROR: revalidate requires MANIFEST and accepts an optional OUTPUT." >&2
      usage >&2
      exit 2
    fi
    REVALIDATE_MANIFEST="$1"
    REVALIDATION_OUTPUT="${2:-$(dirname "$REVALIDATE_MANIFEST")/revalidation.json}"
    ;;
  *)
    echo "ERROR: unknown command: $COMMAND" >&2
    usage >&2
    exit 2
    ;;
esac

UV_BIN="${UV_BIN:-$(command -v uv || true)}"
if [[ -z "$UV_BIN" ]]; then
  echo "ERROR: uv is required but was not found on PATH." >&2
  exit 2
fi

if [[ "$COMMAND" == "revalidate" ]]; then
  exec "$UV_BIN" run --directory "$PROJECT_ROOT" \
    python scripts/opencode_market_report_harness.py \
    --revalidate-manifest "$REVALIDATE_MANIFEST" \
    --revalidation-output "$REVALIDATION_OUTPUT"
fi

ARGS=(
  --mode "$MODE"
  --project-root "$PROJECT_ROOT"
  --model "${MODEL:-deepseek/deepseek-v4-pro}"
  --timezone "${REPORT_TIMEZONE:-Asia/Singapore}"
  --timeout "${HARNESS_TIMEOUT_SECONDS:-3000}"
  --serve-start-timeout "${OPENCODE_SERVE_START_TIMEOUT_SECONDS:-30}"
  --uv-bin "$UV_BIN"
)

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  ARGS+=(--dry-run)
fi
if [[ -n "${RUN_ID:-}" ]]; then
  ARGS+=(--run-id "$RUN_ID")
fi
if [[ -n "${TARGET_DATE:-}" ]]; then
  ARGS+=(--target-date "$TARGET_DATE")
fi
if [[ -n "${RUNTIME_BASE_DIR:-}" ]]; then
  ARGS+=(--base-dir "$RUNTIME_BASE_DIR")
fi
if [[ -n "${OPENCODE_BIN:-}" ]]; then
  ARGS+=(--opencode-bin "$OPENCODE_BIN")
fi
if [[ -n "${OPENCODE_PORT:-}" ]]; then
  ARGS+=(--port "$OPENCODE_PORT")
fi

exec "$UV_BIN" run --directory "$PROJECT_ROOT" \
  python scripts/opencode_market_report_harness.py "${ARGS[@]}" "$@"
