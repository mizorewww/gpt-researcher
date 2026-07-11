# Codex Search Helper Delivery Report

## What Was Built

Added a small Codex web-search wrapper in `codex_search/`.

Files:

- `codex_search/codex_search.py`
- `codex_search/README.md`
- `codex_search/AGENTS.md`
- `codex_search/DELIVERY_REPORT.md`

## Local Codex Findings

Local CLI version observed by the worker: `codex-cli 0.142.5`.

The installed Codex CLI supports non-interactive execution through:

```sh
codex exec ...
```

Live web search is enabled by a top-level flag:

```sh
codex --search exec ...
```

Important detail: `--search` must appear before `exec` on this install. `codex exec --search ...` fails as an unexpected argument.

Another important detail: `--ask-for-approval never` is also a top-level flag on this install, so the wrapper places it before `exec`.

The current implementation uses a documented custom provider to avoid failed WebSocket probes:

```toml
model_provider = "chatgpt-http"

[model_providers.chatgpt-http]
name = "ChatGPT HTTP"
base_url = "https://chatgpt.com/backend-api/codex"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
```

`codex doctor` verifies this with: `Responses WebSocket is not enabled for the active provider`.

## Script Behavior

Default mode:

```sh
./codex_search/codex_search.py "question"
```

This runs one search-enabled Codex pass and captures the final answer.

Deeper mode:

```sh
./codex_search/codex_search.py --mode plan-exec "question"
```

This first runs a no-search planning pass, then runs a search-enabled answer pass with that plan included as context. It is slower and costs more, but should be better for broad or messy research questions.

## Configuration

The model is configurable without editing code:

```sh
CODEX_SEARCH_MODEL=gpt-5.5 CODEX_SEARCH_REASONING_EFFORT=medium CODEX_SEARCH_SERVICE_TIER=fast ./codex_search/codex_search.py "question"
```

Other supported knobs:

- `CODEX_SEARCH_MODEL`
- `CODEX_MODEL`
- `CODEX_SEARCH_REASONING_EFFORT`
- `CODEX_SEARCH_SERVICE_TIER`
- `CODEX_SEARCH_MODEL_PROVIDER`
- `CODEX_SEARCH_PROVIDER_BASE_URL`
- `CODEX_SEARCH_SUPPORTS_WEBSOCKETS`
- `CODEX_SEARCH_MODE`
- `CODEX_SEARCH_WORKDIR`
- `CODEX_SEARCH_CODEX_BIN`
- `CODEX_SEARCH_CODEX_HOME`
- `CODEX_SEARCH_TIMEOUT`

When `CODEX_SEARCH_SERVICE_TIER=fast`, the helper also passes `features.fast_mode=true` to match current Codex Fast mode configuration.

## Auth Handling

No local login information was copied into the repository.

The script reuses the existing machine Codex login through the normal Codex CLI auth path. This keeps the tool runnable locally while avoiding committed credentials. If a separate Codex home is needed, use `CODEX_SEARCH_CODEX_HOME` or `--codex-home`.

Failure output redacts sensitive-looking environment values before printing command details.

## Verification

Commands run:

```sh
python3 -m py_compile codex_search/codex_search.py
python3 codex_search/codex_search.py --help
codex -c 'model_provider="chatgpt-http"' -c 'model_providers.chatgpt-http.supports_websockets=false' doctor --json
./codex_search/codex_search.py --timeout 60 --output /tmp/codex-wrapper-ok.txt "只输出 OK"
./codex_search/codex_search.py --timeout 90 --output /tmp/codex-wrapper-search.txt "用一句话回答 OpenAI Codex CLI 非交互命令叫什么，并给一个官方来源链接"
```

Smoke results:

```text
HTTPS-only no-search smoke: 3.49s, output OK, stderr contained no websocket/fallback messages.
HTTPS-only search smoke: 24.80s, output cited https://developers.openai.com/codex/noninteractive, stderr contained no websocket/fallback messages.
```

## Notes

- Direct helper use captures the final Codex response. GPT Researcher retriever integration uses a JSON schema to normalize claims and real HTTP(S) sources into evidence items.
- `--show-events` can print raw JSONL events for debugging.
- GPT Researcher integration is implemented as `gpt_researcher.retrievers.codex.CodexSearch`; timeouts and transient failures are recorded per run so Tavily and other retrievers can continue while the report-level evidence gate still fails closed when coverage is insufficient.

## Concurrent MCP delivery

The long-report profile uses `search + medium + fast`, retains up to `12` source-addressable results per Codex call, and has two explicit concurrency layers:

- Up to three initial structured work items per report; available work items run concurrently, while the observed overlap is telemetry rather than a quality gate.
- After initial evidence merging, at most one follow-up round can add up to three concurrent Codex-backed gap queries. This makes the per-report lifetime bound six calls while retaining a simultaneous per-report ceiling of three.
- At most three isolated report workers run together, and the cross-process slot pool caps the machine at nine simultaneous Codex processes.
- Four ordinary retrievers and five scraper workers inside each report worker.
- A 2700-second job budget, a queue limit of nine, and 72-hour terminal-job retention.

The local MCP entry point is launched from the checkout with `uv run --directory ... gpt-researcher`. Long clients use `research_report_start`, batch long-poll with `research_reports_status`, fetch terminal data with `research_report_result`, and can terminate the worker tree with `research_report_cancel`.

OpenCode uses this server as an ordinary MCP tool. The native example in
`opencode/market-research-smoke` places task requirements in `AGENTS.md` and
keeps the coordinator and parallel-research skill generic; Yahoo Finance is a
separate peer MCP selected by OpenCode when structured market data is useful.
See [`docs/OPENCODE_MCP_WORKFLOW.md`](../docs/OPENCODE_MCP_WORKFLOW.md).
