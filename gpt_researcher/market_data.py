"""Keyless, source-addressable market history helpers.

This module intentionally contains no GPT Researcher orchestration. It turns
explicit generated market-daily lanes and regional gaps into bounded Yahoo
Chart and allowlisted HTML sources, then parses exact target-date/previous-
session closes without inferred dates.
"""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as datetime_time, timedelta
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bs4 import BeautifulSoup

from .evidence import EvidenceItem


YAHOO_CHART_BASES = (
    "https://query2.finance.yahoo.com/v8/finance/chart",
    "https://query1.finance.yahoo.com/v8/finance/chart",
)
YAHOO_CHART_TIMEOUT_SECONDS = 18.0
YAHOO_CHART_USER_AGENT = "Mozilla/5.0 (compatible; GPT-Researcher/1.0; +https://github.com/assafelovic/gpt-researcher)"

INDEX_HTML_TIMEOUT_SECONDS = 18.0
INDEX_HTML_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36 "
    "GPT-Researcher/1.0"
)
INVESTING_TOPIX_HISTORY_URL = (
    "https://www.investing.com/indices/topix-historical-data"
)
INVESTING_SP500_HISTORY_URL = (
    "https://www.investing.com/indices/us-spx-500-historical-data"
)
INVESTING_DOW_HISTORY_URL = (
    "https://www.investing.com/indices/us-30-historical-data"
)
INVESTING_NASDAQ_COMPOSITE_HISTORY_URL = (
    "https://www.investing.com/indices/nasdaq-composite-historical-data"
)
INVESTING_RUSSELL_2000_HISTORY_URL = (
    "https://www.investing.com/indices/smallcap-2000-historical-data"
)
INVESTING_KOSPI_HISTORY_URL = (
    "https://www.investing.com/indices/kospi-historical-data"
)
INVESTING_KOSDAQ_HISTORY_URL = (
    "https://www.investing.com/indices/kosdaq-historical-data"
)
INVESTING_HSI_HISTORY_URL = (
    "https://www.investing.com/indices/hang-sen-40-historical-data"
)
YAHOO_JAPAN_TOPIX_HISTORY_URL = (
    "https://finance.yahoo.co.jp/quote/998405.T/history"
)
INVESTING_HSTECH_HISTORY_URL = (
    "https://www.investing.com/indices/hang-seng-tech-historical-data"
)
HANG_SENG_HSTECH_QUOTE_URL = (
    "https://cbbc.hangseng.com/en-hk/market/stock/code/hstech"
)


@dataclass(frozen=True, slots=True)
class YahooInstrument:
    region: str
    name: str
    symbol: str
    kind: str


@dataclass(frozen=True, slots=True)
class YahooChartQuote:
    instrument: YahooInstrument
    source_url: str
    exchange_timezone: str
    exchange: str
    currency: str
    target_date: date
    close: float
    previous_date: date
    previous_close: float
    percent_change: float

    def flat_summary(self) -> str:
        return (
            f"Market: {self.instrument.region} | Company/Index: {self.instrument.name} | "
            f"Ticker: {self.instrument.symbol} | Exchange: {self.exchange} | "
            f"Date: {self.target_date.isoformat()} | Close: {self.close:g} {self.currency} | "
            f"Change: {self.percent_change:+.6f}% | Previous close: "
            f"{self.previous_close:g} on {self.previous_date.isoformat()} | "
            f"Exchange timezone: {self.exchange_timezone} | Source: {self.source_url}"
        )

    def to_search_result(self) -> dict[str, Any]:
        title = f"Yahoo Finance chart: {self.instrument.name} ({self.instrument.symbol})"
        summary = self.flat_summary()
        common = {
            "source_url": self.source_url,
            "source_title": title,
            "retriever": "YahooChart",
            "as_of_date": self.target_date.isoformat(),
        }
        evidence = [
            EvidenceItem(
                claim=f"{self.instrument.symbol} target-date close",
                value=self.close,
                unit=self.currency,
                summary=(
                    f"{summary} Claim detail: exact chart close in "
                    f"{self.exchange_timezone}; previous valid session was "
                    f"{self.previous_date.isoformat()} at {self.previous_close:g}."
                ),
                **common,
            ),
            EvidenceItem(
                claim=f"{self.instrument.symbol} target-date daily percentage change",
                value=self.percent_change,
                unit="percent",
                summary=(
                    f"{summary} Claim detail: computed from exact closes: "
                    f"({self.close:g} / {self.previous_close:g} - 1) * 100."
                ),
                **common,
            ),
        ]
        return {
            "title": title,
            "href": self.source_url,
            "body": summary,
            "raw_content": summary,
            "retriever": "YahooChart",
            "evidence": [item.to_dict() for item in evidence],
        }


