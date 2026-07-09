#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_ROOT/.env"
  set +a
fi

UV_BIN="${UV_BIN:-$(command -v uv || true)}"
OPENCODE_BIN="${OPENCODE_BIN:-$(command -v opencode || true)}"
MODEL="${MODEL:-deepseek/deepseek-v4-pro}"
MIN_RUNTIME_SECONDS="${MIN_RUNTIME_SECONDS:-1200}"
DRY_RUN="${DRY_RUN:-0}"
MCP_RESEARCH_JOB_TIMEOUT="${MCP_RESEARCH_JOB_TIMEOUT:-1800}"
MCP_RESEARCH_ATTEMPT_TIMEOUT="${MCP_RESEARCH_ATTEMPT_TIMEOUT:-1800}"
MCP_RESEARCH_MIXED_ATTEMPTS="${MCP_RESEARCH_MIXED_ATTEMPTS:-2}"

if [[ -z "$UV_BIN" ]]; then
  echo "ERROR: uv is required but was not found on PATH." >&2
  exit 1
fi

if [[ -z "$OPENCODE_BIN" ]]; then
  echo "ERROR: opencode is required but was not found on PATH." >&2
  exit 1
fi

if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
  echo "ERROR: DEEPSEEK_API_KEY is required. Put it in .env or export it." >&2
  exit 1
fi

RUN_ID="${RUN_ID:-$(date '+%Y%m%d_%H%M%S')}"
RUNTIME_DIR="${RUNTIME_DIR:-$PROJECT_ROOT/.tmp/opencode-stability-$RUN_ID}"
LOG_DIR="$PROJECT_ROOT/run_logs"
OUTPUT_DIR="$PROJECT_ROOT/outputs/stability"
RUN_LOG="$LOG_DIR/opencode-stability-market-report-$RUN_ID.jsonl"
FINAL_REPORT="$OUTPUT_DIR/opencode-stability-market-report-$RUN_ID.md"
OPENCODE_CONFIG_DIR="$RUNTIME_DIR/xdg_config/opencode"

mkdir -p "$OPENCODE_CONFIG_DIR" "$RUNTIME_DIR/xdg_data" "$LOG_DIR" "$OUTPUT_DIR"

cat >"$OPENCODE_CONFIG_DIR/opencode.jsonc" <<JSON
{
  "\$schema": "https://opencode.ai/config.json",
  "mcp": {
    "gpt-researcher-codex-long": {
      "type": "local",
      "command": [
        "$UV_BIN",
        "run",
        "python",
        "-m",
        "gpt_researcher.mcp_profile_server"
      ],
      "environment": {
        "GPT_RESEARCHER_PROFILE_DIR": "$PROJECT_ROOT",
        "RETRIEVER": "tavily,codex",
        "TAVILY_INCLUDE_RAW_CONTENT": "true",
        "TAVILY_SEARCH_DEPTH": "advanced",
        "COMPRESSION_FALLBACK_ON_ERROR": "true",
        "MCP_RESEARCH_ATTEMPT_TIMEOUT": "$MCP_RESEARCH_ATTEMPT_TIMEOUT",
        "MCP_RESEARCH_JOB_TIMEOUT": "$MCP_RESEARCH_JOB_TIMEOUT",
        "MCP_RESEARCH_MIXED_ATTEMPTS": "$MCP_RESEARCH_MIXED_ATTEMPTS",
        "CODEX_SEARCH_MODE": "search",
        "CODEX_SEARCH_TIMEOUT": "300",
        "CODEX_SEARCH_RETRIEVER_TIMEOUT": "300",
        "CODEX_SEARCH_RETRIEVER_DEBUG": "true",
        "CODEX_SEARCH_RETRIEVER_CONCURRENCY": "1",
        "CODEX_SEARCH_MODEL": "gpt-5.5",
        "CODEX_SEARCH_REASONING_EFFORT": "medium",
        "CODEX_SEARCH_SERVICE_TIER": "fast",
        "CODEX_SEARCH_SUPPORTS_WEBSOCKETS": "false"
      }
    }
  }
}
JSON

CURRENT_DATE="$(TZ=Asia/Singapore date '+%Y-%m-%d')"
if YESTERDAY="$(TZ=Asia/Singapore date -v-1d '+%Y-%m-%d' 2>/dev/null)"; then
  :
else
  YESTERDAY="$(TZ=Asia/Singapore date -d 'yesterday' '+%Y-%m-%d')"
fi

PROMPT_FILE="$RUNTIME_DIR/prompt.md"
cat >"$PROMPT_FILE" <<PROMPT
You are running a stability test for GPT Researcher MCP from zero.

Hard requirements:
- Use model/profile information from the MCP first via profile_info.
- Use ONLY the gpt-researcher MCP research tools for investigation jobs.
- Use research_report_start, not the synchronous research_report tool.
- Poll every job until a terminal status: completed, failed, timeout, or error.
- Every single MCP research job has a timeout budget of ${MCP_RESEARCH_JOB_TIMEOUT}s.
- Current date is ${CURRENT_DATE} in Asia/Singapore. ${YESTERDAY} is not a future date.
- Do not stop after the first report. You must ask the research tool to go deeper exactly three follow-up rounds after the primary report.
- Each deeper round must be based on gaps or weak evidence found in previous results:
  1. Round 1: missing/weak index, macro, commodities, and rate/inflation evidence.
  2. Round 2: missing/weak individual-stock evidence, including US megacap tech, Japan, Korea, Hong Kong, ADRs, and semiconductors.
  3. Round 3: cross-check contradictions, exact closing levels, percentage changes, commodity prices, source dates, and any stale/future-date risk.
