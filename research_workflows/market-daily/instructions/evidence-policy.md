# Market evidence policy

- Structured quote data must retain provider, ticker, exchange, currency, interval, requested date range, and returned observation timestamp.
- Yahoo Finance data in this workflow is supplied through a community `yfinance` MCP server. It is useful for research but is not official Yahoo software or trading-grade market data.
- Count only unique HTTP(S) URLs actually used in the written report as `source_count`. Do not count MCP calls, search result pages, pseudo-URLs, or provider labels as URL sources.
- Prefer primary and first-party evidence for policy, filings, earnings, guidance, and corporate actions. Use secondary reporting for context and corroboration.
- Separate sourced facts, calculations, market expectations, and analysis. State the formula and comparison session for calculated changes.
- Do not invent a URL, close, unit, catalyst, or unavailable ticker. Report material conflicts and limitations.