@dataclass(frozen=True, slots=True)
class IndexHtmlSupplement:
    """One narrowly scoped HTML source used to fill an index evidence gap."""

    region: str
    name: str
    symbol: str
    provider: str
    source_url: str
    parser: str


@dataclass(frozen=True, slots=True)
class IndexHtmlQuote:
    """An exact target-date index observation from a human-readable page."""

    supplement: IndexHtmlSupplement
    target_date: date
    close: float
    percent_change: float | None = None
    previous_date: date | None = None
    previous_close: float | None = None
    observed_at: datetime | None = None

    def flat_summary(self) -> str:
        parts = [
            f"Market: {self.supplement.region}",
            f"Company/Index: {self.supplement.name}",
            f"Ticker: {self.supplement.symbol}",
            "Exchange: index",
            f"Date: {self.target_date.isoformat()}",
            f"Close: {self.close:g} index points",
        ]
        if self.percent_change is not None:
            parts.append(f"Change: {self.percent_change:+.6f}%")
        if self.previous_close is not None and self.previous_date is not None:
            parts.append(
                f"Previous close: {self.previous_close:g} on "
                f"{self.previous_date.isoformat()}"
            )
        if self.observed_at is not None:
            parts.append(f"Page observed at: {self.observed_at.isoformat()}")
        parts.extend(
            [
                f"Provider: {self.supplement.provider}",
                f"Source: {self.supplement.source_url}",
            ]
        )
        return " | ".join(parts)

    def to_search_result(self) -> dict[str, Any]:
        title = (
            f"{self.supplement.provider}: {self.supplement.name} "
            f"({self.supplement.symbol}) historical close"
        )
        summary = self.flat_summary()
        common = {
            "source_url": self.supplement.source_url,
            "source_title": title,
            "retriever": "IndexHtml",
            "as_of_date": self.target_date.isoformat(),
        }
        evidence = [
            EvidenceItem(
                claim=(
                    f"{self.supplement.symbol} target-date close from "
                    f"{self.supplement.provider}"
                ),
                value=self.close,
                unit="index points",
                summary=summary,
                **common,
            )
        ]
        if self.percent_change is not None:
            evidence.append(
                EvidenceItem(
                    claim=(
                        f"{self.supplement.symbol} target-date daily percentage "
                        f"change from {self.supplement.provider}"
                    ),
                    value=self.percent_change,
                    unit="percent",
                    summary=summary,
                    **common,
                )
            )
        return {
            "title": title,
            "href": self.supplement.source_url,
            "body": summary,
            "raw_content": summary,
            "retriever": "IndexHtml",
            "evidence": [item.to_dict() for item in evidence],
        }


_INITIAL_US_INDEX_INSTRUMENTS = (
    YahooInstrument("U.S.", "S&P 500", "^GSPC", "index"),
    YahooInstrument("U.S.", "Dow Jones Industrial Average", "^DJI", "index"),
    YahooInstrument("U.S.", "Nasdaq Composite", "^IXIC", "index"),
    YahooInstrument("U.S.", "Russell 2000", "^RUT", "index"),
)

_INITIAL_US_EQUITY_INSTRUMENTS = (
    YahooInstrument("U.S.", "NVIDIA", "NVDA", "stock"),
    YahooInstrument("U.S.", "Apple", "AAPL", "stock"),
    YahooInstrument("U.S.", "Microsoft", "MSFT", "stock"),
    YahooInstrument("U.S.", "Micron Technology", "MU", "stock"),
    YahooInstrument("U.S.", "PepsiCo", "PEP", "stock"),
    YahooInstrument("U.S.", "MARA Holdings", "MARA", "stock"),
    YahooInstrument("U.S.", "Dell Technologies", "DELL", "stock"),
    YahooInstrument("U.S.", "Sandisk", "SNDK", "stock"),
)

