import asyncio
import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError
from unittest.mock import AsyncMock, patch

from codex_search.codex_search import bounded_timeout, build_codex_env, run_codex
from gpt_researcher.evidence import EvidenceItem, canonical_http_url, unique_http_sources
from gpt_researcher.market_data import (
    HANG_SENG_HSTECH_QUOTE_URL,
    INDEX_HTML_USER_AGENT,
    INVESTING_DOW_HISTORY_URL,
    INVESTING_HSI_HISTORY_URL,
    INVESTING_HSTECH_HISTORY_URL,
    INVESTING_KOSDAQ_HISTORY_URL,
    INVESTING_KOSPI_HISTORY_URL,
    INVESTING_NASDAQ_COMPOSITE_HISTORY_URL,
    INVESTING_RUSSELL_2000_HISTORY_URL,
    INVESTING_SP500_HISTORY_URL,
    INVESTING_TOPIX_HISTORY_URL,
    YAHOO_JAPAN_TOPIX_HISTORY_URL,
    IndexHtmlQuote,
    YahooInstrument,
    fetch_index_html_supplement,
    fetch_yahoo_chart,
    index_html_supplements_for_initial_market_lane,
    index_html_supplements_for_regional_gap,
    parse_hang_seng_hstech_previous_close,
    parse_investing_index_history,
    parse_yahoo_japan_index_history,
    parse_yahoo_chart,
    yahoo_instruments_for_initial_commodities_lane,
    yahoo_instruments_for_initial_equities_lane,
    yahoo_instruments_for_initial_market_lane,
    yahoo_instruments_for_regional_gap,
)
from gpt_researcher.retrievers.codex.codex import CodexSearch, _GlobalCodexSlot
from gpt_researcher.skills.researcher import ResearchConductor


class EvidenceTests(unittest.TestCase):
    def test_evidence_requires_real_http_and_canonicalizes_tracking(self):
        with self.assertRaises(ValueError):
            EvidenceItem(claim="x", source_url="codex-search://local")
        with self.assertRaises(ValueError):
            EvidenceItem(claim="   ", source_url="https://example.com")
        with self.assertRaises(ValueError):
            EvidenceItem(
                claim="invalid value",
                value={"nested": "object"},
                source_url="https://example.com",
            )

        first = EvidenceItem(
            claim="Close was 10",
            source_url="HTTPS://Example.COM/market/?utm_source=test&b=2&a=1#top",
        )
        second = EvidenceItem(
            claim="Another claim",
            source_url="https://example.com/market?a=1&b=2",
        )
        self.assertEqual(first.source_url, "https://example.com/market?a=1&b=2")
        self.assertEqual(unique_http_sources([first, second]), [first.source_url])
        self.assertIsNone(canonical_http_url("mcp://analysis"))
        self.assertEqual(
            canonical_http_url("https://example.test/path."),
            "https://example.test/path",
        )


