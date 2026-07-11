---
name: market-daily-method
description: Use for dated multi-market equity reports that combine structured prices, macro expectations, company catalysts, and evidence reconciliation.
---

# Market daily method

1. Freeze relative dates from the canonical input before research.
2. Convert the requested coverage into independent data, macro/context, and company-event evidence lanes. Dispatch independent lanes concurrently and avoid duplicate work unless a material fact needs corroboration.
3. Prefer batch market-data retrieval for comparable price series. Normalize exchange calendar, close timestamp, currency, unit, contract basis, and previous-session comparison before calculating changes.
4. Treat structured quotes as observations from a data provider, not as independent primary-source URLs. Use direct web evidence for catalysts, policy expectations, filings, and company context.
5. Select stock movers only after observing the target-date session. Preserve the required balance between liquid leaders and event-driven or abnormal movers.
6. Reconcile conflicting values explicitly. Perform at most one bounded gap-filling round for missing required coverage; do not restart the whole investigation.
7. Audit the final tables and conclusions against the input, evidence ledger, and source URLs. Fail closed on an unresolved core date, instrument, or evidence gap.