- If any job fails, times out, or has low context, continue the remaining rounds where possible and document the failure in the final markdown.
- After the primary job plus exactly three deeper follow-up jobs finish, read the generated report files and write a final synthesized Markdown report to:
  ${FINAL_REPORT}
- The final Markdown must be in Chinese, serious and professional, and must include:
  - YAML frontmatter with model, started_at, current_date, target_date, job_ids, terminal statuses, elapsed_seconds, context_chars, visited_urls_count, and report paths.
  - A complete market daily report for ${YESTERDAY}: US, Japan, Korea, Hong Kong indices; macro expectations; commodities; important stocks; recent hot events; risk summary and outlook.
  - A stability audit section explaining every MCP job, what it added, what failed, and whether the evidence is enough.
  - Citations or source links preserved from the MCP reports where available.
- Do not print only a summary. The final artifact must be an actual Markdown file at the exact path above.

Research prompt for the primary job:
帮我调研昨天(${YESTERDAY})的股票市场,针对市场大盘(美,日,韩,港),市场对宏观经济的预期,大宗商品,以及各种重要股票,结合最近热点,写一份详尽的日报,每个股票都需要调查透彻,写的日报详尽且严肃.

并且,虽然写昨天的日报,但是需要你调查最近一段时间的信息,并且请多次调用调查工具,直到获取所有可用证据,鼓励追问调查工具.

At the very end, print a compact machine-auditable summary with:
- final_markdown_path
- model/profile
- all job_ids and terminal statuses
- total elapsed seconds
- whether exactly three deeper follow-up rounds were completed
- whether the final markdown exists
PROMPT

echo "project_root=$PROJECT_ROOT"
echo "run_id=$RUN_ID"
echo "model=$MODEL"
echo "opencode_config=$OPENCODE_CONFIG_DIR/opencode.jsonc"
echo "run_log=$RUN_LOG"
echo "final_report=$FINAL_REPORT"
echo "min_runtime_seconds=$MIN_RUNTIME_SECONDS"

if [[ "$DRY_RUN" == "1" ]]; then
  "$UV_BIN" run python -m json.tool "$OPENCODE_CONFIG_DIR/opencode.jsonc" >/dev/null
  echo "DRY_RUN=1: config and prompt generated; opencode was not started."
  echo "prompt=$PROMPT_FILE"
  exit 0
fi

START_EPOCH="$(date '+%s')"

set +e
XDG_CONFIG_HOME="$RUNTIME_DIR/xdg_config" \
XDG_DATA_HOME="$RUNTIME_DIR/xdg_data" \
DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" \
"$OPENCODE_BIN" run \
  --model "$MODEL" \
  --print-logs \
  "$(cat "$PROMPT_FILE")" 2>&1 | tee "$RUN_LOG"
OPENCODE_STATUS="${PIPESTATUS[0]}"
set -e

END_EPOCH="$(date '+%s')"
ELAPSED_SECONDS="$((END_EPOCH - START_EPOCH))"

echo "elapsed_seconds=$ELAPSED_SECONDS"
echo "opencode_status=$OPENCODE_STATUS"

if [[ "$OPENCODE_STATUS" -ne 0 ]]; then
  echo "ERROR: opencode exited with status $OPENCODE_STATUS. See $RUN_LOG" >&2
  exit "$OPENCODE_STATUS"
fi

if [[ ! -s "$FINAL_REPORT" ]]; then
  echo "ERROR: final markdown report was not created or is empty: $FINAL_REPORT" >&2
  exit 2
fi

if [[ "$ELAPSED_SECONDS" -lt "$MIN_RUNTIME_SECONDS" ]]; then
  echo "ERROR: stability run finished too quickly (${ELAPSED_SECONDS}s < ${MIN_RUNTIME_SECONDS}s)." >&2
  echo "This usually means opencode did not perform the required three deeper follow-up rounds." >&2
  exit 3
fi

if ! grep -Eiq 'three(_deeper)?_followup_rounds_launched:[[:space:]]*true|exactly three deeper follow-up rounds were completed:[[:space:]]*true' "$FINAL_REPORT"; then
  echo "ERROR: final markdown does not prove that three deeper follow-up rounds were launched." >&2
  exit 4
fi

if grep -Eiq 'three(_deeper)?_followup_rounds_all_terminal:[[:space:]]*false|three(_deeper)?_followup_rounds_completed:[[:space:]]*false|exactly three deeper follow-up rounds were completed:[[:space:]]*false' "$FINAL_REPORT"; then
  echo "ERROR: final markdown says the three deeper follow-up rounds did not all reach terminal status." >&2
  exit 5
fi

echo "OK: stability report generated at $FINAL_REPORT"
