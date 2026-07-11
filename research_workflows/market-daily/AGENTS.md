# Market daily workflow

This workflow produces a serious Chinese daily report for a frozen trading date. The canonical input contains the full request, `target_date`, and IANA `timezone`.

## Required MCP protocol

1. Call `profile_info` and verify the expected research profile.
2. Call `research_report_start` exactly once with the complete input query, target date, and timezone.
3. Poll only with `research_reports_status(job_ids=[job_id], wait_seconds=20)` until a terminal state.
4. On completion call `research_report_result(job_id, include_report=false)`. Never read old output directories or invent a report when the job failed.
5. Ask `market-auditor` to audit the returned metrics and coverage before completing.

The GPT Researcher service owns the report-internal decomposition, three-way Codex/Tavily concurrency, bounded gap round, writing, and quality gate. Do not create client-side follow-up reports or duplicate the same investigation through multiple jobs.

## Coverage expectations

- Cover the major US, Japan, Korea, and Hong Kong indices and current macro expectations.
- Cover WTI, Brent, gold, and copper with date, unit, contract basis, price, and change.
- Select important liquid stocks and event-driven movers across all four markets; include identifiers, prices, catalysts, background, risks, and direct sources.
- Prefer exchange, central-bank, regulator, filing, and company IR evidence, with independent corroboration for major market figures.
- Preserve source conflicts and fail closed when the server quality gate fails.

Load the `market-daily-method` skill. Follow `instructions/output-contract.md`; the result marker must be the final line.
