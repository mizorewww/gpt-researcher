# Codex Search vs Tavily Test Report

## Test Setup

Question:

```text
调查上周的美股市场,调查不同板块的表现,并说明详细逻辑
```

Date of run: 2026-07-07.

Codex configuration came from `.env`: `gpt-5.5`, `model_reasoning_effort=medium`, `service_tier=priority`, custom `chatgpt-http` provider with `supports_websockets=false`.

## Commands Tested

Codex short search:

```sh
/usr/bin/time -p -o comparisons/codex_short_time.txt ./codex_search/codex_search.py --mode search --timeout 120 --output comparisons/codex_short_market.md "$QUESTION"
```

Codex long search:

```sh
/usr/bin/time -p -o comparisons/codex_long_300_time.txt ./codex_search/codex_search.py --mode plan-exec --timeout 300 --output comparisons/codex_long_300_market.md "$QUESTION"
```

Tavily-only GPT Researcher report:

```sh
/usr/bin/time -p -o comparisons/tavily_only_time.txt env RETRIEVER=tavily .venv/bin/python cli.py "$QUESTION" --report_type research_report --tone objective --report_source web --no-pdf --no-docx
```

## Timing And Size

| Mode | Output | Time | Size | Links | Result |
| --- | --- | ---: | ---: | ---: | --- |
| Codex short search | `comparisons/codex_short_market.md` | 84.29s | 2,033 chars | 6 | Success |
| Codex long search, 300s budget | `comparisons/codex_long_300_market.md` | 104.55s | 2,539 chars | 8 | Success |
| Codex long search, 120s budget | no final output | 136.15s | n/a | n/a | Search pass timed out |
| Tavily only, full researcher | `outputs/美股上周板块轮动解析.md` | 155.83s | 9,733 chars | 41 | Success |

## Quality Comparison

### Codex Short Search

The short mode produced a compact answer with the important market facts:

- S&P 500, Dow, Nasdaq, and Russell 2000 weekly moves.
- Semiconductor weakness and AI/chip profit-taking.
- Rotation into Dow/blue-chip/value areas.
- Macro explanation around softer labor data, rate expectations, oil, and sector rotation.

Weaknesses:

- It is more of a concise research note than a full report.
- Sector coverage is selective, not a clean 11-sector table.
- It converted "上周" into exact dates. That is fine for this latest test question, but it failed the earlier relative-time stress condition.

### Codex Long Search

The 300s long mode completed in 104.55s, which is acceptable under the 120s practical target and much better than the previous WSS-delayed behavior.

Compared with short mode, it improved on:

- Source quality: NYSE calendar, AP, BLS, MarketWatch, Kiplinger, State Street, WSJ-style yield source.
- Logic detail: separated index performance, growth/AI, financials/industrials, discretionary, defensives, energy, small caps, labor data, and rates.
- Explicit caveat: it admitted it did not find a public directly citable GICS 11-sector weekly performance table.

Weaknesses:

- Still not a polished final report; it is a strong source-backed briefing.
- It still resolves "上周" into absolute dates.
- It cannot guarantee exhaustive sector performance unless a direct sector table source is found.

### Tavily Only

The Tavily-only GPT Researcher run produced the longest and most report-like artifact:

- Full frontmatter with `sources_count: 20`.
- Formal structure: executive summary, index review, sector attribution, macro/micro/market-structure logic, conclusion, references.
- More prose depth and more links than either Codex run.

But quality was materially weaker on factual reliability:

- It relied heavily on secondary Chinese/aggregator sources such as Eastmoney, TradingKey, and Vocus.
- It reported suspicious index levels and moves, for example S&P 500 at `744.78`, which does not align with normal index level conventions and looks like a bad source/market-symbol mix.
- It said the week ended 2026-07-03 even though the U.S. market was closed that day for Independence Day observed.
- It mixed sources from adjacent periods such as "截至6月26日当周" into a report about "上周", which weakens time consistency.

## Verdict

Best current default for search-result quality and latency: **Codex long search with 300s timeout**.

Reason:

- It finished in 104.55s, inside the user's acceptable 120s practical threshold.
- It used stronger primary or near-primary sources.
- It gave better caveats and did not over-polish weak data into false certainty.
- It was faster and more factually grounded than Tavily-only full researcher.

Best for a polished long-form report: **Tavily-only GPT Researcher**, but only after adding source-quality controls. Without those controls, it can produce a nice-looking report from weaker or time-mismatched sources.

Recommended architecture:

- Use Codex long search as a high-quality research retriever/context source.
- Keep Tavily as breadth search.
- Let GPT Researcher synthesize, but add source-ranking or prompt constraints that prefer primary/market-data sources over low-quality aggregators.
- Preserve Codex timeout/failure fallback so Tavily still runs when Codex hangs.

## One-Line Command

Use the repo `.env` options and run the same investigation through GPT Researcher:

```sh
.venv/bin/python cli.py "调查上周的美股市场,调查不同板块的表现,并说明详细逻辑" --report_type research_report --tone objective --report_source web --no-pdf --no-docx
```

With the current `.env`, this uses `RETRIEVER=tavily,codex`, Codex `gpt-5.5`, medium reasoning, priority tier, and HTTPS-only Codex transport.
