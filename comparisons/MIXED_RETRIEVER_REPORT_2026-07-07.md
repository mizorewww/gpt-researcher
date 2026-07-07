# Tavily-only vs Tavily+Codex Long Mixed Report

## What Was Fixed

The earlier implementation did not combine retrievers cleanly during planning. `plan_research()` used only `self.researcher.retrievers[0]`, so with `RETRIEVER=tavily,codex`, Tavily alone shaped the initial sub-query plan.

This was changed in `gpt_researcher/skills/researcher.py`:

- Planning now runs the same input query through every configured non-MCP retriever.
- Sub-query search also runs the same sub-query through every configured non-MCP retriever.
- Results are merged and deduplicated by URL.
- Retriever results with `raw_content`, such as Codex, enter the original context compression path directly.
- Retriever results with URLs, such as Tavily, continue through the original scrape/compress path.
- Codex concurrency is capped by `CODEX_SEARCH_RETRIEVER_CONCURRENCY=1` so multiple sub-query fan-out does not launch many long Codex processes at once.

## Test Question

```text
调查上周的美股市场,调查不同板块的表现,并说明详细逻辑
```

## Commands

Tavily-only:

```sh
/usr/bin/time -p -o comparisons/tavily_only_after_merge_time.txt \
  env RETRIEVER=tavily \
  .venv/bin/python cli.py "$QUESTION" \
  --report_type research_report --tone objective --report_source web --no-pdf --no-docx
```

Tavily + Codex long mixed:

```sh
/usr/bin/time -p -o comparisons/mixed_tavily_codex_long_time.txt \
  env RETRIEVER=tavily,codex CODEX_SEARCH_MODE=plan-exec CODEX_SEARCH_TIMEOUT=300 CODEX_SEARCH_RETRIEVER_TIMEOUT=300 \
  .venv/bin/python cli.py "$QUESTION" \
  --report_type research_report --tone objective --report_source web --no-pdf --no-docx
```

## Results

| Mode | Output | Time | Size | Links | Cost |
| --- | --- | ---: | ---: | ---: | ---: |
| Tavily-only | `outputs/上周美股板块表现逻辑.md` | 141.57s | 4,840 chars | 21 | $0.2162 |
| Tavily + Codex long mixed | `outputs/上周美股板块表现调查.md` | 858.48s | 13,219 chars | 48 | $0.232165 |

## Quality Findings

### Tavily-only

Tavily-only is much faster and produces a plausible report, but source quality is weak:

- It cites placeholder-looking URLs such as `https://www.example.com/财闻网` and `https://www.example.com/moomoo`.
- It leans on Chinese secondary/aggregator sources.
- It gives a clean narrative, but the source trail is not reliable enough for financial research.
- It has less detail on rates, official labor data, and source caveats.

### Tavily + Codex Long Mixed

The mixed run is clearly stronger on evidence quality:

- It includes AP for index-level weekly performance.
- It includes BLS for employment data.
- It includes U.S. Treasury yield data.
- It includes Kiplinger, MarketWatch, Barron's, BlackRock, Schwab, Janus Henderson, and J.P. Morgan sources.
- It gives a more complete 11-sector discussion, including technology, communication services, healthcare, industrials, financials, discretionary, staples, energy, utilities, real estate, and materials.
- It is more explicit about estimates and caveats where exact sector weekly returns were not directly available.

The problem is latency: 858.48s is not acceptable as a default interactive path.

## Verdict

The correct architecture is not "Codex replaces Tavily". Tavily is better as the fast breadth search tool. Codex long search is better as a high-quality evidence and sanity-check tool.

Current full mixed mode proves that combining them improves report quality, but running Codex long search for every generated sub-query is too slow.

Recommended default:

- Keep Tavily for every generated sub-query.
- Run Codex long search once on the original user query, or at most on the original query plus one selected high-value sub-query.
- Merge Codex raw content into the same context pool before report writing.
- Keep `CODEX_SEARCH_TIMEOUT=300`, but avoid multiplying that budget by every sub-query.

Recommended experimental/full mode:

- `RETRIEVER=tavily,codex`
- `CODEX_SEARCH_MODE=plan-exec`
- `CODEX_SEARCH_RETRIEVER_TIMEOUT=300`
- `CODEX_SEARCH_RETRIEVER_CONCURRENCY=1`

This gives the best quality, but the observed runtime was about 14.3 minutes.