_INITIAL_COMMODITY_INSTRUMENTS = (
    YahooInstrument("Global", "WTI crude oil", "CL=F", "commodity"),
    YahooInstrument("Global", "Brent crude oil", "BZ=F", "commodity"),
    YahooInstrument("Global", "Gold", "GC=F", "commodity"),
    YahooInstrument("Global", "Copper", "HG=F", "commodity"),
)

_REGIONAL_INSTRUMENTS: dict[str, tuple[YahooInstrument, ...]] = {
    "Japan": (
        YahooInstrument("Japan", "Toyota Motor", "7203.T", "stock"),
        YahooInstrument("Japan", "SoftBank Group", "9984.T", "stock"),
        YahooInstrument("Japan", "Mitsubishi UFJ Financial", "8306.T", "stock"),
        YahooInstrument("Japan", "Tokyo Electron", "8035.T", "stock"),
        YahooInstrument("Japan", "Sony Group", "6758.T", "stock"),
        YahooInstrument("Japan", "Kioxia Holdings", "285A.T", "stock"),
        YahooInstrument("Japan", "Nikkei 225", "^N225", "index"),
        # Yahoo may not publish this symbol in every region; fetching is fail-soft.
        YahooInstrument("Japan", "TOPIX", "^TOPX", "index"),
    ),
    "Korea": (
        YahooInstrument("Korea", "Samsung Electronics", "005930.KS", "stock"),
        YahooInstrument("Korea", "SK Hynix", "000660.KS", "stock"),
        YahooInstrument("Korea", "Hyundai Motor", "005380.KS", "stock"),
        YahooInstrument("Korea", "Naver", "035420.KS", "stock"),
        YahooInstrument("Korea", "Kakao", "035720.KS", "stock"),
        YahooInstrument("Korea", "Krafton", "259960.KS", "stock"),
        YahooInstrument("Korea", "KOSPI", "^KS11", "index"),
        YahooInstrument("Korea", "KOSDAQ", "^KQ11", "index"),
    ),
    "Hong Kong": (
        YahooInstrument("Hong Kong", "Tencent", "0700.HK", "stock"),
        YahooInstrument("Hong Kong", "Alibaba", "9988.HK", "stock"),
        YahooInstrument("Hong Kong", "Meituan", "3690.HK", "stock"),
        YahooInstrument("Hong Kong", "BYD", "1211.HK", "stock"),
        YahooInstrument("Hong Kong", "Xiaomi", "1810.HK", "stock"),
        YahooInstrument("Hong Kong", "Hong Kong Exchanges", "0388.HK", "stock"),
        YahooInstrument("Hong Kong", "CNOOC", "0883.HK", "stock"),
        YahooInstrument("Hong Kong", "Hang Seng Index", "^HSI", "index"),
        YahooInstrument("Hong Kong", "Hang Seng TECH Index", "HSTECH.HK", "index"),
    ),
}

_INITIAL_INDEX_HTML_SUPPLEMENTS = (
    IndexHtmlSupplement(
        region="U.S.",
        name="S&P 500",
        symbol="^GSPC",
        provider="Investing.com",
        source_url=INVESTING_SP500_HISTORY_URL,
        parser="investing_history",
    ),
    IndexHtmlSupplement(
        region="U.S.",
        name="Dow Jones Industrial Average",
        symbol="^DJI",
        provider="Investing.com",
        source_url=INVESTING_DOW_HISTORY_URL,
        parser="investing_history",
    ),
    IndexHtmlSupplement(
        region="U.S.",
        name="Nasdaq Composite",
        symbol="^IXIC",
        provider="Investing.com",
        source_url=INVESTING_NASDAQ_COMPOSITE_HISTORY_URL,
        parser="investing_history",
    ),
    IndexHtmlSupplement(
        region="U.S.",
        name="Russell 2000",
        symbol="^RUT",
        provider="Investing.com",
        source_url=INVESTING_RUSSELL_2000_HISTORY_URL,
        parser="investing_history",
    ),
)

