# Codex Search Helper

This directory contains a bounded helper for running Codex CLI as a non-interactive web research assistant from this repository.

## Local CLI Findings

Local help commands were used only:

- `codex --help`
- `codex exec --help`
- `codex review --help`
- `codex doctor --help`
- `codex --search exec --help`
- `codex exec --search --help`
- `codex --version`

Observed version: `codex-cli 0.142.5`.

The installed CLI has a non-interactive mode:

```sh
codex exec [OPTIONS] [PROMPT]
```

Useful options for this helper:

- `--json` emits JSONL events.
- `--output-last-message <FILE>` writes the final agent response to a file.
- `--ephemeral` avoids persisting session files.
- `--cd <DIR>` sets the agent workspace.
- `--model <MODEL>` selects the model.
- `--ask-for-approval never` is a top-level option in this installed version and must appear before `exec`.
- `--ignore-user-config` is used by default by this helper to avoid loading unrelated personal plugins/skills during search. Auth still uses `CODEX_HOME`.

The live web search flag exists as a top-level CLI option:

```sh
codex --search exec ...
```

It must appear before `exec` in this installed version. `codex exec --search ...` fails with an unexpected argument error.

## Usage

From the repository root:

```sh
./codex_search/codex_search.py "What changed in the latest GPT Researcher release?"
```

Read a query from stdin:

```sh
printf '%s\n' "Find current docs for GPT Researcher MCP usage" | ./codex_search/codex_search.py -
```

Choose a model via env:

```sh
CODEX_SEARCH_MODEL=gpt-5.5 CODEX_SEARCH_REASONING_EFFORT=medium CODEX_SEARCH_SERVICE_TIER=fast ./codex_search/codex_search.py "Compare Tavily and SerpAPI support in GPT Researcher"
```

Write the final answer to a file:

```sh
./codex_search/codex_search.py --output /tmp/codex-answer.md "Search question"
```

Optional configuration knobs:

- `CODEX_SEARCH_MODEL`, or `CODEX_MODEL`: model passed to `codex exec --model`.
- `CODEX_SEARCH_REASONING_EFFORT`, or `CODEX_REASONING_EFFORT`: passed as `-c model_reasoning_effort="..."`.
- `CODEX_SEARCH_SERVICE_TIER`, or `CODEX_SERVICE_TIER`: passed as `-c service_tier="..."`. When set to `fast`, the helper also passes `-c features.fast_mode=true`, matching the current Codex Fast mode documentation.
- `CODEX_SEARCH_MODEL_PROVIDER`: defaults to `chatgpt-http`.
- `CODEX_SEARCH_PROVIDER_BASE_URL`: defaults to `https://chatgpt.com/backend-api/codex`.
- `CODEX_SEARCH_SUPPORTS_WEBSOCKETS`: defaults to `false`, forcing HTTPS Responses transport instead of WebSocket.
- `CODEX_SEARCH_WORKDIR`: workspace passed to `codex exec --cd`; defaults to this helper directory so `AGENTS.md` is loaded by the launched Codex.
- `CODEX_SEARCH_CODEX_BIN`: Codex executable; defaults to `codex`.
- `CODEX_SEARCH_CODEX_HOME`: optional `CODEX_HOME` override.
- `CODEX_SEARCH_USE_USER_CONFIG`: set to `true` to allow personal Codex config/plugins.
- `CODEX_SEARCH_MODE`: `search` or `plan-exec`.
- `CODEX_SEARCH_TIMEOUT`: per-invocation timeout in seconds; defaults to `900` unless `.env` overrides it.
- `CODEX_SEARCH_RETRIEVER_TIMEOUT`: timeout used when the GPT Researcher `CodexSearch` retriever calls this helper.
- `CODEX_SEARCH_MAX_RESULTS`: maximum source-addressable Codex results retained per call; the market profile uses `12` so multi-entity evidence is not silently truncated to the generic five-result limit.
- `CODEX_SEARCH_RETRIEVER_CONCURRENCY`: per-report Codex ceiling; the concurrent report profile sets it to `3`.
- `CODEX_SEARCH_GLOBAL_CONCURRENCY`: machine-wide Codex ceiling shared through cross-process slots; the tested profile sets it to `9`.
- `CODEX_SEARCH_GLOBAL_SLOT_DIR`: shared writable directory containing the global slot locks.

## GPT Researcher Integration

The repository registers a `codex` retriever alongside the built-in retrievers. The tested long-report configuration is `search + medium + fast` with `CODEX_SEARCH_MAX_RESULTS=12`. With:

```sh
RETRIEVER=tavily,codex
CODEX_SEARCH_MODE=search
CODEX_SEARCH_TIMEOUT=300
CODEX_SEARCH_RETRIEVER_TIMEOUT=300
CODEX_SEARCH_MAX_RESULTS=12
CODEX_SEARCH_RETRIEVER_RETRIES=1
CODEX_SEARCH_RETRIEVER_RETRY_DELAY=2
CODEX_SEARCH_RETRIEVER_CONCURRENCY=3
CODEX_SEARCH_GLOBAL_CONCURRENCY=9
CODEX_SEARCH_MODEL=gpt-5.5
CODEX_SEARCH_REASONING_EFFORT=medium
CODEX_SEARCH_SERVICE_TIER=fast
```

GPT Researcher preserves up to three structured work items from its planner and executes them concurrently. If planning returns no usable item, it creates three domain-neutral fallback lanes. When Codex is configured, each work item may use it alongside Tavily as a peer search tool:

1. Planning uses a lightweight retriever and does not call Codex serially in advance.
2. Each work-item query is sent to every configured non-MCP retriever.
3. Results are normalized into evidence, merged, and deduplicated by real HTTP(S) URL and checksum.
4. Tavily URLs continue through the normal scraping pipeline.
5. Codex returns structured claims and sources instead of one synthetic local document.
6. A coverage/conflict check may launch one follow-up round of at most three concurrent Codex-backed gap queries.
7. The final report writer receives one combined evidence pool.

This yields at most three initial Codex calls and at most six Codex calls over the lifetime of one report. The per-report semaphore remains `3`, so initial calls or follow-up calls can overlap three at a time, but overlap is telemetry rather than a success condition. `CODEX_SEARCH_MAX_RESULTS=12` retains up to twelve source-addressable results from each call. This is intentionally different from using Codex as a separate report writer: Codex is a search retriever here, while GPT Researcher still owns planning, compression, and final report writing.

Run a report with the current `.env` profile:

```sh
.venv/bin/python cli.py "调查上周的美股市场,调查不同板块的表现,并说明详细逻辑" --report_type research_report --tone objective --report_source web --no-pdf --no-docx
```

Performance note: `search` mode is the default stability profile. `plan-exec` first asks Codex to plan and then runs a search-enabled pass, so every generated GPT Researcher sub-query can become two Codex CLI invocations. Use it only for targeted one-off searches where the extra latency is acceptable.

OpenCode can orchestrate this MCP alongside other tools without an additional
runner. See the native example and usage guide in
[`docs/OPENCODE_MCP_WORKFLOW.md`](../docs/OPENCODE_MCP_WORKFLOW.md).

## MCP Profile

This checkout includes a packaged MCP stdio coordinator that loads `.env` and exposes synchronous and asynchronous report tools. The checked-in `.mcp.json` starts the local checkout directly:

```json
{
  "mcpServers": {
    "gpt-researcher-codex-long": {
      "command": "uv",
      "args": ["run", "--directory", ".", "gpt-researcher"]
    }
  }
}
```

Validate the same entry point by listing tools with any MCP client, or start it directly:

```sh
uv run --directory . gpt-researcher
```

`uv run --directory` executes the checkout directly and avoids `uvx --refresh` cold starts and stale package environments.

The server loads credentials and model settings from `$GPT_RESEARCHER_PROFILE_DIR`, normally the same checkout passed to `--directory`.

Long reports should use `research_report_start`, `research_reports_status`, and `research_report_result`; `research_report_cancel` terminates the isolated worker process tree. `research_report_status` remains available for one job, and synchronous `research_report` is retained for short requests. The coordinator runs at most three report workers and queues at most nine jobs. Each worker may make up to three initial Codex calls and up to three more in one follow-up round, but runs no more than three simultaneously; the global slot pool caps the machine at nine simultaneous Codex processes.

Call the MCP tool with:

```json
{
  "query": "调查上周的美股市场,调查不同板块的表现,并说明详细逻辑",
  "report_type": "research_report",
  "tone": "objective",
  "report_source": "web"
}
```

For repeatable relative-date requests, also pass `target_date` and `timezone` to `research_report_start`. Status responses stay compact; fetch report content only when needed with `research_report_result(job_id, include_report=true)`.

## Mode Comparison

### `search` mode

Default. Runs a single command shaped like:

```sh
codex --search --ask-for-approval never \
  -c 'model_provider="chatgpt-http"' \
  -c 'model_providers.chatgpt-http.base_url="https://chatgpt.com/backend-api/codex"' \
  -c 'model_providers.chatgpt-http.wire_api="responses"' \
  -c 'model_providers.chatgpt-http.requires_openai_auth=true' \
  -c 'model_providers.chatgpt-http.supports_websockets=false' \
  exec --ignore-user-config --ephemeral --json --cd "$WORKDIR" --output-last-message "$TMP" -
```

This is the simplest and lowest-latency path. It lets Codex decide when to call the native web search tool, then captures only the final answer for normal stdout.

The default provider is intentionally a custom ChatGPT-backed provider with `supports_websockets=false`. `codex doctor` reports this as "Responses WebSocket is not enabled for the active provider", so failed WSS probes do not delay each search run.

Best for ordinary current-information lookups, small comparisons, and citations.

### `plan-exec` mode

Runs two non-interactive Codex passes:

1. A no-search planning pass with `codex exec`.
2. A search-enabled answering pass with `codex --search exec`, including the plan as context.

Example:

```sh
./codex_search/codex_search.py --mode plan-exec "Research a messy multi-source question"
```

This costs more time and tokens, but can help when the query is broad, ambiguous, or needs a deliberate source strategy before browsing.

## Auth And Credential Handling

The helper does not hardcode, copy, print, or commit credentials.

By default it inherits the machine's existing Codex login exactly as the Codex CLI normally would. If you need a separate auth/config location, set `CODEX_SEARCH_CODEX_HOME` or pass `--codex-home`.

The wrapper does not inspect Codex auth files. On CLI failures, it redacts values from environment variables whose names look sensitive, such as those containing `TOKEN`, `KEY`, `SECRET`, `PASSWORD`, or `CREDENTIAL`, before printing stderr/stdout details.

## Limitations

- It depends on a local Codex CLI compatible with `codex --search exec`.
- Web search quality and source selection are controlled by Codex and the selected model.
- `--ephemeral` reduces local session persistence, but Codex provider-side behavior follows the installed CLI and account settings.
- Direct wrapper use captures the final Codex message. Retriever integration additionally requests and validates structured claims and sources for evidence normalization. Use `--show-events` only for debugging because it prints raw JSONL event output to stderr.
