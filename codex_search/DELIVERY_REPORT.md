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

- Exactly three initial structured work items per report, each making one Codex call; the three initial calls run concurrently.
- After initial evidence merging, at most one follow-up round can add up to three concurrent Codex-backed gap queries. This makes the per-report lifetime bound six calls while retaining a simultaneous per-report ceiling of three.
- At most three isolated report workers run together, and the cross-process slot pool caps the machine at nine simultaneous Codex processes.
- Four ordinary retrievers and five scraper workers inside each report worker.
- A 2700-second job budget, a queue limit of nine, and 72-hour terminal-job retention.

Strict market-daily work also uses keyless, allowlisted Yahoo Chart and HTML-history checks inside the same four ordinary-retriever slots. They produce dated, source-addressable evidence and fail softly per source. Deterministic index, commodity, and stock ledgers are given to the writer only for complete target-date rows: indices require two retrieved URLs; WTI, Brent, gold, and copper freeze the target-date Yahoo continuous-futures value, unit, contract basis, and a distinct corroborating URL; stocks require four rows per market with the required 2+2 selection mix. Draft citations are restricted to retrieved evidence and duplicate full-report stream restarts are repaired with an audit entry before judging. This grounding does not weaken the final fail-closed gate or increase Codex concurrency.

The local MCP entry point is launched from the checkout with `uv run --directory ... gpt-researcher`. Long clients use `research_report_start`, batch long-poll with `research_reports_status`, fetch terminal data with `research_report_result`, and can terminate the worker tree with `research_report_cancel`.

`scripts/opencode_stability_market_report.sh` provides two acceptance paths. `single` submits one report. `stress` starts one persistent `opencode serve` and launches three simultaneous `opencode run --attach` sessions with the same market-daily question. Both modes use a fresh XDG/job directory, JSONL logs, an atomic run manifest, a hard timeout, current-run-only artifact validation, and a residual-process check. Stress acceptance also requires three overlapping durable workers, three overlapping initial Codex calls per report, a global Codex peak no greater than nine, complete deterministic market coverage, and 14 common index/commodity values that can be parsed and compared across all three final reports without numeric or unit conflicts. Validate the complete setup without external calls using:

The repeatable operator workflow, including quick commands, the MCP call protocol, artifact inspection, read-only revalidation, and failure triage, is documented in [`docs/OPENCODE_MCP_WORKFLOW.md`](../docs/OPENCODE_MCP_WORKFLOW.md). Run `scripts/opencode_stability_market_report.sh --help` for the short command reference.

```sh
DRY_RUN=1 scripts/opencode_stability_market_report.sh single
DRY_RUN=1 scripts/opencode_stability_market_report.sh stress
```

The Python harness also supports read-only revalidation of an immutable prior run. It hashes the source manifest and all reports, preserves structured raw-evidence conflicts for audit, and recomputes acceptance from the final 14-value table comparison without rewriting the source artifacts.