_INDEX_HTML_SUPPLEMENTS: dict[str, tuple[IndexHtmlSupplement, ...]] = {
    "Japan": (
        IndexHtmlSupplement(
            region="Japan",
            name="TOPIX",
            symbol="998405.T",
            provider="Investing.com",
            source_url=INVESTING_TOPIX_HISTORY_URL,
            parser="investing_history",
        ),
        IndexHtmlSupplement(
            region="Japan",
            name="TOPIX",
            symbol="998405.T",
            provider="Yahoo! Finance Japan",
            source_url=YAHOO_JAPAN_TOPIX_HISTORY_URL,
            parser="yahoo_japan_history",
        ),
    ),
    "Korea": (
        IndexHtmlSupplement(
            region="Korea",
            name="KOSPI",
            symbol="^KS11",
            provider="Investing.com",
            source_url=INVESTING_KOSPI_HISTORY_URL,
            parser="investing_history",
        ),
        IndexHtmlSupplement(
            region="Korea",
            name="KOSDAQ",
            symbol="^KQ11",
            provider="Investing.com",
            source_url=INVESTING_KOSDAQ_HISTORY_URL,
            parser="investing_history",
        ),
    ),
    "Hong Kong": (
        IndexHtmlSupplement(
            region="Hong Kong",
            name="Hang Seng Index",
            symbol="^HSI",
            provider="Investing.com",
            source_url=INVESTING_HSI_HISTORY_URL,
            parser="investing_history",
        ),
        IndexHtmlSupplement(
            region="Hong Kong",
            name="Hang Seng TECH Index",
            symbol="HSTECH",
            provider="Investing.com",
            source_url=INVESTING_HSTECH_HISTORY_URL,
            parser="investing_history",
        ),
        IndexHtmlSupplement(
            region="Hong Kong",
            name="Hang Seng TECH Index",
            symbol="HSTECH",
            provider="Hang Seng Bank",
            source_url=HANG_SENG_HSTECH_QUOTE_URL,
            parser="hang_seng_previous_close",
        ),
    ),
}


_INITIAL_MARKET_LANE_PATTERN = re.compile(
    r"\bresearch lane 1\s*[—-]\s*market indices and macro expectations\b",
    flags=re.IGNORECASE,
)
_INITIAL_EQUITIES_LANE_PATTERN = re.compile(
    r"\bresearch lane 3\s*[—-]\s*important equities in depth\b",
    flags=re.IGNORECASE,
)
_INITIAL_COMMODITIES_LANE_PATTERN = re.compile(
    r"\bresearch lane 2\s*[—-]\s*commodities and cross-asset hot topics\b",
    flags=re.IGNORECASE,
)


def yahoo_instruments_for_initial_market_lane(
    query: str,
) -> tuple[YahooInstrument, ...]:
    """Return the four U.S. indices only for the generated initial lane 1."""

    if _INITIAL_MARKET_LANE_PATTERN.search(str(query)) is None:
        return ()
    return _INITIAL_US_INDEX_INSTRUMENTS


def yahoo_instruments_for_initial_equities_lane(
    query: str,
) -> tuple[YahooInstrument, ...]:
    """Return the U.S. candidate pool only for the generated initial lane 3."""

    if _INITIAL_EQUITIES_LANE_PATTERN.search(str(query)) is None:
        return ()
    return _INITIAL_US_EQUITY_INSTRUMENTS


def yahoo_instruments_for_initial_commodities_lane(
    query: str,
) -> tuple[YahooInstrument, ...]:
    """Return four deterministic futures series only for generated lane 2."""

    if _INITIAL_COMMODITIES_LANE_PATTERN.search(str(query)) is None:
        return ()
    return _INITIAL_COMMODITY_INSTRUMENTS


def yahoo_instruments_for_regional_gap(query: str) -> tuple[YahooInstrument, ...]:
    """Return instruments only for JP/KR/HK market-daily regional gaps."""

    lowered = str(query).casefold()
    if "market-daily regional evidence gap" not in lowered:
        return ()
    if re.search(r"[—-]\s*japan\b", lowered):
        return _REGIONAL_INSTRUMENTS["Japan"]
    if re.search(r"[—-]\s*korea\b", lowered):
        return _REGIONAL_INSTRUMENTS["Korea"]
    if re.search(r"[—-]\s*hong kong\b", lowered):
        return _REGIONAL_INSTRUMENTS["Hong Kong"]
    return ()


