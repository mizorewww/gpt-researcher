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
CODEX_SEARCH_MODEL=gpt-5.5 CODEX_SEARCH_REASONING_EFFORT=medium CODEX_SEARCH_SERVICE_TIER=priority ./codex_search/codex_search.py "Compare Tavily and SerpAPI support in GPT Researcher"
```

Write the final answer to a file:

```sh
./codex_search/codex_search.py --output /tmp/codex-answer.md "Search question"
```

Optional configuration knobs:

- `CODEX_SEARCH_MODEL`, or `CODEX_MODEL`: model passed to `codex exec --model`.
- `CODEX_SEARCH_REASONING_EFFORT`, or `CODEX_REASONING_EFFORT`: passed as `-c model_reasoning_effort="..."`.
- `CODEX_SEARCH_SERVICE_TIER`, or `CODEX_SERVICE_TIER`: passed as `-c service_tier="..."`.
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
- `CODEX_SEARCH_RETRIEVER_CONCURRENCY`: maximum concurrent Codex retriever processes. Defaults to `1` in the long-search profile.

## GPT Researcher Integration

The repository registers a `codex` retriever alongside the built-in retrievers. With:

```sh
RETRIEVER=tavily,codex
CODEX_SEARCH_MODE=plan-exec
CODEX_SEARCH_TIMEOUT=300
CODEX_SEARCH_RETRIEVER_TIMEOUT=300
CODEX_SEARCH_RETRIEVER_CONCURRENCY=1
```

GPT Researcher treats Tavily and Codex as peer search tools:

1. The same planning query is sent to every configured non-MCP retriever.
2. Results are merged and deduplicated by URL.
3. Tavily URLs continue through the normal scraping pipeline.
4. Codex results include `raw_content`, so they enter the normal context compression pipeline directly.
5. The final report writer receives one combined context pool.

This is intentionally different from using Codex as a separate report writer. Codex is a search retriever here; GPT Researcher still owns planning, compression, and final report writing.

Run a report with the current `.env` profile:

```sh
.venv/bin/python cli.py "调查上周的美股市场,调查不同板块的表现,并说明详细逻辑" --report_type research_report --tone objective --report_source web --no-pdf --no-docx
```

Performance note: a full `tavily,codex` run with Codex `plan-exec` on every generated sub-query produced a higher-quality report in local testing, but took about 14 minutes. Use this profile when quality matters more than latency.

## MCP Profile

This checkout includes a packaged MCP stdio server that loads `.env` and exposes `profile_info` plus `research_report`. The checked-in `.mcp.json` starts the local checkout through `uvx --no-cache --from .`:

```json
{
  "mcpServers": {
    "gpt-researcher-codex-long": {
      "command": "uvx",
      "args": ["--no-cache", "--from", ".", "gpt-researcher"]
    }
  }
}
```

Validate the same entry point by listing tools with any MCP client, or start it directly:

```sh
uvx --no-cache --from . gpt-researcher
```

A bare `uvx gpt-researcher` installs the published PyPI package, not this checkout. Use `--no-cache --from .` for local development unless the package has been released with this console entry point.

The server loads credentials and model settings from `$GPT_RESEARCHER_PROFILE_DIR/.env`; the checked-in `.mcp.json` sets that profile directory to this checkout so API keys are loaded even if the MCP client does not launch from the repository root.

Call the MCP tool with:

```json
{
  "query": "调查上周的美股市场,调查不同板块的表现,并说明详细逻辑",
  "report_type": "research_report",
  "tone": "objective",
  "report_source": "web"
}
```

The result includes the markdown report, the saved output path, source count, total cost, and the active profile values.

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
- The helper captures the final agent message, not a structured source database. Use `--show-events` only for debugging because it prints raw JSONL event output to stderr.
