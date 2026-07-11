---
name: market-daily-method
description: Use for daily equity-market reports spanning US, Japan, Korea, Hong Kong, macro expectations, commodities, and event-driven stocks.
---

# Market daily method

- Freeze all relative dates using the input `target_date` and `timezone`.
- Treat index, commodity, and stock-table values as dated facts that require direct sources, units, identifiers, and consistent market-session definitions.
- Distinguish macro expectations from released data and from the analyst's own inference.
- For each market, balance high-liquidity leaders with stocks selected for target-date news or abnormal movement.
- Explain stale markets, holidays, futures contract bases, currency differences, and conflicting closes.
- Use the service's internal three-work-item planner. Do not multiply jobs at the OpenCode layer.