def index_html_supplements_for_initial_market_lane(
    query: str,
) -> tuple[IndexHtmlSupplement, ...]:
    """Return Investing U.S. index pages only for the generated initial lane 1."""

    if _INITIAL_MARKET_LANE_PATTERN.search(str(query)) is None:
        return ()
    return _INITIAL_INDEX_HTML_SUPPLEMENTS


def index_html_supplements_for_regional_gap(
    query: str,
) -> tuple[IndexHtmlSupplement, ...]:
    """Select only explicit JP/KR/HK index supplements for a gap query."""

    lowered = str(query).casefold()
    if "market-daily regional evidence gap" not in lowered:
        return ()
    if re.search(r"[—-]\s*japan\b", lowered):
        return _INDEX_HTML_SUPPLEMENTS["Japan"]
    if re.search(r"[—-]\s*korea\b", lowered):
        return _INDEX_HTML_SUPPLEMENTS["Korea"]
    if re.search(r"[—-]\s*hong kong\b", lowered):
        return _INDEX_HTML_SUPPLEMENTS["Hong Kong"]
    return ()


def target_date_for_regional_gap(query: str, fallback: str | None = None) -> date | None:
    match = re.search(r"\b20\d{2}-\d{2}-\d{2}\b", str(query))
    value = match.group(0) if match else fallback
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def yahoo_chart_urls(symbol: str, target_date: date) -> tuple[str, str]:
    start = datetime.combine(target_date - timedelta(days=10), datetime_time.min, tzinfo=UTC)
    end = datetime.combine(target_date + timedelta(days=2), datetime_time.min, tzinfo=UTC)
    params = urlencode(
        {
            "period1": int(start.timestamp()),
            "period2": int(end.timestamp()),
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "true",
        }
    )
    encoded_symbol = quote(symbol, safe="")
    return tuple(f"{base}/{encoded_symbol}?{params}" for base in YAHOO_CHART_BASES)  # type: ignore[return-value]


def parse_yahoo_chart(
    payload: dict[str, Any],
    *,
    instrument: YahooInstrument,
    target_date: date,
    source_url: str,
) -> YahooChartQuote:
    chart = payload.get("chart")
    if not isinstance(chart, dict) or chart.get("error"):
        raise ValueError("Yahoo chart returned an error")
    results = chart.get("result")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        raise ValueError("Yahoo chart returned no result")
    result = results[0]
    meta = result.get("meta")
    if not isinstance(meta, dict):
        raise ValueError("Yahoo chart metadata is missing")
    timezone_name = meta.get("exchangeTimezoneName")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValueError("Yahoo chart exchange timezone is missing")
    try:
        exchange_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Yahoo chart exchange timezone is invalid") from exc

    timestamps = result.get("timestamp")
    indicators = result.get("indicators")
    if not isinstance(timestamps, list) or not isinstance(indicators, dict):
        raise ValueError("Yahoo chart observations are missing")
    quotes = indicators.get("quote")
    if not isinstance(quotes, list) or not quotes or not isinstance(quotes[0], dict):
        raise ValueError("Yahoo chart close series is missing")
    closes = quotes[0].get("close")
    if not isinstance(closes, list):
        raise ValueError("Yahoo chart close series is missing")

    observations: dict[date, float] = {}
    for raw_timestamp, raw_close in zip(timestamps, closes):
        if not isinstance(raw_timestamp, (int, float)) or isinstance(raw_timestamp, bool):
            continue
        if not isinstance(raw_close, (int, float)) or isinstance(raw_close, bool):
            continue
        close = float(raw_close)
        if not math.isfinite(close):
            continue
        observation_date = datetime.fromtimestamp(
            raw_timestamp,
            tz=exchange_timezone,
        ).date()
        observations[observation_date] = close

    if target_date not in observations:
        raise ValueError("Yahoo chart has no exact target-date close")
    previous_dates = [value for value in observations if value < target_date]
    if not previous_dates:
        raise ValueError("Yahoo chart has no previous valid trading session")
    previous_date = max(previous_dates)
    raw_close = observations[target_date]
    raw_previous_close = observations[previous_date]
    if raw_previous_close == 0:
        raise ValueError("Yahoo chart previous close is zero")
    price_hint = meta.get("priceHint", 4)
    if not isinstance(price_hint, int) or isinstance(price_hint, bool):
        price_hint = 4
    price_hint = min(8, max(0, price_hint))
    close = round(raw_close, price_hint)
    previous_close = round(raw_previous_close, price_hint)
    percent_change = round((close / previous_close - 1) * 100, 6)

    return YahooChartQuote(
        instrument=instrument,
        source_url=source_url,
        exchange_timezone=timezone_name,
        exchange=str(meta.get("fullExchangeName") or meta.get("exchangeName") or "Yahoo"),
        currency=str(meta.get("currency") or ""),
        target_date=target_date,
        close=close,
        previous_date=previous_date,
        previous_close=previous_close,
        percent_change=percent_change,
    )