class YahooChartTests(unittest.TestCase):
    @staticmethod
    def chart_payload():
        return {
            "chart": {
                "result": [
                    {
                        "meta": {
                            "symbol": "0700.HK",
                            "currency": "HKD",
                            "exchangeName": "HKG",
                            "fullExchangeName": "Hong Kong",
                            "exchangeTimezoneName": "Asia/Hong_Kong",
                            "priceHint": 2,
                        },
                        "timestamp": [
                            int(datetime(2026, 7, 8, 8, tzinfo=UTC).timestamp()),
                            int(datetime(2026, 7, 9, 8, tzinfo=UTC).timestamp()),
                        ],
                        "indicators": {
                            "quote": [{"close": [478.8, 469.6]}],
                        },
                    }
                ],
                "error": None,
            }
        }

    def test_parser_uses_exchange_date_and_previous_valid_close(self):
        instrument = YahooInstrument("Hong Kong", "Tencent", "0700.HK", "stock")
        quote = parse_yahoo_chart(
            self.chart_payload(),
            instrument=instrument,
            target_date=date(2026, 7, 9),
            source_url="https://query2.finance.yahoo.com/v8/finance/chart/0700.HK",
        )

        self.assertEqual(quote.close, 469.6)
        self.assertEqual(quote.previous_close, 478.8)
        self.assertEqual(quote.previous_date, date(2026, 7, 8))
        self.assertAlmostEqual(quote.percent_change, -1.92147, places=5)
        result = quote.to_search_result()
        self.assertEqual(len(result["evidence"]), 2)
        self.assertTrue(
            all(
                "Market: Hong Kong | Company/Index: Tencent"
                in evidence["summary"]
                for evidence in result["evidence"]
            )
        )
        self.assertEqual(
            result["href"],
            "https://query2.finance.yahoo.com/v8/finance/chart/0700.HK",
        )
        self.assertIn("Market: Hong Kong | Company/Index: Tencent", result["body"])

    def test_query2_falls_back_to_query1_with_explicit_user_agent(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        requests = []

        def opener(request, timeout):
            requests.append((request, timeout))
            if len(requests) == 1:
                raise URLError("query2 unavailable")
            return Response(self.chart_payload())

        quote = fetch_yahoo_chart(
            YahooInstrument("Hong Kong", "Tencent", "0700.HK", "stock"),
            date(2026, 7, 9),
            opener=opener,
        )

        self.assertEqual(quote.close, 469.6)
        self.assertIn("query2.finance.yahoo.com", requests[0][0].full_url)
        self.assertIn("query1.finance.yahoo.com", requests[1][0].full_url)
        self.assertIn("GPT-Researcher", requests[0][0].get_header("User-agent"))
        self.assertTrue(all(0 < timeout <= 20 for _, timeout in requests))

    def test_selection_is_limited_to_asian_regional_gaps(self):
        cases = {
            "Japan": {"7203.T", "^N225", "^TOPX"},
            "Korea": {"005930.KS", "^KS11", "^KQ11"},
            "Hong Kong": {"0700.HK", "^HSI", "HSTECH.HK"},
        }
        for region, expected in cases.items():
            query = f"Market-daily regional evidence gap — {region}. Target 2026-07-09."
            symbols = {
                instrument.symbol
                for instrument in yahoo_instruments_for_regional_gap(query)
            }
            self.assertTrue(expected.issubset(symbols))
        self.assertEqual(yahoo_instruments_for_regional_gap("Research lane 3"), ())
        self.assertEqual(
            yahoo_instruments_for_regional_gap(
                "Market-daily regional evidence gap — U.S. Target 2026-07-09"
            ),
            (),
        )

    def test_initial_lane_selector_is_strict_and_does_not_broaden_gap_selector(self):
        lane_one = (
            "Target trading date: 2026-07-09. Research lane 1 — market indices "
            "and macro expectations. Cover all ten benchmarks."
        )
        self.assertEqual(
            {item.symbol for item in yahoo_instruments_for_initial_market_lane(lane_one)},
            {"^GSPC", "^DJI", "^IXIC", "^RUT"},
        )
        self.assertEqual(yahoo_instruments_for_regional_gap(lane_one), ())
        self.assertEqual(
            yahoo_instruments_for_initial_market_lane(
                "Research lane 1 — important stocks and company catalysts."
            ),
            (),
        )

    def test_initial_equities_lane_selector_is_strict_and_named(self):
        lane_three = (
            "Target trading date: 2026-07-09. Research lane 3 — important "
            "equities in depth. Dynamically select at least 16 distinct stocks."
        )
        instruments = yahoo_instruments_for_initial_equities_lane(lane_three)
        self.assertEqual(
            {item.symbol: item.name for item in instruments},
            {
                "NVDA": "NVIDIA",
                "AAPL": "Apple",
                "MSFT": "Microsoft",
                "MU": "Micron Technology",
                "PEP": "PepsiCo",
                "MARA": "MARA Holdings",
                "DELL": "Dell Technologies",
                "SNDK": "Sandisk",
            },
        )
        self.assertTrue(all(item.region == "U.S." for item in instruments))
        self.assertTrue(all(item.kind == "stock" for item in instruments))
        for unrelated in (
            "Research lane 3 — commodities and cross-asset hot topics.",
            "Research lane 1 — market indices and macro expectations.",
            "Market-daily regional evidence gap — Japan. Target 2026-07-09.",
            "ordinary important equities in depth research",
        ):
            self.assertEqual(
                yahoo_instruments_for_initial_equities_lane(unrelated),
                (),
            )
        self.assertEqual(yahoo_instruments_for_regional_gap(lane_three), ())
        self.assertEqual(
            yahoo_instruments_for_initial_market_lane(
                "Market-daily regional evidence gap — Korea. Target 2026-07-09."
            ),
            (),
        )

    def test_initial_commodities_lane_selector_is_strict_and_named(self):
        lane_two = (
            "Target trading date: 2026-07-09. Research lane 2 — commodities "
            "and cross-asset hot topics. Cover WTI, Brent, gold and copper."
        )

        instruments = yahoo_instruments_for_initial_commodities_lane(lane_two)

        self.assertEqual(
            {item.symbol: item.name for item in instruments},
            {
                "CL=F": "WTI crude oil",
                "BZ=F": "Brent crude oil",
                "GC=F": "Gold",
                "HG=F": "Copper",
            },
        )
        self.assertTrue(all(item.kind == "commodity" for item in instruments))
        self.assertEqual(
            yahoo_instruments_for_initial_commodities_lane(
                "ordinary commodities research"
            ),
            (),
        )


class IndexHtmlSupplementTests(unittest.TestCase):
    @staticmethod
    def supplements(region: str):
        return index_html_supplements_for_regional_gap(
            f"Market-daily regional evidence gap — {region}. Target 2026-07-09."
        )

    def test_selection_is_allowlisted_to_explicit_initial_and_regional_queries(self):
        initial = index_html_supplements_for_initial_market_lane(
            "Research lane 1 — market indices and macro expectations."
        )
        japan = self.supplements("Japan")
        korea = self.supplements("Korea")
        hong_kong = self.supplements("Hong Kong")

        self.assertEqual(
            {item.source_url for item in initial},
            {
                INVESTING_SP500_HISTORY_URL,
                INVESTING_DOW_HISTORY_URL,
                INVESTING_NASDAQ_COMPOSITE_HISTORY_URL,
                INVESTING_RUSSELL_2000_HISTORY_URL,
            },
        )
        self.assertEqual(
            {item.source_url for item in japan},
            {INVESTING_TOPIX_HISTORY_URL, YAHOO_JAPAN_TOPIX_HISTORY_URL},
        )
        self.assertEqual(
            {item.source_url for item in korea},
            {INVESTING_KOSPI_HISTORY_URL, INVESTING_KOSDAQ_HISTORY_URL},
        )
        self.assertEqual(
            {item.source_url for item in hong_kong},
            {
                INVESTING_HSI_HISTORY_URL,
                INVESTING_HSTECH_HISTORY_URL,
                HANG_SENG_HSTECH_QUOTE_URL,
            },
        )
        self.assertEqual(
            index_html_supplements_for_regional_gap("ordinary TOPIX research"),
            (),
        )
        self.assertEqual(
            index_html_supplements_for_initial_market_lane(
                "Research lane 1 — commodities and cross-asset hot topics."
            ),
            (),
        )
        self.assertEqual(
            index_html_supplements_for_regional_gap(
                "Research lane 1 — market indices and macro expectations."
            ),
            (),
        )

    def test_investing_parser_requires_exact_target_date(self):
        supplement = self.supplements("Japan")[0]
        html = """
        <table><tr><th>Date</th><th>Price</th><th>Change %</th></tr>
        <tr><td>Jul 10, 2026</td><td>4,036.08</td><td>+0.39%</td></tr>
        <tr><td>Jul 09, 2026</td><td>4,020.37</td><td>+0.35%</td></tr>
        <tr><td>Jul 08, 2026</td><td>4,006.43</td><td>-1.37%</td></tr>
        </table>
        """

        quote = parse_investing_index_history(
            html,
            supplement=supplement,
            target_date=date(2026, 7, 9),
        )

        self.assertEqual(quote.close, 4020.37)
        self.assertEqual(quote.percent_change, 0.35)
        self.assertEqual(quote.previous_date, date(2026, 7, 8))
        self.assertEqual(quote.to_search_result()["href"], supplement.source_url)
        with self.assertRaisesRegex(ValueError, "exact target-date"):
            parse_investing_index_history(
                html,
                supplement=supplement,
                target_date=date(2026, 7, 7),
            )

    def test_yahoo_japan_parser_computes_change_from_previous_session(self):
        supplement = self.supplements("Japan")[1]
        html = """
        <table><tr><th>日付</th><th>始値</th><th>高値</th><th>安値</th><th>終値</th></tr>
        <tr><td>2026/7/9</td><td>3,998.07</td><td>4,035.46</td><td>3,995.49</td><td>4,020.37</td></tr>
        <tr><td>2026/7/8</td><td>4,041.75</td><td>4,065.09</td><td>4,006.43</td><td>4,006.43</td></tr>
        </table>
        """

        quote = parse_yahoo_japan_index_history(
            html,
            supplement=supplement,
            target_date=date(2026, 7, 9),
        )

        self.assertEqual(quote.close, 4020.37)
        self.assertEqual(quote.previous_close, 4006.43)
        self.assertAlmostEqual(quote.percent_change or 0, 0.34794, places=5)
        self.assertIn("Yahoo! Finance Japan", quote.flat_summary())

    def test_hang_seng_previous_close_only_maps_from_next_calendar_day(self):
        supplement = self.supplements("Hong Kong")[1]

        def html(updated: str) -> str:
            return f"""
            <html><head><title>HSTECH HANG SENG TECH INDEX</title></head>
            <body><h1>HSTECH HANG SENG TECH INDEX</h1>
            <dl><dt>Previous close</dt><dd>4,731.560</dd></dl>
            <span id="stock-stime">{updated}</span></body></html>
            """

        quote = parse_hang_seng_hstech_previous_close(
            html("10/07/2026 16:08"),
            supplement=supplement,
            target_date=date(2026, 7, 9),
        )

        self.assertEqual(quote.close, 4731.56)
        self.assertEqual(quote.target_date, date(2026, 7, 9))
        self.assertEqual(len(quote.to_search_result()["evidence"]), 1)
        with self.assertRaisesRegex(ValueError, "unambiguously"):
            parse_hang_seng_hstech_previous_close(
                html("11/07/2026 16:08"),
                supplement=supplement,
                target_date=date(2026, 7, 9),
            )

    def test_live_wrapper_sets_explicit_user_agent_and_bounded_timeout(self):
        supplement = self.supplements("Japan")[0]
        html = b"""
        <table><tr><th>Date</th><th>Price</th><th>Change %</th></tr>
        <tr><td>Jul 09, 2026</td><td>4,020.37</td><td>+0.35%</td></tr>
        </table>
        """
        captured = {}

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            @staticmethod
            def read():
                return html

        def opener(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return Response()

        quote = fetch_index_html_supplement(
            supplement,
            date(2026, 7, 9),
            timeout_seconds=999,
            opener=opener,
        )

        self.assertEqual(quote.close, 4020.37)
        self.assertEqual(captured["timeout"], 20)
        self.assertEqual(
            captured["request"].get_header("User-agent"),
            INDEX_HTML_USER_AGENT,
        )


class CodexResultParsingTests(unittest.TestCase):
    def test_retriever_hard_caps_timeout_and_retries(self):
        with patch.dict(
            os.environ,
            {
                "CODEX_SEARCH_RETRIEVER_TIMEOUT": "999",
                "CODEX_SEARCH_RETRIEVER_RETRIES": "7",
            },
        ):
            retriever = CodexSearch("bounded")

        self.assertEqual(retriever.timeout, 300)
        self.assertEqual(retriever.retries, 1)
        self.assertEqual(bounded_timeout("999"), 300)

    def test_helper_and_inner_codex_environments_use_explicit_allowlists(self):
        with patch.dict(
            os.environ,
            {
                "CODEX_SEARCH_CODEX_BIN": "/tmp/fake-codex",
                "CODEX_SEARCH_PRIVATE_TOKEN": "must-not-leak",
                "TAVILY_API_KEY": "must-not-leak-either",
                "HTTPS_PROXY": "http://127.0.0.1:8080",
            },
            clear=True,
        ):
            retriever_env = CodexSearch("env")._helper_env()
            inner_env = build_codex_env(SimpleNamespace(codex_home=None))

        self.assertEqual(retriever_env["CODEX_SEARCH_CODEX_BIN"], "/tmp/fake-codex")
        self.assertEqual(retriever_env["HTTPS_PROXY"], "http://127.0.0.1:8080")
        self.assertEqual(inner_env["HTTPS_PROXY"], "http://127.0.0.1:8080")
        for env in (retriever_env, inner_env):
            self.assertNotIn("CODEX_SEARCH_PRIVATE_TOKEN", env)
            self.assertNotIn("TAVILY_API_KEY", env)
        self.assertNotIn("CODEX_SEARCH_CODEX_BIN", inner_env)

    def test_helper_uses_schema_and_readonly_isolated_workdir(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inspection_path = root / "inspection.json"
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                textwrap.dedent(
                    """
                    #!/usr/bin/env python3
                    import json
                    import os
                    import stat
                    import sys
                    from pathlib import Path

                    args = sys.argv[1:]
                    workdir = Path(args[args.index("--cd") + 1])
                    schema = Path(args[args.index("--output-schema") + 1])
                    output = Path(args[args.index("--output-last-message") + 1])
                    write_failed = False
                    try:
                        (workdir / "should-not-write").write_text("x")
                    except OSError:
                        write_failed = True
                    Path(__INSPECTION__).write_text(json.dumps({
                        "args": args,
                        "workdir_mode": stat.S_IMODE(workdir.stat().st_mode),
                        "write_failed": write_failed,
                        "schema": json.loads(schema.read_text()),
                    }))
                    output.write_text(json.dumps({
                        "claims": [],
                        "sources": [],
                        "caveats": [],
                    }))
                    """
                )
                .replace("__INSPECTION__", repr(str(inspection_path)))
                .lstrip(),
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            args = SimpleNamespace(
                codex_bin=str(fake_codex),
                model_provider=None,
                supports_websockets=False,
                reasoning_effort=None,
                service_tier=None,
                use_user_config=False,
                model=None,
                codex_home=None,
                workdir=None,
                timeout=5,
                show_events=False,
                telemetry_file=None,
            )

            run_codex(args, "test", search_enabled=True, label="search")
            inspection = json.loads(inspection_path.read_text(encoding="utf-8"))

        self.assertEqual(inspection["workdir_mode"], 0o555)
        self.assertTrue(inspection["write_failed"])
        self.assertEqual(
            inspection["args"][inspection["args"].index("--sandbox") + 1],
            "read-only",
        )
        self.assertIn("--skip-git-repo-check", inspection["args"])
        self.assertEqual(
            set(inspection["schema"]["required"]),
            {"claims", "sources", "caveats"},
        )

    def test_incomplete_thread_event_is_retryable_but_empty_sources_are_not(self):
        self.assertTrue(
            CodexSearch._is_transient(
                '{"type":"thread.started","thread_id":"test"}',
                "failed",
            )
        )
        self.assertFalse(
            CodexSearch._is_transient(
                "Codex returned no valid HTTP(S) sources",
                "invalid_output",
            )
        )
        self.assertFalse(
            CodexSearch._is_transient(
                '{"type":"thread.started"} You have hit your usage limit; purchase more credits',
                "failed",
            )
        )
        self.assertTrue(CodexSearch._is_transient("HTTP status 503", "failed"))
        self.assertFalse(
            CodexSearch._is_transient("Expected at most 500 characters", "failed")
        )

    def test_structured_payload_becomes_one_result_per_real_source(self):
        retriever = CodexSearch("market close")
        payload = {
            "claims": [
                {
                    "claim": "Index closed higher",
                    "value": 123.4,
                    "unit": "points",
                    "as_of_date": "2026-07-09",
                    "source_urls": ["https://exchange.example/data?utm_source=x"],
                    "summary": "Official close.",
                }
            ],
            "sources": [
                {
                    "url": "https://exchange.example/data",
                    "title": "Exchange data",
                    "summary": "Primary market data.",
                },
                {
                    "url": "codex-search://local",
                    "title": "Synthetic",
                    "summary": "Must be ignored.",
                },
            ],
            "caveats": [],
        }

        results = retriever._parse_results(json.dumps(payload), max_results=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["href"], "https://exchange.example/data")
        self.assertIn("Source URL: https://exchange.example/data", results[0]["body"])
        self.assertEqual(results[0]["evidence"][0]["value"], 123.4)

    def test_malformed_claim_is_isolated_without_failing_valid_source(self):
        retriever = CodexSearch("malformed claim")
        payload = {
            "claims": [
                {
                    "claim": "Boolean is outside the evidence schema",
                    "value": True,
                    "unit": None,
                    "as_of_date": None,
                    "source_urls": ["https://example.com/source"],
                    "summary": "invalid claim value",
                }
            ],
            "sources": [
                {
                    "url": "https://example.com/source",
                    "title": "Valid source",
                    "summary": "A valid source summary remains usable.",
                }
            ],
            "caveats": [],
        }

        results = retriever._parse_results(json.dumps(payload), max_results=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["href"], "https://example.com/source")
        self.assertEqual(
            results[0]["evidence"][0]["claim"],
            "A valid source summary remains usable.",
        )

    def test_legacy_markdown_fallback_keeps_only_http_links(self):
        retriever = CodexSearch("legacy")
        results = retriever._parse_results(
            "Finding supported by [Official](https://example.com/fact). Ignore codex-search://local.",
            max_results=5,
        )
        self.assertEqual([result["href"] for result in results], ["https://example.com/fact"])


class ConductorPlanningTests(unittest.IsolatedAsyncioTestCase):
    def test_conflict_detection_normalizes_equivalent_numeric_formats(self):
        conductor = ResearchConductor(SimpleNamespace())
        same_value = [
            EvidenceItem(
                claim="KOSPI target-date close",
                value=value,
                unit="points",
                as_of_date="2026-07-09",
                source_url=f"https://example.com/same-{index}",
            )
            for index, value in enumerate(("3,200.0", 3200))
        ]
        for item in same_value:
            conductor._evidence_by_checksum[item.checksum] = item
        self.assertEqual(conductor._find_evidence_conflicts(), [])

        different = EvidenceItem(
            claim="KOSPI target-date close",
            value=3201,
            unit="points",
            as_of_date="2026-07-09",
            source_url="https://example.com/different",
        )
        conductor._evidence_by_checksum[different.checksum] = different

        conflicts = conductor._find_evidence_conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["values"], ["3200", "3201"])

    async def test_planning_presearch_excludes_codex_and_normalizes_three_queries(self):
        calls = {"codex": 0, "tavily": 0}

        class CodexSearch:
            def __init__(self, query, query_domains=None):
                calls["codex"] += 1

            async def search_async(self, max_results=5):
                raise AssertionError("Codex must not run during planning")

        class TavilySearch:
            def __init__(self, query, query_domains=None):
                self.query = query

            def search(self, max_results=5):
                calls["tavily"] += 1
                return [{"href": "https://example.com", "body": "planning context"}]

        researcher = SimpleNamespace(
            retrievers=[CodexSearch, TavilySearch],
            cfg=SimpleNamespace(max_search_results_per_query=5),
            websocket=None,
            role="analyst",
            parent_query="",
            report_type="research_report",
            add_costs=lambda *args, **kwargs: None,
            kwargs={},
        )
        conductor = ResearchConductor(researcher)

        with (
            patch(
                "gpt_researcher.skills.researcher.plan_research_outline",
                new=AsyncMock(return_value=["one", "two", "three", "four"]),
            ),
            patch(
                "gpt_researcher.skills.researcher.stream_output",
                new=AsyncMock(),
            ),
        ):
            queries = await conductor.plan_research("investigate this")

        self.assertEqual(queries, ["one", "two", "three"])
        self.assertEqual(len(conductor.research_work_items), 3)
        self.assertEqual(calls, {"codex": 0, "tavily": 1})

    def test_market_daily_work_items_lock_all_acceptance_coverage(self):
        conductor = ResearchConductor(SimpleNamespace())
        query = "调研股票市场：美、日、韩、港大盘、宏观预期、大宗商品和重要股票日报"

        items = conductor._normalize_work_items([], query)

        self.assertEqual(len(items), 3)
        combined = " ".join(item.query for item in items)
        for required in (
            "S&P 500",
            "Russell 2000",
            "Nikkei 225",
            "TOPIX",
            "KOSPI",
            "KOSDAQ",
            "Hang Seng TECH",
            "WTI",
            "Brent",
            "gold",
            "copper",
            "at least 16 distinct stocks",
            "ticker, exchange",
        ):
            self.assertIn(required, combined)
    def test_lightweight_retriever_queries_are_bounded_but_keep_lane_coverage(self):
        conductor = ResearchConductor(SimpleNamespace())
        work_items = conductor._market_daily_work_items(
            "调研股票市场：美、日、韩、港大盘、宏观预期、大宗商品和重要股票日报"
        )

        self.assertIsNotNone(work_items)
        for item in work_items:
            compact = conductor._compact_web_retriever_query(item.query)
            self.assertLessEqual(len(compact), 380)
            self.assertIn("requested trading date", compact)
        self.assertIn("S&P 500", conductor._compact_web_retriever_query(work_items[0].query))
        self.assertIn("WTI", conductor._compact_web_retriever_query(work_items[1].query))
        self.assertIn("Hong Kong stocks", conductor._compact_web_retriever_query(work_items[2].query))
        for item in work_items:
            lightweight_queries = conductor._lightweight_web_retriever_queries(item.query)
            self.assertEqual(len(lightweight_queries), 3)
            self.assertTrue(all(len(query) <= 380 for query in lightweight_queries))
        commodity_queries = conductor._lightweight_web_retriever_queries(work_items[1].query)
        self.assertIn("WTI Brent", commodity_queries[0])
        self.assertIn("gold copper", commodity_queries[1])

    def test_same_url_merges_retriever_provenance_and_evidence(self):
        existing = {
            "href": "https://example.com/data",
            "retriever": "TavilySearch",
            "body": "Tavily snippet",
            "evidence": [{"checksum": "a", "claim": "first"}],
        }
        incoming = {
            "href": "https://example.com/data",
            "body": "Codex claim",
            "raw_content": "Long Codex evidence",
            "evidence": [{"checksum": "b", "claim": "second"}],
        }

        ResearchConductor._merge_duplicate_search_result(existing, incoming, "CodexSearch")

        self.assertEqual(existing["retrievers"], ["TavilySearch", "CodexSearch"])
        self.assertEqual({item["checksum"] for item in existing["evidence"]}, {"a", "b"})
        self.assertIn("Tavily snippet", existing["body"])
        self.assertIn("Codex claim", existing["body"])

    def test_empty_market_stock_lane_splits_gap_into_three_asian_queries(self):
        conductor = ResearchConductor(SimpleNamespace())
        original = "调研股票市场：美、日、韩、港大盘、宏观预期、大宗商品和重要股票日报"
        work_items = conductor._normalize_work_items([], original)
        conductor._query_http_source_counts = {
            work_items[0].query: 8,
            work_items[1].query: 8,
            work_items[2].query: 0,
        }

        with patch.dict(
            os.environ,
            {
                "RESEARCH_MIN_HTTP_SOURCES_PER_WORK_ITEM": "8",
                "RESEARCH_MIN_STOCK_SOURCES_PER_MARKET": "4",
                "MCP_RESEARCH_MIN_HTTP_SOURCES": "25",
                "MCP_RESEARCH_TARGET_DATE": "2026-07-09",
            },
        ):
            followups = conductor._build_gap_followups(
                original_query=original,
                work_items=work_items,
                initial_results=["index evidence", "commodity evidence", ""],
            )

        self.assertEqual(len(followups), 3)
        self.assertIn("— Japan", followups[0])
        self.assertIn("— Korea", followups[1])
        self.assertIn("— Hong Kong", followups[2])
        combined = " ".join(followups)
        for required in (
            "at least four",
            "at least two index-weight/high-liquidity leaders",
            "at least two target-date event-driven",
            "ticker, exchange, exact target-date close, daily percentage change",
            "recent fundamental background, principal risk",
            "two independent direct HTTP(S) URLs for each index",
            "Nikkei 225",
            "TOPIX",
            "KOSPI",
            "KOSDAQ",
            "Hang Seng Index",
            "Hang Seng TECH Index",
        ):
            self.assertIn(required, combined)
        compact_followups = [
            conductor._compact_web_retriever_query(followup) for followup in followups
        ]
        self.assertIn("Nikkei 225 TOPIX", compact_followups[0])
        self.assertIn("KOSPI KOSDAQ", compact_followups[1])
        self.assertIn("Hang Seng HSI Hang Seng Tech HSTECH", compact_followups[2])
        self.assertIn("Toyota 7203", compact_followups[0])
        self.assertIn("Samsung 005930", compact_followups[1])
        self.assertIn("Tencent 0700", compact_followups[2])
        self.assertTrue(all("at least four stocks" in query for query in compact_followups))
        korea_web_queries = conductor._lightweight_web_retriever_queries(followups[1])
        self.assertEqual(len(korea_web_queries), 5)
        self.assertTrue(all(len(query) <= 380 for query in korea_web_queries))
        self.assertIn("KOSPI KOSDAQ", korea_web_queries[0])
        self.assertIn("Samsung Electronics 005930.KS", korea_web_queries[1])
        self.assertIn("Naver 035420.KS", korea_web_queries[2])
        self.assertIn("gainers losers", korea_web_queries[3])
        self.assertIn("company IR exchange filing", korea_web_queries[4])

    def test_gap_followups_are_disabled_after_the_single_round(self):
        conductor = ResearchConductor(SimpleNamespace())
        work_items = conductor._normalize_work_items([], "generic research request")
        conductor._gap_followup_rounds = 1

        followups = conductor._build_gap_followups(
            original_query="generic research request",
            work_items=work_items,
            initial_results=["", "", ""],
        )

        self.assertEqual(followups, [])

    def test_uniquely_missing_us_stock_region_can_enter_gap_round(self):
        conductor = ResearchConductor(SimpleNamespace())
        original = "调研股票市场：美、日、韩、港大盘、宏观预期、大宗商品和重要股票日报"
        stock_item = conductor._normalize_work_items([], original)[2]
        evidence = []
        for index in range(4):
            evidence.extend(
                (
                    EvidenceItem(
                        claim="Japan listed stock",
                        source_url=f"https://issuer{index}.co.jp/ir",
                    ),
                    EvidenceItem(
                        claim="Korea listed stock",
                        source_url=f"https://issuer{index}.co.kr/ir",
                    ),
                    EvidenceItem(
                        claim="Hong Kong listed stock",
                        source_url=f"https://issuer{index}.com.hk/ir",
                    ),
                )
            )
        conductor._evidence_by_query[stock_item.query] = evidence
        counts = conductor._market_stock_region_source_counts(stock_item.query)

        followups = conductor._market_stock_gap_followups(
            stock_item,
            region_counts=counts,
            minimum_per_market=4,
        )

        self.assertEqual(len(followups), 1)
        self.assertIn("— U.S.", followups[0])
        self.assertIn("S&P 500", followups[0])
        self.assertIn("Nasdaq Composite", followups[0])

    def test_kospi_only_articles_do_not_count_as_korea_stock_evidence(self):
        conductor = ResearchConductor(SimpleNamespace())
        original = "调研股票市场：美、日、韩、港大盘、宏观预期、大宗商品和重要股票日报"
        stock_item = conductor._normalize_work_items([], original)[2]
        conductor._evidence_by_query[stock_item.query] = [
            EvidenceItem(
                claim="KOSPI and KOSDAQ index close and macro market breadth",
                source_title="Korea index market daily",
                source_url=f"https://news.example.com/markets/kospi-{index}",
                summary="This broad article discusses KOSPI and KOSDAQ, not an individual stock.",
            )
            for index in range(4)
        ]

        counts = conductor._market_stock_region_source_counts(stock_item.query)

        self.assertEqual(counts["Korea"], 0)

    def test_failed_initial_stock_codex_forces_three_asian_gap_queries(self):
        conductor = ResearchConductor(SimpleNamespace())
        original = "调研股票市场：美、日、韩、港大盘、宏观预期、大宗商品和重要股票日报"
        work_items = conductor._normalize_work_items([], original)
        stock_item = work_items[2]
        conductor._query_http_source_counts = {
            item.query: 8 for item in work_items
        }
        conductor._codex_run_metadata = [
            {
                "query": stock_item.query,
                "initial_work_item": True,
                "status": "invalid_output",
            }
        ]
        apparently_complete = {
            "Japan": 4,
            "Korea": 4,
            "Hong Kong": 4,
            "U.S.": 4,
        }

        with (
            patch.object(
                conductor,
                "_market_stock_region_source_counts",
                return_value=apparently_complete,
            ),
            patch.dict(
                os.environ,
                {
                    "RESEARCH_MIN_HTTP_SOURCES_PER_WORK_ITEM": "8",
                    "RESEARCH_MIN_STOCK_SOURCES_PER_MARKET": "4",
                    "MCP_RESEARCH_MIN_HTTP_SOURCES": "25",
                },
            ),
        ):
            followups = conductor._build_gap_followups(
                original_query=original,
                work_items=work_items,
                initial_results=["index", "commodity", "broad stock articles"],
            )

        self.assertEqual(len(followups), 3)
        self.assertIn("— Japan", followups[0])
        self.assertIn("— Korea", followups[1])
        self.assertIn("— Hong Kong", followups[2])

    def test_any_stock_region_gap_reserves_followup_round_for_asia(self):
        conductor = ResearchConductor(SimpleNamespace())
        original = "调研股票市场：美、日、韩、港大盘、宏观预期、大宗商品和重要股票日报"
        work_items = conductor._normalize_work_items([], original)
        conductor._query_http_source_counts = {item.query: 20 for item in work_items}

        with (
            patch.object(
                conductor,
                "_market_stock_region_source_counts",
                return_value={
                    "U.S.": 0,
                    "Japan": 4,
                    "Korea": 4,
                    "Hong Kong": 4,
                },
            ),
            patch.dict(
                os.environ,
                {
                    "RESEARCH_MIN_HTTP_SOURCES_PER_WORK_ITEM": "8",
                    "RESEARCH_MIN_STOCK_SOURCES_PER_MARKET": "4",
                },
            ),
        ):
            followups = conductor._build_gap_followups(
                original_query=original,
                work_items=work_items,
                initial_results=["index", "commodity", "stock"],
            )

        self.assertEqual(len(followups), 3)
        self.assertIn("— Japan", followups[0])
        self.assertIn("— Korea", followups[1])
        self.assertIn("— Hong Kong", followups[2])
        self.assertNotIn("— U.S.", " ".join(followups))

    def test_regional_gap_evidence_is_attributed_to_stock_work_item(self):
        conductor = ResearchConductor(SimpleNamespace())
        original = "调研股票市场：美、日、韩、港大盘、宏观预期、大宗商品和重要股票日报"
        conductor.research_work_items = conductor._normalize_work_items([], original)
        stock_item = conductor.research_work_items[2]
        conductor._evidence_by_query[stock_item.query] = [
            EvidenceItem(claim="initial stock", source_url="https://example.com/initial")
        ]
        regional_query = "Market-daily regional evidence gap — Korea. Target 2026-07-09."
        conductor._evidence_by_query[regional_query] = [
            EvidenceItem(
                claim=f"Korea stock {index}",
                source_url=f"https://example.com/korea-{index}",
            )
            for index in range(8)
        ]
        for evidence in conductor._evidence_by_query.values():
            for item in evidence:
                conductor._evidence_by_checksum[item.checksum] = item

        with patch.dict(
            os.environ,
            {
                "RESEARCH_MIN_HTTP_SOURCES_PER_WORK_ITEM": "8",
                "MCP_RESEARCH_MIN_HTTP_SOURCES": "1",
            },
        ):
            metrics = conductor.evidence_metrics

        self.assertEqual(metrics["per_work_item_http_sources"]["3"], 9)


class ConductorConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_ordinary_retrievers_share_one_four_slot_semaphore(self):
        conductor = ResearchConductor(SimpleNamespace())
        with patch.dict(os.environ, {"SEARCH_RETRIEVER_CONCURRENCY": "4"}):
            tavily = conductor._get_retriever_semaphore("TavilySearch")
            yahoo = conductor._get_retriever_semaphore("YahooChart")
            index_html = conductor._get_retriever_semaphore("IndexHtml")
            brave = conductor._get_retriever_semaphore("BraveSearch")
            codex = conductor._get_retriever_semaphore("CodexSearch")

        self.assertIs(tavily, yahoo)
        self.assertIs(tavily, index_html)
        self.assertIs(tavily, brave)
        self.assertIsNot(tavily, codex)
        self.assertEqual(tavily._value, 4)

    async def test_yahoo_regional_fetches_share_the_four_slot_ceiling(self):
        conductor = ResearchConductor(SimpleNamespace())
        state = {"active": 0, "peak": 0}
        lock = threading.Lock()

        def fake_fetch(instrument, target_date):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.02)
            with lock:
                state["active"] -= 1
            return SimpleNamespace(
                to_search_result=lambda: {
                    "title": instrument.name,
                    "href": f"https://query2.finance.yahoo.com/{instrument.symbol}",
                    "body": str(target_date),
                }
            )

        queries = [
            f"Market-daily regional evidence gap — {region}. Target 2026-07-09."
            for region in ("Japan", "Korea", "Hong Kong")
        ]
        with (
            patch.dict(os.environ, {"SEARCH_RETRIEVER_CONCURRENCY": "4"}),
            patch(
                "gpt_researcher.skills.researcher.fetch_yahoo_chart",
                new=fake_fetch,
            ),
        ):
            results = await asyncio.gather(
                *(conductor._run_yahoo_chart_fallback(query) for query in queries)
            )

        self.assertEqual(state["peak"], 4)
        self.assertTrue(all(name == "YahooChart" for name, _ in results))
        self.assertTrue(all(items for _, items in results))

    async def test_yahoo_regional_fetch_is_fail_soft_per_symbol(self):
        conductor = ResearchConductor(SimpleNamespace())

        with patch(
            "gpt_researcher.skills.researcher.fetch_yahoo_chart",
            side_effect=URLError("temporary Yahoo failure"),
        ):
            retriever_name, results = await conductor._run_yahoo_chart_fallback(
                "Market-daily regional evidence gap — Hong Kong. "
                "Target trading date: 2026-07-09."
            )

        self.assertEqual(retriever_name, "YahooChart")
        self.assertEqual(results, [])

    async def test_initial_index_sources_share_ordinary_ceiling_and_fail_soft(self):
        conductor = ResearchConductor(SimpleNamespace())
        state = {"active": 0, "peak": 0}
        lock = threading.Lock()

        def enter():
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.02)
            with lock:
                state["active"] -= 1

        def fake_yahoo(instrument, target_date):
            enter()
            if instrument.symbol == "^DJI":
                raise URLError("one Yahoo symbol unavailable")
            return SimpleNamespace(
                to_search_result=lambda: {
                    "title": instrument.name,
                    "href": f"https://query2.finance.yahoo.com/{instrument.symbol}",
                    "body": str(target_date),
                }
            )

        def fake_index_html(supplement, target_date):
            enter()
            if supplement.symbol == "^RUT":
                raise URLError("one Investing page unavailable")
            return IndexHtmlQuote(
                supplement=supplement,
                target_date=target_date,
                close=100.0,
                percent_change=1.0,
            )

        query = (
            "Target trading date: 2026-07-09. Research lane 1 — market indices "
            "and macro expectations."
        )
        with (
            patch.dict(os.environ, {"SEARCH_RETRIEVER_CONCURRENCY": "4"}),
            patch(
                "gpt_researcher.skills.researcher.fetch_yahoo_chart",
                new=fake_yahoo,
            ),
            patch(
                "gpt_researcher.skills.researcher.fetch_index_html_supplement",
                new=fake_index_html,
            ),
        ):
            yahoo, index_html = await asyncio.gather(
                conductor._run_yahoo_chart_fallback(query),
                conductor._run_index_html_supplements(query),
            )

        self.assertEqual(state["peak"], 4)
        self.assertEqual(yahoo[0], "YahooChart")
        self.assertEqual(index_html[0], "IndexHtml")
        self.assertEqual(len(yahoo[1]), 3)
        self.assertEqual(len(index_html[1]), 3)
        self.assertFalse(any("^DJI" in item["href"] for item in yahoo[1]))
        self.assertNotIn("^RUT", {item["title"] for item in index_html[1]})

    async def test_initial_equities_integration_shares_ceiling_and_fails_soft(self):
        researcher = SimpleNamespace(
            retrievers=[],
            cfg=SimpleNamespace(max_search_results_per_query=5),
        )
        conductor = ResearchConductor(researcher)
        state = {"active": 0, "peak": 0}
        lock = threading.Lock()

        def fake_fetch(instrument, target_date):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.02)
            with lock:
                state["active"] -= 1
            if instrument.symbol == "PEP":
                raise URLError("one equity unavailable")
            return SimpleNamespace(
                to_search_result=lambda: {
                    "title": instrument.name,
                    "href": f"https://query2.finance.yahoo.com/{instrument.symbol}",
                    "body": str(target_date),
                }
            )

        query = (
            "Target trading date: 2026-07-09. Research lane 3 — important "
            "equities in depth. Dynamically select stocks."
        )
        with (
            patch.dict(os.environ, {"SEARCH_RETRIEVER_CONCURRENCY": "4"}),
            patch(
                "gpt_researcher.skills.researcher.fetch_yahoo_chart",
                new=fake_fetch,
            ),
        ):
            results = await conductor._get_search_results_from_all_retrievers(
                query,
                record_evidence=False,
            )

        self.assertEqual(state["peak"], 4)
        self.assertEqual(len(results), 7)
        self.assertEqual(
            {item["title"] for item in results},
            {
                "NVIDIA",
                "Apple",
                "Microsoft",
                "Micron Technology",
                "MARA Holdings",
                "Dell Technologies",
                "Sandisk",
            },
        )

    async def test_index_html_supplements_are_fail_soft_per_source(self):
        conductor = ResearchConductor(SimpleNamespace())

        def fake_fetch(supplement, target_date):
            if supplement.provider == "Investing.com":
                raise URLError("temporary source failure")
            return IndexHtmlQuote(
                supplement=supplement,
                target_date=target_date,
                close=4020.37,
                percent_change=0.35,
            )

        with patch(
            "gpt_researcher.skills.researcher.fetch_index_html_supplement",
            new=fake_fetch,
        ):
            retriever_name, results = await conductor._run_index_html_supplements(
                "Market-daily regional evidence gap — Japan. "
                "Target trading date: 2026-07-09."
            )

        self.assertEqual(retriever_name, "IndexHtml")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["href"], YAHOO_JAPAN_TOPIX_HISTORY_URL)
        self.assertEqual(results[0]["evidence"][0]["as_of_date"], "2026-07-09")

    async def test_hybrid_report_reuses_one_web_plan_for_local_context(self):
        class ContextManager:
            def __init__(self):
                self.queries = []

            async def get_similar_content_by_query(self, query, data):
                self.queries.append(query)
                return f"local:{query}"

        class PromptFamily:
            @staticmethod
            def join_local_web_documents(local, web):
                return f"{local}|{web}"

        researcher = SimpleNamespace(
            query="hybrid research",
            retrievers=[],
            cfg=SimpleNamespace(doc_path="unused", curate_sources=False),
            verbose=False,
            websocket=None,
            visited_urls=set(),
            agent="analyst",
            role="role",
            source_urls=[],
            complement_source_urls=False,
            report_source="hybrid",
            document_urls=[],
            vector_store=None,
            query_domains=[],
            context_manager=ContextManager(),
            prompt_family=PromptFamily(),
            get_costs=lambda: 0,
        )
        conductor = ResearchConductor(researcher)

        async def one_web_pass(query, scraped_data=None, query_domains=None):
            conductor.research_work_items = conductor._normalize_work_items(
                ["hybrid one", "hybrid two", "hybrid three"],
                query,
            )
            return "web context"

        conductor._get_context_by_web_search = AsyncMock(side_effect=one_web_pass)
        loader = SimpleNamespace(load=AsyncMock(return_value=[{"content": "local doc"}]))
        with patch(
            "gpt_researcher.skills.researcher.DocumentLoader",
            return_value=loader,
        ):
            context = await conductor.conduct_research()

        self.assertEqual(conductor._get_context_by_web_search.await_count, 1)
        self.assertEqual(
            researcher.context_manager.queries,
            ["hybrid one", "hybrid two", "hybrid three"],
        )
        self.assertIn("web context", context)

    async def test_regional_gap_runs_five_bounded_lightweight_searches(self):
        seen_queries = []

        class TavilySearch:
            def __init__(self, query, query_domains=None):
                self.query = query

            def search(self, max_results=5):
                seen_queries.append(self.query)
                return [
                    {
                        "href": f"https://example.com/result-{len(seen_queries)}",
                        "title": "Regional evidence",
                        "body": "regional source-backed evidence " * 10,
                    }
                ]

        researcher = SimpleNamespace(
            retrievers=[TavilySearch],
            cfg=SimpleNamespace(max_search_results_per_query=5),
        )
        conductor = ResearchConductor(researcher)
        query = (
            "Market-daily regional evidence gap — Korea. Target trading date: "
            "2026-07-09; timezone: Asia/Singapore. Investigate four stocks."
        )

        with (
            patch.object(
                conductor,
                "_run_yahoo_chart_fallback",
                new=AsyncMock(return_value=("YahooChart", [])),
            ),
            patch.object(
                conductor,
                "_run_index_html_supplements",
                new=AsyncMock(return_value=("IndexHtml", [])),
            ),
        ):
            results = await conductor._get_search_results_from_all_retrievers(query)

        self.assertEqual(len(seen_queries), 5)
        self.assertEqual(len(results), 5)

    async def test_market_stock_gap_queries_run_three_way_parallel(self):
        original = "调研股票市场：美、日、韩、港大盘、宏观预期、大宗商品和重要股票日报"
        researcher = SimpleNamespace(
            retrievers=[],
            cfg=SimpleNamespace(),
            verbose=False,
            websocket=None,
        )
        conductor = ResearchConductor(researcher)
        work_items = conductor._normalize_work_items([], original)
        state = {"active": 0, "peak": 0}

        async def fake_plan(query, query_domains=None):
            conductor.research_work_items = work_items
            return [item.query for item in work_items]

        async def fake_process(query, scraped_data=None, query_domains=None):
            if "Market-daily regional evidence gap" in query:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                await asyncio.sleep(0.06)
                state["active"] -= 1
                return f"regional evidence for {query}"
            if "Research lane 3" in query:
                conductor._query_http_source_counts[query] = 0
                return ""
            conductor._query_http_source_counts[query] = 8
            return f"initial evidence for {query}"

        conductor.plan_research = fake_plan
        conductor._process_sub_query = fake_process
        with patch.dict(
            os.environ,
            {
                "RESEARCH_MIN_HTTP_SOURCES_PER_WORK_ITEM": "8",
                "RESEARCH_MIN_STOCK_SOURCES_PER_MARKET": "4",
                "MCP_RESEARCH_MIN_HTTP_SOURCES": "25",
            },
        ):
            context = await conductor._get_context_by_web_search(original)

        self.assertTrue(context)
        self.assertEqual(len(conductor.research_work_items), 3)
        self.assertEqual(len(conductor.gap_followup_queries), 3)
        self.assertEqual(state["peak"], 3)

    async def test_three_initial_codex_searches_overlap_without_fourth_original_query(self):
        state = {"active": 0, "peak": 0, "calls": 0}

        class CodexSearch:
            def __init__(self, query, query_domains=None):
                self.query = query
                self.run_history = []

            async def search_async(self, max_results=5):
                state["calls"] += 1
                call_number = state["calls"]
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                await asyncio.sleep(0.08)
                state["active"] -= 1
                self.run_history.append(
                    {
                        "pid": 1000 + call_number,
                        "attempt": 1,
                        "started_at": f"start-{call_number}",
                        "finished_at": f"finish-{call_number}",
                        "status": "completed",
                    }
                )
                suffix = abs(hash(self.query))
                return [
                    {
                        "href": f"https://codex.example/{suffix}",
                        "title": "Codex evidence",
                        "body": "source-backed evidence " * 20,
                        "raw_content": "source-backed evidence " * 20,
                    }
                ]

        class TavilySearch:
            def __init__(self, query, query_domains=None):
                self.query = query

            def search(self, max_results=5):
                suffix = abs(hash(self.query))
                return [
                    {
                        "href": f"https://tavily.example/{suffix}",
                        "title": "Tavily evidence",
                        "body": "independent evidence " * 20,
                        "raw_content": "independent evidence " * 20,
                    }
                ]

        class ContextManager:
            async def get_similar_content_by_query(self, query, data):
                return "\n".join(item["raw_content"] for item in data)

        class ScraperManager:
            async def browse_urls(self, urls):
                return []

        class FakeResearcher:
            def __init__(self):
                self.retrievers = [CodexSearch, TavilySearch]
                self.cfg = SimpleNamespace(max_search_results_per_query=5)
                self.verbose = False
                self.websocket = None
                self.visited_urls = set()
                self.research_sources = []
                self.context_manager = ContextManager()
                self.scraper_manager = ScraperManager()
                self.vector_store = None
                self.report_type = "research_report"
                self.query_domains = []

            def add_research_sources(self, sources):
                self.research_sources.extend(sources)

        conductor = ResearchConductor(FakeResearcher())

        async def fake_plan(query, query_domains=None):
            conductor.research_work_items = conductor._normalize_work_items(
                ["lane one", "lane two", "lane three", "lane four"], query
            )
            return [item.query for item in conductor.research_work_items]

        conductor.plan_research = fake_plan
        with patch.dict(
            os.environ,
            {
                "CODEX_SEARCH_RETRIEVER_CONCURRENCY": "3",
                "RESEARCH_MIN_HTTP_SOURCES_PER_WORK_ITEM": "1",
                "MCP_RESEARCH_MIN_HTTP_SOURCES": "1",
            },
        ):
            context = await conductor._get_context_by_web_search("original query")

        self.assertTrue(context)
        self.assertEqual(state["calls"], 3)
        self.assertEqual(state["peak"], 3)
        self.assertEqual(len(conductor.research_work_items), 3)
        self.assertEqual(conductor.gap_followup_queries, [])
        self.assertEqual(conductor.evidence_metrics["codex_initial_calls"], 3)
        self.assertEqual(conductor.evidence_metrics["active_codex_peak"], 3)
        self.assertEqual(conductor.evidence_metrics["codex_run_count"], 3)
        self.assertEqual(conductor.evidence_metrics["codex_pids"], [1001, 1002, 1003])


class CodexCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_transient_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            retriever = CodexSearch("fatal")
            retriever.slot_directory = Path(directory) / "slots"
            retriever.retries = 1
            retriever._run_helper = AsyncMock(
                return_value=(1, "", "invalid configuration", 1234)
            )

            results = await retriever.search_async()

            self.assertEqual(results, [])
            self.assertEqual(retriever._run_helper.await_count, 1)

    async def test_transient_failure_retries_exactly_once(self):
        with tempfile.TemporaryDirectory() as directory:
            retriever = CodexSearch("transient")
            retriever.slot_directory = Path(directory) / "slots"
            retriever.retries = 1
            retriever.retry_delay = 0
            payload = {
                "claims": [],
                "sources": [
                    {
                        "url": "https://example.com/recovered",
                        "title": "Recovered",
                        "summary": "valid evidence",
                    }
                ],
                "caveats": [],
            }
            retriever._run_helper = AsyncMock(
                side_effect=[
                    (1, "", "HTTP status 503", 1234),
                    (0, json.dumps(payload), "", 1235),
                ]
            )

            results = await retriever.search_async()

            self.assertEqual(results[0]["href"], "https://example.com/recovered")
            self.assertEqual(retriever._run_helper.await_count, 2)

    async def test_codex_specific_result_limit_keeps_multi_entity_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            retriever = CodexSearch("multi-entity market evidence")
            retriever.slot_directory = Path(directory) / "slots"
            retriever.retries = 0
            payload = {
                "claims": [],
                "sources": [
                    {
                        "url": f"https://example.com/source-{index}",
                        "title": f"Source {index}",
                        "summary": "verified evidence",
                    }
                    for index in range(10)
                ],
                "caveats": [],
            }
            retriever._run_helper = AsyncMock(
                return_value=(0, json.dumps(payload), "", 1234)
            )

            with patch.dict(os.environ, {"CODEX_SEARCH_MAX_RESULTS": "7"}):
                results = await retriever.search_async(max_results=2)

            self.assertEqual(len(results), 7)

    async def test_global_slot_pool_caps_twelve_calls_at_nine(self):
        with tempfile.TemporaryDirectory() as directory:
            state = {"active": 0, "peak": 0}

            async def use_slot():
                async with _GlobalCodexSlot(limit=9, directory=Path(directory)):
                    state["active"] += 1
                    state["peak"] = max(state["peak"], state["active"])
                    await asyncio.sleep(0.08)
                    state["active"] -= 1

            await asyncio.gather(*(use_slot() for _ in range(12)))

            self.assertEqual(state["peak"], 9)

    async def test_global_slot_is_exclusive_across_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            slot_directory = Path(directory) / "slots"
            acquired_marker = Path(directory) / "child-acquired"
            script = textwrap.dedent(
                """
                import asyncio
                import sys
                from pathlib import Path
                from gpt_researcher.retrievers.codex.codex import _GlobalCodexSlot

                async def main():
                    async with _GlobalCodexSlot(limit=1, directory=Path(sys.argv[1])):
                        Path(sys.argv[2]).write_text("acquired", encoding="utf-8")

                asyncio.run(main())
                """
            )

            async with _GlobalCodexSlot(limit=1, directory=slot_directory):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    script,
                    str(slot_directory),
                    str(acquired_marker),
                    cwd=str(Path(__file__).resolve().parents[1]),
                )
                await asyncio.sleep(0.2)
                self.assertFalse(acquired_marker.exists())

            await asyncio.wait_for(process.wait(), timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assertTrue(acquired_marker.exists())

    async def test_helper_reports_inner_codex_pid_and_slot_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                textwrap.dedent(
                    """
                    #!/usr/bin/env python3
                    import json
                    import sys
                    import time
                    from pathlib import Path

                    args = sys.argv[1:]
                    output = Path(args[args.index("--output-last-message") + 1])
                    output.write_text(json.dumps({
                        "claims": [{
                            "claim": "Verified fact",
                            "value": 1,
                            "unit": "point",
                            "as_of_date": "2026-07-09",
                            "source_urls": ["https://example.com/fact"],
                            "summary": "source-backed",
                        }],
                        "sources": [{
                            "url": "https://example.com/fact",
                            "title": "Example",
                            "summary": "source-backed",
                        }],
                        "caveats": [],
                    }))
                    time.sleep(0.1)
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            retriever = CodexSearch("telemetry")
            retriever.slot_directory = root / "slots"
            retriever.retries = 0
            with patch.dict(
                os.environ,
                {
                    "CODEX_SEARCH_CODEX_BIN": str(fake_codex),
                    "CODEX_SEARCH_USE_USER_CONFIG": "false",
                },
            ):
                results = await retriever.search_async()

            self.assertEqual(results[0]["href"], "https://example.com/fact")
            metadata = retriever.last_run_metadata
            self.assertIsInstance(metadata["helper_pid"], int)
            self.assertIsInstance(metadata["codex_pid"], int)
            self.assertNotEqual(metadata["helper_pid"], metadata["codex_pid"])
            self.assertIsNotNone(metadata["slot_acquired_at"])
            self.assertIsNotNone(metadata["slot_released_at"])
            self.assertIsNotNone(metadata["codex_started_at"])
            self.assertIsNotNone(metadata["codex_finished_at"])

    async def test_helper_timeout_terminates_inner_codex_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_pid_path = root / "inner-child.pid"
            fake_codex = root / "fake-codex"
            fake_codex.write_text(
                textwrap.dedent(
                    """
                    #!/usr/bin/env python3
                    import subprocess
                    import sys
                    import time
                    from pathlib import Path

                    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                    Path(__PID_FILE__).write_text(str(child.pid), encoding="utf-8")
                    time.sleep(60)
                    """
                )
                .replace("__PID_FILE__", repr(str(child_pid_path)))
                .lstrip(),
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            retriever = CodexSearch("timeout")
            retriever.slot_directory = root / "slots"
            retriever.timeout = 1
            retriever.retries = 0

            with patch.dict(
                os.environ,
                {"CODEX_SEARCH_CODEX_BIN": str(fake_codex)},
            ):
                results = await retriever.search_async()

            self.assertEqual(results, [])
            self.assertTrue(child_pid_path.exists())
            child_pid = int(child_pid_path.read_text(encoding="utf-8"))
            for _ in range(100):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.02)
            else:
                self.fail("Inner Codex child survived helper timeout")

    async def test_cancellation_terminates_helper_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child_pid_path = root / "child.pid"
            helper = root / "fake_helper.py"
            helper.write_text(
                textwrap.dedent(
                    """
                    import subprocess
                    import sys
                    import time
                    from pathlib import Path

                    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                    Path(__PID_FILE__).write_text(str(child.pid))
                    time.sleep(60)
                    """
                ).replace("__PID_FILE__", repr(str(child_pid_path))),
                encoding="utf-8",
            )
            retriever = CodexSearch("cancel me")
            retriever.helper_path = helper
            retriever.slot_directory = root / "slots"
            retriever.retries = 0

            task = asyncio.create_task(retriever.search_async())
            for _ in range(100):
                if child_pid_path.exists():
                    break
                await asyncio.sleep(0.02)
            self.assertTrue(child_pid_path.exists())
            child_pid = int(child_pid_path.read_text())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            self.assertEqual(retriever.last_run_metadata["status"], "cancelled")
            self.assertIsNotNone(retriever.last_run_metadata["pid"])
            self.assertEqual(retriever.last_run_metadata["slot"], 0)

            for _ in range(100):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.02)
            else:
                self.fail("Codex helper child survived cancellation")


if __name__ == "__main__":
    unittest.main()
