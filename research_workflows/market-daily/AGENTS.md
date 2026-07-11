# Market daily task context

Produce a detailed, serious Chinese daily report for the frozen `target_date` and IANA `timezone` supplied in the canonical input. The report must explain what moved, why it mattered, how the market interpreted the macro backdrop, and what remains uncertain.

## Required coverage

- US: S&P 500 (`^GSPC`), Dow (`^DJI`), Nasdaq Composite (`^IXIC`), and Russell 2000 (`^RUT`).
- Japan: Nikkei 225 (`^N225`) and TOPIX (`998405.T`).
- Korea: KOSPI (`^KS11`) and KOSDAQ (`^KQ11`).
- Hong Kong: Hang Seng (`^HSI`) and Hang Seng TECH (`HSTECH`).
- Commodities: WTI (`CL=F`), Brent (`BZ=F`), gold (`GC=F`), and copper (`HG=F`), with price, change, currency/unit, contract basis, and observation date.
- Stocks: at least four distinct stocks per market. In each market include at least two liquid leaders and two names selected for target-date news or unusual movement.
- Macro: distinguish released data, policy communication, market expectations, and the report's own inference.

## Evidence and report requirements

- Every price or percentage must identify its ticker, market session, observation date, currency/unit, and comparison basis.
- Every selected stock needs its close, change, target-date catalyst, recent fundamental context, risk, and evidence.
- Cross-check every required index and commodity value with an independent second source, and explain unresolved differences.
- Use direct HTTP(S) links for qualitative claims. Structured market-data observations without URLs must name their provider and retrieval basis, but must not be counted as independent URL sources.
- Explain holidays, stale quotes, unavailable instruments, conflicting closes, and delayed or unofficial data instead of silently filling gaps.
- Use at least 25 unique direct HTTP(S) sources, prioritizing exchanges, central banks, regulators, filings, company IR, and directly linked reporting.
- End with a concise risk watchlist and a transparent limitations section. Fail closed if the required date or core market coverage cannot be established.