def fetch_yahoo_chart(
    instrument: YahooInstrument,
    target_date: date,
    *,
    timeout_seconds: float = YAHOO_CHART_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urlopen,
) -> YahooChartQuote:
    """Fetch query2 then query1 within one bounded wall-clock budget."""

    deadline = time.monotonic() + min(20.0, max(1.0, timeout_seconds))
    last_error: Exception | None = None
    for url in yahoo_chart_urls(instrument.symbol, target_date):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        request = Request(
            url,
            headers={
                "User-Agent": YAHOO_CHART_USER_AGENT,
                "Accept": "application/json",
            },
        )
        try:
            with opener(request, timeout=remaining) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Yahoo chart payload is not an object")
            return parse_yahoo_chart(
                payload,
                instrument=instrument,
                target_date=target_date,
                source_url=url,
            )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            continue
    raise ValueError(f"Yahoo chart lookup failed for {instrument.symbol}") from last_error


def _parse_decimal(text: str) -> float:
    cleaned = (
        str(text)
        .strip()
        .replace(",", "")
        .replace("%", "")
        .replace("\u2212", "-")
        .replace("\u2013", "-")
    )
    if not cleaned or cleaned in {"-", "--", "N/A"}:
        raise ValueError("market value is missing")
    value = float(cleaned)
    if not math.isfinite(value):
        raise ValueError("market value is not finite")
    return value


def _table_rows_by_header(html: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    records: list[dict[str, str]] = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [
            cell.get_text(" ", strip=True)
            for cell in rows[0].find_all(["th", "td"])
        ]
        if not headers:
            continue
        for row in rows[1:]:
            cells = [
                cell.get_text(" ", strip=True)
                for cell in row.find_all(["th", "td"])
            ]
            if len(cells) < len(headers):
                continue
            records.append(dict(zip(headers, cells)))
    return records


def parse_investing_index_history(
    html: str,
    *,
    supplement: IndexHtmlSupplement,
    target_date: date,
) -> IndexHtmlQuote:
    """Parse an exact dated Investing.com historical-table row."""

    observations: dict[date, tuple[float, float | None]] = {}
    for row in _table_rows_by_header(html):
        raw_date = row.get("Date")
        raw_close = row.get("Price")
        if not raw_date or not raw_close:
            continue
        try:
            observation_date = datetime.strptime(raw_date, "%b %d, %Y").date()
            close = _parse_decimal(raw_close)
            raw_change = row.get("Change %")
            change = _parse_decimal(raw_change) if raw_change else None
        except ValueError:
            continue
        observations[observation_date] = (close, change)

    if target_date not in observations:
        raise ValueError("Investing history has no exact target-date row")
    close, percent_change = observations[target_date]
    previous_dates = [value for value in observations if value < target_date]
    previous_date = max(previous_dates) if previous_dates else None
    previous_close = (
        observations[previous_date][0] if previous_date is not None else None
    )
    if percent_change is None and previous_close not in (None, 0):
        percent_change = round((close / previous_close - 1) * 100, 6)
    return IndexHtmlQuote(
        supplement=supplement,
        target_date=target_date,
        close=close,
        percent_change=percent_change,
        previous_date=previous_date,
        previous_close=previous_close,
    )


def parse_yahoo_japan_index_history(
    html: str,
    *,
    supplement: IndexHtmlSupplement,
    target_date: date,
) -> IndexHtmlQuote:
    """Parse TOPIX from Yahoo! Finance Japan's server-rendered history table."""

    observations: dict[date, float] = {}
    for row in _table_rows_by_header(html):
        raw_date = row.get("日付")
        raw_close = row.get("終値")
        if not raw_date or not raw_close:
            continue
        date_match = re.match(r"^(20\d{2})/(\d{1,2})/(\d{1,2})", raw_date)
        if date_match is None:
            continue
        try:
            observation_date = date(*(int(value) for value in date_match.groups()))
            observations[observation_date] = _parse_decimal(raw_close)
        except ValueError:
            continue

    if target_date not in observations:
        raise ValueError("Yahoo Japan history has no exact target-date row")
    previous_dates = [value for value in observations if value < target_date]
    if not previous_dates:
        raise ValueError("Yahoo Japan history has no previous trading session")
    previous_date = max(previous_dates)
    close = observations[target_date]
    previous_close = observations[previous_date]
    if previous_close == 0:
        raise ValueError("Yahoo Japan previous close is zero")
    return IndexHtmlQuote(
        supplement=supplement,
        target_date=target_date,
        close=close,
        percent_change=round((close / previous_close - 1) * 100, 6),
        previous_date=previous_date,
        previous_close=previous_close,
    )


def parse_hang_seng_hstech_previous_close(
    html: str,
    *,
    supplement: IndexHtmlSupplement,
    target_date: date,
) -> IndexHtmlQuote:
    """Map a next-calendar-day Hang Seng Bank previous close to target date.

    The live quote page has no historical table.  Its ``Previous close`` can
    only corroborate the target session when the page's explicit update date
    is exactly the following calendar day.  Every other date is rejected so a
    current quote can never be silently attributed to an old report.
    """

    soup = BeautifulSoup(html, "html.parser")
    identity = " ".join(
        node.get_text(" ", strip=True)
        for node in [soup.title, soup.find("h1")]
        if node is not None
    ).casefold()
    if "hang seng tech" not in identity and "hstech" not in identity:
        raise ValueError("Hang Seng quote page identity is not HSTECH")

    updated_node = soup.select_one("#stock-stime")
    if updated_node is None:
        raise ValueError("Hang Seng quote update timestamp is missing")
    try:
        observed_at = datetime.strptime(
            updated_node.get_text(" ", strip=True),
            "%d/%m/%Y %H:%M",
        ).replace(tzinfo=ZoneInfo("Asia/Hong_Kong"))
    except ValueError as exc:
        raise ValueError("Hang Seng quote update timestamp is invalid") from exc
    if observed_at.date() != target_date + timedelta(days=1):
        raise ValueError(
            "Hang Seng previous close does not map unambiguously to target date"
        )

    raw_close: str | None = None
    for label in soup.find_all("dt"):
        if label.get_text(" ", strip=True).casefold() != "previous close":
            continue
        value_node = label.find_next_sibling("dd")
        if value_node is not None:
            raw_close = value_node.get_text(" ", strip=True)
            break
    if raw_close is None:
        raise ValueError("Hang Seng previous close is missing")
    return IndexHtmlQuote(
        supplement=supplement,
        target_date=target_date,
        close=_parse_decimal(raw_close),
        observed_at=observed_at,
    )


def fetch_index_html_supplement(
    supplement: IndexHtmlSupplement,
    target_date: date,
    *,
    timeout_seconds: float = INDEX_HTML_TIMEOUT_SECONDS,
    opener: Callable[..., Any] = urlopen,
) -> IndexHtmlQuote:
    """Fetch and parse one allowlisted HTML supplement within 15--20 seconds."""

    timeout = min(20.0, max(15.0, float(timeout_seconds)))
    request = Request(
        supplement.source_url,
        headers={
            "User-Agent": INDEX_HTML_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
        },
    )
    with opener(request, timeout=timeout) as response:
        html = response.read().decode("utf-8", errors="replace")

    parser_by_name: dict[str, Callable[..., IndexHtmlQuote]] = {
        "investing_history": parse_investing_index_history,
        "yahoo_japan_history": parse_yahoo_japan_index_history,
        "hang_seng_previous_close": parse_hang_seng_hstech_previous_close,
    }
    parser = parser_by_name.get(supplement.parser)
    if parser is None:
        raise ValueError(f"unsupported index HTML parser: {supplement.parser}")
    return parser(html, supplement=supplement, target_date=target_date)
